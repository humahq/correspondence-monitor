"""Capture: every message that reaches the bot is stored whole before any judgment."""

from __future__ import annotations

import email
import email.policy
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import getaddresses, parsedate_to_datetime
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from . import config, db, settings


@dataclass
class InAttachment:
    filename: str
    mime: str
    data: bytes
    component: str = "attachment"   # attachment | forwarded-message | inline-forward
    source_date: str | None = None
    text: str | None = None


@dataclass
class Inbound:
    channel: str                      # email | teams
    external_id: str
    thread_ref: str
    from_addr: str | None
    from_name: str | None
    subject: str | None
    body_text: str
    sent_at: str
    to_addrs: list[str] = field(default_factory=list)
    cc_addrs: list[str] = field(default_factory=list)
    delivered_to: list[str] = field(default_factory=list)
    body_html: str | None = None
    headers: dict = field(default_factory=dict)
    rfc_message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    attachments: list[InAttachment] = field(default_factory=list)
    raw: bytes | None = None
    study_id: str | None = None       # set by channel adapters that know it (Teams)
    visibility: str | None = None     # set by channel adapters that know it (Teams)
    reply_to_external: str | None = None  # Teams: id of the message this replies to


# ---------------------------------------------------------------- email parsing

def _iso(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def _date_header(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _iso(parsedate_to_datetime(value))
    except (TypeError, ValueError):
        return None


def _loose_date(value: str) -> str | None:
    """A date as mail clients print it in a forwarded block, e.g. "Tue, Jul 14, 2026 at 10:02 AM" (no zone:
    taken as the study's local time)."""
    found = _date_header(value)
    if found:
        return found
    try:
        dt = dtparser.parse(value.replace(" at ", " ").replace("\u202f", " "), fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(settings.get("system.timezone", "UTC")))
    return _iso(dt)


def _addrs(msg, name: str) -> list[str]:
    return [a.lower() for _, a in getaddresses(msg.get_all(name, [])) if a]


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    import html as h

    return re.sub(r"\n{3,}", "\n\n", h.unescape(text)).strip()


def _body(msg) -> tuple[str, str | None]:
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    html_s = html.get_content() if html else None
    if plain:
        return plain.get_content(), html_s
    if html_s:
        return _html_to_text(html_s), html_s
    return "", None


def attachment_text(filename: str, mime: str, data: bytes) -> str | None:
    name = (filename or "").lower()
    try:
        if mime == "application/pdf" or name.endswith(".pdf"):
            from pypdf import PdfReader

            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        if mime.startswith("text/") or name.endswith((".txt", ".md", ".csv")):
            return data.decode("utf-8", errors="replace")
        if name.endswith(".docx"):
            import zipfile

            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n"))
    except Exception as e:  # unreadable attachment is recorded, not fatal
        return f"[could not extract text: {e}]"
    return None


_FWD = re.compile(
    r"-{3,}\s*Forwarded message\s*-{3,}(?P<block>.*?)(?=\n-{3,}\s*Forwarded message|\Z)", re.S | re.I)


def inline_forwards(body: str) -> list[InAttachment]:
    """Forwarded messages pasted into the body keep their own date."""
    out = []
    for m in _FWD.finditer(body or ""):
        block = m.group("block")
        date = re.search(r"(?im)^\s*Date:\s*(.+)$", block)
        out.append(InAttachment(
            filename="forwarded-message.txt", mime="text/plain", data=block.encode(),
            component="inline-forward",
            source_date=_loose_date(date.group(1).strip()) if date else None,
            text=block.strip(),
        ))
    return out


def parse_email(raw: bytes, *, external_id: str | None = None, thread_ref: str | None = None) -> Inbound:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text, html = _body(msg)
    rfc_id = (msg.get("Message-ID") or "").strip() or None
    refs = (msg.get("References") or "").split()
    sent_at = _date_header(msg.get("Date")) or _iso(None)
    atts: list[InAttachment] = []
    for part in msg.iter_attachments():
        ctype = part.get_content_type()
        if ctype == "message/rfc822":
            inner = part.get_content()
            inner_text, _ = _body(inner)
            atts.append(InAttachment(
                filename=(inner.get("Subject") or "forwarded message") + ".eml", mime=ctype,
                data=inner.as_bytes(), component="forwarded-message",
                source_date=_date_header(inner.get("Date")),
                text=f"From: {inner.get('From')}\nDate: {inner.get('Date')}\nSubject: {inner.get('Subject')}\n\n{inner_text}",
            ))
            continue
        data = part.get_payload(decode=True) or b""
        fname = part.get_filename() or "attachment"
        atts.append(InAttachment(filename=fname, mime=ctype, data=data, component="attachment",
                                 source_date=sent_at, text=attachment_text(fname, ctype, data)))
    atts += inline_forwards(text)
    from_list = getaddresses([msg.get("From", "")])
    from_name, from_addr = from_list[0] if from_list else ("", "")
    delivered = _addrs(msg, "Delivered-To") + _addrs(msg, "X-Original-To")
    return Inbound(
        channel="email",
        external_id=external_id or rfc_id or f"sha:{hashlib.sha256(raw).hexdigest()[:32]}",
        thread_ref=thread_ref or (refs[0] if refs else rfc_id) or external_id or "",
        from_addr=(from_addr or "").lower() or None,
        from_name=from_name or None,
        subject=msg.get("Subject"),
        body_text=text,
        body_html=html,
        sent_at=sent_at,
        to_addrs=_addrs(msg, "To"),
        cc_addrs=_addrs(msg, "Cc"),
        delivered_to=delivered,
        headers={k: str(v) for k, v in msg.items()},
        rfc_message_id=rfc_id,
        in_reply_to=(msg.get("In-Reply-To") or "").strip() or None,
        references=refs,
        attachments=atts,
        raw=raw,
    )


# ---------------------------------------------------------------- study resolution

def _bot_parts() -> tuple[str, str]:
    local, domain = settings.get("mailbox.address", "bot@huma.ai").lower().split("@")
    return local, domain


def general_addresses() -> set[str]:
    return {a.lower() for a in (settings.get("mailbox.general_addresses") or [])}


def resolve_email_route(inb: Inbound) -> tuple[str | None, str | None, str]:
    """Return (study_id, alias the message arrived on, visibility)."""
    local, domain = _bot_parts()
    aliases = {k.lower(): v for k, v in (settings.get("mailbox.aliases") or {}).items()}
    general = general_addresses()

    def match(addr: str) -> tuple[bool, str | None]:
        a_local, _, a_domain = addr.partition("@")
        if addr in aliases:
            return True, aliases[addr]
        if addr in general:
            return True, None
        if a_domain != domain:
            return False, None
        if a_local == local:
            return True, None
        if a_local.startswith(local + "+"):
            s = config.study_for_tag(a_local[len(local) + 1:])
            return True, s.study_id if s else f"unknown-tag:{a_local[len(local) + 1:]}"
        return False, None

    found_study, found_alias, visibility = None, None, "bcc"
    for field_name, addrs in (("to", inb.to_addrs), ("cc", inb.cc_addrs), ("bcc", inb.delivered_to)):
        for addr in addrs:
            ok, sid = match(addr)
            if ok:
                if found_alias is None or (sid and not found_study):
                    found_alias, found_study = addr, sid
                    visibility = field_name
    if found_study and found_study.startswith("unknown-tag:"):
        return None, found_alias, visibility
    return found_study, found_alias, visibility


# ---------------------------------------------------------------- store

def _save_raw(channel: str, msg_id: int, raw: bytes | None) -> str | None:
    if raw is None:
        return None
    d = settings.data_dir() / "raw" / channel
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{msg_id}.{'eml' if channel == 'email' else 'json'}"
    p.write_bytes(raw)
    return str(p.relative_to(settings.data_dir()))


def _save_attachment(msg_id: int, idx: int, att: InAttachment) -> str:
    d = settings.data_dir() / "attachments" / str(msg_id)
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", att.filename or f"att{idx}")[:80]
    p = d / f"{idx:02d}_{safe}"
    p.write_bytes(att.data)
    return str(p.relative_to(settings.data_dir()))


def known_rfc_ids() -> set[str]:
    ids = {r["rfc_message_id"] for r in db.q("SELECT rfc_message_id FROM messages WHERE rfc_message_id IS NOT NULL")}
    ids |= {r["rfc_message_id"] for r in db.q("SELECT rfc_message_id FROM outbox WHERE rfc_message_id IS NOT NULL")}
    return ids


def capture(inb: Inbound) -> int | None:
    """Store a message. Returns its id, or None if it was already captured."""
    if db.q1("SELECT id FROM messages WHERE channel=? AND external_id=?", (inb.channel, inb.external_id)):
        return None

    if inb.channel == "email":
        study_id, alias, visibility = resolve_email_route(inb)
    else:
        study_id, alias, visibility = inb.study_id, None, inb.visibility or "channel"
    route = "study" if study_id else "general"

    thread_key = f"{inb.channel}:{inb.thread_ref}"
    # A reply to something we sent belongs to the thread of that outbound message.
    if inb.in_reply_to:
        sent = db.q1("SELECT meta FROM outbox WHERE rfc_message_id=?", (inb.in_reply_to,))
        if sent and db.unjs(sent["meta"], {}).get("thread_key"):
            thread_key = db.unjs(sent["meta"])["thread_key"]

    meta = dict(inb.headers)
    gap: list[str] = []
    if inb.channel == "email" and inb.references:
        prior = db.q1("SELECT COUNT(*) n FROM messages WHERE thread_key=?", (thread_key,))["n"]
        if prior:
            known = known_rfc_ids()
            gap = [r for r in inb.references if r not in known]
    if gap:
        meta["_cormon_gap_refs"] = gap
    if inb.reply_to_external:
        meta["_cormon_reply_to"] = inb.reply_to_external

    received = db.now()
    msg_id = db.ex(
        "INSERT INTO messages (channel, external_id, thread_key, study_id, route, visibility, recipient_alias,"
        " from_addr, from_name, to_addrs, cc_addrs, subject, body_text, body_html, headers, rfc_message_id,"
        " in_reply_to, refs, sent_at, received_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (inb.channel, inb.external_id, thread_key, study_id, route, visibility, alias,
         inb.from_addr, inb.from_name, db.js(inb.to_addrs), db.js(inb.cc_addrs), inb.subject,
         inb.body_text, inb.body_html, db.js(meta), inb.rfc_message_id, inb.in_reply_to,
         db.js(inb.references), inb.sent_at, received, "new"),
    )
    raw_path = _save_raw(inb.channel, msg_id, inb.raw)
    if raw_path:
        db.ex("UPDATE messages SET raw_path=? WHERE id=?", (raw_path, msg_id))

    for i, att in enumerate(inb.attachments):
        path = _save_attachment(msg_id, i, att)
        db.ex(
            "INSERT INTO attachments (message_id, filename, mime, size, path, text, component, source_date)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (msg_id, att.filename, att.mime, len(att.data), path, att.text, att.component,
             att.source_date or inb.sent_at),
        )

    existing = db.q1("SELECT thread_key FROM threads WHERE thread_key=?", (thread_key,))
    if existing:
        db.ex("UPDATE threads SET last_seen=? WHERE thread_key=?", (received, thread_key))
    else:
        db.ex("INSERT INTO threads(thread_key, channel, study_id, subject, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
              (thread_key, inb.channel, study_id, inb.subject, received, received))

    db.audit("message_captured", entity_type="message", entity_id=msg_id, study_id=study_id,
             channel=inb.channel, route=route, visibility=visibility, alias=alias,
             sender=inb.from_addr, subject=inb.subject, attachments=len(inb.attachments),
             thread_key=thread_key, gap_refs=gap or None)
    return msg_id


_CALLED = re.compile(r"^\W*(?:(?:hey|hi|hello|ok|okay)\s+)?(?:huma\s+)?bot\b", re.I)


def turn(msg: dict) -> str:
    """Whether a chat message is talking to the bot: "direct" (a 1-on-1 chat), "addressed" (in a group: it
    @mentions the bot, starts "bot, ...", or replies without @mentioning someone else in a thread the bot started
    or one that began by talking to the bot), "quiet" (other group chat), or "email"."""
    if msg.get("channel") != "teams":
        return "email"
    if msg.get("visibility") == "direct":
        return "direct"
    h = msg.get("headers") or {}
    if _to_bot(h, msg.get("body_text")):
        return "addressed"
    m = re.fullmatch(r"teams:(spaces/[^/]+)/threads/([^/]+)", msg.get("thread_key") or "")
    if not m or h.get("_cormon_mentions"):
        return "quiet"
    if db.q1("SELECT 1 FROM outbox WHERE channel='teams' AND external_ref=?", (f"{m[1]}/messages/{m[2]}.{m[2]}",)):
        return "addressed"
    first = db.q1("SELECT id, headers, body_text FROM messages WHERE thread_key=? ORDER BY id LIMIT 1",
                  (msg["thread_key"],))
    if first and first["id"] != msg.get("id") and _to_bot(db.unjs(first["headers"], {}), first["body_text"]):
        return "addressed"
    return "quiet"


def _to_bot(headers: dict, text: str | None) -> bool:
    return bool(headers.get("_cormon_to_bot") or _CALLED.match(text or ""))


def message(msg_id: int) -> dict:
    row = db.q1("SELECT * FROM messages WHERE id=?", (msg_id,))
    if row is None:
        raise KeyError(f"message {msg_id} not found")
    d = dict(row)
    for k in ("to_addrs", "cc_addrs", "refs", "headers"):
        d[k] = db.unjs(d[k], [] if k != "headers" else {})
    d["attachments"] = [dict(a) for a in db.q("SELECT * FROM attachments WHERE message_id=? ORDER BY id", (msg_id,))]
    return d


def thread_history(thread_key: str, before_id: int, limit: int = 8) -> list[dict]:
    rows = db.q("SELECT id FROM messages WHERE thread_key=? AND id<? ORDER BY id DESC LIMIT ?",
                (thread_key, before_id, limit))
    return [message(r["id"]) for r in reversed(rows)]

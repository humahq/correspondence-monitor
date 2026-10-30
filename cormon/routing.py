"""Turns a classified item into outbound messages according to config/routing.yaml."""

from __future__ import annotations

import hashlib
import re
from email.utils import make_msgid

from . import capture, careful, config, db, drafting, items, phi, settings
from .compliance import records
from .config import Person, Study

REPEAT_HOURS = 6


_INTERNAL = [
    ("a private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("an API key", r"\bsk-[A-Za-z0-9_-]{16,}|\bAIza[0-9A-Za-z_-]{30,}|\bya29\.[0-9A-Za-z_-]{20,}"),
    ("a password or key", r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token|client[_ -]?secret)"
                          r"\s*(?:is|[:=])\s*\S+"),
    ("a server path", r"(?<![\w.:/])/(?:opt|etc|home|var|usr|root|srv)/[\w.-]+/"),
    ("a secrets file", r"\bsecrets/[\w.-]+"),
    ("a setting's name", r"\b[A-Z][A-Z0-9]*_(?:API_KEY|SECRET|TOKEN|PASSWORD)\b"),
]
BLOCKED = "I can't share that. I only answer questions about the study from its documents."


def internal(text: str | None) -> list[str]:
    """What in an outgoing message looks like the bot's own workings (keys, passwords, server paths, settings)."""
    return [what for what, pat in _INTERNAL if re.search(pat, text or "")]


def enqueue(*, purpose: str, channel: str, destination: str, body: str, item_id: int | None = None,
            message_id: int | None = None, template_id: str | None = None, from_addr: str | None = None,
            to: list[str] | None = None, cc: list[str] | None = None, subject: str | None = None,
            meta: dict | None = None, status: str = "queued", study_id: str | None = None,
            again: bool = False) -> int:
    """Queue one outgoing message. The same draft queued again within REPEAT_HOURS (a message read a second
    time after a crash) returns the first row instead, unless `again` (an operator resending on purpose).
    Every chat post passes the careful check here (careful.py): in a room with a doctor it may be reworded, or
    kept out ('withheld') for routing to report privately."""
    idem = hashlib.sha256(db.js([item_id, message_id, purpose, channel, destination, to or [], cc or [],
                                 subject, body, (meta or {}).get("target")]).encode()).hexdigest()
    if not again:
        first = db.q1("SELECT id FROM outbox WHERE idem_key=? AND created_at>=? AND status!='failed'",
                      (idem, db.later(-60 * REPEAT_HOURS)))
        if first:
            db.audit("draft_repeat_skipped", entity_type="item" if item_id else "outbox",
                     entity_id=item_id or first["id"], study_id=study_id, outbox_id=first["id"], purpose=purpose)
            return first["id"]
    said = careful.check_post(study_id=study_id, purpose=purpose, body=body, item_id=item_id, message_id=message_id,
                              meta=meta) if channel == "teams" else None
    if said:
        body, meta, status = said["body"], dict(meta or {}, careful=said["note"]), said["status"] or status
    files = (meta or {}).get("files") or []
    found = internal(body) + internal(subject) + [w for f in files for w in internal(f.get("text"))]
    if found:  # whatever wrote it, nothing of the bot's own workings goes out
        db.audit("outgoing_blocked", entity_type="message" if message_id else "item", entity_id=message_id or item_id,
                 study_id=study_id, purpose=purpose, found=sorted(set(found)))
        body, subject = BLOCKED, (subject and "Message withheld")
        meta = {k: v for k, v in meta.items() if k != "files"} if files else meta
    keep = phi.documents_own(study_id)
    (body, in_body), (subject, in_subject) = phi.mask(body, keep), phi.mask(subject, keep)
    masked_files = [dict(f, text=phi.mask(f.get("text"), keep)[0]) for f in (meta or {}).get("files") or []]
    in_files = [k for f in (meta or {}).get("files") or [] for k in phi.mask(f.get("text"), keep)[1]]
    if masked_files:
        meta = dict(meta, files=masked_files)
    if in_body or in_subject or in_files:
        db.audit("outgoing_phi_masked", entity_type="message" if message_id else "item",
                 entity_id=message_id or item_id, study_id=study_id, purpose=purpose,
                 found=sorted(set(in_body + in_subject + in_files)))
    rfc_id = make_msgid(domain=settings.get("mailbox.address", "bot@huma.ai").split("@")[1]) \
        if channel == "email" else None
    to_json, cc_json = db.js(to or []), db.js(cc or [])
    oid = db.ex(
        "INSERT INTO outbox (item_id, message_id, purpose, channel, destination, template_id, from_addr, to_addrs,"
        " cc_addrs, subject, body, meta, status, rfc_message_id, created_at, idem_key, study_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, message_id, purpose, channel, destination, template_id, from_addr, to_json,
         cc_json, subject, body, db.js(meta or {}), status, rfc_id, db.now(), idem, study_id),
    )
    digest = records.put("outbound", "outbox", oid, records.sent_bytes(channel, destination, from_addr, to_json,
                                                                       cc_json, subject, body), None)
    db.ex("UPDATE outbox SET sha256=? WHERE id=?", (digest, oid))
    db.audit("draft_created", entity_type="item" if item_id else "outbox", entity_id=item_id or oid,
             study_id=study_id, outbox_id=oid, purpose=purpose, channel=channel, destination=destination,
             template=template_id, to=to, cc=cc, subject=subject, status=status)
    if said and said["action"] == "withhold":
        db.audit("post_withheld_careful", entity_type="item" if item_id else "outbox", entity_id=item_id or oid,
                 study_id=study_id, outbox_id=oid, room=said["note"]["room"], about=said["note"]["about"],
                 why=said["note"]["why"])
    return oid


def from_alias(study: Study, msg: dict) -> str:
    """The alias the message arrived on, spelled as configured: Gmail only honours a send-as address it
    recognises, and inbound addresses are stored lower-cased."""
    alias = msg.get("recipient_alias")
    if not alias or alias.lower() == study.alias.lower():
        return study.alias
    return alias


def _sender(study: Study, msg: dict) -> tuple[Person | None, str | None]:
    return study.person_by_email(msg.get("from_addr")), msg.get("from_addr")


def has_safety(msg: dict) -> bool:
    """SAF-10: the message reports harm to a participant (any part of it), so no notice goes back to its writer."""
    from . import events
    data = events.latest(msg["id"]) or {}
    if any(e.get("harm", "none") != "none" for e in data.get("events", [])):
        return True
    return bool(db.q1("SELECT 1 FROM items WHERE kind='out_of_scope' AND (first_message_id=? OR last_message_id=?)",
                      (msg["id"], msg["id"])))


def _resolve_people(study: Study, entry: dict, item: dict, msg: dict) -> list[tuple[str, Person | None, str]]:
    """Return [(recipient_axis, person, email)] for an email routing entry."""
    out = []
    if "to_role" in entry:
        people = study.people_with_role(entry["to_role"], item.get("site"))
        if entry["to_role"] == "cra" and item.get("recipient_person"):
            chosen = study.person(item["recipient_person"])
            if chosen:
                people = [chosen]
        for p in people:
            if p.email:
                out.append((entry["to_role"], p, p.email))
    elif entry.get("to") == "sender":
        person, addr = _sender(study, msg)
        need = entry.get("only_if_sender_role")
        if need and (person is None or need not in person.roles):
            return []
        if item["kind"] != "out_of_scope" and has_safety(msg):
            return []
        if addr:
            axis = next((r for r in (person.roles if person else []) if r), "sender")
            out.append((axis, person, addr))
    return out


def _cc(study: Study, entry: dict, item: dict, exclude: set[str]) -> list[str]:
    cc = []
    for role in entry.get("cc_roles", []):
        for p in study.people_with_role(role, item.get("site")):
            if p.email and p.email.lower() not in exclude:
                cc.append(p.email)
    return cc


def _routed_to(study: Study, entries: list[dict], item: dict, msg: dict) -> str:
    names = []
    for e in entries:
        if e["destination"] == "email":
            for axis, p, addr in _resolve_people(study, e, item, msg):
                label = study.role_label(axis) if axis in {r["id"] for r in study.roles} else axis
                names.append(f"{p.name if p else addr} ({label})")
        elif e["destination"] == "teams_alert_channel" and group_thread(msg):
            names.append("this chat")
        elif e["destination"] == "teams_alert_channel":
            room = (study.teams.get("alert_channel") or {}).get("name")
            names.append(f"the {room} room" if room else "the alert room")
        elif e["destination"] == "clearing_list":
            names.append("the CRA's to-do list")
    return ", ".join(dict.fromkeys(names)) or "nobody else (posted here only)"


def teams_target(study: Study, destination: str) -> dict:
    key = "study_channel" if destination == "teams_study_channel" else "alert_channel"
    return dict(study.teams.get(key) or {}, key=key)


def _room(target: dict) -> tuple | None:
    if target.get("space"):
        return ("space", target["space"])
    if target.get("team_id") and target.get("channel_id"):
        return ("channel", target["team_id"], target["channel_id"])
    return None


def _one_post_per_room(study: Study, entries: list[dict]) -> list[dict]:
    """When the study and alert channels are the same room, only the alert is posted there."""
    rooms = {e["destination"]: _room(teams_target(study, e["destination"]))
             for e in entries if e["destination"].startswith("teams_")}
    alert = rooms.get("teams_alert_channel")
    if alert and rooms.get("teams_study_channel") == alert:
        return [e for e in entries if e["destination"] != "teams_study_channel"]
    return entries


def _entries(rconf: dict, item: dict) -> list[dict]:
    if item["kind"] == "out_of_scope":
        return rconf.get("out_of_scope", [])
    if item["kind"] == "good_news":
        return rconf.get("good_news", [])
    return rconf.get("tiers", {}).get(item["tier"], [])


def _chat_posts(item: dict, turn: str, entries: list[dict]) -> list[dict]:
    """No group-space post for what was said to the bot 1-on-1 (the reply there shows it; emails still go), nor
    for chat news nobody asked the bot about (still listed)."""
    if turn == "direct" or (item["kind"] == "good_news" and turn == "quiet"):
        return [e for e in entries if not e["destination"].startswith("teams_")]
    return entries


def group_thread(msg: dict) -> dict | None:
    """The thread of a message in a group chat the bot was added to, where its chat post goes instead."""
    m = re.fullmatch(r"teams:((spaces/[^/]+)/threads/[^/]+)", msg.get("thread_key") or "")
    if msg.get("visibility") != "group" or not m:
        return None
    return {"space": m.group(2), "thread": m.group(1), "key": "group"}


def _in_group(msg: dict, entries: list[dict]) -> list[dict]:
    """A group chat's talk stays in it: one chat post there (the alert's, if any), none in the study's rooms."""
    if not group_thread(msg):
        return entries
    posts = [e for e in entries if e["destination"].startswith("teams_")]
    keep = next((e for e in posts if e["destination"] == "teams_alert_channel"), posts[0] if posts else None)
    return [e for e in entries if not e["destination"].startswith("teams_") or e is keep]


def route_items(item_ids: list[int]) -> list[int]:
    """One message's new problems: a single email per person and a single post, led by the most serious one.
    Each problem stays its own item (its own tier, to-do entry and clearing)."""
    rank = {"low": 0, "medium": 1, "immediate": 2}
    rows = [items.get(i) for i in item_ids]
    live = [r for r in rows if r["state"] != "suppressed" and r["counts"] and not r["routed"]]
    for r in rows:
        if r not in live:
            route_item(r["id"])  # records why it is skipped
    if not live:
        return []
    live.sort(key=lambda r: -rank[r["tier"]])
    return route_item(live[0]["id"], also=[r["id"] for r in live[1:]])


def route_item(item_id: int, *, force: bool = False, actor: str = "system", also: list[int] = ()) -> list[int]:
    item = items.get(item_id)
    study = config.study(item["study_id"])
    others = [items.get(i) for i in also]
    if item["state"] in ("suppressed",) or (not item["counts"] and item["kind"] != "out_of_scope"):
        db.audit("routing_skipped", entity_type="item", entity_id=item_id, study_id=study.study_id,
                 reason=f"state={item['state']} counts={item['counts']}")
        return []
    if item["routed"] and not force:
        return []
    rconf = config.routing()
    entries = _one_post_per_room(study, _entries(rconf, item))

    msg = capture.message(item["last_message_id"] if force else item["first_message_id"])
    entries = _in_group(msg, _chat_posts(item, capture.turn(msg), entries))
    routed_to = ", ".join(dict.fromkeys(
        n for it in [item] + others
        for n in _routed_to(study, _one_post_per_room(study, _entries(rconf, it)), it, msg).split(", ")))
    topic_default = "good_news" if item["kind"] == "good_news" else (item.get("pattern_class") or "*")
    created: list[int] = []
    sent_to: set[str] = set()

    def context(person):
        ctx = drafting.item_context(study, item, person, routed_to)
        if others:
            ctx.update(problems=drafting.bundle_problems(study, [item] + others), bundle=True)
        return ctx

    for it in others:  # their own to-do list and digest entries; the messages below carry them
        for e in _entries(rconf, it):
            if e["destination"] in ("clearing_list", "digest"):
                db.audit("routed", entity_type="item", entity_id=it["id"], study_id=study.study_id,
                         destination=e["destination"])
    for entry in entries:
        dest = entry["destination"]
        topic = entry.get("topic", topic_default)
        if dest in ("clearing_list", "digest"):
            db.audit("routed", entity_type="item", entity_id=item_id, study_id=study.study_id, destination=dest)
            continue
        if dest.startswith("teams_"):
            axis = entry.get("recipient", "study_team")
            tpl = drafting.select(topic, axis, "teams", item["tier"])
            _, body = drafting.render(tpl, context(None))
            target = group_thread(msg) or teams_target(study, dest)
            created.append(enqueue(
                purpose=f"teams_{axis}", channel="teams", destination=dest, body=body, item_id=item_id,
                template_id=tpl["id"], meta={"target": target, "thread_key": item["thread_key"],
                                                "item_ids": [item_id] + [it["id"] for it in others]},
                study_id=study.study_id, again=force and actor != "system"))
            continue
        if dest == "email":
            writer = _sender(study, msg)[0]
            if (entry.get("to") == "sender" and item["kind"] != "out_of_scope" and has_safety(msg)
                    and writer and entry.get("only_if_sender_role", next(iter(writer.roles), None)) in writer.roles):
                db.audit("site_notice_withheld_safety", entity_type="item", entity_id=item_id,
                         study_id=study.study_id, message_id=msg["id"])
            for axis, person, addr in _resolve_people(study, entry, item, msg):
                if addr.lower() in sent_to:
                    continue
                sent_to.add(addr.lower())
                tpl = drafting.select(topic, axis, "email", item["tier"])
                subject, body = drafting.render(tpl, context(person))
                cc = _cc(study, entry, item, {addr.lower()})
                meta = {"thread_key": item["thread_key"], "item_id": item_id,
                        "item_ids": [item_id] + [it["id"] for it in others]}
                if entry.get("to") == "sender" and msg.get("rfc_message_id"):
                    meta["in_reply_to"] = msg["rfc_message_id"]
                    meta["references"] = (msg.get("refs") or []) + [msg["rfc_message_id"]]
                    meta["gmail_thread"] = msg["thread_key"].split(":", 1)[1] if msg["channel"] == "email" else None
                status = "queued" if (item["send_draft"] or item["kind"] == "out_of_scope") else "held"
                created.append(enqueue(
                    purpose=f"email_{axis}", channel="email", destination="email", body=body,
                    subject=subject, item_id=item_id, template_id=tpl["id"],
                    from_addr=from_alias(study, msg), to=[addr], cc=cc, meta=meta,
                    status=status, study_id=study.study_id, again=force and actor != "system"))
            continue
        db.audit("routing_unknown_destination", entity_type="item", entity_id=item_id,
                 study_id=study.study_id, destination=dest)
    created += careful.report_privately(study, item, others, msg, created, context)

    for it in [item] + others:
        db.ex("UPDATE items SET routed=1, updated_at=? WHERE id=?", (db.now(), it["id"]))
        db.audit("item_routed", entity_type="item", entity_id=it["id"], study_id=study.study_id, actor=actor,
                 tier=it["tier"], routed_to=routed_to, outbox_ids=created, forced=force or None,
                 sent_with=[x["id"] for x in [item] + others if x["id"] != it["id"]] or None)
    return created


def copy_nudge(study: Study, msg: dict, first_writer: str) -> int | None:
    """Ask the first person who wrote while the monitor was off the thread to keep copying it."""
    if not first_writer or first_writer.lower() == settings.get("mailbox.address", "").lower():
        return None
    person = study.person_by_email(first_writer)
    tpl = drafting.select("copy_nudge", "*", "email", "*")
    ctx = {
        "study": {"id": study.study_id, "short_name": study.short_name, "title": study.title,
                  "alias": study.alias, "monitor_signature": study.monitor_signature},
        "finding": {"message_subject": msg.get("subject") or "(no subject)"},
        "recipient": drafting._person_ctx(person, "Hello"),
    }
    subject, body = drafting.render(tpl, ctx)
    meta = {"thread_key": msg["thread_key"], "in_reply_to": msg.get("rfc_message_id"),
            "references": (msg.get("refs") or []) + ([msg["rfc_message_id"]] if msg.get("rfc_message_id") else []),
            "gmail_thread": msg["thread_key"].split(":", 1)[1]}
    return enqueue(purpose="copy_nudge", channel="email", destination="email", body=body, subject=subject,
                   message_id=msg["id"], template_id=tpl["id"],
                   from_addr=from_alias(study, msg), to=[first_writer], meta=meta,
                   study_id=study.study_id)

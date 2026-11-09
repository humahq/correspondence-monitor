"""Read-only admin pages for the people who run the bot (web.admin.emails): is it healthy, every message with its whole
conversation and what the bot did, every alert, who has signed in or written, and what patient memory holds. A small
React app (cormon/web) draws the pages from the JSON answers here. The only change these pages make is an admin's
correction to patient memory (memory_change), each with a reason in the audit trail."""

from __future__ import annotations

import html
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import capture, config, coverage, db, outbox, senders, settings
from .drafting import local_time
from .triage import TIER_LABELS

PAGE = 50
WEB = Path(__file__).parent / "web"
PRODUCT = "OpenBook"   # the product's name (openbook.phd); the bot inside it has its own name (bot.name)
# In load order: React and htm before the app.
STATIC = {"admin.css": "text/css", "vendor/react.production.min.js": "text/javascript",
          "vendor/react-dom.production.min.js": "text/javascript", "vendor/htm.umd.js": "text/javascript",
          "app.js": "text/javascript"}

# What each "nothing to raise" decision means, in plain words.
OUTCOMES = {
    "not_trial_related": "Not about the trial",
    "general_message": "General message, nothing to check",
    "general_question_no_alert": "A question, not a problem",
    "hypothetical_question_no_alert": "A question, not a problem",
    "historical_reference_no_alert": "About something already handled",
    "earlier_fact_already_raised": "Already raised before",
    "question_answered": "Answered a question",
    "question_left_to_people": "Left the question to the study team",
    "question_answer_failed": "Could not answer the question",
    "request_declined": "Declined a request",
    "instruction_ignored_unverified_sender": "Ignored an instruction: the sender check failed",
    "instruction_not_applied": "An instruction was not applied",
    "outgoing_blocked": "A reply was held back",
    "processing_error": "Error while reading",
    "error_resolved": "An earlier error was looked at and resolved",
}

CHANNEL = {"email": "Email", "teams": "Chat"}
STATUS = {"new": "Waiting", "processing": "Reading now", "processed": "Done", "error": "Error"}
STATE = {"open": "Open", "cleared": "Cleared", "suppressed": "Disregarded", "out_of_scope": "Not reviewed (safety)"}
PURPOSE = {"chat_received": "Quick receipt", "question_answer": "Answer", "chat_acknowledgement": "Reply in chat",
           "teams_study_team": "Alert in the study chat", "teams_study_channel": "Alert in the study chat",
           "email_cra": "Alert email to the CRA", "email_sponsor": "Email to the sponsor", "digest": "Daily digest",
           "ops_warning": "Warning to the bot's operators", "copy_nudge": "Reminder to copy the bot"}

# What the bot sends back into the conversation itself; everything else (alerts, operator notes) goes elsewhere.
REPLIES = {"chat_received", "question_answer", "chat_acknowledgement", "copy_nudge"}

WHERE = {("teams", "channel"): "Study group chat", ("teams", "direct"): "1-on-1 chat with the bot",
         ("teams", "group"): "Other group chat", ("email", "to"): "Email to the bot",
         ("email", "cc"): "Email, bot copied", ("email", "bcc"): "Email, bot blind-copied"}


def _where(channel: str, visibility: str) -> str:
    return WHERE.get((channel, visibility), f"{CHANNEL.get(channel, channel)} ({visibility})")


def _dest(d: str) -> str:
    return "email" if d == "email" else "study chat" if d.startswith("teams") else d.replace("_", " ")


_loaded: dict = {"at": 0.0, "studies": {}}


def _studies() -> dict:
    """The study files, read at most every 30 seconds: a page uses them once per row."""
    if time.monotonic() - _loaded["at"] > 30:
        _loaded["studies"] = config.studies()
        _loaded["at"] = time.monotonic()
    return _loaded["studies"]


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.get("system.timezone", "America/New_York"))


def _day_start(d: date) -> str:
    """UTC time of local midnight starting day `d`, in the same form as the database's times."""
    return datetime(d.year, d.month, d.day, tzinfo=_tz()).astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_day(iso: str | None) -> date | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_tz()).date() if iso else None
    except ValueError:
        return None


def _day_label(d: date) -> str:
    today = datetime.now(_tz()).date()
    return "Today" if d == today else "Yesterday" if d == today - timedelta(days=1) else f"{d:%a} {d.day} {d:%b %Y}"


def period(query: dict, default: str = "7") -> dict:
    """The days a page covers, in local days: one chosen day (date=YYYY-MM-DD), or days= 1 (today), yesterday,
    a number of days up to today, or all. "from"/"to" are the UTC bounds; None means no bound."""
    today = datetime.now(_tz()).date()
    try:
        chosen = date.fromisoformat(query.get("date", "")) if query.get("date") else None
    except ValueError:
        chosen = None
    days = query.get("days", default)
    if chosen:
        first = last = chosen
    elif days == "yesterday":
        first = last = today - timedelta(days=1)
    elif days.isdigit() and int(days) >= 1:
        first, last = today - timedelta(days=min(int(days), 3660) - 1), today
    else:
        return {"from": None, "to": None, "first": None, "last": today, "label": "All time", "single": False}
    label = _day_label(first) if first == last else f"Last {(last - first).days + 1} days"
    return {"from": _day_start(first), "to": _day_start(last + timedelta(days=1)), "first": first, "last": last,
            "label": label, "single": first == last}


def _between(column: str, p: dict) -> tuple[str, list]:
    if p["from"] is None:
        return "1=1", []
    return f"{column}>=? AND {column}<?", [p["from"], p["to"]]


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if secs >= size:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


def _when(iso: str | None) -> dict:
    return {"at": iso, "text": local_time(iso) if iso else "", "ago": _ago(iso)}


def _name(study_id: str | None, pid: str | None) -> str:
    study = _studies().get(study_id)
    p = study.person(pid) if study and pid else None
    return p.name if p else (pid or "nobody")


def alert_type(item: dict) -> dict:
    """Patient safety, good news, or the study area of a protocol problem (the study's pattern class label)."""
    if item["kind"] == "out_of_scope":
        return {"label": "Patient safety", "kind": "safety"}
    if item["kind"] == "good_news":
        return {"label": "Good news", "kind": "good"}
    study = _studies().get(item["study_id"])
    area = study.pattern(item["pattern_class"]) if study and item["pattern_class"] else None
    return {"label": area["label"] if area else "Protocol problem", "kind": "protocol"}


_sends: dict = {"key": None, "by_item": {}}


def deliveries() -> dict[int, list[dict]]:
    """Every outgoing message per alert, from the outbox. One email or chat post can carry several alerts of a
    message (meta item_ids), so it is listed under each of them. Rebuilt only when the outbox changes."""
    key = tuple(db.q1("SELECT MAX(id) AS m, COUNT(*) AS n, SUM(status='sent') AS s FROM outbox"))
    if key != _sends["key"]:
        by_item: dict[int, list[dict]] = {}
        for o in db.q("SELECT * FROM outbox WHERE purpose!='digest' ORDER BY id"):
            ids = {o["item_id"]} | set(db.unjs(o["meta"], {}).get("item_ids") or [])
            row = {"channel": "email" if o["channel"] == "email" else "chat", "destination": o["destination"],
                   "to": [a.lower() for a in db.unjs(o["to_addrs"], [])],
                   "cc": [a.lower() for a in db.unjs(o["cc_addrs"], [])], "status": o["status"]}
            for i in ids - {None}:
                by_item.setdefault(int(i), []).append(row)
        _sends.update(key=key, by_item=by_item)
    return _sends["by_item"]


def _who(study, addr: str) -> dict:
    p = study.person_by_email(addr) if study else None
    return {"name": p.name if p else addr, "email": addr,
            "role": ", ".join(study.role_label(r) for r in p.roles) if p else "not in the study files"}


def _sent_to(a: dict) -> dict:
    """Who got this alert: each person once (main recipient before copy), whether it went to a chat room, and
    how many of its messages are not sent (waiting for approval or failed)."""
    study = _studies().get(a["study_id"])
    rows = deliveries().get(a["id"], [])
    people: dict[str, dict] = {}
    for r in rows:
        if r["status"] != "sent" or r["channel"] != "email":
            continue
        for how, addrs in (("to", r["to"]), ("copy", r["cc"])):
            for addr in addrs:
                if addr not in people or (how == "to" and people[addr]["how"] == "copy"):
                    people[addr] = dict(_who(study, addr), how=how)
    chats = sorted({_dest(r["destination"]) for r in rows if r["status"] == "sent" and r["channel"] == "chat"})
    return {"people": sorted(people.values(), key=lambda p: p["how"] != "to"), "chats": chats,
            "not_sent": sum(r["status"] != "sent" for r in rows)}


def _alert(a: dict) -> dict:
    study = _studies().get(a["study_id"])
    return {"id": a["id"], "link": f"/items/{a['link_token']}", "title": a["title"], "type": alert_type(a),
            "tier": a["tier"], "tier_label": TIER_LABELS.get(a["tier"], a["tier"]),
            "state": STATE.get(a["state"], a["state"]), "counts": bool(a["counts"]),
            "site": study.site_name(a["site"]) if study else a["site"], "participant": a["participant_id"],
            "sent": _sent_to(a), "opened": _when(a["created_at"]), "message_id": a["first_message_id"]}


# ---------------------------------------------------------------- what the bot did with a message

def outcome(msg: dict) -> list[dict]:
    """What the bot did with one message: alerts it made, answers, or why nothing was needed."""
    if msg["status"] == "error":
        return [{"kind": "error", "text": "Error: " + (msg.get("error") or "")[:160]}]
    if msg["status"] in ("new", "processing"):
        return [{"kind": "waiting", "text": STATUS[msg["status"]]}]
    said = []
    for it in db.q("SELECT * FROM items WHERE first_message_id=? OR last_message_id=? ORDER BY id",
                   (msg["id"], msg["id"])):
        verb = "New alert" if it["first_message_id"] == msg["id"] else "Added to alert"
        said.append({"kind": "alert", "text": f"{verb} #{it['id']}", "alert": _alert(dict(it))})
    seen = set()
    log = [r["action"] for r in db.q("SELECT action FROM audit_log WHERE entity_type='message' AND entity_id=?"
                                     " ORDER BY id", (str(msg["id"]),))]
    if "error_resolved" in log:
        seen.add(OUTCOMES["processing_error"])
    for action in log:
        words = OUTCOMES.get(action)
        if words and words not in seen:
            seen.add(words)
            said.append({"kind": "error" if action == "processing_error" else "note", "text": words})
    sent = [r["purpose"] for r in db.q("SELECT purpose FROM outbox WHERE message_id=? AND status='sent'"
                                       " AND purpose!='chat_received'", (msg["id"],))]
    replies = sum(p in REPLIES for p in sent)
    if replies:
        said.append({"kind": "note", "text": f"{replies} repl{'y' if replies == 1 else 'ies'} sent"})
    if len(sent) > replies:
        n = len(sent) - replies
        said.append({"kind": "note", "text": f"{n} alert message{'' if n == 1 else 's'} sent elsewhere"})
    return said or [{"kind": "note", "text": "Checked: nothing to raise"}]


# ---------------------------------------------------------------- home

def _problems() -> list[dict]:
    out = []
    for r in db.q("SELECT id, error FROM messages WHERE status='error' ORDER BY id DESC LIMIT 20"):
        out.append({"text": f"Message {r['id']} could not be read: {(r['error'] or '')[:160]}",
                    "link": f"/admin/messages/{r['id']}"})
    for r in db.q("SELECT id FROM messages WHERE status IN ('new','processing') AND received_at<?", (db.later(-15),)):
        out.append({"text": f"Message {r['id']} has waited more than 15 minutes.", "link": f"/admin/messages/{r['id']}"})
    for r in db.q("SELECT id, purpose, last_error FROM outbox WHERE status='failed' ORDER BY id DESC LIMIT 20"):
        out.append({"text": f"Outgoing message {r['id']} ({PURPOSE.get(r['purpose'], r['purpose'])}) was not sent: "
                            f"{(r['last_error'] or '')[:160]}", "link": None})
    for r in db.q("SELECT id, link_token FROM items WHERE routed=0 AND state!='suppressed' AND counts=1"
                  " AND created_at<?", (db.later(-15),)):
        out.append({"text": f"Alert #{r['id']} was made but not sent yet.", "link": f"/items/{r['link_token']}"})
    for r in db.q("SELECT i.* FROM items i JOIN audit_log a ON a.entity_type='item'"
                  " AND a.entity_id=CAST(i.id AS TEXT) AND a.action='item_held_off_roster'"
                  " WHERE i.state='open' AND a.ts>=? GROUP BY i.id ORDER BY i.id DESC LIMIT 20", (db.later(-30 * 24 * 60),)):
        if who := senders.item_off_roster(dict(r)):
            out.append({"text": f"Alert #{r['id']} is held: the sender ({who}) is not on the study's people list. "
                                f"Add them and run `cormon draft {r['id']} --force`, or leave it.",
                        "link": f"/admin/messages/{r['first_message_id']}"})
    for st in _studies().values():
        g = coverage.last(st.study_id)
        if g and g["warnings"]:
            out.append({"text": f"Study {st.study_id}: the document check has warnings: {'; '.join(g['warnings'])}",
                        "link": None})
    return out


ANSWERED = ("EXISTS (SELECT 1 FROM audit_log a WHERE a.entity_type='message' AND a.entity_id=CAST(messages.id AS TEXT)"
            " AND a.action='question_answered')")
MAX_DAYS = 90
COUNTS = ("messages", "email", "chat", "problems", "safety", "answered", "errors")


def activity(p: dict) -> dict:
    """What came in and what the bot did in a period: totals, and one row per local day (newest first, at most
    MAX_DAYS rows, quiet days included so a gap shows)."""
    by_day: dict[date, dict] = {}

    def add(iso, *keys):
        d = _local_day(iso)
        if d is None:
            return
        row = by_day.setdefault(d, dict.fromkeys(COUNTS, 0))
        for k in keys:
            row[k] += 1

    # Each count uses the same filter as the page its number opens (messages(), alerts()), so the two agree.
    where, params = _between("received_at", p)
    for m in db.q(f"SELECT received_at, channel, status, {ANSWERED} AS answered FROM messages WHERE {where}",
                  params):
        add(m["received_at"], "messages", "email" if m["channel"] == "email" else "chat",
            *(["errors"] if m["status"] == "error" else []), *(["answered"] if m["answered"] else []))
    where, params = _between("created_at", p)
    for it in db.q(f"SELECT created_at, kind FROM items WHERE {where}", params):
        if it["kind"] == "out_of_scope":
            add(it["created_at"], "safety")
        elif it["kind"] == "finding":
            add(it["created_at"], "problems")

    totals = dict.fromkeys(COUNTS, 0)
    for row in by_day.values():
        for k in COUNTS:
            totals[k] += row[k]
    days, capped = [], False
    if not p["single"]:
        first = p["first"] or min(by_day, default=p["last"])
        if (p["last"] - first).days + 1 > MAX_DAYS:
            first, capped = p["last"] - timedelta(days=MAX_DAYS - 1), True
        d = p["last"]
        while d >= first:
            days.append({"date": d.isoformat(), "label": _day_label(d), **by_day.get(d, dict.fromkeys(COUNTS, 0))})
            d -= timedelta(days=1)
    return {"totals": totals, "days": days, "capped": capped}


def home(query: dict | None = None) -> dict:
    beat = db.get_state("heartbeat")
    fresh = False
    if beat:
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 300
        except ValueError:
            pass
    p = period(query or {}, "7")
    return {
        "bot": {"state": "paused" if outbox.is_paused() else "running" if fresh else "down",
                "checkin": _when(beat), "last_message": _when(db.q1("SELECT MAX(received_at) AS at FROM messages")["at"]),
                "last_push": _when(db.get_state("gchat_event_at")),
                "email": settings.get("channels.email"), "chat": settings.get("channels.teams")},
        "period": {"label": p["label"], "single": p["single"],
                   "date": p["first"].isoformat() if p["single"] else None},
        **activity(p),
        "open_now": db.q1("SELECT COUNT(*) AS n FROM items WHERE state='open' AND counts=1")["n"],
        "open_by_tier": [{"tier": r["tier"], "label": TIER_LABELS.get(r["tier"], r["tier"]), "n": r["n"]} for r in db.q(
            "SELECT tier, COUNT(*) n FROM items WHERE state='open' AND counts=1 GROUP BY tier")],
        "problems": _problems(),
        "team": team(),
    }


# ---------------------------------------------------------------- messages and conversations

def options() -> dict:
    areas = {p["id"]: p["label"] for s in _studies().values() for p in s.pattern_classes}
    return {"studies": list(_studies()),
            "types": [{"value": "", "label": "Any type"}, {"value": "safety", "label": "Patient safety"},
                      {"value": "protocol", "label": "Any protocol problem"}]
            + [{"value": f"area:{k}", "label": v} for k, v in areas.items()]
            + [{"value": "good_news", "label": "Good news"}],
            "tiers": [{"value": "", "label": "Any urgency"}] + [{"value": k, "label": v} for k, v in TIER_LABELS.items()],
            "people": [{"value": "", "label": "Sent to anyone"}] + _recipients()
            + [{"value": "nobody", "label": "Sent to nobody"}]}


def _alerts_for(person: str) -> list[int]:
    """Alerts emailed to an address (main recipient or copy, sent). For "nobody": alerts with anything sent at
    all, which the caller leaves out."""
    if person == "nobody":
        return sorted(i for i, rows in deliveries().items() if any(r["status"] == "sent" for r in rows))
    addr = person.lower()
    return sorted(i for i, rows in deliveries().items()
                  if any(r["status"] == "sent" and addr in r["to"] + r["cc"] for r in rows))


def _recipients() -> list[dict]:
    """Everyone with an email in the study files, plus any other address an alert was emailed to."""
    known = {p.email.lower(): f"{p.name} ({', '.join(s.role_label(r) for r in p.roles)})"
             for s in _studies().values() for p in s.people if p.email}
    for rows in deliveries().values():
        for r in rows:
            if r["channel"] == "email" and r["status"] == "sent":
                for addr in r["to"] + r["cc"]:
                    known.setdefault(addr, f"{addr} (not in the study files)")
    return [{"value": k, "label": v} for k, v in sorted(known.items(), key=lambda x: x[1].lower())]


def team() -> list[dict]:
    """Each study's sponsor, sites and people exactly as the study file lists them; the study with the latest
    message first."""
    latest = {r["study_id"]: r["at"] for r in db.q("SELECT study_id, MAX(received_at) AS at FROM messages"
                                                    " GROUP BY study_id")}
    out = []
    for s in sorted(_studies().values(), key=lambda s: latest.get(s.study_id) or "", reverse=True):
        sites = {x["id"]: x["name"] for x in s.sites}
        out.append({"study": s.study_id, "short_name": s.short_name, "title": s.title, "sponsor": s.sponsor,
                    "sites": list(sites.values()),
                    "people": [{"name": p.name, "email": p.email, "roles": [s.role_label(r) for r in p.roles],
                                "sites": "All sites" if "*" in p.sites else ", ".join(sites.get(x, x) for x in p.sites)}
                               for p in s.people]})
    return out


def _row(m: dict) -> dict:
    return {"id": m["id"], "received": _when(m["received_at"]), "channel": CHANNEL.get(m["channel"], m["channel"]),
            "where": _where(m["channel"], m["visibility"]),
            "from": m["from_name"] or m["from_addr"], "from_addr": m["from_addr"],
            "study": m["study_id"], "subject": m["subject"],
            "summary": m["subject"] or (m["body_text"] or "")[:120] or "(no text)",
            "status": STATUS.get(m["status"], m["status"]), "outcome": outcome(m)}


def messages(query: dict) -> dict:
    channel, study, before = query.get("channel", ""), query.get("study", ""), query.get("before", "")
    where, params = _between("received_at", period(query, "7"))
    sql = f"SELECT * FROM messages WHERE {where}"
    if query.get("did") == "answered":
        sql += f" AND {ANSWERED}"
    elif query.get("did") == "error":
        sql += " AND status='error'"
    if channel in CHANNEL:
        sql += " AND channel=?"; params.append(channel)
    if study:
        sql += " AND study_id=?"; params.append(study)
    if before.isdigit():
        sql += " AND id<?"; params.append(int(before))
    rows = [dict(r) for r in db.q(sql + " ORDER BY id DESC LIMIT ?", (*params, PAGE + 1))]
    return {"rows": [_row(m) for m in rows[:PAGE]], "more": len(rows) > PAGE}


def conversation(m: dict) -> list[dict]:
    """The whole thread in time order: every message people wrote in it and everything the bot sent about it."""
    msgs = [dict(r) for r in db.q("SELECT * FROM messages WHERE thread_key=? ORDER BY id DESC LIMIT 200",
                                  (m["thread_key"],))]
    ids = [x["id"] for x in msgs]
    item_ids = [r["id"] for r in db.q("SELECT id FROM items WHERE thread_key=?", (m["thread_key"],))]
    out = [{"side": "person", "who": x["from_name"] or x["from_addr"], "when": _when(x["sent_at"] or x["received_at"]),
            "text": x["body_text"] or "(the text is no longer kept)", "subject": x["subject"],
            "channel": CHANNEL.get(x["channel"], x["channel"]), "message_id": x["id"], "current": x["id"] == m["id"],
            "outcome": outcome(x)} for x in msgs]
    where = [f"message_id IN ({','.join('?' * len(ids))})"] if ids else []
    if item_ids:
        where.append(f"item_id IN ({','.join('?' * len(item_ids))})")
    notices: dict = {}
    if where:
        for o in db.q(f"SELECT * FROM outbox WHERE ({' OR '.join(where)}) AND purpose!='digest' ORDER BY id",
                      (*ids, *item_ids)):
            to = ", ".join(db.unjs(o["to_addrs"], []))
            sent = {"when": _when(o["sent_at"] or o["created_at"]), "text": _plain(o["body"]), "subject": o["subject"],
                    "kind": PURPOSE.get(o["purpose"], o["purpose"]), "channel": "email" if o["channel"] == "email"
                    else "chat", "to": to or _dest(o["destination"]), "status": o["status"]}
            if o["purpose"] in REPLIES:
                out.append({"side": "bot", "who": settings.bot_name(), "small": o["purpose"] == "chat_received",
                            **sent})
                continue
            key = o["item_id"] or f"o{o['id']}"
            if key not in notices:
                item = db.q1("SELECT * FROM items WHERE id=?", (o["item_id"],)) if o["item_id"] else None
                notices[key] = {"side": "notice", "when": sent["when"], "alert": _alert(dict(item)) if item else None,
                                "sent": []}
                out.append(notices[key])
            notices[key]["sent"].append(sent)
    return sorted(out, key=lambda e: e["when"]["at"] or "")


def _plain(text: str | None) -> str:
    """A bot post as people saw it: chat posts are stored as HTML and emails mark bold with **. HTML links become
    <address|words>, the chat form of a link, which the page shows as a link."""
    text = text or ""
    if re.search(r"</?(p|b|i|br|ul|li|a)\b", text):
        text = re.sub(r"""<a\s[^>]*href=["'](https?://[^"']+)["'][^>]*>(.*?)</a>""",
                      lambda m: f"\x00{m[1]}|{re.sub(r'<[^>]+>', '', m[2]) or m[1]}\x01", text, flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text)
        text = re.sub(r"<li[^>]*>", "• ", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\x00", "<").replace("\x01", ">")
    return re.sub(r"\n{3,}", "\n\n", text.replace("**", "")).strip()


def message(msg_id: int) -> dict | None:
    try:
        m = capture.message(msg_id)
    except KeyError:
        return None
    alerts = [_alert(dict(a)) for a in db.q("SELECT * FROM items WHERE first_message_id=? OR last_message_id=?"
                                            " ORDER BY id", (msg_id, msg_id))]
    t = db.q1("SELECT * FROM message_timings WHERE message_id=? ORDER BY id DESC LIMIT 1", (msg_id,))
    steps = db.unjs(t["steps"], {}) if t else {}
    return {
        "message": dict(_row(m), to=m["to_addrs"], cc=m["cc_addrs"], sent=_when(m["sent_at"]),
                        text=m["body_text"] or "(the text is no longer kept)",
                        attachments=[{"name": a["filename"], "part": a["component"]} for a in m["attachments"]]),
        "alerts": alerts,
        "conversation": conversation(m),
        "answers": [{"link": f"/answers/{p['link_token']}", "when": _when(p["created_at"])} for p in db.q(
            "SELECT link_token, created_at FROM answer_pages WHERE message_id=? ORDER BY id", (msg_id,))],
        "speed": {"seconds": round(t["seconds"], 1) if t and t["seconds"] is not None else None,
                  "steps": [{"name": k, "seconds": round(v, 1)} for k, v in steps.items() if v >= 0.5]},
        "log": [{"when": _when(r["ts"]), "who": r["actor"], "what": r["action"].replace("_", " ")} for r in db.q(
            "SELECT ts, actor, action FROM audit_log WHERE entity_type='message' AND entity_id=? ORDER BY id",
            (str(msg_id),))],
    }


# ---------------------------------------------------------------- alerts

def alerts(query: dict) -> dict:
    state, tier, kind, before = (query.get(k, d) for k, d in (("state", "open"), ("tier", ""), ("type", ""),
                                                               ("before", "")))
    where, params = _between("created_at", period(query, "all"))
    sql = f"SELECT * FROM items WHERE {where}"
    if state == "open" and kind == "safety":
        sql += " AND state='out_of_scope'"
    elif state == "open":
        sql += " AND state='open' AND counts=1"
    elif state in STATE:
        sql += " AND state=?"; params.append(state)
    if tier in TIER_LABELS:
        sql += " AND tier=?"; params.append(tier)
    if kind == "safety":
        sql += " AND kind='out_of_scope'"
    elif kind == "good_news":
        sql += " AND kind='good_news'"
    elif kind == "protocol":
        sql += " AND kind='finding'"
    elif kind.startswith("area:"):
        sql += " AND kind='finding' AND pattern_class=?"; params.append(kind[5:])
    person = query.get("person", "")
    if person:
        ids = _alerts_for(person)
        if ids or person != "nobody":
            sql += f" AND id {'NOT ' if person == 'nobody' else ''}IN ({','.join('?' * len(ids)) or 'NULL'})"
            params += ids
    if before.isdigit():
        sql += " AND id<?"; params.append(int(before))
    rows = [dict(r) for r in db.q(sql + " ORDER BY id DESC LIMIT ?", (*params, PAGE + 1))]
    return {"rows": [_alert(a) for a in rows[:PAGE]], "more": len(rows) > PAGE}


# ---------------------------------------------------------------- sign-ins and use

SIGN_IN_WORDS = {"signed_in": "Signed in", "page_viewed": "Opened a page", "page_refused": "Refused a page",
                 "admin_viewed": "Opened an admin page", "admin_refused": "Refused an admin page",
                 "sign_in_failed": "Sign-in failed", "sign_in_refused": "Sign-in refused"}


def people() -> dict:
    acts = tuple(SIGN_IN_WORDS)
    marks = ",".join("?" * len(acts))
    web: dict = {}
    for r in db.q(f"SELECT action, details, ts FROM audit_log WHERE action IN ({marks}) ORDER BY id", acts):
        who = (db.unjs(r["details"], {}).get("email") or "unknown").lower()
        w = web.setdefault(who, {"signed_in": 0, "pages": 0, "last": None})
        w["signed_in"] += r["action"] == "signed_in"
        w["pages"] += r["action"] in ("page_viewed", "admin_viewed")
        w["last"] = r["ts"]
    wrote = {(r["who"] or "").lower(): dict(r) for r in db.q(
        "SELECT lower(from_addr) AS who, COUNT(*) AS n, MAX(received_at) AS last FROM messages GROUP BY lower(from_addr)")}
    by_email: dict = {}
    for st in _studies().values():
        for p in st.people:
            if not p.email:
                continue
            e = p.email.lower()
            row = by_email.setdefault(e, {"name": p.name, "email": e, "studies": [], "roles": []})
            row["studies"].append(st.study_id)
            row["roles"] += [st.role_label(r) for r in p.roles if st.role_label(r) not in row["roles"]]
    got: dict = {}
    for o in db.q("SELECT purpose, to_addrs, cc_addrs FROM outbox WHERE channel='email' AND status='sent'"):
        kind = "summaries" if o["purpose"] == "digest" else "emails"
        for how, addrs in (("", o["to_addrs"]), ("_copy", o["cc_addrs"])):
            for a in {x.lower() for x in db.unjs(addrs, [])}:
                g = got.setdefault(a, {"emails": 0, "emails_copy": 0, "summaries": 0, "summaries_copy": 0})
                g[kind + how] += 1
    rows = []
    for e, row in by_email.items():
        w, m = web.get(e, {}), wrote.get(e, {})
        g = got.get(e, {})
        rows.append(dict(row, studies=", ".join(row["studies"]), roles=", ".join(row["roles"]),
                         last_visit=_when(w.get("last")), pages=w.get("pages", 0),
                         messages=m.get("n", 0), last_message=_when(m.get("last")),
                         emails=g.get("emails", 0) + g.get("emails_copy", 0), emails_copy=g.get("emails_copy", 0),
                         summaries=g.get("summaries", 0) + g.get("summaries_copy", 0)))
    listed = set(by_email)
    return {"people": rows,
            "others": [{"email": e, "last_visit": _when(w["last"]), "pages": w["pages"]}
                       for e, w in web.items() if e not in listed],
            "events": [{"when": _when(r["ts"]), "who": db.unjs(r["details"], {}).get("email"),
                        "what": SIGN_IN_WORDS.get(r["action"], r["action"])} for r in db.q(
                f"SELECT ts, action, details FROM audit_log WHERE action IN ({marks}) ORDER BY id DESC LIMIT 100",
                acts)]}


# ---------------------------------------------------------------- patient memory

def memory_page(query: dict) -> dict:
    """What patient memory holds for a study: its participants, or one participant's facts, and the facts linked
    to nobody."""
    from . import hindsight
    studies = list(_studies())
    study = query.get("study") if query.get("study") in studies else (studies[0] if studies else "")
    out = {"studies": studies, "study": study, "enabled": hindsight.enabled(), "participant": query.get("participant", "")}
    if not out["enabled"] or not study:
        return out
    try:
        if out["participant"]:
            out["facts"] = hindsight.participant_facts(study, out["participant"])
        else:
            out.update(hindsight.overview(study))
    except Exception as e:
        out["error"] = f"Patient memory could not be read: {str(e)[:200]}"
    return out


def memory_change(body: dict, email: str) -> dict:
    """An admin's correct, retire, restore or link of one fact (hindsight.change_fact)."""
    from . import hindsight
    if body.get("study") not in _studies():
        raise ValueError("unknown study")
    return hindsight.change_fact(body["study"], str(body.get("fact") or ""), str(body.get("action") or ""), email,
                                 str(body.get("reason") or ""), str(body.get("text") or ""),
                                 str(body.get("participant") or ""))


# ---------------------------------------------------------------- routing

def shell(email: str) -> str:
    """The one HTML page; the React app reads the path and asks /admin/api/... for the data."""
    v = int(max((WEB / f).stat().st_mtime for f in STATIC))
    scripts = "".join(f'<script src="/admin/static/{f}?v={v}"></script>' for f in STATIC if f.endswith(".js"))
    name = html.escape(settings.bot_name())
    avatar = html.escape(settings.bot_avatar(), quote=True)
    product = PRODUCT if settings.feature("openbook_look") else ""
    look = f' class="openbook" data-product="{product}"' if product else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{product or name} admin</title><link rel="stylesheet" href="/admin/static/admin.css?v={v}">'
            f'</head><body{look} data-email="{email.replace(chr(34), "")}" data-bot-name="{name}"'
            f' data-bot-avatar="{avatar}"><div id="root"></div>{scripts}</body></html>')


def static(path: str) -> tuple[bytes, str] | None:
    name = urlparse(path).path.removeprefix("/admin/static/")
    if name not in STATIC:
        return None
    return (WEB / name).read_bytes(), STATIC[name]


def api(path: str) -> dict | None:
    """The data for an /admin/api path, or None when there is no such page."""
    url = urlparse(path)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    p = url.path.removeprefix("/admin/api").rstrip("/")
    if p == "/home":
        return home(query)
    if p == "/options":
        return options()
    if p == "/messages":
        return messages(query)
    if p.startswith("/messages/") and p.rsplit("/", 1)[1].isdigit():
        return message(int(p.rsplit("/", 1)[1]))
    if p == "/alerts":
        return alerts(query)
    if p == "/people":
        return people()
    if p == "/memory":
        return memory_page(query)
    return None

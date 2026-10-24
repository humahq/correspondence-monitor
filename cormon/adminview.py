"""Read-only admin pages for the people who run the bot (web.admin.emails): is it healthy, every message with its whole
conversation and what the bot did, every alert, and who has signed in or written. A small React app (cormon/web)
draws the pages from the JSON answers here. Nothing on these pages changes anything."""

from __future__ import annotations

import html
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import capture, config, coverage, db, outbox, settings, timing
from .drafting import local_time
from .triage import TIER_LABELS

PAGE = 50
WEB = Path(__file__).parent / "web"
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
}

CHANNEL = {"email": "Email", "teams": "Chat"}
STATUS = {"new": "Waiting", "processing": "Reading now", "processed": "Done", "error": "Error"}
STATE = {"open": "Open", "cleared": "Cleared", "suppressed": "Disregarded", "out_of_scope": "Not reviewed (safety)"}
PURPOSE = {"chat_received": "Quick receipt", "question_answer": "Answer", "chat_acknowledgement": "Reply in chat",
           "teams_study_team": "Alert in the study chat", "teams_study_channel": "Alert in the study chat",
           "email_cra": "Alert email to the CRA", "email_sponsor": "Email to the sponsor", "digest": "Daily digest",
           "ops_warning": "Warning to the bot's operators", "copy_nudge": "Reminder to copy the bot"}

_loaded: dict = {"at": 0.0, "studies": {}}


def _studies() -> dict:
    """The study files, read at most every 30 seconds: a page uses them once per row."""
    if time.monotonic() - _loaded["at"] > 30:
        _loaded["studies"] = config.studies()
        _loaded["at"] = time.monotonic()
    return _loaded["studies"]


def _local_midnight(days_back: int = 0) -> str:
    """UTC time of local midnight `days_back` days ago, in the same form as the database's times."""
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_back)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


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


def _alert(a: dict) -> dict:
    study = _studies().get(a["study_id"])
    dest = sorted({o["destination"].replace("_", " ") for o in db.q(
        "SELECT destination FROM outbox WHERE item_id=?", (a["id"],))})
    return {"id": a["id"], "link": f"/items/{a['link_token']}", "title": a["title"], "type": alert_type(a),
            "tier": a["tier"], "tier_label": TIER_LABELS.get(a["tier"], a["tier"]),
            "state": STATE.get(a["state"], a["state"]), "counts": bool(a["counts"]),
            "site": study.site_name(a["site"]) if study else a["site"], "participant": a["participant_id"],
            "sent_to": _name(a["study_id"], a["recipient_person"]), "destinations": dest,
            "opened": _when(a["created_at"]), "message_id": a["first_message_id"]}


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
    for r in db.q("SELECT action FROM audit_log WHERE entity_type='message' AND entity_id=? ORDER BY id",
                  (str(msg["id"]),)):
        words = OUTCOMES.get(r["action"])
        if words and words not in seen:
            seen.add(words)
            said.append({"kind": "error" if r["action"] == "processing_error" else "note", "text": words})
    replies = db.q1("SELECT COUNT(*) AS n FROM outbox WHERE message_id=? AND status='sent'"
                    " AND purpose!='chat_received'", (msg["id"],))["n"]
    if replies:
        said.append({"kind": "note", "text": f"{replies} repl{'y' if replies == 1 else 'ies'} sent"})
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
    for st in _studies().values():
        g = coverage.last(st.study_id)
        if g and g["warnings"]:
            out.append({"text": f"Study {st.study_id}: the document check has warnings: {'; '.join(g['warnings'])}",
                        "link": None})
    return out


def _median(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def home() -> dict:
    beat = db.get_state("heartbeat")
    fresh = False
    if beat:
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 300
        except ValueError:
            pass
    today = _local_midnight(0)
    n = lambda sql, *p: db.q1(sql, p)["n"]
    rep = timing.report(20)["messages"]
    return {
        "bot": {"state": "paused" if outbox.is_paused() else "running" if fresh else "down",
                "checkin": _when(beat), "last_message": _when(db.q1("SELECT MAX(received_at) AS at FROM messages")["at"]),
                "last_push": _when(db.get_state("gchat_event_at")),
                "email": settings.get("channels.email"), "chat": settings.get("channels.teams")},
        "today": [
            {"label": "Messages today", "n": n("SELECT COUNT(*) AS n FROM messages WHERE received_at>=?", today)},
            {"label": "Alerts today", "n": n("SELECT COUNT(*) AS n FROM items WHERE created_at>=? AND counts=1", today)},
            {"label": "Questions answered today",
             "n": n("SELECT COUNT(*) AS n FROM audit_log WHERE action='question_answered' AND ts>=?", today)},
            {"label": "Open alerts", "n": n("SELECT COUNT(*) AS n FROM items WHERE state='open' AND counts=1")},
        ],
        "open_by_tier": [{"tier": r["tier"], "label": TIER_LABELS.get(r["tier"], r["tier"]), "n": r["n"]} for r in db.q(
            "SELECT tier, COUNT(*) n FROM items WHERE state='open' AND counts=1 GROUP BY tier")],
        "speed": {"messages": len(rep), "first_reply": _median(m["to_first_reply"] for m in rep),
                  "read": _median(m["seconds"] for m in rep)},
        "problems": _problems(),
    }


# ---------------------------------------------------------------- messages and conversations

def options() -> dict:
    areas = {p["id"]: p["label"] for s in _studies().values() for p in s.pattern_classes}
    return {"studies": list(_studies()),
            "types": [{"value": "", "label": "Any type"}, {"value": "safety", "label": "Patient safety"},
                      {"value": "protocol", "label": "Any protocol problem"}]
            + [{"value": f"area:{k}", "label": v} for k, v in areas.items()]
            + [{"value": "good_news", "label": "Good news"}],
            "tiers": [{"value": "", "label": "Any urgency"}] + [{"value": k, "label": v} for k, v in TIER_LABELS.items()]}


def _row(m: dict) -> dict:
    return {"id": m["id"], "received": _when(m["received_at"]), "channel": CHANNEL.get(m["channel"], m["channel"]),
            "visibility": m["visibility"], "from": m["from_name"] or m["from_addr"], "from_addr": m["from_addr"],
            "study": m["study_id"], "subject": m["subject"],
            "summary": m["subject"] or (m["body_text"] or "")[:120] or "(no text)",
            "status": STATUS.get(m["status"], m["status"]), "outcome": outcome(m)}


def messages(query: dict) -> dict:
    days, channel = query.get("days", "7"), query.get("channel", "")
    study, before = query.get("study", ""), query.get("before", "")
    sql, params = "SELECT * FROM messages WHERE 1=1", []
    if days.isdigit():
        sql += " AND received_at>=?"; params.append(_local_midnight(int(days) - 1))
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
    if where:
        for o in db.q(f"SELECT * FROM outbox WHERE ({' OR '.join(where)}) AND purpose!='digest' ORDER BY id",
                      (*ids, *item_ids)):
            to = ", ".join(db.unjs(o["to_addrs"], []))
            out.append({"side": "bot", "who": "Study monitor", "when": _when(o["sent_at"] or o["created_at"]),
                        "text": _plain(o["body"]), "subject": o["subject"],
                        "kind": PURPOSE.get(o["purpose"], o["purpose"]),
                        "to": to or o["destination"].replace("_", " "), "status": o["status"],
                        "item_id": o["item_id"]})
    return sorted(out, key=lambda e: e["when"]["at"] or "")


def _plain(text: str | None) -> str:
    """A bot post as people saw it: chat posts are stored as HTML and emails mark bold with **."""
    text = text or ""
    if re.search(r"</?(p|b|i|br|ul|li|a)\b", text):
        text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text)
        text = re.sub(r"<li[^>]*>", "• ", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
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
    sql, params = "SELECT * FROM items WHERE 1=1", []
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
    wrote = {((r["who"] or "").lower(), r["study_id"]): dict(r) for r in db.q(
        "SELECT from_addr AS who, study_id, COUNT(*) AS n, MAX(received_at) AS last FROM messages"
        " GROUP BY from_addr, study_id")}
    rows = []
    for st in _studies().values():
        for p in st.people:
            if not p.email:
                continue
            e = p.email.lower()
            w, m = web.get(e, {}), wrote.get((e, st.study_id), {})
            rows.append({"name": p.name, "email": e, "study": st.study_id,
                         "roles": ", ".join(st.role_label(r) for r in p.roles),
                         "last_visit": _when(w.get("last")), "pages": w.get("pages", 0),
                         "messages": m.get("n", 0), "last_message": _when(m.get("last"))})
    listed = {p.email.lower() for st in _studies().values() for p in st.people if p.email}
    return {"people": rows,
            "others": [{"email": e, "last_visit": _when(w["last"]), "pages": w["pages"]}
                       for e, w in web.items() if e not in listed],
            "events": [{"when": _when(r["ts"]), "who": db.unjs(r["details"], {}).get("email"),
                        "what": SIGN_IN_WORDS.get(r["action"], r["action"])} for r in db.q(
                f"SELECT ts, action, details FROM audit_log WHERE action IN ({marks}) ORDER BY id DESC LIMIT 100",
                acts)]}


# ---------------------------------------------------------------- routing

def shell(email: str) -> str:
    """The one HTML page; the React app reads the path and asks /admin/api/... for the data."""
    v = int(max((WEB / f).stat().st_mtime for f in STATIC))
    scripts = "".join(f'<script src="/admin/static/{f}?v={v}"></script>' for f in STATIC if f.endswith(".js"))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Study monitor admin</title><link rel="stylesheet" href="/admin/static/admin.css?v={v}">'
            f'</head><body data-email="{email.replace(chr(34), "")}"><div id="root"></div>{scripts}</body></html>')


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
        return home()
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
    return None

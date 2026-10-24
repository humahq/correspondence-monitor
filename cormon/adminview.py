"""Read-only admin pages for the people who run the bot (web.admin.emails): is it healthy, every message and what
the bot did with it, every alert, and who has signed in or written. Nothing on these pages changes anything."""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from . import capture, config, coverage, db, outbox, settings, timing
from .drafting import local_time
from .triage import TIER_LABELS
from .webview import CSS, _e

ADMIN_CSS = CSS.replace("max-width:860px", "max-width:1100px") + """
nav{display:flex;gap:16px;align-items:center;border-bottom:1px solid #e3e6ec;padding-bottom:10px;margin-bottom:8px}
nav a{color:#1d2433;text-decoration:none;font-weight:600}nav a.on{color:#2457c5;border-bottom:2px solid #2457c5}
nav .who{margin-left:auto;font-weight:400}.ok{background:#e3f4e8;color:#1e6b38}.bad{background:#fde2e1;color:#9b1c1c}
.warn{background:#fff1d6;color:#8a5300}.cards{display:flex;gap:12px;flex-wrap:wrap}.card{border:1px solid #e3e6ec;
border-radius:8px;padding:10px 14px;min-width:150px}.card b{display:block;font-size:22px}form.filters{margin:12px 0;
font-size:13px}form.filters select,form.filters button{margin-right:8px}pre{white-space:pre-wrap;font-family:inherit;
background:#f7f8fa;padding:10px;border-radius:4px}"""

PAGE = 50

PAGES = [("/admin", "Home"), ("/admin/messages", "Messages"), ("/admin/alerts", "Alerts"),
         ("/admin/people", "Sign-ins and use")]

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


def alert_type(item: dict) -> str:
    """Patient safety, good news, or the study area of a protocol problem (the study's pattern class label)."""
    if item["kind"] == "out_of_scope":
        return _tag("Patient safety", "bad")
    if item["kind"] == "good_news":
        return _tag("Good news", "ok")
    study = config.studies().get(item["study_id"])
    area = study.pattern(item["pattern_class"]) if study and item["pattern_class"] else None
    return _tag(area["label"] if area else "Protocol problem")


def _type_options() -> list[tuple[str, str]]:
    areas = {p["id"]: p["label"] for s in config.studies().values() for p in s.pattern_classes}
    return [("", "Any type"), ("safety", "Patient safety"), ("protocol", "Any protocol problem")] \
        + [(f"area:{k}", f"  {v}") for k, v in areas.items()] + [("good_news", "Good news")]


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


def _tag(text: str, cls: str = "") -> str:
    return f'<span class="tag {cls}">{_e(text)}</span>'


def _shell(title: str, here: str, email: str, body: str) -> str:
    links = "".join(f'<a href="{href}" class="{"on" if href == here else ""}">{name}</a>' for href, name in PAGES)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{_e(title)} · Study monitor admin</title>
<style>{ADMIN_CSS}</style></head><body>
<nav>{links}<span class="who muted">{_e(email)} · <a href="/auth/logout">Sign out</a></span></nav>
<h1>{_e(title)}</h1>
{body}
<p class="muted">Read-only. Every page view is logged. This monitor does not review safety information.</p>
</body></html>"""


def _table(head: list[str], rows: list[list[str]], empty: str = "Nothing to show.") -> str:
    if not rows:
        return f'<p class="muted">{_e(empty)}</p>'
    th = "".join(f"<th>{_e(h)}</th>" for h in head)
    return f"<table><tr>{th}</tr>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) \
        + "</table>"


# ---------------------------------------------------------------- what the bot did with a message

def outcome(msg: dict) -> list[str]:
    """Short plain lines saying what the bot did with one message (alerts, answers, or why nothing was needed)."""
    if msg["status"] == "error":
        return [_tag("Error", "bad") + _e((msg.get("error") or "")[:120])]
    if msg["status"] in ("new", "processing"):
        return [_tag(STATUS[msg["status"]], "warn")]
    said = []
    for it in db.q("SELECT id, study_id, kind, pattern_class, tier, link_token, first_message_id FROM items"
                   " WHERE first_message_id=? OR last_message_id=? ORDER BY id", (msg["id"], msg["id"])):
        verb = "New alert" if it["first_message_id"] == msg["id"] else "Added to alert"
        said.append(alert_type(dict(it)) + _tag(TIER_LABELS.get(it["tier"], it["tier"]), it["tier"])
                    + f'<a href="/items/{_e(it["link_token"])}">{verb} #{it["id"]}</a>')
    seen = set()
    for r in db.q("SELECT action FROM audit_log WHERE entity_type='message' AND entity_id=? ORDER BY id",
                  (str(msg["id"]),)):
        words = OUTCOMES.get(r["action"])
        if words and words not in seen:
            seen.add(words)
            said.append(_e(words))
    replies = db.q1("SELECT COUNT(*) AS n FROM outbox WHERE message_id=? AND status='sent'"
                    " AND purpose NOT IN ('chat_received')", (msg["id"],))["n"]
    if replies:
        said.append(_e(f"{replies} repl{'y' if replies == 1 else 'ies'} sent"))
    return said or [_e("Checked: nothing to raise")]


# ---------------------------------------------------------------- page 1: home

def _problems() -> list[str]:
    out = []
    for r in db.q("SELECT id, status, error FROM messages WHERE status='error' ORDER BY id DESC LIMIT 20"):
        out.append(f'<a href="/admin/messages/{r["id"]}">Message {r["id"]}</a> could not be read: '
                   f'{_e((r["error"] or "")[:160])}')
    for r in db.q("SELECT id FROM messages WHERE status IN ('new','processing') AND received_at<?", (db.later(-15),)):
        out.append(f'<a href="/admin/messages/{r["id"]}">Message {r["id"]}</a> has waited more than 15 minutes.')
    for r in db.q("SELECT id, purpose, last_error FROM outbox WHERE status='failed' ORDER BY id DESC LIMIT 20"):
        out.append(f'Outgoing message {r["id"]} ({_e(r["purpose"])}) was not sent: {_e((r["last_error"] or "")[:160])}')
    for r in db.q("SELECT id, link_token FROM items WHERE routed=0 AND state!='suppressed' AND counts=1"
                  " AND created_at<?", (db.later(-15),)):
        out.append(f'<a href="/items/{_e(r["link_token"])}">Alert #{r["id"]}</a> was made but not sent yet.')
    for st in config.studies().values():
        g = coverage.last(st.study_id)
        if g and g["warnings"]:
            out.append(f"Study {_e(st.study_id)}: the document check has warnings: {_e('; '.join(g['warnings']))}")
    return out


def _median(xs) -> str:
    xs = [x for x in xs if x is not None]
    return f"{statistics.median(xs):.1f} s" if xs else "no data yet"


def home(email: str) -> str:
    beat = db.get_state("heartbeat")
    fresh = False
    if beat:
        try:
            fresh = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() < 300
        except ValueError:
            pass
    paused = outbox.is_paused()
    state = _tag("Paused", "warn") if paused else _tag("Running", "ok") if fresh else _tag("Not responding", "bad")
    push = db.get_state("gchat_event_at")
    last_msg = db.q1("SELECT MAX(received_at) AS at FROM messages")["at"]
    today = _local_midnight(0)
    n = lambda sql, *p: db.q1(sql, p)["n"]
    cards = [
        ("Messages today", n("SELECT COUNT(*) AS n FROM messages WHERE received_at>=?", today)),
        ("Alerts today", n("SELECT COUNT(*) AS n FROM items WHERE created_at>=? AND counts=1", today)),
        ("Questions answered today", n("SELECT COUNT(*) AS n FROM audit_log WHERE action='question_answered'"
                                       " AND ts>=?", today)),
        ("Open alerts", n("SELECT COUNT(*) AS n FROM items WHERE state='open' AND counts=1")),
    ]
    card_html = "".join(f'<div class="card"><b>{v}</b><span class="muted">{_e(k)}</span></div>' for k, v in cards)
    open_by_tier = ", ".join(f"{_e(TIER_LABELS.get(r['tier'], r['tier']))}: {r['n']}" for r in db.q(
        "SELECT tier, COUNT(*) n FROM items WHERE state='open' AND counts=1 GROUP BY tier")) or "none"
    rep = timing.report(20)["messages"]
    problems = _problems()
    body = f"""<p>{state} <span class="muted">Last check-in {_e(_ago(beat))} · last message {_e(_ago(last_msg))}
 · last instant chat notice {_e(_ago(push))} · email {_e(settings.get('channels.email'))}, chat
 {_e(settings.get('channels.teams'))}</span></p>
<h2>Today</h2><div class="cards">{card_html}</div>
<p class="muted">Open alerts by urgency: {open_by_tier}</p>
<h2>Speed (last {len(rep)} messages)</h2>
<p>Typical time from a message being sent to the bot's first reply: <b>{_median(m['to_first_reply'] for m in rep)}</b>.
 Typical time to read and check one message: <b>{_median(m['seconds'] for m in rep)}</b>.</p>
<h2>Problems</h2>
{"<ul>" + "".join(f"<li>{p}</li>" for p in problems) + "</ul>" if problems
     else '<p>' + _tag("None", "ok") + ' Nothing is stuck or failed.</p>'}"""
    return _shell("Study monitor", "/admin", email, body)


# ---------------------------------------------------------------- page 2: messages

def _select(name: str, current: str, options: list[tuple[str, str]]) -> str:
    opts = "".join(f'<option value="{_e(v)}"{" selected" if v == current else ""}>{_e(t)}</option>'
                   for v, t in options)
    return f'<select name="{name}">{opts}</select>'


def messages(email: str, query: dict) -> str:
    days = query.get("days", "7")
    channel = query.get("channel", "")
    study = query.get("study", "")
    before = query.get("before", "")
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
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    studies = [("", "All studies")] + [(s, s) for s in config.studies()]
    filters = f"""<form class="filters" method="get">
{_select("days", days, [("1", "Today"), ("7", "Last 7 days"), ("30", "Last 30 days"), ("all", "All time")])}
{_select("channel", channel, [("", "Chat and email"), ("teams", "Chat only"), ("email", "Email only")])}
{_select("study", study, studies)}<button>Show</button></form>"""
    table = _table(
        ["#", "Received", "Channel", "From", "Study", "Subject or first words", "What the bot did"],
        [[f'<a href="/admin/messages/{m["id"]}">{m["id"]}</a>', _e(local_time(m["received_at"])),
          _e(CHANNEL.get(m["channel"], m["channel"])) + f'<br><span class="muted">{_e(m["visibility"])}</span>',
          _e(m["from_name"] or m["from_addr"]), _e(m["study_id"] or "none"),
          _e(m["subject"] or (m["body_text"] or "")[:80] or "(no text)"),
          "<br>".join(outcome(m))] for m in rows],
        "No messages in this period.")
    nxt = ""
    if more:
        nxt = f'<p><a href="/admin/messages?{urlencode(dict(query, before=rows[-1]["id"]))}">Older messages</a></p>'
    return _shell("Messages", "/admin/messages", email, filters + table + nxt)


def message(email: str, msg_id: int) -> str | None:
    try:
        m = capture.message(msg_id)
    except KeyError:
        return None
    alerts = [dict(r) for r in db.q("SELECT * FROM items WHERE first_message_id=? OR last_message_id=? ORDER BY id",
                                    (msg_id, msg_id))]
    study = config.studies().get(m["study_id"])
    name = lambda pid: (study.person(pid).name if study and study.person(pid) else pid) or "nobody"
    alert_rows = [[f'<a href="/items/{_e(a["link_token"])}">#{a["id"]}</a>', alert_type(a),
                   _tag(TIER_LABELS.get(a["tier"], a["tier"]), a["tier"]), _e(STATE.get(a["state"], a["state"])),
                   _e(a["title"]), _e(name(a["recipient_person"]))] for a in alerts]
    item_ids = [a["id"] for a in alerts]
    sent = [dict(r) for r in db.q(
        "SELECT * FROM outbox WHERE message_id=?" + (f" OR item_id IN ({','.join('?' * len(item_ids))})"
                                                     if item_ids else "") + " ORDER BY id", (msg_id, *item_ids))]
    sent_rows = [[_e(local_time(o["sent_at"] or o["created_at"])), _e(o["purpose"].replace("_", " ")),
                  _e(", ".join(db.unjs(o["to_addrs"], [])) or o["destination"].replace("_", " ")), _e(o["status"]),
                  f'<details><summary>{_e(o["subject"] or "show")}</summary><pre>{_e(o["body"])}</pre></details>']
                 for o in sent]
    pages = db.q("SELECT link_token, created_at FROM answer_pages WHERE message_id=? ORDER BY id", (msg_id,))
    page_links = "".join(f'<li><a href="/answers/{_e(p["link_token"])}">Answer details</a> '
                         f'({_e(local_time(p["created_at"]))})</li>' for p in pages)
    t = db.q1("SELECT * FROM message_timings WHERE message_id=? ORDER BY id DESC LIMIT 1", (msg_id,))
    steps = db.unjs(t["steps"], {}) if t else {}
    speed = (f"Read and checked in {t['seconds']:.1f} s ("
             + ", ".join(f"{k} {v:.1f}" for k, v in steps.items() if v >= 0.5) + ")") \
        if t and t["seconds"] is not None else "No timing saved for this message."
    log = db.q("SELECT ts, actor, action FROM audit_log WHERE entity_type='message' AND entity_id=? ORDER BY id",
               (str(msg_id),))
    attachments = "".join(f"<li>{_e(a['filename'])} ({_e(a['component'])})</li>" for a in m["attachments"])
    body = f"""<p>{_tag(CHANNEL.get(m['channel'], m['channel']))}{_tag(STATUS.get(m['status'], m['status']))}
{_tag(m['study_id'] or 'no study')}{_tag(m['visibility'])}</p>
<p class="muted">From {_e(m['from_name'])} &lt;{_e(m['from_addr'])}&gt;
{(' · to ' + _e(', '.join(m['to_addrs']))) if m['to_addrs'] else ''}
{(' · cc ' + _e(', '.join(m['cc_addrs']))) if m['cc_addrs'] else ''}<br>
Sent {_e(local_time(m['sent_at']))} · received {_e(local_time(m['received_at']))}
{(' · subject: ' + _e(m['subject'])) if m['subject'] else ''}</p>
<h2>The message</h2><pre>{_e(m['body_text'] or '(the text is no longer kept)')}</pre>
{f'<p class="muted">Attachments:</p><ul>{attachments}</ul>' if attachments else ''}
<h2>What the bot did</h2><ul>{''.join(f'<li>{x}</li>' for x in outcome(m))}</ul>
<h2>Alerts</h2>{_table(['#', 'Type', 'Urgency', 'State', 'Title', 'Sent to'], alert_rows,
                        'No alert from this message.')}
<h2>Messages the bot sent</h2>{_table(['When', 'Kind', 'To', 'Status', 'Text'], sent_rows, 'None.')}
{f'<h2>Answers</h2><ul>{page_links}</ul>' if page_links else ''}
<h2>Speed</h2><p>{_e(speed)}</p>
<h2>Steps logged</h2>{_table(['When', 'Who', 'What'], [[_e(local_time(r['ts'])), _e(r['actor']),
                                                      _e(r['action'].replace('_', ' '))]
                                                     for r in log])}"""
    return _shell(f"Message {msg_id}", "/admin/messages", email, body)


# ---------------------------------------------------------------- page 4: alerts

def alerts(email: str, query: dict) -> str:
    state = query.get("state", "open")
    tier = query.get("tier", "")
    kind = query.get("type", "")
    before = query.get("before", "")
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
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    filters = f"""<form class="filters" method="get">
{_select("state", state, [("open", "Open"), ("cleared", "Cleared"), ("suppressed", "Disregarded"), ("all", "All")])}
{_select("type", kind, _type_options())}
{_select("tier", tier, [("", "Any urgency")] + [(k, v) for k, v in TIER_LABELS.items()])}<button>Show</button></form>"""
    out = []
    for a in rows:
        dest = ", ".join(sorted({o["destination"] for o in db.q("SELECT destination FROM outbox WHERE item_id=?",
                                                                (a["id"],))})) or "not sent"
        study = config.studies().get(a["study_id"])
        person = study.person(a["recipient_person"]) if study else None
        out.append([f'<a href="/items/{_e(a["link_token"])}">#{a["id"]}</a>', _e(local_time(a["created_at"])),
                    alert_type(a), _tag(TIER_LABELS.get(a["tier"], a["tier"]), a["tier"]),
                    _e(STATE.get(a["state"], a["state"])) + ("" if a["counts"] else ' <span class="muted">(does not count)</span>'),
                    _e(study.site_name(a["site"]) if study else a["site"]), _e(a["participant_id"] or "-"),
                    _e(a["title"]), _e(person.name if person else a["recipient_person"] or "nobody")
                    + f'<br><span class="muted">{_e(dest.replace("_", " "))}</span>',
                    _e(_ago(a["created_at"]).replace(" ago", ""))])
    nxt = ""
    if more:
        nxt = f'<p><a href="/admin/alerts?{urlencode(dict(query, before=rows[-1]["id"]))}">Older alerts</a></p>'
    table = _table(["#", "Opened", "Type", "Urgency", "State", "Site", "Participant", "Problem", "Sent to", "Age"], out,
                   "No alerts here.")
    return _shell("Alerts", "/admin/alerts", email, filters + table + nxt)


# ---------------------------------------------------------------- page 5: sign-ins and use

SIGN_IN_WORDS = {"signed_in": "Signed in", "page_viewed": "Opened a page", "page_refused": "Refused a page",
                 "admin_viewed": "Opened an admin page", "admin_refused": "Refused an admin page",
                 "sign_in_failed": "Sign-in failed", "sign_in_refused": "Sign-in refused"}


def people(email: str) -> str:
    acts = tuple(SIGN_IN_WORDS)
    marks = ",".join("?" * len(acts))
    web = {}
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
    for st in config.studies().values():
        for p in st.people:
            if not p.email:
                continue
            e = p.email.lower()
            w, m = web.get(e, {}), wrote.get((e, st.study_id), {})
            rows.append([_e(p.name), _e(e), _e(st.study_id), _e(", ".join(st.role_label(r) for r in p.roles)),
                         _e(local_time(w["last"])) if w.get("last") else '<span class="muted">never</span>',
                         _e(w.get("pages", 0)),
                         (f"{m['n']} · last {_e(local_time(m['last']))}") if m else '<span class="muted">none</span>'])
    listed = {p.email.lower() for st in config.studies().values() for p in st.people if p.email}
    others = [[_e(e), _e(local_time(w["last"])), _e(w["pages"])] for e, w in web.items() if e not in listed]
    recent = db.q(f"SELECT ts, action, details FROM audit_log WHERE action IN ({marks}) ORDER BY id DESC LIMIT 100",
                  acts)
    body = f"""<p class="muted">Everyone on a study's people list: when they last used the web pages, and how many
 messages they have sent the bot (chat or email).</p>
{_table(["Name", "Email", "Study", "Role", "Last web visit", "Pages opened", "Messages to the bot"], rows)}
<h2>Others who visited the web pages</h2>
{_table(["Email", "Last visit", "Pages opened"], others, "Nobody else.")}
<h2>Latest 100 sign-in events</h2>
{_table(["When", "Who", "What"], [[_e(local_time(r["ts"])), _e(db.unjs(r["details"], {}).get("email")),
                                   _e(SIGN_IN_WORDS.get(r["action"], r["action"]))] for r in recent])}"""
    return _shell("Sign-ins and use", "/admin/people", email, body)


# ---------------------------------------------------------------- routing

def render(path: str, email: str) -> str | None:
    """The page for an /admin path, or None when there is no such page."""
    url = urlparse(path)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    p = url.path.rstrip("/") or "/admin"
    if p == "/admin":
        return home(email)
    if p == "/admin/messages":
        return messages(email, query)
    if p.startswith("/admin/messages/") and p.rsplit("/", 1)[1].isdigit():
        return message(email, int(p.rsplit("/", 1)[1]))
    if p == "/admin/alerts":
        return alerts(email, query)
    if p == "/admin/people":
        return people(email)
    return None

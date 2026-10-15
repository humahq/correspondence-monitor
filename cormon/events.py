"""Event model: a message converted into structured events."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from . import db, ingest, llm, notes, settings
from .config import Study

HARM = ["none", "symptom_or_adverse_event", "injury_or_hospitalization", "death",
        "writer_says_result_is_a_risk"]

EVENT_SCHEMA = llm.obj({
    "trial_related": {"type": "boolean"},
    "summary": {"type": "string"},
    "instruction_to_monitor": llm.obj({
        "type": {"type": "string", "enum": ["none", "disregard", "cleared", "other"]},
        "text": llm.nullable("string"),
    }),
    "events": {
        "type": "array",
        "items": llm.obj({
            "description": {"type": "string"},
            "participant_id": llm.nullable("string"),
            "event_type": {"type": "string", "enum": ingest.EVENT_TYPES},
            "temporality": {"type": "string", "enum": ["current", "planned", "historical", "hypothetical"]},
            "temporality_evidence": {"type": "string"},
            "event_datetime": llm.nullable("string"),
            "source": {"type": "string", "enum": ["message_body", "quoted_history", "attachment", "forwarded_message"]},
            "polarity": {"type": "string", "enum": ["problem", "good_news", "neutral"]},
            "quote": {"type": "string"},
            "facts": {
                "type": "array",
                "items": llm.obj({
                    "parameter": {"type": "string"},
                    "value": {"type": "string"},
                    "numeric_value": llm.nullable("number"),
                    "unit": llm.nullable("string"),
                    "from_when": llm.nullable("string"),
                    "to_when": llm.nullable("string"),
                    "measured": llm.nullable("number"),
                    "reference": llm.nullable("number"),
                    "quote": {"type": "string"},
                }),
            },
            "harm": {"type": "string", "enum": HARM},
            "safety_signal": {"type": "boolean"},
            "safety_only": {"type": "boolean"},
            "prevented": {"type": "boolean"},
        }),
    },
    "requests": {"type": "array", "items": {"type": "string"}},
    "attachments": {
        "type": "array",
        "items": llm.obj({
            "attachment_index": {"type": "integer"},
            "placement": {"type": "string", "enum": ["placed", "unplaced"]},
            "what_it_is": {"type": "string"},
        }),
    },
})

SYSTEM = """You read clinical trial correspondence and convert it into structured events for a protocol-deviation monitor. You do not judge whether anything is a deviation; you only record what the correspondence says happened, to whom, and when.

Rules:
1. One event per distinct thing that happened or is planned (an enrollment, a visit, a blood draw, a dose start, a scan, a consent signature). Small talk and sign-offs are not events.
2. temporality is about whether this is NEW information, not only about when it happened:
   - current: being reported now, for the first time. This includes results or facts that happened days or weeks ago but are reported here as new (e.g. "screening labs came back last week: ..."), and supporting details given alongside a new event.
   - planned: has not happened yet and is scheduled.
   - historical: something already known, previously reported, or already dealt with, mentioned only as recap or reference (e.g. "as we discussed three weeks ago", "that deviation was already reported and closed"). Quote the words that show this in temporality_evidence.
   - hypothetical: a general question or what-if that reports nothing about a real participant or sample ("is an eGFR of 43 OK for enrollment?", "what if a sample sits for 70 minutes?"). If the question names a participant who is being screened, enrolled or treated, or says something was or will be done, it is current or planned, not hypothetical.
   For events in a forwarded message or attachment, judge from that item's own date and context, not from when it reached us: a forwarded message from weeks ago that was already acted on is historical.
3. event_datetime: ISO 8601 date or datetime of the event if stated or clearly inferable from the message date ("today", "this afternoon", "tomorrow"); otherwise null.
4. facts: every measurement, score, time interval or status stated about the participant or sample. Map each fact to one of the canonical parameter names given when it fits (convert units only if trivially exact, e.g. "1.5 hours" -> 90 minutes for a minutes parameter); otherwise use a short snake_case name. numeric_value only for numbers. A clock time or a date is never a duration.
   - A time between two steps (an interval, a time since or before something, a duration, an age): give from_when and to_when, the two points as ISO 8601 dates or datetimes (resolve them against the message date; a clock time alone is on the event's date; "around August 2024" is 2024-08-15), and the unit the parameter is counted in (minutes, hours, days, weeks, months, years). An age is from the date of birth to the event date. Code works the number out from the two points, so give them even when the email also states the difference. Leave them null when one point is not stated.
   - A value counted against a reference ("times the upper limit of normal", "percent of predicted"): give measured (the value) and reference (the limit or predicted value) when both are stated; code works out the multiple. Otherwise null.
5. quote / facts.quote / temporality_evidence: copy text VERBATIM from the correspondence. Never paraphrase in these fields.
6. polarity: problem if the writer says something went wrong or is about to go wrong; good_news only for a milestone worth recording (a visit completed on schedule, a participant completing the study); routine logistics are neutral. Do not decide eligibility or protocol compliance yourself.
7. harm: what harm to the participant the correspondence reports for this event.
   - symptom_or_adverse_event: a symptom, side effect or adverse event the participant had ("nausea since starting the tablets", "felt dizzy", "headache").
   - injury_or_hospitalization: a fall, injury, ER visit or hospital stay.
   - death.
   - writer_says_result_is_a_risk: the writer or the PI treats a result as a danger to the participant ("concerning", "worrying", "holding the drug because of it", "sent to the ER").
   - none: anything else. Medical history from before the participant's time on this study is none, even if it was a symptom or a reaction ("had pneumonia last year, before screening", "got a rash from another drug years ago"): it is background for eligibility, not a new safety event. A lab value, vital sign or score reported as data is none, even when it is out of range or breaks a study limit, and even when the site manages it routinely (a supplement for a low vitamin). Eligibility limits are study rules, not harm.
   safety_signal: true exactly when harm is not none. safety_only: true if the harm is the only thing the event is about (no eligibility, schedule, sample, sequence or consent aspect).
   Put the harm and the study facts in separate events when one message has both (e.g. "randomized with eGFR 40; she also had chest pain" is an enrollment event with harm none and a symptom event with harm symptom_or_adverse_event).
   prevented: true only when the writer says the site caught the problem before any rule was broken, so nothing went wrong (a participant with a failing value was screen-failed or not randomized; a bad sample was recollected and not used). False if the action came after the fact (randomized, then withdrawn).
8. instruction_to_monitor: only if the writer is addressing the monitoring bot itself ("disregard this", "please ignore", "this is cleared/resolved"). Otherwise type none.
9. trial_related: false for messages that have nothing to do with running the trial.
10. Apply the operator notes; they are facts or instructions that override your own inference."""


def _fmt_message(m: dict, label: str, max_chars: int = 6000) -> str:
    body = (m.get("body_text") or "")[:max_chars]
    return (f"--- {label} (id {m['id']}) ---\nFrom: {m.get('from_name') or ''} <{m.get('from_addr') or ''}>\n"
            f"Date: {m.get('sent_at')}\nSubject: {m.get('subject') or ''}\n\n{body}\n")


def build_prompt(study: Study, msg: dict, history: list[dict], note_list: list[dict]) -> str:
    rules = ingest.active_rules(study.study_id)
    params = sorted({r["parameter"] for r in rules if r["parameter"]}
                    | {r["condition"]["parameter"] for r in rules if r.get("condition")}
                    | {t["parameter"] for r in rules for t in r.get("unless") or []} - {"exception_applies"})
    sender = study.person_by_email(msg.get("from_addr"))
    sender_desc = (f"{sender.name} ({', '.join(study.role_label(r) for r in sender.roles)}; "
                   f"{', '.join(study.site_name(s) for s in sender.sites)})") if sender else "unknown sender"
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    try:
        sent_local = dtparser.isoparse(msg["sent_at"]).astimezone(tz)
        when = f"{sent_local:%A %d %B %Y, %H:%M} ({tz.key}, UTC offset {sent_local:%z})"
    except (TypeError, ValueError):
        when = msg.get("sent_at") or "unknown"
    parts = [
        f"Study: {study.title} ({study.study_id})",
        f"The message to convert was sent on {when}. Resolve 'today', 'this afternoon', 'tomorrow', "
        f"'next Thursday' etc. against this date and give event_datetime with this UTC offset "
        f"(use 09:00 for 'morning', 14:00 for 'afternoon' when no time is given).",
        f"Message channel: {msg['channel']}; monitor saw it as: {msg['visibility']}",
        f"Sender: {sender_desc}",
        f"Canonical parameter names: {', '.join(params)}",
        f"Operator notes:\n{notes.render(note_list)}",
        "",
        "EARLIER MESSAGES IN THIS THREAD (context only; their events were already recorded):" if history else "",
    ]
    for h in history:
        parts.append(_fmt_message(h, "earlier message", 2000))
    parts.append(_fmt_message(msg, "MESSAGE TO CONVERT"))
    gap = (msg.get("headers") or {}).get("_cormon_gap_refs")
    if gap:
        parts.append("NOTE: the monitor was not copied on some earlier messages in this thread. Read the quoted "
                     "history in the message above in full and record events from it (source quoted_history), "
                     "judging their timing by their own dates.")
    for i, a in enumerate(msg.get("attachments", [])):
        parts.append(f"--- ATTACHMENT {i}: {a['filename']} ({a['component']}, part of message dated "
                     f"{a['source_date']}) ---\n{(a.get('text') or '[no extractable text]')[:6000]}")
    return "\n".join(p for p in parts if p is not None)


def extract(study: Study, msg: dict, history: list[dict], *, use_cache: bool = True) -> dict:
    emails = [msg.get("from_addr")] + list(msg.get("to_addrs") or []) + list(msg.get("cc_addrs") or [])
    note_list = notes.applicable(msg["id"], study.study_id, emails)
    user = build_prompt(study, msg, history, note_list)
    data = llm.structured("extract", SYSTEM, user, EVENT_SCHEMA, "correspondence_events", use_cache=use_cache)
    data["_notes_applied"] = [n["id"] for n in note_list]
    return data


def store(msg_id: int, data: dict, source: str = "system") -> int:
    row = db.q1("SELECT COALESCE(MAX(version), 0) v FROM events WHERE message_id=?", (msg_id,))
    version = row["v"] + 1
    from .llm import model_for

    eid = db.ex("INSERT INTO events(message_id, version, data, model, source, created_at) VALUES (?,?,?,?,?,?)",
                (msg_id, version, db.js(data), model_for("extract"), source, db.now()))
    return eid


def latest(msg_id: int) -> dict | None:
    row = db.q1("SELECT * FROM events WHERE message_id=? ORDER BY version DESC LIMIT 1", (msg_id,))
    return db.unjs(row["data"]) if row else None

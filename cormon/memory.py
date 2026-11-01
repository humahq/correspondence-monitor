"""What the bot knows about each participant from earlier messages: the dates and dated facts it read before.

A message often leaves out what was said before ("wk8 done 11/20" without the Baseline date). Each message's
dates are saved per participant, and the next message about them is read with the earlier ones it needs: the
anchor dates that windows count from (Baseline, first dose, ...) for the AI judge, and every earlier dated fact for
the code checks. A departure code finds between earlier facts only was already checked then and is not raised
again; one that pairs an earlier fact with a new one is new, and its evidence names the earlier message.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dateutil import parser as dtparser

from . import db
from .drafting import local_time

# the dates other dates are counted from; the judge gets these from earlier messages, never earlier visits
ANCHOR_KINDS = ("baseline", "first_dose", "extension_start", "last_dose", "date_of_birth", "randomization", "consent",
                "screening")
EARLIER = "(earlier message, {when}) "
KEEP_DAYS = 400


def key(pid: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (pid or "").upper())


def save(study_id: str, pid: str | None, msg: dict, dates: list[dict], events: list[dict]) -> None:
    """Record one message's dates for a participant (a rerun of the message replaces them)."""
    k = key(pid)
    if not k or not msg.get("id") or not (dates or events):
        return
    db.ex("INSERT INTO patient_facts (study_id, participant_key, participant_id, message_id, sent_at, dates, events,"
          " created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (study_id, participant_key, message_id) DO UPDATE SET"
          " dates=excluded.dates, events=excluded.events, participant_id=excluded.participant_id",
          (study_id, k, pid.strip(), msg["id"], msg.get("sent_at"), db.js(dates), db.js(events), db.now()))


def _instant(s: str | None) -> datetime | None:
    try:
        d = dtparser.isoparse(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def history(study_id: str, pid: str | None, msg: dict, *, include_current: bool = False) -> list[dict]:
    """Earlier messages' facts about a participant, oldest first; each with the message it came from."""
    k = key(pid)
    if not k:
        return []
    now = _instant(msg.get("sent_at")) or datetime.now(timezone.utc)
    out = []
    for r in db.q("SELECT * FROM patient_facts WHERE study_id=? AND participant_key=? ORDER BY sent_at, id",
                  (study_id, k)):
        if r["message_id"] == msg.get("id") and not include_current:
            continue
        when = _instant(r["sent_at"])
        if when and (when > now + timedelta(minutes=1) or when < now - timedelta(days=KEEP_DAYS)):
            continue
        out.append({"message_id": r["message_id"], "when": local_time(r["sent_at"]) if r["sent_at"] else "",
                    "dates": db.unjs(r["dates"], []), "events": db.unjs(r["events"], [])})
    return out


def _tag(h: dict) -> str:
    return EARLIER.format(when=h["when"] or f"message {h['message_id']}")


def anchor_kind(d: dict) -> str | None:
    """The anchor a date is, if any: the Baseline visit's date is the Baseline, however it was labelled."""
    if d.get("kind") in ANCHOR_KINDS:
        return d["kind"]
    if d.get("kind") == "visit" and re.search(r"\bbaseline\b", f"{d.get('label')} {d.get('visit_id')}", re.I):
        return "baseline"
    return None


def anchor_dates(past: list[dict], dates: list[dict]) -> list[dict]:
    """Anchor dates the message does not give, from the newest earlier message that gave them, marked as earlier."""
    have = {anchor_kind(d) for d in dates}
    out: dict[str, dict] = {}
    for h in past:
        for d in h["dates"]:
            k = anchor_kind(d)
            if k and k not in have and d.get("date"):
                out[k] = dict(d, kind=k, label=f"{d.get('label') or k} {_tag(h).strip()}", earlier=True)
    return list(out.values())


def earlier_events(past: list[dict], events: list[dict]) -> list[dict]:
    """Earlier dated facts that happened, for the code checks, for event names this message does not give; the
    newest message's occurrences of each name. Their quotes say where they came from."""
    have = {e["event"] for e in events}
    out: dict[str, list[dict]] = {}
    for h in past:
        done = [e for e in h["events"] if e.get("status") == "done" and e.get("date")]
        for name in {e["event"] for e in done} - have:
            out[name] = [dict(e, quote=_tag(h) + (e.get("quote") or ""), earlier=True)
                         for e in done if e["event"] == name]
    return [e for es in out.values() for e in es]


def is_earlier_quote(q: str) -> bool:
    return q.startswith("(earlier message, ")


def only_earlier(departure: dict) -> bool:
    """A code departure resting on earlier facts alone: it was checked when those messages came in."""
    qs = departure.get("quotes") or []
    return bool(qs) and all(is_earlier_quote(q) for q in qs)


def record_lines(past: list[dict], corrected: set[str] = frozenset()) -> list[str]:
    """The participant's earlier dates as plain lines, oldest first (for answering questions). corrected: messages
    a later report corrected (patient memory found it): their dates are marked, so the later report holds."""
    lines = []
    for h in past:
        stale = " [from a message a later report corrected: the later report holds]" \
            if str(h.get("message_id")) in corrected else ""
        for d in sorted(h["dates"], key=lambda d: (d.get("date") or "", d.get("time") or "")):
            if d.get("date"):
                lines.append(f"{d.get('label') or d.get('kind')}: {d['date']}"
                             + (f" {d['time']}" if d.get("time") else "") + f" ({h['when'] or 'earlier message'})"
                             + stale)
    return lines

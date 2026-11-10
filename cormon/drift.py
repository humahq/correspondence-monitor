"""Watching the bot's quality in use (GOV-02). Once a week the admins get:

- grounding: of the problems the AI proposed, how many the check against the protocol rejected, how many were kept
  as "please check" (not sure) and how many were kept without the protocol's exact words;
- failures: AI calls that failed, fallback model use, AI wording that the output check refused;
- questions: how many the documents did not answer;
- each rate next to the four weeks before, flagged when it has clearly risen;
- a sample of messages that made no alert, for a person to read and mark "ok" or "missed"
  (`cormon drift --mark ID ok|missed`). The marks give the miss rate the AI cannot measure on itself.

Counts only: no patient text goes into the report.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import db, settings

WEEKS_BEFORE = 4
MIN_COUNT = 5   # a rate on fewer cases than this is shown but not flagged


def _now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=1)  # times are kept to the second: include this one


def _window(end: datetime, days: int = 7) -> tuple[str, str]:
    return ((end - timedelta(days=days)).isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))


def _count(action: str, start: str, end: str) -> int:
    return db.q1("SELECT COUNT(*) n FROM audit_log WHERE action=? AND ts>=? AND ts<?", (action, start, end))["n"]


def _grounding(start: str, end: str) -> dict:
    proposed = rejected = unsure = no_words = 0
    for r in db.q("SELECT details FROM audit_log WHERE action='reader_checked' AND ts>=? AND ts<?", (start, end)):
        for part in (db.unjs(r["details"], {}).get("trace") or {}).get("parts") or []:
            for f in part.get("findings") or []:
                outcome = f.get("outcome")
                if outcome not in ("clear", "unsure", "rejected"):
                    continue
                proposed += 1
                rejected += outcome == "rejected"
                unsure += outcome == "unsure"
                no_words += outcome != "rejected" and not f.get("quote_found")
    for action in ("finding_rejected_quote_not_in_message", "finding_rejected_source_not_in_document",
                   "finding_not_confirmed_by_document"):  # the rules route
        n = _count(action, start, end)
        proposed += n
        rejected += n
    return {"proposed": proposed, "rejected": rejected, "unsure": unsure, "no_exact_words": no_words}


def measure(start: str, end: str) -> dict:
    g = _grounding(start, end)
    calls = db.q1("SELECT COUNT(*) n, COALESCE(SUM(ok=0), 0) bad FROM ai_log WHERE ts>=? AND ts<?", (start, end))
    asked = _count("question_answered", start, end)
    return {
        "messages": db.q1("SELECT COUNT(*) n FROM messages WHERE study_id IS NOT NULL AND received_at>=?"
                          " AND received_at<?", (start, end))["n"],
        "alerts": db.q1("SELECT COUNT(*) n FROM items WHERE kind='finding' AND counts=1 AND created_at>=?"
                        " AND created_at<?", (start, end))["n"],
        "safety": db.q1("SELECT COUNT(*) n FROM items WHERE kind='out_of_scope' AND created_at>=? AND created_at<?",
                        (start, end))["n"],
        **g,
        "ai_calls": calls["n"], "ai_failed": calls["bad"], "fallback_used": _count("llm_fallback", start, end),
        "wording_refused": sum(_count(a, start, end) for a in
                               ("draft_edit_refused", "careful_reword_refused", "answer_wording_refused")),
        "judgment_flagged": _count("answer_judgment_flagged", start, end),
        "blocked_outgoing": _count("outgoing_blocked", start, end),
        "answer_messages": asked, "not_answered": _count("question_not_answered", start, end),
        "tricks_flagged": _count("trick_detector_flagged", start, end),
    }


RATES = {  # name: (numerator, denominator, plain words)
    "rejected": ("rejected", "proposed", "problems the AI proposed that the protocol check rejected"),
    "unsure": ("unsure", "proposed", "problems kept only as 'please check' (not sure)"),
    "no_exact_words": ("no_exact_words", "proposed", "problems kept without the protocol's exact words"),
    "ai_failed": ("ai_failed", "ai_calls", "AI calls that failed"),
    "not_answered": ("not_answered", "answer_messages", "questions the documents did not answer"),
}


def _rate(m: dict, name: str) -> float | None:
    num, den, _ = RATES[name]
    return m[num] / m[den] if m[den] else None


def report(end: datetime | None = None) -> dict:
    end = end or _now()
    this = measure(*_window(end))
    before = [measure(*_window(end - timedelta(days=7 * k))) for k in range(1, WEEKS_BEFORE + 1)]
    rates, flags = {}, []
    for name, (_, den, words) in RATES.items():
        now = _rate(this, name)
        past = [r for b in before if (r := _rate(b, name)) is not None]
        usual = sum(past) / len(past) if past else None
        rates[name] = {"now": now, "usual": usual, "words": words, "cases": this[den]}
        if now is not None and usual is not None and this[den] >= MIN_COUNT and now > max(2 * usual, usual + 0.10):
            flags.append(f"{words}: {now:.0%} this week, usually {usual:.0%}")
    reviewed = [dict(r) for r in db.q("SELECT verdict FROM drift_samples WHERE verdict IS NOT NULL AND reviewed_at>=?",
                                      ((end - timedelta(days=7 * (WEEKS_BEFORE + 1))).isoformat(timespec="seconds"),))]
    missed = sum(r["verdict"] == "missed" for r in reviewed)
    if missed:
        flags.append(f"people found {missed} missed problem(s) in {len(reviewed)} sampled message(s)")
    return {"end": end.isoformat(timespec="seconds"), "this": this, "rates": rates, "flags": flags,
            "reviewed": len(reviewed), "missed": missed,
            "unreviewed": db.q1("SELECT COUNT(*) n FROM drift_samples WHERE verdict IS NULL")["n"]}


def week_of(when: datetime) -> str:
    y, w, _ = when.isocalendar()
    return f"{y}-W{w:02d}"


def pick_sample(end: datetime | None = None, n: int | None = None) -> list[int]:
    """Messages of the last week that made no alert (and were not a safety report), picked at random."""
    end = end or _now()
    n = int(settings.get("drift.sample_size", 10) if n is None else n)
    start, stop = _window(end)
    quiet = [r["id"] for r in db.q(
        "SELECT m.id FROM messages m WHERE m.study_id IS NOT NULL AND m.status='processed' AND m.received_at>=?"
        " AND m.received_at<? AND NOT EXISTS (SELECT 1 FROM items i WHERE i.first_message_id=m.id"
        " OR i.last_message_id=m.id) ORDER BY m.id", (start, stop))]
    week = week_of(end)
    picked = random.Random(week).sample(quiet, min(n, len(quiet)))
    for mid in sorted(picked):
        db.ex("INSERT OR IGNORE INTO drift_samples (week, message_id, picked_at) VALUES (?,?,?)", (week, mid, db.now()))
    if picked:
        db.audit("drift_sample_picked", entity_type="service", entity_id="drift", week=week, messages=sorted(picked),
                 out_of=len(quiet))
    return sorted(picked)


def mark(message_id: int, verdict: str, reviewer: str, note: str | None = None) -> None:
    if verdict not in ("ok", "missed"):
        raise ValueError("mark a sampled message ok or missed")
    row = db.q1("SELECT id FROM drift_samples WHERE message_id=? ORDER BY id DESC LIMIT 1", (message_id,))
    if row is None:
        raise ValueError(f"message {message_id} was not in a sample")
    db.ex("UPDATE drift_samples SET verdict=?, reviewer=?, note=?, reviewed_at=? WHERE id=?",
          (verdict, reviewer, note, db.now(), row["id"]))
    db.audit("drift_sample_reviewed", entity_type="message", entity_id=message_id, actor=reviewer,
             actor_type="operator", verdict=verdict, note=note)


def render(rep: dict, sample: list[int]) -> str:
    t = rep["this"]
    lines = [f"Week to {rep['end'][:10]}: {t['messages']} study messages read, {t['alerts']} alerts, "
             f"{t['safety']} safety messages, {t['ai_calls']} AI calls.", ""]
    lines.append("Needs a look: " + ("; ".join(rep["flags"]) if rep["flags"] else "nothing has clearly got worse."))
    lines += ["", "Rates (this week, and the usual of the four weeks before):"]
    for r in rep["rates"].values():
        now = "no cases" if r["now"] is None else f"{r['now']:.0%} of {r['cases']}"
        usual = "" if r["usual"] is None else f" (usually {r['usual']:.0%})"
        lines.append(f"- {r['words']}: {now}{usual}")
    lines += ["", f"Fallback model used {t['fallback_used']} time(s). AI wording refused by the output check "
              f"{t['wording_refused']} time(s); answers flagged for a possible clinical judgment "
              f"{t['judgment_flagged']}; outgoing messages blocked {t['blocked_outgoing']}."]
    if t.get("tricks_flagged"):
        lines.append(f"Messages the trick detector flagged: {t['tricks_flagged']} (see trick_detector_flagged in the audit log).")
    base = settings.get("web.base_url", "")
    if sample:
        lines += ["", f"Please read these {len(sample)} messages, which made no alert, and mark each one:",
                  *[f"- {base}/admin/messages/{m}   then: cormon drift --mark {m} ok   (or: missed --note ...)"
                    for m in sample]]
    lines += ["", f"Sampled messages marked so far: {rep['reviewed']} ({rep['missed']} missed); "
              f"not yet marked: {rep['unreviewed']}."]
    return "\n".join(lines)


def weekly(log=print) -> int | None:
    """Once each week: the report to the admins, with this week's sample."""
    from . import routing
    from .compliance import alerts
    week = week_of(datetime.now(timezone.utc))
    if db.get_state("drift_week") == week:
        return None
    db.set_state("drift_week", week)
    to = alerts.recipients()
    sample = pick_sample()
    rep = report()
    if not to:
        db.audit("drift_report_no_recipients", entity_type="service", entity_id="drift", week=week)
        return None
    oid = routing.enqueue(purpose="drift_report", channel="email", destination="email", body=render(rep, sample),
                          subject=f"Study monitor: weekly quality check {week}"
                          + (" (needs a look)" if rep["flags"] else ""),
                          from_addr=settings.get("mailbox.address"), to=to, again=True)
    db.audit("drift_reported", entity_type="service", entity_id="drift", week=week, outbox_id=oid, flags=rep["flags"],
             counts=rep["this"])
    log(f"weekly quality check {week}: {len(rep['flags'])} flag(s), {len(sample)} message(s) to review")
    return oid

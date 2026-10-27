"""Where the time goes: how long each step of reading a message takes, and the time and tokens of every AI call.

Kept for finding what makes a reply slow. A failure here is never allowed to stop a message.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from . import db

# one message is read at a time, and the reader's worker threads read the same message: so one shared record
_lock = threading.Lock()
_now: dict = {}


def start(message_id: int) -> None:
    try:
        run = db.ex("INSERT INTO message_timings(message_id, started_at, status) VALUES (?,?,?)",
                    (message_id, db.now(), "running"))
    except Exception:
        run = None
    with _lock:
        _now.clear()
        _now.update(run=run, message_id=message_id, began=time.perf_counter(), step=None, step_began=None, steps={})


def current() -> tuple[int | None, int | None]:
    """(timing run id, message id) of the message being read now; (None, None) outside a read."""
    with _lock:
        return _now.get("run"), _now.get("message_id")


def step(name: str) -> None:
    """The step starting now; the one before it ends here."""
    with _lock:
        if not _now:
            return
        t = time.perf_counter()
        _close(t)
        _now.update(step=name, step_began=t)


def _close(t: float) -> None:
    if _now.get("step"):
        _now["steps"][_now["step"]] = round(_now["steps"].get(_now["step"], 0) + t - _now["step_began"], 2)


def finish(status: str) -> None:
    with _lock:
        if not _now:
            return
        t = time.perf_counter()
        _close(t)
        rec = dict(_now)
        _now.clear()
    if rec["run"] is None:
        return
    try:
        db.ex("UPDATE message_timings SET finished_at=?, seconds=?, steps=?, status=? WHERE id=?",
              (db.now(), round(t - rec["began"], 2), db.js(rec["steps"]), status, rec["run"]))
    except Exception:
        pass


def llm_call(task: str, model: str, seconds: float, *, cached: bool, usage=None, attempts: int = 1,
             ok: bool = True) -> None:
    with _lock:
        run, current = _now.get("run"), _now.get("step")
    details = getattr(usage, "prompt_tokens_details", None)
    try:
        db.ex("INSERT INTO llm_calls(ts, run_id, step, task, model, seconds, cached, input_tokens,"
              " cached_input_tokens, output_tokens, attempts, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (db.now(), run, current, task, model, round(seconds, 2), int(cached),
               getattr(usage, "prompt_tokens", 0) or 0,
               (getattr(details, "cached_tokens", 0) or 0) if details else 0,
               getattr(usage, "completion_tokens", 0) or 0, attempts, int(ok)))
    except Exception:
        pass


def _gap(a: str | None, b: str | None) -> float | None:
    try:
        return round((datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds(), 1)
    except (TypeError, ValueError):
        return None


def report(limit: int = 20) -> dict:
    """The latest reads, each with its waits, steps, AI time and tokens; and every AI task's totals."""
    messages = []
    for r in db.q("SELECT t.*, m.channel, m.sent_at, m.received_at FROM message_timings t"
                  " JOIN messages m ON m.id=t.message_id ORDER BY t.id DESC LIMIT ?", (limit,)):
        calls = db.q("SELECT seconds, cached, input_tokens, output_tokens FROM llm_calls WHERE run_id=?", (r["id"],))
        first = db.q1("SELECT MIN(sent_at) AS at FROM outbox WHERE message_id=? AND status='sent' AND created_at>=?",
                      (r["message_id"], r["started_at"]))
        messages.append({
            "message_id": r["message_id"], "channel": r["channel"], "status": r["status"],
            "to_capture": _gap(r["sent_at"], r["received_at"]),
            "to_start": _gap(r["received_at"], r["started_at"]),
            "seconds": r["seconds"], "steps": db.unjs(r["steps"], {}),
            "ai_calls": sum(1 for c in calls if not c["cached"]), "cache_hits": sum(1 for c in calls if c["cached"]),
            "ai_seconds": round(sum(c["seconds"] for c in calls if not c["cached"]), 1),
            "input_tokens": sum(c["input_tokens"] for c in calls),
            "output_tokens": sum(c["output_tokens"] for c in calls),
            "to_first_reply": _gap(r["sent_at"], first["at"] if first else None),
        })
    tasks = [dict(t) for t in db.q(
        "SELECT task, model, COUNT(*) AS calls, SUM(cached) AS cache_hits,"
        " ROUND(AVG(CASE WHEN cached=0 THEN seconds END), 1) AS avg_seconds, ROUND(MAX(seconds), 1) AS max_seconds,"
        " SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(1-ok) AS failed"
        " FROM llm_calls GROUP BY task, model ORDER BY SUM(CASE WHEN cached=0 THEN seconds ELSE 0 END) DESC")]
    return {"messages": messages, "tasks": tasks}

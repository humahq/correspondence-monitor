"""Sends queued drafts through the channel adapters and records the outcome.

Each row is taken by one sender only (queued -> sending in one update), so two senders never send it twice.
A failed send waits longer before each new try and is marked failed after the last. A row left 'sending'
(the service stopped mid-send) is checked against the channel: sent -> marked sent; not sent -> queued again;
can't tell -> failed, so a person decides (`cormon outbox --send ID`) rather than risking a duplicate."""

from __future__ import annotations

from . import db
from .channels import email_channel, teams_channel

RETRY_MINUTES = (0.5, 2, 10, 30, 120)  # the wait after each failed try; then the row is failed
STALE_MINUTES = 10


def is_paused() -> bool:
    return db.get_state("paused") == "1"


def _adapter(channel: str):
    adapter = email_channel() if channel == "email" else teams_channel()
    if adapter is None:
        raise RuntimeError(f"{channel} channel is off")
    return adapter


def _row(row) -> dict:
    r = dict(row)
    r["to_addrs"] = db.unjs(r["to_addrs"], [])
    r["cc_addrs"] = db.unjs(r["cc_addrs"], [])
    r["meta"] = db.unjs(r["meta"], {})
    return r


def _claim(oid: int, statuses: tuple[str, ...]) -> bool:
    marks = ",".join("?" * len(statuses))
    return db.changed(f"UPDATE outbox SET status='sending', claimed_at=?, attempts=attempts+1"
                      f" WHERE id=? AND status IN ({marks})", (db.now(), oid, *statuses)) == 1


def _mark_sent(r: dict, result: dict, **extra) -> None:
    db.ex("UPDATE outbox SET status='sent', sent_at=?, external_ref=?, web_url=?,"
          " rfc_message_id=COALESCE(?, rfc_message_id), last_error=NULL WHERE id=?",
          (db.now(), result.get("external_ref"), result.get("web_url"), result.get("rfc_message_id"), r["id"]))
    db.audit("sent", entity_type="item" if r["item_id"] else "outbox",
             entity_id=r["item_id"] or r["id"], outbox_id=r["id"], channel=r["channel"],
             destination=r["destination"], to=r["to_addrs"], external_ref=result.get("external_ref"), **extra)


def dispatch(limit: int = 50, only_id: int | None = None) -> dict:
    if is_paused() and only_id is None:
        return {"sent": 0, "failed": 0, "paused": True}
    if only_id is None:
        statuses: tuple[str, ...] = ("queued",)
        rows = db.q("SELECT * FROM outbox WHERE status='queued' AND (next_try_at IS NULL OR next_try_at<=?)"
                    " ORDER BY id LIMIT ?", (db.now(), limit))
    else:
        statuses = ("queued", "failed", "held")
        rows = db.q("SELECT * FROM outbox WHERE id=? AND status IN ('queued','failed','held')", (only_id,))
    sent = failed = 0
    for row in rows:
        r = _row(row)
        if r["item_id"]:
            item = db.q1("SELECT state FROM items WHERE id=?", (r["item_id"],))
            if item and item["state"] == "suppressed":
                db.ex("UPDATE outbox SET status='held', last_error='item suppressed' WHERE id=? AND status IN"
                      f" ({','.join('?' * len(statuses))})", (r["id"], *statuses))
                continue
        if not _claim(r["id"], statuses):
            continue  # another sender has it
        r["attempts"] += 1
        try:
            _mark_sent(r, _adapter(r["channel"]).send(r))
            sent += 1
        except Exception as e:
            last = r["attempts"] > len(RETRY_MINUTES)
            db.ex("UPDATE outbox SET status=?, next_try_at=?, last_error=? WHERE id=?",
                  ("failed" if last else "queued", None if last else db.later(RETRY_MINUTES[r["attempts"] - 1]),
                   str(e)[:500], r["id"]))
            db.audit("send_failed", entity_type="item" if r["item_id"] else "outbox",
                     entity_id=r["item_id"] or r["id"], outbox_id=r["id"], error=str(e)[:500],
                     attempt=r["attempts"], gave_up=last or None)
            failed += 1
    return {"sent": sent, "failed": failed, "paused": False}


def recover(log=print) -> dict:
    """Rows left 'sending' by a stop mid-send: ask the channel whether the message went out."""
    out = {"confirmed": 0, "requeued": 0, "unknown": 0}
    for row in db.q("SELECT * FROM outbox WHERE status='sending' AND (claimed_at IS NULL OR claimed_at<?)",
                    (db.later(-STALE_MINUTES),)):
        r = _row(row)
        try:
            adapter = _adapter(r["channel"])
            found = adapter.find_sent(r) if hasattr(adapter, "find_sent") else None
        except Exception as e:
            found, error = None, str(e)[:300]
        else:
            error = None
        if found:
            _mark_sent(r, found, after_interrupt=True)
            out["confirmed"] += 1
        elif found is False:
            db.ex("UPDATE outbox SET status='queued', next_try_at=NULL, last_error=? WHERE id=?",
                  ("stopped while sending; it had not gone out, so it is sent again", r["id"]))
            db.audit("send_retried_after_interrupt", entity_type="outbox", entity_id=r["id"], channel=r["channel"])
            out["requeued"] += 1
        else:
            db.ex("UPDATE outbox SET status='failed', last_error=? WHERE id=?",
                  ("stopped while sending and the channel could not say if it arrived; check, then send it with"
                   f" `cormon outbox --send {r['id']}`", r["id"]))
            db.audit("send_interrupted_unknown", entity_type="outbox", entity_id=r["id"], channel=r["channel"],
                     error=error)
            out["unknown"] += 1
        log(f"outbox {r['id']} was left sending: {'sent' if found else 'queued again' if found is False else 'failed'}")
    return out

"""Sends queued drafts through the channel adapters and records the outcome."""

from __future__ import annotations

from . import db
from .channels import email_channel, teams_channel


def is_paused() -> bool:
    return db.get_state("paused") == "1"


def dispatch(limit: int = 50, only_id: int | None = None) -> dict:
    if is_paused() and only_id is None:
        return {"sent": 0, "failed": 0, "paused": True}
    sql = "SELECT * FROM outbox WHERE status='queued'"
    params: list = []
    if only_id is not None:
        sql = "SELECT * FROM outbox WHERE id=? AND status IN ('queued','failed','held')"
        params = [only_id]
    sql += " ORDER BY id LIMIT ?"
    params.append(limit)
    sent = failed = 0
    for row in db.q(sql, params):
        r = dict(row)
        r["to_addrs"] = db.unjs(r["to_addrs"], [])
        r["cc_addrs"] = db.unjs(r["cc_addrs"], [])
        r["meta"] = db.unjs(r["meta"], {})
        if r["item_id"]:
            item = db.q1("SELECT state FROM items WHERE id=?", (r["item_id"],))
            if item and item["state"] == "suppressed":
                db.ex("UPDATE outbox SET status='held', last_error='item suppressed' WHERE id=?", (r["id"],))
                continue
        try:
            adapter = email_channel() if r["channel"] == "email" else teams_channel()
            if adapter is None:
                raise RuntimeError(f"{r['channel']} channel is off")
            result = adapter.send(r)
            db.ex("UPDATE outbox SET status='sent', attempts=attempts+1, sent_at=?, external_ref=?, web_url=?,"
                  " rfc_message_id=COALESCE(?, rfc_message_id), last_error=NULL WHERE id=?",
                  (db.now(), result.get("external_ref"), result.get("web_url"), result.get("rfc_message_id"),
                   r["id"]))
            db.audit("sent", entity_type="item" if r["item_id"] else "outbox",
                     entity_id=r["item_id"] or r["id"], outbox_id=r["id"], channel=r["channel"],
                     destination=r["destination"], to=r["to_addrs"], external_ref=result.get("external_ref"))
            sent += 1
        except Exception as e:
            db.ex("UPDATE outbox SET status=CASE WHEN attempts>=4 THEN 'failed' ELSE 'queued' END,"
                  " attempts=attempts+1, last_error=? WHERE id=?", (str(e)[:500], r["id"]))
            db.audit("send_failed", entity_type="item" if r["item_id"] else "outbox",
                     entity_id=r["item_id"] or r["id"], outbox_id=r["id"], error=str(e)[:500])
            failed += 1
    return {"sent": sent, "failed": failed, "paused": False}

"""Recalling or correcting a message the bot sent (OUT-07).

A sent message cannot be unsent. The bot sends a follow-up to the same people, in the same email thread or chat
thread: "please disregard" (recall) or "correction" with the right words. A message not sent yet is held instead,
so it never goes; a correction then goes out in its place. The original is never changed: both stay on record,
linked both ways in the audit trail.
"""

from __future__ import annotations

from . import db, routing
from .drafting import local_time


class CannotCorrect(ValueError):
    pass


def _thread(orig: dict, meta: dict) -> dict:
    """Where the follow-up goes: the original's email thread or chat thread."""
    if orig["channel"] == "email":
        out = {"thread_key": meta.get("thread_key"), "gmail_thread": meta.get("gmail_thread")}
        if orig.get("rfc_message_id"):
            out["in_reply_to"] = orig["rfc_message_id"]
            out["references"] = (meta.get("references") or []) + [orig["rfc_message_id"]]
        return out
    return {"target": meta.get("target") or {}, "thread_key": meta.get("thread_key")}


def _body(orig: dict, text: str | None, reason: str) -> str:
    when = local_time(orig.get("sent_at")) if orig.get("sent_at") else None
    which = f'my message{" " + repr(orig["subject"]) if orig.get("subject") else ""}' + (f" sent {when}" if when else "")
    if text:
        return f"Correction to {which}:\n\n{text}\n\nReason: {reason}\n\nPlease use this version instead."
    return f"Please disregard {which}. It was sent in error.\n\nReason: {reason}"


def follow_up(outbox_id: int, actor: str, reason: str, text: str | None = None) -> dict:
    """Recall (text None) or correct one outgoing message. Returns {held, outbox_id} of what was done."""
    if not (reason or "").strip():
        raise CannotCorrect("give the reason (--reason)")
    row = db.q1("SELECT * FROM outbox WHERE id=?", (outbox_id,))
    if row is None:
        raise CannotCorrect(f"there is no outgoing message {outbox_id}")
    orig = dict(row)
    meta = db.unjs(orig["meta"], {})
    if orig["purpose"] in ("correction", "recall") and not text:
        raise CannotCorrect(f"message {outbox_id} is itself a {orig['purpose']}; correct it with --text instead")
    held = False
    if orig["status"] in ("queued", "held", "failed", "sending"):
        if orig["status"] == "sending":
            raise CannotCorrect(f"message {outbox_id} is being sent right now; try again in a minute")
        db.ex("UPDATE outbox SET status='held', last_error=? WHERE id=? AND status IN ('queued','held','failed')",
              (f"recalled before it was sent: {reason}"[:500], outbox_id))
        held = True
    new_id = None
    if text or not held:
        purpose = "correction" if text else "recall"
        subject = orig["subject"] if held else orig["subject"] and (
            ("Correction: " if text else "Please disregard: ") + orig["subject"].removeprefix("Re: "))
        body = text if (held and text) else _body(orig, text, reason)
        new_id = routing.enqueue(
            purpose=purpose, channel=orig["channel"], destination=orig["destination"], body=body,
            subject=subject, item_id=orig["item_id"], message_id=orig["message_id"], from_addr=orig["from_addr"],
            to=db.unjs(orig["to_addrs"], []), cc=db.unjs(orig["cc_addrs"], []),
            meta=dict(_thread(orig, meta), corrects=outbox_id), study_id=orig.get("study_id"), again=True)
    db.audit("sent_message_recalled" if not text else "sent_message_corrected",
             entity_type="item" if orig["item_id"] else "outbox", entity_id=orig["item_id"] or outbox_id,
             study_id=orig.get("study_id"), actor=actor, actor_type="operator", outbox_id=outbox_id,
             was=orig["status"], held_before_sending=held or None, follow_up_outbox_id=new_id, reason=reason)
    return {"held": held, "outbox_id": new_id}

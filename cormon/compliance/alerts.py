"""Telling a person the bot is unhealthy (stopped, a failed integrity check). Each problem is sent once and again
at most every `every_minutes` while it lasts; when it ends, one "running again" note.

The email backend sends at once through the outbox (so the alert is kept and fingerprinted like every sent
message), even while sending is paused. Recipients: system.ops_emails, else the admins (web.admin.emails).
Certified backend (later): a pager with on-call escalation.
"""

from __future__ import annotations

from datetime import datetime

from .. import db, settings
from . import backend

BUILT = ("email",)


def recipients() -> list[str]:
    return list(settings.get("system.ops_emails") or settings.get("web.admin.emails") or [])


def _send(key: str, subject: str, body: str) -> int | None:
    from .. import outbox, routing
    to = recipients()
    if not to:
        db.audit("ops_alert_no_recipients", entity_type="service", entity_id=key, subject=subject)
        return None
    oid = routing.enqueue(purpose="ops_alert", channel="email", destination="email", body=body, subject=subject,
                          from_addr=settings.get("mailbox.address"), to=to, again=True)
    outbox.dispatch(only_id=oid)
    db.audit("ops_alerted", entity_type="service", entity_id=key, outbox_id=oid, to=to, subject=subject)
    return oid


def notify(key: str, subject: str, body: str, *, every_minutes: int = 60) -> int | None:
    backend("alerts", BUILT)
    last = db.get_state(f"alert:{key}")
    if last and (datetime.fromisoformat(db.now()) - datetime.fromisoformat(last)).total_seconds() < every_minutes * 60:
        return None
    db.set_state(f"alert:{key}", db.now())
    return _send(key, subject, body)


def clear(key: str, subject: str, body: str) -> int | None:
    """The problem has ended: one note if an alert for it went out."""
    backend("alerts", BUILT)
    if not db.get_state(f"alert:{key}"):
        return None
    db.set_state(f"alert:{key}", "")
    return _send(key, subject, body)

"""The watchdog: run every 5 minutes by its own timer (deploy/cormon-watchdog.timer), outside the listener, so a
listener that has stopped or hung is still noticed. The listener writes a heartbeat every round; when it is older
than system.stall_minutes (15), the admins are emailed, again every hour while it lasts, and once when it is back."""

from __future__ import annotations

from datetime import datetime, timezone

from . import db, settings
from .compliance import alerts


def minutes_since_heartbeat() -> float | None:
    beat = db.get_state("heartbeat")
    if not beat:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() / 60
    except ValueError:
        return None


def stalled() -> bool:
    age = minutes_since_heartbeat()
    return age is None or age > float(settings.get("system.stall_minutes", 15))


def check(log=print) -> dict:
    age = minutes_since_heartbeat()
    if stalled():
        if not db.get_state("down_since"):
            db.set_state("down_since", db.get_state("heartbeat") or db.now())
        when = "never" if age is None else f"{age:.0f} minutes ago"
        oid = alerts.notify("stalled", "Study monitor: the bot has stopped",
                            f"The bot last checked in {when}. Until it is running again it is not reading or "
                            "sending messages. Messages are not lost: they are read when it restarts.")
        log(f"bot stalled (last heartbeat {when}){'; admins emailed' if oid else ''}")
        return {"stalled": True, "minutes": age, "alerted": oid}
    since = db.get_state("down_since")
    if since:   # the gap is kept for the digest (digest.py)
        db.audit("bot_downtime", entity_type="service", entity_id="listener", start=since, end=db.now())
        db.set_state("down_since", "")
    oid = alerts.clear("stalled", "Study monitor: the bot is running again",
                       f"The bot checked in {age:.0f} minute(s) ago and is reading messages again.")
    return {"stalled": False, "minutes": age, "recovered": oid}

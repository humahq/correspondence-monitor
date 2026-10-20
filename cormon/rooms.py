"""Who is in a chat room (a Google Chat space): the chat's own member list, and everyone who has written there.

The member list is asked of the chat channel and kept for CACHE_MINUTES. When the channel can't give it (an API
error, or a channel without member lists) only the people who have written in the room are known, and the answer
says the list may be incomplete. Nothing here raises."""

from __future__ import annotations

import time

from . import db
from .channels import teams_channel

CACHE_MINUTES = 10
_listed: dict[str, tuple[float, list[str] | None, str | None]] = {}


def forget() -> None:
    _listed.clear()


def _member_list(space: str) -> tuple[list[str] | None, str | None]:
    """(emails, error) from the chat channel; emails is None when it could not say."""
    now = time.monotonic()
    hit = _listed.get(space)
    if hit and now - hit[0] < CACHE_MINUTES * 60:
        return hit[1], hit[2]
    try:
        ch = teams_channel()
        listed = ch.members(space) if ch is not None and hasattr(ch, "members") else None
        error = None
    except Exception as e:
        listed, error = None, str(e)[:300]
        db.audit("room_members_failed", entity_type="space", entity_id=space, error=error)
    listed = None if listed is None else [e.lower() for e in listed if e]
    _listed[space] = (now, listed, error)
    return listed, error


def wrote_in(space: str) -> set[str]:
    """Everyone who has written in the room, from the messages kept."""
    prefix = f"teams:{space}/threads/"
    try:
        rows = db.q("SELECT DISTINCT lower(from_addr) a FROM messages WHERE channel='teams' AND from_addr IS NOT NULL"
                    " AND substr(thread_key, 1, ?)=?", (len(prefix), prefix))
    except Exception:
        return set()
    return {r["a"] for r in rows if r["a"]}


def members(space: str) -> dict:
    """{emails, complete, error}: the room's listed members and everyone who has written there. complete is False
    when the member list could not be read, so people who are in the room but never wrote may be missing."""
    try:
        listed, error = _member_list(space)
    except Exception as e:
        listed, error = None, str(e)[:300]
    return {"emails": set(listed or []) | wrote_in(space), "complete": listed is not None, "error": error}

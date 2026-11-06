"""What was said just before a message in the same conversation, so a follow-up ("and week 8?") or a correction
("sorry, I meant 012") is understood. A conversation is one chat space or one email thread; nothing is ever taken
from another chat. Only the last few messages are kept, from the last half hour, cut to a size limit."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta

from . import db

TURNS = 20
MINUTES = 30
CHARS = 6000
TURN_CHARS = 600


def _space(thread_key: str) -> str | None:
    m = re.fullmatch(r"teams:(spaces/[^/]+)/threads/[^/]+", thread_key or "")
    return m.group(1) if m else None


def _plain(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = " ".join(text.split())
    return text if len(text) <= TURN_CHARS else text[:TURN_CHARS].rsplit(" ", 1)[0] + " ..."


def recent(msg: dict, minutes: int = MINUTES, turns_max: int = TURNS, chars: int = CHARS) -> list[dict]:
    """[{"who", "text", "at"}] oldest first: people's messages and the bot's replies before this message."""
    at = msg.get("received_at") or db.now()
    since = (datetime.fromisoformat(at) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    space = _space(msg.get("thread_key"))
    if not msg.get("thread_key") or (msg.get("channel") == "teams" and not space):
        return []
    if space:
        people = db.q("SELECT from_name, from_addr, body_text, received_at FROM messages WHERE channel='teams'"
                      " AND thread_key LIKE ? AND id<? AND received_at>=? ORDER BY id DESC LIMIT ?",
                      (f"teams:{space}/threads/%", msg["id"], since, turns_max))
        bot = db.q("SELECT body, sent_at FROM outbox WHERE channel='teams' AND status='sent'"
                   " AND purpose!='chat_received'"   # "give me a moment" is not an answer
                   " AND json_extract(meta, '$.target.space')=? AND sent_at>=? AND sent_at<=?"
                   " ORDER BY id DESC LIMIT ?", (space, since, at, turns_max))
    else:
        people = db.q("SELECT from_name, from_addr, body_text, received_at FROM messages WHERE thread_key=?"
                      " AND id<? AND received_at>=? ORDER BY id DESC LIMIT ?",
                      (msg["thread_key"], msg["id"], since, turns_max))
        bot = db.q("SELECT body, sent_at FROM outbox WHERE channel='email' AND status='sent'"
                   " AND json_extract(meta, '$.thread_key')=? AND sent_at>=? AND sent_at<=?"
                   " ORDER BY id DESC LIMIT ?", (msg["thread_key"], since, at, turns_max))
    turns = [{"who": r["from_name"] or r["from_addr"] or "someone", "text": _plain(r["body_text"]),
              "at": r["received_at"]} for r in people]
    turns += [{"who": "the bot", "text": _plain(r["body"]), "at": r["sent_at"]} for r in bot]
    turns = sorted(turns, key=lambda t: t["at"])[-turns_max:]
    while turns and sum(len(t["text"]) for t in turns) > chars:
        turns.pop(0)
    return [t for t in turns if t["text"]]


ABOUT_DAYS = 30


def about(msg: dict, participant: str | None) -> list[dict]:
    """What the readers of this conversation have already seen that may show who a participant is (privacy.py):
    the last few turns, and every turn in the last 30 days that names the participant's study ID, oldest first."""
    turns = recent(msg, minutes=ABOUT_DAYS * 24 * 60, turns_max=500, chars=10 ** 9)
    pid = (participant or "").lower()
    keep = [t for i, t in enumerate(turns) if i >= len(turns) - TURNS or (pid and pid in t["text"].lower())]
    while keep and sum(len(t["text"]) for t in keep) > CHARS:
        keep.pop(0)
    return keep


def block(turns: list[dict]) -> str:
    return "\n".join(f"{t['who']}: {t['text']}" for t in turns)


def bot_answered_lately(turns: list[dict]) -> bool:
    return bool(turns) and any(t["who"] == "the bot" for t in turns[-3:])

"""Operator notes: a fact or instruction with an explicit scope."""

from __future__ import annotations

from . import db

SCOPES = ("message", "person", "study", "global")


def add(scope: str, target: str | None, kind: str, text: str, author: str) -> int:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if scope != "global" and not target:
        raise ValueError(f"a {scope} note needs a target")
    nid = db.ex("INSERT INTO notes(scope, target, kind, text, author, created_at) VALUES (?,?,?,?,?,?)",
                (scope, target if scope != "global" else None, kind, text, author, db.now()))
    db.audit("note_added", entity_type="note", entity_id=nid, actor=author, actor_type="operator",
             study_id=target if scope == "study" else None, scope=scope, target=target, kind=kind, text=text)
    return nid


def retire(note_id: int, author: str) -> None:
    db.ex("UPDATE notes SET active=0 WHERE id=?", (note_id,))
    db.audit("note_retired", entity_type="note", entity_id=note_id, actor=author, actor_type="operator")


def applicable(message_id: int | None, study_id: str | None, emails: list[str]) -> list[dict]:
    """Notes that govern this message: its own, its people's, its study's, and global ones."""
    rows = db.q("SELECT * FROM notes WHERE active=1 ORDER BY id")
    emails_l = {e.lower() for e in emails if e}
    out = []
    for r in rows:
        if r["scope"] == "global" \
                or (r["scope"] == "study" and r["target"] == study_id) \
                or (r["scope"] == "message" and message_id is not None and r["target"] == str(message_id)) \
                or (r["scope"] == "person" and (r["target"] or "").lower() in emails_l):
            out.append(dict(r))
    return out


def render(notes_: list[dict]) -> str:
    if not notes_:
        return "(none)"
    return "\n".join(f"- [{n['scope']}{':' + n['target'] if n['target'] else ''}] ({n['kind']}) {n['text']}"
                     for n in notes_)

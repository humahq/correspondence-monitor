"""Operator notes: a fact or instruction with an explicit scope.

A note applies once a second person has approved it and its start date has come (DSP-06). A change is a new
version that replaces the old one when approved; the old one is kept. notes.need_approval: false makes a note
apply at once (a practice copy)."""

from __future__ import annotations

from . import db, settings

SCOPES = ("message", "person", "study", "global")


class NoteRefused(ValueError):
    pass


def _need_approval() -> bool:
    return bool(settings.get("notes.need_approval", True))


def get(note_id: int) -> dict:
    row = db.q1("SELECT * FROM notes WHERE id=?", (note_id,))
    if row is None:
        raise NoteRefused(f"there is no note {note_id}")
    return dict(row)


def add(scope: str, target: str | None, kind: str, text: str, author: str, *, effective_from: str | None = None,
        replaces: int | None = None, version: int = 1) -> int:
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if scope != "global" and not target:
        raise ValueError(f"a {scope} note needs a target")
    approved = None if _need_approval() else author
    nid = db.ex("INSERT INTO notes(scope, target, kind, text, author, created_at, version, replaces, effective_from,"
                " approved_by, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scope, target if scope != "global" else None, kind, text, author, db.now(), version, replaces,
                 effective_from, approved, approved and db.now()))
    db.audit("note_added", entity_type="note", entity_id=nid, actor=author, actor_type="operator",
             study_id=target if scope == "study" else None, scope=scope, target=target, kind=kind, text=text,
             version=version, replaces=replaces, effective_from=effective_from, waiting_for_approval=not approved)
    return nid


def edit(note_id: int, text: str, author: str, effective_from: str | None = None) -> int:
    """A new version of a note; the old one applies until the new one is approved and starts."""
    old = get(note_id)
    if not old["active"]:
        raise NoteRefused(f"note {note_id} is retired")
    return add(old["scope"], old["target"], old["kind"], text, author, effective_from=effective_from,
               replaces=note_id, version=(old["version"] or 1) + 1)


def approve(note_id: int, who: str) -> dict:
    n = get(note_id)
    if n["approved_by"]:
        raise NoteRefused(f"note {note_id} was already approved by {n['approved_by']}")
    if who.lower() == n["author"].lower():
        db.audit("note_approval_refused", entity_type="note", entity_id=note_id, actor=who, actor_type="operator",
                 why="the author may not approve their own note")
        raise NoteRefused(f"note {note_id} was written by {who}; a different person must approve it")
    db.ex("UPDATE notes SET approved_by=?, approved_at=? WHERE id=? AND approved_by IS NULL", (who, db.now(), note_id))
    db.audit("note_approved", entity_type="note", entity_id=note_id, actor=who, actor_type="operator",
             author=n["author"], replaces=n["replaces"], effective_from=n["effective_from"])
    return get(note_id)


def retire(note_id: int, author: str) -> None:
    db.ex("UPDATE notes SET active=0 WHERE id=?", (note_id,))
    db.audit("note_retired", entity_type="note", entity_id=note_id, actor=author, actor_type="operator")


def _in_force(at: str | None = None) -> list[dict]:
    """Active, approved notes whose start date has come, without those a newer version in force replaces."""
    at = at or db.now()
    rows = [dict(r) for r in db.q("SELECT * FROM notes WHERE active=1 AND approved_by IS NOT NULL"
                                  " AND (effective_from IS NULL OR effective_from<=?) ORDER BY id", (at,))]
    replaced = {r["replaces"] for r in rows if r["replaces"]}
    return [r for r in rows if r["id"] not in replaced]


def status(n: dict, at: str | None = None) -> str:
    at = at or db.now()
    if not n["active"]:
        return "retired"
    if not n["approved_by"]:
        return "waiting for approval"
    if n["effective_from"] and n["effective_from"] > at:
        return f"starts {n['effective_from']}"
    if db.q1("SELECT 1 FROM notes WHERE replaces=? AND active=1 AND approved_by IS NOT NULL"
             " AND (effective_from IS NULL OR effective_from<=?)", (n["id"], at)):
        return "replaced"
    return "in force"


def history(note_id: int) -> list[dict]:
    """Every version of the note, oldest first."""
    first = get(note_id)
    while first["replaces"]:
        first = get(first["replaces"])
    out, todo = [], [first]
    while todo:
        n = todo.pop(0)
        out.append(dict(n, status=status(n)))
        todo += [dict(r) for r in db.q("SELECT * FROM notes WHERE replaces=? ORDER BY id", (n["id"],))]
    return out


def applicable(message_id: int | None, study_id: str | None, emails: list[str]) -> list[dict]:
    """Notes that govern this message: its own, its people's, its study's, and global ones."""
    emails_l = {e.lower() for e in emails if e}
    out = []
    for r in _in_force():
        if r["scope"] == "global" \
                or (r["scope"] == "study" and r["target"] == study_id) \
                or (r["scope"] == "message" and message_id is not None and r["target"] == str(message_id)) \
                or (r["scope"] == "person" and (r["target"] or "").lower() in emails_l):
            out.append(r)
    return out


def render(notes_: list[dict]) -> str:
    if not notes_:
        return "(none)"
    return "\n".join(f"- [{n['scope']}{':' + n['target'] if n['target'] else ''}] ({n['kind']}) {n['text']}"
                     for n in notes_)

"""Two people for an action that destroys data or hides an alert the sponsor would see (SEC-04, DSP-05).

One person asks (the command, run without an approval, records the request and stops). A different person
approves it (`cormon approve ID`). Then the action is run with `--approval ID`: the approval covers that one action
on that one target, once, within APPROVAL_HOURS. Every step is in the audit trail.

Names on the command line come from --as or the computer login, which the bot cannot check (IAM-01): this stops a
slip by one person, not two people agreeing. compliance.two_person: false turns it off (a practice copy).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .. import db, settings

APPROVAL_HOURS = 24
ACTIONS = {
    "reset": "archive the database and start an empty one",
    "restore": "replace the database with a backup",
    "purge": "clear message text older than the retention period",
    "forget-participant": "delete what the bot remembers about a participant",
    "import-model": "replace the study model",
    "disregard": "disregard an alert the sponsor would see",
    "not-counted": "mark an alert the sponsor would see as not counting",
}


class Refused(PermissionError):
    """The approval does not cover this action, is not approved, was used, expired, or is the asker's own."""


def on() -> bool:
    return bool(settings.get("compliance.two_person", True))


def request(action: str, target: str, who: str, reason: str | None) -> int:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action}")
    rid = db.ex("INSERT INTO approvals (action, target, reason, requested_by, requested_at) VALUES (?,?,?,?,?)",
                (action, target, reason, who, db.now()))
    db.audit("approval_requested", entity_type="approval", entity_id=rid, actor=who, actor_type="operator",
             action_asked=action, target=target, reason=reason)
    return rid


def get(rid: int) -> dict:
    row = db.q1("SELECT * FROM approvals WHERE id=?", (rid,))
    if row is None:
        raise Refused(f"there is no approval request #{rid}")
    return dict(row)


def _expired(iso: str) -> bool:
    return datetime.fromisoformat(iso) < datetime.fromisoformat(db.now()) - timedelta(hours=APPROVAL_HOURS)


def _refuse(rid: int, who: str, why: str, step: str) -> None:
    db.audit("approval_refused", entity_type="approval", entity_id=rid, actor=who, actor_type="operator",
             step=step, why=why)
    raise Refused(why)


def approve(rid: int, who: str) -> dict:
    a = get(rid)
    if a["approved_by"]:
        _refuse(rid, who, f"#{rid} was already approved by {a['approved_by']}", "approve")
    if who.lower() == a["requested_by"].lower():
        _refuse(rid, who, f"#{rid} was asked for by {who}; a different person must approve it", "approve")
    if _expired(a["requested_at"]):
        _refuse(rid, who, f"#{rid} is older than {APPROVAL_HOURS} hours; ask again", "approve")
    db.ex("UPDATE approvals SET approved_by=?, approved_at=? WHERE id=? AND approved_by IS NULL", (who, db.now(), rid))
    db.audit("approval_given", entity_type="approval", entity_id=rid, actor=who, actor_type="operator",
             action_asked=a["action"], target=a["target"], requested_by=a["requested_by"])
    return get(rid)


def use(rid: int, action: str, target: str, who: str) -> dict:
    a = get(rid)
    if a["action"] != action or a["target"] != target:
        _refuse(rid, who, f"#{rid} approves {a['action']} {a['target']}, not {action} {target}", "use")
    if not a["approved_by"]:
        _refuse(rid, who, f"#{rid} is not approved yet: another person runs `cormon approve {rid}`", "use")
    if a["used_at"]:
        _refuse(rid, who, f"#{rid} was already used on {a['used_at']}", "use")
    if who.lower() == a["approved_by"].lower():
        _refuse(rid, who, f"#{rid} was approved by {who}, who may not also run it", "use")
    if _expired(a["approved_at"]):
        _refuse(rid, who, f"#{rid} was approved more than {APPROVAL_HOURS} hours ago; ask again", "use")
    if not db.changed("UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL", (db.now(), rid)):
        _refuse(rid, who, f"#{rid} was already used", "use")
    db.audit("approval_used", entity_type="approval", entity_id=rid, actor=who, actor_type="operator",
             action_asked=action, target=target, approved_by=a["approved_by"])
    return get(rid)


def gate(action: str, target: str, who: str, reason: str | None, approval: int | None, say=print) -> bool:
    """True when the action may go ahead now. Without an approval, the request is recorded and False returned."""
    if not on():
        return True
    if approval:
        use(approval, action, target, who)
        return True
    rid = request(action, target, who, reason)
    say(f"This needs a second person ({ACTIONS[action]}). Request #{rid} recorded.\n"
        f"  1. Someone else runs:  cormon --as <their name> approve {rid}\n"
        f"  2. Then run this command again with  --approval {rid}")
    return False


def pending() -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM approvals WHERE used_at IS NULL ORDER BY id DESC LIMIT 50")]

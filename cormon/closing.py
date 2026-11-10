"""Closing an alert on its web page (DSP-01, DSP-02, DSP-04; Eliot, 29 Oct and 10 Nov), behind
features.close_on_page. The person signed in with Google picks one of three fixed choices, so the AI decides nothing:

- "I did what was asked", and when: the alert is cleared.
- "I did something else", what and when: it waits for a second person.
- "Nothing can be done", and why: it waits for a second person too.

The second person (the PI) approves it (cleared), sends it back (open again, the person is told), or reports it to
the sponsor (cleared as reported, the sponsor is told). Nobody reviews their own choice. Who may close and who may
review are roles in study.yaml `closing:`. Every step is in the audit log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, items, routing, settings
from .config import Person, Study
from .drafting import item_link, local_time

CHOICES = {"done": "I did what was asked", "other": "I did something else", "nothing": "Nothing can be done"}
REVIEWS = {"approve": "Approve: close the alert", "send_back": "Send it back to be fixed",
           "report": "Do not approve: report it to the sponsor"}
APPROVED_CODE = {"other": "other_fix_approved", "nothing": "not_remediable_approved"}
DEFAULT_ROLES = {"close": ["crc", "cra", "clinops_lead", "tester"], "review": ["pi"]}


class NotAllowed(PermissionError):
    """Someone who may not close or review this alert tried to."""


def on() -> bool:
    return settings.feature("close_on_page")


def roles(study: Study, what: str) -> list[str]:
    return list((study.raw.get("closing") or {}).get(what) or DEFAULT_ROLES[what])


def person(study: Study, email: str | None, what: str) -> Person | None:
    """The study person signed in as `email`, when one of their roles may `what` ("close" or "review")."""
    p = study.person_by_email(email) if email else None
    return p if p and set(p.roles) & set(roles(study, what)) else None


def latest(item_id: int) -> dict | None:
    row = db.q1("SELECT * FROM closings WHERE item_id=? ORDER BY id DESC LIMIT 1", (item_id,))
    return dict(row) if row else None


def waiting(study_id: str | None = None) -> list[dict]:
    """Closings waiting for a second person's review, oldest first."""
    sql = "SELECT c.* FROM closings c JOIN items i ON i.id=c.item_id WHERE c.status='waiting' AND i.state='waiting'"
    params = []
    if study_id:
        sql += " AND c.study_id=?"; params.append(study_id)
    return [dict(r) for r in db.q(sql + " ORDER BY c.id", params)]


def when_done(value: str) -> str:
    """The date and time the person gave (their local time, from the page), as UTC. Not in the future."""
    try:
        local = datetime.fromisoformat((value or "").strip())
    except ValueError:
        raise ValueError("give the date and time it was done") from None
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    at = (local if local.tzinfo else local.replace(tzinfo=tz)).astimezone(timezone.utc)
    if at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("the date and time it was done is in the future")
    return at.isoformat(timespec="seconds")


def check_close(item: dict, study: Study, email: str | None, choice: str, done_at: str, details: str) -> dict:
    """What closing would record, or ValueError / NotAllowed saying why it can't be done. Nothing is changed."""
    p = person(study, email, "close")
    if p is None:
        raise NotAllowed(f"{email or 'Someone not signed in'} may not close alerts in {study.short_name}")
    if item["state"] != "open" or item["kind"] != "finding":
        raise ValueError(f"alert #{item['id']} is not open, so it cannot be closed here")
    if why := items.order_problem(item, "cleared", "page"):
        raise ValueError(why)
    if choice not in CHOICES:
        raise ValueError("pick one of the three choices")
    details = (details or "").strip()
    if choice != "done" and not details:
        raise ValueError("say what was done instead" if choice == "other" else "say why nothing can be done")
    return {"item_id": item["id"], "choice": choice, "details": details[:2000],
            "done_at": when_done(done_at) if choice != "nothing" else None,
            "by_email": p.email.lower(), "by_name": p.name}


def close(item_id: int, email: str | None, choice: str, done_at: str, details: str) -> str:
    """Record the person's choice. "done" clears the alert; the other two wait for review. Returns the new state."""
    item = items.get(item_id)
    study = config.study(item["study_id"])
    c = check_close(item, study, email, choice, done_at, details)
    who = f"{c['by_name']} <{c['by_email']}>"
    status = "closed" if choice == "done" else "waiting"
    cid = db.ex("INSERT INTO closings (item_id, study_id, choice, done_at, details, by_email, by_name, status,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (item_id, study.study_id, choice, c["done_at"], c["details"] or None, c["by_email"], c["by_name"],
                 status, db.now()))
    db.audit("item_closing_chosen", entity_type="item", entity_id=item_id, study_id=study.study_id, actor=who,
             actor_type="page", closing_id=cid, choice=choice, choice_label=CHOICES[choice], done_at=c["done_at"],
             details=c["details"] or None)
    if choice == "done":
        items.set_state(item_id, "cleared", who, "page", reason=c["details"] or CHOICES[choice],
                        study_id=study.study_id, code="fixed")
        return "cleared"
    items.set_state(item_id, "waiting", who, "page", reason=CHOICES[choice], study_id=study.study_id)
    reviewers = [p.email for p in _reviewers(study, item, c["by_email"])]
    _email(study, item, "closing_review", reviewers,
           f"Alert #{item_id} waits for your review · {study.short_name}",
           f"{c['by_name']} closed alert #{item_id} ({item['title']}) with \"{CHOICES[choice]}\"."
           + (f"\nWhen: {local_time(c['done_at'])}" if c["done_at"] else "")
           + f"\nWhat they wrote: {c['details']}\n\nPlease open the alert and approve it, send it back, or report "
             f"it to the sponsor:\n{item_link(item)}")
    if not reviewers:
        db.audit("item_closing_no_reviewer", entity_type="item", entity_id=item_id, study_id=study.study_id,
                 roles=roles(study, "review"))
    return "waiting"


def check_review(item: dict, study: Study, email: str | None, decision: str, note: str) -> dict:
    p = person(study, email, "review")
    if p is None:
        raise NotAllowed(f"{email or 'Someone not signed in'} may not review alerts in {study.short_name}")
    c = latest(item["id"])
    if item["state"] != "waiting" or not c or c["status"] != "waiting":
        raise ValueError(f"alert #{item['id']} is not waiting for a review")
    if p.email.lower() == c["by_email"]:
        raise NotAllowed("a second person must review this: you closed it yourself")
    if decision not in REVIEWS:
        raise ValueError("pick one of the three choices")
    note = (note or "").strip()
    if decision != "approve" and not note:
        raise ValueError("say why, so the person knows what to do")
    return {"item_id": item["id"], "closing_id": c["id"], "decision": decision, "note": note[:2000],
            "by_email": p.email.lower(), "by_name": p.name}


def review(item_id: int, email: str | None, decision: str, note: str) -> str:
    """The second person's decision on a waiting closing. Returns the alert's new state."""
    item = items.get(item_id)
    study = config.study(item["study_id"])
    r = check_review(item, study, email, decision, note)
    c = latest(item_id)
    who = f"{r['by_name']} <{r['by_email']}>"
    status = {"approve": "approved", "send_back": "sent_back", "report": "reported"}[decision]
    db.ex("UPDATE closings SET status=?, reviewed_by=?, review_note=?, reviewed_at=? WHERE id=?",
          (status, who, r["note"] or None, db.now(), c["id"]))
    db.audit("item_closing_reviewed", entity_type="item", entity_id=item_id, study_id=study.study_id, actor=who,
             actor_type="page", closing_id=c["id"], decision=decision, decision_label=REVIEWS[decision],
             note=r["note"] or None, closed_by=c["by_email"])
    head = f"{r['by_name']} reviewed alert #{item_id} ({item['title']})"
    said = f"\nTheir note: {r['note']}" if r["note"] else ""
    if decision == "send_back":
        items.set_state(item_id, "open", who, "page", reason=f"sent back: {r['note']}", study_id=study.study_id)
        _email(study, item, "closing_sent_back", [c["by_email"]], f"Alert #{item_id} was sent back · {study.short_name}",
               f"{head} and sent it back: \"{CHOICES[c['choice']]}\" was not enough.{said}\n\n"
               f"Please fix it and close the alert again:\n{item_link(item)}")
        return "open"
    code = APPROVED_CODE[c["choice"]] if decision == "approve" else "reported_to_sponsor"
    items.set_state(item_id, "cleared", who, "page", reason=r["note"] or REVIEWS[decision],
                    study_id=study.study_id, code=code)
    if decision == "approve":
        _email(study, item, "closing_approved", [c["by_email"]], f"Alert #{item_id} is closed · {study.short_name}",
               f"{head} and approved \"{CHOICES[c['choice']]}\". The alert is closed.{said}\n\n{item_link(item)}")
    else:
        sponsor = [p.email for p in study.people_with_role("clinops_lead", item["site"]) if p.email]
        _email(study, item, "closing_reported", sorted({*sponsor, c["by_email"]}),
               f"Alert #{item_id} reported to the sponsor · {study.short_name}",
               f"{head} and did not approve what was done. It is reported to the sponsor.\n"
               f"{c['by_name']} chose \"{CHOICES[c['choice']]}\": {c['details']}{said}\n\n{item_link(item)}")
    return "cleared"


def _reviewers(study: Study, item: dict, closer: str) -> list[Person]:
    seen, out = {closer}, []
    for role in roles(study, "review"):
        for p in study.people_with_role(role, item["site"]):
            if p.email and p.email.lower() not in seen:
                seen.add(p.email.lower())
                out.append(p)
    return out


def _email(study: Study, item: dict, purpose: str, to: list[str], subject: str, text: str) -> None:
    if to:
        routing.enqueue(purpose=purpose, channel="email", destination="email", subject=subject,
                        body=f"{text}\n\n{study.monitor_signature}", item_id=item["id"], from_addr=study.alias,
                        to=sorted(set(to)), study_id=study.study_id)

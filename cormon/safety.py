"""A safety message after it reaches the medical monitor (SAF-03 to SAF-05). Fixed code decides all of it; the AI
decides none of it.

Severity (SAF-03): severe when the message has a word from the severe list (the `safety:` part of
config/routing.yaml, plus the study file's own `safety.severe_words`), otherwise standard. Severity is routing
metadata, not a clinical judgment: it sets the clock, 1 hour (severe) or 24 hours (standard), to acknowledge. The
clock starts when the message was received (ingestion), not when the bot read it.

Acknowledgment (SAF-04): only the medical monitor or the safety backup (`safety.ack_roles`) acknowledges: by
replying "ACK <code>" (the code is in every safety email) from their address on the study's people list, passing
the sender check, or by phone, which an admin records with `cormon ack N --by <person>` naming who answered (or,
with features.ack_on_page, a person on the safety contact list records on the alert's page, webview.py). It is
a disposition: who, when, how and who recorded it, in the audit trail. Anyone else on the people list who replies
is told it did not count; a sender not on the list, or failing the sender check, is told nothing. A safety item is
still never cleared or disregarded.

Escalation (SAF-05): not acknowledged in time, the next role in `safety.chain` is emailed, each step after
`step_minutes`: the backup safety contact, then the ClinOps lead, then the terminus, the on-call person, asked to
phone the medical monitor, and the admins. Reaching the terminus is recorded as a deviation (ack_deviation_at, with
the steps that went unanswered). The terminus is reminded `terminus_reminders` times. The chain stops at the first
acknowledgment. A step with no one to email is recorded and passed over.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from . import config, db, drafting, items, privacy, senders, settings
from .config import Study

DEFAULTS = {"ack_minutes": {"severe": 60, "standard": 1440}, "step_minutes": {"severe": 30, "standard": 240},
            "chain": ["pv_backup", "clinops_lead", "safety_on_call"], "terminus_reminders": 3, "severe_words": [],
            "ack_roles": ["medical_monitor", "pv_backup"]}
ACK = re.compile(r"\bACK[\s:#-]*(S\d+-[A-Z0-9]{4})\b", re.I)


def conf(study: Study) -> dict:
    out = dict(DEFAULTS, **(config.routing().get("safety") or {}))
    out["severe_words"] = list(out["severe_words"]) + list((study.raw.get("safety") or {}).get("severe_words") or [])
    return out


def severity(study: Study, text: str) -> tuple[str, str | None]:
    """'severe' and the word found, or ('standard', None)."""
    for w in conf(study)["severe_words"]:
        if m := re.search(rf"(?i)\b{w}", text or ""):
            return "severe", m.group(0)
    return "standard", None


def ack_code(item: dict) -> str:
    return f"S{item['id']}-{hashlib.sha256(item['link_token'].encode()).hexdigest()[:4].upper()}"


def _at(minutes: int, base: str | None = None) -> str:
    start = datetime.fromisoformat(base) if base else datetime.now(timezone.utc)
    return (start.astimezone(timezone.utc) + timedelta(minutes=int(minutes))).isoformat(timespec="seconds")


def _hours(minutes: int) -> str:
    return f"{minutes} minutes" if minutes < 60 else f"{minutes // 60} hour{'s' if minutes >= 120 else ''}"


def context(study: Study, item: dict) -> dict:
    """What the safety templates show: severity, the code to reply with and when it is due."""
    c = conf(study)
    sev = item.get("severity") or "standard"
    phones = "; ".join(f"{p.name} ({study.role_label(r)}): {p.phone}" for r in ("medical_monitor", "pv_backup")
                       for p in study.people_with_role(r, item.get("site")) if p.phone)
    return {"severity": sev, "severity_label": "Severe" if sev == "severe" else "Standard",
            "within": _hours(c["ack_minutes"][sev]), "ack_code": ack_code(item),
            "due": drafting.local_time(item.get("ack_due_at")), "step": item.get("escalation_step") or 0,
            "phone_line": f"Their phone numbers: {phones}." if phones else
                          "I don't have their phone number, so please call them using the study's contact list.",
            "record_phone": " on the alert's page (the link above)" if page_on() else
                            f" (cormon ack {item.get('id')} --by <person> --note \"phoned\")"}


class NotAllowed(PermissionError):
    """Someone who may not record a phone acknowledgment on the alert's page tried to."""


def page_on() -> bool:
    return settings.feature("ack_on_page")


def ackers(study: Study, item: dict) -> list:
    """Who can have acknowledged by phone: the study's people in an acknowledging role, the item's site first."""
    roles = conf(study)["ack_roles"]
    return sorted((p for p in study.people if set(roles) & set(p.roles)),
                  key=lambda p: bool(p.sites) and item.get("site") not in p.sites)


def may_record(study: Study, email: str | None) -> bool:
    """Who may record a phone acknowledgment on the page: a study person in an acknowledging role or on the safety
    contact list (the people the escalation emails go to), or an admin."""
    if not email:
        return False
    c = conf(study)
    p = study.person_by_email(email)
    admins = [e.lower() for e in settings.get("web.admin.emails") or []]
    return bool(p and set(p.roles) & set(c["ack_roles"] + c["chain"])) or email.lower() in admins


def check_phone_ack(item: dict, study: Study, email: str | None, person_id: str, note: str) -> dict:
    """The phone acknowledgment form, checked before the confirm step: who answered, and a note."""
    if not may_record(study, email):
        raise NotAllowed(f"{email} may not record a safety acknowledgment in this study")
    if item["kind"] != "out_of_scope":
        raise ValueError(f"#{item['id']} is not a safety item")
    if item["acked_at"]:
        raise ValueError(f"it was already acknowledged by {item['acked_by']}")
    p = ack_person(study, person_id)
    return {"person": p.id, "who": f"{p.name} <{p.email}>", "label": label(study, p),
            "note": " ".join((note or "").split())[:500] or "phoned"}


def label(study: Study, p) -> str:
    roles = conf(study)["ack_roles"]
    return f"{p.name} ({', '.join(study.role_label(r) for r in p.roles if r in roles)})"


def start(item_id: int, msg: dict) -> None:
    """Set a safety item's severity and start its clock; a later, severe message about an unacknowledged item
    shortens the clock."""
    item = items.get(item_id)
    study = config.study(item["study_id"])
    sev, word = severity(study, f"{msg.get('subject') or ''}\n{msg.get('body_text') or ''}")
    c = conf(study)
    if item.get("severity") and (item["acked_at"] or item["severity"] == "severe" or sev != "severe"):
        return
    due = _at(c["ack_minutes"][sev], msg.get("received_at"))
    db.ex("UPDATE items SET severity=?, ack_due_at=?, next_step_at=?, updated_at=? WHERE id=?",
          (sev, due, due, db.now(), item_id))
    db.audit("safety_severity_set", entity_type="item", entity_id=item_id, study_id=study.study_id,
             severity=sev, word=word, ack_due_at=due, message_id=msg["id"], raised=bool(item.get("severity")))


def ack_person(study: Study, person_id: str):
    """The person named as having acknowledged (by phone): on the study's list, in an acknowledging role."""
    p = study.person(person_id)
    roles = conf(study)["ack_roles"]
    if p is None or not set(roles) & set(p.roles):
        names = ", ".join(q.id for q in study.people if set(roles) & set(q.roles)) or "nobody is listed"
        raise ValueError(f"{person_id} cannot acknowledge a safety message; who can: {names}")
    return p


def acknowledge(item_id: int, who: str, how: str, *, actor_type: str, note: str | None = None,
                message_id: int | None = None, recorded_by: str | None = None) -> bool:
    item = items.get(item_id)
    if item["kind"] != "out_of_scope":
        raise ValueError(f"#{item_id} is not a safety item")
    if item["acked_at"]:
        db.audit("safety_ack_repeat", entity_type="item", entity_id=item_id, study_id=item["study_id"], actor=who,
                 actor_type=actor_type, how=how, first_by=item["acked_by"], message_id=message_id)
        return False
    now = db.now()
    db.ex("UPDATE items SET acked_at=?, acked_by=?, next_step_at=NULL, updated_at=? WHERE id=?",
          (now, who, now, item_id))
    db.audit("safety_acknowledged", entity_type="item", entity_id=item_id, study_id=item["study_id"],
             actor=recorded_by or who, actor_type=actor_type, acknowledged_by=who, how=how, note=note,
             message_id=message_id, severity=item.get("severity"),
             ack_due_at=item.get("ack_due_at"), late=bool(item.get("ack_due_at") and now > item["ack_due_at"]),
             escalation_step=item.get("escalation_step"))
    return True


def handle_ack(msg: dict) -> bool:
    """A reply with "ACK <code>", to the study's address or the bot's own: True when it acknowledged (or repeated)
    a safety item, so it is not read as study correspondence. The code names the item, and so its study."""
    codes = {c.upper() for c in ACK.findall(f"{msg.get('subject') or ''}\n{msg.get('body_text') or ''}")}
    done = False
    for code in sorted(codes):
        try:
            item = items.get(int(code[1:].split("-")[0]))
        except KeyError:
            item = None
        if not item or item["kind"] != "out_of_scope" or ack_code(item) != code:
            db.audit("safety_ack_unknown_code", entity_type="message", entity_id=msg["id"],
                     study_id=msg.get("study_id"), code=code, sender=msg.get("from_addr"))
            continue
        study = config.study(item["study_id"])
        person = study.person_by_email(msg.get("from_addr"))
        wrong_role = person is not None and not set(conf(study)["ack_roles"]) & set(person.roles)
        why = ("the sender check failed" if senders.failed(msg) else "the sender is not on the study's people list"
               if person is None else "only the medical monitor or the safety backup can acknowledge"
               if wrong_role else None)
        if why:
            told = _send(study, item, [person], "safety_ack_refused", again=True) \
                if wrong_role and not senders.failed(msg) and person.email else []
            db.audit("safety_ack_refused", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     message_id=msg["id"], code=code, sender=msg.get("from_addr"), why=why, told=told or None)
            continue
        acknowledge(item["id"], f"{person.name} <{person.email}>", "reply", actor_type="medical",
                    message_id=msg["id"])
        done = True
    return done


def _send(study: Study, item: dict, people, topic: str, again: bool = False) -> list[int]:
    from . import routing
    tpl = drafting.select(topic, "*", "email", "*")
    quotes = any(k in f"{tpl.get('subject')}{tpl.get('body')}" for k in ("message_quote", "message_subject"))
    out = []
    for p in people:
        ctx = drafting.item_context(study, item, p)
        if quotes and settings.feature("safety_names") and not privacy.may_see_names(study, p):
            ctx = privacy.without_names(study, ctx)
        subject, body = drafting.render(tpl, ctx)
        out.append(routing.enqueue(purpose=topic, channel="email", destination="email", body=body, subject=subject,
                                   item_id=item["id"], template_id=tpl["id"], from_addr=study.alias, to=[p.email],
                                   meta={"thread_key": item["thread_key"], "item_id": item["id"]},
                                   study_id=study.study_id, again=again))
    return out


def escalate(log=print) -> list[int]:
    """Each tick: every unacknowledged safety item whose time is up goes one step up the chain."""
    from .compliance import alerts
    moved = []
    for row in db.q("SELECT id FROM items WHERE kind='out_of_scope' AND acked_at IS NULL AND next_step_at IS NOT NULL"
                    " AND next_step_at<=? ORDER BY id", (db.now(),)):
        item = items.get(row["id"])
        study = config.study(item["study_id"])
        c = conf(study)
        chain, sev = c["chain"], item.get("severity") or "standard"
        step = item["escalation_step"] + 1
        while step < len(chain) and not [p for p in study.people_with_role(chain[step - 1], item.get("site"))
                                         if p.email]:
            db.audit("safety_escalation_nobody", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     step=step, role=chain[step - 1])
            step += 1
        last = len(chain)
        role = chain[min(step, last) - 1]
        terminus = step >= last
        reminder = step - last if terminus else 0
        people = [p for p in study.people_with_role(role, item.get("site")) if p.email]
        item = dict(item, escalation_step=min(step, last))
        sent = _send(study, item, people, "safety_phone" if terminus else "safety_escalation", again=reminder > 0)
        if terminus and not item.get("ack_deviation_at"):
            rungs = [{"step": 0, "role": "medical_monitor", "at": item["created_at"]}] + [
                {"step": d.get("step"), "role": d.get("role"), "at": r["ts"]}
                for r in db.q("SELECT ts, details FROM audit_log WHERE action='safety_escalated' AND entity_type='item'"
                              " AND entity_id=? ORDER BY id", (str(item["id"]),)) for d in [db.unjs(r["details"], {})]]
            db.ex("UPDATE items SET ack_deviation_at=? WHERE id=?", (db.now(), item["id"]))
            db.audit("safety_ack_deviation", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     severity=sev, ack_due_at=item["ack_due_at"], unanswered=rungs,
                     note="not acknowledged by the end of the safety chain: a deviation (SAF-05, TRN-04)")
        if terminus:
            alerts.notify(f"safety:{item['id']}", f"Safety message not acknowledged: {study.study_id} #{item['id']}",
                          f"Safety alert #{item['id']} ({sev}) was due to be acknowledged by "
                          f"{drafting.local_time(item['ack_due_at'])} and nobody has. Phone the medical monitor now. "
                          f"{context(study, item)['phone_line']} This is recorded as a deviation.\n\n"
                          + ("When the medical monitor or the safety backup has it, record who answered on the "
                             "alert's page (sign in with Google, then \"Record who answered the phone\"): "
                             if page_on() else
                             f"When the medical monitor or the safety backup has it, record who: cormon ack "
                             f"{item['id']} --by <person id> --note \"phoned\"\n\n")
                          + drafting.item_link(item),
                          every_minutes=0)
        done = terminus and reminder >= c["terminus_reminders"]
        db.ex("UPDATE items SET escalation_step=?, next_step_at=?, updated_at=? WHERE id=?",
              (step, None if done else _at(c["step_minutes"][sev]), db.now(), item["id"]))
        db.audit("safety_escalated", entity_type="item", entity_id=item["id"], study_id=study.study_id, step=step,
                 role=role, terminus=terminus or None, reminder=reminder or None, outbox_ids=sent,
                 to=[p.email for p in people], severity=sev, last=done or None)
        log(f"safety #{item['id']}: not acknowledged, step {step} ({role})")
        moved.append(item["id"])
    return moved

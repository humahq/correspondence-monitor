"""Items: one per distinct finding in a thread. Operator and CRA values are never overwritten."""

from __future__ import annotations

import secrets

from . import config, db, hindsight
from .config import Study

OVERRIDABLE = ("tier", "recipient_person", "send_draft", "counts")
SOURCE_COL = {"tier": "tier_source", "recipient_person": "recipient_source",
              "send_draft": "send_draft_source", "counts": "counts_source"}
HUMAN_SOURCES = ("operator", "cra", "sponsor")


def get(item_id: int) -> dict:
    row = db.q1("SELECT * FROM items WHERE id=?", (item_id,))
    if row is None:
        raise KeyError(f"item {item_id} not found")
    d = dict(row)
    d["evidence"] = db.unjs(d["evidence"], {})
    return d


def choose_recipient(study: Study, site: str | None) -> tuple[str | None, str]:
    cras = study.people_with_role("cra", site)
    if cras:
        return cras[0].id, f"CRA for {study.site_name(site)}"
    leads = study.people_with_role("clinops_lead", site)
    if leads:
        return leads[0].id, "no CRA listed for this site; sponsor ClinOps lead instead"
    return None, "no CRA or ClinOps lead is listed in the study's role list"


def upsert(*, study: Study, thread_key: str, message_id: int, dedup_key: str, kind: str,
           pattern_class: str | None, rule_id: str | None, participant_id: str | None, site: str | None,
           polarity: str, title: str, tier: str, tier_reason: str, evidence: dict,
           reasoning: str | None, state: str = "open") -> tuple[int, bool]:
    """Create the item, or record a repeat mention on the existing one. Returns (id, created)."""
    existing = db.q1("SELECT * FROM items WHERE dedup_key=?", (dedup_key,))
    ts = db.now()
    if existing:
        db.ex("UPDATE items SET last_message_id=?, updated_at=? WHERE id=?", (message_id, ts, existing["id"]))
        db.audit("item_repeat_mention", entity_type="item", entity_id=existing["id"], study_id=study.study_id,
                 message_id=message_id, note="same subject in the same thread; no new item created")
        return existing["id"], False

    recipient, reason = choose_recipient(study, site)
    item_id = db.ex(
        "INSERT INTO items (study_id, thread_key, first_message_id, last_message_id, dedup_key, kind, pattern_class,"
        " rule_id, participant_id, site, polarity, title, tier, tier_reason, recipient_person, recipient_reason,"
        " send_draft, counts, state, evidence, reasoning, link_token, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (study.study_id, thread_key, message_id, message_id, dedup_key, kind, pattern_class, rule_id,
         participant_id, site, polarity, title, tier, tier_reason, recipient, reason,
         0 if kind == "out_of_scope" else 1, 0 if kind == "out_of_scope" else 1, state,
         db.js(evidence), reasoning, secrets.token_urlsafe(12), ts, ts),
    )
    db.audit("item_created", entity_type="item", entity_id=item_id, study_id=study.study_id,
             kind=kind, tier=tier, tier_reason=tier_reason, pattern_class=pattern_class, rule_id=rule_id,
             participant_id=participant_id, recipient=recipient, recipient_reason=reason,
             message_id=message_id, evidence=evidence, reasoning=reasoning)
    hindsight.save_alert(item_id, kind=kind, participant_id=participant_id)
    return item_id, True


def system_update(item_id: int, **fields) -> dict:
    """Update fields from an automatic pass, skipping any a human has set."""
    item = get(item_id)
    applied, kept = {}, {}
    for k, v in fields.items():
        src = SOURCE_COL.get(k)
        if src and item[src] in HUMAN_SOURCES:
            kept[k] = item[k]
            continue
        if item.get(k) != v:
            applied[k] = v
    if applied:
        sets = ", ".join(f"{k}=?" for k in applied)
        db.ex(f"UPDATE items SET {sets}, updated_at=? WHERE id=?", (*applied.values(), db.now(), item_id))
        db.audit("item_updated_by_system", entity_type="item", entity_id=item_id, study_id=item["study_id"],
                 changes=applied, kept_human_values=kept or None)
    return {"applied": applied, "kept": kept}


def check_consistency(item: dict) -> list[str]:
    problems = []
    if item["state"] == "suppressed" and item["send_draft"]:
        problems.append("item is suppressed but send_draft is on; nothing will be sent while suppressed")
    if not item["counts"] and item["state"] not in ("suppressed", "out_of_scope"):
        problems.append("finding does not count but the item is still open; consider suppressing it")
    if item["tier"] == "immediate" and not item["recipient_person"]:
        problems.append("immediate item has no named recipient")
    if item["tier"] == "immediate" and not item["send_draft"]:
        problems.append("immediate item will not send its drafts (send_draft is off)")
    if item["kind"] == "out_of_scope" and item["counts"]:
        problems.append("a safety (out of scope) item was set to count; the system never routes safety findings")
    if item["recipient_person"]:
        s = config.study(item["study_id"])
        if s.person(item["recipient_person"]) is None:
            problems.append(f"recipient {item['recipient_person']} is not in the study's people list")
    return problems


class OtherStudy(PermissionError):
    """A person acting on one study tried to change another study's item."""


def _in_study(item_id: int, study_id: str, actor: str, change: str) -> dict:
    item = get(item_id)
    if item["study_id"] != study_id:
        db.audit("item_change_refused_other_study", entity_type="item", entity_id=item_id,
                 study_id=item["study_id"], actor=actor, acting_for_study=study_id, change=change)
        raise OtherStudy(f"item #{item_id} is not in study {study_id}")
    return item


def override(item_id: int, field: str, value, actor: str, actor_type: str = "operator",
             reason: str | None = None, force: bool = False, *, study_id: str) -> list[str]:
    """Apply a human override. Returns inconsistencies; refuses to apply them unless forced. Only within study_id."""
    if field not in OVERRIDABLE:
        raise ValueError(f"can override only {OVERRIDABLE}")
    item = _in_study(item_id, study_id, actor, f"override {field}")
    proposed = dict(item, **{field: value})
    problems = check_consistency(proposed)
    if problems and not force:
        db.audit("override_refused_inconsistent", entity_type="item", entity_id=item_id,
                 study_id=item["study_id"], actor=actor, actor_type=actor_type, field=field,
                 value=value, problems=problems)
        return problems
    db.ex(f"UPDATE items SET {field}=?, {SOURCE_COL[field]}=?, updated_at=? WHERE id=? AND study_id=?",
          (value, actor_type, db.now(), item_id, study_id))
    db.audit("item_override", entity_type="item", entity_id=item_id, study_id=item["study_id"],
             actor=actor, actor_type=actor_type, field=field, old=item[field], new=value,
             reason=reason, forced=bool(problems) or None, problems=problems or None)
    return []


class StepOrder(ValueError):
    """A change made out of order: clearing what was never sent, clearing twice, clearing a safety item."""


# which state an item may move to from each state (ARC-03). A safety item (out_of_scope) is never cleared or
# hidden by anyone. "waiting": closed on its page with a choice a second person must review (closing.py).
NEXT = {"open": {"cleared", "suppressed", "handled", "waiting"}, "cleared": {"open"}, "suppressed": {"open"},
        "handled": {"open"}, "waiting": {"cleared", "open"}, "out_of_scope": set()}


def order_problem(item: dict, state: str, actor_type: str) -> str | None:
    """Why this change may not be made now, or None."""
    if state == item["state"]:
        return f"#{item['id']} is already {state}"
    if state not in NEXT.get(item["state"], set()):
        if item["state"] == "out_of_scope":
            return f"#{item['id']} is a safety item, which is never cleared or disregarded"
        return f"#{item['id']} cannot go from {item['state']} to {state}"
    if actor_type != "operator" and state in ("cleared", "suppressed") and not item["routed"]:
        return f"#{item['id']} was never sent to anyone, so there is nothing to clear by reply yet"
    return None


# why an alert was cleared or disregarded: one of a fixed list (DSP-04), with words as well when "other"
REASON_CODES = {
    "fixed": "Fixed or followed up at the site",
    "allowed": "Allowed by the study documents",
    "misread": "Not a problem: the bot misread the message",
    "duplicate": "Already reported in another alert",
    "deviation_logged": "Recorded as a protocol deviation",
    "not_this_study": "Not about this study",
    "other": "Other (the reason is written out)",
    "by_reply": "Cleared by a reply (the reply's words are kept)",
    # closed on the alert's page (closing.py); "fixed" is "I did what was asked"
    "other_fix_approved": "Something else was done, and a second person approved it",
    "not_remediable_approved": "Nothing could be done, and a second person approved it",
    "reported_to_sponsor": "A second person did not approve what was done, and reported it to the sponsor",
}
# set by a reply or by the alert's page, never given by an operator
NOT_TYPED = ("by_reply", "other_fix_approved", "not_remediable_approved", "reported_to_sponsor")
NEEDS_CODE = ("cleared", "suppressed")


class NoReasonCode(ValueError):
    """Clearing or disregarding an alert without a reason from the fixed list."""


def reason_problem(state: str, code: str | None, reason: str | None) -> str | None:
    if state not in NEEDS_CODE:
        return None
    if code not in REASON_CODES:
        return "give a reason code: " + ", ".join(k for k in REASON_CODES if k not in NOT_TYPED)
    if code == "other" and not (reason or "").strip():
        return 'reason code "other" needs the reason written out (--reason)'
    return None


def set_state(item_id: int, state: str, actor: str, actor_type: str = "operator", reason: str | None = None, *,
              study_id: str, code: str | None = None) -> None:
    item = _in_study(item_id, study_id, actor, f"state {state}")
    why = order_problem(item, state, actor_type)
    if why:
        db.audit("item_change_refused_order", entity_type="item", entity_id=item_id, study_id=item["study_id"],
                 actor=actor, actor_type=actor_type, old_state=item["state"], state=state, why=why)
        raise StepOrder(why)
    if bad := reason_problem(state, code, reason):
        raise NoReasonCode(bad)
    cleared = 1 if state == "cleared" else (0 if state == "open" else item["cleared"])
    db.ex("UPDATE items SET state=?, cleared=?, reason_code=?, updated_at=? WHERE id=? AND study_id=?",
          (state, cleared, code if state in NEEDS_CODE else None, db.now(), item_id, study_id))
    if state == "suppressed":
        db.ex("UPDATE outbox SET status='held', last_error='item suppressed' WHERE item_id=? AND status='queued'",
              (item_id,))
    db.audit(f"item_{state}", entity_type="item", entity_id=item_id, study_id=item["study_id"],
             actor=actor, actor_type=actor_type, old_state=item["state"], reason=reason, code=code,
             code_label=REASON_CODES.get(code))
    hindsight.save_alert(item_id, kind=item["kind"], participant_id=item["participant_id"])


def history(item_id: int) -> list[dict]:
    return [dict(r) for r in db.q(
        "SELECT * FROM audit_log WHERE entity_type='item' AND entity_id=? ORDER BY id", (str(item_id),))]

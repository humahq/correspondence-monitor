"""Patient memory across threads and years, kept in Hindsight beside the bot (issue #32, docs/hindsight-plan.md).

Each study message is sent to Hindsight while the normal check runs, never before it, so reading is not slowed.
Hindsight's AI pulls out the facts about participants and tags each one with the participant it is about; the
bot adds who said it, their role, when, and which message. A fact with no clear participant gets no participant
tag, so it is in nobody's record until a person links it. The bot's own alerts are saved too, so it knows what was
already raised and done about a problem. One bank per study, so studies never mix.

memory.py is different: the dates the rule checks count from. This module is the wider record (gout, a fall, a
medicine), for answers and for linking a new message to an old one.

A failure never stops a message being read: it is logged, and the admins are told while it lasts.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import Future, ThreadPoolExecutor

import requests

from . import db, memory, settings
from .drafting import local_time

MAX_TEXT = 20000
TIMEOUT = 180

MISSION = (
    "This is the memory of a monitoring bot for one clinical trial. Keep facts about the trial's participants: "
    "health events, symptoms, diagnoses, medicines and doses, dosing mistakes, lab and test results, visits done or "
    "missed, consent, and life changes that matter to the study (moving, travel, a new caregiver). Keep what the "
    "site or the bot did about a problem. Each fact says who reported it and their role, as the message gives them. "
    "When a message corrects itself (a typo, 'sorry, I meant 140/90'), keep only the corrected fact and do not "
    "mention the mistake at all. When it corrects an earlier message, keep the new fact and say it replaces what was said before. "
    "A question, a what-if or a plan is not a fact that happened: keep it only as '<who> asked whether ...' or "
    "'<who> plans to ...'. Ignore greetings, signatures, call scheduling and instructions to the bot. Never record "
    "a participant's name, only their study code."
)

LABELS = [{
    "key": "participant",
    "type": "multi-text",
    "tag": True,
    "description": (
        "The study code of each trial participant this fact is about, in capitals with only letters and digits "
        "(no spaces, dashes or words such as 'Subject'): 'subject 01-014' is 01014, 'pt 14' is 14. Only a code the "
        "message gives, or one it clearly means ('she', 'the patient' right after the code was named). Leave it out "
        "when the participant is not clear. Never a name, never a staff member."),
}]

BANK_CONFIG = {
    "retain_mission": MISSION,
    "retain_extraction_mode": "concise",
    "entity_labels": LABELS,
    "enable_observations": False,
}

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hindsight")
_pending: set[Future] = set()


def enabled() -> bool:
    return bool(settings.get("memory.enabled", False))


def bank(study_id: str) -> str:
    return f"{settings.get('memory.bank_prefix', '') or ''}{study_id}"


def participant_tag(pid: str | None) -> str | None:
    k = memory.key(pid)
    return f"participant:{k}" if k else None


def _key() -> str:
    path = settings.resolve(settings.get("memory.key_file", "secrets/hindsight_key"))
    return path.read_text().strip()


def _call(method: str, path: str, body: dict | None = None, timeout: float = TIMEOUT) -> dict:
    url = settings.get("memory.url", "http://127.0.0.1:8888").rstrip("/") + path
    r = requests.request(method, url, json=body, timeout=timeout,
                         headers={"Authorization": f"Bearer {_key()}"})
    r.raise_for_status()
    return r.json() if r.content else {}


def _ensure_bank(study_id: str) -> str:
    """The study's bank with our settings; sent again only when they change."""
    b = bank(study_id)
    digest = hashlib.sha256(json.dumps(BANK_CONFIG, sort_keys=True).encode()).hexdigest()[:16]
    if db.get_state(f"hindsight_bank:{b}") != digest:
        _call("PATCH", f"/v1/default/banks/{b}/config", {"updates": BANK_CONFIG})
        db.set_state(f"hindsight_bank:{b}", digest)
        db.audit("memory_bank_configured", entity_type="service", entity_id="hindsight", bank=b, config=digest)
    return b


def _speaker(study, msg: dict) -> tuple[str, str]:
    person = study.person_by_email(msg.get("from_addr"))
    name = (person.name if person else None) or msg.get("from_name") or msg.get("from_addr") or "unknown sender"
    role = ", ".join(study.role_label(r) for r in person.roles) if person and person.roles else "role not known"
    return name, role


def message_content(study, msg: dict, own_text: str) -> str:
    name, role = _speaker(study, msg)
    lines = [f"From: {name} ({role})", f"Sent: {local_time(msg.get('sent_at'))}",
             f"Channel: {msg.get('channel')}"]
    if msg.get("subject"):
        lines.append(f"Subject: {msg['subject']}")
    lines += ["", own_text.strip()]
    for a in msg.get("attachments") or []:
        if a.get("text"):
            lines += ["", f"[Attachment: {a.get('filename') or 'file'}]", a["text"]]
    return "\n".join(lines)[:MAX_TEXT]


def _failed(what: str, study_id: str, entity_id, error: Exception) -> None:
    from .compliance import alerts
    db.audit("memory_save_failed", entity_type=what, entity_id=entity_id, study_id=study_id, error=str(error)[:500])
    alerts.notify("hindsight", "cormon: saving to patient memory FAILED",
                  f"A {what} could not be saved to Hindsight: {str(error)[:300]}. Messages are still read and "
                  "checked as usual; only the memory is missing what failed. See `cormon log`.", every_minutes=360)


def _saved(what: str, study_id: str, entity_id, b: str, doc: str, res: dict) -> None:
    from .compliance import alerts
    db.audit("memory_saved", entity_type=what, entity_id=entity_id, study_id=study_id, bank=b, document=doc,
             usage=res.get("usage"))
    alerts.clear("hindsight", "cormon: patient memory saves work again", "Saving to Hindsight works again.")


def _save_message(study, msg: dict, own_text: str) -> None:
    try:
        name, role = _speaker(study, msg)
        b = _ensure_bank(study.study_id)
        doc = f"message-{msg['id']}"
        item = {"content": message_content(study, msg, own_text), "document_id": doc,
                "timestamp": msg.get("sent_at") or "unset",
                "context": f"a message in a clinical trial, from {name} ({role})",
                "metadata": {"message_id": str(msg["id"]), "channel": str(msg.get("channel")), "sender": name,
                             "role": role, "thread": str(msg.get("thread_key"))},
                "tags": [f"study:{study.study_id}", "source:message"]}
        res = _call("POST", f"/v1/default/banks/{b}/memories", {"items": [item]})
        _saved("message", study.study_id, msg["id"], b, doc, res)
    except Exception as e:
        _failed("message", study.study_id, msg.get("id"), e)


def save_message(study, msg: dict, own_text: str) -> Future | None:
    """Send one message to Hindsight in the background (a re-read replaces its earlier facts). Only messages from
    people on the study's list: what an unknown sender says goes into nobody's record."""
    if not enabled():
        return None
    if not study.person_by_email(msg.get("from_addr")):
        db.audit("memory_not_saved", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 why="the sender is not on the study's people list")
        return None
    return _submit(_save_message, study, msg, own_text)


def alert_content(item: dict) -> str:
    ev = item.get("evidence") or {}
    when, pid = local_time(item.get("created_at")), item["participant_id"]
    if item["kind"] == "out_of_scope":   # the facts themselves are saved from the message
        return (f"On {when} the monitoring bot sent a safety report about participant {pid} to the medical "
                f"monitor, unassessed (record #{item['id']}).")
    words = [f"On {when} the monitoring bot raised alert #{item['id']} about participant {pid}: {item['title']}."]
    if ev.get("next_step"):
        words.append(f"Next step it asked for: {ev['next_step']}")
    words.append(f"Alert status on {local_time(item.get('updated_at'))}: {item['state']}"
                 + (" (cleared)." if item.get("cleared") else "."))
    return " ".join(words)


def _save_alert(item_id: int) -> None:
    from . import items
    item = items.get(item_id)
    try:
        b = _ensure_bank(item["study_id"])
        doc = f"alert-{item_id}"
        tags = [t for t in (f"study:{item['study_id']}", "source:alert", participant_tag(item["participant_id"])) if t]
        res = _call("POST", f"/v1/default/banks/{b}/memories", {"items": [{
            "content": alert_content(item), "document_id": doc, "timestamp": item.get("updated_at") or "unset",
            "context": "the monitoring bot's own record of an alert it raised and what became of it",
            "metadata": {"alert_id": str(item_id), "message_id": str(item["first_message_id"]),
                         "state": item["state"]},
            "tags": tags}]})
        _saved("item", item["study_id"], item_id, b, doc, res)
    except Exception as e:
        _failed("item", item["study_id"], item_id, e)


def save_alert(item_id: int, *, kind: str | None = None, participant_id: str | None = None) -> Future | None:
    """Save (or update) the bot's record of an alert about a participant, in the background."""
    if not enabled() or kind not in ("finding", "out_of_scope") or not participant_tag(participant_id):
        return None
    return _submit(_save_alert, item_id)


def _submit(fn, *args) -> Future:
    fut = _pool.submit(fn, *args)
    _pending.add(fut)
    fut.add_done_callback(_pending.discard)
    return fut


def participant_tags(study_id: str, pid: str | None) -> list[str]:
    """Every participant tag in the study's bank that means this participant. Hindsight's AI writes the code as
    it reads it ('1001-022' one time, '1001022' the next) and tags cannot be edited, so the spellings are matched
    here with the same rule the bot matches codes with (memory.key)."""
    k = memory.key(pid)
    if not k:
        return []
    out, offset = [], 0
    while True:
        res = _call("GET", f"/v1/default/banks/{bank(study_id)}/tags?q=participant:*&limit=500&offset={offset}",
                    timeout=30)
        page = res.get("items") or []
        out += [t["tag"] for t in page if memory.key(t["tag"].split(":", 1)[1]) == k]
        offset += len(page)
        if not page or offset >= (res.get("total") or 0):
            return out


def facts(study_id: str, limit: int = 200) -> list[dict]:
    """The study's saved facts, newest first: text, participant tags, where each came from."""
    res = _call("GET", f"/v1/default/banks/{bank(study_id)}/memories/list?limit={limit}", timeout=60)
    return res.get("items") or res.get("memories") or []


def clear_test_bank(study_id: str) -> None:
    """Empty a test copy's bank, so one test case's facts do not carry into the next. Never a real bank."""
    b = bank(study_id)
    if not b.startswith("test-"):
        raise RuntimeError(f"{b} is not a test bank; refusing to empty it")
    flush()
    url = settings.get("memory.url", "http://127.0.0.1:8888").rstrip("/")
    r = requests.delete(f"{url}/v1/default/banks/{b}", timeout=60, headers={"Authorization": f"Bearer {_key()}"})
    if r.status_code != 404:
        r.raise_for_status()
    db.set_state(f"hindsight_bank:{b}", "")


def flush() -> None:
    """Wait for every save sent so far (tests, and before the service stops)."""
    for fut in list(_pending):
        fut.result()

"""Patient memory across threads and years, kept in Hindsight beside the bot (issue #32, docs/hindsight-plan.md).

Each study message is sent to Hindsight while the normal check runs, never before it, so reading is not slowed.
Hindsight's AI pulls out the facts about participants and tags each one with the participant it is about; the
bot adds who said it, their role, when, and which message. A fact with no clear participant gets no participant
tag, so it is in nobody's record until a person links it. The bot's own alerts are saved too, so it knows what was
already raised and done about a problem. One bank per study, so studies never mix.

memory.py is different: the dates the rule checks count from. This module is the wider record (gout, a fall, a
medicine): an answer about a participant is given what memory holds about them (recall_lines), with when and by
whom each fact was reported, so it can link a new message to one from another thread or a year ago.

A failure never stops a message being read: it is logged, and the admins are told while it lasts.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import quote

import requests

from . import db, llm, memory, settings
from .drafting import local_time

MAX_TEXT = 20000
MAX_FACTS = 12
TIMEOUT = 180

MISSION = (
    "This is the memory of a monitoring bot for one clinical trial. Keep facts about the trial's participants: "
    "health events, symptoms, diagnoses, medicines and doses, dosing mistakes, lab and test results, visits done or "
    "missed, consent, and life changes that matter to the study (moving, travel, a new caregiver). Keep what the "
    "site or the bot did about a problem. Each fact says who reported it and their role, as the message gives them. "
    "When a message corrects itself (a typo, 'sorry, I meant 140/90'), keep only the corrected fact and do not "
    "mention the mistake or write the wrong value at all. When it corrects an earlier message, keep the new fact and say it replaces what was said before. "
    "A question, a what-if or a plan is not a fact that happened: keep it only as '<who> asked whether ...' or "
    "'<who> plans to ...'. Keep nothing that is not about a participant: greetings, signatures, team meetings, "
    "monitoring visits, binders and logs, staff schedules, call scheduling and instructions to the bot. Never "
    "record a participant's name: write their study code, or 'a participant' when there is none. Write each fact "
    "in the language of the message, never mixing in words of another language."
)

LABELS = [{
    "key": "participant",
    "type": "multi-text",
    "tag": True,
    "description": (
        "The study code of each trial participant this fact is about, in capitals with only letters and digits "
        "(no spaces, dashes or words such as 'Subject'): 'subject 01-014' is 01014, 'pt 14' is 14, '1001-111' is "
        "1001111. Copy every digit exactly as written, repeated digits too. Only a code the "
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
    doc = f"message-{msg['id']}"
    if curated(study.study_id, doc):   # saving again replaces the document's facts, undoing the admin's change
        db.audit("memory_not_saved", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 why="an admin corrected, retired or linked its facts on the memory page")
        return
    try:
        name, role = _speaker(study, msg)
        b = _ensure_bank(study.study_id)
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
        return
    try:
        _check_tags(study.study_id, doc, item["content"], item["tags"])
    except Exception as e:
        db.audit("memory_tag_check_failed", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 document=doc, error=str(e)[:300])


def written_codes(text: str) -> set[str]:
    """Everything in a text that could be a participant code as written, by the bot's code rule (memory.key): each
    run of one to three letter-or-digit pieces joined by a dash, slash, dot or space, with a digit in every piece
    after the first ('1001-111', 'MAP 0301', 'Subj 1001-138')."""
    pieces = list(re.finditer(r"[A-Za-z0-9]+", text))
    out = set()
    for i in range(len(pieces)):
        for j in range(i, min(i + 3, len(pieces))):
            if j > i and not (re.fullmatch(r"[\s\-/#.]{1,2}", text[pieces[j - 1].end():pieces[j].start()])
                              and any(c.isdigit() for c in pieces[j].group())):
                break
            k = memory.key("".join(p.group() for p in pieces[i:j + 1]))
            if any(c.isdigit() for c in k):
                out.add(k)
    return out


def _one_off(a: str, b: str) -> bool:
    """One digit or letter added, dropped, changed, or two side by side swapped."""
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    short, long_ = sorted((a, b), key=len)
    return len(long_) - len(short) == 1 and any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _check_tags(study_id: str, doc: str, text: str, tags: list[str]) -> None:
    """Hindsight's AI can miscopy a code into a fact's participant tag ('1001-111' tagged 100111), which files the
    fact under nobody's record. Each tag is checked against the codes the message itself writes; one that is one
    character off exactly one of them gets the right code added, when the message's facts are all about that
    participant (a document's tags are changed all together; the AI's own wrong tag cannot be removed, so it stays
    beside the right one). Anything else is left and logged."""
    codes = written_codes(text)
    rows = [r for r in _every(study_id, f"&document_id={quote(doc, safe='')}") if r.get("state") != "invalidated"]
    fix, unsure = {}, set()
    for k in {memory.key(p) for r in rows for p in _pids(r)} - codes:
        near = [c for c in codes if len(c) >= 4 and _one_off(k, c)]
        if len(near) == 1:
            fix[k] = near[0]
        else:
            unsure.add(k)
    if not fix and not unsure:
        return
    who = {frozenset(fix.get(memory.key(p), memory.key(p)) for p in _pids(r)) for r in rows if _pids(r)}
    log = dict(entity_type="message", entity_id=doc.removeprefix("message-"), study_id=study_id, document=doc)
    if fix and not unsure and len(who) == 1:
        new = sorted(set(tags) | {f"participant:{k}" for k in next(iter(who))})
        _call("PATCH", f"/v1/default/banks/{bank(study_id)}/documents/{quote(doc, safe='')}", {"tags": new}, timeout=60)
        db.audit("memory_tag_fixed", fixed=fix, **log)
    else:
        db.audit("memory_tag_unsure", tags=sorted(unsure | set(fix)), likely=fix, **log)


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
    """Save (or update) the bot's record of an alert about a participant, in the background. Not for an alert on a
    message from someone off the study's people list: what they said goes into nobody's record, the alert too."""
    if not enabled() or kind not in ("finding", "out_of_scope") or not participant_tag(participant_id):
        return None
    from . import config
    row = db.q1("SELECT m.study_id, m.from_addr FROM items i JOIN messages m ON m.id = i.first_message_id "
                "WHERE i.id=?", (item_id,))
    if row and not config.study(row["study_id"]).person_by_email(row["from_addr"]):
        db.audit("memory_not_saved", entity_type="item", entity_id=item_id, study_id=row["study_id"],
                 why="the alert's message is from someone not on the study's people list")
        return None
    return _submit(_save_alert, item_id)


def _submit(fn, *args) -> Future:
    fut = _pool.submit(fn, *args)
    _pending.add(fut)
    fut.add_done_callback(_pending.discard)
    return fut


def _all_participant_tags(study_id: str, timeout: float) -> list[str]:
    every, offset = [], 0
    while True:
        try:
            res = _call("GET", f"/v1/default/banks/{bank(study_id)}/tags?q=participant:*&limit=500&offset={offset}",
                        timeout=timeout)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:   # nothing saved for the study yet
                return []
            raise
        page = res.get("items") or []
        every += [t["tag"] for t in page]
        offset += len(page)
        if not page or offset >= (res.get("total") or 0):
            return every


def participants(study_id: str, timeout: float = 5) -> dict[str, str]:
    """{code key: the code as memory wrote it} for every participant memory holds facts about; {} when memory is
    off or could not be read in time."""
    if not enabled():
        return {}
    try:
        return {memory.key(t.split(":", 1)[1]): t.split(":", 1)[1] for t in _all_participant_tags(study_id, timeout)}
    except Exception:
        return {}


def participant_tags(study_id: str, pid: str | None, timeout: float = 30) -> list[str]:
    """Every participant tag in the study's bank that means this participant. Hindsight's AI writes the code as
    it reads it ('1001-022' one time, '1001022' the next) and tags cannot be edited, so the spellings are matched
    here with the same rule the bot matches codes with (memory.key). A short form the site wrote ('212' for
    1001-212) counts when it fits no other participant (memory.same_person)."""
    if not memory.key(pid):
        return []
    every = _all_participant_tags(study_id, timeout)
    keys = {memory.key(t.split(":", 1)[1]) for t in every}
    mine = memory.same_person(pid, keys | memory.known_keys(study_id))
    return [t for t in every if memory.key(t.split(":", 1)[1]) in mine]


def _day(r: dict) -> str:
    return local_time(r.get("mentioned_at")).rsplit(" ", 1)[0]


def _parts(r: dict) -> tuple[str, str]:
    """A fact's own words and when it happened ('' when not known), from Hindsight's 'text | When: ...' form."""
    text = r["text"].split(" | ")
    when = next((p[5:].strip() for p in text[1:] if p.startswith("When:")), "")
    return text[0].strip(), (when[:10] if when[:4].isdigit() else when)


def _who(r: dict) -> str:
    meta = r.get("metadata") or {}
    return f"{meta['sender']} ({meta['role']})" if meta.get("sender") else "the monitoring bot"


def _fact_line(r: dict, replaced_by: dict | None = None, corrected: dict | None = None) -> str:
    text, when = _parts(r)
    happened = f" (happened: {when})" if when else ""
    kind = (f"NO LONGER TRUE (a later report, on {_day(replaced_by)}, changed it)" if replaced_by
            else "earlier fact")
    fixed = f" [corrected by {corrected['by']} on {_day({'mentioned_at': corrected['ts']})}]" if corrected else ""
    return f"{kind}, reported on {_day(r)} by {_who(r)}: {text}{happened}{fixed}"


NEWER_SYSTEM = """You read the facts a clinical-trial study monitor holds about one participant, numbered, oldest
report first. A later fact replaces an earlier one when both are about the same thing (a birth year, a diagnosis,
a dose, a date, whether a medicine is still taken, a result) and the later one gives a different value, says the
earlier one was wrong, or says it has stopped or ended. A fact that only adds detail, repeats it, or is about
something else replaces nothing. An event and what happened after it are both true and replace nothing: a visit
missed and later done on another day, a problem and what was done about it. List each replaced fact with the number of the fact that replaces it; list none
when nothing is replaced."""

NEWER_SCHEMA = llm.obj({"replaced": {"type": "array", "items": llm.obj({
    "old": {"type": "integer"}, "by": {"type": "integer"}})}})


def _newest_wins(rows: list[dict]) -> dict[int, int]:
    """{old fact: the later fact that replaces it} ("born 1967", later "born 1972"). Both are kept and shown; the
    AI says which facts are about the same thing, and code keeps a pair only when the replacing fact was reported
    later. The bot's own record of an alert replaces nothing: it is the bot's reading of what the site reported, not
    a new report. rows are oldest first."""
    if len(rows) < 2:
        return {}
    listed = "\n".join(f"{n}. reported {r.get('mentioned_at') or 'date unknown'}: {r['text'].split(' | ')[0]}"
                       for n, r in enumerate(rows, 1))
    pairs = llm.structured("memory_newer", NEWER_SYSTEM, listed, NEWER_SCHEMA, "newer")["replaced"]
    out = {}
    for p in pairs:
        old, by = p["old"] - 1, p["by"] - 1
        if 0 <= old < by < len(rows) and (rows[old].get("mentioned_at") or "") < (rows[by].get("mentioned_at") or "") \
                and not (rows[by].get("metadata") or {}).get("alert_id"):
            out.setdefault(old, by)
    for old in list(out):   # a chain (1967, then 1970, then 1972) points at the newest
        while out[old] in out:
            out[old] = out[out[old]]
    return out


def recall_lines(study, pid: str | None, query: str, msg: dict) -> list[str]:
    return recall(study, pid, query, msg)[0]


def recall(study, pid: str | None, query: str, msg: dict) -> tuple[list[str], set[str]]:
    """What memory holds about a participant that bears on a question, oldest first, one line per fact with when
    it was reported and by whom; and the ids of the messages a later report corrected. The message being answered
    is left out (its own words are read anyway). Only the participant's own facts: a fact tagged to nobody or to
    someone else never comes back. A slow or failed search gives nothing and is logged, so an answer is never held
    up by memory."""
    if not enabled() or not memory.key(pid):
        return [], set()
    seconds = float(settings.get("memory.search_seconds", 5))
    began = time.monotonic()
    try:
        tags = participant_tags(study.study_id, pid, timeout=seconds)
        found = _call("POST", f"/v1/default/banks/{bank(study.study_id)}/memories/recall", {
            "query": query, "tags": tags, "tags_match": "any_strict", "budget": "mid", "max_tokens": 1500},
            timeout=seconds) if tags else {}
    except Exception as e:
        db.audit("memory_search_failed", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 participant_id=pid, seconds=round(time.monotonic() - began, 2), error=str(e)[:300])
        return [], set()
    mine = str(msg.get("id"))
    rows = [r for r in found.get("results") or [] if (r.get("metadata") or {}).get("message_id") != mine]
    rows = sorted(rows[:MAX_FACTS], key=lambda r: r.get("mentioned_at") or "")
    try:
        newer = _newest_wins(rows)
    except Exception as e:   # without the check every fact is shown with its date, and the answer is told the later wins
        newer = {}
        db.audit("memory_newer_failed", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 error=str(e)[:300])
    db.audit("memory_searched", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
             participant_id=pid, facts=len(rows), seconds=round(time.monotonic() - began, 2),
             replaced=[{"old": rows[o].get("id"), "by": rows[b].get("id")} for o, b in newer.items()])
    fixed = _corrections(study.study_id, [r.get("id") for r in rows])
    corrected = {(rows[n].get("metadata") or {}).get("message_id") for n in newer} - {None}
    return ([_fact_line(r, rows[newer[n]] if n in newer else None, fixed.get(r.get("id"))) for n, r in enumerate(rows)],
            corrected)


# ---------------------------------------------------------------- the memory page (adminview.py)

CHANGES = ("memory_fact_corrected", "memory_fact_retired", "memory_fact_restored", "memory_fact_linked",
           "memory_fact_forgotten")


def _changes(study_id: str, ids: list) -> dict[str, list[dict]]:
    """The admin changes to each fact, oldest first, from the audit trail."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return {}
    out: dict[str, list[dict]] = {}
    for r in db.q(f"SELECT ts, actor, action, entity_id, details FROM audit_log WHERE entity_type='memory_fact'"
                  f" AND study_id=? AND action IN ({','.join('?' * len(CHANGES))})"
                  f" AND entity_id IN ({','.join('?' * len(ids))}) ORDER BY id", (study_id, *CHANGES, *ids)):
        out.setdefault(r["entity_id"], []).append(dict(db.unjs(r["details"], {}), ts=r["ts"], by=r["actor"],
                                                       action=r["action"]))
    return out


def _corrections(study_id: str, ids: list) -> dict[str, dict]:
    """The latest correction of each fact that has one: {fact id: {by, ts}}."""
    return {i: [c for c in cs if c["action"] == "memory_fact_corrected"][-1]
            for i, cs in _changes(study_id, ids).items() if any(c["action"] == "memory_fact_corrected" for c in cs)}


def curated(study_id: str, document_id: str) -> bool:
    """An admin has changed a fact from this document, so it must not be saved again."""
    return bool(db.q1("SELECT 1 FROM audit_log WHERE entity_type='memory_fact' AND study_id=? AND action IN"
                      f" ({','.join('?' * len(CHANGES))}) AND details LIKE ? LIMIT 1",
                      (study_id, *CHANGES, f'%"document": "{document_id}"%')))


def _every(study_id: str, extra: str = "") -> list[dict]:
    """Every fact in the study's bank, current and retired."""
    out = []
    for state in ("valid", "invalidated"):
        offset = 0
        while True:
            try:
                res = _call("GET", f"/v1/default/banks/{bank(study_id)}/memories/list?limit=500&offset={offset}"
                                   f"&state={state}{extra}", timeout=60)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:   # nothing saved for the study yet
                    return []
                raise
            page = res.get("items") or []
            out += page
            offset += len(page)
            if not page or offset >= (res.get("total") or 0):
                break
    return out


def _pids(r: dict) -> list[str]:
    return [t.split(":", 1)[1] for t in r.get("tags") or [] if t.startswith("participant:")]


def _fact(r: dict, changes: list[dict]) -> dict:
    text, when = _parts(r)
    meta = r.get("metadata") or {}
    return {"id": r["id"], "text": text, "happened": when, "reported": local_time(r.get("mentioned_at")),
            "reported_at": r.get("mentioned_at"), "who": _who(r), "message_id": meta.get("message_id"),
            "alert_id": meta.get("alert_id"), "document": r.get("document_id"),
            "participants": sorted({memory.key(p) for p in _pids(r)}), "retired": r.get("state") == "invalidated",
            "retired_why": r.get("invalidation_reason"),
            "changes": [{"when": local_time(c["ts"]), "by": c["by"], "what": c["action"].removeprefix("memory_fact_"),
                         "reason": c.get("reason"), "old": c.get("old"), "new": c.get("new")} for c in changes]}


def overview(study_id: str) -> dict:
    """The memory page's first view: each participant with how many facts memory holds, and the facts linked to
    nobody (no participant code was clear), for a person to link or leave."""
    rows = _every(study_id)
    changes = _changes(study_id, [r["id"] for r in rows])
    people: dict[str, dict] = {}
    for r in rows:
        for k in {memory.key(p) for p in _pids(r)}:
            p = people.setdefault(k, {"id": _pids(r)[0], "key": k, "facts": 0, "retired": 0, "last": ""})
            p["retired" if r.get("state") == "invalidated" else "facts"] += 1
            p["last"] = max(p["last"], r.get("mentioned_at") or "")
    unlinked = [_fact(r, changes.get(r["id"], [])) for r in rows if not _pids(r) and r.get("state") != "invalidated"]
    return {"participants": sorted(people.values(), key=lambda p: p["last"], reverse=True),
            "unlinked": sorted(unlinked, key=lambda f: f["reported_at"] or "", reverse=True)}


def participant_facts(study_id: str, pid: str) -> list[dict]:
    """Every fact about one participant, newest report first, current and retired, with its admin changes."""
    tags = participant_tags(study_id, pid)
    rows = [r for r in _every(study_id) if set(r.get("tags") or []) & set(tags)] if tags else []
    changes = _changes(study_id, [r["id"] for r in rows])
    return sorted((_fact(r, changes.get(r["id"], [])) for r in rows), key=lambda f: f["reported_at"] or "",
                  reverse=True)


def change_fact(study_id: str, fact_id: str, action: str, by: str, reason: str, text: str = "",
                participant: str = "") -> dict:
    """An admin's change to one fact: correct its words, retire it (left out of answers, kept), restore it, or link
    a fact saved without a participant to one. Each needs a reason and goes in the audit trail with the old and
    new words. Nothing is deleted. Linking saves a tagged copy (tags cannot be edited) and retires the original."""
    reason, text = " ".join((reason or "").split()), " ".join((text or "").split())
    if len(reason) < 5:
        raise ValueError("Please give a reason (a few words).")
    b = bank(study_id)
    old = _call("GET", f"/v1/default/banks/{b}/memories/{fact_id}", timeout=30)
    words, _ = _parts(old)
    log = dict(entity_type="memory_fact", entity_id=fact_id, study_id=study_id, actor=by, actor_type="person",
               reason=reason, document=old.get("document_id"), bank=b)
    if action == "correct":
        if not text or text == words:
            raise ValueError("Write the corrected fact.")
        _call("PATCH", f"/v1/default/banks/{b}/memories/{fact_id}", {"text": text}, timeout=60)
        db.audit("memory_fact_corrected", old=words, new=text, **log)
    elif action == "retire":
        _call("PATCH", f"/v1/default/banks/{b}/memories/{fact_id}",
              {"state": "invalidated", "reason": f"{reason} ({by})"}, timeout=60)
        db.audit("memory_fact_retired", old=words, **log)
    elif action == "restore":
        _call("PATCH", f"/v1/default/banks/{b}/memories/{fact_id}", {"state": "valid"}, timeout=60)
        db.audit("memory_fact_restored", old=words, **log)
    elif action == "link":
        tag = participant_tag(participant)
        if not tag:
            raise ValueError("Write the participant's study code.")
        if _pids(old):
            raise ValueError("This fact is already linked to a participant.")
        meta = dict(old.get("metadata") or {}, linked_from=fact_id, linked_by=by)
        doc = f"link-{fact_id}"
        _call("POST", f"/v1/default/banks/{_ensure_bank(study_id)}/memories", {"items": [{
            "content": f"A fact about participant {participant.strip()}: {words}", "document_id": doc,
            "timestamp": old.get("mentioned_at") or "unset",
            "context": f"a fact first saved without a participant code, linked by {by}",
            "metadata": {k: str(v) for k, v in meta.items()},
            "tags": [f"study:{study_id}", "source:link", tag]}]})
        _call("PATCH", f"/v1/default/banks/{b}/memories/{fact_id}",
              {"state": "invalidated", "reason": f"linked to {participant.strip()} ({by}): {reason}"}, timeout=60)
        db.audit("memory_fact_linked", old=words, participant_id=participant.strip(), new_document=doc, **log)
    else:
        raise ValueError(f"unknown change {action!r}")
    return {"ok": True}


# ---------------------------------------------------------------- retention, forgetting and records

def used() -> bool:
    """Memory is on, or was on once (its facts are still kept and still need retention and forgetting)."""
    return enabled() or bool(db.q1("SELECT 1 FROM audit_log WHERE action='memory_saved' LIMIT 1"))


def _documents_of(study_id: str, extra: str) -> set[str]:
    return {r["document_id"] for r in _every(study_id, extra) if r.get("document_id")}


def purge_before(study_id: str, cutoff: str) -> int:
    """Delete every message's facts reported before `cutoff` (the retention period), with the message's Hindsight
    record; returns how many messages. The audit trail keeps that they were deleted."""
    b = bank(study_id)
    docs = _documents_of(study_id, f"&time_field=mentioned_at&end_date={quote(cutoff)}")
    for d in sorted(docs):
        _call("DELETE", f"/v1/default/banks/{b}/documents/{quote(d, safe='')}", timeout=60)
    if docs:
        db.audit("memory_purged", entity_type="study", entity_id=study_id, study_id=study_id, before=cutoff,
                 documents=len(docs))
    return len(docs)


FORGOTTEN = "[removed: the participant's facts were deleted]"


def forget_participant(study_id: str, pid: str) -> int:
    """Erase what memory holds about one participant; returns how many facts. A message whose facts are all
    theirs is deleted from Hindsight outright; in a message about others too, each of their facts is blanked and
    retired, so the others' facts stay. Hindsight keeps no message text, so nothing else holds it. The bot does
    not save those messages again on a re-read (curated)."""
    tags = set(participant_tags(study_id, pid))
    if not tags:
        return 0
    b, rows = bank(study_id), _every(study_id)
    theirs = [r for r in rows if set(r.get("tags") or []) & tags]
    n = 0
    for doc in sorted({r.get("document_id") for r in theirs if r.get("document_id")}):
        mine = [r for r in theirs if r.get("document_id") == doc]
        others = [r for r in rows if r.get("document_id") == doc and r not in mine]
        if not others:
            _call("DELETE", f"/v1/default/banks/{b}/documents/{quote(doc, safe='')}", timeout=60)
        else:
            for r in mine:
                _call("PATCH", f"/v1/default/banks/{b}/memories/{r['id']}", {
                    "text": FORGOTTEN, "entities": [], "state": "invalidated",
                    "reason": "participant forgotten"}, timeout=60)
        n += len(mine)
        db.audit("memory_fact_forgotten", entity_type="memory_fact", entity_id=mine[0]["id"], study_id=study_id,
                 document=doc, facts=len(mine), deleted_document=not others, participant_id=memory.key(pid))
    return n


def delete_study(study_id: str) -> None:
    """Delete a study's whole memory (its Hindsight bank). Its saves and changes stay in the audit trail."""
    flush()
    b = bank(study_id)
    try:
        _call("DELETE", f"/v1/default/banks/{b}", timeout=120)
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
    db.set_state(f"hindsight_bank:{b}", "")
    db.audit("memory_study_deleted", entity_type="study", entity_id=study_id, study_id=study_id, bank=b)


def export_rows(study_id: str, since: str | None = None, until: str | None = None) -> list[dict]:
    """Every fact in the study's memory, current and retired, for the records package (oldest report first)."""
    out = []
    for r in _every(study_id):
        at = r.get("mentioned_at") or ""
        if (since and at < since) or (until and at[:10] > until):
            continue
        text, when = _parts(r)
        meta = r.get("metadata") or {}
        out.append({"fact": r["id"], "reported_at": at, "reported_by": _who(r), "participants": " ".join(_pids(r)),
                    "fact_text": text, "happened": when, "state": r.get("state"),
                    "retired_why": r.get("invalidation_reason"), "edited_at": r.get("edited_at"),
                    "document": r.get("document_id"), "message_id": meta.get("message_id"),
                    "alert_id": meta.get("alert_id")})
    return sorted(out, key=lambda x: x["reported_at"])


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

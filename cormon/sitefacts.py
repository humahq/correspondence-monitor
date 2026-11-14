"""Durable facts: what happened at a site and how a site works, kept apart from patient memory (features.durable_facts).

Patient memory (hindsight.py) keeps only facts about participants. A site message's other facts ("the lab fridge
door was left open overnight", "our courier picks up at 3 pm") are saved here, filed under the sender's own site the
way patient facts are (hindsight.site_of), and read back only inside the asker's sites (hindsight.reader_sites).
Never about a participant, never who did something, never a rule. People list, add, correct and retire them
(`cormon site-facts`); every change is in the audit trail.
"""

from __future__ import annotations

import re
from datetime import datetime

from dateutil import parser as dtparser

from . import db, hindsight, llm, memory, settings

KINDS = ("event", "how_it_works")
SHOWN = 30   # the newest facts an answer is given

SYSTEM = """You keep a clinical-trial site's durable facts, apart from facts about participants.
From the site's message, list only:
- event: something that happened at the site itself: equipment or a fridge or freezer failing or left open, a
  temperature excursion, a power or internet outage, the site closed or short-staffed, supplies, kits or drug
  shipments arriving late, damaged or missing.
- how_it_works: how this site does things: where its lab is, its courier and pick-up times, its opening hours, a
  local way of working.
Each fact is one short sentence in the message's own words, in the language of the message. happened: the ISO date
it happened when the message gives or clearly implies one (resolve "yesterday" or "Tuesday" from the message
date), else "".
Leave out, every time:
- anything about a particular participant (their health, medicines, visits, samples, consent): another memory
  keeps those;
- who did, caused or reported something, and any staff member's name or how anyone works or performs;
- questions, requests, plans and what-ifs; anything about the messages, the chat or the bot;
- what the protocol, a manual or a rule says, or whether something is allowed.
Most messages have no such fact: then return an empty list."""

SCHEMA = llm.obj({"facts": {"type": "array", "items": llm.obj({
    "text": {"type": "string"},
    "kind": {"type": "string", "enum": list(KINDS)},
    "happened": {"type": "string"}})}})


def on() -> bool:
    return settings.feature("durable_facts")


def _staff_named(study, text: str) -> str | None:
    """A study person's name in the fact: the fact would say who did something."""
    for p in study.people:
        for part in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", (p.name or "").split("(")[0]):
            if re.search(rf"\b{re.escape(part)}\b", text, re.I):
                return part
    return None


def _participant_named(study_id: str, text: str) -> bool:
    return bool(hindsight.written_codes(text) & memory.known_keys(study_id))


def _day(value: str | None) -> str | None:
    try:
        return dtparser.parse(value).date().isoformat() if value else None
    except (ValueError, OverflowError):
        return None


def _save(study, msg: dict, own_text: str, site: str) -> None:
    try:
        res = llm.structured("site_facts", SYSTEM, f"MESSAGE DATE: {msg.get('sent_at') or 'unknown'}\n\n"
                             f"MESSAGE:\n{own_text.strip()}", SCHEMA, "site_facts")
    except Exception as e:
        db.audit("site_fact_save_failed", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 error=str(e)[:500])
        return
    for r in db.q("SELECT id FROM site_facts WHERE message_id=? AND active=1 AND author='bot'", (msg["id"],)):
        db.ex("UPDATE site_facts SET active=0 WHERE id=?", (r["id"],))   # a re-read replaces its own facts
        db.audit("site_fact_retired", entity_type="site_fact", entity_id=r["id"], study_id=study.study_id,
                 actor="bot", why="the message was read again")
    for f in res["facts"][:10]:
        text = " ".join(f["text"].split())
        why = ("it names a staff member" if _staff_named(study, text) else
               "it names a participant" if _participant_named(study.study_id, text) else
               "" if text else "empty")
        if why:
            db.audit("site_fact_not_saved", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                     why=why)
            continue
        fid = db.ex("INSERT INTO site_facts (study_id, site, kind, text, happened, message_id, author, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", (study.study_id, site, f["kind"], text, _day(f["happened"]),
                                                    msg["id"], "bot", msg.get("sent_at") or db.now()))
        db.audit("site_fact_saved", entity_type="site_fact", entity_id=fid, study_id=study.study_id, site=site,
                 kind=f["kind"], text=text, message_id=msg["id"])


def save_message(study, msg: dict, own_text: str):
    """Save a site message's durable facts in the background. Only from people on the study's list, filed under
    their own site; nothing when the site is not clear."""
    if not on() or not own_text.strip():
        return None
    if not study.person_by_email(msg.get("from_addr")):
        return None
    site = hindsight.site_of(study, msg)
    if not site:
        db.audit("site_fact_not_saved", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 why="not clear which site the message is about")
        return None
    return hindsight._submit(_save, study, msg, own_text, site)


def _site_name(study, site: str | None) -> str:
    if not site:
        return "all sites"
    return next((s.get("name") or site for s in study.sites if s["id"] == site), site)


def line(study, row: dict) -> str:
    """One fact as the answer step reads it: dated, with its site, never who reported it."""
    day = datetime.fromisoformat(row["created_at"]).strftime("%d %b %Y").lstrip("0")
    happened = f" (happened: {row['happened']})" if row["happened"] else ""
    return f"site fact, {_site_name(study, row['site'])}, reported on {day}: {row['text']}{happened}"


def rows_for(study, sites: set[str]) -> list[dict]:
    if not sites:
        return [dict(r) for r in db.q("SELECT * FROM site_facts WHERE study_id=? AND active=1 AND site IS NULL"
                                      " ORDER BY created_at DESC LIMIT ?", (study.study_id, SHOWN))][::-1]
    marks = ",".join("?" * len(sites))
    return [dict(r) for r in db.q(
        f"SELECT * FROM site_facts WHERE study_id=? AND active=1 AND (site IS NULL OR site IN ({marks}))"
        " ORDER BY created_at DESC LIMIT ?", (study.study_id, *sorted(sites), SHOWN))][::-1]


def lines(study, msg: dict) -> list[str]:
    """The durable facts an answer may use: the asker's own sites' and the study's own."""
    if not on():
        return []
    return [line(study, r) for r in rows_for(study, hindsight.reader_sites(study, msg))]


def add(study, site: str | None, kind: str, text: str, who: str, happened: str | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if site and site not in {s["id"] for s in study.sites}:
        raise ValueError(f"{study.study_id} has no site {site}")
    fid = db.ex("INSERT INTO site_facts (study_id, site, kind, text, happened, author, created_at)"
                " VALUES (?,?,?,?,?,?,?)", (study.study_id, site, kind, text.strip(), _day(happened), who, db.now()))
    db.audit("site_fact_added", entity_type="site_fact", entity_id=fid, study_id=study.study_id, actor=who,
             actor_type="operator", site=site, kind=kind, text=text.strip())
    return fid


def _get(fact_id: int) -> dict:
    row = db.q1("SELECT * FROM site_facts WHERE id=?", (fact_id,))
    if not row or not row["active"]:
        raise ValueError(f"there is no current site fact {fact_id}")
    return dict(row)


def correct(fact_id: int, text: str, who: str) -> int:
    old = _get(fact_id)
    db.ex("UPDATE site_facts SET active=0 WHERE id=?", (fact_id,))
    fid = db.ex("INSERT INTO site_facts (study_id, site, kind, text, happened, message_id, author, replaces,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)", (old["study_id"], old["site"], old["kind"], text.strip(),
                                                          old["happened"], old["message_id"], who, fact_id,
                                                          old["created_at"]))
    db.audit("site_fact_corrected", entity_type="site_fact", entity_id=fid, study_id=old["study_id"], actor=who,
             actor_type="operator", old=old["text"], new=text.strip(), replaces=fact_id)
    return fid


def retire(fact_id: int, who: str) -> None:
    old = _get(fact_id)
    db.ex("UPDATE site_facts SET active=0 WHERE id=?", (fact_id,))
    db.audit("site_fact_retired", entity_type="site_fact", entity_id=fact_id, study_id=old["study_id"], actor=who,
             actor_type="operator", old=old["text"])

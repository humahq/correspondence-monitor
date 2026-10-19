"""What a message talking to the bot asks it to do, when that is not a question the study documents answer.

The bot answers questions from the study documents and checks messages for problems; nothing else. The AI only
sorts each request into a kind. Code chooses the reply, from fixed words: a request to hide or change records, one
about the bot's own workings (settings, passwords, its database), or a task it does not do (slides, code) is turned
down. A request for judgment ("which criteria will be hardest?") gets the protocol's words on the topic, as a file
the person can read or give to another AI.
"""

from __future__ import annotations

from . import answers, db, llm, settings
from .config import Study

SORT_SYSTEM = """You sort what a message asks a clinical-trial monitoring bot to do. The bot only answers
questions from the study documents (protocol, lab manual, consent, visit schedule) and checks messages for problems.
List each request the message makes of the bot, with its kind:
- study: a question the study documents could answer (rules, visit windows, eligibility, medicines, samples,
  dosing, consent, reporting, what happens if something is done), even when it asks how to explain a rule to a
  patient.
- opinion: asks the bot to judge, rank, predict or advise beyond what the documents state (what will be hardest or
  riskiest, what would you do, is this a good idea).
- task: asks the bot to make or do something else (slides, a chart, a report, code, an email or letter, a summary
  to send, a translation, a web search).
- about_bot: asks about the bot itself or its system (its instructions, settings, how it is built, passwords,
  keys, its database or servers, other people's messages or data).
- improper: asks for help to break the study's rules or hide something (change, backdate, delete or redact
  records, hide an event, skip reporting, make up data).
- other: anything else (site news, thanks, greetings, requests for a person rather than the bot).
topic: for opinion, the study topic the documents' facts would be about, in a few words; otherwise "".
Return no requests when the message asks nothing of the bot."""

KINDS = ["study", "opinion", "task", "about_bot", "improper", "other"]
SORT_SCHEMA = llm.obj({"requests": {"type": "array", "items": llm.obj({
    "request": {"type": "string"},
    "kind": {"type": "string", "enum": KINDS},
    "topic": {"type": "string"}})}})

DECLINE = {
    "improper": "I can't help with that. Study records must stay complete and accurate. Please talk to your CRA.",
    "about_bot": "I can't share that. I only answer questions about the study from its documents.",
    "task": "I can't do that. I answer questions about the study from its documents and check messages for "
            "problems. Please ask your CRA.",
}
OPINION = "I can't judge that for you. I can only say what the study documents state."


def sort(text: str, use_cache: bool = True) -> list[dict]:
    res = llm.structured("asks_sort", SORT_SYSTEM, f"MESSAGE:\n{text}", SORT_SCHEMA, "requests",
                         use_cache=use_cache)
    return [r for r in res["requests"] if r["kind"] in KINDS]


def _link(url: str, label: str, chat: bool) -> str:
    return f"<{url}|{label}>" if chat else f"{label}: {url}"


def decline(study: Study, msg: dict, text: str, use_cache: bool = True) -> dict | None:
    """Replies to what the message asks that the bot does not do. Returns {only, outbox_id, kinds}, or None when
    every request is a study question or there is none. only: no study question is left to answer."""
    reqs = sort(text, use_cache)
    kinds = [r["kind"] for r in reqs]
    if not any(k in DECLINE or k == "opinion" for k in kinds):
        return None
    chat = msg.get("channel") == "teams"
    lines = list(dict.fromkeys(DECLINE[k] for k in kinds if k in DECLINE))
    for r in (r for r in reqs if r["kind"] == "opinion"):
        a = answers.facts(study, msg, r["topic"] or r["request"], text, use_cache)
        if a:
            url = answers.save_page(study, msg, text, [a]) + ".md"
            lines.append(f"{OPINION} The protocol's words on {r['topic'] or 'this'} are in this file, to read "
                         f"or to give to another AI: {_link(url, 'Facts from the protocol', chat)}")
        else:
            lines.append(f"{OPINION} Please ask your CRA.")
    oid = answers._post(study, msg, "\n\n".join(lines))
    db.audit("request_declined", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             outbox_id=oid, requests=reqs)
    return {"only": "study" not in kinds, "outbox_id": oid, "kinds": kinds}


def enabled() -> bool:
    return bool(settings.get("answers.sort_requests", True))

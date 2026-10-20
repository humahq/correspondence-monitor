"""What a message talking to the bot asks it to do, when that is not a question the study documents answer.

The bot answers questions from the study documents and checks messages for problems; nothing else. The AI only
sorts each request into a kind. Code chooses the reply, from fixed words: a request to hide or change records, or
one about the bot's own workings (settings, passwords, its database), is turned down. A request for judgment
("which criteria will be hardest?") or a task the bot does not do (slides, code) gets the protocol's words on the
topic as an attached file, with a question the person can give to another AI. In a group chat no document facts
are sent (they would reach everyone in it): the bot offers them in a 1-on-1 chat instead. An email reply goes to
its sender only, so email is treated like a 1-on-1 chat.
"""

from __future__ import annotations

import re

from . import answers, conversation, db, llm, settings
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
topic: for opinion or task, the study topic the documents' facts would be about, in a few words. "" when the
request has nothing to do with the study (the weather, music, sport, general news), and for every other kind.
Return no requests when the message asks nothing of the bot."""
SORT_EARLIER = """
EARLIER IN THIS CONVERSATION is only there to understand MESSAGE: sort only what MESSAGE asks. A follow-up ("and
week 8?", "make slides of that") or a correction ("sorry, I meant 012") has the kind and topic of what it now asks,
read with the earlier messages. When MESSAGE gives what the bot asked for (a date, a participant, more context),
it repeats the person's own earlier request with that added (not the bot's question): sort it as that request."""

KINDS = ["study", "opinion", "task", "about_bot", "improper", "other"]
SORT_SCHEMA = llm.obj({"requests": {"type": "array", "items": llm.obj({
    "request": {"type": "string"},
    "kind": {"type": "string", "enum": KINDS},
    "topic": {"type": "string"}})}})

DECLINE = {
    "improper": "I can't help with that. Study records must stay complete and accurate. Please talk to your CRA.",
    "about_bot": "I can't share that. I only answer questions about the study from its documents.",
}
LEAD = {  # what the bot does not do itself, but can give the documents' facts for, to use with another AI
    "opinion": "I can't judge that for you. It needs an opinion, or knowledge I don't have, such as about your site, "
               "your team or your patients. I only know what the study documents say.",
    "task": "I can't do that myself. I answer questions about the study from its documents and check messages "
            "for problems.",
}
OPINION = LEAD["opinion"]
ONE_ON_ONE = "Message me 1-on-1 and I can send you what the study documents say on it."
GROUP = {  # a group chat: no document facts, so nothing from the documents reaches everyone in it
    "opinion": "I can't judge that here. It needs an opinion, or knowledge I don't have, such as about your site, "
               f"your team or your patients. {ONE_ON_ONE}",
    "task": f"I can't do that here. I answer questions about the study from its documents. {ONE_ON_ONE}",
}


def private(msg: dict) -> bool:
    """Only the person who asked sees the reply: a 1-on-1 chat, or an email (the reply goes to its sender only)."""
    return msg.get("channel") != "teams" or msg.get("visibility") == "direct"


def sort(text: str, use_cache: bool = True, earlier: list[dict] | None = None) -> list[dict]:
    prompt = (f"EARLIER IN THIS CONVERSATION:\n{conversation.block(earlier)}\n\n" if earlier else "") + \
        f"MESSAGE:\n{text}"
    res = llm.structured("asks_sort", SORT_SYSTEM + (SORT_EARLIER if earlier else ""), prompt, SORT_SCHEMA,
                         "requests", use_cache=use_cache)
    return [r for r in res["requests"] if r["kind"] in KINDS]


def _link(url: str, label: str, chat: bool) -> str:
    return f"<{url}|{label}>" if chat else f"{label}: {url}"


def decline(study: Study, msg: dict, text: str, use_cache: bool = True) -> dict | None:
    """Replies to what the message asks that the bot does not do. Returns {only, outbox_id, kinds}, or None when
    every request is a study question or there is none. only: no study question is left to answer."""
    reqs = sort(text, use_cache, conversation.recent(msg) if msg.get("id") else None)
    kinds = [r["kind"] for r in reqs]
    if not any(k in DECLINE or k in LEAD for k in kinds):
        return None
    chat = msg.get("channel") == "teams"
    lines = list(dict.fromkeys(DECLINE[k] for k in kinds if k in DECLINE))
    files = []
    for r in (r for r in reqs if r["kind"] in LEAD):
        topic = r["topic"].strip()
        if not topic:
            lines.append(f"{LEAD[r['kind']]} The study documents don't cover that.")
            continue
        if not private(msg):
            lines.append(GROUP[r["kind"]])
            continue
        a = answers.facts(study, msg, topic, text, use_cache)
        if not a:
            lines.append(f"{LEAD[r['kind']]} The study documents don't cover {topic}, so please ask your CRA.")
            continue
        url = answers.save_page(study, msg, text, [a]) + ".md"
        files.append({"name": f"facts-{re.sub(r'[^a-z0-9]+', '-', topic.lower()).strip('-')[:40] or 'study'}.md",
                      "text": answers.facts_file({"message_text": text, "answers": db.js([a])}, study.title)})
        lines.append(f"{LEAD[r['kind']]} If you'd like to use another AI, the study documents' facts on {topic} "
                     f"are in the attached file, with a question to give it "
                     f"(also here: {_link(url, 'Facts from the protocol', chat)}).")
    oid = answers._post(study, msg, "\n\n".join(lines), files)
    db.audit("request_declined", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             outbox_id=oid, requests=reqs)
    return {"only": "study" not in kinds, "outbox_id": oid, "kinds": kinds}


def enabled() -> bool:
    return bool(settings.get("answers.sort_requests", True))

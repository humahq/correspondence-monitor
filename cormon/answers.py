"""Answers to questions a site asks the monitor about the study ("what is the window for Week 8?", "when is
G-708-002's next visit?"), posted in the same chat thread or as a reply to the email.

An answer is shown only with the protocol's own words behind it, found in the documents; with none found the
monitor says so instead of guessing. A participant's visit windows are worked out by code from the dates earlier
messages gave (see memory.py), not by the AI. Safety questions are not judged.
"""

from __future__ import annotations

import re
from datetime import date

from dateutil.relativedelta import relativedelta

from . import db, guide, llm, memory, reader, routing, settings
from .config import Study

ASKED = re.compile(r"\?|^\s*(?:what|when|how|which|where|why|who|is|are|can|could|should|do|does|did|may|must|"
                   r"would|will)\b", re.I | re.M)
MAX_QUESTIONS = 3
FOOTER = "From the study documents. Please confirm with your CRA before acting on it."

FIND_SYSTEM = """You read a message a clinical-trial site sent to the study monitor and list the questions in it
that the study documents (protocol, lab manual, consent, visit schedule) or the participant's visit dates could
answer: visit windows and timing, when a visit or sample is due, what a visit includes, eligibility limits, dosing
rules, allowed and forbidden medicines, sample handling, consent steps.
Leave out questions for a person rather than the documents (asking for kits, a call, a reply, whether an email
arrived), rhetorical questions, and questions the message itself already answers.
Reword each question to stand alone, keeping every number, date and visit name. participant: the participant id
it is about, or "" when it is general. Return no questions when there are none."""

FIND_SCHEMA = llm.obj({"questions": {"type": "array", "items": llm.obj({
    "question": {"type": "string"},
    "participant": {"type": "string"}})}})

PICK_SYSTEM = """You pick the parts of a study guide needed to answer a site's question: the sections and visit
checklists whose rules decide it (visit windows, the schedule of activities, the procedures, limits or steps it
asks about). At most 8 ids, the most decisive first. Use only ids from the index."""

PICK_SCHEMA = llm.obj({"ids": {"type": "array", "items": {"type": "string"}}})

ANSWER_SYSTEM = """You are a clinical-trial monitor answering a site's question from the study documents only.
Rules:
- Answer only from the PROTOCOL SECTIONS and the PARTICIPANT RECORD given. Never from general knowledge.
- answerable: false when the sections do not settle the question; then say in answer what is missing.
- answer: two to four short sentences in plain English. Lead with the direct answer (a number, a date range,
  yes or no). For a participant, use the visit windows worked out by code in the record; do not redo that
  arithmetic. Do no arithmetic of your own either (adding up weeks, gaps between visits, target dates): give the
  protocol's own numbers and the windows worked out by code. If the protocol does not state the answer directly
  but its numbers bear on it (each phone visit's day in the schedule, for a question about the gap between
  them), answerable is true: say the protocol does not state it directly and list those numbers. Say which earlier message a date came from when you use one. No quotation, section ids or brackets in
  the answer: the protocol's words go in the quotes field and are shown under the answer.
- quotes: the protocol's own words that decide the answer, copied exactly (one or two sentences, or one cell of
  the schedule table), each with the id of the section it is in. Never quote a topic brief or a checklist written
  for the guide; quote the numbered section or the schedule table itself.
- safety: true when the question is about an adverse event, a death, a hospital stay or other harm to a
  participant. The monitor does not judge safety: say only that safety reporting goes to the medical monitor and
  the PI, and quote the protocol's reporting rule if it is in the sections.
- If the record shows the participant has died or withdrawn, do not give future visit dates."""

ANSWER_SCHEMA = llm.obj({
    "answerable": {"type": "boolean"},
    "safety": {"type": "boolean"},
    "answer": {"type": "string"},
    "quotes": {"type": "array", "items": llm.obj({"section_id": {"type": "string"}, "words": {"type": "string"}})}})


def asks_something(text: str) -> bool:
    return bool(ASKED.search(text or ""))


def find(text: str, use_cache: bool = True) -> list[dict]:
    res = llm.structured("answer_find", FIND_SYSTEM, f"MESSAGE:\n{text}", FIND_SCHEMA, "questions",
                         use_cache=use_cache)
    return [q for q in res["questions"] if q["question"].strip()][:MAX_QUESTIONS]


def _day(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def schedule(past: list[dict], visits: list[dict]) -> list[str]:
    """Each visit's window in calendar dates, counted by code from the participant's anchor dates, and the date a
    visit was reported done when an earlier message gave it."""
    anchors: dict[str, tuple[date, str]] = {}
    done: dict[str, tuple[date, str]] = {}
    for h in past:
        for d in h["dates"]:
            day = _day(d.get("date"))
            if not day:
                continue
            k = memory.anchor_kind(d)
            if k in ("baseline", "extension_start", "last_dose"):
                anchors[k] = (day, h["when"])
            if d.get("visit_id"):
                done[d["visit_id"]] = (day, h["when"])
    lines = []
    for v in visits:
        w = reader.window(v)
        if not w or w[0] not in anchors:
            continue
        start, when = anchors[w[0]]
        lo, hi = start + relativedelta(days=w[1]), start + relativedelta(days=w[2])
        line = (f"{v['title']}: Day {w[1]} to Day {w[2]} from {w[0].replace('_', ' ')} ({start:%d %b %Y}, said in "
                f"the message of {when}) = {lo:%a %d %b %Y} to {hi:%a %d %b %Y}")
        if v["id"] in done:
            line += f"; reported on {done[v['id']][0]:%d %b %Y} (message of {done[v['id']][1]})"
        lines.append(line)
    return lines


def _short(visit: dict) -> str:
    """A visit's name as its schedule table column starts: "Week 12" for "Study Week in ATE: Week 12 = ..."."""
    return re.sub(r"\s*=.*$", "", re.sub(r"^.*\bATE:\s*", "", visit["title"])).strip()


def schedule_cell(visit: dict, sources: list[dict]) -> tuple[dict, str] | None:
    """The schedule table's own column heading for a visit ("Week 8 [12] / Clinic / Day 56 ± 7"), from the table
    the visit checklist was made from: the protocol's words for the visit's window."""
    m = re.search(r"From: (.+?)\.?\s*$", visit["text"].splitlines()[0])
    want = reader._nums(re.search(r"window ([^.]*)", visit["text"]).group(1)) if "window " in visit["text"] else set()
    if not m or not want:
        return None
    label = re.compile(rf"^{re.escape(_short(visit))}(?!\d)", re.I)
    for s in sources:
        head = next((ln for ln in s["text"].splitlines() if f"Schedule table ({m.group(1)})" in ln), None)
        if not head:
            continue
        for cell in re.split(r"\s+\|\s+", head.split("Columns:", 1)[-1]):
            cell = cell.strip()
            if label.search(cell) and want <= reader._nums(cell):
                return s, cell
    return None


def _clean(answer: str) -> str:
    """The answer's own sentences: quoted protocol lines and section ids are shown under it instead."""
    lines = [ln for ln in answer.splitlines() if not re.match(r'^\s*["“]', ln)]
    return re.sub(r"\s*\[[\w.:-]+\]", "", " ".join(ln.strip() for ln in lines if ln.strip())).strip()


def _numbers_traced(answer: str, backing: str) -> bool:
    """Every number in the answer appears in the question, quotes or record (dates and day counts the AI did not
    make up). Leading zeros do not matter."""
    norm = lambda s: {str(int(float(x))) if x.replace(".", "", 1).isdigit() else x
                      for x in re.findall(r"\d+(?:\.\d+)?", s)}
    return norm(answer) <= norm(backing)


def answer_one(study: Study, msg: dict, q: dict, items: list[dict], sources: list[dict], email: str,
               use_cache: bool = True) -> dict:
    """One question's answer: {question, participant, text, quotes: [(heading, words)], found, safety}."""
    pid = q["participant"].strip()
    past = memory.history(study.study_id, pid, msg, include_current=True) if pid else []
    visits = [i for i in items if i["kind"] == "visit"]
    record = memory.record_lines(past)
    sched = schedule(past, visits)
    # the numbered sections themselves, not topic briefs: a brief cites dozens of sections, too many to quote from
    pickable = sources + visits
    known = {i["id"]: i for i in pickable}
    picked_ids = llm.structured("answer_pick", PICK_SYSTEM, "GUIDE INDEX (id | heading | what it covers):\n"
                                + reader.menu(pickable) + f"\n\nQUESTION: {q['question']}", PICK_SCHEMA, "pick",
                                use_cache=use_cache)["ids"]
    picked = [known[x] for x in dict.fromkeys(picked_ids) if x in known][:8]
    first_doc = (guide.documents(study) or [""])[0] if study else ""
    secs = sorted((i for i in picked if i["kind"] == "section"), key=lambda i: i["doc_id"] != first_doc)
    checklists = [i for i in picked if i["kind"] == "visit"]
    user = ("PROTOCOL SECTIONS (when two documents give the same rule, quote the first one listed):\n\n"
            + "\n\n".join(reader._block(i) for i in secs)
            + "\n\nVISIT CHECKLISTS (the guide's summary of the schedule table: use them, never quote them):\n\n"
            + ("\n\n".join(reader._block(i) for i in checklists) or "none")
            + f"\n\nMESSAGE DATE: {reader._email_date(msg):%A %d %B %Y}"
            + f"\n\nPARTICIPANT RECORD ({pid or 'no participant named'}):\n"
            + ("\n".join(f"- {x}" for x in record) or "- no dates on record")
            + "\n\nVISIT WINDOWS WORKED OUT BY CODE:\n" + ("\n".join(f"- {x}" for x in sched) or "- none")
            + f"\n\nTHE SITE'S MESSAGE:\n{email}\n\nQUESTION TO ANSWER: {q['question']}")
    res = llm.structured("answer_question", ANSWER_SYSTEM, user, ANSWER_SCHEMA, "answer", use_cache=use_cache)
    text = _clean(res["answer"])
    proven, windows = [], set()
    for v in checklists:  # a visit the question names, or whose day the answer gives: its schedule table window
        cell = schedule_cell(v, sources)
        day = re.search(r"Day\s*(\d+)", cell[1]) if cell else None
        asked = re.sub(r"\b(?:wk|week)\s*(\d+)", r"Week \1", q["question"], flags=re.I)
        named = re.search(rf"\b{re.escape(_short(v))}(?!\d)", asked, re.I)
        if cell and (named or (day and re.search(rf"\bDay\s*{day.group(1)}\b", text))):
            window = re.sub(r"\s+", "", cell[1].rsplit("/", 1)[-1])
            if window not in windows:  # the same window in both periods' tables is shown once
                windows.add(window)
                proven.append((reader._heading(cell[0]), cell[1]))
    about = {"problem": q["question"], "why": text}
    for c in res["quotes"][:3]:
        hit = reader._exact_words(c["words"], secs, sources, c["section_id"], about)
        if hit and hit[0]["kind"] != "section":  # a visit checklist is the guide's own summary, not the protocol
            hit = None
        n = reader._nums(hit[1]) if hit else set()
        if hit and not any(w == hit[1] or (n and n <= reader._nums(w)) for _, w in proven):  # one rule, two documents
            proven.append((reader._heading(hit[0]), re.sub(r"^[\s.,;:]+", "", hit[1])))
    backing = " ".join([q["question"], email] + [w for _, w in proven] + record + sched
                       + [i["text"].splitlines()[0] for i in checklists])
    # an answer that says the protocol is silent but gives its proven numbers is still shown
    return {"question": q["question"], "participant": pid, "text": text, "quotes": proven[:4],
            "found": bool(proven), "safety": res["safety"],
            "traced": _numbers_traced(text, backing), "picked": [i["id"] for i in secs + checklists]}


def _shown(words: str, limit: int = 320) -> str:
    """A long quote cut at a sentence end, so the chat stays readable; the words are still the protocol's."""
    if len(words) <= limit:
        return words
    cut = max(words.rfind(". ", 0, limit), words.rfind(" ... ", 0, limit))
    return (words[:cut + 1] if cut > 80 else words[:limit].rsplit(" ", 1)[0]).rstrip(" .") + " ..."


def render(answers: list[dict]) -> str:
    blocks = []
    for a in answers:
        head = f"Q: {a['question']}"
        if a["safety"] and not a["quotes"]:
            body = ("Safety questions are not checked by this monitor. Please report it to the medical monitor and "
                    "the PI as the protocol says.")
        elif not a["found"]:
            body = "I could not find this in the study documents, so I won't guess. Please ask your CRA."
        else:
            words = "\n".join(f'Protocol, {h}: "{_shown(w)}"' for h, w in a["quotes"])
            # an answer whose numbers are not all in the documents or the record: the words, not a guess
            body = (f"{a['text']}\n{words}" if a["traced"] else
                    f"I could not work out a sure answer. These are the protocol's words on it:\n{words}")
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks) + f"\n\n{FOOTER}"


def _post(study: Study, msg: dict, text: str) -> int | None:
    if msg.get("channel") == "teams":
        m = re.fullmatch(r"teams:((spaces/[^/]+)/threads/[^/]+)", msg.get("thread_key") or "")
        if not m:
            return None
        return routing.enqueue(purpose="question_answer", channel="teams", destination="teams_study_channel",
                               body=text, message_id=msg["id"], study_id=study.study_id,
                               meta={"target": {"space": m.group(2), "thread": m.group(1), "key": "study_channel"}})
    sender = msg.get("from_addr")
    if not sender or sender.lower() == (settings.get("mailbox.address") or "").lower():
        return None
    subject = msg.get("subject") or "your question"
    meta = {"thread_key": msg["thread_key"], "in_reply_to": msg.get("rfc_message_id"),
            "references": (msg.get("refs") or []) + ([msg["rfc_message_id"]] if msg.get("rfc_message_id") else []),
            "gmail_thread": msg["thread_key"].split(":", 1)[1]}
    return routing.enqueue(purpose="question_answer", channel="email", destination="email", body=text,
                           subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
                           message_id=msg["id"], from_addr=routing.from_alias(study, msg), to=[sender], meta=meta,
                           study_id=study.study_id)


def reply(study: Study, msg: dict, email: str, use_cache: bool = True) -> dict | None:
    """Answer the message's questions, if it asks any the documents could answer. Returns a trace, or None."""
    if not guide.checker(study).get("answer_questions", True) or not asks_something(email):
        return None
    qs = find(email, use_cache)
    if not qs:
        return None
    every = guide.load(study.study_id)
    items = reader.view(study, every)
    sources = [i for i in every if i["kind"] == "section"]
    answers = [answer_one(study, msg, q, items, sources, email, use_cache) for q in qs]
    text = render(answers)
    oid = _post(study, msg, text)
    db.audit("question_answered", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             outbox_id=oid, answers=answers)
    return {"answers": answers, "outbox_id": oid, "text": text}

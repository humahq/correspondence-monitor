"""Answers to questions a site asks the monitor about the study ("what is the window for Week 8?", "when is
G-708-002's next visit?"), posted in the same chat thread or as a reply to the email.

An answer is shown only with the protocol's own words behind it, found in the documents; with none found the
monitor says so instead of guessing. A participant's visit windows and any sum are worked out by code, from the
dates earlier messages gave (see memory.py) and the protocol's numbers. A message reporting serious harm always
starts with the reminder to contact the PI and the Medical Monitor; the medical side is not judged.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import secrets
from collections import Counter
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta

from . import (capture, careful, conversation, db, guide, hindsight, language, llm, memory, outcheck, privacy, reader, routing,
               settings)
from .compliance import sha256
from .config import Study

ASKED = re.compile(r"\?|^\W*(?:(?:hey|hi|hello|ok|okay)\s+)?(?:@?huma\s+)?(?:@?bot\b\W*)?"
                   r"(?:what|when|how|which|where|why|who|is|are|can|could|should|do|does|did|may|must|would|will|"
                   r"tell|list|explain|describe|summari[sz]e)\b", re.I | re.M)
MAX_QUESTIONS = 3
FOOTER = "From the study documents. Please confirm with your CRA before acting on it."
FACTS_CHARS = 40000  # whole protocol sections in a facts file, so another AI can be pasted all of it

FIND_SYSTEM = """You read a message a clinical-trial site sent to the study monitor and list the questions in it
that the study documents (protocol, lab manual, consent, visit schedule) or the participant's visit dates could
answer: anything on how the study is run, for example visit windows and timing, what a visit includes,
eligibility, dosing, medicines, samples, consent, deviations, adverse event reporting, records and data entry,
monitoring, scales and scores, withdrawal and early termination. Also list questions on what earlier messages
reported about a participant (what happened to them, their medicines, allergies, results, events, visits,
consent, who reported something and when): record_only is true for those, when they ask nothing about a rule.
Leave out questions for a person rather than the documents (asking for kits, a call, a reply, whether an email
arrived, who is covering), rhetorical questions, and questions the message itself already answers.
Reword each question to stand alone, keeping every number, date and visit name and what the message reports that
bears on it. participant: the participant id it is about, or "" when it is general. harm: true when the message
reports a death, a hospital admission, a life-threatening event, a disability, a birth defect or other serious
harm to a participant (or their baby) that happened, whatever the question is about; a reason for a remote
visit or a condition at enrolment is not harm. harm and harm_participant are also set for the whole message.
Return no questions when there are none."""
FIND_EARLIER = """
EARLIER IN THIS CONVERSATION is only there to understand MESSAGE. List only what MESSAGE asks. When
MESSAGE follows up on an earlier message ("and week 8?", "what about 014?") or corrects one ("sorry, I meant
012", "no, the visit was on the 12th"), write the question it now asks in full from the earlier messages, with
MESSAGE winning where they disagree. When MESSAGE gives what the bot asked for (a date, a participant, more
context), list the person's own earlier question again (never the bot's question back to them), in full, with what
MESSAGE gives. Any other question asked earlier and
not asked again in MESSAGE is not listed. harm is only about what MESSAGE reports."""

FIND_SCHEMA = llm.obj({"questions": {"type": "array", "items": llm.obj({
    "question": {"type": "string"},
    "participant": {"type": "string"},
    "record_only": {"type": "boolean"},
    "harm": {"type": "boolean"}})},
    "harm": {"type": "boolean"}, "harm_participant": {"type": "string"}})

HARM_WORDS = re.compile(r"\b(?:died|death|dead|passed away|deceased|hospitali[sz]|admitted|admission|\bE[DR]\b|"
                        r"emergency|life[- ]threatening|anaphyla|intubat|ICU|surgery|fracture|stroke|seizure|"
                        r"congenital|birth defect|disabilit|suicid|overdose)", re.I)
REPORTING_QUESTION = "This message reports serious harm to a participant: how must it be reported under the protocol?"

PICK_SYSTEM = """You pick the parts of a study guide needed to answer a site's question: the sections and visit
checklists whose rules decide it, for the question and for what the message reports (an event that happened can
bring its own rules: reporting, deviations, exceptions), and for what the participant record says that bears on
the question (a medicine taken or stopped, an event, an unblinding). Include sections with exceptions to the rule asked about.
At most 10 ids, the most decisive first. Use only ids from the index."""

PICK_SCHEMA = llm.obj({"ids": {"type": "array", "items": {"type": "string"}}})

ANSWER_SYSTEM = """You are a clinical-trial monitor answering a site's question from the study documents only.
Rules:
- Answer only from the PROTOCOL SECTIONS and the PARTICIPANT RECORD given. Never from general knowledge.
- answerable: false when the sections do not settle the question; then say in answer what is missing.
- answer: two to four short sentences in plain English. Lead with the direct answer (a number, a date range,
  yes or no). For a participant, use the visit windows worked out by code in the record; do not redo that
  arithmetic. A sum of your own (a gap between visits, a total, a difference) goes in calculations, written with
  the protocol's or the record's own numbers ("84 - 14"), or for days between two dates with the dates as
  YYYY-MM-DD ("2026-06-22 - 2026-06-01"), or for a date some days after another as the date and the days
  ("2026-09-01 + 28"); code works it out and checks each number, and you may give its result
  in the answer. In the answer itself write dates as "14 Sep 2026". If the protocol does not state the answer directly but its numbers settle it,
  answerable is true: say it is not stated directly and give the numbers and the calculation. Check exceptions:
  a specific exception in the sections overrides the general rule, and the schedule's own visit days override a
  general statement about visit frequency. Each visit checklist says what its window counts from. Say which earlier message a date came from
  when you use one. No quotation, section ids or brackets in the answer: the protocol's words go in the quotes
  field and are shown under the answer.
- quotes: the protocol's own words that decide the answer, copied exactly (one or two sentences, or one cell of
  the schedule table), each with the id of the section it is in. Never quote a topic brief or a checklist written
  for the guide; quote the numbered section or the schedule table itself.
- serious_event: true when the message reports an event that meets or may meet the protocol's definition of a
  serious adverse event (death, hospital admission, life-threatening, disability, birth defect, important medical
  event). Then put first in quotes the protocol's rule on reporting it (to whom and how fast) and say it in the
  answer, then answer the question itself. Do not judge the medical side (cause, relatedness, treatment).
- If the record or message shows the participant has died or withdrawn, give no future visit dates.
- An "earlier fact" in the record was reported in another message, perhaps another thread or long ago. Use one
  only when it bears on the question; one about the very thing asked (the same medicine, dose, consent, visit or
  result, the reason a visit was missed, what was done the last time a problem happened) always bears on it and
  must be used. The answer must then say who reported it and on what date, in the
  sentence that uses it ("The study coordinator reported on 12 Oct 2026 that she has gout."), so the reader can
  check it. When two earlier facts disagree, the one reported later holds; mention the earlier one only if the
  question needs it. A fact marked NO LONGER TRUE was changed by a later report: it is false now; never use it,
  or a date marked as from a corrected message, as true. Answer from the fact that replaced it, and mention the
  old one only when the question asks what changed or when the change matters, saying it was later corrected.
- record_used: each PARTICIPANT RECORD line the answer rests on, copied exactly. A question only on what
  happened to the participant (is the nausea still ongoing, any allergies, who reported the seizure) is answered
  from the record: answerable is true when the record settles it; when the record says nothing on it, answerable
  is false and the answer says no earlier message reports it."""

ANSWER_SCHEMA = llm.obj({
    "answerable": {"type": "boolean"},
    "record_used": {"type": "array", "items": {"type": "string"}},
    "serious_event": {"type": "boolean"},
    "answer": {"type": "string"},
    "calculations": {"type": "array", "items": llm.obj({"what": {"type": "string"},
                                                       "expression": {"type": "string"}})},
    "quotes": {"type": "array", "items": llm.obj({"section_id": {"type": "string"}, "words": {"type": "string"}})}})

PLAIN_SYSTEM = """You turn a study monitor's checked answer into the reply a helpful human assistant would post in a
clinical-trial team chat. The reader is busy site or study staff, often a nurse or a doctor.
Rules:
- Plain English, at most four short sentences. Lead with the direct answer (a number, a date range, yes or no).
- Rewrite, do not summarise: keep every fact the checked answer gives in reply to the question (numbers, windows
  such as "± 3 days", definitions and their thresholds, counts, who does or reviews what, conditions, exceptions).
- Remove only how it was worked out: sums, "worked out by code", section numbers, quotation marks, "the protocol
  does not state this directly", where a date came from. Keep who reported an earlier fact about the
  participant and when ("the coordinator reported on 12 Oct that she has gout"): that is part of the answer.
- Keep the participant's own calendar dates when the checked answer gives them; do not swap them for day numbers.
- Use only facts in the CHECKED ANSWER, the PROTOCOL'S WORDS and the earlier messages' facts. Never add a
  number, a date or a rule of your own.
- When a serious event must be reported, say to whom and how fast, taken from the protocol's words.
- No greeting and no sign-off."""

PLAIN_SCHEMA = llm.obj({"text": {"type": "string"}})

ASK_SYSTEM = """A clinical-trial study monitor bot could not settle a site's question from the protocol with what it
knows. Decide whether one or two facts that the person who asked can give would let the protocol settle it: a date
(baseline, a visit, a dose, an event), which participant, visit, sample or medicine they mean, or what exactly
happened. If so, write ask: one short, friendly question (ending with "?") asking for just those facts, saying in a few words what
it is for ("To tell if the Week 12 visit was in window, I need ..."). Ask only for what is missing from the
question, the message and the participant record.
ask is "" when the protocol sections don't cover the topic at all, when the answer needs a medical, safety or site
judgment rather than a fact, or when nothing the person could add would settle it. Whether to change, hold or stop
a dose or treatment, how severe an event is, and whether it is related to the study drug are the investigator's
and medical monitor's judgment: never ask about them.
Never ask for a name, a date of birth, contact details or other personal details; a participant id is enough."""
ASK_SCHEMA = llm.obj({"ask": {"type": "string"}})

SAE_TITLE = re.compile(r"serious adverse|\bSAEs?\b|reportable event|reporting of (?:serious )?adverse", re.I)
SERIOUS_LINE = "This may be a Serious Adverse Event. Contact the PI and the Medical Monitor now."


def asks_something(text: str) -> bool:
    return bool(ASKED.search(text or ""))


ASKS_DOCS = ("The message asks the clinical-trial monitoring bot a question, or asks it for information, that the "
             "study documents (protocol, lab manual, consent, visit schedule) could answer: rules, visit windows, "
             "eligibility, medicines, samples, dosing, consent, reporting, what to do next. It counts even with no "
             "question mark or when worded as a need or request (\"I need ...\", \"send me ...\", \"not sure if ...\"). "
             "It does not count when the message only reports what happened, thanks or greets, asks the bot to "
             "make or change something (slides, an email, a record), asks its opinion, or asks about something "
             "outside the study.")


def asks_the_documents(text: str, use_cache: bool = True) -> bool:
    """For a message talking to the bot: whether it asks something the documents could answer, worded any way
    ("I need the baseline list"). The word check first; a yes/no from the Decisions API when it finds nothing
    (on 60 test messages: 59 right, against 34 for the words alone). A failed call falls back to the words."""
    if asks_something(text):
        return True
    try:
        return llm.decide("asks_docs", text or "", [{"type": "predicate", "name": "asks", "instructions": ASKS_DOCS}],
                          use_cache=use_cache)["asks"] >= 0.5
    except Exception as e:
        db.audit("question_check_failed", entity_type="llm", entity_id="asks_docs", error=str(e)[:300])
        return False


def find(text: str, use_cache: bool = True, earlier: list[dict] | None = None) -> list[dict]:
    """The message's questions for the documents. A message reporting serious harm always gets one: how to report
    it, if none of its own questions is about the harm. earlier: the conversation just before (follow-ups)."""
    prompt = (f"EARLIER IN THIS CONVERSATION:\n{conversation.block(earlier)}\n\n" if earlier else "") + \
        f"MESSAGE:\n{text}"
    res = llm.structured("answer_find", FIND_SYSTEM + (FIND_EARLIER if earlier else ""), prompt, FIND_SCHEMA,
                         "questions", use_cache=use_cache)
    qs = [q for q in res["questions"] if q["question"].strip()][:MAX_QUESTIONS]
    if res["harm"] and not any(q["harm"] for q in qs):
        qs = [{"question": REPORTING_QUESTION, "participant": res["harm_participant"], "harm": True}] + qs
    return qs


def _day(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
STATED_ANCHOR = re.compile(
    r"\b(baseline|(?:open[- ]label )?extension start|start of (?:the )?(?:open[- ]label )?extension|last dose)\b"
    r"[^.?!;]{0,25}?\b(\d{4}-\d{2}-\d{2}|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"(?:,?\s+\d{4})?|"
    + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)", re.I)


def stated_anchors(question: str, today: date) -> list[dict]:
    """Anchor dates the question itself gives ("given a baseline on 4 July"), as a record entry, so code counts the
    windows from them. A date without a year is the latest one not after the message date."""
    from dateutil import parser as dateparser
    dates = []
    for what, when in STATED_ANCHOR.findall(question):
        try:
            day = dateparser.parse(when, default=datetime(today.year, 1, 1)).date()
        except (ValueError, OverflowError):
            continue
        if not re.search(r"\d{4}", when) and day > today:
            day = day.replace(year=day.year - 1)
        kind = "baseline" if what.lower() == "baseline" else "last_dose" if "dose" in what.lower() else \
            "extension_start"
        dates.append({"kind": kind, "label": f"{what} (as given in the question)", "date": day.isoformat()})
    return [{"message_id": None, "when": "this conversation", "dates": dates, "events": []}] if dates else []


def schedule(past: list[dict], visits: list[dict], asking: int | None = None) -> list[str]:
    """Each visit's window in calendar dates, counted by code from the participant's anchor dates, and the date a
    visit was reported done when an earlier message gave it. asking: the message being answered, whose visit dates
    may only be asked about ("can wk 12 be on 3 Jul?"), so they are never shown as done."""
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
            if d.get("visit_id") and (asking is None or h["message_id"] != asking):
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


def _norm_nums(s: str) -> set[str]:
    return {str(int(float(x))) if float(x).is_integer() else x for x in re.findall(r"\d+(?:\.\d+)?", s)}


def _numbers_traced(answer: str, backing: str) -> bool:
    """Every number in the answer appears in the question, quotes or record, or was worked out by code (dates and
    day counts the AI did not make up). Leading zeros do not matter."""
    return _norm_nums(answer) <= _norm_nums(backing)


FACT_LINE = re.compile(r"^(?:earlier fact|NO LONGER TRUE)[^:]*?, reported on (\d{1,2}) (\w{3}) (\d{4}) by (.+?): (.*)$")
PLAIN_WORDS = {"participant", "reported", "started", "taking", "stopped", "screening", "protocol", "medicine",
               "medication", "monitoring", "coordinator", "research", "associate", "because", "before", "after",
               "during", "visit", "study", "daily", "every", "today", "yesterday", "earlier", "should", "would",
               "about", "their", "there", "which", "other"}


def _unsourced(answer: str, record: list[str], asked: str) -> list[str]:
    """Earlier facts from memory the answer uses (it has a telling word of the fact the question did not) without
    the date the fact was reported: the reader cannot check them."""
    words = lambda s: set(re.findall(r"[a-z][a-z'-]{5,}", s.lower())) - PLAIN_WORDS  # noqa: E731
    said, low = words(answer), answer.lower()
    out = []
    for line in record:
        m = FACT_LINE.match(line)
        if not m:
            continue
        day, mon, year, who, fact = m.groups()
        used = sorted((words(fact.split(" (happened:")[0]) - words(asked)) & said)
        dated = re.search(rf"\b0?{int(day)}\s+{mon}|{mon}\w*\s+0?{int(day)}\b", answer, re.I) or \
            f"{year}-{datetime.strptime(mon, '%b').month:02d}-{int(day):02d}" in low
        if used and not dated:
            out.append(f"'{used[0]}' (reported on {day} {mon} {year} by {who})")
    return out


_ARITH = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        return _ARITH[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval(node.operand)
    raise ValueError("not plain arithmetic")


EXTREME_GAP = re.compile(r"\b(longest|maximum|max|most|shortest|minimum|least)\b.{0,40}\b(?:gap|days? between|"
                         r"time between|apart)", re.I)
INLINE_SUM = re.compile(r"(\d+(?:\.\d+)?(?:\s*[-+*/×−]\s*\d+(?:\.\d+)?)+)\s*=\s*(\d+(?:\.\d+)?)")


def _given_date(iso: str, backing: str) -> date | None:
    """The date, when the backing gives it: as 2026-09-01, or as "1 Sep", "01 Sept 2026", "September 1"."""
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return None
    mon = f"{d:%b}"
    written = rf"\b0?{d.day}(?:st|nd|rd|th)?\s+{mon}[a-z]*\.?(?!\w)|\b{mon}[a-z]*\.?\s+0?{d.day}(?!\d)"
    return d if iso in backing or re.search(written, backing, re.I) else None


def calculate(expression: str, backing: str) -> str | None:
    """A sum the AI asked for, worked out by code ("84 - 14 = 70"), when it is plain arithmetic on numbers found in
    the protocol's words or the record; days between two dates the record gives ("2026-06-22 - 2026-06-01"); or a
    date the record gives plus or minus a number of days the backing gives ("2026-09-01 + 28"). None otherwise."""
    expr = expression.replace("−", "-").replace("×", "*").replace("÷", "/").strip()
    expr = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", " * ", expr.split("=", 1)[0]).strip()   # code gives the result itself
    if m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*-\s*(\d{4}-\d{2}-\d{2})", expr):
        later, earlier = (_given_date(d, backing) for d in m.groups())
        return f"{expr} = {(later - earlier).days} days" if later and earlier else None
    if m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*([+-])\s*(\d+)(?:\s*days?)?", expr, re.I):
        day, sign, n = m.groups()
        start = _given_date(day, backing)
        if not start or n not in _norm_nums(backing):
            return None
        return f"{day} {sign} {n} days = {start + timedelta(days=int(n) * (1 if sign == '+' else -1)):%Y-%m-%d}"
    if not re.fullmatch(r"[\d\s+\-*/().]+", expr) or not _norm_nums(expr) <= _norm_nums(backing):
        return None
    try:
        value = _eval(ast.parse(expr, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError):
        return None
    value = round(value, 2)
    return f"{expr} = {int(value) if float(value).is_integer() else value}"


def lexical(text: str, sources: list[dict], k: int = 6) -> list[dict]:
    """The sections whose full text shares the question's rarer words: the heading index alone misses an exception
    buried in a section ("not reported as protocol deviations" for a language barrier)."""
    stems = lambda s: {w[:6] for w in reader._words(s) if len(w) > 3}
    per = [(s, stems(f"{s['title']} {s['text']}")) for s in sources]
    df = Counter(w for _, ws in per for w in ws)
    want = stems(text)
    scored = sorted(((sum(math.log(len(per) / df[w]) for w in want & ws), s) for s, ws in per),
                    key=lambda x: -x[0])
    return [s for score, s in scored[:k] if score > 0]


VISIT_TYPES = {"phone": r"phone|telephone|call", "clinic": r"clinic|in[- ]person|on[- ]site"}


def named_visits(question: str, visits: list[dict]) -> list[dict]:
    """The visit checklists a question names ("wk 8", "ATE Week 12", "Baseline") or whose type it asks about
    ("phone visits"), from the period it is about."""
    asked = re.sub(r"\b(?:wk|week)\s*(\d+)", r"Week \1", question, flags=re.I)
    ate = bool(re.search(r"\bATE\b|extension|open[- ]label", asked, re.I))
    period = [v for v in visits if ("ATE" in v["title"]) == ate]
    named = [v for v in period
             if re.search(rf"\b{re.escape(re.split(r' or ', _short(v))[0])}(?!\d)", asked, re.I)]
    if named:
        return named
    kinds = [k for k, pat in VISIT_TYPES.items() if re.search(rf"\b(?:{pat})s?\b", asked, re.I)]
    return [v for v in period if any(f"({k.title()})" in v["text"].splitlines()[0] for k in kinds)]


def _checklist_block(v: dict) -> str:
    w = reader.window(v)
    counted = f"\n(The window counts from {w[0].replace('_', ' ')}.)" if w else ""
    return reader._block(v) + counted


def answer_one(study: Study, msg: dict, q: dict, items: list[dict], sources: list[dict], email: str,
               use_cache: bool = True) -> dict:
    """One question's answer: {question, participant, text, quotes: [(heading, words)], found, serious, ...}."""
    pid = q["participant"].strip()
    past = memory.history(study.study_id, pid, msg, include_current=True) if pid else []
    known_anchors = {memory.anchor_kind(d) for h in past for d in h["dates"]}
    past += [dict(h, dates=[d for d in h["dates"] if d["kind"] not in known_anchors])
             for h in stated_anchors(q["question"], reader._email_date(msg).date())]
    visits = [i for i in items if i["kind"] == "visit"]
    facts, corrected = hindsight.recall(study, pid, q["question"], msg)
    # the message's own dates are read in the message: in the record they would look like what already happened
    record = memory.record_lines([h for h in past if h["message_id"] != msg.get("id")], corrected) + facts
    sched = schedule(past, visits, msg.get("id"))
    # the numbered sections themselves, not topic briefs: a brief cites dozens of sections, too many to quote from
    pickable = sources + visits
    known = {i["id"]: i for i in pickable}
    picked_ids = llm.structured("answer_pick", PICK_SYSTEM, "GUIDE INDEX (id | heading | what it covers):\n"
                                + reader.menu(pickable) + f"\n\nTHE SITE'S MESSAGE:\n{email}"
                                + ("\n\nPARTICIPANT RECORD:\n" + "\n".join(f"- {x}" for x in record) if record else "")
                                + f"\n\nQUESTION: {q['question']}", PICK_SCHEMA, "pick", use_cache=use_cache)["ids"]
    picked = [known[x] for x in dict.fromkeys(picked_ids) if x in known][:10]
    usable = [s for s in sources if reader._usable(s)]
    picked += lexical(f"{q['question']} {email}", usable)
    if q.get("harm"):  # the protocol's serious adverse event definition and reporting rules, whatever was asked
        picked += [s for s in usable if SAE_TITLE.search(s["title"])][:3]
    picked += named_visits(q["question"], visits)
    picked = list({i["id"]: i for i in picked}.values())
    first_doc = (guide.documents(study) or [""])[0] if study else ""
    secs = sorted((i for i in picked if i["kind"] == "section"), key=lambda i: i["doc_id"] != first_doc)
    checklists = [i for i in picked if i["kind"] == "visit"]
    user = ("PROTOCOL SECTIONS (when two documents give the same rule, quote the first one listed):\n\n"
            + "\n\n".join(reader._block(i) for i in secs)
            + "\n\nVISIT CHECKLISTS (the guide's summary of the schedule table: use them, never quote them):\n\n"
            + ("\n\n".join(_checklist_block(i) for i in checklists) or "none")
            + f"\n\nMESSAGE DATE: {reader._email_date(msg):%A %d %B %Y}"
            + f"\n\nPARTICIPANT RECORD ({pid or 'no participant named'}):\n"
            + ("\n".join(f"- {x}" for x in record) or "- no dates on record")
            + "\n\nVISIT WINDOWS WORKED OUT BY CODE:\n" + ("\n".join(f"- {x}" for x in sched) or "- none")
            + f"\n\nTHE SITE'S MESSAGE:\n{email}\n\nQUESTION TO ANSWER: {q['question']}")
    res = llm.structured("answer_question", ANSWER_SYSTEM, user, ANSWER_SCHEMA, "answer", use_cache=use_cache)
    given = sched + [f"message date {reader._email_date(msg):%d %B %Y}"]   # numbers the AI was given
    found = _proof(res, q, secs, checklists, sources, visits, email, record, given)
    unsourced = _unsourced(found["text"], record, f"{q['question']} {email}")
    if (not found["traced"] or unsourced) and (found["proven"] or found["from_record"]):
        loose = sorted(_norm_nums(found["text"]) - _norm_nums(found["backing"]), key=len)
        why = found["conflict"] or (f"These numbers in your answer are not in the protocol's words or the record: "
                                    f"{', '.join(loose)}. Give each one as a calculation from the protocol's or "
                                    f"the record's numbers, or leave it out." if loose else "")
        if unsourced:
            why += (" Your answer uses earlier facts without saying who reported them and when: "
                    + "; ".join(unsourced) + ". Say it in the sentence that uses each one.")
        fix = f"\n\nYOUR ANSWER:\n{json.dumps(res, ensure_ascii=False)}\n\n{why.strip()} Answer again in full."
        res2 = llm.structured("answer_question", ANSWER_SYSTEM, user + fix, ANSWER_SCHEMA, "answer",
                              use_cache=use_cache)
        again = _proof(res2, q, secs, checklists, sources, visits, email, record, given)
        if again["traced"] and again["proven"] and (not found["traced"] or len(
                _unsourced(again["text"], record, f"{q['question']} {email}")) < len(unsourced)):
            res, found = res2, again
        elif found["conflict"]:  # the AI keeps the wrong pair: code's own answer from the quoted rows
            found = dict(found, text=found["code_answer"], traced=True, sums=found["sums"][-1:])
    ask = "" if res["answerable"] else ask_back(q, found["text"], secs, email, record, use_cache)
    # an answer that says the protocol is silent but gives its proven numbers is still shown; one on what happened
    # to the participant stands on the record lines it rests on
    from_record = found["from_record"] if pid else []
    return {"question": q["question"], "participant": pid, "text": found["text"], "quotes": found["proven"][:4],
            "ask": ask, "from_record": from_record,
            "sums": found["sums"], "serious": bool(res["serious_event"]),
            "found": bool(found["proven"] or (q.get("record_only") and res["answerable"] and from_record)),
            "traced": found["traced"], "picked": [i["id"] for i in secs + checklists],
            "read": [reader._heading(i) for i in secs + checklists], "record": record, "windows": sched,
            "chosen": [x for x in dict.fromkeys(picked_ids) if x in known and known[x]["kind"] == "section"]}


PERSONAL = re.compile(r"\b(?:(?:participant|patient|subject|their|his|her|full|first|last)'?s? name|date of birth|"
                      r"birth ?date|DOB|home address|phone number|e-?mail address|contact details|initials|MRN|"
                      r"medical record number|social security|NHS number)\b", re.I)


def ask_back(q: dict, said: str, secs: list[dict], email: str, record: list[str], use_cache: bool = True) -> str:
    """A question back to the person for the fact the documents need to settle it; "" when none would help.
    Kept only when it is a short question, asks nothing personal and adds no number of its own."""
    try:
        ask = llm.structured("answer_ask", ASK_SYSTEM, f"QUESTION: {q['question']}\n\nTHE SITE'S MESSAGE:\n{email}"
                             f"\n\nPARTICIPANT RECORD:\n" + ("\n".join(f"- {x}" for x in record) or "- none")
                             + "\n\nPROTOCOL SECTIONS READ: " + "; ".join(reader._heading(s) for s in secs)
                             + f"\n\nWHAT THE BOT FOUND:\n{said}", ASK_SCHEMA, "ask", use_cache=use_cache)["ask"]
    except Exception:
        return ""
    ask = " ".join(ask.split())
    backing = " ".join([q["question"], email, said] + record + [s["text"] for s in secs])
    if len(ask) < 15 or len(ask) > 300 or PERSONAL.search(ask) or not _numbers_traced(ask, backing):
        return ""
    return ask if ask.endswith("?") else ask.rstrip(".") + ". Can you tell me?"


def record_used(cited: list[str], record: list[str]) -> list[str]:
    """The record lines an answer cited that really are in the participant record, whole, in record order: the
    proof for an answer on what happened, as protocol quotes are for a rule. A cited part of a line counts when it
    is long enough to tell lines apart."""
    flat = lambda s: " ".join(s.lower().split()).strip(" -.")  # noqa: E731
    want = [flat(c) for c in cited if len(flat(c)) >= 20]
    return [ln for ln in record if any(w in flat(ln) for w in want)][:6]


def _proof(res: dict, q: dict, secs: list[dict], checklists: list[dict], sources: list[dict], visits: list[dict],
           email: str, record: list[str], sched: list[str]) -> dict:
    """An answer's proven protocol words, the sums code worked out for it, and whether its numbers are traced."""
    text = _clean(res["answer"])
    proven, windows = [], set()
    for v in checklists:  # a visit the question names, or whose day the answer gives: its schedule table window
        cell = schedule_cell(v, sources)
        day = re.search(r"Day\s*(\d+)", cell[1]) if cell else None
        if cell and (v in named_visits(q["question"], visits)
                     or (day and re.search(rf"\bDay\s*{day.group(1)}\b", text))):
            window = re.sub(r"\s+", "", cell[1].rsplit("/", 1)[-1])
            if window not in windows:  # the same window in both periods' tables is shown once
                windows.add(window)
                proven.append((reader._heading(cell[0]), cell[1]))
    about = {"problem": q["question"], "why": text}
    for c in res["quotes"][:4]:
        hit = reader._exact_words(c["words"], secs, sources, c["section_id"], about)
        if hit and hit[0]["kind"] != "section":  # a visit checklist is the guide's own summary, not the protocol
            hit = None
        n = reader._nums(hit[1]) if hit else set()
        if hit and not any(w == hit[1] or (n and n <= reader._nums(w)) for _, w in proven):  # one rule, two documents
            proven.append((reader._heading(hit[0]), re.sub(r"^[\s.,;:]+", "", hit[1])))
    backing = " ".join([q["question"], email] + [w for _, w in proven] + record + sched
                       + [i["text"].splitlines()[0] for i in checklists])
    backing += " " + " ".join(f"{int(a) - int(b)} {int(a) + int(b)}"  # the two ends of every "Day 84 ± 3" window
                              for a, b in re.findall(r"(\d+)\s*(?:days?\s*)?±\s*(\d+)", backing))
    sums = [(c["what"], r) for c in res["calculations"][:4] if (r := calculate(c["expression"], backing))]
    for lhs, rhs in INLINE_SUM.findall(re.sub(r"\bDays?\s+(?=\d)", "", text)):  # a sum written in the answer itself counts only if code agrees
        r = calculate(lhs, backing)
        if r and _norm_nums(r.rsplit("=", 1)[1]) == _norm_nums(rhs) and r not in (s for _, s in sums):
            sums.append(("sum in the answer", r))
    conflict = code_answer = ""
    if extreme := EXTREME_GAP.search(q["question"]):  # "longest gap": code picks the pair, not just checks the sum
        days = sorted({int(d) for _, w in proven for d in re.findall(r"^[^/]*/[^/]*/\s*Day\s*(\d+)\b", w)})
        gaps = [(b - a, a, b) for a, b in zip(days, days[1:])]
        if len(gaps) > 1:
            g, a, b = (min if extreme.group(1).lower() in ("shortest", "minimum", "least") else max)(gaps)
            sums = [s for s in sums if s[1] != f"{b} - {a} = {g}"] + [(f"the {extreme.group(1).lower()} gap", f"{b} - {a} = {g}")]
            first = re.search(r"\d+", text)
            code_answer = (f"{g} days. In the schedule rows below, the {extreme.group(1).lower()} gap is from "
                           f"Day {a} to Day {b}.")
            if not first or int(first.group()) != g:
                conflict = f"Code finds the {extreme.group(1).lower()} gap is Day {a} to Day {b}: {g} days."
    backing += " " + " ".join(r for _, r in sums)
    return {"text": text, "proven": proven, "sums": sums, "backing": backing,
            "from_record": record_used(res.get("record_used") or [], record),
            "traced": _numbers_traced(text, backing) and not conflict, "conflict": conflict,
            "code_answer": code_answer}


def _shown(words: str, limit: int = 320) -> str:
    """A long quote cut at a sentence end, so the chat stays readable; the words are still the protocol's."""
    if len(words) <= limit:
        return words
    cut = max(words.rfind(". ", 0, limit), words.rfind(" ... ", 0, limit))
    return (words[:cut + 1] if cut > 80 else words[:limit].rsplit(" ", 1)[0]).rstrip(" .") + " ..."


NOT_FOUND = "I couldn't find this in the study documents, so I won't guess. Please ask your CRA."
NOT_SURE = "I couldn't work out a sure answer. The protocol's words on it are in the details."


def plain(a: dict, reader_role: str, use_cache: bool = True) -> str:
    """The checked answer as a human assistant would say it in chat: short, no workings. Every number in it must
    be in the checked answer, its sums, its quotes or the question; otherwise the checked answer is used."""
    ask = a.get("ask")
    if not a["found"]:
        return ask or NOT_FOUND
    if not a["traced"]:
        return f"{NOT_SURE} {ask}" if ask else NOT_SURE
    if ask:
        return f"{_plain_said(a, reader_role, use_cache)}\n\n{ask}"
    return _plain_said(a, reader_role, use_cache)


def _plain_said(a: dict, reader_role: str, use_cache: bool) -> str:
    """a["tone"] (a careful reader, see careful.py) adds that tone's wording rule to PLAIN_SYSTEM."""
    facts = "\n".join([a["text"]] + [f"{what}: {r}" for what, r in a.get("sums", [])])
    words = "\n".join(f"- {w}" for _, w in a["quotes"]) or "- none"
    earlier = a.get("from_record") or []
    rule = careful.tone(a.get("tone"))
    try:
        said = _clean(llm.structured("answer_plain", PLAIN_SYSTEM + (f"\n{rule}" if rule else ""),
                                     f"READER: {reader_role}\n\nQUESTION: "
                                     f"{a['question']}\n\nCHECKED ANSWER:\n{facts}\n\nPROTOCOL'S WORDS:\n{words}"
                                     + ("\n\nEARLIER MESSAGES' FACTS IT RESTS ON:\n"
                                        + "\n".join(f"- {x}" for x in earlier) if earlier else "")
                                     + ("\n\nThis is a serious event." if a.get("serious") else ""),
                                     PLAIN_SCHEMA, "reply", use_cache=use_cache)["text"])
    except Exception:
        return a["text"]
    backing = " ".join([facts, a["question"]] + [w for _, w in a["quotes"]] + earlier)
    lead = re.split(r"(?<=[.!?])\s", a["text"], maxsplit=1)[0]  # the direct answer: its numbers must all stay
    ok = said and _numbers_traced(said, backing) and _norm_nums(lead) <= _norm_nums(said)
    if ok and (added := outcheck.added_judgment(said, backing)):
        a.setdefault("output_check", []).extend(added)
        ok = False
    return said if ok else a["text"]


def cra_names(study: Study) -> str | None:
    """The study's CRA by name ("John Ernest", "A or B"), for the reply's last line; None for none or more than two."""
    names = list(dict.fromkeys(p.name for p in study.people_with_role("cra") if p.name))
    return " or ".join(names) if 0 < len(names) <= 2 else None


def footer(cra: str | None = None) -> str:
    return FOOTER.replace("your CRA", f"your CRA, {cra},") if cra else FOOTER


def render(answers: list[dict], link: str | None = None, chat: bool = True, cra: str | None = None) -> str:
    """The chat or email reply: each answer in plain words; the quotes and workings are on the details page.
    cra names the CRA in the last line."""
    blocks = []
    for a in answers:
        lines = [f"*{a['question']}*" if chat else f"Q: {a['question']}"] if len(answers) > 1 else []
        if a.get("serious"):  # said whatever else is found: reporting a serious event cannot wait for the answer
            lines.append(f"⚠️ {SERIOUS_LINE}")
        lines.append(a.get("plain") or (a.get("ask") or NOT_FOUND if not a["found"] else NOT_SURE if not a["traced"]
                                        else a["text"]))
        blocks.append("\n".join(lines))
    more = ("" if not link else f"\n\n<{link}|Details and the protocol's words>" if chat
            else f"\n\nDetails and the protocol's words: {link}")
    end = footer(cra)
    if any(a.get("from_record") and a.get("found") for a in answers):
        end = end.replace("From the study documents.", "From the study documents and earlier messages.")
    return "\n\n".join(blocks) + more + (f"\n\n_{end}_" if chat else f"\n\n{end}")


def render_full(answers: list[dict]) -> str:
    """Every answer with its workings and the protocol's words, as plain text (the command line and tests)."""
    blocks = []
    for a in answers:
        lines = [f"Q: {a['question']}"] + ([f"⚠️ {SERIOUS_LINE}"] if a.get("serious") else [])
        words = [f'Protocol, {h}: "{_shown(w)}"' for h, w in a["quotes"]]
        if not a["found"]:
            lines.append(NOT_FOUND)
        elif not a["traced"]:
            lines += ["I could not work out a sure answer. These are the protocol's words on it:"] + words
        else:
            lines += [a["text"]] + [f"Worked out by code: {what}: {r}" for what, r in a.get("sums", [])] + words
            lines += [f"From an earlier message: {x}" for x in a.get("from_record") or []]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def save_page(study: Study, msg: dict, email: str, answers: list[dict]) -> str:
    """Keeps the full answers for the details page; returns its link."""
    token = secrets.token_urlsafe(12)
    db.ex("INSERT INTO answer_pages (study_id, message_id, link_token, message_text, answers, created_at)"
          " VALUES (?,?,?,?,?,?)", (study.study_id, msg.get("id"), token, email, db.js(answers), db.now()))
    return f"{settings.get('web.base_url', 'http://localhost:8080').rstrip('/')}/answers/{token}"


def _post(study: Study, msg: dict, text: str, files: list[dict] | None = None,
          purpose: str = "question_answer") -> int | None:
    if msg.get("channel") == "teams":
        m = re.fullmatch(r"teams:((spaces/[^/]+)/threads/[^/]+)", msg.get("thread_key") or "")
        if not m:
            return None
        return routing.enqueue(purpose=purpose, channel="teams", destination="teams_study_channel",
                               body=text, message_id=msg["id"], study_id=study.study_id,
                               meta={"target": {"space": m.group(2), "thread": m.group(1), "key": "study_channel"},
                                     **({"files": files} if files else {})})
    sender = msg.get("from_addr")
    if not sender or sender.lower() == (settings.get("mailbox.address") or "").lower():
        return None
    subject = msg.get("subject") or "your question"
    meta = {"thread_key": msg["thread_key"], "in_reply_to": msg.get("rfc_message_id"),
            "references": (msg.get("refs") or []) + ([msg["rfc_message_id"]] if msg.get("rfc_message_id") else []),
            "gmail_thread": msg["thread_key"].split(":", 1)[1], **({"files": files} if files else {})}
    # An email reply ends like the alerts do: the deployment's sign-off, with its automated-message disclaimer.
    body = f"{text}\n\n{study.monitor_signature}"
    return routing.enqueue(purpose=purpose, channel="email", destination="email", body=body,
                           subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
                           message_id=msg["id"], from_addr=routing.from_alias(study, msg), to=[sender], meta=meta,
                           study_id=study.study_id)


def answer_message(study: Study, msg: dict, email: str, use_cache: bool = True,
                   harm_only: bool = False) -> list[dict]:
    """Answers to the questions the message asks that the documents could answer (none: an empty list).
    harm_only: group chat not talking to the bot, where only how to report serious harm is said."""
    earlier = conversation.recent(msg) if msg.get("id") else []
    turn = capture.turn(msg)
    to_bot = turn in ("direct", "addressed") or (turn == "email" and msg.get("visibility") == "to")
    follow_up = to_bot and conversation.bot_answered_lately(earlier)
    if not guide.checker(study).get("answer_questions", True) or not (
            HARM_WORDS.search(email or "") or (not harm_only and (
                asks_something(email) or follow_up or (to_bot and asks_the_documents(email, use_cache))))):
        return []
    qs = [q for q in find(email, use_cache, earlier) if q.get("harm") or not harm_only]
    if earlier:
        db.audit("conversation_used", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                 turns=len(earlier), questions=[q["question"] for q in qs])
    if not qs:
        return []
    every = guide.load(study.study_id)
    items = reader.view(study, every)
    sources = [i for i in every if i["kind"] == "section"]
    answers = [answer_one(study, msg, q, items, sources, email, use_cache) for q in qs]
    # the reporting question was added on a hunch of harm: kept only when the protocol's definition makes it serious
    answers = [a for a in answers if a["question"] != REPORTING_QUESTION or a["serious"]]
    who = study.person_by_email(msg.get("from_addr"))
    role = study.role_label(who.roles[0]) if who and who.roles else "study site staff"
    tone = (careful.reader_rule(study, msg.get("from_addr")) or {}).get("tone")
    for a in answers:
        if tone:
            a["tone"] = tone
        a["plain"] = plain(a, role, use_cache)
        if a.get("output_check"):  # the rewording added a judgment: the checked answer was sent instead
            db.audit("answer_wording_refused", entity_type="message", entity_id=msg.get("id"),
                     study_id=study.study_id, question=a["question"], problems=a["output_check"])
        backing = " ".join([a["question"], email] + [w for _, w in a.get("quotes") or []] + (a.get("record") or []))
        if judged := outcheck.added_judgment(a["plain"], backing):  # recorded to measure; not blocked yet
            db.audit("answer_judgment_flagged", entity_type="message", entity_id=msg.get("id"),
                     study_id=study.study_id, question=a["question"], problems=judged)
    if settings.feature("privacy_check"):
        privacy.screen(study, msg, answers, use_cache)
    return answers


def facts(study: Study, msg: dict, topic: str, email: str, use_cache: bool = True) -> dict | None:
    """The documents' proven words on a topic, for a question the bot does not judge itself; None when none."""
    every = guide.load(study.study_id)
    sources = [i for i in every if i["kind"] == "section"]
    q = {"question": f"What do the study documents say about {topic}?", "participant": "", "harm": False}
    a = answer_one(study, msg, q, reader.view(study, every), sources, email, use_cache)
    if not a["found"]:
        return None
    by_heading = {reader._heading(s): s for s in sources}
    by_id = {s["id"]: s for s in sources}
    order = [by_heading[h] for h, _ in a["quotes"] if h in by_heading] + [by_id[x] for x in a["chosen"]]
    a["sections"], a["left_out"], size = [], [], 0
    for s in {s["id"]: s for s in order}.values():
        if size + len(s["text"]) > FACTS_CHARS:
            a["left_out"].append(reader._heading(s))
            continue
        a["sections"].append((reader._heading(s), s["text"]))
        size += len(s["text"])
    return a


def facts_file(page: dict, study_title: str) -> str:
    """An answer page as a plain file of facts, with the protocol's words and a question for another AI."""
    asked = re.sub(r"^\W*(?:(?:hey|hi|hello|ok|okay)\s+)?@?(?:huma\s+)?bot\b[\s,:]*", "", page["message_text"].strip(),
                   flags=re.I) or page["message_text"].strip()
    out = [f"# Facts from the study documents", f"Study: {study_title}", "", f"Asked: {asked}", ""]
    for a in json.loads(page["answers"]):
        out += [f"## {a['question']}", "", a["text"], ""]
        out += [f"- {what}: {r}" for what, r in a.get("sums", [])]
        out += ["### The protocol's words (found word for word in the documents)", ""]
        out += [f"- {h}: \"{w}\"" for h, w in a["quotes"]] + [""]
        if a.get("sections"):
            out += ["### The protocol sections, in full (word for word)", ""]
            for h, text in a["sections"]:
                out += [f"#### {h}", "", text, ""]
        if a.get("left_out"):
            out += ["Left out to keep this file short (see the protocol): " + "; ".join(a["left_out"]), ""]
    out += ["## To ask another AI", "",
            f"Using only the facts and protocol sections above from the {study_title} documents, "
            f"help with this: {asked}", "", FOOTER]
    return "\n".join(out)


def reply(study: Study, msg: dict, email: str, use_cache: bool = True, harm_only: bool = False) -> dict | None:
    """Answer the message's questions, if it asks any the documents could answer. Returns a trace, or None."""
    answers = answer_message(study, msg, email, use_cache, harm_only)
    if not answers:
        return None
    text = render(answers, save_page(study, msg, email, answers), chat=msg.get("channel") == "teams",
                  cra=cra_names(study))
    text = language.fit(study, msg, text, use_cache)
    oid = _post(study, msg, text)
    hashes = [sha256(" ".join((a.get("question") or "").lower().split())) for a in answers]
    db.audit("question_answered", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             outbox_id=oid, answers=answers, question_sha256=hashes)
    for a, h in zip(answers, hashes):   # a question the documents did not answer is its own record (QAA-02)
        if not a.get("found"):
            db.audit("question_not_answered", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                     outbox_id=oid, question=a.get("question"), question_sha256=h,
                     reply=a.get("plain") or a.get("text"))
    return {"answers": answers, "outbox_id": oid, "text": text}

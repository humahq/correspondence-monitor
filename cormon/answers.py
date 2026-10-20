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
from datetime import date

from dateutil.relativedelta import relativedelta

from . import db, guide, llm, memory, reader, routing, settings
from .config import Study

ASKED = re.compile(r"\?|^\s*(?:what|when|how|which|where|why|who|is|are|can|could|should|do|does|did|may|must|"
                   r"would|will)\b", re.I | re.M)
MAX_QUESTIONS = 3
FOOTER = "From the study documents. Please confirm with your CRA before acting on it."
FACTS_CHARS = 40000  # whole protocol sections in a facts file, so another AI can be pasted all of it

FIND_SYSTEM = """You read a message a clinical-trial site sent to the study monitor and list the questions in it
that the study documents (protocol, lab manual, consent, visit schedule) or the participant's visit dates could
answer: anything on how the study is run, for example visit windows and timing, what a visit includes,
eligibility, dosing, medicines, samples, consent, deviations, adverse event reporting, records and data entry,
monitoring, scales and scores, withdrawal and early termination.
Leave out questions for a person rather than the documents (asking for kits, a call, a reply, whether an email
arrived, who is covering), rhetorical questions, and questions the message itself already answers.
Reword each question to stand alone, keeping every number, date and visit name and what the message reports that
bears on it. participant: the participant id it is about, or "" when it is general. harm: true when the message
reports a death, a hospital admission, a life-threatening event, a disability, a birth defect or other serious
harm to a participant (or their baby) that happened, whatever the question is about; a reason for a remote
visit or a condition at enrolment is not harm. harm and harm_participant are also set for the whole message.
Return no questions when there are none."""

FIND_SCHEMA = llm.obj({"questions": {"type": "array", "items": llm.obj({
    "question": {"type": "string"},
    "participant": {"type": "string"},
    "harm": {"type": "boolean"}})},
    "harm": {"type": "boolean"}, "harm_participant": {"type": "string"}})

HARM_WORDS = re.compile(r"\b(?:died|death|dead|passed away|deceased|hospitali[sz]|admitted|admission|\bE[DR]\b|"
                        r"emergency|life[- ]threatening|anaphyla|intubat|ICU|surgery|fracture|stroke|seizure|"
                        r"congenital|birth defect|disabilit|suicid|overdose)", re.I)
REPORTING_QUESTION = "This message reports serious harm to a participant: how must it be reported under the protocol?"

PICK_SYSTEM = """You pick the parts of a study guide needed to answer a site's question: the sections and visit
checklists whose rules decide it, for the question and for what the message reports (an event that happened can
bring its own rules: reporting, deviations, exceptions). Include sections with exceptions to the rule asked about.
At most 10 ids, the most decisive first. Use only ids from the index."""

PICK_SCHEMA = llm.obj({"ids": {"type": "array", "items": {"type": "string"}}})

ANSWER_SYSTEM = """You are a clinical-trial monitor answering a site's question from the study documents only.
Rules:
- Answer only from the PROTOCOL SECTIONS and the PARTICIPANT RECORD given. Never from general knowledge.
- answerable: false when the sections do not settle the question; then say in answer what is missing.
- answer: two to four short sentences in plain English. Lead with the direct answer (a number, a date range,
  yes or no). For a participant, use the visit windows worked out by code in the record; do not redo that
  arithmetic. A sum of your own (a gap between visits, a total, a difference) goes in calculations, written with
  the protocol's or the record's own numbers ("84 - 14"); code works it out and checks each number, and you may
  give its result in the answer. If the protocol does not state the answer directly but its numbers settle it,
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
- If the record or message shows the participant has died or withdrawn, give no future visit dates."""

ANSWER_SCHEMA = llm.obj({
    "answerable": {"type": "boolean"},
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
  does not state this directly", where a date came from.
- Keep the participant's own calendar dates when the checked answer gives them; do not swap them for day numbers.
- Use only facts in the CHECKED ANSWER and the PROTOCOL'S WORDS. Never add a number, a date or a rule of your own.
- When a serious event must be reported, say to whom and how fast, taken from the protocol's words.
- No greeting and no sign-off."""

PLAIN_SCHEMA = llm.obj({"text": {"type": "string"}})

SAE_TITLE = re.compile(r"serious adverse|\bSAEs?\b|reportable event|reporting of (?:serious )?adverse", re.I)
SERIOUS_LINE = "This may be a Serious Adverse Event. Contact the PI and the Medical Monitor now."


def asks_something(text: str) -> bool:
    return bool(ASKED.search(text or ""))


def find(text: str, use_cache: bool = True) -> list[dict]:
    """The message's questions for the documents. A message reporting serious harm always gets one: how to report
    it, if none of its own questions is about the harm."""
    res = llm.structured("answer_find", FIND_SYSTEM, f"MESSAGE:\n{text}", FIND_SCHEMA, "questions",
                         use_cache=use_cache)
    qs = [q for q in res["questions"] if q["question"].strip()][:MAX_QUESTIONS]
    if res["harm"] and not any(q["harm"] for q in qs):
        qs = [{"question": REPORTING_QUESTION, "participant": res["harm_participant"], "harm": True}] + qs
    return qs


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


def _norm_nums(s: str) -> set[str]:
    return {str(int(float(x))) if float(x).is_integer() else x for x in re.findall(r"\d+(?:\.\d+)?", s)}


def _numbers_traced(answer: str, backing: str) -> bool:
    """Every number in the answer appears in the question, quotes or record, or was worked out by code (dates and
    day counts the AI did not make up). Leading zeros do not matter."""
    return _norm_nums(answer) <= _norm_nums(backing)


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


def calculate(expression: str, backing: str) -> str | None:
    """A sum the AI asked for, worked out by code ("84 - 14 = 70"), when it is plain arithmetic on numbers found in
    the protocol's words or the record. None otherwise."""
    expr = expression.replace("−", "-").replace("×", "*").replace("÷", "/").strip()
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
    visits = [i for i in items if i["kind"] == "visit"]
    record = memory.record_lines(past)
    sched = schedule(past, visits)
    # the numbered sections themselves, not topic briefs: a brief cites dozens of sections, too many to quote from
    pickable = sources + visits
    known = {i["id"]: i for i in pickable}
    picked_ids = llm.structured("answer_pick", PICK_SYSTEM, "GUIDE INDEX (id | heading | what it covers):\n"
                                + reader.menu(pickable) + f"\n\nTHE SITE'S MESSAGE:\n{email}\n\nQUESTION: "
                                f"{q['question']}", PICK_SCHEMA, "pick", use_cache=use_cache)["ids"]
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
    found = _proof(res, q, secs, checklists, sources, visits, email, record, sched)
    if not found["traced"] and found["proven"]:
        loose = sorted(_norm_nums(found["text"]) - _norm_nums(found["backing"]), key=len)
        why = found["conflict"] or (f"These numbers in your answer are not in the protocol's words or the record: "
                                    f"{', '.join(loose)}. Give each one as a calculation from the protocol's or "
                                    f"the record's numbers, or leave it out.")
        fix = f"\n\nYOUR ANSWER:\n{json.dumps(res, ensure_ascii=False)}\n\n{why} Answer again in full."
        res2 = llm.structured("answer_question", ANSWER_SYSTEM, user + fix, ANSWER_SCHEMA, "answer",
                              use_cache=use_cache)
        again = _proof(res2, q, secs, checklists, sources, visits, email, record, sched)
        if again["traced"] and again["proven"]:
            res, found = res2, again
        elif found["conflict"]:  # the AI keeps the wrong pair: code's own answer from the quoted rows
            found = dict(found, text=found["code_answer"], traced=True, sums=found["sums"][-1:])
    # an answer that says the protocol is silent but gives its proven numbers is still shown
    return {"question": q["question"], "participant": pid, "text": found["text"], "quotes": found["proven"][:4],
            "sums": found["sums"], "found": bool(found["proven"]), "serious": bool(res["serious_event"]),
            "traced": found["traced"], "picked": [i["id"] for i in secs + checklists],
            "read": [reader._heading(i) for i in secs + checklists], "record": record, "windows": sched,
            "chosen": [x for x in dict.fromkeys(picked_ids) if x in known and known[x]["kind"] == "section"]}


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
    if not a["found"]:
        return NOT_FOUND
    if not a["traced"]:
        return NOT_SURE
    facts = "\n".join([a["text"]] + [f"{what}: {r}" for what, r in a.get("sums", [])])
    words = "\n".join(f"- {w}" for _, w in a["quotes"]) or "- none"
    try:
        said = _clean(llm.structured("answer_plain", PLAIN_SYSTEM, f"READER: {reader_role}\n\nQUESTION: "
                                     f"{a['question']}\n\nCHECKED ANSWER:\n{facts}\n\nPROTOCOL'S WORDS:\n{words}"
                                     + ("\n\nThis is a serious event." if a.get("serious") else ""),
                                     PLAIN_SCHEMA, "reply", use_cache=use_cache)["text"])
    except Exception:
        return a["text"]
    backing = " ".join([facts, a["question"]] + [w for _, w in a["quotes"]])
    lead = re.split(r"(?<=[.!?])\s", a["text"], maxsplit=1)[0]  # the direct answer: its numbers must all stay
    ok = said and _numbers_traced(said, backing) and _norm_nums(lead) <= _norm_nums(said)
    return said if ok else a["text"]


def render(answers: list[dict], link: str | None = None, chat: bool = True) -> str:
    """The chat or email reply: each answer in plain words; the quotes and workings are on the details page."""
    blocks = []
    for a in answers:
        lines = [f"*{a['question']}*" if chat else f"Q: {a['question']}"] if len(answers) > 1 else []
        if a.get("serious"):  # said whatever else is found: reporting a serious event cannot wait for the answer
            lines.append(f"⚠️ {SERIOUS_LINE}")
        lines.append(a.get("plain") or (NOT_FOUND if not a["found"] else NOT_SURE if not a["traced"] else a["text"]))
        blocks.append("\n".join(lines))
    more = ("" if not link else f"\n\n<{link}|Details and the protocol's words>" if chat
            else f"\n\nDetails and the protocol's words: {link}")
    return "\n\n".join(blocks) + more + (f"\n\n_{FOOTER}_" if chat else f"\n\n{FOOTER}")


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
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def save_page(study: Study, msg: dict, email: str, answers: list[dict]) -> str:
    """Keeps the full answers for the details page; returns its link."""
    token = secrets.token_urlsafe(12)
    db.ex("INSERT INTO answer_pages (study_id, message_id, link_token, message_text, answers, created_at)"
          " VALUES (?,?,?,?,?,?)", (study.study_id, msg.get("id"), token, email, db.js(answers), db.now()))
    return f"{settings.get('web.base_url', 'http://localhost:8080').rstrip('/')}/answers/{token}"


def _post(study: Study, msg: dict, text: str, files: list[dict] | None = None) -> int | None:
    if msg.get("channel") == "teams":
        m = re.fullmatch(r"teams:((spaces/[^/]+)/threads/[^/]+)", msg.get("thread_key") or "")
        if not m:
            return None
        return routing.enqueue(purpose="question_answer", channel="teams", destination="teams_study_channel",
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
    return routing.enqueue(purpose="question_answer", channel="email", destination="email", body=text,
                           subject=subject if subject.lower().startswith("re:") else f"Re: {subject}",
                           message_id=msg["id"], from_addr=routing.from_alias(study, msg), to=[sender], meta=meta,
                           study_id=study.study_id)


def answer_message(study: Study, msg: dict, email: str, use_cache: bool = True,
                   harm_only: bool = False) -> list[dict]:
    """Answers to the questions the message asks that the documents could answer (none: an empty list).
    harm_only: group chat not talking to the bot, where only how to report serious harm is said."""
    if not guide.checker(study).get("answer_questions", True) or not (
            HARM_WORDS.search(email or "") or (asks_something(email) and not harm_only)):
        return []
    qs = [q for q in find(email, use_cache) if q.get("harm") or not harm_only]
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
    for a in answers:
        a["plain"] = plain(a, role, use_cache)
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
    text = render(answers, save_page(study, msg, email, answers), chat=msg.get("channel") == "teams")
    oid = _post(study, msg, text)
    db.audit("question_answered", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             outbox_id=oid, answers=answers)
    return {"answers": answers, "outbox_id": oid, "text": text}

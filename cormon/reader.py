"""The reader checker: the AI reads, whole, the protocol sections an email is about and lists every problem.

1. Scope: which participants the email is about and, for each, the email split into single statements, each
   anchored to the guide parts that govern it, plus the visits and dates involved.
2. Date arithmetic is done in code (study days, hours from dose to sample, ages) and handed to the AI as facts.
3. The judge reads the anchored sections, the always-read key sections and the named visits' checklists for one
   participant at a time. It must check every statement against every part anchored to it, run five checks
   (conditions, disagreeing passages, time limits, what already happened, no problem inside an ok) and list every
   departure, with the protocol's words and the next step. One second round covers parts it asked for, statements
   it left unchecked, numbers outside its own ranges and a reviewer's notes.
4. Every finding is confirmed: its protocol quote must be found in the sections, and a second reading must agree;
   one the second reading rejects but whose protocol words are found goes out as "please check" (checker.disputed).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import date, datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser
from dateutil.relativedelta import relativedelta

from . import guide, ingest, llm, settings, textutil
from .config import Study
from .matching import Finding

KEY_TOPICS = ["eligibility", "concomitant_medications", "dosing", "safety"]
NOT_CHECKABLE = {"background", "statistics", "admin"}
DATE_KINDS = ["baseline", "date_of_birth", "screening", "consent", "randomization", "first_dose", "dose", "last_dose",
              "visit", "phone_visit", "sample", "procedure", "lab", "adverse_event", "extension_start",
              "termination", "shipment", "other"]


# ---------------------------------------------------------------- the guide, as the checker sees it

def _usable(item: dict) -> bool:
    return bool(item["text"].strip()) and (item["kind"] == "visit" or not set(item["topics"]) <= NOT_CHECKABLE)


def _heading(item: dict) -> str:
    if item["kind"] == "visit":
        return f"Visit checklist: {item['title']} ({item['path']})"
    if item["kind"] == "pack":
        return f"Topic pack: {item['title']}"
    if item["kind"] == "piece":
        return f"Topic: {item['title']}"
    if item["kind"] == "brief":
        return f"Topic brief: {item['title']}"
    return f"{item['number']} {item['title']}".strip()


def view_kind(study: Study) -> str:
    """What the checker reads besides the visit checklists: "sections", whole topic "packs", "pieces" (one
    sub-topic of a pack each) or "briefs" (each pack as short lines; findings are confirmed against the source
    sections the lines cite). Set by checker.view; CORMON_VIEW overrides it for comparing on a test run."""
    c = guide.checker(study)
    return os.environ.get("CORMON_VIEW") or c.get("view") or ("packs" if c.get("packs") else "sections")


def view(study: Study, items: list[dict]) -> list[dict]:
    kind = {"sections": "section", "packs": "pack", "pieces": "piece", "briefs": "brief"}[view_kind(study)]
    chosen = [i for i in items if i["kind"] == kind]
    if not chosen:
        chosen = [i for i in items if i["kind"] == "section"]
    return chosen + [i for i in items if i["kind"] == "visit"]


def _block(item: dict) -> str:
    return f"[{item['id']}] {_heading(item)}\n{item['text']}"


def key_sections(study: Study, items: list[dict]) -> list[dict]:
    """Sections read for every email: those whose main topic is a key topic (eligibility, medicines, dosing,
    safety by default)."""
    c = guide.checker(study)
    topics = set(c["key_topics"] if "key_topics" in c else KEY_TOPICS)
    return [i for i in items if i["kind"] in ("section", "pack", "brief") and _usable(i) and i["topics"]
            and i["topics"][0] in topics]


def menu(items: list[dict]) -> str:
    return "\n".join(f"{i['id']} | {_heading(i)} | {i['about']}" for i in items if _usable(i))


# ---------------------------------------------------------------- 1. scope

SCOPE_SYSTEM = """You prepare a site email for a check against the study protocol.
List each participant the email is about, one entry per participant, plus one entry with participant "" for
anything not about a particular participant (drug shipments, storage, site supplies, general questions).
Leave out participants only mentioned in passing with nothing done, planned or asked about them.
For each entry:
- summary: what the email says was done, is planned, or is asked for this participant. Keep every number, date,
  time, dose, medicine and test result.
- statements: the email's content for this participant split into single facts, one per statement: one thing
  done, planned, asked or reported (a visit and its date, a sample and its time, a dose, a medicine, a test result,
  a missed or skipped item, a question). Leave none out. Each medicine named is its own statement, even within a
  list ("tacrolimus and quetiapine" are two statements). For each, status, and anchors: the ids of every guide
  section and visit checklist that governs it, including the rules that could restrict it (allowed and forbidden
  medicines, timing rules, conditions, reporting duties). At most 15 anchors per statement.
- visits: ids of the visit checklists for every visit this part names or implies. Mind the period: weeks in the
  active treatment extension (ATE) have their own checklists, and the Week 24 visit ends the placebo period.
- sections: ids of every guide section that could govern anything in this part: medicines, doses and dosing times,
  samples and their timing, procedures and their order, visit windows, eligibility, pregnancy and contraception,
  dose changes, overdose, early termination, the extension, supplies and storage, remote visits. Be generous: a
  section left out is a problem missed.
- dates: every date and clock time stated or implied for this participant, as ISO dates, resolving weekdays and
  relative dates ("Wednesday", "tomorrow", "next week") from the email's date. Include the Baseline date, date of
  birth, first and last dose, visits, samples, procedures, lab draws, adverse events and the start of the extension
  when given. Every consent signing (main study, regimen-specific, re-consent) is its own entry of kind consent,
  and randomization its own entry of kind randomization, with clock times when given. Every study-drug dose taken
  or given is its own entry of kind dose (a dose at home and another in clinic on one day are two entries); a
  dose that is agreed, planned, held or missed is not kind dose. When a visit also starts a new period (for example the visit where an extension begins), add a
  separate entry of kind extension_start with that date. A clock time goes in time (HH:MM, 24-hour),
  never in the date. visit_id: for a visit's date, the id of that visit's checklist; otherwise "".
Use only ids from the guide index. If the email is not about the study at all, return no entries."""

SCOPE_SCHEMA = llm.obj({"parts": {"type": "array", "items": llm.obj({
    "participant": {"type": "string"},
    "summary": {"type": "string"},
    "statements": {"type": "array", "items": llm.obj({
        "text": {"type": "string"},
        "status": {"type": "string", "enum": ["done", "planned", "asked", "reported", "historical"]},
        "anchors": {"type": "array", "items": {"type": "string"}}})},
    "visits": {"type": "array", "items": {"type": "string"}},
    "sections": {"type": "array", "items": {"type": "string"}},
    "dates": {"type": "array", "items": llm.obj({
        "label": {"type": "string"},
        "kind": {"type": "string", "enum": DATE_KINDS},
        "date": {"type": "string"},
        "time": llm.nullable("string"),
        "visit_id": {"type": "string"}})}})}})


def _email_date(msg: dict) -> datetime:
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    try:
        return dtparser.isoparse(msg["sent_at"]).astimezone(tz)
    except (KeyError, TypeError, ValueError):
        return datetime.now(tz)


def scope(study: Study, items: list[dict], email: str, sent: datetime, use_cache: bool) -> list[dict]:
    user = (f"EMAIL DATE: {sent:%A %d %B %Y}\n\nGUIDE INDEX (id | heading | what it covers):\n{menu(items)}\n\n"
            f"EMAIL:\n{email}")
    parts = llm.structured("scope", SCOPE_SYSTEM, user, SCOPE_SCHEMA, "scope", use_cache=use_cache)["parts"]
    kind = {i["id"]: i["kind"] for i in items}
    for p in parts:
        for s in p.get("statements", []):
            s["anchors"] = [a for a in s["anchors"] if a in kind][:15]
            for a in s["anchors"]:
                (p["visits"] if kind[a] == "visit" else p["sections"]).append(a)
        p["visits"], p["sections"] = list(dict.fromkeys(p["visits"])), list(dict.fromkeys(p["sections"]))
    return parts


# ---------------------------------------------------------------- 2. date arithmetic in code

def _day(s: str | None) -> date | None:
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _clock(d: date, t: str | None) -> datetime | None:
    m = re.match(r"^(\d{1,2}):(\d{2})", t or "")
    return datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2))) if m else None


_WINDOW_DAY = re.compile(r"day\s*(-?\d+)\s*(?:±\s*(\d+))?", re.I)
_WINDOW_RANGE = re.compile(r"[–-]\s*(\d+)\s*to\s*[–-]?\s*(\d+)\s*days?", re.I)
_WINDOW_AFTER_LAST = re.compile(r"(\d+)\s*days?\s*after\s*(?:the\s*)?last\s*dose\s*(?:±\s*(\d+))?", re.I)


def window(visit: dict) -> tuple[str, int, int] | None:
    """A visit checklist's window as (anchor, first day, last day); anchor is baseline, extension_start or
    last_dose. None when the window is not a fixed day count."""
    w = re.sub(r"\s+", " ", re.search(r"window ([^.]*)\.", visit["text"]).group(1)) if "window " in visit["text"] else ""
    ext = bool(re.search(r"extension|\bATE\b", visit.get("path") or "", re.I))
    if m := _WINDOW_AFTER_LAST.search(w):
        k = int(m.group(2) or 0)
        return "last_dose", int(m.group(1)) - k, int(m.group(1)) + k
    if m := _WINDOW_RANGE.search(w):
        return ("extension_start" if ext else "baseline"), -int(m.group(1)), -int(m.group(2))
    if (m := _WINDOW_DAY.search(w)) and "from last visit" not in w.lower():
        k = int(m.group(2) or 0)
        return ("extension_start" if ext else "baseline"), int(m.group(1)) - k, int(m.group(1)) + k
    return None


_AFTER_CONSENT = {"screening", "randomization", "first_dose", "dose", "visit", "sample", "procedure", "lab",
                  "baseline", "extension_start"}


def date_notes(dates: list[dict], sent: datetime, visits: dict[str, dict] | None = None) -> list[str]:
    pts = [dict(d, day=_day(d["date"])) for d in dates]
    pts = [p for p in pts if p["day"]]
    if not pts:
        return []
    today = sent.date()
    base = next((p["day"] for p in pts if p["kind"] == "baseline"), None)
    ext = next((p["day"] for p in pts if p["kind"] == "extension_start"), None)
    dob = next((p["day"] for p in pts if p["kind"] == "date_of_birth"), None)
    last = next((p for p in pts if p["kind"] == "last_dose"), None)
    out = []
    for p in sorted(pts, key=lambda p: (p["day"], p.get("time") or "")):
        if p["kind"] == "date_of_birth":
            continue
        bits = [f"{p['label']}: {p['day']:%A %d %b %Y}" + (f" {p['time']}" if p.get("time") else "")]
        if base and p["kind"] != "baseline":
            n = (p["day"] - base).days
            bits.append(f"Day {n} counting Baseline ({base:%d %b %Y}) as Day 0, i.e. {n / 7:.1f} weeks after Baseline")
        if ext and p["day"] > ext:
            bits.append(f"Day {(p['day'] - ext).days} after the extension started ({ext:%d %b %Y})")
        if last and p is not last and p["day"] > last["day"]:
            bits.append(f"{(p['day'] - last['day']).days} days after the last dose")
        if dob and p["kind"] in ("screening", "consent", "randomization", "baseline", "first_dose"):
            bits.append(f"age {relativedelta(p['day'], dob).years} (completed years)")
        v = (visits or {}).get(p.get("visit_id") or "")
        w = window(v) if v else None
        anchor = {"baseline": base, "extension_start": ext, "last_dose": last["day"] if last else None}.get(w[0]) if w else None
        if w and anchor:
            n = (p["day"] - anchor).days
            lo, hi = w[1], w[2]
            where = ("inside the window" if lo <= n <= hi else
                     f"{lo - n} day(s) before the window opens" if n < lo else f"{n - hi} day(s) after the window closes")
            bits.append(f"{v['title']} window is Day {lo} to Day {hi} from {w[0].replace('_', ' ')} "
                        f"({anchor + relativedelta(days=lo):%d %b} to {anchor + relativedelta(days=hi):%d %b %Y}); "
                        f"this date is Day {n}: {where}")
        k = (p["day"] - today).days
        bits.append("the email's date" if k == 0 else f"{abs(k)} days {'after' if k > 0 else 'before'} the email's date")
        out.append("; ".join(bits))
    for c in (p for p in pts if p["kind"] == "consent" and not re.search(r"re-?\s?consent", p["label"], re.I)):
        for p in pts:
            if p is c or p["kind"] not in _AFTER_CONSENT:
                continue
            a, b = _clock(p["day"], p.get("time")), _clock(c["day"], c.get("time"))
            if p["day"] < c["day"] or (a and b and a < b):
                gap = f"{(c['day'] - p['day']).days} day(s)" if p["day"] < c["day"] else f"{(b - a).seconds / 3600:.1f} hours"
                out.append(f"{p['label']} is {gap} BEFORE {c['label']}")
            elif p["day"] == c["day"] and not (a and b):
                out.append(f"{p['label']} is on the same day as {c['label']}; no clock times say which came first, "
                           f"so take the order from the email's words")
    doses = [p for p in pts if p["kind"] in ("dose", "first_dose", "last_dose") and _clock(p["day"], p.get("time"))]
    for d in doses:
        for p in pts:
            if p is d or not _clock(p["day"], p.get("time")) or p["kind"] in ("dose", "first_dose", "last_dose"):
                continue
            h = (_clock(p["day"], p["time"]) - _clock(d["day"], d["time"])).total_seconds() / 3600
            if abs(h) <= 48:
                out.append(f"{p['label']} is {abs(h):.1f} hours {'after' if h >= 0 else 'before'} {d['label']}")
    for d in (p for p in pts if p["kind"] == "first_dose"):
        for p in pts:
            gap = (d["day"] - p["day"]).days
            if p["kind"] in ("sample", "procedure", "lab", "screening", "baseline") and 1 < gap <= 30:
                out.append(f"{p['label']} is {gap} days before {d['label']}")
    by_day: dict[date, list[dict]] = {}
    for p in pts:
        if p["kind"] in ("dose", "first_dose") and not _NOT_TAKEN.search(p["label"]):
            by_day.setdefault(p["day"], []).append(p)
    for day, group in by_day.items():
        times = {p["time"] for p in group if p.get("time")}
        if len(group) > 1 and len(times) >= 2:  # two doses taken at different clock times, not one dose named twice
            out.append(f"{len(group)} doses on {day:%d %b %Y} ({'; '.join(p['label'] for p in group)}): "
                       f"more than one dose in a day")
    return out


_NOT_TAKEN = re.compile(r"\b(?:agree|plan|decid|decision|schedul|instruct|order|approv|discuss|prescri|withh[oe]ld|"
                        r"hold|missed|skipped)", re.I)

_EXTRA_DOSE = re.compile(
    r"\b(?:[3-9]|three|four|five|six|double|extra|additional|second)\s+(?:\w+\s+)?(?:packets?|sachets?|stick ?packs?|"
    r"doses?)\b|\bcatch(?:ing)?[- ]?up\b|\bmake[- ]?up\b|\bdouble[- ]?dos", re.I)


def dose_notes(text: str, sources: list[dict]) -> tuple[list[str], list[str]]:
    """A fact when the text describes more study drug than one dose (extra packets, a catch-up or second dose), with
    the protocol sections on overdose and medication errors, so their duties (reporting, contacting the medical
    monitor, monitoring the participant) are checked."""
    hits = sorted({m.group(0).strip() for m in _EXTRA_DOSE.finditer(text)})
    if not hits:
        return [], []
    secs = [s["id"] for s in sources if re.search(r"overdos|medication (?:administration )?error", s["title"], re.I)]
    return ([f"The email describes possibly more than one dose ({', '.join(hits)}). If more study drug was taken or "
             f"given than the dose studied (including a dose at home and another in clinic the same day), it is an "
             f"overdose / medication error: check each duty in the overdose sections {', '.join(secs) or ''} "
             f"(reporting, contacting the medical monitor and its time limit, monitoring the participant) and "
             f"whether the email shows it done; name every duty not shown done, with its time limit, in the "
             f"finding's next step."], secs)


# ---------------------------------------------------------------- 3. the judge, one participant at a time

READ_SYSTEM = """You are an experienced clinical-trial monitor checking one site email against the study protocol.
You get the protocol text that governs what the email is about (in the protocol's own words; tables are one line
per row), checklists for the visits it names, and date arithmetic done by code. Trust the code's numbers; you decide
what they mean against the protocol's windows and rules. A topic pack gathers one topic's rules from every study
document; each line ends with its source section in brackets, and a line noted "replaced by ..." is overruled by
the document named.

Work in two steps.
1. checks: the email has been split into numbered STATEMENTS, each with the ids of the protocol parts linked to it.
   Check every statement against every part linked to it, not only the ones that first come to mind: at least one
   check per statement (give its number in statement), and one check per linked part that has a rule for it.
   Also check anything in the email the statements missed (statement 0): every date and visit, procedure, sample
   and its timing, dose and dosing time, medicine, test result, pregnancy or contraception detail, consent, dose
   change, supply or storage detail. For each check, name the protocol text that governs it and decide: ok,
   problem, reminder or not_covered. Then go through the checklist of each visit named (statement 0).
   A required item the email says will be skipped or done wrongly, or that is missing from a list the email
   presents as complete, is a problem. A required item the email simply does not mention is a reminder.
   When a check compares a number with a protocol limit (hours after a dose, a study day, days before the first
   dose, days since the last dose, a temperature, a dose, a count), fill measured and the allowed range
   (allowed_min, allowed_max) in the same unit; otherwise leave them null.
2. findings: every departure from the protocol is its own finding, however minor. Two departures are two findings,
   even at the same visit. Being brief must never hide a departure. For each finding, copy the protocol's words
   exactly (one or two sentences, or for a visit checklist the item's line, as written in the sections given;
   no brackets or added words), give the id of the section they come from, quote the email, explain in one or two sentences, and
   give the next step the protocol calls for (for example: reschedule inside the window, do not collect, collect
   two post-dose samples instead, consult the medical monitor first, report within 24 hours, record a deviation).

Rules:
- Only the protocol text given counts. Do not add requirements it does not state.
- "Should" in the protocol is a requirement. Where it allows something ("may", "if possible", an alternative, the
  investigator's or medical monitor's judgment), that is not a problem, unless the email shows the site skipped the
  judgment or consultation the protocol requires.
- A plan to do something the protocol forbids, or to do it at the wrong time, is a problem now, before it happens.
- A question the site asks is a finding when the action it asks about would break the protocol.
- Compare every visit date with its window. The code states the comparison where the window is a fixed day count.
- Every line of the date arithmetic is a check of its own: compare it with every timing rule in the sections that
  could apply (visit windows, hours before or after a dose, days before the first dose, time since the last dose,
  the order of procedures and dosing on the same day).
- Every item the email names (a medicine, a contraceptive method, a device, a food or drink) is checked against the
  protocol's lists of allowed, restricted or forbidden items, minding whom each list applies to.
- Conditions matter: when the protocol allows something only for some participants or situations ("only if",
  "only for participants who", "within", "prior to"), check that the condition holds for this participant.
- Use the right period: the placebo-controlled schedule and the extension (ATE) schedule differ, and week numbers in
  the ATE count from the start of the ATE.
- Eligibility criteria apply to entering the study; a later event is judged by the rules for that later time.
- Read names and roles by their plain meaning: a step named in short or loosely counts as that step ("compliance"
  for accountability/compliance, "labs" for safety labs), and a person counts as any role they plainly belong to
  (study coordinators and nurses are clinical staff); a role not named word for word is not therefore excluded.
- What an email does not mention is not a finding unless the email sets out the whole of something (all of a
  visit's procedures, every step of a process) and a required part is missing from it, or says it was not done.
- "Should consider" (for example resuming the dose within 28 days) is a duty to weigh it: a plan that goes past it
  with no sign it was weighed is a finding. Doing more than the protocol requires in a way that affects the
  participant or the record (stopping a permitted medicine, logging a deviation that is not one) is a finding.
- A positive pregnancy test, or study drug continued while a pregnancy is possible, is never only a reminder: check
  the protocol's pregnancy duties (stopping drug, confirming, reporting) and whether the email shows them done.
- Consent comes before any study procedure, randomization or dose it covers; in a re-consent the discussion or
  review comes before the signature. A code fact that something came BEFORE a consent is a problem unless the
  protocol allows it (for example tests done as clinical care). Each consent covers its own part: a regimen's
  own tests, randomization within it and its dosing need that regimen's consent first, despite the master consent.
  A study may randomize more than once (to a regimen or arm, later to drug or placebo): match each order rule to
  the step it names, and say which one the email describes.
- A medicine the code finds in one of the protocol's drug lists: apply that list's rule to this participant
  (excluded, permitted with monitoring, washout before the first dose, consult the medical monitor), and check
  what the site did or plans against it, including stopping a medicine the protocol permits. Whether a medicine
  has a narrow therapeutic index is standard clinical knowledge: use it where clear (say which, and so whether the
  participant is excluded or must be monitored); where not clear, the unmade judgement is the finding.
- An adverse event is not itself a protocol problem; a missed reporting, handling or dosing step the protocol
  requires for it is.
- A departure in the past is still a finding (status done) until the email says it was corrected and reported
  (a deviation filed, the sponsor told, the record fixed); only then is its status already_handled. A monitor's
  letter listing findings from a visit lists departures that still need action.
- Do not report the same departure twice.
- If a protocol part you need is not in the sections given, put its id from the GUIDE INDEX in need_more.

Before you mark anything ok, run these five checks on every statement. Most missed problems come from skipping one.
1. Conditions. When the protocol allows something only under a condition ("only if", "if needed to", "provided
   that", "unless", "for participants who ..."), name the condition and check the email meets it. Another reason,
   or the condition not met, is a problem. If the email reports doing that thing but does not say whether the
   condition holds, it is a finding with certainty cannot_tell. A list of what may be done is not permission on
   its own: read the condition attached to the list.
2. Passages that disagree. When two passages give different values for the same thing (a window, an interval, a
   dose, a count), do not pick one or decide the email "fits both". Report the disagreement as a finding with
   certainty cannot_tell, quoting one of the values, giving both in why, with the next step "confirm with the
   sponsor / medical monitor".
3. Time limits. Work out every duration the email states or implies (days since an event, since a dose change,
   until the next supply or visit) and compare it with every time limit in the sections ("within 28 days", "for at
   least 14 days", "until the next dispensing visit").
4. What has already happened. When something went wrong, the next step must also cover what has already happened:
   drug already dispensed or taken, samples already drawn, procedures already done, reports already due.
   Fixing only what comes next is not enough. A pack's "Also applies" rules list such duties from other topics
   (report an overdose, record a deviation, a second-person check); each unmet one is its own finding.
5. No problem inside an ok. If an ok check needs a corrective action, or quotes a limit the email exceeds, it is
   not ok: make it its own finding.
cannot_tell is only for checks 1 and 2. A required item or step the email simply does not mention (a review, a
test, a report, a document) is a reminder, never a finding: the site may have done it without saying so.
In each finding, problem is what the site did, plans or asks, in your words, not the protocol's text. One
departure is one finding even when several sections govern it."""

READ_SYSTEM_SHORT = """You are an experienced clinical-trial monitor checking one site email against the study protocol.
You get the protocol text for what the email is about (topic packs: each line is the protocol's own words with its
source section in brackets; a line noted "replaced by ..." is overruled), visit checklists, the email split into
numbered statements, and date arithmetic done by code. Trust the code's numbers.

1. checks: check every statement against the protocol: at least one check per statement (statement 0 for anything
   the statements missed). Name the governing text and decide ok, problem, reminder or not_covered. When comparing a
   number with a limit, fill measured, allowed_min and allowed_max in the same unit.
2. findings: one finding per departure from the protocol, however minor. Copy the protocol's words exactly, give
   their section id, quote the email, explain briefly, and give the next step the protocol calls for, including
   what must now be reported, recorded or redone for what already happened.

Before marking anything ok, ask:
- Is there a condition ("only if", "unless", "for participants who") and does the email meet it?
- Is there a time limit or window, and does the date arithmetic fall inside it?
- When something went wrong, does an "Also applies" rule add a duty (report an overdose, record a deviation, a
  second-person check)? Each unmet duty is its own finding.
- Is a required item on a list the email presents as complete missing?

Consent comes before any procedure, randomization or dose it covers (a re-consent's discussion before its
signature); a code fact that something came BEFORE a consent is a problem unless the protocol allows it. A medicine
the code finds in a protocol drug list: apply that list's rule to this participant and to what the site did, using
standard clinical knowledge of whether it has a narrow therapeutic index (an unmade judgement is the finding).
Read names and roles plainly (a short name for a step is that step; coordinators are clinical staff). Silence is
not a finding unless the email sets out the whole of something and a required part is missing."Should consider" is a duty to weigh it; a plan past it with no sign it was weighed is a finding. Doing more than
required (stopping a permitted medicine) is a finding. A positive pregnancy test is never only a reminder.
Rules: only the protocol text given counts. "Should" is a requirement; "may" is not. A plan or question about doing
something forbidden is a finding now. A required item the email simply does not mention is a reminder, not a
finding. A past departure stays a finding (status done) until the email says it was corrected and reported; then its
status is already_handled. An adverse event is not itself a problem; a missed step the protocol requires for it is.
If two passages disagree on a value, report it with certainty cannot_tell. Otherwise certainty is conflict.
If you need a part not given, put its id from the GUIDE INDEX in need_more."""


def _read_system(study: Study) -> str:
    """The judge's instructions: the full version, or a short one (checker.prompt: short, or CORMON_PROMPT)."""
    short = (os.environ.get("CORMON_PROMPT") or guide.checker(study).get("prompt")) == "short"
    return READ_SYSTEM_SHORT if short else READ_SYSTEM


_TABLE_TITLE = re.compile(r"^Table\s+\d+\s*:\s*(.+)$", re.M)
_DRUG_ROW = re.compile(r"^- (?:[^|\n]*\|\s*)?Generic Drug Name:\s*([^|\n]+)(.*)$", re.M)


def medicine_lists(sources: list[dict]) -> dict[str, dict]:
    """Medicines named in the protocol's drug tables, by lower-case name: the table, the row's other columns and
    the section's own rule for the table (the text around it), read by code from the table rows."""
    out = {}
    for s in sources:
        if "Generic Drug Name:" not in s["text"]:
            continue
        rule = " ".join(ln for ln in s["text"].splitlines()
                        if re.search(r"\bTable \d+\b|exclud|permitted|prohibit|medical monitor", ln)
                        and not ln.startswith(("- ", "Table columns:", "Source:", "Abbreviations:"))
                        and not _TABLE_TITLE.match(ln))
        title = (_TABLE_TITLE.search(s["text"]) or re.search(r"(.+)", s["title"])).group(1).strip()
        for m in _DRUG_ROW.finditer(s["text"]):
            prefix = m.group(0)[2:m.group(0).find("Generic Drug Name:")].strip(" |")
            for name in re.split(r"\s*/\s*|\s*,\s*", m.group(1).strip()):
                key = _plain(re.sub(r"\s*\(.*\)", "", name))
                if len(key) > 3:
                    entry = {"name": name, "table": title, "section": s["id"],
                             "row": " | ".join(x for x in (prefix, m.group(2).strip(" |")) if x), "rule": rule}
                    out[key] = entry
                    if len(key.split()[0]) > 6:
                        out.setdefault(key.split()[0], entry)
    return out


INDEX_SYSTEM = """You are a clinical pharmacologist. For each medicine, say whether it is generally considered to have a
narrow therapeutic index (small differences in dose or blood level can cause serious failure or toxicity), from
standard clinical references. Give "narrow", "wide", or "unclear" when references disagree or it depends on use,
with one short reason."""

INDEX_SCHEMA = llm.obj({"medicines": {"type": "array", "items": llm.obj({
    "name": {"type": "string"}, "index": {"type": "string", "enum": ["narrow", "wide", "unclear"]},
    "reason": {"type": "string"}})}})


def therapeutic_index(meds: dict[str, dict], use_cache: bool = True) -> None:
    """Adds the ingest model's narrow/wide therapeutic-index judgement to each listed medicine, once per list
    (the protocol leaves it to clinical knowledge); a failure leaves the lists without it."""
    names = sorted({m["name"] for m in meds.values()})
    if not names:
        return
    try:
        res = llm.structured("ingest_meds", INDEX_SYSTEM, "\n".join(names), INDEX_SCHEMA, "therapeutic_index",
                             use_cache=use_cache, model=settings.get("llm.models.ingest") or llm.model_for("ingest"))
    except Exception:
        return
    got = {_plain(r["name"]): r for r in res["medicines"]}
    for m in meds.values():
        if r := got.get(_plain(m["name"])):
            m["index"] = r


def _plain(s: str) -> str:
    return re.sub(r"[’'`.]", "", s).lower().strip()


def medicine_notes(text: str, meds: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Facts for each listed medicine the text names, and the sections they come from."""
    notes, secs, seen, text = [], [], set(), _plain(text)
    for key, m in meds.items():
        if id(m) not in seen and re.search(rf"\b{re.escape(key)}\b", text):
            seen.add(id(m))
            notes.append(f"{m['name']} is listed in the protocol's \"{m['table']}\" [{m['section']}] ({m['row']}). "
                         f"The protocol's rule for that list: {m['rule']}"
                         + (f" Clinical knowledge, not protocol text: {m['name']} is generally considered "
                            f"{m['index']['index']} therapeutic index ({m['index']['reason']})."
                            if m.get("index") and m["index"]["index"] != "unclear"
                            and "therapeutic index" in m["rule"] else "")
                         + (" Where the rule permits the medicine only with monitoring or dose adjustment, the site "
                            "must show that monitoring; a plan that treats the medicine as no issue and names none "
                            "is a finding." if re.search(r"monitor", m["rule"], re.I) else ""))
            secs.append(m["section"])
    return notes, list(dict.fromkeys(secs))


def number_slips(checks: list[dict]) -> list[str]:
    """Checks the judge passed although its own number is outside its own allowed range."""
    out = []
    for c in checks:
        v, lo, hi = c.get("measured"), c.get("allowed_min"), c.get("allowed_max")
        if v is None or c["verdict"] == "problem" or (lo is None and hi is None):
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            rng = f"{lo if lo is not None else '-'} to {hi if hi is not None else '-'} {c.get('unit') or ''}".strip()
            out.append(f"\"{c['what']}\": you marked it {c['verdict']}, but {v:g} {c.get('unit') or ''} is outside "
                       f"the allowed range {rng}.")
    return out


def _class_enum(study: Study) -> list[str]:
    return [p["id"] for p in study.pattern_classes] or ["other"]


def read_schema(study: Study) -> dict:
    return llm.obj({
        "checks": {"type": "array", "items": llm.obj({
            "statement": {"type": "integer"},
            "what": {"type": "string"},
            "governed_by": {"type": "string"},
            "measured": llm.nullable("number"),
            "allowed_min": llm.nullable("number"),
            "allowed_max": llm.nullable("number"),
            "unit": {"type": "string"},
            "verdict": {"type": "string", "enum": ["ok", "problem", "reminder", "not_covered"]},
            "note": {"type": "string"}})},
        "findings": {"type": "array", "items": llm.obj({
            "problem": {"type": "string"},
            "email_quote": {"type": "string"},
            "protocol_quote": {"type": "string"},
            "section_id": {"type": "string"},
            "why": {"type": "string"},
            "next_step": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "moderate", "high"]},
            "certainty": {"type": "string", "enum": ["conflict", "cannot_tell"]},
            "pattern_class": {"type": "string", "enum": _class_enum(study)},
            "status": {"type": "string", "enum": ["done", "planned", "asked", "already_handled"]}})},
        "reminders": {"type": "array", "items": {"type": "string"}},
        "need_more": {"type": "array", "items": {"type": "string"}}})


def uncovered(statements: list[dict], checks: list[dict]) -> list[str]:
    """Statements the judge gave no check."""
    done = {c.get("statement") for c in checks}
    return [f"statement {n} (\"{s['text']}\") has no check" for n, s in enumerate(statements, 1) if n not in done]


REVIEW_SYSTEM = """You review a clinical-trial monitor's check of one site email before it is final.
You get the protocol sections the monitor read, the email split into numbered statements with their linked
protocol parts, and the monitor's checks and findings. Find what the monitor missed or got wrong:
- a statement not checked against a linked part that has a rule for it;
- a condition ("only if", "unless", "provided that", "for participants who") not checked;
- two passages giving different values for the same thing, not reported;
- a duration not compared with a time limit;
- a next step that does not cover what has already happened (drug taken, samples drawn, reports due);
- an ok check that needs an action or exceeds a limit;
- a finding the protocol does not support, that only says the email did not mention something, or that repeats
  another finding.
List each as one short, specific note naming the statement and the protocol text. Only raise a note that would
change a verdict, add a finding or remove one. Do not invent requirements, and do not ask for things the email
simply does not mention. If nothing needs changing, return no notes."""

REVIEW_SCHEMA = llm.obj({"notes": {"type": "array", "items": {"type": "string"}}})


def _statements(part: dict) -> str:
    return "\n".join(f"{n}. [{s['status']}] {s['text']} | linked: {', '.join(s['anchors']) or 'none'}"
                     for n, s in enumerate(part.get("statements", []), 1)) or "(none)"


def _classes_note(study: Study) -> str:
    return "\n".join(f"- {p['id']}: {p.get('description', p.get('label', ''))}" for p in study.pattern_classes)


def _context(items: list[dict], part: dict, keys: list[dict]) -> list[dict]:
    by_id = {i["id"]: i for i in items}
    wanted = [by_id[x] for x in part["visits"] + part["sections"] if x in by_id and _usable(by_id[x])]
    seen, lines, out = set(), set(), []
    for i in sorted(wanted + keys, key=lambda i: (i["kind"] == "visit", i["doc_id"], i["ord"])):
        if i["id"] in seen:
            continue
        seen.add(i["id"])
        if i["kind"] == "brief":
            kept = [ln for ln in i["text"].splitlines() if ln not in lines]
            lines.update(kept)
            i = dict(i, text="\n".join(kept))
        out.append(i)
    return out


def _judge_prompt(study: Study, part: dict, context: list[dict], items: list[dict], email: str, sent: datetime,
                  notes: list[str]) -> str:
    who = part["participant"] or "matters not about a particular participant"
    index = "\n".join(f"{i['id']} | {_heading(i)}" for i in items if _usable(i))
    briefs = ("A topic brief gives one topic's rules as short lines, each ending with its source section ids in "
              "brackets. For a finding, copy the brief line that governs it as protocol_quote and give one of its "
              "bracketed source section ids as section_id.\n\n") if any(i["kind"] == "brief" for i in context) else ""
    return (f"{briefs}PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(i) for i in context)
            + f"\n\nGUIDE INDEX (other parts you may ask for in need_more):\n{index}"
            + f"\n\nPROBLEM TYPES (pattern_class):\n{_classes_note(study)}"
            + f"\n\nEMAIL DATE: {sent:%A %d %B %Y}\n\nFACTS FROM CODE (dates, order, medicine lists):\n"
            + ("\n".join(f"- {n}" for n in notes) or "- none")
            + f"\n\nIN FOCUS: {who}. What the email says about it: {part['summary']}\n"
            + "Check only this participant (or, for general matters, only what is not about a participant).\n\n"
            + f"STATEMENTS:\n{_statements(part)}\n\nEMAIL:\n{email}")


def review(context: list[dict], part: dict, email: str, notes: list[str], res: dict, use_cache: bool) -> list[str]:
    user = ("PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(i) for i in context)
            + "\n\nFACTS FROM CODE (dates, order, medicine lists):\n" + ("\n".join(f"- {n}" for n in notes) or "- none")
            + f"\n\nSTATEMENTS:\n{_statements(part)}\n\nEMAIL:\n{email}\n\nMONITOR'S ANSWER:\n"
            + json.dumps({k: res[k] for k in ("checks", "findings")}, indent=1))
    return llm.structured("read_review", REVIEW_SYSTEM, user, REVIEW_SCHEMA, "review", use_cache=use_cache)["notes"]


def judge(study: Study, part: dict, context: list[dict], items: list[dict], email: str, sent: datetime,
          notes: list[str], use_cache: bool) -> tuple[dict, list[dict]]:
    """The judge's answer and the sections it finally read. One second round covers the parts it asked for,
    statements it left unchecked, numbers outside its own ranges, and the reviewer's notes."""
    user = _judge_prompt(study, part, context, items, email, sent, notes)
    res = llm.structured("read", _read_system(study), user, read_schema(study), "read", use_cache=use_cache)
    by_id = {i["id"]: i for i in items}
    have = {i["id"] for i in context}
    more = [by_id[x] for x in dict.fromkeys(res.get("need_more") or []) if x in by_id and x not in have
            and _usable(by_id[x])]
    slips = number_slips(res["checks"])
    gaps = uncovered(part.get("statements", []), res["checks"])
    checked = json.dumps(res["checks"]).lower()
    gaps += [f"the listed medicine {m.group(1)} has no check of its own against its list's rule"
             for n in notes if (m := re.match(r"(.+?) is listed in the protocol's", n))
             and re.sub(r"\s*\(.*", "", m.group(1)).lower() not in checked]
    notes_r = review(context, part, email, notes, res, use_cache) if guide.checker(study).get("review") else []
    if more or slips or gaps or notes_r:
        context = context + more
        fix = ("\n\nYOUR FIRST ANSWER:\n" + json.dumps(res, indent=1)
               + (f"\n\nSECTIONS YOU ASKED FOR are now included above: {', '.join(i['id'] for i in more)}."
                  if more else "")
               + ("\n\nCODE CHECK OF YOUR NUMBERS:\n" + "\n".join(f"- {x}" for x in slips) if slips else "")
               + ("\n\nUNCHECKED STATEMENTS:\n" + "\n".join(f"- {x}" for x in gaps) if gaps else "")
               + ("\n\nREVIEWER'S NOTES (check each against the protocol; act only where the protocol supports it):\n"
                  + "\n".join(f"- {x}" for x in notes_r) if notes_r else "")
               + "\n\nLook again and give your complete final checks and findings.")
        user = _judge_prompt(study, part, context, items, email, sent, notes) + fix
        res = llm.structured("read", _read_system(study), user, read_schema(study), "read", use_cache=use_cache)
    res.update(number_slips=slips, unchecked=gaps, review_notes=notes_r, need_more_added=[i["id"] for i in more])
    return res, context


# ---------------------------------------------------------------- 4. confirm

CONFIRM_SYSTEM = """You double-check one finding from a clinical-trial monitor before it is sent to the site.
You get the protocol sections the monitor read (the cited one first), the email, date arithmetic done by code, and
the finding. Read the cited protocol words in their full context, including any exception, alternative or
allowance elsewhere in the sections. Write your reasons first, then the verdict:
- confirmed: the protocol requires or forbids what the finding says, for this situation and period, and the email
  shows the site did, plans or asks to do otherwise.
- rejected: the protocol allows it, the requirement is not in the text, the finding misreads the email or the
  numbers, or it applies a rule for a different visit, period or kind of participant.
- unsure: the protocol is genuinely ambiguous on this point. A missing fact alone is not a reason to be unsure.
When a rule turns on a clinical judgement the protocol leaves to the site or medical monitor (for example whether a
listed medicine has a narrow therapeutic index), answer it from standard clinical knowledge where that is clear. The
judgement itself is required even if consulting the medical monitor "may" be done: going ahead with no sign anyone
made it, or acting more strictly than the rule (stopping a medicine it permits), confirms the finding.
Reject a finding that rests only on wording: a step named in short ("compliance" for accountability/compliance),
a person in a role they plainly belong to (study coordinators are clinical staff), or something the email simply
does not mention when it is not setting out the whole of a visit or process.
Reject a missing, late or failed step when the email meets an exception in the sections ("is not required as long
as", "will not be reported as protocol deviations", "may be reassessed", "only selected procedures"): do the
arithmetic of its condition. Reject a rule applied to a medicine, person, period or situation it does not name.
"Should consider" or "should be discussed" is a duty to weigh it: a plan that plainly goes past it (for example
resuming a dose later than the time it says to consider) with no sign it was weighed confirms the finding. Acting
more strictly than the protocol in a way that affects the participant or the record (stopping a permitted medicine,
logging a deviation that is not one) is a departure too.
Each consent covers its own part of the study: tests specific to a regimen or sub-study, randomization within it
and its dosing need that consent first, even though an earlier (master) consent was signed. Consent "before any
study procedures" applies to every consent, except what the protocol names as standard of care. A study may
randomize more than once (to a regimen or arm, later to drug or placebo); match each order rule to the step it names.
In protocol_words, copy exactly the protocol's own words (one or two sentences) that decide it, from a numbered
section or visit checklist, not from a topic brief or pack heading."""

CONFIRM_SCHEMA = llm.obj({
    "reasons": {"type": "string"},
    "protocol_words": {"type": "string"},
    "verdict": {"type": "string", "enum": ["confirmed", "rejected", "unsure"]}})


def _proof(f: dict, context: list[dict], sources: list[dict]) -> list[dict]:
    """What a finding is confirmed against. With topic briefs: the source sections its brief line cites (and any
    it names itself), with the briefs and visit checklists; otherwise what the judge read."""
    briefs = [i for i in context if i["kind"] == "brief"]
    if not briefs:
        return context
    by_id = {s["id"]: s for s in sources}
    cited = f["section_id"] + " " + f["protocol_quote"]
    found_ids = lambda s: [x.rstrip(".") for x in re.findall(r"[\w.-]+:[\w.-]+", s)]
    ids = found_ids(cited)
    lines = [ln for b in briefs for ln in b["text"].splitlines() if ln.startswith("- ")]
    if lines:
        best = max(lines, key=lambda ln: SequenceMatcher(None, _norm(ln), _norm(f["protocol_quote"])).ratio())
        ids += found_ids(best[best.rfind("["):])
        words = _words(" ".join(f[k] for k in ("problem", "why", "protocol_quote")))
        near = sorted(lines, key=lambda ln: -len(words & _words(ln[:ln.rfind("[")])) / (1 + len(_words(ln)) ** .5))
        extra: list[str] = []
        for ln in near:
            if len(extra) >= 4:
                break
            extra += [i for i in found_ids(ln[ln.rfind("["):]) if i not in ids + extra]
        ids += extra[:4]
    title_stems = {s["id"]: {w[:6] for w in _words(s["title"]) if len(w) > 4} for s in sources}
    common = Counter(w for t in title_stems.values() for w in t)
    stems = {w[:6] for w in _words(f["problem"]) if len(w) > 4 and common[w[:6]] <= 4}
    titled = sorted((s for s in sources if s["id"] not in ids and stems & title_stems[s["id"]]),
                    key=lambda s: -len(stems & title_stems[s["id"]]))
    ids += [s["id"] for s in titled[:3]]
    secs = [by_id[i] for i in dict.fromkeys(ids) if i in by_id]
    return secs + [i for i in context if i["kind"] == "visit"] + briefs


def _order_fact(f: dict, notes: list[str]) -> str | None:
    """The code's 'X is ... BEFORE <consent>' fact a finding is about (it names both steps), if any: such a finding
    goes out as 'please check' even if the second AI rejects it, since the order itself is worked out by code."""
    stems = lambda s: {w[:6] for w in _words(s) if len(w) > 4}
    said = stems(f["problem"] + " " + f["why"])
    for n in notes:
        m = re.match(r"(.+?) is [\d.]+ (?:day\(s\)|hours) BEFORE (.+)$", n)
        if m and stems(m.group(1)) & said and stems(m.group(2)) & said:
            return n
    return None


def _section_id(cited: str, context: list[dict]) -> str:
    """The cited section's id, tolerating brackets or a heading after it."""
    ids = [i["id"] for i in context]
    bare = cited.strip().strip("[]").split("]")[0].split()[0] if cited.strip() else ""
    return bare if bare in ids else next((i for i in ids if i in cited), cited)


def _find_quote(quote: str, context: list[dict], cited: str) -> tuple[dict, str] | None:
    order = sorted(context, key=lambda i: i["id"] != cited)
    for i in order:
        span = textutil.find_quote(quote, i["text"]) or ingest.repair_quote(quote, i["text"])
        if span:
            return i, i["text"][span[0]:span[1]]
    for i in order:
        pieces = textutil.quote_pieces(quote, i["text"])
        if pieces and sum(len(p) for p in pieces) >= 0.6 * len(quote):
            return i, " ... ".join(pieces)
    return None


_TAG = re.compile(r"\s*\[[\w.:-]+\]\s*$")
_OWN_TEXT = ("brief", "pack", "piece")  # written by the bot from the documents, so never quoted as the protocol


def _about(f: dict) -> set[str]:
    return _words(" ".join(f.get(k) or "" for k in ("problem", "why", "email_quote")))


def _fits(asked: str, got: str, about: set[str]) -> bool:
    """A match found for a rule sentence that can stand as its evidence: it keeps most of the sentence's numbers
    (a loose match can land on another visit's line, "Day 0" for "Day 28 ± 7"), is not a bare heading, and shares
    a word with the finding."""
    want, have = _nums(asked), _nums(got)
    return (len(want & have) * 2 >= len(want) and re.search(r"[a-z]{3}", got) is not None
            and bool(about & _words(got)))
_LIST_LINE = re.compile(r"^\s*(?:\d+[.)]|[a-z][.)]|[-•*])\s+|:\s*$")


def _quote_parts(quote: str) -> list[tuple[str, bool]]:
    """A quote's sentences, each with whether it is a list item or a list's lead-in ("... the following:")."""
    out = []
    for line in re.split(r"\n+|\s*(?:\.\.\.|…)\s*", quote or ""):
        line = _TAG.sub("", line.strip())
        cells = re.split(r"\s+\|\s+", line)
        if len(cells) >= 5:  # a wide table row: each cell is its own fact (one visit's column of the schedule)
            out += [(re.sub(r"^.*\bColumns:\s*", "", c).strip(), "cell") for c in cells if len(c.strip()) >= 8]
            continue
        listy = bool(_LIST_LINE.search(line))
        out += [(s.strip(), listy) for s in re.split(r"(?<=[.;!?])\s+(?=[A-Z(])", line) if len(s.strip()) >= 12]
    return out


def _nums(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def _exact_words(quote: str, proof: list[dict], sources: list[dict], cited: str, f: dict) -> tuple[dict, str] | None:
    """The protocol's own words behind a quote, sentence by sentence: a quote can join sentences from two places or
    carry a topic brief's tag, so each is looked up on its own. Kept: the sentences of the section that best fits the
    finding, less list lines that are not about it (a visit's procedure list after the visit's timing)."""
    ids = {i["id"] for i in proof}
    pool = [i for i in proof + [s for s in sources if s["id"] not in ids] if i["kind"] not in _OWN_TEXT]
    about = _about(f)
    about_nums = _nums(" ".join(f.get(k) or "" for k in ("problem", "why")))
    hits = []
    order = sorted(pool, key=lambda i: i["id"] != cited)
    for text, listy in _quote_parts(quote):
        exact = next(((i, i["text"][s[0]:s[1]]) for i in order if (s := textutil.find_quote(text, i["text"]))), None)
        h = exact or _find_quote(text, pool, cited)  # a loose match can land on a look-alike line elsewhere
        if h and _fits(text, h[1], about):
            hits.append((h[0], h[1], listy, len(about & _words(h[1])), len(about_nums & _nums(h[1]))))
    if not hits:
        return None
    best = max(hits, key=lambda h: (h[3] + h[4], not h[2]))
    keep = [h[1] for h in hits if h[0]["id"] == best[0]["id"]
            and (h is best or not h[2] or (h[2] is True and h[3] >= 2 and h[4]))]  # one table cell: the best
    if _nums(quote) and not any(_nums(quote) & _nums(k) for k in keep):
        return None  # the rule's numbers are not in what was found: it is about something else
    return best[0], " ... ".join(dict.fromkeys(keep))


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s or "").strip().lower()


_STOP = set("the a an and or of to in on for is are be was were with by at as that this it its not no from any "
            "before after must should will can may their they participant participants site".split())


def _words(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) > 2 and w not in _STOP}


def same_problem(a: dict, b: dict) -> bool:
    """Two findings about one departure: the same email words, or nearly the same description. Findings resting
    on different departures worked out by code are never one ("predose too early" and "postdose too soon")."""
    fa, fb = a.get("code_fact") or a.get("from_fact"), b.get("code_fact") or b.get("from_fact")
    if fa and fb and fa != fb:
        return False
    qa, qb = _norm(a["email_quote"]), _norm(b["email_quote"])
    if qa and qa == qb and a["section_id"] == b["section_id"]:
        return True
    pa, pb = _norm(a["problem"]), _norm(b["problem"])
    if set(re.findall(r"\d+", pa)) != set(re.findall(r"\d+", pb)) or len(_words(pa) ^ _words(pb)) > 4:
        return False
    return SequenceMatcher(None, pa, pb).ratio() >= 0.8


def _email_words(f: dict) -> str:
    """The email words to show for a finding: its own quote, plus any other words the code's arithmetic rested on
    (the dose time behind "postdose 9:20"), so the reader can check the numbers from the alert alone."""
    q = f["email_quote"]
    extra = [x for x in f.get("code_quotes") or [] if _norm(x) not in _norm(q)]
    return " ... ".join([q] + list(dict.fromkeys(extra)))


def _same_fact(a: dict, b: dict) -> bool:
    """Two findings resting on the same departure worked out by code (one date gap, one dose count) are one
    problem, however differently the AI worded them."""
    fa, fb = a.get("code_fact") or a.get("from_fact"), b.get("code_fact") or b.get("from_fact")
    return bool(a.get("code_fact") or b.get("code_fact")) and bool(fa) and fa == fb


def confirm(f: dict, context: list[dict], email: str, notes: list[str], use_cache: bool) -> dict:
    order = sorted(context, key=lambda i: i["id"] != f["section_id"])
    user = ("PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(i) for i in order)
            + "\n\nFACTS FROM CODE (dates, order, medicine lists):\n" + ("\n".join(f"- {n}" for n in notes) or "- none")
            + f"\n\nEMAIL:\n{email}\n\nFINDING:\n" + json.dumps(
                {k: f[k] for k in ("problem", "email_quote", "protocol_quote", "section_id", "why", "next_step")},
                indent=1))
    return llm.structured("read_confirm", CONFIRM_SYSTEM, user, CONFIRM_SCHEMA, "confirm", use_cache=use_cache)


def _window_conflict(f: dict, notes: list[str]) -> bool:
    """True when the finding says a visit is out of its window but the code's date arithmetic put that visit
    inside it: the code wins."""
    text = re.findall(r"\w+", (f["problem"] + " " + f["why"]).lower())
    if "window" not in text or f.get("code_fact"):
        return False
    if {"hour", "hours", "hr", "hrs", "minutes", "postdose", "predose"} & set(text):
        return False  # a sampling-time window in hours, not the visit's window in days
    inside = False
    for n in notes:
        m = re.search(r"(?:^|; )([^;]+?) window is Day [^;]*; this date is Day \d+: (inside the window)?", n)
        if m and all(w in text for w in re.findall(r"\w+", m.group(1).lower()) if w.isdigit() or len(w) > 3):
            if not m.group(2):
                return False
            inside = True
    return inside


DUTY_SYSTEM = """A clinical-trial monitor has confirmed one departure from the protocol in a site email. List every
follow-up action the protocol requires for this kind of departure that the email does not show done and the
finding's next step does not already name: recording or reporting it (and to whom), time limits, contacting the
medical monitor, repeating or replacing a sample, stopping, holding or resuming study drug, monitoring the
participant. Only duties the given sections state for this kind of departure; nothing from general knowledge, and
no general administrative steps (recording a deviation, entering it in a log, reporting to the IRB) unless the
section governing this departure itself names that step for it. For each, copy the protocol's own words that
require it (one sentence) and the section id. Return an empty list when the sections add nothing."""

DUTY_SCHEMA = llm.obj({"duties": {"type": "array", "items": llm.obj({
    "action": {"type": "string"}, "deadline": {"type": "string"},
    "protocol_words": {"type": "string"}, "section_id": {"type": "string"}})}})

_DUTY_TITLES = re.compile(r"overdos|medication error|pregnan|safety call|unscheduled|missed visits", re.I)


def duties(f: dict, proof: list[dict], sources: list[dict], email: str, use_cache: bool) -> list[str]:
    """Follow-up duties the protocol attaches to a confirmed departure, each verified against the protocol text."""
    have = {i["id"] for i in proof}
    secs = ([i for i in proof if i["kind"] == "section"]
            + [s for s in sources if _DUTY_TITLES.search(s["title"]) and s["id"] not in have and len(s["text"]) > 80])
    user = ("PROTOCOL SECTIONS:\n\n" + "\n\n".join(_block(i) for i in secs) + f"\n\nEMAIL:\n{email}\n\n"
            "CONFIRMED FINDING:\n" + json.dumps({k: f[k] for k in ("problem", "why", "next_step", "section_id")},
                                                indent=1))
    res = llm.structured("read_duties", DUTY_SYSTEM, user, DUTY_SCHEMA, "duties", use_cache=use_cache)
    out = []
    for d in res.get("duties", []):
        if d["action"] and _find_quote(d["protocol_words"], secs, d["section_id"]):
            deadline = d.get("deadline", "")
            if re.search(r"not specified|none|n/a|unspecified|no deadline", deadline, re.I):
                deadline = ""
            out.append(d["action"].rstrip(".") + (f" ({deadline})" if deadline else "") + f" [{d['section_id']}]")
    return out


MATHS_SYSTEM = """You prepare the arithmetic in a clinical-trial site email for checking by code. List every quantity
in the email, for one participant, that can be computed from numbers the email gives or that the study fixes
(the dose is two packets a day; a carton holds the number of packets the email or protocol states): counts
(cartons x packets per carton, days of supply = packets / packets per day, doses expected between two dates,
compliance = doses taken / doses expected), hours or minutes between two clock times (write times as minutes:
17:10 is 17*60+10), totals against a stated limit, days between dates. For each: what is being computed, a Python
arithmetic expression using only numbers, + - * / and parentheses, the unit, and the value the email itself gives
for it (empty if the email gives none). Leave out anything that needs facts the email does not give."""

MATHS_SCHEMA = llm.obj({"sums": {"type": "array", "items": llm.obj({
    "what": {"type": "string"}, "expression": {"type": "string"}, "unit": {"type": "string"},
    "email_says": {"type": "string"}})}})


def _safe_eval(expr: str) -> float | None:
    import ast
    import operator as op
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in ops:
            return ops[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError
    try:
        return ev(ast.parse(expr.strip(), mode="eval")) if len(expr) < 200 else None
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError):
        return None


def maths_notes(said: str, use_cache: bool) -> list[str]:
    """Code facts for the email's arithmetic: the AI writes each sum as an expression, code computes it and says
    whether the email's own figure agrees."""
    if not re.search(r"\d", said):
        return []
    res = llm.structured("read_maths", MATHS_SYSTEM, f"EMAIL, this participant:\n{said}", MATHS_SCHEMA, "maths",
                         use_cache=use_cache)
    out = []
    for s in res.get("sums", [])[:12]:
        v = _safe_eval(s["expression"])
        if v is None:
            continue
        shown = f"{v:g}" if abs(v - round(v)) > 1e-9 else f"{int(round(v))}"
        if s["unit"].lower().startswith("minute") and v >= 60:
            shown += f" ({int(v // 60)} h {int(round(v % 60))} min)"
        line = f"{s['what']}: {s['expression']} = {shown} {s['unit']}".rstrip()
        nums = re.findall(r"\d+(?:\.\d+)?", s.get("email_says") or "")
        if nums:
            agree = any(abs(float(x) - v) < 0.01 or abs(float(x) / 100 - v) < 0.005 for x in nums)
            line += f"; the email says {s['email_says']}" + ("" if agree else " (DIFFERS from the code's result)")
        out.append(line)
    return out


ROLL_SYSTEM = """You compare one visit's protocol checklist with what a site email says about that visit for one
participant. For each required item, and each sub-item a footnote names (for example the tests in the safety
laboratory set), give its status: done (the email says or plainly implies it was done), not_done (the email says it
was skipped, omitted, failed, not collected or not done), unknown (not mentioned). Also say whether the email sets
out the visit as complete or lists what was done ("all done", "completed: ...", a list of the visit's procedures)."""

ROLL_SCHEMA = llm.obj({
    "items": {"type": "array", "items": llm.obj({"item": {"type": "string"},
                                                 "status": {"type": "string", "enum": ["done", "not_done", "unknown"]}})},
    "sets_out_whole_visit": {"type": "boolean"}})


def roll_call(part: dict, visits: dict, said: str, use_cache: bool) -> list[str]:
    """A code fact per visit: which checklist items the email says were not done, and which it does not mention,
    so a list that names one missing item does not hide the others."""
    out = []
    for vid in part.get("visits", []):
        v = visits.get(vid)
        if not v:
            continue
        res = llm.structured("read_roll", ROLL_SYSTEM, f"VISIT CHECKLIST:\n{v['text']}\n\nEMAIL, this participant:\n{said}",
                             ROLL_SCHEMA, "roll_call", use_cache=use_cache)
        not_done = [i["item"] for i in res["items"] if i["status"] == "not_done"]
        if not_done:  # items merely not mentioned are not passed on: silence is not a finding
            out.append(f"{v['title']} checklist: the email says these required items were NOT done: "
                       f"{'; '.join(not_done)}. Check each against the visit's rules and exceptions; name every "
                       f"missing item that has no exception.")
    return out


FACT_SYSTEM = """You are an experienced clinical-trial monitor. You check ONE fact from a site email against the
protocol rules given (each rule line ends with its source section ids in brackets; visit checklists list what a
visit requires). The whole email is given only so you know what else it says; judge this one fact.
Decide whether this fact, as the email states it, departs from any rule given: done or planned wrongly, at the
wrong time or outside a limit (use the code's numbers), against a condition ("only if", "within", "unless"),
by the wrong person, forbidden, or more than the protocol allows. Also: when this fact is an event the protocol
attaches duties to (an overdose or extra dose, a positive pregnancy test, a dose change or interruption, a missed
or late dose or visit, a failed or invalid sample, a temperature excursion, an adverse event), check each duty the
rules state for it (report, contact the medical monitor and its time limit, an extra or replacement sample, stop or
hold the drug, record a deviation where the rule says so) against the whole email; each duty the email shows not
done, or a plan that goes against it, is a finding.
Each departure is its own finding. Copy the governing rule line as protocol_quote and one of its bracketed ids as
section_id. A routine item the email simply does not mention is not a finding. If nothing departs, return no
findings."""


MERGE_SYSTEM = """You get findings about one participant from a check of a site email. Group the findings that
describe the same departure (the same act, omission or plan), however they are worded. Findings about different
medicines, samples, visits, doses, people or duties are different departures, even under the same rule. Every
finding number appears in exactly one group."""

MERGE_SCHEMA = llm.obj({"groups": {"type": "array", "items": llm.obj({
    "members": {"type": "array", "items": {"type": "integer"}}})}})


def duplicates(findings: list[dict], use_cache: bool) -> dict[int, int]:
    """Group number of each finding that shares a departure with another (the judge and the fact checks often
    find one departure twice)."""
    if len(findings) < 2:
        return {}
    listing = "\n".join(f"{n}. {f['problem']} | email: \"{f['email_quote']}\" | rule: {f['section_id']}"
                        + (f" | filed under: {f['participant']}" if f.get("participant") else "")
                        for n, f in enumerate(findings))
    res = llm.structured("read_merge", MERGE_SYSTEM, f"FINDINGS:\n{listing}", MERGE_SCHEMA, "merge",
                         use_cache=use_cache)
    group: dict[int, int] = {}
    for n, g in enumerate(res["groups"]):
        members = {m for m in g["members"] if 0 <= m < len(findings)}
        if len(members) > 1:
            group.update({m: n for m in members if m not in group})
    return group


def _fact_schema(study: Study) -> dict:
    return llm.obj({"findings": read_schema(study)["properties"]["findings"]})


def _fact_lines(fact: str, anchors: list[str], items: list[dict], limit: int = 40) -> list[str]:
    """The brief lines most likely to govern one fact: lines of its anchored briefs, then of every brief, ranked by
    the words they share with it."""
    words = _words(fact)
    by_id = {i["id"]: i for i in items}
    anchored = [by_id[a] for a in anchors if a in by_id and by_id[a]["kind"] == "brief"]
    briefs = [i for i in items if i["kind"] == "brief"]

    def ranked(src: list[dict]) -> list[str]:
        lines = [ln for b in src for ln in b["text"].splitlines() if ln.startswith("- ")]
        scored = sorted(((len(words & _words(ln)), ln) for ln in lines), key=lambda x: -x[0])
        return [ln for s, ln in scored if s > 0]
    out = list(dict.fromkeys(ranked(anchored)[:limit // 2 + 10] + ranked(briefs)[:limit]))
    return out[:limit + 10]


def fact_checks(study: Study, part: dict, items: list[dict], notes: list[str], email: str, sent: datetime,
                use_cache: bool) -> list[dict]:
    """One small check per statement, so a second or third departure in an email is not lost behind the first."""
    from concurrent.futures import ThreadPoolExecutor
    by_id = {i["id"]: i for i in items}
    schema = _fact_schema(study)

    def one(s: dict) -> list[dict]:
        visits = [by_id[a] for a in s["anchors"] if a in by_id and by_id[a]["kind"] == "visit"]
        words = _words(s["text"])
        facts = [n for n in notes if words & _words(n)] or notes[:10]
        user = ("PROTOCOL RULES:\n" + "\n".join(_fact_lines(s["text"], s["anchors"], items))
                + ("\n\nVISIT CHECKLISTS:\n" + "\n\n".join(_block(v) for v in visits) if visits else "")
                + f"\n\nFACTS FROM CODE:\n" + ("\n".join(f"- {n}" for n in facts) or "- none")
                + f"\n\nPROBLEM TYPES (pattern_class):\n{_classes_note(study)}"
                + f"\n\nEMAIL DATE: {sent:%A %d %B %Y}\nPARTICIPANT: {part['participant'] or '(none)'}"
                + f"\n\nTHE FACT TO CHECK [{s['status']}]: {s['text']}\n\nWHOLE EMAIL:\n{email}")
        res = llm.structured("read_fact", FACT_SYSTEM, user, schema, "fact_check", use_cache=use_cache)
        return [dict(f, from_fact=s["text"]) for f in res["findings"]]
    stmts = part.get("statements", [])
    if not stmts:
        return []
    with ThreadPoolExecutor(min(8, len(stmts))) as ex:
        return [f for fs in ex.map(one, stmts) for f in fs]


def _related(result: dict, f: dict) -> bool:
    """Does a finding concern the event (cites a step's section, or shares a word with the event's name)?"""
    if f["section_id"] in {st["section_id"] for st in result["missing"]}:
        return True
    text = _words(f["problem"] + " " + f["why"])
    return any(any(t.startswith(w[:6]) for t in text) for w in _words(result["event"]))


_DUTY_CLASS = [("visit|termination|withdraw|deviation", "visit_schedule"), ("consent", "consent"),
               ("sample|csf|pk", "sample_handling"), ("", "study_drug")]


def _attach_duties(study: Study, part: dict, said: str, notes: list[str], email: str, found: list[Finding],
                   metas: list, start: int, use_cache: bool) -> list[dict]:
    """The follow-up table, checked by code: steps the email leaves unaddressed after an event it reports are
    added to the finding about that event; with no such finding they become one finding of their own."""
    from . import duties as table
    results = table.check(study, said + "\n" + "\n".join(notes), email, part["participant"] or "", use_cache,
                          medicine=any("is listed in the protocol" in n for n in notes),
                          departure=len(found) > start,
                          facts=[n for n in notes if "listed in the protocol" in n or "window" in n
                                 or "dose" in n.lower()][:12])
    for r in results:
        if not r["acted"]:
            continue
        lines = [table.step_line(st) for st in r["missing"]]
        add = " Also required: " + "; ".join(lines) + "."
        owners = [n for n in range(start, len(found)) if _related(r, metas[n][0])]
        if "deviation" in r["event"].lower():
            # "record / report the deviation" steps only ever ride on the finding that is the deviation
            owners = owners or list(range(start, len(found)))
            if not owners:
                r["attached_to"] = []
                continue
        r["attached_to"] = [metas[n][0]["problem"] for n in owners]
        if owners:
            fo = found[owners[0]]
            fo.reasoning = fo.reasoning.rstrip(". ") + "." + add
            fo.extra["next_step"] = (fo.extra.get("next_step") or "").rstrip(". ") + "." + add
            continue
        pclass = next(c for pat, c in _DUTY_CLASS if re.search(pat, r["event"], re.I))
        st = r["missing"][0]
        sec = next((i for i in guide.load(study.study_id) if i["id"] == st["section_id"]), None) or {
            "id": st["section_id"], "doc_id": guide.documents(study)[0], "title": "", "number": "", "text": "",
            "kind": "section", "path": ""}
        pid = part["participant"] or None
        problem = f"{r['event'].capitalize()}: the email does not show the required follow-up."
        next_step = "Required: " + "; ".join(lines) + "."
        found.append(Finding(
            rule={"id": sec["id"], "pattern_class": pclass, "doc_id": sec["doc_id"],
                  "text": _heading(sec).strip() or r["event"], "check_type": "judgment"},
            event={"participant_id": pid, "event_type": "other", "description": r["event"],
                   "quote": r["present_note"][:300], "temporality": "current", "event_datetime": None,
                   "polarity": "problem", "source": "body", "facts": []},
            method="judgment", message_quote=r["present_note"][:300],
            reasoning=f"The email reports {r['event']} ({r['present_note']}). {next_step}", certainty="clear",
            doc_quote=st["protocol_words"], doc_passage=sec["text"][:2000], confirmation="follow-up table",
            extra={"headline": problem, "next_step": next_step, "severity": "major",
                   "problem_key": hashlib.sha1(f"duty|{r['event']}|{pid}".encode()).hexdigest()[:10],
                   "unsure_reason": None}))
        metas.append(({"problem": problem, "why": next_step, "email_quote": r["present_note"][:300],
                       "section_id": sec["id"], "participant": pid or ""}, {"outcome": "clear", "duty": True}))
    return results


# ---------------------------------------------------------------- the whole check

def _code_checks(study: Study, rules: list[dict], vocab: list[dict], part: dict, email: str, sent: datetime,
                 notes: list[str], res: dict, context: list[dict], sources: list[dict], use_cache: bool) -> dict:
    """The check list's countable rules, run by code on facts pulled from the email. A departure the AI already
    reported (same email words) only adds its fact for the confirm step; any other becomes a finding of its own.
    Rules the facts pass are handed to the confirm step too, so a wrong timing finding can be turned down."""
    from . import checks
    facts = checks.extract(study, rules, vocab, email, part["participant"] or "", sent, use_cache)
    bad, ok = checks.evaluate(rules, facts)
    ai = [f for f in res["findings"] if f["status"] != "already_handled"]
    added = []
    cfs = [checks.finding(d) for d in bad]
    score = {(i, j): checks.overlaps(cf, f) for i, cf in enumerate(cfs) for j, f in enumerate(ai)}
    best = {j: max(range(len(cfs)), key=lambda i: score[i, j]) for j in range(len(ai))} if cfs else {}
    for i, (d, cf) in enumerate(zip(bad, cfs)):
        notes.append(f"code check, PROBLEM: {d['fact']}")
        same = [f for j, f in enumerate(ai) if score[i, j] and best[j] == i]
        for f in same:
            f["code_fact"] = d["fact"]
            f["code_quotes"] = d["quotes"]
            f["code_rule_quote"] = d["rule"]["quote"]
        if same:
            continue
        res["findings"].append(cf)
        added.append(cf["problem"])
        if cf["section_id"] not in {i["id"] for i in context}:
            context += [s for s in sources if s["id"] == cf["section_id"]]
    notes += [f"code check, within the limit: {d['fact']}" for d in ok if d["rule"]["kind"] == "interval"][:10]
    return {"facts": facts, "problems": [d["fact"] for d in bad], "passed": [d["fact"] for d in ok],
            "added": added}


def check(study: Study, msg: dict, email: str, *, use_cache: bool = True, log=None) -> tuple[list[Finding], dict]:
    """Findings for one email, and a trace of what was read and decided (for the audit log and tests)."""
    every = guide.load(study.study_id)
    if not every:
        raise RuntimeError(f"{study.study_id}: no guide; run `cormon guide --study {study.study_id}`")
    items = view(study, every)
    sources = [i for i in every if i["kind"] == "section"]
    sent = _email_date(msg)
    keys = key_sections(study, items)
    meds = medicine_lists(sources)
    therapeutic_index(meds, use_cache)
    trace: dict = {"parts": []}
    out: list[Finding] = []
    meta: list[tuple[dict, dict]] = []
    ai_merge = bool(guide.checker(study).get("merge"))  # off by default: code's same_problem is enough
    code_rules, vocab = [], []
    if guide.checker(study).get("code_checks"):
        from . import checks
        rules, vocab = checks.load(study)
        code_rules = checks.live(rules)
    for part in scope(study, items, email, sent, use_cache):
        notes = date_notes(part["dates"], sent, {i["id"]: i for i in items if i["kind"] == "visit"})
        said = part["summary"] + "\n" + "\n".join(s["text"] for s in part.get("statements", []))
        start = len(out)
        med_notes, med_secs = medicine_notes(said, meds)
        over_notes, over_secs = dose_notes(said, sources)
        if any("more than one dose in a day" in n for n in notes) and not over_notes:
            over_notes, over_secs = dose_notes("second dose", sources)
        notes += med_notes + over_notes
        if guide.checker(study).get("roll_call"):  # off by default: raised false alarms on items merely not named
            notes += roll_call(part, {i["id"]: i for i in items if i["kind"] == "visit"}, said, use_cache)
        notes += maths_notes(said, use_cache)
        med_secs += over_secs
        context = _context(items, part, keys)
        context += [s for s in sources if s["id"] in med_secs and s["id"] not in {i["id"] for i in context}]
        res, context = judge(study, part, context, items, email, sent, notes, use_cache)
        if guide.checker(study).get("fact_checks"):  # off by default: more alerts, mostly false, on 100 emails
            more = fact_checks(study, part, items, notes, email, sent, use_cache)
            res["findings"] = res["findings"] + more
            have = {i["id"] for i in context}
            ids = {x.rstrip(".") for f in more for x in re.findall(r"[\w.-]+:[\w.-]+", f["section_id"] + " "
                                                                     + f["protocol_quote"])}
            context = context + [s for s in sources if s["id"] in ids and s["id"] not in have]
        code: dict = {}
        if code_rules:
            code = _code_checks(study, code_rules, vocab, part, email, sent, notes, res, context, sources, use_cache)
        pt = {"participant": part["participant"], "visits": part["visits"], "sections": part["sections"],
              "statements": part.get("statements", []), "date_notes": notes,
              "tokens_in": sum(len(i["text"]) for i in context) // 4,
              "checks": res["checks"], "reminders": res["reminders"],
              **{k: res[k] for k in ("number_slips", "unchecked", "review_notes", "need_more_added")},
              "code_checks": code, "findings": []}
        trace["parts"].append(pt)
        kept: list[dict] = []
        live = [f for f in res["findings"] if f["status"] != "already_handled"]
        group = {id(live[n]): g for n, g in duplicates(live, use_cache).items()} if ai_merge else {}
        reported: set[int] = set()
        for f in res["findings"]:
            g = group.get(id(f))
            repeat = g is not None and g in reported
            f = dict(f, section_id=_section_id(f["section_id"], context))
            rec = dict(f)
            pt["findings"].append(rec)
            if f["status"] == "already_handled":
                rec["outcome"] = "already_handled"
                continue
            if repeat or any(same_problem(f, k) or _same_fact(f, k) for k in kept):
                rec["outcome"] = "duplicate"
                continue
            doubt = _window_conflict(f, notes)  # sent as "please check", not dropped: the match can pick the wrong date
            rec["window_conflict"] = doubt
            proof = _proof(f, context, sources)
            words = [i for i in proof if i["kind"] not in ("brief", "pack", "piece")] or proof
            c = confirm(f, proof, email, notes, use_cache)
            found = (_find_quote(f["protocol_quote"], words, f["section_id"])
                     or (c["protocol_words"] and _find_quote(c["protocol_words"], words, f["section_id"])))
            if found and (found[0]["kind"] in _OWN_TEXT or not _fits(f["protocol_quote"], found[1], _about(f))):
                found = None
            found = ((f.get("code_rule_quote") and _exact_words(f["code_rule_quote"], proof, sources, "", f))
                     or _exact_words(found[1] if found else c["protocol_words"], proof, sources, f["section_id"], f)
                     or (not found and _exact_words(f["protocol_quote"], proof, sources, f["section_id"], f))
                     or found)
            rec["quote_found"] = bool(found)
            rec["proof"] = [i["id"] for i in proof if i["kind"] == "section"]
            rec["confirm"] = c
            if c["verdict"] == "rejected" and (f.get("code_fact") or _order_fact(f, notes)):
                rec["kept_by_order_fact"] = True  # the numbers behind it were worked out by code
            elif (c["verdict"] == "rejected" and found and not f.get("from_fact")
                  and guide.checker(study).get("disputed", "check") == "check"):
                rec["disputed"] = True
            elif c["verdict"] == "rejected":
                rec["outcome"] = "rejected"
                continue
            certainty = "clear" if c["verdict"] == "confirmed" and found and not doubt else "unsure"
            rec["outcome"] = certainty
            kept.append(f)
            if g is not None:
                reported.add(g)
            if c["verdict"] == "confirmed" and guide.checker(study).get("duties"):  # off by default, see roll_call
                more_duties = duties(f, proof, sources, email, use_cache)
                rec["duties"] = more_duties
                if more_duties:
                    f = dict(f, next_step=f["next_step"].rstrip(". ") + ". Also required: "
                             + "; ".join(more_duties) + ".")
            sec, quote = found if found else (next((i for i in proof if i["id"] == f["section_id"]), None),
                                              _TAG.sub("", f["protocol_quote"]))
            if sec and sec["kind"] in _OWN_TEXT:
                hit = _find_quote(quote, [s for s in sources if s["kind"] not in _OWN_TEXT], "")
                if hit and _fits(quote, hit[1], _about(f)):
                    sec, quote = found = hit
            if not found:
                quote = ""  # unproven words are not shown as the protocol's: the alert points to the section
            sec = sec or {"id": f["section_id"], "doc_id": guide.documents(study)[0], "title": "", "number": "",
                          "text": "", "kind": "section", "path": ""}
            pid = part["participant"] or None
            event = {"participant_id": pid, "event_type": "other", "description": f["problem"],
                     "quote": f["email_quote"], "temporality": "current" if f["status"] == "done" else "planned",
                     "event_datetime": None, "polarity": "problem", "source": "body", "facts": []}
            rule = {"id": sec["id"], "pattern_class": f["pattern_class"], "doc_id": sec["doc_id"],
                    "text": f"{_heading(sec)}".strip() or f["problem"], "check_type": "judgment"}
            meta.append((dict(f, participant=pid or ""), rec))
            out.append(Finding(
                rule=rule, event=event, method="judgment", message_quote=_email_words(f),
                reasoning=f"{f['why']} Next step: {f['next_step']}", certainty=certainty, doc_quote=quote,
                doc_passage=sec["text"][:2000], confirmation=c["reasons"],
                extra={"headline": f["problem"], "next_step": f["next_step"], "severity": f["severity"],
                       "problem_key": hashlib.sha1(f"{_norm(f['email_quote'])}|{sec['id']}".encode()).hexdigest()[:10],
                       "unsure_reason": None if certainty == "clear" else
                       ("the code's date check put a visit date mentioned here inside its window" if doubt else
                        c["reasons"] if c["verdict"] == "unsure" else
                        f["why"] if f["certainty"] == "cannot_tell" else
                        "the exact words were not found in the protocol")}))
        if guide.checker(study).get("duty_table"):  # off until it clears its pass mark: 40.5/49 and 11 false alarms
            pt["duties"] = _attach_duties(study, part, said, notes, email, out, meta, start, use_cache)
        if log:
            log(f"reader {part['participant'] or '-'}: {len(context)} sections, {pt['tokens_in']} tokens, "
                f"{len(res['findings'])} findings")
    # every part reads the whole email, so two parts can report the same departure
    order = sorted(range(len(out)), key=lambda n: out[n].certainty != "clear")
    group = duplicates([meta[n][0] for n in order], use_cache) if ai_merge else {}
    seen: set[int] = set()
    drop: set[int] = set()
    for pos, n in enumerate(order):
        g = group.get(pos)
        if (g is not None and g in seen) or any(same_problem(meta[n][0], meta[k][0]) or _same_fact(meta[n][0], meta[k][0])
                                                for k in order[:pos] if k not in drop):
            drop.add(n)
            meta[n][1]["outcome"] = "duplicate"
        elif g is not None:
            seen.add(g)
    return [x for n, x in enumerate(out) if n not in drop], trace

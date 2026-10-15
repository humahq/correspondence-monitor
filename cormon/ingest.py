"""Turns study documents into atomic, source-quoted rules (the study model).

Each rule is one include/exclude-style statement with a verbatim quote from
the original document. A quote that cannot be found in the document makes the
rule a defect: it is kept for the record but marked inactive.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import db, llm, textutil
from .config import Study

EVENT_TYPES = [
    "screening", "enrollment", "randomization", "consent", "visit", "contact",
    "dosing", "imaging", "sample_collection", "sample_shipment", "other",
]

OPERATORS = ["lt", "le", "gt", "ge", "eq", "ne", "outside", "between"]


# ---------------------------------------------------------------- documents

def read_pages(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        pages = [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
        scanned = ocr_pages(path)
        return [scanned.get(i, t) if len(t.strip()) < 20 else t for i, t in enumerate(pages, start=1)]
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".md":
        parts = re.split(r"(?m)^(?=## )", text)
        return [p for p in parts if p.strip()]
    return [text]


def ocr_pages(pdf: Path) -> dict[int, str]:
    """Text of a scanned PDF, read once by bin/ocr-pdf into <file>.ocr.txt; empty if there is none."""
    side = pdf.with_suffix(".ocr.txt")
    if not side.exists():
        return {}
    parts = re.split(r"(?m)^=== PAGE (\d+)\n", side.read_text(encoding="utf-8"))
    return {int(n): t for n, t in zip(parts[1::2], parts[2::2])}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256(path.read_bytes())
    side = path.with_suffix(".ocr.txt")
    if path.suffix.lower() == ".pdf" and side.exists():
        h.update(side.read_bytes())
    return h.hexdigest()


def store_document(study: Study, doc_id: str) -> tuple[str, list[str], bool]:
    """Store a document's pages; returns (sha, pages, changed)."""
    path = study.doc_path(doc_id)
    sha = sha256_file(path)
    row = db.q1("SELECT sha256 FROM documents WHERE study_id=? AND doc_id=?", (study.study_id, doc_id))
    if row and row["sha256"] == sha:
        pages = [r["text"] for r in db.q(
            "SELECT text FROM doc_pages WHERE study_id=? AND doc_id=? ORDER BY page",
            (study.study_id, doc_id))]
        return sha, pages, False
    pages = read_pages(path)
    db.ex("DELETE FROM doc_pages WHERE study_id=? AND doc_id=?", (study.study_id, doc_id))
    for i, t in enumerate(pages, start=1):
        db.ex("INSERT INTO doc_pages(study_id, doc_id, page, text) VALUES (?,?,?,?)",
              (study.study_id, doc_id, i, t))
    db.ex(
        "INSERT INTO documents(study_id, doc_id, path, sha256, pages, ingested_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(study_id, doc_id) DO UPDATE SET path=excluded.path, sha256=excluded.sha256,"
        " pages=excluded.pages, ingested_at=excluded.ingested_at",
        (study.study_id, doc_id, str(path), sha, len(pages), db.now()),
    )
    db.audit("document_stored", entity_type="document", entity_id=f"{study.study_id}/{doc_id}",
             study_id=study.study_id, sha256=sha, pages=len(pages), previous=row["sha256"] if row else None)
    return sha, pages, True


def doc_text(study_id: str, doc_id: str) -> tuple[str, list[int]]:
    """Full text of a stored document and the start offset of each page."""
    rows = db.q("SELECT page, text FROM doc_pages WHERE study_id=? AND doc_id=? ORDER BY page",
                (study_id, doc_id))
    text, starts = "", []
    for r in rows:
        starts.append(len(text))
        text += r["text"] + "\n\n"
    return text, starts


def page_of(starts: list[int], pos: int) -> int:
    page = 1
    for i, s in enumerate(starts, start=1):
        if s <= pos:
            page = i
    return page


def best_span(quote: str, text: str, starts: list[int], section: str | None) -> tuple[int, int] | None:
    """Pick the occurrence of a quote that sits in the rule's own section when it appears more than once."""
    spans = textutil.find_all_quotes(quote, text)
    if len(spans) <= 1 or not section:
        return spans[0] if spans else None
    m = re.match(r"\s*(\d+(?:\.\d+)*)", section)
    key = textutil.normalize(m.group(1) if m else section)
    for span in spans:
        page = page_of(starts, span[0])
        page_text = text[starts[page - 1]: starts[page] if page < len(starts) else len(text)]
        prev_text = text[starts[page - 2]: starts[page - 1]] if page >= 2 else ""
        if key and (key in textutil.normalize(page_text) or key in textutil.normalize(prev_text)):
            return span
    return spans[-1]


def locate(study_id: str, doc_id: str, quote: str, section: str | None = None) -> dict | None:
    """Find a quote in the original document; return page and surrounding passage."""
    text, starts = doc_text(study_id, doc_id)
    span = best_span(quote, text, starts, section)
    if not span:
        return None
    return {
        "page": page_of(starts, span[0]),
        "exact": textutil.clean_spaces(text[span[0]:span[1]]),
        "passage": textutil.passage_around(text, span[0], span[1]),
    }


# ---------------------------------------------------------------- rules

TEST = llm.obj({
    "parameter": {"type": "string"},
    "operator": {"type": "string", "enum": OPERATORS},
    "value": {"type": "number"},
    "value2": llm.nullable("number"),
})

RULE_ITEM = llm.obj({
    "kind": {"type": "string", "enum": [
        "inclusion", "exclusion", "requirement", "prohibition", "window", "sequence"]},
    "text": {"type": "string"},
    "check_type": {"type": "string", "enum": ["numeric", "judgment"]},
    "parameter": llm.nullable("string"),
    "operator": {"type": ["string", "null"], "enum": OPERATORS + [None]},
    "value": llm.nullable("number"),
    "value2": llm.nullable("number"),
    "unit": llm.nullable("string"),
    "condition": llm.nullable(TEST),
    "unless": {"type": "array", "items": TEST},
    "applies_to": {"type": "array", "items": {"type": "string", "enum": EVENT_TYPES}},
    "source_quote": {"type": "string"},
    "section": {"type": "string"},
})

RULE_SCHEMA = llm.obj({"rules": {"type": "array", "items": RULE_ITEM}})

RULE_SYSTEM = """You convert clinical trial documents into atomic, checkable rules.

Write rules, not narrative. Each rule is ONE condition, phrased as an instruction a checker can apply to a single piece of correspondence between site staff, monitors and the sponsor, e.g.
  "Exclude if eGFR is below 45 mL/min at screening."
  "Plasma tubes must be centrifuged within 60 minutes of the blood draw."
  "Study drug must not start before all brain imaging is complete."
Split compound criteria into separate rules. Keep the document's order.

Only extract rules that belong to the requested problem class. Do not invent rules, thresholds or units that the document does not state. Ignore background, rationale, statistics, and anything about assessing safety or adverse events.

Only extract rules a site's correspondence could show being broken. Never extract:
- signature blocks, form fields, dates to fill in, copies to hand out;
- statements addressed to the participant that set no requirement on the site ("you will have the opportunity to ask questions");
- regulatory, IRB, sponsor or administrative duties that would never appear in site correspondence;
- descriptions of what the study is, or what will happen in general;
- how study drug is made, formulated, packed or blinded (what a capsule contains, how the placebo is matched);
- what MAY be done ("may be performed", "may be assessed"), and analyses a laboratory does after the samples leave the site;
- rows of the schedule table copied as they are: write the requirement the row states instead.

An approximate amount ("approximately 2 weeks", "~5 days") is not an exact limit: never write eq or ne for it. Write a window only when the document gives a range or tolerance, else a judgment rule.

Each requirement belongs to ONE problem class. The other problem classes of the study are listed with the document; a requirement that one of them names more directly goes there, not here.

Numbers, ranges and alternatives:
- When a participant needs only ONE of several listed options ("at least one of the following", "one of the following", "any of"), write ONE judgment rule for the whole group, broken only when none of the options holds. Never write a rule for one option on its own: missing one option is no problem when another holds.
- When one criterion gives two limits on the same value ("> 180 mmHg or < 100 mmHg"), keep both: an outside rule for the allowed range, or one rule per limit.
- A requirement that only applies in a stated situation (a fallback: "if the visit cannot occur in the morning", "if dosing has already occurred") must state that situation in its text and condition, so it is never checked when the usual case happened.
- A time window that belongs to one visit or step must name it in the parameter (days_visit3_after_visit2, not days_from_previous_visit), so it is never applied to other visits. When the document states different windows for the same visit in different places, write one rule with the widest window it states, and name the other statements in the text. When the document does not say what a day number counts from ("Visit 4: Day 21" in a table), do not guess: write a judgment rule that quotes it.
- When the document allows ALTERNATIVE ranges (e.g. early MCI OR late MCI), a value is acceptable if it fits ANY alternative. Write one numeric rule that flags only values that fit NONE of them, e.g. allowed 9-11 or <=8 for 16+ years of education becomes "violation when > 11".
- When a limit depends on another value (education, age, sex), write one rule per band, with the band in condition (use between value..value2 for a band such as 8-15 years). Bands must not overlap.
- Never write two numeric rules on the same parameter and the same band that disagree.

For every rule:
- source_quote: copy a contiguous passage VERBATIM from the document (10 to 60 words) that states the rule. Copy characters exactly as they appear, including odd spacing and line breaks. Never paraphrase in this field.
- check_type "numeric" only when the rule is a threshold on one measurable value; then set:
    parameter: a snake_case name with the unit, e.g. egfr_ml_min, hba1c_percent, mmse_score, bmi_kg_m2, age_years, systolic_bp_mmhg, diastolic_bp_mmhg, gds_score, minutes_draw_to_centrifuge, hours_draw_to_freeze, hours_stored_minus20, fasting_hours
    operator/value/value2: the values that VIOLATE the rule (lt, le, gt, ge, eq, ne, outside value..value2 for an allowed range, between value..value2 for a forbidden range).
    condition: the band in which the rule applies (e.g. education_years gt 8, or education_years between 8..15), else null.
    unless: when the document gives an exception that allows a value the rule would otherwise flag, the tests that together make the exception (all must hold), e.g. "CDR = 0.5 ... the CDR can be 0 if the memory box score is 0.5" is cdr_score ne 0.5 unless [cdr_score eq 0, cdr_memory_box_score ge 0.5]. Read the sentences after each requirement for "can be", "unless", "except", "however". Else an empty list.
  Otherwise check_type "judgment" and the numeric fields are null.
- applies_to: the event types the rule should be checked against.
- section: the section number and heading the rule comes from."""

DROP_REASONS = ["not_checkable", "not_stated", "other_class", "duplicate", "wrong_numbers", "not_atomic"]

REVIEW_SCHEMA = llm.obj({
    "drop": {"type": "array", "items": llm.obj({
        "rule_number": {"type": "integer"},
        "why": {"type": "string", "enum": DROP_REASONS},
        "duplicate_of": llm.nullable("integer"),
        "reason": {"type": "string"},
    })},
    "add": {"type": "array", "items": RULE_ITEM},
    "contradictions": {"type": "array", "items": llm.obj({
        "rule_numbers": {"type": "array", "items": {"type": "integer"}},
        "explanation": {"type": "string"},
    })},
})

REVIEW_SYSTEM = """You audit a list of rules that was extracted from a clinical trial document for one problem class. You are the second reader: find what the first reader got wrong.

Check the list against the DOCUMENT TEXT and return:
- drop: rules that should not be in the list, with why and a short reason:
    not_checkable: lines of a blank form (signature, print name, date), statements to the participant that set no duty on the site, or duties of the IRB or sponsor that a site never writes about. Site duties to record, document, notify, label or include something ARE checkable: an email can show them missed. Keep them;
    not_stated: the document does not say it. "Should" in a protocol is a duty of the site like "must"; a rule is never not_stated because the document says should;
    other_class: it belongs to a different problem class;
    duplicate: it says the same as another rule in the list; give that rule's number in duplicate_of and keep that one;
    wrong_numbers: its limits are wrong; add the corrected rule;
    not_atomic: it bundles several conditions (rules marked SPLIT THIS); add one rule per condition.
- add: requirements of this problem class that the document states and the list misses, written to the same standard as the list (one condition per rule, a VERBATIM source_quote of 10 to 60 words, the numeric fields where the rule is a threshold). Include corrected versions of rules you dropped for wrong numbers, and the parts of rules you dropped as not_atomic. Time windows ("within 28 days", "plus or minus 30 days") are numeric rules on a days parameter. Do not add anything the document does not state.
- contradictions: rules that cannot all be true at once, or numeric rules that would flag values the document allows (for example two rules on the same score and band, or bands that should be alternatives treated as requirements).

When the document gives alternative allowed ranges, a value is acceptable if it fits ANY of them. A rule for one option of a "one of the following" group is wrong (missing that option is fine when another holds): drop it as not_stated and add one judgment rule for the whole group if the list has none. When a limit depends on another value, there must be one rule per band, bands must not overlap, and every band in the document must be covered.

In the rules you add, operator/value/value2 describe the values that VIOLATE the rule, never the allowed values: "within 28 days" is gt 28; "a window of plus or minus 30 days" is outside -30..30; "at least 0.5" is lt 0.5.

Rule numbers refer to the numbered list you are given. Return empty lists if the list is already right."""

PROBE_SCHEMA = llm.obj({"probes": {"type": "array", "items": llm.obj({
    "rule_number": {"type": "integer"},
    "breaking_value": {"type": "number"},
    "fine_value": {"type": "number"},
})}})

PROBE_SYSTEM = """For each numbered rule, read its kind, its sentence and its quote from the document and give two example values of its parameter: breaking_value, one that should be FLAGGED by a monitor, and fine_value, one that is clearly acceptable. For an exclusion criterion, the value that meets the exclusion is the one to flag ("Body mass index < 20" as an exclusion: 17 is flagged, 25 is fine). For an inclusion criterion or a requirement, the value that fails it is the one to flag. Pick values well away from any limit, inside the realistic range of the parameter. Judge from the words only."""

EXCEPTION_SCHEMA = llm.obj({"answers": {"type": "array", "items": llm.obj({
    "rule_number": {"type": "integer"},
    "has_exception": {"type": "boolean"},
    "unless": {"type": "array", "items": TEST},
    "exception_quote": {"type": "string"},
})}})

EXCEPTION_SYSTEM = """Each numbered number rule below comes with the document text around it. Decide whether that text gives an EXCEPTION: a case in which a value the rule would flag is still allowed ("the CDR can be 0 if the memory box score is 0.5", "unless approved by the medical monitor in writing", "except for participants on a stable dose for 3 months").

Another way to measure the same value ("SVC, or FVC if SVC cannot be done", "central or local lab") is not an exception either: the limit still applies to whichever measurement is used. A limit for a different band (another education or age level) is not an exception: each band is already its own rule, and the rule's band is shown. When there is a real exception and it can be tested on numbers, set has_exception, give the tests that together make the exception in unless (all must hold; use the parameter names listed when they fit, else a short snake_case name with the unit), and copy the words that state the exception VERBATIM into exception_quote. When the exception cannot be tested on numbers (an approval, a judgment), set has_exception with an empty unless and quote it. Otherwise has_exception is false, unless is empty and exception_quote is empty. Most rules have no exception."""

_EXCEPT = re.compile(r"(?i)\b(unless|except|however|can be|may be|but|if approved|exception|waiver|allowed if)\b")

# an exception only a reader can apply: no email fact has this name, so a number hit is always judged
JUDGED_EXCEPTION = {"parameter": "exception_applies", "operator": "eq", "value": 1, "value2": None}

FLIP = {"lt": "ge", "le": "gt", "gt": "le", "ge": "lt", "eq": "ne", "ne": "eq", "outside": "between",
        "between": "outside"}

# Backstop for text that is never a checkable requirement, whatever the model says.
_JUNK = re.compile(r"(?i)\b(signature of|print name|date\s*$|we will give you a copy|initials?\s*:)")


def _pattern_key(study: Study, doc_id: str, pattern: dict, sha: str) -> str:
    pattern = {k: v for k, v in pattern.items() if k != "match_event_types"}  # matching-only, not rule making
    blob = json.dumps([sha, pattern, RULE_SYSTEM, REVIEW_SYSTEM, PROBE_SYSTEM, GAP_SYSTEM, EXCEPTION_SYSTEM,
                       CRITERIA_SYSTEM],
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _context(study: Study, doc_id: str, pattern: dict, pages: list[str]) -> str:
    doc = study.doc(doc_id)
    body = "\n".join(f"[[page {i}]]\n{t}" for i, t in enumerate(pages, start=1))
    return (
        f"Study: {study.title} ({study.study_id})\n"
        f"Document: {doc['title']} (kind: {doc['kind']})\n\n"
        f"Problem class: {pattern['id']} - {pattern['label']}\n"
        f"Definition: {pattern['description']}\n"
        f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n"
        f"{_other_classes(study, pattern)}\n"
        f"DOCUMENT TEXT:\n{body}"
    )


def _other_classes(study: Study, pattern: dict) -> str:
    others = [p for p in study.pattern_classes if p["id"] != pattern["id"]]
    return "".join(["Other problem classes of this study (their requirements are not for this class):\n"]
                   + [f"- {p['id']} - {p['label']}: {p['description']}\n" for p in others]) if others else ""


def extract_rules(study: Study, doc_id: str, pattern: dict, pages: list[str]) -> list[dict]:
    out = llm.structured("ingest", RULE_SYSTEM, _context(study, doc_id, pattern, pages), RULE_SCHEMA, "study_rules")
    return out["rules"]


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip()) if len(s) > 15])


def _describe(n: int, r: dict) -> str:
    line = f"{n}. [{r['check_type']}] {r['text']}"
    if _sentences(r["text"]) >= 3 or len(_LIST_MARKERS.findall(r["source_quote"] or "")) >= 2:
        line += "  (SPLIT THIS: it bundles several conditions)"
    if r["check_type"] == "numeric":
        cond = r.get("condition")
        line += (f"  (violation when {r['parameter']} {r['operator']} {r['value']}"
                 f"{'..' + str(r['value2']) if r.get('value2') is not None else ''}"
                 f"{'; applies when ' + json.dumps(cond) if cond else ''}"
                 f"{'; not when ' + json.dumps(r['unless']) if r.get('unless') else ''})")
    return line + f"\n   source: \"{r['source_quote'][:200]}\""


def review_rules(study: Study, doc_id: str, pattern: dict, pages: list[str], rules: list[dict]) -> dict:
    """Second reader: what to drop, what is missing, what contradicts."""
    listing = "\n".join(_describe(i, r) for i, r in enumerate(rules, start=1)) or "(no rules were extracted)"
    user = _context(study, doc_id, pattern, pages) + f"\n\nEXTRACTED RULES:\n{listing}"
    return llm.structured("ingest", REVIEW_SYSTEM, user, REVIEW_SCHEMA, "rule_review")


GAP_SYSTEM = """No rule has been written yet for any of the numbered passages below, which come from a clinical trial document. For each passage that states a requirement of the given problem class, write the rule or rules, to the standard in the instructions that follow. Skip passages that are background, belong to another problem class, or are not checkable against site correspondence. Most passages will be skipped; that is expected.

""" + RULE_SYSTEM

CRITERIA_SCHEMA = llm.obj({"answers": {"type": "array", "items": llm.obj({
    "item_number": {"type": "integer"},
    "rules": {"type": "array", "items": RULE_ITEM},
    "not_checkable_reason": {"type": "string"},
})}})

CRITERIA_SYSTEM = """Each numbered passage below is a criterion or required step of the trial that has no rule yet. Answer for EVERY number. Either write its rule or rules, to the standard in the instructions that follow, with not_checkable_reason empty; or, only when a site's correspondence could never show it being broken (a statement of policy, a note about the document itself), leave rules empty and say why in not_checkable_reason.

""" + RULE_SYSTEM

_BULLETS = "•o\\-\\*▪●◦■\uf0a7\uf0b7\uf0d8\uf076"   # the last four are Word's private-use bullet glyphs in PDFs
_BULLET = re.compile(rf"^\s*(?:[{_BULLETS}]|\d{{1,2}}[.)]|[a-z][.)])\s+\S")
_MUST = re.compile(r"(?i)\b(must|should|shall|required?|within|at least|no (?:more|later|less) than|not (?:be|allowed|"
                   r"permitted)|only|exclu|prior to|before|after|never|may not|cannot|eligible|fluent|minimum|maximum|deviations?|violations?|prohibited)\b")


def passages(text: str) -> list[str]:
    """Every list item, and every sentence that reads like a requirement: the units a rule should come from."""
    out, cur = [], None
    for line in text.splitlines():
        if _BULLET.match(line):
            if cur:
                out.append(cur)
            cur = line.strip()
        elif cur is not None and line.strip() and len(cur) < 400:
            cur += " " + line.strip()
        else:
            if cur:
                out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    flat = re.sub(r"\s+", " ", text)
    out += [s for s in re.split(rf"(?<=[.;])\s+(?=[A-Z{_BULLETS}])", flat) if _MUST.search(s)]
    seen, keep = set(), []
    for p in out:
        p = re.sub(r"\s+", " ", p).strip()
        k = textutil.normalize(p)
        shortest = 12 if _BULLET.match(p) else 25   # "5. Hemoglobin < 10 g/dL" is a whole criterion
        if shortest <= len(p) <= 600 and k not in seen:
            seen.add(k)
            keep.append(p)
    return keep


def _covers(quote: str, passage: str) -> bool:
    import difflib
    q, p = textutil.normalize(quote), textutil.normalize(passage)
    if q in p or p in q:
        return True
    m = difflib.SequenceMatcher(None, q, p, autojunk=False).find_longest_match(0, len(q), 0, len(p))
    return m.size >= min(80, max(30, len(p) // 2))   # sharing "biopsies will not be performed" is not covering


_NUMBERED_ITEM = re.compile(r"^\s*\d{1,2}[.)]\s")
_LIST_MARKERS = re.compile(r"(?m)^\s*\d{1,2}[.)]\s+\S")


def bundles_criteria(quote: str, items: list[str]) -> bool:
    """A quote that spans two or more numbered criteria lumps them together, so it covers none of them on its own.
    Lettered options under one criterion ("a.", "i.") may share a rule."""
    q = textutil.normalize(quote)
    return len({textutil.normalize(_NUMBERED_ITEM.sub("", i)) for i in items if _NUMBERED_ITEM.match(i)
                and textutil.normalize(_NUMBERED_ITEM.sub("", i)) in q}) >= 2


def covering_quotes(quotes: list[str], items: list[str]) -> list[str]:
    return [q for q in quotes if not bundles_criteria(q, items)]


def uncovered(rules: list[dict], text: str) -> list[str]:
    """Passages that no rule, kept or dropped, was taken from."""
    quotes = [r["source_quote"] for r in rules]
    return [p for p in passages(text) if not any(_covers(q, p) for q in quotes)]


def fill_gaps(study: Study, doc_id: str, pattern: dict, rules: list[dict], text: str) -> list[dict]:
    """Third reader, driven by code: show the model only the passages nothing was taken from."""
    others = [dict(r, review=None) for r in db.q(
        "SELECT source_quote FROM rules WHERE study_id=? AND doc_id=? AND active=1 AND pattern_class<>?",
        (study.study_id, doc_id, pattern["id"]))]
    gaps = uncovered(rules + others, text)
    if not gaps:
        return []
    doc = study.doc(doc_id)
    user = (f"Study: {study.title} ({study.study_id})\nDocument: {doc['title']} (kind: {doc['kind']})\n\n"
            f"Problem class: {pattern['id']} - {pattern['label']}\nDefinition: {pattern['description']}\n"
            f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n"
            f"{_other_classes(study, pattern)}\nPASSAGES:\n"
            + "\n".join(f"{i}. {p}" for i, p in enumerate(gaps, start=1)))
    return llm.structured("ingest", GAP_SYSTEM, user, RULE_SCHEMA, "rule_gaps")["rules"]


def must_cover(doc_kind: str, pattern: dict) -> bool:
    return doc_kind == "lab_manual" or (doc_kind == "protocol" and pattern["id"] == "eligibility")


def cover_criteria(study: Study, doc_id: str, pattern: dict, rules: list[dict], log=print) -> list[dict]:
    """Every criterion in the checklist ends with a rule or a stated reason why it has none: no silent gaps."""
    doc = study.doc(doc_id)
    others = [dict(r) for r in db.q("SELECT source_quote FROM rules WHERE study_id=? AND doc_id=? AND active=1 "
                                    "AND pattern_class<>?", (study.study_id, doc_id, pattern["id"]))]
    have = [r["source_quote"] for r in rules if not (r["review"] or "").startswith("dropped")]
    have += [r["source_quote"] for r in others]
    every = checklist_items(study.study_id, doc_id, doc["kind"])
    have = covering_quotes(have, every)
    items = [i for i in every if not any(_covers(q, i) for q in have)]
    if not items:
        return []
    log(f"  {doc_id} / {pattern['id']}: {len(items)} criteria still without a rule; asking for each one ...")
    user = (f"Study: {study.title} ({study.study_id})\nDocument: {doc['title']} (kind: {doc['kind']})\n\n"
            f"Problem class: {pattern['id']} - {pattern['label']}\nDefinition: {pattern['description']}\n"
            f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n"
            f"{_other_classes(study, pattern)}\nPASSAGES:\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(items, start=1)))
    answers = {a["item_number"]: a for a in
               llm.structured("ingest", CRITERIA_SYSTEM, user, CRITERIA_SCHEMA, "rule_criteria")["answers"]}
    out = []
    for n, item in enumerate(items, start=1):
        a = answers.get(n)
        if a and a["rules"]:
            out += [dict(r, review="added_for_criterion") for r in a["rules"]]
            continue
        reason = (a or {}).get("not_checkable_reason") or "no answer from the model"
        out.append({"kind": "requirement", "text": item, "check_type": "judgment", "parameter": None,
                    "operator": None, "value": None, "value2": None, "unit": None, "condition": None,
                    "unless": [], "applies_to": [], "source_quote": item, "section": "",
                    "review": f"dropped: not checkable ({reason})"})
    return out


def review_gap_rules(study: Study, doc_id: str, pattern: dict, pages: list[str], rules: list[dict],
                     gaps: list[dict]) -> dict:
    """The coverage check's rules get the same second reading as the first reader's. The reader sees the rules
    already kept, to spot repeats, but only the new ones can be dropped."""
    settled = [r for r in rules if not (r["review"] or "").startswith("dropped")]
    rev = review_rules(study, doc_id, pattern, pages, settled + gaps)
    n = len(settled)
    own = []
    for d in rev["drop"]:
        i, twin = d["rule_number"] - n, d.get("duplicate_of")
        if not 1 <= i <= len(gaps):
            continue
        if d["why"] == "duplicate" and twin and 1 <= twin <= n:
            gaps[i - 1]["review"] = f"dropped by review (duplicate): repeats a kept rule ({d['reason']})"
            continue
        own.append(dict(d, rule_number=i, duplicate_of=twin - n if twin and twin > n else None))
    apply_drops(gaps, own, rev["add"])
    return rev


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def lost_numbers(rule: dict, replacements: list[dict]) -> set[str]:
    """Numbers in a bundled rule that none of the rules split from it carry ("28 days" lost when three windows
    are split into two rules)."""
    def nums(r: dict) -> set[str]:
        found = set(_NUMBER.findall(f'{r.get("text") or ""} {r.get("source_quote") or ""}'))
        found |= {f"{v:g}" for v in (r.get("value"), r.get("value2")) if v is not None}
        return {n.rstrip("0").rstrip(".") if "." in n else n for n in found}
    have = set().union(*(nums(a) for a in replacements)) if replacements else set()
    return nums(rule) - have


def _check(r: dict) -> tuple:
    return (r["check_type"], r.get("parameter"), r.get("operator"), r.get("value"), r.get("value2"),
            json.dumps(r.get("condition"), sort_keys=True))


def apply_drops(live: list[dict], drops: list[dict], added: list[dict]) -> None:
    """Accept the second reader's drops, except ones that would lose a requirement: a duplicate whose twin is
    dropped too, or a rule with "wrong numbers" or "bundled" that comes back with no replacement."""
    valid = {d["rule_number"]: d for d in drops if 1 <= d["rule_number"] <= len(live)}
    added_params = {r["parameter"] for r in added if r["parameter"]}
    for n, d in valid.items():
        r = live[n - 1]
        if d["why"] == "duplicate":
            twin = d.get("duplicate_of")
            if not twin or twin == n or twin in valid or not 1 <= twin <= len(live):
                r["review"] = f"kept: review called it a duplicate but would drop its twin too ({d['reason']})"
                continue
            if r["check_type"] == "numeric" and _check(r) != _check(live[twin - 1]):
                r["review"] = f"kept: review called it a duplicate but it checks a different limit ({d['reason']})"
                continue
            r["_twin"] = live[twin - 1]
        if d["why"] == "wrong_numbers" and r["check_type"] == "numeric" and r["parameter"] not in added_params:
            r["review"] = f"kept: review said the numbers are wrong but gave no correction ({d['reason']})"
            continue
        if d["why"] == "not_stated" and re.search(r"\bshould\b", d["reason"], re.I):
            r["review"] = f"kept: review read \"should\" as optional, but a protocol's should is a duty ({d['reason']})"
            continue
        if d["why"] == "not_atomic":
            parts = [a for a in added if _covers(a["source_quote"], r["source_quote"])]
            if not parts:
                r["review"] = f"kept: review said it bundles conditions but did not split it ({d['reason']})"
                continue
            lost = lost_numbers(r, parts)
            if lost:
                r["review"] = (f"kept: splitting it would lose {', '.join(sorted(lost))} ({d['reason']})")
                continue
        r["review"] = f"dropped by review ({d['why']}): {d['reason']}"


def repair_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Find a quote the model copied with small slips (spacing, a dropped character); None if not close enough."""
    import difflib
    nq = textutil.normalize(quote)
    if len(nq) < 20:
        return None
    nt, idx = textutil._norm_char_map(text)
    m = difflib.SequenceMatcher(None, nt, nq, autojunk=False).find_longest_match(0, len(nt), 0, len(nq))
    if m.size < 12:
        return None
    start = max(0, m.a - m.b)
    best = None
    for s in range(max(0, start - 15), min(len(nt), start + 16)):
        window = nt[s:s + len(nq)]
        ratio = difflib.SequenceMatcher(None, window, nq, autojunk=False).ratio()
        if best is None or ratio > best[0]:
            best = (ratio, s)
    if not best or best[0] < 0.9:
        return None
    s, e = best[1], min(len(nt), best[1] + len(nq)) - 1
    return idx[s], idx[e] + 1


def check_exceptions(rules: list[dict], text: str, starts: list[int]) -> int:
    """Look next to each number rule for an exception the rule-maker may have missed, and attach it as tests that
    turn the rule off. The exception's words must be in the document."""
    asked = []
    for r in rules:
        if r["check_type"] != "numeric" or r.get("unless"):
            continue
        span = best_span(r["source_quote"], text, starts, r.get("section"))
        if not span:
            continue
        window = text[max(0, span[0] - 300):span[1] + 900]
        if _EXCEPT.search(window):
            asked.append((r, window))
    if not asked:
        return 0
    params = sorted({r["parameter"] for r in rules if r.get("parameter")})
    listing = "\n\n".join(
        f"{i}. RULE: {r['text']}  (violation when {r['parameter']} {r['operator']} {_num(r['value'])}"
        f"{'..' + _num(r['value2']) if r.get('value2') is not None else ''}"
        f"{'; only for ' + json.dumps(r['condition']) if r.get('condition') else ''})\nTEXT AROUND IT:\n{w}"
        for i, (r, w) in enumerate(asked, start=1))
    out = llm.structured("ingest", EXCEPTION_SYSTEM, f"Parameter names: {', '.join(params)}\n\n{listing}",
                         EXCEPTION_SCHEMA, "rule_exceptions")["answers"]
    found = 0
    for a in out:
        if not a["has_exception"] or not 1 <= a["rule_number"] <= len(asked):
            continue
        r, window = asked[a["rule_number"] - 1]
        if not textutil.quote_in(a["exception_quote"], window, short_ok=True):
            continue
        band = (r.get("condition") or {}).get("parameter")
        if a["unless"] and band and all(t["parameter"] == band for t in a["unless"]):
            continue  # another band of the same limit, already its own rule

        note = f"exception: \"{a['exception_quote'][:120]}\""
        if a["unless"]:
            r["unless"] = a["unless"]
            found += 1
        else:
            note += " (not testable on numbers: a number hit goes to the judgment step)"
            r["text"] = f"{r['text']} Exception: {a['exception_quote']}"
            r["unless"] = [JUDGED_EXCEPTION]
            found += 1
        r["review"] = (r["review"] + "; " if r["review"] else "") + note
    return found


def check_directions(rules: list[dict]) -> int:
    """The model writes a rule's limits; a second, independent question asks for a value that breaks the rule
    and one that does not, and code tests both. Limits written the wrong way round are turned back."""
    from .matching import violates

    numeric = [r for r in rules if r["check_type"] == "numeric"]
    if not numeric:
        return 0
    listing = "\n".join(f"{i}. ({r.get('kind') or 'requirement'}) [{r['parameter']}] {r['text']}  QUOTE: \"{r['source_quote']}\""
                        for i, r in enumerate(numeric, start=1))
    probes = llm.structured("ingest", PROBE_SYSTEM, listing, PROBE_SCHEMA, "rule_probe")["probes"]
    fixed = 0
    for p in probes:
        if not 1 <= p["rule_number"] <= len(numeric):
            continue
        r = numeric[p["rule_number"] - 1]
        breaks = violates(p["breaking_value"], r["operator"], r["value"], r["value2"])
        fine = not violates(p["fine_value"], r["operator"], r["value"], r["value2"])
        if breaks and fine:
            continue
        flipped = FLIP.get(r["operator"])
        if not breaks and not fine and flipped:
            note = (f"limits turned round: {r['operator']} -> {flipped} "
                    f"({_num(p['breaking_value'])} breaks the rule, {_num(p['fine_value'])} does not)")
            r["operator"] = flipped
            r["review"] = (r["review"] + "; " if r["review"] else "") + note
            fixed += 1
        else:
            r["review"] = (r["review"] + "; " if r["review"] else "") + (
                f"direction unconfirmed: {_num(p['breaking_value'])} should break it, "
                f"{_num(p['fine_value'])} should not")
    return fixed


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def quote_fragment(rule: dict, text: str, starts: list[int]) -> tuple[int, int] | None:
    """When the model glued a heading onto a table row ("For early MCI: 5-9 for 8-15 years") or cut a sentence
    with "[...]", keep the longest piece that is really in the document and still carries the rule's numbers."""
    pieces = [p.strip(" .,") for p in re.split(r"[:;\n•\[\]…]|\.{3}|\s{2,}", rule["source_quote"]) if p.strip(" .,")]
    needed = [_num(v) for v in (rule.get("value"), rule.get("value2")) if v is not None]
    for piece in sorted(pieces, key=len, reverse=True):
        if len(piece) < 12 or not all(n in piece for n in needed):
            continue
        span = best_span(piece, text, starts, rule.get("section"))
        if span:
            return span
    return None


def _allowed(r: dict) -> tuple[float, float] | None:
    """The range of values that does NOT break a numeric rule, or None when it isn't one range."""
    inf = float("inf")
    o, v, v2 = r["operator"], r["value"], r["value2"]
    if o in ("gt", "ge"):
        return (-inf, v)
    if o in ("lt", "le"):
        return (v, inf)
    if o == "outside" and v2 is not None:
        return (v, v2)
    return None


def rescue_replaced(rules: list[dict]) -> int:
    """A rule dropped as bundled, as having wrong numbers or as a duplicate comes back when none of the rules meant
    to replace it survived the quote check."""
    back = 0
    for r in rules:
        twin = r.get("_twin")
        if r["_span"] is not None and twin is not None and not twin["_active"] and not r["_active"]:
            r["_active"] = True
            r["review"] = f"kept: the rule it duplicates did not pass the quote check ({r['_origin'][:80]})"
            back += 1
            continue
        if r["_span"] is None or not r["_origin"].startswith(("dropped by review (not_atomic)",
                                                               "dropped by review (wrong_numbers)")):
            continue
        reps = [a for a in rules if a["_origin"].startswith("added_by") and (
            _covers(a["source_quote"], r["source_quote"])
            or (r["check_type"] == "numeric" and a.get("parameter") == r.get("parameter")))]
        alive = [a for a in reps if a["_active"]]
        lost = lost_numbers(r, alive) if r["_origin"].startswith("dropped by review (not_atomic)") else set()
        if not alive or lost:
            r["_active"] = True
            r["review"] = ("kept: the rules meant to replace it did not pass the quote check" if not alive else
                           f"kept: the rules split from it lose {', '.join(sorted(lost))}") + f" ({r['_origin'][:80]})"
            back += 1
    return back


def merge_alternatives(rules: list[dict]) -> int:
    """Two limits on the same value for the same band that cannot both hold ("9-11" and "8 or less") are
    alternatives in a protocol: allow either, as one rule. Nested limits are left alone and reported."""
    merged = 0
    groups: dict[tuple, list[dict]] = {}
    for r in rules:
        if r["check_type"] == "numeric" and _allowed(r) and not r.get("unless"):
            groups.setdefault((r["parameter"], json.dumps(r["condition"], sort_keys=True)), []).append(r)
    for rs in groups.values():
        while len(rs) > 1:
            pair = next(((a, b) for i, a in enumerate(rs) for b in rs[i + 1:] if _joinable(a, b)), None)
            if not pair:
                nested = next(((a, b) for i, a in enumerate(rs) for b in rs[i + 1:] if _same_list(a, b)), None)
                if not nested:
                    break
                # two limits for the same group in the same passage are a list of allowed options
                # ("early MCI 9-11, late MCI 8 or less"): keep the looser one
                a, b = sorted(nested, key=lambda r: _allowed(r)[1] - _allowed(r)[0], reverse=True)
                a["review"] = f"kept the wider of two allowed limits from the same passage ({b['text'][:60]})"
                b["review"] = "dropped: a narrower allowed option listed with a wider one in the same passage"
                b["_active"] = False
                rs.remove(b)
                merged += 1
                continue
            a, b = pair
            (lo1, hi1), (lo2, hi2) = _allowed(a), _allowed(b)
            lo, hi = min(lo1, lo2), max(hi1, hi2)
            if lo == float("-inf"):
                a.update(operator="gt", value=hi, value2=None)
            elif hi == float("inf"):
                a.update(operator="lt", value=lo, value2=None)
            else:
                a.update(operator="outside", value=lo, value2=hi)
            a["text"] = f"{a['text']} OR {b['text']}"
            a["review"] = f"merged alternatives: either range is allowed ({b['source_quote'][:60]})"
            b["review"] = "dropped: merged into an either-or rule with the other range"
            b["_active"] = False
            rs.remove(b)
            merged += 1
    return merged


def _same_list(a: dict, b: dict) -> bool:
    """One allowed range inside the other, both taken from the same passage."""
    (lo1, hi1), (lo2, hi2) = _allowed(a), _allowed(b)
    nested = (lo1 <= lo2 and hi2 <= hi1) or (lo2 <= lo1 and hi1 <= hi2)
    qa, qb = textutil.normalize(a["source_quote"]), textutil.normalize(b["source_quote"])
    return nested and (lo1, hi1) != (lo2, hi2) and len(min(qa, qb, key=len)) >= 20 and (qa in qb or qb in qa)


def _joinable(a: dict, b: dict) -> bool:
    """Ranges that touch or overlap, neither inside the other; whole-number scores may leave a gap of 1."""
    (lo1, hi1), (lo2, hi2) = _allowed(a), _allowed(b)
    if (lo1 <= lo2 and hi2 <= hi1) or (lo2 <= lo1 and hi1 <= hi2):
        return False
    if min(lo1, lo2) == float("-inf") and max(hi1, hi2) == float("inf"):
        return False  # together they allow every value: one of them points the wrong way, not an either-or
    if lo2 < lo1:
        (lo1, hi1), (lo2, hi2) = (lo2, hi2), (lo1, hi1)
    whole = all(float(x).is_integer() for x in (hi1, lo2) if abs(x) != float("inf"))
    return lo2 <= hi1 + (1 if whole else 0)


_APPROX = re.compile(r"(?i)\b(approximately|approx\.?|about|around|roughly)\b|~\s*\d")


_GENERIC_TIME_WORDS = {"days", "weeks", "hours", "visit", "visits", "from", "since", "after", "before", "between",
                       "to", "of", "the", "prior", "previous", "next", "nominal", "target", "scheduled", "planned",
                       "date", "baseline", "window", "each", "any", "all", "deviation", "offset"}


def names_no_event(parameter: str) -> bool:
    """A time window named only in general words (days_visit_after_baseline, days_from_nominal) would be applied
    to every visit; the rule's words must say which one."""
    words = parameter.split("_")
    return words[0] in ("days", "weeks", "hours") and not any(c.isdigit() for c in parameter) \
        and set(words) <= _GENERIC_TIME_WORDS


def canonical_parameter(name: str) -> str:
    """One name for one value, whichever way the model spelled it (days_studyvisit3_after_study_visit2 is
    days_visit3_after_visit2)."""
    name = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return re.sub(r"study_?(visit)", r"\1", name)


def _same_words(a: str, b: str) -> bool:
    """The same rule written twice ("Exclude if X." and "Exclude if participant has X")."""
    import difflib
    return (set(_NUMBER.findall(a)) == set(_NUMBER.findall(b))
            and difflib.SequenceMatcher(None, textutil.normalize(a), textutil.normalize(b)).ratio() >= 0.9)


def lint(rules: list[dict]) -> list[tuple[int, str]]:
    """Checks made in code, whatever the model said: (rule index, reason to drop)."""
    out, seen, judged = [], {}, []
    for i, r in enumerate(rules):
        if _JUNK.search(r["text"]) or _JUNK.search(r["source_quote"]):
            out.append((i, "not a checkable requirement (form or signature text)"))
            continue
        if r["check_type"] == "numeric" and (not r["parameter"] or not r["operator"] or r["value"] is None):
            r.update(check_type="judgment", parameter=None, operator=None, value=None, value2=None)
        for t in ([r["condition"]] if r.get("condition") else []) + list(r.get("unless") or []):
            if t["parameter"] != "exception_applies":
                t["parameter"] = canonical_parameter(t["parameter"])
        if r["check_type"] == "numeric":
            r["parameter"] = canonical_parameter(r["parameter"])
            if r["operator"] in ("outside", "between") and r["value2"] is not None and r["value"] > r["value2"]:
                r["value"], r["value2"] = r["value2"], r["value"]
            if names_no_event(r["parameter"]):
                r.update(check_type="judgment", parameter=None, operator=None, value=None, value2=None)
        if (r["check_type"] == "numeric" and r["operator"] in ("eq", "ne")
                and _APPROX.search(r["text"] + " " + r["source_quote"])):
            r.update(check_type="judgment", parameter=None, operator=None, value=None, value2=None)
        if r["check_type"] == "judgment":
            twin = next((j for j, t in judged if _same_words(t, r["text"])), None)
            if twin is not None:
                out.append((i, f"duplicate of rule {twin + 1}"))
                continue
            judged.append((i, r["text"]))
        key = (r["check_type"], r["parameter"], r["operator"], r["value"], r["value2"],
               json.dumps(r["condition"], sort_keys=True), json.dumps(r.get("unless") or [], sort_keys=True),
               textutil.normalize(r["text"]) if
               r["check_type"] == "judgment" else "")
        if key in seen:
            out.append((i, f"duplicate of rule {seen[key] + 1}"))
        else:
            seen[key] = i
    return out


def contradictions(rules: list[dict]) -> list[str]:
    """Numeric rules on the same parameter and the same band that set different limits."""
    groups: dict[tuple, list[dict]] = {}
    for r in rules:
        if r["check_type"] == "numeric":
            groups.setdefault((r["parameter"], json.dumps(r["condition"], sort_keys=True)), []).append(r)
    out = []
    for (param, cond), rs in groups.items():
        limits = {(r["operator"], r["value"], r["value2"]) for r in rs}
        if len(limits) > 1 and not _compatible(limits):
            out.append(f"{param} ({'when ' + cond if cond != 'null' else 'always'}): "
                       + "; ".join(f"{o} {v}{'..' + str(v2) if v2 is not None else ''}" for o, v, v2 in sorted(
                           limits, key=str)))
    return out


def alternatives_not_exceptions(rules: list[dict]) -> int:
    """"SVC < 50 unless pandemic" next to "FVC < 50 when pandemic": the same limit on another way of measuring,
    not a case where the low value is allowed. The limit stays on."""
    cleared = 0
    for r in rules:
        if r["check_type"] != "numeric" or not r.get("unless"):
            continue
        twin = next((o for o in rules if o is not r and o["check_type"] == "numeric" and o.get("condition")
                     and o["parameter"] != r["parameter"]
                     and (o["operator"], o["value"], o["value2"]) == (r["operator"], r["value"], r["value2"])
                     and any(json.dumps(o["condition"], sort_keys=True) == json.dumps(t, sort_keys=True)
                             for t in r["unless"])), None)
        if twin:
            r["unless"] = []
            r["review"] = ((r.get("review") or "") + f"; the 'unless' was another way of measuring ({twin['parameter']}),"
                           " not an exception").lstrip("; ")
            cleared += 1
    return cleared


def read_clashing_windows(rules: list[dict]) -> int:
    """Windows on the same value that clash (one name for several visits) cannot be applied by code: each one
    becomes a rule the AI reads, whose words say which visit it is for."""
    groups: dict[tuple, list[dict]] = {}
    for r in rules:
        if r["check_type"] == "numeric":
            groups.setdefault((r["parameter"], json.dumps(r["condition"], sort_keys=True)), []).append(r)
    turned = 0
    for rs in groups.values():
        limits = {(r["operator"], r["value"], r["value2"]) for r in rs}
        if len(limits) < 2 or _compatible(limits) or not any(o in ("outside", "between") for o, _, _ in limits):
            continue
        for r in rs:
            r.update(check_type="judgment", parameter=None, operator=None, value=None, value2=None)
            r["review"] = ((r.get("review") or "") + "; read, not counted: other windows on the same value clash"
                           ).lstrip("; ")
            turned += 1
    return turned


def _compatible(limits: set[tuple]) -> bool:
    """A lower and an upper limit on the same value (lt 55 and gt 90) are fine together."""
    lows = {o for o, _, _ in limits if o in ("lt", "le")}
    highs = {o for o, _, _ in limits if o in ("gt", "ge")}
    return len(limits) == 2 and len(lows) == 1 and len(highs) == 1


def ingest_study(study: Study, force: bool = False, log=print) -> dict:
    summary = {"documents": 0, "rules": 0, "unverified": 0, "skipped": 0}
    any_built = False
    for doc in study.documents:
        doc_id = doc["id"]
        sha, pages, changed = store_document(study, doc_id)
        summary["documents"] += 1
        full_text, starts = doc_text(study.study_id, doc_id)
        built = False
        for pattern in study.pattern_classes:
            if doc_id not in pattern.get("documents", []):
                continue
            key = _pattern_key(study, doc_id, pattern, sha)
            state_key = f"ingest:{study.study_id}:{doc_id}:{pattern['id']}"
            if not force and db.get_state(state_key) == key:
                summary["skipped"] += 1
                log(f"  {doc_id} / {pattern['id']}: unchanged, kept existing rules")
                continue
            log(f"  {doc_id} / {pattern['id']}: extracting rules ...")
            rules = [dict(r, review=None) for r in extract_rules(study, doc_id, pattern, pages)]
            for i, reason in lint(rules):
                rules[i]["review"] = f"dropped: {reason}"
            log(f"  {doc_id} / {pattern['id']}: second reader reviewing {len(rules)} rules ...")
            live = [r for r in rules if not r["review"]]
            rev = review_rules(study, doc_id, pattern, pages, live)
            apply_drops(live, rev["drop"], rev["add"])
            rules += [dict(r, review="added_by_review") for r in rev["add"]]
            log(f"  {doc_id} / {pattern['id']}: checking which requirements no rule covers ...")
            gaps = [dict(r, review="added_by_coverage_check")
                    for r in fill_gaps(study, doc_id, pattern, rules, full_text)]
            if gaps:
                log(f"  {doc_id} / {pattern['id']}: second reader reviewing {len(gaps)} coverage-check rules ...")
                rev2 = review_gap_rules(study, doc_id, pattern, pages, rules, gaps)
                rules += gaps + [dict(r, review="added_by_review") for r in rev2["add"]]
            if must_cover(study.doc(doc_id)["kind"], pattern):
                rules += cover_criteria(study, doc_id, pattern, rules, log)
            kept = [r for r in rules if not (r["review"] or "").startswith("dropped")]
            for i, reason in lint(kept):
                kept[i]["review"] = f"dropped: {reason}"
            live = [r for r in kept if not (r["review"] or "").startswith("dropped")]
            exceptions = check_exceptions(live, full_text, starts)
            turned = check_directions(live)
            counts = store_rules(study, doc_id, pattern, sha, rules, full_text, starts)
            counts["limits_turned_round"] = turned
            counts["exceptions"] = exceptions
            kept = [r for r in rules if r.get("_active")]
            clash = contradictions(kept)
            for c in clash:
                db.audit("rule_contradiction", entity_type="study", entity_id=study.study_id,
                         study_id=study.study_id, doc_id=doc_id, pattern_class=pattern["id"], detail=c)
            for c in rev["contradictions"]:
                db.audit("rule_contradiction_review", entity_type="study", entity_id=study.study_id,
                         study_id=study.study_id, doc_id=doc_id, pattern_class=pattern["id"],
                         rules=c["rule_numbers"], detail=c["explanation"])
            db.set_state(state_key, key)
            db.audit("rules_built", entity_type="study", entity_id=study.study_id, study_id=study.study_id,
                     doc_id=doc_id, pattern_class=pattern["id"], contradictions=clash, **counts)
            summary["rules"] += counts["active"]
            summary["unverified"] += counts["quote_not_found"]
            built = True
            log(f"    {counts['active']} active ({counts['added_by_review']} added by the second reader, "
                f"{counts['added_by_coverage_check']} by the coverage check), "
                f"{counts['dropped']} dropped, {counts['quote_not_found']} rejected (quote not in the document), "
                f"{counts['repaired_quotes']} quotes repaired, {counts['merged']} either-or merges, {turned} limits turned round, {exceptions} exceptions, "
                f"{counts['rescued']} rescued, {counts['read_not_counted']} clashing windows left to the reader"
                + (f", CONTRADICTIONS: {clash}" if clash else ""))
        any_built = any_built or built
        summary["allowances"] = summary.get("allowances", 0) + build_allowances(
            study, doc_id, sha, full_text, starts, force, log)
    if any_built:
        summary["rules"] -= settle_classes(study, log)
        summary["rules"] -= settle_documents(study, log)
    return summary


ALLOWANCE_SCHEMA = llm.obj({"allowances": {"type": "array", "items": llm.obj({
    "text": {"type": "string"},
    "source_quote": {"type": "string"},
    "classes": {"type": "array", "items": {"type": "string"}},
})}})

ALLOWANCE_SYSTEM = """You read part of a clinical trial document and list its ALLOWANCES: statements that make something acceptable that a strict reading of another requirement would report as a problem. A monitor reads them before raising an alert, so that an allowed case is not reported.

Allowances include:
- a permitted redo or retest, and which result then counts;
- a permitted alternative way, place, time, route or method of doing a required step;
- what may be done when a step is missed, late or cannot be done, where doing that is acceptable;
- statements that something is not a deviation, or that a step is not required, in a stated situation (including steps required "only if" something holds);
- definitions that change how a limit is counted (what one dose, one visit or one day consists of);
- a problem handled in time in the way the document asks (set aside and not used until released), so that no requirement was broken.
Not allowances: the requirements themselves (a statement that something is or will be recorded as a deviation is a requirement, whether minor or major), background, statistics, how adverse events are classified or reported, and duties of the sponsor or ethics board only.

For each allowance: text, one plain sentence saying what is allowed and when; source_quote, the words that state it, copied VERBATIM from the text, 8 to 60 words; classes, the ids of the problem classes whose requirements it relaxes, from the list given. Return an empty list if the text has none."""


def _chunks(text: str, size: int = 12000, overlap: int = 800) -> list[str]:
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size])
        i += size - overlap
    return out


def names_a_deviation(text: str) -> bool:
    """"Will be recorded as a minor protocol deviation" sets a requirement; "is not a deviation" allows something."""
    t = text.lower()
    return bool(re.search(r"\bdeviations?\b", t)) and not re.search(r"\bnot\b[^.]{0,60}\bdeviations?\b", t)


def build_allowances(study: Study, doc_id: str, sha: str, text: str, starts: list[int], force: bool,
                     log=print) -> int:
    """Collect what the document allows, each with a quote checked against the document."""
    classes = {p["id"]: p for p in study.pattern_classes if doc_id in p.get("documents", [])}
    if not classes:
        return 0
    key = hashlib.sha256(f"{sha}|{ALLOWANCE_SYSTEM}|{sorted(classes)}|{llm.model_for('ingest')}".encode()).hexdigest()
    state_key = f"allowances:{study.study_id}:{doc_id}"
    if not force and db.get_state(state_key) == key:
        return 0
    log(f"  {doc_id}: collecting what the document allows ...")
    doc = study.doc(doc_id)
    head = (f"Study: {study.title} ({study.study_id})\nDocument: {doc['title']}\n\nPROBLEM CLASSES:\n"
            + "".join(f"- {c}: {p['label']} - {p['description']}\n" for c, p in classes.items()))
    found, seen = [], set()
    for part in _chunks(text):
        for a in llm.structured("ingest", ALLOWANCE_SYSTEM, f"{head}\nTEXT:\n{part}", ALLOWANCE_SCHEMA,
                                "allowances")["allowances"]:
            span = best_span(a["source_quote"], text, starts, None) or repair_quote(a["source_quote"], text)
            cls = [c for c in a["classes"] if c in classes]
            norm = textutil.normalize(a["source_quote"])
            if not span or not cls or norm in seen or names_a_deviation(norm):
                continue
            seen.add(norm)
            found.append((a, cls, span))
    db.ex("DELETE FROM allowances WHERE study_id=? AND doc_id=?", (study.study_id, doc_id))
    for n, (a, cls, span) in enumerate(found, start=1):
        db.ex("INSERT INTO allowances(id, study_id, doc_id, text, classes, source_quote, source_page, created_at)"
              " VALUES (?,?,?,?,?,?,?,?)",
              (f"{study.short_name}-{doc_id}-allow-{n:03d}", study.study_id, doc_id, a["text"], db.js(cls),
               textutil.clean_spaces(text[span[0]:span[1]]), page_of(starts, span[0]), db.now()))
    db.set_state(state_key, key)
    db.audit("allowances_built", entity_type="study", entity_id=study.study_id, study_id=study.study_id,
             doc_id=doc_id, count=len(found))
    log(f"    {len(found)} allowances")
    return len(found)


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def allowances_for(study_id: str, pattern_class: str, about: str, limit: int = 12) -> list[dict]:
    """The allowances that relax a problem class, those sharing the most words with the case first."""
    rows = [dict(r) for r in db.q("SELECT * FROM allowances WHERE study_id=? ORDER BY id", (study_id,))]
    rows = [r for r in rows if pattern_class in db.unjs(r["classes"], [])]
    want = _words(about)
    rows.sort(key=lambda r: -len(want & _words(r["text"] + " " + r["source_quote"])))
    return rows[:limit]


SETTLE_DOCS_SCHEMA = llm.obj({"replaced": {"type": "array", "items": llm.obj({
    "general_rule": {"type": "integer"},
    "specific_rule": {"type": "integer"},
    "reason": {"type": "string"},
    "cannot_both_be_followed": {"type": "boolean"},
})}})

SETTLE_DOCS_SYSTEM = """Below are numbered rules from two documents of one clinical trial: a GENERAL document written for several treatments or sites, and a SPECIFIC document for this one, which takes precedence where the two differ.

Return each GENERAL rule that the SPECIFIC document changes: both rules are about the same step or value, and the specific document sets a different time, order, window, limit or procedure for it, so that a site following the specific rule would break the general one. Give the general rule number, the number of the specific rule that replaces it, a short reason, and cannot_both_be_followed: true only if a site could not follow both rules at once. Rules about different things never replace each other (a screening window does not replace a limit on disease duration). Do not return general rules that the specific document only repeats, clarifies or adds to; most general rules still apply. Return an empty list if none are changed."""


def settle_documents(study: Study, log=print) -> int:
    """Where a study-specific document changes a step of a general one, its rule wins and the general rule is off."""
    dropped = 0
    for doc in study.documents:
        for general in doc.get("overrides") or []:
            spec = [dict(r) for r in db.q("SELECT id, pattern_class, text FROM rules WHERE study_id=? AND doc_id=? "
                                          "AND active=1 ORDER BY pattern_class, order_idx", (study.study_id, doc["id"]))]
            gen = [dict(r) for r in db.q("SELECT id, pattern_class, text FROM rules WHERE study_id=? AND doc_id=? "
                                         "AND active=1 ORDER BY pattern_class, order_idx", (study.study_id, general))]
            if not spec or not gen:
                continue
            user = (f"Study: {study.title}\n\nGENERAL document: {study.doc(general)['title']}\n"
                    + "\n".join(f"G{n}. [{r['pattern_class']}] {r['text']}" for n, r in enumerate(gen, 1))
                    + f"\n\nSPECIFIC document: {doc['title']}\n"
                    + "\n".join(f"S{n}. [{r['pattern_class']}] {r['text']}" for n, r in enumerate(spec, 1))
                    + "\n\nRule numbers: give G rules and S rules by their number only.")
            out = llm.structured("ingest", SETTLE_DOCS_SYSTEM, user, SETTLE_DOCS_SCHEMA, "replaced_rules")["replaced"]
            for x in out:
                if not (1 <= x["general_rule"] <= len(gen) and 1 <= x["specific_rule"] <= len(spec)):
                    continue
                g, s = gen[x["general_rule"] - 1], spec[x["specific_rule"] - 1]
                if not x["cannot_both_be_followed"] or g["pattern_class"] != s["pattern_class"]:
                    continue
                db.ex("UPDATE rules SET active=0, review=? WHERE id=? AND active=1",
                          (f"dropped: replaced by {s['id']} ({doc['id']} takes precedence): {x['reason']}", g["id"]))
                db.audit("rule_dropped", entity_type="rule", entity_id=g["id"], study_id=study.study_id,
                         reason=f"replaced by {s['id']} ({doc['id']} takes precedence)", detail=x["reason"],
                         text=g["text"])
                dropped += 1
    if dropped:
        log(f"  {dropped} general rules replaced by the study-specific document")
    return dropped


SETTLE_SCHEMA = llm.obj({"groups": {"type": "array", "items": llm.obj({
    "rule_numbers": {"type": "array", "items": {"type": "integer"}},
    "problem_class": {"type": "string"},
})}})

SETTLE_SYSTEM = """Below are the rules of one clinical trial, numbered, each with its problem class. A requirement belongs to ONE class, but the same requirement is sometimes written for two classes in different words.

Return every group of two or more rules from DIFFERENT classes that state the same requirement: a site that breaks one breaks the other, for the same reason. Rules on the same topic that set different requirements are NOT the same (no inducers in the 2 weeks before the first dose, versus no inducers during treatment; consent before screening, versus re-consent for a new version). For each group give the rule numbers and the id of the class whose definition fits the requirement best. A requirement about what must already be true when a participant is screened, randomized or first dosed (a washout before the first dose, a stable dose before screening) belongs to the eligibility class; the same topic during treatment is a different requirement and keeps its own class. Most rules are in no group; return an empty list if there are none."""


def settle_classes(study: Study, log=print) -> int:
    """The same requirement kept by two problem classes is one rule: keep it in the class that fits best."""
    rows = [dict(r) for r in db.q("SELECT id, pattern_class, text FROM rules WHERE study_id=? AND active=1 "
                                  "ORDER BY doc_id, pattern_class, order_idx", (study.study_id,))]
    if len({r["pattern_class"] for r in rows}) < 2:
        return 0
    classes = {p["id"]: p for p in study.pattern_classes}
    user = ("PROBLEM CLASSES:\n" + "".join(f"- {c}: {p['label']} - {p['description']}\n" for c, p in classes.items())
            + "\nRULES:\n" + "\n".join(f"{n}. [{r['pattern_class']}] {r['text']}" for n, r in enumerate(rows, 1)))
    groups = llm.structured("ingest", SETTLE_SYSTEM, user, SETTLE_SCHEMA, "rule_classes")["groups"]
    dropped = 0
    for g in groups:
        members = [rows[n - 1] for n in dict.fromkeys(g["rule_numbers"]) if 1 <= n <= len(rows)]
        home = g["problem_class"]
        if len({r["pattern_class"] for r in members}) < 2 or home not in {r["pattern_class"] for r in members}:
            continue
        keep = next(r for r in members if r["pattern_class"] == home)
        for r in members:
            if r["pattern_class"] != home:
                db.ex("UPDATE rules SET active=0, review=? WHERE id=? AND active=1",
                      (f"dropped: duplicate of {keep['id']} (the same requirement, which belongs to {home})", r["id"]))
                db.audit("rule_dropped", entity_type="rule", entity_id=r["id"], study_id=study.study_id,
                         reason=f"same requirement as {keep['id']} ({home})", text=r["text"])
                dropped += 1
    log(f"  {dropped} rules kept by more than one problem class were moved to one")
    return dropped


def store_rules(study: Study, doc_id: str, pattern: dict, sha: str, rules: list[dict], full_text: str,
               starts: list[int]) -> dict:
    """Verify each rule's quote against the document and store the lot (inactive rules are kept for the record)."""
    db.ex("DELETE FROM rules WHERE study_id=? AND doc_id=? AND pattern_class=?",
          (study.study_id, doc_id, pattern["id"]))
    counts = {"active": 0, "dropped": 0, "quote_not_found": 0, "added_by_review": 0, "added_by_coverage_check": 0,
              "repaired_quotes": 0,
              "merged": 0}
    checked_on = pattern.get("match_event_types") or []
    for r in rules:
        r["_origin"] = r.get("review") or ""
        if r.get("applies_to") and checked_on:
            # "SVC at screening" is still checked when the participant is randomized
            r["applies_to"] = list(dict.fromkeys(r["applies_to"] + checked_on))
        span = best_span(r["source_quote"], full_text, starts, r["section"])
        if span is None:
            span = repair_quote(r["source_quote"], full_text) or quote_fragment(r, full_text, starts)
            if span:
                counts["repaired_quotes"] += 1
        r["_span"] = span
        if span is None:
            review = r.get("review")
            r["review"] = ((review + "; " if review and not review.startswith("added_by") else "")
                           + "quote not found in the document")
        r["_active"] = span is not None and not (r.get("review") or "").startswith("dropped")
    counts["rescued"] = rescue_replaced(rules)
    counts["merged"] = merge_alternatives([r for r in rules if r["_active"]])
    counts["read_not_counted"] = read_clashing_windows([r for r in rules if r["_active"]])
    counts["alternatives_not_exceptions"] = alternatives_not_exceptions([r for r in rules if r["_active"]])
    active = [r for r in rules if r["_active"]]
    for i, reason in lint(active):
        active[i].update(_active=False, review=f"dropped: {reason} (after merging)")
    for idx, r in enumerate(rules):
        span, review, active = r["_span"], r.get("review"), r["_active"]
        verified = span is not None
        if not verified:
            counts["quote_not_found"] += 1
        if active:
            counts["active"] += 1
            counts["added_by_review"] += (review or "").startswith("added_by_review")
            counts["added_by_coverage_check"] += (review or "").startswith(("added_by_coverage_check",
                                                                             "added_for_criterion"))
        elif verified:
            counts["dropped"] += 1
        page = page_of(starts, span[0]) if span else None
        exact = textutil.clean_spaces(full_text[span[0]:span[1]]) if span else r["source_quote"]
        rule_id = f"{study.short_name}-{doc_id}-{pattern['id']}-{idx + 1:03d}"
        db.ex(
            "INSERT INTO rules (id, study_id, doc_id, doc_sha256, pattern_class, kind, text, check_type,"
            " parameter, operator, value, value2, unit, condition, applies_to, source_quote, source_page,"
            " section, verified, order_idx, active, created_at, review, unless) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rule_id, study.study_id, doc_id, sha, pattern["id"], r["kind"], r["text"],
             r["check_type"], r["parameter"], r["operator"], r["value"], r["value2"], r["unit"],
             db.js(r["condition"]) if r["condition"] else None, db.js(r["applies_to"]),
             exact, page, r["section"], int(verified), idx, int(active), db.now(), review,
             db.js(r["unless"]) if r.get("unless") else None),
        )
        if not verified:
            db.audit("rule_defect_unverified_source", entity_type="rule", entity_id=rule_id,
                     study_id=study.study_id, claimed_quote=r["source_quote"], text=r["text"])
        elif not active:
            db.audit("rule_dropped", entity_type="rule", entity_id=rule_id, study_id=study.study_id,
                     reason=review, text=r["text"])
    from . import prune
    counts["removed_by_team"] = prune.reapply(study.study_id, doc_id, pattern["id"])
    counts["active"] -= counts["removed_by_team"]
    return counts


def report(study: Study) -> str:
    """What the study model holds, what was dropped and why, and any contradictions left."""
    rows = [dict(r) for r in db.q("SELECT * FROM rules WHERE study_id=? ORDER BY doc_id, pattern_class, order_idx",
                                  (study.study_id,))]
    lines = [f"Study model for {study.study_id}: {sum(r['active'] for r in rows)} active rules "
             f"out of {len(rows)} extracted\n"]
    for (doc, pc) in sorted({(r["doc_id"], r["pattern_class"]) for r in rows}):
        rs = [r for r in rows if r["doc_id"] == doc and r["pattern_class"] == pc]
        act = [r for r in rs if r["active"]]
        added = sum(1 for r in act if (r.get("review") or "").startswith("added_by_review"))
        gaps = sum(1 for r in act if (r.get("review") or "").startswith("added_by_coverage_check"))
        lines.append(f"{doc} / {pc}: {len(act)} active ({added} added by the second reader, {gaps} by the "
                     f"coverage check), "
                     f"{len(rs) - len(act)} not used")
        for r in rs:
            if not r["active"]:
                lines.append(f"    not used: {r['text'][:90]}\n              why: {r.get('review') or 'quote not found'}")
    lines.append("\nNumber rules (checked in code):")
    for r in rows:
        if r["active"] and r["check_type"] == "numeric":
            cond = db.unjs(r["condition"])
            band = (f"  when {cond['parameter']} {cond['operator']} {cond['value']:g}"
                    f"{'..' + format(cond['value2'], 'g') if cond.get('value2') is not None else ''}") if cond else ""
            v2 = f"..{r['value2']:g}" if r["value2"] is not None else ""
            unless = db.unjs(r.get("unless"), [])
            exc = ("  unless " + " and ".join(
                "an exception applies (judged)" if t["parameter"] == "exception_applies" else
                f"{t['parameter']} {t['operator']} {t['value']:g}" for t in unless)) if unless else ""
            lines.append(f"    {r['parameter']:34s} alert if {r['operator']} {r['value']:g}{v2}{band}{exc}")
    act = [dict(r, condition=db.unjs(r["condition"])) for r in rows if r["active"]]
    clash = contradictions(act)
    lines.append("\nContradictions between active number rules: " + ("none" if not clash else ""))
    lines += [f"    {c}" for c in clash]
    builds: dict[tuple, list[int]] = {}
    for a in db.q("SELECT id, details FROM audit_log WHERE action='rules_built' AND study_id=? ORDER BY id",
                  (study.study_id,)):
        d = json.loads(a["details"])
        builds.setdefault((d.get("doc_id"), d.get("pattern_class")), []).append(a["id"])
    for a in db.q("SELECT id, details FROM audit_log WHERE action='rule_contradiction_review' AND study_id=? "
                  "ORDER BY id", (study.study_id,)):
        d = json.loads(a["details"])
        ids = builds.get((d.get("doc_id"), d.get("pattern_class")), [])
        if len(ids) > 1 and a["id"] < ids[-2] or not ids or a["id"] > ids[-1]:
            continue  # from an earlier build, or a build that did not finish
        lines.append(f"    second reader flagged ({d.get('doc_id')}/{d.get('pattern_class')}): {d.get('detail')}")
    return "\n".join(lines)


# "5.2 Inclusion Criteria", "5.2.1 Master Protocol Inclusion Criteria", "4.2.2 Regimen-Specific Appendix Exclusion Criteria"
_CRITERIA = re.compile(r"(?im)^\s*(\d+(?:\.\d+)*)\s+(?:[\w-]+\s+){0,3}(?:INCLUSION|EXCLUSION) CRITERIA\b")
_TOC_LINE = re.compile(r"(?:\.{2,}|\. \. \.|•|…).*\d+\s*$")
_NEXT_SECTION = re.compile(r"(?m)^\s*\d+\.\d+\s+[A-Z][A-Z ,/&-]{3,}$")
_NUMBERED_HEADING = re.compile(r"(?m)^\s*(\d+(?:\.\d+)*)\s+[A-Z][A-Za-z]")


def _section_end(text: str, start: int, number: str) -> int:
    """Where the section numbered `number` (e.g. "5.2") ends: at the next heading at its level or above
    ("5.3", "6", "6.1"), however it is capitalised, or at an all-capitals numbered heading."""
    own = [int(x) for x in number.split(".")]
    ends = []
    caps = _NEXT_SECTION.search(text, start)
    if caps:
        ends.append(caps.start())
    for m in _NUMBERED_HEADING.finditer(text, start):
        num = [int(x) for x in m.group(1).split(".")]
        if len(num) <= len(own) and num[:-1] == own[:len(num) - 1] and num[-1] in (own[len(num) - 1] + 1,
                                                                                   own[len(num) - 1] + 2):
            ends.append(m.start())
            break
    return min(ends) if ends else len(text)


def checklist_items(study_id: str, doc_id: str, kind: str) -> list[str]:
    """The units every one of which needs a rule: each bullet of the protocol's inclusion and exclusion sections,
    and each requirement sentence of a lab manual."""
    text, _ = doc_text(study_id, doc_id)
    if kind != "protocol":
        return passages(text)
    items = []
    for m in _CRITERIA.finditer(text):
        if _TOC_LINE.search(text[m.start():text.find("\n", m.start())]):
            continue  # the table of contents
        section = text[m.end():_section_end(text, m.end(), m.group(1))]
        items += [p for p in passages(section) if _BULLET.match(p)]
    norm = [textutil.normalize(i) for i in items]
    keep = [(i, n) for i, n in zip(items, norm) if not any(o != n and o.startswith(n) for o in norm)]
    return [i for k, (i, n) in enumerate(keep) if n not in {m for _, m in keep[:k]}]  # a parent heading repeats its subsections


def checklist(study: Study) -> tuple[str, dict]:
    """Every criterion and requirement in the documents, with the rule that covers it or why none does."""
    rules = [dict(r) for r in db.q("SELECT id, active, review, source_quote FROM rules WHERE study_id=?",
                                   (study.study_id,))]
    lines, totals = [], {"covered": 0, "dropped": 0, "missing": 0}
    for doc in study.documents:
        if doc["kind"] not in ("protocol", "lab_manual"):
            continue
        items = checklist_items(study.study_id, doc["id"], doc["kind"])
        lines.append(f"\n{doc['title']} ({len(items)} items)")
        for item in items:
            hits = [r for r in rules if _covers(r["source_quote"], item)
                    and not bundles_criteria(r["source_quote"], items)]
            live = [r["id"] for r in hits if r["active"]]
            if not live and any("duplicate" in (r["review"] or "") for r in hits):
                live = ["(same as another kept rule)"]
            if live:
                totals["covered"] += 1
                mark, why = "covered", ", ".join(live[:3])
            elif hits:
                totals["dropped"] += 1
                mark, why = "dropped", (hits[0]["review"] or "")[:110]
            else:
                totals["missing"] += 1
                mark, why = "MISSING", ""
            lines.append(f"  [{mark:7s}] {item[:110]}" + (f"\n             {why}" if why else ""))
    head = (f"Coverage checklist for {study.study_id}: {totals['covered']} covered, {totals['dropped']} considered "
            f"and dropped (reason shown), {totals['missing']} missing")
    return head + "\n" + "\n".join(lines), totals


def import_model(src: Path, reason: str, actor: str = "operator", only: list[str] | None = None) -> dict:
    """Replace this database's study model (documents, pages, rules, guide) with the one in another cormon database,
    keeping messages, items and the log. Used to move a model rebuilt and tested elsewhere onto the live bot."""
    import shutil
    import sqlite3
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "model.db"
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(tmp)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    c = db.conn()
    counts = {}
    c.execute("ATTACH DATABASE ? AS model", (str(tmp),))
    try:
        has = lambda tbl: bool(c.execute(f"PRAGMA model.table_info({tbl})").fetchall())
        studies = [r[0] for r in c.execute("SELECT study_id FROM model.rules UNION SELECT study_id FROM model.guide"
                                           if has("guide") else "SELECT DISTINCT study_id FROM model.rules")]
        studies = [s for s in studies if not only or s in only]
        before = {sid: c.execute("SELECT COUNT(*) FROM main.rules WHERE study_id=? AND active=1",
                                 (sid,)).fetchone()[0] for sid in studies}
        with c:
            for tbl in ("documents", "doc_pages", "rules", "allowances", "rule_decisions", "guide", "llm_cache"):
                cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({tbl})")]
                have = {r[1] for r in c.execute(f"PRAGMA model.table_info({tbl})")}
                names = ", ".join(col for col in cols if col in have)
                if not names or (tbl == "guide" and not has("guide")):
                    continue
                marks = ",".join("?" * len(studies))
                where, args = "", []
                if tbl != "llm_cache":
                    c.execute(f"DELETE FROM main.{tbl} WHERE study_id IN ({marks})", studies)
                    where, args = f" WHERE study_id IN ({marks})", studies
                cur = c.execute(f"INSERT OR IGNORE INTO main.{tbl} ({names}) SELECT {names} FROM model.{tbl}{where}",
                                args)
                counts[tbl] = cur.rowcount
            for sid in studies:
                c.execute("INSERT OR REPLACE INTO main.state (key, value) SELECT key, value FROM model.state "
                          "WHERE (key LIKE 'ingest:%' OR key LIKE 'allowances:%') AND key LIKE ?", (f"%{sid}%",))
    finally:
        c.execute("DETACH DATABASE model")
        shutil.rmtree(tmp.parent, ignore_errors=True)
    for sid in studies:
        after = db.q1("SELECT COUNT(*) n FROM rules WHERE study_id=? AND active=1", (sid,))["n"]
        db.audit("study_model_imported", entity_type="study", entity_id=sid, study_id=sid, actor=actor,
                 actor_type="operator", source=str(src), reason=reason, active_rules_before=before[sid],
                 active_rules_after=after, rows=counts)
    return counts


def active_rules(study_id: str, pattern_classes: list[str] | None = None) -> list[dict]:
    sql = "SELECT * FROM rules WHERE study_id=? AND active=1"
    params: list = [study_id]
    if pattern_classes is not None:
        if not pattern_classes:
            return []
        sql += f" AND pattern_class IN ({','.join('?' * len(pattern_classes))})"
        params += pattern_classes
    sql += " ORDER BY pattern_class, doc_id, order_idx"
    out = []
    for r in db.q(sql, params):
        d = dict(r)
        d["applies_to"] = db.unjs(d["applies_to"], [])
        d["condition"] = db.unjs(d["condition"])
        d["unless"] = db.unjs(d.get("unless"), [])
        out.append(d)
    return out

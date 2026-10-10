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

        return [(p.extract_text() or "") for p in PdfReader(str(path)).pages]
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".md":
        parts = re.split(r"(?m)^(?=## )", text)
        return [p for p in parts if p.strip()]
    return [text]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
- descriptions of what the study is, or what will happen in general.

Numbers, ranges and alternatives:
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
    not_stated: the document does not say it;
    other_class: it belongs to a different problem class;
    duplicate: it says the same as another rule in the list; give that rule's number in duplicate_of and keep that one;
    wrong_numbers: its limits are wrong; add the corrected rule;
    not_atomic: it bundles several conditions (rules marked SPLIT THIS); add one rule per condition.
- add: requirements of this problem class that the document states and the list misses, written to the same standard as the list (one condition per rule, a VERBATIM source_quote of 10 to 60 words, the numeric fields where the rule is a threshold). Include corrected versions of rules you dropped for wrong numbers, and the parts of rules you dropped as not_atomic. Time windows ("within 28 days", "plus or minus 30 days") are numeric rules on a days parameter. Do not add anything the document does not state.
- contradictions: rules that cannot all be true at once, or numeric rules that would flag values the document allows (for example two rules on the same score and band, or bands that should be alternatives treated as requirements).

When the document gives alternative allowed ranges, a value is acceptable if it fits ANY of them. When a limit depends on another value, there must be one rule per band, bands must not overlap, and every band in the document must be covered.

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

A limit for a different band (another education or age level) is not an exception: each band is already its own rule, and the rule's band is shown. When there is a real exception and it can be tested on numbers, set has_exception, give the tests that together make the exception in unless (all must hold; use the parameter names listed when they fit, else a short snake_case name with the unit), and copy the words that state the exception VERBATIM into exception_quote. When the exception cannot be tested on numbers (an approval, a judgment), set has_exception with an empty unless and quote it. Otherwise has_exception is false, unless is empty and exception_quote is empty. Most rules have no exception."""

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
        f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n\n"
        f"DOCUMENT TEXT:\n{body}"
    )


def extract_rules(study: Study, doc_id: str, pattern: dict, pages: list[str]) -> list[dict]:
    out = llm.structured("ingest", RULE_SYSTEM, _context(study, doc_id, pattern, pages), RULE_SCHEMA, "study_rules")
    return out["rules"]


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip()) if len(s) > 15])


def _describe(n: int, r: dict) -> str:
    line = f"{n}. [{r['check_type']}] {r['text']}"
    if _sentences(r["text"]) >= 3:
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

_BULLET = re.compile(r"^\s*(?:[•o\-\*▪]|\d{1,2}[.)]|[a-z][.)])\s+\S")
_MUST = re.compile(r"(?i)\b(must|should|shall|required?|within|at least|no (?:more|later|less) than|not (?:be|allowed|"
                   r"permitted)|only|exclu|prior to|before|after|never|may not|cannot|eligible|fluent|minimum|maximum)\b")


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
    out += [s for s in re.split(r"(?<=[.;])\s+(?=[A-Z•])", flat) if _MUST.search(s)]
    seen, keep = set(), []
    for p in out:
        p = re.sub(r"\s+", " ", p).strip()
        k = textutil.normalize(p)
        if 25 <= len(p) <= 600 and k not in seen:
            seen.add(k)
            keep.append(p)
    return keep


def _covers(quote: str, passage: str) -> bool:
    import difflib
    q, p = textutil.normalize(quote), textutil.normalize(passage)
    if q in p or p in q:
        return True
    m = difflib.SequenceMatcher(None, q, p, autojunk=False).find_longest_match(0, len(q), 0, len(p))
    return m.size >= min(30, len(p) // 2)


def uncovered(rules: list[dict], text: str) -> list[str]:
    """Passages that no rule, kept or dropped, was taken from."""
    quotes = [r["source_quote"] for r in rules]
    return [p for p in passages(text) if not any(_covers(q, p) for q in quotes)]


def fill_gaps(study: Study, doc_id: str, pattern: dict, rules: list[dict], text: str) -> list[dict]:
    """Third reader, driven by code: show the model only the passages nothing was taken from."""
    gaps = uncovered(rules, text)
    if not gaps:
        return []
    doc = study.doc(doc_id)
    user = (f"Study: {study.title} ({study.study_id})\nDocument: {doc['title']} (kind: {doc['kind']})\n\n"
            f"Problem class: {pattern['id']} - {pattern['label']}\nDefinition: {pattern['description']}\n"
            f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n\nPASSAGES:\n"
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
    items = [i for i in checklist_items(study.study_id, doc_id, doc["kind"]) if not any(_covers(q, i) for q in have)]
    if not items:
        return []
    log(f"  {doc_id} / {pattern['id']}: {len(items)} criteria still without a rule; asking for each one ...")
    user = (f"Study: {study.title} ({study.study_id})\nDocument: {doc['title']} (kind: {doc['kind']})\n\n"
            f"Problem class: {pattern['id']} - {pattern['label']}\nDefinition: {pattern['description']}\n"
            f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n\nPASSAGES:\n"
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
        found = set(_NUMBER.findall(r.get("text") or ""))
        found |= {f"{v:g}" for v in (r.get("value"), r.get("value2")) if v is not None}
        return {n.rstrip("0").rstrip(".") if "." in n else n for n in found}
    have = set().union(*(nums(a) for a in replacements)) if replacements else set()
    return nums(rule) - have


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
        if d["why"] == "wrong_numbers" and r["check_type"] == "numeric" and r["parameter"] not in added_params:
            r["review"] = f"kept: review said the numbers are wrong but gave no correction ({d['reason']})"
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
    """When the model glued a heading onto a table row ("For early MCI: 5-9 for 8-15 years"), keep the longest
    piece that is really in the document and still carries the rule's numbers."""
    pieces = [p.strip() for p in re.split(r"[:;\n•]|\s{2,}", rule["source_quote"]) if p.strip()]
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
    """A rule dropped as bundled or as having wrong numbers comes back when none of the rules meant to replace it
    survived the quote check."""
    back = 0
    for r in rules:
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


def lint(rules: list[dict]) -> list[tuple[int, str]]:
    """Checks made in code, whatever the model said: (rule index, reason to drop)."""
    out, seen = [], {}
    for i, r in enumerate(rules):
        if _JUNK.search(r["text"]) or _JUNK.search(r["source_quote"]):
            out.append((i, "not a checkable requirement (form or signature text)"))
            continue
        if r["check_type"] == "numeric" and (not r["parameter"] or not r["operator"] or r["value"] is None):
            r.update(check_type="judgment", parameter=None, operator=None, value=None, value2=None)
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


def _compatible(limits: set[tuple]) -> bool:
    """A lower and an upper limit on the same value (lt 55 and gt 90) are fine together."""
    lows = {o for o, _, _ in limits if o in ("lt", "le")}
    highs = {o for o, _, _ in limits if o in ("gt", "ge")}
    return len(limits) == 2 and len(lows) == 1 and len(highs) == 1


def ingest_study(study: Study, force: bool = False, log=print) -> dict:
    summary = {"documents": 0, "rules": 0, "unverified": 0, "skipped": 0}
    for doc in study.documents:
        doc_id = doc["id"]
        sha, pages, changed = store_document(study, doc_id)
        summary["documents"] += 1
        full_text, starts = doc_text(study.study_id, doc_id)
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
            log(f"    {counts['active']} active ({counts['added_by_review']} added by the second reader, "
                f"{counts['added_by_coverage_check']} by the coverage check), "
                f"{counts['dropped']} dropped, {counts['quote_not_found']} rejected (quote not in the document), "
                f"{counts['repaired_quotes']} quotes repaired, {counts['merged']} either-or merges, {turned} limits turned round, {exceptions} exceptions, "
                f"{counts['rescued']} rescued" + (f", CONTRADICTIONS: {clash}" if clash else ""))
    return summary


def store_rules(study: Study, doc_id: str, pattern: dict, sha: str, rules: list[dict], full_text: str,
               starts: list[int]) -> dict:
    """Verify each rule's quote against the document and store the lot (inactive rules are kept for the record)."""
    db.ex("DELETE FROM rules WHERE study_id=? AND doc_id=? AND pattern_class=?",
          (study.study_id, doc_id, pattern["id"]))
    counts = {"active": 0, "dropped": 0, "quote_not_found": 0, "added_by_review": 0, "added_by_coverage_check": 0,
              "repaired_quotes": 0,
              "merged": 0}
    for r in rules:
        r["_origin"] = r.get("review") or ""
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


_CRITERIA = re.compile(r"(?im)^\s*\d+(?:\.\d+)*\s+(?:INCLUSION|EXCLUSION) CRITERIA\b")
_NEXT_SECTION = re.compile(r"(?m)^\s*\d+\.\d+\s+[A-Z][A-Z ,/&-]{3,}$")


def checklist_items(study_id: str, doc_id: str, kind: str) -> list[str]:
    """The units every one of which needs a rule: each bullet of the protocol's inclusion and exclusion sections,
    and each requirement sentence of a lab manual."""
    text, _ = doc_text(study_id, doc_id)
    if kind != "protocol":
        return passages(text)
    items = []
    for m in _CRITERIA.finditer(text):
        if "...." in text[m.start():text.find("\n", m.start())]:
            continue  # the table of contents
        nxt = _NEXT_SECTION.search(text, m.end())
        section = text[m.end():nxt.start() if nxt else len(text)]
        items += [p for p in passages(section) if _BULLET.match(p)]
    norm = [textutil.normalize(i) for i in items]
    return [i for i, n in zip(items, norm) if not any(o != n and o.startswith(n) for o in norm)]


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
            hits = [r for r in rules if _covers(r["source_quote"], item)]
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


def import_model(src: Path, reason: str, actor: str = "operator") -> dict:
    """Replace this database's study model (documents, pages, rules) with the one in another cormon database,
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
        studies = [r[0] for r in c.execute("SELECT DISTINCT study_id FROM model.rules")]
        before = {sid: c.execute("SELECT COUNT(*) FROM main.rules WHERE study_id=? AND active=1",
                                 (sid,)).fetchone()[0] for sid in studies}
        with c:
            for tbl in ("documents", "doc_pages", "rules", "llm_cache"):
                cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({tbl})")]
                have = {r[1] for r in c.execute(f"PRAGMA model.table_info({tbl})")}
                names = ", ".join(col for col in cols if col in have)
                if tbl != "llm_cache":
                    marks = ",".join("?" * len(studies))
                    c.execute(f"DELETE FROM main.{tbl} WHERE study_id IN ({marks})", studies)
                cur = c.execute(f"INSERT OR IGNORE INTO main.{tbl} ({names}) SELECT {names} FROM model.{tbl}")
                counts[tbl] = cur.rowcount
            c.execute("INSERT OR REPLACE INTO main.state (key, value) SELECT key, value FROM model.state "
                      "WHERE key LIKE 'ingest:%'")
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

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

OPERATORS = ["lt", "le", "gt", "ge", "eq", "ne", "outside"]


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

RULE_SCHEMA = llm.obj({
    "rules": {
        "type": "array",
        "items": llm.obj({
            "kind": {"type": "string", "enum": [
                "inclusion", "exclusion", "requirement", "prohibition", "window", "sequence"]},
            "text": {"type": "string"},
            "check_type": {"type": "string", "enum": ["numeric", "judgment"]},
            "parameter": llm.nullable("string"),
            "operator": {"type": ["string", "null"], "enum": OPERATORS + [None]},
            "value": llm.nullable("number"),
            "value2": llm.nullable("number"),
            "unit": llm.nullable("string"),
            "condition": llm.nullable(llm.obj({
                "parameter": {"type": "string"},
                "operator": {"type": "string", "enum": OPERATORS},
                "value": {"type": "number"},
            })),
            "applies_to": {"type": "array", "items": {"type": "string", "enum": EVENT_TYPES}},
            "source_quote": {"type": "string"},
            "section": {"type": "string"},
        }),
    }
})

RULE_SYSTEM = """You convert clinical trial documents into atomic, checkable rules.

Write rules, not narrative. Each rule is ONE condition, phrased as an instruction a checker can apply to a single piece of correspondence, e.g.
  "Exclude if eGFR is below 45 mL/min at screening."
  "Plasma tubes must be centrifuged within 60 minutes of the blood draw."
  "Study drug must not start before all brain imaging is complete."
Split compound criteria into separate rules. Keep the document's order.

Only extract rules that belong to the requested problem class. Do not invent rules, thresholds or units that the document does not state. Ignore background, rationale, statistics, and anything about assessing safety or adverse events.

For every rule:
- source_quote: copy a contiguous passage VERBATIM from the document (10 to 60 words) that states the rule. Copy characters exactly as they appear, including odd spacing. Never paraphrase in this field.
- check_type "numeric" only when the rule is a threshold on one measurable value; then set:
    parameter: a snake_case name with the unit, e.g. egfr_ml_min, hba1c_percent, mmse_score, bmi_kg_m2, age_years, systolic_bp_mmhg, diastolic_bp_mmhg, gds_score, minutes_draw_to_centrifuge, hours_draw_to_freeze, hours_stored_minus20, fasting_hours
    operator/value/value2: the values that VIOLATE the rule (lt, le, gt, ge, eq, ne, or outside value..value2 for an allowed range).
    condition: a second numeric condition that must hold for the rule to apply (e.g. education_years gt 8), else null.
  Otherwise check_type "judgment" and the numeric fields are null.
- applies_to: the event types the rule should be checked against.
- section: the section number and heading the rule comes from."""


def _pattern_key(study: Study, doc_id: str, pattern: dict, sha: str) -> str:
    blob = json.dumps([sha, pattern, RULE_SYSTEM], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def extract_rules(study: Study, doc_id: str, pattern: dict, pages: list[str]) -> list[dict]:
    doc = study.doc(doc_id)
    body = "\n".join(f"[[page {i}]]\n{t}" for i, t in enumerate(pages, start=1))
    user = (
        f"Study: {study.title} ({study.study_id})\n"
        f"Document: {doc['title']} (kind: {doc['kind']})\n\n"
        f"Problem class to extract rules for: {pattern['id']} - {pattern['label']}\n"
        f"Definition: {pattern['description']}\n"
        f"Relevant event types: {', '.join(pattern.get('event_types', []))}\n\n"
        f"DOCUMENT TEXT:\n{body}"
    )
    out = llm.structured("ingest", RULE_SYSTEM, user, RULE_SCHEMA, "study_rules")
    return out["rules"]


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
            rules = extract_rules(study, doc_id, pattern, pages)
            db.ex("DELETE FROM rules WHERE study_id=? AND doc_id=? AND pattern_class=?",
                  (study.study_id, doc_id, pattern["id"]))
            n_ok = n_bad = 0
            for idx, r in enumerate(rules):
                span = best_span(r["source_quote"], full_text, starts, r["section"])
                verified = span is not None
                page = page_of(starts, span[0]) if span else None
                exact = textutil.clean_spaces(full_text[span[0]:span[1]]) if span else r["source_quote"]
                rule_id = f"{study.short_name}-{doc_id}-{pattern['id']}-{idx + 1:03d}"
                db.ex(
                    "INSERT INTO rules (id, study_id, doc_id, doc_sha256, pattern_class, kind, text, check_type,"
                    " parameter, operator, value, value2, unit, condition, applies_to, source_quote, source_page,"
                    " section, verified, order_idx, active, created_at) VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rule_id, study.study_id, doc_id, sha, pattern["id"], r["kind"], r["text"],
                     r["check_type"], r["parameter"], r["operator"], r["value"], r["value2"], r["unit"],
                     db.js(r["condition"]) if r["condition"] else None, db.js(r["applies_to"]),
                     exact, page, r["section"], int(verified), idx, int(verified), db.now()),
                )
                if verified:
                    n_ok += 1
                else:
                    n_bad += 1
                    db.audit("rule_defect_unverified_source", entity_type="rule", entity_id=rule_id,
                             study_id=study.study_id, claimed_quote=r["source_quote"], text=r["text"])
            db.set_state(state_key, key)
            db.audit("rules_built", entity_type="study", entity_id=study.study_id, study_id=study.study_id,
                     doc_id=doc_id, pattern_class=pattern["id"], verified=n_ok, unverified=n_bad)
            summary["rules"] += n_ok
            summary["unverified"] += n_bad
            log(f"    {n_ok} rules verified against the document, {n_bad} rejected (quote not found)")
    return summary


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
        out.append(d)
    return out

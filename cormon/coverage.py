"""The coverage gate: what the study model may be missing, checked after each build and shown by `cormon status`.

The checker reads the topic briefs, so a site rule in no brief line is never checked by the AI. The gate looks for:
- documents listed for the study that the checker does not read, or reads but has no sections, brief lines or
  checks for;
- sections with no topic at all (left out of every brief);
- rule-like sentences (in any section, background and admin included) whose words are in no brief line. Code
  finds them; the AI then reads each with the brief lines nearest to it and says whether one of them already says
  it (covered), it is a site rule no line has (missing), or it is not something a site does (not_a_site_rule).
Missing sentences are the warnings; the rest is kept in the report as evidence.
"""
from __future__ import annotations

import json
import re

from . import db, guide, llm, packs, settings
from .config import Study

_STOP = set("the a an and or of to in on for is are be was were with by at as that this it its not no from any all "
            "will shall must should may can each per than then into such these those their there which when where "
            "who whom have has had been being also only other more most".split())

JUDGE_SYSTEM = """You audit the brief of a clinical-trial monitor: short lines, each one rule a study site must
follow, written from the study documents. For each numbered sentence from a document you get the brief lines most
like it. Besides the brief, the monitor gets every visit's checklist (each row of the schedule of activities: what
is done at which visit, in which window) and the documents' medicine tables row by row, applied with the rule
that introduces each table. Say:
- covered: one of the lines (or two together) says what the sentence requires, with the same numbers and
  conditions; or the sentence is a schedule-of-activities row, a visit's list of procedures, or a medicine table
  row, which the checklists and medicine tables carry;
- missing: the sentence states something the site staff must do, must not do or may do (a requirement, limit,
  timing, condition, exception or required step) and no line given says it, or a line says it with a different
  number or condition;
- not_a_site_rule: rationale, background, results, statistics, a definition with no duty, a pointer to another
  document ("details will be in the RSA"), a general statement that the study follows laws, GCP, ICH or IRB rules,
  or a duty of the sponsor, a committee or a vendor rather than the site.
For missing, say in a few words what the lines lack."""

JUDGE_SCHEMA = llm.obj({"sentences": {"type": "array", "items": llm.obj({
    "n": {"type": "integer"},
    "verdict": {"type": "string", "enum": ["covered", "missing", "not_a_site_rule"]},
    "lacks": {"type": "string"}})}})


def _stems(text: str) -> set[str]:
    return {w[:6] for w in re.findall(r"[a-z0-9]+", text.lower()) if (len(w) > 3 or w.isdigit()) and w not in _STOP}


def _brief_lines(rows: list[dict]) -> list[str]:
    return list(dict.fromkeys(ln for r in rows if r["kind"] == "brief" for ln in r["text"].splitlines()
                              if ln.startswith("- ")))


def candidates(secs: list[dict], lines: list[str], overlap: float = 0.5) -> list[dict]:
    """Rule-like sentences whose words no brief line shares by `overlap` or more, each with its nearest lines."""
    stems = [_stems(ln) for ln in lines]
    out = []
    for s in secs:
        for sid, sent in packs._uncovered([s], {}):
            w = _stems(sent)
            if not w:
                continue
            score = sorted(((len(w & st) / len(w), n) for n, st in enumerate(stems)), reverse=True)
            if not score or score[0][0] < overlap:
                out.append({"section": sid, "topics": s["topics"], "sentence": sent,
                            "nearest": [lines[n] for _, n in score[:6]]})
    return out


def judge(cands: list[dict], use_cache: bool = True, batch: int = 20) -> None:
    """Sets verdict and lacks on each candidate (see JUDGE_SYSTEM)."""
    for k in range(0, len(cands), batch):
        part = cands[k:k + batch]
        user = "\n\n".join(f"{n}. [{c['section']}] {c['sentence']}\nNEAREST BRIEF LINES:\n" + "\n".join(c["nearest"])
                           for n, c in enumerate(part, 1))
        res = llm.structured("ingest_brief", JUDGE_SYSTEM, user, JUDGE_SCHEMA, "coverage", use_cache=use_cache,
                             model=llm.model_for("ingest"))
        got = {r["n"]: r for r in res["sentences"]}
        for n, c in enumerate(part, 1):
            r = got.get(n) or {"verdict": "missing", "lacks": "no answer from the audit"}
            c.update(verdict=r["verdict"], lacks=r.get("lacks", ""))


def gate(study: Study, use_cache: bool = True, ai: bool = True, log=print) -> dict:
    """Run the gate on the stored model, store the result (state, audit log, report file) and log its warnings."""
    rows = guide.load(study.study_id)
    read = guide.documents(study)
    listed = [d["id"] for d in study.documents if d.get("guide_file")]
    secs = [r for r in rows if r["kind"] == "section" and r["text"].strip() and r["doc_id"] in read]
    lines = _brief_lines(rows)
    cited = {x for ln in lines for x in re.findall(r"\[([^\]\s]+)\]\s*$", ln)}
    checked = {json.loads(r["text"]).get("section_id", "") for r in rows if r["kind"] == "check"}
    has = lambda ids, d: any(i.split(":")[0] == d for i in ids)  # noqa: E731
    docs = {
        "not_read": [d for d in listed if d not in read],
        "unknown": [d for d in read if d not in {x["id"] for x in study.documents}],
        "no_sections": [d for d in read if not any(s["doc_id"] == d for s in secs)],
        "no_brief_line": [d for d in read if not has(cited, d)],
        "no_check": [d for d in read if not has(checked, d)] if checked else [],
    }
    no_topic = [s["id"] for s in secs if not s["topics"]]
    cands = candidates(secs, lines)
    if ai and cands:
        judge(cands, use_cache)
    missing = [c for c in cands if c.get("verdict") == "missing"]
    result = {"study_id": study.study_id, "documents": docs, "sections_with_no_topic": no_topic,
              "sentences_checked": sum(len(packs._uncovered([s], {})) for s in secs),
              "candidates": len(cands), "judged": ai,
              "missing": [{k: c[k] for k in ("section", "sentence", "lacks")} for c in missing],
              "not_a_site_rule": sum(c.get("verdict") == "not_a_site_rule" for c in cands),
              "covered_reworded": sum(c.get("verdict") == "covered" for c in cands)}
    warnings = ([f"{k.replace('_', ' ')}: {', '.join(v)}" for k, v in docs.items() if v]
                + ([f"{len(no_topic)} sections with no topic: {' '.join(no_topic[:10])}"] if no_topic else [])
                + ([f"{len(missing)} site rules in no brief line" if ai else
                    f"{len(cands)} rule-like sentences whose words are in no brief line (not yet judged)"]
                   if (missing if ai else cands) else []))
    result["warnings"] = warnings
    db.set_state(f"coverage_gate:{study.study_id}", json.dumps({"warnings": warnings, "at": db.now()}))
    db.audit("coverage_gate", entity_type="study", entity_id=study.study_id, study_id=study.study_id,
             warnings=warnings, candidates=len(cands), missing=len(missing))
    out = settings.data_dir() / "guides" / f"{study.study_id}-coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dict(result, judged_sentences=cands), indent=1, ensure_ascii=False))
    log(f"coverage gate: {result['sentences_checked']} rule-like sentences, {len(cands)} not plainly in a brief line"
        + (f": {result['covered_reworded']} covered in other words, {result['not_a_site_rule']} not site rules, "
           f"{len(missing)} missing" if ai else "") + f" -> {out}")
    for w in warnings:
        log(f"  WARNING {w}")
    for m in result["missing"][:30]:
        log(f"    [{m['section']}] {m['sentence'][:160]}  ({m['lacks']})")
    return result


def last(study_id: str) -> dict | None:
    raw = db.get_state(f"coverage_gate:{study_id}")
    return json.loads(raw) if raw else None

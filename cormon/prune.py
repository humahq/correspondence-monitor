"""Rule pruning: the AI suggests which rules to keep or remove, and people decide (the AI never removes a rule).

Suggestions and decisions live in `rule_decisions`, keyed on the rule's words and check so they survive a rebuild.
A rule the team marks "remove" is turned off; marking it "keep" again turns it back on.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db, llm
from .config import Study

REMOVED = "removed by team decision"

SUGGEST_SYSTEM = """You help a clinical trial team trim the rules that a correspondence monitor checks emails against. Fewer rules make the checker more accurate, but a rule wrongly removed means a real problem is missed, so suggest remove only when you are confident. People make the final decision.

Suggest remove only for one of these reasons:
- already_checked_elsewhere: a step that is completed and recorded on a required form, checklist or the EDC at the moment it happens (e.g. the participant gives verbal consent while the intake person fills out the intake checklist), so watching emails for it adds little.
- study_start_only: applies only to setting up the study or the site (site initiation, staff training, document approval), not to participants during the running study.
- cannot_appear_in_email: nothing a site would write in an email could show this rule being broken.
- duplicate: checks exactly the same thing as another rule of the same problem type (give its id in duplicate_of). Rules with the same words but a different check (a different value, limit or condition, e.g. one for more and one for fewer years of education, or systolic and diastolic) are NOT duplicates.

Everything else: keep (reason_type keep). Eligibility criteria, visit windows, procedure order, sample handling and consent timing apply to every participant throughout the study: keep them. When in doubt, keep.

why: one short plain sentence a busy person can read."""

REASONS = ["keep", "already_checked_elsewhere", "study_start_only", "cannot_appear_in_email", "duplicate"]


def rule_key(r: dict) -> str:
    """Two rules can share words and quote but check different things (systolic and diastolic from one sentence)."""
    cond = r.get("condition")
    if isinstance(cond, str):
        cond = db.unjs(cond)
    return json.dumps([r["doc_id"], r["pattern_class"], r["text"], r["source_quote"], r.get("parameter"),
                       r.get("operator"), r.get("value"), r.get("value2"), cond], sort_keys=True, ensure_ascii=False)


def _rules(study: Study, include_removed: bool = False) -> list[dict]:
    sql = "SELECT * FROM rules WHERE study_id=? AND (active=1"
    sql += f" OR review LIKE '{REMOVED}%')" if include_removed else ")"
    return [dict(r) for r in db.q(sql + " ORDER BY doc_id, pattern_class, order_idx", (study.study_id,))]


def decisions(study_id: str) -> dict[str, dict]:
    """rule_key -> suggestion and decision"""
    return {r["rule_key"]: dict(r) for r in db.q("SELECT * FROM rule_decisions WHERE study_id=?", (study_id,))}


def suggest(study: Study, *, batch: int = 30, use_cache: bool = True) -> dict:
    """Ask the AI for keep/remove on every rule in use; stores the suggestion next to any decision already made."""
    rules = _rules(study, include_removed=True)
    titles = {d["id"]: d["title"] for d in study.documents}
    answers: dict[str, dict] = {}
    for i in range(0, len(rules), batch):
        part = rules[i:i + batch]
        schema = llm.obj({"rules": {"type": "array", "items": llm.obj({
            "rule_id": {"type": "string", "enum": [r["id"] for r in part]},
            "suggest": {"type": "string", "enum": ["keep", "remove"]},
            "reason_type": {"type": "string", "enum": REASONS},
            "duplicate_of": llm.nullable("string"),
            "why": {"type": "string"},
        })}})
        others = "\n".join(f"{r['id']} [{r['pattern_class']}]: {r['text']}{_check(r)}" for r in rules if r not in part)
        lines = "\n".join(f"{r['id']} [{r['pattern_class']}, {titles.get(r['doc_id'], r['doc_id'])}]: "
                          f"{r['text']}{_check(r)}\n   document says: \"{r['source_quote'][:300]}\"" for r in part)
        user = (f"Study: {study.title}\n\nRULES TO ASSESS (one answer each):\n{lines}\n\n"
                f"OTHER RULES IN THE LIST (for spotting duplicates only):\n{others}")
        out = llm.structured("prune_suggest", SUGGEST_SYSTEM, user, schema, "rule_pruning", use_cache=use_cache)
        ids = {r["id"] for r in part}
        answers.update({s["rule_id"]: s for s in out["rules"] if s["rule_id"] in ids})
    counts = {"keep": 0, "remove": 0}
    for r, (suggest_, reason, why) in zip(rules, _settle(rules, answers)):
        _store(r, suggest=suggest_, reason_type=reason, why=why)
        counts[suggest_] += 1
    db.audit("rule_pruning_suggested", entity_type="study", entity_id=study.study_id, study_id=study.study_id,
             **counts)
    return counts


def _check(r: dict) -> str:
    if r["check_type"] != "numeric" or r.get("value") is None:
        return ""
    cond = r.get("condition")
    cond = db.unjs(cond) if isinstance(cond, str) else cond
    limit = f"{r['value']:g}" + (f"-{r['value2']:g}" if r.get("value2") is not None else "")
    return f"  (check: {r['parameter']} {r['operator']} {limit}" + (f", only when {cond}" if cond else "") + ")"


def _same_check(a: dict, b: dict) -> bool:
    fields = ("check_type", "parameter", "operator", "value", "value2")
    cond = [db.unjs(x["condition"]) if isinstance(x.get("condition"), str) else x.get("condition") for x in (a, b)]
    return all(a.get(f) == b.get(f) for f in fields) and cond[0] == cond[1]


def _settle(rules: list[dict], answers: dict[str, dict]) -> list[tuple[str, str, str]]:
    """(suggest, reason_type, why) per rule. A duplicate is only suggested for removal when its twin is a rule of the
    same problem type with the same check that itself stays, so following every suggestion never loses a check."""
    by_id = {r["id"]: r for r in rules}
    out: dict[str, tuple[str, str, str]] = {}
    for r in rules:
        s = answers.get(r["id"])
        if s is None:
            out[r["id"]] = ("keep", "keep", "No suggestion was given, so it stays.")
        elif s["suggest"] != "remove" or s["reason_type"] == "keep":
            out[r["id"]] = ("keep", "keep", s["why"])
        else:
            out[r["id"]] = ("remove", s["reason_type"], s["why"])
    for r in rules:  # in rule order, so of two rules naming each other the first one stays
        s = answers.get(r["id"])
        if out[r["id"]][0] != "remove" or s["reason_type"] != "duplicate":
            continue
        twin = by_id.get((s.get("duplicate_of") or "").strip())
        problem = ("it names no rule in the list" if twin is None or twin is r else
                   f"{twin['id']} is a different problem type" if twin["pattern_class"] != r["pattern_class"] else
                   f"{twin['id']} checks something different" if not _same_check(r, twin) else
                   f"{twin['id']} is also suggested for removal" if out[twin["id"]][0] == "remove" else None)
        if problem:
            out[r["id"]] = ("keep", "keep", f"Kept: the AI called it a duplicate, but {problem}. ({s['why']})")
    return [out[r["id"]] for r in rules]


def _store(r: dict, **fields) -> None:
    key = rule_key(r)
    db.ex("INSERT OR IGNORE INTO rule_decisions (study_id, rule_key, source_quote, text) VALUES (?,?,?,?)",
          (r["study_id"], key, r["source_quote"], r["text"]))
    sets = ", ".join(f"{k}=?" for k in fields)
    db.ex(f"UPDATE rule_decisions SET {sets} WHERE study_id=? AND rule_key=?", (*fields.values(), r["study_id"], key))


def decide(study: Study, rule_id: str, decision: str, actor: str, note: str = "") -> bool:
    """Record a person's keep/remove for one rule and turn the rule off or back on. Returns True if it changed."""
    r = db.q1("SELECT * FROM rules WHERE id=? AND study_id=?", (rule_id, study.study_id))
    if r is None:
        raise ValueError(f"no rule {rule_id}")
    r = dict(r)
    old = decisions(study.study_id).get(rule_key(r), {}).get("decision")
    if old == decision:
        return False
    _store(r, decision=decision, decided_by=actor, decided_at=db.now(), note=note or None)
    _apply(r, decision, actor, note)
    db.audit("rule_decided", entity_type="rule", entity_id=rule_id, study_id=study.study_id, actor=actor,
             actor_type="operator", decision=decision, before=old, note=note, text=r["text"])
    return True


def _apply(r: dict, decision: str | None, actor: str, note: str) -> None:
    removed = (r.get("review") or "").startswith(REMOVED)
    if decision == "remove" and r["active"]:
        why = f"{REMOVED} ({actor}, {db.now()[:10]})" + (f": {note}" if note else "")
        db.ex("UPDATE rules SET active=0, review=? WHERE id=?", (why, r["id"]))
    elif decision == "keep" and removed:
        db.ex("UPDATE rules SET active=1, review=NULL WHERE id=?", (r["id"],))


def reapply(study_id: str, doc_id: str | None = None, pattern_class: str | None = None) -> int:
    """After a rebuild: turn off again every rule the team removed (matched on its words). Returns how many."""
    made = decisions(study_id)
    sql, params = "SELECT * FROM rules WHERE study_id=? AND active=1", [study_id]
    if doc_id:
        sql, params = sql + " AND doc_id=? AND pattern_class=?", params + [doc_id, pattern_class]
    n = 0
    for r in db.q(sql, params):
        d = made.get(rule_key(dict(r)))
        if d and d["decision"] == "remove":
            _apply(dict(r), "remove", d["decided_by"] or "team", d["note"] or "")
            n += 1
    return n


def read_sheet(study: Study, path: Path, actor: str) -> dict:
    """Read the "Team decision" column of an exported rules spreadsheet and apply it."""
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True)["Rules in use"]
    rows = ws.iter_rows(values_only=True)
    head = [str(h or "").strip() for h in next(rows)]
    col_id, col_dec = head.index("Rule"), head.index("Team decision")
    col_note = head.index("Team note") if "Team note" in head else None
    counts = {"keep": 0, "remove": 0, "unchanged": 0, "blank": 0, "not understood": []}
    for row in rows:
        rid = row[col_id]
        if not rid:
            continue
        word = str(row[col_dec] or "").strip().lower()
        if not word:
            counts["blank"] += 1
            continue
        decision = {"keep": "keep", "k": "keep", "yes": "keep", "remove": "remove", "r": "remove",
                    "drop": "remove", "delete": "remove", "no": "remove"}.get(word)
        if decision is None:
            counts["not understood"].append(f"{rid}: {word}")
            continue
        note = str(row[col_note] or "").strip() if col_note is not None else ""
        changed = decide(study, str(rid), decision, actor, note)
        counts[decision if changed else "unchanged"] += 1
    return counts

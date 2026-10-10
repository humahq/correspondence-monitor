"""Matching an event against the study model, then confirming against the original document.

1. Candidate rules come only from the configured pattern classes whose event
   types fit the event: there is no open-ended search for "anything unusual".
2. Numeric rules are evaluated in code, so the same value always gives the same answer.
3. Judgment rules are checked by the model, one atomic rule at a time in a list.
4. Every finding is confirmed against the original document passage, and both
   quotes (correspondence and document) must be found verbatim before it is reported.
"""

from __future__ import annotations

import operator as op
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from . import db, ingest, llm, notes, settings, textutil
from .config import Study

_OPS = {"lt": op.lt, "le": op.le, "gt": op.gt, "ge": op.ge, "eq": op.eq, "ne": op.ne}


@dataclass
class Finding:
    rule: dict
    event: dict
    method: str                 # numeric | judgment
    message_quote: str
    reasoning: str
    certainty: str = "clear"    # clear | possible
    observed: str | None = None
    doc_quote: str | None = None
    doc_page: int | None = None
    doc_passage: str | None = None
    confirmation: str | None = None
    extra: dict = field(default_factory=dict)


def violates(value: float, operator: str, v1: float | None, v2: float | None) -> bool:
    if operator == "outside":
        return v1 is not None and v2 is not None and (value < v1 or value > v2)
    if operator == "between":
        return v1 is not None and v2 is not None and v1 <= value <= v2
    fn = _OPS.get(operator or "")
    return bool(fn and v1 is not None and fn(value, v1))


# a shipped sample was collected first, so the rules about the collected sample still apply to it
IMPLIED_EVENTS = {"sample_shipment": ["sample_collection"]}


def candidate_rules(study: Study, event: dict) -> list[dict]:
    classes = [p["id"] for p in study.pattern_classes
               if event["event_type"] in p.get("match_event_types", p.get("event_types", []))]
    rules = ingest.active_rules(study.study_id, classes)
    types = {event["event_type"], *IMPLIED_EVENTS.get(event["event_type"], [])}
    out = [r for r in rules if not r["applies_to"] or types & set(r["applies_to"])]
    if event["event_type"] == "other":
        # an unclassified event can still carry a measured value that a numeric rule checks in code
        params = {f["parameter"] for f in event["facts"] if f.get("numeric_value") is not None}
        seen = {r["id"] for r in out}
        out += [r for r in ingest.active_rules(study.study_id, [p["id"] for p in study.pattern_classes])
                if r["check_type"] == "numeric" and r["parameter"] in params and r["id"] not in seen]
    return out


def with_weekday(event: dict) -> dict:
    """Rules such as "ship Monday to Wednesday only" need the weekday, which the model does not work out reliably."""
    try:
        dt = dtparser.isoparse(event.get("event_datetime") or "")
    except (ValueError, TypeError):
        return event
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    local = dt.astimezone(tz) if dt.tzinfo else dt
    return dict(event, event_weekday=local.strftime("%A"))


_TO_KG = {"weight_kg": 1.0, "weight_lb": 0.45359237, "weight_lbs": 0.45359237}
_TO_M = {"height_m": 1.0, "height_cm": 0.01, "height_in": 0.0254, "height_inches": 0.0254}


def derived_facts(facts: dict[str, dict], wanted: set[str]) -> dict[str, dict]:
    """Values a rule needs that the email gives only through others (BMI from weight and height)."""
    out = {}
    for param in wanted - set(facts):
        if not param.startswith("bmi"):
            continue
        w = next(((f, k) for p, k in _TO_KG.items() if (f := facts.get(p))), None)
        h = next(((f, k) for p, k in _TO_M.items() if (f := facts.get(p))), None)
        if w and h and h[0]["numeric_value"] > 0:
            bmi = round(w[0]["numeric_value"] * w[1] / (h[0]["numeric_value"] * h[1]) ** 2, 1)
            out[param] = {"parameter": param, "value": f"{bmi} (worked out from weight and height)",
                          "numeric_value": bmi, "unit": "kg/m2", "quote": f"{w[0]['quote']} … {h[0]['quote']}"}
    return out


def numeric_checks(rules: list[dict], event: dict) -> tuple[list[Finding], list[dict]]:
    """Returns findings, plus numeric rules whose condition could not be evaluated."""
    facts = {f["parameter"]: f for f in event["facts"] if f.get("numeric_value") is not None}
    facts.update(derived_facts(facts, {r["parameter"] for r in rules if r["check_type"] == "numeric"}))
    worded = {f["parameter"] for f in event["facts"] if f.get("numeric_value") is None}
    found, undecided = [], []
    for r in rules:
        if r["check_type"] != "numeric":
            continue
        if r["parameter"] not in facts:
            if r["parameter"] in worded:
                undecided.append(r)  # mentioned without a number ("hadn't fasted"): the judgment step reads it
            continue
        cond = r.get("condition")
        if cond:
            cf = facts.get(cond["parameter"])
            if cf is None:
                undecided.append(r)
                continue
            if not violates(cf["numeric_value"], cond["operator"], cond["value"], cond.get("value2")):
                continue
        unless = r.get("unless") or []
        if unless:
            known = [facts.get(t["parameter"]) for t in unless]
            if all(k is not None for k in known):
                if all(violates(k["numeric_value"], t["operator"], t["value"], t.get("value2"))
                       for k, t in zip(known, unless)):
                    continue  # the document's exception applies
            elif violates(facts[r["parameter"]]["numeric_value"], r["operator"], r["value"], r["value2"]):
                undecided.append(r)  # the exception might apply: let the judgment step read the whole rule
                continue
        f = facts[r["parameter"]]
        if violates(f["numeric_value"], r["operator"], r["value"], r["value2"]):
            bound = (f"{r['value']:g}-{r['value2']:g}" if r["operator"] == "outside"
                     else f"{r['operator']} {r['value']:g}")
            found.append(Finding(
                rule=r, event=event, method="numeric", message_quote=f["quote"],
                observed=f"{f['value']} {f.get('unit') or ''}".strip(),
                reasoning=f"{r['parameter']} = {f['numeric_value']:g} violates the rule (violation when {bound}).",
                extra={"fact_quote": f["quote"]},
            ))
    return found, undecided


JUDGE_SYSTEM = """You check one event from clinical trial correspondence against a list of atomic study rules.

Apply the rules in order. Report a rule ONLY if the correspondence states facts that conflict with it. Missing information is never a conflict. Do not infer that a step was skipped from the way a result is described (a result that "came back" was collected), or because the email reports only some results (listing two labs does not mean the others were not done). Do not report anything that is not covered by one of the listed rules. Report each stated fact under the single most specific rule only; do not report the same fact again under broader rules. If another listed rule explicitly allows what happened (an exception or a permitted alternative), it is not a conflict. Do not assess participant safety, adverse events or clinical risk; those are out of scope.

For each conflict:
- message_quote: the exact words from the correspondence that show the conflict, copied VERBATIM.
- reasoning: one or two sentences linking the quote to the rule.
- certainty: clear if the facts stated plainly conflict; possible if the conflict depends on an assumption you had to make.
- basis: stated_fact_breaks_rule when the correspondence says something that breaks the rule ("randomized before the MRI", "spun at 75 minutes", "forgot the log"); inferred_from_wording when you are reading it between the lines (guessing from how a result is described, or assuming an order the writer did not state); required_step_not_mentioned when the correspondence is only silent about a step the rule requires.
Apply the operator notes; they override your own inference."""

PREVENTED_NOTE = """

The writer says this event was stopped, cancelled or not done. Stopping something that would have broken a rule is not a conflict. Report a rule only if the stopping itself, or what was done instead, breaks it (for example a rule that says a step must not be cancelled)."""


def _when(event: dict) -> str:
    if not event.get("event_datetime"):
        return ""
    return f"Event date: {event['event_datetime'][:10]}" + (
        f" ({event['event_weekday']})" if event.get("event_weekday") else "") + "\n"


def judgment_checks(study: Study, event: dict, rules: list[dict], msg_text: str,
                    note_list: list[dict], use_cache: bool) -> list[Finding]:
    if not rules:
        return []
    ids = [r["id"] for r in rules]
    schema = llm.obj({
        "conflicts": {"type": "array", "items": llm.obj({
            "rule_id": {"type": "string", "enum": ids},
            "message_quote": {"type": "string"},
            "reasoning": {"type": "string"},
            "certainty": {"type": "string", "enum": ["clear", "possible"]},
            "basis": {"type": "string", "enum": ["stated_fact_breaks_rule", "inferred_from_wording",
                                                 "required_step_not_mentioned"]},
        })},
    })
    def line(r: dict) -> str:
        unless = [t for t in r.get("unless") or [] if t["parameter"] != "exception_applies"]
        exc = (" Does not apply when " + " and ".join(
            f"{t['parameter']} {t['operator']} {t['value']:g}" for t in unless) + ".") if unless else ""
        return f"{r['id']}: {r['text']}{exc}"

    rule_lines = "\n".join(line(r) for r in rules)
    user = (
        f"Study: {study.title}\n\nOperator notes:\n{notes.render(note_list)}\n\n"
        f"EVENT:\n{db.js({k: v for k, v in event.items() if not k.startswith('_')})}\n{_when(event)}\n"
        f"CORRESPONDENCE TEXT:\n{msg_text[:12000]}\n\nRULES (in order):\n{rule_lines}"
    )
    system = JUDGE_SYSTEM + (PREVENTED_NOTE if event.get("prevented") else "")
    out = llm.structured("match", system, user, schema, "rule_conflicts", use_cache=use_cache)
    by_id = {r["id"]: r for r in rules}
    return [Finding(rule=by_id[c["rule_id"]], event=event, method="judgment", message_quote=c["message_quote"],
                    reasoning=c["reasoning"], certainty=c["certainty"])
            for c in out["conflicts"] if c["rule_id"] in by_id and c["basis"] == "stated_fact_breaks_rule"
            and c["certainty"] == "clear"]


CONFIRM_SYSTEM = """You confirm or reject a suspected protocol conflict using the ORIGINAL study document text, not a summary.

Given the original passage, the event and the correspondence quote, decide whether the original document really sets a requirement that the correspondence conflicts with. Reject if the passage does not say what the rule claims, if the rule does not apply to this kind of event, if the conflict concerns participant safety rather than the study's procedures, or if confirming it would need a threshold, fact or assumption that is not stated in the passage or the correspondence. Reject if the passage itself allows what happened (an exception or a permitted alternative). Reject if the correspondence is only silent about a required step: not mentioning something is not a conflict, and reporting some results does not mean the others were not done.
doc_quote: the single sentence (or shortest clause) from the ORIGINAL passage that states the requirement, copied VERBATIM, characters exactly as given.
headline: at most 16 words, neutral and factual, naming what was reported and the requirement it appears to conflict with, e.g. "Participant randomized with eGFR 41; protocol excludes eGFR below 45". Keep the event's tense: for a planned event say what is planned ("Study drug to start tomorrow before baseline MRI; ..."). No diagnosis, no instruction, no blame."""

CONFIRM_SCHEMA = llm.obj({
    "confirmed": {"type": "boolean"},
    "doc_quote": {"type": "string"},
    "explanation": {"type": "string"},
    "headline": {"type": "string"},
})


def confirm(study: Study, f: Finding, use_cache: bool) -> bool:
    loc = ingest.locate(study.study_id, f.rule["doc_id"], f.rule["source_quote"], f.rule.get("section"))
    if loc is None:
        db.audit("finding_rejected_source_not_in_document", entity_type="rule", entity_id=f.rule["id"],
                 study_id=study.study_id, quote=f.rule["source_quote"])
        return False
    user = (
        f"Document: {study.doc(f.rule['doc_id'])['title']}, page {loc['page']}\n"
        f"ORIGINAL PASSAGE:\n{loc['passage']}\n\n"
        f"Rule as summarised: {f.rule['text']}\n"
        f"Event: {f.event['description']} (type {f.event['event_type']}, {f.event['temporality']})\n"
        f"{_when(with_weekday(f.event))}"
        f"Correspondence quote: \"{f.message_quote}\"\n"
        f"Why it was flagged: {f.reasoning}"
    )
    if f.method == "numeric":
        user += ("\nThe value was compared with the rule's limit by code, and that comparison stands: do not "
                 "re-decide whether the value meets the limit. Decide only whether the original passage sets this "
                 "limit and whether it applies to this event.")
    out = llm.structured("confirm", CONFIRM_SYSTEM, user, CONFIRM_SCHEMA, "confirm_conflict", use_cache=use_cache)
    f.confirmation = out["explanation"]
    f.extra["headline"] = out["headline"]
    if not out["confirmed"]:
        return False
    doc_quote = out["doc_quote"]
    if not textutil.quote_in(doc_quote, loc["passage"]):
        doc_quote = loc["exact"]
    f.doc_quote = textutil.clean_spaces(doc_quote)
    f.doc_page = loc["page"]
    f.doc_passage = loc["passage"]
    return True


def match_event(study: Study, event: dict, msg: dict, corpus: str, *, use_cache: bool = True) -> list[Finding]:
    rules = candidate_rules(study, event)
    emails = [msg.get("from_addr")] + list(msg.get("to_addrs") or []) + list(msg.get("cc_addrs") or [])
    note_list = notes.applicable(msg["id"], study.study_id, emails)

    if event.get("prevented"):
        # what was stopped is not checked against the numbers; only the stopping itself can break a rule
        findings, undecided = [], [r for r in rules if r["check_type"] == "numeric"]
    else:
        findings, undecided = numeric_checks(rules, event)
    numeric_hit = {f.rule["id"] for f in findings}
    judgment_rules = [r for r in rules if r["check_type"] == "judgment"] + undecided
    findings += [f for f in judgment_checks(study, with_weekday(event), judgment_rules, corpus, note_list, use_cache)
                 if f.rule["id"] not in numeric_hit]

    confirmed: list[Finding] = []
    for f in findings:
        pieces = textutil.quote_pieces(f.message_quote, corpus)
        if not pieces:
            db.audit("finding_rejected_quote_not_in_message", entity_type="message", entity_id=msg["id"],
                     study_id=study.study_id, rule_id=f.rule["id"], quote=f.message_quote)
            continue
        f.message_quote = " … ".join(textutil.expand_to_sentence(p, corpus) for p in pieces)
        if not confirm(study, f, use_cache):
            db.audit("finding_not_confirmed_by_document", entity_type="message", entity_id=msg["id"],
                     study_id=study.study_id, rule_id=f.rule["id"], reasoning=f.reasoning,
                     explanation=f.confirmation)
            continue
        confirmed.append(f)
    return confirmed

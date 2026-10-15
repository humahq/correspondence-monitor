"""The check list: every rule in the protocol, typed so code can check the countable ones.

Built once per study from the guide sections by the ingestion model:
- interval: the time from one event to another must fall in a range (visit windows, washouts, screening limits,
  sample timing, reporting deadlines);
- order:    one event must happen before another;
- count:    an event may happen at most N times per period (doses per day);
- value:    a measured value must meet a limit (lab values, temperatures, ages);
- follow_up: an event requires steps (kept in the duties table);
- judgment: needs understanding (a good reason, safe, appropriate): left to the AI reading the topic briefs;
- not_checkable: background, admin or patient-facing text a site email cannot break.

Events and values are named from one study vocabulary, so the facts pulled from a message and the rules meet on
the same names. Every rule's quote is verified against the section by code; rules whose quote is not found are
dropped and reported.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import db, guide, ingest, llm, settings, textutil
from .config import Study

KINDS = ["interval", "order", "count", "value", "follow_up", "judgment", "not_checkable"]
UNITS = ["minutes", "hours", "days", "weeks", "months", "years", ""]

BUILD_SYSTEM = """You turn clinical-trial protocol sections into a complete check list for a monitor who reads site
emails and chat messages. List EVERY rule a site could break, one rule per item, in the order they appear. Also
list each requirement that cannot be broken by a site (background, rationale, admin, signature lines, statistics,
patient-facing text) as one not_checkable item per section, so nothing is silently skipped.

For each rule choose its kind:
- interval: the time from one event to another must be within limits. Visit windows ("Week 16: 112 +/- 7 days
  after Baseline": from_event "baseline visit", to_event "week 16 visit", unit days, min 105, max 119), washouts
  ("no strong CYP3A4 inducer within 14 days before first dose": from_event "last dose of strong CYP3A4/5 inducer",
  to_event "first dose of study drug", min 14, max null), screening limits, sample timing ("predose PK 0 to 2 h
  before dosing": from_event "predose PK sample", to_event "study drug dose", unit hours, min 0, max 2), reporting
  deadlines ("enter SAE within 24 hours of awareness": from_event "site aware of SAE", to_event "SAE entered in
  EDC", unit hours, min 0, max 24).
- order: one event must happen before another (first_event before then_event): "consent before any study
  procedure", "dose after all Baseline procedures", "ECG before dosing".
- count: an event may happen at most max times per unit ("one dose per day": event "study drug dose", max 1,
  unit days).
- value: a measured value must meet a limit (value_name "eGFR", operator lt/le/gt/ge/outside/between, limit,
  limit2, value_unit). Write the limit that makes it a PROBLEM: "eGFR must be >= 30" is operator lt, limit 30.
- follow_up: an event requires steps by the site (report, notify, record, repeat). Put the event in event.
- judgment: needs understanding, not counting ("only for a safety reason", "if clinically indicated").

Name events and values plainly and the same way every time ("baseline visit", "first dose of study drug",
"study drug dose", "week 4 visit", "master protocol screening visit", "baseline CSF collection", "postdose PK
sample"). applies_when: the condition under which the rule applies, or "" if always ("for participants with
baseline CSF", "during the ATE"). exception: when the protocol excuses the departure, or "".

quote: ONE sentence copied exactly from the section that states the rule; section_id: the id in brackets.
Unused fields: "" or null."""

RULE = llm.obj({
    "kind": {"type": "string", "enum": KINDS},
    "text": {"type": "string"},
    "from_event": {"type": "string"}, "to_event": {"type": "string"},
    "first_event": {"type": "string"}, "then_event": {"type": "string"},
    "event": {"type": "string"},
    "value_name": {"type": "string"}, "operator": {"type": "string"},
    "limit": {"type": ["number", "null"]}, "limit2": {"type": ["number", "null"]}, "value_unit": {"type": "string"},
    "unit": {"type": "string", "enum": UNITS},
    "min": {"type": ["number", "null"]}, "max": {"type": ["number", "null"]},
    "applies_when": {"type": "string"}, "exception": {"type": "string"},
    "quote": {"type": "string"}, "section_id": {"type": "string"}})
BUILD_SCHEMA = llm.obj({"rules": {"type": "array", "items": RULE}})

VOCAB_SYSTEM = """These event and value names come from a clinical-trial protocol's check list. Merge names that mean
the same thing into one entry (case, word order, synonyms: "first dose", "first dose of study drug" and "day 1
dose" are one event). Do not merge different visits, samples, medicines classes or doses. For each entry give: id
(snake_case, short), kind (event or value), description (one line), and phrases: 3 to 8 ways site staff write it
in emails and chat ("bl", "baseline", "day 1"; "wk 4", "week 4 visit"). members: the exact input names it covers.
Every input name appears in exactly one entry."""

VOCAB_SCHEMA = llm.obj({"entries": {"type": "array", "items": llm.obj({
    "id": {"type": "string"}, "kind": {"type": "string", "enum": ["event", "value"]},
    "description": {"type": "string"}, "phrases": {"type": "array", "items": {"type": "string"}},
    "members": {"type": "array", "items": {"type": "string"}}})}})

_CODE_KINDS = {"interval": ("from_event", "to_event"), "order": ("first_event", "then_event"),
               "count": ("event",), "value": ("value_name",)}


def _batches(secs: list[dict], limit: int = 20000) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for s in secs:
        if cur and size + len(s["text"]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s["text"])
    return out + ([cur] if cur else [])


def _find(quote: str, secs: list[dict], cited: str) -> tuple[str, str] | None:
    bare = re.sub(r"^\s*(?:\d+|[a-z])[.)]\s+", "", quote)  # Word list numbers are not in the section text
    for s in sorted(secs, key=lambda i: i["id"] != cited):
        span = textutil.find_quote(quote, s["text"]) or ingest.repair_quote(quote, s["text"])
        if span:
            return s["id"], s["text"][span[0]:span[1]]
        for q in {quote.strip(), bare.strip()}:  # short list items: an exact whole line
            if q and re.search(rf"(?m)^\W*{re.escape(q)}\W*$", s["text"]):
                return s["id"], q
    return None


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def code_ready(rule: dict) -> bool:
    """Can code check this rule: its kind is countable and every name it needs is in the vocabulary, with a limit."""
    need = _CODE_KINDS.get(rule["kind"])
    if not need or not all(rule.get(f"{f}_id") for f in need):
        return False
    if rule["kind"] == "interval":
        return rule["unit"] in ("minutes", "hours", "days", "weeks", "months") and (
            rule["min"] is not None or rule["max"] is not None)
    if rule["kind"] == "count":
        return rule["max"] is not None and rule["unit"] in ("days", "weeks")
    if rule["kind"] == "value":
        return rule["operator"] in ("lt", "le", "gt", "ge", "outside", "between") and rule["limit"] is not None
    return True


_LONGER = re.compile(r"within (\d+) (days|weeks) or (\d+) half-lives \(whichever is longer\)", re.I)
_HALF_LIFE = re.compile(r"half-life:([^|]*)", re.I)
_HL_VALUE = re.compile(r"([\d.]+)(?:\s*(?:±\s*[\d.]+|(?:–|-|to)\s*([\d.]+)))?\s*(?:\([^)]*\)\s*)?(min|h|d|hours?|days?|weeks?)\b",
                       re.I)
_DAYS = {"min": 1 / 1440, "h": 1 / 24, "hour": 1 / 24, "hours": 1 / 24, "d": 1, "day": 1, "days": 1, "week": 7,
         "weeks": 7}


def half_life_days(field: str) -> float | None:
    """The longest half-life a table cell states, in days ("3–5 h" is 5 h; "6.8 h (IV); 8.1 h (oral)" is 8.1 h)."""
    vals = [max(float(a), float(b or 0)) * _DAYS[u.lower()] for a, b, u in _HL_VALUE.findall(field)]
    return max(vals) if vals else None


def longer_of(rules: list[dict], secs: list[dict]) -> list[str]:
    """Washouts written as "within N days or K half-lives (whichever is longer)": each medicine row of the tables
    the rule points to gets the longest of its table washout, N days and K times its half-life (the upper value
    of a half-life range). Returns one line per changed rule, saying how its limit was worked out."""
    cats = [(r, int(m.group(1)) * _DAYS[m.group(2).lower()], int(m.group(3)))
            for r in rules if (m := _LONGER.search(r["quote"]))]
    if not cats:
        return []
    by_id = {s["id"]: s for s in secs}
    words = lambda s: {w for w in re.findall(r"[a-z0-9/]+", s.lower()) if len(w) > 3}
    out = []
    for r in rules:
        m = _HALF_LIFE.search(r["quote"])
        hl = half_life_days(m.group(1)) if m else None
        if not (r["kind"] == "interval" and r["code"] and hl and r["unit"] in ("days", "weeks")):
            continue
        sec = by_id.get(r["section_id"], {})
        about = words(sec.get("title", "") + " " + sec.get("text", "")[:400])
        cat, base, k = max(cats, key=lambda c: len(about & words(c[0]["quote"])))
        table = (r["min"] or 0) * _DAYS[r["unit"]]
        need = max(table, base, -(-k * hl // 1))
        out.append(f"{r['from_event_id']}: {need:g} days = longest of table {table:g}, {base:g} days, "
                   f"{k} x half-life {hl:.2f} d [{cat['section_id']}]")
        if need != table or r["unit"] != "days":
            r.update(min=need, unit="days", text=f"{r['text']} Limit: {need:g} days, the longest of the table's "
                     f"{table:g} days, {base:g} days and {k} half-lives ({k} x {hl:g} days), as {cat['section_id']} "
                     f"requires.")
    return out


def resolve(study: Study, rules: list[dict]) -> list[dict]:
    """Settle intervals that give the same pair of events different limits. A document that overrides another wins
    (the appendix over the master protocol); among the rest the widest limits win, since the scanned master text
    loses the "±" sign ("112 ± 7" read as "112 - 7") and a narrower copy is the damaged one. Returns the conflicts;
    the losing rules get "superseded" set to the winner's id and stop being code-checked."""
    over = {d["id"]: set(d.get("overrides") or []) for d in study.documents}
    groups: dict[tuple, list[dict]] = {}
    for r in rules:
        if r["kind"] == "interval" and r.get("code"):
            groups.setdefault((r["from_event_id"], r["to_event_id"], r["unit"]), []).append(r)
    conflicts = []
    for rs in groups.values():
        if len({(r["min"], r["max"]) for r in rs}) < 2:
            continue
        docs = {r["section_id"].split(":")[0] for r in rs}
        beaten = set().union(*(over.get(d, set()) for d in docs)) & docs
        live = [r for r in rs if r["section_id"].split(":")[0] not in beaten]
        lo = None if any(r["min"] is None for r in live) else min(r["min"] for r in live)
        hi = None if any(r["max"] is None for r in live) else max(r["max"] for r in live)
        copies = [f"{r['min']}..{r['max']} [{r['section_id']}]" for r in rs]
        win = next((r for r in live if (r["min"], r["max"]) == (lo, hi)), live[0])
        for r in rs:
            if r is not win:
                r.update(superseded=win["id"], code=False)
        win.update(min=lo, max=hi)
        conflicts.append({"events": f"{win['from_event_id']} -> {win['to_event_id']}", "kept": f"{lo}..{hi}",
                          "copies": copies})
    return conflicts


def build(study: Study, log=print, use_cache: bool = True) -> dict:
    docs = guide.documents(study)
    secs = [i for i in guide.load(study.study_id) if i["kind"] == "section" and i["doc_id"] in docs]
    secs.sort(key=lambda s: (docs.index(s["doc_id"]), s["ord"]))
    titles = {d["id"]: d.get("title", d["id"]) for d in study.documents}
    model = llm.model_for("ingest")
    dropped: list[dict] = []

    def one(batch: list[dict]) -> list[dict]:
        user = "\n\n".join(f"[{s['id']}] {titles.get(s['doc_id'], s['doc_id'])}: {s['number']} {s['title']}\n{s['text']}"
                           for s in batch)
        res = llm.structured("checks_build", BUILD_SYSTEM, user, BUILD_SCHEMA, "rules", use_cache=use_cache,
                             model=model)
        out = []
        for r in res["rules"]:
            hit = _find(r["quote"], batch, r["section_id"]) if r["quote"].strip() else None
            if not hit:
                dropped.append(r)
                continue
            out.append(dict(r, section_id=hit[0], quote=hit[1]))
        return out

    with ThreadPoolExecutor(4) as ex:
        rules = [r for part in ex.map(one, _batches(secs)) for r in part]

    names = sorted({_norm(r[f]) for r in rules for f in ("from_event", "to_event", "first_event", "then_event",
                                                          "event", "value_name") if _norm(r[f])})
    vocab: list[dict] = []
    for i in range(0, len(names), 250):
        res = llm.structured("checks_build", VOCAB_SYSTEM, "\n".join(names[i:i + 250]), VOCAB_SCHEMA, "vocab",
                             use_cache=use_cache, model=model)
        vocab += res["entries"]
    by_name = {_norm(m): e["id"] for e in vocab for m in e["members"]}
    for r in rules:
        for f in ("from_event", "to_event", "first_event", "then_event", "event", "value_name"):
            r[f"{f}_id"] = by_name.get(_norm(r[f]), "") if _norm(r[f]) else ""
        r["code"] = code_ready(r)
    for n, r in enumerate(rules):
        r["id"] = f"chk:{n:04d}"
    washouts = longer_of(rules, secs)
    conflicts = resolve(study, rules)
    mark_implied(rules, use_cache)

    version = "; ".join(f"{r['doc_id']}={r['version']}" for r in db.q(
        "SELECT DISTINCT doc_id, version FROM guide WHERE study_id=? AND kind='section'", (study.study_id,)))
    db.ex("DELETE FROM guide WHERE study_id=? AND kind IN ('check', 'vocab')", (study.study_id,))
    rows = [dict(id=r["id"], kind="check", title=r["kind"], text=json.dumps(r), ord=6000 + n)
            for n, r in enumerate(rules)]
    rows.append(dict(id="vocab", kind="vocab", title="vocabulary", text=json.dumps(vocab), ord=5999))
    for r in rows:
        db.ex("INSERT INTO guide(study_id, doc_id, id, kind, number, title, path, text, about, topics, ord, version) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (study.study_id, "checks", r["id"], r["kind"], "", r["title"], "", r["text"], "", "[]", r["ord"], version))
    report = coverage(rules, secs, dropped, conflicts)
    report["washouts_set_by_half_life"] = washouts
    out = settings.data_dir() / "guides" / f"{study.study_id}-checks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"study_id": study.study_id, "version": version, "rules": rules, "vocab": vocab,
                               "dropped": dropped, "coverage": report}, indent=1))
    log(render_coverage(report) + f"\n-> {out}")
    return report


def load(study: Study) -> tuple[list[dict], list[dict]]:
    rows = [r for r in guide.load(study.study_id) if r["kind"] in ("check", "vocab")]
    rules = [json.loads(r["text"]) for r in rows if r["kind"] == "check"]
    vocab = next((json.loads(r["text"]) for r in rows if r["kind"] == "vocab"), [])
    return rules, vocab


# ---------------------------------------------------------------- reading a message: facts, then code

FACTS_SYSTEM = """You pull dated facts out of a clinical-trial site email or chat for code to check against the
protocol. Use ONLY the event and value names listed. Report what the message states or clearly implies about the
named participant (or, with no participant, about the site's study drug and supplies).

events: one entry per occurrence (two doses on one day are two entries). date as YYYY-MM-DD, working out
"yesterday", "last Tuesday", weekdays and "Day N" from the message date and the other dates given; "" if it cannot
be worked out. time as 24-hour HH:MM, or "". status: done (it happened), planned (scheduled, booked, intended),
not_done (the message says it did not happen, was missed, or has not happened yet). quote: the message's exact
words for it.
values: a measured number for a listed value, converted to the unit shown after its name when the message gives a
different unit; quote the message's words.
Leave out anything the message does not say. Do not judge whether anything is allowed."""

FACTS_SCHEMA = llm.obj({
    "events": {"type": "array", "items": llm.obj({
        "event": {"type": "string"}, "date": {"type": "string"}, "time": {"type": "string"},
        "status": {"type": "string", "enum": ["done", "planned", "not_done"]}, "quote": {"type": "string"}})},
    "values": {"type": "array", "items": llm.obj({
        "value": {"type": "string"}, "number": {"type": "number"}, "quote": {"type": "string"}})}})

_CLASS = [("consent", "consent"), (r"csf|pk|sample|blood|urine|dna|plasma|shipment", "sample_handling"),
          (r"storage|temp|dose|dosing|drug|dnl343|kit|packet", "study_drug"),
          (r"age|egfr|alt|ast|fsh|vc|eligib|screening|diagnos|onset", "eligibility"),
          (r"visit|call|ate_|w\d+", "visit_schedule")]
_UNIT_HOURS = {"minutes": 1 / 60, "hours": 1, "days": 24, "weeks": 168, "months": 730.5}


IMPLIED_SYSTEM = """Each line is a protocol rule that code checks from dated events in a site message, with the
condition under which the rule applies. Code only sees the events named in the rule. For each rule say whether the
condition is IMPLIED: true whenever those events happen ("for postdose PK sampling" when the events are a dose and
a postdose PK sample; "prior apalutamide use" when an event is the last apalutamide dose; "during the ATE" for ATE
visits). Answer not implied when the condition needs a further situation or choice the events do not show ("when
the ET visit is used instead of the safety call", "at eligibility determination" for a lab value that may be
measured later, "for participants with a history of seizure", "when amendments apply")."""

IMPLIED_SCHEMA = llm.obj({"rules": {"type": "array", "items": llm.obj({
    "id": {"type": "string"}, "implied": {"type": "boolean"}})}})


def mark_implied(rules: list[dict], use_cache: bool = True) -> None:
    """Sets "implied" on each code rule with a condition: code checks it only when the condition is true whenever
    its events happen; otherwise the AI, which reads the whole message, judges it."""
    todo = [r for r in rules if r["code"] and r["applies_when"].strip()]
    lines = []
    for r in todo:
        names = ", ".join(r[f] for f in ("from_event_id", "to_event_id", "first_event_id", "then_event_id",
                                         "event_id", "value_name_id") if r.get(f))
        lines.append(f"{r['id']} | {r['text']} | events: {names} | applies when: {r['applies_when']}")
    got: dict[str, bool] = {}
    for i in range(0, len(lines), 80):
        res = llm.structured("checks_build", IMPLIED_SYSTEM, "\n".join(lines[i:i + 80]), IMPLIED_SCHEMA, "implied",
                             use_cache=use_cache, model=llm.model_for("ingest"))
        got.update({x["id"]: x["implied"] for x in res["rules"]})
    for r in todo:
        r["implied"] = got.get(r["id"], False)


def live(rules: list[dict]) -> list[dict]:
    """The rules code checks, appendix rules first (the appendix overrides the master protocol): those with no
    condition, or a condition true whenever their events happen (see mark_implied)."""
    return sorted((r for r in rules if r["code"] and not r.get("superseded")
                   and (not r["applies_when"].strip() or r.get("implied"))),
                  key=lambda r: not r["section_id"].startswith("regimen"))


def _names(rules: list[dict], vocab: list[dict]) -> list[dict]:
    ids = {r[f] for r in rules for f in ("from_event_id", "to_event_id", "first_event_id", "then_event_id",
                                         "event_id", "value_name_id") if r.get(f)}
    unit = {r["value_name_id"]: r["value_unit"] for r in rules if r["kind"] == "value"}
    return [dict(e, unit=unit.get(e["id"], "")) for e in vocab if e["id"] in ids]


def extract(study: Study, rules: list[dict], vocab: list[dict], email: str, participant: str, sent,
            use_cache: bool = True) -> dict:
    names = _names(rules, vocab)
    lines = [f"- {e['id']}{' (' + e['unit'] + ')' if e['unit'] else ''}: {e['description']} "
             f"e.g. {'; '.join(e['phrases'][:5])}" for e in names]
    events = [ln for ln, e in zip(lines, names) if e["kind"] == "event"]
    values = [ln for ln, e in zip(lines, names) if e["kind"] == "value"]
    user = (f"EVENT NAMES:\n" + "\n".join(events) + "\n\nVALUE NAMES (unit):\n" + "\n".join(values)
            + f"\n\nMESSAGE DATE: {sent:%A %d %B %Y}\nPARTICIPANT: {participant or '(none: site level)'}"
            + f"\n\nMESSAGE:\n{email}")
    res = llm.structured("checks_facts", FACTS_SYSTEM, user, FACTS_SCHEMA, "facts", use_cache=use_cache)
    known = {e["id"] for e in names}
    return {"events": [e for e in res["events"] if e["event"] in known],
            "values": [v for v in res["values"] if v["value"] in known]}


def _when(e: dict):
    try:
        d = datetime.fromisoformat(e["date"][:10])
    except (ValueError, TypeError):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", e.get("time") or "")
    return d.replace(hour=int(m.group(1)), minute=int(m.group(2))) if m else d


def _label(event_id: str) -> str:
    return event_id.replace("_", " ")


def _gap(a: dict, b: dict, unit: str) -> float | None:
    """b minus a in the rule's unit; None when the unit needs clock times the message does not give."""
    ta, tb = _when(a), _when(b)
    if not ta or not tb:
        return None
    if unit in ("minutes", "hours"):
        if not (a.get("time") and b.get("time")):
            return None
        return (tb - ta).total_seconds() / 3600 / _UNIT_HOURS[unit]
    return (tb.date() - ta.date()).days * 24 / _UNIT_HOURS[unit]


def evaluate(rules: list[dict], facts: dict) -> tuple[list[dict], list[dict]]:
    """Run every code rule on the message's facts. Returns (departures, passes): each names the rule, the fact in
    words and the message quotes. A rule with a fact missing is neither."""
    by: dict[str, list[dict]] = {}
    for e in facts["events"]:
        by.setdefault(e["event"], []).append(e)
    real = lambda eid: [e for e in by.get(eid, []) if e["status"] != "not_done"]
    bad, ok, seen = [], [], set()

    def add(r, fact, quotes, broken, status="done"):
        key = (r["kind"], fact)
        if key in seen:
            return
        seen.add(key)
        (bad if broken else ok).append({"rule": r, "fact": fact, "quotes": [q for q in quotes if q],
                                        "status": status})

    for r in rules:
        k = r["kind"]
        if k == "interval":
            # nearest pairs first, each occurrence used once: a predose sample goes with the next dose, not every dose
            pairs = sorted(((a, b, _gap(a, b, r["unit"])) for a in real(r["from_event_id"])
                            for b in real(r["to_event_id"]) if a is not b), key=lambda p: abs(p[2] or 0))
            used: set[int] = set()
            for a, b, g in pairs:
                if g is None or id(a) in used or id(b) in used:
                    continue
                used |= {id(a), id(b)}
                lo, hi = r["min"], r["max"]
                broken = (lo is not None and g < lo) or (hi is not None and g > hi)
                allowed = (f"{lo:g} to {hi:g}" if lo is not None and hi is not None else
                           f"at least {lo:g}" if lo is not None else f"at most {hi:g}")
                fact = (f"{_label(r['to_event_id'])} ({b['date']} {b.get('time') or ''}) is {g:g} {r['unit']} after "
                        f"{_label(r['from_event_id'])} ({a['date']} {a.get('time') or ''}); the protocol allows "
                        f"{allowed} {r['unit']}")
                add(r, re.sub(r" +", " ", fact).replace(" )", ")"), [a["quote"], b["quote"]], broken,
                    "planned" if "planned" in (a["status"], b["status"]) else "done")
        elif k == "order":
            for b in real(r["then_event_id"]):
                for a in by.get(r["first_event_id"], []):
                    ta, tb = _when(a), _when(b)
                    if a["status"] == "not_done" and b["status"] == "done":
                        add(r, f"{_label(r['then_event_id'])} happened but {_label(r['first_event_id'])} had not; "
                               f"the protocol requires {_label(r['first_event_id'])} first", [a["quote"], b["quote"]],
                            True)
                    elif ta and tb and a["status"] != "not_done":
                        same_day_no_clock = ta.date() == tb.date() and not (a.get("time") and b.get("time"))
                        if same_day_no_clock:
                            continue
                        add(r, f"{_label(r['first_event_id'])} ({a['date']} {a.get('time') or ''}) is "
                               f"{'after' if ta > tb else 'before'} {_label(r['then_event_id'])} ({b['date']} "
                               f"{b.get('time') or ''}); the protocol requires {_label(r['first_event_id'])} first",
                            [a["quote"], b["quote"]], ta > tb,
                            "planned" if "planned" in (a["status"], b["status"]) else "done")
        elif k == "count":
            days: dict = {}
            for e in by.get(r["event_id"], []):
                if e["status"] == "done" and _when(e):
                    d = _when(e).date()
                    days.setdefault(d if r["unit"] == "days" else f"week of {d.isocalendar()[:2]}", []).append(e)
            for d, es in days.items():
                distinct = {e.get("time") or e["quote"] for e in es}
                if len(distinct) > r["max"]:
                    add(r, f"{len(distinct)} x {_label(r['event_id'])} on {d}; the protocol allows at most "
                           f"{r['max']:g} per {r['unit'].rstrip('s')}", [e["quote"] for e in es], True)
        elif k == "value":
            for v in facts["values"]:
                if v["value"] != r["value_name_id"]:
                    continue
                x, a, b, op = v["number"], r["limit"], r["limit2"], r["operator"]
                broken = {"lt": x < a, "le": x <= a, "gt": x > a, "ge": x >= a,
                          "outside": b is not None and not (a <= x <= b),
                          "between": b is not None and a <= x <= b}.get(op, False)
                words = {"lt": f"below {a:g}", "le": f"{a:g} or below", "gt": f"above {a:g}", "ge": f"{a:g} or above",
                         "outside": f"outside {a:g} to {b}", "between": f"between {a:g} and {b}"}[op]
                add(r, f"{_label(r['value_name_id'])} is {x:g} {r['value_unit']}; a problem when {words} "
                       f"{r['value_unit']}".strip(), [v["quote"]], broken)
    return bad, ok


def pattern_class(rule: dict) -> str:
    names = " ".join(rule.get(f) or "" for f in ("first_event_id", "then_event_id", "from_event_id", "to_event_id",
                                                 "event_id", "value_name_id"))
    if rule["kind"] == "order" and "consent" not in names:
        return "procedure_sequence"
    return next((c for pat, c in _CLASS if re.search(pat, names, re.I)), "visit_schedule")


def finding(d: dict) -> dict:
    """A departure found by code, in the reader's finding form."""
    r = d["rule"]
    cond = f" This applies {r['applies_when']}." if r.get("applies_when") else ""
    exc = f" Unless: {r['exception']}." if r.get("exception") else ""
    return {"problem": d["fact"][0].upper() + d["fact"][1:] + ".",
            "email_quote": " ... ".join(d["quotes"])[:400], "protocol_quote": r["quote"],
            "section_id": r["section_id"], "why": f"Checked by code: {d['fact']}. Rule: {r['text']}{cond}{exc}",
            "next_step": "Confirm the dates and values; if they are right, record this as a protocol deviation and "
                         "follow the protocol's steps for it.",
            "severity": "moderate", "certainty": "conflict", "pattern_class": pattern_class(r),
            "status": d["status"], "from_fact": d["fact"], "from_code": r["id"]}


def overlaps(code: dict, ai: dict) -> int:
    """How many of a code finding's quoted message pieces an AI finding also quotes; 0 when it reports another
    departure. Two departures can share a piece (one dose before a late predose and an early postdose sample)."""
    w = lambda s: {x for x in re.findall(r"\w+", s.lower()) if len(x) > 1}  # short words and clock times count
    a = w(ai["email_quote"])
    return sum(len(w(q) & a) >= max(2, 0.6 * len(w(q))) for q in code["email_quote"].split(" ... ") if w(q))


def coverage(rules: list[dict], secs: list[dict], dropped: list[dict], conflicts: list[dict]) -> dict:
    kinds = Counter(r["kind"] for r in rules)
    code = Counter(r["kind"] for r in rules if r["code"])
    checkable = sum(n for k, n in kinds.items() if k != "not_checkable")
    covered = {r["section_id"] for r in rules}
    silent = [s["id"] for s in secs if s["id"] not in covered and len(s["text"]) > 200]
    return {"rules": len(rules), "by_kind": dict(kinds), "code_ready": dict(code),
            "site_rules": checkable, "code_checked": sum(code.values()),
            "judgment": kinds.get("judgment", 0), "follow_up": kinds.get("follow_up", 0),
            "countable_not_ready": sum(1 for r in rules if r["kind"] in _CODE_KINDS and not r["code"]
                                       and not r.get("superseded")),
            "superseded": sum(1 for r in rules if r.get("superseded")), "conflicts": conflicts,
            "dropped_quote_not_found": len(dropped), "sections_with_no_rule": silent}


def render_coverage(c: dict) -> str:
    lines = [f"check list: {c['rules']} items, {c['site_rules']} rules a site could break",
             f"  checked by code now: {c['code_checked']}  "
             + ", ".join(f"{k} {n}" for k, n in sorted(c["code_ready"].items())),
             f"  countable but not ready for code (a name or limit missing): {c['countable_not_ready']}",
             f"  follow-up (duties table): {c['follow_up']}",
             f"  judgment (AI reads the briefs): {c['judgment']}",
             f"  by kind: " + ", ".join(f"{k} {n}" for k, n in sorted(c["by_kind"].items())),
             f"  conflicting limits settled: {len(c['conflicts'])} pairs, {c['superseded']} copies set aside",
             *(f"    {x['events']}: kept {x['kept']}  from " + ", ".join(x["copies"]) for x in c["conflicts"]),
             f"  washouts set by 'whichever is longer': {len(c.get('washouts_set_by_half_life', []))}",
             *(f"    {x}" for x in c.get("washouts_set_by_half_life", [])),
             f"  dropped, quote not found in the protocol: {c['dropped_quote_not_found']}",
             f"  sections longer than 200 characters with no item: {len(c['sections_with_no_rule'])} "
             + " ".join(c["sections_with_no_rule"][:20])]
    return "\n".join(lines)

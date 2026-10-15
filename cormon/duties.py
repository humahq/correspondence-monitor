"""The follow-up table: what the protocol requires a site to do after an event (overdose, hospitalisation,
pregnancy, a prohibited medicine, a missed visit...), by when and to whom.

Built once per study from the guide sections by the ingestion model, each step verified against the protocol
text by code, and stored as guide rows of kind "duty". When an email mentions an event, code picks the table rows
and one small AI call ticks the list: each step is shown done, planned, not addressed or not applicable. Steps not
addressed are attached to the email's findings, so the AI never has to remember them.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from . import db, guide, ingest, llm, settings, textutil
from .config import Study

BUILD_SYSTEM = """You read sections of a clinical-trial protocol for a monitor who checks site emails. List every
EVENT after which the protocol requires the site to take follow-up steps: an overdose or medication error, a
serious adverse event or hospitalisation, a pregnancy (participant or partner), a prohibited or restricted
medicine started, a missed, late or out-of-window visit or dose, early termination or withdrawal, unblinding, a
temperature excursion or drug supply problem, a dose reduction, interruption or restart, a failed or missed
sample, an abnormal lab or ECG, a consent problem, a protocol deviation. Only events these sections set duties
for.

For each event give:
- name: short, plain ("overdose or medication error");
- triggers: 4 to 10 words or short phrases a site email would use when this event happened ("overdose", "extra
  dose", "two doses", "took both", "double dose"); lower case, no participant ids;
- steps: every follow-up the sections require, each with: action (one plain sentence, who does what), to_whom
  (the person or body told, or ""), deadline (the protocol's time limit, or ""), condition (when the step applies,
  or ""), protocol_words (ONE sentence copied exactly from a section, that requires this step), section_id.

Only steps these sections state for this event; nothing from general knowledge. Do not list what the site must
record in the eCRF or report to the IRB unless the section about this event says so itself. Return an empty
list when the sections set no event duties."""

BUILD_SCHEMA = llm.obj({"events": {"type": "array", "items": llm.obj({
    "name": {"type": "string"},
    "triggers": {"type": "array", "items": {"type": "string"}},
    "steps": {"type": "array", "items": llm.obj({
        "action": {"type": "string"}, "to_whom": {"type": "string"}, "deadline": {"type": "string"},
        "condition": {"type": "string"}, "protocol_words": {"type": "string"}, "section_id": {"type": "string"}})}})}})

MERGE_SYSTEM = """These event names come from different parts of one clinical-trial protocol. Group the names that
mean the same event (an "overdose" and a "medication administration error" are one event if the protocol treats
them together; a participant pregnancy and a partner pregnancy are one event). Give each group one plain name.
Every name appears in exactly one group."""

MERGE_SCHEMA = llm.obj({"groups": {"type": "array", "items": llm.obj({
    "name": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}}})}})

TICK_SYSTEM = """A clinical-trial site email mentions an event. You get the follow-up steps the protocol requires
after that event. First say whether the event really happened or is planned for this participant in this email
(present), or the email only mentions the word (for example "no overdose", a question about the rule, a past event
already handled). Then, for each step, say what the email shows:
- done: the email says the step was done;
- planned: the email says it will be done;
- not_addressed: the email says nothing about it;
- not_applicable: the step cannot apply here (say why in note).
For a step with an "only if" condition, also say whether the email shows that condition holds (condition_met:
yes), shows it does not or says nothing that makes it hold (no), or leaves it open (unclear). A condition that
needs a fact the email never mentions is "no".
Judge only from the email's words. "We will follow the usual process" addresses nothing specific."""

TICK_SCHEMA = llm.obj({
    "present": {"type": "string", "enum": ["yes", "no", "unclear"]},
    "present_note": {"type": "string"},
    "steps": {"type": "array", "items": llm.obj({
        "n": {"type": "integer"}, "status": {"type": "string", "enum": ["done", "planned", "not_addressed",
                                                                        "not_applicable"]},
        "condition_met": {"type": "string", "enum": ["yes", "no", "unclear", "no_condition"]},
        "note": {"type": "string"}})}})

_STOP = {"the", "and", "for", "with", "that", "this", "from", "not", "are", "was", "were", "has", "have", "been",
         "will", "than", "into", "its", "any", "all", "per", "who", "did", "does", "study", "site", "participant"}


# ---------------------------------------------------------------- building the table

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _find(words: str, secs: list[dict], cited: str) -> tuple[str, str] | None:
    """The section id and exact text of a quote, searching the cited section first."""
    for s in sorted(secs, key=lambda i: i["id"] != cited):
        span = textutil.find_quote(words, s["text"]) or ingest.repair_quote(words, s["text"])
        if span:
            return s["id"], s["text"][span[0]:span[1]]
    return None


def _batches(secs: list[dict], limit: int = 24000) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for s in secs:
        if cur and size + len(s["text"]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += len(s["text"])
    return out + ([cur] if cur else [])


def build(study: Study, log=print, use_cache: bool = True) -> list[dict]:
    """Read the study's guide sections, list the events and their duties, verify every step's quote, and store
    the table as guide rows of kind "duty" (one per event). Returns the events."""
    docs = guide.documents(study)
    secs = [i for i in guide.load(study.study_id) if i["kind"] == "section" and i["doc_id"] in docs
            and not set(i["topics"]) <= {"background", "statistics", "admin"} and len(i["text"]) > 60]
    titles = {d["id"]: d.get("title", d["id"]) for d in study.documents}
    model = llm.model_for("ingest")
    raw: list[dict] = []
    dropped: list[str] = []

    def one(batch: list[dict]) -> list[dict]:
        user = "\n\n".join(f"[{s['id']}] {titles.get(s['doc_id'], s['doc_id'])}: {s['number']} {s['title']}\n{s['text']}"
                           for s in batch)
        res = llm.structured("duties_build", BUILD_SYSTEM, user, BUILD_SCHEMA, "events", use_cache=use_cache,
                             model=model)
        out = []
        for e in res["events"]:
            steps = []
            for st in e["steps"]:
                hit = _find(st["protocol_words"], batch, st["section_id"])
                if not hit or not st["action"].strip():
                    dropped.append(f"{e['name']}: {st['action']} [{st['section_id']}]")
                    continue
                steps.append(dict(st, section_id=hit[0], protocol_words=hit[1]))
            if steps:
                out.append(dict(e, steps=steps))
        return out

    with ThreadPoolExecutor(4) as ex:
        for part in ex.map(one, _batches(secs)):
            raw.extend(part)

    names = sorted({e["name"] for e in raw})
    groups = {n: n for n in names}
    labels = {n: n for n in names}
    if len(names) > 1:
        res = llm.structured("duties_build", MERGE_SYSTEM, "\n".join(f"- {n}" for n in names), MERGE_SCHEMA, "groups",
                             use_cache=use_cache, model=model)
        for g in res["groups"]:
            for m in g["members"]:
                if m in groups:
                    groups[m] = g["name"]
                    labels[m] = g["name"]
    events: dict[str, dict] = {}
    for e in raw:
        name = groups.get(e["name"], e["name"])
        ev = events.setdefault(name, {"name": name, "triggers": [], "steps": []})
        for t in e["triggers"]:
            t = t.strip().lower()
            if t and t not in ev["triggers"]:
                ev["triggers"].append(t)
        for st in e["steps"]:
            if not any(_same_step(st, s) for s in ev["steps"]):
                ev["steps"].append(st)

    version = "; ".join(f"{r['doc_id']}={r['version']}" for r in db.q(
        "SELECT DISTINCT doc_id, version FROM guide WHERE study_id=? AND kind='section'", (study.study_id,)))
    db.ex("DELETE FROM guide WHERE study_id=? AND kind='duty'", (study.study_id,))
    rows = []
    for n, ev in enumerate(sorted(events.values(), key=lambda e: e["name"])):
        rows.append(dict(id=f"duty:{_slug(ev['name'])}", kind="duty", number="", title=ev["name"], path="",
                         text=json.dumps(ev), about=f"follow-up steps after {ev['name']}", topics=["records_reporting"],
                         ord=5000 + n))
    for r in rows:
        db.ex("INSERT INTO guide(study_id, doc_id, id, kind, number, title, path, text, about, topics, ord, version) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (study.study_id, "duties", r["id"], r["kind"], r["number"], r["title"], r["path"], r["text"],
               r["about"], json.dumps(r["topics"]), r["ord"], version))
    out = settings.data_dir() / "guides" / f"{study.study_id}-duties.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"study_id": study.study_id, "version": version, "events": list(events.values()),
                               "dropped": dropped}, indent=1))
    log(f"duties: {len(events)} events, {sum(len(e['steps']) for e in events.values())} steps, "
        f"{len(dropped)} steps dropped (quote not found) -> {out}")
    return list(events.values())


def _same_step(a: dict, b: dict) -> bool:
    return a["protocol_words"] == b["protocol_words"] or _wordset(a["action"]) == _wordset(b["action"])


def _wordset(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in _STOP}


def table(study: Study) -> list[dict]:
    return [json.loads(r["text"]) for r in guide.load(study.study_id) if r["kind"] == "duty"]


def render(events: list[dict]) -> str:
    """The table as plain text, for a person to check once against the protocol."""
    lines = []
    for ev in events:
        lines.append(f"{ev['name'].upper()}  (triggers: {', '.join(ev['triggers'])})")
        for st in ev["steps"]:
            extra = "; ".join(x for x in (f"to {st['to_whom']}" if st["to_whom"] else "",
                                          f"by {st['deadline']}" if st["deadline"] else "",
                                          f"if {st['condition']}" if st["condition"] else "") if x)
            lines.append(f"  - {st['action']}" + (f" ({extra})" if extra else "") + f"  [{st['section_id']}]")
            lines.append(f"      \"{st['protocol_words']}\"")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- checking an email

def _mentions(trigger: str, text: str) -> bool:
    words = [re.escape(w) for w in re.findall(r"[a-z0-9]+", trigger.lower())]
    if not words:
        return False
    pattern = r"\b" + r"\W+".join(w + r"\w{0,3}" for w in words)
    return re.search(pattern, text.lower()) is not None


# plain ways of saying an event that the built trigger lists tend to miss, by a word in the event's name
_EXTRA = {"overdos": ["more than one dose", "two doses", "second dose", "both doses", "doses together", "double"],
          "prohibited": ["prohibited", "excluded medication", "not permitted", "contraindicated"],
          "hospital": ["hospital", "emergency department", "er visit"],
          "deviation": ["deviation", "out of window", "outside the window", "after the window closes"],
          "termination": ["withdraw", "discontinue", "come off study", "stop the study"]}


def events_in(text: str, events: list[dict]) -> list[dict]:
    """The table rows whose trigger words appear in the text: the email's statements plus the code's notes (which
    already say when a medicine is prohibited or a day had two doses). Code; no AI."""
    out = []
    for ev in events:
        extra = [t for k, ts in _EXTRA.items() if k in ev["name"].lower() for t in ts]
        if any(_mentions(t, text) for t in ev["triggers"] + extra):
            out.append(ev)
    return out


def tick(ev: dict, email: str, participant: str, use_cache: bool, facts: list[str] = ()) -> dict:
    """One small AI call: is the event present, and which required steps does the email leave unaddressed?
    `facts` are the code's notes (which medicines are on which protocol list, dates, doses): treat them as true."""
    listing = "\n".join(
        f"{n}. {st['action']}" + (f" (to {st['to_whom']})" if st["to_whom"] else "")
        + (f" (deadline: {st['deadline']})" if st["deadline"] else "")
        + (f" (only if {st['condition']})" if st["condition"] else "")
        for n, st in enumerate(ev["steps"]))
    user = (f"EVENT: {ev['name']}\nPARTICIPANT: {participant or '(none named)'}\n\nREQUIRED STEPS:\n{listing}"
            + ("\n\nFACTS ESTABLISHED BY CODE (true; use them to judge conditions):\n"
               + "\n".join(f"- {f}" for f in facts) if facts else "")
            + f"\n\nEMAIL:\n{email}")
    res = llm.structured("read_duty", TICK_SYSTEM, user, TICK_SCHEMA, "tick", use_cache=use_cache)
    status = {s["n"]: s for s in res["steps"] if 0 <= s["n"] < len(ev["steps"])}
    missing = [dict(ev["steps"][n], note=status[n]["note"]) for n in sorted(status)
               if status[n]["status"] == "not_addressed"
               and (not ev["steps"][n].get("condition") or status[n]["condition_met"] == "yes")]
    return {"event": ev["name"], "present": res["present"], "present_note": res["present_note"], "missing": missing,
            "ticked": [{"n": n, "status": s["status"], "condition_met": s["condition_met"], "note": s["note"]}
                       for n, s in sorted(status.items())]}


_GENERIC = re.compile(r"^(all |any |when deviations|if all|every )|^$", re.I)


def _specific(ev: dict) -> dict:
    """The event with only the steps tied to a particular situation (a condition that is not "all deviations")."""
    return dict(ev, steps=[st for st in ev["steps"] if not _GENERIC.search(st.get("condition", ""))])


def check(study: Study, text: str, email: str, participant: str, use_cache: bool, *, medicine: bool = False,
          departure: bool = False, facts: list[str] = ()) -> list[dict]:
    """For every event the email's text mentions: the steps the email does not address. Only events the AI
    confirms as present count. `medicine`: the code found a listed medicine, so the medicine event applies.
    `departure`: a departure was confirmed, so the deviation event's specific steps apply."""
    rows = table(study)
    found = events_in(text, rows)
    names = {ev["name"] for ev in found}
    if medicine:
        found += [ev for ev in rows if re.search(r"medic", ev["name"], re.I) and ev["name"] not in names]
    if departure:
        found += [_specific(ev) for ev in rows if "deviation" in ev["name"].lower() and ev["name"] not in names]
    found = [ev for ev in found if ev["steps"]]
    if not found:
        return []
    with ThreadPoolExecutor(min(4, len(found))) as ex:
        results = list(ex.map(lambda ev: tick(ev, email, participant, use_cache, facts), found))
    for r in results:
        r["acted"] = r["present"] == "yes" and bool(r["missing"])
    return results


def step_line(st: dict) -> str:
    extra = "; ".join(x for x in (f"to {st['to_whom']}" if st.get("to_whom") else "",
                                  f"by {st['deadline']}" if st.get("deadline") else "") if x)
    return st["action"].rstrip(".") + (f" ({extra})" if extra else "") + f" [{st['section_id']}]"

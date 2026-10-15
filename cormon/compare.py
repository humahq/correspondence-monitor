"""Does matching against the rules beat matching against the full study documents?

    cormon compare demo/test-compare/ --out results/compare

Every message in the folder is checked three ways and scored against the same expected answers:

  rules      the real bot: email reader, numeric rules in code, judgment rules by the AI, confirmation against the
             original document (run through the sandbox, exactly like `cormon test`).
  document   one AI call per message with the FULL text of every study document and no rules.
  rulelist   one AI call per message with the list of rules and no code checks.

`document` and `rulelist` get the same instructions; only the reference material differs. So `rules` vs `document`
answers "do the rules help", and `rulelist` vs `rules` shows how much of that comes from the pipeline (code checks
and confirmation) rather than from the rules themselves. `document+quotes` is `document` keeping only findings whose
email quote and document quote are both found word for word (no extra AI calls).
"""

from __future__ import annotations

import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import config, db, ingest, llm, testbench, textutil

MODES = ("rules", "document", "rulelist")

POLICY = """You monitor email from a clinical trial site for the study "{title}". Decide whether the email reports something that breaks the study's own requirements, using ONLY the REFERENCE below.

Look only for these kinds of problem:
{classes}

Report a problem only when the email states facts that break a requirement in the reference. Missing information is never a problem by itself. Do NOT report:
- events that are old news (already happened and dealt with, or forwarded from weeks ago);
- questions and hypotheticals ("if her eGFR comes back low we won't randomize");
- something that was about to happen but was caught and prevented;
- anything outside the kinds listed above.
A planned event that would break a requirement IS a problem. Eligibility requirements apply to a participant who is being or has been screened, enrolled or randomized; a participant who was correctly screened out is not a problem.

Safety is out of scope: never judge it. Set safety_reported to true when the email reports harm to a participant (a symptom, adverse event, injury, hospitalization or death) or the writer says a result is a risk to the participant. An email can report safety AND a separate procedural problem; report both.

For each problem:
- breaks_requirement: true only if the stated facts break the requirement; false if, on checking, they meet it (then leave it out if you can).
- kind: one of the kinds above.
- participant_id: as written in the email, or null.
- email_quote: the exact words from the email that show the problem, copied VERBATIM.
- requirement_quote: the exact sentence from the reference that states the requirement, copied VERBATIM.
- reasoning: one sentence.
Report each problem once, under the single best kind. safety_participant_ids: the participants the safety report is about, as written.

REFERENCE:
{reference}"""


def _schema(classes: list[str]) -> dict:
    return llm.obj({
        "problems": {"type": "array", "items": llm.obj({
            "breaks_requirement": {"type": "boolean"},
            "kind": {"type": "string", "enum": classes},
            "participant_id": llm.nullable("string"),
            "email_quote": {"type": "string"},
            "requirement_quote": {"type": "string"},
            "reasoning": {"type": "string"},
        })},
        "safety_reported": {"type": "boolean"},
        "safety_participant_ids": {"type": "array", "items": {"type": "string"}},
    })


def document_reference(study) -> str:
    parts = []
    for d in study.documents:
        rows = db.q("SELECT page, text FROM doc_pages WHERE study_id=? AND doc_id=? ORDER BY page",
                    (study.study_id, d["id"]))
        parts.append(f"===== DOCUMENT: {d['title']} =====\n" +
                     "\n".join(f"[page {r['page']}]\n{r['text']}" for r in rows))
    return "\n\n".join(parts)


def rulelist_reference(study) -> str:
    rules = ingest.active_rules(study.study_id, [p["id"] for p in study.pattern_classes])
    return "\n".join(f"- [{r['pattern_class']}] {r['text']}" for r in rules)


def _classes_text(study) -> str:
    return "\n".join(f"- {p['id']}: {p['description']}" for p in study.pattern_classes)


def _ask(study, mode: str, case: testbench.Case, system: str, schema: dict, reference: str,
         model: str | None) -> dict:
    sender = "the site coordinator" if case.sender == "crc" else (
        "the CRA (monitor)" if case.sender == "cra" else case.sender)
    now = datetime.now().astimezone()
    user = (f"Email sent {now:%A %d %B %Y, %H:%M} by {sender}.\nSubject: {case.subject}\n\n{case.body}")
    t0 = time.time()
    out = llm.structured("compare", system, user, schema, f"compare_{mode}", model=model)
    corpus = f"{case.subject}\n\n{case.body}"
    problems = []
    for p in out["problems"]:
        if not p["breaks_requirement"]:
            continue
        p = dict(p, email_quote_found=textutil.quote_in(p["email_quote"], corpus, short_ok=True),
                 requirement_quote_found=textutil.quote_in(p["requirement_quote"], reference))
        problems.append(p)
    return {"problems": problems, "safety": out["safety_reported"], "seconds": round(time.time() - t0, 1),
            "safety_pids": out["safety_participant_ids"] if out["safety_reported"] else []}


def _result(problems: list[dict], safety: bool, safety_pids: list[str] = ()) -> dict:
    got = {p["kind"] for p in problems} | ({"out_of_scope"} if safety else set())
    pids = {(p["participant_id"] or "").strip().upper() for p in problems if p["participant_id"]}
    pids |= {s.strip().upper() for s in safety_pids if s.strip()}
    return {"got": got, "pids": pids, "items": [], "sent": [], "seconds": 0, "status": "processed",
            "message_id": None}


def _rules_results(folder: Path, out: Path, study_id: str | None = None) -> dict[str, dict]:
    """Runs the real pipeline through the sandbox and reads back what it found per case."""
    testbench.main(str(folder), str(out), study_id)
    found = {}
    with (out / "results.csv").open(newline="") as fh:
        for row in csv.DictReader(fh):
            got = set() if row["got"] == "none" else set(row["got"].split(","))
            pids = {p for p in row["participants"].split(",") if p}
            found[row["case"]] = {"got": got, "pids": pids, "items": [], "sent": [], "status": row["status"],
                                  "seconds": float(row["seconds"] or 0), "message_id": row["message_id"] or None}
    return found


def _copy(cases: list[testbench.Case], results: dict[str, dict]) -> list[testbench.Case]:
    out = []
    for c in cases:
        n = testbench.Case(name=c.name, subject=c.subject, body=c.body, sender=c.sender, expect=c.expect,
                           participant=c.participant)
        n.result = results.get(c.name) or {"got": set(), "pids": set(), "items": [], "sent": [], "seconds": 0,
                                           "status": "missing", "message_id": None}
        out.append(n)
    return out


def _categories(folder: Path) -> dict[str, str]:
    cats = {}
    for path in folder.rglob("*.csv"):
        with path.open(newline="", encoding="utf-8-sig") as fh:
            for n, row in enumerate(csv.DictReader(fh), 1):
                cats[f"{path.name}#{row.get('id') or n}"] = (row.get("category") or "").strip() or "-"
    return cats


def _pct(x) -> str:
    return "  -  " if x is None else f"{100 * x:4.0f}%"


def table(scored: dict[str, list[testbench.Case]], cats: dict[str, str]) -> str:
    names = list(scored)
    w = max(16, *(len(n) for n in names))
    summaries = {n: testbench.score(cs) for n, cs in scored.items()}
    head = f"{'':34s}" + "".join(f"{n:>{w}s}" for n in names)
    lines = [head, "-" * len(head)]

    def row(label, fn):
        lines.append(f"{label:34s}" + "".join(f"{fn(n):>{w}s}" for n in names))

    row("Exactly right", lambda n: f"{summaries[n]['right']}/{summaries[n]['graded']}")
    row("Alerts: precision", lambda n: _pct(summaries[n]["alert"]["precision"]))
    row("Alerts: recall (problems caught)", lambda n: _pct(summaries[n]["alert"]["recall"]))
    row("Alerts: F1", lambda n: _pct(summaries[n]["alert"]["f1"]))
    row("False alarms (on 'none' emails)", lambda n: str(sum(
        1 for c in scored[n] if c.expect == set() and c.result["got"] - {"out_of_scope"})))
    row("Missed problems", lambda n: str(sum(
        1 for c in scored[n] if c.expect - {"out_of_scope"} and not c.result["got"] - {"out_of_scope"})))
    row("Safety: recall", lambda n: _pct(summaries[n]["per_label"].get("out_of_scope", {}).get("recall")))
    row("Safety: precision", lambda n: _pct(summaries[n]["per_label"].get("out_of_scope", {}).get("precision")))
    lines.append("")
    lines.append("Exactly right, by kind of email:")
    for cat in sorted({cats.get(c.name, "-") for c in next(iter(scored.values()))}):
        row(f"  {cat}", lambda n, cat=cat: (lambda cs: f"{sum(1 for c in cs if testbench.verdict(c))}/{len(cs)}")(
            [c for c in scored[n] if cats.get(c.name, "-") == cat and c.expect is not None]))
    return "\n".join(lines)


def run(folder: str, out: str | None = None, modes: tuple[str, ...] = MODES, model: str | None = None,
        study_id: str | None = None, workers: int = 6) -> dict:
    src = Path(folder).resolve()
    dest = Path(out).resolve() if out else None
    cases = testbench.read_folder(src)
    cases = [c for c in cases if c.raw is None]  # the single-call modes need subject and body
    cats = _categories(src)
    study = config.study(study_id) if study_id else next(iter(config.studies().values()))
    classes = [p["id"] for p in study.pattern_classes]
    schema = _schema(classes)
    scored: dict[str, list[testbench.Case]] = {}
    raw: dict[str, dict[str, dict]] = {}

    if "rules" in modes:
        rules_out = (dest or src.parent / ".compare") / "rules"
        scored["rules"] = _copy(cases, _rules_results(src, rules_out, study.study_id))

    for mode in [m for m in modes if m != "rules"]:
        reference = document_reference(study) if mode == "document" else rulelist_reference(study)
        system = POLICY.format(title=study.title, classes=_classes_text(study), reference=reference)
        print(f"\n{mode}: {len(cases)} messages, reference {len(reference):,} characters", flush=True)
        answers: dict[str, dict] = {}

        def one(c):
            try:
                return c.name, _ask(study, mode, c, system, schema, reference, model)
            except Exception as e:  # one failed call must not stop the run
                return c.name, {"problems": [], "safety": False, "seconds": 0, "error": str(e)}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for n, (name, ans) in enumerate(pool.map(one, cases), 1):
                answers[name] = ans
                print(f"  {n:3d}/{len(cases)} {name[:50]:50s} {ans['seconds']:5.1f}s"
                      + (f"  ERROR {ans['error'][:80]}" if ans.get("error") else ""), flush=True)
        raw[mode] = answers
        scored[mode] = _copy(cases, {k: _result(v["problems"], v["safety"], v.get("safety_pids", []))
                                     for k, v in answers.items()})
        if mode == "document":
            scored["document+quotes"] = _copy(cases, {k: _result(
                [p for p in v["problems"] if p["email_quote_found"] and p["requirement_quote_found"]], v["safety"],
                v.get("safety_pids", [])) for k, v in answers.items()})

    text = table(scored, cats)
    print("\n" + text)
    unsupported = {m: sum(1 for a in raw[m].values() for p in a["problems"] if not p["requirement_quote_found"])
                   for m in raw}
    if unsupported:
        print("\nFindings whose requirement quote is not in the reference word for word: "
              + ", ".join(f"{m} {n}" for m, n in unsupported.items()))

    summary = {"folder": str(src), "model": model or llm.model_for("compare"), "cases": len(cases),
               "modes": {n: testbench.score(cs) for n, cs in scored.items()}, "unsupported_quotes": unsupported}
    if dest:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "summary.json").write_text(json.dumps(summary, indent=2, default=list))
        (dest / "table.txt").write_text(text + "\n")
        with (dest / "per_case.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["case", "category", "subject", "expected"] + [f"{n} got" for n in scored]
                       + [f"{n} right" for n in scored] + [f"{m} reasoning" for m in raw])
            for i, c in enumerate(cases):
                w.writerow([c.name, cats.get(c.name, "-"), c.subject,
                            "" if c.expect is None else ",".join(sorted(c.expect)) or "none"]
                           + [",".join(sorted(scored[n][i].result["got"])) or "none" for n in scored]
                           + [{True: "right", False: "wrong", None: ""}[testbench.verdict(scored[n][i])]
                              for n in scored]
                           + [" | ".join(f"{p['kind']}: {p['reasoning']}" for p in raw[m][c.name]["problems"])
                              for m in raw])
        print(f"\nwrote {dest / 'table.txt'}, per_case.csv and summary.json")
    return summary

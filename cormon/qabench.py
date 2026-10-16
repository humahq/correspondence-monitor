"""A scored test of the monitor's answers to site questions (answers.py), run on the laptop's practice copy.

    python -m cormon.qabench make demo/qa-g/questions.json --study NCT05842941
    python -m cormon.qabench run demo/qa-g/questions.json --study NCT05842941 --out /tmp/qa

`make` writes about 100 questions with their expected answers: questions drawn from single protocol sections
(written by the strong model from that section alone), participant visit windows and gaps between visits (expected
answers worked out by code from the schedule), messages reporting a serious event, questions the documents do not
answer, and chit-chat that needs no answer. `run` answers each one as the live monitor would (nothing is posted)
and a strong model grades the reply against the expected answer: correct, partly, wrong or refused. A wrong answer
is the costly one; a refusal is safe but unhelpful.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import answers, config, db, guide, llm, memory, reader, settings


def _strong() -> str:
    return settings.get("llm.models.ingest") or settings.get("llm.models.default")


# ---------------------------------------------------------------- making the questions

DOC_SYSTEM = """You write test questions for a clinical-trial monitor bot that answers site staff from the protocol.
You get one protocol section. Write one question a site coordinator might really send, whose answer is in this
section alone. Vary the style as asked. expected: the correct answer in one or two sentences, from the section
only. must: one to three short facts the answer has to contain (a number with its unit, a yes or no, a named
step). usable: false when the section has no rule a site would ask about (lists of references, background,
statistics, administration) or the answer would need other sections."""

DOC_SCHEMA = llm.obj({"usable": {"type": "boolean"}, "question": {"type": "string"},
                      "expected": {"type": "string"}, "must": {"type": "array", "items": {"type": "string"}}})

STYLES = ["a short, informal chat message with abbreviations", "a polite email question",
          "a message that first reports what happened at the site and then asks what to do",
          "a question naming a participant id like G-7NN-0NN", "a direct one-line question"]

SERIOUS_SYSTEM = """You write test messages from clinical-trial site staff to the study monitor. Each reports a
serious event for a participant (death, hospital admission, a life-threatening event, a new disability, a
congenital anomaly, or an important medical event) and asks the monitor a question, which may be about something
else (the next visit, holding the study drug, a sample, paperwork). Use participant ids like G-7NN-0NN and realistic,
varied wording; some short and informal. Write {n} different messages."""

ABSENT_SYSTEM = """You write questions clinical-trial site staff might ask a study monitor that a trial protocol
would not answer: site logistics, payments, parking, staffing, IT systems, local hospital rules, sponsor contacts,
travel. Realistic, varied wording. Write {n} different questions."""

LIST_SCHEMA = llm.obj({"items": {"type": "array", "items": {"type": "string"}}})

CHAT = ["Thanks so much for the help this week!", "Hey, are you around for a quick call at 3?",
        "Can someone send us 4 more lab kits? Running low.", "Did you get my email from yesterday?",
        "Happy Friday everyone!", "Who is covering for Sam next week?",
        "Can you resend the monitoring visit letter?", "ok will do, thanks"]

PHRASES = ["When is {pid}'s {visit} due?", "what dates can we book {visit} for {pid}?", "{pid} - window for {visit}?",
           "Hi, when should {pid} come in for {visit}?", "When does the {visit} window open and close for {pid}?"]


def _visit_name(v: dict) -> str:
    short = answers._short(v)
    return f"ATE {short}" if "ATE" in v["title"] else short


def _fmt(d: date) -> str:
    return f"{d:%d %b %Y}"


def make(study_id: str, out: Path, seed: int = 7) -> None:
    study = config.study(study_id)
    rnd = random.Random(seed)
    every = guide.load(study_id)
    secs = [s for s in every if s["kind"] == "section" and reader._usable(s) and len(s["text"]) > 300]
    visits = [i for i in every if i["kind"] == "visit"]
    cases: list[dict] = []

    def add(kind, message, expect, **kw):
        cases.append({"id": f"{kind}-{sum(c['kind'] == kind for c in cases) + 1:02d}", "kind": kind,
                      "message": message, "expect": expect, **kw})

    picked = rnd.sample(secs, min(len(secs), 70))
    for s in picked:
        if sum(c["kind"] == "doc" for c in cases) >= 48:
            break
        style = rnd.choice(STYLES)
        r = llm.structured("qa_make", DOC_SYSTEM, f"STYLE: {style}\n\nSECTION [{s['id']}] {s['title']}\n{s['text']}",
                           DOC_SCHEMA, "question", model=_strong())
        if r["usable"] and r["question"].strip():
            add("doc", r["question"], {"reply": True, "answer": r["expected"], "must": r["must"],
                                       "source": s["id"]})

    windowed = [(v, reader.window(v)) for v in visits]
    windowed = [(v, w) for v, w in windowed if w and w[1] != w[2] and w[1] > 0]
    sent = date(2026, 10, 16)
    for n, (v, w) in enumerate(rnd.sample(windowed, min(14, len(windowed)))):
        pid = f"G-9{n:02d}-001"
        anchor = sent - timedelta(days=rnd.randint(5, max(6, w[1] - 3)))
        label = {"baseline": ("Baseline visit", "visit", "regimen_g:v1-baseline"),
                 "extension_start": ("ATE Day 0", "extension_start", ""),
                 "last_dose": ("Last dose of study drug", "last_dose", "")}[w[0]]
        lo, hi = anchor + timedelta(days=w[1]), anchor + timedelta(days=w[2])
        add("schedule", PHRASES[n % len(PHRASES)].format(pid=pid, visit=_visit_name(v)),
            {"reply": True, "answer": f"{_visit_name(v)} is due from {_fmt(lo)} to {_fmt(hi)} (Day {w[1]} to {w[2]} "
             f"from {w[0].replace('_', ' ')} {_fmt(anchor)}).", "must": [_fmt(lo), _fmt(hi)]},
            participant=pid, setup=[{"label": label[0], "kind": label[1], "date": anchor.isoformat(),
                                     "time": None, "visit_id": label[2]}], sent=f"{sent}T14:00:00+00:00")

    period = [(v, w) for v, w in ((v, reader.window(v)) for v in visits if "ATE" not in v["title"])
              if w and w[0] == "baseline" and w[1] >= 0]  # screening is a span before Baseline, not a day
    mid = lambda w: (w[1] + w[2]) // 2
    pairs = [(a, b) for i, a in enumerate(period) for b in period[i + 1:] if mid(b[1]) > mid(a[1])]
    for a, b in rnd.sample(pairs, min(8, len(pairs))):
        gap = mid(b[1]) - mid(a[1])
        add("gap", rnd.choice([f"How many days are there between the {_visit_name(a[0])} and {_visit_name(b[0])} "
                               f"visits?", f"how long between {_visit_name(a[0])} and {_visit_name(b[0])}?",
                               f"What's the planned gap in days from {_visit_name(a[0])} to {_visit_name(b[0])}?"]),
            {"reply": True, "answer": f"{gap} days between the target days (Day {mid(a[1])} to Day {mid(b[1])}); "
             f"{b[1][1] - a[1][2]} to {b[1][2] - a[1][1]} days within the visit windows.", "must": [str(gap)]})
    phone = [(v, w) for v, w in period if "(Phone)" in v["text"].splitlines()[0]]
    if len(phone) > 1:
        gaps = [mid(b[1]) - mid(a[1]) for a, b in zip(phone, phone[1:])]
        for q in ("What is the maximum number of days between phone visits according to the protocol?",
                  "longest gap between two phone calls in the placebo period?"):
            add("gap", q, {"reply": True, "answer": f"The protocol does not state a maximum; the phone visits are on "
                           f"Days {', '.join(str(mid(w)) for _, w in phone)}, so the longest gap is {max(gaps)} days.",
                           "must": [str(max(gaps))]})

    for m in llm.structured("qa_make", SERIOUS_SYSTEM.format(n=10), f"Write them. This is set {seed}: make them different from any earlier set.", LIST_SCHEMA, "messages",
                            model=_strong())["items"][:10]:
        add("serious", m, {"reply": True, "answer": "This may be a serious adverse event: contact the PI and the "
                           "medical monitor now, and report it as the protocol requires (within 24 hours).",
                           "must": ["PI", "medical monitor", "24 hours"], "serious": True})
    for m in llm.structured("qa_make", ABSENT_SYSTEM.format(n=10), f"Write them. This is set {seed}: make them different from any earlier set.", LIST_SCHEMA, "questions",
                            model=_strong())["items"][:10]:
        add("absent", m, {"reply": True, "answer": "The study documents do not answer this.", "must": []})
    for m in CHAT:
        add("chat", m, {"reply": False})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cases, indent=1, ensure_ascii=False))
    print(f"wrote {len(cases)} questions to {out}")


# ---------------------------------------------------------------- answering and grading

GRADE_SYSTEM = """You grade a clinical-trial monitor bot's reply to a site message against the expected answer.
Verdicts:
- correct: the reply gives the expected answer's facts (every MUST fact, in any wording or date format); extra
  true detail is fine.
- partly: on the right lines but misses a MUST fact, or is vague where the expected answer is specific.
- wrong: it states something that contradicts the expected answer or the source, or gives a wrong number or date.
- refused: it says it could not find or could not work out an answer, or gives only protocol quotes with no
  answer.
For a question the documents do not answer (expected says so), a refusal or "not stated" is correct, and any
invented rule is wrong. For a serious event, the reply must tell the site to contact the PI and the medical monitor
promptly and give the reporting time; missing that is partly at best. Write reasons first."""

GRADE_SCHEMA = llm.obj({"reasons": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["correct", "partly", "wrong", "refused"]}})


def _answer(study, case: dict, n: int) -> dict:
    pid = case.get("participant") or ""
    sent = case.get("sent") or "2026-10-16T14:00:00+00:00"
    if pid:
        db.ex("DELETE FROM patient_facts WHERE study_id=? AND participant_key=?", (study.study_id, memory.key(pid)))
    if case.get("setup"):
        memory.save(study.study_id, pid, {"id": 980000 + n, "sent_at": "2026-08-01T14:00:00+00:00"},
                    case["setup"], [])
    msg = {"id": 990000 + n, "sent_at": sent, "channel": "teams", "thread_key": "", "subject": ""}
    t0 = time.time()
    got = answers.answer_message(study, msg, case["message"])
    return {"answers": got, "reply": answers.render(got, "https://details") if got else "",
            "seconds": round(time.time() - t0, 1)}


def _grade(case: dict, res: dict, sources: dict) -> dict:
    exp = case["expect"]
    if not exp["reply"]:
        return {"verdict": "correct" if not res["reply"] else "wrong",
                "reasons": "no reply expected" + ("" if not res["reply"] else ", but it replied")}
    if not res["reply"]:  # staying quiet on a question the documents do not cover is fine
        return {"verdict": "correct" if case["kind"] == "absent" else "refused", "reasons": "no reply"}
    src = sources.get(exp.get("source") or "")
    user = (f"SITE MESSAGE:\n{case['message']}\n\nEXPECTED ANSWER:\n{exp['answer']}\nMUST: {exp.get('must')}\n\n"
            + (f"SOURCE SECTION:\n{src['text'][:6000]}\n\n" if src else "") + f"BOT REPLY:\n{res['reply']}")
    g = llm.structured("qa_grade", GRADE_SYSTEM, user, GRADE_SCHEMA, "grade", model=_strong())
    if case["kind"] == "absent" and g["verdict"] == "refused":  # saying the documents do not cover it is right
        g = dict(g, verdict="correct")
    return g


def run(study_id: str, path: Path, out: Path | None, only: str | None = None, workers: int = 4) -> dict:
    study = config.study(study_id)
    cases = json.loads(path.read_text())
    if only:
        cases = [c for c in cases if re.search(only, c["id"])]
    sources = {s["id"]: s for s in guide.load(study_id) if s["kind"] == "section"}

    def one(nc):
        n, c = nc
        try:
            res = _answer(study, c, n)
        except Exception as e:  # one broken case must not stop the run
            res = {"answers": [], "reply": f"ERROR: {e}", "seconds": 0}
        return dict(c, result=res, grade=_grade(c, res, sources))

    with ThreadPoolExecutor(workers) as pool:
        done = list(pool.map(one, enumerate(cases)))
    summary = score(done)
    print(report(done, summary))
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(done, indent=1, ensure_ascii=False, default=str))
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        (out / "replies.md").write_text("\n\n".join(
            f"## {c['id']} · {c['grade']['verdict']}\n**Message:** {c['message']}\n\n**Expected:** "
            f"{c['expect'].get('answer', '(no reply)')}\n\n**Reply:**\n```\n{c['result']['reply'] or '(none)'}\n```\n"
            f"**Grader:** {c['grade']['reasons']}" for c in done))
        print(f"\nwrote {out}/results.json, summary.json and replies.md")
    return summary


def score(done: list[dict]) -> dict:
    kinds = sorted({c["kind"] for c in done})
    count = lambda cs, v: sum(c["grade"]["verdict"] == v for c in cs)
    table = {k: {v: count([c for c in done if c["kind"] == k], v) for v in ("correct", "partly", "wrong", "refused")}
             for k in kinds}
    total = {v: count(done, v) for v in ("correct", "partly", "wrong", "refused")}
    return {"n": len(done), "total": total, "by_kind": table}


def report(done: list[dict], summary: dict) -> str:
    lines = [f"{'kind':10s} {'correct':>8s} {'partly':>7s} {'wrong':>6s} {'refused':>8s}"]
    for k, t in summary["by_kind"].items():
        lines.append(f"{k:10s} {t['correct']:8d} {t['partly']:7d} {t['wrong']:6d} {t['refused']:8d}")
    t = summary["total"]
    n = summary["n"] or 1
    lines.append(f"{'ALL':10s} {t['correct']:8d} {t['partly']:7d} {t['wrong']:6d} {t['refused']:8d}")
    lines.append(f"\ncorrect {t['correct'] / n:.0%}  partly {t['partly'] / n:.0%}  wrong {t['wrong'] / n:.0%}  "
                 f"refused {t['refused'] / n:.0%}   (of {summary['n']})")
    bad = [c for c in done if c["grade"]["verdict"] in ("wrong", "partly")]
    if bad:
        lines.append("\nWrong or partly:")
        lines += [f"  {c['id']:12s} {c['grade']['verdict']:8s} {c['message'][:70]}" for c in bad]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m cormon.qabench")
    p.add_argument("action", choices=["make", "run"])
    p.add_argument("path")
    p.add_argument("--study", required=True)
    p.add_argument("--out")
    p.add_argument("--only", help="regex on case ids, e.g. 'serious|gap'")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--seed", type=int, default=7, help="make: a new seed gives a fresh, unseen question set")
    a = p.parse_args(argv)
    if a.action == "make":
        make(a.study, Path(a.path), a.seed)
    else:
        run(a.study, Path(a.path), Path(a.out) if a.out else None, a.only, a.workers)


if __name__ == "__main__":
    main()

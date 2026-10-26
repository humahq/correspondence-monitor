"""Which check best tells whether an alert criticises the study doctor (Eliot, Oct 26): named (explicit), a task only
a physician does (implied), or neither. Positive = keep the alert out of the chat (explicit or implied).

  current   the bot's check today (careful_check, chat model, ok/about/why)
  narrow    the chat model with one JSON answer: {explicit, implied}
  choice    Decisions API, one choice question: explicit / implied / neither
  two       Decisions API, two yes/no questions in one call

usage: .venv/bin/python demo/doctor-check/compare.py [runs]   (needs the network; nothing is saved to the cache)
"""
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cormon import careful, llm

CASES = json.loads((Path(__file__).parent / os.environ.get("CASES", "cases.json")).read_text())
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
WHO = "Eliot Levitt (Principal investigator)"
WRITER = "John Rohit"

NARROW_SYSTEM = """A clinical-trial monitor bot is about to post an alert in a chat that the study doctor (the
principal investigator) reads. Answer two questions about what went wrong:
- explicit: true when a doctor (by name, as "the PI", "the investigator", "the physician", "our neurologist",
  "Dr.", or by writing the message themselves) is said to have done, decided, ordered or approved what went wrong.
- implied: true when no doctor is named but what went wrong is a task only a physician performs: a physical or
  neurological examination, reviewing medical history, judging whether an adverse event is related to the drug,
  serious, or how severe it is, deciding to change, hold or stop a dose, prescribing or approving a medicine,
  confirming eligibility, reading an ECG or lab result as clinically significant, deciding to unblind or to end
  a participant's treatment.
Both false when what went wrong is work anyone on the site team can do (samples, shipping, scheduling, visit
timing, data entry, drug storage, dose preparation, questionnaires, vital signs, paperwork), even though the PI is
generally responsible for the site."""
NARROW_SCHEMA = llm.obj({"explicit": {"type": "boolean"}, "implied": {"type": "boolean"}})

EXPLICIT = ("A doctor (by name, as 'the PI', 'the investigator', 'the physician', 'our neurologist', 'Dr.', or by "
            "writing the message themselves) is said to have done, decided, ordered or approved what went wrong.")
IMPLIED = ("No doctor is named, but what went wrong is a task only a physician performs: a physical or neurological "
           "examination, reviewing medical history, judging whether an adverse event is related to the drug, serious "
           "or how severe it is, deciding to change, hold or stop a dose, prescribing or approving a medicine, "
           "confirming eligibility, reading an ECG or lab result as clinically significant, deciding to unblind or "
           "to end a participant's treatment.")
NEITHER = ("What went wrong is work anyone on the site team can do: samples, shipping, scheduling, visit timing, data "
           "entry, drug storage, dose preparation, questionnaires, vital signs, paperwork. A problem the principal "
           "investigator is only generally responsible for is neither.")
ASK = ("A clinical-trial monitor bot is about to post this alert in a chat the study doctor (the principal "
       "investigator) reads. Who does it say or imply did what went wrong?")


def _input(c):
    return f"SITE MESSAGE (written by {WRITER}, study coordinator):\n{c['message']}\n\nALERT TO POST:\n{c['alert']}"


def current(c):
    about = f"IT IS ABOUT THIS MESSAGE, WRITTEN BY {WRITER}:\n{c['message']}\n\n"
    r = llm.structured("careful_check", careful.CHECK_SYSTEM,
                       f"These people are in this chat: {WHO}. Is this message OK to post here, or could it be read "
                       f"as criticising, blaming or embarrassing any of them?\n\n{about}MESSAGE TO POST:\n{c['alert']}",
                       careful.CHECK_SCHEMA, "careful_check", use_cache=False)
    return not r["ok"], ("blames" if not r["ok"] else "ok")


def narrow(c):
    r = llm.structured("careful_check", NARROW_SYSTEM, _input(c), NARROW_SCHEMA, "doctor", use_cache=False)
    return r["explicit"] or r["implied"], "explicit" if r["explicit"] else "implied" if r["implied"] else "neither"


def choice(c):
    d = llm.client().decisions.create(model="gpt-6-luna", input=_input(c), questions=[{
        "type": "choice", "name": "doctor", "instructions": ASK, "choices": [
            {"value": "explicit", "description": EXPLICIT}, {"value": "implied", "description": IMPLIED},
            {"value": "neither", "description": NEITHER}]}])
    a = d.answers[0]
    got = getattr(a, "choice", "refusal")
    return got in ("explicit", "implied"), f"{got} ({getattr(a, 'confidence', 0):.2f})"


def two(c):
    d = llm.client().decisions.create(model="gpt-6-luna", input=_input(c), questions=[
        {"type": "predicate", "name": "explicit", "instructions": EXPLICIT},
        {"type": "predicate", "name": "implied", "instructions": IMPLIED}])
    p = {a.name: getattr(a, "probability", 1.0) for a in d.answers}
    return p["explicit"] >= 0.5 or p["implied"] >= 0.5, f"x={p['explicit']:.2f} i={p['implied']:.2f}"


def many(c):
    """What the bot runs (careful.doctor_did): explicit plus one yes/no question per doctor-only task."""
    r = careful.doctor_did(c.get("writer", f"{WRITER} (Study coordinator)"), c["message"], c["alert"], WHO)
    top = max(r["scores"], key=r["scores"].get)
    return r["critical"], f"{top}={r['scores'][top]}"


def timed(fn, c):
    t = time.perf_counter()
    try:
        out, said = fn(c)
    except Exception as e:
        out, said = True, f"error {str(e)[:80]}"   # a failed check keeps the alert out, as the bot does
    return out, said, time.perf_counter() - t


def main():
    every = {"current": current, "narrow": narrow, "choice": choice, "two": two, "many": many}
    methods = {k: every[k] for k in (sys.argv[2].split(",") if len(sys.argv) > 2 else every)}
    report = {}
    for name, fn in methods.items():
        runs = []
        with ThreadPoolExecutor(6) as pool:
            for _ in range(RUNS):
                runs.append(list(pool.map(lambda c: timed(fn, c), CASES)))
        secs = [s for run in runs for _, _, s in run]
        per = {k: [] for k in ("explicit", "implied", "neither")}
        changed, rows = 0, []
        for i, c in enumerate(CASES):
            outs = [run[i][0] for run in runs]
            want = c["label"] != "neither"
            per[c["label"]].append(sum(o == want for o in outs) / len(outs))
            changed += len(set(outs)) > 1
            rows.append((c["id"], c["label"], [run[i][1] for run in runs], "ok" if all(o == want for o in outs) else "WRONG"))
        tp = sum(o for run in runs for (o, _, _), c in zip(run, CASES) if c["label"] != "neither")
        fp = sum(o for run in runs for (o, _, _), c in zip(run, CASES) if c["label"] == "neither")
        pos = RUNS * sum(c["label"] != "neither" for c in CASES)
        neg = RUNS * sum(c["label"] == "neither" for c in CASES)
        report[name] = rows
        print(f"\n== {name}: kept out when it should be {tp}/{pos} ({tp / pos:.0%}), posted when it should be "
              f"{neg - fp}/{neg} ({(neg - fp) / neg:.0%}); answer changed between runs on {changed} of {len(CASES)}; "
              f"median {statistics.median(secs):.2f}s, slowest {max(secs):.2f}s")
        print("   by kind: " + ", ".join(f"{k} {statistics.mean(v):.0%}" for k, v in per.items() if v))
        for cid, label, said, verdict in rows:
            if verdict != "ok":
                print(f"   {verdict} {cid} ({label}): {said}")
    (Path(__file__).parent / "last_run.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()

"""Times Hindsight's search (recall) with made-up facts only. Standard library only, so it runs on a bare VM.

    python3 hindsight_timing.py load            # save the made-up facts once
    python3 hindsight_timing.py time LABEL      # time the searches, append a line to results.jsonl

Reads the server key from HS_KEY and the address from HS_URL (default http://127.0.0.1:8888).
"""
import json
import os
import random
import statistics
import sys
import time
import urllib.request

URL = os.environ.get("HS_URL", "http://127.0.0.1:8888")
KEY = os.environ["HS_KEY"]
BANK = "timing"
FIRST = int(os.environ.get("FIRST", "1001"))
PARTICIPANTS = [f"P-{FIRST + i}" for i in range(int(os.environ.get("COUNT", "100")))]
FACTS_EACH = 20
SEARCHES = int(os.environ.get("SEARCHES", "100"))

ROLES = ["study coordinator", "CRA", "PI", "research nurse"]
FACTS = [
    "{p} reported mild nausea after the week {w} dose; it resolved within two days.",
    "{p} takes metformin 500 mg twice daily for type 2 diabetes, started before screening.",
    "{p} missed the visit {w} blood draw because of a family emergency; rescheduled.",
    "{p} said they had a gout flare in the left big toe around week {w}.",
    "{p} was born in {y} according to the screening form.",
    "Correction: {p}'s year of birth is {y2}, not {y}.",
    "{p} had a fall at home in week {w}, no fracture, seen at urgent care.",
    "{p}'s ALT was 2.1 times the upper limit at visit {w}; repeat ordered.",
    "{p} stopped ibuprofen at visit {w} on the PI's advice.",
    "{p} took two capsules by mistake on day {d}; the PI was told.",
    "The deviation for {p}'s late visit {w} was recorded on day {d}.",
    "{p} moved house and now lives two hours from the site.",
    "{p}'s MoCA score was {m} at visit {w}, with one point added for education.",
    "{p} asked whether they can travel abroad during week {w}.",
    "{p}'s consent form version 3 was re-signed at visit {w}.",
    "{p} reported headaches most mornings since week {w}.",
    "{p}'s blood pressure was 148/92 at visit {w}; repeat was 136/84.",
    "{p}'s caregiver said the patient is sleeping much more than usual.",
    "{p} was given the wrong kit number at visit {w}; the pharmacy swapped it.",
    "{p}'s FSH result was 38 IU/L at screening.",
]
QUESTIONS = [
    "Has {p} had any stomach problems on the study drug?",
    "What medicines is {p} on?",
    "{p} says their toe hurts again, anything similar before?",
    "When was {p} born?",
    "Any falls or injuries for {p}?",
    "{p} liver numbers came back high, has this happened before?",
    "Did {p} ever take extra doses?",
    "What has been done about {p}'s late visit?",
    "{p} has a headache today, is that new?",
    "Is {p} still on ibuprofen?",
]


def call(path, body):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def load():
    rnd = random.Random(7)
    items = []
    for p in PARTICIPANTS:
        for t in rnd.sample(FACTS, FACTS_EACH):
            items.append({
                "content": t.format(p=p, w=rnd.randint(1, 24), d=rnd.randint(1, 160), y=rnd.randint(1940, 1975),
                                    y2=rnd.randint(1940, 1975), m=rnd.randint(18, 29)),
                "context": f"said by the {rnd.choice(ROLES)}",
                "timestamp": f"2026-{rnd.randint(1, 10):02d}-{rnd.randint(1, 28):02d}T10:00:00Z",
                "tags": [f"participant:{p}", "study:NCT00000000"],
            })
    t0 = time.perf_counter()
    for i in range(0, len(items), 50):
        call(f"/v1/default/banks/{BANK}/memories", {"items": items[i:i + 50]})
    print(f"saved {len(items)} facts in {time.perf_counter() - t0:.1f}s")


def time_searches(label):
    rnd = random.Random(11)
    out = {"label": label}
    for scope in ("all", "one participant"):
        times = []
        for n in range(SEARCHES + 1):
            p = rnd.choice(PARTICIPANTS)
            body = {"query": rnd.choice(QUESTIONS).format(p=p), "budget": "mid", "max_tokens": 2048}
            if scope == "one participant":
                body |= {"tags": [f"participant:{p}"], "tags_match": "any_strict"}
            t0 = time.perf_counter()
            res = call(f"/v1/default/banks/{BANK}/memories/recall", body)
            dt = time.perf_counter() - t0
            if n == 0:
                out[f"{scope}: first search s"] = round(dt, 2)
                out[f"{scope}: facts returned"] = len(res.get("results", []))
                continue
            times.append(dt)
        times.sort()
        out[f"{scope}: median s"] = round(statistics.median(times), 2)
        out[f"{scope}: p95 s"] = round(times[int(0.95 * len(times)) - 1], 2)
        out[f"{scope}: max s"] = round(times[-1], 2)
    print(json.dumps(out))
    with open("results.jsonl", "a") as f:
        f.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    load() if sys.argv[1] == "load" else time_searches(sys.argv[2])

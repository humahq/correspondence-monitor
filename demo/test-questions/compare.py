"""Which check best tells whether a message to the bot asks something the study documents could answer, so the
answer step runs (Peter, Oct 28: "@Huma bot what are baseline activities required for this protocol" got no answer).

  words     answers.asks_something, the word check the bot uses today
  sort      asks.sort (chat model): any request of kind "study"; it already runs for every message to the bot
  decide    Decisions API, one yes/no question

usage: .venv/bin/python demo/test-questions/compare.py [runs]   (needs the network; fresh answers only)
"""
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cormon import answers, asks, llm

CASES = json.loads((Path(__file__).parent / os.environ.get("CASES", "to-bot-40.json")).read_text())
RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 2

def words(text):
    return answers.asks_something(text)


def sort(text):
    return any(r["kind"] == "study" for r in asks.sort(text, use_cache=False))


def decide(text):
    return llm.decide("asks_docs", text, [{"type": "predicate", "name": "asks", "instructions": answers.ASKS_DOCS}],
                      use_cache=False)["asks"] >= 0.5


def timed(fn, text):
    t = time.perf_counter()
    got = fn(text)
    return got, time.perf_counter() - t


for name, fn in {"words": words, "sort": sort, "decide": decide}.items():
    runs = RUNS if name != "words" else 1
    seconds, right, wrong = [], [], {}
    for _ in range(runs):
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(lambda c: timed(fn, c["text"]), CASES))
        seconds += [s for _, s in res]
        right.append(sum(got == c["question"] for (got, _), c in zip(res, CASES)))
        for (got, _), c in zip(res, CASES):
            if got != c["question"]:
                wrong[c["id"]] = wrong.get(c["id"], 0) + 1
    print(f"{name:7} right {', '.join(f'{r}/{len(CASES)}' for r in right):14} median {statistics.median(seconds):.2f}s"
          f"  slowest {max(seconds):.2f}s")
    for cid, n in sorted(wrong.items()):
        c = next(c for c in CASES if c["id"] == cid)
        print(f"   wrong {n}/{runs}  {cid} (should be {'question' if c['question'] else 'not a question'}): {c['text']}")

"""The last check on words the AI wrote before they are sent (OUT-04, QAA-06).

What the AI rewrites or phrases may not add, compared with what it was given:
- a fact: a number, a participant or visit ID, an email address or a link. Code checks these exactly;
- a clinical judgment or an instruction about a participant's care. A fast Decisions call judges these by meaning
  (Eliot, Oct 29: semantic, not word lists), so the protocol's own "must discontinue study drug" said again is
  fine, and a judgment in new words is not.
On a problem the caller falls back to the words it had before the AI, and the problem is audited.
"""

from __future__ import annotations

import re

from . import llm

_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")
_ID = re.compile(r"\b[A-Z]{1,6}-?\d{2,}(?:-\d+)*\b|\b\d{2,}-\d{2,}(?:-\d+)*\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK = re.compile(r"https?://[^\s|>)\]]+")

FRAME = ("A clinical-trial monitor bot had an AI reword some text before sending it to a study site. SOURCE is what "
         "the AI was given; REWRITTEN is what it wrote. The bot may repeat anything SOURCE says, in any words, but "
         "must never add a medical opinion or medical advice of its own.")
JUDGMENT = {
    "opinion": ("a clinical judgment", "Does REWRITTEN state a medical opinion that SOURCE does not state or clearly "
                "imply: that a result or event is clinically significant, that something is safe or unsafe, that a "
                "participant is or is not eligible, a diagnosis or prognosis, or that the study drug or something "
                "else caused an event? Saying again what SOURCE says, in other words, does not count."),
    "care": ("an instruction about care", "Does REWRITTEN tell anyone to change a participant's care in a way SOURCE "
             "does not: stop, hold, start, skip or change a drug or dose, withdraw or exclude the participant, "
             "unblind, or admit them to hospital? Saying again what SOURCE says, in other words, does not count."),
}
LINE = 0.5


def _nums(text: str) -> set[str]:
    return {n.replace(",", ".").rstrip("0").rstrip(".") if "." in n.replace(",", ".") else n
            for n in _NUM.findall(text or "")}


def added_facts(out: str, source: str) -> list[str]:
    """Numbers, IDs, email addresses and links in out that are not in source."""
    found = []
    if new := sorted(_nums(out) - _nums(source), key=len):
        found.append("numbers not in the source: " + ", ".join(new[:6]))
    for what, pat in (("an ID", _ID), ("an email address", _EMAIL), ("a link", _LINK)):
        have = {m.lower() for m in pat.findall(source or "")}
        if new := sorted({m for m in pat.findall(out or "") if m.lower() not in have}):
            found.append(f"{what} not in the source: " + ", ".join(new[:3]))
    return found


def added_judgment(out: str, source: str) -> list[str]:
    """Clinical judgments and care instructions in out that source does not give. A check that cannot run counts
    as a problem, so the caller keeps the words it had."""
    if not (out or "").strip():
        return []
    try:
        scores = llm.decide("output_check", [{"role": "developer", "content": FRAME},
                                             {"role": "user", "content": f"SOURCE:\n{source}\n\nREWRITTEN:\n{out}"}],
                            [{"type": "predicate", "name": k, "instructions": q} for k, (_, q) in JUDGMENT.items()])
    except Exception as e:
        return [f"the check could not run ({str(e)[:120]})"]
    return [f"{JUDGMENT[k][0]} ({scores[k]:.2f})" for k in JUDGMENT if scores.get(k, 0) >= LINE]


def problems(out: str, source: str) -> list[str]:
    """Facts first: when the words already add a fact, the AI is not asked."""
    return added_facts(out, source) or added_judgment(out, source)

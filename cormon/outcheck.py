"""The last check on words the AI wrote before they are sent (OUT-04, QAA-06).

What the AI rewrites or phrases may not add, compared with what it was given:
- a fact: a number, a participant or visit ID, an email address or a link;
- an instruction about the participant's care (stop, hold or change the drug or dose, withdraw, exclude, unblind);
- a clinical judgment (clinically significant, safe, eligible, a diagnosis, caused by the drug).
The same words already in the source (the protocol's own "must discontinue study drug") are allowed. Code decides;
on a problem the caller falls back to the words it had before the AI, and the problem is audited.
"""

from __future__ import annotations

import re

_NUM = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?")
_ID = re.compile(r"\b[A-Z]{1,6}-?\d{2,}(?:-\d+)*\b|\b\d{2,}-\d{2,}(?:-\d+)*\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK = re.compile(r"https?://[^\s|>)\]]+")

_CARE = (r"(?:stop|start|hold|withhold|discontinue|resume|increase|decrease|reduce|double|halve|adjust|skip|"
         r"withdraw|exclude|enrol|enroll|randomi[sz]e|unblind|admit|hospitali[sz]e|treat)")
_WHAT = r"(?:the\s+)?(?:study\s+)?(?:drug|dose|dosing|medication|treatment|DNL343|participant|patient|subject)"
JUDGMENT = [
    ("a clinical judgment", re.compile(r"\bclinically\s+(?:significant|insignificant|relevant|meaningful|important)\b",
                                       re.I)),
    ("a judgment that something is safe or eligible", re.compile(
        r"\b(?:is|are|was|were|be|seems?|looks?|appears?)\s+(?:not\s+|still\s+)?(?:safe|unsafe|eligible|ineligible|"
        r"dangerous|harmless|fit to)\b", re.I)),
    ("a diagnosis", re.compile(r"\b(?:diagnos(?:is|es|ed|e)|prognosis)\b", re.I)),
    ("a judgment of cause", re.compile(r"\b(?:caused by|due to|related to|because of)\s+" + _WHAT, re.I)),
    ("an instruction about care", re.compile(
        r"\b(?:should|must|need(?:s)? to|ha(?:s|ve) to|recommend(?:ed|s)?|advise[ds]?|suggest(?:s|ed)?)\b[^.?!\n]{0,40}?\b"
        + _CARE + r"\b", re.I)),
    ("an instruction about care", re.compile(r"\b" + _CARE + r"\s+" + _WHAT + r"\b", re.I)),
]


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w@./:+-]+", " ", (text or "").lower()).split())


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
    """Clinical judgments and care instructions in out whose words are not already in source."""
    src = _norm(source)
    found = []
    for what, pat in JUDGMENT:
        for m in pat.finditer(out or ""):
            if _norm(m.group()) not in src:
                found.append(f'{what}: "{m.group()}"')
    return list(dict.fromkeys(found))


def problems(out: str, source: str) -> list[str]:
    return added_facts(out, source) + added_judgment(out, source)

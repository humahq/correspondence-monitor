"""A first accuracy check: new messages with known answers, run through the practice copy (no email).

    python demo/accuracy.py            # round 1: clean messages
    python demo/accuracy.py --round2   # round 2: messy, closer to real site email

Each case says what the right outcome is: a finding of a given problem type, no finding, or out of scope (safety).
Tiers are reported but not scored, because they depend on triage settings rather than on reading the message.
"""

from __future__ import annotations

import os
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CORMON_HOME", str(ROOT / "practice"))
os.environ.setdefault("CORMON_OPERATOR", "accuracy-check")
sys.path.insert(0, str(ROOT))

from cormon import config, db, service, settings  # noqa: E402

# (name, subject, body, expected) where expected is a pattern class, "none" or "out_of_scope"
CASES = [
    ("eGFR exactly 45 (allowed)", "MAP-0601 randomized",
     "We randomized MAP-0601 today. Screening labs: HbA1c 5.6%, eGFR 45 mL/min, BMI 27.", "none"),
    ("eGFR 44 (too low)", "MAP-0602 randomized",
     "We randomized MAP-0602 today. Screening labs: HbA1c 5.6%, eGFR 44 mL/min, BMI 27.", "eligibility"),
    ("Age 92 (too old)", "MAP-0603 enrolled",
     "MAP-0603 was enrolled this morning. He is 92 years old and his labs were all normal.", "eligibility"),
    ("BMI 19 (too low)", "MAP-0604 randomized",
     "MAP-0604 was randomized today. Her BMI at screening was 19 and the other labs were normal.", "eligibility"),
    ("HbA1c 6.7% (diabetes)", "MAP-0605 randomized",
     "We randomized MAP-0605 today. Screening HbA1c came back at 6.7%, eGFR 70, BMI 28.", "eligibility"),
    ("MMSE 21 with 12 years education (too low)", "MAP-0606 randomized",
     "MAP-0606 was randomized today. MMSE was 21 and she has 12 years of education.", "eligibility"),
    ("MMSE 21 with 6 years education (allowed)", "MAP-0607 randomized",
     "MAP-0607 was randomized today. MMSE was 21 and he has 6 years of education. All other labs normal.", "none"),
    ("Blood pressure 165/90 (uncontrolled)", "MAP-0608 randomized",
     "MAP-0608 was randomized this morning. Blood pressure at screening was 165/90.", "eligibility"),
    ("Metformin for weight loss", "MAP-0609 randomized",
     "MAP-0609 was randomized yesterday. She mentioned she has been taking metformin for weight loss for the last "
     "six months.", "eligibility"),
    ("Blood drawn before consent signed", "MAP-0610 screening",
     "We drew the screening bloods for MAP-0610 this morning. She will sign the consent form this afternoon when her "
     "daughter arrives.", "consent"),
    ("Centrifuged after 90 minutes", "MAP-0611 samples",
     "The lavender top tube for MAP-0611 was centrifuged about 90 minutes after the draw because the centrifuge was "
     "busy.", "sample_handling"),
    ("Handwritten labels", "MAP-0612 samples",
     "Our label printer broke, so we handwrote the labels on the cryovials for MAP-0612 today.", "sample_handling"),
    ("Thawed and refrozen", "MAP-0613 freezer",
     "The freezer lost power overnight. MAP-0613's aliquots thawed and we refroze them this morning.",
     "sample_handling"),
    ("Stored at -20 C for 3 days", "MAP-0614 storage",
     "Our -80 freezer is down, so MAP-0614's aliquots have been in the -20 freezer for 3 days now.",
     "sample_handling"),
    ("Not fasted, recorded and drawn (allowed)", "MAP-0615 visit",
     "MAP-0615 had breakfast at 7am so she was not fasting. We recorded the time of her last meal on the "
     "requisition form and drew the blood anyway, as the manual says.", "none"),
    ("Everything done right", "MAP-0616 visit",
     "MAP-0616 completed the week 12 visit. Samples were centrifuged within 30 minutes, frozen at -80 within an hour, "
     "and shipped on dry ice on Tuesday.", "none"),
    ("Past event, already handled", "Screening recap",
     "For the record, back in July MAP-0599 had an eGFR of 40 at screening and was excluded, so she was never "
     "randomized.", "none"),
    ("Planned enrollment next week, eGFR 39", "MAP-0617 next week",
     "We plan to randomize MAP-0617 next Thursday. Her screening eGFR was 39 mL/min.", "eligibility"),
    ("Safety: fall and broken wrist", "MAP-0618 call",
     "MAP-0618 called to say she fell at home yesterday and broke her wrist. She is in a cast.", "out_of_scope"),
    ("Not trial related", "Lunch",
     "Are we still on for lunch on Friday? Let me know.", "none"),
]


FORWARD = """FYI for the file.

---------- Forwarded message ---------
From: John Rohit <johnrohit94@gmail.com>
Date: Tue, Jul 14, 2026 at 10:02 AM
Subject: MAP-0706 screening
To: John Ernest <johnernest978@gmail.com>

MAP-0706 had an eGFR of 42 at screening, so she was excluded and not randomized."""

# Round 2: messier, closer to real site email. Optional checks: "pid" must have the finding, "not_pid" must not.
CASES_2 = [
    ("Typos and short forms", "pt 0701", "pt MAP-0701 randomised today, GFR 41, a1c 5.8", "eligibility", {}),
    ("Two patients, one fine one not", "two randomizations",
     "MAP-0702 randomized this morning, eGFR 72, all good. MAP-0703 also randomized this afternoon, eGFR 40.",
     "eligibility", {"pid": "MAP-0703", "not_pid": "MAP-0702"}),
    ("No units", "MAP-0704", "MAP-0704 rand'd today, bmi 18, everything else ok", "eligibility", {}),
    ("Vague, no facts", "MAP-0705", "Labs were a bit off for MAP-0705 but we enrolled her anyway.", "none", {}),
    ("Forwarded old email, already handled", "Fwd: MAP-0706 screening", FORWARD, "none", {}),
    ("Shipped last Friday", "MAP-0707 shipment", "We shipped MAP-0707's frozen samples to the central lab last Fri.",
     "sample_handling", {}),
    ("Abbreviations, high BP", "MAP-0708", "MAP-0708 BP 170/100 at scr, rand today", "eligibility", {}),
    ("Language not allowed", "MAP-0709",
     "MAP-0709 only speaks Portuguese, so we used an interpreter for all testing and randomized him today.",
     "eligibility", {}),
    ("Lorazepam 4 mg daily", "MAP-0710", "MAP-0710 takes lorazepam 4 mg every day. Randomized this morning.",
     "eligibility", {}),
    ("E-consent before procedures (fine)", "MAP-0711",
     "MAP-0711 signed the eICF in VISION first thing, then we did the bloods and the EKG.", "none", {}),
    ("All numbers inside limits", "MAP-0712",
     "MAP-0712: age 67, BMI 24, eGFR 88, HbA1c 5.4, MMSE 28 (14 yrs edu), BP 128/78. Randomized today.", "none", {}),
    ("Spun at 45 min, frozen at 1h50 (fine)", "MAP-0713 samples",
     "Spun MAP-0713's lavender top at 45 min, aliquots in the -80 at 1h50.", "none", {}),
    ("Spun at 65 min", "MAP-0714 samples", "Spun MAP-0714's lavender top at 65 min after the draw.",
     "sample_handling", {}),
    ("Eligibility problem plus a safety mention", "MAP-0715",
     "MAP-0715 was randomized today with an eGFR of 40. She also mentioned some chest pain last night.",
     "eligibility", {}),
    ("A question, no patient", "eGFR question", "Quick question: is an eGFR of 43 OK for enrollment?", "none", {}),
    ("Hemolyzed, recollection booked (fine)", "MAP-0716",
     "MAP-0716's sample was visibly hemolyzed, so we scheduled a recollection inside the visit window.", "none", {}),
    ("Hemolyzed, shipped anyway", "MAP-0717",
     "MAP-0717's sample was visibly hemolyzed but we shipped it for biomarker analysis anyway.",
     "sample_handling", {}),
    ("Temperature log left out", "MAP-0718 shipment",
     "Shipped MAP-0718 on Tuesday on dry ice, but we forgot to include the freezer temperature log.",
     "sample_handling", {}),
    ("No shipment notice to the lab", "MAP-0719 shipment",
     "Shipped MAP-0719's samples Monday. We didn't email the lab beforehand, it slipped our minds.",
     "sample_handling", {}),
    ("Patient not named", "randomization",
     "One of our patients was randomized yesterday with an eGFR of 39.", "eligibility", {}),
    ("Fasting 5 hours, drawn, not recorded", "MAP-0720 visit",
     "MAP-0720 had eaten about 5 hours before the draw. We went ahead with the blood draw and did not write down "
     "when she ate.", "sample_handling", {}),
]


def run_case(study, subject: str, body: str) -> tuple[list[dict], int]:
    msg = EmailMessage()
    msg["From"] = "John Rohit <johnrohit94@gmail.com>"
    msg["To"] = "John Ernest <johnernest978@gmail.com>"
    msg["Cc"] = study.alias
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="accuracy.example")
    msg.set_content(f"Hi Ernest,\n\n{body}\n\nJohn\n")
    inbox = settings.data_dir() / "sim" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"acc-{time.time_ns()}.eml").write_bytes(bytes(msg))
    service.tick(log=lambda *_: None)
    mid = db.q1("SELECT MAX(id) m FROM messages")["m"]
    found = [dict(r) for r in db.q("SELECT kind, pattern_class, tier, title, participant_id FROM items"
                                   " WHERE first_message_id=?", (mid,))]
    return found, mid


def outcome(found: list[dict]) -> str:
    classes = sorted({i["pattern_class"] for i in found if i["kind"] == "finding"})
    if any(i["kind"] == "out_of_scope" for i in found):
        classes.append("out_of_scope")
    return ",".join(classes) or "none"


def main() -> None:
    if settings.get("channels.email") != "sim":
        sys.exit(f"{settings.ROOT} is not a practice copy (channels.email is not 'sim'); refusing to run")
    study = next(iter(config.studies().values()))
    cases = CASES_2 if "--round2" in sys.argv else [(*c, {}) for c in CASES]
    right = 0
    rows = []
    for n, (name, subject, body, expected, extra) in enumerate(cases, 1):
        t0 = time.time()
        found, _ = run_case(study, subject, body)
        got = outcome(found)
        ok = got == expected or (expected not in ("none", "out_of_scope") and expected in got.split(","))
        pids = {i["participant_id"] for i in found if i["kind"] == "finding"}
        if extra.get("pid") and extra["pid"] not in pids:
            ok, got = False, f"{got} (not on {extra['pid']})"
        if extra.get("not_pid") and extra["not_pid"] in pids:
            ok, got = False, f"{got} (also on {extra['not_pid']})"
        right += ok
        tiers = ",".join(i["tier"] for i in found if i["kind"] == "finding") or "-"
        rows.append((ok, name, expected, got, tiers))
        print(f"{'RIGHT' if ok else 'WRONG'}  {n:2d}. {name:45s} expected {expected:16s} got {got:22s} "
              f"tier {tiers:10s} {time.time() - t0:5.1f}s", flush=True)
    print(f"\n{right}/{len(cases)} right ({100 * right / len(cases):.0f}%)")
    wrong = [r for r in rows if not r[0]]
    if wrong:
        print("\nWrong ones:")
        for _, name, exp, got, _ in wrong:
            print(f"  - {name}: expected {exp}, got {got}")


if __name__ == "__main__":
    main()

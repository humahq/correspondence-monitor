"""Runs every demo scenario through the local simulator and checks the outcome.

    python demo/rehearse.py            # fresh database (study model kept), all scenarios
    python demo/rehearse.py --keep     # add to the current database instead
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cormon import cli, db, service  # noqa: E402

SCEN = ROOT / "demo" / "scenarios"

EXPECT = {
    "01_eligibility_medium.eml": {"kind": "finding", "tier": "medium", "pattern_class": "eligibility", "participant_id": "MAP-0142"},
    "02_sequence_immediate.eml": {"kind": "finding", "tier": "immediate", "pattern_class": "procedure_sequence", "participant_id": "MAP-0157"},
    "03_sample_handling.eml": {"kind": "finding", "tier": "medium", "pattern_class": "sample_handling", "participant_id": "MAP-0120"},
    "04_historical_and_good_news.eml": {"kind": "good_news", "participant_id": "MAP-0110", "no_item_for": "MAP-0098"},
    "05_safety_out_of_scope.eml": {"kind": "out_of_scope", "participant_id": "MAP-0133"},
    "06_general_no_tag.eml": {"general": True},
    "07_teams_bp_immediate.json": {"kind": "finding", "tier": "immediate", "pattern_class": "eligibility", "participant_id": "MAP-0161"},
    "08b_thread_readded.eml": {"kind": "finding", "pattern_class": "eligibility", "participant_id": "MAP-0170", "nudge": True, "one_item": True},
}


def inject(name: str) -> int:
    cli.main(["inject", str(SCEN / name)])
    service.tick(log=lambda *_: None)
    return db.q1("SELECT MAX(id) m FROM messages")["m"]


def check(name: str, msg_id: int) -> list[str]:
    exp = EXPECT.get(name)
    if not exp:
        return []
    errors = []
    msg = db.q1("SELECT * FROM messages WHERE id=?", (msg_id,))
    if exp.get("general"):
        if msg["route"] != "general":
            errors.append("expected a general (untagged) message")
        if db.q1("SELECT COUNT(*) n FROM items WHERE first_message_id=?", (msg_id,))["n"]:
            errors.append("general message must not create items")
        return errors
    its = [dict(r) for r in db.q("SELECT * FROM items WHERE last_message_id=? OR first_message_id=?", (msg_id, msg_id))]
    match = [i for i in its if i["kind"] == exp["kind"] and i["participant_id"] == exp["participant_id"]
             and ("pattern_class" not in exp or i["pattern_class"] == exp["pattern_class"])]
    if not match:
        errors.append(f"no {exp['kind']} item for {exp['participant_id']} "
                      f"(got {[(i['kind'], i['pattern_class'], i['participant_id'], i['tier']) for i in its]})")
    elif "tier" in exp and match[0]["tier"] != exp["tier"]:
        errors.append(f"tier {match[0]['tier']}, expected {exp['tier']}")
    if exp.get("no_item_for") and any(i["participant_id"] == exp["no_item_for"] for i in its):
        errors.append(f"historical mention of {exp['no_item_for']} raised an item")
    if exp.get("one_item"):
        pid_items = [i for i in its if i["participant_id"] == exp["participant_id"] and i["kind"] == "finding"]
        if len(pid_items) != 1:
            errors.append(f"expected one item for {exp['participant_id']}, got {len(pid_items)}")
    if exp.get("nudge") and not db.q1("SELECT id FROM outbox WHERE purpose='copy_nudge' AND message_id=?", (msg_id,)):
        errors.append("expected a copy nudge after the thread was re-read")
    if exp["kind"] == "out_of_scope":
        sent = db.q("SELECT template_id FROM outbox WHERE item_id=?", (match[0]["id"],)) if match else []
        if sent:
            errors.append(f"safety item must send nothing, sent {[s['template_id'] for s in sent]}")
    return errors


def cra_disregard(item_id: int) -> list[str]:
    row = db.q1("SELECT * FROM outbox WHERE item_id=? AND purpose='email_cra'", (item_id,))
    if not row:
        return [f"item {item_id} has no CRA email to reply to"]
    reply = (
        f"From: John Ernest <johnernest978@gmail.com>\nTo: {row['from_addr']}\n"
        f"Subject: Re: {row['subject']}\nMessage-ID: <cra-reply-{int(time.time())}@cra.example>\n"
        f"In-Reply-To: {row['rfc_message_id']}\nReferences: {row['rfc_message_id']}\n"
        f"Content-Type: text/plain; charset=utf-8\n\nDisregard this one, the site already re-checked "
        f"the eGFR and the value in the email was a typo.\n\nErnest\n"
    )
    p = ROOT / "data" / "cra_reply.eml"
    p.write_text(reply)
    cli.main(["inject", str(p)])
    service.tick(log=lambda *_: None)
    it = db.q1("SELECT state, counts, counts_source FROM items WHERE id=?", (item_id,))
    if it["state"] != "suppressed" or it["counts_source"] != "cra":
        return [f"CRA reply did not suppress item {item_id} (state={it['state']}, source={it['counts_source']})"]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if not args.keep:
        cli.main(["--as", "rehearsal", "reset", "--yes", "--reason", "rehearsal"])
    results = []
    for name in ["01_eligibility_medium.eml", "02_sequence_immediate.eml", "03_sample_handling.eml",
                 "04_historical_and_good_news.eml", "05_safety_out_of_scope.eml", "06_general_no_tag.eml",
                 "07_teams_bp_immediate.json", "08a_thread_start.eml", "08b_thread_readded.eml"]:
        t = time.time()
        mid = inject(name)
        errs = check(name, mid)
        results.append((name, errs, time.time() - t))
        print(f"{'PASS' if not errs else 'FAIL'}  {name:38s} {time.time() - t:5.1f}s  {'; '.join(errs)}")
    first = db.q1("SELECT id FROM items WHERE participant_id='MAP-0142' AND kind='finding'")
    errs = cra_disregard(first["id"]) if first else ["no MAP-0142 item"]
    results.append(("CRA replies 'disregard'", errs, 0))
    print(f"{'PASS' if not errs else 'FAIL'}  {'CRA replies disregard':38s}        {'; '.join(errs)}")
    failed = [r for r in results if r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

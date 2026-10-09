"""Try a message without email: it goes through the practice copy and prints what the bot would send.

    python demo/try.py "MAP-0301 randomized" "We randomized MAP-0301 today. eGFR was 41."
    python demo/try.py --from cra --reply-to 3 "Re: item" "disregard, approved by the sponsor"

Uses the practice copy in practice/ (files, not Gmail), so nothing real is sent and the live data is untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CORMON_HOME", str(ROOT / "practice"))
os.environ.setdefault("CORMON_OPERATOR", "practice")
sys.path.insert(0, str(ROOT))

from cormon import config, db, service, settings  # noqa: E402

SENDERS = {"crc": "John Rohit <johnrohit94@gmail.com>", "cra": "John Ernest <johnernest978@gmail.com>"}
OTHER = {"crc": "John Ernest <johnernest978@gmail.com>", "cra": "John Rohit <johnrohit94@gmail.com>"}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("subject")
    p.add_argument("body")
    p.add_argument("--from", dest="sender", choices=SENDERS, default="crc", help="crc = site (default), cra = monitor")
    p.add_argument("--reply-to", type=int, help="item number the message answers (for disregard / cleared)")
    p.add_argument("--to", default=None, help="bot address to copy (default: the study address)")
    args = p.parse_args()

    if settings.get("channels.email") != "sim":
        sys.exit(f"{settings.ROOT} is not a practice copy (channels.email is not 'sim'); refusing to run")

    study = next(iter(config.studies().values()))
    msg = EmailMessage()
    msg["From"] = SENDERS[args.sender]
    msg["To"] = OTHER[args.sender]
    msg["Cc"] = args.to or study.alias
    msg["Subject"] = args.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="practice.example")
    if args.reply_to:
        row = db.q1("SELECT rfc_message_id FROM outbox WHERE item_id=? AND channel='email' AND rfc_message_id IS NOT NULL"
                    " ORDER BY id LIMIT 1", (args.reply_to,))
        if not row:
            sys.exit(f"item {args.reply_to} has no email to reply to")
        msg["In-Reply-To"] = row["rfc_message_id"]
        msg["References"] = row["rfc_message_id"]
    msg.set_content(args.body)

    inbox = settings.data_dir() / "sim" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    before_items = db.q1("SELECT COALESCE(MAX(id), 0) m FROM items")["m"]
    before_out = db.q1("SELECT COALESCE(MAX(id), 0) m FROM outbox")["m"]
    (inbox / f"try-{os.getpid()}.eml").write_bytes(bytes(msg))
    print("The bot is reading the message...\n")
    service.tick(log=lambda *_: None)

    mid = db.q1("SELECT MAX(id) m FROM messages")["m"]
    notes: dict[str, int] = {}
    for a in db.q("SELECT action FROM audit_log WHERE entity_type='message' AND entity_id=?"
                  " AND action NOT IN ('message_captured','event_extracted') ORDER BY id", (str(mid),)):
        notes[a["action"]] = notes.get(a["action"], 0) + 1
    for action, n in notes.items():
        print(f"* {action.replace('_', ' ')}" + (f" (x{n})" if n > 1 else ""))
    changed = db.q("SELECT id, tier, state, kind, participant_id, title FROM items WHERE id>? OR updated_at>="
                   "(SELECT received_at FROM messages WHERE id=?) ORDER BY id", (before_items, mid))
    if changed:
        print("ITEMS")
        for i in changed:
            print(f"  #{i['id']} [{i['tier']}, {i['state']}] {i['kind']} · {i['participant_id'] or '-'} · {i['title']}")
    else:
        print("ITEMS: none (nothing in this message broke a study rule)")
    sent = db.q("SELECT * FROM outbox WHERE id>? ORDER BY id", (before_out,))
    print(f"\nWHAT THE BOT WOULD SEND ({len(sent)})")
    for o in sent:
        to = ", ".join(db.unjs(o["to_addrs"]) or []) or o["destination"]
        print(f"\n--- {o['purpose']} → {to}")
        if o["subject"]:
            print(f"Subject: {o['subject']}")
        print(o["body"])


if __name__ == "__main__":
    main()

"""Local simulator: files in data/sim stand in for the mailbox and Teams.

Email: drop .eml files in data/sim/inbox; sent mail is written to data/sim/outbox.
Teams: drop .json files in data/sim/teams_inbox; posts are appended to data/sim/teams/<channel>.md.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate

from .. import settings
from ..capture import Inbound, parse_email


def _dir(*parts: str):
    d = settings.data_dir().joinpath("sim", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


_QUOTE_STYLE = ("margin:2px 0 8px 0;padding:6px 10px;border-left:3px solid #9aa4b2;background:#f4f6f8;"
                "font-family:Georgia,'Times New Roman',serif;color:#222")


def email_html(body: str) -> str:
    """The plain-text email as HTML: **bold** lines, "> " evidence quotes in a grey box, a rule for "──", links."""
    out = []
    for line in body.splitlines():
        t = html.escape(line)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', t)
        if line.startswith("> "):
            out.append(f'<div style="{_QUOTE_STYLE}">{t[5:]}</div>')
        elif line.startswith("From: "):
            out.append(f'<div style="font-size:12px;color:#666;margin-bottom:10px">{t}</div>')
        elif line.startswith("──"):
            out.append('<hr style="border:0;border-top:1px solid #ccc;margin:12px 0">')
        else:
            out.append(f"{t}<br>")
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45">'
            + "\n".join(out) + "</div>")


def build_mime(row: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = row["from_addr"]
    # Gmail rewrites From to the account address unless the alias is a verified send-as address;
    # Reply-To keeps replies on the study alias either way.
    msg["Reply-To"] = row["from_addr"]
    msg["To"] = ", ".join(row["to_addrs"])
    if row["cc_addrs"]:
        msg["Cc"] = ", ".join(row["cc_addrs"])
    msg["Subject"] = row["subject"] or ""
    msg["Date"] = formatdate(localtime=False)
    if row.get("rfc_message_id"):
        msg["Message-ID"] = row["rfc_message_id"]
    meta = row.get("meta") or {}
    if meta.get("in_reply_to"):
        msg["In-Reply-To"] = meta["in_reply_to"]
        msg["References"] = " ".join(meta.get("references") or [meta["in_reply_to"]])
    msg.set_content(row["body"].replace("**", ""))
    if "**" in row["body"] or "\n> " in row["body"]:
        msg.add_alternative(email_html(row["body"]), subtype="html")
    return msg


class SimEmail:
    def fetch(self) -> list[Inbound]:
        out = []
        done = _dir("inbox", "processed")
        for p in sorted(_dir("inbox").glob("*.eml")):
            raw = p.read_bytes()
            inb = parse_email(raw, external_id=f"sim:{p.name}:{len(raw)}")
            if not inb.delivered_to:
                inb.delivered_to = inb.to_addrs + inb.cc_addrs
            out.append(inb)
            shutil.move(str(p), done / p.name)
        return out

    def send(self, row: dict) -> dict:
        msg = build_mime(row)
        path = _dir("outbox") / f"{row['id']:05d}_{row['purpose']}.eml"
        path.write_bytes(bytes(msg))
        return {"external_ref": str(path.name), "rfc_message_id": row.get("rfc_message_id")}


class SimTeams:
    def fetch(self) -> list[Inbound]:
        out = []
        done = _dir("teams_inbox", "processed")
        for p in sorted(_dir("teams_inbox").glob("*.json")):
            d = json.loads(p.read_text())
            ext = d.get("id") or f"sim:{p.name}"
            out.append(Inbound(
                channel="teams", external_id=ext,
                thread_ref=d.get("reply_to") or ext,
                from_addr=(d.get("from_email") or "").lower() or None,
                from_name=d.get("from_name"), subject=d.get("subject"),
                body_text=d["text"],
                sent_at=d.get("sent_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
                study_id=d["study_id"], visibility="direct" if d.get("channel") == "direct" else "channel",
                reply_to_external=d.get("reply_to"), raw=p.read_bytes(),
            ))
            shutil.move(str(p), done / p.name)
        return out

    def send(self, row: dict) -> dict:
        target = (row.get("meta") or {}).get("target") or {}
        name = target.get("key", "direct")
        path = _dir("teams") / f"{name}.md"
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"\n---\n<!-- outbox {row['id']} · {stamp} -->\n{row['body']}\n")
        return {"external_ref": f"sim-teams:{name}:{row['id']}", "web_url": None}

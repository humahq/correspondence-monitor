"""Gmail over IMAP (read) and SMTP (send) with an app password, the same way the scheduler reads bot@huma.ai.

The inbox is read-only: nothing is marked, moved or deleted, so other programs sharing the mailbox are not
disturbed. Messages already captured are recognised by Message-ID. With `only_study_aliases`, only mail sent to
a known study alias (bot+<tag>@...) is taken, because bare bot@huma.ai mail belongs to other bots on the inbox.

Password, first found wins: the env var named in `password_env`, then Google Secret Manager (`secret`).
"""

from __future__ import annotations

import imaplib
import os
import re
import smtplib
import subprocess
from datetime import date, datetime, timedelta, timezone

from .. import db, settings
from ..capture import Inbound, general_addresses, known_rfc_ids, parse_email, resolve_email_route
from .sim import build_mime

HEADERS = "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES FROM TO CC DELIVERED-TO X-ORIGINAL-TO DATE)]"


def _cfg(key: str, default=None):
    return settings.get(f"mailbox.imap.{key}", default)


def password() -> str:
    env = _cfg("password_env", "CORMON_GMAIL_APP_PASSWORD")
    if os.environ.get(env):
        return os.environ[env].replace(" ", "").strip()
    secret = _cfg("secret") or {}
    if secret.get("name"):
        on_gce = _secret_from_vm(secret)
        if on_gce:
            return on_gce
        cmd = ["gcloud", "secrets", "versions", "access", "latest", f"--secret={secret['name']}"]
        if secret.get("project"):
            cmd.append(f"--project={secret['project']}")
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            raise RuntimeError("gcloud is not installed, and the app password env var is not set") from None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.replace(" ", "").strip()
        raise RuntimeError(f"could not read the app password from Secret Manager: {out.stderr.strip()[:300]}")
    raise RuntimeError(f"no Gmail app password: set {env} or mailbox.imap.secret")


def _secret_from_vm(secret: dict) -> str | None:
    """On a Google Cloud VM, read the secret with the VM's own service account (no gcloud, no copied password)."""
    import base64

    import requests
    meta = "http://metadata.google.internal/computeMetadata/v1"
    try:
        tok = requests.get(f"{meta}/instance/service-accounts/default/token",
                           headers={"Metadata-Flavor": "Google"}, timeout=2).json()["access_token"]
    except (requests.RequestException, ValueError, KeyError):
        return None  # not on Google Cloud
    project = secret.get("project") or requests.get(f"{meta}/project/project-id",
                                                    headers={"Metadata-Flavor": "Google"}, timeout=2).text
    r = requests.get(f"https://secretmanager.googleapis.com/v1/projects/{project}/secrets/{secret['name']}"
                     "/versions/latest:access", headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"could not read the app password from Secret Manager: {r.text[:300]}")
    return base64.b64decode(r.json()["payload"]["data"]).decode().replace(" ", "").strip()


def _uid(meta: bytes) -> str | None:
    found = re.search(rb"UID (\d+)", meta)
    return found.group(1).decode() if found else None


def _fetch(conn, uids: list[str], part: str) -> dict[str, bytes]:
    if not uids:
        return {}
    status, data = conn.uid("FETCH", ",".join(uids), f"(UID {part})")
    if status != "OK":
        raise RuntimeError(f"the mailbox refused to fetch emails ({status})")
    return {_uid(i[0]): i[1] for i in data if isinstance(i, tuple) and _uid(i[0])}


class ImapChannel:
    def __init__(self):
        self.user = (_cfg("user") or settings.get("mailbox.address")).lower()
        self.me = settings.get("mailbox.address").lower()
        self._password = password()

    def _connect(self):
        conn = imaplib.IMAP4_SSL(_cfg("host", "imap.gmail.com"), 993, timeout=30)
        conn.login(self.user, self._password)
        return conn

    def _own(self, sender: str) -> bool:
        local, domain = self.me.split("@")
        return sender in (self.me, self.user) or (sender.startswith(local + "+") and sender.endswith("@" + domain))

    @staticmethod
    def _ours(head: Inbound, sent_ids: set[str] = frozenset()) -> bool:
        """Sent to a study alias or one of our general addresses, or a reply to mail we sent
        (not other bare bot@ mail, which other bots read)."""
        general = general_addresses()
        if {head.in_reply_to, *head.references} & sent_ids:
            return True
        return bool(resolve_email_route(head)[0]) or any(
            a in general for a in head.to_addrs + head.cc_addrs + head.delivered_to)

    # -------------------------------------------------------------- inbound
    def fetch(self) -> list[Inbound]:
        start = db.get_state("imap_start")
        if not start:
            start = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.set_state("imap_start", start)
        start = datetime.fromisoformat(start)
        conn = self._connect()
        try:
            since = (date.today() - timedelta(days=int(_cfg("days", 3)))).strftime("%d-%b-%Y")
            known = known_rfc_ids()
            sent_ids = {r["rfc_message_id"] for r in db.q(
                "SELECT rfc_message_id FROM outbox WHERE rfc_message_id IS NOT NULL")}
            out: list[Inbound] = []
            for folder in _cfg("folders", ["INBOX"]):
                status, _ = conn.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue
                status, data = conn.uid("SEARCH", None, "SINCE", since)
                if status != "OK":
                    raise RuntimeError(f"the mailbox refused the search in {folder} ({status})")
                uids = [u.decode() for u in (data[0].split() if data and data[0] else [])][-500:]
                wanted = []
                for uid, raw in _fetch(conn, uids, HEADERS).items():
                    head = parse_email(raw)
                    if not head.rfc_message_id or head.rfc_message_id in known:
                        continue
                    if self._own(head.from_addr or "") or datetime.fromisoformat(head.sent_at) < start:
                        continue
                    if _cfg("only_study_aliases", True) and not self._ours(head, sent_ids):
                        continue
                    wanted.append(uid)
                    known.add(head.rfc_message_id)
                out += [parse_email(raw) for _, raw in sorted(_fetch(conn, wanted, "BODY.PEEK[]").items(),
                                                              key=lambda kv: int(kv[0]))]
            return sorted(out, key=lambda m: datetime.fromisoformat(m.sent_at))
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    # -------------------------------------------------------------- outbound
    def send(self, row: dict) -> dict:
        msg = build_mime(row)
        with smtplib.SMTP_SSL(_cfg("smtp_host", "smtp.gmail.com"), int(_cfg("smtp_port", 465)), timeout=30) as smtp:
            smtp.login(self.user, self._password)
            smtp.send_message(msg)
        return {"external_ref": row.get("rfc_message_id"), "rfc_message_id": row.get("rfc_message_id")}

    def check(self) -> dict:
        conn = self._connect()
        try:
            status, data = conn.select("INBOX", readonly=True)
            return {"user": self.user, "inbox_messages": int(data[0]) if status == "OK" else None}
        finally:
            conn.logout()

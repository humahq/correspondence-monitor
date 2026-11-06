"""Gmail API adapter for the bot mailbox (bot@huma.ai with plus-addressed study aliases).

Auth, either:
  oauth:            run `cormon auth gmail` once (opens a browser), token saved to secrets/
  service_account:  Workspace domain-wide delegation, impersonating mailbox.address
"""

from __future__ import annotations

import base64

from .. import settings
from ..capture import Inbound, parse_email
from .sim import build_mime

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]


def credentials(interactive: bool = False):
    mode = settings.get("mailbox.gmail.auth", "oauth")
    if mode == "service_account":
        from google.oauth2 import service_account

        sa = settings.resolve(settings.get("mailbox.gmail.service_account_file"))
        return service_account.Credentials.from_service_account_file(str(sa), scopes=SCOPES).with_subject(
            settings.get("mailbox.address"))

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token = settings.resolve(settings.get("mailbox.gmail.token_file"))
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
    if creds and creds.valid:
        return creds
    if not interactive:
        raise RuntimeError("Gmail is not authorised; run `cormon auth gmail`")
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_file = settings.resolve(settings.get("mailbox.gmail.client_secrets_file"))
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    return creds


class GmailChannel:
    def __init__(self):
        from googleapiclient.discovery import build

        self.svc = build("gmail", "v1", credentials=credentials(), cache_discovery=False)
        self.me = settings.get("mailbox.address").lower()
        self._label_id = None

    # -------------------------------------------------------------- inbound
    def _processed_label(self) -> str:
        if self._label_id:
            return self._label_id
        name = settings.get("mailbox.gmail.processed_label", "cormon-processed")
        labels = self.svc.users().labels().list(userId="me").execute().get("labels", [])
        found = next((lb for lb in labels if lb["name"] == name), None)
        if not found:
            found = self.svc.users().labels().create(
                userId="me", body={"name": name, "labelListVisibility": "labelHide",
                                   "messageListVisibility": "hide"}).execute()
        self._label_id = found["id"]
        return self._label_id

    def fetch(self) -> list[Inbound]:
        label = settings.get("mailbox.gmail.processed_label", "cormon-processed")
        query = f"{settings.get('mailbox.gmail.query', 'in:inbox')} -label:{label}"
        resp = self.svc.users().messages().list(userId="me", q=query, maxResults=50).execute()
        out = []
        for ref in reversed(resp.get("messages", [])):
            full = self.svc.users().messages().get(userId="me", id=ref["id"], format="raw").execute()
            raw = base64.urlsafe_b64decode(full["raw"].encode())
            inb = parse_email(raw, external_id=full["id"], thread_ref=full["threadId"])
            local, domain = self.me.split("@")
            sender = inb.from_addr or ""
            if sender == self.me or (sender.startswith(local + "+") and sender.endswith("@" + domain)):
                self.ack(inb)
                continue
            out.append(inb)
        return out

    def ack(self, inb: Inbound) -> None:
        self.svc.users().messages().modify(
            userId="me", id=inb.external_id, body={"addLabelIds": [self._processed_label()]}).execute()

    # -------------------------------------------------------------- outbound
    def send(self, row: dict) -> dict:
        msg = build_mime(row)
        body = {"raw": base64.urlsafe_b64encode(bytes(msg)).decode()}
        thread = (row.get("meta") or {}).get("gmail_thread")
        if thread and not thread.startswith(("sim:", "<")):
            body["threadId"] = thread
        sent = self.svc.users().messages().send(userId="me", body=body).execute()
        meta = self.svc.users().messages().get(
            userId="me", id=sent["id"], format="metadata", metadataHeaders=["Message-ID", "From"]).execute()
        headers = {h["name"].lower(): h["value"] for h in meta.get("payload", {}).get("headers", [])}
        return {"external_ref": sent["id"], "rfc_message_id": headers.get("message-id")}

    def find_sent(self, row: dict) -> dict | None:
        """This row's email in Sent, by the Message-ID we gave it. The Gmail API may replace that id,
        so not finding it means "can't tell" (None), never "not sent"."""
        rfc = (row.get("rfc_message_id") or "").strip("<>")
        if not rfc:
            return None
        hits = self.svc.users().messages().list(userId="me", q=f"in:sent rfc822msgid:{rfc}").execute()
        ref = (hits.get("messages") or [None])[0]
        return {"external_ref": ref["id"], "rfc_message_id": row["rfc_message_id"]} if ref else None

    def _send_as(self) -> list[dict]:
        resp = self.svc.users().settings().sendAs().list(userId="me").execute()
        return resp.get("sendAs", [])

    def send_as_addresses(self) -> list[str]:
        return [s["sendAsEmail"].lower() for s in self._send_as()]

    def ensure_send_as(self, address: str) -> str:
        """Create the study alias and keep its display name on the deployment's bot name. Gmail can take the
        From name from here, so email, the admin pages and the Chat app name agree."""
        name = settings.bot_name()
        found = next((s for s in self._send_as() if s["sendAsEmail"].lower() == address.lower()), None)
        if found is None:
            self.svc.users().settings().sendAs().create(
                userId="me", body={"sendAsEmail": address, "displayName": name, "treatAsAlias": True}).execute()
            return "created"
        if found.get("displayName") != name:
            self.svc.users().settings().sendAs().patch(
                userId="me", sendAsEmail=address, body={"displayName": name}).execute()
            return "display name updated"
        return "already configured"

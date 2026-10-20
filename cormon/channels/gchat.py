"""Google Chat adapter over the Chat API, signed in as the bot user (bot@huma.ai).

Takes the place of Teams when `channels.teams: gchat`: the study channel and the alert channel are Chat
spaces the bot is a member of, and direct messages to the bot and group chats it is added to are read as well. Run `cormon auth gchat`
once and sign in as the bot account.
"""

from __future__ import annotations

import html
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from .. import config, db, settings
from ..capture import Inbound
from . import study_for_person

CHAT = "https://chat.googleapis.com/v1"
UPLOAD = "https://chat.googleapis.com/upload/v1"
PEOPLE = "https://people.googleapis.com/v1"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]


def credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token = settings.resolve(settings.get("gchat.token_file", "secrets/gchat_token.json"))
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token.write_text(creds.to_json())
    if creds and creds.valid and not interactive:
        return creds
    if not interactive:
        raise RuntimeError("Google Chat is not authorised; run `cormon auth gchat`")
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets_file = settings.resolve(settings.get("gchat.client_secrets_file", "secrets/gchat_client.json"))
    if not secrets_file.exists():
        raise RuntimeError(f"{secrets_file} is missing: download the OAuth client (Desktop app) JSON there")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  login_hint=settings.get("gchat.account", settings.get("mailbox.address")))
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    token.chmod(0o600)
    return creds


def chat_text(body: str) -> str:
    """The Teams HTML templates as Google Chat text (*bold*, _italic_, `monospace`, <url|label> links)."""
    t = re.sub(r"(?is)<code>\s*(.*?)\s*</code>",
               lambda m: "`" + re.sub(r"\s+", " ", m.group(1)).replace("`", "'") + "`", body or "")
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>\s*", "\n\n", t)
    t = re.sub(r"(?is)<b>\s*(.*?)\s*</b>", r"*\1*", t)
    t = re.sub(r"(?is)<i>\s*(.*?)\s*</i>", r"_\1_", t)
    t = re.sub(r"(?is)<a\s+href=\"([^\"]+)\"\s*>(.*?)</a>", r"<\1|\2>", t)
    t = re.sub(r"(?i)</?(p|div|span)[^>]*>", "", t)
    t = html.unescape(t)
    t = "\n".join(line.strip() for line in t.splitlines())
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def message_id(row: dict) -> str:
    """Our own id for the post an outbox row makes, so a retry is recognised instead of posted twice.
    The install id changes when a backup is restored, as outbox numbers are then used again."""
    install = db.get_state("install_id")
    if not install:
        install = secrets.token_hex(4)
        db.set_state("install_id", install)
    return f"client-cormon-{install}-{row['id']}"


def _time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class GoogleChatChannel:
    def __init__(self, session=None):
        if session is None:
            from google.auth.transport.requests import AuthorizedSession

            session = AuthorizedSession(credentials())
        self.http = session
        self._me = None
        self._users: dict[str, str] = {}
        self._app = None

    def _get(self, url: str, **params) -> dict:
        r = self.http.get(url if url.startswith("http") else CHAT + url, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, body: dict, **params) -> dict:
        r = self.http.post(CHAT + url, json=body, params=params or None, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from Google Chat: {r.text[:300]}")
        return r.json()

    @property
    def me(self) -> str:
        """The bot's Chat user name (users/<id>); the sign-in id and the Chat id are the same number."""
        if self._me is None:
            info = self._get("https://openidconnect.googleapis.com/v1/userinfo")
            self._me = f"users/{info['sub']}"
            self._users[self._me] = (info.get("email") or "").lower()
        return self._me

    def _email_for(self, user: str) -> str | None:
        if user not in self._users:
            try:
                p = self._get(f"{PEOPLE}/people/{user.split('/', 1)[1]}", personFields="emailAddresses",
                              sources=["READ_SOURCE_TYPE_PROFILE", "READ_SOURCE_TYPE_DOMAIN_CONTACT"])
                emails = [e.get("value", "") for e in p.get("emailAddresses", [])]
                self._users[user] = (emails[0] if emails else "").lower()
            except Exception:
                self._users[user] = ""
        return self._users[user] or None

    # -------------------------------------------------------------- inbound
    def _since(self) -> datetime:
        v = db.get_state("gchat_since")
        if not v:
            now = datetime.now(timezone.utc)
            db.set_state("gchat_since", now.isoformat())
            return now
        return datetime.fromisoformat(v) - timedelta(minutes=10)

    def _messages(self, space: str, since: datetime) -> list[dict]:
        out, token = [], None
        stamp = since.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        for _ in range(10):
            params = {"filter": f'createTime > "{stamp}"', "orderBy": "createTime ASC", "pageSize": 100}
            if token:
                params["pageToken"] = token
            data = self._get(f"/{space}/messages", **params)
            out += data.get("messages", [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def _spaces(self, filter: str) -> list[str]:
        out, token = [], None
        for _ in range(10):
            data = self._get("/spaces", filter=filter, pageSize=100, **({"pageToken": token} if token else {}))
            out += [s["name"] for s in data.get("spaces", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def _to_inbound(self, m: dict, *, study_id: str | None, visibility: str) -> Inbound | None:
        sender = m.get("sender") or {}
        if sender.get("type") != "HUMAN" or sender.get("name") == self.me or m.get("deleteTime"):
            return None
        text = (m.get("text") or m.get("argumentText") or "").strip()
        if not text:
            return None
        thread = (m.get("thread") or {}).get("name") or m["name"]
        named = [(a.get("userMention") or {}).get("user") or {} for a in m.get("annotations") or []
                 if a.get("type") == "USER_MENTION"]
        bot = lambda u: u.get("name") == self.me or (u.get("email") or "").lower() == self._users.get(self.me)
        headers = {"_cormon_mentions": [u.get("email") or u.get("name") for u in named],
                   "_cormon_to_bot": any(bot(u) for u in named)} if named else {}
        return Inbound(
            headers=headers,
            channel="teams", external_id=m["name"], thread_ref=thread,
            from_addr=self._email_for(sender["name"]), from_name=sender.get("displayName") or None,
            subject=None, body_text=text, body_html=m.get("formattedText"), sent_at=m.get("createTime"),
            study_id=study_id, visibility=visibility,
            reply_to_external=thread if m.get("threadReply") else None,
            raw=db.js(m).encode(),
        )

    def fetch(self) -> list[Inbound]:
        since = self._since()
        out: list[Inbound] = []
        newest = since
        seen: set[str] = set()
        configured = {(study.teams.get(k) or {}).get("space") for study in config.studies().values()
                      for k in ("study_channel", "alert_channel")}
        for study in config.studies().values():
            space = (study.teams.get("study_channel") or {}).get("space")
            if not space or space in seen:
                continue
            seen.add(space)
            for m in self._messages(space, since):
                inb = self._to_inbound(m, study_id=study.study_id, visibility="channel")
                if inb:
                    out.append(inb)
                    newest = max(newest, _time(m["createTime"]))
        if settings.get("gchat.read_direct_chats", True):
            dms = self._get("/spaces", filter='spaceType = "DIRECT_MESSAGE"', pageSize=100)
            for sp in dms.get("spaces", []):
                for m in self._messages(sp["name"], since):
                    inb = self._to_inbound(m, study_id=None, visibility="direct")
                    if inb:
                        inb.study_id = study_for_person(inb.from_addr)
                        out.append(inb)
                        newest = max(newest, _time(m["createTime"]))
        if settings.get("gchat.read_group_chats", True):
            # any group chat or space someone added the bot to; the study and alert spaces are read above or not
            for name in self._spaces('spaceType = "GROUP_CHAT" OR spaceType = "SPACE"'):
                if name in configured:
                    continue
                for m in self._messages(name, since):
                    inb = self._to_inbound(m, study_id=None, visibility="group")
                    if inb:
                        inb.study_id = study_for_person(inb.from_addr, known_only=True)
                        out.append(inb)
                        newest = max(newest, _time(m["createTime"]))
        db.set_state("gchat_since", (newest + timedelta(minutes=10)).isoformat())
        return sorted(out, key=lambda i: i.sent_at)

    # -------------------------------------------------------------- outbound
    def send(self, row: dict) -> dict:
        target = (row.get("meta") or {}).get("target") or {}
        space = target.get("space")
        if not space:
            raise RuntimeError(f"Google Chat space for {target.get('key', 'channel')} is not set in study.yaml")
        body: dict = {"text": chat_text(row["body"])}
        files = [ref for f in (row.get("meta") or {}).get("files") or [] if (ref := self._upload(space, f))]
        if files:
            body["attachment"] = files
        params = {"messageId": message_id(row)} if row.get("id") else {}
        if target.get("thread"):
            body["thread"] = {"name": target["thread"]}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        try:
            res = self._post(f"/{space}/messages", body, **params)
        except RuntimeError as e:
            if str(e).startswith("409") and params.get("messageId") and (found := self.find_sent(row)):
                return found  # this row was already posted (a retry after a stop mid-send)
            if str(e).startswith("400") and "messageid" in str(e).lower() and params.get("messageId"):
                db.audit("chat_message_id_refused", entity_type="outbox", entity_id=row["id"], error=str(e)[:300])
                params.pop("messageId")
                res = self._post(f"/{space}/messages", body, **params)
                return {"external_ref": res.get("name"), "web_url": None}
            if not target.get("thread") or not str(e).startswith("403"):
                raise
            # a 1-on-1 chat can refuse a threaded reply; the plain message reaches the same person
            res = self._post(f"/{space}/messages", {"text": body["text"]},
                             **({"messageId": params["messageId"]} if params.get("messageId") else {}))
        return {"external_ref": res.get("name"), "web_url": None}

    def find_sent(self, row: dict) -> dict | bool:
        """The post this row made, found by its own message id; False when there is none."""
        space = ((row.get("meta") or {}).get("target") or {}).get("space")
        r = self.http.get(f"{CHAT}/{space}/messages/{message_id(row)}", timeout=30)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        got = r.json()
        if " ".join((got.get("text") or "").split())[:200] != " ".join(chat_text(row["body"]).split())[:200]:
            raise RuntimeError(f"message id {message_id(row)} holds a different message")
        return {"external_ref": got.get("name"), "web_url": None}

    def _upload(self, space: str, f: dict) -> dict | None:
        """A file posted with the message; None when the upload fails (the message still goes, its link stands)."""
        boundary = "cormon-file-boundary"
        data = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps({"filename": f["name"]})
                + f"\r\n--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n" + f["text"]
                + f"\r\n--{boundary}--\r\n").encode()
        try:
            r = self.http.post(f"{UPLOAD}/{space}/attachments:upload", data=data, params={"uploadType": "multipart"},
                               headers={"Content-Type": f"multipart/related; boundary={boundary}"}, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")
            return r.json()
        except Exception as e:
            db.audit("chat_file_upload_failed", entity_type="space", entity_id=space, file=f["name"],
                     error=str(e)[:300])
            return None

    def _members_http(self):
        """The Chat app's own sign-in (chat.bot scope) when gchat.app_key_file names its key, else the bot account's."""
        key = settings.get("gchat.app_key_file")
        if not key:
            return self.http
        if self._app is None:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                str(settings.resolve(key)), scopes=["https://www.googleapis.com/auth/chat.bot"])
            self._app = AuthorizedSession(creds)
        return self._app

    def members(self, space: str) -> list[str]:
        """The email of each person in a space (spaces.members.list). Raises when the list can't be read."""
        http, out, token = self._members_http(), [], None
        for _ in range(10):
            r = http.get(f"{CHAT}/{space}/members", params={"pageSize": 100, **({"pageToken": token} if token else {})},
                         timeout=30)
            r.raise_for_status()
            data = r.json()
            for m in data.get("memberships", []):
                user = m.get("member") or {}
                if user.get("type") == "HUMAN" and user.get("name") and (email := self._email_for(user["name"])):
                    out.append(email)
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def list_spaces(self) -> list[dict]:
        out, token = [], None
        for _ in range(10):
            data = self._get("/spaces", pageSize=100, **({"pageToken": token} if token else {}))
            out += [{"space": s["name"], "name": s.get("displayName") or "(direct message)",
                     "type": s.get("spaceType", "")} for s in data.get("spaces", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return out

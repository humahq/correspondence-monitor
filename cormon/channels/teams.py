"""Microsoft Teams adapter over Microsoft Graph, signed in as the bot user.

The bot is a member user of our own tenant (e.g. the study bot account); the
customer's people join our team as guests. Delegated permissions let it read
and post in channels it belongs to and in its 1:1 chats. Run `cormon auth teams`
once to sign in with a device code.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import requests

from .. import config, db, settings
from ..capture import Inbound
from . import study_for_person

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["ChannelMessage.Send", "ChannelMessage.Read.All", "Chat.ReadWrite",
          "Team.ReadBasic.All", "Channel.ReadBasic.All", "User.ReadBasic.All"]


def _app():
    import msal

    cache = msal.SerializableTokenCache()
    path = settings.resolve(settings.get("teams.token_cache", "secrets/teams_token.json"))
    if path.exists():
        cache.deserialize(path.read_text())
    tenant = settings.get("teams.tenant_id") or "organizations"
    app = msal.PublicClientApplication(settings.get("teams.client_id"),
                                       authority=f"https://login.microsoftonline.com/{tenant}", token_cache=cache)
    return app, cache, path


def _save(cache, path):
    if cache.has_state_changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize())


def token(interactive: bool = False) -> str:
    if not settings.get("teams.client_id"):
        raise RuntimeError("teams.client_id is not set in config/settings.yaml")
    app, cache, path = _app()
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result and interactive:
        flow = app.initiate_device_flow(scopes=SCOPES)
        print(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    _save(cache, path)
    if not result or "access_token" not in result:
        raise RuntimeError(f"Teams is not authorised; run `cormon auth teams` ({(result or {}).get('error_description', '')})")
    return result["access_token"]


def _text(content: str) -> str:
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content or "")
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def _touched(m: dict) -> datetime:
    """When the message was last written, edited or deleted."""
    stamps = [m.get(k) for k in ("createdDateTime", "lastEditedDateTime", "deletedDateTime")]
    return max(datetime.fromisoformat(s.replace("Z", "+00:00")) for s in stamps if s)


class TeamsChannel:
    def __init__(self):
        self._me = None
        self._users: dict[str, str] = {}

    def _get(self, url: str, **params) -> dict:
        r = requests.get(url if url.startswith("http") else GRAPH + url,
                         headers={"Authorization": f"Bearer {token()}"}, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, body: dict) -> dict:
        r = requests.post(GRAPH + url, headers={"Authorization": f"Bearer {token()}"}, json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    @property
    def me(self) -> str:
        if self._me is None:
            self._me = self._get("/me")["id"]
        return self._me

    def _email_for(self, user_id: str) -> str | None:
        if user_id not in self._users:
            try:
                u = self._get(f"/users/{user_id}", **{"$select": "mail,userPrincipalName"})
                self._users[user_id] = (u.get("mail") or u.get("userPrincipalName") or "").lower()
            except requests.HTTPError:
                self._users[user_id] = ""
        return self._users[user_id] or None

    # -------------------------------------------------------------- inbound
    def _since(self) -> datetime:
        v = db.get_state("teams_since")
        if not v:
            now = datetime.now(timezone.utc)
            db.set_state("teams_since", now.isoformat())
            return now
        return datetime.fromisoformat(v) - timedelta(minutes=10)

    def _to_inbound(self, m: dict, *, study_id: str | None, visibility: str, thread_ref: str,
                    reply_to: str | None, ext_prefix: str) -> Inbound | None:
        if m.get("messageType") != "message":
            return None
        if m.get("deletedDateTime"):   # capture keeps it as a deleted version if the bot had read it, else drops it
            return Inbound(channel="teams", external_id=f"{ext_prefix}:{m['id']}", thread_ref=thread_ref,
                           from_addr=None, from_name=None, subject=None, body_text="",
                           sent_at=m.get("createdDateTime") or m["deletedDateTime"], study_id=study_id,
                           visibility=visibility, deleted_at=m["deletedDateTime"], raw=db.js(m).encode())
        user = (m.get("from") or {}).get("user") or {}
        if not user or user.get("id") == self.me:
            return None
        edited = m.get("lastEditedDateTime")
        return Inbound(
            channel="teams", external_id=f"{ext_prefix}:{m['id']}", thread_ref=thread_ref,
            from_addr=self._email_for(user["id"]), from_name=user.get("displayName"),
            subject=m.get("subject"), body_text=_text((m.get("body") or {}).get("content", "")),
            body_html=(m.get("body") or {}).get("content"), sent_at=m.get("createdDateTime"),
            study_id=study_id, visibility=visibility, reply_to_external=reply_to,
            raw=db.js(m).encode(), edited_at=edited,
        )

    def fetch(self) -> list[Inbound]:
        since = self._since()
        out: list[Inbound] = []
        newest = since
        for study in config.studies().values():
            ch = study.teams.get("study_channel") or {}
            if not ch.get("team_id") or not ch.get("channel_id"):
                continue
            base = f"/teams/{ch['team_id']}/channels/{ch['channel_id']}/messages"
            data = self._get(base, **{"$top": 25, "$expand": "replies"})
            for m in data.get("value", []):
                created = _touched(m)
                prefix = f"{ch['team_id']}:{ch['channel_id']}"
                if created > since:
                    inb = self._to_inbound(m, study_id=study.study_id, visibility="channel",
                                           thread_ref=f"{ch['channel_id']}:{m['id']}", reply_to=None,
                                           ext_prefix=prefix)
                    if inb:
                        out.append(inb)
                        newest = max(newest, created)
                for rep in m.get("replies", []):
                    rc = _touched(rep)
                    if rc <= since:
                        continue
                    inb = self._to_inbound(rep, study_id=study.study_id, visibility="channel",
                                           thread_ref=f"{ch['channel_id']}:{m['id']}",
                                           reply_to=f"{prefix}:{m['id']}", ext_prefix=prefix)
                    if inb:
                        out.append(inb)
                        newest = max(newest, rc)
        if settings.get("teams.read_direct_chats", True):
            chats = self._get("/me/chats", **{"$filter": "chatType eq 'oneOnOne'", "$top": 20})
            for chat in chats.get("value", []):
                msgs = self._get(f"/chats/{chat['id']}/messages", **{"$top": 20})
                for m in msgs.get("value", []):
                    created = _touched(m)
                    if created <= since:
                        continue
                    inb = self._to_inbound(m, study_id=None, visibility="direct",
                                           thread_ref=f"chat:{chat['id']}", reply_to=None,
                                           ext_prefix=f"chat:{chat['id']}")
                    if inb:
                        inb.study_id = study_for_person(inb.from_addr)
                        out.append(inb)
                        newest = max(newest, created)
        db.set_state("teams_since", (newest + timedelta(minutes=10)).isoformat())
        return sorted(out, key=lambda i: i.sent_at)

    # -------------------------------------------------------------- outbound
    def send(self, row: dict) -> dict:
        meta = row.get("meta") or {}
        target = meta.get("target") or {}
        body = {"body": {"contentType": "html", "content": row["body"]}}
        if target.get("chat_id"):
            res = self._post(f"/chats/{target['chat_id']}/messages", body)
        else:
            if not target.get("team_id") or not target.get("channel_id"):
                raise RuntimeError(f"Teams {target.get('key', 'channel')} is not configured for this study")
            base = f"/teams/{target['team_id']}/channels/{target['channel_id']}/messages"
            if meta.get("reply_to_message"):
                res = self._post(f"{base}/{meta['reply_to_message']}/replies", body)
            else:
                res = self._post(base, body)
        return {"external_ref": res.get("id"), "web_url": res.get("webUrl")}

    def list_channels(self) -> list[dict]:
        out = []
        for t in self._get("/me/joinedTeams").get("value", []):
            for c in self._get(f"/teams/{t['id']}/channels").get("value", []):
                out.append({"team": t["displayName"], "team_id": t["id"],
                            "channel": c["displayName"], "channel_id": c["id"]})
        return out

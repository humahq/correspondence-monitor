"""Mattermost adapter over its REST API, signed in with the bot account's token (the local test and demo chat).

Takes the place of Google Chat when `channels.teams: mattermost`. Rooms, threads and posts are named the way Google
Chat names them (spaces/<channel id>, spaces/<channel id>/threads/<root post id>, spaces/<channel id>/messages/
<root post id>.<post id>), so the rest of the bot reads them the same. The server is set by MM_URL and the token by
MM_BOT_TOKEN, from the environment or from the file mattermost.env_file names (the chat repo's .env).
"""

from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .. import config, db, settings
from ..capture import InAttachment, Inbound, attachment_text
from . import study_for_person
from .gchat import MAX_ATTACHMENT, message_id

API = "/api/v4"
_MENTION = re.compile(r"(?<![\w@])@([a-z0-9][a-z0-9._-]*)", re.I)


def _env() -> dict[str, str]:
    out = {}
    f = settings.get("mattermost.env_file")
    if f and Path(f).expanduser().exists():
        for line in Path(f).expanduser().read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("MM_URL", "MM_BOT_TOKEN"):
                out[k.strip()] = v.strip()
    return {k: os.environ.get(k) or out.get(k, "") for k in ("MM_URL", "MM_BOT_TOKEN")}


def chat_text(body: str) -> str:
    """The HTML templates as Mattermost markdown (**bold**, _italic_, `monospace`, [label](url) links)."""
    t = re.sub(r"(?is)<code>\s*(.*?)\s*</code>",
               lambda m: "`" + re.sub(r"\s+", " ", m.group(1)).replace("`", "'") + "`", body or "")
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</p>\s*", "\n\n", t)
    t = re.sub(r"(?is)<li[^>]*>\s*", "- ", t)
    t = re.sub(r"(?i)</li>\s*", "\n", t)
    t = re.sub(r"(?is)<b>\s*(.*?)\s*</b>", r"**\1**", t)
    t = re.sub(r"(?is)<i>\s*(.*?)\s*</i>", r"_\1_", t)
    t = re.sub(r"(?is)<a\s+href=\"([^\"]+)\"\s*>(.*?)</a>", r"[\2](\1)", t)
    t = re.sub(r"<(https?://[^|>\s]+)\|([^>]+)>", r"[\2](\1)", t)   # Google Chat links some answers are written with
    t = re.sub(r"<(https?://[^>\s]+)>", r"\1", t)
    t = re.sub(r"(?i)</?(p|div|span|ul|ol)[^>]*>", "", t)
    t = html.unescape(t)
    t = "\n".join(line.strip() for line in t.splitlines())
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def space(channel_id: str) -> str:
    return f"spaces/{channel_id}"


def _channel(space_name: str) -> str:
    return space_name.split("/", 1)[1] if space_name.startswith("spaces/") else space_name


class MattermostChannel:
    def __init__(self, session=None, url: str | None = None, token: str | None = None):
        env = _env()
        self.url = (url or env["MM_URL"] or "http://localhost:8065").rstrip("/")
        token = token or env["MM_BOT_TOKEN"]
        if session is None:
            if not token:
                raise RuntimeError("Mattermost is not set up: MM_BOT_TOKEN is missing (run setup.sh in cormon-chat)")
            session = requests.Session()
            session.headers["Authorization"] = f"Bearer {token}"
        self.http = session
        self._me: dict | None = None
        self._users: dict[str, dict] = {}
        self._kinds: dict[str, str] = {}
        self._answered: dict[str, float] = {}
        self._typing_every: float | None = None

    def _get(self, path: str, **params):
        r = self.http.get(self.url + API + path, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict | list):
        r = self.http.post(self.url + API + path, json=body, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from Mattermost: {r.text[:300]}")
        return r.json()

    @property
    def me(self) -> str:
        if self._me is None:
            self._me = self._get("/users/me")
        return f"users/{self._me['id']}"

    def _user(self, uid: str) -> dict:
        if uid not in self._users:
            try:
                self._users[uid] = self._get(f"/users/{uid}")
            except requests.RequestException:
                return {}
        return self._users[uid]

    def _email_for(self, uid: str) -> str | None:
        """The person's email, as the study knows it: mattermost.people maps a demo address to a study person's."""
        email = (self._user(uid).get("email") or "").lower()
        people = {k.lower(): v.lower() for k, v in (settings.get("mattermost.people") or {}).items()}
        return people.get(email, email) or None

    def _channels(self) -> list[dict]:
        self.me
        out, seen = [], set()
        only = settings.get("mattermost.team")   # one demo team at a time: another customer's rooms are not read
        for team in self._get("/users/me/teams"):
            if only and team.get("name") != only:
                continue
            for ch in self._get(f"/users/me/teams/{team['id']}/channels"):
                if ch["id"] not in seen:
                    seen.add(ch["id"])
                    self._kinds[ch["id"]] = ch["type"]
                    out.append(ch)
        return out

    # -------------------------------------------------------------- inbound
    def _since(self) -> int:
        v = db.get_state("mattermost_since")
        if not v:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            db.set_state("mattermost_since", str(now))
            return now
        return int(v) - 10 * 60 * 1000

    def _posts(self, cid: str, since: int) -> list[dict]:
        data = self._get(f"/channels/{cid}/posts", since=since)
        posts = data.get("posts") or {}
        return sorted(posts.values(), key=lambda p: p["create_at"])

    def _to_inbound(self, p: dict, *, study_id: str | None, visibility: str) -> Inbound | None:
        cid, root = p["channel_id"], p.get("root_id") or p["id"]
        name = f"{space(cid)}/messages/{root}.{p['id']}"
        thread = f"{space(cid)}/threads/{root}"
        if p.get("delete_at"):
            return Inbound(channel="teams", external_id=name, thread_ref="", from_addr=None, from_name=None,
                           subject=None, body_text="", sent_at=_iso(p["create_at"]), study_id=study_id,
                           visibility=visibility, deleted_at=_iso(p["delete_at"]), raw=db.js(p).encode())
        if p.get("user_id") == self._me["id"] or (p.get("type") or "").startswith("system_") \
                or (p.get("props") or {}).get("from_bot") == "true":
            return None
        text = (p.get("message") or "").strip()
        files = self._attachments(p, name)
        if not text and not files:
            return None
        user = self._user(p["user_id"])
        named = [m.lower() for m in _MENTION.findall(text)]
        headers = {"_cormon_mentions": named,
                   "_cormon_to_bot": (self._me.get("username") or "").lower() in named} if named else {}
        who = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username")
        return Inbound(
            headers=headers, channel="teams", external_id=name, thread_ref=thread,
            from_addr=self._email_for(p["user_id"]), from_name=who or None, subject=None, body_text=text,
            body_html=None, sent_at=_iso(p["create_at"]), study_id=study_id, visibility=visibility,
            reply_to_external=thread if p.get("root_id") else None, raw=db.js(p).encode(),
            edited_at=_iso(p["edit_at"]) if p.get("edit_at") else None, attachments=files)

    def _attachments(self, p: dict, name: str) -> list[InAttachment]:
        """The files posted with a message, downloaded once (the overlapping polls see a post again)."""
        infos = (p.get("metadata") or {}).get("files") or []
        if not infos or db.q1("SELECT 1 FROM messages WHERE channel='teams' AND external_id=?", (name,)):
            return []
        out = []
        for f in infos:
            fname, mime = f.get("name") or "attachment", f.get("mime_type") or "application/octet-stream"
            note = None
            if (f.get("size") or 0) > MAX_ATTACHMENT:
                data, note = b"", f"[not downloaded: larger than {MAX_ATTACHMENT // 2**20} MB]"
            else:
                try:
                    r = self.http.get(f"{self.url}{API}/files/{f['id']}", timeout=60)
                    r.raise_for_status()
                    data = r.content
                except Exception as e:
                    data, note = b"", f"[could not download: {str(e)[:200]}]"
            if note:
                out.append(InAttachment(filename=fname, mime="application/json", component="attachment-reference",
                                        data=db.js(f).encode(), source_date=_iso(p["create_at"]), text=note))
            else:
                out.append(InAttachment(filename=fname, mime=mime, data=data, component="attachment",
                                        source_date=_iso(p["create_at"]), text=attachment_text(fname, mime, data)))
        return out

    def fetch(self) -> list[Inbound]:
        since = self._since()
        newest = since
        rooms = {}
        for study in config.studies().values():
            for key in ("study_channel", "alert_channel"):
                s = (study.teams.get(key) or {}).get("space")
                if s:
                    rooms.setdefault(_channel(s), (key, study.study_id))
        out: list[Inbound] = []
        for ch in self._channels():
            key, study_id = rooms.get(ch["id"], (None, None))
            if key == "alert_channel":
                continue
            if key == "study_channel":
                visibility = "channel"
            elif ch["type"] == "D":
                if not settings.get("mattermost.read_direct_chats", True):
                    continue
                visibility = "direct"
            elif settings.get("mattermost.read_group_chats", True):
                visibility = "group"
            else:
                continue
            for p in self._posts(ch["id"], since):
                inb = self._to_inbound(p, study_id=study_id, visibility=visibility)
                if not inb:
                    continue
                if visibility == "direct":
                    inb.study_id = study_for_person(inb.from_addr)
                elif visibility == "group":
                    inb.study_id = study_for_person(inb.from_addr, known_only=True)
                out.append(inb)
                newest = max(newest, p.get("update_at") or p["create_at"])
        db.set_state("mattermost_since", str(newest + 10 * 60 * 1000))
        return sorted(out, key=lambda i: i.sent_at)

    # -------------------------------------------------------------- outbound
    def send(self, row: dict) -> dict:
        target = (row.get("meta") or {}).get("target") or {}
        if not target.get("space"):
            raise RuntimeError(f"Mattermost room for {target.get('key', 'channel')} is not set (mattermost.rooms)")
        cid = _channel(target["space"])
        if row.get("purpose") != "chat_received":   # the quick "give me a moment" is not the answer
            self._answered[cid] = time.time()
        if row.get("id") and (found := self.find_sent(row)):
            return found  # this row was already posted (a retry after a stop mid-send)
        body: dict = {"channel_id": cid, "message": chat_text(row["body"])}
        if row.get("id"):
            body["props"] = {"cormon_id": message_id(row)}
        if target.get("thread"):
            body["root_id"] = target["thread"].rsplit("/", 1)[1]
        files = [i for f in (row.get("meta") or {}).get("files") or [] if (i := self._upload(cid, f))]
        if files:
            body["file_ids"] = files
        p = self._post("/posts", body)
        return {"external_ref": f"{space(cid)}/messages/{p.get('root_id') or p['id']}.{p['id']}", "web_url": None}

    def find_sent(self, row: dict) -> dict | bool:
        """The post this row made, found by its own id in the room's last two days; False when there is none."""
        cid = _channel(((row.get("meta") or {}).get("target") or {}).get("space") or "")
        if not cid:
            return False
        since = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp() * 1000)
        mine = message_id(row)
        for p in self._posts(cid, since):
            if (p.get("props") or {}).get("cormon_id") == mine and not p.get("delete_at"):
                return {"external_ref": f"{space(cid)}/messages/{p.get('root_id') or p['id']}.{p['id']}",
                        "web_url": None}
        return False

    def _upload(self, cid: str, f: dict) -> str | None:
        """A file posted with the message; None when the upload fails (the message still goes, its link stands)."""
        try:
            r = self.http.post(f"{self.url}{API}/files", params={"channel_id": cid},
                               files={"files": (f["name"], f["text"].encode(), "text/plain")}, timeout=60)
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} {r.text[:200]}")
            return r.json()["file_infos"][0]["id"]
        except Exception as e:
            db.audit("chat_file_upload_failed", entity_type="space", entity_id=space(cid), file=f["name"],
                     error=str(e)[:300])
            return None

    def members(self, space_name: str) -> list[str]:
        """The email of each person (not bot) in a room. Raises when the list can't be read."""
        out, page = [], 0
        while True:
            batch = self._get(f"/channels/{_channel(space_name)}/members", page=page, per_page=200)
            for m in batch:
                u = self._user(m["user_id"])
                if u and not u.get("is_bot") and (email := self._email_for(m["user_id"])):
                    out.append(email)
            if len(batch) < 200:
                return out
            page += 1

    def list_spaces(self) -> list[dict]:
        kinds = {"O": "SPACE", "P": "SPACE", "G": "GROUP_CHAT", "D": "DIRECT_MESSAGE"}
        return [{"space": space(c["id"]), "name": c.get("display_name") or "(direct message)",
                 "type": kinds.get(c["type"], c["type"])} for c in self._channels()]

    @property
    def typing_seconds(self) -> float:
        """How often to say "typing" again: a little under how long the chat shows it (the server's
        TimeBetweenUserTypingUpdatesMilliseconds, 5 s unless set lower)."""
        if self._typing_every is None:
            try:
                ms = int(self._get("/config/client", format="old").get("TimeBetweenUserTypingUpdatesMilliseconds"))
            except Exception:
                ms = 5000
            self._typing_every = max(0.5, ms / 1000 * 0.8)
        return self._typing_every

    def typing(self, space_name: str, thread_root: str | None = None, since: float | None = None) -> None:
        """"cormon is typing..." in the room (in the thread, given its root post) for a few seconds. Not once an
        answer has gone there after `since`: the chat does not clear it when the answer is in a thread, so it
        would linger."""
        cid = _channel(space_name)
        if since is not None and self._answered.get(cid, 0) >= since:
            return
        self.me
        body = {"channel_id": cid}
        if thread_root:
            body["parent_id"] = thread_root
        self._post(f"/users/{self._me['id']}/typing", body)

    def direct_space(self, email: str) -> str | None:
        """spaces/<id> of the bot's 1-on-1 chat with the study person at this email (made if new); None when no
        Mattermost user stands for them."""
        email = email.lower()
        people = settings.get("mattermost.people") or {}
        for addr in [k for k, v in people.items() if v.lower() == email] + [email]:
            try:
                uid = self._get(f"/users/email/{addr}")["id"]
            except requests.RequestException:
                continue
            self.me
            return space(self._post("/channels/direct", [self._me["id"], uid])["id"])
        return None

    def find_room(self, team: str, name: str) -> str:
        """spaces/<id> of a room given as its team and channel name (as in setup.sh)."""
        return space(self._get(f"/teams/name/{team}/channels/name/{name}")["id"])

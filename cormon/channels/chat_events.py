"""Google Chat push: a note through Pub/Sub the moment a message is posted, so the bot reads Chat at once.

A Workspace Events subscription, made with the bot account's sign-in, watches every space the bot is in
(direct messages, group chats, the study spaces) and sends a note to a Pub/Sub topic for each new message. The
note carries only the message's name, no text. A background thread pulls the notes and wakes the service, which
reads just the messages named (capture keeps one copy of each message). The regular poll stays, less often: a
note that is lost or late costs at most the chat poll interval, and a stopped listener only makes the bot slower.

Setup (once): a topic in the Cloud project of the bot's OAuth client, with chat-api-push@system.gserviceaccount.com
as Pub/Sub Publisher; a pull subscription on it that the machine's account may read (Pub/Sub Subscriber); both
names in settings (gchat.events.topic, gchat.events.subscription). `cormon chat-events` makes the Workspace
subscription and checks the pull.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import db, settings

EVENTS = "https://workspaceevents.googleapis.com/v1"
PUBSUB = "https://pubsub.googleapis.com/v1"
TARGET = "//chat.googleapis.com/spaces/-"   # every space the bot account is a member of
# created: read the new message; updated and deleted: capture keeps the change as a new version (CAP-03)
TYPES = ["google.workspace.chat.message.v1.created", "google.workspace.chat.message.v1.updated",
         "google.workspace.chat.message.v1.deleted"]
RENEW_DAYS = 3        # a subscription without message text lasts up to 7 days; renewed when fewer remain
CHECK_HOURS = 6       # how often the service looks at the subscription
PULL_SECONDS = 60     # a pull waits this long for a note before asking again


def topic() -> str:
    return settings.get("gchat.events.topic") or ""


def subscription() -> str:
    return settings.get("gchat.events.subscription") or ""


def enabled() -> bool:
    return settings.get("channels.teams") == "gchat" and bool(topic() and subscription())


def _when(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _wait_op(http, op: dict) -> dict:
    """The subscription a long-running create or renew returns."""
    for _ in range(20):
        if op.get("done"):
            if op.get("error"):
                raise RuntimeError(f"Workspace Events: {op['error']}")
            return op.get("response") or {}
        time.sleep(1)
        r = http.get(f"{EVENTS}/{op['name']}", timeout=30)
        r.raise_for_status()
        op = r.json()
    raise RuntimeError("Workspace Events: the request did not finish")


def _find(http) -> dict | None:
    flt = f'event_types:"{TYPES[0]}" AND target_resource="{TARGET}"'
    r = http.get(f"{EVENTS}/subscriptions", params={"filter": flt}, timeout=30)
    r.raise_for_status()
    subs = [s for s in r.json().get("subscriptions", [])
            if (s.get("notificationEndpoint") or {}).get("pubsubTopic") == topic()]
    return subs[0] if subs else None


def ensure(http, log=print, force: bool = False) -> dict:
    """Makes the Workspace subscription, renews it when it nears its end, and makes it again when Google ended
    or suspended it (for example after the bot's sign-in changed). Returns {name, state, expires, action}."""
    if not force:
        last = _when(db.get_state("gchat_events_checked"))
        if last and datetime.now(timezone.utc) - last < timedelta(hours=CHECK_HOURS):
            return {"action": "checked lately"}
    sub, action = None, "ok"
    name = db.get_state("gchat_events_sub")
    if name:
        r = http.get(f"{EVENTS}/{name}", timeout=30)
        if r.status_code != 404:
            r.raise_for_status()
            sub = r.json()
    sub = sub or _find(http)
    if sub and sub.get("state") not in ("ACTIVE", None):
        http.delete(f"{EVENTS}/{sub['name']}", timeout=30)
        db.audit("chat_events_ended", entity_type="service", entity_id=sub["name"], state=sub.get("state"),
                 reason=sub.get("suspensionReason"))
        sub = None
    if sub and sub.get("eventTypes") and not set(TYPES) <= set(sub["eventTypes"]):
        http.delete(f"{EVENTS}/{sub['name']}", timeout=30)   # made before edits were watched: made again below
        db.audit("chat_events_types_changed", entity_type="service", entity_id=sub["name"],
                 had=sub["eventTypes"], want=TYPES)
        sub = None
    if sub is None:
        r = http.post(f"{EVENTS}/subscriptions", json={
            "targetResource": TARGET, "eventTypes": TYPES, "notificationEndpoint": {"pubsubTopic": topic()},
            "payloadOptions": {"includeResource": False}}, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from Workspace Events: {r.text[:300]}")
        sub, action = _wait_op(http, r.json()), "made"
    else:
        ends = _when(sub.get("expireTime"))
        if ends is None or ends - datetime.now(timezone.utc) < timedelta(days=RENEW_DAYS):
            r = http.patch(f"{EVENTS}/{sub['name']}", params={"updateMask": "ttl"}, json={"ttl": "0s"}, timeout=30)
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} from Workspace Events: {r.text[:300]}")
            sub, action = _wait_op(http, r.json()) or sub, "renewed"
    db.set_state("gchat_events_sub", sub["name"])
    db.set_state("gchat_events_checked", db.now())
    if action != "ok":
        db.audit(f"chat_events_{action}", entity_type="service", entity_id=sub["name"], expires=sub.get("expireTime"))
        log(f"chat push subscription {action}, until {sub.get('expireTime')}")
    return {"name": sub["name"], "state": sub.get("state"), "expires": sub.get("expireTime"), "action": action}


def pubsub_session():
    """The machine's own Cloud account, or the service account key named in gchat.events.key_file."""
    from google.auth.transport.requests import AuthorizedSession

    scopes = ["https://www.googleapis.com/auth/pubsub"]
    key = settings.get("gchat.events.key_file")
    if key:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(settings.resolve(key)), scopes=scopes)
    else:
        import google.auth

        creds, _ = google.auth.default(scopes=scopes)
    return AuthorizedSession(creds)


_named: list[str] = []
_named_lock = threading.Lock()


def _message_names(note: dict) -> list[str]:
    """The Chat messages a note is about (spaces/S/messages/M): one, or several for a batch."""
    try:
        data = json.loads(base64.b64decode(note.get("data") or b"") or b"{}")
    except ValueError:
        data = {}
    found = [m["message"]["name"] for m in [data] + list(data.get("messages") or [])
             if isinstance((m or {}).get("message"), dict) and (m["message"].get("name") or "").startswith("spaces/")]
    subject = ((note.get("attributes") or {}).get("ce-subject") or "").removeprefix("//chat.googleapis.com/")
    return found or ([subject] if "/messages/" in subject else [])


def take_named() -> list[str]:
    """The messages named by notes since the last call (each once)."""
    with _named_lock:
        out = list(dict.fromkeys(_named))
        _named.clear()
    return out


def pull(http, wait: bool = True) -> int:
    """Takes the waiting notes off the subscription and acknowledges them; returns how many there were. The
    messages they name are kept for take_named()."""
    r = http.post(f"{PUBSUB}/{subscription()}:pull", json={"maxMessages": 100, "returnImmediately": not wait},
                  timeout=PULL_SECONDS + 30)
    r.raise_for_status()
    got = r.json().get("receivedMessages", [])
    names = [n for m in got for n in _message_names(m.get("message") or {})]
    with _named_lock:
        _named.extend(names)
    ids = [m["ackId"] for m in got]
    if ids:
        http.post(f"{PUBSUB}/{subscription()}:acknowledge", json={"ackIds": ids}, timeout=30).raise_for_status()
    return len(ids)


class Listener(threading.Thread):
    """Pulls notes and sets `wake`. Errors are recorded once per kind and retried with a growing wait."""

    def __init__(self, wake: threading.Event, log=print, session=None):
        super().__init__(daemon=True, name="chat-events")
        self.wake, self.log, self.http = wake, log, session
        self.stop = threading.Event()

    def run(self) -> None:
        backoff, last_error = 1, None
        while not self.stop.is_set():
            try:
                if self.http is None:
                    self.http = pubsub_session()
                if pull(self.http):
                    db.set_state("gchat_event_at", db.now())
                    self.wake.set()
                backoff, last_error = 1, None
            except Exception as e:
                err = str(e)[:300]
                if err != last_error:
                    db.audit("chat_events_error", entity_type="service", entity_id="chat-events", error=err)
                    self.log(f"chat push listener: {err} (the regular poll goes on)")
                    last_error = err
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 300)

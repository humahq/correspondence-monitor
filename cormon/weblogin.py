"""Google sign-in for the item and answer pages. A page opens only for someone signed in with Google whose email is on
that study's people list (site staff, CRA, sponsor) or in an allowed domain (web.login.allowed_domains). The link's
token is still needed too. Every view and every refusal is logged.

Off until web.login.enabled is true and a Google "Web application" OAuth client is set up, with
<base_url>/auth/callback as its redirect URI (client id in web.login.client_id; its secret in Secret Manager
under web.login.secret, or in the environment variable named by web.login.client_secret_env)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from html import escape
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import dotenv_values

from . import config, db, settings

COOKIE = "cormon_session"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"

LOCAL_EMAIL = "operator@localhost"
LOGIN = "/admin/login"
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
LOCAL_NOT_SET_UP = ("The admin pages on this copy need a local account: set web.admin.local (ADMIN_PASSWORD) and restart. "
                    "This copy is on this computer, so Google sign-in is not used here.")
LOCAL_OFF = "The local admin account is off here (web.admin.local)."
_lock = threading.Lock()
_fails: dict = {}        # client address -> (failures, locked_until)
_pw_cache: dict = {}     # path -> (mtime_ns, values)


def _host(url: str) -> str:
    """Lower-case host of a URL, an Origin, or a Host header ("localhost:8091", "[::1]:8091"); "" if unreadable."""
    try:
        return (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    except ValueError:
        return ""


def _port(url: str) -> int:
    try:
        u = urlparse(url if "//" in url else "//" + url)
        return u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return -1


def _base() -> str:
    return (settings.get("web.base_url") or "").rstrip("/")


def _local_copy() -> bool:
    return not config.live() and _host(_base()) in _LOOPBACK


def local_user() -> str:
    return str(settings.get("web.admin.local.user") or "").strip()


def local_password() -> str:
    name = settings.get("web.admin.local.password_env") or "ADMIN_PASSWORD"
    value = os.environ.get(name) or ""
    if not value:
        rel = settings.get("web.admin.local.password_file")
        if rel:
            path = settings.resolve(rel)
            try:
                mtime = path.stat().st_mtime_ns
                if _pw_cache.get(str(path), (None,))[0] != mtime:
                    _pw_cache[str(path)] = (mtime, dotenv_values(path))
                value = _pw_cache[str(path)][1].get(name) or ""
            except OSError:
                value = ""
    return value if value.strip() else ""


def local_enabled() -> bool:
    return bool(_local_copy() and settings.get("web.admin.local.enabled") and local_user() and local_password())


def local_accounts() -> dict:
    """Sign-in name -> the email its session carries: the cast (names standing for study people, from
    web.admin.local.accounts) and, last so its name always wins, the operator."""
    out: dict = {}
    cast = settings.get("web.admin.local.accounts") or {}
    if isinstance(cast, dict):
        for name, email in cast.items():
            name, email = str(name).strip(), str(email or "").strip().lower()
            if name and "@" in email and email != LOCAL_EMAIL:
                out[name] = email
    if local_user():
        out[local_user()] = LOCAL_EMAIL
    return out


def _pv(acct: str, email: str) -> str:
    """Tag of this account + email + the password: changing any of them ends that local session."""
    return hmac.new(_key(), f"{acct}\n{email}\n{local_password()}".encode(), hashlib.sha256).hexdigest()[:16]


def _session(cookie_header: str | None) -> dict | None:
    jar = SimpleCookie(cookie_header or "")
    return _unsign(jar[COOKIE].value) if COOKIE in jar else None


def _local_ok(data: dict | None) -> bool:
    """A valid local session: local sign-in on, the account still exists with the same email, password unchanged."""
    acct = (data or {}).get("acct")
    return bool(data and data.get("local") and acct and local_enabled()
                and local_accounts().get(acct) == data.get("email")
                and hmac.compare_digest(str(data.get("pv", "")), _pv(acct, data["email"])))


def _cookie(value: str, max_age: int) -> str:
    """Set-Cookie text. Byte-for-byte the old text, except `Secure` is dropped on a plain-http loopback copy
    (Safari keeps no Secure cookie over http)."""
    plain_http = _local_copy() and not _base().startswith("https://")
    return (f"{COOKIE}={value}; Path=/; HttpOnly{'' if plain_http else '; Secure'}; SameSite=Lax; Max-Age={max_age}")


def login_url(path: str) -> str:
    return LOGIN + "?" + urlencode({"next": path})


def safe_next(value: str | None) -> str:
    """Same-site path only; anything odd becomes /admin."""
    v = value or ""
    ok = (v.startswith("/") and not v.startswith(("//", "/\\")) and "\\" not in v and len(v) <= 500
          and all(32 <= ord(c) < 127 for c in v) and v.split("?")[0].split("#")[0] != LOGIN)
    return v if ok else "/admin"


def origin_ok(origin: str | None) -> bool:
    """No Origin: allowed (as before). Else it must be base_url's origin; on a local copy also a loopback alias
    (127.0.0.1 for localhost) with the SAME scheme and port (another local site on another port is refused)."""
    origin = (origin or "").rstrip("/")
    if not origin or origin == _base():
        return True
    return bool(local_enabled() and urlparse(origin).scheme == urlparse(_base()).scheme
                and _host(origin) in _LOOPBACK and _port(origin) == _port(_base()))


def enabled() -> bool:
    return bool(settings.get("web.login.enabled", False))


def ready() -> bool:
    """Google sign-in can run: on for every page, or set up (client id) for the admin pages only."""
    return enabled() or bool(settings.get("web.login.client_id") and settings.get("web.admin.emails"))


def _key() -> bytes:
    k = db.get_state("session_key")
    if not k:
        k = secrets.token_hex(32)
        db.set_state("session_key", k)
    return bytes.fromhex(k)


def _sign(data: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{mac}"


def _unsign(value: str | None) -> dict | None:
    try:
        body, mac = (value or "").rsplit(".", 1)
        if not hmac.compare_digest(mac, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if data.get("exp", 0) > time.time() else None


def seal(data: dict, minutes: float) -> str:
    """`data`, signed so it comes back unchanged, for `minutes` (a confirm step's choices kept in its form)."""
    return _sign(dict(data, exp=time.time() + minutes * 60))


def unseal(value: str | None) -> dict | None:
    return _unsign(value)


def _redirect_uri() -> str:
    return settings.get("web.base_url").rstrip("/") + "/auth/callback"


def signed_in(cookie_header: str | None) -> str | None:
    """The email of the person signed in, from the session cookie; None when there is none or it is not valid."""
    data = _session(cookie_header)
    if not data or (data.get("local") and not _local_ok(data)):
        return None
    return data.get("email")


def sign_in_url(back_to: str) -> str:
    state = _sign({"back": back_to if back_to.startswith("/") else "/", "n": secrets.token_hex(8),
                   "exp": time.time() + 600})
    return AUTH + "?" + urlencode({"client_id": settings.get("web.login.client_id"), "redirect_uri": _redirect_uri(),
                                   "response_type": "code", "scope": "openid email", "state": state,
                                   "prompt": "select_account"})


def may_see(email: str, study_id: str) -> bool:
    email = (email or "").lower()
    if email == LOCAL_EMAIL and local_enabled():
        return True
    domain = email.rsplit("@", 1)[-1]
    if domain in [d.lower() for d in settings.get("web.login.allowed_domains", ["huma.ai"])]:
        return True
    study = config.studies().get(study_id)
    return bool(study) and any((p.email or "").lower() == email for p in study.people)


def _client_secret() -> str:
    from .compliance import secrets as vault
    value = vault.get(env=settings.get("web.login.client_secret_env"), secret=settings.get("web.login.secret"),
                        what="the sign-in client secret")
    if not value:
        raise RuntimeError("the sign-in client secret is not set (web.login.secret or web.login.client_secret_env)")
    return value


def exchange(code: str) -> dict:
    """The signed-in person's claims, from Google's token endpoint. The ID token comes straight from Google over
    HTTPS in reply to our own secret, so its claims are read without a second signature check."""
    r = requests.post(TOKEN, data={"code": code, "client_id": settings.get("web.login.client_id"),
                                   "client_secret": _client_secret(), "redirect_uri": _redirect_uri(),
                                   "grant_type": "authorization_code"}, timeout=15)
    r.raise_for_status()
    part = r.json()["id_token"].split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def callback(path: str) -> tuple[int, dict, str]:
    """(status, headers, body) for /auth/callback: check the state, ask Google who it is, set the session."""
    q = parse_qs(urlparse(path).query)
    state = _unsign((q.get("state") or [""])[0])
    if not state or not q.get("code"):
        return 400, {}, "Sign-in failed or took too long. Please open the link again."
    try:
        claims = exchange(q["code"][0])
    except Exception as e:
        db.audit("sign_in_failed", entity_type="web", entity_id="login", error=str(e)[:300])
        return 502, {}, "Google sign-in could not be checked. Please try again."
    ok = (claims.get("aud") == settings.get("web.login.client_id")
          and claims.get("iss") in ("accounts.google.com", "https://accounts.google.com")
          and claims.get("email_verified") in (True, "true") and claims.get("exp", 0) > time.time())
    if not ok:
        db.audit("sign_in_refused", entity_type="web", entity_id="login", email=claims.get("email"))
        return 403, {}, "That Google sign-in could not be confirmed."
    email = claims["email"].lower()
    hours = float(settings.get("web.login.session_hours", 12))
    cookie = _cookie(_sign({'email': email, 'exp': time.time() + hours * 3600}), int(hours * 3600))
    db.audit("signed_in", entity_type="web", entity_id="login", email=email)
    return 302, {"Location": state["back"], "Set-Cookie": cookie}, ""


def gate(path: str, cookie_header: str | None, study_id: str, what: str, ref) -> tuple[int, dict, str] | None:
    """None when the page may be shown; else the response to send instead (sign in, or not allowed)."""
    if not enabled():
        return None
    email = signed_in(cookie_header)
    if not email:
        return 302, {"Location": sign_in_url(path)}, ""
    if not may_see(email, study_id):
        db.audit("page_refused", entity_type=what, entity_id=ref, study_id=study_id, email=email)
        return 403, {}, (f"You are signed in as {email}, which is not on this study's people list. "
                         f"Ask the study's CRA to add you, or sign in with another account at /auth/logout.")
    db.audit("page_viewed", entity_type=what, entity_id=ref, study_id=study_id, email=email)
    return None


def admin_gate(path: str, cookie_header: str | None, log: bool = True
               ) -> tuple[tuple[int, dict, str] | None, str | None]:
    """(response to send instead, None) or (None, email) for an admin page. Unlike gate(), it stays shut while
    sign-in is not set up, and only the people in web.admin.emails get in. It needs only web.login.client_id
    (and the secret), so the admin pages can ask for sign-in while the item and answer pages still do not."""
    data = _session(cookie_header)
    mine = bool(data and data.get("local"))
    if _local_copy():
        if not local_enabled():
            if mine:
                db.audit("admin_refused", entity_type="web", entity_id=path[:200], why="local account off")
            return (403, {}, LOCAL_OFF if mine else LOCAL_NOT_SET_UP), None
        if _local_ok(data):
            email = data["email"]
            if email != LOCAL_EMAIL and email not in [e.lower() for e in settings.get("web.admin.emails") or []]:
                db.audit("admin_refused", entity_type="web", entity_id=path[:200], email=email)
                return (403, {}, f"You are signed in as {email}, which is not on the admin list. "
                                 f"Sign in as someone else at {LOGIN}."), None
            if log:
                db.audit("admin_viewed", entity_type="web", entity_id=path[:200], email=email)
            return None, email
        return (302, {"Location": login_url(path)}, ""), None     # no session, a Google one, or password changed
    if mine:                                                      # a laptop's cookie shown to a server
        db.audit("admin_refused", entity_type="web", entity_id=path[:200], why="local account off")
        return (403, {}, LOCAL_OFF), None
    if not ready():
        return (403, {}, "The admin pages need Google sign-in set up (web.login.client_id)."), None
    email = signed_in(cookie_header)
    if not email:
        return (302, {"Location": sign_in_url(path)}, ""), None
    if email not in [e.lower() for e in settings.get("web.admin.emails") or []]:
        db.audit("admin_refused", entity_type="web", entity_id=path[:200], email=email)
        return (403, {}, f"You are signed in as {email}, which is not on the admin list. "
                         f"Sign in with another account at /auth/logout."), None
    if log:
        db.audit("admin_viewed", entity_type="web", entity_id=path[:200], email=email)
    return None, email


def login_page(next_: str, bad: bool = False) -> tuple[int, dict, str]:
    note = '<p style="color:#b00">That name or password is not right.</p>' if bad else ""
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
            f'initial-scale=1"><title>Sign in</title><style>body{{font:15px system-ui;margin:40px auto;max-width:340px}}'
            f'input{{display:block;width:100%;padding:8px;margin:6px 0 14px;box-sizing:border-box}}</style></head><body>'
            f'<h1>Sign in</h1><p>This copy runs on this computer. Names: {escape(", ".join(local_accounts()))}. They share one password.</p>{note}'
            f'<form method="post" action="{LOGIN}"><label>Name<input name="user" autocomplete="username" autofocus></label>'
            f'<label>Password<input name="password" type="password" autocomplete="current-password"></label>'
            f'<input type="hidden" name="next" value="{escape(safe_next(next_), quote=True)}"><button>Sign in</button>'
            f'</form></body></html>')
    return 200, {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                 "base-uri 'none'; frame-ancestors 'none'"}, body


def login_post(body: bytes, *, host: str, origin: str | None, ctype: str, client: str) -> tuple[int, dict, str]:
    """(status, headers, text) for POST /admin/login. Caller has already capped the body at 4096 bytes."""
    if not local_enabled():
        return 404, {}, "not found"
    if _host(host or "") not in _LOOPBACK or not origin_ok(origin) or not ctype.startswith("application/x-www-form-urlencoded"):
        db.audit("sign_in_refused", entity_type="web", entity_id="login", why="not from the sign-in page")
        return 403, {}, "That sign-in did not come from this computer's sign-in page."
    with _lock:
        locked = _fails.get(client, (0, 0.0))[1] > time.time()
    if locked:
        db.audit("sign_in_refused", entity_type="web", entity_id="login", why="locked out")
        return 429, {}, "Too many wrong tries. Wait 30 seconds."
    form = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    f = lambda k: (form.get(k) or [""])[0]
    email = local_accounts().get(f("user"))
    good = email is not None and hmac.compare_digest(f("password").encode(), local_password().encode())
    nxt = safe_next(f("next"))
    if not good:
        time.sleep(0.5)
        with _lock:
            n, _ = _fails.get(client, (0, 0.0))
            _fails[client] = (0, time.time() + 30) if n + 1 >= 10 else (n + 1, 0.0)
        db.audit("sign_in_failed", entity_type="web", entity_id="login", why="wrong name or password")
        return 303, {"Location": login_url(nxt) + "&bad=1"}, ""
    with _lock:
        _fails.pop(client, None)
    hours = float(settings.get("web.login.session_hours", 12))
    acct = f("user")
    value = _sign({"email": email, "local": True, "acct": acct, "pv": _pv(acct, email),
                   "exp": time.time() + hours * 3600})
    db.audit("signed_in", entity_type="web", entity_id="login", email=email, account=acct)
    return 303, {"Location": nxt, "Set-Cookie": _cookie(value, int(hours * 3600))}, ""


def logout() -> tuple[int, dict, str]:
    return 200, {"Set-Cookie": _cookie("", 0)}, \
        "Signed out. Open the link again to sign in with another account."

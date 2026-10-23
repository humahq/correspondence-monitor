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
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import config, db, settings

COOKIE = "cormon_session"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"


def enabled() -> bool:
    return bool(settings.get("web.login.enabled", False))


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


def _redirect_uri() -> str:
    return settings.get("web.base_url").rstrip("/") + "/auth/callback"


def signed_in(cookie_header: str | None) -> str | None:
    """The email of the person signed in, from the session cookie; None when there is none or it is not valid."""
    jar = SimpleCookie(cookie_header or "")
    data = _unsign(jar[COOKIE].value) if COOKIE in jar else None
    return data.get("email") if data else None


def sign_in_url(back_to: str) -> str:
    state = _sign({"back": back_to if back_to.startswith("/") else "/", "n": secrets.token_hex(8),
                   "exp": time.time() + 600})
    return AUTH + "?" + urlencode({"client_id": settings.get("web.login.client_id"), "redirect_uri": _redirect_uri(),
                                   "response_type": "code", "scope": "openid email", "state": state,
                                   "prompt": "select_account"})


def may_see(email: str, study_id: str) -> bool:
    email = (email or "").lower()
    domain = email.rsplit("@", 1)[-1]
    if domain in [d.lower() for d in settings.get("web.login.allowed_domains", ["huma.ai"])]:
        return True
    study = config.studies().get(study_id)
    return bool(study) and any((p.email or "").lower() == email for p in study.people)


def _client_secret() -> str:
    env = settings.get("web.login.client_secret_env")
    if env and os.environ.get(env):
        return os.environ[env]
    from .channels.imap import _secret_from_vm
    value = _secret_from_vm(settings.get("web.login.secret") or {}) if settings.get("web.login.secret") else None
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
    cookie = (f"{COOKIE}={_sign({'email': email, 'exp': time.time() + hours * 3600})}; Path=/; HttpOnly; Secure; "
              f"SameSite=Lax; Max-Age={int(hours * 3600)}")
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


def admin_gate(path: str, cookie_header: str | None) -> tuple[tuple[int, dict, str] | None, str | None]:
    """(response to send instead, None) or (None, email) for an admin page. Unlike gate(), it stays shut while
    sign-in is off, and only the people in web.admin.emails get in."""
    if not enabled():
        return (403, {}, "The admin pages need Google sign-in turned on (web.login.enabled)."), None
    email = signed_in(cookie_header)
    if not email:
        return (302, {"Location": sign_in_url(path)}, ""), None
    if email not in [e.lower() for e in settings.get("web.admin.emails") or []]:
        db.audit("admin_refused", entity_type="web", entity_id=path[:200], email=email)
        return (403, {}, f"You are signed in as {email}, which is not on the admin list. "
                         f"Sign in with another account at /auth/logout."), None
    db.audit("admin_viewed", entity_type="web", entity_id=path[:200], email=email)
    return None, email


def logout() -> tuple[int, dict, str]:
    return 200, {"Set-Cookie": f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"}, \
        "Signed out. Open the link again to sign in with another account."

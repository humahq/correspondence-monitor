"""Loads system settings and resolves project paths."""

from __future__ import annotations

import getpass
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CODE_ROOT = Path(__file__).resolve().parent.parent
ROOT = Path(os.environ.get("CORMON_HOME", CODE_ROOT))
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"
# Deployment-specific settings (the bot's name, avatar and signature, for example). Not in the repository:
# each customer deployment keeps its own. Values here win over settings.yaml, key by key.
LOCAL_FILE = CONFIG_DIR / "settings.local.yaml"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _merge(base: dict, override: dict) -> dict:
    """Override wins; dictionaries merge key by key so a local file can set just one setting."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


@lru_cache(maxsize=1)
def settings() -> dict:
    """A copy in another folder (CORMON_HOME) takes its llm: section (models, efforts) from the code's own
    settings, so an old copy never runs on models the project has moved away from. CORMON_MODEL and the other
    CORMON_* variables still override it for one run. settings.local.yaml holds this deployment's own values."""
    data = load_yaml(SETTINGS_FILE)
    main = CODE_ROOT / "config" / "settings.yaml"
    if ROOT.resolve() != CODE_ROOT and main.exists():
        data["llm"] = load_yaml(main).get("llm", {})
    if LOCAL_FILE.exists():
        _merge(data, load_yaml(LOCAL_FILE))
    return data


def reload() -> None:
    settings.cache_clear()


def get(path: str, default: Any = None) -> Any:
    node: Any = settings()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def bot_name() -> str:
    """The deployment's name for the bot, in email, the admin pages and ops warnings."""
    return get("bot.name") or "Study monitor"


def bot_avatar() -> str:
    return get("bot.avatar_url") or ""


def bot_signature() -> str:
    """The default email sign-off. A deployment can set the whole block with bot.signature."""
    return get("bot.signature") or (
        f"{bot_name()}\n"
        "(automated message from the study monitoring service; it does not review safety information)"
    )


def feature(name: str) -> bool:
    """A new feature's switch (features: in settings.yaml, docs/features.md): off unless switched on.
    CORMON_TEST_FEATURES (names separated by commas, or "all") switches features on for a test run."""
    wanted = {x.strip() for x in os.environ.get("CORMON_TEST_FEATURES", "").split(",") if x.strip()}
    return bool(get(f"features.{name}", False)) or name in wanted or "all" in wanted


def resolve(rel: str | Path) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def data_dir() -> Path:
    d = resolve(get("system.data_dir", "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def studies_dir() -> Path:
    return resolve(get("system.studies_dir", "studies"))


def actor() -> str:
    return os.environ.get("CORMON_OPERATOR") or getpass.getuser()


class NotAnAdmin(PermissionError):
    pass


def login_name(email: str) -> str:
    """The server login Google OS Login makes for an account: john@huma.ai -> john_huma_ai."""
    return "".join(c if c.isalnum() else "_" for c in email.lower())[:32]


def signed_in() -> str | None:
    """IAM-01: the person's own server login, when names are checked; None when they are not (a laptop copy).
    On the server `cormon` runs through sudo, which sets SUDO_USER to the Google sign-in; the caller cannot
    choose it. compliance.checked_names turns the check on or off (default: on under sudo)."""
    login = os.environ.get("SUDO_USER")
    if not get("compliance.checked_names", bool(login)):
        return None
    admins = {login_name(e) for e in get("web.admin.emails", []) or []}
    if not login or login.lower() not in admins:
        raise NotAnAdmin(f"{login or 'this login'} is not one of the admins (web.admin.emails); "
                         "commands that act on the bot are run by an admin signed in with their own account")
    return login.lower()

"""Loads system settings and resolves project paths."""

from __future__ import annotations

import getpass
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(os.environ.get("CORMON_HOME", Path(__file__).resolve().parent.parent))
load_dotenv(ROOT / ".env")

CONFIG_DIR = ROOT / "config"
SETTINGS_FILE = CONFIG_DIR / "settings.yaml"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def settings() -> dict:
    return load_yaml(SETTINGS_FILE)


def reload() -> None:
    settings.cache_clear()


def get(path: str, default: Any = None) -> Any:
    node: Any = settings()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


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

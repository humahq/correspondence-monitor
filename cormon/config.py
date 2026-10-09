"""Study, triage, routing and template configuration, with change tracking."""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import db, settings
from .settings import CONFIG_DIR, load_yaml


@dataclass
class Person:
    id: str
    name: str
    email: str | None
    roles: list[str]
    sites: list[str]
    teams_upn: str | None = None

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else "Hello"

    def at_site(self, site_id: str | None) -> bool:
        return "*" in self.sites or site_id is None or site_id in self.sites


@dataclass
class Study:
    study_id: str
    short_name: str
    title: str
    sponsor: str
    address_tag: str
    monitor_signature: str
    documents: list[dict]
    roles: list[dict]
    sites: list[dict]
    people: list[Person]
    pattern_classes: list[dict]
    teams: dict
    triage: list[dict]
    dir: Path
    raw: dict = field(repr=False, default_factory=dict)

    def doc(self, doc_id: str) -> dict:
        for d in self.documents:
            if d["id"] == doc_id:
                return d
        raise KeyError(doc_id)

    def doc_path(self, doc_id: str) -> Path:
        return self.dir / self.doc(doc_id)["file"]

    def pattern(self, pid: str) -> dict | None:
        return next((p for p in self.pattern_classes if p["id"] == pid), None)

    def role_label(self, role_id: str) -> str:
        return next((r["label"] for r in self.roles if r["id"] == role_id), role_id)

    def site_name(self, site_id: str | None) -> str:
        if not site_id:
            return "site not identified"
        return next((s["name"] for s in self.sites if s["id"] == site_id), site_id)

    def person_by_email(self, email: str | None) -> Person | None:
        if not email:
            return None
        e = email.lower()
        return next((p for p in self.people if p.email and p.email.lower() == e), None)

    def person(self, pid: str | None) -> Person | None:
        return next((p for p in self.people if p.id == pid), None)

    def people_with_role(self, role: str, site_id: str | None = None) -> list[Person]:
        return [p for p in self.people if role in p.roles and p.at_site(site_id)]

    @property
    def alias(self) -> str:
        local, domain = settings.get("mailbox.address", "bot@huma.ai").split("@")
        return f"{local}+{self.address_tag}@{domain}"


def _person(d: dict) -> Person:
    return Person(
        id=d["id"], name=d.get("name", d["id"]), email=d.get("email"),
        roles=list(d.get("roles", [])), sites=list(d.get("sites", ["*"])),
        teams_upn=d.get("teams_upn"),
    )


def load_study(path: Path) -> Study:
    raw = load_yaml(path)
    return Study(
        study_id=raw["study_id"],
        short_name=raw.get("short_name", raw["study_id"]),
        title=raw.get("title", ""),
        sponsor=raw.get("sponsor", ""),
        address_tag=str(raw.get("address_tag", raw["study_id"])),
        monitor_signature=raw.get("monitor_signature", "Correspondence Monitor"),
        documents=raw.get("documents", []),
        roles=raw.get("roles", []),
        sites=raw.get("sites", []),
        people=[_person(p) for p in raw.get("people", [])],
        pattern_classes=raw.get("pattern_classes", []),
        teams=raw.get("teams", {}) or {},
        triage=raw.get("triage", []) or [],
        dir=path.parent,
        raw=raw,
    )


def studies() -> dict[str, Study]:
    out: dict[str, Study] = {}
    sdir = settings.studies_dir()
    if not sdir.exists():
        return out
    for p in sorted(sdir.glob("*/study.yaml")):
        s = load_study(p)
        out[s.study_id] = s
    return out


def study(study_id: str) -> Study:
    s = studies().get(study_id)
    if s is None:
        raise KeyError(f"unknown study {study_id}")
    return s


def study_for_tag(tag: str) -> Study | None:
    t = tag.lower()
    return next((s for s in studies().values() if s.address_tag.lower() == t), None)


def triage_rules(study_id: str | None = None) -> list[dict]:
    rules = list(load_yaml(CONFIG_DIR / "triage.yaml").get("rules", []))
    if study_id:
        rules = list(study(study_id).triage) + rules
    return rules


def routing() -> dict:
    return load_yaml(CONFIG_DIR / "routing.yaml")


def templates() -> list[dict]:
    return load_yaml(CONFIG_DIR / "templates.yaml").get("templates", [])


# ---------------------------------------------------------------------------
# Change tracking: every edit to a config file is recorded with who and when.

def tracked_files() -> list[Path]:
    files = sorted(CONFIG_DIR.glob("*.yaml"))
    files += sorted(settings.studies_dir().glob("*/study.yaml"))
    return files


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(settings.ROOT))
    except ValueError:
        return str(p)


def record_config_changes(actor: str | None = None, reason: str | None = None) -> list[str]:
    """Compare config files with their last recorded version; log any change."""
    changed: list[str] = []
    for path in tracked_files():
        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode()).hexdigest()
        rel = _rel(path)
        last = db.q1(
            "SELECT sha256, content FROM config_versions WHERE path=? ORDER BY id DESC LIMIT 1", (rel,)
        )
        if last and last["sha256"] == sha:
            continue
        who = actor or "unknown (file edited directly)"
        db.ex(
            "INSERT INTO config_versions(path, sha256, content, actor, recorded_at) VALUES (?,?,?,?,?)",
            (rel, sha, content, who, db.now()),
        )
        diff = ""
        if last:
            diff = "".join(difflib.unified_diff(
                last["content"].splitlines(True), content.splitlines(True), "before", "after", n=1
            ))[:8000]
        db.audit(
            "config_changed" if last else "config_recorded",
            entity_type="config", entity_id=rel,
            actor=who, actor_type="operator" if actor else "system",
            diff=diff, reason=reason,
        )
        changed.append(rel)
    if changed:
        settings.reload()
    return changed


def get_path(data: Any, dotted: str) -> Any:
    node = data
    for part in dotted.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node[part]
    return node


def set_path(data: Any, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node.setdefault(part, {})
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value

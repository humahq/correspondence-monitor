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
    phone: str | None = None
    languages: list[str] = field(default_factory=list)   # the languages they read; none listed: the shared one

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
        return next((s["name"] for s in self.sites if s["id"] == site_id), f"{site_id} (not in the study's site list)")

    def known_site(self, site_id: str | None) -> bool:
        return any(s["id"] == site_id for s in self.sites)

    def version_at(self, site_id: str, doc_id: str, day: str) -> str | None:
        """VAL-04: the version of a document the site worked to on a day (YYYY-MM-DD), from the site's `protocols`
        list (each version with the day it took effect there). None before the first. A document the site does not
        list is taken to be the version loaded."""
        site = next((s for s in self.sites if s["id"] == site_id), {})
        listed = sorted((p for p in site.get("protocols") or [] if p["document"] == doc_id),
                        key=lambda p: str(p.get("from") or ""))
        if not listed:
            return self.doc(doc_id).get("version")
        now = [p for p in listed if str(p.get("from") or "") <= day]
        return str(now[-1]["version"]) if now else None

    def version_problems(self, site_id: str | None, day: str) -> list[str]:
        """Why an alert's check may not hold for that site on that day: the site was on a different version of a
        document than the one loaded. A site that is not known is checked against every site."""
        sites = [site_id] if self.known_site(site_id) else [s["id"] for s in self.sites]
        out = []
        for d in self.documents:
            for s in sites:
                had = self.version_at(s, d["id"], day)
                if had != d.get("version"):
                    out.append(f"{self.site_name(s)} was on {d['title']} version {had or '(none yet)'} on {day}; "
                               f"the bot checks against version {d.get('version')}")
        return out

    def person_by_email(self, email: str | None) -> Person | None:
        if not email:
            return None
        e = email.lower()
        return next((p for p in self.people if p.email and p.email.lower() == e), None)

    def person(self, pid: str | None) -> Person | None:
        return next((p for p in self.people if p.id == pid), None)

    def people_with_role(self, role: str, site_id: str | None = None) -> list[Person]:
        """People in the role who cover the site. A site the study does not list is covered by everyone in the
        role, so its alerts are not lost."""
        if not self.known_site(site_id):
            site_id = None
        return [p for p in self.people if role in p.roles and p.at_site(site_id)]

    @property
    def alias(self) -> str:
        local, domain = settings.get("mailbox.address", "bot@huma.ai").split("@")
        return f"{local}+{self.address_tag}@{domain}"


def _person(d: dict) -> Person:
    return Person(
        id=d["id"], name=d.get("name", d["id"]), email=d.get("email"),
        roles=list(d.get("roles", [])), sites=list(d.get("sites", ["*"])),
        teams_upn=d.get("teams_upn"), phone=d.get("phone"),
        languages=[str(x).strip() for x in d.get("languages") or [] if str(x).strip()],
    )


def _chat_rooms(raw: dict) -> dict:
    """The study's chat rooms: study.yaml's, or with a local Mattermost the rooms mattermost.rooms gives it
    ({study_channel: {space: spaces/<id>, name: ...}}), so the demo never posts to the real rooms."""
    teams = raw.get("teams", {}) or {}
    if settings.get("channels.teams") != "mattermost":
        return teams
    return dict((settings.get("mattermost.rooms") or {}).get(raw["study_id"]) or {})


def load_study(path: Path) -> Study:
    raw = load_yaml(path)
    return Study(
        study_id=raw["study_id"],
        short_name=raw.get("short_name", raw["study_id"]),
        title=raw.get("title", ""),
        sponsor=raw.get("sponsor", ""),
        address_tag=str(raw.get("address_tag", raw["study_id"])),
        monitor_signature=raw.get("monitor_signature") or settings.bot_signature(),
        documents=raw.get("documents", []),
        roles=raw.get("roles", []),
        sites=raw.get("sites", []),
        people=[_person(p) for p in raw.get("people", [])],
        pattern_classes=raw.get("pattern_classes", []),
        teams=_chat_rooms(raw),
        triage=raw.get("triage", []) or [],
        dir=path.parent,
        raw=raw,
    )


def studies() -> dict[str, Study]:
    """The studies this copy runs: every folder under studies/, or only those listed in system.studies."""
    out: dict[str, Study] = {}
    sdir = settings.studies_dir()
    if not sdir.exists():
        return out
    only = settings.get("system.studies") or None
    for p in sorted(sdir.glob("*/study.yaml")):
        s = load_study(p)
        if only is None or s.study_id in only:
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
    files = sorted(p for p in CONFIG_DIR.glob("*.yaml") if not p.name.startswith("._"))  # macOS copy leftovers
    files += sorted(settings.studies_dir().glob("*/study.yaml"))
    return files


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(settings.ROOT))
    except ValueError:
        return str(p)


# What the release gate tests (CFG-02): on the live copy these change only through GitHub and deploy/release.sh.
TESTED_FILES = ("config/triage.yaml", "config/routing.yaml", "config/templates.yaml", "config/careful.yaml",
                "config/privacy.yaml")
TESTED_SETTINGS = ("llm", "matching", "features", "language", "safety")
GATED = ("{} changes what the release gate tests (CFG-02), so on the live bot it changes only through GitHub: "
         "commit it, let the gate pass, then deploy/release.sh puts it in place")


def live() -> bool:
    """The live bot's copy (not a laptop copy or the server's sandbox): compliance.live, default /opt/cormon."""
    return bool(settings.get("compliance.live", settings.ROOT == Path("/opt/cormon")))


def tested_part(rel: str, content: str) -> Any:
    """The part of a config file the gate tests; None for a file it does not test."""
    if rel in TESTED_FILES or (rel.startswith("studies/") and rel.endswith("/study.yaml")):
        return content
    if rel == "config/settings.yaml":
        import yaml
        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError:
            return content
        return {k: data.get(k) for k in TESTED_SETTINGS}
    return None


def changes_tested(rel: str, old: str, new: str) -> bool:
    return tested_part(rel, old) != tested_part(rel, new)


def in_force(rel: str) -> dict | None:
    """The version of a config file in use: the newest one that is approved or was never made to wait."""
    row = db.q1("SELECT * FROM config_versions WHERE path=? AND (waiting=0 OR approved_by IS NOT NULL)"
                " ORDER BY id DESC LIMIT 1", (rel,))
    return dict(row) if row else None


def needs_approval() -> bool:
    """system.config_needs_approval, read from the settings in use, so turning it off waits for approval too.
    Default: on for the live copy only, so a laptop copy's files (edited in git) are never put back."""
    on = live()
    current = in_force(_rel(settings.SETTINGS_FILE))
    if current:
        import yaml
        return bool(((yaml.safe_load(current["content"]) or {}).get("system") or {}).get("config_needs_approval", on))
    return bool(settings.get("system.config_needs_approval", on))


def record_config_changes(actor: str | None = None, reason: str | None = None) -> list[str]:
    """Compare config files with their last recorded version; log any change. With approvals on (CFG-01) a change
    to a file that already has a version in use is kept as a waiting version and the file is put back to the
    version in use, until a second person approves it (approve_version). Returns the files whose use changed."""
    changed: list[str] = []
    wait_on = needs_approval()
    for path in tracked_files():
        content = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode()).hexdigest()
        rel = _rel(path)
        live = in_force(rel)
        if live and live["sha256"] == sha:
            continue
        wait = bool(live) and wait_on
        same = db.q1("SELECT id FROM config_versions WHERE path=? AND sha256=? AND waiting=1 AND approved_by IS NULL"
                     " AND id>?", (rel, sha, live["id"])) if wait else None
        if not same:
            who = actor or "unknown (file edited directly)"
            vid = db.ex(
                "INSERT INTO config_versions(path, sha256, content, actor, recorded_at, waiting) VALUES (?,?,?,?,?,?)",
                (rel, sha, content, who, db.now(), int(wait)),
            )
            diff = ""
            if live:
                diff = "".join(difflib.unified_diff(
                    live["content"].splitlines(True), content.splitlines(True), "before", "after", n=1
                ))[:8000]
            db.audit(
                "config_change_waiting" if wait else "config_changed" if live else "config_recorded",
                entity_type="config", entity_id=rel,
                actor=who, actor_type="operator" if actor else "system",
                diff=diff, reason=reason, version=vid,
            )
        if wait:  # the file goes back to the version in use
            path.write_text(live["content"], encoding="utf-8")
        else:
            changed.append(rel)
    settings.reload()  # also picks up a version another process (cormon config approve) put in use
    return changed


def waiting() -> list[dict]:
    """Config versions recorded but not in use until a second person approves them."""
    return [dict(r) for r in db.q(
        "SELECT v.id, v.path, v.actor, v.recorded_at FROM config_versions v WHERE v.waiting=1"
        " AND v.approved_by IS NULL AND v.id > COALESCE((SELECT MAX(id) FROM config_versions w WHERE w.path=v.path"
        " AND (w.waiting=0 OR w.approved_by IS NOT NULL)), 0) ORDER BY v.id")]


class ConfigApprovalRefused(ValueError):
    pass


def unapproved() -> list[dict]:
    """Each config file whose version in use has no second person's approval: one recorded before approvals
    were required, or the first version of a file."""
    return [dict(r) for r in db.q(
        "SELECT v.id, v.path, v.actor, v.recorded_at FROM config_versions v WHERE v.approved_by IS NULL"
        " AND v.waiting=0 AND v.id=(SELECT MAX(id) FROM config_versions w WHERE w.path=v.path"
        " AND (w.waiting=0 OR w.approved_by IS NOT NULL)) ORDER BY v.id")]


def approve_version(version_id: int, who: str) -> dict:
    row = db.q1("SELECT * FROM config_versions WHERE id=?", (version_id,))
    if row is None:
        raise ConfigApprovalRefused(f"there is no config version {version_id}")
    if row["approved_by"]:
        raise ConfigApprovalRefused(f"config version {version_id} was already approved by {row['approved_by']}")
    if who.lower() == (row["actor"] or "").lower():
        raise ConfigApprovalRefused(f"config version {version_id} was made by {who}; a different person must approve it")
    current = in_force(row["path"])
    if row["waiting"] and current and current["id"] > row["id"]:
        raise ConfigApprovalRefused(f"config version {version_id} is older than version {current['id']}, which is "
                                    "in use; make the change again")
    if row["waiting"] and current and live() and changes_tested(row["path"], current["content"], row["content"]):
        raise ConfigApprovalRefused(GATED.format(row["path"]))
    if row["waiting"]:
        import yaml
        try:
            yaml.safe_load(row["content"])
        except yaml.YAMLError as e:
            raise ConfigApprovalRefused(f"config version {version_id} is not valid YAML: {e}") from None
    now = db.now()
    db.ex("UPDATE config_versions SET approved_by=?, approved_at=? WHERE id=? AND approved_by IS NULL",
          (who, now, version_id))
    db.audit("config_approved", entity_type="config", entity_id=row["path"], actor=who, actor_type="operator",
             version=version_id, sha256=row["sha256"], made_by=row["actor"],
             effective_from=now if row["waiting"] else row["recorded_at"])
    if row["waiting"]:  # it applies from now: the file gets its content
        settings.resolve(row["path"]).write_text(row["content"], encoding="utf-8")
        settings.reload()
    return dict(db.q1("SELECT id, path, approved_by, approved_at FROM config_versions WHERE id=?", (version_id,)))


def _merge_tested_settings(server: str, repo: str) -> str:
    """The server's settings with the gate-tested sections (TESTED_SETTINGS) taken from the repo's."""
    import io

    from ruamel.yaml import YAML
    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096
    rt.indent(mapping=2, sequence=4, offset=2)
    data, new = rt.load(server), rt.load(repo) or {}
    for k in TESTED_SETTINGS:
        if k in new:
            data[k] = new[k]
        elif k in data:
            del data[k]
    out = io.StringIO()
    rt.dump(data, out)
    return out.getvalue()


def release(folder: Path, who: str, gate: str) -> list[str]:
    """CFG-02: deploy/release.sh puts the gate-tested config of a commit in place (folder holds the commit's
    config/ and studies/*/study.yaml). Each change is in use at once, approved by the gate; the server-only
    parts of settings.yaml stay. Returns the files changed."""
    record_config_changes()  # a direct edit made before this is kept as its own (waiting) version
    changed = []
    sources = [(rel, folder / rel) for rel in TESTED_FILES + ("config/settings.yaml",)]
    sources += [(str(p.relative_to(folder)), p) for p in sorted(folder.glob("studies/*/study.yaml"))]
    for rel, src in sources:
        dest = settings.resolve(rel)
        if not src.exists() or not dest.exists():
            continue
        content = src.read_text(encoding="utf-8")
        old = dest.read_text(encoding="utf-8")
        if rel == "config/settings.yaml":
            content = _merge_tested_settings(old, content)
        if not changes_tested(rel, old, content):
            continue
        dest.write_text(content, encoding="utf-8")
        now = db.now()
        vid = db.ex("INSERT INTO config_versions(path, sha256, content, actor, recorded_at, approved_by, approved_at,"
                    " waiting) VALUES (?,?,?,?,?,?,?,0)",
                    (rel, hashlib.sha256(content.encode()).hexdigest(), content, who, now, f"release gate ({gate})",
                     now))
        db.audit("config_released", entity_type="config", entity_id=rel, actor=who, actor_type="operator",
                 version=vid, gate=gate, diff="".join(difflib.unified_diff(
                     old.splitlines(True), content.splitlines(True), "before", "after", n=1))[:8000])
        changed.append(rel)
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

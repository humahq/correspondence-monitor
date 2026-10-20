"""Daily database backups, each checked by opening the copy, and a restore that keeps the old database aside.

A backup is a full SQLite copy (the online backup API, safe while the service runs) in data/backups/.
It counts as made only after the copy passes an integrity check and holds the same rows as the live
database at that moment. Restore needs the service stopped."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db, settings

TABLES = ("messages", "items", "outbox", "audit_log", "rules", "documents")


def folder() -> Path:
    path = settings.data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _counts(c: sqlite3.Connection) -> dict[str, int]:
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def check(path: Path, expect: dict[str, int] | None = None) -> dict:
    """Open a backup read-only: integrity check, and row counts (equal to `expect` when given)."""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(c)
    finally:
        c.close()
    problems = [] if integrity == "ok" else [f"integrity check: {integrity}"]
    problems += [f"{t}: {counts[t]} rows, live had {n}" for t, n in (expect or {}).items() if counts.get(t) != n]
    return {"path": str(path), "ok": not problems, "problems": problems, "counts": counts}


def make(keep: int | None = None) -> dict:
    keep = keep or int(settings.get("system.backup_keep", 7))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = folder() / f"cormon-{stamp}.db"
    live = db.conn()
    out = sqlite3.connect(path)
    try:
        live.execute("BEGIN")  # hold one snapshot so the counts and the copy agree
        try:
            expect = _counts(live)
            live.backup(out)
        finally:
            live.execute("COMMIT")
        out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm beside it
    finally:
        out.close()
    result = check(path, expect)
    if not result["ok"]:
        path.rename(path.with_suffix(".bad"))
    else:
        for old in sorted(folder().glob("cormon-*.db"))[:-keep]:
            for p in (old, Path(f"{old}-wal"), Path(f"{old}-shm")):
                p.unlink(missing_ok=True)
    db.audit("backup_made" if result["ok"] else "backup_failed", entity_type="service", entity_id="backup",
             path=path.name, problems=result["problems"] or None, counts=result["counts"])
    return result


def due() -> bool:
    rehearsal = {settings.get("channels.email"), settings.get("channels.teams")} <= {"sim", "off"}
    return not rehearsal and db.get_state("backup_day") != datetime.now(timezone.utc).strftime("%Y-%m-%d")


def daily(log=print) -> dict | None:
    if not due():
        return None
    result = make()
    db.set_state("backup_day", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    log(f"backup {'made' if result['ok'] else 'FAILED'}: {Path(result['path']).name}")
    return result


def restore(path: Path) -> dict:
    """Replace the live database with a checked backup. The current one is kept beside it (.before-restore).
    Outbox numbers are reused after a restore, so the chat message ids get a new install id."""
    result = check(path)
    if not result["ok"]:
        raise RuntimeError(f"backup {path} failed its check: {result['problems']}")
    live = db.db_path()
    if db._local.__dict__.get("conn") is not None:
        db._local.conn.close()
        db._local.conn = None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    kept = live.with_name(f"{live.name}.before-restore-{stamp}")
    if live.exists():  # through SQLite, so changes still in the -wal file are kept too
        src, dst = sqlite3.connect(live), sqlite3.connect(kept)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    for side in ("-wal", "-shm"):
        Path(str(live) + side).unlink(missing_ok=True)
    shutil.copy2(path, live)
    db.set_state("install_id", secrets.token_hex(4))
    db.audit("backup_restored", entity_type="service", entity_id="backup", path=str(path), kept=kept.name)
    return {"restored": str(path), "kept": str(kept), "counts": result["counts"]}

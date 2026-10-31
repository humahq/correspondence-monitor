"""The write-once store (OUT-01, AUD-02): a second copy of every original, every sent message and every audit-trail
entry, kept apart from the bot's database, which cannot be changed and cannot be deleted before its keep-until date.

    separate_db    (built) a separate SQLite file, compliance.vault_path (default data/vault/vault.db). Its
                   triggers refuse every UPDATE, and a DELETE before keep_until. A stand-in: someone with the file
                   can still drop the triggers.
    locked_bucket  (later) a Cloud Storage bucket with a retention lock, in a project the operator role cannot
                   write. Same functions, so nothing that calls them changes.

Originals and sent messages are kept until data.retention_days after they were stored (then expire() removes them,
like the retention purge); audit entries are kept forever (AUD-08). A failed copy never stops the bot: it is
recorded, and the daily check copies what is missing and tells the admins about anything that differs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, settings
from . import backend, sha256

BUILT = ("separate_db",)
_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}
_NOW = "strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
    data BLOB NOT NULL, stored_at TEXT NOT NULL, keep_until TEXT, UNIQUE(kind, ref, sha256));
CREATE TRIGGER IF NOT EXISTS objects_write_once BEFORE UPDATE ON objects
BEGIN SELECT RAISE(ABORT, 'write-once: a stored object cannot be changed'); END;
CREATE TRIGGER IF NOT EXISTS objects_kept BEFORE DELETE ON objects
WHEN OLD.keep_until IS NULL OR OLD.keep_until > {_NOW}
BEGIN SELECT RAISE(ABORT, 'write-once: a stored object is kept until its keep-until date'); END;
"""


def path() -> Path:
    p = settings.get("compliance.vault_path")
    return settings.resolve(p) if p else settings.data_dir() / "vault" / "vault.db"


def _conn() -> sqlite3.Connection:
    backend("vault", BUILT)
    p = path()
    c = _conns.get(str(p))
    if c is None:
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(p, timeout=30, check_same_thread=False, isolation_level=None)
        c.executescript(_SCHEMA)
        _conns[str(p)] = c
    return c


def _keep_until(keep_days: int | None) -> str | None:
    if keep_days is None:
        return None
    return (datetime.now(timezone.utc) + timedelta(days=int(keep_days))).isoformat(timespec="seconds")


def put(kind: str, ref: str, data: bytes | str, keep_days: int | None = None) -> str | None:
    """Keep one object; its SHA-256, or None when the copy failed (recorded, retried by the daily check)."""
    raw = data.encode() if isinstance(data, str) else data
    digest = sha256(raw)
    try:
        with _lock:
            _conn().execute("INSERT OR IGNORE INTO objects(kind, ref, sha256, size, data, stored_at, keep_until)"
                            " VALUES (?,?,?,?,?,?,?)", (kind, ref, digest, len(raw), raw, db.now(),
                                                        _keep_until(keep_days)))
    except NotImplementedError:
        raise
    except Exception as e:
        db.set_state("vault_error", f"{db.now()} {kind} {ref}: {str(e)[:300]}")
        return None
    return digest


def get(kind: str, ref: str) -> list[bytes]:
    with _lock:
        return [r[0] for r in _conn().execute("SELECT data FROM objects WHERE kind=? AND ref=? ORDER BY id",
                                              (kind, ref))]


def retention_days() -> int | None:
    days = settings.get("data.retention_days")
    return days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else None


def audit_line(row) -> str:
    """One audit-trail entry exactly as the chain holds it (its fields, the entry before's hash and its own)."""
    from .trail import FIELDS
    return json.dumps([row["id"], *[row[k] for k in FIELDS], row["prev_hash"], row["hash"]],
                      ensure_ascii=False, separators=(",", ":"))


def expire() -> int:
    """Remove the objects past their keep-until date (the retention period); audit entries are never removed."""
    with _lock:
        return _conn().execute(f"DELETE FROM objects WHERE keep_until IS NOT NULL AND keep_until <= {_NOW}").rowcount


def _held() -> tuple[set[tuple[str, str]], dict[str, bytes]]:
    with _lock:
        c = _conn()
        have = {(k, s) for k, s in c.execute("SELECT kind, sha256 FROM objects")}
        audit = {r: d for r, d in c.execute("SELECT ref, data FROM objects WHERE kind='audit'")}
    return have, audit


def backfill() -> dict:
    """Copy what is not in the store yet: originals kept before it existed, and anything a failed put() missed."""
    from . import records
    have, audit = _held()
    n_rec = n_aud = 0
    for r in db.q("SELECT * FROM records WHERE purged_at IS NULL ORDER BY id"):
        if (r["kind"], r["sha256"]) in have:
            continue
        data = records._current(r)
        if data is not None and sha256(data) == r["sha256"]:
            n_rec += bool(put(r["kind"], f"{r['owner_type']}:{r['owner_id']}", data, retention_days()))
    for r in db.q("SELECT * FROM audit_log WHERE hash IS NOT NULL ORDER BY id"):
        if str(r["id"]) not in audit:
            n_aud += bool(put("audit", str(r["id"]), audit_line(r)))
    return {"records": n_rec, "audit": n_aud}


def verify() -> dict:
    """Every stored object still has its hash; every original and every chained audit entry in the bot's database
    has its copy here, and each audit entry still reads as its copy does."""
    problems = []
    with _lock:
        for oid, kind, ref, digest, data in _conn().execute("SELECT id, kind, ref, sha256, data FROM objects"):
            if sha256(data) != digest:
                problems.append(f"stored {kind} {ref} (object {oid}) no longer matches its hash")
    have, audit = _held()
    for r in db.q("SELECT id, kind, owner_type, owner_id, sha256 FROM records WHERE purged_at IS NULL"):
        if (r["kind"], r["sha256"]) not in have:
            problems.append(f"{r['kind']} of {r['owner_type']} {r['owner_id']} (record {r['id']}) has no stored copy")
    entries = 0
    for r in db.q("SELECT * FROM audit_log WHERE hash IS NOT NULL ORDER BY id"):
        entries += 1
        copy = audit.get(str(r["id"]))
        if copy is None:
            problems.append(f"audit entry {r['id']} has no stored copy")
        elif copy.decode() != audit_line(r):
            problems.append(f"audit entry {r['id']} differs from its stored copy")
    return {"ok": not problems, "objects": len(have), "audit_entries": entries, "problems": problems}


def daily(log=print) -> dict | None:
    """Once a day: copy what is missing, remove what is past its keep-until date, then check. Problems are
    recorded and the admins are told once."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.get_state("vault_day") == today:
        return None
    db.set_state("vault_day", today)
    copied, expired = backfill(), (expire() if retention_days() else 0)
    result = verify()
    error = db.get_state("vault_error")
    db.audit("vault_checked", entity_type="service", entity_id="vault", copied=copied, expired=expired,
             objects=result["objects"], problems=len(result["problems"]), last_error=error)
    if result["problems"]:
        from . import alerts
        lines = "\n".join(f"- {p}" for p in result["problems"][:50])
        alerts.notify("vault", f"Study monitor: {len(result['problems'])} problem(s) in the write-once store",
                      f"The daily check of the write-once store ({path()}) found:\n\n{lines}\n\n"
                      + (f"Last failed copy: {error}\n\n" if error else "") + "Nothing was changed.")
    log(f"write-once store: {result['objects']} objects, copied {copied}, expired {expired}, "
        f"{len(result['problems'])} problem(s)")
    return result

"""The audit trail: append-only (database triggers refuse UPDATE and DELETE) and hash-chained, so an entry that is
changed, removed or slipped in later breaks the chain where it happened.

Each entry's hash covers its fields and the hash of the entry before it. The first chained entry links to a seal:
one hash over every entry written before the chain began. Once a day the latest entry's id and hash are written to
an anchor file outside the database; the verifier walks the whole chain and checks every anchor still matches, so
cutting entries off the end is caught too. Each entry and each anchor is also copied to the write-once store
(vault.py), which checks every day that the entries here still read as their copies do.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import db, settings
from . import backend, sha256, vault

BUILT = ("local",)
FIELDS = ("ts", "actor", "actor_type", "action", "entity_type", "entity_id", "study_id", "details")
_lock = threading.Lock()

# rows changed in place: every change is kept in row_changes with its old and new value. Patient text (the
# columns listed) is kept as its length only, so the retention purge still removes it.
TRACKED = {
    "messages": {"from_name", "subject", "body_text", "body_html", "headers", "error"},
    "attachments": {"filename", "text"},
    "items": {"title", "evidence", "reasoning", "tier_reason", "recipient_reason"},
    "outbox": {"subject", "body", "meta", "last_error"},
    "rules": {"text", "source_quote"},
}


def _line(row) -> str:
    return json.dumps([row[k] for k in FIELDS], ensure_ascii=False, separators=(",", ":"))


def entry_hash(prev: str, row) -> str:
    return sha256(prev + "\n" + _line(row))


def _seal(c: sqlite3.Connection) -> str:
    h = hashlib.sha256()
    for r in c.execute("SELECT * FROM audit_log WHERE hash IS NULL ORDER BY id"):
        h.update((_line(r) + "\n").encode())
    return "seal:" + h.hexdigest()


def append(row: dict) -> int:
    """Write one entry linked to the last one. BEGIN IMMEDIATE keeps two writers (the service and a command run
    at the same time) from linking to the same entry."""
    backend("trail", BUILT)
    c = db.conn()
    with _lock:
        own = not c.in_transaction
        if own:
            c.execute("BEGIN IMMEDIATE")
        try:
            last = c.execute("SELECT hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else _seal(c)
            own_hash = entry_hash(prev, row)
            rid = c.execute(
                "INSERT INTO audit_log (ts, actor, actor_type, action, entity_type, entity_id, study_id, details,"
                " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*[row[k] for k in FIELDS], prev, own_hash)).lastrowid
            if own:
                c.execute("COMMIT")
        except BaseException:
            if own:
                c.execute("ROLLBACK")
            raise
    vault.put("audit", str(rid), vault.audit_line(dict(row, id=rid, prev_hash=prev, hash=own_hash)))
    return rid


def _trigger_sql(c: sqlite3.Connection, table: str) -> tuple[str, list[str]]:
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    key = "id"
    parts = []
    for col in cols:
        if col == key:
            continue
        if col in TRACKED[table]:
            value = f"json_array(length(OLD.{col}), length(NEW.{col}))"
            name = f"{col}:chars"
        else:
            value = f"json_array(OLD.{col}, NEW.{col})"
            name = col
        parts.append(f"'{name}', CASE WHEN OLD.{col} IS NOT NEW.{col} THEN {value} END")
    changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in cols if c != key)
    ts = "strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"
    tag = sha256(",".join(cols))[:8]
    update = (f"CREATE TRIGGER chg_{table}_{tag}_u AFTER UPDATE ON {table} WHEN {changed} BEGIN "
              f"INSERT INTO row_changes(ts, tbl, row_id, changes) VALUES ({ts}, '{table}', OLD.{key}, "
              f"json_patch('{{}}', json_object({', '.join(parts)}))); END")
    delete = (f"CREATE TRIGGER chg_{table}_{tag}_d AFTER DELETE ON {table} BEGIN "
              f"INSERT INTO row_changes(ts, tbl, row_id, changes) VALUES ({ts}, '{table}', OLD.{key}, "
              f"'{{\"_deleted\":true}}'); END")
    return tag, [update, delete]


def track_changes(c: sqlite3.Connection) -> None:
    """(Re)make the change triggers; made again when a table gains a column."""
    for table in TRACKED:
        tag, sqls = _trigger_sql(c, table)
        have = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE ?",
                                        (f"chg_{table}_%",))]
        if sorted(have) == sorted([f"chg_{table}_{tag}_u", f"chg_{table}_{tag}_d"]):
            continue
        for name in have:
            c.execute(f"DROP TRIGGER {name}")
        for sql in sqls:
            c.execute(sql)


def _changes_hash(c: sqlite3.Connection, first: int, last: int) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    for r in c.execute("SELECT id, ts, tbl, row_id, changes FROM row_changes WHERE id BETWEEN ? AND ? ORDER BY id",
                       (first, last)):
        h.update((json.dumps(list(r), ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        n += 1
    return h.hexdigest(), n


def seal_changes(log=print) -> dict | None:
    """Fold the row changes since the last seal into the audit chain (one entry with their hash), so a change
    record altered or removed later breaks the check. Run every hour."""
    c = db.conn()
    last = db.q1("SELECT MAX(id) m FROM row_changes")["m"]
    done = int(db.get_state("changes_sealed_to") or 0)
    if not last or last <= done:
        return None
    digest, n = _changes_hash(c, done + 1, last)
    db.audit("row_changes_sealed", entity_type="service", entity_id="row_changes", first=done + 1, last=last,
             rows=n, sha256=digest)
    db.set_state("changes_sealed_to", str(last))
    return {"first": done + 1, "last": last, "rows": n}


def hourly(log=print) -> dict | None:
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    if db.get_state("changes_hour") == hour:
        return None
    db.set_state("changes_hour", hour)
    return seal_changes(log)


def _verify_changes(c: sqlite3.Connection) -> dict | None:
    """None while every sealed range of row changes still has the hash it was sealed with."""
    end = 0
    for r in c.execute("SELECT id, details FROM audit_log WHERE action='row_changes_sealed' ORDER BY id"):
        d = json.loads(r["details"] or "{}")
        digest, n = _changes_hash(c, d["first"], d["last"])
        if digest != d["sha256"] or n != d["rows"]:
            return {"ok": False, "broken_at": r["id"],
                    "why": f"the record of changes {d['first']} to {d['last']} was altered after it was sealed"}
        if d["first"] != end + 1:
            return {"ok": False, "broken_at": r["id"], "why": "a range of the change record was never sealed"}
        end = d["last"]
    return None


def anchor_file() -> Path:
    return settings.data_dir() / "anchors" / "audit-anchors.txt"


def anchors() -> list[tuple[str, int, str]]:
    f = anchor_file()
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].isdigit():
            out.append((parts[0], int(parts[1]), parts[2]))
    return out


def anchor() -> dict | None:
    """Write the latest entry's id and hash to the anchor file (append-only by habit; write-once when certified)."""
    last = db.q1("SELECT id, hash FROM audit_log WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1")
    if not last:
        return None
    f = anchor_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with f.open("a") as out:
        out.write(f"{day}\t{last['id']}\t{last['hash']}\n")
    vault.put("anchor", day, f"{day}\t{last['id']}\t{last['hash']}")
    return {"at": day, "id": last["id"], "hash": last["hash"]}


def verify() -> dict:
    """Walk the chain from the start. ok, or the first entry where it breaks and why."""
    c = db.conn()
    seal = hashlib.sha256()
    prev, start, checked, hashes = None, None, 0, {}
    for r in c.execute("SELECT * FROM audit_log ORDER BY id"):
        if r["hash"] is None:
            if start is None:
                seal.update((_line(r) + "\n").encode())
                continue
            return {"ok": False, "broken_at": r["id"], "why": "an entry without a hash after the chain began"}
        expect = ("seal:" + seal.hexdigest()) if start is None else prev
        start = start or r["id"]
        if r["prev_hash"] != expect:
            return {"ok": False, "broken_at": r["id"], "why": "it does not link to the entry before it"}
        if entry_hash(expect, r) != r["hash"]:
            return {"ok": False, "broken_at": r["id"], "why": "its content changed after it was written"}
        prev, checked = r["hash"], checked + 1
        hashes[r["id"]] = r["hash"]
    for at, aid, ahash in anchors():
        if hashes.get(aid) != ahash:
            return {"ok": False, "broken_at": aid, "why": f"the anchor written {at} no longer matches (entries removed?)"}
    bad = _verify_changes(c)
    if bad:
        return bad
    return {"ok": True, "entries": checked, "chain_start": start, "last_hash": prev, "anchors": len(anchors())}


def daily(log=print) -> dict | None:
    """Once a day: verify the whole chain, then anchor it. A broken chain is recorded and the admins are told."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.get_state("trail_day") == today:
        return None
    db.set_state("trail_day", today)
    result = verify()
    if result["ok"]:
        result["anchor"] = anchor()
        db.audit("trail_verified", entity_type="service", entity_id="trail", entries=result["entries"])
        log(f"audit trail verified: {result['entries']} entries")
    else:
        db.audit("trail_verify_failed", entity_type="service", entity_id="trail", broken_at=result["broken_at"],
                 why=result["why"])
        from . import alerts
        alerts.notify("trail", "Study monitor: the audit trail failed its check",
                      f"The audit trail check failed at entry {result['broken_at']}: {result['why']}.\n"
                      "Nothing was changed. Look into it before trusting the audit log.")
        log(f"audit trail check FAILED at {result['broken_at']}: {result['why']}")
    return result

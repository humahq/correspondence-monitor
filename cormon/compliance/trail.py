"""The audit trail: append-only (database triggers refuse UPDATE and DELETE) and hash-chained, so an entry that is
changed, removed or slipped in later breaks the chain where it happened.

Each entry's hash covers its fields and the hash of the entry before it. The first chained entry links to a seal:
one hash over every entry written before the chain began. Once a day the latest entry's id and hash are written to
an anchor file outside the database; the verifier walks the whole chain and checks every anchor still matches, so
cutting entries off the end is caught too. Certified backend (later): the anchors and a copy of each entry go to a
write-once bucket the operator role cannot rewrite.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import db, settings
from . import backend, sha256

BUILT = ("local",)
FIELDS = ("ts", "actor", "actor_type", "action", "entity_type", "entity_id", "study_id", "details")
_lock = threading.Lock()


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
            rid = c.execute(
                "INSERT INTO audit_log (ts, actor, actor_type, action, entity_type, entity_id, study_id, details,"
                " prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*[row[k] for k in FIELDS], prev, entry_hash(prev, row))).lastrowid
            if own:
                c.execute("COMMIT")
        except BaseException:
            if own:
                c.execute("ROLLBACK")
            raise
    return rid


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

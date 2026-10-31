"""Originals: every message as it arrived, every attachment and every sent message, each kept with the SHA-256 it
had at capture. The original is the record; what the bot reads from it (events, attachment text) carries its hash.

The local backend writes the bytes under data/ and the hash to the records table. A nightly check re-hashes them;
a message whose original no longer matches is not read (pipeline) and the admins are told. Certified backend
(later): the bytes go to a write-once bucket, the same table keeps the hash and the object's address.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .. import db, settings
from . import backend, sha256, vault

BUILT = ("local",)


def _path(location: str) -> Path:
    p = Path(location)
    return p if p.is_absolute() else settings.data_dir() / p


def put(kind: str, owner_type: str, owner_id: int, data: bytes, location: str | None) -> str:
    """Keep one original and return its SHA-256. location: where it is written under data/; None when the bytes
    already live in a table row (a sent message), so only the hash is kept here."""
    backend("records", BUILT)
    digest = sha256(data)
    if location:
        p = _path(location)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    db.ex("INSERT INTO records(kind, owner_type, owner_id, sha256, size, location, created_at) VALUES (?,?,?,?,?,?,?)",
          (kind, owner_type, owner_id, digest, len(data), location, db.now()))
    vault.put(kind, f"{owner_type}:{owner_id}", data, vault.retention_days())
    return digest


def sent_bytes(channel: str, destination: str, from_addr: str | None, to_json: str, cc_json: str,
               subject: str | None, body: str) -> bytes:
    """A sent message exactly as stored in the outbox (to and cc as the stored JSON text), for its hash."""
    return db.js([channel, destination, from_addr, to_json, cc_json, subject, body]).encode()


def _current(row) -> bytes | None:
    if row["location"]:
        p = _path(row["location"])
        return p.read_bytes() if p.exists() else None
    if row["owner_type"] == "outbox":
        o = db.q1("SELECT * FROM outbox WHERE id=?", (row["owner_id"],))
        return sent_bytes(o["channel"], o["destination"], o["from_addr"], o["to_addrs"], o["cc_addrs"],
                          o["subject"], o["body"]) if o else None
    return None


def check(row) -> str | None:
    """None while the original matches its hash; otherwise what is wrong."""
    if row["purged_at"]:
        return None
    data = _current(row)
    if data is None:
        return "the original is missing"
    if sha256(data) != row["sha256"]:
        return "the original changed after it was kept"
    return None


def check_message(msg_id: int) -> list[str]:
    """Problems with a message's originals (its raw copy and attachments), for the pipeline to refuse to read it."""
    return [f"{r['kind']} {r['location'] or r['id']}: {why}"
            for r in db.q("SELECT * FROM records WHERE owner_type='message' AND owner_id=?", (msg_id,))
            if (why := check(r))]


def mark_purged(locations: list[str]) -> int:
    """Originals removed on purpose at the end of the retention period: no longer expected to match."""
    n = 0
    for loc in locations:
        n += db.changed("UPDATE records SET purged_at=? WHERE location=? AND purged_at IS NULL", (db.now(), loc))
    return n


def backfill() -> int:
    """Fingerprint originals captured before records were kept (kind ends in _backfill: hashed later, not at
    capture, so they prove nothing about before today but are protected from now on)."""
    n = 0
    for m in db.q("SELECT id, raw_path FROM messages WHERE raw_path IS NOT NULL AND id NOT IN"
                  " (SELECT owner_id FROM records WHERE owner_type='message')"):
        p = _path(m["raw_path"])
        if not p.exists():
            continue
        data = p.read_bytes()
        digest = sha256(data)
        db.ex("INSERT INTO records(kind, owner_type, owner_id, sha256, size, location, created_at) VALUES"
              " (?,?,?,?,?,?,?)", ("message_raw_backfill", "message", m["id"], digest, len(data),
                                   m["raw_path"], db.now()))
        db.ex("UPDATE messages SET sha256=? WHERE id=? AND sha256 IS NULL", (digest, m["id"]))
        n += 1
        for a in db.q("SELECT id, path FROM attachments WHERE message_id=? AND path IS NOT NULL", (m["id"],)):
            ap = _path(a["path"])
            if ap.exists():
                data = ap.read_bytes()
                digest = sha256(data)
                db.ex("INSERT INTO records(kind, owner_type, owner_id, sha256, size, location, created_at) VALUES"
                      " (?,?,?,?,?,?,?)", ("attachment_backfill", "message", m["id"], digest, len(data),
                                           a["path"], db.now()))
                db.ex("UPDATE attachments SET sha256=? WHERE id=? AND sha256 IS NULL", (digest, a["id"]))
    if n:
        db.audit("records_backfilled", entity_type="service", entity_id="records", messages=n)
    return n


def verify_all() -> dict:
    bad = []
    total = 0
    for r in db.q("SELECT * FROM records WHERE purged_at IS NULL ORDER BY id"):
        total += 1
        why = check(r)
        if why:
            bad.append({"record": r["id"], "kind": r["kind"], "owner": f"{r['owner_type']} {r['owner_id']}",
                        "why": why})
    return {"ok": not bad, "checked": total, "problems": bad}


def daily(log=print) -> dict | None:
    """Once a day: re-hash every original. Each mismatch is recorded and the admins are told once."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.get_state("records_day") == today:
        return None
    db.set_state("records_day", today)
    result = verify_all()
    for p in result["problems"]:
        db.audit("record_integrity_failed", entity_type="record", entity_id=p["record"], kind=p["kind"],
                 owner=p["owner"], why=p["why"])
    if result["problems"]:
        from . import alerts
        lines = "\n".join(f"- {p['kind']} of {p['owner']}: {p['why']}" for p in result["problems"][:50])
        alerts.notify("records", f"Study monitor: {len(result['problems'])} original(s) failed the nightly check",
                      f"These originals no longer match the fingerprint they had when kept:\n\n{lines}\n\n"
                      "The bot will not read a message whose original fails. Nothing was changed.")
    log(f"originals checked: {result['checked']}, problems: {len(result['problems'])}")
    return result

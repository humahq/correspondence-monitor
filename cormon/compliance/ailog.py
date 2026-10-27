"""Every AI call in full, so any decision can be explained later without re-running it (re-runs differ): the exact
request (prompt, schema or questions), the answer, the model and its settings, whether it came from the cache, the
message it was about with that message's SHA-256, the study model and config version in force.

It holds patient text, so it is a PHI store: kept as long as the message text (data.retention_days) and cleared with
it. The request is compressed (the study documents repeat in most prompts). Certified backend (later): the same
rows in a write-once bucket with its own access list.
"""

from __future__ import annotations

import time
import zlib

from .. import db
from . import backend, sha256

BUILT = ("local",)
_models: dict[str, tuple[float, str | None]] = {}


def study_model(study_id: str | None) -> str | None:
    """A short fingerprint of what the bot checks a study against: its documents' hashes and its rules and guide.
    It changes whenever the study is rebuilt."""
    if not study_id:
        return None
    hit = _models.get(study_id)
    if hit and time.monotonic() - hit[0] < 60:
        return hit[1]
    docs = [tuple(r) for r in db.q("SELECT doc_id, sha256 FROM documents WHERE study_id=? ORDER BY doc_id",
                                   (study_id,))]
    rules = tuple(db.q1("SELECT COUNT(*), MAX(created_at) FROM rules WHERE study_id=? AND active=1", (study_id,)))
    guide = tuple(db.q1("SELECT COUNT(*), GROUP_CONCAT(DISTINCT version) FROM guide WHERE study_id=?", (study_id,)))
    value = sha256(db.js([docs, rules, guide]))[:16] if docs or rules[0] or guide[0] else None
    _models[study_id] = (time.monotonic(), value)
    return value


def record(task: str, api: str, model: str, params: dict, request: dict, response: str | None, *, cached: bool,
           ok: bool = True, error: str | None = None) -> int:
    backend("ailog", BUILT)
    from .. import timing
    run, msg_id = timing.current()
    msg = db.q1("SELECT sha256, study_id FROM messages WHERE id=?", (msg_id,)) if msg_id else None
    study_id = msg["study_id"] if msg else None
    cfg = db.q1("SELECT MAX(id) v FROM config_versions")
    text = db.js(request)
    return db.ex(
        "INSERT INTO ai_log(ts, run_id, message_id, message_sha256, study_id, study_model, config_version, task, api,"
        " model, params, request, request_sha256, response, cached, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (db.now(), run, msg_id, msg["sha256"] if msg else None, study_id, study_model(study_id),
         cfg["v"] if cfg else None, task, api, model, db.js(params), zlib.compress(text.encode(), 6), sha256(text),
         response, int(cached), int(ok), error))


def request_of(row) -> dict:
    """The request a logged call sent, uncompressed."""
    return db.unjs(zlib.decompress(row["request"]).decode())

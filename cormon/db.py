"""SQLite store. The audit log is append-only and enforced by triggers."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    study_id TEXT,
    route TEXT NOT NULL,
    visibility TEXT NOT NULL,
    recipient_alias TEXT,
    from_addr TEXT,
    from_name TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    subject TEXT,
    body_text TEXT,
    body_html TEXT,
    headers TEXT,
    rfc_message_id TEXT,
    in_reply_to TEXT,
    refs TEXT,
    sent_at TEXT,
    received_at TEXT NOT NULL,
    raw_path TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    error TEXT,
    UNIQUE(channel, external_id)
);
CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_key);
CREATE INDEX IF NOT EXISTS ix_messages_status ON messages(status);
CREATE INDEX IF NOT EXISTS ix_messages_rfc ON messages(rfc_message_id);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    filename TEXT,
    mime TEXT,
    size INTEGER,
    path TEXT,
    text TEXT,
    component TEXT NOT NULL,
    source_date TEXT,
    placement TEXT,
    classification TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    thread_key TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    study_id TEXT,
    subject TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    version INTEGER NOT NULL,
    data TEXT NOT NULL,
    model TEXT,
    source TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    study_id TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    first_message_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    pattern_class TEXT,
    rule_id TEXT,
    participant_id TEXT,
    site TEXT,
    polarity TEXT NOT NULL,
    title TEXT NOT NULL,
    tier TEXT NOT NULL,
    tier_source TEXT NOT NULL DEFAULT 'system',
    tier_reason TEXT,
    recipient_person TEXT,
    recipient_source TEXT NOT NULL DEFAULT 'system',
    recipient_reason TEXT,
    send_draft INTEGER NOT NULL DEFAULT 1,
    send_draft_source TEXT NOT NULL DEFAULT 'system',
    counts INTEGER NOT NULL DEFAULT 1,
    counts_source TEXT NOT NULL DEFAULT 'system',
    state TEXT NOT NULL DEFAULT 'open',
    cleared INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL,
    reasoning TEXT,
    link_token TEXT NOT NULL,
    routed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_items_study ON items(study_id, created_at);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    item_id INTEGER,
    message_id INTEGER,
    purpose TEXT NOT NULL,
    channel TEXT NOT NULL,
    destination TEXT NOT NULL,
    template_id TEXT,
    from_addr TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    subject TEXT,
    body TEXT NOT NULL,
    meta TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    external_ref TEXT,
    web_url TEXT,
    rfc_message_id TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON outbox(status);

CREATE TABLE IF NOT EXISTS documents (
    study_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    pages INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (study_id, doc_id)
);

CREATE TABLE IF NOT EXISTS doc_pages (
    study_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (study_id, doc_id, page)
);

CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    doc_sha256 TEXT NOT NULL,
    pattern_class TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    check_type TEXT NOT NULL,
    parameter TEXT,
    operator TEXT,
    value REAL,
    value2 REAL,
    unit TEXT,
    condition TEXT,
    applies_to TEXT,
    source_quote TEXT NOT NULL,
    source_page INTEGER,
    section TEXT,
    verified INTEGER NOT NULL,
    order_idx INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rules_study ON rules(study_id, pattern_class);

-- what the documents allow ("may be repeated on another day", "not a deviation if"), each with a verified quote:
-- shown to the checks so that an allowed case is not reported
CREATE TABLE IF NOT EXISTS allowances (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    text TEXT NOT NULL,
    classes TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    source_page INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_allowances_study ON allowances(study_id);

-- the protocol as a guide the checker reads whole: sections in the protocol's words (tables one line per row)
-- and one checklist per visit from the schedule tables
CREATE TABLE IF NOT EXISTS guide (
    study_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    number TEXT,
    title TEXT NOT NULL,
    path TEXT,
    text TEXT NOT NULL,
    about TEXT,
    topics TEXT,
    ord INTEGER,
    PRIMARY KEY (study_id, id)
);

-- rule pruning: the AI suggests keep or remove, people decide. Keyed on the rule's words and its check, not its
-- id, so a decision survives a rebuild that renumbers the rules; a changed rule comes back undecided (kept).
CREATE TABLE IF NOT EXISTS rule_decisions (
    study_id TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    text TEXT NOT NULL,
    suggest TEXT CHECK (suggest IN ('keep','remove')),
    reason_type TEXT,
    why TEXT,
    decision TEXT CHECK (decision IN ('keep','remove')),
    decided_by TEXT,
    decided_at TEXT,
    note TEXT,
    PRIMARY KEY (study_id, rule_key)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('message','person','study','global')),
    target TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('fact','instruction')),
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patient_facts (
    id INTEGER PRIMARY KEY,
    study_id TEXT NOT NULL,
    participant_key TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT,
    dates TEXT NOT NULL,
    events TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (study_id, participant_key, message_id)
);
CREATE INDEX IF NOT EXISTS patient_facts_who ON patient_facts (study_id, participant_key);

CREATE TABLE IF NOT EXISTS answer_pages (
    id INTEGER PRIMARY KEY,
    study_id TEXT NOT NULL,
    message_id INTEGER,
    link_token TEXT NOT NULL UNIQUE,
    message_text TEXT NOT NULL,
    answers TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_timings (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    seconds REAL,
    steps TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timings_message ON message_timings(message_id);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    run_id INTEGER,
    step TEXT,
    task TEXT NOT NULL,
    model TEXT NOT NULL,
    seconds REAL NOT NULL,
    cached INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    ok INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_run ON llm_calls(run_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    study_id TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log(entity_type, entity_id);

-- every original kept (raw message, attachment, sent message) with its SHA-256 at capture: compliance/records.py
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    location TEXT,
    created_at TEXT NOT NULL,
    purged_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_records_owner ON records(owner_type, owner_id);

-- a chat message edited or deleted after capture: each change a new version; the original row is never changed
CREATE TABLE IF NOT EXISTS message_versions (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    version INTEGER NOT NULL,
    change TEXT NOT NULL CHECK (change IN ('edited','deleted')),
    body_text TEXT,
    body_html TEXT,
    sha256 TEXT NOT NULL,
    raw_path TEXT,
    changed_at TEXT,
    received_at TEXT NOT NULL,
    UNIQUE (message_id, version)
);

-- every AI call in full: compliance/ailog.py
CREATE TABLE IF NOT EXISTS ai_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    run_id INTEGER,
    message_id INTEGER,
    message_sha256 TEXT,
    study_id TEXT,
    study_model TEXT,
    config_version INTEGER,
    task TEXT NOT NULL,
    api TEXT NOT NULL,
    model TEXT NOT NULL,
    params TEXT NOT NULL,
    request BLOB NOT NULL,
    request_sha256 TEXT NOT NULL,
    response TEXT,
    cached INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_log_message ON ai_log(message_id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

_local = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path():
    return settings.data_dir() / "cormon.db"


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.executescript(SCHEMA)
        _migrate(c)
        _local.conn = c
    return c


MIGRATIONS = {
    # why a rule is inactive, or that the review pass added it ("added_by_review", "dropped: signature line")
    ("rules", "review"): "ALTER TABLE rules ADD COLUMN review TEXT",
    # tests that, when all hold, turn the rule off: an exception the document allows
    ("rules", "unless"): "ALTER TABLE rules ADD COLUMN unless TEXT",
    # the source a guide item was read from: document version and file hash; for a pack, its sources' versions
    ("guide", "version"): "ALTER TABLE guide ADD COLUMN version TEXT",
    # the same draft queued twice (a message read again after a crash) is sent once
    ("outbox", "idem_key"): "ALTER TABLE outbox ADD COLUMN idem_key TEXT",
    # a failed send waits longer before each new try
    ("outbox", "next_try_at"): "ALTER TABLE outbox ADD COLUMN next_try_at TEXT",
    # when a sender took the row; a row left 'sending' long after was interrupted
    ("outbox", "claimed_at"): "ALTER TABLE outbox ADD COLUMN claimed_at TEXT",
    ("messages", "processing_at"): "ALTER TABLE messages ADD COLUMN processing_at TEXT",
    # SHA-256 of the original at capture; derived records carry their source's (compliance/records.py)
    ("messages", "sha256"): "ALTER TABLE messages ADD COLUMN sha256 TEXT",
    ("attachments", "sha256"): "ALTER TABLE attachments ADD COLUMN sha256 TEXT",
    ("events", "source_sha256"): "ALTER TABLE events ADD COLUMN source_sha256 TEXT",
    ("outbox", "sha256"): "ALTER TABLE outbox ADD COLUMN sha256 TEXT",
    # the audit hash chain (compliance/trail.py)
    ("audit_log", "prev_hash"): "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
    ("audit_log", "hash"): "ALTER TABLE audit_log ADD COLUMN hash TEXT",
}
INDEXES = ["CREATE INDEX IF NOT EXISTS ix_outbox_idem ON outbox(idem_key)"]


def _migrate(c: sqlite3.Connection) -> None:
    for (table, column), sql in MIGRATIONS.items():
        if column not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(sql)
    for sql in INDEXES:
        c.execute(sql)


def q(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, tuple(params)).fetchall()


def q1(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn().execute(sql, tuple(params)).fetchone()


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    cur = conn().execute(sql, tuple(params))
    return cur.lastrowid


def changed(sql: str, params: Iterable[Any] = ()) -> int:
    """Run an UPDATE and return how many rows it changed (1 means this caller won the row)."""
    return conn().execute(sql, tuple(params)).rowcount


def later(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def unjs(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def audit(
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: Any = None,
    study_id: str | None = None,
    actor: str = "system",
    actor_type: str = "system",
    **details: Any,
) -> int:
    """Append to the audit log (hash-chained: compliance/trail.py) and confirm the row was written. Patient details
    are masked first."""
    from . import phi
    from .compliance import trail
    details = phi.mask_all(details)
    row_id = trail.append({
        "ts": now(), "actor": actor, "actor_type": actor_type, "action": action, "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id), "study_id": study_id, "details": js(details)})
    if q1("SELECT id FROM audit_log WHERE id=?", (row_id,)) is None:
        raise RuntimeError(f"audit write for {action} could not be confirmed")
    return row_id


def get_state(key: str, default: str | None = None) -> str | None:
    row = q1("SELECT value FROM state WHERE key=?", (key,))
    return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    ex("INSERT INTO state(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, value))

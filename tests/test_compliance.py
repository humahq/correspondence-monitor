"""Part 11 groundwork: fingerprints, the hash-chained audit trail, kept chat edits, the AI log, alerts, the watchdog."""

import http.client
import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from cormon import capture, db, settings
from cormon.capture import Inbound
from cormon.compliance import SAFETY_NOTICE, ailog, alerts, backend, records, secrets, trail, vault


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "data_dir", lambda: tmp_path)
    db._local.conn = None
    yield
    db._local.conn = None


def _set(monkeypatch, **conf):
    real = settings.get
    conf = {k.replace("__", "."): v for k, v in conf.items()}
    monkeypatch.setattr(settings, "get", lambda k, d=None: conf[k] if k in conf else real(k, d))


def _chat(ext="spaces/S1/messages/1", text="BP 168/92 before dosing", **kw):
    return Inbound(channel="teams", external_id=ext, thread_ref="t1", from_addr="crc@site.org", from_name="C",
                   subject=None, body_text=text, sent_at="2026-10-26T10:00:00+00:00", study_id="NCT04098666",
                   visibility="channel", **kw)


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ audit trail

def test_audit_trail_links_every_entry_and_verifies():
    for i in range(5):
        db.audit("test_action", entity_type="x", entity_id=i, n=i)
    out = trail.verify()
    assert out["ok"] and out["entries"] == 5
    rows = db.q("SELECT prev_hash, hash FROM audit_log ORDER BY id")
    assert rows[0]["prev_hash"].startswith("seal:")
    assert all(b["prev_hash"] == a["hash"] for a, b in zip(rows, rows[1:]))


def test_audit_trail_catches_a_changed_entry():
    for i in range(3):
        db.audit("test_action", entity_type="x", entity_id=i)
    c = db.conn()
    c.execute("DROP TRIGGER IF EXISTS audit_no_update")
    for t in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'").fetchall():
        c.execute(f"DROP TRIGGER {t['name']}")
    c.execute("UPDATE audit_log SET details='{\"n\":99}' WHERE id=2")
    c.commit()
    out = trail.verify()
    assert not out["ok"] and out["broken_at"] == 2 and "content changed" in out["why"]


def test_audit_trail_anchor_catches_entries_cut_off_the_end():
    for i in range(4):
        db.audit("test_action", entity_type="x", entity_id=i)
    a = trail.anchor()
    assert trail.verify()["ok"] and trail.verify()["anchors"] == 1
    c = db.conn()
    for t in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'").fetchall():
        c.execute(f"DROP TRIGGER {t['name']}")
    c.execute("DELETE FROM audit_log WHERE id=?", (a["id"],))
    c.commit()
    out = trail.verify()
    assert not out["ok"] and out["broken_at"] == a["id"] and "anchor" in out["why"]


def test_entries_written_before_the_chain_are_sealed_into_it():
    c = db.conn()
    c.execute("INSERT INTO audit_log (ts, actor, actor_type, action) VALUES (?,?,?,?)",
              (db.now(), "system", "system", "old_entry"))
    c.commit()
    db.audit("test_action", entity_type="x", entity_id=1)
    assert trail.verify()["ok"]
    for t in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'").fetchall():
        c.execute(f"DROP TRIGGER {t['name']}")
    c.execute("UPDATE audit_log SET action='changed' WHERE action='old_entry'")
    c.commit()
    assert not trail.verify()["ok"]


def test_audit_trail_still_refuses_update_and_delete():
    rid = db.audit("test_action", entity_type="x", entity_id=1)
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE audit_log SET hash='x' WHERE id=?", (rid,))


def test_every_change_to_a_row_keeps_old_and_new_values():
    mid = capture.capture(_chat())
    db.ex("UPDATE messages SET status='processed', body_text='[cleared after retention period]' WHERE id=?", (mid,))
    db.ex("UPDATE messages SET status='processed' WHERE id=?", (mid,))         # no change: nothing kept
    rows = db.q("SELECT changes FROM row_changes WHERE tbl='messages' AND row_id=?", (str(mid),))
    changes = [db.unjs(r["changes"]) for r in rows]
    last = changes[-1]
    assert last["status"] == ["new", "processed"]
    assert last["body_text:chars"] == [len("BP 168/92 before dosing"), len("[cleared after retention period]")]
    assert "BP 168" not in db.js(changes)                                    # patient text never copied
    db.ex("DELETE FROM attachments WHERE message_id=?", (mid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE row_changes SET changes='{}'")
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("DELETE FROM row_changes")


def test_row_changes_are_sealed_into_the_audit_chain():
    mid = capture.capture(_chat())
    db.ex("UPDATE messages SET status='processed' WHERE id=?", (mid,))
    assert trail.seal_changes()["rows"] >= 1 and trail.seal_changes() is None
    assert trail.verify()["ok"]
    c = db.conn()
    c.execute("DROP TRIGGER row_changes_no_update")
    c.execute("UPDATE row_changes SET changes='{\"status\":[\"new\",\"cleared\"]}' WHERE id=1")
    out = trail.verify()
    assert not out["ok"] and "altered" in out["why"]


def test_change_triggers_are_made_again_when_a_table_gains_a_column():
    c = db.conn()
    before = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'chg_items_%'")}
    c.execute("ALTER TABLE items ADD COLUMN extra TEXT")
    trail.track_changes(c)
    after = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'chg_items_%'")}
    assert len(after) == 2 and after != before


def test_daily_trail_check_anchors_once_a_day(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: sent.append(a))
    db.audit("test_action", entity_type="x", entity_id=1)
    assert trail.daily(log=lambda *_: None)["ok"]
    assert trail.daily(log=lambda *_: None) is None
    assert len(trail.anchors()) == 1 and not sent


def test_unbuilt_backend_is_refused_not_silently_replaced(monkeypatch):
    _set(monkeypatch, compliance__trail="worm_bucket")
    with pytest.raises(NotImplementedError):
        backend("trail", trail.BUILT)
    with pytest.raises(NotImplementedError):
        db.audit("test_action", entity_type="x", entity_id=1)


# ------------------------------------------------------------------ originals and fingerprints

def test_every_message_and_attachment_is_fingerprinted_at_capture():
    att = capture.InAttachment(filename="labs.pdf", mime="application/pdf", data=b"%PDF fake")
    mid = capture.capture(_chat(attachments=[att], raw=b'{"name": "m1"}'))
    m = db.q1("SELECT sha256, raw_path FROM messages WHERE id=?", (mid,))
    assert m["sha256"] == records.sha256(b'{"name": "m1"}') and m["raw_path"].endswith(f"{mid}.json")
    a = db.q1("SELECT sha256 FROM attachments WHERE message_id=?", (mid,))
    assert a["sha256"] == records.sha256(b"%PDF fake")
    assert records.check_message(mid) == []
    assert db.q1("SELECT details FROM audit_log WHERE action='message_captured'")["details"].count(m["sha256"])


def test_a_message_with_no_raw_copy_keeps_its_fields():
    mid = capture.capture(_chat())
    m = db.q1("SELECT sha256, raw_path FROM messages WHERE id=?", (mid,))
    assert m["raw_path"].endswith(".fields.json") and m["sha256"]
    assert records.check_message(mid) == []


def test_pipeline_refuses_to_read_a_changed_original(monkeypatch):
    from cormon import pipeline

    mid = capture.capture(_chat(raw=b'{"text": "BP 168/92"}'))
    (settings.data_dir() / db.q1("SELECT raw_path FROM messages WHERE id=?", (mid,))["raw_path"]).write_bytes(
        b'{"text": "BP 120/80"}')
    out = pipeline.process(mid)
    assert out["integrity_failed"] and "changed" in out["integrity_failed"][0]
    m = db.q1("SELECT status, error FROM messages WHERE id=?", (mid,))
    assert m["status"] == "error" and "fingerprint" in m["error"]
    assert db.q1("SELECT 1 FROM audit_log WHERE action='message_integrity_failed'")


def test_a_missing_original_is_caught_by_the_nightly_check(monkeypatch):
    told = []
    monkeypatch.setattr(alerts, "notify", lambda key, subject, body, **k: told.append((key, subject)))
    mid = capture.capture(_chat(raw=b"{}"))
    (settings.data_dir() / db.q1("SELECT raw_path FROM messages WHERE id=?", (mid,))["raw_path"]).unlink()
    out = records.daily(log=lambda *_: None)
    assert not out["ok"] and out["problems"][0]["why"] == "the original is missing"
    assert told and told[0][0] == "records"
    assert db.q1("SELECT 1 FROM audit_log WHERE action='record_integrity_failed'")
    assert records.daily(log=lambda *_: None) is None


def test_purged_originals_are_no_longer_expected_to_match():
    mid = capture.capture(_chat(raw=b"{}"))
    path = db.q1("SELECT raw_path FROM messages WHERE id=?", (mid,))["raw_path"]
    (settings.data_dir() / path).unlink()
    assert records.mark_purged([path]) == 1
    assert records.verify_all()["ok"]


def test_sent_messages_are_fingerprinted_and_a_change_is_caught():
    from cormon import routing

    oid = routing.enqueue(purpose="test", channel="email", destination="email", body="Please check MAP-0161.",
                          subject="Check", to=["cra@site.org"])
    o = db.q1("SELECT sha256 FROM outbox WHERE id=?", (oid,))
    assert o["sha256"] and records.verify_all()["ok"]
    db.ex("UPDATE outbox SET body='Nothing to see.' WHERE id=?", (oid,))
    bad = records.verify_all()["problems"]
    assert bad and bad[0]["owner"] == f"outbox {oid}" and "changed" in bad[0]["why"]


def test_old_messages_are_fingerprinted_only_by_backfill():
    mid = db.ex("INSERT INTO messages (channel, external_id, thread_key, route, visibility, body_text, sent_at,"
                " received_at, status, raw_path) VALUES ('email','old','t','general','direct','x',?,?,'done',?)",
                (db.now(), db.now(), "raw/email/old.eml"))
    (settings.data_dir() / "raw/email").mkdir(parents=True, exist_ok=True)
    (settings.data_dir() / "raw/email/old.eml").write_bytes(b"From: a\r\n\r\nx")
    assert records.check_message(mid) == []                 # nothing kept yet: nothing to fail
    assert records.backfill() == 1 and records.backfill() == 0
    assert db.q1("SELECT kind FROM records WHERE owner_id=?", (mid,))["kind"] == "message_raw_backfill"
    assert db.q1("SELECT sha256 FROM messages WHERE id=?", (mid,))["sha256"] == records.sha256(b"From: a\r\n\r\nx")


# ------------------------------------------------------------------ write-once store

def test_originals_and_audit_entries_get_a_write_once_copy():
    mid = capture.capture(_chat(raw=b'{"text": "BP 168/92"}'))
    assert vault.get("message_raw", f"message:{mid}") == [b'{"text": "BP 168/92"}']
    assert vault.get("audit", "1")
    out = vault.verify()
    assert out["ok"] and out["audit_entries"] == db.q1("SELECT COUNT(*) n FROM audit_log")["n"]


def test_the_write_once_store_refuses_a_change_and_an_early_delete():
    vault.put("message_raw", "message:1", b"original", keep_days=30)
    vault.put("audit", "1", "entry")
    c = vault._conn()
    with pytest.raises(sqlite3.DatabaseError, match="cannot be changed"):
        c.execute("UPDATE objects SET data=x'00' WHERE ref='message:1'")
    with pytest.raises(sqlite3.DatabaseError, match="kept until"):
        c.execute("DELETE FROM objects WHERE ref='message:1'")
    with pytest.raises(sqlite3.DatabaseError, match="kept until"):
        c.execute("DELETE FROM objects WHERE kind='audit'")


def test_the_write_once_store_drops_originals_after_retention_but_keeps_the_audit():
    vault.put("message_raw", "message:1", b"old", keep_days=0)
    vault.put("audit", "1", "entry")
    assert vault.expire() == 1
    assert vault.get("message_raw", "message:1") == [] and vault.get("audit", "1") == [b"entry"]


def test_the_write_once_store_catches_a_changed_audit_entry(monkeypatch):
    told = []
    monkeypatch.setattr(alerts, "notify", lambda key, subject, body, **k: told.append(key))
    for i in range(3):
        db.audit("test_action", entity_type="x", entity_id=i)
    c = db.conn()
    for t in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'").fetchall():
        c.execute(f"DROP TRIGGER {t['name']}")
    c.execute("UPDATE audit_log SET details='{\"n\":99}' WHERE id=2")
    c.commit()
    out = vault.daily(log=lambda *_: None)
    assert out["problems"] == ["audit entry 2 differs from its stored copy"]
    assert told == ["vault"]
    assert vault.daily(log=lambda *_: None) is None


def test_a_failed_copy_does_not_stop_the_bot_and_the_daily_check_makes_it(monkeypatch):
    real = vault._conn
    monkeypatch.setattr(vault, "_conn", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")))
    mid = capture.capture(_chat(raw=b"{}"))
    assert "disk full" in db.get_state("vault_error")
    monkeypatch.setattr(vault, "_conn", real)
    assert not vault.verify()["ok"]
    copied = vault.backfill()
    assert copied["records"] == 1 and copied["audit"] >= 1
    assert vault.get("message_raw", f"message:{mid}") == [b"{}"] and vault.verify()["ok"]


def test_events_carry_the_hash_of_the_message_they_came_from():
    from cormon import events

    mid = capture.capture(_chat(raw=b'{"a": 1}'))
    eid = events.store(mid, {"facts": []})
    assert db.q1("SELECT source_sha256 FROM events WHERE id=?", (eid,))["source_sha256"] == records.sha256(b'{"a": 1}')


# ------------------------------------------------------------------ chat edits and deletions

def test_an_edited_chat_message_is_kept_as_a_new_version():
    mid = capture.capture(_chat(raw=b'{"v": 1}'))
    assert capture.capture(_chat(text="BP 128/82 before dosing", edited_at="2026-10-26T10:05:00Z",
                                 raw=b'{"v": 2}')) is None
    assert capture.capture(_chat(text="BP 128/82 before dosing", edited_at="2026-10-26T10:05:00Z")) is None
    v = db.q("SELECT * FROM message_versions WHERE message_id=?", (mid,))
    assert len(v) == 1 and v[0]["change"] == "edited" and v[0]["body_text"] == "BP 128/82 before dosing"
    assert db.q1("SELECT body_text FROM messages WHERE id=?", (mid,))["body_text"] == "BP 168/92 before dosing"
    assert v[0]["sha256"] == records.sha256(b'{"v": 2}')
    assert db.q1("SELECT 1 FROM audit_log WHERE action='message_edited'")


def test_a_deleted_chat_message_is_kept_and_marked_deleted():
    mid = capture.capture(_chat())
    capture.capture(_chat(text="", deleted_at="2026-10-26T10:09:00Z"))
    capture.capture(_chat(text="", deleted_at="2026-10-26T10:09:00Z"))
    v = db.q("SELECT * FROM message_versions WHERE message_id=?", (mid,))
    assert [r["change"] for r in v] == ["deleted"] and v[0]["body_text"] is None
    assert db.q1("SELECT body_text FROM messages WHERE id=?", (mid,))["body_text"]
    assert db.q1("SELECT 1 FROM audit_log WHERE action='message_deleted'")


def test_a_message_deleted_before_the_bot_read_it_is_not_stored():
    assert capture.capture(_chat(text="", deleted_at="2026-10-26T10:09:00Z")) is None
    assert not db.q1("SELECT 1 FROM messages")


def test_google_chat_passes_edits_and_deletions_to_capture():
    from cormon.channels.gchat import GoogleChatChannel

    ch = GoogleChatChannel.__new__(GoogleChatChannel)
    ch._me = "users/100"
    ch._email_for = lambda user: "crc@site.org"
    base = {"name": "spaces/S1/messages/1", "sender": {"name": "users/7", "type": "HUMAN"},
            "createTime": "2026-10-26T10:00:00Z", "text": "BP 128/82", "thread": {"name": "spaces/S1/threads/T"}}
    edited = ch._to_inbound(dict(base, lastUpdateTime="2026-10-26T10:05:00Z"), study_id="S", visibility="channel")
    assert edited.edited_at == "2026-10-26T10:05:00Z" and not edited.deleted_at
    same = ch._to_inbound(dict(base, lastUpdateTime=base["createTime"]), study_id="S", visibility="channel")
    assert same.edited_at is None
    gone = ch._to_inbound({"name": base["name"], "deleteTime": "2026-10-26T10:09:00Z"}, study_id="S",
                          visibility="channel")
    assert gone.deleted_at == "2026-10-26T10:09:00Z" and gone.external_id == base["name"]


def test_chat_attachments_are_downloaded_once_and_fingerprinted():
    from cormon.channels.gchat import GoogleChatChannel

    got = []

    def get(url, params=None, timeout=None):
        got.append(url)
        return SimpleNamespace(content=b"Week 4 ECG: QTcF 512 ms", raise_for_status=lambda: None)

    ch = GoogleChatChannel(session=SimpleNamespace(get=get))
    ch._me = "users/100"
    ch._email_for = lambda user: "crc@site.org"
    m = {"name": "spaces/S1/messages/7", "sender": {"name": "users/7", "type": "HUMAN"},
         "createTime": "2026-10-26T10:00:00Z", "text": "", "thread": {"name": "spaces/S1/threads/T"},
         "attachment": [{"contentName": "ecg.txt", "contentType": "text/plain",
                         "attachmentDataRef": {"resourceName": "spaces/S1/attachments/A"}},
                        {"contentName": "labs.pdf", "driveDataRef": {"driveFileId": "D"}}]}
    inb = ch._to_inbound(m, study_id="NCT04098666", visibility="channel")
    assert inb and got == ["https://chat.googleapis.com/v1/media/spaces/S1/attachments/A"]
    assert inb.attachments[0].text == "Week 4 ECG: QTcF 512 ms"
    assert inb.attachments[1].component == "attachment-reference" and "Drive" in inb.attachments[1].text
    mid = capture.capture(inb)
    a = db.q1("SELECT sha256 FROM attachments WHERE message_id=? AND filename='ecg.txt'", (mid,))
    assert a["sha256"] == records.sha256(b"Week 4 ECG: QTcF 512 ms") and records.check_message(mid) == []
    ch._to_inbound(m, study_id="NCT04098666", visibility="channel")
    assert len(got) == 1                                                   # seen again: not downloaded again


def test_a_push_note_for_a_message_that_is_gone_marks_it_deleted():
    import requests

    from cormon.channels.gchat import GoogleChatChannel

    def get(url, params=None, timeout=None):
        r = requests.Response()
        r.status_code = 404
        r.raise_for_status = lambda: (_ for _ in ()).throw(requests.HTTPError(response=r))
        return r

    ch = GoogleChatChannel(session=SimpleNamespace(get=get))
    ch._me, ch._places = "users/100", {"spaces/S1": ("channel", "NCT04098666")}
    mid = capture.capture(_chat())
    got = ch.fetch_named(["spaces/S1/messages/1"])
    assert len(got) == 1 and got[0].deleted_at
    capture.capture(got[0])
    assert db.q1("SELECT change FROM message_versions WHERE message_id=?", (mid,))["change"] == "deleted"


def test_teams_passes_edits_and_deletions_to_capture():
    from cormon.channels.teams import TeamsChannel, _touched

    ch = TeamsChannel()
    ch._me = "me"
    ch._email_for = lambda uid: "crc@site.org"
    m = {"id": "1", "messageType": "message", "createdDateTime": "2026-10-26T10:00:00Z",
         "lastEditedDateTime": "2026-10-26T10:05:00Z", "from": {"user": {"id": "u7", "displayName": "C"}},
         "body": {"content": "<p>BP 128/82</p>"}}
    kw = dict(study_id="S", visibility="channel", thread_ref="c:1", reply_to=None, ext_prefix="t:c")
    assert ch._to_inbound(m, **kw).edited_at == "2026-10-26T10:05:00Z"
    gone = ch._to_inbound(dict(m, deletedDateTime="2026-10-26T10:09:00Z", **{"from": None}), **kw)
    assert gone.deleted_at and gone.external_id == "t:c:1"
    assert _touched(m) == datetime(2026, 10, 26, 10, 5, tzinfo=timezone.utc)


# ------------------------------------------------------------------ AI log

class _Resp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]
        self.usage = None


def test_every_ai_call_is_logged_in_full(monkeypatch):
    from cormon import llm, timing

    calls = []

    def create(**kw):
        calls.append(kw)
        return _Resp('{"ok": true}')

    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    mid = capture.capture(_chat(raw=b"{}"))
    timing.start(mid)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    assert llm.structured("extract", "SYS", "USER TEXT", schema, "s") == {"ok": True}
    assert llm.structured("extract", "SYS", "USER TEXT", schema, "s") == {"ok": True}
    timing.finish("done")
    rows = db.q("SELECT * FROM ai_log ORDER BY id")
    assert [r["cached"] for r in rows] == [0, 1] and len(calls) == 1
    r = rows[0]
    assert r["message_id"] == mid and r["message_sha256"] == db.q1("SELECT sha256 FROM messages")["sha256"]
    assert r["study_id"] == "NCT04098666" and r["task"] == "extract" and r["ok"] == 1
    req = ailog.request_of(r)
    assert req["system"] == "SYS" and req["user"] == "USER TEXT" and req["schema"] == schema
    assert r["request_sha256"] == records.sha256(db.js(req)) and r["response"] == '{"ok": true}'


def test_a_failed_ai_call_is_logged_too(monkeypatch):
    from cormon import llm

    def create(**kw):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        llm.structured("extract", "S", "U", {"type": "object"}, "s", use_cache=False)
    r = db.q1("SELECT * FROM ai_log")
    assert r["ok"] == 0 and "rate limited" in r["error"] and r["response"] is None


def test_a_failing_model_falls_back_once_and_tells_the_admins(monkeypatch):
    from cormon import llm

    used = []

    def create(**kw):
        used.append(kw["model"])
        if kw["model"] == "old-model":
            raise RuntimeError("model_not_found")
        return _Resp('{"ok": true}')

    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    sent = _no_send(monkeypatch)
    _set(monkeypatch, llm__models__extract="old-model", llm__fallback="gpt-4.1")
    assert llm.structured("extract", "S", "U", {"type": "object"}, "s", use_cache=False) == {"ok": True}
    assert used[-1] == "gpt-4.1" and set(used[:-1]) == {"old-model"}
    assert [r["ok"] for r in db.q("SELECT ok FROM ai_log ORDER BY id")] == [0, 1]
    a = db.q1("SELECT * FROM audit_log WHERE action='llm_fallback'")
    assert '"used": "gpt-4.1"' in a["details"] and len(sent) == 1


def test_no_fallback_when_none_is_set(monkeypatch):
    from cormon import llm

    def create(**kw):
        raise RuntimeError("model_not_found")

    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    _set(monkeypatch, llm__fallback=None)
    with pytest.raises(RuntimeError):
        llm.structured("extract", "S", "U", {"type": "object"}, "s", use_cache=False)


def test_weekly_model_check_warns_about_a_retired_model(monkeypatch):
    from cormon import llm

    sent = _no_send(monkeypatch)
    _set(monkeypatch, llm__models={"default": "gpt-4.1", "ingest": "old-model"}, llm__fallback=None)
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(models=SimpleNamespace(
        list=lambda: [SimpleNamespace(id="gpt-4.1")])))
    assert llm.check_models(lambda *a: None) == ["old-model"] and len(sent) == 1
    assert llm.check_models(lambda *a: None) is None


def test_dependency_scan_writes_the_list_and_records_problems(monkeypatch):
    from cormon.compliance import deps

    monkeypatch.setattr(deps, "packages", lambda: [{"name": "pypdf", "version": "1.0"},
                                                   {"name": "requests", "version": "9.9"}])

    class Http:
        def post(self, url, json, timeout):
            assert [q["package"]["name"] for q in json["queries"]] == ["pypdf", "requests"]
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"results": [{"vulns": [{"id": "GHSA-1"}]}, {}]})

    res = deps.scan(Http())
    assert res["packages"] == 2 and res["problems"] == [{"name": "pypdf", "version": "1.0", "ids": ["GHSA-1"]}]
    assert "known_problems" in (db.settings.data_dir() / "sbom.json").read_text()
    assert "pypdf 1.0: GHSA-1" in db.q1("SELECT details FROM audit_log WHERE action='dependency_scan'")["details"]


def test_decisions_calls_are_logged(monkeypatch):
    from cormon import llm

    ans = SimpleNamespace(answers=[SimpleNamespace(type="predicate", name="q1", probability=0.9)])
    monkeypatch.setattr(llm, "client", lambda: SimpleNamespace(decisions=SimpleNamespace(create=lambda **k: ans)))
    assert llm.decide("doctor", "text", [{"name": "q1"}]) == {"q1": 0.9}
    llm.decide("doctor", "text", [{"name": "q1"}])
    rows = db.q("SELECT api, cached, response FROM ai_log ORDER BY id")
    assert [(r["api"], r["cached"]) for r in rows] == [("decisions", 0), ("decisions", 1)]


def test_ai_log_is_purged_with_the_message_text():
    from cormon import phi

    db.ex("INSERT INTO ai_log(ts, task, api, model, params, request, request_sha256, cached, ok) VALUES"
          " (?,?,?,?,?,?,?,?,?)", ("2000-01-01T00:00:00+00:00", "t", "a", "m", "{}", b"x", "h", 0, 1))
    phi.purge()
    assert not db.q1("SELECT 1 FROM ai_log")


# ------------------------------------------------------------------ secrets

def test_secrets_come_from_the_environment_first(monkeypatch):
    monkeypatch.setenv("CORMON_TEST_SECRET", "from-env")
    monkeypatch.setattr(secrets, "_from_vm", lambda *a: pytest.fail("asked Secret Manager"))
    assert secrets.get(env="CORMON_TEST_SECRET", secret={"name": "x"}) == "from-env"
    monkeypatch.delenv("CORMON_TEST_SECRET")
    assert secrets.get(env="CORMON_TEST_SECRET") is None


def test_secrets_fall_back_to_secret_manager(monkeypatch):
    monkeypatch.setattr(secrets, "_from_vm", lambda secret, what: "from-vm")
    assert secrets.get(env="CORMON_NOT_SET_ANYWHERE", secret={"name": "x"}) == "from-vm"
    monkeypatch.setattr(secrets, "_from_vm", lambda secret, what: None)
    monkeypatch.setattr(secrets.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="from-gcloud\n", stderr=""))
    assert secrets.get(secret={"name": "x"}) == "from-gcloud"
    monkeypatch.setattr(secrets.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="denied"))
    with pytest.raises(RuntimeError, match="denied"):
        secrets.get(secret={"name": "x"})


# ------------------------------------------------------------------ alerts and the watchdog

def _no_send(monkeypatch):
    from cormon import outbox

    sent = []
    monkeypatch.setattr(outbox, "dispatch", lambda only_id=None, **k: sent.append(only_id))
    return sent


def test_alerts_are_sent_once_an_hour_and_once_when_cleared(monkeypatch):
    sent = _no_send(monkeypatch)
    _set(monkeypatch, system__ops_emails=["john@huma.ai", "peter@huma.ai"])
    first = alerts.notify("stalled", "Stopped", "body")
    assert first and sent == [first]
    assert alerts.notify("stalled", "Stopped", "body") is None
    o = db.q1("SELECT to_addrs, purpose, sha256 FROM outbox WHERE id=?", (first,))
    assert "peter@huma.ai" in o["to_addrs"] and o["purpose"] == "ops_alert" and o["sha256"]
    assert alerts.clear("stalled", "Running", "body") and len(sent) == 2
    assert alerts.clear("stalled", "Running", "body") is None


def test_alert_with_nobody_to_tell_is_recorded(monkeypatch):
    _no_send(monkeypatch)
    _set(monkeypatch, system__ops_emails=[], web__admin__emails=[])
    assert alerts.notify("x", "s", "b") is None
    assert db.q1("SELECT 1 FROM audit_log WHERE action='ops_alert_no_recipients'")


def test_watchdog_emails_when_the_bot_stops_and_when_it_is_back(monkeypatch):
    from cormon import watchdog

    sent = _no_send(monkeypatch)
    _set(monkeypatch, system__ops_emails=["john@huma.ai"])
    db.set_state("heartbeat", _ago(3))
    assert not watchdog.check(log=lambda *_: None)["stalled"] and not sent
    db.set_state("heartbeat", _ago(20))
    out = watchdog.check(log=lambda *_: None)
    assert out["stalled"] and out["alerted"]
    assert "stopped" in db.q1("SELECT subject FROM outbox WHERE id=?", (out["alerted"],))["subject"]
    db.set_state("heartbeat", _ago(1))
    back = watchdog.check(log=lambda *_: None)
    assert back["recovered"] and "running again" in db.q1("SELECT subject FROM outbox WHERE id=?",
                                                         (back["recovered"],))["subject"]


def test_hourly_mailbox_check_reads_an_email_missed_twice(monkeypatch):
    from cormon import channels, stuck

    raw = (b"From: crc@site.org\r\nTo: bot+NCT04098666@huma.ai\r\nSubject: labs\r\nMessage-ID: <missed@site.org>\r\n"
           b"Date: Mon, 26 Oct 2026 09:00:00 +0000\r\n\r\nALT 3x ULN\r\n")
    told = []
    monkeypatch.setattr(stuck.settings, "get", lambda k, d=None: d)
    from cormon.compliance import alerts as al
    monkeypatch.setattr(al, "notify", lambda key, subject, body, **k: told.append(body))
    fake = SimpleNamespace(
        reconcile=lambda days: {"checked": 3, "no_id": 0, "missing": [
            {"rfc": "<missed@site.org>", "from": "crc@site.org", "sent": "2026-10-26T09:00:00+00:00",
             "folder": "INBOX", "uid": "9"}]},
        take=lambda missing: [capture.parse_email(raw)])
    monkeypatch.setattr(channels, "email_channel", lambda: fake)
    stuck.reconcile_mailbox(log=lambda *_: None)
    assert not told and not db.q1("SELECT 1 FROM messages")             # first time: may just be arriving
    db.set_state("mailbox_check_hour", "")
    stuck.reconcile_mailbox(log=lambda *_: None)
    assert db.q1("SELECT 1 FROM messages WHERE rfc_message_id='<missed@site.org>'")
    assert told and "read now" in told[0]


def test_a_bounce_is_kept_noted_on_the_sent_email_and_the_admins_told(monkeypatch):
    from cormon import routing
    from cormon.channels import imap

    told = []
    from cormon.compliance import alerts as al
    monkeypatch.setattr(al, "notify", lambda key, subject, body, **k: told.append((key, body)))
    oid = routing.enqueue(purpose="alert", channel="email", destination="email", body="Please check.",
                          subject="Alert", to=["cra@nowhere.org"])
    db.ex("UPDATE outbox SET rfc_message_id='<sent1@huma.ai>', status='sent' WHERE id=?", (oid,))
    raw = (b"From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\nTo: bot+NCT04098666@huma.ai\r\n"
           b"Subject: Delivery Status Notification (Failure)\r\nMessage-ID: <b1@google.com>\r\n"
           b"Date: Mon, 26 Oct 2026 09:00:00 +0000\r\n\r\nFinal-Recipient: rfc822; cra@nowhere.org\r\n"
           b"Status: 5.1.1\r\nDiagnostic-Code: smtp; 550 5.1.1 The email account does not exist\r\n\r\n"
           b"Message-ID: <sent1@huma.ai>\r\n")
    assert imap.is_bounce(capture.parse_email(raw))
    assert imap.outbox_bounced(raw, {"<sent1@huma.ai>"}) == oid
    a = db.q1("SELECT details FROM audit_log WHERE action='send_bounced'")
    assert "does not exist" in a["details"] and told and told[0][0] == f"bounce:{oid}"
    assert db.q1("SELECT 1 FROM records WHERE kind='bounce' AND owner_id=?", (oid,))
    assert db.get_state("bounce_seen:<b1@google.com>") and not db.q1("SELECT 1 FROM messages")


def test_health_page_says_stalled_when_the_heartbeat_is_old():
    from cormon import webview

    server = ThreadingHTTPServer(("127.0.0.1", 0), webview.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def get():
        c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        c.request("GET", "/health")
        r = c.getresponse()
        return r.status, r.read().decode()

    try:
        db.set_state("heartbeat", _ago(1))
        assert get() == (200, "ok")
        db.set_state("heartbeat", _ago(30))
        status, body = get()
        assert status == 503 and "stalled" in body
    finally:
        server.shutdown()


def test_stuck_check_reports_a_waiting_message_and_an_unread_channel():
    from cormon import stuck

    db.set_state("stuck_since", _ago(120))
    mid = capture.capture(_chat())
    db.ex("UPDATE messages SET received_at=? WHERE id=?", (_ago(30), mid))
    db.set_state("polled:email", _ago(40))
    db.set_state("polled:teams", _ago(1))
    keys = [p["key"] for p in stuck.problems()]
    assert f"waiting:{mid}" in keys and any(k.startswith("unread:email:") for k in keys)
    assert not any(k.startswith("unread:teams") for k in keys)
    db.set_state("paused", "1")
    assert f"waiting:{mid}" not in [p["key"] for p in stuck.problems()]


# ------------------------------------------------------------------ the safety sentence

def test_digest_lists_downtime_and_late_messages(monkeypatch):
    from cormon import digest, watchdog

    _no_send(monkeypatch)
    _set(monkeypatch, system__ops_emails=["john@huma.ai"])
    db.set_state("heartbeat", _ago(40))
    watchdog.check(log=lambda *_: None)
    db.set_state("heartbeat", _ago(0))
    watchdog.check(log=lambda *_: None)
    late = capture.capture(_chat(ext="spaces/S1/messages/late"))
    db.ex("UPDATE messages SET sent_at=? WHERE id=?", (_ago(300), late))
    waited = capture.capture(_chat(ext="spaces/S1/messages/wait"))
    db.ex("INSERT INTO message_timings(message_id, started_at, status) VALUES (?,?,?)",
          (waited, (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds"), "done"))
    now = datetime.now(timezone.utc)
    out = digest.build(now - timedelta(days=1), now + timedelta(hours=1))
    notes = " ".join(out["notes"])
    assert "was not running from" in notes
    assert f"Message {late} " in notes and "reached the bot late" in notes
    assert f"Message {waited} " in notes and "waited 30 minutes" in notes
    assert out["footer"].startswith("Gaps and late messages") and SAFETY_NOTICE in out["footer"]


def test_sender_not_on_the_people_list_is_flagged(monkeypatch):
    from cormon import config, digest, senders

    study = config.study("NCT04098666")
    known = next(p.email for p in study.people if p.email)
    assert senders.flags({"channel": "teams", "from_addr": known}, study) == []
    assert "not on the study's people list" in senders.flags({"channel": "teams", "from_addr": "x@else.org"}, study)[0]
    forged = {"channel": "email", "from_addr": known, "headers": {"_cormon_sender_check": {"result": "fail"}}}
    assert "sender check failed" in senders.flags(forged, study)[0]


def test_each_question_is_hashed_and_an_unanswered_one_is_its_own_record(monkeypatch):
    from cormon import answers

    found = {"question": "What is the Week 4 window?", "found": True, "text": "Day 28 ± 3."}
    missed = {"question": "What is the wifi password?", "found": False, "text": "Not in the documents."}
    monkeypatch.setattr(answers, "answer_message", lambda *a, **k: [found, missed])
    monkeypatch.setattr(answers, "save_page", lambda *a, **k: "tok")
    monkeypatch.setattr(answers, "render", lambda *a, **k: "reply")
    monkeypatch.setattr(answers, "_post", lambda *a, **k: 5)
    monkeypatch.setattr(answers, "cra_names", lambda s: [])
    answers.reply(SimpleNamespace(study_id="S1"), {"id": 9, "channel": "teams"}, "x@site.org")
    a = db.unjs(db.q1("SELECT details FROM audit_log WHERE action='question_answered'")["details"])
    assert a["question_sha256"][0] == records.sha256("what is the week 4 window?")
    miss = db.q("SELECT details FROM audit_log WHERE action='question_not_answered'")
    assert len(miss) == 1 and "wifi" in miss[0]["details"]


def _item(iid, state="open", routed=1, kind="finding"):
    db.ex("INSERT INTO items (id, study_id, thread_key, first_message_id, last_message_id, dedup_key, kind,"
          " polarity, title, tier, evidence, link_token, created_at, updated_at, state, routed)"
          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (iid, "S1", "t", 1, 1, f"k{iid}", kind, "problem", "x", "medium", "{}", f"l{iid}", db.now(), db.now(),
           state, routed))


def test_steps_must_happen_in_order():
    from cormon import items

    _item(1, routed=0)
    with pytest.raises(items.StepOrder, match="never sent"):
        items.set_state(1, "cleared", "cra@site.org", "cra", study_id="S1")
    items.set_state(1, "cleared", "ops", "operator", study_id="S1", code="fixed")   # the operator may
    with pytest.raises(items.StepOrder, match="already cleared"):
        items.set_state(1, "cleared", "ops", "operator", study_id="S1", code="fixed")
    _item(2, state="out_of_scope")
    for state in ("cleared", "suppressed"):
        with pytest.raises(items.StepOrder, match="safety item"):
            items.set_state(2, state, "ops", "operator", study_id="S1", code="fixed")
    assert items.get(2)["state"] == "out_of_scope"
    assert db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='item_change_refused_order'")["n"] == 4


def test_a_reply_cannot_clear_a_safety_item(monkeypatch):
    from cormon import items, pipeline

    _item(3, state="out_of_scope")
    said = []
    monkeypatch.setattr(pipeline, "_answer_in_chat", lambda study, msg, text: said.append(text))
    person = SimpleNamespace(name="Ann", email="cra@site.org", roles=["cra"])
    study = SimpleNamespace(study_id="S1", person_by_email=lambda a: person)
    msg = {"id": 1, "channel": "teams", "study_id": "S1", "from_addr": "cra@site.org", "headers": {}}
    assert pipeline.handle_instruction(study, msg, {"type": "disregard", "text": "ignore #3"}) is False
    assert items.get(3)["state"] == "out_of_scope" and items.get(3)["counts"] == 1
    assert "safety item" in said[0]


def test_every_digest_says_silence_is_not_clearance():
    from cormon import digest

    now = datetime.now(timezone.utc)
    assert SAFETY_NOTICE in digest.build(now - timedelta(days=1), now)["footer"]
    assert "Silence does not constitute clearance." in SAFETY_NOTICE


# ------------------------------------------------------------------ reason codes (DSP-04)

def test_clearing_needs_a_reason_code_from_the_fixed_list():
    from cormon import items

    _item(4)
    with pytest.raises(items.NoReasonCode, match="reason code"):
        items.set_state(4, "cleared", "ops", "operator", study_id="S1")
    with pytest.raises(items.NoReasonCode, match="reason code"):
        items.set_state(4, "cleared", "ops", "operator", study_id="S1", code="because")
    with pytest.raises(items.NoReasonCode, match="written out"):
        items.set_state(4, "suppressed", "ops", "operator", study_id="S1", code="other")
    assert items.get(4)["state"] == "open"
    items.set_state(4, "suppressed", "ops", "operator", "site re-sent the right form", study_id="S1", code="other")
    assert items.get(4)["reason_code"] == "other"
    a = db.unjs(db.q1("SELECT details FROM audit_log WHERE action='item_suppressed'")["details"])
    assert a["code"] == "other" and a["code_label"].startswith("Other")
    items.set_state(4, "open", "ops", "operator", study_id="S1")                     # reopening needs none
    assert items.get(4)["reason_code"] is None


def test_a_reply_that_clears_is_recorded_with_its_own_code(monkeypatch):
    from cormon import items, pipeline

    _item(5)
    monkeypatch.setattr(pipeline, "_answer_in_chat", lambda *a: None)
    person = SimpleNamespace(name="Ann", email="cra@site.org", roles=["cra"])
    study = SimpleNamespace(study_id="S1", person_by_email=lambda a: person)
    msg = {"id": 1, "channel": "teams", "study_id": "S1", "from_addr": "cra@site.org", "headers": {}}
    assert pipeline.handle_instruction(study, msg, {"type": "cleared", "text": "cleared #5"})
    assert items.get(5)["state"] == "cleared" and items.get(5)["reason_code"] == "by_reply"


# ------------------------------------------------------------------ two people (SEC-04, DSP-05)

def test_two_person_rule_one_asks_another_approves_once():
    from cormon.compliance import approvals

    said = []
    assert approvals.gate("purge", "days=30", "ann", "old text", None, say=said.append) is False
    rid = db.q1("SELECT id FROM approvals")["id"]
    assert f"approve {rid}" in said[0]
    with pytest.raises(approvals.Refused, match="not approved yet"):
        approvals.use(rid, "purge", "days=30", "ann")
    with pytest.raises(approvals.Refused, match="different person"):
        approvals.approve(rid, "Ann")
    approvals.approve(rid, "bob")
    with pytest.raises(approvals.Refused, match="not purge days=7"):
        approvals.use(rid, "purge", "days=7", "ann")
    with pytest.raises(approvals.Refused, match="may not also run it"):
        approvals.use(rid, "purge", "days=30", "bob")
    assert approvals.gate("purge", "days=30", "ann", None, rid) is True
    with pytest.raises(approvals.Refused, match="already used"):
        approvals.use(rid, "purge", "days=30", "ann")
    acts = [r["action"] for r in db.q("SELECT action FROM audit_log WHERE entity_type='approval' ORDER BY id")]
    assert acts.count("approval_refused") == 5 and "approval_given" in acts and "approval_used" in acts


def test_an_old_approval_has_expired():
    from cormon.compliance import approvals

    rid = approvals.request("reset", "db", "ann", None)
    db.ex("UPDATE approvals SET requested_at=? WHERE id=?", (_ago(25 * 60), rid))
    with pytest.raises(approvals.Refused, match="older than"):
        approvals.approve(rid, "bob")


def test_disregarding_an_alert_the_sponsor_sees_needs_a_second_person(capsys):
    from cormon import cli, items

    _item(6)
    cli.main(["--as", "ann", "suppress", "6", "--code", "misread", "--reason", "not a deviation"])
    assert items.get(6)["state"] == "open" and "Request #1" in capsys.readouterr().out
    cli.main(["--as", "bob", "approve", "1"])
    cli.main(["--as", "ann", "suppress", "6", "--code", "misread", "--reason", "not a deviation", "--approval", "1"])
    assert items.get(6)["state"] == "suppressed" and items.get(6)["reason_code"] == "misread"
    _item(7)
    cli.main(["--as", "ann", "suppress", "7", "--reason", "x"])                     # no code: nothing asked
    assert "give a reason code" in capsys.readouterr().out and not db.q1("SELECT 1 FROM approvals WHERE id=2")


def test_two_person_rule_can_be_turned_off_for_a_practice_copy(monkeypatch):
    from cormon.compliance import approvals

    _set(monkeypatch, compliance__two_person=False)
    assert approvals.gate("reset", "db", "ann", None, None) is True and not db.q1("SELECT 1 FROM approvals")


# ------------------------------------------------------------------ hourly send cap (OUT-05)

def test_a_study_sends_at_most_its_hourly_number(monkeypatch):
    from cormon import outbox, routing

    _set(monkeypatch, outbox__hourly_cap_per_study=2)
    told = []
    from cormon.compliance import alerts as al
    monkeypatch.setattr(al, "notify", lambda key, subject, body, **k: told.append(key))
    went = []
    monkeypatch.setattr(outbox, "_adapter", lambda ch: SimpleNamespace(send=lambda r: went.append(r["id"]) or {}))
    ids = [routing.enqueue(purpose="alert", channel="email", destination="email", body=f"b{i}", subject="s",
                           to=["cra@site.org"], study_id="S1") for i in range(3)]
    other = routing.enqueue(purpose="alert", channel="email", destination="email", body="b", subject="s",
                            to=["cra@site.org"], study_id="S2")
    health = routing.enqueue(purpose="ops_alert", channel="email", destination="email", body="down", subject="s",
                             to=["john@huma.ai"], study_id="S1")
    outbox.dispatch()
    assert went == ids[:2] + [other, health]
    row = db.q1("SELECT status, next_try_at FROM outbox WHERE id=?", (ids[2],))
    assert row["status"] == "queued" and row["next_try_at"] > db.now()
    assert told == ["send_cap:S1"] and db.q1("SELECT 1 FROM audit_log WHERE action='send_capped'")
    assert outbox.dispatch(only_id=ids[2])["sent"] == 1                       # a person sending one on purpose may


# ------------------------------------------------------------------ recall and correction (OUT-07)

def _sent_email():
    from cormon import routing

    oid = routing.enqueue(purpose="email_cra", channel="email", destination="email", body="Visit 3 was late.",
                          subject="Re: Visit 3", to=["cra@site.org"], cc=["lead@sponsor.org"], item_id=9,
                          meta={"thread_key": "email:T1", "gmail_thread": "T1", "references": ["<a@site.org>"]},
                          study_id="S1")
    db.ex("UPDATE outbox SET status='sent', sent_at=?, rfc_message_id='<o1@huma.ai>' WHERE id=?", (db.now(), oid))
    return oid


def test_a_sent_email_is_recalled_in_its_own_thread():
    from cormon import corrections

    oid = _sent_email()
    res = corrections.follow_up(oid, "ann", "wrong participant")
    new = db.q1("SELECT * FROM outbox WHERE id=?", (res["outbox_id"],))
    meta = db.unjs(new["meta"])
    assert new["purpose"] == "recall" and new["subject"] == "Please disregard: Visit 3"
    assert db.unjs(new["to_addrs"]) == ["cra@site.org"] and db.unjs(new["cc_addrs"]) == ["lead@sponsor.org"]
    assert meta["in_reply_to"] == "<o1@huma.ai>" and meta["gmail_thread"] == "T1" and meta["corrects"] == oid
    assert "disregard" in new["body"] and "wrong participant" in new["body"] and new["study_id"] == "S1"
    assert db.q1("SELECT status FROM outbox WHERE id=?", (oid,))["status"] == "sent"      # the original is kept
    a = db.unjs(db.q1("SELECT details FROM audit_log WHERE action='sent_message_recalled'")["details"])
    assert a["outbox_id"] == oid and a["follow_up_outbox_id"] == res["outbox_id"]


def test_a_correction_carries_the_right_words():
    from cormon import corrections

    oid = _sent_email()
    res = corrections.follow_up(oid, "ann", "the visit was 2 days late, not 5", text="Visit 3 was 2 days late.")
    new = db.q1("SELECT * FROM outbox WHERE id=?", (res["outbox_id"],))
    assert new["purpose"] == "correction" and new["subject"] == "Correction: Visit 3"
    assert "Visit 3 was 2 days late." in new["body"]


def test_a_message_not_yet_sent_is_held_instead():
    from cormon import corrections, routing

    oid = routing.enqueue(purpose="teams_study_team", channel="teams", destination="teams_alert_channel", body="b",
                          meta={"target": {"space": "spaces/A", "thread": "spaces/A/threads/t"}})
    res = corrections.follow_up(oid, "ann", "sent by mistake")
    assert res == {"held": True, "outbox_id": None}
    assert db.q1("SELECT status FROM outbox WHERE id=?", (oid,))["status"] == "held"
    with pytest.raises(corrections.CannotCorrect, match="reason"):
        corrections.follow_up(oid, "ann", " ")


def test_a_chat_correction_goes_to_the_same_thread():
    from cormon import corrections, routing

    oid = routing.enqueue(purpose="teams_study_team", channel="teams", destination="teams_alert_channel", body="b",
                          meta={"target": {"space": "spaces/A", "thread": "spaces/A/threads/t"}})
    db.ex("UPDATE outbox SET status='sent', sent_at=? WHERE id=?", (db.now(), oid))
    res = corrections.follow_up(oid, "ann", "typo", text="Visit 3, not Visit 4.")
    meta = db.unjs(db.q1("SELECT meta FROM outbox WHERE id=?", (res["outbox_id"],))["meta"])
    assert meta["target"] == {"space": "spaces/A", "thread": "spaces/A/threads/t"}


# ------------------------------------------------------------------ output check (OUT-04, QAA-06)

def _judge(monkeypatch, **scores):
    """llm.decide for the output check: the given scores (0 for any other question); the inputs it was asked."""
    from cormon import llm

    asked = []
    monkeypatch.setattr(llm, "decide", lambda task, inp, qs, use_cache=True: asked.append(inp[1]["content"])
                        or {q["name"]: scores.get(q["name"], 0.0) for q in qs})
    return asked


def test_output_check_finds_added_facts_exactly(monkeypatch):
    from cormon import outcheck

    asked = _judge(monkeypatch)
    src = "Participant 104-002 had eGFR 52 at Visit 3. See https://x.org/a"
    assert outcheck.problems("Participant 104-002 had eGFR 52 at Visit 3. See https://x.org/a", src) == []
    assert any("numbers" in p and "48" in p for p in outcheck.added_facts("eGFR 52, down from 48.", src))
    assert any("ID" in p for p in outcheck.added_facts("Also 104-009.", src))
    assert any("link" in p for p in outcheck.added_facts("See https://evil.org", src))
    assert outcheck.problems("eGFR 52, down from 48.", src) and len(asked) == 1   # a new fact: the AI is not asked


def test_output_check_asks_the_ai_about_judgments_by_meaning(monkeypatch):
    from cormon import llm, outcheck

    asked = _judge(monkeypatch, opinion=0.93)
    assert outcheck.added_judgment("This is clinically significant.", "eGFR 52.") == ["a clinical judgment (0.93)"]
    assert "SOURCE:\neGFR 52." in asked[0] and "REWRITTEN:\nThis is clinically significant." in asked[0]
    _judge(monkeypatch, care=0.88)
    assert outcheck.added_judgment("Hold the dose.", "eGFR 52.") == ["an instruction about care (0.88)"]
    _judge(monkeypatch, opinion=0.2, care=0.1)
    assert outcheck.added_judgment("They must discontinue study drug.", "Must discontinue study drug.") == []

    def down(*a, **k):
        raise RuntimeError("Decisions call failed")
    monkeypatch.setattr(llm, "decide", down)
    assert "could not run" in outcheck.added_judgment("Fine.", "Fine.")[0]   # no check: the checked words are kept


def test_an_ai_edit_that_adds_advice_is_not_used(monkeypatch):
    from cormon import drafting, llm

    body = 'Visit 3 for 104-002 was late. Site wrote: "seen on day 30". Details: https://b/items/x (#5)'
    _judge(monkeypatch, care=0.97)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"body": body + " You should hold the study drug."})
    ctx = {"finding": {"message_quote": "seen on day 30", "doc_quote": ""}, "item": {"link": "https://b/items/x", "id": 5}}
    assert drafting.ai_edit(body, ctx) == body
    assert db.q1("SELECT 1 FROM audit_log WHERE action='draft_edit_refused'")
    _judge(monkeypatch)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"body": body.replace("Details", "See")})
    assert drafting.ai_edit(body, ctx) == body.replace("Details", "See")


def test_an_answer_reworded_into_a_judgment_falls_back_to_the_checked_words(monkeypatch):
    from cormon import answers, llm

    a = {"question": "When is the Week 4 visit?", "text": "Day 28, give or take 3 days.", "sums": [],
         "quotes": [("Schedule", "Week 4 / Day 28 ± 3")], "found": True, "traced": True}
    _judge(monkeypatch, opinion=0.91)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"text": "Day 28 ± 3. It is safe to dose then."})
    assert answers._plain_said(a, "CRC", False) == a["text"] and a["output_check"]
    _judge(monkeypatch)
    monkeypatch.setattr(llm, "structured", lambda *a, **k: {"text": "Day 28, within 3 days either side."})
    assert answers._plain_said(dict(a), "CRC", False) == "Day 28, within 3 days either side."


# ------------------------------------------------------------------ notes and config approval (DSP-06, CFG-01)

def test_a_note_applies_only_once_another_person_approves_it():
    from cormon import notes

    nid = notes.add("study", "S1", "fact", "Site 104 uses paper diaries", "ann")
    assert notes.applicable(None, "S1", []) == [] and notes.status(notes.get(nid)) == "waiting for approval"
    with pytest.raises(notes.NoteRefused, match="different person"):
        notes.approve(nid, "ann")
    notes.approve(nid, "bob")
    assert [n["id"] for n in notes.applicable(None, "S1", [])] == [nid]


def test_a_note_starts_on_its_date_and_a_new_version_replaces_the_old():
    from cormon import notes

    first = notes.add("study", "S1", "fact", "v1", "ann")
    notes.approve(first, "bob")
    later = notes.edit(first, "v2", "ann", effective_from="2999-01-01T00:00:00+00:00")
    notes.approve(later, "bob")
    assert [n["text"] for n in notes.applicable(None, "S1", [])] == ["v1"]      # v2 has not started
    assert notes.status(notes.get(later)).startswith("starts")
    now = notes.edit(first, "v3", "ann")
    assert [n["text"] for n in notes.applicable(None, "S1", [])] == ["v1"]      # v3 not approved yet
    notes.approve(now, "bob")
    assert [n["text"] for n in notes.applicable(None, "S1", [])] == ["v3"]
    hist = notes.history(first)
    assert [h["text"] for h in hist] == ["v1", "v2", "v3"] and hist[0]["status"] == "replaced"


def test_notes_apply_at_once_when_approval_is_off(monkeypatch):
    from cormon import notes

    _set(monkeypatch, notes__need_approval=False)
    nid = notes.add("global", None, "instruction", "Use UK dates", "ann")
    assert [n["id"] for n in notes.applicable(None, "S1", [])] == [nid]


def test_notes_made_before_approvals_keep_applying(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, scope TEXT, target TEXT, kind TEXT, text TEXT,"
              " author TEXT, active INTEGER DEFAULT 1, created_at TEXT)")
    c.execute("INSERT INTO notes (scope, target, kind, text, author, created_at) VALUES"
              " ('global', NULL, 'fact', 'old', 'ann', '2026-01-01')")
    for (table, column), sql in db.MIGRATIONS.items():
        if table == "notes":
            c.execute(sql)
            if (table, column) in db.BACKFILL:
                c.execute(db.BACKFILL[(table, column)])
    row = c.execute("SELECT approved_by, approved_at, version FROM notes").fetchone()
    assert row == ("(before approvals)", "2026-01-01", 1)


def test_a_config_version_is_approved_by_someone_else():
    from cormon import config

    db.ex("INSERT INTO config_versions(path, sha256, content, actor, recorded_at) VALUES (?,?,?,?,?)",
          ("config/triage.yaml", "abc", "x: 1", "ann", db.now()))
    vid = db.q1("SELECT id FROM config_versions")["id"]
    assert [w["id"] for w in config.unapproved()] == [vid]
    with pytest.raises(config.ConfigApprovalRefused, match="different person"):
        config.approve_version(vid, "ann")
    config.approve_version(vid, "bob")
    assert config.unapproved() == [] and db.q1("SELECT 1 FROM audit_log WHERE action='config_approved'")


def test_a_config_change_is_not_used_until_someone_else_approves_it(tmp_path, monkeypatch):
    from cormon import config

    f = tmp_path / "triage.yaml"
    f.write_text("hours: 4\n")
    monkeypatch.setattr(config, "tracked_files", lambda: [f])
    monkeypatch.setattr(config, "live", lambda: True)
    config.record_config_changes("ann", "first")  # the first version of a file is in use at once
    assert config.in_force(str(f))["content"] == "hours: 4\n" and config.waiting() == []

    f.write_text("hours: 48\n")  # edited on the server, or by `cormon config set`
    assert config.record_config_changes("ann") == []
    assert f.read_text() == "hours: 4\n", "the file goes back to the version in use"
    [w] = config.waiting()
    assert w["actor"] == "ann" and db.q1("SELECT 1 FROM audit_log WHERE action='config_change_waiting'")
    f.write_text("hours: 48\n")  # the same change again is the same waiting version
    config.record_config_changes()
    assert [x["id"] for x in config.waiting()] == [w["id"]] and f.read_text() == "hours: 4\n"

    with pytest.raises(config.ConfigApprovalRefused, match="different person"):
        config.approve_version(w["id"], "ann")
    assert f.read_text() == "hours: 4\n"
    config.approve_version(w["id"], "bob")
    assert f.read_text() == "hours: 48\n" and config.waiting() == [] and config.unapproved() == []
    assert config.record_config_changes() == [] and f.read_text() == "hours: 48\n"

    f.write_text("hours: [\n")
    config.record_config_changes("ann")
    [bad] = config.waiting()
    with pytest.raises(config.ConfigApprovalRefused, match="not valid YAML"):
        config.approve_version(bad["id"], "bob")
    f.write_text("hours: 24\n")
    config.record_config_changes("cy")
    newer = config.waiting()[-1]
    config.approve_version(newer["id"], "ann")
    with pytest.raises(config.ConfigApprovalRefused, match="older than"):
        config.approve_version(bad["id"], "bob")


def test_a_laptop_copy_never_puts_its_files_back(tmp_path, monkeypatch):
    from cormon import config

    f = tmp_path / "triage.yaml"
    f.write_text("hours: 4\n")
    monkeypatch.setattr(config, "tracked_files", lambda: [f])
    monkeypatch.setattr(config, "live", lambda: False)
    config.record_config_changes()
    f.write_text("hours: 48\n")
    assert config.record_config_changes() == [str(f)] and f.read_text() == "hours: 48\n"


def _live_home(tmp_path, monkeypatch):
    from cormon import config

    home = tmp_path / "live"
    (home / "config").mkdir(parents=True)
    (home / "config" / "triage.yaml").write_text("hours: 4\n")
    (home / "config" / "settings.yaml").write_text(
        "llm:\n  model: a\nweb:\n  host: 127.0.0.1  # server only\n")
    monkeypatch.setattr(config, "live", lambda: True)
    monkeypatch.setattr(config, "tracked_files", lambda: sorted((home / "config").glob("*.yaml")))
    real = config._rel
    monkeypatch.setattr(config, "_rel", lambda p: str(p.relative_to(home)) if p.is_relative_to(home) else real(p))
    monkeypatch.setattr(settings, "resolve", lambda rel: home / rel)
    config.record_config_changes("ann", "first")
    return home


def test_on_the_live_bot_a_change_the_gate_tests_cannot_be_approved_there(tmp_path, monkeypatch):
    from cormon import config

    home = _live_home(tmp_path, monkeypatch)
    (home / "config" / "triage.yaml").write_text("hours: 48\n")
    s = home / "config" / "settings.yaml"
    s.write_text(s.read_text().replace("127.0.0.1", "0.0.0.0"))
    config.record_config_changes("ann")
    tri, sett = sorted(config.waiting(), key=lambda w: w["path"], reverse=True)
    with pytest.raises(config.ConfigApprovalRefused, match="only through GitHub"):
        config.approve_version(tri["id"], "bob")
    config.approve_version(sett["id"], "bob")  # a server-only part of the settings: two people are enough
    assert "0.0.0.0" in s.read_text() and (home / "config" / "triage.yaml").read_text() == "hours: 4\n"
    s.write_text(s.read_text().replace("model: a", "model: b"))
    config.record_config_changes("ann")
    with pytest.raises(config.ConfigApprovalRefused, match="only through GitHub"):
        config.approve_version(config.waiting()[-1]["id"], "bob")


def test_config_set_can_add_a_setting_the_file_does_not_have_yet(tmp_path, monkeypatch, capsys):
    from cormon import cli, config

    home = _live_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "actor", lambda args: "ann")
    cli.main(["config", "set", "settings", "system.backup_bucket", "my-bucket"])
    assert "changed from None to 'my-bucket'" in capsys.readouterr().out
    assert "my-bucket" not in (home / "config" / "settings.yaml").read_text()
    assert config.waiting()[-1]["path"] == "config/settings.yaml"


def test_the_release_puts_the_tested_config_in_place_and_keeps_the_servers_own(tmp_path, monkeypatch):
    from cormon import config

    home = _live_home(tmp_path, monkeypatch)
    commit = tmp_path / "commit"
    (commit / "config").mkdir(parents=True)
    (commit / "config" / "triage.yaml").write_text("hours: 24\n")
    (commit / "config" / "settings.yaml").write_text("llm:\n  model: b\nweb:\n  host: 0.0.0.0\n")
    (home / "config" / "triage.yaml").write_text("hours: 99\n")  # a hand edit just before: it waits
    assert sorted(config.release(commit, "john_huma_ai", "run 7")) == ["config/settings.yaml", "config/triage.yaml"]
    assert (home / "config" / "triage.yaml").read_text() == "hours: 24\n"
    s = (home / "config" / "settings.yaml").read_text()
    assert "model: b" in s and "host: 127.0.0.1  # server only" in s
    assert config.waiting() == [] and config.unapproved() == []
    assert config.in_force("config/triage.yaml")["approved_by"] == "release gate (run 7)"
    assert config.record_config_changes() == [] and db.q1("SELECT 1 FROM audit_log WHERE action='config_released'")
    assert config.release(commit, "john_huma_ai", "run 8") == []


def test_the_live_study_model_is_not_rebuilt_on_the_server(monkeypatch):
    from cormon import cli, config

    monkeypatch.setattr(config, "live", lambda: True)
    for fn, args in ((cli.cmd_ingest, {"study": None, "force": False}), (cli.cmd_checks, {"study": "x"}),
                     (cli.cmd_guide, {"study": "x"}), (cli.cmd_rules_decide, {"study": "x"}),
                     (cli.cmd_coverage, {"study": "x", "no_ai": False}),
                     (cli.cmd_duties, {"study": "x", "rebuild": True})):
        with pytest.raises(SystemExit, match="without the release gate"):
            fn(SimpleNamespace(**args))


def test_a_merge_reuses_the_pull_requests_pass_only_for_the_same_code(tmp_path, monkeypatch):
    import subprocess

    from cormon import gate

    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,  # noqa: E731
                                    check=True).stdout.strip()
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@x"), git("config", "user.name", "t")
    (repo / "f").write_text("1")
    git("add", "f"), git("commit", "-qm", "base")
    git("switch", "-qc", "pr")
    (repo / "f").write_text("2")
    git("commit", "-qam", "change")
    head = git("rev-parse", "HEAD")
    git("switch", "-q", "main"), git("merge", "-q", "--squash", "pr"), git("commit", "-qm", "change (#2)")
    real = gate._git
    monkeypatch.setattr(gate, "REPO", repo)
    monkeypatch.setattr(gate, "_git", lambda *a: "" if a[0] == "fetch" else real(*a))
    gate_ok = {"jobs": [{"name": "gate", "conclusion": "success"}]}
    answers = {"pulls": [{"head": {"sha": head}}], "runs": {"workflow_runs": [{"id": 7, "run_number": 3}]},
               "jobs": gate_ok}
    monkeypatch.setattr(gate, "_github", lambda path: answers["pulls" if "/pulls" in path else
                                                      "jobs" if "/jobs" in path else "runs"])
    assert gate.same_code_passed() == 7
    answers["jobs"] = {"jobs": [{"name": "gate", "conclusion": "failure"}]}
    assert gate.same_code_passed() is None, "the pull request's gate failed"
    answers["jobs"] = gate_ok
    (repo / "f").write_text("3")
    git("commit", "-qam", "another change on main")
    assert gate.same_code_passed() is None, "different code from what the pull request tested"


def test_every_commit_must_name_a_task_that_exists():
    from cormon import gate

    found = [{"sha": "a" * 40, "subject": "Hourly backups (#14)", "body": "Hourly backups (#14)\n"},
             {"sha": "b" * 40, "subject": "Fix typo", "body": "Fix typo\n\nsee https://x/y#3 and &#9;\n"},
             {"sha": "c" * 40, "subject": "Names checked", "body": "Names checked\n\nRefs #99\n"}]
    problems = gate.task_problems(found, exists=lambda n: n == 14)
    assert len(problems) == 2 and "bbbbbbbb" in problems[0] and "names no task" in problems[0]
    assert "cccccccc" in problems[1] and "#99" in problems[1]
    assert gate.task_problems(found[1:2], pr=14, exists=lambda n: n == 14) == [], "a pull request's own number"
    assert len(gate.task_problems(found)) == 1, "offline only the form is checked"


def test_the_gate_runs_every_set_when_matching_settings_change():
    from cormon import config, gate

    assert gate.TESTED_SETTINGS == config.TESTED_SETTINGS
    assert gate._llm_block("matching:\n  sweep: true\n") != gate._llm_block("matching:\n  sweep: false\n")
    assert gate._llm_block("web:\n  host: a\n") == gate._llm_block("web:\n  host: b\n")


def test_on_the_server_the_name_is_the_admins_own_sign_in(monkeypatch):
    from cormon import cli

    _set(monkeypatch, web__admin__emails=["peter@huma.ai", "john@huma.ai"])
    monkeypatch.setenv("CORMON_OPERATOR", "peter")
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert cli.actor(SimpleNamespace(as_="anyone")) == "anyone", "a laptop copy keeps --as"
    assert cli.actor(SimpleNamespace(as_=None)) == "peter"

    monkeypatch.setenv("SUDO_USER", "john_huma_ai")  # set by sudo from the Google sign-in
    assert settings.login_name("John@Huma.ai") == "john_huma_ai"
    assert cli.actor(SimpleNamespace(as_=None)) == "john_huma_ai", "CORMON_OPERATOR cannot change it"
    assert cli.actor(SimpleNamespace(as_="JOHN_HUMA_AI")) == "john_huma_ai"
    with pytest.raises(SystemExit, match="signed in as john_huma_ai"):
        cli.actor(SimpleNamespace(as_="peter_huma_ai"))
    for login in ("mallory_huma_ai", "root"):
        monkeypatch.setenv("SUDO_USER", login)
        with pytest.raises(SystemExit, match="not one of the admins"):
            cli.actor(SimpleNamespace(as_=None))

    _set(monkeypatch, compliance__checked_names=True, web__admin__emails=["john@huma.ai"])
    monkeypatch.delenv("SUDO_USER")
    with pytest.raises(SystemExit, match="not one of the admins"):
        cli.actor(SimpleNamespace(as_="john_huma_ai"))


def test_one_admin_cannot_approve_their_own_request_by_another_name(monkeypatch):
    from cormon import cli
    from cormon.compliance import approvals

    _set(monkeypatch, web__admin__emails=["peter@huma.ai", "john@huma.ai"])
    monkeypatch.setenv("SUDO_USER", "john_huma_ai")
    rid = approvals.request("reset", "db", cli.actor(SimpleNamespace(as_=None)), "test")
    monkeypatch.setenv("CORMON_OPERATOR", "peter_huma_ai")
    with pytest.raises(approvals.Refused, match="different person"):
        approvals.approve(rid, cli.actor(SimpleNamespace(as_=None)))
    monkeypatch.setenv("SUDO_USER", "peter_huma_ai")
    assert approvals.approve(rid, cli.actor(SimpleNamespace(as_=None)))["approved_by"] == "peter_huma_ai"


def test_config_approvals_can_be_turned_off_only_with_approval(monkeypatch):
    from cormon import config

    db.ex("INSERT INTO config_versions(path, sha256, content, actor, recorded_at) VALUES (?,?,?,?,?)",
          ("config/settings.yaml", "a", "system: {config_needs_approval: true}\n", "ann", db.now()))
    db.ex("INSERT INTO config_versions(path, sha256, content, actor, recorded_at, waiting) VALUES (?,?,?,?,?,1)",
          ("config/settings.yaml", "b", "system: {config_needs_approval: false}\n", "ann", db.now()))
    assert config.needs_approval()
    db.ex("UPDATE config_versions SET approved_by='bob' WHERE sha256='b'")
    assert not config.needs_approval()


# ------------------------------------------------------------------ quality in use (GOV-02)

def test_drift_counts_grounding_from_the_reader_trace():
    from cormon import drift

    trace = {"parts": [{"findings": [{"outcome": "clear", "quote_found": True},
                                     {"outcome": "unsure", "quote_found": False},
                                     {"outcome": "rejected"}, {"outcome": "duplicate"}]}]}
    db.audit("reader_checked", entity_type="message", entity_id=1, trace=trace)
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    m = drift.measure(*drift._window(now))
    assert (m["proposed"], m["rejected"], m["unsure"], m["no_exact_words"]) == (3, 1, 1, 1)


def test_drift_flags_a_rate_that_has_clearly_risen(monkeypatch):
    from cormon import drift

    base = {k: 0 for k in ("messages", "alerts", "safety", "proposed", "rejected", "unsure", "no_exact_words",
                           "ai_calls", "ai_failed", "fallback_used", "wording_refused", "judgment_flagged",
                           "blocked_outgoing", "answer_messages", "not_answered")}
    weeks = iter([dict(base, proposed=20, rejected=10)] + [dict(base, proposed=20, rejected=2)] * 4)
    monkeypatch.setattr(drift, "measure", lambda s, e: next(weeks))
    rep = drift.report()
    assert any("rejected" in f and "50%" in f and "10%" in f for f in rep["flags"])
    assert "Needs a look" in drift.render(rep, [])


def test_drift_samples_quiet_messages_for_a_person_to_mark(monkeypatch):
    from cormon import drift

    sent = _no_send(monkeypatch)
    _set(monkeypatch, system__ops_emails=["john@huma.ai"], drift__sample_size=2)
    mids = [capture.capture(_chat(ext=f"spaces/S1/messages/q{i}")) for i in range(4)]
    db.ex("UPDATE messages SET status='processed'")
    _item(1)
    db.ex("UPDATE items SET first_message_id=?, last_message_id=? WHERE id=1", (mids[0], mids[0]))
    oid = drift.weekly(log=lambda *_: None)
    picked = [r["message_id"] for r in db.q("SELECT message_id FROM drift_samples")]
    assert len(picked) == 2 and mids[0] not in picked
    assert oid and "cormon drift --mark" in db.q1("SELECT body FROM outbox WHERE id=?", (oid,))["body"]
    assert drift.weekly(log=lambda *_: None) is None                           # once a week
    drift.mark(picked[0], "missed", "ann", "late visit not raised")
    rep = drift.report()
    assert rep["missed"] == 1 and any("missed problem" in f for f in rep["flags"])


# ------------------------------------------------------------------ records package (EXP-01, SAF-07)

def test_the_records_package_holds_a_study_and_shows_a_changed_original(tmp_path):
    import csv
    import io
    import zipfile

    from cormon import config, items, routing
    from cormon.compliance import package

    study = config.study("NCT04098666")
    mid = capture.capture(_chat(text="Participant 001 was admitted to hospital", raw=b'{"text": "admitted"}'))
    other = capture.capture(replace(_chat(ext="spaces/S1/messages/2", raw=b"{}"), study_id="NCT05842941"))
    iid, _ = items.upsert(study=study, thread_key="teams:t1", message_id=mid, dedup_key="s1", kind="out_of_scope",
                          pattern_class=None, rule_id=None, participant_id="001", site=None, polarity="problem",
                          title="safety", tier="low", tier_reason="safety", evidence={}, reasoning="hospital",
                          state="out_of_scope")
    routing.enqueue(purpose="safety", channel="email", destination="email", body="Safety message", item_id=iid,
                    subject="Safety", to=["mm@sponsor.org"], study_id=study.study_id)

    def read(res):
        z = zipfile.ZipFile(res["path"])
        return z, {n: list(csv.DictReader(io.StringIO(z.read(n).decode()))) for n in z.namelist()
                   if n.endswith(".csv")}

    res = package.build(study.study_id, tmp_path / "out", actor="ann")
    z, t = read(res)
    assert [m["id"] for m in t["messages.csv"]] == [str(mid)]
    assert {r["owner"] for r in t["manifest.csv"]} == {f"message {mid}", "outbox 1"}
    assert all(r["status"] == "ok" for r in t["manifest.csv"])
    raw = next(r for r in t["manifest.csv"] if r["kind"] == "message_raw")
    assert z.read(raw["file"]) == b'{"text": "admitted"}'
    assert t["safety.csv"][0]["sent_to"] == '["mm@sponsor.org"]' and t["safety.csv"][0]["send_status"] == "queued"
    assert t["alerts.csv"][0]["state_label"] == "safety (not assessed)"
    assert not any(r["entity_id"] == str(other) for r in t["audit_trail.csv"] if r["entity_type"] == "message")
    assert "Audit chain: intact" in z.read("README.txt").decode()
    logged = db.q1("SELECT actor, details FROM audit_log WHERE action='records_exported'")
    assert logged["actor"] == "ann" and res["sha256"] in logged["details"]

    (settings.data_dir() / db.q1("SELECT raw_path FROM messages WHERE id=?", (mid,))["raw_path"]).write_bytes(b"{}")
    res = package.build(study.study_id, tmp_path / "out2")
    z, t = read(res)
    assert next(r for r in t["manifest.csv"] if r["kind"] == "message_raw")["status"] == "changed"
    assert "do NOT match" in z.read("README.txt").decode()


# ------------------------------------------------------------------ safety routing failure paths (SAF-06)

def test_no_monitor_listed_a_safety_message_goes_to_nobody_and_cannot_be_cleared():
    from cormon import config, items, routing

    study = config.study("NCT04098666")
    assert not any(p.email for p in study.people_with_role("medical_monitor"))
    mid = capture.capture(replace(_chat(text="Participant 001 was admitted to hospital"),
                                  from_addr="johnrohit94@gmail.com"))
    iid, _ = items.upsert(study=study, thread_key="teams:t1", message_id=mid, dedup_key="s1", kind="out_of_scope",
                          pattern_class=None, rule_id=None, participant_id="001", site=None, polarity="problem",
                          title="safety", tier="low", tier_reason="safety", evidence={}, reasoning=None,
                          state="out_of_scope")
    assert routing.route_item(iid) == [] and not db.q1("SELECT 1 FROM outbox")
    with pytest.raises(items.StepOrder, match="safety item"):
        items.set_state(iid, "cleared", "ops", "operator", study_id=study.study_id, code="fixed")


def test_a_safety_message_goes_unassessed_to_the_medical_monitor_only(monkeypatch):
    from cormon import config, items, llm, routing

    monkeypatch.setattr(llm, "structured", lambda *a, **k: pytest.fail("no AI call expected"))
    study = config.study("NCT05842941")
    monitor = study.people_with_role("medical_monitor")[0].email
    crc = study.people_with_role("crc")[0].email
    mid = capture.capture(Inbound(channel="email", external_id="<s1@site.org>", thread_ref="s1", from_addr=crc,
                                  from_name="C", subject="DNL-1012 visit", sent_at=_ago(5), study_id=study.study_id,
                                  body_text="DNL-1012 fell at home and spent a night in the ER.",
                                  rfc_message_id="<s1@site.org>"))
    iid, _ = items.upsert(study=study, thread_key="email:s1", message_id=mid, dedup_key="s1", kind="out_of_scope",
                          pattern_class=None, rule_id=None, participant_id="DNL-1012", site="site-01",
                          polarity="neutral", title="safety", tier="low", tier_reason="safety",
                          evidence={"message_quote": "fell at home and spent a night in the ER"}, reasoning=None,
                          state="out_of_scope")
    routing.route_item(iid)
    out = [dict(r) for r in db.q("SELECT * FROM outbox WHERE item_id=?", (iid,))]
    assert [(json.loads(r["to_addrs"]), r["status"], r["template_id"]) for r in out] == \
        [([monitor], "queued", "email-safety-to-medical-monitor")]
    assert "fell at home and spent a night in the ER" in out[0]["body"] and "have not assessed" in out[0]["body"]


def test_a_message_with_a_safety_part_sends_no_notice_back_to_the_site(monkeypatch):
    from cormon import careful, config, events, items, llm, routing

    monkeypatch.setattr(careful, "check_post", lambda **k: None)                 # the chat posts are not the point
    monkeypatch.setattr(llm, "structured", lambda *a, **k: pytest.fail("no AI call expected"))
    study = config.study("NCT05842941")
    crc = study.people_with_role("crc")[0].email

    def urgent(n, harm):
        mid = capture.capture(Inbound(channel="email", external_id=f"<m{n}@site.org>", thread_ref=f"t{n}",
                                      from_addr=crc, from_name="C", subject="DNL-1012", sent_at=_ago(5),
                                      body_text="DNL-1012 randomized with eGFR 25. She also fell and broke her wrist.",
                                      rfc_message_id=f"<m{n}@site.org>", study_id=study.study_id))
        events.store(mid, {"events": [{"description": "randomized", "harm": "none"},
                                      {"description": "fell, broke her wrist", "harm": harm}]})
        iid, _ = items.upsert(study=study, thread_key=f"email:t{n}", message_id=mid, dedup_key=f"k{n}",
                              kind="finding", pattern_class="eligibility", rule_id=None, participant_id="DNL-1012",
                              site="site-01", polarity="problem", title="eGFR below the minimum", tier="immediate",
                              tier_reason="test", evidence={}, reasoning="r")
        routing.route_item(iid)
        return iid, {r["purpose"] for r in db.q("SELECT purpose FROM outbox WHERE item_id=?", (iid,))}

    iid, sent = urgent(1, "injury_or_hospitalization")
    assert "email_crc" not in sent and "email_cra" in sent                     # the alert itself still goes out
    assert db.q1("SELECT 1 FROM audit_log WHERE action='site_notice_withheld_safety' AND entity_id=?", (str(iid),))
    assert "email_crc" in urgent(2, "none")[1]                                  # no harm: the notice goes as before


def test_a_message_from_someone_not_on_the_people_list_reaches_only_the_admins(monkeypatch):
    from datetime import timedelta as td

    from cormon import careful, cli, config, digest, items, outbox, routing

    monkeypatch.setattr(careful, "check_post", lambda **k: None)
    monkeypatch.setattr(outbox, "dispatch", lambda **k: {"sent": 0, "failed": 0})
    _set(monkeypatch, web__admin__emails=["ops@huma.ai"], web__base_url="https://bot.example")
    study = config.study("NCT05842941")

    def alert(n, sender):
        mid = capture.capture(Inbound(channel="email", external_id=f"<r{n}@x.org>", thread_ref=f"r{n}",
                                      from_addr=sender, from_name="S", subject="DNL-1012", sent_at=_ago(5),
                                      body_text="DNL-1012 randomized with eGFR 25.", rfc_message_id=f"<r{n}@x.org>",
                                      study_id=study.study_id))
        iid, _ = items.upsert(study=study, thread_key=f"email:r{n}", message_id=mid, dedup_key=f"r{n}",
                              kind="finding", pattern_class="eligibility", rule_id=None, participant_id="DNL-1012",
                              site="site-01", polarity="problem", title="eGFR below the minimum", tier="immediate",
                              tier_reason="test", evidence={}, reasoning="r")
        return mid, iid

    mid, iid = alert(1, "someone@other-study.org")
    assert routing.route_item(iid) == []
    sent = db.q("SELECT purpose, to_addrs, body FROM outbox")
    assert [(o["purpose"], db.unjs(o["to_addrs"])) for o in sent] == [("ops_alert", ["ops@huma.ai"])]
    assert "someone@other-study.org" in sent[0]["body"] and f"cormon draft {iid} --force" in sent[0]["body"]
    assert items.get(iid)["routed"] and db.q1("SELECT 1 FROM audit_log WHERE action='item_held_off_roster'")
    assert routing.route_item(iid) == [] and len(db.q("SELECT 1 FROM outbox")) == 1   # not tried again
    assert not cli._sponsor_sees(iid)
    now = datetime.now(timezone.utc)
    assert digest.build(now - td(hours=1), now + td(minutes=1))["total"] == 0

    _, ok = alert(2, study.people_with_role("crc")[0].email)
    assert routing.route_item(ok) and cli._sponsor_sees(ok)
    assert digest.build(now - td(hours=1), now + td(minutes=1))["total"] == 1


def test_only_the_live_bot_reads_and_sends_real_correspondence(monkeypatch):
    from cormon import channels, config, service

    monkeypatch.setattr(channels, "_cache", {})
    monkeypatch.setattr(config, "live", lambda: False)
    _set(monkeypatch, channels__email="imap", channels__teams="sim")
    with pytest.raises(channels.NotTheLiveBot, match="only the live bot"):
        channels.email_channel()
    with pytest.raises(channels.NotTheLiveBot):
        service.run(once=True)
    assert channels.teams_channel() is not None                     # the simulator is fine anywhere
    _set(monkeypatch, channels__email="sim", channels__teams="gchat")
    with pytest.raises(channels.NotTheLiveBot, match="channels.teams"):
        channels.teams_channel()


def test_a_copy_that_is_not_the_live_bot_will_not_open_the_live_database(monkeypatch, tmp_path):
    from cormon import config

    monkeypatch.setattr(config, "live", lambda: True)
    db.q1("SELECT 1")
    assert db.get_state(db.LIVE_MARK)                               # the live bot marks its database
    db._local.conn.close()
    db._local.conn = None
    monkeypatch.setattr(config, "live", lambda: False)
    with pytest.raises(db.LiveDataElsewhere, match="not the live bot"):
        db.q1("SELECT 1")
    db._local.conn = None
    for ext in ("", "-wal", "-shm"):
        (tmp_path / f"cormon.db{ext}").unlink(missing_ok=True)
    db.q1("SELECT 1")                                               # an unmarked practice database opens
    assert db.get_state(db.LIVE_MARK) is None


def test_off_server_backups_are_not_downloaded_by_a_copy_that_is_not_the_live_bot(monkeypatch, capsys):
    from cormon import cli, config

    monkeypatch.setattr(config, "live", lambda: False)
    for args in (["backup", "--fetch"], ["backup", "--drill"]):
        with pytest.raises(SystemExit, match="only on the live server"):
            cli.main(args)


def test_no_cra_or_lead_listed_is_said_and_flagged():
    from cormon import items

    study = SimpleNamespace(people_with_role=lambda role, site=None: [])
    person, why = items.choose_recipient(study, "104")
    assert person is None and "no CRA or ClinOps lead" in why
    assert "immediate item has no named recipient" in items.check_consistency(
        {"state": "open", "send_draft": 1, "counts": 1, "tier": "immediate", "recipient_person": None,
         "kind": "finding"})


def test_unreachable_recipient_is_retried_then_failed_then_reported(monkeypatch):
    from cormon import outbox, routing, stuck

    db.set_state("stuck_since", _ago(5))
    _set(monkeypatch, system__ops_emails=["john@huma.ai"])

    def down(row):
        raise RuntimeError("550 no such user")
    monkeypatch.setattr(outbox, "_adapter", lambda ch: SimpleNamespace(send=down))
    oid = routing.enqueue(purpose="email_cra", channel="email", destination="email", body="b", subject="s",
                          to=["gone@site.org"], study_id="S1")
    for _ in range(len(outbox.RETRY_MINUTES) + 1):
        db.ex("UPDATE outbox SET next_try_at=NULL WHERE id=?", (oid,))
        outbox.dispatch()
    row = db.q1("SELECT status, attempts, last_error FROM outbox WHERE id=?", (oid,))
    assert row["status"] == "failed" and row["attempts"] == len(outbox.RETRY_MINUTES) + 1 and "550" in row["last_error"]
    assert f"outbox:{oid}" in [p["key"] for p in stuck.problems()]
    warned = stuck.warn(log=lambda *_: None)
    assert "john@huma.ai" in db.q1("SELECT to_addrs FROM outbox WHERE id=?", (warned,))["to_addrs"]


def test_router_down_an_alert_never_sent_is_retried_then_reported(monkeypatch):
    from cormon import pipeline, routing, stuck

    db.set_state("route_missed_since", _ago(60))
    db.set_state("stuck_since", _ago(60))
    mid = capture.capture(_chat())
    db.ex("UPDATE messages SET status='processed' WHERE id=?", (mid,))
    _item(8, routed=0)
    db.ex("UPDATE items SET first_message_id=?, created_at=? WHERE id=8", (mid, _ago(30)))

    def broken(item_id, **k):
        raise RuntimeError("routing config unreadable")
    monkeypatch.setattr(routing, "route_item", broken)
    assert pipeline.route_missed(log=lambda *_: None) == [8]
    assert db.q1("SELECT 1 FROM audit_log WHERE action='item_route_failed' AND entity_id='8'")
    assert "item:8" in [p["key"] for p in stuck.problems()]
    assert pipeline.route_missed(log=lambda *_: None) == []                     # tried once, then left to a person


# ------------------------------------------------------------------ release gates (SDL-05, VAL-05, CFG-02, CAP-09)

def test_a_change_runs_only_the_test_sets_it_can_affect():
    from cormon import gate

    assert gate.plan(["cormon/reader.py"]) == ["findings", "questions"]
    assert gate.plan(["cormon/outcheck.py"]) == ["questions", "wording", "doctors"]
    assert gate.plan(["config/careful.yaml"]) == ["wording", "doctors"]
    assert gate.plan(["studies/model/model.sql"]) == ["findings", "questions"]
    assert gate.plan(["docs/compliance-part1-plan.md", "cormon/corrections.py"]) == []
    every = list(gate.gates()["sets"])
    assert gate.plan(["cormon/llm.py"]) == every                                # the model or its supplier
    assert gate.plan(["config/gates.yaml"]) == every                            # the gate itself
    assert gate.plan(["docs/x.md"], llm_changed=True) == every                  # the models in settings.yaml
    assert gate.plan(None) == every                                             # no live version known


def test_every_code_file_is_tied_to_a_test_set_or_known_to_need_none():
    from cormon import gate

    cfg = gate.gates()
    tied = {p for s in cfg["sets"].values() for p in gate._flat(s["when"])} | set(cfg["all_when"])
    none_needed = {  # operator tools, records, alerts and pages: covered by the code tests only
        "__init__", "adminview", "backup", "cli", "compare", "corrections", "coverage", "digest", "drift", "export",
        "notes", "prune", "senders", "settings", "stuck", "timing", "watchdog", "weblogin", "webview",
        "outbox"}  # its AI set (the trick emails) is left out for now
    from pathlib import Path
    for f in sorted((gate.REPO / "cormon").glob("*.py")):
        rel = f"cormon/{f.name}"
        assert f.stem in none_needed or any(gate.fnmatch.fnmatch(rel, p) for p in tied), f"{rel} is in no test set"
    assert Path(gate.REPO / cfg["sets"]["findings"]["folder"]).is_dir()


def test_the_pass_marks_block_a_drop_and_a_stranger_recipient(tmp_path):
    from cormon import gate

    sets = gate.gates()["sets"]
    assert gate.check("findings", sets["findings"], {"recall": 0.98, "precision": 0.97}) == []
    low = gate.check("findings", sets["findings"], {"recall": 0.93, "precision": 0.97})
    assert low and "recall 93%" in low[0]
    assert gate.check("questions", sets["questions"], {"right_or_partly": 0.98, "wrong": 0.01}) == []
    assert gate.check("questions", sets["questions"], {"right_or_partly": 0.98, "wrong": 0.04})
    assert gate.check("wording", sets["wording"], {"right": 25 / 26})
    assert gate.check("doctors", sets["doctors"], {"right": "20/28"}) == []        # shown only
    tricks = {"marks": {"recipients_from_config": True, "show": ["accuracy"]}}  # the trick set, when it returns
    assert gate.check("tricks", tricks, {"accuracy": 0.6, "strangers": []}) == []
    results = tmp_path / "results.csv"
    results.write_text("case,sent\nj01,email_cra -> john@huma.ai | teams_cra -> teams_alert_channel\n"
                       "j07,email_cra -> attacker@evil.example\n")
    bad = gate.strangers("NCT05842941", results)
    assert bad == ["j07: attacker@evil.example"]
    assert gate.check("tricks", tricks, {"accuracy": 0.6, "strangers": bad})


def test_the_question_grader_sees_every_section_the_bot_read():
    from cormon import qabench

    sources = {"a:1": {"title": "Expected", "text": "28 + 3 days"}, "b:2": {"title": "Read", "text": "28 ± 3 days"},
               "c:3": {"title": "Big", "text": "x" * 50000}}
    case = {"expect": {"source": "a:1"}}
    res = {"answers": [{"picked": ["b:2", "a:1", "gone:9", "c:3"]}]}
    text = qabench._sources_text(case, res, sources)
    assert text.index("[a:1]") < text.index("[b:2]") and "28 ± 3" in text      # expected section first, no repeats
    assert text.count("[a:1]") == 1 and "gone:9" not in text
    assert len(text) <= qabench.SOURCES_CHARS                                    # each cut, and the whole capped


def test_the_test_home_sends_nothing_and_has_the_study_model(tmp_path):
    from cormon import gate

    gate.make_home(tmp_path / "home")
    cfg = __import__("yaml").safe_load((tmp_path / "home" / "config" / "settings.yaml").read_text())
    assert cfg["channels"]["email"] == "sim" and cfg["channels"]["teams"] == "sim" and not cfg["web"]["enabled"]
    c = sqlite3.connect(tmp_path / "home" / "seed.db")
    assert c.execute("SELECT COUNT(*) FROM rules WHERE study_id='NCT05842941'").fetchone()[0] > 200
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"documents", "doc_pages", "rules", "allowances", "guide", "rule_decisions"}  # no message data

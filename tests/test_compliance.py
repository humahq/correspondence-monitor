"""Part 11 groundwork: fingerprints, the hash-chained audit trail, kept chat edits, the AI log, alerts, the watchdog."""

import http.client
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from cormon import capture, db, settings
from cormon.capture import Inbound
from cormon.compliance import SAFETY_NOTICE, ailog, alerts, backend, records, secrets, trail


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
    items.set_state(1, "cleared", "ops", "operator", study_id="S1")             # the operator may
    with pytest.raises(items.StepOrder, match="already cleared"):
        items.set_state(1, "cleared", "ops", "operator", study_id="S1")
    _item(2, state="out_of_scope")
    for state in ("cleared", "suppressed"):
        with pytest.raises(items.StepOrder, match="safety item"):
            items.set_state(2, state, "ops", "operator", study_id="S1")
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

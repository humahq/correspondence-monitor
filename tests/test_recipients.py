"""Someone on a study email the study does not list (recipients.py, Eliot, Nov 10, message 335)."""

import pytest

from cormon import capture, config, db, recipients
from cormon.capture import Inbound
from cormon.compliance import alerts


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "data_dir", lambda: tmp_path)
    db._local.conn = None
    yield
    db._local.conn = None


@pytest.fixture
def told(monkeypatch):
    said = []
    monkeypatch.setattr(alerts, "notify", lambda key, subject, body, **k: said.append((key, subject, body)) or 1)
    return said


def _email(sender, to=(), cc=()):
    study = config.study("NCT05842941")
    mid = capture.capture(Inbound(channel="email", external_id="<o1@x.org>", thread_ref="o1", from_addr=sender,
                                  from_name="P", subject="101-004", sent_at="2026-11-09T20:05:59+00:00",
                                  study_id=study.study_id, body_text="Do they need to wash out quetiapine?",
                                  to_addrs=list(to), cc_addrs=list(cc), rfc_message_id="<o1@x.org>"))
    return study, capture.message(mid)


def _out(study):
    return [dict(r, to=db.unjs(r["to_addrs"])) for r in db.q("SELECT purpose, to_addrs, subject, body FROM outbox")]


def test_everyone_listed_and_the_bot_itself_are_not_outside():
    alias = config.study("NCT05842941").alias
    study, msg = _email("johnrohit94@gmail.com", to=[alias], cc=["John@huma.ai", alias.replace("NCT05842941", "x")])
    assert recipients.outside(study, msg) == []


def test_a_listed_gmail_person_does_not_approve_gmail():
    study, msg = _email("johnrohit94@gmail.com", cc=["someone.else@gmail.com", "new.cra@huma.ai"])
    assert recipients.outside(study, msg) == [{"address": "someone.else@gmail.com", "domain_approved": False},
                                              {"address": "new.cra@huma.ai", "domain_approved": True}]


def test_message_335_an_outside_sender_gets_a_note_and_no_study_information(told):
    study, msg = _email("peter@prbventures.com", to=["peter@prbventures.com"], cc=[config.study("NCT05842941").alias])
    done = recipients.handle(study, msg, recipients.outside(study, msg))
    assert done["not_approved"] == ["peter@prbventures.com"] and not done["new"]
    out = {o["purpose"]: o for o in _out(study)}
    note = out["outside_recipients_sender"]
    assert note["to"] == ["peter@prbventures.com"] and "not on this study's list" in note["body"]
    assert "quetiapine" not in note["body"].lower() and "wash" not in note["body"].lower()
    cra = out["outside_recipients_cra"]
    assert set(cra["to"]) == {p.email for p in study.people_with_role("cra")} and "Privacy" in cra["subject"]
    assert "quetiapine" not in cra["body"].lower()
    assert told and told[0][0] == f"outside_recipients:{msg['id']}"
    assert db.q1("SELECT 1 FROM audit_log WHERE action='outside_recipients'")


def test_a_new_colleague_from_an_approved_domain_is_set_up_not_reported(told):
    study, msg = _email("johnrohit94@gmail.com", cc=["new.cra@huma.ai"])
    done = recipients.handle(study, msg, recipients.outside(study, msg))
    assert done["new"] == ["new.cra@huma.ai"] and not done["not_approved"] and not told
    out = {o["purpose"]: o for o in _out(study)}
    assert out["outside_recipients_sender"]["to"] == ["johnrohit94@gmail.com"]       # only the sender, not the Cc
    assert "not set up on this study" in out["outside_recipients_sender"]["body"]
    assert out["outside_recipients_cra"]["subject"].startswith("Please set up a new person")


def _chat(sender):
    study = config.study("NCT05842941")
    mid = capture.capture(Inbound(channel="teams", external_id="spaces/S1/messages/T1.T1", thread_ref="spaces/S1/threads/T1",
                                  from_addr=sender, from_name="X", subject=None, sent_at="2026-11-09T20:05:59+00:00",
                                  study_id=study.study_id, body_text="bot, what is the washout for quetiapine?",
                                  visibility="direct"))
    return study, capture.message(mid)


def test_in_chat_only_the_writer_is_checked():
    study, msg = _chat("johnrohit94@gmail.com")
    assert recipients.outside(study, dict(msg, cc_addrs=["x@gmail.com"])) == []


def test_a_chat_writer_not_on_the_list_gets_a_note_in_the_chat_and_is_reported(told):
    study, msg = _chat("stranger@gmail.com")
    done = recipients.handle(study, msg, recipients.outside(study, msg))
    assert done["not_approved"] == ["stranger@gmail.com"]
    rows = db.q("SELECT purpose, channel, body, meta FROM outbox ORDER BY id")
    note = next(r for r in rows if r["purpose"] == "outside_recipients_sender")
    assert note["channel"] == "teams" and db.unjs(note["meta"])["target"]["space"] == "spaces/S1"
    assert "not on this study's list" in note["body"] and "quetiapine" not in note["body"].lower()
    cra = next(r for r in rows if r["purpose"] == "outside_recipients_cra")
    assert cra["channel"] == "email" and "A chat message" in cra["body"] and told


def test_the_note_names_strangers_and_new_colleagues_apart(told):
    study, msg = _email("johnrohit94@gmail.com", cc=["x@gmail.com", "new.cra@huma.ai"])
    text = recipients.to_sender(study, msg, recipients.outside(study, msg))
    assert "x@gmail.com is on it and may not receive study information" in text
    assert "new.cra@huma.ai is not set up on this study yet either" in text

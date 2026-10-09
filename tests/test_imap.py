"""The IMAP adapter takes only new mail sent to a study alias, using a fake mailbox."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from cormon import capture, db
from cormon.channels import imap


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "data_dir", lambda: tmp_path)
    db._local.conn = None
    yield
    db._local.conn = None


def _mail(mid, to, frm="johnrohit94@gmail.com", minutes=5):
    when = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=minutes))
    return (f"Message-ID: <{mid}@x>\r\nFrom: {frm}\r\nTo: {to}\r\nDate: {when}\r\n"
            f"Subject: s\r\n\r\nhello\r\n").encode()


class FakeConn:
    def __init__(self, mails):
        self.mails = mails

    def select(self, *a, **k):
        return "OK", [str(len(self.mails)).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.mails)))]
        return "OK", [(f"{u} (UID {u} BODY[] {{1}}".encode(), self.mails[int(u) - 1]) for u in args[0].split(",")]

    def logout(self):
        pass


def test_imap_takes_only_new_study_alias_mail(monkeypatch):
    monkeypatch.setenv("CORMON_GMAIL_APP_PASSWORD", "x")
    mails = [
        _mail("a", "bot+NCT04098666@huma.ai"),
        _mail("b", "bot@huma.ai"),
        _mail("c", "bot+NCT04098666@huma.ai", frm="bot+NCT04098666@huma.ai"),
        _mail("d", "bot+NCT04098666@huma.ai", minutes=-600),
        _mail("e", "bot+clinical@huma.ai"),
    ]
    ch = imap.ImapChannel()
    monkeypatch.setattr(ch, "_connect", lambda: FakeConn(mails))
    got = ch.fetch()
    assert [m.rfc_message_id for m in got] == ["<a@x>", "<e@x>"]
    assert capture.resolve_email_route(got[1]) == (None, "bot+clinical@huma.ai", "to")


def test_imap_takes_reply_to_our_mail_on_bare_address(monkeypatch):
    monkeypatch.setenv("CORMON_GMAIL_APP_PASSWORD", "x")
    db.ex("INSERT INTO outbox (purpose, channel, destination, body, status, rfc_message_id, created_at)"
          " VALUES ('email_cra','email','email','b','sent','<ours@huma.ai>',?)", (db.now(),))
    reply = _mail("r", "bot@huma.ai").replace(b"Subject: s", b"In-Reply-To: <ours@huma.ai>\r\nSubject: s")
    ch = imap.ImapChannel()
    monkeypatch.setattr(ch, "_connect", lambda: FakeConn([reply, _mail("x", "bot@huma.ai")]))
    assert [m.rfc_message_id for m in ch.fetch()] == ["<r@x>"]

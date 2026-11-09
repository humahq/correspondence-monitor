"""The deployment's bot identity: name, avatar and sign-off in settings, email, admin pages and warnings."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cormon import adminview, answers, config, digest, settings, stuck


@pytest.fixture
def named(monkeypatch):
    """A deployment that has set its own bot name and avatar."""
    conf = {"bot.name": "Mira Belle", "bot.avatar_url": "https://example.test/mira.png"}
    real = settings.get
    monkeypatch.setattr(settings, "get", lambda k, d=None: conf.get(k, real(k, d)))
    return conf


@pytest.fixture
def unnamed(monkeypatch):
    monkeypatch.setattr(settings, "get", lambda k, d=None: None)


def test_the_default_bot_identity_is_neutral(unnamed):
    assert settings.bot_name() == "Study monitor"
    assert settings.bot_avatar() == ""
    assert settings.bot_signature() == (
        "Study monitor\n"
        "(automated message from the study monitoring service; it does not review safety information)")


def test_the_deployment_sets_the_bot_name_and_avatar(named):
    assert settings.bot_name() == "Mira Belle"
    assert settings.bot_avatar() == "https://example.test/mira.png"
    assert settings.bot_signature().splitlines()[0] == "Mira Belle"


def test_a_local_settings_file_overrides_the_shared_file(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.yaml"
    local.write_text("bot:\n  name: Mira Belle\n", encoding="utf-8")
    monkeypatch.setattr(settings, "LOCAL_FILE", local)
    settings.reload()
    try:
        assert settings.bot_name() == "Mira Belle"
        assert settings.get("bot.avatar_url") == ""      # the shared file still supplies the rest
        assert settings.get("system.studies")             # and its other settings survive the merge
    finally:
        settings.reload()


def test_a_local_settings_file_can_replace_a_whole_signature(tmp_path, monkeypatch):
    local = tmp_path / "settings.local.yaml"
    local.write_text("bot:\n  signature: |-\n    Mira Belle\n    Automated message.\n", encoding="utf-8")
    monkeypatch.setattr(settings, "LOCAL_FILE", local)
    settings.reload()
    try:
        assert settings.bot_signature() == "Mira Belle\nAutomated message."
    finally:
        settings.reload()


def test_the_email_from_line_carries_the_deployment_name(named):
    from cormon.channels.sim import build_mime

    msg = build_mime({"from_addr": "bot+NCT04098666@huma.ai", "to_addrs": ["cra@x.org"], "cc_addrs": [],
                      "subject": "Week 8", "body": "The visit was late."})
    assert msg["From"] == "Mira Belle <bot+NCT04098666@huma.ai>"
    assert msg["Reply-To"] == "bot+NCT04098666@huma.ai"


def test_the_email_from_line_defaults_to_a_neutral_name(unnamed):
    from cormon.channels.sim import build_mime

    msg = build_mime({"from_addr": "bot@x.org", "to_addrs": ["cra@x.org"], "cc_addrs": [],
                      "subject": "s", "body": "b"})
    assert msg["From"] == "Study monitor <bot@x.org>"


def test_an_email_reply_ends_with_the_deployment_signature(named, monkeypatch):
    sent = {}

    def capture(**kw):
        sent.update(kw)
        return 7

    monkeypatch.setattr(answers.routing, "enqueue", capture)
    study = SimpleNamespace(study_id="NCT04098666", alias="bot+NCT04098666@huma.ai",
                            monitor_signature="Mira Belle\n(automated message)")
    msg = {"channel": "email", "id": 1, "from_addr": "site@x.org", "subject": "Week 8",
           "thread_key": "email:abc", "rfc_message_id": "<m@x>", "refs": []}
    assert answers._post(study, msg, "Yes, that is a deviation.") == 7
    assert sent["body"] == "Yes, that is a deviation.\n\nMira Belle\n(automated message)"


def test_a_chat_reply_does_not_add_the_email_signature(named, monkeypatch):
    sent = {}
    monkeypatch.setattr(answers.routing, "enqueue", lambda **kw: sent.update(kw) or 8)
    study = SimpleNamespace(study_id="S", alias="bot+s@huma.ai", monitor_signature="Mira Belle")
    msg = {"channel": "teams", "id": 2, "thread_key": "teams:spaces/S/threads/T"}
    assert answers._post(study, msg, "Sure.") == 8
    assert sent["body"] == "Sure."


def test_the_admin_page_uses_the_deployment_name_and_avatar(named):
    page = adminview.shell("peter@huma.ai")
    assert "<title>Mira Belle admin</title>" in page
    assert 'data-bot-name="Mira Belle"' in page
    assert 'data-bot-avatar="https://example.test/mira.png"' in page


def test_the_admin_page_defaults_to_the_neutral_name(unnamed):
    page = adminview.shell("peter@huma.ai")
    assert "<title>Study monitor admin</title>" in page
    assert 'data-bot-name="Study monitor"' in page


def test_the_admin_page_has_the_openbook_look_only_when_switched_on(named, monkeypatch):
    assert 'class="openbook"' not in adminview.shell("peter@huma.ai"), "off: the page looks as before"
    monkeypatch.setattr(settings, "feature", lambda name: name == "openbook_look")
    page = adminview.shell("peter@huma.ai")
    assert '<body class="openbook" data-product="OpenBook"' in page
    assert "<title>OpenBook admin</title>" in page
    assert 'data-bot-name="Mira Belle"' in page


def test_a_study_without_a_signature_inherits_the_deployment_signature(named, tmp_path):
    path = tmp_path / "study.yaml"
    path.write_text("study_id: NCT00000001\nshort_name: X\n", encoding="utf-8")
    assert config.load_study(path).monitor_signature == settings.bot_signature()


def test_a_study_can_still_set_its_own_signature(named, tmp_path):
    path = tmp_path / "study.yaml"
    path.write_text("study_id: NCT00000002\nmonitor_signature: |-\n  Local Monitor\n  Local disclaimer\n",
                    encoding="utf-8")
    assert config.load_study(path).monitor_signature == "Local Monitor\nLocal disclaimer"


def test_the_digest_footer_uses_the_deployment_name(named, monkeypatch):
    monkeypatch.setattr(digest.config, "studies", lambda: {})
    monkeypatch.setattr(digest, "gaps_and_late", lambda *a: [])
    monkeypatch.setattr(digest.db, "q", lambda *a, **k: [])
    now = datetime.now(timezone.utc)
    assert digest.build(now, now)["footer"].startswith("Sent by Mira Belle.")


def test_the_ops_warning_subject_uses_the_deployment_name(named, monkeypatch):
    sent = {}
    monkeypatch.setattr(stuck, "problems", lambda: [{"key": "k", "study_id": "S", "what": "x"}])
    monkeypatch.setattr(stuck, "_ops", lambda ids: ["ops@x.org"])
    monkeypatch.setattr(stuck.db, "get_state", lambda k: None)
    monkeypatch.setattr(stuck.db, "set_state", lambda *a: None)
    monkeypatch.setattr(stuck.db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(stuck.routing, "enqueue", lambda **kw: sent.update(kw) or 9)
    assert stuck.warn(log=lambda *_: None) == 9
    assert sent["subject"] == "Mira Belle: 1 thing needs a look"


class _Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _SendAs:
    def __init__(self, current):
        self.current, self.calls = current, []

    def list(self, **kw):
        return _Call({"sendAs": self.current})

    def create(self, **kw):
        self.calls.append(("create", kw))
        return _Call({})

    def patch(self, **kw):
        self.calls.append(("patch", kw))
        return _Call({})


class _Service:
    def __init__(self, send_as):
        self._send_as = send_as

    def users(self):
        return self

    def settings(self):
        return self

    def sendAs(self):
        return self._send_as


def test_gmail_keeps_the_send_as_display_name_in_step(named):
    from cormon.channels.gmail import GmailChannel

    send_as = _SendAs([{"sendAsEmail": "bot+a@huma.ai", "displayName": "Old name"}])
    channel = object.__new__(GmailChannel)
    channel.svc = _Service(send_as)
    assert channel.ensure_send_as("bot+a@huma.ai") == "display name updated"
    assert send_as.calls == [("patch", {"userId": "me", "sendAsEmail": "bot+a@huma.ai",
                                        "body": {"displayName": "Mira Belle"}})]


def test_gmail_creates_a_new_send_as_with_the_deployment_name(named):
    from cormon.channels.gmail import GmailChannel

    send_as = _SendAs([])
    channel = object.__new__(GmailChannel)
    channel.svc = _Service(send_as)
    assert channel.ensure_send_as("bot+b@huma.ai") == "created"
    assert send_as.calls[0][1]["body"] == {"sendAsEmail": "bot+b@huma.ai", "displayName": "Mira Belle",
                                           "treatAsAlias": True}

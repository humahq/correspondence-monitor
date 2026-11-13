"""The local chat demo runs the bot in the Podman pod; these are the parts that need no Podman."""
import subprocess
from pathlib import Path

import yaml

from cormon import chatdemo


def _repo(tmp_path: Path) -> Path:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "HOME": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    for cmd in (["init", "-q"], ["add", "a"], ["commit", "-q", "-m", "1"]):
        if cmd[0] == "add":
            (tmp_path / "a").write_text("1")
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True, env=env)
    return tmp_path


def test_image_tag_follows_commit_and_unsaved_changes(tmp_path):
    repo = _repo(tmp_path)
    clean = chatdemo.image_tag(repo)
    assert "-dirty-" not in clean
    (repo / "a").write_text("2")
    dirty = chatdemo.image_tag(repo)
    assert dirty.startswith(clean + "-dirty-")
    (repo / "a").write_text("3")
    assert chatdemo.image_tag(repo) != dirty            # another edit, another image
    (repo / "a").write_text("1")
    assert chatdemo.image_tag(repo) == clean            # back to the commit: the cached image again


def test_image_tag_outside_git(tmp_path):
    assert chatdemo.image_tag(tmp_path) == "local"


def test_run_args_mount_same_paths_and_keep_data_in_a_volume():
    a = chatdemo.run_args("cormon-bot:x", Path("/b/chat-demo/t"), Path("/b"), Path("/c"), "t", "cormon", "run",
                          name="cormon-bot")
    assert a[:2] == ["podman", "run"] and "-d" in a and a[a.index("--name") + 1] == "cormon-bot"
    assert a[a.index("--pod") + 1] == "cormon-chat"
    mounts = [a[i + 1] for i, x in enumerate(a) if x == "-v"]
    assert mounts == ["/b:/b", "/c:/c", "cormon-demo-t:/b/chat-demo/t/data"]
    assert "CORMON_HOME=/b/chat-demo/t" in a
    assert a[-3:] == ["cormon-bot:x", "cormon", "run"] or a[-2:] == ["cormon", "run"]


def test_one_shot_run_is_not_detached():
    a = chatdemo.run_args("img", Path("/h"), Path("/l"), Path("/c"), "t", "python", "-m", "x")
    assert "-d" not in a and "--name" not in a


def test_in_pod_only_when_asked(monkeypatch):
    monkeypatch.delenv("CORMON_LOCAL_POD", raising=False)
    monkeypatch.delenv("OPENBOOK_BOT", raising=False)
    assert not chatdemo.in_pod()                        # the live bot and plain `cormon chat-demo`
    monkeypatch.setenv("CORMON_LOCAL_POD", "1")
    assert chatdemo.in_pod()
    monkeypatch.setenv("OPENBOOK_BOT", "host")
    assert not chatdemo.in_pod()


def test_the_demo_copy_gets_the_local_account(tmp_path, monkeypatch, capsys):
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "bot.yaml").write_text("""\
team: huma-demo
bot_name: Mira Belle
rooms: {}
people:
  peter@demo.local: peter@huma.ai
  eliot@demo.local: eliot@huma.ai
local_accounts:   # ghost is not under people:, so it is skipped
  peter: peter@demo.local
  eliot: eliot@demo.local
  ghost: ghost@demo.local
""")
    (chat / ".env").write_text("MM_BOT_TOKEN=x\n")
    live = tmp_path / "live"
    (live / "config").mkdir(parents=True)
    (live / "config" / "settings.yaml").write_text("web:\n  admin:\n    emails: [a@b.c]\n")
    monkeypatch.setattr(chatdemo, "_rooms", lambda *a, **k: {})
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    def local_cfg(home):
        cfg = yaml.safe_load((home / "config" / "settings.yaml").read_text())
        return cfg["web"]["admin"]

    home = chatdemo.prepare(live, chat)
    admin = local_cfg(home)
    assert admin["local"]["enabled"] is True and admin["local"]["user"] == "operator"
    assert admin["local"]["password_env"] == "ADMIN_PASSWORD"
    assert admin["local"]["password_file"] == str(chat / ".env")
    assert admin["local"]["accounts"] == {"peter": "peter@huma.ai", "eliot": "eliot@huma.ai"}
    assert admin["emails"] == ["a@b.c", "operator@localhost"], "the existing email kept, appended once"
    out = capsys.readouterr().out
    assert "ghost -> ghost@demo.local is not under people:, skipped" in out
    assert f"no ADMIN_PASSWORD in {chat / '.env'}" in out

    (chat / ".env").write_text("MM_BOT_TOKEN=x\nADMIN_PASSWORD=abc123\n")
    home = chatdemo.prepare(live, chat)
    assert local_cfg(home)["emails"] == ["a@b.c", "operator@localhost"], "no duplicate on a second start"
    assert "no ADMIN_PASSWORD" not in capsys.readouterr().out
    hint = chatdemo.admin_hint(chat, 8091)
    assert "http://localhost:8091/admin" in hint and "operator, peter, eliot" in hint and "ADMIN_PASSWORD" in hint

"""The local chat demo runs the bot in the Podman pod; these are the parts that need no Podman."""
import subprocess
from pathlib import Path

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

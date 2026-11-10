"""`cormon chat-demo`: the bot on the local Mattermost (the cormon-chat repo), for testing and demos.

Runs in <home>/chat-demo/<team>, a copy like the test sandbox: its own database (seeded with the study model),
email on the simulator so nothing is mailed, and the chat on the local Mattermost, never the real Google Chat rooms.
Each Mattermost team is one customer's demo, with its own copy and its own memory banks, so no customer sees
another's alerts or facts. The chat repo's bot.yaml names the default team, each study's rooms and which study
person each demo user stands for. A page at localhost:8090 makes a new customer's team (setup.sh) and moves the
bot from one team to another, so a demo needs no command line.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import yaml

HOME_NAME = "chat-demo"


def _demo_settings(chat: Path) -> dict:
    f = chat / "bot.yaml"
    if not f.exists():
        sys.exit(f"{f} is missing: is {chat} the cormon-chat repo?")
    return yaml.safe_load(f.read_text()) or {}


def _mm(chat: Path):
    from .channels.mattermost import MattermostChannel
    env = dict(line.split("=", 1) for line in (chat / ".env").read_text().splitlines() if "=" in line)
    if not env.get("MM_BOT_TOKEN"):
        sys.exit(f"no MM_BOT_TOKEN in {chat / '.env'}: run ./up.sh and ./setup.sh in {chat} first")
    return MattermostChannel(url=env.get("MM_URL"), token=env["MM_BOT_TOKEN"])


def tag(team: str, default: str) -> str:
    """What the team's own people have after their names (crc-acme): none in the default team. Same rule as
    setup.sh: the team without -demo, cut to 11 letters, since a Mattermost username has at most 22."""
    return "" if team == default else "-" + team.removesuffix("-demo")[:11].rstrip("-")


def people(demo: dict, team: str) -> dict:
    """Demo address -> study person, for the team's own people (bot.yaml lists the default team's)."""
    t = tag(team, demo["team"])
    return {(k.replace("@", f"{t}@", 1) if t else k): v for k, v in (demo.get("people") or {}).items()}


def _rooms(chat: Path, demo: dict, team: str) -> dict:
    """Each study's rooms in the team as spaces/<id>, looked up on the running Mattermost."""
    mm = _mm(chat)
    out: dict = {}
    for study_id, rooms in (demo.get("rooms") or {}).items():
        for key, name in rooms.items():
            try:
                out.setdefault(study_id, {})[key] = {"space": mm.find_room(team, name), "name": name}
            except Exception:
                sys.exit(f"no room {name} in the team {team}: run ./setup.sh {team} in {chat} first")
    return out


def prepare(live_root: Path, chat: Path, team: str | None = None) -> Path:
    demo = _demo_settings(chat)
    team = team or demo["team"]
    home = live_root / HOME_NAME / team
    old = live_root / HOME_NAME / "data"
    if team == demo["team"] and old.is_dir() and not home.exists():   # made before there was one copy per team
        home.mkdir(parents=True)
        old.rename(home / "data")
    (home / "data").mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / "config", ignore_errors=True)
    shutil.copytree(live_root / "config", home / "config")
    path = home / "config" / "settings.yaml"
    cfg = yaml.safe_load(path.read_text()) or {}
    cfg.setdefault("channels", {}).update({"email": "sim", "teams": "mattermost"})
    # the chat server is on this laptop, so reading it often is free; only the studies with a demo room run
    cfg.setdefault("system", {}).update({"poll_seconds": 3, "chat_poll_seconds": 3,
                                         "studies": list((demo.get("rooms") or {}).keys())})
    # the alert pages on this laptop only; memory in the laptop's Hindsight (deploy/hindsight.sh), in the team's banks
    cfg.setdefault("web", {}).update({"enabled": True, "host": "127.0.0.1", "port": 8091,
                                      "base_url": "http://localhost:8091"})
    cfg.setdefault("memory", {}).update({"enabled": True, "bank_prefix": f"demo-{team}-"})
    # the demo shows every new feature; the live bot gets each one when it is switched on there (docs/features.md)
    cfg["features"] = {name: True for name in cfg.get("features") or {}}
    if demo.get("bot_name"):   # the name people see in the chat (bot.yaml), so email and the admin page agree
        cfg.setdefault("bot", {})["name"] = demo["bot_name"]
    # a report kept out of the room also goes to the reader's 1-on-1 chat with the bot, not only to email
    cfg["mattermost"] = {"env_file": str(chat / ".env"), "team": team, "people": people(demo, team),
                         "rooms": _rooms(chat, demo, team), "reports_by_chat": True}
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    for name in ("studies", ".env", "secrets"):
        link, target = home / name, live_root / name
        if target.exists() and not link.exists():
            link.symlink_to(target)
    return home


LOGINS = ("crc", "pi", "cra", "sponsor", "medmonitor")   # the study roles; eliot, peter and john are testers
PAGE_PORT = 8090
PAGE = f"http://localhost:{PAGE_PORT}"


def model_seed(live_root: Path) -> Path:
    """A new download has no database of its own, so the demo starts from the study model in the repo
    (studies/model/model.sql: the live rules, without any message or patient data)."""
    seed = live_root / HOME_NAME / "model-seed.db"
    seed.unlink(missing_ok=True)
    c = sqlite3.connect(seed)
    c.executescript((live_root / "studies" / "model" / "model.sql").read_text())
    c.close()
    return seed


def slug(name: str) -> str:
    """The team's Mattermost name from the customer name typed on the page: "Acme Pharma" -> acme-pharma-demo."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50].strip("-")
    return f"{s}-demo" if s and not s.endswith("demo") else s


class Demo:
    """The bot running on one team at a time, and the page that makes a new team or moves the bot to another."""

    def __init__(self, live: Path, chat: Path):
        self.live, self.chat = live, chat
        self.default = _demo_settings(chat)["team"]
        self.child: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.busy = self.error = ""

    @property
    def team(self) -> str:
        f = self.live / HOME_NAME / "active_team"
        return f.read_text().strip() if f.exists() else self.default

    def start(self, team: str, fresh: bool = False) -> None:
        from . import db, settings
        home = prepare(self.live, self.chat, team)
        env = dict(os.environ, CORMON_HOME=str(home), CORMON_OPERATOR=os.environ.get("CORMON_OPERATOR", "demo"))
        if fresh or not (home / settings.get("system.data_dir", "data") / "cormon.db").exists():
            seed = db.db_path() if db.db_path().exists() else model_seed(self.live)
            if subprocess.call([sys.executable, "-m", "cormon.chatdemo", "--seed-from", str(seed)], env=env):
                raise RuntimeError("could not make the demo database")
        self.child = subprocess.Popen([sys.executable, "-m", "cormon.cli", "run"], env=env)
        (self.live / HOME_NAME / "active_team").write_text(team)
        print(f"the bot is on team {team} (email goes to the simulator); demo page {PAGE}")

    def stop(self) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(30)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()

    def use(self, team: str) -> None:
        with self.lock:
            self.error = ""
            self.stop()
            try:
                self.start(team)
            except (Exception, SystemExit) as e:
                self.error = f"Could not move the bot to {team}: {e}. It is back on {self.team}."
                self.start(self.team)

    def create(self, name: str) -> None:
        team = slug(name)
        if not team:
            self.error = "Type the customer's name, with at least one letter or digit."
            return
        self.busy, self.error = f"Making {name}. This takes about a minute.", ""
        try:
            r = subprocess.run(["./setup.sh", team, f"{name.strip()} demo"], cwd=self.chat, capture_output=True,
                               text=True, timeout=600)
            if r.returncode:
                self.error = f"Could not make {name}: {(r.stderr or r.stdout).strip()[-300:]}"
                return
            self.use(team)
        finally:
            self.busy = ""

    def teams(self) -> list[dict]:
        try:
            mm = _mm(self.chat)
            return sorted(({"name": t["name"], "label": t.get("display_name") or t["name"]}
                           for t in mm._get("/users/me/teams")), key=lambda t: t["label"].lower())
        except (Exception, SystemExit) as e:
            self.error = self.error or f"Could not read the teams from Mattermost: {str(e)[:200]}"
            return []

    def page(self) -> str:
        now = self.team
        rows = "".join(
            f"<tr><td>{html.escape(t['label'])}</td><td><code>{html.escape(t['name'])}</code></td>"
            f"<td><code>{html.escape(', '.join(u + tag(t['name'], self.default) for u in LOGINS))}</code></td><td>"
            + ("<b>The bot is here</b>" if t["name"] == now else
               f"<form method=post action=/use><input type=hidden name=team value=\"{html.escape(t['name'])}\">"
               f"<button {'disabled' if self.busy else ''}>Use this one</button></form>")
            + "</td></tr>" for t in self.teams())
        note = (f"<p class=busy>{html.escape(self.busy)}</p>" if self.busy else "") + \
               (f"<p class=err>{html.escape(self.error)}</p>" if self.error else "")
        again = "<meta http-equiv=refresh content=5>" if self.busy else ""
        return f"""<!doctype html><html><head><meta charset=utf-8><title>Demo teams</title>{again}
<style>body{{font:15px system-ui;margin:40px;max-width:760px}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.busy{{color:#555}}.err{{color:#b00}}
input[type=text]{{padding:6px;width:300px}}button{{padding:6px 12px}}</style></head><body>
<h1>Demo teams</h1>
<p>Each customer gets their own team in Mattermost, so no customer sees another's demo.
The bot works in one team at a time. Open Mattermost at <a href="http://localhost:8065">localhost:8065</a>.</p>
{note}
<table><tr><th>Customer</th><th>Team</th><th>Log in as (same password)</th><th></th></tr>{rows}</table>
<h2>New demo</h2>
<form method=post action=/new><input type=text name=name placeholder="Customer name, like Acme Pharma" required>
<button {'disabled' if self.busy else ''}>Create</button></form>
<p>It makes the team, the site room and the team's own demo people, adds the bot, and moves the bot there.
Each team has its own people, so no customer sees another's 1-on-1 chats with the bot.</p>
</body></html>"""


def _handler(demo: Demo):
    class Page(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: str = "", where: str | None = None):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if where:
                self.send_header("Location", where)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/":
                return self._send(404, "not found")
            self._send(200, demo.page())

        def do_POST(self):
            # only the page itself: another site open in the browser cannot press these buttons
            if (self.headers.get("Origin") or "").rstrip("/") not in (PAGE, f"http://127.0.0.1:{PAGE_PORT}"):
                return self._send(403, "refused")
            size = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(size, 2000)).decode()).items()}
            if not demo.busy and self.path == "/use" and form.get("team") in {t["name"] for t in demo.teams()}:
                threading.Thread(target=demo.use, args=(form["team"],), daemon=True).start()
                time.sleep(2)
            elif not demo.busy and self.path == "/new" and form.get("name", "").strip():
                demo.busy = "Starting..."
                threading.Thread(target=demo.create, args=(form["name"][:80],), daemon=True).start()
            self._send(303, where="/")
    return Page


def main(chat_dir: str, fresh: bool = False, team: str | None = None) -> int:
    """Called in the live home: runs the bot on a team (its own copy, seeded once) and the demo page beside it."""
    from . import settings
    chat = Path(chat_dir).expanduser().resolve()
    live = Path(settings.ROOT)
    if HOME_NAME in live.parts:
        sys.exit("already in a chat-demo copy: run `cormon chat-demo` from the bot's own folder")
    (live / HOME_NAME).mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # stopped from outside: the bot stops too
    demo = Demo(live, chat)
    demo.start(team or demo.team, fresh)
    server = ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), _handler(demo))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Ctrl-C stops it. Open {PAGE} to make a new demo team or move the bot to another")
    try:
        while True:
            time.sleep(1)
            with demo.lock:
                if demo.child and demo.child.poll() is not None:
                    return demo.child.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        demo.stop()


if __name__ == "__main__":
    from .testbench import _fresh_db
    _fresh_db(Path(sys.argv[sys.argv.index("--seed-from") + 1]))
    print("made the demo database (study model copied, no messages)")

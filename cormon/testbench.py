"""Run a folder of test messages through a sandbox copy and score what the bot found.

    cormon test my-emails/                 # every .txt, .eml and .csv in the folder
    cormon test my-emails/ --out results/  # also write results.csv and summary.json there

Nothing is sent and the live data is untouched: the run happens in <home>/sandbox, a copy that uses the
file-based simulator for every channel and starts each run from an empty database with the live study model.

Input formats (mix freely):

  .txt   optional header lines, a blank line, then the body:
             Subject: MAP-0301 randomized
             From: crc                 (crc = site coordinator, cra = monitor, or an email address)
             Expect: eligibility       (a pattern class, several separated by commas, none, or out_of_scope)
             Participant: MAP-0301     (optional: the finding must be on this participant)

             We randomized MAP-0301 today. eGFR was 41.
  .eml   a real email file; the study address is added to Cc if no study address is on it.
         Expectations can go in X-Expect / X-Participant headers.
  .csv   one message per row, columns: id, subject, body, from, expect, participant (only body is required).
  .json  a chat message (Teams or Google Chat), posted in the study room or sent to the bot directly:
             {"text": "MAP-0301 randomized today, eGFR 41", "from": "crc", "chat": "room",
              "expect": "eligibility", "participant": "MAP-0301"}
         "chat" is "room" (the default) or "direct"; only "text" is required.
"""

from __future__ import annotations

import argparse
import csv
import email
import email.policy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from datetime import datetime
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path

import yaml

SEED_TABLES = ("documents", "doc_pages", "rules", "allowances", "guide", "rule_decisions", "llm_cache")


@dataclass
class Case:
    name: str
    subject: str
    body: str = ""
    sender: str = "crc"
    expect: set[str] | None = None
    participant: str | None = None
    raw: bytes | None = None
    chat: str | None = None          # room | direct, for a chat message instead of an email
    date: str | None = None          # when the message was sent ("Mon, 21 Sep 2026"); default now
    result: dict = field(default_factory=dict)


# ---------------------------------------------------------------- reading the folder

def _expect(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    labels = {v.strip().lower() for v in value.replace(";", ",").split(",") if v.strip()}
    return set() if labels == {"none"} else labels - {"none"}


def _read_txt(path: Path) -> Case:
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    head, body = {}, text
    first, _, rest = text.partition("\n\n")
    lines = [ln for ln in first.split("\n") if ln.strip()]
    if lines and all(":" in ln and ln.split(":", 1)[0].strip().lower() in
                     ("subject", "from", "expect", "participant") for ln in lines):
        head = {ln.split(":", 1)[0].strip().lower(): ln.split(":", 1)[1].strip() for ln in lines}
        body = rest
    return Case(name=path.name, subject=head.get("subject") or path.stem.replace("_", " "), body=body.strip(),
                sender=head.get("from") or "crc", expect=_expect(head.get("expect")),
                participant=head.get("participant") or None)


def _read_eml(path: Path) -> Case:
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return Case(name=path.name, subject=str(msg.get("Subject") or path.stem), raw=raw,
                expect=_expect(msg.get("X-Expect")), participant=msg.get("X-Participant") or None)


def _read_csv(path: Path) -> list[Case]:
    out = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for n, row in enumerate(csv.DictReader(fh), 1):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("body"):
                continue
            out.append(Case(name=f"{path.name}#{row.get('id') or n}", subject=row.get("subject") or f"message {n}",
                            body=row["body"], sender=row.get("from") or "crc", expect=_expect(row.get("expect")),
                            participant=row.get("participant") or None, date=row.get("date") or None))
    return out


def _read_json(path: Path) -> Case:
    d = json.loads(path.read_text(encoding="utf-8"))
    chat = (d.get("chat") or "room").strip().lower()
    return Case(name=path.name, subject=d.get("subject") or d["text"][:60], body=d["text"].strip(),
                sender=d.get("from") or "crc", expect=_expect(d.get("expect")),
                participant=d.get("participant") or None, chat="direct" if chat == "direct" else "room")


def read_folder(folder: Path) -> list[Case]:
    files = sorted(p for p in folder.rglob("*") if p.is_file() and not p.name.startswith("."))
    cases: list[Case] = []
    for p in files:
        ext = p.suffix.lower()
        if ext == ".txt":
            cases.append(_read_txt(p))
        elif ext == ".eml":
            cases.append(_read_eml(p))
        elif ext == ".csv":
            cases.extend(_read_csv(p))
        elif ext == ".json":
            cases.append(_read_json(p))
    return cases


# ---------------------------------------------------------------- the sandbox copy

def prepare_sandbox(live_root: Path) -> Path:
    """A copy of the live config that only uses the simulator; studies, .env and secrets are shared read-only."""
    sb = live_root / "sandbox"
    (sb / "data").mkdir(parents=True, exist_ok=True)
    shutil.rmtree(sb / "config", ignore_errors=True)
    shutil.copytree(live_root / "config", sb / "config")
    path = sb / "config" / "settings.yaml"
    cfg = yaml.safe_load(path.read_text()) or {}
    cfg.setdefault("channels", {}).update({"email": "sim", "teams": "sim"})
    cfg.setdefault("web", {}).update({"enabled": False, "base_url": "(test run: no web page)"})
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    for name in ("studies", ".env", "secrets"):
        link, target = sb / name, live_root / name
        if target.exists() and not link.exists():
            link.symlink_to(target)
    return sb


def _fresh_db(seed_from: Path) -> None:
    """Empty database with the live study model (and the model-answer cache, so repeated runs are quick)."""
    from . import db, settings
    path = db.db_path()
    seed = path.with_suffix(".seed.db")
    keep_cache = path.with_suffix(".cache.db")
    _snapshot(seed_from, seed)
    if path.exists():
        _snapshot(path, keep_cache)
    for ext in ("", "-wal", "-shm"):
        Path(str(path) + ext).unlink(missing_ok=True)
    shutil.rmtree(settings.data_dir() / "sim", ignore_errors=True)
    c = db.conn()
    for src, tables in ((seed, SEED_TABLES), (keep_cache, ("llm_cache",))):
        if not src.exists():
            continue
        c.execute("ATTACH DATABASE ? AS seed", (str(src),))
        for tbl in tables:
            cols = [r[1] for r in c.execute(f"PRAGMA main.table_info({tbl})")]
            have = {r[1] for r in c.execute(f"PRAGMA seed.table_info({tbl})")}
            names = ", ".join(col for col in cols if col in have)
            if names:
                c.execute(f"INSERT OR IGNORE INTO {tbl} ({names}) SELECT {names} FROM seed.{tbl}")
        c.execute("DETACH DATABASE seed")
        src.unlink(missing_ok=True)


def _snapshot(src: Path, dest: Path) -> None:
    """Consistent copy of a database another process may be writing to."""
    dest.unlink(missing_ok=True)
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


# ---------------------------------------------------------------- running one case

def _sender(study, who: str) -> tuple[str, str]:
    """(From, To) for a role name or an address; the other party is the opposite role."""
    def addr(role):
        p = next(iter(study.people_with_role(role)), None)
        return f"{p.name} <{p.email}>" if p and p.email else f"{role} <{role}@example.org>"
    who = (who or "crc").strip()
    if "@" in who:
        return who, addr("cra")
    role = who.lower()
    return addr(role), addr("cra" if role != "cra" else "crc")


def _date(when: str | None) -> str:
    if not when:
        return formatdate(localtime=True)
    from dateutil import parser as dtparser
    from email.utils import format_datetime
    dt = dtparser.parse(when, default=datetime.now().replace(hour=9, minute=0, second=0, microsecond=0))
    return format_datetime(dt.astimezone() if dt.tzinfo is None else dt)


def _build(study, case: Case) -> bytes:
    if case.raw is not None:
        msg = email.message_from_bytes(case.raw, policy=email.policy.default)
        on = [a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))]
        if study.alias.lower() not in on and not any(a.startswith("bot+") for a in on):
            cc = msg.get("Cc")
            del msg["Cc"]
            msg["Cc"] = f"{cc}, {study.alias}" if cc else study.alias
        if not msg.get("Message-ID"):
            msg["Message-ID"] = make_msgid(domain="test.example")
        if not msg.get("Date"):
            msg["Date"] = formatdate(localtime=True)
        return bytes(msg)
    frm, to = _sender(study, case.sender)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Cc"] = frm, to, study.alias
    msg["Subject"] = case.subject
    msg["Date"] = _date(case.date)
    msg["Message-ID"] = make_msgid(domain="test.example")
    msg.set_content(case.body + "\n")
    return bytes(msg)


def _post_chat(study, case: Case, inbox: Path) -> str:
    """Drop the case into the chat simulator; returns the message's external id."""
    (name, addr), = getaddresses([_sender(study, case.sender)[0]])
    ext = f"test-chat:{time.time_ns()}"
    (inbox / f"case-{time.time_ns()}.json").write_text(json.dumps({
        "id": ext, "study_id": study.study_id, "channel": case.chat, "from_email": addr, "from_name": name,
        "text": case.body}))
    return ext


def run_case(study, case: Case) -> None:
    from . import db, service, settings
    before_out = db.q1("SELECT COALESCE(MAX(id), 0) m FROM outbox")["m"]
    t0 = time.time()
    if case.chat:
        inbox = settings.data_dir() / "sim" / "teams_inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        ext = _post_chat(study, case, inbox)
        service.tick(log=lambda *_: None)
        m = db.q1("SELECT id, study_id, status, error FROM messages WHERE channel='teams' AND external_id=?", (ext,))
    else:
        raw = _build(study, case)
        rfc = email.message_from_bytes(raw, policy=email.policy.default).get("Message-ID", "").strip()
        inbox = settings.data_dir() / "sim" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"case-{time.time_ns()}.eml").write_bytes(raw)
        service.tick(log=lambda *_: None)
        m = db.q1("SELECT id, study_id, status, error FROM messages WHERE rfc_message_id=? ORDER BY id DESC", (rfc,))
    found = [dict(r) for r in db.q("SELECT id, kind, pattern_class, tier, participant_id, title, reasoning FROM items "
                                   "WHERE first_message_id=?", (m["id"],))] if m else []
    classes = {i["pattern_class"] for i in found if i["kind"] == "finding" and i["pattern_class"]}
    if any(i["kind"] == "out_of_scope" for i in found):
        classes.add("out_of_scope")
    sent = db.q("SELECT purpose, destination, to_addrs FROM outbox WHERE id>? AND purpose != 'digest'", (before_out,))
    case.result = {
        "message_id": m["id"] if m else None,
        "status": (m["status"] if m else "not captured") + (f": {m['error']}" if m and m["error"] else ""),
        "got": classes,
        "items": found,
        "sent": [f"{o['purpose']} -> {', '.join(db.unjs(o['to_addrs']) or []) or o['destination']}" for o in sent],
        "pids": {i["participant_id"] for i in found if i["kind"] in ("finding", "out_of_scope")},
        "seconds": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- scoring

def verdict(case: Case) -> bool | None:
    if case.expect is None:
        return None
    got = case.result["got"]
    if case.expect != got:
        return False
    return not (case.participant and case.expect and case.participant.upper() not in
                {p.upper() for p in case.result["pids"] if p})


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"precision": p, "recall": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}


def score(cases: list[Case]) -> dict:
    graded = [c for c in cases if c.expect is not None]
    if not graded:
        return {}
    labels = sorted({lbl for c in graded for lbl in c.expect | c.result["got"]})
    per = {}
    for lbl in labels:
        tp = sum(1 for c in graded if lbl in c.expect and lbl in c.result["got"])
        fp = sum(1 for c in graded if lbl not in c.expect and lbl in c.result["got"])
        fn = sum(1 for c in graded if lbl in c.expect and lbl not in c.result["got"])
        per[lbl] = _prf(tp, fp, fn)
    problem = lambda s: bool(s - {"out_of_scope"})  # noqa: E731
    alert = _prf(sum(1 for c in graded if problem(c.expect) and problem(c.result["got"])),
                 sum(1 for c in graded if not problem(c.expect) and problem(c.result["got"])),
                 sum(1 for c in graded if problem(c.expect) and not problem(c.result["got"])))
    right = sum(1 for c in graded if verdict(c))
    wrong = [c for c in graded if verdict(c) is False]
    kinds = {
        "missed": sum(1 for c in wrong if problem(c.expect) and not problem(c.result["got"])),
        "false_alarm": sum(1 for c in wrong if not problem(c.expect) and problem(c.result["got"])),
        "wrong_box": sum(1 for c in wrong if problem(c.expect) and problem(c.result["got"])
                         and not (c.expect - {"out_of_scope"}) & c.result["got"]),
    }
    kinds["other"] = len(wrong) - sum(kinds.values())
    return {"graded": len(graded), "right": right, "accuracy": right / len(graded), "alert": alert, "per_label": per,
            "wrong_kinds": kinds}


def _fmt(x) -> str:
    return "  -  " if x is None else f"{x:5.2f}"


def report(cases: list[Case], summary: dict) -> str:
    lines = []
    for n, c in enumerate(cases, 1):
        v = verdict(c)
        mark = {True: "RIGHT", False: "WRONG", None: "     "}[v]
        got = ",".join(sorted(c.result["got"])) or "none"
        exp = "" if c.expect is None else f" expected {','.join(sorted(c.expect)) or 'none'}"
        tiers = ",".join(i["tier"] for i in c.result["items"] if i["kind"] == "finding") or "-"
        msg = f"msg {c.result['message_id']}" if c.result.get("message_id") else "msg -"
        lines.append(f"{mark} {n:3d}. {c.name[:40]:40s} got {got:28s} tier {tiers:12s} {msg:8s}{exp}")
        for i in c.result["items"]:
            lines.append(f"            #{i['id']} {i['kind']} · {i['participant_id'] or '-'} · {i['title']}")
        if not c.result["status"].startswith("processed"):
            lines.append(f"            status: {c.result['status']}")
    if summary:
        a = summary["alert"]
        lines += ["", f"{summary['right']}/{summary['graded']} right ({100 * summary['accuracy']:.0f}%)",
                  "", "Raising an alert at all (any problem found vs none):",
                  f"  precision {_fmt(a['precision'])}  (of the alerts raised, how many were real)",
                  f"  recall    {_fmt(a['recall'])}  (of the real problems, how many were caught)",
                  f"  F1        {_fmt(a['f1'])}  (both together)"]
        k = summary.get("wrong_kinds")
        if k:
            lines += ["", "The wrong ones, by kind:",
                      f"  missed a real problem          {k['missed']}",
                      f"  alert on a clean email         {k['false_alarm']}",
                      f"  caught, but in the wrong box   {k['wrong_box']}",
                      f"  partly right (a label extra or missing) {k['other']}"]
        lines += ["", f"{'Per label':22s} precision  recall    F1    (tp/fp/fn)"]
        for lbl, s in summary["per_label"].items():
            lines.append(f"  {lbl:20s} {_fmt(s['precision'])}     {_fmt(s['recall'])}  {_fmt(s['f1'])}"
                         f"   ({s['tp']}/{s['fp']}/{s['fn']})")
        wrong = [c for c in cases if verdict(c) is False]
        if wrong:
            lines += ["", "Wrong ones:"] + [
                f"  - {c.name}: expected {','.join(sorted(c.expect)) or 'none'}, "
                f"got {','.join(sorted(c.result['got'])) or 'none'}" for c in wrong]
    return "\n".join(lines)


def write_out(out: Path, cases: list[Case], summary: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with (out / "results.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["case", "subject", "expected", "got", "verdict", "tiers", "participants", "titles", "reasoning",
                    "sent", "message_id", "item_ids", "seconds", "status"])
        for c in cases:
            v = verdict(c)
            w.writerow([c.name, c.subject, "" if c.expect is None else ",".join(sorted(c.expect)) or "none",
                        ",".join(sorted(c.result["got"])) or "none", {True: "right", False: "wrong", None: ""}[v],
                        ",".join(i["tier"] for i in c.result["items"]),
                        ",".join(sorted(p for p in c.result["pids"] if p)),
                        " | ".join(i["title"] for i in c.result["items"]),
                        " | ".join(i.get("reasoning") or "" for i in c.result["items"]), " | ".join(c.result["sent"]),
                        c.result.get("message_id") or "", ",".join(str(i["id"]) for i in c.result["items"]),
                        c.result["seconds"], c.result["status"]])
    from . import llm
    summary["llm_usage"] = llm.USAGE
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=list))


# ---------------------------------------------------------------- entry points

def child_main(argv: list[str]) -> None:
    """Runs inside the sandbox (CORMON_HOME=<home>/sandbox)."""
    p = argparse.ArgumentParser()
    p.add_argument("folder")
    p.add_argument("--out")
    p.add_argument("--seed-from", required=True)
    p.add_argument("--study")
    args = p.parse_args(argv)
    from . import config, settings
    if settings.get("channels.email") != "sim" or settings.get("channels.teams") not in ("sim", "off"):
        sys.exit(f"{settings.ROOT} is not a sandbox (channels are not 'sim'); refusing to run")
    cases = read_folder(Path(args.folder))
    if not cases:
        sys.exit(f"no .txt, .eml, .csv or .json messages found in {args.folder}")
    _fresh_db(Path(args.seed_from))
    study = config.study(args.study) if args.study else next(iter(config.studies().values()))
    print(f"running {len(cases)} message(s) through the sandbox (nothing is sent)\n", flush=True)
    for n, c in enumerate(cases, 1):
        try:
            run_case(study, c)
        except Exception as e:  # one broken case must not stop the run
            c.result = {"got": set(), "items": [], "sent": [], "pids": set(), "seconds": 0,
                        "message_id": None, "status": f"error: {e}"}
        v = verdict(c)
        print(f"  {n:3d}/{len(cases)} {c.name[:50]:50s} {c.result['seconds']:5.1f}s "
              f"{ {True: 'right', False: 'WRONG', None: ''}[v]}", flush=True)
    summary = score(cases)
    print("\n" + report(cases, summary))
    if args.out:
        write_out(Path(args.out), cases, summary)
        print(f"\nwrote {Path(args.out) / 'results.csv'} and summary.json")


def main(folder: str, out: str | None = None, study: str | None = None) -> int:
    """Called by `cormon test` in the live home: prepares the sandbox and runs the folder in a child process."""
    from . import db, settings
    src = Path(folder).resolve()
    if not src.is_dir():
        sys.exit(f"{folder} is not a folder")
    live = Path(settings.ROOT)
    if settings.get("channels.email") == "sim":
        sb, seed = live, db.db_path()   # already a practice or sandbox copy: run in place
    else:
        sb, seed = prepare_sandbox(live), db.db_path()
    env = dict(os.environ, CORMON_HOME=str(sb), CORMON_OPERATOR=os.environ.get("CORMON_OPERATOR", "test"))
    cmd = [sys.executable, "-m", "cormon.testbench", str(src), "--seed-from", str(seed)]
    if out:
        cmd += ["--out", str(Path(out).resolve())]
    if study:
        cmd += ["--study", study]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    child_main(sys.argv[1:])

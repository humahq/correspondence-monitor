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
         A story is one .json file with several messages in order, sharing patient memory (CORMON_TEST_MEMORY=1),
         each with optional checks on the answer sent and on what memory holds once it is saved:
             {"story": "gout across threads", "messages": [
                 {"text": "...", "date": "2026-10-01 10:00", "memory": {"1001-014": {"has": ["gout"]}}},
                 {"text": "...?", "chat": "direct", "answer_has": ["gout", "1 Oct|01 Oct"], "answer_lacks": ["1001-022"]}]}
         A story message may be an email instead ("chat": "email", with an optional "subject").
         "a|b" passes on either word; case does not matter. In "memory", a participant code (or "unlinked", the
         facts with no participant) takes "has", "lacks" and "count" (how many current facts). The next story starts
         from an empty memory.
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
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, getaddresses, make_msgid
from pathlib import Path

import yaml

SEED_TABLES = ("documents", "doc_pages", "rules", "allowances", "guide", "rule_decisions", "llm_cache")
# The date a test message without its own date was sent. The date is part of what the AI is asked, so a fixed
# one lets a run reuse the kept AI answers of the last run; a change to the code or prompts still asks afresh.
TEST_SENT = datetime(2026, 10, 29, 9, 0, tzinfo=timezone(timedelta(hours=-4)))


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
    date: str | None = None          # when the message was sent ("Mon, 21 Sep 2026"); default TEST_SENT
    story: str | None = None         # the story file this message is part of
    checks: dict = field(default_factory=dict)   # answer_has, answer_lacks, memory (see the module docstring)
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


def _chat_case(name: str, d: dict, story: str | None = None) -> Case:
    chat = (d.get("chat") or "room").strip().lower()
    return Case(name=name, subject=d.get("subject") or d["text"][:60], body=d["text"].strip(),
                sender=d.get("from") or "crc", expect=_expect(d.get("expect")),
                participant=d.get("participant") or None,
                chat=None if chat == "email" else "direct" if chat == "direct" else "room",
                date=d.get("date") or None, story=story,
                checks={k: d[k] for k in ("answer_has", "answer_lacks", "memory") if k in d})


def _read_json(path: Path) -> list[Case]:
    d = json.loads(path.read_text(encoding="utf-8"))
    if "messages" not in d:
        return [_chat_case(path.name, d)]
    return [_chat_case(f"{path.name}#{n}", m, story=path.name) for n, m in enumerate(d["messages"], 1)]


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
            cases.extend(_read_json(p))
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
    cfg.setdefault("memory", {})["bank_prefix"] = "test-"
    if os.environ.get("CORMON_TEST_MEMORY") == "1":
        cfg["memory"]["enabled"] = True
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


def _sent(when: str | None) -> datetime:
    if not when:
        return TEST_SENT
    from dateutil import parser as dtparser
    dt = dtparser.parse(when, default=datetime.now().replace(hour=9, minute=0, second=0, microsecond=0))
    return dt.astimezone() if dt.tzinfo is None else dt


def _date(when: str | None) -> str:
    return format_datetime(_sent(when))


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
            msg["Date"] = format_datetime(TEST_SENT)
        return bytes(msg)
    frm, to = _sender(study, case.sender)
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Cc"] = frm, to, study.alias
    msg["Subject"] = case.subject
    msg["Date"] = _date(case.date)
    msg["Message-ID"] = make_msgid(domain="test.example")
    msg.set_content(case.body + "\n")
    return bytes(msg)


_direct_chats: dict[tuple[str, str], str] = {}   # (story, sender): the private chat's first message


def _post_chat(study, case: Case, inbox: Path) -> str:
    """Drop the case into the chat simulator; returns the message's external id. A story's private chat messages
    from one sender are one conversation, as they are in Teams, so a follow-up ("and for 1001-225?") has the
    messages before it."""
    (name, addr), = getaddresses([_sender(study, case.sender)[0]])
    ext = f"test-chat:{time.time_ns()}"
    chat = (case.story, addr) if case.story and case.chat == "direct" else None
    first = _direct_chats.setdefault(chat, ext) if chat else None
    (inbox / f"case-{time.time_ns()}.json").write_text(json.dumps({
        "id": ext, "study_id": study.study_id, "channel": case.chat, "from_email": addr, "from_name": name,
        "text": case.body, "sent_at": _sent(case.date).isoformat(),
        **({"thread": f"chat:{first}"} if first else {})}))
    return ext


def _answers(message_id: int) -> tuple[str, str]:
    """The answers written to a message, sent or not (a test chat message has no thread to post the reply into):
    as said to the person, and the checked answer it was reworded from."""
    from . import db
    said, checked = [], []
    for r in db.q("SELECT details FROM audit_log WHERE action='question_answered' AND entity_type='message' "
                  "AND entity_id=? ORDER BY id", (str(message_id),)):
        for a in db.unjs(r["details"], {}).get("answers") or []:
            said.append(a.get("plain") or a.get("text") or "")
            checked.append(a.get("text") or "")
    return "\n".join(said), "\n".join(checked)


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
    sent = db.q("SELECT purpose, destination, to_addrs FROM outbox WHERE id>? AND purpose NOT IN ('digest','ops_warning')",
                (before_out,))
    said, checked = _answers(m["id"]) if m else ("", "")
    case.result = {
        "message_id": m["id"] if m else None,
        "status": (m["status"] if m else "not captured") + (f": {m['error']}" if m and m["error"] else ""),
        "got": classes,
        "items": found,
        "sent": [f"{o['purpose']} -> {', '.join(db.unjs(o['to_addrs']) or []) or o['destination']}" for o in sent],
        "answer": said,
        "answer_checked": checked,
        "pids": {i["participant_id"] for i in found if i["kind"] in ("finding", "out_of_scope")},
        "seconds": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- story checks

def _norm(s: str) -> str:
    return " ".join((s or "").replace("\u2019", "'").replace("\u2018", "'").lower().split())


def _has(text: str, want: str) -> bool:
    return any(_norm(w) in text for w in want.split("|") if w.strip())


def run_checks(study, case: Case) -> list[dict]:
    """The story checks of one message, after its memory saves are done: each {check, ok, saw}."""
    from . import hindsight
    out = []
    if "answer_has" in case.checks or "answer_lacks" in case.checks:
        ans = _norm(case.result.get("answer") or "")
        saw = (case.result.get("answer") or "(no answer sent)")[:600]
        for w in case.checks.get("answer_has") or []:
            out.append({"check": f"answer has '{w}'", "ok": _has(ans, w), "saw": saw})
        for w in case.checks.get("answer_lacks") or []:
            out.append({"check": f"answer lacks '{w}'", "ok": bool(ans) and not _has(ans, w), "saw": saw})
    for pid, want in (case.checks.get("memory") or {}).items():
        try:
            if pid == "unlinked":
                facts = hindsight.overview(study.study_id)["unlinked"]
            else:
                facts = [f for f in hindsight.participant_facts(study.study_id, pid) if not f["retired"]]
        except Exception as e:
            out.append({"check": f"memory of {pid}", "ok": False, "saw": f"memory could not be read: {e}"})
            continue
        words = [f["text"] + (f" (happened: {f['happened']})" if f.get("happened") else "") for f in facts]
        text = _norm(" || ".join(words))
        saw = " || ".join(words)[:600] or "(no facts)"
        for w in want.get("has") or []:
            out.append({"check": f"memory of {pid} has '{w}'", "ok": _has(text, w), "saw": saw})
        for w in want.get("lacks") or []:
            out.append({"check": f"memory of {pid} lacks '{w}'", "ok": not _has(text, w), "saw": saw})
        if "count" in want:
            out.append({"check": f"memory of {pid} holds {want['count']} fact(s)", "ok": len(facts) == want["count"],
                        "saw": f"{len(facts)}: {saw}"})
    return out


def story_summary(cases: list[Case]) -> dict:
    checked = [c for c in cases if c.result.get("checks")]
    if not checked:
        return {}
    stories: dict[str, bool] = {}
    for c in cases:
        if c.story:
            stories[c.story] = stories.get(c.story, True) and all(k["ok"] for k in c.result.get("checks") or [])
    every = [k for c in checked for k in c.result["checks"]]
    return {"checks": len(every), "checks_passed": sum(k["ok"] for k in every),
            "stories": len(stories), "stories_passed": sum(stories.values()),
            "failed_stories": sorted(s for s, ok in stories.items() if not ok)}


def memory_cost(messages: int) -> str:
    """What patient memory cost in this run: Hindsight's saves (its own AI, from the audit trail) and the bot's
    'which fact is newer' checks, in tokens, in total and per message read."""
    from . import db
    saves = [db.unjs(r["details"], {}).get("usage") or {} for r in
             db.q("SELECT details FROM audit_log WHERE action='memory_saved'")]
    newer = db.q1("SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(cached_input_tokens),0) ci, "
                  "COALESCE(SUM(output_tokens),0) o FROM llm_calls WHERE task='memory_newer' AND cached=0")
    tot = lambda k: sum(u.get(k) or 0 for u in saves)  # noqa: E731
    per = max(messages, 1)
    took = sorted(db.unjs(r["details"], {}).get("seconds") or 0 for r in
                  db.q("SELECT details FROM audit_log WHERE action='memory_searched'"))
    failed = db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='memory_search_failed'")["n"]
    fixed = db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='memory_tag_fixed'")["n"]
    unsure = db.q1("SELECT COUNT(*) n FROM audit_log WHERE action='memory_tag_unsure'")["n"]
    return "\n".join([
        f"Memory searches: {len(took)} done, {failed} failed or too slow"
        + (f"; middle {took[len(took) // 2]:.1f}s, slowest {took[-1]:.1f}s" if took else "")
        + f". Participant tags put right: {fixed}; left unsure: {unsure}",
        f"Memory cost: {len(saves)} saves to Hindsight, {tot('input_tokens'):,} input tokens "
        f"({tot('cached_tokens'):,} of them cached), {tot('output_tokens'):,} output; "
        f"{newer['n']} newer-fact checks, {newer['i']:,} input ({newer['ci']:,} cached), {newer['o']:,} output",
        f"  per message read: {(tot('input_tokens') + newer['i']) / per:,.0f} input "
        f"({(tot('cached_tokens') + newer['ci']) / per:,.0f} cached), "
        f"{(tot('output_tokens') + newer['o']) / per:,.0f} output tokens"])


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
        for k in c.result.get("checks") or []:
            if not k["ok"]:
                lines.append(f"            FAILED check: {k['check']}; saw: {k['saw'][:300]}")
    st = story_summary(cases)
    if st:
        lines += ["", f"Story checks: {st['checks_passed']}/{st['checks']} passed; whole stories right: "
                      f"{st['stories_passed']}/{st['stories']}"]
        if st["failed_stories"]:
            lines.append("  stories with a failed check: " + ", ".join(st["failed_stories"]))
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
                    "sent", "message_id", "item_ids", "seconds", "status", "failed_checks", "answer",
                    "answer_checked"])
        for c in cases:
            v = verdict(c)
            w.writerow([c.name, c.subject, "" if c.expect is None else ",".join(sorted(c.expect)) or "none",
                        ",".join(sorted(c.result["got"])) or "none", {True: "right", False: "wrong", None: ""}[v],
                        ",".join(i["tier"] for i in c.result["items"]),
                        ",".join(sorted(p for p in c.result["pids"] if p)),
                        " | ".join(i["title"] for i in c.result["items"]),
                        " | ".join(i.get("reasoning") or "" for i in c.result["items"]), " | ".join(c.result["sent"]),
                        c.result.get("message_id") or "", ",".join(str(i["id"]) for i in c.result["items"]),
                        c.result["seconds"], c.result["status"],
                        " | ".join(k["check"] for k in c.result.get("checks") or [] if not k["ok"]),
                        c.result.get("answer") or "", c.result.get("answer_checked") or ""])
    from . import llm, timing
    summary["llm_usage"] = llm.USAGE
    summary["stories"] = story_summary(cases)
    (out / "checks.json").write_text(json.dumps({c.name: c.result.get("checks") or [] for c in cases}, indent=2))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=list))
    (out / "timings.json").write_text(json.dumps(timing.report(limit=len(cases) * 2), indent=2))


def time_report() -> str:
    """Where this run's time went: the average read, each step's share, and the slowest AI tasks."""
    from . import timing
    rep = timing.report(limit=100000)
    reads = [m for m in rep["messages"] if m["seconds"] is not None]
    if not reads:
        return "Time: no reads recorded"
    avg = lambda xs: sum(xs) / len(xs) if xs else 0  # noqa: E731
    steps: dict[str, list[float]] = {}
    for m in reads:
        for k, v in m["steps"].items():
            steps.setdefault(k, []).append(v)
    lines = [f"Time: {len(reads)} reads, average {avg([m['seconds'] for m in reads]):.1f}s, "
             f"longest {max(m['seconds'] for m in reads):.1f}s; "
             f"average {avg([m['ai_calls'] for m in reads]):.1f} AI calls and "
             f"{avg([m['input_tokens'] for m in reads]):,.0f} input / {avg([m['output_tokens'] for m in reads]):,.0f}"
             f" output tokens per read",
             "  average seconds per step: " + ", ".join(
                 f"{k} {sum(v) / len(reads):.1f}" for k, v in sorted(steps.items(), key=lambda kv: -sum(kv[1]))
                 if sum(v) / len(reads) >= 0.05),
             "  slowest AI tasks (total seconds, calls, average): " + ", ".join(
                 f"{t['task']} {t['avg_seconds'] * (t['calls'] - t['cache_hits']):.0f}s/{t['calls'] - t['cache_hits']}"
                 f"/{t['avg_seconds']:.1f}s" for t in rep["tasks"][:6] if t["avg_seconds"])]
    return "\n".join(lines)


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
    # cases are separate stories unless the folder says they follow one another: a reused participant id must not
    # carry one case's dates into the next
    story = (Path(args.folder) / ".memory").exists()
    print(f"running {len(cases)} message(s) through the sandbox (nothing is sent)"
          f"{'; participant memory carries over between messages' if story else ''}\n", flush=True)
    from . import db, hindsight
    for n, c in enumerate(cases, 1):
        fresh = n == 1 or (c.story != cases[n - 2].story if c.story else not story)
        if fresh:
            db.ex("DELETE FROM patient_facts")
            if hindsight.enabled():
                hindsight.clear_test_bank(study.study_id)
        try:
            run_case(study, c)
            hindsight.flush()
            if c.checks:
                c.result["checks"] = run_checks(study, c)
        except Exception as e:  # one broken case must not stop the run
            c.result = {"got": set(), "items": [], "sent": [], "pids": set(), "seconds": 0,
                        "message_id": None, "status": f"error: {e}",
                        "checks": [{"check": "the message ran", "ok": False, "saw": str(e)}] if c.checks else []}
        v = verdict(c)
        failed = sum(not k["ok"] for k in c.result.get("checks") or [])
        print(f"  {n:3d}/{len(cases)} {c.name[:50]:50s} {c.result['seconds']:5.1f}s "
              f"{ {True: 'right', False: 'WRONG', None: ''}[v]}"
              f"{f' {failed} check(s) FAILED' if failed else ''}", flush=True)
    summary = score(cases)
    print("\n" + report(cases, summary))
    print("\n" + time_report())
    if hindsight.enabled():
        print("\n" + memory_cost(len(cases)))
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

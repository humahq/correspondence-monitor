"""Release gates: which AI test sets a change must pass, running them, and the pass marks (SDL-05, VAL-03,
VAL-05, CFG-02, CAP-09). The sets, marks and triggers are in config/gates.yaml.

    python -m cormon.gate plan [--base live] [--all]   which sets this change needs (JSON list)
    python -m cormon.gate home DIR                     make a test home in DIR that sends nothing
    CORMON_HOME=DIR python -m cormon.gate run SET OUT  run one set there and check its marks (exit 1 if below)
        [--baseline DIR]                               ... and compare each case with that earlier run (VAL-07)
    python -m cormon.gate last-fresh                   the newest passed fresh run on main (GitHub's API)
    python -m cormon.gate tasks [--base live]          every commit since base names its GitHub issue (SDL-02)

Model drift (VAL-07): the weekly run asks every set fresh (no kept answers) and compares each case, right or
wrong, with the last fresh run that passed. More changed cases than a set's changed_at_most fail it, and
deploy/release.sh stops unless the newest fresh run passed and is at most 8 days old.

A test home is the repo's config with every channel on the simulator, the repo's studies, and the study model
from studies/model/model.sql (the live rules, exported without any message or patient data). The AI answers it
gets are kept in DIR/cache.db between runs, so an unchanged input is not asked again.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def gates() -> dict:
    return yaml.safe_load((REPO / "config" / "gates.yaml").read_text())


def _flat(items) -> list[str]:
    out = []
    for i in items or []:
        out += _flat(i) if isinstance(i, list) else [i]
    return out


# ---------------------------------------------------------------- which sets a change needs

def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def changed_files(base: str) -> list[str] | None:
    """Files changed between base and the working tree's HEAD; None when base is not known (run everything)."""
    try:
        _git("rev-parse", "--verify", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        return None
    return [f for f in _git("diff", "--name-only", f"{base}...HEAD").splitlines() if f]


TESTED_SETTINGS = ("llm", "matching", "features", "language", "safety")  # = config.TESTED_SETTINGS; only pyyaml here


def _llm_block(text: str | None) -> dict:
    """The settings the gate tests: the models, how matching runs, and the feature switches."""
    data = yaml.safe_load(text or "") or {}
    return {k: data.get(k) for k in TESTED_SETTINGS}


def llm_settings_changed(base: str) -> bool:
    try:
        old = _git("show", f"{base}:config/settings.yaml")
    except subprocess.CalledProcessError:
        return True
    return _llm_block(old) != _llm_block((REPO / "config" / "settings.yaml").read_text())


def plan(files: list[str] | None, llm_changed: bool = False, cfg: dict | None = None) -> list[str]:
    cfg = cfg or gates()
    names = list(cfg["sets"])
    if files is None or llm_changed:
        return names
    hit = lambda pats: any(fnmatch.fnmatch(f, p) for f in files for p in _flat(pats))  # noqa: E731
    if hit(cfg.get("all_when")):
        return names
    return [n for n in names if hit(cfg["sets"][n].get("when"))]


# ---------------------------------------------------------------- a home that sends nothing

def make_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / "config", ignore_errors=True)
    shutil.copytree(REPO / "config", home / "config")
    path = home / "config" / "settings.yaml"
    cfg = yaml.safe_load(path.read_text()) or {}
    cfg.setdefault("channels", {}).update({"email": "sim", "teams": "sim"})
    cfg.setdefault("web", {}).update({"enabled": False, "base_url": "(test run: no web page)"})
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    if not (home / "studies").exists():
        (home / "studies").symlink_to(REPO / "studies")
    (home / "data").mkdir(exist_ok=True)
    seed = home / "seed.db"
    seed.unlink(missing_ok=True)
    c = sqlite3.connect(seed)
    c.executescript((REPO / "studies" / "model" / "model.sql").read_text())
    c.close()


def _with_cache(home: Path) -> Path:
    """The seed with the kept AI answers added, so the run's fresh database starts with both."""
    seed, cache = home / "seed.db", home / "cache.db"
    c = sqlite3.connect(seed)
    c.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, model TEXT, response TEXT, created_at TEXT)")
    if cache.exists():
        c.execute("ATTACH DATABASE ? AS kept", (str(cache),))
        c.execute("INSERT OR IGNORE INTO llm_cache (key, model, response, created_at) "
                  "SELECT key, model, response, created_at FROM kept.llm_cache")
        c.commit()
        c.execute("DETACH DATABASE kept")
    c.close()
    return seed


def _keep_cache(home: Path) -> None:
    from . import db
    out = home / "cache.db"
    out.unlink(missing_ok=True)
    c = db.conn()
    c.execute("ATTACH DATABASE ? AS kept", (str(out),))
    c.execute("CREATE TABLE kept.llm_cache AS SELECT key, model, response, created_at FROM main.llm_cache")
    c.execute("DETACH DATABASE kept")


# ---------------------------------------------------------------- running a set

def _run_folder(name: str, spec: dict, study: str, home: Path, out: Path) -> dict:
    from . import testbench
    testbench.child_main([str(REPO / spec["folder"]), "--out", str(out), "--seed-from", str(_with_cache(home)),
                          "--study", study])
    s = json.loads((out / "summary.json").read_text())
    a = s["alert"]
    got = {"recall": a["recall"], "precision": a["precision"], "accuracy": s["accuracy"],
           "right": f"{s['right']}/{s['graded']}"}
    if spec["marks"].get("recipients_from_config"):
        got["strangers"] = strangers(study, out / "results.csv")
    return got


def strangers(study_id: str, results_csv: Path) -> list[str]:
    """Email addresses the bot sent to that are not on the study's people list or the admin list."""
    from . import config, settings
    study = config.study(study_id)
    known = {p.email.lower() for p in study.people if p.email}
    known |= {e.lower() for e in _flat(settings.get("web.admin.emails"))}
    known.add(str(settings.get("mailbox.address", "")).lower())
    bad = set()
    with results_csv.open(newline="") as fh:
        for row in csv.DictReader(fh):
            for sent in filter(None, (row.get("sent") or "").split(" | ")):
                for addr in sent.split("->", 1)[-1].split(","):
                    addr = addr.strip().lower()
                    if "@" in addr and addr not in known:
                        bad.add(f"{row['case']}: {addr}")
    return sorted(bad)


def _run_questions(spec: dict, study: str, home: Path, out: Path) -> dict:
    from . import qabench, testbench
    testbench._fresh_db(_with_cache(home))
    s = qabench.run(study, REPO / spec["file"], out)
    n, t = s["n"] or 1, s["total"]
    return {"right_or_partly": (t["correct"] + t["partly"]) / n, "wrong": t["wrong"] / n,
            "correct": t["correct"] / n, "refused": t["refused"] / n}


def _run_wording(spec: dict, home: Path, out: Path) -> dict:
    from . import outcheck, testbench
    testbench._fresh_db(_with_cache(home))
    cases = json.loads((REPO / spec["file"]).read_text())
    rows = []
    for c in cases:
        got = outcheck.problems(c["text"], c["source"])
        ok = bool(got) == (c["want"] == "catch")
        rows.append({**c, "got": got, "ok": ok})
        print(f"  {'right' if ok else 'WRONG'} {c['id']} want {c['want']:5} got {got or 'pass'}", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    right = sum(r["ok"] for r in rows)
    return {"right": right / len(rows), "count": f"{right}/{len(rows)}",
            "wrong_ones": [r["id"] for r in rows if not r["ok"]]}


def _run_doctors(spec: dict, home: Path, out: Path) -> dict:
    from . import testbench
    testbench._fresh_db(_with_cache(home))
    script = REPO / spec["script"]
    subprocess.run([sys.executable, str(script)], check=True, env=dict(os.environ))
    rows = json.loads((script.parent / "real_world_run.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(script.parent / "real_world_run.json", out / "results.json")
    right = sum(r["ok"] for r in rows)
    return {"right": f"{right}/{len(rows)}", "wrong_ones": [r["id"] for r in rows if not r["ok"]]}


def check(name: str, spec: dict, got: dict) -> list[str]:
    """What is below its pass mark (empty: passed)."""
    m, bad = spec.get("marks") or {}, []
    for key in ("recall", "precision", "right_or_partly", "right"):
        if key in m and (got.get(key) is None or got[key] < m[key]):
            bad.append(f"{key} {_pct(got.get(key))} is below the pass mark {_pct(m[key])}")
    if "wrong_at_most" in m and got["wrong"] > m["wrong_at_most"]:
        bad.append(f"wrong {_pct(got['wrong'])} is above the most allowed, {_pct(m['wrong_at_most'])}")
    if m.get("recipients_from_config") and got.get("strangers"):
        bad.append("sent to someone not on the study's people list: " + "; ".join(got["strangers"]))
    return bad


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.0f}%" if isinstance(x, float) else str(x)


# ---------------------------------------------------------------- drift: the answers against the last fresh run

def outcomes(name: str, out: Path) -> dict[str, bool]:
    """Each case in a set's results folder: right (True) or wrong (False). A question counts as right when it
    is correct or partly correct, as in its pass mark."""
    if (out / "results.csv").exists():
        with (out / "results.csv").open(newline="") as fh:
            return {r["case"]: r["verdict"] == "right" for r in csv.DictReader(fh)}
    rows = json.loads((out / "results.json").read_text())
    if name == "questions":
        return {r["id"]: (r.get("grade") or {}).get("verdict") in ("correct", "partly") for r in rows}
    return {r["id"]: bool(r["ok"]) for r in rows}


def drift(name: str, out: Path, baseline: Path) -> dict:
    """The cases that went from right to wrong or from wrong to right since the baseline run (VAL-07)."""
    now, before = outcomes(name, out), outcomes(name, baseline)
    both = sorted(set(now) & set(before))
    changed = [c for c in both if now[c] != before[c]]
    return {"changed": len(changed) / len(both) if both else 0.0, "compared": len(both),
            "changed_ones": [f"{c} (now {'right' if now[c] else 'wrong'})" for c in changed]}


def drift_problems(spec: dict, got: dict) -> list[str]:
    m = spec.get("marks") or {}
    if "changed_at_most" in m and got.get("changed") is not None and got["changed"] > m["changed_at_most"]:
        return [(f"changed {_pct(got['changed'])} of the cases since the last fresh run, above the most allowed, "
                 f"{_pct(float(m['changed_at_most']))}")]
    return []


def last_fresh() -> int | None:
    """The newest fresh run on main (every set asked with no kept answers) whose gate passed: its run id. A fresh
    run leaves a "fresh-check" file; the weekly runs from before that count too."""
    runs = [a["workflow_run"] for a in _github("actions/artifacts?name=fresh-check&per_page=30")["artifacts"]
            if not a["expired"] and a["workflow_run"].get("head_branch") == "main"]
    runs += _github("actions/runs?event=schedule&branch=main&status=completed&per_page=10")["workflow_runs"]
    for r in sorted({r["id"]: r for r in runs}.values(), key=lambda r: r["id"], reverse=True):
        jobs = _github(f"actions/runs/{r['id']}/jobs?per_page=100")["jobs"]
        if any(j["name"] == "gate" and j["conclusion"] == "success" for j in jobs):
            return r["id"]
    return None


def run(name: str, out: Path, baseline: Path | None = None, baseline_run: str = "",
        accept_changes: bool = False) -> int:
    from . import settings
    if settings.get("channels.email") != "sim" or settings.get("channels.teams") not in ("sim", "off"):
        sys.exit(f"{settings.ROOT} is not a test home (channels are not 'sim'); refusing to run")
    cfg = gates()
    spec, study, home = cfg["sets"][name], cfg["study"], Path(settings.ROOT)
    out.mkdir(parents=True, exist_ok=True)
    if "folder" in spec:
        got = _run_folder(name, spec, study, home, out)
    elif name == "questions":
        got = _run_questions(spec, study, home, out)
    elif name == "wording":
        got = _run_wording(spec, home, out)
    elif name == "doctors":
        got = _run_doctors(spec, home, out)
    else:
        sys.exit(f"no runner for set {name}")
    _keep_cache(home)
    bad = check(name, spec, got)
    accepted = []
    if baseline and baseline.is_dir():
        got.update(drift(name, out, baseline))
        if accept_changes:
            accepted = drift_problems(spec, got)
        else:
            bad += drift_problems(spec, got)
    result = {"set": name, "what": spec["what"], "passed": not bad, "below": bad, "scores": got,
              "marks": spec.get("marks"), "ai_use": ai_use(), "baseline_run": baseline_run or None,
              "changes_accepted": accepted}
    (out / "gate.json").write_text(json.dumps(result, indent=1, default=str))
    text = summary_md(result)
    print("\n" + text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text + "\n")
    return 0 if not bad else 1


def ai_use() -> dict:
    """This run's AI calls and tokens (what it cost), and the answers taken from the kept ones (free)."""
    from . import llm
    total = {k: sum(u[k] for u in llm.USAGE.values()) for k in ("calls", "input", "cached_input", "output")}
    return {**total, "reused": llm.REUSED["answers"], "by_model": {m: dict(u) for m, u in llm.USAGE.items()}}


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f} M" if n >= 1e6 else f"{n / 1e3:.0f} k" if n >= 1e3 else str(n)


def summary_md(r: dict) -> str:
    lists = ("strangers", "wrong_ones", "changed_ones", "compared")
    shown = ", ".join(f"{k} {_pct(v)}" for k, v in r["scores"].items() if k not in lists)
    lines = [f"### {'PASSED' if r['passed'] else 'FAILED'}: {r['set']} ({r['what']})", "", f"Scores: {shown}"]
    lines += [f"- {b}" for b in r["below"]]
    lines += [f"- accepted by the person who ran it: {b}" for b in r.get("changes_accepted") or []]
    if r["scores"].get("wrong_ones"):
        lines.append(f"Wrong: {', '.join(r['scores']['wrong_ones'])}")
    if "compared" in r["scores"]:
        changed = r["scores"]["changed_ones"]
        lines.append(f"Compared with the last fresh run ({r.get('baseline_run') or 'given folder'}): "
                     f"{len(changed)} of {r['scores']['compared']} cases changed"
                     + (f": {', '.join(changed)}" if changed else ""))
    if u := r.get("ai_use"):
        lines.append(f"AI use: {u['calls']} paid calls ({_tokens(u['input'])} input tokens, of them "
                     f"{_tokens(u['cached_input'])} at the cheaper cached price; {_tokens(u['output'])} output), "
                     f"{u['reused']} answers taken free from the kept ones")
    return "\n".join(lines)


# ---------------------------------------------------------------- a merge of a passed pull request

def _github(path: str) -> dict | list:
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/{path}",
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def same_code_passed(sha: str = "HEAD") -> int | None:
    """The pull request run whose gate passed on exactly this code, or None. This commit (a merge of the pull
    request into main) holds the same files as the pull request's head, and the head already held everything
    main had before, so the pull request's run tested these very files: its AI sets need not run again."""
    tree, parent = _git("rev-parse", f"{sha}^{{tree}}").strip(), _git("rev-parse", f"{sha}^").strip()
    for pr in _github(f"commits/{_git('rev-parse', sha).strip()}/pulls"):
        head = pr["head"]["sha"]
        try:
            _git("fetch", "-q", "origin", head)
            if _git("rev-parse", f"{head}^{{tree}}").strip() != tree:
                continue
            _git("merge-base", "--is-ancestor", parent, head)
        except subprocess.CalledProcessError:
            continue
        runs = _github(f"actions/runs?head_sha={head}&event=pull_request&status=completed")["workflow_runs"]
        for run in sorted(runs, key=lambda r: r["run_number"], reverse=True)[:1]:
            jobs = _github(f"actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
            if any(j["name"] == "gate" and j["conclusion"] == "success" for j in jobs):
                return run["id"]
    return None


# ---------------------------------------------------------------- every change names its task (SDL-02)

TASK_REF = re.compile(r"(?<![\w/&])#(\d+)\b")


def commits(base: str, head: str = "HEAD") -> list[dict] | None:
    """The commits in base..head with their messages; None when base is not known. A commit a merge brought in
    also carries the merge's message ("merged_by"), so "Merge pull request #55" counts for the commits in it."""
    try:
        _git("rev-parse", "--verify", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        return None
    out = _git("log", "--format=%H%x1f%P%x1f%s%x1f%B%x1e", f"{base}..{head}")
    found = [dict(zip(("sha", "parents", "subject", "body"), c.strip("\n").split("\x1f", 3)))
             for c in out.split("\x1e") if c.strip()]
    by_sha = {c["sha"]: c for c in found}
    for m in found:
        parents = m.pop("parents").split()
        if len(parents) > 1:
            for sha in _git("rev-list", f"{parents[0]}..{parents[1]}").split():
                if sha in by_sha:
                    by_sha[sha]["merged_by"] = by_sha[sha].get("merged_by", "") + "\n" + (m["body"] or m["subject"])
    return found


def _github_task(n: int) -> bool:
    """The issue or pull request #n exists in this repository (GitHub's API, with the run's own token)."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/issues/{n}",
                                 headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def task_problems(found: list[dict], exists=None, pr: int | None = None) -> list[str]:
    """Each commit must name a GitHub issue or pull request (#12) that exists. A pull request's own number
    counts for the commits in it (its squash or merge commit on main names it too)."""
    known: dict[int, bool] = {}
    problems = []
    for c in found:
        text = (c["body"] or c["subject"]) + c.get("merged_by", "")
        refs = sorted({int(n) for n in TASK_REF.findall(text)} | ({pr} if pr else set()))
        if not refs:
            problems.append(f"{c['sha'][:8]} {c['subject'][:70]!r} names no task (add e.g. '#12' for issue 12)")
            continue
        if exists:
            for n in refs:
                if n not in known:
                    known[n] = exists(n)
            if not any(known[n] for n in refs):
                problems.append(f"{c['sha'][:8]} {c['subject'][:70]!r} names #{', #'.join(map(str, refs))}, "
                                "which is not an issue or pull request here")
    return problems


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m cormon.gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("tasks", help="every commit since base names a GitHub issue or pull request (SDL-02)")
    s.add_argument("--base", default="live")
    s.add_argument("--head", default="HEAD")
    s.add_argument("--pr", type=int, help="the pull request being checked")
    s = sub.add_parser("plan")
    s.add_argument("--base", default="live")
    s.add_argument("--all", action="store_true")
    s.add_argument("--reuse-pr", action="store_true",
                   help="no AI sets when a pull request's run already passed on exactly this code")
    s = sub.add_parser("home")
    s.add_argument("dir")
    s = sub.add_parser("run")
    s.add_argument("set")
    s.add_argument("out")
    s.add_argument("--baseline", help="the last fresh run's results folder for this set: compare each case (VAL-07)")
    s.add_argument("--baseline-run", default="", help="that run's id, for the summary")
    s.add_argument("--accept-changes", action="store_true",
                   help="more changes than allowed are shown but do not fail (a wanted change, e.g. a new model)")
    sub.add_parser("last-fresh", help="the newest fresh run on main whose gate passed (its id, or nothing)")
    a = p.parse_args(argv)
    if a.cmd == "plan":
        files = None if a.all else changed_files(a.base)
        sets = plan(files, files is not None and llm_settings_changed(a.base))
        reused = None
        if a.reuse_pr and sets:
            try:
                reused = same_code_passed()
            except Exception as e:  # GitHub not reachable: the sets run
                print(f"could not look for a passed pull request run ({e}); running the sets", file=sys.stderr)
        kept = []
        if reused:
            print(f"the AI sets {sets} passed on exactly this code in pull request run {reused}; not run again",
                  file=sys.stderr)
            sets, kept = [], sets
        print(json.dumps(sets))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
                # kept_sets, from_run: the pull request run whose kept AI answers become main's (no AI calls)
                fh.write(f"sets={json.dumps(sets)}\nkept_sets={json.dumps(kept)}\nfrom_run={reused or ''}\n")
    elif a.cmd == "tasks":
        found = commits(a.base, a.head)
        if found is None:
            sys.exit(f"{a.base} is not known: fetch the tags (git fetch --tags)")
        online = os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY")
        problems = task_problems(found, _github_task if online else None, a.pr)
        print(f"{len(found)} commit(s) since {a.base}" + ("" if online else " (task numbers not looked up: no token)"))
        for x in problems:
            print(f"  - {x}")
        sys.exit(1 if problems else 0)
    elif a.cmd == "home":
        make_home(Path(a.dir).resolve())
    elif a.cmd == "last-fresh":
        print(last_fresh() or "")
    else:
        sys.exit(run(a.set, Path(a.out).resolve(), Path(a.baseline).resolve() if a.baseline else None,
                     a.baseline_run, a.accept_changes))


if __name__ == "__main__":
    main()

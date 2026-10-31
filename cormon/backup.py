"""Hourly database backups, each checked by opening the copy, kept off the server too, and a restore that keeps
the old database aside (BCK-01).

A backup is a full SQLite copy (the online backup API, safe while the service runs) in data/backups/.
It counts as made only after the copy passes an integrity check and holds the same rows as the live
database at that moment. When system.backup_bucket is set, each one is also gzipped to that Cloud Storage
bucket (never overwritten; the bucket's own rules keep and delete them), so at most an hour is lost (RPO 1 hour).
Once a week a restore drill downloads the newest off-server copy, checks it and times it (RTO 4 hours).
A failure emails the admins. Restore needs the service stopped."""

from __future__ import annotations

import base64
import gzip
import hashlib
import secrets
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import db, settings

RPO_HOURS = 1
RTO_HOURS = 4
GCS = "https://storage.googleapis.com"

TABLES = ("messages", "items", "outbox", "audit_log", "rules", "documents")


def folder() -> Path:
    path = settings.data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _counts(c: sqlite3.Connection) -> dict[str, int]:
    return {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def check(path: Path, expect: dict[str, int] | None = None) -> dict:
    """Open a backup read-only: integrity check, and row counts (equal to `expect` when given)."""
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(c)
    finally:
        c.close()
    problems = [] if integrity == "ok" else [f"integrity check: {integrity}"]
    problems += [f"{t}: {counts[t]} rows, live had {n}" for t, n in (expect or {}).items() if counts.get(t) != n]
    return {"path": str(path), "ok": not problems, "problems": problems, "counts": counts}


def make(keep: int | None = None) -> dict:
    keep = keep or int(settings.get("system.backup_keep", 24))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = folder() / f"cormon-{stamp}.db"
    live = db.conn()
    out = sqlite3.connect(path)
    try:
        live.execute("BEGIN")  # hold one snapshot so the counts and the copy agree
        try:
            expect = _counts(live)
            live.backup(out)
        finally:
            live.execute("COMMIT")
        out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file, no -wal/-shm beside it
    finally:
        out.close()
    result = check(path, expect)
    if not result["ok"]:
        path.rename(path.with_suffix(".bad"))
    else:
        for old in sorted(folder().glob("cormon-*.db"))[:-keep]:
            for p in (old, Path(f"{old}-wal"), Path(f"{old}-shm")):
                p.unlink(missing_ok=True)
    db.audit("backup_made" if result["ok"] else "backup_failed", entity_type="service", entity_id="backup",
             path=path.name, problems=result["problems"] or None, counts=result["counts"])
    return result


def _rehearsal() -> bool:
    return {settings.get("channels.email"), settings.get("channels.teams")} <= {"sim", "off"}


def due() -> bool:
    return not _rehearsal() and db.get_state("backup_hour") != datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def hourly(log=print) -> dict | None:
    if not due():
        return None
    from .compliance import alerts
    db.set_state("backup_hour", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H"))
    result = make()
    log(f"backup {'made' if result['ok'] else 'FAILED'}: {Path(result['path']).name}")
    if not result["ok"]:
        alerts.notify("backup", "cormon: a backup FAILED its check",
                      f"The backup {Path(result['path']).name} failed its check: {result['problems']}. "
                      "The last good copy is still kept. See `cormon log`.")
        return result
    if bucket():
        try:
            result["offsite"] = upload(Path(result["path"]))
        except Exception as e:
            db.audit("backup_offsite_failed", entity_type="service", entity_id="backup",
                     path=Path(result["path"]).name, error=str(e)[:500])
            log(f"backup copy to the bucket FAILED: {e}")
            alerts.notify("backup_offsite", "cormon: the off-server backup copy FAILED",
                          f"The hourly backup could not be copied to gs://{bucket()}: {str(e)[:300]}. "
                          "Copies on the server are still made. This is sent again every 6 hours while it lasts.",
                          every_minutes=360)
            return result
        alerts.clear("backup_offsite", "cormon: off-server backups work again",
                     f"The hourly backup reached gs://{bucket()} again.")
    alerts.clear("backup", "cormon: backups pass their check again", "The hourly backup passed its check.")
    return result


MEMORY_HOURS = 2   # one hour between copies, plus the hour being made


def memory_hourly(log=print) -> dict | None:
    """Patient memory's backups, made every hour by root (deploy/hindsight_backup.sh: the bot's user has no Docker).
    The newest must be under MEMORY_HOURS old and have passed its restore check; it is copied off the server once.
    Otherwise the admins are told."""
    from . import hindsight
    hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    if _rehearsal() or not hindsight.used() or db.get_state("memory_backup_hour") == hour:
        return None
    from .compliance import alerts
    import json
    db.set_state("memory_backup_hour", hour)
    dumps = sorted(folder().glob("hindsight-*.dump"))
    newest = dumps[-1] if dumps else None
    info = json.loads(Path(f"{newest}.json").read_text()) if newest and Path(f"{newest}.json").exists() else {}
    made = datetime.fromisoformat(info["at"].replace("Z", "+00:00")) if info.get("at") else None
    problem = ("no patient memory backup has been made" if not newest
               else "its check result is missing" if not info
               else f"the newest is {(datetime.now(timezone.utc) - made).total_seconds() / 3600:.1f} hours old"
               if (datetime.now(timezone.utc) - made).total_seconds() > MEMORY_HOURS * 3600
               else f"the newest failed its restore check ({info.get('facts')} of {info.get('live_facts')} facts)"
               if not info.get("ok") else None)
    if problem:
        db.audit("backup_memory_failed", entity_type="service", entity_id="backup", file=newest and newest.name,
                 problem=problem)
        log(f"patient memory backup: {problem}")
        alerts.notify("backup_memory", "cormon: the patient memory backup FAILED",
                      f"Patient memory (Hindsight) is not backed up: {problem}. Check the timer with "
                      "`systemctl status cormon-hindsight-backup` and run deploy/hindsight_backup.sh by hand. "
                      "This is sent again every 6 hours while it lasts.", every_minutes=360)
        return {"ok": False, "problem": problem}
    out = {"ok": True, "file": newest.name, "facts": info.get("facts")}
    if bucket() and db.get_state("memory_backup_sent") != newest.name:
        try:
            out["offsite"] = upload(newest)
            db.set_state("memory_backup_sent", newest.name)
        except Exception as e:
            db.audit("backup_offsite_failed", entity_type="service", entity_id="backup", path=newest.name,
                     error=str(e)[:500])
            alerts.notify("backup_memory", "cormon: the patient memory backup could not leave the server",
                          f"{newest.name} could not be copied to gs://{bucket()}: {str(e)[:300]}.",
                          every_minutes=360)
            return dict(out, ok=False, problem=str(e)[:300])
    alerts.clear("backup_memory", "cormon: patient memory is backed up again",
                 f"{newest.name} passed its restore check ({info.get('facts')} facts).")
    return out


# ---------------------------------------------------------------- off the server (Cloud Storage)

def bucket() -> str | None:
    return settings.get("system.backup_bucket") or None


def _prefix() -> str:
    return settings.get("system.backup_prefix", "cormon/")


def _session():
    """The machine's own Cloud account (the VM's service account), allowed to add and read the bucket's objects."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    return AuthorizedSession(creds)


def upload(path: Path, session=None) -> dict:
    """Gzip one checked backup to the bucket under a new name; the bucket's MD5 must match ours."""
    gz = path.with_name(path.name + ".gz")
    with open(path, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    try:
        md5 = base64.b64encode(hashlib.md5(gz.read_bytes()).digest()).decode()
        name = f"{_prefix()}{gz.name}"
        with open(gz, "rb") as fh:
            r = (session or _session()).post(
                f"{GCS}/upload/storage/v1/b/{bucket()}/o", data=fh, timeout=180,
                params={"uploadType": "media", "name": name, "ifGenerationMatch": "0"},
                headers={"Content-Type": "application/gzip"})
        r.raise_for_status()
        obj = r.json()
        if obj.get("md5Hash") != md5:
            raise RuntimeError(f"the bucket's copy of {name} does not match (md5 {obj.get('md5Hash')}, ours {md5})")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        db.audit("backup_offsite", entity_type="service", entity_id="backup", object=f"gs://{bucket()}/{name}",
                 bytes=int(obj.get("size", 0)), md5=md5, db_sha256=sha)
        return {"object": f"gs://{bucket()}/{name}", "bytes": int(obj.get("size", 0))}
    finally:
        gz.unlink(missing_ok=True)


def newest(session=None) -> dict | None:
    """The newest backup in the bucket: name, size and time."""
    s, best, token = session or _session(), None, None
    while True:
        params = {"prefix": _prefix(), "fields": "items(name,size,timeCreated),nextPageToken"}
        if token:
            params["pageToken"] = token
        r = s.get(f"{GCS}/storage/v1/b/{bucket()}/o", params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        for o in data.get("items", []):
            if o["name"].endswith(".db.gz") and (best is None or o["name"] > best["name"]):
                best = o
        token = data.get("nextPageToken")
        if not token:
            return best


def fetch(name: str | None = None, dest: Path | None = None, session=None) -> Path:
    """Download one backup from the bucket (the newest when no name) and unzip it, ready for `cormon restore`."""
    s = session or _session()
    if not name:
        o = newest(s)
        if o is None:
            raise RuntimeError(f"there are no backups in gs://{bucket()}/{_prefix()}")
        name = o["name"]
    dest = dest or folder() / Path(name).name.removesuffix(".gz")
    gz = dest.with_name(dest.name + ".gz")
    with s.get(f"{GCS}/storage/v1/b/{bucket()}/o/{quote(name, safe='')}", params={"alt": "media"},
               stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(gz, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    try:
        with gzip.open(gz, "rb") as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    finally:
        gz.unlink(missing_ok=True)
    return dest


def drill(session=None) -> dict:
    """The restore drill: fetch the newest off-server copy into a scratch file, check it, time it. The live
    database is not touched. It fails when the copy is older than the RPO or the whole took longer than the RTO."""
    began = time.monotonic()
    s = session or _session()
    o = newest(s)
    problems, counts, name = [], {}, o["name"] if o else None
    if o is None:
        problems.append(f"no backups in gs://{bucket()}/{_prefix()}")
    else:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(o["timeCreated"].replace("Z", "+00:00")))
        if age.total_seconds() > (RPO_HOURS + 1) * 3600:  # an hour between copies, plus the hour being made
            problems.append(f"the newest copy is {age.total_seconds() / 3600:.1f} hours old")
        scratch = folder() / "drill"
        scratch.mkdir(exist_ok=True)
        try:
            res = check(fetch(name, scratch / "drill.db", s))
            problems += res["problems"]
            counts = res["counts"]
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    seconds = round(time.monotonic() - began, 1)
    if seconds > RTO_HOURS * 3600:
        problems.append(f"the restore took {seconds / 3600:.1f} hours, more than {RTO_HOURS}")
    out = {"ok": not problems, "object": name, "seconds": seconds, "counts": counts, "problems": problems}
    db.audit("restore_drill" if out["ok"] else "restore_drill_failed", entity_type="service", entity_id="backup",
             **out)
    return out


def weekly_drill(log=print) -> dict | None:
    week = datetime.now(timezone.utc).strftime("%G-W%V")
    if _rehearsal() or not bucket() or db.get_state("restore_drill_week") == week:
        return None
    from .compliance import alerts
    db.set_state("restore_drill_week", week)
    try:
        res = drill()
    except Exception as e:
        res = {"ok": False, "object": None, "seconds": None, "counts": {}, "problems": [str(e)[:300]]}
        db.audit("restore_drill_failed", entity_type="service", entity_id="backup", **res)
    log(f"restore drill {'passed' if res['ok'] else 'FAILED'}: {res['object']} in {res['seconds']}s")
    if not res["ok"]:
        alerts.notify("restore_drill", "cormon: the weekly restore drill FAILED",
                      f"The newest off-server backup could not be restored and checked: {res['problems']}.")
    else:
        alerts.clear("restore_drill", "cormon: the restore drill passes again",
                     f"{res['object']} restored and checked in {res['seconds']} seconds.")
    return res


def restore(path: Path) -> dict:
    """Replace the live database with a checked backup. The current one is kept beside it (.before-restore).
    Outbox numbers are reused after a restore, so the chat message ids get a new install id."""
    result = check(path)
    if not result["ok"]:
        raise RuntimeError(f"backup {path} failed its check: {result['problems']}")
    live = db.db_path()
    if db._local.__dict__.get("conn") is not None:
        db._local.conn.close()
        db._local.conn = None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    kept = live.with_name(f"{live.name}.before-restore-{stamp}")
    if live.exists():  # through SQLite, so changes still in the -wal file are kept too
        src, dst = sqlite3.connect(live), sqlite3.connect(kept)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    for side in ("-wal", "-shm"):
        Path(str(live) + side).unlink(missing_ok=True)
    shutil.copy2(path, live)
    db.set_state("install_id", secrets.token_hex(4))
    db.audit("backup_restored", entity_type="service", entity_id="backup", path=str(path), kept=kept.name)
    return {"restored": str(path), "kept": str(kept), "counts": result["counts"]}

"""The records package for one study (EXP-01, SAF-07): what a sponsor or an inspector asks for, in one zip.

    README.txt           what is in it, who made it and when, and the result of the checks
    manifest.csv         every original: its hash at capture, its hash now, and ok / changed / missing / purged
    originals/           the originals themselves (messages as they arrived, attachments, sent messages, bounces)
    messages.csv         the messages read
    alerts.csv           every alert with its current state and reason code
    dispositions.csv     every change to an alert: who, when, what (from the audit trail)
    safety.csv           every safety message and where it was sent (SAF-07)
    sent.csv             every message the bot sent or queued
    audit_trail.csv      the study's audit entries with their chain hashes; anchors.txt holds the daily anchors
    ai_log.csv           every AI call made for the study, in full
    patient_memory.csv   every fact patient memory holds (current and retired), who reported it and from which
                         message; its saves, searches and admin changes are in the audit trail
    config_history.csv   every settings version, with config/ holding each one's text
    checks.json          the audit-chain check and the originals check, run when the package was made

since / until (dates, inclusive) limit messages, alerts, sent messages, audit entries and AI calls; the settings
history is always whole. Making a package is itself recorded in the audit trail, with the zip's SHA-256.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from . import ailog, records, sha256, trail

STATE_LABEL = {"open": "open", "cleared": "cleared", "suppressed": "disregarded", "handled": "handled",
               "waiting": "waiting for review",
               "out_of_scope": "safety (not assessed)"}


def _csv(rows: list[dict], cols: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: "" if r.get(k) is None else r.get(k) for k in cols})
    return buf.getvalue()


def _window(col: str, since: str | None, until: str | None) -> tuple[str, list]:
    sql, args = "", []
    if since:
        sql, args = sql + f" AND {col} >= ?", args + [since]
    if until:
        sql, args = sql + f" AND substr({col}, 1, 10) <= ?", args + [until]
    return sql, args


def _rows(sql: str, args) -> list[dict]:
    return [dict(r) for r in db.q(sql, tuple(args))]


def _ext(r) -> str:
    if r["location"]:
        return Path(r["location"]).suffix or ".bin"
    return ".json"


def build(study_id: str, out: Path, since: str | None = None, until: str | None = None,
          actor: str | None = None) -> dict:
    """Write <out>/<study>-records-<time>.zip and return its path, SHA-256 and what it holds."""
    made = datetime.now(timezone.utc).isoformat(timespec="seconds")
    w, wa = _window("received_at", since, until)
    messages = _rows(f"SELECT * FROM messages WHERE study_id=?{w} ORDER BY id", [study_id, *wa])
    w, wa = _window("created_at", since, until)
    alerts = _rows(f"SELECT * FROM items WHERE study_id=?{w} ORDER BY id", [study_id, *wa])
    w, wa = _window("o.created_at", since, until)
    sent = _rows("SELECT o.* FROM outbox o LEFT JOIN items i ON i.id=o.item_id LEFT JOIN messages m ON m.id=o.message_id"
                 f" WHERE COALESCE(o.study_id, i.study_id, m.study_id)=?{w} ORDER BY o.id", [study_id, *wa])
    msg_ids = {str(m["id"]) for m in messages}
    item_ids = {str(i["id"]) for i in alerts}
    out_ids = {str(o["id"]) for o in sent}
    for a in alerts:
        a["state_label"] = STATE_LABEL.get(a["state"], a["state"])

    w, wa = _window("ts", since, until)
    audit = [r for r in _rows(f"SELECT * FROM audit_log WHERE 1=1{w} ORDER BY id", wa)
             if r["study_id"] == study_id
             or (r["entity_type"] == "item" and r["entity_id"] in item_ids)
             or (r["entity_type"] == "message" and r["entity_id"] in msg_ids)
             or (r["entity_type"] == "outbox" and r["entity_id"] in out_ids)]
    dispositions = [r for r in audit if r["entity_type"] == "item" and r["entity_id"] in item_ids]

    safety = []
    for a in alerts:
        if a["state"] != "out_of_scope":
            continue
        routes = [o for o in sent if o["item_id"] == a["id"]]
        received = db.q1("SELECT received_at, sha256 FROM messages WHERE id=?", (a["first_message_id"],))
        for o in routes or [{}]:
            safety.append({"alert": a["id"], "message": a["first_message_id"], "participant": a["participant_id"],
                           "site": a["site"], "received_at": received["received_at"] if received else None,
                           "message_sha256": received["sha256"] if received else None, "found_at": a["created_at"],
                           "why": a["reasoning"], "severity": a.get("severity"), "ack_due_at": a.get("ack_due_at"),
                           "acknowledged_at": a.get("acked_at"), "acknowledged_by": a.get("acked_by"),
                           "escalation_step": a.get("escalation_step"), "deviation_at": a.get("ack_deviation_at"),
                           "email": o.get("purpose"), "sent_to": o.get("to_addrs"), "cc": o.get("cc_addrs"),
                           "send_status": o.get("status") or "not sent", "sent_at": o.get("sent_at"),
                           "outbox": o.get("id")})

    w, wa = _window("ts", since, until)
    calls = _rows(f"SELECT * FROM ai_log WHERE study_id=?{w} ORDER BY id", [study_id, *wa])
    for c in calls:
        c["request"] = json.dumps(ailog.request_of(c), ensure_ascii=False)
    configs = _rows("SELECT * FROM config_versions ORDER BY id", [])
    from .. import hindsight
    memory_facts, memory_problem = [], None
    if hindsight.used():
        try:
            memory_facts = hindsight.export_rows(study_id, since, until)
        except Exception as e:
            memory_problem = f"patient memory could not be read: {str(e)[:200]}"

    owners = {("message", int(i)) for i in msg_ids} | {("outbox", int(i)) for i in out_ids}
    manifest = []
    out.mkdir(parents=True, exist_ok=True)
    name = f"{study_id}-records-{made[:19].replace(':', '').replace('-', '')}.zip"
    path = out / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for r in db.q("SELECT * FROM records ORDER BY id"):
            if (r["owner_type"], r["owner_id"]) not in owners:
                continue
            inner = f"originals/{r['owner_type']}-{r['owner_id']}/{r['id']}-{r['kind']}{_ext(r)}"
            data = None if r["purged_at"] else records._current(r)
            now = sha256(data) if data is not None else None
            status = ("purged" if r["purged_at"] else "missing" if data is None
                      else "ok" if now == r["sha256"] else "changed")
            if data is not None:
                z.writestr(inner, data)
            manifest.append({"file": inner if data is not None else "", "record": r["id"], "kind": r["kind"],
                             "owner": f"{r['owner_type']} {r['owner_id']}", "size": r["size"],
                             "sha256_at_capture": r["sha256"], "sha256_now": now, "status": status,
                             "kept_at": r["created_at"], "purged_at": r["purged_at"]})
        for c in configs:
            z.writestr(f"config/{c['id']}-{Path(c['path']).name}", c["content"])
        chain = trail.verify()
        checks = {"audit_chain": chain,
                  "originals": {"checked": len(manifest),
                                "problems": [m for m in manifest if m["status"] in ("changed", "missing")]},
                  "patient_memory": {"facts": len(memory_facts), "problem": memory_problem}}
        z.writestr("manifest.csv", _csv(manifest, ["file", "record", "kind", "owner", "size", "sha256_at_capture",
                                                   "sha256_now", "status", "kept_at", "purged_at"]))
        z.writestr("messages.csv", _csv(messages, ["id", "channel", "from_addr", "from_name", "to_addrs", "cc_addrs",
                                                   "subject", "sent_at", "received_at", "status", "sha256",
                                                   "body_text"]))
        z.writestr("alerts.csv", _csv(alerts, ["id", "created_at", "updated_at", "kind", "rule_id", "participant_id",
                                               "site", "title", "tier", "state_label", "reason_code", "routed",
                                               "recipient_person", "first_message_id", "last_message_id",
                                               "evidence", "reasoning"]))
        z.writestr("dispositions.csv", _csv(dispositions, ["ts", "entity_id", "action", "actor", "actor_type",
                                                           "details"]))
        z.writestr("safety.csv", _csv(safety, ["alert", "message", "participant", "site", "received_at",
                                               "message_sha256", "found_at", "why", "severity", "ack_due_at",
                                               "acknowledged_at", "acknowledged_by", "escalation_step",
                                               "deviation_at", "email", "sent_to", "cc", "send_status", "sent_at",
                                               "outbox"]))
        z.writestr("sent.csv", _csv(sent, ["id", "created_at", "sent_at", "status", "purpose", "channel",
                                           "destination", "template_id", "item_id", "message_id", "from_addr",
                                           "to_addrs", "cc_addrs", "subject", "body", "sha256", "last_error"]))
        z.writestr("audit_trail.csv", _csv(audit, ["id", "ts", "actor", "actor_type", "action", "entity_type",
                                                   "entity_id", "study_id", "details", "prev_hash", "hash"]))
        z.writestr("ai_log.csv", _csv(calls, ["id", "ts", "message_id", "message_sha256", "study_model",
                                              "config_version", "task", "api", "model", "params", "cached", "ok",
                                              "error", "request_sha256", "request", "response"]))
        z.writestr("config_history.csv", _csv(configs, ["id", "path", "sha256", "actor", "recorded_at",
                                                        "approved_by", "approved_at", "waiting"]))
        z.writestr("patient_memory.csv", _csv(memory_facts, ["fact", "reported_at", "reported_by", "participants",
                                                             "fact_text", "happened", "state", "retired_why",
                                                             "edited_at", "document", "message_id", "alert_id"]))
        if trail.anchor_file().exists():
            z.writestr("anchors.txt", trail.anchor_file().read_text())
        z.writestr("checks.json", json.dumps(checks, indent=2, default=str))
        counts = {"messages": len(messages), "alerts": len(alerts), "dispositions": len(dispositions),
                  "safety": len(safety), "sent": len(sent), "originals": len(manifest), "audit_entries": len(audit),
                  "ai_calls": len(calls), "config_versions": len(configs), "memory_facts": len(memory_facts)}
        z.writestr("README.txt", _readme(study_id, made, actor, since, until, counts, checks))
    digest = sha256(path.read_bytes())
    db.audit("records_exported", actor=actor or "system", actor_type="operator" if actor else "system",
             entity_type="study", entity_id=study_id, study_id=study_id, file=name, sha256=digest, since=since,
             until=until, **counts)
    return {"path": path, "sha256": digest, "counts": counts, "checks": checks}


def _readme(study_id, made, actor, since, until, counts, checks) -> str:
    chain = checks["audit_chain"]
    bad = checks["originals"]["problems"]
    lines = [f"Records package for study {study_id}",
             f"Made {made} by {actor or 'the system'}.",
             f"Period: {since or 'the start'} to {until or 'now'} (the settings history is always whole).", "",
             "Holds: " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in counts.items()) + ".", "",
             "Audit chain: " + ("intact" if chain["ok"] else f"BROKEN at entry {chain.get('broken_at')}: "
                                                              f"{chain.get('why')}") + ".",
             "Originals: " + ("all match the hash they had when kept" if not bad
                              else f"{len(bad)} do NOT match or are missing (see manifest.csv)") + ".",
             *([f"Patient memory: NOT included, {checks['patient_memory']['problem']}."]
               if checks["patient_memory"]["problem"] else []), "",
             "Each original's SHA-256 is in manifest.csv; re-hash the file in originals/ to check it.",
             "Each audit entry's hash covers the entry before it, so an entry changed or removed breaks the chain.",
             "Safety messages are recorded and sent to the medical monitor unassessed; the bot gives no clinical",
             "judgment and never tells the site that something is fine."]
    return "\n".join(lines) + "\n"

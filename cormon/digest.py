"""Daily digest for the sponsor: grouped by study, each item marked by status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, drafting, routing, senders, settings
from .compliance import SAFETY_NOTICE
from .triage import TIER_LABELS

GROUPS = [
    ("attention", "Needs your attention (medium or immediate, not yet resolved)"),
    ("awaiting", "Awaiting clearance by the CRA (low tier, not cleared)"),
    ("review", "Waiting for a second person's review (closed on the alert's page)"),
    ("handled", "Handled by the CRA"),
    ("updates", "Updates (good news)"),
]


def _status(item: dict) -> str:
    if item["kind"] == "good_news":
        return "updates"
    if item["state"] in ("cleared", "suppressed", "handled"):
        return "handled"
    if item["state"] == "waiting":
        return "review"
    if item["tier"] in ("medium", "immediate"):
        return "attention"
    return "awaiting"


def build(since: datetime, until: datetime, study_ids: list[str] | None = None) -> dict:
    all_studies = config.studies()
    rows = db.q(
        "SELECT * FROM items WHERE kind != 'out_of_scope' AND (counts=1 OR state IN ('suppressed','cleared')) AND ("
        " (created_at >= ? AND created_at < ?) OR (state IN ('open','waiting') AND created_at < ?)"
        " OR (updated_at >= ? AND updated_at < ? AND state IN ('cleared','suppressed'))"
        ") ORDER BY study_id, id",
        (since.isoformat(), until.isoformat(), since.isoformat(), since.isoformat(), until.isoformat()))
    per_study: dict[str, dict] = {}
    for r in rows:
        if study_ids and r["study_id"] not in study_ids:
            continue
        s = all_studies.get(r["study_id"])
        if s is None or senders.item_off_roster(dict(r), s):
            continue
        entry = per_study.setdefault(r["study_id"], {
            "study_id": s.study_id, "short_name": s.short_name,
            "buckets": {k: [] for k, _ in GROUPS}})
        item = dict(r)
        carried = item["created_at"] < since.isoformat()
        flags = senders.item_flags(item, s)
        entry["buckets"][_status(item)].append({
            "id": item["id"], "tier_label": TIER_LABELS.get(item["tier"], item["tier"]),
            "site": s.site_name(item["site"]), "participant_id": item["participant_id"],
            "title": item["title"] + {"suppressed": " (disregarded)", "cleared": " (cleared)"}.get(item["state"], "")
                     + (" (carried over)" if carried else "")
                     + "".join(f" (check the sender: {f})" for f in flags),
            "flags": flags,
        })
    studies_out = []
    total = 0
    for sid, e in per_study.items():
        groups = [{"label": label, "entries": e["buckets"][k]} for k, label in GROUPS
                  if k != "review" or e["buckets"][k]]   # only alerts closed on their page wait for a review
        total += sum(len(g["entries"]) for g in groups)
        studies_out.append({"study_id": sid, "short_name": e["short_name"], "groups": groups})
    tz = ZoneInfo(settings.get("digest.timezone", "America/New_York"))
    notes = gaps_and_late(since, until, study_ids)
    footer = f"Sent by {settings.bot_name()}. It does not review safety information. {SAFETY_NOTICE}"
    if notes:
        footer = "Gaps and late messages in this window:\n" + "\n".join(f"- {n}" for n in notes) + "\n\n" + footer
    return {
        "date": until.astimezone(tz).strftime("%d %b %Y"),
        "window": f"{since.astimezone(tz):%d %b %H:%M} to {until.astimezone(tz):%d %b %H:%M} ({tz.key})",
        "studies": studies_out, "total": total, "notes": notes,
        "footer": footer,
    }


def _when(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None
    except ValueError:
        return None
    return d.replace(tzinfo=timezone.utc) if d and d.tzinfo is None else d


def gaps_and_late(since: datetime, until: datetime, study_ids: list[str] | None = None) -> list[str]:
    """When the bot was not running, and messages that reached it late or waited long to be read, so a reader
    knows the window was not fully covered (the sender's own time is not trusted, so "late" is a hint)."""
    out = []
    for r in db.q("SELECT details FROM audit_log WHERE action='bot_downtime' AND ts>=?", (since.isoformat(),)):
        d = db.unjs(r["details"], {})
        start, end = _when(d.get("start")), _when(d.get("end"))
        if start and end and end >= since and start < until:
            out.append(f"The bot was not running from {drafting.local_time(d['start'])} to "
                       f"{drafting.local_time(d['end'])}: messages from then were read when it restarted.")
    down = db.get_state("down_since")
    if down:
        out.append(f"The bot has not been running since {drafting.local_time(down)}.")
    late = float(settings.get("digest.late_minutes", 60))
    wait = float(settings.get("system.stall_minutes", 15))
    rows = db.q("SELECT m.id, m.study_id, m.channel, m.from_addr, m.sent_at, m.received_at,"
                " (SELECT MIN(started_at) FROM message_timings t WHERE t.message_id=m.id) AS read_at"
                " FROM messages m WHERE m.received_at>=? AND m.received_at<? ORDER BY m.id",
                (since.isoformat(), until.isoformat()))
    for m in rows:
        if study_ids and m["study_id"] not in study_ids:
            continue
        sent, got, read = _when(m["sent_at"]), _when(m["received_at"]), _when(m["read_at"])
        who = f"Message {m['id']} from {m['from_addr'] or 'unknown sender'}"
        if sent and got and (got - sent).total_seconds() > late * 60:
            out.append(f"{who} reached the bot late: the sender's time says {drafting.local_time(m['sent_at'])}, "
                       f"it arrived {drafting.local_time(m['received_at'])}.")
        if got and read and (read - got).total_seconds() > wait * 60:
            out.append(f"{who} waited {int((read - got).total_seconds() // 60)} minutes before the bot read it.")
    return out


def send(hours: float | None = None, study_ids: list[str] | None = None, *, dry_run: bool = False,
         actor: str = "system") -> list[dict]:
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=hours or settings.get("digest.window_hours", 24))
    roles = config.routing().get("digest", {}).get("to_roles", ["clinops_lead"])
    recipients: dict[str, dict] = {}
    for s in config.studies().values():
        if study_ids and s.study_id not in study_ids:
            continue
        for role in roles:
            for p in s.people_with_role(role):
                if p.email:
                    r = recipients.setdefault(p.email.lower(), {"person": p, "studies": [], "alias": s.alias})
                    r["studies"].append(s.study_id)
    out = []
    tpl = drafting.select("digest", "*", "email", "*")
    for email, r in recipients.items():
        content = build(since, until, r["studies"])
        ctx = {"digest": content, "recipient": drafting._person_ctx(r["person"], "Hello")}
        subject, body = drafting.render(tpl, ctx)
        rec = {"to": email, "subject": subject, "body": body, "total": content["total"]}
        if not dry_run:
            rec["outbox_id"] = routing.enqueue(
                purpose="digest", channel="email", destination="email", body=body, subject=subject,
                template_id=tpl["id"], from_addr=r["alias"], to=[email],
                meta={"window": [since.isoformat(), until.isoformat()], "studies": r["studies"]})
        out.append(rec)
    db.audit("digest_built", entity_type="digest", entity_id=until.isoformat(), actor=actor,
             actor_type="operator" if actor != "system" else "system",
             window=[since.isoformat(), until.isoformat()], recipients=list(recipients), dry_run=dry_run)
    return out


def due() -> bool:
    tz = ZoneInfo(settings.get("digest.timezone", "America/New_York"))
    now = datetime.now(tz)
    hh, mm = (int(x) for x in str(settings.get("digest.send_at", "07:30")).split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False
    return db.get_state("digest_last_date") != now.strftime("%Y-%m-%d")


def mark_sent() -> None:
    tz = ZoneInfo(settings.get("digest.timezone", "America/New_York"))
    db.set_state("digest_last_date", datetime.now(tz).strftime("%Y-%m-%d"))

"""Daily digest for the sponsor: grouped by study, each item marked by status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, db, drafting, routing, settings
from .compliance import SAFETY_NOTICE
from .triage import TIER_LABELS

GROUPS = [
    ("attention", "Needs your attention (medium or immediate, not yet resolved)"),
    ("awaiting", "Awaiting clearance by the CRA (low tier, not cleared)"),
    ("handled", "Handled by the CRA"),
    ("updates", "Updates (good news)"),
]


def _status(item: dict) -> str:
    if item["kind"] == "good_news":
        return "updates"
    if item["state"] in ("cleared", "suppressed", "handled"):
        return "handled"
    if item["tier"] in ("medium", "immediate"):
        return "attention"
    return "awaiting"


def build(since: datetime, until: datetime, study_ids: list[str] | None = None) -> dict:
    all_studies = config.studies()
    rows = db.q(
        "SELECT * FROM items WHERE kind != 'out_of_scope' AND (counts=1 OR state IN ('suppressed','cleared')) AND ("
        " (created_at >= ? AND created_at < ?) OR (state='open' AND created_at < ?)"
        " OR (updated_at >= ? AND updated_at < ? AND state IN ('cleared','suppressed'))"
        ") ORDER BY study_id, id",
        (since.isoformat(), until.isoformat(), since.isoformat(), since.isoformat(), until.isoformat()))
    per_study: dict[str, dict] = {}
    for r in rows:
        if study_ids and r["study_id"] not in study_ids:
            continue
        s = all_studies.get(r["study_id"])
        if s is None:
            continue
        entry = per_study.setdefault(r["study_id"], {
            "study_id": s.study_id, "short_name": s.short_name,
            "buckets": {k: [] for k, _ in GROUPS}})
        item = dict(r)
        carried = item["created_at"] < since.isoformat()
        entry["buckets"][_status(item)].append({
            "id": item["id"], "tier_label": TIER_LABELS.get(item["tier"], item["tier"]),
            "site": s.site_name(item["site"]), "participant_id": item["participant_id"],
            "title": item["title"] + {"suppressed": " (disregarded)", "cleared": " (cleared)"}.get(item["state"], "")
                     + (" (carried over)" if carried else ""),
        })
    studies_out = []
    total = 0
    for sid, e in per_study.items():
        groups = [{"label": label, "entries": e["buckets"][k]} for k, label in GROUPS]
        total += sum(len(g["entries"]) for g in groups)
        studies_out.append({"study_id": sid, "short_name": e["short_name"], "groups": groups})
    tz = ZoneInfo(settings.get("digest.timezone", "America/New_York"))
    return {
        "date": until.astimezone(tz).strftime("%d %b %Y"),
        "window": f"{since.astimezone(tz):%d %b %H:%M} to {until.astimezone(tz):%d %b %H:%M} ({tz.key})",
        "studies": studies_out, "total": total,
        "footer": f"Sent by the correspondence monitor. It does not review safety information. {SAFETY_NOTICE}",
    }


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

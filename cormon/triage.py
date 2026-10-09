"""Tier assignment from the editable rules in config/triage.yaml (and study overrides)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser

from . import config, settings

TIERS = ("low", "medium", "immediate")
TIER_LABELS = {"low": "Low", "medium": "Medium", "immediate": "Immediate"}


def hours_until(event_dt: str | None, message_dt: str | None) -> float | None:
    if not event_dt or not message_dt:
        return None
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    try:
        ref = dtparser.isoparse(message_dt)
        ev = dtparser.isoparse(event_dt)
    except (ValueError, TypeError):
        return None
    if len(event_dt) <= 10:  # date only: assume midday local
        ev = datetime(ev.year, ev.month, ev.day, 12, tzinfo=tz)
    if ev.tzinfo is None:
        ev = ev.replace(tzinfo=tz)
    return (ev - ref) / timedelta(hours=1)


def _listify(v):
    return v if isinstance(v, list) else [v]


def matches(cond: dict, facts: dict) -> bool:
    for key, want in cond.items():
        if key == "hours_until_max":
            h = facts.get("hours_until")
            if h is None or h > want:
                return False
        elif key in ("pattern_class", "temporality", "polarity", "check_type"):
            if facts.get(key) not in _listify(want):
                return False
        else:
            return False  # unknown condition: never match silently
    return True


def assign(study_id: str, facts: dict) -> tuple[str, str]:
    """Return (tier, reason) using the first matching rule."""
    for rule in config.triage_rules(study_id):
        if matches(rule.get("when", {}), facts):
            tier = rule["tier"]
            if tier not in TIERS:
                raise ValueError(f"triage rule {rule.get('id')} has unknown tier {tier}")
            return tier, f"{rule.get('id')}: {rule.get('description', '')}".strip(": ")
    return "low", "no triage rule matched (default low)"

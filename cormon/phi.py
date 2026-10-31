"""Patient details: what counts as one, masking it in logs and in what the bot sends, and how long message text is kept.

Participant study ids (e.g. G-501-004) are coded, not personal, so they stay: the monitor needs them. Masked:
dates of birth, phone numbers, social security numbers and medical record numbers, which a site may type by
mistake. Names cannot be told apart from staff names by pattern, so they are not masked; message text is cleared
after data.retention_days instead (7 years in config/settings.yaml; off when unset)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db, settings

_DATE = r"(?:\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{2,4})"
PATTERNS = [
    ("date of birth", re.compile(r"\b(?:DOB|D\.O\.B\.?|date of birth|birth ?date|born(?: on)?)\b[\s:#-]*" + _DATE, re.I)),
    ("social security number", re.compile(r"\b(?:SSN[\s:#-]*)?\d{3}-\d{2}-\d{4}\b")),
    ("medical record number", re.compile(r"\b(?:MRN|medical record (?:number|no\.?|#))[\s:#-]*[A-Z0-9-]{4,}\b", re.I)),
    ("phone number", re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?![\w-])")),
]


def mask(text: str | None, keep: frozenset = frozenset()) -> tuple[str | None, list[str]]:
    """The text with patient details replaced by [kind removed], and the kinds found. `keep`: matches that are
    the study documents' own words (a safety hotline number), which stay."""
    if not text:
        return text, []
    found = []
    for kind, pat in PATTERNS:
        def swap(m, kind=kind):
            if m.group(0) in keep:
                return m.group(0)
            found.append(kind)
            return f"[{kind} removed]"
        text = pat.sub(swap, text)
    return text, list(dict.fromkeys(found))


_KEEP: dict[str, frozenset] = {}


def documents_own(study_id: str | None) -> frozenset:
    """Pattern matches in the study's documents (contact numbers, etc.): not patient details."""
    if not study_id:
        return frozenset()
    if study_id not in _KEEP:
        from . import guide
        try:
            text = "\n".join(i.get("text") or "" for i in guide.load(study_id) if i.get("kind") == "section")
        except Exception:
            text = ""
        _KEEP[study_id] = frozenset(m.group(0) for _, pat in PATTERNS for m in pat.finditer(text))
    return _KEEP[study_id]


def mask_all(value):
    """mask() through nested lists and dicts (audit details)."""
    if isinstance(value, str):
        return mask(value)[0]
    if isinstance(value, dict):
        return {k: mask_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_all(v) for v in value]
    return value


def purge(days: int | None = None, log=print) -> dict:
    """Clear message text, raw copies and edited versions, answer-page questions, model-call caches and the full AI
    log, and participant facts older than `days` (data.retention_days when not given). Alerts keep their short quote
    of the message as the monitoring record; the audit log is append-only and keeps its (masked) entries; the
    fingerprints of purged originals stay, marked purged."""
    days = days if days is not None else settings.get("data.retention_days")
    if not days:
        return {"skipped": "data.retention_days is not set"}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat(timespec="seconds")
    raws = [r["raw_path"] for r in db.q("SELECT raw_path FROM messages WHERE received_at<? AND raw_path IS NOT NULL",
                                        (cutoff,))]
    files = [r["path"] for r in db.q("SELECT a.path FROM attachments a JOIN messages m ON m.id=a.message_id"
                                     " WHERE m.received_at<? AND a.path IS NOT NULL", (cutoff,))]
    versions = [r["raw_path"] for r in db.q("SELECT v.raw_path FROM message_versions v JOIN messages m ON"
                                            " m.id=v.message_id WHERE m.received_at<? AND v.raw_path IS NOT NULL",
                                            (cutoff,))]
    for p in raws + files + versions:
        path = Path(p) if Path(p).is_absolute() else settings.data_dir() / p
        path.unlink(missing_ok=True)
    from .compliance import records
    records.mark_purged(raws + files + versions)
    out = {
        "attachments": db.changed("UPDATE attachments SET text=NULL, path=NULL WHERE (path IS NOT NULL OR text IS NOT NULL)"
                                  " AND message_id IN (SELECT id FROM messages WHERE received_at<?)", (cutoff,)),
        "messages": db.changed("UPDATE messages SET body_text='[cleared after retention period]', body_html=NULL,"
                               " raw_path=NULL WHERE received_at<? AND body_text!='[cleared after retention period]'",
                               (cutoff,)),
        "raw_files": len(raws),
        "answer_pages": db.changed("UPDATE answer_pages SET message_text='[cleared after retention period]'"
                                   " WHERE created_at<? AND message_text!='[cleared after retention period]'",
                                   (cutoff,)),
        "llm_cache": db.changed("DELETE FROM llm_cache WHERE created_at<?", (cutoff,)),
        "ai_log": db.changed("DELETE FROM ai_log WHERE ts<?", (cutoff,)),
        "message_versions": db.changed("UPDATE message_versions SET body_text=NULL, body_html=NULL, raw_path=NULL"
                                       " WHERE (body_text IS NOT NULL OR raw_path IS NOT NULL) AND message_id IN"
                                       " (SELECT id FROM messages WHERE received_at<?)", (cutoff,)),
        "patient_facts": db.changed("DELETE FROM patient_facts WHERE COALESCE(sent_at, created_at)<?", (cutoff,)),
    }
    db.audit("retention_purge", entity_type="service", entity_id="retention", days=int(days), **out)
    log(f"retention: cleared data older than {days} days: {out}")
    return out


class NoRetentionPeriod(RuntimeError):
    pass


def check_retention() -> int:
    """The bot does not run without a retention period (data.retention_days, a whole number of days)."""
    days = settings.get("data.retention_days")
    if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
        raise NoRetentionPeriod(
            f"refused: no retention period is set (data.retention_days is {days!r}). Set how many days message text "
            "is kept, e.g. `cormon config set settings data.retention_days 2557` (7 years), then start the bot again")
    return days


def daily(log=print) -> dict | None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not settings.get("data.retention_days") or db.get_state("purge_day") == today:
        return None
    db.set_state("purge_day", today)
    return purge(log=log)


def forget_participant(study_id: str, participant: str) -> int:
    """Delete what the bot remembers about one participant (their dates and events); returns rows removed."""
    from . import memory
    n = db.changed("DELETE FROM patient_facts WHERE study_id=? AND participant_key=?",
                   (study_id, memory.key(participant)))
    db.audit("participant_forgotten", entity_type="participant", entity_id=memory.key(participant),
             study_id=study_id, rows=n)
    return n

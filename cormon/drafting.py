"""Template selection (topic x recipient x channel x severity) and rendering."""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo

from dateutil import parser as dtparser
from jinja2 import Environment, StrictUndefined

from . import config, db, llm, settings, textutil
from .config import Person, Study
from .triage import TIER_LABELS

_env = Environment(undefined=StrictUndefined, autoescape=False, keep_trailing_newline=False)
AXES = ("topic", "recipient", "channel", "severity")


class DraftError(Exception):
    pass


def select(topic: str, recipient: str, channel: str, severity: str) -> dict:
    want = {"topic": topic, "recipient": recipient, "channel": channel, "severity": severity}
    best, best_score = None, -1
    for t in config.templates():
        score = 0
        ok = True
        for axis in AXES:
            v = t.get(axis, "*")
            if v == "*":
                continue
            if v != want[axis]:
                ok = False
                break
            score += 1
        if ok and score > best_score:
            best, best_score = t, score
    if best is None:
        raise DraftError(f"no template for topic={topic} recipient={recipient} channel={channel} severity={severity}")
    return best


def local_time(iso: str | None) -> str:
    if not iso:
        return "date unknown"
    tz = ZoneInfo(settings.get("system.timezone", "America/New_York"))
    try:
        return dtparser.isoparse(iso).astimezone(tz).strftime("%d %b %Y %H:%M")
    except (ValueError, TypeError):
        return iso


def item_link(item: dict) -> str:
    base = settings.get("web.base_url", "http://localhost:8080").rstrip("/")
    return f"{base}/items/{item['link_token']}"


def _person_ctx(p: Person | None, fallback: str) -> dict:
    if p is None:
        return {"name": fallback, "first_name": fallback, "email": None}
    return {"name": p.name, "first_name": p.first_name, "email": p.email}


def people_ctx(study: Study, site: str | None) -> dict:
    def first(role: str, fallback: str):
        ps = study.people_with_role(role, site)
        return _person_ctx(ps[0] if ps else None, fallback)

    return {
        "cra": first("cra", "the study CRA"),
        "crc": first("crc", "the site coordinator"),
        "clinops_lead": first("clinops_lead", "the sponsor ClinOps lead"),
        "medical_monitor": first("medical_monitor", ""),
    }


def item_context(study: Study, item: dict, recipient: Person | None, routed_to: str = "") -> dict:
    ev = item["evidence"] if isinstance(item["evidence"], dict) else db.unjs(item["evidence"], {})
    pattern = study.pattern(item.get("pattern_class") or "") or {}
    return {
        "study": {"id": study.study_id, "short_name": study.short_name, "title": study.title,
                  "alias": study.alias, "monitor_signature": study.monitor_signature},
        "item": {
            "id": item["id"], "title": item["title"], "tier": item["tier"],
            "tier_label": TIER_LABELS.get(item["tier"], item["tier"]),
            "participant_id": item.get("participant_id"),
            "site": study.site_name(item.get("site")),
            "pattern_class": item.get("pattern_class"),
            "pattern_label": pattern.get("label", item.get("pattern_class") or "study"),
            "link": item_link(item), "routed_to": routed_to or "study channel",
        },
        "finding": {
            "requirement": ev.get("rule_text", ""),
            "doc_title": ev.get("doc_title", ""),
            "doc_quote": ev.get("doc_quote", ""),
            "page": ev.get("doc_page", ""),
            "message_quote": ev.get("message_quote", ""),
            "message_subject": ev.get("message_subject") or "(no subject)",
            "message_date": local_time(ev.get("message_date")),
            "sender_name": ev.get("sender_name") or ev.get("sender_email") or "unknown sender",
            "unsure": ev.get("certainty") == "unsure",
            "unsure_reason": ev.get("unsure_reason") or "",
            "earlier": (f"{ev['earlier']['date']}, \"{ev['earlier']['subject']}\"" if ev.get("earlier") else ""),
        },
        "also": also_broken(study, ev),
        "recipient": _person_ctx(recipient, "Hello"),
        "people": people_ctx(study, item.get("site")),
    }


def also_broken(study: Study, ev: dict) -> list[dict]:
    """Other confirmed rules the same participant broke in the same message, shown under the lead finding."""
    out = []
    seen = {textutil.normalize(ev.get("doc_quote") or "")}
    for a in ev.get("additional") or []:
        key = textutil.normalize(a.get("doc_quote") or "")
        if key in seen:
            continue
        seen.add(key)
        title = a.get("doc_title")
        if not title:
            rule = db.q1("SELECT doc_id FROM rules WHERE id=?", (a.get("rule_id"),))
            title = study.doc(rule["doc_id"])["title"] if rule else "study documents"
        page = a.get("doc_page")
        out.append({"doc_title": title, "where": f"{title}, p. {page}" if page else title,
                    "doc_quote": a.get("doc_quote") or "", "message_quote": a.get("message_quote") or ""})
    return out


_LEFTOVER = re.compile(r"\{\{|\}\}|\{%|%\}|\bNone\b")


def render(template: dict, ctx: dict) -> tuple[str | None, str]:
    try:
        subject = _env.from_string(template["subject"]).render(**ctx).strip() if template.get("subject") else None
        body = _env.from_string(template["body"]).render(**ctx).strip()
    except Exception as e:
        raise DraftError(f"template {template['id']} could not be filled: {e}") from e
    for part in (subject or "", body):
        if _LEFTOVER.search(part):
            raise DraftError(f"template {template['id']} left a placeholder or empty value in the draft")
    if template.get("ai_edit"):
        body = ai_edit(body, ctx)
    return subject, body


EDIT_SYSTEM = """You tighten a drafted message from a clinical trial monitoring service. Keep its structure, order, greeting and signature. Keep every quoted passage, number, participant ID, item number, name and link exactly as written. Make it shorter and clearer if you can; do not add facts, advice, or any clinical judgment."""


def ai_edit(body: str, ctx: dict) -> str:
    schema = llm.obj({"body": {"type": "string"}})
    out = llm.structured("draft_edit", EDIT_SYSTEM, body, schema, "edited_message")["body"]
    must_keep = [ctx["finding"]["message_quote"], ctx["finding"]["doc_quote"], ctx["item"]["link"],
                 str(ctx["item"]["id"])]
    if all(k in out for k in must_keep if k) and not _LEFTOVER.search(out):
        return out
    return body

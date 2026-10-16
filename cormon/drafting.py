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
            "link": item_link(item), "routed_to": routed_to or "nobody else (posted here only)",
        },
        "finding": {
            "requirement": ev.get("rule_text", ""),
            "doc_title": ev.get("doc_title", ""),
            "doc_quote": ev.get("doc_quote", ""),
            "page": ev.get("doc_page", ""),
            "where": _where(ev.get("doc_title", ""), ev.get("doc_page"), ev.get("rule_text")),
            "next_step": ev.get("next_step") or "",
            "message_quote": ev.get("message_quote", ""),
            "message_subject": ev.get("message_subject") or "(no subject)",
            "message_date": local_time(ev.get("message_date")),
            "sender_name": ev.get("sender_name") or ev.get("sender_email") or "unknown sender",
            "unsure": ev.get("certainty") == "unsure",
            "unsure_reason": ev.get("unsure_reason") or "",
            "earlier": (f"{ev['earlier']['date']}, \"{ev['earlier']['subject']}\"" if ev.get("earlier") else ""),
        },
        "also": also_broken(study, ev),
        "problems": problems(study, item, ev),
        "bundle": False,
        "recipient": _person_ctx(recipient, "Hello"),
        "people": people_ctx(study, item.get("site")),
    }


def _where(title: str, page, section: str | None) -> str:
    """Where a quote is in the documents: its page, or its section for a quote from the protocol guide."""
    if page:
        return f"{title}, page {page}"
    return f"{title}, section {section}" if section else title


def also_broken(study: Study, ev: dict) -> list[dict]:
    """Other confirmed rules the same participant broke in the same message, shown under the lead finding."""
    out = []
    key_of = lambda e, title: (textutil.normalize(e.get("doc_quote") or title or ""),
                               textutil.normalize(e.get("message_quote") or ""))
    seen = {key_of(ev, ev.get("headline"))}
    for a in ev.get("additional") or []:
        key = key_of(a, a.get("headline"))
        if key in seen:
            continue
        seen.add(key)
        title = a.get("doc_title")
        if not title:
            rule = db.q1("SELECT doc_id FROM rules WHERE id=?", (a.get("rule_id"),))
            title = study.doc(rule["doc_id"])["title"] if rule else "study documents"
        out.append({"doc_title": title, "where": _where(title, a.get("doc_page"), a.get("rule_text")),
                    "doc_quote": a.get("doc_quote") or "", "message_quote": a.get("message_quote") or "",
                    "title": a.get("headline") or a.get("rule_text") or "Also against this rule",
                    "next_step": a.get("next_step") or "", "unsure": a.get("certainty") == "unsure",
                    "unsure_reason": a.get("unsure_reason") or "", "detail": a.get("detail") or ""})
    return out


_EARLIER = re.compile(r"^\(earlier message, ([^)]+)\)\s*")
_FOOTNOTE = re.compile(r"\s*\[\d+\]")


def _shown(p: dict) -> dict:
    """A problem as the alert shows it: this message's words apart from an earlier message's, and the schedule
    table's footnote marks left out of the rule (the exact words stay on the details page)."""
    now, earlier = [], []
    for piece in (p.get("message_quote") or "").split(" ... "):
        m = _EARLIER.match(piece.strip())
        if m:
            earlier.append({"when": m.group(1), "words": piece.strip()[m.end():]})
        elif piece.strip() and earlier:  # the earlier message's own quote can have gaps too
            earlier[-1]["words"] += f" ... {piece.strip()}"
        elif piece.strip():
            now.append(piece.strip())
    return dict(p, message_quote=" ... ".join(now) or p.get("message_quote") or "", earlier_quotes=earlier,
                doc_quote=_FOOTNOTE.sub("", p.get("doc_quote") or ""))


_UNSURE = re.compile(r"^Please check \(not sure\):\s*", re.I)


def problems(study: Study, item: dict, ev: dict) -> list[dict]:
    """Every problem in the alert, the lead one first, each with its own next step and evidence."""
    lead = {"title": _UNSURE.sub("", item["title"]), "unsure": ev.get("certainty") == "unsure",
            "unsure_reason": ev.get("unsure_reason") or "", "next_step": ev.get("next_step") or "",
            "message_quote": ev.get("message_quote", ""), "doc_quote": ev.get("doc_quote", ""),
            "where": _where(ev.get("doc_title", ""), ev.get("doc_page"), ev.get("rule_text")),
            "detail": ev.get("detail") or ""}
    tier = TIER_LABELS.get(item.get("tier"), item.get("tier") or "")
    return [dict(_shown(p), n=n, item_id=item["id"], tier_label=tier)
            for n, p in enumerate([lead] + also_broken(study, ev), 1)]


def bundle_problems(study: Study, its: list[dict]) -> list[dict]:
    """The problems of several items sent in one message, numbered through."""
    ps = [p for it in its
          for p in problems(study, it, it["evidence"] if isinstance(it["evidence"], dict) else db.unjs(it["evidence"], {}))]
    return [dict(p, n=n) for n, p in enumerate(ps, 1)]


_LEFTOVER = re.compile(r"\{\{|\}\}|\{%|%\}|\bNone\b")


def render(template: dict, ctx: dict) -> tuple[str | None, str]:
    try:
        subject = _env.from_string(template["subject"]).render(**ctx).strip() if template.get("subject") else None
        body = _env.from_string(template["body"]).render(**ctx).strip()
    except Exception as e:
        raise DraftError(f"template {template['id']} could not be filled: {e}") from e
    for part in (subject or "", body):
        if m := _LEFTOVER.search(part):
            near = re.sub(r"\s+", " ", part[max(0, m.start() - 80):m.end() + 20])
            raise DraftError(f"template {template['id']} left a placeholder or empty value in the draft: ...{near}...")
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

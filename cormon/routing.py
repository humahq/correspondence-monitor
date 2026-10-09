"""Turns a classified item into outbound messages according to config/routing.yaml."""

from __future__ import annotations

from email.utils import make_msgid

from . import capture, config, db, drafting, items, settings
from .config import Person, Study


def enqueue(*, purpose: str, channel: str, destination: str, body: str, item_id: int | None = None,
            message_id: int | None = None, template_id: str | None = None, from_addr: str | None = None,
            to: list[str] | None = None, cc: list[str] | None = None, subject: str | None = None,
            meta: dict | None = None, status: str = "queued", study_id: str | None = None) -> int:
    rfc_id = make_msgid(domain=settings.get("mailbox.address", "bot@huma.ai").split("@")[1]) \
        if channel == "email" else None
    oid = db.ex(
        "INSERT INTO outbox (item_id, message_id, purpose, channel, destination, template_id, from_addr, to_addrs,"
        " cc_addrs, subject, body, meta, status, rfc_message_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (item_id, message_id, purpose, channel, destination, template_id, from_addr, db.js(to or []),
         db.js(cc or []), subject, body, db.js(meta or {}), status, rfc_id, db.now()),
    )
    db.audit("draft_created", entity_type="item" if item_id else "outbox", entity_id=item_id or oid,
             study_id=study_id, outbox_id=oid, purpose=purpose, channel=channel, destination=destination,
             template=template_id, to=to, cc=cc, subject=subject, status=status)
    return oid


def from_alias(study: Study, msg: dict) -> str:
    """The alias the message arrived on, spelled as configured: Gmail only honours a send-as address it
    recognises, and inbound addresses are stored lower-cased."""
    alias = msg.get("recipient_alias")
    if not alias or alias.lower() == study.alias.lower():
        return study.alias
    return alias


def _sender(study: Study, msg: dict) -> tuple[Person | None, str | None]:
    return study.person_by_email(msg.get("from_addr")), msg.get("from_addr")


def _resolve_people(study: Study, entry: dict, item: dict, msg: dict) -> list[tuple[str, Person | None, str]]:
    """Return [(recipient_axis, person, email)] for an email routing entry."""
    out = []
    if "to_role" in entry:
        people = study.people_with_role(entry["to_role"], item.get("site"))
        if entry["to_role"] == "cra" and item.get("recipient_person"):
            chosen = study.person(item["recipient_person"])
            if chosen:
                people = [chosen]
        for p in people:
            if p.email:
                out.append((entry["to_role"], p, p.email))
    elif entry.get("to") == "sender":
        person, addr = _sender(study, msg)
        need = entry.get("only_if_sender_role")
        if need and (person is None or need not in person.roles):
            return []
        if addr:
            axis = next((r for r in (person.roles if person else []) if r), "sender")
            out.append((axis, person, addr))
    return out


def _cc(study: Study, entry: dict, item: dict, exclude: set[str]) -> list[str]:
    cc = []
    for role in entry.get("cc_roles", []):
        for p in study.people_with_role(role, item.get("site")):
            if p.email and p.email.lower() not in exclude:
                cc.append(p.email)
    return cc


def _routed_to(study: Study, entries: list[dict], item: dict, msg: dict) -> str:
    names = []
    for e in entries:
        if e["destination"] == "email":
            for axis, p, addr in _resolve_people(study, e, item, msg):
                label = study.role_label(axis) if axis in {r["id"] for r in study.roles} else axis
                names.append(f"{p.name if p else addr} ({label})")
        elif e["destination"] == "teams_alert_channel":
            names.append("alert channel")
        elif e["destination"] == "clearing_list":
            names.append("clearing list")
    return ", ".join(dict.fromkeys(names)) or "study channel only"


def teams_target(study: Study, destination: str) -> dict:
    key = "study_channel" if destination == "teams_study_channel" else "alert_channel"
    return dict(study.teams.get(key) or {}, key=key)


def route_item(item_id: int, *, force: bool = False, actor: str = "system") -> list[int]:
    item = items.get(item_id)
    study = config.study(item["study_id"])
    if item["state"] in ("suppressed",) or (not item["counts"] and item["kind"] != "out_of_scope"):
        db.audit("routing_skipped", entity_type="item", entity_id=item_id, study_id=study.study_id,
                 reason=f"state={item['state']} counts={item['counts']}")
        return []
    if item["routed"] and not force:
        return []
    rconf = config.routing()
    if item["kind"] == "out_of_scope":
        entries = rconf.get("out_of_scope", [])
    elif item["kind"] == "good_news":
        entries = rconf.get("good_news", [])
    else:
        entries = rconf.get("tiers", {}).get(item["tier"], [])

    msg = capture.message(item["last_message_id"] if force else item["first_message_id"])
    routed_to = _routed_to(study, entries, item, msg)
    topic_default = "good_news" if item["kind"] == "good_news" else (item.get("pattern_class") or "*")
    created: list[int] = []
    sent_to: set[str] = set()

    for entry in entries:
        dest = entry["destination"]
        topic = entry.get("topic", topic_default)
        if dest in ("clearing_list", "digest"):
            db.audit("routed", entity_type="item", entity_id=item_id, study_id=study.study_id, destination=dest)
            continue
        if dest.startswith("teams_"):
            axis = entry.get("recipient", "study_team")
            tpl = drafting.select(topic, axis, "teams", item["tier"])
            ctx = drafting.item_context(study, item, None, routed_to)
            _, body = drafting.render(tpl, ctx)
            target = teams_target(study, dest)
            created.append(enqueue(
                purpose=f"teams_{axis}", channel="teams", destination=dest, body=body, item_id=item_id,
                template_id=tpl["id"], meta={"target": target, "thread_key": item["thread_key"]},
                study_id=study.study_id))
            continue
        if dest == "email":
            for axis, person, addr in _resolve_people(study, entry, item, msg):
                if addr.lower() in sent_to:
                    continue
                sent_to.add(addr.lower())
                tpl = drafting.select(topic, axis, "email", item["tier"])
                ctx = drafting.item_context(study, item, person, routed_to)
                subject, body = drafting.render(tpl, ctx)
                cc = _cc(study, entry, item, {addr.lower()})
                meta = {"thread_key": item["thread_key"], "item_id": item_id}
                if entry.get("to") == "sender" and msg.get("rfc_message_id"):
                    meta["in_reply_to"] = msg["rfc_message_id"]
                    meta["references"] = (msg.get("refs") or []) + [msg["rfc_message_id"]]
                    meta["gmail_thread"] = msg["thread_key"].split(":", 1)[1] if msg["channel"] == "email" else None
                status = "queued" if (item["send_draft"] or item["kind"] == "out_of_scope") else "held"
                created.append(enqueue(
                    purpose=f"email_{axis}", channel="email", destination="email", body=body,
                    subject=subject, item_id=item_id, template_id=tpl["id"],
                    from_addr=from_alias(study, msg), to=[addr], cc=cc, meta=meta,
                    status=status, study_id=study.study_id))
            continue
        db.audit("routing_unknown_destination", entity_type="item", entity_id=item_id,
                 study_id=study.study_id, destination=dest)

    db.ex("UPDATE items SET routed=1, updated_at=? WHERE id=?", (db.now(), item_id))
    db.audit("item_routed", entity_type="item", entity_id=item_id, study_id=study.study_id, actor=actor,
             tier=item["tier"], routed_to=routed_to, outbox_ids=created, forced=force or None)
    return created


def copy_nudge(study: Study, msg: dict, first_writer: str) -> int | None:
    """Ask the first person who wrote while the monitor was off the thread to keep copying it."""
    if not first_writer or first_writer.lower() == settings.get("mailbox.address", "").lower():
        return None
    person = study.person_by_email(first_writer)
    tpl = drafting.select("copy_nudge", "*", "email", "*")
    ctx = {
        "study": {"id": study.study_id, "short_name": study.short_name, "title": study.title,
                  "alias": study.alias, "monitor_signature": study.monitor_signature},
        "finding": {"message_subject": msg.get("subject") or "(no subject)"},
        "recipient": drafting._person_ctx(person, "Hello"),
    }
    subject, body = drafting.render(tpl, ctx)
    meta = {"thread_key": msg["thread_key"], "in_reply_to": msg.get("rfc_message_id"),
            "references": (msg.get("refs") or []) + ([msg["rfc_message_id"]] if msg.get("rfc_message_id") else []),
            "gmail_thread": msg["thread_key"].split(":", 1)[1]}
    return enqueue(purpose="copy_nudge", channel="email", destination="email", body=body, subject=subject,
                   message_id=msg["id"], template_id=tpl["id"],
                   from_addr=from_alias(study, msg), to=[first_writer], meta=meta,
                   study_id=study.study_id)

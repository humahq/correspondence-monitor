"""Who is on a study email (sender, To, Cc), or writes to the bot in chat, that the study does not list (Eliot, Nov 10).

A message with someone on it the study does not list gets no answer: the bot replies to the sender only, with no
study information, to say who should not be on it. In chat only the writer is checked, not the room's members. Someone whose email domain the study approves
(`email_domains:` in study.yaml) is likely a new colleague not set up yet, so the site's CRA is asked to add them.
Anyone else is a reportable event: the CRA and the admins are told. A domain is approved only by name, never by a
listed person's address (one gmail.com person does not make every gmail.com address safe)."""

from __future__ import annotations

from . import answers, db, routing, settings
from .config import Study


def _bot(study: Study, addr: str) -> bool:
    local, _, domain = (settings.get("mailbox.address") or "bot@huma.ai").lower().partition("@")
    a_local, _, a_domain = addr.partition("@")
    return addr == study.alias.lower() or (a_domain == domain and (a_local == local or a_local.startswith(local + "+")))


def listed(study: Study, addr: str) -> bool:
    return study.person_by_email(addr) is not None or any((p.teams_upn or "").lower() == addr for p in study.people)


def approved_domain(study: Study, addr: str) -> bool:
    domain = addr.rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d)
               for d in (str(x).strip().lower().lstrip("@") for x in study.raw.get("email_domains") or []) if d)


def outside(study: Study, msg: dict) -> list[dict]:
    """Each address on the message the study does not list, with whether its domain is approved."""
    seen, out = set(), []
    others = list(msg.get("to_addrs") or []) + list(msg.get("cc_addrs") or []) if msg.get("channel") == "email" else []
    for a in [msg.get("from_addr")] + others:
        a = (a or "").strip().lower()
        if not a or "@" not in a or a in seen or _bot(study, a):
            continue
        seen.add(a)
        if not listed(study, a):
            out.append({"address": a, "domain_approved": approved_domain(study, a)})
    return out


def _cras(study: Study, msg: dict) -> list[str]:
    sender = study.person_by_email(msg.get("from_addr"))
    sites = [s for s in (sender.sites if sender else []) if s != "*"]
    people = study.people_with_role("cra", sites[0] if len(sites) == 1 else None)
    return sorted({p.email for p in people if p.email})


def _names(addrs: list[str]) -> str:
    return addrs[0] if len(addrs) == 1 else ", ".join(addrs[:-1]) + " and " + addrs[-1]


def to_sender(study: Study, msg: dict, out: list[dict]) -> str:
    sender = (msg.get("from_addr") or "").lower()
    new = [o["address"] for o in out if o["domain_approved"]]
    strangers = [o["address"] for o in out if not o["domain_approved"]]
    lines = []
    if sender in strangers and msg.get("channel") != "email":
        lines.append(f"I can't answer this: {sender} is not on this study's list, so I can't send study information "
                     "here. If you work on this study, please ask the study's CRA to add you.")
    elif sender in strangers:
        lines.append(f"I can't answer this email: {sender} is not on this study's list, so I can't send study "
                     "information to it. If you work on this study, please write from the address the study has "
                     "for you.")
    elif strangers:
        lines.append(f"I haven't answered this email because {_names(strangers)} "
                     f"{'is' if len(strangers) == 1 else 'are'} on it and may not receive study information. "
                     "Please send it again without them. I have told the study team.")
    if sender in new:
        lines.append(f"{sender} is not set up on this study yet, so I haven't answered. I've asked the study's "
                     "CRA to add you; once they have, please send it again.")
    elif new and not strangers:
        lines.append(f"I haven't answered this email yet because {_names(new)} "
                     f"{'is' if len(new) == 1 else 'are'} not set up on this study. I've asked the study's CRA "
                     "to add them; once they have, please send it again.")
    elif new:
        lines.append(f"{_names(new)} {'is' if len(new) == 1 else 'are'} not set up on this study yet either; "
                     "I've asked the study's CRA to add them.")
    return "\n\n".join(lines)


def handle(study: Study, msg: dict, out: list[dict]) -> dict:
    """Reply to the sender only, ask the CRA to add new colleagues, and report anyone else. Returns what was done."""
    from .compliance import alerts
    sender = (msg.get("from_addr") or "").lower()
    new = [o["address"] for o in out if o["domain_approved"]]
    strangers = [o["address"] for o in out if not o["domain_approved"]]
    base = (settings.get("web.base_url") or "").rstrip("/")
    link = f"\nSee it: {base}/admin/messages/{msg['id']}\n" if base else ""
    email = msg.get("channel") == "email"
    what = (f"An email to the study {study.short_name} (\"{msg.get('subject') or '(no subject)'}\", from {sender}) "
            "had people on it the study does not list." if email else
            f"A chat message to the bot in {study.short_name} came from {sender}, who is not on the study's list.")
    sent = {}
    if sender and not _bot(study, sender):
        sent["sender"] = answers._post(study, msg, to_sender(study, msg, out), purpose="outside_recipients_sender")
    cras = _cras(study, msg)
    if cras and (new or strangers):
        lines = [f"{what} The bot did not answer it, and sent no study information to anyone on it."]
        if new:
            lines.append(f"New, from an approved domain, so probably a colleague to set up: {_names(new)}. "
                         "If they work on this study, please ask for them to be added to its people list.")
        if strangers:
            lines.append(f"Not from an approved domain: {_names(strangers)}. This may be a privacy incident to "
                         + ("report: the email itself went to them." if email else "report."))
        sent["cra"] = routing.enqueue(
            purpose="outside_recipients_cra", channel="email", destination="email",
            body="\n\n".join(lines) + f"\n{link}\n{study.monitor_signature}",
            subject=(f"Privacy: someone outside the study was on {'an email' if email else 'a chat'} · "
                     f"{study.short_name}" if strangers else f"Please set up a new person · {study.short_name}"),
            message_id=msg["id"], from_addr=study.alias, to=cras, study_id=study.study_id)
    if strangers:
        sent["admins"] = alerts.notify(
            f"outside_recipients:{msg['id']}", f"Privacy: someone outside {study.study_id} wrote to or was on a message",
            f"Message {msg['id']} to study {study.study_id} (from {sender}, by {msg.get('channel')}) had "
            f"{_names(strangers)} on it, whose email domain the study does not approve. The bot did not answer it "
            f"and told the sender and the CRA.\n{link}", every_minutes=10 ** 7)
    db.audit("outside_recipients", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
             new_colleagues=new, not_approved=strangers, reportable=bool(strangers), told=sent)
    return {"new": new, "not_approved": strangers, "sent": sent}

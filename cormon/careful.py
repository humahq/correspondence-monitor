"""People the bot is careful with: principal investigators and other doctors (config/careful.yaml, and a study's
own `careful:` list). A doctor criticised in front of others may stop working with the sponsor.

Every chat post is checked here before it is queued (routing.enqueue). In a chat where a careful person is present,
an alert that could read as criticism of them is not posted: it goes by email to the rule's report_to roles. A reply
that could read that way is reworded calmly. Whether a post is critical of someone is decided by code first (the
message it is about was written by them, or it names them or their role), then by an AI check (for an alert: is
it a doctor's own action, see doctor_did); code decides what happens with the answer. A chat with no careful person in it is not touched: no extra check and no AI call."""

from __future__ import annotations

import re

from . import capture, config, db, items, llm, rooms
from .channels.gchat import chat_text
from .config import Person, Study
from .settings import CONFIG_DIR, load_yaml

CALM_REPLY = "Thank you for letting me know. I have shared this with the study team so they can follow up."
SAFETY = "Contact the PI and the Medical Monitor now"

CHECK_SYSTEM = """You check a post a clinical-trial study monitor bot is about to make in a chat, before it is sent.
Some people in the chat are doctors on the study (for example the principal investigator). A doctor who is
criticised in front of others may stop working with the sponsor, so the bot must never post anything that could be
read as criticising, blaming or embarrassing them.
- ok: false when the post could be read that way about any of the people listed: it says, or lets readers work out,
  that something one of them did, decided, ordered, wrote or is responsible for was wrong, late, missing or against
  the rules, or it corrects one of them in front of others.
- ok: true for neutral facts and study rules that point at nobody in the chat, a calm answer to a question,
  problems clearly about someone else's own work, thanks, and reminders to report serious harm.
- about: the names, from the list, of the people it could be read as criticising; [] when ok.
- why: one short sentence."""
CHECK_SCHEMA = llm.obj({"ok": {"type": "boolean"}, "about": {"type": "array", "items": {"type": "string"}},
                        "why": {"type": "string"}})

# An alert is kept out of a chat a doctor is in only when it is about the doctor's own actions (Eliot, Oct 26): a
# doctor is said to have done what went wrong (explicit), or what went wrong is a task only a physician does
# (implied). Anything else is posted, even though the PI is responsible for the whole site. One fast Decisions
# call asks each as its own yes/no question: on 71 test alerts this was right on all, where one combined question
# missed most implied ones (demo/doctor-check).
DOCTOR_EXPLICIT = ("A doctor (by name, as 'the PI', 'the investigator', 'the physician', 'our neurologist', 'Dr.', "
                   "or by writing the message themselves) is said to have done, decided, ordered or approved what "
                   "went wrong.")
DOCTOR_TASKS = {
    "exam": "Is what went wrong about a physical or neurological examination (missed, incomplete, not documented, "
            "or something found in it not followed up)?",
    "history": "Is what went wrong about reviewing or recording the participant's medical history?",
    "ae_judgment": "Is what went wrong a medical judgment about an adverse event or illness (whether it is related "
                   "to the study drug, serious, how severe, or whether it needed recording or reporting)?",
    "dose_decision": "Is what went wrong a decision to change, reduce, hold, stop or continue the study drug dose?",
    "prescribing": "Is what went wrong a medicine other than the study drug being prescribed or ordered for the "
                   "participant? Dispensing, preparing, storing or taking the study drug is not prescribing.",
    "eligibility": "Is what went wrong confirming that a participant was eligible, or enrolling or randomising them?",
    "results": "Is what went wrong how an ECG, lab or scan result was medically interpreted or acted on?",
    "unblinding": "Is what went wrong breaking the blind for a participant?",
    "ending": "Is what went wrong a decision to end a participant's treatment or participation?",
}
DOCTOR_LINE = 0.5

# the same two questions for the chat model, used when the Decisions call fails
DOCTOR_SYSTEM = f"""A clinical-trial monitor bot is about to post an alert in a chat that the study doctor (the
principal investigator) reads. Answer two questions about what went wrong:
- explicit: true when {DOCTOR_EXPLICIT[0].lower()}{DOCTOR_EXPLICIT[1:]}
- implied: true when no doctor is named but what went wrong is a task only a physician performs: a physical or
  neurological examination, reviewing medical history, judging whether an adverse event is related to the drug,
  serious, or how severe it is, deciding to change, hold or stop a dose, prescribing or approving a medicine,
  confirming eligibility, reading an ECG or lab result as clinically significant, deciding to unblind or to end
  a participant's treatment.
Both false when what went wrong is work anyone on the site team can do (samples, shipping, scheduling, visit
timing, data entry, drug storage, dose preparation, questionnaires, vital signs, paperwork), even though the PI is
generally responsible for the site."""
DOCTOR_SCHEMA = llm.obj({"explicit": {"type": "boolean"}, "implied": {"type": "boolean"}})

REWORD_SYSTEM = """You reword a post a clinical-trial study monitor bot is about to make in a chat that doctors on
the study can read, so that nobody in the chat could read it as criticism, blame or embarrassment. Keep every fact,
number, date, participant id, item number and link exactly as written, and keep its meaning. Say what the study
documents say, not what anyone did wrong. Do not add facts, advice or opinions, and make it no longer than the
original. No greeting and no sign-off."""
REWORD_SCHEMA = llm.obj({"text": {"type": "string"}})


def _file() -> dict:
    path = CONFIG_DIR / "careful.yaml"
    return load_yaml(path) if path.exists() else {}


def rules(study: Study) -> list[dict]:
    """The general rules, with the study's own added or changed by id; a rule with `enabled: false` is left out."""
    by_id = {r["id"]: dict(r) for r in _file().get("rules") or []}
    for r in (getattr(study, "raw", None) or {}).get("careful") or []:
        by_id[r["id"]] = {**by_id.get(r["id"], {}), **r}
    return [r for r in by_id.values() if r.get("enabled", True) and r.get("roles")]


def rule_for(study: Study, person: Person | None) -> dict | None:
    if person is None:
        return None
    return next((r for r in rules(study) if set(r["roles"]) & set(person.roles)), None)


def reader_rule(study: Study | None, addr: str | None) -> dict | None:
    """The careful rule for whoever has this address; None for anyone else, and on any error."""
    try:
        return rule_for(study, study.person_by_email(addr)) if study and addr else None
    except Exception:
        return None


def calm_reply(rule: dict) -> str:
    return rule.get("calm_reply") or _file().get("calm_reply") or CALM_REPLY


def tone(name: str | None) -> str:
    """The wording instruction for a rule's tone ("calm"); "" for none."""
    return ((_file().get("tones") or {}).get(name) or "").strip() if name else ""


def _label(study: Study, p: Person, rule: dict) -> str:
    return ", ".join(study.role_label(r) for r in p.roles if r in rule["roles"])


def _who(study: Study, people: list[tuple[Person, dict]]) -> str:
    return "; ".join(f"{p.name} ({_label(study, p, r)})" for p, r in people)


def present(study: Study, space: str) -> tuple[list[tuple[Person, dict]], dict | None]:
    """The careful people in a chat space, each with their rule, and the room's members (None when the study has
    no careful people, so the room is not looked up)."""
    people = [(p, r) for p in study.people if p.email and (r := rule_for(study, p))]
    if not people:
        return [], None
    room = rooms.members(space)
    return [(p, r) for p, r in people if p.email.lower() in room["emails"]], room


def _says(text: str, word: str, exact: bool) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, 0 if exact else re.I))


def named(study: Study, text: str, people: list[tuple[Person, dict]]) -> list[str]:
    """The careful people a text names: by full, first or last name ("Dr. Levitt"), or by their role ("the PI")."""
    out = []
    for p, r in people:
        names = [n for n in [p.name, *p.name.split()] if len(n) > 2]
        words = [study.role_label(x).lower() for x in p.roles if x in r["roles"]] + list(r.get("words") or [])
        if any(_says(text, n, True) for n in names) or any(_says(text, w, w != w.lower()) for w in words):
            out.append(p.name)
    return out


def _alert_words(item_ids: list[int]) -> str:
    """What an alert says happened: its problems and the message's own words (not the protocol's)."""
    parts = []
    for i in item_ids:
        try:
            ev = (it := items.get(i))["evidence"]
        except KeyError:
            continue
        parts += [it["title"], ev.get("message_quote"), ev.get("headline"), ev.get("detail"), ev.get("next_step")]
        for a in ev.get("additional") or []:
            parts += [a.get("headline"), a.get("message_quote"), a.get("detail"), a.get("next_step")]
    return "\n".join(x for x in parts if x)


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", s))


def _ask(study: Study, here: list[tuple[Person, dict]], about_msg: str, text: str) -> dict:
    return llm.structured("careful_check", CHECK_SYSTEM,
                          f"These people are in this chat: {_who(study, here)}. Is this message OK to post here, "
                          f"or could it be read as criticising, blaming or embarrassing any of them?\n\n"
                          f"{about_msg}MESSAGE TO POST:\n{text}", CHECK_SCHEMA, "careful_check")


def doctor_did(writer: str, message: str, alert: str) -> dict:
    """Whether an alert is about a doctor's own actions: {critical, why, scores, by}. The Decisions API first; the
    chat model when it fails. Raises when both fail."""
    said = f"SITE MESSAGE (written by {writer or 'someone at the site'}):\n{message}\n\nALERT TO POST:\n{alert}"
    try:
        scores = llm.decide("careful_doctor", said, [{"type": "predicate", "name": "explicit",
                                                      "instructions": DOCTOR_EXPLICIT}]
                            + [{"type": "predicate", "name": k, "instructions": v} for k, v in DOCTOR_TASKS.items()])
        top = max(scores, key=scores.get)
        hit = scores[top] >= DOCTOR_LINE
        why = (("it says a doctor did what went wrong" if top == "explicit" else
                f"what went wrong is a task only a doctor does ({top.replace('_', ' ')})") if hit else
               "what went wrong is not a doctor's own action")
        return {"critical": hit, "why": why, "scores": {k: round(v, 2) for k, v in scores.items()}, "by": "decisions"}
    except Exception as e:
        first = str(e)[:200]
    r = llm.structured("careful_doctor", DOCTOR_SYSTEM, said, DOCTOR_SCHEMA, "doctor")
    why = ("it says a doctor did what went wrong" if r["explicit"] else
           "what went wrong is a task only a doctor does" if r["implied"] else "what went wrong is not a doctor's own action")
    return {"critical": r["explicit"] or r["implied"], "why": why, "scores": r, "by": f"chat model ({first})"}


def _reword(study: Study, text: str, about: list[tuple[Person, dict]], why: str) -> str | None:
    """The post reworded calmly; None when the AI fails or its wording adds a number or drops a link."""
    try:
        said = llm.structured("careful_reword", REWORD_SYSTEM + "\n" + tone(about[0][1].get("tone") or "calm"),
                              f"PEOPLE IN THE CHAT: {_who(study, about)}\nWHY IT NEEDS REWORDING: {why}"
                              f"\n\nPOST:\n{text}", REWORD_SCHEMA, "post")["text"].strip()
    except Exception:
        return None
    links = re.findall(r"https?://[^\s|>]+", text)
    if not said or not _numbers(said) <= _numbers(text) or any(u not in said for u in links):
        return None
    return said


def check_post(*, study_id: str | None, purpose: str, body: str, item_id: int | None = None,
               message_id: int | None = None, meta: dict | None = None) -> dict | None:
    """The careful check on one chat post, before it is queued. None: post it as it is (no careful person in the
    room). Otherwise {action: send | reword | withhold, body, status, note}, logged as careful_check."""
    target = (meta or {}).get("target") or {}
    if not study_id or not target.get("space") or purpose == "chat_received":   # fixed text naming nobody
        return None
    try:
        study = config.study(study_id)
        here, room = present(study, target["space"])
    except Exception:
        return None
    if not here:
        return None
    try:
        return _check(study, here, room, target, purpose, body, item_id, message_id, meta)
    except Exception as e:  # never stops a post: an alert is kept out to be safe, anything else goes as it is
        alert = bool(item_id) and purpose.startswith("teams_")
        why = f"the careful check failed ({str(e)[:300]})"
        db.audit("careful_check", entity_type="item" if item_id else "message", entity_id=item_id or message_id,
                 study_id=study_id, space=target["space"], purpose=purpose, present=_who(study, here),
                 decision="withhold" if alert else "send", reason=why)
        note = {"about": [p.name for p, _ in here], "who": _who(study, here), "room": target.get("name") or "the chat",
                "why": why, "rules": list(dict.fromkeys(r["id"] for _, r in here))}
        return {"action": "withhold", "body": body, "status": "withheld", "note": note} if alert else None


def _check(study: Study, here: list[tuple[Person, dict]], room: dict, target: dict, purpose: str, body: str,
           item_id: int | None, message_id: int | None, meta: dict | None) -> dict:
    study_id = study.study_id
    try:
        lead = items.get(item_id) if item_id else None
    except KeyError:
        lead = None
    alert = bool(lead) and lead["kind"] == "finding" and purpose.startswith("teams_")
    try:
        source = capture.message(message_id or lead["last_message_id"]) if (message_id or lead) else None
    except Exception:
        source = None
    writer = ((source or {}).get("from_addr") or "").lower()
    wrote = [p.name for p, _ in here if p.email.lower() == writer]
    text = chat_text(body)
    ids = (meta or {}).get("item_ids") or ([item_id] if item_id else [])
    names = named(study, _alert_words(ids) if alert else text.replace(SAFETY, ""), here)
    ai = error = None
    if not alert and SAFETY in text:
        critical, about, reason = False, [], "it is about reporting serious harm, which is always sent"
    elif alert and (wrote or names):
        critical, about = True, list(dict.fromkeys(wrote + names))
        reason = f"the message it is about was written by {', '.join(wrote)}" if wrote else f"it names {', '.join(names)}"
    elif alert:
        sender = study.person_by_email(writer) if writer else None
        by = sender.name if sender else (source or {}).get("from_name") or writer
        try:
            ai = doctor_did(by, (source or {}).get("body_text") or "", text)
            critical, reason = ai["critical"], ai["why"]
            about = [p.name for p, _ in here] if critical else []
        except Exception as e:
            error = str(e)[:300]
            critical, about = True, [p.name for p, _ in here]
            reason = f"the AI check failed ({error}), so the alert is kept out to be safe"
    else:
        sender = study.person_by_email(writer) if writer else None
        about_msg = (f"IT IS ABOUT THIS MESSAGE, WRITTEN BY {sender.name if sender else source.get('from_name') or writer}:"
                     f"\n{source.get('body_text') or ''}\n\n" if source else "")
        try:
            ai = _ask(study, here, about_msg, text)
        except Exception as e:
            error = str(e)[:300]
        if ai:
            critical, reason = not ai["ok"], ai["why"]
            about = [p.name for p, _ in here if p.name in ai["about"]] or ([] if ai["ok"] else [p.name for p, _ in here])
        else:
            critical = alert or bool(names)
            about = names or [p.name for p, _ in here]
            reason = f"the AI check failed ({error})" + (", so the alert is kept out to be safe" if alert else "")
    hit = [(p, r) for p, r in here if p.name in about]
    action, status, out = "send", None, body
    if critical and alert and any(r.get("in_group") == "private" for _, r in hit):
        action, status = "withhold", "withheld"
    elif critical and not alert:
        action = "reword"
        out = _reword(study, text, hit or here, reason)
        alone = (source or {}).get("visibility") == "direct"   # nobody else reads a 1-on-1: the answer goes
        try:
            again = bool(out) and (alone or not ai or _ask(study, here, about_msg, out)["ok"])
        except Exception:
            again = False
        if not again:  # in a group the facts themselves can blame: then only the calm line goes
            out = calm_reply((hit or here)[0][1])
    room_name = target.get("name") or ("this group chat" if target.get("key") == "group" else "the chat")
    db.audit("careful_check", entity_type="item" if item_id else "message", entity_id=item_id or message_id,
             study_id=study_id, space=target["space"], room=room_name, purpose=purpose, present=_who(study, here),
             members_complete=room["complete"], members_error=room["error"], wrote=wrote or None,
             named=names or None, ai=ai, ai_error=error, decision=action, about=about or None, reason=reason)
    note = {"about": about, "who": _who(study, hit), "room": room_name, "why": reason,
            "rules": list(dict.fromkeys(r["id"] for _, r in hit))}
    return {"action": action, "body": out, "status": status, "note": note}


def report_privately(study: Study, item: dict, others: list[dict], msg: dict, created: list[int],
                     context) -> list[int]:
    """By email to the careful rule's report_to roles: an alert kept out of a chat because of a careful person,
    or one found in what a careful person told the bot 1-on-1. Anyone already emailed that alert is only logged.
    context(person) gives the alert's template context. A failure here is logged and stops no other message."""
    try:
        return _report(study, item, others, msg, created, context)
    except Exception as e:
        db.audit("careful_report_failed", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                 error=str(e)[:500])
        return []


def _report(study: Study, item: dict, others: list[dict], msg: dict, created: list[int], context) -> list[int]:
    notes = []
    if created:
        rows = db.q(f"SELECT meta FROM outbox WHERE status='withheld' AND id IN ({','.join('?' * len(created))})",
                    created)
        notes = [db.unjs(r["meta"], {}).get("careful") or {} for r in rows]
        notes = [dict(n, why=f"{n['who']} can read {n['room']} ({n['why']})") for n in notes if n.get("rules")]
    if not notes and item["kind"] == "finding" and msg.get("channel") == "teams" and msg.get("visibility") == "direct":
        rule = reader_rule(study, msg.get("from_addr"))
        if rule:
            p = study.person_by_email(msg["from_addr"])
            who = _who(study, [(p, rule)])
            notes = [{"who": who, "room": "a 1-on-1 chat with the bot", "rules": [rule["id"]],
                      "why": f"{who} told the bot about it in a 1-on-1 chat"}]
    if not notes:
        return []
    from . import drafting, routing

    by_id = {r["id"]: r for r in rules(study)}
    roles = list(dict.fromkeys(role for n in notes for rid in n["rules"] for role in by_id.get(rid, {}).get("report_to") or []))
    ids = [item["id"]] + [o["id"] for o in others]
    emailed: dict[str, list[int]] = {}
    for r in db.q(f"SELECT id, to_addrs, cc_addrs FROM outbox WHERE channel='email' AND item_id IN"
                  f" ({','.join('?' * len(ids))})", ids):
        for a in db.unjs(r["to_addrs"], []) + db.unjs(r["cc_addrs"], []):
            emailed.setdefault(a.lower(), []).append(r["id"])
    said = {"room": " and ".join(dict.fromkeys(n["room"] for n in notes)),
            "people": "; ".join(dict.fromkeys(n["who"] for n in notes)),
            "why": "; ".join(dict.fromkeys(n["why"] for n in notes))}
    sent, seen = [], set()
    for role in roles:
        for p in study.people_with_role(role, item.get("site")):
            if not p.email or p.email.lower() in seen:
                continue
            seen.add(p.email.lower())
            if p.email.lower() in emailed:
                db.audit("reported_privately", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                         to=p.email, role=role, already_emailed=emailed[p.email.lower()], why=said["why"])
                continue
            tpl = drafting.select("careful_report", "*", "email", "*")  # a role's own alert template would win
            subject, body = drafting.render(tpl, dict(context(p), careful=said))
            oid = routing.enqueue(
                purpose=f"careful_report_{role}", channel="email", destination="email", body=body, subject=subject,
                item_id=item["id"], template_id=tpl["id"], from_addr=routing.from_alias(study, msg), to=[p.email],
                meta={"thread_key": item["thread_key"], "item_id": item["id"], "item_ids": ids},
                status="queued" if item["send_draft"] else "held", study_id=study.study_id)
            db.audit("reported_privately", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     to=p.email, role=role, outbox_id=oid, why=said["why"])
            sent.append(oid)
    return sent

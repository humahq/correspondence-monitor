"""Processes one captured message end to end."""

from __future__ import annotations

import re

from dateutil import parser as dtparser

from . import (answers, asks, capture, checks, config, db, events, guide, items, matching, reader, routing, settings,
               textutil, triage)
from .config import Study
from . import drafting
from .drafting import item_link, local_time

SAFETY_TITLE = "Safety information received; not reviewed by this monitor (out of scope)"


def _own_text(body: str) -> str:
    """The writer's own words: quoted earlier replies cut off."""
    m = _WROTE.search(body)
    body = body[:m.start()] if m else body
    return "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))


_SITE_NUMBER = re.compile(r"\bsite\s*(?:#|no\.?|number)?\s*(\d{1,4})\b", re.I)


def _sites_named(study: Study, text: str) -> list[str]:
    """Sites the message names, by number ("Site 3", "site #03") or by place ("the Columbia site").
    A numbered site the study does not list is returned as written, e.g. "Site 03"."""
    found: list[tuple[int, str]] = []
    numbered = {}
    for s in study.sites:
        num = re.search(r"\d+", s["id"]) or re.search(r"\d+", s.get("name", ""))
        if num:
            numbered[int(num.group())] = s["id"]
        places = re.findall(r"\(([^)]+)\)", s.get("name", "")) + list(s.get("aliases", []))
        for place in places:
            m = re.search(rf"\b{re.escape(place.strip())}\b", text, re.I)
            if m:
                found.append((m.start(), s["id"]))
    for m in _SITE_NUMBER.finditer(text):
        n = int(m.group(1))
        found.append((m.start(), numbered.get(n) or f"Site {m.group(1)}"))
    return list(dict.fromkeys(site for _, site in sorted(found)))


def _site_for(study: Study, msg: dict) -> str | None:
    named = _sites_named(study, f"{msg.get('subject') or ''}\n{_own_text(msg.get('body_text') or '')}")
    if len(named) == 1:
        return named[0]
    p = study.person_by_email(msg.get("from_addr"))
    if p:
        sites = [s for s in p.sites if s != "*"]
        if sites:
            return sites[0]
    return study.sites[0]["id"] if len(study.sites) == 1 else None


def _corpus(msg: dict) -> str:
    parts = [msg.get("subject") or "", msg.get("body_text") or ""]
    parts += [a.get("text") or "" for a in msg.get("attachments", [])]
    return "\n\n".join(parts)


def _instant(iso: str) -> float:
    try:
        return dtparser.isoparse(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _pid(value: str | None) -> str | None:
    return value.strip().upper() if value and value.strip() else None


TIER_RANK = {"low": 0, "medium": 1, "immediate": 2}


def _same_words(a: str, b: str) -> bool:
    import difflib
    a, b = textutil.normalize(a or ""), textutil.normalize(b or "")
    if not a or not b:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def _one_alert_per_problem(study: Study, groups: dict[tuple, list[dict]]) -> dict[tuple, list[dict]]:
    """One problem found under two problem classes (riluzole stability under eligibility and under study drug)
    is one alert: the other class's rule is shown in it as a further rule the same words go against."""
    order = {p["id"]: i for i, p in enumerate(study.pattern_classes)}

    def rank(item):
        (pclass, _), group = item
        return (-max(TIER_RANK[c["tier"]] for c in group), not any(c["f"].method == "numeric" for c in group),
                order.get(pclass, 99))

    kept: dict[tuple, list[dict]] = {}
    for key, group in sorted(groups.items(), key=rank):
        home = next((k for k, g in kept.items() if k[1] == key[1] and key[1]
                     and any(_same_words(c["f"].message_quote, o["f"].message_quote) for c in group for o in g)),
                    None)
        if home:
            kept[home] += group
        else:
            kept[key] = list(group)
    return kept


def _drop_restatements(candidates: list[dict], msg_id: int) -> list[dict]:
    """A fact caught by a numeric rule is not reported again under judgment rules."""
    caught: dict[str | None, list[str]] = {}
    for c in candidates:
        if c["f"].method == "numeric" and c["f"].extra.get("fact_quote"):
            caught.setdefault(c["pid"], []).append(textutil.normalize(c["f"].extra["fact_quote"]))
    out = []
    for c in candidates:
        f = c["f"]
        if f.method == "judgment" and any(q in textutil.normalize(f.message_quote) for q in caught.get(c["pid"], [])):
            db.audit("finding_dropped_restatement", entity_type="message", entity_id=msg_id,
                     rule_id=f.rule["id"], note="same fact already caught by a numeric rule")
            continue
        out.append(c)
    return out


def _pooled_facts(event: dict, pid: str | None, all_events: list[dict]) -> list[dict]:
    """An event's facts plus the same participant's facts from other new events in the message."""
    facts = list(event["facts"])
    if pid:
        for other in all_events:
            if (other is event or other["temporality"] in ("historical", "hypothetical") or other["safety_only"]
                    or other.get("prevented")):
                continue
            if _pid(other.get("participant_id")) == pid:
                facts += other["facts"]
    seen, out = set(), []
    for f in facts:
        if f["parameter"] not in seen:
            seen.add(f["parameter"])
            out.append(f)
    return out


MEMORY_EVENTS = ("enrollment", "randomization")


def _earlier_facts(study: Study, pid: str, msg: dict) -> list[dict]:
    """The participant's facts from earlier messages (any thread), newest first, each marked with where it came
    from. Used only for enrollment and randomization, where eligibility depends on values reported before."""
    days = settings.get("matching.memory_days", 60)
    now = _instant(msg.get("sent_at") or "")
    out = []
    rows = db.q("SELECT e.data, m.id, m.sent_at, m.subject FROM events e JOIN messages m ON m.id=e.message_id "
                "WHERE m.study_id=? AND m.id<>? AND e.version=(SELECT MAX(version) FROM events WHERE message_id=m.id)",
                (study.study_id, msg["id"]))
    for r in sorted(rows, key=lambda r: _instant(r["sent_at"] or ""), reverse=True):
        when = _instant(r["sent_at"] or "")
        if not now or not when or when >= now or now - when > days * 86400:
            continue
        for e in db.unjs(r["data"], {}).get("events", []):
            if _pid(e.get("participant_id")) != pid or e["temporality"] == "hypothetical" or e.get("prevented"):
                continue
            out += [dict(f, earlier={"message_id": r["id"], "date": local_time(r["sent_at"]),
                                     "subject": r["subject"] or "(no subject)"}) for f in e["facts"]]
    return out


def _with_memory(study: Study, event: dict, pid: str | None, msg: dict, corpus: str) -> tuple[dict, str]:
    if event["event_type"] not in MEMORY_EVENTS or not pid:
        return event, corpus
    have = {f["parameter"] for f in event["facts"]}
    added = []
    for f in _earlier_facts(study, pid, msg):
        if f["parameter"] not in have:
            have.add(f["parameter"])
            added.append(f)
    if not added:
        return event, corpus
    notes_ = "\n".join(f"[earlier email, {f['earlier']['date']}, \"{f['earlier']['subject']}\"] {f['quote']}"
                       for f in added)
    return dict(event, facts=event["facts"] + added), f"{corpus}\n\nEARLIER EMAILS ABOUT {pid}:\n{notes_}"


def _already_raised(study: Study, f, pid: str | None, msg_id: int) -> bool:
    """A finding that rests on an earlier email's fact is not raised again if that participant already has an item
    of the same kind (in any thread)."""
    if not f.extra.get("earlier") or not pid:
        return False
    hit = db.q1("SELECT id FROM items WHERE study_id=? AND kind='finding' AND pattern_class=? AND participant_id=? "
                "AND first_message_id<>?", (study.study_id, f.rule["pattern_class"], pid, msg_id))
    if hit:
        db.audit("earlier_fact_already_raised", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                 item_id=hit["id"], rule_id=f.rule["id"], participant_id=pid)
    return hit is not None


def _base_evidence(study: Study, msg: dict, event: dict) -> dict:
    sender = study.person_by_email(msg.get("from_addr"))
    return {
        "message_id": msg["id"],
        "message_subject": msg.get("subject"),
        "message_date": msg.get("sent_at"),
        "sender_email": msg.get("from_addr"),
        "sender_name": sender.name if sender else (msg.get("from_name") or msg.get("from_addr")),
        "channel": msg["channel"],
        "visibility": msg["visibility"],
        "event": {k: event.get(k) for k in ("description", "event_type", "temporality",
                                             "temporality_evidence", "event_datetime", "source", "quote")},
    }


# ---------------------------------------------------------------- replies to the monitor

def _replied_to(msg: dict) -> dict | None:
    """The alert a message replies to: by email headers, or by the chat thread the alert started (Google Chat
    names a thread spaces/S/threads/T and the post that opened it spaces/S/messages/T.T)."""
    if msg.get("in_reply_to"):
        row = db.q1("SELECT item_id, meta FROM outbox WHERE rfc_message_id=? AND item_id IS NOT NULL",
                    (msg["in_reply_to"],))
        if row:
            return row
    thread = (msg.get("headers") or {}).get("_cormon_reply_to") or ""
    m = re.fullmatch(r"(spaces/[^/]+)/threads/([^/]+)", thread)
    if m:
        head = f"{m.group(1)}/messages/{m.group(2)}."
        rows = db.q("SELECT item_id, meta, external_ref FROM outbox WHERE channel='teams' AND item_id IS NOT NULL"
                    " AND external_ref LIKE ? ORDER BY id", (head + "%",))
        return next((r for r in rows if r["external_ref"].startswith(head)), None)
    return None


def _target_items(msg: dict, instruction: dict) -> list[int]:
    """The items a reply is about: all the problems of the alert it answers, or those it names ("cleared #14").
    A number named outside any alert's thread counts when it is an item of this study."""
    numbers = [int(n) for n in re.findall(r"#\s*(\d+)", instruction.get("text") or "")]
    row = _replied_to(msg)
    if row:
        sent = db.unjs(row["meta"], {}).get("item_ids") or [row["item_id"]]
        return [n for n in numbers if n in sent] if numbers else sent
    if numbers:
        return [n for n in numbers if db.q1("SELECT 1 FROM items WHERE id=? AND study_id=? AND kind != 'good_news'",
                                            (n, msg.get("study_id")))]
    one = _target_item(msg)
    return [one] if one is not None else []


def _answer_in_chat(study: Study, msg: dict, text: str, purpose: str = "instruction_answer") -> None:
    """A short reply in the chat thread a message came from, so the sender sees it was taken."""
    m = re.fullmatch(r"teams:((spaces/[^/]+)/threads/[^/]+)", msg.get("thread_key") or "")
    if msg.get("channel") != "teams" or not m:
        return
    routing.enqueue(purpose=purpose, channel="teams", destination="teams_study_channel", body=text,
                    message_id=msg["id"], study_id=study.study_id,
                    meta={"target": {"space": m.group(2), "thread": m.group(1), "key": "study_channel"}})


CAN_HELP = ("I couldn't find anything I can answer here. I answer questions about the study from its documents "
            "(visit windows, eligibility, medicines, samples, reporting) and check messages for problems.")


def _acknowledge(study: Study, msg: dict, summary: dict, trial_related: bool) -> None:
    """A reply when someone talks to the bot and nothing else answered them: the problems it raised, or that
    it found none. A 1-on-1 chat always gets one; a group chat only when the bot was asked."""
    raised = [it for it in (items.get(i) for i in summary["items_created"] + summary["items_repeat"])
              if it and it["kind"] == "finding" and it["state"] != "suppressed" and it["counts"]]
    new = [it for it in raised if it["id"] in summary["items_created"]]
    names = lambda its: ", ".join(f'<a href="{item_link(it)}">Item #{it["id"]}</a>' for it in its)
    if new and msg.get("visibility") == "direct":  # said only here: the problems themselves, not a pointer
        shown = [p for it in new for p in drafting.problems(study, it, it["evidence"])]
        lines = [f"Thanks. I found {len(shown)} problem{'s' if len(shown) > 1 else ''}:"]
        for n, p in enumerate(shown, 1):
            lines.append(f"<b>{n}. {p['title']}</b>" + (" <i>(please check: not sure)</i>" if p["unsure"] else ""))
            lines += [p["detail"]] if p["detail"] else []
            lines += [f"<b>What to do:</b> {p['next_step']}"] if p["next_step"] else []
        lines.append(f"I've sent {'them' if len(shown) > 1 else 'it'} to the CRA. Details: {names(new)}.")
        text = "<br>".join(lines)
    elif new:
        n = len(new)
        text = f"Thanks. I found {n} problem{'s' if n > 1 else ''} to check and raised {'them' if n > 1 else 'it'}: {names(new)}."
    elif raised:
        text = f"This is already raised: {names(raised)}."
    elif trial_related and (msg.get("visibility") != "direct" or answers.asks_something(msg.get("body_text"))):
        text = "I checked this against the protocol and found no problem."
    elif answers.asks_something(msg.get("body_text")):
        text = CAN_HELP
    else:
        text = "Thanks for letting me know."
    _answer_in_chat(study, msg, text, purpose="chat_acknowledgement")
    summary["acknowledged"] = text


def _target_item(msg: dict) -> int | None:
    if msg.get("in_reply_to"):
        row = db.q1("SELECT item_id FROM outbox WHERE rfc_message_id=? AND item_id IS NOT NULL",
                    (msg["in_reply_to"],))
        if row:
            return row["item_id"]
    m = re.search(r"Item #(\d+)", msg.get("subject") or "")
    if m:
        return int(m.group(1))
    reply_to = (msg.get("headers") or {}).get("_cormon_reply_to")
    if reply_to:
        root = reply_to.rsplit(":", 1)[-1]
        row = db.q1("SELECT item_id FROM outbox WHERE external_ref=? AND item_id IS NOT NULL", (root,))
        if row:
            return row["item_id"]
    if msg.get("thread_key"):
        # a reply on the site's own thread: only unambiguous when that thread raised a single open item
        rows = db.q("SELECT id FROM items WHERE thread_key=? AND study_id=? AND state='open' AND kind='finding'",
                    (msg["thread_key"], msg.get("study_id")))
        if len(rows) == 1:
            return rows[0]["id"]
    return None


def _only_a_question(event: dict, pid: str | None, events: list[dict]) -> bool:
    """A finding read from a question about the rules, about no participant, in a message reporting nothing done
    or planned: the question is answered, it is not a problem at the site."""
    return (event.get("status") == "asked" and not pid
            and all(e["temporality"] == "hypothetical" for e in events))


def _code_step(said: dict, ai_step: str | None) -> str | None:
    """Code's next step for a visit-window departure, keeping the duty table's "Also required: ..." from the AI's."""
    if not said.get("next_step"):
        return ai_step
    also = re.search(r"Also required: .*", ai_step or "")
    return said["next_step"] + (f" {also.group(0)}" if also else "")


def _shown_key(f) -> tuple[str, str]:
    """What a reader sees of a problem; without a proven protocol quote its headline stands in for it."""
    return (textutil.normalize(f.doc_quote or (f.extra or {}).get("headline") or ""),
            textutil.normalize(f.message_quote or ""))


def handle_instruction(study: Study, msg: dict, instruction: dict) -> bool:
    kind = instruction.get("type")
    if kind not in ("disregard", "cleared"):
        return False
    person = study.person_by_email(msg.get("from_addr"))
    item_ids = _target_items(msg, instruction)
    if not item_ids or person is None or not ({"cra", "clinops_lead", "tester"} & set(person.roles)):
        db.audit("instruction_not_applied", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                 instruction=instruction, item_id=item_ids[0] if item_ids else None,
                 reason="no item referenced" if not item_ids else "sender is not the CRA or sponsor lead")
        if person is not None and not item_ids:
            _answer_in_chat(study, msg, f'I could not tell which problem you mean. Reply "{kind}" in the alert\'s '
                                        f'thread, or name it, for example "{kind} #12".')
        return False
    actor_type = "cra" if "cra" in person.roles else "sponsor" if "clinops_lead" in person.roles else "tester"
    who = f"{person.name} <{person.email}>"
    elsewhere = [i for i in item_ids if items.get(i)["study_id"] != study.study_id]
    if elsewhere:
        db.audit("instruction_not_applied", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                 instruction=instruction, item_id=elsewhere[0], reason="item belongs to another study")
        _answer_in_chat(study, msg, f"I can't change {', '.join(f'#{i}' for i in elsewhere)}: "
                                    f"{'it is' if len(elsewhere) == 1 else 'they are'} not part of this study.")
        return False
    sid = study.study_id
    for item_id in item_ids:
        if kind == "disregard":
            items.override(item_id, "counts", 0, who, actor_type, reason=instruction.get("text"), force=True,
                           study_id=sid)
            items.set_state(item_id, "suppressed", who, actor_type, reason=f"reply: {instruction.get('text')}",
                            study_id=sid)
        else:
            items.set_state(item_id, "cleared", who, actor_type, reason=f"reply: {instruction.get('text')}",
                            study_id=sid)
    done = "cleared" if kind == "cleared" else "disregarded (it will not count)"
    _answer_in_chat(study, msg, f"Done: {', '.join(f'#{i}' for i in item_ids)} {done}, by {person.name}.")
    return True


# ---------------------------------------------------------------- dropped-and-re-added threads

_WROTE = re.compile(r"(?im)^[ \t]*>*[ \t]*On (?P<date>[^<\n]+?)[ \t]*<(?P<email>[^>\s]+@[^>\s]+)>[ \t]*(?:\r?\n[ \t>]*)?wrote:")


def first_writer_while_absent(msg: dict) -> str | None:
    prior = db.q1("SELECT MAX(sent_at) t FROM messages WHERE thread_key=? AND id<?", (msg["thread_key"], msg["id"]))
    last_seen = prior["t"] if prior else None
    bot_local = (config.settings.get("mailbox.address") or "bot@").split("@")[0]
    candidates = []
    for m in _WROTE.finditer(msg.get("body_text") or ""):
        addr = m.group("email").lower()
        if addr.split("@")[0].split("+")[0] == bot_local:
            continue
        try:
            when = dtparser.parse(m.group("date").replace(" at ", " "), fuzzy=True)
        except (ValueError, OverflowError):
            continue
        candidates.append((when, addr))
    if last_seen:
        ls = dtparser.isoparse(last_seen)
        after = []
        for when, addr in candidates:
            w = when if when.tzinfo else when.replace(tzinfo=ls.tzinfo)
            if w > ls:
                after.append((w, addr))
        candidates = after
    if candidates:
        return sorted(candidates, key=lambda c: c[0])[0][1]
    gap = (msg.get("headers") or {}).get("_cormon_gap_refs") or []
    to = db.unjs(msg.get("to_addrs")) if isinstance(msg.get("to_addrs"), str) else msg.get("to_addrs")
    if msg.get("in_reply_to") in gap and to:
        return to[0]  # a reply is addressed to the author of the message it answers, which we missed
    return msg.get("from_addr")


# ---------------------------------------------------------------- main entry

def process(msg_id: int, *, use_cache: bool = True, rerun: bool = False, actor: str = "system") -> dict:
    msg = capture.message(msg_id)
    summary: dict = {"message_id": msg_id, "route": msg["route"], "items_created": [], "items_repeat": [],
                     "historical": 0, "hypothetical": 0, "prevented": 0, "out_of_scope": 0, "findings": 0}
    db.ex("UPDATE messages SET status='processing', error=NULL WHERE id=?", (msg_id,))
    try:
        if msg["route"] != "study" or not msg["study_id"]:
            db.audit("general_message", entity_type="message", entity_id=msg_id,
                     note="no study tag in the address; handled as a general bot message, not matched")
            db.ex("UPDATE messages SET status='processed' WHERE id=?", (msg_id,))
            return summary

        study = config.study(msg["study_id"])
        history = capture.thread_history(msg["thread_key"], msg_id)
        data = events.extract(study, msg, history, use_cache=use_cache)
        event_id = events.store(msg_id, data, source="operator-rerun" if rerun else "system")
        db.audit("event_extracted", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                 actor=actor, actor_type="operator" if rerun else "system", event_record=event_id,
                 trial_related=data["trial_related"], events=len(data["events"]),
                 instruction=data["instruction_to_monitor"], summary=data["summary"])

        for a in data.get("attachments", []):
            atts = msg.get("attachments", [])
            if 0 <= a["attachment_index"] < len(atts):
                att = atts[a["attachment_index"]]
                db.ex("UPDATE attachments SET placement=?, classification=? WHERE id=?",
                      (a["placement"], db.js(a), att["id"]))
                if a["placement"] == "unplaced":
                    db.audit("attachment_unplaced", entity_type="message", entity_id=msg_id,
                             study_id=study.study_id, attachment=att["filename"], what=a["what_it_is"])

        told = data["instruction_to_monitor"]
        if handle_instruction(study, msg, told) or (told or {}).get("type") in ("disregard", "cleared"):
            summary["instruction"] = told["type"]
            if not data["events"]:  # a bare "cleared #19" is an answer to us, not site news to check
                db.ex("UPDATE messages SET status='processed' WHERE id=?", (msg_id,))
                return summary

        if not data["trial_related"]:
            db.audit("not_trial_related", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                     summary=data["summary"])

        site = _site_for(study, msg)
        corpus = _corpus(msg)
        seen_item_ids: set[int] = set()
        problem_pids: set[str] = set()
        good_news: list[tuple[dict, str | None, dict]] = []
        candidates: list[dict] = []
        anchors: list[dict] = []   # events a finding can be tied to (not old news, not hypothetical)

        def add_candidate(f, event: dict, pid: str | None, base: dict) -> None:
            hours = triage.hours_until(event.get("event_datetime"), msg.get("sent_at"))
            facts = {"polarity": "problem", "pattern_class": f.rule["pattern_class"],
                     "temporality": event["temporality"], "hours_until": hours, "check_type": f.method}
            tier, reason = triage.assign(study.study_id, facts)
            if f.certainty == "unsure" and tier == "immediate":
                tier, reason = "medium", f"{reason}; held at medium because the document check was not sure"
            candidates.append({"f": f, "pid": pid, "event": event, "base": base, "hours": hours,
                               "facts": facts, "tier": tier, "reason": reason})

        part_dates = [r["source_date"] for r in db.q(
            "SELECT source_date FROM attachments WHERE message_id=? AND source_date IS NOT NULL", (msg_id,))]
        newest_part_date = max(part_dates, key=_instant) if part_dates else None

        reader_route = guide.checker(study).get("route") == "reader"
        safety_waiting: list[tuple[dict, str | None, dict]] = []

        def out_of_scope(why: str, pid: str | None, event: dict, base: dict) -> None:
            summary["out_of_scope"] += 1
            item_id, created = items.upsert(
                study=study, thread_key=msg["thread_key"], message_id=msg_id,
                dedup_key=f"{study.study_id}|{msg['thread_key']}|safety|{pid or '-'}",
                kind="out_of_scope", pattern_class=None, rule_id=None, participant_id=pid, site=site,
                polarity="neutral", title=SAFETY_TITLE, tier="low",
                tier_reason="safety signals are out of scope (Section 4.6); routed nowhere",
                evidence=dict(base, message_quote=event["quote"]), reasoning=why, state="out_of_scope")
            (summary["items_created"] if created else summary["items_repeat"]).append(item_id)
            if created:
                db.audit("safety_out_of_scope", entity_type="item", entity_id=item_id, study_id=study.study_id,
                         why=why, note="recorded only; sender told to contact the medical monitor")
                routing.route_item(item_id)

        for event in data["events"]:
            if event.get("harm", "none") == "none":
                event["safety_signal"] = event["safety_only"] = False
            pid = _pid(event.get("participant_id"))
            base = _base_evidence(study, msg, event)

            if event["safety_only"]:
                if event["temporality"] in ("current", "planned"):
                    anchors.append(dict(event, facts=_pooled_facts(event, pid, data["events"])))
                out_of_scope("event is only about participant safety", pid, event, base)
                continue

            stale_days = settings.get("matching.forwarded_stale_days", 7)
            until = triage.hours_until(event.get("event_datetime") or newest_part_date, msg.get("sent_at"))
            age = -until if until is not None else None
            if (event.get("source") in ("forwarded_message", "attachment") and event["temporality"] != "historical"
                    and age is not None and age > stale_days * 24):
                event = dict(event, temporality="historical", temporality_evidence=(
                    f"forwarded or attached content dated {event.get('event_datetime') or newest_part_date}, "
                    f"more than {stale_days} days "
                    f"before this message ({event.get('temporality_evidence') or ''})"))

            if event["temporality"] == "historical":
                summary["historical"] += 1
                db.audit("historical_reference_no_alert", entity_type="message", entity_id=msg_id,
                         study_id=study.study_id, participant_id=pid, description=event["description"],
                         evidence=event["temporality_evidence"])
                continue
            if event["temporality"] == "hypothetical":
                summary["hypothetical"] += 1
                db.audit("hypothetical_question_no_alert", entity_type="message", entity_id=msg_id,
                         study_id=study.study_id, participant_id=pid, description=event["description"],
                         evidence=event["temporality_evidence"])
                continue
            if event.get("prevented") and not event["safety_signal"]:
                summary["prevented"] += 1
                db.audit("prevented_checked_for_the_stopping_only", entity_type="message", entity_id=msg_id,
                         study_id=study.study_id, participant_id=pid, description=event["description"],
                         quote=event["quote"])

            if reader_route:
                if event["safety_signal"]:
                    safety_waiting.append((event, pid, base))
                continue
            pooled = dict(event, facts=_pooled_facts(event, pid, data["events"]))
            pooled, event_corpus = _with_memory(study, pooled, pid, msg, corpus)
            anchors.append(pooled)
            findings = matching.match_event(study, pooled, msg, event_corpus, use_cache=use_cache)
            findings = [f for f in findings if not _already_raised(study, f, pid, msg_id)]
            if event["safety_signal"] and not findings:
                out_of_scope("safety signal that matches no study requirement, so its only reading is safety",
                             pid, event, base)
                continue
            for f in findings:
                add_candidate(f, event, pid, base)
            if not findings and event["polarity"] == "good_news" and event["temporality"] == "current":
                good_news.append((event, pid, base))

        if reader_route:
            found, trace = reader.check(study, msg, corpus, use_cache=use_cache)
            db.audit("reader_checked", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                     trace=trace)
            hit = set()
            for f in found:
                pid = _pid(f.event.get("participant_id"))
                if _only_a_question(f.event, pid, data["events"]):
                    summary["hypothetical"] += 1   # a question about the rules is answered, not raised
                    db.audit("general_question_no_alert", entity_type="message", entity_id=msg_id,
                             study_id=study.study_id, problem=f.extra.get("headline"))
                    continue
                if _already_raised(study, f, pid, msg_id):
                    continue
                hit.add(pid)
                add_candidate(f, f.event, pid, _base_evidence(study, msg, f.event))
            for event, pid, base in safety_waiting:
                if pid not in hit:
                    out_of_scope("safety signal that matches no study requirement, so its only reading is safety",
                                 pid, event, base)

        if not reader_route and settings.get("matching.sweep", True):
            enrolled = {_pid(e.get("participant_id")) for e in data["events"]
                        if e["event_type"] in ("enrollment", "randomization") and e.get("participant_id")}
            for f in matching.sweep_findings(study, msg, corpus, anchors, [c["f"] for c in candidates],
                                             enrolled=enrolled, use_cache=use_cache):
                pid = _pid(f.event.get("participant_id"))
                add_candidate(f, f.event, pid, _base_evidence(study, msg, f.event))
                db.audit("sweep_finding_added", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                         rule_id=f.rule["id"], participant_id=pid, reasoning=f.reasoning)

        candidates = _drop_restatements(candidates, msg_id)
        summary["findings"] = len(candidates)
        groups: dict[tuple, list[dict]] = {}
        for c in candidates:
            key = (c["f"].rule["pattern_class"], c["pid"])
            if reader_route:   # the reader merges repeats itself: each of its findings is its own alert
                key += (c["f"].extra.get("problem_key", ""),)
            groups.setdefault(key, []).append(c)
            if c["pid"]:
                problem_pids.add(c["pid"])
        if not reader_route:
            groups = _one_alert_per_problem(study, groups)

        bundle: list[int] = []  # this message's new non-urgent problems: one email and one post for all of them
        for (pclass, pid, *problem), group in groups.items():
            group.sort(key=lambda c: (c["f"].rule["pattern_class"] != pclass, c["f"].certainty == "unsure",
                                      -TIER_RANK[c["tier"]], c["f"].method != "numeric"))
            lead = group[0]
            f = lead["f"]
            shown = {_shown_key(f)}
            others = []
            for c in group[1:]:
                key = _shown_key(c["f"])
                if key not in shown:  # two rules can rest on the same sentence and email words; show them once
                    shown.add(key)
                    others.append(c)
            said = {id(c["f"]): checks.explain(c["f"].extra.get("numbers")) for c in group}
            headline = lambda x: said[id(x)].get("headline") or x.extra.get("headline")
            step = lambda x: _code_step(said[id(x)], x.extra.get("next_step"))
            evidence = dict(
                lead["base"], message_quote=f.message_quote, rule_id=f.rule["id"], rule_text=f.rule["text"],
                doc_id=f.rule["doc_id"], doc_title=study.doc(f.rule["doc_id"])["title"],
                doc_quote=f.doc_quote, doc_page=f.doc_page, doc_passage=f.doc_passage,
                method=f.method, observed=f.observed, certainty=f.certainty,
                confirmation=f.confirmation, unsure_reason=f.extra.get("unsure_reason"),
                earlier=f.extra.get("earlier"), next_step=step(f), headline=headline(f),
                numbers=f.extra.get("numbers"), detail=said[id(f)].get("detail"),
                why=f.extra.get("why"), verdict=f.extra.get("verdict"), kept_by_code=f.extra.get("kept_by_code"),
                code_fact=f.extra.get("code_fact"), date_notes=f.extra.get("date_notes"),
                hours_until=lead["hours"], triage_facts=lead["facts"],
                additional=[{"rule_id": c["f"].rule["id"], "rule_text": c["f"].rule["text"],
                             "doc_id": c["f"].rule["doc_id"], "doc_title": study.doc(c["f"].rule["doc_id"])["title"],
                             "doc_quote": c["f"].doc_quote, "doc_page": c["f"].doc_page,
                             "message_quote": c["f"].message_quote, "reasoning": c["f"].reasoning,
                             "confirmation": c["f"].confirmation, "headline": headline(c["f"]),
                             "next_step": step(c["f"]), "certainty": c["f"].certainty,
                             "detail": said[id(c["f"])].get("detail"),
                             "unsure_reason": c["f"].extra.get("unsure_reason")} for c in others])
            pattern = study.pattern(pclass) or {}
            title = headline(f) or f"{pattern.get('label', 'Possible issue')}: {f.rule['text']}"
            if f.certainty == "unsure":
                title = f"Please check (not sure): {title}"
            item_id, created = items.upsert(
                study=study, thread_key=msg["thread_key"], message_id=msg_id,
                dedup_key=f"{study.study_id}|{msg['thread_key']}|{pclass}|{pid or '-'}"
                          + (f"|{problem[0]}" if problem and problem[0] else ""),
                kind="finding", pattern_class=pclass, rule_id=f.rule["id"],
                participant_id=pid, site=site, polarity="problem", title=title, tier=lead["tier"],
                tier_reason=lead["reason"], evidence=evidence,
                reasoning=" ".join(dict.fromkeys(c["f"].reasoning for c in group)))
            seen_item_ids.add(item_id)
            if created:
                summary["items_created"].append(item_id)
                if lead["tier"] == "immediate":
                    routing.route_item(item_id)  # never held back to share a message with smaller problems
                else:
                    bundle.append(item_id)
                continue
            summary["items_repeat"].append(item_id)
            existing = items.get(item_id)
            if rerun:
                items.system_update(item_id, tier=lead["tier"], title=title)
            elif TIER_RANK[lead["tier"]] > TIER_RANK[existing["tier"]]:
                res = items.system_update(item_id, tier=lead["tier"])
                if res["applied"]:
                    db.audit("item_escalated", entity_type="item", entity_id=item_id, study_id=study.study_id,
                             old=existing["tier"], new=lead["tier"], message_id=msg_id, reason=lead["reason"])
                    routing.route_item(item_id, force=True)
        if bundle:
            routing.route_items(bundle)

        for event, pid, base in good_news:
            if pid in problem_pids:
                continue
            item_id, created = items.upsert(
                study=study, thread_key=msg["thread_key"], message_id=msg_id,
                dedup_key=f"{study.study_id}|{msg['thread_key']}|good|{event['event_type']}|{pid or '-'}",
                kind="good_news", pattern_class=None, rule_id=None, participant_id=pid, site=site,
                polarity="good_news", title=event["description"], tier="low",
                tier_reason="good news", evidence=dict(base, message_quote=event["quote"]), reasoning=None)
            if created:
                summary["items_created"].append(item_id)
                routing.route_item(item_id)
            else:
                summary["items_repeat"].append(item_id)

        turn = capture.turn(msg)
        summary["turn"] = turn
        text = f"{msg.get('subject') or ''}\n\n{msg.get('body_text') or ''}"
        declined = None
        to_bot = turn in ("direct", "addressed") or (turn == "email" and msg.get("visibility") == "to")
        for_people = False  # an email the bot is only copied on: its opinion or task requests are for the people
        if reader_route and not rerun and asks.enabled() and (to_bot or answers.asks_something(text)):
            try:
                if to_bot:
                    declined = asks.decline(study, msg, text, use_cache=use_cache)
                    if declined:
                        summary["declined"] = declined["kinds"]
                elif turn == "email":
                    kinds = [r["kind"] for r in asks.sort(text, use_cache)]
                    for_people = bool(kinds) and "study" not in kinds
                    if for_people:
                        db.audit("question_left_to_people", entity_type="message", entity_id=msg_id,
                                 study_id=study.study_id, kinds=kinds)
            except Exception as e:
                db.audit("request_sort_failed", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                         error=str(e)[:500])
        if reader_route and not rerun:
            try:  # an answer that fails must not stop the problems from going out
                said = answers.reply(study, msg, text, use_cache=use_cache,
                                     harm_only=turn == "quiet" or for_people or bool(declined and declined["only"]))
                if said:
                    summary["answered"] = len(said["answers"])
            except Exception as e:
                summary["answer_failed"] = True
                db.audit("question_answer_failed", entity_type="message", entity_id=msg_id, study_id=study.study_id,
                         error=str(e)[:500])
        if (turn in ("direct", "addressed") and not rerun and not summary.get("answered")
                and not summary.get("answer_failed") and not summary.get("instruction")
                and not (declined and declined["only"] and not summary["items_created"])):
            _acknowledge(study, msg, summary, data["trial_related"])

        if rerun:
            previous = db.q("SELECT id FROM items WHERE first_message_id=? AND kind='finding'", (msg_id,))
            for row in previous:
                if row["id"] not in seen_item_ids:
                    db.audit("rerun_finding_not_reproduced", entity_type="item", entity_id=row["id"],
                             study_id=study.study_id, actor=actor, actor_type="operator",
                             note="re-run did not reproduce this finding; item left unchanged for operator review")
                    summary.setdefault("not_reproduced", []).append(row["id"])

        gap = (msg.get("headers") or {}).get("_cormon_gap_refs")
        state_key = f"nudged:{msg['thread_key']}"
        if gap and not rerun and not db.get_state(state_key):
            writer = first_writer_while_absent(msg)
            oid = routing.copy_nudge(study, msg, writer) if writer else None
            db.set_state(state_key, str(msg_id))
            db.audit("thread_reread_after_absence", entity_type="message", entity_id=msg_id,
                     study_id=study.study_id, missed_messages=len(gap), nudged=writer, outbox_id=oid)
            summary["nudged"] = writer

        db.ex("UPDATE messages SET status='processed' WHERE id=?", (msg_id,))
        return summary
    except Exception as e:
        db.ex("UPDATE messages SET status='error', error=? WHERE id=?", (str(e)[:1000], msg_id))
        db.audit("processing_error", entity_type="message", entity_id=msg_id, error=str(e)[:1000])
        raise


def process_pending(log=print) -> list[dict]:
    results = []
    for row in db.q("SELECT id FROM messages WHERE status='new' ORDER BY id"):
        if db.get_state("paused") == "1":
            break
        try:
            results.append(process(row["id"]))
        except Exception as e:
            log(f"message {row['id']} failed: {e}")
    return results

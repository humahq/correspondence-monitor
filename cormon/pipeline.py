"""Processes one captured message end to end."""

from __future__ import annotations

import re

from dateutil import parser as dtparser

from . import capture, config, db, events, items, matching, routing, settings, textutil, triage
from .config import Study

SAFETY_TITLE = "Safety information received; not reviewed by this monitor (out of scope)"


def _site_for(study: Study, msg: dict) -> str | None:
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


def handle_instruction(study: Study, msg: dict, instruction: dict) -> bool:
    kind = instruction.get("type")
    if kind not in ("disregard", "cleared"):
        return False
    person = study.person_by_email(msg.get("from_addr"))
    item_id = _target_item(msg)
    if item_id is None or person is None or not ({"cra", "clinops_lead"} & set(person.roles)):
        db.audit("instruction_not_applied", entity_type="message", entity_id=msg["id"], study_id=study.study_id,
                 instruction=instruction, item_id=item_id,
                 reason="no item referenced" if item_id is None else "sender is not the CRA or sponsor lead")
        return False
    actor_type = "cra" if "cra" in person.roles else "sponsor"
    who = f"{person.name} <{person.email}>"
    if kind == "disregard":
        items.override(item_id, "counts", 0, who, actor_type, reason=instruction.get("text"), force=True)
        items.set_state(item_id, "suppressed", who, actor_type, reason=f"reply: {instruction.get('text')}")
    else:
        items.set_state(item_id, "cleared", who, actor_type, reason=f"reply: {instruction.get('text')}")
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

        if handle_instruction(study, msg, data["instruction_to_monitor"]):
            summary["instruction"] = data["instruction_to_monitor"]["type"]
            if not data["events"]:
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

        part_dates = [r["source_date"] for r in db.q(
            "SELECT source_date FROM attachments WHERE message_id=? AND source_date IS NOT NULL", (msg_id,))]
        newest_part_date = max(part_dates, key=_instant) if part_dates else None

        for event in data["events"]:
            if event.get("harm", "none") == "none":
                event["safety_signal"] = event["safety_only"] = False
            pid = _pid(event.get("participant_id"))
            base = _base_evidence(study, msg, event)

            def out_of_scope(why: str) -> None:
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

            if event["safety_only"]:
                out_of_scope("event is only about participant safety")
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

            pooled = dict(event, facts=_pooled_facts(event, pid, data["events"]))
            findings = matching.match_event(study, pooled, msg, corpus, use_cache=use_cache)
            if event["safety_signal"] and not findings:
                out_of_scope("safety signal that matches no study requirement, so its only reading is safety")
                continue
            hours = triage.hours_until(event.get("event_datetime"), msg.get("sent_at"))
            for f in findings:
                facts = {"polarity": "problem", "pattern_class": f.rule["pattern_class"],
                         "temporality": event["temporality"], "hours_until": hours, "check_type": f.method}
                tier, reason = triage.assign(study.study_id, facts)
                candidates.append({"f": f, "pid": pid, "event": event, "base": base, "hours": hours,
                                   "facts": facts, "tier": tier, "reason": reason})
            if not findings and event["polarity"] == "good_news" and event["temporality"] == "current":
                good_news.append((event, pid, base))

        candidates = _drop_restatements(candidates, msg_id)
        summary["findings"] = len(candidates)
        groups: dict[tuple, list[dict]] = {}
        for c in candidates:
            groups.setdefault((c["f"].rule["pattern_class"], c["pid"]), []).append(c)
            if c["pid"]:
                problem_pids.add(c["pid"])

        for (pclass, pid), group in groups.items():
            group.sort(key=lambda c: (-TIER_RANK[c["tier"]], c["f"].method != "numeric"))
            lead = group[0]
            f = lead["f"]
            shown = {textutil.normalize(f.doc_quote or "")}
            others = []
            for c in group[1:]:
                key = textutil.normalize(c["f"].doc_quote or "")
                if key not in shown:  # two rules can rest on the same sentence; quote it once
                    shown.add(key)
                    others.append(c)
            evidence = dict(
                lead["base"], message_quote=f.message_quote, rule_id=f.rule["id"], rule_text=f.rule["text"],
                doc_id=f.rule["doc_id"], doc_title=study.doc(f.rule["doc_id"])["title"],
                doc_quote=f.doc_quote, doc_page=f.doc_page, doc_passage=f.doc_passage,
                method=f.method, observed=f.observed, certainty=f.certainty,
                confirmation=f.confirmation, hours_until=lead["hours"], triage_facts=lead["facts"],
                additional=[{"rule_id": c["f"].rule["id"], "rule_text": c["f"].rule["text"],
                             "doc_id": c["f"].rule["doc_id"], "doc_title": study.doc(c["f"].rule["doc_id"])["title"],
                             "doc_quote": c["f"].doc_quote, "doc_page": c["f"].doc_page,
                             "message_quote": c["f"].message_quote, "reasoning": c["f"].reasoning,
                             "confirmation": c["f"].confirmation} for c in others])
            pattern = study.pattern(pclass) or {}
            title = f.extra.get("headline") or f"{pattern.get('label', 'Possible issue')}: {f.rule['text']}"
            item_id, created = items.upsert(
                study=study, thread_key=msg["thread_key"], message_id=msg_id,
                dedup_key=f"{study.study_id}|{msg['thread_key']}|{pclass}|{pid or '-'}",
                kind="finding", pattern_class=pclass, rule_id=f.rule["id"],
                participant_id=pid, site=site, polarity="problem", title=title, tier=lead["tier"],
                tier_reason=lead["reason"], evidence=evidence,
                reasoning=" ".join(dict.fromkeys(c["f"].reasoning for c in group)))
            seen_item_ids.add(item_id)
            if created:
                summary["items_created"].append(item_id)
                routing.route_item(item_id)
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

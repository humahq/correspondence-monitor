"""Doctors in the chat (cormon/careful.py) with the real AI. Nothing is sent: everything runs on a copy of the database.

Run: CORMON_HOME=practice7 CORMON_MODEL=gpt-5.4-mini .venv/bin/python tests/scenarios/doctor/run.py [out_dir]

Each case puts people in a chat room by role (pi = Eliot Levitt, sub_i = a sub-investigator added for the run,
crc, cra, lead) and checks what the bot does:
- alert: a problem raised from the message; expect "withhold" (kept out of the chat, sent privately) or "send".
- reply: one bot reply; expect "send" or "reword" (worded calmly before it is posted).
- answer: the bot answers the doctor's question; an AI judge checks it is calm and still answers ("judge").
- message: the whole pipeline on the message; expect the group post (posted, not_posted or none), the reply to the
  writer (calm: the calm line or a calm answer; none) and whether the study team was told privately.
no_ai: the case must not ask the AI anything for the careful check.
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

from cormon import answers, careful, config, db, items, llm, pipeline, rooms, routing, settings

HERE = pathlib.Path(__file__).parent
out_dir = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/doctor-set")
out_dir.mkdir(parents=True, exist_ok=True)
cases = json.loads((HERE / (sys.argv[2] if len(sys.argv) > 2 else "cases.json")).read_text())

work = pathlib.Path(tempfile.mkdtemp(prefix="doctor-"))
src, dst = sqlite3.connect(settings.data_dir() / "cormon.db"), sqlite3.connect(work / "cormon.db")
src.backup(dst)
src.close(), dst.close()
settings.data_dir = lambda: work   # every write goes to the copy

SID = "NCT05842941"
study = config.study(SID)
study.people.append(config.Person(id="chen-subi", name="Maria Chen", email="maria.chen@site.org",
                                  roles=["sub_investigator"], sites=["site-01"]))
config.study = lambda sid: study
config.studies = lambda: {SID: study}
EMAIL = {"pi": "eliot@huma.ai", "sub_i": "maria.chen@site.org", "crc": "johnrohit94@gmail.com",
         "cra": "johnernest978@gmail.com", "lead": "john@huma.ai"}
ROOM: list[str] = []


class Room:
    def members(self, space):
        return [EMAIL[r] for r in ROOM]


rooms.teams_channel = lambda: Room()
CALLS: list[str] = []
_real = llm.structured
llm.structured = lambda task, *a, **k: CALLS.append(task) or _real(task, *a, **k)

JUDGE = """A clinical-trial monitoring bot replied in chat to a doctor on the study (the principal investigator).
Doctors must never feel criticised by it. Does the reply do what EXPECTED says? ok: true only if it does and it
nowhere says or implies that the doctor did something wrong, broke a rule or made a mistake."""
JUDGE_SCHEMA = llm.obj({"ok": {"type": "boolean"}, "why": {"type": "string"}})


def message(c, n):
    vis = c.get("visibility", "group")
    space, thread = f"spaces/doctor-{c['id']}", f"t{n}"
    headers = {"_cormon_to_bot": True, "_cormon_mentions": ["bot@huma.ai"]} if c.get("addressed") else {}
    mid = db.ex("INSERT INTO messages (channel, external_id, thread_key, study_id, route, visibility, from_addr,"
                " from_name, body_text, headers, sent_at, received_at, status) VALUES"
                " ('teams',?,?,?,'study',?,?,?,?,?,?,?,'new')",
                (f"{space}/messages/{thread}", f"teams:{space}/threads/{thread}", SID, vis, EMAIL[c["from"]],
                 study.person_by_email(EMAIL[c["from"]]).name, c["message"], db.js(headers), db.now(), db.now()))
    return mid, space, f"{space}/threads/{thread}"


def last_check(entity_type, entity_id):
    row = db.q1("SELECT details FROM audit_log WHERE action='careful_check' AND entity_type=? AND entity_id=?"
                " ORDER BY id DESC", (entity_type, str(entity_id)))
    return db.unjs(row["details"]) if row else {}


def judge(expected, reply):
    return llm.structured("doctor_judge", JUDGE, f"EXPECTED: {expected}\n\nREPLY:\n{reply}", JUDGE_SCHEMA, "verdict",
                          use_cache=False)


def run(c, n):
    ROOM[:] = c["room"]
    rooms.forget()
    CALLS.clear()
    mid, space, thread = message(c, n)
    msg = pipeline.capture.message(mid)
    why, got = [], {}
    if c["kind"] == "alert":
        ev = {"message_quote": c["message"], "doc_quote": "", "doc_title": "Appendix G", "rule_text": "6",
              "next_step": "Record a protocol deviation.", "certainty": "clear", "sender_email": msg["from_addr"],
              "sender_name": msg["from_name"], "message_date": msg["sent_at"], "channel": "teams",
              "visibility": msg["visibility"]}
        item_id, _ = items.upsert(study=study, thread_key=msg["thread_key"], message_id=mid, dedup_key=f"doctor{mid}",
                                  kind="finding", pattern_class="visit_schedule", rule_id=None,
                                  participant_id=None, site="site-01", polarity="problem", title=c["title"],
                                  tier="low", tier_reason="scenario", evidence=ev, reasoning="")
        routing.route_item(item_id)
        post = db.q1("SELECT status FROM outbox WHERE item_id=? AND channel='teams'", (item_id,))
        got = {"decision": "withhold" if post and post["status"] == "withheld" else "send",
               "check": last_check("item", item_id),
               "private": [r["purpose"] for r in db.q("SELECT purpose FROM outbox WHERE item_id=? AND channel='email'",
                                                      (item_id,))]}
        if got["decision"] != c["expect"]:
            why.append(f"expected {c['expect']}, got {got['decision']}: {got['check'].get('reason')}")
    elif c["kind"] == "reply":
        oid = routing.enqueue(purpose="question_answer", channel="teams", destination="teams_study_channel",
                              body=c["post"], message_id=mid, study_id=SID,
                              meta={"target": {"space": space, "thread": thread, "key": "study_channel"}})
        body = db.q1("SELECT body FROM outbox WHERE id=?", (oid,))["body"]
        got = {"decision": last_check("message", mid).get("decision", "send"), "posted": body,
               "check": last_check("message", mid)}
        if got["decision"] != c["expect"]:
            why.append(f"expected {c['expect']}, got {got['decision']}: {got['check'].get('reason')}")
    elif c["kind"] == "answer":
        said = answers.reply(study, msg, c["message"], use_cache=False)
        posted = db.q1("SELECT body FROM outbox WHERE message_id=? AND channel='teams' ORDER BY id DESC", (mid,))
        got = {"posted": posted["body"] if posted else None, "tone": [a.get("tone") for a in (said or {}).get("answers", [])]}
        if not posted:
            why.append("no answer was posted")
        else:
            if "calm" not in got["tone"]:
                why.append("the answer was not worded with the calm tone")
            v = judge(c["judge"], got["posted"])
            if not v["ok"]:
                why.append(f"judge: {v['why']}")
    else:
        pipeline.process(mid, use_cache=False)
        posts = db.q("SELECT o.status FROM outbox o JOIN items i ON i.id=o.item_id WHERE i.first_message_id=?"
                     " AND o.channel='teams'", (mid,))
        replies = [r["body"] for r in db.q("SELECT body FROM outbox WHERE message_id=? AND channel='teams'", (mid,))]
        mails = db.q("SELECT o.purpose FROM outbox o JOIN items i ON i.id=o.item_id WHERE i.first_message_id=?"
                     " AND o.channel='email'", (mid,))
        private = db.q("SELECT a.details FROM audit_log a JOIN items i ON a.entity_id=CAST(i.id AS TEXT) WHERE"
                       " a.entity_type='item' AND a.action='reported_privately' AND i.first_message_id=?", (mid,))
        group = ("none" if not posts else "posted" if any(p["status"] == "queued" for p in posts) else "not_posted")
        got = {"group_post": group, "replies": replies, "emails": [m["purpose"] for m in mails],
               "private": [db.unjs(p["details"]).get("to") for p in private],
               "items": [dict(r) for r in db.q("SELECT id, title, tier FROM items WHERE first_message_id=?", (mid,))]}
        want = c["expect"]
        if group != want["group_post"]:
            why.append(f"group post: expected {want['group_post']}, got {group}")
        if want["private"] != bool(private):
            why.append(f"told privately: expected {want['private']}, got {got['private']}")
        if want["reply"] == "none" and replies:
            why.append(f"expected no reply, got {replies}")
        if want["reply"] == "calm":
            if not replies:
                why.append("expected a calm reply, got none")
            elif replies[-1] != careful.calm_reply(careful.reader_rule(study, msg["from_addr"]) or {}):
                v = judge("Thanks the doctor and does not tell them they broke a rule.", replies[-1])
                if not v["ok"]:
                    why.append(f"judge: {v['why']}")
    if c.get("no_ai") and any(t.startswith("careful") for t in CALLS):
        why.append(f"asked the AI although it should not: {CALLS}")
    return why, got


passed, rows = 0, []
for n, c in enumerate(cases):
    try:
        why, got = run(c, n)
    except Exception as e:
        why, got = [f"error: {e}"], {}
    ok = not why
    passed += ok
    rows.append({"id": c["id"], "kind": c["kind"], "ok": ok, "why": why, "got": got, "ai_tasks": list(CALLS)})
    print(f"{'PASS' if ok else 'FAIL'} {c['id']} ({c['kind']}): {c['message']!r}"
          + (f"\n   posted: {got.get('posted')!r}" if got.get("posted") else "")
          + (f"\n   replies: {got.get('replies')}" if got.get("replies") else "")
          + (f"\n   WHY: {why}" if why else ""))
print(f"\n{passed}/{len(cases)} passed (database copy: {work})")
(out_dir / "results.json").write_text(json.dumps(rows, indent=1, default=str))

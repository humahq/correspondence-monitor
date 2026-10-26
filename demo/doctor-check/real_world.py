"""Each message in real_world.json goes through the whole bot (on a practice copy, simulated chat) as a post in
the study chat room with Eliot (the PI) in it. Prints whether an alert was made and whether it was posted in the
chat or kept out. Run: CORMON_HOME=<practice copy> python real_world.py [ids...]"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent.parent))
from cormon import config, db, service, settings  # noqa: E402

STUDY = config.study("NCT05842941")
ROOM = [p.email for p in STUDY.people if p.email]
CASES = [c for c in json.loads((HERE / "real_world.json").read_text())
         if len(sys.argv) < 2 or c["id"] in sys.argv[1:]]

sim = settings.data_dir() / "sim"
(sim / "teams_inbox").mkdir(parents=True, exist_ok=True)
(sim / "teams_members.json").write_text(json.dumps({t["space"]: ROOM for t in STUDY.teams.values() if t.get("space")}))

rows, right = [], 0
for c in CASES:
    who = STUDY.person_by_email(c["from"])
    ext = f"real-world:{c['id']}:{time.time_ns()}"
    (sim / "teams_inbox" / f"{c['id']}.json").write_text(json.dumps({
        "id": ext, "study_id": STUDY.study_id, "channel": "room", "from_email": c["from"], "from_name": who.name,
        "text": c["text"]}))
    t0 = time.time()
    service.tick(log=lambda *_: None)
    m = db.q1("SELECT id, status FROM messages WHERE channel='teams' AND external_id=?", (ext,))
    found = db.q("SELECT id, title FROM items WHERE first_message_id=? AND kind='finding'", (m["id"],)) if m else []
    posts = [dict(r) for r in db.q("SELECT o.item_id, o.status, o.meta FROM outbox o WHERE o.channel='teams' AND "
                                   "o.item_id IN (%s)" % ",".join(str(i["id"]) for i in found))] if found else []
    checks = []
    for i in found:
        a = db.q1("SELECT details FROM audit_log WHERE action='careful_check' AND entity_id=? ORDER BY id DESC",
                  (i["id"],))
        if a:
            d = db.unjs(a["details"])
            checks.append({"reason": d.get("reason"), "scores": (d.get("ai") or {}).get("scores")})
    if not found:
        got = "no alert"
    elif any(p["status"] == "withheld" for p in posts):
        got = "kept out"
    elif posts:
        got = "posted"
    else:
        got = "alert, no chat post"
    ok = got == c["want"]
    right += ok
    rows.append({**c, "got": got, "ok": ok, "alerts": [i["title"] for i in found], "checks": checks,
                 "seconds": round(time.time() - t0, 1)})
    top = [max(k["scores"].items(), key=lambda kv: kv[1]) for k in checks if k.get("scores")]
    print(f"{'ok   ' if ok else 'WRONG'} {c['id']} want {c['want']:8} got {got:10} {top} "
          f"| {'; '.join(i['title'] for i in found)[:110]}", flush=True)

print(f"\n{right}/{len(CASES)} right")
(HERE / "real_world_run.json").write_text(json.dumps(rows, indent=1, default=str))

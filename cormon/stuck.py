"""Things that went wrong for good: a message that could not be sent, a site message that could not be read or
has waited too long, a channel not read for too long, an alert that was never sent. Each is emailed once to the
people who run the bot (system.ops_emails, or each study's clinops lead), so nothing fails silently. Only problems
since this check first ran are reported. A listener that has stopped altogether is caught by watchdog.py."""

from __future__ import annotations

from . import config, db, routing, settings


def problems() -> list[dict]:
    since = db.get_state("stuck_since")
    if not since:
        db.set_state("stuck_since", db.now())
        return []
    out = []
    for r in db.q("SELECT o.id, o.purpose, o.channel, o.last_error, i.study_id FROM outbox o"
                  " LEFT JOIN items i ON i.id=o.item_id WHERE o.status='failed' AND o.purpose!='ops_warning'"
                  " AND o.created_at>=?", (since,)):
        out.append({"key": f"outbox:{r['id']}", "study_id": r["study_id"],
                    "what": f"Outgoing {r['channel']} message {r['id']} ({r['purpose']}) was not sent: "
                            f"{r['last_error']}. Send it again with `cormon outbox --send {r['id']}`."})
    for r in db.q("SELECT id, study_id, error FROM messages WHERE status='error' AND received_at>=?", (since,)):
        out.append({"key": f"message:{r['id']}", "study_id": r["study_id"],
                    "what": f"Site message {r['id']} could not be read: {r['error']}. "
                            f"Read it again with `cormon rerun {r['id']}`."})
    lag = float(settings.get("system.stall_minutes", 15))
    if db.get_state("paused") != "1":   # while paused, waiting messages are expected
        for r in db.q("SELECT id, study_id, received_at FROM messages WHERE status IN ('new','processing')"
                      " AND received_at<? AND received_at>=?", (db.later(-lag), since)):
            out.append({"key": f"waiting:{r['id']}", "study_id": r["study_id"],
                        "what": f"Site message {r['id']} has waited more than {lag:.0f} minutes to be read "
                                f"(arrived {r['received_at']})."})
    for name, label in (("email", "Email"), ("teams", "Chat")):
        last = db.get_state(f"polled:{name}")
        if last and last < db.later(-lag):
            out.append({"key": f"unread:{name}:{last}", "study_id": None,
                        "what": f"{label} has not been read for more than {lag:.0f} minutes (last read {last}). "
                                "See `cormon log` for the channel error."})
    for r in db.q("SELECT i.id, i.study_id FROM items i JOIN audit_log a ON a.entity_type='item'"
                  " AND a.entity_id=CAST(i.id AS TEXT) AND a.action='item_route_failed'"
                  " WHERE i.routed=0 AND i.created_at>=?", (since,)):
        out.append({"key": f"item:{r['id']}", "study_id": r["study_id"],
                    "what": f"Alert {r['id']} was made but could not be sent. Send it with `cormon draft {r['id']}`."})
    return out


def _ops(study_ids: set) -> list[str]:
    fixed = settings.get("system.ops_emails") or []
    if fixed:
        return list(fixed)
    studies = config.studies()
    wanted = [studies[s] for s in study_ids if s in studies] or list(studies.values())
    return list(dict.fromkeys(p.email for s in wanted for p in s.people_with_role("clinops_lead") if p.email))


def warn(log=print) -> int | None:
    new = [p for p in problems() if not db.get_state(f"warned:{p['key']}")]
    if not new:
        return None
    to = _ops({p["study_id"] for p in new})
    body = "These need a look:\n\n" + "\n".join(f"- {p['what']}" for p in new)
    oid = routing.enqueue(purpose="ops_warning", channel="email", destination="email", body=body,
                          subject=f"Study monitor: {len(new)} thing{'s' if len(new) > 1 else ''} need a look",
                          from_addr=settings.get("mailbox.address"), to=to) if to else None
    for p in new:
        db.set_state(f"warned:{p['key']}", db.now())
    db.audit("ops_warned", entity_type="service", entity_id="stuck", to=to, outbox_id=oid,
             problems=[p["key"] for p in new])
    log(f"warned {', '.join(to) or 'nobody (no ops email set)'} about {len(new)} stuck thing(s)")
    return oid

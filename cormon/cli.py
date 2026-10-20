"""Operator control surface. Every command works while the listener is running."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

from . import capture, config, db, digest, ingest, items, notes, outbox, pipeline, routing, settings
from .drafting import local_time
from .triage import TIERS


def actor(args) -> str:
    return getattr(args, "as_", None) or settings.actor()


def table(rows: list[dict], cols: list[tuple[str, str, int]]) -> None:
    if not rows:
        print("(none)")
        return
    print("  ".join(h.ljust(w) for _, h, w in cols))
    print("  ".join("-" * w for _, _, w in cols))
    for r in rows:
        cells = []
        for key, _, w in cols:
            v = r.get(key) if isinstance(r, dict) else r[key]
            s = "" if v is None else str(v).replace("\n", " ")
            cells.append((s[: w - 1] + "…") if len(s) > w else s.ljust(w))
        print("  ".join(cells))


# ---------------------------------------------------------------- setup

def cmd_init(args):
    db.conn()
    changed = config.record_config_changes(actor(args), "init")
    print(f"database ready at {db.db_path()}")
    for s in config.studies().values():
        print(f"study {s.study_id} ({s.short_name}) listens on {s.alias}")
    if changed:
        print(f"recorded config: {', '.join(changed)}")


def cmd_reset(args):
    """Archive the database and start a fresh one, keeping the study model (for rehearsals)."""
    import shutil
    import sqlite3
    import time

    if not args.yes:
        print("this archives the current database and starts an empty one; add --yes to confirm")
        return
    src = db.db_path()
    db.audit("database_archived", entity_type="service", entity_id="db", actor=actor(args), actor_type="operator",
             reason=args.reason)
    db.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.conn().close()
    db._local.conn = None
    archive = settings.data_dir() / "archive"
    archive.mkdir(exist_ok=True)
    dest = archive / f"cormon-{time.strftime('%Y%m%d-%H%M%S')}.db"
    shutil.move(str(src), dest)
    for ext in ("-wal", "-shm"):
        Path(str(src) + ext).unlink(missing_ok=True)
    db.conn()
    old = sqlite3.connect(dest)
    old.row_factory = sqlite3.Row
    for tbl in ("documents", "doc_pages", "rules", "allowances", "guide", "rule_decisions", "llm_cache"):
        if not old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
            continue
        rows = old.execute(f"SELECT * FROM {tbl}").fetchall()
        if rows:
            cols = rows[0].keys()
            db.conn().executemany(f"INSERT INTO {tbl} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                  [tuple(r) for r in rows])
    for r in old.execute("SELECT key, value FROM state WHERE key LIKE 'ingest:%' OR key LIKE 'allowances:%'"):
        db.set_state(r["key"], r["value"])
    old.close()
    for sub in ("sim/outbox", "sim/teams"):
        shutil.rmtree(settings.data_dir() / sub, ignore_errors=True)
    config.record_config_changes(actor(args), "reset")
    db.audit("database_reset", entity_type="service", entity_id="db", actor=actor(args), actor_type="operator",
             archived_to=str(dest), reason=args.reason)
    print(f"archived to {dest}; fresh database ready (study model kept)")


def cmd_ingest(args):
    targets = [config.study(args.study)] if args.study else list(config.studies().values())
    for s in targets:
        print(f"{s.study_id}: building study model")
        res = ingest.ingest_study(s, force=args.force)
        print(f"  done: {res}")


def cmd_guide(args):
    from . import guide
    s = config.study(args.study)
    if not args.packs_only:
        for doc_id in guide.documents(s):
            guide.prepare(s, doc_id, guide.source(s, doc_id))
    if guide.checker(s).get("packs"):
        from . import packs
        packs.build(s)
    if guide.checker(s).get("route") == "reader":
        from . import duties
        duties.build(s)


def cmd_duties(args):
    """Print the follow-up table (or rebuild it) so a person can check it against the protocol once."""
    from . import duties
    s = config.study(args.study)
    events = duties.build(s) if args.rebuild else duties.table(s)
    print(duties.render(events))


def cmd_export_rules(args):
    from . import export

    for p in export.export(config.study(args.study), Path(args.folder).expanduser()):
        print(f"  wrote {p}")


def cmd_rules_suggest(args):
    from . import prune

    counts = prune.suggest(config.study(args.study), use_cache=not args.fresh)
    print(f"AI suggestions stored: keep {counts['keep']}, remove {counts['remove']}. Nothing was turned off.\n"
          f"Next: cormon export-rules <folder>, fill in 'Team decision', then cormon rules-decide <sheet.xlsx>")


def cmd_rules_decide(args):
    from . import prune

    study = config.study(args.study)
    if args.rule:
        if args.decision not in ("keep", "remove"):
            sys.exit("give --decision keep or --decision remove with --rule")
        changed = prune.decide(study, args.rule, args.decision, actor(args), args.note or "")
        print(f"{args.rule}: {args.decision}" + ("" if changed else " (already so)"))
        return
    if not args.sheet:
        sys.exit("give the filled-in rules spreadsheet, or --rule ID --decision keep|remove")
    counts = prune.read_sheet(study, Path(args.sheet).expanduser(), actor(args))
    print(f"turned off {counts['remove']}, kept or turned back on {counts['keep']}, unchanged {counts['unchanged']}, "
          f"no decision {counts['blank']}")
    for bad in counts["not understood"]:
        print(f"  not understood (use keep or remove): {bad}")
    print(f"rules in use now: {len(ingest.active_rules(study.study_id))}")


def cmd_import_model(args):
    counts = ingest.import_model(Path(args.db), args.reason, actor(args), args.study)
    print(f"study model replaced from {args.db}: {counts}")


def cmd_rules(args):
    if args.report:
        print(ingest.report(config.study(args.study)))
        return
    if args.checklist:
        print(ingest.checklist(config.study(args.study))[0])
        return
    rows = ingest.active_rules(args.study, [args.pattern] if args.pattern else None) if not args.all else \
        [dict(r) for r in db.q("SELECT * FROM rules WHERE study_id=? ORDER BY pattern_class, order_idx", (args.study,))]
    if args.show:
        r = db.q1("SELECT * FROM rules WHERE id=?", (args.show,))
        print(json.dumps(dict(r), indent=2, ensure_ascii=False) if r else "not found")
        return
    for r in rows:
        r["check"] = (f"{r['parameter']} {r['operator']} {r['value']:g}" if r["check_type"] == "numeric"
                      and r.get("value") is not None else r["check_type"])
        r["ok"] = "yes" if r["verified"] else "NO"
    table(rows, [("id", "rule", 34), ("check", "check", 26), ("source_page", "p.", 4), ("ok", "src", 4),
                 ("text", "text", 70)])


# ---------------------------------------------------------------- running

def cmd_run(args):
    from . import service

    service.run(once=args.once)


def cmd_status(args):
    paused = outbox.is_paused()
    print(f"listener heartbeat: {local_time(db.get_state('heartbeat'))}   processing: "
          f"{'PAUSED' if paused else 'running'}   channels: email={settings.get('channels.email')} "
          f"teams={settings.get('channels.teams')}")
    counts = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM messages GROUP BY status")}
    ob = {r["status"]: r["n"] for r in db.q("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
    print(f"messages: {counts or '{}'}   outbox: {ob or '{}'}")
    print("open items: " + ", ".join(
        f"{r['tier']}={r['n']}" for r in db.q(
            "SELECT tier, COUNT(*) n FROM items WHERE state='open' AND counts=1 GROUP BY tier")) or "none")
    print("\nqueue (waiting to process or send):")
    q = [dict(r, what="message") for r in db.q(
        "SELECT id, subject AS summary, status FROM messages WHERE status IN ('new','processing','error') ORDER BY id")]
    q += [dict(r, what="outbox") for r in db.q(
        "SELECT id, purpose || ' -> ' || COALESCE(to_addrs, destination) AS summary, status FROM outbox"
        " WHERE status IN ('queued','sending','held','failed') ORDER BY id")]
    table(q, [("what", "kind", 8), ("id", "id", 5), ("status", "status", 10), ("summary", "summary", 80)])
    print("\nrecent activity:")
    cmd_log(argparse.Namespace(item=None, message=None, limit=args.limit, verbose=False))


def cmd_backup(args):
    from . import backup
    if args.check:
        res = backup.check(Path(args.check))
    else:
        res = backup.make()
    print(f"{'OK' if res['ok'] else 'FAILED'}: {res['path']}  rows: {res['counts']}")
    for p in res["problems"]:
        print(f"  - {p}")


def cmd_restore(args):
    from . import backup
    if not args.yes:
        print("Stop the service first (sudo systemctl stop cormon), then run again with --yes.")
        return
    res = backup.restore(Path(args.file))
    print(f"restored {res['restored']}; the old database is kept as {res['kept']}  rows: {res['counts']}")


def cmd_pause(args):
    db.set_state("paused", "1")
    db.audit("processing_paused", entity_type="service", entity_id="listener", actor=actor(args),
             actor_type="operator", reason=args.reason)
    print("processing paused (messages are still captured; nothing is classified or sent)")


def cmd_resume(args):
    db.set_state("paused", "0")
    db.audit("processing_resumed", entity_type="service", entity_id="listener", actor=actor(args),
             actor_type="operator")
    print("processing resumed")


# ---------------------------------------------------------------- inspecting

def cmd_messages(args):
    rows = [dict(r) for r in db.q(
        "SELECT id, channel, study_id, visibility, from_addr, subject, status, sent_at FROM messages"
        " ORDER BY id DESC LIMIT ?", (args.limit,))]
    for r in rows:
        r["sent"] = local_time(r["sent_at"])
    table(rows, [("id", "id", 5), ("channel", "chan", 6), ("study_id", "study", 12), ("visibility", "seen as", 8),
                 ("from_addr", "from", 26), ("sent", "sent", 17), ("status", "status", 10), ("subject", "subject", 40)])


def cmd_message(args):
    m = capture.message(args.id)
    print(f"#{m['id']} {m['channel']} · study {m['study_id'] or '(general)'} · seen as {m['visibility']}"
          f" · alias {m['recipient_alias']} · status {m['status']}")
    print(f"From: {m['from_name']} <{m['from_addr']}>\nTo: {', '.join(m['to_addrs'])}\nCc: {', '.join(m['cc_addrs'])}")
    print(f"Date: {local_time(m['sent_at'])} (received {local_time(m['received_at'])})\nSubject: {m['subject']}\n")
    print(m["body_text"])
    for a in m["attachments"]:
        print(f"\n[attachment] {a['filename']} · {a['component']} · dated {local_time(a['source_date'])}"
              f" · placement {a['placement']}")
    ev = db.q1("SELECT * FROM events WHERE message_id=? ORDER BY version DESC LIMIT 1", (args.id,))
    if ev:
        print(f"\n--- event model v{ev['version']} ({ev['source']}, {ev['model']}) ---")
        print(json.dumps(db.unjs(ev["data"]), indent=2, ensure_ascii=False))


def cmd_items(args):
    sql = "SELECT * FROM items WHERE 1=1"
    params: list = []
    if args.study:
        sql += " AND study_id=?"; params.append(args.study)
    if args.tier:
        sql += " AND tier=?"; params.append(args.tier)
    if args.state:
        sql += " AND state=?"; params.append(args.state)
    if args.clearing:
        sql += " AND tier='low' AND kind IN ('finding','good_news')"
    if args.sponsor_view:
        sql += " AND cleared=0 AND state='open' AND counts=1 AND kind != 'out_of_scope'"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = [dict(r) for r in db.q(sql, params)]
    for r in rows:
        r["clr"] = "yes" if r["cleared"] else ""
        r["dest"] = ", ".join(sorted({o["destination"] for o in db.q(
            "SELECT destination FROM outbox WHERE item_id=?", (r["id"],))})) or "-"
        r["src"] = "/".join(s[0] for s in (r["tier_source"], r["recipient_source"]))
    table(rows, [("id", "id", 4), ("tier", "tier", 9), ("src", "src", 4), ("state", "state", 12), ("clr", "clr", 4),
                 ("kind", "kind", 12), ("participant_id", "participant", 11), ("recipient_person", "recipient", 13),
                 ("dest", "destinations", 30), ("title", "title", 60)])
    print("\nsrc = who set tier/recipient (s=system, o=operator, c=CRA)")


def cmd_item(args):
    it = items.get(args.id)
    ev = it["evidence"]
    print(f"Item #{it['id']}  [{it['tier']} · set by {it['tier_source']}]  state={it['state']}  kind={it['kind']}")
    print(f"Title: {it['title']}")
    print(f"Study {it['study_id']} · site {it['site']} · participant {it['participant_id']} · class {it['pattern_class']}")
    print(f"Recipient: {it['recipient_person']} ({it['recipient_source']}: {it['recipient_reason']})")
    print(f"send_draft={it['send_draft']} ({it['send_draft_source']})  counts={it['counts']} ({it['counts_source']})")
    print(f"Tier reason: {it['tier_reason']}")
    print(f"Detail page: {settings.get('web.base_url')}/items/{it['link_token']}")
    print(f"\nCorrespondence: \"{ev.get('message_quote')}\"  (message {ev.get('message_id')})")
    if ev.get("doc_quote"):
        print(f"Document: \"{ev.get('doc_quote')}\"  ({ev.get('doc_title')}, p. {ev.get('doc_page')}, rule {ev.get('rule_id')})")
    for a in ev.get("additional") or []:
        print(f"Also does not meet: \"{a.get('doc_quote')}\"  (p. {a.get('doc_page')}, rule {a.get('rule_id')})"
              f"  site wrote: \"{a.get('message_quote')}\"")
    print(f"Reasoning: {it.get('reasoning')}")
    if ev.get("confirmation"):
        print(f"Confirmation against original: {ev.get('confirmation')}")
    problems = items.check_consistency(it)
    if problems:
        print("\nINCONSISTENT STATE: " + "; ".join(problems))
    print("\nOutbound:")
    table([dict(r) for r in db.q("SELECT id, destination, to_addrs, status, subject FROM outbox WHERE item_id=?",
                                 (it["id"],))],
          [("id", "id", 5), ("destination", "destination", 20), ("to_addrs", "to", 30), ("status", "status", 8),
           ("subject", "subject", 60)])
    if args.drafts:
        for r in db.q("SELECT * FROM outbox WHERE item_id=? ORDER BY id", (it["id"],)):
            print(f"\n=== outbox {r['id']} · {r['destination']} · {r['status']} · template {r['template_id']} ===")
            if r["subject"]:
                print(f"From: {r['from_addr']}\nTo: {r['to_addrs']}  Cc: {r['cc_addrs']}\nSubject: {r['subject']}\n")
            print(r["body"])
    print("\nHistory:")
    for h in items.history(it["id"]):
        print(f"  {local_time(h['ts'])}  {h['actor_type']}:{h['actor']}  {h['action']}")


def cmd_log(args):
    sql = "SELECT * FROM audit_log WHERE 1=1"
    params: list = []
    if args.item:
        sql += " AND entity_type='item' AND entity_id=?"; params.append(str(args.item))
    if args.message:
        sql += " AND entity_type='message' AND entity_id=?"; params.append(str(args.message))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = [dict(r) for r in db.q(sql, params)][::-1]
    for r in rows:
        r["when"] = local_time(r["ts"])
        r["who"] = f"{r['actor_type'][0]}:{r['actor']}"
        r["what"] = f"{r['entity_type'] or ''} {r['entity_id'] or ''}".strip()
        d = db.unjs(r["details"], {})
        r["info"] = d if args.verbose else ", ".join(
            f"{k}={v}" for k, v in d.items() if v not in (None, "", [], {}) and k not in ("evidence", "diff"))[:120]
    table(rows, [("id", "#", 5), ("when", "when", 17), ("who", "who", 18), ("action", "action", 30),
                 ("what", "entity", 14), ("info", "details", 90)])


def cmd_outbox(args):
    if args.send:
        print(outbox.dispatch(only_id=args.send))
        return
    rows = [dict(r) for r in db.q("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (args.limit,))]
    table(rows, [("id", "id", 5), ("item_id", "item", 5), ("purpose", "purpose", 22), ("destination", "dest", 20),
                 ("to_addrs", "to", 28), ("status", "status", 8), ("last_error", "error", 40)])


# ---------------------------------------------------------------- control actions

def cmd_rerun(args):
    db.audit("rerun_requested", entity_type="message", entity_id=args.id, actor=actor(args), actor_type="operator",
             fresh=not args.cached)
    res = pipeline.process(args.id, use_cache=args.cached, rerun=True, actor=actor(args))
    print(json.dumps(res, indent=2))
    if not outbox.is_paused():
        print(outbox.dispatch())


def cmd_draft(args):
    it = items.get(args.id)
    if it["routed"] and not args.force:
        print("item already has drafts; use --force to produce them again from the current state")
    created = routing.route_item(args.id, force=True, actor=actor(args))
    db.audit("draft_forced", entity_type="item", entity_id=args.id, study_id=it["study_id"], actor=actor(args),
             actor_type="operator", outbox_ids=created)
    for oid in created:
        r = db.q1("SELECT * FROM outbox WHERE id=?", (oid,))
        print(f"\n=== outbox {oid} · {r['destination']} · {r['status']} ===")
        if r["subject"]:
            print(f"To: {r['to_addrs']}  Subject: {r['subject']}\n")
        print(r["body"])
    if args.send:
        for oid in created:
            print(outbox.dispatch(only_id=oid))
    elif created:
        print("\nqueued; the listener sends them on its next pass (use `cormon outbox --send ID` to send now)")


def _study_of(item_id: int) -> str:
    """The server operator acts on any study: the item's own."""
    return items.get(item_id)["study_id"]


def cmd_suppress(args):
    items.set_state(args.id, "suppressed", actor(args), "operator", args.reason, study_id=_study_of(args.id))
    print(f"item {args.id} suppressed; queued messages for it are held")


def cmd_unsuppress(args):
    items.set_state(args.id, "open", actor(args), "operator", args.reason, study_id=_study_of(args.id))
    db.ex("UPDATE outbox SET status='queued' WHERE item_id=? AND status='held' AND last_error='item suppressed'",
          (args.id,))
    print(f"item {args.id} reopened")


def cmd_clear(args):
    items.set_state(args.id, "open" if args.undo else "cleared", actor(args), "operator", args.reason,
                    study_id=_study_of(args.id))
    print(f"item {args.id} {'uncleared' if args.undo else 'cleared'}")


def _bool(v: str) -> int:
    return 1 if v.lower() in ("1", "yes", "y", "true", "on") else 0


def cmd_override(args):
    changes = []
    if args.tier:
        changes.append(("tier", args.tier))
    if args.recipient:
        changes.append(("recipient_person", args.recipient))
    if args.send_draft is not None:
        changes.append(("send_draft", _bool(args.send_draft)))
    if args.counts is not None:
        changes.append(("counts", _bool(args.counts)))
    if not changes:
        print("nothing to change (use --tier, --recipient, --send-draft, --counts)")
        return
    for field, value in changes:
        problems = items.override(args.id, field, value, actor(args), "operator", args.reason, args.force,
                                  study_id=_study_of(args.id))
        if problems:
            print(f"NOT APPLIED ({field}={value}); it would leave the item inconsistent:")
            for p in problems:
                print(f"  - {p}")
            print("re-run with --force to apply anyway (the inconsistency is recorded)")
            return
        print(f"{field} set to {value} (operator-supplied; later automatic passes will not change it)")
    it = items.get(args.id)
    if args.tier and it["routed"]:
        print("note: the tier changed after routing. Existing messages were not changed. "
              f"Run `cormon draft {args.id} --force` to route again for the new tier.")


def cmd_digest(args):
    res = digest.send(hours=args.hours, study_ids=[args.study] if args.study else None,
                      dry_run=not args.send, actor=actor(args))
    for r in res:
        print(f"=== to {r['to']} · {r['total']} item(s) ===\nSubject: {r['subject']}\n\n{r['body']}\n")
        if args.send:
            print(outbox.dispatch(only_id=r["outbox_id"]))
    if not args.send:
        print("(preview only; add --send to send it now. The daily schedule is unaffected.)")


def cmd_note(args):
    if args.action == "add":
        nid = notes.add(args.scope, args.target, args.kind, args.text, actor(args))
        print(f"note {nid} added ({args.scope}{':' + args.target if args.target else ''})")
    elif args.action == "list":
        table([dict(r) for r in db.q("SELECT * FROM notes WHERE active=1 ORDER BY id")],
              [("id", "id", 4), ("scope", "scope", 8), ("target", "target", 26), ("kind", "kind", 11),
               ("author", "author", 12), ("text", "text", 70)])
    elif args.action == "retire":
        notes.retire(int(args.target), actor(args))
        print(f"note {args.target} retired")


CONFIG_FILES = {"triage": "config/triage.yaml", "routing": "config/routing.yaml",
                "templates": "config/templates.yaml", "settings": "config/settings.yaml"}


def _config_file(name: str) -> Path:
    if name in CONFIG_FILES:
        return settings.resolve(CONFIG_FILES[name])
    if name.startswith("study:"):
        return config.study(name.split(":", 1)[1]).dir / "study.yaml"
    return settings.resolve(name)


def cmd_config(args):
    path = _config_file(args.file)
    if args.action == "show":
        data = settings.load_yaml(path)
        node = config.get_path(data, args.path) if args.path else data
        print(yaml.safe_dump(node, sort_keys=False, allow_unicode=True))
    elif args.action == "set":
        from ruamel.yaml import YAML

        config.record_config_changes()  # attribute earlier direct edits separately
        rt = YAML()
        rt.preserve_quotes = True
        rt.width = 4096
        rt.indent(mapping=2, sequence=4, offset=2)
        with open(path, encoding="utf-8") as fh:
            data = rt.load(fh)
        value = yaml.safe_load(args.value)
        old = config.get_path(data, args.path) if args.path else None
        config.set_path(data, args.path, value)
        with open(path, "w", encoding="utf-8") as fh:
            rt.dump(data, fh)
        config.record_config_changes(actor(args), args.reason or f"set {args.path}")
        print(f"{args.file}: {args.path} changed from {old!r} to {value!r} (recorded)")
    elif args.action == "edit":
        config.record_config_changes()
        editor = os.environ.get("EDITOR", "nano")
        subprocess.call([editor, str(path)])
        try:
            settings.load_yaml(path)
        except yaml.YAMLError as e:
            print(f"WARNING: {path} is not valid YAML now: {e}")
        changed = config.record_config_changes(actor(args), args.reason or "edited")
        print(f"recorded: {changed or 'no change'}")
    elif args.action == "history":
        table([dict(r) for r in db.q("SELECT id, path, actor, recorded_at, substr(sha256,1,10) sha FROM config_versions"
                                     " ORDER BY id DESC LIMIT 30")],
              [("id", "id", 4), ("path", "file", 36), ("actor", "who", 24), ("recorded_at", "when", 26), ("sha", "sha", 10)])


def cmd_test(args):
    """Run a folder of test messages through the sandbox copy and score them (nothing is sent)."""
    from . import testbench
    sys.exit(testbench.main(args.folder, args.out, args.study))


def cmd_compare(args):
    """Score the same test folder three ways: the bot's rules, the full documents, and a plain rule list."""
    from . import compare
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    bad = set(modes) - set(compare.MODES)
    if bad:
        sys.exit(f"unknown mode(s): {', '.join(sorted(bad))}; choose from {', '.join(compare.MODES)}")
    compare.run(args.folder, args.out, modes, args.model, args.study)


def cmd_inject(args):
    """Put a message into the local simulator inbox (rehearsal without real accounts)."""
    src = Path(args.file)
    dest_dir = settings.data_dir() / "sim" / ("teams_inbox" if src.suffix == ".json" else "inbox")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{int(__import__('time').time() * 1000)}_{src.name}"
    dest.write_bytes(src.read_bytes())
    print(f"queued {src.name} in the simulator inbox")
    if args.now:
        from . import service

        service.tick()


def cmd_auth(args):
    if args.service == "gmail":
        from .channels.gmail import credentials

        credentials(interactive=True)
        print("Gmail authorised")
    elif args.service == "imap":
        from .channels.imap import ImapChannel

        r = ImapChannel().check()
        print(f"Gmail login works for {r['user']} ({r['inbox_messages']} emails in the inbox)")
    elif args.service == "gchat":
        from .channels.gchat import GoogleChatChannel, credentials

        credentials(interactive=True)
        ch = GoogleChatChannel()
        who = ch._users.get(ch.me) or ch.me
        print(f"Google Chat authorised as {who}")
        if who != settings.get("gchat.account", settings.get("mailbox.address")):
            print(f"Warning: this is not {settings.get('gchat.account', settings.get('mailbox.address'))}."
                  " Run it again and sign in as the bot account.")
    else:
        from .channels.teams import token

        token(interactive=True)
        print("Teams authorised")


def cmd_teams(args):
    from .channels.teams import TeamsChannel

    table(TeamsChannel().list_channels(), [("team", "team", 24), ("channel", "channel", 24),
                                           ("team_id", "team_id", 38), ("channel_id", "channel_id", 50)])


def cmd_chat_spaces(args):
    from .channels.gchat import GoogleChatChannel

    table(GoogleChatChannel().list_spaces(), [("name", "name", 30), ("type", "type", 16), ("space", "space", 24)])


def cmd_gmail(args):
    from .channels.gmail import GmailChannel

    g = GmailChannel()
    if args.action == "send-as":
        print("send-as addresses:", ", ".join(g.send_as_addresses()))
    else:
        for s in config.studies().values():
            print(f"{s.alias}: {g.ensure_send_as(s.alias)}")


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cormon", description="Correspondence monitor: operator control surface",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=textwrap.dedent("""\
                                    examples:
                                      cormon status
                                      cormon items --tier immediate
                                      cormon item 12 --drafts
                                      cormon override 12 --tier medium --reason "routine for this study"
                                      cormon note add --scope person --target crc@site.org --kind fact "Uses MAP-0xxx IDs"
                                      cormon digest --hours 24 --send
                                    """))
    p.add_argument("--as", dest="as_", help="operator identity recorded in the log (default: $CORMON_OPERATOR or OS user)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the database and record the current config").set_defaults(fn=cmd_init)

    s = sub.add_parser("reset", help="archive the database and start fresh, keeping the study model")
    s.add_argument("--yes", action="store_true"); s.add_argument("--reason"); s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("ingest", help="build the study model from the study documents")
    s.add_argument("--study"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("guide", help="prepare the protocol as a guide (sections, tables by row, visit checklists)")
    s.add_argument("--study", required=True)
    s.add_argument("--packs-only", action="store_true", help="rebuild the topic packs from the stored sections")
    s.set_defaults(fn=cmd_guide)

    s = sub.add_parser("checks", help="build the check list from the protocol and print its coverage")
    s.add_argument("--study", required=True)
    s.add_argument("--fresh", action="store_true", help="ask the model again instead of using stored answers")
    s.set_defaults(fn=lambda a: __import__("cormon.checks", fromlist=["build"]).build(
        config.study(a.study), use_cache=not a.fresh))

    s = sub.add_parser("duties", help="print the follow-up table: what the site must do after each event")
    s.add_argument("--study", required=True)
    s.add_argument("--rebuild", action="store_true", help="build it again from the guide sections")
    s.set_defaults(fn=cmd_duties)

    s = sub.add_parser("export-rules", help="write the rules (Word and Excel) and the source documents to a folder")
    s.add_argument("folder"); s.add_argument("--study", default=next(iter(config.studies()), None))
    s.set_defaults(fn=cmd_export_rules)

    s = sub.add_parser("rules-suggest", help="AI suggests keep or remove for each rule (turns nothing off)")
    s.add_argument("--study", default=next(iter(config.studies()), None))
    s.add_argument("--fresh", action="store_true", help="ask again instead of using stored answers")
    s.set_defaults(fn=cmd_rules_suggest)

    s = sub.add_parser("rules-decide", help="apply the team's keep/remove decisions from the rules spreadsheet")
    s.add_argument("sheet", nargs="?", help="the exported rules .xlsx with the 'Team decision' column filled in")
    s.add_argument("--rule", help="decide one rule by id instead"); s.add_argument("--decision")
    s.add_argument("--note"); s.add_argument("--study", default=next(iter(config.studies()), None))
    s.set_defaults(fn=cmd_rules_decide)

    s = sub.add_parser("import-model", help="replace the study model with one rebuilt and tested in another copy")
    s.add_argument("db", help="the other copy's database, e.g. practice/data/cormon.db")
    s.add_argument("--reason", required=True)
    s.add_argument("--study", action="append", help="only this study (repeatable); default every study in it")
    s.set_defaults(fn=cmd_import_model)

    s = sub.add_parser("rules", help="list the study model's rules")
    s.add_argument("--study", default=next(iter(config.studies()), None))
    s.add_argument("--pattern"); s.add_argument("--all", action="store_true", help="include rejected rules")
    s.add_argument("--show", help="show one rule in full")
    s.add_argument("--report", action="store_true", help="what was kept, dropped and why, and contradictions")
    s.add_argument("--checklist", action="store_true",
                   help="every inclusion/exclusion criterion and lab-manual step, with its rule or why none")
    s.set_defaults(fn=cmd_rules)

    s = sub.add_parser("run", help="run the listener"); s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", help="state, queue and recent activity"); s.add_argument("--limit", type=int, default=15)
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("backup", help="back up the database now and check the copy")
    s.add_argument("--check", metavar="FILE", help="only check an existing backup"); s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore", help="replace the database with a checked backup (service stopped)")
    s.add_argument("file"); s.add_argument("--yes", action="store_true"); s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("pause", help="pause processing and sending"); s.add_argument("--reason"); s.set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume processing").set_defaults(fn=cmd_resume)

    s = sub.add_parser("messages", help="recent messages"); s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_messages)
    s = sub.add_parser("message", help="one message, its attachments and event model"); s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_message)

    s = sub.add_parser("items", help="list items")
    s.add_argument("--study"); s.add_argument("--tier", choices=TIERS); s.add_argument("--state")
    s.add_argument("--clearing", action="store_true", help="the low-tier clearing list")
    s.add_argument("--sponsor-view", action="store_true", help="only what is not cleared")
    s.add_argument("--limit", type=int, default=30); s.set_defaults(fn=cmd_items)
    s = sub.add_parser("item", help="one item with evidence, drafts and history")
    s.add_argument("id", type=int); s.add_argument("--drafts", action="store_true"); s.set_defaults(fn=cmd_item)

    s = sub.add_parser("log", help="audit log")
    s.add_argument("--item", type=int); s.add_argument("--message", type=int)
    s.add_argument("--limit", type=int, default=40); s.add_argument("--verbose", action="store_true")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("outbox", help="outbound messages"); s.add_argument("--limit", type=int, default=30)
    s.add_argument("--send", type=int, metavar="ID", help="send (or retry) one outbox message now")
    s.set_defaults(fn=cmd_outbox)

    s = sub.add_parser("rerun", help="re-run classification on one message")
    s.add_argument("id", type=int); s.add_argument("--cached", action="store_true", help="reuse cached model output")
    s.set_defaults(fn=cmd_rerun)

    s = sub.add_parser("draft", help="force drafts to be produced for an item")
    s.add_argument("id", type=int); s.add_argument("--force", action="store_true")
    s.add_argument("--send", action="store_true", help="send them immediately"); s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("suppress", help="suppress an item"); s.add_argument("id", type=int)
    s.add_argument("--reason", required=True); s.set_defaults(fn=cmd_suppress)
    s = sub.add_parser("unsuppress", help="reopen a suppressed item"); s.add_argument("id", type=int)
    s.add_argument("--reason"); s.set_defaults(fn=cmd_unsuppress)
    s = sub.add_parser("clear", help="mark a clearing-list item cleared"); s.add_argument("id", type=int)
    s.add_argument("--undo", action="store_true"); s.add_argument("--reason"); s.set_defaults(fn=cmd_clear)

    s = sub.add_parser("override", help="override tier, recipient, sending or whether a finding counts")
    s.add_argument("id", type=int); s.add_argument("--tier", choices=TIERS); s.add_argument("--recipient")
    s.add_argument("--send-draft"); s.add_argument("--counts"); s.add_argument("--reason")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_override)

    s = sub.add_parser("digest", help="produce the digest for a chosen window")
    s.add_argument("--hours", type=float); s.add_argument("--study"); s.add_argument("--send", action="store_true")
    s.set_defaults(fn=cmd_digest)

    s = sub.add_parser("note", help="record a fact or instruction with a scope")
    s.add_argument("action", choices=["add", "list", "retire"])
    s.add_argument("text", nargs="?"); s.add_argument("--scope", choices=notes.SCOPES, default="study")
    s.add_argument("--target", help="message id, person email, or study id (note id for retire)")
    s.add_argument("--kind", choices=["fact", "instruction"], default="fact"); s.set_defaults(fn=cmd_note)

    s = sub.add_parser("config", help="show, set or edit routing rules, thresholds, templates (changes recorded)")
    s.add_argument("action", choices=["show", "set", "edit", "history"])
    s.add_argument("file", nargs="?", default="triage", help="triage | routing | templates | settings | study:<id>")
    s.add_argument("path", nargs="?", help="dotted path, e.g. rules.0.tier"); s.add_argument("value", nargs="?")
    s.add_argument("--reason"); s.set_defaults(fn=cmd_config)

    s = sub.add_parser("test", help="run a folder of test messages (.txt, .eml, .csv, .json) through a sandbox and "
                                    "score them")
    s.add_argument("folder"); s.add_argument("--out", help="folder for results.csv and summary.json")
    s.add_argument("--study", help="study id the messages are about (default: the first study)")
    s.set_defaults(fn=cmd_test)

    s = sub.add_parser("compare", help="score a test folder against the rules, the full documents and a rule list")
    s.add_argument("folder"); s.add_argument("--out", help="folder for table.txt, per_case.csv and summary.json")
    s.add_argument("--study", help="study id the messages are about (default: the first study)")
    s.add_argument("--modes", default="rules,document,rulelist")
    s.add_argument("--model", help="model for the document and rulelist modes (default: the configured one)")
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("inject", help="drop a .eml or Teams .json file into the local simulator")
    s.add_argument("file"); s.add_argument("--now", action="store_true", help="process immediately")
    s.set_defaults(fn=cmd_inject)

    s = sub.add_parser("auth", help="sign in to Gmail, Teams or Google Chat")
    s.add_argument("service", choices=["imap", "gmail", "teams", "gchat"])
    s.set_defaults(fn=cmd_auth)
    sub.add_parser("teams-channels", help="list Teams channels the bot can see (for study.yaml)").set_defaults(fn=cmd_teams)
    sub.add_parser("chat-spaces", help="list Google Chat spaces the bot is in (for study.yaml)").set_defaults(
        fn=cmd_chat_spaces)
    s = sub.add_parser("gmail", help="Gmail setup helpers"); s.add_argument("action", choices=["send-as", "add-aliases"])
    s.set_defaults(fn=cmd_gmail)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

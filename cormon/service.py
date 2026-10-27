"""The listener: polls channels, processes messages, sends drafts, sends the digest."""

from __future__ import annotations

import threading
import time
import traceback

from . import backup, capture, config, db, digest, llm, outbox, phi, pipeline, settings, stuck, webview
from .channels import chat_events, email_channel, teams_channel
from .compliance import deps, records, trail


_last_poll: dict[str, float] = {}


def poll_seconds(name: str) -> int:
    """How often a channel is read in full. With Chat push on, the Chat poll only catches up on lost notes."""
    every = int(settings.get("system.poll_seconds", 20))
    if name == "teams":
        return int(settings.get("system.chat_poll_seconds") or (60 if chat_events.enabled() else every))
    return int(settings.get("system.email_poll_seconds") or every)


poll_times: dict[str, float] = {}   # seconds each channel took in the last round, for the timing line


def poll_channels(log=print, named: list[str] | None = None, force: bool = False) -> int:
    """Reads each channel whose poll is due, and the Chat messages push notes named. A round started by a push
    note reads Chat only: a slow mailbox must not hold the message back (email is read next round)."""
    n = 0
    poll_times.clear()
    for name, get in (("teams", teams_channel), ("email", email_channel)):
        due = force or time.monotonic() - _last_poll.get(name, -1e9) >= poll_seconds(name)
        quick = name == "teams" and bool(named) and not force   # a due full read waits one round
        if not (due or quick) or (named and name == "email" and not force):
            continue
        began = time.perf_counter()
        try:
            adapter = get()
            if adapter is None:
                continue
            if quick and hasattr(adapter, "fetch_named"):
                got = adapter.fetch_named(named)
            else:
                _last_poll[name] = time.monotonic()
                got = adapter.fetch()
            for inb in got:
                mid = capture.capture(inb)
                if hasattr(adapter, "ack"):
                    adapter.ack(inb)
                if mid:
                    n += 1
                    log(f"captured {name} message {mid}")  # no message text: the system log is not for patient data
            db.set_state(f"polled:{name}", db.now())
        except Exception as e:
            if quick:  # the full read is made next tick instead
                _last_poll.pop(name, None)
            db.audit("channel_error", entity_type="channel", entity_id=name, error=str(e)[:500])
            log(f"{name} channel error: {e}")
        poll_times[("chat" if name == "teams" else name) + (" (pushed)" if quick else "")] = \
            time.perf_counter() - began
    return n


def _safely(step, log) -> None:
    """A housekeeping step that fails is logged; it never stops reading or sending."""
    try:
        step(log)
    except Exception as e:
        db.audit("housekeeping_error", entity_type="service", entity_id=step.__qualname__, error=str(e)[:500])
        log(f"{step.__qualname__} failed: {e}")


def tick(log=print, named: list[str] | None = None, force: bool = True) -> None:
    """One round. force: read every channel now (tests, --once); the listener reads each when its poll is due."""
    t0 = time.perf_counter()
    config.record_config_changes()
    captured = poll_channels(log, named, force)  # capture continues while paused, so nothing is dropped
    t1 = time.perf_counter()
    db.set_state("heartbeat", db.now())
    _safely(backup.daily, log)
    _safely(phi.daily, log)
    _safely(trail.hourly, log)
    _safely(trail.daily, log)
    _safely(records.daily, log)
    _safely(deps.weekly, log)
    _safely(llm.check_models, log)
    if outbox.is_paused():
        return
    _safely(pipeline.recover_messages, log)
    t2 = time.perf_counter()
    done = pipeline.process_pending(log)
    for r in done:
        if r.get("items_created"):
            log(f"message {r['message_id']}: items {r['items_created']}")
    t3 = time.perf_counter()
    _safely(pipeline.route_missed, log)
    if digest.due():
        digest.send()
        digest.mark_sent()
        log("daily digest queued")
    _safely(outbox.recover, log)
    _safely(stuck.warn, log)
    _safely(stuck.reconcile_mailbox, log)
    _safely(chat_push, log)
    t4 = time.perf_counter()
    res = outbox.dispatch()
    if res["sent"] or res["failed"]:
        log(f"outbox: sent {res['sent']}, failed {res['failed']}")
    if captured or done or res["sent"] or res["failed"]:
        each = ", ".join(f"{k} {v:.1f}s" for k, v in poll_times.items())
        log(f"timing: poll {t1 - t0:.1f}s ({each or 'none due'}), read {len(done)} message(s) {t3 - t2:.1f}s, "
            f"send {time.perf_counter() - t4:.1f}s, tick {time.perf_counter() - t0:.1f}s")


def chat_push(log=print) -> None:
    """Keeps the Google Chat push subscription alive (renewed well before Google ends it)."""
    if chat_events.enabled():
        chat_events.ensure(teams_channel().http, log)


def run(once: bool = False, log=print) -> None:
    server = None if once else webview.start_background()
    if server:
        log(f"item pages on {settings.get('web.base_url')}")
    db.audit("listener_started", entity_type="service", entity_id="listener", once=once)
    interval = int(settings.get("system.poll_seconds", 20))
    wake = threading.Event()   # set by the Chat push listener: a message was posted, read now
    pushed = None
    if not once and chat_events.enabled():
        pushed = chat_events.Listener(wake, log)
        pushed.start()
        log("chat push listener on; the poll stays as a fallback")
    try:
        while True:
            wake.clear()
            try:
                tick(log, chat_events.take_named(), force=False)
            except Exception:
                log(traceback.format_exc())
            if once:
                break
            wake.wait(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if pushed:
            pushed.stop.set()
        db.audit("listener_stopped", entity_type="service", entity_id="listener")

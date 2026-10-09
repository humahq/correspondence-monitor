"""The listener: polls channels, processes messages, sends drafts, sends the digest."""

from __future__ import annotations

import time
import traceback

from . import capture, config, db, digest, outbox, pipeline, settings, webview
from .channels import email_channel, teams_channel


def poll_channels(log=print) -> int:
    n = 0
    for name, get in (("email", email_channel), ("teams", teams_channel)):
        try:
            adapter = get()
            if adapter is None:
                continue
            for inb in adapter.fetch():
                mid = capture.capture(inb)
                if hasattr(adapter, "ack"):
                    adapter.ack(inb)
                if mid:
                    n += 1
                    log(f"captured {name} message {mid}: {inb.subject or inb.body_text[:60]!r}")
        except Exception as e:
            db.audit("channel_error", entity_type="channel", entity_id=name, error=str(e)[:500])
            log(f"{name} channel error: {e}")
    return n


def tick(log=print) -> None:
    config.record_config_changes()
    poll_channels(log)  # capture continues while paused, so nothing is dropped
    db.set_state("heartbeat", db.now())
    if outbox.is_paused():
        return
    for r in pipeline.process_pending(log):
        if r.get("items_created"):
            log(f"message {r['message_id']}: items {r['items_created']}")
    if digest.due():
        digest.send()
        digest.mark_sent()
        log("daily digest queued")
    res = outbox.dispatch()
    if res["sent"] or res["failed"]:
        log(f"outbox: sent {res['sent']}, failed {res['failed']}")


def run(once: bool = False, log=print) -> None:
    server = None if once else webview.start_background()
    if server:
        log(f"item pages on {settings.get('web.base_url')}")
    db.audit("listener_started", entity_type="service", entity_id="listener", once=once)
    interval = int(settings.get("system.poll_seconds", 20))
    try:
        while True:
            try:
                tick(log)
            except Exception:
                log(traceback.format_exc())
            if once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        db.audit("listener_stopped", entity_type="service", entity_id="listener")

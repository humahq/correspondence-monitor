"""Read-only item detail pages, reached only through the unguessable link in a draft."""

from __future__ import annotations

import html
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, items, settings
from .drafting import also_broken, local_time
from .triage import TIER_LABELS

CSS = """body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:860px;margin:32px auto;
padding:0 16px;color:#1d2433;line-height:1.45}h1{font-size:22px}h2{font-size:16px;margin-top:28px;
border-bottom:1px solid #e3e6ec;padding-bottom:4px}.tag{display:inline-block;padding:2px 8px;border-radius:10px;
font-size:12px;background:#eef1f6;margin-right:6px}.immediate{background:#fde2e1;color:#9b1c1c}
.medium{background:#fff1d6;color:#8a5300}.low{background:#e3f4e8;color:#1e6b38}blockquote{margin:8px 0;
padding:8px 12px;background:#f7f8fa;border-left:3px solid #9aa4b5}table{border-collapse:collapse;width:100%;
font-size:13px}td,th{text-align:left;padding:4px 6px;border-bottom:1px solid #eef0f4;vertical-align:top}
.muted{color:#6b7385;font-size:13px}"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v))


_NO_QUOTE = "Exact words not found: please read this section of the study documents."


def _also(study, ev: dict) -> str:
    parts = [f"""<p class="muted">{_e(a['where'])}</p><blockquote>{_e(a['doc_quote'] or _NO_QUOTE)}</blockquote>
<p class="muted">Site wrote: "{_e(a['message_quote'])}"</p>""" for a in also_broken(study, ev)]
    return f"<h2>It also goes against this rule</h2>{''.join(parts)}" if parts else ""


def render_item(item: dict) -> str:
    s = config.study(item["study_id"])
    ev = item["evidence"]
    hist = items.history(item["id"])
    rows = "".join(
        f"<tr><td>{_e(local_time(h['ts']))}</td><td>{_e(h['actor'])}</td><td>{_e(h['action'])}</td></tr>"
        for h in hist)
    event = ev.get("event") or {}
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Item #{item['id']}</title>
<style>{CSS}</style></head><body>
<p class="muted">{_e(s.title)} ({_e(s.study_id)})</p>
<h1>Item #{item['id']}: {_e(item['title'])}</h1>
<p><span class="tag {item['tier']}">{_e(TIER_LABELS.get(item['tier'], item['tier']))}</span>
<span class="tag">{_e(item['state'])}</span><span class="tag">{_e(s.site_name(item['site']))}</span>
<span class="tag">{_e(item['participant_id'] or 'participant not named')}</span></p>
<h2>What the site wrote</h2>
<p class="muted">{_e(ev.get('sender_name'))}, {_e(local_time(ev.get('message_date')))}, via {_e(ev.get('channel'))}
 ({_e(ev.get('visibility'))}) · subject: {_e(ev.get('message_subject'))}</p>
<blockquote>{_e(ev.get('message_quote'))}</blockquote>
<p class="muted">Timing: {_e(event.get('temporality'))}. "{_e(event.get('temporality_evidence'))}"</p>
<h2>What the study rule says</h2>
<p class="muted">{_e(ev.get('doc_title'))}, page {_e(ev.get('doc_page'))} · rule {_e(ev.get('rule_id'))}</p>
<blockquote>{_e(ev.get('doc_quote') or _NO_QUOTE)}</blockquote>
<details><summary class="muted">Surrounding passage in the original document</summary>
<blockquote>{_e(ev.get('doc_passage'))}</blockquote></details>
{_also(s, ev)}
<h2>Why it was flagged</h2>
<p>{_e(item.get('reasoning'))}</p>
<p class="muted">Check: {_e(ev.get('method'))} · certainty: {_e(ev.get('certainty'))} ·
confirmed against the original document: {_e(ev.get('confirmation'))}</p>
<p class="muted">Tier reason: {_e(item['tier_reason'])} · recipient: {_e(item['recipient_person'])}
({_e(item['recipient_reason'])})</p>
<h2>History</h2><table><tr><th>When</th><th>Who</th><th>What</th></tr>{rows}</table>
<p class="muted">This monitor does not review safety information.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Robots-Tag", "noindex")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, "ok", "text/plain")
        if self.path.startswith("/items/"):
            token = self.path.split("/items/", 1)[1].split("?")[0]
            row = db.q1("SELECT id FROM items WHERE link_token=?", (token,))
            if row:
                return self._send(200, render_item(items.get(row["id"])))
        return self._send(404, "not found", "text/plain")


def start_background() -> ThreadingHTTPServer | None:
    if not settings.get("web.enabled", True):
        return None
    server = ThreadingHTTPServer((settings.get("web.host", "0.0.0.0"), int(settings.get("web.port", 8080))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

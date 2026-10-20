"""Read-only item and answer detail pages, reached only through the unguessable link in a draft or a reply."""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, items, senders, settings, weblogin
from .drafting import also_broken, local_time, sender_ctx
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
<p class="muted">{_e(sender_ctx(study, ev)['wrote'])}: "{_e(a['message_quote'])}"</p>""" for a in also_broken(study, ev)]
    return f"<h2>It also goes against this rule</h2>{''.join(parts)}" if parts else ""


_VERDICT = {"confirmed": "it agreed: the message does go against this rule.",
            "unsure": "it was not sure the message goes against this rule.",
            "rejected": "it did not agree."}


def _how(item: dict, ev: dict) -> str:
    """How the bot reached the finding, step by step, from what it saved while checking (no new AI call)."""
    steps = [f"<b>What it read in the message.</b> \"{_e(ev.get('message_quote'))}\""
             + (f" It also used an earlier message about this participant ({_e(ev['earlier'].get('date'))}, "
                f"\"{_e(ev['earlier'].get('subject'))}\")." if isinstance(ev.get("earlier"), dict) else "")]
    first = db.q1("SELECT channel, from_addr, headers FROM messages WHERE id=?", (item.get("first_message_id"),))
    if first and senders.failed(dict(first, headers=db.unjs(first["headers"], {}))):
        steps[0] += (f" <b>Warning: the email's sender check failed</b>, so it may not really be from "
                     f"{_e(first['from_addr'])}. It was still checked for problems, but a request in it to clear or hide an alert is ignored.")
    key = ev.get("detail") or ev.get("code_fact")
    dates = ev.get("date_notes") or []
    if key or dates:
        steps.append("<b>What code worked out</b> (dates, gaps and times are counted by code, not by the AI)."
                     + (f" {_e(key)}" if key else "")
                     + (f"<details><summary class=\"muted\">All the dates code worked out for this message "
                        f"({len(dates)})</summary><ul>" + "".join(f"<li>{_e(d)}</li>" for d in dates)
                        + "</ul></details>" if dates else ""))
    steps.append(f"<b>The rule it compared with.</b> {_e(ev.get('doc_title'))}: "
                 f"\"{_e(ev.get('doc_quote') or _NO_QUOTE)}\"")
    steps.append(f"<b>Its reasoning.</b> {_e(ev.get('why') or item.get('reasoning'))}")
    if ev.get("verdict") or ev.get("confirmation"):
        check = _VERDICT.get(ev.get("verdict"), "")
        if ev.get("kept_by_code"):
            check = "it did not agree, but the finding was kept because code worked out the numbers behind it."
        steps.append(f"<b>A second check.</b> A separate AI check reread the message and the protocol, and "
                     f"{_e(check)} {_e(ev.get('confirmation'))}")
    sure = ev.get("certainty") == "clear"
    steps.append("<b>How sure it is.</b> " + ("Sure: the protocol's exact words were found and the second check "
                                              "agreed." if sure else f"Not sure: {_e(ev.get('unsure_reason') or 'please check it.')}"))
    steps.append(f"<b>Why this urgency.</b> {_e(TIER_LABELS.get(item['tier'], item['tier']))}: "
                 f"{_e(item['tier_reason'])}")
    if ev.get("next_step"):
        steps.append(f"<b>Next step.</b> {_e(ev['next_step'])}")
    return "<h2>How the bot reached this</h2><ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>"


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
{_how(item, ev)}
<p class="muted">Check: {_e(ev.get('method'))} · recipient: {_e(item['recipient_person'])}
({_e(item['recipient_reason'])})</p>
<h2>History</h2><table><tr><th>When</th><th>Who</th><th>What</th></tr>{rows}</table>
<p class="muted">This monitor does not review safety information.</p>
</body></html>"""


def _answer_how(a: dict) -> str:
    """How the answer was reached, from what the bot saved while answering (no new AI call)."""
    li = lambda xs: "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in xs) + "</ul>"
    steps = [f"<b>What it read.</b> {li(a.get('read') or a.get('picked') or ['no sections'])}"]
    if a.get("participant"):
        steps.append(f"<b>What it knew about {_e(a['participant'])}</b> (from earlier messages): "
                     + li(a.get("record") or ["no dates on record"]))
    if a.get("windows"):
        steps.append("<b>Visit windows worked out by code</b> (not by the AI): " + li(a["windows"]))
    steps.append("<b>Checks.</b> " + ("Every number in the answer was found in the protocol's words, the record or "
                                      "a sum worked out by code." if a.get("traced") else
                                      "Not every number could be checked, so the reply says it is not sure."))
    if a.get("ask"):
        steps.append(f"<b>It asked back.</b> The documents could not settle this without a fact only you can give, "
                     f"so it asked: \"{_e(a['ask'])}\" Reply in the same chat and it will answer with it.")
    return "<p><b>How the answer was reached</b></p><ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>"


def render_answers(page: dict) -> str:
    s = config.study(page["study_id"])
    blocks = []
    for a in json.loads(page["answers"]):
        quotes = "".join(f'<p class="muted">{_e(h)}</p><blockquote>{_e(w)}</blockquote>' for h, w in a["quotes"])
        sums = "".join(f"<li>{_e(what)}: {_e(r)}</li>" for what, r in a.get("sums", []))
        blocks.append(f"""<h2>{_e(a['question'])}</h2>
<p><b>Reply posted:</b> {_e(a.get('plain') or a['text'])}</p>
<p><b>Full answer:</b> {_e(a['text'])}</p>
{f'<p><b>Worked out by code</b></p><ul>{sums}</ul>' if sums else ''}
<p><b>The protocol's words</b> (found word for word in the documents)</p>
{quotes or '<p class="muted">None found.</p>'}
{_answer_how(a)}""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Answer details</title>
<style>{CSS}</style></head><body>
<p class="muted">{_e(s.title)} ({_e(s.study_id)}) · {_e(local_time(page['created_at']))}</p>
<h1>Answer details</h1>
<h2>The question as asked</h2><blockquote>{_e(page['message_text'])}</blockquote>
{''.join(blocks)}
<p class="muted">From the study documents. Please confirm with your CRA before acting on it.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Cache-Control", "private, no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _gated(self, study_id: str, what: str, ref) -> bool:
        """True when a sign-in redirect or a refusal was sent instead of the page."""
        stop = weblogin.gate(self.path, self.headers.get("Cookie"), study_id, what, ref)
        if stop:
            self._send(stop[0], stop[2], "text/plain; charset=utf-8", stop[1])
        return bool(stop)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, "ok", "text/plain")
        if self.path.startswith("/auth/callback") and weblogin.enabled():
            code, headers, body = weblogin.callback(self.path)
            return self._send(code, body, "text/plain; charset=utf-8", headers)
        if self.path.startswith("/auth/logout"):
            code, headers, body = weblogin.logout()
            return self._send(code, body, "text/plain; charset=utf-8", headers)
        if self.path.startswith("/items/"):
            token = self.path.split("/items/", 1)[1].split("?")[0]
            row = db.q1("SELECT id, study_id FROM items WHERE link_token=?", (token,))
            if row:
                if self._gated(row["study_id"], "item", row["id"]):
                    return
                return self._send(200, render_item(items.get(row["id"])))
        if self.path.startswith("/answers/"):
            token = self.path.split("/answers/", 1)[1].split("?")[0]
            as_file = token.endswith(".md")
            row = db.q1("SELECT * FROM answer_pages WHERE link_token=?", (token.removesuffix(".md"),))
            if row and self._gated(row["study_id"], "answer_page", row["id"]):
                return
            if row and as_file:
                from . import answers
                return self._send(200, answers.facts_file(dict(row), config.study(row["study_id"]).title),
                                  "text/markdown; charset=utf-8")
            if row:
                return self._send(200, render_answers(dict(row)))
        return self._send(404, "not found", "text/plain")


def start_background() -> ThreadingHTTPServer | None:
    if not settings.get("web.enabled", True):
        return None
    server = ThreadingHTTPServer((settings.get("web.host", "0.0.0.0"), int(settings.get("web.port", 8080))), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

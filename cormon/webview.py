"""Item and answer detail pages, reached only through the unguessable link in a draft or a reply, and the admin
pages under /admin (adminview.py). Read-only except an admin's corrections to patient memory and, with
features.close_on_page, closing an alert on its page (closing.py), and, with features.ack_on_page, recording who
answered the phone for a safety alert (safety.py)."""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import config, db, items, senders, settings, weblogin
from .compliance import SAFETY_NOTICE
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
    if careful := _careful(item.get("id")):
        steps.append(f"<b>Careful with doctors.</b> {careful}")
    return "<h2>How the bot reached this</h2><ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>"


_DECIDED = {"send": "posted it", "reword": "reworded it calmly", "withhold": "did not post it"}


def _careful(item_id) -> str:
    """What the careful check (careful.py) decided for this item's chat posts, and who was told privately."""
    said = []
    for r in db.q("SELECT action, details FROM audit_log WHERE entity_type='item' AND entity_id=? AND action IN"
                  " ('careful_check','reported_privately') ORDER BY id", (str(item_id),)):
        d = db.unjs(r["details"], {})
        if r["action"] == "careful_check":
            said.append(f"In {_e(d.get('room') or 'the chat')} were {_e(d.get('present'))}, so the bot "
                        f"{_DECIDED.get(d.get('decision'), _e(d.get('decision')))}: {_e(d.get('reason'))}."
                        + ("" if d.get("members_complete", True) else " (The chat's member list could not be read, "
                                                                        "so only people who had written there were known.)"))
        elif d.get("already_emailed"):
            said.append(f"{_e(d.get('to'))} already had this alert by email.")
        else:
            said.append(f"Sent privately to {_e(d.get('to'))} by email.")
    return " ".join(said)


def _closed_as(c: dict) -> str:
    from . import closing
    said = f"<p class=\"muted\">{_e(c['by_name'])} chose <b>{_e(closing.CHOICES.get(c['choice'], c['choice']))}</b>" \
           + (f", done {_e(local_time(c['done_at']))}" if c["done_at"] else "") + f" (on {_e(local_time(c['created_at']))})"
    said += f": \"{_e(c['details'])}\"</p>" if c["details"] else ".</p>"
    if c["reviewed_by"]:
        said += (f"<p class=\"muted\">Reviewed by {_e(c['reviewed_by'])} on {_e(local_time(c['reviewed_at']))}: "
                 f"<b>{_e({'approved': 'approved', 'sent_back': 'sent back', 'reported': 'reported to the sponsor'}.get(c['status'], c['status']))}</b>"
                 + (f". \"{_e(c['review_note'])}\"" if c["review_note"] else "") + "</p>")
    return said


def _radios(name: str, options: dict) -> str:
    return "<p>" + "<br>".join(f'<label><input type="radio" name="{name}" value="{k}" required> {_e(v)}</label>'
                               for k, v in options.items()) + "</p>"


def _closing_part(item: dict, email: str | None, path: str) -> str:
    """Close this alert (closing.py): the three choices, or the second person's review, for whoever may."""
    from . import closing
    if not closing.on() or item["kind"] != "finding":
        return ""
    s = config.study(item["study_id"])
    c = closing.latest(item["id"])
    record = _closed_as(c) if c else ""
    if item["state"] not in ("open", "waiting"):
        return f"<h2>How it was closed</h2>{record}" if record else ""
    head = "<h2>Close this alert</h2>" if item["state"] == "open" else \
        f"<h2>Waiting for a second person's review</h2>{record}"
    token = item["link_token"]
    if not email:
        if not weblogin.ready():
            return head + '<p class="muted">Closing an alert here needs Google sign-in, which is not set up here.</p>'
        return head + f'<p><a href="{_e(weblogin.sign_in_url(path))}">Sign in with Google</a> to close this alert.</p>'
    if item["state"] == "open":
        if not closing.person(s, email, "close"):
            return head + f'<p class="muted">You are signed in as {_e(email)}, who may not close alerts here.</p>'
        return head + f"""{f'<p class="muted">Earlier:</p>{record}' if record else ''}
<form method="post" action="/items/{_e(token)}/close"><input type="hidden" name="step" value="check">
{_radios("choice", closing.CHOICES)}
<p><label>When it was done (your local time; not needed for "Nothing can be done")<br>
<input type="datetime-local" name="done_at"></label></p>
<p><label>What was done, or why nothing can be done (needed unless you did what was asked)<br>
<textarea name="details" rows="3" cols="70" maxlength="2000"></textarea></label></p>
<p><button>Check before closing</button></p></form>"""
    if not closing.person(s, email, "review"):
        return head + '<p class="muted">A person who reviews alerts in this study will look at it.</p>'
    if c and c["by_email"] == email.lower():
        return head + '<p class="muted">You closed it, so a second person must review it.</p>'
    return head + f"""<form method="post" action="/items/{_e(token)}/review"><input type="hidden" name="step" value="check">
{_radios("decision", closing.REVIEWS)}
<p><label>Your note (needed unless you approve)<br>
<textarea name="note" rows="3" cols="70" maxlength="2000"></textarea></label></p>
<p><button>Check before saving</button></p></form>"""


def _ack_part(item: dict, email: str | None, path: str) -> str:
    """Record who answered the phone for a safety alert (safety.py), for whoever may."""
    from . import safety
    if not safety.page_on() or item["kind"] != "out_of_scope":
        return ""
    if item.get("acked_at"):
        return (f"<h2>Acknowledged</h2><p class=\"muted\">By {_e(item['acked_by'])}, "
                f"{_e(local_time(item['acked_at']))}.</p>")
    s = config.study(item["study_id"])
    head = "<h2>Record who answered the phone</h2>"
    if not email:
        if not weblogin.ready():
            return head + '<p class="muted">Recording it here needs Google sign-in, which is not set up here.</p>'
        return head + (f'<p><a href="{_e(weblogin.sign_in_url(path))}">Sign in with Google</a> to record that the '
                       f'medical monitor or the safety backup has it.</p>')
    if not safety.may_record(s, email):
        return head + f'<p class="muted">You are signed in as {_e(email)}, who may not record it here.</p>'
    who = {p.id: safety.label(s, p) for p in safety.ackers(s, item)}
    if not who:
        return head + '<p class="muted">Nobody on the study list can acknowledge a safety message.</p>'
    return head + f"""<form method="post" action="/items/{_e(item['link_token'])}/ack">
<input type="hidden" name="step" value="check"><p>Who answered the phone and has it now?</p>
{_radios("person", who)}
<p><label>Note (for example the time of the call)<br>
<textarea name="note" rows="2" cols="70" maxlength="500"></textarea></label></p>
<p><button>Check before saving</button></p></form>"""


def render_confirm(item: dict, lines: list[str], action: str, sealed: str) -> str:
    """The confirm step (DSP-02): exactly what will be kept, before it counts."""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Check: item #{item['id']}</title>
<style>{CSS}</style></head><body>
<h1>Please check: item #{item['id']}: {_e(item['title'])}</h1>
<ul>{''.join(f'<li>{_e(x)}</li>' for x in lines)}</ul>
<p>By confirming, you state that this is true. Your name, the time and your words are kept in the alert's record.</p>
<form method="post" action="/items/{_e(item['link_token'])}/{action}"><input type="hidden" name="step" value="confirm">
<input type="hidden" name="sealed" value="{_e(sealed)}"><p><button>Confirm</button>
<a href="/items/{_e(item['link_token'])}">Go back</a></p></form></body></html>"""


def render_problem(item: dict, why: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Not saved</title><style>{CSS}</style></head>
<body><h1>Not saved</h1><p>{_e(why[:1].upper() + why[1:])}.</p>
<p><a href="/items/{_e(item['link_token'])}">Back to item #{item['id']}</a></p></body></html>"""


def render_item(item: dict, email: str | None = None, path: str = "") -> str:
    s = config.study(item["study_id"])
    ev = item["evidence"]
    hist = items.history(item["id"])
    rows = "".join(
        f"<tr><td>{_e(local_time(h['ts']))}</td><td>{_e(h['actor'])}</td><td>{_e(h['action'])}</td></tr>"
        for h in hist)
    event = ev.get("event") or {}
    warn = "".join(f"<p><b>Check the sender:</b> {_e(f)}.</p>" for f in senders.item_flags(item, s))
    first = db.q1("SELECT sent_at, received_at FROM messages WHERE id=?", (item.get("first_message_id"),))
    times = (f"sent {_e(local_time(first['sent_at'] or ev.get('message_date')))} (the sender's own clock, not checked)"
             f" · received by the bot {_e(local_time(first['received_at']))}") if first else \
        _e(local_time(ev.get("message_date")))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Item #{item['id']}</title>
<style>{CSS}</style></head><body>
<p class="muted">{_e(s.title)} ({_e(s.study_id)})</p>
<h1>Item #{item['id']}: {_e(item['title'])}</h1>
<p><span class="tag {item['tier']}">{_e(TIER_LABELS.get(item['tier'], item['tier']))}</span>
<span class="tag">{_e(item['state'])}</span><span class="tag">{_e(s.site_name(item['site']))}</span>
<span class="tag">{_e(item['participant_id'] or 'participant not named')}</span></p>
{warn}
<h2>What the site wrote</h2>
<p class="muted">{_e(ev.get('sender_name'))}, {times}, via {_e(ev.get('channel'))}
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
{_closing_part(item, email, path or f"/items/{item['link_token']}")}
{_ack_part(item, email, path or f"/items/{item['link_token']}")}
<h2>History</h2><table><tr><th>When</th><th>Who</th><th>What</th></tr>{rows}</table>
<p class="muted">This monitor does not review safety information. {SAFETY_NOTICE}</p>
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
        earlier = "".join(f"<li>{_e(x)}</li>" for x in a.get("from_record") or [])
        blocks.append(f"""<h2>{_e(a['question'])}</h2>
<p><b>Reply posted:</b> {_e(a.get('plain') or a['text'])}</p>
<p><b>Full answer:</b> {_e(a['text'])}</p>
{f'<p><b>Worked out by code</b></p><ul>{sums}</ul>' if sums else ''}
<p><b>The protocol's words</b> (found word for word in the documents)</p>
{quotes or '<p class="muted">None found.</p>'}
{f'<p><b>From earlier messages</b> (what the bot remembers about the participant)</p><ul>{earlier}</ul>' if earlier else ''}
{_answer_how(a)}""")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Answer details</title>
<style>{CSS}</style></head><body>
<p class="muted">{_e(s.title)} ({_e(s.study_id)}) · {_e(local_time(page['created_at']))}</p>
<h1>Answer details</h1>
<h2>The question as asked</h2><blockquote>{_e(page['message_text'])}</blockquote>
{''.join(blocks)}
<p class="muted">From the study documents. Please confirm with your CRA before acting on it.</p>
</body></html>"""


ADMIN_HEADERS = {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none';"
                                            " base-uri 'none'",
                 "X-Content-Type-Options": "nosniff"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body if isinstance(body, bytes) else body.encode()
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

    def _admin(self):
        from . import adminview
        is_file = self.path.startswith("/admin/static/")
        stop, email = weblogin.admin_gate(self.path, self.headers.get("Cookie"), log=not is_file)
        if stop:
            return self._send(stop[0], stop[2], "text/plain; charset=utf-8", stop[1])
        if is_file:
            found = adminview.static(self.path)
            return self._send(200, found[0], found[1], ADMIN_HEADERS) if found else self._send(404, "not found",
                                                                                                  "text/plain")
        if self.path.startswith("/admin/api/"):
            data = adminview.api(self.path)
            if data is None:
                return self._send(404, json.dumps({"error": "not found"}), "application/json", ADMIN_HEADERS)
            return self._send(200, json.dumps(data), "application/json", ADMIN_HEADERS)
        return self._send(200, adminview.shell(email), headers=ADMIN_HEADERS)

    def _same_site(self) -> bool:
        """A change must come from the admin app itself: its own header (a form or a link on another site cannot
        send one without asking first, and nothing here allows it), JSON, and no other site's Origin."""
        origin = self.headers.get("Origin")
        base = (settings.get("web.base_url") or "").rstrip("/")
        return (self.headers.get("X-Cormon-Admin") == "1"
                and (self.headers.get("Content-Type") or "").startswith("application/json")
                and (not origin or origin.rstrip("/") == base))

    def _closing_post(self, token: str, action: str):
        """The close and review forms on an alert's page (closing.py). Only the signed-in person may act, from
        this site (the session cookie is SameSite=Lax, and another site's Origin is refused); a check step comes
        first, and what it showed is sealed into the confirm form, so the confirm saves exactly that."""
        from . import closing
        row = db.q1("SELECT id, study_id FROM items WHERE link_token=?", (token,))
        if not row or not closing.on():
            return self._send(404, "not found", "text/plain")
        item = items.get(row["id"])
        study = config.study(item["study_id"])
        email = weblogin.signed_in(self.headers.get("Cookie"))
        origin = (self.headers.get("Origin") or "").rstrip("/")
        size = int(self.headers.get("Content-Length") or 0)
        if (origin and origin != (settings.get("web.base_url") or "").rstrip("/")) or not 0 < size <= 20000:
            db.audit("closing_refused", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     email=email, why="not from the alert's page")
            return self._send(403, render_problem(item, "this form did not come from the alert's page"))
        if not email or not weblogin.may_see(email, study.study_id):
            return self._send(403, render_problem(item, "please sign in with Google first, from the alert's page"))
        form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
        f = lambda k: (form.get(k) or [""])[0]
        try:
            if f("step") == "confirm":
                got = weblogin.unseal(f("sealed"))
                if not got or got.get("email") != email or got.get("item") != item["id"] or got.get("action") != action:
                    raise ValueError("this check has run out of time or is not yours. Please start again")
                if action == "close":
                    closing.close(item["id"], email, got["choice"], got["done_at"], got["details"])
                else:
                    closing.review(item["id"], email, got["decision"], got["note"])
                return self._send(303, "", "text/plain", {"Location": f"/items/{token}"})
            if action == "close":
                c = closing.check_close(item, study, email, f("choice"), f("done_at"), f("details"))
                kept = {"choice": c["choice"], "done_at": f("done_at"), "details": c["details"]}
                lines = [f"Your choice: {closing.CHOICES[c['choice']]}"] \
                    + ([f"When it was done: {local_time(c['done_at'])}"] if c["done_at"] else []) \
                    + ([f"What you wrote: {c['details']}"] if c["details"] else []) \
                    + ["The alert closes." if c["choice"] == "done" else "It goes to a second person for review."]
            else:
                r = closing.check_review(item, study, email, f("decision"), f("note"))
                kept = {"decision": r["decision"], "note": r["note"]}
                lines = [f"Your decision: {closing.REVIEWS[r['decision']]}"] \
                    + ([f"Your note: {r['note']}"] if r["note"] else [])
            sealed = weblogin.seal(dict(kept, email=email, item=item["id"], action=action), minutes=15)
            return self._send(200, render_confirm(item, [f"Signed in as: {email}"] + lines, action, sealed))
        except closing.NotAllowed as e:
            db.audit("closing_refused", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     email=email, why=str(e))
            return self._send(403, render_problem(item, str(e)))
        except (ValueError, KeyError, items.StepOrder) as e:
            return self._send(400, render_problem(item, str(e).strip("'")))

    def _ack_post(self, token: str):
        """The phone acknowledgment form on a safety alert's page (safety.py): the same guards and check step as
        the close form."""
        from . import safety
        row = db.q1("SELECT id, study_id FROM items WHERE link_token=?", (token,))
        if not row or not safety.page_on():
            return self._send(404, "not found", "text/plain")
        item = items.get(row["id"])
        study = config.study(item["study_id"])
        email = weblogin.signed_in(self.headers.get("Cookie"))
        origin = (self.headers.get("Origin") or "").rstrip("/")
        size = int(self.headers.get("Content-Length") or 0)
        if (origin and origin != (settings.get("web.base_url") or "").rstrip("/")) or not 0 < size <= 20000:
            db.audit("safety_ack_page_refused", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     email=email, why="not from the alert's page")
            return self._send(403, render_problem(item, "this form did not come from the alert's page"))
        if not email or not weblogin.may_see(email, study.study_id):
            return self._send(403, render_problem(item, "please sign in with Google first, from the alert's page"))
        form = parse_qs(self.rfile.read(size).decode("utf-8", "replace"))
        f = lambda k: (form.get(k) or [""])[0]
        try:
            if f("step") == "confirm":
                got = weblogin.unseal(f("sealed"))
                if not got or got.get("email") != email or got.get("item") != item["id"] or got.get("action") != "ack":
                    raise ValueError("this check has run out of time or is not yours. Please start again")
                a = safety.check_phone_ack(item, study, email, got["person"], got["note"])
                safety.acknowledge(item["id"], a["who"], "phone", actor_type="page", note=a["note"],
                                   recorded_by=email)
                return self._send(303, "", "text/plain", {"Location": f"/items/{token}"})
            a = safety.check_phone_ack(item, study, email, f("person"), f("note"))
            sealed = weblogin.seal({"person": a["person"], "note": a["note"], "email": email, "item": item["id"],
                                    "action": "ack"}, minutes=15)
            return self._send(200, render_confirm(item, [f"Signed in as: {email}", f"Who answered the phone and has "
                                                          f"it: {a['label']}", f"Note: {a['note']}",
                                                         "The safety contact list stops here."], "ack", sealed))
        except safety.NotAllowed as e:
            db.audit("safety_ack_page_refused", entity_type="item", entity_id=item["id"], study_id=study.study_id,
                     email=email, why=str(e))
            return self._send(403, render_problem(item, str(e)))
        except (ValueError, KeyError) as e:
            return self._send(400, render_problem(item, str(e).strip("'")))

    def do_POST(self):
        from . import adminview
        if self.path.startswith("/items/"):
            token, _, action = self.path.split("/items/", 1)[1].split("?")[0].partition("/")
            if action in ("close", "review"):
                return self._closing_post(token, action)
            if action == "ack":
                return self._ack_post(token)
        if self.path != "/admin/api/memory/change":
            return self._send(404, "not found", "text/plain")
        stop, email = weblogin.admin_gate(self.path, self.headers.get("Cookie"), log=False)
        if stop:
            return self._send(stop[0], stop[2], "text/plain; charset=utf-8", stop[1])
        size = int(self.headers.get("Content-Length") or 0)
        if not self._same_site() or not 0 < size <= 20000:
            db.audit("admin_refused", entity_type="web", entity_id=self.path, email=email, why="not from the admin page")
            return self._send(403, json.dumps({"error": "refused"}), "application/json", ADMIN_HEADERS)
        try:
            result = adminview.memory_change(json.loads(self.rfile.read(size)), email)
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, json.dumps({"error": str(e)}), "application/json", ADMIN_HEADERS)
        except Exception as e:
            return self._send(502, json.dumps({"error": f"Patient memory could not be changed: {str(e)[:200]}"}),
                              "application/json", ADMIN_HEADERS)
        return self._send(200, json.dumps(result), "application/json", ADMIN_HEADERS)

    def do_GET(self):
        if self.path == "/health":
            from . import watchdog
            age = watchdog.minutes_since_heartbeat()
            if watchdog.stalled():
                return self._send(503, "stalled: " + ("no heartbeat yet" if age is None else
                                                      f"last heartbeat {age:.0f} minutes ago"), "text/plain")
            return self._send(200, "ok", "text/plain")
        if self.path.startswith("/auth/callback") and weblogin.ready():
            code, headers, body = weblogin.callback(self.path)
            return self._send(code, body, "text/plain; charset=utf-8", headers)
        if self.path.startswith("/auth/logout"):
            code, headers, body = weblogin.logout()
            return self._send(code, body, "text/plain; charset=utf-8", headers)
        if self.path == "/admin" or self.path.startswith(("/admin/", "/admin?")):
            return self._admin()
        if self.path.startswith("/items/"):
            token = self.path.split("/items/", 1)[1].split("?")[0]
            row = db.q1("SELECT id, study_id FROM items WHERE link_token=?", (token,))
            if row:
                if self._gated(row["study_id"], "item", row["id"]):
                    return
                return self._send(200, render_item(items.get(row["id"]), weblogin.signed_in(self.headers.get("Cookie")),
                                                   self.path))
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

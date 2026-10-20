"""Is an email really from the address it shows? Gmail checks SPF, DKIM and DMARC when mail arrives and
writes the results in an Authentication-Results header at the top. Only the topmost one, written by a mail
server we trust, counts: a sender can put a fake one lower down.

A failed email is still read for problems (a forged report of a missed dose still deserves a look), but
nothing in it is acted on: it cannot clear or hide an alert."""

from __future__ import annotations

import re
from email.message import Message

from . import settings

FAILED = "fail"


def check(msg: Message) -> dict:
    """{"result": pass | fail | unknown, "dmarc", "spf", "dkim", "server"}."""
    found = msg.get_all("Authentication-Results") or []
    if not found:
        return {"result": "unknown", "why": "no Authentication-Results header"}
    top = " ".join(str(found[0]).split())
    server = top.split(";", 1)[0].strip().lower()
    if server not in [s.lower() for s in settings.get("mailbox.auth_servers", ["mx.google.com"])]:
        return {"result": "unknown", "server": server, "why": "the top result is not from a trusted mail server"}
    got: dict[str, list[str]] = {}
    for method, value in re.findall(r"\b(spf|dkim|dmarc)=([a-z]+)", top.lower()):
        got.setdefault(method, []).append(value)
    dmarc, spf, dkim = (got.get(m, []) for m in ("dmarc", "spf", "dkim"))
    if "fail" in dmarc:
        result = FAILED
    elif {"pass", "bestguesspass"} & set(dmarc):
        result = "pass"
    elif "pass" in dkim or "pass" in spf:
        result = "pass"
    elif {"fail", "softfail"} & set(spf) or "fail" in dkim:
        result = FAILED
    else:
        result = "unknown"
    return {"result": result, "server": server, "dmarc": ",".join(dmarc) or None, "spf": ",".join(spf) or None,
            "dkim": ",".join(dkim) or None}


def failed(msg: dict) -> bool:
    return msg.get("channel") == "email" and ((msg.get("headers") or {}).get("_cormon_sender_check") or {}) \
        .get("result") == FAILED

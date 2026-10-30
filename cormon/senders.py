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


def flags(msg: dict | None, study) -> list[str]:
    """What a reader should know about who sent a message: its sender check failed, or the sender is not on the
    study's people list. Shown on the item page and in the digest."""
    if not msg:
        return []
    out = []
    if failed(msg):
        out.append(f"the email's sender check failed, so it may not really be from {msg.get('from_addr')}")
    if addr := off_roster(msg, study):
        out.append(f"the sender ({addr}) is not on the study's people list")
    return out


def off_roster(msg: dict | None, study) -> str | None:
    """CAP-06: the sender's address when it is not on the study's people list. Such a message is kept and read,
    but nothing from it goes to the study's people or the sponsor. A sender with no address is not counted."""
    addr = (msg or {}).get("from_addr")
    if not addr or study is None or study.person_by_email(addr) is not None:
        return None
    if any((p.teams_upn or "").lower() == addr.lower() for p in study.people):
        return None
    return addr


def _first_message(item: dict) -> dict | None:
    from . import db
    m = db.q1("SELECT channel, from_addr, headers FROM messages WHERE id=?", (item.get("first_message_id"),))
    return dict(m, headers=db.unjs(m["headers"], {})) if m else None


def item_flags(item: dict, study) -> list[str]:
    return flags(_first_message(item), study)


def item_off_roster(item: dict, study=None) -> str | None:
    """The alert came from a sender not on the study's people list, so the sponsor does not see it (CAP-06)."""
    from . import config
    if study is None:
        study = config.studies().get(item.get("study_id"))
    return off_roster(_first_message(item), study)


def failed(msg: dict) -> bool:
    return msg.get("channel") == "email" and ((msg.get("headers") or {}).get("_cormon_sender_check") or {}) \
        .get("result") == FAILED

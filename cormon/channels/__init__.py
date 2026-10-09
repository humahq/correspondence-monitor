"""Channel adapters. Each has fetch() -> list[Inbound] and send(outbox_row) -> dict."""

from __future__ import annotations

from .. import settings

_cache: dict = {}


def email_channel():
    mode = settings.get("channels.email", "sim")
    if ("email", mode) not in _cache:
        if mode == "gmail":
            from .gmail import GmailChannel

            _cache[("email", mode)] = GmailChannel()
        elif mode == "imap":
            from .imap import ImapChannel

            _cache[("email", mode)] = ImapChannel()
        elif mode == "sim":
            from .sim import SimEmail

            _cache[("email", mode)] = SimEmail()
        else:
            _cache[("email", mode)] = None
    return _cache[("email", mode)]


def teams_channel():
    mode = settings.get("channels.teams", "sim")
    if ("teams", mode) not in _cache:
        if mode == "graph":
            from .teams import TeamsChannel

            _cache[("teams", mode)] = TeamsChannel()
        elif mode == "sim":
            from .sim import SimTeams

            _cache[("teams", mode)] = SimTeams()
        else:
            _cache[("teams", mode)] = None
    return _cache[("teams", mode)]

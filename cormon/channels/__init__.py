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


def study_for_person(addr: str | None) -> str | None:
    """The study a direct chat belongs to: the sender's study, or the only study there is."""
    from .. import config

    all_studies = list(config.studies().values())
    for s in all_studies:
        p = s.person_by_email(addr)
        if p is not None or (addr and any((x.teams_upn or "").lower() == addr for x in s.people)):
            return s.study_id
    return all_studies[0].study_id if len(all_studies) == 1 else None


def teams_channel():
    """The chat channel: Microsoft Teams (graph), Google Chat (gchat) or the simulator."""
    mode = settings.get("channels.teams", "sim")
    if ("teams", mode) not in _cache:
        if mode == "graph":
            from .teams import TeamsChannel

            _cache[("teams", mode)] = TeamsChannel()
        elif mode == "gchat":
            from .gchat import GoogleChatChannel

            _cache[("teams", mode)] = GoogleChatChannel()
        elif mode == "sim":
            from .sim import SimTeams

            _cache[("teams", mode)] = SimTeams()
        else:
            _cache[("teams", mode)] = None
    return _cache[("teams", mode)]

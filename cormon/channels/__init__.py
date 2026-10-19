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


def study_for_person(addr: str | None, known_only: bool = False) -> str | None:
    """The study a direct chat belongs to: the sender's study, or the only study there is (not with known_only:
    a group chat anyone can add the bot to is read only for people on a study)."""
    from .. import config

    from .. import db

    all_studies = list(config.studies().values())
    theirs = [s.study_id for s in all_studies if s.person_by_email(addr) is not None
              or (addr and any((x.teams_upn or "").lower() == addr for x in s.people))]
    if len(theirs) > 1:  # on several studies: the one whose group space they wrote in last
        last = db.q1("SELECT study_id FROM messages WHERE lower(from_addr)=? AND visibility='channel' AND study_id IN "
                     f"({','.join('?' * len(theirs))}) ORDER BY id DESC LIMIT 1", ((addr or "").lower(), *theirs))
        return last["study_id"] if last else theirs[0]
    if theirs:
        return theirs[0]
    return all_studies[0].study_id if len(all_studies) == 1 and not known_only else None


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

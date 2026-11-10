"""Who may hear what about a participant (config/privacy.yaml, and a study's own `privacy:` list), and the last check
before an answer that uses a participant's record goes out (Eliot, Nov 2).

Patient memory holds everything anyone reported, and an answer may use it. The control is on what is said: the
readers are everyone in the chat room the answer is posted in, or the person the email reply goes to. Each is placed
in a group by their study roles; anyone the study does not list, and anyone a room's member list could not show, is
`unknown`. When every reader may hear everything (the site care team) nothing is checked. Otherwise one AI call
reads the answer, with what the details page would show, against what each reader may hear. Anything one of them may
not hear is taken out and replaced by what is appropriate to say ("please have the site investigator review"),
without the private reason. The rewording must add no number and must pass the same check; when it does not, or the
check fails, the safe reply is sent instead. Every check is audited.

Outside the care team a participant is only their study ID (Eliot, Nov 4: the sponsor and CRO have no BAA with the
site). Health facts that bear on the study are fine for them by ID; but once the conversation has shown who the
participant is, the bot adds nothing about their health, so the check reads what the readers already saw.

An alert whose finding was read with patient memory's facts (reader.py) gets the same check for each chat room and
email it goes to (routing.route_item, careful.report_privately); one a reader may not hear is held for a person."""

from __future__ import annotations

import re

from . import conversation, db, llm, rooms, settings
from .config import Person, Study
from .settings import CODE_ROOT, CONFIG_DIR, load_yaml

SAFE_REPLY = ("Please ask the site investigator to review this participant's record; they can see everything "
              "reported about them.")

CHECK_SYSTEM = """You are the last check before a clinical-trial study monitor bot says something about a
participant. The bot remembers everything anyone reported, but each reader may hear only what their role needs.
You get the readers, grouped, with what each group may hear; what the conversation has already said in front of
them; the bot's reply; and the earlier facts about the participant that the reply and its details page rest on.
- ok: true when every reader may hear everything the reply says or lets them work out, and every earlier fact
  marked as shown to them. Earlier facts marked as context only are not shown: judge only what the reply itself
  says, including any details page it comes with; whatever the reply or that page repeats from an earlier fact is
  said to the readers. Study rules and dates from the documents, and what the person asking said in the message being answered,
  are always fine to repeat to that person.
- Who the participant is: to anyone outside the site care team a participant is only their study ID. Never their
  name, initials, date of birth, address, phone, record number, workplace, photo, relatives, or any other detail
  that could show who they are. Staff names (who reported something) are not about the participant.
- When the conversation shows that a reader outside the site care team can tell who this participant is (their
  name or such a detail was said there for the same participant), the reply must add nothing about the
  participant's health, medicines, results or care that the conversation has not already said. Study rules from
  the documents and what to do next are still fine. A name said for a different participant does not count.
- not_for: for each thing a reader may not hear, the group it is not for and the thing, in a few words; [] when ok.
- text: when not ok, the reply to send instead. Keep everything the readers may hear, and keep the help: say what
  they should do and who should look at it (for example "please ask the site investigator to review this
  participant's history before the next dose"), but never the private fact itself or a hint of it ("a sensitive
  condition", "a known infection"). Add no fact, number or advice of your own; no longer than the original; no
  greeting and no sign-off. "" when ok."""
CHECK_SCHEMA = llm.obj({"ok": {"type": "boolean"},
                        "not_for": {"type": "array", "items": llm.obj({"group": {"type": "string"},
                                                                       "what": {"type": "string"}})},
                        "text": {"type": "string"}})


def _file() -> dict:
    """The copy's own guide; a copy made before there was one (CORMON_HOME) uses the code's, so its readers are
    not all strangers."""
    path = next((p for p in (CONFIG_DIR / "privacy.yaml", CODE_ROOT / "config" / "privacy.yaml") if p.exists()), None)
    return load_yaml(path) if path else {}


def groups(study: Study) -> list[dict]:
    """The general groups, widest first, with the study's own added or changed by id; `enabled: false` drops one."""
    by_id = {g["id"]: dict(g) for g in _file().get("groups") or []}
    for g in (getattr(study, "raw", None) or {}).get("privacy") or []:
        by_id[g["id"]] = {**by_id.get(g["id"], {}), **g}
    return [g for g in by_id.values() if g.get("enabled", True) and g.get("roles")]


def unknown() -> dict:
    return {"id": "unknown", "label": "someone the study does not list", "may_hear": "Nothing about any participant.",
            **(_file().get("unknown") or {})}


def group_of(study: Study, addr: str | None) -> dict:
    """The widest group any of the person's roles is in; `unknown` for an address the study does not list."""
    p = study.person_by_email(addr)
    roles = set(p.roles) if p else set()
    return next((g for g in groups(study) if roles & set(g["roles"])), unknown())


def in_room(study: Study, space: str, also: list[str] = ()) -> list[tuple[str, dict]]:
    """(who, group) for a chat room's members and everyone who wrote there. A room whose member list could not be
    read adds an unknown reader."""
    room = rooms.members(space)
    addrs = sorted(room["emails"] | {a.lower() for a in also if a})
    out = [(a, group_of(study, a)) for a in addrs]
    return out + ([] if room["complete"] else [("others in the room the bot could not list", unknown())])


def readers(study: Study, msg: dict) -> list[tuple[str, dict]]:
    """(who, group) for everyone who will read the reply to this message: the chat room, or the email's sender."""
    m = re.fullmatch(r"teams:(spaces/[^/]+)/threads/[^/]+", msg.get("thread_key") or "")
    if m:
        return in_room(study, m.group(1), [msg.get("from_addr") or ""])
    sender = msg.get("from_addr")
    return [(sender, group_of(study, sender))] if sender else [("the reader", unknown())]


def _listed(study: Study, who: list[tuple[str, dict]]) -> str:
    by_group: dict[str, tuple[dict, list[str]]] = {}
    for addr, g in who:
        by_group.setdefault(g["id"], (g, []))[1].append(_name(study, addr))
    shared = " ".join((_file().get("shared") or "").split())
    return "\n".join(f"- {g['label']} ({', '.join(names)}): may hear: {' '.join(g['may_hear'].split())}"
                     + (f" Also: {shared}" if shared and g["id"] != "unknown" else "")
                     for g, names in by_group.values())


def details_shown(items: list[dict]) -> str:
    """What an alert's details page says in the AI's own words (webview._how), which the alert's readers can open."""
    return "\n".join(dict.fromkeys(str(t) for it in items for ev in [it.get("evidence") or {}]
                                   for t in (ev.get("why") or it.get("reasoning"), ev.get("confirmation"),
                                             ev.get("next_step")) if t))


def alert_problem(study: Study, body: str, facts: list[str], who: list[tuple[str, dict]],
                  use_cache: bool = True, details: str = "", before: str = "") -> dict | None:
    """For an alert whose finding was read with patient memory's facts: None when every reader may hear what it
    and its details page say, else {not_for, error}. Such an alert is held for a person rather than reworded,
    since the details page can't be reworded. A failed check holds it too. before: what the chat room has
    already seen (seen())."""
    if not facts or all(g.get("everything") for _, g in who):
        return None
    if outside := [a for a, g in who if g["id"] == "unknown"]:
        return {"not_for": [{"group": unknown()["label"], "what": f"anything about the participant ({', '.join(outside)})"}],
                "error": None}
    if details:
        body += f"\n\nITS DETAILS PAGE, WHICH THE READERS CAN OPEN, ALSO SAYS:\n{details}"
    try:
        res = _ask(body, facts, _listed(study, who), use_cache, shown=False, before=before)
    except Exception as e:
        return {"not_for": [], "error": str(e)[:300]}
    return None if res["ok"] else {"not_for": res.get("not_for") or [], "error": None}


def _name(study: Study, addr: str) -> str:
    p = study.person_by_email(addr)
    return f"{p.name} ({', '.join(study.role_label(r) for r in p.roles)})" if p and p.name else addr


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", s or ""))


def safe_reply() -> str:
    return _file().get("safe_reply") or SAFE_REPLY


def seen(msg: dict, participant: str | None) -> str:
    """The conversation the readers have already seen, ending with the message being answered (if any)."""
    turns = conversation.about(msg, participant) if msg.get("thread_key") else []
    if msg.get("body_text"):
        turns = turns + [{"who": msg.get("from_name") or msg.get("from_addr") or "someone",
                          "text": conversation._plain(msg["body_text"])}]
    return conversation.block(turns)


def _ask(said: str, facts: list[str], who: str, use_cache: bool, shown: bool = True, before: str = "") -> dict:
    """shown: the earlier facts are shown to the readers too (an answer's details page); else context only."""
    return llm.structured("privacy_check", CHECK_SYSTEM,
                          f"READERS:\n{who}\n\nTHE CONVERSATION SO FAR, AS THE READERS SAW IT:\n{before or '(nothing)'}"
                          f"\n\nTHE BOT'S REPLY:\n{said}\n\nEARLIER FACTS ABOUT THE PARTICIPANT ("
                          + ("shown to the readers on the details page" if shown else "context only, not shown")
                          + "):\n" + ("\n".join(f"- {f}" for f in facts) or "- none"), CHECK_SCHEMA, "privacy",
                          use_cache=use_cache)


def screen(study: Study, msg: dict, answers: list[dict], use_cache: bool = True) -> None:
    """Checks each answer that rests on a participant's record against who will read it, in place: a["plain"] is
    reworded (or the safe reply) when a reader may not hear part of it. When anyone outside the groups that may hear
    everything reads it, the details page keeps only the record lines the answer used, and none when reworded."""
    need = [a for a in answers if a.get("participant") and a.get("record")]
    if not need:
        return
    who = readers(study, msg)
    narrow = [(addr, g) for addr, g in who if not g.get("everything")]
    if not narrow:
        return
    listed = _listed(study, who)
    for a in need:
        a["record"] = list(a.get("from_record") or [])
        said = a.get("plain") or a.get("text") or ""
        facts = a["record"]
        before = seen(msg, a.get("participant"))
        try:
            res = _ask(said, facts, listed, use_cache, before=before)
        except Exception as e:
            res = {"ok": False, "not_for": [], "text": "", "error": str(e)[:300]}
        sent = said
        if not res["ok"]:
            new = " ".join((res.get("text") or "").split())
            fits = bool(new) and len(new) <= max(len(said), 200) and _numbers(new) <= _numbers(said)
            if fits:
                try:
                    fits = _ask(new, [], listed, use_cache, before=before)["ok"]
                except Exception:
                    fits = False
            sent = new if fits else safe_reply()
            a.update(plain=sent, text=sent, record=[], from_record=[], ask="", sums=[])
        db.audit("privacy_check", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
                 participant_id=a.get("participant"), question=a.get("question"),
                 readers=sorted({g["id"] for _, g in who}), ok=res["ok"], not_for=res.get("not_for") or [],
                 reworded=sent != said, safe_reply=sent == safe_reply() and sent != said, error=res.get("error"))


# Names in a safety email (switch: safety_names; Eliot, Nov 6): the medical monitor's own email may name the
# participant, as the site care team may; the CRA's copy and every escalation email may not. The site's words are
# quoted in those emails, so the names in them are found and taken out for everyone else.
NAMES_SYSTEM = """List every name in a clinical-trial site's message that names a participant (patient) or one of
their relatives or friends, exactly as written: full names, first names, surnames, nicknames and initials (for
example "Mr. Tanaka", "Jane", "J.T."). Leave out study and site staff, doctors, hospitals, companies, drugs and
participant study IDs (such as G-723-041). [] when there are none."""
NAMES_SCHEMA = llm.obj({"names": {"type": "array", "items": {"type": "string"}}})
NAME_REMOVED = "[name removed]"
QUOTE_WITHHELD = "(not quoted here, as it may name the participant: please open the full message)"


def may_see_names(study: Study, person: Person | None) -> bool:
    """Whether a safety email to this person may name the participant: the site care team and the roles in
    safety.names_for (the medical monitor)."""
    roles = set(person.roles) if person else set()
    care = next((g for g in groups(study) if g.get("everything")), {})
    return bool(roles & (set(care.get("roles") or []) | set(settings.get("safety.names_for") or ["medical_monitor"])))


def participant_names(study: Study, text: str, use_cache: bool = True) -> list[str] | None:
    """The names of participants and their relatives in a site's words, longest first; None when the check failed.
    A study person's name is never one of them."""
    if not (text or "").strip():
        return []
    try:
        found = llm.structured("participant_names", NAMES_SYSTEM, text[:6000], NAMES_SCHEMA, "names",
                               use_cache=use_cache)["names"]
    except Exception as e:
        db.audit("participant_names_failed", entity_type="llm", entity_id="participant_names", error=str(e)[:300])
        return None
    words = lambda s: re.findall(r"[^\W\d_]{2,}", s or "")  # noqa: E731
    staff = {w.lower() for p in study.people for w in words(p.name)} | {"mr", "mrs", "ms", "miss", "dr"}
    names = {" ".join(n.split()) for n in found if n and n.strip()}
    names = {n for n in names if not words(n) or not {w.lower() for w in words(n)} <= staff}   # "J.T." stays
    # each part on its own too ("Mr Tanaka" for "Mr. Tanaka", "Tanaka" later on); a staff name's part stays
    names |= {w for n in names for w in words(n) if len(w) >= 3 and w.lower() not in staff}
    return sorted(names, key=len, reverse=True)


def without_names(study: Study, ctx: dict, use_cache: bool = True) -> dict:
    """A safety email's context with the participant's names taken out of the site's quoted words and subject."""
    f = dict(ctx.get("finding") or {})
    quote, subject = f.get("message_quote") or "", f.get("message_subject") or ""
    names = participant_names(study, f"{subject}\n{quote}", use_cache)
    if names is None:
        f.update(message_quote=QUOTE_WITHHELD, message_subject="(subject not shown)")
    else:
        for n in names:
            pat = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, n.split())) + r"(?!\w)", re.I)
            quote, subject = pat.sub(NAME_REMOVED, quote), pat.sub(NAME_REMOVED, subject)
        f.update(message_quote=quote, message_subject=subject)
    return dict(ctx, finding=f)

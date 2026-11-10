"""Which language the bot answers in: courtesy in a shared chat (Eliot, Nov 6).

In a chat room the bot answers in a language everyone there reads. When someone writes in a language another
reader does not know, the bot says so kindly and answers in the shared language (`language.shared`, English).
In a 1-on-1 chat, or a room where everyone reads the writer's language, it answers in that language.

A person's languages are `languages:` on their entry in study.yaml; someone with none listed, someone the study
does not list, and anyone a room's member list could not show read only the shared language. The writer always
reads the language they wrote in.

The bot's replies are made in English and checked there. A reply in another language is that checked reply
translated at the end: the translation must keep every number and every link, or the English reply is sent."""
from __future__ import annotations

import re

from . import db, llm, privacy, settings
from .config import Study

CODES = {"en": "English", "ja": "Japanese", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
         "pt": "Portuguese", "zh": "Chinese", "ko": "Korean", "ru": "Russian", "cs": "Czech", "pl": "Polish",
         "nl": "Dutch", "hi": "Hindi", "ar": "Arabic", "he": "Hebrew", "tr": "Turkish", "uk": "Ukrainian"}
ENGLISH_WORDS = set("""the a an and or of to in is are was were be been for on at with from what when where which who how
why do does did can could should would will may i we you it this that these my our your please thanks hi hello yes no
not about after before has have had there their they he she his her if any pt pts patient participant subject
visit week wk day dose dosed labs lab study site screening baseline consent held hold missed today""".split())
NOTE = "Not everyone here reads {wrote}, so I'm answering in {reply}."

DETECT_SYSTEM = """Name the language a chat message to a clinical-trial assistant is written in, in English
("English", "Japanese", "Spanish"). Ignore names, codes, numbers and medical abbreviations. A message mixing
languages is in the one most of its words are in."""
DETECT_SCHEMA = llm.obj({"language": {"type": "string"}})

TRANSLATE_SYSTEM = """Translate a clinical-trial assistant's chat reply into {lang}. Keep its meaning exactly and
add nothing. Keep every number, date, participant ID, visit name, drug name, link and markup (<...>, *...*, _..._,
<b>, <br>) exactly as written. Return only the translation in text."""
TRANSLATE_SCHEMA = llm.obj({"text": {"type": "string"}})


def shared() -> str:
    return name(settings.get("language.shared") or "English")


def name(lang: str) -> str:
    """A language as one name: "ja", "japanese" and "Japanese" are all Japanese."""
    s = (lang or "").strip()
    return CODES.get(s.lower(), s[:1].upper() + s[1:].lower())


def _plain_english(text: str) -> bool | None:
    """True for text that is clearly English and False for text that clearly is not, by its letters and words;
    None when only the AI can tell."""
    t = re.sub(r"<[^>]+>|https?://\S+|@[\w.-]+|[A-Za-z-]*\d[A-Za-z0-9-]*", " ", text or "")
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return True
    if sum(c.isascii() for c in letters) < 0.8 * len(letters):
        return False
    words = re.findall(r"[a-z']+", t.lower())
    if sum(w in ENGLISH_WORDS for w in words) >= max(1, 0.15 * len(words)):
        return True
    return True if len(words) < 3 else None   # "ok", "thanks": too short to be anything but the shared language


def written_in(text: str, use_cache: bool = True) -> str:
    """The language a message is written in; the shared one when it can't be told."""
    return "English" if _plain_english(text) else _ask(text, use_cache)


def _ask(text: str, use_cache: bool) -> str:
    try:
        return name(llm.structured("language", DETECT_SYSTEM, text[:2000], DETECT_SCHEMA, "language",
                                   use_cache=use_cache)["language"]) or shared()
    except Exception as e:
        db.audit("language_check_failed", entity_type="llm", entity_id="language", error=str(e)[:300])
        return shared()


def reads(study: Study, addr: str, wrote_in: str, sender: str) -> set[str]:
    p = study.person_by_email(addr) if addr and "@" in addr else None
    langs = {name(x) for x in (p.languages if p else [])} or {shared()}
    return langs | ({wrote_in} if addr and addr.lower() == sender else set())


def choose(study: Study, msg: dict, wrote_in: str) -> tuple[str, str]:
    """(the language to answer in, the courtesy note to put first or "")."""
    sender = (msg.get("from_addr") or "").lower()
    every = [reads(study, a, wrote_in, sender) for a, _ in privacy.readers(study, msg)] or [{wrote_in}]
    if all(wrote_in in s for s in every):
        return wrote_in, ""
    common = set.intersection(*every)
    reply = shared() if shared() in common or not common else sorted(common)[0]
    return reply, ("" if reply == wrote_in else NOTE.format(wrote=wrote_in, reply=reply))


def _kept(original: str, translated: str) -> bool:
    nums = lambda s: sorted(re.findall(r"\d+(?:[.,]\d+)?", s))  # noqa: E731
    links = lambda s: set(re.findall(r"https?://[^\s|>\"]+", s))  # noqa: E731
    return bool(translated.strip()) and nums(original) == nums(translated) and links(original) <= links(translated)


def translate(text: str, lang: str, use_cache: bool = True) -> str | None:
    """The reply in another language, or None when the translation dropped or changed a number or a link."""
    try:
        out = llm.structured("translate", TRANSLATE_SYSTEM.format(lang=lang), text, TRANSLATE_SCHEMA, "text",
                             use_cache=use_cache)["text"].strip()
    except Exception as e:
        db.audit("translation_failed", entity_type="llm", entity_id="translate", error=str(e)[:300])
        return None
    return out if _kept(text, out) else None


def fit(study: Study, msg: dict, text: str, use_cache: bool = True) -> str:
    """The bot's English reply to a message, in the language its readers share (switch: language_courtesy)."""
    if not settings.feature("language_courtesy") or not text:
        return text
    wrote = written_in(msg.get("body_text") or "", use_cache)
    reply, note = choose(study, msg, wrote)
    english = f"{note}\n\n{text}" if note else text
    out = (translate(english, reply, use_cache) if reply != "English" else None) or english
    db.audit("language_chosen", entity_type="message", entity_id=msg.get("id"), study_id=study.study_id,
             written_in=wrote, wanted=reply, answered_in=reply if out is not english else "English", note=bool(note))
    return out

"""The language the bot answers in: courtesy in a shared chat (language.py, Eliot, Nov 6)."""

from types import SimpleNamespace

import pytest

from cormon import config, db, language, llm, privacy

PEOPLE = {"aiko@site.test": ["Japanese", "en"], "kenji@site.test": ["ja"], "cra@cro.test": []}
ROOM = {"teams:spaces/site/threads/1": ["aiko@site.test", "cra@cro.test"],
        "teams:spaces/jp/threads/1": ["aiko@site.test", "kenji@site.test"],
        "teams:spaces/dm/threads/1": ["cra@cro.test"]}


@pytest.fixture
def study(monkeypatch):
    monkeypatch.setenv("CORMON_TEST_FEATURES", "language_courtesy")
    monkeypatch.setattr(privacy, "readers", lambda s, m: [(a, {}) for a in ROOM[m["thread_key"]]])
    monkeypatch.setattr(db, "audit", lambda *a, **k: None)
    people = {e: config._person({"id": e, "email": e, "languages": langs}) for e, langs in PEOPLE.items()}
    return SimpleNamespace(study_id="S1", person_by_email=lambda e: people.get((e or "").lower()))


@pytest.fixture
def ai(monkeypatch):
    """The AI: it names Japanese, and translates by tagging the text (or, when told to, drops a number)."""
    said = {"drop": False, "calls": []}

    def structured(task, system, user, schema, name, use_cache=True):
        said["calls"].append(task)
        if task == "language":
            return {"language": "Japanese"}
        return {"text": "[ja] " + (user.replace("28", "") if said["drop"] else user)}
    monkeypatch.setattr(llm, "structured", structured)
    return said


def msg(thread, sender, text):
    return {"id": 1, "thread_key": thread, "from_addr": sender, "body_text": text, "channel": "teams"}


def test_plain_english_needs_no_ai_call(ai):
    assert language.written_in("@mira what is the visit window for Week 16?") == "English"
    assert language.written_in("thanks!") == "English"
    assert ai["calls"] == []
    assert language.written_in("第16週の来院期間は?") == "Japanese" and ai["calls"] == ["language"]


def test_a_room_where_someone_does_not_read_the_language_gets_the_shared_one_and_a_kind_note(study, ai):
    m = msg("teams:spaces/site/threads/1", "aiko@site.test", "第16週の来院期間は?")
    out = language.fit(study, m, "Day 112 ± 7 days.")
    assert out == "Not everyone here reads Japanese, so I'm answering in English.\n\nDay 112 ± 7 days."
    assert "translate" not in ai["calls"]


def test_a_room_where_everyone_reads_it_and_a_private_chat_get_the_writers_language(study, ai):
    for m in (msg("teams:spaces/jp/threads/1", "aiko@site.test", "第16週の来院期間は?"),
              msg("teams:spaces/dm/threads/1", "cra@cro.test", "第16週の来院期間は?")):   # the writer reads it
        assert language.fit(study, m, "Day 112 ± 7 days.") == "[ja] Day 112 ± 7 days."


def test_a_translation_that_changes_a_number_is_not_sent(study, ai):
    ai["drop"] = True
    m = msg("teams:spaces/jp/threads/1", "kenji@site.test", "4週目はいつですか?")
    assert language.fit(study, m, "Week 4 is Day 28 ± 7.") == "Week 4 is Day 28 ± 7."


def test_english_in_any_room_is_unchanged_and_the_switch_off_changes_nothing(study, ai, monkeypatch):
    m = msg("teams:spaces/site/threads/1", "cra@cro.test", "what is the window for week 4?")
    assert language.fit(study, m, "Day 28 ± 7.") == "Day 28 ± 7."
    monkeypatch.setenv("CORMON_TEST_FEATURES", "")
    jp = msg("teams:spaces/site/threads/1", "aiko@site.test", "第16週の来院期間は?")
    assert language.fit(study, jp, "Day 112 ± 7 days.") == "Day 112 ± 7 days." and ai["calls"] == []


def test_a_persons_languages_are_read_from_study_yaml():
    p = config._person({"id": "a", "email": "a@x", "languages": ["ja", " English ", ""]})
    assert p.languages == ["ja", "English"]
    assert language.name("ja") == "Japanese" and language.name("english") == "English"
    assert config._person({"id": "b"}).languages == []

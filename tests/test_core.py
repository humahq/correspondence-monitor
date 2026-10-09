"""Fast checks that need no network or model calls."""

import sqlite3

import pytest

from cormon import capture, db, drafting, matching, textutil, triage


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "data_dir", lambda: tmp_path)
    db._local.conn = None
    yield
    db._local.conn = None


def test_quote_found_despite_pdf_spacing():
    doc = "o A mini -mental state exam ≥ 2 2 for participant s with more than 8 years of education."
    assert textutil.quote_in("A mini-mental state exam ≥ 22 for participants with more", doc)
    assert not textutil.quote_in("A mini-mental state exam ≥ 24 for participants", doc)


def test_quoted_reply_headers_found_in_both_gmail_styles():
    from cormon import pipeline

    body = ("Yes.\r\n\r\nOn Fri, Oct 9, 2026 at 6:59\u202fAM John Ernest <johnernest978@gmail.com> wrote:\r\n\r\n"
            "> Thanks.\r\n>\r\n> On Fri, 9 Oct 2026 at 06:58, John Rohit <johnrohit94@gmail.com>\r\n> wrote:\r\n")
    found = [(m.group("date"), m.group("email")) for m in pipeline._WROTE.finditer(body)]
    assert [e for _, e in found] == ["johnernest978@gmail.com", "johnrohit94@gmail.com"]
    assert found[0][0].startswith("Fri, Oct 9, 2026 at 6:59")


def test_short_quote_expands_to_sentence():
    text = "Hello. Screening labs came back: HbA1c 5.9%, eGFR 41 mL/min, BMI 31. Thanks."
    assert textutil.expand_to_sentence("eGFR 41 mL/min", text) == \
        "Screening labs came back: HbA1c 5.9%, eGFR 41 mL/min, BMI 31."


def test_numeric_violation_rules():
    assert matching.violates(41, "lt", 45, None)
    assert not matching.violates(45, "lt", 45, None)
    assert matching.violates(91, "outside", 55, 90)
    assert not matching.violates(70, "outside", 55, 90)


def test_numeric_check_respects_condition():
    rule = {"id": "r", "check_type": "numeric", "parameter": "mmse_score", "operator": "lt", "value": 22,
            "value2": None, "condition": {"parameter": "education_years", "operator": "gt", "value": 8}}
    ev = {"facts": [{"parameter": "mmse_score", "numeric_value": 21, "value": "21", "quote": "MMSE 21"}]}
    found, undecided = matching.numeric_checks([rule], ev)
    assert not found and undecided == [rule]
    ev["facts"].append({"parameter": "education_years", "numeric_value": 16, "value": "16", "quote": "16 years"})
    found, _ = matching.numeric_checks([rule], ev)
    assert len(found) == 1 and found[0].observed == "21"


def test_triage_first_match_wins():
    facts = {"polarity": "problem", "pattern_class": "eligibility", "temporality": "planned", "hours_until": 5}
    assert triage.assign("NCT04098666", facts)[0] == "immediate"
    facts.update(temporality="current", hours_until=None)
    assert triage.assign("NCT04098666", facts)[0] == "medium"
    assert triage.assign("NCT04098666", {"polarity": "problem", "pattern_class": "visit_schedule",
                                          "temporality": "current"})[0] == "low"


def test_template_specificity():
    assert drafting.select("eligibility", "cra", "email", "medium")["id"] == "email-cra-medium-eligibility"
    assert drafting.select("sample_handling", "cra", "email", "medium")["id"] == "email-cra-medium"
    assert drafting.select("eligibility", "crc", "email", "immediate")["id"] == "email-site-notice"


def test_template_refuses_missing_values():
    with pytest.raises(drafting.DraftError):
        drafting.render({"id": "t", "body": "Hello {{ recipient.first_name }} {{ missing }}"},
                        {"recipient": {"first_name": "A"}})


def test_audit_log_is_append_only():
    rid = db.audit("test_action", entity_type="x", entity_id=1)
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("UPDATE audit_log SET action='changed' WHERE id=?", (rid,))
    with pytest.raises(sqlite3.DatabaseError):
        db.ex("DELETE FROM audit_log WHERE id=?", (rid,))


def test_study_resolved_from_plus_address():
    raw = (b"From: a@site.org\r\nTo: cra@x.org\r\nCc: bot+NCT04098666@huma.ai\r\nSubject: hi\r\n"
           b"Message-ID: <m1@site.org>\r\n\r\nbody\r\n")
    inb = capture.parse_email(raw)
    assert capture.resolve_email_route(inb) == ("NCT04098666", "bot+nct04098666@huma.ai", "cc")
    raw2 = raw.replace(b"bot+NCT04098666@huma.ai", b"bot@huma.ai")
    assert capture.resolve_email_route(capture.parse_email(raw2))[0] is None


def test_capture_is_idempotent_and_keeps_forward_dates():
    raw = (b"From: a@site.org\r\nTo: bot+NCT04098666@huma.ai\r\nSubject: fwd\r\nMessage-ID: <m2@site.org>\r\n"
           b"Date: Fri, 09 Oct 2026 09:00:00 +0000\r\n\r\nSee below.\r\n\r\n"
           b"---------- Forwarded message ---------\r\nFrom: b@site.org\r\nDate: Mon, 14 Sep 2026 10:00:00 +0000\r\n"
           b"Subject: old\r\n\r\nOld news.\r\n")
    inb = capture.parse_email(raw)
    mid = capture.capture(inb)
    assert mid and capture.capture(capture.parse_email(raw)) is None
    att = capture.message(mid)["attachments"]
    assert att[0]["component"] == "inline-forward" and att[0]["source_date"].startswith("2026-09-14")


def _lm(op, v, v2=None, band=16):
    return {"check_type": "numeric", "parameter": "logical_memory_ii_score", "operator": op, "value": v,
            "value2": v2, "condition": {"parameter": "education_years", "operator": "ge", "value": band},
            "text": f"{op} {v}", "source_quote": f"{op} {v}", "review": None, "_active": True}


def test_either_or_ranges_merge_into_one_rule():
    from cormon import ingest

    early, late = _lm("outside", 9, 11), _lm("gt", 8)
    assert ingest.merge_alternatives([early, late]) == 1
    assert (early["operator"], early["value"]) == ("gt", 11) and not late["_active"]


def test_nested_limits_are_not_merged():
    from cormon import ingest

    assert ingest.merge_alternatives([_lm("gt", 8), _lm("gt", 11)]) == 0


def test_quote_fragment_drops_glued_heading():
    from cormon import ingest

    text = "For early MCI:\n9-11 for 16 or more years of education\n5-9 for 8-15 years of education\n"
    rule = {"source_quote": "For early MCI: 5-9 for 8-15 years of education", "value": 5.0, "value2": 9.0,
            "section": None}
    s, e = ingest.quote_fragment(rule, text, [0])
    assert text[s:e].strip() == "5-9 for 8-15 years of education"


def test_opposite_limits_are_not_merged_into_anything_goes():
    from cormon import ingest

    assert ingest.merge_alternatives([_lm("lt", 0.5), _lm("ge", 0.5)]) == 0


def test_limits_written_the_wrong_way_are_turned_round(monkeypatch):
    from cormon import ingest

    rule = dict(_lm("le", 28), parameter="screening_window_days", condition=None)
    monkeypatch.setattr(ingest.llm, "structured", lambda *a, **k: {
        "probes": [{"rule_number": 1, "breaking_value": 40, "fine_value": 10}]})
    assert ingest.check_directions([rule]) == 1
    assert rule["operator"] == "gt"


def test_review_cannot_drop_both_twins_or_drop_without_a_fix():
    from cormon import ingest

    live = [dict(_lm("ne", 0.5), parameter="cdr"), dict(_lm("lt", 0.5), parameter="cdr_memory_box"),
            _lm("gt", 8)]
    ingest.apply_drops(live, [
        {"rule_number": 1, "why": "not_stated", "duplicate_of": None, "reason": "x"},
        {"rule_number": 2, "why": "duplicate", "duplicate_of": 1, "reason": "x"},
        {"rule_number": 3, "why": "wrong_numbers", "duplicate_of": None, "reason": "x"}], [])
    assert live[0]["review"].startswith("dropped")
    assert live[1]["review"].startswith("kept") and live[2]["review"].startswith("kept")


def test_exception_turns_a_number_rule_off():
    rule = {"id": "r", "check_type": "numeric", "parameter": "cdr_score", "operator": "ne", "value": 0.5,
            "value2": None, "condition": None,
            "unless": [{"parameter": "cdr_score", "operator": "eq", "value": 0, "value2": None},
                       {"parameter": "cdr_memory_box_score", "operator": "ge", "value": 0.5, "value2": None}]}

    def event(**vals):
        return {"facts": [{"parameter": k, "numeric_value": v, "value": str(v), "quote": k} for k, v in vals.items()]}

    found, undecided = matching.numeric_checks([rule], event(cdr_score=0, cdr_memory_box_score=0.5))
    assert not found and not undecided
    found, _ = matching.numeric_checks([rule], event(cdr_score=1, cdr_memory_box_score=0.5))
    assert found
    found, undecided = matching.numeric_checks([rule], event(cdr_score=0))
    assert not found and undecided == [rule]

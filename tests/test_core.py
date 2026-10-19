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


def test_unsure_finding_says_so_in_the_email():
    t = dict(drafting.select("sample_handling", "cra", "email", "medium"), ai_edit=False)
    ctx = {"study": {"short_name": "MAP", "monitor_signature": "Monitor"},
           "item": {"id": 7, "title": "Please check (not sure): Sample late", "participant_id": "P1", "site": "S",
                    "link": "http://x/items/t"},
           "finding": {"sender_name": "A", "message_date": "1 Jan", "message_quote": "q", "doc_title": "Lab",
                       "page": 3, "where": "Lab, page 3", "next_step": "", "doc_quote": "d", "unsure": True, "unsure_reason": "Years of education not given.", "earlier": ""},
           "also": [], "bundle": False, "recipient": {"first_name": "Bo"}}
    ev = {"certainty": "unsure", "unsure_reason": "Years of education not given.", "message_quote": "q",
          "doc_quote": "d", "doc_title": "Lab", "doc_page": 3, "next_step": ""}
    ctx["problems"] = drafting.problems(None, ctx["item"], ev)
    _, body = drafting.render(t, ctx)
    assert "**1. Sample late** (please check: not sure)" in body
    assert "Why not sure: Years of education not given." in body
    assert "> q" in body and "> d" in body
    _, body = drafting.render(t, dict(ctx, problems=drafting.problems(None, ctx["item"], dict(ev, certainty="clear"))))
    assert "not sure" not in body
    elig = dict(drafting.select("eligibility", "cra", "email", "medium"), ai_edit=False)
    item = dict(ctx["item"], title="Randomization planned with eGFR 41")
    _, body = drafting.render(elig, dict(ctx, item=item, problems=drafting.problems(None, item, ev)))
    assert "**1. Randomization planned with eGFR 41**" in body and "enrolled" not in body


def test_protocol_words_are_found_sentence_by_sentence():
    from cormon.reader import _exact_words
    a = {"id": "x:4.2", "kind": "section", "text": "Exclusion: Use of strong inducers within 2 weeks of the first dose."}
    b = {"id": "x:s9", "kind": "section", "text": "Table. phenytoin: recommended washout is 2 weeks."}
    brief = {"id": "brief:meds", "kind": "brief", "text": "- Exclude inducers within 2 weeks [x:4.2]"}
    f = {"problem": "Phenytoin stopped too close to first dose", "why": "washout 2 weeks", "email_quote": "phenytoin 6/25"}
    sec, words = _exact_words("Use of strong inducers within 2 weeks of the first dose.\n"
                              "phenytoin: recommended washout is 2 weeks. [x:s9]", [a, brief], [a, b], "x:4.2", f)
    assert sec["id"] in ("x:4.2", "x:s9") and words in a["text"] + b["text"]
    assert _exact_words("Exclude inducers within 2 weeks", [brief], [a, b], "x:4.2", f) is None  # a brief is not proof


def test_protocol_words_drop_a_procedure_list_that_is_not_about_the_finding():
    from cormon.reader import _exact_words
    v = {"id": "g:6.4", "kind": "section", "text": "This visit will take place 84 ± 3 days after the Baseline visit via "
         "telephone. The following procedures will be performed for the regimen schedule:\n1. Perform drug compliance "
         "check-in (remind of the importance of compliance)"}
    f = {"problem": "The Week 12 phone call was done late.", "why": "91 days after baseline; allowed 84 ± 3 days",
         "email_quote": "Week 12 phone call done 9/30"}
    _, words = _exact_words(v["text"], [v], [v], "g:6.4", f)
    assert words == "This visit will take place 84 ± 3 days after the Baseline visit via telephone."


def test_protocol_words_are_never_a_heading_another_visit_or_the_bots_own_checklist():
    from cormon.reader import _exact_words
    soa = {"id": "g:s010", "kind": "section", "text": "SCHEDULE OF ACTIVITIES – PLACEBO-CONTROLLED TREATMENT PERIOD)"}
    own = {"id": "g:v1-baseline", "kind": "piece", "text": "Visit checklist: Baseline (Clinic), window Day 0."}
    v2 = {"id": "g:6.2", "kind": "section", "text": "This visit will take place 14 ± 3 days after the Baseline Visit "
          "via telephone."}
    f = {"problem": "The Week 2 phone visit was done late.", "why": "Day 21 is after Day 17.",
         "email_quote": "wk2 phone call done 10/1"}
    assert _exact_words("Conduct Week 2 by phone on Day 14 ± 3. (Placebo-controlled treatment period)",
                        [soa], [soa, own], "g:s010", f) is None
    assert _exact_words("Visit checklist: Week 4 (Clinic), window Day 28 ± 7.", [own], [own], "", f) is None
    _, words = _exact_words(v2["text"], [soa], [soa, v2], "", f)  # the code check's rule quote
    assert words == v2["text"]


def test_a_schedule_table_quote_keeps_only_the_visit_in_question():
    from cormon.reader import _exact_words
    row = ("Schedule table (SCHEDULE OF ACTIVITIES). Columns: Screening [1] / Clinic / –42 to –1 Days [13] | "
           "Baseline [1] / Clinic / Day 0 | Week 2 / Phone / Day 14 ± 3 | Week 4 [12] / Clinic / Day 28 ± 7 | "
           "Week 8 [12] / Clinic / Day 56 ± 7 | Week 24 / Clinic / Day 168 ± 7")
    soa = {"id": "g:s010", "kind": "section", "text": row}
    f = {"problem": "The planned Week 4 visit is outside the allowed window.", "why": "Day 40 is after Day 35.",
         "email_quote": "wk4 visit planned for 10/20"}
    assert _exact_words(row, [soa], [soa], "g:s010", f)[1] == "Week 4 [12] / Clinic / Day 28 ± 7"
    other = {"id": "g:6.9", "kind": "section", "text": "Visit may be conducted via phone if needed."}
    assert _exact_words("Conduct Week 8 in clinic on Day 56 ± 7. Visit may be conducted via phone if needed.",
                        [other], [other], "", dict(f, problem="Week 8 visit outside window")) is None


def test_email_words_add_the_other_time_the_code_used():
    from cormon.reader import _email_words
    assert _email_words({"email_quote": "postdose 9:20", "code_quotes": ["dosed 9:00", "postdose 9:20"]}) \
        == "postdose 9:20 ... dosed 9:00"
    assert _email_words({"email_quote": "predose pk 8:00, dosed 9:00"}) == "predose pk 8:00, dosed 9:00"


def test_low_problems_are_emailed_to_the_cra():
    tpl = drafting.select("visit_schedule", "cra", "email", "low")
    assert tpl["id"] == "email-cra-low" and "to-do list" in tpl["subject"]


def test_alert_lists_every_problem_with_its_own_step_and_evidence():
    from cormon.channels.gchat import chat_text
    from cormon.channels.sim import build_mime
    ev = {"certainty": "clear", "message_quote": "predose pk 7:00, dosed 9:30", "doc_quote": "0 to 2 hours before",
          "doc_title": "Appendix G", "doc_page": None, "rule_text": "s010", "next_step": "Do not use the predose sample.",
          "additional": [{"doc_title": "Appendix G", "doc_quote": "1 to 8 hours postdose", "rule_text": "s010",
                          "message_quote": "postdose 10:00", "headline": "Postdose sample too soon.",
                          "next_step": "Recollect the postdose sample.", "certainty": "unsure", "unsure_reason": "r"}]}
    item = {"id": 8, "title": "Predose sample too early.", "participant_id": "G-1", "site": "S", "link": "http://x/i",
            "tier_label": "Medium", "routed_to": "CRA"}
    ps = drafting.problems(None, item, ev)
    assert [p["title"] for p in ps] == ["Predose sample too early.", "Postdose sample too soon."]
    ctx = {"study": {"short_name": "DNL"}, "item": item, "problems": ps, "bundle": False,
           "finding": {"sender_name": "A", "message_date": "1 Jan", "earlier": ""}}
    chat = chat_text(drafting.render(dict(drafting.select("sample_handling", "study_team", "teams", "medium"),
                                          ai_edit=False), ctx)[1])
    assert "*2 problems found:*" in chat and "*2. Postdose sample too soon.*" in chat
    assert "2. Recollect the postdose sample." in chat and "`postdose 10:00`" in chat
    mime = build_mime({"from_addr": "a@x", "to_addrs": ["b@x"], "cc_addrs": [], "subject": "s",
                       "body": "**1. Problem**\n> site words\n──────────\nhttp://x/i"})
    plain, rich = (p.get_content() for p in mime.iter_parts())
    assert "**" not in plain and "<b>1. Problem</b>" in rich and "site words</div>" in rich


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


def test_compare_scores_safety_and_problems_together():
    from cormon import compare, testbench

    res = compare._result([{"kind": "eligibility", "participant_id": "map-0905"}], safety=True)
    assert res["got"] == {"eligibility", "out_of_scope"} and res["pids"] == {"MAP-0905"}
    case = testbench.Case(name="x", subject="s", expect={"eligibility", "out_of_scope"}, participant="MAP-0905")
    case.result = res
    assert testbench.verdict(case) is True
    case.result = compare._result([], safety=True)
    assert testbench.verdict(case) is False


def test_stitched_quote_is_accepted_only_when_every_piece_is_in_the_message():
    text = "Plan for MAP-2073: we'll randomize him tomorrow morning. His screening ECG is booked for Friday."
    stitched = "His screening ECG is booked for Friday. Plan for MAP-2073: we'll randomize him tomorrow morning."
    assert textutil.quote_pieces(stitched, text) == [
        "Plan for MAP-2073: we'll randomize him tomorrow morning.", "His screening ECG is booked for Friday."]
    assert textutil.quote_pieces("His ECG was done. We randomized him on Monday.", text) == []


def test_value_mentioned_without_a_number_goes_to_the_judgment_step():
    rule = {"id": "r1", "check_type": "numeric", "parameter": "fasting_hours", "operator": "lt", "value": 8,
            "value2": None, "condition": None, "unless": None}
    event = {"facts": [{"parameter": "fasting_hours", "value": "had not fasted", "numeric_value": None,
                        "quote": "hadn't fasted"}]}
    found, undecided = matching.numeric_checks([rule], event)
    assert found == [] and undecided == [rule]


def test_split_that_loses_a_number_keeps_the_original():
    from cormon import ingest

    bundled = {"text": "Screening within 28 days; 6-month visits ±30 days; monthly contacts ±7 days.",
               "value": None, "value2": None}
    parts = [{"text": "The 6-month visit window is ±30 days.", "value": -30.0, "value2": 30.0},
             {"text": "The monthly contact window is ±7 days.", "value": -7.0, "value2": 7.0}]
    assert ingest.lost_numbers(bundled, parts) == {"28"}
    assert ingest.lost_numbers(bundled, parts + [{"text": "Screening within 28 days.", "value": 28.0,
                                                  "value2": None}]) == set()
    bundled_quote = {"text": "PK timing at Week 24.", "value": None, "value2": None,
                     "source_quote": "Postdose sample can be collected anytime between 1 to 8 hours postdose."}
    assert ingest.lost_numbers(bundled_quote, [{"text": "Take the dose in clinic.", "value": None,
                                                "value2": None, "source_quote": "dose in the clinic"}]) == {"1", "8", "24"}


def test_bmi_is_worked_out_from_weight_and_height():
    rule = {"id": "bmi", "check_type": "numeric", "parameter": "bmi_kg_m2", "operator": "lt", "value": 20,
            "value2": None, "condition": None, "unless": None}
    event = {"facts": [{"parameter": "weight_kg", "value": "52", "numeric_value": 52.0, "quote": "weight 52.0 kg"},
                       {"parameter": "height_cm", "value": "163", "numeric_value": 163, "quote": "height 163 cm"}]}
    found, _ = matching.numeric_checks([rule], event)
    assert len(found) == 1 and "19.6" in found[0].observed


def test_short_reading_counts_as_a_quote():
    assert textutil.quote_in("162/96", "BP readings 162/96, 158/95", short_ok=True)
    assert not textutil.quote_in("5.9", "potassium 5.9", short_ok=True)


def test_narrower_option_listed_with_a_wider_one_is_not_a_second_limit():
    from cormon import ingest

    quote = "For early MCI: 9-11 for 16 or more years of education. For late MCI: 8 or less for 16 or more years"
    cond = {"parameter": "education_years", "operator": "ge", "value": 16, "value2": None}
    early = {"check_type": "numeric", "parameter": "lm2", "operator": "gt", "value": 11.0, "value2": None,
             "condition": cond, "unless": None, "text": "early", "source_quote": quote, "review": "", "_active": True}
    late = dict(early, value=8.0, text="late")
    assert ingest.merge_alternatives([early, late]) == 1
    assert early["_active"] and not late["_active"]
    other = dict(late, source_quote="A completely different sentence about the same score.")
    assert ingest.merge_alternatives([dict(early, _active=True), other]) == 0


def test_team_decisions_turn_rules_off_and_survive_a_rebuild(tmp_path):
    from types import SimpleNamespace

    from openpyxl import Workbook

    from cormon import prune

    def add(rid, text):
        db.ex("INSERT INTO rules (id, study_id, doc_id, doc_sha256, pattern_class, kind, text, check_type,"
              " source_quote, verified, order_idx, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "S", "protocol", "x", "consent", "requirement", text, "judgment", f"quote for {text}", 1, 0, 1,
               db.now()))

    add("R1", "Verbal consent is given at intake")
    add("R2", "Consent is signed before any procedure")
    study = SimpleNamespace(study_id="S")
    wb = Workbook()
    wb.active.title = "Rules in use"
    wb.active.append(["Rule", "Rule text", "Team decision", "Team note"])
    wb.active.append(["R1", "Verbal consent is given at intake", "Remove", "on the intake checklist"])
    wb.active.append(["R2", "Consent is signed before any procedure", ""])
    sheet = tmp_path / "rules.xlsx"
    wb.save(sheet)

    counts = prune.read_sheet(study, sheet, "eliot")
    assert counts["remove"] == 1 and counts["blank"] == 1
    assert [r["id"] for r in db.q("SELECT id FROM rules WHERE active=1")] == ["R2"]

    db.ex("DELETE FROM rules")              # a rebuild renumbers the rules
    add("R7", "Verbal consent is given at intake")
    assert prune.reapply("S") == 1
    assert db.q1("SELECT active FROM rules WHERE id='R7'")["active"] == 0

    assert prune.decide(study, "R7", "keep", "eliot")
    assert db.q1("SELECT active FROM rules WHERE id='R7'")["active"] == 1

    # one sentence, two checks: removing the systolic rule must not remove the diastolic one
    for rid, param in (("BP1", "systolic_bp"), ("BP2", "diastolic_bp")):
        db.ex("INSERT INTO rules (id, study_id, doc_id, doc_sha256, pattern_class, kind, text, check_type, parameter,"
              " source_quote, verified, order_idx, active, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "S", "protocol", "x", "eligibility", "exclusion", "Uncontrolled hypertension", "numeric", param,
               "BP 160/95", 1, 0, 1, db.now()))
    prune.decide(study, "BP1", "remove", "eliot")
    prune.reapply("S")
    assert db.q1("SELECT active FROM rules WHERE id='BP2'")["active"] == 1


def test_duplicate_suggestions_never_lose_a_check():
    from cormon import prune

    def rule(rid, cls="eligibility", value=None):
        return {"id": rid, "pattern_class": cls, "check_type": "numeric" if value else "judgment",
                "parameter": "mmse_score" if value else None, "operator": "lt" if value else None,
                "value": value, "value2": None, "condition": None}

    rules = [rule("A"), rule("B"), rule("C", value=22), rule("D", value=20), rule("E", cls="visit_schedule"),
             rule("F")]
    dup = lambda of: {"suggest": "remove", "reason_type": "duplicate", "duplicate_of": of, "why": "same"}
    answers = {"A": dup("B"), "B": dup("A"), "C": dup("D"), "F": dup("E")}
    got = {r["id"]: s for r, s in zip(rules, prune._settle(rules, answers))}
    assert got["A"][0] == "keep" and got["B"][0] == "remove"   # of two naming each other, one stays
    assert got["C"][0] == "keep"                                # 22 and 20 are different checks
    assert got["F"][0] == "keep"                                # twin is another problem type


class _FakeGoogle:
    """Answers the few Chat, People and sign-in calls the Google Chat adapter makes."""

    def __init__(self, spaces):
        self.spaces, self.posted = spaces, []

    def _reply(self, data):
        return type("R", (), {"status_code": 200, "raise_for_status": lambda s: None, "json": lambda s: data})()

    def get(self, url, params=None, timeout=None):
        if url.endswith("/userinfo"):
            return self._reply({"sub": "100", "email": "bot@huma.ai"})
        if "/people/" in url:
            return self._reply({"emailAddresses": [{"value": f"user{url.rsplit('/', 1)[1]}@site.org"}]})
        if url.endswith("/spaces"):
            kind = "spaces/dm" if "DIRECT_MESSAGE" in params["filter"] else "spaces/g"
            return self._reply({"spaces": [{"name": n} for n in self.spaces if n.startswith(kind)]})
        return self._reply({"messages": self.spaces[url.split("/v1/", 1)[1].rsplit("/messages", 1)[0]]})

    def post(self, url, json=None, params=None, timeout=None):
        self.posted.append((url, json, params))
        return self._reply({"name": "spaces/S1/messages/new"})


def test_google_chat_reads_spaces_and_direct_messages_and_posts_alerts(monkeypatch):
    from cormon import config
    from cormon.channels.gchat import GoogleChatChannel, chat_text

    study = config.study("NCT04098666")
    monkeypatch.setattr(study, "teams", {"study_channel": {"space": "spaces/S1"}})
    monkeypatch.setattr(config, "studies", lambda: {study.study_id: study})
    db.set_state("gchat_since", "2026-10-12T10:00:00+00:00")
    msg = lambda name, user, t, **kw: dict({"name": name, "sender": {"name": f"users/{user}", "type": "HUMAN"},
                                            "createTime": t, "text": "BP 168/92, randomizing at 3pm",
                                            "thread": {"name": "spaces/S1/threads/T1"}}, **kw)
    fake = _FakeGoogle({
        "spaces/S1": [msg("spaces/S1/messages/1", 7, "2026-10-12T10:05:00Z"),
                      msg("spaces/S1/messages/2", 100, "2026-10-12T10:06:00Z"),          # the bot itself
                      msg("spaces/S1/messages/3", 8, "2026-10-12T10:07:00Z", threadReply=True)],
        "spaces/dm1": [msg("spaces/dm1/messages/9", 9, "2026-10-12T10:08:00Z", thread={"name": "spaces/dm1/threads/D"})],
    })
    got = GoogleChatChannel(session=fake).fetch()
    assert [i.external_id for i in got] == ["spaces/S1/messages/1", "spaces/S1/messages/3", "spaces/dm1/messages/9"]
    assert got[0].study_id == "NCT04098666" and got[0].visibility == "channel" and got[0].from_addr == "user7@site.org"
    assert got[1].reply_to_external == "spaces/S1/threads/T1" and got[1].thread_ref == got[0].thread_ref
    assert got[2].visibility == "direct" and got[2].study_id == "NCT04098666"

    GoogleChatChannel(session=fake).send({"body": '<p><b>IMMEDIATE · MAP</b></p>\n<p>BP &gt; limit<br><i>"q"</i></p>'
                                                  '\n<p>Item #3 · <a href="https://x/items/3">details</a></p>',
                                          "meta": {"target": {"space": "spaces/S1", "key": "alert_channel"}}})
    url, body, _ = fake.posted[0]
    assert url.endswith("/spaces/S1/messages")
    assert body["text"] == '*IMMEDIATE · MAP*\n\nBP > limit\n_"q"_\n\nItem #3 · <https://x/items/3|details>'
    assert chat_text("<p>a</p><p>b</p>") == "a\n\nb"


def test_site_named_in_the_message_wins_over_the_senders_site(monkeypatch):
    from cormon import config, pipeline

    study = config.study("NCT04098666")
    monkeypatch.setattr(study, "sites", [{"id": "site-01", "name": "Site 01 (Columbia)"},
                                         {"id": "site-02", "name": "Site 02 (Boston)", "aliases": ["MGH"]}])
    crc = next(p.email for p in study.people if "crc" in p.roles)
    site = lambda body, subject="": pipeline._site_for(study, {"from_addr": crc, "subject": subject, "body_text": body})
    assert site("MAP-0161 at site #2: BP 168/92") == "site-02"
    assert site("Update from MGH: MAP-0161 BP 168/92") == "site-02"
    assert site("BP 168/92, randomizing at 3pm") == "site-01"                         # nothing named: sender's site
    assert site("MAP-0161 at Site 03: BP 168/92") == "Site 03"                        # not in the list: as written
    assert site("Moved from Site 01 to Site 02 today") == "site-01"                   # two named: sender's site
    assert site("Thanks.\n\nOn Mon, Oct 12, 2026 at 9:00 AM A <a@b.org> wrote:\n> Site 02 called") == "site-01"
    assert study.site_name("Site 03") == "Site 03 (not in the study's site list)"
    assert {p.id for p in study.people_with_role("cra", "Site 03")} == {p.id for p in study.people_with_role("cra")}


def test_one_chat_post_when_study_and_alert_channels_are_the_same_room(monkeypatch):
    from cormon import config, routing

    study = config.study("NCT04098666")
    immediate = [{"destination": "teams_study_channel"}, {"destination": "teams_alert_channel"},
                 {"destination": "email"}]
    monkeypatch.setattr(study, "teams", {"study_channel": {"space": "spaces/A"}, "alert_channel": {"space": "spaces/A"}})
    assert [e["destination"] for e in routing._one_post_per_room(study, immediate)] == \
        ["teams_alert_channel", "email"]
    monkeypatch.setattr(study, "teams", {"study_channel": {"space": "spaces/A"}, "alert_channel": {"space": "spaces/B"}})
    assert routing._one_post_per_room(study, immediate) == immediate
    assert routing._one_post_per_room(study, immediate[:1]) == immediate[:1]


def test_duplicate_drop_keeps_a_different_limit():
    from cormon import ingest
    base = {"check_type": "numeric", "parameter": "systolic_bp_mmhg", "value2": None, "condition": None,
            "text": "SBP > 180 or < 100", "source_quote": "SBP > 180 mmHg or < 100 mmHg", "review": None}
    live = [dict(base, operator="gt", value=180.0), dict(base, operator="lt", value=100.0)]
    ingest.apply_drops(live, [{"rule_number": 2, "why": "duplicate", "duplicate_of": 1, "reason": "same"}], [])
    assert live[1]["review"].startswith("kept")


def test_a_quote_spanning_numbered_criteria_covers_none():
    from cormon import ingest
    items = ["1. NYHA Class II-III symptoms", "2. Left ventricular ejection fraction >= 50%",
             "a. Mitral E/e' ratio > 14"]
    blob = "1. NYHA Class II-III symptoms 2. Left ventricular ejection fraction >= 50%"
    assert ingest.bundles_criteria(blob, items)
    assert not ingest.bundles_criteria("Left ventricular ejection fraction >= 50%", items)


def test_same_rule_twice_is_one_rule_and_approximate_is_not_exact():
    from cormon import ingest
    base = {"check_type": "judgment", "parameter": None, "operator": None, "value": None, "value2": None,
            "condition": None, "source_quote": "x"}
    rules = [dict(base, text="Exclude if clinically significant pericardial disease, as per investigator judgment"),
             dict(base, text="Exclude if participant has clinically significant pericardial disease, as per "
                             "investigator judgment."),
             dict(base, text="Visit 5 must occur after Visit 4."), dict(base, text="Visit 7 must occur after Visit 6."),
             dict(base, check_type="numeric", parameter="days_washout", operator="ne", value=14.0,
                  text="A washout of approximately 14 days")]
    assert [i for i, _ in ingest.lint(rules)] == [1]
    assert rules[4]["check_type"] == "judgment"


def test_clashing_windows_on_one_value_are_read_not_counted():
    from cormon import ingest
    base = {"check_type": "numeric", "parameter": "days_since_prior_visit", "operator": "outside",
            "condition": None, "text": "x"}
    rules = [dict(base, value=2.0, value2=7.0), dict(base, value=20.0, value2=22.0),
             dict(base, parameter="lvef_percent", operator="lt", value=50.0, value2=None),
             dict(base, parameter="lvef_percent", operator="lt", value=45.0, value2=None)]
    assert ingest.read_clashing_windows(rules) == 2
    assert [r["check_type"] for r in rules] == ["judgment", "judgment", "numeric", "numeric"]


def test_a_window_that_names_no_visit_is_read_not_counted():
    from cormon import ingest
    base = {"check_type": "numeric", "operator": "outside", "value": 7.0, "value2": -7.0, "condition": None,
            "text": "x", "source_quote": "y"}
    rules = [dict(base, parameter="days_visit_after_baseline"), dict(base, parameter="Days_Week4_after_Baseline"),
             dict(base, parameter="days_screening_before_baseline")]
    ingest.lint(rules)
    assert [r["check_type"] for r in rules] == ["judgment", "numeric", "numeric"]
    assert rules[1]["parameter"] == "days_week4_after_baseline" and rules[1]["value"] == -7.0


def test_another_way_of_measuring_is_not_an_exception():
    from cormon import ingest
    when = {"parameter": "pandemic_related_restrictions", "operator": "eq", "value": 1, "value2": None}
    svc = {"check_type": "numeric", "parameter": "svc_percent_predicted", "operator": "lt", "value": 50.0,
           "value2": None, "condition": None, "unless": [dict(when)], "review": None}
    fvc = dict(svc, parameter="fvc_percent_predicted", condition=dict(when), unless=[])
    assert ingest.alternatives_not_exceptions([svc, fvc]) == 1 and svc["unless"] == []


def test_one_problem_under_two_classes_is_one_alert():
    from types import SimpleNamespace as NS
    from cormon import pipeline
    study = NS(pattern_classes=[{"id": "eligibility"}, {"id": "study_drug"}, {"id": "consent"}])
    c = lambda cls, quote, tier="medium", pid="P1": {"tier": tier, "pid": pid, "f": NS(
        message_quote=quote, method="judgment", certainty="clear", rule={"pattern_class": cls})}
    groups = {("study_drug", "P1"): [c("study_drug", "started riluzole two weeks before screening")],
              ("eligibility", "P1"): [c("eligibility", "his neurologist started riluzole two weeks before screening")],
              ("consent", "P1"): [c("consent", "signed the consent form on Wednesday", "immediate")]}
    out = pipeline._one_alert_per_problem(study, groups)
    assert set(out) == {("eligibility", "P1"), ("consent", "P1")} and len(out[("eligibility", "P1")]) == 2


def test_a_value_code_found_within_its_window_is_not_flagged_again_by_judgment():
    from cormon import matching
    rule = {"id": "w4", "check_type": "numeric", "parameter": "days_week4_after_baseline", "operator": "outside",
            "value": 21.0, "value2": 35.0, "condition": None, "unless": None, "pattern_class": "visit_schedule"}
    event = {"quote": "Week 4 visit for DNL-1036 was on Day 33.",
             "facts": [{"parameter": "days_week4_after_baseline", "numeric_value": 33, "quote": "on Day 33"}]}
    passed = matching.passed_checks([rule], event, set(), [])
    assert {r["id"] for r, _ in passed} == {"w4"}
    assert any(matching._only_about("Week 4 visit for DNL-1036 was on Day 33", q) for _, q in passed)
    assert not matching._only_about("the ALSFRS-R was skipped at the Week 4 visit", passed[0][1])


def test_a_passed_value_does_not_silence_other_facts_in_the_same_sentence():
    from cormon import matching
    rule = {"id": "age", "check_type": "numeric", "parameter": "age_years", "operator": "lt", "value": 18.0,
            "value2": None, "condition": None, "unless": None, "pattern_class": "eligibility"}
    event = {"quote": "Randomized DNL-1007 today, age 45, had a seizure in June.",
             "facts": [{"parameter": "age_years", "numeric_value": 45, "quote": "age 45"},
                       {"parameter": "seizure_history", "numeric_value": None, "quote": "had a seizure in June"}]}
    passed = matching.passed_checks([rule], event, set(), [])
    assert not any(matching._only_about("Randomized DNL-1007 today, age 45, had a seizure in June", q)
                   for _, q in passed)


def test_duplicate_comes_back_when_its_twin_fails_the_quote_check():
    from cormon import ingest

    twin = {"_span": None, "_active": False, "_origin": "", "check_type": "judgment", "source_quote": "x"}
    dup = {"_span": (0, 10), "_active": False, "_origin": "dropped by review (duplicate): same as 4",
           "_twin": twin, "check_type": "numeric", "source_quote": "y"}
    assert ingest.rescue_replaced([twin, dup]) == 1
    assert dup["_active"] and "duplicates did not pass" in dup["review"]


def test_should_is_not_dropped_as_not_stated():
    from cormon import ingest

    r = {"check_type": "judgment", "source_quote": "All dosing should be completed within 15 minutes.", "review": None}
    ingest.apply_drops([r], [{"rule_number": 1, "why": "not_stated", "duplicate_of": None,
                              "reason": "The word 'should' is not a strict requirement."}], [])
    assert r["review"].startswith("kept")


def test_intervals_ages_and_multiples_are_worked_out_in_code():
    from cormon import matching

    ev = {"facts": [
        {"parameter": "days_between_baseline_csf_and_first_dose", "value": "9/30 to 10/9", "numeric_value": 7,
         "unit": "days", "from_when": "2026-09-30", "to_when": "2026-10-09", "quote": "x"},
        {"parameter": "hours_predose_pk_before_dose", "value": "8:00 / 8:45", "numeric_value": 8, "unit": "hours",
         "from_when": "2026-10-05T08:00", "to_when": "2026-10-05T08:45", "quote": "x"},
        {"parameter": "age_years", "value": "DOB 11/14/2008", "numeric_value": 18, "unit": "years",
         "from_when": "2008-11-14", "to_when": "2026-10-01", "quote": "x"},
        {"parameter": "ast_x_uln", "value": "AST 117 (ULN 39)", "numeric_value": 3.1, "unit": "x ULN",
         "measured": 117, "reference": 39, "quote": "x"},
        {"parameter": "egfr", "value": "74", "numeric_value": 74, "unit": None, "quote": "x"},
    ]}
    got = {f["parameter"]: f["numeric_value"] for f in matching.worked_out(ev)["facts"]}
    assert got == {"days_between_baseline_csf_and_first_dose": 9, "hours_predose_pk_before_dose": 0.75,
                   "age_years": 17, "ast_x_uln": 3.0, "egfr": 74}
    assert not matching.violates(3.0, "gt", 3.0, None)


def test_quote_cut_with_brackets_is_found_by_its_pieces():
    from cormon import ingest

    text = ("The following values [alanine aminotransferase (ALT) or aspartate aminotransferase (AST) > 3 times the "
            "upper limit of normal (ULN) or eGFR < 30] are exclusionary")
    rule = {"source_quote": "The following values [alanine aminotransferase (ALT) or aspartate aminotransferase "
                            "(AST) > 3 times the upper limit of normal (ULN) ...] are exclusionary",
            "value": 3.0, "value2": None, "section": None}
    assert ingest.quote_fragment(rule, text, [0]) is not None


def test_deviation_sentences_count_as_requirements():
    from cormon import ingest

    text = ("If initiation of riluzole therapy begins at any point during participation, this will be recorded as "
            "a minor protocol deviation. The weather was fine.")
    assert any("riluzole" in p for p in ingest.passages(text))


def test_a_named_deviation_is_not_an_allowance():
    from cormon import ingest

    assert ingest.names_a_deviation("If initiation of riluzole begins during participation, this will be recorded "
                                    "as a minor protocol deviation.")
    assert not ingest.names_a_deviation("Procedures attempted but failed will not be reported as protocol deviations.")
    assert not ingest.names_a_deviation("A missed dose may be taken later the same day.")


# ---------------------------------------------------------------- the protocol guide and the reader

def test_guide_reads_word_tables_and_visit_checklists():
    from pathlib import Path
    from cormon import guide
    src = Path(__file__).resolve().parent.parent / "studies/NCT05842941/docs/regimen_g.docx"
    if not src.exists():
        pytest.skip("Regimen G Word file not present")
    sections, visits = guide.read_docx(src, "g")
    by_num = {s.number: s for s in sections}
    assert "Diagnosis of epilepsy" in by_num["4.2.2"].text            # numbered list kept under its heading
    assert not any("....." in s.text for s in sections)                # table of contents left out
    names = [v.name for v in visits]
    assert "Week 4" in names and "Follow-Up Safety Call" in names      # real visit numbers kept, footnote numbers dropped
    wk4 = next(v for v in visits if v.name == "Week 4")
    assert wk4.window == "Day 28 ± 7" and wk4.kind == "Clinic"         # symbol-font ± read
    assert any(n.startswith("15]") and "predose" in n for n in wk4.notes)
    ate = [v for v in visits if "ATE" in v.name]
    assert ate and "Week 28" in ate[1].name                            # extension weeks carry both counts


def test_reader_windows_and_date_arithmetic():
    from datetime import datetime
    from cormon import reader
    wk16 = {"id": "v16", "title": "Week 16", "path": "SCHEDULE - PLACEBO-CONTROLLED",
            "text": "Visit checklist: Week 16 (Clinic), window Day 112 ± 7. From: x."}
    fu = {"id": "fu", "title": "Follow-Up Safety Call", "path": "SCHEDULE - ACTIVE TREATMENT EXTENSION",
          "text": "Visit checklist: Follow-Up Safety Call (Phone), window 28 days after last dose ± 3 days. From: x."}
    assert reader.window(wk16) == ("baseline", 105, 119)
    assert reader.window(fu) == ("last_dose", 25, 31)
    dates = [{"label": "Baseline", "kind": "baseline", "date": "2026-06-15", "time": None, "visit_id": ""},
             {"label": "Week 16 visit", "kind": "visit", "date": "2026-10-13", "time": None, "visit_id": "v16"},
             {"label": "dose", "kind": "dose", "date": "2026-10-13", "time": "08:00", "visit_id": ""},
             {"label": "PK sample", "kind": "sample", "date": "2026-10-13", "time": "08:15", "visit_id": ""}]
    notes = reader.date_notes(dates, datetime(2026, 10, 5, 9), {"v16": wk16})
    text = " ".join(notes)
    assert "this date is Day 120: 1 day(s) after the window closes" in text
    assert "PK sample is 0.2 hours after dose" in text


def test_reader_catches_its_own_number_slips():
    from cormon import reader
    checks = [{"what": "postdose PK", "measured": 0.7, "allowed_min": 1, "allowed_max": 8, "unit": "hours", "verdict": "ok"},
              {"what": "visit day", "measured": 53, "allowed_min": 49, "allowed_max": 63, "unit": "days", "verdict": "ok"},
              {"what": "CSF at ET", "measured": 50, "allowed_min": 84, "allowed_max": None, "unit": "days", "verdict": "ok"},
              {"what": "already flagged", "measured": 120, "allowed_min": 105, "allowed_max": 119, "unit": "days",
               "verdict": "problem"}]
    slips = reader.number_slips(checks)
    assert len(slips) == 2 and "0.7 hours is outside the allowed range 1 to 8 hours" in slips[0]


def test_reader_flags_unchecked_statements_and_repeated_findings():
    from cormon import reader
    stmts = [{"text": "Week 4 visit on 12 Oct", "status": "done", "anchors": []},
             {"text": "took ibuprofen", "status": "reported", "anchors": []}]
    gaps = reader.uncovered(stmts, [{"statement": 1}, {"statement": 0}])
    assert len(gaps) == 1 and "statement 2" in gaps[0]
    a = {"email_quote": "St John's wort stopped 9 days before", "section_id": "s1",
         "problem": "St. John's wort washout too short (9 days, 14 required)"}
    b = dict(a, section_id="s2", problem="St John's wort washout too short: 9 days instead of 14")
    c = {"email_quote": "ECG done after the dose", "section_id": "s3", "problem": "ECG done after dosing"}
    assert reader.same_problem(a, b) and not reader.same_problem(a, c)


def test_guide_reads_ocr_text_by_its_table_of_contents(tmp_path):
    from cormon import guide
    src = tmp_path / "p.txt"
    src.write_text("\n".join([
        "=== PAGE 1", "TABLE OF CONTENTS", "TABLE OF CONTENTS .....2", "PROTOCOL SUMMARY .....3",
        "7 INVESTIGATIONAL PRODUCT .....36", "7.1 Acquisition and Storage •• 36", "7.4 Overdose •• 37",
        "Reportable Events 10.3 ...57",
        "=== PAGE 2", "PROTOCOL SUMMARY", "A platform trial.",
        "Trial Protocol", "Version 6.0 Page 2 of 9",
        "=== PAGE 3", "7 INVESTIGATIONAL PRODUCT", "7.1 Acquisition and Storage",
        "Temperature in the area will be controlled, monitored and",
        "recorded, at a minimum daily.", "1. Store as labelled.", "2 Participants must not be dosed",
        "Trial Protocol", "Page 3 of 9 Version 6.0",
        "=== PAGE 4", "7.4 Overdose", "Details are in the RSA.", "Reportable Events 10.3",
        "Report within 24 hours.", "Trial Protocol", "Version 6.0 Page 4 of 9"]))
    secs, visits = guide.read_text(src, "m")
    by = {s.id: s for s in secs}
    assert "m:7.1" in by and "m:7.4" in by and "m:10.3" in by and "m:protocol-summary" in by
    assert "controlled, monitored and recorded, at a minimum daily." in by["m:7.1"].text
    assert "1. Store as labelled." in by["m:7.1"].text.splitlines()
    assert not any("Page" in s.text or "Trial Protocol" in s.text for s in secs) and not visits


def test_packs_keep_only_excerpts_found_in_the_source_and_report_what_is_left_out():
    from cormon import packs
    sec = {"id": "g:5.4", "doc_id": "g", "text": "Take the dose in the morning. A missed dose may be taken the same "
           "day. Do not take two doses within 12 hours. The drug was studied in mice."}
    groups = [{"heading": "dose timing", "items": [
        {"source": "g:5.4", "excerpt": "Take the dose in the morning.", "applies_to": "", "note": ""},
        {"source": "g:5.4", "excerpt": "Doses may be doubled.", "applies_to": "", "note": ""}]}]
    spans, dropped = {}, []
    kept = packs._verify(groups, {"g:5.4": sec}, spans, dropped)
    assert [i["excerpt"] for i in kept[0]["items"]] == ["Take the dose in the morning."] and len(dropped) == 1
    left = [t for _, t in packs._uncovered([sec], spans)]
    assert "Do not take two doses within 12 hours." in left and not any("mice" in t for t in left)
    assert "- Take the dose in the morning. [g:5.4]" in packs.render("dosing", kept)


def test_window_conflict_uses_the_visit_named():
    from cormon.reader import _window_conflict
    notes = ["Baseline: Monday 01 Jun 2026; Baseline window is Day 0 to Day 0 from baseline (01 Jun to 01 Jun 2026); "
             "this date is Day 0: inside the window",
             "Week 16 clinic visit: Wednesday 30 Sep 2026; Week 16 window is Day 105 to Day 119 from baseline "
             "(14 Sep to 28 Sep 2026); this date is Day 121: 2 day(s) after the window closes"]
    late = {"problem": "The Week 16 visit is outside its window.", "why": "Baseline on 1 June makes it Day 121."}
    assert not _window_conflict(late, notes)
    assert _window_conflict({"problem": "Baseline visit outside its window.", "why": ""}, notes[:1])


def test_code_checks_interval_order_count_value():
    from cormon import checks
    rules = [
        {"id": "a", "kind": "interval", "from_event_id": "baseline_visit", "to_event_id": "w16_visit", "unit": "days",
         "min": 105, "max": 119},
        {"id": "b", "kind": "interval", "from_event_id": "predose_pk", "to_event_id": "study_drug_dose",
         "unit": "hours", "min": 0, "max": 2},
        {"id": "c", "kind": "order", "first_event_id": "consent", "then_event_id": "screening_labs"},
        {"id": "d", "kind": "count", "event_id": "study_drug_dose", "unit": "days", "max": 1},
        {"id": "e", "kind": "value", "value_name_id": "egfr", "operator": "lt", "limit": 30, "limit2": None,
         "value_unit": "mL/min"}]
    ev = lambda e, d, t="", s="done", q="": {"event": e, "date": d, "time": t, "status": s, "quote": q or e}
    facts = {"events": [ev("baseline_visit", "2026-06-01"), ev("w16_visit", "2026-09-30"),
                        ev("predose_pk", "2026-06-29", "08:00"), ev("study_drug_dose", "2026-06-29", "11:00"),
                        ev("study_drug_dose", "2026-06-29", "20:00", q="second dose"),
                        ev("consent", "2026-05-02", "10:00"), ev("screening_labs", "2026-05-02", "09:00")],
             "values": [{"value": "egfr", "number": 28, "quote": "eGFR 28"}]}
    bad, ok = checks.evaluate(rules, facts)
    got = {d["rule"]["id"]: d["fact"] for d in bad}
    assert set(got) == {"a", "b", "c", "d", "e"}
    assert "121 days" in got["a"] and "3 hours" in got["b"] and "after screening labs" in got["c"]
    facts["events"][1]["date"] = "2026-09-20"
    facts["events"][6]["status"] = "not_done"
    bad, ok = checks.evaluate(rules, facts)
    assert "a" not in {d["rule"]["id"] for d in bad} and any("111 days" in d["fact"] for d in ok)
    # no clock times: an hours rule is not judged
    facts["events"][2]["time"] = ""
    assert "b" not in {d["rule"]["id"] for d in checks.evaluate(rules, facts)[0]}


def test_code_finding_overlaps_ai_finding_by_email_words():
    from cormon import checks
    code = {"email_quote": "Week 16 visit done on 30 Sept ... baseline was 1 June"}
    assert checks.overlaps(code, {"email_quote": "Pt 004 had the Week 16 visit done on 30 Sept"})
    assert not checks.overlaps(code, {"email_quote": "CSF was spun late"})


def test_shared_dose_time_goes_to_the_better_matching_code_problem():
    from cormon import checks
    predose = {"email_quote": "predose pk 6:30 ... dosed 9:15"}
    postdose = {"email_quote": "dosed 9:15 ... postdose 9:45"}
    ai = {"email_quote": "predose pk 6:30, dosed 9:15"}
    assert checks.overlaps(predose, ai) > checks.overlaps(postdose, ai) > 0
    assert checks.overlaps(postdose, {"email_quote": "postdose 9:45"}) > checks.overlaps(predose, {"email_quote": "postdose 9:45"})


def test_findings_on_different_code_facts_are_not_one_problem():
    from cormon.reader import same_problem
    a = {"email_quote": "predose pk 6:30, dosed 9:15", "section_id": "regimen_g:s010",
         "problem": "The predose PK sample was collected too early before dosing.", "code_fact": "dose 2.75 h after predose"}
    b = {"email_quote": "postdose 9:45", "section_id": "regimen_g:s010",
         "problem": "The postdose PK sample was collected too soon after dosing.", "code_fact": "postdose 0.5 h after dose"}
    assert not same_problem(a, b)
    assert same_problem(a, dict(a, problem="The predose PK sample was taken too early before dosing."))


def test_half_life_takes_the_longest_value_stated():
    from cormon.checks import half_life_days
    assert half_life_days(" 4.2 d ") == 4.2
    assert round(half_life_days(" 3–5 h ") * 24, 6) == 5
    assert round(half_life_days(" 6.8 h (IV); 8.1 h (oral) ") * 24, 6) == 8.1
    assert round(half_life_days(" 31.9 ± 10.5 h (based on radioactivity); 48.4 ± 12.3 h ") * 24, 6) == 48.4


def test_window_conflict_ignores_sampling_hours_and_code_backed_findings():
    from cormon.reader import _window_conflict
    notes = ["Week 8 visit: Tuesday 06 Oct 2026; Week 8 window is Day 49 to Day 63 from baseline "
             "(29 Sep to 13 Oct 2026); this date is Day 56: inside the window"]
    pk = {"problem": "The Week 8 postdose PK sample was collected too late.",
          "why": "16:30 is 8.5 hours after the 08:00 dose, outside the 1 to 8 hour window."}
    assert not _window_conflict(pk, notes)
    visit = {"problem": "The Week 8 visit is outside its window.", "why": ""}
    assert _window_conflict(visit, notes) and not _window_conflict(dict(visit, code_fact="x"), notes)


def test_findings_on_the_same_code_fact_are_one_problem():
    from cormon.reader import _same_fact
    fact = "first study dose (2026-10-12) is 9 days after last carbamazepine (2026-10-03)"
    a = {"code_fact": fact, "problem": "Tegretol stopped too close to first dose."}
    b = {"code_fact": fact, "problem": "Washout not long enough under the medication table."}
    assert _same_fact(a, b) and _same_fact(a, {"from_fact": fact})
    assert not _same_fact(a, {"code_fact": "2 x study drug dose on 2026-10-14"}) and not _same_fact({}, {})


def test_one_message_with_two_problems_goes_out_as_one_email():
    ev = lambda q, d: {"certainty": "clear", "message_quote": q, "doc_quote": d, "doc_title": "Appendix G",
                       "doc_page": 4, "next_step": "Follow up."}
    fridge = {"id": 14, "tier": "medium", "title": "Fridge too warm.", "evidence": ev("fridge 32C", "below 30")}
    call = {"id": 13, "tier": "low", "title": "Week 20 call late.", "evidence": ev("wk20 call", "140 ± 3 days")}
    ps = drafting.bundle_problems(None, [fridge, call])
    assert [(p["n"], p["item_id"], p["tier_label"]) for p in ps] == [(1, 14, "Medium"), (2, 13, "Low")]
    item = dict(fridge, participant_id="G-7", site="S", link="http://x/i", tier_label="Medium", routed_to="CRA")
    ctx = {"study": {"short_name": "DNL", "monitor_signature": "M"}, "item": item, "problems": ps, "bundle": True,
           "also": [], "recipient": {"first_name": "Bo"}, "finding": {"sender_name": "A", "message_date": "1 Jan",
                                                                    "earlier": ""}}
    subject, body = drafting.render(dict(drafting.select("study_drug", "cra", "email", "medium"), ai_edit=False), ctx)
    assert "(+1 more)" in subject
    assert "**1. Fridge too warm.** (Medium, Item #14)" in body and "**2. Week 20 call late.** (Low, Item #13)" in body
    assert '"cleared #13"' in body


def test_a_reply_clears_every_problem_in_the_email_or_the_one_it_names():
    from cormon import pipeline, routing
    oid = routing.enqueue(purpose="email_cra", channel="email", destination="email", body="b", item_id=14,
                          meta={"item_id": 14, "item_ids": [14, 13]})
    rfc = db.q1("SELECT rfc_message_id FROM outbox WHERE id=?", (oid,))["rfc_message_id"]
    msg = {"in_reply_to": rfc}
    assert pipeline._target_items(msg, {"text": "cleared"}) == [14, 13]
    assert pipeline._target_items(msg, {"text": "cleared #13"}) == [13]
    assert pipeline._target_items(msg, {"text": "cleared #99"}) == []  # not in this email: nothing is cleared


def test_unproven_protocol_words_are_not_shown_and_two_such_problems_stay_apart():
    ev = {"certainty": "unsure", "unsure_reason": "the exact words were not found in the protocol",
          "message_quote": "wk8 planned 11/20", "doc_quote": "", "doc_title": "Appendix G", "doc_page": 4,
          "rule_text": "6.3 Week 8 Visit", "headline": "Week 8 late.",
          "additional": [{"headline": "Week 12 late.", "message_quote": "wk8 planned 11/20", "doc_quote": "",
                          "doc_title": "Appendix G", "certainty": "unsure"}]}
    item = {"id": 5, "title": "Week 8 late.", "participant_id": "G-1", "site": "S", "link": "http://x/i",
            "tier": "medium", "tier_label": "Medium", "routed_to": "CRA"}
    ps = drafting.problems(None, item, ev)
    assert [p["title"] for p in ps] == ["Week 8 late.", "Week 12 late."]
    ctx = {"study": {"short_name": "DNL", "monitor_signature": "M"}, "item": item, "problems": ps, "bundle": False,
           "also": [], "recipient": {"first_name": "Bo"},
           "finding": {"sender_name": "A", "message_date": "1 Jan", "earlier": ""}}
    _, body = drafting.render(dict(drafting.select("visit_schedule", "cra", "email", "medium"), ai_edit=False), ctx)
    assert "exact words not found, please read this section: Appendix G" in body and "> \n" not in body


def test_cleared_in_chat_works_in_the_alerts_thread_or_by_number():
    from cormon import pipeline, routing
    for i in (19, 20, 21):
        db.ex("INSERT INTO items (id, study_id, thread_key, first_message_id, last_message_id, dedup_key, kind,"
              " polarity, title, tier, evidence, link_token, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (i, "S1", "t", 1, 1, f"k{i}", "finding", "problem", "x", "medium", "{}", f"l{i}", db.now(), db.now()))
    oid = routing.enqueue(purpose="teams_study_team", channel="teams", destination="teams_study_channel", body="b",
                          item_id=19, meta={"item_ids": [19, 21, 20]})
    db.ex("UPDATE outbox SET external_ref=? WHERE id=?", ("spaces/AA/messages/Hz_0M.Hz_0M", oid))
    in_thread = {"headers": {"_cormon_reply_to": "spaces/AA/threads/Hz_0M"}, "study_id": "S1"}
    assert pipeline._target_items(in_thread, {"text": "cleared"}) == [19, 21, 20]
    assert pipeline._target_items(in_thread, {"text": "cleared #20"}) == [20]
    new_post = {"headers": {}, "study_id": "S1"}
    assert pipeline._target_items(new_post, {"text": "cleared #19"}) == [19]
    assert pipeline._target_items(new_post, {"text": "cleared #99"}) == []
    assert pipeline._target_items(dict(new_post, study_id="S2"), {"text": "cleared #19"}) == []


def _two_messages_about_one_participant():
    from cormon import memory
    ev = lambda e, d, t="", q="": {"event": e, "date": d, "time": t, "status": "done", "quote": q or e}
    first = {"id": 1, "sent_at": "2026-06-01T14:00:00+00:00"}
    memory.save("S1", "G-708-002", first,
                [{"label": "Baseline", "kind": "baseline", "date": "2026-06-01", "time": None, "visit_id": ""}],
                [ev("baseline_visit", "2026-06-01", q="baseline done today"),
                 ev("study_drug_dose", "2026-06-01", "09:00", q="first dose 9am")])
    later = {"id": 3, "sent_at": "2026-11-01T14:00:00+00:00"}
    memory.save("S1", "G708002", later, [{"label": "Week 24", "kind": "visit", "date": "2026-11-01"}], [])
    return {"id": 2, "sent_at": "2026-09-30T14:00:00+00:00"}


def test_earlier_messages_give_the_dates_a_new_message_counts_from():
    from cormon import memory
    msg = _two_messages_about_one_participant()
    past = memory.history("S1", "g-708-002", msg)
    assert [h["message_id"] for h in past] == [1]  # same person however the id is written; not a later message
    assert memory.history("S2", "G-708-002", msg) == []
    added = memory.anchor_dates(past, [{"label": "Week 16", "kind": "visit", "date": "2026-09-30"}])
    assert [d["kind"] for d in added] == ["baseline"] and "earlier message" in added[0]["label"]
    assert memory.anchor_dates(past, [{"label": "Baseline", "kind": "baseline", "date": "2026-06-02"}]) == []
    as_visit = [{"label": "Baseline visit", "kind": "visit", "date": "2026-06-01", "visit_id": "g:v1-baseline"}]
    assert memory.anchor_kind(as_visit[0]) == "baseline" and memory.anchor_dates(past, as_visit) == []


def test_code_checks_pair_an_earlier_fact_with_a_new_one_but_never_raise_old_news():
    from cormon import memory, reader
    msg = _two_messages_about_one_participant()
    past = memory.history("S1", "G-708-002", msg)
    rules = [{"id": "a", "kind": "interval", "from_event_id": "baseline_visit", "to_event_id": "w16_visit",
              "unit": "days", "min": 105, "max": 119},
             {"id": "b", "kind": "interval", "from_event_id": "predose_pk", "to_event_id": "study_drug_dose",
              "unit": "hours", "min": 0, "max": 2},
             {"id": "c", "kind": "interval", "from_event_id": "baseline_visit", "to_event_id": "study_drug_dose",
              "unit": "days", "min": 1, "max": 3}]
    now = {"events": [{"event": "w16_visit", "date": "2026-09-30", "time": "", "status": "done", "quote": "wk16 done"},
                      {"event": "predose_pk", "date": "2026-09-30", "time": "08:00", "status": "done",
                       "quote": "predose 8am"}], "values": []}
    bad, ok, notes = [], [], []
    reader._with_earlier(rules, now, past, bad, ok, notes)
    assert [d["rule"]["id"] for d in bad] == ["a"] and "121 days" in bad[0]["fact"]
    assert bad[0]["quotes"][0].startswith("(earlier message, ") and "wk16 done" in bad[0]["quotes"]
    assert notes and "baseline done today" in notes[0]
    # an old dose is not this sample's dose (hours rule), and baseline-to-first-dose was checked back then
    assert {d["rule"]["id"] for d in bad + ok} == {"a"}


def test_answers_show_only_proven_protocol_words_and_say_so_when_none(monkeypatch):
    from cormon import answers, memory
    sec = {"id": "g:6.9", "kind": "section", "title": "Week 8 Visit", "number": "6.9", "path": "", "doc_id": "p",
           "ord": 1, "topics": ["visits"], "about": "Week 8 visit",
           "text": "Phone visits will occur every 28 days (± 3 days) after the Week 24 visit."}
    wk8 = {"id": "g:v-w8", "kind": "visit", "title": "Week 8", "path": "SCHEDULE", "doc_id": "p", "ord": 2,
           "topics": [], "about": "", "text": "Visit checklist: Week 8 (Clinic), window Day 56 ± 7. From: x."}
    msg = _two_messages_about_one_participant()
    replies = {"answer_pick": {"ids": ["g:6.9", "g:v-w8"]},
               "answer_question": {"answerable": True, "serious_event": False, "calculations": [], "quotes": [
                   {"section_id": "g:6.9", "words": "Phone visits will occur every 28 days (± 3 days)"}],
                   "answer": "Phone visits are every 28 days, give or take 3 days."}}
    monkeypatch.setattr(answers.llm, "structured", lambda task, *a, **k: replies[task])
    q = {"question": "How often are phone visits?", "participant": ""}
    a = answers.answer_one(None, msg, q, [sec, wk8], [sec], "how often are phone visits?")
    assert a["found"] and a["traced"] and "every 28 days (± 3 days)" in a["quotes"][0][1]
    assert 'Protocol, ' in answers.render_full([a]) and answers.FOOTER in answers.render([a])
    replies["answer_question"] = dict(replies["answer_question"], quotes=[
        {"section_id": "g:6.9", "words": "Phone visits will occur every 19 days."}])
    a = answers.answer_one(None, msg, q, [sec, wk8], [sec], "how often are phone visits?")
    assert not a["found"] and answers.NOT_FOUND in answers.render([a])
    # the participant's windows are worked out by code from the Baseline an earlier message gave
    sched = answers.schedule(memory.history("S1", "G-708-002", msg), [wk8])
    assert sched == ["Week 8: Day 49 to Day 63 from baseline (01 Jun 2026, said in the message of "
                     f"{memory.history('S1', 'G-708-002', msg)[0]['when']}) = Mon 20 Jul 2026 to Mon 03 Aug 2026"]
    assert not answers._numbers_traced("Visit between 20 and 35 days", "Day 20 to Day 30")
    assert answers._numbers_traced("Window opens 03 Aug", "Mon 3 Aug 2026")
    assert answers.asks_something("When is her week 8 visit?") and not answers.asks_something("wk8 done 7/22.")


def test_one_departure_worded_two_ways_on_the_same_email_words_is_one_problem():
    from cormon.reader import same_problem
    a = {"problem": "The Week 12 phone call was done outside the allowed visit window.", "section_id": "g:s010",
         "email_quote": "Week 12 phone call done 9/30."}
    b = dict(a, problem="The Week 12 Telephone Visit was done outside the protocol window.", section_id="g:6.4")
    assert same_problem(a, b)
    assert not same_problem(a, dict(b, problem="The Week 12 call missed the ALSFRS-R questionnaire."))
    assert not same_problem(a, dict(b, problem="The Week 16 Telephone Visit was done outside the protocol window."))


def test_a_visits_window_is_quoted_from_the_schedule_table_not_the_checklist():
    from cormon import answers
    soa = {"id": "g:s011", "kind": "section", "title": "SOA ATE", "text": "Schedule table (SOA ATE). Columns: "
           "Week 2 / Week 26 / Phone / Day 14 ± 3 | Week 12 / Week 36 / Phone / Day 84 ± 3 | Week 120 / Clinic / x"}
    v = {"id": "g:v2-week-12", "kind": "visit", "title": "Study Week in ATE: Week 12 = Study Week from Baseline: Week 36",
         "text": "Visit checklist: Study Week in ATE: Week 12 = ... (Phone), window Day 84 ± 3. From: SOA ATE.\n- ECG"}
    assert answers.schedule_cell(v, [soa]) == (soa, "Week 12 / Week 36 / Phone / Day 84 ± 3")
    assert answers._clean('Day 84 ± 3 [g:6.4].\n"Week 12 / Phone" [g:s010]') == "Day 84 ± 3."


def test_sums_are_worked_out_by_code_from_the_protocols_numbers_only():
    from cormon import answers
    backing = '"Week 2 / Phone / Day 14 ± 3" "Week 12 / Phone / Day 84 ± 3"'
    assert answers.calculate("84 - 14", backing) == "84 - 14 = 70"
    assert answers.calculate("(84 + 3) - (14 - 3)", backing) == "(84 + 3) - (14 - 3) = 76"
    assert answers.calculate("90 - 14", backing) is None           # 90 is not in the documents
    assert answers.calculate("__import__('os')", backing) is None  # only plain arithmetic
    a = {"question": "Max gap between phone visits?", "quotes": [("SOA", "Week 12 / Phone / Day 84 ± 3")],
         "found": True, "traced": True, "serious": False, "text": "Not stated directly; the longest gap is 70 days.",
         "sums": [("Week 2 to Week 12", "84 - 14 = 70")]}
    shown = answers.render_full([a])
    assert "Worked out by code: Week 2 to Week 12: 84 - 14 = 70" in shown and answers.SERIOUS_LINE not in shown
    assert answers.SERIOUS_LINE in answers.render([dict(a, serious=True, found=False)])


def test_the_chat_reply_is_short_and_never_adds_a_number(monkeypatch):
    from cormon import answers
    a = {"question": "Max gap between phone visits?", "found": True, "traced": True, "serious": False,
         "quotes": [("SOA", "Week 2 / Phone / Day 14 ± 3"), ("SOA", "Week 12 / Phone / Day 84 ± 3")],
         "sums": [("the maximum gap", "84 - 14 = 70")],
         "text": "70 days. It is not stated directly; the phone visits are Day 14 and Day 84, so 84 - 14 = 70."}
    said = {"text": "The longest gap is 70 days (Week 2 to Week 12), give or take 3 days."}
    monkeypatch.setattr(answers.llm, "structured", lambda *a, **k: said)
    assert answers.plain(a, "Study coordinator") == said["text"]
    said["text"] = "The longest gap is 76 days."  # a number the checked answer never gave
    assert answers.plain(a, "Study coordinator") == a["text"]
    reply = answers.render([dict(a, plain="The longest gap is 70 days.")], "https://x/answers/t")
    assert "Worked out by code" not in reply and "<https://x/answers/t|" in reply
    assert "Details and the protocol's words: https://x/answers/t" in answers.render(
        [dict(a, plain="70 days.")], "https://x/answers/t", chat=False)
    assert answers.plain(dict(a, found=False), "x") == answers.NOT_FOUND
    dated = dict(a, text="The Week 4 window is 19 Oct 2026 to 2 Nov 2026. It counts from baseline on 28 Sep 2026.",
                 sums=[], quotes=[("SOA", "Week 4 / Clinic / Day 28 ± 7")])
    said["text"] = "Week 4 is Day 28 ± 7."  # the participant's dates swapped for the protocol's day numbers
    assert answers.plain(dated, "x") == dated["text"]
    said["text"] = "Week 4 is due between 19 Oct and 2 Nov 2026."
    assert answers.plain(dated, "x") == said["text"]


def test_a_sum_written_in_the_answer_counts_only_when_code_agrees():
    from cormon import answers
    q = {"question": "Longest gap between phone visits?", "participant": ""}
    def proof(text):
        res = {"answer": text, "quotes": [], "calculations": []}
        return answers._proof(res, q, [], [], [], [], "phone visits at Day 14 and Day 84", [], [])
    assert proof("70 days: Day 84 - Day 14 = 70.")["traced"]
    assert not proof("71 days: Day 84 - Day 14 = 71.")["traced"]
    gap = {"question": "What is the maximum number of days between phone visits?", "participant": ""}
    cells = [("SOA", "Week 2 / Phone / Day 14 ± 3"), ("SOA", "Week 12 / Phone / Day 84 ± 3"),
             ("SOA", "Week 20 / Phone / Day 140 ± 3")]
    import unittest.mock as mock
    with mock.patch.object(answers, "schedule_cell", lambda v, s: None), \
         mock.patch.object(answers.reader, "_exact_words", lambda words, *a: ({"kind": "section", "title": "SOA"}, words)), \
         mock.patch.object(answers.reader, "_heading", lambda s: "SOA"):
        res = {"answer": "56 days: 140 - 84 = 56.", "quotes": [{"section_id": "x", "words": w} for _, w in cells],
               "calculations": []}
        wrong = answers._proof(res, gap, [], [], [], [], "", [], [])
        right = answers._proof(dict(res, answer="70 days: 84 - 14 = 70."), gap, [], [], [], [], "", [], [])
    assert not wrong["traced"] and "Day 14 to Day 84: 70" in wrong["conflict"]
    assert right["traced"] and ("the maximum gap", "84 - 14 = 70") in right["sums"]
    res = {"answer": "Days 81 to 87.", "quotes": [], "calculations": []}
    assert answers._proof(res, q, [], [], [], [], "Week 12 is Day 84 ± 3", [], [])["traced"]


def test_the_full_text_finds_an_exception_the_headings_do_not_show():
    from cormon import answers
    secs = [{"id": "a", "title": "Missed Visits and Procedures", "text": "Procedures not performed will be reported "
             "as protocol deviations."},
            {"id": "b", "title": "Patient Reported Outcomes", "text": "PROs not performed due to a participant "
             "language barrier are not reported as protocol deviations."},
            {"id": "c", "title": "Storage", "text": "Keep the drug at room temperature."}]
    got = [s["id"] for s in answers.lexical("ALSAQ-40 not done because of a language barrier: deviation?", secs)]
    assert got[0] == "b" and "c" not in got


def test_a_late_visit_alert_says_how_late_and_why_in_dates():
    from cormon import checks
    n = {"from": "baseline_visit", "to": "w4_visit", "from_date": "2026-09-01", "from_time": None,
         "to_date": "2026-10-16", "to_time": None, "gap": 45, "unit": "days", "min": 21, "max": 35, "status": "done"}
    said = checks.explain(n)
    assert said["headline"] == "Week 4 visit was 10 days late."
    assert said["detail"] == ("Done 16 Oct 2026 (Day 45 after Baseline on 1 Sep 2026). "
                              "Allowed: 22 Sep 2026 to 6 Oct 2026 (Day 21 to 35).")
    assert said["next_step"] == "Record a protocol deviation."
    planned = checks.explain(dict(n, gap=18, to_date="2026-09-19", status="planned"))
    assert planned["headline"] == "Week 4 visit is planned 3 days early."
    assert planned["next_step"].startswith("Move it to between 22 Sep 2026 and 6 Oct 2026")
    assert "headline" not in checks.explain(dict(n, gap=30, to_date="2026-10-01"))  # inside the window
    # a gap between two other events gets only the dated "why"; late or early is not sure there
    pk = checks.explain({"from": "dose", "to": "pk_sample", "from_date": "2026-10-16", "from_time": "09:00",
                         "to_date": "2026-10-16", "to_time": "09:20", "gap": 0.33, "unit": "hours",
                         "min": 0.5, "max": None, "status": "done"})
    assert set(pk) == {"detail"}
    assert pk["detail"] == ("PK sample: 09:20 on 16 Oct 2026, 20 minutes after dose (09:00 on 16 Oct 2026). "
                            "Allowed: at least 30 minutes.")
    assert checks.pretty("ate_w8") == checks.pretty("w8_ate") == "ATE Week 8 visit"
    assert checks.pretty("w24_csf") == "Week 24 CSF" and checks.pretty("last_study_dose") == "last study dose"
    assert "headline" not in checks.explain(dict(n, **{"from": "previous_ate_visit", "to": "next_ate_post_w52"}))
    assert checks.explain(None) == {}


def test_the_alert_shows_earlier_messages_apart_and_no_footnote_marks():
    p = drafting._shown({"message_quote": "G-931-001 week 4 visit done today. ... (earlier message, 16 Oct 2026 "
                                          "16:38) baseline visit and first dose done 9/1, all fine.",
                         "doc_quote": "Week 4 [12] / Clinic / Day 28 ± 7"})
    assert p["message_quote"] == "G-931-001 week 4 visit done today."
    assert p["earlier_quotes"] == [{"when": "16 Oct 2026 16:38",
                                    "words": "baseline visit and first dose done 9/1, all fine."}]
    assert p["doc_quote"] == "Week 4 / Clinic / Day 28 ± 7"
    gaps = drafting._shown({"message_quote": "G-960-001 week 4 visit done today. ... (earlier message, 16 Oct 2026 "
                                             "18:48) baseline visit ... done 9/1", "doc_quote": ""})
    assert gaps["message_quote"] == "G-960-001 week 4 visit done today."
    assert gaps["earlier_quotes"] == [{"when": "16 Oct 2026 18:48", "words": "baseline visit ... done 9/1"}]
    plain = drafting._shown({"message_quote": "Dose at 09:00 ... PK at 09:20", "doc_quote": ""})
    assert plain["message_quote"] == "Dose at 09:00 ... PK at 09:20" and plain["earlier_quotes"] == []


def test_the_bot_speaks_in_a_group_chat_only_when_talked_to():
    from cormon import routing
    S = "spaces/AAQ"
    group = lambda text, thread="t1", **h: {"channel": "teams", "visibility": "channel", "body_text": text,
                                             "thread_key": f"teams:{S}/threads/{thread}", "headers": h}
    assert capture.turn(group("G-1 week 4 visit done today.")) == "quiet"
    assert capture.turn(group("@Huma bot is this ok?", _cormon_to_bot=True, _cormon_mentions=["bot@huma.ai"])) \
        == "addressed"
    assert capture.turn(group("Bot, is a Day 40 week 4 visit late?")) == "addressed"
    assert capture.turn(group("Hey bot when is week 8?")) == "addressed"
    assert capture.turn(group("Bottles arrived for the kit.")) == "quiet"
    assert capture.turn(dict(group("thanks"), visibility="direct")) == "direct"
    assert capture.turn({"channel": "email", "body_text": "bot, hi"}) == "email"
    # a reply in a thread the bot started (its alert) is talking to the bot, unless it names someone else
    oid = routing.enqueue(purpose="teams_study_team", channel="teams", destination="teams_study_channel", body="x")
    db.ex("UPDATE outbox SET external_ref=? WHERE id=?", (f"{S}/messages/alert1.alert1", oid))
    assert capture.turn(group("Is the window counted from baseline?", thread="alert1")) == "addressed"
    assert capture.turn(group("@Peter can you look?", thread="alert1", _cormon_mentions=["peter@huma.ai"])) \
        == "quiet"
    # the bot's reply inside someone else's thread does not make that thread the bot's
    oid = routing.enqueue(purpose="question_answer", channel="teams", destination="teams_study_channel", body="x")
    db.ex("UPDATE outbox SET external_ref=? WHERE id=?", (f"{S}/messages/t1.reply9", oid))
    assert capture.turn(group("Interesting, does it need the pharmacy manual?")) == "quiet"


def test_google_chat_notes_who_a_message_mentions():
    from cormon.channels.gchat import GoogleChatChannel
    ch = GoogleChatChannel(session=object())
    ch._me, ch._users = "users/1", {"users/1": "bot@huma.ai", "users/2": "peter@huma.ai"}
    S = "spaces/AAQ"
    msg = lambda *who: {"name": f"{S}/messages/m", "sender": {"type": "HUMAN", "name": "users/2"},
                        "text": "hello", "createTime": "2026-10-16T20:00:00Z", "thread": {"name": f"{S}/threads/m"},
                        "annotations": [{"type": "USER_MENTION", "userMention": {"user": u}} for u in who]}
    to_bot = ch._to_inbound(msg({"name": "users/1", "email": "bot@huma.ai"}), study_id="x", visibility="channel")
    assert to_bot.headers == {"_cormon_mentions": ["bot@huma.ai"], "_cormon_to_bot": True}
    other = ch._to_inbound(msg({"name": "users/3", "email": "eliot@huma.ai"}), study_id="x", visibility="channel")
    assert other.headers == {"_cormon_mentions": ["eliot@huma.ai"], "_cormon_to_bot": False}
    assert ch._to_inbound(msg(), study_id="x", visibility="channel").headers == {}


def test_a_message_to_the_bot_always_gets_a_reply(monkeypatch):
    from cormon import pipeline
    said = []
    monkeypatch.setattr(pipeline, "_answer_in_chat", lambda study, msg, text, purpose="": said.append(text))
    found = {7: {"id": 7, "kind": "finding", "state": "open", "counts": 1, "link_token": "a", "evidence": {}},
             8: {"id": 8, "kind": "good_news", "state": "open", "counts": 1, "link_token": "b", "evidence": {}}}
    monkeypatch.setattr(pipeline.items, "get", lambda i: found[i])
    monkeypatch.setattr(pipeline.drafting, "problems", lambda study, it, ev: [
        {"title": "Week 4 visit was 10 days late.", "unsure": False, "next_step": "Record a protocol deviation.",
         "detail": "Done 16 Oct 2026 (Day 45 after Baseline on 1 Sep 2026)."}])
    direct = {"visibility": "direct", "body_text": "G-1 week 4 visit done today."}
    run = lambda msg, created=(), repeat=(), trial=True: pipeline._acknowledge(
        None, msg, {"items_created": list(created), "items_repeat": list(repeat)}, trial) or said[-1]
    shown = run(direct, created=[7]).split("<br>")
    assert shown[:4] == ["Thanks. I found 1 problem:", "<b>1. Week 4 visit was 10 days late.</b>",
                         "Done 16 Oct 2026 (Day 45 after Baseline on 1 Sep 2026).",
                         "<b>What to do:</b> Record a protocol deviation."]
    assert shown[4].startswith("I've sent it to the CRA. Details: <a href=") and "Item #7" in shown[4]
    group = {"visibility": "channel", "body_text": "bot, G-1 week 4 visit done today."}
    assert run(group, created=[7]).startswith("Thanks. I found 1 problem to check and raised it: <a href=")
    assert "Item #7" in run(direct, repeat=[7]) and run(direct, repeat=[7]).startswith("This is already raised")
    assert run(direct, created=[8]) == "Thanks for letting me know."
    assert run(dict(direct, body_text="Is a Day 30 week 4 visit ok?")) == \
        "I checked this against the protocol and found no problem."
    assert run({"visibility": "channel", "body_text": "Bot, check G-1 week 4 done today"}) == \
        "I checked this against the protocol and found no problem."
    assert run(dict(direct, body_text="thanks!"), trial=False) == "Thanks for letting me know."


def test_group_chatter_gets_only_the_serious_harm_warning(monkeypatch):
    from cormon import answers
    asked = []
    qs = [{"question": answers.REPORTING_QUESTION, "participant": "G-1", "harm": True},
          {"question": "When is the next visit?", "participant": "G-1", "harm": False}]
    monkeypatch.setattr(answers.guide, "checker", lambda s: {})
    monkeypatch.setattr(answers, "find", lambda text, use_cache=True: qs)
    monkeypatch.setattr(answers.guide, "load", lambda sid: [])
    monkeypatch.setattr(answers.reader, "view", lambda s, every: [])
    monkeypatch.setattr(answers, "answer_one", lambda st, m, q, *a: asked.append(q["question"]) or
                        {"question": q["question"], "serious": True})
    monkeypatch.setattr(answers, "plain", lambda a, role, use_cache=True: "")
    study = type("S", (), {"study_id": "x", "person_by_email": lambda self, e: None})()
    text = "G-1 was admitted to hospital last night. When is the next visit?"
    answers.answer_message(study, {}, text, harm_only=True)
    assert asked == [answers.REPORTING_QUESTION]
    asked.clear()
    answers.answer_message(study, {}, text)
    assert asked == [answers.REPORTING_QUESTION, "When is the next visit?"]
    asked.clear()
    assert answers.answer_message(study, {}, "When is G-1's next visit?", harm_only=True) == [] and not asked


def test_a_direct_chat_belongs_to_the_study_the_person_last_wrote_in(monkeypatch):
    from cormon import config
    from cormon.channels import study_for_person
    one = lambda sid: type("S", (), {"study_id": sid, "people": [],
                                     "person_by_email": lambda self, e: object() if e == "john@huma.ai" else None})()
    monkeypatch.setattr(config, "studies", lambda: {"A": one("A"), "B": one("B")})
    assert study_for_person("john@huma.ai") == "A"
    db.ex("INSERT INTO messages (channel, external_id, thread_key, study_id, route, visibility, from_addr, body_text,"
          " sent_at, received_at, status) VALUES ('teams','m1','t','B','study','channel','john@huma.ai','x','',"
          "'','processed')")
    assert study_for_person("john@huma.ai") == "B"
    assert study_for_person("nobody@x.org") is None


def test_a_refused_threaded_chat_reply_is_sent_plain():
    from cormon.channels.gchat import GoogleChatChannel
    sent = []
    class R:
        def __init__(self, code): self.status_code, self.text = code, "PERMISSION_DENIED"
        def json(self): return {"name": "spaces/D/messages/1"}
    class Session:
        def post(self, url, json, params=None, timeout=0):
            sent.append((json, params))
            return R(403 if "thread" in json else 200)
    ch = GoogleChatChannel(session=Session())
    out = ch.send({"body": "Thanks for letting me know.",
                   "meta": {"target": {"space": "spaces/D", "thread": "spaces/D/threads/t"}}})
    assert out["external_ref"] == "spaces/D/messages/1" and sent[-1] == ({"text": "Thanks for letting me know."}, None)


def test_a_problem_told_to_the_bot_1_on_1_is_not_posted_in_the_group():
    from cormon import routing
    entries = [{"destination": "clearing_list"}, {"destination": "teams_study_channel"},
               {"destination": "teams_alert_channel"}, {"destination": "email", "to_role": "cra"}]
    finding, news = {"kind": "finding"}, {"kind": "good_news"}
    kept = lambda item, turn: [e["destination"] for e in routing._chat_posts(item, turn, entries)]
    assert kept(finding, "direct") == ["clearing_list", "email"]
    assert kept(finding, "quiet") == kept(finding, "addressed") == kept(finding, "email") == \
        ["clearing_list", "teams_study_channel", "teams_alert_channel", "email"]
    assert kept(news, "quiet") == ["clearing_list", "email"] and len(kept(news, "addressed")) == 4


def test_group_chats_the_bot_is_added_to_are_read_for_people_on_a_study(monkeypatch):
    from cormon import config
    from cormon.channels.gchat import GoogleChatChannel

    study = config.study("NCT04098666")
    monkeypatch.setattr(study, "teams", {"study_channel": {"space": "spaces/S1"},
                                         "alert_channel": {"space": "spaces/gAlert"}})
    monkeypatch.setattr(study, "person_by_email", lambda e: object() if e == "user7@site.org" else None)
    monkeypatch.setattr(config, "studies", lambda: {study.study_id: study})
    db.set_state("gchat_since", "2026-10-12T10:00:00+00:00")
    msg = lambda name, user, t: {"name": name, "sender": {"name": f"users/{user}", "type": "HUMAN"},
                                 "createTime": t, "text": "bot, which criteria are hardest?",
                                 "thread": {"name": name.split("/messages/")[0] + "/threads/T"}}
    fake = _FakeGoogle({
        "spaces/S1": [],
        "spaces/gPeter": [msg("spaces/gPeter/messages/1", 7, "2026-10-12T10:05:00Z"),
                          msg("spaces/gPeter/messages/2", 9, "2026-10-12T10:06:00Z")],   # not on the study
        "spaces/gAlert": [msg("spaces/gAlert/messages/3", 7, "2026-10-12T10:07:00Z")],  # the alert room: not read
    })
    got = GoogleChatChannel(session=fake).fetch()
    assert [(i.external_id, i.visibility, i.study_id) for i in got] == [
        ("spaces/gPeter/messages/1", "group", "NCT04098666"), ("spaces/gPeter/messages/2", "group", None)]


def test_a_group_chats_alert_stays_in_that_chat(monkeypatch):
    from cormon import routing
    from cormon.channels import study_for_person
    group = {"channel": "teams", "visibility": "group", "thread_key": "teams:spaces/G/threads/T"}
    assert routing.group_thread(group) == {"space": "spaces/G", "thread": "spaces/G/threads/T", "key": "group"}
    assert routing.group_thread(dict(group, visibility="channel")) is None
    entries = [{"destination": "clearing_list"}, {"destination": "teams_study_channel"},
               {"destination": "teams_alert_channel"}, {"destination": "email", "to_role": "cra"}]
    kept = lambda msg, es=entries: [e["destination"] for e in routing._in_group(msg, es)]
    assert kept(group) == ["clearing_list", "teams_alert_channel", "email"]
    assert kept(group, entries[:2]) == ["clearing_list", "teams_study_channel"]
    assert kept(dict(group, visibility="channel")) == [e["destination"] for e in entries]
    study = type("S", (), {"teams": {"alert_channel": {"name": "DNL343 URGENT"}}})()
    assert routing._routed_to(study, entries[2:3], {}, group) == "this chat"
    assert routing._routed_to(study, entries[2:3], {}, dict(group, visibility="channel")) == "the DNL343 URGENT room"
    # a group message talks to the bot the same way as in the study space
    assert capture.turn(dict(group, body_text="G-1 week 4 done", headers={})) == "quiet"
    assert capture.turn(dict(group, body_text="bot, is week 4 late?", headers={})) == "addressed"
    assert study_for_person("nobody@x.org", known_only=True) is None


def test_a_question_about_the_rules_is_answered_not_raised():
    from cormon import pipeline
    asked, planned = {"status": "asked"}, {"status": "planned"}
    what_if = [{"temporality": "hypothetical"}]
    assert pipeline._only_a_question(asked, None, what_if)
    assert pipeline._only_a_question(asked, None, [])
    assert not pipeline._only_a_question(asked, "G-1", what_if)                        # about a participant
    assert not pipeline._only_a_question(planned, None, what_if)                       # a plan at the site
    assert not pipeline._only_a_question(asked, None, what_if + [{"temporality": "current"}])  # something happened


def test_replies_in_a_thread_that_began_by_talking_to_the_bot_are_to_the_bot():
    T = "teams:spaces/G/threads/t9"
    add = lambda text, headers: db.ex(
        "INSERT INTO messages (channel, external_id, thread_key, study_id, route, visibility, from_addr, body_text,"
        " headers, sent_at, received_at, status) VALUES ('teams',?,?,'x','study','group','e@huma.ai',?,?,'','',"
        "'processed')", (f"m{text[:9]}", T, text, db.js(headers)))
    reply = lambda i, text, **h: {"id": i, "channel": "teams", "visibility": "group", "body_text": text,
                                  "thread_key": T, "headers": h}
    first = add("bot, can they take sleeping pills?", {})
    later = add("What if they take St. John's wort?", {})
    assert capture.turn(reply(later, "What if they take St. John's wort?")) == "addressed"
    assert capture.turn(reply(later, "@Peter can you look?", _cormon_mentions=["peter@huma.ai"])) == "quiet"
    assert capture.turn(reply(first, "bot, can they take sleeping pills?")) == "addressed"
    db.ex("UPDATE messages SET body_text='Peter, are you on the 3pm call?' WHERE id=?", (first,))
    assert capture.turn(reply(later, "What if they take St. John's wort?")) == "quiet"   # began as chatter


def test_what_the_bot_does_not_do_is_turned_down_in_fixed_words(monkeypatch):
    from cormon import asks
    posted, attached = [], []
    monkeypatch.setattr(asks.answers, "_post", lambda study, msg, text, files=None:
                        posted.append(text) or attached.append(files) or 1)
    monkeypatch.setattr(asks.answers, "save_page", lambda *a: "https://x/answers/tok")
    monkeypatch.setattr(asks.answers, "facts", lambda study, msg, topic, text, use_cache=True: {
        "question": "q", "text": "t", "sums": [], "quotes": []} if topic in ("eligibility criteria", "enrollment")
        else None)
    study = type("S", (), {"study_id": "x", "title": "Study X"})()
    msg = {"id": 1, "channel": "teams", "visibility": "direct"}
    run = lambda *reqs: (monkeypatch.setattr(asks, "sort", lambda text, use_cache=True: [
        {"request": "r", "kind": k, "topic": t} for k, t in reqs]), asks.decline(study, msg, "m"))[1]
    assert run(("study", "")) is None and run() is None and run(("other", "")) is None and not posted
    got = run(("improper", ""))
    assert got["only"] and posted[-1] == asks.DECLINE["improper"]
    got = run(("about_bot", ""), ("study", ""))
    assert not got["only"] and posted[-1] == asks.DECLINE["about_bot"]   # the study question is still answered
    assert attached[-1] == []                                           # nothing attached to a refusal
    run(("opinion", "eligibility criteria"))
    assert posted[-1].startswith(asks.OPINION) and "<https://x/answers/tok.md|Facts from the protocol>" in posted[-1]
    assert "use another AI" in posted[-1] and attached[-1][0]["name"] == "facts-eligibility-criteria.md"
    assert "# Facts from the study documents" in attached[-1][0]["text"]
    run(("task", "enrollment"))
    assert posted[-1].startswith(asks.LEAD["task"]) and "attached file" in posted[-1]
    assert attached[-1][0]["name"] == "facts-enrollment.md"
    run(("opinion", "the weather"))
    assert posted[-1] == f"{asks.OPINION} The study documents don't cover the weather, so please ask your CRA."
    assert attached[-1] == []
    run(("task", ""))
    assert posted[-1] == f"{asks.LEAD['task']} The study documents don't cover that." and attached[-1] == []
    msg.update(visibility="group")                  # a group chat: no document facts, an offer of a 1-on-1 chat
    run(("opinion", "eligibility criteria"), ("task", "enrollment"))
    assert posted[-1] == f"{asks.GROUP['opinion']}\n\n{asks.GROUP['task']}" and attached[-1] == []
    assert "answers/" not in posted[-1]
    run(("improper", ""))
    assert posted[-1] == asks.DECLINE["improper"]
    msg.update(channel="email", visibility="cc")    # an email reply goes to its sender only
    run(("opinion", "eligibility criteria"))
    assert attached[-1][0]["name"] == "facts-eligibility-criteria.md"
    assert "Facts from the protocol: https://x/answers/tok.md" in posted[-1]


def test_an_email_reply_carries_the_facts_file_as_an_attachment():
    from cormon.channels.sim import build_mime
    row = {"from_addr": "bot@x.org", "to_addrs": ["site@x.org"], "cc_addrs": [], "subject": "Re: q", "body": "Hi",
           "meta": {"files": [{"name": "facts.md", "text": "# Facts\nAge 18 or older."}]}}
    msg = build_mime(row)
    files = [p for p in msg.iter_attachments()]
    assert [(p.get_filename(), p.get_content_type()) for p in files] == [("facts.md", "text/markdown")]
    assert "Age 18 or older." in files[0].get_content() and msg.get_body(("plain",)).get_content().strip() == "Hi"
    assert not list(build_mime(dict(row, meta={})).iter_attachments())


def test_the_facts_file_has_the_protocols_words_and_a_question_for_another_ai():
    from cormon import answers
    page = {"message_text": "@Huma bot Which criteria will be hardest?", "answers": db.js([
        {"question": "What do the study documents say about eligibility?", "text": "Inclusion needs SVC >= 50%.",
         "sums": [], "quotes": [["Section 5.1", "Slow vital capacity (SVC) of at least 50% of predicted"]]}])}
    out = answers.facts_file(page, "HEALEY Regimen G")
    assert '- Section 5.1: "Slow vital capacity (SVC) of at least 50% of predicted"' in out
    assert "Using only the facts and protocol sections above from the HEALEY Regimen G documents, help with this: " \
        "Which criteria will be hardest?" in out and "Asked: Which criteria will be hardest?" in out
    assert "in full" not in out and "Left out" not in out


def test_the_facts_file_holds_whole_protocol_sections_up_to_a_size_limit(monkeypatch):
    from cormon import answers, guide
    secs = [{"id": f"m:{n}", "kind": "section", "number": n, "title": t, "text": x, "doc_id": "m", "topics": ["eligibility"]}
            for n, t, x in [("5.2.1", "Inclusion", "1. ALS.\n2. Age 18 or older."), ("5.2.2", "Exclusion", "1. Unstable."),
                            ("9.9", "Huge", "x" * answers.FACTS_CHARS)]]
    monkeypatch.setattr(guide, "load", lambda sid: secs)
    monkeypatch.setattr(answers.reader, "view", lambda study, items: items)
    monkeypatch.setattr(answers, "answer_one", lambda *a: {
        "question": "q", "text": "t", "sums": [], "found": True,
        "quotes": [("5.2.2 Exclusion", "Unstable.")],
        "chosen": ["m:5.2.1", "m:9.9", "m:5.2.2"]})
    a = answers.facts(type("S", (), {"study_id": "s"})(), {}, "eligibility", "e")
    assert a["sections"] == [("5.2.2 Exclusion", "1. Unstable."), ("5.2.1 Inclusion", "1. ALS.\n2. Age 18 or older.")]
    assert a["left_out"] == ["9.9 Huge"]
    out = answers.facts_file({"message_text": "hardest?", "answers": db.js([a])}, "G")
    assert "#### 5.2.1 Inclusion\n\n1. ALS.\n2. Age 18 or older." in out and "Left out to keep this file short" in out


def test_a_facts_file_is_attached_to_the_chat_post_and_checked_like_the_text():
    from cormon import routing
    from cormon.channels.gchat import GoogleChatChannel
    calls = []
    class R:
        def __init__(self, code, data): self.status_code, self.text, self._d = code, "x", data
        def json(self): return self._d
    class Session:
        fail = False
        def post(self, url, json=None, data=None, params=None, headers=None, timeout=0):
            calls.append((url, json, data, params))
            if "attachments:upload" in url:
                return R(500, {}) if self.fail else R(200, {"attachmentDataRef": {"resourceName": "r1"}})
            return R(200, {"name": "spaces/G/messages/1"})
    s = Session()
    row = {"body": "Facts are attached.", "meta": {"target": {"space": "spaces/G", "thread": "spaces/G/threads/t"},
                                                   "files": [{"name": "facts.md", "text": "# Facts"}]}}
    GoogleChatChannel(session=s).send(row)
    up, msg = calls[-2], calls[-1]
    assert up[0].endswith("/upload/v1/spaces/G/attachments:upload") and up[3] == {"uploadType": "multipart"}
    assert b'{"filename": "facts.md"}' in up[2] and b"# Facts" in up[2]
    assert msg[1]["attachment"] == [{"attachmentDataRef": {"resourceName": "r1"}}]
    s.fail = True
    GoogleChatChannel(session=s).send(row)
    assert "attachment" not in calls[-1][1] and calls[-1][1]["text"] == "Facts are attached."   # still sent
    oid = routing.enqueue(purpose="question_answer", channel="teams", destination="teams_study_channel", body="ok",
                          meta={"files": [{"name": "f.md", "text": "server at /opt/cormon/data/x"}]})
    row = db.q1("SELECT body, meta FROM outbox WHERE id=?", (oid,))
    assert row["body"] == routing.BLOCKED and "files" not in db.unjs(row["meta"])


def test_a_question_the_bot_cannot_answer_says_what_it_can_do(monkeypatch):
    from cormon import pipeline
    said = []
    monkeypatch.setattr(pipeline, "_answer_in_chat", lambda study, msg, text, purpose="": said.append(text))
    pipeline._acknowledge(None, {"visibility": "channel", "body_text": "@Huma bot what's the wifi password?"},
                          {"items_created": [], "items_repeat": []}, False)
    assert said[-1] == pipeline.CAN_HELP
    pipeline._acknowledge(None, {"visibility": "channel", "body_text": "Hmm, that's odd."},
                          {"items_created": [], "items_repeat": []}, False)
    assert said[-1] == "Thanks for letting me know."


def test_an_alert_names_the_senders_real_role():
    from cormon import config
    study = config.study("NCT05842941")
    assert drafting.sender_ctx(study, {"sender_email": "johnrohit94@gmail.com"})["wrote"] == "Site wrote"
    assert drafting.sender_ctx(study, {"sender_email": "someone@othersite.org"})["sender_is_site"]   # not listed
    tester = drafting.sender_ctx(study, {"sender_email": "eliot@huma.ai"})
    assert tester == {"sender_is_site": False, "sender_role": "Study team member", "wrote": "Study team member wrote"}
    ev = {"certainty": "clear", "message_quote": "help me redact the record", "doc_quote": "", "doc_title": "Master",
          "doc_page": None, "rule_text": "12.6", "next_step": "Do not redact the record.",
          "sender_email": "eliot@huma.ai", "sender_name": "Eliot Levitt", "message_date": "2026-10-19T15:51:10Z"}
    item = {"id": 47, "title": "Asked to hide a record.", "tier": "immediate", "participant_id": None,
            "site": "site-01", "pattern_class": "study_drug", "link_token": "a", "evidence": ev}
    tpl = lambda who: dict(drafting.select("study_drug", who, "email", "immediate"), ai_edit=False)
    _, cra = drafting.render(tpl("cra"), drafting.item_context(study, item, None))
    _, sponsor = drafting.render(tpl("clinops_lead"), drafting.item_context(study, item, None))
    assert "please call Eliot Levitt (Study team member), who sent it, then:" in cra
    assert "Study team member wrote (Eliot Levitt," in cra and "Site wrote" not in cra
    assert "The site has been told" not in cra and "John Rohit" not in sponsor
    ev["sender_email"], ev["sender_name"] = "johnrohit94@gmail.com", "John Rohit"
    _, cra = drafting.render(tpl("cra"), drafting.item_context(study, item, None))
    assert "please call John Rohit at Site 01 (MGH), then:" in cra and "Site wrote (John Rohit," in cra
    assert "The site has been told only that a possible issue was noted" in cra

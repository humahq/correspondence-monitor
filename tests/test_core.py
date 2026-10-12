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
                       "page": 3, "doc_quote": "d", "unsure": True, "unsure_reason": "Years of education not given.", "earlier": ""},
           "also": [], "recipient": {"first_name": "Bo"}}
    _, body = drafting.render(t, ctx)
    assert "Please check (not sure): Sample late" in body
    assert "Why I am not sure: Years of education not given." in body
    _, body = drafting.render(t, dict(ctx, finding=dict(ctx["finding"], unsure=False)))
    assert "not sure:" not in body
    elig = dict(drafting.select("eligibility", "cra", "email", "medium"), ai_edit=False)
    _, body = drafting.render(elig, dict(ctx, item=dict(ctx["item"], title="Randomization planned with eGFR 41")))
    assert "Randomization planned with eGFR 41 (P1, S)." in body and "enrolled" not in body


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
        return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: data})()

    def get(self, url, params=None, timeout=None):
        if url.endswith("/userinfo"):
            return self._reply({"sub": "100", "email": "bot@huma.ai"})
        if "/people/" in url:
            return self._reply({"emailAddresses": [{"value": f"user{url.rsplit('/', 1)[1]}@site.org"}]})
        if url.endswith("/spaces"):
            return self._reply({"spaces": [{"name": n} for n in self.spaces if n.startswith("spaces/dm")]})
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

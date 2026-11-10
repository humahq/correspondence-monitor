# Compliance checklist (Part I)

Source: "Open Book (Clinical Trials AI Monitor): 21 CFR Part 11 Compliance PRD", version 1.0, 26 Oct 2026, with
Eliot's Addendum A of 5 Nov 2026 (VAL-06 to VAL-09, at the end of this list).
Part I is the developer's part. It has 71 requirements. Part II (policies, contracts, training) is Eliot's and is not in this list.

This list is fixed: the same 71 IDs, in the document's order. Only the boxes change. The Addendum's 4 are
listed on their own, at the end. They amend GOV-02 (by VAL-07), VAL-01 (by VAL-06), VAL-03 (by VAL-09) and
PRV-02 (by VAL-08).

Status on 10 Nov 2026: **52 done, 17 partly done, 0 not done, 2 later (V2).** Addendum A: 1 of 4 done (VAL-07).

The safety contacts (medical monitor, safety backup, on-call) are stand-in addresses that reach John until the
sponsor names the real people: put their names, emails and phones in the study file, nothing else changes.

## What to do and when

Every item that is not done, grouped by when it gets done and who does it. The tables further down say what is
left for each one.

**1. Next project: hindsight (patient memory).** No checklist item waits on it, but the items in group 2 come after it.
Plan and test results in `docs/hindsight-plan.md` (issue #32).

Status on 1 Nov 2026. Sources: the calls with Eliot on 28, 29 and 30 Oct (transcripts) and John's memory email
to Eliot (28 Oct).

Done:

*Choosing and setting up Hindsight*
- [x] Use Hindsight (open source, MIT), not our own build. If it lacks something, we send its makers a fix.
  (call 29 Oct)
- [x] Re-ranker: Hindsight's own CPU re-ranker, timed first on Google Cloud as Eliot asked (0 to 3 seconds is
  fine). Result: about 1 second to search one patient's facts, worst 1.2 seconds, with 2,000 made-up facts. No GPU,
  no GPU rental and no Cohere needed. (call 29 Oct)
- [x] Server: 4 GB (e2-medium), about $24 a month. 2 GB was tested and was too slow.
- [x] Security: Hindsight listens only on the machine itself (loopback) and needs a password. It refuses to stay
  up if it answers without the password. (call 28 Oct: "hindsight's dangerous")
- [x] Portable: the same setup runs on a laptop, with no cloud-only parts. (call 28 Oct)

*Saving facts*
- [x] Each message is saved to Hindsight at the same time as the normal check, not before it, so there is no
  extra wait. (call 28 Oct)
- [x] Only facts about patients are saved, not team logistics or names. (call 28 Oct; email)
- [x] Each fact keeps who said it, their role, the date and the message it came from. (email)
- [x] A fact with no clear patient code is linked to nobody, and listed on the memory page for a person to
  link. (call 28 Oct; email)
- [x] Patient codes written in different ways ("1001-022", "1001022", or just "022" when it matches only one
  patient) all reach the same patient.
- [x] Typos and corrections stay out of the record. "BP 500/10, sorry, 150/10" is kept as 150/10. (email)
- [x] The bot's own alerts, and what happens to them, are saved for their patient. (call 30 Oct)

*Using facts*
- [x] Before answering a question about a patient, the bot searches that patient's facts. Facts from other
  threads and from years before are found. (call 28 Oct)
- [x] Eliot's gout example works: the answer uses the earlier fact and says who reported it and when.
  Example: "The study coordinator reported on 29 Oct 2026 that she takes St. John's wort every day ... a
  prohibited CYP3A4 inducer." (call 28 Oct)
- [x] Newest fact wins, and the old one is kept. "Born 1967", then "born 1972": the bot uses 1972 and both are
  kept. The old fact is marked "no longer true". (call 28 Oct)
- [x] No leaks: one patient's facts never come back for another patient. (email)
- [x] A slow or failed search never stops the answer: it goes out without memory, and the failure is logged.
  (call 28 Oct: slow re-ranking "will time out")
- [x] The bot can work out dates from facts, for example "started on 1 Sep, plus a 28-day washout, so 29 Sep".
- [x] A question only about what earlier messages said ("what did the site report about her dose?") is answered
  from memory, and the answer says it comes from an earlier message.

*Memory page*
- [x] An admin page lists each patient's facts, with who reported each one, when, and a link to the message.
  (email)
- [x] An admin can correct, retire, restore or link a fact. Each change needs a reason and goes in the audit
  trail with the old and new words. Nothing is deleted. (email)

*Logging, keeping and deleting*
- [x] Every save, search, failure and change is in the audit trail.
- [x] Facts are kept 7 years, then deleted by the daily purge. (call 30 Oct: "It can be seven years")
- [x] One patient's facts can be forgotten, and a whole study's memory can be deleted (a second person must
  approve).
- [x] Facts are in the records export (`patient_memory.csv`).
- [x] Hindsight's data is backed up every hour, and each backup is checked by restoring it.

*Cost and tests*
- [x] Cost: about a quarter of a cent per message, so about $2.70 for 1,000 messages. (call 30 Oct: "it really
  should be pennies per message")
- [x] Searches take about 1.4 seconds in the middle case and 3.4 seconds at worst on a laptop.
- [x] Tests: 44 made-up stories (several messages, then a question), with 116 to 119 of 123 checks passed. Two
  new sets the bot had never seen: 71%, then 80% (101 of 126). In the latest set only 1 failure was about memory;
  the rest were how the answer reads the protocol.

What is left:
- [ ] Use memory when checking every new message, so alerts use it too, not only answers. (call 28 Oct: "when an
  email comes through on a topic that's covered in a thread ... there could be patient context in there that you
  would want to include", and "the first thing it does it makes a very quick hindsight call".) Today alerts use
  only the older dates memory (visit windows, dose timing).
- [ ] The AI knows "where you are in remediation": what was already done about an alert. (call 30 Oct) The bot's
  alerts and their changes are saved to memory, but alert checks don't read them yet.
- [ ] Long email threads: compress or summarize instead of cutting. (call 28 Oct: "We may have to do some
  compression or summarization")
- [ ] Turn memory on live, after Eliot approves settings version 61.
- [ ] Questions for Eliot (email): can a fact from a private chat be used in a group chat? Who may correct facts,
  admins only or coordinators too? Is OpenAI's patient-data agreement (BAA) in place?

**2. Done, live on 10 Nov 2026: closing alerts on the web page (DSP-01, DSP-02, DSP-04, and the "clear by reply"
part of OUT-03).** Issue #74, `features.close_on_page`, `cormon/closing.py`. For now only Peter, Eliot and John may
close and review (`closing:` in study.yaml).
Eliot decided this on 29 Oct. Alerts are no longer closed by email or chat reply. On the alert's web page, the person picks one:
- "I did what was asked", with the date and time. This closes the alert.
- "I did something else", with what and when. This goes to the PI.
- "Nothing can be done". This also goes to the PI.

The PI approves or rejects. A rejection goes back to the person or is reported to the sponsor. A confirm step
before the close takes effect covers DSP-02. The fixed choice is the reason (DSP-04). The AI no longer decides
anything here (DSP-01, OUT-03).

**3. After the sponsor contract is signed: plug in their details (no new code).**
- SAF-02, SAF-05: the real medical monitor, safety backup and on-call names, emails and phone numbers, in the study file.
- SAF-03 (done): the sponsor's safety team approves the severe-word list. Ours is a draft.
- VAL-04: each site's protocol version dates, and the older protocol versions loaded.
- PRV-01: the consent form, so we can check it covers sending patient data to the AI.

**4. Code we can build any time (no one else needed).**
- CAP-09: trick emails. Research done and checked (31 Oct, `docs/prompt-injection-research.md`). Later, when John asks:
  1. A random marker around the email, and invisible characters removed.
  2. A claim of permission in an email ("the sponsor said it's fine") never stops an alert; only the protocol or our approved notes can.
  3. A second check that sees only the facts and the protocol, not the email's words; it can only add a "please check". Measure its false alerts first.
  4. An email that gives the bot orders is flagged to the admins, and its "no problem" result goes out as "please check".
  5. Severe safety words always reach the medical monitor, even if the AI missed it (only adds). Needs Eliot's OK.
  6. Test like an attacker: 100 trick emails (20 per harm) plus 20 clean ones, an AI attacker that rewords each trick up to 5 times, and John, Peter and Eliot trying to fool it. Score per harm. About $25 a run, weekly.
  7. A weekly list for the admins of flagged emails and "please check" alerts from steps 2 to 5.

  No ready-made detector for now: adaptive tricks get past them over 90% of the time, cloud ones need a new patient-data agreement, and they flag normal clinical orders. Closing alerts without the AI is group 2.
- OUT-03: the doctor check and the careful check. The AI answers fixed questions and code decides.
- QAA-06: block clinical judgment in answers. Needs real answers to measure first, so after real use.

**5. Someone outside the code.**
- Eliot: approve settings version 61 (`./bin/cormon-cloud config approve settings 61`). This starts off-server backups (BCK-01, done).
- Eliot: a written rule for how long the mailbox keeps mail (CAP-10).
- Whoever runs the huma.ai domain: DMARC `p=reject` (OUT-02).
- The Google Workspace admin: two-step login for everyone (IAM-01).
- A GitHub repo admin, plus a named reviewer (Peter or Eliot): require a review before merge (SDL-01).

**6. Phase 3, paid, when we go for certification (a few dollars a month).**
Locked storage (OUT-01, AUD-02), a pager (ARC-04), our own encryption key held by a separate person (SEC-01,
IAM-04), all secrets in Secret Manager with rotation (SEC-03).

**7. V2 (sponsor web features).** IAM-05, EXP-02.

## Done (52)

- [x] CAP-01 Original bytes preserved
- [x] CAP-02 Hash-linked derivations
- [x] CAP-03 Teams mutability
- [x] CAP-04 Authoritative timestamps
- [x] CAP-05 Inbound source integrity
- [x] CAP-06 Study authorization at ingestion (a message from someone not on the study's people list is kept and read, but goes only to the admins: no alert, chat post, reply or digest line.)
- [x] CAP-07 Ingestion completeness
- [x] CAP-08 Coverage statements
- [x] OUT-04 Template and content guardrails
- [x] OUT-05 Kill switch and rate limits
- [x] OUT-06 Bounce and non-delivery escalation
- [x] OUT-07 Recall procedure
- [x] OUT-08 Default for Open Decision 1
- [x] QAA-01 Answers are records
- [x] QAA-02 Grounding verification
- [x] QAA-03 Standing answer disclaimer
- [x] QAA-04 Q&A validation (the question test set now blocks a release if it scores too low)
- [x] QAA-05 Physician handling (templates are versioned config, changed only through a tested pull request)
- [x] SAF-03 Severity as routing metadata (severe when the message has a word from a fixed list, `safety.severe_words` in config/routing.yaml, plus the study's own; acknowledge within 1 hour if severe, 24 hours if not, counted from when the message was received. The list is our draft for the sponsor's safety team to approve)
- [x] SAF-04 Acknowledgment is a disposition (only the medical monitor or the safety backup: they reply "ACK <code>", checked by code against the people list, their role and the sender check; or an admin records which of them answered the phone with `cormon ack N --by <person>`. Anyone else on the list who replies is told it did not count. Who, when, how and who recorded it are audited; the item is never cleared)
- [x] SAF-06 Safety routing validation
- [x] SAF-07 Safety routing record (each safety message in the records export, `safety.csv`: received time and hash, severity, where it was sent, who acknowledged and when, how far it escalated, and any deviation)
- [x] SAF-10 Site notice never fires for safety
- [x] SAF-11 Negative assurance
- [x] DSP-01 Deterministic disposition grammar (an alert is closed on its page with three fixed choices, by a person signed in with Google; a "cleared" reply gets the page's link and changes nothing. Live 10 Nov, issue #74)
- [x] DSP-02 Echo-back confirmation (a check step shows exactly what will be kept, and only Confirm saves it, sealed so it can't change in between; "something else" and "nothing can be done" also need a second person's review, never your own)
- [x] DSP-03 Roster authority check
- [x] DSP-04 Reason and visibility (the page's choice is the reason code: fixed, other_fix_approved, not_remediable_approved, reported_to_sponsor, with the person's words, on the alert's page and in the audit log)
- [x] DSP-05 Operator override controls
- [x] DSP-06 Operator notes are configuration
- [x] AUD-01 Non-obscuring writes
- [x] AUD-03 Time synchronization
- [x] AUD-04 Display consistency
- [x] AUD-05 Contextual AI logging
- [x] AUD-06 Re-run semantics
- [x] AUD-08 Audit-trail retention
- [x] CFG-01 Configuration under change control
- [x] CFG-02 Configuration triggers revalidation
- [x] ARC-01 Read-only boundary
- [x] ARC-02 Open-system controls (11.30)
- [x] ARC-03 Operational sequence checks
- [x] SDL-02 Change linking
- [x] SDL-03 Environment isolation (only the live bot may read or send real mail and chat; a copy that is not the live bot will not open the live database or a backup of it, and will not download the off-server backups)
- [x] SDL-05 Regression blocking
- [x] SEC-02 Dependency and SBOM scanning in CI
- [x] SEC-04 Two-person rule for destructive actions
- [x] PRV-04 Non-production isolation (the same checks as SDL-03)
- [x] BCK-01 Backup automation and recovery targets (built and deployed; copies to Google Cloud Storage start once Eliot approves settings version 61)
- [x] SUP-02 Model lifecycle controls
- [x] VAL-05 Revalidation triggers
- [x] GOV-02 Drift monitoring
- [x] EXP-01 MVP export procedure (`cormon export-records`: one zip per study with originals and their hashes, alerts and every change to them, safety routing, sent messages, audit trail, AI log and settings history, plus the integrity checks. CSV, not PDF. PR #19)

## Partly done (17)

| ID | Name | What is left |
|---|---|---|
| CAP-09 | Prompt-injection defense | 2 tricks still fool the bot; the trick test set is small (20). Biggest gap: if the AI is talked into "fine", nothing else checks that email. Checked research and a 7-step plan (group 4 above) in `docs/prompt-injection-research.md` |
| CAP-10 | Second-copy governance | No written rule for how long the mailbox keeps mail |
| OUT-01 | Sent-message records | A write-once copy now goes to a separate database (PR #17), a stand-in: the locked bucket is Phase 3 |
| OUT-02 | Outbound domain authentication | DMARC `p=reject` on the sending domain (a DNS task) |
| OUT-03 | LLM output never routes | The AI still decides "whether" in 2 places (doctor check, careful check). Clear by reply is gone: alerts are closed on their page (live 10 Nov, issue #74) |
| QAA-06 | No clinical judgment | A judgment in a final answer is recorded, not blocked. Needs real answers to measure |
| SAF-02 | Detection and routing | Safety mail goes, unassessed, to the medical monitor with the CRA copied, but the address is a stand-in (john+medicalmonitor@huma.ai) until the sponsor names one |
| SAF-05 | Escalation and terminus | Built: not acknowledged in time, it goes to the safety backup, then the ClinOps lead, then the on-call person and the admins are told to phone (3 reminders); reaching that last step is recorded as a deviation, with the steps that went unanswered. The contacts are stand-ins and no phone numbers are on file yet |
| IAM-01 | Operator access | MFA depends on the Google Workspace setting |
| IAM-04 | Key management | No separate key-admin role; no key rotation |
| AUD-02 | Immutability via compensating controls | Each audit entry is copied to a separate write-once database and checked daily (PR #17); someone with the file can still drop its triggers, so the locked bucket is Phase 3 |
| ARC-04 | Availability controls | Alerts go by email, not by pager (Phase 3) |
| SDL-01 | Version control and peer review | GitHub does not force a review before merge (main branch is not protected) |
| SEC-01 | Encryption | No app-level encryption or customer-owned key |
| SEC-03 | Secrets and credentials | Some secrets still in env vars and files; no rotation |
| PRV-01 | PHI handling | No consent-form coverage check when a study starts |
| VAL-04 | Per-site protocol version effectivity | An alert checked against a version the site was not on that day is held for a person (PR #21). Only one version per document is loaded, and the sites have no version dates yet |
## Not done (0)

None.


## Later, V2 (2)

- [ ] IAM-05 Sponsor web UI SSO and MFA
- [ ] EXP-02 Sponsor self-service export

## Addendum A, 5 Nov 2026 (4: 1 done)

| ID | Name | Status |
|---|---|---|
| VAL-06 | Validated against FDA CSV and CSA | Eliot's paperwork: the VAL-01 package says, for each requirement, how it is validated (CSV or CSA). Not started |
| VAL-07 | Model drift as a release gate | Done, live on 10 Nov 2026 (issue #70): the weekly run asks every test set fresh and compares each case with the last passed fresh run; more changes than the limit in `config/gates.yaml` fail it, and `deploy/release.sh` stops unless the newest fresh run passed and is at most 8 days old |
| VAL-08 | GDPR and EU AI Act alongside Part 11 | Eliot's paperwork: the lawful basis, how long data is kept, and who checks the AI (human oversight). Not started |
| VAL-09 | About 1,000 validated questions | Not done: 100 questions today (`demo/qa-g/questions.json`). Later |

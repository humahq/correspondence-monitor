# Compliance checklist (Part I)

Source: "Open Book (Clinical Trials AI Monitor): 21 CFR Part 11 Compliance PRD", version 1.0, 26 Oct 2026.
Part I is the developer's part. It has 71 requirements. Part II (policies, contracts, training) is Eliot's and is not in this list.

This list is fixed: the same 71 IDs, in the document's order. Only the boxes change.

Status on 30 Oct 2026: **49 done, 19 partly done, 1 not done, 2 later (V2).**

The safety contacts (medical monitor, safety backup, on-call) are stand-in addresses that reach John until the
sponsor names the real people: put their names, emails and phones in the study file, nothing else changes.

## What to do and when

Every item that is not done, grouped by when it gets done and who does it. The tables further down say what is
left for each one.

**1. Next project: hindsight (patient memory).** No checklist item waits on it, but the items in group 2 come after it.
Plan in `docs/hindsight-plan.md` (issue #32): first a timing test of its re-ranker, then 7 build steps, about 7 to 8 days.

**2. After hindsight: closing alerts on the web page (DSP-01, DSP-02, DSP-04, and the "clear by reply" part of OUT-03).**
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

## Done (49)

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
- [x] DSP-03 Roster authority check
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

## Partly done (19)

| ID | Name | What is left |
|---|---|---|
| CAP-09 | Prompt-injection defense | 2 tricks still fool the bot; the trick test set is small (20). Biggest gap: if the AI is talked into "fine", nothing else checks that email. Checked research and a 7-step plan (group 4 above) in `docs/prompt-injection-research.md` |
| CAP-10 | Second-copy governance | No written rule for how long the mailbox keeps mail |
| OUT-01 | Sent-message records | A write-once copy now goes to a separate database (PR #17), a stand-in: the locked bucket is Phase 3 |
| OUT-02 | Outbound domain authentication | DMARC `p=reject` on the sending domain (a DNS task) |
| OUT-03 | LLM output never routes | The AI still decides "whether" in 3 places (doctor check, careful check, clear by reply) |
| QAA-06 | No clinical judgment | A judgment in a final answer is recorded, not blocked. Needs real answers to measure |
| SAF-02 | Detection and routing | Safety mail goes, unassessed, to the medical monitor with the CRA copied, but the address is a stand-in (john+medicalmonitor@huma.ai) until the sponsor names one |
| SAF-05 | Escalation and terminus | Built: not acknowledged in time, it goes to the safety backup, then the ClinOps lead, then the on-call person and the admins are told to phone (3 reminders); reaching that last step is recorded as a deviation, with the steps that went unanswered. The contacts are stand-ins and no phone numbers are on file yet |
| DSP-01 | Deterministic disposition grammar | Today the AI reads a reply to clear an alert. Eliot decided (29 Oct) that alerts are closed on the alert's web page with fixed choices, not by reply. After hindsight |
| DSP-04 | Reason and visibility | A clear by reply gets the code "by_reply". The web page choices will give the reason. After hindsight, with DSP-01 |
| IAM-01 | Operator access | MFA depends on the Google Workspace setting |
| IAM-04 | Key management | No separate key-admin role; no key rotation |
| AUD-02 | Immutability via compensating controls | Each audit entry is copied to a separate write-once database and checked daily (PR #17); someone with the file can still drop its triggers, so the locked bucket is Phase 3 |
| ARC-04 | Availability controls | Alerts go by email, not by pager (Phase 3) |
| SDL-01 | Version control and peer review | GitHub does not force a review before merge (main branch is not protected) |
| SEC-01 | Encryption | No app-level encryption or customer-owned key |
| SEC-03 | Secrets and credentials | Some secrets still in env vars and files; no rotation |
| PRV-01 | PHI handling | No consent-form coverage check when a study starts |
| VAL-04 | Per-site protocol version effectivity | An alert checked against a version the site was not on that day is held for a person (PR #21). Only one version per document is loaded, and the sites have no version dates yet |
## Not done (1)

| ID | Name | What is missing |
|---|---|---|
| DSP-02 | Echo-back confirmation | No confirm step before a close takes effect. It will be a confirm step on the web page, plus the PI's review. After hindsight, with DSP-01 |


## Later, V2 (2)

- [ ] IAM-05 Sponsor web UI SSO and MFA
- [ ] EXP-02 Sponsor self-service export

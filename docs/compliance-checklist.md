# Compliance checklist (Part I)

Source: "Open Book (Clinical Trials AI Monitor): 21 CFR Part 11 Compliance PRD", version 1.0, 26 Oct 2026.
Part I is the developer's part. It has 71 requirements. Part II (policies, contracts, training) is Eliot's and is not in this list.

This list is fixed: the same 71 IDs, in the document's order. Only the boxes change.

Status on 30 Oct 2026: **45 done, 18 partly done, 6 not done, 2 later (V2).**

## Done (45)

- [x] CAP-01 Original bytes preserved
- [x] CAP-02 Hash-linked derivations
- [x] CAP-03 Teams mutability
- [x] CAP-04 Authoritative timestamps
- [x] CAP-05 Inbound source integrity
- [x] CAP-06 Study authorization at ingestion (a message from someone not on the study's people list is kept and read, but goes only to the admins: no alert, chat post, reply or digest line. Built 30 Oct, not deployed yet)
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
- [x] SAF-06 Safety routing validation
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
- [x] SDL-03 Environment isolation (only the live bot may read or send real mail and chat; a copy that is not the live bot will not open the live database or a backup of it, and will not download the off-server backups. Built 30 Oct, not deployed yet)
- [x] SDL-05 Regression blocking
- [x] SEC-02 Dependency and SBOM scanning in CI
- [x] SEC-04 Two-person rule for destructive actions
- [x] PRV-04 Non-production isolation (the same checks as SDL-03. Built 30 Oct, not deployed yet)
- [x] BCK-01 Backup automation and recovery targets (built and deployed; copies to Google Cloud Storage start once Eliot approves the bucket setting)
- [x] SUP-02 Model lifecycle controls
- [x] VAL-05 Revalidation triggers
- [x] GOV-02 Drift monitoring

## Partly done (18)

| ID | Name | What is left |
|---|---|---|
| CAP-09 | Prompt-injection defense | 2 tricks still fool the bot; the trick test set is small (20). Parked |
| CAP-10 | Second-copy governance | No written rule for how long the mailbox keeps mail |
| OUT-01 | Sent-message records | Sent messages are stored and checked, but not in write-once storage (Phase 3) |
| OUT-02 | Outbound domain authentication | DMARC `p=reject` on the sending domain (a DNS task) |
| OUT-03 | LLM output never routes | The AI still decides "whether" in 3 places (doctor check, careful check, clear by reply) |
| QAA-06 | No clinical judgment | A judgment in a final answer is recorded, not blocked. Needs real answers to measure |
| SAF-02 | Detection and routing | Safety mail is caught but goes to nobody: we need the medical monitor's email |
| SAF-07 | Safety routing record | The record exists, but it is not in an export yet |
| DSP-01 | Deterministic disposition grammar | No fixed "token + action + reason" reply format; the AI reads the reply |
| DSP-04 | Reason and visibility | A clear by reply gets the code "by_reply"; waits on DSP-01 |
| IAM-01 | Operator access | MFA depends on the Google Workspace setting |
| IAM-04 | Key management | No separate key-admin role; no key rotation |
| AUD-02 | Immutability via compensating controls | No separate write-once store (Phase 3) |
| ARC-04 | Availability controls | Alerts go by email, not by pager (Phase 3) |
| SDL-01 | Version control and peer review | GitHub does not force a review before merge (main branch is not protected) |
| SEC-01 | Encryption | No app-level encryption or customer-owned key |
| SEC-03 | Secrets and credentials | Some secrets still in env vars and files; no rotation |
| PRV-01 | PHI handling | No consent-form coverage check when a study starts |
## Not done (6)

| ID | Name | What is missing |
|---|---|---|
| SAF-03 | Severity as routing metadata | No severity levels; no 1-hour / 24-hour acknowledgment clock |
| SAF-04 | Acknowledgment is a disposition | The medical monitor cannot acknowledge a safety alert |
| SAF-05 | Escalation and terminus | No escalation chain (PV backup, then ClinOps lead, then a person by phone) |
| DSP-02 | Echo-back confirmation | No "reply CONFIRM" step before a clear takes effect |
| EXP-01 | MVP export procedure | Export gives the rules, not a study's records package |
| VAL-04 | Per-site protocol version effectivity | No protocol version by site and date |

SAF-03, SAF-04 and SAF-05 all need the medical monitor's contact (and a backup) first.

## Later, V2 (2)

- [ ] IAM-05 Sponsor web UI SSO and MFA
- [ ] EXP-02 Sponsor self-service export

# 21 CFR Part 11, Part I: where we stand and how we build it

For Eliot, 2026-10-27. Covers Part I of the compliance PRD (the developer's list). Part II (policies,
contracts, programs) is Eliot's and is not covered here. The free groundwork (Phase 1, first part) was built,
tested and deployed to the live bot on 27 Oct; the rest is not built yet.

## 1. Summary

- Part I has 71 requirement IDs. Before this work: 7 done, 41 partly done, 21 not done, 2 are V2 (later).
  After the 27 Oct deploy: 12 done, 40 partly done, 17 not done, 2 V2. After the second 27 Oct deploy: 24 done,
  32 partly done, 13 not done, 2 V2.
- Nothing we found needs a rewrite. Most gaps are missing records (hashes, versions, full AI logs) or missing
  workflow (safety routing, clearing grammar, bounces, recall).
- Approach, as Eliot asked: a thin wrapper (facade) in front of each compliance function. Each wrapper has a
  free local backend now and a compliant cloud backend later, chosen by one setting. The rest of the code only
  calls the wrapper, so switching on the paid services is a configuration change plus a revalidation run.
- The current server (e2-micro) is enough for now. No new paid services are needed until we switch on the
  certified backends. Estimated cost at that point is a few dollars a month at today's data size (section 6).
- Six product decisions and two scope checks are needed from Eliot before the rest of the work (section 7). The
  biggest: today the AI can clear or suppress an alert from a reply, which the PRD forbids (OUT-03, DSP-01).

### Deployed on 27 Oct

- Wrappers for records, audit trail, AI log, secrets and alerts (`cormon/compliance/`), all on the free local
  or email backend. Naming a backend that is not built yet stops the bot instead of quietly using another.
- A SHA-256 fingerprint for every message, attachment and sent message when it arrives or goes out. What the bot
  reads from a message carries the message's hash. Every night the bot hashes the files again; a message whose
  original no longer matches is not read, and the admins are emailed. Messages from before 27 Oct were
  fingerprinted on 27 Oct, so they are protected from then on but prove nothing about earlier.
- The audit trail is hash-chained: each entry includes the hash of the one before. Every day the whole chain is
  checked and the latest hash is written to a separate anchor file. A changed, removed or inserted entry, or
  entries cut off the end, are caught.
- Every AI call is logged in full: the request, the answer, the model and its settings, cache hit or not, the
  message and its hash, the study-model fingerprint and the config version. It holds patient text, so it is
  kept and purged with the message text.
- An edited or deleted Google Chat message is kept as a new version; the original is never changed.
- The sentence "This system does not guarantee the detection of safety signals. Silence does not constitute
  clearance." is on every digest, item page and admin page.
- A separate watchdog runs every 5 minutes. If the bot has not checked in for 15 minutes it emails Peter,
  Eliot and John, again every hour while it lasts, and once when it is back. `/health` returns an error while
  the bot is stalled. A message waiting more than 15 minutes, or a mailbox not read for 15 minutes, is also
  emailed.
- `cormon verify` runs the audit-trail and fingerprint checks by hand. 33 new tests; all 185 pass.

## 2. The wrappers (facades)

Each is a small, self-contained component with one job and a fixed interface. These are also the first pieces
of the wider "split the code into components" work.

| Wrapper | Job | Local backend (now, free) | Certified backend (later) | PRD IDs |
|---|---|---|---|---|
| `records` | Store every original (email bytes, chat JSON, attachments, sent messages) with its SHA-256; read and verify by hash | Files under `data/` plus a `records` table of hashes | Cloud Storage bucket with retention lock (write once, never edit), separate project | CAP-01, CAP-02, CAP-03, OUT-01, EXP-01 |
| `trail` | The audit trail: append-only, each entry hash-chained to the one before, a daily anchor, a verifier | SQLite `audit_log` (already append-only by trigger) plus the chain columns; daily anchor written to a separate file | Same entries mirrored to the locked bucket; anchor in a place the operator role cannot write | AUD-01, AUD-02, AUD-08, SAF-07 |
| `ailog` | Full record of every AI call: prompt, answer, model and settings, rule-set and study-model version, source hash | SQLite table | Locked bucket (it holds patient text, so it is a PHI store with its own access) | AUD-05, QAA-01, PRV-01 |
| `secrets` | One place every credential is read from | Environment variable, else Secret Manager | Secret Manager (already used for two secrets), per-person access, rotation | SEC-03, IAM-04 |
| `backups` | Copy the database and records, check the copy, restore | Today's daily local copy (`cormon/backup.py`) | Hourly copy to a bucket (RPO 1 hour), timed restore test (RTO 4 hours) | BCK-01 |
| `alerts` | Tell a human the bot is unhealthy (no heartbeat, ingestion lag, failed sends) | Email to the admins | A paging service | ARC-04, OUT-06, SAF-05 |
| `keys` | Encryption keys | Google's default disk encryption | Cloud KMS key held by a separate key-admin role | SEC-01, IAM-04 |

How the switch works: `config/settings.yaml` gets one block, for example

```yaml
compliance:
  records: local      # local | gcs_locked
  trail: local        # local | gcs_locked
  ailog: local        # local | gcs_locked
  secrets: local      # local | secret_manager
  alerts: email       # email | pager
```

The first five wrappers are built (27 Oct). `backups` stays the existing component until Phase 3; `keys` is
Phase 3.

Each wrapper has a test suite that runs against both backends. That way, flipping a setting also runs the
same tests that prove the certified backend behaves like the local one.

The existing code already has natural chokepoints to put these in front of, so the change is mostly local:

- Originals: `capture.capture` (saves raw files and attachments).
- Sent messages: `routing.enqueue`, then `outbox.dispatch`.
- Audit: `db.audit` (the only way the audit log is written today).
- AI calls: `llm.structured` and `llm.decide`.
- Backups: `backup.make` and `backup.restore`.
- Secrets: scattered today (`imap.py`, `weblogin.py`, `settings.py`). The wrapper brings them into one place.

## 3. Where we stand, requirement by requirement

Status from reading the code on 2026-10-27, updated after the 27 Oct deploy. "Partly" means part of the
requirement is in place; the gap is the note.

### Done (24)

Built, tested and deployed on the evening of 27 Oct (with pypdf 6.21.2 and certifi 2026.10.23, which fix
the known problems the first scan found):

- AUD-01 Every change to messages, attachments, items, outbox and rules is kept as old and new values in an
  append-only change log (patient text as lengths only); each hour's changes are sealed into the hash chain.
- CAP-01 Chat attachments downloaded, read and fingerprinted like email attachments; ones that cannot be
  downloaded (Drive links, too large) are kept as a reference with a note.
- CAP-04 The sender's time is marked as the sender's own clock (not checked); pages show both times.
- CAP-05 A failed sender check is shown as a flag on the item page and in the digest.
- CAP-07 Every hour the mailbox is compared with the store; a message missing twice in a row is read in
  and the admins are told.
- CAP-08 The digest lists the times the bot was down and messages that arrived or were read late.
- OUT-06 Bounces are matched to the sent message, kept as a record, audited, and the admins are told.
- ARC-03 Step-order checks: an item can only move along allowed steps, and a reply can only clear an item
  that was sent to someone; refusals are audited and told to the sender.
- QAA-01 Each answer's audit entry carries a hash of the question.
- QAA-02 Each question the bot could not answer gets its own audit record.
- SEC-02 `cormon scan` and a weekly check list every package (saved as an SBOM) and look them up in the OSV
  database of known security problems; problems go to the admins.
- SUP-02 A backup model (`llm.fallback`, off until set) is tried once when a model fails, and the admins are
  told; a weekly check warns when a model in the settings is no longer offered.

Done on 27 Oct (deployed):

- CAP-02 SHA-256 at capture; events carry the source hash; nightly re-check; a changed original is not read.
- CAP-03 Chat edits and deletes kept as new versions.
- AUD-05 Every AI call logged in full with model, settings, study-model and config version, and source hash.
- AUD-08 The AI log is kept as long as the message text, and the audit trail forever.
- SAF-11 The required sentence is on every digest, item page and admin page.

Done before:

- QAA-03 Fixed disclaimer footer on every answer.
- AUD-03 All times stored in UTC; display timezone shown.
- AUD-04 Alerts are stored as shown, not re-made when a page opens.
- AUD-06 Re-reading a message makes a new version; the old one stays.
- ARC-01 No code path writes to any sponsor system (no EDC, CRM or calendar connection exists).
- ARC-02 HTTPS through Caddy; mail and AI calls over TLS.
- OUT-08 No approval queue, which matches the PRD's default (unattended send).

### Not done (13)

| ID | What is missing |
|---|---|
| OUT-07 | No recall/correction of a sent alert |
| SAF-03 | No severity taxonomy, no 1-hour / 24-hour acknowledgment clock |
| SAF-04 | No safety acknowledgment by the medical monitor |
| SAF-05 | No escalation chain (PV backup, ClinOps lead, then a human by phone) |
| DSP-02 | No "reply CONFIRM" step before a clear takes effect |
| CFG-02 | Changing rules or prompts does not run the test sets |
| EXP-01 | `export.py` exports the rules, not a study's records package |
| SDL-02 | Commits are not linked to tickets |
| SDL-05 | No CI; nothing blocks a deploy when tests fail |
| SEC-04 | Destructive commands need `--yes` from one person, not two |
| VAL-04 | No protocol version by site and date |
| VAL-05 | Model, prompt or rule changes do not trigger test runs |
| GOV-02 | No production drift monitoring (sampling for misses, grounding failure rate) |

### Partly done (32)

| ID | In place | Gap |
|---|---|---|
| CAP-06 | Plus-address picks the study; untagged mail never matched; senders not on the people list are flagged on the page and in the digest (built 27 Oct) | Not kept from sponsor-facing posts |
| CAP-09 | Recipients come from config, not the AI; a 20-message trick test set (built 27 Oct), run on the live DNL rules (12 of 20 right; the same emails without the tricks 13 of 17) | Two tricks worked: an invented "standing exception" hid an old consent form (j19), and an order inside an admin email made the bot report a made-up eGFR deviation (j11). The set is small (20) |
| CAP-10 | Hourly check that the mailbox and our store agree (built 27 Oct) | No mailbox retention procedure |
| OUT-01 | Rendered text stored per sent message, hashed and re-checked nightly (27 Oct) | Not write-once (Phase 3) |
| OUT-02 | Gmail sends with its own SPF/DKIM | DMARC `p=reject` on the sending domain is a DNS task |
| OUT-03 | Who and where come from config | AI decides some "whether": the doctor check (withhold from chat), the careful check, and clearing from a reply (see section 7) |
| OUT-04 | Templates are fixed; AI edit is optional and checked | No schema check for new facts or instructions |
| OUT-05 | Pause switch stops sending, ingestion keeps going | No per-study hourly send cap |
| QAA-04 | Q&A test bench with scores | No pass/fail threshold that blocks a release |
| QAA-05 | Physician rules and calm-reply templates | Templates not under versioned config with approver |
| QAA-06 | Prompts forbid clinical judgment | No output check |
| SAF-02 | Safety messages are kept out of normal checks | Routed to nobody today; the medical monitor's email is still missing |
| SAF-06 | Stuck-message and missed-routing checks | No tests for "no monitor assigned", "recipient unreachable", "router down" |
| SAF-07 | Safety detection is in the audit log, which is hash-chained (27 Oct); the message is fingerprinted | Not in an export |
| SAF-10 | Site notice only fires on immediate findings from a coordinator | A message with both a safety part and a finding could still trigger it |
| DSP-01 | Clear and disregard by reply or command | No token + action + reason-code grammar; the AI reads the intent |
| DSP-03 | Only CRA, ClinOps lead or tester roles can clear by email | Token's study is not matched to the sender's study; commands not roster-checked |
| DSP-04 | A reason is kept when given | Free text, not a fixed list of reason codes |
| DSP-05 | Operator overrides are marked and survive later passes | Reason optional; no second person for a suppression the sponsor would see |
| DSP-06 | Notes can be added and retired | No version history, approver or effective date |
| IAM-01 | Admin pages need Google sign-in and an allow-list | MFA relies on Google settings; command-line operator name is the computer login |
| IAM-04 | Two secrets already in Secret Manager | No separate key-admin role or rotation |
| AUD-02 | Append-only by trigger; never pruned; hash chain, daily anchor and verifier built and deployed 27 Oct | No separate write-once store (Phase 3) |
| CFG-01 | Config files snapshotted with hash and actor; rules carry their document's hash | No approver or effective date; a rebuild replaces the rules |
| ARC-04 | Heartbeat every tick; `/health` and a separate watchdog email the admins after 15 minutes (27 Oct) | Email, not a pager with escalation (Phase 3) |
| SDL-01 | Git, tests before deploy (by habit) | No enforced review before deploy |
| SDL-03 | Live bot on its own server; practice copies on the laptop | Not enforced by a setting |
| SEC-01 | TLS in transit; Google disk encryption | No app-level encryption or customer key |
| SEC-03 | Credentials read through one wrapper (27 Oct); two in Secret Manager | Others in env vars and files; no rotation |
| PRV-01 | Patient details masked in the audit log; retention purge | No ICF coverage check at study activation |
| PRV-04 | Same as SDL-03 | Same |
| BCK-01 | Daily backup, checked, 7 kept, restore command | Not hourly; not off the server; restore time not tested |
### Later (V2): IAM-05 (sponsor SSO), EXP-02 (sponsor self-service export).

## 4. Build order

### Phase 1: the free foundation (no new services, no behaviour change for users)

Deployed 27 Oct: item 1 (five wrappers), item 2, item 3 (chain, anchor, verifier), item 4, item 5 (edits and
deletes), the safety sentence from item 7, and item 9 except the hourly email reconciliation.

Still to do in Phase 1, in this order: old/new values in the audit (rest of item 3), chat attachment bytes (rest
of item 5), item 6, the rest of item 7, the hourly email reconciliation (item 9), then item 8 and item 10 once
Eliot answers the two scope checks in section 7.

1. Create the wrappers with local backends: `records`, `trail`, `ailog`, `backups`, `alerts`, `secrets`.
   Route the existing chokepoints through them. Each has its own tests, run against the local backend.
2. Hashes: SHA-256 for every original at capture and every sent message; derived text (events, attachment
   text) records the source hash; a nightly job re-checks and flags mismatches (CAP-02, OUT-01).
3. Hash-chained audit trail with a daily anchor and a verifier command (AUD-02). Old/new values written for the
   in-place updates on items, outbox and messages (AUD-01).
4. Full AI log for every call (AUD-05), kept as long as the record (AUD-08).
5. Chat edits and deletes kept as new versions (CAP-03); chat attachments fetched as bytes (CAP-01).
6. Sender check shown as a flag on the record and in the digest; roster check on senders (CAP-05, CAP-06).
7. Digest: the safety sentence (SAF-11), the window, gaps and late items (CAP-08).
8. Records export package: originals, manifest of hashes, audit trail, AI log, config history, PDF/CSV of
   alerts and dispositions (EXP-01).
9. Ops: `/health` reflects the heartbeat; email the admins on ingestion lag over 15 minutes or failed sends;
   hourly email reconciliation (ARC-04, CAP-07).
10. CI: tests, the AI test sets and a dependency scan run on every change; a red result blocks the deploy
    (SDL-05, SEC-02, VAL-05, CFG-02).

### Phase 2: behaviour changes (need Eliot's decisions first, section 7)

- Safety routing to the medical monitor with the CRA copied, the severity clock and the escalation chain
  (SAF-02 to SAF-06).
- Clearing by grammar: token + action + reason code, then "reply CONFIRM" (DSP-01 to DSP-04). The AI no
  longer clears anything on its own reading.
- Bounces, recall, per-study send cap, two-person rule for sponsor-visible suppression and destructive
  commands (OUT-05 to OUT-07, DSP-05, SEC-04).
- Protocol version by site and date (VAL-04). This is the largest single item.

### Phase 3: switch on the certified backends (paid; only when we go for certification)

- Locked buckets for records, trail and AI log; hourly backups to a bucket; KMS key; paging; Secret Manager for
  every secret.
- Run the same wrapper tests against the certified backends, then the full revalidation.

## 5. Server

- Today: e2-micro (2 shared vCPUs, 1 GB memory, 1 GB swap), 30 GB disk, Caddy in front. Measured
  2026-10-27: load average 0.08, 563 MB memory free, 22 GB disk free; the bot's data is 320 MB
  (database 38 MB).
- The bot's time is almost all spent waiting on the AI, not on the server, so a bigger machine would not make it
  faster.
- Phase 1 adds hashing, a few tables and some extra files. That is small; the current machine handles it.
- When to grow: if memory gets tight as more studies are added, an e2-small (2 GB) is the next step. Not needed
  now.
- Phase 3 adds managed services (Cloud Storage, KMS, Secret Manager). These are separate from the VM, so no new
  server is needed for them.

## 6. Cost when we switch on (Phase 3, rough, at today's data size)

- Cloud Storage with retention lock: about $0.02 per GB per month. At under 1 GB, cents a month.
- Hourly backups kept 30 days in a bucket: a few dollars a month at most.
- Cloud KMS: well under $1 a month for one key.
- Secret Manager: well under $1 a month for our handful of secrets.
- Paging service: depends on the product; some have free tiers.

Prices to be confirmed against Google's price list before switching on. One warning: a locked retention
policy on a bucket cannot be undone or shortened, even by us. We test with an unlocked bucket first and lock
only when certifying.

## 7. Decisions needed from Eliot

Scope checks (Phase 1 items we have not started):

A. Records export package (EXP-01, item 8): a study's originals, the hash manifest, audit trail, AI log, config
   history and a PDF/CSV of alerts and dispositions. It is a sizeable job. Do it now, or later?
B. Automatic tests before deploy (SDL-05, item 10): GitHub Actions (free, but a new service for us), or a gate
   in our own deploy script that refuses to deploy when tests fail?

Product decisions (Phase 2):

1. AI and consequences (OUT-03, DSP-01). Today the AI can (a) clear or suppress an alert from a reply, after
   the sender and role checks; (b) keep an alert out of the group chat and send it by email only (the doctor
   check and the careful check). Proposal: (a) is replaced by the token grammar in Phase 2. For (b), the AI
   answers fixed questions and fixed code applies a fixed threshold. Does that satisfy OUT-03, or should the
   chat-versus-email choice also become rule-based?
2. Safety routing. Route safety messages to the medical monitor with the CRA copied, as SAF-02 says? We need the
   medical monitor's and PV backup's emails, and who answers the final step of the escalation chain.
3. Severity words. Who writes the severe-content word list per study (SAF-03): the sponsor's safety team at
   activation, or us as a draft for them to approve?
4. Retention. The PRD default is 2 years after study end (RET-01). The bot is set to 7 years today. Which do we
   use?
5. Protocol versions by site and date (VAL-04). The largest single item: each finding would be checked against
   the protocol version in force at that site on that date. When should we do it?

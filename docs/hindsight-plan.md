# Hindsight plan (patient memory)

Issue #32. Sources, and only these: John's memory email to Eliot (28 Oct 2026) and the calls with Eliot on
28, 29 and 30 Oct 2026.

## What Eliot decided

- Use Hindsight (open source, MIT). Don't build our own. If it lacks something, send its makers a fix. (29 Oct)
- Re-ranker, in this order (29 Oct):
  1. Hindsight's own CPU re-ranker on Google Cloud. Time it: 0 to 3 seconds is fine, 15 to 45 seconds is a problem.
  2. If too slow: an agreement with Cohere (check it covers patient data).
  3. Last resort, only while developing: Eliot's Mac Studio over a separate tailnet.
- No GPU rental (too expensive). (29 Oct)
- Hindsight comes before closing alerts on the web page: "hindsight's more important". (30 Oct)
- The AI should know "where you are in remediation": what was already done about a problem. (30 Oct)
- Cost must stay low for the product to work. (30 Oct)
- It must stay portable and open source, so the whole thing runs on a laptop. (28 Oct)
- It does not replace the chat's recent messages; it adds facts from other threads. (28 Oct)

## Steps

**1. Timing test (done 31 Oct, see Results).**
A temporary Google Cloud VM with 4 GB, made-up data only. Hindsight listens only inside the VM, with a password.
Time 100 searches with the CPU re-ranker. 3 seconds or less: go on. 15 seconds or more: look at Cohere.
Delete the VM after.

**2. Set it up (1 day).**
Live server to 4 GB (e2-medium), about $24 a month instead of free. Hindsight runs next to the bot, listening
only on loopback with a password (28 Oct: "Hindsight's dangerous"). Same setup on a laptop.
Built: `deploy/hindsight.sh` (version 1.1.0, pinned by digest; key in `secrets/hindsight_key`; capped at 2 GB of
memory; refuses to stay up if it answers without the key) and `deploy/resize_vm.sh` (reserves the server's address
first so links in sent mail keep working, then resizes and starts Hindsight). Backups of its data come with step 3,
when there is data to keep.

**3. Save facts (1 day).**
- Each message goes to Hindsight while the normal check runs, not one after the other. (28 Oct)
- Each fact keeps who said it and their role, the date, and which message it came from. (28 Oct, email)
- A retain mission so only facts about participants are saved. Hindsight's "observations" (its own AI summaries)
  off at first. (email)
- Typos and corrections stay in the chat only, not in the record. (email)
- No clear participant: nothing is linked; the fact is listed for a person to link. (email; 28 Oct)
- What was done about each problem is saved too. (30 Oct)

Built (`cormon/hindsight.py`, off until `memory.enabled` is on): one bank per study; each message from someone
on the study's people list goes to Hindsight in the background as the check starts; the bot's own alerts and
their status changes are saved for their participant. Hindsight's AI (the bot's default model, about 3,800 tokens
a message) tags each fact with its participant. It writes codes in more than one way ('1001-022', '1001022') and
tags cannot be edited, so `participant_tags` matches the spellings with the bot's own code rule. Every save and
failure is in the audit trail; a failure emails the admins and never stops the read. `cormon test` with
`CORMON_TEST_MEMORY=1` uses a separate test bank, emptied for each run.
Made-up check (`demo/test-hindsight`, 5 messages): gout kept for 1001-014; the BP typo kept only as 140/90; two
participants in one message kept apart; "the patient" with no code tagged to nobody; the fall-date correction
kept for 1001-022 as replacing the earlier date. A bare "014" does not find "1001-014", the same as the bot's
dates memory today.
To switch on live: run `deploy/hindsight.sh` there again (so it uses OpenAI) and set `memory.enabled: true`
through a settings change, which waits for Eliot to approve settings version 61.

**4. Use the facts (1 day).**
Each new message first makes a quick Hindsight search (28 Oct). Answers can link facts across threads and years,
with the date of the earlier message (28 Oct, gout example). If the search is slow, the bot goes on without memory
and logs it, never fails quietly (28 Oct: slow re-ranking "will time out").

**5. Newest fact wins (half a day).**
"Born 1967", later "born 1972": use 1972, keep both. The older one is replaced, not deleted. (28 Oct)

**6. A page to view, correct and retire facts (1 to 1.5 days).** (email)

**7. Logging and retention (1 day).**
Every save and search is in the audit trail. Facts kept for 7 years, deletable per study, and in the records export.
(Our suggestion, to match the checklist.)

**8. Tests (1 day).**
30 to 50 multi-message cases: gout, born 1967 then 1972, a corrected typo, no participant ID, a fact from another
thread, what was done about a problem. Also measure cost per message. (30 Oct)

Total: about 7 to 8 days.

## Still open (from the email)

1. Can a fact said in a private chat be used in a group chat?
2. Who corrects facts: admins only, or coordinators too?
3. Are facts kept as long as messages?
4. Is OpenAI's patient-data agreement (BAA) in place?

## Results

**Timing test, 31 Oct 2026.** A temporary Google Cloud VM, e2-medium (2 shared CPUs, 4 GB), deleted after.
2,000 made-up facts about 100 made-up participants, 20 each. No AI key (`LLM_PROVIDER=none`), so only search
and re-ranking were timed. Scripts: `deploy/hindsight_timing.py` and `deploy/hindsight_timing_vm.sh`.

| Setup | All facts: usual / worst | One participant's facts: usual / worst |
|---|---|---|
| Default (CPU re-ranker, 300 candidates) | 10.8 s / 18.3 s | 1.0 s / 1.2 s |
| 100 candidates, bucket batching | 2.1 s / 4.2 s | 1.0 s / 1.2 s |
| flashrank | 13.5 s / 29.2 s | 1.2 s / 1.3 s |

- The re-ranker is almost all the time: about 10.5 of 11 seconds. Finding the facts takes about half a second.
- Searching one participant (by tag) passes Eliot's 0 to 3 seconds, on CPU, with no GPU and no Cohere.
- Searching all facts (a message with no participant code) passes only with 100 candidates.
- flashrank is slower, not faster. Don't use it.
- Memory: Hindsight used 1 to 1.4 GB. 4 GB for the server is enough.

**2 GB check, 31 Oct.** The same test on an e2-small (2 GB), 100 candidates: one participant 1.5 s (worst 2.0 s),
all facts 6.2 s (worst 9.2 s), saving 2,000 facts took 225 s instead of 65 s, memory 1.04 GB of 1.9 GB. Under
steady load its CPU slowed until saving 50 facts took about 4 minutes, so the 10,000-fact part was stopped.
John chose 4 GB (e2-medium).

Decision: Hindsight's own CPU re-ranker, 100 candidates, bucket batching on, searches tagged by participant
whenever there is a code. Cohere is not needed for now. Check again when a study has much more than 2,000 facts.

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
Built (31 Oct): before the bot answers a question about a participant, `hindsight.recall_lines` searches that
participant's facts only (by tag; facts tagged to nobody or someone else never come back). Each fact is given to
the answer with who reported it and when, and the answer must say both ("The study coordinator reported on 29 Oct
2026 that she takes St. John's wort every day"). The message being answered is left out. A search over
`memory.search_seconds` (5) or an error gives no facts, is logged (`memory_search_failed`) and the answer still goes
out; every search is logged (`memory_searched`, with the number of facts and seconds).
Alerts too (4 Nov; Eliot, 2 Nov: "even in a group chat" it must relate to everything memory holds, and "censor
itself before it opens its mouth"): when the reader checks a message about a participant, it searches that
participant's facts at the same time and gives them to the judge and the second check, with an instruction to
check every statement against them. The code's prohibited-medicine scan also runs over the earlier facts. A finding
must still quote this message: one whose quote is only in an earlier fact is dropped (`earlier_fact_only`). The
facts are kept off the alert page. A message with no earlier facts gets the same prompt as before.
Before anyone hears an answer or alert that rests on memory, the privacy check (`cormon/privacy.py`,
`config/privacy.yaml`) compares it with who will read it: an answer is reworded (or the safe reply sent), an alert
is held for a person. The check on an alert also reads what its details page says (the AI's reasoning, the
second check, the next step), and an alert with an unlisted reader is held by code.
Made-up check of the privacy check (`demo/privacy-check`, gpt-5.4-mini, two runs each): after a general rewording
of the guide, the first 36 cases scored 35/36 and 36/36 (private facts stopped 31/32, normal facts let through
40/40). A fresh set of 21 written afterwards scored 21/21 and 20/21; its one miss (an outsider in the room) is now
held by code. The open miss: an STI told to the medical monitor alongside an unrelated rash (once in two runs).
Made-up check (`demo/memory-alerts`, 9 two-message stories, two runs): with memory, St. John's wort then first
dose, riluzole started 11 days before randomization, and a prior ALS trial 15 days before baseline were caught in
both runs; labs drawn after a withdrawal were caught in one run of two; itraconazole (named only loosely in the EDC
guide, in no protocol medicine list) was missed. The 4 controls (medicine stopped long before, HIV on a permitted
medicine, stable riluzole, gout) raised nothing. Without memory none of the 5 was caught. The privacy check sent
all 5 real alert emails to the CRA and held one that named HIV.
Made-up check (`demo/test-hindsight-answers`, 4 messages in two chats): a group-chat note that 1001-014 takes
St. John's wort (no date); later, in a private chat, "is any medicine she is on now a problem?". With memory: "Yes.
The study coordinator reported on 29 Oct 2026 that she takes St. John's wort every day ... a prohibited strong
CYP3A4/5 inducer". Without memory: "what medicines is 1001-014 taking now?". The same question for 1001-022 saw
only her amlodipine, never 014's facts. Searches took about 0.5 to 3 seconds.
Not done: a question only about the past ("has she told us anything about her joints?") gets no answer, because an
answer needs the protocol's words behind it. Whether the bot should answer those from memory alone is a question
for Eliot.

**5. Newest fact wins (half a day).**
"Born 1967", later "born 1972": use 1972, keep both. The older one is replaced, not deleted. (28 Oct)
Built (31 Oct): when a search finds two or more facts, one quick AI check (`memory_newer`) says which facts a later
one replaces (same thing, a different value, or stopped). Code keeps a pair only when the replacing fact was
reported later, and follows chains (1967, then 1970, then 1972) to the newest. Nothing is deleted: the old fact is
still given to the answer, marked "NO LONGER TRUE (a later report, on <date>, changed it)", and the answer must not
use it as true. The pairs are in the audit trail (`memory_searched`). If the check fails, every fact is still
given with its date (`memory_newer_failed`). Test messages can now carry a `"date"`.
Made-up check (`demo/test-hindsight-newer`): March 2025 "born 1967, takes St. John's wort every day"; October 2026
"has stopped the St. John's wort ... birth year is 1972"; then "is any medicine she takes now a problem?". The 2025
fact was marked replaced, and the answer was "No. The study coordinator reported on 20 Oct 2026 that 1001-031 has
stopped St. John's wort completely". The search plus the check took about 4 seconds.

**6. A page to view, correct and retire facts (1 to 1.5 days).** (email)
Built (31 Oct): admin page "Patient memory" (`/admin/memory`, admins only, Google sign-in). It lists each
participant with their number of facts, then each fact with who reported it, when, and a link to the message.
"Not linked to a participant" lists facts from messages with no clear study code. An admin can correct a fact's
words, retire it (Hindsight keeps it but leaves it out of answers), restore it, or link an unlinked fact to a
participant (tags cannot be edited, so a tagged copy is saved and the original retired). Each change needs a
reason and goes in the audit trail with the old and new words (`memory_fact_corrected`, `_retired`, `_restored`,
`_linked`). Nothing is deleted. An answer that uses a corrected fact says who corrected it. A message whose facts
an admin changed is not saved again when it is re-read, so the change is not undone. A change is accepted only
from the admin page itself (its own header, JSON, no other site's Origin).
Checked on the laptop's Hindsight: correct, retire, restore and link all worked, with the audit entries. The check
also found Hindsight's AI writing a Hebrew word into an English fact ("dizziness מאז Tuesday"); its instructions now
say to write each fact in the message's language, and the rerun was clean.
Still open (question 2 below): only admins can change facts; whether site coordinators may too is for Eliot.

**7. Logging and retention (1 day).**
Every save and search is in the audit trail. Facts kept for 7 years, deletable per study, and in the records export.
(Our suggestion, to match the checklist.)
Built (31 Oct):
- Logging: saves, searches, failures and every admin change were already in the audit trail (steps 3 to 6).
- Retention: the daily purge also deletes from Hindsight every message whose facts were reported before
  `data.retention_days` (2557 days, 7 years), with its facts (`memory_purged`). A failure emails the admins.
- Forgetting: `cormon forget-participant` also erases the participant's facts. A message only about them is
  deleted from Hindsight; in a message about others too, their facts are blanked and retired. Such messages are
  not saved again on a re-read. If Hindsight cannot be reached it says so and asks to run it again.
- Per study: `cormon forget-study-memory <study>` deletes the study's whole memory. Like purge, it needs a second
  person to approve.
- Records export: the package has `patient_memory.csv` (every fact, current and retired, who reported it, from
  which message). If memory cannot be read, the README says so.
- Backups: `deploy/hindsight_backup.sh` runs every hour as root (`cormon-hindsight-backup.timer`; the bot's user has
  no Docker). It dumps Hindsight's database, restores it into a scratch database and counts the facts against the
  live ones, so each copy is shown to restore. The bot checks the newest every hour (under 2 hours old, check
  passed), copies it to the backup bucket once, and emails the admins otherwise. Restore:
  `YES=1 deploy/hindsight_backup.sh restore <file>`.
- Found while doing this: Hindsight logs each AI call with its full input (`llm_requests`), so a second copy of
  every message's text for a day, and in every backup. It is now off (`HINDSIGHT_API_LLM_TRACE_ENABLED=false`).
  Live has made no such calls yet (it runs without AI until `deploy/hindsight.sh` is run there again).
- Checked on the laptop: backup 9 of 9 facts restored; restore put them back; forgetting 1001-022 removed her 4
  facts and left 1001-014's 4; the purge emptied the test bank.
- Gap to discuss: Hindsight's own AI calls (to pull facts out) are not in the bot's AI log; the audit trail has
  their token counts only.

**8. Tests (1 day).**
30 to 50 multi-message cases: gout, born 1967 then 1972, a corrected typo, no participant ID, a fact from another
thread, what was done about a problem. Also measure cost per message. (30 Oct)
Built (31 Oct): `demo/memory-stories/`, 44 made-up stories, 86 messages (chat and email, group and private, over
days to nearly three years). `cormon test` now reads a story file: several messages in order sharing memory, each
with checks on what memory holds for a participant afterwards (`memory`: has, lacks, count; `unlinked` for facts
with no code) and on the answer as said to the person (`answer_has`, `answer_lacks`). Run with
`CORMON_TEST_MEMORY=1 cormon test demo/memory-stories --study NCT05842941`.
- First run: 103 of 123 checks passed, 29 of 44 stories fully right. Fixed from it:
  - Hindsight's AI miscopied a code: '1001-111' tagged 100111, so her facts were in nobody's record and her
    answer had none. After each save the bot now checks every participant tag against the codes the message
    writes; a tag one character off exactly one of them gets the right code added (`memory_tag_fixed`), anything
    else is logged (`memory_tag_unsure`). Its instructions also say to copy every digit.
  - A name was kept ("Mrs Alvarez"), team logistics were kept as facts (a meeting time, a monitoring visit), and
    an alert raised on a stranger's email went into the participant's memory. The instructions now say to write
    'a participant' and to keep only facts about participants; an alert is saved only when its message came from
    someone on the study's people list, as messages are.
  - A study with nothing saved yet made the memory page and the checks fail; it now counts as no facts.
- Second run: 113 of 123 checks (92%), 36 of 44 stories. Every memory check passed but one (a typo fixed in the
  same message is kept as "replaces the misstated 500 mg"). What fails is how answers use memory:
  - the answer uses the fact but does not say who reported it or when (gout, phenytoin);
  - the answer is right but works out a number itself (an age, a gap in days), so the check on its numbers fails
    and the person gets "I couldn't work out a sure answer" (1972, born; riluzole 30 days in the first run);
  - the answer leaves out a fact it was given (re-consent on version 4; take the dose with food; the flu that
    explains a missed visit);
  - one asked back which regimen the participant is on, before using memory at all.
- Then the answers (31 Oct):
  - An answer that uses a word from an earlier fact (colchicine) but not the date it was reported is asked once
    more, naming the fact (`_unsourced`).
  - The number check counted only plain sums: days between two dates ("2026-06-22 - 2026-06-01") and sums the AI
    wrote as "3 x 40 = 120" now count, and so does the message's own date. Right answers had been sent as "I
    couldn't work out a sure answer".
  - An earlier fact about the very thing asked must be used (the instructions say so).
  - A fact a later report changed is shown as "NO LONGER TRUE", and the dates memory (memory.py) marks its dates
    from that message: one answer had said 1001-115 missed a dose after the message saying it was 1001-116.
  - The 'which fact is newer' check no longer treats "missed the visit" and "visit done a week later" as one
    replacing the other.
  - Two story checks were too strict and were fixed (a date written 2026-06-01; St. John's wort named by the
    protocol itself).
  Last two runs: 116 and 119 of 123 checks (94% and 97%), 39 and 41 of 44 stories. Runs differ by a few checks
  because Hindsight's AI pulls out facts a little differently each time. Still failing: the re-consent answer
  ignores "re-consented on version 4"; the vomiting answer leaves out "take the dose with food"; the missed-visit
  answer sometimes leaves out the flu; Hindsight sometimes keeps a typo's wrong value beside the right one, and
  in 2 of 7 runs saved "1001-111 has been on riluzole" with no participant at all.
- Searches: 28, none failed or too slow, middle 1.4 s, slowest 3.4 s on the laptop. One search in a first trial
  run took over 5 s and was dropped, while Hindsight was busy saving the same message; not seen again.
- Cost per message read (gpt-5.4-mini, $0.75 per million input, $0.075 cached, $4.50 output): about 5,400 input
  tokens (3,900 of them cached) and 280 output, about $0.0027 (a quarter of a cent). Almost all is Hindsight's
  saves: 123 for 86 messages, as the bot's alerts are saved too. The 'which fact is newer' checks add about 70
  input and 20 output tokens a message. 1,000 messages a month: about $2.70.

Total: about 7 to 8 days.

## Still open (from the email)

1. Can a fact said in a private chat be used in a group chat? Answered by Eliot, 2 Nov: yes, memory is used in
   every chat, but "there has to be a censorship step at the end": the bot considers who it is speaking to and
   says only what they may know (built: the privacy check, `cormon/privacy.py`, roles in `config/privacy.yaml`).
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

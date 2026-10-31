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

**1. Timing test (half a day, under $1).**
A temporary Google Cloud VM with 4 GB, made-up data only. Hindsight listens only inside the VM, with a password.
Time 100 searches with the CPU re-ranker. 3 seconds or less: go on. 15 seconds or more: look at Cohere.
Delete the VM after.

**2. Set it up (1 day).**
Live server to 4 GB, about $18 to $25 a month more (needs Eliot's OK). Hindsight runs next to the bot, listening
only on loopback with a password (28 Oct: "Hindsight's dangerous"). Backed up with our data. Same setup on a laptop.

**3. Save facts (1 day).**
- Each message goes to Hindsight while the normal check runs, not one after the other. (28 Oct)
- Each fact keeps who said it and their role, the date, and which message it came from. (28 Oct, email)
- A retain mission so only facts about participants are saved. Hindsight's "observations" (its own AI summaries)
  off at first. (email)
- Typos and corrections stay in the chat only, not in the record. (email)
- No clear participant: nothing is linked; the fact is listed for a person to link. (email; 28 Oct)
- What was done about each problem is saved too. (30 Oct)

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

Timing test: not run yet.

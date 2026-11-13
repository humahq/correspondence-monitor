# Plan: manager process, work queue and pattern finder

From the Eliot/John meeting of 11 Nov 2026 (Peter's lab-kit example). Not started; to take up later. Everything
goes on server 2 first, behind feature switches, off on the live bot.

## The idea

- **Mira stays the only bot**: the only one with a name people see. She stays simple: a message comes in, she
  replies, done.
- **The manager process** and **the pattern finder** are background processes, not bots. They never post
  themselves; when something must be said, they ask Mira to say it.
- No new systems: the same database and server. No Redis: the bot's send queue (`outbox`) already shows how.
- Everything is logged in the audit trail.

## Part A: Mira's to-do list (work queue)

- New table **jobs**: what (tell a person / post in the room / reply in a thread / ask and wait), to whom (a
  **role** from `study.yaml`, never a typed address), about (thread, patient, alert), when (now or later),
  status (waiting, doing, done, failed, cancelled), made by (always the manager).
- Each round of her loop, Mira does the jobs that are due. Her message goes out through the existing send path
  (`routing.enqueue`), so every check still applies: careful check for doctors, privacy check, output check,
  hourly cap, no duplicates.
- A reply in the thread Mira started for a job is linked back to that job, so the manager sees it.
- **Security:** only the manager's code can create jobs. A message never becomes a job ("bot, tell the sponsor
  to ..." is just a message). The manager chooses only from a fixed list of actions, to roles its rules allow.
  Code checks each job before Mira acts.

## Part B: The manager process

- Its own process beside Mira, so it never slows her replies. Reads the same database.
- **Looks only at cases:** problems Mira raised (alerts, safety), times Mira couldn't answer or wasn't sure, and
  news on open plans (a reply arrived, a check-in is due).
- **Step 1, yes/no (cheap):** one Decisions AI call per case: "does this need follow-up beyond the alert?" Most
  stop here.
- **Step 2, plan:** a stronger AI writes a plan from fixed steps: `tell(role, what)`, `ask(role, question, wait
  N minutes)`, `post_in_room(what)`, `check_back(N minutes, what)`, `done_when(condition)`. Code checks it.
  Saved in a **plans** table: goal, steps, status (open, waiting, done, stuck), history.
- **Step 3, run it:** on each event ("reply arrived", "check-in due") it decides the next step and adds jobs.
- **Limits:** most messages and check-ins per plan, a time limit; when stuck, hand to a person (CRA or sponsor
  lead) and stop. It never closes an alert (only humans do).
- **Rules file** `config/manager.yaml` (per study): who may be contacted and about what, what counts as an
  emergency, check-in times, quiet hours, and **site facts** (where the lab is, courier cut-off, kit delivery
  days). The site facts turn "the kit didn't arrive" into an emergency.
- **Answer review:** checks a sample of Mira's answers ("wrong for this person?", e.g. telling the CRA to ask
  the CRA) and makes a correction job (existing `cormon correct`).
- **Suggest mode first:** the manager drafts the plan, a person approves it on the admin page, then Mira acts.
  Later, safe steps (a check-in) may run without approval.
- **Admin page "Plans" board:** waiting, doing, done, stuck; every step, to whom, when; cancel or edit.

**Lab-kit example:** the coordinator writes the kit didn't arrive, patient comes Thursday. Mira flags a risk and
says she will follow up. The manager sees an emergency (lab three states away, FedEx cut-off 4 pm) and plans:
tell the sponsor lead to order today, ask for confirmation, check back in 30 minutes, then tell the coordinator and
the room the delivery date. The CRA approves; Mira does each step. No answer by the cut-off: stuck, the CRA is
told.

## Part C: The nightly pattern finder

- **Activity table:** one row per event: time, site, person (by role), action, category, patient code, outcome,
  delay ("Tue 09:10, Site 01, coordinator, reported late, lab kit"). Filled **by code** from what is already
  stored (alerts, cleared times, questions and topics, unanswered questions, plans); a cheap AI tags a category
  only when missing.
- **Statistics, not AI**, once a night:
  - rate comparisons per person or site (Fisher's exact test, for small numbers);
  - links between things, e.g. lab kits and Mondays (chi-square, Cramér's V, mutual information);
  - trends, week over week;
  - repeated questions (simple text clustering);
  - clustering on categories (k-modes) to explore. Not linear regression.
- **No false alarms:** correct for many tests (Benjamini-Hochberg), a minimum number of events, a "watch list"
  below it.
- **Then AI:** only for the few patterns found, a strong model writes a short note with possible actions.
- **Results:** a weekly report and a "Patterns" page for the CRA and sponsor lead. Patterns naming a staff member
  go only to them, never to the room or the person named.
- **First patterns:** late lab kits or supplies; visits near or outside windows per site; the same deviation
  again per site or person; the same questions again; alerts left uncleared; questions the bot couldn't answer.
- **Test:** fake data with planted patterns: it must find them, and find nothing in random data.

## To do (in order)

- [ ] 1. **Spot more problems:** delay, risk of breaking the rules, risk of a patient dropping out (the
      lab-kit message was not seen as a problem). Needed first: the manager only reads flagged cases.
- [ ] 2. **Mira's job list:** `jobs` table; Mira runs due jobs through the existing send path; a command for a
      person to add a test job.
- [ ] 3. **Manager in suggest mode:** yes/no check, plans, `config/manager.yaml` with site facts, Plans board
      with an Approve button.
- [ ] 4. **Replies and check-ins:** the plan loop, "stuck" handed to a person, the limits.
- [ ] 5. **Answer review and corrections.**
- [ ] 6. **Activity table** (filled from past data too), the nightly pattern finder, the weekly report.
- [ ] Each step: a test set of realistic cases (lab kit, consent, drug temperature, missed visit), scored before
      and after, using the cheapest AI models first.

## Open questions (John and Eliot)

1. Manager in suggest mode first, a person approving each plan? (Suggested: yes.)
2. Site facts (lab distance, courier cut-off): a new section in `study.yaml`, filled in by the CRA? (Suggested.)
3. Who sees patterns that name staff? (Suggested: only the sponsor lead and the CRA.)
4. Manager as a separate process, not inside Mira's loop? (Suggested: separate.)

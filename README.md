# Correspondence Monitor

A monitor for clinical trial messages. It reads the emails and chat messages it is included on, checks them
against the study's own documents, and tells the right people when something breaks the protocol. It answers
questions about the study and about each patient, from the documents and from what earlier messages reported. It
never judges safety information.

**Live study:** HEALEY ALS Platform Trial, **Regimen G (DNL343), NCT05842941**. The documents are the Master
Protocol V6.0 and the Regimen G Appendix V2.0 (`studies/NCT05842941/docs/`). An older demo study, **MAP
(NCT04098666)**, is still in the code; its lab manual is a placeholder.

**To install it on your laptop and test it, see [PETER_SETUP.md](PETER_SETUP.md).**

## The three bots

| | Server 1: live | Server 2: test | Laptop demo |
|---|---|---|---|
| Talk to it in | Google Chat (DNL343 rooms) and email | Google Chat (Test Study Room) | Mattermost on your laptop |
| Bot | bot@huma.ai | bot_test@huma.ai | Mira Belle |
| New features | only those John said yes to | all of them | all of them |
| Updated | only when John runs the release | after every merge to main | when you pull (see PETER_SETUP.md) |

Details and the update steps: [docs/servers.md](docs/servers.md). Every new feature has a switch, off on the live
bot until John says yes: [docs/features.md](docs/features.md).

## The workflow: what happens to a message

1. **A message arrives.** An email with the study address in To or Cc (`bot+NCT05842941@huma.ai`), or a chat
   message in a room the bot is in. It is stored whole: text, headers, attachments, time and channel. It belongs to
   the study of its address or chat room; the bot never guesses the study.
2. **The bot reads it.** For each patient it works out what happened and when, and whether it is **done**,
   **planned** or **old news**. Dates like "9/15" or "wk2 done 10/6" become days from Baseline (Day 0).
3. **It remembers.** The facts about each patient (medicines, events, visits, dates, who reported them) are saved
   in patient memory, tagged with the patient's code. Only messages from people on the study's list are saved. A
   correction ("the fall was on 5 Nov, not 3 Nov") replaces the old fact.
4. **It checks the rules.**
   - Dates, times and numbers are checked by plain **code**, so the same message always gets the same answer.
   - Rules without a number ("consent before any study procedure") are judged by the **AI**.
   - A second AI reads the original protocol section and must agree. The site's words must be in the message, and
     the protocol's words must be found word for word in the document.
5. **It decides how urgent it is, and who to tell** (see "Who gets what").
6. **It answers** when someone talks to it: a question, a request, or a reply to its alert.
7. **It records everything.** Every message, decision, send and person's action goes into an append-only log.

### Where the rules come from

`cormon ingest` reads the study documents into sections. From them it builds the **study model**:

- **code checks**: each date, time or number limit with its quote (`cormon/checks.py`);
- **topic briefs**: short summaries for the AI, each line citing its section (`cormon/guide.py`).

`cormon rules --report` shows what was kept, dropped or merged, and why. `cormon rules --checklist` lists every
inclusion/exclusion criterion and lab-manual step, with its rule or the reason it has none. The live model is kept
in `studies/model/model.sql`, so every server checks with the same rules.

## Who's who

The bot tells people by their **role** in the study (`studies/<study>/study.yaml`). Right now the roles use test
addresses:

| Role | What they do | Test address now |
|---|---|---|
| **Coordinator (CRC)** | Works at the site. Writes the day-to-day messages about patients. | `johnrohit94@gmail.com` |
| **CRA** | The sponsor's monitor for the site. Gets the bot's alerts and decides what to do. | `johnernest978@gmail.com` |
| **Sponsor lead (ClinOps)** | Runs the study for the sponsor. Gets urgent alerts and the daily summary. | `john@huma.ai` |
| **Medical monitor** | The doctor who handles safety. Gets safety information, unjudged. | `john+medicalmonitor@huma.ai` |
| **Principal investigator (PI)** | The doctor in charge at the site. The bot is careful with them. | `eliot@huma.ai` (for testing) |
| **Testers** | Peter and Eliot. Can clear and disregard alerts; get no alert emails. | |

## Who gets what

| What the bot finds | Where it goes |
|---|---|
| **Immediate** (a consent problem, or a problem planned within 48 hours) | Its own post in the urgent room and the study room, an email to the CRA and to the sponsor lead. If a site coordinator wrote the message, they get a short careful note too. |
| **Medium** (a problem that already happened, or one planned later) | A post in the study room and an email to the CRA, sponsor lead in CC. It is in the daily summary. |
| **Low** (for example, a visit a few days outside its window) | A post in the study room, an email to the CRA ("For your to-do list") and the daily summary. |
| **Safety** (a death, side effects, falls, hospital stays) | Recorded as "safety information received; not reviewed" and sent to the medical monitor unjudged. A serious event gets the reporting warning (see below). |
| **Nothing wrong, or not about the study** | Nobody. The bot stays quiet. |
| **A question** | The bot answers the person who asked, in the same thread or by email reply. |

**One message, one alert.** Several problems in one site message come together in **one** post and **one** email,
numbered 1, 2, 3. Immediate problems always go out on their own.

### What an alert looks like

```
Medium · DNL343 · G-708-002 · Site 01 (MGH)

3 problems found:
1. Baseline CSF was collected too early. (Medium, Item #19)
2. The study drug storage temperature was above the allowed limit. (Medium, Item #21)
3. The Week 2 phone visit was done late. (Low, Item #20)

What to do:
1. Record a deviation ...

──────────
Evidence for problem 1
Site wrote: baseline csf was 9/5 ... baseline + first dose were 9/15
Study rule: For the Baseline visit, CSF may be collected at any time within one week prior to ...
From: Regimen-Specific Appendix G, DNL343, V2.0, section 6.1 Baseline Visit
```

- **Study rule** is always the protocol's own words, found word for word. If the bot can't find them, it shows no
  quote, says which section to read, and marks the problem **please check**.
- **Please check (not sure)** means the bot thinks it is a problem but is not certain. "Why not sure" says why.
- Each problem has an **item number** (#19). The **details** link opens a page with everything the bot used.

### Replying to an alert: "cleared" and "disregard"

Only the CRA, the sponsor lead and the testers can do this, in chat or by email.

| You write | What happens |
|---|---|
| `cleared` (in the alert's thread, or as a reply to its email) | Every problem in that alert is marked resolved. |
| `cleared #20` | Only problem #20 is marked resolved. |
| `disregard` / `disregard #20` | The same, but the problem is dropped as not real. |

The bot confirms in the thread, for example "Done: #20 cleared, by John Huma."

## Asking the bot a question

Anyone on the study's list can ask, in chat or by email. For example: "When is G-711-001's Week 8 visit due?"

```
It is due between 21 Sep and 5 Oct 2026.

Details and the protocol's words
```

**How an answer is made:**

1. The bot works out what is asked, and about which patient. A short code ("131") that fits two patients gets a
   question back: "Which participant do you mean: G-708-131 or G-718-131?"
2. It gathers what could answer it: the protocol sections on the topic, the patient's facts from memory, and the
   visit windows worked out by code from the patient's dates.
3. The AI writes the answer, saying for each sentence where it came from: a reported fact, the protocol's words,
   a window code worked out, or the person's own message.
4. **Code checks each sentence against its source.** The fact must really be in memory. The protocol words must
   really be in the protocol. Every number must be in the source or worked out by code. A sentence with no
   protocol words may not add a rule, a permission or a judgment. A sentence that fails is dropped.
5. If nothing is left, the bot says "I couldn't find this in the study documents, so I won't guess. Please ask your
   CRA." If one fact would settle it (a baseline date, which visit), it asks for that instead.
6. The answer is reworded short and plain, and checked again: no new number, no medical opinion, and nothing
   about a patient that the people in the chat should not see.

**What answers always do:**

- Name who reported a fact about a patient, and when: "The coordinator reported on 12 Oct that she has gout."
- Use the latest fact when two disagree, and never use one that was corrected.
- Work out sums (days between visits, a window from Baseline) in code.
- Start with "This may be a Serious Adverse Event. Contact the PI and the Medical Monitor now." when someone may be
  seriously hurt, with the reporting deadline.
- Never ask for names, birth dates or contact details. Never judge dose changes, severity or whether an event is
  related to the drug: the investigator decides those.

**Follow-ups.** The bot reads the conversation before the message: a reply in a thread is read with that thread,
so "and week 8?" or "what about the other one?" is understood.

**Requests that are not study questions.** The bot sorts them first (`cormon/asks.py`). A request to hide or
change records gets "I can't help with that". A question about the bot itself (passwords, settings, its database)
gets "I can't share that". A task (slides, code) or an opinion ("which criteria will be hardest?") gets "I can't
do that", with the protocol's sections on the topic as a file in a 1-on-1 chat or email. Every outgoing message
is checked last for anything that looks like a key, password or server setting.

## When the bot speaks in chat

| Where | What the bot does |
|---|---|
| Study room, normal chat | Stays quiet. It still checks every message and posts an alert for a real problem. |
| Study room, talking to the bot (tag it, start with "bot,", or reply in a thread it started) | Always answers, in the thread. With nothing wrong: "I checked this against the protocol and found no problem." |
| Study room, serious harm reported | Always gives the Serious Adverse Event warning. |
| 1-on-1 chat with the bot | Always replies: an answer, the problem itself (the CRA is emailed; nothing is posted in the group), or "Thanks for letting me know." |
| Another group chat the bot is added to | Same rules as the study room. Its alerts stay in that chat. People not on a study's list are ignored. |
| Email | Answers questions; emails alerts. |

### Doctors in the chat

Principal investigators and other doctors (`pi`, `sub_investigator` roles) matter most to a sponsor, so the bot
never embarrasses them in front of others.

- Before an alert is posted in a chat with a doctor in it, code and then the AI check whether it could read as
  criticism of them. If it could, it goes by email to the CRA and sponsor lead instead.
- A doctor who talks to the bot always gets a calm reply. If the bot found a problem in what the doctor wrote, it
  replies "Thank you for letting me know. I have shared this with the study team so they can follow up." and the
  study team gets the alert privately.
- The Serious Adverse Event warning is never held back.
- Rules are in `config/careful.yaml` (or a `careful:` list in a study's `study.yaml`). Test:
  `CORMON_HOME=practice7 .venv/bin/python tests/scenarios/doctor/run.py`.

## Patient memory

Each study message's facts are saved in **Hindsight** (`cormon/hindsight.py`, [docs/hindsight-plan.md](docs/hindsight-plan.md)),
one bank per study, each fact tagged with the patient's code.

- A later message is checked with the patient's earlier dates. "G-711-001 week 4 visit done today", with Baseline
  given 6 weeks earlier, is caught as late, and the evidence names the earlier message.
- Answers use the facts, saying who reported each one and when.
- Old problems are never alerted again: a problem needs at least one fact from the new message.
- Only messages from people on the study's list are saved. A code the memory AI copied wrong is fixed against the
  codes the message writes, or logged.
- Memory is on for server 2 and the laptop demo. On the live bot it is off until it is switched on through a
  settings change.
- `cormon forget-participant STUDY PID --yes` deletes what is remembered about one patient.

**Scoped memory** (`features.scoped_memory`, Eliot and John, 13 Nov), on server 2 and the demo:

- Only real-world facts about patients are kept. No questions, plans, chat events, bot alerts, site matters or
  staff, and never what someone says the protocol says. Rules come only from the protocol.
- A fact never says who reported it. "Who reported ...?" and questions about how staff work are turned down
  (EU AI Act: no AI to judge staff).
- Each fact is tagged with its site and reporter. A search must match the patient and one of the asker's own
  sites, and code drops anything else. The patient's saved visit dates follow the same rule. A study has many sites, which may be competitors. In a shared room, memory
  is used only when the study has one site.
- Study staff review their facts at `/memory/<study>`. The PI also sees the site's facts. Each correction needs a
  reason and goes in the audit trail.

**Durable site facts** (`features.durable_facts`, Eliot and John, 13 Nov), on server 2 and the demo:

- What happened at a site ("the lab fridge door was left open overnight") and how a site works ("our courier
  picks up at 3 pm") are saved from site messages, apart from patient memory, filed under the sender's own site.
- The bot uses them to answer questions inside that site ("any fridge problems at Site 01?"), with the date
  reported. Never about a patient, never who did something, never a rule.
- `cormon site-facts list | add | correct | retire` (every change is in the audit trail).

## What the bot will NOT do, by design

- **Judge safety:** no judging of side effects, deaths or hospital visits.
- **Alert old news:** no alert on past events that were already dealt with.
- **Alert missing information:** no alert just because something wasn't mentioned.
- **Guess:** no answer without a checked source behind it.
- **Guess the study:** mail to the general address `bot+clinical@huma.ai` is never checked.

## How it works (code map)

1. **Capture** (`cormon/capture.py`, `cormon/channels/`). Email (IMAP or the Gmail API), Google Chat, Microsoft
   Teams and Mattermost. Edited and deleted chat messages are kept as new versions beside the original.
2. **Study model** (`cormon/ingest.py`, `cormon/guide.py`, `cormon/checks.py`).
3. **Reader** (`cormon/reader.py`, chosen by `checker:` in `study.yaml`). For each patient: scope, date notes in
   code, an AI judge on the topic briefs, the code checks, duplicates merged, and a second AI confirming each
   finding against the original section.
4. **Evidence.** Quotes are looked up sentence by sentence in the real documents; words that can't be proven are
   not shown.
5. **Memory** (`cormon/hindsight.py`, `cormon/memory.py`) and **conversation** (`cormon/conversation.py`).
6. **Answers** (`cormon/answers.py`, `cormon/asks.py`, `cormon/privacy.py`, `cormon/outcheck.py`).
7. **Triage** (`config/triage.yaml`, a first-match rule list) and **routing** (`config/routing.yaml`,
   `config/templates.yaml`). Replies ("cleared", "#N") are matched to the alert by email headers or chat thread.
8. **Record.** An append-only audit log, sealed in a hash chain.

## Quick start (local, no accounts needed)

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
echo "OPENAI_API_KEY=sk-..." > .env

cormon init
cormon ingest                 # builds the study model (about 2-3 minutes the first time)
python -m pytest -q tests/    # the fast checks (no AI calls)
python demo/rehearse.py       # runs every demo scenario (MAP study) and checks the result
cormon items                  # what it found
cormon item 2 --drafts        # one item: evidence, every drafted message, history
cormon digest --hours 24      # preview the sponsor digest
```

With `channels.email: sim` and `channels.teams: sim` (the default), mail is read from `data/sim/inbox/` and "sent" to `data/sim/outbox/`. Teams posts are appended to `data/sim/teams/*.md`.

**Test Regimen G on the laptop** (uses a practice copy, so the live data is safe; run one test at a time, never two in parallel, because they share the practice database):

```bash
CORMON_HOME=practice7 CORMON_VIEW=briefs CORMON_MODEL=gpt-5.4-mini \
  .venv/bin/cormon test demo/test-chat-g/chats --study NCT05842941 --out /tmp/run1
```

## Demo scenarios for the MAP study (`demo/scenarios/`)

| File | What it shows | Expected |
|---|---|---|
| 01 | Participant randomized with eGFR 41 (protocol excludes < 45) | Medium: CRA email (sponsor cc), study channel |
| 02 | Study drug to start tomorrow, MRI pushed to next week | Immediate: alert channel, CRA, sponsor, hedged site notice |
| 03 | Plasma spun 2.5 h after the draw (lab manual: 60 min) | Medium |
| 04 | A three-week-old resolved deviation, plus a completed visit | No alert for the old one; good-news item |
| 05 | Participant admitted to hospital | Out of scope; sender redirected to the medical monitor |
| 06 | Mail to bare `bot@huma.ai` | General message, not matched |
| 07 | Teams: BP 168/92, randomizing this afternoon | Immediate |
| 08a/b | Bot dropped from a thread, then re-added | Missed messages re-read, issue found, writer nudged to keep copying |
| (auto) | CRA replies "disregard" | Item suppressed, recorded as a CRA decision |

## Operator commands (all work while the listener runs)

```
cormon status                         state, queue, recent activity
cormon messages / message ID          what came in, with its event model
cormon items [--tier T] [--clearing] [--sponsor-view]
cormon item ID [--drafts]             evidence, drafts, full history
cormon rerun MESSAGE_ID               re-classify one message (fresh model call)
cormon draft ITEM_ID [--force --send] produce drafts on demand
cormon suppress ITEM_ID --code CODE --reason ...  / unsuppress / clear --code CODE [--undo]
                                      CODE: fixed, allowed, misread, duplicate, deviation_logged, not_this_study,
                                      other (other needs --reason)
cormon override ITEM_ID --tier medium | --recipient PERSON_ID | --send-draft no | --counts no
cormon correct OUTBOX_ID --text "..." --reason "..."   follow-up with the right words, same people and thread
cormon recall OUTBOX_ID --reason "..."                 "please disregard" follow-up; not sent yet: held instead
cormon digest --hours 24 [--send]     digest for any window; the schedule is unaffected
cormon pause / resume                 capture continues while paused; nothing is classified or sent
cormon config show|set|edit|history   routing rules, thresholds, templates (changes recorded with who/when)
cormon config approve FILE VERSION_ID a second person approves the config version in use
cormon note add --scope message|person|study|global --target X --kind fact|instruction [--from DATE] "text"
cormon note edit|approve|history|retire --target NOTE_ID   a note applies once another person approves it
cormon drift [--sample] [--send]      the weekly quality check (also emailed weekly to the admins)
cormon drift --mark MESSAGE_ID ok|missed [--note ...]     record your review of a sampled message
cormon approve ID / approvals         approve another person's two-person request; list the requests
cormon log [--item ID] [--message ID] the audit log
cormon rules [--pattern eligibility]  the study model
cormon rules --report                 what was kept, dropped or merged and why; number rules; contradictions
cormon rules --checklist              every inclusion/exclusion criterion and lab-manual step, with its rule or why none
cormon rules-suggest                  AI suggests keep or remove for each rule, with a reason (turns nothing off)
cormon export-rules FOLDER            Word + Excel of the rules; the Excel has "AI suggests", "Why", "Team decision"
cormon rules-decide SHEET.xlsx        apply the "Team decision" column (keep / remove); decisions survive a rebuild
cormon rules-decide --rule ID --decision keep|remove [--note "..."]   decide one rule
cormon reset --yes                    archive the database, start fresh (study model kept)
cormon outbox [--send ID]             outgoing messages; send or resend one now
cormon backup [--check FILE]          back up the database now and check the copy (also done daily)
cormon restore FILE --yes             replace the database with a checked backup (stop the service first)
cormon purge [--days N] --yes         clear message text older than N days (data.retention_days)
cormon forget-participant STUDY PID --yes   delete the dates and events remembered for one participant
```

Two people: reset, restore, purge, forget-participant, import-model, and disregarding (or marking "not counted")
an alert the sponsor would see. Run the command; it records a request. A different person runs
`cormon --as NAME approve ID`; then run the command again with `--approval ID` (within 24 hours, once).
`compliance.two_person: false` turns this off in a practice copy. Names come from `--as` or the computer login,
which the bot cannot check.

Each study sends at most `outbox.hourly_cap_per_study` messages an hour (default 100; 0 = no cap). The rest wait,
and the admins are emailed once an hour while it lasts. Alerts about the bot's own health are never held.

### Alerts arrive once
- Each outgoing message is taken by one sender only, so two senders never send it twice. The same draft queued twice within 6 hours is kept once (`cormon draft ID --force` resends on purpose).
- A failed send is tried again after 30 seconds, 2, 10 and 30 minutes and 2 hours; then it is marked failed.
- If the service stops mid-send, the bot asks the channel whether the message went out (each chat post carries its own id; each email's id is looked up in Sent Mail). Sent: marked sent. Not sent: sent again. Can't tell: marked failed for a person to decide, never sent twice.
- A site message left half-read is read once more; a second stop marks it an error. An alert made but never sent goes out 10 minutes later.
- Anything that fails for good is emailed once to `system.ops_emails` (or each study's clinops lead), with the command that fixes it.
- The database is backed up every hour to `data/backups/` (last `system.backup_keep`, default 24). A backup counts only after the copy passes an integrity check and holds the same rows as the live database. With `system.backup_bucket` set, each one is also gzipped to that Cloud Storage bucket, and once a week a restore drill downloads the newest copy, checks it and times it. A failure emails the admins. To restore from the bucket: `cormon backup --fetch` (the newest, or give its name), then `cormon restore <file> --yes`.

### Who can do what
- **Email sender check.** Gmail checks SPF, DKIM and DMARC on each email. Only the topmost result from Google counts. An email that fails is still checked for problems, but it can't clear or hide an alert; the attempt is logged and the alert's details page warns that the sender check failed.
- **Study scope.** "cleared" or "disregard" only changes alerts of the study the message came in on. The check is inside the code that changes alerts, so no path can skip it.
- **Sign-in for item and answer pages** (off until set up). With `web.login.enabled: true`, a page opens only for someone signed in with Google whose email is on that study's people list, or in `web.login.allowed_domains` (default huma.ai). Every view and refusal is logged. Setup: in Google Cloud console, APIs & Services, Credentials, create an OAuth client ID of type "Web application" with the redirect URI `<web.base_url>/auth/callback`; put its id in `web.login.client_id` and its secret in Secret Manager (`web.login.secret: {project, name}`). While the consent screen is in testing, add each study person as a test user. A copy on this computer (web.base_url a loopback address, not the live copy) can instead give local accounts (web.admin.local) that share one password in `ADMIN_PASSWORD`; a server never offers them, and the demo generator turns them on (cormon/chatdemo.py).

### Patient data
- Dates of birth, phone numbers, social security and medical record numbers are masked in the audit log and in every message the bot sends (numbers in the study documents themselves, like a safety hotline, stay). Participant study ids stay: they are coded, not personal.
- The system log (journal) holds no message text.
- Message text, attachments and remembered participant dates are cleared after `data.retention_days`. Off until set: how long to keep them is the sponsor's decision. Alerts keep their short quote as the monitoring record.
- Message text is sent to the AI model provider to be read; that needs the provider's data agreement to cover patient data.

Use `--as NAME` (or `CORMON_OPERATOR`) to set the identity recorded for an action. Operator and CRA values are marked as such and are never overwritten by a later automatic pass. An override that would leave an item inconsistent is refused and explained; `--force` applies it anyway and records that.

## Going live

The name, avatar and email sign-off are per deployment. Put them in an untracked `config/settings.local.yaml` (never committed; keep it on the server, where a release leaves it in place), not in `config/settings.yaml`:

```yaml
bot:
  name: ""         # this deployment's name (e.g. "Study monitor"); empty uses that
  avatar_url: ""   # an image URL for the admin header; empty keeps the plain mark
  signature: ""    # the whole email sign-off; empty uses the name + the automated-message disclaimer
```

The Google Chat app's own name and avatar are set in the Cloud console (**Google Chat API → Configuration**), per project; the settings here keep email, the admin pages and ops warnings in step with it.

### Gmail with the app password (simplest; same as the scheduler)

1. `gcloud auth login` as someone who can read the `kol-gmail-bot-app-password` secret in `optical-depth-506808-t0` (or set `CORMON_GMAIL_APP_PASSWORD` in `.env`).
2. `cormon auth imap` checks the login.
3. Add `bot+NCT04098666@huma.ai` under Gmail → Settings → Accounts → **Send mail as** (signed in as bot@huma.ai), and give it the deployment's bot name.
4. `cormon config set settings channels.email imap`, then `cormon run`.

The inbox is shared with the scheduler, so only mail sent to a `bot+<study>` address or to the general address `bot+clinical@huma.ai` is read, and nothing in the inbox is marked or moved. Mail that arrived before the first run is ignored.

### Gmail API (alternative)

1. In a GCP project on the huma.ai Workspace, enable the **Gmail API**. Create an OAuth client of type **Desktop app** (consent screen: *Internal*) and save the JSON as `secrets/gmail_client.json`.
2. `cormon auth gmail` and sign in as bot@huma.ai. The token is saved to `secrets/gmail_token.json`.
3. In Gmail (as the bot) go to Settings → Accounts → **Send mail as** and add `bot+NCT04098666@huma.ai` as an alias, with the deployment's bot name, so replies leave from the study address. `cormon gmail send-as` lists what is configured; `cormon gmail add-aliases` also keeps each alias's display name in step with `bot.name`.
4. Set `channels.email: gmail` in `config/settings.yaml`.

### Google Chat

The bot posts and reads as bot@huma.ai. The Chat API and People API are already on in project optical-depth-506808-t0.

1. In Google Chat (as bot@huma.ai or anyone), create a space, for example **MAP study alerts**. Add bot@huma.ai and the people who should see alerts.
2. In the Cloud console, open **Google Chat API → Configuration**. Give the app a name (e.g. "Study monitor"), an avatar URL and a description. Leave **interactive features** off. Save. Set the same name and avatar in this deployment's `config/settings.local.yaml` (see Going live above) so email and the admin pages agree.
3. Under **APIs & Services → Credentials**, create an **OAuth client ID** of type **Desktop app** (consent screen: *Internal*). Save the JSON as `secrets/gchat_client.json`.
4. Run `cormon auth gchat` and sign in as **bot@huma.ai**. The token is saved to `secrets/gchat_token.json`.
5. Run `cormon chat-spaces`. Put the space name (`spaces/...`) as `space:` for both `study_channel` and `alert_channel` in `studies/NCT04098666/study.yaml`.
6. Set `channels.teams: gchat` in `config/settings.yaml`.

### Microsoft Teams

A personal Microsoft account cannot do this. You need a work tenant, for example a Microsoft 365 Business Basic trial or the Microsoft 365 Developer Program sandbox.

1. Create the bot as a normal user in our tenant (e.g. the study bot account). Create a team with a **Study** channel and an **Alerts** channel, add the bot user, and invite the customer's people as guests.
2. In Entra ID, register an app. Under Authentication, turn on **Allow public client flows**. Add the delegated Graph permissions `ChannelMessage.Send`, `ChannelMessage.Read.All`, `Chat.ReadWrite`, `Team.ReadBasic.All`, `Channel.ReadBasic.All` and `User.ReadBasic.All`, then **grant admin consent**.
3. Put the tenant ID and client ID under `teams:` in `config/settings.yaml` and set `channels.teams: graph`.
4. `cormon auth teams` (device code; sign in as the bot user), then `cormon teams-channels` to get the IDs for the `teams:` section of `studies/NCT04098666/study.yaml`.

### GCP (the cloud copy)

```bash
./deploy/create_vm.sh                          # create or update (PROJECT, ZONE, NAME can be overridden)
./deploy/grant_access.sh someone@huma.ai       # let a person use ./bin/cormon-cloud
```

**Updating a server.** Follow [docs/servers.md](docs/servers.md). Server 2: `echo yes | CORMON_VM=cormon-edge ./deploy/release.sh` after every merge to main, once main's gate has passed. The live server: only John runs `./deploy/release.sh`. The release checks the gate, the packages and the study model, backs up the database and the code, restarts the bot and checks its health. `./deploy/create_vm.sh` is only for creating a new server.

- When copying single files by hand, make the archive with `COPYFILE_DISABLE=1 tar --no-xattrs ...`. Otherwise macOS adds hidden `._` files, which stop the bot reading its settings.

What this sets up:

- **Server:** an always-on `e2-medium` (4 GB) running Ubuntu 24.04 in `us-east1-b`, about $24 a month, with a reserved address. The bot runs as a systemd service. Hindsight (patient memory, `deploy/hindsight.sh`) runs beside it in Docker, on 127.0.0.1:8888 only, with a key. Until the resize (`deploy/resize_vm.sh`) it is the free-tier `e2-micro`.
- **Gmail password:** the server reads it from Secret Manager with its own service account (`cormon-vm`), which may read that one secret and nothing else. No password is copied onto the server.
- **Item pages:** served over HTTPS by Caddy at `https://<ip-with-dashes>.sslip.io`. The app itself only listens on localhost.
- **SSH:** open only to Google's IAP range, and people log in with their own Google account (OS Login).
- **First install:** the server starts with an empty database containing the laptop's study model. Updates keep the server's data.
- **Backups off the server:** `./deploy/create_backup_bucket.sh` makes the bucket the hourly backups are copied to; then set `system.backup_bucket` on the server (two people).

**Every change names its task (SDL-02).** Each commit message names the GitHub issue (or pull request) it is for, for example `Hourly backups off the server (#14)`. The gate on GitHub fails a change with a commit that names none, or names a number that is not an issue or pull request here, and `deploy/release.sh` deploys only a passed commit. Make the issue first (`gh issue create`), and turn on the local reminder once per clone: `git config core.hooksPath .githooks`.

## Switching to Peter's study

1. Create `studies/<study-id>/` with a `docs/` folder holding the protocol, lab manual and consent form.
2. Copy `study.yaml` and edit the people, roles, sites, problem classes and Teams channel IDs.
3. Run `cormon ingest --study <study-id>`. Mail to `bot+<address_tag>@huma.ai` is then routed to it.

## Known limits of this build

- Answers: the protocol words shown are always real, but the sentence above them is written by the AI. Check it against the words. Known slip: for a named visit it sometimes adds a test the schedule does not list there (for example ALSFRS-R at a phone visit).
- The same problem can sometimes be alerted twice; some headlines are worded loosely; some date problems arrive as "please check" when they are clear.
- The web address is a test one (`sslip.io`); a proper Huma address needs one DNS entry.
- The MAP lab manual is a placeholder; real lab manuals will need their own check.
- There is no approval queue: drafts are sent automatically (open question 1). `cormon pause` and `--send-draft no` are the brakes.
- Item detail pages are protected by an unguessable link (over HTTPS on the cloud copy), not by a login.
- Later: when mail reaches the general address (bot+clinical@huma.ai), reply once saying it is not linked to a study and naming the study addresses. It must never guess the study.
- The arrears pattern pass, a structured study data model, CTMS write-back and a correction UI are deferred, as the requirements say.

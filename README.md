# Correspondence Monitor

A monitor for clinical trial messages. It reads the emails and Google Chat messages it is included on, checks them against the study's own documents, and tells the right people when something breaks the protocol. It never reviews safety information.

**Live study:** HEALEY ALS Platform Trial, **Regimen G (DNL343), NCT05842941**. The documents are the Master Protocol V6.0 and the Regimen G Appendix V2.0 (`studies/NCT05842941/docs/`).

An older demo study, **MAP (NCT04098666)**, is still in the code. Its lab manual is a placeholder written for the demo.

## Testing guide (for Eliot and Peter)

**How it's set up:** the bot runs on a small Google Cloud server that is always on (project `optical-depth-506808-t0`, server `cormon-demo`). You don't install the bot. You talk to it in Google Chat or by email. To run test sets or look inside, you type short commands in Terminal, signed in with your own huma.ai Google account.

Only one copy of the bot should read the inbox and chat, so **don't start the bot on your own laptop.** Always use `./bin/cormon-cloud`.

### Part 1: One-time setup (about 5 minutes; only needed for the Terminal commands)

**Step 1. Install the Google Cloud tool.** Open the Terminal app and type:

```
brew install --cask google-cloud-sdk
```

No Homebrew? Download it from <https://cloud.google.com/sdk/docs/install> instead.

**Step 2. Sign in with your huma.ai Google account:**

```
gcloud auth login
```

A browser window opens. Choose your huma.ai account and click **Allow**. (John must first give you access: `./deploy/grant_access.sh you@huma.ai`.)

**Step 3. Download the code:**

```
git clone https://github.com/humahq/correspondence-monitor.git
cd correspondence-monitor
```

**Step 4. Check that it works:**

```
./bin/cormon-cloud status
```

The first time, it asks to create a key. Press **Enter**. If you see the bot's status, you're ready. Run all commands from inside the `correspondence-monitor` folder.

### Who's who

The bot tells people by their **role** in the study. Right now the roles use John's test addresses:

| Role | What they do | Test address now |
|---|---|---|
| **Coordinator (CRC)** | Works at the site. Writes the day-to-day messages about patients. | `johnrohit94@gmail.com` |
| **CRA** | The sponsor's monitor for the site. Gets the bot's alerts and decides what to do. | `johnernest978@gmail.com` |
| **Sponsor lead (ClinOps)** | Runs the study for the sponsor. Gets urgent alerts and the daily summary. | `john@huma.ai` |
| **Medical monitor** | The doctor who handles safety. The bot never alerts them; it tells people to contact them. | none |
| **The bot (email)** | Reads every email it is copied on. | `bot+NCT05842941@huma.ai` |
| **The bot (chat)** | Reads and posts in the study's chat spaces. | spaces **DNL343 Study Updates** and **DNL343 URGENT** |

### Who gets what

| What the bot finds | Where it goes |
|---|---|
| **Immediate** (a consent problem, or a problem planned within 48 hours) | Its own post in **DNL343 URGENT** and **DNL343 Study Updates**, an email to the CRA, and an email to the sponsor lead. If a site coordinator wrote the message, they get a short careful note too. |
| **Medium** (a problem that already happened, or one planned later) | A post in **DNL343 Study Updates** and an email to the CRA, with the sponsor lead in CC. It goes in the sponsor lead's daily summary. |
| **Low** (for example, a visit a few days outside its window) | A post in **DNL343 Study Updates**, an email to the CRA (subject "For your to-do list"), the CRA's to-do list and the daily summary. |
| **Safety** (a death, side effects, falls, hospital visits) | Only the person who wrote gets a reply: "please take it to the medical monitor". Nothing is judged. |
| **Nothing wrong, or not about the study** | Nobody. The bot stays quiet. |
| **A general question** ("what is the Week 4 window?") | Nobody. The bot does not answer questions (yet). |

**One message, one alert.** If one site message has several problems, they come together in **one** chat post and **one** email, numbered 1, 2, 3. Immediate problems are the exception: they always go out on their own.

### What an alert looks like

```
Medium · DNL343 · G-708-002 · Site 01 (MGH)

3 problems found:
1. Baseline CSF was collected too early. (Medium, Item #19)
2. The study drug storage temperature was above the allowed limit. (Medium, Item #21)
3. The Week 2 phone visit was done late. (Low, Item #20)

What to do:
1. Record a deviation ...
2. Quarantine the affected supply ...
3. Record the visit deviation ...

──────────
Evidence for problem 1
Site wrote: baseline csf was 9/5 ... baseline + first dose were 9/15
Study rule: For the Baseline visit, CSF may be collected at any time within one week prior to ...
From: Regimen-Specific Appendix G, DNL343, V2.0, section 6.1 Baseline Visit
```

- **Study rule** is always the protocol's own words, found word for word in the document. If the bot cannot find the exact words, it shows no quote. It writes "exact words not found, please read this section: ..." and marks the problem **please check**.
- **Please check (not sure)** means the bot thinks it is a problem but is not certain. The line "Why not sure" says why.
- Each problem has an **item number** (#19). The **details** link opens a web page with everything the bot used.

### Replying to an alert: "cleared" and "disregard"

Only the CRA or the sponsor lead can do this. It works in chat and by email.

| You write | What happens |
|---|---|
| `cleared` (in the alert's chat thread, or as a reply to the alert email) | Every problem in that alert is marked resolved. |
| `cleared #20` | Only problem #20 is marked resolved. |
| `disregard` / `disregard #20` | The same, but the problem is dropped as not real (it no longer counts). |
| `cleared #20` posted anywhere in the study space | Works too, by the number. |

In chat the bot answers in the thread, for example "Done: #20 cleared, by John Huma." If it can't tell which problem you mean, it says how to name one.

### How the bot thinks (in 5 steps)

1. **Reads the message.** It works out which patient, what happened, and when. It decides if it is **done**, **planned**, or **old news**.
2. **Works out the dates.** It turns "9/15" and "wk2 done 10/6" into days from Baseline (Day 0) and compares them with each visit's window.
3. **Checks the rules.**
   - Dates, times and numbers are checked by plain **code** (139 checks made from the documents), so the same message always gets the same answer. Example: Week 2 must be Day 11 to 17.
   - Rules without a number ("consent before any study procedure") are judged by the **AI**, using short topic summaries of the documents (visit schedule, dosing, samples, eligibility, consent and others).
4. **Double-checks.** A second AI reads the original protocol section and must agree. The bot must also find the site's words in the message and the protocol's words in the document.
5. **Decides how urgent it is and who to tell** (see "Who gets what").

**What it will NOT do, by design:**

- **Safety:** no judging of side effects, deaths or hospital visits. It replies "please take it to the medical monitor".
- **Old news:** no alert on past events that were already dealt with.
- **Missing information:** no alert just because something wasn't mentioned.
- **Questions:** no answers to protocol questions.
- **Unknown study:** mail to the general address `bot+clinical@huma.ai` is never checked, because the bot never guesses the study.

**Key Regimen G limits (handy for writing tests).** Baseline is Day 0.

| Area | Rule (from the study documents) |
|---|---|
| Visit windows | Week 2 phone: Day 14 ± 3 · Week 4 clinic: Day 28 ± 7 · Week 8 clinic: Day 56 ± 7 · Week 12 phone: Day 84 ± 3 · Week 16 clinic: Day 112 ± 7 · Week 20 phone: Day 140 ± 3 · Week 24: Day 168 ± 7 |
| Samples | Baseline CSF within one week before the first dose · predose PK 0 to 2 hours before the dose · postdose PK 1 to 8 hours after the dose · samples labelled with a code, never the patient's name |
| Study drug | stored below 30 °C · one dose a day in the morning · temperature excursions in transit resolved before use |
| Medicines | strong CYP3A4/5 inducers (for example phenytoin) stopped at least 2 weeks before the first dose |
| Consent | Master Protocol consent signed before any screening procedure |

### Part 2: Test in chat or by email

**In chat (easiest).** Post a message in **DNL343 Study Updates**, as a site coordinator would. For example:

```
Hi team, update on G-708-002: baseline + first dose were 9/15. Baseline CSF was drawn 9/5. Wk2 phone call done 10/6. Wk4 visit done 10/13, all fine. Also our kit fridge read 32C this morning.
```

Within about a minute you should get one post with 3 problems (CSF too early, fridge too warm, Week 2 late). The Week 4 visit is inside its window, so it should not be flagged. The CRA gets the same alert by email.

**By email.** Send an email **to** the CRA (`johnernest978@gmail.com`) with the bot in **CC** (`bot+NCT05842941@huma.ai`). Any email account works as the sender.

**Tips:**

- Use a **new patient number** for each test (G-708-002, G-709-001 ...). Reusing one can make the bot treat it as a repeat.
- An urgent test: `G-709-001 came in today for screening. We did the blood draws and ECG first, then he signed the master consent form after.` It should come as **Immediate** in DNL343 URGENT too.
- See what the bot found: `./bin/cormon-cloud items --study NCT05842941`, then `./bin/cormon-cloud item 19 --drafts` for one item.

### Part 3: Test many messages at once (nothing is sent)

This runs test messages through a separate practice copy of the bot on the server. Nothing is posted or emailed, and the live data isn't touched.

```
./bin/cormon-cloud test demo/test-chat-g/chats --study NCT05842941
```

**Always add `--study NCT05842941`** for Regimen G. Without it, the test uses the old MAP study.

**Write your own tests.** Make a folder, for example `my-tests`, with one file per message:

- **A chat message:** a `.json` file, for example `my-tests/t1.json`:

  ```
  {"text": "G-801-001 wk2 call done 10/6, baseline was 9/15", "from": "crc", "expect": "visit_schedule"}
  ```

- **An email:** a `.txt` file:

  ```
  Subject: G-801-002 update
  From: crc
  Expect: sample_handling

  Baseline CSF drawn 9/1, first dose 9/15.
  ```

- **Many at once:** one `.csv` spreadsheet with the columns `subject`, `body`, `from` and `expect`.

**Expect** (optional) is what the bot *should* find, so it can be scored: `eligibility`, `visit_schedule`, `procedure_sequence`, `sample_handling`, `study_drug`, `consent`, `none` (nothing wrong) or `out_of_scope` (safety). Several are allowed, separated by commas.

Then run `./bin/cormon-cloud test my-tests --study NCT05842941`.

**Reading the result.** Each message shows **RIGHT** or **WRONG** and what the bot found. At the end:

- **Precision:** of the alerts raised, how many were real problems (fewer false alarms is higher).
- **Recall:** of the real problems, how many were caught (fewer misses is higher).
- **F1:** both combined. 1.00 is perfect.

The results are saved on your laptop in `results/<date-time>/results.csv`. To see why the bot decided something in the last test run:

```
./bin/cormon-cloud sandbox item 2 --drafts    # what it found, both quotes, what it would send
./bin/cormon-cloud sandbox message 4          # how it understood the message
./bin/cormon-cloud sandbox log --message 4    # every step it took, including ideas it rejected
```

Each test message uses the AI, so a run costs a few cents.

### Part 4: Other useful commands

| Command | What it does |
|---|---|
| `./bin/cormon-cloud status` | What the bot did recently |
| `./bin/cormon-cloud items --study NCT05842941` | Every problem found |
| `./bin/cormon-cloud log --item 19` | The full history of item 19 |
| `./bin/cormon-cloud digest --hours 24` | Preview the sponsor's daily summary |
| `./bin/cormon-cloud pause` / `resume` | Stop the bot from acting (it still collects messages) / start it again |
| `./bin/cormon-cloud logs` | The server's technical log |

Anything you do is recorded under your own name.

## How it works (technical)

1. **Capture.** Every message is stored whole (body, headers, attachments, arrival time, channel). Mail to `bot+NCT05842941@huma.ai` belongs to Regimen G. Chat messages belong to the study whose space they are in.
2. **Study model.** `cormon ingest` reads the documents into sections. `cormon/guide.py` writes short **topic briefs** (each line cites its section). `cormon/checks.py` holds the **code checks**: dates, times and numbers with their limits, each with a quote from the document.
3. **Reader** (`cormon/reader.py`, chosen by `checker:` in `study.yaml`). For each patient in the message: scope (which visits and sections apply), date notes worked out in code, an AI judge reads the topic briefs, the code checks run on facts taken from the message, duplicates are merged, and a second AI confirms each finding against the original section.
4. **Evidence.** The protocol quote is looked up sentence by sentence in the real documents. It is never a heading, another visit's line, or the bot's own summary, and a schedule table shows only the visit's own cell. Words that can't be proven are not shown, and the finding becomes "please check".
5. **Safety boundary.** Safety events are recorded as out of scope, routed nowhere, and the sender is told to take them to the medical monitor.
6. **Triage.** Tiers (low / medium / immediate) come from `config/triage.yaml`, a first-match rule list.
7. **Routing.** `config/routing.yaml` says where each tier goes. `config/templates.yaml` holds the message layouts. The problems of one message are sent as one alert (`routing.route_items`); immediate ones go alone. Replies ("cleared", "disregard", "#N") are matched to the alert by email headers or by chat thread.
8. **Record.** Every message, decision, send and operator action goes to an append-only log.

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
cormon suppress ITEM_ID --reason ...  / unsuppress / clear [--undo]
cormon override ITEM_ID --tier medium | --recipient PERSON_ID | --send-draft no | --counts no
cormon digest --hours 24 [--send]     digest for any window; the schedule is unaffected
cormon pause / resume                 capture continues while paused; nothing is classified or sent
cormon config show|set|edit|history   routing rules, thresholds, templates (changes recorded with who/when)
cormon note add --scope message|person|study|global --target X --kind fact|instruction "text"
cormon log [--item ID] [--message ID] the audit log
cormon rules [--pattern eligibility]  the study model
cormon rules --report                 what was kept, dropped or merged and why; number rules; contradictions
cormon rules --checklist              every inclusion/exclusion criterion and lab-manual step, with its rule or why none
cormon rules-suggest                  AI suggests keep or remove for each rule, with a reason (turns nothing off)
cormon export-rules FOLDER            Word + Excel of the rules; the Excel has "AI suggests", "Why", "Team decision"
cormon rules-decide SHEET.xlsx        apply the "Team decision" column (keep / remove); decisions survive a rebuild
cormon rules-decide --rule ID --decision keep|remove [--note "..."]   decide one rule
cormon reset --yes                    archive the database, start fresh (study model kept)
```

Use `--as NAME` (or `CORMON_OPERATOR`) to set the identity recorded for an action. Operator and CRA values are marked as such and are never overwritten by a later automatic pass. An override that would leave an item inconsistent is refused and explained; `--force` applies it anyway and records that.

## Going live

### Gmail with the app password (simplest; same as the scheduler)

1. `gcloud auth login` as someone who can read the `kol-gmail-bot-app-password` secret in `optical-depth-506808-t0` (or set `CORMON_GMAIL_APP_PASSWORD` in `.env`).
2. `cormon auth imap` checks the login.
3. Add `bot+NCT04098666@huma.ai` under Gmail → Settings → Accounts → **Send mail as** (signed in as bot@huma.ai).
4. `cormon config set settings channels.email imap`, then `cormon run`.

The inbox is shared with the scheduler, so only mail sent to a `bot+<study>` address or to the general address `bot+clinical@huma.ai` is read, and nothing in the inbox is marked or moved. Mail that arrived before the first run is ignored.

### Gmail API (alternative)

1. In a GCP project on the huma.ai Workspace, enable the **Gmail API**. Create an OAuth client of type **Desktop app** (consent screen: *Internal*) and save the JSON as `secrets/gmail_client.json`.
2. `cormon auth gmail` and sign in as bot@huma.ai. The token is saved to `secrets/gmail_token.json`.
3. In Gmail (as the bot) go to Settings → Accounts → **Send mail as** and add `bot+NCT04098666@huma.ai` as an alias, so replies leave from the study address. `cormon gmail send-as` lists what is configured.
4. Set `channels.email: gmail` in `config/settings.yaml`.

### Google Chat

The bot posts and reads as bot@huma.ai. The Chat API and People API are already on in project optical-depth-506808-t0.

1. In Google Chat (as bot@huma.ai or anyone), create a space, for example **MAP study alerts**. Add bot@huma.ai and the people who should see alerts.
2. In the Cloud console, open **Google Chat API → Configuration**. Give the app a name (e.g. "Study monitor"), an avatar URL and a description. Leave **interactive features** off. Save.
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

**Updating the cloud with new code.** Run the tests first (`python -m pytest -q tests/`), then `./deploy/create_vm.sh`. It copies the code, keeps the server's database and messages, sets the server's own web address in its settings, and restarts the bot. Check afterwards with `./bin/cormon-cloud status` and `./bin/cormon-cloud logs`.

- Only use `SYNC_MODEL=1 ./deploy/create_vm.sh` after rebuilding and testing the study model on the laptop. It replaces the server's rules with the laptop's.
- When copying single files by hand, make the archive with `COPYFILE_DISABLE=1 tar --no-xattrs ...`. Otherwise macOS adds hidden `._` files, which stop the bot reading its settings.

What this sets up:

- **Server:** an always-on `e2-micro` running Ubuntu 24.04 in `us-east1-b`, Google's smallest machine and within the free tier. The bot runs as a systemd service.
- **Gmail password:** the server reads it from Secret Manager with its own service account (`cormon-vm`), which may read that one secret and nothing else. No password is copied onto the server.
- **Item pages:** served over HTTPS by Caddy at `https://<ip-with-dashes>.sslip.io`. The app itself only listens on localhost.
- **SSH:** open only to Google's IAP range, and people log in with their own Google account (OS Login).
- **First install:** the server starts with an empty database containing the laptop's study model. Updates keep the server's data.

## Switching to Peter's study

1. Create `studies/<study-id>/` with a `docs/` folder holding the protocol, lab manual and consent form.
2. Copy `study.yaml` and edit the people, roles, sites, problem classes and Teams channel IDs.
3. Run `cormon ingest --study <study-id>`. Mail to `bot+<address_tag>@huma.ai` is then routed to it.

## Known limits of this build

- The bot does not answer protocol questions yet.
- The same problem can sometimes be alerted twice; some headlines are worded loosely; some date problems arrive as "please check" when they are clear.
- The web address is a test one (`sslip.io`); a proper Huma address needs one DNS entry.
- The MAP lab manual is a placeholder; real lab manuals will need their own check.
- There is no approval queue: drafts are sent automatically (open question 1). `cormon pause` and `--send-draft no` are the brakes.
- Item detail pages are protected by an unguessable link (over HTTPS on the cloud copy), not by a login.
- Later: when mail reaches the general address (bot+clinical@huma.ai), reply once saying it is not linked to a study and naming the study addresses. It must never guess the study.
- The arrears pattern pass, a structured study data model, CTMS write-back and a correction UI are deferred, as the requirements say.

# Correspondence Monitor (board demo build)

A headless monitor for clinical trial correspondence. It reads the email and Teams messages it is copied on, checks them against the study's own documents, and routes what it finds: a Teams study channel, an alert channel, the CRA's inbox, the sponsor's daily digest, and in the most urgent case a short hedged notice to the site. It never reviews safety information.

Demo study: **MAP, Metformin in Alzheimer's Dementia Prevention (NCT04098666)**. The protocol and consent form come from ClinicalTrials.gov. The lab manual (`studies/NCT04098666/docs/lab_manual.md`) is a **placeholder written for the demo**, because no public one exists.

## Testing guide (for Eliot and Peter)

**How it's set up:** the bot runs on a small Google Cloud server that is always on. You don't install the bot. You install a small Google tool, and then you type short commands in Terminal on your laptop to talk to the bot. You sign in with your own huma.ai Google account. The connection is secure, and there's no VPN or extra password.

Only one copy of the bot should read the email, so **don't start the bot on your own laptop.** Always use `./bin/cormon-cloud`.

### Part 1: One-time setup (about 5 minutes)

**Step 1. Install the Google Cloud tool.** Open the Terminal app and type:

```
brew install --cask google-cloud-sdk
```

No Homebrew? Download it from <https://cloud.google.com/sdk/docs/install> instead.

**Step 2. Sign in with your huma.ai Google account:**

```
gcloud auth login
```

A browser window opens. Choose your huma.ai account and click **Allow**.

**Step 3. Download the code:**

```
git clone https://github.com/humahq/correspondence-monitor.git
cd correspondence-monitor
```

**Step 4. Check that it works:**

```
./bin/cormon-cloud status
```

The first time, it asks to create a key. Just press **Enter** when asked. If you see the bot's status, you're ready.

You'll run all the commands below from inside the `correspondence-monitor` folder.

### Who's who (and which email is which)

The bot sends alerts to people by their **role** in the study. Right now the roles use John's test addresses:

| Role | What they do | Test email now |
|---|---|---|
| **Coordinator (CRC)** | Works at the hospital site. Writes the day-to-day emails about patients. | `johnrohit94@gmail.com` |
| **CRA** | The sponsor's monitor for the site. Gets the bot's alerts and decides what to do. | `johnernest978@gmail.com` |
| **Sponsor lead (ClinOps)** | Runs the study for the sponsor. Copied on alerts and gets the daily summary. | `john@huma.ai` |
| **Medical monitor** | The doctor who handles safety. The bot never alerts them; it tells people to contact them. | none |
| **The bot** | Reads every email it is copied on. | `bot+NCT04098666@huma.ai` |

**The normal flow:** the coordinator emails the CRA as usual and puts the bot in **CC**. The bot reads the email, checks it against the study rules, and if something is wrong it emails an alert. The coordinator doesn't have to do anything different.

**Who gets what:**

| What the bot finds | Who is emailed |
|---|---|
| **Medium** (a problem that already happened) | The CRA, with the sponsor lead in CC. It also goes in the sponsor lead's daily summary. |
| **Immediate** (a problem planned within 48 hours, or a consent problem) | The CRA and the sponsor lead, each in a separate email. If a coordinator wrote the email, they get a short careful note too. |
| **Low** (for example, a visit a little out of its window) | No email. It goes on a to-do list and in the daily summary. |
| **Safety** (side effects, falls, hospital visits) | Only the person who wrote the email gets a reply: "please contact the medical monitor". |
| **Nothing wrong, or not about the study** | Nobody. The bot stays quiet. |

Teams messages are shown in the bot's records only (simulated); they are not posted to a real Teams channel yet.

### Before you test: how the bot thinks

**What it does with each email, in 4 steps:**

1. **Reads it.** It works out what happened, to which patient, and when. It also decides whether this is **new** (happening now), **planned** (about to happen), or **old news** (already happened and been dealt with, or forwarded from weeks ago).
2. **Checks it against the study rules.** The bot turned the protocol, lab manual and consent form into **124 short rules** (see them with `./bin/cormon-cloud rules`). It only looks for 5 kinds of problem: **eligibility, visit schedule, procedure order, sample handling and consent.** Anything else is ignored on purpose.
   - Rules with a number ("eGFR must be 45 or more") are checked by plain code, so the same email always gets the same answer.
   - Rules without a number ("consent must be signed before any study procedure") are judged by the AI.
   - **Second reading:** it also reads the whole email once against the full rule list, to catch anything the first pass missed.
   - **Memory:** when an email says a patient was enrolled or randomized, it also checks that patient's numbers from earlier emails (any thread, last 60 days). The alert says which earlier email the number came from.
3. **Double-checks.** Before raising anything, it must find the exact words in the email **and** the exact sentence in the protocol. If it can't find both, it stays quiet. That's why every alert quotes both.
   - **Not sure?** If the protocol leaves the call to the study doctor, or the email is missing one fact (for example years of education), it doesn't stay quiet. It sends the alert to the CRA marked **"Please check (not sure)"** with the reason.
4. **Decides how urgent it is, and who to tell:**
   - **Immediate:** the problem is planned within 48 hours (so it can still be stopped), or it's a consent problem. The CRA and the sponsor are emailed, and the site gets a short careful note.
   - **Medium:** a problem that has already happened. The CRA is emailed (the sponsor lead is copied), and it goes in the daily summary.
   - **Low:** for example, a visit outside its window. It goes on a to-do list and in the daily summary.

**What it will NOT do, by design:**

- **Safety:** it doesn't judge safety (side effects, hospital visits, falls). It replies "please contact the medical monitor" and does nothing else.
- **Old news:** it doesn't alert on past events or old forwarded emails.
- **Vague or missing information:** it doesn't alert on missing information ("we didn't mention the fasting time" isn't a problem by itself).
- **Repeats:** it doesn't send a second alert for the same problem in the same email thread.
- **Unknown study:** it doesn't check emails sent to the general address `bot+clinical@huma.ai`, because it never guesses the study.

**Key limits it checks (handy for writing tests):**

| Area | Rule (from the study documents) |
|---|---|
| Eligibility | age 55–90 · eGFR 45 or more · BMI 20 or more · HbA1c below 6.5% · blood pressure below 160/95 · MMSE 22 or more (20 with 8 years of school or less) · CDR exactly 0.5 · GDS below 9 · fluent in English or Spanish · no known diabetes |
| Sample handling | fasting 8 hours or more · spin the tube within 60 minutes · freeze within 2 hours · no more than 24 hours at −20 °C · ship Monday to Wednesday only · hemolyzed samples not acceptable · temperature log in the box · email the lab before shipping |
| Visit schedule | screening within 28 days · 6-month visit ±30 days · monthly call ±7 days |
| Procedure order | brain MRI before the study drug starts · screening labs, vitals and ECG before randomization |
| Consent | consent signed before any study procedure |

The lab manual is a practice version written for this demo, so the sample-handling rules are examples, not a real lab's rules.

**Test ideas** (mix easy and tricky):

- **Clear problem:** "Randomized MAP-0301 today, eGFR 41." It should alert (eligibility).
- **Within limits:** "eGFR 62, BMI 24, all fine." It should stay quiet.
- **Planned:** "We'll start study drug tomorrow; the MRI is next week." It should give an Immediate alert.
- **Old news:** "Back in July, MAP-0201 was screened out for low eGFR." It should stay quiet.
- **Safety only:** "MAP-0305 fell and broke her wrist." It should refuse and point to the medical monitor.
- **Messy writing:** typos, short forms ("pt", "rand'd", "GFR 4l"), missing units.
- **Two patients in one email,** one fine and one not. It should flag only the right patient.
- **Two emails together:** first "MAP-0410 screened, eGFR 41", then a few minutes later a new email "Randomized MAP-0410 today." The second one should alert, quoting the first.
- **Vague:** "Labs a bit off, enrolled anyway." This is a judgment call, so tell us what you'd expect.
- **Not about the study at all:** "Office closed Monday." It should stay quiet.

### Part 2: Test with a real email

**Step 1.** Send an email like a coordinator would: **To** the CRA, with the bot in **CC**. (Any email account works as the sender.) For example:

> **To:** `johnernest978@gmail.com` (the CRA)
> **CC:** `bot+NCT04098666@huma.ai`
> **Subject:** MAP-0142 randomized today
> **Body:** We randomized MAP-0142 this morning. Her eGFR at screening was 41.

Use a new patient number (MAP-0142, MAP-0143 …) for each test. The bot doesn't alert twice on the same problem in the same thread.

**Step 2.** Wait about one minute. The bot should notice that an eGFR of 41 is too low (the study needs 45 or more). It is a Medium problem, so it emails an alert to the CRA (`johnernest978@gmail.com`) with the sponsor lead (`john@huma.ai`) in CC. The alert quotes the email and the protocol page.

**Step 3.** See what the bot found. Each problem gets an item number:

```
./bin/cormon-cloud items
```

**Step 4.** See the full details of one item (use the number from Step 3): what it quoted from the email and the protocol, its reasoning, and every email it sent:

```
./bin/cormon-cloud item 3 --drafts
```

Each item also has a web page link (shown as "Detail page") that you can open in a browser.

**Step 5 (optional).** Reply to the bot's alert email with the word **disregard**. The bot marks the alert as dropped.

### Part 3: Test many emails at once (no emails are sent)

This runs test emails through a separate practice copy of the bot. Nothing is emailed to anyone, and the real data isn't touched.

**Step 1. Try 3 ready-made examples:**

```
./bin/cormon-cloud test demo/test-quick
```

**Step 2. Try 41 practice emails** (about 4 minutes):

```
./bin/cormon-cloud test demo/test-emails
```

**Step 3. Write your own test emails.** Make a folder, for example `my-emails`, and put one text file per email in it. Example file `my-emails/test1.txt`:

```
Subject: MAP-0301 randomized
From: crc
Expect: eligibility

We randomized MAP-0301 today. eGFR was 41.
```

What the top lines mean (all of them are optional):

- **Subject:** the email's subject line.
- **From:** who sent it: `crc` (the site coordinator) or `cra` (the monitor).
- **Expect:** what the bot *should* find, so it can be scored right or wrong:

| Write | When the email is about |
|---|---|
| `eligibility` | a patient who doesn't meet the entry criteria |
| `visit_schedule` | a visit outside its allowed window |
| `procedure_sequence` | steps done in the wrong order |
| `sample_handling` | a lab sample handled wrongly |
| `consent` | a consent problem |
| `none` | nothing wrong; the bot should stay quiet |
| `out_of_scope` | safety only; the bot should refuse and point to the medical monitor |

Leave out **Expect** if you just want to see what the bot does.

Then run:

```
./bin/cormon-cloud test my-emails
```

**Many emails?** Instead of one file each, use a single spreadsheet saved as `.csv` with the columns `subject`, `body`, `from` and `expect`. See `demo/test-emails/practice-41.csv` for an example.

**Step 4. Read the result.** For each email you'll see **RIGHT** or **WRONG**, and what the bot found. At the end there's a score:

- **Precision:** of the alerts the bot raised, how many were real problems (fewer false alarms means higher).
- **Recall:** of the real problems, how many the bot caught (fewer misses means higher).
- **F1:** both combined into one number. 1.00 is perfect.

The results are also saved as a spreadsheet on your laptop: `results/<date-time>/results.csv`. It includes the bot's reasoning for every email.

**Step 5. Find out why the bot decided something.** Each line of the result shows a message number (`msg 4`) and item numbers (`#2`). Look inside the test run with `sandbox`:

```
./bin/cormon-cloud sandbox item 2 --drafts    # what it found, both quotes, its reasoning, what it would send
./bin/cormon-cloud sandbox message 4          # how it understood the email (useful when it found nothing)
./bin/cormon-cloud sandbox log --message 4    # every step it took, including ideas it rejected and why
```

The `sandbox` view shows the most recent test run only. Each new run starts fresh.

Each test email uses the AI, so a run costs a few cents.

### Part 4: Other useful commands

| Command | What it does |
|---|---|
| `./bin/cormon-cloud rules` | The 124 rules the bot made from the study documents |
| `./bin/cormon-cloud status` | What the bot did recently |
| `./bin/cormon-cloud log --item 1` | The full history of item 1 |
| `./bin/cormon-cloud digest --hours 24` | Preview the sponsor's daily summary |
| `./bin/cormon-cloud pause` | Stop the bot from acting (it still collects email) |
| `./bin/cormon-cloud resume` | Start it again |
| `./bin/cormon-cloud logs` | The server's technical log |

Anything you do is recorded under your own name.

### For John: updating the cloud copy

- **Updating the code:** `./deploy/create_vm.sh` copies the current code to the server and restarts the bot. The live data on the server is kept.
- **Updating the rules:** rebuild them in the practice copy (`CORMON_HOME=practice cormon ingest --force`), read `cormon rules --report`, run the test sets, then `cormon import-model practice/data/cormon.db --reason "..."` on the laptop and `SYNC_MODEL=1 ./deploy/create_vm.sh`. Messages and items on the server are kept; the swap is recorded in the log.
- **Settings:** changes made on the server with `cormon config set` are overwritten by the repo's `config/` on the next update, so make permanent changes in the repo.
- **Giving someone access:** `./deploy/grant_access.sh name@huma.ai` (Eliot and Peter already have access).

## How it works

1. **Capture.** Every message is stored whole: body, headers, attachments, arrival time, and how the monitor saw it (to, cc, bcc, channel, direct). The study comes from the address: `bot+NCT04098666@huma.ai` belongs to MAP. Mail to the bare `bot@huma.ai` is logged as a general message and never matched against a study.
2. **Study model.** `cormon ingest` turns each document into atomic rules ("Exclude if eGFR is below 45 mL/min"). Each rule keeps a verbatim quote from the document. A rule whose quote can't be found in the document is rejected as a defect. Rules are rebuilt only when a document changes.
3. **Event model.** Each message becomes structured events: what happened, to whom, when, whether it is new, planned, or historical (already known), plus the facts stated.
4. **Matching.** An event is checked only against the problem classes agreed for the study (eligibility, visit schedule, procedure order, sample handling, consent).
   - Number thresholds are checked in code, so the same value always gives the same answer.
   - Other rules are checked by the AI, one atomic rule at a time.
   - Every finding is then confirmed against the original document passage, and both quotes (email and document) must be found verbatim before anything is raised.
5. **Safety boundary.** An event about participant safety (or a safety signal that matches no study rule) is recorded as out of scope. It is routed nowhere, and the sender is told to take it to the medical monitor.
6. **Triage.** Tiers (low / medium / immediate) come from `config/triage.yaml`, a first-match rule list you can read and edit. One deviation discussed over several replies is one item.
7. **Routing and drafts.** `config/routing.yaml` says where each tier goes. `config/templates.yaml` holds templates chosen by topic × recipient × channel × severity. Everything is sent from the study's own alias, never from a customer address.
8. **Record.** Every message, classification, routing decision, send and operator action goes to an append-only log (database triggers block updates and deletes).

## Quick start (local, no accounts needed)

```bash
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
echo "OPENAI_API_KEY=sk-..." > .env

cormon init
cormon ingest                 # builds the study model (about 2-3 minutes the first time)
python demo/rehearse.py       # runs every demo scenario and checks the result
cormon items                  # what it found
cormon item 2 --drafts        # one item: evidence, every drafted message, history
cormon digest --hours 24      # preview the sponsor digest
```

With `channels.email: sim` and `channels.teams: sim` (the default), mail is read from `data/sim/inbox/` and "sent" to `data/sim/outbox/`. Teams posts are appended to `data/sim/teams/*.md`.

## Demo scenarios (`demo/scenarios/`)

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

### Microsoft Teams

A personal Microsoft account cannot do this. You need a work tenant, for example a Microsoft 365 Business Basic trial or the Microsoft 365 Developer Program sandbox.

1. Create the bot as a normal user in our tenant (e.g. the study bot account). Create a team with a **Study** channel and an **Alerts** channel, add the bot user, and invite the customer's people as guests.
2. In Entra ID, register an app. Under Authentication, turn on **Allow public client flows**. Add the delegated Graph permissions `ChannelMessage.Send`, `ChannelMessage.Read.All`, `Chat.ReadWrite`, `Team.ReadBasic.All`, `Channel.ReadBasic.All` and `User.ReadBasic.All`, then **grant admin consent**.
3. Put the tenant ID and client ID under `teams:` in `config/settings.yaml` and set `channels.teams: graph`.
4. `cormon auth teams` (device code; sign in as the bot user), then `cormon teams-channels` to get the IDs for the `teams:` section of `studies/NCT04098666/study.yaml`.

### GCP

```bash
./deploy/create_vm.sh                          # create or update (PROJECT, ZONE, NAME can be overridden)
./deploy/grant_access.sh someone@huma.ai       # let a person use ./bin/cormon-cloud
```

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

- The lab manual is a placeholder; real lab manuals will need their own check.
- There is no approval queue: drafts are sent automatically (open question 1). `cormon pause` and `--send-draft no` are the brakes.
- Item detail pages are protected by an unguessable link (over HTTPS on the cloud copy), not by a login.
- Later: when mail reaches the general address (bot+clinical@huma.ai), reply once saying it is not linked to a study and naming the study addresses. It must never guess the study.
- The arrears pattern pass, a structured study data model, CTMS write-back and a correction UI are deferred, as the requirements say.

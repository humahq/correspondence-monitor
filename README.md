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

### Part 2: Test with a real email

**Step 1.** From any email account, send an email and **CC `bot+NCT04098666@huma.ai`**. For example:

> **Subject:** MAP-0142 randomized today
> **Body:** We randomized MAP-0142 this morning. Her eGFR at screening was 41.

**Step 2.** Wait about one minute. The bot should notice that an eGFR of 41 is too low (the study needs 45 or more) and send an alert email to the study team.

**Step 3.** See what the bot found:

```
./bin/cormon-cloud items
```

**Step 4.** See the full details of item 1: what it quoted from the email and the protocol, its reasoning, and every email it sent:

```
./bin/cormon-cloud item 1 --drafts
```

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

The results are also saved as a spreadsheet on your laptop: `results/<date-time>/results.csv`.

Each test email uses the AI, so a run costs a few cents.

### Part 4: Other useful commands

| Command | What it does |
|---|---|
| `./bin/cormon-cloud rules` | The 111 rules the bot made from the study documents |
| `./bin/cormon-cloud status` | What the bot did recently |
| `./bin/cormon-cloud log --item 1` | The full history of item 1 |
| `./bin/cormon-cloud digest --hours 24` | Preview the sponsor's daily summary |
| `./bin/cormon-cloud pause` | Stop the bot from acting (it still collects email) |
| `./bin/cormon-cloud resume` | Start it again |
| `./bin/cormon-cloud logs` | The server's technical log |

Anything you do is recorded under your own name.

### For John: updating the cloud copy

- **Updating the code:** `./deploy/create_vm.sh` copies the current code to the server and restarts the bot. The live data on the server is kept.
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

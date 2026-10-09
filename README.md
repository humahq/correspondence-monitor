# Correspondence Monitor (board demo build)

A headless monitor for clinical trial correspondence. It reads the email and Teams messages it is copied on, checks them against the study's own documents, and routes what it finds: a Teams study channel, an alert channel, the CRA's inbox, the sponsor's daily digest, and in the most urgent case a short hedged notice to the site. It never reviews safety information.

Demo study: **MAP, Metformin in Alzheimer's Dementia Prevention (NCT04098666)**. The protocol and consent form come from ClinicalTrials.gov. The lab manual (`studies/NCT04098666/docs/lab_manual.md`) is a **placeholder written for the demo**, because no public one exists.

## Testing guide (for Eliot and Peter)

The bot runs on a small always-on server in Google Cloud (`cormon-demo`, project `optical-depth-506808-t0`). Only one copy reads the inbox, so **don't run `cormon run` on your laptop**. Use the cloud copy instead. You control it from your laptop with `./bin/cormon-cloud`, using your own Google account. The connection is encrypted through Google's IAP proxy, so no VPN or password is needed.

### One-time setup (5 minutes)

1. Install the Google Cloud CLI: <https://cloud.google.com/sdk/docs/install> (on a Mac: `brew install --cask google-cloud-sdk`).
2. Sign in with your huma.ai account: `gcloud auth login`.
3. Get the code: `git clone <repo URL>` and `cd` into it.
4. Check it works: `./bin/cormon-cloud status`. The first time, gcloud creates an SSH key for you; just press Enter.

Access is granted per person by `./deploy/grant_access.sh name@huma.ai` (Eliot and Peter already have it).

### Test 1: real email

Send an email and **CC `bot+NCT04098666@huma.ai`**, for example:

> Subject: MAP-0142 randomized today
> We randomized MAP-0142 this morning. Her eGFR at screening was 41.

Within about a minute the bot finds the problem (the protocol excludes eGFR below 45) and emails the people set in `studies/NCT04098666/study.yaml`. Then look at what it did:

```bash
./bin/cormon-cloud items              # what it found
./bin/cormon-cloud item 1 --drafts    # quotes, reasoning, every message it sent, full history
./bin/cormon-cloud status             # recent activity
```

To tell the bot to drop an alert, reply to its email with "disregard". The bot reads replies too.

### Test 2: many emails at once, from a folder (nothing is sent)

```bash
./bin/cormon-cloud test demo/test-quick       # 3 small examples
./bin/cormon-cloud test demo/test-emails      # the 41 practice emails (about 4 minutes)
./bin/cormon-cloud test my-emails/            # your own folder
```

Each run uses a separate sandbox copy: no email is sent and the live data isn't touched. It prints what the bot found for each message and a score, and saves `results/<time>/results.csv` on your laptop.

A test folder can hold any mix of:

- **`.txt`**: one email per file. The header lines are optional:
  ```
  Subject: MAP-0301 randomized
  From: crc
  Expect: eligibility
  Participant: MAP-0301

  We randomized MAP-0301 today. eGFR was 41.
  ```
  - `From`: `crc` (site, the default), `cra` (monitor), or an email address.
  - `Expect`: what the bot should find, used for the score. Use `eligibility`, `visit_schedule`, `procedure_sequence`, `sample_handling` or `consent` (several separated by commas), `none` for nothing to flag, or `out_of_scope` for safety-only messages. Leave it out to just see what the bot does.
- **`.csv`**: many emails, one per row, with columns `id, subject, body, from, expect, participant`. Only `body` is required. See `demo/test-emails/practice-41.csv`.
- **`.eml`**: real saved emails. The study address is added automatically.

How to read the score:

- **Precision:** of the alerts the bot raised, how many were real problems.
- **Recall:** of the real problems, how many the bot caught.
- **F1:** both combined into one number.

### Other useful commands

```bash
./bin/cormon-cloud rules                  # the 111 rules the bot built from the study documents
./bin/cormon-cloud log --item 1           # the audit trail for one item
./bin/cormon-cloud digest --hours 24      # preview the sponsor's daily summary (add --send to send it)
./bin/cormon-cloud pause / resume         # stop or restart processing (email is still captured)
./bin/cormon-cloud logs                   # the server's own log
./bin/cormon-cloud shell                  # a terminal on the server
```

Every command in "Operator commands" below works the same way through `./bin/cormon-cloud`. Your actions are recorded under your own login name.

### Updating the cloud copy (John)

`./deploy/create_vm.sh` copies the current code to the server and restarts it. The live data on the server is kept. Settings changed on the server with `cormon config set` are replaced by the laptop's `config/` on the next update, so make permanent changes in the repo.

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

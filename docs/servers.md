# The three bots, and how each is kept up to date

| | Server 1: live | Server 2: test | Mattermost demo |
|---|---|---|---|
| Where | `cormon-demo` (Google Cloud) | `cormon-edge` (Google Cloud) | John's laptop (this folder + `~/Documents/cormon-chat`) |
| Bot account | bot@huma.ai | bot_test@huma.ai | Mira Belle (local Mattermost) |
| Chat | DNL343 / MAP rooms in Google Chat | Test Study Room, Test Urgent | local Mattermost teams |
| New features | only those John has said yes to | every one, always (`CORMON_TEST_FEATURES=all`) | every one, always |
| Patient memory (Hindsight) | off | on, on the server itself | on, in the laptop's Hindsight |
| Updated | **only when John says**; John runs the release | **after every merge to main**, without asking | **after every merge to main**, without asking |

## After every merge to main (servers 2 and Mattermost: no need to ask John)

1. Wait for the gate on main to pass (`gh run list --branch main --limit 1`).
2. Server 2: `echo yes | CORMON_VM=cormon-edge ./deploy/release.sh`. It checks the gate, the packages and the
   study model, backs up, restarts and checks health. If it stops, fix the cause and tell John in one line.
3. Mattermost: `git checkout main && git pull` here, and `git pull` in `~/Documents/cormon-chat`. If the demo is
   running, it picks up the new code only when restarted: restart it, unless a demo may be on (ask first on demo
   days, and never during Friday's or Monday's demo).
4. A new feature switch needs nothing more: server 2 and Mattermost turn every switch on by themselves.
5. Write the new version in the table below.

## Server 1 (live): only when John says

John runs `./deploy/release.sh` himself. A feature goes on there only when John says "yes, put this feature in the
live bot" (the steps in `docs/features.md`). Eliot: no live change before Friday's demo unless an important bug fix.

## Versions in use

| Date | Server 1 (live) | Server 2 (test) | Mattermost |
|---|---|---|---|
| 12 Nov 2026 | bc195a1 | bc195a1 | main (4b6714b: same bot code as bc195a1) |

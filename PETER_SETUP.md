# Install, update and test the bot (for Peter and Eliot)

This guide shows how to put the bot on your own laptop, keep it up to date, and test it. How the bot works is
in [README.md](README.md).

## Which bot to use

There are three copies of the bot ([docs/servers.md](docs/servers.md)):

| Bot | Where you talk to it | Use it for |
|---|---|---|
| **Laptop demo** (Mira Belle) | Mattermost on your own laptop, http://localhost:8065 | Trying anything, as often as you like. Nobody else sees it. |
| **Server 2** (Test Bot, bot_test@huma.ai) | Google Chat: **Test Study Room** and **Test Urgent** | Testing the newest version the way real users will meet it. |
| **Server 1** (the live bot, bot@huma.ai) | Google Chat: the DNL343 rooms, and email | Real study messages only. Please don't test here. |

The laptop demo and server 2 always run the newest code, with every new feature switched on. John updates
server 2 after every change. The live bot only changes when John says so.

---

## Part 1: Install the laptop demo (once, about 20 minutes)

You need a Mac (or a Linux laptop) with 16 GB of memory and 20 GB of free disk, and an OpenAI key
(https://platform.openai.com/api-keys).

**On a Mac:** open **Terminal** (press Cmd+Space, type `Terminal`, press Return). Paste this line and press Return:

```
curl -fsSL https://raw.githubusercontent.com/humahq/cormon-chat/main/Install%20OpenBook.command | bash
```

It does everything by itself. It installs the tools, downloads the chat server (`cormon-chat`) and the bot
(`correspondence-monitor`) into a folder named `OpenBook`, and starts them. It asks only for:

- the folder to put `OpenBook` in (press Return for your home folder);
- your Mac password, if Homebrew is not installed yet (nothing shows as you type);
- your OpenAI key (nothing shows as you paste). It checks the key before saving it.

At the end, your browser opens the demo, and the Terminal shows the sign-in names and the password:

```
  Sign in at http://localhost:8065 as crc, pi, cra, sponsor, medmonitor, eliot, peter or john
  Password:  Demo-3fa91c0b2e7d!
```

The password is made on your laptop, so yours is different. **Keep that Terminal window open.** Closing it
stops the bot.

**On Linux**, and for anything that goes wrong during the install, follow the
[cormon-chat README](https://github.com/humahq/cormon-chat#readme).

### Start it again later

After a restart, double-click **Start demo.command** in the `OpenBook/cormon-chat` folder. If macOS says it can't
open the file, right-click it and choose **Open**. It shows the password again each time.

---

## Part 2: Get the newest version

John merges changes into the `main` branch on GitHub. To get them on your laptop, **paste the same install line
again** (Part 1). It keeps your data, skips what is already installed, and runs `git pull` for both
`cormon-chat` and the bot. Close the demo's Terminal window first, so it starts again with the new code.

If you prefer to do it by hand:

```
cd ~/OpenBook/correspondence-monitor && git pull
cd ~/OpenBook/cormon-chat && git pull
```

Then close the demo's Terminal window and double-click **Start demo.command** again. The bot is rebuilt with the
new code (a few minutes the first time).

The installer leaves a folder alone, and says why, if you have changed files in it or it is not on `main`. To
check, run `git status` in that folder.

**Which version am I running?** In the `correspondence-monitor` folder, `git log --oneline -1` shows the latest
change. It should match the top of https://github.com/humahq/correspondence-monitor/commits/main.

---

## Part 3: Test it

### In the laptop demo (Mattermost)

Sign in as `crc` (the site coordinator) and open **Site 01 (MGH)**. The pinned welcome guide has messages to try.

- **Normal messages:** the bot reads every message and stays quiet unless something is wrong. Then it posts an
  alert.
- **Questions:** tag the bot, `@mira`, and it answers in the thread.
- **1-on-1:** message `mira` directly. It always replies there.

To test as different people, sign in as `cra`, `pi` or `sponsor` in another browser, or in a private window.

### In Google Chat (server 2)

Post in **Test Study Room**, as a site coordinator would. Tag **@Test Bot** to ask it something. Alerts for
urgent problems also appear in **Test Urgent**.

### Good things to test

**Problems in a site message** (no tag needed):

```
Hi team, update on G-913-001: baseline + first dose were 9/15. Baseline CSF was drawn 9/5. Wk2 phone call done 10/6. Also our kit fridge read 32C this morning.
```

You should get one alert with 3 problems: the CSF was drawn too early, the fridge was too warm, and the Week 2
call was late.

**Rule questions** (tag the bot):

- "What is the window for the Week 8 visit?"
- "Is St John's wort allowed in this study?"
- "Who do we report an SAE to, and how fast?"

**Patient memory.** Post facts in one message, then ask about them later, from another person or another
thread:

1. As `crc`: `G-708-401 started St John's wort capsules for low mood two weeks ago.`
2. As `crc`: `G-718-405 uses a salbutamol inhaler when needed.`
3. As `cra`, tag the bot: `Is G-708-401 on anything we should sort out before her next dose?`
4. In the same thread: `What about G-718-405?`

Also try:

- a fact that is later corrected ("the fall was on 5 Nov, not 3 Nov"), then ask for the date;
- a short code that fits two patients ("what is 131 taking?" when G-708-131 and G-718-131 both exist). The bot
  should ask which one;
- a medicine that was stopped, or was only asked about and never started;
- a vague question: "anything we should know about G-708-370?";
- "who told us about it, and when?"

**What a good answer looks like:**

- It names who reported a fact, and on what date.
- It never mixes up two patients.
- It never gives another patient's medicine.
- A rule comes with the protocol's own words: click **Details and the protocol's words**.
- If the bot can't back an answer, it says "I couldn't find this in the study documents, so I won't guess."
  That is better than a wrong answer.

**Mark an alert as done** (as `cra` or `sponsor`): reply in the alert's thread with `cleared`, `cleared #20` or
`disregard #20`.

### Tips

- Use a **new patient number** for each test (G-913-001, G-914-001 ...). The bot remembers every patient, so
  reusing a number mixes your tests.
- Answers take up to a minute.
- **Found a problem?** Send John a screenshot, the time, and which bot (laptop or server 2).

---

## Part 4 (optional): Run many test messages at once

This runs a folder of test messages through a practice copy of the bot on server 2. Nothing is posted or
emailed, and no real data is touched. Each message costs a few cents of AI use.

### One-time setup

1. **Ask John for access.** Send him your huma.ai address (for the server) and your GitHub username (for the
   code). Accept GitHub's invitation email.
2. **Install the Google Cloud tool and sign in:**
   ```
   curl https://sdk.cloud.google.com | bash
   ```
   Press Return at every question. Then close the Terminal, open a new one, and run:
   ```
   gcloud auth login
   gcloud config set project optical-depth-506808-t0
   ```
   If no browser opens, use `gcloud auth login --no-launch-browser` and paste the code it gives you.
3. **Use the bot folder from Part 1:** `cd ~/OpenBook/correspondence-monitor`. If `git pull` asks for a password,
   make a GitHub token at https://github.com/settings/tokens/new (tick **repo**; if you see **Configure SSO**,
   authorize **humahq**). Paste the token as the password.
4. **Check it works:**
   ```
   CORMON_VM=cormon-edge ./bin/cormon-cloud status
   ```
   The first time, it asks to create a key. Press Return twice.

**Always start these commands with `CORMON_VM=cormon-edge`**, so they go to server 2. Without it, they go to the
live bot.

### Write and run tests

Make a folder, for example `my-tests`, with one file per message:

- **A chat message:** `t1.json`
  ```
  {"text": "G-915-001 wk2 call done 10/6, baseline was 9/15", "from": "crc", "expect": "visit_schedule"}
  ```
- **An email:** `t2.txt`
  ```
  Subject: G-915-002 update
  From: crc
  Expect: sample_handling

  Baseline CSF drawn 9/1, first dose 9/15.
  ```
- **A story over several messages, with checks on the answers:** one `.json` file, plus an empty file named
  `.memory` in the folder:
  ```
  {"story": "a supplement asked about later", "messages": [
    {"text": "G-915-003 drinks St John's wort tea every evening.", "chat": "room", "date": "2026-11-02 10:00"},
    {"text": "Is anything G-915-003 takes a problem?", "chat": "direct", "date": "2026-11-12 09:00",
     "answer_has": ["john's wort|johns wort"], "answer_lacks": ["couldn't find"]}]}
  ```

`expect` is the kind of problem the message has: `eligibility`, `visit_schedule`, `procedure_sequence`,
`sample_handling`, `study_drug`, `consent`, `none` or `out_of_scope` (safety). `from` is a role: `crc`, `cra`,
`pi`, `clinops_lead` or `medical_monitor`.

Run them:

```
CORMON_VM=cormon-edge ./bin/cormon-cloud test my-tests --study NCT05842941
```

Each message shows **RIGHT** or **WRONG**. At the end you get precision (how many alerts were real), recall (how
many real problems were caught) and F1 (both together). A ready-made set: `demo/test-chat-g/chats`.

**Regimen G limits, handy for writing tests** (Baseline is Day 0):

| Area | Rule |
|---|---|
| Visit windows | Week 2 phone: Day 14 ± 3 · Week 4: Day 28 ± 7 · Week 8: Day 56 ± 7 · Week 12 phone: Day 84 ± 3 · Week 16: Day 112 ± 7 · Week 20 phone: Day 140 ± 3 · Week 24: Day 168 ± 7 |
| Samples | Baseline CSF within one week before the first dose · predose PK 0 to 2 hours before the dose · postdose PK 1 to 8 hours after |
| Study drug | stored below 30 °C · one dose a day in the morning |
| Medicines | strong CYP3A4/5 inducers (for example phenytoin) stopped at least 2 weeks before the first dose |
| Consent | Master Protocol consent signed before any screening procedure |

### Other commands (all with `CORMON_VM=cormon-edge` in front)

| Command | What it does |
|---|---|
| `./bin/cormon-cloud status` | What the bot did recently |
| `./bin/cormon-cloud items --study NCT05842941` | Every problem it found |
| `./bin/cormon-cloud item 19 --drafts` | Everything about problem 19 |
| `./bin/cormon-cloud sandbox log --message 4` | Every step of the last test run for message 4 |

**Please don't** use `pause`, and don't start a second copy of the bot that reads Google Chat or the inbox.
Everything you do is recorded under your own name.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| The demo page doesn't open | Is the demo's Terminal window still open? If not, double-click **Start demo.command**. |
| The bot doesn't answer in Mattermost | Did you tag `@mira`? Wait a minute. Check the demo's Terminal window for errors. |
| "OpenAI did not accept that key" | Copy the whole key again, from `sk-` to the end. |
| `gcloud: command not found` | Close the Terminal and open a new one. |
| `Permission denied`, `403` or `not authorized` | Your server access isn't ready. Ask John, then wait 5 minutes. |
| `Password authentication is not supported` | Paste the GitHub token, not your password. |
| `No such file or directory: ./bin/cormon-cloud` | Run `cd ~/OpenBook/correspondence-monitor` first. |

Stuck? Send John a screenshot.

# Setup guide for Peter

This guide shows you how to use the study bot for HEALEY Regimen G (DNL343).

**First: you may not need any setup at all.** The bot runs on a Google server that is always on. To try it, you only need Google Chat or email. Part 1 shows how.

Part 2 (Terminal) is only needed if you want to run many test messages at once, or look at everything the bot has found. It is a one-time setup and takes about 20 minutes.

---

## Part 1: Use the bot in chat or by email (no setup)

### How the bot behaves in chat

- In the space **DNL343 Study Updates**, the bot **stays quiet during normal chat**. It still reads every message, and it posts an alert when it sees a real problem.
- It **answers when you talk to it**. Do one of these:
  - **@mention it:** type `@`, then pick **Huma bot** from the list;
  - **start your message with** `bot,`;
  - **reply in the thread of one of its alerts.**
- If a message says a patient may be seriously hurt (for example, a death or a hospital stay), the bot always replies: "This may be a Serious Adverse Event. Contact the PI and the Medical Monitor now."

### Ask the bot a question

In **DNL343 Study Updates**, @mention the bot and write a question, for example:

```
@Huma bot Question about G-912-001: What is the window for the Week 8 visit?
```

The bot answers in a reply under your message (click "1 reply" to open it). It takes about a minute.

- The answer is short and plain. Click **Details and the protocol's words** under it to see the protocol's exact words and how the answer was worked out.
- If the documents don't answer it, it says so. It doesn't guess.
- If you ask it "is this a problem?" and nothing is wrong, it says "I checked this against the protocol and found no problem."

### Check a site message for problems

Write a message in **DNL343 Study Updates** as a site coordinator would. You don't need to @mention the bot for this. For example:

```
Hi team, update on G-913-001: baseline + first dose were 9/15. Baseline CSF was drawn 9/5. Wk2 phone call done 10/6. Also our kit fridge read 32C this morning.
```

In about a minute, the bot posts one alert with 3 problems: the CSF was drawn too early, the fridge was too warm, and the Week 2 call was outside its window. A problem the bot is less sure of is marked "please check (not sure)".

A message with nothing wrong (for example `G-913-002 baseline visit done today, all fine.`) gets no reply. That's on purpose.

**Tip:** use a new patient number for each test (G-913-001, G-914-001 and so on). The bot remembers each patient, so reusing a number mixes your tests.

**By email:** send an email **to** the CRA test address `johnernest978@gmail.com`, with the bot in **CC**: `bot+NCT05842941@huma.ai`. Any email account works.

### Chat with the bot 1-on-1

Start a direct chat with **Huma bot** in Google Chat. There it always replies:

- a question gets an answer;
- a site update with a problem gets the problem itself: what's wrong, why, and what to do. The CRA gets an email. Nothing is posted in the group, because you said it privately;
- anything else gets "Thanks for letting me know."

The first time you message the bot 1-on-1, it may not reply until John accepts the chat on the bot's side. If it stays silent, tell John.

### Add the bot to your own group chat

Make a group chat (or use one you have), and add **Huma bot**. It follows the same rules as the study space: tag it (`@Huma bot`) or start with "bot," to ask it something. Otherwise it stays quiet, unless someone reports serious harm. Its alerts stay in that chat; nothing is posted in the study space.

### Mark an alert as done

Reply in the alert's thread (or reply to the alert email):

| You write | What happens |
|---|---|
| `cleared` | All problems in that alert are marked as dealt with. |
| `cleared #20` | Only problem number 20 is marked as dealt with. |
| `disregard #20` | Problem number 20 is dropped as not a real problem. |

The bot confirms in the thread, for example "Done: #20 cleared, by Peter Brown."

You and Eliot are set up as testers: you can do all of the above, and the bot doesn't email you its alerts.

That's all you need for chat and email. Stop here unless you want Part 2.

---

## Part 2: Terminal setup (one time, about 20 minutes)

### Before you start: two things from John

1. **Access to the bot's server.** Send John your huma.ai email address. He gives you access. Wait **5 minutes** after he says it's done.
2. **Access to the code on GitHub.** You need a free GitHub account. Make one at <https://github.com/signup> if you don't have one. Send John your GitHub username, and he adds you. GitHub then emails you an invitation: open it and click **Accept invitation**.

### Step 1. Open the Terminal

The Terminal is a window where you type commands. Type (or paste) each command, then press **Enter**.

- **Mac:** press **Cmd + Space**, type `Terminal`, press **Enter**.
- **Windows:** first install Ubuntu (a Linux system that runs inside Windows). You only do this once:
  1. Click Start, type `PowerShell`, right-click **Windows PowerShell**, choose **Run as administrator**, and click **Yes**.
  2. Type this and press Enter:
     ```
     wsl --install
     ```
  3. When it finishes, restart your computer.
  4. After the restart, click Start, type `Ubuntu`, and open it. The first time, it asks you to make a username and password. Pick anything simple, and remember the password. (Nothing shows while you type the password. That's normal.)
  5. From now on, "the Terminal" means this **Ubuntu** window. Use it for every step below.
- **Linux:** open the **Terminal** app.

### Step 2. Mac only: install Apple's developer tools

Skip this step on Windows and Linux.

```
xcode-select --install
```

A box pops up. Click **Install**, then **Agree**, and wait until it says it's done (5 to 15 minutes).
If the Terminal says `command line tools are already installed`, that's fine. Go to Step 3.

### Step 3. Install the Google Cloud tool

```
curl https://sdk.cloud.google.com | bash
```

It downloads for a minute, then asks a few questions. **Press Enter at every question** (the default answers are right).

When it says it's done, **close the Terminal window and open a new one**. (On Windows: close Ubuntu and open it again.)

Check that it worked:

```
gcloud --version
```

You should see a line starting with `Google Cloud SDK` and a number. Google now calls the tool the "Google Cloud CLI", but it's the same tool.

### Step 4. Sign in to Google

**On Mac and Linux:**

```
gcloud auth login
```

A browser opens. Choose your **huma.ai** account and click **Allow**.

**On Windows (Ubuntu), or if no browser opens after 10 seconds** (press **Ctrl + C** first to stop the waiting):

```
gcloud auth login --no-launch-browser
```

It shows a long web link. Copy it into your browser, choose your huma.ai account, and click **Allow**. The page shows a code. Copy the code, paste it into the Terminal, and press **Enter**.

Then tell the tool which project to use:

```
gcloud config set project optical-depth-506808-t0
```

**Don't run `gcloud init`.** It asks you to pick from a long list of projects. If you already did and it's asking, pick `optical-depth-506808-t0`.

### Step 5. Make a GitHub token (GitHub's password for the Terminal)

GitHub doesn't accept your normal password in the Terminal. You need a "token" instead.

1. Sign in to GitHub in your browser, then open <https://github.com/settings/tokens/new>.
2. **Note:** type `study bot`.
3. **Expiration:** choose **90 days**.
4. Tick the box **repo** (the first box in the list).
5. Scroll to the bottom and click **Generate token**.
6. Copy the token. It starts with `ghp_`. **Save it somewhere safe now.** GitHub shows it only once.
7. If you see a **Configure SSO** button next to the token, click it, then click **Authorize** next to **humahq**.

### Step 6. Download the code

```
cd ~
git clone https://github.com/humahq/correspondence-monitor.git
```

It asks for two things:

- **Username:** your GitHub username.
- **Password:** paste your **token** from Step 5 (not your GitHub password). Nothing shows while you paste. That's normal. Just press **Enter**.

If you see `git: command not found` on Linux or Ubuntu, run `sudo apt-get install -y git` (it asks for your Ubuntu password), then try `git clone` again.

### Step 7. Check that it all works

```
cd ~/correspondence-monitor
./bin/cormon-cloud status
```

- The first time, it may say it's creating a key, and ask for a "passphrase". **Press Enter twice** (no passphrase).
- You may see a warning about **NumPy**. Ignore it. It's harmless.

**It worked if you see** a line like:

```
listener heartbeat: 16 Oct 2026 11:31   processing: running   channels: email=imap teams=gchat
```

You're set up. 🎉

---

## Part 3: Everyday use

Each time, open the Terminal and go to the bot's folder first:

```
cd ~/correspondence-monitor
```

Then use any of these:

| What you want | Type this |
|---|---|
| Is the bot running? What did it do recently? | `./bin/cormon-cloud status` |
| Every problem the bot found | `./bin/cormon-cloud items --study NCT05842941` |
| Everything about one problem (for example number 19) | `./bin/cormon-cloud item 19 --drafts` |
| Preview the sponsor's daily summary | `./bin/cormon-cloud digest --hours 24` |
| Run 30 ready-made test chat messages (nothing is sent to anyone; takes about 5 minutes) | `./bin/cormon-cloud test demo/test-chat-g/chats --study NCT05842941` |

**Always add `--study NCT05842941`** to `items` and `test`. Without it, the bot uses an old demo study.

### Write your own test messages

Tests run on a practice copy of the bot. Nothing is posted or emailed, and the live data isn't touched. Each message costs a few cents of AI use.

1. Make a folder for your tests:
   ```
   mkdir -p ~/correspondence-monitor/my-tests
   ```
2. Open that folder in a normal window. **Mac:** type `open ~/correspondence-monitor/my-tests`. **Windows:** type `cd ~/correspondence-monitor/my-tests && explorer.exe .` (with the dot at the end).
3. In that folder, make a plain text file called `t1.txt` (Mac: use TextEdit and choose **Format > Make Plain Text**; Windows: use Notepad). Write a message like this:
   ```
   Subject: G-915-001 update
   From: crc
   Expect: sample_handling

   Baseline CSF drawn 9/1, first dose 9/15.
   ```
   - `From: crc` means it is written as a site coordinator.
   - `Expect:` (optional) is the kind of problem you think it has, so the bot can be scored. You can use `eligibility`, `visit_schedule`, `procedure_sequence`, `sample_handling`, `study_drug`, `consent`, or `none` (nothing wrong).
   - Make one file per message: `t2.txt`, `t3.txt` and so on.
4. Run them:
   ```
   cd ~/correspondence-monitor
   ./bin/cormon-cloud test my-tests --study NCT05842941
   ```

Each message shows **right** or **WRONG**, and what the bot found. The results are also saved in the `results` folder.

### Get the newest version of the code

```
cd ~/correspondence-monitor
git pull
```

If it asks for a username and password, use your GitHub username and your **token** again.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| `gcloud: command not found` | Close the Terminal and open a new one. If it still happens, do Step 3 again. |
| `Password authentication is not supported` | You typed your GitHub password. Paste the **token** from Step 5 instead. |
| `Repository not found` | John hasn't added you yet, or you haven't accepted the GitHub invitation email. Or the token is missing the **repo** box: make a new token (Step 5). |
| `Permission denied`, `403`, `4033` or `not authorized` | Your server access isn't ready yet. Ask John, then wait 5 minutes and try again. |
| It asks you to pick a project | Pick `optical-depth-506808-t0`. |
| A warning about **NumPy** | Ignore it. |
| `No such file or directory: ./bin/cormon-cloud` | You're in the wrong folder. Type `cd ~/correspondence-monitor` first. |
| The token stopped working after 90 days | Make a new one (Step 5). |

**Please don't** start the bot on your own laptop, and don't use `pause` unless John asks. Only one copy of the bot may read the inbox and chat. Everything you do is recorded under your own name.

Stuck? Send John a screenshot of the Terminal.

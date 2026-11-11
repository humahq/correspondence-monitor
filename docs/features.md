# New features: demo or live

Every new feature has a switch in `config/settings.yaml`. It is **off on the live bot** until John says
"yes, put this feature in the live bot". The Mattermost demo (`cormon chat-demo`) always has every feature on.

When a switch is off, the bot works exactly as the live version does (a code test checks this:
`test_new_features_are_off_on_the_live_bot_until_switched_on`).

## The list

| Feature | What it does | Switch | Demo | Live bot | Going live? |
|---|---|---|---|---|---|
| Mattermost chat | The bot in a local Mattermost: rooms, 1-on-1 chats, one team per customer, the demo page | `channels.teams: mattermost` | on | no (demo only) | no |
| "is typing" | Shows "Mira Belle is typing..." while the bot works on a chat message to it | works only in Mattermost | on | no (Teams later) | later, with Teams |
| Private reports in chat | A report kept out of the room also goes to the person's 1-on-1 chat | `mattermost.reports_by_chat` | on | no (demo only) | no |
| Patient memory | Remembers facts about a participant from earlier messages (Hindsight) and uses them when checking a new one | `memory.enabled` | on | off | yes |
| Permission claims | "Dr. Smith said it's fine": a permission passed on by someone else is not accepted; the bot asks to hear it from them | `features.permission_claims` | on | on (11 Nov 2026) | yes |
| Second look | A second reading of the email's facts only, without its wording, so words aimed at the AI can't hide a problem | `features.second_look` | on | on (11 Nov 2026) | yes |
| Privacy check | Before an answer or alert that uses patient memory goes out: checks who may hear what (Eliot, Nov 4: study ID only outside the care team; nothing new about a patient once the chat has shown who they are) | `features.privacy_check` | on | off | yes, with patient memory |
| OpenBook look | The admin page in OpenBook's look (openbook.phd): its name, navy, teal and cream, and the Inter font. Mattermost's own OpenBook look is set by cormon-chat (`brand.sh`) | `features.openbook_look` | on | off | ask John |
| Numbered steps | An answer from a numbered list of steps ("1. Click Add Subject") counts as the document's words, so the bot gives it instead of "I couldn't find this" (Eliot, Nov 6). Tested: the 100 questions gain one right answer; the 100 alert emails score the same | `features.numbered_steps` | on | on (9 Nov 2026) | yes |
| Language courtesy | In a chat, the bot answers in a language everyone there reads. If someone writes in a language another person doesn't read, it says so kindly and answers in the shared language (English). In a 1-on-1 chat it answers in the person's language. A person's languages are `languages:` in study.yaml (Eliot, Nov 6) | `features.language_courtesy` | on | on (9 Nov 2026) | yes |
| Names in safety emails | The medical monitor's own safety email may name the participant. The CRA gets a separate copy, and the escalation emails go out, with the names taken out of the site's words (Eliot, Nov 6) | `features.safety_names` | on | on (9 Nov 2026) | yes |
| Outside people on an email | An email with someone on it the study doesn't list (sender, To or Cc) gets no answer. The bot tells only the sender who shouldn't be on it, with no study information. Someone from an approved domain (`email_domains:` in study.yaml) is likely a new colleague: the CRA is asked to set them up. Anyone else is a privacy event: the CRA and the admins are told (Eliot, Nov 10, message 335) | `features.outside_recipients` | on | on (10 Nov 2026) | yes |
| Close an alert on its page | The alert's page has three fixed choices: "I did what was asked" (with when) closes it; "I did something else" (what and when) and "Nothing can be done" (why) wait for a second person, who approves it, sends it back, or reports it to the sponsor. A check-and-confirm step first; nobody reviews their own; Google sign-in needed. A "cleared" reply in chat gets the page's link instead. Who may close and review: `closing:` in study.yaml (for now Peter, Eliot and John) (Eliot, 29 Oct and 10 Nov) | `features.close_on_page` | on | on (switched on 10 Nov 2026, goes to the server with the next release) | yes |
| Trick detector | Meta's open-source Llama Prompt Guard 2 (22M) reads each message on our own machine. A message that looks like it gives the AI orders is recorded and the admins are told; nothing is cleared or changed. Measured (5 Nov): 4 of 89 trick messages flagged, 0 of 1,220 normal ones. Needs `uv sync --extra detector` and the model folder from `deploy/prompt_guard_export.py` (Eliot, 4 Nov: a known company's library first) | `features.trick_detector` | on | off | ask John |
| Marked messages | Before the AI reads a message, invisible characters are taken out and the message is marked at both ends with a tag made from it, so words in it can't pose as the bot's own instructions (CAP-09 step 1) | `features.mark_email` | on | on (11 Nov 2026) | yes |
| Orders to the bot | One small AI question: does the message tell the bot what to conclude, record or report ("no alert is needed", "also report ...")? If so the admins are told, the participant's alert says so, or the orders become a "please check" of their own; an order to report something never makes an alert by itself (CAP-09 step 4) | `features.flag_orders` | on | on (11 Nov 2026) | yes |
| Orders: second judge | A sentence the orders question listed is asked again, on its own, of OpenAI's Decisions API: does it steer the bot or the monitor? The flag is dropped only when it is sure not (score under 0.2); if the call fails, the flag stays. Fewer false "orders" alerts, e.g. "please note for your records" | `features.confirm_orders` | on | off | ask John |
| Safety backstop | A message with one of the severe safety words (`safety.severe_words`) that the AI did not send to the medical monitor is sent anyway. Only adds (CAP-09 step 5; needs Eliot's OK, as it is a word list) | `features.safety_backstop` | on | off | ask Eliot |
| Other AI models | Claude and Gemini can be picked as the model for a task (for model comparisons) | a model name under `llm.models` | not used | not used | only if a test shows it is better |

Patient memory and the privacy check go live together: memory can bring up private facts, and the privacy check
is what stops them reaching the wrong person.

### Still to do before patient memory and the privacy check go live

1. Send Eliot the updated roles page (`docs/eliot/privacy-roles.pdf`, 6 Nov) and get his answer to its one
   question: may the sponsor hear why a participant takes a medicine, or a diagnosis that changes nothing in the
   study (BRCA1, SOD1)?
2. Show Eliot the patient memory page (`/admin/memory`) and ask who may correct a fact.
3. Decide how long facts are kept, and whether we need a BAA with OpenAI.
4. Install Hindsight on the server (`deploy/hindsight.sh`).
5. Then John says yes: the steps below, with `memory.enabled` and `features.privacy_check` switched on together.

## When John says "yes, put this feature in the live bot"

1. Set its switch to `true` in `config/settings.yaml` (for patient memory: `memory.enabled: true`).
2. Update the table above: Live bot "on", and the date.
3. Run the code tests (`pytest tests`), then commit with an issue number and push.
4. The release gate runs the AI test sets with the switch on. They must pass.
5. John runs `deploy/release.sh`. The code and the switch go live together: the release puts the `features:`,
   `language:` and `safety:` parts of `settings.yaml` in place, approved by the gate, so no second person has
   to approve the switch on the server. On the server itself these parts can't be changed by hand.
   Patient memory is the exception: `memory:` holds the server's own Hindsight details, so `memory.enabled` is
   switched on on the server (`cormon config set settings memory.enabled true`) and a second person approves it.

To try a feature in a test run before then: `CORMON_TEST_FEATURES=permission_claims cormon test <folder>`
(names separated by commas, or `all`). Patient memory in a test run: `CORMON_TEST_MEMORY=1`.

## Adding a new feature

Give it a switch under `features:` in `config/settings.yaml`, set to `false`, and read it with
`settings.feature("name")` where the feature starts. With the switch off the bot must work as before. Add it to the
table above. The demo turns on every switch under `features:` by itself.

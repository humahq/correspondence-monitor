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
| Permission claims | "Dr. Smith said it's fine": a permission passed on by someone else is not accepted; the bot asks to hear it from them | `features.permission_claims` | on | off | yes |
| Second look | A second reading of the email's facts only, without its wording, so words aimed at the AI can't hide a problem | `features.second_look` | on | off | ask John |
| Privacy check | Before an answer or alert that uses patient memory goes out: checks who may hear what (Eliot, Nov 4: study ID only outside the care team; nothing new about a patient once the chat has shown who they are) | `features.privacy_check` | on | off | yes, with patient memory |
| OpenBook look | The admin page in OpenBook's look (openbook.phd): its name, navy, teal and cream, and the Inter font. Mattermost's own OpenBook look is set by cormon-chat (`brand.sh`) | `features.openbook_look` | on | off | ask John |
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
5. John runs `deploy/release.sh`. The code and the switch go live together.

To try a feature in a test run before then: `CORMON_TEST_FEATURES=permission_claims cormon test <folder>`
(names separated by commas, or `all`). Patient memory in a test run: `CORMON_TEST_MEMORY=1`.

## Adding a new feature

Give it a switch under `features:` in `config/settings.yaml`, set to `false`, and read it with
`settings.feature("name")` where the feature starts. With the switch off the bot must work as before. Add it to the
table above. The demo turns on every switch under `features:` by itself.

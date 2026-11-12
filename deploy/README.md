# Updating the cloud copy

- **Updating the code:** push to `main` on GitHub. The gate (`.github/workflows/ci.yml`) runs the code tests and
  the AI test sets the change can affect (`config/gates.yaml`). When the gate has passed, `./deploy/release.sh`
  puts that commit's code on the server, restarts the bot and checks it. It refuses a commit whose gate did not
  pass, and stops if the server's packages, code or study model are not what the gate tested. Settings and data
  on the server are not touched.
- **Updating the rules:** rebuild them in the practice copy (`CORMON_HOME=practice cormon ingest --force`), read
  `cormon rules --report`, export the model tables to `studies/model/model.sql`, commit and push so the gate tests
  them, then `SYNC_MODEL=1 ./deploy/create_vm.sh` (it puts the committed `studies/model/model.sql` on the server).
  Messages and items on the server are kept; the swap is recorded in the log.
- **Packages:** the gate installs exactly `deploy/requirements-live.txt` (the server's `pip freeze`). Changing a
  package means changing that file (so every test set runs) and installing it on the server as its own step.
- **Settings:** changes made on the server with `cormon config set` can be overwritten by the repo's `config/` (a
  release puts the gate-tested triage, routing, templates, careful, studies and llm/matching settings in place),
  so make permanent changes in the repo. Deployment-specific values (the bot's name, avatar and email signature
  under `bot:`) go in the server's untracked `config/settings.local.yaml`, which a release leaves alone.
- **Giving someone access:** `./deploy/grant_access.sh name@huma.ai` (Eliot and Peter already have access).
- **The test server (`cormon-edge`):** a second bot with its own account (bot_test@huma.ai), its own Chat rooms
  and the new features on, set in `deploy/edge/settings.local.yaml`. It never gets the live bot's sign-ins.
  1. `NAME=cormon-edge LOCAL_SETTINGS=deploy/edge/settings.local.yaml ./deploy/create_vm.sh` (it starts with
     email and chat off).
  2. Store bot_test's Gmail app password:
     `gcloud secrets versions add bot-test-gmail-app-password --project optical-depth-506808-t0 --data-file=-`.
  3. `./deploy/edge_connect.sh chat`: sign in as bot_test in the browser; it lists bot_test's Chat spaces.
  4. `STUDY_SPACE=spaces/... ALERT_SPACE=spaces/... ./deploy/edge_connect.sh on`, then a second person approves
     the settings change with the command it prints (the server keeps the old settings until then), and
     `./deploy/edge_connect.sh restart`.
  Later updates: `CORMON_VM=cormon-edge ./deploy/release.sh` (same gate; its tag is `live-cormon-edge`).

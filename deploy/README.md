# Updating the cloud copy

- **Updating the code:** `./deploy/create_vm.sh` copies the current code to the server and restarts the bot. The live data on the server is kept.
- **Updating the rules:** rebuild them in the practice copy (`CORMON_HOME=practice cormon ingest --force`), read `cormon rules --report`, run the test sets, then `cormon import-model practice/data/cormon.db --reason "..."` on the laptop and `SYNC_MODEL=1 ./deploy/create_vm.sh`. Messages and items on the server are kept; the swap is recorded in the log.
- **Settings:** changes made on the server with `cormon config set` are overwritten by the repo's `config/` on the next update, so make permanent changes in the repo.
- **Giving someone access:** `./deploy/grant_access.sh name@huma.ai` (Eliot and Peter already have access).

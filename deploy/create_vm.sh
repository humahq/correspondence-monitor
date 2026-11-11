#!/usr/bin/env bash
# Creates the always-on cloud copy of the bot, or updates it with the current code.
# Run it from the project folder, the first time and after every change:
#     ./deploy/create_vm.sh
# The live data on the VM (database, messages) is kept across updates.
# To also replace the VM's study model (rules) with the laptop's, after rebuilding and testing it:
#     SYNC_MODEL=1 ./deploy/create_vm.sh
# A second server with its own bot account (Peter's newest-features copy):
#     NAME=cormon-edge LOCAL_SETTINGS=deploy/edge/settings.local.yaml ./deploy/create_vm.sh
# It gets its own machine account, may read only its own app password, and gets none of the live bot's sign-ins
# (secrets/), so it can never read or answer the live bot's mail and chat. LOCAL_SETTINGS becomes its
# config/settings.local.yaml the first time; later runs leave the server's copy alone.
set -euo pipefail

PROJECT="${PROJECT:-optical-depth-506808-t0}"
ZONE="${ZONE:-us-east1-b}"
NAME="${NAME:-cormon-demo}"
MACHINE="${MACHINE:-e2-medium}"         # 4 GB: the bot plus Hindsight (issue #32); 2 GB was too slow
LOCAL_SETTINGS="${LOCAL_SETTINGS:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
G=(gcloud --project "$PROJECT" --quiet)
EXCLUDE=()
if [ "$NAME" = cormon-demo ]; then
  SA_ID=cormon-vm
  SECRET="${SECRET:-kol-gmail-bot-app-password}"
else
  [ -n "$LOCAL_SETTINGS" ] && [ -f "$LOCAL_SETTINGS" ] \
    || { echo "NAME=$NAME needs LOCAL_SETTINGS=<its settings.local.yaml> with its own bot account" >&2; exit 1; }
  grep -q 'address: *bot@huma.ai' "$LOCAL_SETTINGS" && { echo "$LOCAL_SETTINGS uses the live bot's account" >&2; exit 1; }
  SA_ID="$NAME-vm"
  SECRET="${SECRET:-$(sed -n 's/^ *name: *\([^ #]*gmail-app-password\).*/\1/p' "$LOCAL_SETTINGS" | head -1)}"
  [ -n "$SECRET" ] || { echo "$LOCAL_SETTINGS names no mailbox.imap.secret.name" >&2; exit 1; }
  EXCLUDE=(--exclude ./secrets --exclude ./config/settings.local.yaml)
fi
SA="$SA_ID@$PROJECT.iam.gserviceaccount.com"

"${G[@]}" services enable compute.googleapis.com iap.googleapis.com secretmanager.googleapis.com >/dev/null

# The VM's own identity: it may read its Gmail app password secret and nothing else.
if ! "${G[@]}" iam service-accounts describe "$SA" >/dev/null 2>&1; then
  "${G[@]}" iam service-accounts create "$SA_ID" --display-name "Correspondence monitor VM ($NAME)"
fi
if ! "${G[@]}" secrets describe "$SECRET" >/dev/null 2>&1; then
  "${G[@]}" secrets create "$SECRET" --replication-policy automatic >/dev/null
  echo "made the empty secret $SECRET: add the bot account's app password with"
  echo "  gcloud secrets versions add $SECRET --project $PROJECT --data-file=-"
fi
"${G[@]}" secrets add-iam-policy-binding "$SECRET" \
  --member "serviceAccount:$SA" --role roles/secretmanager.secretAccessor >/dev/null

if ! "${G[@]}" compute instances describe "$NAME" --zone "$ZONE" >/dev/null 2>&1; then
  "${G[@]}" compute instances create "$NAME" \
    --zone "$ZONE" --machine-type "$MACHINE" \
    --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 30GB --boot-disk-type pd-standard \
    --service-account "$SA" --scopes cloud-platform \
    --metadata enable-oslogin=TRUE \
    --tags cormon-web
  echo "waiting for the VM to boot ..."; sleep 40
fi

# Item pages over HTTPS (80 is needed for the certificate); SSH only through Google's IAP proxy.
if ! "${G[@]}" compute firewall-rules describe cormon-web-https >/dev/null 2>&1; then
  "${G[@]}" compute firewall-rules create cormon-web-https --allow tcp:80,tcp:443 --target-tags cormon-web \
    --description "Item detail pages for the correspondence monitor (HTTPS)"
fi
if ! "${G[@]}" compute firewall-rules describe cormon-iap-ssh >/dev/null 2>&1; then
  "${G[@]}" compute firewall-rules create cormon-iap-ssh --allow tcp:22 --target-tags cormon-web \
    --source-ranges 35.235.240.0/20 --description "SSH to the correspondence monitor through IAP only"
fi

IP=$("${G[@]}" compute instances describe "$NAME" --zone "$ZONE" \
  --format 'get(networkInterfaces[0].accessConfigs[0].natIP)')
HOST="${IP//./-}.sslip.io"

echo "packing the project ..."
SEED=/tmp/cormon-seed.db
rm -f "$SEED"
sqlite3 "$ROOT/data/cormon.db" ".backup '$SEED'"          # study model only is taken from this (see setup_vm.sh)
COPYFILE_DISABLE=1 tar --no-xattrs --exclude '._*' --exclude .DS_Store --exclude .venv --exclude ./data --exclude './practice*' --exclude ./sandbox --exclude ./results \
    --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache --exclude '*.egg-info' --exclude .git \
    --exclude ./.work --exclude ./models ${EXCLUDE[@]+"${EXCLUDE[@]}"} \
    -czf /tmp/cormon.tgz -C "$ROOT" .

echo "copying to $NAME ..."
SSH=("${G[@]}" compute ssh "$NAME" --zone "$ZONE" --tunnel-through-iap)
FILES=(/tmp/cormon.tgz "$SEED")
LOCAL_STEP=true
if [ -n "$LOCAL_SETTINGS" ]; then
  cp "$LOCAL_SETTINGS" /tmp/cormon-settings.local.yaml
  FILES+=(/tmp/cormon-settings.local.yaml)
  LOCAL_STEP="{ [ -f /opt/cormon/config/settings.local.yaml ] || sudo install -m 644 /tmp/cormon-settings.local.yaml /opt/cormon/config/settings.local.yaml; }"
fi
"${G[@]}" compute scp --zone "$ZONE" --tunnel-through-iap "${FILES[@]}" "$NAME":/tmp/
"${SSH[@]}" --command "sudo mkdir -p /opt/cormon && sudo tar -xzf /tmp/cormon.tgz -C /opt/cormon 2>/dev/null \
  && sudo find /opt/cormon -name '._*' -not -path '*/.venv/*' -delete \
  && $LOCAL_STEP && rm -f /tmp/cormon-settings.local.yaml \
  && sudo HOST=$HOST SYNC_MODEL=${SYNC_MODEL:-0} bash /opt/cormon/deploy/setup_vm.sh"
rm -f /tmp/cormon.tgz "$SEED" /tmp/cormon-settings.local.yaml

echo
echo "The cloud bot is running on $NAME ($IP)."
echo "  item pages:     https://$HOST"
if [ "$NAME" = cormon-demo ]; then
  echo "  command line:   ./bin/cormon-cloud status"
  echo "  give access:    ./deploy/grant_access.sh someone@huma.ai"
else
  echo "  command line:   CORMON_VM=$NAME ./bin/cormon-cloud status"
  echo "  give access:    NAME=$NAME ./deploy/grant_access.sh someone@huma.ai"
fi

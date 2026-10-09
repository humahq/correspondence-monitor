#!/usr/bin/env bash
# Lets a person use ./bin/cormon-cloud: log in to the bot's VM with their own Google account, through IAP.
# Usage: ./deploy/grant_access.sh eliot@huma.ai peter@huma.ai
set -euo pipefail

PROJECT="${PROJECT:-optical-depth-506808-t0}"
ZONE="${ZONE:-us-east1-b}"
NAME="${NAME:-cormon-demo}"
SA="cormon-vm@$PROJECT.iam.gserviceaccount.com"
G=(gcloud --project "$PROJECT" --quiet)
[ $# -gt 0 ] || { echo "usage: $0 email [email ...]"; exit 1; }

for who in "$@"; do
  m="user:$who"
  echo "granting $who ..."
  "${G[@]}" compute instances add-iam-policy-binding "$NAME" --zone "$ZONE" \
    --member "$m" --role roles/compute.osAdminLogin >/dev/null
  "${G[@]}" compute instances add-iam-policy-binding "$NAME" --zone "$ZONE" \
    --member "$m" --role roles/compute.viewer >/dev/null
  # only opens the tunnel; logging in still needs the instance-level role above
  "${G[@]}" projects add-iam-policy-binding "$PROJECT" --condition None \
    --member "$m" --role roles/iap.tunnelResourceAccessor >/dev/null
  "${G[@]}" iam service-accounts add-iam-policy-binding "$SA" \
    --member "$m" --role roles/iam.serviceAccountUser >/dev/null
done
echo "done. They can now run ./bin/cormon-cloud status (see README, 'Testing guide')."

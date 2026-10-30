#!/usr/bin/env bash
# Makes the Cloud Storage bucket the live bot copies its hourly backups to (BCK-01). Run once from the laptop.
#
# The bucket: in the VM's region, never public, one access rule for the whole bucket. A copy cannot be deleted
# for 7 days (retention), and is deleted after 30 days (lifecycle). The bot's own account may only add copies
# and read them (it cannot delete or overwrite one). Then set it on the server, with a second person's approval:
#   cormon config set settings system.backup_bucket <bucket>      (and another admin: cormon config approve ...)
set -euo pipefail

PROJECT="${CORMON_PROJECT:-optical-depth-506808-t0}"
REGION="${CORMON_REGION:-us-east1}"
BUCKET="${BUCKET:-$PROJECT-cormon-backups}"
SA="cormon-vm@$PROJECT.iam.gserviceaccount.com"
G=(gcloud --project "$PROJECT" --quiet)

if ! "${G[@]}" storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  "${G[@]}" storage buckets create "gs://$BUCKET" --location "$REGION" --default-storage-class STANDARD \
    --uniform-bucket-level-access --public-access-prevention --retention-period 7d
fi
rules=$(mktemp)
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}' > "$rules"
"${G[@]}" storage buckets update "gs://$BUCKET" --lifecycle-file "$rules" >/dev/null
rm -f "$rules"
for role in roles/storage.objectCreator roles/storage.objectViewer; do
  "${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$SA" --role "$role" >/dev/null
done
echo "gs://$BUCKET ready: copies kept 7 to 30 days; $SA may add and read them"

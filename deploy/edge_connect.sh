#!/usr/bin/env bash
# Connects a second server (made by create_vm.sh with LOCAL_SETTINGS) to its own bot account, then switches its
# email and chat on. Run from the laptop, in steps, after the account's app password is stored:
#     gcloud secrets versions add bot-test-gmail-app-password --project optical-depth-506808-t0 --data-file=-
#
#   ./deploy/edge_connect.sh chat      sign in to Google Chat as the bot account (a browser opens) and copy the
#                                      sign-in to the server; then it lists the Chat spaces the account is in
#   STUDY_SPACE=spaces/... ALERT_SPACE=spaces/... ./deploy/edge_connect.sh on
#                                      the test bot's rooms for the study, then email and chat switched on
#
# The live bot's sign-ins on this laptop (secrets/) are never touched: the Chat sign-in is made in a folder of
# its own and deleted after it is copied.
set -euo pipefail

PROJECT="${PROJECT:-optical-depth-506808-t0}"
ZONE="${ZONE:-us-east1-b}"
VM="${CORMON_VM:-cormon-edge}"
STUDY="${STUDY:-NCT05842941}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL="$ROOT/deploy/edge/settings.local.yaml"
[ "$VM" != cormon-demo ] || { echo "this is for a second server, not the live one" >&2; exit 1; }
SSH=(gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
ACCOUNT=$(sed -n 's/^ *account: *\([^ #]*\).*/\1/p' "$LOCAL" | head -1)
SECRET=$(sed -n 's/^ *name: *\([^ #]*gmail-app-password\).*/\1/p' "$LOCAL" | head -1)
remote() { "${SSH[@]}" --command "$1" </dev/null; }
cormon() { remote "cd /opt/cormon && sudo -u cormon env CORMON_HOME=/opt/cormon .venv/bin/cormon $*"; }

case "${1:-}" in
  chat)
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    mkdir -p "$tmp/secrets"
    cp -R "$ROOT/config" "$tmp/config"
    rm -f "$tmp/config/settings.local.yaml"
    cp "$LOCAL" "$tmp/config/settings.local.yaml"
    cp "$ROOT/secrets/gchat_client.json" "$tmp/secrets/"
    ln -s "$ROOT/studies" "$tmp/studies"
    echo "a browser opens: sign in as $ACCOUNT and tick every box"
    (cd "$ROOT" && CORMON_HOME="$tmp" uv run cormon auth gchat)
    gcloud compute scp --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet \
      "$tmp/secrets/gchat_token.json" "$VM":/tmp/gchat_token.json
    remote "sudo install -o cormon -g cormon -m 600 /tmp/gchat_token.json /opt/cormon/secrets/gchat_token.json \
      && rm -f /tmp/gchat_token.json"
    echo "the Chat spaces $ACCOUNT is in:"
    cormon chat-spaces || true
    echo "next: STUDY_SPACE=spaces/... ALERT_SPACE=spaces/... $0 on"
    ;;
  on)
    : "${STUDY_SPACE:?STUDY_SPACE=spaces/... (the test study room)}" "${ALERT_SPACE:?ALERT_SPACE=spaces/... (the test urgent room)}"
    gcloud secrets versions list "$SECRET" --project "$PROJECT" --filter state=enabled --limit 1 --format 'value(name)' \
      | grep -q . || { echo "no app password in $SECRET yet" >&2; exit 1; }
    remote "sudo test -s /opt/cormon/secrets/gchat_token.json" || { echo "run '$0 chat' first" >&2; exit 1; }
    cormon auth imap
    # Every other study gets no room: the live bot's rooms refuse this account, and one refusal stops all Chat.
    remote "cd /opt/cormon && sudo -u cormon .venv/bin/python - <<'EOF'
from pathlib import Path
from ruamel.yaml import YAML
p = 'config/settings.local.yaml'
y = YAML(); cfg = y.load(open(p))
cfg['channels'] = {'email': 'imap', 'teams': 'gchat'}
none = {'space': '', 'name': 'none on this server'}
rooms = {f.parent.name: {'study_channel': none, 'alert_channel': none} for f in Path('studies').glob('*/study.yaml')}
rooms['$STUDY'] = {'study_channel': {'space': '$STUDY_SPACE', 'name': 'study room (test)'},
                   'alert_channel': {'space': '$ALERT_SPACE', 'name': 'urgent room (test)'}}
cfg.setdefault('chat', {})['rooms'] = rooms
y.dump(cfg, open(p, 'w'))
EOF"
    sleep 30   # the bot records the change and, until a second person approves it, keeps the old settings
    id=$(remote "sudo sqlite3 'file:/opt/cormon/data/cormon.db?mode=ro' \"SELECT max(id) FROM config_versions
      WHERE path='config/settings.local.yaml' AND waiting=1 AND approved_by IS NULL\"" 2>/dev/null | tr -dc 0-9)
    echo
    echo "Settings changes need a second person. Someone other than the one who ran this approves with:"
    echo "  CORMON_VM=$VM ./bin/cormon-cloud config approve settings ${id:-<id from: config history settings>}"
    echo "Then $VM reads and answers $ACCOUNT's mail (study addresses only) and its own Chat rooms."
    ;;
  *)
    sed -n '2,12p' "$0"; exit 1 ;;
esac

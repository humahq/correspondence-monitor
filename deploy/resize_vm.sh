#!/usr/bin/env bash
# Moves the live server to a bigger machine (issue #32: e2-medium, 4 GB, for Hindsight) and starts Hindsight on it.
# Run from the laptop:   ./deploy/resize_vm.sh
# The bot is down for about 2 minutes; mail and chat that arrive meanwhile are read when it is back.
# The server's address is reserved first, so it stays the same and the item-page links in sent mail keep working.
set -euo pipefail

PROJECT="${CORMON_PROJECT:-optical-depth-506808-t0}"
ZONE="${CORMON_ZONE:-us-east1-b}"
REGION="${ZONE%-*}"
VM="${CORMON_VM:-cormon-demo}"
MACHINE="${MACHINE:-e2-medium}"
G=(gcloud --project "$PROJECT" --quiet compute)
SSH=("${G[@]}" ssh "$VM" --zone "$ZONE" --tunnel-through-iap)

IP=$("${G[@]}" instances describe "$VM" --zone "$ZONE" --format 'get(networkInterfaces[0].accessConfigs[0].natIP)')
NOW=$("${G[@]}" instances describe "$VM" --zone "$ZONE" --format 'value(machineType.basename())')
echo "$VM is $NOW at $IP"

if ! "${G[@]}" addresses describe "$VM-ip" --region "$REGION" >/dev/null 2>&1; then
  "${G[@]}" addresses create "$VM-ip" --region "$REGION" --addresses "$IP"
  echo "address $IP reserved as $VM-ip"
fi

if [ "$NOW" != "$MACHINE" ]; then
  read -r -p "Stop the live bot for about 2 minutes and move it to $MACHINE? Type yes: " ok || ok=""
  [ "$ok" = yes ] || { echo "not confirmed"; exit 1; }
  "${SSH[@]}" --command "cd /opt/cormon && sudo -u cormon env CORMON_HOME=/opt/cormon .venv/bin/cormon backup" 2>/dev/null
  "${G[@]}" instances stop "$VM" --zone "$ZONE"
  "${G[@]}" instances set-machine-type "$VM" --zone "$ZONE" --machine-type "$MACHINE"
  "${G[@]}" instances start "$VM" --zone "$ZONE"
  AFTER=$("${G[@]}" instances describe "$VM" --zone "$ZONE" --format 'get(networkInterfaces[0].accessConfigs[0].natIP)')
  [ "$AFTER" = "$IP" ] || echo "WARNING: the address changed from $IP to $AFTER" >&2
  sleep 30
fi

"${G[@]}" scp --zone "$ZONE" --tunnel-through-iap "$(dirname "$0")/hindsight.sh" "$VM":/tmp/hindsight.sh 2>/dev/null
"${SSH[@]}" --command "set -e
  sudo install -m 755 -o root /tmp/hindsight.sh /opt/cormon/deploy/hindsight.sh
  sudo CORMON_HOME=/opt/cormon bash /opt/cormon/deploy/hindsight.sh
  for i in \$(seq 12); do curl -fsS 127.0.0.1:8091/health >/dev/null && break; sleep 5; done
  systemctl is-active -q cormon && curl -fsS 127.0.0.1:8091/health >/dev/null && echo 'the bot is healthy'
  free -m | head -2" 2>/dev/null

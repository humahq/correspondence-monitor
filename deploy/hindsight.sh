#!/usr/bin/env bash
# Starts Hindsight (patient memory, issue #32) next to the bot, or updates it to the pinned version.
# The same on the live server (run as root, from setup_vm.sh) and on a laptop with Docker:
#     ./deploy/hindsight.sh
# On a laptop with Podman, POD=<name> puts it in that pod (the local chat demo's, where the bot runs too and the
# pod publishes 8888 on 127.0.0.1): CONTAINER=podman POD=cormon-chat ./deploy/hindsight.sh. Without POD nothing changes.
# It listens only on this machine (127.0.0.1:8888) and every call needs the key in secrets/hindsight_key.
# Its data is in the Docker volume hindsight-data and is kept across updates.
# The bot saves facts to it when memory.enabled is on (cormon/hindsight.py, plan step 3 in docs/hindsight-plan.md).
# It pulls facts out with OpenAI, using the key in .env and the bot's default model; with no key it stores
# messages as they are.
set -euo pipefail

ROOT="${CORMON_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
# Pinned by digest: a new version is a reviewed change to this line, never a silent `latest`.
IMAGE="ghcr.io/vectorize-io/hindsight:1.1.0@sha256:50311d02b4d5d2b7c5242bd336edf8735df8374eaad496a92f49c83b37d2aa06"
KEY_FILE="$ROOT/secrets/hindsight_key"
DOCKER=(docker)
[ "$(id -u)" = 0 ] || [ "$(uname)" = Darwin ] || DOCKER=(sudo docker)
[ "${CONTAINER:-}" = podman ] && DOCKER=(podman)   # CONTAINER=podman ./deploy/hindsight.sh, next to the local chat

PLACE=(-p 127.0.0.1:8888:8888)
[ -n "${POD:-}" ] && PLACE=(--pod "$POD")   # the pod already publishes the port

if [ "${DOCKER[0]}" = docker ] && ! command -v docker >/dev/null; then
  [ "$(uname)" = Linux ] || { echo "install Docker Desktop first" >&2; exit 1; }
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io >/dev/null
fi

mkdir -p "$ROOT/secrets"
if [ ! -s "$KEY_FILE" ]; then
  (umask 077; head -c 32 /dev/urandom | base64 | tr -d '/+=\n' > "$KEY_FILE")
fi
id cormon >/dev/null 2>&1 && chown cormon:cormon "$KEY_FILE"
chmod 600 "$KEY_FILE"

OPENAI_KEY=$(sed -n 's/^OPENAI_API_KEY=//p' "$ROOT/.env" 2>/dev/null | tr -d '"'"'" | head -1)
MODEL=$(sed -n 's/^    default: *\([^ #]*\).*/\1/p' "$ROOT/config/settings.yaml" | head -1)
if [ -n "$OPENAI_KEY" ]; then
  LLM=(-e HINDSIGHT_API_LLM_PROVIDER=openai -e HINDSIGHT_API_LLM_API_KEY="$OPENAI_KEY"
       -e HINDSIGHT_API_LLM_MODEL="${MODEL:-gpt-5.4-mini}" -e HINDSIGHT_API_LLM_REASONING_EFFORT=low)
else
  LLM=(-e HINDSIGHT_API_LLM_PROVIDER=none)
fi

"${DOCKER[@]}" pull -q "$IMAGE" >/dev/null
"${DOCKER[@]}" rm -f hindsight >/dev/null 2>&1 || true
# --memory: on the live server it may never take the bot's memory; it used 1 to 1.4 GB in the timing test.
# The re-ranker settings are the ones the timing test chose (docs/hindsight-plan.md, Results).
# Observations (its own AI summaries) are off, and it keeps no second copy of the message text: the bot's
# database has the original, and each fact names the message it came from. Its log of AI calls (llm_requests)
# would hold each message's text for a day, and so every hourly backup, so it is off too.
"${DOCKER[@]}" run -d --name hindsight --restart unless-stopped --memory 2g \
  "${PLACE[@]}" -v hindsight-data:/home/hindsight/.pg0 \
  "${LLM[@]}" \
  -e HINDSIGHT_API_ENABLE_OBSERVATIONS=false \
  -e HINDSIGHT_API_STORE_DOCUMENT_TEXT=false \
  -e HINDSIGHT_API_LLM_TRACE_ENABLED=false \
  -e HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension \
  -e HINDSIGHT_API_TENANT_API_KEY="$(cat "$KEY_FILE")" \
  -e HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100 \
  -e HINDSIGHT_API_RERANKER_LOCAL_BUCKET_BATCHING=true \
  "$IMAGE" >/dev/null

echo -n "waiting for Hindsight to start "
for _ in $(seq 120); do
  if curl -sf http://127.0.0.1:8888/health >/dev/null; then
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/v1/default/banks)
    [ "$code" = 401 ] || { echo; echo "Hindsight answered without the key (HTTP $code); stopping it" >&2;
                           "${DOCKER[@]}" rm -f hindsight >/dev/null; exit 1; }
    echo; echo "Hindsight is running on 127.0.0.1:8888 and refuses calls without the key"
    if [ -d /etc/systemd/system ] && [ "$(id -u)" = 0 ]; then
      # Hourly backups of its database (deploy/hindsight_backup.sh), run by root: the bot's user has no Docker.
      cp "$ROOT/deploy/cormon-hindsight-backup.service" "$ROOT/deploy/cormon-hindsight-backup.timer" /etc/systemd/system/
      systemctl daemon-reload
      systemctl enable --now cormon-hindsight-backup.timer >/dev/null 2>&1
      echo "hourly Hindsight backups are on (cormon-hindsight-backup.timer)"
    fi
    exit 0
  fi
  echo -n .; sleep 5
done
echo; "${DOCKER[@]}" logs --tail 30 hindsight >&2; exit 1

#!/usr/bin/env bash
# Runs on the temporary timing VM, not the live one. Made-up facts only, no AI key: Hindsight runs with
# LLM_PROVIDER=none, so facts are stored as written and only search and re-ranking are timed.
set -euo pipefail
cd "$(dirname "$0")"

command -v docker >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq docker.io >/dev/null; }
IMAGE=ghcr.io/vectorize-io/hindsight:latest
sudo docker pull -q "$IMAGE"
HS_KEY="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"
export HS_KEY

start() {
  sudo docker rm -f hs >/dev/null 2>&1 || true
  sudo docker run -d --name hs -p 127.0.0.1:8888:8888 -v hsdata:/home/hindsight/.pg0 \
    -e HINDSIGHT_API_LLM_PROVIDER=none \
    -e HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension \
    -e HINDSIGHT_API_TENANT_API_KEY="$HS_KEY" \
    "$@" "$IMAGE" >/dev/null
  for _ in $(seq 120); do curl -sf http://127.0.0.1:8888/health >/dev/null && return; sleep 5; done
  sudo docker logs --tail 50 hs; exit 1
}

if [ -n "${CHOSEN_ONLY:-}" ]; then
  # The chosen setup only, at 2,000 and then 10,000 facts.
  start -e HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100 -e HINDSIGHT_API_RERANKER_LOCAL_BUCKET_BATCHING=true
  python3 hindsight_timing.py load
  python3 hindsight_timing.py time "100 candidates, 2,000 facts"
  sudo docker stats --no-stream --format 'memory at 2,000 facts: {{.MemUsage}}' hs
  FIRST=1101 COUNT=400 python3 hindsight_timing.py load
  COUNT=500 python3 hindsight_timing.py time "100 candidates, 10,000 facts"
  sudo docker stats --no-stream --format 'memory at 10,000 facts: {{.MemUsage}}' hs
  free -m | head -3
  exit
fi

start
if [ -z "${SKIP_LOAD:-}" ]; then
  python3 hindsight_timing.py load
  sudo docker stats --no-stream --format 'memory after load: {{.MemUsage}}' hs
fi

python3 hindsight_timing.py time "default: local cross-encoder, 300 candidates"
sudo docker stats --no-stream --format 'memory after searches: {{.MemUsage}}' hs

start -e HINDSIGHT_API_RERANKER_MAX_CANDIDATES=100 -e HINDSIGHT_API_RERANKER_LOCAL_BUCKET_BATCHING=true
python3 hindsight_timing.py time "local cross-encoder, 100 candidates, bucket batching"

start -e HINDSIGHT_API_RERANKER_PROVIDER=flashrank
python3 hindsight_timing.py time "flashrank"

free -m | head -2
nproc

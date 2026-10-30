#!/usr/bin/env bash
# Puts a version of the bot's code on the live server, only if GitHub's gate passed for that exact commit
# (SDL-05, VAL-05). Run from the laptop; you log in with your own Google and GitHub accounts.
#
#   ./deploy/release.sh            the latest commit on GitHub's main
#   ./deploy/release.sh <commit>   a given commit (it must be on main and its gate must have passed)
#
# It stops, changing nothing, when: the gate did not pass for the commit; the server's packages differ from
# deploy/requirements-live.txt; the live code is not the version tagged `live` (someone changed it by hand);
# or the live study model differs from studies/model/model.sql (a rebuild the gate has not tested).
# Otherwise it backs up the code and the database, puts the commit's cormon/ in place, restarts the bot and
# checks its health; if the bot is not healthy it puts the old code back. Once healthy, the config the gate tested
# (triage, routing, templates, careful, studies/*/study.yaml, and the llm: and matching: parts of settings.yaml)
# is put in place from the commit (`cormon config release`, CFG-02); the rest of the server's settings stays.
set -euo pipefail

PROJECT="${CORMON_PROJECT:-optical-depth-506808-t0}"
ZONE="${CORMON_ZONE:-us-east1-b}"
VM="${CORMON_VM:-cormon-demo}"
SSH=(gcloud compute ssh "$VM" --project "$PROJECT" --zone "$ZONE" --tunnel-through-iap --quiet)
LIVE=/opt/cormon
cd "$(dirname "$0")/.."

remote() { "${SSH[@]}" --command "$1" 2>/dev/null </dev/null; }
stop() { echo "STOPPED: $*" >&2; exit 1; }

git fetch -q origin main --tags --force
SHA=$(git rev-parse "${1:-origin/main}^{commit}")
git merge-base --is-ancestor "$SHA" origin/main || stop "$SHA is not on GitHub's main"
echo "commit: $(git log -1 --format='%h %s' "$SHA")"

# 1. The gate passed for this exact commit.
RUN_ID=$(gh run list --workflow ci.yml --commit "$SHA" --limit 20 --json databaseId,event,status \
  --jq '[.[] | select(.event == "push" or .event == "workflow_dispatch") | select(.status == "completed")][0].databaseId // empty')
[ -n "$RUN_ID" ] || stop "no finished gate run on GitHub for $SHA"
GATE=$(gh run view "$RUN_ID" --json jobs --jq '.jobs[] | select(.name == "gate") | .conclusion')
[ "$GATE" = success ] || stop "the gate did not pass for $SHA (run $RUN_ID: $GATE)"
echo "gate passed: run $RUN_ID"

# 2. The server's packages are the ones the gate tested with.
diff <(git show "$SHA:deploy/requirements-live.txt" | sort) \
     <(remote "$LIVE/.venv/bin/python -m pip freeze --exclude-editable" | sort) >/dev/null \
  || stop "the server's packages differ from deploy/requirements-live.txt (a package change needs its own step)"

# 3. The live code is the version tagged live (skipped until the first release sets the tag).
FILES="find cormon -name '*.py' -not -path '*/__pycache__/*' | LC_ALL=C sort"
if git rev-parse -q --verify "refs/tags/live" >/dev/null; then
  tmp=$(mktemp -d)
  git archive live cormon | tar -x -C "$tmp"
  diff <(cd "$tmp" && eval "$FILES" | xargs shasum -a 256) \
       <(remote "cd $LIVE && sudo sh -c \"$FILES | xargs sha256sum\"") \
    || { rm -rf "$tmp"; stop "the live code is not the version tagged live (changed by hand?)"; }
  rm -rf "$tmp"
  echo "live code is $(git rev-parse --short live)"
  git --no-pager diff --stat live "$SHA" -- cormon
else
  echo "no live tag yet: first release through the gate"
fi

# 4. The live study model is the one in the repo.
diff -q <(git show "$SHA:studies/model/model.sql") <(remote "sudo sqlite3 'file:$LIVE/data/cormon.db?mode=ro' \
  '.dump documents doc_pages rules allowances guide rule_decisions'") >/dev/null \
  || stop "the live study model differs from studies/model/model.sql: export it, commit it and let the gate test it"
echo "live study model matches studies/model/model.sql"

read -r -p "Deploy $(git rev-parse --short "$SHA") to the live bot? Type yes: " ok || ok=""
[ "$ok" = yes ] || stop "not confirmed"

# 5. Back up, put the code in place, restart, check.
TS=$(date -u +%Y%m%d-%H%M%S)
git archive --format=tar.gz "$SHA" cormon config $(git ls-tree -r --name-only "$SHA" studies | grep '/study.yaml$') \
  | "${SSH[@]}" --command "set -e
  sudo tar -czf $LIVE/code-backup-$TS.tgz -C $LIVE cormon
  cd $LIVE && sudo -u cormon env CORMON_HOME=$LIVE $LIVE/.venv/bin/python -c 'import sys; from cormon.cli import main; main([\"backup\"])'
  rm -rf /tmp/release-$TS && mkdir /tmp/release-$TS && tar -xzf - -C /tmp/release-$TS
  sudo rsync -a --delete --exclude __pycache__ --chown=cormon:cormon /tmp/release-$TS/cormon/ $LIVE/cormon/
  sudo systemctl restart cormon; sleep 10
  if systemctl is-active -q cormon && curl -fsS 127.0.0.1:8091/health >/dev/null; then
    echo healthy
  else
    echo 'NOT HEALTHY: putting the old code back'
    sudo rm -rf $LIVE/cormon && sudo tar -xzf $LIVE/code-backup-$TS.tgz -C $LIVE && sudo systemctl restart cormon
    rm -rf /tmp/release-$TS
    exit 1
  fi
  chmod -R a+rX /tmp/release-$TS
  cd $LIVE && sudo -u cormon env CORMON_HOME=$LIVE $LIVE/.venv/bin/cormon config release /tmp/release-$TS $RUN_ID
  rm -rf /tmp/release-$TS
  sudo journalctl -u cormon --since '-1 min' --no-pager | grep -iE 'error|traceback' | head -5 || true" 2>/dev/null

git tag -f live "$SHA"
git push -q -f origin refs/tags/live
echo "deployed $(git rev-parse --short "$SHA"); tag live moved. Code backup: $LIVE/code-backup-$TS.tgz"

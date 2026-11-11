#!/usr/bin/env bash
# Puts a version of the bot's code on a server (the live one; CORMON_VM=cormon-edge for the test one), only if GitHub's gate passed for that exact commit
# (SDL-05, VAL-05). Run from the laptop; you log in with your own Google and GitHub accounts.
#
#   ./deploy/release.sh            the latest commit on GitHub's main
#   ./deploy/release.sh <commit>   a given commit (it must be on main and its gate must have passed)
#
# It stops, changing nothing, when: the gate did not pass for the commit; the newest fresh AI run (weekly, VAL-07)
# did not pass or is more than 8 days old; the server's packages differ from
# deploy/requirements-live.txt; the server's code is not the version tagged `live` (`live-<server>` for
# other servers; someone changed it by hand);
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
TAG=$([ "$VM" = cormon-demo ] && echo live || echo "live-$VM")   # each server's own record of what it runs
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

# 1b. Model drift (VAL-07): the newest fresh run (every AI set asked with no kept answers and compared with the
# one before) passed and is at most 8 days old, so a change on the model supplier's side has been tested.
# A fresh run leaves a "fresh-check" file; a weekly run is a fresh run even from before that file existed.
FRESH_LINE=$(
  gh api "repos/{owner}/{repo}/actions/artifacts?name=fresh-check&per_page=30" \
    --jq '.artifacts[] | select(.expired | not) | select(.workflow_run.head_branch == "main")
          | "\(.workflow_run.id) \(.created_at)"'
  gh run list --workflow ci.yml --event schedule --branch main --limit 1 --json databaseId,createdAt,status \
    --jq '.[] | select(.status == "completed") | "\(.databaseId) \(.createdAt)"'
) || true
FRESH_LINE=$(printf '%s\n' "$FRESH_LINE" | sort -n | tail -1)
FRESH_ID=${FRESH_LINE%% *}
FRESH_AT=${FRESH_LINE#* }
[ -n "${FRESH_ID:-}" ] || stop "no fresh AI run on main yet: wait for the weekly one or start it with: gh workflow run ci.yml -f fresh=true"
FRESH_GATE=$(gh run view "$FRESH_ID" --json jobs --jq '.jobs[] | select(.name == "gate") | .conclusion')
[ "$FRESH_GATE" = success ] || stop "the newest fresh AI run did not pass (run $FRESH_ID: ${FRESH_GATE:-not finished}): the model's answers changed or dropped; see that run"
FRESH_DAYS=$(python3 -c 'import sys, datetime as d; print((d.datetime.now(d.timezone.utc) - d.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))).days)' "$FRESH_AT")
[ "$FRESH_DAYS" -le 8 ] || stop "the newest fresh AI run is $FRESH_DAYS days old (run $FRESH_ID): start one with: gh workflow run ci.yml -f fresh=true"
echo "fresh AI run passed: run $FRESH_ID, $FRESH_DAYS days ago"

# 2. The server's packages are the ones the gate tested with.
diff <(git show "$SHA:deploy/requirements-live.txt" | sort) \
     <(remote "$LIVE/.venv/bin/python -m pip freeze --exclude-editable" | sort) >/dev/null \
  || stop "the server's packages differ from deploy/requirements-live.txt (a package change needs its own step)"

# 3. The live code is the version tagged $TAG (skipped until the first release sets the tag).
FILES="find cormon -name '*.py' -not -path '*/__pycache__/*' | LC_ALL=C sort"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  tmp=$(mktemp -d)
  git archive "$TAG" cormon | tar -x -C "$tmp"
  diff <(cd "$tmp" && eval "$FILES" | xargs shasum -a 256) \
       <(remote "cd $LIVE && sudo sh -c \"$FILES | xargs sha256sum\"") \
    || { rm -rf "$tmp"; stop "the code on $VM is not the version tagged $TAG (changed by hand?)"; }
  rm -rf "$tmp"
  echo "code on $VM is $(git rev-parse --short "$TAG")"
  git --no-pager diff --stat "$TAG" "$SHA" -- cormon
else
  echo "no $TAG tag yet: first release to $VM through the gate"
fi

# 4. The live study model is the one in the repo. Row order is not compared: import-model may store rows in another order.
diff -q <(git show "$SHA:studies/model/model.sql" | LC_ALL=C sort) <(remote "sudo sqlite3 'file:$LIVE/data/cormon.db?mode=ro' \
  '.dump documents doc_pages rules allowances guide rule_decisions'" | LC_ALL=C sort) >/dev/null \
  || stop "the live study model differs from studies/model/model.sql: export it, commit it and let the gate test it"
echo "live study model matches studies/model/model.sql"

read -r -p "Deploy $(git rev-parse --short "$SHA") to $VM? Type yes: " ok || ok=""
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

git tag -f "$TAG" "$SHA"
git push -q -f origin "refs/tags/$TAG"
echo "deployed $(git rev-parse --short "$SHA") to $VM; tag $TAG moved. Code backup: $LIVE/code-backup-$TS.tgz"

#!/usr/bin/env bash
# Backs up patient memory (Hindsight's own database, issue #32), every hour by cormon-hindsight-backup.timer as
# root: the bot's user cannot run Docker, which would make it root. Each copy is restored into a scratch database
# at once and its facts counted against the live ones, so a copy counts only when it is shown to restore.
#     deploy/hindsight_backup.sh                  make, check and keep a copy in data/backups/hindsight-<time>.dump
#     YES=1 deploy/hindsight_backup.sh restore F  put copy F back (Hindsight restarts; the bot goes on without memory)
# The bot's hourly backup (cormon/backup.py) copies the newest checked one off the server and tells the admins
# when none is fresh. The database password stays inside the container and is never printed.
set -euo pipefail

ROOT="${CORMON_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
DIR="$ROOT/data/backups"
KEEP="${KEEP:-24}"
DOCKER=(docker)
[ "$(id -u)" = 0 ] || [ "$(uname)" = Darwin ] || DOCKER=(sudo docker)
# Inside the container: its PostgreSQL tools, and the connection from the instance file pg0 writes.
PG='B=$(echo /home/hindsight/.pg0/installation/*/bin)
eval "$(/app/api/.venv/bin/python3 -c "import json, shlex
d = json.load(open(\"/home/hindsight/.pg0/instances/hindsight/instance.json\"))
print(\"export PGOPTIONS=\x27-c client_min_messages=warning\x27 PGHOST=127.0.0.1 PGPASSWORD=%s PGPORT=%s PGUSER=%s PGDATABASE=%s\" % tuple(
    shlex.quote(str(d[k])) for k in (\"password\", \"port\", \"username\", \"database\")))")"'
in_hindsight() { "${DOCKER[@]}" exec -i hindsight sh -c "$PG
$1"; }
facts() { in_hindsight "\$B/psql -At -d ${1:-\$PGDATABASE} -c 'select count(*) from memory_units'"; }

if [ "${1:-}" = restore ]; then
  file="${2:?which copy? deploy/hindsight_backup.sh restore data/backups/hindsight-<time>.dump}"
  [ "${YES:-}" = 1 ] || { echo "This replaces patient memory with $file. Run again with YES=1." >&2; exit 1; }
  in_hindsight '$B/pg_restore --clean --if-exists --single-transaction --no-owner -d $PGDATABASE' < "$file"
  "${DOCKER[@]}" restart hindsight >/dev/null
  for _ in $(seq 60); do curl -sf http://127.0.0.1:8888/health >/dev/null && break; sleep 5; done
  echo "restored $file: $(facts) facts"
  exit 0
fi

mkdir -p "$DIR"
id cormon >/dev/null 2>&1 && chown cormon:cormon "$DIR"   # the bot writes its own backups here too
stamp=$(date -u +%Y%m%d-%H%M%S)
out="$DIR/hindsight-$stamp.dump"
trap 'rm -f "$out.part"; in_hindsight "\$B/dropdb --if-exists restore_check" >/dev/null 2>&1 || true' EXIT
before=$(facts)
in_hindsight '$B/pg_dump -Fc' > "$out.part"
after=$(facts)
in_hindsight '$B/dropdb --if-exists restore_check && $B/createdb restore_check'
in_hindsight '$B/pg_restore --no-owner -d restore_check' < "$out.part"
restored=$(facts restore_check)
in_hindsight '$B/dropdb restore_check'
lo=$(( before < after ? before : after )); hi=$(( before > after ? before : after ))
ok=false; [ "$restored" -ge "$lo" ] && [ "$restored" -le "$hi" ] && ok=true
sha=$( (sha256sum "$out.part" 2>/dev/null || shasum -a 256 "$out.part") | cut -d' ' -f1)
mv "$out.part" "$out"
printf '{"file": "%s", "bytes": %s, "sha256": "%s", "facts": %s, "live_facts": %s, "ok": %s, "at": "%s"}\n' \
  "$(basename "$out")" "$(wc -c < "$out" | tr -d ' ')" "$sha" "$restored" "$after" "$ok" \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$out.json"
id cormon >/dev/null 2>&1 && chown cormon:cormon "$out" "$out.json"
# The oldest copies beyond KEEP go; the bot keeps its own off-server copies.
ls -1 "$DIR"/hindsight-*.dump | sort -r | tail -n +$((KEEP + 1)) | while read -r old; do rm -f "$old" "$old.json"; done
echo "patient memory backup $(basename "$out"): $restored facts restored in the check (live $after), ok=$ok"
[ "$ok" = true ]

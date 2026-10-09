#!/usr/bin/env bash
# Runs on the VM (as root, from create_vm.sh): installs or updates the app under /opt/cormon and (re)starts it.
# HOST is the public name for the item pages, e.g. 34-1-2-3.sslip.io.
set -euo pipefail

APP=/opt/cormon
HOST="${HOST:?HOST not set}"

if ! command -v caddy >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv sqlite3 caddy >/dev/null
fi
if [ ! -f /swapfile ]; then   # the e2-micro has 1 GB of memory
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id cormon >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin cormon
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q "$APP"
mkdir -p "$APP/data" "$APP/secrets"

# Cloud-only settings: pages are served by Caddy over HTTPS, the app itself listens on localhost only.
"$APP/.venv/bin/python" - "$APP/config/settings.yaml" "$HOST" <<'EOF'
import sys
from ruamel.yaml import YAML
path, host = sys.argv[1], sys.argv[2]
y = YAML()
cfg = y.load(open(path))
cfg["web"]["host"] = "127.0.0.1"
cfg["web"]["base_url"] = f"https://{host}"
y.dump(cfg, open(path, "w"))
EOF

# First install: an empty database holding the study model built on the laptop (rules, document pages).
if [ ! -f "$APP/data/cormon.db" ] && [ -f /tmp/cormon-seed.db ]; then
  chown -R cormon:cormon "$APP"
  sudo -u cormon CORMON_HOME="$APP" "$APP/.venv/bin/python" -c \
    "from pathlib import Path; from cormon import testbench; testbench._fresh_db(Path('/tmp/cormon-seed.db'))"
fi
rm -f /tmp/cormon-seed.db /tmp/cormon.tgz

chown -R cormon:cormon "$APP"
chmod 700 "$APP/secrets"
[ -f "$APP/.env" ] && chmod 600 "$APP/.env"

# `cormon ...` for anyone who logs in; their actions are logged under their own login name.
cat > /usr/local/bin/cormon <<'EOF'
#!/usr/bin/env bash
case "${CORMON_COPY:-live}" in
  sandbox) export CORMON_HOME=/opt/cormon/sandbox ;;   # the copy that `cormon test` runs in
  *)       export CORMON_HOME=/opt/cormon ;;
esac
export CORMON_OPERATOR="${CORMON_OPERATOR:-${SUDO_USER:-$USER}}"
cd /opt/cormon
exec sudo -u cormon --preserve-env=CORMON_HOME,CORMON_OPERATOR /opt/cormon/.venv/bin/cormon "$@"
EOF
chmod 755 /usr/local/bin/cormon

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    reverse_proxy 127.0.0.1:8091
}
EOF
systemctl reload-or-restart caddy

cp "$APP/deploy/cormon.service" /etc/systemd/system/cormon.service
systemctl daemon-reload
sudo -u cormon CORMON_HOME="$APP" "$APP/.venv/bin/cormon" init >/dev/null
systemctl enable cormon >/dev/null 2>&1
systemctl restart cormon
sleep 3
systemctl --no-pager status cormon | head -5

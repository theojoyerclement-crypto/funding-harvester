#!/usr/bin/env bash
# Installe le bot de paper trading sur un VPS Debian/Ubuntu.
# Usage (depuis le dossier du projet copié sur le serveur) :  sudo bash deploy/install.sh
set -euo pipefail

APP=/opt/funding-harvester
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync

id funding >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin funding
mkdir -p "$APP"
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude 'paper.db' \
      --exclude 'funding.db' --exclude '.env' "$SRC"/ "$APP"/

python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$APP/.env" ]; then
    cp "$APP/deploy/env.example" "$APP/.env"
    echo ">> Renseigner TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans $APP/.env"
fi
chmod 600 "$APP/.env"
chown -R funding:funding "$APP"

cp "$APP/deploy/funding-paper.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now funding-paper
sleep 3
systemctl --no-pager status funding-paper | head -15
echo ">> Logs en direct : journalctl -u funding-paper -f"

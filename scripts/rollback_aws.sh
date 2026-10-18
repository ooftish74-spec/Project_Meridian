#!/bin/bash
# Project Meridian — restore a production backup created by deploy_to_aws.sh.
# Restores code + config ONLY by default. State (results/) is restored only with --with-state,
# because live state has usually advanced since the backup was taken.
#
# Usage: scripts/rollback_aws.sh [backup_file] [--with-state]
set -euo pipefail

EC2_IP="54.116.149.149"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/meridian-key.pem"
REMOTE_DIR="/home/ubuntu/Project_Meridian"
REMOTE_BACKUP_DIR="/home/ubuntu/meridian_backups"
SSH=(ssh -i "$KEY_PATH" -o BatchMode=yes -o ConnectTimeout=15 "$EC2_USER@$EC2_IP")

BACKUP_FILE=""
WITH_STATE=0
for arg in "$@"; do
  case "$arg" in
    --with-state) WITH_STATE=1 ;;
    *) BACKUP_FILE="$arg" ;;
  esac
done
if [ -z "$BACKUP_FILE" ]; then
  BACKUP_FILE="$("${SSH[@]}" "ls -1t '$REMOTE_BACKUP_DIR'/deploy_*.tgz 2>/dev/null | head -1")"
fi
[ -n "$BACKUP_FILE" ] || { echo "❌ No backup found in $REMOTE_BACKUP_DIR"; exit 1; }

MEMBERS="src scripts config tests dashboard"
[ "$WITH_STATE" = "1" ] && MEMBERS="$MEMBERS results"

echo "⏪ Rolling back production from: $BACKUP_FILE  (members: $MEMBERS)"
"${SSH[@]}" "set -e; cd '$REMOTE_DIR'; \
  sudo systemctl stop meridian_live_trader_daemon.service || true; \
  tar tzf '$BACKUP_FILE' > /dev/null; \
  for m in $MEMBERS; do if tar tzf '$BACKUP_FILE' \"\$m/\" > /dev/null 2>&1; then rm -rf \"\$m\"; fi; done; \
  tar xzf '$BACKUP_FILE' $MEMBERS; \
  find . -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
  echo '{\"rolled_back_from\": \"$BACKUP_FILE\"}' > DEPLOYED_VERSION; \
  venv/bin/python3 -m compileall -q src scripts config > /dev/null; \
  sudo systemctl start meridian_live_trader_daemon.service; \
  systemctl is-active meridian_live_trader_daemon.service"
echo "✅ Rollback complete."

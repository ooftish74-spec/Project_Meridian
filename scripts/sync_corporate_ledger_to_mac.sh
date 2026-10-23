#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# scripts/sync_corporate_ledger_to_mac.sh
# Project Meridian — AWS EC2 -> Local MacBook Corporate Ledger Auto-Sync
#
# Execution Schedule:
#   - Every Sunday at 21:05 KST & 23:05 KST (and on MacBook wake/login via RunAtLoad)
#   - Generates the latest K-GAAP / Moving-Average Corporate Accounting Ledger
#     on AWS Production Server (54.116.149.149) and syncs all outputs (.xlsx, .csv, .md, .json)
#     directly to:
#       1) ~/Desktop/Meridian_Corporate_Ledger/
#       2) Project_Meridian/results/corporate_ledger/
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

EC2_IP="54.116.149.149"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/meridian-key.pem"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="/home/ubuntu/Project_Meridian"
LOCAL_LEDGER_DIR="$PROJECT_DIR/results/corporate_ledger"
DESKTOP_LEDGER_DIR="$HOME/Desktop/Meridian_Corporate_Ledger"

mkdir -p "$LOCAL_LEDGER_DIR" "$DESKTOP_LEDGER_DIR" "$PROJECT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 Starting Corporate Accounting Ledger generation on AWS ($EC2_IP)..."

# 1. Run export_corporate_ledger.py on AWS EC2 against Live KIS SSOT
ssh -i "$KEY_PATH" -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20 "$EC2_USER@$EC2_IP" \
  "cd '$REMOTE_DIR' && ./venv/bin/python3 scripts/export_corporate_ledger.py"

# 2. Sync generated ledger package from AWS EC2 to Local Project results/corporate_ledger/
rsync -az -e "ssh -i $KEY_PATH -o BatchMode=yes -o StrictHostKeyChecking=no" \
  "$EC2_USER@$EC2_IP:$REMOTE_DIR/results/corporate_ledger/" "$LOCAL_LEDGER_DIR/"

# 3. Copy all user-facing ledger files (.xlsx, .csv, .md) to MacBook Desktop folder
rsync -az --include='*.xlsx' --include='*.csv' --include='*.md' --exclude='*' \
  "$LOCAL_LEDGER_DIR/" "$DESKTOP_LEDGER_DIR/"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Corporate Ledger synced to MacBook:"
echo "   • Desktop Folder : $DESKTOP_LEDGER_DIR"
echo "   • Project Folder : $LOCAL_LEDGER_DIR"
ls -lh "$DESKTOP_LEDGER_DIR"

#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# scripts/sync_daily_brief_to_gdrive.sh
# Project Meridian — AWS EC2 -> Google Drive (NotebookLM Master DB) Auto-Sync
#
# Execution Schedule:
#   - Daily at 09:03 KST (and on MacBook wake/login via RunAtLoad)
#   - Pulls read-only Daily Brief outputs generated at 09:00 KST on AWS (54.116.149.149)
#   - Saves ONLY:
#       1) Meridian_Daily_Brief_Master_DB.pdf (Single Cumulative Master DB PDF)
#       2) Meridian_Daily_Brief_YYYY-MM-DD.mp3 (2-Host Audio Brief)
#     into the user's Google Drive for Desktop mount:
#       • My Drive/Project-A-NotebookLM/daily_brief/
#       • My Drive/Meridian_NotebookLM_Daily_Brief/
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

EC2_IP="54.116.149.149"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/meridian-key.pem"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_BRIEF_DIR="/home/ubuntu/Project_Meridian/results/daily_brief"
LOCAL_BRIEF_DIR="$PROJECT_DIR/results/daily_brief"

DRIVE_BASE="$HOME/Library/CloudStorage/GoogleDrive-ooftish74@gmail.com/My Drive"
DRIVE_NOTEBOOKLM_DIR="$DRIVE_BASE/Project-A-NotebookLM/daily_brief"
DRIVE_MERIDIAN_DIR="$DRIVE_BASE/Meridian_NotebookLM_Daily_Brief"

mkdir -p "$LOCAL_BRIEF_DIR" "$PROJECT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 Syncing Meridian Daily Brief Master DB (.pdf & .mp3) from AWS ($EC2_IP)..."

# 1. Pull generated daily brief files from AWS EC2 to local cache
rsync -az -e "ssh -i $KEY_PATH -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=20" \
  "$EC2_USER@$EC2_IP:$REMOTE_BRIEF_DIR/" "$LOCAL_BRIEF_DIR/"

# 2. Sync ONLY Meridian_Daily_Brief_Master_DB.pdf and .mp3 files to Google Drive
if [ -d "$DRIVE_BASE" ]; then
    mkdir -p "$DRIVE_NOTEBOOKLM_DIR" "$DRIVE_MERIDIAN_DIR"

    # Clean up individual dated PDFs, markdowns, txts, jsonls, and *_Latest duplicates from Google Drive
    rm -f "$DRIVE_NOTEBOOKLM_DIR"/*.md "$DRIVE_NOTEBOOKLM_DIR"/*.txt "$DRIVE_NOTEBOOKLM_DIR"/*.jsonl \
          "$DRIVE_NOTEBOOKLM_DIR"/*_Latest.* "$DRIVE_NOTEBOOKLM_DIR"/Meridian_Daily_Brief_20*.pdf
    rm -f "$DRIVE_MERIDIAN_DIR"/*.md "$DRIVE_MERIDIAN_DIR"/*.txt "$DRIVE_MERIDIAN_DIR"/*.jsonl \
          "$DRIVE_MERIDIAN_DIR"/*_Latest.* "$DRIVE_MERIDIAN_DIR"/Meridian_Daily_Brief_20*.pdf

    rsync -az --update \
      --exclude='*_Latest.*' \
      --include='Meridian_Daily_Brief_Master_DB.pdf' \
      --include='*.mp3' \
      --exclude='*' \
      "$LOCAL_BRIEF_DIR/" "$DRIVE_NOTEBOOKLM_DIR/"

    rsync -az --update \
      --exclude='*_Latest.*' \
      --include='Meridian_Daily_Brief_Master_DB.pdf' \
      --include='*.mp3' \
      --exclude='*' \
      "$LOCAL_BRIEF_DIR/" "$DRIVE_MERIDIAN_DIR/"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ Synced Master DB PDF & Audio MP3 to Google Drive:"
    echo "   • $DRIVE_NOTEBOOKLM_DIR"
    echo "   • $DRIVE_MERIDIAN_DIR"
    ls -lh "$DRIVE_MERIDIAN_DIR"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ Google Drive mount not found at $DRIVE_BASE"
fi

#!/bin/bash
set -euo pipefail

EC2_IP="54.116.149.149"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/meridian-key.pem"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="/home/ubuntu/Project_Meridian"

echo "=================================================="
echo "🔄 Project Meridian AWS -> MacBook Data Syncing 🔄"
echo "=================================================="
echo ""

if [ ! -f "$KEY_PATH" ]; then
    echo "❌ SSH 키를 찾을 수 없습니다: $KEY_PATH"
    exit 1
fi

echo "[1/2] Syncing SSOT Results & Portfolios from AWS..."
rsync -avz -e "ssh -i $KEY_PATH -o StrictHostKeyChecking=no" \
    "$EC2_USER@$EC2_IP:$REMOTE_DIR/results/" "$PROJECT_DIR/results/"

echo ""
echo "[2/2] Syncing Realtime Market & Signal Data from AWS..."
rsync -avz -e "ssh -i $KEY_PATH -o StrictHostKeyChecking=no" \
    --include="kr_markets/***"\
    --include="signals/***"\
    --include="raw/***"\
    --include="sentiment/***"\
    "$EC2_USER@$EC2_IP:$REMOTE_DIR/data/" "$PROJECT_DIR/data/"

echo ""
echo "✅ AWS -> MacBook 동기화 완료 (100%)."
echo "=================================================="

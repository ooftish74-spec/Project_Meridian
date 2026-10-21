#!/bin/bash
# ══════════════════════════════════════════════════════════════════════
# Project Meridian — AWS deployment (Wave 0 rewrite, 2026-10-19)
#
# Previous version problems (fixed here):
#   • Backup / dependency / health-check steps were `echo` only (no real backup existed on EC2).
#   • Pushed local results/*.json (position ledger, night futures, ...) over production state.
#   • Pushed local config/.kis_token* over production broker tokens.
#   • Printed a hardcoded NAV as "health check".
#
# Usage:
#   scripts/deploy_to_aws.sh --dry-run            # show what would change, touch nothing
#   scripts/deploy_to_aws.sh                      # gated deploy (pytest must be green)
#   scripts/deploy_to_aws.sh --reinstall-systemd  # also re-run install_systemd_services.sh
# Rollback:
#   scripts/rollback_aws.sh [backup_file]         # defaults to latest backup
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail

EC2_IP="54.116.149.149"
EC2_USER="ubuntu"
KEY_PATH="$HOME/.ssh/meridian-key.pem"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_DIR="/home/ubuntu/Project_Meridian"
REMOTE_BACKUP_DIR="/home/ubuntu/meridian_backups"   # outside REMOTE_DIR: rsync can never touch it
KEEP_BACKUPS=5
SSH=(ssh -i "$KEY_PATH" -o BatchMode=yes -o ConnectTimeout=15 "$EC2_USER@$EC2_IP")

DRY_RUN=0
REINSTALL_SYSTEMD=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --reinstall-systemd) REINSTALL_SYSTEMD=1 ;;
    *) echo "Unknown argument: $arg"; exit 2 ;;
  esac
done

# Code-only payload. State (results/, data/, logs/) and secrets (tokens, .env, *.pem) are NEVER pushed.
RSYNC_EXCLUDES=(
  --exclude '.git' --exclude '.env' --exclude '.env.*' --exclude '*.pem' --exclude '*.key'
  --exclude 'config/.kis_token*' --exclude '*.lock'
  --exclude '/results/' --exclude '/data/' --exclude '/logs/' --exclude '/backups/'
  --exclude '/venv' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache'
  --exclude 'catboost_info' --exclude '/reports/' --exclude '/scratch/' --exclude '/.archive/'
  --exclude '.DS_Store' --exclude '*.pdf' --exclude '*.parquet' --exclude '/models/'
  --exclude '.ruff_cache' --exclude '.mypy_cache' --exclude '/docs/' --exclude 'ec2-trust-policy.json'
  --exclude '/models_candidate/' --exclude '*.joblib' --exclude '*.pkl'
)

GIT_SHA="$(git -C "$PROJECT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_DIRTY="$(git -C "$PROJECT_DIR" status --porcelain -- src scripts config tests dashboard 2>/dev/null | { grep -v 'kis_token' || true; } | wc -l | tr -d ' ')"
TS="$(date +%Y%m%d_%H%M%S)"

echo "=================================================="
echo "🚀 Project Meridian AWS Deployment  (commit $GIT_SHA, dirty files: $GIT_DIRTY)"
echo "=================================================="

[ -f "$KEY_PATH" ] || { echo "❌ SSH key not found: $KEY_PATH"; exit 1; }

# ── [1] Dry run: itemized diff only ───────────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
  echo "[DRY-RUN] Files that would be uploaded (code only):"
  rsync -azn --itemize-changes "${RSYNC_EXCLUDES[@]}" -e "ssh -i $KEY_PATH -o BatchMode=yes" \
    "$PROJECT_DIR/" "$EC2_USER@$EC2_IP:$REMOTE_DIR/" | grep -E '^[<>c]f' || echo "  (no changes)"
  exit 0
fi

# ── [2] Gate: local pytest must be green (Standing Rule) ──────────────
echo "[1/6] Local test gate (pytest)..."
# Deterministic money-path gate (offline, no env dependency). Full suite: TEST_SCOPE=full.
GATE_TESTS=(tests/test_wave0_invariants.py tests/test_redteam_ssot_integrity.py
            "tests/test_intraday_capital_router.py::TestIntradayCapitalRouter::test_resolve_signal_conflicts_same_ticker"
            "tests/test_live_execution_guards.py::test_holdings_guard_allows_buy_and_blocks_unheld_sell")
[ "${TEST_SCOPE:-gate}" = "full" ] && GATE_TESTS=(tests/)
if [ "${SKIP_TESTS:-0}" = "1" ]; then
  echo "⚠️  SKIP_TESTS=1 — test gate bypassed explicitly."
else
  (cd "$PROJECT_DIR" && "$PROJECT_DIR/venv/bin/python3" -m pytest "${GATE_TESTS[@]}" -q --no-header -p no:cacheprovider \
      -o addopts="" --timeout=120) \
    || { echo "❌ pytest failed — deployment aborted."; exit 1; }
fi
if [ "$GIT_DIRTY" != "0" ]; then
  echo "⚠️  Working tree has $GIT_DIRTY uncommitted code files. Commit before deploying for traceability."
  [ "${ALLOW_DIRTY:-0}" = "1" ] || { echo "❌ Aborted (set ALLOW_DIRTY=1 to override)."; exit 1; }
fi

# ── [3] Real remote backup (code + config + state) ────────────────────
echo "[2/6] Creating remote backup..."
BACKUP_FILE="$REMOTE_BACKUP_DIR/deploy_${TS}_pre_${GIT_SHA}.tgz"
"${SSH[@]}" "set -e; mkdir -p '$REMOTE_BACKUP_DIR'; cd '$REMOTE_DIR'; \
  tar --warning=no-file-changed -czf '$BACKUP_FILE' --exclude='__pycache__' --exclude='*.lock' --exclude='results/models/ensemble_v_*.pkl' src scripts config tests dashboard results \
    \$(ls -d requirements.txt pytest.ini Makefile DEPLOYED_VERSION 2>/dev/null) || [ \$? -eq 1 ]; \
  tar tzf '$BACKUP_FILE' > /dev/null; \
  ls -1t '$REMOTE_BACKUP_DIR'/deploy_*.tgz | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f; \
  echo \"  backup: \$(du -h '$BACKUP_FILE' | cut -f1) $BACKUP_FILE\""

# ── [4] Stop daemon to avoid mixed-version imports mid-run ────────────
echo "[3/6] Stopping live trader daemon..."
"${SSH[@]}" "sudo systemctl stop meridian_live_trader_daemon.service || true"

# ── [5] Upload code only ──────────────────────────────────────────────
echo "[4/6] Uploading code..."
RSYNC_LOG="$(mktemp)"
if ! rsync -az --itemize-changes "${RSYNC_EXCLUDES[@]}" -e "ssh -i $KEY_PATH -o BatchMode=yes" \
  "$PROJECT_DIR/" "$EC2_USER@$EC2_IP:$REMOTE_DIR/" > "$RSYNC_LOG"; then
  echo "❌ Upload failed — rolling back to $BACKUP_FILE"
  "$PROJECT_DIR/scripts/rollback_aws.sh" "$BACKUP_FILE"
  exit 1
fi
grep -E '^[<>c]f' "$RSYNC_LOG" || true
"${SSH[@]}" "cd '$REMOTE_DIR' && find . -name '__pycache__' -type d -prune -exec rm -rf {} + ; \
  echo '{\"commit\": \"$GIT_SHA\", \"deployed_at\": \"$TS\", \"backup\": \"$BACKUP_FILE\"}' > DEPLOYED_VERSION"

# ── [6] Remote verification: compile + import smoke; auto-rollback on failure ──
echo "[5/6] Remote compile & import verification..."
if ! "${SSH[@]}" "cd '$REMOTE_DIR' && venv/bin/python3 -m compileall -q src scripts config > /dev/null && \
     venv/bin/python3 -c 'import sys; sys.path.insert(0, \".\"); \
import src.portfolio.shadow_manager, src.execution._kis_adapter, src.execution.execution_engine, \
src.execution.intraday_capital_router, src.risk.kill_switch, src.risk.universal_exit_engine, \
src.streams.s5_overnight.overnight_stream, src.data_collection.central_data_gateway, \
src.data_collection.realtime_data_bus, src.measurement.measurement_engine; print(\"  imports OK\")'"; then
  echo "❌ Remote verification failed — rolling back to $BACKUP_FILE"
  "$PROJECT_DIR/scripts/rollback_aws.sh" "$BACKUP_FILE"
  exit 1
fi

if [ "$REINSTALL_SYSTEMD" = "1" ]; then
  "${SSH[@]}" "sudo chown -R ubuntu:ubuntu '$REMOTE_DIR' && bash '$REMOTE_DIR/scripts/install_systemd_services.sh'"
fi
"${SSH[@]}" "sudo systemctl start meridian_live_trader_daemon.service && (sudo systemctl try-restart meridian_telegram.service || true)"

# ── [7] Real health check (reads production state; prints nothing hardcoded) ──
echo "[6/6] Health check..."
"${SSH[@]}" "cd '$REMOTE_DIR' && sleep 5 && systemctl is-active meridian_live_trader_daemon.service && \
  venv/bin/python3 - <<'PY'
import json
k = json.load(open('results/kis_portfolio.json'))
s = json.load(open('results/shadow_portfolio.json'))
print(f\"  broker NAV={k.get('nav')}  holdings={len(k.get('holdings') or {})}  updated={k.get('updated_at') or k.get('timestamp')}\")
print(f\"  shadow positions={len(s.get('positions') or {})}  trades={len(s.get('trade_history') or [])}\")
sc = json.load(open('results/signal_cache.json'))
print(f\"  signal_cache usdkrw={sc.get('usdkrw')}  vix={sc.get('vix')}\")
print('  DEPLOYED_VERSION', open('DEPLOYED_VERSION').read().strip())
PY"

echo "=================================================="
echo "✅ Deployed commit $GIT_SHA. Backup: $BACKUP_FILE"
echo "   Rollback: scripts/rollback_aws.sh $BACKUP_FILE"
echo "=================================================="

#!/bin/bash
set -euo pipefail

# Remove legacy Systemd Timers that overlap with Airflow DAG

echo "=============================================="
echo "🧹 Removing legacy Systemd Timers..."
echo "=============================================="

# List of timers to disable (we keep the daemons like intraday, telegram, night_monitor)
TIMERS=(
    "meridian_night_futures.timer"
    "meridian_collect.timer"
    "meridian_morning.timer"
    "meridian_premarket_calibration.timer"
    "meridian_closing.timer"
    "meridian_aftermarket.timer"
    "meridian_evening.timer"
)

for timer in "${TIMERS[@]}"; do
    if sudo systemctl is-active --quiet "$timer"; then
        echo "Stopping and disabling $timer..."
        sudo systemctl stop "$timer" || true
        sudo systemctl disable "$timer" || true
    else
        echo "$timer is not active."
    fi
done

sudo systemctl daemon-reload
echo "✅ Legacy timers disabled successfully."

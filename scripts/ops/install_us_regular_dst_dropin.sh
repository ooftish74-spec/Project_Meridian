#!/bin/bash
# O2 (DST) — install only the extra 23:25 KST trigger for meridian_us_regular.timer as a systemd drop-in.
# Avoids a full install_systemd_services.sh re-run (which would rewrite every unit) on production.
# Idempotent. Run on the production host: bash scripts/ops/install_us_regular_dst_dropin.sh
set -euo pipefail
DROPIN_DIR=/etc/systemd/system/meridian_us_regular.timer.d
sudo mkdir -p "$DROPIN_DIR"
cat <<'EOF' | sudo tee "$DROPIN_DIR/10-dst-winter.conf" > /dev/null
# Wave 0 O2: EST(겨울) 개장 09:30 ET = 23:30 KST. 세션 판정은 코드가 뉴욕 시각으로 수행.
[Timer]
OnCalendar=*-*-* 23:25:00 Asia/Seoul
# 새 OnCalendar 추가 시 Persistent=true면 '어젯밤 23:25 누락'으로 간주해 restart 즉시 발화 → 비계획 실행 방지
Persistent=false
EOF
sudo systemctl daemon-reload
sudo systemctl restart meridian_us_regular.timer
systemctl list-timers --all --no-pager | grep meridian_us_regular || true
systemctl cat meridian_us_regular.timer | grep OnCalendar

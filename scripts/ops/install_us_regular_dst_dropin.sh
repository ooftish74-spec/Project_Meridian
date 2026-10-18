#!/bin/bash
# O2 (DST) — install only the extra 23:25 KST trigger for meridian_us_regular.timer as a systemd drop-in.
# Avoids a full install_systemd_services.sh re-run (which would rewrite every unit) on production.
# Idempotent. Run on the production host: bash scripts/ops/install_us_regular_dst_dropin.sh
#
# Track B (Wave 1):
#  - install_systemd_services.sh 가 23:25 를 본 unit 에 포함(Persistent=false)하고 이 드롭인을 제거한다.
#    본 unit 에 이미 23:25 가 있으면 이 스크립트는 아무것도 하지 않는다 (OnCalendar 중복 방지).
#  - 드롭인 내용이 이미 동일하면 reload 도 하지 않는다 (재실행 무해).
#  - `systemctl restart` 제거: 2026-10-19 03:27 KST 운영에서 드롭인 추가 + restart 가 Persistent=false 였음에도
#    서비스를 즉시 1회 실행시켰다. 새 OnCalendar 는 daemon-reload 로 반영되지만, 지나간 시각 기준 elapse 가
#    발생할 수 있으므로 코드 가드가 no-op 인 시간대(주말 주간 KST 등)에 적용하고 list-timers/journal 로 확인할 것.
set -euo pipefail
UNIT=/etc/systemd/system/meridian_us_regular.timer
DROPIN_DIR=/etc/systemd/system/meridian_us_regular.timer.d
DROPIN="$DROPIN_DIR/10-dst-winter.conf"

if grep -q '^OnCalendar=\*-\*-\* 23:25:00 Asia/Seoul' "$UNIT" 2>/dev/null; then
    echo "base unit already contains the 23:25 KST trigger (installer-managed) → nothing to do"
    systemctl cat meridian_us_regular.timer | grep OnCalendar
    exit 0
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
cat <<'EOF' > "$TMP"
# Wave 0 O2: EST(겨울) 개장 09:30 ET = 23:30 KST. 세션 판정은 코드가 뉴욕 시각으로 수행.
[Timer]
OnCalendar=*-*-* 23:25:00 Asia/Seoul
# 새 OnCalendar 추가 시 Persistent=true면 '어젯밤 23:25 누락'으로 간주해 restart 즉시 발화 → 비계획 실행 방지
Persistent=false
EOF

if sudo test -f "$DROPIN" && sudo cmp -s "$TMP" "$DROPIN"; then
    echo "drop-in already up to date → no reload"
else
    sudo mkdir -p "$DROPIN_DIR"
    sudo install -m 0644 "$TMP" "$DROPIN"
    sudo systemctl daemon-reload
fi
systemctl list-timers --all --no-pager | grep meridian_us_regular || true
systemctl cat meridian_us_regular.timer | grep OnCalendar

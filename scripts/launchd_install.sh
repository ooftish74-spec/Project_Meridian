#!/bin/bash
# ═══════════════════════════════════════════════════
# Project Meridian — launchd 설치/제거 스크립트
# ═══════════════════════════════════════════════════
#
# Usage:
#   ./scripts/launchd_install.sh install   — 모든 plist 설치
#   ./scripts/launchd_install.sh uninstall — 모든 plist 제거
#   ./scripts/launchd_install.sh status    — 현재 상태 확인

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PLIST_DIR="${PROJECT_ROOT}/config/launchd"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"

ACTION="${1:-status}"

PLISTS=(
    "com.meridian.overnight"
    "com.meridian.data-hub"
    "com.meridian.alpha-factory"
    "com.meridian.collect"
    "com.meridian.collect-dart"
    "com.meridian.collect-flow"
    "com.meridian.premarket"
    "com.meridian.morning"
    "com.meridian.morning-ml"
    "com.meridian.market"
    "com.meridian.intraday"
    "com.meridian.closing"
    "com.meridian.post-close"
    "com.meridian.aftermarket"
    "com.meridian.evening"
    "com.meridian.evening-data"
    "com.meridian.us-market"
    "com.meridian.us-mid"
    "com.meridian.krx-refresh"
    "com.meridian.weekly-retrain"
    "com.meridian.weekly-validate"
    "com.meridian.crypto-arb"
    "com.project.meridian.daily_report"
    "com.project.meridian.weekly_report"
)

case "$ACTION" in
    install)
        echo "═══ Project Meridian — launchd 설치 ═══"
        mkdir -p "$LAUNCH_AGENTS_DIR"

        for label in "${PLISTS[@]}"; do
            src="${PLIST_DIR}/${label}.plist"
            dst="${LAUNCH_AGENTS_DIR}/${label}.plist"

            if [ ! -f "$src" ]; then
                echo "  ⚠️  ${label}.plist 없음 — 건너뜀"
                continue
            fi

            # 기존 제거
            launchctl unload "$dst" 2>/dev/null || true
            rm -f "$dst"

            # 복사 + 로드
            cp "$src" "$dst"
            launchctl load "$dst"
            echo "  ✅ ${label} 설치 완료"
        done

        echo ""
        echo "═══ 설치 완료: ${#PLISTS[@]}개 plist ═══"
        echo "  상태 확인: $0 status"
        ;;

    uninstall)
        echo "═══ Project Meridian — launchd 제거 ═══"

        for label in "${PLISTS[@]}"; do
            dst="${LAUNCH_AGENTS_DIR}/${label}.plist"

            launchctl unload "$dst" 2>/dev/null || true
            rm -f "$dst"
            echo "  🗑️  ${label} 제거 완료"
        done

        echo ""
        echo "═══ 제거 완료 ═══"
        ;;

    status)
        echo "═══ Project Meridian — launchd 상태 ═══"
        echo ""

        for label in "${PLISTS[@]}"; do
            dst="${LAUNCH_AGENTS_DIR}/${label}.plist"

            if [ -f "$dst" ]; then
                status=$(launchctl list | grep "$label" 2>/dev/null || echo "not running")
                if echo "$status" | grep -q "$label"; then
                    echo "  ✅ ${label}: LOADED"
                else
                    echo "  ⚠️  ${label}: installed but NOT loaded"
                fi
            else
                echo "  ❌ ${label}: NOT installed"
            fi
        done

        echo ""
        echo "═══ 스케줄 요약 ═══"
        echo "  04:00  data-hub     — 거시경제/대체데이터 파케이 수집"
        echo "  04:15  alpha-factory— 기호적 회귀 및 딥러닝 연산/시그널 발행"
        echo "  05:15  overnight    — 야간 인텔리전스 (Alpha 시그널 수신)"
        echo "  06:30  collect      — 기존 데이터 수집"
        echo "  07:45  premarket    — 프리마켓 분석"
        echo "  08:00  morning      — S1 갭 트레이딩 시작"
        echo "  09:05  market       — 전체 오케스트레이터"
        echo "  15:10  closing      — 강제 청산"
        echo "  15:35  aftermarket  — 성과 측정 + 학습"
        echo "  20:00  evening      — 야간 보고서 + S4"
        echo "  20:30  daily_report — 일간 성과 분석 및 이메일 발송"
        echo "  Sat 07:00 weekly_report — 주간 알파 성과/발굴 종합 이메일 발송"
        ;;

    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac

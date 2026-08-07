#!/bin/bash
# ═══════════════════════════════════════════════════
# Project Meridian — Daily Pipeline Runner
# ═══════════════════════════════════════════════════
# launchd에 의해 호출되며, daily_pipeline.py를 실행합니다.
#
# Usage:
#   ./scripts/run_pipeline.sh [phase]
#
# Phases (전체 목록):
#   overnight       — 05:15  야간 인텔리전스 수집
#   collect         — 06:00  데이터 수집 (pykrx, fdr, KIS)
#   premarket       — 07:45  프리마켓 분석 + 레짐 판정
#   morning         — 08:00  Intelligence Cascade + 매매 신호
#   market          — 09:05  매매 실행
#   intraday        — 09:30  장중 실시간 수집/모니터링
#   closing         — 15:10  포지션 청산 + PnL 계산
#   aftermarket     — 15:35  애프터마켓 분석 + Shadow 확정
#   krx_refresh     — 16:10  KRX 확정 데이터 리프레시
#   collect_flow    — 16:30  투자자 수급 수집
#   evening_data    — 17:00  US 가격 + 저녁 데이터 수집
#   collect_dart    — 19:00  DART 공시 수집
#   evening         — 20:00  자가학습 + 리포트 + Advisory
#   us_market       — 22:35  미국 시장 트레이딩 (레거시 — us_premarket/us_regular로 대체)
#   us_premarket    — 17:30 KST(DST)/18:30 KST(표준시)  [Phase 41] S6-B 프리마켓
#   us_regular      — 22:30 KST(DST)/23:30 KST(표준시)  [Phase 41] S5+S6-B 본장
#   weekly_retrain  — 토 02:00  주간 ML 재학습
#   weekly_validate — 토 03:00  주간 검증

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV="${PROJECT_ROOT}/venv"
LOG_DIR="${PROJECT_ROOT}/logs"
DATE=$(date +%Y%m%d)
PHASE="${1:-all}"

# 로그 디렉토리 생성
mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_DIR}/pipeline_${DATE}_${PHASE}.log"

# 가상환경 활성화 (실제 venv만 사용, symlink 제외)
# ★ venv가 Project-A symlink인 경우 numpy 1.x 호환 문제 발생
#    → system Python (numpy 2.x)을 사용하여 joblib 모델 로드 보장
if [ -d "$VENV" ] && [ ! -L "$VENV" ]; then
    source "${VENV}/bin/activate"
fi

# PYTHONPATH 설정
export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

# .env 로드
if [ -f "${PROJECT_ROOT}/.env" ]; then
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

echo "═══════════════════════════════════════" | tee -a "$LOG_FILE"
echo " Project Meridian — ${PHASE} (${DATE})" | tee -a "$LOG_FILE"
echo " Started: $(date '+%Y-%m-%d %H:%M:%S')" | tee -a "$LOG_FILE"
echo "═══════════════════════════════════════" | tee -a "$LOG_FILE"

cd "$PROJECT_ROOT"

# daily_pipeline.py 실행
python3 scripts/daily_pipeline.py "$PHASE" 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

echo "" | tee -a "$LOG_FILE"
echo " Finished: $(date '+%Y-%m-%d %H:%M:%S') (exit=$EXIT_CODE)" | tee -a "$LOG_FILE"
echo "═══════════════════════════════════════" | tee -a "$LOG_FILE"

exit $EXIT_CODE

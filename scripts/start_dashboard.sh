#!/bin/bash
# Project Meridian 대시보드 서버 시작 스크립트
# launchd에서 호출됨 — 맥 로그인 시 자동 실행

PROJECT_DIR="/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian"
VENV_PYTHON="${PROJECT_DIR}/venv/bin/python3"
STREAMLIT_CMD="${PROJECT_DIR}/venv/bin/streamlit"
LOG_DIR="${PROJECT_DIR}/logs"

mkdir -p "$LOG_DIR"

cd "$PROJECT_DIR"

# 기존 대시보드 프로세스 정리
pkill -f "streamlit run dashboard/app.py" 2>/dev/null || true
sleep 1

# 대시보드 서버 실행 (포트 8501, 외부 접속 허용)
exec "$STREAMLIT_CMD" run dashboard/app.py --server.address=0.0.0.0 --server.port=8501 --server.headless=true

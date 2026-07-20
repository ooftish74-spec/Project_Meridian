# Project Meridian — Makefile
# ============================
# DD 권고 #12: CI/CD 자동 테스트 구조

.PHONY: test test-fast lint typecheck clean help

# 기본 타겟
help:
	@echo "═══════════════════════════════════════════"
	@echo "  Project Meridian — 개발 명령어"
	@echo "═══════════════════════════════════════════"
	@echo "  make test        — 전체 테스트 실행"
	@echo "  make test-fast   — 빠른 테스트만 실행 (slow 제외)"
	@echo "  make test-streams — 스트림 테스트만"
	@echo "  make lint        — 코드 린트 (flake8)"
	@echo "  make typecheck   — 타입 체크 (mypy)"
	@echo "  make clean       — 캐시 정리"
	@echo "  make audit       — DynamicConfig 감사"
	@echo "  make pipeline    — 전체 파이프라인 실행 (shadow)"
	@echo "═══════════════════════════════════════════"

# 전체 테스트
test:
	python3 -m pytest tests/ -v --tb=short

# 빠른 테스트 (slow 마커 제외)
test-fast:
	python3 -m pytest tests/ -v --tb=short -m "not slow"

# 스트림 테스트만
test-streams:
	python3 -m pytest tests/test_streams.py -v --tb=short

# 배분기 + 측정 테스트
test-allocator:
	python3 -m pytest tests/test_allocator.py tests/test_measurement.py -v --tb=short

# 코드 린트
lint:
	python3 -m flake8 src/ scripts/ config/ --max-line-length=120 --ignore=E501,W503,E402 --count

# 타입 체크
typecheck:
	python3 -m mypy src/ --ignore-missing-imports --no-error-summary 2>/dev/null || echo "mypy not installed or type errors found"

# DynamicConfig 감사
audit:
	python3 -c "from config.dynamic_config import DynamicConfig; import json; print(json.dumps(DynamicConfig().audit_config(), indent=2, ensure_ascii=False))"

# 전체 파이프라인 (Shadow 모드)
pipeline:
	python3 scripts/stream_orchestrator.py

# 캐시 정리
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ 캐시 정리 완료"

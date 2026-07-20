#!/usr/bin/env python3
"""
Dynamic Optimizer v1 — Optuna 기반 전사적 매개변수 베이지안 최적화
===================================================================

[개요]
목표: 세후 CAGR 20% 이상 달성 및 MDD 최소화
방법: 
  - DynamicConfig 객체에 override 값을 동적으로 주입.
  - V6 이벤트 백테스터(run_event_backtest)를 반복 실행하여 포트폴리오 성과 측정.
  - 최적화된 파라미터를 도출하여 라이브 환경에 동적으로 반영 가능하도록 함.

Usage:
    python3 src/learning/dynamic_optimizer.py --trials 100 --start 2024-01-01 --end 2026-07-01
"""

import argparse
import logging
import json
from pathlib import Path
import sys

import optuna

# 프로젝트 루트 경로 추가
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
from scripts.run_event_backtest import run_event_backtest

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def objective(trial, start_date, end_date):
    cfg = DynamicConfig()
    
    # ── 1. Search Space (탐색 공간) 설정 ──────────────────────────
    
    # S0 Beta Z-Score & Kelly 관련 파라미터
    s0_max_conf = trial.suggest_float('s0_beta.max_confidence_threshold', 0.80, 0.95, step=0.01)
    s0_zscore_thresh = trial.suggest_float('s0_beta.zscore_threshold', 1.0, 2.5, step=0.1)
    s0_max_sweep = trial.suggest_float('s0_beta.max_sweep_ratio', 0.20, 0.60, step=0.05)
    
    # Alpha Allocator 파라미터 (리스크 패리티 블렌드)
    rp_blend = trial.suggest_float('allocator.risk_parity_blend', 0.2, 0.8, step=0.1)
    sharpe_alpha = trial.suggest_float('allocator.sharpe_alpha', 0.05, 0.30, step=0.05)
    
    # 레짐별 현금(S5/S6-A) 기본 보존 비율
    bear_s5 = trial.suggest_float('allocator.base_weight.bear.S5', 0.10, 0.40, step=0.05)
    crash_s5 = trial.suggest_float('allocator.base_weight.crash.S5', 0.15, 0.60, step=0.05)

    # ── 2. 설정 Override 적용 ──────────────────────────────────────
    cfg.set('s0_beta.max_confidence_threshold', s0_max_conf)
    cfg.set('s0_beta.zscore_threshold', s0_zscore_thresh)
    cfg.set('s0_beta.max_sweep_ratio', s0_max_sweep)
    cfg.set('allocator.risk_parity_blend', rp_blend)
    cfg.set('allocator.sharpe_alpha', sharpe_alpha)
    cfg.set('allocator.base_weight.bear.S5', bear_s5)
    cfg.set('allocator.base_weight.crash.S5', crash_s5)
    
    # ── 3. 백테스트 실행 ───────────────────────────────────────────
    # 최적화 속도를 위해 출력 억제 (혹은 verbose=False 처리)
    try:
        # 백테스터 실행 (약간의 슬리피지/수수료 기본값 적용)
        result = run_event_backtest(start_date, end_date, initial_capital=150_000_000, 
                                    slippage_bps=5.0, commission_bps=1.5, verbose=False)
        
        cagr = result.get('cagr', 0.0)
        mdd = result.get('mdd_pct', -100.0)
        
        # ── 4. 목표 함수(Objective Function) 평가 ──────────────────
        # 브릿지워터 철학: CAGR 20%를 달성하되, 돈을 잃지 않는다(MDD 제한)
        
        # Penalize if CAGR < 20%
        cagr_penalty = max(0, 0.20 - cagr) * 10.0  # 20% 미달 시 큰 패널티
        
        # Penalize if MDD is severe (e.g., lower than -10%)
        mdd_penalty = max(0, abs(mdd) - 0.10) * 5.0
        
        # 스코어 = CAGR - (MDD 패널티 + CAGR 패널티)
        # Optuna는 기본적으로 최소화(minimize) 또는 최대화(maximize) 가능.
        # 우리는 이 스코어를 maximize 하도록 설정.
        score = cagr - cagr_penalty - mdd_penalty
        
        # 극단적으로 망가진 경우 조기 종료 반환
        if cagr < 0 or abs(mdd) > 0.30:
            score -= 1.0 
            
        return score
    except Exception as e:
        logger.error(f"Trial failed: {e}")
        return -999.0

def run_optimization(start_date: str, end_date: str, n_trials: int = 100):
    logger.info(f"🚀 메달리온/브릿지워터 다이내믹 최적화 엔진 가동 (Trials: {n_trials})")
    
    study = optuna.create_study(direction='maximize', study_name="Meridian_CAGR_Optimization")
    
    # Suppress optuna logging for cleaner output, only show criticals or trial summary
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    
    def objective_wrapper(trial):
        return objective(trial, start_date, end_date)
        
    study.optimize(objective_wrapper, n_trials=n_trials, show_progress_bar=True, n_jobs=2)
    
    logger.info("=========================================")
    logger.info("🏆 최적화 완료! (Best Trial)")
    best_trial = study.best_trial
    logger.info(f"Best Score: {best_trial.value:.4f}")
    logger.info("Best Parameters:")
    for key, value in best_trial.params.items():
        logger.info(f"  {key}: {value}")
        
    # 결과를 파일로 저장
    out_file = _PROJECT_ROOT / 'results' / 'optimizer_best_params.json'
    out_file.parent.mkdir(exist_ok=True)
    with open(out_file, 'w') as f:
        json.dump(best_trial.params, f, indent=4)
    logger.info(f"💾 파라미터 저장 완료: {out_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--trials', type=int, default=50)
    parser.add_argument('--start', type=str, default='2025-01-01')
    parser.add_argument('--end', type=str, default='2026-07-01')
    args = parser.parse_args()
    
    run_optimization(args.start, args.end, args.trials)

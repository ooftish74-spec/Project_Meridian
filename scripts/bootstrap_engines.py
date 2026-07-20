"""
Meridian — Engine Bootstrap & Validation Simulation
=====================================================
새롭게 구축된 수학적 모델(HMM, Risk Parity, Capital Allocator, Gap Analyzer)의
결합 테스트 및 초기 피팅(Initial Fitting)을 수행하는 시뮬레이션 스크립트입니다.
"""

import sys
import os
import pandas as pd
import numpy as np
import logging
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from src.regime.hmm_regime_model import HMMRegimeModel
from src.allocation.risk_parity import RiskParityOptimizer
from src.allocation.capital_allocator import MetaCapitalAllocator
from src.learning.gap_analysis import GapAnalyzer

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def simulate_hmm_regime():
    logger.info("=== 1. HMM Regime Model 초기 학습(Fitting) 시뮬레이션 ===")
    # 5년치 가상 VIX/변동성 데이터 생성 (252 * 5 days)
    np.random.seed(42)
    days = 252 * 5
    # 시뮬레이션: 평상시(Mean=15, Std=3), 가끔 위기(Mean=35, Std=10)
    normal_vix = np.random.normal(15, 3, int(days * 0.8))
    crisis_vix = np.random.normal(35, 10, int(days * 0.2))
    vix_history = np.concatenate([normal_vix, crisis_vix])
    np.random.shuffle(vix_history)
    
    df_hist = pd.DataFrame({'vix': vix_history})
    
    engine = HMMRegimeModel(n_components=4)
    try:
        engine.fit(df_hist)
        logger.info("✅ HMM 모델 학습 완료 (No exception raised).")
    except Exception as e:
        logger.error(f"❌ HMM 모델 학습 실패: {e}")

def simulate_risk_parity():
    logger.info("\n=== 2. Risk Parity Optimizer 최적화 시뮬레이션 ===")
    # 3개 스트림의 가상 공분산 행렬 생성
    cov_matrix = np.array([
        [0.04, 0.01, 0.005],
        [0.01, 0.05, 0.02],
        [0.005, 0.02, 0.06]
    ])
    
    optimizer = RiskParityOptimizer()
    try:
        weights = optimizer.optimize(cov_matrix)
        logger.info(f"✅ 산출된 최적 비중 (Risk Parity Weights): {weights}")
        # 검증: 가중치의 합이 1인지
        assert np.isclose(np.sum(weights), 1.0), "가중치 합이 1이 아닙니다."
    except Exception as e:
        logger.error(f"❌ Risk Parity 최적화 실패: {e}")

def simulate_capital_allocator():
    logger.info("\n=== 3. Meta-Level Capital Allocator 가상 장부 연동 시뮬레이션 ===")
    allocator = MetaCapitalAllocator(total_capital=100_000_000.0) # 1억
    
    stream_metrics = {
        "S1": {"win_rate": 0.55, "edge": 0.05},  # Edge 높음 -> 할당량 많아야 함
        "S2": {"win_rate": 0.50, "edge": 0.01},  # Edge 낮음 -> 할당량 적어야 함
        "S3": {"win_rate": 0.45, "edge": -0.02}  # Edge 음수 -> 할당 0
    }
    
    cov_matrix = np.eye(3)
    try:
        allocations = allocator.reallocate(stream_metrics, cov_matrix)
        logger.info(f"✅ 산출된 가상 계좌 할당액 (KRW): {allocations}")
        # Virtual Ledger 저장 확인
        ledger_file = PROJECT_ROOT / "results" / "virtual_ledger.json"
        if ledger_file.exists():
            logger.info("✅ virtual_ledger.json 가상 장부 저장 완료.")
    except Exception as e:
        logger.error(f"❌ Capital Allocator 실패: {e}")

def simulate_continuous_learning():
    logger.info("\n=== 4. Gap Analysis (Continuous Learning) 시뮬레이션 ===")
    analyzer = GapAnalyzer(decay_factor=0.90)
    
    predictions = pd.DataFrame({
        'ticker': ['AAPL', 'MSFT', 'TSLA'],
        'predicted_return': [0.02, 0.01, 0.05],
        'model_id': ['GBR', 'XGB', 'RF']
    })
    
    actual_trades = pd.DataFrame({
        'ticker': ['AAPL', 'MSFT', 'TSLA'],
        'entry_price': [150, 200, 700],
        'predicted_entry_price': [149.5, 200, 690],
        'actual_return': [0.01, 0.01, -0.02]
    })
    
    try:
        gap_df = analyzer.calculate_gap(predictions, actual_trades)
        logger.info("✅ Gap 계산 성공. 샘플:")
        logger.info(f"\n{gap_df[['ticker', 'model_id', 'return_gap', 'slippage_gap']]}")
        
        current_weights = {'GBR': 0.33, 'XGB': 0.33, 'RF': 0.34}
        new_weights = analyzer.update_model_weights(gap_df, current_weights)
        logger.info(f"✅ 피드백 반영 후 새로운 모델 가중치: {new_weights}")
    except Exception as e:
        logger.error(f"❌ Gap Analysis 실패: {e}")

if __name__ == "__main__":
    logger.info("Meridian Engine Bootstrap Simulator 시작...")
    simulate_hmm_regime()
    simulate_risk_parity()
    simulate_capital_allocator()
    simulate_continuous_learning()
    logger.info("\n모든 엔진 시뮬레이션 완료.")

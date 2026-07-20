#!/usr/bin/env python3
"""
Alpha Miner 실행 스크립트.
macOS launchd (com.project.meridian.alpha_miner) 에 의해 매일 00:15에 독립 실행됩니다.
수백/수천 번의 Symbolic Regression 연산을 수행하여 새로운 수학적 알파를 발굴하고
결과와 모델(alpha_model.joblib)을 저장합니다.
저장된 모델은 저녁(18:00) 훈련 파이프라인에서 S2(ML Alpha)의 AutoML 피처로 활용됩니다.
"""

import sys
import logging
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_PROJECT_ROOT))

from src.alpha_factory.alpha_miner import AlphaMiner

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('AlphaMinerRunner')

def main():
    logger.info("🚀 Alpha Miner 심야 배치 작업 시작")
    miner = AlphaMiner()
    
    try:
        # 매일 밤 10세대의 유전 알고리즘 교배 수행 (환경에 따라 세대수/개체군 조절)
        miner.mine_alphas(n_generations=10, pop_size=500)
        logger.info("✅ Alpha Miner 심야 배치 작업 성공적으로 완료")
    except Exception as e:
        logger.error(f"❌ Alpha Miner 실행 중 치명적 오류: {e}")

if __name__ == '__main__':
    main()

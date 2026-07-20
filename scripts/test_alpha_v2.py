#!/usr/bin/env python3
import sys
import logging
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_PROJECT_ROOT))

from src.alpha_factory.alpha_miner import AlphaMiner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('TestAlphaV2')

def main():
    logger.info("🧪 AlphaMiner V2 테스트 모드 실행 (generations=2, pop_size=20)")
    miner = AlphaMiner()
    try:
        # 가벼운 테스트 (빠르게 끝내기 위해)
        miner.mine_alphas(n_generations=2, pop_size=20)
        logger.info("✅ AlphaMiner V2 테스트 런 성공!")
    except Exception as e:
        logger.error(f"❌ AlphaMiner V2 에러 발생: {e}", exc_info=True)

if __name__ == '__main__':
    main()

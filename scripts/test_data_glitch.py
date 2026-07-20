import sys
import os
from pathlib import Path
from unittest.mock import patch

# 경로 설정
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from src.data.market_data_bridge import MarketDataBridge, DataIntegrityError, DataSanityGate

def run_tests():
    print("🧪 [Test 1] DataSanityGate 변동폭 초과 테스트 (Glitch)")
    try:
        # KOSPI가 2700에서 1000으로 폭락한 글리치 상황 가정 (제한 15%)
        DataSanityGate.validate('KOSPI', 1000.0, 2700.0, max_pct_change=0.15)
        print("❌ 실패: 예외가 발생하지 않았습니다!")
    except DataIntegrityError as e:
        print(f"✅ 성공: 예외 정상 발생 -> {e}")

    print("\n🧪 [Test 2] MarketDataBridge 수집 실패 시 Freeze 테스트")
    bridge = MarketDataBridge()
    
    # yfinance와 parquet 모두 실패(또는 Stale)하게 만들어서 _get_vix가 Freeze를 발생시키는지 확인
    with patch('yfinance.Ticker.history') as mock_yf, \
         patch('pandas.read_parquet') as mock_pq, \
         patch('pathlib.Path.exists', return_value=False): # OIS JSON 및 parquet 존재하지 않는 것으로 속임
        
        # yfinance 빈 데이터 반환
        import pandas as pd
        mock_yf.return_value = pd.DataFrame() 
        mock_pq.side_effect = Exception("Mock Parquet Error")

        try:
            bridge._get_vix()
            print("❌ 실패: _get_vix()가 하드코딩 값을 반환하거나 정상 종료되었습니다!")
        except DataIntegrityError as e:
            print(f"✅ 성공: 모든 데이터 소스 실패 시 셧다운 예외 정상 발생 -> {e}")

if __name__ == "__main__":
    run_tests()

import pandas as pd
import numpy as np
import logging
from src.allocation.tax_optimizer import TaxOptimizer

logging.basicConfig(level=logging.INFO)

def test_tax_optimizer():
    optimizer = TaxOptimizer(target_tax_bracket_profit=200_000_000, harvest_loss_threshold=-0.10)
    
    # 1. 3억 수익 (S0/S1의 확정 이익)
    realized_gains = 300_000_000 
    
    # 2. 포트폴리오
    portfolio = {
        '005930': {'qty': 2000, 'entry_price': 80000}, # 삼성전자
        '035420': {'qty': 1000, 'entry_price': 200000}, # NAVER (손실 아님)
        '000660': {'qty': 500, 'entry_price': 150000} # 하이닉스
    }
    
    # 3. 현재가 설정
    # 삼성전자는 -30% 손실 (-4,800만 원 손실)
    # 하이닉스도 -40% 손실 (-3,000만 원 손실)
    current_prices = {
        '005930': 56000, 
        '035420': 210000, 
        '000660': 90000
    }
    
    # 4. 과거 가격 데이터 (Correlation)
    dates = pd.date_range('2024-01-01', periods=100)
    np.random.seed(42)
    
    # 삼성전자와 하이닉스 상관관계 높게 생성, NAVER는 상관관계 낮게
    base_market = np.random.normal(0, 0.02, 100)
    samsung_ret = base_market + np.random.normal(0, 0.005, 100)
    hynix_ret = base_market + np.random.normal(0, 0.008, 100)
    naver_ret = np.random.normal(0, 0.02, 100)
    
    historical_prices = pd.DataFrame(index=dates)
    historical_prices['005930'] = (1 + samsung_ret).cumprod() * 80000
    historical_prices['000660'] = (1 + hynix_ret).cumprod() * 150000
    historical_prices['035420'] = (1 + naver_ret).cumprod() * 200000
    
    # Proxy test (KODEX 200과 반도체 대체재 추가)
    proxy_ret = samsung_ret + np.random.normal(0, 0.001, 100)
    historical_prices['KODEX_SEMI'] = (1 + proxy_ret).cumprod() * 30000
    current_prices['KODEX_SEMI'] = 28000
    
    print("--- Tax Optimizer 실행 ---")
    signals = optimizer.calculate_tax_alpha_signals(
        realized_gains=realized_gains,
        portfolio=portfolio,
        historical_prices=historical_prices,
        current_prices=current_prices
    )
    
    print("\n[생성된 시그널]")
    for sig in signals:
        print(sig)

if __name__ == "__main__":
    test_tax_optimizer()

import os
import sys
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.streams.s3_active_macro.qvm_universe import QVMUniverse
from src.streams.s3_active_macro.qvm_scorer import QVMScorer
from src.portfolio.shadow_manager import ShadowPortfolioManager

def run_track_b_population():
    print("🚀 QVM 엔진을 통한 S3 Track B 강제 종목 발굴 시작...")
    
    universe_builder = QVMUniverse()
    scorer = QVMScorer()
    
    print("1. KOSPI 200 유니버스 추출 중...")
    qvm_raw = universe_builder.build_universe()
    
    print("2. 퀄리티-가치-모멘텀(QVM) 스코어링 진행 중...")
    qvm_scored = scorer.score_universe(qvm_raw)
    
    print("3. Value Trap (가치함정) 및 재무 위험 필터링 중...")
    qvm_safe = scorer.screen_value_traps(qvm_scored)
    
    # Sort and pick top 5
    qvm_safe.sort(key=lambda x: x.get('qvm_score', 0), reverse=True)
    top_5 = qvm_safe[:5]
    
    print("\n🌟 발굴된 Track B 최우수 종목 (Top 5):")
    for s in top_5:
        print(f"  - {s['name']} ({s['ticker']}): QVM Score {s.get('qvm_score', 0):.2f}, Margin of Safety: {s.get('margin_of_safety_pct', 0):.1f}%")
        
    print("\n4. Shadow Portfolio에 Mock 체결로 강제 주입 중...")
    sp = ShadowPortfolioManager()
    
    budget_per_stock = 4000000  # 400만원씩 (총 2000만원)
    
    for s in top_5:
        ticker = s['ticker']
        price = s.get('current_price', s.get('price', 10000))
        qty = int(budget_per_stock // price)
        if qty == 0: continue
        
        cost = qty * price
        
        pos_key = f"S3:{ticker}"
        
        if pos_key not in sp.data['positions']:
            sp.data['positions'][pos_key] = {
                'ticker': ticker,
                'name': s['name'],
                'quantity': qty,
                'avg_price': price,
                'entry_price': price,
                'amount': cost,
                'entry_date': '2026-06-19',
                'stream_id': 'S3',
                'strategy': 'qvm_value_stock',
                'account': 'BROKERAGE',
                'current_price': price,
                'current_value': cost,
                'market_value': cost,
                'unrealized_pnl': 0.0,
                'unrealized_pnl_pct': 0.0
            }
            sp.data['cash'] -= cost
            print(f"  ✅ 편입 완료: {s['name']} {qty}주 (평단가 {price:,}원)")
            
    sp.save()
    print(f"💾 포트폴리오 저장 완료. (남은 현금: {sp.data['cash']:,.0f}원)")

if __name__ == '__main__':
    run_track_b_population()

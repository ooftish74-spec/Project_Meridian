import json
from pathlib import Path
import os

def main():
    _ROOT = Path(__file__).resolve().parent.parent
    result_path = _ROOT / 'results' / 'event_backtest_result.json'
    
    if not result_path.exists():
        print(f"File not found: {result_path}")
        return
        
    with open(result_path, 'r') as f:
        data = json.load(f)
        
    stats = data.get('summary', {})
    print("="*60)
    print(" 펀드 전체 통합 성과 ")
    print("="*60)
    print(f"총 수익률(Total Return): {stats.get('total_return_pct', 0):.2f}%")
    print(f"MDD: {stats.get('max_drawdown_pct', 0):.2f}%")
    print(f"Sharpe Ratio: {stats.get('sharpe_ratio', 0):.4f}")
    
    print("\n" + "="*60)
    print(" 투자 스트림별 성과 기여도 (Virtual Sub-accounts)")
    print("="*60)
    stream_metrics = stats.get('stream_metrics', {})
    
    if not stream_metrics:
        print("스트림별 성과 데이터가 없습니다. (백테스터에 기록되지 않음)")
        return
        
    print(f"{'Stream':<12} | {'Return Contrib (%)':<20} | {'MDD (%)':<10} | {'Final NAV (KRW)'}")
    print("-" * 75)
    
    for sid, metrics in sorted(stream_metrics.items(), key=lambda x: x[1].get('return_contrib_pct', 0), reverse=True):
        ret = metrics.get('return_contrib_pct', 0)
        mdd = metrics.get('mdd_pct', 0)
        nav = metrics.get('final_nav', 0)
        print(f"[{sid:<10}] | {ret:>18.2f}% | {mdd:>9.2f}% | ₩{nav:>15,.0f}")
        
    print("\n* Return Contrib (%): 초기 전체 자본금 대비 해당 스트림이 창출한 수익 기여율")
    print("* SYS_KILL 및 SYS_HEDGE가 발생시킨 강제 청산 손익은 원래 해당 종목을 보유한 스트림으로 안전하게 귀속(Attribution)되었습니다.")

if __name__ == '__main__':
    main()

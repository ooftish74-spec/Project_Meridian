import json
import pandas as pd
import numpy as np

try:
    data = json.load(open('results/event_backtest_result.json'))
except FileNotFoundError:
    print("No backtest result found.")
    exit(1)

trades = data.get('trades', [])

if not trades:
    print("No trades available to calculate stream performance.")
    exit()

ticker_to_stream = {}
for t in trades:
    if t['stream_id'] not in ('SYS_KILL', 'SYS_HEDGE', 'UNKNOWN', 'S_BETA'):
        ticker_to_stream[t['ticker']] = t['stream_id']

stream_pnl = {s: 0.0 for s in ticker_to_stream.values()}
stream_pnl['SYS_HEDGE'] = 0.0
stream_pnl['S_BETA'] = 0.0
stream_pnl['UNKNOWN'] = 0.0

for t in trades:
    sid = t['stream_id']
    if sid in ('SYS_KILL', 'UNKNOWN') or sid not in stream_pnl:
        sid = ticker_to_stream.get(t['ticker'], 'UNKNOWN')
        
    if sid in stream_pnl:
        stream_pnl[sid] += t.get('pnl', 0.0)
    else:
        stream_pnl[sid] = t.get('pnl', 0.0)

print("=== 120-Day Realized P&L by Stream ===")
total_pnl = sum(stream_pnl.values())
for s, pnl in sorted(stream_pnl.items(), key=lambda x: x[1], reverse=True):
    if pnl != 0:
        pct = (pnl / total_pnl * 100) if total_pnl != 0 else 0
        print(f"[{s:10s}] {pnl:>15,.0f} KRW ({pct:>5.1f}%)")

print("\n* MDD: 정확한 스트림별 MDD는 데일리 스트림 단위의 NAV 스냅샷이 있어야 계산 가능합니다.")
print("현재 백테스터 구조상 실현 손익(Realized P&L)으로 스트림별 기여도를 추정했습니다.")

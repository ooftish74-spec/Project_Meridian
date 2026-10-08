#!/usr/bin/env python3
"""
AWS Live Stream Diagnostic Script
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.stream_orchestrator import StreamOrchestrator

def main():
    orch = StreamOrchestrator(exec_mode='live')
    signal_cache = orch.data_bridge.build_signal_cache(force=True)
    overnight = orch.data_bridge.build_overnight_intel()
    regime_hist = orch.data_bridge.get_regime_history()

    market_data = {
        'signal_cache': signal_cache,
        'overnight_intel': overnight,
        'vix_history': regime_hist.get('vix_history', []),
        'kospi_returns': regime_hist.get('kospi_returns', []),
        'us_regime': 'neutral',
    }
    regime_result = orch.regime_detector.detect(market_data)
    regime = regime_result['regime']
    conf = regime_result.get('confidence', 0.0)
    print(f"=== Detected Regime: {regime} (Confidence: {conf:.2f}) ===")
    
    technicals = signal_cache.get('stock_technicals', {})
    if isinstance(technicals, dict):
        print(f"Stock technicals count: {len(technicals)}, keys: {list(technicals.keys())}")
    else:
        print(f"Stock technicals type: {type(technicals)}, len: {len(technicals)}")

    for s in orch.streams:
        try:
            sigs = s.generate_signals(regime, market_data)
            print(f"Stream {s.stream_id}: {len(sigs)} signals")
            for sig in sigs:
                ticker = sig.get('ticker')
                action = sig.get('action') or sig.get('direction')
                confidence = sig.get('confidence')
                reason = sig.get('reason', '')[:60]
                print(f"   -> {ticker} | {action} | conf: {confidence} | {reason}")
        except Exception as e:
            print(f"Stream {s.stream_id} ERROR: {e}")

    try:
        s10_out = orch.s10.generate_signals(regime, market_data)
        s10_sigs = s10_out if isinstance(s10_out, list) else s10_out.get('orders', [])
        print(f"Stream S10_MEGA_TREND: {len(s10_sigs)} signals")
        for sig in s10_sigs:
            ticker = sig.get('ticker')
            action = sig.get('action') or sig.get('direction')
            confidence = sig.get('confidence')
            reason = sig.get('reason', '')[:60]
            print(f"   -> {ticker} | {action} | conf: {confidence} | {reason}")
    except Exception as e:
        print(f"Stream S10_MEGA_TREND ERROR: {e}")

if __name__ == '__main__':
    main()

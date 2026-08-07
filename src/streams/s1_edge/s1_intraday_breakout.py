"""
s1_intraday_breakout.py — S1 장중 거래량 돌파 시그널 (S1-B)
=============================================================
[Phase 36: Intraday Dynamic Adjustment]

09:30~10:30 사이, 관심 유니버스 종목 중
  ① 거래량이 전일 대비 폭발적으로 증가 (> volume_spike_threshold × 전일 거래량)
  ② 외국인 순매수 유입 (> min_foreign_flow_krw)
인 종목을 포착하여 제한적 비중(breakout_size_pct)의 매수 시그널을 생성합니다.

레짐이 'caution'이더라도 수급·거래량이 뒷받침되면 돌파 매수를 허용합니다.
모든 파라미터는 DynamicConfig를 통해 관리됩니다.
"""
from __future__ import annotations
import json
from src.utils.file_ops import atomic_write_json

import logging
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'

class S1IntradayBreakout:
    """
    [Phase 36] 장중 거래량 돌파 시그널 스캐너 (S1-B).

    Usage:
        flow_data = IntradayFlowCollector.load_cache()
        scanner   = S1IntradayBreakout()
        signals   = scanner.scan(flow_data, universe=["005930", "000660", ...])
    """

    def __init__(self, config=None):
        try:
            if config is None:
                from config.dynamic_config import DynamicConfig
                config = DynamicConfig()
            self.config = config
        except Exception as e:
            logger.warning('[S1-B] DynamicConfig 로드 실패: %s', e)
            self.config = None

    def _cfg(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        return self.config.get(key, default)

    def _in_active_window(self) -> bool:
        """현재 시각이 스캔 활성 윈도우(09:30~10:30) 안에 있는지."""
        now = datetime.now().time()
        start_str = self._cfg('s1_breakout.active_window_start', '09:30')
        end_str = self._cfg('s1_breakout.active_window_end', '10:30')
        try:
            h_s, m_s = map(int, start_str.split(':'))
            h_e, m_e = map(int, end_str.split(':'))
            t_start = dtime(h_s, m_s)
            t_end = dtime(h_e, m_e)
            return t_start <= now <= t_end
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return True

    def _evaluate_ticker(self, ticker: str, ticker_flow: Dict[str, Any], volume_spike_thr: float, min_foreign_krw_m: float) -> Optional[Dict[str, Any]]:
        """
        단일 종목에 대해 돌파 조건을 평가합니다.

        Returns:
            조건 충족 시 시그널 dict, 미충족 시 None.
        """
        if not ticker_flow:
            return None
        vol_ratio = float(ticker_flow.get('volume_ratio', 0.0))
        frgn_krw = float(ticker_flow.get('foreign_net_krw', 0.0))
        today_vol = int(ticker_flow.get('today_volume', 0))
        prev_vol = int(ticker_flow.get('prev_volume', 0))
        cur_px = int(ticker_flow.get('current_price', 0))
        vol_spike = vol_ratio >= volume_spike_thr
        frgn_inflow = frgn_krw >= min_foreign_krw_m
        if not (vol_spike and frgn_inflow):
            logger.debug('[S1-B] %s 미충족 — 거래량비=%0.fx(조건≥%0.f) 외인=%+.0f백만(조건≥%.0f)', ticker, vol_ratio, volume_spike_thr, frgn_krw, min_foreign_krw_m)
            return None
        signal = {'ticker': ticker, 'signal': 'BREAKOUT_LONG', 'size_pct': self._cfg('s1_breakout.breakout_size_pct', 0.05), 'detected_at': datetime.now().isoformat(), 'volume_ratio': vol_ratio, 'today_volume': today_vol, 'prev_volume': prev_vol, 'foreign_net_krw_m': frgn_krw, 'current_price': cur_px, 'trigger_conditions': [f'거래량 {vol_ratio:.1f}x (임계: {volume_spike_thr:.1f}x)', f'외국인 순매수 {frgn_krw:+.0f} (임계: {min_foreign_krw_m:.0f})'], 'regime_override': bool(self._cfg('s1_breakout.regime_override_enabled', True))}
        logger.info('[S1-B] 🚀 BREAKOUT 포착 %s — 거래량×%.1f 외인+%.0f백만원 비중=%.0f%%', ticker, vol_ratio, frgn_krw, signal['size_pct'] * 100)
        return signal

    def scan(self, flow_data: Dict[str, Any], universe: Optional[List[str]]=None, regime: str='bull') -> List[Dict[str, Any]]:
        """
        유니버스 전체에 대해 장중 돌파 조건 스캔.

        Args:
            flow_data: IntradayFlowCollector.load_cache() 반환값
            universe:  스캔 대상 종목코드 리스트 (None이면 flow_data 내 전체)
            regime:    현재 시장 레짐

        Returns:
            List of signal dicts (조건 충족 종목만).
        """
        if not self._in_active_window():
            logger.debug('[S1-B] 활성 윈도우 외 — 스캔 생략')
            return []
        if not flow_data or not flow_data.get('tickers'):
            logger.warning('[S1-B] flow_data 없음 — 스캔 불가')
            return []
        volume_spike_thr = float(self._cfg('s1_breakout.volume_spike_threshold', 3.0))
        min_frgn_raw = float(self._cfg('s1_breakout.min_foreign_flow_krw', 5000000000))
        flow_unit = float(flow_data.get('flow_unit_krw') or self._cfg('intraday.flow_unit_krw', 1000000))
        min_frgn_m = min_frgn_raw / flow_unit
        max_signals = int(self._cfg('s1_breakout.max_signals_per_day', 2))
        unit_label = f'{int(flow_unit / 1000000)}백만원'
        tickers_in_cache = list(flow_data.get('tickers', {}).keys())
        scan_targets = universe if universe else tickers_in_cache
        scan_targets = [t for t in scan_targets if t in tickers_in_cache]
        logger.info('[S1-B] 스캔: %d종목 | 레징=%s | 거래량임계=%.0fx | 외인최소=%.0f%s', len(scan_targets), regime, volume_spike_thr, min_frgn_m, unit_label)
        signals = []
        for ticker in scan_targets:
            ticker_flow = flow_data['tickers'].get(ticker, {})
            sig = self._evaluate_ticker(ticker, ticker_flow, volume_spike_thr, min_frgn_m)
            if sig:
                signals.append(sig)
                if len(signals) >= max_signals:
                    logger.info('[S1-B] 최대 시그널 수(%d) 달성 — 스캔 중단', max_signals)
                    break
        if signals:
            logger.info('[S1-B] 총 %d개 돌파 시그널 생성', len(signals))
            self._save_signals(signals)
        else:
            logger.info('[S1-B] 돌파 시그널 없음')
        return signals

    def _save_signals(self, signals: List[Dict[str, Any]]) -> None:
        """results/s1_breakout_signals.json 저장."""
        out = {'generated_at': datetime.now().isoformat(), 'count': len(signals), 'signals': signals}
        path = _RESULTS_DIR / 's1_breakout_signals.json'
        try:
            atomic_write_json(path, out, ensure_ascii=False, indent=2)
            logger.info('[S1-B] 시그널 저장: %s', path.name)
        except Exception as e:
            logger.error('[S1-B] 저장 실패: %s', e)
if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s')
    parser = argparse.ArgumentParser(description='[Phase 36] S1 장중 돌파 스캐너')
    parser.add_argument('--regime', default='bull', choices=['bull', 'bear', 'caution', 'crash'])
    parser.add_argument('--mock', action='store_true', help='Mock flow 데이터 주입 후 테스트')
    args = parser.parse_args()
    if args.mock:
        mock_flow = {'timestamp': datetime.now().isoformat(), 'tickers': {'005930': {'foreign_net_krw': 7000.0, 'institution_net_krw': 3000.0, 'combined_net_krw': 10000.0, 'volume_ratio': 4.2, 'today_volume': 21000000, 'prev_volume': 5000000, 'current_price': 75300}, '000660': {'foreign_net_krw': 800.0, 'institution_net_krw': 200.0, 'combined_net_krw': 1000.0, 'volume_ratio': 1.5, 'today_volume': 3000000, 'prev_volume': 2000000, 'current_price': 200000}}}
        scanner = S1IntradayBreakout()
        sigs = scanner.scan(mock_flow, regime=args.regime)
        logger.debug(json.dumps(sigs, ensure_ascii=False, indent=2))
    else:
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector
        flow = IntradayFlowCollector.load_cache()
        scanner = S1IntradayBreakout()
        sigs = scanner.scan(flow, regime=args.regime)
        logger.debug(json.dumps(sigs, ensure_ascii=False, indent=2))
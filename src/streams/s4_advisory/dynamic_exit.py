"""
Dynamic Exit Evaluator Alias (V3 Single SSOT Redirection)
=========================================================
구형 s4_advisory/dynamic_exit.py는 V3 단일 SSOT인 `src.risk.universal_exit_engine.UniversalExitEngine`으로
100% 통합 및 리다이렉트되어 파편화 및 중복 코드가 100% 원천 제거되었습니다.
"""

import logging
from typing import Dict, Optional
from src.risk.universal_exit_engine import UniversalExitEngine

logger = logging.getLogger(__name__)

import logging
from typing import Dict, Any, Optional
from src.risk.universal_exit_engine import UniversalExitEngine

logger = logging.getLogger(__name__)

class DynamicExitEvaluator:
    """UniversalExitEngine 단일 SSOT 리다이렉트 에이전트 및 장중 수급 동적 엑시트 지원."""

    def __init__(self):
        self._engine = UniversalExitEngine()

    def evaluate(self, s4_pos: Dict, market_data: Optional[Dict] = None, regime: str = 'caution', flow_data: Optional[Dict] = None) -> Dict:
        """UniversalExitEngine으로 전사 평가 이관 (레가시 키 및 flow_data 호환성 유지)."""
        logger.info("  🔄 [SSOT Redirection] DynamicExitEvaluator ➔ UniversalExitEngine 단일 뼈대 호출")
        res = self._engine.evaluate_all_positions(s4_pos, market_data, regime)
        
        exit_orders = res.get('exit_orders', [])
        exit_candidates = []
        for o in exit_orders:
            exit_candidates.append({
                'ticker': o.get('ticker'),
                'reasons': [{'rule': 'universal_exit', 'detail': o.get('reason')}]
            })
        
        # flow_data 긴급 청산 주입
        if flow_data and 'tickers' in flow_data:
            positions_dict = s4_pos if isinstance(s4_pos, dict) else {}
            for pos_key, pos in positions_dict.items():
                ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else pos_key)
                thresholds = {"stop_loss_pct": 0.07, "trail_stop_atr_mult": 1.5}
                flow_res = self._check_flow_dynamic_exit(pos, thresholds, flow_data, ticker)
                if flow_res.get('exit'):
                    existing_tickers = [c['ticker'] if isinstance(c, dict) else c for c in exit_candidates]
                    if ticker not in existing_tickers:
                        exit_candidates.append({
                            'ticker': ticker,
                            'reasons': [{'rule': 'flow_dynamic', 'detail': flow_res.get('detail')}]
                        })
                        exit_orders.append({
                            'stream_id': pos.get('stream_id', 'S4'),
                            'ticker': ticker,
                            'direction': 'short',
                            'action': 'sell',
                            'quantity': pos.get('quantity', 1.0),
                            'price': pos.get('current_price', 1.0),
                            'execution_algo': 'market',
                            'reason': flow_res.get('detail', 'Flow Dynamic Panic Exit')
                        })

        exit_count = len(exit_orders)
        total_positions = len(s4_pos) if isinstance(s4_pos, dict) else 0
        hold_count = max(0, total_positions - exit_count)
        hold_positions = [k for k in (s4_pos.keys() if isinstance(s4_pos, dict) else [])]

        return {
            'results': {},
            'exit_count': exit_count,
            'exit_candidates': exit_candidates,
            'hold_count': hold_count,
            'hold_positions': hold_positions,
            'total_positions': total_positions,
            'dynamic_thresholds': {},
            'raw_eval': res
        }

    def _check_flow_dynamic_exit(self, pos: Dict[str, Any], thresholds: Dict[str, Any], flow_data: Dict[str, Any], ticker: str) -> Dict[str, Any]:
        """장중 기관/외인 수급 흐름(Flow) 기반 동적 Exit 조건 평가."""
        tickers_map = flow_data.get('tickers', {}) if isinstance(flow_data, dict) else {}
        if not flow_data or ticker not in tickers_map:
            return {
                "action": "neutral", "exit": False, "whipsaw_defer": False,
                "detail": "Fallback: 수급 데이터 없음", "flow_adjusted_sl_pct": None,
                "flow_adjusted_ts_mult": None, "urgency": 0
            }

        t_data = tickers_map[ticker]
        inst_krw = float(t_data.get('institution_net_krw', 0.0))
        for_krw = float(t_data.get('foreign_net_krw', 0.0))
        comb_krw = inst_krw + for_krw
        vol_ratio = float(t_data.get('volume_ratio', 1.0))
        pnl_pct = float(pos.get('pnl_pct', 0.0))
        base_ts_mult = float(thresholds.get('trail_stop_atr_mult', 1.5))
        base_sl_pct = float(thresholds.get('stop_loss_pct', 0.07))

        # Case 1: Whipsaw Filter (거래량 극소 + 수급 미미 + 손실 중 ➔ 개미털기 유예)
        if vol_ratio < 0.30 and abs(comb_krw) < 500.0 and pnl_pct < 0:
            return {
                "action": "whipsaw_defer", "exit": False, "whipsaw_defer": True,
                "detail": f"Whipsaw 감지: 거래량 비율 {vol_ratio:.1%} < 30% 및 수급 부재({comb_krw:+.1f}백만) ➔ 손절 유예",
                "flow_adjusted_sl_pct": None, "flow_adjusted_ts_mult": None, "urgency": 0
            }

        # Case 2: Trend Rider (쌍끌이 강력 매수 ➔ Trailing Stop 확장)
        if inst_krw > 1000.0 and for_krw > 1000.0:
            widen_mult = base_ts_mult * 1.5  # 1.5 -> 2.25
            return {
                "action": "widen", "exit": False, "whipsaw_defer": False,
                "detail": f"Trend Rider: 기관({inst_krw:+.0f}백만) + 외인({for_krw:+.0f}백만) 쌍끌이 매수 ➔ TS 확장({base_ts_mult:.1f}x → {widen_mult:.2f}x)",
                "flow_adjusted_sl_pct": None, "flow_adjusted_ts_mult": widen_mult, "urgency": 0
            }

        # Case 3: Panic Tightener (쌍끌이 폭풍 매도 + 손실 중 ➔ 긴급 즉시 청산)
        if inst_krw < -1000.0 and for_krw < -1000.0 and pnl_pct < -1.5:
            tight_sl = base_sl_pct * 0.5  # 0.07 -> 0.035
            return {
                "action": "tighten", "exit": True, "whipsaw_defer": False,
                "detail": f"Panic Tightener: 기관({inst_krw:+.0f}백만) + 외인({for_krw:+.0f}백만) 동시 매도 ➔ 즉시 긴급 청산",
                "flow_adjusted_sl_pct": tight_sl, "flow_adjusted_ts_mult": None, "urgency": 3
            }

        return {
            "action": "neutral", "exit": False, "whipsaw_defer": False,
            "detail": "수급 중립", "flow_adjusted_sl_pct": None,
            "flow_adjusted_ts_mult": None, "urgency": 0
        }
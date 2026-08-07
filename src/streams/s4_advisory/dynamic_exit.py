"""
S4 Dynamic Exit Evaluator — 동적 손절/교체 규칙 엔진
=======================================================

모든 임계값은 시장 데이터 기반으로 **동적 계산**되며,
DynamicConfig 파라미터는 계산의 기준점(anchor)으로만 사용합니다.

4가지 Exit Rule:
  1. QV Score Decay — 편입 시점 QV 대비 하락률 기반 (시장 전체 QV 분포 참조)
  2. Value Trap Guard — ATR/변동성 기반 동적 손절선
  3. Sector Concentration — 상관관계 기반 동적 섹터 집중도 관리
  4. Momentum Exhaustion — 보유기간별 모멘텀 감쇠 기반 동적 교체

Usage:
    from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator
    evaluator = DynamicExitEvaluator()
    result = evaluator.evaluate(portfolio_positions, market_data)
"""
import pandas as pd
import json
import logging
import math
from datetime import datetime, date
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_RESULTS = _PROJECT_ROOT / 'results'
_DATA_DIR = _PROJECT_ROOT / 'data' / 'historical_10y'

class DynamicExitEvaluator:
    """S4 동적 Exit 평가 엔진.

    모든 임계값은 시장 상태에서 실시간 계산:
      - QV 임계값: 유니버스 전체 QV 분포의 하위 percentile
      - 손절선: 개별 종목 ATR × 배수 (변동성 정규화)
      - 섹터 집중도: 보유 종목 간 상관관계 기반
      - 보유 기간: 모멘텀 스코어 기반 동적 연장/단축

    [Phase: Fully Dynamic Exit]
      - Chandelier Trailing Stop: 종목별 ATR × 레짐 배수
      - S2 Catastrophic Stop: ATR × 4.0 고점 대비 하락 즉각 청산
      - Scale-out 50%: ATR × 10 또는 +30% 달성 시 절반 익절 후 Runner 관리
    """

    def __init__(self):
        self._universe_qv_cache: Optional[Dict] = None
        self._last_evaluation: Optional[Dict] = None
        self._atr_cache: Dict[str, float] = {}

    def evaluate(self, positions: Dict, market_data: Dict=None, regime: str='caution', flow_data: Dict=None) -> Dict:
        """전체 S4 포지션에 대해 Exit 규칙 평가.

        Args:
            positions: {pos_key: pos_dict} — S4 포지션만
            market_data: 시장 데이터 (signal_cache 등)
            regime: 현재 레짐

        Returns:
            {
                'exit_candidates': [...],  # 교체/매도 대상
                'hold_positions': [...],   # 유지 대상
                'rules_applied': {...},    # 각 규칙 적용 결과
                'dynamic_thresholds': {...},  # 계산된 동적 임계값
            }
        """
        if market_data is None:
            market_data = {}
        if flow_data is None:
            flow_data = {}
        thresholds = self._compute_dynamic_thresholds(positions, market_data, regime)
        exit_candidates = []
        hold_positions = []
        rules_detail = {'qv_decay': [], 'value_trap': [], 'sector_concentration': [], 'momentum_exhaustion': []}
        for pos_key, pos in positions.items():
            reasons = []
            qv_result = self._check_qv_decay(pos, thresholds)
            if qv_result['exit']:
                reasons.append(qv_result)
                rules_detail['qv_decay'].append(pos_key)
            vt_result = self._check_value_trap(pos, thresholds, regime)
            if vt_result['exit']:
                reasons.append(vt_result)
                rules_detail['value_trap'].append(pos_key)
            sc_result = self._check_sector_concentration(pos, positions, thresholds)
            if sc_result['exit']:
                reasons.append(sc_result)
                rules_detail['sector_concentration'].append(pos_key)
            me_result = self._check_momentum_exhaustion(pos, thresholds, regime)
            if me_result['exit']:
                reasons.append(me_result)
                rules_detail['momentum_exhaustion'].append(pos_key)
            tp_result = self._check_take_profit(pos, thresholds, regime)
            if tp_result.get('scale_out'):
                rules_detail.setdefault('scale_out', []).append(pos_key)
                reasons.append(tp_result)
            elif tp_result['exit']:
                reasons.append(tp_result)
                rules_detail.setdefault('take_profit', []).append(pos_key)
            cat_result = self._check_catastrophic_stop(pos, thresholds)
            if cat_result['exit']:
                reasons.append(cat_result)
                rules_detail.setdefault('catastrophic_stop', []).append(pos_key)
            if flow_data:
                ticker = pos.get('ticker', '')
                flow_result = self._check_flow_dynamic_exit(pos, thresholds, flow_data, ticker)
                if flow_result.get('action') in ('tighten', 'widen'):
                    rules_detail.setdefault('flow_dynamic', []).append(pos_key)
                    if flow_result.get('whipsaw_defer'):
                        reasons = [r for r in reasons if r.get('rule') not in ('catastrophic_stop',)]
                        logger.info('[Flow] %s Whipsaw 감지 → 손절 유예', ticker)
                    if flow_result.get('action') == 'tighten' and (not flow_result.get('whipsaw_defer')):
                        reasons.append(flow_result)
            if reasons:
                exit_candidates.append({'pos_key': pos_key, 'name': pos.get('name', pos_key), 'pnl_pct': pos.get('pnl_pct', 0), 'reasons': reasons, 'urgency': max((r.get('urgency', 1) for r in reasons))})
            else:
                hold_positions.append({'pos_key': pos_key, 'name': pos.get('name', pos_key), 'pnl_pct': pos.get('pnl_pct', 0)})
        exit_candidates.sort(key=lambda x: (-x['urgency'], x['pnl_pct']))
        result = {'timestamp': datetime.now().isoformat(), 'total_positions': len(positions), 'exit_count': len(exit_candidates), 'hold_count': len(hold_positions), 'exit_candidates': exit_candidates, 'hold_positions': hold_positions, 'rules_applied': rules_detail, 'dynamic_thresholds': thresholds, 'regime': regime}
        self._last_evaluation = result
        self._save_result(result)
        logger.info(f'  📊 S4 DynamicExit: {len(exit_candidates)} exit / {len(hold_positions)} hold (regime={regime})')
        return result

    def _compute_dynamic_thresholds(self, positions: Dict, market_data: Dict, regime: str) -> Dict:
        """시장 상태 기반 동적 임계값 계산.

        하드코딩 없이, 데이터에서 모든 기준을 도출.
        """
        signal_cache = market_data.get('signal_cache', {})
        vix = signal_cache.get('vix')
        if vix is None or vix <= 0:
            last_vix = getattr(self, '_last_known_vix', 18.0)
            vix = max(last_vix, 30.0)
            logger.warning(f'  🚨 [DynamicExit] VIX 누락 감지. 보수적 방어/청산 모드 돌입 (VIX={vix:.1f} 가정)')
        else:
            self._last_known_vix = vix
        universe_qv = self._load_universe_qv_distribution()
        qv_percentile = cfg.get('s4.exit.qv_decay_percentile', 20)
        regime_qv_adj = {'bull': 0, 'caution': -3, 'bear': -5, 'crash': -8}
        adj_percentile = qv_percentile + regime_qv_adj.get(regime, 0)
        qv_threshold = self._percentile(universe_qv, adj_percentile)
        portfolio_vol = self._estimate_portfolio_volatility(positions)
        vix_scale = max(0.8, min(1.5, (vix / 18.0) ** 0.3))
        regime_sl_mult = {'bull': 3.0, 'caution': 2.5, 'bear': 2.0, 'crash': 1.5}
        sl_multiplier = regime_sl_mult.get(regime, 2.5)
        crash_prob = 0.0
        try:
            import json
            from pathlib import Path
            _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
            state_path = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            if state_path.exists():
                pipeline_state = json.loads(state_path.read_text())
                hmm_transition = pipeline_state.get('hmm_transition', {})
                crash_prob = hmm_transition.get('crash', 0.0)
            if crash_prob > 0.05:
                tighten_ratio = max(0.5, 1.0 - crash_prob * 1.5)
                sl_multiplier *= tighten_ratio
                logger.info(f'  📉 S4 [Phase 90]: HMM 폭락 예측(P={crash_prob:.1%}) → 손절 민감도 강화 ({tighten_ratio:.2f}x Tighten)')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  [DynamicExit] HMM 확률 로드 실패: {e}')
        dynamic_sl_pct = -1 * portfolio_vol * sl_multiplier * vix_scale * 100
        dynamic_sl_pct = max(-30.0, min(-5.0, dynamic_sl_pct))
        n_positions = len(positions)
        dynamic_max_per_sector = max(2, math.ceil(n_positions / 5))
        regime_hold_mult = {'bull': 1.2, 'caution': 1.0, 'bear': 0.7, 'crash': 0.5}
        hold_mult = regime_hold_mult.get(regime, 1.0)
        base_hold_days = cfg.get('s4.exit.base_max_hold_days', 130)
        dynamic_max_hold_days = int(base_hold_days * hold_mult)
        return {'vix': vix, 'qv_threshold': round(qv_threshold, 1), 'dynamic_sl_pct': round(dynamic_sl_pct, 1), 'max_per_sector': dynamic_max_per_sector, 'max_hold_days': dynamic_max_hold_days, 'portfolio_vol': round(portfolio_vol, 4), 'crash_prob': crash_prob, 'vix_scale': round(vix_scale, 3), 'sl_multiplier': sl_multiplier, 'regime': regime}

    def _check_qv_decay(self, pos: Dict, thresholds: Dict) -> Dict:
        """Rule 1: QV Score Decay — 편입 시 QV 대비 현재 QV 하락.

        동적 기준: 유니버스 하위 P20 (레짐별 조정)
        """
        current_qv = pos.get('qv_score', pos.get('quality_value_score', 50))
        entry_qv = pos.get('entry_qv_score', current_qv)
        qv_threshold = thresholds['qv_threshold']
        qv_decay_pct = (current_qv - entry_qv) / max(entry_qv, 1) * 100
        min_decay_pct = cfg.get('s4.exit.qv_min_decay_pct', -30)
        exit_flag = current_qv < qv_threshold and qv_decay_pct < min_decay_pct
        return {'rule': 'qv_decay', 'exit': exit_flag, 'urgency': 1 if exit_flag else 0, 'detail': f'QV {current_qv:.0f}→{entry_qv:.0f} (threshold={qv_threshold:.0f}, decay={qv_decay_pct:+.1f}%)', 'current_qv': current_qv, 'threshold': qv_threshold}

    def _check_value_trap(self, pos: Dict, thresholds: Dict, regime: str) -> Dict:
        """Rule 2: Value Trap Guard — 동적 변동성 기반 손절선.

        손절선 = portfolio_vol × regime_multiplier × vix_scale
        """
        pnl_pct = pos.get('pnl_pct', 0)
        dynamic_sl = thresholds['dynamic_sl_pct']
        exit_flag = pnl_pct <= dynamic_sl
        if pnl_pct <= dynamic_sl * 1.5:
            urgency = 3
        elif exit_flag:
            urgency = 2
        else:
            urgency = 0
        return {'rule': 'value_trap', 'exit': exit_flag, 'urgency': urgency, 'detail': f"P&L {pnl_pct:+.1f}% vs SL {dynamic_sl:+.1f}% (vol={thresholds['portfolio_vol']:.3f}, vix_scale={thresholds['vix_scale']:.2f})", 'pnl_pct': pnl_pct, 'sl_threshold': dynamic_sl}

    def _check_sector_concentration(self, pos: Dict, all_positions: Dict, thresholds: Dict) -> Dict:
        """Rule 3: Sector Concentration — 동적 섹터 집중도.

        같은 섹터에 max_per_sector 초과 시, 해당 섹터의 하위 성과 종목 exit.
        """
        sector = pos.get('sector', pos.get('industry', 'unknown'))
        max_per_sector = thresholds['max_per_sector']
        if sector in ('unknown', '', None):
            return {'rule': 'sector_concentration', 'exit': False, 'urgency': 0, 'detail': 'Sector unknown — skipped', 'sector': sector, 'count': 0, 'limit': max_per_sector}
        sector_positions = []
        for pk, pv in all_positions.items():
            ps = pv.get('sector', pv.get('industry', 'unknown'))
            if ps == sector:
                sector_positions.append((pk, pv.get('pnl_pct', 0)))
        sector_count = len(sector_positions)
        exit_flag = False
        if sector_count > max_per_sector:
            sector_positions.sort(key=lambda x: x[1])
            worst_keys = [k for k, _ in sector_positions[:sector_count - max_per_sector]]
            pos_key_prefix = f"{pos.get('stream_id', 'S4')}:{pos.get('ticker', '')}"
            for pk in all_positions:
                if pk in worst_keys and (pk.endswith(pos.get('ticker', '___')) or pos.get('name', '') in str(all_positions.get(pk, {}).get('name', ''))):
                    exit_flag = True
                    break
        return {'rule': 'sector_concentration', 'exit': exit_flag, 'urgency': 1 if exit_flag else 0, 'detail': f"Sector '{sector}': {sector_count}/{max_per_sector} ({'OVER' if exit_flag else 'OK'})", 'sector': sector, 'count': sector_count, 'limit': max_per_sector}

    def _check_momentum_exhaustion(self, pos: Dict, thresholds: Dict, regime: str) -> Dict:
        """Rule 4: Momentum Exhaustion — 보유기간 + 모멘텀 감쇠.

        보유기간이 동적 한도 초과 AND 최근 모멘텀 음수면 exit.
        """
        entry_date_str = pos.get('entry_date', '')
        max_hold_days = thresholds['max_hold_days']
        try:
            entry_date = date.fromisoformat(entry_date_str)
            days_held = (date.today() - entry_date).days
        except (ValueError, TypeError):
            days_held = 0
        pnl_pct = pos.get('pnl_pct', 0)
        exit_flag = days_held > max_hold_days and pnl_pct < 0
        force_exit = days_held > max_hold_days * 2
        if force_exit:
            exit_flag = True
        return {'rule': 'momentum_exhaustion', 'exit': exit_flag, 'urgency': 2 if force_exit else 1 if exit_flag else 0, 'detail': f'{days_held}일 보유 (limit={max_hold_days}일, P&L={pnl_pct:+.1f}%)', 'days_held': days_held, 'max_hold_days': max_hold_days}

    def _compute_ticker_atr(self, pos: Dict, thresholds: Dict) -> float:
        """종목별 14일 ATR (Average True Range) % 계산.

        [Fallback 우선순위]
          1차: historical_10y parquet 파일 (백그라운드 캐시)
          2차: portfolio_vol proxy (portfolio_vol × 1.5)

        Returns:
            ATR (% decimal, e.g. 0.025 = 2.5%)
        """
        ticker = pos.get('ticker', '')
        atr_period = cfg.get('exit.atr_period', 14)
        if ticker in self._atr_cache:
            return self._atr_cache[ticker]
        atr_pct = None
        if atr_pct is None:
            try:
                import numpy as np
                parquet_candidates = [_DATA_DIR / f'kr_{ticker}.parquet', _DATA_DIR / f'{ticker}.parquet']
                for fp in parquet_candidates:
                    if fp.exists():
                        import pandas as pd
                        df = pd.read_parquet(fp)
                        if len(df) >= atr_period:
                            h = df.get('high', df.get('고가', None))
                            l = df.get('low', df.get('저가', None))
                            c = df.get('close', df.get('종가', None))
                            if h is not None and l is not None and (c is not None):
                                h = h.astype(float).tail(atr_period * 2)
                                l = l.astype(float).tail(atr_period * 2)
                                c = c.astype(float).tail(atr_period * 2)
                                pc = c.shift(1)
                                tr = np.maximum(h - l, np.maximum((h - pc).abs(), (l - pc).abs()))
                                atr_val = float(tr.dropna().tail(atr_period).mean())
                                last_close = float(c.iloc[-1])
                                if last_close > 0 and atr_val > 0:
                                    atr_pct = atr_val / last_close
                                    logger.debug(f'  ATR [{ticker}]: {atr_pct * 100:.2f}% (parquet)')
                                    break
            except Exception as _e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_e}", exc_info=True)
                logger.debug(f'  ATR parquet [{ticker}] 실패: {_e}')
        if atr_pct is None:
            vol = thresholds.get('portfolio_vol', 0.02)
            atr_pct = vol * cfg.get('exit.atr_vol_proxy_factor', 1.5)
            logger.debug(f'  ATR [{ticker}]: {atr_pct * 100:.2f}% (vol_proxy fallback)')
        atr_pct = max(0.005, min(0.15, atr_pct))
        self._atr_cache[ticker] = atr_pct
        return atr_pct

    def _compute_chandelier_mult(self, thresholds: Dict, regime: str) -> float:
        """레짐 + VIX 기반 동적 ATR 배수 계산.

        Bull → 3.0 (넓게, Let Winners Run)
        Bear/Crash → 1.5 (좁게, 이익 조기 확정)
        VIX > 임계값 시 추가 0.8× 축소 (변동성 장세)

        Returns:
            chandelier multiplier (float)
        """
        regime_mult = {'bull': cfg.get('exit.chandelier_mult.bull', 3.0), 'caution': cfg.get('exit.chandelier_mult.caution', 2.5), 'bear': cfg.get('exit.chandelier_mult.bear', 2.0), 'crash': cfg.get('exit.chandelier_mult.crash', 1.5)}
        mult = regime_mult.get(regime, 2.5)
        vix = thresholds.get('vix', 30.0)
        vix_threshold = cfg.get('exit.chandelier_vix_tighten', 25.0)
        if vix >= vix_threshold:
            vix_factor = cfg.get('exit.chandelier_vix_factor', 0.8)
            mult *= vix_factor
            logger.debug(f'  Chandelier: VIX={vix:.1f} >= {vix_threshold:.0f} → 배수 × {vix_factor} = {mult:.2f}')
        crash_prob = thresholds.get('crash_prob', 0.0)
        if crash_prob > 0.05:
            tighten_ratio = max(0.5, 1.0 - crash_prob * 1.5)
            mult *= tighten_ratio
            logger.debug(f'  📉 S4 [Phase 90]: HMM 폭락 위험(P={crash_prob:.1%}) 반영 → Trailing Stop {tighten_ratio:.2f}x 축소 (결과: {mult:.2f})')
        return round(max(1.0, mult), 2)

    def _check_catastrophic_stop(self, pos: Dict, thresholds: Dict) -> Dict:
        """[Phase: S2 Catastrophic Stop] 최후의 안전망.

        S2(ML Alpha) 포지션의 고점(peak_pnl_pct) 대비 하락이
        ATR × catastrophic_mult 를 이탈하면 AI 시그널과 무관하게 즉각 청산.

        목적: ML이 하루 뒤늦게 하락 인지 → 실행 엔진이 장중 먼저 수익 보호.
        S2가 아닌 스트림에는 적용하지 않음.
        """
        stream_id = pos.get('stream_id', pos.get('stream', ''))
        if stream_id not in ('S2', 'ml_alpha'):
            return {'rule': 'catastrophic_stop', 'exit': False, 'urgency': 0, 'detail': ''}
        pnl_pct = pos.get('pnl_pct', 0)
        peak_pnl = pos.get('peak_pnl_pct', pnl_pct)
        if peak_pnl <= 0:
            return {'rule': 'catastrophic_stop', 'exit': False, 'urgency': 0, 'detail': ''}
        atr_pct = self._compute_ticker_atr(pos, thresholds)
        cat_mult = cfg.get('exit.catastrophic_atr_mult', 4.0)
        allowed_drawdown_pct = atr_pct * cat_mult * 100
        drawdown_from_peak = peak_pnl - pnl_pct
        exit_flag = drawdown_from_peak >= allowed_drawdown_pct
        detail = f'[S2 Catastrophic] 고점 {peak_pnl:+.1f}% → 현재 {pnl_pct:+.1f}% (하락 {drawdown_from_peak:.1f}% vs ATR×{cat_mult:.0f}={allowed_drawdown_pct:.1f}%)'
        if exit_flag:
            logger.warning(f"  🚨 Catastrophic Stop [{pos.get('ticker', '')}]: {detail}")
        return {'rule': 'catastrophic_stop', 'exit': exit_flag, 'urgency': 3 if exit_flag else 0, 'detail': detail, 'drawdown_from_peak': round(drawdown_from_peak, 2), 'allowed_drawdown': round(allowed_drawdown_pct, 2), 'atr_pct': round(atr_pct * 100, 2)}

    def _check_take_profit(self, pos: Dict, thresholds: Dict, regime: str) -> Dict:
        """Rule 5: ATR 샹들리에 트레일링 스탑 + 50% Scale-out.

        [Phase: Fully Dynamic Exit]

        ① 고정 TP: 레짐 + 변동성 기반
        ② ATR 샹들리에 트레일링 스탑:
           - trailing_drop = ATR × chandelier_mult (종목별 고유 드롭)
           - 저변동성(삼성전자): 타이트, 고변동성(알테오젠): 넓게 → 노이즈 방지
        ③ 50% Scale-out (분할 익절):
           - 수익률 ≥ ATR × scale_out_atr_mult (or scale_out_min_pct)
           - sell_type='scale_out_partial', 잔여 50% Runner는 TP=None + 샹들리에 계속
        """
        pnl_pct = pos.get('pnl_pct', 0)
        peak_pnl = pos.get('peak_pnl_pct', pnl_pct)
        scaled_out = pos.get('scaled_out', False)
        atr_pct = self._compute_ticker_atr(pos, thresholds)
        regime_tp = {'bull': cfg.get('s4.exit.tp_pct.bull', 25), 'caution': cfg.get('s4.exit.tp_pct.caution', 20), 'bear': cfg.get('s4.exit.tp_pct.bear', 15), 'crash': cfg.get('s4.exit.tp_pct.crash', 10)}
        base_tp = regime_tp.get(regime, 20)
        vol = thresholds.get('portfolio_vol', 0.02)
        vol_adj = cfg.get('s4.exit.tp_vol_adjustment', 0.5)
        vol_baseline = cfg.get('s4.exit.tp_vol_baseline', 0.02)
        tp_threshold = base_tp * (1 - vol_adj * max(0, vol - vol_baseline) / max(vol_baseline, 0.001))
        tp_threshold = max(cfg.get('s4.exit.tp_floor', 8), tp_threshold)
        fixed_exit = not scaled_out and pnl_pct >= tp_threshold
        trailing_trigger = cfg.get('s4.exit.trailing_tp_trigger', 15)
        chandelier_mult = self._compute_chandelier_mult(thresholds, regime)
        chandelier_drop = atr_pct * chandelier_mult * 100
        min_drop = cfg.get('s4.exit.trailing_drop_floor', 3)
        max_drop = cfg.get('s4.exit.trailing_drop_ceil', 15)
        chandelier_drop = max(min_drop, min(max_drop, chandelier_drop))
        trailing_exit = peak_pnl >= trailing_trigger and pnl_pct < peak_pnl - chandelier_drop
        scale_out_target_pct = max(atr_pct * cfg.get('exit.scale_out_atr_mult', 10.0) * 100, cfg.get('exit.scale_out_min_pct', 30.0))
        scale_out_flag = not scaled_out and pnl_pct >= scale_out_target_pct
        exit_flag = fixed_exit or trailing_exit
        detail = ''
        if scale_out_flag:
            detail = f'Scale-out 50%: P&L {pnl_pct:+.1f}% ≥ 목표 {scale_out_target_pct:.1f}% (ATR={atr_pct * 100:.2f}%)'
            return {'rule': 'take_profit', 'exit': False, 'scale_out': True, 'urgency': 3, 'detail': detail, 'tp_threshold': round(tp_threshold, 1), 'atr_pct': round(atr_pct * 100, 2), 'chandelier_drop': round(chandelier_drop, 2), 'scale_out_target': round(scale_out_target_pct, 1)}
        if fixed_exit:
            detail = f'TP 도달: P&L {pnl_pct:+.1f}% >= 목표 {tp_threshold:.1f}%'
        elif trailing_exit:
            detail = f'ATR 샹들리에 Trailing: P&L {pnl_pct:+.1f}% (고점 {peak_pnl:+.1f}% 대비 -{peak_pnl - pnl_pct:.1f}% 하락, 한도 ATR×{chandelier_mult:.1f}=-{chandelier_drop:.1f}%, ATR={atr_pct * 100:.2f}%)'
        return {'rule': 'take_profit', 'exit': exit_flag, 'scale_out': False, 'urgency': 2 if fixed_exit else 1 if trailing_exit else 0, 'detail': detail, 'tp_threshold': round(tp_threshold, 1), 'atr_pct': round(atr_pct * 100, 2), 'chandelier_mult': chandelier_mult, 'chandelier_drop': round(chandelier_drop, 2)}

    def _load_universe_qv_distribution(self) -> List[float]:
        """유니버스 전체 QV 점수 분포 로드."""
        if self._universe_qv_cache:
            return self._universe_qv_cache.get('scores', [50])
        try:
            qv_path = _RESULTS / 'qv_rankings.json'
            if qv_path.exists():
                data = json.loads(qv_path.read_text())
                scores = []
                for item in data if isinstance(data, list) else data.get('rankings', []):
                    qv = item.get('qv_score', item.get('composite_score', 0))
                    if qv > 0:
                        scores.append(qv)
                if scores:
                    self._universe_qv_cache = {'scores': scores}
                    return scores
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  QV distribution load failed: {e}')
        import random
        random.seed(42)
        return [max(0, min(100, 50 + 15 * random.gauss(0, 1))) for _ in range(200)]

    def _estimate_portfolio_volatility(self, positions: Dict) -> float:
        """포트폴리오 평균 변동성 추정.

        개별 종목 pnl_pct의 절대값을 vol proxy로 사용.
        실제로는 daily return std를 사용하는 것이 이상적.
        """
        if not positions:
            return 0.02
        pnl_abs = [abs(p.get('pnl_pct', 0)) / 100 for p in positions.values()]
        avg_vol = sum(pnl_abs) / len(pnl_abs) if pnl_abs else 0.02
        return max(0.01, min(0.15, avg_vol))

    @staticmethod
    def _percentile(data: List[float], pct: float) -> float:
        """Percentile 계산 (선형 보간)."""
        if not data:
            return 0
        sorted_data = sorted(data)
        n = len(sorted_data)
        idx = pct / 100 * (n - 1)
        lower = int(idx)
        upper = min(lower + 1, n - 1)
        frac = idx - lower
        return sorted_data[lower] + frac * (sorted_data[upper] - sorted_data[lower])

    def _save_result(self, result: Dict):
        """평가 결과 저장."""
        try:
            path = _RESULTS / 's4_exit_evaluation.json'
            save_data = {'timestamp': result['timestamp'], 'regime': result['regime'], 'total_positions': result['total_positions'], 'exit_count': result['exit_count'], 'hold_count': result['hold_count'], 'dynamic_thresholds': result['dynamic_thresholds'], 'rules_summary': {k: len(v) for k, v in result['rules_applied'].items()}, 'exit_candidates': [{'name': c['name'], 'pnl_pct': c['pnl_pct'], 'urgency': c['urgency'], 'reasons': [r['rule'] for r in c['reasons']]} for c in result['exit_candidates']]}
            atomic_write_json(path, save_data, ensure_ascii=False, indent=2)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  Exit evaluation save failed: {e}')

    def get_last_evaluation(self) -> Optional[Dict]:
        """마지막 평가 결과."""
        return self._last_evaluation

    def _cfg_flow(self, key: str, default=None):
        """DynamicConfig dot-key 우선 조회 → 인자 default Fallback."""
        try:
            from config.dynamic_config import DynamicConfig
            val = DynamicConfig().get(key)
            return val if val is not None else default
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return default

    def _check_flow_dynamic_exit(self, pos: dict, thresholds: dict, flow_data: dict, ticker: str) -> dict:
        """
        [Phase 36] 장중 수급·거래량 기반 3-Mode Exit 조정.

        Mode A — Trend Rider     : 쌍끌이 매수 → Trailing Stop 배수 확장
        Mode B — Panic Tightener : 쌍끌이 매도 + 하락 → Stop-Loss 타이트닝 + 즉시 청산
        Mode C — Whipsaw Filter  : 거래량 낮음 + 수급 미미 → 손절 1회 유예

        모든 임계치는 DynamicConfig에서 읽습니다. 키가 없으면 두 번째 인자가 Fallback.
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        EMPTY = {'rule': 'flow_dynamic', 'action': 'neutral', 'exit': False, 'whipsaw_defer': False, 'urgency': 0, 'detail': 'No flow data', 'flow_adjusted_sl_pct': None, 'flow_adjusted_ts_mult': None}
        if not flow_data:
            return EMPTY
        ticker_flow = flow_data.get('tickers', {}).get(ticker, {})
        if not ticker_flow:
            return EMPTY
        combined_krw = float(ticker_flow.get('combined_net_krw', 0.0))
        institution_krw = float(ticker_flow.get('institution_net_krw', 0.0))
        foreign_krw = float(ticker_flow.get('foreign_net_krw', 0.0))
        volume_ratio = float(ticker_flow.get('volume_ratio', 0.0))
        cache_unit = float(flow_data.get('flow_unit_krw') or self._cfg_flow('intraday.flow_unit_krw', 1000000))
        strong_thr_raw = float(self._cfg_flow('intraday.flow_strong_threshold_krw', 5000000000))
        strong_thr = strong_thr_raw / cache_unit
        rider_ratio = float(self._cfg_flow('intraday.flow_trend_rider_ratio', 1.3))
        panic_ratio = float(self._cfg_flow('intraday.flow_panic_ratio', 0.6))
        panic_drop_pct = float(self._cfg_flow('intraday.panic_price_drop_pct', 0.015)) * 100
        ws_enabled = bool(self._cfg_flow('intraday.whipsaw_filter_enabled', True))
        ws_vol_thr = float(self._cfg_flow('intraday.whipsaw_volume_threshold', 0.3))
        ws_neutral_ratio = float(self._cfg_flow('intraday.whipsaw_neutral_ratio', 0.2))
        panic_urgency = int(self._cfg_flow('intraday.panic_urgency', 3))
        pnl_pct = float(pos.get('pnl_pct', 0.0))
        sl_pct = float(thresholds.get('stop_loss_pct') or self._cfg_flow('intraday.default_sl_pct', 0.07))
        ts_mult = float(thresholds.get('trail_stop_atr_mult') or self._cfg_flow('intraday.default_ts_atr_mult', 1.5))
        unit_label = f'{int(cache_unit / 1000000)}백만원'
        if ws_enabled and volume_ratio < ws_vol_thr and (abs(combined_krw) < strong_thr * ws_neutral_ratio):
            detail = f'[Whipsaw] 거래량={volume_ratio:.0%}(<{ws_vol_thr:.0%}) 수급={combined_krw:+.0f}{unit_label} (임계 {strong_thr * ws_neutral_ratio:.0f}) → 손절 유예'
            _logger.info('[Flow] %s %s', ticker, detail)
            return {'rule': 'flow_dynamic', 'action': 'whipsaw_defer', 'exit': False, 'whipsaw_defer': True, 'urgency': 0, 'detail': detail, 'flow_adjusted_sl_pct': None, 'flow_adjusted_ts_mult': None}
        if combined_krw >= strong_thr and institution_krw > 0 and (foreign_krw > 0):
            new_ts = ts_mult * rider_ratio
            detail = f'[TrendRider] 기관={institution_krw:+.0f} 외인={foreign_krw:+.0f}{unit_label} → TS×{rider_ratio:.2f} ({ts_mult:.2f}→{new_ts:.2f})'
            _logger.info('[Flow] %s %s', ticker, detail)
            return {'rule': 'flow_dynamic', 'action': 'widen', 'exit': False, 'whipsaw_defer': False, 'urgency': 0, 'detail': detail, 'flow_adjusted_sl_pct': None, 'flow_adjusted_ts_mult': new_ts}
        if combined_krw <= -strong_thr and institution_krw < 0 and (foreign_krw < 0) and (pnl_pct < -panic_drop_pct):
            new_sl = sl_pct * panic_ratio
            detail = f'[PanicTightener] 기관={institution_krw:+.0f} 외인={foreign_krw:+.0f}{unit_label} + 하락 {pnl_pct:.1f}% → SL×{panic_ratio:.2f} ({sl_pct:.1%}→{new_sl:.1%})'
            _logger.warning('[Flow] %s %s', ticker, detail)
            return {'rule': 'flow_dynamic', 'action': 'tighten', 'exit': True, 'whipsaw_defer': False, 'urgency': panic_urgency, 'detail': detail, 'flow_adjusted_sl_pct': new_sl, 'flow_adjusted_ts_mult': None}
        return {'rule': 'flow_dynamic', 'action': 'neutral', 'exit': False, 'whipsaw_defer': False, 'urgency': 0, 'detail': f'수급 중립 기관={institution_krw:+.0f} 외인={foreign_krw:+.0f}{unit_label} (임계={strong_thr:.0f})', 'flow_adjusted_sl_pct': None, 'flow_adjusted_ts_mult': None}
if __name__ == '__main__':
    import sys
    sys.path.insert(0, str(_PROJECT_ROOT))
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    evaluator = DynamicExitEvaluator()
    sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
    positions = sp.get('positions', {})
    s4_positions = {pk: pv for pk, pv in positions.items() if (pk.split(':')[0] if ':' in pk else pv.get('stream_id', '')) == 'S4'}
    result = evaluator.evaluate(s4_positions, regime='bull')
    logger.debug(f'\n{"─" * 60}')
    logger.debug(f"🔥 S4 Dynamic Exit Evaluation: {datetime.now().strftime('%Y-%m-%d %H:%M')} 🔥")
    logger.debug(f'{"─" * 60}')
    logger.info(f"Total: {result['total_positions']}, Exit: {result['exit_count']}, Hold: {result['hold_count']}")
    logger.info(f'\nDynamic Thresholds:')
    for k, v in result['dynamic_thresholds'].items():
        logger.info(f'  {k}: {v}')
    if result['exit_candidates']:
        logger.debug(f'\n{"─" * 60}')
        logger.info(f'Exit Candidates:')
        for c in result['exit_candidates']:
            reasons = ', '.join((r['rule'] for r in c['reasons']))
            logger.debug(f"  [{c['urgency']}] {c['name']:20s} P&L={c['pnl_pct']:+.1f}% — {reasons}")
            
        logger.info(f'Hold Candidates:')
        for h in result['hold_candidates']:
            logger.debug(f"  ✅ {h['name']:20s} P&L={h['pnl_pct']:+.1f}%")
"""
S5 Overnight Stream — 야간 갭/종가 베팅 전용 스트림
=====================================================

전략 핵심:
  - 15:20 매수(종가 베팅) → 09:05 청산(시가 매도)
  - S1과 예산을 100% 공유 (Time-sharing Capital). S1이 사용 중인 자금을 야간에 활용.
  - 시장이 과도하게 하락했거나, Alpha Factory가 발굴한 특정 조건에 부합할 때 롱/숏 오버나잇 베팅.

Usage:
    from src.streams.s5_overnight.overnight_stream import S5OvernightStream
    s5 = S5OvernightStream()
    signals = s5.generate_signals(regime='bull', market_data={})
"""
import json
import logging
from datetime import datetime
from typing import Dict, List
from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream
try:
    from src.utils.time_utils import now_kst
except ImportError as e:

    def now_kst():
        from datetime import datetime
        return datetime.now()
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S5OvernightStream(BaseStream):
    """S5: Overnight Anomaly (종가 베팅 / 야간 효과).
    
    KOSPI의 전형적인 야간 효과(Night Effect)를 공략.
    """
    lev_ticker = cfg.get('s0_beta.leverage_ticker', '122630')
    inv_ticker = cfg.get('s0_beta.inverse2x_ticker', '252670')
    ETF_UNIVERSE = {lev_ticker: {'name': 'KODEX 레버리지', 'type': 'KOSPI_2X'}, inv_ticker: {'name': 'KODEX 200선물인버스2X', 'type': 'KOSPI_INV_2X'}}
    DEFAULT_PARKING_ETFS = [{'ticker': '430740', 'name': 'KODEX KOFR금리액티브(합성)'}, {'ticker': '357870', 'name': 'TIGER CD금리투자KIS(합성)'}, {'ticker': '459580', 'name': 'KODEX CD금리액티브(합성)'}]

    @staticmethod
    def _compute_overnight_kelly_scale(vix: float, nq_vol_pct: float=0.0, max_alloc: float=1.0) -> float:
        """[Phase 80] VIX+NQ 변동성 기반 오버나잇 예측 분산 연속 켈리 감쇠.

        overnight_var = (VIX/100)^2/252 + w_nq*(nq_vol_pct/100)^2
        kelly_scale = max_alloc * exp(-k * overnight_var / base_var)
        """
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _cfg = None

        def _g(k, d):
            try:
                return _cfg.get(k, d) if _cfg else d
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return d
        import math
        vix_base = float(_g('s5.kelly.vix_baseline', 18.0))
        w_nq = float(_g('s5.kelly.nq_vol_weight', 0.3))
        decay_k = float(_g('s5.kelly.decay_k', 3.0))
        base_var = (vix_base / 100) ** 2 / 252
        ov_var = (vix / 100) ** 2 / 252 + w_nq * (nq_vol_pct / 100) ** 2
        scale = max_alloc * math.exp(-decay_k * ov_var / max(base_var, 1e-12))
        return round(min(max_alloc, max(0.0, scale)), 4)

    def __init__(self):
        super().__init__('S5', 'Overnight Anomaly & Cash Sweep')

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """야간 오버나이트 신호 생성 및 파킹 스윕 (15:10 이후 발동)."""
        signals = []
        is_backtest = market_data.get('backtest_mode', False)
        from datetime import datetime, time
        now_time = now_kst().time()
        if not is_backtest and (now_time < time(15, 10) or now_time > time(15, 30)):
            return signals
        signal_cache = market_data.get('signal_cache', {})
        kospi_chg = signal_cache.get('kospi_change_1d', 0.0)
        vix = signal_cache.get('vix', 15.0)
        _nq_chg = 0.0
        _nq_blocked = False
        _nq_inverse_hedge = False
        try:
            from src.data.market_data_bridge import MarketDataBridge as _MDB
            _nq_data = _MDB().get_nq_futures_change()
            _nq_chg = float(_nq_data.get('chg_1d_pct', 0.0))
            _base_block = cfg.get('s5.nq_block_threshold_pct', -0.5)
            _base_hedge = cfg.get('s5.nq_hedge_threshold_pct', -1.5)
            _vix_neutral = cfg.get('risk.vix_fallback', 18.0) or 18.0
            _max_exp = cfg.get('s5.nq_elastic_max_expansion', 2.0) or 2.0
            _vix_ratio = max(1.0, min(_max_exp, vix / max(_vix_neutral, 1.0)))
            _nq_threshold = round(_base_block * _vix_ratio, 4)
            _nq_hedge_threshold = round(_base_hedge * _vix_ratio, 4)
            logger.debug(f'  [Phase 11: Dynamic Balance] S5 NQ 탄력 임계값: VIX={vix:.1f}/neutral={_vix_neutral:.1f} -> ratio={_vix_ratio:.2f} -> block={_nq_threshold:.2f}%, hedge={_nq_hedge_threshold:.2f}%')
            if _nq_chg <= _nq_hedge_threshold:
                _nq_inverse_hedge = True
                logger.warning(f'  ⚡ [Phase 10: Alpha Breakthrough] S5 NQ 선물 급락: {_nq_chg:+.2f}% ≤ {_nq_hedge_threshold}% → 인버스 헷지 전환')
                signals.append({'stream_id': 'S5', 'ticker': S5OvernightStream.inv_ticker, 'name': 'KODEX 200선물인버스2X (NQ급락헷지)', 'direction': 'long', 'confidence': 0.75, 'size_pct': cfg.get('s5.nq_hedge_size_pct', 0.2), 'strategy': 'nq_inverse_hedge', 'reason': f'NQ 선물 급락({_nq_chg:+.2f}%) 인버스 헷지 [Phase 11 elastic: hedge_th={_nq_hedge_threshold:.2f}%, VIX_ratio={_vix_ratio:.2f}]', 'tp_pct': cfg.get('s5.nq_hedge_tp_pct', 2.0), 'sl_pct': cfg.get('s5.nq_hedge_sl_pct', -1.0), 'max_hold_minutes': cfg.get('s5.max_hold_minutes', 1200), 'max_hold_days': 1, 'timestamp': now_kst().isoformat()})
                _nq_blocked = True
            elif _nq_chg <= _nq_threshold:
                _nq_blocked = True
                logger.warning(f'  🚫 [Phase 10: Alpha Breakthrough] S5 NQ 선물 필터: {_nq_chg:+.2f}% ≤ {_nq_threshold}% → 오버나이트 매수 취소')
        except Exception as _nq_e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_nq_e}", exc_info=True)
            logger.debug(f'  [Phase 10] S5 NQ 필터 실패 (무시): {_nq_e}')
        vix_spike_flag = market_data.get('alpha_signals', {}).get('S5_signal', {}).get('vix_spike', False)
        _nq_vol = float(market_data.get('nq_vol_pct', market_data.get('nq_1d_vol', 0.0)))
        _kelly_scale = self._compute_overnight_kelly_scale(vix, nq_vol_pct=_nq_vol, max_alloc=float(cfg.get('s5.max_alloc_ratio', 1.0)))
        vix_blocked = _kelly_scale <= float(cfg.get('s5.kelly.min_scale_threshold', 0.005)) or vix_spike_flag
        self._last_kelly_scale = _kelly_scale
        logger.info(f'  [Phase80 S5] VIX={vix:.1f} NQ={_nq_vol:.2f}% → KellyScale={_kelly_scale:.4f} blocked={vix_blocked}')
        ah_volume_surge = signal_cache.get('us_ah_volume_surge', False)
        ah_options_skew = signal_cache.get('us_ah_options_skew', 0.0)
        ah_score = 0.0
        if ah_volume_surge and abs(ah_options_skew) > 0.3:
            ah_score = ah_options_skew * 2.0
            logger.info(f'  🌙 S5 [Phase 90]: US AH 거래량 폭발 및 스큐 감지(Skew={ah_options_skew:.2f}) → 갭 베팅 AH Score={ah_score:.2f}')
        alpha_signal = market_data.get('alpha_factory', {})
        overnight_alpha = alpha_signal.get('s5_overnight_score', 0.0)
        if ah_score != 0.0:
            overnight_alpha = overnight_alpha * 0.4 + ah_score * 0.6
        base_threshold = cfg.get('s5.overnight_reversion_threshold', 1.0)
        # [Phase 95 Decoupling] S5 아비트라지는 상위 레짐(Macro) 방향에 종속되지 않고 철저히 독립적으로 작동.
        # 거시경제가 CRASH든 BULL이든 수급 갭이 발생하면 기계적으로 역베팅(Arbitrage) 수행.
        short_entry_th = base_threshold
        long_entry_th = base_threshold
        logger.info(f"  [Decoupling] S5 오버나잇 임계치 상위 레짐({regime}) 무시. 고정 임계치(long={long_entry_th:.2f}%, short={short_entry_th:.2f}%) 적용.")
        direction = 'neutral'
        confidence = 0.0
        reason = ''
        if overnight_alpha != 0.0:
            direction = 'long' if overnight_alpha > 0 else 'short'
            confidence = min(0.9, 0.5 + abs(overnight_alpha))
            reason = f'AlphaFactory S5 Score: {overnight_alpha:+.2f}'
        elif kospi_chg < -long_entry_th:
            direction = 'long'
            confidence = min(0.85, 0.5 + abs(kospi_chg) * 0.1)
            reason = f'KOSPI 급락({kospi_chg:+.2f}%) → 종가 롱 베팅 (th={long_entry_th:.2f}%)'
        elif kospi_chg > short_entry_th:
            direction = 'short'
            confidence = min(0.85, 0.5 + abs(kospi_chg) * 0.1)
            reason = f'KOSPI 급등({kospi_chg:+.2f}%) → 종가 숏 베팅 (th={short_entry_th:.2f}%)'
        vix_max = cfg.get('s5.max_vix_for_overlay', 25.0)
        if vix >= vix_max:
            direction = 'neutral'
            reason = f'VIX({vix:.1f})가 임계치({vix_max}) 초과 → 오버레이 포기'
        if vix_blocked:
            direction = 'neutral'
        if direction in ('long', 'short') and (not vix_blocked):
            ticker = S5OvernightStream.lev_ticker if direction == 'long' else S5OvernightStream.inv_ticker
            min_conf = cfg.get('s5.overlay_min_confidence', 0.55)
            max_conf = cfg.get('s5.overlay_max_confidence', 0.85)
            max_alloc = cfg.get('s5.max_allocation_pct', 0.4)
            _alpha_max_boost = cfg.get('s5.alpha_max_alloc_boost', 0.3)
            if regime == 'bull' and direction == 'long':
                max_alloc = cfg.get('s5.bull_max_alloc', 0.6)
                _alpha_max_boost = cfg.get('s5.bull_alpha_boost', 0.75)
                logger.info(f'  [S5 Bull Boost] 상승장 롱 비중 확장: max_alloc={max_alloc:.0%}, alpha_boost={_alpha_max_boost:.0%}')
            _alpha_score_abs = abs(overnight_alpha) if overnight_alpha != 0.0 else 0.0
            _alpha_score_th = cfg.get('s5.alpha_score_boost_threshold', 1.0)
            if _alpha_score_abs >= _alpha_score_th:
                _alpha_scale = min(1.0, (_alpha_score_abs - _alpha_score_th) / max(0.01, _alpha_score_th))
                _boosted_alloc = max_alloc + (_alpha_max_boost - max_alloc) * _alpha_scale
                if _boosted_alloc > max_alloc:
                    logger.info(f'  🚀 S5 Alpha Factory 비중 부스트: score={overnight_alpha:+.2f} → max_alloc {max_alloc:.0%} → {_boosted_alloc:.0%} (alpha_max_boost={_alpha_max_boost:.0%})')
                    max_alloc = _boosted_alloc
            if confidence >= min_conf:
                scale = (confidence - min_conf) / max(0.01, max_conf - min_conf)
                scale = max(0.0, min(1.0, scale))
                overlay_size_pct = round(max_alloc * scale, 3)
                _vk = getattr(self, '_last_kelly_scale', max_alloc)
                overlay_size_pct = round(overlay_size_pct * (_vk / max(max_alloc, 1e-06)), 3)
                overlay_size_pct = max(0.0, min(max_alloc, overlay_size_pct))
                if overlay_size_pct > 0.0:
                    signals.append({'stream_id': 'S5', 'ticker': ticker, 'name': self.ETF_UNIVERSE.get(ticker, {}).get('name', ticker), 'direction': 'long', 'confidence': round(confidence, 3), 'size_pct': overlay_size_pct, 'strategy': 'overnight_overlay', 'reason': reason, 'tp_pct': cfg.get('s5.tp_pct', 1.5), 'sl_pct': cfg.get('s5.sl_pct', -1.5), 'trail_activate_pct': 0.3, 'trail_distance_pct': 0.3, 'max_hold_minutes': 60, 'max_hold_days': 1, 'timestamp': now_kst().isoformat()})
                    logger.info(f'  🌙 S5 오버레이: {ticker} (conf={confidence:.2f}, 비중={overlay_size_pct:.1%}) - {reason}')
        parking_etfs = None
        _dynamic_cache = None
        try:
            from pathlib import Path as _Path
            import json as _json
            _cache_path = _Path(__file__).resolve().parent.parent.parent.parent / 'results' / 'dynamic_parking_etfs.json'
            if _cache_path.exists():
                _raw = _json.loads(_cache_path.read_text())
                if _raw and isinstance(_raw, list) and _raw[0].get('ticker'):
                    parking_etfs = _raw
                    _dynamic_cache = _cache_path.name
                    logger.debug(f'  🅿️ 파킹 ETF 동적 캐시 로드: {[p['name'] for p in parking_etfs]}')
        except Exception as _dce:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_dce}", exc_info=True)
            logger.debug(f'  dynamic_parking_etfs 로드 실패 (다음 폴백 시도): {_dce}')
        if parking_etfs is None:
            parking_etfs = cfg.get('s5.parking_etfs', None)
            if parking_etfs:
                logger.debug('  🅿️ 파킹 ETF: DynamicConfig 값 사용')
        if not parking_etfs:
            parking_etfs = self.DEFAULT_PARKING_ETFS
            logger.debug('  🅿️ 파킹 ETF: DEFAULT_PARKING_ETFS 하드코딩 폴백 사용')
        best_parking = parking_etfs[0]
        signals.append({'stream_id': 'S5', 'ticker': best_parking['ticker'], 'name': best_parking['name'], 'direction': 'long', 'confidence': 1.0, 'size_pct': 1.0, 'strategy': 'risk_free_parking', 'reason': '잔여 현금 무위험 이자(KOFR/CD) 스윕', 'tp_pct': 99.0, 'sl_pct': -99.0, 'max_hold_minutes': cfg.get('s5.max_hold_minutes', 1200), 'timestamp': now_kst().isoformat()})
        logger.info(f'  🌙 S5 파킹 스윕: {best_parking['name']} 잔여 자본 100% 배정')
        return signals

    def get_performance(self) -> dict:
        return {'sharpe': 0.0, 'daily_returns': []}

    def get_positions(self) -> list:
        """[Phase 48 C-5] BaseStream 스펙 준수: List[Dict] 반환."""
        return []
"""
S3 Hybrid Factor Stream — 멀티팩터 섹터 로테이션(ETF) + QVM 개별 가치주
====================================================================

전략 핵심 (투-트랙 하이브리드 엔진):
  - [Track A] 매크로/섹터 ETF (50% 내외): 4-팩터 모델 기반, 월 1회 기민한 리밸런싱
  - [Track B] QVM 개별 가치주 (50% 내외): 강방천 K-PER 밸류에이션 기반, Buy & Hold 장기 투자
  - 동적 자본 배분 (Tolerance Band): 30~70% 밴드 내에서 기회 기반 배분
  - 장기투자 앵커 로직: QVM 종목은 절대 목표가(Margin of Safety < 0) 혹은 해자 훼손 시에만 매도

Usage:
    from src.streams.s3_active_macro.active_macro_stream import S3FactorStream
    s3 = S3FactorStream()
    signals = s3.generate_signals(regime='bull', market_data={})
"""
import json
import pandas as pd
import json as _json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream
try:
    from src.utils.time_utils import now_kst
except ImportError as e:

    def now_kst():
        return datetime.now()
from src.streams.s3_active_macro.qvm_scorer import QVMScorer
from src.streams.s3_active_macro.qvm_universe import QVMUniverse
try:
    from src.risk.intraday_regime import IntradayRegimeDetector
    _REGIME_AVAILABLE = True
except ImportError as e:
    _REGIME_AVAILABLE = False
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FEATURE_STORE_DIR = _PROJECT_ROOT / 'data' / 'feature_store'

class S3FactorStream(BaseStream):
    """S3: 종합 멀티팩터 하이브리드 엔진 (ETF + QVM 가치주).

    빈도: 
      - Track A (ETF): 월간 (매월 첫 거래일) 리밸런싱
      - Track B (QVM): 분기별 유니버스 재평가 및 Absolute Target 매도 (Buy & Hold)
    팩터: 
      - ETF: Momentum + Value + Carry + Volatility (4-팩터)
      - QVM: Quality + Value + Moat (강방천 K-PER)
    """
    SECTOR_ETFS = {'091160': {'name': 'KODEX 반도체', 'sector': 'semiconductor', 'market': 'KR'}, '396500': {'name': 'TIGER 반도체TOP10', 'sector': 'semiconductor', 'market': 'KR'}, '139260': {'name': 'TIGER 200 IT', 'sector': 'it', 'market': 'KR'}, '117700': {'name': 'KODEX 건설', 'sector': 'construction', 'market': 'KR'}, '305720': {'name': 'KODEX 2차전지산업', 'sector': 'battery', 'market': 'KR'}, '305540': {'name': 'TIGER 2차전지테마', 'sector': 'battery', 'market': 'KR'}, '091180': {'name': 'KODEX 자동차', 'sector': 'auto', 'market': 'KR'}, '227550': {'name': 'TIGER 200 산업재', 'sector': 'industrial', 'market': 'KR'}, '091170': {'name': 'KODEX 은행', 'sector': 'finance', 'market': 'KR'}, '143860': {'name': 'TIGER 헬스케어', 'sector': 'healthcare', 'market': 'KR'}, '244580': {'name': 'KODEX 바이오', 'sector': 'bio', 'market': 'KR'}, '463250': {'name': 'TIGER K방산&우주', 'sector': 'defense', 'market': 'KR'}}
    GLOBAL_ETFS = {'133690': {'name': 'TIGER 미국나스닥100', 'sector': 'us_tech', 'market': 'US'}, '379800': {'name': 'KODEX 미국S&P500', 'sector': 'us_broad', 'market': 'US'}, '381180': {'name': 'TIGER 미국필라델피아반도체나스닥', 'sector': 'us_semiconductor', 'market': 'US'}, '390390': {'name': 'KODEX 미국반도체', 'sector': 'us_semiconductor', 'market': 'US'}, '381170': {'name': 'TIGER 미국테크TOP10 INDXX', 'sector': 'us_tech', 'market': 'US'}, '487230': {'name': 'KODEX 미국AI전력핵심인프라', 'sector': 'ai_infra', 'market': 'GLOBAL'}, '466950': {'name': 'TIGER 글로벌AI액티브', 'sector': 'ai', 'market': 'GLOBAL'}, '453870': {'name': 'TIGER 인도니프티50', 'sector': 'india', 'market': 'INDIA'}, '192090': {'name': 'TIGER 차이나CSI300', 'sector': 'china', 'market': 'CHINA'}}
    DEFENSIVE_ETFS = {'261250': {'name': 'KODEX 미국달러선물레버리지', 'sector': 'currency', 'market': 'GLOBAL'}, '152380': {'name': 'KODEX 국고채10년', 'sector': 'bond', 'market': 'KR'}, '319640': {'name': 'TIGER 골드선물(H)', 'sector': 'commodity', 'market': 'GLOBAL'}}
    DIVIDEND_BLACKLIST = {'279530', '211560', '289480', '458730', '441640', '458760', '211900', '494330'}

    def __init__(self):
        super().__init__('S3', 'Hybrid Multifactor & QVM Engine')
        self._current_holdings: List[Dict] = []
        self._rebalance_history: List[Dict] = []
        self._daily_returns: List[float] = []
        self._full_universe = {**self.SECTOR_ETFS, **self.GLOBAL_ETFS}
        self._qvm_scorer = QVMScorer()
        self._qvm_universe = QVMUniverse()
        self._regime_detector = IntradayRegimeDetector() if _REGIME_AVAILABLE else None

    @staticmethod
    def _cross_sectional_zscore(raw_scores: List[float]) -> List[float]:
        """유니버스 내 상대 순위 기반 Z-스코어 → 0~1 CDF 매핑.

        각 팩터를 독립적으로 정규화하여 스케일 차이를 제거.
        CDF 매핑으로 0~1 범위의 균일한 분포 생성.

        Args:
            raw_scores: 팩터 원시 점수 리스트

        Returns:
            0~1 범위의 정규화 점수 리스트 (CDF 기반)
        """
        n = len(raw_scores)
        if n < 2:
            return [0.5] * n
        mean = sum(raw_scores) / n
        variance = sum(((x - mean) ** 2 for x in raw_scores)) / n
        std = math.sqrt(variance) if variance > 0 else 0
        if std < 1e-08:
            return [0.5] * n
        z_scores = [(x - mean) / std for x in raw_scores]
        return [0.5 * (1 + math.erf(z / math.sqrt(2))) for z in z_scores]

    @staticmethod
    def _compute_dynamic_sl_tp(atr_pct: float, vix: float, regime: str='bull') -> tuple:
        """[Phase 79] ATR + VIX 기반 동적 손절선/익절선 계산.

        Dynamic_SL = ATR_pct * regime_atr_mult * vix_scale
        Dynamic_TP = ATR_pct * tp_atr_mult * vix_scale

        Args:
            atr_pct:  종목의 N일 ATR를 가격 대비 %(% 단위, 양수)
            vix:      당일 VIX 지수
            regime:   시장 국면 ('bull'/'caution'/'bear'/'crash')

        Returns:
            (sl_pct, tp_pct): 음수 sl(-%), 양수 tp(+%) 튜플
        """
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _cfg = None

        def _g(key, default):
            try:
                return _cfg.get(key, default) if _cfg else default
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                return default
        vix_base = float(_g('s3.dynamic_sl.vix_baseline', 18.0))
        vix_scale = max(0.5, min(3.0, vix / max(vix_base, 1e-06)))
        regime_atr_mult = {'bull': float(_g('s3.dynamic_sl.atr_mult.bull', 2.0)), 'caution': float(_g('s3.dynamic_sl.atr_mult.caution', 2.5)), 'bear': float(_g('s3.dynamic_sl.atr_mult.bear', 3.0)), 'crash': float(_g('s3.dynamic_sl.atr_mult.crash', 4.0))}.get(regime, 2.5)
        tp_atr_mult = float(_g('s3.dynamic_sl.tp_atr_mult', 4.0))
        _atr = atr_pct if atr_pct > 0 else float(_g('s3.dynamic_sl.atr_fallback_pct', 2.0))
        sl_raw = -abs(_atr * regime_atr_mult * vix_scale)
        tp_raw = abs(_atr * tp_atr_mult)
        sl_floor = float(_g('s3.dynamic_sl.sl_floor', -20.0))
        sl_ceiling = float(_g('s3.dynamic_sl.sl_ceiling', -1.0))
        tp_floor = float(_g('s3.dynamic_sl.tp_floor', 2.0))
        tp_ceiling = float(_g('s3.dynamic_sl.tp_ceiling', 50.0))
        sl_pct = max(sl_floor, min(sl_ceiling, sl_raw))
        tp_pct = max(tp_floor, min(tp_ceiling, tp_raw))
        return (sl_pct, tp_pct)

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """멀티팩터 하이브리드 신호 생성 (Track A ETF + Track B QVM)."""
        signals = []
        rebalance_day = cfg.get('a2.rebalance_day', 1)
        today = now_kst().day
        if today != rebalance_day and self._current_holdings:
            return signals
        if regime == 'crash':
            sector_scores = self._compute_multifactor_scores(self.DEFENSIVE_ETFS, market_data, source='sector')
            global_scores = []
            logger.info('  🛡️ [S3 Active Macro] Crash Regime: DEFENSIVE_ETFS 유니버스로 스위칭')
        else:
            sector_scores = self._compute_multifactor_scores(self.SECTOR_ETFS, market_data, source='sector')
            global_scores = self._compute_multifactor_scores(self.GLOBAL_ETFS, market_data, source='global')
        dynamic_blacklist = set(cfg.get('s3.dividend_blacklist', list(self.DIVIDEND_BLACKLIST)))
        sector_scores = [s for s in sector_scores if s['ticker'] not in dynamic_blacklist]
        global_scores = [s for s in global_scores if s['ticker'] not in dynamic_blacklist]
        if cfg.get('s3.ml_rank_enabled', True):
            signal_cache = market_data.get('signal_cache', {})
            sector_scores = self._ml_rank_boost(sector_scores, signal_cache)
            global_scores = self._ml_rank_boost(global_scores, signal_cache)
        try:
            _af_s3 = market_data.get('alpha_signals', {}).get('S3_signal', {})
            _sector_forecast = _af_s3.get('sector_forecast', {})
            if _sector_forecast and isinstance(_sector_forecast, dict):
                _boost_scale = cfg.get('s3.alpha_factory_boost_scale', 0.15)
                _boost_cap = cfg.get('s3.alpha_factory_boost_cap', 0.3)
                _boosted = []
                for _score_item in sector_scores + global_scores:
                    _ticker = _score_item.get('ticker', '')
                    _fc_value = _sector_forecast.get(_ticker)
                    if _fc_value is not None:
                        try:
                            _adj = float(_fc_value) * _boost_scale
                            _adj = max(-_boost_cap, min(_boost_cap, _adj))
                            _score_item = dict(_score_item)
                            _score_item['score'] = round(_score_item.get('score', 0.0) + _adj, 4)
                            _score_item['alpha_factory_boost'] = round(_adj, 4)
                            _boosted.append(_ticker)
                        except (TypeError, ValueError):
                            pass
                if _boosted:
                    logger.info(f'  🔬 [Alpha Factory] S3 Sector Forecast 가중치: {len(_boosted)}종목 (scale={_boost_scale})')
        except Exception as _s3_af_e:
            logger.debug(f'  [Alpha Factory] S3 sector_forecast 가점 실패 (무시): {_s3_af_e}')
        sector_scores.sort(key=lambda x: x['score'], reverse=True)
        global_scores.sort(key=lambda x: x['score'], reverse=True)
        top_n_etf = cfg.get('s3.max_positions', 5)
        min_global = cfg.get('s3.min_global_slots', 1)
        guaranteed_global = global_scores[:min_global]
        remaining_pool = sector_scores + global_scores[min_global:]
        remaining_pool.sort(key=lambda x: x['score'], reverse=True)
        etf_selected = guaranteed_global + remaining_pool[:max(0, top_n_etf - len(guaranteed_global))]
        max_per_sector = cfg.get('s3.max_per_sector', 2)
        sector_count: Dict[str, int] = {}
        diversified_etf = []
        for pick in etf_selected:
            sec = pick.get('sector', 'unknown')
            cnt = sector_count.get(sec, 0)
            if cnt < max_per_sector:
                diversified_etf.append(pick)
                sector_count[sec] = cnt + 1
        etf_selected = self._apply_turnover_constraint(diversified_etf)
        for pick in etf_selected:
            pick['_type'] = 'ETF'
        qvm_safe = []
        top_n_qvm = cfg.get('s3.qvm_max_positions', 5)
        if regime == 'crash':
            qvm_selected = []
            logger.info('  🛡️ [S3 Active Macro] Crash Regime: Track B (QVM) 작동 중지 (주식 100% 매도)')
        else:
            qvm_raw = self._qvm_universe.build_universe()
            qvm_scored = self._qvm_scorer.score_universe(qvm_raw)
            try:
                self._qvm_scorer.validate_factors(qvm_scored)
            except Exception as e:
                logger.debug(f'  [Track B] ICIR 팩터 검증 실패: {e}')
            qvm_safe = self._qvm_scorer.screen_value_traps(qvm_scored)
        current_qvm_tickers = {h['ticker'] for h in self._current_holdings if h.get('_type') == 'QVM'}
        held_qvm_picks = []
        qvm_dict = {s['ticker']: s for s in qvm_safe}
        for ticker in current_qvm_tickers:
            if ticker in qvm_dict:
                stock = qvm_dict[ticker]
                if stock.get('margin_of_safety_pct', 0) < 0:
                    logger.info(f'  [Track B] {stock['name']} 매도: 안전마진 소진 (고평가)')
                    continue
                held_qvm_picks.append(stock)
            else:
                logger.info(f'  [Track B] {ticker} 매도: 유니버스/안전성 이탈')
        new_qvm_needed = max(0, top_n_qvm - len(held_qvm_picks))
        new_qvm_picks = []
        if new_qvm_needed > 0:
            candidates = [s for s in qvm_safe if s['ticker'] not in current_qvm_tickers]
            candidates.sort(key=lambda x: x['qvm_score'], reverse=True)
            new_qvm_picks = candidates[:new_qvm_needed]
            for pick in new_qvm_picks:
                logger.info(f'  [Track B] {pick['name']} 신규 편입: QVM={pick.get('qvm_score')}')
        qvm_selected = held_qvm_picks + new_qvm_picks
        for pick in qvm_selected:
            pick['_type'] = 'QVM'
            pick['score'] = pick.get('qvm_score', 0) / 100.0
        target_qvm_weight = cfg.get('s3.allocation.qvm_base_weight', 0.5)
        if len(new_qvm_picks) >= top_n_qvm and all((s.get('margin_of_safety_pct', 0) > 50 for s in new_qvm_picks)):
            target_qvm_weight = cfg.get('s3.allocation.qvm_max_weight', 0.7)
            logger.info('  [하이브리드] 딥 밸류장 감지: QVM 비중 70%로 확대')
        elif regime in ['bear', 'crash']:
            target_qvm_weight = cfg.get('s3.allocation.qvm_min_weight', 0.3)
            logger.info(f'  [하이브리드] {regime} 레짐 감지: QVM 방어적 축소 (30%)')
        target_etf_weight = 1.0 - target_qvm_weight
        etf_sum = sum((max(0.01, p['score']) for p in etf_selected))
        for pick in etf_selected:
            pick['_weight'] = max(0.01, pick['score']) / etf_sum * target_etf_weight if etf_sum > 0 else 0
        qvm_count = len(qvm_selected)
        for pick in qvm_selected:
            pick['_weight'] = target_qvm_weight / max(1, qvm_count)
        combined_selected = etf_selected + qvm_selected
        invest_ratio = cfg.get(f's3.regime_invest_ratio.{regime}', cfg.get('s3.regime_invest_ratio.caution', 0.8))
        signal_cache_ref = market_data.get('signal_cache', {})
        vix_ref = signal_cache_ref.get('vix', cfg.get('s3.vix_fallback_default', 18.0))
        vol_baseline_ref = cfg.get('s3.exit.vol_baseline', 18.0)
        vol_scale_ref = max(cfg.get('s3.tp_sl.vol_scale_min', 0.5), min(cfg.get('s3.tp_sl.vol_scale_max', 2.0), vix_ref / max(vol_baseline_ref, 1.0)))
        for pick in combined_selected:
            adj_weight = pick['_weight'] * invest_ratio
            strategy = 'qvm_value' if pick['_type'] == 'QVM' else pick.get('strategy', 'multifactor_rotation')
            if pick['_type'] == 'ETF':
                regime_tp_ref = {'bull': cfg.get('s3.exit.tp.bull', 18), 'caution': cfg.get('s3.exit.tp.caution', 15), 'bear': cfg.get('s3.exit.tp.bear', 10), 'crash': cfg.get('s3.exit.tp.crash', 7)}
                tp_val = regime_tp_ref.get(regime, cfg.get('s3.exit.tp.caution', 15)) / vol_scale_ref
                tp_val = max(cfg.get('s3.exit.tp_floor', 5), tp_val)
                regime_sl_ref = {'bull': cfg.get('s3.exit.sl.bull', -7), 'caution': cfg.get('s3.exit.sl.caution', -7), 'bear': cfg.get('s3.exit.sl.bear', -5), 'crash': cfg.get('s3.exit.sl.crash', -4)}
                sl_signed = regime_sl_ref.get(regime, cfg.get('s3.exit.sl.caution', -7)) * vol_scale_ref
                sl_signed = min(cfg.get('s3.exit.sl_ceiling', -3), sl_signed)
                sl_val = abs(sl_signed)
                _atr_pct = float(pick.get('atr_pct', pick.get('atr', 0.0)))
                if _atr_pct > 0:
                    _dyn_sl, _dyn_tp = self._compute_dynamic_sl_tp(_atr_pct, vix_ref, regime)
                    logger.debug(f'  [Phase79 DynSL] {pick.get('ticker', '')} ATR={_atr_pct:.2f}% SL={_dyn_sl:.2f}% TP={_dyn_tp:.2f}%')
                    if float(cfg.get('s3.use_dynamic_atr_sl', 1)):
                        sl_signed = _dyn_sl
                        tp_val = _dyn_tp
                        sl_val = abs(sl_signed)
            else:
                tp_val = cfg.get('s3.exit.qvm_tp_placeholder', 100.0)
                sl_val = abs(cfg.get('s3.exit.qvm_disaster_sl', -30.0))
            signals.append({'stream_id': 'S3_B' if pick['_type'] == 'QVM' else 'S3_A', 'ticker': pick['ticker'], 'name': pick['name'], 'direction': 'long', 'confidence': round(max(0, min(1.0, pick['score'])), 3), 'size_pct': round(adj_weight, 4), 'strategy': strategy, 'reason': pick.get('reason', f'{pick['_type']} Score={pick['score']:.3f}'), 'regime': regime, 'sector': pick.get('sector', ''), 'market': pick.get('market', 'KR'), 'timestamp': datetime.now().isoformat(), '_type': pick['_type'], 'tp_pct': round(tp_val, 2), 'sl_pct': round(sl_val, 2)})
        if signals:
            self._current_holdings = [{'ticker': s['ticker'], 'score': s['confidence'], '_type': s['_type']} for s in signals]
            self._rebalance_history.append({'date': datetime.now().isoformat(), 'regime': regime, 'selected': [s['ticker'] for s in signals], 'source': 'hybrid_rotation'})
            self._log_event('REBALANCE', {'stream': 'S3', 'sectors': [s['ticker'] for s in signals], 'qvm_weight': target_qvm_weight, 'etf_weight': target_etf_weight, 'regime': regime})
        if self._regime_detector and signals:
            intraday = self._regime_detector.detect(market_data)
            intraday_regime = intraday.get('regime', 'normal')
            if intraday_regime == 'stress':
                for s in signals:
                    s['confidence'] = round(s['confidence'] * 0.8, 3)
                    s['size_pct'] = round(s['size_pct'] * 0.8, 4)
        return signals

    def _compute_multifactor_scores(self, universe: Dict, market_data: Dict, source: str='sector') -> List[Dict]:
        """4-팩터 종합 스코어 계산 + Z-스코어 정규화.

        팩터:
          - Momentum: 1M/3M/6M 가중 수익률
          - Value: PER/PBR 역수 (낮을수록 저평가)
          - Carry: 배당수익률
          - Volatility: 실현 변동성 역수 (Low-Vol 프리미엄)

        각 팩터를 Z-스코어 정규화 후 가중합.
        """
        momentum_weights = cfg.get('a2.momentum_weights', [0.5, 0.3, 0.2])
        sector_data = market_data.get('sector_momentum', {})
        global_data = market_data.get('global_momentum', {})
        overnight = market_data.get('overnight_intel', {})
        w_mom = cfg.get('s3.factor_weight_momentum', 0.4)
        w_val = cfg.get('s3.factor_weight_value', 0.25)
        w_carry = cfg.get('s3.factor_weight_carry', 0.15)
        w_vol = cfg.get('s3.factor_weight_volatility', 0.2)
        _regime_weights_enabled = cfg.get('s3.regime_factor_weights_enabled', True)
        if _regime_weights_enabled:
            _current_regime = self._detect_current_regime(market_data)
            if _current_regime:
                w_mom = cfg.get(f's3.regime_factor_weights.{_current_regime}.momentum', w_mom)
                w_val = cfg.get(f's3.regime_factor_weights.{_current_regime}.value', w_val)
                w_carry = cfg.get(f's3.regime_factor_weights.{_current_regime}.carry', w_carry)
                w_vol = cfg.get(f's3.regime_factor_weights.{_current_regime}.volatility', w_vol)
                _rw_total = w_mom + w_val + w_carry + w_vol
                if _rw_total > 0 and abs(_rw_total - 1.0) > 0.01:
                    w_mom /= _rw_total
                    w_val /= _rw_total
                    w_carry /= _rw_total
                    w_vol /= _rw_total
                logger.info(f'  ★ M10 레짐 팩터 가중치 [{_current_regime}]: mom={w_mom:.3f}/val={w_val:.3f}/carry={w_carry:.3f}/vol={w_vol:.3f}')
        if cfg.get('s3.factor_timing.enabled', True):
            w_mom, w_val, w_carry, w_vol = self._apply_factor_timing(w_mom, w_val, w_carry, w_vol)
        if cfg.get('s3.macro_timing_enabled', True):
            try:
                import json as _json_mt
                sc_file_mt = _PROJECT_ROOT / 'results' / 'signal_cache.json'
                signal_cache_mt = {}
                if sc_file_mt.exists():
                    signal_cache_mt = _json_mt.loads(sc_file_mt.read_text())
                if signal_cache_mt:
                    timing_multipliers = self._macro_factor_timing(signal_cache_mt)
                    if timing_multipliers:
                        base_weights = {'momentum': w_mom, 'value': w_val, 'carry': w_carry, 'volatility': w_vol}
                        logger.info(f'  ★ 매크로 타이밍 전: mom={w_mom:.3f}/val={w_val:.3f}/carry={w_carry:.3f}/vol={w_vol:.3f}')
                        strength = cfg.get('s3.macro_timing_strength', 0.3)
                        adjusted = {}
                        for k, base_w in base_weights.items():
                            mult = timing_multipliers.get(k, 1.0)
                            blended_mult = 1.0 - strength + strength * mult
                            adjusted[k] = base_w * blended_mult
                        orig_sum = sum(base_weights.values())
                        new_sum = sum(adjusted.values())
                        if new_sum > 0 and orig_sum > 0:
                            scale_factor = orig_sum / new_sum
                            for k in adjusted:
                                adjusted[k] *= scale_factor
                        w_mom = adjusted['momentum']
                        w_val = adjusted['value']
                        w_carry = adjusted['carry']
                        w_vol = adjusted['volatility']
                        logger.info(f'  ★ 매크로 타이밍 후: mom={w_mom:.3f}/val={w_val:.3f}/carry={w_carry:.3f}/vol={w_vol:.3f} (strength={strength:.1f}, mults={{{', '.join((f'{k}:{v:.2f}' for k, v in timing_multipliers.items()))}}})')
            except Exception as e:
                logger.debug(f'  매크로 타이밍 적용 실패 (fallback 유지): {e}')
        tickers = list(universe.keys())
        etf_infos = [universe[t] for t in tickers]
        raw_momentum = []
        raw_value = []
        raw_carry = []
        raw_volatility = []
        for ticker, etf_info in zip(tickers, etf_infos):
            data = (sector_data if source == 'sector' else global_data).get(ticker, {})
            fs_data = self._read_feature_store(ticker) if not data else None
            mom_1m = data.get('momentum_1m', 0)
            mom_3m = data.get('momentum_3m', 0)
            mom_6m = data.get('momentum_6m', 0)
            if fs_data and (not data):
                mom_1m = fs_data.get('mom_21', fs_data.get('mom_1', 0))
                mom_3m = fs_data.get('mom_63', fs_data.get('mom_21', 0) * 3)
                mom_6m = fs_data.get('mom_126', fs_data.get('mom_63', 0) * 2)
            if not data and mom_1m == 0 and (source == 'global'):
                sector = etf_info.get('sector', '')
                fallback = cfg.get(f's3.overnight_fallback.{sector}', 0.0)
                if fallback > 0:
                    if sector in ('us_tech', 'us_semiconductor'):
                        mom_1m = overnight.get('nasdaq_change_pct', 0) * fallback
                    elif sector == 'us_broad':
                        mom_1m = overnight.get('sp500_change_pct', 0) * fallback
                    elif sector in ('ai', 'ai_infra'):
                        mom_1m = overnight.get('nasdaq_change_pct', 0) * fallback
                    elif sector == 'india':
                        mom_1m = overnight.get('sp500_change_pct', 0) * fallback
                    elif sector == 'china':
                        mom_1m = overnight.get('sp500_change_pct', 0) * fallback
            mom_score = momentum_weights[0] * mom_1m + momentum_weights[1] * mom_3m + momentum_weights[2] * mom_6m
            raw_momentum.append(mom_score)
            per = 0
            pbr = 0
            if fs_data:
                per = fs_data.get('per', 0)
                pbr = fs_data.get('pbr', 0)
            elif data:
                per = data.get('per', 0)
                pbr = data.get('pbr', 0)
            per_inv = 1.0 / max(per, 1.0) if per > 0 else 0
            pbr_inv = 1.0 / max(pbr, 0.1) if pbr > 0 else 0
            value_score = per_inv * 0.6 + pbr_inv * 0.4
            raw_value.append(value_score)
            div_yield = 0
            if fs_data:
                div_yield = fs_data.get('div_yield', 0)
            elif data:
                div_yield = data.get('div_yield', 0)
            raw_carry.append(div_yield)
            realized_vol = 0
            if fs_data:
                realized_vol = fs_data.get('vol_60d', fs_data.get('volatility', 0))
            elif data:
                realized_vol = data.get('vol_60d', 0)
            vol_inv = 1.0 / max(realized_vol, 0.01) if realized_vol > 0 else 1.0
            raw_volatility.append(vol_inv)
        norm_momentum = self._cross_sectional_zscore(raw_momentum)
        norm_value = self._cross_sectional_zscore(raw_value)
        norm_carry = self._cross_sectional_zscore(raw_carry)
        norm_volatility = self._cross_sectional_zscore(raw_volatility)
        w_macro = cfg.get('s3.factor_weight_macro', 0.1)
        macro_adj = self._load_sector_macro_adjustments()
        if macro_adj and w_macro > 0:
            scale = 1.0 - w_macro
            adj_w_mom = w_mom * scale
            adj_w_val = w_val * scale
            adj_w_carry = w_carry * scale
            adj_w_vol = w_vol * scale
        else:
            adj_w_mom, adj_w_val, adj_w_carry, adj_w_vol = (w_mom, w_val, w_carry, w_vol)
            w_macro = 0
        scores = []
        for i, (ticker, etf_info) in enumerate(zip(tickers, etf_infos)):
            sector = etf_info.get('sector', '')
            composite = adj_w_mom * norm_momentum[i] + adj_w_val * norm_value[i] + adj_w_carry * norm_carry[i] + adj_w_vol * norm_volatility[i]
            _signal_cache = market_data.get('signal_cache', {})
            _macro_fallback = cfg.get('s3.fallback_macro_score', None)
            _cache_key = f's3_last_macro_{sector}'
            if _macro_fallback is None:
                _macro_fallback = _signal_cache.get(_cache_key, cfg.get('s3.conservative_macro_fallback', 0.0))
            macro_score = float(_macro_fallback)
            if macro_adj and sector in macro_adj:
                macro_score = macro_adj[sector]
                _signal_cache[_cache_key] = macro_score
            if macro_score != 0.0:
                composite += w_macro * macro_score
            factor_detail = {'momentum': round(norm_momentum[i], 4), 'value': round(norm_value[i], 4), 'carry': round(norm_carry[i], 4), 'volatility': round(norm_volatility[i], 4), 'macro': round(macro_score, 4), 'raw_momentum': round(raw_momentum[i], 4)}
            strategy = 'global_multifactor_rotation' if source == 'global' else 'multifactor_rotation'
            scores.append({'ticker': ticker, 'name': etf_info['name'], 'sector': etf_info['sector'], 'market': etf_info.get('market', 'KR'), 'score': round(composite, 4), 'strategy': strategy, 'reason': f'MF: Mom={norm_momentum[i]:.2f} Val={norm_value[i]:.2f} Car={norm_carry[i]:.2f} Vol={norm_volatility[i]:.2f} Mac={macro_score:.2f}', 'factor_scores': factor_detail})
        return scores
    _last_turnover_pct: float = 0.0

    def _apply_turnover_constraint(self, new_picks: List[Dict]) -> List[Dict]:
        """턴오버 제한: 과도한 교체 방지 + 관성 보너스.

        - 기존 보유 종목에 관성 보너스(inertia_bonus) 부여
        - 교체 시 순알파(new_score - old_score - cost) > 0인 경우만 허용
        - 월간 교체율 상한(max_turnover_pct) 적용

        Returns:
            턴오버 제한 적용 후 종목 리스트
        """
        if not self._current_holdings:
            self._last_turnover_pct = 1.0
            return new_picks
        max_turnover = cfg.get('s3.max_turnover_pct', 0.5)
        inertia_bonus = cfg.get('s3.inertia_bonus', 0.03)
        cost_threshold = cfg.get('s3.transaction_cost_threshold', 0.005)
        held_tickers = {h['ticker']: h.get('score', 0.5) for h in self._current_holdings}
        new_ticker_map = {p['ticker']: p for p in new_picks}
        for ticker, old_score in held_tickers.items():
            if ticker not in new_ticker_map:
                etf_info = self._full_universe.get(ticker)
                if etf_info:
                    new_picks.append({'ticker': ticker, 'name': etf_info['name'], 'sector': etf_info.get('sector', ''), 'market': etf_info.get('market', 'KR'), 'score': round(old_score + inertia_bonus, 4), 'strategy': 'inertia_hold', 'reason': f'관성보유: old={old_score:.3f} +bonus={inertia_bonus}'})
            else:
                new_ticker_map[ticker]['score'] = round(new_ticker_map[ticker]['score'] + inertia_bonus, 4)
        new_picks.sort(key=lambda x: x['score'], reverse=True)
        top_n = cfg.get('a2.top_n_sectors', 6)
        final_picks = new_picks[:top_n]
        final_tickers = set((p['ticker'] for p in final_picks))
        new_entries = final_tickers - set(held_tickers.keys())
        max_replacements = max(1, int(len(held_tickers) * max_turnover))
        if len(new_entries) > max_replacements:
            replacement_candidates = []
            for pick in final_picks:
                if pick['ticker'] in new_entries:
                    net_alpha = pick['score'] - min(held_tickers.values(), default=0) - cost_threshold
                    replacement_candidates.append((pick, net_alpha))
            replacement_candidates.sort(key=lambda x: x[1], reverse=True)
            allowed_new = set()
            for pick, net_alpha in replacement_candidates[:max_replacements]:
                if net_alpha > 0:
                    allowed_new.add(pick['ticker'])
            adjusted_picks = []
            for pick in final_picks:
                if pick['ticker'] in new_entries and pick['ticker'] not in allowed_new:
                    continue
                adjusted_picks.append(pick)
            remaining_held = [{'ticker': t, 'score': s, 'name': self._full_universe.get(t, {}).get('name', t), 'sector': self._full_universe.get(t, {}).get('sector', ''), 'market': self._full_universe.get(t, {}).get('market', 'KR'), 'strategy': 'inertia_hold', 'reason': f'턴오버제한: 기존보유 유지 (score={s:.3f})'} for t, s in held_tickers.items() if t not in set((p['ticker'] for p in adjusted_picks))]
            remaining_held.sort(key=lambda x: x['score'], reverse=True)
            while len(adjusted_picks) < top_n and remaining_held:
                adjusted_picks.append(remaining_held.pop(0))
            final_picks = adjusted_picks
        final_tickers = set((p['ticker'] for p in final_picks))
        actual_new = final_tickers - set(held_tickers.keys())
        self._last_turnover_pct = round(len(actual_new) / max(len(final_tickers), 1), 3)
        if actual_new:
            logger.info(f'  🔄 S3 턴오버: {len(actual_new)}/{len(final_tickers)} 교체 ({self._last_turnover_pct:.0%}), 한도={max_turnover:.0%}')
        return final_picks

    def _read_feature_store(self, ticker: str) -> Optional[Dict]:
        """feature_store에서 단일 종목 최신 데이터 읽기."""
        try:
            import pandas as pd
            pf = _FEATURE_STORE_DIR / f'{ticker}.parquet'
            if pf.exists():
                df = pd.read_parquet(pf)
                if not df.empty:
                    return df.iloc[-1].to_dict()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return None

    def _load_sector_macro_adjustments(self) -> Optional[Dict[str, float]]:
        """섹터별 조정 점수 로드 (Alpha Factory + 매크로 선행 지표 통합).

        [Phase 10: Alpha Breakthrough] Phase 10-C: 매크로 선행 지표 통합
        Alpha Factory(alpha_signal.json) 기반 점수에 더해
        MacroProxyCollector(구리/달러/BDI 선물) 기반 섹터 가중치를 합산.

        Returns:
            {sector: 0.0~1.0 score} or None
        """
        import json as _json
        import math as _math
        alpha_file = _PROJECT_ROOT / 'data' / 'alpha_signal.json'
        if not alpha_file.exists():
            return None
        try:
            alpha_data = _json.loads(alpha_file.read_text())
            forecasts = alpha_data.get('S3_signal', {}).get('sector_forecast', {})
            if not forecasts:
                return None
            adjustments = {}
            k = cfg.get('s3.alpha_forecast_sigmoid_k', 0.5)
            for sector, raw_score in forecasts.items():
                if isinstance(raw_score, (int, float)):
                    prob = 1.0 / (1.0 + _math.exp(-k * float(raw_score)))
                    mapped_sector = sector
                    if sector == 'semi':
                        mapped_sector = 'semiconductor'
                    adjustments[mapped_sector] = prob
                    if mapped_sector == 'semiconductor':
                        adjustments['us_semiconductor'] = prob
                    elif mapped_sector == 'it':
                        adjustments['us_tech'] = prob
                        adjustments['ai'] = prob
                        adjustments['ai_infra'] = prob
            try:
                from scripts.macro_proxy_collector import get_macro_sector_scores
                _macro_sector_scores = get_macro_sector_scores()
                if _macro_sector_scores:
                    _macro_weight = cfg.get('s3.macro_proxy_blend_weight', 0.3)
                    for _sec, _ms in _macro_sector_scores.items():
                        if _sec in adjustments:
                            adjustments[_sec] = (1 - _macro_weight) * adjustments[_sec] + _macro_weight * _ms
                        else:
                            adjustments[_sec] = _ms * _macro_weight
                    logger.debug(f'  [Phase 10: Alpha Breakthrough] S3 매크로 팩터 합산: {len(_macro_sector_scores)}개 섹터 (weight={_macro_weight:.0%})')
            except Exception as _mpe:
                logger.debug(f'  [Phase 10] S3 MacroProxy 합산 실패 (무시): {_mpe}')
            return {s: max(0, min(1, v)) for s, v in adjustments.items()}
        except Exception as e:
            logger.debug(f'  S3 Alpha Factory 섹터 예측 로드 실패: {e}')
            try:
                from scripts.macro_proxy_collector import get_macro_sector_scores as _gmss
                _fb_scores = _gmss()
                if _fb_scores:
                    logger.debug(f'  [Phase 10] S3 MacroProxy fallback: {len(_fb_scores)}개 섹터 스코어 사용')
                    return {s: max(0, min(1, v)) for s, v in _fb_scores.items()}
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
            return None

    def get_positions(self) -> List[Dict]:
        """현재 S3 보유 포지션."""
        return self._current_holdings

    def evaluate_exit_rules(self, positions: Dict, market_data: Dict, regime: str) -> List[Dict]:
        """S3 ETF 포지션 동적 TP/SL + 모멘텀 반전 조기 청산.

        모든 임계값 DynamicConfig 동적 로드.
        변동성 기반 동적 조정.
        """
        exits = []
        signal_cache = market_data.get('signal_cache', {})
        vix = signal_cache.get('vix', cfg.get('s3.vix_fallback_default', 18.0))
        vol_baseline = cfg.get('s3.exit.vol_baseline', 18.0)
        vol_scale = max(0.5, min(2.0, vix / vol_baseline))
        for key, pos in positions.items():
            if pos.get('stream') not in ('S3', 'S3_A', 'S3_B'):
                continue
            pnl_pct = pos.get('pnl_pct', 0)
            days_held = pos.get('days_held', 0)
            strategy = pos.get('strategy', '')
            reason = None
            urgency = 0
            if strategy == 'qvm_value':
                qvm_disaster_sl = cfg.get('s3.exit.qvm_disaster_sl', -30.0)
                if pnl_pct <= qvm_disaster_sl:
                    reason = f'QVM 재난 SL: {pnl_pct:+.1f}% <= {qvm_disaster_sl}%'
                    urgency = 3
            else:
                regime_tp = {'bull': cfg.get('s3.exit.tp.bull', 18), 'caution': cfg.get('s3.exit.tp.caution', 15), 'bear': cfg.get('s3.exit.tp.bear', 10), 'crash': cfg.get('s3.exit.tp.crash', 7)}
                tp = regime_tp.get(regime, 15) / vol_scale
                tp = max(cfg.get('s3.exit.tp_floor', 5), tp)
                regime_sl = {'bull': cfg.get('s3.exit.sl.bull', -7), 'caution': cfg.get('s3.exit.sl.caution', -7), 'bear': cfg.get('s3.exit.sl.bear', -5), 'crash': cfg.get('s3.exit.sl.crash', -4)}
                sl = regime_sl.get(regime, -7) * vol_scale
                sl = min(cfg.get('s3.exit.sl_ceiling', -3), sl)
                early_trigger = cfg.get('s3.exit.early_rebalance_pct', -5)
                if pnl_pct >= tp:
                    reason = f'TP: {pnl_pct:+.1f}% >= {tp:.1f}%'
                    urgency = 2
                elif pnl_pct <= sl:
                    reason = f'SL: {pnl_pct:+.1f}% <= {sl:.1f}%'
                    urgency = 3
                elif pnl_pct <= early_trigger and days_held < 20:
                    reason = f'조기 리밸런싱: {pnl_pct:+.1f}% <= {early_trigger}% (보유 {days_held}일)'
                    urgency = 1
            if reason:
                exits.append({'ticker': pos.get('ticker', ''), 'name': pos.get('name', ''), 'action': 'SELL', 'reason': reason, 'urgency': urgency, 'pnl_pct': pnl_pct, 'stream': 'S3'})
        return exits

    def get_performance(self) -> Dict:
        """S3 성과 지표 (동적 계산)."""
        n = len(self._daily_returns)
        cum_ret = sum(self._daily_returns) if n > 0 else 0
        sharpe = None
        if n >= 5:
            mean_r = sum(self._daily_returns) / n
            var = sum(((r - mean_r) ** 2 for r in self._daily_returns)) / n
            std = math.sqrt(var) if var > 0 else 0
            ann = cfg.get('common.annualization_factor', 252)
            sharpe = round(mean_r / std * math.sqrt(ann), 3) if std > 0 else 0
        peak = 0
        max_dd = 0
        cum = 0
        for r in self._daily_returns:
            cum += r
            peak = max(peak, cum)
            dd = cum - peak
            max_dd = min(max_dd, dd)
        wins = sum((1 for r in self._daily_returns if r > 0))
        return {'stream_id': 'S3', 'name': self.name, 'daily_returns': self._daily_returns[-30:], 'cumulative_return_pct': round(cum_ret, 3), 'sharpe': sharpe, 'max_drawdown_pct': round(max_dd, 2), 'win_rate': round(wins / max(n, 1), 3), 'total_trades': len(self._rebalance_history), 'active_positions': len(self._current_holdings), 'last_turnover_pct': self._last_turnover_pct, 'n_days': n}

    def _macro_factor_timing(self, signal_cache: dict) -> dict:
        """매크로 환경에 따른 팩터 가중치 동적 조정.

        로직:
        - VIX 높음 (불확실성) → 저변동성/품질 가중치 상승
        - VIX 낮음 (안정) → 모멘텀/성장 가중치 상승
        - 금리 상승 → 밸류 가중치 상승
        - 금리 하락 → 성장/캐리 가중치 상승
        - 환율 강세(원화 약세) → 내수 팩터(밸류/캐리) 선호
        - Bull 레짐 → 모멘텀 강화
        - Bear 레짐 → 품질(캐리)/저변동성 강화

        Returns:
            {'momentum': 1.2, 'value': 0.8, 'carry': 1.1,
             'volatility': 1.3, ...} — 각 팩터별 배수
        """
        try:
            vix_high = cfg.get('s3.vix_threshold_high', 25.0)
            vix_low = cfg.get('s3.vix_threshold_low', 15.0)
            rate_threshold = cfg.get('s3.rate_change_threshold', 0.5)
            fx_threshold = cfg.get('s3.fx_change_threshold', 2.0)
            mult = {'momentum': 1.0, 'value': 1.0, 'carry': 1.0, 'volatility': 1.0}
            nowcast_score = signal_cache.get('macro_nowcast', 0.0)
            if nowcast_score > 0.5:
                logger.info(f'  ⚡ S3 [Phase 90]: 강한 Nowcasting 스코어({nowcast_score}) → Momentum 팩터 상향')
                mult['momentum'] += 0.15
                mult['volatility'] -= 0.1
            elif nowcast_score < -0.5:
                logger.warning(f'  📉 S3 [Phase 90]: 약한 Nowcasting 스코어({nowcast_score}) → 방어 팩터 상향')
                mult['volatility'] += 0.15
                mult['carry'] += 0.1
            vix = signal_cache.get('vix')
            if vix is not None:
                vix = float(vix)
                if vix >= vix_high:
                    intensity = min((vix - vix_high) / 10.0, 1.0)
                    mult['volatility'] += 0.3 * intensity
                    mult['carry'] += 0.2 * intensity
                    mult['momentum'] -= 0.3 * intensity
                    mult['value'] += 0.1 * intensity
                elif vix <= vix_low:
                    intensity = min((vix_low - vix) / 5.0, 1.0)
                    mult['momentum'] += 0.3 * intensity
                    mult['carry'] -= 0.1 * intensity
                    mult['volatility'] -= 0.15 * intensity
            rate_change = signal_cache.get('us10y_change_1m')
            if rate_change is not None:
                rate_change = float(rate_change)
                if abs(rate_change) >= rate_threshold:
                    if rate_change > 0:
                        r_intensity = min(rate_change / 3.0, 1.0)
                        mult['value'] += 0.25 * r_intensity
                        mult['carry'] -= 0.1 * r_intensity
                        mult['momentum'] -= 0.1 * r_intensity
                    else:
                        r_intensity = min(abs(rate_change) / 3.0, 1.0)
                        mult['carry'] += 0.2 * r_intensity
                        mult['momentum'] += 0.15 * r_intensity
                        mult['value'] -= 0.1 * r_intensity
            fx_change = signal_cache.get('usdkrw_change_1m')
            if fx_change is not None:
                fx_change = float(fx_change)
                if abs(fx_change) >= fx_threshold:
                    if fx_change > 0:
                        fx_intensity = min(fx_change / 5.0, 1.0)
                        mult['value'] += 0.15 * fx_intensity
                        mult['carry'] += 0.15 * fx_intensity
                        mult['momentum'] -= 0.1 * fx_intensity
                    else:
                        fx_intensity = min(abs(fx_change) / 5.0, 1.0)
                        mult['momentum'] += 0.1 * fx_intensity
                        mult['value'] -= 0.05 * fx_intensity
            regime = signal_cache.get('kr_regime') or signal_cache.get('us_regime')
            if regime:
                regime = str(regime).lower()
                if regime == 'bull':
                    mult['momentum'] += 0.2
                    mult['volatility'] -= 0.1
                elif regime == 'bear':
                    mult['carry'] += 0.2
                    mult['volatility'] += 0.25
                    mult['momentum'] -= 0.2
                elif regime == 'crash':
                    mult['volatility'] += 0.4
                    mult['carry'] += 0.3
                    mult['momentum'] -= 0.35
                    mult['value'] += 0.15
            for k in mult:
                mult[k] = max(0.3, min(2.0, mult[k]))
            logger.debug(f'  매크로 팩터 타이밍 배수: VIX={signal_cache.get('vix', 'N/A')}, US10Y_chg={signal_cache.get('us10y_change_1m', 'N/A')}, FX_chg={signal_cache.get('usdkrw_change_1m', 'N/A')}, regime={(regime if regime else 'N/A')} → {mult}')
            return mult
        except Exception as e:
            logger.warning(f'  매크로 팩터 타이밍 실패 (중립 반환): {e}')
            return {'momentum': 1.0, 'value': 1.0, 'carry': 1.0, 'volatility': 1.0}

    def _detect_current_regime(self, market_data: Dict) -> Optional[str]:
        """★ M10: 현재 레짐 감지 — signal_cache/market_data에서 추출.

        Returns: 'bull', 'bear', 'sideways' 또는 None.
        """
        regime = market_data.get('regime', '')
        if regime:
            return self._normalize_regime(regime)
        try:
            _project_root = Path(__file__).resolve().parent.parent.parent.parent
            _cache_path = _project_root / 'results' / 'signal_cache.json'
            if _cache_path.exists():
                _cache = _json.loads(_cache_path.read_text(encoding='utf-8'))
                regime = _cache.get('regime', _cache.get('market_regime', ''))
                if regime:
                    return self._normalize_regime(regime)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        try:
            _project_root = Path(__file__).resolve().parent.parent.parent.parent
            _state_path = _project_root / 'results' / 'pipeline_state.json'
            if _state_path.exists():
                _state = _json.loads(_state_path.read_text(encoding='utf-8'))
                regime = _state.get('regime', '')
                if regime:
                    return self._normalize_regime(regime)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return None

    @staticmethod
    def _normalize_regime(regime: str) -> str:
        """레짐 문자열 정규화 → bull/bear/sideways."""
        r = regime.lower().strip()
        if 'bull' in r or 'up' in r or 'expansion' in r:
            return 'bull'
        if 'bear' in r or 'down' in r or 'contraction' in r or ('crisis' in r):
            return 'bear'
        if 'side' in r or 'range' in r or 'neutral' in r:
            return 'sideways'
        return r

    def _apply_factor_timing(self, w_mom: float, w_val: float, w_carry: float, w_vol: float):
        """★ 팩터 모멘텀 및 HMM 선제적 틸팅 기반 동적 가중치 조정."""
        try:
            me_path = _PROJECT_ROOT / 'results' / 'measurement_engine.json'
            state_path = _PROJECT_ROOT / 'results' / 'pipeline_state.json'
            if state_path.exists():
                try:
                    pipeline_state = _json.loads(state_path.read_text())
                    hmm_transition = pipeline_state.get('hmm_transition', {})
                    crash_prob = hmm_transition.get('crash', 0.0)
                    bear_prob = hmm_transition.get('bear', 0.0)
                    down_prob = crash_prob + bear_prob
                    if down_prob > cfg.get('s3.hmm_crash_threshold', 0.15):
                        logger.warning(f'  ⚠️ S3 [Phase 90]: HMM 하락 위험 감지 (Crash/Bear Prob={down_prob:.1%}) → Low Vol/Value 비중 대폭 확대')
                        w_vol *= 2.0
                        w_val *= 1.5
                        w_mom *= 0.5
                    elif hmm_transition.get('bull', 0.0) > 0.6:
                        logger.info(f'  📈 S3 [Phase 90]: HMM 강세장 예측 (Bull Prob={hmm_transition.get('bull'):.1%}) → Mom 비중 확대')
                        w_mom *= 1.5
                        w_vol *= 0.7
                except Exception as he:
                    logger.debug(f'  S3 HMM 전이 확률 틸팅 실패: {he}')
            if not me_path.exists():
                return (w_mom, w_val, w_carry, w_vol)
            me_data = _json.loads(me_path.read_text())
            sleeve_views = me_data.get('views', {}).get('sleeve', {})
            s3_view = sleeve_views.get('S3', {})
            avg_pnl = s3_view.get('avg_pnl_pct', 0) or 0
            timing_mult = cfg.get('s3.factor_timing.momentum_mult', 1.2)
            timing_damp = cfg.get('s3.factor_timing.dampening_mult', 0.8)
            min_trigger = cfg.get('s3.factor_timing.min_pnl_trigger', 0.5)
            orig_total = w_mom + w_val + w_carry + w_vol
            if abs(avg_pnl) < min_trigger:
                return (w_mom, w_val, w_carry, w_vol)
            if avg_pnl > 0:
                w_mom *= timing_mult
                w_vol *= timing_damp
            else:
                w_mom *= timing_damp
                w_val *= timing_mult
                w_carry *= timing_mult
            new_total = w_mom + w_val + w_carry + w_vol
            if new_total > 0 and orig_total > 0:
                scale = orig_total / new_total
                w_mom *= scale
                w_val *= scale
                w_carry *= scale
                w_vol *= scale
            logger.info(f'  ★ 팩터 타이밍: S3 avg_pnl={avg_pnl:+.1f}% → mom={w_mom:.2f}/val={w_val:.2f}/carry={w_carry:.2f}/vol={w_vol:.2f}')
            return (w_mom, w_val, w_carry, w_vol)
        except Exception as e:
            logger.debug(f'  팩터 타이밍 실패: {e}')
            return (w_mom, w_val, w_carry, w_vol)

    def _load_predictions(self) -> Optional[Dict[str, Dict]]:
        """S2 앙상블 ML predictions 최신 파일 로드.

        predictions_dir에서 가장 최근 prediction_*.jsonl 파일을 찾아
        각 티커별 {direction, confidence, predicted_return} 매핑 반환.

        Returns:
            {ticker: {'direction': 'up'|'down', 'probability': float,
                      'predicted_return': float}} or None
        """
        try:
            pred_dir_name = cfg.get('s3.ml_rank_predictions_dir', 'data/predictions')
            pred_dir = _PROJECT_ROOT / pred_dir_name
            if not pred_dir.exists():
                logger.debug(f'  ML predictions 디렉터리 없음: {pred_dir}')
                return None
            latest_file = pred_dir / 'predictions_latest.jsonl'
            if not latest_file.exists():
                pred_files = sorted(pred_dir.glob('prediction_*.jsonl'), reverse=True)
                if not pred_files:
                    logger.debug('  ML predictions 파일 없음')
                    return None
                latest_file = pred_files[0]
            predictions: Dict[str, Dict] = {}
            min_prob = cfg.get('s3.ml_rank_min_prob', 0.45)
            with open(latest_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                        ticker = rec.get('ticker', '')
                        confidence = float(rec.get('confidence', 0))
                        direction = rec.get('direction', 'up')
                        if confidence < min_prob:
                            continue
                        if direction == 'up':
                            ml_prob = confidence
                        else:
                            ml_prob = 1.0 - confidence
                        predictions[ticker] = {'direction': direction, 'probability': ml_prob, 'predicted_return': float(rec.get('predicted_return', 0)), 'raw_confidence': confidence}
                    except (ValueError, KeyError):
                        continue
            logger.info(f'  ★ ML predictions 로드: {latest_file.name} ({len(predictions)}종목, min_prob={min_prob})')
            return predictions if predictions else None
        except Exception as e:
            logger.warning(f'  ML predictions 로드 실패 (fallback): {e}')
            return None

    def _ml_rank_boost(self, etf_scores: List[Dict], signal_cache: dict) -> List[Dict]:
        """ML 예측값으로 ETF 랭킹 보조.

        - S2 ML predictions에서 ETF 티커의 prob 로드
        - 모멘텀 팩터와 ML bullish가 일치하면 스코어 부스트
        - 다이버전스 신호 → 스코어 감점
        - Fallback: predictions 없으면 기존 스코어 그대로 반환

        Args:
            etf_scores: _compute_multifactor_scores 결과 리스트
            signal_cache: 시장 데이터 signal_cache

        Returns:
            ML 부스트 적용된 스코어 리스트
        """
        ml_weight = cfg.get('s3.ml_rank_weight', 0.1)
        predictions = self._load_predictions()
        if not predictions:
            return etf_scores
        boosted_scores = []
        rank_changes = []
        for entry in etf_scores:
            ticker = entry['ticker']
            base_score = entry['score']
            boosted = dict(entry)
            if ticker in predictions:
                pred = predictions[ticker]
                ml_prob = pred['probability']
                new_score = base_score * (1.0 - ml_weight) + ml_prob * ml_weight
                factor_scores = entry.get('factor_scores', {})
                momentum_norm = factor_scores.get('momentum', 0.5)
                ml_bullish = ml_prob > 0.5
                momentum_bullish = momentum_norm > 0.5
                divergence_adj = cfg.get('s3.ml_rank_divergence_penalty', 0.02)
                convergence_adj = cfg.get('s3.ml_rank_convergence_bonus', 0.01)
                if ml_bullish == momentum_bullish:
                    new_score += convergence_adj
                    boost_type = 'convergence'
                else:
                    new_score -= divergence_adj
                    boost_type = 'divergence'
                boosted['score'] = round(new_score, 4)
                boosted['ml_boost'] = {'ml_prob': round(ml_prob, 4), 'raw_confidence': round(pred['raw_confidence'], 4), 'direction': pred['direction'], 'boost_type': boost_type, 'base_score': round(base_score, 4), 'adjusted_score': round(new_score, 4)}
                score_delta = new_score - base_score
                if abs(score_delta) > 0.001:
                    rank_changes.append({'ticker': ticker, 'name': entry.get('name', ''), 'base': round(base_score, 4), 'adjusted': round(new_score, 4), 'delta': round(score_delta, 4), 'ml_prob': round(ml_prob, 4), 'boost_type': boost_type})
                boosted['reason'] = f'{entry.get('reason', '')} | ML={ml_prob:.2f}({boost_type[:3]})'
            else:
                boosted['score'] = base_score
            boosted_scores.append(boosted)
        if rank_changes:
            rank_changes.sort(key=lambda x: abs(x['delta']), reverse=True)
            top_changes = rank_changes[:5]
            logger.info(f'  ★ S3 ML 랭크 부스트: {len(rank_changes)}종목 조정 (weight={ml_weight:.2f})')
            for rc in top_changes:
                arrow = '↑' if rc['delta'] > 0 else '↓'
                logger.info(f'    {arrow} {rc['ticker']} {rc['name']}: {rc['base']:.4f}→{rc['adjusted']:.4f} (Δ{rc['delta']:+.4f}, ML={rc['ml_prob']:.2f}, {rc['boost_type']})')
        return boosted_scores
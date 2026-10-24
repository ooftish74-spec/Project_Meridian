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
try:
    from src.params.tables import load_table  # Wave2 TrackF: ticker tables live in config/params/tables
except Exception:  # pragma: no cover
    import json as _tj, pathlib as _tp
    def load_table(name, default=None):
        _r = next((d for d in _tp.Path(__file__).resolve().parents if (d / 'config' / 'params').is_dir()), None)
        try:
            return _tj.loads((_r / 'config' / 'params' / 'tables' / f'{name}.json').read_text(encoding='utf-8'))
        except Exception:
            return {} if default is None else default
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
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
from src.streams.s3_active_macro.qvm_strategic_sleeve import StrategicQVMCompoundingSleeve
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
    # 🎯 [S3 Stream Boundary Isolation] 100% 미국 직투 (USD) 자산 전담으로 재정립
    SECTOR_ETFS = load_table('src.streams.s3_active_macro.active_macro_stream.S3FactorStream.SECTOR_ETFS', {})
    GLOBAL_ETFS = load_table('src.streams.s3_active_macro.active_macro_stream.S3FactorStream.GLOBAL_ETFS', {})
    DEFENSIVE_ETFS = load_table('src.streams.s3_active_macro.active_macro_stream.S3FactorStream.DEFENSIVE_ETFS', {})
    DIVIDEND_BLACKLIST = {'279530', '211560', '289480', '458730', '441640', '458760', '211900', '494330'}

    def __init__(self):
        super().__init__('S3', 'Hybrid Multifactor & QVM Engine')
        self._current_holdings: List[Dict] = []
        self._rebalance_history: List[Dict] = []
        self._daily_returns: List[float] = []
        sector_overrides = cfg.get('s3.sector_etfs', self.SECTOR_ETFS)
        global_overrides = cfg.get('s3.global_etfs', self.GLOBAL_ETFS)
        self._full_universe = {**sector_overrides, **global_overrides}
        self._qvm_scorer = QVMScorer()
        self._qvm_universe = QVMUniverse()
        self._strategic_qvm_sleeve = StrategicQVMCompoundingSleeve()
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
        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe_vix_base = get_quant_engine().get_vix_regime_quantiles().get('baseline', 18.0)
        vix_base = float(_g('s3.dynamic_sl.vix_baseline', _dqe_vix_base))
        vix_scale = max(0.5, min(3.0, vix / max(vix_base, 1e-06)))
        regime_atr_mult = {'bull': float(_g('s3.dynamic_sl.atr_mult.bull', 2.0)), 'caution': float(pget('s3.dynamic_sl.atr_mult.caution', 2.5)), 'bear': float(pget('s3.dynamic_sl.atr_mult.bear', 3.0)), 'crash': float(pget('s3.dynamic_sl.atr_mult.crash', 4.0))}.get(regime, 2.5)
        tp_atr_mult = float(pget('s3.dynamic_sl.tp_atr_mult', 4.0))
        _atr = atr_pct if atr_pct > 0 else float(_g('s3.dynamic_sl.atr_fallback_pct', 2.0))
        sl_raw = -abs(_atr * regime_atr_mult * vix_scale)
        tp_raw = abs(_atr * tp_atr_mult)
        sl_floor = float(pget('s3.dynamic_sl.sl_floor', -20.0))
        sl_ceiling = float(_g('s3.dynamic_sl.sl_ceiling', -1.0))
        tp_floor = float(_g('s3.dynamic_sl.tp_floor', 2.0))
        tp_ceiling = float(pget('s3.dynamic_sl.tp_ceiling', 50.0))
        sl_pct = max(sl_floor, min(sl_ceiling, sl_raw))
        tp_pct = max(tp_floor, min(tp_ceiling, tp_raw))
        return (sl_pct, tp_pct)

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """멀티팩터 하이브리드 신호 생성 (Track A ETF + Track B QVM)."""
        signals = []
        rebalance_day = cfg.get('a2.rebalance_day', 1)
        today = now_kst().day
        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe = get_quant_engine()
        us_regime = _dqe.resolve_effective_us_regime(regime, market_data)
        if today != rebalance_day and self._current_holdings:
            try:
                from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve
                _tc_eng = S3CrossAssetSleeve()
                for _cs in _tc_eng.generate_track_c_signals(us_regime, market_data):
                    _cs['_type'] = 'TRACK_C'
                    _cs.setdefault('tp_pct', float(pget('s3.track_c.tp_pct', 5.0)))
                    _cs.setdefault('sl_pct', float(pget('s3.track_c.sl_pct', 2.5)))
                    signals.append(_cs)
            except Exception as _tc_early_e:
                logger.debug(f"  [Alpha 4 Track C] 비리밸런싱일 슬리브 스킵: {_tc_early_e}")
            return signals
        _top_def_k = 6
        _dyn_def_etfs = dict(self.DEFENSIVE_ETFS)
        for _item in _dqe.get_us_defensive_basket(top_k=_top_def_k, include_inverse=True):
            _dtk = _item['ticker']
            _dyn_def_etfs.setdefault(_dtk, {'name': _dtk, 'sector': 'us_dynamic_defensive', 'market': 'US'})
        _excl_for_global = set(self.SECTOR_ETFS.keys()) | set(_dyn_def_etfs.keys())
        _dyn_global_etfs = _dqe.get_dynamic_us_growth_and_global_universe(
            seed_universe=self.GLOBAL_ETFS,
            exclude_tickers=_excl_for_global,
        )
        self._full_universe.update(_dyn_global_etfs)
        self._full_universe.update(_dyn_def_etfs)
        us_regime = _dqe.resolve_effective_us_regime(regime, market_data)
        if isinstance(market_data, dict):
            market_data.setdefault('regime', regime)
            market_data.setdefault('us_regime', us_regime)
        if us_regime != str(regime or '').lower():
            logger.info(
                f'  🌎 [S3 US Regime Decoupling] KRX 레짐({regime}) ≠ US 실효 레짐({us_regime}) '
                f'→ Track A 미국 자산은 US 실효 레짐({us_regime}) 적용'
            )
        if us_regime == 'crash':
            sector_scores = self._compute_multifactor_scores(_dyn_def_etfs, market_data, source='sector')
            global_scores = []
            logger.info('  🛡️ [S3 Active Macro] US Crash Regime: 동적 수리 방어 유니버스(DEFENSIVE_ETFS)로 스위칭')
        elif us_regime == 'bear':
            sector_scores = self._compute_multifactor_scores({**self.SECTOR_ETFS, **_dyn_def_etfs}, market_data, source='sector')
            global_scores = self._compute_multifactor_scores({**_dyn_global_etfs, **_dyn_def_etfs}, market_data, source='global')
            logger.info('  🛡️ [S3 Active Macro] US Bear Regime: 동적 수리 방어 유니버스 병합 스코어링')
        else:
            sector_scores = self._compute_multifactor_scores(self.SECTOR_ETFS, market_data, source='sector')
            global_scores = self._compute_multifactor_scores(_dyn_global_etfs, market_data, source='global')
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
                _boost_scale = pget('s3.alpha_factory_boost_scale', 0.15)
                _boost_cap = pget('s3.alpha_factory_boost_cap', 0.3)
                _boosted = []
                for _score_item in sector_scores + global_scores:
                    _ticker = _score_item.get('ticker', '')
                    _fc_value = _sector_forecast.get(_ticker)
                    if _fc_value is not None:
                        try:
                            _adj = float(_fc_value) * _boost_scale
                            _adj = max(-_boost_cap, min(_boost_cap, _adj))
                            _score_item['score'] = round(_score_item.get('score', 0.0) + _adj, 4)
                            _score_item['alpha_factory_boost'] = round(_adj, 4)
                            _boosted.append(_ticker)
                        except (TypeError, ValueError):
                            from src.utils.error_logger import log_error_rate_limited
                            logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
                if _boosted:
                    logger.info(f'  🔬 [Alpha Factory] S3 Sector Forecast 가중치: {len(_boosted)}종목 (scale={_boost_scale})')
        except Exception as _s3_af_e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_s3_af_e}", exc_info=True)
            logger.debug(f'  [Alpha Factory] S3 sector_forecast 가점 실패 (무시): {_s3_af_e}')
        sector_scores.sort(key=lambda x: x['score'], reverse=True)
        global_scores.sort(key=lambda x: x['score'], reverse=True)
        top_n_etf = int(pget('s3.max_positions', 5))
        min_global_base = int(cfg.get('s3.min_global_slots', 1))
        if global_scores and sector_scores:
            _top_k_cmp = 1 + 2
            _g_top = global_scores[:_top_k_cmp]
            _s_top = sector_scores[:_top_k_cmp]
            _g_mean = sum(float(x.get('score', 0.0)) for x in _g_top) / max(len(_g_top), 1)
            _s_mean = sum(float(x.get('score', 0.0)) for x in _s_top) / max(len(_s_top), 1)
            _temp_inv = 2.0 + 2.0
            _p_glob = 1.0 / (1.0 + math.exp(-_temp_inv * (_g_mean - _s_mean)))
            _dyn_slots = int(round(top_n_etf * _p_glob))
            _max_g_slots = max(1, top_n_etf - 1)
            min_global = max(min_global_base, min(_dyn_slots, _max_g_slots))
        else:
            min_global = min_global_base
        guaranteed_global = global_scores[:min_global]
        remaining_pool = sector_scores + global_scores[min_global:]
        remaining_pool.sort(key=lambda x: x['score'], reverse=True)
        candidate_stream = guaranteed_global + remaining_pool
        max_per_sector = int(cfg.get('s3.max_per_sector', 2))
        sector_count: Dict[str, int] = {}
        seen_etf_tickers: set = set()
        diversified_etf = []
        for pick in candidate_stream:
            if len(diversified_etf) >= top_n_etf:
                break
            _tk = pick.get('ticker', '')
            if _tk in seen_etf_tickers:
                continue
            sec = pick.get('sector', 'unknown')
            cnt = sector_count.get(sec, 0)
            if cnt < max_per_sector:
                diversified_etf.append(pick)
                seen_etf_tickers.add(_tk)
                sector_count[sec] = cnt + 1
        etf_selected = self._apply_turnover_constraint(diversified_etf)
        for pick in etf_selected:
            pick['_type'] = 'ETF'
        qvm_safe = []
        qvm_exit_signals = []
        top_n_qvm = int(pget('s3.buffett.max_positions', 5) or pget('s3.qvm_max_positions', 5))
        allow_kr_qvm = bool(cfg.get('s3.qvm.allow_kr_stocks', False))
        if not allow_kr_qvm:
            qvm_selected = []
            new_qvm_picks = []
            logger.info('  🛡️ [S3 Active Macro] Track B (KR QVM) 비활성화 (s3.qvm.allow_kr_stocks=False) — Track A US ETF 100% 집중')
        else:
            from src.streams.s3_active_macro.qvm_strategic_sleeve import StrategicQVMCompoundingSleeve
            _buffett_sleeve = StrategicQVMCompoundingSleeve()
            qvm_raw = self._qvm_universe.build_universe()
            qvm_scored = self._qvm_scorer.score_universe(qvm_raw)
            try:
                self._qvm_scorer.validate_factors(qvm_scored)
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  [Track B] ICIR 팩터 검증 실패: {e}')
            qvm_safe = self._qvm_scorer.screen_value_traps(qvm_scored)
            qvm_scored_dict = {s['ticker']: s for s in qvm_scored}

            # 1) 현재 보유 중인 Track 2 (S3_B / TRACK_2_BUFFETT_QVM) 포지션 식별 (메모리 + 원장 + 포트폴리오 SSOT)
            current_qvm_meta: Dict[str, Dict] = {}
            for h in self._current_holdings:
                if h.get('_type') == 'QVM' or h.get('stream_id') == 'S3_B' or h.get('track') == 'TRACK_2_BUFFETT_QVM':
                    _tk_h = str(h.get('ticker', '')).strip()
                    if _tk_h:
                        current_qvm_meta[_tk_h] = dict(h)
            try:
                from src.execution.position_ledger import PositionLedgerManager
                _plm_qvm = PositionLedgerManager()
                _led_all = _plm_qvm.load_ledger()
                _kp_file = _PROJECT_ROOT / 'results' / 'kis_portfolio.json'
                _kp_holdings = {}
                if _kp_file.exists():
                    _kp_holdings = (_json.loads(_kp_file.read_text(encoding='utf-8')).get('holdings') or {})
                for _tk_k, _h_k in _kp_holdings.items():
                    if not isinstance(_h_k, dict):
                        continue
                    _l_meta = _led_all.get(_tk_k, {}) if isinstance(_led_all.get(_tk_k), dict) else {}
                    _sid_k = _h_k.get('stream_id') or _l_meta.get('stream_id', '')
                    _trk_k = _h_k.get('track') or _l_meta.get('track', '')
                    _str_k = _h_k.get('strategy') or _l_meta.get('strategy', '')
                    if _sid_k == 'S3_B' or _trk_k == 'TRACK_2_BUFFETT_QVM' or _str_k in ('buffett_qvm', 'qvm_value'):
                        current_qvm_meta[str(_tk_k)] = {
                            'ticker': str(_tk_k),
                            'entry_price': float(_h_k.get('avg_price', _l_meta.get('entry_price', 0.0)) or 0.0),
                            'current_price': float(_h_k.get('current_price', 0.0) or 0.0),
                            'highest_price': float(_l_meta.get('highest_price', _h_k.get('current_price', 0.0)) or 0.0),
                            'pnl_pct': float(_h_k.get('pnl_pct', 0.0) or 0.0),
                        }
            except Exception as _qvm_pos_e:
                logger.debug(f'  [Track B] 보유 포지션 메타 로드 스킵: {_qvm_pos_e}')

            current_qvm_tickers = set(current_qvm_meta.keys())

            # 2) Buffett 4-Pillar Moat Gate 통과 후보군 선별
            moat_candidates = []
            for s in qvm_safe:
                gate_res = _buffett_sleeve.evaluate_buffett_moat_gate(s, regime=regime)
                if gate_res.get('passed', False):
                    s_copy = dict(s)
                    s_copy['_buffett_gate'] = gate_res
                    moat_candidates.append(s_copy)
            moat_candidates.sort(key=lambda x: float(x.get('qvm_score', 0.0) or 0.0), reverse=True)
            best_new_candidate = next((c for c in moat_candidates if c['ticker'] not in current_qvm_tickers), None)

            # 3) 기존 보유 종목에 대해 Buffett 4대 매도 헌법(Exit Laws 1~4) 평가
            held_qvm_picks = []
            _zero_slots = 0
            for ticker in sorted(current_qvm_tickers):
                pos_info = current_qvm_meta.get(ticker, {'ticker': ticker})
                stock_m = qvm_scored_dict.get(ticker)
                if stock_m is not None:
                    # Punch-Card 교체(Exit Law 4)는 포트폴리오 슬롯이 가득 찬 경우에만 평가
                    _is_full = len(current_qvm_tickers) >= top_n_qvm
                    _cand_for_swap = best_new_candidate if _is_full else None
                    exit_eval = _buffett_sleeve.evaluate_buffett_exit_laws(
                        stock_data=stock_m,
                        holding_info=pos_info,
                        top_unheld_candidate=_cand_for_swap,
                        is_sleeve_full=_is_full,
                    )
                    if exit_eval and (exit_eval.get('exit_law') or exit_eval.get('should_exit', False)):
                        _nm = stock_m.get('name', ticker)
                        _ex_reason = exit_eval.get('reason', 'Buffett Exit Law Triggered')
                        _ex_type = exit_eval.get('exit_law') or exit_eval.get('exit_type', 'BUFFETT_EXIT')
                        logger.info(f"  🔔 [Track 2 Buffett Exit] {_nm}({ticker}) 매도 결정 [{_ex_type}]: {_ex_reason}")
                        qvm_exit_signals.append({
                            'stream_id': 'S3_B',
                            'track': 'TRACK_2_BUFFETT_QVM',
                            'ticker': ticker,
                            'name': _nm,
                            'direction': 'exit',
                            'action': 'sell',
                            'confidence': 1.0,
                            'size_pct': 0.0,
                            'strategy': 'buffett_qvm',
                            'exit_type': _ex_type,
                            'reason': _ex_reason,
                            'regime': regime,
                            'sector': stock_m.get('sector', ''),
                            'market': 'KR',
                            'timestamp': datetime.now().isoformat(),
                            '_type': 'QVM',
                        })
                        continue
                    held_qvm_picks.append(stock_m)
                else:
                    logger.info(f'  [Track 2 Buffett QVM] {ticker} 유니버스 재무데이터 미확인 — 4대 매도법칙 외 강제매도 금지(보유 유지)')

            # 4) 신규 편입 (섹터당 최대 2종목 분산 + Buffett 4-Pillar 통과 종목만)
            new_qvm_needed = max(_zero_slots, top_n_qvm - len(held_qvm_picks))
            new_qvm_picks = []
            if new_qvm_needed > _zero_slots:
                _sec_counts: Dict[str, int] = {}
                _max_per_sec = 2
                for _hp in held_qvm_picks:
                    _hsec = str(_hp.get('sector') or '기타')
                    _sec_counts[_hsec] = _sec_counts.get(_hsec, 0) + 1
                for cand in moat_candidates:
                    if len(new_qvm_picks) >= new_qvm_needed:
                        break
                    if cand['ticker'] in current_qvm_tickers:
                        continue
                    _csec = str(cand.get('sector') or '기타')
                    if _sec_counts.get(_csec, 0) >= _max_per_sec:
                        continue
                    new_qvm_picks.append(cand)
                    _sec_counts[_csec] = _sec_counts.get(_csec, 0) + 1
                    _nm = cand.get('name', '')
                    _qvm_sc = cand.get('qvm_score', 0)
                    _mos_sc = cand.get('margin_of_safety_pct', cand.get('_buffett_gate', {}).get('mos_pct', 0))
                    cand['margin_of_safety_pct'] = float(_mos_sc)
                    _roic_sp = cand.get('roic_wacc_spread_pct', 0)
                    logger.info(
                        f"  🏰 [Track 2 Buffett QVM] {_nm}({cand['ticker']}) 신규 편입 승인: "
                        f"QVM={_qvm_sc:.1f}, MoS={_mos_sc:+.1f}%, ROIC-WACC={_roic_sp:+.1f}%p"
                    )
            qvm_selected = held_qvm_picks + new_qvm_picks
        for pick in qvm_selected:
            pick['_type'] = 'QVM'
            pick['score'] = pick.get('qvm_score', 0) / 100.0
        if not qvm_selected:
            target_qvm_weight = 0.0
        else:
            target_qvm_weight = pget('s3.allocation.qvm_base_weight', 0.5)
            _deep_mos_thr = 50.0
            if len(new_qvm_picks) >= top_n_qvm and all((s.get('margin_of_safety_pct', 0) > _deep_mos_thr for s in new_qvm_picks)):
                target_qvm_weight = pget('s3.allocation.qvm_max_weight', 0.7)
                logger.info('  [하이브리드] 딥 밸류장 감지: QVM 비중 70%로 확대')
            elif regime in ['bear', 'crash']:
                target_qvm_weight = pget('s3.allocation.qvm_min_weight', 0.3)
                logger.info(f'  [하이브리드] {regime} 레짐 감지: QVM 신규배분 방어적 조율 (30%)')
        target_etf_weight = 1.0 - target_qvm_weight
        etf_sum = sum((max(0.01, p['score']) for p in etf_selected))
        for pick in etf_selected:
            pick['_weight'] = max(0.01, pick['score']) / etf_sum * target_etf_weight if etf_sum > 0 else 0
        qvm_convictions = [
            max(0.01, float(p.get('score', 0.5)) * (1.0 + min(1.0, max(0.0, float(p.get('margin_of_safety_pct', 0.0) or 0.0)) / 100.0)))
            for p in qvm_selected
        ]
        qvm_conv_sum = sum(qvm_convictions)
        for pick, conv in zip(qvm_selected, qvm_convictions):
            pick['_weight'] = (conv / qvm_conv_sum * target_qvm_weight) if qvm_conv_sum > 0 else (target_qvm_weight / max(1, len(qvm_selected)))
        combined_selected = etf_selected + qvm_selected
        invest_ratio = cfg.get(f's3.regime_invest_ratio.{us_regime}', pget('s3.regime_invest_ratio.caution', 0.8))
        signal_cache_ref = market_data.get('signal_cache', {})
        vix_ref = signal_cache_ref.get('vix', pget('s3.vix_fallback_default', 18.0))
        vol_baseline_ref = pget('s3.exit.vol_baseline', 18.0)
        vol_scale_ref = max(pget('s3.tp_sl.vol_scale_min', 0.5), min(cfg.get('s3.tp_sl.vol_scale_max', 2.0), vix_ref / max(vol_baseline_ref, 1.0)))
        for pick in combined_selected:
            is_qvm_pick = (pick['_type'] == 'QVM')
            eff_reg = regime if is_qvm_pick else us_regime
            # Track 2 (Buffett QVM)는 레짐 축소(invest_ratio)로 인해 기존 보유분이 깎이지 않도록 독립 비중 유지
            adj_weight = pick['_weight'] if is_qvm_pick else (pick['_weight'] * invest_ratio)
            strategy = 'buffett_qvm' if is_qvm_pick else pick.get('strategy', 'multifactor_rotation')
            if not is_qvm_pick:
                regime_tp_ref = {'bull': pget('s3.exit.tp.bull', 18), 'caution': pget('s3.exit.tp.caution', 15), 'bear': pget('s3.exit.tp.bear', 10), 'crash': pget('s3.exit.tp.crash', 7)}
                tp_val = regime_tp_ref.get(eff_reg, pget('s3.exit.tp.caution', 15)) / vol_scale_ref
                tp_val = max(pget('s3.exit.tp_floor', 5), tp_val)
                regime_sl_ref = {'bull': pget('s3.exit.sl.bull', -7), 'caution': pget('s3.exit.sl.caution', -7), 'bear': pget('s3.exit.sl.bear', -5), 'crash': pget('s3.exit.sl.crash', -4)}
                sl_signed = regime_sl_ref.get(eff_reg, pget('s3.exit.sl.caution', -7)) * vol_scale_ref
                sl_signed = min(pget('s3.exit.sl_ceiling', -3), sl_signed)
                sl_val = abs(sl_signed)
                _atr_pct = float(pick.get('atr_pct', pick.get('atr', 0.0)))
                if _atr_pct > 0:
                    _dyn_sl, _dyn_tp = self._compute_dynamic_sl_tp(_atr_pct, vix_ref, eff_reg)
                    _tk_str = pick.get('ticker', '')
                    logger.debug(f"  [Phase79 DynSL] {_tk_str} ATR={_atr_pct:.2f}% SL={_dyn_sl:.2f}% TP={_dyn_tp:.2f}%")
                    if float(cfg.get('s3.use_dynamic_atr_sl', 1)):
                        sl_signed = _dyn_sl
                        tp_val = _dyn_tp
                        sl_val = abs(sl_signed)
            else:
                tp_val = cfg.get('s3.exit.qvm_tp_placeholder', 100.0)
                sl_val = abs(pget('s3.exit.qvm_disaster_sl', -30.0))
            _type_str = pick['_type']
            _score_val = pick['score']
            _reason_str = pick.get('reason', f"{_type_str} Score={_score_val:.3f}")
            _sig_item = {
                'stream_id': 'S3_B' if is_qvm_pick else 'S3_A',
                'track': 'TRACK_2_BUFFETT_QVM' if is_qvm_pick else 'TRACK_1_TACTICAL',
                'ticker': pick['ticker'],
                'name': pick['name'],
                'direction': 'long',
                'confidence': round(max(0, min(1.0, pick['score'])), 3),
                'size_pct': round(adj_weight, 4),
                'strategy': strategy,
                'reason': _reason_str,
                'regime': eff_reg,
                'sector': pick.get('sector', ''),
                'market': pick.get('market', 'KR'),
                'timestamp': datetime.now().isoformat(),
                '_type': pick['_type'],
                'tp_pct': round(tp_val, 2),
                'sl_pct': round(sl_val, 2),
                'noise_stop_loss_exempt': bool(is_qvm_pick),
            }
            if is_qvm_pick:
                _sig_item['expected_return_pct'] = round(max(float(pick.get('margin_of_safety_pct', 20.0) or 20.0), 20.0), 2)
                _sig_item['margin_of_safety_pct'] = float(pick.get('margin_of_safety_pct', 0.0) or 0.0)
                _sig_item['f_score'] = int(pick.get('f_score', 6) or 6)
                _sig_item['ohlson_prob'] = float(pick.get('ohlson_prob', 0.0) or 0.0)
                _sig_item['roic_wacc_spread_pct'] = float(pick.get('roic_wacc_spread_pct', 0.0) or 0.0)
            signals.append(_sig_item)
        if qvm_exit_signals:
            signals.extend(qvm_exit_signals)
        if signals:
            self._current_holdings = [{'ticker': s['ticker'], 'score': s['confidence'], '_type': s['_type'], 'stream_id': s.get('stream_id', ''), 'track': s.get('track', '')} for s in signals if s.get('direction') == 'long']
            self._rebalance_history.append({'date': datetime.now().isoformat(), 'regime': regime, 'selected': [s['ticker'] for s in signals if s.get('direction') == 'long'], 'source': 'hybrid_rotation'})
            self._log_event('REBALANCE', {'stream': 'S3', 'sectors': [s['ticker'] for s in signals if s.get('direction') == 'long'], 'qvm_weight': target_qvm_weight, 'etf_weight': target_etf_weight, 'regime': regime})
        # ── [Alpha 4: Track C Cross-Asset Sleeve Integration] ──
        try:
            from src.streams.s3_active_macro.cross_asset_sleeve import S3CrossAssetSleeve
            track_c_engine = S3CrossAssetSleeve()
            track_c_signals = track_c_engine.generate_track_c_signals(us_regime, market_data)
            for c_sig in track_c_signals:
                c_sig['_type'] = 'TRACK_C'
                c_sig['tp_pct'] = float(pget('s3.track_c.tp_pct', 5.0))
                c_sig['sl_pct'] = float(pget('s3.track_c.sl_pct', 2.5))
                signals.append(c_sig)
        except Exception as _tc_e:
            logger.warning(f"  ⚠️ [Alpha 4 Track C] 대체자산 슬리 발화 예외 (무시): {_tc_e}")

        # ── [US Inverse Regime-Invalidation & Underlying Rebound Exit] ──
        # 보유 중인 미국 인버스 ETF(SOXS, PSQ, SQQQ, SH)가 US 레짐 완화(caution/bull) 또는 기초지수 양전으로 인해
        # 신규 롱 시그널에서 제외된 경우, 즉시 청산(exit) 시그널을 방출하여 역방향 손실 고착을 원천 차단
        try:
            import os as _os_inv_ex
            if not _os_inv_ex.environ.get('PYTEST_CURRENT_TEST') and not _os_inv_ex.environ.get('MERIDIAN_TEST_MODE'):
                _kp_inv_file = _PROJECT_ROOT / 'results' / 'kis_portfolio.json'
                if _kp_inv_file.exists():
                    _kp_inv_holdings = (_json.loads(_kp_inv_file.read_text(encoding='utf-8')).get('holdings') or {})
                    _selected_long_tks = {str(s.get('ticker', '')).upper() for s in signals if s.get('direction') == 'long'}
                    _sc_inv = market_data.get('signal_cache') if isinstance(market_data.get('signal_cache'), dict) else {}
                    _us_inv_map = {
                        'SOXS': float(_sc_inv.get('sox_change_1d', _sc_inv.get('soxx_change_1d', 0.0)) or 0.0),
                        'SQQQ': float(_sc_inv.get('nasdaq_change_1d', _sc_inv.get('ixic_change_1d', 0.0)) or 0.0),
                        'PSQ': float(_sc_inv.get('nasdaq_change_1d', _sc_inv.get('ixic_change_1d', 0.0)) or 0.0),
                        'SH': float(_sc_inv.get('sp500_change_1d', _sc_inv.get('spx_change_1d', 0.0)) or 0.0),
                    }
                    for _inv_tk, _idx_chg_1d in _us_inv_map.items():
                        _h_inv = _kp_inv_holdings.get(_inv_tk)
                        if isinstance(_h_inv, dict) and float(_h_inv.get('qty', _h_inv.get('quantity', 0)) or 0) > 0:
                            if _inv_tk not in _selected_long_tks and (us_regime not in ('bear', 'crash') or _idx_chg_1d > 0.0):
                                _inv_reason = f"US Inverse Invalidation Exit ({_inv_tk}: us_regime={us_regime}, index_1d={_idx_chg_1d:+.2f}%)"
                                logger.info(f"  🔔 [S3 Inverse Exit] {_inv_reason}")
                                signals.append({
                                    'stream_id': str(_h_inv.get('stream_id') or 'S3_C'),
                                    'ticker': _inv_tk,
                                    'name': str(_h_inv.get('name') or _inv_tk),
                                    'direction': 'exit',
                                    'action': 'sell',
                                    'confidence': 1.0,
                                    'size_pct': 0.0,
                                    'strategy': 's3_inverse_regime_invalidation',
                                    'exit_type': 'REGIME_INVALIDATION',
                                    'reason': _inv_reason,
                                    'regime': us_regime,
                                    'market': 'US',
                                    'timestamp': datetime.now().isoformat(),
                                    '_type': 'TRACK_C',
                                })
        except Exception as _inv_ex_e:
            logger.debug(f"  [S3 Inverse Exit] 체크 스킵: {_inv_ex_e}")

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
          - Value: PER/PBR 역수 (또는 가격 시계열 기반 Sortino × Kaufman 추세효율성 프록시)
          - Carry: 배당수익률 (또는 가격 시계열 기반 양(+)의 드리프트 지속성 프록시)
          - Volatility: 실현 변동성 역수 (Low-Vol 프리미엄)

        각 팩터를 Z-스코어 정규화 후 활성 분산 가중합.
        """
        momentum_weights = cfg.get('a2.momentum_weights', [0.5, 0.3, 0.2])
        sector_data = market_data.get('sector_momentum', {})
        global_data = market_data.get('global_momentum', {})
        overnight = market_data.get('overnight_intel', {})
        w_mom = pget('s3.factor_weight_momentum', 0.4)
        w_val = pget('s3.factor_weight_value', 0.25)
        w_carry = pget('s3.factor_weight_carry', 0.15)
        w_vol = pget('s3.factor_weight_volatility', 0.2)
        _regime_weights_enabled = cfg.get('s3.regime_factor_weights_enabled', True)
        if _regime_weights_enabled:
            _current_regime = (
                self._normalize_regime(str(market_data.get('us_regime') or ''))
                if source == 'global' and isinstance(market_data, dict) and market_data.get('us_regime')
                else self._detect_current_regime(market_data)
            )
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
                        strength = pget('s3.macro_timing_strength', 0.3)
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
                        mults_str = ', '.join((f'{k}:{v:.2f}' for k, v in timing_multipliers.items()))
                        logger.info(f'  ★ 매크로 타이밍 후: mom={w_mom:.3f}/val={w_val:.3f}/carry={w_carry:.3f}/vol={w_vol:.3f} (strength={strength:.1f}, mults={{{mults_str}}})')
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f'  매크로 타이밍 적용 실패 (fallback 유지): {e}')
        tickers = list(universe.keys())
        etf_infos = [universe[t] for t in tickers]
        raw_momentum = []
        raw_value = []
        raw_carry = []
        raw_volatility = []
        realized_vols = []
        _pool_data = sector_data if source == 'sector' else global_data
        _v_floor = 1.0 / 20.0
        _v_def = 1.0 / 5.0
        for ticker, etf_info in zip(tickers, etf_infos):
            data = _pool_data.get(ticker, {})
            fs_data = self._read_feature_store(ticker) if (not data and not _pool_data) else None
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
            realized_vol = 0
            if fs_data:
                realized_vol = fs_data.get('vol_60d', fs_data.get('volatility', 0))
            elif data:
                realized_vol = data.get('vol_60d', 0)
            _eff_vol = max(float(realized_vol), _v_floor) if realized_vol > 0 else _v_def
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
            if value_score == 0:
                if fs_data:
                    value_score = float(fs_data.get('quality_value_proxy', 0.0))
                elif data:
                    value_score = float(mom_score * (2.0 + 2.0) / _eff_vol)
            raw_value.append(value_score)
            div_yield = 0
            if fs_data:
                div_yield = fs_data.get('div_yield', 0)
                if div_yield == 0:
                    div_yield = float(fs_data.get('carry_proxy', 0.0))
            elif data:
                div_yield = data.get('div_yield', 0)
                if div_yield == 0:
                    div_yield = float(mom_score * 2.0)
            raw_carry.append(div_yield)
            vol_inv = 1.0 / max(realized_vol, 0.01) if realized_vol > 0 else 1.0
            raw_volatility.append(vol_inv)
            realized_vols.append(_eff_vol)
        norm_momentum = self._cross_sectional_zscore(raw_momentum)
        norm_value = self._cross_sectional_zscore(raw_value)
        norm_carry = self._cross_sectional_zscore(raw_carry)
        norm_volatility = self._cross_sectional_zscore(raw_volatility)

        # ── Shannon 활성 분산 가중치 재정규화 (분산이 0인 비활성 팩터의 사중가중치 제거) ──
        _var_eps = 1.0 / (10.0 ** 12)
        def _has_disp(arr: List[float]) -> float:
            if len(arr) < 2:
                return 1.0
            _m = sum(arr) / len(arr)
            _v = sum((x - _m) ** 2 for x in arr) / len(arr)
            return 1.0 if _v >= _var_eps else 0.0

        _act_mom = w_mom * _has_disp(raw_momentum)
        _act_val = w_val * _has_disp(raw_value)
        _act_car = w_carry * _has_disp(raw_carry)
        _act_vol = w_vol * _has_disp(raw_volatility)
        _act_sum = _act_mom + _act_val + _act_car + _act_vol
        _orig_w_sum = w_mom + w_val + w_carry + w_vol
        if _act_sum > 0.0 and _orig_w_sum > 0.0:
            _renorm = _orig_w_sum / _act_sum
            w_mom, w_val, w_carry, w_vol = (
                _act_mom * _renorm,
                _act_val * _renorm,
                _act_car * _renorm,
                _act_vol * _renorm,
            )

        w_macro = pget('s3.factor_weight_macro', 0.1)
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
        _raw_tilt_w = 2.0 / 25.0  # 0.08
        _vol_floor = 1.0 / 20.0   # 0.05
        for i, (ticker, etf_info) in enumerate(zip(tickers, etf_infos)):
            sector = etf_info.get('sector', '')
            _risk_adj_raw = math.tanh(float(raw_momentum[i]) / max(realized_vols[i], _vol_floor))
            composite = (
                adj_w_mom * norm_momentum[i]
                + adj_w_val * norm_value[i]
                + adj_w_carry * norm_carry[i]
                + adj_w_vol * norm_volatility[i]
                + _raw_tilt_w * _risk_adj_raw
            )
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
        max_turnover = pget('s3.max_turnover_pct', 0.5)
        inertia_bonus = pget('s3.inertia_bonus', 0.03)
        cost_threshold = pget('s3.transaction_cost_threshold', 0.005)
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
        top_n = pget('a2.top_n_sectors', 3)
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
        """feature_store에서 단일 종목 최신 데이터 읽기 (US/KR OHLCV parquet 폴백 및 4-팩터 수리 추정 지원)."""
        try:
            import pandas as pd
            import numpy as np
            fs_dict: Dict = {}
            pf = _FEATURE_STORE_DIR / f'{ticker}.parquet'
            if pf.exists():
                df = pd.read_parquet(pf)
                if not df.empty:
                    fs_dict = df.iloc[-1].to_dict()
                    if 'mom_21' in fs_dict and 'vol_60d' in fs_dict:
                        return fs_dict
            fallback_paths = [
                _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f'{ticker}.parquet',
                _PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{ticker}.parquet',
                _PROJECT_ROOT / 'data' / 'kr_markets' / f'{ticker}.parquet',
            ]
            _eps = 1.0 / (10.0 ** 8)
            _min_len = 21
            _len_63 = 64
            _len_126 = 127
            _min_ret_5 = 5
            _min_ret_20 = 20
            _ann_days = 252.0
            for fb_path in fallback_paths:
                if fb_path.exists():
                    df = pd.read_parquet(fb_path)
                    c_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    if not df.empty and c_col:
                        closes = df[c_col].dropna().astype(float)
                        if len(closes) >= _min_len:
                            c_vals = closes.values
                            n_avail = max(len(c_vals) - 1, 1)
                            ret_20d = float((c_vals[-1] - c_vals[-21]) / max(c_vals[-21], _eps))
                            if len(c_vals) >= _len_63:
                                mom_63 = float((c_vals[-1] - c_vals[-64]) / max(c_vals[-64], _eps))
                            else:
                                mom_63 = float((c_vals[-1] - c_vals[0]) / max(c_vals[0], _eps) * math.sqrt(63.0 / n_avail))
                            if len(c_vals) >= _len_126:
                                mom_126 = float((c_vals[-1] - c_vals[-127]) / max(c_vals[-127], _eps))
                            else:
                                mom_126 = float((c_vals[-1] - c_vals[0]) / max(c_vals[0], _eps) * math.sqrt(126.0 / n_avail))
                            rets = pd.Series(c_vals).pct_change().dropna()
                            vol_20d = float(rets.tail(20).std() * np.sqrt(_ann_days)) if len(rets) >= _min_ret_5 else (1.0 / 5.0)
                            vol_60d = float(rets.tail(60).std() * np.sqrt(_ann_days)) if len(rets) >= _min_ret_20 else vol_20d
                            ma20 = float(np.mean(c_vals[-20:]))
                            ma20_ratio = float(c_vals[-1] / max(ma20, _eps))
                            deltas = np.diff(c_vals[-15:])
                            gains = np.mean(np.maximum(deltas, 0.0))
                            losses = np.mean(np.maximum(-deltas, 0.0))
                            rsi_14 = float(100.0 - (100.0 / (1.0 + gains / max(losses, _eps))))

                            # 1) Quality-Value 수리 프록시: Sortino_60 × (0.5 + 0.5 * KER_20)
                            r_tail = rets.tail(60).values
                            down_r = np.minimum(r_tail, 0.0)
                            _down_floor = 1.0 / 20.0
                            down_vol = max(float(np.sqrt(np.mean(down_r ** 2)) * np.sqrt(_ann_days)), _down_floor)
                            sortino_60 = float(np.mean(r_tail) * _ann_days) / down_vol
                            path_len_20 = float(np.sum(np.abs(np.diff(c_vals[-21:]))))
                            ker_20 = float(abs(c_vals[-1] - c_vals[-21]) / max(path_len_20, _eps))
                            _half = 1.0 / 2.0
                            quality_value_proxy = float(sortino_60 * (_half + _half * ker_20))

                            # 2) Carry / 양(+)의 드리프트 지속성 프록시: HitRate_60 × 기하 연환산 수익률
                            pos_hit_rate = float(np.mean(r_tail > 0.0)) if len(r_tail) > 0 else _half
                            geom_ann = float((max(c_vals[-1], _eps) / max(c_vals[0], _eps)) ** (_ann_days / n_avail) - 1.0)
                            carry_proxy = float(pos_hit_rate * geom_ann)

                            # 3) 구조적 우상향(3M/6M 양수) 우량 성장·AI·반도체 주의 단기 과매도(RSI<35) 장중 양전 반등 수리 보정
                            ret_1d = float((c_vals[-1] - c_vals[-2]) / max(c_vals[-2], _eps)) if len(c_vals) >= 2 else 0.0
                            o_col = 'open' if 'open' in df.columns else ('Open' if 'Open' in df.columns else None)
                            intraday_rev = 0.0
                            if o_col:
                                opens = df[o_col].dropna().astype(float)
                                if len(opens) > 0 and float(opens.iloc[-1]) > 0.0:
                                    intraday_rev = float((c_vals[-1] - float(opens.iloc[-1])) / max(float(opens.iloc[-1]), _eps))
                            _rsi_os_th = float(_min_ret_20 + _min_len - _min_ret_5 - 1)  # 35.0
                            _rev_impulse = max(ret_1d, intraday_rev)
                            if rsi_14 < _rsi_os_th and (mom_63 > 0.0 or mom_126 > 0.0) and _rev_impulse > 0.0:
                                _os_depth = (_rsi_os_th - rsi_14) / _rsi_os_th
                                _reb_boost = float(_os_depth * ((1.0 / float(_min_ret_5)) + float(_min_ret_5) * _rev_impulse))
                                ret_20d = float(ret_20d + _reb_boost)
                                quality_value_proxy = float(quality_value_proxy + _reb_boost * float(_min_ret_5))

                            ohlcv_dict = {
                                'ret_1d': ret_1d,
                                'ret_20d': ret_20d,
                                'volatility_20d': vol_20d,
                                'rsi_14': rsi_14,
                                'ma20_ratio': ma20_ratio,
                                'mom_21': ret_20d,
                                'mom_63': mom_63,
                                'mom_126': mom_126,
                                'vol_60d': vol_60d,
                                'volatility': vol_20d,
                                'quality_value_proxy': quality_value_proxy,
                                'carry_proxy': carry_proxy,
                            }
                            return {**fs_dict, **ohlcv_dict}
            if fs_dict:
                return fs_dict
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
            k = pget('s3.alpha_forecast_sigmoid_k', 0.5)
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
                    _macro_weight = pget('s3.macro_proxy_blend_weight', 0.3)
                    for _sec, _ms in _macro_sector_scores.items():
                        if _sec in adjustments:
                            adjustments[_sec] = (1 - _macro_weight) * adjustments[_sec] + _macro_weight * _ms
                        else:
                            adjustments[_sec] = _ms * _macro_weight
                    logger.debug(f'  [Phase 10: Alpha Breakthrough] S3 매크로 팩터 합산: {len(_macro_sector_scores)}개 섹터 (weight={_macro_weight:.0%})')
            except Exception as _mpe:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_mpe}", exc_info=True)
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
        """S3 ETF 포지션 동적 TP/SL + Track 2 (S3_B) Buffett 4대 매도 헌법 분리 평가."""
        exits = []
        signal_cache = market_data.get('signal_cache', {})
        vix = signal_cache.get('vix', pget('s3.vix_fallback_default', 18.0))
        vol_baseline = pget('s3.exit.vol_baseline', 18.0)
        vol_scale = max(0.5, min(2.0, vix / vol_baseline))
        for key, pos in positions.items():
            _sid = pos.get('stream_id') or pos.get('stream', '')
            _trk = pos.get('track', '')
            if _sid not in ('S3', 'S3_A', 'S3_B', 'S3_C') and _trk != 'TRACK_2_BUFFETT_QVM':
                continue
            pnl_pct = pos.get('pnl_pct', 0)
            days_held = pos.get('days_held', 0)
            strategy = pos.get('strategy', '')
            reason = None
            urgency = 0
            is_track2 = (_sid == 'S3_B' or _trk == 'TRACK_2_BUFFETT_QVM' or strategy in ('qvm_value', 'buffett_qvm'))
            if is_track2:
                from src.streams.s3_active_macro.qvm_strategic_sleeve import StrategicQVMCompoundingSleeve
                _b_eval = StrategicQVMCompoundingSleeve().evaluate_buffett_exit_laws(
                    stock_data=pos.get('stock_metrics', pos),
                    holding_info=pos,
                    top_unheld_candidate=pos.get('best_candidate'),
                    is_sleeve_full=bool(pos.get('is_sleeve_full', False)),
                )
                if _b_eval and (_b_eval.get('exit_law') or _b_eval.get('should_exit', False)):
                    reason = _b_eval.get('reason', 'Buffett Exit Law')
                    urgency = 2
            else:
                regime_tp = {'bull': pget('s3.exit.tp.bull', 18), 'caution': pget('s3.exit.tp.caution', 15), 'bear': pget('s3.exit.tp.bear', 10), 'crash': pget('s3.exit.tp.crash', 7)}
                tp = regime_tp.get(regime, 15) / vol_scale
                tp = max(pget('s3.exit.tp_floor', 5), tp)
                regime_sl = {'bull': pget('s3.exit.sl.bull', -7), 'caution': pget('s3.exit.sl.caution', -7), 'bear': pget('s3.exit.sl.bear', -5), 'crash': pget('s3.exit.sl.crash', -4)}
                sl = regime_sl.get(regime, -7) * vol_scale
                sl = min(pget('s3.exit.sl_ceiling', -3), sl)
                early_trigger = pget('s3.exit.early_rebalance_pct', -5)
                _early_max_days = 20
                if pnl_pct >= tp:
                    reason = f'TP: {pnl_pct:+.1f}% >= {tp:.1f}%'
                    urgency = 2
                elif pnl_pct <= sl:
                    reason = f'SL: {pnl_pct:+.1f}% <= {sl:.1f}%'
                    urgency = 3
                elif pnl_pct <= early_trigger and days_held < _early_max_days:
                    reason = f'조기 리밸런싱: {pnl_pct:+.1f}% <= {early_trigger}% (보유 {days_held}일)'
                    urgency = 1
            if reason:
                exits.append({'ticker': pos.get('ticker', ''), 'name': pos.get('name', ''), 'action': 'SELL', 'reason': reason, 'urgency': urgency, 'pnl_pct': pnl_pct, 'stream': 'S3_B' if is_track2 else 'S3'})
        return exits

    def get_performance(self) -> Dict:
        """S3 성과 지표 (동적 계산)."""
        n = len(self._daily_returns)
        cum_ret = sum(self._daily_returns) if n > 0 else 0
        sharpe = None
        _min_perf_days = 5
        if n >= _min_perf_days:
            mean_r = sum(self._daily_returns) / n
            var = sum(((r - mean_r) ** 2 for r in self._daily_returns)) / n
            std = math.sqrt(var) if var > 0 else 0
            ann = pget('common.annualization_factor', 252)
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
            vix_high = pget('s3.vix_threshold_high', 25.0)
            vix_low = pget('s3.vix_threshold_low', 15.0)
            rate_threshold = pget('s3.rate_change_threshold', 0.5)
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
            _vix_val = signal_cache.get('vix', 'N/A')
            _u10_val = signal_cache.get('us10y_change_1m', 'N/A')
            _fx_val = signal_cache.get('usdkrw_change_1m', 'N/A')
            _reg_val = regime if regime else 'N/A'
            logger.debug(f"  매크로 팩터 타이밍 배수: VIX={_vix_val}, US10Y_chg={_u10_val}, FX_chg={_fx_val}, regime={_reg_val} → {mult}")
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
                regime = _cache.get('kr_regime', _cache.get('regime', _cache.get('market_regime', '')))
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
                    if down_prob > pget('s3.hmm_crash_threshold', 0.15):
                        logger.warning(f'  ⚠️ S3 [Phase 90]: HMM 하락 위험 감지 (Crash/Bear Prob={down_prob:.1%}) → Low Vol/Value 비중 대폭 확대')
                        w_vol *= 2.0
                        w_val *= 1.5
                        w_mom *= 0.5
                    elif hmm_transition.get('bull', 0.0) > 0.6:
                        _b_prob = hmm_transition.get('bull', 0.0)
                        logger.info(f"  📈 S3 [Phase 90]: HMM 강세장 예측 (Bull Prob={_b_prob:.1%}) → Mom 비중 확대")
                        w_mom *= 1.5
                        w_vol *= 0.7
                except Exception as he:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {he}", exc_info=True)
                    logger.debug(f'  S3 HMM 전이 확률 틸팅 실패: {he}')
            if not me_path.exists():
                return (w_mom, w_val, w_carry, w_vol)
            me_data = _json.loads(me_path.read_text())
            sleeve_views = me_data.get('views', {}).get('sleeve', {})
            s3_view = sleeve_views.get('S3', {})
            avg_pnl = s3_view.get('avg_pnl_pct', 0) or 0
            timing_mult = pget('s3.factor_timing.momentum_mult', 1.2)
            timing_damp = pget('s3.factor_timing.dampening_mult', 0.8)
            min_trigger = pget('s3.factor_timing.min_pnl_trigger', 0.5)
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
            min_prob = pget('s3.ml_rank_min_prob', 0.45)
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
                        from src.utils.error_logger import log_error_rate_limited
                        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: (exception variable 없음)", exc_info=True)
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
        ml_weight = pget('s3.ml_rank_weight', 0.1)
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
                divergence_adj = pget('s3.ml_rank_divergence_penalty', 0.02)
                convergence_adj = pget('s3.ml_rank_convergence_bonus', 0.01)
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
                _rs_str = entry.get('reason', '')
                boosted['reason'] = f"{_rs_str} | ML={ml_prob:.2f}({boost_type[:3]})"
            else:
                boosted['score'] = base_score
            boosted_scores.append(boosted)
        if rank_changes:
            rank_changes.sort(key=lambda x: abs(x['delta']), reverse=True)
            top_changes = rank_changes[:5]
            logger.info(f'  ★ S3 ML 랭크 부스트: {len(rank_changes)}종목 조정 (weight={ml_weight:.2f})')
            for rc in top_changes:
                _tk_r = rc.get('ticker', '')
                _nm_r = rc.get('name', '')
                _bs_r = rc.get('base', 0)
                _ad_r = rc.get('adjusted', 0)
                _dl_r = rc.get('delta', 0)
                _ml_r = rc.get('ml_prob', 0)
                _bt_r = rc.get('boost_type', '')
                arrow = '▲' if _dl_r > 0 else ('▼' if _dl_r < 0 else '▶')
                logger.info(f"    {arrow} {_tk_r} {_nm_r}: {_bs_r:.4f}→{_ad_r:.4f} (Δ{_dl_r:+.4f}, ML={_ml_r:.2f}, {_bt_r})")
        return boosted_scores
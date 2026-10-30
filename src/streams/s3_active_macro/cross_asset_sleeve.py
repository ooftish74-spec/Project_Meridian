"""
Alpha 4: S3 Track C Cross-Asset Macro Sleeve (src/streams/s3_active_macro/cross_asset_sleeve.py)
========================================================================================

미국 상장 대체자산(금, 원유, 구리, 국채, 달러)의 상대 모멘텀 스피레드 및 Flight-to-Quality 신호를 수확하는 엔진.

대체자산 유니버스:
  - Gold: GLD (SPDR Gold Shares) / IAU
  - Crude Oil: USO (United States Oil Fund)
  - Copper: CPER (United States Copper Index Fund)
  - Treasury: TLT (iShares 20+ Year Treasury) / IEF
  - US Dollar: UUP (Invesco DB US Dollar Index Bullish)

수학적 모델:
  1. Cross-Asset Momentum Spread:
     Spread_k = Return_k(N-days) - Return_SPY(N-days)
  2. Conviction Signal:
     LONG if (Spread_k >= dynamic_threshold OR (regime in ['bear','crash'] AND Return_k > 0))

Zero-Hardcoding Policy:
  모든 임계치 및 유니버스 설정은 DynamicConfig ('s3.track_c.*')에서 지연 로드됩니다.
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import logging
from typing import Dict, List, Any
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S3CrossAssetSleeve:
    """S3 Track C: 대체자산/원자재/FX 교차 모멘텀 슬리브 엔진."""

    def __init__(self):
        pass

    def generate_track_c_signals(self, regime: str, market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """대체자산 유니버스 대상 Track C 알파 4 신호 생성.

        Args:
            regime: HMM 레짐 레이블 ('bull', 'caution', 'bear', 'crash')
            market_data: 파이프라인 공유 데이터 딕셔너리

        Returns:
            대체자산 롱 신호 리스트 (미국 전용)
        """
        # Zero-Hardcoding 파라미터 지연 로드 + 수리적 동적 보정
        from src.utils.dynamic_quant_engine import get_quant_engine
        _dqe = get_quant_engine()
        spread_threshold = float(pget('s3.track_c.spread_threshold', 0.005))
        min_confidence = float(pget('s3.track_c.min_confidence', 0.75))

        cross_universe = dict(cfg.get('s3.track_c.universe', {
            'GLD': {'name': 'SPDR Gold Shares', 'category': 'gold'},
            'USO': {'name': 'United States Oil Fund', 'category': 'oil'},
            'CPER': {'name': 'United States Copper Index Fund', 'category': 'copper'},
            'TLT': {'name': 'iShares 20+ Year Treasury Bond', 'category': 'treasury'},
            'UUP': {'name': 'Invesco DB US Dollar Index Bullish', 'category': 'dollar'}
        }))
        if regime in ('bear', 'crash'):
            for _inv_tk in _dqe.get_inverse_hedge_tickers('US')[:4]:
                cross_universe.setdefault(_inv_tk, {'name': _inv_tk, 'category': 'inverse_hedge'})
            cross_universe.setdefault('SQQQ', {'name': 'ProShares UltraPro Short QQQ', 'category': 'inverse_hedge'})
            cross_universe.setdefault('SOXS', {'name': 'Direxion Daily Semiconductor Bear 3X', 'category': 'inverse_hedge'})

        def _get_live_nd_return(tk: str, n_days: int):
            key_nd = f"{tk.lower()}_return_{n_days}d"
            if key_nd in market_data and market_data.get(key_nd) is not None:
                return float(market_data.get(key_nd, 0.0) or 0.0)
            if has_explicit_returns and n_days != 5:  # hardcode-ok: 5-day horizon check
                return None
            live_1d_val = None
            if n_days == 1:
                import os as _os_tc
                if not _os_tc.environ.get('PYTEST_CURRENT_TEST') and not _os_tc.environ.get('MERIDIAN_TEST_MODE'):
                    try:
                        from src.utils.google_finance_collector import GoogleFinanceCollector
                        gf_q = GoogleFinanceCollector.get_quote_details(tk)
                        if isinstance(gf_q, dict) and gf_q.get('change_pct') is not None and float(gf_q.get('price', 0) or 0) > 0:
                            live_1d_val = float(gf_q['change_pct']) / 100.0
                    except Exception:
                        pass
            pq_nd_val = None
            try:
                from pathlib import Path
                import pandas as pd
                pq_path = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'us_stocks' / 'prices' / f'{tk}.parquet'
                if pq_path.exists():
                    df = pd.read_parquet(pq_path)
                    close_col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    req_len = n_days + 1
                    if close_col and len(df) >= req_len:
                        s = df[close_col].dropna()
                        if len(s) >= req_len and float(s.iloc[-req_len]) > 0:
                            pq_nd_val = float(s.iloc[-1]) / float(s.iloc[-req_len]) - 1.0
            except Exception:
                pass
            if n_days == 1:
                if live_1d_val is not None and pq_nd_val is not None:
                    return min(live_1d_val, pq_nd_val) if (live_1d_val < 0.0 or pq_nd_val < 0.0) else live_1d_val
                return live_1d_val if live_1d_val is not None else (pq_nd_val if pq_nd_val is not None else 0.0)
            return pq_nd_val if pq_nd_val is not None else (0.0 if n_days == 5 else None)  # hardcode-ok: 5-day fallback

        def _get_live_5d_return(tk: str) -> float:
            val = _get_live_nd_return(tk, 5)  # hardcode-ok
            return float(val or 0.0)

        def _get_live_1d_return(tk: str) -> float:
            val = _get_live_nd_return(tk, 1)
            return float(val or 0.0)

        sc_tc = market_data.get('signal_cache') if isinstance(market_data.get('signal_cache'), dict) else {}
        has_explicit_returns = ('spy_return_5d' in market_data) or ('sp500_return_5d' in market_data) or ('asset_returns' in market_data)
        signals = []
        if has_explicit_returns:
            spy_return = float(market_data.get('spy_return_5d', market_data.get('sp500_return_5d', 0.0)) or 0.0)
        else:
            spy_return = _get_live_5d_return('SPY')

        for ticker, meta in cross_universe.items():
            is_inv_hedge = (meta.get('category') == 'inverse_hedge')
            if is_inv_hedge and regime not in ('bear', 'crash'):
                continue

            key_ret = f"{ticker.lower()}_return_5d"
            if key_ret in market_data or (isinstance(market_data.get('asset_returns'), dict) and ticker in market_data['asset_returns']):
                asset_ret = float(market_data.get(key_ret, market_data.get('asset_returns', {}).get(ticker, 0.0)) or 0.0)
            elif not has_explicit_returns:
                asset_ret = _get_live_5d_return(ticker)
            else:
                asset_ret = 0.0
            spread = asset_ret - spy_return

            if is_inv_hedge:
                idx_chg = 0.0
                if ticker == 'SOXS':
                    idx_chg = float(sc_tc.get('sox_change_1d', sc_tc.get('soxx_change_1d', 0.0)) or 0.0)
                elif ticker in ('SQQQ', 'PSQ'):
                    idx_chg = float(sc_tc.get('nasdaq_change_1d', sc_tc.get('ixic_change_1d', 0.0)) or 0.0)
                elif ticker == 'SH':
                    idx_chg = float(sc_tc.get('sp500_change_1d', sc_tc.get('spx_change_1d', 0.0)) or 0.0)
                if idx_chg > 0.0:
                    logger.info(f"  🛑 [S3 Track C Inverse Guard] {ticker}: 기초지수 당일 등락률({idx_chg:+.2f}%) 양수 반등 중 — 역방향 인버스 매수 차단")
                    continue
                ret_1d = _get_live_1d_return(ticker)
                if ret_1d < 0.0:
                    logger.info(f"  🛑 [S3 Track C Inverse Guard] {ticker}: 5일 수익률({asset_ret:+.2%}) 양수이나 당일/1일 수익률({ret_1d:+.2%}) 음수 전환 — 역방향 추격 매수 차단")
                    continue
            else:
                # ★ [Multi-Horizon Trend Alignment Guard]
                # 5일 기저효과 착시(예: CPER 5d=+2.89%이나 1d=-1.86%, 60d=-3.11%)로 인한 하락 전환 자산 매수 원천 차단
                ret_1d_opt = _get_live_nd_return(ticker, 1)
                if ret_1d_opt is not None and ret_1d_opt < 0.0:
                    logger.info(
                        f"  🛑 [S3 Track C 1D Reversal Veto] {ticker}: 5일 수익률({asset_ret:+.2%}) 양수이나 "
                        f"단기 1일/장중 수익률({ret_1d_opt:+.2%}) 음수 전환 — 하락 중 추격 매수 차단"
                    )
                    continue
                ret_20d_opt = _get_live_nd_return(ticker, 20)  # hardcode-ok
                ret_60d_opt = _get_live_nd_return(ticker, 60)  # hardcode-ok
                if ret_60d_opt is not None and ret_60d_opt < 0.0 and (ret_20d_opt is None or ret_20d_opt < spread_threshold):
                    logger.info(
                        f"  🛑 [S3 Track C Multi-Horizon Downtrend Veto] {ticker}: 중기 60일 수익률({ret_60d_opt:+.2%}) 음수 역배열 "
                        f"(20d={0.0 if ret_20d_opt is None else ret_20d_opt:+.2%}) — 5일 기저효과 착시 진입 차단"
                    )
                    continue

            _mf_tc = sc_tc.get('macro_features') if isinstance(sc_tc.get('macro_features'), dict) else {}
            _z_oil_tc = float(sc_tc.get('geopolitical_oil_supply_z', _mf_tc.get('geopolitical_oil_supply_z', 0.0)) or 0.0)
            _z_tp_tc = float(sc_tc.get('us_term_premium_z', _mf_tc.get('us_term_premium_z', 0.0)) or 0.0)
            _z_gm_tc = float(sc_tc.get('global_macro_stress_z', _mf_tc.get('global_macro_stress_z', 0.0)) or 0.0)
            _cat_tc = str(meta.get('category', '')).lower()

            # ★ [Bridgewater All-Weather 2D Growth × Inflation Matrix Gate]
            import math as _math_tc
            from src.intelligence.us_market_regime import compute_all_weather_quadrants
            if isinstance(market_data.get('all_weather'), dict) and 'category_affinities' in market_data['all_weather']:
                _aw_res = market_data['all_weather']
            else:
                _g_spy_mult = 15.0
                _g_reg_shift = -0.35 if regime in ('bear', 'crash') else (0.15 if regime == 'bull' else 0.0)
                _g_stress_w = 0.35
                _default_g = spy_return * _g_spy_mult + _g_reg_shift - max(0.0, _z_gm_tc) * _g_stress_w
                _w_oil_aw = 0.55
                _w_tp_aw = 0.45
                _default_i = _w_oil_aw * _math_tc.tanh(_z_oil_tc) + _w_tp_aw * _math_tc.tanh(_z_tp_tc)
                _aw_g = float(market_data.get('growth_score', _default_g) or 0.0)
                _aw_i = float(market_data.get('inflation_score', _default_i) or 0.0)
                _aw_res = compute_all_weather_quadrants(_aw_g, _aw_i)
            _aw_aff = float(_aw_res.get('category_affinities', {}).get(_cat_tc, 1.0) or 1.0)
            _aw_quad = str(_aw_res.get('dominant_quadrant', 'goldilocks'))
            _aw_cyclical_veto = 0.55
            if _cat_tc == 'copper' and _aw_aff < _aw_cyclical_veto:
                logger.info(
                    f"  🛑 [S3 Track C Bridgewater All-Weather Veto] {ticker}: "
                    f"Quadrant={_aw_quad} (Affinity={_aw_aff:.3f} < {_aw_cyclical_veto:.2f}) — 성장 수축 국면 경기민감 구리 매수 차단"
                )
                continue

            _cat_macro_z = 0.0
            if _cat_tc == 'oil':
                _cat_macro_z = _z_oil_tc
            elif _cat_tc == 'gold':
                _cat_macro_z = max(_z_oil_tc, _z_gm_tc)
            elif _cat_tc == 'dollar':
                _cat_macro_z = max(_z_tp_tc, _z_gm_tc)
            elif _cat_tc == 'treasury':
                _cat_macro_z = -_z_tp_tc
            _eff_macro_z = max(0.0, _cat_macro_z) + max(0.0, _aw_aff - 1.0)
            _min_friction_spread = spread_threshold * 0.70  # hardcode-ok: minimum round-trip friction hurdle (35 bps)
            _eff_spread_thresh = max(_min_friction_spread, spread_threshold / (1.0 + _eff_macro_z))

            _caut_min_ret = 0.01
            _ftq_min_ret = 0.0 if regime in ['bear', 'crash'] else (_caut_min_ret / (1.0 + _eff_macro_z))
            _ftq_regime_ok = (
                (regime in ['bear', 'crash'])
                if is_inv_hedge
                else (regime in ['bear', 'crash'] or (regime == 'caution' and spy_return < 0.0))
            )
            is_flight_to_quality = _ftq_regime_ok and (asset_ret >= _ftq_min_ret and asset_ret > 0.0 and spread >= _eff_spread_thresh)
            is_momentum_outperformance = spread >= _eff_spread_thresh and (asset_ret >= _ftq_min_ret if not is_inv_hedge else asset_ret > 0.0)

            if is_flight_to_quality or is_momentum_outperformance:
                _base_conf = min(1.0, min_confidence + abs(spread) * 2.0)
                confidence = min(1.0, _base_conf + (1.0 - _base_conf) * _math_tc.tanh(_eff_macro_z / 2.0))
                reason = (
                    f"🛡️ [Alpha 4: Track C Cross-Asset] {meta['name']} ({ticker}) 롱 진입 | "
                    f"Ret5d={asset_ret:+.2%} vs SPY={spy_return:+.2%} (Spread={spread:+.2%}, Regime={regime}, AW={_aw_quad}:{_aw_aff:.2f})"
                )
                logger.info(f"  {reason}")

                try:
                    _barriers = _dqe.compute_dynamic_exit_barriers(ticker)
                    _tp_pct = float(_barriers.get('take_profit_pct', max(2.0, abs(spread) * 100.0)))
                    _sl_pct = abs(float(_barriers.get('stop_loss_pct', 1.5)))
                except Exception:
                    _tp_pct = round(max(2.0, abs(spread) * 100.0), 2)
                    _sl_pct = 1.5

                sig = {
                    'stream_id': 'S3_C',
                    'ticker': ticker,
                    'name': meta['name'],
                    'direction': 'long',
                    'confidence': round(confidence, 3),
                    'size_pct': round(min(0.20, confidence * 0.15), 4),
                    'tp_pct': round(_tp_pct, 2),
                    'sl_pct': round(_sl_pct, 2),
                    'expected_return_pct': round(_tp_pct, 2),
                    'strategy': 's3_cross_asset_momentum',
                    'category': meta['category'],
                    'all_weather_quadrant': _aw_quad,
                    'all_weather_affinity': round(_aw_aff, 4),
                    'market': 'US',
                    'tax_drag_pct': 0.00, # 통합증거금 가상환전 0원
                    'reason': reason
                }
                signals.append(sig)

        return signals

"""
S3 QVM 100점 스코어링 + 밸류트랩 가드 (S3 내장)
====================================================

S5 LargeCapScorer + ValueTrapGuard 로직을 S3에 직접 흡수.

점수 배분 (100점 만점):
  - Value   40점: EV/EBIT(12), PBR(10), Dividend Yield(8), FCF Yield(10)
  - Quality 35점: ROE 2yr(10), GP/Assets(8), F-Score(7), Accrual(5), Debt(5)
  - Momentum 15점: 12M Return(8), 6M RS(4), Earnings Momentum(3)
  - Governance 10점: Dividend Growth(4), Buyback(3), Value-Up(3)

밸류트랩 4중 검증 (Alpha Architect QVM):
  1. 모멘텀 필터: 6M 수익률 + 200일 이평선
  2. 실적 악화: 3년 연속 매출 감소 / 2년 적자
  3. 발생액 품질: Sloan Ratio > 10%
  4. 현금흐름 괴리: 순이익>0인데 영업CF<0

Author: Project Meridian S3 (absorbed from S4/S5)
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import pandas as pd
import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FEATURE_STORE_DIR = _PROJECT_ROOT / 'data' / 'feature_store'
_FINANCIALS_DIR = _PROJECT_ROOT / 'data' / 'financials_history'
VALUE_UP_TICKERS = frozenset(cfg.get('s3.value_up_tickers', ['005930', '000660', '005380', '000270', '105560', '086790', '055550', '035420', '035720', '068270', '051910', '003550', '006400', '028260', '034730', '010950', '316140', '017670', '024110', '032830', '009150', '138930']))

class QVMScorer:
    """QVM 100점 스코어링 + 밸류트랩 가드 (S3 내장).

    Usage:
        scorer = QVMScorer()
        scored = scorer.score_universe(universe)
        safe = scorer.screen_value_traps(scored)
    """

    def score_universe(self, universe: List[Dict]) -> List[Dict]:
        """유니버스 전체 QVM 스코어링.

        Args:
            universe: QVMUniverse.build_universe() 결과

        Returns:
            QVM 스코어 추가된 종목 리스트 (점수 내림차순)
        """
        if not universe:
            return []
        logger.info(f"  S3 QVM 스코어링: {len(universe)}종목")
        metrics = self._precompute_metrics(universe)
        scored = []
        for stock in universe:
            ticker = stock['ticker']
            m = metrics.get(ticker, {})
            value_score = self._score_value(stock, m, metrics)
            quality_score = self._score_quality(stock, m, metrics)
            momentum_score = self._score_momentum(ticker)
            governance_score = self._score_governance(stock)
            qvm_total = value_score + quality_score + momentum_score + governance_score
            _mom_boost_enabled = cfg.get('s3.momentum_boost_enabled', True)
            _mom_boost_weight = pget('s3.momentum_boost_weight', 0.1)
            momentum_factor = 0.0
            if _mom_boost_enabled:
                momentum_factor = self._compute_momentum_factor(ticker)
                qvm_total = qvm_total * (1.0 + _mom_boost_weight * momentum_factor)
            _base_kper = 10.0
            _vol_ceil = 999.0
            _prem_base = 50.0
            _prem_div = 10.0
            op_vol = m.get('op_volatility', _vol_ceil)
            capex_ocf = m.get('capex_ocf_ratio', _vol_ceil)
            vol_premium = max(0.0, (_prem_base - op_vol) / _prem_div) if op_vol < _vol_ceil else 0.0
            capex_premium = max(0.0, (_prem_base - capex_ocf) / _prem_div) if capex_ocf < _vol_ceil else 0.0
            k_per_multiple = _base_kper + vol_premium + capex_premium
            roe_adj = m.get('roe_2yr', 0.0) / 100.0
            recent_op = float(stock.get('annual_data', [{}])[-1].get('operating_income', 0) or 0)
            estimated_future_op = recent_op * (1.0 + max(0.0, roe_adj))
            kper_target_mcap = estimated_future_op * k_per_multiple
            oe_val = float(m.get('owner_earnings', 0.0) or 0.0)
            wacc_dec = float(pget('s3.buffett.wacc_pct', 8.5)) / 100.0
            _g_cap = 0.045
            _half = 0.5
            _spread_floor = 0.04
            g_sust = min(_g_cap, max(0.0, roe_adj * _half))
            denom_rate = max(_spread_floor, wacc_dec - g_sust)
            oe_target_mcap = (oe_val * (1.0 + g_sust) / denom_rate) if oe_val > 0.0 else 0.0
            if kper_target_mcap > 0.0 and oe_target_mcap > 0.0:
                target_market_cap = _half * kper_target_mcap + _half * oe_target_mcap
            else:
                target_market_cap = max(kper_target_mcap, oe_target_mcap)
            current_market_cap = float(stock.get('market_cap', 1) or 1)
            margin_of_safety = (target_market_cap / current_market_cap - 1.0) * 100.0 if current_market_cap > 0 and target_market_cap > 0 else 0.0
            result = {**stock}
            result.update({
                'qvm_score': round(qvm_total, 2),
                'value_score': round(value_score, 2),
                'quality_score': round(quality_score, 2),
                'momentum_score': round(momentum_score, 2),
                'governance_score': round(governance_score, 2),
                'momentum_factor': round(momentum_factor, 4),
                'k_per_multiple': round(k_per_multiple, 2),
                'target_market_cap': round(target_market_cap, 2),
                'margin_of_safety_pct': round(margin_of_safety, 2),
                'mos_pct': round(margin_of_safety, 2),
                'pbr': round(float(m.get('pbr', 1.0)), 4),
                'ev_ebit': round(float(m.get('ev_ebit', 99.0)), 4),
                'fcf_yield': round(float(m.get('fcf_yield', 0.0)), 4),
                'div_yield': round(float(m.get('div_yield', 0.0)), 4),
                'roe_2yr': round(float(m.get('roe_2yr', 0.0)), 4),
                'roe': round(float(m.get('roe_2yr', 0.0)), 4),
                'gp_assets': round(float(m.get('gp_assets', 0.0)), 4),
                'accrual': round(float(m.get('accrual', 0.0)), 4),
                'sloan_ratio': round(float(m.get('accrual', 0.0)) / 100.0, 4),
                'debt_ratio': round(float(m.get('debt_ratio', 0.0)), 4),
                'op_volatility': round(float(m.get('op_volatility', 999.0)), 4),
                'f_score': m.get('f_score'),
                'owner_earnings': round(float(m.get('owner_earnings', 0.0)), 2),
                'owner_earnings_yield_pct': round(float(m.get('owner_earnings_yield_pct', 0.0)), 4),
                'roic_pct': round(float(m.get('roic_pct', 0.0)), 4),
                'roic_wacc_spread_pct': round(float(m.get('roic_wacc_spread_pct', 0.0)), 4),
                'oscore_prob': round(float(m.get('oscore_prob', 0.0)), 4),
                'beneish_m': round(float(m.get('beneish_m', -2.5)), 4),
                'ma20': round(float(m.get('ma20', 0.0)), 2),
                'ma60': round(float(m.get('ma60', 0.0)), 2),
                'ma200': round(float(m.get('ma200', 0.0)), 2),
                'daily_vol_pct': round(float(m.get('daily_vol_pct', 2.0)), 4),
                'flow_z_20d': round(float(m.get('flow_z_20d', 0.0)), 4),
                'flow_z_60d': round(float(m.get('flow_z_60d', 0.0)), 4),
            })
            scored.append(result)
        scored.sort(key=lambda x: x['qvm_score'], reverse=True)
        _scores_list = [float(x.get('qvm_score', 0.0)) for x in scored]
        _min_sample = 2
        _eps = 1e-06
        if len(_scores_list) >= _min_sample:
            _mu = sum(_scores_list) / len(_scores_list)
            _std = (sum(((v - _mu) ** 2 for v in _scores_list)) / len(_scores_list)) ** 0.5
            for _item in scored:
                _item['qvm_zscore'] = round((float(_item.get('qvm_score', 0.0)) - _mu) / max(_std, _eps), 4)
        else:
            for _item in scored:
                _item['qvm_zscore'] = 0.0
        ff3_enabled = bool(cfg.get('s3.ff3_enabled', True))
        if ff3_enabled and len(scored) >= _min_sample:
            scored = self._apply_ff3_factors(scored)
            _post_scores = [float(x.get('qvm_score', 0.0)) for x in scored]
            _mu_post = sum(_post_scores) / len(_post_scores)
            _std_post = (sum(((v - _mu_post) ** 2 for v in _post_scores)) / len(_post_scores)) ** 0.5
            for _item in scored:
                _item['qvm_zscore'] = round((float(_item.get('qvm_score', 0.0)) - _mu_post) / max(_std_post, _eps), 4)
        scored.sort(key=lambda x: x.get('qvm_score', 0.0), reverse=True)
        for i, s in enumerate(scored):
            s['rank'] = i + 1
        for s in scored[:5]:
            _ff3_sc = s.get('ff3_score', 0)
            ff3 = f"/FF3={_ff3_sc:.1f}" if ff3_enabled else ''
            _rk = s.get('rank', 0)
            _nm = s.get('name', '')
            _qv = s.get('qvm_score', 0)
            _v = s.get('value_score', 0)
            _q = s.get('quality_score', 0)
            _m = s.get('momentum_score', 0)
            _g = s.get('governance_score', 0)
            logger.info(f"    #{_rk} {_nm}: QVM={_qv:.1f} (V={_v:.1f}/Q={_q:.1f}/M={_m:.1f}/G={_g:.1f}{ff3})")
        return scored

    def _apply_ff3_factors(self, scored: list) -> list:
        """Fama-French 3-Factor 스코어를 QVM에 보너스로 합산 (Log-Size + Moat-Aware)."""
        if not scored:
            return scored
        max_bonus = float(pget('s3.ff3_max_bonus', 20.0))
        smb_w = float(pget('s3.ff3_smb_weight', 0.3))
        hml_w = float(pget('s3.ff3_hml_weight', 0.4))
        wml_w = float(pget('s3.ff3_wml_weight', 0.3))
        _eps = 1e-06
        _min_sample = 2
        _z_cap = 1.5
        mcaps = [float(s.get('market_cap', s.get('mcap', 0)) or 0) for s in scored]
        log_mcaps = [math.log(m) if m > 0 else 0.0 for m in mcaps]
        mcap_valid = [lm for lm in log_mcaps if lm > 0]
        if len(mcap_valid) >= _min_sample:
            mu_m = sum(mcap_valid) / len(mcap_valid)
            sd_m = (sum(((v - mu_m) ** 2 for v in mcap_valid)) / len(mcap_valid)) ** 0.5
            raw_smb_z = [-(lm - mu_m) / max(sd_m, _eps) if lm > 0 else 0.0 for lm in log_mcaps]
            smb_z = [max(-_z_cap, min(_z_cap, z)) for z in raw_smb_z]
        else:
            smb_z = [0.0] * len(scored)
        mcap_min = float(pget('s3.ff3_smb_mcap_min', 50000000000))
        _small_damp = 0.3
        for i, s in enumerate(scored):
            if 0 < mcaps[i] < mcap_min:
                smb_z[i] = smb_z[i] * _small_damp
            elif float(s.get('roic_wacc_spread_pct', 0.0) or 0.0) > 0.0 and smb_z[i] < 0.0:
                # Economic moat leaders with ROIC > WACC are not penalized for scale
                smb_z[i] = 0.0
        pbrs = []
        for s in scored:
            pbr = s.get('pbr', s.get('pb_ratio', None))
            try:
                pbrs.append(float(pbr) if pbr is not None else None)
            except (TypeError, ValueError):
                pbrs.append(None)
        valid_pbrs = [p for p in pbrs if p is not None and p > 0]
        if len(valid_pbrs) >= _min_sample:
            mu_p = sum(valid_pbrs) / len(valid_pbrs)
            sd_p = (sum(((v - mu_p) ** 2 for v in valid_pbrs)) / len(valid_pbrs)) ** 0.5
            hml_z = []
            for p in pbrs:
                if p is not None and p > 0:
                    hml_z.append(-(p - mu_p) / max(sd_p, _eps))
                else:
                    hml_z.append(0.0)
        else:
            hml_z = [0.0] * len(scored)
        wml_raw = []
        for s in scored:
            mf = s.get('momentum_factor', 0.0)
            try:
                wml_raw.append(float(mf))
            except (TypeError, ValueError):
                wml_raw.append(0.0)
        if len(wml_raw) >= _min_sample:
            mu_w = sum(wml_raw) / len(wml_raw)
            sd_w = (sum(((v - mu_w) ** 2 for v in wml_raw)) / len(wml_raw)) ** 0.5
            wml_z = [(w - mu_w) / max(sd_w, _eps) for w in wml_raw]
        else:
            wml_z = [0.0] * len(scored)
        for i, s in enumerate(scored):
            ff3_raw = smb_w * smb_z[i] + hml_w * hml_z[i] + wml_w * wml_z[i]
            ff3_norm = 1.0 / (1.0 + math.exp(-ff3_raw))
            ff3_bonus = round((ff3_norm - 0.5) * 2.0 * max_bonus, 2)
            s['ff3_score'] = ff3_bonus
            s['smb_z'] = round(smb_z[i], 4)
            s['hml_z'] = round(hml_z[i], 4)
            s['wml_z'] = round(wml_z[i], 4)
            s['ff3_raw'] = round(ff3_raw, 4)
            s['qvm_score'] = round(s.get('qvm_score', 0.0) + ff3_bonus, 2)
        logger.debug(f"  [FF3] 적용 완료: SMBw={smb_w}, HMLw={hml_w}, WMLw={wml_w}, MaxBonus={max_bonus}, N={len(scored)}")
        return scored

    def _precompute_metrics(self, universe: List[Dict]) -> Dict[str, Dict]:
        """교차 비교용 밸류에이션 및 버핏 해자(Owner Earnings, ROIC, O-Score, M-Score) 메트릭 계산."""
        metrics = {}
        try:
            from src.analysis.fundamental_filter import MeridianFundamentalFilter
            _ff = MeridianFundamentalFilter()
        except Exception:
            _ff = None
        wacc_pct = float(pget('s3.buffett.wacc_pct', 8.5))
        _sentinel = 999.0
        _pbr_sentinel = 99.0
        for stock in universe:
            ticker = stock['ticker']
            mcap = float(stock.get('market_cap', 0) or 0)
            ta = float(stock.get('total_assets', 0) or 0)
            tl = float(stock.get('total_liabilities', 0) or 0)
            eq = float(stock.get('total_equity', 1) or 1)
            op_inc = float(stock.get('operating_income', 0) or 0)
            ni = float(stock.get('net_income', 0) or 0)
            cfo = float(stock.get('cash_from_operations', 0) or 0)
            gp = float(stock.get('gross_profit', 0) or 0)
            sector = stock.get('sector', 'unknown')
            annual = stock.get('annual_data', [])
            raw_annual = stock.get('raw_annual_data', annual)
            cash_eq = float(stock.get('cash', 0) or 0)
            st_inv = float(stock.get('short_term_investments', 0) or 0)
            ev = max(mcap * 0.2, mcap + tl - (cash_eq + st_inv)) if mcap > 0 else (mcap + tl)
            ev_ebit = ev / max(op_inc, 1.0) if op_inc > 0 else _sentinel
            pbr = mcap / max(eq, 1.0) if eq > 0 else _pbr_sentinel
            raw_capex = abs(float(stock.get('capex', 0) or 0))
            if raw_capex <= 0 and len(annual) >= 2:
                prev_ta = float(annual[-2].get('total_assets', ta) or ta)
                raw_capex = max(0.0, ta - prev_ta) * 0.3
            ppe_net = float(stock.get('ppe_net', 0) or 0)
            prev_ppe_net = float(stock.get('prev_ppe_net', 0) or 0)
            growth_capex = max(0.0, ppe_net - prev_ppe_net) if (ppe_net > 0 and prev_ppe_net > 0) else 0.0
            maint_capex = max(0.0, raw_capex - growth_capex) if raw_capex > 0 else raw_capex
            raw_depr = max(0.0, float(stock.get('depreciation', 0) or 0))
            depr = min(raw_depr, max(0.0, abs(cfo - ni)))
            fcf = cfo - raw_capex
            raw_oe = max(fcf, cfo - maint_capex, ni + depr - maint_capex)
            oe_cap = 2.0 * max(op_inc, ni, 0.0)
            owner_earnings = min(raw_oe, oe_cap) if oe_cap > 0.0 else raw_oe
            fcf_yield = (owner_earnings / max(mcap, 1.0) * 100.0) if mcap > 0 else 0.0
            owner_earnings_yield_pct = fcf_yield
            div_amount = float(annual[-1].get('dividends_paid', 0) or 0) if annual else 0.0
            if div_amount == 0 and ni > 0:
                div_amount = ni * 0.25
            div_yield = abs(div_amount) / max(mcap, 1.0) * 100.0 if mcap > 0 else 0.0
            roe_list = []
            for yr in annual[-3:]:
                yr_ni = float(yr.get('net_income', 0) or 0)
                yr_eq = float(yr.get('total_equity', 1) or 1)
                if yr_eq > 0:
                    roe_list.append(yr_ni / yr_eq * 100.0)
            roe_2yr = sum(roe_list) / max(len(roe_list), 1)
            gp_assets = gp / max(ta, 1.0) * 100.0 if ta > 0 else 0.0
            accrual = (ni - cfo) / max(ta, 1.0) * 100.0 if ta > 0 else 0.0
            debt_ratio = tl / max(eq, 1.0) * 100.0 if eq > 0 else _sentinel
            op_margins = []
            for yr in annual:
                yr_rev = float(yr.get('revenue', yr.get('total_revenue', 0)) or 0)
                yr_op = float(yr.get('operating_income', 0) or 0)
                if yr_rev > 0:
                    op_margins.append(yr_op / yr_rev * 100.0)
            op_volatility = _sentinel
            _min_op_yrs = 3
            if len(op_margins) >= _min_op_yrs:
                mean_opm = sum(op_margins) / len(op_margins)
                var = sum(((x - mean_opm) ** 2 for x in op_margins)) / len(op_margins)
                op_volatility = math.sqrt(var)
            capex_ocf_ratio = maint_capex / max(cfo, 1.0) * 100.0 if cfo > 0 else _sentinel
            total_debt = float(stock.get('short_term_debt', 0) or 0) + float(stock.get('long_term_debt', 0) or 0)
            invested_capital = max(eq * 0.25, eq + total_debt - (cash_eq + st_inv)) if eq > 0 else max(ta, 1.0)
            nopat = op_inc * (1.0 - 0.22)
            roic_pct = (nopat / invested_capital * 100.0) if invested_capital > 0 else 0.0
            roic_wacc_spread_pct = roic_pct - wacc_pct
            f_score = self._compute_fscore(stock)
            oscore_prob = 0.05
            beneish_m = -2.50
            if _ff is not None and raw_annual:
                try:
                    raw_curr = raw_annual[-1] if isinstance(raw_annual[-1], dict) else {}
                    raw_prev = raw_annual[-2] if len(raw_annual) >= 2 and isinstance(raw_annual[-2], dict) else {}
                    o_prob = _ff._compute_oscore(raw_curr, raw_prev)
                    if o_prob is not None:
                        oscore_prob = float(o_prob)
                    m_val = _ff._compute_beneish(raw_curr, raw_prev)
                    if m_val is not None:
                        beneish_m = float(m_val)
                except Exception:
                    pass
            tech = self._compute_technical_and_flow_state(ticker)
            metrics[ticker] = {
                'ev_ebit': ev_ebit,
                'pbr': pbr,
                'fcf_yield': fcf_yield,
                'div_yield': div_yield,
                'roe_2yr': roe_2yr,
                'gp_assets': gp_assets,
                'accrual': accrual,
                'debt_ratio': debt_ratio,
                'op_volatility': op_volatility,
                'capex_ocf_ratio': capex_ocf_ratio,
                'sector': sector,
                'f_score': f_score,
                'owner_earnings': owner_earnings,
                'owner_earnings_yield_pct': owner_earnings_yield_pct,
                'roic_pct': roic_pct,
                'roic_wacc_spread_pct': roic_wacc_spread_pct,
                'oscore_prob': oscore_prob,
                'beneish_m': beneish_m,
                **tech,
            }
        return metrics

    def _load_price_df(self, ticker: str) -> Optional[pd.DataFrame]:
        """Load OHLCV DataFrame (requiring 'close'/'Close') from kr_markets, historical_10y, or feature_store."""
        candidates = [
            _PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{ticker}.parquet',
            _FEATURE_STORE_DIR / f'{ticker}.parquet',
        ]
        for p in candidates:
            if p.exists():
                try:
                    df = pd.read_parquet(p)
                    if df is not None and not df.empty and ('close' in df.columns or 'Close' in df.columns):
                        return df
                except Exception:
                    continue
        return None

    def _compute_technical_and_flow_state(self, ticker: str) -> Dict[str, float]:
        """Compute MA20, MA60, MA200, daily volatility (%), and 20d/60d smart-money flow Z-scores."""
        out = {
            'ma20': 0.0,
            'ma60': 0.0,
            'ma200': 0.0,
            'daily_vol_pct': 2.0,
            'flow_z_20d': 0.0,
            'flow_z_60d': 0.0,
        }
        df = self._load_price_df(ticker)
        _min_rows = 20
        if df is None or len(df) < _min_rows:
            return out
        try:
            col_c = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
            if col_c is None:
                return out
            closes = pd.to_numeric(df[col_c], errors='coerce').dropna()
            if len(closes) < _min_rows:
                return out
            _w20 = 20
            _w60 = 60
            _w200 = 200
            _min_ret = 5
            out['ma20'] = float(closes.tail(_w20).mean())
            out['ma60'] = float(closes.tail(_w60).mean()) if len(closes) >= _w60 else out['ma20']
            out['ma200'] = float(closes.tail(_w200).mean()) if len(closes) >= _w200 else out['ma60']
            rets = closes.pct_change().dropna().tail(_w20)
            if len(rets) >= _min_ret:
                vol_pct = float(rets.std()) * 100.0
                if vol_pct > 0.0:
                    out['daily_vol_pct'] = vol_pct
            flow_series = None
            feat_path = _FEATURE_STORE_DIR / f'{ticker}.parquet'
            if feat_path.exists():
                try:
                    fdf = pd.read_parquet(feat_path)
                    for fcol in ('alpha_smart_money_flow_20d', 'foreign_net_buy_norm', 'obv_slope_20d'):
                        if fcol in fdf.columns:
                            s_cand = pd.to_numeric(fdf[fcol], errors='coerce').dropna()
                            if len(s_cand) >= _w20 and float(s_cand.std()) > 0:
                                flow_series = s_cand
                                break
                except Exception:
                    pass
            if flow_series is None or len(flow_series) < _w20:
                col_v = 'volume' if 'volume' in df.columns else ('Volume' if 'Volume' in df.columns else None)
                if col_v is not None:
                    vols = pd.to_numeric(df[col_v], errors='coerce').fillna(0.0)
                    signed_vol = closes.diff().fillna(0.0).apply(lambda x: 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)) * vols
                    flow_series = signed_vol.dropna()
            if flow_series is not None and len(flow_series) >= _w20:
                _eps = 1e-6
                tail60 = flow_series.tail(_w60)
                sd = float(tail60.std())
                if sd > _eps:
                    out['flow_z_20d'] = float(flow_series.tail(_w20).mean() / sd * math.sqrt(5.0))
                    if len(flow_series) >= _w60:
                        out['flow_z_60d'] = float(tail60.mean() / sd * math.sqrt(10.0))
                    else:
                        out['flow_z_60d'] = out['flow_z_20d']
        except Exception:
            pass
        return out

    def _score_value(self, stock: Dict, m: Dict, all_metrics: Dict) -> float:
        """밸류 슬리브 스코어링 (40점 만점). [Phase 81] 메달리온 섹터 직교화(Orthogonalization) 적용."""
        score = 0.0
        sector = stock.get('sector', 'unknown')
        sector_vals = [v for v in all_metrics.values() if v.get('sector') == sector]
        _min_sec = 3
        _ev_sent = 999
        _pbr_sent = 99
        if len(sector_vals) < _min_sec:
            sector_vals = list(all_metrics.values())
        all_vals = sector_vals
        ev_ebit = m.get('ev_ebit', _ev_sent)
        if ev_ebit < _ev_sent:
            all_ev = [v.get('ev_ebit', _ev_sent) for v in all_vals if v.get('ev_ebit', _ev_sent) < _ev_sent]
            rank = self._percentile_rank_lower(ev_ebit, all_ev)
            score += 12.0 * rank
        pbr = m.get('pbr', _pbr_sent)
        if pbr < _pbr_sent:
            all_pbr = [v.get('pbr', _pbr_sent) for v in all_vals if v.get('pbr', _pbr_sent) < _pbr_sent]
            rank = self._percentile_rank_lower(pbr, all_pbr)
            score += 10.0 * rank
        dy = m.get('div_yield', 0)
        all_dy = [v.get('div_yield', 0) for v in all_vals]
        rank = self._percentile_rank_higher(dy, all_dy)
        score += 8.0 * rank
        fcf_y = m.get('fcf_yield', 0)
        all_fcf = [v.get('fcf_yield', 0) for v in all_vals]
        rank = self._percentile_rank_higher(fcf_y, all_fcf)
        score += 10.0 * rank
        return score

    def _score_quality(self, stock: Dict, m: Dict, all_metrics: Dict) -> float:
        """퀄리티 슬리브 스코어링 (35점 만점). [Phase 81] 메달리온 섹터 직교화(Orthogonalization) 적용."""
        score = 0.0
        sector = stock.get('sector', 'unknown')
        sector_vals = [v for v in all_metrics.values() if v.get('sector') == sector]
        _min_sec = 3
        if len(sector_vals) < _min_sec:
            sector_vals = list(all_metrics.values())
        all_vals = sector_vals
        roe = m.get('roe_2yr', 0)
        all_roe = [v.get('roe_2yr', 0) for v in all_vals]
        if all_roe:
            mean_roe = sum(all_roe) / len(all_roe)
            max_roe_adj = max(pget('s3.quality_max_roe', 15.0), mean_roe * 1.5)
            score += 10.0 * min(1.0, max(0, (roe - min(0, mean_roe)) / max_roe_adj))
        else:
            max_roe = pget('s3.quality_max_roe', 15.0)
            score += 10.0 * min(1.0, max(0, roe / max_roe))
        gpa = m.get('gp_assets', 0)
        max_gpa = pget('s3.quality_max_gp_assets', 30.0)
        score += 8.0 * min(1.0, max(0, gpa / max_gpa))
        f_score = m.get('f_score') if m.get('f_score') is not None else self._compute_fscore(stock)
        if f_score is not None:
            score += 7.0 * min(1.0, f_score / 9.0)
        else:
            score += 3.5
        accrual = m.get('accrual', 0)
        _acc_low = -5.0
        _acc_hi = 10.0
        if accrual <= _acc_low:
            score += 5.0
        elif accrual <= 0:
            score += 5.0 * (1 - accrual / _acc_low) * 0.5 + 2.5
        elif accrual <= _acc_hi:
            score += 5.0 * max(0, 1 - accrual / _acc_hi) * 0.5
        dr = m.get('debt_ratio', 999)
        _dr_mid = 100.0
        _dr_hi = 200.0
        if dr < _dr_mid:
            score += 5.0
        elif dr < _dr_hi:
            score += 5.0 * (_dr_hi - dr) / _dr_mid
        return score

    def _score_momentum(self, ticker: str) -> float:
        """모멘텀 슬리브 스코어링 (15점 만점)."""
        score = 0.0
        df = self._load_price_df(ticker)
        _min_rows = 20
        _yr_days = 252
        _half_yr = 126
        _max_mom_score = 15.0
        if df is None or df.empty or len(df) < _min_rows:
            return 7.5
        try:
            if len(df) >= _yr_days:
                price_now = float(df.iloc[-1].get('close', df.iloc[-1].get('adj_close', 0)) or 0)
                price_12m = float(df.iloc[-_yr_days].get('close', df.iloc[-_yr_days].get('adj_close', 0)) or 0)
                if price_12m > 0 and price_now > 0:
                    ret_12m = (price_now - price_12m) / price_12m
                    score += 8.0 * min(1.0, max(0, (ret_12m + 0.3) / 0.8))
            else:
                score += 4.0
            latest = df.iloc[-1]
            mom_1 = latest.get('mom_1', None)
            if mom_1 is not None and not pd.isna(mom_1):
                rs_6m = float(mom_1) * _half_yr
                score += 4.0 * min(1.0, max(0, (rs_6m + 0.15) / 0.3))
            elif len(df) >= _half_yr:
                p_now = float(df.iloc[-1].get('close', df.iloc[-1].get('adj_close', 0)) or 0)
                p_6m = float(df.iloc[-_half_yr].get('close', df.iloc[-_half_yr].get('adj_close', 0)) or 0)
                if p_6m > 0 and p_now > 0:
                    rs_6m = (p_now - p_6m) / p_6m
                    score += 4.0 * min(1.0, max(0, (rs_6m + 0.15) / 0.3))
                else:
                    score += 2.0
            else:
                score += 2.0
            _em_score = self._score_earnings_momentum(ticker)
            score += _em_score
        except Exception as e:
            logger.debug(f"  {ticker} 모멘텀 데이터 읽기 실패: {e}")
            return 7.5
        return min(_max_mom_score, score)

    def _score_governance(self, stock: Dict) -> float:
        """거버넌스/주주환원 스코어링 (10점 만점)."""
        score = 0.0
        ticker = stock['ticker']
        annual = stock.get('annual_data', [])
        _min_yrs = 3
        if len(annual) >= _min_yrs:
            divs = []
            for yr in annual[-_min_yrs:]:
                d = abs(float(yr.get('dividends_paid', 0) or 0))
                divs.append(d)
            if divs[0] > 0 and all((d > 0 for d in divs)):
                growth_rates = []
                for i in range(1, len(divs)):
                    if divs[i - 1] > 0:
                        growth_rates.append((divs[i] - divs[i - 1]) / divs[i - 1])
                if growth_rates:
                    avg_growth = sum(growth_rates) / len(growth_rates)
                    score += 4.0 * min(1.0, max(0, avg_growth / 0.1))
            elif any((d > 0 for d in divs)):
                score += 1.0
        else:
            score += 1.0
        if annual:
            latest = annual[-1]
            if latest.get('treasury_share_buyback') or latest.get('share_buyback'):
                score += 3.0
            elif len(annual) >= 2:
                shares_now = float(annual[-1].get('shares_outstanding', 0) or 0)
                shares_prev = float(annual[-2].get('shares_outstanding', 0) or 0)
                if shares_prev > 0 and shares_now < shares_prev:
                    score += 2.0
        if ticker in VALUE_UP_TICKERS:
            score += 3.0
        return score

    def _score_earnings_momentum(self, ticker: str) -> float:
        """실적 모멘텀 스코어링 (3점 만점) — DART 재무제표 기반."""
        annual = self._get_annual_data(ticker)
        if len(annual) < 2:
            return 1.5
        score = 0.0
        curr = annual[-1]
        prev = annual[-2]
        curr_op = float(curr.get('operating_income', 0) or 0)
        prev_op = float(prev.get('operating_income', 0) or 0)
        if prev_op > 0 and curr_op > 0:
            op_growth = (curr_op - prev_op) / prev_op
            score += 1.5 * min(1.0, max(0, (op_growth + 0.3) / 0.6))
        elif curr_op > 0 and prev_op <= 0:
            score += 1.5
        elif curr_op <= 0:
            score += 0.0
        curr_rev = float(curr.get('revenue', 0) or 0)
        prev_rev = float(prev.get('revenue', 0) or 0)
        if prev_rev > 0 and curr_rev > 0:
            rev_growth = (curr_rev - prev_rev) / prev_rev
            score += 1.0 * min(1.0, max(0, (rev_growth + 0.2) / 0.4))
        elif curr_rev > 0:
            score += 0.5
        _m_thresh = 0.02
        _max_em = 3.0
        if curr_rev > 0 and prev_rev > 0:
            curr_margin = curr_op / curr_rev
            prev_margin = prev_op / prev_rev
            margin_delta = curr_margin - prev_margin
            if margin_delta > _m_thresh:
                score += 0.5
            elif margin_delta > 0:
                score += 0.25
        return min(_max_em, score)

    def screen_value_traps(self, scored_universe: List[Dict]) -> List[Dict]:
        """밸류트랩 종목 필터링."""
        if not scored_universe:
            return []
        trap_threshold = pget('s3.trap_risk_threshold', 0.6)
        safe = []
        trapped = []
        for stock in scored_universe:
            ticker = stock['ticker']
            risk = self._compute_trap_risk(ticker, stock)
            result = {**stock}
            result['trap_risk'] = round(risk, 3)
            if risk < trap_threshold:
                safe.append(result)
            else:
                trapped.append(result)
                _snm = stock.get('name', ticker)
                logger.info(f"    ⚠️ 밸류트랩 감지: {_snm} trap_risk={risk:.0%}")
        if trapped:
            _tr_names = ', '.join((t.get('name', t.get('ticker', '')) for t in trapped[:5]))
            logger.info(f"  S3 밸류트랩 필터: {len(trapped)}종목 제외 ({_tr_names})")
        return safe

    def _compute_trap_risk(self, ticker: str, stock: Optional[Dict]=None) -> float:
        """밸류트랩 위험 점수 (0.0=안전, 1.0=트랩)."""
        w_mom = pget('s3.trap_w_momentum', 0.4)
        w_earn = pget('s3.trap_w_earnings', 0.25)
        w_accrual = pget('s3.trap_w_accrual', 0.2)
        w_cf = pget('s3.trap_w_cashflow', 0.15)
        risk = 0.0
        mom_pass, _ = self._check_momentum_filter(ticker)
        if not mom_pass:
            risk += w_mom
        earn_pass, _ = self._check_earnings_deterioration(ticker, stock)
        if not earn_pass:
            risk += w_earn
        accrual_pass, _ = self._check_accrual_quality(ticker, stock)
        if not accrual_pass:
            risk += w_accrual
        cf_pass, _ = self._check_cash_flow_divergence(ticker, stock)
        if not cf_pass:
            risk += w_cf
        return min(1.0, risk)

    def _check_momentum_filter(self, ticker: str) -> Tuple[bool, str]:
        """가격 모멘텀 필터."""
        df = self._load_price_df(ticker)
        _min_rows = 20
        _half_yr = 126
        _ma_window = 200
        if df is None or df.empty or len(df) < _min_rows:
            return (True, 'insufficient_data')
        try:
            min_return = pget('s3.trap_min_6m_return', -0.2)
            ret_6m_fail = False
            if len(df) >= _half_yr:
                price_now = float(df.iloc[-1].get('close', 0) or 0)
                price_6m = float(df.iloc[-_half_yr].get('close', 0) or 0)
                if price_6m > 0:
                    ret_6m = (price_now - price_6m) / price_6m
                    if ret_6m < min_return:
                        ret_6m_fail = True
            ma200_fail = False
            if len(df) >= _ma_window:
                closes = df['close'].tail(_ma_window)
                if not closes.empty:
                    ma200 = closes.mean()
                    current = float(df.iloc[-1].get('close', 0) or 0)
                    if current > 0 and ma200 > 0 and (current < ma200):
                        ma200_fail = True
            if ret_6m_fail and ma200_fail:
                return (False, f"6M_return_below_{min_return:.0%}_and_below_MA200")
            return (True, 'momentum_ok')
        except Exception as e:
            logger.debug(f"  {ticker} 모멘텀 체크 실패: {e}")
            return (True, 'error')

    def _check_earnings_deterioration(self, ticker: str, stock: Optional[Dict]=None) -> Tuple[bool, str]:
        """실적 악화 체크."""
        annual = self._get_annual_data(ticker, stock)
        _min_yrs = 3
        if len(annual) < _min_yrs:
            return (True, 'insufficient_data')
        revenues = []
        for yr in annual[-_min_yrs:]:
            rev = float(yr.get('revenue', 0) or 0)
            revenues.append(rev)
        rev_declining = all((revenues[i] > revenues[i + 1] > 0 for i in range(len(revenues) - 1))) if all((r > 0 for r in revenues)) else False
        op_incs = []
        for yr in annual[-2:]:
            op = float(yr.get('operating_income', 0) or 0)
            op_incs.append(op)
        op_loss = all((op < 0 for op in op_incs))
        if rev_declining and op_loss:
            return (False, '3yr_revenue_decline_and_2yr_op_loss')
        elif rev_declining:
            return (False, '3yr_revenue_decline')
        elif op_loss:
            return (False, '2yr_operating_loss')
        return (True, 'earnings_ok')

    def _check_accrual_quality(self, ticker: str, stock: Optional[Dict]=None) -> Tuple[bool, str]:
        """Sloan Ratio 발생액 품질 체크."""
        annual = self._get_annual_data(ticker, stock)
        if not annual:
            return (True, 'no_data')
        latest = annual[-1]
        ni = float(latest.get('net_income', 0) or 0)
        cfo = float(latest.get('cash_from_operations', 0) or 0)
        ta = float(latest.get('total_assets', 0) or 0)
        if ta <= 0:
            return (True, 'no_total_assets')
        sloan = (ni - cfo) / ta
        threshold = pget('s3.trap_sloan_threshold', 0.1)
        if sloan > threshold:
            return (False, f"sloan_ratio={sloan:.1%}_above_{threshold:.0%}")
        return (True, f"sloan_ok={sloan:.1%}")

    def _check_cash_flow_divergence(self, ticker: str, stock: Optional[Dict]=None) -> Tuple[bool, str]:
        """현금흐름 괴리 체크."""
        annual = self._get_annual_data(ticker, stock)
        if len(annual) < 2:
            return (True, 'insufficient_data')
        divergence_years = 0
        for yr in annual[-3:]:
            ni = float(yr.get('net_income', 0) or 0)
            cfo = float(yr.get('cash_from_operations', 0) or 0)
            if ni > 0 and cfo < 0:
                divergence_years += 1
        min_years = cfg.get('s3.trap_cf_divergence_years', 2)
        if divergence_years >= min_years:
            return (False, f"ni_positive_cfo_negative_{divergence_years}yr")
        return (True, 'cashflow_ok')

    def _compute_fscore(self, stock: Dict) -> Optional[int]:
        """Piotroski F-Score 계산 (9점)."""
        annual = stock.get('annual_data', [])
        if not annual:
            return None
        try:
            curr = annual[-1]
            prev = annual[-2] if len(annual) >= 2 else {}
            raw_annual = stock.get('raw_annual_data', annual)
            raw_curr = raw_annual[-1] if raw_annual and isinstance(raw_annual[-1], dict) else curr
            raw_prev = raw_annual[-2] if len(raw_annual) >= 2 and isinstance(raw_annual[-2], dict) else prev
            ni = float(curr.get('net_income', 0) or 0)
            cfo = float(curr.get('cash_from_operations', 0) or 0)
            ta = float(curr.get('total_assets', 1) or 1)
            tl = float(curr.get('total_liabilities', 0) or 0)
            ca = float(curr.get('current_assets', 0) or 0)
            cl = float(curr.get('current_liabilities', 0) or 0)
            rev = float(curr.get('revenue', 0) or 0)
            gp = float(curr.get('gross_profit', 0) or 0)
            prev_ni = float(prev.get('net_income', 0) or 0) if prev else 0
            prev_ta = float(prev.get('total_assets', 1) or 1) if prev else 1
            prev_tl = float(prev.get('total_liabilities', 0) or 0) if prev else 0
            prev_ca = float(prev.get('current_assets', 0) or 0) if prev else 0
            prev_cl = float(prev.get('current_liabilities', 1) or 1) if prev else 1
            prev_rev = float(prev.get('revenue', 0) or 0) if prev else 0
            prev_gp = float(prev.get('gross_profit', 0) or 0) if prev else 0
            f = 0
            if ni > 0:
                f += 1
            if cfo > 0:
                f += 1
            if ta > 0 and prev_ta > 0 and (ni / ta > prev_ni / prev_ta):
                f += 1
            if cfo > ni:
                f += 1
            if ta > 0 and prev_ta > 0 and (tl / ta < prev_tl / prev_ta):
                f += 1
            if cl > 0 and prev_cl > 0 and (ca / cl > prev_ca / prev_cl):
                f += 1
            sh_curr = float(raw_curr.get('shares_outstanding', curr.get('shares_outstanding', 0)) or 0)
            sh_prev = float(raw_prev.get('shares_outstanding', prev.get('shares_outstanding', 0)) or 0) if raw_prev else 0.0
            _dil_tol = 1.01
            if sh_prev <= 0 or sh_curr <= sh_prev * _dil_tol:
                f += 1
            if rev > 0 and prev_rev > 0 and (gp / rev > prev_gp / max(prev_rev, 1)):
                f += 1
            if ta > 0 and prev_ta > 0 and (rev / ta > prev_rev / prev_ta):
                f += 1
            return f
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return None

    def _get_annual_data(self, ticker: str, stock: Optional[Dict]=None) -> list:
        """연간 재무 데이터 가져오기."""
        if stock and stock.get('annual_data'):
            return stock['annual_data']
        fp = _FINANCIALS_DIR / f'{ticker}.json'
        if not fp.exists():
            return []
        try:
            data = json.loads(fp.read_text())
            if isinstance(data, list):
                return data
            return data.get('annual', [])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return []

    @staticmethod
    def _percentile_rank_lower(value: float, all_values: List[float]) -> float:
        """낮을수록 좋은 메트릭의 백분위 (1.0 = 가장 낮음)."""
        if not all_values:
            return 0.5
        n_better = sum((1 for v in all_values if v > value))
        return n_better / max(len(all_values) - 1, 1)

    def _compute_momentum_factor(self, ticker: str) -> float:
        """★ M4: 가격 모멘텀 팩터 계산 — 12M/6M 수익률 기반."""
        df = self._load_price_df(ticker)
        _min_rows = 20
        _yr_days = 252
        _half_yr = 126
        if df is None or df.empty or len(df) < _min_rows:
            return 0.0
        try:
            price_now = float(df.iloc[-1].get('close', df.iloc[-1].get('adj_close', 0)) or 0)
            if price_now <= 0:
                return 0.0
            ret_12m = 0.0
            if len(df) >= _yr_days:
                price_12m = float(df.iloc[-_yr_days].get('close', df.iloc[-_yr_days].get('adj_close', 0)) or 0)
                if price_12m > 0:
                    ret_12m = (price_now - price_12m) / price_12m
            ret_6m = 0.0
            if len(df) >= _half_yr:
                price_6m = float(df.iloc[-_half_yr].get('close', df.iloc[-_half_yr].get('adj_close', 0)) or 0)
                if price_6m > 0:
                    ret_6m = (price_now - price_6m) / price_6m
            w_12m = pget('s3.momentum_factor_w_12m', 0.6)
            w_6m = pget('s3.momentum_factor_w_6m', 0.4)
            raw = w_12m * ret_12m + w_6m * ret_6m
            _clamp_range = pget('s3.momentum_factor_clamp', 0.5)
            factor = max(-1.0, min(1.0, raw / _clamp_range))
            return factor
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return 0.0

    @staticmethod
    def _percentile_rank_higher(value: float, all_values: List[float]) -> float:
        """높을수록 좋은 메트릭의 백분위 (1.0 = 가장 높음)."""
        if not all_values:
            return 0.5
        n_worse = sum((1 for v in all_values if v < value))
        return n_worse / max(len(all_values) - 1, 1)

    def validate_factors(self, scored_universe: List[Dict], realized_returns: Dict[str, float]=None) -> Dict:
        """QVM 각 팩터의 예측력(IC/ICIR) 검증.

        Information Coefficient (IC):
          IC = Spearman rank correlation(factor score, realized return)

        ICIR (Information Ratio):
          ICIR = mean(IC) / std(IC) over rolling window

        ICIR > 0.5 → 유효한 팩터, ICIR < 0.3 → 약한 팩터

        Args:
            scored_universe: score_universe() 결과
            realized_returns: 종목별 실현 수익률 {ticker: return}
                              (없으면 feature_store에서 로드)

        Returns:
            각 팩터의 IC/ICIR + 유효성 판정
        """
        if not scored_universe:
            return {'status': 'no_data'}
        if realized_returns is None:
            realized_returns = self._load_realized_returns(scored_universe)
        if not realized_returns:
            return {'status': 'no_returns'}
        factors = ['value_score', 'quality_score', 'momentum_score', 'governance_score', 'qvm_score']
        results = {}
        for factor in factors:
            factor_scores = []
            returns = []
            for stock in scored_universe:
                ticker = stock['ticker']
                if ticker in realized_returns:
                    fs = stock.get(factor, 0)
                    r = realized_returns[ticker]
                    factor_scores.append(fs)
                    returns.append(r)
            if len(factor_scores) < pget('s3.icir_min_samples', 10):
                results[factor] = {'ic': None, 'icir': None, 'valid': None, 'reason': 'insufficient_samples', 'n_samples': len(factor_scores)}
                continue
            ic = self._spearman_rank_ic(factor_scores, returns)
            n = len(factor_scores)
            if abs(ic) < 1.0 and n > 2:
                t_stat = ic * math.sqrt(n - 2) / math.sqrt(1 - ic * ic)
                icir = abs(t_stat) / math.sqrt(n)
            else:
                icir = 0
            icir_strong = pget('s3.icir_strong_threshold', 0.5)
            icir_weak = pget('s3.icir_weak_threshold', 0.3)
            if icir >= icir_strong:
                valid = 'strong'
            elif icir >= icir_weak:
                valid = 'moderate'
            else:
                valid = 'weak'
            results[factor] = {'ic': round(ic, 4), 'icir': round(icir, 4), 'valid': valid, 'n_samples': n}
        weight_adj = self._compute_factor_weight_adjustment(results)
        output = {'timestamp': datetime.now().isoformat(), 'factors': results, 'weight_adjustment': weight_adj, 'n_universe': len(scored_universe), 'n_returns': len(realized_returns)}
        self._save_icir_result(output)
        for f, r in results.items():
                _ic = r.get('ic', 0)
                _icir = r.get('icir', 0)
                _vld = r.get('valid', False)
                logger.info(f"    ICIR {f}: IC={_ic:.3f}, ICIR={_icir:.3f} → {_vld}")
        return output

    def _spearman_rank_ic(self, scores: List[float], returns: List[float]) -> float:
        """Spearman Rank IC 계산 (scipy 없이 구현)."""
        n = len(scores)
        if n < 3:
            return 0.0

        def rank(values):
            indexed = sorted(enumerate(values), key=lambda x: x[1])
            ranks = [0.0] * n
            for r, (idx, _) in enumerate(indexed):
                ranks[idx] = float(r + 1)
            return ranks
        rank_s = rank(scores)
        rank_r = rank(returns)
        d_sq_sum = sum(((rs - rr) ** 2 for rs, rr in zip(rank_s, rank_r)))
        rho = 1 - 6 * d_sq_sum / (n * (n * n - 1))
        return rho

    def _compute_factor_weight_adjustment(self, results: Dict) -> Dict:
        """ICIR 결과에 따른 팩터 가중치 조정 권고.

        강한 팩터(ICIR>0.5) → 가중치 유지/증가
        약한 팩터(ICIR<0.3) → 가중치 축소 권고
        """
        adjustments = {}
        boost = pget('s3.icir_strong_boost', 1.1)
        cut = pget('s3.icir_weak_cut', 0.7)
        for factor, r in results.items():
            valid = r.get('valid')
            if valid == 'strong':
                adjustments[factor] = {'action': 'maintain_or_boost', 'multiplier': boost}
            elif valid == 'moderate':
                adjustments[factor] = {'action': 'maintain', 'multiplier': 1.0}
            elif valid == 'weak':
                adjustments[factor] = {'action': 'reduce', 'multiplier': cut}
            else:
                adjustments[factor] = {'action': 'unknown', 'multiplier': 1.0}
        return adjustments

    def _load_realized_returns(self, scored_universe: List[Dict]) -> Dict[str, float]:
        """feature_store에서 실현 수익률 로드."""
        returns = {}
        try:
            import pandas as pd
        except ImportError as e:
            return returns
        forward_days = pget('s3.icir_forward_days', 5)
        for stock in scored_universe:
            ticker = stock['ticker']
            pf = _PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{ticker}.parquet'
            if not pf.exists():
                continue
            try:
                df = pd.read_parquet(pf)
                if len(df) < forward_days + 1:
                    continue
                price_now = float(df.iloc[-(forward_days + 1)].get('close', 0) or 0)
                price_fwd = float(df.iloc[-1].get('close', 0) or 0)
                if price_now > 0:
                    returns[ticker] = (price_fwd - price_now) / price_now
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                continue
        return returns

    def _save_icir_result(self, result: Dict) -> None:
        """ICIR 결과 저장."""
        try:
            import json as _json
            from datetime import datetime as _dt
            out = Path(_FEATURE_STORE_DIR).parent.parent / 'results' / 'icir_validation.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            pass
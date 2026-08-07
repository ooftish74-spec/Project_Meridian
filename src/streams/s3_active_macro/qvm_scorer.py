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
        logger.info(f'  S3 QVM 스코어링: {len(universe)}종목')
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
            _mom_boost_weight = cfg.get('s3.momentum_boost_weight', 0.1)
            momentum_factor = 0.0
            if _mom_boost_enabled:
                momentum_factor = self._compute_momentum_factor(ticker)
                qvm_total = qvm_total * (1.0 + _mom_boost_weight * momentum_factor)
            base_kper = 10.0
            op_vol = m.get('op_volatility', 999.0)
            capex_ocf = m.get('capex_ocf_ratio', 999.0)
            vol_premium = max(0, (50.0 - op_vol) / 10.0) if op_vol < 999.0 else 0
            capex_premium = max(0, (50.0 - capex_ocf) / 10.0) if capex_ocf < 999.0 else 0
            k_per_multiple = base_kper + vol_premium + capex_premium
            roe_adj = m.get('roe_2yr', 0) / 100.0
            recent_op = float(stock.get('annual_data', [{}])[-1].get('operating_income', 0) or 0)
            estimated_future_op = recent_op * (1.0 + max(0, roe_adj))
            target_market_cap = estimated_future_op * k_per_multiple
            current_market_cap = float(stock.get('market_cap', 1) or 1)
            margin_of_safety = (target_market_cap / current_market_cap - 1) * 100 if current_market_cap > 0 and target_market_cap > 0 else 0.0
            result = {**stock}
            result.update({'qvm_score': round(qvm_total, 2), 'value_score': round(value_score, 2), 'quality_score': round(quality_score, 2), 'momentum_score': round(momentum_score, 2), 'governance_score': round(governance_score, 2), 'momentum_factor': round(momentum_factor, 4), 'k_per_multiple': round(k_per_multiple, 2), 'margin_of_safety_pct': round(margin_of_safety, 2)})
            scored.append(result)
        scored.sort(key=lambda x: x['qvm_score'], reverse=True)
        _scores_list = [float(x.get('qvm_score', 0.0)) for x in scored]
        if len(_scores_list) >= 2:
            _mu = sum(_scores_list) / len(_scores_list)
            _std = (sum(((v - _mu) ** 2 for v in _scores_list)) / len(_scores_list)) ** 0.5
            for _item in scored:
                _item['qvm_zscore'] = round((float(_item.get('qvm_score', 0.0)) - _mu) / max(_std, 1e-06), 4)
        else:
            for _item in scored:
                _item['qvm_zscore'] = 0.0
        ff3_enabled = bool(cfg.get('s3.ff3_enabled', True))
        if ff3_enabled and len(scored) >= 2:
            scored = self._apply_ff3_factors(scored)
        scored.sort(key=lambda x: x.get('qvm_score', 0.0), reverse=True)
        for i, s in enumerate(scored):
            s['rank'] = i + 1
        for s in scored[:5]:
            ff3 = f'/FF3={s.get('ff3_score', 0):.1f}' if ff3_enabled else ''
            logger.info(f'    #{s['rank']} {s['name']}: QVM={s['qvm_score']:.1f} (V={s['value_score']:.1f}/Q={s['quality_score']:.1f}/M={s['momentum_score']:.1f}/G={s['governance_score']:.1f}{ff3})')
        return scored

    def _apply_ff3_factors(self, scored: list) -> list:
        """Fama-French 3-Factor 스코어를 QVM에 보너스로 합산.

        Factor 구성:
          SMB (Small-Minus-Big):
            소형주가 대형주 대비 초과수익 → 시가총액 역순 z-score
            KR 시장 실증: 소형주 프리미엄 존재 (단, 유동성 필터 적용)

          HML (High-Minus-Low):
            고 PBR 역방향 = 저 PBR(장부가치 높음) 선호
            → EV/EBIT, PBR 역수 z-score (이미 Value Score와 연동)

          WML (Winners-Minus-Losers, 모멘텀):
            1~12개월 수익률 상위 종목 선호
            KR 시장: 단기(1-3개월)는 역모멘텀, 중기(6-12개월)는 모멘텀 우위

        모든 임계치·가중치: DynamicConfig `s3.ff3.*` 키
        최종 보너스: qvm_score += ff3_score (최대 max_bonus점)
        """
        if not scored:
            return scored
        max_bonus = float(cfg.get('s3.ff3_max_bonus', 20.0))
        smb_w = float(cfg.get('s3.ff3_smb_weight', 0.3))
        hml_w = float(cfg.get('s3.ff3_hml_weight', 0.4))
        wml_w = float(cfg.get('s3.ff3_wml_weight', 0.3))
        mcaps = [float(s.get('market_cap', s.get('mcap', 0)) or 0) for s in scored]
        mcap_valid = [m for m in mcaps if m > 0]
        if len(mcap_valid) >= 2:
            mu_m = sum(mcap_valid) / len(mcap_valid)
            sd_m = (sum(((v - mu_m) ** 2 for v in mcap_valid)) / len(mcap_valid)) ** 0.5
            smb_z = [-(m - mu_m) / max(sd_m, 1e-06) if m > 0 else 0.0 for m in mcaps]
        else:
            smb_z = [0.0] * len(scored)
        mcap_min = float(cfg.get('s3.ff3_smb_mcap_min', 50000000000))
        smb_z = [z if mcaps[i] >= mcap_min or mcaps[i] == 0 else z * 0.3 for i, z in enumerate(smb_z)]
        pbrs = []
        for s in scored:
            pbr = s.get('pbr', s.get('pb_ratio', None))
            try:
                pbrs.append(float(pbr) if pbr is not None else None)
            except (TypeError, ValueError):
                pbrs.append(None)
        valid_pbrs = [p for p in pbrs if p is not None and p > 0]
        if len(valid_pbrs) >= 2:
            mu_p = sum(valid_pbrs) / len(valid_pbrs)
            sd_p = (sum(((v - mu_p) ** 2 for v in valid_pbrs)) / len(valid_pbrs)) ** 0.5
            hml_z = []
            for p in pbrs:
                if p is not None and p > 0:
                    hml_z.append(-(p - mu_p) / max(sd_p, 1e-06))
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
        if len(wml_raw) >= 2:
            mu_w = sum(wml_raw) / len(wml_raw)
            sd_w = (sum(((v - mu_w) ** 2 for v in wml_raw)) / len(wml_raw)) ** 0.5
            wml_z = [(w - mu_w) / max(sd_w, 1e-06) for w in wml_raw]
        else:
            wml_z = [0.0] * len(scored)
        for i, s in enumerate(scored):
            ff3_raw = smb_w * smb_z[i] + hml_w * hml_z[i] + wml_w * wml_z[i]
            import math as _math
            ff3_norm = 1.0 / (1.0 + _math.exp(-ff3_raw))
            ff3_bonus = round((ff3_norm - 0.5) * 2 * max_bonus, 2)
            s['ff3_score'] = ff3_bonus
            s['smb_z'] = round(smb_z[i], 4)
            s['hml_z'] = round(hml_z[i], 4)
            s['wml_z'] = round(wml_z[i], 4)
            s['ff3_raw'] = round(ff3_raw, 4)
            s['qvm_score'] = round(s.get('qvm_score', 0.0) + ff3_bonus, 2)
        logger.debug(f'  [FF3] 적용 완료: SMBw={smb_w}, HMLw={hml_w}, WMLw={wml_w}, MaxBonus={max_bonus}, N={len(scored)}')
        return scored

    def _precompute_metrics(self, universe: List[Dict]) -> Dict[str, Dict]:
        """교차 비교용 밸류에이션 메트릭 계산."""
        metrics = {}
        for stock in universe:
            ticker = stock['ticker']
            mcap = stock.get('market_cap', 0)
            ta = stock.get('total_assets', 0)
            tl = stock.get('total_liabilities', 0)
            eq = stock.get('total_equity', 1)
            op_inc = stock.get('operating_income', 0)
            ni = stock.get('net_income', 0)
            cfo = stock.get('cash_from_operations', 0)
            revenue = stock.get('revenue', 0)
            gp = stock.get('gross_profit', 0)
            sector = stock.get('sector', 'unknown')
            ev = mcap + tl
            ev_ebit = ev / max(op_inc, 1) if op_inc > 0 else 999
            pbr = mcap / max(eq, 1) if eq > 0 else 99
            annual = stock.get('annual_data', [])
            capex = 0
            if len(annual) >= 2:
                prev_ta = float(annual[-2].get('total_assets', ta) or ta)
                capex = max(0, ta - prev_ta) * 0.3
            fcf = cfo - capex
            fcf_yield = fcf / max(mcap, 1) * 100 if mcap > 0 else 0
            div_amount = float(annual[-1].get('dividends_paid', 0) or 0) if annual else 0
            if div_amount == 0 and ni > 0:
                div_amount = ni * 0.3
            div_yield = abs(div_amount) / max(mcap, 1) * 100
            roe_list = []
            for yr in annual[-2:]:
                yr_ni = float(yr.get('net_income', 0) or 0)
                yr_eq = float(yr.get('total_equity', 1) or 1)
                if yr_eq > 0:
                    roe_list.append(yr_ni / yr_eq * 100)
            roe_2yr = sum(roe_list) / max(len(roe_list), 1)
            gp_assets = gp / max(ta, 1) * 100 if ta > 0 else 0
            accrual = (ni - cfo) / max(ta, 1) * 100 if ta > 0 else 0
            debt_ratio = tl / max(eq, 1) * 100 if eq > 0 else 999
            op_margins = []
            for yr in annual:
                yr_rev = float(yr.get('total_revenue', 0) or 0)
                yr_op = float(yr.get('operating_income', 0) or 0)
                if yr_rev > 0:
                    op_margins.append(yr_op / yr_rev * 100)
            op_volatility = 999.0
            if len(op_margins) >= 3:
                mean_opm = sum(op_margins) / len(op_margins)
                var = sum(((x - mean_opm) ** 2 for x in op_margins)) / len(op_margins)
                op_volatility = math.sqrt(var)
            capex_ocf_ratio = capex / max(cfo, 1) * 100 if cfo > 0 else 999.0
            metrics[ticker] = {'ev_ebit': ev_ebit, 'pbr': pbr, 'fcf_yield': fcf_yield, 'div_yield': div_yield, 'roe_2yr': roe_2yr, 'gp_assets': gp_assets, 'accrual': accrual, 'debt_ratio': debt_ratio, 'op_volatility': op_volatility, 'capex_ocf_ratio': capex_ocf_ratio, 'sector': sector}
        return metrics

    def _score_value(self, stock: Dict, m: Dict, all_metrics: Dict) -> float:
        """밸류 슬리브 스코어링 (40점 만점). [Phase 81] 메달리온 섹터 직교화(Orthogonalization) 적용."""
        score = 0.0
        sector = stock.get('sector', 'unknown')
        sector_vals = [v for v in all_metrics.values() if v.get('sector') == sector]
        if len(sector_vals) < 3:
            sector_vals = list(all_metrics.values())
        all_vals = sector_vals
        ev_ebit = m.get('ev_ebit', 999)
        if ev_ebit < 999:
            all_ev = [v.get('ev_ebit', 999) for v in all_vals if v.get('ev_ebit', 999) < 999]
            rank = self._percentile_rank_lower(ev_ebit, all_ev)
            score += 12.0 * rank
        pbr = m.get('pbr', 99)
        if pbr < 99:
            all_pbr = [v.get('pbr', 99) for v in all_vals if v.get('pbr', 99) < 99]
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
        if len(sector_vals) < 3:
            sector_vals = list(all_metrics.values())
        all_vals = sector_vals
        roe = m.get('roe_2yr', 0)
        all_roe = [v.get('roe_2yr', 0) for v in all_vals]
        if all_roe:
            mean_roe = sum(all_roe) / len(all_roe)
            max_roe_adj = max(cfg.get('s3.quality_max_roe', 15.0), mean_roe * 1.5)
            score += 10.0 * min(1.0, max(0, (roe - min(0, mean_roe)) / max_roe_adj))
        else:
            max_roe = cfg.get('s3.quality_max_roe', 15.0)
            score += 10.0 * min(1.0, max(0, roe / max_roe))
        gpa = m.get('gp_assets', 0)
        max_gpa = cfg.get('s3.quality_max_gp_assets', 30.0)
        score += 8.0 * min(1.0, max(0, gpa / max_gpa))
        f_score = self._compute_fscore(stock)
        if f_score is not None:
            score += 7.0 * min(1.0, f_score / 9.0)
        else:
            score += 3.5
        accrual = m.get('accrual', 0)
        if accrual <= -5:
            score += 5.0
        elif accrual <= 0:
            score += 5.0 * (1 - accrual / -5) * 0.5 + 2.5
        elif accrual <= 10:
            score += 5.0 * max(0, 1 - accrual / 10) * 0.5
        dr = m.get('debt_ratio', 999)
        if dr < 100:
            score += 5.0
        elif dr < 200:
            score += 5.0 * (200 - dr) / 100
        return score

    def _score_momentum(self, ticker: str) -> float:
        """모멘텀 슬리브 스코어링 (15점 만점)."""
        score = 0.0
        try:
            import pandas as pd
        except ImportError as e:
            return 7.5
        pf = _FEATURE_STORE_DIR / f'{ticker}.parquet'
        if not pf.exists():
            return 7.5
        try:
            df = pd.read_parquet(pf)
            if df.empty or len(df) < 20:
                return 7.5
            if len(df) >= 252:
                price_now = df.iloc[-1].get('close', df.iloc[-1].get('adj_close', 0))
                price_12m = df.iloc[-252].get('close', df.iloc[-252].get('adj_close', 0))
                if price_12m > 0 and price_now > 0:
                    ret_12m = (price_now - price_12m) / price_12m
                    score += 8.0 * min(1.0, max(0, (ret_12m + 0.3) / 0.8))
            else:
                score += 4.0
            latest = df.iloc[-1]
            mom_1 = latest.get('mom_1', 0)
            if not pd.isna(mom_1):
                rs_6m = mom_1 * 126
                score += 4.0 * min(1.0, max(0, (rs_6m + 0.15) / 0.3))
            else:
                score += 2.0
            _em_score = self._score_earnings_momentum(ticker)
            score += _em_score
        except Exception as e:
            logger.debug(f'  {ticker} 모멘텀 데이터 읽기 실패: {e}')
            return 7.5
        return min(15.0, score)

    def _score_governance(self, stock: Dict) -> float:
        """거버넌스/주주환원 스코어링 (10점 만점)."""
        score = 0.0
        ticker = stock['ticker']
        annual = stock.get('annual_data', [])
        if len(annual) >= 3:
            divs = []
            for yr in annual[-3:]:
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
        """실적 모멘텀 스코어링 (3점 만점) — DART 재무제표 기반.

        산출 방식:
          1. 영업이익 YoY 변화율 (1.5점)
          2. 매출 QoQ/YoY 성장률 (1.0점)
          3. 영업이익률 개선 (0.5점)
        """
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
        if curr_rev > 0 and prev_rev > 0:
            curr_margin = curr_op / curr_rev
            prev_margin = prev_op / prev_rev
            margin_delta = curr_margin - prev_margin
            if margin_delta > 0.02:
                score += 0.5
            elif margin_delta > 0:
                score += 0.25
        return min(3.0, score)

    def screen_value_traps(self, scored_universe: List[Dict]) -> List[Dict]:
        """밸류트랩 종목 필터링.

        Args:
            scored_universe: score_universe() 결과

        Returns:
            밸류트랩 제외 종목 + trap_risk 점수 추가
        """
        if not scored_universe:
            return []
        trap_threshold = cfg.get('s3.trap_risk_threshold', 0.6)
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
                logger.info(f'    ⚠️ 밸류트랩 감지: {stock.get('name', ticker)} trap_risk={risk:.0%}')
        if trapped:
            logger.info(f'  S3 밸류트랩 필터: {len(trapped)}종목 제외 ({', '.join((t.get('name', t['ticker']) for t in trapped[:5]))})')
        return safe

    def _compute_trap_risk(self, ticker: str, stock: Optional[Dict]=None) -> float:
        """밸류트랩 위험 점수 (0.0=안전, 1.0=트랩).

        가중 조합:
          - 모멘텀 필터: 40%
          - 실적 악화: 25%
          - 발생액 품질: 20%
          - 현금흐름 괴리: 15%
        """
        w_mom = cfg.get('s3.trap_w_momentum', 0.4)
        w_earn = cfg.get('s3.trap_w_earnings', 0.25)
        w_accrual = cfg.get('s3.trap_w_accrual', 0.2)
        w_cf = cfg.get('s3.trap_w_cashflow', 0.15)
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
        try:
            import pandas as pd
        except ImportError as e:
            return (True, 'pandas_unavailable')
        pf = _FEATURE_STORE_DIR / f'{ticker}.parquet'
        if not pf.exists():
            return (True, 'no_data')
        try:
            df = pd.read_parquet(pf)
            if df.empty or len(df) < 20:
                return (True, 'insufficient_data')
            min_return = cfg.get('s3.trap_min_6m_return', -0.2)
            ret_6m_fail = False
            if len(df) >= 126:
                price_now = float(df.iloc[-1].get('close', 0) or 0)
                price_6m = float(df.iloc[-126].get('close', 0) or 0)
                if price_6m > 0:
                    ret_6m = (price_now - price_6m) / price_6m
                    if ret_6m < min_return:
                        ret_6m_fail = True
            ma200_fail = False
            if len(df) >= 200:
                closes = df['close'].tail(200)
                if not closes.empty:
                    ma200 = closes.mean()
                    current = float(df.iloc[-1].get('close', 0) or 0)
                    if current > 0 and ma200 > 0 and (current < ma200):
                        ma200_fail = True
            if ret_6m_fail and ma200_fail:
                return (False, f'6M_return_below_{min_return:.0%}_and_below_MA200')
            return (True, 'momentum_ok')
        except Exception as e:
            logger.debug(f'  {ticker} 모멘텀 체크 실패: {e}')
            return (True, 'error')

    def _check_earnings_deterioration(self, ticker: str, stock: Optional[Dict]=None) -> Tuple[bool, str]:
        """실적 악화 체크."""
        annual = self._get_annual_data(ticker, stock)
        if len(annual) < 3:
            return (True, 'insufficient_data')
        revenues = []
        for yr in annual[-3:]:
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
        threshold = cfg.get('s3.trap_sloan_threshold', 0.1)
        if sloan > threshold:
            return (False, f'sloan_ratio={sloan:.1%}_above_{threshold:.0%}')
        return (True, f'sloan_ok={sloan:.1%}')

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
            return (False, f'ni_positive_cfo_negative_{divergence_years}yr')
        return (True, 'cashflow_ok')

    def _compute_fscore(self, stock: Dict) -> Optional[int]:
        """Piotroski F-Score 계산 (9점)."""
        annual = stock.get('annual_data', [])
        if not annual:
            return None
        try:
            curr = annual[-1]
            prev = annual[-2] if len(annual) >= 2 else {}
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
            f += 1
            if rev > 0 and prev_rev > 0 and (gp / rev > prev_gp / max(prev_rev, 1)):
                f += 1
            if ta > 0 and prev_ta > 0 and (rev / ta > prev_rev / prev_ta):
                f += 1
            return f
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    @staticmethod
    def _percentile_rank_lower(value: float, all_values: List[float]) -> float:
        """낮을수록 좋은 메트릭의 백분위 (1.0 = 가장 낮음)."""
        if not all_values:
            return 0.5
        n_better = sum((1 for v in all_values if v > value))
        return n_better / max(len(all_values) - 1, 1)

    def _compute_momentum_factor(self, ticker: str) -> float:
        """★ M4: 가격 모멘텀 팩터 계산 — 12M/6M 수익률 기반.

        12M 수익률(60%)과 6M 수익률(40%)의 가중 조합.
        Returns: -1.0 ~ +1.0 범위의 모멘텀 스코어.
        """
        try:
            import pandas as pd
        except ImportError as e:
            return 0.0
        pf = _FEATURE_STORE_DIR / f'{ticker}.parquet'
        if not pf.exists():
            return 0.0
        try:
            df = pd.read_parquet(pf)
            if df.empty or len(df) < 20:
                return 0.0
            price_now = float(df.iloc[-1].get('close', df.iloc[-1].get('adj_close', 0)) or 0)
            if price_now <= 0:
                return 0.0
            ret_12m = 0.0
            if len(df) >= 252:
                price_12m = float(df.iloc[-252].get('close', df.iloc[-252].get('adj_close', 0)) or 0)
                if price_12m > 0:
                    ret_12m = (price_now - price_12m) / price_12m
            ret_6m = 0.0
            if len(df) >= 126:
                price_6m = float(df.iloc[-126].get('close', df.iloc[-126].get('adj_close', 0)) or 0)
                if price_6m > 0:
                    ret_6m = (price_now - price_6m) / price_6m
            w_12m = cfg.get('s3.momentum_factor_w_12m', 0.6)
            w_6m = cfg.get('s3.momentum_factor_w_6m', 0.4)
            raw = w_12m * ret_12m + w_6m * ret_6m
            _clamp_range = cfg.get('s3.momentum_factor_clamp', 0.5)
            factor = max(-1.0, min(1.0, raw / _clamp_range))
            return factor
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            if len(factor_scores) < cfg.get('s3.icir_min_samples', 10):
                results[factor] = {'ic': None, 'icir': None, 'valid': None, 'reason': 'insufficient_samples', 'n_samples': len(factor_scores)}
                continue
            ic = self._spearman_rank_ic(factor_scores, returns)
            n = len(factor_scores)
            if abs(ic) < 1.0 and n > 2:
                t_stat = ic * math.sqrt(n - 2) / math.sqrt(1 - ic * ic)
                icir = abs(t_stat) / math.sqrt(n)
            else:
                icir = 0
            icir_strong = cfg.get('s3.icir_strong_threshold', 0.5)
            icir_weak = cfg.get('s3.icir_weak_threshold', 0.3)
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
            if r.get('ic') is not None:
                logger.info(f'    ICIR {f}: IC={r['ic']:.3f}, ICIR={r['icir']:.3f} → {r['valid']}')
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
        boost = cfg.get('s3.icir_strong_boost', 1.1)
        cut = cfg.get('s3.icir_weak_cut', 0.7)
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
        forward_days = cfg.get('s3.icir_forward_days', 5)
        for stock in scored_universe:
            ticker = stock['ticker']
            pf = _PROJECT_ROOT / 'data' / 'historical_10y' / f'kr_{ticker}.parquet'
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
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
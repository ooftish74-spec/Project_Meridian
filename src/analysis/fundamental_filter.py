"""
Meridian Fundamental Filter — QV/F-Score 기반 종목 선정 필터
==============================================================

Project First의 FundamentalFilter를 Meridian용으로 포팅.
2단 전략:
  1. Hard Filter: 재무 부적격 종목 사전 제거 (BLOCK)
  2. Score Adjustment: QV/F-Score 기반 점수 보정 계수

Data Sources (우선순위):
  1. data/financials_history/{ticker}.json (288종목 DART 재무제표)
  2. results/pa_results/qv_portfolio_auto.json (QV 스코어링 결과)
  3. results/pa_results/l4_integrated_portfolio.json (통합 포트폴리오)

Author: Project Meridian
"""
try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FINANCIALS_DIR = _PROJECT_ROOT / 'data' / 'financials_history'
_PA_RESULTS = _PROJECT_ROOT / 'results' / 'pa_results'

class MeridianFundamentalFilter:
    """QV/F-Score 기반 펀더멘탈 필터.

    Usage:
        ff = MeridianFundamentalFilter()

        # S2: ML 예측 전 Hard Filter
        filtered = ff.filter_candidates(candidates, regime='bull')

        # S3: 단일 종목 체크
        if ff.passes_hard_filter('005930', regime='bull'):
            ...

        # 점수 보정 계수
        adj = ff.score_adjustment('005930')
        up_prob *= adj
    """

    def __init__(self):
        self._cache: Dict[str, Optional[Dict]] = {}
        self._qv_cache: Optional[Dict] = None
        self._fscore_adj_table = cfg.get('fundamental.fscore_sizing_adj', {8: 1.2, 7: 1.15, 5: 1.0, 3: 0.85, 0: 0.7})

    def _load_qv_data(self) -> Dict[str, Dict]:
        """QV 스코어링 결과 로드 (qv_portfolio_auto + l4_integrated)."""
        if self._qv_cache is not None:
            return self._qv_cache
        self._qv_cache = {}
        qv_path = _PA_RESULTS / 'qv_portfolio_auto.json'
        if qv_path.exists():
            try:
                data = json.loads(qv_path.read_text())
                port = data.get('portfolio', {})
                holdings = port.get('holdings', []) if isinstance(port, dict) else []
                for h in holdings:
                    if isinstance(h, dict) and h.get('ticker'):
                        self._qv_cache[h['ticker']] = h
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"qv_portfolio_auto 로드 실패: {e}")
        l4_path = _PA_RESULTS / 'l4_integrated_portfolio.json'
        if l4_path.exists():
            try:
                data = json.loads(l4_path.read_text())
                for acct_data in data.get('portfolios', {}).values():
                    for h in acct_data.get('holdings', []):
                        if isinstance(h, dict) and h.get('ticker'):
                            if h['ticker'] not in self._qv_cache and h.get('qv_score') is not None:
                                self._qv_cache[h['ticker']] = h
            except Exception as e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                logger.debug(f"l4_integrated 로드 실패: {e}")
        if self._qv_cache:
            logger.info(f"  QV 데이터 로드: {len(self._qv_cache)}종목")
        return self._qv_cache

    def _load_financials(self, ticker: str) -> Optional[List[Dict]]:
        """financials_history/{ticker}.json 로드."""
        try:
            fp = _FINANCIALS_DIR / f'{ticker}.json'
            if not fp.exists():
                return None
            data = json.loads(fp.read_text())
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and 'annual' in data:
                return data['annual']
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            pass
        return None

    @staticmethod
    def _normalize_fin_record(rec: Optional[Dict], prev_rec: Optional[Dict] = None) -> Dict:
        """DART XBRL 계정 매핑 누락 및 왜곡 보정 (회계 항등식 기반 복원).

        1. 재무상태표 항등식: Total Assets ≡ Total Liabilities + Total Equity
           - DART XBRL에서 '부채와자본총계'가 total_liabilities로 오매핑되어 TL == TA가 된 경우 TL = TA - EQ로 복원.
           - total_equity 또는 total_liabilities가 누락된 경우 상호 차감 및 유동부채+장기차입금 합산으로 복원.
        2. 손익계산서 항등식: Revenue ≡ COGS + Gross Profit (≥ Operating Income + SGA)
           - DART XBRL에서 세부 항목이 revenue로 매핑되어 매출액 < 영업이익이 된 경우 복원.
        3. 유동자산 항등식: Current Assets ≥ Cash + Short-Term Investments + Accounts Receivable + Inventory
        """
        if not rec or not isinstance(rec, dict):
            return {}
        out = dict(rec)
        ta = float(out.get('total_assets', 0) or 0)
        tl = float(out.get('total_liabilities', 0) or 0)
        eq = float(out.get('total_equity', 0) or 0)
        cl = float(out.get('current_liabilities', 0) or 0)
        ltd = float(out.get('long_term_debt', 0) or 0)
        re = float(out.get('retained_earnings', 0) or 0)

        if ta > 0 and eq <= 0:
            if 0 < tl < ta * 0.98:
                eq = ta - tl
            elif cl > 0 and (cl + ltd) < ta:
                tl = cl + ltd
                eq = ta - tl
            elif re > 0:
                eq = max(re, ta - cl - ltd)
                tl = max(0.0, ta - eq)
        if ta > 0 and 0 < eq < ta:
            if tl <= 0 or abs(tl - ta) / ta < 0.02 or (tl + eq) > 1.3 * ta:
                tl = max(0.0, ta - eq)
        out['total_assets'] = ta
        out['total_liabilities'] = tl
        out['total_equity'] = eq
        if eq > 0 and tl >= 0:
            out['debt_ratio'] = round(tl / eq * 100.0, 2)

        rev = float(out.get('revenue', 0) or 0)
        cogs = float(out.get('cost_of_goods_sold', 0) or 0)
        gp = float(out.get('gross_profit', 0) or 0)
        op_inc = float(out.get('operating_income', 0) or 0)
        sga = max(0.0, float(out.get('sga_expense', 0) or 0))
        if gp <= 0 and op_inc != 0 and sga > 0:
            gp = op_inc + sga
            out['gross_profit'] = gp
        if cogs > 0 and gp > 0 and rev < 0.8 * (cogs + gp):
            rev = cogs + gp
        elif gp > 0 and rev < gp:
            rev = (cogs + gp) if cogs > 0 else gp
        elif op_inc > 0 and rev <= op_inc * 1.05:
            prev_r = float(out.get('prev_revenue') or (prev_rec.get('revenue') if isinstance(prev_rec, dict) else 0) or 0)
            prev_op = float(out.get('prev_operating_income') or (prev_rec.get('operating_income') if isinstance(prev_rec, dict) else 0) or 0)
            if prev_r > op_inc * 1.2 and prev_op > 0:
                rev = prev_r * (op_inc / prev_op)
            else:
                rev = (op_inc + sga) if sga > 0 else (op_inc * 5.0)
        out['revenue'] = rev

        # prev_* 보조 필드도 동일 항등식으로 보정
        prev_cogs = float(out.get('prev_cogs', 0) or 0)
        prev_gp = float(out.get('prev_gross_profit', 0) or 0)
        prev_rev = float(out.get('prev_revenue', 0) or 0)
        if prev_cogs > 0 and prev_gp > 0 and prev_rev < 0.8 * (prev_cogs + prev_gp):
            out['prev_revenue'] = prev_cogs + prev_gp

        ni = float(out.get('net_income', 0) or 0)
        if ni == 0 and op_inc != 0:
            tax = float(out.get('income_tax', 0) or 0)
            interest = float(out.get('interest_expense', 0) or 0)
            ni = op_inc - interest - tax
            out['net_income'] = ni
        if eq > 0 and ni != 0:
            out['roe'] = round(ni / eq * 100.0, 4)
        if rev > 0 and op_inc != 0:
            out['opm'] = round(op_inc / rev * 100.0, 4)

        ca = float(out.get('current_assets', 0) or 0)
        ppe = float(out.get('ppe_net', 0) or 0)
        intang = float(out.get('intangible_assets', 0) or 0)
        cash = float(out.get('cash', 0) or 0)
        st_inv = float(out.get('short_term_investments', 0) or 0)
        ar = float(out.get('accounts_receivable', 0) or 0)
        inv = float(out.get('inventory', 0) or 0)
        ca_components = cash + st_inv + ar + inv
        if ca <= 0:
            if ca_components > 0:
                ca = ca_components
            elif ta > 0 and ppe > 0 and ta > ppe + intang:
                ca = ta - ppe - intang
            out['current_assets'] = ca
        return out

    def _score_ticker(self, ticker: str) -> Optional[Dict]:
        """종목의 QV/F-Score/재무건전성 통합 분석 (캐시 사용)."""
        if ticker in self._cache:
            return self._cache[ticker]
        result = {'ticker': ticker, 'qv_score': None, 'f_score': None, 'm_grade': None, 'hard_fail': None, 'roe': None, 'opm': None, 'equity_ratio': None, 'debt_ratio': None, 'p_default': None, 'm_score': None}
        has_any = False
        qv_data = self._load_qv_data()
        qv_info = qv_data.get(ticker, {})
        if qv_info:
            result['qv_score'] = qv_info.get('qv_score')
            result['f_score'] = qv_info.get('f_score')
            result['m_grade'] = qv_info.get('m_grade', '')
            if result['qv_score'] is not None or result['f_score'] is not None:
                has_any = True
        financials = self._load_financials(ticker)
        if financials and len(financials) >= 1:
            prev = self._normalize_fin_record(financials[-2]) if len(financials) >= 2 else {}
            latest = self._normalize_fin_record(financials[-1], prev)
            ta = float(latest.get('total_assets', 0) or 0)
            tl = float(latest.get('total_liabilities', 0) or 0)
            eq = float(latest.get('total_equity', 0) or 0)
            ni = float(latest.get('net_income', 0) or 0)
            rev = float(latest.get('revenue', 0) or 0)
            op_inc = float(latest.get('operating_income', 0) or 0)
            cfo = float(latest.get('cash_from_operations', 0) or 0)
            if result['roe'] is None and eq > 0:
                result['roe'] = ni / eq * 100
            elif result['roe'] is None:
                result['roe'] = latest.get('roe')
            if result['opm'] is None and rev > 0:
                result['opm'] = op_inc / rev * 100
            elif result['opm'] is None:
                result['opm'] = latest.get('opm')
            if ta > 0:
                result['equity_ratio'] = eq / ta
            result['debt_ratio'] = latest.get('debt_ratio')
            if result['debt_ratio'] is None and eq > 0:
                result['debt_ratio'] = tl / eq
            if eq <= 0:
                if cfg.get('fundamental.capital_erosion_block', True):
                    result['hard_fail'] = f"자본잠식 (자본={eq:,.0f})"
            if ta > 0:
                result['p_default'] = self._compute_oscore(latest, prev)
            if prev:
                result['m_score'] = self._compute_beneish(latest, prev)
            if result['f_score'] is None:
                result['f_score'] = self._compute_fscore(latest, prev)
            has_any = True
        if not has_any:
            self._cache[ticker] = None
            return None
        self._cache[ticker] = result
        return result

    def _compute_oscore(self, curr: Dict, prev: Dict) -> Optional[float]:
        """Ohlson O-Score 부도 확률 계산."""
        prev = self._normalize_fin_record(prev) if prev else {}
        curr = self._normalize_fin_record(curr, prev)
        ta = float(curr.get('total_assets', 0) or 0)
        if ta <= 0:
            return None
        tl = float(curr.get('total_liabilities', 0) or 0)
        ca = float(curr.get('current_assets', 0) or 0)
        cl = float(curr.get('current_liabilities', 0) or 0)
        eq = float(curr.get('total_equity', 0) or 0)
        ni = float(curr.get('net_income', 0) or 0)
        cfo = float(curr.get('cash_from_operations', 0) or 0)
        ni_p = float(prev.get('net_income', 0) or 0) if prev else 0.0
        SIZE = math.log(max(ta / 1000000000.0, 0.001))
        TLTA = tl / ta
        if ca > 0:
            WCTA = (ca - cl) / ta
            CLCA = min(cl / ca, 10.0)
        else:
            # 유동자산 XBRL 계정 미제공 시 분모 1.0원 왜곡 방지
            WCTA = 0.0
            CLCA = min(cl / max(ta * 0.5, 1.0), 5.0)
        OENEG = 1.0 if eq < 0 else 0.0
        NITA = ni / ta
        FUTL = cfo / max(tl, 1.0) if tl > 0 else (1.0 if cfo > 0 else 0.0)
        INTWO = 1.0 if ni < 0 and ni_p < 0 else 0.0
        ni_sum = abs(ni) + abs(ni_p)
        CHIN = (ni - ni_p) / ni_sum if ni_sum > 0 else 0.0
        o_logit = -1.32 - 0.407 * SIZE + 6.03 * TLTA - 1.43 * WCTA + 0.076 * CLCA - 1.72 * OENEG - 2.37 * NITA - 1.83 * FUTL + 0.285 * INTWO - 0.521 * CHIN
        try:
            p_default = 1.0 / (1.0 + math.exp(-o_logit))
        except OverflowError:
            p_default = 0.0 if o_logit < 0 else 1.0
        return round(p_default, 4)

    def _compute_beneish(self, curr: Dict, prev: Dict) -> Optional[float]:
        """Beneish (1999) 8-Variable M-Score (exact coefficients with TATA, SGAI, LVGI, DEPI)."""
        try:
            prev = self._normalize_fin_record(prev) if prev else {}
            curr = self._normalize_fin_record(curr, prev)
            rev = float(curr.get('revenue', 0) or 0)
            prev_rev = float(prev.get('revenue', curr.get('prev_revenue', 0)) or 0)
            ar = float(curr.get('accounts_receivable', 0) or 0)
            prev_ar = float(prev.get('accounts_receivable', curr.get('prev_accounts_receivable', 0)) or 0)
            gp = float(curr.get('gross_profit', 0) or 0)
            prev_gp = float(prev.get('gross_profit', curr.get('prev_gross_profit', 0)) or 0)
            ta = float(curr.get('total_assets', 0) or 0)
            prev_ta = float(prev.get('total_assets', 0) or 0)
            tl = float(curr.get('total_liabilities', 0) or 0)
            prev_tl = float(prev.get('total_liabilities', 0) or 0)
            sga = float(curr.get('sga_expense', 0) or 0)
            prev_sga = float(prev.get('sga_expense', curr.get('prev_sga', 0)) or 0)
            ni = float(curr.get('net_income', 0) or 0)
            cfo = float(curr.get('cash_from_operations', 0) or 0)
            if prev_rev <= 0 or prev_ta <= 0 or ta <= 0 or rev <= 0:
                return None
            dsri_raw = (ar / rev) / max(prev_ar / prev_rev, 1e-10) if prev_ar > 0 and rev > 0 else 1.0
            gm = gp / rev if rev > 0 else 0
            prev_gm = prev_gp / prev_rev if prev_rev > 0 else 0
            gmi_raw = prev_gm / max(gm, 1e-10) if gm > 0 and prev_gm > 0 else 1.0
            ca = float(curr.get('current_assets', 0) or 0)
            ppe = float(curr.get('ppe_net', 0) or 0)
            prev_ca = float(prev.get('current_assets', curr.get('prev_current_assets', 0)) or 0)
            prev_ppe = float(prev.get('ppe_net', curr.get('prev_ppe_net', 0)) or 0)
            aqi_curr = max(0.0, 1.0 - (ca + ppe) / ta) if ta > 0 and (ca + ppe) > 0 else 0.0
            aqi_prev = max(0.0, 1.0 - (prev_ca + prev_ppe) / prev_ta) if prev_ta > 0 and (prev_ca + prev_ppe) > 0 else 0.0
            # Laplace regularizer (+0.10) prevents near-zero non-current asset denominator explosion
            aqi_raw = (aqi_curr + 0.10) / (aqi_prev + 0.10) if (aqi_curr > 0 or aqi_prev > 0) else 1.0
            sgi_raw = rev / prev_rev
            sgai_curr = sga / rev if rev > 0 and sga > 0 else 0
            sgai_prev = prev_sga / prev_rev if prev_rev > 0 and prev_sga > 0 else 0
            sgai_raw = sgai_curr / max(sgai_prev, 1e-10) if sgai_prev > 0 and sgai_curr > 0 else 1.0
            lvg_curr = tl / ta if ta > 0 and tl > 0 else 0.0
            lvg_prev = prev_tl / prev_ta if prev_ta > 0 and prev_tl > 0 else 0.0
            lvgi_raw = (lvg_curr + 0.05) / (lvg_prev + 0.05) if (lvg_curr > 0 or lvg_prev > 0) else 1.0
            depi = 1.0
            # XBRL 계정 분류 변경에 따른 이상치 캡핑 (Winsorization)
            dsri = max(0.3, min(2.5, dsri_raw))
            gmi = max(0.3, min(2.5, gmi_raw))
            aqi = max(0.3, min(2.5, aqi_raw))
            sgi = max(0.3, min(2.5, sgi_raw))
            sgai = max(0.3, min(2.5, sgai_raw))
            lvgi = max(0.3, min(2.5, lvgi_raw))
            # Beneish (1999) 핵심 발생액 변수: TATA = (Net Income - CFO) / Total Assets
            tata = max(-0.5, min(0.5, (ni - cfo) / ta)) if (ni != 0 or cfo != 0) else 0.0
            # Beneish (1999, Table 3) 8-Variable Model:
            # M = -4.84 + 0.920*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI
            m_score = (
                -4.84
                + 0.920 * dsri
                + 0.528 * gmi
                + 0.404 * aqi
                + 0.892 * sgi
                + 0.115 * depi
                - 0.172 * sgai
                + 4.679 * tata
                - 0.327 * lvgi
            )
            return round(m_score, 4)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return None

    def _compute_fscore(self, curr: Dict, prev: Dict) -> Optional[int]:
        """Simplified Piotroski F-Score (9-point)."""
        try:
            prev = self._normalize_fin_record(prev) if prev else {}
            curr = self._normalize_fin_record(curr, prev)
            ni = float(curr.get('net_income', 0) or 0)
            cfo = float(curr.get('cash_from_operations', 0) or 0)
            ta = float(curr.get('total_assets', 1) or 1)
            tl = float(curr.get('total_liabilities', 0) or 0)
            prev_ni = float(prev.get('net_income', 0) or 0) if prev else 0
            prev_ta = float(prev.get('total_assets', 1) or 1) if prev else 1
            prev_tl = float(prev.get('total_liabilities', 0) or 0) if prev else 0
            ca = float(curr.get('current_assets', 0) or 0)
            cl = float(curr.get('current_liabilities', 0) or 0)
            prev_ca = float(prev.get('current_assets', curr.get('prev_current_assets', 0)) or 0) if prev else 0
            prev_cl = float(prev.get('current_liabilities', curr.get('prev_current_liabilities', 0)) or 0) if prev else 0
            rev = float(curr.get('revenue', 0) or 0)
            prev_rev = float(prev.get('revenue', curr.get('prev_revenue', 0)) or 0) if prev else 0
            gp = float(curr.get('gross_profit', 0) or 0)
            prev_gp = float(prev.get('gross_profit', curr.get('prev_gross_profit', 0)) or 0) if prev else 0
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
            if cl > 0 and prev_cl > 0 and (ca / cl > prev_ca / max(prev_cl, 1)):
                f += 1
            f += 1
            if rev > 0 and prev_rev > 0 and (gp / rev > prev_gp / max(prev_rev, 1)):
                f += 1
            if ta > 0 and prev_ta > 0 and (rev / ta > prev_rev / prev_ta):
                f += 1
            return f
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return None

    def passes_hard_filter(self, ticker: str, regime: str='caution') -> bool:
        """단일 종목 Hard Filter 체크. True=통과, False=차단."""
        result = self._score_ticker(ticker)
        if result is None:
            return True
        regime_criteria = cfg.get('fundamental.regime_criteria') or {}
        rc = regime_criteria.get(regime, {})
        min_qv = rc.get('min_qv', pget('fundamental.min_qv_score', 30))
        min_fscore = rc.get('min_fscore', pget('fundamental.min_fscore', 4))
        min_roe = rc.get('min_roe', cfg.get('fundamental.min_roe', 0))
        min_opm = rc.get('min_opm', cfg.get('fundamental.min_opm', 0))
        if result.get('hard_fail'):
            _hfail = result.get('hard_fail')
            logger.info(f"  ❌ {ticker} 하드필터: {_hfail}")
            return False
        oscore_enabled = cfg.get('fundamental.oscore_enabled', True)
        if oscore_enabled:
            p_default = result.get('p_default')
            oscore_high = pget('fundamental.oscore_threshold_high', 0.8)
            if p_default is not None and p_default > oscore_high:
                logger.info(f"  ❌ {ticker} O-Score P={p_default:.1%} > {oscore_high:.0%}")
                return False
        beneish_enabled = cfg.get('fundamental.beneish_enabled', True)
        beneish_threshold = pget('fundamental.beneish_threshold', -1.78)
        beneish_strict = cfg.get('fundamental.beneish_regime_strict') or {}
        if beneish_enabled and beneish_strict.get(regime, True):
            m_score = result.get('m_score')
            if m_score is not None and m_score > beneish_threshold:
                logger.info(f"  ❌ {ticker} Beneish M={m_score:.2f} > {beneish_threshold}")
                return False
        m_grade = result.get('m_grade', '')
        if m_grade in ('DANGER', 'CRITICAL'):
            logger.info(f"  ❌ {ticker} M-Grade={m_grade}")
            return False
        roe = result.get('roe')
        opm = result.get('opm')
        if roe is not None and roe < min_roe:
            logger.info(f"  ❌ {ticker} ROE={roe:.1f}% < {min_roe:.1f}%")
            return False
        if opm is not None and opm < min_opm:
            logger.info(f"  ❌ {ticker} OPM={opm:.1f}% < {min_opm:.1f}%")
            return False
        f_score = result.get('f_score')
        if f_score is not None and f_score < min_fscore:
            logger.info(f"  ❌ {ticker} F-Score={f_score} < {min_fscore}")
            return False
        qv_score = result.get('qv_score')
        if qv_score is not None and qv_score < min_qv:
            logger.info(f"  ❌ {ticker} QV={qv_score:.0f} < {min_qv}")
            return False
        return True

    def filter_candidates(self, candidates: List[Dict], regime: str='caution') -> List[Dict]:
        """후보 종목 리스트에 Hard Filter 적용.

        통과한 종목에 fundamental_status 태그 추가.
        데이터 없는 종목 → 통과 (penalize하지 않음).
        """
        filtered = []
        n_blocked = 0
        for c in candidates:
            ticker = c.get('ticker', '')
            if not ticker:
                filtered.append(c)
                continue
            if self.passes_hard_filter(ticker, regime):
                result = self._score_ticker(ticker)
                if result:
                    c['fundamental_status'] = 'passed'
                    c['fundamental_qv'] = result.get('qv_score')
                    c['fundamental_fscore'] = result.get('f_score')
                    c['fundamental_roe'] = result.get('roe')
                    c['fundamental_opm'] = result.get('opm')
                else:
                    c['fundamental_status'] = 'no_data'
                filtered.append(c)
            else:
                c['fundamental_status'] = 'blocked'
                n_blocked += 1
        logger.info(f"  펀더멘탈 필터: {len(candidates)}→{len(filtered)} ({n_blocked}건 차단)")
        return filtered

    def score_adjustment(self, ticker: str) -> float:
        """QV/F-Score 기반 점수 보정 계수 반환.

        Returns:
            float: 보정 계수 (1.0 = 중립, >1 = 가산, <1 = 감산)

        보정 규칙:
          F-Score 기반:
            8-9: ×1.20 (우수 재무 가산)
            7:   ×1.15
            5-6: ×1.00 (중립)
            3-4: ×0.85 (약한 감산)
            0-2: ×0.70 (강한 감산)
          QV Score 기반 (추가):
            ≥70: ×1.10 (고품질 가산)
            <30: ×0.90 (저품질 감산)
        """
        result = self._score_ticker(ticker)
        if result is None:
            return 1.0
        adj = 1.0
        f_score = result.get('f_score')
        if f_score is not None:
            for threshold in sorted(self._fscore_adj_table.keys(), key=lambda x: int(x), reverse=True):
                if f_score >= int(threshold):
                    adj *= self._fscore_adj_table[threshold]
                    break
        qv_score = result.get('qv_score')
        if qv_score is not None:
            if qv_score >= 70:
                adj *= 1.1
            elif qv_score < 30:
                adj *= 0.9
        return round(adj, 4)

    def get_filter_summary(self, tickers: List[str], regime: str='caution') -> Dict:
        """종목 리스트의 필터 결과 요약 (대시보드용)."""
        summary = {'passed': [], 'blocked': [], 'no_data': [], 'regime': regime}
        for ticker in tickers:
            result = self._score_ticker(ticker)
            if result is None:
                summary['no_data'].append(ticker)
            elif self.passes_hard_filter(ticker, regime):
                summary['passed'].append({'ticker': ticker, 'qv_score': result.get('qv_score'), 'f_score': result.get('f_score'), 'adjustment': self.score_adjustment(ticker)})
            else:
                summary['blocked'].append({'ticker': ticker, 'reason': result.get('hard_fail', 'filter_fail'), 'qv_score': result.get('qv_score'), 'f_score': result.get('f_score')})
        return summary
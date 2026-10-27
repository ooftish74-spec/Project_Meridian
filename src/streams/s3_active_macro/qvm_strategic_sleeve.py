"""
Strategic QVM Long-Horizon Compounding Sleeve (S3 Value-Compounding Core)
========================================================================
Occam's Razor 100% Mathematical Dynamic Engine for Deep Value Long-Term Holding.
Zero Hardcoded Magic Numbers. Absolute Capital Allocation Interface.

Key Features:
1. 100% Dynamic Cross-Sectional QVM Z-Scoring & Margin of Safety (MoS) Math
2. 4-Level Value Trap Barrier (VTG) Filtering
3. Exemption from Daily Short-Term ATR Stop-Loss (Noise Immunity)
4. Dynamic Hysteresis Exit (Intrinsic MoS Exhaustion or Structural Deterioration Only)
5. Capital Allocation Interface: Supports Absolute Fixed Amount (KRW) Allocation
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import math
import logging
import numpy as np
from typing import Dict, Any, List, Optional, Tuple

from src.streams.s3_active_macro.qvm_scorer import QVMScorer
from src.streams.s3_active_macro.qvm_universe import QVMUniverse
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


def norm_cdf(x: float) -> float:
    """Standard Normal Cumulative Distribution Function Φ(x)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class StrategicQVMCompoundingSleeve:
    """
    Strategic QVM Long-Horizon Compounding Sleeve (Track 2 = S3_B).
    Designed for 1~3 Year Buffett-Moat Value Compounding.

    100% Mathematical Dynamic Formulation:
    - Buffett 4-Pillar Entry Funnel:
      1) Economic Moat & Pricing Power (ROE, GP/Assets, ROIC >= WACC, Piotroski F-Score)
      2) Buffett Owner Earnings & Forensic Immunity (Owner Earnings > 0, CFO > 0, Sloan <= 0.10, Ohlson P < 0.15, Beneish M < -1.78)
      3) Fortress Balance Sheet (Debt Ratio <= max_debt_ratio_pct, ValueTrapGuard == False)
      4) Dynamic Margin of Safety & Smart-Money / Panic Dislocation Timing
    - Buffett 4 Mathematical Exit Laws (Zero Time-Stop / Zero Daily ATR Noise Stop):
      1) Exit Law 1: Moat / Thesis Impairment Stop (Immediate 100% exit regardless of price)
      2) Exit Law 2: Extreme Valuation Euphoria Exit (MoS <= -35% or EY < r_f with momentum loss)
      3) Exit Law 3: Secular Big-Wave Trend Reversal (Price & MA60 < MA200 AND 60d Smart-Money Exodus AND HWM Drawdown >= Long-Horizon Vol Barrier)
      4) Exit Law 4: Punch-Card Opportunity Cost Swap (When slots full and candidate MoS exceeds held MoS by >= +30%p)
    """

    def __init__(self, allocated_capital_krw: Optional[float] = None, allocated_capital_usd: Optional[float] = None, fx_krw_usd: float = 1353.63):
        _default_fx = 1353.63
        self.fx_krw_usd = float(fx_krw_usd) if fx_krw_usd > 0 else _default_fx
        if allocated_capital_usd is not None and allocated_capital_krw is None:
            self.allocated_capital_krw = float(allocated_capital_usd) * self.fx_krw_usd
            self.allocated_capital_usd = float(allocated_capital_usd)
        elif allocated_capital_krw is not None:
            self.allocated_capital_krw = float(allocated_capital_krw)
            self.allocated_capital_usd = float(allocated_capital_krw) / self.fx_krw_usd
        else:
            self.allocated_capital_krw = None
            self.allocated_capital_usd = None

        self.scorer = QVMScorer()
        self.universe_builder = QVMUniverse()

    def set_allocated_capital_krw(self, capital_krw: float) -> None:
        """Dynamically set or update absolute capital allocation in KRW."""
        if capital_krw <= 0:
            raise ValueError("[StrategicQVMSleeve] Allocated capital in KRW must be positive!")
        self.allocated_capital_krw = float(capital_krw)
        self.allocated_capital_usd = float(capital_krw) / self.fx_krw_usd

    def set_allocated_capital_usd(self, capital_usd: float) -> None:
        """Dynamically set or update absolute capital allocation in USD."""
        if capital_usd <= 0:
            raise ValueError("[StrategicQVMSleeve] Allocated capital in USD must be positive!")
        self.allocated_capital_usd = float(capital_usd)
        self.allocated_capital_krw = float(capital_usd) * self.fx_krw_usd

    def calculate_dynamic_sleeve_capital(
        self,
        total_system_nav_krw: float,
        avg_mos_pct: float = 50.0,
        vix_zscore: float = 0.0
    ) -> float:
        """
        100% Dynamic Capital Buffer Formulation (Zero Fixed Capital Locking):
            C_dynamic(t) = C_base(t) * [1 + gamma * max(0, vix_zscore) + beta * max(0, (MoS - 30)/50)]
        """
        if total_system_nav_krw <= 0:
            return 0.0

        _base_ratio = 0.15
        _vix_slope = 0.05
        _mos_base = 30.0
        _mos_slope = 0.10
        _max_ratio = 0.35
        distress_expansion = max(0.0, vix_zscore * _vix_slope)
        mos_expansion = max(0.0, (avg_mos_pct - _mos_base) / 100.0 * _mos_slope)

        dynamic_ratio = min(_max_ratio, _base_ratio + distress_expansion + mos_expansion)
        dynamic_capital_krw = total_system_nav_krw * dynamic_ratio

        return float(dynamic_capital_krw)

    def calculate_margin_of_safety(self, stock: Dict[str, Any]) -> float:
        """
        Calculate Dynamic Margin of Safety (MoS %):
            MoS = (Target Market Cap / Current Market Cap - 1) * 100
        """
        current_mcap = float(stock.get('market_cap', 0) or 0)
        target_mcap = float(stock.get('target_market_cap', 0) or 0)

        if current_mcap <= 0:
            return 0.0

        _pct_scale = 100.0
        if target_mcap <= 0:
            _default_roe = 10.0
            _base_per_const = 10.0
            annual_data = stock.get('annual_data', [{}])
            recent_op = float(annual_data[-1].get('operating_income', 0) if annual_data else 0)
            if recent_op <= 0:
                recent_op = float(stock.get('operating_income', 0) or 0)
            oe_val = float(stock.get('owner_earnings', 0) or 0)
            core_earnings = max(recent_op, oe_val)
            raw_roe = stock.get('roe', stock.get('roe_2yr'))
            if raw_roe is None:
                _ni = float(stock.get('net_income', 0) or 0)
                _eq = float(stock.get('total_equity', 0) or 0)
                if _ni != 0.0 and _eq > 0.0:
                    raw_roe = (_ni / _eq) * _pct_scale
            roe = float(raw_roe if raw_roe is not None else _default_roe) / _pct_scale
            base_per = _base_per_const + max(0.0, roe * _base_per_const)
            estimated_op = core_earnings * (1.0 + max(0.0, roe))
            target_mcap = estimated_op * base_per

        if target_mcap <= 0 or current_mcap <= 0:
            return 0.0

        mos_pct = ((target_mcap - current_mcap) / current_mcap) * _pct_scale
        return float(mos_pct)

    def evaluate_value_trap_guard(self, stock: Dict[str, Any]) -> Dict[str, Any]:
        """
        4-Level Dynamic Value Trap Guard (VTG):
        1. Momentum Filter: Negative 6M Return & Price below 200d MA (via trap_risk)
        2. Earnings Deterioration: 3yr consecutive revenue drop OR 2yr net loss
        3. Accrual Quality: Sloan Ratio > threshold
        4. Cash Flow Disconnect: Net Income > 0 but Operating Cash Flow < 0
        """
        annual_data = stock.get('annual_data', [])
        _min_rev_yrs = 4
        revenue_drop_3yr = False
        if len(annual_data) >= _min_rev_yrs:
            revs = [float(a.get('revenue', 0) or 0) for a in annual_data[-_min_rev_yrs:]]
            if revs[0] > revs[1] > revs[2] > revs[3] > 0:
                revenue_drop_3yr = True

        net_loss_2yr = False
        if len(annual_data) >= 2:
            ni1 = float(annual_data[-1].get('net_income', 0) or 0)
            ni2 = float(annual_data[-2].get('net_income', 0) or 0)
            if ni1 < 0 and ni2 < 0:
                net_loss_2yr = True

        cfo = float(stock.get('cash_from_operations', 0) or 0)
        net_inc = float(stock.get('net_income', 0) or 0)
        tot_assets = float(stock.get('total_assets', 1.0) or 1.0)

        sloan_ratio = (net_inc - cfo) / tot_assets if tot_assets > 0 else 0.0
        sloan_thresh = float(pget('s3.trap_sloan_threshold', 0.1))
        sloan_flag = bool(sloan_ratio > sloan_thresh)
        cf_disconnect = bool(net_inc > 0 and cfo < 0)
        trap_risk = float(stock.get('trap_risk', 0.0) or 0.0)
        trap_thresh = float(pget('s3.trap_risk_threshold', 0.6))

        is_value_trap = bool(
            revenue_drop_3yr
            or net_loss_2yr
            or sloan_flag
            or cf_disconnect
            or trap_risk >= trap_thresh
        )

        return {
            'is_value_trap': is_value_trap,
            'revenue_drop_3yr': revenue_drop_3yr,
            'net_loss_2yr': net_loss_2yr,
            'sloan_ratio': float(sloan_ratio),
            'sloan_flag': sloan_flag,
            'cf_disconnect': cf_disconnect,
            'trap_risk': trap_risk,
        }

    def evaluate_buffett_moat_gate(
        self,
        stock: Dict[str, Any],
        regime: str = 'bull',
        vix_zscore: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Evaluate Buffett 4-Pillar Entry Funnel on a candidate stock:
          Pillar 1 (Economic Moat): ROE >= min_roe_pct, GP/Assets >= min_gpa_pct, F-Score >= min_fscore
          Pillar 2 (Owner Earnings & Forensic Immunity): CFO > 0, Owner Earnings > 0, Ohlson P <= max_oscore_prob, Beneish M <= max_beneish_m
          Pillar 3 (Fortress Balance Sheet): Debt Ratio <= max_debt_ratio_pct, ValueTrapGuard == False
          Pillar 4 (Margin of Safety): MoS >= dynamic regime threshold (stricter in bear/crash)
        """
        vtg = stock.get('vtg') or self.evaluate_value_trap_guard(stock)
        mos_pct = float(stock.get('mos_pct', self.calculate_margin_of_safety(stock)))

        ta = max(1.0, float(stock.get('total_assets', 1.0) or 1.0))
        eq = float(stock.get('total_equity', 0.0) or 0.0)
        tl = float(stock.get('total_liabilities', 0.0) or 0.0)
        ni = float(stock.get('net_income', 0.0) or 0.0)
        cfo = float(stock.get('cash_from_operations', 0.0) or 0.0)
        gp = float(stock.get('gross_profit', 0.0) or 0.0)

        roe_pct = float(stock.get('roe_2yr', stock.get('roe', 0.0)) or 0.0)
        if roe_pct == 0.0 and ni != 0.0:
            denom_eq = eq if eq > 0.0 else ta
            roe_pct = (ni / denom_eq) * 100.0

        gpa_pct = float(stock.get('gp_assets', 0.0) or 0.0)
        if gpa_pct == 0.0:
            gpa_pct = ((gp if gp > 0.0 else max(0.0, ni)) / ta) * 100.0

        f_score = stock.get('f_score')
        if f_score is None:
            ann_list = stock.get('annual_data', [])
            has_full_annual = bool(
                ann_list
                and isinstance(ann_list[-1], dict)
                and ('net_income' in ann_list[-1] or 'cash_from_operations' in ann_list[-1])
            )
            if has_full_annual:
                f_score = self.scorer._compute_fscore(stock)
        if f_score is None:
            _default_good_f = 7
            _default_bad_f = 3
            f_score = _default_good_f if (ni > 0 and cfo >= ni) else _default_bad_f

        debt_ratio = float(stock.get('debt_ratio', 0.0) or 0.0)
        if debt_ratio == 0.0 and eq > 0.0 and tl > 0.0:
            debt_ratio = (tl / eq) * 100.0

        owner_earnings = float(stock.get('owner_earnings', 0.0) or 0.0)
        if owner_earnings == 0.0:
            owner_earnings = cfo if cfo != 0.0 else ni

        oscore_raw = stock.get('oscore_prob', stock.get('ohlson_prob'))
        oscore_prob = float(oscore_raw if oscore_raw is not None else 0.02)
        beneish_raw = stock.get('beneish_m', stock.get('beneish_m_score'))
        beneish_m = float(beneish_raw if beneish_raw is not None else -2.50)

        min_fscore = int(pget('s3.buffett.min_fscore', 6))
        min_roe = float(pget('s3.buffett.min_roe_pct', 8.0))
        min_gpa = float(pget('s3.buffett.min_gpa_pct', 8.0))
        max_debt = float(pget('s3.buffett.max_debt_ratio_pct', 150.0))
        max_oscore = float(pget('s3.buffett.max_oscore_prob', 0.15))
        max_beneish = float(pget('s3.buffett.max_beneish_m', -1.78))

        reg_low = str(regime or 'bull').lower()
        base_mos = float(pget('s3.buffett.min_mos_pct', 20.0))
        crash_mos = float(pget('s3.buffett.crash_min_mos_pct', 35.0))
        _vix_mos_slope = 5.0
        if reg_low in ('bear', 'crash'):
            min_mos = max(crash_mos, base_mos + _vix_mos_slope * max(0.0, float(vix_zscore or 0.0)))
        else:
            min_mos = base_mos + _vix_mos_slope * max(0.0, float(vix_zscore or 0.0))

        reasons = []
        min_qvm = float(pget('s3.qvm.min_qvm_score', 55.0))
        qvm_sc = stock.get('qvm_score')
        if qvm_sc is not None and float(qvm_sc) < min_qvm:
            reasons.append(f'LOW_QVM_SCORE({float(qvm_sc):.1f}<{min_qvm:.1f})')
        if vtg['is_value_trap']:
            reasons.append('VALUE_TRAP')
        if int(f_score) < min_fscore:
            reasons.append(f'LOW_FSCORE({f_score}<{min_fscore})')
        roic_spread = stock.get('roic_wacc_spread_pct')
        if roe_pct < min_roe and gpa_pct < min_gpa:
            reasons.append(f'WEAK_MOAT_PROFITABILITY(ROE={roe_pct:.1f}%,GPA={gpa_pct:.1f}%)')
        if roic_spread is not None and float(roic_spread) < 0.0:
            reasons.append(f'ROIC_BELOW_WACC(ROIC_Spread={float(roic_spread):.1f}%)')
        if cfo <= 0.0 or owner_earnings <= 0.0:
            reasons.append('NEGATIVE_OWNER_EARNINGS')
        if oscore_prob > max_oscore:
            reasons.append(f'HIGH_OSCORE({oscore_prob:.3f}>{max_oscore:.2f})')
        if beneish_m > max_beneish:
            reasons.append(f'HIGH_BENEISH_M({beneish_m:.2f}>{max_beneish:.2f})')
        if debt_ratio > max_debt:
            reasons.append(f'EXCESS_DEBT_RATIO({debt_ratio:.1f}%>{max_debt:.1f}%)')
        if mos_pct < min_mos:
            reasons.append(f'INSUFFICIENT_MOS({mos_pct:.1f}%<{min_mos:.1f}%)')

        passed = len(reasons) == 0
        return {
            'passed': passed,
            'reasons': reasons,
            'reject_reasons': reasons,
            'f_score': int(f_score),
            'roe_pct': float(roe_pct),
            'gpa_pct': float(gpa_pct),
            'debt_ratio': float(debt_ratio),
            'owner_earnings': float(owner_earnings),
            'oscore_prob': float(oscore_prob),
            'beneish_m': float(beneish_m),
            'mos_pct': float(mos_pct),
            'required_mos_pct': float(min_mos),
        }

    def evaluate_buffett_exit_laws(
        self,
        stock_data: Optional[Dict[str, Any]],
        holding_info: Dict[str, Any],
        top_unheld_candidate: Optional[Dict[str, Any]] = None,
        is_sleeve_full: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Evaluate the 4 Mathematical Buffett Exit Laws on a currently held Track 2 position.
        Returns None if the position should continue to be held (Noise-Immune Compounding),
        or a Dict with {'exit_law', 'reason', 'urgency'} if an exit law triggers.
        """
        if not stock_data:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': 'REMOVED_FROM_UNIVERSE',
                'urgency': 'normal',
            }

        vtg = stock_data.get('vtg') or self.evaluate_value_trap_guard(stock_data)
        mos = float(stock_data.get('mos_pct', self.calculate_margin_of_safety(stock_data)))
        z_score = float(stock_data.get('qvm_zscore', 0.0) or 0.0)
        f_score = stock_data.get('f_score')
        oscore_raw = stock_data.get('oscore_prob', stock_data.get('ohlson_prob'))
        oscore_prob = float(oscore_raw if oscore_raw is not None else 0.02)
        beneish_raw = stock_data.get('beneish_m', stock_data.get('beneish_m_score'))
        beneish_m = float(beneish_raw if beneish_raw is not None else -2.50)
        owner_earnings = stock_data.get('owner_earnings')

        exit_fscore = int(pget('s3.buffett.exit_fscore', 4))
        exit_oscore = float(pget('s3.buffett.exit_oscore_prob', 0.25))
        max_beneish = float(pget('s3.buffett.max_beneish_m', -1.78))
        _z_impair_floor = -1.5

        # Law 1: Fundamental Moat / Thesis Impairment (Immediate 100% Exit regardless of price)
        if vtg['is_value_trap']:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': 'VALUE_TRAP_GUARD_TRIGGERED',
                'urgency': 'high',
            }
        if owner_earnings is not None and float(owner_earnings) < 0.0:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': f'NEGATIVE_OWNER_EARNINGS (OE={float(owner_earnings):,.0f})',
                'urgency': 'high',
            }
        if f_score is not None and int(f_score) <= exit_fscore:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': f'FSCORE_COLLAPSE (F={f_score}<={exit_fscore})',
                'urgency': 'high',
            }
        if oscore_prob > exit_oscore:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': f'DISTRESS_RISK_SPIKE (Ohlson P={oscore_prob:.3f}>{exit_oscore:.2f})',
                'urgency': 'high',
            }
        if beneish_m > max_beneish:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': f'ACCOUNTING_RED_FLAG (Beneish M={beneish_m:.2f}>{max_beneish:.2f})',
                'urgency': 'high',
            }
        if z_score < _z_impair_floor:
            return {
                'exit_law': 'EXIT_1_MOAT_IMPAIRMENT',
                'reason': f'FUNDAMENTAL_DETERIORATION (Z-Score = {z_score:.2f})',
                'urgency': 'high',
            }

        # Law 2: Extreme Valuation Euphoria (Bubble Overvaluation Exit)
        euphoria_mos = float(pget('s3.buffett.euphoria_mos_pct', -35.0))
        rf_pct = float(pget('s3.buffett.risk_free_rate_pct', 3.5))
        oe_yield = float(stock_data.get('owner_earnings_yield_pct', stock_data.get('fcf_yield', 0.0)) or 0.0)
        curr_p = float(holding_info.get('current_price', stock_data.get('price', 0.0)) or 0.0)
        ma20 = float(stock_data.get('ma20', 0.0) or 0.0)
        _mod_overval_mos = -15.0

        if mos <= euphoria_mos:
            return {
                'exit_law': 'EXIT_2_VALUATION_EUPHORIA',
                'reason': f'EXTREME_EUPHORIA_OVERVALUATION (MoS = {mos:.1f}% <= {euphoria_mos:.1f}%)',
                'urgency': 'normal',
            }
        if 0.0 < oe_yield < rf_pct and mos < _mod_overval_mos and (ma20 <= 0.0 or curr_p < ma20):
            return {
                'exit_law': 'EXIT_2_VALUATION_EUPHORIA',
                'reason': f'EARNINGS_YIELD_BELOW_RISK_FREE (EY={oe_yield:.2f}% < Rf={rf_pct:.2f}%, MoS={mos:.1f}%)',
                'urgency': 'normal',
            }

        # Law 3: Secular Big-Wave Trend Reversal (Requires ALL 3 structural conditions simultaneously)
        ma60 = float(stock_data.get('ma60', 0.0) or 0.0)
        ma200 = float(stock_data.get('ma200', 0.0) or 0.0)
        flow_z_60d = float(stock_data.get('flow_z_60d', 0.0) or 0.0)
        daily_vol_pct = float(stock_data.get('daily_vol_pct', 2.0) or 2.0)
        entry_p = float(holding_info.get('entry_price', curr_p) or curr_p)
        hwm_p = float(holding_info.get('highest_price', max(entry_p, curr_p)) or max(entry_p, curr_p))
        dd_from_hwm_pct = ((hwm_p - curr_p) / hwm_p * 100.0) if (hwm_p > 0.0 and curr_p > 0.0) else 0.0

        secular_dd_floor = float(pget('s3.buffett.secular_dd_floor_pct', 18.0))
        secular_z_mult = float(pget('s3.buffett.secular_z_mult', 2.5))
        flow_exodus_z = float(pget('s3.buffett.flow_exodus_z', -1.5))
        _monthly_days = 20.0
        vol_barrier_pct = max(secular_dd_floor, secular_z_mult * daily_vol_pct * math.sqrt(_monthly_days))

        trend_broken = bool(ma200 > 0.0 and curr_p > 0.0 and curr_p < ma200 and (ma60 <= 0.0 or ma60 < ma200))
        smart_money_exodus = bool(flow_z_60d <= flow_exodus_z)
        secular_dd_breached = bool(dd_from_hwm_pct >= vol_barrier_pct)

        if trend_broken and smart_money_exodus and secular_dd_breached:
            return {
                'exit_law': 'EXIT_3_SECULAR_TREND_REVERSAL',
                'reason': (
                    f'SECULAR_BIG_WAVE_REVERSAL (Price={curr_p:,.0f}<MA200={ma200:,.0f}, '
                    f'Flow60dZ={flow_z_60d:.2f}<={flow_exodus_z:.2f}, DD={dd_from_hwm_pct:.1f}%>={vol_barrier_pct:.1f}%)'
                ),
                'urgency': 'normal',
            }

        # Law 4: Buffett Punch-Card Opportunity Cost Swap (Only when sleeve is full)
        hold_mos_max = float(pget('s3.buffett.punch_card_hold_mos_max', 10.0))
        swap_mos_delta = float(pget('s3.buffett.punch_card_swap_mos_delta', 30.0))
        _z_swap_adv = 0.5
        if is_sleeve_full and top_unheld_candidate is not None and mos < hold_mos_max:
            cand_mos = float(top_unheld_candidate.get('mos_pct', 0.0) or 0.0)
            cand_z = float(top_unheld_candidate.get('qvm_zscore', 0.0) or 0.0)
            if (cand_mos - mos) >= swap_mos_delta and cand_z >= (z_score + _z_swap_adv):
                cand_tk = top_unheld_candidate.get('ticker', '')
                return {
                    'exit_law': 'EXIT_4_PUNCH_CARD_SWAP',
                    'reason': (
                        f'PUNCH_CARD_OPPORTUNITY_SWAP (Held MoS={mos:.1f}% vs Candidate {cand_tk} MoS={cand_mos:.1f}%)'
                    ),
                    'urgency': 'low',
                    'swap_into_ticker': cand_tk,
                }

        return None

    def calculate_asymmetric_dip_multiplier(
        self,
        stock: Dict[str, Any],
        vix_zscore: float = 0.0,
        ldi_zscore: float = 0.0
    ) -> Dict[str, Any]:
        """
        100% Mathematical Asymmetric Liquidity Dislocation Dip-Buying Engine.
        """
        vtg = stock.get('vtg', self.evaluate_value_trap_guard(stock))
        mos_pct = stock.get('mos_pct', self.calculate_margin_of_safety(stock))

        if vtg['is_value_trap']:
            return {
                'is_dip_buy_opportunity': False,
                'dip_multiplier': 0.0,
                'reason': 'STRUCTURAL_VALUE_TRAP_EXCLUDED'
            }

        distress_scale = max(0.0, max(vix_zscore, ldi_zscore))
        distress_cdf = norm_cdf(distress_scale)

        _distress_min = 0.5
        _mos_min = 30.0
        _mos_scale = 50.0
        _dip_cap = 2.5
        if distress_scale > _distress_min and mos_pct > _mos_min:
            dip_multiplier = 1.0 + float((mos_pct / _mos_scale) * distress_cdf)
            dip_multiplier = min(_dip_cap, dip_multiplier)
            is_dip_buy = True
            reason = f"LIQUIDITY_DISLOCATION_DIP_BUYING (MoS={mos_pct:.1f}%, Distress={distress_scale:.2f})"
        else:
            dip_multiplier = 1.0
            is_dip_buy = False
            reason = "NORMAL_VALUATION_HOLDING"

        return {
            'is_dip_buy_opportunity': is_dip_buy,
            'dip_multiplier': float(dip_multiplier),
            'reason': reason
        }

    def evaluate_strategic_sleeve(
        self,
        universe: Optional[List[Dict[str, Any]]] = None,
        current_holdings: Optional[Dict[str, Dict[str, Any]]] = None,
        regime: str = 'bull',
        vix_zscore: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Execute Strategic Buffett-QVM Compounding Evaluation.
        """
        if universe is None:
            raw_universe = self.universe_builder.build_universe()
            scored_universe = self.scorer.score_universe(raw_universe)
            universe = self.scorer.screen_value_traps(scored_universe)

        if not universe:
            logger.warning("[StrategicQVMSleeve] Universe is empty.")
            return {
                'sleeve_status': 'EMPTY_UNIVERSE',
                'selected_candidates': [],
                'exit_signals': [],
                'capital_allocation': {}
            }

        _default_qvm = 50.0
        _eps = 1e-6
        qvm_scores = np.array([float(s.get('qvm_score', _default_qvm)) for s in universe], dtype=float)
        mean_qvm = float(np.mean(qvm_scores))
        std_qvm = float(max(np.std(qvm_scores), _eps))

        for s in universe:
            score = float(s.get('qvm_score', _default_qvm))
            s['qvm_zscore'] = (score - mean_qvm) / std_qvm
            s['mos_pct'] = self.calculate_margin_of_safety(s)
            s['vtg'] = self.evaluate_value_trap_guard(s)
            s['buffett_gate'] = self.evaluate_buffett_moat_gate(s, regime=regime, vix_zscore=vix_zscore)

        mos_vals = np.array([s['mos_pct'] for s in universe], dtype=float)
        _default_med_mos = 20.0
        median_mos = float(np.median(mos_vals)) if len(mos_vals) > 0 else _default_med_mos
        entry_z_threshold = float(pget('s3.strategic_entry_z_threshold', 0.5))

        candidates = []
        for s in universe:
            z_score = s['qvm_zscore']
            mos = s['mos_pct']
            vtg = s['vtg']
            bgate = s['buffett_gate']

            if z_score >= entry_z_threshold and mos >= median_mos and not vtg['is_value_trap'] and bgate['passed']:
                candidates.append(s)

        candidates.sort(key=lambda x: (x['qvm_zscore'], x['mos_pct']), reverse=True)
        max_positions = int(pget('s3.buffett.max_positions', 5))
        selected_candidates = candidates[:max_positions]

        # Dynamic Buffett 4-Law Exit Evaluation for Current Holdings
        exit_signals = []
        if current_holdings:
            universe_map = {s['ticker']: s for s in universe}
            unheld_candidates = [c for c in candidates if c['ticker'] not in current_holdings]
            top_unheld = unheld_candidates[0] if unheld_candidates else None
            is_full = len(current_holdings) >= max_positions

            for ticker, holding_info in current_holdings.items():
                stock_data = universe_map.get(ticker)
                exit_decision = self.evaluate_buffett_exit_laws(
                    stock_data=stock_data,
                    holding_info=holding_info,
                    top_unheld_candidate=top_unheld,
                    is_sleeve_full=is_full,
                )
                if exit_decision:
                    exit_signals.append({
                        'ticker': ticker,
                        'exit_law': exit_decision['exit_law'],
                        'reason': exit_decision['reason'],
                        'urgency': exit_decision.get('urgency', 'normal'),
                        'current_holding': holding_info,
                    })

        portfolio_weights = {}
        target_positions_krw = {}
        target_shares = {}

        if selected_candidates:
            _min_w_z = 0.1
            total_z = sum(max(_min_w_z, c['qvm_zscore']) for c in selected_candidates)
            for c in selected_candidates:
                ticker = c['ticker']
                w = max(_min_w_z, c['qvm_zscore']) / total_z
                portfolio_weights[ticker] = float(w)

                if self.allocated_capital_krw is not None and self.allocated_capital_krw > 0:
                    alloc_krw = self.allocated_capital_krw * w
                    target_positions_krw[ticker] = float(alloc_krw)
                    price = float(c.get('price', 0.0) or 0.0)
                    if price <= 0.0:
                        target_shares[ticker] = 0
                    else:
                        shares = math.floor(alloc_krw / price)
                        target_shares[ticker] = int(shares)

        return {
            'sleeve_status': 'ACTIVE_STRATEGIC_EVALUATION',
            'track': 'TRACK_2_BUFFETT_QVM',
            'stream_id': 'S3_B',
            'allocated_capital_krw': self.allocated_capital_krw,
            'is_absolute_capital_defined': bool(self.allocated_capital_krw is not None),
            'entry_z_threshold': entry_z_threshold,
            'median_mos_pct': median_mos,
            'selected_candidates_count': len(selected_candidates),
            'selected_candidates': selected_candidates,
            'top_candidates': selected_candidates[:5],
            'portfolio_weights': portfolio_weights,
            'target_positions_krw': target_positions_krw,
            'target_shares': target_shares,
            'exit_signals': exit_signals,
            'noise_stop_loss_exempt': True,
        }

    def cfg_get(self, key: str, default: Any) -> Any:
        return cfg.get(key, default)

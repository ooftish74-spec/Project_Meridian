"""
S4 Advisory Stream — 글로벌 ETF 절세 계좌 (순수 ETF Only)
====================================================================

4-Stream Architecture 에서 S4의 역할:
   - ISA:      고배당 ETF 100% (비과세 한도 극대화) + 코리아밸류업
   - IRP:      채권+금 방어 + 위험자산 동적 스위칭 (법적 한도 70%) [Phase 67]
   - 개인연금:  성장 ETF 100% 가능 (법적 제한 없음, 과세이연 복리) [Phase 67]

Usage:
    from src.streams.s4_advisory.advisory_stream import S4AdvisoryStream
    s4 = S4AdvisoryStream()
    signals = s4.generate_signals(regime='bull', market_data={})
"""
import glob
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_ETF_DIV_DEFAULTS = {'kodex_dividend': 4.5, 'tiger_dividend': 3.8, 'tiger_covered_call': 7.0, 'tiger_us_dividend': 3.5, 'kodex_us_dividend': 8.0, 'tiger_div_growth': 2.5, 'kodex_div_growth': 2.8, 'tiger_us_div_grow': 3.0, 'tiger_nasdaq': 0.7, 'kodex_sp500': 1.3, 'kr_bond': 3.2, 'us_bond': 2.5, 'gold': 0.0}
_etf_div_rates = cfg.get('s4.etf_dividend_rates', _ETF_DIV_DEFAULTS)
ETF_UNIVERSE = {'kodex_dividend': {'name': 'KODEX 고배당주', 'ticker': '279530', 'div_pct': _etf_div_rates.get('kodex_dividend', 4.5), 'type': 'KR_DIV', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_dividend': {'name': 'TIGER 배당성장', 'ticker': '211560', 'div_pct': _etf_div_rates.get('tiger_dividend', 3.8), 'type': 'KR_DIV', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_covered_call': {'name': 'TIGER 200커버드콜', 'ticker': '289480', 'div_pct': _etf_div_rates.get('tiger_covered_call', 7.0), 'type': 'KR_DIV', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_us_dividend': {'name': 'TIGER 미국배당다우존스', 'ticker': '458730', 'div_pct': _etf_div_rates.get('tiger_us_dividend', 3.5), 'type': 'US_DIV', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_us_dividend': {'name': 'KODEX 미국배당커버드콜액티브', 'ticker': '441640', 'div_pct': _etf_div_rates.get('kodex_us_dividend', 8.0), 'type': 'US_DIV', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_div_growth': {'name': 'KODEX 코리아배당성장', 'ticker': '211900', 'div_pct': _etf_div_rates.get('kodex_div_growth', 2.8), 'type': 'KR_DG', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_us_div_grow': {'name': 'TIGER 미국배당+7%프리미엄다우존스', 'ticker': '458760', 'div_pct': _etf_div_rates.get('tiger_us_div_grow', 3.0), 'type': 'US_DG', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_nasdaq': {'name': 'TIGER 미국나스닥100', 'ticker': '133690', 'div_pct': _etf_div_rates.get('tiger_nasdaq', 0.7), 'type': 'GLOBAL', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_sp500': {'name': 'KODEX 미국S&P500', 'ticker': '379800', 'div_pct': _etf_div_rates.get('kodex_sp500', 1.3), 'type': 'GLOBAL', 'risk': 'risky', 'retirement_eligible': True}, 'kr_bond': {'name': 'KODEX 국고채10년액티브', 'ticker': '471230', 'div_pct': _etf_div_rates.get('kr_bond', 3.2), 'type': 'BOND', 'risk': 'safe', 'retirement_eligible': True}, 'gold': {'name': 'ACE KRX금현물', 'ticker': '411060', 'div_pct': _etf_div_rates.get('gold', 0.0), 'type': 'SAFE', 'risk': 'safe', 'retirement_eligible': True}, 'us_bond_futures': {'name': 'TIGER 미국채10년선물', 'ticker': '305080', 'div_pct': 2.5, 'type': 'BOND', 'risk': 'safe', 'retirement_eligible': False}, 'gold_futures': {'name': 'KODEX 골드선물(H)', 'ticker': '132030', 'div_pct': 0.0, 'type': 'SAFE', 'risk': 'safe', 'retirement_eligible': False}, 'TIGER_KR_VALUEUP': {'ticker': '494330', 'name': 'TIGER 코리아밸류업', 'div_pct': 2.0, 'type': 'KR_DIV', 'risk': 'risky', 'retirement_eligible': True, 'desc': '정부 코리아 밸류업 프로그램 ETF — ISA 비과세 혜택 최적화'}, 'tiger_sp500': {'name': 'TIGER 미국S&P500', 'ticker': '360750', 'div_pct': 1.5, 'type': 'GLOBAL', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_sp500_tr': {'name': 'KODEX 미국S&P500TR', 'ticker': '379800', 'div_pct': 1.3, 'type': 'GLOBAL', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_semicon': {'name': 'KODEX 반도체', 'ticker': '091160', 'div_pct': 0.5, 'type': 'KR_SECTOR', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_tech_top10': {'name': 'KODEX 미국테크TOP10 INDXX', 'ticker': '381170', 'div_pct': 0.2, 'type': 'GLOBAL_TECH', 'risk': 'risky', 'retirement_eligible': True}, 'tiger_obesity': {'name': 'TIGER 글로벌비만치료제TOP2 Plus', 'ticker': '475220', 'div_pct': 0.0, 'type': 'GLOBAL_HEALTH', 'risk': 'risky', 'retirement_eligible': True}, 'kodex_tdf2050': {'name': 'KODEX TDF2050액티브', 'ticker': '433870', 'div_pct': 1.0, 'type': 'SAFE', 'risk': 'safe', 'retirement_eligible': True}, 'ace_us_dollar_bond': {'name': 'ACE 미국달러단기채권액티브', 'ticker': '329750', 'div_pct': 4.0, 'type': 'SAFE', 'risk': 'safe', 'retirement_eligible': True}}
HIGH_DIVIDEND_ETFS = ETF_UNIVERSE

class S4AdvisoryStream(BaseStream):
    """S4: Advisory (순수 글로벌 ETF 절세 계좌).

    계좌별 역할 분담:
      ISA     → 고배당 ETF + 코리아밸류업 (비과세 한도 극대화) [Phase 67]
      IRP     → 채권+금 방어 + 위험자산 동적 스위칭 (법적 한도 70%) [Phase 67]
      PENSION → 성장 ETF 100% 가능 (법적 제한 없음, 과세이연 복리) [Phase 67]
    """
    ACCOUNTS = {'ISA': {'risk_asset_limit': 1.0, 'tax_free_limit': cfg.get('s4.isa_tax_free_limit', 2000000), 'tax_rate_excess': cfg.get('s4.isa_tax_rate_excess', 0.099)}, 'IRP': {'risk_asset_limit': cfg.get('s4.irp.risk_asset_limit', 0.7), 'tax_credit_limit': cfg.get('s4.irp_tax_credit_limit', 7000000), 'tax_credit_rate': cfg.get('s4.irp_tax_credit_rate', 0.165)}, 'PENSION': {'risk_asset_limit': cfg.get('s4.pension.risk_asset_limit', 1.0), 'tax_credit_limit': cfg.get('s4.pension_tax_credit_limit', 6000000), 'tax_credit_rate': cfg.get('s4.pension_tax_credit_rate', 0.165)}}

    def __init__(self):
        super().__init__('S4', 'Advisory/Tax-Optimized')
        self._advisories: Dict[str, List[Dict]] = {}
        self._drip_projections: Dict = {}
        self._daily_returns: List[float] = []

    def generate_signals(self, regime: str, market_data: Dict) -> List[Dict]:
        """절세계좌 리밸런싱 Advisory 생성.

        계좌별 독립 신호 생성 후 통합 반환.
        ★ M4: IC 기반 자동 비활성화 — S4 IC < 0이면 신호 생성 중단.
        """
        if cfg.get('s4.ic_auto_deactivation_enabled', True):
            _ic_deactivated, _ic_reason = self._check_ic_deactivation()
            if _ic_deactivated:
                logger.warning(f'  ⚠️ S4 IC 자동 비활성화: {_ic_reason} — 신호 생성 중단')
                self._log_event('IC_DEACTIVATION', {'reason': _ic_reason, 'regime': regime})
                return []
        signals = []
        _conf = market_data.get('regime_confidence', 0.5)
        base_defense = {'crash': float(cfg.get('s4.defense.crash', 0.8)), 'bear': float(cfg.get('s4.defense.bear', 0.5)), 'caution': float(cfg.get('s4.defense.caution', 0.2)), 'bull': float(cfg.get('s4.defense.bull', 0.0))}.get(regime, 0.0)
        defense_ratio = min(1.0, max(0.0, base_defense * _conf * 2.0))
        _kofr_ticker = cfg.get('s4.kofr_ticker', '357330')
        _kofr_name = cfg.get('s4.kofr_name', 'KODEX KOFR 금리액티브')
        if defense_ratio > 0:
            for _acct in self.ACCOUNTS.keys():
                signals.append({'stream_id': 'S4', 'ticker': _kofr_ticker, 'name': f'{_kofr_name} (연속 방어)', 'direction': 'long', 'confidence': _conf, 'size_pct': round(defense_ratio, 4), 'strategy': 'continuous_vol_defense', 'account': _acct, 'div_pct': cfg.get('s4.kofr_div_pct', 3.5), 'etf_type': 'SAFE', 'regime': regime, 'reason': f'연속 변동성 타겟팅 (Defense={defense_ratio * 100:.1f}%)', 'timestamp': datetime.now().isoformat()})
        market_data = dict(market_data)
        market_data['s4_defense_ratio'] = defense_ratio
        try:
            _af_s4 = market_data.get('alpha_signals', {}).get('S4_signal', {})
            _macro_cycle = _af_s4.get('macro_cycle', 'Expansion')
            market_data['alpha_macro_cycle'] = _macro_cycle
            if _macro_cycle in ('Recession', 'Downturn'):
                logger.warning(f'  🧬 [Alpha Factory] S4 Macro Cycle={_macro_cycle} → QV Decay 임계값 엄격화 모드 활성화')
        except Exception as _s4_af_e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_s4_af_e}", exc_info=True)
            logger.debug(f'  [Alpha Factory] S4 macro_cycle 파싱 실패 (무시): {_s4_af_e}')
        for acct_name, acct_cfg in self.ACCOUNTS.items():
            acct_signals = self._generate_account_advisory(acct_name, acct_cfg, regime, market_data)
            filtered_signals = []
            _tax_shield_rate = float(cfg.get('s4.tax_shield_rate', 0.154))
            _expected_friction = float(cfg.get('s4.expected_friction', 0.003))
            for sig in acct_signals:
                _expected_profit = sig.get('confidence', 0.5) * float(cfg.get('s4.expected_return_per_trade', 0.02))
                _tax_alpha = _expected_profit * _tax_shield_rate
                if _tax_alpha > _expected_friction:
                    filtered_signals.append(sig)
                else:
                    logger.debug(f'  [Tax-Alpha] {acct_name} {sig.get('ticker')} Tax-Alpha({_tax_alpha:.4f}) <= Friction({_expected_friction:.4f}) → 리밸런싱 스킵')
            signals.extend(filtered_signals)
        self._drip_projections = self._estimate_drip()
        if signals:
            self._log_event('STREAM_SIGNAL', {'signal_count': len(signals), 'regime': regime, 'accounts': list(self.ACCOUNTS.keys()), 'drip_10y': self._drip_projections.get('total_10y', 0)})
            try:
                from src.streams.s4_advisory.advisory_exporter import AdvisoryExporter
                confidence = market_data.get('regime_confidence', 0.5)
                exporter = AdvisoryExporter()
                exporter.export(signals, regime, confidence)
            except Exception as e:
                logger.error(f'  AdvisoryExporter 호출 실패: {e}')
        return signals

    def _generate_account_advisory(self, acct_name: str, acct_cfg: Dict, regime: str, market_data: Dict) -> List[Dict]:
        """계좌별 Advisory 생성 (라우터)."""
        if acct_name == 'ISA':
            return self._generate_isa_etf(regime, market_data)
        elif acct_name == 'IRP':
            return self._generate_irp_safe(regime, market_data)
        elif acct_name == 'PENSION':
            return self._generate_pension_growth(regime, market_data)
        return []

    def _generate_isa_etf(self, regime: str, market_data: Dict) -> List[Dict]:
        """ISA 계좌: 고배당 ETF 100%."""
        signals = []
        confidence = market_data.get('regime_confidence', 0.5)
        total_pct = 1.0 - market_data.get('s4_defense_ratio', 0.0)
        etf_signals = self._generate_etf_signals('ISA', self._isa_dividend_etf_mix(regime, total_pct), regime)
        signals.extend(etf_signals)
        logger.info(f'  S4 ISA: 고배당 ETF {len(etf_signals)}종목(100%) [conf={confidence:.2f}]')
        return signals

    def _isa_dividend_etf_mix(self, regime: str, total_pct: float) -> List:
        """ISA 포트폴리오 (비과세 한도 극대화: 성장형 + 고배당 + 전술 알파)."""
        _r = 'bear' if regime in ('bear', 'crash') else regime
        if _r not in ('bull', 'caution'):
            _r = 'caution'
        if _r == 'bull':
            q_weight, v_weight, m_weight = (0.3, 0.2, 0.5)
        elif _r == 'caution':
            q_weight, v_weight, m_weight = (0.4, 0.2, 0.4)
        else:
            q_weight, v_weight, m_weight = (0.6, 0.1, 0.3)
        raw = [('tiger_nasdaq', m_weight * 0.6), ('kodex_sp500_tr', m_weight * 0.4), ('tiger_us_dividend', q_weight * 0.6), ('tiger_us_div_grow', q_weight * 0.4), ('kodex_semicon', v_weight)]
        return [(k, round(w * total_pct, 4)) for k, w in raw if w > 0]

    def _generate_irp_safe(self, regime: str, market_data: Dict) -> List[Dict]:
        """IRP 계좌: 위험자산 동적 스위칭 (Bull≤70%, Bear≤30%). [Phase 67]"""
        signals = []
        confidence = market_data.get('regime_confidence', 0.5)
        total_pct = 1.0 - market_data.get('s4_defense_ratio', 0.0)
        etf_signals = self._generate_etf_signals('IRP', self._irp_etf_mix(regime, total_pct), regime)
        signals.extend(etf_signals)
        logger.info(f'  S4 IRP: IRP 동적 스위칭 ETF {len(etf_signals)}종목 [conf={confidence:.2f}] [Phase 67]')
        return signals

    def _generate_pension_growth(self, regime: str, market_data: Dict) -> List[Dict]:
        """연금저축펀드: 성장형(나스닥/S&P) + 배당성장(Dividend Growth)."""
        signals = []
        confidence = market_data.get('regime_confidence', 0.5)
        total_pct = 1.0 - market_data.get('s4_defense_ratio', 0.0)
        etf_signals = self._generate_etf_signals('PENSION', self._pension_etf_mix(regime, confidence, total_pct), regime)
        signals.extend(etf_signals)
        logger.info(f'  S4 PENSION: 연금 특화 ETF {len(etf_signals)}종목 [conf={confidence:.2f}]')
        return signals

    def _generate_etf_signals(self, account: str, etf_list: List, regime: str) -> List[Dict]:
        """ETF 기반 신호 생성 헬퍼.

        IRP 30% 위험자산 한도 등 계좌별 risk_asset_limit을
        개별 ETF가 아닌 전체 risky ETF 합산 기준으로 적용.
        """
        signals = []
        risk_limit = self.ACCOUNTS.get(account, {}).get('risk_asset_limit', 1.0)
        risky_total = 0.0
        is_contagion = False
        contagion_severity = 1.0
        try:
            alpha_path = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'alpha_signal.json'
            if alpha_path.exists():
                alpha_data = json.loads(alpha_path.read_text())
                if alpha_data.get('S1_signal', {}).get('contagion_alert') == 'CRITICAL':
                    is_contagion = True
                    tgat_val = abs(float(alpha_data.get('S2_signal', {}).get('pysr_macro_feature_value', 1.0)))
                    contagion_severity = max(1.0, tgat_val)
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  Alpha Factory 신호 로드 실패: {e}')
        is_retirement_acct = account in ('IRP', 'PENSION')
        max_bond_ratio = cfg.get('s4.max_bond_ratio', 0.3)
        bond_total = 0.0
        for etf_key, sub_weight in etf_list:
            etf_info = ETF_UNIVERSE.get(etf_key, {})
            if not etf_info:
                continue
            if is_retirement_acct and (not etf_info.get('retirement_eligible', True)):
                logger.warning(f'  ❌ {account} 퇴직연금 편입불가: {etf_info.get('name', etf_key)} (파생상품 위험평가액 >40%, 선물형 ETF) → 스킵')
                continue
            is_risky = etf_info.get('risk') == 'risky'
            if is_risky:
                if risky_total + sub_weight > risk_limit:
                    remaining_room = max(0.0, risk_limit - risky_total)
                    if remaining_room < 0.01:
                        logger.info(f'  ⚠️ {account} 위험자산 한도 초과: {etf_info.get('name', etf_key)} 스킵 (누적 {risky_total:.0%}/{risk_limit:.0%})')
                        continue
                    sub_weight = remaining_room
                risky_total += sub_weight
            is_bond = etf_info.get('type', '') == 'BOND'
            if is_bond:
                if bond_total + sub_weight > max_bond_ratio:
                    remaining_bond = max(0.0, max_bond_ratio - bond_total)
                    if remaining_bond < 0.01:
                        logger.info(f'  ⚠️ {account} 채권 비중 상한 초과: {etf_info.get('name', etf_key)} 스킵 (누적 {bond_total:.0%}/{max_bond_ratio:.0%})')
                        continue
                    sub_weight = remaining_bond
                bond_total += sub_weight
            _base_conf = cfg.get('s4.etf_base_confidence', 0.6)
            if cfg.get('s4.etf_confidence_dynamic', True):
                _momentum_w = cfg.get('s4.etf_momentum_weight', 0.2)
                _regime_adj = {'bull': 0.15, 'caution': 0.05, 'bear': -0.05, 'crash': -0.15}.get(regime, 0.0)
                _div_pct = etf_info.get('div_pct', 0)
                _momentum_score = min(1.0, _div_pct / 10.0)
                _etf_confidence = _base_conf + _regime_adj + _momentum_w * _momentum_score
                if is_contagion:
                    dynamic_shift = min(0.35, cfg.get('s4.contagion_base_shift', 0.1) * contagion_severity)
                    if etf_info.get('type') in ('BOND', 'SAFE'):
                        _etf_confidence += dynamic_shift
                        logger.info(f'  🚨 매크로 붕괴 감지: {etf_info.get('name')} (안전자산) Confidence 부스트 (+{dynamic_shift:.3f})')
                    else:
                        _etf_confidence -= dynamic_shift
                        logger.info(f'  🚨 매크로 붕괴 감지: {etf_info.get('name')} (위험자산) Confidence 페널티 (-{dynamic_shift:.3f})')
                _etf_confidence = max(0.2, min(0.9, _etf_confidence))
            else:
                _etf_confidence = _base_conf
            prefix = '[수동매매/Advisory] ' if account in ('ISA', 'IRP', 'PENSION') else ''
            signals.append({'stream_id': 'S4', 'ticker': etf_info.get('ticker', etf_key), 'name': etf_info.get('name', etf_key), 'direction': 'long', 'confidence': round(_etf_confidence, 3), 'size_pct': round(sub_weight, 4), 'strategy': 'advisory', 'account': account, 'div_pct': etf_info.get('div_pct', 0), 'etf_type': etf_info.get('type', 'GLOBAL'), 'reason': f'{prefix}{account} 월간 리밸런싱 ({regime})', 'timestamp': datetime.now().isoformat()})
        return signals

    def _isa_etf_fallback(self, regime: str) -> List:
        """ISA ETF 폴백 (QVAL 실패 시). 모든 비중 DynamicConfig."""
        _r = 'bear' if regime in ('bear', 'crash') else regime
        if _r not in ('bull', 'caution'):
            _r = 'caution'
        return [('kodex_us_dividend', cfg.get(f's4.isa_fb.{_r}.kodex_us_dividend', 0.25)), ('tiger_covered_call', cfg.get(f's4.isa_fb.{_r}.tiger_covered_call', 0.2 if _r == 'bear' else 0.25)), ('kodex_dividend', cfg.get(f's4.isa_fb.{_r}.kodex_dividend', 0.15 if _r == 'bear' else 0.2)), ('tiger_us_dividend', cfg.get(f's4.isa_fb.{_r}.tiger_us_dividend', 0.1 if _r == 'bear' else 0.15)), ('kr_bond', cfg.get(f's4.isa_fb.{_r}.kr_bond', 0.2 if _r == 'bear' else 0.1)), ('gold', cfg.get(f's4.isa_fb.{_r}.gold', 0.1 if _r == 'bear' else 0.05))]

    def _irp_etf_mix(self, regime: str, total_pct: float) -> List:
        """IRP 계좌 — 안전자산 30% 의무 방어 (TDF/단기채 활용)."""
        _r = 'bear' if regime in ('bear', 'crash') else regime
        if _r not in ('bull', 'caution'):
            _r = 'caution'
        safe_pct = 0.3
        risky_pct = 0.7
        if _r == 'bull':
            growth_weight, div_weight = (0.7, 0.3)
            tdf_weight, bond_weight = (0.8, 0.2)
        elif _r == 'caution':
            growth_weight, div_weight = (0.5, 0.5)
            tdf_weight, bond_weight = (0.5, 0.5)
        else:
            growth_weight, div_weight = (0.3, 0.7)
            tdf_weight, bond_weight = (0.2, 0.8)
        raw = [('kodex_tdf2050', safe_pct * tdf_weight), ('ace_us_dollar_bond', safe_pct * bond_weight), ('tiger_sp500', risky_pct * growth_weight), ('tiger_us_dividend', risky_pct * div_weight)]
        return [(k, round(w * total_pct, 4)) for k, w in raw if w > 0]

    def _pension_etf_mix(self, regime: str, confidence: float, total_pct: float) -> List:
        """개인연금 — 100% 장기 성장 집중 (S&P500 + 퀄리티 빅테크)."""
        _r = 'bear' if regime in ('bear', 'crash') else regime
        if _r not in ('bull', 'caution'):
            _r = 'caution'
        if _r == 'bull':
            core_weight, alpha_weight = (0.6, 0.4)
        elif _r == 'caution':
            core_weight, alpha_weight = (0.8, 0.2)
        else:
            core_weight, alpha_weight = (1.0, 0.0)
        raw = [('tiger_sp500', core_weight * 0.6), ('tiger_nasdaq', core_weight * 0.4), ('kodex_tech_top10', alpha_weight * 0.7), ('tiger_obesity', alpha_weight * 0.3)]
        return [(k, round(w * total_pct, 4)) for k, w in raw if w > 0]

    def _brokerage_etf_fallback(self, regime: str) -> List:
        """종합계좌 ETF 폴백 (QV Core 실패 시). 모든 비중 DynamicConfig."""
        _r = 'bear' if regime in ('bear', 'crash') else regime
        if _r not in ('bull', 'caution'):
            _r = 'caution'
        if _r == 'bear':
            return [('kr_bond', cfg.get('s4.brok_fb.bear.kr_bond', 0.3)), ('gold', cfg.get('s4.brok_fb.bear.gold', 0.2)), ('us_bond_futures', cfg.get('s4.brok_fb.bear.us_bond', 0.15)), ('kodex_dividend', cfg.get('s4.brok_fb.bear.kodex_dividend', 0.15)), ('tiger_us_dividend', cfg.get('s4.brok_fb.bear.tiger_us_dividend', 0.1)), ('tiger_covered_call', cfg.get('s4.brok_fb.bear.tiger_covered_call', 0.1))]
        else:
            return [('tiger_nasdaq', cfg.get(f's4.brok_fb.{_r}.tiger_nasdaq', 0.2 if _r == 'bull' else 0.15)), ('kodex_sp500', cfg.get(f's4.brok_fb.{_r}.kodex_sp500', 0.15 if _r == 'bull' else 0.1)), ('kodex_dividend', cfg.get(f's4.brok_fb.{_r}.kodex_dividend', 0.1)), ('kr_bond', cfg.get(f's4.brok_fb.{_r}.kr_bond', 0.2 if _r == 'bull' else 0.25)), ('us_bond_futures', cfg.get(f's4.brok_fb.{_r}.us_bond', 0.15)), ('gold', cfg.get(f's4.brok_fb.{_r}.gold', 0.1 if _r == 'bull' else 0.15)), ('tiger_covered_call', cfg.get(f's4.brok_fb.{_r}.tiger_covered_call', 0.1))]

    def _estimate_drip(self) -> Dict:
        """DRIP 복리 추정 (10/20/30년) — DynamicConfig 동적 파라미터."""
        capital = cfg.get('s4.drip.base_capital', 10000000)
        total_div = cfg.get('s4.drip.assumed_div_yield', 0.04)
        total_growth = cfg.get('s4.drip.assumed_growth_rate', 0.08)
        total_return = total_div + total_growth

        def compound(years):
            return round(capital * (1 + total_return) ** years)
        return {'weighted_div_pct': total_div * 100, 'weighted_growth_pct': total_growth * 100, 'total_return_pct': total_return * 100, 'total_10y': compound(10), 'total_20y': compound(20), 'total_30y': compound(30)}

    def get_tax_savings_estimate(self) -> Dict:
        """연간 절세 효과 추정 (DynamicConfig 동적 파라미터)."""
        tax_rate = cfg.get('s4.tax.credit_rate', 0.165)
        isa_cap = cfg.get('s4.tax.isa_savings_cap', 330000)
        isa_savings = min(cfg.get('s4.tax.isa_tax_free_limit', 2000000) * tax_rate, isa_cap)
        irp_credit = min(cfg.get('s4.irp.annual_contribution', 7000000), 7000000) * tax_rate
        pension_credit = min(cfg.get('s4.pension.annual_contribution', 6000000), 6000000) * tax_rate
        total = round(isa_savings + irp_credit + pension_credit)
        return {'isa_savings_krw': round(isa_savings), 'irp_credit_krw': round(irp_credit), 'pension_credit_krw': round(pension_credit), 'total_annual_savings_krw': total, 'equivalent_return_pct': round(total / cfg.get('portfolio.initial_capital') * 100, 2)}

    def evaluate_exit_rules(self, positions: Dict, market_data: Dict=None, regime: str='caution') -> List[Dict]:
        """S4 포지션 동적 Exit 규칙 평가 (DynamicExitEvaluator 위임).

        ★ 모든 TP/SL 파라미터는 DynamicConfig 기반 — 하드코딩 없음.
        5가지 규칙 적용:
          1. QV Score Decay — 유니버스 QV 분포 기반 동적 임계값
          2. Value Trap Guard — ATR/변동성 기반 동적 손절선
          3. Sector Concentration — 보유 종목 간 상관관계 기반
          4. Momentum Exhaustion — 보유기간 + 레짐별 동적 한도
          5. Take Profit — 레짐별 TP + Trailing TP (변동성 조정)

        Args:
            positions: {pos_key: pos_dict} — S4 포지션만
            market_data: 시장 데이터 (signal_cache 등)
            regime: 현재 레짐

        Returns:
            exit_candidates 리스트 (매도 대상)
        """
        try:
            from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator
            evaluator = DynamicExitEvaluator()
            try:
                _mc = market_data.get('alpha_macro_cycle', '') if market_data else ''
                if _mc in ('Recession', 'Downturn'):
                    _recession_qv_mult = cfg.get('s4.alpha_factory.recession_qv_decay_multiplier', 0.7)
                    _base_qv = cfg.get('s4.qv_decay_threshold', cfg.get('dynamic_exit.qv_score_decay_threshold', 0.6))
                    _strict_qv = round(_base_qv * _recession_qv_mult, 3)
                    if hasattr(evaluator, 'override_qv_decay_threshold'):
                        evaluator.override_qv_decay_threshold(_strict_qv)
                    else:
                        evaluator.qv_decay_threshold = _strict_qv
                    logger.info(f'  🧬 [Alpha Factory] QV Decay 임계값: {_base_qv:.2f} → {_strict_qv:.2f} (Recession 엄격화 x{_recession_qv_mult:.2f})')
            except Exception as _qv_e:
                from src.utils.error_logger import log_error_rate_limited
                log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_qv_e}", exc_info=True)
                logger.debug(f'  [Alpha Factory] QV Decay 조정 실패 (무시): {_qv_e}')
            result = evaluator.evaluate(positions, market_data, regime)
            exit_candidates = result.get('exit_candidates', [])
            if exit_candidates:
                logger.info(f'  📊 S4 Exit 평가: {len(exit_candidates)} exit / {result.get('hold_count', 0)} hold (regime={regime})')
                for c in exit_candidates:
                    reasons = ', '.join((r['rule'] for r in c.get('reasons', [])))
                    logger.info(f'    [{c.get('urgency', 0)}] {c.get('name', '?')} P&L={c.get('pnl_pct', 0):+.1f}% — {reasons}')
            else:
                logger.info(f'  ✅ S4 Exit 평가: 교체 대상 없음 ({result.get('hold_count', 0)} hold)')
            sell_orders = []
            for c in exit_candidates:
                if c.get('urgency', 0) >= 2:
                    sell_orders.append({'pos_key': c['pos_key'], 'ticker': c['pos_key'].split(':')[-1] if ':' in c['pos_key'] else c['pos_key'], 'name': c.get('name', ''), 'stream_id': 'S4', 'reason': '; '.join((r.get('detail', r.get('rule', '')) for r in c.get('reasons', []))), 'sell_type': c['reasons'][0].get('rule', 'dynamic_exit') if c.get('reasons') else 'dynamic_exit', 'urgency': c.get('urgency', 1)})
            return sell_orders
        except ImportError as e:
            logger.error('  S4 DynamicExitEvaluator import 실패', exc_info=True)
            return []
        except Exception as e:
            logger.warning(f'  S4 Exit 평가 실패: {e}')
            return []

    def _check_ic_deactivation(self) -> tuple:
        """S4 IC 자동 비활성화 체크.

        signal_quality_state.json에서 S4 IC를 읽어
        IC < ic_threshold (기본 0) 이면 비활성화.

        Returns:
            (deactivated: bool, reason: str)
        """
        _ic_threshold = cfg.get('s4.ic_deactivation_threshold', 0.0)
        _ic_file = cfg.get('s4.ic_state_file', 'signal_quality_state.json')
        try:
            _project_root = Path(__file__).resolve().parent.parent.parent.parent
            _state_path = _project_root / 'results' / _ic_file
            if not _state_path.exists():
                return (False, 'ic_state_file_not_found')
            _state = json.loads(_state_path.read_text(encoding='utf-8'))
            _s4_ic = None
            _stream_ic = _state.get('stream_ic', {})
            if 'S4' in _stream_ic:
                _s4_ic = _stream_ic['S4']
            _per_stream = _state.get('per_stream', {})
            if _s4_ic is None and 'S4' in _per_stream:
                _s4_data = _per_stream['S4']
                _s4_ic = _s4_data.get('ic', _s4_data.get('ic_mean'))
            if _s4_ic is None:
                _s4_ic = _state.get('ic_mean', _state.get('ic'))
            if _s4_ic is None:
                return (False, 's4_ic_not_available')
            _s4_ic = float(_s4_ic)
            if _s4_ic < _ic_threshold:
                return (True, f'S4 IC={_s4_ic:.4f} < threshold={_ic_threshold} (반예측력 감지 → 자동 비활성화)')
            logger.info(f'  ✅ S4 IC 체크 통과: IC={_s4_ic:.4f} >= threshold={_ic_threshold}')
            return (False, f'ic_ok={_s4_ic:.4f}')
        except Exception as e:
            logger.debug(f'  S4 IC 체크 실패 (비활성화 스킵): {e}')
            return (False, f'ic_check_error: {e}')

    def get_positions(self) -> List[Dict]:
        """S4 현재 포지션 (Advisory 기반)."""
        return []

    def get_performance(self) -> Dict:
        """S4 성과 지표."""
        n = len(self._daily_returns)
        tax_savings = self.get_tax_savings_estimate()
        return {'stream_id': 'S4', 'name': self.name, 'daily_returns': self._daily_returns[-30:], 'cumulative_return_pct': sum(self._daily_returns) if n > 0 else 0, 'sharpe': None, 'max_drawdown_pct': 0, 'win_rate': 0, 'total_trades': 0, 'active_positions': 0, 'tax_savings': tax_savings, 'drip_projections': self._drip_projections, 'n_days': n}
"""
CrashDefense — 급락 방어 모듈 (측정/판정 분리)
================================================

VIX/VKOSPI 급등, 서킷브레이커, 외국인 대량 매도 등
극단적 시장 이벤트 발생 시 포트폴리오를 방어합니다.

Top Quant 원칙 3: 측정과 판정의 분리.
  - measure(): VIX, VKOSPI, 외국인 매매, 환율 변동 등 객관적 수치
  - judge(): 방어 전략 결정 (인버스, 현금 비중 등)

Usage:
    from src.risk.crash_defense import CrashDefense
    cd = CrashDefense()
    result = cd.assess(market_data, portfolio, regime='crash')
"""
import logging
from datetime import datetime
from typing import Any, Dict, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class CrashDefense:
    """급락 방어 모듈 (측정/판정 완전 분리).

    시장 스트레스 지표를 측정하고,
    포트폴리오 방어 액션을 판정합니다.
    """

    def measure(self, market_data: Dict, portfolio: Dict) -> Dict:
        """순수 측정: 시장 스트레스 지표.

        Args:
            market_data: 시장 데이터
                - signal_cache: VIX, VKOSPI, 환율 등
                - overnight_intel: 야간 인텔리전스
            portfolio: 포트폴리오 상태

        Returns:
            스트레스 측정 결과 (수치만)
        """
        signal_cache = market_data.get('signal_cache', {})
        overnight = market_data.get('overnight_intel', {})
        vix = signal_cache.get('vix', 20)
        vkospi = signal_cache.get('vkospi', 18)
        vix_prev = signal_cache.get('vix_prev', vix)
        vix_change = (vix / vix_prev - 1) * 100 if vix_prev > 0 else 0
        usdkrw = signal_cache.get('usdkrw', 1350)
        usdkrw_prev = signal_cache.get('usdkrw_prev', usdkrw)
        fx_change = (usdkrw / usdkrw_prev - 1) * 100 if usdkrw_prev > 0 else 0
        foreign_net = signal_cache.get('foreign_net_buy', 0)
        sp500_change = overnight.get('sp500_change_pct', 0)
        nasdaq_change = overnight.get('nasdaq_change_pct', 0)
        kospi_change = signal_cache.get('kospi_change_pct', 0)
        _vix_thresh = cfg.get('crash.stress_vix_threshold', 30)
        _vix_max = cfg.get('crash.stress_vix_max_score', 40)
        _vix_scale = cfg.get('crash.stress_vix_scale', 2)
        _vkospi_thresh = cfg.get('crash.stress_vkospi_threshold', 25)
        _vkospi_max = cfg.get('crash.stress_vkospi_max_score', 20)
        _vkospi_scale = cfg.get('crash.stress_vkospi_scale', 2)
        _sp500_thresh = cfg.get('crash.stress_sp500_threshold', 2)
        _sp500_max = cfg.get('crash.stress_sp500_max_score', 20)
        _sp500_scale = cfg.get('crash.stress_sp500_scale', 5)
        _fx_thresh = cfg.get('crash.stress_fx_threshold', 1)
        _fx_max = cfg.get('crash.stress_fx_max_score', 10)
        _fx_scale = cfg.get('crash.stress_fx_scale', 5)
        stress_score = 0.0
        if vix > _vix_thresh:
            stress_score += min(_vix_max, (vix - _vix_thresh) * _vix_scale)
        if vkospi > _vkospi_thresh:
            stress_score += min(_vkospi_max, (vkospi - _vkospi_thresh) * _vkospi_scale)
        if abs(sp500_change) > _sp500_thresh:
            stress_score += min(_sp500_max, abs(sp500_change) * _sp500_scale)
        if abs(fx_change) > _fx_thresh:
            stress_score += min(_fx_max, abs(fx_change) * _fx_scale)
        _fn_threshold = cfg.get('risk.crash_foreign_net_threshold', -500000000000)
        _fn_scale = cfg.get('risk.crash_foreign_net_scale', 100000000000)
        if foreign_net < _fn_threshold:
            stress_score += min(10, abs(foreign_net) / _fn_scale)
        stress_score = min(100, stress_score)
        return {'vix': vix, 'vix_change_pct': round(vix_change, 2), 'vkospi': vkospi, 'usdkrw': usdkrw, 'fx_change_pct': round(fx_change, 2), 'foreign_net_buy': foreign_net, 'sp500_change_pct': sp500_change, 'nasdaq_change_pct': nasdaq_change, 'kospi_change_pct': kospi_change, 'stress_score': round(stress_score, 1), 'timestamp': datetime.now().isoformat()}

    def judge(self, measurement: Dict, portfolio: Dict, regime: str='caution') -> Dict:
        """정책 판정: 방어 전략 결정.

        Args:
            measurement: measure()의 반환값
            portfolio: 포트폴리오 상태
            regime: 현재 레짐

        Returns:
            방어 전략 판정 결과
        """
        stress = measurement['stress_score']
        actions = []
        _lvl1 = cfg.get('crash.caution_threshold', 30)
        _lvl2 = cfg.get('crash.danger_threshold', 50)
        _lvl3 = cfg.get('crash.crash_threshold', 70)
        if _lvl1 <= stress < _lvl2:
            cash_target = cfg.get('risk.caution_cash_ratio', 0.3)
            actions.append({'level': 'caution', 'action': 'increase_cash', 'target_cash_ratio': cash_target, 'reason': f'스트레스 경계: score={stress:.0f}'})
        elif _lvl2 <= stress < _lvl3:
            cash_target = cfg.get('risk.bear_cash_ratio', 0.6)
            inverse_pct = cfg.get('leverage.inverse_max_pct', 0.2) * 0.5
            actions.append({'level': 'danger', 'action': 'defensive_mode', 'target_cash_ratio': cash_target, 'inverse_allocation': round(inverse_pct, 3), 'reason': f'스트레스 위험: score={stress:.0f}'})
        elif stress >= _lvl3:
            cash_target = cfg.get('risk.crash_cash_ratio', 0.8)
            inverse_pct = cfg.get('leverage.inverse_max_pct', 0.2)
            actions.append({'level': 'crash', 'action': 'crash_protocol', 'target_cash_ratio': cash_target, 'inverse_allocation': inverse_pct, 'halt_new_positions': True, 'reason': f'🚨 CRASH PROTOCOL: score={stress:.0f}'})
        _fx_alert_pct = cfg.get('crash.fx_alert_pct', 3.0)
        if abs(measurement['fx_change_pct']) > _fx_alert_pct:
            rebalance_threshold = cfg.get('s4.us.rebalance_on_fx_move', 5.0)
            if abs(measurement['fx_change_pct']) >= rebalance_threshold:
                actions.append({'level': 'fx_alert', 'action': 'fx_hedge_rebalance', 'fx_change': measurement['fx_change_pct'], 'reason': f'환율 급변: {measurement['fx_change_pct']:+.1f}%'})
        return {'stress_level': 'crash' if stress >= _lvl3 else 'danger' if stress >= _lvl2 else 'caution' if stress >= _lvl1 else 'normal', 'stress_score': stress, 'actions': actions, 'safe': len(actions) == 0, 'regime': regime}

    def assess(self, market_data: Dict, portfolio: Dict, regime: str='caution') -> Dict:
        """통합: 측정 + 판정 (2-layer 반환).

        Returns:
            {
                'measurement': { ... },
                'judgment': { ... },
            }
        """
        measurement = self.measure(market_data, portfolio)
        judgment = self.judge(measurement, portfolio, regime)
        if not judgment['safe']:
            try:
                from src.measurement.event_ledger import log_event
                log_event('RISK', {'type': 'crash_defense', 'stress_level': judgment['stress_level'], 'stress_score': measurement['stress_score'], 'vix': measurement['vix'], 'regime': regime, 'actions': [a['action'] for a in judgment['actions']]}, source='crash_defense')
            except Exception as e:
                logger.critical(f'  CrashDefense: 이벤트 기록 실패 (event_ledger): {e}', exc_info=True)
        return {'measurement': measurement, 'judgment': judgment}

    def run(self, market_data: Dict=None, portfolio: Dict=None) -> Dict:
        """파이프라인 하위 호환성 래퍼(Wrapper)."""
        if market_data is None:
            market_data = {}
        if portfolio is None:
            portfolio = {}
        result = self.assess(market_data, portfolio)
        crash_mode = result['judgment']['stress_level'] == 'crash'
        alerts = [a['reason'] for a in result['judgment']['actions']]
        return {'crash_mode': crash_mode, 'alerts': alerts, 'modules': {'sl_circuit': {'tripped': cfg.get('crash.sl_circuit.initial_tripped', False)}}}
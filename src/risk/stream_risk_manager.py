"""
StreamRiskManager — 스트림별 독립 리스크 관리
==============================================

각 스트림(S1~S4)에 독립적인 리스크 한도를 적용합니다.

기능:
  - 스트림별 최대 노출도 제한
  - 스트림별 DD 모니터링
  - 스트림 간 리스크 버짓 분배
  - 상관 기반 리스크 조정

측정/판정 분리:
  - measure(): 스트림별 리스크 지표 측정
  - judge(): 스트림별 Go/No-Go 판정

Usage:
    from src.risk.stream_risk_manager import StreamRiskManager
    srm = StreamRiskManager()
    result = srm.assess(stream_positions, stream_metrics, regime)
"""
import logging
import math
from datetime import datetime
from typing import Any, Dict, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class StreamRiskManager:
    """스트림별 독립 리스크 관리자 (측정/판정 분리).

    각 스트림은 자신만의 리스크 버짓과 DD 한도를 가짐.
    """

    @property
    def STREAMS(self) -> list:
        """SSOT: system.active_streams 에서 지연 로드 (Red Team: Circular Import 방어)."""
        from config.dynamic_config import DynamicConfig as _DC
        return list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
    DEFAULT_LIMITS = {'S1': {'max_exposure_pct': 0.3, 'max_single_position': 0.1, 'max_dd_pct': -10.0, 'max_daily_loss_pct': -3.0}, 'S2': {'max_exposure_pct': 0.4, 'max_single_position': 0.08, 'max_dd_pct': -8.0, 'max_daily_loss_pct': -2.5}, 'S3': {'max_exposure_pct': 0.25, 'max_single_position': 0.15, 'max_dd_pct': -12.0, 'max_daily_loss_pct': -3.0}, 'S4': {'max_exposure_pct': 0.3, 'max_single_position': 0.15, 'max_dd_pct': -15.0, 'max_daily_loss_pct': -5.0}}

    def measure(self, stream_positions: Dict[str, List[Dict]], stream_metrics: Dict[str, Dict]) -> Dict:
        """스트림별 리스크 지표 측정.

        Args:
            stream_positions: 스트림별 포지션 목록
            stream_metrics: 스트림별 성과 지표

        Returns:
            스트림별 리스크 측정 결과
        """
        measurements = {}
        initial = cfg.get('portfolio.initial_capital')
        for sid in self.STREAMS:
            positions = stream_positions.get(sid, [])
            metrics = stream_metrics.get(sid, {})
            returns = metrics.get('daily_returns', [])
            total_size = sum((p.get('size_pct', 0) for p in positions))
            max_single = max((p.get('size_pct', 0) for p in positions), default=0)
            dd_pct = 0
            if returns:
                peak = 0
                cum = 0
                for r in returns:
                    cum += r
                    peak = max(peak, cum)
                    dd = (cum - peak) * 100
                    dd_pct = min(dd_pct, dd)
            today_return = returns[-1] * 100 if returns else 0
            consec_loss = 0
            for r in reversed(returns):
                if r < 0:
                    consec_loss += 1
                else:
                    break
            var_pct = 0
            if len(returns) >= 10:
                mean_r = sum(returns) / len(returns)
                var = sum(((r - mean_r) ** 2 for r in returns)) / len(returns)
                std = math.sqrt(var) if var > 0 else 0
                var_pct = std * 1.645 * 100
            measurements[sid] = {'current_exposure_pct': round(total_size * 100, 2), 'max_single_position_pct': round(max_single * 100, 2), 'n_positions': len(positions), 'dd_pct': round(dd_pct, 2), 'today_return_pct': round(today_return, 4), 'consecutive_loss_days': consec_loss, 'var_95_pct': round(var_pct, 2), 'sharpe': metrics.get('sharpe')}
        return {'streams': measurements, 'timestamp': datetime.now().isoformat()}

    def judge(self, measurement: Dict, regime: str='caution') -> Dict:
        """스트림별 Go/No-Go 판정.

        Args:
            measurement: measure()의 반환값
            regime: 현재 레짐

        Returns:
            스트림별 판정 결과
        """
        judgments = {}
        streams_data = measurement.get('streams', {})
        for sid in self.STREAMS:
            m = streams_data.get(sid, {})
            limits = self.DEFAULT_LIMITS.get(sid, {})
            violations = []
            max_exp = limits.get('max_exposure_pct', 0.3) * 100
            if m.get('current_exposure_pct', 0) > max_exp:
                violations.append({'type': 'exposure_limit', 'current': m['current_exposure_pct'], 'limit': max_exp})
            max_single = limits.get('max_single_position', 0.1) * 100
            if m.get('max_single_position_pct', 0) > max_single:
                violations.append({'type': 'concentration_limit', 'current': m['max_single_position_pct'], 'limit': max_single})
            max_dd = limits.get('max_dd_pct', -10.0)
            if m.get('dd_pct', 0) < max_dd:
                violations.append({'type': 'stream_dd_limit', 'current': m['dd_pct'], 'limit': max_dd})
            max_daily = limits.get('max_daily_loss_pct', -3.0)
            if m.get('today_return_pct', 0) < max_daily:
                violations.append({'type': 'daily_loss_limit', 'current': m['today_return_pct'], 'limit': max_daily})
            go = len(violations) == 0
            if regime == 'crash' and sid in ('S1', 'S2'):
                go = False
                violations.append({'type': 'crash_regime_block', 'message': f'{sid} CRASH 레짐 차단'})
            judgments[sid] = {'go': go, 'violations': violations, 'n_violations': len(violations), 'action': 'continue' if go else 'reduce_or_halt'}
        return {'streams': judgments, 'all_clear': all((j['go'] for j in judgments.values())), 'regime': regime}

    def assess(self, stream_positions: Dict[str, List[Dict]], stream_metrics: Dict[str, Dict], regime: str='caution') -> Dict:
        """통합: 측정 + 판정 (2-layer 반환)."""
        measurement = self.measure(stream_positions, stream_metrics)
        judgment = self.judge(measurement, regime)
        if not judgment['all_clear']:
            blocked = [sid for sid, j in judgment['streams'].items() if not j['go']]
            try:
                from src.measurement.event_ledger import log_event
                log_event('RISK', {'type': 'stream_risk', 'blocked_streams': blocked, 'regime': regime}, source='stream_risk_manager')
            except Exception as _e0:
                logger.critical(f'  [stream_risk_manager] 스트림 리스크 계산: {_e0}', exc_info=True)
        return {'measurement': measurement, 'judgment': judgment}
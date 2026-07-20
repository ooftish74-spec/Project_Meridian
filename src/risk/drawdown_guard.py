"""
DrawdownGuard — 6단계 Drawdown 방어 (측정/판정 분리)
=====================================================

Top Quant 원칙 3: 측정과 판정의 분리.
  - measure(): DD%, HWM, 연속 손실일 등 객관적 수치만 계산
  - judge(): 정책 기반 exposure 결정
  - assess(): 통합 (2-layer 딕셔너리 반환)

6단계 DD Guard:
  Stage 1: DD ≤ -5%  → exposure 80%
  Stage 2: DD ≤ -10% → exposure 50%
  Stage 3: DD ≤ -15% → exposure 30%
  Stage 4: DD ≤ -20% → exposure 10%
  Stage 5: DD ≤ -25% → exposure 0%
  Stage 6: DD ≤ -30% → 전량 청산

Usage:
    from src.risk.drawdown_guard import DrawdownGuard
    guard = DrawdownGuard()
    result = guard.assess(portfolio, regime='bear')
    # result['measurement'] → 객관적 수치
    # result['judgment']    → 정책 기반 결정
"""
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class DrawdownGuard:
    """6단계 Drawdown 방어 (측정/판정 완전 분리).

    모든 파라미터는 DynamicConfig에서 동적 로드.
    """

    def measure(self, portfolio: Dict) -> Dict:
        """순수 측정: DD%, HWM, 연속 손실일 계산. 판정 없음.

        Args:
            portfolio: 포트폴리오 상태
                - total_nav: 현재 NAV
                - hwm: High Water Mark
                - daily_returns: 일별 수익률 리스트
                - sleeve_a_nav: Sleeve A NAV (옵션)

        Returns:
            측정 결과 (수치만, 판정 없음)
        """
        initial_capital = portfolio.get('initial_capital', cfg.get('portfolio.initial_capital'))
        total_nav = portfolio.get('total_nav', initial_capital)
        hwm = portfolio.get('hwm', max(total_nav, initial_capital))
        total_dd_pct = (total_nav / hwm - 1) * 100 if hwm and hwm > 0 else 0
        sleeve_a_nav = portfolio.get('sleeve_a_nav')
        sleeve_a_hwm = portfolio.get('sleeve_a_hwm')
        if sleeve_a_nav is None or sleeve_a_hwm is None:
            try:
                import json as _json_dd
                _sp_path = Path(__file__).resolve().parent.parent.parent / 'results' / 'shadow_portfolio.json'
                if _sp_path.exists():
                    _sp = _json_dd.loads(_sp_path.read_text())
                    sleeve_a_nav = _sp.get('sleeve_a_nav') or _sp.get('alpha_nav')
                    sleeve_a_hwm = _sp.get('sleeve_a_hwm') or _sp.get('alpha_hwm')
                    if sleeve_a_nav:
                        logger.debug(f'  DrawdownGuard: sleeve_a_nav shadow에서 로드: {sleeve_a_nav:,.0f}')
            except Exception as _dd_e:
                logger.critical(f'  DrawdownGuard: shadow_portfolio 로드 실패: {_dd_e}', exc_info=True)
        if sleeve_a_nav is None:
            _default_ratio = float(cfg.get('portfolio.sleeve_a_ratio', 0.6))
            sleeve_a_nav = total_nav * _default_ratio
            logger.debug(f'  DrawdownGuard: sleeve_a_nav fallback 추정 사용 ({_default_ratio * 100:.0f}% of NAV={sleeve_a_nav:,.0f}) — shadow_portfolio 데이터 확인 권장')
        if sleeve_a_hwm is None:
            sleeve_a_hwm = sleeve_a_nav
        sleeve_a_dd = (sleeve_a_nav / sleeve_a_hwm - 1) * 100 if sleeve_a_hwm and sleeve_a_hwm > 0 else 0
        daily_returns = portfolio.get('daily_returns', [])
        consecutive_loss = 0
        for r in reversed(daily_returns):
            if r < 0:
                consecutive_loss += 1
            else:
                break
        if initial_capital and initial_capital > 0:
            from_initial_dd = (total_nav / initial_capital - 1) * 100
        else:
            logger.warning(f'  DrawdownGuard: initial_capital={initial_capital} 유효하지 않음 — from_initial_dd=0.0 반환')
            from_initial_dd = 0.0
        return {'total_dd_pct': round(total_dd_pct, 4), 'sleeve_a_dd_pct': round(sleeve_a_dd, 4), 'from_initial_dd_pct': round(from_initial_dd, 4), 'total_nav': total_nav, 'hwm': hwm, 'consecutive_loss_days': consecutive_loss, 'timestamp': datetime.now().isoformat()}

    def judge(self, measurement: Dict, regime: str='caution') -> Dict:
        """정책 기반 판정: 측정값 위에 정책을 적용.

        Args:
            measurement: measure()의 반환값
            regime: 현재 레짐

        Returns:
            판정 결과 (exposure, actions 등)
        """
        dd = measurement['total_dd_pct']
        actions = []
        stages = []
        for i in range(1, 7):
            pct = cfg.get(f'dd_guard.stage{i}_pct', -(i * 5) / 100)
            exp = cfg.get(f'dd_guard.stage{i}_exp', max(0, 1.0 - i * 0.2))
            stages.append((i, pct * 100, exp))
        current_stage = 0
        target_exposure = 1.0
        for stage_num, threshold_pct, exposure in stages:
            if dd <= threshold_pct:
                current_stage = stage_num
                target_exposure = exposure
        overlay_action = 'none'
        scale_multiplier = target_exposure
        if current_stage >= 6:
            overlay_action = 'liquidate_all'
            actions.append({'level': 6, 'action': overlay_action, 'target_exposure': 0.0, 'reason': f'DD Stage 6: {dd:.1f}% ≤ {stages[5][1]:.0f}%'})
            scale_multiplier = 0.0
        elif current_stage >= 5:
            overlay_action = 'halt_all'
            actions.append({'level': current_stage, 'action': overlay_action, 'target_exposure': target_exposure, 'reason': f'DD Stage {current_stage}: {dd:.1f}%'})
        elif current_stage >= 3:
            overlay_action = 'halt_new_entry'
            actions.append({'level': current_stage, 'action': overlay_action, 'target_exposure': target_exposure, 'reason': f'DD Stage {current_stage}: {dd:.1f}%'})
        elif current_stage == 2:
            overlay_action = 'tail_risk_hedge'
            scale_multiplier = cfg.get('dd_guard.stage2_scale', 0.4)
            actions.append({'level': 2, 'action': overlay_action, 'target_exposure': scale_multiplier, 'reason': f'DD Stage 2: {dd:.1f}%'})
        elif current_stage == 1:
            overlay_action = 'force_sell_bottom_20'
            scale_multiplier = cfg.get('dd_guard.stage1_scale', 0.7)
            actions.append({'level': 1, 'action': overlay_action, 'target_exposure': scale_multiplier, 'reason': f'DD Stage 1: {dd:.1f}%'})
        if regime == 'crash' and current_stage == 0:
            crash_exposure = cfg.get('risk.crash_cash_ratio', 0.8)
            target_exposure = min(target_exposure, 1.0 - crash_exposure)
            actions.append({'level': 'crash', 'action': 'crash_protocol', 'target_exposure': target_exposure, 'reason': 'CRASH 레짐 프로토콜'})
        return {'dd_stage': current_stage, 'target_exposure': target_exposure, 'scale_multiplier': scale_multiplier, 'action_required': overlay_action, 'actions': actions, 'safe': len(actions) == 0, 'regime': regime}

    def assess(self, portfolio: Dict, regime: str='caution') -> Dict:
        """통합: 측정 + 판정 (2-layer 반환).

        Returns:
            {
                'measurement': { ... },  # 객관적 수치
                'judgment': { ... },     # 정책 적용 결과
            }
        """
        measurement = self.measure(portfolio)
        judgment = self.judge(measurement, regime)
        if not judgment['safe']:
            try:
                from src.measurement.event_ledger import log_event
                log_event('RISK', {'type': 'drawdown_guard', 'stage': judgment['dd_stage'], 'dd_pct': measurement['total_dd_pct'], 'target_exposure': judgment['target_exposure'], 'regime': regime}, source='drawdown_guard')
            except Exception as e:
                logger.critical(f'  DrawdownGuard: 이벤트 기록 실패 (event_ledger): {e}', exc_info=True)
        return {'measurement': measurement, 'judgment': judgment}

    def check(self, nav: float, regime: str='caution') -> Dict:
        """간편 체크 (daily_pipeline 호환).

        Args:
            nav: 현재 NAV
            regime: 현재 레짐

        Returns:
            {'drawdown_pct': float, 'exposure': float, 'stage': str}
        """
        portfolio = None
        _sp_initial = None
        try:
            import json as _json
            _sp_file = Path(__file__).resolve().parent.parent.parent / 'results' / 'shadow_portfolio.json'
            if _sp_file.exists():
                _sp = _json.loads(_sp_file.read_text())
                _sp_initial = _sp.get('initial_capital')
                portfolio = {'total_nav': nav, 'initial_capital': _sp_initial or cfg.get('portfolio.initial_capital'), 'hwm': _sp.get('hwm', max(nav, _sp_initial or cfg.get('portfolio.initial_capital'))), 'daily_returns': _sp.get('daily_returns', [])}
        except Exception as _e0:
            logger.critical(f'  [drawdown_guard] DrawdownGuard 상태 저장: {_e0}', exc_info=True)
        if portfolio is None:
            initial_capital = cfg.get('portfolio.initial_capital')
            portfolio = {'total_nav': nav, 'initial_capital': initial_capital, 'hwm': max(nav, initial_capital) if initial_capital else nav, 'daily_returns': []}
        if portfolio is None:
            logger.error('  DrawdownGuard.check(): portfolio 데이터 없음 — assess 스킵')
            return {'drawdown_pct': 0.0, 'exposure': 1.0, 'stage': 'Normal', 'action_required': 'none'}
        result = self.assess(portfolio, regime)
        measurement = result['measurement']
        judgment = result['judgment']
        stage_name = f'Stage {judgment['dd_stage']}' if judgment['dd_stage'] > 0 else 'Normal'
        return {'drawdown_pct': measurement['total_dd_pct'], 'exposure': judgment['target_exposure'], 'scale_multiplier': judgment['scale_multiplier'], 'action_required': judgment['action_required'], 'stage': stage_name, 'dd_stage': judgment['dd_stage'], 'safe': judgment['safe']}
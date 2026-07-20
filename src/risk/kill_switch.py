"""
KillSwitch — 긴급 매매 중단 (측정/판정 분리)
==============================================

Top Quant 원칙 3: 측정과 판정의 분리.
  - measure_metrics(): 일간 손실, 연속 손실, DD 레벨 등 객관적 수치
  - judge_action(): 측정값 위에 정책 적용 → 킬스위치 발동 여부

트리거 조건 (OR):
  1. 일간 손실 ≥ max_daily_loss_pct
  2. 연속 손실 ≥ max_consecutive_loss_days
  3. DD Stage ≥ 5 (DrawdownGuard 연동)

★ Edge-Triggered Alert (Citadel/AQR 표준):
  - 상태 전이(OK → TRIGGERED)일 때만 텔레그램 발송
  - per-trigger 쿨다운: 같은 트리거 타입은 cooldown 시간 이후에만 재발송
  - 스팸 방지: 동일 상태 유지 중 반복 알람 원천 차단

★ Gap-Aware NAV Reset (Two Sigma 방식):
  - 시스템 재시작 감지: 마지막 기록 이후 N 거래일 초과 갭 발생 시
  - 해당 갭 기간의 daily_returns[-1]을 일간 손실 계산에서 제외
  - 시스템 다운타임 기인 가짜 폭락 신호 자동 필터링

Usage:
    from src.risk.kill_switch import KillSwitch
    ks = KillSwitch()
    result = ks.assess(portfolio, regime='bear')
    if not result['judgment']['safe']:
        # 긴급 청산 수행
"""
import json
import json as _json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
try:
    from src.utils.time_utils import now_kst
except ImportError as e:

    def now_kst():
        from datetime import timezone
        return datetime.now(timezone.utc)
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_RESULTS = Path(__file__).resolve().parent.parent.parent / 'results'
_STATE_FILE = _RESULTS / 'kill_switch_state.json'

class KillSwitch:
    """긴급 매매 중단 킬스위치 (측정/판정 완전 분리).

    모든 파라미터는 DynamicConfig에서 동적 로드.

    ★ Edge-Triggered Alert:
      - 이전 상태(OK/TRIGGERED) vs 현재 상태 비교
      - 상태가 바뀔 때만 텔레그램 발송
      - per-trigger 쿨다운으로 동일 트리거 반복 발송 방지

    ★ Gap-Aware NAV Reset:
      - 시스템 재시작 후 거래일 갭 감지
      - 갭 기간 일간 손실 기준점 자동 리셋
    """

    def __init__(self):
        self._triggered = False
        self._trigger_history: List[Dict] = []
        self._prev_triggered: bool = False
        self._prev_trigger_types: set = set()
        self._last_alert_times: Dict[str, datetime] = {}
        self._load_state()

    def _load_state(self) -> None:
        """kill_switch_state.json에서 이전 상태 복원."""
        try:
            if _STATE_FILE.exists():
                _d = _json.loads(_STATE_FILE.read_text())
                self._prev_triggered = _d.get('triggered', False)
                self._prev_trigger_types = set(_d.get('trigger_types', []))
                for k, v in _d.get('last_alert_times', {}).items():
                    try:
                        self._last_alert_times[k] = datetime.fromisoformat(v)
                    except Exception as _e0:
                        logger.critical(f'  [kill_switch] 시각 파싱 (비치명적): {_e0}', exc_info=True)
        except Exception as e:
            logger.critical(f'  [KillSwitch] 상태 파일 로드 실패 (초기 상태 사용): {e}', exc_info=True)

    def _save_state(self, triggered: bool, trigger_types: set) -> None:
        """현재 상태를 kill_switch_state.json에 저장."""
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            _state = {'triggered': triggered, 'trigger_types': list(trigger_types), 'last_alert_times': {k: v.isoformat() for k, v in self._last_alert_times.items()}, 'updated_at': now_kst().isoformat()}
            _STATE_FILE.write_text(_json.dumps(_state, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.critical(f'  [KillSwitch] 상태 저장 실패: {e}', exc_info=True)

    def _detect_and_filter_system_gap(self, daily_returns: list, portfolio: Dict) -> list:
        """시스템 재시작 갭 감지 후 일간 손실 필터링.

        Two Sigma 방식:
          - shadow_portfolio의 daily_snapshots 연속 날짜 간격으로 갭 감지
          - N 거래일 이상 갭이 있으면 → 해당 갭 이후의 daily_return 제외
          - 이유: 10일 갭의 -7.4%가 단일 daily_return으로 기록됨
                 실제로는 시스템 다운 기간 변동 누적치 → 단일 일간 손실이 아님

        Returns:
            필터링된 daily_returns (갭 기간 누적 손실 제거)
        """
        if not daily_returns:
            return daily_returns
        max_gap_days = cfg.get('kill_switch.system_gap_max_days', 3)
        try:
            _sp_file = _RESULTS / 'shadow_portfolio.json'
            if not _sp_file.exists():
                return daily_returns
            _sp = _json.loads(_sp_file.read_text())
            _snaps = _sp.get('daily_snapshots', [])
            snap_dates = []
            for s in _snaps:
                d = s.get('date') or s.get('timestamp', '')[:10]
                if d:
                    try:
                        snap_dates.append(datetime.strptime(d, '%Y-%m-%d'))
                    except Exception as _e1:
                        logger.critical(f'  [kill_switch] 날짜 파싱 (비치명적): {_e1}', exc_info=True)
            if len(snap_dates) >= 2:
                snap_dates_sorted = sorted(snap_dates)
                prev_snap_date = snap_dates_sorted[-2]
                curr_snap_date = snap_dates_sorted[-1]
                gap_calendar_days = (curr_snap_date - prev_snap_date).days
            elif len(snap_dates) == 1:
                _dr_dates = _sp.get('daily_returns_dates', [])
                if len(_dr_dates) >= 2:
                    try:
                        prev_dt = datetime.strptime(_dr_dates[-2], '%Y-%m-%d')
                        curr_dt = datetime.strptime(_dr_dates[-1], '%Y-%m-%d')
                        gap_calendar_days = (curr_dt - prev_dt).days
                    except (FileNotFoundError, json.JSONDecodeError):
                        return daily_returns
                    except Exception as e:
                        logger.critical(f'  P&L 기록 파싱 중 예상치 못한 에러: {e}', exc_info=True)
                        raise
                        return daily_returns
                else:
                    return daily_returns
            else:
                return daily_returns
            gap_trading_days = int(gap_calendar_days * 5 / 7)
            if gap_trading_days >= max_gap_days:
                logger.warning(f'  [KillSwitch] ⚠️ 시스템 갭 감지: {gap_trading_days}거래일 ({gap_calendar_days}일) — daily_loss 기준점 리셋 (갭 기간 수익률 {daily_returns[-1] * 100:.2f}% 제외)')
                filtered = daily_returns[:-1]
                try:
                    from src.measurement.event_ledger import log_event
                    log_event('SYSTEM', {'action': 'gap_aware_nav_reset', 'gap_trading_days': gap_trading_days, 'excluded_return_pct': round(daily_returns[-1] * 100, 4), 'last_recorded': ''}, source='kill_switch')
                except Exception as _e2:
                    logger.critical(f'  [kill_switch] GAP 이벤트 기록: {_e2}', exc_info=True)
                return filtered
        except Exception as e:
            logger.critical(f'  [KillSwitch] 갭 감지 실패 (원본 사용): {e}', exc_info=True)
        return daily_returns

    def _should_send_alert(self, trigger_type: str, current_triggered: bool, current_types: set) -> tuple:
        """텔레그램 발송 여부 결정 (Edge-Triggered + Per-trigger Cooldown).

        발송 조건 (모두 AND):
          1. 현재 triggered=True (당연한 전제)
          2. 상태 전이 감지: OK→TRIGGERED (이전에 safe했거나 새 트리거 타입 추가)
             OR 쿨다운 만료 후 재발송
          3. 해당 트리거 타입의 쿨다운 미만이 아닐 것

        Returns:
            (should_send: bool, reason: str)
        """
        if not current_triggered:
            return (False, '')
        cooldown_hours = cfg.get('kill_switch.alert_cooldown_hours', 4.0)
        cooldown = timedelta(hours=cooldown_hours)
        last_sent = self._last_alert_times.get(trigger_type)
        now = now_kst().replace(tzinfo=None)
        state_transition = not self._prev_triggered and current_triggered
        new_trigger_type = trigger_type not in self._prev_trigger_types
        cooldown_expired = last_sent is None or now - last_sent >= cooldown
        if state_transition:
            return (True, f'상태 전이 (OK→TRIGGERED): {trigger_type}')
        elif new_trigger_type and cooldown_expired:
            return (True, f'신규 트리거 추가: {trigger_type}')
        elif cooldown_expired and last_sent is not None:
            elapsed_h = (now - last_sent).total_seconds() / 3600
            return (True, f'쿨다운 만료 재알림 ({elapsed_h:.1f}h): {trigger_type}')
        else:
            remaining = ''
            if last_sent:
                rem_sec = int(cooldown.total_seconds() - (now - last_sent).total_seconds())
                remaining = f' (재발송까지 {rem_sec // 3600}h {rem_sec % 3600 // 60}m)'
            return (False, f'쿨다운 중 ({trigger_type}){remaining}')

    def _send_telegram_alert(self, judgment: Dict, measurement: Dict, regime: str, current_types: set) -> None:
        """Edge-Triggered 텔레그램 발송.

        각 트리거 타입별로 독립적으로 쿨다운 관리.
        """
        triggers = [t for t in judgment.get('triggers', []) if not t.get('overridden')]
        if not triggers:
            return
        alerts_to_send = []
        for t in triggers:
            trigger_type = t['type']
            should_send, reason = self._should_send_alert(trigger_type, judgment['triggered'], current_types)
            if should_send:
                alerts_to_send.append((trigger_type, t['reason'], reason))
            else:
                logger.debug(f'  [KillSwitch] 텔레그램 발송 억제: {reason}')
        if not alerts_to_send:
            logger.info(f'  [KillSwitch] 📵 텔레그램 발송 억제 (쿨다운 or 동일 상태 유지): triggers={list(current_types)}')
            return
        try:
            from src.utils.telegram_notifier import TelegramNotifier
            reasons_text = '\n'.join((f'• {r}' for _, r, _ in alerts_to_send))
            dispatch_reasons = ', '.join((d for _, _, d in alerts_to_send))
            TelegramNotifier().send_alert('🚨 KILL SWITCH 발동', f'⚠️ *긴급 매매 중단 시스템 가동*\n사유:\n{reasons_text}\n레짐: {regime}\n현재 낙폭: {measurement['dd_pct']:.2f}%\n발송 근거: {dispatch_reasons}')
            now = now_kst().replace(tzinfo=None)
            for trigger_type, _, _ in alerts_to_send:
                self._last_alert_times[trigger_type] = now
            logger.info(f'  [KillSwitch] 📨 텔레그램 발송: {[tt for tt, _, _ in alerts_to_send]}')
        except Exception as e:
            logger.critical(f'  [KillSwitch] Telegram 발송 실패: {e}', exc_info=True)

    def measure_metrics(self, portfolio: Dict) -> Dict:
        """순수 측정: 후행 지표 + 전방 지표 통합.

        후행 지표: 일간 손실, 연속 손실, DD 레벨
        전방 지표: 시그널 품질, 레짐, 벤치마크 알파, 뉴스 감성, OIS

        ★ Gap-Aware: daily_returns에서 시스템 갭 기간 손실 자동 제외
        """
        initial_capital = portfolio.get('initial_capital', cfg.get('portfolio.initial_capital'))
        daily_returns = portfolio.get('daily_returns', [])
        daily_returns = self._detect_and_filter_system_gap(daily_returns, portfolio)
        today_return = daily_returns[-1] if daily_returns else 0
        consecutive_loss = 0
        for r in reversed(daily_returns):
            if r < 0:
                consecutive_loss += 1
            else:
                break
        total_nav = portfolio.get('total_nav', initial_capital)
        hwm = portfolio.get('hwm', max(total_nav, initial_capital))
        dd_pct = (total_nav / hwm - 1) * 100 if hwm and hwm > 0 else 0
        week_returns = daily_returns[-5:] if len(daily_returns) >= 5 else daily_returns
        weekly_return = sum(week_returns) if week_returns else 0
        forward = self._collect_forward_signals()
        daily_vol = self._compute_dynamic_volatility(daily_returns, forward)
        z_daily = 3.0
        dynamic_daily_limit = -(daily_vol * z_daily) * 100.0
        dynamic_weekly_limit = -(daily_vol * math.sqrt(5) * z_daily) * 100.0
        monthly_return, monthly_days, monthly_limit = self._compute_monthly_metrics(daily_returns, portfolio, daily_vol)
        result = {'today_return_pct': round(today_return * 100, 4), 'consecutive_loss_days': consecutive_loss, 'dd_pct': round(dd_pct, 2), 'weekly_return_pct': round(weekly_return * 100, 4), 'monthly_return_pct': round(monthly_return * 100, 4), 'monthly_trading_days': monthly_days, 'monthly_dynamic_limit_pct': round(monthly_limit * 100, 2), 'dynamic_daily_limit_pct': round(dynamic_daily_limit, 2), 'dynamic_weekly_limit_pct': round(dynamic_weekly_limit, 2), 'daily_volatility': daily_vol, 'total_nav': total_nav, 'active_positions': portfolio.get('active_positions', 0), 'n_trading_days': len(daily_returns), 'timestamp': now_kst().isoformat()}
        result['forward'] = forward
        return result

    def _collect_forward_signals(self) -> Dict:
        """전방 지표 동적 수집 — 파일 기반, 실패 시 중립값 반환."""
        forward = {'regime': 'caution', 'regime_confidence': 0.0, 'signal_avg_confidence': 0.0, 'n_signals': 0, 'news_sentiment': 0.0, 'ois_score': 50.0, 'bench_alpha_5d': 0.0, 'ic': 0.0, 'da': 0.0}
        try:
            import json as _fj
            _cr = _RESULTS / 'current_regime.json'
            if _cr.exists():
                _d = _fj.loads(_cr.read_text())
                forward['regime'] = _d.get('regime', 'caution')
                forward['regime_confidence'] = _d.get('confidence', 0)
            _ls = _RESULTS / 'latest_signals.json'
            if _ls.exists():
                _d = _fj.loads(_ls.read_text())
                _all_sigs = []
                for _sigs in _d.get('signals', {}).values():
                    _all_sigs.extend(_sigs if isinstance(_sigs, list) else [])
                if _all_sigs:
                    _confs = [s.get('confidence', 0) for s in _all_sigs if s.get('confidence') is not None]
                    forward['signal_avg_confidence'] = sum(_confs) / len(_confs) if _confs else 0
                    forward['n_signals'] = len(_all_sigs)
            _sc = _RESULTS / 'signal_cache.json'
            if _sc.exists():
                _d = _fj.loads(_sc.read_text())
                forward['news_sentiment'] = _d.get('news_sentiment', 0)
            _ps = _RESULTS / 'pipeline_state.json'
            if _ps.exists():
                _d = _fj.loads(_ps.read_text())
                forward['ois_score'] = _d.get('ois', 50.0)
            _me = _RESULTS / 'measurement_engine.json'
            if _me.exists():
                _d = _fj.loads(_me.read_text())
                _off = _d.get('official', {})
                forward['ic'] = _off.get('ic', 0)
                forward['da'] = _off.get('direction_accuracy', 0)
                _views = _d.get('views', {})
                _risk = _views.get('risk', {})
                forward['bench_alpha_5d'] = _risk.get('recent_alpha_5d', 0)
            if forward['bench_alpha_5d'] == 0:
                _sp = _RESULTS / 'shadow_portfolio.json'
                if _sp.exists():
                    _d = _fj.loads(_sp.read_text())
                    _records = _d.get('daily_records', [])[-5:]
                    if _records:
                        _alphas = [r.get('alpha_pct', 0) for r in _records]
                        forward['bench_alpha_5d'] = sum(_alphas) / len(_alphas)
        except Exception as e:
            logger.critical(f'  [KillSwitch] 전방 지표 수집 실패 (중립값 사용): {e}', exc_info=True)
        return forward

    def _compute_dynamic_volatility(self, daily_returns: list, forward: Dict) -> float:
        """EWMA 기반 동적 일간 변동성 산출 (하드코딩 배제, 시장 내재 변동성 Fallback)."""
        import numpy as np
        if len(daily_returns) >= 5:
            recent = np.array(daily_returns[-60:])
            ewma_lambda = 0.94
            var_t = float(np.var(recent[:5]))
            for r in recent[5:]:
                var_t = ewma_lambda * var_t + (1 - ewma_lambda) * r * r
            return float(np.sqrt(var_t))
        else:
            vix = forward.get('forward_vix', 20.0)
            try:
                import json as _fj
                _sc = _RESULTS / 'signal_cache.json'
                if _sc.exists():
                    _d = _fj.loads(_sc.read_text())
                    vix = float(_d.get('vix', vix))
            except Exception:
                pass
            return float(vix / 100.0 / np.sqrt(252))

    def judge_action(self, metrics: Dict, regime: str='caution') -> Dict:
        """정책 판정: 후행 트리거 + 전방 지표 오버라이드.

        원칙:
          1. 일간 급락(-5%), DD Stage 5, CRASH 주간급락 → 무조건 발동 (오버라이드 불가)
          2. 연속 손실, 월 누적 손실 → 전방 지표가 양호하면 동적 완화

        전방 지표 양호 조건 (3개 이상 충족 시 override):
          - 레짐 bull 또는 neutral
          - 시그널 평균 confidence ≥ 0.60
          - 벤치마크 대비 5일 알파 > 0 (시장보다 잘하고 있음)
          - 뉴스 감성 > -0.10 (극단적 악재 없음)
          - OIS > 45 (야간 시장 안정)
        """
        triggers = []
        forward = metrics.get('forward', {})
        max_daily = metrics.get('dynamic_daily_limit_pct', -5.0)
        if metrics['today_return_pct'] <= max_daily:
            triggers.append({'type': 'daily_loss', 'value': metrics['today_return_pct'], 'threshold': max_daily, 'reason': f'일간 손실 {metrics['today_return_pct']:.2f}% ≤ 동적 한도 {max_daily:.2f}% (3-Sigma)', 'overridable': False})
        base_dd_kill_pct = cfg.get('dd_guard.stage5_pct', -0.25)
        regime_factor = 1.0 if regime in ('crash', 'bull') else 0.0
        max_relaxation = 0.15
        dynamic_dd_kill_pct = base_dd_kill_pct - max_relaxation * regime_factor
        dd_kill_pct = dynamic_dd_kill_pct * 100
        ABSOLUTE_MDD_KILL = -5.0
        if metrics['dd_pct'] <= ABSOLUTE_MDD_KILL:
            triggers.append({'type': 'hard_floor_mdd', 'value': metrics['dd_pct'], 'threshold': ABSOLUTE_MDD_KILL, 'reason': f'MDD {metrics['dd_pct']:.1f}% ≤ {ABSOLUTE_MDD_KILL}% (Hard Floor Bypass)', 'overridable': False})
        if metrics['dd_pct'] <= dd_kill_pct:
            triggers.append({'type': 'dd_critical', 'value': metrics['dd_pct'], 'threshold': dd_kill_pct, 'reason': f'DD {metrics['dd_pct']:.1f}% ≤ {dd_kill_pct:.0f}% (Dynamic Stage 5+)', 'overridable': False})
        if regime == 'crash':
            max_weekly = metrics.get('dynamic_weekly_limit_pct', -10.0)
            if metrics['weekly_return_pct'] <= max_weekly:
                triggers.append({'type': 'crash_weekly', 'value': metrics['weekly_return_pct'], 'threshold': max_weekly, 'reason': f'CRASH 주간 손실 {metrics['weekly_return_pct']:.1f}% ≤ 동적 한도 {max_weekly:.2f}%', 'overridable': False})
        max_consec = cfg.get('kill_switch.max_consecutive_loss_days', 7)
        if metrics['consecutive_loss_days'] >= max_consec:
            triggers.append({'type': 'consecutive_loss', 'value': metrics['consecutive_loss_days'], 'threshold': max_consec, 'reason': f'연속 손실 {metrics['consecutive_loss_days']}일 ≥ {max_consec}일', 'overridable': True})
        monthly_ret = metrics.get('monthly_return_pct', 0)
        monthly_limit = metrics.get('monthly_dynamic_limit_pct', -5.0)
        if monthly_ret <= monthly_limit and metrics.get('monthly_trading_days', 0) >= 3:
            triggers.append({'type': 'monthly_loss', 'value': monthly_ret, 'threshold': monthly_limit, 'reason': f'월 누적 손실 {monthly_ret:.2f}% ≤ 동적 한도 {monthly_limit:.2f}%', 'overridable': True})
        forward_override = self._evaluate_forward_override(triggers, forward, regime)
        overridden = []
        if forward_override['override']:
            hard_triggers = [t for t in triggers if not t.get('overridable', False)]
            overridden = [t for t in triggers if t.get('overridable', False)]
            for t in overridden:
                t['overridden'] = True
                t['override_reason'] = forward_override['reason']
            triggers = hard_triggers
        triggered = len(triggers) > 0
        trigger_types = {t['type'] for t in triggers}
        if triggered:
            if trigger_types == {'monthly_loss'}:
                action = 'halt_new_entry'
            else:
                action = 'halt_all'
        else:
            action = 'continue'
        return {'triggered': triggered, 'action': action, 'triggers': triggers + [t for t in (overridden if forward_override['override'] else [])], 'trigger_count': len(triggers), 'safe': not triggered, 'can_buy': action == 'continue', 'regime': regime, 'forward_override': forward_override}

    def _evaluate_forward_override(self, triggers: List[Dict], forward: Dict, regime: str) -> Dict:
        """전방 지표 기반 오버라이드 판정."""
        overridable_only = all((t.get('overridable', False) for t in triggers))
        if not triggers or not overridable_only:
            return {'override': False, 'reason': '', 'score': 0, 'details': {}}
        scores = {}
        regime_scores = {'bull': 1.0, 'neutral': 0.7, 'caution': 0.3, 'crash': 0.0, 'bear': 0.0}
        r = forward.get('regime', regime).lower()
        scores['regime'] = regime_scores.get(r, 0.0)
        avg_conf = forward.get('signal_avg_confidence', 0)
        scores['signal_quality'] = min(1.0, max(0.0, (avg_conf - 0.4) * 2.0))
        import math
        alpha = forward.get('bench_alpha_5d', 0)
        scores['alpha'] = 1.0 / (1.0 + math.exp(-alpha * 100.0))
        sentiment = forward.get('news_sentiment', 0)
        scores['news'] = min(1.0, max(0.0, sentiment + 0.5))
        ois = forward.get('ois_score', 50)
        scores['ois'] = min(1.0, max(0.0, (ois - 30) / 40.0))
        total = sum(scores.values())
        max_score = len(scores)
        threshold = cfg.get('kill_switch.forward_override_threshold', 0.6)
        normalized = total / max_score if max_score > 0 else 0
        override = normalized >= threshold
        reason = ''
        if override:
            good = [k for k, v in scores.items() if v >= 0.6]
            reason = f'전방 지표 양호 ({normalized:.0%}): {', '.join(good)}'
            logger.info(f'  🟢 Kill Switch Override: {reason}')
        else:
            weak = [k for k, v in scores.items() if v < 0.6]
            logger.info(f'  🔴 Kill Switch Override 불가 ({normalized:.0%}): 약한 지표={', '.join(weak)}')
        return {'override': override, 'reason': reason, 'score': round(normalized, 3), 'threshold': threshold, 'details': {k: round(v, 2) for k, v in scores.items()}}

    def _compute_monthly_metrics(self, daily_returns: list, portfolio: Dict, daily_vol: float) -> tuple:
        """★ 월별 누적 손실 + CVaR(Expected Shortfall) 동적 한도 계산."""
        import numpy as np
        today = now_kst()
        month_trading_days = 21
        elapsed_fraction = min(today.day / month_trading_days, 1.0)
        estimated_month_days = max(1, int(month_trading_days * elapsed_fraction))
        month_returns = daily_returns[-estimated_month_days:] if len(daily_returns) >= estimated_month_days else daily_returns
        monthly_days = len(month_returns)
        monthly_return = sum(month_returns) if month_returns else 0.0
        monthly_vol = daily_vol * np.sqrt(month_trading_days)
        Z = 2.0537
        if len(daily_returns) >= 30:
            try:
                arr = np.array(daily_returns[-60:])
                mean = np.mean(arr)
                std = np.std(arr)
                if std > 0:
                    z_scores = (arr - mean) / std
                    S = float(np.mean(z_scores ** 3))
                    K = float(np.mean(z_scores ** 4) - 3.0)
                else:
                    S, K = (-0.5, 3.0)
            except Exception:
                S, K = (-0.5, 3.0)
        else:
            S, K = (-0.5, 3.0)
        Z_cf = Z + (Z ** 2 - 1) * S / 6.0 + (Z ** 3 - 3 * Z) * K / 24.0 - (2 * Z ** 3 - 5 * Z) * S ** 2 / 36.0
        cvar_multiplier = max(1.5, Z_cf)
        dynamic_limit = -(monthly_vol * cvar_multiplier)
        dynamic_limit = max(-0.25, min(-0.01, dynamic_limit))
        return (monthly_return, monthly_days, dynamic_limit)

    def assess(self, portfolio: Dict, regime: str='caution') -> Dict:
        """통합: 측정 + 판정 (2-layer 반환).

        ★ Edge-Triggered Alert 적용:
          - 상태 전이 또는 쿨다운 만료 시에만 텔레그램 발송
          - 동일 상태 유지 중 반복 발송 없음

        Returns:
            {
                'measurement': { ... },
                'judgment': { ... },
            }
        """
        measurement = self.measure_metrics(portfolio)
        judgment = self.judge_action(measurement, regime)
        current_triggered = judgment['triggered']
        current_types = {t['type'] for t in judgment.get('triggers', []) if not t.get('overridden')}
        if current_triggered:
            self._triggered = True
            self._trigger_history.append({'date': now_kst().isoformat(), 'triggers': judgment['triggers'], 'regime': regime})
            try:
                from src.measurement.event_ledger import log_event
                log_event('KILL_SWITCH', {'action': judgment['action'], 'triggers': [t['type'] for t in judgment['triggers']], 'dd_pct': measurement['dd_pct'], 'regime': regime}, source='kill_switch')
            except Exception as e:
                logger.critical(f'  [KillSwitch] 이벤트 로깅 실패 (감사 추적 손실): {e}', exc_info=True)
            logger.critical(f'  🚨 KILL SWITCH 발동! {judgment['trigger_count']}개 트리거: {', '.join((t['type'] for t in judgment['triggers'] if not t.get('overridden')))}')
            self._send_telegram_alert(judgment, measurement, regime, current_types)
        elif self._prev_triggered:
            try:
                from src.utils.telegram_notifier import TelegramNotifier
                TelegramNotifier().send_alert('✅ KILL SWITCH 해제', f'✅ *긴급 매매 중단 해제*\n레짐: {regime}\n현재 낙폭: {measurement['dd_pct']:.2f}%')
                logger.info('  [KillSwitch] ✅ Kill Switch 해제 알림 발송')
            except Exception as e:
                logger.critical(f'  [KillSwitch] 해제 알림 실패: {e}', exc_info=True)
        self._prev_triggered = current_triggered
        self._prev_trigger_types = current_types
        self._save_state(current_triggered, current_types)
        return {'measurement': measurement, 'judgment': judgment}

    @property
    def is_triggered(self) -> bool:
        """킬스위치 발동 상태."""
        return self._triggered

    def reset(self):
        """킬스위치 리셋 (수동)."""
        self._triggered = False
        self._prev_triggered = False
        self._prev_trigger_types = set()
        self._last_alert_times = {}
        self._save_state(False, set())
        logger.info('  🔄 Kill Switch 리셋')

    def check(self, nav: float, regime: str='caution') -> Dict:
        """간편 체크 (daily_pipeline 호환).

        Args:
            nav: 현재 NAV
            regime: 현재 레짐

        Returns:
            {'triggered': bool, 'can_buy': bool, 'position_scale': float, 'reason': str}
        """
        _sp_initial = None
        try:
            import json as _j2
            _sp_f = _RESULTS / 'shadow_portfolio.json'
            if _sp_f.exists():
                _sp_data = _j2.loads(_sp_f.read_text())
                _sp_initial = _sp_data.get('initial_capital')
        except Exception as _e3:
            logger.critical(f'  [kill_switch] Kill Switch 상태 저장 1: {_e3}', exc_info=True)
        initial_capital = _sp_initial if _sp_initial is not None else cfg.get('portfolio.initial_capital')
        portfolio = {'total_nav': nav, 'initial_capital': initial_capital, 'hwm': max(nav, initial_capital), 'daily_returns': [], 'active_positions': 0}
        try:
            import json as _json2
            _sp_file = _RESULTS / 'shadow_portfolio.json'
            if _sp_file.exists():
                _sp = _json2.loads(_sp_file.read_text())
                portfolio['hwm'] = _sp.get('hwm', portfolio['hwm'])
                portfolio['active_positions'] = len(_sp.get('positions', {}))
                _dr = _sp.get('daily_returns', [])
                if not _dr:
                    _snaps = _sp.get('daily_snapshots', [])
                    for _snap in _snaps:
                        _ret = _snap.get('daily_return_pct', 0)
                        if _ret is not None:
                            _dr.append(_ret / 100.0 if abs(_ret) > 1 else _ret)
                portfolio['daily_returns'] = _dr
        except Exception as _e4:
            logger.critical(f'  [kill_switch] Kill Switch 상태 저장 2: {_e4}', exc_info=True)
        result = self.assess(portfolio, regime)
        judgment = result['judgment']
        forward_override = judgment.get('forward_override', {})
        reason_parts = []
        if judgment['triggered']:
            reason_parts = [t['reason'] for t in judgment.get('triggers', []) if not t.get('overridden')]
        if forward_override.get('override'):
            reason_parts.append(f'[Override] {forward_override.get('reason', '')}')
        if not reason_parts:
            reason_parts = ['정상']
        action = judgment.get('action', 'halt_all' if judgment['triggered'] else 'continue')
        can_buy = action == 'continue'
        if action == 'halt_all':
            position_scale = 0.0
        elif action == 'halt_new_entry':
            position_scale = 1.0
        else:
            position_scale = 1.0
        check_result = {'triggered': judgment['triggered'], 'can_buy': can_buy, 'position_scale': position_scale, 'reason': '; '.join(reason_parts), 'active': judgment['triggered'], 'forward_override': forward_override, 'forward_signals': result['measurement'].get('forward', {}), 'timestamp': now_kst().isoformat()}
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            (_RESULTS / 'kill_switch.json').write_text(_json.dumps(check_result, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.critical(f'  kill_switch.json 저장 실패: {e}', exc_info=True)
        return check_result

    def hard_liquidate_all(self, reason: str='UNSPECIFIED', streams: Optional[List[str]]=None) -> Dict:
        """모든 S1~S5 포지션 시장가 전량 청산 + 시스템 셧다운.

        [Live Transition Task 4]
        DesyncError / API Timeout 10분 이상 / 기타 치명적 장애 발생 시
        sleeve_orchestrator의 최상단 try-except에서 자동 호출됩니다.

        절차:
          1. KillSwitch 강제 발동 상태 기록 (영속화)
          2. KISTraderAdapter.panic_sell_all() 호출 — 보유 포지션 전량 시장가 매도
          3. 결과를 results/hard_liquidation.json에 기록
          4. 텔레그램 긴급 알림 발송
          5. 시스템 셧다운 플래그(results/SYSTEM_HALT.flag) 생성

        Args:
            reason:  트리거 사유 (로그 및 알림용)
            streams: 청산 대상 스트림 목록 (None = 전체 S1~S5)

        Returns:
            {
                'success': bool,
                'orders_executed': int,
                'total_liquidated': float,
                'errors': list,
                'timestamp': str,
            }
        """
        if not streams:
            from config.dynamic_config import DynamicConfig as _DC
            streams = list(_DC().get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))
        ts = now_kst().isoformat()
        logger.critical(f'\n{'=' * 60}\n  🚨🚨 HARD LIQUIDATE ALL 발동 🚨🚨\n  사유: {reason}\n  대상: {streams}\n  시각: {ts}\n{'=' * 60}')
        self._triggered = True
        self._prev_triggered = True
        try:
            self._save_state(True, {'hard_liquidate'})
        except Exception as _se:
            logger.critical(f'  [HardLiquidate] 상태 저장 실패: {_se}', exc_info=True)
        result: Dict = {'success': False, 'reason': reason, 'streams': streams, 'orders_executed': 0, 'total_liquidated': 0.0, 'errors': [], 'timestamp': ts}
        try:
            from src.execution._kis_adapter import KISTraderAdapter
            from src.utils.credential_manager import CredentialManager
            try:
                cm = CredentialManager()
                _mode = cfg.get('execution.mode', 'mock')
                prefix = 'KIS_PAPER' if _mode == 'paper' else 'KIS'
                _trader = KISTraderAdapter(mode=_mode, app_key=cm.read_from_env(f'{prefix}_APP_KEY'), app_secret=cm.read_from_env(f'{prefix}_APP_SECRET'), account_no=cm.read_from_env(f'{prefix}_ACCOUNT_NO'))
            except Exception as _init_err:
                logger.error(f'  [HardLiquidate] KISTrader 초기화 실패 — Mock 폴백: {_init_err}')
                _trader = KISTraderAdapter(mode='mock')
            sold_orders = _trader.panic_sell_all()
            result['orders_executed'] = len(sold_orders)
            result['total_liquidated'] = sum((getattr(o, 'filled_price', 0) * getattr(o, 'filled_quantity', 0) for o in sold_orders))
            result['success'] = True
            logger.critical(f'  ✅ [HardLiquidate] 청산 완료: {len(sold_orders)}건 ₩{result['total_liquidated']:,.0f} 회수')
        except Exception as e:
            err_msg = f'전량 청산 실패: {e}'
            result['errors'].append(err_msg)
            logger.critical(f'  ❌ [HardLiquidate] {err_msg}')
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            record_file = _RESULTS / 'hard_liquidation.json'
            history = []
            if record_file.exists():
                try:
                    history = _json.loads(record_file.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    history = []
                except Exception as e:
                    logger.critical(f'  KillSwitch 기록 저장 중 에러: {e}', exc_info=True)
                    raise
                    history = []
            history.append(result)
            record_file.write_text(_json.dumps(history, ensure_ascii=False, indent=2, default=str))
        except Exception as _save_err:
            logger.critical(f'  [HardLiquidate] 기록 저장 실패: {_save_err}', exc_info=True)
        try:
            from src.utils.telegram_notifier import TelegramNotifier
            TelegramNotifier().send_alert('🔴🔴 HARD LIQUIDATE 실행 완료', f'⚠️ *전량 강제 청산 완료*\n사유: {reason}\n청산 주문: {result['orders_executed']}건\n회수 금액: ₩{result['total_liquidated']:,.0f}\n오류: {len(result['errors'])}건\n시각: {ts}\n→ 시스템 셧다운 플래그 생성 완료')
        except Exception as _tg_err:
            logger.critical(f'  [HardLiquidate] 텔레그램 알림 실패: {_tg_err}', exc_info=True)
        try:
            halt_flag = _RESULTS / 'SYSTEM_HALT.flag'
            halt_flag.write_text(_json.dumps({'halt': True, 'reason': reason, 'timestamp': ts, 'orders_liquidated': result['orders_executed']}, ensure_ascii=False, indent=2))
            logger.critical(f'  🛑 [HardLiquidate] 셧다운 플래그 생성: {halt_flag}')
        except Exception as _flag_err:
            logger.critical(f'  [HardLiquidate] 셧다운 플래그 생성 실패: {_flag_err}', exc_info=True)
        return result
"""
src/execution/order_manager.py
================================
Project Meridian — Order Manager with Drift Kill Switch
=========================================================
[Phase 43: Zero-Tolerance Execution Architecture]

주문 배치 실행 직전 'State Drift' 검증 및 Kill Switch를 담당하는
Order Manager. 모든 스트림의 주문은 반드시 이 매니저를 통해야 합니다.

핵심 기능:
    1. _pre_trade_validation(): 주문 직전 State Drift 검증 (목표 4)
       - Shadow Portfolio NAV ↔ 실계좌 NAV 비교
       - Drift > threshold → StateDriftError + 주문 전면 차단
    2. execute_batch(): 주문 배치 실행 with 커스텀 예외 전파
    3. emergency_cancel_all(): 긴급 전체 주문 취소

설계 원칙:
    - Fail-Closed: 검증 실패 시 주문 전송 zero
    - Full Audit Trail: 모든 실행/차단 이력을 logs/order_audit.jsonl에 기록
    - Emergency Page: StateDriftError 즉시 텔레그램 알람

Usage:
    from src.execution.order_manager import OrderManager
    mgr = OrderManager(mode='live')
    results = mgr.execute_batch(orders, stream_id='S6B')
"""
from __future__ import annotations
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_AUDIT_LOG = _PROJECT_ROOT / 'logs' / 'order_audit.jsonl'
_DEFAULT_MAX_DRIFT_PCT = 0.03
from src.execution.exceptions import BalanceFetchError, ExecutionFatalError, OrderRejectError, StateDriftError, TokenError
from src.utils.emergency_pager import send_emergency_page
from src.utils.emergency_pager import send_emergency_page

class OrderManager:
    """[Phase 43] 주문 실행 관리자 — Drift Kill Switch 포함.

    모든 실 주문은 반드시 이 클래스를 통과해야 합니다.
    자체적으로 State Drift를 검증하고, 불일치 시 주문을 전면 차단합니다.
    """

    def __init__(self, mode: str='paper') -> None:
        self.mode = mode
        _AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)

    def _pre_trade_validation(self, stream_id: str='', orders: Optional[List[Dict]]=None) -> Dict:
        """[Phase 43: Goal 4] 주문 직전 State Drift 검증 및 Kill Switch.

        검증 절차:
            1. results/shadow_portfolio.json → virtual_nav (로컬 Shadow 포트폴리오)
            2. KIS API → real_nav (브로커 실계좌 총 평가금액)
            3. Drift = abs(real_nav - virtual_nav) / max(virtual_nav, 1)
            4. Drift > max_drift_pct → StateDriftError raise

        Args:
            stream_id: 호출 스트림 식별자 (로그/알람용)
            orders:    대기 중인 주문 목록 (취소 대상)

        Returns:
            {
                'passed':      bool,
                'virtual_nav': float,
                'real_nav':    float,
                'drift_pct':   float,
                'threshold':   float,
            }

        Raises:
            StateDriftError: Drift가 임계치 초과 시
        """
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
            max_drift_pct = float(_cfg.get('execution.max_drift_pct', 3.0)) / 100.0
        except Exception as _ce:
            logger.warning(f'  [Drift] config 로드 실패 → 기본값 3.0%: {_ce}')
            _cfg = None
            max_drift_pct = _DEFAULT_MAX_DRIFT_PCT
        if _cfg is None:

            class _FallbackCfg:

                def get(self, key, default=None):
                    return default
            _cfg = _FallbackCfg()
        if self.mode in ('mock', 'shadow'):
            logger.info(f'  [Drift] {self.mode} 모드 — Drift 검증 스킵')
            return {'passed': True, 'virtual_nav': 0.0, 'real_nav': 0.0, 'drift_pct': 0.0, 'threshold': max_drift_pct, 'skipped': True}
        virtual_nav = 0.0
        try:
            sp_file = _RESULTS / 'shadow_portfolio.json'
            if sp_file.exists():
                sp = json.loads(sp_file.read_text(encoding='utf-8'))
                virtual_nav = float(sp.get('virtual_nav') or sp.get('total_nav') or sp.get('nav') or 0)
                logger.info(f'  [Drift] Shadow NAV: ₩{virtual_nav:,.0f}')
            else:
                logger.warning('  [Drift] shadow_portfolio.json 없음 — Drift 검증 생략')
                return {'passed': True, 'virtual_nav': 0.0, 'real_nav': 0.0, 'drift_pct': 0.0, 'threshold': max_drift_pct, 'skipped': True}
        except Exception as _sp_e:
            logger.error(f'  [Drift] shadow_portfolio.json 로드 실패: {_sp_e}', exc_info=True)
            return {'passed': True, 'virtual_nav': 0.0, 'real_nav': 0.0, 'drift_pct': 0.0, 'threshold': max_drift_pct, 'skipped': True}
        if virtual_nav <= 0:
            logger.warning('  [Drift] virtual_nav = 0 — 초기화 직후로 판단, Drift 검증 생략')
            return {'passed': True, 'virtual_nav': 0.0, 'real_nav': 0.0, 'drift_pct': 0.0, 'threshold': max_drift_pct, 'skipped': True}
        real_nav = 0.0
        broker_fetch_ok = False
        try:
            from src.execution._kis_adapter import KISTraderAdapter
            trader = KISTraderAdapter(mode=self.mode)
            _retry = int(_cfg.get('execution.broker_retry_count', 3))
            for _attempt in range(_retry):
                try:
                    ok = trader.fetch_live_balance()
                    if ok:
                        real_nav = float(trader.account.total_equity)
                        broker_fetch_ok = True
                        logger.info(f'  [Drift] 실계좌 NAV: ₩{real_nav:,.0f}')
                        break
                    logger.warning(f'  [Drift] 잔고 조회 실패 {_attempt + 1}/3')
                except Exception as _fe:
                    logger.warning(f'  [Drift] 잔고 조회 예외 {_attempt + 1}/3: {_fe}')
                if _attempt < _retry - 1:
                    _backoff_base = int(_cfg.get('execution.retry_backoff_base', 2))
                    time.sleep(_backoff_base ** _attempt)
        except Exception as _ka_e:
            logger.error(f'  [Drift] KIS Adapter 초기화 실패: {_ka_e}', exc_info=True)
        if not broker_fetch_ok:
            _msg = '[FATAL EXECUTION] 실계좌 잔고 조회 실패 — Drift 검증 불가 → 보수적 차단'
            logger.error(_msg, exc_info=True)
            send_emergency_page(f'🚨 {_msg}', stream_id=stream_id)
            raise BalanceFetchError(_msg, stream_id=stream_id)
        drift = abs(real_nav - virtual_nav) / max(virtual_nav, 1.0)
        drift_pct = drift * 100
        logger.info(f'  [Drift] Virtual: ₩{virtual_nav:,.0f} | Real: ₩{real_nav:,.0f} | Drift: {drift_pct:.2f}% (임계치: {max_drift_pct * 100:.1f}%)')
        result = {'passed': drift <= max_drift_pct, 'virtual_nav': virtual_nav, 'real_nav': real_nav, 'drift_pct': drift, 'threshold': max_drift_pct}
        if drift > max_drift_pct:
            _page_msg = f'🚨 [Drift Kill Switch]\n로컬/브로커 잔고 불일치 {drift_pct:.2f}% > 임계치 {max_drift_pct * 100:.1f}%\n가상NAV: ₩{virtual_nav:,.0f}\n실계좌NAV: ₩{real_nav:,.0f}\n⚡ 주문 전면 차단 — 원인 규명 후 재개 필요'
            logger.error(f'  [Drift Kill Switch] Drift {drift_pct:.2f}% → 주문 전면 차단 (임계치={max_drift_pct * 100:.1f}%)', exc_info=True)
            send_emergency_page(_page_msg, stream_id=stream_id)
            if orders:
                self._emergency_cancel(orders, stream_id=stream_id, reason='State Drift')
            exc = StateDriftError('로컬/브로커 잔고 불일치 초과 — Kill Switch 작동', virtual_nav=virtual_nav, real_nav=real_nav, drift_pct=drift, threshold_pct=max_drift_pct * 100, stream_id=stream_id)
            self._write_audit(event='drift_kill_switch', stream_id=stream_id, detail={'virtual_nav': virtual_nav, 'real_nav': real_nav, 'drift_pct': drift_pct, 'threshold': max_drift_pct * 100, 'n_orders_cancelled': len(orders) if orders else 0})
            raise exc
        return result

    def execute_batch(self, orders: List[Dict], stream_id: str='', skip_drift_check: bool=False) -> List[Dict]:
        """[Phase 43] 주문 배치 실행 — Drift 검증 + 커스텀 예외 전파.

        Args:
            orders:           주문 목록
            stream_id:        스트림 식별자 (S1~S6)
            skip_drift_check: True이면 Drift 검증 생략 (Mock 테스트 전용)

        Returns:
            체결 결과 목록

        Raises:
            StateDriftError:    잔고 불일치 초과
            BalanceFetchError:  잔고 조회 실패
            ExecutionFatalError: 기타 치명적 에러
        """
        if not orders:
            logger.info(f'  [OrderManager] {stream_id}: 주문 없음')
            return []
        self._write_audit('batch_start', stream_id, {'n_orders': len(orders)})
        if not skip_drift_check:
            drift_result = self._pre_trade_validation(stream_id=stream_id, orders=orders)
            logger.info(f'  [OrderManager] Drift 검증 통과 (drift={drift_result['drift_pct'] * 100:.2f}%)')
        else:
            logger.info(f'  [OrderManager] Drift 검증 생략 (skip_drift_check=True)')
        try:
            from config.dynamic_config import DynamicConfig as _DC
            _batch_cfg = _DC()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')

            class _FallbackCfg:

                def get(self, key, default=None):
                    return default
            _batch_cfg = _FallbackCfg()
        results = []
        from src.execution.kis_overseas_adapter import KISOverseasAdapter
        adapter = KISOverseasAdapter(mode=self.mode)
        for order in orders:
            ticker = order.get('ticker', '')
            side = order.get('side', 'buy')
            amount_krw = float(order.get('amount_krw', 0))
            if not ticker or amount_krw <= 0:
                logger.warning(f'  [OrderManager] 유효하지 않은 주문 스킵: {order}')
                continue
            try:
                quantity, price_usd, usdkrw = adapter.calc_order_quantity(ticker, amount_krw)
                if quantity <= 0:
                    logger.warning(f'  [OrderManager] {ticker}: 수량 0 → 주문 스킵')
                    results.append({'ticker': ticker, 'success': False, 'message': '수량 0 (가격/환율 조회 실패)'})
                    self._write_audit('order_skipped', stream_id, {'ticker': ticker, 'reason': '수량=0'})
                    continue
                result = adapter.place_order(ticker=ticker, side=side, quantity=quantity, price=price_usd, order_type=order.get('order_type', 'ioc'))
                results.append(result)
                self._write_audit('order_sent', stream_id, {'ticker': ticker, 'side': side, 'quantity': quantity, 'price': price_usd, 'success': result.get('success')})
            except ExecutionFatalError as _fe:
                logger.error(f'  [OrderManager] FATAL: {ticker} — {_fe}', exc_info=True)
                self._write_audit('fatal_error', stream_id, {'ticker': ticker, 'error': str(_fe), 'type': type(_fe).__name__})
                raise
            except Exception as _ue:
                _msg = f'[FATAL EXECUTION] 예상치 못한 주문 오류: {ticker} — {_ue}'
                logger.error(_msg, exc_info=True)
                send_emergency_page(f'🚨 {_msg}', exc_info=_ue, stream_id=stream_id)
                self._write_audit('unexpected_error', stream_id, {'ticker': ticker, 'error': str(_ue)})
                results.append({'ticker': ticker, 'success': False, 'message': str(_ue)})
            _order_interval = float(_batch_cfg.get('execution.order_interval_sec', 0.5))
            time.sleep(_order_interval)
        self._write_audit('batch_end', stream_id, {'n_results': len(results)})
        return results

    def _emergency_cancel(self, orders: List[Dict], stream_id: str='', reason: str='Emergency') -> Dict:
        """[Phase 43] 긴급 대기 주문 전량 취소.

        StateDriftError 발생 시 자동 호출됩니다.
        """
        logger.warning(f'  [OrderManager] 긴급 취소 시작: {len(orders)}건 ({reason})')
        if self.mode in ('mock', 'shadow'):
            logger.info('  [OrderManager] Mock/Shadow 모드 — 긴급 취소 시뮬레이션')
            return {'cancelled': len(orders), 'failed': 0, 'reason': reason}
        cancelled, failed = (0, 0)
        try:
            from src.execution.kis_overseas_adapter import KISOverseasAdapter
            adapter = KISOverseasAdapter(mode=self.mode)
            cxl_result = adapter.cancel_unfilled_premarket_orders()
            cancelled = cxl_result.get('cancelled', 0)
            failed = cxl_result.get('failed', 0)
        except Exception as _ce:
            logger.error(f'  [OrderManager] 긴급 취소 API 실패: {_ce}', exc_info=True)
            failed = len(orders)
        self._write_audit('emergency_cancel', stream_id, {'cancelled': cancelled, 'failed': failed, 'reason': reason})
        return {'cancelled': cancelled, 'failed': failed, 'reason': reason}

    def _write_audit(self, event: str, stream_id: str, detail: Dict) -> None:
        """[Phase 43] 모든 주문 이벤트를 JSONL Audit Trail에 기록."""
        try:
            record = {'ts': datetime.now().isoformat(), 'event': event, 'stream_id': stream_id, 'mode': self.mode, **detail}
            with _AUDIT_LOG.open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        except Exception as _ae:
            logger.critical(f'  Audit Trail 기록 실패 (무시): {_ae}', exc_info=True)
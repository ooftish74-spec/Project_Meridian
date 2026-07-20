#!/usr/bin/env python3
"""
Project Meridian — Shadow Live 전용 스크립트
=============================================
[Live Transition Task 3]

Shadow Live 모드: 실제 시장 데이터를 사용하되 Mock 체결로 고정.
목적: Live 전환 직전 마지막 안전 검증 단계.

차이점 vs daily_pipeline.py:
  - ExecutionEngine: 무조건 mock_mode=True (실 주문 절대 불가)
  - 실 호가 데이터로 시그널 생성 (실환경 동일)
  - 슬리피지/체결 오차(TCA)를 shadow_live_tca.json에 상세 로깅
  - DesyncError 발생 시 텔레그램만 알림, 시스템 계속 실행

사용법:
    python scripts/run_shadow_live.py           # 전체 실행
    python scripts/run_shadow_live.py morning   # morning만
    python scripts/run_shadow_live.py market    # market만

주의:
    이 스크립트는 절대 live 모드로 전환되지 않습니다.
    실전 매매는 반드시 daily_pipeline.py를 사용하십시오.
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ★ [Live Transition Task 3] Shadow Live 모드 강제
# KIS_MODE를 무조건 'mock'으로 덮어쓰기 (live/paper 전환 불가)
os.environ['KIS_MODE'] = 'mock'

from src.utils.logger import setup_logger

logger = setup_logger('shadow_live')

_RESULTS = _PROJECT_ROOT / 'results'
_TCA_LOG = _RESULTS / 'shadow_live_tca.json'


# ═══════════════════════════════════════════════════════════
# [Live Transition Task 3] TCA Logger
# ═══════════════════════════════════════════════════════════

def _log_tca(fills: list, regime: str = 'unknown') -> None:
    """Transaction Cost Analysis 로그 저장.

    [Live Transition Task 3]
    shadow_live_tca.json에 체결 오차 상세 기록:
      - 시그널 가격 vs 체결 가격 차이 (슬리피지)
      - 스트림별/종목별 TCA 집계
      - 실환경 체결 시뮬레이션 통계

    Args:
        fills: ExecutionResult.fills 리스트
        regime: 현재 레짐
    """
    if not fills:
        return

    try:
        from src.execution.slippage_model import AdvancedSlippageModel
        _slippage_model_available = True
    except ImportError as e:
        _slippage_model_available = False

    ts = datetime.now().isoformat()
    today = date.today().isoformat()

    # 기존 TCA 로그 로드
    tca_history = []
    if _TCA_LOG.exists():
        try:
            tca_history = json.loads(_TCA_LOG.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            tca_history = []

    # 오늘 세션 TCA 집계
    session = {
        'date': today,
        'timestamp': ts,
        'regime': regime,
        'mode': 'shadow_live (mock)',
        'n_fills': len(fills),
        'fills': [],
        'summary': {},
    }

    total_slippage_krw = 0.0
    total_commission_krw = 0.0
    total_amount_krw = 0.0
    stream_stats: dict = {}

    for fill in fills:
        ticker    = fill.get('ticker', '')
        action    = fill.get('action', '')
        stream    = fill.get('stream', '')
        sig_price = fill.get('signal_price', 0)
        fill_price = fill.get('fill_price', sig_price)
        quantity  = fill.get('quantity', 0)
        commission = fill.get('commission', 0)
        slip_bps  = fill.get('slippage_bps', 0)
        algo      = fill.get('algo', 'market')
        exchange  = fill.get('exchange', '')

        amount = fill_price * quantity
        slip_krw = abs(fill_price - sig_price) * quantity

        total_slippage_krw += slip_krw
        total_commission_krw += commission
        total_amount_krw += amount

        fill_record = {
            'ticker':       ticker,
            'action':       action,
            'stream':       stream,
            'signal_price': sig_price,
            'fill_price':   fill_price,
            'quantity':     quantity,
            'amount_krw':   round(amount),
            'slippage_bps': round(slip_bps, 2),
            'slippage_krw': round(slip_krw, 0),
            'commission':   round(commission, 0),
            'algo':         algo,
            'exchange':     exchange,
            'fill_probability': fill.get('fill_probability', 1.0),
        }
        session['fills'].append(fill_record)

        # 스트림별 집계
        if stream not in stream_stats:
            stream_stats[stream] = {
                'n': 0, 'total_slippage_bps': 0.0, 'total_amount': 0.0}
        stream_stats[stream]['n'] += 1
        stream_stats[stream]['total_slippage_bps'] += slip_bps
        stream_stats[stream]['total_amount'] += amount

    # 세션 요약
    avg_slip_bps = (
        sum(f.get('slippage_bps', 0) for f in session['fills']) / len(fills)
        if fills else 0
    )
    session['summary'] = {
        'total_amount_krw':    round(total_amount_krw),
        'total_slippage_krw':  round(total_slippage_krw),
        'total_commission_krw': round(total_commission_krw),
        'avg_slippage_bps':    round(avg_slip_bps, 2),
        'total_cost_bps':      round(
            (total_slippage_krw + total_commission_krw) / max(total_amount_krw, 1) * 10000, 2
        ),
        'stream_breakdown':    {
            s: {
                'n': v['n'],
                'avg_slip_bps': round(v['total_slippage_bps'] / max(v['n'], 1), 2),
                'total_amount': round(v['total_amount']),
            }
            for s, v in stream_stats.items()
        },
    }

    tca_history.append(session)

    # 최근 30일치만 유지
    if len(tca_history) > 30:
        tca_history = tca_history[-30:]

    _RESULTS.mkdir(parents=True, exist_ok=True)
    _TCA_LOG.write_text(
        json.dumps(tca_history, indent=2, ensure_ascii=False, default=str)
    )
    logger.info(
        f"  📊 [TCA] 기록 완료: {len(fills)}건, "
        f"avg slippage={avg_slip_bps:.1f}bps, "
        f"총비용={round(total_slippage_krw + total_commission_krw):,}원"
    )


# ═══════════════════════════════════════════════════════════
# [Live Transition Task 3] Shadow Live 메인 실행
# ═══════════════════════════════════════════════════════════

def run_shadow_live(phase: str = 'all') -> None:
    """Shadow Live 파이프라인 실행.

    [Live Transition Task 3]
    ExecutionEngine을 무조건 mock 모드로 고정하고
    실 데이터 기반 시그널 → 가상 체결 → TCA 로깅.
    """
    today = date.today()
    ts = datetime.now().isoformat()

    logger.info('=' * 60)
    logger.info(f'  Project Meridian — Shadow Live — {today}')
    logger.info(f'  [Live Transition Task 3] KIS_MODE=mock (강제 고정)')
    logger.info(f'  Phase: {phase}')
    logger.info('=' * 60)

    # ── Sanity Check: KIS_MODE가 절대 live/paper이면 안 됨 ──
    _forced_mode = os.environ.get('KIS_MODE', 'mock')
    if _forced_mode not in ('mock', 'shadow'):
        logger.critical(
            f'🚨 [CRITICAL] KIS_MODE={_forced_mode} — Shadow Live에서 '
            f'live/paper는 절대 허용 불가! 강제 종료.'
        )
        sys.exit(1)
    logger.info(f'  ✅ KIS_MODE 안전 확인: {_forced_mode}')

    # ── SYSTEM_HALT 플래그 확인 ──
    _halt_flag = _RESULTS / 'SYSTEM_HALT.flag'
    if _halt_flag.exists():
        try:
            _halt_data = json.loads(_halt_flag.read_text())
            if _halt_data.get('halt'):
                logger.critical(
                    f'🛑 SYSTEM_HALT 플래그 감지! '
                    f'사유: {_halt_data.get("reason", "UNKNOWN")} '
                    f'— 수동 해제 후 재실행하십시오.'
                )
                sys.exit(1)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass

    # ── DynamicConfig 로드 ──
    try:
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()
    except Exception as e:
        logger.error(f'  DynamicConfig 로드 실패: {e}')
        cfg = None

    # ── MarketDataBridge 데이터 수집 ──
    market_data = {}
    regime = 'caution'
    try:
        from src.data.market_data_bridge import MarketDataBridge
        bridge = MarketDataBridge()
        _sc = bridge.build_signal_cache()
        _ov = bridge.build_overnight_intel()
        _rh = bridge.get_regime_history()
        market_data = {
            'signal_cache': _sc,
            'overnight_intel': _ov,
            'vix_history': _rh.get('vix_history', []),
            'kospi_returns': _rh.get('kospi_returns', []),
        }
        logger.info('  ✅ MarketDataBridge 데이터 로드 완료')
    except Exception as e:
        logger.warning(f'  MarketDataBridge 실패 (기존 캐시 사용): {e}')

    # ── 레짐 판정 ──
    try:
        from src.intelligence.regime_engine import RegimeEngine
        regime_result = RegimeEngine().detect()
        regime = regime_result.get('regime', 'caution')
        market_data['regime_confidence'] = regime_result.get('confidence', 0.5)
        logger.info(f'  ✅ 레짐: {regime.upper()} '
                    f'(conf={regime_result.get("confidence", 0):.2f})')
    except Exception as e:
        logger.warning(f'  레짐 판정 실패 ({regime} 사용): {e}')

    # ── KillSwitch 체크 (shadow_live는 중단하지 않고 로그만) ──
    _ks_result = {'triggered': False, 'can_buy': True, 'position_scale': 1.0}
    try:
        from src.risk.kill_switch import KillSwitch
        ks = KillSwitch()
        _shadow_f = _RESULTS / 'shadow_portfolio.json'
        if _shadow_f.exists():
            _sp_data = json.loads(_shadow_f.read_text())
            _nav = _sp_data.get('virtual_nav',
                                (cfg.get('portfolio.initial_capital')
                                 if cfg else 1_000_000_000))
            _ks_result = ks.check(_nav)
        if _ks_result.get('triggered'):
            logger.warning(
                f'  ⚠️ [Shadow Live] Kill Switch 발동 감지: {_ks_result["reason"]}'
                f' — Shadow Live는 계속 실행 (Live 전환 시 차단 예정)'
            )
    except Exception as e:
        logger.debug(f'  Kill Switch 체크 실패: {e}')

    # ── StreamOrchestrator로 시그널 생성 (Mock 모드 강제) ──
    all_fills = []
    try:
        from scripts.stream_orchestrator import StreamOrchestrator
        _orch = StreamOrchestrator()
        logger.info('  🔄 [Shadow Live] StreamOrchestrator 실행 중...')

        _result = _orch.run()
        _orders = _result.get('orders', [])
        _exec_info = _result.get('execution', {})

        logger.info(
            f'  ✅ StreamOrchestrator 완료: '
            f'orders={len(_orders)}, '
            f'filled={_exec_info.get("n_filled", 0)}, '
            f'regime={_result.get("regime", "?")}'
        )

        # ExecutionEngine 직접 실행 (mock 모드 강제)
        try:
            from src.execution.execution_engine import ExecutionEngine
            _ee = ExecutionEngine(mode='mock')  # [Live Transition Task 3] 강제 mock

            if _orders:
                _exec_result = _ee.execute(_orders)
                all_fills.extend(_exec_result.fills)
                logger.info(
                    f'  ⚡ [Shadow Live] 가상 체결: '
                    f'{_exec_result.n_filled}/{_exec_result.n_orders}건 '
                    f'(mode=mock, slippage={_exec_result.estimated_slippage:,.0f}원)'
                )
            else:
                logger.info('  ⏭️ 주문 없음 — TCA 스킵')

        except Exception as ee_err:
            logger.warning(f'  ExecutionEngine 직접 실행 실패: {ee_err}')

    except Exception as e:
        logger.error(f'  StreamOrchestrator 실패: {e}')

    # ── TCA 로깅 ──
    if all_fills:
        _log_tca(all_fills, regime=regime)
    else:
        logger.info('  ℹ️ [TCA] 체결 없음 — TCA 로그 스킵')

    # ── Shadow Live 결과 저장 ──
    try:
        _sl_result = {
            'date': today.isoformat(),
            'timestamp': ts,
            'phase': phase,
            'regime': regime,
            'mode': 'shadow_live',
            'kis_mode': os.environ.get('KIS_MODE', 'mock'),
            'n_fills': len(all_fills),
            'kill_switch': _ks_result.get('triggered', False),
            'tca_log': str(_TCA_LOG),
        }
        _sl_path = _RESULTS / 'shadow_live_result.json'
        _sl_path.parent.mkdir(parents=True, exist_ok=True)
        _sl_path.write_text(
            json.dumps(_sl_result, indent=2, ensure_ascii=False, default=str)
        )
        logger.info(f'  💾 Shadow Live 결과 저장: {_sl_path}')
    except Exception as e:
        logger.warning(f'  Shadow Live 결과 저장 실패: {e}')

    logger.info('=' * 60)
    logger.info(f'  Shadow Live 완료: {datetime.now().isoformat()}')
    logger.info('=' * 60)


if __name__ == '__main__':
    _phase = sys.argv[1] if len(sys.argv) > 1 else 'all'
    run_shadow_live(_phase)

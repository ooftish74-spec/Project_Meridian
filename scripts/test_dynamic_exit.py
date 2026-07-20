#!/usr/bin/env python3
"""
Fully Dynamic Exit Algorithm — 단위 테스트
==========================================

5가지 시나리오를 Mock 데이터로 검증:
  테스트 1: ATR 기반 샹들리에 드롭 — 저변동성 vs 고변동성 종목 비교
  테스트 2: S2 Catastrophic Stop — 고점 대비 ATR×4 하락 시 즉각 청산
  테스트 3: Scale-out 50% — 수익 30%+ 달성 시 절반 청산 + Runner 플래그
  테스트 4: Runner 상태 관리 — scaled_out=True 포지션의 TP=None + 트레일링 적용
  테스트 5: ATR Fallback — pykrx/parquet 없을 때 portfolio_vol proxy 사용

Usage:
    python3 scripts/test_dynamic_exit.py
"""

import sys
import logging
import pathlib
from unittest.mock import patch, MagicMock
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format='%(message)s')

PASS = 0
FAIL = 0

def _check(name: str, condition: bool, detail: str = ''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  ✅ PASS: {name}' + (f' — {detail}' if detail else ''))
    else:
        FAIL += 1
        print(f'  ❌ FAIL: {name}' + (f' — {detail}' if detail else ''))


# ══════════════════════════════════════════════════════════
# 테스트 1: ATR 샹들리에 드롭 — 저/고 변동성 비교
# ══════════════════════════════════════════════════════════
print('\n=== 테스트 1: ATR 샹들리에 드롭 (삼성전자 vs 알테오젠) ===')

try:
    from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator

    evaluator = DynamicExitEvaluator()

    # Mock pykrx 반환값 (삼성전자: ATR~1%, 알테오젠: ATR~4%)
    def _make_ohlcv_df(atr_pct: float, close: float = 100.0, n: int = 20):
        import pandas as pd
        import numpy as np
        dates = pd.date_range('2026-01-01', periods=n, freq='B')
        rng = np.random.default_rng(42)
        closes = close + rng.normal(0, close * atr_pct, n).cumsum()
        highs  = closes + close * atr_pct * 0.6
        lows   = closes - close * atr_pct * 0.6
        df = pd.DataFrame({
            '고가': highs, '저가': lows, '종가': closes,
        }, index=dates)
        return df

    # 삼성전자: ATR≈1%
    samsung_df  = _make_ohlcv_df(0.01, close=75000)
    # 알테오젠: ATR≈4%
    alteogen_df = _make_ohlcv_df(0.04, close=120000)

    thresholds_bull = {
        'portfolio_vol': 0.02, 'vix': 18.0, 'vix_scale': 1.0,
        'sl_multiplier': 3.0, 'regime': 'bull',
    }

    with patch('src.streams.s4_advisory.dynamic_exit.pykrx_stock'
               if hasattr(sys, '_mocked') else
               'pykrx.stock.get_market_ohlcv_by_date', side_effect=[samsung_df, alteogen_df]):
        try:
            pos_samsung  = {'ticker': '005930', 'pnl_pct': 18.0, 'peak_pnl_pct': 20.0}
            pos_alteogen = {'ticker': '196170', 'pnl_pct': 18.0, 'peak_pnl_pct': 20.0}
            evaluator._atr_cache = {}

            with patch('pykrx.stock.get_market_ohlcv_by_date') as mock_pykrx:
                mock_pykrx.side_effect = [samsung_df, alteogen_df]
                atr_samsung  = evaluator._compute_ticker_atr(pos_samsung,  thresholds_bull)
                atr_alteogen = evaluator._compute_ticker_atr(pos_alteogen, thresholds_bull)

            mult = evaluator._compute_chandelier_mult(thresholds_bull, 'bull')
            drop_samsung  = atr_samsung  * mult * 100
            drop_alteogen = atr_alteogen * mult * 100

            _check('삼성전자 ATR < 알테오젠 ATR (저변동성)',
                   atr_samsung < atr_alteogen,
                   f'삼성={atr_samsung*100:.2f}% vs 알테오젠={atr_alteogen*100:.2f}%')
            _check('샹들리에 드롭: 삼성 < 알테오젠 (타이트 vs 넓음)',
                   drop_samsung < drop_alteogen,
                   f'삼성={drop_samsung:.2f}% vs 알테오젠={drop_alteogen:.2f}%')
            _check('Bull 레짐 chandelier_mult = 3.0',
                   abs(mult - 3.0) < 0.01, f'mult={mult}')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _check('pykrx mock ATR 계산', False, str(e))

    # ATR Fallback 경로 검증 (pykrx 실패 → vol_proxy)
    evaluator._atr_cache = {}
    with patch('pykrx.stock.get_market_ohlcv_by_date', side_effect=Exception('timeout')):
        fallback_thresh = {'portfolio_vol': 0.03}
        pos_fb = {'ticker': 'TEST', 'pnl_pct': 5.0}
        atr_fb = evaluator._compute_ticker_atr(pos_fb, fallback_thresh)
        expected = 0.03 * 1.5  # vol × proxy_factor
        _check('ATR Fallback: portfolio_vol proxy 적용',
               abs(atr_fb - expected) < 0.01,
               f'expected={expected*100:.2f}%, got={atr_fb*100:.2f}%')

except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _check('테스트 1 로드', False, str(e))


# ══════════════════════════════════════════════════════════
# 테스트 2: S2 Catastrophic Stop
# ══════════════════════════════════════════════════════════
print('\n=== 테스트 2: S2 Catastrophic Stop (고점 대비 ATR×4 하락) ===')

try:
    evaluator2 = DynamicExitEvaluator()
    thresholds_cat = {'portfolio_vol': 0.02, 'vix': 18.0}

    # ATR = 2% 가정 → catastrophic threshold = 2% × 4 = 8%
    # 고점 +20%, 현재 +10% → 하락 10% > 8% → 청산
    pos_s2_cat = {
        'ticker': 'S2TEST', 'stream_id': 'S2',
        'pnl_pct': 10.0, 'peak_pnl_pct': 20.0,
    }
    evaluator2._atr_cache = {'S2TEST': 0.02}

    with patch('pykrx.stock.get_market_ohlcv_by_date', side_effect=Exception('skip')):
        cat_result = evaluator2._check_catastrophic_stop(pos_s2_cat, thresholds_cat)

    _check('Catastrophic Stop: 발동 (고점-10% > ATR×4=8%)',
           cat_result['exit'] is True, f"exit={cat_result['exit']}")
    _check('Catastrophic urgency = 3 (즉시)',
           cat_result['urgency'] == 3, f"urgency={cat_result['urgency']}")
    _check('ATR 필드 포함',
           'atr_pct' in cat_result, f"keys={list(cat_result.keys())}")

    # 비발동 케이스: 하락 3% < 8%
    pos_s2_ok = {
        'ticker': 'S2OK', 'stream_id': 'S2',
        'pnl_pct': 17.0, 'peak_pnl_pct': 20.0,
    }
    evaluator2._atr_cache['S2OK'] = 0.02
    cat_ok = evaluator2._check_catastrophic_stop(pos_s2_ok, thresholds_cat)
    _check('Catastrophic Stop: 비발동 (하락 3% < 8%)',
           cat_ok['exit'] is False, f"drawdown=3%")

    # S2 아닌 스트림: 미적용
    pos_s4 = {'ticker': 'S4TEST', 'stream_id': 'S4', 'pnl_pct': 5.0, 'peak_pnl_pct': 20.0}
    cat_s4 = evaluator2._check_catastrophic_stop(pos_s4, thresholds_cat)
    _check('Catastrophic Stop: S4에는 미적용',
           cat_s4['exit'] is False, f"stream=S4")

except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _check('테스트 2', False, str(e))


# ══════════════════════════════════════════════════════════
# 테스트 3: Scale-out 50% 분할 익절
# ══════════════════════════════════════════════════════════
print('\n=== 테스트 3: Scale-out 50% 분할 익절 ===')

try:
    evaluator3 = DynamicExitEvaluator()
    thresholds_so = {'portfolio_vol': 0.02, 'vix': 18.0}

    # ATR=2%, scale_out_atr_mult=10 → target = max(2%×10=20%, 30%) = 30%
    # pnl_pct=35% → scale_out 발동
    pos_so_trigger = {
        'ticker': 'SOTRIG', 'pnl_pct': 35.0, 'peak_pnl_pct': 35.0,
        'scaled_out': False,
    }
    evaluator3._atr_cache = {'SOTRIG': 0.02}

    tp_result = evaluator3._check_take_profit(pos_so_trigger, thresholds_so, 'bull')

    _check('Scale-out 발동 (pnl=35% >= target=30%)',
           tp_result.get('scale_out') is True,
           f"scale_out={tp_result.get('scale_out')}")
    _check('Scale-out: exit=False (전량 청산 아님)',
           tp_result['exit'] is False, f"exit={tp_result['exit']}")
    _check('Scale-out urgency=3',
           tp_result['urgency'] == 3, f"urgency={tp_result['urgency']}")
    _check('scale_out_target 필드 존재',
           'scale_out_target' in tp_result,
           f"target={tp_result.get('scale_out_target')}%")

    # 미발동 케이스: pnl=20% < 30%
    pos_so_notyet = {
        'ticker': 'SONOTYET', 'pnl_pct': 20.0, 'peak_pnl_pct': 20.0,
        'scaled_out': False,
    }
    evaluator3._atr_cache['SONOTYET'] = 0.02
    tp_no = evaluator3._check_take_profit(pos_so_notyet, thresholds_so, 'bull')
    _check('Scale-out 미발동 (pnl=20% < 30%)',
           tp_no.get('scale_out', False) is False,
           f"scale_out={tp_no.get('scale_out', False)}")

except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _check('테스트 3', False, str(e))


# ══════════════════════════════════════════════════════════
# 테스트 4: Runner 상태 관리
# ══════════════════════════════════════════════════════════
print('\n=== 테스트 4: Runner 상태 (scaled_out=True) 포지션 처리 ===')

try:
    evaluator4 = DynamicExitEvaluator()
    thresholds_r = {'portfolio_vol': 0.02, 'vix': 18.0}

    # Runner 포지션: scaled_out=True → 고정 TP 없음
    # 고점 50%, 현재 45%, ATR=2%, mult=3 → 드롭=6% → 5% 하락이므로 트레일링 미발동
    pos_runner_hold = {
        'ticker': 'RUNNER1', 'pnl_pct': 45.0, 'peak_pnl_pct': 50.0,
        'scaled_out': True,
    }
    evaluator4._atr_cache = {'RUNNER1': 0.02}
    tp_runner = evaluator4._check_take_profit(pos_runner_hold, thresholds_r, 'bull')

    _check('Runner: 고정 TP 비활성 (scaled_out=True)',
           tp_runner['exit'] is False and tp_runner.get('scale_out', False) is False,
           f"exit={tp_runner['exit']}, scale_out={tp_runner.get('scale_out')}")

    # Runner: 트레일링 스탑 발동 (고점 50% 대비 -8% > chan=6%)
    pos_runner_exit = {
        'ticker': 'RUNNER2', 'pnl_pct': 42.0, 'peak_pnl_pct': 50.0,
        'scaled_out': True,
    }
    evaluator4._atr_cache['RUNNER2'] = 0.02
    tp_runner_exit = evaluator4._check_take_profit(pos_runner_exit, thresholds_r, 'bull')

    _check('Runner: 트레일링 스탑 발동 (50%→42% = -8% > chan=6%)',
           tp_runner_exit['exit'] is True,
           f"exit={tp_runner_exit['exit']}, detail={tp_runner_exit.get('detail', '')[:50]}")
    _check('Runner 트레일링: chandelier_drop 포함',
           'chandelier_drop' in tp_runner_exit,
           f"keys={list(tp_runner_exit.keys())}")

    # Runner 포지션에 Scale-out 추가 발동 없음
    pos_runner_no_so = {
        'ticker': 'RUNNER3', 'pnl_pct': 60.0, 'peak_pnl_pct': 60.0,
        'scaled_out': True,  # 이미 익절 완료
    }
    evaluator4._atr_cache['RUNNER3'] = 0.02
    tp_no_so = evaluator4._check_take_profit(pos_runner_no_so, thresholds_r, 'bull')
    _check('Runner: scale_out 재발동 없음 (scaled_out=True)',
           tp_no_so.get('scale_out', False) is False,
           f"scale_out={tp_no_so.get('scale_out', False)}")

except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _check('테스트 4', False, str(e))


# ══════════════════════════════════════════════════════════
# 테스트 5: VIX 高수 시 chandelier_mult 축소
# ══════════════════════════════════════════════════════════
print('\n=== 테스트 5: VIX > 25 시 Chandelier 배수 축소 ===')

try:
    evaluator5 = DynamicExitEvaluator()

    thresh_bull_lowvix  = {'portfolio_vol': 0.02, 'vix': 18.0}
    thresh_bull_highvix = {'portfolio_vol': 0.02, 'vix': 30.0}

    mult_low  = evaluator5._compute_chandelier_mult(thresh_bull_lowvix,  'bull')
    mult_high = evaluator5._compute_chandelier_mult(thresh_bull_highvix, 'bull')

    _check('VIX=18: chandelier_mult = 3.0 (정상)',
           abs(mult_low - 3.0) < 0.01, f'mult={mult_low}')
    _check('VIX=30: chandelier_mult < 3.0 (축소, 이익 조기 확정)',
           mult_high < mult_low,
           f'VIX18={mult_low:.2f} > VIX30={mult_high:.2f}')
    _check('VIX=30: 배수 = 3.0 × 0.8 = 2.4',
           abs(mult_high - 2.4) < 0.01, f'got={mult_high}')

    # Crash 레짐: 배수=1.5
    mult_crash = evaluator5._compute_chandelier_mult({'portfolio_vol': 0.02, 'vix': 18.0}, 'crash')
    _check('Crash 레짐: chandelier_mult = 1.5 (최소, 이익 즉시 확정)',
           abs(mult_crash - 1.5) < 0.01, f'got={mult_crash}')

except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _check('테스트 5', False, str(e))


# ══════════════════════════════════════════════════════════
# 구문 검증 (3개 파일)
# ══════════════════════════════════════════════════════════
print('\n=== 구문 검증 ===')
import ast

TARGET_FILES = [
    'src/streams/s4_advisory/dynamic_exit.py',
    'src/portfolio/shadow_manager.py',
]
for rel_path in TARGET_FILES:
    fp = ROOT / rel_path
    if fp.exists():
        try:
            ast.parse(fp.read_text())
            lines = len(fp.read_text().splitlines())
            _check(f'{rel_path} 구문 OK', True, f'{lines}줄')
        except SyntaxError as e:
            _check(f'{rel_path} 구문 OK', False, f'L{e.lineno}: {e.msg}')
    else:
        _check(f'{rel_path} 파일 존재', False, '파일 없음')


# ══════════════════════════════════════════════════════════
# 코드 구조 검증
# ══════════════════════════════════════════════════════════
print('\n=== 코드 구조 검증 ===')

de_src = (ROOT / 'src/streams/s4_advisory/dynamic_exit.py').read_text()
sm_src = (ROOT / 'src/portfolio/shadow_manager.py').read_text()

_check('dynamic_exit: _compute_ticker_atr 존재', 'def _compute_ticker_atr' in de_src)
_check('dynamic_exit: _compute_chandelier_mult 존재', 'def _compute_chandelier_mult' in de_src)
_check('dynamic_exit: _check_catastrophic_stop 존재', 'def _check_catastrophic_stop' in de_src)
_check('dynamic_exit: scale_out_flag 로직 존재', 'scale_out_flag' in de_src)
_check('dynamic_exit: chandelier_drop 로직 존재', 'chandelier_drop' in de_src)
_check('dynamic_exit: 3단 Fallback (pykrx→parquet→vol)', 'vol_proxy fallback' in de_src)
_check('dynamic_exit: Zero Hardcoding (cfg.get)', 'exit.chandelier_mult.bull' in de_src)
_check('dynamic_exit: catastrophic_atr_mult cfg', 'exit.catastrophic_atr_mult' in de_src)
_check('dynamic_exit: scale_out_atr_mult cfg', 'exit.scale_out_atr_mult' in de_src)

_check('shadow_manager: _compute_ticker_atr_pct 존재', 'def _compute_ticker_atr_pct' in sm_src)
_check('shadow_manager: Catastrophic Stop 로직 존재', 'catastrophic_stop' in sm_src)
_check('shadow_manager: scale_out_partial sell_type 존재', 'scale_out_partial' in sm_src)
_check('shadow_manager: scaled_out 플래그 존재', "pos['scaled_out'] = True" in sm_src)
_check('shadow_manager: take_profit_pct=None (Runner) 존재', "pos['take_profit_pct'] = None" in sm_src)
_check('shadow_manager: chandelier_trailing sell_type 존재', 'chandelier_trailing' in sm_src)
_check('shadow_manager: _atr_loop_cache 존재', '_atr_loop_cache' in sm_src)
_check('shadow_manager: VIX 로드 추가', '_vix = _sc_data.get(\'vix\'' in sm_src)


# ══════════════════════════════════════════════════════════
# 최종 결과
# ══════════════════════════════════════════════════════════
print(f'\n{"="*60}')
print(f'최종 결과: PASS={PASS}, FAIL={FAIL}')
if FAIL == 0:
    print('✅ 전체 검증 완료 — Fully Dynamic Exit 알고리즘 정상')
else:
    print(f'⚠️ {FAIL}개 항목 점검 필요')
    sys.exit(1)

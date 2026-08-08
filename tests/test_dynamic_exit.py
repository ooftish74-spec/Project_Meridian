#!/usr/bin/env python3
"""
test_dynamic_exit.py — Phase 36 장중 동적 조정 단위 테스트
============================================================
[Phase 36: Intraday Dynamic Adjustment]

Mock intraday_flow_cache.json 을 주입하여
Trend Rider / Panic Tightener / Whipsaw Filter / Fallback 4케이스를 검증합니다.

실행:
    pytest tests/test_dynamic_exit.py -v
    python3 tests/test_dynamic_exit.py  # pytest 없을 때
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import unittest

# ── 프로젝트 루트를 sys.path에 추가 ─────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ══════════════════════════════════════════════════════════════════════════════
# Mock 헬퍼
# ══════════════════════════════════════════════════════════════════════════════

def _make_flow_data(
    ticker: str = "005930",
    institution_krw: float = 0.0,
    foreign_krw: float = 0.0,
    volume_ratio: float = 1.0,
    current_price: int = 75_000,
) -> Dict[str, Any]:
    """테스트용 Mock flow_data 생성 (백만원 단위)."""
    combined = institution_krw + foreign_krw
    return {
        "timestamp":   datetime.now().isoformat(),
        "market_date": datetime.now().strftime("%Y%m%d"),
        "tickers": {
            ticker: {
                "institution_net_qty":   int(institution_krw * 1_000_000 / max(current_price, 1)),
                "foreign_net_qty":       int(foreign_krw * 1_000_000 / max(current_price, 1)),
                "institution_net_krw":   institution_krw,
                "foreign_net_krw":       foreign_krw,
                "combined_net_krw":      combined,
                "today_volume":          int(volume_ratio * 10_000_000),
                "prev_volume":           10_000_000,
                "volume_ratio":          volume_ratio,
                "current_price":         current_price,
            }
        },
    }


def _make_position(
    ticker: str = "005930",
    pnl_pct: float = 5.0,
    peak_pnl_pct: float = 8.0,
    stream_id: str = "S2",
) -> Dict[str, Any]:
    """테스트용 단일 포지션 dict."""
    return {
        "ticker":         ticker,
        "name":           f"TestPos_{ticker}",
        "stream_id":      stream_id,
        "stream":         stream_id,
        "pnl_pct":        pnl_pct,
        "peak_pnl_pct":   peak_pnl_pct,
        "qv_score":       65.0,
        "entry_price":    70_000,
        "current_price":  70_000 * (1 + pnl_pct / 100),
        "atr_pct":        0.02,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 테스트 클래스
# ══════════════════════════════════════════════════════════════════════════════

class TestIntradayFlowCollector(unittest.TestCase):
    """IntradayFlowCollector 단위 테스트."""

    def test_load_cache_missing_file_returns_empty(self):
        """캐시 파일 없을 때 {} 반환 (Graceful Fallback)."""
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector

        with tempfile.TemporaryDirectory() as tmpdir:
            result = IntradayFlowCollector.load_cache(Path(tmpdir))
        self.assertEqual(result, {})

    def test_load_cache_valid_file(self):
        """유효한 캐시 파일 정상 로드."""
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector

        mock_data = _make_flow_data("005930", institution_krw=3_000.0, foreign_krw=2_000.0)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = Path(tmpdir) / "intraday_flow_cache.json"
            cache_path.write_text(json.dumps(mock_data), encoding="utf-8")
            result = IntradayFlowCollector.load_cache(Path(tmpdir))

        self.assertIn("tickers", result)
        self.assertIn("005930", result["tickers"])
        self.assertAlmostEqual(result["tickers"]["005930"]["combined_net_krw"], 5_000.0)

    def test_get_ticker_flow_helper(self):
        """get_ticker_flow 헬퍼 정상 동작."""
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector

        cache = _make_flow_data("000660", foreign_krw=7_000.0)
        flow = IntradayFlowCollector.get_ticker_flow(cache, "000660")
        self.assertEqual(flow["foreign_net_krw"], 7_000.0)

    def test_get_ticker_flow_missing(self):
        """없는 종목 조회 → 빈 dict."""
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector

        cache = _make_flow_data("005930")
        flow = IntradayFlowCollector.get_ticker_flow(cache, "NONEXIST")
        self.assertEqual(flow, {})

    def test_save_and_load_cache_roundtrip(self):
        """저장 후 로드 시 데이터 보존 확인."""
        from src.data_collection.intraday_flow_collector import IntradayFlowCollector

        with tempfile.TemporaryDirectory() as tmpdir:
            collector = IntradayFlowCollector(results_dir=Path(tmpdir))
            mock = _make_flow_data("005930", institution_krw=1_000.0, volume_ratio=2.5)
            collector._save_cache(mock)
            loaded = IntradayFlowCollector.load_cache(Path(tmpdir))

        self.assertIn("005930", loaded.get("tickers", {}))
        self.assertAlmostEqual(
            loaded["tickers"]["005930"]["volume_ratio"], 2.5
        )


class TestDynamicExitFlowIntegration(unittest.TestCase):
    """DynamicExitEvaluator Phase 36 Flow 연동 테스트."""

    def _get_evaluator(self):
        """DynamicExitEvaluator 인스턴스 생성."""
        from src.streams.s4_advisory.dynamic_exit import DynamicExitEvaluator
        return DynamicExitEvaluator()

    # ── Case 1: Trend Rider ────────────────────────────────────────────────
    def test_trend_rider_widen_trailing_stop(self):
        """
        [Trend Rider] 쌍끌이 매수 → action='widen', exit=False,
        flow_adjusted_ts_mult > 원래 값.
        """
        evaluator = self._get_evaluator()
        pos = _make_position("005930", pnl_pct=5.0)
        thresholds = {"stop_loss_pct": 0.07, "trail_stop_atr_mult": 1.5}

        # 기관 3,750백만 + 외인 2,250백만 = 6,000백만 (60억) → 강한 쌍끌이 매수
        flow_data = _make_flow_data(
            "005930",
            institution_krw=3_750.0,  # 37.5억 (백만원)
            foreign_krw=2_250.0,       # 22.5억
            volume_ratio=0.80,
        )

        result = evaluator._check_flow_dynamic_exit(pos, thresholds, flow_data, "005930")

        self.assertEqual(result["action"], "widen",
                         f"쌍끌이 매수에서 action=widen 기대, 실제={result['action']}")
        self.assertFalse(result["exit"], "Trend Rider는 exit=False 여야 함")
        self.assertIsNotNone(result["flow_adjusted_ts_mult"])
        self.assertGreater(result["flow_adjusted_ts_mult"], 1.5,
                           "Trailing Stop 배수가 확장되어야 함")
        print(f"  ✅ Trend Rider: ts_mult {1.5:.1f} → {result['flow_adjusted_ts_mult']:.2f}")

    # ── Case 2: Panic Tightener ────────────────────────────────────────────
    def test_panic_tightener_tighten_stop_loss(self):
        """
        [Panic Tightener] 쌍끌이 매도 + 하락 중 → action='tighten', exit=True,
        urgency=3.
        """
        evaluator = self._get_evaluator()
        # 현재 -3% 손실 (panic_drop_pct=0.015 = 1.5% 기준 초과)
        pos = _make_position("005930", pnl_pct=-3.0)
        thresholds = {"stop_loss_pct": 0.07, "trail_stop_atr_mult": 1.5}

        # 기관 -4,000백만 + 외인 -2,000백만 = -6,000백만 쌍끌이 매도
        flow_data = _make_flow_data(
            "005930",
            institution_krw=-4_000.0,
            foreign_krw=-2_000.0,
            volume_ratio=1.5,
        )

        result = evaluator._check_flow_dynamic_exit(pos, thresholds, flow_data, "005930")

        self.assertEqual(result["action"], "tighten",
                         f"쌍끌이 매도+하락에서 action=tighten 기대, 실제={result['action']}")
        self.assertTrue(result["exit"], "Panic Tightener는 exit=True 여야 함")
        self.assertEqual(result["urgency"], 3, "긴급도=3(즉시 청산) 이어야 함")
        self.assertIsNotNone(result["flow_adjusted_sl_pct"])
        self.assertLess(result["flow_adjusted_sl_pct"], 0.07,
                        "Stop-Loss가 타이트닝되어야 함(0.07 → 더 작은 값)")
        print(f"  ✅ Panic Tightener: sl {0.07:.2%} → {result['flow_adjusted_sl_pct']:.2%}")

    # ── Case 3: Whipsaw Filter ─────────────────────────────────────────────
    def test_whipsaw_filter_defers_stop_loss(self):
        """
        [Whipsaw Filter] 거래량 낮음 + 수급 없음 → whipsaw_defer=True, exit=False.
        """
        evaluator = self._get_evaluator()
        pos = _make_position("005930", pnl_pct=-4.0)
        thresholds = {"stop_loss_pct": 0.07, "trail_stop_atr_mult": 1.5}

        # 거래량 15% (30% 임계 미달) + 수급 거의 없음 → 개미털기
        flow_data = _make_flow_data(
            "005930",
            institution_krw=50.0,   # 0.5억 — 미미
            foreign_krw=30.0,       # 0.3억 — 미미
            volume_ratio=0.15,      # 전일 대비 15% (30% 미달)
        )

        result = evaluator._check_flow_dynamic_exit(pos, thresholds, flow_data, "005930")

        self.assertTrue(result["whipsaw_defer"],
                        "거래량 낮고 수급 없으면 whipsaw_defer=True 여야 함")
        self.assertFalse(result["exit"], "Whipsaw 감지 시 exit=False(손절 유예) 여야 함")
        self.assertEqual(result["action"], "whipsaw_defer")
        print(f"  ✅ Whipsaw Filter: 손절 유예 발동 → {result['detail'][:60]}...")

    # ── Case 4: Fallback (flow_data 없음) ─────────────────────────────────
    def test_fallback_no_flow_data_neutral(self):
        """
        [Fallback] flow_data={} → action='neutral', exit=False.
        evaluate()가 기존 정적 로직으로 동작해야 함.
        """
        evaluator = self._get_evaluator()
        pos = _make_position("005930", pnl_pct=2.0)
        thresholds = {"stop_loss_pct": 0.07, "trail_stop_atr_mult": 1.5}
        flow_data = {}  # 빈 dict → Fallback

        result = evaluator._check_flow_dynamic_exit(pos, thresholds, flow_data, "005930")

        self.assertEqual(result["action"], "neutral",
                         "flow_data 없을 때 action=neutral 이어야 함")
        self.assertFalse(result["exit"])
        self.assertFalse(result["whipsaw_defer"])
        print(f"  ✅ Fallback: flow_data 없음 → neutral ({result['detail']})")

    # ── Case 5: evaluate() 시그니처 하위 호환성 ──────────────────────────
    def test_evaluate_backward_compatible(self):
        """
        [하위 호환] evaluate(positions, market_data, regime) — flow_data 없이 호출해도 동작.
        """
        evaluator = self._get_evaluator()
        positions = {"test:005930": _make_position("005930", pnl_pct=2.0)}
        # flow_data 생략 → 내부에서 None → {} 처리
        result = evaluator.evaluate(positions, market_data={}, regime="bull")
        self.assertIn("exit_candidates", result)
        self.assertIn("hold_positions",  result)
        print(f"  ✅ 하위 호환: total={result['total_positions']} exit={result['exit_count']}")

    # ── Case 6: evaluate() with flow_data ────────────────────────────────
    def test_evaluate_with_panic_flow_triggers_exit(self):
        """
        [Panic] evaluate(flow_data=panic_data) → exit_candidates에 해당 종목 포함.
        """
        evaluator = self._get_evaluator()
        positions = {
            "S2:005930": _make_position("005930", pnl_pct=-4.0, stream_id="S2"),
        }
        # 강한 쌍끌이 매도 + 하락
        flow_data = _make_flow_data("005930", institution_krw=-5_000.0, foreign_krw=-3_000.0, volume_ratio=1.5)

        result = evaluator.evaluate(
            positions, market_data={}, regime="bull", flow_data=flow_data
        )

        flow_exits = [
            c for c in result["exit_candidates"]
            if any(r.get("rule") == "flow_dynamic" for r in c.get("reasons", []))
        ]
        self.assertGreater(len(flow_exits), 0,
                           "Panic Tightener 발동 시 exit_candidates에 flow_dynamic 포함 기대")
        print(f"  ✅ evaluate() Panic Flow: {len(flow_exits)}개 flow_dynamic exit 발생")


class TestS1IntradayBreakout(unittest.TestCase):
    """S1IntradayBreakout 단위 테스트."""

    def test_scan_with_spike_triggers_signal(self):
        """
        거래량 폭발 + 외국인 유입 → 시그널 반환.
        """
        from src.streams.s1_edge.s1_intraday_breakout import S1IntradayBreakout

        flow_data = _make_flow_data(
            "005930",
            foreign_krw=7_000.0,   # 70억 (min=50억 초과)
            volume_ratio=4.2,      # 4.2배 (임계 3.0배 초과)
        )
        scanner = S1IntradayBreakout()
        # 시간 윈도우 무시하고 직접 _evaluate_ticker 테스트
        result = scanner._evaluate_ticker(
            "005930",
            flow_data["tickers"]["005930"],
            volume_spike_thr=3.0,
            min_foreign_krw_m=5_000.0,  # 50억 = 5,000백만
        )

        self.assertIsNotNone(result, "조건 충족 시 시그널 반환 기대")
        self.assertEqual(result["signal"], "BREAKOUT_LONG")
        weight = result.get("weight", result.get("size_pct", 0))
        self.assertGreater(weight, 0)
        print(f"  ✅ S1-B: BREAKOUT_LONG 시그널 생성 — size={weight:.0%}")

    def test_scan_below_threshold_returns_none(self):
        """
        거래량 미달 → None 반환 (시그널 없음).
        """
        from src.streams.s1_edge.s1_intraday_breakout import S1IntradayBreakout

        flow_data = _make_flow_data(
            "005930",
            foreign_krw=7_000.0,
            volume_ratio=1.5,   # 임계 3.0배 미달
        )
        scanner = S1IntradayBreakout()
        result = scanner._evaluate_ticker(
            "005930",
            flow_data["tickers"]["005930"],
            volume_spike_thr=3.0,
            min_foreign_krw_m=5_000.0,
        )
        self.assertIsNone(result, "거래량 미달 시 None 기대")
        print("  ✅ S1-B: 거래량 미달 → 시그널 없음 (정상)")

    def test_scan_no_flow_data_returns_empty(self):
        """
        flow_data 없을 때 빈 리스트 반환.
        """
        from src.streams.s1_edge.s1_intraday_breakout import S1IntradayBreakout

        scanner = S1IntradayBreakout()
        result = scanner.scan({}, universe=["005930"])
        self.assertEqual(result, [])
        print("  ✅ S1-B: flow_data 없음 → [] (정상)")


class TestDynamicConfigPhase36(unittest.TestCase):
    """DynamicConfig Phase 36 파라미터 존재 확인."""

    def test_intraday_params_exist(self):
        """intraday 섹션 파라미터 존재 확인."""
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()

        pairs = [
            ("intraday.flow_fetch_interval_min",   10),
            ("intraday.flow_trend_rider_ratio",     1.3),
            ("intraday.flow_panic_ratio",           0.6),
            ("intraday.whipsaw_filter_enabled",    True),
            ("intraday.whipsaw_volume_threshold",  0.3),
        ]
        for key, expected in pairs:
            val = cfg.get(key)
            self.assertIsNotNone(val, f"{key} 파라미터가 config에 없음")
            self.assertEqual(val, expected, f"{key}: 기대={expected}, 실제={val}")
            print(f"  ✅ {key} = {val}")

    def test_s1_breakout_params_exist(self):
        """s1_breakout 섹션 파라미터 존재 확인."""
        from config.dynamic_config import DynamicConfig
        cfg = DynamicConfig()

        pairs = [
            ("s1_breakout.volume_spike_threshold", 3.0),
            ("s1_breakout.breakout_size_pct",      0.05),
            ("s1_breakout.max_signals_per_day",    2),
        ]
        for key, expected in pairs:
            val = cfg.get(key)
            self.assertIsNotNone(val, f"{key} 파라미터가 config에 없음")
            self.assertEqual(val, expected, f"{key}: 기대={expected}, 실제={val}")
            print(f"  ✅ {key} = {val}")


# ══════════════════════════════════════════════════════════════════════════════
# 직접 실행 지원 (pytest 없을 때)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)  # 테스트 중 로그 최소화

    print("=" * 65)
    print("Phase 36 Intraday Dynamic Adjustment — 단위 테스트")
    print("=" * 65)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # 테스트 클래스 등록
    for cls in [
        TestDynamicConfigPhase36,
        TestIntradayFlowCollector,
        TestDynamicExitFlowIntegration,
        TestS1IntradayBreakout,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    if result.wasSuccessful():
        print("✅ 모든 테스트 통과!")
    else:
        print(f"❌ 실패: {len(result.failures)}건, 오류: {len(result.errors)}건")
        sys.exit(1)

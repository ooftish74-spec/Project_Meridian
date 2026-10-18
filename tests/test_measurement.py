#!/usr/bin/env python3
"""
test_measurement.py — MeasurementEngine 핵심 메서드 테스트
============================================================

DD 권고 #11: 테스트 커버리지 확대
- 측정 파이프라인 실행
- 결과 구조 검증
- 빈 데이터 안전성
"""

import json
import os
import sys
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)


# ★ Track E: 결정적(hermetic) 테스트 — 로컬 results/ 상태와 네트워크(pykrx/Naver)에 의존하던 4건 실패의
#   근본 원인(① 벤치마크 조회의 requests.ConnectionError 가 compute() 를 중단 ② 실제 results/ 를 읽고 씀)을
#   제거한다. 엔진 측은 네트워크 예외를 비치명 폴백하도록 수정되었고, 여기서는 결과 디렉터리를 tmp 로 격리한다.
_SYNTH_SHADOW = {
    'initial_capital': 10_000_000.0,
    'virtual_nav': 10_150_000.0,
    'cash': 2_000_000.0,
    'positions': {},
    'trade_history': [
        {'date': '2026-10-13', 'action': 'BUY', 'ticker': '069500', 'quantity': 10, 'price': 90000.0,
         'stream': 'S1', 'stream_id': 'S1', 'confidence': 0.6},
        {'date': '2026-10-15', 'action': 'SELL', 'ticker': '069500', 'quantity': 10, 'price': 91000.0,
         'avg_price': 90000.0, 'realized_pnl': 10000.0, 'pnl_pct': 1.11, 'stream': 'S1', 'stream_id': 'S1'},
    ],
    'daily_snapshots': [
        {'date': '2026-10-12', 'nav': 10_000_000.0, 'n_positions': 1},
        {'date': '2026-10-13', 'nav': 10_050_000.0, 'n_positions': 1},
        {'date': '2026-10-14', 'nav': 9_980_000.0, 'n_positions': 1},
        {'date': '2026-10-15', 'nav': 10_100_000.0, 'n_positions': 1},
        {'date': '2026-10-16', 'nav': 10_150_000.0, 'n_positions': 1},
    ],
    'daily_records': [],
}


@pytest.fixture(autouse=True)
def _isolated_results(tmp_path, monkeypatch):
    import src.measurement.measurement_engine as me_mod
    res = tmp_path / 'results'
    res.mkdir()
    (res / 'shadow_portfolio.json').write_text(json.dumps(_SYNTH_SHADOW), encoding='utf-8')
    monkeypatch.setattr(me_mod, 'RESULTS', res)
    try:
        import src.measurement.beta_tracker as bt
        monkeypatch.setattr(bt, '_RESULTS_DIR', res)
        monkeypatch.setattr(bt, '_BETA_HISTORY_FILE', res / 'portfolio_beta_history.json')
    except Exception:
        pass
    try:
        import src.measurement.pnl_attribution as pa
        monkeypatch.setattr(pa, '_RESULTS', res)
    except Exception:
        pass

    def _no_network(*a, **k):
        raise ConnectionError('network disabled in unit test')
    for modname in ('pykrx.stock', 'src.data_collection.pykrx_compat'):
        try:
            mod = __import__(modname, fromlist=['x'])
            target = getattr(mod, 'stock', mod)
            if hasattr(target, 'get_market_ohlcv_by_date'):
                monkeypatch.setattr(target, 'get_market_ohlcv_by_date', _no_network)
        except Exception:
            pass
    yield res


class TestMeasurementEngine:
    """MeasurementEngine 단일 측정 파이프라인 테스트."""

    def test_init(self):
        """MeasurementEngine 생성."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        assert me is not None
        assert me.result == {}

    def test_compute_returns_dict(self):
        """compute()는 딕셔너리를 반환."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        assert isinstance(result, dict)

    def test_result_structure(self):
        """결과에 핵심 섹션이 포함."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        # 주요 키 존재 확인 (데이터 없어도 구조는 유지)
        expected_keys = {'portfolio', 'risk', 'timestamp'}
        present = set(result.keys())
        missing = expected_keys - present
        # 데이터가 없으면 일부 키가 빠질 수 있으므로 최소한 timestamp는 있어야 함
        assert 'timestamp' in result or len(result) > 0, \
            f"Result should have structure, got keys: {list(result.keys())}"
        # ★ Track E: 결정적 fixture 이므로 구조를 강하게 검증
        assert {'official', 'views', 'timestamp'} <= present
        assert result['official']['verdict'] == 'DIAGNOSE_CONTINUE'
        assert result['official']['blocking'] is False
        assert result['views']['go_nogo']['blocking'] is False
        # 5개 거래일 NAV → 4개 수익률 (휴장일 행 없음)
        assert result['views']['risk']['n_return_days'] == 4
        assert result['views']['risk']['sharpe'] is not None

    def test_compute_idempotent(self):
        """같은 데이터로 두 번 compute해도 같은 결과."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        r1 = me.compute()
        r2 = me.compute()
        # timestamp 제외하고 같아야 함
        for key in r1:
            if key != 'timestamp':
                assert r1[key] == r2[key], \
                    f"Key {key} differs between runs"

    def test_result_stored(self):
        """compute() 후 result 속성에 저장."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        result = me.compute()
        assert me.result == result


class TestMeasurementEngineMetrics:
    """개별 측정 지표 테스트."""

    def test_portfolio_view(self):
        """_compute_portfolio_view는 안전하게 동작."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        # 빈 shadow_portfolio로 테스트
        portfolio = me._compute_portfolio_view({})
        assert isinstance(portfolio, dict)

    def test_risk_view(self):
        """_compute_risk_view는 안전하게 동작."""
        from src.measurement.measurement_engine import MeasurementEngine
        me = MeasurementEngine()
        risk = me._compute_risk_view({})
        assert isinstance(risk, dict)

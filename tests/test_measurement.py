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

    def test_alpha_accounting_identity_ignores_stale_unrealized_pnl(self, monkeypatch):
        """유령 unrealized_pnl(+980,495)이 존재해도 alpha_pct와 jensen_alpha_pct는 실제 NAV 수익률 항등식을 따른다."""
        import pandas as pd
        from src.measurement.measurement_engine import MeasurementEngine
        import src.data_collection.pykrx_compat as pk_mod

        def _mock_bench(start, end, ticker):
            idx = pd.to_datetime(['2026-10-16', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22'])
            return pd.DataFrame({'종가': [88750.0, 89930.0, 86755.0, 89725.0, 86525.0]}, index=idx)

        monkeypatch.setattr(pk_mod.stock, 'get_market_ohlcv_by_date', _mock_bench)
        monkeypatch.setenv('MERIDIAN_TEST_MODE', '1')

        sp = {
            'initial_capital': 18_610_659.0,
            'virtual_nav': 18_268_272.0,
            'cash': 5_225_450.0,
            'cash_usd_krw': 6_527_950.0,
            'realized_pnl': 210_868.49,
            'unrealized_pnl': 980_495.19,  # 과거 고정된 유령 미실현손익
            'daily_snapshots': [
                {'date': '2026-10-19', 'nav': 19_052_453.0, 'cash': 402_111.0, 'n_positions': 6},
                {'date': '2026-10-20', 'nav': 18_123_583.0, 'cash': 3_068_567.0, 'n_positions': 6},
                {'date': '2026-10-21', 'nav': 18_658_493.0, 'cash': 3_068_567.0, 'n_positions': 6},
                {'date': '2026-10-22', 'nav': 18_268_272.0, 'cash': 5_225_450.0, 'n_positions': 8},
            ],
            'daily_records': [],
            'positions': {},
            'trade_history': [],
        }
        pv = MeasurementEngine()._compute_portfolio_view(sp)
        # 1) 누적수익률 = (18268272 / 18610659 - 1) * 100 = -1.839736% -> -1.84%
        expected_cum_ret = (18_268_272.0 / 18_610_659.0 - 1.0) * 100.0
        # 2) 벤치마크(069500): 10/16 기준종가(88,750) -> 10/22 종가(86,525) = -2.507042% -> -2.507%
        expected_bench_ret = (86525.0 / 88750.0 - 1.0) * 100.0
        assert pv['cumulative_return_pct'] == pytest.approx(round(expected_cum_ret, 3), abs=1e-3)
        assert pv['benchmark_return_pct'] == pytest.approx(round(expected_bench_ret, 3), abs=1e-3)
        # 3) 단순 초과수익(alpha_pct == nav_alpha) = -1.840% - (-2.507%) = +0.667% (절대 +6.638%가 아님!)
        assert pv['alpha_pct'] == pytest.approx(round(expected_cum_ret - expected_bench_ret, 3), abs=2e-3)
        assert pv['alpha_pct'] == pv['nav_alpha']
        # 4) Jensen's Alpha + Allocation Effect == Active Alpha 항등식 검증
        assert pv['jensen_alpha_pct'] + pv['allocation_effect'] == pytest.approx(pv['alpha_pct'], abs=5e-3)
        assert pv['jensen_alpha_pct'] < 0.0  # 현금 효과를 제거한 순수 젠센 알파는 음수

    def test_heal_positions_recomputes_root_unrealized_pnl(self):
        """_heal_positions가 개별 포지션 및 루트 unrealized_pnl을 최신 현재가/환율 기준으로 재계산한다."""
        from src.portfolio.shadow_manager import ShadowPortfolioManager
        data = {
            'cash_usd_fx': 1325.0,
            'unrealized_pnl': 980495.19,
            'positions': {
                'S_RECON:069500': {
                    'ticker': '069500', 'quantity': 51, 'avg_price': 89476.9608,
                    'current_price': 86525.0, 'currency': 'KRW', 'market': 'KR',
                    'unrealized_pnl': 0.0,
                },
                'S_RECON:TLT': {
                    'ticker': 'TLT', 'quantity': 1, 'avg_price': 79.00,
                    'current_price': 80.00, 'currency': 'USD', 'market': 'US',
                    'unrealized_pnl': 0.0,
                },
            },
        }
        ShadowPortfolioManager._heal_positions(data)
        expected_kr = (86525.0 - 89476.9608) * 51
        expected_us_usd = (80.00 - 79.00) * 1
        assert data['positions']['S_RECON:069500']['unrealized_pnl'] == pytest.approx(expected_kr, rel=1e-9)
        assert data['positions']['S_RECON:TLT']['unrealized_pnl'] == pytest.approx(expected_us_usd, rel=1e-9)
        assert data['unrealized_pnl'] == pytest.approx(round(expected_kr + expected_us_usd * 1325.0, 2), abs=1e-2)


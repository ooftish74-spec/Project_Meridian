#!/usr/bin/env python3
"""
test_wave3_measurement_diagnostics.py — Track E (Wave 3) 측정/진단/배선 검증
==========================================================================
1) 지표 수식 vs 손계산 (Sharpe / Sortino / MDD / Calmar / PSR / DSR / MinTRL)
2) 휴장일이 0 수익률로 삽입되지 않음
3) 라이브 진단은 어떤 입력에서도 차단 결정을 내지 않음 (Go/No-Go 폐지)
4) GapFeedback: 경계·일일한도·risk tighten-only + dynamic_overrides.json 실제 적용 + 감사 로그
5) DATA_NOGO: 영속 저장소 왕복, TTL, 손상 fail-closed, 오케스트레이터 Exit-Only 의미론
"""
import inspect
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.measurement import perf_stats as ps  # noqa: E402

ND = NormalDist()
R5 = [0.01, -0.005, 0.02, -0.01, 0.015]


# ═════════════════════════════════════════════════════════════
# 1) 수식 vs 손계산
# ═════════════════════════════════════════════════════════════
class TestMetricFormulas:
    def test_sharpe_hand_computed(self):
        # mean = 0.006, 표본분산 = 670e-6/4 = 167.5e-6
        sd = math.sqrt(167.5e-6)
        expected = 0.006 / sd * math.sqrt(252)
        got = ps.sharpe_ratio(R5, rf_annual=0.0)
        assert got['reason'] is None
        assert got['value'] == pytest.approx(expected, rel=1e-12)
        assert expected == pytest.approx(7.3594, abs=1e-3)

    def test_sharpe_uses_excess_over_rf(self):
        rf = 0.0252  # rf_d = 0.0001
        x = [r - 0.0001 for r in R5]
        m = sum(x) / 5
        sd = math.sqrt(sum((v - m) ** 2 for v in x) / 4)
        assert ps.sharpe_ratio(R5, rf)['value'] == pytest.approx(m / sd * math.sqrt(252), rel=1e-12)

    def test_sortino_downside_over_all_observations(self):
        # min(r,0)^2 = [0, 25e-6, 0, 100e-6, 0] → 전체 5개 평균 = 25e-6 → DD = 0.005
        expected = 0.006 / 0.005 * math.sqrt(252)
        got = ps.sortino_ratio(R5, target_annual=0.0)
        assert got['value'] == pytest.approx(expected, rel=1e-12)
        # 음수 관측만으로 평균하면 sqrt(62.5e-6)=0.0079 → 다른 값 (이전 버그 형태)과 구분
        wrong = 0.006 / math.sqrt(125e-6 / 2) * math.sqrt(252)
        assert abs(got['value'] - wrong) > 1.0

    def test_mdd_hand_computed(self):
        navs = [100, 110, 99, 105, 120, 90]
        got = ps.max_drawdown(navs)
        assert got['value'] == pytest.approx(90 / 120 - 1)
        assert got['current_drawdown'] == pytest.approx(-0.25)
        assert got['hwm'] == 120

    def test_calmar_is_cagr_over_abs_mdd(self):
        navs = [100, 120, 90, 110]
        cagr = 1.1 ** (252 / 3) - 1
        got = ps.calmar_ratio(navs)
        assert got['value'] == pytest.approx(cagr / 0.25, rel=1e-12)

    def _hand_moments(self, r):
        n = len(r)
        m = sum(r) / n
        m2 = sum((x - m) ** 2 for x in r) / n
        m3 = sum((x - m) ** 3 for x in r) / n
        m4 = sum((x - m) ** 4 for x in r) / n
        return m3 / m2 ** 1.5, m4 / m2 ** 2

    def test_psr_hand_computed(self):
        r = [0.012, -0.004, 0.007, 0.001, -0.009, 0.015, 0.003, -0.002, 0.006, 0.010]
        n = len(r)
        m = sum(r) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))
        sr = m / sd
        g3, g4 = self._hand_moments(r)
        z = sr * math.sqrt(n - 1) / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2)
        got = ps.probabilistic_sharpe_ratio(r, 0.0, rf_annual=0.0)
        assert got['value'] == pytest.approx(ND.cdf(z), rel=1e-10)
        assert 0.5 < got['value'] < 1.0

    def test_psr_known_closed_form(self):
        # 정규(γ3=0, γ4=3) 가정 SR_d=0.1, n=101 → z = 0.1*10/sqrt(1.005) → Φ(z) ≈ 0.8407
        z = 0.1 * 10 / math.sqrt(1 + 0.5 * 0.01)
        assert ND.cdf(z) == pytest.approx(0.8407, abs=1e-4)

    def test_dsr_equals_psr_when_single_trial_and_lower_with_more_trials(self):
        r = [0.012, -0.004, 0.007, 0.001, -0.009, 0.015, 0.003, -0.002, 0.006, 0.010] * 3
        psr = ps.probabilistic_sharpe_ratio(r)['value']
        d1 = ps.deflated_sharpe_ratio(r, n_trials=1)
        assert d1['value'] == pytest.approx(psr, rel=1e-12)
        d11 = ps.deflated_sharpe_ratio(r, n_trials=11)
        assert d11['value'] < psr
        assert d11['sr0_daily'] > 0

    def test_expected_max_sharpe_formula(self):
        v = 0.01
        n = 10
        g = 0.5772156649015329
        exp = math.sqrt(v) * ((1 - g) * ND.inv_cdf(1 - 1 / n) + g * ND.inv_cdf(1 - 1 / (n * math.e)))
        assert ps.expected_max_sharpe(n, v) == pytest.approx(exp, rel=1e-12)

    def test_min_trl_hand_computed(self):
        r = [0.012, -0.004, 0.007, 0.001, -0.009, 0.015, 0.003, -0.002, 0.006, 0.010]
        n = len(r)
        m = sum(r) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1))
        sr = m / sd
        g3, g4 = self._hand_moments(r)
        exp = 1 + (1 - g3 * sr + (g4 - 1) / 4 * sr ** 2) * (ND.inv_cdf(0.95) / sr) ** 2
        got = ps.min_track_record_length(r, 0.0, alpha=0.05)
        assert got['value'] == pytest.approx(exp, rel=1e-10)
        assert got['more_days_needed'] == max(0, math.ceil(exp) - n)

    def test_min_trl_undefined_when_sr_not_positive(self):
        got = ps.min_track_record_length([-0.01, 0.002, -0.004, 0.001, -0.003])
        assert got['value'] is None and got['reason'] == 'sr_not_above_benchmark'

    @pytest.mark.parametrize('rets,reason', [([], 'insufficient_data(n<2)'), ([0.01], 'insufficient_data(n<2)'),
                                             ([0.001] * 10, 'zero_variance')])
    def test_degenerate_inputs_return_none_with_reason(self, rets, reason):
        got = ps.sharpe_ratio(rets)
        assert got['value'] is None and got['reason'] == reason

    def test_sortino_no_downside_is_none(self):
        assert ps.sortino_ratio([0.01, 0.02, 0.005])['value'] is None

    def test_calmar_no_drawdown_is_none(self):
        assert ps.calmar_ratio([100, 101, 102])['value'] is None

    def test_flow_adjusted_returns(self):
        # 100 → 160 중 50 은 입금 → 실제 수익률 (160-50)/100-1 = 10%
        assert ps.simple_returns([100, 160], [0, 50]) == [pytest.approx(0.10)]

    def test_bootstrap_deterministic(self):
        r = [0.012, -0.004, 0.007, 0.001, -0.009, 0.015, 0.003, -0.002, 0.006, 0.010] * 2
        f = lambda s: ps.sharpe_ratio(s)['value']  # noqa: E731
        a = ps.bootstrap_ci(r, f, n_boot=300)
        b = ps.bootstrap_ci(r, f, n_boot=300)
        assert a['value'] == b['value'] and a['value'][0] < a['value'][1]


# ═════════════════════════════════════════════════════════════
# 2) 휴장일 0 수익률 삽입 금지
# ═════════════════════════════════════════════════════════════
class TestTradingDayCalendar:
    def test_holiday_and_weekend_rows_dropped(self):
        holidays = {'2026-10-09'}
        fn = lambda ds: datetime.strptime(ds, '%Y-%m-%d').weekday() < 5 and ds not in holidays  # noqa: E731
        recs = [
            {'date': '2026-10-08', 'nav': 100.0},
            {'date': '2026-10-09', 'nav': 100.0},   # 공휴일 (가격 이월)
            {'date': '2026-10-10', 'nav': 100.0},   # 토
            {'date': '2026-10-11', 'nav': 100.0},   # 일
            {'date': '2026-10-12', 'nav': 102.0},
            {'date': '2026-10-12T15:40:00', 'nav': 103.0},  # 같은 날 마지막 값 우선
        ]
        s = ps.build_nav_series(recs, is_trading_day=fn)
        assert s['dates'] == ['2026-10-08', '2026-10-12']
        assert s['navs'] == [100.0, 103.0]
        assert s['dropped']['non_trading_day'] == 3
        rets = ps.simple_returns(s['navs'])
        assert rets == [pytest.approx(0.03)]
        assert 0.0 not in rets  # 휴장일 0 수익률 없음

    def test_default_calendar_drops_weekends(self):
        recs = [{'date': '2026-10-16', 'nav': 1.0}, {'date': '2026-10-17', 'nav': 1.0},
                {'date': '2026-10-18', 'nav': 1.0}, {'date': '2026-10-19', 'nav': 1.01}]
        for mkt in ('KR', 'US', 'ANY'):
            s = ps.build_nav_series(recs, market=mkt)
            assert '2026-10-17' not in s['dates'] and '2026-10-18' not in s['dates']

    def test_measurement_engine_risk_view_excludes_weekend(self, tmp_path, monkeypatch):
        import src.measurement.measurement_engine as me_mod
        monkeypatch.setattr(me_mod, 'RESULTS', tmp_path)
        sp = {'initial_capital': 1000.0, 'daily_snapshots': [
            {'date': '2026-10-15', 'nav': 1000.0}, {'date': '2026-10-16', 'nav': 1010.0},
            {'date': '2026-10-17', 'nav': 1010.0}, {'date': '2026-10-18', 'nav': 1010.0},
            {'date': '2026-10-19', 'nav': 1000.0}]}
        risk = me_mod.MeasurementEngine()._compute_risk_view(sp)
        assert risk['n_return_days'] == 2
        assert risk['nav_dropped']['non_trading_day'] == 2
        assert risk['max_drawdown_pct'] == pytest.approx((1000 / 1010 - 1) * 100, abs=0.01)
        assert risk['diagnostic_only'] is True


# ═════════════════════════════════════════════════════════════
# 3) 라이브 진단 — 절대 비차단
# ═════════════════════════════════════════════════════════════
def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')


_NOW = datetime(2026, 10, 16, 20, 0, 0)
_ALL_WEEKDAYS = lambda ds: datetime.strptime(ds[:10], '%Y-%m-%d').weekday() < 5  # noqa: E731


def _diag(res, **kw):
    from src.measurement.live_diagnostics import LiveDiagnostics
    return LiveDiagnostics(results_dir=res, cfg={}, now=_NOW, is_trading_day=_ALL_WEEKDAYS, **kw)


class TestLiveDiagnosticsNonBlocking:
    def test_empty_results_dir_is_insufficient_but_continue(self, tmp_path):
        out = _diag(tmp_path).run(write=True)
        assert out['decision'] == 'DIAGNOSE_CONTINUE' and out['blocking'] is False
        assert out['performance']['total']['status'] == 'insufficient_data'
        assert (tmp_path / 'live_diagnostics.json').exists()
        assert (tmp_path / 'live_diagnostics_report.md').exists()

    def test_adversarial_state_still_non_blocking_and_no_safety_writes(self, tmp_path):
        snaps = [{'date': (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat(),
                  'nav': 100.0 * (0.97 ** i)} for i in range(30)]
        _write(tmp_path / 'shadow_portfolio.json', {'daily_snapshots': snaps, 'trade_history': []})
        _write(tmp_path / 'kis_portfolio.json', {'timestamp': _NOW.isoformat(), 'account': {
            'total_equity': 50.0, 'cash': 1.0, 'positions': {'005930': {'eval_krw': 49.0, 'name': 'X'}}}})
        _write(tmp_path / 'kill_switch.json', {'triggered': True, 'can_buy': False})
        _write(tmp_path / 'overnight_market_ssot.json', {'status': 'DEGRADED_MISSING', 'vix': None})
        (tmp_path / 'signal_cache.json').write_text('{corrupt', encoding='utf-8')
        before = sorted(p.name for p in tmp_path.iterdir())
        out = _diag(tmp_path, record_nav=False).run(write=True)
        assert out['decision'] == 'DIAGNOSE_CONTINUE' and out['blocking'] is False
        ids = {i['id'] for i in out['issues']}
        assert 'RISK_DD_NEAR_KILL' in ids and 'DATA_OVERNIGHT_SSOT_DEGRADED' in ids
        assert all(i['auto_action'].startswith('none') for i in out['issues'])
        after = sorted(p.name for p in tmp_path.iterdir())
        new_files = set(after) - set(before)
        assert not any('kill' in f or 'EXIT_ONLY' in f or 'override' in f for f in new_files)
        assert not (tmp_path / 'EXIT_ONLY.flag').exists()

    def test_runner_exception_still_returns_continue(self, monkeypatch, tmp_path):
        from src.measurement import live_diagnostics as ld

        def boom(self, *a, **k):
            raise RuntimeError('synthetic failure')
        monkeypatch.setattr(ld.LiveDiagnostics, 'run', boom)
        out = ld.run_live_diagnostics(results_dir=tmp_path)
        assert out['decision'] == 'DIAGNOSE_CONTINUE' and out['blocking'] is False

    def test_go_nogo_wrapper_is_non_blocking(self, tmp_path):
        from scripts.go_nogo import GoNoGoEngine
        eng = GoNoGoEngine(results_dir=tmp_path, write=False)
        for res in (eng.evaluate(), eng.evaluate_meridian()):
            assert res['verdict'] == 'DIAGNOSE_CONTINUE' and res['blocking'] is False
            assert all(c['pass'] for c in res['criteria'].values())
        assert eng._calc_mdd([{'nav': 100}, {'nav': 80}, {'nav': 90}]) == pytest.approx(-20.0)

    def test_implementation_shortfall_from_real_fills(self, tmp_path):
        _write(tmp_path / 'shadow_trades' / '2026-10-16.json', [
            {'timestamp': '2026-10-16T09:00:00', 'orders': [
                {'stream_id': 'S1', 'ticker': '005930', 'direction': 'long', 'price': 100.0},
                {'stream_id': 'S1', 'ticker': '000660', 'action': 'sell', 'direction': 'sell', 'price': 200.0}]},
            {'timestamp': '2026-10-16T09:00:05', 'mode': 'live', 'n_orders': 2, 'n_filled': 0, 'n_rejected': 0,
             'fills': [], 'errors': []},
        ])
        _write(tmp_path / 'shadow_portfolio.json', {'trade_history': [
            {'date': '2026-10-16', 'timestamp': '2026-10-16T09:01:00', 'action': 'BUY', 'ticker': '005930',
             'quantity': 10, 'price': 101.0, 'market': 'KR', 'stream': 'S_RECON'},
            {'date': '2026-10-16', 'timestamp': '2026-10-16T09:02:00', 'action': 'SELL', 'ticker': '000660',
             'quantity': 1, 'price': 198.0, 'market': 'KR', 'stream': 'S_RECON'},
            {'date': '2026-10-16', 'timestamp': '2026-10-16T08:00:00', 'action': 'BUY', 'ticker': '035420',
             'quantity': 1, 'price': 50.0, 'market': 'KR', 'stream': 'S_RECON'},
        ]})
        out = _diag(tmp_path, record_nav=False).run(write=False)
        ex = out['execution']
        assert ex['n_broker_fills'] == 3 and ex['n_matched_fills'] == 2 and ex['n_unmatched_fills'] == 1
        # 매수 +100bp, 매도 (198/200-1)*-1 = +100bp → 평균 100bp (비용)
        assert ex['implementation_shortfall']['total']['mean_is_bps'] == pytest.approx(100.0)
        assert ex['implementation_shortfall']['by_stream']['S1']['n'] == 2

    def test_nav_history_accumulates_trading_close_marks_only(self, tmp_path):
        from src.measurement.live_diagnostics import LiveDiagnostics
        for ts, nav in (('2026-10-14T06:05:00', 100.0), ('2026-10-14T17:00:00', 101.0),
                        ('2026-10-15T06:05:00', 102.0), ('2026-10-15T11:00:00', 999.0),
                        ('2026-10-17T06:05:00', 103.0), ('2026-10-18T06:05:00', 555.0)):
            t = datetime.fromisoformat(ts)
            _write(tmp_path / 'kis_portfolio.json', {'timestamp': ts, 'account': {'total_equity': nav}})
            LiveDiagnostics(results_dir=tmp_path, cfg={}, now=t + timedelta(minutes=5),
                            is_trading_day=_ALL_WEEKDAYS).run(write=True)
        rows = json.loads((tmp_path / 'live_nav_history.json').read_text())['rows']
        got = {r['date']: r['nav'] for r in rows}
        # 10-13(화)←10-14 06:05, 10-14←17:00 및 10-15 06:05(마지막 우선), 11:00 장중 제외,
        # 10-16(금)←10-17 06:05, 10-17(토) 평가일은 제외
        assert got == {'2026-10-13': 100.0, '2026-10-14': 102.0, '2026-10-16': 103.0}

    def test_dynamic_mctr_risk_budget_position_limit_closed_form(self, tmp_path):
        from src.measurement.live_diagnostics import compute_dynamic_position_limit
        # 1. Cash-equivalent ETF (S5 / CD금리액티브): limit = 1.00
        ce = compute_dynamic_position_limit(
            '459580', {'name': 'KODEX CD금리액티브(합성)', 'stream_id': 'S5'}, base_anchor=0.15
        )
        assert ce['role'] == 'cash_equivalent'
        assert ce['limit'] == pytest.approx(1.00, rel=1e-9)

        # 2. Inverse hedge leg (114800 KODEX 인버스): limit = min(0.35, max(0.15, total_long_weight))
        inv = compute_dynamic_position_limit(
            '114800',
            {'name': 'KODEX 인버스', 'stream_id': 'S14_PAIR_ALPHA', 'strategy': 'kr_sector_pair_alpha'},
            base_anchor=0.15,
            total_long_weight=0.70,
        )
        assert inv['role'] == 'inverse_hedge'
        assert inv['limit'] == pytest.approx(0.35, rel=1e-9)

        # 3. Correlated directional equity (086280 paired at rho=0.7509, VIX=18.0 -> vix_factor=1.0)
        # corr_factor = 1 / sqrt(1 + 0.7509) = 0.755779573..., limit = round(0.15 * 0.755779573, 4) = 0.1134
        dir_eq = compute_dynamic_position_limit(
            '086280',
            {'name': '현대글로비스', 'stream_id': 'S3_B', 'daily_vol': 0.020},
            base_anchor=0.15,
            pair_max_corr=0.7509,
            vix_level=18.0,
        )
        assert dir_eq['role'] == 'directional_risk'
        assert dir_eq['limit'] == pytest.approx(round(0.15 / math.sqrt(1.7509), 4), rel=1e-9)

        # 4. Dynamic No-Trade Zone (Constantinides cube-root scaling):
        # vol = 8 * ref_vol (0.120 vs 0.015) -> (1 / 8)^(1/3) = 0.5 -> 0.01 * 0.5 = 0.005
        from src.allocation.portfolio_optimizer import TransactionCostFilter
        ntz = TransactionCostFilter.compute_dynamic_no_trade_zone(0.01, 0.0015, stream_daily_vol=0.120)
        assert ntz == pytest.approx(0.005, rel=1e-9)

        # 5. Date-only broker fill matching + ticker-only DATA_NOGO isolation
        _write(tmp_path / 'shadow_trades' / '2026-10-16.json', [
            {'timestamp': '2026-10-16T09:15:00', 'orders': [
                {'stream_id': 'S3_B', 'ticker': '000270', 'action': 'buy', 'signal_price': 105000.0, 'price': 105000.0},
            ]},
        ])
        _write(tmp_path / 'shadow_portfolio.json', {'trade_history': [
            {'date': '2026-10-16', 'action': 'BUY', 'ticker': '000270', 'quantity': 2.0, 'price': 105525.0, 'stream': 'S_RECON'},
        ]})
        _write(tmp_path / 'data_nogo.json', {
            'entries': {
                'ticker:012510': {
                    'asset': '012510',
                    'kind': 'ticker',
                    'reason': 'KIS 거래정지',
                    'date': '2026-10-16',
                    'recorded_at': '2026-10-16T09:00:00',
                    'expires_at': '2026-11-16T09:00:00',
                }
            }
        })
        out_date_only = _diag(tmp_path, record_nav=False).run(write=False)
        assert out_date_only['execution']['n_matched_fills'] == 1
        # (105525 / 105000 - 1) * 1e4 = +50.0 bps
        assert out_date_only['execution']['implementation_shortfall']['total']['mean_is_bps'] == pytest.approx(50.0, rel=1e-9)
        assert out_date_only['execution']['implementation_shortfall']['by_stream']['S3_B']['n'] == 1
        issue_map = {i['id']: i['severity'] for i in out_date_only['issues']}
        assert issue_map.get('DATA_NOGO_TICKER_ISOLATED') == 'info'
        assert 'DATA_NOGO_ACTIVE' not in issue_map


# ═════════════════════════════════════════════════════════════
# 4) GapFeedback — 경계/일일한도/tighten-only + 실제 적용
# ═════════════════════════════════════════════════════════════
class TestGapFeedbackBounded:
    def test_bound_rules(self):
        from scripts.gap_feedback import bound_adjustment
        # risk 파라미터 완화(손절폭 확대) 거부
        assert bound_adjustment('exit.sl_atr_multiplier', 1.8, 2.2)['status'] == 'rejected_loosen'
        # 하드 하한 클램프 + 일일 10% 제한
        r = bound_adjustment('exit.sl_atr_multiplier', 1.3, 0.5)
        assert r['status'] == 'clamped' and r['new'] == pytest.approx(1.2)
        r = bound_adjustment('exit.sl_atr_multiplier', 2.0, 1.2)
        assert r['new'] == pytest.approx(1.8)  # 2.0 * (1 - 10%)
        # 당일 기준선 대비 누적 제한
        r = bound_adjustment('exit.sl_atr_multiplier', 1.8, 1.6, baseline=2.0)
        assert r['new'] == pytest.approx(1.8) and r['status'] == 'noop'
        # 와일드카드 스트림 스케일: 하한 이하에서 출발해도 완화(상향) 금지
        assert bound_adjustment('sizer.stream_scale.S3', 0.4, 0.5)['status'] == 'rejected_loosen'
        r = bound_adjustment('sizer.stream_scale.S3', 0.4, 0.3)
        assert r['status'] == 'noop' and r['new'] == pytest.approx(0.4)
        # signal: min_confidence 하향(완화) 거부, 상향은 하드 상한/일일 5%
        assert bound_adjustment('s2.min_confidence', 0.6, 0.55)['status'] == 'rejected_loosen'
        assert bound_adjustment('s2.min_confidence', 0.60, 0.70)['new'] == pytest.approx(0.63)
        # 화이트리스트 외 키 거부
        assert bound_adjustment('risk.total_dd_limit', -0.1, -0.2)['status'] == 'rejected_unknown_key'

    def test_apply_writes_dynamic_overrides_and_audit(self, tmp_path, monkeypatch):
        import scripts.gap_feedback as gf
        ov = tmp_path / 'dynamic_overrides.json'
        au = tmp_path / 'gap_feedback_audit.jsonl'
        ov.write_text(json.dumps({'exit.sl_atr_multiplier': 1.8, 'other.key': 7}), encoding='utf-8')
        monkeypatch.setattr(gf, '_OVERRIDES_FILE', ov)
        monkeypatch.setattr(gf, '_AUDIT_FILE', au)
        eng = gf.GapFeedbackEngine()
        eng.dry_run = False
        eng.adjustments_made = [
            {'rule': 'consecutive_losses', 'key': 'exit.sl_atr_multiplier', 'old': 1.8, 'new': 1.7},
            {'rule': 'large_loss_cluster', 'key': 'exit.sl_atr_multiplier', 'old': 1.7, 'new': 1.4},
            {'rule': 'stream_concentration', 'key': 'sizer.stream_scale.S1', 'old': 0.4, 'new': 0.5},
            {'rule': 'x', 'key': 'risk.total_dd_limit', 'old': -0.1, 'new': -0.3},
        ]
        eng._apply_adjustments()
        data = json.loads(ov.read_text())
        # 1.7 적용 후 1.4 요청 → 당일 기준선 1.8 대비 -10% = 1.62 로 클램프
        assert data['exit.sl_atr_multiplier'] == pytest.approx(1.62)
        assert data['other.key'] == 7  # 다른 키 보존
        assert 'sizer.stream_scale.S1' not in data and 'risk.total_dd_limit' not in data
        assert data['_updated_by'] == 'gap_feedback'
        lines = [json.loads(x) for x in au.read_text().splitlines()]
        assert [r['status'] for r in lines] == ['applied', 'clamped', 'rejected_loosen', 'rejected_unknown_key']

    def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        import scripts.gap_feedback as gf
        ov = tmp_path / 'dynamic_overrides.json'
        monkeypatch.setattr(gf, '_OVERRIDES_FILE', ov)
        monkeypatch.setattr(gf, '_AUDIT_FILE', tmp_path / 'a.jsonl')
        eng = gf.GapFeedbackEngine()
        eng.dry_run = True
        eng.adjustments_made = [{'rule': 'r', 'key': 'exit.sl_atr_multiplier', 'old': 2.0, 'new': 1.9}]
        eng._apply_adjustments()
        assert not ov.exists()
        assert eng.apply_report[0]['status'] == 'applied' and eng.apply_report[0]['dry_run'] is True

    def test_target_is_the_file_dynamic_config_reads(self):
        import scripts.gap_feedback as gf
        import config.dynamic_config as dc
        assert Path(gf._OVERRIDES_FILE).resolve() == Path(dc._OVERRIDES_FILE).resolve()


# ═════════════════════════════════════════════════════════════
# 5) DATA_NOGO 영속화 + 오케스트레이터 의미론
# ═════════════════════════════════════════════════════════════
class TestDataNogoPersistence:
    def test_round_trip_and_ttl(self, tmp_path):
        from src.measurement.data_nogo_store import record_data_nogo, load_active_data_nogo
        p = tmp_path / 'data_nogo.json'
        t0 = datetime(2026, 10, 19, 6, 30)
        record_data_nogo('gscpi', 'R2 0.42 < 0.9', kind='macro', ttl_hours=20, path=p, now=t0)
        record_data_nogo('005930', 'stale quote', kind='ticker', ttl_hours=2, path=p, now=t0)
        a = load_active_data_nogo(path=p, now=t0 + timedelta(hours=1))
        assert a['macro'] == ['gscpi'] and a['ticker'] == ['005930'] and not a['corrupt']
        b = load_active_data_nogo(path=p, now=t0 + timedelta(hours=3))
        assert b['macro'] == ['gscpi'] and b['ticker'] == []
        c = load_active_data_nogo(path=p, now=t0 + timedelta(hours=21))
        assert c['macro'] == [] and c['ticker'] == []
        obj = json.loads(p.read_text())
        assert obj['entries']['macro:gscpi']['date'] == '2026-10-19'

    def test_corrupt_store_fails_closed(self, tmp_path):
        from src.measurement.data_nogo_store import apply_data_nogo_to_run
        p = tmp_path / 'data_nogo.json'
        p.write_text('{not json', encoding='utf-8')
        md, info = apply_data_nogo_to_run({}, path=p)
        assert info['store_corrupt'] is True and md['exit_only'] is True

    def test_orchestrator_semantics_exit_only_not_halt(self, tmp_path):
        from src.measurement.data_nogo_store import record_data_nogo, apply_data_nogo_to_run
        p = tmp_path / 'data_nogo.json'
        md, info = apply_data_nogo_to_run({'x': 1}, path=p)
        assert info['active'] is False and 'exit_only' not in md
        t0 = datetime.now()
        record_data_nogo('005930', 'bad', kind='ticker', ttl_hours=5, path=p, now=t0)
        md, info = apply_data_nogo_to_run({}, path=p)
        assert info['mode'] == 'ticker_entry_block' and md['data_nogo_tickers'] == ['005930']
        assert 'exit_only' not in md
        record_data_nogo('hy_spread', 'R2', kind='macro', ttl_hours=5, path=p, now=t0)
        md, info = apply_data_nogo_to_run({}, path=p)
        assert info['mode'] == 'exit_only' and md['exit_only'] is True
        md, info = apply_data_nogo_to_run({}, legacy_flag=True, legacy_reason='orch.data_nogo',
                                          path=tmp_path / 'none.json')
        assert md['exit_only'] is True and 'legacy:orch.data_nogo' in info['reason']

    def test_orchestrator_wired_to_store_and_never_halts(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        src = inspect.getsource(StreamOrchestrator.run)
        assert 'apply_data_nogo_to_run' in src
        assert "'halted_data_nogo'" not in src

    def test_exit_only_market_data_removes_buys_keeps_sells(self):
        from scripts.stream_orchestrator import StreamOrchestrator
        orch = object.__new__(StreamOrchestrator)
        orders = [{'action': 'buy', 'ticker': '005930', 'quantity': 1, 'price': 100},
                  {'action': 'sell', 'ticker': '000660', 'quantity': 1, 'price': 100}]
        kept = orch._apply_final_execution_gate(orders, {'cash': 1e9}, {'exit_only': True})
        assert [o['action'] for o in kept] == ['sell']

    def test_pipeline_producers_and_consumers_use_store(self):
        root = Path(__file__).resolve().parent.parent
        sp = (root / 'scripts' / 'pipeline' / 'sub_phases.py').read_text(encoding='utf-8')
        dp = (root / 'scripts' / 'daily_pipeline.py').read_text(encoding='utf-8')
        assert 'record_data_nogo(' in sp
        assert 'load_active_data_nogo' in dp
        assert 'from scripts.go_nogo import GoNoGoEngine' not in sp
        assert 'run_live_diagnostics' in sp

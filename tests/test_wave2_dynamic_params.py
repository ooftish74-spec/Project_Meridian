"""
Wave 2 Track F — dynamic parameter architecture, ticker master, hardcoding ratchet.

  * DynamicParameterService: quality decay, stale → L1 fallback, fail_closed, tighten_only, bounds clamp,
    L0 immutability, ctx-templated config keys, shadow mode (default) never changes live values.
  * Registry: schema valid; defaults within bounds; current DynamicConfig values within bounds (no clamp =
    behavior-preserving); every ``pget(name, fallback)`` call site's fallback == registry default.
  * Ticker master: KIS fixed-width (KOSPI/KOSDAQ) + TSV (US) parsing from fixture bytes, leverage parsing,
    fresh / stale / missing cache behaviour, legacy-set preservation for overnight & exit classification.
  * Hardcoding scanner: category counting, pragma, registry-bound pget, ratchet --check vs committed baseline.
"""
from __future__ import annotations

import ast
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────
def _reg(params, tmp_path, name='reg.json'):
    p = tmp_path / name
    p.write_text(json.dumps({'version': 1, 'global': {'cache_ttl_sec': 0, 'audit_throttle_sec': 0},
                             'params': params}), encoding='utf-8')
    return p


def _l2(**over):
    spec = {'level': 'L2', 'default': 30.0, 'bounds': [15.0, 50.0], 'policy': 'tighten_only', 'tighten': 'down',
            'estimator': {'type': 'rolling_quantile', 'window': 10, 'q': 0.5}, 'source': 'test_src',
            'tau_hours': 24.0, 'q_min': 0.5, 'min_obs': 5, 'rationale': 'test'}
    spec.update(over)
    return spec


def _svc(tmp_path, params, cfg=None, values=None, asof=NOW, mode='live'):
    from src.params.dynamic_parameter_service import DynamicParameterService
    from src.params.estimators import SeriesSnapshot
    cfg = cfg or {}
    src = {'test_src': (lambda: SeriesSnapshot(tuple(values), asof, 'test')) if values is not None else (lambda: None)}
    return DynamicParameterService(_reg(params, tmp_path), audit_path=tmp_path / 'audit.jsonl',
                                   config_getter=lambda k, d: cfg.get(k, d), clock=lambda: NOW,
                                   l2_mode=mode, sources=src, audit_throttle_sec=0)


# ─────────────────────────────────────────────────────────────
# 1. DynamicParameterService
# ─────────────────────────────────────────────────────────────
def test_quality_decay_math():
    from src.params import quality_from_age
    assert quality_from_age(0, 24) == 1.0
    assert quality_from_age(24, 24) == pytest.approx(math.exp(-1))
    assert quality_from_age(48, 24) == pytest.approx(math.exp(-2))
    assert quality_from_age(-5, 24) == 1.0  # clock skew never inflates above 1


def test_l2_fresh_tightens_and_reports_quality(tmp_path):
    vals = [20.0] * 10  # median 20 < L1 30 → tighter (down)
    s = _svc(tmp_path, {'x': _l2()}, values=vals, asof=NOW - timedelta(hours=6))
    pv = s.get('x')
    assert pv.value == 20.0 and pv.level == 'L2' and pv.reason == 'l2_tightened'
    assert pv.quality == pytest.approx(math.exp(-6 / 24), rel=1e-5)
    assert pv.l1_value == 30.0 and pv.l2_estimate == 20.0


def test_l2_stale_falls_back_to_l1(tmp_path):
    s = _svc(tmp_path, {'x': _l2()}, values=[20.0] * 10, asof=NOW - timedelta(hours=48))  # q = e^-2 < 0.5
    pv = s.get('x')
    assert pv.value == 30.0 and pv.level == 'L1'
    assert 'l2_stale_fallback_l1' in pv.reason
    rec = [json.loads(l) for l in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert rec and rec[-1]['name'] == 'x' and 'stale' in rec[-1]['reason']


def test_l2_missing_and_insufficient_obs_fall_back(tmp_path):
    assert _svc(tmp_path, {'x': _l2()}, values=None).get('x').reason.endswith('l2_missing_fallback_l1')
    pv = _svc(tmp_path, {'x': _l2()}, values=[20.0, 21.0]).get('x')  # n=2 < min_obs 5
    assert pv.value == 30.0 and 'insufficient_obs' in pv.reason


def test_fail_closed_returns_none_only_in_live_mode(tmp_path):
    p = {'x': _l2(fail_closed=True)}
    assert _svc(tmp_path, p, values=[20.0] * 10, asof=NOW - timedelta(days=10)).get('x').value is None
    # shadow mode must never block (behavior-preserving rollout)
    assert _svc(tmp_path, p, values=[20.0] * 10, asof=NOW - timedelta(days=10), mode='shadow').get('x').value == 30.0


def test_tighten_only_rejects_loosening(tmp_path):
    s = _svc(tmp_path, {'x': _l2()}, values=[45.0] * 10)  # estimate 45 > L1 30 → looser for 'down'
    pv = s.get('x')
    assert pv.value == 30.0 and pv.reason == 'l2_not_tighter_keep_l1'
    s2 = _svc(tmp_path, {'y': _l2(tighten='up', default=20.0)}, values=[25.0] * 10)
    assert s2.get('y').value == 25.0  # 'up' policy accepts higher


def test_free_policy_and_bounds_clamp_on_estimate(tmp_path):
    s = _svc(tmp_path, {'x': _l2(policy='free', tighten=None)}, values=[5.0] * 10)
    pv = s.get('x')
    assert pv.value == 15.0 and pv.level == 'L2'  # clamped to lower bound


def test_shadow_mode_never_changes_value(tmp_path):
    s = _svc(tmp_path, {'x': _l2()}, values=[20.0] * 10, mode='shadow')
    pv = s.get('x')
    assert pv.value == 30.0 and pv.l2_estimate == 20.0 and pv.reason.startswith('l2_shadow')


def test_l1_config_key_bounds_clamp_and_zero_is_missing(tmp_path):
    base = {'level': 'L1', 'default': 0.5, 'bounds': [0.0, 1.0], 'config_key': 'k', 'rationale': 't'}
    assert _svc(tmp_path, {'a': base}, cfg={'k': 0.7}).get('a').value == 0.7
    pv = _svc(tmp_path, {'a': base}, cfg={'k': 3.0}).get('a')
    assert pv.value == 1.0 and 'l1_clamped_to_bounds' in pv.reason
    assert _svc(tmp_path, {'a': base}, cfg={}).get('a').value == 0.5
    assert _svc(tmp_path, {'a': {**base, 'zero_is_missing': True}}, cfg={'k': 0}).get('a').value == 0.5
    assert _svc(tmp_path, {'a': base}, cfg={'k': 0}).get('a').value == 0.0


def test_l0_is_immutable_and_ctx_template(tmp_path):
    from src.params import ParamRegistryError, validate_registry
    l0 = {'level': 'L0', 'default': 18.0, 'bounds': [0.0, 50.0], 'rationale': 'tax'}
    assert _svc(tmp_path, {'tax': l0}, cfg={'tax': 99.0}).get('tax').value == 18.0
    assert validate_registry({'params': {'tax': {**l0, 'config_key': 'tax'}}})  # L0 + config_key rejected
    t = {'level': 'L1', 'default': 0.35, 'bounds': [0.0, 5.0], 'config_key': 'ev.{regime}', 'rationale': 't'}
    s = _svc(tmp_path, {'ev': t}, cfg={'ev.bear': 0.5})
    assert s.get('ev', {'regime': 'bear'}).value == 0.5
    assert s.get('ev', {'regime': 'bull'}).value == 0.35
    assert s.get('ev').value == 0.35  # missing ctx → default
    with pytest.raises(ParamRegistryError):
        _svc(tmp_path, {'bad': {'level': 'L1', 'default': 2.0, 'bounds': [0.0, 1.0], 'rationale': 't'}})


def test_validate_registry_catches_schema_errors():
    from src.params import validate_registry
    bad = {'params': {
        'a': {'level': 'L9', 'default': 1.0, 'bounds': [0, 2], 'rationale': 'x'},
        'b': {'level': 'L1', 'default': 1.0, 'bounds': [2, 0], 'rationale': 'x'},
        'c': {'level': 'L1', 'default': 1.0, 'bounds': [0, 2], 'policy': 'tighten_only', 'rationale': 'x'},
        'd': {'level': 'L2', 'default': 1.0, 'bounds': [0, 2], 'rationale': 'x'},
        'e': {'level': 'L1', 'default': 1.0, 'bounds': [0, 2]},
    }}
    errs = ' | '.join(validate_registry(bad))
    for frag in ('a: level', 'b: bounds', 'c: tighten_only', 'd: L2 requires estimator', 'e: rationale'):
        assert frag in errs


# ─────────────────────────────────────────────────────────────
# 2. Production registry invariants
# ─────────────────────────────────────────────────────────────
def _registry():
    return json.loads((_ROOT / 'config' / 'params' / 'parameter_registry.json').read_text(encoding='utf-8'))


def test_registry_valid_and_defaults_within_bounds():
    from src.params import validate_registry
    reg = _registry()
    assert validate_registry(reg) == []
    for name, spec in reg['params'].items():
        lo, hi = spec['bounds']
        assert lo <= spec['default'] <= hi, name
        if spec['level'] == 'L2':
            assert spec.get('policy') == 'tighten_only' or 'free' in spec['rationale'].lower(), name


def test_current_config_values_inside_bounds_no_live_clamp():
    """Behavior preservation: wiring the service must not clamp any value DynamicConfig serves today."""
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
    for name, spec in _registry()['params'].items():
        ck = spec.get('config_key')
        if not ck:
            continue
        keys = [ck.format(regime=r) for r in ('bull', 'caution', 'bear', 'crash')] if '{regime}' in ck else [ck]
        for k in keys:
            v = cfg.get(k, None)
            if v is None or isinstance(v, bool):
                continue
            lo, hi = spec['bounds']
            assert lo <= float(v) <= hi, f"{name}: DynamicConfig[{k}]={v} outside {spec['bounds']}"


def _pget_calls():
    out = []
    for root in ('src', 'scripts'):
        for p in (_ROOT / root).rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, 'id', getattr(n.func, 'attr', '')) == 'pget' \
                        and len(n.args) >= 2 and isinstance(n.args[0], ast.Constant):
                    out.append((str(p.relative_to(_ROOT)), n.lineno, n.args[0].value, n.args[1]))
    return out


def test_every_pget_call_site_fallback_equals_registry_default():
    params = _registry()['params']
    calls = _pget_calls()
    assert len(calls) >= 50
    for f, ln, name, node in calls:
        assert name in params, f"{f}:{ln} pget('{name}') not registered"
        val = ast.literal_eval(node)
        assert float(val) == float(params[name]['default']), f"{f}:{ln} {name}: fallback {val} != registry"


def test_pget_uses_fallback_when_registry_broken(tmp_path, monkeypatch):
    import src.params.dynamic_parameter_service as dps
    monkeypatch.setattr(dps, 'REGISTRY_PATH', tmp_path / 'missing.json')
    dps.reset_param_service(None)
    try:
        assert dps.pget('sizer.half_kelly_factor', 0.5) == 0.5
    finally:
        dps.reset_param_service(None)


def test_migrated_call_sites_resolve_to_pre_migration_values(tmp_path):
    """Real registry + real DynamicConfig: L1 values equal what the old `cfg.get(key, literal)` produced."""
    from config.dynamic_config import DynamicConfig
    from src.params.dynamic_parameter_service import DynamicParameterService
    cfg = DynamicConfig()
    s = DynamicParameterService(audit_path=tmp_path / 'a.jsonl', l2_mode='shadow')
    for name, spec in s.params.items():
        ck = spec.get('config_key')
        if not ck or '{' in ck:
            continue
        old = cfg.get(ck, spec['default'])
        if spec.get('zero_is_missing'):
            old = old or spec['default']
        assert s.value(name) == pytest.approx(float(old)), name
    assert s.value('allocator.min_net_ev_pct', {'regime': 'crash'}) == pytest.approx(
        float(cfg.get('allocator.min_net_ev_pct.crash', 0.35) or 0.35))


def test_position_sizer_friction_unchanged():
    from src.execution.position_sizer import PositionSizer
    ps = PositionSizer()
    ps._slippage = None
    f = ps._compute_friction({'stream_id': 'S2'}, 0.1, 0.0, 'caution', 0.0, 0.0, '005930')
    assert f == pytest.approx((5.0 * 2 + 1.5 * 2 + 18.0) / 100.0)  # pre-migration literals
    f_etf = ps._compute_friction({'stream_id': 'S1'}, 0.1, 0.0, 'caution', 0.0, 0.0, '069500')
    assert f_etf == pytest.approx((5.0 * 2 + 1.5 * 2) / 100.0)


# ─────────────────────────────────────────────────────────────
# 3. Ticker master
# ─────────────────────────────────────────────────────────────
# Two real KOSPI master lines (KIS kospi_code.mst, 2026-10-19) + synthesized lines with the same layout.
_KOSPI_REAL = [
    b'005930   KR7005930003\xbb\xef\xbc\xba\xc0\xfc\xc0\xda                                ST1002700130000 NN5YYY YYNNNNNNN0NNNNNNNY0002155000000100001NNN00NNN000000040Y0900000189085600000000001001975061100000000584627800000000077804668500012       0 NYY00305372900146725200153268211884000031.3920260630012598730511NNY',
    b'252670   KR7252670005KODEX 200\xbc\xb1\xb9\xb0\xc0\xce\xb9\xf6\xbd\xba2X                   EF 000000000000NN 0NNN2NN    N  0  N     0000001060000100001NNN00NNN000000100N090-011746562520000000000002016092200000000900250000000000000000000000012       0 NNN00000000000000000000000000000000000000.00        000009542   NNN',
]


def _kr_line(code, name, group, width, halted_off, halted=False):
    p2 = bytearray((group + ' ' * (width - 2)).encode('ascii'))
    if halted:
        p2[halted_off] = ord('Y')
    return code.ljust(9).encode() + ('KR7' + code + '000').ljust(12).encode() + name.encode('cp949').ljust(40) + bytes(p2)


def _kospi_bytes():
    lines = list(_KOSPI_REAL) + [
        _kr_line('122630', 'KODEX 레버리지', 'EF', 227, 60),
        _kr_line('114800', 'KODEX 인버스', 'EF', 227, 60),
        _kr_line('0193L0', 'PLUS 삼성전자선물단일종목인버스2X', 'EF', 227, 60),
        _kr_line('Q520077', '미래에셋 -1.5X 천연가스 선물 ETN', 'EN', 227, 60),
        _kr_line('000020', '동화약품', 'ST', 227, 60, halted=True),
    ]
    return b'\r\n'.join(lines) + b'\r\n'


def _kosdaq_bytes():
    return b'\n'.join([_kr_line('247540', '에코프로비엠', 'ST', 221, 55),
                       _kr_line('900110', '딥커머스', 'FS', 221, 55)]) + b'\n'


_US_ROWS = [
    ['US', '22', 'NAS', '나스닥', 'TQQQ', 'NASTQQQ', 'PROSHARES QQQ 3X', 'PROSHARES TRUST ULTRAPRO QQQ USD', '3', 'USD', '4', '', '73.82', '1', '1', '930', '1600', 'N', '', '000', '0', '0', '001', '   '],
    ['US', '22', 'NAS', '나스닥', 'SQQQ', 'NASSQQQ', 'PROSHARES QQQ -3X', 'PROSHARES ULTRAPRO SHORT QQQ', '3', 'USD', '4', '', '36.14', '1', '1', '930', '1600', 'N', '', '000', '0', '0', '001', '   '],
    ['US', '22', 'NAS', '나스닥', 'AAPL', 'NASAAPL', '애플', 'APPLE INC', '2', 'USD', '4', '', '342.0', '1', '1', '930', '1600', 'N', '', '730', '0', '0', '   ', '   '],
    ['US', '23', 'AMS', '아멕스', 'SH', 'AMSSH', 'PROSHARES S&P 500 -1X', 'PROSHARES SHORT S&P 500', '3', 'USD', '4', '', '32.09', '1', '1', '930', '1600', 'N', '', '000', '0', '0', '001', '   '],
    ['US', '23', 'AMS', '아멕스', 'BSV', 'AMSBSV', 'VANGUARD SHORT TERM BOND', 'VANGUARD BOND INDEX FUNDS SHORT-TERM BON', '3', 'USD', '4', '', '78.1', '1', '1', '930', '1600', 'N', '', '000', '0', '0', '001', '   '],
]


def _us_bytes(rows):
    return '\n'.join('\t'.join(r) for r in rows).encode('cp949') + b'\n'


def _zip(name, raw):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr(name, raw)
    return buf.getvalue()


def test_parse_kr_master_fixed_width_from_fixture_bytes():
    from src.data_collection.ticker_master import parse_kr_master
    rows = {r.code: r for r in parse_kr_master(_zip('kospi_code.mst', _kospi_bytes()), 'KOSPI', '2026-10-19')}
    sam = rows['005930']
    assert sam.name == '삼성전자' and sam.market == 'KOSPI' and not sam.is_etf and sam.leverage == 1.0
    assert sam.listed_date == '19750611' and sam.listed is True and sam.halted is False
    inv2 = rows['252670']
    assert inv2.name == 'KODEX 200선물인버스2X' and inv2.is_etf and inv2.leverage == -2.0
    assert inv2.underlying == 'KOSPI200' and inv2.listed_date == '20160922'
    assert rows['122630'].leverage == 2.0 and rows['114800'].leverage == -1.0
    assert rows['0193L0'].leverage == -2.0 and rows['0193L0'].underlying == '005930'
    etn = rows['Q520077']
    assert etn.market == 'ETN' and etn.is_etn and not etn.is_etf and etn.leverage == -1.5
    assert rows['000020'].halted is True
    kq = {r.code: r for r in parse_kr_master(_kosdaq_bytes(), 'KOSDAQ')}
    assert kq['247540'].name == '에코프로비엠' and kq['247540'].market == 'KOSDAQ'


def test_parse_us_master_tsv_from_fixture_bytes():
    from src.data_collection.ticker_master import parse_us_master
    rows = {r.code: r for r in parse_us_master(_zip('NASMST.COD', _us_bytes(_US_ROWS)), 'NAS')}
    assert rows['TQQQ'].leverage == 3.0 and rows['TQQQ'].underlying == 'NASDAQ100' and rows['TQQQ'].is_etf
    assert rows['SQQQ'].leverage == -3.0
    assert rows['AAPL'].leverage == 1.0 and not rows['AAPL'].is_etf and rows['AAPL'].name_en == 'APPLE INC'
    assert rows['SH'].leverage == -1.0 and rows['SH'].underlying == 'SP500'
    assert rows['BSV'].leverage == 1.0  # 'SHORT-TERM' bond fund is not inverse


@pytest.mark.parametrize('name,lev', [
    ('KODEX 레버리지', 2.0), ('KODEX 인버스', -1.0), ('KODEX 200선물인버스2X', -2.0),
    ('TIGER 미국나스닥100레버리지(합성)', 2.0), ('메리츠 3X 레버리지 국채10년 ETN', 3.0),
    ('메리츠 인버스 3X 국채10년 ETN', -3.0), ('삼성 인버스0.5X S&P500 VIX S/T선물 ETN B', -0.5),
    ('미래에셋 -1.5X 천연가스 선물 ETN', -1.5), ('KODEX 200', 1.0), ('TIGER 2차전지테마', 1.0),
    ('RISE 삼성전자단일종목레버리지', 2.0),
])
def test_parse_kr_leverage(name, lev):
    from src.data_collection.ticker_master import parse_kr_leverage
    assert parse_kr_leverage(name) == lev


@pytest.mark.parametrize('kor,eng,lev', [
    ('PROSHARES QQQ 3X', 'PROSHARES TRUST ULTRAPRO QQQ', 3.0),
    ('X', 'PROSHARES ULTRAPRO SHORT QQQ', -3.0), ('X', 'PROSHARES ULTRASHORT QQQ', -2.0),
    ('X', 'PROSHARES ULTRA QQQ', 2.0), ('X', 'DIREXION DAILY SEMICONDUCTOR BEAR 3X ETF', -3.0),
    ('PROSHARES VIX SHORT TERM FUTURES -0.5X', 'X', -0.5), ('X', 'JP MORGAN ULTRA SHORT INCOME ETF', 1.0),
    ('X', 'INNOVATOR US EQUITY ULTRA BUFFER - APR', 1.0), ('X', 'MILITIA LONG/SHORT EQUITY ETF', 1.0),
    ('X', 'STATE STREET SPDR S&P 500 ETF', 1.0),
])
def test_parse_us_leverage(kor, eng, lev):
    from src.data_collection.ticker_master import parse_us_leverage
    assert parse_us_leverage(kor, eng) == lev


def _write_cache(tmp_path, as_of):
    from src.data_collection.ticker_master import build_master
    data = build_master({'KOSPI': _kospi_bytes()}, {'NAS': _us_bytes(_US_ROWS)}, as_of=as_of.isoformat())
    p = tmp_path / 'ticker_master.json'
    p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return p


def test_ticker_master_fresh_lookup_api(tmp_path):
    from src.data_collection.ticker_master import TickerMaster
    tm = TickerMaster(_write_cache(tmp_path, NOW - timedelta(hours=1)), ttl_hours=24, clock=lambda: NOW)
    assert tm.is_fresh() and not tm.degraded
    assert tm.is_leveraged('TQQQ') and not tm.is_inverse('TQQQ') and tm.leverage('TQQQ') == 3.0
    assert tm.is_inverse('114800') and not tm.is_leveraged('114800')
    assert tm.leverage('5930') == 1.0 and tm.name('005930') == '삼성전자'  # zero-padding
    assert tm.get('005930').source == 'master'
    # structural code missing from a fresh KRX master → delisted, but leverage fact kept
    info = tm.get('233740')
    assert info.listed is False and info.leverage == 2.0
    assert tm.master_leverage('252670') == -2.0


def test_ticker_master_stale_and_missing_are_degraded(tmp_path):
    from src.data_collection.ticker_master import TickerMaster
    stale = TickerMaster(_write_cache(tmp_path, NOW - timedelta(days=30)), ttl_hours=24, clock=lambda: NOW)
    assert stale.degraded and stale.get('TQQQ').source == 'master_stale'
    assert stale.master_leverage('TQQQ') is None  # opt-in extensions need a FRESH master
    missing = TickerMaster(tmp_path / 'nope.json', ttl_hours=24, clock=lambda: NOW)
    st = missing.status()
    assert st['degraded'] and st['error'] == 'cache_missing'
    assert missing.leverage('252670') == -2.0 and missing.get('252670').source == 'structural'
    assert missing.is_leveraged('SOXL') and missing.leverage('005930') is None
    # legacy delisted code is canonicalized (ticker_canonical) before lookup
    assert missing._norm('430740') == '423160'


def test_overnight_classification_preserves_legacy_sets(tmp_path, monkeypatch):
    import src.data_collection.ticker_master as tmm
    legacy_bull = {'233740', '122630', 'TQQQ', 'SOXL', 'UPRO', 'QLD', 'SSO'}
    legacy_bear = {'252670', '114800', 'SQQQ', 'SOXS', 'SPXS', 'QID', 'SDS'}
    assert set(tmm.OVERNIGHT_BULL_ETPS) == legacy_bull and set(tmm.OVERNIGHT_BEAR_ETPS) == legacy_bear
    assert set(tmm.EXIT_GROUP1_ETPS) == {'252670', '233740', 'TQQQ', 'SOXL', 'SQQQ'}
    for t in legacy_bull:
        assert tmm.overnight_etp_direction(t) == 'bull'
    for t in legacy_bear:
        assert tmm.overnight_etp_direction(t) == 'bear'
    # default: extension OFF → non-legacy inverse ETP stays unclassified (identical behavior)
    tmm.reset_ticker_master(tmm.TickerMaster(_write_cache(tmp_path, NOW), ttl_hours=1e9))
    try:
        assert tmm.overnight_etp_direction('0193L0') is None
        import src.params as sp
        monkeypatch.setattr(sp, 'pget', lambda name, fb, ctx=None: 1 if name.endswith('extend_overnight_classification') else fb)
        assert tmm.overnight_etp_direction('0193L0') == 'bear'
        assert tmm.overnight_etp_direction('005930') is None
    finally:
        tmm.reset_ticker_master(None)


def test_execution_engine_overnight_matrix_unchanged():
    from src.execution.execution_engine import ExecutionEngine
    e = ExecutionEngine.__new__(ExecutionEngine)
    e.fast_switch = None
    e.decay_shield = None
    assert e.check_etp_overnight_allowed('TQQQ', regime='bull') is True
    assert e.check_etp_overnight_allowed('SQQQ', regime='bull') is False
    assert e.check_etp_overnight_allowed('114800', regime='bear') is True
    assert e.check_etp_overnight_allowed('005930', regime='sideways') is True
    assert e.check_etp_overnight_allowed('SOXL', regime='sideways') is False


# ─────────────────────────────────────────────────────────────
# 4. Hardcoding scanner + ratchet
# ─────────────────────────────────────────────────────────────
def _scanner():
    import importlib.util
    spec = importlib.util.spec_from_file_location('hardcoding_scan', _ROOT / 'scripts' / 'ops' / 'hardcoding_scan.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scanner_counts_categories():
    hs = _scanner()
    src = '''
"""doc 005930"""
X = {'005930', '0193L0', 'TQQQ', 'BUY'}
T = '090000'
def f(cfg, v):
    """122630"""
    a = cfg.get('k', 0.35) or 0.7
    b = _cfg_get('k', 12.0)
    c = cfg.get('n', 0) or 1
    if v > 0.25 and v < 1:
        return min(v, 0.9)
    s = f"{v} 252670"
    p = pget('x', 0.5)
    return getattr(cfg, 'z', 3.5)
OK = {'252670': 1}  # hardcode-ok: SSOT table
'''
    r = hs.scan_source(src)
    c = r['counts']
    assert c['kr_ticker_literals'] == 2   # 005930, 0193L0 (docstrings / f-string / time / pragma excluded)
    assert c['us_ticker_literals'] == 1   # TQQQ ('BUY' is not a ticker)
    assert c['fallback_literals'] == 4    # .get 0.35, or 0.7, _cfg_get 12.0, getattr 3.5 (pget + trivials excluded)
    assert c['threshold_literals'] == 2   # > 0.25, min(…, 0.9)
    assert r['allowed'] == 1


def test_scanner_ratchet_detects_growth(tmp_path):
    hs = _scanner()
    base = {'totals': {'kr_ticker_literals': 1, 'us_ticker_literals': 0, 'threshold_literals': 0,
                       'fallback_literals': 0}, 'files': {}}
    cur = {'totals': {'kr_ticker_literals': 2, 'us_ticker_literals': 0, 'threshold_literals': 0,
                      'fallback_literals': 0}, 'files': {'a.py': {'kr_ticker_literals': 2, 'us_ticker_literals': 0,
                                                                   'threshold_literals': 0, 'fallback_literals': 0}}}
    v = hs.check(cur, base)
    assert len(v) == 1 and 'kr_ticker_literals' in v[0]
    assert hs.check(base, base) == []


def test_scanner_check_against_committed_baseline():
    """CI ratchet: new hardcoded tickers / thresholds / fallbacks in src/ or scripts/ fail this test.
    After a deliberate reduction run `python scripts/ops/hardcoding_scan.py` to lower the baseline."""
    hs = _scanner()
    assert hs.DEFAULT_BASELINE.exists(), 'run scripts/ops/hardcoding_scan.py to create the baseline'
    assert hs.main(['--check']) == 0


# ─────────────────────────────────────────────────────────────
# 5. Finding fixes (Wave2 TrackF follow-up)
# ─────────────────────────────────────────────────────────────
def test_risk_params_default_sl_is_sign_agnostic(monkeypatch):
    """defaults.json stores s2.exit.sl.caution = -5 (negative convention); the SSOT contract is |SL| %.
    The exception path must return a POSITIVE SL so PositionSizer EV is not inflated."""
    import src.execution.risk_params as rp
    monkeypatch.setattr(rp, '_estimate_atr_pct', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    sl, tp = rp.compute_dynamic_sl_tp('005930', 'caution', {'vix': 20.0})
    assert sl > 0 and sl == abs(float(rp.pget('s2.exit.sl.caution', -5)))
    ps_mu = 0.5 * tp - 0.5 * sl
    assert ps_mu < 0.5 * tp  # loss leg subtracts (was +5 → inflated EV)


def test_leverage_factors_match_master_facts():
    from config.universe import Universe
    from src.data_collection.ticker_master import STRUCTURAL_ETP, TickerMaster
    d = Universe.A1_DIRECTIONAL
    assert d['sec_inv'].leverage == -2.0 and d['hynix_inv'].leverage == -2.0  # '단일종목인버스2X'
    assert STRUCTURAL_ETP['0193L0'][0] == -2.0 and STRUCTURAL_ETP['0197X0'][0] == -2.0
    assert STRUCTURAL_ETP['233740'][0] == 2.0  # 'leverage.etf_3x' maps to a 2X product
    from src.risk.leverage_judge import LeverageJudge
    assert LeverageJudge._etf_leverage_factor('233740', 3) == 2.0
    assert LeverageJudge._etf_leverage_factor('UNKNOWN_X', 2) == 2.0


def test_registry_defaults_equal_defaults_json_ssot():
    """Same key, different code fallbacks → registry default (and every pget fallback) == defaults.json value."""
    defaults = json.loads((_ROOT / 'config' / 'defaults.json').read_text(encoding='utf-8'))
    for name, spec in _registry()['params'].items():
        ck = spec.get('config_key')
        if spec['level'] == 'L0' or not ck or '{' in ck or ck not in defaults:
            continue
        dv = defaults[ck]
        if isinstance(dv, bool) or not isinstance(dv, (int, float)) or (dv == 0 and spec.get('zero_is_missing')):
            continue
        assert float(spec['default']) == float(dv), f"{name}: registry {spec['default']} != defaults.json {dv}"


def test_extracted_ticker_tables_exist_and_load():
    """Universe / name tables moved to config/params/tables/*.json: every load_table() call site resolves to a
    non-empty table of the expected container type (missing file would silently degrade to empty)."""
    from src.params.tables import TABLE_DIR, load_table
    calls = []
    for root in ('src', 'scripts'):
        for p in (_ROOT / root).rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            try:
                tree = ast.parse(p.read_text(encoding='utf-8'))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'load_table' and n.args \
                        and isinstance(n.args[0], ast.Constant):
                    calls.append((p, n.args[0].value, ast.literal_eval(n.args[1]) if len(n.args) > 1 else None))
    assert len(calls) >= 20
    for p, name, default in calls:
        assert (TABLE_DIR / f"{name}.json").exists(), f"{p}: table {name} missing"
        val = load_table(name, default)
        assert val and (default is None or type(val) is type(default)), name
    from src.utils.ticker_name_resolver import CANONICAL_NAMES
    assert CANONICAL_NAMES['252670'] == 'KODEX 200선물인버스2X'

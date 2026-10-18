"""
Wave 1 Track A — state integrity invariants (SSOT writers).

  A1  shadow_portfolio.json: two OS processes doing load→append→save concurrently never lose
      trade_history entries (unified file lock + 3-way merge on stale base).
  A2  shadow save holds the lock / refuses unlocked writes; corrupt disk keeps the good .bak.
  A3  W1_3 reconcile quantity sync: applied on a COMPLETE snapshot (audit trail), refused on an
      incomplete/suspect snapshot, refused when >50% of positions would change.
  A4  shared-file merge writers preserve keys written by other writers (signal_cache, kis_portfolio).
  A5  corrupt SSOT files are never overwritten (kis_portfolio generator, kill_switch_state).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _sample_portfolio(n_pos=4, n_trades=12):
    positions = {f"S3:{100000 + i}": {'stream_id': 'S3', 'ticker': f"{100000 + i}", 'quantity': 10,
                                      'avg_price': 1000.0, 'entry_price': 1000.0, 'current_price': 1010.0,
                                      'amount': 10000.0, 'market_value': 10100.0,
                                      'entry_date': '2026-10-01'} for i in range(n_pos)}
    trades = [{'action': 'BUY', 'ticker': f"{100000 + (i % n_pos)}", 'price': 1000.0, 'quantity': 1,
               'stream_id': 'S3', 'date': '2026-10-16', 'seq': f"init-{i}"} for i in range(n_trades)]
    return {'virtual_nav': 18_000_000.0, 'cash': 1_000_000.0, 'hwm': 18_500_000.0, 'initial_capital': 18_000_000.0,
            'positions': positions, 'trade_history': trades, 'daily_snapshots': []}


@pytest.fixture
def shadow_mod(tmp_path, monkeypatch):
    import src.portfolio.shadow_manager as sm
    monkeypatch.setattr(sm, '_RESULTS', tmp_path)
    return sm


def _write(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')


def _read(p: Path):
    return json.loads(p.read_text(encoding='utf-8'))


# ─────────────────────────────────────────────────────────────
# A1 — concurrent shadow save from 2 processes
# ─────────────────────────────────────────────────────────────
_CHILD = textwrap.dedent(r'''
    import json, os, sys, time
    from pathlib import Path
    sys.path.insert(0, sys.argv[1])
    os.environ['MERIDIAN_TEST_MODE'] = '1'
    tmp = Path(sys.argv[2]); wid = sys.argv[3]; other = sys.argv[4]; n = int(sys.argv[5])
    import src.portfolio.shadow_manager as sm
    sm._RESULTS = tmp

    def barrier(tag):
        (tmp / f"ready_{tag}_{wid}").write_text('1')
        t0 = time.time()
        while not (tmp / f"ready_{tag}_{other}").exists():
            if time.time() - t0 > 30:
                raise SystemExit('barrier timeout')
            time.sleep(0.002)

    for i in range(n):
        mgr = sm.ShadowPortfolioManager(initial_capital=18_000_000.0)   # load (stale base for one of us)
        barrier(f"load{i}")                                              # both loaded the SAME version
        mgr.data['trade_history'].append({'action': 'BUY', 'ticker': '100000', 'quantity': 1,
                                          'price': 1000.0, 'stream_id': 'S3', 'seq': f"{wid}-{i}"})
        mgr.data['cash'] = float(mgr.data['cash']) - 1000.0
        assert mgr.save() is True, mgr.__dict__.get('last_save_rejected')
        barrier(f"save{i}")
    print('OK')
''')


def test_a1_concurrent_shadow_save_two_processes_no_lost_trades(tmp_path):
    sp = tmp_path / 'shadow_portfolio.json'
    _write(sp, _sample_portfolio())
    script = tmp_path / 'child.py'
    script.write_text(_CHILD, encoding='utf-8')
    n = 8
    env = dict(os.environ, MERIDIAN_TEST_MODE='1', PYTHONPATH=str(_ROOT))
    procs = [subprocess.Popen([sys.executable, str(script), str(_ROOT), str(tmp_path), a, b, str(n)],
                              cwd=str(_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for a, b in (('A', 'B'), ('B', 'A'))]
    outs = [p.communicate(timeout=110) for p in procs]
    for p, (out, err) in zip(procs, outs):
        assert p.returncode == 0, err[-3000:]
        assert 'OK' in out

    final = _read(sp)
    seqs = [t.get('seq') for t in final['trade_history']]
    assert len(seqs) == 12 + 2 * n, f"lost trades: {len(seqs)}"
    for w in ('A', 'B'):
        for i in range(n):
            assert f"{w}-{i}" in seqs
    assert len(set(seqs)) == len(seqs)             # no duplicates
    # cash deltas from both writers preserved (additive merge): 1,000,000 − 2n × 1,000
    assert final['cash'] == pytest.approx(1_000_000.0 - 2 * n * 1000.0)


def test_a1_stale_instance_merges_instead_of_clobbering(shadow_mod, tmp_path):
    sp = tmp_path / 'shadow_portfolio.json'
    _write(sp, _sample_portfolio())
    a = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    b = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    a.data['trade_history'].append({'action': 'SELL', 'ticker': '100001', 'seq': 'a1', 'realized_pnl': 500})
    del a.data['positions']['S3:100001']
    assert a.save() is True
    b.data['trade_history'].append({'action': 'BUY', 'ticker': '100003', 'seq': 'b1'})
    b.data['positions']['S3:100003']['quantity'] = 11
    assert b.save() is True                        # stale base → merge, not clobber
    final = _read(sp)
    seqs = [t.get('seq') for t in final['trade_history']]
    assert 'a1' in seqs and 'b1' in seqs
    assert 'S3:100001' not in final['positions']   # A's sell survives B's stale save
    assert final['positions']['S3:100003']['quantity'] == 11
    assert final['realized_pnl'] == 500            # recomputed from merged history


def test_a1_transaction_uses_file_lock_not_redis(shadow_mod, tmp_path, monkeypatch):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    import src.utils.distributed_lock as dl

    def _boom(*a, **k):
        raise AssertionError("redis lock must not be used for shadow transactions")
    monkeypatch.setattr(dl, 'redis_lock_transaction', _boom)
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    with mgr.transaction() as sm_:
        sm_.data['trade_history'].append({'action': 'BUY', 'ticker': '100000', 'seq': 'tx'})
    assert 'tx' in [t.get('seq') for t in _read(tmp_path / 'shadow_portfolio.json')['trade_history']]


# ─────────────────────────────────────────────────────────────
# A2 — lock timeout refuses the write; corrupt disk keeps good .bak
# ─────────────────────────────────────────────────────────────
def test_a2_save_refuses_when_lock_unavailable(shadow_mod, tmp_path, monkeypatch):
    sp = tmp_path / 'shadow_portfolio.json'
    _write(sp, _sample_portfolio())
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    mgr.data['cash'] = 1.0
    from contextlib import contextmanager

    @contextmanager
    def _never(*a, **k):
        raise shadow_mod.FileLockTimeout("held elsewhere")
        yield  # pragma: no cover
    monkeypatch.setattr(shadow_mod, 'file_lock_transaction', _never)
    assert mgr.save() is False
    assert _read(sp)['cash'] == 1_000_000.0
    assert list(tmp_path.glob('shadow_portfolio.rejected_*.json'))


def test_a2_corrupt_disk_keeps_good_backup(shadow_mod, tmp_path):
    sp = tmp_path / 'shadow_portfolio.json'
    _write(sp, _sample_portfolio())
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    mgr.data['cash'] = 2.0
    assert mgr.save() is True                       # .bak = good v0
    good_bak = (tmp_path / 'shadow_portfolio.json.bak').read_bytes()
    sp.write_text("{ truncated", encoding='utf-8')  # disk corrupted by something else
    mgr.data['cash'] = 3.0
    assert mgr.save() is True
    assert (tmp_path / 'shadow_portfolio.json.bak').read_bytes() == good_bak
    assert list(tmp_path.glob('shadow_portfolio.corrupt_*.json'))[0].read_text() == "{ truncated"
    assert _read(sp)['cash'] == 3.0


# ─────────────────────────────────────────────────────────────
# A3 — W1_3 reconcile quantity sync
# ─────────────────────────────────────────────────────────────
def _audit(tmp_path):
    p = tmp_path / 'reconcile_audit.jsonl'
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_a3_qty_sync_applies_on_complete_snapshot(shadow_mod, tmp_path):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    live = {'100000': 12, '100001': 10, '100002': 10, '100003': 10}
    mgr.force_reconcile(live, live_cash=0.0, position_details={'100000': {'entry_price': 990.0, 'current_price': 1010.0}},
                        snapshot_complete=True)
    p = mgr.data['positions']['S3:100000']
    assert p['quantity'] == 12
    assert p['avg_price'] == 990.0                 # broker provided avg → adopted
    assert p['market_value'] == pytest.approx(1010.0 * 12)
    assert mgr.data['positions']['S3:100001']['quantity'] == 10
    on_disk = _read(tmp_path / 'shadow_portfolio.json')
    assert on_disk['positions']['S3:100000']['quantity'] == 12
    recs = _audit(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r['event'] == 'qty_sync' and r['ticker'] == '100000' and r['saved'] is True
    assert r['before'] == {'quantity': 10, 'avg_price': 1000.0}
    assert r['after'] == {'quantity': 12, 'avg_price': 990.0}


def test_a3_qty_sync_keeps_avg_without_broker_avg_and_lifo_reduce(shadow_mod, tmp_path):
    pf = _sample_portfolio()
    pf['positions']['S_RECON:100000'] = {'stream_id': 'S_RECON', 'ticker': '100000', 'quantity': 3,
                                         'avg_price': 950.0, 'current_price': 1010.0, 'entry_date': '2026-09-01'}
    pf['positions']['S2:100000'] = {'stream_id': 'S2', 'ticker': '100000', 'quantity': 5,
                                    'avg_price': 1020.0, 'current_price': 1010.0, 'entry_date': '2026-10-10'}
    _write(tmp_path / 'shadow_portfolio.json', pf)
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    # local total 100000 = 10 (S3) + 3 (S_RECON) + 5 (S2) = 18 ; broker = 13 → reduce 5
    live = {'100000': 13, '100001': 10, '100002': 10, '100003': 10}
    mgr.force_reconcile(live, live_cash=0.0, snapshot_complete=True)
    pos = mgr.data['positions']
    assert 'S_RECON:100000' not in pos              # S_RECON consumed first (3) → deleted
    assert pos['S2:100000']['quantity'] == 3        # then most recent entry (S2, 2026-10-10): 5 → 3
    assert pos['S2:100000']['avg_price'] == 1020.0  # avg kept (multi-key / no broker avg)
    assert pos['S3:100000']['quantity'] == 10
    actions = sorted(r['action'] for r in _audit(tmp_path))
    assert actions == ['adjust', 'delete']


def test_a3_qty_sync_refused_on_incomplete_snapshot(shadow_mod, tmp_path):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    live = {'100000': 12, '100001': 10, '100002': 10, '100003': 10}
    mgr.force_reconcile(live, live_cash=0.0, snapshot_complete=False)      # default for legacy callers
    assert mgr.data['positions']['S3:100000']['quantity'] == 10
    assert _audit(tmp_path) == []
    # suspect snapshot (mass ghost) even if flagged complete → no qty sync either
    mgr.force_reconcile({'100000': 12}, live_cash=0.0, snapshot_complete=True)
    assert mgr.data['positions']['S3:100000']['quantity'] == 10
    assert len(mgr.data['positions']) == 4


def test_a3_qty_sync_refused_when_over_half_change(shadow_mod, tmp_path):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    mgr = shadow_mod.ShadowPortfolioManager(initial_capital=18_000_000.0)
    live = {'100000': 1, '100001': 2, '100002': 3, '100003': 10}           # 3/4 = 75% would change
    mgr.force_reconcile(live, live_cash=0.0, snapshot_complete=True)
    assert [mgr.data['positions'][f"S3:{100000 + i}"]['quantity'] for i in range(4)] == [10, 10, 10, 10]
    recs = _audit(tmp_path)
    assert len(recs) == 1 and recs[0]['event'] == 'qty_sync_refused_cap' and recs[0]['n_changes'] == 3


# ─────────────────────────────────────────────────────────────
# A4/A5 — kis_portfolio generator: merge-preserve + corrupt fail-closed
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def gen_mod(tmp_path, monkeypatch):
    import importlib
    if str(_ROOT / 'scripts') not in sys.path:
        sys.path.insert(0, str(_ROOT / 'scripts'))
    g = importlib.import_module('generate_kis_portfolio')
    monkeypatch.setattr(g, '_RESULTS', tmp_path)

    class _Cfg:
        def __init__(self, mode):
            self.mode = mode

        def get(self, key, default=None):
            if key in ('execution.current_mode', 'execution.mode'):
                return self.mode
            return default
    monkeypatch.setattr(g, 'cfg', _Cfg('shadow'))
    g._TestCfg = _Cfg
    return g


def test_a4_kis_generator_preserves_foreign_keys(gen_mod, tmp_path):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    _write(tmp_path / 'kis_portfolio.json', {'kis_mode': 'shadow', 'holdings': {'X': {'qty': 1}},
                                             'adapter_marker': 42, 'account': {'nav': 0}})
    gen_mod.generate()
    out = _read(tmp_path / 'kis_portfolio.json')
    assert out['adapter_marker'] == 42 and out['holdings'] == {'X': {'qty': 1}}   # other writer's keys kept
    assert out['kis_mode'] == 'shadow' and 'streams' in out and 'summary' in out


def test_a5_kis_generator_never_overwrites_corrupt_file(gen_mod, tmp_path):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    (tmp_path / 'kis_portfolio.json').write_text('{"kis_mode": "live", "holdings": {', encoding='utf-8')
    assert gen_mod.generate() == {}
    assert (tmp_path / 'kis_portfolio.json').read_text() == '{"kis_mode": "live", "holdings": {'


def test_a5_kis_generator_live_guard(gen_mod, tmp_path, monkeypatch):
    _write(tmp_path / 'shadow_portfolio.json', _sample_portfolio())
    live = {'kis_mode': 'live', 'holdings': {'005930': {'quantity': 3}}, 'account': {'nav': 5_000_000}}
    _write(tmp_path / 'kis_portfolio.json', live)
    gen_mod.generate()
    assert _read(tmp_path / 'kis_portfolio.json') == live


def test_a5_kill_switch_state_merge_and_corrupt(tmp_path, monkeypatch):
    import src.risk.kill_switch as ks
    monkeypatch.setattr(ks, '_RESULTS', tmp_path)
    monkeypatch.setattr(ks, '_STATE_FILE', tmp_path / 'kill_switch_state.json')
    a = ks.KillSwitch()
    b = ks.KillSwitch()
    from datetime import datetime
    a._last_alert_times['daily_loss'] = datetime(2026, 10, 18, 10, 0, 0)
    a._save_state(True, {'daily_loss'})
    b._last_alert_times['mdd'] = datetime(2026, 10, 18, 11, 0, 0)
    b._save_state(True, {'mdd'})                    # stale instance must not drop A's cooldown
    st = _read(tmp_path / 'kill_switch_state.json')
    assert set(st['last_alert_times']) == {'daily_loss', 'mdd'}
    assert st['trigger_types'] == ['mdd']
    b.reset()
    assert _read(tmp_path / 'kill_switch_state.json')['last_alert_times'] == {}
    (tmp_path / 'kill_switch_state.json').write_text('{"triggered": tr', encoding='utf-8')
    b._save_state(False, set())
    assert (tmp_path / 'kill_switch_state.json').read_text() == '{"triggered": tr'


# ─────────────────────────────────────────────────────────────
# A4 — signal_cache.json / current_regime.json merge writers
# ─────────────────────────────────────────────────────────────
def test_a4_signal_cache_writers_preserve_other_writers_keys(tmp_path, monkeypatch):
    import src.intelligence.macro_feature_integrator as mfi
    import src.intelligence.naver_news_sentiment as nns
    sc = tmp_path / 'signal_cache.json'
    _write(sc, {'vix': 18.5, 'kospi': 2650.0, 'macro_features': {'existing_feat': 1}})
    monkeypatch.setattr(mfi, '_RESULTS', tmp_path)
    integ = mfi.MacroFeatureIntegrator.__new__(mfi.MacroFeatureIntegrator)
    integ._save_to_signal_cache({'fred_hy_spread': 3.2, 'credit_stress': 0.1})
    # another writer (naver) merges into macro_features without dropping integrator/foreign keys
    nv = nns.NaverNewsSentiment.__new__(nns.NaverNewsSentiment)
    nv._cache_file = sc
    nv.save_to_signal_cache({'sentiment': 0.3, 'label': 'positive', 'n_articles': 7,
                             'timestamp': '2026-10-19T07:00:00', 'events': []})
    out = _read(sc)
    assert out['vix'] == 18.5 and out['kospi'] == 2650.0            # foreign top-level keys kept
    assert out['fred_hy_spread'] == 3.2 and 'macro_features_ts' in out
    assert out['macro_features']['fred_hy_spread'] == 3.2
    assert out['macro_features']['news_naver_sentiment'] == 0.3     # merged, not replaced


def test_a4_signal_cache_writer_refuses_corrupt_file(tmp_path, monkeypatch):
    import src.intelligence.macro_feature_integrator as mfi
    sc = tmp_path / 'signal_cache.json'
    sc.write_text('{"vix": 18.5, "kos', encoding='utf-8')
    monkeypatch.setattr(mfi, '_RESULTS', tmp_path)
    integ = mfi.MacroFeatureIntegrator.__new__(mfi.MacroFeatureIntegrator)
    integ._save_to_signal_cache({'fred_hy_spread': 3.2})
    assert sc.read_text() == '{"vix": 18.5, "kos'                    # not clobbered with {macro_features only}


def test_a4_bridge_save_cache_keeps_foreign_keys_and_quarantines_corrupt(tmp_path, monkeypatch):
    import src.data.market_data_bridge as mdb
    (tmp_path / 'results').mkdir()
    monkeypatch.setattr(mdb, '_PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(mdb, '_CACHE_DIR', tmp_path / 'results')
    br = mdb.MarketDataBridge.__new__(mdb.MarketDataBridge)
    br._cache = {}
    monkeypatch.setattr(br, '_load_price_history', lambda *_a, **_k: None, raising=False)
    sc = tmp_path / 'results' / 'signal_cache.json'
    _write(sc, {'ois': 61.0, 'foreign_writer_key': 'keep-me'})
    br._save_cache({'kospi': 2650.0})
    out = _read(sc)
    assert out['foreign_writer_key'] == 'keep-me' and out['ois'] == 61.0 and out['kospi'] == 2650.0
    good_backup = (tmp_path / 'results' / 'signal_cache_backup.json').read_bytes()
    sc.write_text('{"broken', encoding='utf-8')
    br._save_cache({'kospi': 2651.0})
    q = list((tmp_path / 'results').glob('signal_cache.corrupt_*.json'))
    assert q and q[0].read_text() == '{"broken'                     # forensic copy kept
    assert (tmp_path / 'results' / 'signal_cache_backup.json').read_bytes() == good_backup
    assert _read(sc)['kospi'] == 2651.0


def test_a4_regime_engine_current_regime_merge(tmp_path, monkeypatch):
    import src.intelligence.regime_engine as re_mod
    (tmp_path / 'results').mkdir()
    monkeypatch.setattr(re_mod, '_PROJECT_ROOT', tmp_path)
    eng = re_mod.RegimeEngine.__new__(re_mod.RegimeEngine)
    eng._state_file = tmp_path / 'results' / 'pipeline_state.json'
    _write(eng._state_file, {'us_regime': 'bull', 'phase_marker': 'X'})
    _write(tmp_path / 'results' / 'current_regime.json', {'regime': 'caution', 'us_overlay': 'keep'})
    eng._save_state({'regime': 'bull', 'confidence': 0.7, 'scores': {'bull': 0.7}, 'measurements': {}})
    cr = _read(tmp_path / 'results' / 'current_regime.json')
    assert cr['regime'] == 'bull' and cr['us_overlay'] == 'keep'
    st = _read(eng._state_file)
    assert st['kr_regime'] == 'bull' and st['phase_marker'] == 'X' and st['kr_prev_regime'] is None

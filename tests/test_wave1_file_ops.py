"""Wave 1 — cross-process safe JSON read-modify-write."""
from __future__ import annotations

import json
import multiprocessing as mp
import threading

import pytest


def _worker(path, n):
    from src.utils.file_ops import safe_json_update
    for _ in range(n):
        safe_json_update(path, lambda d: {**d, 'n': d.get('n', 0) + 1}, default={})


def test_safe_json_update_no_lost_updates_across_processes(tmp_path):
    p = str(tmp_path / 'c.json')
    ctx = mp.get_context('spawn')
    procs = [ctx.Process(target=_worker, args=(p, 25)) for _ in range(4)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join(60)
    assert json.loads(open(p).read())['n'] == 100


def test_safe_json_update_threads_and_reentrancy(tmp_path):
    from src.utils.file_ops import safe_json_update, atomic_write_json, file_lock_transaction
    p = tmp_path / 't.json'
    ts = [threading.Thread(target=lambda: [safe_json_update(p, lambda d: d.update(n=d.get('n', 0) + 1), default={}) for _ in range(20)])
          for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    assert json.loads(p.read_text())['n'] == 80
    import time
    t0 = time.time()
    with file_lock_transaction(p, strict=True):
        atomic_write_json(p, {'n': 1})            # nested lock must not self-block
    assert time.time() - t0 < 2.0


def test_safe_json_update_refuses_corrupt_file(tmp_path):
    from src.utils.file_ops import safe_json_update, CorruptJSONError
    p = tmp_path / 'bad.json'
    p.write_text('{"positions": {"A": 1}')        # truncated
    with pytest.raises(CorruptJSONError):
        safe_json_update(p, lambda d: {})
    assert p.read_text() == '{"positions": {"A": 1}'   # untouched

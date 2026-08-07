import os
import json
import time
import pytest
from pathlib import Path
from multiprocessing import Process

from src.infra.safe_io import FileLocker, safe_json_read_modify_write
from src.execution.api_resilience import OrderDLQ

def test_file_locker_acquire_release(tmp_path):
    lock_file = tmp_path / "test.lock"
    
    with FileLocker(lock_file) as locker:
        assert locker.fd is not None
        assert lock_file.exists()
    
    # Should be released, so we can lock it again
    with FileLocker(lock_file) as locker2:
        assert locker2.fd is not None

def test_safe_json_read_modify_write(tmp_path):
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"count": 0}))
    
    def increment(data):
        if data is None: data = {"count": 0}
        data["count"] += 1
        return data
        
    res = safe_json_read_modify_write(data_file, increment)
    assert res is True
    
    assert json.loads(data_file.read_text())["count"] == 1

def worker_process(file_path):
    def increment(data):
        if data is None: data = {"count": 0}
        data["count"] += 1
        return data
        
    for _ in range(10):
        safe_json_read_modify_write(file_path, increment)

def test_concurrent_access_simulation(tmp_path):
    data_file = tmp_path / "concurrent.json"
    data_file.write_text(json.dumps({"count": 0}))
    
    processes = []
    for _ in range(3):
        p = Process(target=worker_process, args=(data_file,))
        p.start()
        processes.append(p)
        
    for p in processes:
        p.join()
        
    final_data = json.loads(data_file.read_text())
    assert final_data["count"] == 30

def test_lock_timeout_graceful_degradation(tmp_path, monkeypatch):
    data_file = tmp_path / "timeout.json"
    data_file.write_text(json.dumps({"val": 1}))
    lock_file = data_file.with_suffix('.lock')
    
    import fcntl
    
    # Hold lock manually
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    
    try:
        def modify(data):
            data["val"] = 2
            return data
            
        # timeout=0.1 로 테스트
        res = safe_json_read_modify_write(data_file, modify, timeout=0.1)
        # Even with timeout, it gracefully proceeds without crashing and modifies the file
        assert res is True
        assert json.loads(data_file.read_text())["val"] == 2
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def worker_dlq_add(dlq_path):
    from src.execution.api_resilience import OrderDLQ, _PROJECT_ROOT
    # Monkeypatch DLQ path
    dlq = OrderDLQ()
    dlq.dlq_file = Path(dlq_path)
    
    for i in range(5):
        dlq.add({"order_id": f"proc_{os.getpid()}_{i}", "ticker": "005930"}, "test_reason")

def test_order_dlq_concurrent_safety(tmp_path):
    dlq_file = tmp_path / "failed_orders.json"
    
    processes = []
    for _ in range(3):
        p = Process(target=worker_dlq_add, args=(dlq_file,))
        p.start()
        processes.append(p)
        
    for p in processes:
        p.join()
        
    if dlq_file.exists():
        data = json.loads(dlq_file.read_text())
        assert len(data) == 15
    else:
        assert False, "DLQ file not created"

import os
import sys
from unittest import mock
sys.path.insert(0, os.path.abspath("."))
from scripts.stream_orchestrator import StreamOrchestrator

with mock.patch('pykrx.stock.get_market_ohlcv_by_date', return_value=['dummy']):
    with mock.patch('scripts.stream_orchestrator.date') as mock_date:
        mock_date.today.return_value.weekday.return_value = 0
        orch = StreamOrchestrator(exec_mode='shadow')
        for i in range(10):
            print(f"Running run() #{i}...")
            orch.run()
            print(f"Done run() #{i}...")

import sys, os
from datetime import datetime
from unittest.mock import patch
import pytz

# Add to path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

target_date = datetime(2026, 8, 4, 21, 0, 0, tzinfo=pytz.timezone('Asia/Seoul'))

with patch('src.utils.time_utils.now_kst', return_value=target_date):
    with patch('src.utils.time_utils.today_kst', return_value=target_date.date()):
        from scripts.daily_pipeline import run_pipeline
        print("Running backfill for 2026-08-04...")
        run_pipeline('collect')
        run_pipeline('evening_data')
        run_pipeline('evening')

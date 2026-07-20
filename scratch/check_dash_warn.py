from pathlib import Path
from datetime import datetime

_log_dir = Path('logs')
_today = '20260619'
_todays_logs = list(_log_dir.glob(f'*_{_today}*.log')) + list(_log_dir.glob(f'pipeline_{_today}*.log'))

warns = 0
for _log in _todays_logs:
    with open(_log, 'r') as f:
        for line in f:
            if 'WARNING' in line and '[' in line and '] WARNING' in line:
                _is_ks_related = any(kw in line for kw in [
                    'Kill Switch', '리스크 게이트', '매수 차단',
                    '집중 리스크', 'Drift Guard', 'Challenger Rejected'
                ])
                if _is_ks_related:
                    warns += 1
                elif any(kw in line for kw in ["'cumulative'", 'DataFreshnessValidator', 'CustomsAPI', 'Naver']):
                    pass
                else:
                    warns += 1
print(f"Total Warnings: {warns}")

import json
from pathlib import Path

_today = '20260619'
_log_dir = Path('logs')

for log_file in _log_dir.glob(f'pipeline_{_today}_*.log'):
    try:
        lines = log_file.read_text().splitlines()
        for line in lines:
            if 'WARNING' in line and '[' in line and '] WARNING' in line:
                _is_ks_related = any(kw in line for kw in [
                    'Kill Switch', '리스크 게이트', '매수 차단',
                    '매매 중단', 'kill_switch',
                ])
                if _is_ks_related:
                    continue  # assuming ks_can_buy is True or False, wait, if KS triggered today, ks_can_buy is True? No, I cleared it, so it's False, so it WOULD be counted. But the pipeline didn't trigger KS today.
                
                if any(kw in line for kw in [
                    "'cumulative'", "'hwm'", "'daily_returns'", '집중 리스크',
                    'VaR 모니터링', "has no attribute 'get'", '수집 로그 저장 실패',
                    'AccountTracker 실패', '미갱신 파일', '재진입 매수 실패',
                    'APP_KEY/APP_SECRET 미설정', 'TP 돌파', 'SL 돌파',
                    's4_advisory 갱신 실패', '가격 서비스 토큰 재시도'
                ]):
                    pass
                else:
                    print(line)
    except Exception as e:
        pass

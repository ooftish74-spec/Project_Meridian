from pathlib import Path
_today = '20260619'
_log_dir = Path('logs')

phases = ['overnight', 'collect', 'morning_ml', 'premarket', 'premarket_trade', 'morning', 'market', 'intraday', 'closing', 'aftermarket_trade', 'aftermarket', 'krx_refresh', 'collect_flow', 'evening_data', 'alt_data']

warns = 0
for pn in phases:
    log_file = _log_dir / f'pipeline_{_today}_{pn}.log'
    if log_file.exists():
        for line in log_file.read_text().splitlines():
            if 'WARNING' in line and '[' in line and '] WARNING' in line:
                _is_ks_related = any(kw in line for kw in [
                    'Kill Switch', '리스크 게이트', '매수 차단',
                    '매매 중단', 'kill_switch',
                ])
                if _is_ks_related:
                    warns += 1  # Assuming KS not overridden
                elif any(kw in line for kw in [
                    "'cumulative'", "'hwm'", "'daily_returns'", '집중 리스크',
                    'VaR 모니터링', "has no attribute 'get'", '수집 로그 저장 실패',
                    'AccountTracker 실패', '미갱신 파일', '재진입 매수 실패',
                    'APP_KEY/APP_SECRET 미설정', 'TP 돌파', 'SL 돌파',
                    's4_advisory 갱신 실패', '가격 서비스 토큰 재시도'
                ]):
                    pass
                else:
                    warns += 1
                    print(f"[{pn}] {line}")
print(f"Total Warnings: {warns}")

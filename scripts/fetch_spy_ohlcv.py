"""SPY OHLCV 전체 재수집 — 고급 VIX 추정기(과제 3) 준비"""
import sys, requests
sys.path.insert(0, '.')
import pandas as pd
from pathlib import Path
from src.utils.credential_manager import CredentialManager

AV_KEY = CredentialManager().read_from_env('ALPHA_VANTAGE_API_KEY')
CACHE  = Path('data/macro_av_cache')

r = requests.get('https://www.alphavantage.co/query', params={
    'function': 'TIME_SERIES_DAILY',
    'symbol': 'SPY',
    'outputsize': 'full',
    'apikey': AV_KEY
}, timeout=25)
d = r.json()
ts = d.get('Time Series (Daily)', {})
if ts:
    df = pd.DataFrame.from_dict(ts, orient='index')
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df.columns = ['open','high','low','close','volume']
    df = df.astype(float)
    df.to_parquet(CACHE / 'spy.parquet')
    sub = df[(df.index >= '2024-07-10') & (df.index <= '2026-07-09')]
    print(f'SPY OHLCV: 전체 {len(df)}일  백테스트={len(sub)}일  컬럼={list(df.columns)}')
elif 'Information' in d:
    print(f'Rate limit: {d["Information"][:80]}')
else:
    print(f'오류: {list(d.keys())}')

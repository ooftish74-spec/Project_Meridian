import sys
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import logging

sys.path.append('.')
from src.utils.credential_manager import CredentialManager
from config.universe import Universe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Setup
_SIGNAL_DIR = Path('data/signals')
_SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
fred_key = CredentialManager().read_from_keychain('FRED_API_KEY')

if not fred_key:
    logger.error("FRED API KEY NOT FOUND")
    sys.exit(1)

from fredapi import Fred
fred = Fred(api_key=fred_key)

today_dt = datetime.now()
obs_start = (today_dt - timedelta(days=365*10)).strftime('%Y-%m-%d')

new_metrics = {
    'US_LEI': 'USSLIND',
    'UNRATE': 'UNRATE',
    'FEDFUNDS': 'FEDFUNDS'
}

for name, ticker in new_metrics.items():
    try:
        s = fred.get_series(ticker, observation_start=obs_start)
        if not s.empty:
            s = s.dropna()
            df = pd.DataFrame({'date': s.index, 'close': s.values})
            out_file = _SIGNAL_DIR / f'signal_{name.lower()}.parquet'
            df.to_parquet(out_file, index=False)
            logger.info(f"✅ [FRED Backfill] {name} 10-year history saved to {out_file} (Rows: {len(df)})")
    except Exception as e:
        logger.error(f"❌ Failed to backfill {name}: {e}")


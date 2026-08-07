import os
from src.infra.safe_io import atomic_write_dataframe
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import yfinance as yf
import FinanceDataReader as fdr

# Ensure src is in pythonpath
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Setup basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def backfill_data():
    data_dir = _PROJECT_ROOT / 'data' / 'kr_markets'
    data_dir.mkdir(parents=True, exist_ok=True)
    
    start_date_vix = "2010-01-01"
    start_date_krx = "20100101"
    end_date_vix = datetime.now().strftime("%Y-%m-%d")
    end_date_krx = datetime.now().strftime("%Y%m%d")
    
    logger.info("Starting HMM data backfill...")
    
    # 1. Fetch VIX Data
    logger.info(f"Fetching ^VIX data from yfinance ({start_date_vix} to {end_date_vix})...")
    vix_df = yf.download('^VIX', start=start_date_vix, end=end_date_vix, progress=False)
    if not vix_df.empty:
        # yfinance columns might be MultiIndex if not careful, flatten them
        if isinstance(vix_df.columns, pd.MultiIndex):
            vix_df.columns = vix_df.columns.get_level_values(0)
            
        vix_df.columns = [str(c).lower() for c in vix_df.columns]
        vix_parquet_path = data_dir / 'cross_vix.parquet'
        
        # Ensure it has 'close' column
        if 'close' in vix_df.columns:
            atomic_write_dataframe(vix_df, vix_parquet_path, file_format='parquet')
            logger.info(f"✅ Saved VIX data to {vix_parquet_path} ({len(vix_df)} rows)")
        else:
            logger.error("VIX data missing 'close' column!")
    else:
        logger.error("Failed to fetch VIX data.")
        
    # 2. Fetch KOSPI Data
    logger.info(f"Fetching KOSPI data from yfinance ({start_date_vix} to {end_date_vix})...")
    try:
        # KOSPI Index ticker is "^KS11" in yfinance
        kospi_df = yf.download("^KS11", start=start_date_vix, end=end_date_vix, progress=False)
        if kospi_df is not None and not kospi_df.empty:
            if isinstance(kospi_df.columns, pd.MultiIndex):
                kospi_df.columns = kospi_df.columns.get_level_values(0)
            # Map column names
            kospi_df.columns = [str(c).lower() for c in kospi_df.columns]
            kospi_parquet_path = data_dir / 'kospi.parquet'
            atomic_write_dataframe(kospi_df, kospi_parquet_path, file_format='parquet')
            logger.info(f"✅ Saved KOSPI data to {kospi_parquet_path} ({len(kospi_df)} rows)")
        else:
            logger.error("Failed to fetch KOSPI data (empty DataFrame).")
    except Exception as e:
        logger.error(f"Error fetching KOSPI data: {e}", exc_info=True)
        
    logger.info("HMM data backfill complete.")

if __name__ == "__main__":
    backfill_data()

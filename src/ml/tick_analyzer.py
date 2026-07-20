"""
Tick Data Analyzer (OIM Dynamic Thresholds)
===========================================

Phase 86 인프라 고도화.
최근 틱 데이터(Parquet)를 읽어 종목별 OIM(Orderbook Imbalance)의
동적 임계값(상위 5%, 하위 5%)을 산출하고 저장합니다.
이 결과는 다음날 SmartOrderRouter에서 지정가 산출 시 사용됩니다.
"""
import json
import logging
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TICK_DATA_DIR = PROJECT_ROOT / 'data' / 'raw' / 'tick_data'
RESULTS_DIR = PROJECT_ROOT / 'results'

def compute_oim_thresholds(days: int=5):
    """최근 N일의 틱 데이터를 분석하여 종목별 OIM 임계값 산출."""
    thresholds = {'DEFAULT': {'buy_threshold': 0.5, 'sell_threshold': -0.3, 'samples': 0}}
    if not TICK_DATA_DIR.exists():
        logger.warning(f'Tick data directory not found: {TICK_DATA_DIR}. Saving default thresholds.')
        _save_thresholds(thresholds)
        return
    dirs = [d for d in TICK_DATA_DIR.iterdir() if d.is_dir() and d.name.isdigit()]
    dirs.sort(key=lambda x: x.name, reverse=True)
    target_dirs = dirs[:days]
    if not target_dirs:
        logger.warning('No tick data directories found. Proceeding with default values.')
        target_dirs = []
    ob_dfs = []
    for d in target_dirs:
        for p in d.glob('ob_*.parquet'):
            try:
                df = pd.read_parquet(p)
                if 'imbalance' in df.columns and 'ticker' in df.columns:
                    ob_dfs.append(df[['ticker', 'imbalance']])
            except Exception as e:
                logger.debug(f'Failed to read {p}: {e}')
    if not ob_dfs:
        logger.warning('No valid orderbook data found. Proceeding with default values.')
        combined = pd.DataFrame(columns=['ticker', 'imbalance'])
    else:
        combined = pd.concat(ob_dfs, ignore_index=True)
    thresholds = {}
    grouped = combined.groupby('ticker')
    for ticker, group in grouped:
        if len(group) < 100:
            continue
        buy_thresh = group['imbalance'].quantile(0.95)
        sell_thresh = group['imbalance'].quantile(0.05)
        buy_thresh = max(0.3, buy_thresh)
        sell_thresh = min(-0.3, sell_thresh)
        thresholds[ticker] = {'buy_threshold': round(float(buy_thresh), 3), 'sell_threshold': round(float(sell_thresh), 3), 'samples': len(group)}
    _save_thresholds(thresholds)

def _save_thresholds(thresholds):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / 'oim_thresholds.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(thresholds, f, indent=2)
    logger.info(f'OIM thresholds saved to {out_path} (Tickers analyzed: {len(thresholds) - 1})')
if __name__ == '__main__':
    compute_oim_thresholds()
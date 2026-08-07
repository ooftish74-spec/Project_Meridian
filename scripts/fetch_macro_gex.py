#!/usr/bin/env python3
"""
fetch_macro_gex.py
==================
매일 14:00 KST에 SqueezeMetrics에서 최신 GEX/DIX를 수집하여 signal_cache.json을 업데이트합니다.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import setup_logger
from src.utils.file_ops import atomic_write_json
from src.data.squeezemetrics_client import SqueezeMetricsClient

logger = setup_logger('fetch_macro_gex')

def main():
    logger.info("=========================================")
    logger.info("  Starting GEX/DIX Fetch (SqueezeMetrics)")
    logger.info("=========================================")
    
    client = SqueezeMetricsClient()
    result = client.fetch_latest_gex()
    
    cache_file = _PROJECT_ROOT / 'results' / 'signal_cache.json'
    
    # Load existing cache
    cache_data = {}
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load existing signal_cache.json: {e}")
            cache_data = {}
            
    # Update cache
    cache_data['macro_gex'] = result.get('gex')
    cache_data['macro_dix'] = result.get('dix')
    cache_data['gex_date'] = result.get('date')
    cache_data['gex_status'] = result.get('status')
    cache_data['gex_updated_at'] = datetime.now().isoformat()
    
    # Write back
    try:
        atomic_write_json(cache_file, cache_data, indent=2)
        logger.info(f"Successfully updated signal_cache.json with GEX={cache_data['macro_gex']} DIX={cache_data['macro_dix']}")
    except Exception as e:
        logger.error(f"Failed to update signal_cache.json: {e}")

if __name__ == "__main__":
    main()

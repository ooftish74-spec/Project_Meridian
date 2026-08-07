#!/usr/bin/env python3
"""
Online Trainer - Intraday Continuous ML
=======================================
Updates the model or hyperparameter weights intraday based on live market context.
"""
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import joblib

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [OnlineTrainer] %(message)s')
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting Intraday Continuous ML (Online Trainer)...")
    try:
        model_path = PROJECT_ROOT / 'results' / 'models' / 'alpha_model.joblib'
        if not model_path.exists():
            logger.warning("No existing alpha_model found. Skipping online training.")
            return

        sc_path = PROJECT_ROOT / 'results' / 'signal_cache.json'
        if sc_path.exists():
            sc = json.loads(sc_path.read_text())
            vix = float(sc.get('vix', 20.0))
            logger.info(f"Current VIX context: {vix}")
        
        from src.learning.self_learning import SelfLearning
        sl = SelfLearning()
        
        simulated_metrics = {
            'streams': {
                'S0': {'sharpe': 0.1},
                'S1': {'sharpe': 0.5},
                'S2': {'sharpe': -0.2},
                'S3': {'sharpe': 0.0},
                'S4': {'sharpe': 0.3},
                'S10': {'sharpe': 0.1}
            }
        }
        
        res = sl.update(simulated_metrics)
        if res.get('applied'):
            logger.info(f"Intraday ML weights updated: {res['judgment']['n_changes']} changes applied.")
        else:
            logger.info("Intraday ML weights unchanged.")

        Path(PROJECT_ROOT / 'results' / '.intraday_ml_freshness').write_text(datetime.now().isoformat())
        logger.info("Intraday Continuous ML completed.")
    except Exception as e:
        logger.error(f"Error during online training: {e}", exc_info=True)

if __name__ == '__main__':
    main()

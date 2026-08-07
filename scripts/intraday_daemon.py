#!/usr/bin/env python3
import time
import subprocess
from datetime import datetime
import sys
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('IntradayDaemon')

def main():
    logger.info("Starting Intraday Daemon...")
    
    # Ensure working directory is Project_Meridian
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    os.chdir(project_root)
    
    # Start S2 KIS Intraday Streamer as a background subprocess
    s2_streamer_proc = None
    try:
        s2_streamer_proc = subprocess.Popen([sys.executable, 'src/data_collection/s2_intraday_streamer.py'])
        logger.info("✅ S2 KIS Intraday Streamer started in background.")
    except Exception as e:
        logger.error(f"Failed to start S2 Intraday Streamer: {e}")
        
    while True:
        now = datetime.now()
        # 09:00 ~ 15:20
        if (now.hour == 9 and now.minute >= 0) or \
           (10 <= now.hour <= 14) or \
           (now.hour == 15 and now.minute <= 20):
            logger.info("Triggering run_pipeline.sh intraday...")
            try:
                subprocess.run(['/bin/bash', 'scripts/run_pipeline.sh', 'intraday'], check=False)
            except Exception as e:
                logger.error(f"Execution failed: {e}")
        else:
            # Optionally sleep longer when outside of hours, but 1-min loop is fine.
            pass
        
        # Sleep until the start of the next minute
        now2 = datetime.now()
        sleep_sec = 60 - now2.second
        time.sleep(sleep_sec)

if __name__ == '__main__':
    main()

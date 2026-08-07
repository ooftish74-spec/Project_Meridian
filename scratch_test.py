import sys
from datetime import datetime
from src.data_collection.unified_collector import collect_global_signals
import logging
logging.basicConfig(level=logging.INFO)
collect_global_signals()

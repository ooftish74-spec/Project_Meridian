import json
import logging
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

class RedisStateBackend:
    """
    Redis In-Memory State Backend for Project Meridian.
    Provides sub-millisecond state persistence and crash recovery.
    """
    def __init__(self, host='localhost', port=6379, db=0):
        self.host = host
        self.port = port
        self.db = db
        self.r = None
        self.use_fake = False
        self._connect()

    def _connect(self):
        try:
            import redis
            self.r = redis.Redis(host=self.host, port=self.port, db=self.db, decode_responses=True)
            self.r.ping()
            logger.info(f"🟢 Connected to Redis at {self.host}:{self.port}/{self.db} (AOF persistence assumed)")
        except Exception as e:
            logger.warning(f"⚠️ Redis connection failed: {e}. Falling back to fakeredis (in-memory only).")
            try:
                import fakeredis
                self.r = fakeredis.FakeRedis(decode_responses=True)
                self.use_fake = True
                logger.info("🟢 Using fakeredis for state backend.")
            except ImportError:
                logger.error("❌ fakeredis not found. State recovery will not work.")
                self.r = None

    def log_order_intent(self, order_id: str, intent_data: Dict[str, Any]):
        """Logs the intention to place an order BEFORE API call (Crash Recovery)"""
        if not self.r:
            return
        try:
            self.r.hset("meridian:intents", order_id, json.dumps(intent_data))
        except Exception as e:
            logger.error(f"Failed to log order intent to Redis: {e}")

    def remove_order_intent(self, order_id: str):
        """Removes the intent after successful execution/cancellation"""
        if not self.r:
            return
        try:
            self.r.hdel("meridian:intents", order_id)
        except Exception as e:
            logger.error(f"Failed to remove order intent from Redis: {e}")

    def save_position(self, ticker: str, pos_data: Dict[str, Any]):
        """Real-time update of a single position"""
        if not self.r:
            return
        try:
            self.r.hset("meridian:positions", ticker, json.dumps(pos_data))
        except Exception as e:
            logger.error(f"Failed to save position to Redis: {e}")

    def save_capital(self, capital_data: Dict[str, Any]):
        """Real-time update of capital state"""
        if not self.r:
            return
        try:
            self.r.set("meridian:capital", json.dumps(capital_data))
        except Exception as e:
            logger.error(f"Failed to save capital to Redis: {e}")

    def save_trade_history(self, history: list):
        """Save the trade history"""
        if not self.r:
            return
        try:
            self.r.set("meridian:trade_history", json.dumps(history))
        except Exception as e:
            logger.error(f"Failed to save trade history to Redis: {e}")

    def load_full_state(self) -> Dict[str, Any]:
        """Reconstructs the full shadow portfolio state from Redis (Sub-ms latency)"""
        if not self.r:
            return {}
        try:
            state = {
                'positions': {},
                'capital': {},
                'trade_history': [],
                'pending_intents': {}
            }
            
            # Load Positions
            positions_raw = self.r.hgetall("meridian:positions")
            for ticker, pos_str in positions_raw.items():
                state['positions'][ticker] = json.loads(pos_str)
            
            # Load Capital
            capital_raw = self.r.get("meridian:capital")
            if capital_raw:
                state['capital'] = json.loads(capital_raw)
                
            # Load History
            history_raw = self.r.get("meridian:trade_history")
            if history_raw:
                state['trade_history'] = json.loads(history_raw)
                
            # Load Intents
            intents_raw = self.r.hgetall("meridian:intents")
            for order_id, intent_str in intents_raw.items():
                state['pending_intents'][order_id] = json.loads(intent_str)
                
            return state
        except Exception as e:
            logger.error(f"Failed to load state from Redis: {e}")
            return {}

    def clear_state(self):
        """Clears all state from Redis (e.g., at end of day or hard reset)"""
        if not self.r:
            return
        try:
            self.r.delete("meridian:positions", "meridian:capital", "meridian:trade_history", "meridian:intents")
        except Exception as e:
            logger.error(f"Failed to clear Redis state: {e}")

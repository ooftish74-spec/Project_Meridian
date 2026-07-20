import os
import json
import logging

logger = logging.getLogger(__name__)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

class AlphaFactorySubscriber:
    """
    [Triangular Architecture: Meridian Side]
    Alpha Factory가 발행한 JSON 시그널을 수신합니다.
    이 코드는 Meridian 메인 루프에 절대 크래시를 유발하지 않도록 철저한 3중 예외 처리를 거칩니다.
    """
    def __init__(self, data_dir: str = "../../data"):
        self.signal_file = os.path.abspath(os.path.join(os.path.dirname(__file__), data_dir, "alpha_signal.json"))
        
        # [FEATURE FLAG] 아직 완전히 검증되지 않았다면 False로 두고 로그만 찍습니다. (Shadow Mode)
        # Meridian의 dynamic_config.py에서 관리되는 값을 주입받는 것이 정석입니다.
        self.enable_alpha_factory = False 
        
        # Alpha Factory 통신 실패 시 반환할 기본(아무런 영향을 주지 않는) 안전 신호
        self.default_safe_signal = {
            "status": "FALLBACK_MODE",
            "S1_signal": {"contagion_alert": "NORMAL", "vix_spike": False},
            "S2_signal": {"pysr_macro_feature_value": 0.0},
            "S3_signal": {"sector_forecast": {}},
            "S4_signal": {"macro_cycle": "Neutral"}
        }

    def get_latest_signals(self) -> dict:
        """Alpha Factory의 최신 시그널을 읽어옵니다. 에러 발생 시 즉시 Fallback을 반환합니다."""
        
        # 1중 방어: 피처 플래그가 꺼져있으면 무조건 기본값 반환 (Shadow Mode 전용)
        if not self.enable_alpha_factory:
            logging.info("[SHADOW MODE] Alpha Factory integration is OFF. Returning default safe signal.")
            return self.default_safe_signal
            
        try:
            # 2중 방어: 파일 존재 여부 확인
            if not os.path.exists(self.signal_file):
                logging.warning(f"Alpha Signal file missing at {self.signal_file}. Using Fallback.")
                return self.default_safe_signal
                
            # 3중 방어: JSON 파싱 및 데이터 읽기
            with open(self.signal_file, 'r', encoding='utf-8') as f:
                signal_data = json.load(f)
                
            logging.info(f"Successfully loaded Alpha Signal from {signal_data.get('timestamp')}")
            return signal_data
            
        except json.JSONDecodeError as e:
            logging.error(f"Alpha Signal JSON is corrupted ({e}). Using Fallback.")
            return self.default_safe_signal
        except Exception as e:
            logging.error(f"Unexpected error while reading Alpha Signal ({e}). Using Fallback.")
            return self.default_safe_signal

if __name__ == "__main__":
    # Test the subscriber
    subscriber = AlphaFactorySubscriber()
    
    # 1. Shadow Mode Test (Should return fallback)
    logger.debug("=== Test 1: Feature Flag OFF ===")
    safe_signal = subscriber.get_latest_signals()
    logger.info(safe_signal)
    
    # 2. Live Mode Test
    logger.debug("\n=== Test 2: Feature Flag ON ===")
    subscriber.enable_alpha_factory = True
    live_signal = subscriber.get_latest_signals()
    logger.info(live_signal)

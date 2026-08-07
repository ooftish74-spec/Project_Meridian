import re

file_path = "scripts/daily_pipeline.py"

with open(file_path, "r") as f:
    content = f.read()

old_block = """            try:
                from src.data.market_data_bridge import MarketDataBridge
                _bridge = MarketDataBridge()
                _sc = _bridge.build_signal_cache()
                _ov = _bridge.build_overnight_intel()
                _rh = _bridge.get_regime_history()
                _market_data = {
                    'signal_cache': _sc,
                    'overnight_intel': _ov,
                    'vix_history': _rh.get('vix_history', []),
                    'kospi_returns': _rh.get('kospi_returns', []),
                }
            except RuntimeError as re:
                logger.error(f"🚨 [Uncertainty Explosion] MarketDataBridge 수집 실패: {re}. '청산 전용 모드' 강제 전환.")
                _market_data = {'signal_cache': {}, 'overnight_intel': {}, 'vix_history': [], 'kospi_returns': [], 'exit_only': True}
                is_fresh = False"""

new_block = """            try:
                import time, random
                _max_retries = 5
                _market_data = None
                for _attempt in range(_max_retries):
                    try:
                        from src.data.market_data_bridge import MarketDataBridge
                        _bridge = MarketDataBridge()
                        _sc = _bridge.build_signal_cache()
                        _ov = _bridge.build_overnight_intel()
                        _rh = _bridge.get_regime_history()
                        _market_data = {
                            'signal_cache': _sc,
                            'overnight_intel': _ov,
                            'vix_history': _rh.get('vix_history', []),
                            'kospi_returns': _rh.get('kospi_returns', []),
                        }
                        break  # 성공 시 루프 탈출
                    except Exception as e:
                        if _attempt < _max_retries - 1:
                            _sleep_sec = (60 * (2 ** _attempt)) + random.randint(1, 30)
                            logger.warning(f"  ⏳ [Backoff] API 점검/오류 예상. {_sleep_sec}초 후 재시도 ({_attempt+1}/{_max_retries}). 사유: {e}")
                            time.sleep(_sleep_sec)
                        else:
                            logger.error(f"🚨 [API Failure] {_max_retries}회 재시도 실패. KIS API 점검 장기화 예상.")
                            raise RuntimeError(f"Data fetch failed after {_max_retries} retries: {e}")
                            
            except Exception as re:
                logger.error(f"🚨 [Uncertainty Explosion] MarketDataBridge 수집 최종 실패: {re}. '청산 전용 모드' 강제 전환.")
                _market_data = {'signal_cache': {}, 'overnight_intel': {}, 'vix_history': [], 'kospi_returns': [], 'exit_only': True}
                is_fresh = False"""

content = content.replace(old_block, new_block)

with open(file_path, "w") as f:
    f.write(content)

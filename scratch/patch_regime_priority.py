import re

file_path = "src/regime/regime_detector.py"

with open(file_path, "r") as f:
    content = f.read()

# I need to find the `detect(self, market_data: Dict)` method and insert priority check at the start.
detect_def = "    def detect(self, market_data: Dict) -> Dict:\n"
priority_check = """        \"\"\"레짐 감지. (Priority 락 체크 포함)\"\"\"
        try:
            import json
            from datetime import datetime
            state_file = Path(__file__).resolve().parent.parent.parent / 'results' / 'regime_state.json'
            if state_file.exists():
                with open(state_file, 'r') as f:
                    state_data = json.load(f)
                
                # Check Priority & TTL
                if state_data.get('priority', 3) == 1 and state_data.get('ttl_until'):
                    ttl = datetime.fromisoformat(state_data['ttl_until'])
                    if datetime.now() < ttl:
                        logger.warning(f"  🚨 [Priority Lock] 우선순위가 높은(1) 수동/NightWatch 레짐 상태가 발효 중입니다. 남은 시간: {ttl - datetime.now()}")
                        logger.warning(f"  🚨 [Priority Lock] 강제로 {state_data.get('current_state')} 상태를 유지합니다. 일반 앙상블 분석은 기각(Bypass)됩니다.")
                        return {
                            'regime': state_data.get('current_state', 'crash'),
                            'confidence': 1.0,
                            'rule_regime': 'crash',
                            'rule_score': 0.0,
                            'hmm_regime': 'crash',
                            'hmm_state': -1,
                            'method': 'priority_lock_override'
                        }
        except Exception as e:
            logger.error(f"  Priority Lock 검사 중 오류: {e}")

"""

# Let's replace the top of the function
if "[Priority Lock]" not in content:
    content = content.replace("    def detect(self, market_data: Dict) -> Dict:\n        \"\"\"레짐 감지.\n", detect_def + priority_check)
    with open(file_path, "w") as f:
        f.write(content)

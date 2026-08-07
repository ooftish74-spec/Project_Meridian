import re
from pathlib import Path

target_file = Path('src/data/market_data_bridge.py')
content = target_file.read_text(encoding='utf-8')

# Fix line 265: except RuntimeError: pass
content = re.sub(r'except RuntimeError:\s*from src\.utils\.error_logger.*?\s*log_error_rate_limited.*?\(exception variable 없음\).*?\s*pass', 
                 r'except RuntimeError as e:\n                    logger.error(f"🚨 [Critical Data Outage] RuntimeError: {e}")\n                    raise', content, flags=re.DOTALL)

# Fix other Silent Bypasses to raise or return 999.0 instead of fallback 2600.0
content = re.sub(r"logger\.error\('  ❌ 모든 KOSPI 소스 실패\. Fallback KOSPI=2600\.0 사용\.'\)\n\s*self\._field_quality\['kospi'\] = 0\.0\n\s*return \{'close': 2600\.0, 'ma20': 2600\.0\}",
                 r"logger.error('  ❌ 모든 KOSPI 소스 실패. [Uncertainty Explosion] 999.0 반환.')\n        self._field_quality['kospi'] = 0.0\n        return {'close': 999.0, 'ma20': 999.0, 'volatility': 999.0}", content)

target_file.write_text(content, encoding='utf-8')
print("Patched market_data_bridge.py")

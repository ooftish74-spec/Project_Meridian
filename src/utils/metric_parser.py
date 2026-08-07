"""
데이터 파싱 유틸리티 (Metric Parser)
- 다양한 형식(dict, float, str, 대소문자 섞임)으로 저장된 캐시 데이터에서 안전하게 지표를 추출.
"""
from typing import Dict, Any

def parse_vix(cache: Dict[str, Any], default: float = 18.0) -> float:
    """
    캐시에서 VIX(변동성 지수) 값을 안전하게 파싱합니다.
    (AlphaVantage 잔재인 'VIX': {'value': 21.0} 형태 및 대소문자 혼용 완벽 방어)
    """
    if not isinstance(cache, dict):
        return default
        
    vix_val = None
    if 'vix' in cache:
        vix_val = cache['vix']
    elif 'VIX' in cache:
        vix_val = cache['VIX']
        
    if vix_val is None:
        return default
        
    try:
        import math
        if isinstance(vix_val, dict):
            val = float(vix_val.get('value', default))
        else:
            val = float(vix_val)
            
        if math.isnan(val) or math.isinf(val):
            return default
        return val
    except (TypeError, ValueError):
        return default

def parse_metric(cache: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """
    캐시에서 임의의 메트릭(float)을 안전하게 파싱합니다. (대소문자 무시)
    """
    if not isinstance(cache, dict):
        return default
        
    lower_key = key.lower()
    val = None
    for k, v in cache.items():
        if str(k).lower() == lower_key:
            val = v
            break
            
    if val is None:
        return default
        
    try:
        import math
        if isinstance(val, dict):
            parsed_val = float(val.get('value', default))
        else:
            parsed_val = float(val)
            
        if math.isnan(parsed_val) or math.isinf(parsed_val):
            return default
        return parsed_val
    except (TypeError, ValueError):
        return default

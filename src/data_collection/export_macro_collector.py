"""[Phase 78] 고해상도 수출입 매크로 수집기.

관세청 10/20일 속보 + 산업부 MOTIE 6개 핵심 섹터 + 3개 지역별 데이터.
Fail-Fast: 외부 통신 실패 시 빈 딕셔너리 반환.

출력: results/export_macro_snapshot.json
"""
from __future__ import annotations
import json, logging, re, sys
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional
import requests

from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
logger = logging.getLogger(__name__)

_RESULTS_DIR = ROOT / 'results'
_OUTPUT_FILE  = _RESULTS_DIR / 'export_macro_snapshot.json'
_TIMEOUT = 20
_SURPRISE_THRESHOLD = 20.0  # YoY +20% 서프라이즈

_SECTOR_KW: Dict[str, list] = {
    'auto':      ['자동차', '승용차'],
    'ship':      ['선박', '조선'],
    'battery':   ['이차전지', '배터리'],
    'steel':     ['철강', '강관'],
    'petrochem': ['석유화학', '합성수지'],
    'beauty':    ['화장품', '뷰티'],
    'semi':      ['반도체', '메모리'],
}
_REGION_KW: Dict[str, list] = {
    'us':    ['대미', '미국'],
    'china': ['대중', '중국'],
    'eu':    ['EU', '유럽'],
}

def _naver_creds() -> tuple:
    from src.utils.credential_manager import CredentialManager
    cm = CredentialManager()
    return cm.read_from_keychain('NAVER_CLIENT_ID') or '', cm.read_from_keychain('NAVER_CLIENT_SECRET') or ''

def _news_yoy(query: str) -> Optional[float]:
    cid, sec = _naver_creds()
    if not (cid and sec): return None
    try:
        r = requests.get('https://openapi.naver.com/v1/search/news.json',
            headers={'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': sec},
            params={'query': query, 'display': 5, 'sort': 'date'}, timeout=int(cfg.get('data.http_timeout', 20)))
        r.raise_for_status()
        vals = []
        for it in r.json().get('items', []):
            text = re.sub(r'<[^>]+>', '', it.get('title','') + ' ' + it.get('description',''))
            for m in re.finditer(r'([+-]?\d+\.?\d*)\s*%', text):
                v = float(m.group(1))
                if -80 < v < 300: vals.append(v)
        return round(sum(vals)/len(vals), 2) if vals else None
    except Exception as e:
        logger.error(f'  [Phase78] news_yoy 실패: {e}', exc_info=True)
        return None

class ExportMacroCollector:
    """[Phase 78] 수출 매크로 수집기."""
    SECTORS = list(_SECTOR_KW.keys())
    REGIONS = list(_REGION_KW.keys())

    def __init__(self, cache_hours: float = 6.0):
        self._cache_h = cache_hours

    def _cached(self) -> Optional[Dict]:
        if not _OUTPUT_FILE.exists(): return None
        try:
            d = json.loads(_OUTPUT_FILE.read_text(encoding='utf-8'))
            age = (datetime.now() - datetime.fromisoformat(d.get('timestamp','2000-01-01'))).total_seconds()/3600
            return d if age < self._cache_h else None
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)
            return None

    def collect(self, force: bool = False) -> Dict:
        if not force:
            c = self._cached()
            if c: return c
        try:
            _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out: Dict = {'date': date.today().isoformat(), 'timestamp': datetime.now().isoformat(),
                         'export_total_yoy': None, 'export_10d_yoy': None, 'export_20d_yoy': None,
                         'sector': {}, 'region': {}, 'surprise': {}, 'source': 'export_macro_collector', 'confidence': 0.0}

            # 총수출
            try:
                from src.data_collection.motie_collector import MotieCollector
                d = MotieCollector().collect()
                out['export_total_yoy'] = float(d.get('export_yoy', d.get('export_growth', 0.0)))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f'Silent exception caught in fallback: {e}', exc_info=True)

            # 10/20일 속보
            out['export_10d_yoy'] = _news_yoy('관세청 10일 수출 전년대비')
            out['export_20d_yoy'] = _news_yoy('관세청 20일 수출 전년대비')

            # 섹터
            _surp_thr = float(cfg.get('export.surprise_threshold', 20.0))
            for sec, kws in _SECTOR_KW.items():
                v = _news_yoy(f'{kws[0]} 수출 전년 대비 증감')
                out['sector'][sec] = v if v is not None else 0.0
                out['surprise'][sec] = bool((v or 0) >= _surp_thr)

            # 지역
            for reg, kws in _REGION_KW.items():
                v = _news_yoy(f'{kws[0]} 수출 전년 대비')
                out['region'][f'{reg}_export_yoy'] = v if v is not None else 0.0

            # 신뢰도
            vals = [out['export_10d_yoy'], out['export_20d_yoy'],
                    *out['sector'].values(), *out['region'].values()]
            out['confidence'] = round(sum(1 for v in vals if v and v!=0) / max(1, len(vals)), 3)

            _OUTPUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f'  [Phase78] 수집완료 surprise={[k for k,v in out["surprise"].items() if v]}')
            return out
        except Exception as e:
            logger.error(f'  [Phase78] 오류: {e}', exc_info=True)
            return {}

    def get_sector_features(self) -> Dict[str, float]:
        d = self.collect()
        if not d: return {}
        out = {'export_total_yoy': float(d.get('export_total_yoy') or 0),
               'export_10d_yoy':   float(d.get('export_10d_yoy')   or 0),
               'export_20d_yoy':   float(d.get('export_20d_yoy')   or 0)}
        for k, v in d.get('sector', {}).items():
            out[f'export_yoy_{k}'] = float(v or 0)
        for k, v in d.get('region', {}).items():
            out[k] = float(v or 0)
        return out

    def get_surprise_sectors(self) -> list:
        return [k for k,v in self.collect().get('surprise',{}).items() if v]


# --- 파생 피처 계산 ---
def compute_export_macro_features(snapshot: dict) -> dict:
    """[Phase 78] us_momentum_spread, china_rebound_index 계산."""
    reg    = snapshot.get('region', {})
    sector = snapshot.get('sector', {})
    us_yoy    = float(reg.get('us_export_yoy',    0))
    china_yoy = float(reg.get('china_export_yoy', 0))
    semi_yoy  = float(sector.get('semi', 0))
    _w_china = float(cfg.get('export.china_rebound_china_w', 0.6))
    _w_semi  = float(cfg.get('export.china_rebound_semi_w',  0.4))
    china_rebound_index = max(0.0, china_yoy * _w_china + semi_yoy * _w_semi)
    return {
        'export_total_yoy':    float(snapshot.get('export_total_yoy') or 0),
        'export_10d_yoy':      float(snapshot.get('export_10d_yoy')   or 0),
        'export_yoy_auto':     float(sector.get('auto',     0)),
        'export_yoy_semi':     float(sector.get('semi',     0)),
        'export_yoy_battery':  float(sector.get('battery',  0)),
        'us_export_yoy':       us_yoy,
        'china_export_yoy':    china_yoy,
        'us_momentum_spread':  round(us_yoy - china_yoy, 3),
        'china_rebound_index': round(china_rebound_index, 3),
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    col = ExportMacroCollector()
    s = col.collect(force=True)
    print(json.dumps(s, ensure_ascii=False, indent=2))

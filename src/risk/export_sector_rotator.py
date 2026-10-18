"""[Phase 78] 수출 서프라이즈 섹터 ETF 로테이터.

수출 품목 ↔ KODEX/TIGER ETF 매핑 테이블 기반으로
YoY +20% 이상 서프라이즈 섹터 ETF 비중을 1.5배 오버웨이트.
"""
from __future__ import annotations
try:
    from src.params.tables import load_table  # Wave2 TrackF: ticker tables live in config/params/tables
except Exception:  # pragma: no cover
    import json as _tj, pathlib as _tp
    def load_table(name, default=None):
        _r = next((d for d in _tp.Path(__file__).resolve().parents if (d / 'config' / 'params').is_dir()), None)
        try:
            return _tj.loads((_r / 'config' / 'params' / 'tables' / f'{name}.json').read_text(encoding='utf-8'))
        except Exception:
            return {} if default is None else default
import logging
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)

EXPORT_ETF_MAP: Dict[str, List[str]] = load_table('src.risk.export_sector_rotator.EXPORT_ETF_MAP', {})
EXPORT_ETF_NAMES: Dict[str, str] = load_table('src.risk.export_sector_rotator.EXPORT_ETF_NAMES', {})
_DEFAULT_OW, _DEFAULT_THR = 1.5, 20.0


class ExportSectorRotator:
    """[Phase 78] 수출 서프라이즈 → S4 ETF 오버웨이트 로테이터."""

    def __init__(self, ow: float = None, thr: float = None):
        try:
            from config.dynamic_config import DynamicConfig
            _cfg = DynamicConfig()
            self._ow  = float(ow  if ow  is not None else _cfg.get('rotator.export_overweight_multiplier', _DEFAULT_OW))
            self._thr = float(thr if thr is not None else _cfg.get('rotator.export_yoy_threshold',          _DEFAULT_THR))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            self._ow  = float(ow  if ow  is not None else _DEFAULT_OW)
            self._thr = float(thr if thr is not None else _DEFAULT_THR)

    def get_overweight_tickers(self, feats: Dict[str, float]) -> Dict[str, float]:
        ow: Dict[str, float] = {}
        for sec, tickers in EXPORT_ETF_MAP.items():
            yoy = feats.get(f'export_yoy_{sec}', 0.0)
            if yoy >= self._thr:
                for t in tickers:
                    ow[t] = self._ow
                    logger.info(
                        f'  [Phase78 Rotator] {sec} YoY={yoy:+.1f}% → '
                        f'{EXPORT_ETF_NAMES.get(t, t)} x{self._ow}'
                    )
        return ow

    def apply_rotation(
        self, candidates: List[Dict], feats: Optional[Dict] = None
    ) -> List[Dict]:
        if not feats:
            try:
                from src.data_collection.export_macro_collector import ExportMacroCollector
                feats = ExportMacroCollector().get_sector_features()
            except Exception as e:
                logger.critical(f'  [Phase78] Rotator 피처 로드 실패: {e}', exc_info=True)
                return candidates
        ow_map = self.get_overweight_tickers(feats)
        if not ow_map:
            return candidates
        result, boosted = [], 0
        for item in candidates:
            t = str(item.get('ticker', ''))
            mult = ow_map.get(t, 1.0)
            if mult > 1.0:
                ni = dict(item)
                ni['score']  = round(float(item.get('score',  1.0)) * mult, 4)
                ni['weight'] = round(float(item.get('weight', 1.0)) * mult, 4)
                ni['export_overweight'] = mult
                ni['overweight_reason'] = 'export_surprise_phase78'
                result.append(ni)
                boosted += 1
            else:
                result.append(item)
        logger.info(f"  [Phase78 Rotator] 오버웨이트 적용: {boosted}/{len(candidates)}개")
        return result

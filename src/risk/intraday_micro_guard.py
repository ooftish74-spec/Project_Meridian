"""[Phase 76] Intraday Micro Guard - S1/S2 전용 졸단기 방어막.

거시경제 없이 오직 당일 한국 증시 마이크로 지표만 평가:
  - VIX 급등 (VKOSPI 대체): 전일 대비 +10% 이상 폭등 시 Halt
  - 거래량 수급 이상치: 무문한 페닉 수준 시 일시정지

반환: (True=매매허가, False=일시정지), reason
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger(__name__)

try:
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    cfg = None  # type: ignore[assignment]


def _cfg_get(key: str, default):
    try:
        return cfg.get(key, default) if cfg else default
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return default


class IntradayMicroGuard:
    """[Phase 76] S1/S2 전용 졸단기 방어막.

    거시경제(TE-HRP, Regime) 신호와 완전히 독립적.
    오직 당일 한국 증시 마이크로 지표만 평가.
    """

    def __init__(
        self,
        vix_spike_threshold: float = 0.10,   # VIX 일일 스파이크 임계값 (+10%)
        vix_halt_level:      float = 40.0,   # VIX 절대수준 Halt (40 이상)
        vol_spike_ratio:     float = 3.0,    # 거래량 폭등비율 임계값
    ):
        self._vix_spike = float(
            _cfg_get('micro_guard.vix_spike_threshold', vix_spike_threshold)
        )
        self._vix_halt  = float(
            _cfg_get('micro_guard.vix_halt_level', vix_halt_level)
        )
        self._vol_spike = float(
            _cfg_get('micro_guard.vol_spike_ratio', vol_spike_ratio)
        )

    def _load_signal_cache(self) -> dict:
        """signal_cache.json 또는 pipeline_state.json에서 VIX 로드."""
        # 1순위: data/cache/signal_cache.json
        for candidate in [
            ROOT / 'results' / 'signal_cache.json',
            ROOT / 'data' / 'cache' / 'signal_cache.json',
            ROOT / 'data' / 'signal_cache.json',
            ROOT / 'cache' / 'signal_cache.json',
        ]:
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding='utf-8'))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.critical("[SILENT_BYPASS] Suppressed exception at intraday_micro_guard.py:75", exc_info=True)
        # 2순위: pipeline_state.json
        for candidate in [
            ROOT / 'data' / 'pipeline_state.json',
            ROOT / 'pipeline_state.json',
        ]:
            if candidate.exists():
                try:
                    return json.loads(candidate.read_text(encoding='utf-8'))
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    logger.critical("[SILENT_BYPASS] Suppressed exception at intraday_micro_guard.py:87", exc_info=True)
        return {}

    def _load_vix(self, market_data: dict) -> Tuple[float, float]:
        """(vix_current, vix_prev) 로드."""
        # 1. 외부 주입 market_data 우선
        vix_curr = float(market_data.get('vix', 0.0)) if market_data else 0.0
        vix_prev = float(market_data.get('vix_prev', 0.0)) if market_data else 0.0

        # 2. signal_cache fallback
        if vix_curr <= 0:
            sc = self._load_signal_cache()
            vix_curr = float(sc.get('vix', sc.get('VIX', 0.0)))
            vix_prev = float(sc.get('vix_prev', sc.get('vix_1d', 0.0)))

        return vix_curr, vix_prev

    def _check_vix_spike(self, vix_curr: float, vix_prev: float) -> Tuple[bool, str]:
        """일일 VIX 스파이크 점검."""
        if vix_curr <= 0:
            return True, 'vix_unknown'  # 데이터 없으면 허가
        # 절대수준 Halt
        if vix_curr >= self._vix_halt:
            return False, f'VIX_HALT vix={vix_curr:.1f} >= {self._vix_halt}'
        # 전일 대비 스파이크
        if vix_prev > 0:
            spike = (vix_curr - vix_prev) / vix_prev
            if spike >= self._vix_spike:
                return False, f'VIX_SPIKE +{spike:.1%} (curr={vix_curr:.1f} prev={vix_prev:.1f})'
        return True, 'vix_ok'

    def _check_vol_spike(self, market_data: dict) -> Tuple[bool, str]:
        """거래량 수급 이상치 점검 (orderbook imbalance proxy)."""
        if not market_data:
            return True, 'vol_unknown'
        vol_ratio = float(market_data.get('vol_ratio', 1.0))
        ob_imbal  = float(market_data.get('orderbook_imbalance', 0.0))

        # 거래량 폭등 + 강한 매도 우세이면 Halt
        _ob_thr = float(_cfg_get('micro_guard.orderbook_imbalance_threshold', -0.60))
        if vol_ratio >= self._vol_spike and ob_imbal < _ob_thr:
            return False, f'PANIC_SELL vol_ratio={vol_ratio:.1f}x ob_imbal={ob_imbal:.2f}'
        return True, 'vol_ok'

    def check(
        self,
        market_data: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """[Phase 76] S1/S2 매매 허가 여부.

        Args:
            market_data: {vix, vix_prev, vol_ratio, orderbook_imbalance}
                         없으면 signal_cache.json 자동 로드.

        Returns:
            (True=매매허가, False=일시정지), reason
        """
        if market_data is None:
            market_data = {}

        vix_curr, vix_prev = self._load_vix(market_data)

        # VIX 점검
        vix_ok, vix_reason = self._check_vix_spike(vix_curr, vix_prev)
        if not vix_ok:
            logger.warning(f'  [Phase 76 MicroGuard] HALT: {vix_reason}')
            return False, vix_reason

        # 거래량 포닉 점검
        vol_ok, vol_reason = self._check_vol_spike(market_data)
        if not vol_ok:
            logger.warning(f'  [Phase 76 MicroGuard] HALT: {vol_reason}')
            return False, vol_reason

        logger.debug(f'  [Phase 76 MicroGuard] OK: vix={vix_curr:.1f}')
        return True, 'micro_guard_ok'

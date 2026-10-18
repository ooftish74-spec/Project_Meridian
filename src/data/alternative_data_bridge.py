import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class AlternativeDataBridge:
    """
    대안 데이터(Alternative Data) 연동을 위한 브릿지 모듈.
    실시간 텔레메트리(signal_cache.json, pipeline_latest.json, DART/NLP)에서
    실제 감성 점수를 조회하며, 데이터가 없는 종목은 중립(0.50)을 반환합니다.
    (레거시 ASCII 합산 % 10 더미 난수 로직 전면 폐기)
    """
    def __init__(self):
        self._is_ready = True
        self._cache: Dict[str, float] = {}
        self._load_live_telemetry()

    @staticmethod
    def _normalize_sentiment(raw_val: float, is_bipolar_scale: bool = False) -> float:
        s = float(raw_val)
        if s < 0.0 or is_bipolar_scale:
            s = (s + 1.0) / 2.0
        return max(0.0, min(1.0, s))

    def _load_live_telemetry(self) -> None:
        try:
            sc_path = _PROJECT_ROOT / 'results' / 'signal_cache.json'
            if sc_path.exists():
                sc = json.loads(sc_path.read_text(encoding='utf-8'))
                sent_map = sc.get('ticker_sentiments', sc.get('sentiments', {}))
                is_bipolar = bool(sc.get('sentiment_scale') == '[-1,1]')
                if isinstance(sent_map, dict):
                    vals = []
                    for v in sent_map.values():
                        try:
                            vals.append(float(v))
                        except (ValueError, TypeError):
                            pass
                    if any(v < 0.0 for v in vals):
                        is_bipolar = True
                    for k, v in sent_map.items():
                        try:
                            self._cache[str(k)] = self._normalize_sentiment(float(v), is_bipolar_scale=is_bipolar)
                        except (ValueError, TypeError):
                            pass
        except Exception:
            pass

    def fetch_sentiment_score(self, ticker: str, raw_score: float = None) -> float:
        """
        특정 종목에 대한 최신 감성 점수를 [0.0, 1.0] 범위로 정규화하여 반환합니다.
        실측 감성 데이터가 없을 경우 왜곡 없는 중립(0.50)을 반환합니다.
        """
        if raw_score is not None:
            return self._normalize_sentiment(raw_score, is_bipolar_scale=(float(raw_score) < 0.0))
        if not ticker:
            return 0.50
        tk = str(ticker).strip()
        if tk in self._cache:
            return self._normalize_sentiment(self._cache[tk])
        return 0.50

    def fetch_batch_sentiments(self, tickers: list[str]) -> Dict[str, float]:
        """여러 종목의 감성 점수를 한 번에 조회합니다."""
        return {tk: self.fetch_sentiment_score(tk) for tk in tickers}


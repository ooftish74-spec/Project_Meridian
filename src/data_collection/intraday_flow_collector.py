from __future__ import annotations
import pandas as pd
'\nintraday_flow_collector.py — 장중 실시간 수급·거래량 수집기\n============================================================\n[Phase 36: Intraday Dynamic Adjustment]\n\n핵심 원칙:\n  - 하드코딩 Zero: 모든 파라미터는 DynamicConfig 경유\n  - Graceful Fallback: KISDataCollector 장애 → {} 반환\n  - Rate-Limit 방어: intraday.ticker_api_delay_sec 동적 딜레이\n'
import json
import logging
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results'

class IntradayFlowCollector:
    """[Phase 36] 장중 수급·거래량 수집기 (하드코딩 Zero)."""
    _CONFIG_DEFAULTS: Dict[str, Any] = {'intraday.ticker_api_delay_sec': 0.25, 'intraday.flow_unit_krw': 1000000, 'intraday.max_batch_tickers': 20}

    def __init__(self, config=None, results_dir: Optional[Path]=None):
        try:
            if config is None:
                from config.dynamic_config import DynamicConfig
                config = DynamicConfig()
            self.config = config
        except Exception as e:
            logger.warning('[Flow] DynamicConfig 로드 실패: %s → Fallback 모드', e, exc_info=True)
            self.config = None
        self.results_dir = results_dir or _RESULTS_DIR
        self.cache_file = self.results_dir / 'intraday_flow_cache.json'
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._kis: Optional[Any] = None

    def _cfg(self, key: str) -> Any:
        """DynamicConfig 우선 → _CONFIG_DEFAULTS Fallback."""
        default = self._CONFIG_DEFAULTS.get(key)
        if self.config is None:
            return default
        return self.config.get(key, default)

    def _get_kis(self) -> Optional[Any]:
        if self._kis is not None:
            return self._kis
        try:
            from src.data_collection.kis_data_collector import KISDataCollector
            self._kis = KISDataCollector()
            return self._kis
        except Exception as e:
            logger.warning('[Flow] KISDataCollector 초기화 실패: %s', e, exc_info=True)
            return None

    def _fetch_single(self, ticker: str) -> Dict[str, Any]:
        """단일 종목 수급·거래량 수집."""
        empty: Dict[str, Any] = {'institution_net_qty': 0, 'foreign_net_qty': 0, 'institution_net_krw': 0.0, 'foreign_net_krw': 0.0, 'combined_net_krw': 0.0, 'today_volume': 0, 'prev_volume': 0, 'volume_ratio': 0.0, 'current_price': 0}
        kis = self._get_kis()
        if kis is None:
            return empty
        flow_unit = float(self._cfg('intraday.flow_unit_krw'))
        inst_net_qty: int = 0
        frgn_net_qty: int = 0
        inst_net_krw: float = 0.0
        frgn_net_krw: float = 0.0
        try:
            df = kis.get_investor_trading(ticker)
            today_str = date.today().strftime('%Y%m%d')
            if df is not None and (not df.empty):
                today_rows = df[df.index.strftime('%Y%m%d') == today_str]
                if not today_rows.empty:
                    row = today_rows.iloc[0]

                    def _get_qty(r: pd.Series, prefix: str) -> int:
                        keys = [f'{prefix}_ntby_qty', f'{prefix}_ntby_quantity', f'{prefix}_ntby_vol']
                        for k in keys:
                            if k in r and (not pd.isna(r[k])):
                                return int(r[k] or 0)
                        logger.warning(f'[Flow Error] 필드값 오류: {prefix} 수급 필드가 없습니다! API 명세 변경 의심. Keys: {list(r.keys())}')
                        return 0
                    inst_net_qty = _get_qty(row, 'orgn')
                    frgn_net_qty = _get_qty(row, 'frgn')
                    scale = 1000000 / flow_unit
                    inst_net_krw = float(row.get('orgn_ntby_tr_pbmn', 0) or 0) * scale
                    frgn_net_krw = float(row.get('frgn_ntby_tr_pbmn', 0) or 0) * scale
        except Exception as e:
            logger.warning('[Flow] %s 수급 조회 실패: %s', ticker, e, exc_info=True)
        cur_px: int = 0
        today_vol: int = 0
        prev_vol: int = 0
        vol_ratio: float = 0.0
        try:
            price_data = kis.get_current_price(ticker) or {}
            cur_px = int(price_data.get('stck_prpr', price_data.get('current_price', 0)) or 0)
            today_vol = int(price_data.get('acml_vol', 0) or 0)
            prev_vol = int(price_data.get('prdy_vol', 0) or 0)
            vol_ratio = round(today_vol / prev_vol, 4) if prev_vol > 0 else 0.0
        except Exception as e:
            logger.warning('[Flow] %s 현재가 조회 실패: %s', ticker, e, exc_info=True)
        if inst_net_krw == 0.0 and cur_px > 0:
            inst_net_krw = round(inst_net_qty * cur_px / flow_unit, 2)
        if frgn_net_krw == 0.0 and cur_px > 0:
            frgn_net_krw = round(frgn_net_qty * cur_px / flow_unit, 2)
        return {'institution_net_qty': inst_net_qty, 'foreign_net_qty': frgn_net_qty, 'institution_net_krw': inst_net_krw, 'foreign_net_krw': frgn_net_krw, 'combined_net_krw': round(inst_net_krw + frgn_net_krw, 2), 'today_volume': today_vol, 'prev_volume': prev_vol, 'volume_ratio': vol_ratio, 'current_price': cur_px, 'flow_unit_krw': int(flow_unit)}

    def fetch(self, tickers: List[str]) -> Dict[str, Any]:
        """티커 리스트에 대해 수급·거래량 배치 조회 후 캐시 저장."""
        delay_sec = float(self._cfg('intraday.ticker_api_delay_sec'))
        max_tickers = int(self._cfg('intraday.max_batch_tickers'))
        flow_unit = int(self._cfg('intraday.flow_unit_krw'))
        result: Dict[str, Any] = {'timestamp': datetime.now().isoformat(), 'market_date': date.today().strftime('%Y%m%d'), 'flow_unit_krw': flow_unit, 'tickers': {}}
        if not tickers:
            logger.warning('[Flow] 조회 대상 티커 없음')
            self._save_cache(result)
            return result
        if len(tickers) > max_tickers:
            logger.warning('[Flow] 티커 %d개 → 최대 %d개로 제한', len(tickers), max_tickers)
            tickers = tickers[:max_tickers]
        logger.info('[Flow] 수급 수집: %d종목 | 딜레이=%.2fs | 단위=%d원', len(tickers), delay_sec, flow_unit)
        for ticker in tickers:
            try:
                data = self._fetch_single(ticker)
                result['tickers'][ticker] = data
                logger.info('[Flow] %-8s 기관=%+.0f 외인=%+.0f 거래량비=%.0f%%', ticker, data.get('institution_net_krw', 0), data.get('foreign_net_krw', 0), data.get('volume_ratio', 0) * 100)
            except Exception as e:
                logger.error('[Flow] %s 수집 실패: %s', ticker, e, exc_info=True)
                result['tickers'][ticker] = {}
            time.sleep(delay_sec)
        self._save_cache(result)
        return result

    def _save_cache(self, data: Dict[str, Any]) -> None:
        """원자적 JSON 캐시 저장."""
        tmp = self.cache_file.with_suffix('.tmp')
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self.cache_file)
            logger.debug('[Flow] 캐시 저장: %s', self.cache_file.name)
        except Exception as e:
            logger.error('[Flow] 캐시 저장 실패: %s', e, exc_info=True)

    @staticmethod
    def load_cache(results_dir: Optional[Path]=None) -> Dict[str, Any]:
        """intraday_flow_cache.json 로드 (파일 없으면 {} 반환)."""
        cache_path = (results_dir or _RESULTS_DIR) / 'intraday_flow_cache.json'
        try:
            raw = json.loads(cache_path.read_text(encoding='utf-8'))
            logger.debug('[Flow] 캐시 로드: %s', cache_path.name)
            return raw
        except FileNotFoundError:
            logger.warning('[Flow] 캐시 없음 — 정적 Trailing Stop 사용', exc_info=True)
            return {}
        except Exception as e:
            logger.warning('[Flow] 캐시 로드 실패: %s', e, exc_info=True)
            return {}

    @staticmethod
    def get_ticker_flow(cache: Dict[str, Any], ticker: str) -> Dict[str, Any]:
        """캐시에서 특정 종목 수급 데이터 추출."""
        return cache.get('tickers', {}).get(ticker, {})
if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%H:%M:%S')
    parser = argparse.ArgumentParser(description='[Phase 36] 장중 수급·거래량 수집기')
    parser.add_argument('--tickers', nargs='+', default=None, help='조회할 종목코드 (기본: config.intraday.default_watch_tickers)')
    parser.add_argument('--dry-run', action='store_true', help='Mock 데이터로 cache 파일 생성 (테스트용)')
    args = parser.parse_args()
    from config.dynamic_config import DynamicConfig
    DynamicConfig._instance = None
    _cfg_obj = DynamicConfig()
    _tickers = args.tickers or _cfg_obj.get('intraday.default_watch_tickers', [])
    _unit = _cfg_obj.get('intraday.flow_unit_krw', 1000000)
    _strong = _cfg_obj.get('intraday.flow_strong_threshold_krw', 5000000000)
    _combined_m = round(_strong * 1.2 / _unit, 1)
    if args.dry_run:
        mock = {'timestamp': datetime.now().isoformat(), 'market_date': date.today().strftime('%Y%m%d'), 'flow_unit_krw': int(_unit), 'tickers': {t: {'institution_net_qty': 50000, 'foreign_net_qty': 30000, 'institution_net_krw': round(_combined_m * 0.625, 1), 'foreign_net_krw': round(_combined_m * 0.375, 1), 'combined_net_krw': _combined_m, 'today_volume': 8000000, 'prev_volume': 10000000, 'volume_ratio': 0.8, 'current_price': 75000, 'flow_unit_krw': int(_unit)} for t in _tickers}}
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        _p = _RESULTS_DIR / 'intraday_flow_cache.json'
        _p.write_text(json.dumps(mock, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info(f'[DryRun] 저장 완료: {_p}')
        logger.debug(json.dumps(mock, ensure_ascii=False, indent=2))
    else:
        collector = IntradayFlowCollector(config=_cfg_obj)
        result = collector.fetch(_tickers)
        logger.debug(json.dumps(result, ensure_ascii=False, indent=2))
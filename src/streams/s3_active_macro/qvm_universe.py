"""
S3 QVM Universe Builder — S3 내장 유니버스 구축
================================================

S5 LargeCapUniverse 로직을 S3에 직접 흡수.
ISA QVAL + BROKERAGE QV Core에서 사용하는 개별주 유니버스 구축.

데이터 소스:
  1. data/financials_history/{ticker}.json (DART 재무제표)
  2. data/market_cap_cache.json (시가총액 캐시)
  3. data/stock_names.json (종목명)
  4. results/pa_results/stock_list_cache.json (섹터 정보)

필터링:
  - 자본잠식 종목 제외 (total_equity <= 0)
  - 2년 연속 적자 종목 제외
  - 시총 기준 상위 N 선정
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FINANCIALS_DIR = _PROJECT_ROOT / 'data' / 'financials_history'
_MARKET_CAP_CACHE = _PROJECT_ROOT / 'data' / 'market_cap_cache.json'
_STOCK_NAMES_FILE = _PROJECT_ROOT / 'data' / 'stock_names.json'
_STOCK_LIST_CACHE = _PROJECT_ROOT / 'results' / 'pa_results' / 'stock_list_cache.json'

class QVMUniverse:
    """KOSPI 시총 상위 종목 유니버스 빌더 (S3 내장).

    Usage:
        universe = QVMUniverse()
        stocks = universe.build_universe()
    """

    def __init__(self):
        self._market_cap_data: Optional[Dict] = None
        self._stock_info: Optional[Dict] = None

    def build_universe(self, top_n: Optional[int]=None) -> List[Dict]:
        """시총 상위 N종목 유니버스 구축 + 섹터 대표주 보장.

        Args:
            top_n: 상위 N종목 (기본: config s3.universe_size, 50)

        Returns:
            정렬된 종목 리스트 (시총 내림차순, 섹터 대표주 포함)
        """
        if top_n is None:
            top_n = cfg.get('s3.universe_size', 50)
        logger.info(f'  S3 QVM 유니버스 구축 시작 (TOP {top_n})')
        market_caps = self._load_market_caps()
        stock_info = self._load_stock_info()
        candidates = []
        if not _FINANCIALS_DIR.exists():
            logger.warning(f'  재무제표 디렉토리 없음: {_FINANCIALS_DIR}')
            return []
        for fp in sorted(_FINANCIALS_DIR.glob('*.json')):
            ticker = fp.stem
            try:
                data = json.loads(fp.read_text())
                annual = data if isinstance(data, list) else data.get('annual', [])
                if not annual:
                    continue
                latest = annual[-1]
                prev = annual[-2] if len(annual) >= 2 else {}
                equity = float(latest.get('total_equity', 0) or 0)
                if equity <= 0:
                    continue
                ni_curr = float(latest.get('net_income', 0) or 0)
                ni_prev = float(prev.get('net_income', 0) or 0) if prev else 1
                if ni_curr < 0 and ni_prev < 0:
                    continue
                mcap = market_caps.get(ticker)
                if mcap is None or mcap <= 0:
                    mcap = equity
                ta = float(latest.get('total_assets', 0) or 0)
                tl = float(latest.get('total_liabilities', 0) or 0)
                revenue = float(latest.get('revenue', 0) or 0)
                op_inc = float(latest.get('operating_income', 0) or 0)
                ni = float(latest.get('net_income', 0) or 0)
                cfo = float(latest.get('cash_from_operations', 0) or 0)
                gp = float(latest.get('gross_profit', 0) or 0)
                info = stock_info.get(ticker, {})
                candidates.append({'ticker': ticker, 'name': info.get('name', ticker), 'sector': info.get('sector', 'unknown'), 'market_cap': mcap, 'total_assets': ta, 'total_liabilities': tl, 'total_equity': equity, 'revenue': revenue, 'operating_income': op_inc, 'net_income': ni, 'cash_from_operations': cfo, 'gross_profit': gp, 'annual_data': annual, 'is_sector_leader': False})
            except Exception as e:
                logger.debug(f'  {ticker} 로드 실패: {e}')
                continue
        candidates.sort(key=lambda x: x['market_cap'], reverse=True)
        sector_leaders = self._get_sector_leaders(candidates)
        top_universe = candidates[:top_n]
        top_tickers = {s['ticker'] for s in top_universe}
        added_leaders = 0
        for leader in sector_leaders:
            if leader['ticker'] not in top_tickers:
                leader['is_sector_leader'] = True
                top_universe.append(leader)
                top_tickers.add(leader['ticker'])
                added_leaders += 1
            else:
                for s in top_universe:
                    if s['ticker'] == leader['ticker']:
                        s['is_sector_leader'] = True
                        break
        universe = top_universe
        logger.info(f'  S3 QVM 유니버스: {len(candidates)}후보 → {len(universe)}종목 선정 (섹터 대표주 {len(sector_leaders)}종목, 추가편입 {added_leaders}종목)')
        if universe:
            top3 = ', '.join((f'{s['name']}({s['ticker']})' for s in universe[:3]))
            logger.info(f'    상위 3: {top3}')
            leaders_str = ', '.join((f'{s['name']}({s['sector']})' for s in universe if s.get('is_sector_leader')))
            if leaders_str:
                logger.info(f'    섹터 대표주: {leaders_str}')
        return universe

    def _get_sector_leaders(self, candidates: List[Dict]) -> List[Dict]:
        """각 섹터에서 시총 1위 종목을 동적으로 선정.

        하드코딩 없음 — 전체 후보에서 섹터별 그룹핑 후 시총 1위를
        자동 선정합니다. 시총 상위 N위 내에 있는 종목만 자격 부여.

        Returns:
            섹터 대표주 리스트
        """
        if not candidates:
            return []
        leader_min_rank = cfg.get('s3.sector_leader_min_mcap_rank', 30)
        sector_stocks: Dict[str, List[Dict]] = {}
        for i, stock in enumerate(candidates):
            sector = stock.get('sector', 'unknown')
            if sector == 'unknown':
                continue
            if i < leader_min_rank:
                if sector not in sector_stocks:
                    sector_stocks[sector] = []
                sector_stocks[sector].append(stock)
        leaders = []
        for sector, stocks in sector_stocks.items():
            if stocks:
                leader = max(stocks, key=lambda s: s['market_cap'])
                leader_copy = dict(leader)
                leader_copy['is_sector_leader'] = True
                leaders.append(leader_copy)
        return leaders

    def get_sector_weights(self, universe: List[Dict]) -> Dict[str, float]:
        """섹터별 비중 계산 (25% 상한 적용)."""
        if not universe:
            return {}
        max_sector = cfg.get('s3.max_sector_weight', 0.25)
        total_mcap = sum((s['market_cap'] for s in universe))
        if total_mcap <= 0:
            return {}
        sector_mcap: Dict[str, float] = {}
        for s in universe:
            sec = s.get('sector', 'unknown')
            sector_mcap[sec] = sector_mcap.get(sec, 0) + s['market_cap']
        weights = {}
        for sec, mcap in sector_mcap.items():
            weights[sec] = min(max_sector, mcap / total_mcap)
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {k: round(v / total_w, 4) for k, v in weights.items()}
        return dict(sorted(weights.items(), key=lambda x: -x[1]))

    def _load_market_caps(self) -> Dict[str, float]:
        """시가총액 캐시 로드."""
        if self._market_cap_data is not None:
            return self._market_cap_data
        self._market_cap_data = {}
        if _MARKET_CAP_CACHE.exists():
            try:
                data = json.loads(_MARKET_CAP_CACHE.read_text())
                if isinstance(data, dict):
                    for ticker, info in data.items():
                        if isinstance(info, dict):
                            mcap = info.get('market_cap', 0)
                        elif isinstance(info, (int, float)):
                            mcap = info
                        else:
                            continue
                        if mcap and mcap > 0:
                            self._market_cap_data[ticker] = float(mcap)
                logger.info(f'  시가총액 캐시: {len(self._market_cap_data)}종목 로드')
            except Exception as e:
                logger.debug(f'  시가총액 캐시 로드 실패: {e}')
        return self._market_cap_data

    def _load_stock_info(self) -> Dict[str, Dict]:
        """종목명/섹터 정보 로드."""
        if self._stock_info is not None:
            return self._stock_info
        self._stock_info = {}
        if _STOCK_NAMES_FILE.exists():
            try:
                names = json.loads(_STOCK_NAMES_FILE.read_text())
                if isinstance(names, dict):
                    for ticker, name in names.items():
                        self._stock_info[ticker] = {'name': name if isinstance(name, str) else str(name), 'sector': 'unknown'}
                logger.info(f'  종목명 매핑: {len(self._stock_info)}종목 로드')
            except Exception as e:
                logger.debug(f'  stock_names.json 로드 실패: {e}')
        if _STOCK_LIST_CACHE.exists():
            try:
                data = json.loads(_STOCK_LIST_CACHE.read_text())
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get('ticker'):
                            tk = item['ticker']
                            existing = self._stock_info.get(tk, {})
                            self._stock_info[tk] = {'name': existing.get('name') or item.get('name', tk), 'sector': item.get('sector', existing.get('sector', 'unknown'))}
                elif isinstance(data, dict):
                    for ticker, info in data.items():
                        if isinstance(info, dict):
                            existing = self._stock_info.get(ticker, {})
                            self._stock_info[ticker] = {'name': existing.get('name') or info.get('name', ticker), 'sector': info.get('sector', existing.get('sector', 'unknown'))}
            except Exception as e:
                logger.debug(f'  stock_list_cache 로드 실패: {e}')
        return self._stock_info
"""
Polars Analyzer — 초고속 백테스트 이벤트 분석 (Parquet + Polars)
================================================================

Pandas 대신 Polars를 활용하여 대규모 백테스트 Parquet
이벤트 로그를 병렬/지연(Lazy) 처리하여 초고속으로 분석.

Usage:
    from src.analysis.polars_analyzer import PolarsAnalyzer
    analyzer = PolarsAnalyzer()
    stats = analyzer.analyze_trades()
    report = analyzer.full_report()
"""
import json
import logging
import math
from pathlib import Path
from typing import Dict, Any, Optional, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EVENTS_DIR = _PROJECT_ROOT / 'data' / 'events'

class PolarsAnalyzer:
    """Polars 기반 백테스트/이벤트 로그 고속 분석기.

    모든 파라미터는 DynamicConfig(YAML)에서 로드 — 하드코딩 없음.
    주요 분석 지표: PnL, Sharpe, Sortino, Calmar, 슬리피지, 거래통계.
    """

    def __init__(self):
        self.cfg = DynamicConfig()
        try:
            import polars as pl
            self.pl = pl
        except ImportError as e:
            logger.error("polars 패키지가 필요합니다. 'pip install polars' 실행.", exc_info=True)
            self.pl = None
        self._trade_event = self.cfg.get('backtest.event_type_trade', 'TRADE')
        self._buy_dir = self.cfg.get('backtest.event_dir_buy', 'buy')
        self._sell_dir = self.cfg.get('backtest.event_dir_sell', 'sell')
        self._risk_free = float(self.cfg.get('backtest.risk_free_annual', 0.035))
        self._trading_days = int(self.cfg.get('backtest.trading_days_per_year', 252))

    def analyze_trades(self, year_month: Optional[str]=None) -> Dict[str, Any]:
        """TRADE 이벤트 로그를 분석하여 매매 통계 산출.

        Args:
            year_month: 'YYYY-MM' 포맷. 지정하지 않으면 전체 분석.

        Returns:
            Dict: 매매 통계 (거래횟수, 방향, 슬리피지 등)
        """
        df = self._load_parquet(year_month)
        if df is None or len(df) == 0:
            return {}
        pl = self.pl
        trades = df.filter(pl.col('type') == self._trade_event)
        if len(trades) == 0:
            return {'total_trades': 0}
        stats = {'total_trades': len(trades)}
        buy_count, sell_count = (0, 0)
        slippage_list: List[float] = []
        pnl_list: List[float] = []
        for row in trades.iter_rows(named=True):
            try:
                payload = json.loads(row.get('payload', '{}'))
            except (json.JSONDecodeError, TypeError):
                payload = {}
            direction = payload.get('direction', payload.get('dir', ''))
            if self._buy_dir in direction.lower():
                buy_count += 1
            elif self._sell_dir in direction.lower():
                sell_count += 1
            slip = payload.get('slippage', payload.get('slip', None))
            if slip is not None:
                try:
                    slippage_list.append(float(slip))
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
            pnl = payload.get('pnl', payload.get('realized_pnl', None))
            if pnl is not None:
                try:
                    pnl_list.append(float(pnl))
                except (ValueError, TypeError):
                    from src.utils.error_logger import log_error_rate_limited
                    logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        stats['buy_count'] = buy_count
        stats['sell_count'] = sell_count
        stats['buy_ratio'] = round(buy_count / max(len(trades), 1), 4)
        if slippage_list:
            stats['avg_slippage_bp'] = round(sum(slippage_list) / len(slippage_list) * 10000, 2)
            stats['max_slippage_bp'] = round(max(slippage_list) * 10000, 2)
        if pnl_list:
            total_pnl = sum(pnl_list)
            stats['total_pnl'] = round(total_pnl, 0)
            stats['avg_trade_pnl'] = round(total_pnl / len(pnl_list), 0)
            win_trades = [p for p in pnl_list if p > 0]
            stats['win_rate'] = round(len(win_trades) / len(pnl_list), 4)
        return stats

    def analyze_nav(self, year_month: Optional[str]=None) -> Dict[str, Any]:
        """NAV 이벤트 로그에서 포트폴리오 성과 지표 계산.

        Returns:
            cagr, mdd, sharpe, sortino, calmar (모두 동적 계산)
        """
        df = self._load_parquet(year_month)
        if df is None or len(df) == 0:
            return {}
        pl = self.pl
        nav_type = self.cfg.get('backtest.event_type_nav', 'NAV')
        nav_rows = df.filter(pl.col('type') == nav_type)
        if len(nav_rows) < 2:
            return {}
        navs = []
        for row in nav_rows.iter_rows(named=True):
            try:
                payload = json.loads(row.get('payload', '{}'))
                navs.append(float(payload.get('nav', payload.get('value', 0))))
            except (json.JSONDecodeError, TypeError, ValueError):
                from src.utils.error_logger import log_error_rate_limited
                logger.warning("Tier 2/3 Fallback: Caught exception in module. Proceeding with mathematical defaults.", exc_info=True)
        if len(navs) < 2:
            return {}
        n = len(navs)
        yrs = n / self._trading_days
        rf_daily = self._risk_free / self._trading_days
        cagr = (navs[-1] / navs[0]) ** (1 / yrs) - 1 if yrs > 0 and navs[0] > 0 else 0.0
        peak, mdd = (navs[0], 0.0)
        for v in navs:
            peak = max(peak, v)
            mdd = min(mdd, (v - peak) / peak)
        rets = [(navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, n)]
        mean_r = sum(rets) / len(rets)
        std_r = math.sqrt(sum(((r - mean_r) ** 2 for r in rets)) / max(len(rets) - 1, 1))
        sharpe = (mean_r - rf_daily) / std_r * math.sqrt(self._trading_days) if std_r > 1e-09 else 0.0
        dn = [r for r in rets if r < rf_daily]
        dn_std = math.sqrt(sum(((r - rf_daily) ** 2 for r in dn)) / max(len(dn) - 1, 1)) if dn else 1e-09
        sortino = (mean_r - rf_daily) / dn_std * math.sqrt(self._trading_days)
        calmar = cagr / abs(mdd) if mdd < 0 else 0.0
        return {'cagr': round(cagr * 100, 2), 'mdd': round(mdd * 100, 2), 'sharpe': round(sharpe, 3), 'sortino': round(sortino, 3), 'calmar': round(calmar, 3), 'n_days': n}

    def full_report(self, year_month: Optional[str]=None) -> Dict[str, Any]:
        """매매통계 + NAV 지표를 합쳐 전체 리포트 반환."""
        trade_stats = self.analyze_trades(year_month)
        nav_stats = self.analyze_nav(year_month)
        engine = self.cfg.get('backtest.engine', 'polars')
        return {'engine': engine, 'trades': trade_stats, 'nav': nav_stats}

    def _load_parquet(self, year_month: Optional[str]=None):
        """월별 또는 전체 Parquet 로드 (Lazy → collect)."""
        if not self.pl:
            return None
        pl = self.pl
        if year_month:
            files = list(_EVENTS_DIR.glob(f'{year_month}.parquet'))
        else:
            files = list(_EVENTS_DIR.glob('*.parquet'))
        if not files:
            logger.debug('PolarsAnalyzer: 분석할 Parquet 파일 없음')
            return None
        try:
            lazy_dfs = [pl.scan_parquet(f) for f in sorted(files)]
            lf = pl.concat(lazy_dfs)
            return lf.collect()
        except Exception as e:
            logger.error(f'Polars 로드 오류: {e}')
            return None
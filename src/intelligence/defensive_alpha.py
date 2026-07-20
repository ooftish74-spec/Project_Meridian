"""
Defensive Alpha + Quality Growth Discount + Sector Rotation
=============================================================
4. Defensive Alpha: 하락장에서 수익 내는 종목 발굴
5. Contrarian Dip-Buying: 폭락 시 저가매수 시그널
6. Quality + Growth + Discount 스크리너
7. 섹터 로테이션 바닥 감지

Author: Project-A
Date: 2026-04-02
"""
import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, List
logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA = PROJECT_ROOT / 'data'
RESULTS = PROJECT_ROOT / 'results'
CONFIG = PROJECT_ROOT / 'config'
try:
    import sys as _sys
    _sys.path.insert(0, str(PROJECT_ROOT))
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None
_cfg_get = (lambda k, d=None: _cfg.get(k, d)) if _cfg else lambda k, d=None: d

class DefensiveAlphaFinder:
    """하락장에서 수익을 내는 종목 발굴."""

    def compute_stock_beta(self, ticker: str, lookback_days: int=252) -> float:
        """개별 종목의 KOSPI 대비 β 계산."""
        try:
            ETF_DIR = DATA / 'raw' / 'korean_stocks' / 'etf_prices'
            PRICES_DIR = DATA / 'raw' / 'korean_stocks' / 'prices'
            bench = pd.read_csv(ETF_DIR / '069500.csv')
            stock = pd.read_csv(PRICES_DIR / f'{ticker}.csv')
            bench_idx = 'date' if 'date' in bench.columns else bench.columns[0]
            stock_idx = 'date' if 'date' in stock.columns else stock.columns[0]
            bench = bench.sort_values(bench_idx).set_index(bench_idx)['Close'].pct_change().dropna().tail(lookback_days)
            stock = stock.sort_values(stock_idx).set_index(stock_idx)['Close'].pct_change().dropna().tail(lookback_days)
            common = bench.index.intersection(stock.index)
            if len(common) < 60:
                return 1.0
            b = bench.reindex(common).values
            s = stock.reindex(common).values
            cov = np.cov(s, b)
            beta = cov[0, 1] / (cov[1, 1] + 1e-10)
            return round(float(beta), 3)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
            return 1.0

    def find_defensive_stocks(self, top_n: int=20) -> List[Dict]:
        """Low Beta + 역상관 종목 발굴."""
        PRICES_DIR = DATA / 'raw' / 'korean_stocks' / 'prices'
        ETF_DIR = DATA / 'raw' / 'korean_stocks' / 'etf_prices'
        ETF_SET = set((f.stem for f in ETF_DIR.glob('*.csv')))
        candidates = []
        for f in sorted(PRICES_DIR.glob('*.csv'))[:200]:
            ticker = f.stem
            if ticker in ETF_SET:
                continue
            try:
                beta = self.compute_stock_beta(ticker)
                _beta_max = _cfg_get('defensive.low_beta_max', 0.7)
                if beta < _beta_max:
                    df = pd.read_csv(f)
                    _sort_col = 'date' if 'date' in df.columns else df.columns[0]
                    df = df.sort_values(_sort_col)
                    if 'High' not in df.columns:
                        df['High'] = df['Close']
                    if 'Low' not in df.columns:
                        df['Low'] = df['Close']
                    if len(df) > 200:
                        avg_vol = df['Volume'].tail(20).mean()
                        _vol_min = _cfg_get('defensive.avg_vol_min', 50000)
                        if avg_vol > _vol_min:
                            defensive_score = round((1 - beta) * 100, 1)
                            candidates.append({'ticker': ticker, 'beta': beta, 'defensive_score': defensive_score, 'avg_volume': int(avg_vol), 'last_close': float(df['Close'].iloc[-1])})
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        candidates.sort(key=lambda x: x['defensive_score'], reverse=True)
        return candidates[:top_n]

    def find_dip_buying_candidates(self, min_drop_pct: float=-5.0, min_quality: float=70) -> List[Dict]:
        """폭락 시 저가매수 후보 발굴 (Contrarian)."""
        PRICES_DIR = DATA / 'raw' / 'korean_stocks' / 'prices'
        ETF_DIR = DATA / 'raw' / 'korean_stocks' / 'etf_prices'
        ETF_SET = set((f.stem for f in ETF_DIR.glob('*.csv')))
        quality_scores = {}
        dart_dir = DATA / 'dart'
        if dart_dir.exists():
            for ticker_dir in sorted(dart_dir.iterdir()):
                if not ticker_dir.is_dir():
                    continue
                fin_path = ticker_dir / 'financial_summary.csv'
                if not fin_path.exists():
                    continue
                try:
                    df = pd.read_csv(fin_path)
                    annual = df[df['report'] == '연간'] if 'report' in df.columns else df
                    if annual.empty:
                        continue
                    latest_year = annual['year'].max()
                    latest = annual[annual['year'] == latest_year]
                    eq = latest[latest['account'].str.contains('자본총계', na=False)]
                    equity = float(eq['current'].iloc[0]) if len(eq) > 0 else 0
                    ni = latest[latest['account'].str.contains('당기순이익', na=False)]
                    net_income = float(ni['current'].iloc[0]) if len(ni) > 0 else 0
                    debt_row = latest[latest['account'].str.contains('부채총계', na=False)]
                    total_debt = float(debt_row['current'].iloc[0]) if len(debt_row) > 0 else 0
                    op = latest[latest['account'].str.contains('영업이익', na=False)]
                    op_income = float(op['current'].iloc[0]) if len(op) > 0 else 0
                    rev = latest[latest['account'].str.contains('매출액', na=False)]
                    revenue = float(rev['current'].iloc[0]) if len(rev) > 0 else 0
                    roe = net_income / equity * 100 if equity > 0 else 0
                    debt_ratio = total_debt / equity * 100 if equity > 0 else 999
                    opm = op_income / revenue * 100 if revenue > 0 else 0
                    q = 0
                    if roe > 15:
                        q += 35
                    elif roe > 10:
                        q += 20
                    if debt_ratio < 100:
                        q += 35
                    elif debt_ratio < 200:
                        q += 15
                    if opm > 10:
                        q += 30
                    elif opm > 5:
                        q += 15
                    quality_scores[ticker_dir.name] = min(q, 100)
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                    continue
        if not quality_scores:
            factor_path = RESULTS / 'factor_scores.json'
            if factor_path.exists():
                try:
                    factors = json.load(open(factor_path))
                    for t, data in factors.items():
                        if isinstance(data, dict):
                            quality_scores[t] = data.get('quality_score', data.get('score', 50))
                except Exception as _e:
                    logger.warning(f'  suppressed: {_e}', exc_info=True)
        candidates = []
        for f in sorted(PRICES_DIR.glob('*.csv'))[:300]:
            ticker = f.stem
            if ticker in ETF_SET:
                continue
            try:
                df = pd.read_csv(f)
                _sort_col = 'date' if 'date' in df.columns else df.columns[0]
                df = df.sort_values(_sort_col)
                if len(df) < 252:
                    continue
                if 'High' not in df.columns:
                    df['High'] = df['Close']
                if 'Low' not in df.columns:
                    df['Low'] = df['Close']
                today_ret = float(df['Close'].iloc[-1] / df['Close'].iloc[-2] - 1) * 100
                if today_ret > min_drop_pct:
                    continue
                high_52w = float(df['High'].tail(252).max())
                current = float(df['Close'].iloc[-1])
                from_high = (current / high_52w - 1) * 100
                quality = quality_scores.get(ticker, 50)
                beta = self.compute_stock_beta(ticker)
                vol_20d = df['Volume'].tail(20).mean()
                vol_today = df['Volume'].iloc[-1]
                vol_spike = vol_today / (vol_20d + 1) if vol_20d > 0 else 1
                dip_score = 0
                if quality >= min_quality:
                    dip_score += 30
                if from_high <= -30:
                    dip_score += 25
                if beta > 1.2:
                    dip_score += 20
                if vol_spike > 2.0:
                    dip_score += 15
                if today_ret <= -7:
                    dip_score += 10
                if dip_score >= 50:
                    candidates.append({'ticker': ticker, 'today_ret': round(today_ret, 2), 'from_52w_high': round(from_high, 1), 'quality': round(quality, 1), 'beta': beta, 'vol_spike': round(vol_spike, 1), 'dip_score': dip_score, 'last_close': current, 'strategy': 'L3_SWING' if beta > 1.0 else 'L4_STRATEGIC', 'tp_target': round(current * 1.1, 0), 'sl_target': round(current * 0.95, 0)})
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        candidates.sort(key=lambda x: x['dip_score'], reverse=True)
        return candidates[:15]

class QualityGrowthDiscount:
    """중장기 우상향 종목 저가매수 스크리너.

    워런 버핏식: "위대한 기업을 합리적 가격에 산다"
    Quality(40%) + Growth(30%) + Discount(30%) 합산 점수.
    """

    def _load_dart_fundamentals(self) -> Dict:
        """DART CSV에서 직접 재무 데이터 파싱.

        data/dart/{ticker}/financial_summary.csv 구조:
        year,report,account,current,previous,fs_div,sj_div
        """
        fundamentals = {}
        dart_dir = DATA / 'dart'
        if not dart_dir.exists():
            return fundamentals
        for ticker_dir in sorted(dart_dir.iterdir()):
            if not ticker_dir.is_dir():
                continue
            ticker = ticker_dir.name
            fin_path = ticker_dir / 'financial_summary.csv'
            if not fin_path.exists():
                continue
            try:
                df = pd.read_csv(fin_path)
                if df.empty:
                    continue
                annual = df[df['report'] == '연간'] if 'report' in df.columns else df
                if annual.empty:
                    continue
                latest_year = annual['year'].max()
                latest = annual[annual['year'] == latest_year]
                fund = {}
                eq = latest[latest['account'].str.contains('자본총계', na=False)]
                equity = float(eq['current'].iloc[0]) if len(eq) > 0 else 0
                debt = latest[latest['account'].str.contains('부채총계', na=False)]
                total_debt = float(debt['current'].iloc[0]) if len(debt) > 0 else 0
                asset = latest[latest['account'].str.contains('자산총계', na=False)]
                total_assets = float(asset['current'].iloc[0]) if len(asset) > 0 else 0
                ni = latest[latest['account'].str.contains('당기순이익', na=False)]
                net_income = float(ni['current'].iloc[0]) if len(ni) > 0 else 0
                op = latest[latest['account'].str.contains('영업이익', na=False)]
                op_income = float(op['current'].iloc[0]) if len(op) > 0 else 0
                rev = latest[latest['account'].str.contains('매출액', na=False)]
                revenue = float(rev['current'].iloc[0]) if len(rev) > 0 else 0
                roe = net_income / equity * 100 if equity > 0 else 0
                debt_ratio = total_debt / equity * 100 if equity > 0 else 999
                operating_margin = op_income / revenue * 100 if revenue > 0 else 0
                fund['roe'] = round(roe, 2)
                fund['debt_ratio'] = round(debt_ratio, 2)
                fund['operating_margin'] = round(operating_margin, 2)
                fund['net_income'] = net_income
                fund['revenue'] = revenue
                fund['equity'] = equity
                fundamentals[ticker] = fund
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as _e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {_e}')
                continue
        logger.info(f'DART 재무 로드: {len(fundamentals)}종목')
        return fundamentals

    def screen(self, top_n: int=20) -> List[Dict]:
        """Quality+Growth+Discount 종합 스크리닝."""
        PRICES = DATA / 'raw' / 'korean_stocks' / 'prices'
        ETF_DIR = DATA / 'raw' / 'korean_stocks' / 'etf_prices'
        ETF_SET = set((f.stem for f in ETF_DIR.glob('*.csv')))
        fundamentals = self._load_dart_fundamentals()
        if not fundamentals:
            for fpath in [RESULTS / 'factor_scores.json', RESULTS / 'l4_screener_result.json', DATA / 'processed' / 'dart_fundamentals.json']:
                if fpath.exists():
                    try:
                        fundamentals = json.load(open(fpath))
                        break
                    except Exception as _e:
                        logger.warning(f'  suppressed: {_e}', exc_info=True)
        candidates = []
        for f in sorted(PRICES.glob('*.csv'))[:300]:
            ticker = f.stem
            if ticker in ETF_SET:
                continue
            try:
                df = pd.read_csv(f)
                _sort_col = 'date' if 'date' in df.columns else df.columns[0]
                df = df.sort_values(_sort_col)
                if len(df) < 252:
                    continue
                if 'High' not in df.columns:
                    df['High'] = df['Close']
                if 'Low' not in df.columns:
                    df['Low'] = df['Close']
                close = df['Close'].values
                current = float(close[-1])
                quality = 50
                fund = fundamentals.get(ticker, {})
                if isinstance(fund, dict):
                    roe = fund.get('roe', fund.get('ROE', 0))
                    debt = fund.get('debt_ratio', fund.get('부채비율', 100))
                    opm = fund.get('operating_margin', fund.get('영업이익률', 0))
                    q_score = 0
                    if isinstance(roe, (int, float)) and roe > 15:
                        q_score += 35
                    elif isinstance(roe, (int, float)) and roe > 10:
                        q_score += 20
                    if isinstance(debt, (int, float)) and debt < 100:
                        q_score += 35
                    elif isinstance(debt, (int, float)) and debt < 200:
                        q_score += 15
                    if isinstance(opm, (int, float)) and opm > 10:
                        q_score += 30
                    elif isinstance(opm, (int, float)) and opm > 5:
                        q_score += 15
                    quality = min(q_score, 100)
                growth = 50
                if len(df) >= 504:
                    price_2y = float(close[-504]) if len(close) >= 504 else float(close[0])
                    price_1y = float(close[-252])
                    cagr = ((current / price_2y) ** 0.5 - 1) * 100 if price_2y > 0 else 0
                    momentum_1y = (current / price_1y - 1) * 100 if price_1y > 0 else 0
                    g_score = 0
                    if cagr > 20:
                        g_score += 40
                    elif cagr > 10:
                        g_score += 25
                    elif cagr > 0:
                        g_score += 10
                    if momentum_1y > 15:
                        g_score += 30
                    elif momentum_1y > 0:
                        g_score += 15
                    ma20 = float(np.mean(close[-20:]))
                    ma60 = float(np.mean(close[-60:])) if len(close) >= 60 else ma20
                    if ma20 > ma60:
                        g_score += 30
                    elif ma20 > ma60 * 0.95:
                        g_score += 15
                    growth = min(g_score, 100)
                discount = 50
                high_52w = float(df['High'].tail(252).max())
                low_52w = float(df['Low'].tail(252).min())
                from_high = (current / high_52w - 1) * 100
                from_low = (current / low_52w - 1) * 100
                per = fund.get('per', fund.get('PER', 0)) if isinstance(fund, dict) else 0
                per_avg = fund.get('per_3y_avg', 0) if isinstance(fund, dict) else 0
                d_score = 0
                if from_high <= -30:
                    d_score += 35
                elif from_high <= -20:
                    d_score += 20
                if from_low <= 20:
                    d_score += 30
                elif from_low <= 40:
                    d_score += 15
                if isinstance(per, (int, float)) and isinstance(per_avg, (int, float)):
                    if 0 < per < per_avg * 0.8 and per_avg > 0:
                        d_score += 35
                    elif 0 < per < per_avg and per_avg > 0:
                        d_score += 15
                discount = min(d_score, 100)
                total = quality * 0.4 + growth * 0.3 + discount * 0.3
                avg_vol = float(df['Volume'].tail(20).mean())
                _total_min = _cfg_get('defensive.qgd_score_min', 50)
                _qgd_vol_min = _cfg_get('defensive.qgd_vol_min', 30000)
                if total >= _total_min and avg_vol > _qgd_vol_min:
                    candidates.append({'ticker': ticker, 'total_score': round(total, 1), 'quality': round(quality, 1), 'growth': round(growth, 1), 'discount': round(discount, 1), 'from_52w_high': round(from_high, 1), 'from_52w_low': round(from_low, 1), 'last_close': current, 'avg_volume': int(avg_vol), 'strategy': 'L4_STRATEGIC', 'buy_plan': f'1차 30%({current:,.0f}), 2차 40%({current * 0.95:,.0f}), 3차 30%({current * 0.9:,.0f})'})
            except Exception as _e:
                logger.warning(f'  suppressed: {_e}', exc_info=True)
        candidates.sort(key=lambda x: x['total_score'], reverse=True)
        return candidates[:top_n]

class SectorRotationDetector:
    """하락장 섹터 순서 감지 → 바닥 시그널.

    하락 순서: 고β섹터(IT/반도체) → 중β(금융/산업) → 저β(필수소비/유틸)
    반등 순서: 역순 → 고β가 먼저, 가장 크게 반등

    바닥 신호: "모든 섹터가 하락한 후, 저β 섹터까지 빠질 때"
    """
    SECTOR_ETFS = {'high_beta': {'091160': '코덱스 반도체', '091170': '코덱스 은행', '091180': '코덱스 IT'}, 'mid_beta': {'091220': '코덱스 철강', '091230': '코덱스 건설'}, 'low_beta': {'117680': '코덱스 F200', '069500': '코덱스 200'}}

    def detect_rotation_phase(self) -> Dict:
        """현재 섹터 로테이션 단계 감지."""
        ETF_DIR = DATA / 'raw' / 'korean_stocks' / 'etf_prices'
        sector_performance = {}
        for category, etfs in self.SECTOR_ETFS.items():
            rets = []
            for code, name in etfs.items():
                path = ETF_DIR / f'{code}.csv'
                if path.exists():
                    try:
                        df = pd.read_csv(path)
                        sort_col = df.columns[0]
                        df = df.sort_values(sort_col)
                        if len(df) >= 20:
                            close_col = 'Close' if 'Close' in df.columns else 'close'
                            close = df[close_col].values
                            ret_5d = float(close[-1] / close[-5] - 1) * 100
                            ret_20d = float(close[-1] / close[-20] - 1) * 100
                            rets.append({'5d': ret_5d, '20d': ret_20d})
                    except Exception as _e:
                        logger.warning(f'  suppressed: {_e}', exc_info=True)
            if rets:
                sector_performance[category] = {'avg_5d': round(np.mean([r['5d'] for r in rets]), 2), 'avg_20d': round(np.mean([r['20d'] for r in rets]), 2)}
        if len(sector_performance) < 2:
            return {'phase': 'unknown', 'signal': 'no_data'}
        high = sector_performance.get('high_beta', {})
        mid = sector_performance.get('mid_beta', {})
        low = sector_performance.get('low_beta', {})
        h20 = high.get('avg_20d', 0)
        m20 = mid.get('avg_20d', 0)
        l20 = low.get('avg_20d', 0)
        if h20 < -10 and m20 < -5 and (l20 < -3):
            phase = 'BOTTOM_FORMING'
            signal = '🔴 전 섹터 하락 → 바닥 형성 중 → 고β 저가매수 준비'
            action = 'PREPARE_HIGH_BETA_BUY'
        elif h20 < -10 and m20 < -5 and (l20 >= -3):
            phase = 'LATE_DECLINE'
            signal = '🟡 고β/중β 하락, 저β 방어 중 → 아직 바닥 아님'
            action = 'WAIT'
        elif h20 < -5 and m20 >= -3:
            phase = 'EARLY_DECLINE'
            signal = '🟡 고β만 하락 → 하락 초기'
            action = 'DEFENSIVE_ONLY'
        elif h20 > 5 and m20 > 0:
            phase = 'RECOVERY'
            signal = '🟢 고β 반등 중 → 상승 초기'
            action = 'HIGH_BETA_BUY'
        elif h20 > 0 and l20 > 0:
            phase = 'BULL'
            signal = '🟢 전 섹터 상승'
            action = 'MOMENTUM'
        else:
            phase = 'MIXED'
            signal = '🟡 혼조'
            action = 'NEUTRAL'
        return {'phase': phase, 'signal': signal, 'action': action, 'sector_performance': sector_performance, 'recommendation': self._get_recommendation(phase)}

    def _get_recommendation(self, phase: str) -> str:
        recommendations = {'BOTTOM_FORMING': '고β 섹터(반도체/IT) 분할 매수 시작 (30%씩 3회)', 'LATE_DECLINE': '현금 보유, 저β 방어주 유지', 'EARLY_DECLINE': '리스크 축소, 고β 비중 줄이기', 'RECOVERY': '고β 섹터 추가 매수, 저β → 고β 전환', 'BULL': '모멘텀 전략 유지', 'MIXED': '관망, 방향 확인 후 행동'}
        return recommendations.get(phase, '관망')
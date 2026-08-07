"""
Project Meridian — Alpha-Ranked Hysteresis Rebalance Engine
============================================================

보유 + 미보유 종목을 동일 기준으로 스코어링하고,
Hysteresis Band 기반으로 교체 대상을 결정합니다.

핵심 원리:
  - 편입 기준(entry)과 편출 기준(exit)을 다르게 설정 (비대칭 band)
  - 기존 보유 종목에 거래비용 절약분 보너스를 부여
  - 교체는 alpha gap > cost_hurdle일 때만 실행
  - 모든 임계값은 유니버스 분포의 percentile로 동적 계산

Usage:
    from src.allocation.rebalance_engine import RebalanceEngine
    engine = RebalanceEngine()
    trades = engine.rebalance('S3', positions, signals, market_data)
"""
import json
import logging
import math
from datetime import date, datetime, timedelta
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
try:
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
except ImportError as e:
    cfg = None

def _cfg_get(key: str, default: Any=None) -> Any:
    """DynamicConfig에서 값 로드 (cfg 없으면 default)."""
    if cfg:
        return cfg.get(key, default)
    return default

class RebalanceEngine:
    """Alpha-Ranked Hysteresis Band Rebalancing Engine.

    S3/S4 포트폴리오의 자동 리밸런싱을 수행합니다.
    모든 파라미터는 DynamicConfig에서 동적으로 로드됩니다.
    """

    def __init__(self):
        self._price_cache: Dict[str, float] = {}
        self._returns_cache: Dict[str, List[float]] = {}
        self._last_rebalance: Dict[str, str] = {}
        self._load_rebalance_state()

    def _load_rebalance_state(self):
        """리밸런싱 상태 파일 로드."""
        try:
            state_path = _PROJECT_ROOT / 'results' / 'rebalance_state.json'
            if state_path.exists():
                data = json.loads(state_path.read_text())
                self._last_rebalance = data.get('last_rebalance', {})
        except Exception as _e0:
            logger.critical(f'  [rebalance_engine] 리밸런스 엔진 초기화 로드: {_e0}', exc_info=True)

    def _save_rebalance_state(self):
        """리밸런싱 상태 저장."""
        try:
            state_path = _PROJECT_ROOT / 'results' / 'rebalance_state.json'
            state_path.parent.mkdir(parents=True, exist_ok=True)
            data = {'last_rebalance': self._last_rebalance, 'updated_at': datetime.now().isoformat()}
            atomic_write_json(state_path, data, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.critical(f'  리밸런싱 상태 저장 실패: {e}', exc_info=True)

    def rebalance(self, stream_id: str, current_positions: Dict[str, Dict], new_signals: List[Dict], market_data: Dict=None, budget: float=0.0) -> Dict:
        """스트림 리밸런싱 실행.

        Args:
            stream_id: 'S3' or 'S4'
            current_positions: {pos_key: {ticker, name, amount, entry_date, ...}}
            new_signals: [{ticker, name, confidence, strategy, ...}]
            market_data: 시장 데이터 (signal_cache 등)

        Returns:
            {
                'sells': [{ticker, name, reason, amount, ...}],
                'buys': [{ticker, name, amount, confidence, ...}],
                'skipped': str,  # 스킵 사유 (있으면)
                'scores': {ticker: score},
                'replacements': int,
            }
        """
        sid = stream_id.lower()
        result = {'sells': [], 'buys': [], 'skipped': None, 'scores': {}, 'replacements': 0}
        enabled = _cfg_get(f'{sid}.rebalance.enabled', False)
        if not enabled:
            result['skipped'] = 'rebalance disabled'
            return result
        freq_days = _cfg_get(f'{sid}.rebalance.frequency_days', 5)
        today_str = date.today().isoformat()
        last = self._last_rebalance.get(stream_id, '')
        if last:
            try:
                last_date = date.fromisoformat(last)
                days_since = (date.today() - last_date).days
                if days_since < freq_days:
                    result['skipped'] = f'주기 미도래 ({days_since}일/{freq_days}일)'
                    return result
            except Exception as _e1:
                logger.critical(f'  [rebalance_engine] 리밸런스 조건 체크: {_e1}', exc_info=True)
        held_tickers = {}
        for pk, pv in current_positions.items():
            ticker = pk.split(':')[1] if ':' in pk else pv.get('ticker', '')
            if not ticker:
                continue
            held_tickers[ticker] = {'pos_key': pk, 'ticker': ticker, 'name': pv.get('name', ticker), 'amount': pv.get('amount', 0), 'market_value': pv.get('market_value', 0), 'entry_date': pv.get('entry_date', ''), 'confidence': pv.get('up_prob', pv.get('confidence', 0.5)), 'strategy': pv.get('strategy', ''), 'account': pv.get('account', ''), 'is_held': True}
        new_candidates = {}
        for sig in new_signals:
            ticker = sig.get('ticker', '')
            if not ticker or ticker in held_tickers:
                continue
            if ticker in new_candidates:
                if sig.get('confidence', 0) <= new_candidates[ticker]['confidence']:
                    continue
            new_candidates[ticker] = {'ticker': ticker, 'name': sig.get('name', ticker), 'confidence': sig.get('confidence', 0.5), 'strategy': sig.get('strategy', ''), 'is_held': False}
        all_candidates = {}
        all_candidates.update(held_tickers)
        all_candidates.update(new_candidates)
        if not all_candidates:
            result['skipped'] = 'no candidates'
            return result
        cost_hurdle = _cfg_get(f'{sid}.rebalance.cost_hurdle_pct', 0.015)
        scores = {}
        top_50_tickers = set()
        tgat_score = 0.0
        try:
            mcap_cache = _PROJECT_ROOT / 'data' / 'market_cap_cache.json'
            alpha_path = _PROJECT_ROOT / 'data' / 'alpha_signal.json'
            if mcap_cache.exists():
                mcaps = json.loads(mcap_cache.read_text())
                sorted_mcaps = sorted([(k, float(v.get('market_cap', v) if isinstance(v, dict) else v)) for k, v in mcaps.items() if v], key=lambda x: x[1], reverse=True)
                top_50_tickers = {k for k, _ in sorted_mcaps[:50]}
            if alpha_path.exists():
                alpha_data = json.loads(alpha_path.read_text())
                tgat_score = float(alpha_data.get('S2_signal', {}).get('pysr_macro_feature_value', 0.0))
        except Exception as e:
            logger.critical(f'  Alpha Factory/MCAP 데이터 로드 실패: {e}', exc_info=True)
        for ticker, info in all_candidates.items():
            score = self._compute_alpha_score(ticker=ticker, stream_id=stream_id, confidence=info.get('confidence', 0.5), is_held=info.get('is_held', False), cost_hurdle=cost_hurdle, is_large_cap=ticker in top_50_tickers, tgat_score=tgat_score)
            scores[ticker] = score
            info['alpha_score'] = score
        result['scores'] = {k: round(v, 4) for k, v in scores.items()}
        if not scores:
            result['skipped'] = 'scoring failed'
            return result
        score_values = sorted(scores.values())
        n = len(score_values)
        entry_pctl = _cfg_get(f'{sid}.rebalance.entry_percentile', 80)
        exit_pctl = _cfg_get(f'{sid}.rebalance.exit_percentile', 40)
        entry_threshold = np.percentile(score_values, entry_pctl) if n >= 3 else 0.5
        exit_threshold = np.percentile(score_values, exit_pctl) if n >= 3 else 0.3
        logger.info(f'  📊 {stream_id} 리밸런싱: 유니버스 {n}종목, entry>{entry_threshold:.3f} (P{entry_pctl}), exit<{exit_threshold:.3f} (P{exit_pctl})')
        min_hold = _cfg_get(f'{sid}.rebalance.min_holding_days', 5)
        exit_candidates = []
        for ticker, info in held_tickers.items():
            if stream_id == 'S3' and info.get('strategy', '').startswith('qvm'):
                logger.debug(f'    {ticker} QVM 장기투자 종목: 리밸런싱 편출 면제')
                continue
            score = scores.get(ticker, 0)
            if score >= exit_threshold:
                continue
            entry_date_str = info.get('entry_date', '')
            if entry_date_str and min_hold > 0:
                try:
                    entry_dt = date.fromisoformat(entry_date_str[:10])
                    holding_days = (date.today() - entry_dt).days
                    if holding_days < min_hold:
                        logger.debug(f'    {ticker} 최소 보유 미달 ({holding_days}일 < {min_hold}일)')
                        continue
                except Exception as _e2:
                    logger.critical(f'  [rebalance_engine] 개별 종목 매핑: {_e2}', exc_info=True)
            tax_aware = _cfg_get(f'{sid}.rebalance.tax_aware', False)
            if tax_aware and info.get('market_value', 0) > info.get('amount', 0):
                stricter_exit = np.percentile(score_values, max(exit_pctl - 15, 5)) if n >= 3 else 0.2
                if score >= stricter_exit:
                    logger.debug(f'    {ticker} 절세보호: 수익 중이므로 편출 억제 (score={score:.3f} >= {stricter_exit:.3f})')
                    continue
            exit_candidates.append({'ticker': ticker, 'name': info.get('name', ticker), 'alpha_score': score, 'amount': info.get('amount', 0), 'market_value': info.get('market_value', 0), 'pos_key': info.get('pos_key', f'{stream_id}:{ticker}'), 'account': info.get('account', '')})
        exit_candidates.sort(key=lambda x: x['alpha_score'])
        entry_candidates = []
        for ticker, info in new_candidates.items():
            score = scores.get(ticker, 0)
            if score <= entry_threshold:
                continue
            entry_candidates.append({'ticker': ticker, 'name': info.get('name', ticker), 'alpha_score': score, 'confidence': info.get('confidence', 0.5), 'strategy': info.get('strategy', '')})
        entry_candidates.sort(key=lambda x: -x['alpha_score'])
        max_replace = _cfg_get(f'{sid}.rebalance.max_replacements_per_cycle', 2)
        max_turnover = _cfg_get(f'{sid}.rebalance.max_turnover_pct', 0.4)
        total_mv = sum((h.get('market_value', h.get('amount', 0)) for h in held_tickers.values()))
        max_turnover_amount = total_mv * max_turnover if total_mv > 0 else 0
        replacements = 0
        turnover_amount = 0
        n_pairs = min(len(exit_candidates), len(entry_candidates), max_replace)
        for i in range(n_pairs):
            sell = exit_candidates[i]
            buy = entry_candidates[i]
            alpha_gap = buy['alpha_score'] - sell['alpha_score']
            if alpha_gap < cost_hurdle:
                logger.info(f'    ⏸ 교체 중단: alpha gap {alpha_gap:.3f} < hurdle {cost_hurdle:.3f} ({sell['name']} → {buy['name']})')
                break
            sell_amount = sell.get('market_value', sell.get('amount', 0))
            if turnover_amount + sell_amount > max_turnover_amount > 0:
                logger.info(f'    ⏸ 턴오버 한도 도달: ₩{turnover_amount + sell_amount:,.0f} > ₩{max_turnover_amount:,.0f}')
                break
            result['sells'].append({'ticker': sell['ticker'], 'name': sell['name'], 'pos_key': sell['pos_key'], 'stream_id': stream_id, 'alpha_score': round(sell['alpha_score'], 4), 'amount': sell_amount, 'reason': f'리밸런싱: score={sell['alpha_score']:.3f} < exit P{exit_pctl}={exit_threshold:.3f}, 교체→{buy['name']} (gap={alpha_gap:.3f})', 'sell_type': 'rebalance', 'account': sell.get('account', '')})
            result['buys'].append({'ticker': buy['ticker'], 'name': buy['name'], 'stream_id': stream_id, 'direction': 'long', 'alpha_score': round(buy['alpha_score'], 4), 'amount_krw': int(sell_amount), 'confidence': buy.get('confidence', 0.5), 'strategy': buy.get('strategy', 'rebalance'), 'reason': f'리밸런싱: score={buy['alpha_score']:.3f} > entry P{entry_pctl}={entry_threshold:.3f}, 교체←{sell['name']} (gap={alpha_gap:.3f})'})
            turnover_amount += sell_amount
            replacements += 1
            logger.info(f'    🔄 {stream_id} 교체 #{replacements}: {sell['name']} (score={sell['alpha_score']:.3f}) → {buy['name']} (score={buy['alpha_score']:.3f}) gap={alpha_gap:.3f} ₩{sell_amount:,.0f}')
        result['replacements'] = replacements
        max_positions = _cfg_get(f'{sid}.rebalance.max_positions', 5)
        current_count = max(0, len(held_tickers) - replacements)
        if current_count < max_positions and entry_candidates:
            remaining_entry = entry_candidates[replacements:]
            if budget <= 0:
                budget = self._get_stream_budget(stream_id)
            slot_budget = budget / max_positions if max_positions > 0 else 0
            for candidate in remaining_entry:
                if current_count >= max_positions:
                    break
                if slot_budget <= _cfg_get('execution.min_order_value', 100000):
                    break
                result['buys'].append({'ticker': candidate['ticker'], 'name': candidate['name'], 'stream_id': stream_id, 'direction': 'long', 'alpha_score': round(candidate['alpha_score'], 4), 'amount_krw': int(slot_budget), 'confidence': candidate.get('confidence', 0.5), 'strategy': candidate.get('strategy', 'rebalance_fill'), 'reason': f'빈 슬롯 편입: score={candidate['alpha_score']:.3f} > entry P{entry_pctl}={entry_threshold:.3f}'})
                current_count += 1
                logger.info(f'    ➕ {stream_id} 빈 슬롯 편입: {candidate['name']} (score={candidate['alpha_score']:.3f})')
        if replacements > 0 or result['buys']:
            self._last_rebalance[stream_id] = today_str
            self._save_rebalance_state()
        logger.info(f'  📊 {stream_id} 리밸런싱 결과: 매도 {len(result['sells'])}건, 매수 {len(result['buys'])}건, 교체 {replacements}건')
        return result

    def _compute_alpha_score(self, ticker: str, stream_id: str, confidence: float=0.5, is_held: bool=False, cost_hurdle: float=0.015, is_large_cap: bool=False, tgat_score: float=0.0) -> float:
        """보유/미보유 동일 기준 알파 스코어 계산.

        4개 팩터 가중 합산:
          1. 모멘텀 (장기 + 단기 혼합)
          2. 퀄리티 (수익률 안정성)
          3. 시그널 confidence
          4. 저변동성

        모든 가중치와 lookback은 DynamicConfig에서 로드.
        """
        w_mom = _cfg_get('rebalance.score.momentum_weight', 0.4)
        w_qual = _cfg_get('rebalance.score.quality_weight', 0.25)
        w_conf = _cfg_get('rebalance.score.confidence_weight', 0.25)
        w_vol = _cfg_get('rebalance.score.volatility_weight', 0.1)
        mom_score = self._compute_momentum_score(ticker)
        qual_score = self._compute_quality_score(ticker)
        conf_score = (confidence - 0.5) * 2
        vol_score = self._compute_volatility_score(ticker)
        total_weight = w_mom + w_qual + w_conf + w_vol
        if total_weight <= 0:
            total_weight = 1.0
        raw_score = (w_mom * mom_score + w_qual * qual_score + w_conf * conf_score + w_vol * vol_score) / total_weight
        if is_large_cap and tgat_score != 0:
            tgat_weight = _cfg_get('rebalance.score.tgat_weight', 0.15)
            macro_boost = max(-1.0, min(1.0, tgat_score)) * tgat_weight
            raw_score += macro_boost
        if is_held:
            bonus_ratio = _cfg_get('rebalance.score.held_bonus_ratio', 0.5)
            raw_score += cost_hurdle * bonus_ratio
        return raw_score

    def _compute_momentum_score(self, ticker: str) -> float:
        """모멘텀 스코어: 장기/단기 혼합.

        Returns: -1.0 ~ +1.0
        """
        returns = self._get_returns(ticker)
        if not returns or len(returns) < 5:
            return 0.0
        long_lookback = _cfg_get('rebalance.score.momentum_lookback_days', 60)
        short_lookback = _cfg_get('rebalance.score.momentum_short_lookback', 20)
        long_ratio = _cfg_get('rebalance.score.momentum_long_ratio', 0.6)
        long_returns = returns[-long_lookback:]
        if long_returns:
            cum_long = 1.0
            for r in long_returns:
                cum_long *= 1 + r
            long_mom = cum_long - 1
        else:
            long_mom = 0.0
        short_returns = returns[-short_lookback:]
        if short_returns:
            cum_short = 1.0
            for r in short_returns:
                cum_short *= 1 + r
            short_mom = cum_short - 1
        else:
            short_mom = 0.0
        blended = long_ratio * long_mom + (1 - long_ratio) * short_mom
        if returns:
            avg_abs_return = np.mean(np.abs(returns[-long_lookback:]))
            scale = max(avg_abs_return, 0.01)
        else:
            scale = 0.1
        normalized = max(-1.0, min(1.0, blended / scale))
        return normalized

    def _compute_quality_score(self, ticker: str) -> float:
        """퀄리티 스코어: 수익률 안정성 (Sharpe 근사).

        Returns: -1.0 ~ +1.0
        """
        returns = self._get_returns(ticker)
        if not returns or len(returns) < 10:
            return 0.0
        lookback = _cfg_get('rebalance.score.momentum_lookback_days', 60)
        recent = returns[-lookback:]
        mean_r = np.mean(recent)
        std_r = np.std(recent)
        if std_r <= 0:
            return 0.5 if mean_r > 0 else -0.5
        ir = mean_r / std_r * math.sqrt(252)
        normalized = max(-1.0, min(1.0, ir / 2.0))
        return normalized

    def _compute_volatility_score(self, ticker: str) -> float:
        """저변동성 스코어: 변동성 낮을수록 높은 점수.

        Returns: -1.0 ~ +1.0
        """
        returns = self._get_returns(ticker)
        if not returns or len(returns) < 5:
            return 0.0
        lookback = _cfg_get('rebalance.score.volatility_lookback_days', 20)
        recent = returns[-lookback:]
        vol = np.std(recent) * math.sqrt(252)
        avg_vol = _cfg_get('rebalance.score.avg_vol_baseline', 0.25)
        if len(self._returns_cache) > 3:
            all_vols = []
            for _, rets in self._returns_cache.items():
                if rets and len(rets) >= lookback:
                    all_vols.append(np.std(rets[-lookback:]) * math.sqrt(252))
            if all_vols:
                avg_vol = np.mean(all_vols)
        if avg_vol <= 0:
            return 0.0
        normalized = max(-1.0, min(1.0, (avg_vol - vol) / avg_vol))
        return normalized

    def _get_returns(self, ticker: str) -> List[float]:
        """종목 일별 수익률 로드 (캐시 사용)."""
        if ticker in self._returns_cache:
            return self._returns_cache[ticker]
        returns = self._load_returns_from_data(ticker)
        _maxsize = 200
        if len(self._returns_cache) >= _maxsize:
            oldest_key = next(iter(self._returns_cache))
            del self._returns_cache[oldest_key]
        self._returns_cache[ticker] = returns
        return returns

    def _load_returns_from_data(self, ticker: str) -> List[float]:
        """pykrx 또는 parquet에서 수익률 로드."""
        try:
            from pykrx import stock as pykrx
            lookback = _cfg_get('rebalance.score.momentum_lookback_days', 60)
            end = date.today().strftime('%Y%m%d')
            start = (date.today() - timedelta(days=lookback * 2)).strftime('%Y%m%d')
            df = pykrx.get_market_ohlcv_by_date(start, end, ticker)
            if df is not None and len(df) > 1:
                closes = df['종가'].values.astype(float)
                returns = []
                for i in range(1, len(closes)):
                    if closes[i - 1] > 0:
                        returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
                return returns
        except Exception as _e3:
            logger.critical(f'  [rebalance_engine] 상태 저장 1: {_e3}', exc_info=True)
        try:
            import pandas as pd
            for pattern in [f'kr_{ticker}.parquet', f'{ticker}.parquet']:
                pq = _PROJECT_ROOT / 'data' / 'historical_10y' / pattern
                if pq.exists():
                    df = pd.read_parquet(pq)
                    if 'close' in df.columns and len(df) > 1:
                        closes = df['close'].values.astype(float)
                        returns = []
                        for i in range(1, len(closes)):
                            if closes[i - 1] > 0:
                                returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
                        return returns[-120:]
        except Exception as _e4:
            logger.critical(f'  [rebalance_engine] 상태 저장 2: {_e4}', exc_info=True)
        return []

    def _get_stream_budget(self, stream_id: str) -> float:
        """스트림 가용 예산 계산."""
        sid = stream_id.lower()
        total_budget = _cfg_get(f'{sid}.budget', _cfg_get(f'allocation.{sid}_budget', 0))
        try:
            sp_path = _PROJECT_ROOT / 'results' / 'shadow_portfolio.json'
            if sp_path.exists():
                sp = json.loads(sp_path.read_text())
                held = sum((pv.get('amount', 0) for pk, pv in sp.get('positions', {}).items() if pk.startswith(f'{stream_id}:')))
                return max(total_budget - held, 0)
        except Exception as _e5:
            logger.critical(f'  [rebalance_engine] 상태 저장 3: {_e5}', exc_info=True)
        return total_budget

    def _get_current_price(self, ticker: str) -> float:
        """종목 현재가."""
        if ticker in self._price_cache:
            return self._price_cache[ticker]
        try:
            from pykrx import stock as pykrx
            today = date.today().strftime('%Y%m%d')
            week_ago = (date.today() - timedelta(days=7)).strftime('%Y%m%d')
            df = pykrx.get_market_ohlcv_by_date(week_ago, today, ticker)
            if df is not None and len(df) > 0:
                price = float(df.iloc[-1]['종가'])
                if price > 0:
                    self._price_cache[ticker] = price
                    return price
        except Exception as _e6:
            logger.critical(f'  [rebalance_engine] 상태 저장 4: {_e6}', exc_info=True)
        return 0.0
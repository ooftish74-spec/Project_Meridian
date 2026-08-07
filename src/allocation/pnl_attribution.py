"""
PnL Attribution — 스트림별 성과 기여분석
============================================

Medallion A+ Upgrade.

각 스트림(S1~S4)이 전체 포트폴리오 수익에 기여한 비율을 분해:
  1. Holding PnL: 보유 자산의 시가 변동
  2. Trading PnL: 매매(진입/청산)에 의한 손익
  3. Cost PnL: 거래비용(수수료 + 슬리피지) 차감
  4. Risk PnL: 리스크 차감분 (kill switch, drawdown guard 등)

모든 파라미터 DynamicConfig 동적 로드. 하드코딩 0.

Usage:
    from src.allocation.pnl_attribution import PnLAttributor
    attr = PnLAttributor()
    report = attr.compute(stream_metrics, portfolio_value)
"""
import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

class PnLAttributor:
    """스트림별 PnL 기여분석.

    Brinson-Hood-Beebower 모델 변형:
      - Allocation Effect: 비중 배분에 의한 기여
      - Selection Effect: 종목 선택에 의한 기여
      - Interaction Effect: 배분×선택 교차 기여
    """

    @property
    def STREAMS(self):
        """★ SSOT: system.active_streams (Legacy Purge 2026-07-19)"""
        return list(cfg.get('system.active_streams', ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']))

    def compute(self, stream_metrics: Dict=None, portfolio_value: float=None, current_weights: Dict[str, float]=None) -> Dict:
        """PnL Attribution 계산.

        Args:
            stream_metrics: 스트림별 성과 {'S1': {'daily_returns': [...], ...}}
            portfolio_value: 포트폴리오 총 가치
            current_weights: 현재 스트림 비중

        Returns:
            기여분석 결과
        """
        if stream_metrics is None:
            stream_metrics = self._load_stream_metrics()
        if portfolio_value is None:
            portfolio_value = self._load_portfolio_value()
        if current_weights is None:
            current_weights = self._load_current_weights()
        result = {'timestamp': datetime.now().isoformat(), 'portfolio_value': portfolio_value, 'current_weights': current_weights, 'streams': {}, 'summary': {}}
        total_pnl = 0
        total_return = 0
        for sid in self.STREAMS:
            sm = stream_metrics.get(sid, {})
            weight = current_weights.get(sid, 0)
            attribution = self._attribute_stream(sid, sm, weight, portfolio_value)
            result['streams'][sid] = attribution
            total_pnl += attribution['total_pnl']
            total_return += attribution['weighted_return']
        result['summary'] = {'total_pnl': round(total_pnl, 0), 'total_return_pct': round(total_return * 100, 4), 'best_stream': max(result['streams'].items(), key=lambda x: x[1]['total_pnl'])[0] if result['streams'] else 'N/A', 'worst_stream': min(result['streams'].items(), key=lambda x: x[1]['total_pnl'])[0] if result['streams'] else 'N/A'}
        brinson = self._brinson_decomposition(stream_metrics, current_weights)
        result['brinson'] = brinson
        self._save_result(result)
        logger.info(f'  📊 PnL Attribution: total={total_return * 100:.2f}%, best={result['summary']['best_stream']}, worst={result['summary']['worst_stream']}')
        return result

    def _attribute_stream(self, sid: str, metrics: Dict, weight: float, portfolio_value: float) -> Dict:
        """개별 스트림의 PnL 기여 분해."""
        daily_returns = metrics.get('daily_returns', [])
        n = len(daily_returns)
        if n > 0:
            recent_window = cfg.get('allocation.attribution_window', 20)
            recent = daily_returns[-recent_window:]
            stream_return = sum(recent)
            daily_avg = sum(recent) / len(recent)
        else:
            stream_return = 0
            daily_avg = 0
        weighted_return = stream_return * weight
        holding_pnl = weighted_return * portfolio_value
        trade_count = metrics.get('total_trades', 0)
        tx_cost_rate = cfg.get('allocation.attribution_tx_cost', 0.0025)
        avg_trade_size = weight * portfolio_value / max(trade_count, 1)
        trading_cost = trade_count * avg_trade_size * tx_cost_rate
        risk_events = metrics.get('risk_cut_count', 0)
        risk_cut_impact = cfg.get('allocation.risk_cut_avg_impact', 0.005)
        risk_pnl = -risk_events * risk_cut_impact * weight * portfolio_value
        total_pnl = holding_pnl - trading_cost + risk_pnl
        sharpe = metrics.get('sharpe')
        if sharpe is not None:
            sharpe_contribution = weight * sharpe
        else:
            sharpe_contribution = 0
        if n >= 5:
            var = sum(((r - daily_avg) ** 2 for r in daily_returns[-20:])) / min(n, 20)
            vol = math.sqrt(var) * math.sqrt(cfg.get('common.annualization_factor', 252))
        else:
            vol = 0
        return {'weight': round(weight, 4), 'stream_return': round(stream_return, 6), 'weighted_return': round(weighted_return, 6), 'holding_pnl': round(holding_pnl, 0), 'trading_cost': round(trading_cost, 0), 'risk_pnl': round(risk_pnl, 0), 'total_pnl': round(total_pnl, 0), 'contribution_pct': 0, 'sharpe_contribution': round(sharpe_contribution, 4), 'volatility': round(vol, 4), 'trade_count': trade_count, 'n_days': n}

    def _brinson_decomposition(self, stream_metrics: Dict, weights: Dict[str, float]) -> Dict:
        """Brinson-Hood-Beebower 스타일 분해.

        Allocation Effect: (w_p - w_b) × (R_b - R_total)
        Selection Effect: w_b × (R_p - R_b)
        Interaction Effect: (w_p - w_b) × (R_p - R_b)
        """
        n_streams = len(self.STREAMS)
        bench_weight = 1.0 / n_streams
        returns = {}
        for sid in self.STREAMS:
            daily_r = stream_metrics.get(sid, {}).get('daily_returns', [])
            window = cfg.get('allocation.attribution_window', 20)
            if daily_r:
                recent = daily_r[-window:]
                returns[sid] = sum(recent)
            else:
                returns[sid] = 0
        bench_total = sum(returns.values()) / n_streams
        effects = {}
        total_alloc = 0
        total_select = 0
        total_interact = 0
        for sid in self.STREAMS:
            w_p = weights.get(sid, bench_weight)
            w_b = bench_weight
            r_p = returns.get(sid, 0)
            alloc = (w_p - w_b) * (r_p - bench_total)
            select = w_b * (r_p - bench_total)
            interact = (w_p - w_b) * (r_p - bench_total) - alloc
            effects[sid] = {'allocation_effect': round(alloc * 100, 4), 'selection_effect': round(select * 100, 4), 'interaction_effect': round(interact * 100, 4), 'total_effect': round((alloc + select + interact) * 100, 4)}
            total_alloc += alloc
            total_select += select
            total_interact += interact
        return {'stream_effects': effects, 'total_allocation': round(total_alloc * 100, 4), 'total_selection': round(total_select * 100, 4), 'total_interaction': round(total_interact * 100, 4)}

    def _load_stream_metrics(self) -> Dict:
        try:
            f = _RESULTS / 'stream_metrics.json'
            if f.exists():
                return json.loads(f.read_text())
        except Exception as _e0:
            logger.critical(f'  [pnl_attribution] PnL attribution 섹션 계산: {_e0}', exc_info=True)
        return {}

    def _load_portfolio_value(self) -> float:
        try:
            sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
            nav = sp.get('nav', 0) or sp.get('total_value', 0)
            if nav > 0:
                return float(nav)
        except Exception as _e1:
            logger.critical(f'  [pnl_attribution] PnL attribution 섹션 2: {_e1}', exc_info=True)
        return float(cfg.get('portfolio.initial_capital'))

    def _load_current_weights(self) -> Dict[str, float]:
        try:
            f = _RESULTS / 'portfolio_optimizer.json'
            if f.exists():
                data = json.loads(f.read_text())
                w = data.get('final_weights') or data.get('target_weights')
                if w:
                    return w
        except Exception as _e2:
            logger.critical(f'  [pnl_attribution] PnL attribution 섹션 3: {_e2}', exc_info=True)
        return {'S1': cfg.get('allocator.s1_base_weight', 0.08), 'S2': cfg.get('allocator.s2_base_weight', 0.3), 'S3': cfg.get('allocator.s3_base_weight', 0.25), 'S4': cfg.get('allocator.s4_base_weight', 0.3)}

    def _save_result(self, result: Dict) -> None:
        try:
            out = _RESULTS / 'pnl_attribution.json'
            atomic_write_json(out, result, indent=2, ensure_ascii=False, default=str)
        except Exception as _e3:
            logger.critical(f'  [pnl_attribution] PnL attribution 섹션 4: {_e3}', exc_info=True)
PnLAttributionEngine = PnLAttributor
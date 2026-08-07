"""
Transaction Cost Analysis (TCA) — 거래비용 분석
=================================================

Medallion Upgrade Phase 2-C-1.

기능:
  1. Implementation Shortfall: 의사결정가 vs 실제체결가 차이
  2. VWAP Slippage: VWAP 대비 체결 슬리피지
  3. Market Impact: 시장충격 비용 측정
  4. Timing Cost: 의사결정 지연 비용
  5. 거래비용 분해: 명시적(수수료) + 암묵적(슬리피지+충격)

모든 파라미터 DynamicConfig 동적 로드.
"""
import json
import logging
import math
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from typing import Dict, List, Optional
from config.dynamic_config import DynamicConfig
from src.utils.emergency_pager import send_emergency_page
logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class TCAAnalyzer:
    """Transaction Cost Analysis (TCA)."""

    def analyze_trade(self, trade: Dict) -> Dict:
        """개별 거래 TCA 분석.

        Args:
            trade: {
                'ticker': str,
                'action': 'buy'|'sell',
                'quantity': int,
                'decision_price': float,  # 의사결정 시점 가격
                'arrival_price': float,   # 주문 도달 시점 가격
                'fill_price': float,      # 실제 체결 가격
                'vwap': float,            # 해당 시간대 VWAP
                'close_price': float,     # 당일 종가
                'commission': float,      # 수수료
                'amount': float,          # 거래대금
            }

        Returns:
            TCA 지표 딕셔너리
        """
        decision = trade.get('decision_price', 0)
        arrival = trade.get('arrival_price', decision)
        fill = trade.get('fill_price', decision)
        vwap = trade.get('vwap', fill)
        close = trade.get('close_price', fill)
        commission = trade.get('commission', 0)
        tax = trade.get('tax', 0)
        amount = trade.get('amount', 0)
        is_buy = trade.get('action', 'buy').lower() == 'buy'
        sign = 1 if is_buy else -1
        if decision > 0:
            impl_shortfall_bps = round(sign * (fill - decision) / decision * 10000, 2)
        else:
            impl_shortfall_bps = 0
        if vwap > 0:
            vwap_slippage_bps = round(sign * (fill - vwap) / vwap * 10000, 2)
        else:
            vwap_slippage_bps = 0
        explicit_cost_bps = round((commission + tax) / amount * 10000, 2) if amount > 0 else 0
        if decision > 0:
            timing_cost_bps = round(sign * (arrival - decision) / decision * 10000, 2)
        else:
            timing_cost_bps = 0
        if arrival > 0:
            market_impact_bps = round(sign * (fill - arrival) / arrival * 10000, 2)
        else:
            market_impact_bps = 0
        total_cost_bps = round(impl_shortfall_bps + explicit_cost_bps, 2)
        if fill > 0:
            opportunity_bps = round(sign * (close - fill) / fill * 10000, 2)
        else:
            opportunity_bps = 0
        return {'ticker': trade.get('ticker', ''), 'action': trade.get('action', ''), 'implementation_shortfall_bps': impl_shortfall_bps, 'vwap_slippage_bps': vwap_slippage_bps, 'explicit_cost_bps': explicit_cost_bps, 'timing_cost_bps': timing_cost_bps, 'market_impact_bps': market_impact_bps, 'opportunity_cost_bps': opportunity_bps, 'total_cost_bps': total_cost_bps, 'total_cost_krw': round(amount * total_cost_bps / 10000), 'quality_grade': self._grade_execution(total_cost_bps), 'timestamp': datetime.now().isoformat()}

    def analyze_batch(self, trades: List[Dict]) -> Dict:
        """배치 거래 TCA 분석 (일별/기간별).

        Returns:
            집계 TCA 지표
        """
        if not trades:
            return {'n_trades': 0}
        results = [self.analyze_trade(t) for t in trades]
        total_is = [r['implementation_shortfall_bps'] for r in results]
        total_vwap = [r['vwap_slippage_bps'] for r in results]
        total_impact = [r['market_impact_bps'] for r in results]
        total_cost = [r['total_cost_bps'] for r in results]
        n = len(results)
        total_amount = sum((t.get('amount', 0) for t in trades))
        return {'n_trades': n, 'total_amount': total_amount, 'avg_is_bps': round(sum(total_is) / n, 2), 'avg_vwap_slippage_bps': round(sum(total_vwap) / n, 2), 'avg_market_impact_bps': round(sum(total_impact) / n, 2), 'avg_total_cost_bps': round(sum(total_cost) / n, 2), 'worst_trade': max(results, key=lambda x: x['total_cost_bps']), 'best_trade': min(results, key=lambda x: x['total_cost_bps']), 'total_cost_krw': sum((r['total_cost_krw'] for r in results)), 'grade_distribution': self._grade_distribution(results)}

    def benchmark_comparison(self, trades: List[Dict]) -> Dict:
        """벤치마크 대비 실행 품질 비교.

        DynamicConfig에서 목표 슬리피지를 읽어 비교.
        """
        target_is = cfg.get('execution.target_is_bps', 5.0)
        target_vwap = cfg.get('execution.target_vwap_bps', 3.0)
        batch = self.analyze_batch(trades)
        if batch['n_trades'] == 0:
            return batch
        avg_is = batch['avg_is_bps']
        avg_vwap = batch['avg_vwap_slippage_bps']
        return {**batch, 'target_is_bps': target_is, 'is_within_target': avg_is <= target_is, 'vwap_within_target': avg_vwap <= target_vwap, 'is_deviation': round(avg_is - target_is, 2), 'vwap_deviation': round(avg_vwap - target_vwap, 2)}

    def generate_feedback(self, trades: List[Dict]) -> Dict:
        """
        [Phase 86] TCA 피드백 생성 및 저장.
        실제 슬리피지와 목표(예상) 슬리피지의 비율(Slippage Ratio)을 계산하여
        내일의 SmartOrderRouter impact_coefficient를 보정하는 데 사용.
        """
        import json
        from pathlib import Path
        batch = self.benchmark_comparison(trades)
        if batch.get('n_trades', 0) == 0:
            return {}
        avg_actual_bps = batch.get('avg_is_bps', 0)
        target_bps = batch.get('target_is_bps', 5.0)
        if target_bps > 0:
            ratio = max(0.5, min(3.0, avg_actual_bps / target_bps))
        else:
            ratio = 1.0
        feedback = {'timestamp': datetime.now().isoformat(), 'avg_is_bps': avg_actual_bps, 'target_is_bps': target_bps, 'slippage_ratio': round(ratio, 3), 'recommended_impact_modifier': round(ratio, 3)}
        project_root = Path(__file__).resolve().parent.parent.parent
        results_dir = project_root / 'results'
        results_dir.mkdir(parents=True, exist_ok=True)
        feedback_path = results_dir / 'tca_feedback.json'
        try:
            from src.utils.file_ops import atomic_write_json

            atomic_write_json(feedback_path, feedback, indent=2)
            logger.info(f'  [TCA] 피드백 루프 파일 저장: ratio={ratio:.3f}, avg_is={avg_actual_bps}bps')
        except Exception as e:
            logger.critical(f'  [TCA] 피드백 저장 실패: {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at tca.py:227', exc_info=e)
        return feedback

    def enrich_post_market(self, fills: List[Dict], date_str: str=None) -> List[Dict]:
        """장 마감 후 실 VWAP/종가로 TCA 데이터 보강.

        Shadow 체결 기록에 실 시장 데이터를 추가하여
        VWAP Slippage, Opportunity Cost 등을 의미 있게 만듭니다.

        Args:
            fills: shadow_trades fills 리스트
            date_str: 날짜 (YYYYMMDD), None이면 오늘

        Returns:
            보강된 fills 리스트 (원본 수정 없이 복사본 반환)
        """
        if not fills:
            return []
        if date_str is None:
            date_str = datetime.now().strftime('%Y%m%d')
        tickers = list(set((f.get('ticker', '') for f in fills if f.get('ticker'))))
        vwap_map = {}
        close_map = {}
        try:
            from pykrx import stock as pykrx_stock
            for ticker in tickers:
                try:
                    df = pykrx_stock.get_market_ohlcv(date_str, date_str, ticker)
                    if not df.empty:
                        row = df.iloc[-1]
                        close_p = float(row.get('종가', 0))
                        volume = float(row.get('거래량', 0))
                        trdval = float(row.get('거래대금', 0))
                        vwap_p = trdval / volume if volume > 0 else close_p
                        vwap_map[ticker] = vwap_p
                        close_map[ticker] = close_p
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
        except ImportError as e:
            logger.critical('pykrx 미설치 — post-market 보강 스킵', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at tca.py:278', exc_info=e)
            return fills
        enriched = []
        for f in fills:
            ef = dict(f)
            ticker = ef.get('ticker', '')
            signal_p = ef.get('signal_price', 0)
            fill_p = ef.get('fill_price', 0)
            if ticker in vwap_map:
                ef['vwap'] = round(vwap_map[ticker], 2)
            else:
                ef['vwap'] = signal_p
            if ticker in close_map:
                ef['close_price'] = close_map[ticker]
            else:
                ef['close_price'] = fill_p
            timing_ratio = 0.2
            ef['decision_price'] = signal_p
            ef['arrival_price'] = signal_p + (fill_p - signal_p) * timing_ratio
            ef['enriched'] = True
            enriched.append(ef)
        logger.info(f'TCA post-market 보강: {len(enriched)}건 (VWAP {len(vwap_map)}/{len(tickers)} 종목)')
        return enriched

    def compute_and_save_summary(self, date_str: str=None) -> Dict:
        """당일 shadow_trades에서 TCA 요약을 계산하고 저장.

        Args:
            date_str: 날짜 (YYYY-MM-DD), None이면 오늘

        Returns:
            TCA 요약 딕셔너리
        """
        import json
        from pathlib import Path
        project_root = Path(__file__).resolve().parent.parent.parent
        results_dir = project_root / 'results'
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
        date_ym = date_str.replace('-', '')
        trades_file = results_dir / 'shadow_trades' / f'{date_str}.json'
        if not trades_file.exists():
            return {'n_trades': 0, 'error': 'no_shadow_trades'}
        try:
            raw = json.loads(trades_file.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return {'n_trades': 0, 'error': 'parse_error'}
        items = raw if isinstance(raw, list) else [raw]
        all_fills = []
        for batch in items:
            if isinstance(batch, dict) and 'fills' in batch:
                all_fills.extend(batch['fills'])
        if not all_fills:
            return {'n_trades': 0}
        enriched = self.enrich_post_market(all_fills, date_ym)
        tca_inputs = []
        for ef in enriched:
            sp = ef.get('signal_price', 0)
            fp = ef.get('fill_price', 0)
            qty = ef.get('quantity', 0)
            if sp > 0 and fp > 0 and (qty > 0):
                tca_inputs.append({'ticker': ef.get('ticker', ''), 'action': ef.get('action', 'buy'), 'quantity': qty, 'decision_price': ef.get('decision_price', sp), 'arrival_price': ef.get('arrival_price', sp), 'fill_price': fp, 'vwap': ef.get('vwap', sp), 'close_price': ef.get('close_price', fp), 'commission': ef.get('commission', 0), 'tax': ef.get('tax', 0), 'amount': fp * qty})
        if not tca_inputs:
            return {'n_trades': 0}
        summary = self.analyze_batch(tca_inputs)
        summary['date'] = date_str
        summary['enriched'] = True
        summary['n_enriched_vwap'] = sum((1 for ef in enriched if ef.get('vwap', 0) != ef.get('signal_price', 0)))
        summary['timestamp'] = datetime.now().isoformat()
        summary_path = results_dir / 'tca_summary.json'
        atomic_write_json(summary_path, summary, indent=2, ensure_ascii=False, default=str)
        logger.info(f'TCA 요약 저장: {summary_path} ({summary['n_trades']}건, avg_is={summary.get('avg_is_bps', 0):.1f}bps)')
        self._update_ticker_history(tca_inputs, date_str)
        return summary

    @staticmethod
    def _grade_execution(total_cost_bps: float) -> str:
        """실행 품질 등급."""
        if total_cost_bps <= 2:
            return 'A'
        elif total_cost_bps <= 5:
            return 'B'
        elif total_cost_bps <= 10:
            return 'C'
        elif total_cost_bps <= 20:
            return 'D'
        else:
            return 'F'

    @staticmethod
    def _grade_distribution(results: List[Dict]) -> Dict:
        """등급별 분포."""
        dist = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
        for r in results:
            grade = r.get('quality_grade', 'C')
            dist[grade] = dist.get(grade, 0) + 1
        return dist

    def get_market_impact_score(self, ticker: str) -> float:
        """
        특정 종목의 최근 누적 Market Impact Score (BPS) 반환.

        ★ 업그레이드: tca_ticker_history.json에서
           종목별 EWMA(Exponential Weighted Moving Average) impact_bps 반환.

        이전: 전체 tca_summary.json의 평균 impact만 반환 (단순 평균)
        이후: 종목별 EWMA로 정밀한 피드백 구현
        """
        from pathlib import Path
        import json
        project_root = Path(__file__).resolve().parent.parent.parent
        results_dir = project_root / 'results'
        history_path = results_dir / 'tca_ticker_history.json'
        if not history_path.exists():
            return 0.0
        try:
            history = json.loads(history_path.read_text())
            ticker_data = history.get(ticker)
            if not ticker_data:
                return 0.0
            return float(ticker_data.get('ewma_impact_bps', 0.0))
        except Exception as e:
            logger.critical(f'TCA 이력 로드 실패 ({ticker}): {e}', exc_info=True)
            send_emergency_page('🚨 [FATAL] {exc} at tca.py:456', exc_info=e)
            return 0.0

    def _update_ticker_history(self, tca_inputs: List[Dict], date_str: str) -> None:
        """거래 별 Market Impact를 종목별 EWMA 이력에 눈적.

        tca_ticker_history.json 포맷:
        {
          '005930': {
            'ewma_impact_bps': 7.2,
            'n_trades': 15,
            'last_date': '2026-06-20',
            'history': [
              {'date': '2026-06-20', 'impact_bps': 8.5, 'qty': 100}
            ]
          },
          ...
        }
        """
        from pathlib import Path
        import json
        if not tca_inputs:
            return
        project_root = Path(__file__).resolve().parent.parent.parent
        results_dir = project_root / 'results'
        history_path = results_dir / 'tca_ticker_history.json'
        history: dict = {}
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                history = {}
        ewma_days = cfg.get('execution.tca_ewma_days', 10)
        alpha = 2.0 / (ewma_days + 1)
        ticker_impact: dict = {}
        for ti in tca_inputs:
            t = ti.get('ticker', '')
            if not t:
                continue
            fill = ti.get('fill_price', 0)
            arrival = ti.get('arrival_price', fill)
            qty = ti.get('quantity', 0)
            is_buy = ti.get('action', 'buy').lower() == 'buy'
            sign = 1 if is_buy else -1
            if arrival > 0 and fill > 0 and (qty > 0):
                impact_bps = round(sign * (fill - arrival) / arrival * 10000, 2)
                ticker_impact.setdefault(t, []).append(impact_bps)
        for ticker, impacts in ticker_impact.items():
            avg_impact = sum(impacts) / len(impacts)
            entry = history.get(ticker, {})
            prev_ewma = entry.get('ewma_impact_bps', avg_impact)
            new_ewma = alpha * avg_impact + (1 - alpha) * prev_ewma
            hist_list = entry.get('history', [])
            hist_list.append({'date': date_str, 'impact_bps': round(avg_impact, 2), 'ewma_impact_bps': round(new_ewma, 2), 'n_trades': len(impacts)})
            if len(hist_list) > 90:
                hist_list = hist_list[-90:]
            history[ticker] = {'ewma_impact_bps': round(new_ewma, 2), 'n_trades': entry.get('n_trades', 0) + len(impacts), 'last_date': date_str, 'history': hist_list}
        if history:
            atomic_write_json(history_path, history, indent=2, ensure_ascii=False, default=str)
            logger.info(f'TCA 종목별 이력 업데이트: {len(ticker_impact)}개 종목 ({date_str})')
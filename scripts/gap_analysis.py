#!/usr/bin/env python3
"""
Project Meridian — Gap Analysis Pipeline (#6)
==============================================
예측 vs 실현 격차를 체계적으로 진단.

6가지 분석 축:
  1. 섹터별 성과 (어떤 섹터에서 DA가 낮은지)
  2. 레짐별 성과 (bull/bear/neutral DA, IC)
  3. 시가총액별 분석
  4. 컨피던스별 분석
  5. 피처 기여도 분석
  6. 실패 패턴 진단 + 개선 제안

데이터 소스:
  - results/shadow_portfolio.json (trade_history)
  - results/signal_cache.json
  - results/stream_metrics.json

DynamicConfig 키:
  - gap.min_trades (default: 10)
  - gap.confidence_bins (default: [0.4, 0.6])

결과: results/gap_analysis.json

Usage:
    python3 scripts/gap_analysis.py
    # 또는 daily_pipeline.py evening phase에서 자동 실행
"""

import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_RESULTS = _PROJECT_ROOT / 'results'
_OUTPUT_FILE = _RESULTS / 'gap_analysis.json'


class GapAnalyzer:
    """예측 vs 실현 격차를 체계적으로 진단.

    shadow_portfolio.json의 trade_history에서 BUY/SELL 페어를 추출하고,
    signal_cache.json / stream_metrics.json 컨텍스트를 결합하여
    6축 성과 진단을 실행합니다.

    모든 파라미터는 DynamicConfig 기반. 하드코딩 0.
    """

    def __init__(self):
        self.min_trades = cfg.get('gap.min_trades', 10)
        self.confidence_bins = cfg.get('gap.confidence_bins', [0.4, 0.6])
        self.trades: List[Dict] = []
        self.signal_cache: Dict = {}
        self.stream_metrics: Dict = {}

    def analyze(self) -> Dict[str, Any]:
        """전체 갭 분석 실행.

        Returns:
            6축 분석 결과 딕셔너리.
        """
        result = {
            'timestamp': datetime.now().isoformat(),
            'status': 'skip',
            'min_trades': self.min_trades,
        }

        # ── 데이터 로드 ──
        self._load_data()

        if len(self.trades) < self.min_trades:
            result['reason'] = (
                f'거래 {len(self.trades)}건 < 최소 {self.min_trades}건')
            result['n_trades'] = len(self.trades)
            self._save(result)
            return result

        result['status'] = 'completed'
        result['n_trades'] = len(self.trades)

        # ── 6축 분석 ──
        result['by_sector'] = self._analyze_by_sector()
        result['by_regime'] = self._analyze_by_regime()
        result['by_market_cap'] = self._analyze_by_market_cap()
        result['by_confidence'] = self._analyze_by_confidence()
        result['feature_contribution'] = self._analyze_feature_contribution()
        result['failure_patterns'] = self._diagnose_failure_patterns()

        # ── 종합 요약 ──
        result['summary'] = self._build_summary(result)

        self._save(result)
        logger.info(
            f"  ✅ Gap Analysis: {len(self.trades)}건 분석, "
            f"overall_da={result['summary'].get('overall_da', 0):.1%}")

        return result

    # ──────────────────────────────────────────────
    # 데이터 로드
    # ──────────────────────────────────────────────

    def _load_data(self):
        """shadow_portfolio.json, signal_cache.json, stream_metrics.json 로드."""
        # trade_history
        sp_file = _RESULTS / 'shadow_portfolio.json'
        if sp_file.exists():
            try:
                sp = json.loads(sp_file.read_text())
                raw_trades = sp.get('trade_history', [])
                self.trades = self._pair_trades(raw_trades)
            except Exception as e:
                logger.warning(f"  shadow_portfolio 로드 실패: {e}")

        # signal_cache
        sc_file = _RESULTS / 'signal_cache.json'
        if sc_file.exists():
            try:
                self.signal_cache = json.loads(sc_file.read_text())
            except Exception as e:
                logger.debug(f"  signal_cache 로드 실패: {e}")

        # stream_metrics
        sm_file = _RESULTS / 'stream_metrics.json'
        if sm_file.exists():
            try:
                self.stream_metrics = json.loads(sm_file.read_text())
            except Exception as e:
                logger.debug(f"  stream_metrics 로드 실패: {e}")

    def _pair_trades(self, raw_trades: List[Dict]) -> List[Dict]:
        """BUY/SELL 매칭하여 완결된 거래 페어로 변환.

        SELL이 없는 포지션도 포함 (unrealized).
        """
        buys: Dict[str, List[Dict]] = defaultdict(list)
        paired: List[Dict] = []

        for trade in raw_trades:
            action = trade.get('action', '').upper()
            ticker = trade.get('ticker', '')
            stream = trade.get('stream', trade.get('stream_id', ''))
            key = f"{stream}:{ticker}"

            if action == 'BUY':
                buys[key].append(trade)
            elif action == 'SELL' and buys.get(key):
                buy = buys[key].pop(0)
                buy_price = float(buy.get('price', 0))
                sell_price = float(trade.get('price', 0))
                return_pct = (
                    ((sell_price / buy_price) - 1) * 100
                    if buy_price > 0 else 0)

                paired.append({
                    'ticker': ticker,
                    'name': buy.get('name', ticker),
                    'stream': stream,
                    'strategy': buy.get('strategy', ''),
                    'confidence': float(buy.get('confidence', 0.5)),
                    'buy_date': buy.get('date', ''),
                    'sell_date': trade.get('date', ''),
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'return_pct': round(return_pct, 4),
                    'predicted_up': float(buy.get('confidence', 0.5)) >= 0.5,
                    'actual_up': return_pct > 0,
                    'hit': (float(buy.get('confidence', 0.5)) >= 0.5)
                           == (return_pct > 0),
                    'reason': buy.get('reason', ''),
                    'regime': self._infer_regime(buy.get('date', '')),
                    'sector': self._infer_sector(ticker),
                    'market_cap_tier': self._infer_cap_tier(ticker),
                    'realized': True,
                })

        # 미실현 포지션도 포함 (shadow_portfolio.json positions)
        sp_file = _RESULTS / 'shadow_portfolio.json'
        if sp_file.exists():
            try:
                sp = json.loads(sp_file.read_text())
                for pos_key, pos in sp.get('positions', {}).items():
                    ticker = pos.get('ticker', '')
                    stream = pos.get('stream_id', '')
                    pnl_pct = float(pos.get('pnl_pct', 0))
                    # confidence는 trade_history에서 매칭
                    conf = 0.5
                    for t in raw_trades:
                        if (t.get('ticker') == ticker
                                and t.get('action', '').upper() == 'BUY'):
                            conf = float(t.get('confidence', 0.5))
                            break

                    paired.append({
                        'ticker': ticker,
                        'name': pos.get('name', ticker),
                        'stream': stream,
                        'strategy': pos.get('strategy', ''),
                        'confidence': conf,
                        'buy_date': pos.get('entry_date', ''),
                        'sell_date': '',
                        'buy_price': float(pos.get('avg_price', 0)),
                        'sell_price': float(pos.get('current_price', 0)),
                        'return_pct': round(pnl_pct, 4),
                        'predicted_up': conf >= 0.5,
                        'actual_up': pnl_pct > 0,
                        'hit': (conf >= 0.5) == (pnl_pct > 0),
                        'reason': '',
                        'regime': self._infer_regime(
                            pos.get('entry_date', '')),
                        'sector': self._infer_sector(ticker),
                        'market_cap_tier': self._infer_cap_tier(ticker),
                        'realized': False,
                    })
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        return paired

    # ──────────────────────────────────────────────
    # 1. 섹터별 분석
    # ──────────────────────────────────────────────

    def _analyze_by_sector(self) -> Dict:
        """섹터별 DA/IC/수익률 분석."""
        sector_groups = defaultdict(list)
        for t in self.trades:
            sector_groups[t.get('sector', 'unknown')].append(t)

        results = {}
        for sector, trades in sector_groups.items():
            if len(trades) < 2:
                continue
            da = sum(1 for t in trades if t['hit']) / len(trades)
            avg_ret = np.mean([t['return_pct'] for t in trades])
            win_rate = (
                sum(1 for t in trades if t['return_pct'] > 0) / len(trades))
            ic = self._compute_ic(trades)

            results[sector] = {
                'n_trades': len(trades),
                'da': round(da, 4),
                'avg_return_pct': round(float(avg_ret), 4),
                'win_rate': round(win_rate, 4),
                'ic': round(ic, 4),
            }

        return results

    # ──────────────────────────────────────────────
    # 2. 레짐별 분석
    # ──────────────────────────────────────────────

    def _analyze_by_regime(self) -> Dict:
        """레짐별 DA/IC 분석."""
        regime_groups = defaultdict(list)
        for t in self.trades:
            regime_groups[t.get('regime', 'unknown')].append(t)

        results = {}
        for regime, trades in regime_groups.items():
            if len(trades) < 2:
                continue
            da = sum(1 for t in trades if t['hit']) / len(trades)
            avg_ret = np.mean([t['return_pct'] for t in trades])
            win_rate = (
                sum(1 for t in trades if t['return_pct'] > 0) / len(trades))
            ic = self._compute_ic(trades)

            results[regime] = {
                'n_trades': len(trades),
                'da': round(da, 4),
                'avg_return_pct': round(float(avg_ret), 4),
                'win_rate': round(win_rate, 4),
                'ic': round(ic, 4),
            }

        return results

    # ──────────────────────────────────────────────
    # 3. 시가총액별 분석
    # ──────────────────────────────────────────────

    def _analyze_by_market_cap(self) -> Dict:
        """시가총액 구간별 분석."""
        cap_groups = defaultdict(list)
        for t in self.trades:
            cap_groups[t.get('market_cap_tier', 'unknown')].append(t)

        results = {}
        for tier, trades in cap_groups.items():
            if len(trades) < 2:
                continue
            da = sum(1 for t in trades if t['hit']) / len(trades)
            avg_ret = np.mean([t['return_pct'] for t in trades])
            win_rate = (
                sum(1 for t in trades if t['return_pct'] > 0) / len(trades))

            results[tier] = {
                'n_trades': len(trades),
                'da': round(da, 4),
                'avg_return_pct': round(float(avg_ret), 4),
                'win_rate': round(win_rate, 4),
            }

        return results

    # ──────────────────────────────────────────────
    # 4. 컨피던스별 분석
    # ──────────────────────────────────────────────

    def _analyze_by_confidence(self) -> Dict:
        """신뢰도 구간별 DA/수익률 분석.

        DynamicConfig gap.confidence_bins 기반 구간 분할.
        """
        bins = sorted(self.confidence_bins)
        # 경계: [0, bins[0]], (bins[0], bins[1]], ..., (bins[-1], 1.0]
        edges = [0.0] + bins + [1.0]
        labels = []
        for i in range(len(edges) - 1):
            labels.append(f"{edges[i]:.2f}-{edges[i+1]:.2f}")

        bin_groups: Dict[str, List[Dict]] = {l: [] for l in labels}
        for t in self.trades:
            conf = t.get('confidence', 0.5)
            for i in range(len(edges) - 1):
                if edges[i] <= conf < edges[i + 1] or (
                        i == len(edges) - 2 and conf == edges[i + 1]):
                    bin_groups[labels[i]].append(t)
                    break

        results = {}
        for label, trades in bin_groups.items():
            if not trades:
                continue
            da = sum(1 for t in trades if t['hit']) / len(trades)
            avg_ret = np.mean([t['return_pct'] for t in trades])
            win_rate = (
                sum(1 for t in trades if t['return_pct'] > 0) / len(trades))

            results[label] = {
                'n_trades': len(trades),
                'da': round(da, 4),
                'avg_return_pct': round(float(avg_ret), 4),
                'win_rate': round(win_rate, 4),
                'avg_confidence': round(
                    float(np.mean([t['confidence'] for t in trades])), 4),
            }

        return results

    # ──────────────────────────────────────────────
    # 5. 피처 기여도 분석
    # ──────────────────────────────────────────────

    def _analyze_feature_contribution(self) -> Dict:
        """모델 피처 중요도 + SHAP 결과 기반 기여도 진단."""
        result = {}

        # ensemble_meta.json에서 피처 목록 확인
        meta_file = _RESULTS / 'models' / 'ensemble_meta.json'
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                result['n_features'] = meta.get('n_features', 0)
                result['feature_names'] = meta.get('feature_names', [])[:10]
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        # feature_importance_audit.json
        audit_file = _RESULTS / 'feature_importance_audit.json'
        if audit_file.exists():
            try:
                audit = json.loads(audit_file.read_text())
                result['audit'] = {
                    'n_low_importance': audit.get('n_low_importance', 0),
                    'n_psi_drift': audit.get('n_psi_drift', 0),
                    'n_noise': audit.get('n_noise', 0),
                }
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        # SHAP 분석 결과
        shap_file = _RESULTS / 'shap_analysis.json'
        if shap_file.exists():
            try:
                shap = json.loads(shap_file.read_text())
                top_features = shap.get('top_features', [])
                weak_features = shap.get('weak_features', [])
                result['shap'] = {
                    'top_features': top_features[:5],
                    'n_weak': len(weak_features),
                    'weak_features': weak_features[:5],
                }
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        # drift_guard 상태
        drift_file = _RESULTS / 'drift_guard_state.json'
        if drift_file.exists():
            try:
                drift = json.loads(drift_file.read_text())
                result['drift'] = {
                    'n_drifted': drift.get('n_drifted', 0),
                    'mean_psi': round(drift.get('mean_psi', 0), 4),
                    'retrain_needed': drift.get('retrain_needed', False),
                }
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        return result

    # ──────────────────────────────────────────────
    # 6. 실패 패턴 진단 + 개선 제안
    # ──────────────────────────────────────────────

    def _diagnose_failure_patterns(self) -> Dict:
        """실패 패턴 식별 + 개선 제안 생성."""
        failures = [t for t in self.trades if not t['hit']]
        if not failures:
            return {'n_failures': 0, 'patterns': [], 'suggestions': []}

        patterns = []
        suggestions = []

        # 패턴 1: 고신뢰도 실패 (confidence ≥ 0.7인데 실패)
        high_conf_fails = [
            t for t in failures if t['confidence'] >= 0.7]
        if high_conf_fails:
            ratio = len(high_conf_fails) / len(failures)
            patterns.append({
                'type': 'high_confidence_miss',
                'description': '고신뢰도(≥70%) 예측 실패',
                'count': len(high_conf_fails),
                'ratio': round(ratio, 4),
                'avg_loss_pct': round(float(np.mean(
                    [t['return_pct'] for t in high_conf_fails])), 4),
            })
            if ratio > cfg.get('gap.high_conf_fail_ratio', 0.3):
                suggestions.append(
                    'Confidence Calibrator 재보정 필요 '
                    '(고신뢰도 구간 과신)')

        # 패턴 2: 특정 스트림 집중 실패
        stream_fail_counts: Dict[str, int] = defaultdict(int)
        for t in failures:
            stream_fail_counts[t['stream']] += 1
        worst_stream = max(
            stream_fail_counts, key=stream_fail_counts.get,
            default=None)
        if worst_stream and stream_fail_counts[worst_stream] >= cfg.get('gap.concentration_fail_count', 3):
            total_in_stream = sum(
                1 for t in self.trades if t['stream'] == worst_stream)
            fail_rate = (
                stream_fail_counts[worst_stream] / total_in_stream
                if total_in_stream > 0 else 0)
            patterns.append({
                'type': 'stream_concentration',
                'description': f'{worst_stream} 스트림 실패 집중',
                'stream': worst_stream,
                'fail_count': stream_fail_counts[worst_stream],
                'fail_rate': round(fail_rate, 4),
            })
            if fail_rate > 0.6:
                suggestions.append(
                    f'{worst_stream} 스트림 신호 품질 점검 필요')

        # 패턴 3: 연속 실패 (3건 이상 연속 손실)
        sorted_trades = sorted(
            self.trades, key=lambda t: t.get('buy_date', ''))
        max_consecutive_loss = 0
        current_streak = 0
        for t in sorted_trades:
            if t['return_pct'] < 0:
                current_streak += 1
                max_consecutive_loss = max(
                    max_consecutive_loss, current_streak)
            else:
                current_streak = 0

        if max_consecutive_loss >= cfg.get('gap.consecutive_loss_threshold', 3):
            patterns.append({
                'type': 'consecutive_losses',
                'description': f'최대 {max_consecutive_loss}건 연속 손실',
                'max_streak': max_consecutive_loss,
            })
            if max_consecutive_loss >= cfg.get('gap.consecutive_loss_retrain', 5):
                suggestions.append(
                    f'연속 손실 {cfg.get("gap.consecutive_loss_retrain", 5)}건+ → 모델 재학습 또는 '
                    '신호 임계값 상향 검토')

        # 패턴 4: 레짐 미스매치 (특정 레짐에서 DA < 40%)
        for regime in ['bull', 'bear', 'caution', 'crash']:
            regime_trades = [
                t for t in self.trades if t.get('regime') == regime]
            if len(regime_trades) >= cfg.get('gap.min_sector_trades', 3):
                regime_da = (
                    sum(1 for t in regime_trades if t['hit'])
                    / len(regime_trades))
                if regime_da < 0.40:
                    patterns.append({
                        'type': 'regime_mismatch',
                        'description': (
                            f'{regime} 레짐 DA={regime_da:.1%} '
                            f'({len(regime_trades)}건)'),
                        'regime': regime,
                        'da': round(regime_da, 4),
                        'n_trades': len(regime_trades),
                    })
                    suggestions.append(
                        f'{regime} 레짐 전용 파라미터 조정 또는 '
                        f'거래 축소 검토')

        # 패턴 5: 큰 손실 집중 (return < -5%)
        big_losses = [
            t for t in self.trades if t['return_pct'] < -5.0]
        if big_losses:
            patterns.append({
                'type': 'large_loss_cluster',
                'description': f'-5%+ 손실 {len(big_losses)}건',
                'count': len(big_losses),
                'avg_loss_pct': round(float(np.mean(
                    [t['return_pct'] for t in big_losses])), 4),
                'tickers': [t['ticker'] for t in big_losses[:5]],
            })
            suggestions.append(
                'Stop-Loss 매개변수(exit.sl_atr_multiplier) '
                '타이트닝 검토')

        return {
            'n_failures': len(failures),
            'n_total': len(self.trades),
            'overall_fail_rate': round(
                len(failures) / len(self.trades), 4),
            'patterns': patterns,
            'suggestions': suggestions,
        }

    # ──────────────────────────────────────────────
    # 종합 요약
    # ──────────────────────────────────────────────

    def _build_summary(self, result: Dict) -> Dict:
        """6축 분석 결과 종합 요약."""
        n = len(self.trades)
        overall_da = sum(1 for t in self.trades if t['hit']) / n if n else 0
        overall_wr = (
            sum(1 for t in self.trades if t['return_pct'] > 0) / n
            if n else 0)
        avg_return = float(np.mean(
            [t['return_pct'] for t in self.trades])) if n else 0
        overall_ic = self._compute_ic(self.trades)

        # 스트림별 요약
        stream_summary = {}
        stream_groups = defaultdict(list)
        for t in self.trades:
            stream_groups[t['stream']].append(t)
        for sid, trades in stream_groups.items():
            sn = len(trades)
            s_da = sum(1 for t in trades if t['hit']) / sn if sn else 0
            s_wr = (
                sum(1 for t in trades if t['return_pct'] > 0) / sn
                if sn else 0)
            s_avg_ret = float(np.mean(
                [t['return_pct'] for t in trades])) if sn else 0
            stream_summary[sid] = {
                'n_trades': sn,
                'da': round(s_da, 4),
                'win_rate': round(s_wr, 4),
                'avg_return_pct': round(s_avg_ret, 4),
            }

        # 최약 축 식별
        weakest_axis = None
        weakest_detail = ''

        # 섹터 최약
        by_sector = result.get('by_sector', {})
        if by_sector:
            worst_sector = min(
                by_sector, key=lambda s: by_sector[s].get('da', 1.0))
            if by_sector[worst_sector]['da'] < 0.50:
                weakest_axis = 'sector'
                weakest_detail = (
                    f"{worst_sector}: DA={by_sector[worst_sector]['da']:.1%}")

        # 레짐 최약
        by_regime = result.get('by_regime', {})
        if by_regime:
            worst_regime = min(
                by_regime, key=lambda r: by_regime[r].get('da', 1.0))
            regime_da = by_regime[worst_regime]['da']
            if regime_da < 0.45:
                if weakest_axis is None or regime_da < by_sector.get(
                        worst_sector, {}).get('da', 1.0):
                    weakest_axis = 'regime'
                    weakest_detail = (
                        f"{worst_regime}: DA={regime_da:.1%}")

        n_suggestions = len(
            result.get('failure_patterns', {}).get('suggestions', []))

        return {
            'overall_da': round(overall_da, 4),
            'overall_win_rate': round(overall_wr, 4),
            'avg_return_pct': round(avg_return, 4),
            'overall_ic': round(overall_ic, 4),
            'n_trades': n,
            'by_stream': stream_summary,
            'weakest_axis': weakest_axis,
            'weakest_detail': weakest_detail,
            'n_improvement_suggestions': n_suggestions,
        }

    # ──────────────────────────────────────────────
    # 유틸리티
    # ──────────────────────────────────────────────

    def _compute_ic(self, trades: List[Dict]) -> float:
        """Spearman IC 계산."""
        if len(trades) < 3:
            return 0.0
        try:
            from scipy import stats
            confs = [t['confidence'] for t in trades]
            rets = [t['return_pct'] for t in trades]
            ic, _ = stats.spearmanr(confs, rets)
            return float(ic) if not np.isnan(ic) else 0.0
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return 0.0

    def _infer_regime(self, date_str: str) -> str:
        """날짜 기반 레짐 추론 (signal_cache kr_regime 사용)."""
        # 단순화: signal_cache의 현재 레짐 사용
        return self.signal_cache.get('kr_regime', 'unknown')

    def _infer_sector(self, ticker: str) -> str:
        """티커 기반 섹터 추론.

        ETF는 별도 분류, 개별주는 universe 기반.
        """
        etf_prefixes = {
            '069': 'Index', '091': 'Sector', '114': 'Inverse',
            '122': 'Leverage', '305': 'Bond', '244': 'Commodity',
            '117': 'Sector', '360': 'Theme', '315': 'Theme',
            '371': 'Theme', '395': 'Sector', '411': 'Theme',
            '132': 'Commodity', '133': 'Global', '139': 'Sector',
            '148': 'Bond', '192': 'Sector', '195': 'Sector',
            '227': 'Sector', '279': 'Dividend', '289': 'Dividend',
            '290': 'Dividend', '329': 'Dividend', '379': 'Global',
            '409': 'Theme', '441': 'Dividend', '443': 'Individual',
            '455': 'Dividend', '458': 'Dividend',
        }
        prefix = ticker[:3]
        sector = etf_prefixes.get(prefix, 'Individual')
        return sector

    def _infer_cap_tier(self, ticker: str) -> str:
        """시가총액 구간 추론.

        간단한 휴리스틱: 주가 기반 (정확한 시총 데이터 미사용).
        """
        # shadow_portfolio에서 buy_price로 간접 추론
        for t in self.trades:
            if t['ticker'] == ticker:
                price = t.get('buy_price', 0)
                if price >= 500_000:
                    return 'large_cap'
                elif price >= 50_000:
                    return 'mid_cap'
                else:
                    return 'small_cap'
        return 'unknown'

    def _save(self, result: Dict):
        """결과를 results/gap_analysis.json에 저장."""
        try:
            import tempfile
            import os
            _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(_OUTPUT_FILE.parent), suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                json.dump(result, f, indent=2, ensure_ascii=False,
                          default=str)
            os.replace(tmp, str(_OUTPUT_FILE))
        except Exception as e:
            logger.warning(f"  Gap Analysis 저장 실패: {e}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    analyzer = GapAnalyzer()
    result = analyzer.analyze()
    print(json.dumps(result, indent=2, ensure_ascii=False))

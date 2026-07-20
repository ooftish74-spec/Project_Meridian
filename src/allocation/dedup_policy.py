#!/usr/bin/env python3
"""
DedupPolicy — 크로스-스트림 신호 중복 해소 엔진
==================================================

4-Stream Orthogonal Architecture에서 동일 종목이
복수 스트림에서 신호를 발생시킬 수 있습니다.

정책:
  - 동일 종목이 2+ 스트림에서 출현하면 "Edge Strength" 기반 우선권 부여
  - 보유기간 차별화 공존 (S1 단기 vs S4 장기) → 양쪽 유지하되 비중 조절
  - S4 계좌별(ISA/IRP/PENSION/BROKERAGE)은 독립 계좌이므로 dedup 미적용

Edge Strength 공식:
  edge = confidence * stream_priority * regime_boost * size_pct

스트림 우선순위 (높을수록 강함):
  S1 (Leverage): 단기, 고확신 → priority 1.5 (단, 동일 ticker 중복 시 에지↑이면 유지)
  S2 (Alpha):    중기 팩터     → priority 1.2
  S3 (ETF):      ETF 전용      → priority 0.8 (개별주와 겹칠 일 없음)
  S4 (Advisory): 세금최적화    → priority 1.0

중복 유형:
  A. S1과 S2 (개별주 vs 개별주) → edge strength 비교, 강한 쪽만 유지
  B. S1/S2과 S4-BROKERAGE (개별주 vs QV Core) → 보유기간 다르면 공존
  C. S3과 S4 (ETF vs ETF) → 계좌 분리이므로 공존
  D. S4-ISA와 S4-BROKERAGE (동일 ticker) → 계좌 분리이므로 공존

Usage:
    from src.allocation.dedup_policy import DedupPolicy
    dedup = DedupPolicy()
    final_signals = dedup.resolve(all_signals, regime='bull')
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()


# ═══════════════════════════════════════
# Stream Priority & Holding Period
# ═══════════════════════════════════════

STREAM_PRIORITY = {
    'S1': 1.5,   # Leverage 단기 → 높은 우선순위
    'S2': 1.2,   # Alpha 중기 → 중간 우선순위
    'S3': 0.8,   # ETF → 낮은 우선순위 (개별주와 겹칠 일 드묾)
    'S4': 1.0,   # Advisory → 기본
}

HOLDING_CLASS = {
    'S1': 'short_term',    # 1~5일
    'S2': 'medium_term',   # 5~20일
    'S3': 'long_term',     # 월간 리밸런싱
    'S4': 'long_term',     # 월~분기 리밸런싱
}

# S4 계좌별 전략 → dedup 면제 기준
S4_ACCOUNT_STRATEGIES = {
    'isa_qval', 'brokerage_qv_core', 'advisory',
}


class DedupPolicy:
    """크로스-스트림 신호 중복 해소 정책.

    ★ 2026-05-26 비활성화:
      - S1은 ETF 전용 → S2(개별주)와 ticker 중복 불가
      - S2+S4 공존은 이미 허용으로 정의됨
      - 따라서 cross-stream dedup이 불필요

    동일 ticker가 복수 스트림에서 출현할 때:
      1. 보유기간이 다르면 공존 허용 (비중 합산 cap 적용)
      2. 보유기간이 같으면 edge strength 비교하여 강한 쪽만 유지
      3. S4 계좌 간은 무조건 공존 (계좌 분리)
    """

    def __init__(self, enabled: bool = False):
        self._dedup_log: List[Dict] = []
        self._enabled = enabled

    def resolve(self, all_signals: List[Dict],
                regime: str = 'caution') -> List[Dict]:
        """모든 스트림 신호를 받아서 중복 해소.

        비활성화 시 신호를 그대로 통과시킵니다.

        Args:
            all_signals: S1~S4의 모든 신호 리스트
            regime: 현재 레짐

        Returns:
            중복 해소된 최종 신호 리스트 (비활성화 시 원본 그대로)
        """
        if not all_signals:
            return []

        if not self._enabled:
            logger.debug("  DedupPolicy: 비활성화 상태 — 신호 그대로 통과")
            return all_signals

        # Step 1: 신호를 ticker별로 그룹핑
        ticker_groups = self._group_by_ticker(all_signals)

        # Step 2: 그룹별 중복 해소
        resolved = []
        n_deduped = 0

        for ticker, signals in ticker_groups.items():
            if len(signals) == 1:
                # 단일 신호 → 그대로 통과
                resolved.append(signals[0])
                continue

            # 복수 신호 → 중복 해소
            kept, dropped = self._resolve_ticker(ticker, signals, regime)
            resolved.extend(kept)
            n_deduped += len(dropped)

            if dropped:
                self._dedup_log.append({
                    'ticker': ticker,
                    'kept': [(s['stream_id'], s.get('account', ''),
                              round(s.get('_edge_strength', 0), 3))
                             for s in kept],
                    'dropped': [(s['stream_id'], s.get('account', ''),
                                 round(s.get('_edge_strength', 0), 3))
                                for s in dropped],
                    'timestamp': datetime.now().isoformat(),
                })

        # Step 3: 비중 합산 cap 적용
        resolved = self._apply_total_weight_cap(resolved)

        if n_deduped > 0:
            logger.info(f"  🔄 DedupPolicy: {n_deduped}건 중복 해소, "
                        f"{len(resolved)}건 최종 신호")

        return resolved

    def _group_by_ticker(self, signals: List[Dict]) -> Dict[str, List[Dict]]:
        """Ticker별 신호 그룹핑."""
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for sig in signals:
            ticker = sig.get('ticker', '')
            groups[ticker].append(sig)
        return dict(groups)

    def _resolve_ticker(self, ticker: str, signals: List[Dict],
                         regime: str) -> Tuple[List[Dict], List[Dict]]:
        """단일 ticker에 대한 중복 해소.

        Returns:
            (kept_signals, dropped_signals)
        """
        # Edge Strength 계산
        for sig in signals:
            sig['_edge_strength'] = self._compute_edge_strength(sig, regime)

        # 카테고리 분류
        categorized = self._categorize_signals(signals)

        kept = []
        dropped = []

        # Rule 1: S4 계좌 간은 무조건 공존 (계좌 분리)
        s4_signals = categorized.get('S4', [])
        if len(s4_signals) > 1:
            # 계좌가 다르면 모두 유지
            accounts_seen = set()
            for sig in s4_signals:
                acct = sig.get('account', '')
                if acct not in accounts_seen:
                    accounts_seen.add(acct)
                    kept.append(sig)
                else:
                    # 동일 계좌 내 중복 → edge strength 비교
                    dropped.append(sig)

        elif len(s4_signals) == 1:
            kept.append(s4_signals[0])

        # Rule 2: S3 (ETF) → 개별주와 겹칠 일 드물지만, 있으면 공존
        s3_signals = categorized.get('S3', [])
        kept.extend(s3_signals)

        # Rule 3: S1과 S2 (개별주 vs 개별주) → 핵심 중복 해소
        s1_signals = categorized.get('S1', [])
        s2_signals = categorized.get('S2', [])

        if s1_signals and s2_signals:
            # 보유기간이 다르므로 공존 가능
            # 단, 합산 비중이 max_single_ticker_pct를 초과하지 않도록 조절
            max_pct = cfg.get('dedup.max_single_ticker_pct', 0.15)

            all_stock_signals = s1_signals + s2_signals
            total_pct = sum(s.get('size_pct', 0) for s in all_stock_signals)

            if total_pct <= max_pct:
                # 한도 내 → 모두 유지
                kept.extend(all_stock_signals)
            else:
                # 한도 초과 → 비중 비례 축소
                scale = max_pct / max(total_pct, 0.001)
                for sig in all_stock_signals:
                    sig['size_pct'] = round(sig.get('size_pct', 0) * scale, 4)
                    sig['_dedup_scaled'] = True
                kept.extend(all_stock_signals)
        else:
            kept.extend(s1_signals)
            kept.extend(s2_signals)

        # Rule 4: S1/S2와 S4-BROKERAGE 중복 (QV Core 개별주)
        # 보유기간 차별화로 공존하되, 합산 비중 cap 적용
        s4_brokerage = [s for s in s4_signals
                        if s.get('strategy') == 'brokerage_qv_core']
        short_medium = s1_signals + s2_signals

        if s4_brokerage and short_medium:
            overlap_pct = sum(s.get('size_pct', 0)
                              for s in s4_brokerage + short_medium)
            max_overlap = cfg.get('dedup.max_cross_stream_overlap_pct', 0.20)
            if overlap_pct > max_overlap:
                # S4 측 비중 축소 (S1/S2 단기 우선)
                scale = max(0.3, 1 - (overlap_pct - max_overlap) / max_overlap)
                for sig in s4_brokerage:
                    if sig in kept:
                        sig['size_pct'] = round(
                            sig.get('size_pct', 0) * scale, 4)
                        sig['_dedup_scaled'] = True

        return kept, dropped

    def _categorize_signals(self, signals: List[Dict]) -> Dict[str, List[Dict]]:
        """스트림별 분류."""
        cats: Dict[str, List[Dict]] = defaultdict(list)
        for sig in signals:
            sid = sig.get('stream_id', '')
            cats[sid].append(sig)
        return dict(cats)

    def _compute_edge_strength(self, signal: Dict, regime: str) -> float:
        """Edge Strength 계산.

        edge = confidence × stream_priority × regime_boost × (1 + size_pct)

        Args:
            signal: 신호 딕셔너리
            regime: 현재 레짐

        Returns:
            Edge strength (0.0 ~ ∞)
        """
        confidence = signal.get('confidence', 0.5)
        stream_id = signal.get('stream_id', 'S2')
        size_pct = signal.get('size_pct', 0.05)

        priority = STREAM_PRIORITY.get(stream_id, 1.0)

        # 레짐별 부스트
        regime_boosts = {
            'bull':    {'S1': 1.3, 'S2': 1.1, 'S3': 1.0, 'S4': 0.9},
            'caution': {'S1': 0.8, 'S2': 1.0, 'S3': 1.1, 'S4': 1.1},
            'bear':    {'S1': 0.5, 'S2': 0.8, 'S3': 1.2, 'S4': 1.3},
            'crash':   {'S1': 0.3, 'S2': 0.5, 'S3': 1.3, 'S4': 1.5},
        }
        boost = regime_boosts.get(regime, {}).get(stream_id, 1.0)

        # QVM score 보너스 (S4 개별주)
        qvm = signal.get('qvm_score', 0)
        qvm_bonus = 1.0 + min(0.3, qvm / 100.0 * 0.3)

        edge = confidence * priority * boost * (1 + size_pct) * qvm_bonus
        return edge

    def _apply_total_weight_cap(self, signals: List[Dict]) -> List[Dict]:
        """동일 ticker 합산 비중 cap.

        단일 종목에 대한 전체 포트폴리오 비중이 max를 초과하지 않도록.
        """
        max_single = cfg.get('dedup.max_single_ticker_pct', 0.15)

        # Ticker별 합산
        ticker_total: Dict[str, float] = defaultdict(float)
        for sig in signals:
            ticker_total[sig.get('ticker', '')] += sig.get('size_pct', 0)

        # 초과 종목 비례 축소
        for sig in signals:
            ticker = sig.get('ticker', '')
            total = ticker_total.get(ticker, 0)
            if total > max_single and total > 0:
                scale = max_single / total
                sig['size_pct'] = round(sig.get('size_pct', 0) * scale, 4)
                sig['_dedup_capped'] = True

        return signals

    # ═══════════════════════════════════════
    # 분석 & 로깅
    # ═══════════════════════════════════════

    def get_overlap_report(self, all_signals: List[Dict]) -> Dict:
        """크로스-스트림 중복 분석 리포트.

        Returns:
            {
                'total_signals': int,
                'unique_tickers': int,
                'overlapping_tickers': int,
                'overlap_details': [{ticker, streams, total_pct}],
                'stream_signal_counts': {S1: n, S2: n, ...},
            }
        """
        ticker_streams: Dict[str, List[str]] = defaultdict(list)
        ticker_pcts: Dict[str, float] = defaultdict(float)

        for sig in all_signals:
            ticker = sig.get('ticker', '')
            stream = sig.get('stream_id', '')
            ticker_streams[ticker].append(stream)
            ticker_pcts[ticker] += sig.get('size_pct', 0)

        overlapping = {
            t: streams for t, streams in ticker_streams.items()
            if len(set(streams)) > 1
        }

        stream_counts: Dict[str, int] = defaultdict(int)
        for sig in all_signals:
            stream_counts[sig.get('stream_id', '')] += 1

        return {
            'total_signals': len(all_signals),
            'unique_tickers': len(ticker_streams),
            'overlapping_tickers': len(overlapping),
            'overlap_details': [
                {
                    'ticker': t,
                    'streams': sorted(set(streams)),
                    'total_pct': round(ticker_pcts.get(t, 0), 4),
                }
                for t, streams in sorted(overlapping.items())
            ],
            'stream_signal_counts': dict(stream_counts),
        }

    def get_dedup_log(self) -> List[Dict]:
        """최근 중복 해소 기록."""
        return self._dedup_log[-50:]

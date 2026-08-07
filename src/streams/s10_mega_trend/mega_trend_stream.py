#!/usr/bin/env python3
"""
S10 Mega-Trend Alpha Stream — 섹터별 주도주 사이클 추적
=========================================================

[Phase 82] 전면 재설계

전략 핵심:
  KR 증시는 수급 + 섹터 사이클 중심으로 움직임.
  삼성전자 한 종목이 KOSPI의 20~30%를 차지.
  → 섹터별 '외국인이 실제로 매수하는 대장주'를 추적하는 것이
    순수 모멘텀·팩터 전략보다 KR 시장에서 훨씬 직접적인 수익원.

신호 구조 (실증 ICIR 기반):
  1. flow_foreign_streak (ICIR=0.305) — 외국인 연속 순매수 흐름 [가중 45%]
  2. flow_foreign_net_buy (ICIR=0.229) — 당일 외국인 순매수 [가중 35%]
  3. flow_foreign_accel (ICIR=0.219) — 외국인 매수 가속도 [가중 20%]
  4. MA120 추세 확인 (보너스/패널티)
  5. HMM 레짐 멀티플라이어

모든 파라미터: DynamicConfig `s10.*` 관리 (하드코딩 없음)
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream

try:
    from src.utils.time_utils import now_kst
except ImportError as e:
    def now_kst():
        return datetime.now()

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ══════════════════════════════════════════════════════════════════════════
# 섹터→대장주 기본 매핑 (YAML `s10.sector_stock_map` SSoT로 오버라이드)
# ══════════════════════════════════════════════════════════════════════════

_DEFAULT_SECTOR_STOCK_MAP: Dict[str, List[Dict]] = {
    "semiconductor": [
        {"ticker": "005930", "name": "삼성전자",   "weight": 0.60},
        {"ticker": "000660", "name": "SK하이닉스", "weight": 0.40},
    ],
    "battery": [
        {"ticker": "373220", "name": "LG에너지솔루션", "weight": 0.55},
        {"ticker": "006400", "name": "삼성SDI",        "weight": 0.45},
    ],
    "bio": [
        {"ticker": "207940", "name": "삼성바이오로직스", "weight": 0.60},
        {"ticker": "068270", "name": "셀트리온",         "weight": 0.40},
    ],
    "healthcare": [
        {"ticker": "207940", "name": "삼성바이오로직스", "weight": 0.60},
        {"ticker": "068270", "name": "셀트리온",         "weight": 0.40},
    ],
    "defense": [
        {"ticker": "012450", "name": "한화에어로스페이스", "weight": 0.60},
        {"ticker": "047810", "name": "한국항공우주",       "weight": 0.40},
    ],
    "auto": [
        {"ticker": "005380", "name": "현대차", "weight": 0.60},
        {"ticker": "000270", "name": "기아",   "weight": 0.40},
    ],
    "it": [
        {"ticker": "035420", "name": "NAVER",  "weight": 0.60},
        {"ticker": "035720", "name": "카카오", "weight": 0.40},
    ],
    "finance": [
        {"ticker": "105560", "name": "KB금융",   "weight": 0.55},
        {"ticker": "055550", "name": "신한지주", "weight": 0.45},
    ],
    "industrial": [
        {"ticker": "012450", "name": "한화에어로스페이스", "weight": 0.60},
        {"ticker": "047810", "name": "한국항공우주",       "weight": 0.40},
    ],
    "us_semiconductor": [
        {"ticker": "005930", "name": "삼성전자",   "weight": 0.60},
        {"ticker": "000660", "name": "SK하이닉스", "weight": 0.40},
    ],
    "us_tech": [
        {"ticker": "035420", "name": "NAVER",  "weight": 0.60},
        {"ticker": "035720", "name": "카카오", "weight": 0.40},
    ],
}


class S10MegaTrendStream(BaseStream):
    """S10: 섹터별 주도주 사이클 추적 스트림 (Phase 82 전면 재설계).

    Sleeve A 통합, S3 섹터 스코어와 완전 연동.
    """

    def __init__(self):
        super().__init__('S10', 'S10 Mega-Trend')
        self.stream_id = 'S10_MEGA_TREND'
        # 섹터→대장주 매핑: YAML SSoT, 기본값 폴백
        self._sector_map: Dict[str, List[Dict]] = cfg.get(
            's10.sector_stock_map', _DEFAULT_SECTOR_STOCK_MAP
        )
        logger.info(f"  [S10] Mega-Trend Stream 초기화 ({len(self._sector_map)}섹터)")

    # ──────────────────────────────────────────────────────────────────────

    def generate_signals(
        self,
        regime: str,
        market_data: Dict[str, Any],
    ) -> List[Dict]:
        """S10 매매 신호 생성."""
        logger.info(f"  [S10] 신호 생성 시작 (레짐={regime})")

        # Crash 구간 전량 청산
        if bool(cfg.get('s10.exit_on_crash', True)) and str(regime).lower() == 'crash':
            logger.info("  [S10] Crash 레짐 → 전량 청산")
            return self._exit_all_signal("Crash regime: S10 full exit")

        # 주도 섹터 선정
        top_n        = int(cfg.get('s10.top_sector_count', 2))
        top_sectors  = self._select_top_sectors(market_data, regime, top_n)
        logger.info(f"  [S10] 주도 섹터: {top_sectors}")

        signals: List[Dict] = []
        features = market_data.get('features', {})

        for sector in top_sectors:
            stocks = self._sector_map.get(sector, [])
            for stock in stocks:
                ticker         = stock['ticker']
                name           = stock['name']
                sector_weight  = float(stock.get('weight', 0.5))
                f_data         = features.get(ticker, features.get(f'kr_{ticker}', {}))

                sig = self._evaluate_stock(ticker=ticker, name=name,
                                           f_data=f_data, regime=regime)

                # 1. Dynamic VIX-Adjusted TP/SL & 2. 3D Local Panic Index (LPI) Rejection
                _cache = market_data.get('signal_cache', {}) if isinstance(market_data.get('signal_cache'), dict) else {}
                _vix = float(_cache.get('vix', 15.0))
                _vkospi = float(_cache.get('vkospi', 15.0))
                _usdkrw = float(_cache.get('usdkrw', 1350.0))
                _usdkrw_prev = float(_cache.get('usdkrw_prev', 1350.0))
                
                # VIX 연동 가변 방어막 (Dynamic TP/SL)
                if _vix < 15.0:
                    dyn_tp = 0.30
                    dyn_sl = 0.10
                elif _vix < 20.0:
                    dyn_tp = 0.15
                    dyn_sl = 0.05
                else:
                    dyn_tp = 0.05
                    dyn_sl = 0.025
                    
                # [Red Team 2] 3D Local Panic Index (LPI) 오버나이트 거부
                # 미장(VIX)뿐만 아니라 한국 장(VKOSPI)과 환율(USD/KRW) 쇼크를 수학적으로 통합
                _usdkrw_ratio = _usdkrw / _usdkrw_prev if _usdkrw_prev > 0 else 1.0
                _lpi = max(_vix / 18.0, _vkospi / 22.0, _usdkrw_ratio / 1.015)
                
                if _lpi > 1.0:
                    logger.warning(f"    [S10] LPI({_lpi:.2f} > 1.0) 초과! 한국형 디커플링 쉴드 가동 -> {name} 오버나이트 거부 및 강제 청산")
                    sig['action'] = 'exit'
                    sig['reason'] = f'LPI > 1.0 Overnight Rejection (VIX={_vix:.1f}, VKO={_vkospi:.1f}, FX={_usdkrw_ratio:.3f})'

                if sig['action'] == 'buy':
                    kelly_frac = self._compute_kelly(sig)
                    size_pct   = min(
                        round(kelly_frac * sector_weight, 4),
                        float(cfg.get('s10.max_single_stock_pct', 0.30)),
                    )
                    signals.append({
                        'ticker':        ticker,
                        'name':          name,
                        'direction':     'long',
                        'size_pct':      size_pct,
                        'confidence':    sig['confidence'],
                        'strategy':      'S10_MegaTrend',
                        'sector':        sector,
                        'reason':        sig['reason'],
                        'is_mega_trend': True,
                        'tp_pct':        dyn_tp,
                        'sl_pct':        dyn_sl,
                    })
                    logger.info(
                        f"    [S10] BUY {name}({ticker}) "
                        f"size={size_pct:.1%} conf={sig['confidence']:.2f} (TP:{dyn_tp*100:.1f}%, SL:{dyn_sl*100:.1f}%)"
                    )
                elif sig['action'] == 'exit':
                    signals.append({
                        'ticker':    ticker,
                        'name':      name,
                        'direction': 'exit',
                        'strategy':  'S10_MegaTrend',
                        'reason':    sig['reason'],
                    })

        logger.info(f"  [S10] 신호 {len(signals)}건 생성 완료")
        return signals

    # ──────────────────────────────────────────────────────────────────────

    def _select_top_sectors(
        self,
        market_data: Dict[str, Any],
        regime: str,
        top_n: int,
    ) -> List[str]:
        """시장 데이터를 직접 분석하여 동적으로 주도 섹터 선택.
        각 섹터별 대장주의 외국인 수급 및 모멘텀을 합산하여 자체 스코어링.
        """
        features = market_data.get('features', {})
        sector_scores = {}

        for sector, stocks in self._sector_map.items():
            sector_total_score = 0.0
            valid_stocks = 0
            
            for stock in stocks:
                ticker = stock['ticker']
                name = stock['name']
                weight = float(stock.get('weight', 0.5))
                f_data = features.get(ticker, features.get(f'kr_{ticker}', {}))
                
                # 자체 평가 로직 재활용
                sig = self._evaluate_stock(ticker=ticker, name=name, f_data=f_data, regime=regime)
                
                # sig['score']를 가져와서 가중 합산
                score = sig.get('score', 0.0)
                sector_total_score += (score * weight)
                if score > 0:
                    valid_stocks += 1
            
            # 유효한 점수가 하나라도 있으면 섹터 점수로 등록
            if valid_stocks > 0:
                sector_scores[sector] = sector_total_score

        if not sector_scores:
            logger.warning("  [S10] 모든 섹터 점수가 0이거나 데이터가 없습니다. 기본 섹터(방산)로 Fallback.")
            return [str(cfg.get('s10.default_sector', 'defense'))]

        sorted_sectors = sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)
        top_sectors = [s for s, _ in sorted_sectors][:top_n]
        
        logger.debug(f"  [S10] 동적 섹터 스코어링 결과: {sorted_sectors}")
        return top_sectors

    # ──────────────────────────────────────────────────────────────────────

    def _evaluate_stock(
        self,
        ticker: str,
        name: str,
        f_data: Any,
        regime: str,
    ) -> Dict:
        """종목별 복합 신호 평가.

        실증 ICIR 가중 합산:
          flow_foreign_streak  (ICIR=0.305) → 45%
          flow_foreign_net_buy (ICIR=0.229) → 35%
          flow_foreign_accel   (ICIR=0.219) → 20%
        + MA120 보너스/패널티
        × HMM 레짐 멀티플라이어
        """
        import pandas as pd
        import math

        price = ma120 = 0.0
        streak = net_buy = accel = 0.0

        streak_col  = str(cfg.get('s10.feature_streak',  'flow_foreign_streak'))
        netbuy_col  = str(cfg.get('s10.feature_net_buy', 'flow_foreign_net_buy'))
        accel_col   = str(cfg.get('s10.feature_accel',   'flow_foreign_accel'))
        ma_window   = int(cfg.get('s10.ma_window', 120))

        if isinstance(f_data, pd.DataFrame) and not f_data.empty:
            if 'close' in f_data.columns:
                price = float(f_data['close'].iloc[-1])
                if len(f_data) >= ma_window:
                    ma120 = float(f_data['close'].rolling(ma_window).mean().iloc[-1])
                else:
                    ma120 = float(f_data['close'].mean())
            streak  = float(f_data[streak_col].iloc[-1])  if streak_col  in f_data.columns else 0.0
            net_buy = float(f_data[netbuy_col].iloc[-1]) if netbuy_col in f_data.columns else 0.0
            accel   = float(f_data[accel_col].iloc[-1])  if accel_col  in f_data.columns else 0.0

        elif isinstance(f_data, dict):
            price   = float(f_data.get('close', 0))
            ma120   = float(f_data.get('ma_120', price))
            streak  = float(f_data.get(streak_col,  0))
            net_buy = float(f_data.get(netbuy_col, 0))
            accel   = float(f_data.get(accel_col,   0))

        if price <= 0:
            return {'action': 'hold', 'confidence': 0.0, 'reason': '가격 없음'}

        # 소프트 정규화 (tanh)
        def tnorm(x: float, scale: float = 1.0) -> float:
            return math.tanh(x / max(abs(scale), 1e-6))

        w_s = float(cfg.get('s10.weight_streak',  0.45))
        w_n = float(cfg.get('s10.weight_net_buy', 0.35))
        w_a = float(cfg.get('s10.weight_accel',   0.20))

        foreign_score = (
            w_s * tnorm(streak,  1.0)  +
            w_n * tnorm(net_buy, 0.1)  +
            w_a * tnorm(accel,   0.05)
        )

        # MA120 보너스
        ma120_bonus = 0.0
        if ma120 > 0:
            gap = (price - ma120) / ma120
            bonus_thr  = float(cfg.get('s10.ma120_bonus_threshold', 0.0))
            exit_gap   = float(cfg.get('s10.ma120_exit_gap', 0.05))
            if gap > bonus_thr:
                ma120_bonus = float(cfg.get('s10.ma120_bonus', 0.15))
            elif gap < -exit_gap:
                ma120_bonus = float(cfg.get('s10.ma120_penalty', -0.10))

        # 레짐 멀티플라이어
        regime_mult = {
            'bull':           float(cfg.get('s10.regime_mult_bull',    1.20)),
            'neutral':        float(cfg.get('s10.regime_mult_neutral',  1.00)),
            'caution':        float(cfg.get('s10.regime_mult_caution',  0.80)),
            'bear':           float(cfg.get('s10.regime_mult_bear',     0.50)),
            'crash':          float(cfg.get('s10.regime_mult_crash',    0.00)),
            'momentum_surge': float(cfg.get('s10.regime_mult_bull',    1.20)),
        }.get(str(regime).lower(), 1.0)

        total = (foreign_score + ma120_bonus) * regime_mult
        confidence = float(np.clip(total, 0.0, 1.0))

        entry_thr = float(cfg.get('s10.entry_score_threshold', 0.10))
        exit_thr  = float(cfg.get('s10.exit_score_threshold', -0.05))

        reasons = []
        if tnorm(streak,  1.0) > 0.2:  reasons.append(f"외국인연속매수(streak={streak:.2f})")
        if tnorm(net_buy, 0.1) > 0.1:  reasons.append(f"외국인순매수({net_buy:.3f})")
        if tnorm(accel,   0.05)> 0.1:  reasons.append(f"매수가속({accel:.3f})")
        if ma120_bonus > 0:             reasons.append(f"MA{ma_window}돌파")
        if ma120_bonus < 0:             reasons.append(f"MA{ma_window}하회")

        reason_str = ', '.join(reasons) if reasons else f'복합점수={total:.3f}'

        if total >= entry_thr:
            return {'action': 'buy',  'confidence': confidence, 'score': round(total, 4), 'reason': reason_str}
        elif total <= exit_thr:
            return {'action': 'exit', 'confidence': 1.0 - confidence, 'score': round(total, 4), 'reason': f'신호 약화({total:.3f})'}
        else:
            return {'action': 'hold', 'confidence': confidence, 'score': round(total, 4), 'reason': f'관망({total:.3f})'}

    # ──────────────────────────────────────────────────────────────────────

    def _compute_kelly(self, sig: Dict) -> float:
        """Kelly Criterion 포지션 사이징."""
        p  = float(sig.get('confidence', 0.5))
        tp = float(cfg.get('s10.default_tp_pct', 0.30))
        sl = float(cfg.get('s10.default_sl_pct', 0.10))
        b  = tp / max(sl, 1e-4)
        q  = 1.0 - p
        kelly_f = float(np.clip((p * b - q) / max(b, 1e-4), 0, 1))
        return round(kelly_f * float(cfg.get('s10.kelly_fraction', 0.25)), 4)

    def _exit_all_signal(self, reason: str) -> List[Dict]:
        """모든 섹터 대장주 청산 신호."""
        return [
            {'ticker': s['ticker'], 'name': s['name'],
             'direction': 'exit', 'strategy': 'S10_MegaTrend', 'reason': reason}
            for stocks in self._sector_map.values()
            for s in stocks
        ]

    def get_performance(self) -> Dict:
        return {'sharpe': 0.0, 'win_rate': 0.5, 'total_return': 0.0, 'score': 1.0}

    def get_positions(self) -> List[Dict]:
        return []

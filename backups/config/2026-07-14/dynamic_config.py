"""
Project Meridian — Dynamic Configuration (SSoT)
================================================
모든 파라미터의 Single Source of Truth.
하드코딩 Zero: 모든 값은 이 파일에서만 정의하고,
results/dynamic_overrides.json으로 런타임 오버라이드 가능.

Project First 기반 + Meridian 4-Stream 확장 키 추가.

Usage:
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()
    sl_pct = cfg.get('exit.stop_loss_multiplier')  # ATR 배수
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_OVERRIDES_FILE = _PROJECT_ROOT / 'results' / 'dynamic_overrides.json'


# ═══════════════════════════════════════════════════════
# 기본값 정의 (하드코딩 대신 여기서 중앙 관리)
# ═══════════════════════════════════════════════════════

_DEFAULTS: Dict[str, Any] = {
    # ── 유니버스 및 타겟 설정 (Due Diligence V4 동적화) ──
    # [Task 6] 하드코딩 완전 제거: ParameterOptimizer 및 동적 스크리너를 통해 런타임에 결정됨.
    'universe.target_sectors': {},
    'universe.target_themes': {},
    'universe.fallback_tickers': {},
    's4.value_up_tickers': [],
    's2.drift_window': 20,
    's2.drift_threshold': 0.15,

    # ── ★ Adaptive Threshold & Medallion EWMA (신규) ──
    'adaptive.ewma_fast_span': 20,
    'adaptive.ewma_med_span': 60,
    'adaptive.ewma_slow_span': 120,
    'adaptive.ewma_weights': [0.2, 0.5, 0.3],
    'adaptive.z_score_extreme': 1.5,
    'adaptive.percentile_extreme': 90.0,
    'data.ffill_limit': 3,
    'data.max_fetch_limit': 1000,
    'data.max_nan_ratio': 0.8,
    'data.max_missing_ratio': 0.5,
    
    # ── 동적 고도화 (PIT & Async Queue) ──
    'data.use_async_queue': True,             # 비동기 데이터 큐 사용 (asyncio)
    'data.pit_snapshot_freq': 'D',            # Point-in-time 스냅샷 주기 (D: Daily)
    'data.pit_keep_days': 30,                 # 스냅샷 보관 기간 (일)

    # ── 레짐 (Regime) ──
    'regime.vix_bull_threshold': 18.0,
    'regime.vix_caution_threshold': 25.0,
    'regime.vix_bear_threshold': 35.0,
    'regime.transition_smoothing_days': 3,
    'regime.confidence_min': 0.4,

    # 레짐 스코어 가중치 (합계 = 1.0)
    'regime.weight_vix': 0.4,               # VIX 기반 가중치
    'regime.weight_trend': 0.3,             # KOSPI 추세 기반 가중치
    'regime.weight_volatility': 0.3,        # 변동성 기반 가중치

    # 추세 판정 임계값
    'regime.trend_strong_up_dist': 2.0,     # MA20 대비 +2% 이상 → strong bull
    'regime.trend_strong_down_dist': -5.0,  # MA20 대비 -5% 이하 → crash 후보

    # 변동성 임계값
    'regime.vol_bull_threshold': 12.0,      # 12% 미만 → bull
    'regime.vol_caution_threshold': 20.0,   # 20% 미만 → caution
    'regime.vol_bear_threshold': 30.0,      # 30% 미만 → bear, 이상 → crash

    # 동적 매크로 합성 + SSoT 통합 가중치
    'regime.weight_macro_composite': 0.20,  # 동적 매크로 합성 점수 가중치
    'regime.weight_fx_daily': 0.08,          # 일간 환율 변동 가중치
    'regime.weight_vkospi': 0.05,            # VKOSPI 가중치

    # ── 청산 (Exit) ──
    'exit.atr_period': 20,
    'exit.sl_atr_multiplier': 2.0,          # SL = -ATR × 이 값
    'exit.tp_atr_multiplier': 3.5,          # ★ 3.0→3.5 (TP 확대 → 건당 수익 극대화)
    'exit.trailing_atr_multiplier': 2.0,    # Trail = -ATR × 이 값
    'exit.min_tp_sl_ratio': 3.0,            # ★ 2.5→3.0 (W/L 비율 강화)
    'exit.roundtrip_cost_pct': 0.36,        # 실제 왕복 비용
    'exit.tp_cap_bull': 18.0,               # ★ 15→18% BULL TP 상한 (추세 활용)
    'exit.tp_cap_other': 12.0,              # ★ 10→12% 기타 레짐 TP 상한
    'exit.sl_floor': -8.0,                  # SL 하한 (%)
    'exit.partial_tp_ratio': 0.5,           # Partial TP 시 매도 비율

    # ── 레짐별 TP/SL 승수 (★ 비대칭 강화) ──
    'exit.regime_tp_mult.bull': 1.5,        # ★ 1.3→1.5 (추세 활용)
    'exit.regime_tp_mult.caution': 1.0,
    'exit.regime_tp_mult.bear': 0.7,        # ★ 0.8→0.7 (빠른 이익 실현)
    'exit.regime_tp_mult.crash': 0.5,       # ★ 0.6→0.5
    'exit.regime_sl_mult.bull': 1.2,        # ★ 1.0→1.2 (넓은 SL, 조기 청산 방지)
    'exit.regime_sl_mult.caution': 1.0,
    'exit.regime_sl_mult.bear': 0.7,        # ★ 0.8→0.7 (타이트 SL)
    'exit.regime_sl_mult.crash': 0.5,       # ★ 0.6→0.5

    # ── 레짐별 보유기간 (일) ──
    'exit.max_hold_days.bull': 10,
    'exit.max_hold_days.caution': 7,
    'exit.max_hold_days.bear': 5,
    'exit.max_hold_days.crash': 3,

    # ── 포지션 사이징 ──
    'sizer.method': 'kelly',
    'sizer.kelly_fraction': 0.25,           # 켈리의 25% (Half Kelly)
    'sizer.max_single_position_pct': 0.15,  # 개별 종목 최대 15%
    'sizer.max_total_invested_pct': 0.90,   # 전체 투자 비율 최대
    'sizer.slippage_bps': 7.5,              # 편도 슬리피지 (bps)

    # ── 레짐별 투자 비율 ──
    'sizer.regime_invest_ratio.bull': 0.90,
    'sizer.regime_invest_ratio.caution': 0.75,  # ★ 0.65→0.75: 활용률 상향
    'sizer.regime_invest_ratio.bear': 0.30,
    'sizer.regime_invest_ratio.crash': 0.10,

    # ── 슬리브 A 배분 (레짐별) ──
    # 형식: [A1_방향성, A2_섹터, A3_알파, 채권금, 현금]
    'sleeve_a.allocation.bull':     [0.35, 0.30, 0.30, 0.00, 0.05],
    'sleeve_a.allocation.caution':  [0.25, 0.25, 0.25, 0.10, 0.15],
    'sleeve_a.allocation.bear':     [0.15, 0.10, 0.15, 0.30, 0.30],
    'sleeve_a.allocation.crash':    [0.10, 0.00, 0.00, 0.40, 0.50],

    # ── A1 방향성 ETF ──
    'a1.entry_score_high': 0.25,
    'a1.entry_score_mid': 0.15,
    'a1.entry_score_low': 0.10,
    'a1.force_close_time': '15:10',
    'a1.swing_max_days': 3,
    'a1.max_pyramid_entries': 3,
    'a1.pyramid_profit_threshold_pct': 0.5,
    'a1.multi_etf_enabled': True,            # 멀티-ETF 동시 매매
    'a1.max_concurrent_etfs': 3,             # 최대 동시 ETF 수
    'a1.sector_etf_enabled': True,           # 강세 섹터 ETF 추가
    'a1.global_etf_enabled': True,           # 글로벌 ETF 추가

    # ── A1 야간 갭 트레이딩 ──
    'a1.gap_trading_enabled': True,
    'a1.gap_min_us_change_pct': 1.0,         # US +1%↑ 시 갭업 매매
    'a1.gap_hold_minutes': 120,              # 갭 매매 최대 보유 시간
    'a1.gap_take_profit_pct': 1.5,           # 갭 TP
    'a1.gap_stop_loss_pct': -0.7,            # 갭 SL
    'a1.gap_max_allocation_pct': 0.10,       # 전체 대비 최대 10%

    # ── A1 조건부 레버리지 ──
    'a1.leverage_enabled': True,
    'a1.leverage_conditions': {
        'min_ois': 65,                       # OIS ≥ 65
        'min_consecutive_wins': 5,           # 연속 5일 양수
        'max_mdd_pct': -2.0,                 # MDD < -2%
        'regime': 'bull',                    # 레짐 = bull만
    },
    'a1.leverage_etf_ticker': '122630',      # KODEX 레버리지
    'a1.leverage_scale': 1.3,                # 포지션 1.3x

    # ── A1 Feature Weights (하드코딩 제거 — 동적 관리) ──
    'a1.fw.vix_low_threshold': 15,
    'a1.fw.vix_low_bonus': 0.08,
    'a1.fw.vix_high_threshold': 30,
    'a1.fw.vix_high_penalty': -0.10,
    'a1.fw.trend_up_bonus': 0.07,
    'a1.fw.trend_down_penalty': -0.07,
    'a1.fw.ma20_scale': 0.01,
    'a1.fw.ma20_cap': 0.08,
    'a1.fw.regime_bonus': {'bull': 0.05, 'caution': 0, 'bear': -0.05, 'crash': -0.10},
    'a1.fw.sector_scale': 0.1,
    'a1.fw.sector_baseline': 0.5,
    'a1.fw.ois_strong_threshold': 70,
    'a1.fw.ois_strong_bonus': 0.06,
    'a1.fw.ois_mild_threshold': 60,
    'a1.fw.ois_mild_bonus': 0.03,
    'a1.fw.ois_weak_threshold': 40,
    'a1.fw.ois_weak_penalty': -0.03,
    'a1.fw.ois_strong_bear_threshold': 30,
    'a1.fw.ois_strong_bear_penalty': -0.06,
    'a1.fw.foreign_flow_threshold': 500,     # 억 단위
    'a1.fw.foreign_flow_bonus': 0.04,
    'a1.fw.vkospi_low_threshold': 15,
    'a1.fw.vkospi_low_bonus': 0.03,
    'a1.fw.vkospi_mid_threshold': 25,
    'a1.fw.vkospi_mid_penalty': -0.03,
    'a1.fw.vkospi_high_threshold': 30,
    'a1.fw.vkospi_high_penalty': -0.05,
    'a1.fw.event_min_scale': 0.5,
    'a1.fw.event_adj_scale': 0.01,
    'a1.fw.shock_severe_penalty': -0.08,
    'a1.fw.shock_moderate_penalty': -0.04,

    # ── A2 섹터 로테이션 ──
    'a2.rebalance_day': 1,                  # 매월 첫 거래일
    'a2.top_n_sectors': 3,
    'a2.momentum_weights': [0.5, 0.3, 0.2], # 1M, 3M, 6M 가중치
    'a2.max_sector_weight': 0.15,
    'a2.emergency_rebalance_on_regime_change': True,

    # ── A3 알파 종목 ──
    'a3.min_up_probability': 0.60,           # 0.65→0.60 (거래빈도↑)
    'a3.max_positions': 12,                  # 8→12 (분산↑, 빈도↑)
    'a3.min_sector_percentile': 50,
    'a3.hold_period_days': 7,                # 10→7 (회전율↑)
    'a3.use_factor_integrator': True,        # 팩터 통합기 사용
    'a3.ensemble_enabled': True,             # 3모델 앙상블
    'a3.min_cost_adjusted_edge': 0.006,      # 최소 비용 차감 후 엣지 0.6%

    # ── 신호 품질 강화 (건당 수익률 극대화) ──
    'a3.kelly_ev_min_pct': 0.50,             # ★ 0.30→0.50% (더 높은 EV 요구)
    'a3.roundtrip_cost_pct': 0.36,           # 왕복 비용 (슬리피지+수수료+세금)
    'a3.limit_order_enabled': True,          # 지정가 매수 활성화
    'a3.limit_order_offset_pct': 0.25,       # ★ 0.20→0.25% 오프셋 (슬리피지 절감)
    'a3.limit_order_delay_min': 5,           # 장 시작 후 5분 대기
    'a3.limit_order_fill_rate': 0.75,        # ★ 0.80→0.75 (더 보수적 체결률)

    # ── 거래빈도 최적화 (대수의 법칙 유지) ──
    'a3.min_trade_amount': 50_000,          # 소액 라이브: 최소 거래금액 5만원
    'a3.min_hold_days': 2,                   # ★ 신규: 최소 보유일 2일 (스캘핑 방지)
    'a3.max_monthly_trades': 20,             # ★ 신규: 월간 최대 20건 (과도 회전 방지)
    'a3.min_annual_trades': 60,              # ★ 신규: 연간 최소 60건 (대수의 법칙)

    # ── 학습 Target 개선 ──
    'train.target_type': 'max_return',       # 'close_return' | 'max_return'
    'train.max_return_days': 5,              # 5일 내 최대 수익
    'train.positive_threshold_pct': 3.0,     # ≥ 3.0% (양성 ~43%)
    'train.wf_acc_retrain_threshold': 0.55,  # ★ WF 평균 ACC < 이 값이면 자동 재학습

    # ── A3 레짐별 파라미터 ──
    'a3.regime.bull.max_positions': 15,
    'a3.regime.bull.max_hold_days': 5,
    'a3.regime.bull.min_up_prob': 0.58,
    'a3.regime.caution.max_positions': 10,
    'a3.regime.caution.max_hold_days': 7,
    'a3.regime.caution.min_up_prob': 0.62,
    'a3.regime.bear.max_positions': 5,
    'a3.regime.bear.max_hold_days': 3,
    'a3.regime.bear.min_up_prob': 0.70,
    'a3.regime.crash.max_positions': 0,
    'a3.regime.crash.max_hold_days': 0,
    'a3.regime.crash.min_up_prob': 1.0,

    # ── A3 Fallback 가중치 (ML 모델 없을 때 규칙 기반 스코어링) ──
    # SelfLearning이 IC 기반으로 자동 조정
    'a3.fb.base_score': 0.50,               # 기본 점수
    'a3.fb.rsi_oversold_threshold': 30,     # RSI 과매도 기준
    'a3.fb.rsi_oversold_bonus': 0.10,       # 과매도 보너스
    'a3.fb.rsi_overbought_threshold': 70,   # RSI 과매수 기준
    'a3.fb.rsi_overbought_penalty': -0.05,  # 과매수 패널티
    'a3.fb.bb_low_threshold': 0.20,         # BB 하단 기준
    'a3.fb.bb_low_bonus': 0.08,             # BB 하단 보너스
    'a3.fb.bb_high_threshold': 0.80,        # BB 상단 기준
    'a3.fb.bb_high_penalty': -0.03,         # BB 상단 패널티
    'a3.fb.macd_scale': 2.0,               # MACD 신호 스케일
    'a3.fb.volume_spike_threshold': 2.0,    # 거래량 폭증 기준 (배수)
    'a3.fb.volume_spike_bonus': 0.05,       # 거래량 폭증 보너스
    'a3.fb.momentum_scale': 0.005,          # 5일 모멘텀 스케일

    # ── 펀더멘탈 필터 (하드코딩 제거 — 동적 관리) ──
    'fundamental.min_qv_score': 30.0,
    'fundamental.min_garp_score': 40.0,
    'fundamental.min_fscore': 4,
    'fundamental.bear_min_qv': 40.0,
    'fundamental.bear_min_fscore': 5,
    'fundamental.bull_qv_relax': 10,
    'fundamental.bull_fscore_relax': 1,
    'fundamental.value_leg_min_fscore': 5,
    'fundamental.barbell_weights': {'bull': 0.40, 'caution': 0.50, 'bear': 0.65, 'crash': 0.75},
    'fundamental.fscore_sizing_adj': {8: 1.20, 7: 1.15, 5: 1.00, 3: 0.85, 0: 0.70},

    # ── Beneish M-Score 필터 (이익 조작 탐지) ──
    'fundamental.beneish_enabled': True,
    'fundamental.beneish_threshold': -1.78,       # M > -1.78 → RISKY
    'fundamental.beneish_grey_margin': 0.5,       # -2.28 ~ -1.78 → GREY
    'fundamental.beneish_regime_strict': {         # 레짐별 엄격도
        'bull': False, 'caution': True, 'bear': True, 'crash': True,
    },

    # ── 부도위험/자본잠식 필터 ──
    'fundamental.min_equity_ratio': 0.10,         # 자기자본비율 최소 10%
    'fundamental.max_debt_ratio': 5.0,            # 부채비율 최대 500%
    'fundamental.capital_erosion_block': True,     # 자본잠식 시 즉시 차단

    # ── ROE/OPM 하한 필터 ──
    'fundamental.min_roe': 0.0,                   # ROE 최소 0% (적자 배제)
    'fundamental.min_opm': 0.0,                   # OPM 최소 0% (영업적자 배제)
    'fundamental.bear_min_roe': 3.0,              # BEAR 시 ROE 최소 3%
    'fundamental.bear_min_opm': 2.0,              # BEAR 시 OPM 최소 2%

    # ── 레짐별 동적 펀더멘탈 기준 확장 ──
    'fundamental.regime_criteria': {
        'bull':    {'min_qv': 20, 'min_fscore': 3, 'min_garp': 30, 'min_roe': 0.0, 'min_opm': 0.0},
        'caution': {'min_qv': 30, 'min_fscore': 4, 'min_garp': 40, 'min_roe': 0.0, 'min_opm': 0.0},
        'bear':    {'min_qv': 40, 'min_fscore': 5, 'min_garp': 50, 'min_roe': 3.0, 'min_opm': 2.0},
        'crash':   {'min_qv': 50, 'min_fscore': 6, 'min_garp': 60, 'min_roe': 5.0, 'min_opm': 3.0},
    },

    # ── L4 벤치마크: Ohlson O-Score 부도위험 필터 ──
    'fundamental.oscore_enabled': True,
    'fundamental.oscore_threshold_high': 0.80,    # P > 0.80 → 즉시 제외
    'fundamental.oscore_threshold_moderate': 0.65, # P > 0.65 → FLAG
    'fundamental.oscore_regime_strict': {          # 레짐별 엄격도
        'bull': False, 'caution': False, 'bear': True, 'crash': True,
    },

    # ── L4 벤치마크: Short Proxy 공매도 리스크 ──
    'fundamental.short_proxy_enabled': True,
    'fundamental.short_proxy_block_threshold': 0.80,   # ≥ 0.80 → 차단
    'fundamental.short_proxy_penalty_threshold': 0.50,  # ≥ 0.50 → 스코어 차감

    # ── L4 벤치마크: Magic Formula + GP/A (B4용) ──
    'fundamental.mf_enabled': True,                     # MF 필터 활성화
    'fundamental.mf_min_score': 0.40,                   # MF Score 최소 (0~1)
    'fundamental.gpa_min': 0.10,                        # GP/A 최소 10%
    'fundamental.mf_regime_criteria': {                  # 레짐별 MF/GP/A
        'bull':    {'mf_min': 0.35, 'gpa_min': 0.08},
        'caution': {'mf_min': 0.40, 'gpa_min': 0.10},
        'bear':    {'mf_min': 0.50, 'gpa_min': 0.15},
        'crash':   {'mf_min': 0.60, 'gpa_min': 0.20},
    },

    # ── L4 벤치마크: Earnings Surprise (A3 촉매) ──
    'fundamental.earnings_surprise_enabled': True,
    'fundamental.earnings_surprise_bonus_scale': 0.03,   # ±3% 가산

    # ── L4 벤치마크: Insider 매수 (B4용) ──
    'fundamental.insider_bonus': 0.02,                   # 내부자 매수 +2%

    # ── L4 벤치마크: Point-in-Time 재무 보정 ──
    'fundamental.pit_correction_enabled': True,
    'fundamental.pit_delay_days': 45,                    # 결산일 + 45일

    # ── L4 벤치마크: B4 바벨 배분 (레짐별 Value/Growth 비중) ──
    'fundamental.barbell_vg_ratio': {
        'bull':    {'value': 0.40, 'growth': 0.60},
        'caution': {'value': 0.50, 'growth': 0.50},
        'bear':    {'value': 0.65, 'growth': 0.35},
        'crash':   {'value': 0.75, 'growth': 0.25},
    },

    # ── L4 벤치마크: DRIP 복리 추정 (B1용) ──
    'fundamental.drip_growth_rates': {
        'GLOBAL': 0.10, 'SECTOR': 0.08, 'KR_ETF': 0.06,
        'BOND': 0.03, 'SAFE': 0.035, 'INDIVIDUAL': 0.07,
        # Phase 2: 미국 ETF 타입별 성장률
        'US_DIV': 0.10, 'US_GROWTH': 0.12, 'US_SECTOR': 0.09,
    },

    # ── 슬리브 B 배분 (레짐별) ──
    # 형식: [주식, 채권, 금, 현금]
    'sleeve_b.allocation.bull':     [0.70, 0.15, 0.05, 0.10],
    'sleeve_b.allocation.caution':  [0.50, 0.25, 0.10, 0.15],
    'sleeve_b.allocation.bear':     [0.25, 0.35, 0.15, 0.25],
    'sleeve_b.allocation.crash':    [0.10, 0.30, 0.15, 0.45],

    # ── 슬리브 B 계좌별 ──
    'sleeve_b.isa.tax_free_limit': 2_000_000,
    'sleeve_b.isa.tax_rate_excess': 0.099,
    'sleeve_b.pension.tax_credit_limit': 6_000_000,
    'sleeve_b.pension.risk_asset_limit': 0.70,
    'sleeve_b.irp.risk_asset_limit': 0.30,
    'sleeve_b.rebalance_frequency': 'monthly',

    # ── B1 ISA 고배당 개별주 혼합 ──
    'sleeve_b.isa.individual_stock_enabled': True,
    'sleeve_b.isa.individual_stock_ratio': 0.30,  # ISA 자산의 30%를 개별주
    'sleeve_b.isa.qv_top_n': 5,                   # QV 상위 5종목
    'sleeve_b.isa.min_dividend_yield': 2.0,       # 최소 배당수익률 2%

    # ── B4 종합계좌 바벨 ──
    'sleeve_b.brokerage.rebalance_months': [1, 4, 7, 10],  # 분기 리밸런싱
    'sleeve_b.brokerage.value_n': 5,              # Value Leg 종목 수
    'sleeve_b.brokerage.growth_n': 5,             # Growth Leg 종목 수

    # ── 리스크 관리 ──
    'risk.sleeve_dd_limit': -0.08,
    'risk.total_dd_limit': -0.10,
    'risk.sleeve_dd_reduce_ratio': 0.50,
    'risk.correlation_target': 0.30,
    'risk.crash_cash_ratio': 0.80,
    'risk.max_sector_concentration': 0.25,   # 섹터 집중도 25% 한도
    'risk.max_position_correlation': 0.70,   # 포지션 간 상관 0.7 한도
    'risk.position_correlation_lookback': 60, # 상관 계산 60일

    # ── VaR 모니터링 (★ 포지션 차단 용도 아님) ──
    'risk.var_confidence': 0.95,              # VaR 신뢰수준
    'risk.var_lookback': 120,                 # VaR 계산 lookback (일)
    'risk.ewma_lambda': 0.94,                 # EWMA λ (RiskMetrics 표준)

    # ── σ-target 변동성 타겟팅 (★ 퀀트 펀드 표준) ──
    # ExposureOrchestrator가 사용: exposure = σ_target / σ_realized
    # 한국 개별종목 중심 전략: KOSPI 개별종목 평균 연변동 25~35%
    'risk.sigma_target_annual': 0.20,         # 목표 연변동성 20% (KR 주식 전략용)
    'risk.sigma_lookback': 60,                # σ 계산 lookback (일)
    'risk.sigma_scale_floor': 0.4,            # σ 스케일 하한 (최소 40% 노출)
    'risk.sigma_scale_cap': 1.3,              # σ 스케일 상한 (최대 130% 노출)

    # ── 동적 VaR 한도 (모니터링용) ──
    # limit = (σ_target/√252) × z_α × buffer × scale_factor
    'risk.var_limit_buffer_multiplier': 1.5,  # VaR 한도 버퍼 배수
    'risk.var_limit_max_scale': 3.0,          # 고변동 시 최대 확대 배수
    'risk.var_limit_floor_pct': 1.5,          # VaR 한도 절대 하한 (%)
    'risk.var_limit_ceiling_pct': 10.0,       # VaR 한도 절대 상한 (%)
    
    # ── 동적 고도화 (Risk Deadlock & Joint Matrix) ──
    'risk.deadlock_resolution_mode': 'joint_prob', # 다중 레이어 중복 발동 시 해결 방식 ('linear', 'joint_prob')
    'risk.max_combined_hedge_ratio': 0.80,         # 결합된 최대 헤지/현금화 비율 한도

    # ── 전체 자산 ──
    'portfolio.total_capital': 1_500_000,       # 총 자본 (투자+예비, 150만)
    'portfolio.initial_capital': 1000000.0,     # AUM 100만 (소액 라이브 기준)
    'portfolio.reserve_capital': 500_000,       # 예비금 (비상용)
    'portfolio.sleeve_a_ratio': 0.60,           # 투자자본의 60%
    'portfolio.sleeve_b_ratio': 0.40,           # 투자자본의 40%
    'portfolio.reserve_ratio': 0.33,            # 총자본 대비 33%
    'portfolio.min_signal_confidence': 0.60,     # ★ 전체 최소 신호 confidence
    'portfolio.stop_loss_pct': -5.0,             # ★ 종목별 손절 기준 (%)
    'portfolio.target_cash_ratio': 0.18,          # ★ 목표 현금 비중 18% (15-20% 범위)
    'portfolio.max_cash_ratio': 0.25,             # ★ 현금 비중 상한 (초과 시 투자 전환)
    'portfolio.min_cash_ratio': 0.12,             # ★ 현금 비중 하한 (미달 시 매도)
    'portfolio.cash_deploy_streams': ['S2', 'S3'],  # ★ 추가 펀드 배분 대상 (S4=API 미연결, S1=당일청산)

    # ── ★ Exit System 개선 (동적 매도 규칙) ──
    'portfolio.take_profit_pct': 15.0,           # 기본 이익실현 기준 (%) — 스트림별 오버라이드 없으면 사용
    'portfolio.trailing_stop_enabled': True,      # Trailing Stop 활성화
    'portfolio.trailing_activate_pct': 5.0,       # 기본 Trailing 활성화 기준 (%) — 스트림별 오버라이드 없으면 사용
    'portfolio.trailing_stop_pct': -3.0,          # 기본 고점 대비 하락 매도 기준 (%) — 스트림별 오버라이드 없으면 사용
    'portfolio.adaptive_sl_enabled': True,        # ATR 기반 적응형 손절
    'portfolio.adaptive_sl_atr_mult': 2.0,        # ATR 배수 (손절 = -ATR * mult)
    'portfolio.adaptive_sl_floor': -3.0,          # 적응형 손절 하한 (%)
    'portfolio.adaptive_sl_ceiling': -10.0,       # 적응형 손절 상한 (%)
    'portfolio.time_decay_enabled': True,         # 보유기간 기반 매도
    'portfolio.max_holding_days': 20,             # 최대 보유 거래일
    'portfolio.time_decay_start_day': 10,         # Time Decay 시작일
    'portfolio.time_decay_rate': 0.05,            # 일당 Confidence 감소율

    # ── ★ 장중 SL 모니터링 (Intraday Stop-Loss Check) ──
    'portfolio.absolute_max_sl_pct': -10.0,       # 절대 최대 손절 기준 (%) — 이 이하 시 즉시 청산
    'portfolio.intraday_sl_check_enabled': True,  # 장중 SL 체크 활성화 (11:00, 13:30 실행)

    # ── ★ Task #10: 상관관계 인식 사이징 ──
    'portfolio.correlation_penalty': 0.3,            # 상관 할인율 (1 - corr × penalty)
    'portfolio.correlation_threshold': 0.50,         # 상관 할인 적용 임계값
    'portfolio.max_correlated_exposure': 0.60,       # 상관 스트림 합산 최대 노출 (자본 대비)

    # ── ★ 스트림별 Take Profit (동적 — ATR + 기본값 혼합) ──
    # 공식: effective_tp = max(stream_tp_floor, ATR * stream_tp_atr_mult)
    # S1: 당일 청산 → 자체 exit 사용 (s1.ev.take_profit_pct)
    'exit.stream_tp_enabled': True,               # 스트림별 TP 분기 활성화
    's2.take_profit_pct': 20.0,                   # S2 ML Alpha: 높은 TP (ML 예측 큰 움직임)
    's2.take_profit_atr_mult': 4.0,               # S2 ATR 배수 (변동성 대비 TP)
    's2.take_profit_floor': 10.0,                 # S2 최소 TP (%)
    's2.take_profit_ceiling': 35.0,               # S2 최대 TP (%)
    's3.take_profit_pct': 12.0,                   # S3 Factor/Sector ETF: 중간 TP
    's3.take_profit_atr_mult': 3.0,               # S3 ATR 배수
    's3.take_profit_floor': 8.0,                  # S3 최소 TP (%)
    's3.take_profit_ceiling': 25.0,               # S3 최대 TP (%)
    's4.take_profit_pct': 10.0,                   # S4 Advisory: 낮은 TP (절세계좌, 안정적)
    's4.take_profit_atr_mult': 2.5,               # S4 ATR 배수
    's4.take_profit_floor': 7.0,                  # S4 최소 TP (%)
    's4.take_profit_ceiling': 20.0,               # S4 최대 TP (%)

    # ── ★ ATR 기반 Trailing Stop (동적) ──
    # 공식: trailing_activate = ATR * trail_activate_atr_mult
    #       trailing_stop    = -(ATR * trail_stop_atr_mult)
    'exit.trailing_atr_enabled': True,            # ATR 기반 Trailing 활성화
    'exit.trail_activate_atr_mult': 2.0,          # Trailing 활성화 = ATR × 2.0 (예: ATR 3% → +6%에서 활성)
    'exit.trail_stop_atr_mult': 1.5,              # Trailing 하락폭 = ATR × 1.5 (예: ATR 3% → 고점 -4.5%에서 매도)
    'exit.trail_activate_floor': 3.0,             # 최소 활성화 기준 (%)
    'exit.trail_activate_ceiling': 15.0,          # 최대 활성화 기준 (%)
    'exit.trail_stop_floor': -2.0,                # 최소 하락폭 (타이트)
    'exit.trail_stop_ceiling': -8.0,              # 최대 하락폭 (와이드)
    's4.min_hold_days': 20,                      # ★ S4 최소 보유기간 (거래일)
    'portfolio.target_annual_return': 0.25,

    # ── S4 수동매매 전용 설정 ──
    's4.manual_only': True,
    #   - True:  S4는 수동매매 전용 — 자동 매수/매도/리밸런싱 신호 생성 완전 차단
    #            KIS API를 통한 주문 전송 없음
    #   - False: S4 자동매매 활성화 (비권장, 세금 불이익 가능)
    's4.price_collection_enabled': True,
    #   - S4 보유 종목의 현재가·시가평가(MTM)는 항상 수행
    #   - 대시보드 포지션 P&L 표시, NAV 계산에 활용
    's4.auto_exit_enabled': False,
    #   - False: SL/TP/Trailing 등 자동 청산 로직도 S4에 미적용
    #   - True:  자동 청산만 허용 (매수는 여전히 수동)

    # ── ★ Consensus 수집 범위 ──
    's4.consensus_qvm_top_n': 50,                # QVM Top N 종목 수집
    's4.consensus_market_top_n': 30,             # 시총 Top N 종목 수집
    's4.consensus_sell_lookback_days': 14,        # 최근 매도 N일 이내 종목 수집
    's4.consensus_collect_delay': 0.5,            # 종목간 수집 간격 (초, 네이버 차단 방지)


    # ── Go Decision 임계값 (중앙 관리) ──
    'go.sharpe.ok': 1.0,             # Sharpe ≥ 1.0 → OK
    'go.sharpe.caution': 0.0,        # Sharpe ≥ 0 → Caution, < 0 → Poor
    'go.sharpe.target': 0.50,        # Go 기준 목표치
    'go.win_rate.ok': 0.50,          # WR ≥ 50% → OK
    'go.win_rate.caution': 0.30,     # WR ≥ 30% → Caution
    'go.max_dd.safe': -5.0,          # DD > -5% → Safe
    'go.max_dd.caution': -8.0,       # DD > -8% → Caution, ≤ -8% → Poor
    'go.da.ok': 0.55,                # DA ≥ 55% → OK
    'go.da.caution': 0.45,           # DA ≥ 45% → Caution
    'go.ic.ok': 0.05,                # IC > 0.05 → OK
    'go.ic.caution': 0.0,            # IC > 0 → Caution, ≤ 0 → Negative
    'go.alpha.ok': 0.0,              # Alpha > 0% → OK
    'go.alpha.caution': -5.0,        # Alpha > -5% → Caution
    'go.profit_factor.ok': 1.5,      # PF ≥ 1.5 → Good
    'go.profit_factor.caution': 1.0, # PF ≥ 1.0 → Caution
    'go.nav_return.ok': 0.0,         # NAV Return > 0% → OK
    'go.nav_return.caution': -3.0,   # > -3% → Caution
    'go.drift.ok': 0,                # 0 drifted → Clean
    'go.drift.caution': 2,           # ≤ 2 → Caution
    'go.sortino.target': 1.0,         # Sortino ≥ 1.0 → Go 기준
    'go.calmar.target': 0.5,          # Calmar ≥ 0.5 → Go 기준
    'go.beta.target': 1.0,            # Beta ≤ 1.0 → Go 기준

    # ── Calibrator 설정 ──
    'calibrator.min_samples_platt': 30,      # Platt sigmoid 학습 최소 샘플
    'calibrator.min_samples_update': 10,     # 업데이트 최소 샘플
    'calibrator.bucket_edges': [0.50, 0.60, 0.70, 0.80],  # 5-bucket 경계

    # ── Beta Hedge 설정 ──
    'beta_hedge.min_beta_monitor': 0.3,      # beta < 0.3 → MONITOR
    'beta_hedge.benchmark_ticker': '069500', # KODEX 200
    'beta_hedge.min_days': 20,               # 최소 데이터 일수 (Alpha 표시용)

    # ── ★ 포트폴리오 β 헤지 (ExposureOrchestrator) ──
    'hedge.enabled': True,                           # β 헤지 활성화
    'hedge.base_beta.bull': 0.75,                    # Bull 레짐 기본 β (동적 조정 시작점)
    'hedge.base_beta.caution': 0.55,                 # Caution 레짐 기본 β
    'hedge.base_beta.bear': 0.35,                    # Bear 레짐 기본 β
    'hedge.base_beta.crash': 0.15,                   # Crash 레짐 기본 β
    'hedge.vix_neutral': 18.0,                       # VIX 중립선 (이상이면 β 축소)
    'hedge.vix_beta_scale': 0.005,                   # VIX 1pt당 β 변동 스케일
    'hedge.dd_beta_threshold': -3.0,                 # 드로다운 β 조정 시작 기준 (%)
    'hedge.dd_beta_scale': 0.05,                     # 드로다운 1%당 β 변동 스케일
    'hedge.vol_neutral': 0.015,                      # 포트폴리오 일변동성 중립선 (1.5%)
    'hedge.vol_beta_scale': 2.0,                     # 변동성 초과분당 β 변동 스케일
    'hedge.beta_floor': 0.10,                        # 동적 β 하한
    'hedge.beta_ceiling': 0.90,                      # 동적 β 상한
    'hedge.max_hedge_ratio': 0.5,                    # 최대 헤지 비율 (롱 대비)
    'hedge.min_amount': 100_000,                     # 소액 라이브: 최소 헤지 10만
    'hedge.use_2x_regime': ['bear', 'crash'],        # 2X 인버스 사용 레짐
    'hedge.rebalance_tolerance': 0.1,                # 리밸런싱 허용 오차 (10%)
    'hedge.vol_lookback_days': 20,                   # 포트폴리오 변동성 계산 lookback (거래일)

    # ── Feature Audit 설정 ──
    'feature_audit.low_importance_pct': 0.80,  # 하위 20%
    'feature_audit.psi_threshold': 0.10,       # PSI 주의 기준
    'feature_audit.noise_ratio': 0.50,         # 전체 평균 대비 50% 미만 → 노이즈

    # ── Kill Switch ──
    'killswitch.account_daily_loss_pct': -2.0,
    'killswitch.sleeve_a_daily_limit_pct': -1.5,
    'killswitch.max_consecutive_losses': 3,
    'killswitch.drawdown_warning_pct': -3.0,
    'killswitch.drawdown_reduce_pct': -5.0,
    'killswitch.drawdown_liquidate_pct': -8.0,
        # [Phase 60] MRI & Risk-Parity Benchmark
        'regime.mri_decay_k':          0.8,
        'regime.mri_vixy_weight':      0.5,
        'regime.mri_uup_weight':       0.3,
        'regime.mri_ief_weight':       0.2,
        'regime.mri_ma_window':        30,
        'regime.mri_usdkrw_baseline':  1300.0,
        'regime.mri_usdkrw_std':       50.0,
        'regime.mri_us10y_baseline':   4.0,
        'regime.mri_us10y_std':        0.5,
        'defense.min_factor':          0.001,
        'benchmark.vol_floor':         0.005,
        'benchmark.assets':            'spy,tlt,gld,uso',
        'benchmark.window':            60,

    'killswitch.cooldown_hours': 24,
    'gonogo.system_killswitch': False,
    'gonogo.max_drawdown_limit': 0.15,
    'gonogo.minimum_liquidity_krw': 10000000,
    'gonogo.volatility_halt_vix': 35.0,

    # ── ★ 월별 동적 손실 한도 (Kill Switch 트리거 5) ──
    # 한도 = -(월간 변동성 × σ 승수), 변동성은 EWMA 실시간 계산
    'risk.monthly_avg_trading_days': 22,       # 월 평균 거래일 수
    'risk.monthly_loss_sigma_mult': 2.0,       # σ 승수 (높을수록 한도 넓음)
    'risk.monthly_loss_floor': -0.15,          # 절대 하한 -15%
    'risk.monthly_loss_ceiling': -0.02,        # 절대 상한 -2%

    # ── ★ 집중 리스크 (포지션 간 상관관계 → 노출 스케일) ──
    # 직교성 점수 기반 동적 노출 축소
    'risk.corr_lookback': 60,                  # 상관 계산 lookback (일)
    'risk.corr_high_threshold': 0.7,           # 고상관 경고 임계값
    'risk.corr_ortho_good': 0.7,               # 분산 양호 직교성
    'risk.corr_ortho_bad': 0.3,                # 집중 위험 직교성
    'risk.corr_scale_min': 0.5,                # 최소 노출 스케일

    # ── 비용 최적화 ──
    'cost.commission_bps': 1.5,              # 수수료 0.015%
    'cost.slippage_bps': 7.5,                # 슬리피지 0.075%
    'cost.tax_sell_pct': 0.18,               # 매도세 0.18%
    'cost.roundtrip_total_bps': 54,          # 왕복 총비용 0.54%
    'cost.min_expected_return_pct': 0.6,     # 최소 기대수익 > 비용

    # ── 자가학습 ──
    'learning.ic_min_significance': 0.10,
    'learning.weight_decay_factor': 0.95,   # 비효과적 신호 감쇠
    'learning.retrain_frequency_days': 7,   # 주 1회 재학습 (한국 시장 특성)
    'learning.retrain_cooldown_days': 2,    # 이벤트 트리거 쿨다운
    'learning.retrain_window_days': 730,    # Rolling Window 2년
    'learning.retrain_da_failure_threshold': 0.45,  # DA < 45% 5일 연속 시
    'learning.retrain_da_failure_days': 5,  # 연속 실패일 수
    'learning.retrain_vix_spike_pct': 5.0,  # VIX 5%p+ 변동
    'learning.retrain_market_extreme_pct': 10.0,  # KOSPI 20일 |수익률| > 10%
    'learning.review_frequency_days': 90,
    'learning.min_samples_for_adjustment': 20,

    # ── 측정 (Measurement — ★ 퀀트 펀드 표준) ──
    'measurement.da_min_signals': 5,          # DA 유효 최소 신호 수
    'measurement.ic_min_positions': 5,        # IC 유효 최소 포지션 수
    'measurement.sharpe_min_days': 5,         # Sharpe 유효 최소 일수
    'measurement.benchmark_ticker': '069500', # 벤치마크 ETF (KODEX 200)
    'measurement.rolling_window': 20,         # 롤링 지표 기간 (거래일)
    'measurement.ic_method': 'spearman',      # IC 계산 방법 (spearman/pearson)
    'measurement.ic_exclude_fixed_conf': True,      # 고정 confidence 종목 IC 제외
    'measurement.trade_require_price': True,          # ★ 진단 개선: trade 기록 시 price 필수
    'measurement.trade_require_quantity': True,        # ★ 진단 개선: trade 기록 시 quantity 필수
    'measurement.ic_negative_auto_scale': True,        # ★ 진단 개선: IC 음수 시 해당 스트림 자동 축소
    'measurement.ic_negative_scale_factor': 0.5,       # ★ IC 음수 시 position_scale 50%
    'measurement.da_auto_scale_threshold': 0.40,       # ★ DA < 40% 시 자동 축소
    'measurement.da_auto_scale_factor': 0.6,           # ★ DA 미달 시 position_scale 60%
    'measurement.ic_fixed_conf_threshold': 0.01,     # confidence 차이 < 1%면 고정으로 판정
    'measurement.ic_min_days': 20,                   # IC 유의성 판정 최소 거래일
    'go.alpha.min_days': 20,                         # Alpha 판정 최소 거래일

    # ── ★ 스트림별 보유기간 인식 측정 (Holding-Period-Aware Metrics) ──
    # 각 스트림의 전략 특성에 맞는 평가 기간/연환산 설정
    # alpha = (스트림 수익률 - 벤치마크 수익률) / 벤치마크 수익률 × 100
    # 연환산 = (1 + period_return)^(annualize_factor/eval_window) - 1
    'measurement.stream_profile': {
        'S1': {
            'expected_holding_days': 1,        # 당일 청산
            'alpha_eval_window': 5,            # 최근 5거래일 rolling alpha
            'alpha_min_days': 3,               # alpha 유효 최소 3일 (빈번한 거래)
            'sharpe_min_days': 5,              # Sharpe 유효 최소 5일
            'annualize_factor': 252,           # 일별 → 연률
            'benchmark': '069500',             # KODEX 200
            'alpha_method': 'cumulative_excess',  # 초과수익률 누적
        },
        'S2': {
            'expected_holding_days': 7,        # 1~2주 보유
            'alpha_eval_window': 20,           # 최근 20거래일 rolling alpha
            'alpha_min_days': 10,              # alpha 유효 최소 10일
            'sharpe_min_days': 10,             # Sharpe 유효 최소 10일
            'annualize_factor': 252,           # 일별 → 연률
            'benchmark': '069500',             # KODEX 200
            'alpha_method': 'cumulative_excess',
        },
        'S3': {
            'expected_holding_days': 60,        # 1~3개월 보유 (섹터 로테이션)
            'alpha_eval_window': 60,            # 최근 60거래일 rolling alpha
            'alpha_min_days': 20,               # alpha 유효 최소 20일
            'sharpe_min_days': 20,              # Sharpe 유효 최소 20일
            'annualize_factor': 252,            # 일별 → 연률
            'benchmark': '069500',              # KODEX 200
            'alpha_method': 'annualized_excess',  # 연환산 초과수익
        },
        'S4': {
            'expected_holding_days': 120,       # 3~6개월 보유 (절세 장기)
            'alpha_eval_window': 120,           # 최근 120거래일 rolling alpha
            'alpha_min_days': 20,               # alpha 유효 최소 20일
            'sharpe_min_days': 20,              # Sharpe 유효 최소 20일
            'annualize_factor': 252,            # 일별 → 연률
            'benchmark': '069500',              # KODEX 200
            'alpha_method': 'annualized_excess',  # 연환산 초과수익
        },
    },

    # ── Go/No-Go ──
    'gonogo.shadow_min_days': 0,  # ★ 소액 라이브 즉시 가동을 위해 0으로 오버라이드
    'gonogo.a1_min_trades': 5,
    'gonogo.a3_da_threshold': 0.52,
    'gonogo.a3_alpha_threshold': 0.0,
    'gonogo.active_streams': ['S1', 'S2', 'S3', 'S4'],  # [Phase 52: S6-A, S6-B 제거 완료]
    'gonogo.win_rate_threshold': 0.45,        # ★ Go/No-Go 승률 기준
    'gonogo.da_threshold': 0.52,              # ★ DA 기준 (기존 a3_da_threshold와 통합)
    'gonogo.da_min_n': 10,                    # ★ DA 최소 표본 수
    'gonogo.ic_min_days': 20,                 # ★ IC 시계열 최소 거래일
    'gonogo.ic_p_threshold': 0.10,            # ★ IC p-value 기준
    'gonogo.total_return_floor': -3.0,        # ★ Meridian 전체 수익률 하한 (%)
    'gonogo.mdd_threshold': -10.0,            # Meridian MDD 한도 (%)
    'gonogo.s1_min_trades': 3,               # S1 최소 거래 수 (Go/No-Go)
    'gonogo.s2_min_trades': 3,               # S2 최소 거래 수 (Go/No-Go)
    'gonogo.s3_min_trades': 1,               # S3 최소 거래 수 (월 1회 리밸런싱)
    'gonogo.s4_min_trades': 1,               # S4 최소 거래 수 (월 1회 리밸런싱)
    # beta_hedge.min_days는 L492에서 정의 (중복 제거)
    'calibrator.min_samples': 30,       # 신뢰 가능 최소 샘플 수
    'feature_audit.interval_days': 1,   # Feature Audit 실행 간격 (일)

    # ── 시장 시간 ──
    'market.kr_open': '09:00',
    'market.kr_close': '15:30',
    'market.kr_force_close': '15:10',


    # ── Crash Defense (신규) ──
    'crash.vix_threshold': 30,
    'crash.kospi_5d_drop': -0.10,
    'crash.sl_circuit_limit': 3,
    'crash.inverse_ticker': '114800',

    # ── Drawdown Guard (신규) ──
    'dd_guard.stage1_pct': -0.05,
    'dd_guard.stage1_exp': 0.80,
    'dd_guard.stage2_pct': -0.10,
    'dd_guard.stage2_exp': 0.50,
    'dd_guard.stage3_pct': -0.15,
    'dd_guard.stage3_exp': 0.30,
    'dd_guard.stage4_pct': -0.20,
    'dd_guard.stage4_exp': 0.10,
    'dd_guard.stage5_pct': -0.25,
    'dd_guard.stage5_exp': 0.00,

    # ── Black-Litterman (신규) ──
    'bl.risk_aversion': 2.5,
    'bl.tau': 0.05,
    'bl.max_single_weight': 0.30,

    # ── TWAP/VWAP Execution (신규) ──
    'exec.default_strategy': 'twap',
    'exec.n_slices': 5,
    'exec.min_order_value': 100_000,

    # ── Medallion Orchestrator (신규) ──
    'medallion.max_sector_count': 2,
    'medallion.max_single_pct': 0.15,
    'medallion.min_sectors': 3,

    # ── Drift Guard (신규) ──
    'drift.psi_warning': 0.10,
    'drift.psi_critical': 0.25,
    'drift.sparse_zero_pct': 0.40,             # 참조에서 0비율 ≥40% → 희소 피처
    'drift.sparse_psi_mult': 3.0,              # 희소 피처 PSI 임계값 배율 (3x 관대)

    # ── Conformal Predictor (신규) ──
    'conformal.target_coverage': 0.90,
    'conformal.width_high': 0.15,       # width < 0.15 → high confidence
    'conformal.width_low': 0.30,        # width < 0.30 → medium, else low

    # ── Exposure Orchestrator 가중치 (신규) ──
    'exposure.w_regime': 0.30,
    'exposure.w_vix': 0.25,
    'exposure.w_fng': 0.25,
    'exposure.w_vkospi': 0.10,
    'exposure.w_trend': 0.10,

    # ═══════════════════════════════════════════════════════
    # ★ MERIDIAN 확장 키 (4-Stream Architecture)
    # ═══════════════════════════════════════════════════════

    # ── 매매 시간 (프리마켓~애프터마켓 확장) ──
    'market.premarket_open': '08:00',
    'market.aftermarket_close': '20:00',

    # ── Stream 배분 (AlphaAllocator) ──
    'allocator.s1_base_weight': 0.25,
    'allocator.s2_base_weight': 0.30,
    'allocator.s3_base_weight': 0.20,
    'allocator.s4_base_weight': 0.25,
    'allocator.active_streams': ['S0', 'S1', 'S2', 'S3'],  # S6-A, S6-B, S9, S10 등 잉여 스트림 완전 제거
    'allocator.sharpe_exponent': 2.0,
    'allocator.correlation_penalty': 0.10,
    'allocator.rebalance_threshold': 0.05,

    # ── 스트림별 고정 투자 예산 (가상거래용) ──
    # S1: Edge/방향성 | S2: ML Alpha | S3: Factor/섹터 | S4: 절세계좌
    'allocation.s1_budget': 200_000,       # ₩20만 (당일 단타)
    'allocation.s2_budget': 500_000,       # ₩50만 (ML 메인)
    'allocation.s3_budget': 300_000,       # ₩30만 (섹터/팩터)
    'allocation.s6a_budget': 0,            # [Phase 52] S6-A 미사용
    'allocation.s6b_budget': 0,            # [Phase 52] S6-B 미사용
    'allocation.s4_isa_budget': 0,         # 소액 라이브 시 ISA 생략
    'allocation.s4_pension_budget': 0,     # 개인연금 생략
    'allocation.s4_irp_budget': 0,         # IRP 생략
    'allocation.hedge_budget': 200_000,    # ₩20만 — β 헷지 전용
    'allocation.s1_min_trade': 100_000,    # S1 최소 거래 금액 (10만)

    # ── ★ 분할 진입 (Phased Entry) ──
    # 일괄 구축 방지: 1일 최대 진입 종목 수 제한
    # S1은 당일 청산이므로 제한 없음
    'allocation.phased_entry.s2_max_daily': 3,   # S2: 일 최대 3종목 신규 진입
    'allocation.phased_entry.s3_max_daily': 2,   # S3: 일 최대 2종목 신규 진입
    'allocation.phased_entry.s4_max_daily': 8,   # S4: 일 최대 8종목 신규 진입
    'allocation.phased_entry.enabled': True,      # 분할 진입 활성화

    # ── ★ #1: 신뢰도 기반 포지션 사이징 ──
    'allocation.conf_sizing.high_threshold': 0.70,   # 고신뢰 기준
    'allocation.conf_sizing.low_threshold': 0.60,    # 저신뢰 기준
    'allocation.conf_sizing.high_mult': 1.5,         # 고신뢰(≥70%): 1.5배
    'allocation.conf_sizing.mid_mult': 1.0,          # 중신뢰(60-70%): 1.0배
    'allocation.conf_sizing.low_mult': 0.6,          # 저신뢰(<60%): 0.6배

    # ── ★ #3: 스트림 성과 기반 신규 자본 가중 ──
    'allocation.stream_perf_weighting': True,         # 성과 가중 활성화
    'allocation.perf_mult_max': 1.5,                  # 성과 가중 최대 배수
    'allocation.perf_mult_min': 0.5,                  # 성과 가중 최소 배수
    'allocation.perf_mult_scale': 10.0,               # 성과 가중 스케일 (PnL / scale)

    # ── 레버리지 (측정 기반 동적 판정) ──
    'leverage.enabled': True,
    'leverage.2x_min_sharpe': 1.5,
    'leverage.2x_min_confidence': 0.70,
    'leverage.2x_min_consecutive_wins': 5,
    'leverage.2x_max_var_pct': 1.5,
    'leverage.3x_min_sharpe': 2.0,
    'leverage.3x_max_mdd_pct': -3.0,
    'leverage.3x_max_correlation': 0.30,
    'leverage.etf_2x': '122630',
    'leverage.etf_3x': '233740',
    'leverage.inverse_max_pct': 0.20,
    'leverage.inverse_ticker': '114800',
    'leverage.inverse_2x_ticker': '252670',
    # ── S0 Directional Beta ──
    's0_beta.leverage_ticker': '122630',       # KODEX 레버리지
    's0_beta.inverse2x_ticker': '252670',      # KODEX 200선물인버스2X
    's0_beta.bull_prob_threshold': 0.60,       # 강세장 레버리지 진입 임계값
    's0_beta.crash_prob_threshold': 0.15,      # 폭락장 인버스 진입 임계값
    's0_beta.max_confidence_threshold': 0.90,  # Alpha 현금화 발동 고확신 임계값
    's0_beta.target_leverage_weight': 1.0,     # 베타 몰빵 시 목표 비중

    # ── S1 Edge 프리마켓 ──
    's1.gap_entry_start': '08:00',
    's1.force_close_time': '15:10',
    's1.aftermarket_rebalance': '17:00',
    's1.futures.enabled': False,
    's1.futures.margin_ratio': 0.15,
    's1.futures.max_contracts': 5,
    's1.futures.rollover_days_before': 3,

    # ── S1 Edge 비용 최적화 (ETF 전용) ──
    's1.etf_sell_commission_rate': 0.00015,  # ETF 매도: 위탁수수료만 (증권거래세 면제)
    's1.max_daily_trades': 5,               # ★ 거래 빈도 확대 (ETF 비용 우위, 대수의 법칙)
    's1.min_ev_after_cost': 0.002,          # ★ 백테스트/라이브 유연화를 위해 완화: 0.4% → 0.2%
    's1.roundtrip_cost_pct': 0.0018,        # ETF 왕복 비용 (수수료 0.03% + 슬리피지 0.15%)
    'sizer.snr_threshold': 0.2,             # ★ 노이즈 대비 알파 임계값 (0.5 → 0.2 완화)
    's1.ev.take_profit_pct': 0.015,         # EV 필터 기본 TP +1.5%
    's1.ev.stop_loss_pct': -0.010,          # EV 필터 기본 SL -1.0%

    # ── S1 합류(Confluence) 기반 동적 TP / Trailing Stop ──
    # 레버리지 & 인버스 양방향 적용
    # confluence_level 1: 고정 TP만 (기존과 동일)
    # confluence_level 2: 기본 TP 도달 시 Trailing Stop 활성화
    # confluence_level 3: 조기 Trailing 활성화 (기본 TP 전에 전환)
    's1.confluence.tp_pct': {1: 0.015, 2: 0.015, 3: 0.015},       # TP 고정 (trailing으로 대체)
    's1.confluence.trailing_activate': {1: None, 2: 0.015, 3: 0.010},  # trailing 전환 시점
    's1.confluence.trailing_stop': {1: None, 2: -0.005, 3: -0.005},    # HWM 대비 trailing 폭
    's1.confluence.ev_boost': {1: 0.0, 2: 0.003, 3: 0.005},       # EV 필터 confidence 보정

    # ── S4 절세계좌 (ISA/IRP/개인연금) ──
    's4.isa.covered_call_ratio': 0.20,
    's4.isa.drip_reinvest': True,
    's4.irp.annual_contribution': 7_000_000,
    's4.irp.dca_monthly': 583_333,
    's4.irp.risk_asset_limit': 0.30,
    's4.pension.annual_contribution': 6_000_000,
    's4.pension.dca_monthly': 500_000,
    's4.pension.risk_asset_limit': 0.70,
    's4.tax.isa_tax_free_limit': 2_000_000,
    's4.tax.total_tax_credit_target': 2_145_000,
    's4.max_bond_ratio': 0.30,                 # ★ 진단 개선: 채권 비중 30% 상한
    's4.quality_factor_enabled': True,          # ★ 진단 개선: Quality factor 추가 (ROE/이익성장)
    's4.tax.harvest_loss_enabled': True,

    # ── ★ S4 계좌별 투자 한도 (Advisory 자금 관리) ──
    # 각 계좌의 총 투자 가능 자본 (현재 보유 + 신규 매수 합산 한도)
    's4.account_capital.ISA': 20_000_000,       # ISA 총 투자 한도 ₩2,000만
    's4.account_capital.IRP': 22_000_000,       # IRP 총 투자 한도 ₩2,200만
    's4.account_capital.PENSION': 7_000_000,    # 개인연금 총 투자 한도 ₩700만
    's4.account_capital.BROKERAGE': 15_000_000, # 종합계좌 총 투자 한도 ₩1,500만
    # 계좌별 종목당 최대 비중
    's4.max_stock_weight.ISA': 0.25,            # ISA: 종목당 최대 25%
    's4.max_stock_weight.IRP': 0.35,            # IRP: 종목당 최대 35% (채권 집중 허용)
    's4.max_stock_weight.PENSION': 0.30,        # 연금: 종목당 최대 30%
    's4.max_stock_weight.BROKERAGE': 0.15,      # 종합: 종목당 최대 15% (분산 강화)
    # 계좌별 최대 보유 종목 수
    's4.max_holdings.ISA': 8,                   # ISA: 최대 8종목
    's4.max_holdings.IRP': 5,                   # IRP: 최대 5종목
    's4.max_holdings.PENSION': 6,               # 연금: 최대 6종목
    's4.max_holdings.BROKERAGE': 8,             # 종합: 최대 8종목

    # ── 미국 ETF 동적 관리 ──
    's4.us.max_allocation_pct': 0.40,
    's4.us.fx_hedge_enabled': True,
    's4.us.fx_unhedged_limit': 0.15,
    's4.us.rebalance_on_fx_move': 5.0,

    # ── 옵션 오버레이 (Phase 3) ──
    'option.protective_put_enabled': False,
    'option.put_trigger_dd_pct': -0.05,
    'option.put_otm_pct': 0.05,
    'option.covered_call_enabled': False,
    'option.call_otm_pct': 0.03,

    # ── Fallback 동적 업데이트 ──
    'fallback.auto_update_enabled': True,
    'fallback.update_frequency_days': 7,
    'fallback.safety_bounds_enabled': True,

    # ── DD Guard 6단계 (확장) ──
    'dd_guard.stage6_pct': -0.30,
    'dd_guard.stage6_exp': 0.00,
    'dd_guard.recovery_step': 0.05,

    # ═══════════════════════════════════════════════════════
    # ★ MEDALLION UPGRADE — 하드코딩 제거 + 동적 파라미터
    # ═══════════════════════════════════════════════════════

    # ── 공통: 연환산 팩터 ──
    'common.annualization_factor': 252,        # 영업일 기반 연환산

    # ── S1 Edge: 시그모이드 Confidence (#1) ──
    's1.confidence.sigmoid_k.crash': 1.5,      # 하락장 시그모이드 기울기
    's1.confidence.sigmoid_k.bear': 1.5,
    's1.confidence.sigmoid_k.caution': 2.0,
    's1.confidence.sigmoid_k.bull': 2.5,
    's1.confidence.sigmoid_threshold.crash': 1.5,  # 하락장 진입 임계값(%)
    's1.confidence.sigmoid_threshold.bear': 1.5,
    's1.confidence.sigmoid_threshold.caution': 1.0,
    's1.confidence.sigmoid_threshold.bull': 0.8,
    's1.confidence.vix_adjustment_base': 20.0, # VIX 보정 기준선
    's1.confidence.vix_adjustment_scale': 30.0,# VIX 보정 스케일
    's1.confidence.volume_boost_threshold': 1.5,  # 거래량 부스트 발동 배수
    's1.confidence.volume_boost_scale': 0.05,     # 거래량 부스트 스케일
    's1.confidence.volume_boost_cap': 1.1,        # 거래량 부스트 상한
    's1.confidence.max_cap': 0.95,                # 신뢰도 상한
    's1.confidence.min_floor': 0.15,              # ★ 신뢰도 하한 (0.05→0.15)

    # ── S1 Edge: 레짐별 Confidence 할인 (#1) ──
    's1.confidence.regime_discount.crash': 0.50,
    's1.confidence.regime_discount.bear': 0.70,
    's1.confidence.regime_discount.caution': 1.00,
    's1.confidence.regime_discount.bull': 1.10,

    # ── S1 Edge: Cost-Aware EV (#2) ──
    's1.cost.slippage_rate': 0.0008,           # 편도 슬리피지
    's1.cost.commission_rate': 0.000070,        # 편도 수수료
    's1.cost.min_ev_threshold': 0.0005,         # EV 최소 임계값 (0.002→0.0005: Shadow 학습 완화)

    # ── [Phase 47] ATR-Adaptive EV Threshold (Volatility-Adjusted EV Hurdle) ──
    # 정적 min_ev 폐기 → S1 ETF 유니버스 ATR 퍼센타일 기반 동적 허들
    # 수식: min_ev = transaction_cost × cost_mult
    # cost_mult 결정:
    #   Low Vol  (ETF 유니버스 ATR < p30): cost_mult = ev.cost_mult_low_vol  (적극 포획)
    #   Mid Vol  (p30 ~ p70):             cost_mult = ev.cost_mult_mid_vol   (균형)
    #   High Vol (ATR > p70):             cost_mult = ev.cost_mult_high_vol  (크래시/휩소 방어)
    #   + VIX High Bonus: VIX > ev.vix_high_threshold → ×ev.vix_high_extra_mult
    's1.ev.cost_mult_low_vol':    1.3,   # Low Vol (ATR < p30): cost × 1.3 (미세 알파 포획)
    's1.ev.cost_mult_mid_vol':    2.5,   # Mid Vol (p30~p70):   cost × 2.5 (균형)
    's1.ev.cost_mult_high_vol':   4.5,   # High Vol (ATR > p70): cost × 4.5 (크래시 방어)
    's1.ev.vix_high_threshold':  25.0,   # VIX 고변동 판정 기준 (이 이상이면 추가 보정)
    's1.ev.vix_high_extra_mult':  1.5,   # VIX 고변동 시 cost_mult 추가 배수 (고위험 강화)
    's1.ev.atr_low_percentile':  30.0,   # 저변동 기준 percentile (S1 ETF 유니버스 내)
    's1.ev.atr_high_percentile': 70.0,   # 고변동 기준 percentile (S1 ETF 유니버스 내)
    's1.ev.fallback_mult':        2.5,   # ATR 계산 실패 시 Fallback 배수 (mid_vol과 동일)
    's1.ev.atr_default_pct':      1.5,   # signal에 atr_pct 없을 때 기본값 (%)
    's1.cost.default_tp_pct': 1.5,             # 기본 TP (%)
    's1.cost.default_sl_pct': 0.7,             # 기본 SL (절대값 %)

    # ── S1 Edge: 복합 변동성 (#3) ──
    's1.vol.vix_default': 18.0,                # VIX 기본값
    's1.vol.vix_norm_min': 10.0,               # VIX 정규화 하한
    's1.vol.vix_norm_range': 40.0,             # VIX 정규화 범위
    's1.vol.vkospi_default': 15.0,             # VKOSPI 기본값
    's1.vol.vkospi_norm_min': 10.0,            # VKOSPI 정규화 하한
    's1.vol.vkospi_norm_range': 30.0,          # VKOSPI 정규화 범위
    's1.vol.intraday_extreme_pct': 4.0,        # 극단적 일중 변동 (%)
    's1.vol.weight_vix': 0.40,                 # 복합 변동성 VIX 가중치
    's1.vol.weight_vkospi': 0.40,              # 복합 변동성 VKOSPI 가중치
    's1.vol.weight_intraday': 0.20,            # 복합 변동성 일중 가중치

    # ── S1 Edge: 갭 트레이딩 반도체 ──
    's1.gap.sox_min_change': 1.0,              # SOX 갭 최소 변동 (%)
    's1.gap.semi_confidence_cap': 0.85,        # 반도체 갭 신뢰도 상한
    's1.gap.semi_confidence_scale': 4.0,       # 반도체 갭 스케일
    's1.gap.semi_alloc_ratio': 0.25,           # 반도체 갭 배분 비율
    's1.gap.semi_tp_pct': 2.0,                 # 반도체 갭 TP
    's1.gap.semi_sl_pct': -1.0,                # 반도체 갭 SL
    's1.gap.semi_hold_minutes': 180,           # 반도체 갭 최대 보유 분

    # ── S1 Edge: 방향성 매매 ──
    's1.directional.size_base_ratio': 0.25,    # ★ 포지션 기본 비율 (0.20→0.25, 방향성 강화)
    's1.directional.vol_damping': 0.50,        # 변동성 감쇠 계수
    's1.directional.leverage_confidence': 0.60, # 2X 레버리지 전환 신뢰도
    's1.directional.min_confidence': 0.55,      # ★ 방향성 최소 confidence (0.40→0.55)
    's1.directional.max_confidence': 0.95,      # ★ 방향성 최대 confidence (0.85→0.95)

    # ── S1 Edge: 단일종목 레버리지 ──
    's1.single_stock.base_score': 0.50,        # 기본 점수
    's1.single_stock.rsi_oversold': 30,        # RSI 과매도
    's1.single_stock.rsi_oversold_bonus': 0.15,
    's1.single_stock.rsi_overbought': 70,      # RSI 과매수
    's1.single_stock.rsi_overbought_penalty': -0.10,
    's1.single_stock.macd_scale': 2.0,         # MACD 스케일
    's1.single_stock.macd_cap': 0.15,          # MACD 보너스 상한
    's1.single_stock.volume_threshold': 2.0,   # 거래량 급증 기준
    's1.single_stock.volume_bonus': 0.10,      # 거래량 보너스
    's1.single_stock.sox_scale': 5.0,          # SOX 스케일
    's1.single_stock.sox_cap': 0.15,           # SOX 보너스 상한
    's1.single_stock.regime_adj.crash': -0.20, # 레짐별 점수 조정
    's1.single_stock.regime_adj.bear': -0.10,
    's1.single_stock.regime_adj.caution': 0.00,
    's1.single_stock.regime_adj.bull': 0.05,
    's1.single_stock.long_confidence_scale': 3.0,  # 롱 신뢰도 배수
    's1.single_stock.long_confidence_cap': 0.85,
    's1.single_stock.inv_confidence_scale': 3.0,   # 인버스 신뢰도 배수
    's1.single_stock.inv_confidence_cap': 0.80,
    's1.single_stock.min_score': 0.60,         # 롱 최소 점수 (fallback)
    's1.single_stock.max_inv_score': 0.35,     # 인버스 최대 점수 (fallback)
    's1.single_stock_max_alloc': 0.08,         # 단일종목 레버리지 최대 배분
    's1.single_stock_inv_max_alloc': 0.06,     # 단일종목 인버스 최대 배분

    # ── S1 Edge: 레버리지 기초자산 수익률 원리 ──
    # MACD 가격 정규화: macd_hist / close * 100 → % 기반 비교
    's1.single_stock.macd_normalize_by_price': True,   # 가격 정규화 활성화
    's1.single_stock.macd_pct_scale': 0.025,           # 정규화 MACD 1%당 스코어 기여
    's1.single_stock.macd_pct_cap': 0.20,              # 정규화 MACD 스코어 상한

    # ── S1 DA Improvement: KOSPI 교차검증 강화 (#1, #5) ──
    's1.directional.kospi_conflict_penalty': 0.75,     # ★ DA#1: 충돌 감점 강화 (0.50→0.75)
    's1.directional.kospi_conflict_block': True,       # ★ DA#1: 충돌 시 시그널 차단 (감점 대신 차단)
    's1.directional.kospi_conflict_threshold': 0.1,    # KOSPI 변동률 충돌 판정 기준 (%)
    's1.directional.kospi_align_bonus': 0.10,          # KOSPI-OIS 방향 일치 보너스
    's1.directional.deadband': 0.05,                   # ★ DA#4: OIS 중립 구간 확대 (0.005→0.05, OIS 45~55 중립)
    's1.directional.ois_neutral': 0.50,                # OIS 중립점 (0~1 스케일)
    's1.directional.min_directional_confidence': 0.55, # ★ DA#2: 최소 confidence 상향 (0.40→0.55)

    # ── S1 DA Improvement: KOSPI 모멘텀 1순위 (#5, #6) ──
    's1.directional.kospi_primary': True,              # ★ DA#5: KOSPI 장중 모멘텀을 1순위로
    's1.directional.kospi_min_move_pct': 0.3,          # KOSPI 최소 방향 확인 변동 (%)
    's1.directional.kospi_momentum_weight': 0.60,      # KOSPI 모멘텀 가중치 (OIS보다 높음)
    's1.directional.ois_weight': 0.40,                 # OIS 가중치 (보조)
    's1.directional.multi_tf_enabled': True,           # ★ DA#6: 멀티 타임프레임 확인
    's1.directional.multi_tf_lookback_min': 30,        # 멀티TF 확인 기간 (분)

    # ── S1 DA Improvement: 디커플링 감지 (#7) ──
    's1.directional.decoupling_enabled': True,         # ★ DA#7: 미국-한국 디커플링 감지
    's1.directional.decoupling_sp500_threshold': 0.5,  # SP500 변동 판정 기준 (%)
    's1.directional.decoupling_kospi_threshold': 0.3,  # KOSPI 변동 판정 기준 (%)

    # ── S1 DA Improvement: 인트라데이 재진입 제한 (#8) ──
    's1.directional.intraday_reentry_block': True,     # ★ DA#8: 같은 날 같은 방향 손절 후 재진입 금지
    's1.cooldown_same_direction_hours': 24,             # 같은 방향 재진입 쿨다운 (시간)

    # ── S1 DA Improvement: 국내 전용 갭 ML (#9) ──
    's1.domestic_gap_ml_enabled': True,                 # ★ DA#9: 국내 데이터 only 갭 예측
    's1.domestic_gap.kospi_weight': 0.35,               # KOSPI 변동 가중치
    's1.domestic_gap.vkospi_weight': 0.25,              # VKOSPI 가중치
    's1.domestic_gap.foreign_flow_weight': 0.25,        # 외인 수급 가중치
    's1.domestic_gap.prev_pattern_weight': 0.15,        # 전일 패턴 가중치

    # ── S1 DA Improvement: DA 피드백 루프 (#10) ──
    's1.da_feedback.enabled': True,                     # ★ DA#10: DA 피드백 루프 활성화
    's1.da_feedback.lookback_trades': 10,               # DA 계산 최근 N건
    's1.da_feedback.scale_threshold': 0.40,             # DA < 40% → 포지션 축소
    's1.da_feedback.scale_factor': 0.50,                # 축소 비율 (50%)
    's1.da_feedback.disable_threshold': 0.30,           # DA < 30% → 일시 비활성화
    's1.da_feedback.recovery_threshold': 0.50,          # DA > 50% → 복구

    # ── S1 DA: 하드코딩 → 동적 전환 ──
    's1.directional.kospi_score_scale': 2.0,            # KOSPI 스코어 정규화 스케일 (±2% → ±1.0)
    's1.directional.kospi_weak_weight': 0.30,           # KOSPI 움직임 작을 때 KOSPI 가중치
    's1.directional.kospi_weak_ois_weight': 0.70,       # KOSPI 움직임 작을 때 OIS 가중치
    's1.directional.multi_tf_mismatch_penalty': 0.70,   # Multi-TF 불일치 시 score 배수 (0.7=30% 감점)
    's1.domestic_gap.vkospi_neutral': 18.0,             # VKOSPI 중립 기준선
    's1.domestic_gap.vkospi_scale': 20.0,               # VKOSPI 정규화 스케일
    's1.domestic_gap.flow_scale': 50.0,                 # 외인 수급 모멘텀 정규화 스케일
    's1.domestic_gap.flat_zone': 0.10,                  # flat 판정 스코어 범위 (±)
    's1.domestic_gap.sigmoid_k': 3.0,                   # 시그모이드 기울기 (스코어→확률)
    's1.domestic_gap.min_probability': 0.35,             # 확률 하한
    's1.domestic_gap.max_probability': 0.85,             # 확률 상한
    's1.domestic_gap.confidence_boost_scale': 0.15,     # confidence 부스트 스케일 (score × scale)

    # ══════════════════════════════════════════════════════════
    # Alpha-Ranked Hysteresis Rebalancing (S3/S4)
    # ══════════════════════════════════════════════════════════

    # ── 공통 스코어링 (보유/미보유 동일 기준) ──
    'rebalance.score.momentum_weight': 0.40,            # 모멘텀 팩터 가중치
    'rebalance.score.quality_weight': 0.25,             # 퀄리티 팩터 가중치
    'rebalance.score.confidence_weight': 0.25,          # 시그널 confidence 가중치
    'rebalance.score.volatility_weight': 0.10,          # 저변동성 팩터 가중치
    'rebalance.score.momentum_lookback_days': 60,       # 모멘텀 계산 기간 (일)
    'rebalance.score.momentum_short_lookback': 20,      # 단기 모멘텀 기간 (일)
    'rebalance.score.momentum_long_ratio': 0.60,        # 장기 모멘텀 비중 (나머지=단기)
    'rebalance.score.volatility_lookback_days': 20,     # 변동성 계산 기간 (일)
    'rebalance.score.held_bonus_ratio': 0.50,           # 보유 보너스 = cost_hurdle × ratio

    # ── S3 (섹터/팩터 ETF, 1~3개월) ──
    's3.rebalance.enabled': True,
    's3.rebalance.frequency_days': 5,                   # 리밸런싱 주기 (거래일)
    's3.rebalance.max_positions': 5,                    # 최대 보유 종목 수
    's3.rebalance.entry_percentile': 80,                # 편입: 상위 20% (동적 계산)
    's3.rebalance.exit_percentile': 40,                 # 편출: 하위 40% (동적 계산)
    's3.rebalance.max_turnover_pct': 0.40,              # 월 최대 턴오버 40%
    's3.rebalance.min_holding_days': 5,                 # 최소 보유 기간 (whipsaw 방지)
    's3.rebalance.cost_hurdle_pct': 0.015,              # 교체 최소 알파 이점 1.5%
    's3.rebalance.max_replacements_per_cycle': 2,       # 주기당 최대 교체 수

    # ── S4 (절세계좌, 3~6개월+) ──
    's4.rebalance.enabled': True,
    's4.rebalance.frequency_days': 20,                  # 리밸런싱 주기 (거래일)
    's4.rebalance.max_positions': 16,                   # 최대 보유 종목 수
    's4.rebalance.entry_percentile': 85,                # 편입: 상위 15% (보수적)
    's4.rebalance.exit_percentile': 30,                 # 편출: 하위 30%
    's4.rebalance.max_turnover_pct': 0.20,              # 월 최대 턴오버 20%
    's4.rebalance.min_holding_days': 20,                # 최소 보유 기간
    's4.rebalance.cost_hurdle_pct': 0.03,               # 교체 최소 알파 이점 3%
    's4.rebalance.max_replacements_per_cycle': 3,       # 주기당 최대 교체 수
    's4.rebalance.tax_aware': True,                     # 절세계좌 특성 반영

    # Exit 레버리지 스케일링: TP/SL을 기초자산 기준으로 환산
    # adjusted_tp = base_tp * leverage_exit_scale * abs(leverage)
    's1.exit.leverage_scale_enabled': True,             # 레버리지 Exit 스케일 활성화
    's1.exit.leverage_scale_factor': 1.0,               # 스케일 보정 계수 (1.0=풀스케일)

    # ── S1 Exit: 변동성 기반 동적 TP/SL ──
    # σ_daily = vol_index / √252 → dynamic_tp = σ × multiplier × leverage
    's1.exit.dynamic_tp_sl_enabled': True,              # 동적 TP/SL 활성화
    's1.exit.tp_floor_pct': 0.005,                      # TP 최소 0.5% (아무리 낮아도)
    's1.exit.tp_ceiling_pct': 0.06,                     # TP 최대 6.0% (아무리 높아도)
    's1.exit.sl_floor_pct': 0.004,                      # SL 최소 0.4%
    's1.exit.sl_ceiling_pct': 0.05,                     # SL 최대 5.0%

    # ── S1 Exit: Confluence Trailing 동적 ──
    's1.exit.trail_vol_multiplier': 0.7,                # Trailing Stop = σ × 0.7 × leverage
    's1.exit.trail_activate_ratio': 1.0,                # Lv.2: TP와 같은 수준에서 활성
    's1.exit.trail_early_ratio': 0.7,                   # Lv.3: TP × 0.7에서 조기 활성
    's1.exit.trail_floor_pct': 0.003,                   # Trailing 최소 폭 0.3%
    's1.exit.trail_ceiling_pct': 0.04,                  # Trailing 최대 폭 4.0%

    # ── S1 Exit: 베이스 프로파일 (동적 비활성 시 폴백) ──
    's1.exit.base_tp_pct': 0.015,                       # 기본 TP +1.5%
    's1.exit.base_sl_pct': -0.010,                      # 기본 SL -1.0%
    's1.exit.max_hold_days': {'bull': 0, 'caution': 0, 'bear': 0, 'crash': 0},
    's1.exit.min_hold_days': 0,
    's1.exit.signal_expire_days': 0,                    # 당일 신호만 유효
    's1.exit.max_daily_trades': 3,                      # 일일 최대 매매 횟수
    's1.exit.confluence_tp': {1: 0.015, 2: 0.015, 3: 0.015},
    's1.exit.confluence_trailing_activate': {1: None, 2: 0.015, 3: 0.010},
    's1.exit.confluence_trailing_stop': {1: None, 2: -0.005, 3: -0.005},

    # ── S1 Exit: 변동성 소스 매핑 ──
    # VIX 사용 대상 underlying (나머지는 VKOSPI)
    's1.exit.vix_underlyings': ['NASDAQ100', 'SP500', 'PHLX_SEMI'],
    's1.exit.vol_fallback': 18.0,                       # VIX/VKOSPI 로드 실패 시 기본값

    # ── S1 Exit: 장마감 청산 (시간 기반 Exit) ──
    # Lv.2-3: TP 비활성 → SL만 유지 → 장마감 강제 청산
    's1.exit.time_based_exit_enabled': True,             # 시간 기반 Exit 활성화
    's1.exit.close_time_hour': 15,                      # 장마감 청산 시각 (시)
    's1.exit.close_time_minute': 10,                    # 장마감 청산 시각 (분) → 15:10
    's1.exit.time_exit_min_confluence': 2,              # 시간 기반 Exit 최소 합류 수준

    # ── S1 Exit: ETF 프리미엄/디스카운트 필터 ──
    # iNAV 대비 시장가 괴리 모니터링 (레버리지/인버스 공통)
    's1.exit.premium_filter_enabled': True,             # 프리미엄 필터 활성화
    's1.exit.premium_warn_pct': 2.0,                    # 경고 임계값 (%)
    's1.exit.premium_block_pct': 5.0,                   # 진입 차단 임계값 (%)
    's1.exit.premium_tp_adjust': True,                  # 프리미엄 시 TP 하향 조정

    # ── ★ 실시간 Exit 모니터링 (RealtimeExitMonitor) ──
    # 하이브리드: WebSocket(Layer1) + Threshold Alert(Layer2) + REST Heartbeat(Layer3)
    'monitor.enabled': True,                            # 모니터링 전체 활성화
    'monitor.heartbeat_interval_sec': 300,              # REST heartbeat 주기 (기본 5분)
    'monitor.ws_enabled': True,                         # WebSocket 가격 스트림 사용
    'monitor.ws_fallback_interval_sec': 60,             # WS 장애 시 REST 간격 (1분)
    'monitor.ws_fallback_to_rest': True,                # WS 장애 시 REST fallback
    'monitor.alert_margin_pct': 10,                     # Alert Zone 마진 (SL/TP 대비 %)
    'monitor.alert_check_interval_sec': 30,             # Alert Zone 내 가속 체크 (30초)
    'monitor.rest_batch_size': 20,                      # REST 배치 가격 조회 크기
    'monitor.s1_priority': True,                        # S1 레버리지 우선 모니터링
    'monitor.max_exit_per_cycle': 5,                    # 사이클당 최대 Exit 건수
    'monitor.market_start_hour': 9,                     # 장 시작 (시)
    'monitor.market_start_minute': 0,                   # 장 시작 (분)

    # ── S1 Edge: 인버스 헤지 ──
    's1.inverse.confidence_scale': 20.0,       # 인버스 신뢰도 스케일
    's1.inverse.confidence_base': 0.30,        # 인버스 기본 신뢰도
    's1.inverse.confidence_cap': 0.80,         # 인버스 신뢰도 상한
    's1.inverse.size_ratio': 0.15,             # 인버스 포지션 비율

    # ── S1 Edge: 편입 검증 기준 ──
    's1.validation.min_win_rate': 0.53,        # 최소 승률
    's1.validation.min_sharpe': 0.50,          # 최소 Sharpe
    's1.validation.max_slippage': 0.0020,      # 최대 슬리피지 (0.20%)
    's1.validation.min_ev_pass_rate': 0.30,    # EV 필터 최소 통과율
    's1.validation.min_trades': 15,            # 최소 거래 수
    's1.validation.min_days': 20,              # 최소 관찰 일수
    's1.validation.annualization_factor': 252,  # 연율화 계수
    's1.validation.bootstrap_enabled': True,    # 부트스트랩 모드 활성화
    's1.validation.bootstrap_scale': 0.50,      # 부트스트랩 포지션 축소 비율
    's1.validation.bootstrap_max_days': 20,     # 부트스트랩 적용 최대 일수

    # ── S3 Factor: 멀티팩터 가중치 ──
    's3.factor_weight_momentum': 0.40,         # 모멘텀 팩터 가중치
    's3.factor_weight_value': 0.25,            # 밸류 팩터 가중치
    's3.factor_weight_carry': 0.15,            # 캐리(배당) 팩터 가중치
    's3.factor_weight_volatility': 0.20,       # 변동성 팩터 가중치
    's3.momentum_weighted_allocation': True,    # ★ 진단 개선: 모멘텀 가중 배분 (1/N → 점수 기반)
    's3.momentum_weight_exponent': 1.5,        # ★ 모멘텀 점수 제곱 가중 (강한 섹터에 집중)
    's3.momentum_weight_min': 0.15,            # ★ 최소 종목 비중 (극단 집중 방지)
    's3.rebalance_frequency': 'monthly',       # ★ 진단 개선: 분기→월간 리밸런싱

    # ── S3 Factor: 턴오버 제한 ──
    's3.max_turnover_pct': 0.50,               # 월간 최대 교체율
    's3.inertia_bonus': 0.03,                  # 기존 보유종목 관성 보너스
    's3.transaction_cost_threshold': 0.005,     # 교체 시 최소 순알파

    # ── S3 Factor: 레짐별 투자 비율 ──
    's3.regime_invest_ratio.bull': 1.0,
    's3.regime_invest_ratio.caution': 0.8,
    's3.regime_invest_ratio.bear': 0.5,
    's3.regime_invest_ratio.crash': 0.3,

    # ── S3 Factor: 오버나이트 폴백 배수 ──
    's3.overnight_fallback.us_tech': 0.30,
    's3.overnight_fallback.us_semiconductor': 0.30,
    's3.overnight_fallback.us_broad': 0.30,
    's3.overnight_fallback.ai': 0.25,
    's3.overnight_fallback.ai_infra': 0.25,
    's3.overnight_fallback.india': 0.15,
    's3.overnight_fallback.china': 0.10,

    # ── [Phase 36: Paper Trading / Slippage Simulation] (체결 환경) ──────────
    'execution.mode': 'live',                 # 'mock', 'paper', 'live' (Shadow Trading 잠금)
    'execution.commission_rate.krx': 0.000088,
    's3.global_momentum_1m_weight': 0.6,       # 글로벌 1M 모멘텀 가중치
    's3.global_momentum_3m_weight': 0.4,       # 글로벌 3M 모멘텀 가중치
    's3.min_global_slots': 1,                  # 최소 글로벌 ETF 슬롯 보장
    's3.max_per_sector': 2,                    # 섹터당 최대 ETF 수

    # ── S4 Advisory: ISA ETF:주식 비율 (ISA 절세 집중) ──
    's4.isa.etf_ratio.bull': 0.55,
    's4.isa.etf_ratio.caution': 0.60,
    's4.isa.etf_ratio.bear': 0.70,
    's4.isa.etf_ratio.crash': 0.85,
    's4.isa.min_value_score': 15.0,
    's4.isa.max_qv_holdings': 5,
    's4.isa.min_qv_holdings': 3,

    # ── S4 Advisory: ETF confidence ──
    's4.etf_base_confidence': 0.60,            # ETF 기본 confidence (0.80 → 0.60 하향)
    's4.etf_confidence_dynamic': True,         # ETF confidence 동적 계산
    's4.etf_momentum_weight': 0.20,            # 모멘텀 가중치

    # ── S4 Advisory: BROKERAGE 독립운용 (대형주 섹터QV) ──
    's4.brokerage.equity_pct.bull': 0.85,
    's4.brokerage.equity_pct.caution': 0.75,
    's4.brokerage.equity_pct.bear': 0.50,
    's4.brokerage.equity_pct.crash': 0.30,
    's4.brokerage.max_per_sector': 1,
    's4.brokerage.min_qv_score': 40.0,
    's4.brokerage.max_holdings': 8,
    's4.brokerage.min_stock_weight': 0.05,
    's4.brokerage.max_stock_weight': 0.15,

    # ── S4 Advisory: QV 신뢰도 계산 ──
    's4.qv_confidence_scale': 80.0,            # QV→confidence 스케일 (레거시 호환)
    's4.qvm_confidence_scale': 100.0,          # ★ QVM 전체(100점) 기준 스케일
    's4.qv_confidence_cap': 0.85,              # QV 신뢰도 상한
    's4.qv_confidence_floor': 0.20,            # QV 신뢰도 하한

    # ── S4 Advisory: 컨센서스 보정 (Level 3) ──
    's4.consensus_enrichment_enabled': True,    # 컨센서스 보정 활성화
    's4.consensus_confidence_weight': 0.20,     # 보정 가중치 (±10%)
    's4.consensus_max_age_days': 30,            # 데이터 유효기간 (일)

    # ── S4 Advisory: DRIP 추정 ──
    's4.drip.base_capital': 10_000_000,        # DRIP 추정 기준 자본
    's4.drip.assumed_div_yield': 0.04,         # 추정 배당수익률
    's4.drip.assumed_growth_rate': 0.08,       # 추정 성장률

    # ── S4 Advisory: 절세 계산 ──
    's4.tax.isa_savings_cap': 330_000,         # ISA 절세 상한
    's4.tax.credit_rate': 0.165,               # 세액공제율

    # ── S4 Advisory: ML Boost (S2 ML prob 시너지) ──
    's4.ml_boost_enabled': True,               # S2 ML 예측 보조 활성화
    's4.ml_boost_weight': 0.15,                # ML 반영 비중 (0~1, QV 스코어에 결합)
    's4.ml_boost_min_prob': 0.50,              # ML prob 최소 임계값 (이하 무시)
    's4.ml_boost_predictions_dir': 'data/predictions',  # predictions 디렉토리 (프로젝트 루트 기준)

    # ── S4 Advisory: QVM IC/ICIR 검증 (#11) ──
    's4.ic.min_acceptable_ic': 0.02,           # 최소 허용 IC
    's4.ic.warning_ic': 0.01,                  # IC 경고 수준
    's4.ic.icir_lookback_days': 60,            # ICIR 계산 lookback
    's4.ic.forward_return_days': 20,           # IC 계산 forward return 기간

    # ── S4 Advisory: Risk Parity (#12) ──
    's4.risk_parity.enabled': True,            # Risk Parity 활성화
    's4.risk_parity.blend_ratio': 0.50,        # Risk Parity vs 레짐 Tilt 혼합 비율
    's4.risk_parity.vol_lookback_days': 60,    # 변동성 계산 기간
    's4.risk_parity.min_weight': 0.05,         # 최소 비중
    's4.risk_parity.max_weight': 0.40,         # 최대 비중

    # ── S4 Advisory: 동적 Exit Rules (anchor 파라미터) ──
    # ★ 모든 임계값은 DynamicExitEvaluator에서 시장 데이터 기반으로 동적 계산
    's4.exit.qv_decay_percentile': 20,         # QV 하위 percentile 기준점 (레짐별 조정됨)
    's4.exit.qv_min_decay_pct': -30,           # QV 하락률 기준점 (동적 QV와 AND 조건)
    's4.exit.base_max_hold_days': 130,         # 기본 보유기간 기준점 (레짐별 0.5~1.2x)
    's4.exit.enabled': True,                   # 동적 Exit 평가 활성화

    # ═══════════════════════════════════════════════════════
    # ★ PHASE 2 — 전략 실증 + 리스크 고도화 + 실행 정교화
    # ═══════════════════════════════════════════════════════

    # ── CVaR 최적화 (2-B-1) ──
    'risk.target_cvar_pct': -0.03,             # 목표 CVaR (3% 최대 손실)
    'risk.cvar_optimization_iterations': 1000, # 랜덤 탐색 반복 횟수
    'risk.cvar_min_weight': 0.02,              # CVaR 최적화 최소 비중
    'risk.cvar_max_weight': 0.40,              # CVaR 최적화 최대 비중

    # ── 스트레스 테스트 (2-B-2) ──
    'risk.stress.enabled': True,               # 스트레스 테스트 활성화
    'risk.reverse_stress_target': -0.20,       # 역스트레스 목표 손실 (-20%)

    # ── 꼬리 위험 헤지 (2-B-4) ──
    'risk.tail.vix_warning': 25,               # VIX 경고 수준
    'risk.tail.vix_critical': 35,              # VIX 위기 수준
    'risk.tail.vkospi_warning': 22,            # VKOSPI 경고 수준
    'risk.tail.pc_ratio_warning': 1.2,         # P/C 비율 경고 수준
    'risk.tail.skew_warning': 5.0,             # 옵션 스큐 경고 수준
    'risk.tail.iv_rv_warning': 1.3,            # IV/RV 비율 경고 수준
    'risk.tail.hedge_threshold': 40,           # 헤지 발동 위험 점수 (0~100)
    'risk.tail.max_hedge_ratio': 0.30,         # 최대 헤지 비율

    # ── Walk-Forward 백테스트 (2-A-1) ──
    'backtest.train_window_days': 252,         # 훈련 윈도우 (1년)
    'backtest.test_window_days': 63,           # 테스트 윈도우 (1분기)
    'backtest.anchored': False,                # True=확장, False=롤링
    
    # ── 동적 고도화 (Backtesting Engine) ──
    'backtest.engine': 'polars',               # 백테스트 이벤트 파싱/분석 엔진 ('pandas', 'polars')
    'backtest.log_format': 'parquet',          # 이벤트 로그 형식 ('json', 'parquet')
    'backtest.min_folds': 3,                   # 최소 폴드 수

    # ── 알파 감쇠 추적 (2-A-2) ──
    'alpha.min_half_life_days': 30,            # 반감기 경고 임계 (critical)
    'alpha.warning_half_life_days': 60,        # 반감기 주의 임계 (caution)
    'alpha.min_observations': 30,              # 최소 관측 일수
    'alpha.rolling_window': 60,                # 롤링 알파 윈도우
    'alpha.prune_rolling_ic_threshold': 0.01,  # 팩터 폐기 기준 롤링 IC 임계값

    # ── 통계적 유의성 검증 (2-A-4) ──
    'stat.significance_level': 0.05,           # 유의수준 (5%)
    'stat.n_bootstrap': 10000,                 # Bootstrap 반복 횟수
    'stat.min_observations': 30,               # 최소 관측 수 (대시보드 ≥30과 일치)

    # ── TCA 실행 품질 (2-C-1) ──
    'execution.target_is_bps': 5.0,            # 목표 Implementation Shortfall
    'execution.target_vwap_bps': 3.0,          # 목표 VWAP 슬리피지

    # ── 상관관계 모니터 (2-B-3) ──
    'allocator.correlation_alert_threshold': 0.60,  # 상관 경고 임계값

    # ═══════════════════════════════════════════════════════
    # ★ PHASE 3 — 자가진화 인프라 + 실시간 적응
    # ═══════════════════════════════════════════════════════

    # ── 메타 전략 계층 (3-A-2) ──
    'meta.ema_alpha': 0.10,                    # EMA 알파 (성과 평활)
    'meta.evaluation_window': 30,              # 성과 평가 윈도우 (일)
    'meta.disable_sharpe_threshold': -0.5,     # 스트림 비활성화 Sharpe 임계
    'meta.recovery_sharpe_threshold': 0.3,     # 스트림 회복 Sharpe 임계
    'meta.min_stream_weight': 0.05,            # 스트림 최소 비중
    'meta.max_stream_weight': 0.50,            # 스트림 최대 비중
    'meta.regime_blend_ratio': 0.30,           # 성과 vs 레짐 기본값 혼합 비율

    # ── 자동 파라미터 최적화 (3-A-3) ──
    'optimizer.sensitivity_points': 20,        # 감도 분석 포인트 수
    'optimizer.n_iterations': 500,             # Random Search 반복 횟수
    'optimizer.stability_perturbation': 0.05,  # 안정성 검증 변동률 (±5%)
    'optimizer.stability_tests': 20,           # 안정성 검증 테스트 수

    # ── 적응형 리스크 한도 (3-B-3) ──
    'adaptive_risk.max_exposure.bull': 0.90,   # Bull 최대 노출
    'adaptive_risk.max_exposure.caution': 0.70,
    'adaptive_risk.max_exposure.bear': 0.50,
    'adaptive_risk.max_exposure.crash': 0.25,
    'adaptive_risk.dd_limit.bull': -0.07,      # Bull DD 한도
    'adaptive_risk.dd_limit.caution': -0.05,
    'adaptive_risk.dd_limit.bear': -0.03,
    'adaptive_risk.dd_limit.crash': -0.02,
    'adaptive_risk.max_single.bull': 0.15,     # Bull 단일 종목 최대
    'adaptive_risk.max_single.caution': 0.10,
    'adaptive_risk.max_single.bear': 0.07,
    'adaptive_risk.max_single.crash': 0.05,
    'adaptive_risk.max_leverage.bull': 1.2,
    'adaptive_risk.max_leverage.caution': 1.0,
    'adaptive_risk.max_leverage.bear': 0.8,
    'adaptive_risk.max_leverage.crash': 0.5,
    'adaptive_risk.vix_base': 20,              # VIX 기준값
    'adaptive_risk.vix_scale': 15,             # VIX 스케일링 분모
    'adaptive_risk.daily_risk_budget': -0.03,  # 일일 리스크 버짓 (-3%)
    'adaptive_risk.transition_smoothing': 0.5, # 레짐 전환 스무딩 (50%)

    # ── 피드백 루프 (3-B-4) ──
    'feedback.adjustment_sharpe_threshold': -0.3,  # 조정 발동 Sharpe
    'feedback.retrain_execution_rate': 0.50,       # 재훈련 트리거 실행률

    # ── 감사 추적 (3-C-2) ──
    'audit.buffer_size': 100,                  # 플러시 전 버퍼 크기

    # ── 데이터 품질 (2-D-2) ──
    'data_quality.min_score': 0.80,            # 최소 품질 점수
    'data_quality.outlier_zscore': 4.0,        # 이상값 Z-score 임계
    'data_quality.max_gap_days': 5,            # 최대 허용 갭 (일)
    'data_quality.score_weights': [0.30, 0.25, 0.25, 0.20],  # 결측/이상/연속/범위

    # ── 온라인 학습 (3-A-1) ──
    'online.learning_rate': 0.05,              # EWA 학습률
    'online.max_step': 0.10,                   # 1회 최대 변동폭 (10%)
    'online.min_observations_per_update': 10,  # 갱신 최소 관측치
    'online.forgetting_lambda': 0.995,         # 일별 감쇠율 (0.5% 감쇠)
    'online.max_buffer_size': 500,             # 관측 버퍼 최대 크기
    'online.s1.confidence_scale': 1.0,         # S1 confidence 스케일
    'online.s1.size_scale': 1.0,               # S1 포지션 크기 스케일
    'online.s2.confidence_scale': 1.0,         # S2 confidence 스케일
    'online.s2.size_scale': 1.0,               # S2 포지션 크기 스케일
    'online.s3.confidence_scale': 1.0,         # S3 confidence 스케일
    'online.s3.size_scale': 1.0,               # S3 포지션 크기 스케일
    'online.s4.confidence_scale': 1.0,         # S4 confidence 스케일
    'online.s4.size_scale': 1.0,               # S4 포지션 크기 스케일

    # ── 전략 생성기 (3-A-4) ──
    'generator.max_candidates': 100,           # 최대 후보 수
    'generator.min_oos_sharpe': 0.5,           # 최소 OOS Sharpe
    'generator.min_dsr_significance': True,    # DSR 유의성 필터 사용
    'generator.templates': ['momentum', 'mean_revert', 'breakout', 'carry'],
    'generator.random_seed': 42,               # 재현성 시드
    'generator.wf_train_ratio': 0.7,           # Walk-Forward 학습 비율
    'generator.max_final': 10,                 # 최종 채택 최대 수
    'generator.annualization_factor': 252,     # 연환산 팩터

    # ── 시뮬레이션 V2 (3-C-3) ──
    'simulation.initial_capital': 154_000_000, # 초기 자본 (1.54억)
    'simulation.fee_model': 'realistic',       # realistic / zero / custom
    'simulation.slippage_bps': 8,              # 슬리피지 (bps)
    'simulation.commission_bps': 7,            # 수수료 (bps)
    'simulation.annualization_factor': 252,    # 연환산 팩터
    'simulation.n_monte_carlo': 100,           # 몬테카를로 횟수
    'simulation.mc_seed_base': 7777,           # MC 시드 베이스
    'simulation.base_price': 100.0,            # 시나리오 기준 가격
    'simulation.scenario_seed': 123,           # 시나리오 시드
    'simulation.custom_cost_bps': 10,          # 커스텀 비용
    'simulation.scenarios': ['standard', 'flash_crash', 'prolonged_bear', 'sideways'],

    # ── Smart Order Routing (2-C-2) ──
    'router.session_pre_start': '08:00',
    'router.session_pre_end': '08:50',
    'router.session_regular_start': '09:00',
    'router.session_regular_end': '15:20',
    'router.session_after_start': '15:30',
    'router.session_after_end': '20:00',
    'router.commission_bps.krx': 0.88,        # KRX 수수료 (bps)
    'router.commission_bps.nxt': 0.53,        # NXT 수수료 (bps)
    'router.commission_bps.sor': 0.70,        # SOR 수수료 (bps)
    'router.default_spread_bps.krx': 3.0,     # KRX 기본 스프레드 (bps)
    'router.default_spread_bps.nxt': 5.0,     # NXT 기본 스프레드 (bps)
    'router.default_spread_bps.sor': 3.5,     # SOR 기본 스프레드 (bps)
    'router.default_adv.krx': 5_000_000_000,  # KRX 기본 ADV (원)
    'router.default_adv.nxt': 1_000_000_000,  # NXT 기본 ADV (원)
    'router.default_adv.sor': 5_000_000_000,  # SOR 기본 ADV (원)
    'router.impact_coefficient': 0.1,         # Almgren-Chriss 충격 계수
    'router.max_cost_bps': 20,                # 최대 비용 기준 (점수 정규화)
    'router.large_order_threshold': 50_000_000,  # 대형 주문 기준 (5천만)
    'router.large_order_liq_boost': 1.2,      # 대형 주문 유동성 가중 부스트
    'router.vix_instability_threshold': 30,   # VIX 불안정 임계값
    'router.nxt_vix_penalty': 0.15,           # 고VIX 시 NXT 안정성 감점
    'router.split_threshold_amount': 100_000_000,  # 분할 권고 기준 (1억)
    'router.weight_cost_normal': 0.35,
    'router.weight_liquidity_normal': 0.25,
    'router.weight_speed_normal': 0.20,
    'router.weight_reliability_normal': 0.20,
    'router.weight_cost_high': 0.20,
    'router.weight_liquidity_high': 0.30,
    'router.weight_speed_high': 0.35,
    'router.weight_reliability_high': 0.15,
    'router.weight_cost_low': 0.50,
    'router.weight_liquidity_low': 0.20,
    'router.weight_speed_low': 0.10,
    'router.weight_reliability_low': 0.20,
    'router.liquidity_scores': {'KRX': 0.95, 'NXT': 0.50, 'SOR': 0.90},
    'router.speed_scores': {'KRX': 0.90, 'NXT': 0.85, 'SOR': 0.95},
    'router.reliability_scores': {'KRX': 0.98, 'NXT': 0.80, 'SOR': 0.92},

    # ── 실행 엔진 동적 비율 ──
    'execution.mode': 'live',                 # 'mock', 'paper', 'live' (Shadow Trading 잠금)
    'execution.commission_rate.krx': 0.000088,
    'execution.commission_rate.nxt': 0.000053,
    'execution.commission_rate.sor': 0.000070,
    'execution.slippage_rate.krx': 0.001,
    'execution.slippage_rate.nxt': 0.0006,
    'execution.slippage_rate.sor': 0.0008,
    'execution.algo_slip_reduction': 0.4,     # Algo 사용 시 슬리피지 절감률

    # ── Feature Store V2 (2-D-1) ──
    'feature_store.default_ttl_hours': 24,     # 기본 TTL (시간)
    'feature_store.ttl.price': 24,             # 가격 피처 TTL
    'feature_store.ttl.volume': 24,            # 거래량 피처 TTL
    'feature_store.ttl.fundamental': 168,      # 펀더멘탈 TTL (7일)
    'feature_store.ttl.sentiment': 12,         # 감성 피처 TTL
    'feature_store.ttl.macro': 24,             # 매크로 TTL
    'feature_store.ttl.derived': 24,           # 파생 피처 TTL
    'feature_store.zscore_window': 60,         # Z-score 계산 윈도우
    'feature_store.zscore_min_obs': 20,        # Z-score 최소 관측치
    'feature_store.rolling_window': 20,        # Rolling 통계 윈도우
    'feature_store.history_days': 90,          # 파생피처용 히스토리 일수
    'feature_store.signal_cache_macro_keys': [
        'vix', 'sp500', 'nasdaq', 'us10y', 'dxy', 'wti',
        'gold_us', 'usdkrw', 'ois',
    ],

    # ── 대체 데이터 파이프라인 (2-D-3) ──
    'altdata.active_sources': [
        'asian_markets', 'derivatives', 'trade_data',
        'trends', 'social_sentiment', 'economic_indicators',
    ],
    'altdata.asian_tickers': {
        'nikkei225': '^N225',
        'shanghai_comp': '000001.SS',
        'hang_seng': '^HSI',
    },
    'altdata.asian_period': '5d',
    'altdata.yf_timeout': 10,
    'altdata.weight_nikkei': 0.40,          # 일본 가중치
    'altdata.weight_shanghai': 0.30,        # 중국 가중치
    'altdata.weight_hangseng': 0.30,        # 홍콩 가중치
    'altdata.vkospi_low': 15,              # 저변동성 임계
    'altdata.vkospi_high': 25,             # 고변동성 임계
    'altdata.vkospi_extreme': 35,          # 극단 변동성 임계
    'altdata.trends_keywords_kr': [
        '삼성전자', '코스피', '금리', '환율', '반도체',
    ],
    'altdata.trends_lookback_days': 90,     # 트렌드 룩백 기간
    'altdata.kosis_lookback_days': 365,     # KOSIS 룩백 기간
    'altdata.min_quality_score': 0.5,       # 최소 품질 점수

    # ── 포트폴리오 최적화기 (3-B) ──
    'optimizer.tx_cost_rate': 0.0015,           # 왕복 거래비용 (15bps)
    'optimizer.no_trade_zone': 0.02,            # 비중 변동 2% 미만 → 미실행
    'optimizer.min_trade_value': 50_000,        # 소액 라이브: 최소 거래 5만
    'optimizer.max_daily_rebalances': 2,         # 일 최대 리밸런싱 횟수
    'optimizer.max_monthly_rebalances': 10,      # 월 최대 리밸런싱 횟수
    'optimizer.min_rebalance_interval_hours': 4,  # 최소 간격 4시간
    'optimizer.regime_change_override': True,     # 레짐 급변 시 제한 무시
    'optimizer.history_retention_days': 90,       # 이력 보관 90일
    'optimizer.cash_to_defensive': True,          # 미사용 비중 → S4 이동
    'optimizer.max_cvar_pct': -0.03,             # 포트폴리오 최대 CVaR
    'optimizer.exposure.bull': 1.0,              # 레짐별 노출도 fallback
    'optimizer.exposure.caution': 0.65,
    'optimizer.exposure.bear': 0.30,
    'optimizer.exposure.crash': 0.0,

    # ── HMM 레짐 전환 (3-B) ──
    'regime.hmm_n_states': 4,                    # HMM 상태 수 (bull/caution/bear/crash)
    'regime.hmm_covariance': 'diag',             # 공분산 타입 (diag|full|tied)
    'regime.hmm_n_iter': 150,                    # EM 반복 횟수
    'regime.hmm_min_observations': 60,           # 최소 관측치 수
    'regime.hmm_confidence_lookback': 10,        # Confidence 산출 윈도우
    'regime.rule_weight': 0.55,                  # 앙상블: Rule 가중치
    'regime.hmm_weight': 0.45,                   # 앙상블: HMM 가중치

    # ── V자 전환 시그널 ──
    'regime.transition_window': 10,              # 전환 감지 윈도우 (일)
    'regime.v_signal_threshold': 0.3,            # V자 시그널 임계값
    'regime.hmm_recovery_prob_threshold': 0.30,  # HMM bear→bull 전환 임계값
    'regime.hmm_breakdown_prob_threshold': 0.25, # HMM bull→crash 전환 임계값
    'regime.momentum_reversal_window': 5,        # 모멘텀 반전 윈도우 (일)
    'regime.momentum_reversal_threshold': 0.005, # 모멘텀 반전 임계값

    # ── 전환 시그널 가중치 + 노출 조정 ──
    'regime.transition_weight_hmm': 0.45,        # HMM 시그널 가중치
    'regime.transition_weight_intraday': 0.30,   # 장중 시그널 가중치
    'regime.transition_weight_momentum': 0.25,   # 모멘텀 시그널 가중치
    'regime.transition_signal_threshold': 0.20,  # 종합 시그널 임계값
    'regime.recovery_exposure_boost': 1.15,      # 반등 시 노출 확대 배수
    'regime.breakdown_exposure_cut': 0.70,       # 급락 시 노출 축소 배수

    # ── 장중 레짐 (V자 반등) ──
    'regime.intraday_high_vol': 0.30,            # 장중 고변동 임계값
    'regime.intraday_recovery_exposure': 1.15,   # 반등 레짐 노출 배수
    'regime.recovery_strength_threshold': 0.50,  # 반등 강도 임계값

    # ── 인프라 장애 복구 (3-C) ──
    'checkpoint.max_retries': 2,                  # Phase 최대 재시도 횟수
    'health.max_data_age_hours': 24,               # 데이터 최대 경과 시간 — 1일 1회 갱신 기준
    'health.min_disk_gb': 5,                      # 최소 디스크 여유 공간 (GB)
    'health.watchdog_max_age_minutes': 30,        # Watchdog heartbeat 유효 기간
    'circuit_breaker.default_failure_threshold': 3,  # 연속 실패 → OPEN
    'circuit_breaker.default_recovery_timeout': 1800,  # OPEN → HALF_OPEN 대기 (초)
    'backup.retention_days': 7,                   # 백업 보관 일수

    # ── Rule-Based 레짐 스코어 기여값 ──
    'regime.rule_vix_boost': 20,                  # VIX < bull → +점수
    'regime.rule_vix_interp': 10,                 # VIX 보간 배수
    'regime.rule_vix_crash_penalty': 25,          # VIX > bear → -점수
    'regime.rule_vkospi_boost': 15,               # VKOSPI < low → +점수
    'regime.rule_vkospi_mild': 5,                 # VKOSPI < mid → +점수
    'regime.rule_vkospi_stress': 10,              # VKOSPI < high → -점수
    'regime.rule_vkospi_crash': 20,               # VKOSPI > high → -점수
    'regime.rule_fx_risk_pct': 1.0,               # 환율 급락 기준 (%)
    'regime.rule_fx_risk_penalty': 8,             # 환율 급락 페널티
    'regime.rule_fx_safe_pct': -0.5,              # 원화 강세 기준 (%)
    'regime.rule_fx_safe_boost': 5,               # 원화 강세 부스트
    'regime.rule_ois_bull': 70,                   # OIS 강세 기준
    'regime.rule_ois_neutral': 50,                # OIS 중립 기준
    'regime.rule_ois_bear': 30,                   # OIS 약세 기준
    'regime.rule_ois_bull_boost': 10,             # OIS 강세 부스트
    'regime.rule_ois_neutral_boost': 3,           # OIS 중립 부스트
    'regime.rule_ois_bear_penalty': 10,           # OIS 약세 페널티
    'regime.rule_score_bull': 65,                 # 스코어 → bull 기준
    'regime.rule_score_caution': 45,              # 스코어 → caution 기준
    'regime.rule_score_bear': 25,                 # 스코어 → bear 기준
    'regime.rule_confidence_floor': 0.3,          # confidence 하한

    # ── Exposure Orchestrator 스코어맵 ──
    'exposure.regime_score.bull': 1.0,
    'exposure.regime_score.caution': 0.65,
    'exposure.regime_score.bear': 0.30,
    'exposure.regime_score.crash': 0.0,
    'exposure.vix_score.50': 0.0,
    'exposure.vix_score.35': 0.2,
    'exposure.vix_score.25': 0.5,
    'exposure.vix_score.18': 0.8,
    'exposure.vix_score.low': 1.0,
    'exposure.vix_alert_threshold': 30,
    'exposure.fg_score.extreme_greed': 0.2,
    'exposure.fg_score.greed': 0.5,
    'exposure.fg_score.neutral': 0.8,
    'exposure.fg_score.fear': 0.9,
    'exposure.fg_score.extreme_fear': 0.3,
    'exposure.vkospi_score.35': 0.1,
    'exposure.vkospi_score.25': 0.4,
    'exposure.vkospi_score.18': 0.7,
    'exposure.vkospi_score.low': 1.0,
    'exposure.trend_strong_up': 3,
    'exposure.trend_strong_down': -3,
    'exposure.trend_score.strong': 1.0,
    'exposure.trend_score.up': 0.8,
    'exposure.trend_score.neutral': 0.6,
    'exposure.trend_score.down': 0.3,

    # ── Intraday 추가 임계값 ──
    'regime.intraday_vol_window': 12,
    'regime.intraday_surge_threshold': 3.0,
    'regime.intraday_crisis_cum_ret': -0.02,
    'regime.intraday_stress_cum_ret': -0.01,
    'regime.intraday_stress_exposure': 0.7,
    'regime.intraday_highvol_exposure': 0.5,
    'regime.intraday_crisis_exposure': 0.2,
    'regime.recovery_w_reversal': 0.4,
    'regime.recovery_w_positive': 0.3,
    'regime.recovery_positive_threshold': 0.6,
    'regime.recovery_w_volume': 0.2,

    # ── 전환 시그널 추가 ──
    'regime.intraday_crisis_strength': 0.5,
    'regime.intraday_highvol_strength': 0.3,
    'regime.momentum_norm_divisor': 0.02,
    'regime.trigger_multiplier': 1.5,

    # ── Optimizer Fallback 비중 ──
    'optimizer.fallback.bull.S1': 0.15,
    'optimizer.fallback.bull.S2': 0.35,
    'optimizer.fallback.bull.S3': 0.20,
    'optimizer.fallback.bull.S4': 0.30,
    'optimizer.fallback.caution.S1': 0.08,
    'optimizer.fallback.caution.S2': 0.30,
    'optimizer.fallback.caution.S3': 0.25,
    'optimizer.fallback.caution.S4': 0.37,
    'optimizer.fallback.bear.S1': 0.03,
    'optimizer.fallback.bear.S2': 0.22,
    'optimizer.fallback.bear.S3': 0.30,
    'optimizer.fallback.bear.S4': 0.45,
    'optimizer.fallback.crash.S1': 0.00,
    'optimizer.fallback.crash.S2': 0.15,
    'optimizer.fallback.crash.S3': 0.35,
    'optimizer.fallback.crash.S4': 0.50,

    # ── S2 IC 기반 동적 앙상블 (A+) ──
    's2.ic_weight_window': 30,                    # IC 계산 rolling window
    's2.ic_temperature': 5.0,                     # Softmax temperature
    's2.ic_min_weight': 0.05,                     # 최소 모델 가중치
    's2.ic_max_history': 200,                     # IC 이력 최대 보관

    # ── S2 드리프트 심각도 (A+) ──
    's2.drift_calibration_threshold': 0.05,       # 캘리브레이션 에러 임계
    's2.drift_severe_accuracy': 0.45,             # 심각 드리프트 정확도
    's2.drift_moderate_accuracy': 0.55,           # 중간 드리프트 정확도
    's2.drift_severe_multiplier': 0.3,            # 심각 시 confidence 배수
    's2.drift_moderate_multiplier': 0.5,          # 중간 시 confidence 배수
    's2.drift_mild_multiplier': 0.7,              # 경미 시 confidence 배수

    # ── S2 자동 재학습 트리거 (A+) ──
    's2.retrain_accuracy_threshold': 0.45,        # 재학습 트리거 정확도
    's2.retrain_critical_threshold': 0.40,        # 긴급 재학습 정확도

    # ── S4 ICIR 팩터 유효성 검증 (A+) ──
    's4.icir_min_samples': 10,                    # IC 계산 최소 샘플
    's4.icir_strong_threshold': 0.5,              # ICIR 강한 팩터 기준
    's4.icir_weak_threshold': 0.3,                # ICIR 약한 팩터 기준
    's4.icir_strong_boost': 1.1,                  # 강한 팩터 가중치 부스트
    's4.icir_weak_cut': 0.7,                      # 약한 팩터 가중치 축소
    's4.icir_forward_days': 5,                    # 실현 수익률 측정 기간

    # ── PnL Attribution (A+) ──
    'allocation.attribution_window': 20,          # 성과 측정 기간 (거래일)
    'allocation.attribution_tx_cost': 0.0015,     # 거래비용 추정율
    'allocation.risk_cut_avg_impact': 0.005,      # 리스크 컷 평균 영향

    # ═══════════════════════════════════════════════════════
    # ★ 종합 개선: S1 β헤지 + 과제 7~12
    # ═══════════════════════════════════════════════════════

    # ── S1 β 헤지 전략 ──
    's1.hedge.enabled': True,                      # β 헤지 활성화
    's1.hedge.base_beta.bull': 0.7,                # Bull: 기본 β=0.7 (동적 조정 시작점)
    's1.hedge.base_beta.caution': 0.5,             # Caution: 기본 β=0.5
    's1.hedge.base_beta.bear': 0.3,                # Bear: 기본 β=0.3
    's1.hedge.base_beta.crash': 0.2,               # Crash: 기본 β=0.2
    's1.hedge.budget_ratio.bull': 0.30,            # Bull: 예산 30% → 헤지
    's1.hedge.budget_ratio.caution': 0.50,         # Caution: 50% → 헤지
    's1.hedge.budget_ratio.bear': 0.70,            # Bear: 70% → 헤지
    's1.hedge.budget_ratio.crash': 0.80,           # Crash: 80% → 헤지
    's1.hedge.min_amount': 500_000,                # 최소 헤지 금액 (₩50만)

    # ── 과제 9: IS 슬리피지 모델 ──
    'execution.slippage_model': 'is',              # 'fixed' or 'is' (Implementation Shortfall)
    'execution.is_base_bps': 3.0,                  # 기본 슬리피지 (bps)
    'execution.is_impact_coeff': 0.1,              # 시장충격 계수 (Almgren-Chriss)
    'execution.is_volatility_mult': 0.5,           # 변동성 승수

    # ── 과제 10: Tail Risk 헤지 예산 ──
    'risk.tail.hedge_budget_pct': 0.01,            # NAV의 1% 연간 헤지 예산
    'risk.tail.auto_hedge_regime': ['bear', 'crash'],  # 자동 헤지 레짐

    # ── 과제 11: Factor Timing ──
    's3.factor_timing.enabled': True,              # 팩터 타이밍 활성화
    's3.factor_timing.momentum_mult': 1.2,         # 양의 모멘텀 시 확대 배수
    's3.factor_timing.dampening_mult': 0.8,        # 음의 모멘텀 시 축소 배수
    's3.factor_timing.min_pnl_trigger': 0.5,       # 타이밍 발동 최소 PnL (%)

    # ── S3 ML 시너지: 매크로 기반 팩터 타이밍 ──
    's3.macro_timing_enabled': True,               # 매크로 팩터 타이밍 활성화
    's3.macro_timing_strength': 0.3,               # 타이밍 강도 (0=무시, 1=전적 반영)
    's3.vix_threshold_high': 25.0,                 # VIX 불확실성 높음 기준
    's3.vix_threshold_low': 15.0,                  # VIX 안정 기준
    's3.rate_change_threshold': 0.5,               # 금리 변화 임계 (pp)
    's3.fx_change_threshold': 2.0,                 # 환율 변화 임계 (%)

    # ── S3 ML Rank Boost ──
    's3.ml_rank_enabled': True,                    # ML 랭크 부스트 활성화
    's3.ml_rank_weight': 0.10,                     # ML 반영 비중
    's3.ml_rank_min_prob': 0.45,                   # ML prob 최소 임계
    's3.ml_rank_predictions_dir': 'data/predictions',  # predictions 디렉터리
    's3.ml_rank_divergence_penalty': 0.02,         # 다이버전스 감점
    's3.ml_rank_convergence_bonus': 0.01,          # 방향 일치 부스트

    # ── S2 앙상블 ──
    's2.ensemble_disagreement_threshold': 0.15,    # 앙상블 모델 간 std 임계값

    # ── 과제 12: Kelly 재보정 ──
    'a3.kelly_recal_enabled': True,                # 롤링 Kelly 재보정 활성화
    'a3.kelly_recal_min_trades': 50,               # 최소 거래 수
    'a3.kelly_min_fraction': 0.10,                 # Kelly 최소 비율
    'a3.kelly_max_fraction': 0.50,                 # Kelly 최대 비율

    # ── 과제 8: Correlation Decay ──
    'correlation.windows': [30, 60, 120],          # 다중 윈도우
    'correlation.alert_threshold': 0.60,           # 직교성 위반 임계값
    'correlation.critical_threshold': 0.80,        # 심각한 상관 임계값

    # ═══════════════════════════════════════════════════════
    # ★ P2 권장 개선 파라미터
    # ═══════════════════════════════════════════════════════

    # ── P2-1: Walk-Forward 자동 검증 ──
    'ml.wf_validation_window': 5,                  # OOS 검증 윈도우 (일)
    'ml.wf_min_samples': 10,                       # 최소 검증 샘플 수
    'ml.wf_acc_warning_threshold': 0.55,           # ACC 경고 임계값
    'ml.wf_ic_warning_threshold': 0.02,            # IC 경고 임계값
    'ml.wf_max_history_days': 90,                  # 검증 이력 보관 (일)
    'ml.feature_pruning_pct': 10,                  # 피처 제거 비율 (%)
    
    # ── 동적 고도화 (ML 강건성 & 시계열 검증) ──
    'ml.purged_kfold_splits': 5,                   # Purged CV 분할 수
    'ml.embargo_days': 5,                          # Purged CV 엠바고 기간 (일)

    # ── P2-DD: V6 보조 피처 감사 & 타겟 변수 ──
    'ml.excluded_features': [                      # 학습 시 제외할 피처 이름 리스트
        # ── SHAP=0 피처 (12개) — 예측력 없음 ──
        'ma5_ma20_cross',
        'volume_spike',
        'earnings_surprise',
        'usdkrw_change_5d',
        'news_sentiment_mean',
        'news_sentiment_std',
        'news_count_norm',
        'news_pos_ratio',
        'dart_insider_signal',
        'dart_buyback_signal',
        'foreign_net_buy_norm',
        'inst_net_buy_norm',
        # ── zero_variance 피처 (7개, 위와 3개 중복 제거 후 +4) ──
        'revenue_yoy',
        'earnings_qoq',
        'earnings_momentum',
        'roe_2yr_avg',
    ],
    'ml.auto_exclude_zero_features': True,         # all-zero 피처 자동 제외 활성화

    # ── IC 음수 자동 대응 (S2 ML 스트림) ──
    'ml.ic_negative_threshold': -0.1,              # IC 음수 판정 임계값
    'ml.ic_negative_days': 3,                      # IC 연속 음수 일수 기준
    'ml.ic_negative_decay': 0.5,                   # IC 음수 시 confidence 감쇄 배수
    'ml.use_automl_features': False,
    'ml.target_type': 'max_high',                  # 'max_high' | 'close_to_close' | 'ensemble'
    'ml.target_threshold_pct': 3.0,                # max_high 타겟 임계값 (%)
    'ml.close_to_close_threshold_pct': 2.0,        # close-to-close 타겟 임계값 (%)

    # ── P1-4: 모델별 하이퍼파라미터 (하드코딩 제거) ──
    'ml.hp.gbr.n_estimators': 300,
    'ml.hp.gbr.max_depth': 4,
    'ml.hp.gbr.learning_rate': 0.03,
    'ml.hp.gbr.subsample': 0.8,
    'ml.hp.gbr.min_samples_leaf': 100,

    'ml.hp.xgb.n_estimators': 300,
    'ml.hp.xgb.max_depth': 5,
    'ml.hp.xgb.learning_rate': 0.03,
    'ml.hp.xgb.subsample': 0.8,
    'ml.hp.xgb.colsample_bytree': 0.7,
    'ml.hp.xgb.min_child_weight': 100,

    'ml.hp.rf.n_estimators': 500,
    'ml.hp.rf.max_depth': 8,
    'ml.hp.rf.min_samples_leaf': 100,

    'ml.hp.lgbm.n_estimators': 300,
    'ml.hp.lgbm.max_depth': 5,
    'ml.hp.lgbm.learning_rate': 0.03,
    'ml.hp.lgbm.subsample': 0.8,
    'ml.hp.lgbm.colsample_bytree': 0.7,
    'ml.hp.lgbm.min_child_samples': 100,

    'ml.hp.catboost.iterations': 300,
    'ml.hp.catboost.depth': 5,
    'ml.hp.catboost.learning_rate': 0.03,
    'ml.hp.catboost.subsample': 0.8,
    'ml.hp.catboost.min_data_in_leaf': 100,

    # ── P1-4: 선택적 자동 튜닝 ──
    'ml.auto_tune_enabled': False,                  # RandomizedSearchCV 활성화 플래그
    'ml.auto_tune_n_iter': 20,                      # 탐색 반복 횟수
    'ml.auto_tune_cv_folds': 3,                     # CV fold 수
    'ml.auto_tune_scoring': 'roc_auc',              # 튜닝 스코어링 메트릭
    'ml.auto_tune_top_k': 3,                        # top-K 파라미터 세트 저장

    # ── P1-5: 앙상블 가중치 동적화 ──
    'ml.ensemble_weighting': 'equal',               # 'equal' | 'oos_auc' | 'oos_acc'

    # ── P2-2: Multi-asset 확장 ──
    'universe.markets': ['KR'],                     # 활성 마켓 ['KR', 'US']
    'universe.kr_universe_file': 'universe_918.json',
    'universe.us_enabled': False,                   # 미국 시장 활성화
    'universe.us_tickers': [],                      # US 종목 리스트
    'universe.us_data_source': 'yfinance',          # US 데이터 소스
    'universe.us_fx_hedge': True,                   # 환헤지 적용

    # ── P2-3: Slippage 모델 고도화 ──
    'slippage.base_bps': 3.0,                      # 기본 스프레드 (bps)
    'slippage.impact_coefficient': 10.0,           # 시장충격 계수
    'slippage.impact_exponent': 0.5,               # √ 모델 지수
    'slippage.default_impact_bps': 5.0,            # ADV 미제공 시 기본값
    'slippage.small_cap_threshold': 500_000_000_000,  # 소형주 기준 시총
    'slippage.small_cap_premium_bps': 3.0,         # 소형주 할증 (bps)
    'slippage.volatile_open_start': '09:00',       # 개장 변동 구간 시작
    'slippage.volatile_open_end': '09:15',         # 개장 변동 구간 끝
    'slippage.volatile_close_start': '15:15',      # 마감 변동 구간 시작
    'slippage.volatile_close_end': '15:30',        # 마감 변동 구간 끝
    'slippage.open_multiplier': 1.5,               # 개장 슬리피지 배수
    'slippage.close_multiplier': 1.3,              # 마감 슬리피지 배수
    'slippage.midday_multiplier': 1.0,             # 장중 슬리피지 배수
    'slippage.regime_multiplier.bull': 0.9,        # BULL 슬리피지 감소
    'slippage.regime_multiplier.caution': 1.0,     # CAUTION 기본
    'slippage.regime_multiplier.bear': 1.3,        # BEAR 슬리피지 증가
    'slippage.regime_multiplier.crash': 2.0,       # CRASH 슬리피지 2배
    'slippage.max_total_bps': 50.0,                # 슬리피지 상한

    # ── P2-4: 대시보드 WebSocket ──
    'dashboard.ws_enabled': False,                  # WS 실시간 업데이트
    'dashboard.ws_interval_sec': 5,                 # WS 업데이트 주기 (초)

    # ── P2-6: 환경별 설정 ──
    'env.current': 'production',                    # dev/staging/production

    # ── P2-7: Structured Logging ──
    'logging.structured_enabled': True,             # JSON 로그 활성화
    'logging.level': 'INFO',                        # 기본 로그 레벨
    'logging.metrics_enabled': True,                # 메트릭 로그 활성화

    # ── P2-8: API Rate Limiter ──
    'rate_limit.kis.rate': 10,                     # KIS: 초당 10건
    'rate_limit.kis.capacity': 20,
    'rate_limit.pykrx.rate': 2,                    # pykrx: 초당 2건
    'rate_limit.pykrx.capacity': 5,
    'rate_limit.yfinance.rate': 1,                 # yfinance: 초당 1건
    'rate_limit.yfinance.capacity': 3,

    # ── P2-5: 활성 스트림 목록 ──
    'streams.active': ['S1', 'S2', 'S3', 'S4'],

    # ═══════════════════════════════════════════════════════
    # ★ S1 동적 Regime Confidence Threshold
    # ═══════════════════════════════════════════════════════
    # VIX + OIS + 포트폴리오 S1 비중 기반 동적 임계값 계산
    # 공식: threshold = base + vix_adj + ois_adj + entry_adj
    #   vix_adj  = (vix - vix_neutral) × vix_scale
    #   ois_adj  = -(|ois - 50| / 50) × ois_discount
    #   entry_adj = initial_entry_discount (S1 비중=0일 때)
    's1.regime_conf.base_threshold': 0.35,       # 기본 임계값
    's1.regime_conf.vix_neutral': 18.0,          # VIX 중립 기준선
    's1.regime_conf.vix_scale': 0.01,            # VIX 1pt당 임계값 변화
    's1.regime_conf.ois_discount': 0.10,         # OIS 극단성 최대 할인
    's1.regime_conf.initial_entry_discount': 0.05,  # S1 비중=0 초기 진입 할인
    's1.regime_conf.floor': 0.20,                # 임계값 절대 하한
    's1.regime_conf.ceiling': 0.60,              # 임계값 절대 상한

    # ═══════════════════════════════════════════════════════
    # ★ S1 GAP ML — 갭 방향 예측 (US 야간 데이터 기반)
    # ═══════════════════════════════════════════════════════
    # SP500/VIX/환율/금리 변동 → KR 시초가 갭 방향 예측
    # 규칙 기반 가중 스코어링 + 레짐 정합성 부스트
    's1.gap_ml_enabled': True,                     # 갭 ML 예측 활성화
    's1.gap_ml_weight': 0.20,                      # ML 갭 예측 반영 비중 (confidence 부스트)
    's1.gap_sp500_weight': 0.35,                   # SP500 야간 수익률 가중치
    's1.gap_vix_weight': 0.25,                     # VIX 변화율 가중치
    's1.gap_fx_weight': 0.20,                      # USD/KRW 환율 변화 가중치
    's1.gap_rates_weight': 0.20,                   # US10Y 금리 변화 가중치
    's1.gap_nasdaq_weight': 0.15,                  # 나스닥 변화율 가중치 (보조)
    's1.gap_sp500_corr': 0.65,                     # SP500-KR갭 상관계수 (베이스라인)
    's1.gap_vix_spike_threshold': 3.0,             # VIX 급등 판정 기준 (1d 변화%)
    's1.gap_fx_strong_threshold': 0.5,             # 원화 약세 판정 기준 (%변화)
    's1.gap_rates_move_threshold': 2.0,            # 금리 급변 판정 기준 (1d 변화%)
    's1.gap_flat_zone': 0.10,                      # flat 판정 스코어 범위 (±)
    's1.gap_min_probability': 0.35,                # 최소 확률 하한
    's1.gap_max_probability': 0.90,                # 최대 확률 상한
    's1.gap_regime_align_boost': 0.08,             # 레짐 정합 시 confidence 부스트
    's1.gap_regime_conflict_penalty': 0.05,        # 레짐 충돌 시 confidence 감점
    's1.gap_investor_flow_weight': 0.10,           # 외인/기관 수급 가중치

    # ═══════════════════════════════════════════════════════
    # ★ Portfolio-Level β Hedge (Option C) — 레거시 호환
    # 새 키들은 위 'hedge.*' 블록에서 중앙 관리.
    # hedge.base_beta.* = 동적 β 조정의 시작점 (구 hedge.target_beta)
    # ═══════════════════════════════════════════════════════

    # ═══════════════════════════════════════════════════════
    # ★ GAP ANALYSIS — 예측 vs 실현 격차 진단 (#6)
    # ═══════════════════════════════════════════════════════
    'gap.min_trades': 10,                          # 분석 최소 거래 수
    'gap.confidence_bins': [0.4, 0.6],             # 신뢰도 구간 경계

    # ═══════════════════════════════════════════════════════
    # ★ CHALLENGER MODEL — 기존 vs 신규 모델 OOS 비교 (#7)
    # ═══════════════════════════════════════════════════════
    'ml.challenger_min_improvement': 0.005,        # AUC 최소 개선폭 (채택 기준)

    # ═══════════════════════════════════════════════════════
    # ★ WALK-FORWARD SPLIT 진단 (#8)
    # ═══════════════════════════════════════════════════════
    'ml.wf_weak_split_threshold': 0.50,            # 하위 split ACC 임계값

    # ═══════════════════════════════════════════════════════
    # ★ S1 OIS ML — 다변량 방향성 확률 예측 (#4)
    # ═══════════════════════════════════════════════════════
    # OIS + US10Y + VIX + Credit Spread + 수익률 곡선
    # → KR 시장 방향성 (risk_on / risk_off / neutral) 예측
    's1.ois_ml_enabled': True,                      # OIS ML 예측 활성화
    's1.ois_ml_weight': 0.15,                       # OIS ML 반영 비중
    's1.ois_risk_on_threshold': 0.6,                # risk_on 판정 스코어 임계값
    's1.ois_risk_off_threshold': 0.4,               # risk_off 판정 스코어 임계값
    's1.ois_ml_w_ois': 0.30,                        # OIS 수준 가중치
    's1.ois_ml_w_rates': 0.20,                      # US10Y 금리 가중치
    's1.ois_ml_w_vix': 0.25,                        # VIX 수준/변화 가중치
    's1.ois_ml_w_credit': 0.15,                     # 신용 스프레드 가중치
    's1.ois_ml_w_curve': 0.10,                      # 수익률 곡선 가중치
    's1.ois_ml_ois_neutral': 50.0,                  # OIS 중립 기준선
    's1.ois_ml_ois_scale': 50.0,                    # OIS 정규화 스케일
    's1.ois_ml_us10y_spike_th': 0.5,                # US10Y 급변 판정 기준 (1d변화%)
    's1.ois_ml_vix_neutral': 18.0,                  # VIX 중립 기준선
    's1.ois_ml_vix_scale': 15.0,                    # VIX 정규화 스케일
    's1.ois_ml_hy_spread_neutral': 4.0,             # HY 스프레드 중립 기준 (%)
    's1.ois_ml_hy_spread_scale': 3.0,               # HY 스프레드 정규화 스케일
    's1.ois_ml_ig_spread_neutral': 1.5,             # IG 스프레드 중립 기준 (%)
    's1.ois_ml_max_conf_adj': 0.10,                 # confidence 조정 최대폭
    's1.ois_ml_sigmoid_k': 3.0,                     # 스코어→확률 시그모이드 k

    # ═══════════════════════════════════════════════════════
    # ★ S1 동적 TP/SL — ATR + VIX + 레짐 기반 (#5)
    # ═══════════════════════════════════════════════════════
    # 기존 고정 TP/SL → 시장 상황 반영 동적 계산
    's1.dynamic_tpsl_enabled': True,                # 동적 TP/SL 활성화
    's1.base_tp_pct': 3.0,                          # 기본 TP (%)
    's1.base_sl_pct': 2.0,                          # 기본 SL (%)
    's1.tp_atr_multiplier': 1.5,                    # ATR TP 승수
    's1.atr_default_pct': 1.5,                      # ATR 기본값 (%)
    's1.atr_reference_pct': 1.5,                    # ATR 기준값 (정규화 분모)
    's1.vix_base_level': 18.0,                      # VIX 기준선 (SL 스케일링)
    's1.vix_sl_cap': 1.5,                           # VIX SL 스케일 상한
    's1.tp_bull_adj': 1.3,                          # Bull 레짐 TP 조정 (확대)
    's1.sl_bull_adj': 1.0,                          # Bull 레짐 SL 조정 (유지)
    's1.tp_bear_adj': 0.8,                          # Bear 레짐 TP 조정 (축소)
    's1.sl_bear_adj': 0.7,                          # Bear 레짐 SL 조정 (축소)
    's1.tp_crash_adj': 0.6,                         # Crash 레짐 TP 조정
    's1.sl_crash_adj': 0.5,                         # Crash 레짐 SL 조정
    's1.tp_floor_pct': 0.5,                         # TP 절대 하한 (%)
    's1.tp_ceiling_pct': 8.0,                       # TP 절대 상한 (%)
    's1.sl_floor_pct': 0.3,                         # SL 절대 하한 (%)
    's1.sl_ceiling_pct': 5.0,                       # SL 절대 상한 (%)

    # ═══════════════════════════════════════════════════════
    # ★ S4 ADVISORY — 절세계좌 파라미터
    # ═══════════════════════════════════════════════════════
    's4.isa_tax_free_limit': 2_000_000,             # ISA 비과세 한도 (원)
    's4.isa_tax_rate_excess': 0.099,                # ISA 비과세 초과분 세율 (9.9%)
    's4.irp_risk_asset_limit': 0.30,               # IRP 위험자산 한도 (30%)
    's4.irp_tax_credit_limit': 7_000_000,          # IRP 세액공제 한도 (원, 2024 기준)
    's4.irp_tax_credit_rate': 0.165,               # IRP 세액공제율 (16.5% = 총급여 5500만↓)
    's4.pension_risk_asset_limit': 0.70,           # 개인연금 위험자산 한도 (70%)
    's4.pension_tax_credit_limit': 6_000_000,      # 개인연금 세액공제 한도 (원, 2024 기준)
    's4.pension_tax_credit_rate': 0.165,           # 개인연금 세액공제율 (16.5%)
    's4.telegram_debounce_mins': 43200,            # S4 Advisory 텔레그램 쿨다운 (분, 기본 30일)

    # S4 ETF 배당률 (연간 %, 유니버스 기본값)
    's4.etf_dividend_rates': {
        'kodex_dividend': 4.5,
        'tiger_dividend': 3.8,
        'tiger_covered_call': 7.0,
        'tiger_us_dividend': 3.5,
        'kodex_us_dividend': 8.0,
        'tiger_div_growth': 2.5,
        'kodex_div_growth': 2.8,
        'tiger_us_div_grow': 3.0,
        'tiger_nasdaq': 0.7,
        'kodex_sp500': 1.3,
        'kr_bond': 3.2,
        'us_bond': 2.5,
        'gold': 0.0,
    },

    # ═══════════════════════════════════════════════════════
    # ★ S2 ML ALPHA — 마켓 시간 / EV 범위 / VIX 폴백
    # ═══════════════════════════════════════════════════════
    's2.pre_market_hour': 8,                        # 프리마켓 시작 시각 (시)
    's2.after_market_hour': 20,                     # 에프터마켓 종료 시각 (시)
    's2.ev_clamp_win_max': 0.20,                   # 동적 EV avg_win 상한
    's2.ev_clamp_win_min': 0.005,                  # 동적 EV avg_win 하한
    's2.ev_clamp_loss_max': 0.15,                  # 동적 EV avg_loss 상한
    's2.ev_clamp_loss_min': 0.005,                 # 동적 EV avg_loss 하한

    # ═══════════════════════════════════════════════════════
    # ★ VIX 폴백 기본값 (signal_cache 미존재 시)
    # ═══════════════════════════════════════════════════════
    's1.vix_fallback_default': 18.0,               # S1 VIX 기본값
    's2.vix_fallback_default': 18.0,               # S2 VIX 기본값
    's3.vix_fallback_default': 18.0,               # S3 VIX 기본값

    # ── ★ S2 ML 동적 TP/SL (진단 개선) ──
    's2.exit.max_loss_pct': -10.0,                 # ★ 최대 손실 강제 청산 (%)
    's2.exit.vol_baseline': 18.0,                  # 변동성 스케일 기준
    's2.exit.tp.bull': 15,                         # TP: bull
    's2.exit.tp.caution': 12,                      # TP: caution
    's2.exit.tp.bear': 8,                          # TP: bear
    's2.exit.tp.crash': 5,                         # TP: crash
    's2.exit.tp_floor': 5,                         # TP 최소값
    's2.exit.sl.bull': -5,                         # SL: bull
    's2.exit.sl.caution': -5,                      # SL: caution
    's2.exit.sl.bear': -4,                         # SL: bear
    's2.exit.sl.crash': -3,                        # SL: crash
    's2.exit.sl_ceiling': -2,                      # SL 최대값(상한)
    's2.exit.trailing_trigger': 5,                 # Trailing Stop 발동 수익률
    's2.exit.trailing_pct': 3,                     # Trailing Stop 하락폭
    's2.exit.max_hold.bull': 20,                   # 최대 보유기간 (bull)
    's2.exit.max_hold.caution': 15,                # 최대 보유기간 (caution)
    's2.exit.max_hold.bear': 10,                   # 최대 보유기간 (bear)
    's2.exit.max_hold.crash': 7,                   # 최대 보유기간 (crash)

    # ★ S2 거래비용 감안 부분 익절 (2026-06-10)
    # 부분 익절 트리거 = max(비용보전선, TP × ratio)
    # 비용보전선 = 매도수수료 + 세금(주식만) + 최소 순이익
    's2.exit.sell_commission_pct': 0.015,           # 매도 수수료 (%)
    's2.exit.sell_tax_pct': 0.18,                   # 증권거래세 (%, 주식만, ETF면제)
    's2.exit.net_min_profit_pct': 1.0,              # 부분 익절 최소 순수익 (%)
    # S3/S4도 동일 구조 (s3.exit.sell_commission_pct 등으로 오버라이드 가능)
    's2.filter_placeholder_features': True,         # ★ placeholder feature 자동 필터
    's2.placeholder_features': [                    # ★ 제거할 placeholder feature 목록
        'news_sentiment', 'dart_disclosure', 'foreign_flow',
        'institutional_flow', 'short_interest', 'options_flow',
    ],

    # ── ★ S1 Edge 동적 TP/SL (당일 청산) ──
    # ★ TP:SL 비율 = 2.5:1.25 = 2.0:1 → 승률 45%에서도 E[R] > 0
    # 이전: 2.0:1.5 = 1.33:1 → 승률 50%에서 간신히 E[R] > 0
    's1.exit.tp_vol_multiplier': 2.5,              # TP = σ_daily × 2.5 × leverage
    's1.exit.sl_vol_multiplier': 1.25,             # SL = σ_daily × 1.25 × leverage
    's1.exit.tp_floor': 0.5,                       # TP 하한 (%)
    's1.exit.tp_ceiling': 5.0,                     # TP 상한 (%)
    's1.exit.sl_floor': -0.3,                      # SL 하한 (%)
    's1.exit.sl_ceiling': -3.0,                    # SL 상한 (%)
    's1.exit.max_hold_minutes': 360,               # 최대 보유시간 (분)

    # ── ★ S5 Overnight Stream ──
    's5.tp_pct': 1.0,                              # S5 오버나이트 TP (%)
    's5.sl_pct': -1.5,                             # S5 오버나이트 SL (%)
    's5.max_hold_minutes': 1200,                   # S5 오버나이트 최대 보유 (분, ~익일 오전)


    # ── ★ S3 Factor 동적 TP/SL (중기) ──
    's3.exit.vol_baseline': 18.0,                  # 변동성 스케일 기준
    's3.exit.tp.bull': 18,                         # TP: bull (%)
    's3.exit.tp.caution': 15,                      # TP: caution
    's3.exit.tp.bear': 10,                         # TP: bear
    's3.exit.tp.crash': 7,                         # TP: crash
    's3.exit.tp_floor': 5,                         # TP 최소값
    's3.exit.sl.bull': -7,                         # SL: bull
    's3.exit.sl.caution': -7,                      # SL: caution
    's3.exit.sl.bear': -5,                         # SL: bear
    's3.exit.sl.crash': -4,                        # SL: crash
    's3.exit.sl_ceiling': -3,                      # SL 최대값(상한)
    's3.exit.early_rebalance_pct': -5,             # 조기 리밸런싱 트리거

    # ── ★ S4 Advisory 동적 TP/SL (장기) ──
    's4.exit.tp_base': 30,                         # TP 기본값 (%)
    's4.exit.sl_base': -12,                        # SL 기본값 (%)
    's4.exit.trailing_tp_trigger': 10,             # Trailing TP 발동 수익률
    's4.exit.trailing_tp_pct': 15,                 # Trailing TP 하락폭
    's4.exit.regime_tp_mult.bull': 1.2,            # 레짐별 TP 배수: bull
    's4.exit.regime_tp_mult.bear': 0.7,            # 레짐별 TP 배수: bear
    's4.exit.qv_decay_threshold': 30,              # QV Score Decay 임계값 (하위 %)
    's4.exit.max_hold.bull': 180,                  # 최대 보유기간 (bull)
    's4.exit.max_hold.caution': 120,               # 최대 보유기간 (caution)
    's4.exit.max_hold.bear': 90,                   # 최대 보유기간 (bear)

    # ═══════════════════════════════════════════════════════
    # ★ GAP ANALYSIS — 실패 패턴 임계값
    # ═══════════════════════════════════════════════════════
    'gap.high_conf_fail_ratio': 0.3,               # 고신뢰도 실패 비율 임계값
    'gap.concentration_fail_count': 3,             # 스트림 집중 실패 최소 건수
    'gap.consecutive_loss_threshold': 3,           # 연속 손실 경고 임계값
    'gap.consecutive_loss_retrain': 5,             # 연속 손실 재학습 권고 임계값
    'gap.min_sector_trades': 3,                    # 레짐 미스매치 최소 거래 수

    # ═══════════════════════════════════════════════════════
    # ★ TRAIN ENSEMBLE — 기본 파라미터
    # ═══════════════════════════════════════════════════════
    'train.window_days': 730,                       # Rolling Window 크기 (일)
    'train.forward_days': 5,                        # 라벨 생성 미래 기간 (일)
    'train.sample_interval': 5,                     # 샘플 간격 (일)

    # ═══════════════════════════════════════════════════════
    # ★ 신규 추가 키 (Phase 8~10 수정 결과 반영) — SSoT 완성
    # ═══════════════════════════════════════════════════════

    # ── Portfolio 보완 ──
    'portfolio.trading_days_per_month': 21,         # 월간 영업일 (kill_switch M2-02)
    'portfolio.panic_sell_slippage': 0.005,         # Kill Switch 패닉셀 슬리피지 (0.5%)

    # ── MarketShockDetector 임계값 (M2-19) ──
    'shock.threshold.us_sp500_moderate': -0.02,     # S&P500 -2% → MODERATE
    'shock.threshold.us_sp500_severe': -0.04,       # S&P500 -4% → SEVERE
    'shock.threshold.us_nasdaq_moderate': -0.025,   # NASDAQ -2.5% → MODERATE
    'shock.threshold.us_nasdaq_severe': -0.05,      # NASDAQ -5% → SEVERE
    'shock.threshold.vix_moderate': 25.0,           # VIX > 25 → MODERATE
    'shock.threshold.vix_severe': 35.0,             # VIX > 35 → SEVERE
    'shock.threshold.futures_moderate': -0.015,     # 야간선물 -1.5% → MODERATE
    'shock.threshold.futures_severe': -0.03,        # 야간선물 -3% → SEVERE
    'shock.threshold.usdkrw_moderate': 0.01,        # USD/KRW +1% → MODERATE
    'shock.threshold.usdkrw_severe': 0.02,          # USD/KRW +2% → SEVERE

    # ── DefensiveAlphaFinder 임계값 (M2-23) ──
    'defensive.low_beta_max': 0.7,                  # Low-beta 기준: β < 0.7
    'defensive.avg_vol_min': 50_000,                # 유동성 필터: 평균 거래량 > 50,000주
    'defensive.qgd_score_min': 50,                  # QGD 최소 종합점수
    'defensive.qgd_vol_min': 30_000,                # QGD 유동성 필터

    # ── OIS CSV 컬럼 인덱스 (M2-21) ──
    'ois.csv.col_sp500': 1,                         # realtime_sentiment.csv S&P500 열
    'ois.csv.col_nasdaq': 2,                        # NASDAQ 열
    'ois.csv.col_vix': 8,                           # VIX 열
    'ois.csv.col_fear_greed': 9,                    # Fear&Greed 열

    # ── VKOSPI / HY 장기 기준값 (H2-16, H2-17) ──
    'regime.vkospi_baseline': 18.0,                 # VKOSPI 장기 평균
    'regime.vkospi_baseline_std': 6.0,              # VKOSPI 장기 표준편차
    'regime.hy_baseline': 5.0,                      # HY 스프레드 장기 평균 (%)
    'regime.hy_baseline_std': 2.0,                  # HY 스프레드 장기 표준편차

    # ── Watchdog 설정 (동적화) ──
    'watchdog.freshness_grace_minutes': 1440,       # Stale 판정 grace 기간 (분)
    'watchdog.max_runtime_minutes': 30,             # 프로세스 최대 허용 실행 시간 (분)
    'watchdog.krx_refresh_max_minutes': 20,         # KRX refresh 최대 시간
    'watchdog.backtest_max_minutes': 30,            # 백테스트 최대 시간
    'watchdog.collect_max_minutes': 15,             # collect phase 최대 시간

    # ── RealtimeExitMonitor initial_capital (M2-07) ──
    'monitor.initial_capital': 154_000_000,         # 모니터링 기준 자본 (initial_capital과 동기)

    # ── Walk-Forward OOS dispatch (CRITICAL fix) ──
    'backtest.wf_oos_dispatch': True,               # 2-arg strategy_fn OOS dispatch 활성화

    # ── EWMA 파라미터 (M-13/M-15 통합) ──
    'risk.ewma_lookback': 60,                       # EWMA 분산 추정 최대 lookback (일)
    'risk.ewma_init_days': 10,                      # EWMA 초기값 추정 최소 일수

    # ═══════════════════════════════════════════════════════
    # ★ KillSwitch Edge-Triggered Alert + Gap-Aware NAV Reset
    # ═══════════════════════════════════════════════════════

    # ── Edge-Triggered 쿨다운 ──
    'kill_switch.alert_cooldown_hours': 4.0,        # 동일 트리거 재발송 최소 간격 (시간)
    #   - 0: 쿨다운 없음 (매 검사마다 발송 — 스팸)
    #   - 4: 4시간마다 재알림 (권장: 장 중 1~2회)
    #   - 24: 1일 1회 재알림 (보수적)

    # ── Gap-Aware NAV Reset ──
    'kill_switch.system_gap_max_days': 3,           # 시스템 갭 판정 기준 (거래일)
    #   - 시스템이 이 일수 이상 꺼져있다가 재시작되면
    #   - daily_returns 마지막 값(갭 기간 누적 수익률)을 일간 손실 계산에서 제외
    #   - 10일 공백 후 -7.40% 기록 → 킬스위치 가짜 발동 방지

    # ── Panic Sell 스트림별 면제 ──
    'kill_switch.panic_sell_exempt_streams': ['S4'],
    #   - Kill Switch 발동 시 패닉셀에서 제외할 스트림 목록
    #   - S4: 장기 QV/배당 전략 (ISA·연금 계좌) — 단기 낙폭에 의한 강제 청산 시
    #         기회비용 손실 + 세금 불이익 발생 → 킬스위치 발동에도 보유 유지
    #   - [] 로 설정 시 모든 스트림 패닉셀 적용 (기존 동작과 동일)
    #   - 예: ['S4', 'H'] — S4·헷지 모두 면제

    # ══════════════════════════════════════════════════════════════
    # Slippage & Market Impact 고도화 (Almgren-Chriss + POV + Fill Sim)
    # ══════════════════════════════════════════════════════════════

    # ── Phase 1: σ(Volatility) 동적 연결 ──
    'slippage.vol_lookback_days': 20,               # 변동성 계산 롤링 윈도우 (거래일)
    'slippage.default_daily_vol': 0.02,             # ADV/변동성 미조회 시 폴백 σ (2%)
    #   - 실 데이터 없으면 이 값을 σ로 사용
    #   - 중형주 평균 일간 변동성 수준

    # ── Phase 2: POV (Percentage of Volume) 전략 ──
    'execution.pov_rate': 0.05,                     # POV 참여율 (시장 거래량 대비 5%)
    #   - 실시간 시장 거래량의 5%까지만 주문
    #   - 기관 Signaling 은닉 (Stealth Execution)
    'execution.pov_adv_threshold': 0.05,            # POV 전환 기준 (ADV 대비 주문 비율)
    #   - 주문이 ADV의 5% 이상이면 POV 고려
    'execution.pov_max_duration_min': 120,          # POV 최대 실행 허용 시간 (분)
    #   - 이 시간 내에 완료 못하면 VWAP으로 전환

    # ── Phase 3: Fill Rate 시뮬레이션 (지정가 체결확률) ──
    'execution.fill_sim_order_type': 'limit',       # 'limit' or 'market'
    #   - 'limit': 보수적 백테스트 (다음 봉 Low/High로 체결 판정)
    #   - 'market': 기존 방식 (항상 100% 체결)
    'execution.fill_sim_limit_discount_bps': 5.0,   # 매수 지정가 = 현재가 - 5bps
    #   - 매수: 현재가보다 약간 낮게 지정 (슬리피지 최소화 시도)
    #   - 매도: 현재가보다 약간 높게 지정 (최적화)
    'execution.fill_sim_window_candles': 20,        # 체결확률 추정 과거 봉 수 (20일)
    #   - estimate_fill_probability() 계산 기준 봉 수

    # ── Phase 5: TCA per-ticker EWMA Impact History ──
    'execution.tca_ewma_days': 10,                  # 종목별 market impact EWMA 기간
    #   - 0에 가까울수록 최근 값 반영 빠름
    #   - AlgoExecutor Adaptive 피드백 루프 연동,


    # ─────────────────────────────────────────────────────────────────
    # [Phase 17: Ultimate Boosters] S6-B ISA Advisory 모드 & K-ETF 매핑
    # ─────────────────────────────────────────────────────────────────
    's6b.isa_mode':             True,      # ISA Advisory 모드 (True=텔레그램 알림만)
    's6b.leverage_vix_safe':    18.0,      # VIX < 18: 2배수 레버리지 ETF 허용
    's6b.leverage_vix_exit':    25.0,      # VIX > 25: 1배수로 즉시 강등
    's6b.ticker_map_qqq_1x':   '133690',  # TIGER 미국나스닥100 (1배)
    's6b.ticker_map_qqq_2x':   '490330',  # TIGER 미국나스닥100레버리지(합성) (2배)
    's6b.ticker_map_spy':      '379800',  # KODEX 미국S&P500
    's6b.ticker_map_tlt':      '304660',  # KODEX 미국채울트라30년선물(H)
    's6b.min_order_krw':        100_000,  # 최소 주문 금액 (10만원)
    # ─────────────────────────────────────────────────────────────────
    # [Phase 17: Ultimate Boosters] Kelly Sizing 동적 한도
    # ─────────────────────────────────────────────────────────────────
    'kelly.s2_proba_threshold':     0.90,  # S2 극단 고확신 임계값 (predict_proba)
    'kelly.s2_cap_normal':          0.05,  # S2 일반 단일자산 캡 (5%)
    'kelly.s2_cap_high_conviction': 0.15,  # S2 고확신 캡 해제 한도 (15%)
    'kelly.s2_cap_extreme':         0.20,  # S2 극단 고확신 최대 한도 (20%)
    'kelly.s3_zscore_threshold':    2.5,   # S3 섹터 Z-Score 강세 임계값
    'kelly.s3_cap_normal':          0.05,  # S3 일반 섹터 ETF 캡 (5%)
    'kelly.s3_cap_momentum':        0.12,  # S3 강세 모멘텀 캡 (12%)
    # ─────────────────────────────────────────────────────────────────
    # [Phase 17: Ultimate Boosters] 알트코인 차익거래 확장 (S6-A)
    # ─────────────────────────────────────────────────────────────────
    's6a.alt_universe':           ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'ADA'],
    's6a.alt_min_market_cap_usd':  10_000_000_000,  # 100억달러(≈1,000억원) 이상
    's6a.alt_min_funding_annual':  5.0,   # 유지 기준 연환산 펀딩비 5% 이상
    's6a.alt_roll_funding_exit':   5.0,   # 역방향 롤오버: 연 5% 이하 → 즉시 청산
    's6a.margin_danger_ratio':     0.85,  # 마진 위험 수위 (85%)
    's6a.margin_critical_ratio':   0.90,  # 마진 긴급 수위 (90%) → ADL 강제청산
    's6a.adl_confirm_count':       3,     # Flash-spike 필터: 3회 연속 확인
    's6a.adl_confirm_interval_s':  15,   # 연속 확인 간격 (초)
    's6a.api_ping_timeout_s':      0.1,  # API Health Check 타임아웃 (100ms)
    's6a.order_timeout_s':         5.0,  # 주문 타임아웃 (초)
    's6a.retry_max_attempts':      3,    # 재시도 최대 횟수 (지수 백오프)
    's6a.retry_base_wait_s':       1.0,  # 첫 재시도 대기 (초)
    # [Phase 17: Ultimate Boosters] 하드코딩 정밀 제거 — 추가 파라미터
    's6b.vix_fallback_default':   20.0,  # VIX 데이터 누락 시 fallback 기본값 (중립 레벨)
    's6b.tlt_shift_ratio':         0.5,  # VIX 방어 삭감분 중 TLT로 전환하는 비율 (50%)
    'kelly.s2_proba_high_band':    0.05, # S2 고확신 구간 = proba_threshold - high_band
    's6a.alt_roll_interval_h':     8.0,  # 알트코인 롤오버 점검 주기 (시간)

    # ═══════════════════════════════════════════════════════
    # ★ SS-ETF 단일종목 레버리지/인버스 ETF 유동성 팩터
    # ═══════════════════════════════════════════════════════
    # 2026-05-27 KODEX/TIGER 단일종목 ETF 상장 이후 발생하는
    # 'Wag-the-Dog Effect' (파생발 투기적 변동성) 감지·방어용.
    # 장 후반(15:20~) 변동성 40~50% 폭증의 구조적 원인 식별.

    # ── 상장일 (Graceful Fallback 기준) ──
    'ss_etf.listing_date': '20260527',          # 단일종목 ETF 상장일 (YYYYMMDD)

    # ── ETF 티커 매핑 (pykrx 조회 기준) ──────────────────────
    # ★ 실제 KRX 부여 코드로 교체 필요 시 dynamic_overrides.json 사용
    'ss_etf.samsung.lev_ticker': '470450',       # KODEX 삼성전자 레버리지
    'ss_etf.samsung.inv_ticker': '470460',       # KODEX 삼성전자 인버스
    'ss_etf.hynix.lev_ticker':   '470480',       # TIGER SK하이닉스 레버리지
    'ss_etf.hynix.inv_ticker':   '470490',       # TIGER SK하이닉스 인버스

    # ── Feature 이름 (ML 컬럼명 — 변경 시 기존 모델 재학습 필요) ──
    'ss_etf.feature.vol_ratio_name':    'ss_etf_vol_ratio',    # 웩더독 강도 지표
    'ss_etf.feature.lp_pressure_name':  'lp_delta_pressure',   # LP 델타 헤징 압력
    'ss_etf.feature.vol_anomaly_name':  'intraday_vol_anomaly',# 일중 변동성 이상치

    # ── 수식 파라미터 ──────────────────────────────────────────
    # ss_etf_vol_ratio = (lev_vol + inv_vol) / underlying_vol
    #   값 > threshold_high → 장막판 파생 주도 변동성 위험 HIGH
    'ss_etf.vol_ratio_threshold_caution':  0.15,  # 주의: 본주 거래량 대비 15%
    'ss_etf.vol_ratio_threshold_warning':  0.30,  # 경고: 30% (높은 Wag-the-Dog 위험)
    'ss_etf.vol_ratio_threshold_critical': 0.50,  # 위험: 50% (구조적 변동성 위험)

    # lp_delta_pressure = (lev_retail_net - inv_retail_net) / scale
    #   양수 큰 값 → LP 장막판 기초자산 매도 헤징 압력
    'ss_etf.lp_pressure_scale':         1_000_000,  # 백만원 단위 정규화
    'ss_etf.lp_pressure_threshold_buy': -500.0,     # 개인 인버스 매수 우세 (장막판 기초자산 매수 압력)
    'ss_etf.lp_pressure_threshold_sell': 500.0,     # 개인 레버리지 매수 우세 (장막판 기초자산 매도 압력)

    # intraday_vol_anomaly = today_HL / rolling_N_avg_HL
    #   1.5 이상: 오늘 변동폭이 평균 대비 50% 이상 이상 → 파생 수급 이상 의심
    'ss_etf.vol_anomaly_window_days':        14,    # 일중 변동성 이상치 롤링 윈도우 (영업일)
    'ss_etf.vol_anomaly_threshold_caution': 1.3,    # 30% 이상 → 주의
    'ss_etf.vol_anomaly_threshold_warning': 1.5,    # 50% 이상 → 경고

    # ── ML 통합 설정 ───────────────────────────────────────────
    'ss_etf.enabled_streams':   ['S1', 'S2'],       # SS-ETF Feature를 주입할 스트림
    'ss_etf.merge_ticker_col':  'ticker',            # ML DataFrame 티커 컬럼명
    'ss_etf.merge_date_col':    'date',              # ML DataFrame 날짜 컬럼명
    'ss_etf.impute_value':      0.0,                 # 결측치/상장전 Impute 값 (NaN→0.0)
    'ss_etf.cache_ttl_hours':   6,                   # 수집 캐시 유효 시간 (시간)

    # ── S1 Edge 통합: 장막판 Wag-the-Dog 진입 금지 ────────────
    # lp_delta_pressure > threshold_sell AND 장 후반(close_window) → S1 신규 진입 차단
    'ss_etf.s1_entry_block_pressure_threshold': 300.0,    # LP 매도 압력 ≥ 300M원 → 진입 차단
    'ss_etf.s1_close_window_start':             '15:10',  # 장 후반 시작 (S1 진입 차단 기준)
    'ss_etf.s1_close_window_end':               '15:30',  # 장 종료

    # ── S2 ML 통합: 파생 변동성 레짐 보정 ───────────────────────
    # vol_ratio > threshold_warning → S2 Confidence에 penalty 적용
    's2.ss_etf_vol_penalty':        0.05,    # ss_etf_vol_ratio 고수 시 confidence 감쇄 (5%)
    's2.ss_etf_anomaly_penalty':    0.03,    # intraday_vol_anomaly 고수 시 penalty (3%)

    # ═══════════════════════════════════════════════════════
    # ★ Alpha Factory v2 — Genetic Programming 하이퍼파라미터
    # ═══════════════════════════════════════════════════════
    # gplearn 기반 자가 발전형 알파 탐색 엔진.
    # 모든 값을 dynamic_overrides.json으로 런타임 오버라이드 가능.

    # ── GP 핵심 하이퍼파라미터 ────────────────────────────────
    'alpha_factory.population_size':        500,    # 인구 크기 (크면 탐색 넓음, 느림)
    'alpha_factory.generations':             20,    # 세대 수 (크면 수렴 깊음, 느림)
    'alpha_factory.p_crossover':            0.70,   # 교차(Crossover) 확률
    'alpha_factory.p_subtree_mutation':     0.10,   # 서브트리 돌연변이 확률
    'alpha_factory.p_hoist_mutation':       0.05,   # Hoist 돌연변이 확률
    'alpha_factory.p_point_mutation':       0.10,   # Point 돌연변이 확률
    'alpha_factory.max_samples':            0.90,   # 부트스트랩 샘플 비율
    'alpha_factory.parsimony_coefficient':  0.01,   # 수식 복잡도 패널티 (값 크면 단순 수식 선호)
    'alpha_factory.stopping_criteria':      0.01,   # 조기 종료 fitness 임계값
    'alpha_factory.random_state':           42,     # 재현성 시드
    'alpha_factory.n_jobs':                 -1,     # 병렬 CPU 수 (-1=전부)
    'alpha_factory.hall_of_fame':           20,     # Hall of Fame 크기 (최우수 수식 보존 수)
    'alpha_factory.top_k_programs':          5,     # OOS 평가할 Top-K 프로그램 수

    # ── 함수 집합 (수식에 허용할 연산자) ────────────────────────
    'alpha_factory.function_set': [
        'add', 'sub', 'mul', 'div',
        'sqrt', 'log', 'abs', 'neg', 'inv',
        'max', 'min',
    ],

    # ── OOS IC 평가 파라미터 ──────────────────────────────────
    # Rank IC(Spearman) 기준: 알파 신호 vs 다음날 수익률 상관관계
    'alpha_factory.ic_threshold':       0.05,   # OOS IC 최소 기준 (미달 시 기각)
    'alpha_factory.oos_test_ratio':     0.30,   # OOS 검증 데이터 비율 (30%)
    'alpha_factory.oos_n_splits':        3,     # Time-Series Cross-Validation 분할 수

    # ── 직교화 필터 (다중공선성 차단) ────────────────────────────
    # 기존 피처와 Pearson Correlation 절대값이 이 이상이면 기각
    'alpha_factory.corr_threshold':     0.70,   # 직교화 기각 임계값 (|r| ≥ 0.70)

    # ── 알파 붕괴(Decay) 추적 ────────────────────────────────────
    'alpha_factory.ic_decay_window':    30,     # IC 추적 롤링 윈도우 (일)
    'alpha_factory.ic_decay_threshold': 0.02,   # 이 이하로 떨어지면 'retired' 처리

    # ── 데이터 로드 설정 ──────────────────────────────────────
    'alpha_factory.sample_tickers': [           # 알파 탐색용 대표 종목 (유동성 최상위)
        '005930',   # 삼성전자
        '000660',   # SK하이닉스
        '035420',   # NAVER
        '005380',   # 현대차
        '051910',   # LG화학
    ],
    'alpha_factory.active_features':    [],     # 사용할 피처 화이트리스트 (빈 리스트=자동)
    'alpha_factory.max_features_per_ticker': 30,# 종목당 최대 피처 수
    'alpha_factory.forward_return_days':  1,    # target: N일 미래 수익률

    # ── 파이프라인 주입 설정 ──────────────────────────────────
    'alpha_factory.max_inject_alphas':  10,     # ML DF에 주입할 최대 알파 수
    'alpha_factory.inject_enabled':     True,   # v4_features inject 활성화 여부

    # ── AlphaTranslator ──────────────────────────────────────
    'alpha_factory.translator_df_prefix': 'df', # eval() 내 DataFrame 변수명

    # ─── [Phase 36] 장중 동적 조정 (Intraday Dynamic Adjustment) ─────────
    "intraday.flow_fetch_interval_min":   10,
    "intraday.flow_trend_rider_ratio":    1.3,
    "intraday.flow_panic_ratio":          0.6,
    "intraday.whipsaw_filter_enabled":    True,
    "intraday.whipsaw_volume_threshold":  0.3,
    "intraday.flow_strong_threshold_krw": 5_000_000_000,
    "intraday.panic_price_drop_pct":      0.015,
    "intraday.default_watch_tickers":     ["005930", "000660"],
    # ─── [Phase 36] S1 장중 돌파 시그널 ─────────────────────────────────────
    "s1_breakout.volume_spike_threshold": 3.0,
    "s1_breakout.min_foreign_flow_krw":   5_000_000_000,
    "s1_breakout.breakout_size_pct":      0.05,
    "s1_breakout.active_window_start":    "09:30",
    "s1_breakout.active_window_end":      "10:30",
    "s1_breakout.max_signals_per_day":    2,
    "s1_breakout.regime_override_enabled": True,    # caution 레짐에서도 돌파 매수 허용 여부

    # ─── [Phase 36] 추가 동적 파라미터 ───────────────────────────────────────
    # Whipsaw: 수급 중립 판정 비율 (strong_threshold 대비 이 비율 미만 = 미미한 수급)
    "intraday.whipsaw_neutral_ratio":     0.2,
    # Flow Collector: 티커 간 호출 딜레이 (Rate-Limit 방어, 초)
    "intraday.ticker_api_delay_sec":      0.25,
    # Flow Collector: 원화 환산 단위 (1 = 원, 1_000_000 = 백만원)
    "intraday.flow_unit_krw":             1_000_000,
    # Flow Collector: 최대 배치 조회 종목 수
    "intraday.max_batch_tickers":         20,
    # Exit: 기본 Stop-Loss 비율 (DynamicConfig 미설정 시 Fallback)
    "intraday.default_sl_pct":            0.07,
    # Exit: 기본 Trailing Stop ATR 배수 (DynamicConfig 미설정 시 Fallback)
    "intraday.default_ts_atr_mult":       1.5,
    # Exit: Panic Tightener urgency
    "intraday.panic_urgency":             3,


    # ─────────────────────────────────────────────────────────────────────────
    # [Phase 37: Moonshot Booster System]
    # ─────────────────────────────────────────────────────────────────────────

    # ── Task 2: Kelly Booster ────────────────────────────────────────────────
    "kelly.ois_lookback_days":          60,    # OIS 롤링 중앙값 기준 이력 일수
    "kelly.confidence_percentile":      75,    # ML Confidence 임계값 퍼센타일
    "kelly.confidence_lookback":        30,    # Confidence 이력 보관 일수
    "kelly.booster_cash_ratio":         0.0,   # Booster ON 시 현금 비율 (0%)
    "kelly.trail_atr_multiplier":       1.5,   # Booster ON 시 트레일링 스탑 배수 (기본 2.0보다 타이트)
    "kelly.rollover_days":              5,     # 기존 포지션 자연 청산 대기 최대 일수
    "kelly.max_pos_low_corr":           5,     # 상관계수 < 0.3 시 최대 종목 수
    "kelly.max_pos_mid_corr":           3,     # 상관계수 0.3~0.6 시 최대 종목 수
    "kelly.max_pos_high_corr":          2,     # 상관계수 >= 0.6 시 최대 종목 수
    "kelly.max_pos_fallback":           3,     # 상관행렬 없을 때 fallback 종목 수

    # ── Task 1: S6-B VIX 기어변속 + US Direct ───────────────────────────────
    "a1.hard_limit_close_minute":       20,    # A1 Hard Limit 강제 청산 분 (15:20)
    "s6b.vix_gear_p_low":               40,    # TQQQ 허용 VIX 퍼센타일 (P40)
    "s6b.vix_gear_p_high":              70,    # SQQQ/TLT 전환 VIX 퍼센타일 (P70)
    "s6b.vix_tqqq_abs_max":             20.0,  # TQQQ 허용 VIX 절대값 상한 (안전망)
    "s6b.vix_sqqq_abs_min":             25.0,  # SQQQ 진입 VIX 절대값 하한 (안전망)
    "s6b.vix_fallback_default":         20.0,  # VIX 데이터 없을 때 보수적 fallback
    "s6b.us_direct_budget_ratio.bull":   0.35, # 야간 US Direct 예산 비율 (bull)
    "s6b.us_direct_budget_ratio.caution": 0.20,# 야간 US Direct 예산 비율 (caution)
    "s6b.us_direct_budget_ratio.bear":   0.10, # 야간 US Direct 예산 비율 (bear)
    "s6b.us_direct_budget_ratio.crash":  0.05, # 야간 US Direct 예산 비율 (crash)
    "s6b.us_direct_default_budget":     20_000_000,  # 잔고 조회 실패 시 fallback 예산
    "s6b.tlt_enabled":                  True,  # VIX >= P70 시 TLT 방어 허용

    # ── Task 3: S6-A 크립토 레버리지 & ADL ──────────────────────────────────
    # NOTE: s6a.binance_leverage 는 ADL 방어망 완성 후 1 -> 3으로 변경
    "s6a.adl_safety_factor":            0.7,   # ADL 트리거 공식의 안전 계수
    "s6a.adl_action_ratio":             0.5,   # ADL 발동 시 포지션 축소 비율
    "s6a.adl_consecutive_limit":        2,     # 연속 ADL 발동 횟수 초과 시 부스터 해제
    "s6a.surplus_base_upbit_ratio":     0.70,  # 잉여 달러 중 업비트 재배치 최대 비율
    "s6a.surplus_reserve_ratio":        0.10,  # 잉여 달러 중 시스템 유보 고정 비율

    # ════════════════════════════════════════════════════════════════════════════
    # [Phase 40: Institutional Defense] 기관급 방어 및 체결 고도화
    # ════════════════════════════════════════════════════════════════════════════

    # Task 1: S2 ML Alpha 성과 기반 자동 예산 몰수 (Auto-Fallback to Factor)
    's2.ic_lookback_days':      5,        # 최근 N일 IC 추적 창
    's2.wr_threshold':          0.40,     # WR 임계치 (이하 시 penalty 발동)
    's2.ic_threshold':         -0.02,     # IC 임계치 (이하 시 penalty 발동)
    's2.penalty_ratio':         0.20,     # penalty 적용 후 S2 예산 배수 (0.2x)
    's2.fallback_target_s3a':   0.60,     # 잉여 → S3-A Macro 이관 비율
    's2.fallback_target_s3b':   0.40,     # 잉여 → S3-B Value 이관 비율

    # Task 2: S6-B 프리마켓 블랙스완 방어 (Pre-market Black Swan Defense)
    's6b.premarket_nq_drop_pct':      -1.5,  # NQ 선물 하락 차단 임계치 (%)

    # [Phase 42: VIX-Fix] VX=F 단종 → VIXY 변화율 기반 이중 방어망
    # 레거시 절대값 키 ('s6b.premarket_vix_spike': 15.0) 폐기 — VIXY 변화율로 대체
    's6b.premarket_vixy_spike_pct':   5.0,  # VIXY(VIX 단기 선물 ETF) 급등 차단 임계치 (%)
    #   계산식: (VIXY 현재가 - 전일 종가) / 전일 종가 × 100 ≥ 5% → TQQQ 차단
    #   * VX=F 절대값(15.0pt) 기준 완전 대체 — 스케일 오류 없음
    's6b.vol_target_vix14':       3.0,   # Volatility Target: VIX 14 → 3x 레버리지
    's6b.vol_target_vix16':       2.0,   # Volatility Target: VIX 16 → 2x 레버리지
    's6b.vol_target_vix18':       1.0,   # Volatility Target: VIX 18 → 1x (de-lever)

    # Task 3: S1/S5 체결 슬리피지 방어 (Slippage Mitigation)
    's1.adtv_min_billion':       50.0,   # 20일 평균 거래대금 최소 (억 원)
    's1.spread_max_pct':          0.5,   # 최대 허용 호가 스프레드 (%)
    's1.mid_price_retry_sec':    60,     # Mid-Price 미체결 시 정정 대기 (초)
    's1.adtv_lookback_days':     20,     # ADTV 산출 기간 (거래일)

    # Task 4: Stale Data Kill Switch & Consensus Algorithm
    'data.stale_threshold_min':  20,     # Stale 판정 기준 (분 — 이 이상 과거면 Halt)
    'data.bad_tick_sigma':        5.0,   # Bad Tick Z-Score 임계치 (이상이면 배제)
    'data.consensus_min_sources': 2,     # 합의 최소 소스 수 (미달 시 Halt)
    'data.stale_alert_enabled':   True,  # Stale 감지 시 텔레그램 알림 여부

    # ════════════════════════════════════════════════════════════════════════════
    # [Phase 41: US Dual-Phase Split] 프리마켓/본장 이원화 스케줄링
    # ════════════════════════════════════════════════════════════════════════════

    # DST 적용 시 KST 기준 트리거 시각
    'us.dst_premarket_kst':    '17:30',  # 서머타임: 프리마켓 페이즈 시작 (KST)
    'us.dst_regular_kst':      '22:30',  # 서머타임: 본장 페이즈 시작 (KST)

    # DST 해제 시 KST 기준 트리거 시각
    'us.nodst_premarket_kst':  '18:30',  # 표준시: 프리마켓 페이즈 시작 (KST)
    'us.nodst_regular_kst':    '23:30',  # 표준시: 본장 페이즈 시작 (KST)

    # 멱등성 시간 윈도우 (분) — 이 시간 내에 실행 요청이 들어오면 1회만 실행
    'us.phase_window_min':     25,       # 페이즈 실행 허용 윈도우 (분)
    'us.idempotency_lock_dir': 'logs',   # 멱등성 락 파일 저장 디렉토리

    # S6-B 프리마켓 전용 설정
    's6b.session_premarket_enabled': True,   # 프리마켓에서 S6-B 시그널 생성
    's6b.session_regular_enabled':   True,   # 본장 시간에 S6-B 재실행 여부
    's6b.premarket_order_tag':       '[PRE-MARKET]',  # 로그 태그

    # S5 세션 라우팅
    's5.session_premarket_action':   'dip_watch',    # 프리마켓: 딥 대기만
    's5.session_regular_action':     'directional',  # 본장: 주력 방향성 매매
    's5.regular_order_tag':          '[REGULAR]',    # 로그 태그

    # KIS 프리마켓 주문 코드 (KIS UAPI 공식)
    'kis.premarket_ord_dvsn':        '32',   # KIS 프리마켓: IOC 지정가
    'kis.regular_ord_dvsn_limit':    '00',   # KIS 본장: 지정가
    'kis.regular_ord_dvsn_market':   '01',   # KIS 본장: 시장가



    # [Step 3: Freshness Gate] market_calendar 기반 동적 stale 기준
    'data.freshness_stale_h':          20.0,  # 평일 영업일 기준 stale 시간 (시간)
    'data.freshness_holiday_buffer_h':  4.0,  # 공휴일 경과시간 버퍼 (시간)
    # ── [Phase 43: Zero-Tolerance Execution Architecture] ──────────────────
    'execution.max_drift_pct':       3.0,   # State Drift 임계치 (%) — 초과 시 Kill Switch
    'execution.emergency_page_on':   True,  # Emergency Pager 활성화
    'execution.anti_spam_retry_max': 1,     # 주문 통신 에러 최대 재시도 횟수
    'execution.max_retries':         3,     # [패치 ❌→✅] 부분체결 재시도 최대 횟수 (execution_engine.py L757 참조)

    # ── 신용융자 이자율 관련 (CreditRateLoader) ──
    'cost.credit_interest_rate':  0.085,   # 기본 연 8.5% (한투 단기 30일 대표값)
    'cost.credit_free_ratio':     1.0,     # 이자 없는 자본 비율 (1.0=레버리지 없음)
    'cost.credit_holding_days':   30,      # 보유 일수 (CreditRateLoader 연간 이자율 산출에 사용)
    'execution.broker':           'kis',   # 증권사 코드 ('kis', 'kb', 'samsung')
}



class DynamicConfig:
    """동적 설정 관리자.

    모든 파라미터를 중앙에서 관리하고, JSON 오버라이드를 지원.

    Usage:
        cfg = DynamicConfig()
        sl = cfg.get('exit.sl_atr_multiplier')          # 기본값
        sl = cfg.get('exit.sl_atr_multiplier', 2.5)     # 커스텀 기본값
        cfg.set('exit.sl_atr_multiplier', 2.5)          # 런타임 변경
        cfg.save_overrides()                             # 디스크 저장

    동적 오버라이드:
        results/dynamic_overrides.json에 키-값 쌍을 저장하면
        기본값보다 우선 적용됩니다.
    """

    _instance = None

    def __new__(cls) -> 'DynamicConfig':
        """싱글톤 패턴."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._defaults = dict(_DEFAULTS)
        self._overrides: Dict[str, Any] = {}
        self._runtime: Dict[str, Any] = {}

        # ★ P2-6: 환경 프로파일 (dev/staging/prod)
        import os
        self._env = os.environ.get('MERIDIAN_ENV', 'production')
        self._load_overrides()
        self._load_env_profile()
        self._initialized = True

    def _load_env_profile(self):
        """환경별 설정 오버라이드 로드.

        파일 우선순위:
          1. config/env_{env}.json (환경별 설정)
          2. results/dynamic_overrides.json (런타임)
          3. _DEFAULTS (기본값)
        """
        env_file = _PROJECT_ROOT / 'config' / f'env_{self._env}.json'
        if env_file.exists():
            try:
                env_overrides = json.loads(env_file.read_text())
                # 환경 오버라이드는 기본 오버라이드보다 낮은 우선순위
                merged = dict(env_overrides)
                merged.update(self._overrides)  # runtime > env
                self._overrides = merged
                logger.info(
                    f"  DynamicConfig: env={self._env}, "
                    f"{len(env_overrides)}개 환경 설정 로드")
            except Exception as e:
                logger.debug(f"  환경 프로파일 로드 실패: {e}")

    @property
    def environment(self) -> str:
        """현재 환경 프로파일."""
        return self._env

    def _load_overrides(self):
        """JSON 파일에서 오버라이드 로드."""
        if _OVERRIDES_FILE.exists():
            try:
                self._overrides = json.loads(_OVERRIDES_FILE.read_text())
                logger.info(f"  DynamicConfig: {len(self._overrides)}개 오버라이드 로드")
            except Exception as e:
                logger.warning(f"  DynamicConfig 오버라이드 로드 실패: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """파라미터 조회. 우선순위: runtime > overrides > defaults > default arg."""
        if key in self._runtime:
            return self._runtime[key]
        if key in self._overrides:
            return self._overrides[key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value: Any):
        """런타임 파라미터 변경."""
        self._runtime[key] = value

    def get_regime_param(self, base_key: str, regime: str, default: Any = None) -> Any:
        """레짐별 파라미터 조회.

        예: get_regime_param('exit.max_hold_days', 'bull') → 'exit.max_hold_days.bull'
        """
        return self.get(f'{base_key}.{regime}', default)

    def get_allocation(self, sleeve: str, regime: str) -> list:
        """슬리브별 레짐 배분 비율 조회."""
        return self.get(f'{sleeve}.allocation.{regime}', [0.25, 0.25, 0.25, 0.10, 0.15])

    def update_from_market_state(self, market_state: Dict):
        """시장 매크로/리스크 데이터를 반영하여 내부 임계값 동적 업데이트 (Medallion / Bridgewater Style)."""
        vix = market_state.get('vix')
        vix_baseline = market_state.get('vix_20d_avg')
        intraday_vol = market_state.get('intraday_volatility')
        if intraday_vol is None:
            intraday_vol = vix
            
        ois_score = market_state.get('ois_score')
        usdkrw = market_state.get('usdkrw')
        usdkrw_baseline = market_state.get('usdkrw_prev')
        us10y = market_state.get('us10y')
        us2y = market_state.get('us2y')
        foreign_flow = market_state.get('foreign_flow')
        foreign_baseline = market_state.get('foreign_flow_baseline')
        regime = market_state.get('regime', 'bull')
        conf = market_state.get('regime_confidence')
        if conf is None:
            conf = 0.5
        mdd = market_state.get('portfolio_mdd')
        if mdd is None:
            mdd = 0.0
        
        # 1. Medallion-style Intraday Risk Scaling
        vix_factor = 1.0
        if intraday_vol is not None and vix_baseline is not None and vix_baseline > 0:
            vix_factor = max(0.5, min(2.5, intraday_vol / vix_baseline))
        
        self.set('exit.sl_atr_multiplier', 2.0 * vix_factor)
        self.set('exit.tp_atr_multiplier', 3.5 * vix_factor)
        
        base_kelly = 0.25
        kelly_scale = max(0.2, min(1.0, 1.0 - (vix_factor - 1.0) * 0.5))
        if regime in ['bear', 'crash']:
            kelly_scale *= 0.5
        self.set('sizer.kelly_fraction', base_kelly * kelly_scale)
        
        base_kill_dd = -8.0
        if regime == 'crash':
            base_kill_dd = -5.0 + (1.0 - conf) * 3.0
        elif mdd < -5.0: # MDD가 이미 깊다면 추가 손실 방지를 위해 타이트하게
            base_kill_dd = max(-10.0, mdd - 2.0)
            
        # Clamp killswitch DD
        base_kill_dd = max(-8.0, min(-3.0, base_kill_dd))
        self.set('killswitch.drawdown_liquidate_pct', base_kill_dd)

        # 2. Bridgewater-style Macro / Cost of Capital Adjustments
        ois_penalty = 0.0
        if ois_score is not None:
            # OIS Score: 0~100 (50 기준). 점수가 낮을수록 시장 상태 악화 -> 허들 상향
            ois_penalty = max(0.0, (50.0 - ois_score) * 0.001) # 10점 하락당 0.01 (1%) 허들 증가
        
        is_inverted = False
        if us10y is not None and us2y is not None:
            is_inverted = (us10y - us2y) < 0
        spread_penalty = 10.0 if is_inverted else 0.0
        
        fx_stress = False
        if usdkrw is not None and usdkrw_baseline is not None and usdkrw_baseline > 0:
            fx_stress = usdkrw > usdkrw_baseline * 1.01
        
        # 외인 수급 모멘텀 (전일 대비 급격한 매수세)
        foreign_momentum = False
        if foreign_flow is not None and foreign_baseline is not None:
            foreign_momentum = foreign_flow > foreign_baseline + 2000.0
        
        base_qv = 30.0
        base_up_prob = 0.60
        
        new_up_prob = base_up_prob
        new_qv = base_qv
        
        if regime == 'bull':
            new_qv = base_qv - 10.0 + spread_penalty
            new_up_prob = base_up_prob - 0.02
        elif regime == 'caution':
            new_qv = base_qv + spread_penalty
            new_up_prob = base_up_prob + ois_penalty * 0.5
        elif regime in ['bear', 'crash']:
            new_qv = base_qv + 10.0 + spread_penalty
            new_up_prob = base_up_prob + 0.05 + ois_penalty
            if fx_stress:
                self.set('fundamental.min_equity_ratio', 0.20)
                
        # Clamp up probability and QV score
        new_up_prob = max(0.50, min(0.85, new_up_prob))
        new_qv = max(10.0, min(60.0, new_qv))
        
        self.set('fundamental.min_qv_score', new_qv)
        self.set('a3.min_up_probability', new_up_prob)
        
        # Foreign Flow (외인 수급) -> 단순 절대값이 아닌 평소(baseline) 대비 강도로 변경
        if foreign_flow is not None and foreign_baseline is not None:
            if foreign_flow > foreign_baseline + 2000:
                self.set('allocation.conf_sizing.high_mult', 1.8) # 비중 확대
            elif foreign_flow < foreign_baseline - 2000:
                self.set('allocation.conf_sizing.high_mult', 1.2) # 비중 축소
            
        ois_log = f"{ois_score:.1f}" if ois_score is not None else "N/A"
        
        # [Phase 60] Option B: Defense Factor -> Kelly + max_position_pct
        _df60 = float(market_state.get("defense_factor", 1.0))
        if _df60 < 1.0:
            _ck = float(self.get("sizer.kelly_fraction", 0.25))
            _nk = round(_ck * _df60, 6)
            self.set("sizer.kelly_fraction", _nk)
            for _pk in ("sizer.max_position_pct",
                        "s1.max_position_pct","s2.max_position_pct",
                        "s3.max_position_pct","s4.max_position_pct",
                        "s5.max_position_pct"):
                _cv = float(self.get(_pk, 0.0))
                if _cv > 0:
                    self.set(_pk, round(_cv * _df60, 6))
            logger.info(f"  [Phase60 OptionB] df={_df60:.4f} kelly {_ck:.4f}->{_nk:.4f}")
        logger.info(f"  [DynamicConfig] 🔄 Market state applied (VIX Factor: {vix_factor:.2f}x, OIS: {ois_log}, Regime: {regime})")

    def reload(self):
        """오버라이드 파일을 다시 로드하고 런타임 변경 사항을 초기화."""
        self._overrides = {}
        self._runtime = {}
        self._load_overrides()
        logger.info("  DynamicConfig: reloaded")

    def save_overrides(self):
        """현재 오버라이드를 디스크에 저장."""
        try:
            _OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
            merged = {**self._overrides, **self._runtime}
            _OVERRIDES_FILE.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False, default=str)
            )
            logger.info(f"  DynamicConfig: {len(merged)}개 오버라이드 저장")
        except Exception as e:
            logger.warning(f"  DynamicConfig 저장 실패: {e}")

    def all_params(self) -> Dict[str, Any]:
        """모든 파라미터 (합산) 반환."""
        merged = dict(self._defaults)
        merged.update(self._overrides)
        merged.update(self._runtime)
        return merged

    def diff_from_defaults(self) -> Dict[str, Any]:
        """기본값과 다른 파라미터만 반환."""
        merged = {**self._overrides, **self._runtime}
        return {k: v for k, v in merged.items() if v != self._defaults.get(k)}

    def audit_config(self) -> Dict[str, Any]:
        """미사용 키 감사 보고서 생성.

        config/deprecated_keys.json을 읽어서 미사용 키 통계 반환.
        DD 권고 #6: 343개 키 중 222개(65%)가 코드에서 미참조.
        이 키들은 Phase 1 Project_First에서 이관된 레거시 파라미터.

        Returns:
            {'total': 343, 'used': 121, 'unused': 222,
             'unused_pct': 64.7, 'top_unused_prefixes': [...]}
        """
        deprecated_file = _PROJECT_ROOT / 'config' / 'deprecated_keys.json'
        if deprecated_file.exists():
            try:
                data = json.loads(deprecated_file.read_text())
                unused_keys = data.get('unused_keys', {})
                # 프리픽스별 집계
                from collections import Counter
                prefixes = Counter()
                for k in unused_keys:
                    prefixes[k.split('.')[0]] += 1
                top = [{'prefix': p, 'count': c}
                       for p, c in prefixes.most_common(10)]
                return {
                    'total': data.get('total_keys', len(self._defaults)),
                    'used': data.get('used_count', 0),
                    'unused': data.get('unused_count', 0),
                    'unused_pct': round(
                        data.get('unused_count', 0) /
                        max(data.get('total_keys', 1), 1) * 100, 1),
                    'top_unused_prefixes': top,
                    'note': 'Phase 1 레거시 키. 향후 정리 예정.',
                }
            except Exception:
                pass
        return {'total': len(self._defaults), 'audit': 'deprecated_keys.json 없음'}

    @staticmethod
    def project_root() -> Path:
        """프로젝트 루트 경로."""
        return _PROJECT_ROOT

    def __repr__(self) -> str:
        diff = self.diff_from_defaults()
        return f"DynamicConfig(defaults={len(self._defaults)}, overrides={len(diff)})"


# ═══════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════

def get_config() -> DynamicConfig:
    """싱글톤 DynamicConfig 인스턴스."""
    return DynamicConfig()


if __name__ == '__main__':
    cfg = DynamicConfig()
    print(f"Project Root: {cfg.project_root()}")
    print(f"Config: {cfg}")
    print(f"\nSample params:")
    for key in ['exit.min_tp_sl_ratio', 'a3.min_up_probability',
                'risk.total_dd_limit', 'portfolio.target_annual_return']:
        print(f"  {key}: {cfg.get(key)}")

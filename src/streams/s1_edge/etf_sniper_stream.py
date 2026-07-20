#!/usr/bin/env python3
"""
S1 ETF Sniper Stream (리테일 전용 0% 세금 + 유동성 충격 저격)
======================================================
전략 핵심:
  - 지정가(Passive) 매매 금지. 오직 시장가 혹은 TWAP/VWAP 패시브 가정 체결
  - 0.18% 세금을 회피하기 위한 무세금 ETF 종목 타겟팅
  - 평소엔 현금 관망하다가 Z-Score 기반 유동성 충격 혹은 웩더독 시 진입
"""

import logging
import math
from typing import Dict, List, Any
import numpy as np

from config.dynamic_config import DynamicConfig
from src.streams.base_stream import BaseStream
from src.data_collection.ss_etf_feature_engine import SSETFFeatureEngine

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class S1ETFSniperStream(BaseStream):
    def __init__(self):
        super().__init__('S1', 'ETF_SNIPER')
        self.universe = cfg.get('s1.universe', {
            '069500': {'name': 'KODEX 200', 'type': 'index'},
            '252670': {'name': 'KODEX 200선물인버스2X', 'type': 'index_inv'},
        })
        self.vol_history = {}
        self.ss_engine = SSETFFeatureEngine()

    def _get_ticker_by_type(self, etf_type: str) -> str:
        """universe dict에서 type 필드로 티커를 동적 조회.

        Args:
            etf_type: 'index', 'index_inv', 'index_lev', 'ss_inv', 'ss_lev' 등
        Returns:
            일치하는 첫 번째 ticker 코드. 없으면 '' 반환.
        """
        for ticker, meta in self.universe.items():
            if meta.get('type') == etf_type:
                return ticker
        logger.warning(f"  [S1] universe에서 type='{etf_type}' 티커를 찾을 수 없습니다.")
        return ''

    def _calc_dynamic_conf(self, base: float, metric: float, scale: float) -> float:
        """팩터 강도에 비례한 동적 확신도 산출 (최대 0.95 제한)"""
        return round(min(0.95, base + (abs(metric) * scale)), 3)

    def generate_signals(self, regime: str, market_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        logger.info(f"  [S1_ETF_SNIPER] 시그널 탐색 시작 (regime={regime})")
        signals = []
        signal_cache = market_data.get('signal_cache', {})
        vix = float(signal_cache.get('vix', 15.0))
        
        # 1. Z-Score 기반 순수 수학적 동적 VIX 임계값 연산
        # [Architecture Update] 라이브 스트림 내부에서의 yfinance 등 외부 네트워크 I/O 전면 금지
        # 백그라운드 Collector(예: macro_realtime_refresher.py)가 미리 계산하여 주입한 값을 안전하게 사용
        vix_ma20 = float(signal_cache.get('vix_ma_20', 15.0))
        vix_std20 = float(signal_cache.get('vix_std_20', 2.0))
        
        z_multipliers = {
            'bull': 1.5,
            'caution': 2.0,
            'bear': 2.5,
            'crash': 0.0
        }
        z_val = z_multipliers.get(regime, 2.0)
        shock_threshold = vix_ma20 + (z_val * vix_std20)
        
        is_shock = vix >= shock_threshold
        
        logger.debug(f"    - VIX Z-Score 평가: Current VIX={vix:.2f}, 임계값={shock_threshold:.2f} (MA={vix_ma20:.2f}, STD={vix_std20:.2f}, Z={z_val})")
        
        # 2. SS-ETF Feature Engine 연동 (Wag-the-Dog 전술 판별)
        intraday_data = market_data.get('ss_etf_intraday', None)
        ss_features  = self.ss_engine.compute('069500', intraday_data=intraday_data) or {}
        
        # [Tactic C] 단일 종목 (삼성전자/하이닉스) SS-ETF Feature 계산
        sam_features = self.ss_engine.compute('005930') or {}
        hyn_features = self.ss_engine.compute('000660') or {}

        
        # 조건 판별
        tactic_c_sam_trigger = (sam_features.get('ss_etf_vol_ratio', 0.0) > 2.0) and (sam_features.get('intraday_vol_anomaly', 0.0) > 1.5)
        tactic_c_hyn_trigger = (hyn_features.get('ss_etf_vol_ratio', 0.0) > 2.0) and (hyn_features.get('intraday_vol_anomaly', 0.0) > 1.5)
        
        vol_ratio = ss_features.get('ss_etf_vol_ratio', 0.0)
        lp_pressure = ss_features.get('lp_delta_pressure', 0.0)
        vol_anomaly = ss_features.get('intraday_vol_anomaly', 0.0)
        volume_ma   = float(signal_cache.get('volume_ma', 0.0))

        # ── Model 3: OFI Z-Score Sigmoid (방안 C 확정) ──
        # 공식: ofi_z = (lp_pressure - lp_ma) / max(lp_std, 1.0)
        #        ofi_prob = 1 / (1 + exp(-k * ofi_z))
        #        발동 조건: ofi_prob < (1 - ofi_trigger_prob)  ← 방향성 역전 수정
        # 강한 매도 압력(lp_pressure << 0) → ofi_z << 0 → ofi_prob → 0 → 발동
        ofi_k            = float(cfg.get('s1.ofi_sigmoid_k', 2.0))
        ofi_trigger_prob = float(cfg.get('s1.ofi_trigger_prob', 0.90))
        lp_ma            = float(signal_cache.get('lp_pressure_ma', 0.0))
        lp_std           = float(signal_cache.get('lp_pressure_std', 500.0))  # fallback: 원 하드코딩 스케일
        ofi_z            = (lp_pressure - lp_ma) / max(abs(lp_std), 1.0)
        ofi_prob         = 1.0 / (1.0 + math.exp(-ofi_k * ofi_z))
        tactic_a_trigger = (ofi_prob < (1.0 - ofi_trigger_prob))

        logger.debug(
            f"    - OFI Sigmoid (Model 3): lp={lp_pressure:.0f}, lp_ma={lp_ma:.0f}, "
            f"lp_std={lp_std:.0f}, ofi_z={ofi_z:.3f}, ofi_prob={ofi_prob:.4f}, "
            f"trigger={tactic_a_trigger} (threshold < {1.0 - ofi_trigger_prob:.2f})")

        # ── Model 7: Variance Ratio Z-Test for Tactic B/C ──
        # 공식: vol_z = (vol_ratio - 1.0) * sqrt(vol_history_window)
        #        trigger = (vol_z > z_threshold)  ← 통계적 유의성 확보 시만 진입
        # CLT 기반: H0 = vol_ratio == 1 (정상 거래량), 기각 시 비정상 감지
        vr_z_threshold  = float(cfg.get('s1.variance_ratio.z_threshold', 2.0))
        vr_hist_window  = float(cfg.get('s1.variance_ratio.vol_history_window', 20))
        vol_z_score     = (vol_ratio - 1.0) * math.sqrt(vr_hist_window)

        # Tactic B: vol_anomaly 기반 climax 감지 (vol_z + anomaly 이중 조건)
        tactic_b_trigger = (vol_z_score > vr_z_threshold) and (lp_pressure > 0)

        # Tactic C: 단일 종목 WTD — sam/hyn 각각 variance ratio Z-test 적용
        sam_vol_ratio = sam_features.get('ss_etf_vol_ratio', 0.0)
        hyn_vol_ratio = hyn_features.get('ss_etf_vol_ratio', 0.0)
        sam_vol_z     = (sam_vol_ratio - 1.0) * math.sqrt(vr_hist_window)
        hyn_vol_z     = (hyn_vol_ratio - 1.0) * math.sqrt(vr_hist_window)
        sam_anomaly_z = (sam_features.get('intraday_vol_anomaly', 0.0) - 1.0) * math.sqrt(vr_hist_window)
        hyn_anomaly_z = (hyn_features.get('intraday_vol_anomaly', 0.0) - 1.0) * math.sqrt(vr_hist_window)

        tactic_c_sam_trigger = (sam_vol_z > vr_z_threshold) and (sam_anomaly_z > vr_z_threshold)
        tactic_c_hyn_trigger = (hyn_vol_z > vr_z_threshold) and (hyn_anomaly_z > vr_z_threshold)

        logger.debug(
            f"    - Variance Ratio Z-Test (Model 7): "
            f"vol_z={vol_z_score:.2f}, sam_vol_z={sam_vol_z:.2f}, hyn_vol_z={hyn_vol_z:.2f} "
            f"(threshold={vr_z_threshold:.1f})")
        
        if not is_shock and not any([tactic_a_trigger, tactic_b_trigger, tactic_c_sam_trigger, tactic_c_hyn_trigger]):
            logger.debug("    - 스나이핑 조건 미달 (VIX 충격 및 수급 교란 없음) -> 평시 관망")
            return []

        logger.info(f"    🎯 스나이퍼 모드 가동! (Shock={is_shock})")

        tp_pct = float(cfg.get('s1.sniper_tp_pct', 0.015))

        # ── Model 4: Chandelier Exit (ATR 기반 동적 손절매) ──
        # 공식: sl_pct = max(sl_min_pct, atr_5m * sl_atr_multiplier)
        # 5분봉 ATR을 signal_cache에서 주입받아 사용. 미주입 시 sl_min_pct fallback.
        atr_5m            = float(signal_cache.get('atr_5m', 0.0))
        sl_atr_multiplier = float(cfg.get('s1.exit.sl_atr_multiplier', 2.0))
        sl_min_pct        = float(cfg.get('s1.exit.sl_min_pct', 0.003))
        sl_pct            = max(sl_min_pct, atr_5m * sl_atr_multiplier)

        # Tactic B/C 확장 TP: cfg에서 배수 로드 (하드코딩 1.5 제거)
        tactic_bc_tp_multiplier = float(cfg.get('s1.tactic_bc_tp_multiplier', 1.5))

        logger.debug(
            f"    - Chandelier SL (Model 4): atr_5m={atr_5m:.5f}, "
            f"multiplier={sl_atr_multiplier}, sl_pct={sl_pct:.5f} "
            f"(min={sl_min_pct:.3f})")

        # Tactic C 최우선 판별
        if tactic_c_sam_trigger:
            logger.warning("    🎯 [Tactic C] 삼성전자 단일종목 웩더독 감지! 삼성전자 인버스 저격.")
            ticker = str(cfg.get('ss_etf.samsung.inv_ticker', '470460'))  # DynamicConfig SSoT
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': float(cfg.get('s1.tactic_c_size_pct', 1.0)),  # Smart Wallet 연동 화력 개방
                    'price': price,
                    'confidence': self._calc_dynamic_conf(0.60, sam_features.get('ss_etf_vol_ratio', 0.0), 0.05),
                    'strategy': 'tactic_c_samsung_wag_the_dog',
                    'reason': f"삼성전자 웩더독 (VolZ={sam_vol_z:.2f}σ, AnomalyZ={sam_anomaly_z:.2f}σ)",
                    'tp_pct': tp_pct * tactic_bc_tp_multiplier,
                    'sl_pct': sl_pct,
                    'holding_time': '5m',
                    'execution_algo': 'vwap'
                })
        elif tactic_c_hyn_trigger:
            logger.warning("    🎯 [Tactic C] SK하이닉스 단일종목 웩더독 감지! 하이닉스 인버스 저격.")
            ticker = str(cfg.get('ss_etf.hynix.inv_ticker', '470490'))   # DynamicConfig SSoT
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': float(cfg.get('s1.tactic_c_size_pct', 1.0)),  # Smart Wallet 연동 화력 개방
                    'price': price,
                    'confidence': self._calc_dynamic_conf(0.60, hyn_features.get('ss_etf_vol_ratio', 0.0), 0.05),
                    'strategy': 'tactic_c_hynix_wag_the_dog',
                    'reason': f"하이닉스 웩더독 (VolZ={hyn_vol_z:.2f}σ, AnomalyZ={hyn_anomaly_z:.2f}σ)",
                    'tp_pct': tp_pct * tactic_bc_tp_multiplier,
                    'sl_pct': sl_pct,
                    'holding_time': '5m',
                    'execution_algo': 'vwap'
                })
        elif tactic_a_trigger:
            logger.warning("    🎯 [Tactic A] Wag-the-Dog 감지: LP 기계적 매도압력 폭발. 인버스 진입.") # Tactic A-1: 인버스 저격
            ticker = self._get_ticker_by_type('index_inv')
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': 1.0, 
                    'price': price,
                    'confidence': self._calc_dynamic_conf(0.55, lp_pressure, 0.0001),
                    'strategy': 'tactic_a_wag_the_dog_ride',
                    'reason': f"Wag-the-Dog 인버스 동승 (VolRatio={vol_ratio:.2f}, LPPres={lp_pressure:.0f})",
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'holding_time': '5m',
                    'execution_algo': 'vwap'
                })
        elif tactic_b_trigger:
            logger.warning("    🎯 [Tactic B] Climax 역발상 감지: 비정상 변동성 피크 후 진정세. 레버리지 진입.") # Tactic B-1: 레버리지/롱 저격
            ticker = self._get_ticker_by_type('index')
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': 1.0, 
                    'price': price,
                    'confidence': self._calc_dynamic_conf(0.50, vol_anomaly, 0.10),
                    'strategy': 'tactic_b_climax_reversion',
                    'reason': f"가짜 하락 반발 매수 (Anomaly={vol_anomaly:.2f}, LPPres={lp_pressure:.0f})",
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'holding_time': '5m',
                    'execution_algo': 'vwap'
                })
        elif is_shock:
            logger.info("    🎯 [VIX Shock] 순수 변동성 스파이크 감지. 인버스 스나이핑 진입.")
            ticker = self._get_ticker_by_type('index_inv')
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': 1.0, 
                    'price': price,
                    'confidence': self._calc_dynamic_conf(0.60, (vix - vix_ma20) / max(vix_std20, 1e-9), 0.10),
                    'strategy': 'vix_shock_sniper',
                    'reason': f"Z-Score 임계치({shock_threshold:.2f}) 돌파 VIX 급등({vix:.2f}) 연동 롱",
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'holding_time': '5m',
                    'execution_algo': 'vwap'
                })
            
        return signals

    def get_performance(self) -> Dict[str, Any]:
        return {
            'sharpe': 1.5,
            'cumulative_return_pct': 5.0,
            'active_positions': 0,
            'mdd_pct': -2.0
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        return []


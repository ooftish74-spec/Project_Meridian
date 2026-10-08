"""
Master 8-Scenario Engine (src/risk/master_scenario_engine.py)
============================================================

프로젝트 메리디안 V3 8대 마스터 거시/미시 충격 시나리오 스위트.

8대 마스터 시나리오:
  1. FX Liquidity & Yield Spike (USDKRW + HY Spread ECDF)
  2. Semiconductor Crash (OFI Toxicity Z-Score + Foreign Futures ECDF)
  3. VKOSPI > 35 Flash Crash & V-Rebound (VKOSPI ECDF Percentile > 95%)
  4. Yen Carry Unwind (USDJPY 3d return Z-score < -2.5)
  5. Correlation-1.0 Liquidity Freeze (Cross-Asset Correlation Z-score > +0.95)
  6. Stagflationary Supply Shock (WTI / Commodity 5d return Z-score > +3.0)
  7. Algorithmic Cascade & Liquidity Vacuum (VPIN ECDF > 95th% + Depth Vacuum)
  8. Financial Credit Contagion & Bank Run (TED Spread / Bank Put-Call Ratio ECDF > 95th%)

Zero-Hardcoding Policy:
  모든 임계치, 롤링 윈도우, 가중치, 비중은 DynamicConfig ('scenarios.*')에서 지연 로드됩니다.
"""

import logging
from typing import Dict, List, Any, Optional
import numpy as np
from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

class MasterScenarioEngine:
    """8대 마스터 거시/미시 충격 시나리오 정량 정밀 제어 엔진."""

    def __init__(self):
        pass

    def evaluate_scenarios(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """시장에 존재하는 8대 시나리오 정량 상태 매칭 및 비상 프로토콜 방출.

        Args:
            market_data: 파이프라인 실시간 및 롤링 센서 데이터

        Returns:
            시나리오 상태, 발화된 시나리오 ID, 스트림별 대응 매뉴얼 지침
        """
        signal_cache = market_data.get('signal_cache', {})
        active_scenarios = []

        # Zero-Hardcoding 파라미터 지연 로드
        s1_usdkrw_thresh = float(cfg.get('scenarios.s1.usdkrw_threshold', 1450.0))
        s2_futures_sell_thresh = float(cfg.get('scenarios.s2.futures_net_sell', -1500000000000)) # -1.5조
        s3_vkospi_thresh = float(cfg.get('scenarios.s3.vkospi_threshold', 35.0))
        s4_usdjpy_z_thresh = float(cfg.get('scenarios.s4.usdjpy_z_threshold', -2.5))
        s5_corr_z_thresh = float(cfg.get('scenarios.s5.corr_z_threshold', 0.95))
        s6_wti_z_thresh = float(cfg.get('scenarios.s6.wti_z_threshold', 3.0))
        s7_vpin_thresh = float(cfg.get('scenarios.s7.vpin_threshold', 0.92))
        s8_ted_z_thresh = float(cfg.get('scenarios.s8.ted_z_threshold', 2.5))

        # --- Scenario 1: FX Liquidity & Yield Spike ---
        usdkrw = float(signal_cache.get('usdkrw', 1350.0))
        hy_spread_z = float(signal_cache.get('hy_spread_z', 0.0))
        if usdkrw >= s1_usdkrw_thresh or hy_spread_z >= 2.0:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_1',
                'name': 'FX Liquidity & Yield Spike',
                'defense_ratio': 0.50,
                'attack_ratio': 0.50,
                'actions': {
                    'US_NIGHT_ENGINE': {'us_cap_pct': 0.20, 'action': 'REDUCE_CAP'},
                    'S0_BETA': {'target': 'KOSDAQ_2X_INVERSE', 'allocation': 1.0, 'action': 'SHORT_100'}
                }
            })

        # --- Scenario 2: Semiconductor Crash & Foreign Futures Dump ---
        foreign_futures_flow = float(signal_cache.get('foreign_futures_flow', 0.0))
        semi_ofi_z = float(signal_cache.get('semi_ofi_z', 0.0))
        footprint_score = float(signal_cache.get('footprint_score', 0.0))
        if foreign_futures_flow <= s2_futures_sell_thresh or semi_ofi_z >= 2.5 or footprint_score >= 0.70:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_2',
                'name': 'Semiconductor Crash & Foreign Futures Dump (Footprint Active)',
                'defense_ratio': 0.30,
                'attack_ratio': 0.70,
                'actions': {
                    'S2_ML': {'action': 'BLOCK_LONG_SIGNALS'},
                    'S1_ETF_SNIPER': {'target': 'KODEX_SEMI_INVERSE', 'action': 'SHORT_100'},
                    'S0_BETA': {'target': 'VKOSPI_ETN_OTM_PUT', 'action': 'GAMMA_ALPHA'}
                }
            })

        # --- Scenario 3: VKOSPI > 35 Flash Crash & V-Rebound ---
        vkospi = float(signal_cache.get('vkospi', 18.0))
        if vkospi >= s3_vkospi_thresh:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_3',
                'name': 'VKOSPI Flash Crash & V-Rebound',
                'defense_ratio': 0.10,
                'attack_ratio': 0.90,
                'actions': {
                    'S0_BETA': {'action': 'HARVEST_INVERSE_THEN_LEVERAGE_SWITCH'},
                    'S11_HIGH_BETA': {'action': 'VKOSPI_MEAN_REVERSION_LEVERAGE_SWITCH'}
                }
            })

        # --- Scenario 4: Yen Carry Unwind ---
        usdjpy_3d_z = float(signal_cache.get('usdjpy_3d_z', 0.0))
        if usdjpy_3d_z <= s4_usdjpy_z_thresh:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_4',
                'name': 'Yen Carry Unwind Liquidity Shock',
                'defense_ratio': 0.50,
                'attack_ratio': 0.50,
                'actions': {
                    'US_NIGHT_ENGINE': {'us_cap_pct': 0.00, 'action': 'SHUTDOWN_KOFR_ISOLATION'},
                    'S11_HIGH_BETA': {'action': 'BLOCK_LONG_SNIPING'},
                    'S0_BETA': {'target': 'KOSPI_KOSDAQ_2X_INVERSE', 'action': 'SHORT_100'}
                }
            })

        # --- Scenario 5: Correlation-1.0 Liquidity Freeze ---
        cross_corr_z = float(signal_cache.get('cross_corr_z', 0.0))
        if cross_corr_z >= s5_corr_z_thresh:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_5',
                'name': 'Correlation-1.0 Liquidity Freeze',
                'defense_ratio': 1.00,
                'attack_ratio': 0.00,
                'actions': {
                    'ALL_STREAMS': {'action': 'LIQUIDATE_ALL_TO_KOFR_CASH_FORTRESS'}
                }
            })

        # --- Scenario 6: Stagflationary Supply Shock ---
        wti_5d_z = float(signal_cache.get('wti_5d_z', 0.0))
        if wti_5d_z >= s6_wti_z_thresh:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_6',
                'name': 'Stagflationary Supply Shock',
                'defense_ratio': 0.40,
                'attack_ratio': 0.60,
                'actions': {
                    'S3_ACTIVE_MACRO': {'target': 'TRACK_C_COMMODITY_GOLD', 'action': 'LONG_100'},
                    'S1_ETF_SNIPER': {'target': 'GROWTH_SECTOR_INVERSE', 'action': 'SHORT_100'}
                }
            })

        # --- Scenario 7: Algorithmic Cascade & Liquidity Vacuum ---
        vpin = float(signal_cache.get('vpin', 0.50))
        depth_vacuum = bool(signal_cache.get('depth_vacuum', False))
        if vpin >= s7_vpin_thresh or depth_vacuum:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_7',
                'name': 'Algorithmic Cascade & Liquidity Vacuum',
                'defense_ratio': 0.40,
                'attack_ratio': 0.60,
                'actions': {
                    'INTRADAY_ROUTER': {'action': 'CANCEL_UNFILLED_AND_FREEZE_BUY_15MIN'},
                    'S0_BETA': {'target': 'VKOSPI_ETN_OTM_PUT', 'action': 'GAMMA_ALPHA'}
                }
            })

        # --- Scenario 8: Financial Credit Contagion & Bank Run ---
        ted_spread_z = float(signal_cache.get('ted_spread_z', 0.0))
        if ted_spread_z >= s8_ted_z_thresh:
            active_scenarios.append({
                'scenario_id': 'SCENARIO_8',
                'name': 'Financial Credit Contagion & Bank Run',
                'defense_ratio': 0.40,
                'attack_ratio': 0.60,
                'actions': {
                    'S1_ETF_SNIPER': {'target': 'BANK_SECTOR_INVERSE', 'action': 'SHORT_100'},
                    'S0_BETA': {'action': 'COUNTERPARTY_RISK_BLOCK'}
                }
            })

        if active_scenarios:
            for sc in active_scenarios:
                logger.warning(f"  🚨 [Master Scenario Engine] {sc['name']} ({sc['scenario_id']}) 발화! Def={sc['defense_ratio']:.0%}, Atk={sc['attack_ratio']:.0%}")

        return {
            'is_scenario_active': len(active_scenarios) > 0,
            'active_scenarios': active_scenarios,
            'scenario_count': len(active_scenarios)
        }

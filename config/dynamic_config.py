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

_DEFAULTS = None

def _get_defaults() -> Dict[str, Any]:
    global _DEFAULTS
    if _DEFAULTS is None:
        import os
        from pathlib import Path
        p = Path(__file__).resolve().parent / 'defaults.json'
        if p.exists():
            with open(p, 'r', encoding='utf-8') as f:
                _DEFAULTS = json.load(f)
        else:
            _DEFAULTS = {}
            
        # ★ Phase 80: V2 이원화 런칭 모드 설정 (CHAMELEON vs INSTITUTIONAL)
        if 'system.run_mode' not in _DEFAULTS:
            _DEFAULTS['system.run_mode'] = 'CHAMELEON'  # Default for < 20B KRW expansion phase
            
    return _DEFAULTS

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
        self._defaults = dict(_get_defaults())
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
        # [Red Team Fix] 런타임 하드코딩 완전 제거 (defaults.json 연동)
        fb_vix_crisis = self.get('fallback.vix_crisis', 35.0)
        fb_ois_default = self.get('fallback.ois_default', 20.0)
        fb_vix_base = self.get('fallback.vix_baseline', 30.0)
        fb_vix_edge = self.get('fallback.vix_edge_min', 10.0)

        vix = market_state.get('vix')
        vix_baseline = market_state.get('vix_20d_avg')
        intraday_vol = market_state.get('intraday_volatility')
        if intraday_vol is None:
            intraday_vol = vix if vix is not None else fb_vix_crisis 
            
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
        
        # [Red Team Point 2] Data Starvation Fallback: 데이터 누락 시 보수적으로 위기(Crisis) 상황 가정
        _ois = ois_score if ois_score is not None else fb_ois_default 
        _vix_base = vix_baseline if vix_baseline is not None and vix_baseline > 0 else fb_vix_base
        
        is_inverted = False
        if us10y is not None and us2y is not None:
            is_inverted = (us10y - us2y) < 0
            
        fx_stress = False
        if usdkrw is not None and usdkrw_baseline is not None and usdkrw_baseline > 0:
            fx_stress = usdkrw > usdkrw_baseline * 1.01

        # ---------------------------------------------------------
        # 1. Dual Kelly Model Parameters (Mathematical Purge)
        # ---------------------------------------------------------
        _vix_edge_threshold = max(fb_vix_edge, _vix_base * 0.85)
        _skew_momentum_weight = 0.03 + max(0.0, (50.0 - _ois) * 0.001)
        _track_b_max_cap = 0.50 if fx_stress else 0.35
        
        self.set('chameleon.dual_kelly.vix_edge_threshold', _vix_edge_threshold)
        self.set('chameleon.dual_kelly.skew_momentum_weight', _skew_momentum_weight)
        self.set('chameleon.dual_kelly.track_b_max_cap', _track_b_max_cap)
        self.set('chameleon.dual_kelly.base_edge_multiplier', 0.015) # Fixed minimal base scaling

        # ---------------------------------------------------------
        # 2. Medallion-style Risk Scaling & Bayesian Kelly
        # ---------------------------------------------------------
        vix_factor = 1.0
        if intraday_vol is not None and _vix_base > 0:
            vix_factor = max(0.5, min(2.5, intraday_vol / _vix_base))
        
        # SL/TP ATR Multiplier (Dynamic based on Regime Confidence & VIX factor)
        _sl_base = max(1.2, 2.5 - (1.0 - conf) * (1.5 if regime == 'bear' else 0.5))
        self.set('exit.sl_atr_multiplier', _sl_base * vix_factor)
        self.set('exit.tp_atr_multiplier', (_sl_base * 1.5) * vix_factor)
        
        # Bayesian Kelly Base (Dynamic based on OIS & Inversion)
        _kelly_base = 0.15 + (_ois / 100.0) * 0.15 - (0.05 if is_inverted else 0.0)
        kelly_scale = max(0.2, min(1.0, 1.0 - (vix_factor - 1.0) * 0.5))
        if regime in ['bear', 'crash']:
            kelly_scale *= 0.5
        self.set('sizer.kelly_fraction', _kelly_base * kelly_scale)
        
        # Killswitch MDD (Dynamic Trailing Stop)
        _kill_dd = max(-10.0, min(-3.0, -8.0 + (50.0 - _ois) * 0.05 + mdd * 0.2))
        self.set('killswitch.drawdown_liquidate_pct', _kill_dd)

        # ---------------------------------------------------------
        # 3. Bridgewater-style Macro Adjustments
        # ---------------------------------------------------------
        ois_penalty = max(0.0, (50.0 - _ois) * 0.001)
        spread_penalty = 10.0 if is_inverted else 0.0
        
        _qv_base = 20.0 + (_ois / 100.0) * 20.0  # Dynamic QV base from 20 to 40
        _up_prob_base = 0.55 + (_ois / 100.0) * 0.10 # Dynamic UP base from 0.55 to 0.65
        
        new_up_prob = _up_prob_base
        new_qv = _qv_base
        
        if regime == 'bull':
            new_qv = _qv_base - 10.0 + spread_penalty
            new_up_prob = _up_prob_base - 0.02
        elif regime == 'caution':
            new_qv = _qv_base + spread_penalty
            new_up_prob = _up_prob_base + ois_penalty * 0.5
        elif regime in ['bear', 'crash']:
            new_qv = _qv_base + 10.0 + spread_penalty
            new_up_prob = _up_prob_base + 0.05 + ois_penalty
            if fx_stress:
                self.set('fundamental.min_equity_ratio', 0.20)
                
        # Clamp up probability and QV score
        new_up_prob = max(0.50, min(0.85, new_up_prob))
        new_qv = max(10.0, min(60.0, new_qv))
        
        self.set('fundamental.min_qv_score', new_qv)
        self.set('a3.min_up_probability', new_up_prob)
        
        # Foreign Flow Scaling
        if foreign_flow is not None and foreign_baseline is not None:
            if foreign_flow > foreign_baseline + 2000:
                self.set('allocation.conf_sizing.high_mult', 1.8)
            elif foreign_flow < foreign_baseline - 2000:
                self.set('allocation.conf_sizing.high_mult', 1.2)
            
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
        logger.info(f"  [DynamicConfig] 🔄 Pure Math Market state applied (VIX Factor: {vix_factor:.2f}x, OIS: {ois_log}, Regime: {regime})")

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

"""
Morning Intelligence Fusion Engine
====================================
이브닝 학습 결과 + 야간 글로벌 데이터를 복합 분석하여
당일 트레이딩 신호의 정확도를 향상시킵니다.

핵심 로직:
  1. 이브닝 학습 결과 로드 (SelfLearning 조정값)
  2. 야간 글로벌 컨텍스트 로드 (OIS, US Regime, 선물, 뉴스)
  3. 상충 감지 & 조정:
     - SL이 특정 섹터 비중↓ but 야간 해당 섹터↑ → 조정 완화
     - OIS 극단적(>80 or <20) → regime confidence 강화
     - 글로벌 뉴스 센티먼트 ≠ US 가격 방향 → contrarian 시그널
  4. 최종 morning_fusion.json 저장 → premarket/morning에서 참조

Author: Project Meridian
Date: 2026-05-29
"""
import pandas as pd
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'

class MorningIntelligenceFusion:
    """이브닝 학습 결과 + 야간 글로벌 데이터 복합 분석."""

    def __init__(self):
        self._signal_cache = self._load_json('signal_cache.json')
        self._self_learning = self._load_json('self_learning_state.json')
        self._measurement = self._load_json('measurement_engine.json')
        self._overnight_macro = self._load_latest_overnight_macro()

    def fuse(self) -> Dict[str, Any]:
        """복합 분석 실행.

        Returns:
            {
                'regime_adjustment': float,      # -0.3 ~ +0.3 (confidence 보정)
                'sector_overrides': {},           # 섹터별 가중치 보정
                'risk_flags': [],                 # 위험 신호 목록
                'overnight_context': {},          # 야간 글로벌 요약
                'fusion_confidence': float,       # 복합 판단 신뢰도
                'conflicts': [],                  # 학습-글로벌 상충 목록
                'timestamp': str,
            }
        """
        result = {'regime_adjustment': 0.0, 'sector_overrides': {}, 'risk_flags': [], 'overnight_context': {}, 'fusion_confidence': 0.5, 'conflicts': [], 'timestamp': datetime.now().isoformat()}
        overnight = self._build_overnight_context()
        result['overnight_context'] = overnight
        conflicts = self._detect_conflicts(overnight)
        result['conflicts'] = conflicts
        regime_adj = self._compute_regime_adjustment(overnight)
        result['regime_adjustment'] = regime_adj
        sector_overrides = self._compute_sector_overrides(overnight, conflicts)
        result['sector_overrides'] = sector_overrides
        risk_flags = self._check_risk_flags(overnight)
        result['risk_flags'] = risk_flags
        n_flags = len(risk_flags)
        n_conflicts = len(conflicts)
        base_conf = 0.7
        result['fusion_confidence'] = max(0.2, base_conf - n_flags * 0.1 - n_conflicts * 0.05)
        self._save(result)
        logger.info(f'  ✅ MorningFusion: regime_adj={regime_adj:+.2f}, conflicts={n_conflicts}, risk_flags={n_flags}, conf={result['fusion_confidence']:.2f}')
        return result

    def _build_overnight_context(self) -> Dict:
        """signal_cache + overnight_macro에서 야간 글로벌 요약.

        수정 2026-05-29:
          - sp500_change_pct/nasdaq_change_pct 키 부재 시
            components.us_market.raw.changes에서 추출
          - signal_cache 1M 변동률에서 일 환산 fallback
        """
        sc = self._signal_cache
        oi = sc.get('overnight_intel', {})
        sp500_chg = oi.get('sp500_change_pct', 0)
        nasdaq_chg = oi.get('nasdaq_change_pct', 0)
        if sp500_chg == 0 or nasdaq_chg == 0:
            us_market = oi.get('components', {}).get('us_market', {})
            us_raw = us_market.get('raw', {})
            changes = us_raw.get('changes', [])
            if changes and isinstance(changes, list):
                if sp500_chg == 0 and len(changes) >= 1:
                    sp500_chg = changes[0]
                if nasdaq_chg == 0 and len(changes) >= 2:
                    nasdaq_chg = changes[1]
                elif nasdaq_chg == 0 and len(changes) >= 1:
                    nasdaq_chg = changes[0]
        if sp500_chg == 0:
            sp_1m = sc.get('sp500_change_1m', 0)
            if sp_1m:
                sp500_chg = float(sp_1m) / 21
        if nasdaq_chg == 0:
            nq_1m = sc.get('nasdaq_change_1m', 0)
            if nq_1m:
                nasdaq_chg = float(nq_1m) / 21
        return {'ois': sc.get('ois', 50), 'us_regime': sc.get('us_regime', 'neutral'), 'us_regime_confidence': sc.get('us_regime_confidence', 0), 'sp500_change': round(sp500_chg, 4) if sp500_chg else 0, 'nasdaq_change': round(nasdaq_chg, 4) if nasdaq_chg else 0, 'sox_change': oi.get('sox_change_pct', sc.get('sox_change', 0)), 'vix': sc.get('vix', 20), 'vkospi': sc.get('vkospi', 18), 'usdkrw': sc.get('usdkrw', 1350), 'ewy_change': sc.get('ewy_change_1d', sc.get('ewy_change_1m', 0) / 21 if sc.get('ewy_change_1m') else 0), 'dxy_change': sc.get('dxy_change_1d', 0), 'copper_change': sc.get('copper_change_1d', sc.get('copper_change_1m', 0) / 21 if sc.get('copper_change_1m') else 0), 'usdjpy_change': sc.get('usdjpy_change_1d', sc.get('usdjpy_change_1m', 0) / 21 if sc.get('usdjpy_change_1m') else 0), 'taiex_change': sc.get('taiex_change_1d', sc.get('taiex_change_1m', 0) / 21 if sc.get('taiex_change_1m') else 0), 'nikkei_change': sc.get('nikkei_change_1d', sc.get('nikkei_change_1m', 0) / 21 if sc.get('nikkei_change_1m') else 0), 'hangseng_change': sc.get('hangseng_change_1d', sc.get('hangseng_change_1m', 0) / 21 if sc.get('hangseng_change_1m') else 0)}

    def _detect_conflicts(self, overnight: Dict) -> list:
        """이브닝 학습 조정 vs 야간 글로벌 데이터 상충 감지."""
        conflicts = []
        sl_state = self._self_learning
        if not sl_state:
            return conflicts
        changes = sl_state.get('last_changes', [])
        for change in changes:
            param = change.get('param', '')
            direction = 'up' if change.get('delta', 0) > 0 else 'down'
            if 'semiconductor' in param.lower() or 'semi' in param.lower():
                sox = overnight.get('sox_change', 0)
                if direction == 'down' and sox > 1.5:
                    conflicts.append({'type': 'sector_reversal', 'detail': f'SelfLearning이 반도체 비중↓ but SOX {sox:+.1f}%', 'param': param, 'severity': 'medium', 'recommendation': 'ease_reduction'})
                elif direction == 'up' and sox < -1.5:
                    conflicts.append({'type': 'sector_reversal', 'detail': f'SelfLearning이 반도체 비중↑ but SOX {sox:+.1f}%', 'param': param, 'severity': 'medium', 'recommendation': 'ease_increase'})
            if 'exposure' in param.lower() or 'confidence' in param.lower():
                ois = overnight.get('ois', 50)
                if direction == 'down' and ois > 70:
                    conflicts.append({'type': 'regime_conflict', 'detail': f'SelfLearning이 노출↓ but OIS={ois:.0f} (강세)', 'param': param, 'severity': 'low', 'recommendation': 'maintain_moderate'})
        return conflicts

    def _compute_regime_adjustment(self, overnight: Dict) -> float:
        """야간 글로벌 데이터 기반 레짐 confidence 보정.

        Returns:
            -0.3 ~ +0.3 범위의 보정값.
            양수: 강세 쪽으로 보정, 음수: 약세 쪽으로 보정.
        """
        adj = 0.0
        ois = overnight.get('ois', 50)
        if ois > 75:
            adj += 0.15
        elif ois > 65:
            adj += 0.05
        elif ois < 25:
            adj -= 0.15
        elif ois < 35:
            adj -= 0.05
        sp_chg = overnight.get('sp500_change', 0)
        if abs(sp_chg) > 1.0:
            adj += 0.1 if sp_chg > 0 else -0.1
        vix = overnight.get('vix', 20)
        if vix > 30:
            adj -= 0.1
        elif vix < 15:
            adj += 0.05
        usdjpy = overnight.get('usdjpy_change', 0)
        if usdjpy < -1.0:
            adj -= 0.1
        copper = overnight.get('copper_change', 0)
        if copper > 2.0:
            adj += 0.05
        elif copper < -2.0:
            adj -= 0.05
        return max(-0.3, min(0.3, round(adj, 3)))

    def _compute_sector_overrides(self, overnight: Dict, conflicts: list) -> Dict:
        """야간 데이터 기반 섹터 가중치 보정."""
        overrides = {}
        sox = overnight.get('sox_change', 0)
        if abs(sox) > 1.0:
            overrides['semiconductor'] = round(sox * 0.02, 3)
        copper = overnight.get('copper_change', 0)
        if abs(copper) > 1.5:
            overrides['steel'] = round(copper * 0.015, 3)
            overrides['energy'] = round(copper * 0.01, 3)
        taiex = overnight.get('taiex_change', 0)
        if abs(taiex) > 1.0:
            if sox * taiex > 0:
                current = overrides.get('semiconductor', 0)
                overrides['semiconductor'] = round(current + taiex * 0.01, 3)
        for conflict in conflicts:
            if conflict.get('recommendation') == 'ease_reduction':
                sector = 'semiconductor'
                current = overrides.get(sector, 0)
                overrides[sector] = round(current + 0.02, 3)
        return overrides

    def _check_risk_flags(self, overnight: Dict) -> list:
        """리스크 경고 시그널 확인."""
        flags = []
        vix = overnight.get('vix', 20)
        if vix > 35:
            flags.append({'flag': 'VIX_SPIKE', 'detail': f'VIX={vix:.1f} (>35)', 'severity': 'high'})
        elif vix > 25:
            flags.append({'flag': 'VIX_ELEVATED', 'detail': f'VIX={vix:.1f} (>25)', 'severity': 'medium'})
        sp_chg = overnight.get('sp500_change', 0)
        if sp_chg < -2.0:
            flags.append({'flag': 'US_CRASH', 'detail': f'S&P500 {sp_chg:+.1f}%', 'severity': 'high'})
        usdjpy = overnight.get('usdjpy_change', 0)
        if usdjpy < -1.5:
            flags.append({'flag': 'YEN_CARRY_UNWIND', 'detail': f'USD/JPY {usdjpy:+.1f}%', 'severity': 'high'})
        dxy = overnight.get('dxy_change', 0)
        if dxy > 1.0:
            flags.append({'flag': 'DOLLAR_SURGE', 'detail': f'DXY {dxy:+.1f}%', 'severity': 'medium'})
        hsi = overnight.get('hangseng_change', 0)
        if hsi < -2.0:
            flags.append({'flag': 'CHINA_RISK', 'detail': f'HangSeng {hsi:+.1f}%', 'severity': 'medium'})
        macro = self._signal_cache.get('macro_features', {})
        if macro:
            if macro.get('credit_stress', 0):
                flags.append({'flag': 'CREDIT_STRESS', 'detail': f'HY Spread 확대 (fred_hy={macro.get('fred_hy_spread', '?')})', 'severity': 'high'})
            if macro.get('yield_curve_inverted', 0):
                flags.append({'flag': 'YIELD_CURVE_INVERTED', 'detail': f'10Y-2Y={macro.get('cross_yield_curve', '?')}%', 'severity': 'medium'})
            news = macro.get('news_llm_sentiment', macro.get('news_naver_sentiment', 0))
            if news < -0.6:
                flags.append({'flag': 'NEWS_VERY_NEGATIVE', 'detail': f'감성={news:.2f}', 'severity': 'medium'})
        return flags

    def _get_ewy_change(self) -> float:
        """EWY 전일 변동률 (SGX 프록시)."""
        macro = self._overnight_macro
        if macro:
            ewy = macro.get('ewy', {})
            return ewy.get('change_pct', 0)
        return 0.0

    def _get_signal_change(self, name: str) -> float:
        """signal parquet에서 최신 변동률."""
        try:
            import pandas as pd
            path = _PROJECT_ROOT / 'data' / 'historical_10y' / f'signal_{name.lower()}.parquet'
            if not path.exists():
                path = _PROJECT_ROOT / 'data' / 'feature_store' / f'signal_{name.lower()}.parquet'
            if path.exists():
                df = pd.read_parquet(path)
                if len(df) >= 2 and 'close' in df.columns:
                    return float((df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            logger.warning('[SILENT_BYPASS] Suppressed exception at morning_intelligence_fusion.py:389', exc_info=True)
        return 0.0

    def _load_json(self, filename: str) -> Dict:
        """results/ 디렉토리에서 JSON 로드."""
        path = _RESULTS / filename
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at morning_intelligence_fusion.py:401', exc_info=True)
        return {}

    def _load_latest_overnight_macro(self) -> Dict:
        """최신 overnight_macro JSON."""
        macro_dir = _PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
        if macro_dir.exists():
            try:
                files = sorted(macro_dir.glob('*.json'), reverse=True)
                if files:
                    return json.loads(files[0].read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                logger.warning('[SILENT_BYPASS] Suppressed exception at morning_intelligence_fusion.py:415', exc_info=True)
        return {}

    def _save(self, result: Dict):
        """morning_fusion.json 저장."""
        try:
            out = _RESULTS / 'morning_fusion.json'
            out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f'  morning_fusion 저장 실패: {e}', exc_info=True)

def run_morning_fusion() -> Dict:
    """모듈 레벨 실행 함수."""
    engine = MorningIntelligenceFusion()
    return engine.fuse()
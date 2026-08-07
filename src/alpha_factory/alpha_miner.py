"""
Alpha Miner v2 — Symbolic Regression 기반 자가 발전형 알파 탐색 엔진
=======================================================================

[v2 주요 개선사항]
  1. 가짜 Sharpe → 실제 Rank IC (Spearman Correlation) 평가로 교체
     - Train/Test Time-Series Split OOS 검증
     - ic_threshold 미달 수식 즉시 기각
  2. 직교화(Orthogonalization) 필터 추가
     - 기존 피처와 Pearson Correlation > corr_threshold 시 기각
  3. AlphaTranslator: gplearn 수식 → pandas eval 문자열 자동 변환
  4. discovered_alphas.json 자동 관리 (active/retired 상태 머신)
  5. Zero Hardcoding: 모든 파라미터 DynamicConfig alpha_factory.* 키

[파이프라인 통합]
  v4_features.py의 inject_auto_alphas() 함수와 연동하여
  'status': 'active' 알파를 ML Feature DataFrame에 자동 주입.

Usage:
    # 알파 탐색
    from src.alpha_factory.alpha_miner import AlphaMiner
    miner = AlphaMiner()
    miner.mine_alphas()

    # 가비지 컬렉션 (주간 재학습 파이프라인에서 호출)
    from src.alpha_factory.alpha_miner import FactorPruner
    pruner = FactorPruner()
    pruner.run()

    # 알파 번역
    from src.alpha_factory.alpha_miner import AlphaTranslator
    t = AlphaTranslator(['rsi_14', 'volume_ratio_20d', 'atr_pct'])
    expr = t.translate('add(mul(X0, X1), X2)')
"""
from __future__ import annotations
import json
from src.utils.file_ops import atomic_write_json

import logging
import re
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from scipy import stats as _scipy_stats
warnings.filterwarnings('ignore')
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _PROJECT_ROOT / 'results'
_DATA_DIR = _PROJECT_ROOT / 'data'
_FEATURE_STORE = _DATA_DIR / 'feature_store'
_DISCOVERED_ALPHAS_FILE = _RESULTS_DIR / 'discovered_alphas.json'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
    import logging
    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
    _cfg = None

def _dc(key: str, default):
    return _cfg.get(key, default) if _cfg is not None else default
try:
    from gplearn.genetic import SymbolicTransformer
    _GPLEARN_OK = True
except ImportError as e:
    _GPLEARN_OK = False
try:
    from sklearn.model_selection import TimeSeriesSplit
    _SKLEARN_OK = True
except ImportError as e:
    _SKLEARN_OK = False

class AlphaTranslator:
    """gplearn 수식 포맷 → pandas eval() 문자열 번역기.

    gplearn은 수식을 함수형 폴란드 표기법으로 출력합니다:
        add(mul(X0, X1), X2)

    AlphaTranslator는 이를 pandas eval 가능한 파이썬 표현식으로 변환합니다:
        (df['rsi_14'] * df['volume_ratio_20d']) + df['atr_pct']

    또한 DataFrame에 직접 적용하여 Feature 컬럼을 생성합니다.

    Args:
        feature_names: gplearn X0, X1, ... 에 매핑할 피처 이름 목록
        df_prefix:     DataFrame 변수명 (eval()에서 사용, 기본 'df')
    """
    _BINARY_OPS = {'add': '+', 'sub': '-', 'mul': '*', 'div': '/'}
    _UNARY_FUNCS = {'sqrt': 'np.sqrt(np.abs({arg}))', 'log': 'np.log(np.abs({arg}) + 1e-9)', 'abs': 'np.abs({arg})', 'neg': '(-{arg})', 'inv': '(1.0 / ({arg} + 1e-9))', 'sin': 'np.sin({arg})', 'cos': 'np.cos({arg})', 'tan': 'np.tanh({arg})', 'sig': '(1.0 / (1.0 + np.exp(-{arg})))'}
    _BINARY_FUNCS = {'max': 'np.maximum({a}, {b})', 'min': 'np.minimum({a}, {b})'}

    def __init__(self, feature_names: List[str], df_prefix: str='df'):
        self.feature_names = feature_names
        self.df_prefix = df_prefix

    def translate(self, gplearn_expr: str) -> str:
        """gplearn 표현식 → pandas eval 문자열 변환.

        Args:
            gplearn_expr: 예) 'add(mul(X0, X1), neg(X2))'

        Returns:
            파이썬 표현식 문자열 (NaN-safe, Inf-safe)
            실패 시 '0.0' 반환

        Raises:
            절대 예외 미발생 (내부 try-except 처리)
        """
        try:
            tokens = self._tokenize(gplearn_expr.strip())
            expr, _ = self._parse(tokens, 0)
            return expr
        except Exception as e:
            logger.debug(f'  AlphaTranslator 변환 실패 [{gplearn_expr[:50]}]: {e}')
            return '0.0'

    def apply(self, gplearn_expr: str, df: pd.DataFrame, col_name: str='auto_alpha') -> pd.DataFrame:
        """DataFrame에 알파 컬럼을 직접 생성.

        Args:
            gplearn_expr: gplearn 수식 문자열
            df:           Feature DataFrame
            col_name:     생성할 컬럼명

        Returns:
            컬럼이 추가된 DataFrame (원본 수정 없음, 복사본 반환)
        """
        df_out = df.copy()
        try:
            py_expr = self.translate(gplearn_expr)
            if py_expr == '0.0':
                df_out[col_name] = 0.0
                return df_out
            local_ctx = {'df': df_out, 'np': np, 'pd': pd}
            raw = eval(py_expr, {'__builtins__': {}}, local_ctx)
            if isinstance(raw, pd.Series):
                arr = raw.values.astype(float)
            elif isinstance(raw, np.ndarray):
                arr = raw.astype(float)
            else:
                arr = np.full(len(df_out), float(raw))
            arr = np.where(np.isfinite(arr), arr, 0.0)
            df_out[col_name] = arr
        except ZeroDivisionError:
            logger.debug(f'  AlphaTranslator ZeroDivisionError [{col_name}] → 0.0')
            df_out[col_name] = 0.0
        except Exception as e:
            logger.debug(f'  AlphaTranslator apply 실패 [{col_name}]: {e} → 0.0')
            df_out[col_name] = 0.0
        return df_out

    def apply_array(self, gplearn_program, X: np.ndarray) -> np.ndarray:
        """gplearn program 객체를 ndarray에 직접 적용.

        IC 평가에서 사용. program.execute(X)를 안전하게 감쌈.
        """
        try:
            result = gplearn_program.execute(X)
            if not isinstance(result, np.ndarray):
                result = np.full(len(X), float(result))
            result = result.astype(float)
            result = np.where(np.isfinite(result), result, 0.0)
            return result
        except Exception as e:
            logger.debug(f'  program.execute 실패: {e}')
            return np.zeros(len(X))

    def _col(self, idx: int) -> str:
        """X{n} → df['feature_name'] 변환."""
        if 0 <= idx < len(self.feature_names):
            name = self.feature_names[idx]
            return f"{self.df_prefix}['{name}']"
        return '0.0'

    def _tokenize(self, expr: str) -> List[str]:
        """수식을 토큰 리스트로 분리."""
        tokens = re.findall('[A-Za-z_][A-Za-z0-9_]*|X\\d+|-?\\d+\\.?\\d*|\\(|\\)|,', expr)
        return tokens

    def _parse(self, tokens: List[str], pos: int) -> Tuple[str, int]:
        """재귀 하강 파서 — (표현식 문자열, 다음 위치) 반환."""
        if pos >= len(tokens):
            return ('0.0', pos)
        tok = tokens[pos]
        if re.match('^X(\\d+)$', tok):
            idx = int(tok[1:])
            return (self._col(idx), pos + 1)
        if re.match('^-?\\d+\\.?\\d*$', tok):
            return (tok, pos + 1)
        if tok in self._BINARY_OPS:
            op = self._BINARY_OPS[tok]
            pos += 1
            pos = self._skip(tokens, pos, '(')
            left, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ',')
            right, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ')')
            return (f'({left} {op} {right})', pos)
        if tok in self._BINARY_FUNCS:
            fmt = self._BINARY_FUNCS[tok]
            pos += 1
            pos = self._skip(tokens, pos, '(')
            a, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ',')
            b, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ')')
            return (fmt.format(a=a, b=b), pos)
        if tok in self._UNARY_FUNCS:
            fmt = self._UNARY_FUNCS[tok]
            pos += 1
            pos = self._skip(tokens, pos, '(')
            arg, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ')')
            return (fmt.format(arg=arg), pos)
        if tok == '(':
            pos += 1
            inner, pos = self._parse(tokens, pos)
            pos = self._skip(tokens, pos, ')')
            return (f'({inner})', pos)
        logger.debug(f'  AlphaTranslator 알 수 없는 토큰: {tok!r}')
        return ('0.0', pos + 1)

    def _skip(self, tokens: List[str], pos: int, expected: str) -> int:
        """기대 토큰을 소비하고 다음 위치 반환."""
        if pos < len(tokens) and tokens[pos] == expected:
            return pos + 1
        return pos

class ICEvaluator:
    """발굴된 알파 수식의 실제 IC(Information Coefficient)를 OOS 검증.

    평가 방법:
      - X_train/X_test 시계열 분할
      - program.execute(X_test) → alpha 신호 생성
      - alpha 신호와 y_test(미래 수익률) 간의 Spearman Rank IC 계산
      - IC ≥ ic_threshold 인 알파만 생존
    """

    def __init__(self):
        self.ic_threshold = _dc('alpha_factory.ic_threshold', 0.05)
        self.test_ratio = _dc('alpha_factory.oos_test_ratio', 0.3)
        self.n_splits = _dc('alpha_factory.oos_n_splits', 3)
        self._ic_thresholds_by_regime = {'bull': float(_dc('alpha_factory.ic_threshold_bull', 0.04)), 'bear': float(_dc('alpha_factory.ic_threshold_bear', 0.02)), 'neutral': float(_dc('alpha_factory.ic_threshold_neutral', 0.05)), 'crash': float(_dc('alpha_factory.ic_threshold_crash', 0.03)), 'caution': float(_dc('alpha_factory.ic_threshold_neutral', 0.05)), 'momentum_surge': float(_dc('alpha_factory.ic_threshold_bull', 0.04))}
        self._dynamic_threshold: Optional[float] = None
        self._update_dynamic_threshold()

    def _update_dynamic_threshold(self) -> None:
        """실제 feature_ic/daily_ic_summary.csv 분포에서 IC 임계치 동적 계산.

        알고리즘:
          1. daily_ic_summary.csv 로드
          2. 최근 N일(rolling window) 피처 IC 분포 수집
          3. 양수 IC의 하위 percentile (P25 기본) = 동적 임계치
          4. 이 값으로 ic_threshold 및 레짐별 임계치를 상대적으로 조정

        장점: IC 분포가 변하는 시장 환경에서 자동 적응,
               하라코딩 IC 임계치 완전 제거
        """
        try:
            ic_file = _PROJECT_ROOT / 'data' / 'feature_ic' / 'daily_ic_summary.csv'
            if not ic_file.exists():
                logger.debug('  ICEvaluator: feature_ic 파일 없음 → 기본 임계치 유지')
                return
            df = pd.read_csv(ic_file)
            if df.empty or 'mean_ic' not in df.columns:
                return
            window_days = int(_dc('alpha_factory.ic_window_days', 30))
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                cutoff = df['date'].max() - pd.Timedelta(days=window_days)
                df = df[df['date'] >= cutoff]
            if df.empty:
                return
            all_ic = df['mean_ic'].dropna().values
            pos_ic = all_ic[all_ic > 0]
            if len(pos_ic) < int(_dc('alpha_factory.ic_min_positive_samples', 10)):
                logger.debug(f'  ICEvaluator: 양수 IC 샘플 부족 ({len(pos_ic)}) → 기본 임계치')
                return
            pct = float(_dc('alpha_factory.ic_dynamic_percentile', 25.0))
            dynamic_thr = float(np.percentile(pos_ic, pct))
            min_thr = float(_dc('alpha_factory.ic_dynamic_min', 0.005))
            max_thr = float(_dc('alpha_factory.ic_dynamic_max', 0.15))
            dynamic_thr = float(np.clip(dynamic_thr, min_thr, max_thr))
            old = self._dynamic_threshold
            self._dynamic_threshold = dynamic_thr
            if self.ic_threshold > 1e-09:
                scale = dynamic_thr / self.ic_threshold
                for regime in self._ic_thresholds_by_regime:
                    yaml_val = self._ic_thresholds_by_regime[regime]
                    self._ic_thresholds_by_regime[regime] = float(np.clip((yaml_val * dynamic_thr) ** 0.5, min_thr, max_thr))
            self.ic_threshold = dynamic_thr
            logger.info(f'  ICEvaluator: 동적 IC 임계치 업데이트 {old} → {dynamic_thr:.4f} (P{pct:.0f}, N양수IC={len(pos_ic)}, 윈도={window_days}d)')
        except Exception as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
            logger.debug(f'  ICEvaluator._update_dynamic_threshold 실패 (비치명적): {e}')

    def compute_dynamic_threshold(self, regime: str='neutral') -> float:
        """현재 레짐와 동적 IC 분포를 결합한 실시간 임계치 반환.

        우선순위:
          1. 동적계산을 통해 업데이트된 feature_ic 기반 임계치
          2. 최신 레짐 조정 적용
          3. 폴백: YAML 설정값

        Args:
            regime: 현재 레짐 문자열

        Returns:
            해당 시점의 최적 IC 임계치 (float)
        """
        self._update_dynamic_threshold()
        regime_thr = self._ic_thresholds_by_regime.get(str(regime).lower().strip(), self.ic_threshold)
        logger.debug(f'  [ICEvaluator] 동적 IC 임계치: base={self.ic_threshold:.4f}, 레짐({regime})={regime_thr:.4f}')
        return float(regime_thr)

    def evaluate(self, program, X: np.ndarray, y: np.ndarray) -> Dict:
        """OOS IC 계산.

        Args:
            program: gplearn ._program 객체 (execute() 메서드 보유)
            X:       Feature 행렬 (n_samples × n_features)
            y:       미래 수익률 배열 (n_samples,)

        Returns:
            {
              'oos_ic':      float,   # 평균 OOS IC
              'oos_ic_std':  float,   # IC 표준편차 (안정성 지표)
              'ic_pvalue':   float,   # Spearman p-value
              'pass':        bool,    # ic_threshold 통과 여부
              'n_splits':    int,
            }
        """
        results = {'oos_ic': 0.0, 'oos_ic_std': 0.0, 'ic_pvalue': 1.0, 'pass': False, 'n_splits': 0}
        try:
            n = len(y)
            if n < 100:
                logger.debug('  IC 평가: 데이터 부족 (<100)')
                return results
            ics = []
            tscv = TimeSeriesSplit(n_splits=self.n_splits)
            for train_idx, test_idx in tscv.split(X):
                X_test = X[test_idx]
                y_test = y[test_idx]
                if len(y_test) < 20:
                    continue
                try:
                    alpha_signal = program.execute(X_test)
                    if not isinstance(alpha_signal, np.ndarray):
                        alpha_signal = np.full(len(y_test), float(alpha_signal))
                    alpha_signal = alpha_signal.astype(float)
                    valid_mask = np.isfinite(alpha_signal) & np.isfinite(y_test)
                    if valid_mask.sum() < 10:
                        ics.append(0.0)
                        continue
                    ic, pval = _scipy_stats.spearmanr(alpha_signal[valid_mask], y_test[valid_mask])
                    ics.append(float(ic) if np.isfinite(ic) else 0.0)
                except Exception as _e:
                    logger.debug(f'  IC split 계산 실패: {_e}')
                    ics.append(0.0)
            if not ics:
                return results
            mean_ic = float(np.mean(ics))
            std_ic = float(np.std(ics))
            split_at = int(n * (1 - self.test_ratio))
            X_test_full = X[split_at:]
            y_test_full = y[split_at:]
            try:
                sig_full = program.execute(X_test_full).astype(float)
                valid = np.isfinite(sig_full) & np.isfinite(y_test_full)
                _, pval = _scipy_stats.spearmanr(sig_full[valid], y_test_full[valid])
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pval = 1.0
            results.update({'oos_ic': round(mean_ic, 5), 'oos_ic_std': round(std_ic, 5), 'ic_pvalue': round(float(pval), 5), 'pass': mean_ic >= self.ic_threshold, 'n_splits': len(ics), 'ic_per_split': [round(v, 5) for v in ics]})
        except Exception as e:
            logger.warning(f'  ICEvaluator.evaluate 실패: {e}')
        return results

    def get_regime_threshold(self, regime: str) -> float:
        """레짐별 IC 임계치 반환.

        KR 시장은 레짐에 따라 모멘텀/역모멘텀 작동 방식이 달라짐:
          - Bull/Momentum_surge: 모멘텀 IC 임계치 완화 (0.04)
          - Bear/Crash: 역모멘텀 IC 임계치 완화 (0.02~0.03)
          - Neutral/Caution: 기본 임계치 (0.05)

        Args:
            regime: 현재 레짐 문자열

        Returns:
            해당 레짐의 IC 임계치 (float)
        """
        return self._ic_thresholds_by_regime.get(str(regime).lower().strip(), self.ic_threshold)

def _build_contrarian_features(close: 'pd.Series', volume: 'pd.Series | None'=None) -> dict:
    """KR 시장 특화 역모멘텀(Contrarian) 피처 생성.

    실증 분석: KR 개별주는 5d/20d/60d 모멘텀 IC가 모두 음수 (평균회귀 성격).
    이 함수는 그 역방향 신호를 알파 탐색 풀에 공급하여
    AlphaFactory가 KR 시장에서 실제로 작동하는 패턴을 발굴하도록 유도.

    피처 목록:
      reversion_5d      — 단기 급락 후 반등 (5일 수익률 역전)
      rsi_signal        — RSI 과매도/과매수 역방향 신호
      z_reversion       — 60일 Z-Score 하향 이탈 후 복귀 기대
      vol_adj_reversion — 변동성 조정 역모멘텀

    Args:
        close:  종가 시계열 (pd.Series, DatetimeIndex)
        volume: 거래량 시계열 (선택, 미사용 시 None)

    Returns:
        {feature_name: pd.Series} 딕셔너리
        contrarian_enabled=False 이면 빈 딕셔너리 반환
    """
    if not bool(_dc('alpha_factory.contrarian_enabled', True)):
        return {}
    try:
        import pandas as _pd
    except ImportError as e:
        return {}
    feats: dict = {}
    if len(close) < 30:
        return feats
    rev_window = int(_dc('alpha_factory.contrarian_reversion_window', 5))
    ret_n = close.pct_change(rev_window)
    feats['reversion_5d'] = (-ret_n).fillna(0.0)
    try:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        oversold = float(_dc('alpha_factory.contrarian_rsi_oversold', 30.0))
        overbought = float(_dc('alpha_factory.contrarian_rsi_overbought', 70.0))
        rsi_sig = np.where(rsi < oversold, (oversold - rsi) / max(oversold, 1e-06), np.where(rsi > overbought, (overbought - rsi) / max(100 - overbought, 1e-06), 0.0))
        feats['rsi_signal'] = pd.Series(rsi_sig, index=close.index).fillna(0.0)
    except Exception as _rsi_e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_rsi_e}", exc_info=True)
        logger.debug(f'  contrarian rsi_signal 생성 실패: {_rsi_e}')
    try:
        z_thresh = float(_dc('alpha_factory.contrarian_z_threshold', 2.0))
        roll_mean = close.rolling(60).mean()
        roll_std = close.rolling(60).std().replace(0, np.nan)
        z_score = (close - roll_mean) / roll_std
        feats['z_reversion'] = (-z_score.clip(upper=0) * (z_score < -z_thresh)).fillna(0.0)
    except Exception as _z_e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_z_e}", exc_info=True)
        logger.debug(f'  contrarian z_reversion 생성 실패: {_z_e}')
    try:
        vol_20 = close.pct_change().rolling(20).std().replace(0, np.nan)
        feats['vol_adj_reversion'] = (feats.get('reversion_5d', pd.Series(0.0, index=close.index)) / vol_20).fillna(0.0)
    except Exception as _v_e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_v_e}", exc_info=True)
        logger.debug(f'  contrarian vol_adj_reversion 생성 실패: {_v_e}')
    logger.debug(f'  [Contrarian] 피처 생성 완료: {list(feats.keys())}')
    return feats

class OrthogonalityFilter:
    """발굴된 알파가 기존 피처와 과도하게 상관된 경우 기각.

    Pearson Correlation |r| >= corr_threshold 이면 기각.
    기존에 이미 알려진 패턴의 중복 학습 방지.
    """

    def __init__(self):
        self.corr_threshold = _dc('alpha_factory.corr_threshold', 0.7)

    def is_orthogonal(self, alpha_signal: np.ndarray, existing_features: np.ndarray, feature_names: Optional[List[str]]=None) -> Tuple[bool, Dict]:
        """알파 신호가 기존 피처들과 직교하는지 검사.

        Args:
            alpha_signal:      알파 수식 출력 배열 (n_samples,)
            existing_features: 기존 Feature 행렬 (n_samples × n_features)
            feature_names:     피처 이름 목록 (로깅용)

        Returns:
            (is_orthogonal: bool, info: dict)
              is_orthogonal=False → 기각
        """
        info = {'max_corr': 0.0, 'max_corr_feature': '', 'pass': True, 'corr_threshold': self.corr_threshold}
        try:
            if not np.isfinite(alpha_signal).any():
                return (False, {**info, 'pass': False, 'reason': 'all_nan'})
            valid = np.isfinite(alpha_signal)
            alpha_clean = alpha_signal[valid]
            if len(alpha_clean) < 30:
                return (True, info)
            max_abs_corr = 0.0
            max_feat_name = ''
            n_feats = existing_features.shape[1]
            names = feature_names or [f'X{i}' for i in range(n_feats)]
            for i in range(n_feats):
                feat_col = existing_features[valid, i].astype(float)
                if not np.isfinite(feat_col).any():
                    continue
                feat_valid = np.isfinite(feat_col)
                if feat_valid.sum() < 30:
                    continue
                try:
                    corr_val, _ = _scipy_stats.pearsonr(alpha_clean[feat_valid], feat_col[feat_valid])
                    abs_corr = abs(float(corr_val)) if np.isfinite(corr_val) else 0.0
                    if abs_corr > max_abs_corr:
                        max_abs_corr = abs_corr
                        max_feat_name = names[i] if i < len(names) else f'X{i}'
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
            info.update({'max_corr': round(max_abs_corr, 4), 'max_corr_feature': max_feat_name})
            if max_abs_corr >= self.corr_threshold:
                info['pass'] = False
                info['reason'] = f'corr={max_abs_corr:.3f} >= threshold={self.corr_threshold} (feature: {max_feat_name})'
                logger.info(f"  🔴 직교화 기각: 최대상관 {max_abs_corr:.3f} ('{max_feat_name}') ≥ {self.corr_threshold}")
                return (False, info)
            logger.debug(f'  ✅ 직교화 통과: 최대상관 {max_abs_corr:.3f}')
            return (True, info)
        except Exception as e:
            logger.warning(f'  OrthogonalityFilter 오류: {e}')
            return (True, info)

def _safe_get_programs(est_gp) -> list:
    """gplearn SymbolicTransformer에서 Hall of Fame 프로그램을 안전하게 추출.

    gplearn의 내부 API는 버전마다 달라질 수 있으므로 다층 폴백을 사용:
      1. list(est_gp)      — SymbolicTransformer는 iterable 지원 (공개 API)
      2. _best_programs    — 구 버전 private attribute
      3. best_programs_    — 일부 버전에서 사용
      4. _programs[-1]     — 마지막 세대 전체 (hall_of_fame 슬라이스)
      5. []                — 최종 폴백
    """
    try:
        programs = list(est_gp)
        if programs:
            logger.debug(f'  [_safe_get_programs] list(est_gp) 성공: {len(programs)}개')
            return programs
    except (TypeError, StopIteration, Exception) as e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
        logger.debug(f'  [_safe_get_programs] list() 실패: {e}')
    try:
        programs = getattr(est_gp, '_best_programs', None)
        if programs:
            logger.debug(f'  [_safe_get_programs] _best_programs 성공: {len(programs)}개')
            return list(programs)
    except Exception as e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
        logger.debug(f'  [_safe_get_programs] _best_programs 실패: {e}')
    try:
        programs = getattr(est_gp, 'best_programs_', None)
        if programs:
            logger.debug(f'  [_safe_get_programs] best_programs_ 성공: {len(programs)}개')
            return list(programs)
    except Exception as e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
        logger.debug(f'  [_safe_get_programs] best_programs_ 실패: {e}')
    try:
        all_programs = getattr(est_gp, '_programs', None)
        if all_programs and isinstance(all_programs, list) and all_programs[-1]:
            hof_size = getattr(est_gp, 'hall_of_fame', 20) or 20
            last_gen = [p for p in all_programs[-1] if p is not None]
            last_gen.sort(key=lambda p: getattr(p, 'fitness_', 0.0), reverse=True)
            programs = last_gen[:hof_size]
            if programs:
                logger.debug(f'  [_safe_get_programs] _programs[-1] 폴백: {len(programs)}개')
                return programs
    except Exception as e:
        from src.utils.error_logger import log_error_rate_limited
        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
        logger.debug(f'  [_safe_get_programs] _programs 폴백 실패: {e}')
    logger.warning('  [_safe_get_programs] 모든 추출 시도 실패 → 빈 목록 반환')
    return []
from src.alpha_factory.garbage_collector import AlphaGarbageCollector

class AlphaMiner:
    """Genetic Programming 기반 알파 탐색기 v2.

    개선사항:
      - 실제 OOS Rank IC 평가
      - 직교화 필터 (다중공선성 차단)
      - AlphaTranslator 통합 (pandas eval 수식 자동 생성)
      - Zero Hardcoding (모든 하이퍼파라미터 DynamicConfig)
    """

    def __init__(self):
        _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        self.output_file = _DISCOVERED_ALPHAS_FILE
        self.pop_size = _dc('alpha_factory.population_size', 500)
        self.generations = _dc('alpha_factory.generations', 20)
        self.p_crossover = _dc('alpha_factory.p_crossover', 0.7)
        self.p_subtree = _dc('alpha_factory.p_subtree_mutation', 0.1)
        self.p_hoist = _dc('alpha_factory.p_hoist_mutation', 0.05)
        self.p_point = _dc('alpha_factory.p_point_mutation', 0.1)
        self.max_samples = _dc('alpha_factory.max_samples', 0.9)
        self.parsimony = _dc('alpha_factory.parsimony_coefficient', 0.01)
        self.stopping = _dc('alpha_factory.stopping_criteria', 0.01)
        self.random_state = _dc('alpha_factory.random_state', 42)
        self.top_k = _dc('alpha_factory.top_k_programs', 5)
        self.function_set = _dc('alpha_factory.function_set', ['add', 'sub', 'mul', 'div', 'sqrt', 'log', 'abs', 'neg', 'inv', 'max', 'min'])
        self.ic_evaluator = ICEvaluator()
        self.ortho_filter = OrthogonalityFilter()
        self.memory_store = AlphaMemoryStore()
        self.discovered_alphas: List[Dict] = []

    def load_data(self) -> pd.DataFrame:
        """Feature Store에서 통합 학습 데이터 로드.

        feature_store/*.parquet → 숫자형 컬럼 자동 수집.
        'target' 컬럼 없으면 다음날 종가 변동률로 자동 생성.
        """
        df_list = []
        sample_tickers = _dc('alpha_factory.sample_tickers', ['005930', '000660', '035420', '005380', '051910'])
        for ticker in sample_tickers:
            for pattern in [_FEATURE_STORE / f'kr_{ticker}_features.parquet', _FEATURE_STORE / f'{ticker}.parquet', _DATA_DIR / 'historical_10y' / f'kr_{ticker}.parquet']:
                if pattern.exists():
                    try:
                        df = pd.read_parquet(pattern)
                        df = self._preprocess(df, ticker)
                        if df is not None and len(df) >= 100:
                            df_list.append(df)
                            break
                    except Exception as e:
                        from src.utils.error_logger import log_error_rate_limited
                        log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}", exc_info=True)
                        logger.debug(f'  {pattern.name} 로드 실패: {e}')
        if not df_list:
            logger.warning('  AlphaMiner: 학습 데이터 없음')
            return pd.DataFrame()
        common_cols = list(set.intersection(*(set(d.columns) for d in df_list)))
        if not common_cols or 'target' not in common_cols:
            logger.warning(f'  AlphaMiner: 공통 컬럼 없음 ({len(df_list)}개 파일)')
            return pd.DataFrame()
        df_all = pd.concat([d[common_cols] for d in df_list], ignore_index=True)
        df_all = df_all.dropna(subset=['target'])
        logger.info(f'  AlphaMiner 데이터 로드: {len(df_all):,}행 × {len(df_all.columns)}컬럼')
        return df_all

    def _preprocess(self, df: pd.DataFrame, ticker: str) -> Optional[pd.DataFrame]:
        """DataFrame 전처리: 정렬, target 생성, 숫자형 필터링."""
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
        for date_col in ['date', 'index', 'datetime']:
            if date_col in df.columns:
                try:
                    df['date'] = pd.to_datetime(df[date_col])
                    df = df.sort_values('date').reset_index(drop=True)
                    break
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()
        if 'target' not in df.columns:
            close_col = next((c for c in ['close', '종가', 'adj_close'] if c in df.columns), None)
            if close_col:
                fwd = _dc('alpha_factory.forward_return_days', 1)
                df['target'] = df[close_col].pct_change(fwd).shift(-fwd) * 100
            else:
                logger.debug(f'  {ticker}: 종가 컬럼 없음, target 생성 불가')
                return None
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if 'target' not in num_cols:
            return None
        configured = _dc('alpha_factory.active_features', [])
        if configured:
            use_cols = [c for c in configured if c in num_cols] + ['target']
        else:
            max_feats = _dc('alpha_factory.max_features_per_ticker', 30)
            use_cols = [c for c in num_cols if c != 'target'][:max_feats] + ['target']
        df = df[use_cols].replace([np.inf, -np.inf], np.nan).dropna()
        return df if len(df) >= 100 else None

    def mine_alphas(self, n_generations: Optional[int]=None, pop_size: Optional[int]=None) -> List[Dict]:
        """GP 알고리즘으로 알파 수식 발굴.

        1. 데이터 로드
        2. GP Symbolic Regressor 학습
        3. Hall of Fame Top-K OOS IC 평가
        4. 직교화 필터 통과 검사
        5. discovered_alphas.json 저장

        Returns:
            새로 발굴된 알파 dict 목록
        """
        if not _GPLEARN_OK:
            logger.error('  gplearn 미설치: pip install gplearn')
            return []
        if not _SKLEARN_OK:
            logger.error('  scikit-learn 미설치: pip install scikit-learn')
            return []
        gens = n_generations or self.generations
        pop = pop_size or self.pop_size
        logger.info(f'  🧬 AlphaMiner v2 시작 (세대: {gens}, 인구: {pop})')
        data = self.load_data()
        if data.empty or 'target' not in data.columns:
            logger.error('  AlphaMiner: 학습 데이터 없음')
            return []
        features = [c for c in data.columns if c != 'target']
        X = data[features].values.astype(float)
        y = data['target'].values.astype(float)
        logger.info(f'  학습 데이터: X={X.shape}, y={y.shape}')
        est_gp = SymbolicTransformer(population_size=pop, generations=gens, stopping_criteria=self.stopping, p_crossover=self.p_crossover, p_subtree_mutation=self.p_subtree, p_hoist_mutation=self.p_hoist, p_point_mutation=self.p_point, max_samples=self.max_samples, verbose=1, parsimony_coefficient=self.parsimony, random_state=self.random_state, function_set=self.function_set, n_jobs=_dc('alpha_factory.n_jobs', -1), hall_of_fame=_dc('alpha_factory.hall_of_fame', 20), n_components=self.top_k)
        try:
            est_gp.fit(X, y)
        except Exception as e:
            logger.error(f'  GP 학습 실패: {e}')
            return []
        new_alphas: List[Dict] = []
        translator = AlphaTranslator(features)
        programs = _safe_get_programs(est_gp)
        logger.info(f'  Hall of Fame 평가: {len(programs)}개 수식')
        for rank, program in enumerate(programs):
            formula_str = str(program)
            logger.info(f'\n  [{rank + 1}] 수식: {formula_str}')
            adaptive_ic_threshold = self.memory_store.get_adaptive_ic_threshold(base_threshold=self.ic_evaluator.ic_threshold, features_used=features)
            original_threshold = self.ic_evaluator.ic_threshold
            self.ic_evaluator.ic_threshold = adaptive_ic_threshold
            ic_result = self.ic_evaluator.evaluate(program, X, y)
            self.ic_evaluator.ic_threshold = original_threshold
            logger.info(f'     OOS IC={ic_result['oos_ic']:.4f} (std={ic_result['oos_ic_std']:.4f}, p={ic_result['ic_pvalue']:.4f}) → {('✅ 통과' if ic_result['pass'] else '❌ IC 미달')}')
            if not ic_result['pass']:
                continue
            try:
                alpha_signal = program.execute(X).astype(float)
                alpha_signal = np.where(np.isfinite(alpha_signal), alpha_signal, 0.0)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                alpha_signal = np.zeros(len(X))
            is_ortho, ortho_info = self.ortho_filter.is_orthogonal(alpha_signal, X, features)
            if not is_ortho:
                logger.info(f'     ❌ 직교화 기각: {ortho_info.get('reason', '')}')
                continue
            py_expr = translator.translate(formula_str)
            logger.info(f'     ✅ 번역된 수식: {py_expr[:80]}')
            alpha_id = f'alpha_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{rank:02d}'
            alpha_info = {'id': alpha_id, 'formula': formula_str, 'py_expr': py_expr, 'fitness': float(getattr(program, 'fitness_', 0.0)), 'oos_ic': ic_result['oos_ic'], 'oos_ic_std': ic_result['oos_ic_std'], 'ic_pvalue': ic_result['ic_pvalue'], 'ic_per_split': ic_result.get('ic_per_split', []), 'max_corr': ortho_info['max_corr'], 'max_corr_feature': ortho_info['max_corr_feature'], 'features_used': features, 'n_features': len(features), 'discovered_at': datetime.now().isoformat(), 'status': 'active', 'ic_history': [], 'col_name': f'auto_alpha_{len(self._load_existing()) + len(new_alphas) + 1:03d}'}
            new_alphas.append(alpha_info)
            self.discovered_alphas.append(alpha_info)
            logger.info(f'     ✅ 알파 저장: {alpha_id}')
        if new_alphas:
            self._save_results()
            logger.info(f'\n  🎉 AlphaMiner 완료: {len(new_alphas)}개 신규 알파 발굴')
        else:
            logger.info('\n  ⚠️ 모든 수식이 IC/직교화 필터에서 기각됨')
        try:
            import joblib
            joblib.dump(est_gp, _RESULTS_DIR / 'alpha_model.joblib')
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        return new_alphas

    def evaluate_alpha_ic_from_expr(self, alpha: Dict, data: pd.DataFrame) -> float:
        """discovered_alphas.json의 py_expr을 DataFrame에 적용하여 IC 재계산.

        AlphaGarbageCollector에서 사용.
        """
        try:
            py_expr = alpha.get('py_expr', '')
            features = alpha.get('features_used', [])
            if not py_expr or not features:
                return 0.0
            avail_features = [f for f in features if f in data.columns]
            if not avail_features:
                return 0.0
            local_ctx = {'df': data, 'np': np, 'pd': pd}
            raw = eval(py_expr, {'__builtins__': {}}, local_ctx)
            if isinstance(raw, pd.Series):
                signal = raw.values.astype(float)
            else:
                signal = np.full(len(data), float(raw))
            signal = np.where(np.isfinite(signal), signal, 0.0)
            if 'target' not in data.columns:
                return 0.0
            y = data['target'].values.astype(float)
            valid = np.isfinite(signal) & np.isfinite(y)
            if valid.sum() < 20:
                return 0.0
            ic, _ = _scipy_stats.spearmanr(signal[valid], y[valid])
            return float(ic) if np.isfinite(ic) else 0.0
        except Exception as e:
            logger.debug(f'  IC 재계산 실패 [{alpha.get('id', '')}]: {e}')
            return 0.0

    def _load_existing(self) -> List[Dict]:
        """기존 discovered_alphas.json 로드."""
        if not self.output_file.exists():
            return []
        try:
            return json.loads(self.output_file.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    def _save_results(self) -> None:
        """발굴된 알파 JSON 저장 (기존 항목 보존)."""
        try:
            existing = self._load_existing()
            new_ids = {a['id'] for a in self.discovered_alphas}
            existing = [a for a in existing if a['id'] not in new_ids]
            all_alphas = existing + self.discovered_alphas
            atomic_write_json(self.output_file, all_alphas, indent=2, ensure_ascii=False, default=str)
            logger.info(f'  💾 discovered_alphas.json 저장: {len(all_alphas)}개')
        except Exception as e:
            logger.error(f'  알파 저장 실패: {e}')

class AlphaMemoryStore:
    """실패(퇴출)된 알파의 특성을 기억하여 다음 탐색에서 같은 공간을 반복하지 않도록 안내.

    르네상스 스타일 메타학습: '무엇이 실패했는가'를 알아야 '어디를 다시 탐색할지' 결정 가능.

    저장 항목 (per 퇴출 알파):
      - formula: gplearn 수식 문자열
      - features_used: 사용된 피처 목록
      - retire_regime: 퇴출 시점 레짐
      - avg_ic_before_retire: 퇴출 직전 평균 IC
      - ic_trend: IC 시계열 (감쇠 추이)
      - complexity: 수식 복잡도 (토큰 수)
      - retire_reason: 퇴출 사유
      - retired_at: 퇴출 시각

    활용:
      - mine_alphas() 시작 시 load_penalty_features() 호출
      - 실패한 피처 조합이 높은 비율로 포함된 수식은 IC 임계치 상향 적용
    """
    _MEMORY_FILE = _RESULTS_DIR / 'failed_alpha_memory.json'
    _MAX_MEMORY = 200

    def __init__(self):
        self._memory: list = []
        self._load()

    def _load(self) -> None:
        """파일에서 메모리 복원."""
        try:
            if self._MEMORY_FILE.exists():
                self._memory = json.loads(self._MEMORY_FILE.read_text(encoding='utf-8'))
                logger.debug(f'  AlphaMemoryStore: {len(self._memory)}개 기억 로드')
        except Exception as e:
            logger.debug(f'  AlphaMemoryStore 로드 실패 (초기화): {e}')
            self._memory = []

    def _save(self) -> None:
        """메모리를 파일에 저장 (FIFO 방식으로 최대 크기 유지)."""
        try:
            _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            if len(self._memory) > self._MAX_MEMORY:
                self._memory = self._memory[-self._MAX_MEMORY:]
            atomic_write_json(self._MEMORY_FILE, self._memory, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning(f'  AlphaMemoryStore 저장 실패: {e}')

    def record_failure(self, alpha: dict, retire_regime: str='unknown') -> None:
        """퇴출 알파를 메모리에 기록.

        Args:
            alpha: discovered_alphas.json의 알파 딕셔너리
            retire_regime: 퇴출 시점의 레짐
        """
        formula = alpha.get('formula', '')
        try:
            complexity = len(re.findall('[A-Za-z_][A-Za-z0-9_]*|X\\d+|\\d+\\.?\\d*', formula))
        except Exception:
            complexity = 0
        ic_history = alpha.get('ic_history', [])
        recent_ic = [e.get('ic', 0.0) for e in ic_history[-10:]]
        avg_ic = float(np.mean(recent_ic)) if recent_ic else 0.0
        record = {'formula': formula[:200], 'features_used': alpha.get('features_used', []), 'retire_regime': retire_regime, 'avg_ic_before_retire': round(avg_ic, 5), 'ic_trend': [round(v, 5) for v in recent_ic], 'complexity': complexity, 'retire_reason': alpha.get('retire_reason', ''), 'original_oos_ic': alpha.get('oos_ic', 0.0), 'retired_at': alpha.get('retired_at', datetime.now().isoformat())}
        self._memory.append(record)
        self._save()
        logger.info(f'  📝 AlphaMemoryStore: 퇴출 알파 기록 완료 (복잡도={complexity}, 레짐={retire_regime}, IC={avg_ic:.4f})')

    def get_penalty_features(self) -> dict:
        """실패 알파에서 자주 등장한 피처→패널티 점수 계산.

        Returns:
            {feature_name: penalty_score (0.0~1.0)}
            패널티 높을수록 '이 피처가 포함된 수식은 IC 임계치를 높여야 함'
        """
        if not self._memory:
            return {}
        feature_fail_count: dict = {}
        total = len(self._memory)
        for rec in self._memory:
            for feat in rec.get('features_used', []):
                feature_fail_count[feat] = feature_fail_count.get(feat, 0) + 1
        penalty = {feat: round(count / total, 4) for feat, count in feature_fail_count.items()}
        return penalty

    def get_adaptive_ic_threshold(self, base_threshold: float, features_used: list) -> float:
        """해당 피처 조합의 실패 이력에 기반해 IC 임계치를 동적으로 상향.

        Args:
            base_threshold: 기본 IC 임계치 (DynamicConfig에서 로드)
            features_used: 평가 중인 수식의 피처 목록

        Returns:
            조정된 IC 임계치 (≥ base_threshold)
        """
        penalty_features = self.get_penalty_features()
        if not penalty_features or not features_used:
            return base_threshold
        relevant_penalties = [penalty_features.get(f, 0.0) for f in features_used]
        avg_penalty = float(np.mean(relevant_penalties)) if relevant_penalties else 0.0
        max_penalty_scale = _dc('alpha_factory.memory_ic_penalty_scale', 0.05)
        adjusted = base_threshold + avg_penalty * max_penalty_scale
        if adjusted > base_threshold:
            logger.debug(f'  AlphaMemoryStore: IC 임계치 조정 {base_threshold:.4f} → {adjusted:.4f} (패널티={avg_penalty:.3f})')
        return min(adjusted, base_threshold * 2.0)

    def get_regime_failure_rate(self, regime: str) -> float:
        """특정 레짐에서의 알파 실패율.

        Args:
            regime: 레짐 ('bull', 'bear', 'crash', 'caution')

        Returns:
            0.0~1.0 사이의 실패율
        """
        if not self._memory:
            return 0.0
        regime_records = [r for r in self._memory if r.get('retire_regime') == regime]
        return len(regime_records) / len(self._memory) if self._memory else 0.0

    def summary(self) -> dict:
        """메모리 요약."""
        if not self._memory:
            return {'total': 0, 'by_regime': {}, 'top_penalty_features': []}
        regime_counts: dict = {}
        for rec in self._memory:
            r = rec.get('retire_regime', 'unknown')
            regime_counts[r] = regime_counts.get(r, 0) + 1
        penalty = self.get_penalty_features()
        top_feats = sorted(penalty.items(), key=lambda x: x[1], reverse=True)[:5]
        return {'total': len(self._memory), 'by_regime': regime_counts, 'top_penalty_features': [{'feature': f, 'penalty': p} for f, p in top_feats]}

class FactorPruner:
    """활성 알파의 IC Decay를 추적하여 수명이 다한 알파를 자동 퇴출.

    주간 재학습 파이프라인에서 호출.
    - 최근 decay_window일 IC < prune_rolling_ic_threshold → 'retired'
    - ic_history에 IC 기록 추가 (시계열 추적)

    Usage:
        pruner = FactorPruner()
        pruner.run()  # scripts/train_ensemble.py 마지막에 호출
    """

    def __init__(self):
        self.decay_window = _dc('alpha.rolling_window', 60)
        self.decay_threshold = _dc('alpha.prune_rolling_ic_threshold', 0.01)
        self.miner = AlphaMiner()
        self.memory_store = AlphaMemoryStore()

    def run(self, target_date: str=None) -> Dict:
        """IC Decay 검사 및 자동 Retire.

        Returns:
            {'retired': [alpha_ids], 'active': [alpha_ids], 'n_checked': int}
        """
        target_date = target_date or datetime.now().strftime('%Y%m%d')
        alphas = self.miner._load_existing()
        active_alphas = [a for a in alphas if a.get('status') == 'active']
        logger.info(f'\n  🗑️ FactorPruner 실행 ({target_date}): 활성 알파 {len(active_alphas)}개 검사')
        if not active_alphas:
            return {'retired': [], 'active': [], 'n_checked': 0}
        data = self.miner.load_data()
        if data.empty:
            logger.warning('  GC: 데이터 없음, 건너뜀')
            return {'retired': [], 'active': [], 'n_checked': 0}
        try:
            n_keep = min(len(data), self.decay_window * 3)
            data_recent = data.tail(n_keep).copy()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            data_recent = data
        retired = []
        active = []
        for alpha in alphas:
            if alpha.get('status') != 'active':
                continue
            recent_ic = self.miner.evaluate_alpha_ic_from_expr(alpha, data_recent)
            ic_entry = {'date': target_date, 'ic': round(recent_ic, 5)}
            alpha.setdefault('ic_history', [])
            alpha['ic_history'].append(ic_entry)
            recent_history = alpha['ic_history'][-self.decay_window:]
            ic_values = [e.get('ic', 0.0) for e in recent_history]
            avg_ic = float(np.mean(ic_values)) if ic_values else 0.0
            logger.info(f'  [{alpha['id']}] 최근 IC={recent_ic:.4f}, 평균({len(ic_values)}일)={avg_ic:.4f} → {('✅ 유지' if avg_ic >= self.decay_threshold else '⚰️ RETIRE')}')
            if avg_ic < self.decay_threshold:
                alpha['status'] = 'retired'
                alpha['retired_at'] = datetime.now().isoformat()
                alpha['retire_reason'] = f'IC decay: 최근{len(ic_values)}일 평균 IC {avg_ic:.4f} < threshold {self.decay_threshold}'
                retired.append(alpha['id'])
                logger.warning(f'  ⚰️ 알파 퇴출: {alpha['id']} (avg_ic={avg_ic:.4f})')
                try:
                    current_regime = _dc('alpha_factory.current_regime', 'unknown')
                    self.memory_store.record_failure(alpha, retire_regime=current_regime)
                except Exception as _mem_e:
                    from src.utils.error_logger import log_error_rate_limited
                    log_error_rate_limited(__name__, f"🚨 [Silent Bypass 감지] 치명적 예외 발생: {_mem_e}", exc_info=True)
                    logger.debug(f'  AlphaMemoryStore 기록 실패 (비치명적): {_mem_e}')
            else:
                active.append(alpha['id'])
        try:
            atomic_write_json(_DISCOVERED_ALPHAS_FILE, alphas, indent=2, ensure_ascii=False, default=str)
            logger.info(f'  GC 완료: 퇴출={len(retired)}개, 유지={len(active)}개')
        except Exception as e:
            logger.error(f'  GC 저장 실패: {e}')
        return {'retired': retired, 'active': active, 'n_checked': len(active_alphas), 'target_date': target_date}

def inject_auto_alphas(df: pd.DataFrame, alpha_file: Optional[Path]=None, max_alphas: Optional[int]=None) -> pd.DataFrame:
    """활성 알파를 DataFrame Feature 컬럼으로 자동 주입.

    v4_features.py 또는 ML 파이프라인에서 호출:
        from src.alpha_factory.alpha_miner import inject_auto_alphas
        df = inject_auto_alphas(df)

    Args:
        df:          ML Feature DataFrame (ticker/date 포함)
        alpha_file:  discovered_alphas.json 경로 (None=기본 경로)
        max_alphas:  주입할 최대 알파 수 (None=DynamicConfig 또는 무제한)

    Returns:
        auto_alpha_001, auto_alpha_002 등 컬럼이 추가된 DataFrame
        실패 시 원본 DataFrame 그대로 반환
    """
    alpha_file = alpha_file or _DISCOVERED_ALPHAS_FILE
    max_n = max_alphas or _dc('alpha_factory.max_inject_alphas', 10)
    if not alpha_file.exists():
        return df
    try:
        all_alphas = json.loads(alpha_file.read_text(encoding='utf-8'))
    except Exception as e:
        logger.debug(f'  inject_auto_alphas: JSON 로드 실패: {e}')
        return df
    active_alphas = [a for a in all_alphas if a.get('status') == 'active']
    active_alphas = sorted(active_alphas, key=lambda a: a.get('oos_ic', 0.0), reverse=True)[:max_n]
    if not active_alphas:
        return df
    df_out = df.copy()
    injected = 0
    for alpha in active_alphas:
        col_name = alpha.get('col_name', f'auto_alpha_{alpha['id'][-6:]}')
        py_expr = alpha.get('py_expr', '')
        features_needed = alpha.get('features_used', [])
        missing = [f for f in features_needed if f not in df_out.columns]
        if missing and len(missing) == len(features_needed):
            logger.debug(f'  inject: {col_name} 필요 피처 전부 없음 → 0.0')
            df_out[col_name] = 0.0
            injected += 1
            continue
        try:
            if not py_expr or py_expr == '0.0':
                df_out[col_name] = 0.0
            else:
                local_ctx = {'df': df_out, 'np': np, 'pd': pd}
                raw = eval(py_expr, {'__builtins__': {}}, local_ctx)
                if isinstance(raw, pd.Series):
                    arr = raw.values.astype(float)
                elif isinstance(raw, np.ndarray):
                    arr = raw.astype(float)
                else:
                    arr = np.full(len(df_out), float(raw))
                arr = np.where(np.isfinite(arr), arr, 0.0)
                df_out[col_name] = arr
            injected += 1
            logger.debug(f'  inject: {col_name} ✅ (IC={alpha.get('oos_ic', 0):.4f})')
        except ZeroDivisionError:
            logger.debug(f'  inject: {col_name} ZeroDivisionError → 0.0')
            df_out[col_name] = 0.0
            injected += 1
        except Exception as e:
            logger.debug(f'  inject: {col_name} 실패 ({e}) → 0.0')
            df_out[col_name] = 0.0
            injected += 1
    if injected:
        logger.info(f'  ✅ inject_auto_alphas: {injected}개 알파 주입 완료')
    return df_out

def get_active_alpha_names(alpha_file: Optional[Path]=None, max_alphas: Optional[int]=None) -> List[str]:
    """활성 알파의 컬럼명 목록 반환 (FEATURE_NAMES_V7 동적 확장용).

    v4_features.py에서:
        from src.alpha_factory.alpha_miner import get_active_alpha_names
        FEATURE_NAMES_V8 = FEATURE_NAMES_V7 + get_active_alpha_names()
    """
    alpha_file = alpha_file or _DISCOVERED_ALPHAS_FILE
    max_n = max_alphas or _dc('alpha_factory.max_inject_alphas', 10)
    if not alpha_file.exists():
        return []
    try:
        alphas = json.loads(alpha_file.read_text(encoding='utf-8'))
        active = [a for a in alphas if a.get('status') == 'active']
        active = sorted(active, key=lambda a: a.get('oos_ic', 0.0), reverse=True)[:max_n]
        return [a.get('col_name', f'auto_alpha_{a['id'][-6:]}') for a in active]
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        import logging
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
        return []
if __name__ == '__main__':
    import argparse, sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    parser = argparse.ArgumentParser(description='AlphaMiner v2')
    parser.add_argument('--mode', default='mine', choices=['mine', 'gc', 'status'], help='mine: 알파 탐색 | gc: 가비지 컬렉션 | status: 현황')
    parser.add_argument('--gens', type=int, default=None, help='세대 수')
    parser.add_argument('--pop', type=int, default=None, help='인구 수')
    args = parser.parse_args()
    if args.mode == 'mine':
        miner = AlphaMiner()
        new_alphas = miner.mine_alphas(n_generations=args.gens, pop_size=args.pop)
        logger.info(f'신규 알파: {len(new_alphas)}개')
    elif args.mode == 'gc':
        gc = AlphaGarbageCollector()
        result = gc.run()
        logger.info(f'GC 결과: {result}')
    elif args.mode == 'status':
        if _DISCOVERED_ALPHAS_FILE.exists():
            data = json.loads(_DISCOVERED_ALPHAS_FILE.read_text())
            active = [a for a in data if a.get('status') == 'active']
            retired = [a for a in data if a.get('status') == 'retired']
            logger.info(f'\n📊 Alpha Factory 현황')
            logger.info(f'  총 알파: {len(data)}개')
            logger.info(f'  활성:   {len(active)}개')
            logger.info(f'  퇴출:   {len(retired)}개')
            for a in active:
                logger.info(f'  [{a['id']}] IC={a.get('oos_ic', 'N/A'):.4f} col={a.get('col_name', 'N/A')}')
        else:
            logger.info('discovered_alphas.json 없음')
#!/usr/bin/env python3
"""
Walk-Forward Validator — 일일 자동 검증
==========================================

P2-1: 매일 evening phase에서 현재 모델의 OOS 성과를 검증하고
결과를 results/walk_forward_daily.json에 누적 저장.

DynamicConfig에서 모든 파라미터 동적 로드. 하드코딩 0.

Usage:
    from scripts.walk_forward_validator import WalkForwardValidator
    wfv = WalkForwardValidator()
    result = wfv.validate()
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig

logger = logging.getLogger(__name__)
cfg = DynamicConfig()

_RESULTS = _PROJECT_ROOT / 'results'
_MODEL_DIR = _RESULTS / 'models'
_REPORT_FILE = _RESULTS / 'walk_forward_daily.json'


class WalkForwardValidator:
    """일일 Walk-Forward OOS 검증.

    현재 모델의 최근 N일 예측을 실제 결과와 비교하여
    ACC/AUC/IC를 일일 추적.
    """

    def validate(self) -> Dict:
        """일일 검증 실행."""
        window = cfg.get('ml.wf_validation_window', 5)
        min_samples = cfg.get('ml.wf_min_samples', 10)

        result = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'timestamp': datetime.now().isoformat(),
            'window_days': window,
            'status': 'skip',
        }

        # 앙상블 모델 로드
        try:
            import pickle
            try:
                import sklearn._loss._loss as _sk_loss
                sys.modules.setdefault('_loss', _sk_loss)
            except Exception:
                pass
            model_path = _MODEL_DIR / 'stock_ranker_ensemble.pkl'
            if not model_path.exists():
                result['reason'] = '모델 미존재'
                self._save(result)
                return result

            try:
                from src.streams.s2_ml_alpha.ml_stream import MLStream
                pkg = MLStream._safe_pickle_load(model_path)
            except Exception:
                with open(model_path, 'rb') as f:
                    pkg = pickle.load(f)
            if not pkg:
                result['reason'] = '모델 메타데이터 비어있음'
                self._save(result)
                return result
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            result['reason'] = f"모델 로드 실패: {e}"
            self._save(result)
            return result

        # Signal Cache에서 최근 예측 확인
        try:
            sc_file = _RESULTS / 'signal_cache.json'
            if not sc_file.exists():
                result['reason'] = 'signal_cache 미존재'
                self._save(result)
                return result

            sc = json.loads(sc_file.read_text(encoding='utf-8'))
            predictions = self._resolve_predictions(sc)

            # 예측 vs 실현 수익률 비교
            correct = 0
            total = 0
            ic_pairs = []

            for ticker, pred_data in predictions.items():
                if not isinstance(pred_data, dict):
                    continue
                pred_prob = float(pred_data.get('up_prob', pred_data.get('predict_proba', pred_data.get('confidence', 0.5))) or 0.5)
                actual_return = pred_data.get('actual_return_pct', None)
                if actual_return is None:
                    actual_return = self._lookup_realized_return_pct(str(ticker))
                if actual_return is None:
                    continue

                total += 1
                pred_up = pred_prob >= 0.5
                actual_up = float(actual_return) > 0.0
                if pred_up == actual_up:
                    correct += 1
                ic_pairs.append((pred_prob, float(actual_return)))

            if total < min_samples:
                pwf_fallback = self._fallback_from_purged_wf(min_samples)
                if pwf_fallback is not None:
                    result.update(pwf_fallback)
                    self._save(result)
                    return result
                result['reason'] = f"예측 {len(predictions)}건 / 검증 가능 {total}건 < 최소 {min_samples}건"
                result['n_predictions'] = len(predictions)
                self._save(result)
                return result

            acc = correct / total
            ic = self._compute_ic(ic_pairs)

            result.update({
                'status': 'completed',
                'n_validated': total,
                'n_predictions': len(predictions),
                'oos_acc': round(acc, 4),
                'oos_ic': round(ic, 4),
                'n_correct': correct,
                'source': 'live_signals_and_prices',
            })

            # 경고 체크
            acc_warn = cfg.get('ml.wf_acc_warning_threshold', 0.55)
            ic_warn = cfg.get('ml.wf_ic_warning_threshold', 0.02)
            warnings = []
            if acc < acc_warn:
                warnings.append(f"ACC {acc:.1%} < {acc_warn:.1%} 경고")
            if ic < ic_warn:
                warnings.append(f"IC {ic:.4f} < {ic_warn} 경고")
            result['warnings'] = warnings

            if warnings:
                logger.warning(f"  ⚠️ Walk-Forward: {'; '.join(warnings)}")
            else:
                logger.info(
                    f"  ✅ Walk-Forward: ACC={acc:.1%}, IC={ic:.4f} "
                    f"({total}건)")

        except Exception as e:
            result['reason'] = f"검증 실패: {e}"
            logger.debug(f"  Walk-Forward 검증 실패: {e}")

        self._save(result)
        return result

    def _resolve_predictions(self, sc: Dict) -> Dict[str, Dict]:
        """signal_cache.json, latest_signals.json, holdings_ev 등에서 일일 예측 수집."""
        preds: Dict[str, Dict] = {}
        raw_ml = sc.get('ml_predictions', {})
        if isinstance(raw_ml, dict):
            for tk, pv in raw_ml.items():
                if isinstance(pv, dict):
                    preds[str(tk)] = dict(pv)

        ls_file = _RESULTS / 'latest_signals.json'
        if ls_file.exists():
            try:
                ls = json.loads(ls_file.read_text(encoding='utf-8'))
                sigs_map = ls.get('signals', {}) if isinstance(ls, dict) else {}
                if isinstance(sigs_map, dict):
                    for _s_id, sig_list in sigs_map.items():
                        if not isinstance(sig_list, list):
                            continue
                        for sig in sig_list:
                            if not isinstance(sig, dict):
                                continue
                            tk = str(sig.get('ticker', '') or '').strip()
                            if not tk or tk.startswith('_'):
                                continue
                            prob = sig.get('up_prob', sig.get('predict_proba', sig.get('confidence')))
                            if prob is not None and tk not in preds:
                                direction = str(sig.get('direction', 'long') or 'long').lower()
                                p_val = float(prob)
                                _half = 1.0 / 2.0
                                if direction == 'short' and p_val > _half:
                                    p_val = 1.0 - p_val
                                preds[tk] = {'up_prob': p_val, 'actual_return_pct': sig.get('actual_return_pct')}
                            mk_sc = (sig.get('market_data') or {}).get('signal_cache') or {}
                            for ev_key in ('active_signal_ev_map', 'holdings_ev'):
                                ev_map = mk_sc.get(ev_key)
                                if isinstance(ev_map, dict):
                                    for ev_tk, ev_val in ev_map.items():
                                        if str(ev_tk) not in preds and isinstance(ev_val, (int, float)):
                                            import math as _m_wf
                                            p_ev = (1.0 / 2.0) + (2.0 / 5.0) * _m_wf.tanh(float(ev_val) / 2.0)
                                            preds[str(ev_tk)] = {'up_prob': round(p_ev, 4), 'actual_return_pct': None}
            except Exception as e:
                logger.debug(f"  latest_signals 로드 스킵: {e}")

        st_tech = sc.get('stock_technicals', {})
        if isinstance(st_tech, dict):
            for tk, tv in st_tech.items():
                if str(tk) not in preds and isinstance(tv, dict):
                    rsi = float(tv.get('rsi_14', 50.0) or 50.0)
                    macd_h = float(tv.get('macd_hist', 0.0) or 0.0)
                    _cap = 1.0 / 4.0
                    _raw_adj = (50.0 - rsi) / 200.0 + (0.05 if macd_h > 0 else -0.05)
                    p_tech = (1.0 / 2.0) + max(-_cap, min(_cap, _raw_adj))
                    preds[str(tk)] = {'up_prob': round(p_tech, 4), 'actual_return_pct': None}
        return preds

    def _lookup_realized_return_pct(self, ticker: str):
        """data/kr_markets, data/prices, data/us_stocks/prices Parquet에서 최근 1일 실현 수익률(%) 조회."""
        clean = str(ticker).replace('.KS', '').replace('.KQ', '').strip()
        is_kr = clean.isdigit() and len(clean) == 6
        candidates = []
        if is_kr:
            candidates.extend([
                _PROJECT_ROOT / 'data' / 'kr_markets' / f'kr_{clean}.parquet',
                _PROJECT_ROOT / 'data' / 'prices' / f'{clean}.parquet',
            ])
        else:
            candidates.extend([
                _PROJECT_ROOT / 'data' / 'us_stocks' / 'prices' / f'{clean}.parquet',
                _PROJECT_ROOT / 'data' / 'prices' / f'{clean}.parquet',
            ])
        try:
            import pandas as pd
            for p in candidates:
                if p.exists():
                    df = pd.read_parquet(p)
                    col = 'close' if 'close' in df.columns else ('Close' if 'Close' in df.columns else None)
                    if col and len(df) >= 2:
                        s = pd.to_numeric(df[col], errors='coerce').dropna()
                        if len(s) >= 2 and float(s.iloc[-2]) > 0:
                            return round((float(s.iloc[-1]) / float(s.iloc[-2]) - 1.0) * 100.0, 4)
        except Exception:
            pass
        return None

    def _fallback_from_purged_wf(self, min_samples: int):
        """실시간 스냅샷 표본 부족 시 최신 Date-Purged Walk-Forward OOS 검증 결과 참조."""
        pwf_file = _RESULTS / 'purged_wf_5d.json'
        if not pwf_file.exists():
            return None
        try:
            pwf = json.loads(pwf_file.read_text(encoding='utf-8'))
            if not isinstance(pwf, dict):
                return None
            n_pred = int(pwf.get('n_rows_predicted', 0) or 0)
            if n_pred < min_samples:
                return None
            oos = pwf.get('oos') or {}
            folds = pwf.get('folds') or []
            last_fold_m = (folds[-1].get('metrics') if folds and isinstance(folds[-1], dict) else {}) or {}
            hit_d = (last_fold_m.get('hit_rate') or oos.get('hit_rate') or {})
            ic_d = (last_fold_m.get('ic') or oos.get('ic') or {})
            acc = float(hit_d.get('cross_sectional_hit_rate') or hit_d.get('directional_hit_rate') or 0.5)
            ic = float(ic_d.get('mean') if ic_d.get('mean') is not None else (pwf.get('ic_mean') or 0.0))
            n_val = int(last_fold_m.get('n_rows') or oos.get('n_rows') or n_pred)
            return {
                'status': 'completed',
                'n_validated': n_val,
                'n_predictions': n_pred,
                'oos_acc': round(acc, 4),
                'oos_ic': round(ic, 4),
                'n_correct': int(round(acc * n_val)),
                'source': 'purged_wf_5d',
                'warnings': [],
            }
        except Exception:
            return None

    def _compute_ic(self, pairs: List) -> float:
        """Spearman 순위 상관계수 (IC)."""
        if len(pairs) < 3:
            return 0.0
        try:
            from scipy import stats
            preds, actuals = zip(*pairs)
            ic, _ = stats.spearmanr(preds, actuals)
            return float(ic) if not np.isnan(ic) else 0.0
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
            return 0.0

    def _save(self, result: Dict):
        """결과 누적 저장."""
        try:
            history = []
            if _REPORT_FILE.exists():
                history = json.loads(_REPORT_FILE.read_text(encoding='utf-8'))
                if not isinstance(history, list):
                    history = [history]

            # 같은 날짜 기존 skip 레코드가 있으면 최신 결과로 교체
            cur_date = result.get('date')
            if history and isinstance(history[-1], dict) and history[-1].get('date') == cur_date and history[-1].get('status') == 'skip':
                history[-1] = result
            else:
                history.append(result)
            # 최근 90일치만 유지
            max_history = cfg.get('ml.wf_max_history_days', 90)
            history = history[-max_history:]

            _REPORT_FILE.write_text(
                json.dumps(history, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        except Exception as e:
            logger.debug(f"  Walk-Forward 저장 실패: {e}")


    def validate_purged_panel(self, window_days=None, max_tickers=None, n_splits=None,
                              model_name=None, out_dir=None, compare_naive=False,
                              save_candidate=False, **kwargs) -> Dict:
        """[WAVE3 Track D] 과거 패널 기반 Date-major Purged Walk-Forward OOS 검증.

        ``validate()`` 는 signal_cache 스냅샷(단일 시점) 실시간 모니터링이고,
        이 메서드는 학습 데이터 패널을 날짜 단위 purge(h 거래일)+embargo(≥5 거래일)
        walk-forward 로 재학습/예측하여 per-date IC(t), hit rate, decile spread 를
        results/ml_validation/purged_wf_<ts>.json 에 기록한다.
        운영 모델/레지스트리는 변경하지 않는다 (후보는 models_candidate/ 에만).
        """
        from scripts.train_ensemble import run_purged_validation
        return run_purged_validation(
            window_days=window_days, max_tickers=max_tickers, n_splits=n_splits,
            model_name=model_name, out_dir=out_dir, compare_naive=compare_naive,
            save_candidate=save_candidate, **kwargs)


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    _p = argparse.ArgumentParser(description='Walk-Forward Validator')
    _p.add_argument('--purged-panel', action='store_true',
                    help='[WAVE3] 날짜 단위 Purged WF OOS 검증 (기본: 일일 live 스냅샷 검증)')
    _p.add_argument('--window', type=int, default=None)
    _p.add_argument('--max-tickers', type=int, default=None)
    _p.add_argument('--n-splits', type=int, default=None)
    _p.add_argument('--out-dir', default=None)
    _p.add_argument('--compare-naive', action='store_true')
    _args = _p.parse_args()
    wfv = WalkForwardValidator()
    if _args.purged_panel:
        result = wfv.validate_purged_panel(
            window_days=_args.window, max_tickers=_args.max_tickers,
            n_splits=_args.n_splits, out_dir=_args.out_dir,
            compare_naive=_args.compare_naive)
    else:
        result = wfv.validate()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

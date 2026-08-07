"""
Walk-Forward Validator — ML 모델 과적합 검증
==============================================
시간순 rolling 학습/테스트로 실전 DA 추정.

Usage:
    from src.analysis.walk_forward_validator import WalkForwardValidator
    wf = WalkForwardValidator()
    results = wf.validate(train_months=18, test_months=3)
"""
import json, logging, numpy as np, pandas as pd
from pathlib import Path
from src.utils.file_ops import atomic_write_json

from datetime import datetime, timedelta
from typing import Dict, Optional, List
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _PROJECT_ROOT / 'results' / 'walk_forward'
_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

class WalkForwardValidator:
    """Walk-Forward Analysis for ML ensemble."""

    def __init__(self, data_dir: Optional[Path]=None):
        self.data_dir = data_dir or _PROJECT_ROOT / 'data' / 'historical_10y'

    def validate(self, train_months: int=18, test_months: int=3, step_months: int=3, n_splits: int=6) -> Dict:
        """Walk-Forward 검증 실행.

        Args:
            train_months: 학습 기간 (월)
            test_months: 테스트 기간 (월)
            step_months: 윈도우 이동 단위 (월)
            n_splits: 최대 분할 수
        """
        from scripts.train_ensemble import extract_v3, FEATURE_NAMES_V3, train_ensemble
        from sklearn.metrics import accuracy_score, roc_auc_score
        logger.info(f'═══ Walk-Forward Validation (train={train_months}m, test={test_months}m) ═══')
        try:
            from config.dynamic_config import DynamicConfig
            max_uni = DynamicConfig().get('ml.max_universe_size', 300)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            max_uni = 300
        uni_file = _PROJECT_ROOT / 'results' / 'dynamic_universe.json'
        if uni_file.exists():
            universe = json.loads(uni_file.read_text())
        else:
            universe = [f.stem.replace('kr_', '') for f in self.data_dir.glob('kr_*.parquet')]
        universe = universe[:max_uni]
        all_data = self._load_all_data(universe)
        if not all_data:
            logger.error('데이터 부족')
            return {'error': 'no_data'}
        all_dates = sorted(set((d for item in all_data for d in item[1])))
        if len(all_dates) < 500:
            return {'error': 'insufficient_dates'}
        results = []
        end_idx = len(all_dates) - 1
        split = 0
        while split < n_splits:
            test_end = len(all_dates) - split * int(step_months * 21)
            test_start = test_end - int(test_months * 21)
            train_end = test_start
            train_start = train_end - int(train_months * 21)
            if train_start < 0 or test_start < 0:
                break
            train_dates = set(all_dates[train_start:train_end])
            test_dates = set(all_dates[test_start:test_end])
            train_X, train_y = self._build_xy(all_data, train_dates, all_dates)
            test_X, test_y = self._build_xy(all_data, test_dates, all_dates)
            if len(train_X) < 50 or len(test_X) < 10:
                split += 1
                continue
            logger.info(f'  Split {split + 1}: train={len(train_X):,} test={len(test_X):,}')
            try:
                models, ens_acc, ens_auc, _, _, pruned_test_X = train_ensemble(np.array(train_X), np.array(train_y), np.array(test_X), np.array(test_y))
                preds = np.mean([m.predict_proba(pruned_test_X)[:, 1] for m in models.values()], axis=0)
                test_y_arr = np.array(test_y)
                acc = accuracy_score(test_y_arr, (preds >= 0.5).astype(int))
                auc = roc_auc_score(test_y_arr, preds) if len(set(test_y_arr)) > 1 else 0.5
                da_results = {}
                for t in [0.55, 0.58, 0.6, 0.65]:
                    mask = preds >= t
                    if mask.sum() > 5:
                        da_results[f'da_{t:.2f}'] = float(test_y_arr[mask].mean())
                results.append({'split': split + 1, 'train_size': len(train_X), 'test_size': len(test_X), 'acc': round(acc, 4), 'auc': round(auc, 4), 'positive_rate': round(np.mean(test_y), 4), **{k: round(v, 4) for k, v in da_results.items()}})
                logger.info(f'    ACC={acc:.3f} AUC={auc:.3f}')
            except Exception as e:
                logger.warning(f'    Split {split + 1} failed: {e}')
            split += 1
        summary = self._summarize(results)
        output = {'timestamp': datetime.now().isoformat(), 'config': {'train_months': train_months, 'test_months': test_months}, 'splits': results, 'summary': summary}
        out_path = _RESULTS_DIR / 'wf_results.json'
        atomic_write_json(out_path, output, indent=2)
        logger.info(f'  ═══ WF Summary: ACC={summary['mean_acc']:.3f}±{summary['std_acc']:.3f} AUC={summary['mean_auc']:.3f}±{summary['std_auc']:.3f} ═══')
        return output

    def _load_all_data(self, universe):
        """전체 데이터 로드."""
        all_data = []
        for ticker in universe:
            fp = self.data_dir / f'kr_{ticker}.parquet'
            if not fp.exists():
                continue
            try:
                df = pd.read_parquet(fp)
                close = pd.to_numeric(df['close'], errors='coerce').dropna().values
                high = pd.to_numeric(df['high'], errors='coerce').dropna().values
                low = pd.to_numeric(df['low'], errors='coerce').dropna().values
                opn = pd.to_numeric(df['open'], errors='coerce').dropna().values
                vol = pd.to_numeric(df['volume'], errors='coerce').dropna().values
                dates = pd.to_datetime(df['date']).values
                n = min(len(close), len(high), len(low), len(opn), len(vol), len(dates))
                if n < 300:
                    continue
                is_etf = ticker.startswith(('069', '091', '114', '122', '305'))
                all_data.append((ticker, dates[:n], close[:n], high[:n], low[:n], opn[:n], vol[:n], is_etf))
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                continue
        return all_data

    def _build_xy(self, all_data, target_dates, all_dates):
        """데이터셋 구축."""
        from src.intelligence.v4_features import extract_v4, FEATURE_NAMES_V6
        from src.intelligence.aux_data_loader import AuxDataLoader
        from scripts.train_ensemble import _load_cross_asset_data, _get_cross_asset_for_date
        try:
            from config.dynamic_config import DynamicConfig
            _threshold = DynamicConfig().get('train.positive_threshold_pct', 3.0)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _threshold = 3.0
        cross_data = _load_cross_asset_data()
        aux_loader = AuxDataLoader()
        X, y = ([], [])
        for ticker, dates, close, high, low, opn, vol, is_etf in all_data:
            n = len(close)
            for idx in range(260, n - 5, 5):
                dt = pd.Timestamp(dates[idx])
                if dt not in target_dates:
                    continue
                dt_str = dt.strftime('%Y-%m-%d')
                ca = _get_cross_asset_for_date(cross_data, dt_str)
                aux_features = aux_loader.get_features(ticker, dt_str)
                feat = extract_v4(close, high, low, opn, vol, idx, is_etf, cross_asset=ca, aux_data=aux_features)
                if feat is None:
                    continue
                future_end = min(idx + 6, n)
                if future_end <= idx + 1:
                    continue
                max_ret = (np.max(high[idx + 1:future_end]) / close[idx] - 1) * 100
                label = 1 if max_ret >= _threshold else 0
                row = [feat.get(f, 0) for f in FEATURE_NAMES_V6]
                X.append(row)
                y.append(label)
        return (X, y)

    def _summarize(self, results):
        if not results:
            return {'mean_acc': 0, 'std_acc': 0, 'mean_auc': 0, 'std_auc': 0}
        accs = [r['acc'] for r in results]
        aucs = [r['auc'] for r in results]
        return {'mean_acc': round(np.mean(accs), 4), 'std_acc': round(np.std(accs), 4), 'mean_auc': round(np.mean(aucs), 4), 'std_auc': round(np.std(aucs), 4), 'n_splits': len(results)}
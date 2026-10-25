import json
import logging
import pickle
import shutil
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, Any, Tuple
logger = logging.getLogger(__name__)

class ModelRegistryManager:
    """
    메달리온 펀드 수준의 완전 자동화된 ML 모델 버전 관리 및 배포(CI/CD) 시스템.
    학습된 모델을 날짜/버전별로 보존하고, 검증 스코어(Challenger)에 따라 
    운영 환경에 배포(promote)할지 결정합니다.
    """

    def __init__(self, registry_dir: str=None):
        if registry_dir is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            registry_dir = project_root / 'results' / 'models'
        self.registry_dir = Path(registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.registry_dir / 'registry_metadata.json'
        self.latest_link = self.registry_dir / 'stock_ranker_ensemble.pkl'

    def _load_registry(self) -> Dict:
        if self.registry_file.exists():
            try:
                return json.loads(self.registry_file.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
        return {'versions': {}, 'active_version': None}

    def _save_registry(self, data: Dict):
        atomic_write_json(self.registry_file, data, indent=2)

    def register_candidate(self, models_dict: Dict, metadata: Dict) -> str:
        """
        새로 학습된 모델을 Candidate로 등록 (버전 지정)
        """
        now = datetime.now()
        version_id = f"v_{now.strftime('%Y%m%d_%H%M%S')}"
        model_path = self.registry_dir / f'ensemble_{version_id}.pkl'
        pkg = {
            'models': models_dict,
            'version': version_id,
            'metadata': metadata,
            'registered_at': now.isoformat(),
            'val_acc': metadata.get('val_acc', 0.0),
            'val_auc': metadata.get('val_auc', 0.0),
            'feature_names': metadata.get('feature_names', []),
            'model_weights': metadata.get('ensemble_weights', metadata.get('model_weights', {})),
            'feature_means': metadata.get('feature_means', []),
            'feature_stds': metadata.get('feature_stds', []),
            'raw_feature_means': metadata.get('raw_feature_means', []),
            'raw_feature_stds': metadata.get('raw_feature_stds', []),
        }
        with open(model_path, 'wb') as f:
            pickle.dump(pkg, f)
        registry = self._load_registry()
        registry['versions'][version_id] = {'path': str(model_path.name), 'metadata': metadata, 'status': 'candidate', 'registered_at': now.isoformat()}
        self._save_registry(registry)
        self._prune_old_models(registry)
        logger.info(f"  📦 Model {version_id} registered as candidate.")
        return version_id

    def _prune_old_models(self, registry: Dict):
        """디스크 고갈 방지를 위해 active 모델 및 최신 3개 버전 외 구버전 ensemble_v_*.pkl 정리."""
        try:
            active_ver = registry.get('active_version')
            active_file = (registry.get('versions', {}).get(active_ver) or {}).get('path') if active_ver else None
            pkl_files = sorted(self.registry_dir.glob('ensemble_v_*.pkl'), key=lambda p: p.stat().st_mtime, reverse=True)
            for old_p in pkl_files[3:]:
                if old_p.name != active_file and not old_p.samefile(self.latest_link) if self.latest_link.exists() else (old_p.name != active_file):
                    old_p.unlink(missing_ok=True)
        except Exception:
            pass

    def evaluate_and_promote(self, new_version_id: str, new_auc: float, min_improvement: float=0.005) -> Tuple[bool, str]:
        """
        현재 Active 모델과 비교하여 더 나으면 Promote (symlink 교체).
        """
        registry = self._load_registry()
        if new_version_id not in registry['versions']:
            return (False, f"Version {new_version_id} not found.")
        active_version = registry.get('active_version')
        if not active_version or active_version not in registry['versions']:
            self._promote(new_version_id, registry, 'No active baseline. Auto-promoted.')
            return (True, 'Auto-promoted (No baseline)')
        old_metadata = registry['versions'][active_version].get('metadata', {})
        new_metadata = registry['versions'][new_version_id].get('metadata', {})
        old_auc = old_metadata.get('val_auc', 0.0)
        improvement = new_auc - old_auc
        old_pwf = old_metadata.get('purged_wf_oos') or old_metadata.get('purged_wf') or {}
        new_pwf = new_metadata.get('purged_wf_oos') or new_metadata.get('purged_wf') or {}
        _raw_old_ic = old_pwf.get('ic_mean')
        _raw_new_ic = new_pwf.get('ic_mean')
        old_ic = float(_raw_old_ic) if _raw_old_ic is not None else 0.0
        new_ic = float(_raw_new_ic) if _raw_new_ic is not None else 0.0
        new_nw_t = float(new_pwf.get('ic_nw_t_stat') or new_pwf.get('ic_t_stat') or 0.0)
        old_fc = old_metadata.get('feature_count') or len(old_metadata.get('feature_names') or [])
        new_fc = new_metadata.get('feature_count') or len(new_metadata.get('feature_names') or [])
        _t_gate = 2.0
        _min_schema_auc = 0.50
        _min_schema_ic = 0.0
        _is_forced = bool(new_metadata.get('force_promote') or min_improvement <= 0.0)
        _schema_quality_ok = (
            _is_forced
            or (float(new_auc) >= _min_schema_auc)
            or (_raw_new_ic is not None and float(_raw_new_ic) >= _min_schema_ic)
        )
        if improvement >= min_improvement:
            msg = f'Adopted: old_AUC={old_auc:.4f} → new_AUC={new_auc:.4f} (+{improvement:.4f})'
            self._promote(new_version_id, registry, msg)
            return (True, msg)
        elif new_ic > old_ic and new_nw_t >= _t_gate:
            msg = f'Adopted (PurgedWF OOS IC): old_IC={old_ic:.4f} → new_IC={new_ic:.4f} (NW t={new_nw_t:.2f})'
            self._promote(new_version_id, registry, msg)
            return (True, msg)
        elif old_fc and new_fc and int(old_fc) != int(new_fc) and _schema_quality_ok:
            msg = f'Adopted (Feature Schema Upgrade): {old_fc} → {new_fc} features (new_IC={new_ic:.4f})'
            self._promote(new_version_id, registry, msg)
            return (True, msg)
        elif not old_metadata.get('raw_feature_means') and new_metadata.get('raw_feature_means') and _schema_quality_ok:
            msg = f'Adopted (Raw Stats Schema Upgrade): old_AUC={old_auc:.4f} → new_AUC={new_auc:.4f}'
            self._promote(new_version_id, registry, msg)
            return (True, msg)
        else:
            msg = f'Rejected: old_AUC={old_auc:.4f} → new_AUC={new_auc:.4f} (+{improvement:.4f} < {min_improvement})'
            registry['versions'][new_version_id]['status'] = 'rejected'
            registry['versions'][new_version_id]['reject_reason'] = msg
            self._save_registry(registry)
            logger.warning(f"  ⚠️ Challenger {msg}")
            return (False, msg)

    def _promote(self, version_id: str, registry: Dict, reason: str):
        """
        특정 모델을 Active로 승격시키고 latest_model 심볼릭 링크를 갱신합니다.
        """
        model_filename = registry['versions'][version_id]['path']
        target_path = self.registry_dir / model_filename
        if self.latest_link.exists() or self.latest_link.is_symlink():
            try:
                self.latest_link.unlink()
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
                pass
        try:
            self.latest_link.symlink_to(target_path.name)
        except OSError:
            shutil.copy2(target_path, self.latest_link)
        promoted_at = datetime.now().isoformat()
        registry['active_version'] = version_id
        registry['versions'][version_id]['status'] = 'active'
        registry['versions'][version_id]['promoted_at'] = promoted_at
        registry['versions'][version_id]['promote_reason'] = reason
        self._save_registry(registry)

        meta_payload = dict(registry['versions'][version_id].get('metadata', {}))
        meta_payload.setdefault('train_date', registry['versions'][version_id].get('registered_at', promoted_at))
        meta_payload['version'] = version_id
        meta_payload['promoted_at'] = promoted_at
        meta_payload['promote_reason'] = reason
        if 'ensemble_weights' in meta_payload and 'model_weights' not in meta_payload:
            meta_payload['model_weights'] = meta_payload['ensemble_weights']
        atomic_write_json(self.registry_dir / 'ensemble_meta.json', meta_payload, indent=2)
        logger.info(f"  🚀 Model {version_id} successfully promoted to ACTIVE. Reason: {reason}")

    def get_active_model(self) -> Dict:
        """
        현재 Active(Production) 모델 패키지 반환
        """
        if not self.latest_link.exists():
            return None
        try:
            from src.streams.s2_ml_alpha.ml_stream import S2MLAlphaStream
            return S2MLAlphaStream._safe_pickle_load(self.latest_link)
        except Exception:
            with open(self.latest_link, 'rb') as f:
                return pickle.load(f)
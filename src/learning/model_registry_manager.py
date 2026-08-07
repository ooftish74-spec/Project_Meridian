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
            registry_dir = project_root / 'models' / 's2_ensemble'
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
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {'versions': {}, 'active_version': None}

    def _save_registry(self, data: Dict):
        atomic_write_json(self.registry_file, data, indent=2)

    def register_candidate(self, models_dict: Dict, metadata: Dict) -> str:
        """
        새로 학습된 모델을 Candidate로 등록 (버전 지정)
        """
        now = datetime.now()
        version_id = f'v_{now.strftime('%Y%m%d_%H%M%S')}'
        model_path = self.registry_dir / f'ensemble_{version_id}.pkl'
        pkg = {'models': models_dict, 'version': version_id, 'metadata': metadata, 'registered_at': now.isoformat()}
        with open(model_path, 'wb') as f:
            pickle.dump(pkg, f)
        registry = self._load_registry()
        registry['versions'][version_id] = {'path': str(model_path.name), 'metadata': metadata, 'status': 'candidate', 'registered_at': now.isoformat()}
        self._save_registry(registry)
        logger.info(f'  📦 Model {version_id} registered as candidate.')
        return version_id

    def evaluate_and_promote(self, new_version_id: str, new_auc: float, min_improvement: float=0.005) -> Tuple[bool, str]:
        """
        현재 Active 모델과 비교하여 더 나으면 Promote (symlink 교체).
        """
        registry = self._load_registry()
        if new_version_id not in registry['versions']:
            return (False, f'Version {new_version_id} not found.')
        active_version = registry.get('active_version')
        if not active_version or active_version not in registry['versions']:
            self._promote(new_version_id, registry, 'No active baseline. Auto-promoted.')
            return (True, 'Auto-promoted (No baseline)')
        old_metadata = registry['versions'][active_version].get('metadata', {})
        old_auc = old_metadata.get('val_auc', 0.0)
        improvement = new_auc - old_auc
        if improvement >= min_improvement:
            msg = f'Adopted: old_AUC={old_auc:.4f} → new_AUC={new_auc:.4f} (+{improvement:.4f})'
            self._promote(new_version_id, registry, msg)
            return (True, msg)
        else:
            msg = f'Rejected: old_AUC={old_auc:.4f} → new_AUC={new_auc:.4f} (+{improvement:.4f} < {min_improvement})'
            registry['versions'][new_version_id]['status'] = 'rejected'
            registry['versions'][new_version_id]['reject_reason'] = msg
            self._save_registry(registry)
            logger.warning(f'  ⚠️ Challenger {msg}')
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
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        try:
            self.latest_link.symlink_to(target_path.resolve())
        except OSError:
            shutil.copy2(target_path, self.latest_link)
        registry['active_version'] = version_id
        registry['versions'][version_id]['status'] = 'active'
        registry['versions'][version_id]['promoted_at'] = datetime.now().isoformat()
        registry['versions'][version_id]['promote_reason'] = reason
        self._save_registry(registry)
        logger.info(f'  🚀 Model {version_id} successfully promoted to ACTIVE. Reason: {reason}')

    def get_active_model(self) -> Dict:
        """
        현재 Active(Production) 모델 패키지 반환
        """
        if not self.latest_link.exists():
            return None
        with open(self.latest_link, 'rb') as f:
            return pickle.load(f)
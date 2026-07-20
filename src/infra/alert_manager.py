import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)

class AlertManager:
    """
    시스템 경고 및 에러를 캡처하여 대시보드(system_alerts.json)에 기록하는 관리자.
    
    기존 try/except처럼 에러를 조용히 넘기지 않고, 명시적으로 기록하여
    자가 복구 이력과 함께 가시성을 확보합니다.
    """

    def __init__(self):
        self._project_root = Path(__file__).resolve().parent.parent.parent
        self._alert_file = self._project_root / 'results' / 'system_alerts.json'
        self._alert_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_alerts(self) -> List[Dict]:
        if not self._alert_file.exists():
            return []
        try:
            return json.loads(self._alert_file.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            return []

    def _save_alerts(self, alerts: List[Dict]) -> None:
        try:
            if len(alerts) > 100:
                alerts = alerts[-100:]
            self._alert_file.write_text(json.dumps(alerts, indent=2, ensure_ascii=False))
        except Exception as e:
            logger.error(f'AlertManager 저장 실패: {e}')

    def report_error(self, source: str, message: str, severity: str='warning', context: Optional[Dict]=None) -> None:
        """새로운 에러를 보고합니다."""
        alerts = self._load_alerts()
        new_alert = {'timestamp': datetime.now().isoformat(), 'source': source, 'message': message[:500], 'severity': severity, 'status': 'active', 'context': context or {}}
        alerts.append(new_alert)
        self._save_alerts(alerts)
        logger.debug(f'🚨 AlertManager 캡처됨: [{severity.upper()}] {source} - {message}')

    def resolve_alert(self, source: str, resolution_msg: str) -> None:
        """진행 중인 에러를 '해결됨(자가 복구 성공)'으로 표시합니다."""
        alerts = self._load_alerts()
        resolved_count = 0
        for alert in reversed(alerts):
            if alert.get('source') == source and alert.get('status') == 'active':
                alert['status'] = 'resolved'
                alert['resolved_at'] = datetime.now().isoformat()
                alert['resolution'] = resolution_msg
                resolved_count += 1
                break
        if resolved_count > 0:
            self._save_alerts(alerts)
            logger.debug(f'💚 AlertManager 복구 보고됨: {source} - {resolution_msg}')
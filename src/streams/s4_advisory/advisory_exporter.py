from src.utils.file_ops import atomic_write_json, atomic_write_text
import json
import logging
from pathlib import Path
from typing import List, Dict
from datetime import datetime
logger = logging.getLogger(__name__)

class AdvisoryExporter:
    """S4 ISA, IRP, 연금저축 계좌에 대한 수동 매매(Advisory) 오더를 추출하고 포맷팅합니다."""

    def __init__(self):
        self._project_root = Path(__file__).resolve().parent.parent.parent.parent
        self._results_dir = self._project_root / 'results'

    def export(self, signals: List[Dict], regime: str, confidence: float):
        """시그널 중 자동매매(BROKERAGE)가 아닌 수동매매 계좌를 필터링하여 리포트 생성 및 텔레그램 발송"""
        advisory_accounts = ['ISA', 'IRP', 'PENSION']
        advisory_signals = [s for s in signals if s.get('account') in advisory_accounts]
        if not advisory_signals:
            return
        json_path = self._results_dir / 'advisory_orders.json'
        try:
            output = {'timestamp': datetime.now().isoformat(), 'regime': regime, 'regime_confidence': confidence, 'orders': advisory_signals}
            atomic_write_json(json_path, output, indent=2)
        except Exception as e:
            logger.error(f'  Advisory JSON 저장 실패: {e}')
        md_path = self._results_dir / 'advisory_report.md'
        md_content = self._generate_markdown(advisory_signals, regime, confidence)
        try:
            atomic_write_text(md_path, md_content)
            logger.info(f'  📝 S4 Advisory 리포트 생성 완료: {md_path.name}')
        except Exception as e:
            logger.error(f'  Advisory Markdown 저장 실패: {e}')

    def _generate_markdown(self, signals: List[Dict], regime: str, confidence: float) -> str:
        """Advisory 리포트를 마크다운 형식으로 생성합니다."""
        lines = []
        lines.append(f'# 🏛️ Project Meridian - S4 Advisory Report')
        lines.append(f'**Date**: {datetime.now().strftime('%Y-%m-%d %H:%M')}')
        lines.append(f'**Regime**: {regime.upper()} (Confidence: {confidence:.2f})')
        lines.append('')
        lines.append('수동 매매가 필요한 연금/ISA 계좌의 추천 오더입니다.')
        lines.append('')
        for sig in signals:
            acct = sig.get('account', 'UNKNOWN')
            lines.append(f'## 💼 {acct} 계좌')
            target_list = sig.get('target', [])
            if not target_list:
                lines.append('- 현재 진입 추천 종목이 없습니다.')
            else:
                for asset in target_list:
                    ticker = asset.get('ticker', '')
                    weight = asset.get('weight', 0)
                    lines.append(f'- **{ticker}**: {weight * 100:.1f}%')
            lines.append('')
        return '\n'.join(lines)
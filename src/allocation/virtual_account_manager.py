"""
Meridian — Virtual Accounting Engine
====================================
하나의 거대한 프라임(마스터) 계좌 안에서, 소프트웨어 장부(Virtual Sub-ledger)를 통해
개별 스트림(S1, S2, S3 등) 및 자산군(Alpha, Beta) 간의 자금을 격리하고 통제합니다.

물리적 계좌 이체 없이 엣지(Edge) 변화에 따라 즉시 자본을 재배분(Reallocate)할 수 있습니다.
"""
import json
import logging
from typing import Dict, Any, List
from pathlib import Path
logger = logging.getLogger(__name__)

class VirtualAccountManager:

    def __init__(self, state_file: str='results/virtual_ledger.json', total_master_capital: float=200000000.0):
        self._project_root = Path(__file__).resolve().parent.parent.parent
        self.state_file = self._project_root / state_file
        self.total_master_capital = total_master_capital
        self.ledger = {'master_cash': self.total_master_capital, 'streams': {'S1': {'allocated': 0.0, 'used': 0.0}, 'S2': {'allocated': 0.0, 'used': 0.0}, 'S3': {'allocated': 0.0, 'used': 0.0}, 'S5': {'allocated': 0.0, 'used': 0.0}, 'S10': {'allocated': 0.0, 'used': 0.0}, 'Beta': {'allocated': 0.0, 'used': 0.0}}, 'timestamp': None}
        self._load_ledger()

    def _load_ledger(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    self.ledger = json.load(f)
            except Exception as e:
                logger.critical(f'가상 장부 로드 실패: {e}. 기본값 사용.', exc_info=True)
        else:
            self._save_ledger()

    def _save_ledger(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        import datetime
        self.ledger['timestamp'] = datetime.datetime.now().isoformat()
        with open(self.state_file, 'w') as f:
            json.dump(self.ledger, f, indent=4)

    def allocate_capital(self, target_allocations: Dict[str, float]):
        """
        자본 배분기(Capital Allocator)에 의해 산출된 최적 배분 금액을 장부에 반영합니다.
        """
        logger.info('가상 장부 자본 재배분 (Virtual Reallocation) 실행')
        total_requested = sum(target_allocations.values())
        if total_requested > self.total_master_capital:
            scale = self.total_master_capital / total_requested
            target_allocations = {k: v * scale for k, v in target_allocations.items()}
            logger.warning(f'할당 요청액이 총 자본을 초과하여 {scale:.2f} 비율로 스케일 다운됨.')
        allocated_sum = 0.0
        for stream_id, amount in target_allocations.items():
            if stream_id in self.ledger['streams']:
                self.ledger['streams'][stream_id]['allocated'] = amount
                allocated_sum += amount
        self.ledger['master_cash'] = self.total_master_capital - allocated_sum
        self._save_ledger()
        logger.info(f'재배분 완료. Master 유휴 현금: {self.ledger['master_cash']:,.0f} KRW')

    def reserve_capital_for_trade(self, stream_id: str, amount: float) -> bool:
        """
        특정 스트림이 주문을 발생시킬 때, 가상 장부에서 해당 금액을 차감(예약)합니다.
        """
        if stream_id not in self.ledger['streams']:
            logger.error(f'알 수 없는 스트림 ID: {stream_id}')
            return False
        available = self.ledger['streams'][stream_id]['allocated'] - self.ledger['streams'][stream_id]['used']
        if available >= amount:
            self.ledger['streams'][stream_id]['used'] += amount
            self._save_ledger()
            return True
        else:
            if self.ledger['master_cash'] >= amount:
                logger.warning(f'[{stream_id}] 자체 할당량 한도 초과. 마스터 잉여 현금에서 {amount:,.0f} 차입(Borrowing) 실행.')
                self.ledger['master_cash'] -= amount
                self.ledger['streams'][stream_id]['used'] += amount
                self._save_ledger()
                return True
            logger.error(f'[{stream_id}] 할당량 및 마스터 잉여 현금 모두 부족 (요청: {amount:,.0f})')
            return False

    def release_capital_from_trade(self, stream_id: str, amount: float):
        """
        포지션 청산 시, 사용되었던 가상 자본을 반환합니다.
        """
        if stream_id in self.ledger['streams']:
            self.ledger['streams'][stream_id]['used'] = max(0.0, self.ledger['streams'][stream_id]['used'] - amount)
            self._save_ledger()
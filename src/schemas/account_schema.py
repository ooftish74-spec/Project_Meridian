"""
Account & Telemetry Schemas — Strict Dataclass Data Contracts
================================================================

자유 딕셔너리(dict) 방식의 문자열 Key 맵핑 오타 및 수신 누락 방지를 위한 강타입 스키마 모듈.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional

@dataclass
class PositionSchema:
    ticker: str
    quantity: int
    avg_price: float
    current_price: float
    market: str = "KR"  # "KR" | "US"
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
            "current_price": self.current_price,
            "market": self.market,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
        }

@dataclass
class AccountSchema:
    cash_krw: float
    cash_usd: float
    positions_value_krw: float
    total_equity_krw: float
    fx_rate: float = 1386.1
    mode: str = "live"
    positions: Dict[str, PositionSchema] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cash_krw": self.cash_krw,
            "cash_usd": self.cash_usd,
            "positions_value_krw": self.positions_value_krw,
            "total_equity_krw": self.total_equity_krw,
            "fx_rate": self.fx_rate,
            "mode": self.mode,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
        }

@dataclass
class TelemetrySchema:
    vix: float = 18.0
    vkospi: float = 18.0
    usdkrw: float = 1380.0
    spx_change_1d: float = 0.0
    ixic_change_1d: float = 0.0
    soxx_change_1d: float = 0.0
    ofi_z: float = 0.0
    lead_lag: float = 0.0
    wag_the_dog_active: bool = False
    vix_momentum_reversal: bool = False
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vix": self.vix,
            "vkospi": self.vkospi,
            "usdkrw": self.usdkrw,
            "spx_change_1d": self.spx_change_1d,
            "ixic_change_1d": self.ixic_change_1d,
            "soxx_change_1d": self.soxx_change_1d,
            "ofi_z": self.ofi_z,
            "lead_lag": self.lead_lag,
            "wag_the_dog_active": self.wag_the_dog_active,
            "vix_momentum_reversal": self.vix_momentum_reversal,
            "timestamp": self.timestamp,
        }

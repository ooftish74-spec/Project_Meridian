# 🌐 Project Meridian

> 4-Stream Quantitative Trading System for KRX / US Markets

---

## Overview

Project Meridian is a systematic, event-driven trading system designed for the Korean Exchange (KRX) and U.S. equity/ETF markets. It operates via four independent **Streams** (S1–S4), each with distinct investment strategies, time horizons, and risk profiles. All streams are orchestrated through a unified pipeline and measured against a single source of truth (SSoT).

### Key Principles

1. **One Truth, Many Views** — `MeasurementEngine` is the SSoT; all performance metrics derive from one calculation.
2. **Everything is an Event** — `EventLedger` records every system action for auditability.
3. **Automatic Risk Gates** — Six-layer defense: Kill Switch → Crash Defense → Drawdown Guard → Exposure Orchestrator → Realtime VaR → Medallion Orchestrator.
4. **Shadow-First** — All strategies run in shadow mode before any real capital deployment.

---

## Architecture

```
Project_Meridian/
├── config/                 # DynamicConfig (SSoT), launchd plists
│   ├── dynamic_config.py   # 250+ parameters with hot-reload
│   └── launchd/            # macOS launchd scheduler plists
├── scripts/                # Pipeline entry points
│   ├── daily_pipeline.py   # 16-phase daily orchestrator
│   ├── stream_orchestrator.py  # S1~S4 execution engine
│   └── train_ensemble.py   # ML model training
├── src/
│   ├── streams/            # S1 Edge / S2 ML Alpha / S3 Factor / S4 Advisory
│   ├── regime/             # Regime detection (Bull/Caution/Bear/Crash)
│   ├── risk/               # Kill Switch, Crash Defense, Drawdown Guard, etc.
│   ├── execution/          # TWAP/VWAP execution, KIS API client
│   ├── measurement/        # MeasurementEngine (SSoT), ShadowRecorder, EventLedger
│   ├── intelligence/       # OIS, Overnight Intel, Cross-Asset signals
│   ├── data_collection/    # 17+ data collectors (pykrx, yfinance, DART, etc.)
│   ├── learning/           # SelfLearning (IC-based auto-weight adjustment)
│   └── interface/          # Reports, Telegram bot, Dashboard
├── data/                   # Market data, feature store, minute bars
├── results/                # Runtime state JSONs, model artifacts
├── tests/                  # Unit & integration tests
└── venv/                   # Python virtual environment
```

---

## Streams

| Stream | Strategy | Time Window | Frequency | Status |
|--------|----------|-------------|-----------|--------|
| **S1 Edge** | Directional ETF + Gap Trading | 08:00–15:10 KST | Daily | ✅ Active |
| **S2 ML Alpha** | 5-Ensemble ML Stock Selection | 09:00–15:10 KST | Daily | ✅ Active |
| **S3 Factor** | Factor/Sector Rotation | Always | Monthly | ✅ Active |
| **S4 Advisory** | Tax-Advantaged Portfolio Advisory | Always | Quarterly | ✅ Active |

---

## Daily Pipeline (16 Phases)

```
 02:00  WEEKLY_RETRAIN   — 주간 ML 재학습 (토)
 03:00  WEEKLY_VALIDATE  — 주간 검증 (토)
 05:15  OVERNIGHT        — 야간 글로벌 시장 + OIS 계산
 06:00  COLLECT          — 10단계 데이터 수집
 07:45  PREMARKET        — 레짐 판정 + 프리마켓 신호
 08:00  MORNING          — S1 갭 트레이딩 진입
 09:05  MARKET           — StreamOrchestrator 전체 실행
 09:30  INTRADAY         — 장중 모니터링
 15:10  CLOSING          — 포지션 청산 + PnL
 15:35  AFTERMARKET      — Shadow 확정 + Go/No-Go
 16:10  KRX_REFRESH      — KRX 확정 데이터 리프레시
 16:30  COLLECT_FLOW     — 투자자 수급 수집
 17:00  EVENING_DATA     — US 가격 + 저녁 데이터
 19:00  COLLECT_DART     — DART 공시 수집
 20:00  EVENING          — 자가학습 + 리포트
 22:35  US_MARKET        — 미국 시장 데이터
```

---

## Quick Start

### Prerequisites

- Python 3.11+
- macOS (for launchd scheduler)
- KIS API credentials (for live trading)

### Installation

```bash
# Clone
git clone <repo_url> && cd Project_Meridian

# Virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env with your API keys
```

### Running

```bash
# Full pipeline (all 16 phases)
python scripts/daily_pipeline.py

# Single phase
python scripts/daily_pipeline.py overnight
python scripts/daily_pipeline.py morning
python scripts/daily_pipeline.py market

# Multiple phases
python scripts/daily_pipeline.py overnight,collect,morning

# Stream Orchestrator (shadow mode)
python -c "
from scripts.stream_orchestrator import StreamOrchestrator
orch = StreamOrchestrator(exec_mode='shadow')
result = orch.run()
print(result['status'], result['regime'], len(result['orders']), 'orders')
"
```

### Scheduler (macOS launchd)

```bash
# Install all scheduled jobs
./scripts/launchd_install.sh install

# Check status
./scripts/launchd_install.sh status

# Uninstall
./scripts/launchd_install.sh uninstall
```

---

## Configuration

All parameters are managed by `DynamicConfig` (single source of truth):

```python
from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()

# Read
capital = cfg.get('portfolio.initial_capital')  # 150_000_000

# Override at runtime
cfg.set('a3.min_up_probability', 0.65)

# Persist overrides
cfg.save_overrides()  # → config/dynamic_overrides.json
```

Reference: [`config/meridian_config.yaml`](config/meridian_config.yaml)

---

## Risk Management (6-Layer Defense)

```
Kill Switch ──→ Crash Defense ──→ Drawdown Guard
     │                │                │
     ▼                ▼                ▼
position_scale × 0.5  × 0.2         × (0.2~0.7)
     │                │                │
     ▼                ▼                ▼
Exposure Orch ──→ Realtime VaR ──→ Medallion
     │                │                │
     ▼                ▼                ▼
 min(target)       × 0.7           BLOCK if FAIL
```

Worst-case (crash + DD + VaR): scale = **0.014** → effectively blocked ✅

---

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# With coverage
python -m pytest tests/ --cov=src --cov=scripts --cov-report=term-missing

# Specific test
python -m pytest tests/test_regime_detector.py -v
```

---

## Project Phases

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | Architecture Design | ✅ Complete |
| Phase 2 | Infrastructure Build | ✅ Complete |
| Phase 3 | Shadow Trading (14+ days) | 🔄 In Progress |
| Phase 4 | Paper Trading (30+ days) | ⏳ Pending |
| Phase 5 | Live Trading (10% → 30% → 100%) | ⏳ Pending |

---

## License

Private — All rights reserved.

# Project Meridian — Autonomous Quant Firm Architecture (Firm-as-Code)

본 문서는 Project Meridian의 장기적 마이크로서비스 및 멀티에이전트 조직화(Firm-as-Code) 미래 청사진을 보존하는 건축 설계서입니다.

---

## 🏛️ 부서별 역할 매핑 및 R&R

```
+-----------------------------------------------------------------------------------+
|                        6. 이사회 / 투자총괄 (Investment Board)                     |
|           • StreamOrchestrator  • RegimeDetector  • DynamicConfig                  |
+-----------------------------------------------------------------------------------+
                                         |
    +------------------------------------+------------------------------------+
    |                                    |                                    |
+---|--------------------------------+ +---|--------------------------------+ |
| 1. 리서치 / 알파개발본부             | | 2. 포트폴리오 전략본부              | |
| • S0~S12 Alpha Streams             | | • NettingEngine (수수료 상쇄)       | |
| • GeneticFeatureGenerator          | | • QPortfolioOptimizer (QP 배분)     | |
| • AlphaMiner / AutoML              | | • AlphaAllocator (리스크 패리티)     | |
+------------------------------------+ +------------------------------------+ |
                                                                              |
    +------------------------------------+------------------------------------+
    |                                    |
+---|--------------------------------+ +---|--------------------------------+
| 3. 리스크 관리본부 (CRO Office)     | | 4. 트레이딩 & 체결본부               |
| • CROFinalGate (0.1s Pre-Exec Veto)| | • ExecutionEngine (KIS API / SOR)  |
| • RealtimeVaR / KillSwitch         | | • YieldParking (S_YIELD / KOFR)   |
| • MedallionOrchestrator            | | • SlippageMinimizer                |
+------------------------------------+ +------------------------------------+
                                         |
+----------------------------------------v------------------------------------------+
|                     5. 감사 & 자가학습본부 (Audit & AI Dept)                       |
|           • ConflictAuditLedger  • SelfLearning Engine  • AlphaIncubator           |
+-----------------------------------------------------------------------------------+
```

---

## 📋 부서별 세부 모듈 매핑 표

| 부서 | 코드 모듈 | 핵심 역할 및 기능 |
| :--- | :--- | :--- |
| **1. 리서치/알파개발본부** | `S0~S12 Streams`, `GeneticFeatureGenerator`, `AlphaMiner` | 알파 신호 발행, 팩터 채굴, 데이터 가공 |
| **2. 포트폴리오 전략본부** | `NettingEngine`, `QPortfolioOptimizer`, `AlphaAllocator` | 다중 신호 상쇄(Netting), 60일 공분산 기반 QP 배분 |
| **3. 리스크관리본부 (CRO)** | `CROFinalGate`, `RealtimeVaR`, `KillSwitch`, `CrashDefense` | 체결 0.1초 전 99% VaR 비례 축소 (Vector Projection Scaling) 및 Veto |
| **4. 트레이딩&체결본부** | `ExecutionEngine`, `KISAdapter`, `YieldParking` | KIS 실시간 주문 전송, KOFR 무위험 자산 자동 파킹 |
| **5. 감사&자가학습본부** | `ConflictAuditLedger`, `SelfLearning`, `AlphaIncubator` | 텔레메트리 전 과정 정형 감사 로깅, 장 마감 후 자가진화 |
| **6. 이사회/투자총괄** | `StreamOrchestrator`, `RegimeDetector` | 전사 시장 레짐 판정 및 가버넌스 총괄 |

---

## 📡 미래 마이크로서비스 전환 트래킹 메커니즘

- **통신 데이터 구조**: [`src/orchestration/intent_schema.py`](file:///Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian/src/orchestration/intent_schema.py) (`SignalIntent` & UUID5 Idempotency Key)
- **감사 장부 파일**: `results/conflict_audit_ledger.json`
- **인터랙티브 대시보드 위젯 아티팩트**: [`company_dept_architecture.html`](file:///Users/sunghohong/.gemini/antigravity/brain/4e5fd954-247d-4f96-ab53-d8a78ddbd1dc/company_dept_architecture.html)

---

## 🎯 마이크로서비스 전환 분기점 (Triggers)
1. 운용 자산(AUM) **₩5억 원 이상** 달성 시
2. 실시간 감시 종목 **500개 이상** 및 고빈도(Sub-second) 처리 필요 시
3. 개발자/퀀트 연구원 다수 조직 확장 시

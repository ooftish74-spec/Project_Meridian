# 실전 투자(Live Trading) 최종 정밀 진단 및 준비 계획서

본 계획서는 Meridian 시스템이 실제 자본을 투입하여 실시간(Live Trading)으로 수익을 극대화할 수 있는지, 위기 관리와 파이프라인의 안정성이 보장되었는지 6가지 핵심 영역에서 정밀 진단하고 마지막 보완 사항(동적 설정 전환 등)을 처리하기 위한 최종 이행 계획입니다.

## 1. 정밀 진단 (Precision Diagnosis) 결과

### 1.1 투자 전략 및 수익 극대화 준비 상태 (Profit Maximization)
- **S1/S2 (ML Alpha)**: 최신 트리 기반 앙상블(XGBoost, LightGBM 등)이 매일 밤 AutoML을 통해 재학습되며, 최적화된 Confidence(확신도)를 바탕으로 상위 종목을 선정합니다. `stream_metrics.json` 기준 S2는 이미 가상 투자에서 우수한 승률과 수익률을 입증했습니다.
- **S3 (Active Macro / QVM)**: 가치(Value), 우량(Quality), 모멘텀(Momentum)을 혼합 스코어링하여 저평가 우량주(SK하이닉스, 삼성전자 등)를 지속 발굴하는 메커니즘이 완성되어 포트폴리오의 안정적 척추 역할을 수행할 준비가 되었습니다.
- **S4 (Advisory)**: 텔레그램 스크래핑을 통한 인간의 직관적/정보적 엣지를 퀀트 파이프라인에 체계적으로 융합하여 단기 모멘텀 수익을 극대화할 수 있습니다.
- **진단 결론**: 4개의 다변화된 스트림이 서로 상관관계(Correlation)를 낮추어 포트폴리오의 **효율적 경계(Efficient Frontier)** 를 달성할 수학적 준비가 완료되었습니다.

### 1.2 위기 관리 및 리스크 통제 (Risk Management)
- **Crash Defense**: 외인/기관 순매수 수급 이탈, VIX 급등, 시장 급락 시 자동으로 `max_exposure`를 줄이거나 포지션을 전량 청산하는 `crash_defense.py` 모듈이 장착되어 있습니다.
- **Portfolio VaR**: `realtime_var.py`를 통해 최대 예상 손실액을 일일 단위로 모니터링합니다.
- **진단 결론**: 개별 종목의 손절(SL)/익절(TP) 뿐만 아니라, 포트폴리오 전체의 Drawdown을 방어하는 다중 안전망이 확보되어 라이브 전환 시 자본 방어가 충분히 가능합니다.

### 1.3 파이프라인 연결 및 무인 자동화 (Automated Pipeline)
- 데이터 수집 → ML 재학습 → 4-Stream 시그널 발굴 → 자산 배분(Allocator) → 브로커(KIS API) 주문 실행으로 이어지는 `daily_pipeline.py` 가 원활하게 구축되어 있습니다.
- API 단기 에러에 대한 재시도 로직과, 장애 시 에러를 기록하고 다음 틱으로 넘기는 비동기적 회복성(Resilience)이 검증되었습니다.
- **진단 결론**: 100% 무인화된 일일 스케줄러(launchd/cron) 구동에 무리가 없습니다.

### 1.4 대시보드 실시간 무결성 (Dashboard SSoT)
- 대시보드는 로컬/임시 변수를 캐싱하지 않고, 오직 `MeasurementEngine`과 `ShadowPortfolioManager`가 기록하는 JSON SSoT 파일만을 읽어 렌더링하도록 강제되어 있습니다.
- "시간여행" 버그 수정을 통해 입증되었듯, 과거 체결 기록만 있으면 언제든 지표를 100% 오차 없이 실시간으로 재건할 수 있는 강력한 무결성을 지닙니다.

---

## 2. 최종 보완 이행 계획 (Proposed Changes)

현재 시스템 코드베이스에 일부 **하드코딩(Hardcoded)** 된 초기 자본금과 평가 기준일들이 존재합니다. 라이브 환경에서는 투자금이 수시로 변동되거나 추가 납입될 수 있으므로 이를 모두 `DynamicConfig` 기반 동적 조정으로 전환합니다.

### 2.1 하드코딩 값의 동적 설정(Dynamic Config) 전환

#### [MODIFY] `dashboard/app.py`
- `154_000_000` (1.54억) 원으로 하드코딩된 자본금 변수들을 `DynamicConfig().get('portfolio.initial_capital')` 및 실제 `shadow_portfolio.json`의 실시간 Cash/NAV 데이터로 완전 연동.

#### [MODIFY] `scripts/go_nogo.py` & `scripts/rebuild_stream_metrics.py`
- 추적 시작일(`2026-05-26`)과 판정일(`2026-06-09`)이 문자열로 박혀있는 로직 제거.
- `trade_history`의 첫 거래일 또는 `DynamicConfig().get('gonogo.tracking_start_date')`를 읽어오도록 동적 시간 계산 로직으로 교체.

#### [MODIFY] `src/execution/_kis_adapter.py`
- 모의 투자/실전 투자를 구분할 때 임시로 할당하던 `100_000_000` (1억) 초기화 로직을 `DynamicConfig` 및 현재 KIS 계좌의 실제 잔고 조회 연동으로 업데이트.

### 2.2 실전 가동(Live Trading)을 위한 안전 점검

- **Brokerage API 인증 토큰 유효성**: 매일 장전 자동으로 갱신되는지 최종 확인.
- **Execution Router**: 슬리피지 모델(`slippage_model.py`)이 실제 호가창의 얇음/두꺼움을 제대로 추정하여 시장가(Market)가 아닌 지정가(Limit/TWAP)로 유리하게 분할 매수/매도 하도록 세팅.

---

## User Review Required

> [!IMPORTANT]  
> 사용자님, 퀀트 시스템은 본질적으로 **'최적화된 알파 예측'**과 **'냉정한 손실 차단'**의 결합입니다. S1~S4 엔진은 현재 과거 백테스트 상 시장(KOSPI) 대비 압도적인 초과수익(Alpha)을 보였으나, **라이브 환경에서는 필연적으로 슬리피지와 미체결, 그리고 ML 모델의 과최적화(Overfitting) 파훼**가 발생할 수 있습니다. 
> 시스템은 이를 방어하기 위해 초기 진입 비중을 통제하고 있으며, Go/No-Go 엔진이 이미 긍정적 시그널(`SYSTEM GO`)을 보냈습니다.
> 
> 제시된 동적 설정(하드코딩 제거) 작업을 완료한 직후, 곧바로 **실계좌 자동매매(Live Trading)** 파이프라인으로 전환할 스위치를 올릴 준비가 되셨습니까? 승인해 주시면 즉시 리팩토링 및 라이브 파이프라인 최종 패치를 시작하겠습니다.

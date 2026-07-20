#!/usr/bin/env python3
"""[Phase 54: Alt-Data Safeguards] 대안 데이터 무결성 검증 스키마.

Pydantic v2 기반 스키마를 사용하여 Alt-Data 피처의 타입과 기본값을
강제 검증한다. 비정상 업로드를 탐지하고 모델에 가는 데이터 오염을 사전 차단한다.
"""
from __future__ import annotations

from typing import Any

try:
    from pydantic import BaseModel, Field, field_validator
    _PYDANTIC_AVAILABLE = True
except ImportError as e:
    # pydantic 미설치 시 fallback: dict pass-through
    _PYDANTIC_AVAILABLE = False

    class BaseModel:  # type: ignore
        """Pydantic 미설치 시 stub."""
        def __init__(self, **data: Any):
            self.__dict__.update(data)

        def model_dump(self) -> dict:
            return {k: v for k, v in self.__dict__.items()}

    def Field(default: Any = None, **_: Any):
        return default

    def field_validator(*_args: Any, **_kwargs: Any):  # type: ignore
        def decorator(fn: Any) -> Any:
            return fn
        return decorator


class AltDataSchema(BaseModel):
    """[Phase 54: Alt-Data Safeguards] 대안 데이터 무결성 검증 스키마.

    핵심 피처(social_naver_avg, social_reddit_avg)는 명시적으로 선언하여
    float 타입 강제와 기본값 0.0 주입을 보장한다.
    나머지 피체는 extra='allow'로 통과시켜 검증 범위를 한정한다.
    """

    # 핵심 피처: 빈 값 또는 NaN이 오면 0.0 기본값으로 강제 복구
    social_naver_avg: float = Field(default=0.0, description='네이버 뉴스 감성 평균 (중립=0.0)')
    social_reddit_avg: float = Field(default=0.0, description='레딧 감성 평균 (중립=0.0)')

    if _PYDANTIC_AVAILABLE:
        model_config = {'extra': 'allow', 'arbitrary_types_allowed': True}

    if _PYDANTIC_AVAILABLE:
        @field_validator('social_naver_avg', 'social_reddit_avg', mode='before')
        @classmethod
        def validate_sentiment(cls, v: Any) -> float:
            """None, NaN, 문자열을 0.0으로 정규화."""
            import math
            if v is None:
                return 0.0
            try:
                fv = float(v)
                return 0.0 if math.isnan(fv) or math.isinf(fv) else fv
            except (TypeError, ValueError):
                return 0.0

    def model_dump(self) -> dict:  # type: ignore[override]
        """Pydantic v1/v2 호환 지원."""
        if _PYDANTIC_AVAILABLE:
            try:
                return super().model_dump()  # pydantic v2
            except AttributeError:
                return super().dict()  # pydantic v1
        return self.__dict__.copy()

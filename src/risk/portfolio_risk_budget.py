"""
Portfolio Risk Budget — Medallion-Grade 리스크 예산 관리
========================================================
포트폴리오 전체의 리스크 예산을 체계적으로 관리하는 모듈.

기능:
  ① Daily Loss Budget — 일일 손실 한도 (NAV × daily_loss_pct)
  ② Stream Budget     — 스트림별 노출 한도 (max_stream_exposure_pct)
  ③ Consecutive Loss  — 연속 손실 브레이크 (자동 디레버리지)
  ④ Vol Targeting     — 실현 변동성 기반 포지션 스케일링
  ⑤ Overnight VaR     — 야간 VaR + 헤지 제안 (레짐별 배수)
  ⑥ Position Scale    — 모든 체크 통합 → 최종 포지션 승수 (0.0~2.0)

모든 임계값은 DynamicConfig에서 로드. 하드코딩 Zero.

Usage:
    from src.risk.portfolio_risk_budget import PortfolioRiskBudget
    rb = PortfolioRiskBudget()
    daily = rb.compute_daily_budget(portfolio_data)
    scale = rb.get_position_scale(portfolio_data, regime='bull')

State Persistence:
    results/risk_budget_state.json — 일일 상태 자동 저장/복원
"""
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_STATE_FILE = _RESULTS / 'risk_budget_state.json'

class PortfolioRiskBudget:
    """Medallion-Grade 포트폴리오 리스크 예산 관리.

    모든 파라미터는 DynamicConfig에서 동적 로드.
    상태는 results/risk_budget_state.json에 자동 영속화.

    Attributes:
        cfg: DynamicConfig 인스턴스
        state: 영속 상태 (마지막 예산 판정, 연속 손실 카운트 등)
    """

    def __init__(self) -> None:
        """DynamicConfig 로드 + 기존 상태 복원."""
        self.cfg = DynamicConfig()
        self.state: Dict[str, Any] = {}
        self.load_state()
        logger.info('PortfolioRiskBudget initialized (state: %d keys)', len(self.state))

    def compute_daily_budget(self, portfolio_data: dict) -> dict:
        """일일 손실 한도 계산 및 초과 여부 판정.

        daily_loss_limit = NAV × cfg('risk_budget.daily_loss_pct')
        오늘의 실현 PnL이 한도를 초과하면 거래 중단 신호 반환.

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
                - nav: 현재 순자산가치 (float)
                - realized_pnl_today: 오늘 실현 손익 (float)

        Returns:
            {
                'daily_loss_limit': float,  # 일일 손실 한도 (음수)
                'realized_pnl': float,      # 오늘 실현 PnL
                'utilization_pct': float,   # 예산 소진율 (%)
                'halted': bool,             # 거래 중단 여부
                'reason': str | None,       # 중단 사유
            }
        """
        nav = self._safe_nav(portfolio_data)
        daily_loss_pct = self.cfg.get('risk_budget.daily_loss_pct', 0.005)
        realized_pnl = portfolio_data.get('realized_pnl_today', 0.0)
        if realized_pnl is None or (isinstance(realized_pnl, float) and math.isnan(realized_pnl)):
            realized_pnl = 0.0
        daily_loss_limit = -(nav * daily_loss_pct)
        if abs(daily_loss_limit) > 1e-10:
            utilization_pct = min(realized_pnl, 0.0) / daily_loss_limit * 100.0
        else:
            utilization_pct = 0.0
        _tol = max(abs(daily_loss_limit) * 1e-09, 1.0)
        halted = realized_pnl <= daily_loss_limit + _tol
        reason = 'daily_loss_limit' if halted else None
        if halted:
            logger.warning('🚨 Daily loss limit breached: PnL=%.0f ≤ limit=%.0f (NAV=%.0f)', realized_pnl, daily_loss_limit, nav)
        result = {'daily_loss_limit': round(daily_loss_limit, 2), 'realized_pnl': round(realized_pnl, 2), 'utilization_pct': round(utilization_pct, 2), 'halted': halted, 'reason': reason, 'timestamp': datetime.now().isoformat()}
        self.state['last_daily_budget'] = result
        return result

    def compute_stream_budget(self, portfolio_data: dict) -> dict:
        """스트림별 노출 비중 계산 및 한도 초과 검사.

        각 스트림의 current_exposure / NAV가 max_stream_exposure_pct를 초과하면
        over_limit 플래그를 세팅.

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
                - nav: 현재 NAV
                - streams: {stream_id: {'exposure': float, ...}, ...}

        Returns:
            {
                'max_stream_pct': float,       # 스트림별 최대 허용 비중
                'streams': {
                    stream_id: {
                        'current_exposure': float,
                        'current_pct': float,
                        'max_pct': float,
                        'over_limit': bool,
                        'excess_pct': float,
                    }
                },
                'any_over_limit': bool,        # 한도 초과 스트림 존재 여부
                'n_over_limit': int,           # 한도 초과 스트림 수
            }
        """
        nav = self._safe_nav(portfolio_data)
        max_stream_pct = self.cfg.get('risk_budget.max_stream_exposure_pct', 0.25)
        streams_data = portfolio_data.get('streams', {})
        stream_results: Dict[str, Dict] = {}
        n_over = 0
        for stream_id, stream_info in streams_data.items():
            exposure = stream_info.get('exposure', 0.0)
            if exposure is None or (isinstance(exposure, float) and math.isnan(exposure)):
                exposure = 0.0
            current_pct = exposure / nav if nav > 1e-10 else 0.0
            over_limit = current_pct > max_stream_pct
            excess_pct = max(0.0, current_pct - max_stream_pct)
            if over_limit:
                n_over += 1
                logger.warning('⚠️ Stream %s over limit: %.1f%% > %.1f%%', stream_id, current_pct * 100, max_stream_pct * 100)
            stream_results[stream_id] = {'current_exposure': round(exposure, 2), 'current_pct': round(current_pct, 4), 'max_pct': max_stream_pct, 'over_limit': over_limit, 'excess_pct': round(excess_pct, 4)}
        result = {'max_stream_pct': max_stream_pct, 'streams': stream_results, 'any_over_limit': n_over > 0, 'n_over_limit': n_over, 'timestamp': datetime.now().isoformat()}
        self.state['last_stream_budget'] = result
        return result

    def check_consecutive_loss_brake(self, portfolio_data: dict) -> dict:
        """연속 손실일 기반 자동 디레버리지 판정.

        최근 N일(lookback) 동안 연속 음수 수익률이면 포지션 규모를
        brake_scale로 축소. 연속 손실 카운트가 lookback 이상이면 트리거.

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
                - daily_records: [{date, pnl, return_pct}, ...]
                  최신 기록이 리스트 마지막에 위치

        Returns:
            {
                'braked': bool,              # 브레이크 발동 여부
                'scale': float,              # 적용할 포지션 스케일 (1.0 = 정상)
                'consecutive_losses': int,   # 현재 연속 손실일 수
                'lookback': int,             # 판정 기준 연속 손실일
                'brake_scale': float,        # 트리거 시 적용 스케일
            }
        """
        lookback = self.cfg.get('risk_budget.consecutive_loss_days', 3)
        brake_scale = self.cfg.get('risk_budget.brake_scale', 0.5)
        daily_records = portfolio_data.get('daily_records', [])
        consecutive_losses = 0
        for record in reversed(daily_records):
            pnl = record.get('pnl', record.get('return_pct', 0.0))
            if pnl is None or (isinstance(pnl, float) and math.isnan(pnl)):
                break
            if pnl < 0:
                consecutive_losses += 1
            else:
                break
        braked = consecutive_losses >= lookback
        scale = brake_scale if braked else 1.0
        if braked:
            logger.warning('🛑 Consecutive loss brake: %d losses ≥ %d → scale=%.2f', consecutive_losses, lookback, scale)
        result = {'braked': braked, 'scale': scale, 'consecutive_losses': consecutive_losses, 'lookback': lookback, 'brake_scale': brake_scale, 'timestamp': datetime.now().isoformat()}
        self.state['last_consecutive_loss'] = result
        return result

    def compute_volatility_target(self, portfolio_data: dict) -> dict:
        """실현 변동성 대비 목표 변동성 비율로 포지션 스케일 산출.

        vol_ratio = target_annual_vol / max(realized_vol, 0.01)
        scale = clamp(vol_ratio, min_vol_scale, max_vol_scale)

        실현 변동성이 목표보다 높으면 축소, 낮으면 확대.
        최소 10일 데이터가 필요하며, 부족 시 scale=1.0 반환.

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
                - daily_returns: 일별 수익률 리스트 (소수, e.g. 0.01 = 1%)

        Returns:
            {
                'target_vol': float,         # 목표 연간 변동성
                'realized_vol': float,       # 실현 연간 변동성
                'vol_ratio': float,          # target / realized
                'scale': float,              # 최종 스케일 (clamped)
                'data_sufficient': bool,     # 데이터 충분 여부
                'n_days': int,               # 사용된 데이터 일수
            }
        """
        target_vol = self.cfg.get('risk_budget.target_annual_vol', 0.15)
        min_vol_scale = self.cfg.get('risk_budget.min_vol_scale', 0.3)
        max_vol_scale = self.cfg.get('risk_budget.max_vol_scale', 2.0)
        daily_returns = portfolio_data.get('daily_returns', [])
        clean_returns = [r for r in daily_returns if r is not None and isinstance(r, (int, float)) and (not math.isnan(r))]
        n_days = len(clean_returns)
        if n_days < 10:
            logger.debug('Vol targeting: insufficient data (%d < 10), returning scale=1.0', n_days)
            return {'target_vol': target_vol, 'realized_vol': 0.0, 'vol_ratio': 1.0, 'scale': 1.0, 'data_sufficient': False, 'n_days': n_days, 'timestamp': datetime.now().isoformat()}
        mean_ret = sum(clean_returns) / n_days
        variance = sum(((r - mean_ret) ** 2 for r in clean_returns)) / max(n_days - 1, 1)
        daily_vol = math.sqrt(variance)
        realized_vol = daily_vol * math.sqrt(252)
        vol_ratio = target_vol / max(realized_vol, 0.01)
        scale = max(min_vol_scale, min(vol_ratio, max_vol_scale))
        logger.info('Vol targeting: realized=%.2f%% target=%.2f%% ratio=%.2f scale=%.2f', realized_vol * 100, target_vol * 100, vol_ratio, scale)
        result = {'target_vol': round(target_vol, 4), 'realized_vol': round(realized_vol, 4), 'vol_ratio': round(vol_ratio, 4), 'scale': round(scale, 4), 'data_sufficient': True, 'n_days': n_days, 'timestamp': datetime.now().isoformat()}
        self.state['last_vol_target'] = result
        return result

    def compute_overnight_var(self, portfolio_data: dict, regime: str) -> dict:
        """야간(Overnight) VaR 계산 및 헤지 필요량 산출.

        레짐별 VaR 배수를 적용하여 장마감 후 보유 리스크를 평가.
        야간 VaR이 NAV 대비 임계값을 초과하면 헤지 비중을 제안.

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
                - nav: 현재 NAV
                - total_exposure: 총 포지션 노출 금액
            regime: 현재 시장 레짐 ('bull', 'caution', 'bear', 'crash')

        Returns:
            {
                'var_amount': float,           # VaR 금액 (원)
                'var_pct': float,              # NAV 대비 VaR 비율 (%)
                'hedge_needed': bool,          # 헤지 필요 여부
                'suggested_hedge_pct': float,  # 제안 헤지 비중 (0.0~1.0)
                'regime': str,                 # 적용 레짐
                'regime_mult': float,          # 레짐별 VaR 배수
            }
        """
        nav = self._safe_nav(portfolio_data)
        total_exposure = portfolio_data.get('total_exposure', nav)
        if total_exposure is None or (isinstance(total_exposure, float) and math.isnan(total_exposure)):
            total_exposure = nav
        regime_multipliers = {'bull': self.cfg.get('risk_budget.var_bull', 1.0), 'caution': self.cfg.get('risk_budget.var_caution', 1.5), 'bear': self.cfg.get('risk_budget.var_bear', 2.0), 'crash': self.cfg.get('risk_budget.var_crash', 3.0)}
        regime_safe = regime if regime in regime_multipliers else 'caution'
        regime_mult = regime_multipliers[regime_safe]
        base_var_pct = self.cfg.get('risk_budget.base_var_pct', 0.02)
        var_amount = total_exposure * regime_mult * base_var_pct
        var_pct = var_amount / nav * 100 if nav > 1e-10 else 0.0
        overnight_var_limit_pct = self.cfg.get('risk_budget.overnight_var_limit_pct', 3.0)
        hedge_needed = var_pct > overnight_var_limit_pct
        if hedge_needed and var_pct > 0:
            excess_ratio = (var_pct - overnight_var_limit_pct) / var_pct
            suggested_hedge_pct = min(max(excess_ratio, 0.0), 1.0)
        else:
            suggested_hedge_pct = 0.0
        if hedge_needed:
            logger.warning('🌙 Overnight VaR alert: %.0f원 (%.1f%% of NAV) > %.1f%% limit [%s]', var_amount, var_pct, overnight_var_limit_pct, regime_safe)
        result = {'var_amount': round(var_amount, 2), 'var_pct': round(var_pct, 4), 'hedge_needed': hedge_needed, 'suggested_hedge_pct': round(suggested_hedge_pct, 4), 'regime': regime_safe, 'regime_mult': regime_mult, 'base_var_pct': base_var_pct, 'overnight_var_limit_pct': overnight_var_limit_pct, 'timestamp': datetime.now().isoformat()}
        self.state['last_overnight_var'] = result
        return result

    def get_position_scale(self, portfolio_data: dict, regime: str) -> float:
        """모든 리스크 체크를 통합하여 최종 포지션 스케일 반환.

        통합 스케일 = min(daily_scale, loss_brake_scale, vol_scale)
        결과는 0.0 ~ 2.0 범위로 클램프.

        판정 순서:
          1. daily_budget → 한도 초과 시 0.0
          2. consecutive_loss_brake → 트리거 시 brake_scale
          3. vol_target → 변동성 기반 스케일

        Args:
            portfolio_data: 포트폴리오 상태 딕셔너리
            regime: 현재 시장 레짐

        Returns:
            float: 최종 포지션 스케일 (0.0 ~ 2.0)
        """
        daily = self.compute_daily_budget(portfolio_data)
        if daily['halted']:
            logger.warning('Position scale → 0.0 (daily loss limit halted)')
            self._update_scale_state(0.0, 'daily_halt', regime)
            return 0.0
        daily_scale = 1.0
        utilization = daily.get('utilization_pct', 0.0)
        if utilization >= 80.0:
            daily_scale = max(0.0, (100.0 - utilization) / 20.0)
        loss_brake = self.check_consecutive_loss_brake(portfolio_data)
        brake_scale = loss_brake['scale']
        vol_result = self.compute_volatility_target(portfolio_data)
        vol_scale = vol_result['scale']
        _brake_active = bool(loss_brake.get('braked', False))
        _effective_vol_scale = min(vol_scale, 1.0) if _brake_active else vol_scale
        combined_scale = min(daily_scale, brake_scale) * _effective_vol_scale
        final_scale = max(0.0, min(combined_scale, 2.0))
        logger.info('Position scale: daily=%.2f brake=%.2f vol=%.2f → combined=%.2f → final=%.2f [%s]', daily_scale, brake_scale, vol_scale, combined_scale, final_scale, regime)
        self._update_scale_state(final_scale, 'normal', regime)
        return round(final_scale, 4)

    def save_state(self) -> None:
        """현재 상태를 results/risk_budget_state.json에 저장.

        results/ 디렉토리가 없으면 자동 생성.
        직렬화 불가 객체는 문자열로 변환.
        """
        try:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            self.state['last_saved'] = datetime.now().isoformat()
            serialized = json.dumps(self.state, indent=2, default=str)
            _STATE_FILE.write_text(serialized)
            logger.debug('Risk budget state saved (%d bytes)', len(serialized))
        except Exception as e:
            logger.critical('Failed to save risk budget state: %s', e, exc_info=True)

    def load_state(self) -> None:
        """results/risk_budget_state.json에서 상태 복원.

        파일이 없거나 손상되면 빈 상태로 초기화.
        """
        try:
            if _STATE_FILE.exists():
                raw = _STATE_FILE.read_text()
                if raw.strip():
                    self.state = json.loads(raw)
                    logger.debug('Risk budget state loaded (%d keys)', len(self.state))
                else:
                    self.state = {}
                    logger.debug('Risk budget state file empty, starting fresh')
            else:
                self.state = {}
                logger.debug('No risk budget state file, starting fresh')
        except (json.JSONDecodeError, OSError) as e:
            logger.critical('Failed to load risk budget state: %s — starting fresh', e, exc_info=True)
            self.state = {}

    def _safe_nav(self, portfolio_data: dict) -> float:
        """NAV를 안전하게 추출. 유효하지 않으면 cfg 기본값 사용.

        Edge cases:
          - nav 키 없음 → initial_capital 폴백
          - nav == 0 또는 NaN → initial_capital 폴백
          - initial_capital도 없음 → DynamicConfig 기본값
        """
        nav = portfolio_data.get('nav')
        if nav is None or (isinstance(nav, float) and math.isnan(nav)) or nav <= 0:
            nav = portfolio_data.get('initial_capital', self.cfg.get('portfolio.initial_capital', 150000000))
        if nav is None or (isinstance(nav, float) and math.isnan(nav)) or nav <= 0:
            nav = self.cfg.get('portfolio.initial_capital', 150000000)
        try:
            return float(nav)
        except (TypeError, ValueError) as _e:
            logger.critical(f'nav float 변환 실패: {nav!r} ({_e}) → initial_capital 사용', exc_info=True)
            return float(self.cfg.get('portfolio.initial_capital', 150000000))

    def _update_scale_state(self, scale: float, reason: str, regime: str) -> None:
        """포지션 스케일 판정 결과를 상태에 기록 후 자동 저장."""
        self.state['last_position_scale'] = {'scale': scale, 'reason': reason, 'regime': regime, 'timestamp': datetime.now().isoformat()}
        self.save_state()
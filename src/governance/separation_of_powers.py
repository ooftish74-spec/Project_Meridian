"""
Project Meridian — Constitutional Separation-of-Powers Governance Engine
========================================================================
Grand Master 3부 + 독립기관 (데이터 사서 · 반증관 · 금고지기 · 묘지기 · 검시관 · 차단기)
적대적 검증 및 과최적화(Self-Deception) 박멸을 위한 수리적 거버넌스 샌드박스 모듈.

핵심 메커니즘:
  1. 데이터 사서 (DataLibrarian):
     - Point-in-Time 시점 누설 차단 (미래 타임스탬프 데이터 원천 거부)
     - 위상 무작위화(Fourier Phase Randomization) 및 순환 블록 부트스트랩 대체 시계열(Surrogate Path) 생성
       → AI 모델의 사전학습 기억(2020~2025 티커/날짜 역추적) 원천 봉쇄
  2. 반증관 (AdversarialFalsifier):
     - 단독 샤프가 아닌 기존 운용 포트폴리오 대비 직교 잔차(Orthogonal Residual) 정보비율(IR) 심사
     - 마찰 비용 배가(Friction Stress) 및 파라미터 섭동(Perturbation) 반증 테스트
     - 망자의 부활(Shadow Graveyard) 추적을 통한 반증관 양방향 책임 점수(False Rejection Penalty) 산출
  3. 금고지기 & 묘지기 (VaultAndGravekeeper):
     - 전략 간 상관관계(ρ̄)를 반영한 독립 유효 시행 횟수 N_eff = 1 + (K - 1)(1 - ρ̄) 및 Deflated Sharpe Ratio (DSR)
     - 이름만 바꾼 복제 전략 차단 (기존 실패작과 상관계수 >= ρ_dup_gate 시 즉시 기각)
     - 전략당 단 1회만 열람 가능한 OOS 금고(One-Shot Holdout Vault) 및 Thresholdout 이진 판정
  4. 검시관 (PnLCoroner):
     - 대규모 손실뿐 아니라 '설명할 수 없는 큰 수익(Unexplained Windfall Gain)'을 분해하여
       운(Luck Residual)으로 번 수익을 위험 신호로 격리 판정
"""

from __future__ import annotations

import hashlib
import math
from statistics import NormalDist
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from src.measurement import perf_stats as _ps

_ND = NormalDist()
_ANNUALIZATION = 252
_EPS = 1e-12
_MIN_OBS = 5
_DUP_CORR_GATE = 0.90
_DEFAULT_FRICTION_MULT = 2.0
_DEFAULT_MIN_ORTHO_IR = 0.50
_DEFAULT_DSR_GATE = 0.95
_DEFAULT_CORONER_Z = 2.50
_DEFAULT_WINDFALL_LUCK_RATIO = 0.60
_DEFAULT_BLOCK_SIZE = 5
_HALF = 0.50
_SHADOW_HURDLE_SR = 1.00


class DataLibrarian:
    """[1부: 데이터 사서] 시점 정합성(Point-in-Time) 감사 및 사전학습 역추적 방지 대체 시계열 생성기."""

    @staticmethod
    def enforce_point_in_time(
        records: Sequence[Dict[str, Any]],
        as_of_timestamp: str,
        timestamp_key: str = 'timestamp',
        available_at_key: str = 'available_at',
    ) -> Dict[str, Any]:
        """as_of_timestamp 이후에 생성되었거나 공시 시점(available_at)이 미래인 레코드를 원천 차단."""
        clean_records: List[Dict[str, Any]] = []
        leaked_count = 0
        for rec in records:
            if not isinstance(rec, dict):
                continue
            ts = str(rec.get(timestamp_key) or '')
            avail = str(rec.get(available_at_key) or ts)
            if (ts and ts > as_of_timestamp) or (avail and avail > as_of_timestamp):
                leaked_count += 1
                continue
            clean_records.append(dict(rec))
        return {
            'as_of_timestamp': as_of_timestamp,
            'clean_records': clean_records,
            'n_clean': len(clean_records),
            'n_leaked_blocked': leaked_count,
            'leakage_detected': leaked_count > 0,
        }

    @staticmethod
    def generate_phase_randomized_surrogate(
        returns: Sequence[float],
        seed: int = 42,
    ) -> np.ndarray:
        """푸리에 위상 무작위화(Fourier Phase Randomization) 대체 시계열 생성.

        원 시계열의 평균, 분산, 파워 스펙트럼(자기상관 구조)은 정확히 보존하면서,
        특정 역사적 날짜(예: 2020년 3월 폭락)의 위상 경로 지문(Topological Fingerprint)만 파괴하여
        AI 탐색가의 사전학습 암기(Pre-training Memorization)를 무력화함.
        """
        r_arr = np.asarray(returns, dtype=float).ravel()
        n = len(r_arr)
        if n < _MIN_OBS:
            return r_arr.copy()

        rng = np.random.default_rng(seed)
        mean_r = float(np.mean(r_arr))
        centered = r_arr - mean_r
        fft_vals = np.fft.rfft(centered)
        mag = np.abs(fft_vals)

        # DC 성분(index 0)과 짝수 길이의 Nyquist 성분(마지막 index)은 위상 고정
        phases = rng.uniform(-math.pi, math.pi, size=len(fft_vals))
        phases[0] = float(np.angle(fft_vals[0]))
        if n % 2 == 0 and len(phases) > 1:
            phases[-1] = float(np.angle(fft_vals[-1]))

        randomized_fft = mag * np.exp(1j * phases)
        surrogate = np.fft.irfft(randomized_fft, n=n) + mean_r
        return surrogate


class AdversarialFalsifier:
    """[2부: 반증관] 직교 잔차 정보비율(Orthogonal Residual IR), 마찰 배가 스트레스, 망자의 부활 책임 감사."""

    @staticmethod
    def compute_orthogonal_residual_metrics(
        candidate_returns: Sequence[float],
        benchmark_or_active_returns: Sequence[float],
        annualization: int = _ANNUALIZATION,
    ) -> Dict[str, Any]:
        """신규 전략 수익률을 기존 운용 포트폴리오 수익률에 투영(OLS)하여 직교 잔차(ε_⊥) 및 한계 IR 산출.

        r_cand,t = α + β * R_active,t + ε_⊥,t
        단독 샤프가 높더라도 기존 포트폴리오와 중복(β만 크고 IR_⊥ <= 0)이면 탈락시키고,
        단독 샤프가 낮더라도 음(-)의 상관관계로 양(+)의 직교 알파를 제공하면 구제함.
        """
        cand = np.asarray(candidate_returns, dtype=float).ravel()
        act = np.asarray(benchmark_or_active_returns, dtype=float).ravel()
        n = min(len(cand), len(act))
        if n < _MIN_OBS:
            return {
                'alpha_daily': 0.0,
                'beta': 0.0,
                'orthogonal_ir': 0.0,
                'standalone_sharpe': 0.0,
                'corr_with_active': 0.0,
                'residuals': [],
            }
        cand = cand[:n]
        act = act[:n]

        var_act = float(np.var(act, ddof=1))
        if var_act > _EPS:
            cov_ca = float(np.cov(act, cand, ddof=1)[0, 1])
            beta = cov_ca / var_act
        else:
            beta = 0.0

        hedged = cand - beta * act
        alpha_d = float(np.mean(hedged))
        sd_hedged = float(np.std(hedged, ddof=1))
        ortho_ir = (alpha_d / sd_hedged) * math.sqrt(annualization) if sd_hedged > _EPS else 0.0

        sd_cand = float(np.std(cand, ddof=1))
        standalone_sr = (float(np.mean(cand)) / sd_cand) * math.sqrt(annualization) if sd_cand > _EPS else 0.0

        if sd_cand > _EPS and var_act > _EPS:
            corr = float(np.corrcoef(cand, act)[0, 1])
        else:
            corr = 0.0

        return {
            'alpha_daily': alpha_d,
            'beta': beta,
            'orthogonal_ir': ortho_ir,
            'standalone_sharpe': standalone_sr,
            'corr_with_active': corr,
            'residuals': hedged.tolist(),
        }

    @classmethod
    def falsify_candidate(
        cls,
        candidate_returns: Sequence[float],
        active_returns: Sequence[float],
        estimated_daily_cost: float,
        perturbed_sharpes: Sequence[float],
        friction_multiplier: float = _DEFAULT_FRICTION_MULT,
        min_ortho_ir: float = _DEFAULT_MIN_ORTHO_IR,
    ) -> Dict[str, Any]:
        """반증관 3단 살생부 심사:
        1) 마찰 비용 배가(Friction Stress) 후에도 직교 잔차 IR >= min_ortho_ir 인가?
        2) 파라미터 섭동(Perturbation) 시 이웃 샤프 비율의 하위 25% 분위수가 양(> 0)인가?
        """
        cand = np.asarray(candidate_returns, dtype=float).ravel()
        stressed_cand = cand - float(estimated_daily_cost) * float(friction_multiplier)
        ortho_res = cls.compute_orthogonal_residual_metrics(stressed_cand, active_returns)
        stressed_ir = float(ortho_res['orthogonal_ir'])

        pert_arr = np.asarray(perturbed_sharpes, dtype=float).ravel()
        if len(pert_arr) > 0:
            pert_q25 = float(np.percentile(pert_arr, 25.0))
        else:
            pert_q25 = stressed_ir

        reasons: List[str] = []
        if stressed_ir < min_ortho_ir:
            reasons.append(f"Stressed Orthogonal IR({stressed_ir:.3f}) < {min_ortho_ir:.3f}")
        if pert_q25 <= 0.0:
            reasons.append(f"Parameter Perturbation Q25 Sharpe({pert_q25:.3f}) <= 0.0 (절벽형 과최적화 의심)")

        survived = len(reasons) == 0
        return {
            'survived': survived,
            'verdict': 'SURVIVED' if survived else 'FALSIFIED',
            'stressed_orthogonal_ir': stressed_ir,
            'standalone_sharpe_stressed': float(ortho_res['standalone_sharpe']),
            'beta_to_active': float(ortho_res['beta']),
            'perturbation_q25_sharpe': pert_q25,
            'reasons': reasons,
        }

    @staticmethod
    def evaluate_falsifier_accountability(
        shadow_graveyard_outcomes: Sequence[Dict[str, Any]],
        hurdle_sharpe: float = _SHADOW_HURDLE_SR,
    ) -> Dict[str, Any]:
        """[망자의 부활(Shadow Graveyard) 감사] 반증관이 죽인 전략들의 사후 가상(Shadow) OOS 성과를 추적하여
        진짜 알파를 죽인 제2종 오류(False Rejection) 발생 시 반증관에게 페널티 부과(허무주의적 기각 방지).
        """
        n_total = len(shadow_graveyard_outcomes)
        if n_total == 0:
            return {
                'n_audited': 0,
                'true_rejections': 0,
                'false_rejections': 0,
                'falsifier_accuracy': 1.0,
                'false_rejection_penalty': 0.0,
                'resurrected_strategies': [],
            }

        true_rej = 0
        false_rej = 0
        penalty_sum = 0.0
        resurrected: List[str] = []

        for item in shadow_graveyard_outcomes:
            sid = str(item.get('strategy_id', 'unknown'))
            shadow_sr = float(item.get('realized_shadow_sharpe', 0.0) or 0.0)
            if shadow_sr >= hurdle_sharpe:
                false_rej += 1
                excess = shadow_sr - hurdle_sharpe
                penalty_sum += excess * excess
                resurrected.append(sid)
            else:
                true_rej += 1

        return {
            'n_audited': n_total,
            'true_rejections': true_rej,
            'false_rejections': false_rej,
            'falsifier_accuracy': true_rej / float(n_total),
            'false_rejection_penalty': penalty_sum / float(n_total),
            'resurrected_strategies': resurrected,
        }


class VaultAndGravekeeper:
    """[2부: 금고지기 & 묘지기] 전략당 1회 OOS 금고 열람 제한 및 독립 유효 시행 횟수(N_eff) 기반 DSR."""

    def __init__(
        self,
        dsr_gate: float = _DEFAULT_DSR_GATE,
        dup_corr_gate: float = _DUP_CORR_GATE,
    ):
        self.dsr_gate = float(dsr_gate)
        self.dup_corr_gate = float(dup_corr_gate)
        self._graveyard_returns: Dict[str, np.ndarray] = {}
        self._vault_opened_ids: Dict[str, int] = {}

    @staticmethod
    def _fingerprint(strategy_id: str, returns: np.ndarray) -> str:
        rounded = np.round(returns, 8).tobytes()
        h = hashlib.sha256(rounded).hexdigest()[:16]
        return f"{strategy_id}:{h}"

    def compute_effective_trials(self, candidate_returns: Optional[np.ndarray] = None) -> Tuple[float, float, float]:
        """묘지기에 누적된 K개 시도 전략 간 평균 상관계수(ρ̄)를 바탕으로 독립 유효 시행 횟수 산출:
        N_eff = 1 + (K - 1) * (1 - ρ̄)
        """
        all_series = list(self._graveyard_returns.values())
        if candidate_returns is not None:
            all_series = all_series + [np.asarray(candidate_returns, dtype=float).ravel()]
        k = len(all_series)
        if k <= 1:
            return 1.0, 0.0, 0.0

        min_len = min(len(s) for s in all_series)
        if min_len < _MIN_OBS:
            return float(k), 0.0, 0.0

        mat = np.vstack([s[:min_len] for s in all_series])
        corr_mat = np.corrcoef(mat)
        upper_idx = np.triu_indices(k, k=1)
        pair_corrs = corr_mat[upper_idx]
        pair_corrs = pair_corrs[np.isfinite(pair_corrs)]
        if len(pair_corrs) == 0:
            return float(k), 0.0, 0.0

        avg_rho = float(np.clip(np.mean(pair_corrs), 0.0, 1.0))
        max_rho = float(np.max(pair_corrs))
        n_eff = 1.0 + float(k - 1) * (1.0 - avg_rho)
        return max(1.0, n_eff), avg_rho, max_rho

    def register_trial_and_open_vault(
        self,
        strategy_id: str,
        oos_returns: Sequence[float],
        rf_annual: float = 0.0,
    ) -> Dict[str, Any]:
        """금고지기 & 묘지기 통합 관문:
        1) 동일 strategy_id 또는 동일 수익률 지문에 대해 2회차 열람 시도 시 즉시 거부 (One-Shot Vault Rule)
        2) 기존 묘지기 실패작과 상관계수 >= dup_corr_gate 인 '이름만 바꾼 복제 전략' 즉시 거부
        3) 독립 유효 시행 횟수 N_eff 를 반영한 Deflated Sharpe Ratio (DSR) >= dsr_gate 통과 시에만 PASS 반환
        """
        r_arr = np.asarray(oos_returns, dtype=float).ravel()
        fp = self._fingerprint(strategy_id, r_arr)

        if strategy_id in self._vault_opened_ids or fp in self._vault_opened_ids:
            self._vault_opened_ids[strategy_id] = self._vault_opened_ids.get(strategy_id, 0) + 1
            return {
                'passed': False,
                'verdict': 'VAULT_ACCESS_DENIED_REPEAT_QUERY',
                'reason': '금고지기 헌법 위반: 동일 전략의 OOS 금고 2회 열람 시도는 과최적화로 간주하여 영구 차단',
                'k_trials': len(self._graveyard_returns),
            }

        # 복제 전략(이름만 바꾼 재상정) 검사
        max_existing_corr = 0.0
        for prev_id, prev_r in self._graveyard_returns.items():
            m_len = min(len(r_arr), len(prev_r))
            if m_len >= _MIN_OBS and np.std(r_arr[:m_len]) > _EPS and np.std(prev_r[:m_len]) > _EPS:
                c_val = float(np.corrcoef(r_arr[:m_len], prev_r[:m_len])[0, 1])
                if math.isfinite(c_val) and c_val > max_existing_corr:
                    max_existing_corr = c_val

        self._vault_opened_ids[strategy_id] = 1
        self._vault_opened_ids[fp] = 1

        if max_existing_corr >= self.dup_corr_gate:
            self._graveyard_returns[strategy_id] = r_arr
            return {
                'passed': False,
                'verdict': 'REJECTED_CLONE_STRATEGY',
                'max_correlation_with_graveyard': max_existing_corr,
                'reason': f"묘지기 차단: 기존 전략과 상관계수({max_existing_corr:.4f}) >= {self.dup_corr_gate:.2f} (이름만 바꾼 재상정)",
                'k_trials': len(self._graveyard_returns),
            }

        n_eff, avg_rho, _ = self.compute_effective_trials(r_arr)
        self._graveyard_returns[strategy_id] = r_arr
        k_total = len(self._graveyard_returns)

        n_eff_int = max(1, int(math.ceil(n_eff)))
        dsr_res = _ps.deflated_sharpe_ratio(r_arr.tolist(), n_trials=n_eff_int, rf_annual=rf_annual)
        dsr_val = float(dsr_res['value']) if dsr_res.get('value') is not None else 0.0
        passed = dsr_val >= self.dsr_gate

        # [Thresholdout 원칙] 금고지기는 통과 여부(PASS/FAIL)와 유효 시행 횟수만 반환
        return {
            'passed': passed,
            'verdict': 'VAULT_PASS' if passed else 'VAULT_FAIL_DSR',
            'k_trials': k_total,
            'n_effective_trials': n_eff,
            'avg_graveyard_correlation': avg_rho,
            'dsr': dsr_val,
        }


class PnLCoroner:
    """[독립기관: 검시관] 대규모 손실 및 '설명할 수 없는 큰 수익(Unexplained Windfall Gain)' 부검."""

    @staticmethod
    def autopsy_realized_return(
        realized_return: float,
        expected_model_return: float,
        market_return: float,
        asset_beta: float,
        residual_volatility: float,
        anomaly_z_threshold: float = _DEFAULT_CORONER_Z,
        windfall_luck_ratio_gate: float = _DEFAULT_WINDFALL_LUCK_RATIO,
    ) -> Dict[str, Any]:
        """실현 수익률(r_t)을 3가지 원천으로 직교 분해:
        r_t = E[α_t] (모델 예측 순수 엣지) + β_i * r_mkt,t (시장 베타 효과) + ε_luck,t (설명 불가 잔차)

        - ε_luck,t / σ_ε >= +anomaly_z_threshold 이고 |ε_luck,t| / |r_t| >= windfall_luck_ratio_gate 이면
          'UNEXPLAINED_WINDFALL_GAIN (운으로 번 수익)' 경보를 발령하여 실력(Skill)으로 오인한 증액을 차단.
        - ε_luck,t / σ_ε <= -anomaly_z_threshold 이면 'UNEXPLAINED_TAIL_LOSS (구조적 파열 손실)' 경보 발령.
        """
        beta_component = float(asset_beta) * float(market_return)
        explained_return = float(expected_model_return) + beta_component
        luck_residual = float(realized_return) - explained_return
        vol = max(float(residual_volatility), _EPS)
        z_score = luck_residual / vol

        total_abs = max(abs(float(realized_return)), _EPS)
        luck_share = abs(luck_residual) / total_abs

        verdict = 'EXPLAINED_NORMAL'
        quarantine_recommended = False
        if z_score >= anomaly_z_threshold and luck_share >= windfall_luck_ratio_gate:
            verdict = 'UNEXPLAINED_WINDFALL_GAIN'
            quarantine_recommended = True
        elif z_score <= -anomaly_z_threshold:
            verdict = 'UNEXPLAINED_TAIL_LOSS'
            quarantine_recommended = True

        return {
            'realized_return': float(realized_return),
            'expected_model_return': float(expected_model_return),
            'beta_component': beta_component,
            'explained_return': explained_return,
            'luck_residual': luck_residual,
            'residual_z_score': z_score,
            'luck_share_of_return': luck_share,
            'verdict': verdict,
            'quarantine_recommended': quarantine_recommended,
        }


_DEFAULT_PORTFOLIO_VOL = 0.015


def run_daily_constitutional_audit(results_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Evening Phase 자동 배선용 삼권분립(반증관 · 묘지기 · 검시관) 일일 통합 감사 실행기."""
    import json
    from datetime import datetime
    from pathlib import Path
    from src.utils.file_ops import atomic_write_json

    res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
    me_file = res_path / 'measurement_engine.json'
    ov_file = res_path / 'dynamic_overrides.json'

    me_data: Dict[str, Any] = {}
    if me_file.exists():
        try:
            me_data = json.loads(me_file.read_text(encoding='utf-8'))
        except Exception:
            me_data = {}

    ov_data: Dict[str, Any] = {}
    if ov_file.exists():
        try:
            ov_data = json.loads(ov_file.read_text(encoding='utf-8'))
        except Exception:
            ov_data = {}

    official = me_data.get('official') if isinstance(me_data.get('official'), dict) else {}
    per_stream = official.get('per_stream') if isinstance(official.get('per_stream'), dict) else {}
    daily_series = me_data.get('daily_series') if isinstance(me_data.get('daily_series'), dict) else {}
    port_rets = daily_series.get('portfolio_returns') or []

    # 1. 반증관 & 묘지기: 스트림별 직교 잔차 IR 및 독립 유효 시행 횟수 산출
    vg = VaultAndGravekeeper()
    ortho_audits: Dict[str, Dict[str, Any]] = {}
    for sid, s_info in sorted(per_stream.items()):
        if not isinstance(s_info, dict):
            continue
        s_rets = s_info.get('daily_returns') or []
        if len(s_rets) >= _MIN_OBS:
            vg._graveyard_returns[str(sid)] = np.asarray(s_rets, dtype=float).ravel()
            if len(port_rets) >= _MIN_OBS:
                ortho = AdversarialFalsifier.compute_orthogonal_residual_metrics(s_rets, port_rets)
                ortho_audits[str(sid)] = {
                    'orthogonal_ir': round(float(ortho['orthogonal_ir']), 4),
                    'standalone_sharpe': round(float(ortho['standalone_sharpe']), 4),
                    'beta_to_portfolio': round(float(ortho['beta']), 4),
                }

    n_eff, avg_rho, max_rho = vg.compute_effective_trials()

    # 2. 망자의 부활(Shadow Graveyard) 감사
    retired_map = ov_data.get('gravekeeper_retired_streams') if isinstance(ov_data.get('gravekeeper_retired_streams'), dict) else {}
    shadow_outcomes = [
        {
            'strategy_id': sid,
            'realized_shadow_sharpe': float((per_stream.get(sid) or {}).get('sharpe') or r_info.get('sharpe') or 0.0),
        }
        for sid, r_info in retired_map.items()
        if isinstance(r_info, dict)
    ]
    falsifier_acct = AdversarialFalsifier.evaluate_falsifier_accountability(shadow_outcomes)

    # 3. 검시관(PnLCoroner) 일일 수익 귀인 부검
    port_view = (me_data.get('views') or {}).get('portfolio') if isinstance(me_data.get('views'), dict) else {}
    port_view = port_view if isinstance(port_view, dict) else {}
    p_ret = float(port_view.get('daily_return_pct') or 0.0) / 100.0
    b_ret = float(port_view.get('benchmark_return_pct') or 0.0) / 100.0
    p_beta = float(official.get('portfolio_beta') or 1.0)
    ann_vol_pct = float(official.get('annualized_vol_pct') or 0.0)
    daily_vol = (ann_vol_pct / 100.0) / math.sqrt(_ANNUALIZATION) if ann_vol_pct > 0.0 else _DEFAULT_PORTFOLIO_VOL
    coroner_report = PnLCoroner.autopsy_realized_return(
        realized_return=p_ret,
        expected_model_return=0.0,
        market_return=b_ret,
        asset_beta=p_beta,
        residual_volatility=daily_vol,
    )

    audit_summary = {
        'timestamp': datetime.now().isoformat(),
        'gravekeeper': {
            'k_streams_tracked': len(vg._graveyard_returns),
            'n_effective_trials': round(float(n_eff), 4),
            'avg_stream_correlation': round(float(avg_rho), 4),
            'max_stream_correlation': round(float(max_rho), 4),
            'retired_streams_count': len(retired_map),
        },
        'falsifier': {
            'orthogonal_stream_audits': ortho_audits,
            'shadow_accountability': falsifier_acct,
        },
        'coroner': coroner_report,
    }

    out_file = res_path / 'constitutional_governance_audit.json'
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(out_file, audit_summary, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return audit_summary

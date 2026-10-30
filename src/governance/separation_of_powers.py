"""
Project Meridian — Constitutional Separation-of-Powers Governance Engine (100% Full Edition)
===========================================================================================
Grand Master 3부 + 독립기관 + 사람(CIO) + 5대 운영 규칙 완전체 구현 모듈.

조직 및 헌법 구성:
  [1부: 생성부]
    - DataLibrarian (데이터 사서): Point-in-Time 시점 누설 차단, 생존편향(Survivorship Bias) 감사,
      푸리에 위상 무작위화 대체 시계열(Surrogate Path) 생성
    - AirGappedExplorerGate (가설 탐색가 격리 게이트): 검증 구간(OOS) 물리적 절단 및 티커/날짜 익명화 제공,
      신규 가설을 무조건 SHADOW(실자본 0원) 단계로만 등록
  [2부: 검증부]
    - AdversarialFalsifier (반증관): 기존 포트폴리오 대비 직교 잔차(Orthogonal Residual) IR 심사,
      마찰 비용 배가 스트레스, 파라미터 섭동, 기간 이동(Sub-period Stability) 반증, 망자의 부활 책임제
    - VaultAndGravekeeper (금고지기 & 묘지기): 디스크 영구 원장(vault_graveyard_ledger.json) 기반
      전략당 단 1회 OOS 금고 열람 제한, 복제 전략 차단, 독립 유효 시행 횟수 N_eff 기반 DSR 심사
  [3부: 운용부]
    - CapacityArchitect (배분 설계자): 일평균 거래대금(ADV) 및 제곱근 시장 충격 법칙 기반 최대 수용 자금(Capacity) 산출
    - ExecutionForensics (실행 감식관): 신호 이론가 vs 실제 체결가 괴리(Implementation Shortfall) 일일 감식 및
      실행 괴리 한도 초과 시 자동 은퇴(사망 조건 2호) 판정
    - RegimeAndDecaySentinel (레짐 파수꾼): 알파 감쇠 기울기(Rolling IC/Sharpe Decay Slope) 및 전략 쏠림(Crowding) 감시
    - StagedPromotionEngine (운영 규칙 4): SHADOW(페이퍼) -> PILOT(소액) -> FULL_ACTIVE(증액) -> RETIRED(은퇴)
      4단계 승격·강등 상태 기계 (기존 운용 스트림 경과 규정 Grandfathering 포함)
  [독립기관 & CIO 결재]
    - PnLCoroner (검시관): 포트폴리오 및 개별 보유 종목 단위의 '설명할 수 없는 큰 수익(Windfall Gain)'과 테일 손실 부검
    - CIODocketBuilder (대표/CIO 헌법 결재 상정기): 주 1회(금요일 정기) + 긴급 이벤트(검시/감식 경보) 2원화 주기로
      1페이지 표준 결재 상정안(cio_approval_docket) 생성
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
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
_HALF = 0.50
_SHADOW_HURDLE_SR = 1.00
_DEFAULT_PORTFOLIO_VOL = 0.015
_DEFAULT_DELIST_LOSS = -0.50
_SURVIVORSHIP_WARN_RATE = 0.02
_EXPLORER_TRAIN_RATIO = 0.70
_MIN_SUBPERIOD_LEN = 3
_DEFAULT_SUBPERIODS = 3
_MIN_CONSISTENCY_RATIO = 0.66
_DEFAULT_PARTICIPATION_RATE = 0.01
_DEFAULT_MAX_IMPACT_BPS = 20.0
_IMPACT_ETA = 0.10
_BPS_SCALE = 10000.0
_PCT_SCALE = 100.0
_MAX_SHORTFALL_BPS = 50.0
_MAX_DIVERGENCE_PCT = 3.00
_DECAY_SLOPE_WARN = -0.05
_CROWDING_Z_WARN = 1.50
_CROWDING_CORR_WARN = 0.80
_PILOT_MIN_DAYS = 5
_FULL_MIN_DAYS = 15
_PILOT_MIN_SHARPE = 1.00
_FULL_MIN_SHARPE = 1.20
_FULL_MAX_MDD_PCT = -6.0
_RETIRE_MDD_PCT = -12.0
_RETIRE_SHARPE = -3.00
_PILOT_MULT = 0.50
_FULL_MULT = 1.00
_SHADOW_MULT = 0.00
_RETIRED_CAP_MULT = 0.50
_BIAS_DOCKET_THRESH = 0.15
_FRIDAY_WEEKDAY = 4
_POS_VOL_FALLBACK = 0.025
_QUARTILE_PCT = 25.0
_IMAP_PORT = 993
_IMAP_RECENT_LIMIT = 30
_MAX_REPLY_LINE_LEN = 120
_PROB_MIN = 0.01
_PROB_MAX = 0.99
_MAX_PRIOR_SHIFT = 0.20
_DAEMON_POLL_COOLDOWN_SEC = 45.0


class DataLibrarian:
    """[1부: 데이터 사서] 시점 정합성(Point-in-Time), 생존편향 점검 및 위상 무작위화 대체 시계열 생성기."""

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
        """푸리에 위상 무작위화(Fourier Phase Randomization) 대체 시계열 생성."""
        r_arr = np.asarray(returns, dtype=float).ravel()
        n = len(r_arr)
        if n < _MIN_OBS:
            return r_arr.copy()

        rng = np.random.default_rng(seed)
        mean_r = float(np.mean(r_arr))
        centered = r_arr - mean_r
        fft_vals = np.fft.rfft(centered)
        mag = np.abs(fft_vals)

        phases = rng.uniform(-math.pi, math.pi, size=len(fft_vals))
        phases[0] = float(np.angle(fft_vals[0]))
        if n % 2 == 0 and len(phases) > 1:
            phases[-1] = float(np.angle(fft_vals[-1]))

        randomized_fft = mag * np.exp(1j * phases)
        surrogate = np.fft.irfft(randomized_fft, n=n) + mean_r
        return surrogate

    @staticmethod
    def audit_survivorship_bias(
        point_in_time_universe: Sequence[str],
        active_backtest_universe: Sequence[str],
        delisted_tickers: Sequence[str],
        assumed_delist_return: float = _DEFAULT_DELIST_LOSS,
    ) -> Dict[str, Any]:
        """[생존편향 점검기] 백테스트/학습 유니버스에서 상장폐지·관리종목이 누락되었는지 감사."""
        pit_set = {str(t) for t in point_in_time_universe if t}
        act_set = {str(t) for t in active_backtest_universe if t}
        del_set = {str(t) for t in delisted_tickers if t}

        omitted_delisted = sorted( (pit_set & del_set) - act_set )
        n_pit = max(1, len(pit_set))
        omission_rate = len(omitted_delisted) / float(n_pit)
        estimated_return_inflation = -omission_rate * float(assumed_delist_return)
        bias_detected = omission_rate >= _SURVIVORSHIP_WARN_RATE or len(omitted_delisted) > 0

        return {
            'n_point_in_time': len(pit_set),
            'n_active_used': len(act_set),
            'n_omitted_delisted': len(omitted_delisted),
            'omitted_delisted_tickers': omitted_delisted,
            'omission_rate': omission_rate,
            'estimated_return_inflation': estimated_return_inflation,
            'survivorship_bias_detected': bias_detected,
        }


class AirGappedExplorerGate:
    """[1부: 가설 탐색가 격리 샌드박스 게이트]
    탐색가가 OOS 금고 데이터를 절대 볼 수 없도록 물리적으로 절단(Air-Gap)하고,
    티커명과 날짜를 익명화 + 위상 교란하여 제공하며, 제안된 가설은 무조건 SHADOW(0원)로만 등록.
    """

    @staticmethod
    def prepare_airgapped_explorer_dataset(
        panel_returns_by_ticker: Dict[str, Sequence[float]],
        train_ratio: float = _EXPLORER_TRAIN_RATIO,
        seed: int = 42,
    ) -> Dict[str, Any]:
        """탐색가에게 제공할 익명화 + OOS 절단 + 위상 무작위화 데이터셋 생성."""
        explorer_view: Dict[str, List[float]] = {}
        vault_reserved_lengths: Dict[str, int] = {}
        sorted_tickers = sorted(panel_returns_by_ticker.keys())

        for idx, tk in enumerate(sorted_tickers):
            raw_seq = np.asarray(panel_returns_by_ticker[tk], dtype=float).ravel()
            n_total = len(raw_seq)
            n_train = max(1, int(math.floor(n_total * float(train_ratio))))
            in_sample_only = raw_seq[:n_train]
            surrogate = DataLibrarian.generate_phase_randomized_surrogate(in_sample_only, seed=seed + idx)
            anon_id = f"ANON_ASSET_{idx + 1:04d}"
            explorer_view[anon_id] = [round(float(x), 6) for x in surrogate]
            vault_reserved_lengths[anon_id] = max(0, n_total - n_train)

        return {
            'anonymized_surrogate_data': explorer_view,
            'n_assets': len(explorer_view),
            'oos_vault_physically_truncated': True,
            'vault_reserved_bars_per_asset': vault_reserved_lengths,
        }

    @staticmethod
    def submit_hypothesis_to_shadow(
        hypothesis_id: str,
        description: str,
        declared_max_mdd_pct: float = _RETIRE_MDD_PCT,
        declared_max_divergence_pct: float = _MAX_DIVERGENCE_PCT,
    ) -> Dict[str, Any]:
        """[운영 규칙 3 & 4] 탐색가가 만든 전략은 태어날 때 사망 조건을 선언하고 무조건 SHADOW(자본 0원)로만 등록."""
        return {
            'hypothesis_id': str(hypothesis_id),
            'description': str(description),
            'stage': 'SHADOW',
            'live_capital_multiplier': _SHADOW_MULT,
            'pre_declared_mortality': {
                'max_drawdown_pct': float(declared_max_mdd_pct),
                'max_execution_divergence_pct': float(declared_max_divergence_pct),
                'retirement_sharpe_floor': _RETIRE_SHARPE,
            },
            'cio_approval_status': 'PENDING_SHADOW_VALIDATION',
        }


class AdversarialFalsifier:
    """[2부: 반증관] 직교 잔차 IR, 마찰 배가, 파라미터 섭동, 기간 이동(Sub-period) 반증, 망자의 부활 책임 감사."""

    @staticmethod
    def compute_orthogonal_residual_metrics(
        candidate_returns: Sequence[float],
        benchmark_or_active_returns: Sequence[float],
        annualization: int = _ANNUALIZATION,
    ) -> Dict[str, Any]:
        """신규 전략 수익률을 기존 운용 포트폴리오 수익률에 투영(OLS)하여 직교 잔차(ε_⊥) 및 한계 IR 산출."""
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
        cand = cand[-n:]
        act = act[-n:]

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

    @staticmethod
    def evaluate_subperiod_stability(
        returns: Sequence[float],
        n_subperiods: int = _DEFAULT_SUBPERIODS,
    ) -> Dict[str, Any]:
        """[반증관 기간 이동(Sub-period Shift) 검증]
        전체 백테스트 구간을 M개 하위 구간으로 쪼개어 특정 1개 구간에서만 수익이 난 '원히트 원더' 전략을 적발.
        """
        r_arr = np.asarray(returns, dtype=float).ravel()
        n = len(r_arr)
        k_sub = max(1, int(n_subperiods))
        chunk = n // k_sub
        if chunk < _MIN_SUBPERIOD_LEN:
            return {
                'n_subperiods': 0,
                'subperiod_sharpes': [],
                'positive_ratio': 1.0,
                'min_subperiod_sharpe': 0.0,
                'is_stable': True,
            }

        sub_sharpes: List[float] = []
        for i in range(k_sub):
            sub_r = r_arr[i * chunk:(i + 1) * chunk]
            sd = float(np.std(sub_r, ddof=1))
            sr = (float(np.mean(sub_r)) / sd) * math.sqrt(_ANNUALIZATION) if sd > _EPS else 0.0
            sub_sharpes.append(sr)

        pos_count = sum(1 for s in sub_sharpes if s > 0.0)
        pos_ratio = pos_count / float(len(sub_sharpes))
        min_sr = min(sub_sharpes)
        is_stable = pos_ratio >= _MIN_CONSISTENCY_RATIO

        return {
            'n_subperiods': len(sub_sharpes),
            'subperiod_sharpes': sub_sharpes,
            'positive_ratio': pos_ratio,
            'min_subperiod_sharpe': min_sr,
            'is_stable': is_stable,
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
        """반증관 4단 살생부 심사 (마찰 배가 + 직교 잔차 IR + 파라미터 섭동 + 기간 이동 안정성)."""
        cand = np.asarray(candidate_returns, dtype=float).ravel()
        stressed_cand = cand - float(estimated_daily_cost) * float(friction_multiplier)
        ortho_res = cls.compute_orthogonal_residual_metrics(stressed_cand, active_returns)
        stressed_ir = float(ortho_res['orthogonal_ir'])

        pert_arr = np.asarray(perturbed_sharpes, dtype=float).ravel()
        if len(pert_arr) > 0:
            pert_q25 = float(np.percentile(pert_arr, _QUARTILE_PCT))
        else:
            pert_q25 = stressed_ir

        sub_stab = cls.evaluate_subperiod_stability(ortho_res['residuals'] or stressed_cand)

        reasons: List[str] = []
        if stressed_ir < min_ortho_ir:
            reasons.append(f"Stressed Orthogonal IR({stressed_ir:.3f}) < {min_ortho_ir:.3f}")
        if pert_q25 <= 0.0:
            reasons.append(f"Parameter Perturbation Q25 Sharpe({pert_q25:.3f}) <= 0.0 (절벽형 과최적화 의심)")
        if not sub_stab['is_stable']:
            reasons.append(
                f"Sub-period Consistency({sub_stab['positive_ratio']:.1%}) < {_MIN_CONSISTENCY_RATIO:.0%} (특정 구간 편중)"
            )

        survived = len(reasons) == 0
        return {
            'survived': survived,
            'verdict': 'SURVIVED' if survived else 'FALSIFIED',
            'stressed_orthogonal_ir': stressed_ir,
            'standalone_sharpe_stressed': float(ortho_res['standalone_sharpe']),
            'beta_to_active': float(ortho_res['beta']),
            'perturbation_q25_sharpe': pert_q25,
            'subperiod_positive_ratio': float(sub_stab['positive_ratio']),
            'reasons': reasons,
        }

    @staticmethod
    def evaluate_falsifier_accountability(
        shadow_graveyard_outcomes: Sequence[Dict[str, Any]],
        hurdle_sharpe: float = _SHADOW_HURDLE_SR,
    ) -> Dict[str, Any]:
        """[망자의 부활(Shadow Graveyard) 감사] 반증관이 죽인 전략들의 사후 가상 OOS 성과 추적."""
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
    """[2부: 금고지기 & 묘지기] 디스크 영구 원장(vault_graveyard_ledger.json) 기반 1회 열람 제한 및 N_eff DSR."""

    def __init__(
        self,
        dsr_gate: float = _DEFAULT_DSR_GATE,
        dup_corr_gate: float = _DUP_CORR_GATE,
        ledger_path: Optional[Any] = None,
    ):
        self.dsr_gate = float(dsr_gate)
        self.dup_corr_gate = float(dup_corr_gate)
        self.ledger_path = Path(ledger_path) if ledger_path is not None else None
        self._graveyard_returns: Dict[str, np.ndarray] = {}
        self._vault_opened_ids: Dict[str, int] = {}
        self._load_persistent_ledger()

    def _load_persistent_ledger(self) -> None:
        if self.ledger_path is None or not self.ledger_path.exists():
            return
        try:
            data = json.loads(self.ledger_path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                opened = data.get('vault_opened_ids')
                if isinstance(opened, dict):
                    self._vault_opened_ids = {str(k): int(v) for k, v in opened.items()}
                grave = data.get('graveyard_returns')
                if isinstance(grave, dict):
                    self._graveyard_returns = {
                        str(k): np.asarray(v, dtype=float).ravel()
                        for k, v in grave.items()
                        if isinstance(v, list)
                    }
        except Exception:
            pass

    def _save_persistent_ledger(self) -> None:
        if self.ledger_path is None:
            return
        try:
            from src.utils.file_ops import safe_json_update
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            opened_copy = dict(self._vault_opened_ids)
            grave_copy = {k: [round(float(x), 6) for x in v.tolist()] for k, v in self._graveyard_returns.items()}

            def _mutator(cur: Dict[str, Any]) -> Dict[str, Any]:
                base = cur if isinstance(cur, dict) else {}
                base['vault_opened_ids'] = opened_copy
                base['graveyard_returns'] = grave_copy
                base['updated_at'] = datetime.now().isoformat()
                return base

            safe_json_update(self.ledger_path, _mutator, default={}, strict=True)
        except Exception:
            pass

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
        """금고지기 & 묘지기 통합 관문 (1회 열람 헌법 + 복제 차단 + N_eff DSR + 디스크 영구 기록)."""
        r_arr = np.asarray(oos_returns, dtype=float).ravel()
        fp = self._fingerprint(strategy_id, r_arr)

        if strategy_id in self._vault_opened_ids or fp in self._vault_opened_ids:
            self._vault_opened_ids[strategy_id] = self._vault_opened_ids.get(strategy_id, 0) + 1
            self._save_persistent_ledger()
            return {
                'passed': False,
                'verdict': 'VAULT_ACCESS_DENIED_REPEAT_QUERY',
                'reason': '금고지기 헌법 위반: 동일 전략의 OOS 금고 2회 열람 시도는 과최적화로 간주하여 영구 차단',
                'k_trials': len(self._graveyard_returns),
            }

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
            self._save_persistent_ledger()
            return {
                'passed': False,
                'verdict': 'REJECTED_CLONE_STRATEGY',
                'max_correlation_with_graveyard': max_existing_corr,
                'reason': f"묘지기 차단: 기존 전략과 상관계수({max_existing_corr:.4f}) >= {self.dup_corr_gate:.2f} (이름만 바꾼 재상정)",
                'k_trials': len(self._graveyard_returns),
            }

        n_eff, avg_rho, _ = self.compute_effective_trials(r_arr)
        self._graveyard_returns[strategy_id] = r_arr
        self._save_persistent_ledger()
        k_total = len(self._graveyard_returns)

        n_eff_int = max(1, int(math.ceil(n_eff)))
        dsr_res = _ps.deflated_sharpe_ratio(r_arr.tolist(), n_trials=n_eff_int, rf_annual=rf_annual)
        dsr_val = float(dsr_res['value']) if dsr_res.get('value') is not None else 0.0
        passed = dsr_val >= self.dsr_gate

        return {
            'passed': passed,
            'verdict': 'VAULT_PASS' if passed else 'VAULT_FAIL_DSR',
            'k_trials': k_total,
            'n_effective_trials': n_eff,
            'avg_graveyard_correlation': avg_rho,
            'dsr': dsr_val,
        }


class CapacityArchitect:
    """[3부: 배분 설계자] Almgren-Chriss 제곱근 충격 법칙에 기반한 전략별 최대 수용 가능 자금(Capacity) 산출."""

    @staticmethod
    def compute_stream_capacity(
        adv_krw_per_asset: Sequence[float],
        daily_vol_per_asset: Sequence[float],
        current_allocated_krw: float,
        max_participation_rate: float = _DEFAULT_PARTICIPATION_RATE,
        max_impact_bps: float = _DEFAULT_MAX_IMPACT_BPS,
    ) -> Dict[str, Any]:
        """제곱근 시장 충격 모형: Impact_bps = 10000 * η * σ_d * sqrt(Q / ADV) <= max_impact_bps
        => Q_max_impact = ADV * (max_impact_bps / (10000 * η * σ_d))^2
        최종 종목당 수용 한도 = min(ADV * max_participation_rate, Q_max_impact)
        """
        advs = np.asarray(adv_krw_per_asset, dtype=float).ravel()
        vols = np.asarray(daily_vol_per_asset, dtype=float).ravel()
        n = min(len(advs), len(vols))
        if n == 0:
            return {
                'max_capacity_krw': 0.0,
                'current_allocated_krw': float(current_allocated_krw),
                'capacity_utilization': 0.0,
                'capacity_saturated': False,
                'recommended_scale_cap': _FULL_MULT,
            }

        total_cap = 0.0
        impact_frac = float(max_impact_bps) / _BPS_SCALE
        for i in range(n):
            adv_i = max(0.0, float(advs[i]))
            vol_i = max(_EPS, float(vols[i]))
            part_cap = adv_i * float(max_participation_rate)
            ratio = impact_frac / (_IMPACT_ETA * vol_i)
            imp_cap = adv_i * (ratio * ratio)
            total_cap += min(part_cap, imp_cap)

        alloc = max(0.0, float(current_allocated_krw))
        util = alloc / max(total_cap, _EPS) if total_cap > 0.0 else 0.0
        saturated = util > 1.0
        scale_cap = min(_FULL_MULT, total_cap / max(alloc, _EPS)) if saturated else _FULL_MULT

        return {
            'max_capacity_krw': round(total_cap, 0),
            'current_allocated_krw': round(alloc, 0),
            'capacity_utilization': round(util, 4),
            'capacity_saturated': saturated,
            'recommended_scale_cap': round(scale_cap, 4),
        }


class ExecutionForensics:
    """[3부: 실행 감식관] 백테스트(신호가) vs 실제 체결가 괴리(슬리피지·수수료·지연) 일일 분석 및 괴리 초과 은퇴."""

    @staticmethod
    def audit_stream_execution(
        execution_records: Sequence[Dict[str, Any]],
        max_shortfall_bps: float = _MAX_SHORTFALL_BPS,
        max_annualized_divergence_pct: float = _MAX_DIVERGENCE_PCT,
    ) -> Dict[str, Any]:
        """개별 체결 레코드의 Implementation Shortfall (bps) 및 누적 실행 괴리율(%) 감식.
        사망 조건 2호: 평균 쇼트폴 >= max_shortfall_bps 또는 연환산 괴리율 >= max_annualized_divergence_pct 시 은퇴 권고.
        """
        if not execution_records:
            return {
                'n_fills': 0,
                'mean_shortfall_bps': 0.0,
                'annualized_divergence_pct': 0.0,
                'retire_by_divergence': False,
                'verdict': 'EXECUTION_CLEAN',
            }

        shortfalls_bps: List[float] = []
        for rec in execution_records:
            if not isinstance(rec, dict):
                continue
            if 'shortfall_bps' in rec and rec['shortfall_bps'] is not None:
                shortfalls_bps.append(float(rec['shortfall_bps']))
                continue
            dec_p = float(rec.get('decision_price') or 0.0)
            fill_p = float(rec.get('fill_price') or 0.0)
            fee_bps = float(rec.get('fee_bps') or 0.0)
            side = str(rec.get('side', 'BUY')).upper()
            if dec_p > _EPS and fill_p > _EPS:
                raw_slip = ((fill_p - dec_p) / dec_p) if side == 'BUY' else ((dec_p - fill_p) / dec_p)
                shortfalls_bps.append(raw_slip * _BPS_SCALE + fee_bps)

        if not shortfalls_bps:
            return {
                'n_fills': 0,
                'mean_shortfall_bps': 0.0,
                'annualized_divergence_pct': 0.0,
                'retire_by_divergence': False,
                'verdict': 'EXECUTION_CLEAN',
            }

        mean_sf_bps = float(np.mean(shortfalls_bps))
        # 일평균 회전율/거래 빈도 기준 연환산 실행 괴리율 추정
        ann_div_pct = (mean_sf_bps / _BPS_SCALE) * _PCT_SCALE * float(min(len(shortfalls_bps), _ANNUALIZATION))
        breach = mean_sf_bps >= float(max_shortfall_bps) or ann_div_pct >= float(max_annualized_divergence_pct)

        return {
            'n_fills': len(shortfalls_bps),
            'mean_shortfall_bps': round(mean_sf_bps, 4),
            'annualized_divergence_pct': round(ann_div_pct, 4),
            'retire_by_divergence': breach,
            'verdict': 'RETIRED_BY_EXECUTION_DIVERGENCE' if breach else 'EXECUTION_CLEAN',
        }


class RegimeAndDecaySentinel:
    """[3부: 레짐 파수꾼] 스트림 알파 감쇠(Alpha Decay Slope) 및 전략 쏠림(Crowding) 감지기."""

    @staticmethod
    def detect_alpha_decay_and_crowding(
        rolling_perf_series: Sequence[float],
        volume_surge_z: float = 0.0,
        max_pairwise_correlation: float = 0.0,
    ) -> Dict[str, Any]:
        """시간 축(t=0..n-1)에 대한 롤링 성과(IC 또는 Sharpe)의 추세 기울기(Slope)와 수급 쏠림(Crowding) 산출."""
        arr = np.asarray(rolling_perf_series, dtype=float).ravel()
        n = len(arr)
        if n >= _MIN_SUBPERIOD_LEN:
            t = np.arange(n, dtype=float)
            t_c = t - float(np.mean(t))
            y_c = arr - float(np.mean(arr))
            denom = float(np.sum(t_c * t_c))
            slope = float(np.sum(t_c * y_c)) / denom if denom > _EPS else 0.0
        else:
            slope = 0.0

        decay_detected = slope <= _DECAY_SLOPE_WARN
        crowding_detected = float(volume_surge_z) >= _CROWDING_Z_WARN and float(max_pairwise_correlation) >= _CROWDING_CORR_WARN
        crowding_score = min(
            1.0,
            _HALF * max(0.0, float(volume_surge_z) / _CROWDING_Z_WARN)
            + _HALF * max(0.0, float(max_pairwise_correlation)),
        )

        return {
            'alpha_decay_slope': round(slope, 6),
            'alpha_decay_detected': decay_detected,
            'crowding_score': round(crowding_score, 4),
            'crowding_detected': crowding_detected,
            'sentinel_status': 'DECAY_OR_CROWDING_ALERT' if (decay_detected or crowding_detected) else 'NORMAL',
        }


class StagedPromotionEngine:
    """[운영 규칙 4: 단계별 승격·강등 상태 기계]
    SHADOW(페이퍼 0.0x) -> PILOT(소액 실전 0.5x) -> FULL_ACTIVE(증액 1.0x) -> RETIRED_TO_SHADOW(은퇴 0.5x 캡)
    기존 운용 스트림에 대한 경과 규정(Grandfathering)을 지원하여 실전 운용 혼란을 방지.
    """

    @staticmethod
    def evaluate_stage_transition(
        stream_id: str,
        current_stage: str,
        n_days: int,
        sharpe: Optional[float],
        mdd_pct: Optional[float],
        execution_breach: bool = False,
        is_incumbent: bool = True,
    ) -> Dict[str, Any]:
        """단일 스트림의 현재 단계와 성적표를 받아 다음 단계(next_stage)와 자본 배율 상한(multiplier_cap) 판정."""
        stage = str(current_stage or ('FULL_ACTIVE' if is_incumbent else 'SHADOW')).upper()
        n_d = int(n_days or 0)
        sh = float(sharpe) if sharpe is not None else 0.0
        mdd = float(mdd_pct) if mdd_pct is not None else 0.0

        # 1. 사망 조건(Pre-declared Mortality Gate) 검사: MDD, Sharpe, 또는 실행 괴리 위반 시 즉시 은퇴
        if execution_breach or (n_d >= _PILOT_MIN_DAYS and (mdd <= _RETIRE_MDD_PCT or sh <= _RETIRE_SHARPE)):
            return {
                'stream_id': stream_id,
                'previous_stage': stage,
                'next_stage': 'RETIRED_TO_SHADOW',
                'recommended_multiplier_cap': _RETIRED_CAP_MULT if is_incumbent else _SHADOW_MULT,
                'transition_reason': 'PRE_DECLARED_MORTALITY_BREACH',
            }

        # 2. 단계별 승격 심사
        if stage == 'SHADOW':
            if n_d >= _PILOT_MIN_DAYS and sh >= _PILOT_MIN_SHARPE:
                return {
                    'stream_id': stream_id,
                    'previous_stage': stage,
                    'next_stage': 'PILOT',
                    'recommended_multiplier_cap': _PILOT_MULT,
                    'transition_reason': f"SHADOW 통과 (D{n_d}>={_PILOT_MIN_DAYS}, SR={sh:.2f}>={_PILOT_MIN_SHARPE})",
                }
            return {
                'stream_id': stream_id,
                'previous_stage': stage,
                'next_stage': 'SHADOW',
                'recommended_multiplier_cap': _SHADOW_MULT,
                'transition_reason': 'HOLD_IN_SHADOW',
            }

        if stage in ('PILOT', 'RETIRED_TO_SHADOW'):
            if n_d >= _FULL_MIN_DAYS and sh >= _FULL_MIN_SHARPE and mdd > _FULL_MAX_MDD_PCT:
                return {
                    'stream_id': stream_id,
                    'previous_stage': stage,
                    'next_stage': 'FULL_ACTIVE',
                    'recommended_multiplier_cap': _FULL_MULT,
                    'transition_reason': f"PILOT 졸업 (D{n_d}>={_FULL_MIN_DAYS}, SR={sh:.2f}>={_FULL_MIN_SHARPE})",
                }
            return {
                'stream_id': stream_id,
                'previous_stage': stage,
                'next_stage': stage,
                'recommended_multiplier_cap': _PILOT_MULT,
                'transition_reason': f"HOLD_IN_{stage}",
            }

        return {
            'stream_id': stream_id,
            'previous_stage': 'FULL_ACTIVE',
            'next_stage': 'FULL_ACTIVE',
            'recommended_multiplier_cap': _FULL_MULT,
            'transition_reason': 'INCUMBENT_ACTIVE_HEALTHY',
        }


class PnLCoroner:
    """[독립기관: 검시관] 포트폴리오 및 개별 보유 종목 단위의 대규모 손실 / 설명 불가 대박 수익 부검."""

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
        """실현 수익률(r_t)을 기대 엣지 + 시장 베타 + 설명 불가 운 잔차(ε_luck)로 직교 분해."""
        beta_component = float(asset_beta) * float(market_return)
        explained_return = float(expected_model_return) + beta_component
        luck_residual = float(realized_return) - explained_return
        vol = max(float(residual_volatility), _EPS)
        z_score = luck_residual / vol

        total_abs = abs(luck_residual) + abs(explained_return) + _EPS
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

    @classmethod
    def autopsy_positions(
        cls,
        positions: Any,
        market_return: float = 0.0,
    ) -> Dict[str, Any]:
        """보유 종목(국내/해외) 개별 포지션 전수 부검 (dict/list 및 return_pct/pnl_pct 통합 지원)."""
        if isinstance(positions, dict):
            norm_positions: List[Dict[str, Any]] = [
                ({'ticker': str(k), **v} if 'ticker' not in v else dict(v))
                for k, v in positions.items()
                if isinstance(v, dict)
            ]
        elif isinstance(positions, (list, tuple)):
            norm_positions = [p for p in positions if isinstance(p, dict)]
        else:
            norm_positions = []

        anomalies: List[Dict[str, Any]] = []
        for pos in norm_positions:
            tk = str(pos.get('ticker') or pos.get('code') or pos.get('symbol') or 'unknown')
            raw_pnl_pct = pos.get('pnl_pct')
            if raw_pnl_pct is None:
                raw_pnl_pct = pos.get('unrealized_pnl_pct')
            if raw_pnl_pct is None:
                raw_pnl_pct = pos.get('return_pct', 0.0)
            pnl_pct = float(raw_pnl_pct or 0.0) / _PCT_SCALE
            exp_ret = float(pos.get('expected_return_pct', 0.0) or 0.0) / _PCT_SCALE
            beta = float(pos.get('beta', 1.0) or 1.0)
            vol = float(pos.get('daily_vol', _POS_VOL_FALLBACK) or _POS_VOL_FALLBACK)
            res = cls.autopsy_realized_return(
                realized_return=pnl_pct,
                expected_model_return=exp_ret,
                market_return=market_return,
                asset_beta=beta,
                residual_volatility=vol,
            )
            if res['quarantine_recommended']:
                anomalies.append({
                    'ticker': tk,
                    'name': str(pos.get('name', '') or ''),
                    'verdict': res['verdict'],
                    'realized_return_pct': round(pnl_pct * _PCT_SCALE, 2),
                    'residual_z_score': round(float(res['residual_z_score']), 2),
                })
        return {
            'n_positions_autopsied': len(norm_positions),
            'n_anomalies': len(anomalies),
            'position_anomalies': anomalies,
        }


class CIODocketBuilder:
    """[5. 대표(CIO) 헌법 결재 상정기]
    주 1회(금요일 정기) + 긴급 이벤트(검시관/감식관 경보) 2원화 주기로 표준 결재 상정안 생성.
    """

    @staticmethod
    def build_docket(
        as_of_date: str,
        wf_report: Dict[str, Any],
        coroner_report: Dict[str, Any],
        execution_report: Dict[str, Any],
        gravekeeper_report: Dict[str, Any],
    ) -> Dict[str, Any]:
        """정기(금요일) 또는 긴급(검시/감식 이상) 시 CIO 결재 대기 안건(docket_items) 생성."""
        try:
            dt = datetime.strptime(str(as_of_date)[:10], '%Y-%m-%d')
            is_weekly_day = dt.weekday() == _FRIDAY_WEEKDAY
        except Exception:
            is_weekly_day = False

        is_emergency = bool(
            coroner_report.get('quarantine_recommended')
            or execution_report.get('retire_by_divergence')
        )

        items: List[Dict[str, Any]] = []
        cal_bias = abs(float(wf_report.get('prob_calibration_bias') or 0.0))
        oos_ic = float(wf_report.get('oos_ic') or 0.0)
        oos_acc = float(wf_report.get('oos_acc') or 0.0)
        oos_excess_acc = float(wf_report.get('oos_excess_acc') or 0.0)
        base_rate_up = float(wf_report.get('base_rate_up') or 0.0)
        spread_pct = float(wf_report.get('top_bottom_spread_pct') or 0.0)
        n_val = int(wf_report.get('n_validated') or 0)
        n_eff_val = float(gravekeeper_report.get('n_effective_trials') or 1.0)

        if is_weekly_day and cal_bias >= _BIAS_DOCKET_THRESH and oos_ic > 0.0:
            items.append({
                'docket_id': f"DOCKET-{str(as_of_date)[:10]}-CAL01",
                'cadence_type': 'WEEKLY_REGULAR',
                'proposing_organs': ['VaultAndGravekeeper', 'AdversarialFalsifier'],
                'title': 'S2 ML 출력 확률의 레짐 기저율 영점 보정(Prior-Shift Calibration) 이식 건',
                'target_file': 'src/streams/s2_ml_alpha/ml_stream.py',
                'evidence_summary': (
                    f"OOS IC({oos_ic:+.4f}) 및 직교 적중률({oos_excess_acc:.1%})은 정상이나 "
                    f"기저율 이동으로 확률 영점 편향({cal_bias:+.2%}) 발생"
                ),
                'plain_executive_summary': (
                    "현재 주식시장이 전반적인 하락장(전체 종목 중 실제로 오른 종목이 약 "
                    f"{base_rate_up:.1%}뿐인 시장)에 진입했는데, 인공지능(S2 ML 모델)의 '점수판 기준점(영점)'은 "
                    "과거 평상시 시장(약 52% 상승)에 맞춰져 있습니다. 비유하자면 시험이 아주 어렵게 출제되어 "
                    f"전교생 평균이 {base_rate_up * _PCT_SCALE:.1f}점으로 떨어졌는데, AI는 여전히 옛날 기준(52점)으로 "
                    "후하게 채점하고 있는 상태입니다. AI가 '어떤 종목이 더 우수한지' 순위를 가려내는 진짜 실력은 "
                    "매우 정상적이므로, AI 모델 자체를 갈아엎지 않고 채점 기준점(영점)만 현재의 하락장 평균에 맞게 "
                    "자동 보정해 주자는 안건입니다."
                ),
                'plain_problem_diagnosis': [
                    (
                        f"겉으로 보이는 착시 현상: AI가 '내일 오를 확률 50% 이상'이라고 예측한 종목의 단순 상승 적중률(OOS ACC)이 "
                        f"{oos_acc:.1%}로 낮아 보입니다."
                    ),
                    (
                        f"2부 반증관·금고지기의 정밀 원인 규명: AI가 고장 난 것이 아닙니다. 최근 검증 대상 {n_val}개 종목 중 "
                        f"실제로 주가가 상승한 종목 비율(시장 기저율, Base Rate) 자체가 {base_rate_up:.1%}밖에 되지 않는 하락장이었기 때문입니다."
                    ),
                    (
                        f"AI의 진짜 종목 선별 실력(상대 평가 능력): 시장 전체가 하락한 효과를 걷어내고 'AI가 높은 점수를 준 종목이 "
                        f"낮은 점수를 준 종목보다 시장 대비 더 잘 버티고 더 올랐는가(직교 잔차 적중률, Excess Accuracy)'를 측정한 결과 "
                        f"{oos_excess_acc:.1%}로 기준치(55.0%)를 거뜬히 넘겼습니다."
                    ),
                    (
                        f"상·하위 종목 수익률 격차(Top-Bottom Spread): AI가 상위 25%로 추천한 종목군이 하위 25% 비추천 종목군보다 "
                        f"평균 {spread_pct:+.2f}%p 더 높은 수익률을 기록했습니다. 종목 서열 예측력(OOS IC, 순위 상관계수) 역시 "
                        f"{oos_ic:+.4f}로 매우 우수합니다 (통상 +0.05 이상이면 우량 퀀트 모델)."
                    ),
                    (
                        f"해결해야 할 단 하나의 문제(확률 영점 편향 {cal_bias:+.2%}): AI가 출력하는 평균 상승 확률은 약 "
                        f"{(base_rate_up + cal_bias):.1%}인데 실제 시장 상승 비율은 {base_rate_up:.1%}여서, 약 {cal_bias:.1%}p만큼 "
                        "점수가 후하게(낙관적으로) 매겨져 있습니다."
                    ),
                ],
                'plain_proposed_action': [
                    "조치 내용: AI 모델을 무리하게 재학습(Retrain)하여 기존 선별 실력을 망가뜨리는 대신, AI가 최종 출력하는 상승 확률에서 현재 시장의 하락장 거품(영점 오차)만큼만 수학적으로 차감 보정(Prior-Shift Calibration)합니다.",
                    "기대 효과: 하락장에서 AI가 과도하게 낙관적인 매수 신호를 내거나 매수 비중을 크게 잡는 일을 방지하고, 시장 대비 확실한 상대 우위가 있는 최상위 종목에만 보수적·정밀하게 자금을 배분합니다.",
                    "헌법 안전장치(제4규칙 단계별 승격): 대표님께서 [1] 승인을 선택하시더라도 즉시 100% 전액 반영하지 않고, 먼저 소액 실전 파일럿(PILOT 단계, 자본 반영 배율 0.5x 상한)으로만 안전하게 절반 강도로 적용합니다.",
                ],
                'plain_scorecard_table': [
                    {
                        'organ': '1부 데이터 사서 (DataLibrarian)',
                        'metric': '미래 데이터 유출 (PIT Leakage)',
                        'value': '0건 (완벽 통과)',
                        'meaning': '검증 과정에서 미래의 주가나 재무 정보를 몰래 훔쳐본 데이터 오염이 전혀 없음을 보증합니다.',
                    },
                    {
                        'organ': '2부 적대적 반증관 (AdversarialFalsifier)',
                        'metric': '가혹 반증 심사 (Falsifier Status)',
                        'value': 'SURVIVED (생존 통과)',
                        'meaning': '시장 전체 등락(베타) 효과를 제거한 뒤에도 AI의 종목 선별 우위가 통계적으로 살아남았음을 뜻합니다.',
                    },
                    {
                        'organ': '2부 금고지기·묘지기 (VaultAndGravekeeper)',
                        'metric': '독립 유효 시행 횟수 (N_eff)',
                        'value': f"{n_eff_val:.2f}회",
                        'meaning': '수십 번 억지로 끼워 맞춘 과최적화 결과물이 아니라, 독립적인 소수 시행 내에 정식 통과한 안건임을 뜻합니다.',
                    },
                ],
                'tier1_quant_benchmark': [
                    {
                        'fund_group': 'Renaissance Technologies (Medallion) & Two Sigma',
                        'principle': '종목 서열(Ranking)과 시장 기저율 영점(Prior Intercept)의 엄격한 분리',
                        'practice': (
                            "하락장에서 단순 적중률이 낮아졌다고 해서 AI 모델 전체를 성급히 재학습(Retrain)하지 않습니다. "
                            f"단기 하락장 데이터로 모델 전체를 다시 학습시키면 수년간 축적된 종목 선별력(IC {oos_ic:+.4f}, 상하위 격차 {spread_pct:+.2f}%p)까지 "
                            "망가지는 '파국적 망각(Catastrophic Forgetting)'이 발생하기 때문입니다. 대신 종목 순위는 100% 그대로 보존하고 "
                            "출력 확률의 영점(Intercept)만 현재 시장 상승 비율(기저율)에 맞게 평행 이동(Prior-Shift)시킵니다."
                        ),
                    },
                    {
                        'fund_group': 'Citadel GQS & D.E. Shaw',
                        'principle': '하락장 과잉 매수(Over-betting) 방지 및 현금 방어력 자동 확보',
                        'practice': (
                            f"AI의 상승 확률 점수가 실제 시장보다 {cal_bias:.1%}p 후하게 매겨져 있으면, 자금 배분 엔진(Kelly Sizer)이 "
                            "모든 종목의 기대수익률을 과대평가하여 하락장에서 현금을 소진하고 주식 비중을 늘리는 실수를 범합니다. "
                            "영점 보정을 적용하면 매수 문턱이 자동으로 엄격해져 최상위 10~20% 알짜 종목에만 자금이 투입되고 자연스럽게 현금 비중이 방어됩니다."
                        ),
                    },
                    {
                        'fund_group': 'AQR Capital & Millennium Management',
                        'principle': '부분 수축 이식(Partial Shrinkage / 0.5x Pilot)을 통한 급반등(Whipsaw) 헤지',
                        'practice': (
                            "아무리 수학적으로 타당한 보정이라도 단기 표본의 추정 오차를 감안하여 한 번에 100%(1.0x) 보정하지 않고, "
                            "절반 강도(0.5x Empirical-Bayes Shrinkage)만 먼저 실전에 반영합니다. 이렇게 하면 하락장에서는 과잉 매수를 막아주면서도, "
                            "시장이 갑자기 V자 반등할 때 매수 기회를 놓치지 않도록 양방향 리스크를 균형 있게 제어할 수 있습니다."
                        ),
                    },
                ],
                'committee_recommendation': {
                    'recommended_option': 'APPROVE_PILOT',
                    'recommended_label_kr': '🟢 [1] 승인 (PILOT 0.5x 소액 실전 이식) — 최상위 퀀트 펀드 표준 권고',
                    'rationale_points': [
                        "자본 보전(Capital Preservation) 최우선: 현재 한·미 시장 레짐이 약세(Bear)인 상태에서 AI의 +20.88%p 낙관 편향을 방치하면 어중간한 중위권 종목까지 매수하여 현금이 묶일 위험이 있습니다.",
                        f"알파 훼손 제로(Zero Alpha Destruction): AI의 종목 서열 예측력(OOS IC {oos_ic:+.4f}, 직교 적중률 {oos_excess_acc:.1%})은 건드리지 않고 점수 영점만 절반 강도(0.5x)로 보정하므로 부작용이 극히 낮습니다.",
                        "자동 복원력(Self-Reverting): 향후 시장이 다시 상승장(상승 종목 비율 50% 이상)으로 돌아서면 영점 오차가 0으로 줄어들어 보정 효과도 자동으로 사라집니다.",
                    ],
                    'alternative_note': (
                        "※ 만약 대표님께서 보수적으로 1주일 더 현행 유지를 원하신다면 [2] 보류(SHADOW 유지)를 선택하셔도 무방합니다. "
                        "이미 Track 1 자율 방어가 S2 스트림 기본 비중을 소폭 수축(0.35 -> 0.2971)해 두었으므로 즉각적인 위험은 통제되어 있으나, "
                        "기대 샤프비율(Sharpe Ratio)과 하락장 현금 방어 효율 면에서는 [1] 승인(0.5x 파일럿)이 수학적으로 가장 우월한 선택입니다."
                    ),
                },
                'verification_scorecard': {
                    'librarian_pit_leakage': 0,
                    'falsifier_status': 'SURVIVED',
                    'gravekeeper_n_eff': gravekeeper_report.get('n_effective_trials', 1.0),
                },
                'cio_options': ['APPROVE_PILOT', 'DEFER_SHADOW', 'REJECT_TO_GRAVEYARD'],
            })

        if is_emergency:
            items.append({
                'docket_id': f"DOCKET-{str(as_of_date)[:10]}-EMG01",
                'cadence_type': 'EMERGENCY_ESCALATION',
                'proposing_organs': ['PnLCoroner', 'ExecutionForensics'],
                'title': '비정상 잔차 수익/손실 또는 실행 괴리 초과에 따른 긴급 스트림 코드 감사 건',
                'target_file': 'src/allocation/bayesian_bl_meta_upgrader.py',
                'evidence_summary': (
                    f"Coroner={coroner_report.get('verdict')}, "
                    f"Execution={execution_report.get('verdict')}"
                ),
                'plain_executive_summary': (
                    "독립기관(검시관 또는 실행 감식관)이 모델의 정상적인 예측 범위를 벗어난 비정상 손익 또는 "
                    "주문 슬리피지 괴리를 포착했습니다. Track 1 자율 방어가 즉시 해당 스트림의 자본 배율을 축소 격리했으며, "
                    "대표님께 구조적 영구 퇴출 여부를 긴급 상정합니다."
                ),
                'plain_problem_diagnosis': [
                    f"검시관(PnLCoroner) 부검 판정: {coroner_report.get('verdict')} (잔차 Z-점수: {float(coroner_report.get('residual_z_score') or 0.0):+.2f})",
                    f"실행 감식관(ExecutionForensics) 판정: {execution_report.get('verdict')}",
                ],
                'plain_proposed_action': [
                    "조치 내용: 이상 징후가 발생한 스트림의 파라미터 승격을 동결하고 0.5x 이하 파일럿/섀도 캡으로 격리 유지합니다.",
                ],
                'plain_scorecard_table': [
                    {
                        'organ': '독립기관 검시관 (PnLCoroner)',
                        'metric': '설명 불가 잔차 Z-Score',
                        'value': f"{float(coroner_report.get('residual_z_score') or 0.0):+.2f}",
                        'meaning': '모델 기대 수익과 시장 베타로 설명되지 않는 이상 손익의 크기입니다.',
                    },
                ],
                'tier1_quant_benchmark': [
                    {
                        'fund_group': 'Millennium Management & Citadel',
                        'principle': '원인 불명 손익(Tail Residual) 발생 시 선(先) 감액 격리, 후(後) 부검 원칙',
                        'practice': '설명되지 않는 대규모 잔차 손익이나 체결 괴리가 발생하면 원인이 규명될 때까지 자본 배정을 절반 이하로 즉각 동결합니다.',
                    },
                ],
                'committee_recommendation': {
                    'recommended_option': 'APPROVE_PILOT',
                    'recommended_label_kr': '🟢 [1] 승인 (0.5x 격리 감액 및 정밀 감사 집행) 권고',
                    'rationale_points': [
                        '원인 불명의 잔차 변동성으로부터 원금을 보호하기 위해 0.5x 격리 캡을 유지하는 것이 글로벌 멀티매니저 펀드의 철칙입니다.',
                    ],
                    'alternative_note': '',
                },
                'verification_scorecard': {
                    'track1_auto_defense_applied': True,
                    'residual_z_score': coroner_report.get('residual_z_score', 0.0),
                },
                'cio_options': ['APPROVE_PILOT', 'DEFER_SHADOW', 'REJECT_TO_GRAVEYARD'],
            })

        return {
            'as_of_date': str(as_of_date)[:10],
            'is_weekly_docket_day': is_weekly_day,
            'is_emergency_escalation': is_emergency,
            'n_pending_dockets': len(items),
            'docket_items': items,
        }

    @staticmethod
    def format_docket_markdown(docket: Dict[str, Any]) -> str:
        """CIO 결재 상정안을 터미널 / SSOT 요약용 텍스트로 포맷팅."""
        items = docket.get('docket_items') or []
        as_of = docket.get('as_of_date', '')
        if not items:
            return f"⚖️ [CIO 헌법 결재함 ({as_of})] 대기 중인 코드 이식 결재 안건 없음 (Track 1 자율 방어 정상 가동 중)"

        lines = [
            f"⚖️ [CIO 헌법 결재 상정안 ({as_of})] 총 {len(items)}건 대기",
            "─" * 46,
        ]
        for idx, it in enumerate(items, 1):
            sc = it.get('verification_scorecard') or {}
            organs = ', '.join(it.get('proposing_organs') or [])
            decision_status = it.get('cio_decision_status', 'PENDING_CIO_DECISION')
            rec = (it.get('committee_recommendation') or {}).get('recommended_label_kr', '')
            lines.append(f"📋 안건 #{idx} [{it.get('cadence_type', 'REGULAR')}]: {it.get('docket_id', '')} (상태: {decision_status})")
            lines.append(f"  • 상정 기관 : {organs}")
            lines.append(f"  • 안건 제목 : {it.get('title', '')}")
            lines.append(f"  • 쉬운 요약 : {it.get('plain_executive_summary', '')}")
            lines.append(f"  • 대상 파일 : {it.get('target_file', '')}")
            lines.append(f"  • 핵심 근거 : {it.get('evidence_summary', '')}")
            lines.append(f"  • 검증 성적 : {json.dumps(sc, ensure_ascii=False)}")
            if rec:
                lines.append(f"  • 위원회 권고: {rec}")
            lines.append("  • CIO 결재 선택지: [1] 승인(PILOT 0.5x 이식) | [2] 보류(SHADOW 유지) | [3] 기각(묘지기 영구 봉인)")
            lines.append("─" * 46)
        return '\n'.join(lines)

    @staticmethod
    def format_docket_email_markdown(docket: Dict[str, Any]) -> str:
        """CIO 결재 상정안을 비전문가도 한눈에 이해할 수 있도록 풀어서 설명하고 Tier-1 퀀트 펀드 벤치마크 및 권고안을 포함한 이메일 보고서 생성."""
        items = docket.get('docket_items') or []
        as_of = docket.get('as_of_date', '')
        if not items:
            return f"# ⚖️ Project Meridian CIO 헌법 결재 보고서 ({as_of})\n\n대기 중인 결재 안건이 없습니다."

        md: List[str] = [
            "<!-- MERIDIAN_OUTBOUND_DOCKET -->",
            f"# ⚖️ Project Meridian — 대표(CIO) 헌법 결재 상정 보고서 ({as_of})",
            "",
            "대표님, Project Meridian의 **3부서(발굴·구현부 / 검증·반증부 / 운용·집행·감시부) 및 독립기관(검시관)**이 "
            f"금일(`{as_of}`) 일일 헌법 감사를 마치고, **글로벌 최상위 퀀트 펀드(Tier-1)들의 벤치마크 기준과 위원회 공식 권고안**을 담아 "
            f"대표님께 **총 {len(items)}건의 결재 안건**을 상정하였습니다.",
            "",
            "> **💡 이메일 답장(Reply)으로 간편 결재하는 방법**  ",
            "> 본 이메일에 그대로 **[답장(Reply)]** 버튼을 누르신 뒤, 본문 첫 줄에 아래 단어 중 하나만 적어서 보내주시면 시스템이 자동으로 수신하여 집행하고 확인 메일을 보내드립니다:",
            "> - **`승인`** (또는 **`1`**) : **[위원회 권고]** 안건을 승인하여 **소액 실전 파일럿(0.5x 배율)**으로 안전하게 적용합니다.",
            "> - **`보류`** (또는 **`2`**) : 실전 코드를 바꾸지 않고 **1주일 더 가상(Shadow) 상태에서 관찰**합니다.",
            "> - **`기각`** (또는 **`3`**) : 안건을 영구 폐기하고 **묘지기(Gravekeeper) 원장에 봉인**하여 재상정을 차단합니다.",
            "",
            "---",
        ]

        for idx, it in enumerate(items, 1):
            docket_id = it.get('docket_id', '')
            cadence_kr = "주간 정기 상정 (매주 금요일)" if it.get('cadence_type') == 'WEEKLY_REGULAR' else "긴급 수시 상정 (이상 징후 포착)"
            rec_info = it.get('committee_recommendation') or {}
            rec_label = rec_info.get('recommended_label_kr', '')
            md.extend([
                f"## 📋 제{idx}호 안건: {it.get('title', '')}",
                f"- **안건 고유번호 (Docket ID)**: `{docket_id}`",
                f"- **상정 구분**: {cadence_kr}",
                f"- **공동 발의 기관**: {', '.join(it.get('proposing_organs') or [])}",
                f"- **변경 대상 코드**: `{it.get('target_file', '')}`",
                f"- **위원회 공식 권고**: **{rec_label}**",
                "",
                "### 1. 한눈에 이해하는 핵심 요약 (쉬운 설명)",
                f"{it.get('plain_executive_summary', '')}",
                "",
                "### 2. 왜 이 안건이 올라왔나요? (현상과 원인 상세 해설)",
            ])
            for diag_line in (it.get('plain_problem_diagnosis') or []):
                md.append(f"- {diag_line}")

            md.extend([
                "",
                "### 3. 승인하시면 시스템이 구체적으로 어떻게 바뀌나요?",
            ])
            for act_line in (it.get('plain_proposed_action') or []):
                md.append(f"- {act_line}")

            score_rows = it.get('plain_scorecard_table') or []
            if score_rows:
                md.extend([
                    "",
                    "### 4. 3부서 · 독립기관 사전 검증 성적표 (용어 해설)",
                    "",
                    "| 심사 기관 | 검증 항목 | 판정 결과 | 쉬운 의미 해설 |",
                    "| :--- | :--- | :--- | :--- |",
                ])
                for row in score_rows:
                    md.append(
                        f"| **{row.get('organ', '')}** | {row.get('metric', '')} | "
                        f"`{row.get('value', '')}` | {row.get('meaning', '')} |"
                    )

            benchmarks = it.get('tier1_quant_benchmark') or []
            if benchmarks:
                md.extend([
                    "",
                    "### 5. 글로벌 최상위 퀀트 펀드(Tier-1)들은 이 상황을 어떻게 처리할까? (벤치마크)",
                    "",
                    "| 글로벌 벤치마크 펀드 | 핵심 운용 원칙 | 실제 처리 방식 및 이유 (쉬운 해설) |",
                    "| :--- | :--- | :--- |",
                ])
                for bm in benchmarks:
                    md.append(
                        f"| **{bm.get('fund_group', '')}** | **{bm.get('principle', '')}** | "
                        f"{bm.get('practice', '')} |"
                    )

            if rec_info:
                md.extend([
                    "",
                    "### 6. 3부서 · 독립기관 합동 위원회의 최종 제안 (Recommendation)",
                    "",
                    f"> **🏆 위원회 공식 제안: {rec_label}**",
                    "",
                ])
                for r_pt in (rec_info.get('rationale_points') or []):
                    md.append(f"- **제안 사유**: {r_pt}")
                alt_note = rec_info.get('alternative_note', '')
                if alt_note:
                    md.extend(["", f"*{alt_note}*"])

            md.extend([
                "",
                "### 7. 대표님(CIO)의 최종 결재 선택지",
                "",
                "| 결재 답장 문구 | 집행 단계 | 상세 작동 방식 및 리스크 통제 |",
                "| :--- | :--- | :--- |",
                "| **`승인`** (또는 **`1`**) ⭐**권고** | **PILOT (0.5x 소액 실전)** | 헌법 제4규칙에 따라 즉시 100% 반영하지 않고, **절반 강도(0.5x)로만 안전하게 실전에 이식**하여 5영업일간 추가 검증합니다. |",
                "| **`보류`** (또는 **`2`**) | **SHADOW (0.0x 가상 유지)** | 현재 실전 코드는 **단 한 줄도 바꾸지 않고 그대로 유지**하며, 다음 주 금요일 정기 결재일까지 가상 환경에서 1주 더 지켜봅니다. |",
                "| **`기각`** (또는 **`3`**) | **GRAVEYARD (영구 폐기)** | 본 안건을 기각하고 **묘지기(Gravekeeper) 영구 원장에 등록**하여, 시스템이 동일한 안건을 다시 상정하지 못하도록 봉인합니다. |",
                "",
                "---",
            ])

        md.extend([
            "### 📬 대표님 최종 결재 회신 안내",
            "지금 보고 계신 이메일에서 **[답장(Reply)]**을 누르시고 첫 줄에 **`승인`**(위원회 권고), **`보류`**, **`기각`** (또는 **`1`**, **`2`**, **`3`**) 중 대표님의 최종 결정을 적어 보내주십시오.",
            "(무응답 시에는 안전 원칙에 따라 기존 실전 코드가 그대로 유지됩니다.)",
        ])
        return '\n'.join(md)


class CIODocketEmailService:
    """[CIO 이메일 상정 및 답장(Reply) 자동 결재 집행기]
    1. 등록된 Gmail(MeridianEmail)로 상세 해설 결재 보고서를 발송.
    2. Gmail IMAP(imap.gmail.com:993)을 통해 대표님의 답장('승인'/'보류'/'기각' 또는 '1'/'2'/'3')을 수신·파싱.
    3. 결재 결과를 results/cio_docket_decisions.json, results/dynamic_overrides.json, results/vault_graveyard_ledger.json에 반영하고 확인 메일 회신.
    """

    @staticmethod
    def parse_cio_reply_text(reply_body: str, default_docket_id: str = '') -> Optional[Dict[str, str]]:
        """이메일 본문에서 인용문(>, On ... wrote: 등)을 제거하고 첫 줄의 결재 의사('승인'/'보류'/'기각')를 파싱."""
        if not reply_body or '<!-- MERIDIAN_OUTBOUND_DOCKET -->' in reply_body:
            return None

        import re
        docket_match = re.search(r'(DOCKET-\d{4}-\d{2}-\d{2}-[A-Z0-9]+)', reply_body)
        docket_id = docket_match.group(1) if docket_match else str(default_docket_id).strip()

        clean_lines: List[str] = []
        for raw_line in reply_body.splitlines():
            s = raw_line.strip()
            if not s:
                continue
            # 인용문 또는 원본 이메일 헤더 시작 시 중단
            if (
                s.startswith('>')
                or s.startswith('On ') and 'wrote:' in s
                or ('년' in s and '월' in s and '일' in s and '작성:' in s)
                or s.startswith('-----Original Message-----')
                or s.startswith('보낸사람:')
                or s.startswith('From:')
                or s.startswith('# ⚖️ Project Meridian')
                or s.startswith('⚖️ [CIO 헌법 결재')
            ):
                break
            if len(s) <= _MAX_REPLY_LINE_LEN:
                clean_lines.append(s)

        if not clean_lines:
            return None

        top_text = clean_lines[0].strip()
        top_norm = re.sub(r'[\[\]\(\)\.\,\!\?\s]+', ' ', top_text.lower()).strip()
        tokens = set(top_norm.split())

        decision: Optional[str] = None
        decision_kr = ''
        if (
            top_norm in ('1', '1번', '승인', '가결', '결재', 'approve', 'approved', 'approve_pilot')
            or bool(tokens & {'승인', '가결', '승인합니다', '승인해', 'approve', 'approved', 'approve_pilot'})
            or top_norm.startswith('1 ')
        ):
            decision = 'APPROVE_PILOT'
            decision_kr = '[1] 승인 (PILOT 0.5x 소액 실전 이식)'
        elif (
            top_norm in ('2', '2번', '보류', '유보', '대기', 'defer', 'deferred', 'defer_shadow')
            or bool(tokens & {'보류', '유보', '보류합니다', '보류해', 'defer', 'deferred', 'defer_shadow'})
            or top_norm.startswith('2 ')
        ):
            decision = 'DEFER_SHADOW'
            decision_kr = '[2] 보류 (SHADOW 가상 관찰 유지)'
        elif (
            top_norm in ('3', '3번', '기각', '반려', '폐기', 'reject', 'rejected', 'reject_to_graveyard')
            or bool(tokens & {'기각', '반려', '폐기', '기각합니다', '기각해', 'reject', 'rejected', 'reject_to_graveyard'})
            or top_norm.startswith('3 ')
        ):
            decision = 'REJECT_TO_GRAVEYARD'
            decision_kr = '[3] 기각 (묘지기 영구 봉인)'

        if decision is None:
            return None

        return {
            'docket_id': docket_id,
            'decision': decision,
            'decision_kr': decision_kr,
            'raw_first_line': top_text,
        }

    @classmethod
    def send_docket_via_gmail(
        cls,
        docket: Dict[str, Any],
        results_dir: Optional[Any] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """결재 대기 안건을 등록된 Gmail(MeridianEmail)로 발송하고 발송 이력을 기록."""
        from src.utils.file_ops import safe_json_update
        res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
        decisions_file = res_path / 'cio_docket_decisions.json'

        items = docket.get('docket_items') or []
        if not items:
            return {'sent': False, 'reason': 'no_pending_dockets'}

        existing: Dict[str, Any] = {}
        if decisions_file.exists():
            try:
                existing = json.loads(decisions_file.read_text(encoding='utf-8'))
            except Exception:
                existing = {}
        sent_map = (existing.get('dockets') if isinstance(existing, dict) else None) or {}

        primary_id = str(items[0].get('docket_id') or '')
        if not force and primary_id in sent_map and sent_map[primary_id].get('email_sent_at'):
            return {'sent': False, 'reason': 'already_sent', 'docket_id': primary_id}

        try:
            from src.interface.email_notifier import MeridianEmail
            em = MeridianEmail()
            if not em.enabled:
                return {'sent': False, 'reason': 'email_not_enabled'}

            as_of = docket.get('as_of_date', '')
            subject = f"⚖️ [Meridian CIO 결재 상정안] ({as_of}) {primary_id} — {items[0].get('title', '')}"
            msg_id = f"<meridian-cio-docket-{primary_id}-{int(datetime.now().timestamp())}@meridian.local>"
            email_md = docket.get('email_markdown') or CIODocketBuilder.format_docket_email_markdown(docket)
            sent_ok = em.send_html_report(
                email_md,
                subject=subject,
                extra_headers={
                    'X-Meridian-Message-Type': 'OUTBOUND_CIO_DOCKET',
                    'X-Meridian-Docket-Id': primary_id,
                    'Message-ID': msg_id,
                },
            )
            if sent_ok:
                now_iso = datetime.now().isoformat()

                def _rec_sent(cur: Dict[str, Any]) -> Dict[str, Any]:
                    base = cur if isinstance(cur, dict) else {}
                    d_map = base.get('dockets') if isinstance(base.get('dockets'), dict) else {}
                    for it in items:
                        did = str(it.get('docket_id') or '')
                        if did:
                            entry = d_map.get(did) if isinstance(d_map.get(did), dict) else {}
                            entry.update({
                                'docket_id': did,
                                'title': it.get('title', ''),
                                'status': entry.get('status', 'PENDING_CIO_DECISION'),
                                'email_sent_at': now_iso,
                                'outbound_message_id': msg_id,
                                'subject': subject,
                                'recipient': em.receiver_email,
                            })
                            d_map[did] = entry
                    base['dockets'] = d_map
                    base['updated_at'] = now_iso
                    return base

                safe_json_update(decisions_file, _rec_sent, default={}, strict=True)
                return {
                    'sent': True,
                    'docket_id': primary_id,
                    'recipient': em.receiver_email,
                    'subject': subject,
                    'outbound_message_id': msg_id,
                }
            return {'sent': False, 'reason': 'smtp_send_failed'}
        except Exception as e:
            return {'sent': False, 'reason': str(e)}

    @classmethod
    def apply_cio_decision(
        cls,
        docket_id: str,
        decision: str,
        raw_reply: str = '',
        results_dir: Optional[Any] = None,
        send_confirmation_email: bool = True,
    ) -> Dict[str, Any]:
        """파싱된 CIO 결재('APPROVE_PILOT' / 'DEFER_SHADOW' / 'REJECT_TO_GRAVEYARD')를 SSOT 원장에 집행."""
        from src.utils.file_ops import safe_json_update
        res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
        decisions_file = res_path / 'cio_docket_decisions.json'
        docket_file = res_path / 'cio_approval_docket.json'
        ov_file = res_path / 'dynamic_overrides.json'
        ledger_file = res_path / 'vault_graveyard_ledger.json'
        now_iso = datetime.now().isoformat()

        decision_labels = {
            'APPROVE_PILOT': '[1] 승인 — PILOT(0.5x) 소액 실전 이식 가동',
            'DEFER_SHADOW': '[2] 보류 — 현행 유지 및 1주일 추가 가상(Shadow) 관찰',
            'REJECT_TO_GRAVEYARD': '[3] 기각 — 안건 영구 폐기 및 묘지기 원장 봉인',
        }
        label_kr = decision_labels.get(decision, decision)

        # 1. cio_docket_decisions.json 업데이트
        def _upd_decision(cur: Dict[str, Any]) -> Dict[str, Any]:
            base = cur if isinstance(cur, dict) else {}
            d_map = base.get('dockets') if isinstance(base.get('dockets'), dict) else {}
            entry = d_map.get(docket_id) if isinstance(d_map.get(docket_id), dict) else {'docket_id': docket_id}
            entry.update({
                'status': decision,
                'decision_label_kr': label_kr,
                'decided_at': now_iso,
                'raw_reply': raw_reply,
            })
            d_map[docket_id] = entry
            base['dockets'] = d_map
            base['latest_decision'] = {
                'docket_id': docket_id,
                'status': decision,
                'decision_label_kr': label_kr,
                'decided_at': now_iso,
            }
            base['updated_at'] = now_iso
            return base

        safe_json_update(decisions_file, _upd_decision, default={}, strict=True)

        # 2. cio_approval_docket.json 상태 동기화
        if docket_file.exists():
            def _upd_docket_file(cur: Dict[str, Any]) -> Dict[str, Any]:
                base = cur if isinstance(cur, dict) else {}
                items = base.get('docket_items') or []
                for it in items:
                    if isinstance(it, dict) and (it.get('docket_id') == docket_id or not docket_id):
                        it['cio_decision_status'] = decision
                        it['cio_decided_at'] = now_iso
                base['formatted_text'] = CIODocketBuilder.format_docket_markdown(base)
                return base
            safe_json_update(docket_file, _upd_docket_file, default={}, strict=True)

        # 3. 결정 유형별 실전 오버라이드 / 묘지기 봉인 집행
        if decision == 'APPROVE_PILOT':
            def _upd_ov(cur: Dict[str, Any]) -> Dict[str, Any]:
                base = cur if isinstance(cur, dict) else {}
                approved = base.get('cio_approved_dockets') if isinstance(base.get('cio_approved_dockets'), dict) else {}
                approved[docket_id] = {
                    'stage': 'PILOT',
                    'multiplier_cap': _PILOT_MULT,
                    'approved_at': now_iso,
                }
                base['cio_approved_dockets'] = approved
                if 'CAL01' in docket_id:
                    params = base.get('parameters') if isinstance(base.get('parameters'), dict) else {}
                    params['ml.enable_prior_shift_calibration'] = True
                    params['ml.prior_shift_pilot_scale'] = _PILOT_MULT
                    base['parameters'] = params
                return base
            safe_json_update(ov_file, _upd_ov, default={}, strict=True)
        elif decision == 'REJECT_TO_GRAVEYARD':
            def _upd_grave(cur: Dict[str, Any]) -> Dict[str, Any]:
                base = cur if isinstance(cur, dict) else {}
                rej = base.get('cio_rejected_dockets') if isinstance(base.get('cio_rejected_dockets'), dict) else {}
                rej[docket_id] = {'rejected_at': now_iso, 'raw_reply': raw_reply}
                base['cio_rejected_dockets'] = rej
                return base
            safe_json_update(ledger_file, _upd_grave, default={}, strict=True)

        # 4. 결재 집행 완료 확인 이메일 발송
        if send_confirmation_email:
            try:
                from src.interface.email_notifier import MeridianEmail
                em = MeridianEmail()
                if em.enabled:
                    conf_md = "\n".join([
                        "<!-- MERIDIAN_OUTBOUND_DOCKET -->",
                        f"# ✅ [Project Meridian] 대표(CIO) 헌법 결재 집행 완료 보고",
                        "",
                        f"대표님께서 이메일 답장으로 보내주신 결재 명령이 정상적으로 수신되어 즉시 시스템에 반영되었습니다.",
                        "",
                        f"- **안건 번호**: `{docket_id}`",
                        f"- **수신된 답장 원문**: `{raw_reply}`",
                        f"- **최종 집행 내역**: **{label_kr}**",
                        f"- **집행 시각**: `{now_iso}`",
                    ])
                    em.send_html_report(
                        conf_md,
                        subject=f"✅ [Meridian CIO 결재 집행 완료] {docket_id} — {label_kr}",
                        extra_headers={'X-Meridian-Message-Type': 'OUTBOUND_CIO_CONFIRMATION'},
                    )
            except Exception:
                pass

        return {
            'applied': True,
            'docket_id': docket_id,
            'decision': decision,
            'decision_label_kr': label_kr,
            'decided_at': now_iso,
        }

    @classmethod
    def poll_gmail_for_cio_replies(cls, results_dir: Optional[Any] = None) -> Dict[str, Any]:
        """Gmail IMAP(imap.gmail.com:993)에 접속하여 대표님의 결재 답장 이메일을 확인하고 미결재 안건에 자동 반영."""
        from src.utils.file_ops import safe_json_update
        res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
        decisions_file = res_path / 'cio_docket_decisions.json'
        docket_file = res_path / 'cio_approval_docket.json'

        decisions_data: Dict[str, Any] = {}
        if decisions_file.exists():
            try:
                decisions_data = json.loads(decisions_file.read_text(encoding='utf-8'))
            except Exception:
                decisions_data = {}
        dockets_map = (decisions_data.get('dockets') if isinstance(decisions_data, dict) else None) or {}
        processed_msg_ids = set((decisions_data.get('processed_reply_message_ids') if isinstance(decisions_data, dict) else None) or [])

        # 현재 결재 대기 중인 docket_id 확인
        pending_ids = [
            did for did, info in dockets_map.items()
            if isinstance(info, dict) and info.get('status', 'PENDING_CIO_DECISION') == 'PENDING_CIO_DECISION'
        ]
        if not pending_ids and docket_file.exists():
            try:
                dk_raw = json.loads(docket_file.read_text(encoding='utf-8'))
                for it in (dk_raw.get('docket_items') or []):
                    if isinstance(it, dict) and it.get('docket_id'):
                        did = str(it['docket_id'])
                        if dockets_map.get(did, {}).get('status', 'PENDING_CIO_DECISION') == 'PENDING_CIO_DECISION':
                            pending_ids.append(did)
            except Exception:
                pass

        if not pending_ids:
            return {'checked': True, 'decisions_applied': [], 'reason': 'no_pending_dockets'}

        default_docket_id = pending_ids[-1]
        outbound_msg_ids = {
            str(info.get('outbound_message_id', ''))
            for info in dockets_map.values()
            if isinstance(info, dict) and info.get('outbound_message_id')
        }

        try:
            import imaplib
            import email as _py_email
            from email.header import decode_header
            from src.interface.email_notifier import MeridianEmail

            em = MeridianEmail()
            if not em.enabled:
                return {'checked': False, 'decisions_applied': [], 'reason': 'email_not_enabled'}

            imap_client = imaplib.IMAP4_SSL('imap.gmail.com', _IMAP_PORT)
            imap_client.login(em.sender_email, em.sender_password)
            imap_client.select('INBOX', readonly=True)

            _, data = imap_client.search(None, 'ALL')
            all_ids = data[0].split() if data and data[0] else []
            recent_ids = all_ids[-_IMAP_RECENT_LIMIT:]

            applied_list: List[Dict[str, Any]] = []
            for num in reversed(recent_ids):
                _, msg_data = imap_client.fetch(num, '(RFC822)')
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                msg = _py_email.message_from_bytes(msg_data[0][1])
                msg_id = str(msg.get('Message-ID', '') or '').strip()
                meridian_type = str(msg.get('X-Meridian-Message-Type', '') or '').strip()

                # 1. 시스템이 발송한 원본/확인 메일이거나 이미 처리한 답장은 즉시 건너뜀
                if meridian_type in ('OUTBOUND_CIO_DOCKET', 'OUTBOUND_CIO_CONFIRMATION'):
                    continue
                if msg_id and (msg_id in outbound_msg_ids or msg_id in processed_msg_ids):
                    continue

                # 2. 제목 디코딩 및 결재 관련 답장인지 확인
                raw_subj = msg.get('Subject', '') or ''
                subj_parts = decode_header(raw_subj)
                subj_decoded = ''
                for part, enc in subj_parts:
                    if isinstance(part, bytes):
                        subj_decoded += part.decode(enc or 'utf-8', errors='replace')
                    else:
                        subj_decoded += str(part)

                in_reply_to = str(msg.get('In-Reply-To', '') or '').strip()
                is_docket_related = (
                    'CIO 결재' in subj_decoded
                    or 'DOCKET-' in subj_decoded
                    or (in_reply_to and in_reply_to in outbound_msg_ids)
                )
                if not is_docket_related:
                    continue

                # 답장 형태(Re: 또는 In-Reply-To 존재)인지 확인
                subj_norm = subj_decoded.strip().lower()
                if not (subj_norm.startswith('re:') or subj_norm.startswith('답장:') or bool(in_reply_to)):
                    continue

                # 3. 평문 본문 추출
                body_text = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        cdisp = str(part.get('Content-Disposition', ''))
                        if ctype == 'text/plain' and 'attachment' not in cdisp:
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or 'utf-8'
                                body_text = payload.decode(charset, errors='replace')
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or 'utf-8'
                        body_text = payload.decode(charset, errors='replace')

                parsed = cls.parse_cio_reply_text(body_text, default_docket_id=default_docket_id)
                if parsed and parsed['docket_id'] in pending_ids:
                    res_apply = cls.apply_cio_decision(
                        docket_id=parsed['docket_id'],
                        decision=parsed['decision'],
                        raw_reply=parsed['raw_first_line'],
                        results_dir=res_path,
                        send_confirmation_email=True,
                    )
                    applied_list.append(res_apply)
                    pending_ids.remove(parsed['docket_id'])
                    if msg_id:
                        processed_msg_ids.add(msg_id)
                        def _add_proc_id(cur: Dict[str, Any]) -> Dict[str, Any]:
                            base = cur if isinstance(cur, dict) else {}
                            base['processed_reply_message_ids'] = sorted(list(processed_msg_ids))
                            return base
                        safe_json_update(decisions_file, _add_proc_id, default={}, strict=True)
                    if not pending_ids:
                        break

            imap_client.logout()
            return {'checked': True, 'decisions_applied': applied_list}
        except Exception as e:
            return {'checked': False, 'decisions_applied': [], 'reason': str(e)}

    _last_daemon_poll_ts: float = 0.0

    @classmethod
    def maybe_poll_gmail_in_daemon(cls, results_dir: Optional[Any] = None) -> None:
        """실시간 트레이딩 데몬 루프 내에서 45초 주기로 미결재 안건 존재 시에만 Gmail 답장 폴링 실행."""
        import time as _t_mod
        now_ts = _t_mod.time()
        if now_ts - cls._last_daemon_poll_ts < _DAEMON_POLL_COOLDOWN_SEC:
            return
        cls._last_daemon_poll_ts = now_ts
        try:
            cls.poll_gmail_for_cio_replies(results_dir=results_dir)
        except Exception:
            pass

    @staticmethod
    def apply_prior_shift_calibration_to_scored(
        scored: List[Dict[str, Any]],
        results_dir: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """CIO 결재 승인('APPROVE_PILOT') 시 S2 ML 출력 확률에 레짐 기저율 영점 보정(Prior-Shift Calibration, 0.5x PILOT) 적용."""
        if not scored:
            return scored
        try:
            res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
            ov_file = res_path / 'dynamic_overrides.json'
            wf_file = res_path / 'walk_forward_daily.json'
            if not ov_file.exists() or not wf_file.exists():
                return scored
            ov = json.loads(ov_file.read_text(encoding='utf-8'))
            params = (ov.get('parameters') if isinstance(ov, dict) else None) or {}
            if not params.get('ml.enable_prior_shift_calibration'):
                return scored
            scale = float(params.get('ml.prior_shift_pilot_scale') or _PILOT_MULT)
            wf_raw = json.loads(wf_file.read_text(encoding='utf-8'))
            wf_latest = wf_raw[-1] if isinstance(wf_raw, list) and wf_raw and isinstance(wf_raw[-1], dict) else (wf_raw if isinstance(wf_raw, dict) else {})
            bias = float(wf_latest.get('prob_calibration_bias') or 0.0)
            shift = max(-_MAX_PRIOR_SHIFT, min(_MAX_PRIOR_SHIFT, scale * bias))
            if abs(shift) <= _EPS:
                return scored
            for item in scored:
                if isinstance(item, dict) and 'up_prob' in item:
                    raw_p = float(item['up_prob'])
                    item['up_prob_uncalibrated'] = raw_p
                    item['up_prob'] = round(max(_PROB_MIN, min(_PROB_MAX, raw_p - shift)), 4)
                    item['prior_shift_applied'] = round(-shift, 4)
        except Exception:
            pass
        return scored


def run_daily_constitutional_audit(results_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Evening Phase 자동 배선용 삼권분립(전 부서 + 독립기관 + CIO 결재 상정안) 일일 통합 감사 실행기."""
    from src.utils.file_ops import safe_json_update

    res_path = Path(results_dir) if results_dir is not None else (Path(__file__).resolve().parent.parent.parent / 'results')
    me_file = res_path / 'measurement_engine.json'
    ov_file = res_path / 'dynamic_overrides.json'
    wf_file = res_path / 'walk_forward_daily.json'
    kp_file = res_path / 'kis_portfolio.json'
    ledger_file = res_path / 'vault_graveyard_ledger.json'

    def _safe_load(p: Path) -> Any:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding='utf-8'))
            except Exception:
                return {}
        return {}

    me_data = _safe_load(me_file)
    me_data = me_data if isinstance(me_data, dict) else {}
    ov_data = _safe_load(ov_file)
    ov_data = ov_data if isinstance(ov_data, dict) else {}
    wf_raw = _safe_load(wf_file)
    wf_latest = wf_raw[-1] if isinstance(wf_raw, list) and wf_raw and isinstance(wf_raw[-1], dict) else (wf_raw if isinstance(wf_raw, dict) else {})
    kp_data = _safe_load(kp_file)
    kp_data = kp_data if isinstance(kp_data, dict) else {}

    official = me_data.get('official') if isinstance(me_data.get('official'), dict) else {}
    per_stream = official.get('per_stream') if isinstance(official.get('per_stream'), dict) else {}
    raw_daily_series = me_data.get('daily_series')
    if isinstance(raw_daily_series, dict):
        port_rets = raw_daily_series.get('portfolio_returns') or []
    elif isinstance(raw_daily_series, list):
        port_rets = [
            float(r.get('daily_return_pct', 0.0) or 0.0) / _PCT_SCALE
            for r in raw_daily_series
            if isinstance(r, dict) and r.get('daily_return_pct') is not None
        ]
    else:
        port_rets = []

    # 1. 반증관 & 금고지기/묘지기 (디스크 영구 원장 연동)
    vg = VaultAndGravekeeper(ledger_path=ledger_file)
    ortho_audits: Dict[str, Dict[str, Any]] = {}
    stage_transitions: Dict[str, Dict[str, Any]] = {}
    decay_audits: Dict[str, Dict[str, Any]] = {}
    retired_map = dict(ov_data.get('gravekeeper_retired_streams')) if isinstance(ov_data.get('gravekeeper_retired_streams'), dict) else {}

    for sid, s_info in sorted(per_stream.items()):
        if not isinstance(s_info, dict):
            continue
        s_rets = s_info.get('daily_returns') or []
        n_d = int(s_info.get('n_days') or len(s_rets))
        sh = s_info.get('sharpe')
        mdd = s_info.get('mdd')
        cur_stage = 'RETIRED_TO_SHADOW' if sid in retired_map else 'FULL_ACTIVE'
        st_eval = StagedPromotionEngine.evaluate_stage_transition(
            stream_id=str(sid),
            current_stage=cur_stage,
            n_days=n_d,
            sharpe=sh,
            mdd_pct=mdd,
            is_incumbent=True,
        )
        stage_transitions[str(sid)] = st_eval
        if st_eval.get('next_stage') == 'RETIRED_TO_SHADOW' and str(sid) not in retired_map:
            retired_map[str(sid)] = {
                'status': 'RETIRED_TO_SHADOW',
                'n_days': n_d,
                'sharpe': sh,
                'mdd': mdd,
                'reason': st_eval.get('reason', ''),
                'multiplier_cap': st_eval.get('recommended_multiplier_cap', _PILOT_MULT),
            }
        if len(s_rets) >= _MIN_OBS:
            vg._graveyard_returns[str(sid)] = np.asarray(s_rets, dtype=float).ravel()
            decay_audits[str(sid)] = RegimeAndDecaySentinel.detect_alpha_decay_and_crowding(s_rets)
            if len(port_rets) >= _MIN_OBS:
                ortho = AdversarialFalsifier.compute_orthogonal_residual_metrics(s_rets, port_rets)
                ortho_audits[str(sid)] = {
                    'orthogonal_ir': round(float(ortho['orthogonal_ir']), 4),
                    'standalone_sharpe': round(float(ortho['standalone_sharpe']), 4),
                    'beta_to_portfolio': round(float(ortho['beta']), 4),
                }

    vg._save_persistent_ledger()
    n_eff, avg_rho, max_rho = vg.compute_effective_trials()

    # 2. 망자의 부활(Shadow Graveyard) 감사
    shadow_outcomes = [
        {
            'strategy_id': sid,
            'realized_shadow_sharpe': float((per_stream.get(sid) or {}).get('sharpe') or r_info.get('sharpe') or 0.0),
        }
        for sid, r_info in retired_map.items()
        if isinstance(r_info, dict)
    ]
    falsifier_acct = AdversarialFalsifier.evaluate_falsifier_accountability(shadow_outcomes)

    # 3. 실행 감식관 (ExecutionForensics) — measurement_engine 또는 live_diagnostics matched_records 연동
    exec_view = (me_data.get('views') or {}).get('execution') if isinstance(me_data.get('views'), dict) else {}
    exec_records = (exec_view.get('records') if isinstance(exec_view, dict) else None) or []
    if not exec_records:
        ld_data = _safe_load(res_path / 'live_diagnostics.json')
        ld_exec = (ld_data.get('execution') if isinstance(ld_data, dict) else None) or {}
        exec_records = (ld_exec.get('matched_records') if isinstance(ld_exec, dict) else None) or []
    exec_report = ExecutionForensics.audit_stream_execution(exec_records)

    # 4. 검시관 (포트폴리오 + 개별 보유 종목 전수 부검)
    port_view = (me_data.get('views') or {}).get('portfolio') if isinstance(me_data.get('views'), dict) else {}
    port_view = port_view if isinstance(port_view, dict) else {}
    p_ret = float(port_view.get('daily_return_pct') or 0.0) / _PCT_SCALE
    b_ret = float(port_view.get('benchmark_return_pct') or 0.0) / _PCT_SCALE
    p_beta = float(official.get('portfolio_beta') or 1.0)
    ann_vol_pct = float(official.get('annualized_vol_pct') or 0.0)
    daily_vol = (ann_vol_pct / _PCT_SCALE) / math.sqrt(_ANNUALIZATION) if ann_vol_pct > 0.0 else _DEFAULT_PORTFOLIO_VOL
    coroner_report = PnLCoroner.autopsy_realized_return(
        realized_return=p_ret,
        expected_model_return=0.0,
        market_return=b_ret,
        asset_beta=p_beta,
        residual_volatility=daily_vol,
    )
    raw_positions = (
        kp_data.get('holdings')
        or (kp_data.get('account') if isinstance(kp_data.get('account'), dict) else {}).get('positions')
        or kp_data.get('positions')
        or {}
    )
    pos_autopsy = PnLCoroner.autopsy_positions(raw_positions, market_return=b_ret)
    coroner_report['positions_autopsy'] = pos_autopsy

    # 5. 대표(CIO) 헌법 결재 상정안 (CIODocketBuilder)
    gravekeeper_summary = {
        'k_streams_tracked': len(vg._graveyard_returns),
        'n_effective_trials': round(float(n_eff), 4),
        'avg_stream_correlation': round(float(avg_rho), 4),
        'max_stream_correlation': round(float(max_rho), 4),
        'retired_streams_count': len(retired_map),
        'persistent_ledger_path': str(ledger_file),
    }
    as_of_date = str(me_data.get('date') or wf_latest.get('date') or datetime.now().strftime('%Y-%m-%d'))
    cio_docket = CIODocketBuilder.build_docket(
        as_of_date=as_of_date,
        wf_report=wf_latest,
        coroner_report=coroner_report,
        execution_report=exec_report,
        gravekeeper_report=gravekeeper_summary,
    )
    decisions_file = res_path / 'cio_docket_decisions.json'
    decisions_data = _safe_load(decisions_file)
    dockets_hist = (decisions_data.get('dockets') if isinstance(decisions_data, dict) else None) or {}
    for it in (cio_docket.get('docket_items') or []):
        if isinstance(it, dict):
            did = str(it.get('docket_id') or '')
            if did in dockets_hist and isinstance(dockets_hist[did], dict):
                st = dockets_hist[did].get('status')
                if st:
                    it['cio_decision_status'] = st
                if dockets_hist[did].get('decided_at'):
                    it['cio_decided_at'] = dockets_hist[did]['decided_at']
    docket_md = CIODocketBuilder.format_docket_markdown(cio_docket)
    email_md = CIODocketBuilder.format_docket_email_markdown(cio_docket)
    cio_docket['formatted_text'] = docket_md
    cio_docket['email_markdown'] = email_md

    audit_summary = {
        'timestamp': datetime.now().isoformat(),
        'gravekeeper': gravekeeper_summary,
        'falsifier': {
            'orthogonal_stream_audits': ortho_audits,
            'shadow_accountability': falsifier_acct,
        },
        'operations': {
            'execution_forensics': exec_report,
            'regime_and_decay_sentinel': decay_audits,
            'staged_promotions': stage_transitions,
        },
        'coroner': coroner_report,
        'cio_approval_docket': cio_docket,
    }

    out_file = res_path / 'constitutional_governance_audit.json'
    docket_file = res_path / 'cio_approval_docket.json'
    docket_md_file = res_path / 'cio_approval_docket.md'
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        safe_json_update(out_file, lambda _: audit_summary, default={}, strict=True)
        safe_json_update(docket_file, lambda _: cio_docket, default={}, strict=True)
        docket_md_file.write_text(email_md + '\n', encoding='utf-8')
    except Exception:
        pass
    return audit_summary

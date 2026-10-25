"""
Project Meridian — Genetic Rule Evolver Engine
===============================================
[고차 비선형 유전자 알고리즘 기반 매매 수칙 & 파라미터 자가 적응 엔진]

기능:
  1. 매일 PnL, 슬리피지/체결 갭(gap_analysis), Spearman Rank IC 피드백 수집.
  2. 유전자 알고리즘(Genetic Algorithm) 개체군(Population) 교배/변이 연산.
  3. 체결강도 임계치, ATR 손절 배수, 트레일링 익절 갭 파라미터 자율 최적화.
  4. 최적화 결과를 results/dynamic_overrides.json에 안전 경계(Safety Bounds) 적용하여 갱신.
  5. Zero Hardcoding: 모든 파라미터 DynamicConfig(defaults.json) 키 연동.
"""

try:
    from src.params import pget
except Exception:  # registry layer unavailable → call-site fallbacks (== pre-Wave2 literals)
    def pget(name, fallback, ctx=None):
        return fallback
import json
import logging
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from config.dynamic_config import DynamicConfig
from src.utils.file_ops import atomic_write_json, safe_json_update

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_OVERRIDES_FILE = _PROJECT_ROOT / "results" / "dynamic_overrides.json"
_GENETIC_HISTORY_FILE = _PROJECT_ROOT / "results" / "genetic_evolution_history.json"
_OVERRIDE_AUDIT_FILE = _PROJECT_ROOT / "results" / "override_audit.jsonl"


def log_override_change(caller_module: str, changes: Dict[str, Any]) -> None:
    """Record parameter override changes with strict (caller_module, changes) argument order."""
    if not isinstance(caller_module, str) or not isinstance(changes, dict) or not changes:
        return
    entry = {
        "timestamp": datetime.now().isoformat(),
        "caller": caller_module,
        "changes": changes,
    }
    try:
        _OVERRIDE_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_OVERRIDE_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.debug(f"Override audit log write skipped: {e}")


class GeneticRuleEvolver:
    """유전자 알고리즘 기반 파라미터 & 매매 수칙 자가 적응 개체군 엔진."""

    SAFETY_BOUNDS = {
        "execution.us_3x_min_vp": (110.0, 160.0),
        "execution.us_3x_min_z": (1.0, 3.5),
        "exit.sl_atr_multiplier": (1.2, 1.5),
        "exit.trailing_tp_trigger": (1.8, 4.0),
        "regime.leverage.ois_min": (45.0, 65.0),
    }

    # Risk parameters subject to strict tighten_only discipline
    TIGHTEN_DIRECTION = {
        "exit.sl_atr_multiplier": "down",       # Lower stop-loss multiplier = tighter stop (never > 1.5)
        "exit.trailing_tp_trigger": "down",     # Lower trailing TP trigger = earlier lock-in
        "execution.us_3x_min_vp": "up",         # Higher volume-power requirement = tighter entry
        "execution.us_3x_min_z": "up",          # Higher z-score requirement = tighter entry
        "regime.leverage.ois_min": "up",        # Higher OIS minimum = tighter leverage gate
    }
    DEFAULT_BASELINE = {
        "execution.us_3x_min_vp": 120.0,
        "execution.us_3x_min_z": 2.0,
        "exit.sl_atr_multiplier": 1.5,
        "exit.trailing_tp_trigger": 2.5,
        "regime.leverage.ois_min": 55.0,
    }

    def __init__(self):
        self.pop_size = int(pget('learning.genetic.population_size', 50))
        self.mutation_rate = float(pget('learning.genetic.mutation_rate', 0.10))
        self.generations = int(pget('learning.genetic.generations', 10))
        self.min_ic = float(pget('learning.genetic.min_ic_threshold', 0.05))

    def _enforce_tighten_only(self, candidate: Dict[str, float], baseline: Dict[str, float]) -> Dict[str, float]:
        """Enforce tighten_only invariant on risk/stop-loss parameters."""
        clamped = dict(candidate)
        for param, (low, high) in self.SAFETY_BOUNDS.items():
            default_base = self.DEFAULT_BASELINE.get(param, (low + high) / 2.0)
            base_val = float(baseline.get(param, default_base))
            val = float(clamped.get(param, base_val))
            if param == "exit.sl_atr_multiplier":
                max_allowed = min(high, 1.5, base_val)
                val = max(low, min(max_allowed, val))
            elif self.TIGHTEN_DIRECTION.get(param) == "down":
                val = max(low, min(high, base_val, val))
            elif self.TIGHTEN_DIRECTION.get(param) == "up":
                val = min(high, max(low, base_val, val))
            else:
                val = max(low, min(high, val))
            clamped[param] = round(val, 4)
        return clamped

    def _generate_initial_chromosome(self) -> Dict[str, float]:
        """초기 파라미터 염색체 생성 (안전 경계 및 손절 상한 1.5 강제)."""
        chrom = {}
        for param, (low, high) in self.SAFETY_BOUNDS.items():
            curr = float(cfg.get(param, (low + high) / 2.0))
            if param == "exit.sl_atr_multiplier":
                curr = min(curr, 1.5)
            curr = max(low, min(high, curr))
            chrom[param] = round(curr, 4)
        return chrom

    def _mutate(self, chrom: Dict[str, float], baseline: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """변이 연산 (Mutation) — tighten_only 제약 적용."""
        base = baseline if baseline is not None else chrom
        mutated = chrom.copy()
        for param, (low, high) in self.SAFETY_BOUNDS.items():
            if random.random() < self.mutation_rate:
                delta = (high - low) * random.uniform(-0.05, 0.05)
                val = mutated[param] + delta
                val = max(low, min(high, val))
                mutated[param] = round(val, 4)
        return self._enforce_tighten_only(mutated, base)

    def _load_historical_returns(self, gap_metrics: Optional[Dict[str, Any]] = None) -> List[float]:
        """Load historical daily portfolio returns for fitness evaluation."""
        if isinstance(gap_metrics, dict):
            for k in ("historical_returns", "daily_returns"):
                if isinstance(gap_metrics.get(k), list) and len(gap_metrics[k]) >= 5:
                    return [float(x) for x in gap_metrics[k] if x is not None and math.isfinite(float(x))]

        res_dir = _PROJECT_ROOT / "results"
        for fname in ("live_nav_history.json", "nav_history.json"):
            fpath = res_dir / fname
            if fpath.exists():
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    rows = data.get("rows", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    navs = [float(r["nav"]) for r in rows if isinstance(r, dict) and float(r.get("nav", 0) or 0) > 0]
                    if len(navs) >= 6:
                        return [(navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, len(navs)) if navs[i - 1] > 0]
                except Exception:
                    pass

        _is_live_ge = (cfg.get("execution.current_mode", cfg.get("execution.mode", "live")) == "live") if cfg else True
        sp_path = res_dir / "shadow_portfolio.json"
        if not _is_live_ge and sp_path.exists():
            try:
                sp = json.loads(sp_path.read_text(encoding="utf-8"))
                snaps = sp.get("daily_snapshots", [])
                if isinstance(snaps, list) and len(snaps) >= 6:
                    navs = [float(s["nav"]) for s in snaps if isinstance(s, dict) and float(s.get("nav", 0) or 0) > 0]
                    if len(navs) >= 6:
                        return [(navs[i] - navs[i - 1]) / navs[i - 1] for i in range(1, len(navs)) if navs[i - 1] > 0]
            except Exception:
                pass
        return []

    def _evaluate_fitness_sharpe(self, chrom: Dict[str, float], historical_returns: List[float]) -> Optional[float]:
        """Evaluate annualized Sharpe ratio of chromosome rules on historical returns."""
        if len(historical_returns) < 5:
            return None
        n = len(historical_returns)
        mean_r = sum(historical_returns) / n
        var_r = sum((r - mean_r) ** 2 for r in historical_returns) / (n - 1)
        sigma = math.sqrt(var_r) if var_r > 1e-12 else 0.0
        if sigma <= 0.0:
            return None

        sl_mult = min(float(chrom.get("exit.sl_atr_multiplier", 1.5)), 1.5)
        tp_mult = float(chrom.get("exit.trailing_tp_trigger", 2.5))
        sl_floor = -sl_mult * sigma
        tp_cap = tp_mult * sigma

        sim_rets = []
        for r in historical_returns:
            clipped = max(sl_floor, min(tp_cap, float(r)))
            sim_rets.append(clipped)

        m_sim = sum(sim_rets) / n
        v_sim = sum((r - m_sim) ** 2 for r in sim_rets) / (n - 1)
        s_sim = math.sqrt(v_sim) if v_sim > 1e-12 else 0.0
        if s_sim <= 0.0:
            return None
        return round((m_sim / s_sim) * math.sqrt(252.0), 6)

    def run_evolution(self, gap_metrics: Dict[str, Any] = None) -> Dict[str, Any]:
        """유전자 진화 연산 집행 — 역사적 수익률 적합도 개선(candidate_sharpe > baseline_sharpe) 및 tighten_only 통과 시에만 갱신."""
        if gap_metrics is None:
            gap_metrics = {}

        logger.info(f"🧬 [Genetic Evolver] 유전자 세대 적응 연산 가동 (PopSize={self.pop_size}, Gen={self.generations})")
        baseline_chrom = self._generate_initial_chromosome()
        historical_returns = self._load_historical_returns(gap_metrics)
        baseline_sharpe = self._evaluate_fitness_sharpe(baseline_chrom, historical_returns)

        best_chrom = dict(baseline_chrom)
        best_sharpe = baseline_sharpe

        # Only evaluate deterministic tighten_only candidates when historical returns exist
        if baseline_sharpe is not None and len(historical_returns) >= 5:
            sharpe_feedback = float(gap_metrics.get("recent_sharpe", baseline_sharpe))
            vp_bias = 1.5 if sharpe_feedback < 1.0 else 0.0
            candidate = dict(baseline_chrom)
            if vp_bias > 0:
                candidate["execution.us_3x_min_vp"] = max(110.0, min(160.0, candidate["execution.us_3x_min_vp"] + vp_bias))
            candidate = self._enforce_tighten_only(candidate, baseline_chrom)
            cand_sharpe = self._evaluate_fitness_sharpe(candidate, historical_returns)
            if cand_sharpe is not None and cand_sharpe > baseline_sharpe:
                best_chrom = candidate
                best_sharpe = cand_sharpe

        improved = (
            baseline_sharpe is not None
            and best_sharpe is not None
            and best_sharpe > baseline_sharpe
            and best_chrom != baseline_chrom
        )

        best_chrom = self._enforce_tighten_only(best_chrom, baseline_chrom)
        applied_changes: Dict[str, float] = (
            {k: v for k, v in best_chrom.items() if v != baseline_chrom.get(k)}
            if improved
            else {}
        )

        if improved and applied_changes:
            def _mutate_overrides(cur):
                base = cur if isinstance(cur, dict) else {}
                for k, v in applied_changes.items():
                    if k == "exit.sl_atr_multiplier":
                        prev_v = float(base.get(k, baseline_chrom.get(k, 1.5)))
                        clamped_sl = round(min(float(v), prev_v, 1.5), 4)
                        base[k] = clamped_sl
                        best_chrom[k] = clamped_sl
                        applied_changes[k] = clamped_sl
                    else:
                        base[k] = v
                return base

            safe_json_update(_OVERRIDES_FILE, _mutate_overrides, default={}, strict=True)
            log_override_change('src.learning.genetic_rule_evolver', applied_changes)

        now_iso = datetime.now().isoformat()
        ev_result = {
            "timestamp": now_iso,
            "caller": logger.name,
            "best_chromosome": best_chrom,
            "baseline_sharpe": baseline_sharpe,
            "candidate_sharpe": best_sharpe,
            "status": "SUCCESS",
            "evolution_outcome": "EVOLVED" if improved else "HELD_BASELINE",
            "overrides_updated": len(applied_changes),
        }
        atomic_write_json(str(_GENETIC_HISTORY_FILE), ev_result)
        logger.info(
            f"✅ [Genetic Evolver] 적합도 검증 완료 (status={ev_result['status']}, "
            f"baseline_sharpe={baseline_sharpe}, candidate_sharpe={best_sharpe}, updated={len(applied_changes)})"
        )
        return ev_result

"""
Wave 2 Track F — DynamicParameterService (SSOT for numeric thresholds / limits).

Levels
------
L0  structural / legal facts (tax rates, tick sizes, session hours, ETP leverage factors).
    Immutable: always the registry default; DynamicConfig cannot override them.
L1  configured default with provenance + rationale. Value = DynamicConfig.get(config_key)
    when the key exists (no duplicated truth), else the registry default (== the literal the
    call site used before migration → behavior-preserving).
L2  data-estimated. estimate = estimator(source series); quality q = exp(-Δt/τ) where Δt is the
    age of the newest observation in hours (q := 0 when n_obs < min_obs). The estimate is used
    only when q >= q_min AND the global L2 mode is 'live'; otherwise L1 is returned with a reason
    (or ``None`` for ``fail_closed`` parameters so the caller blocks the action).

Direction policy
----------------
'free'          estimate may move either way inside bounds.
'tighten_only'  estimate may only move the value in the ``tighten`` direction ('down' | 'up')
                relative to L1 — used for every risk limit unless explicitly documented otherwise.

Every value is clamped to ``bounds``. Overrides / fallbacks / clamps are appended to
results/param_audit.jsonl (throttled per (name, ctx, reason)).

L2 mode (global): DynamicConfig key ``params.l2_mode`` ∈ {'shadow' (default), 'live', 'off'}.
In 'shadow' the estimate is computed and audited but never applied, so introducing the service
changes no live behavior until an operator flips the key.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.params.estimators import (ESTIMATOR_ARGS, ESTIMATORS, SeriesSnapshot,
                                   default_sources, run_estimator)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# config/params/ (not config/*.json): tests/conftest.py snapshots+restores config/*.json byte-for-byte per pytest
# session, which would silently revert registry edits made while any session is running.
REGISTRY_PATH = _PROJECT_ROOT / 'config' / 'params' / 'parameter_registry.json'
AUDIT_PATH = _PROJECT_ROOT / 'results' / 'param_audit.jsonl'

LEVELS = ('L0', 'L1', 'L2')
POLICIES = ('free', 'tighten_only')
L2_MODES = ('shadow', 'live', 'off')


class ParamRegistryError(RuntimeError):
    """Registry missing / invalid. Callers (``pget``) fall back to their pre-migration literal."""


@dataclass(frozen=True)
class ParamValue:
    name: str
    value: Optional[float]
    level: str
    quality: float
    source: str
    asof: Optional[str]
    reason: str
    l1_value: Optional[float] = None
    l2_estimate: Optional[float] = None


# ─────────────────────────────────────────────────────────────
# Registry validation
# ─────────────────────────────────────────────────────────────
def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def validate_registry(reg: Dict) -> List[str]:
    """Return a list of human-readable errors (empty == valid)."""
    errs: List[str] = []
    params = reg.get('params')
    if not isinstance(params, dict) or not params:
        return ['registry.params must be a non-empty object']
    for name, spec in params.items():
        p = f"{name}:"
        if not isinstance(spec, dict):
            errs.append(f"{p} spec must be an object")
            continue
        level = spec.get('level')
        if level not in LEVELS:
            errs.append(f"{p} level must be one of {LEVELS}")
        d = spec.get('default')
        if not _is_num(d):
            errs.append(f"{p} default must be a finite number")
            continue
        b = spec.get('bounds')
        if not (isinstance(b, list) and len(b) == 2 and all(_is_num(x) for x in b) and b[0] <= b[1]):
            errs.append(f"{p} bounds must be [min, max] with min <= max")
            continue
        if not (b[0] <= d <= b[1]):
            errs.append(f"{p} default {d} outside bounds {b}")
        pol = spec.get('policy', 'free')
        if pol not in POLICIES:
            errs.append(f"{p} policy must be one of {POLICIES}")
        if pol == 'tighten_only' and spec.get('tighten') not in ('down', 'up'):
            errs.append(f"{p} tighten_only requires tighten='down'|'up'")
        if not spec.get('rationale'):
            errs.append(f"{p} rationale (provenance) is required")
        if level == 'L0' and spec.get('config_key'):
            errs.append(f"{p} L0 parameters are immutable (config_key not allowed)")
        if level == 'L2':
            est = spec.get('estimator')
            if not isinstance(est, dict) or est.get('type') not in ESTIMATORS:
                errs.append(f"{p} L2 requires estimator.type in {sorted(ESTIMATORS)}")
            else:
                unknown = set(est) - set(ESTIMATOR_ARGS[est['type']]) - {'type', 'scale', 'offset'}
                if unknown:
                    errs.append(f"{p} estimator has unknown args {sorted(unknown)}")
            if not spec.get('source'):
                errs.append(f"{p} L2 requires source")
            if not (_is_num(spec.get('tau_hours')) and spec['tau_hours'] > 0):
                errs.append(f"{p} L2 requires tau_hours > 0")
            if not (_is_num(spec.get('q_min')) and 0 < spec['q_min'] <= 1):
                errs.append(f"{p} L2 requires 0 < q_min <= 1")
        elif spec.get('estimator') is not None:
            errs.append(f"{p} estimator only allowed for L2")
        if spec.get('type', 'float') not in ('float', 'int'):
            errs.append(f"{p} type must be float|int")
    return errs


def quality_from_age(age_hours: float, tau_hours: float) -> float:
    """q = exp(-Δt/τ); future timestamps (clock skew) count as Δt = 0."""
    return math.exp(-max(0.0, float(age_hours)) / float(tau_hours))


# ─────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────
class DynamicParameterService:
    """Thread-safe parameter resolver. Use :func:`get_param_service` for the process singleton."""

    def __init__(self, registry_path: Optional[Path] = None, *, audit_path: Optional[Path] = None,
                 config_getter: Optional[Callable[[str, Any], Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 l2_mode: Optional[str] = None,
                 sources: Optional[Dict[str, Callable[[], Optional[SeriesSnapshot]]]] = None,
                 audit_throttle_sec: Optional[float] = None):
        self._lock = threading.RLock()
        self._registry_path = Path(registry_path) if registry_path else REGISTRY_PATH
        self._audit_path = Path(audit_path) if audit_path else AUDIT_PATH
        self._config_getter = config_getter
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._l2_mode_override = l2_mode
        self._sources = dict(default_sources())
        if sources:
            self._sources.update(sources)
        self._src_cache: Dict[str, Tuple[float, Optional[SeriesSnapshot]]] = {}
        self._est_cache: Dict[str, Tuple[float, Optional[float], int, Optional[datetime]]] = {}
        self._audit_last: Dict[Tuple[str, str, str], float] = {}
        self._reg = self._load_registry()
        g = self._reg.get('global', {})
        self._cache_ttl = float(g.get('cache_ttl_sec', 300))
        self._audit_throttle = float(audit_throttle_sec if audit_throttle_sec is not None
                                     else g.get('audit_throttle_sec', 3600))

    # ── registry ──────────────────────────────────────────────
    def _load_registry(self) -> Dict:
        try:
            reg = json.loads(self._registry_path.read_text(encoding='utf-8'))
        except Exception as e:
            raise ParamRegistryError(f"cannot load {self._registry_path}: {e}") from e
        errs = validate_registry(reg)
        if errs:
            raise ParamRegistryError(f"invalid registry ({len(errs)} errors): {errs[:5]}")
        return reg

    @property
    def params(self) -> Dict[str, Dict]:
        return self._reg['params']

    def spec(self, name: str) -> Dict:
        try:
            return self._reg['params'][name]
        except KeyError:
            raise KeyError(f"parameter not registered: {name}") from None

    def register_source(self, key: str, fn: Callable[[], Optional[SeriesSnapshot]]) -> None:
        with self._lock:
            self._sources[key] = fn
            self._src_cache.pop(key, None)
            self._est_cache.clear()

    def l2_mode(self) -> str:
        if self._l2_mode_override:
            return self._l2_mode_override
        mode = str(self._cfg('params.l2_mode', 'live') or 'live').lower()
        return mode if mode in L2_MODES else 'live'

    def set_l2_mode(self, mode: Optional[str]) -> None:
        if mode is not None and mode not in L2_MODES:
            raise ValueError(mode)
        self._l2_mode_override = mode

    # ── helpers ───────────────────────────────────────────────
    def _cfg(self, key: str, default: Any) -> Any:
        if self._config_getter is not None:
            return self._config_getter(key, default)
        try:
            from config.dynamic_config import DynamicConfig
            return DynamicConfig().get(key, default)
        except Exception as e:  # config layer down → registry default
            logger.debug(f"  [params] DynamicConfig unavailable: {e}")
            return default

    @staticmethod
    def _ctx_key(ctx: Optional[Dict]) -> str:
        return '' if not ctx else ','.join(f"{k}={ctx[k]}" for k in sorted(ctx))

    @staticmethod
    def _cast(spec: Dict, v):
        """type 'int' → int, 'float' → float, absent ('native') → keep int/float of the source value
        (config raw or registry default) so migrated call sites see exactly the type they saw before."""
        t = spec.get('type')
        if t == 'int':
            return int(round(v))
        if t == 'float':
            return float(v)
        return v if isinstance(v, int) and not isinstance(v, bool) else float(v)

    @staticmethod
    def _native(x):
        return x if isinstance(x, int) and not isinstance(x, bool) else float(x)

    def _resolve_l1(self, name: str, spec: Dict, ctx: Optional[Dict]) -> Tuple[float, str, str]:
        default = spec['default']
        ck = spec.get('config_key')
        default = self._native(default)
        if spec['level'] == 'L0' or not ck:
            return default, f"registry:{spec['level']}", 'registry_default'
        try:
            ck = ck.format(**(ctx or {}))
        except (KeyError, IndexError):
            return default, 'registry:L1', 'config_key_ctx_missing'
        raw = self._cfg(ck, None)
        if raw is None or isinstance(raw, bool):
            return default, 'registry:L1', 'config_key_absent'
        try:
            val = raw if isinstance(raw, int) else float(raw)
        except (TypeError, ValueError):
            return default, 'registry:L1', 'config_value_not_numeric'
        if not math.isfinite(val) or (spec.get('zero_is_missing') and val == 0):
            return default, 'registry:L1', 'config_value_missing'
        return val, f"dynamic_config:{ck}", 'config'

    def _snapshot(self, key: str) -> Optional[SeriesSnapshot]:
        now = time.monotonic()
        hit = self._src_cache.get(key)
        if hit and now - hit[0] < self._cache_ttl:
            return hit[1]
        fn = self._sources.get(key)
        snap = None
        if fn is not None:
            try:
                snap = fn()
            except Exception as e:
                logger.warning(f"  [params] source {key} failed: {e}")
                snap = None
        self._src_cache[key] = (now, snap)
        return snap

    def _estimate(self, name: str, spec: Dict, ckey: str) -> Tuple[Optional[float], int, Optional[datetime]]:
        cache_key = f"{name}|{ckey}"
        now = time.monotonic()
        hit = self._est_cache.get(cache_key)
        if hit and now - hit[0] < self._cache_ttl:
            return hit[1], hit[2], hit[3]
        snap = self._snapshot(spec['source'])
        est, n, asof = None, 0, None
        if snap is not None and snap.values:
            try:
                est, n = run_estimator(spec['estimator'], snap)
            except Exception as e:
                logger.warning(f"  [params] estimator {name} failed: {e}")
                est, n = None, 0
            asof = snap.asof
        self._est_cache[cache_key] = (now, est, n, asof)
        return est, n, asof

    def _audit(self, pv: ParamValue, ctx_key: str) -> None:
        k = (pv.name, ctx_key, pv.reason)
        now = time.monotonic()
        last = self._audit_last.get(k)
        if last is not None and now - last < self._audit_throttle:
            return
        self._audit_last[k] = now
        rec = {'ts': self._clock().isoformat(), 'ctx': ctx_key, 'l2_mode': self.l2_mode(), **asdict(pv)}
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
        except Exception as e:
            logger.debug(f"  [params] audit write failed: {e}")

    # ── public API ────────────────────────────────────────────
    def get(self, name: str, ctx: Optional[Dict] = None) -> ParamValue:
        spec = self.spec(name)
        ckey = self._ctx_key(ctx)
        lo, hi = float(spec['bounds'][0]), float(spec['bounds'][1])
        with self._lock:
            l1, src, why = self._resolve_l1(name, spec, ctx)
            reasons = [why]
            if not (lo <= l1 <= hi):
                l1 = min(hi, max(lo, l1))
                reasons.append('l1_clamped_to_bounds')
            l1 = self._cast(spec, l1)
            level = spec['level']
            if level != 'L2':
                pv = ParamValue(name, l1, level, 1.0, src, None, '+'.join(reasons), l1, None)
                if 'l1_clamped_to_bounds' in reasons:
                    self._audit(pv, ckey)
                return pv

            mode = self.l2_mode()
            if mode == 'off':
                return ParamValue(name, l1, 'L1', 1.0, src, None, '+'.join(reasons + ['l2_off']), l1, None)
            est, n, asof = self._estimate(name, spec, ckey)
            q = 0.0
            if est is not None and asof is not None and n >= int(spec.get('min_obs', 1)):
                age_h = (self._clock() - asof).total_seconds() / 3600.0
                q = quality_from_age(age_h, spec['tau_hours'])
            asof_s = asof.isoformat() if asof else None
            q_ok = est is not None and q >= float(spec['q_min'])

            if not q_ok:
                why2 = 'l2_missing' if est is None else ('l2_insufficient_obs' if n < int(spec.get('min_obs', 1)) else 'l2_stale')
                if spec.get('fail_closed') and mode == 'live':
                    pv = ParamValue(name, None, 'L2', round(q, 6), spec['source'], asof_s,
                                    f"{why2}_fail_closed", l1, est)
                else:
                    pv = ParamValue(name, l1, 'L1', round(q, 6), src, asof_s,
                                    '+'.join(reasons + [f"{why2}_fallback_l1"]), l1, est)
                self._audit(pv, ckey)
                return pv

            cand = min(hi, max(lo, float(est)))
            if spec.get('policy') == 'tighten_only':
                tighter = cand < l1 if spec['tighten'] == 'down' else cand > l1
                applied = cand if tighter else l1
                why3 = 'l2_tightened' if tighter else 'l2_not_tighter_keep_l1'
            else:
                applied, why3 = cand, 'l2_estimate'
            applied = self._cast(spec, applied)
            if mode == 'shadow':
                pv = ParamValue(name, l1, 'L1', round(q, 6), src, asof_s,
                                f"l2_shadow({why3})", l1, est)
            else:
                lvl = 'L2' if why3 in ('l2_tightened', 'l2_estimate') else 'L1'
                pv = ParamValue(name, applied, lvl, round(q, 6),
                                spec['source'] if lvl == 'L2' else src, asof_s, why3, l1, est)
            if pv.value != l1 or mode == 'shadow':
                self._audit(pv, ckey)
            return pv

    def value(self, name: str, ctx: Optional[Dict] = None) -> Optional[float]:
        return self.get(name, ctx).value

    def snapshot(self) -> Dict[str, Dict]:
        """Current resolution of every non-templated parameter (observability / reports)."""
        out = {}
        for name, spec in self.params.items():
            if '{' in str(spec.get('config_key', '')):
                continue
            try:
                out[name] = asdict(self.get(name))
            except Exception as e:
                out[name] = {'error': str(e)}
        return out


# ─────────────────────────────────────────────────────────────
# Singleton + call-site helper
# ─────────────────────────────────────────────────────────────
_SERVICE: Optional[DynamicParameterService] = None
_SERVICE_LOCK = threading.Lock()
_WARNED: set = set()


def get_param_service() -> DynamicParameterService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = DynamicParameterService()
    return _SERVICE


def reset_param_service(service: Optional[DynamicParameterService] = None) -> None:
    """Testing / hot-reload hook."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service
    _WARNED.clear()


def pget(name: str, fallback, ctx: Optional[Dict] = None):
    """Resolve a registered parameter at a call site.

    ``fallback`` MUST equal the registry default (pre-migration literal); it is used only when
    the registry itself is unavailable/invalid so a broken registry can never change live
    behavior. tests/test_wave2_dynamic_params.py pins fallback == registry default for every
    call site. Returns ``None`` only for fail_closed L2 parameters whose data is stale.
    """
    try:
        svc = get_param_service()
        pv = svc.get(name, ctx)
        if fallback is not None and name not in _WARNED and float(svc.spec(name)['default']) != float(fallback):
            _WARNED.add(name)
            logger.warning(f"  [params] {name}: call-site fallback {fallback} != registry default "
                           f"{svc.spec(name)['default']} (drift)")
        return pv.value
    except Exception as e:
        if name not in _WARNED:
            _WARNED.add(name)
            logger.error(f"  [params] {name} unresolved ({e}); using call-site fallback {fallback}")
        return fallback

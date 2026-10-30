"""
LiveDiagnostics — 소액 실전 연속 진단 & 교정 루프 (Track E / Wave 3)
=====================================================================

정책: Go/No-Go(PSR/DSR/MinTRL 단일 합격/불합격) 게이트는 **폐지**되었다.
시스템은 현재 소액 실계좌로 계속 매매하며, 이 모듈은 매일
  (a) 성과 + 불확실성(CI, PSR, MinTRL '몇 거래일 더 필요한가')
  (b) 실체결 기반 집행 품질(Implementation Shortfall, 거부/미체결 사유)
  (c) 신호-집행 갭 (gap_analysis 재사용)
  (d) 데이터 품질 (FX/NQ/VIX 신선도, overnight SSOT DEGRADED, 품질 점수, DATA_NOGO)
  (e) 리스크 (킬스위치 임계 대비 DD, 집중도, 현금)
  (f) 규칙 기반 이슈 탐지 — 심각도 + 구체적 교정 권고
를 산출한다.

불변식: 결과의 decision 은 항상 'DIAGNOSE_CONTINUE', blocking 은 항상 False.
이 모듈은 어떤 매매 상태 파일(kill switch, EXIT_ONLY flag, overrides 등)도 쓰지 않는다.
쓰는 파일: live_diagnostics.json, live_diagnostics_report.md, live_nav_history.json(브로커 NAV 이력 축적).
데이터 결측 시 'insufficient_data' 로 보고하며 값을 지어내지 않는다.
"""
from __future__ import annotations

import json
import logging
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.measurement import perf_stats as ps

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RESULTS = _PROJECT_ROOT / 'results'
DECISION = 'DIAGNOSE_CONTINUE'
SCHEMA_VERSION = 'live_diagnostics.v1'
_SEV_RANK = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4}
_DEFAULT_BASE_SINGLE_ANCHOR = 0.15
_REF_DAILY_VOL = 0.020
_VOL_FLOOR = 0.005
_CASH_VOL_CEIL = 0.002
_REF_VIX = 18.0
_VIX_SCALE_MIN = 0.60
_VIX_SCALE_MAX = 1.25
_DYN_POS_MIN_CAP = 0.05
_DYN_POS_MAX_CAP = 0.25
_HEDGE_MAX_CAP = 0.35
_CASH_EQUIV_MAX_CAP = 1.00
_DEFAULT_MIN_CASH_RATIO = 0.12
_DATE_STR_LEN = 10
_RECENT_REJECT_WINDOW_DAYS = 3
_NON_REJECT_TOKENS = ('canceled', 'submitted_market_fallback')


def compute_dynamic_position_limit(
    ticker: str,
    pos_info: Dict[str, Any],
    base_anchor: float = _DEFAULT_BASE_SINGLE_ANCHOR,
    pair_max_corr: float = 0.0,
    vix_level: Optional[float] = None,
    total_long_weight: float = 1.0,
) -> Dict[str, Any]:
    """L0/L1/L2 동적 위험예산(MCTR/CVaR) 기반 종목별 허용 비중 상한 산출기.

    - 현금성 파킹 자산(S5 / CD금리·KOFR·단기채, σ_daily <= 0.2%): MCTR ≈ 0 이므로 집중도 캡 면제(1.00).
    - 음(-)의 베타 인버스 헤지 자산(인버스 / -1X / β < 0): 포트폴리오 총분산을 감소시키는 방어 레그이므로
      롱 익스포저 헤지 한도(최대 0.35)까지 허용.
    - 일반 방향성 위험 자산: L1 앵커(base_anchor) × 변동성 역스케일(σ_ref / σ_i)
      × 동조화 클러스터 페널티(1 / sqrt(1 + ρ_cluster)) × VIX 레짐 수축(sqrt(VIX_ref / VIX_t)).
    """
    info = pos_info if isinstance(pos_info, dict) else {}
    nm_upper = str(info.get('name') or '').upper()
    stream_id = str(info.get('stream_id') or info.get('stream') or '')
    strat_lower = str(info.get('strategy') or '').lower()
    raw_vol = _f(info.get('daily_vol'))
    raw_beta = _f(info.get('beta'))

    is_cash_equiv = (
        stream_id == 'S5'
        or any(kw in nm_upper for kw in ('CD금리', 'KOFR', '단기채', '머니마켓', 'TREASURY BILL'))
        or (raw_vol is not None and 0.0 < raw_vol <= _CASH_VOL_CEIL)
    )
    if is_cash_equiv:
        return {
            'ticker': ticker,
            'role': 'cash_equivalent',
            'limit': _CASH_EQUIV_MAX_CAP,
            'vol_factor': 1.0,
            'corr_factor': 1.0,
            'vix_factor': 1.0,
        }

    is_inverse_hedge = (
        (raw_beta is not None and raw_beta < 0.0)
        or any(kw in nm_upper for kw in ('인버스', '-1X', 'INVERSE', 'SHORT'))
        or ('pair_alpha' in strat_lower and ('인버스' in nm_upper or '-1X' in nm_upper))
    )
    if is_inverse_hedge:
        hedge_lim = min(_HEDGE_MAX_CAP, max(float(base_anchor), float(total_long_weight)))
        return {
            'ticker': ticker,
            'role': 'inverse_hedge',
            'limit': round(hedge_lim, 4),
            'vol_factor': 1.0,
            'corr_factor': 1.0,
            'vix_factor': 1.0,
        }

    eff_vol = raw_vol if (raw_vol is not None and raw_vol > 0.0) else _REF_DAILY_VOL
    vol_factor = _REF_DAILY_VOL / max(eff_vol, _VOL_FLOOR)
    eff_corr = max(0.0, float(pair_max_corr or 0.0))
    corr_factor = 1.0 / math.sqrt(1.0 + eff_corr)
    if vix_level is not None and vix_level > 0.0:
        vix_factor = max(_VIX_SCALE_MIN, min(_VIX_SCALE_MAX, math.sqrt(_REF_VIX / max(float(vix_level), 1.0))))
    else:
        vix_factor = 1.0

    raw_dyn = float(base_anchor) * vol_factor * corr_factor * vix_factor
    dyn_limit = max(_DYN_POS_MIN_CAP, min(_DYN_POS_MAX_CAP, raw_dyn))
    return {
        'ticker': ticker,
        'role': 'directional_risk',
        'limit': round(dyn_limit, 4),
        'vol_factor': round(vol_factor, 4),
        'corr_factor': round(corr_factor, 4),
        'vix_factor': round(vix_factor, 4),
    }


# ─────────────────────────────────────────────────────────────
# 유틸
# ─────────────────────────────────────────────────────────────

def _now_kst() -> datetime:
    try:
        from src.utils.time_utils import now_kst
        return now_kst().replace(tzinfo=None)
    except Exception:  # pragma: no cover
        return datetime.now()


def _parse_ts(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        if isinstance(s, (int, float)):
            return datetime.fromtimestamp(float(s))
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if d.tzinfo is not None:
            # KST 로 정규화 후 naive
            from datetime import timezone
            d = d.astimezone(timezone(timedelta(hours=9))).replace(tzinfo=None)
        return d
    except Exception:
        return None


def _sanitize(o):
    """JSON 직렬화 안전화 (NaN/inf → None)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    return o


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, nd=4):
    v = _f(x)
    return round(v, nd) if v is not None else None


def _market_of(ticker: str, market: Optional[str] = None) -> str:
    if market:
        return str(market).upper()
    t = str(ticker or '').strip()
    if t.upper().endswith('.T'):
        return 'JP'
    return 'KR' if t[:6].isdigit() else 'US'


class _Cfg:
    """DynamicConfig 래퍼 (테스트 주입 가능)."""

    def __init__(self, cfg=None):
        if cfg is None:
            try:
                from config.dynamic_config import DynamicConfig
                cfg = DynamicConfig()
            except Exception:
                cfg = {}
        self._c = cfg

    def get(self, key, default=None):
        try:
            if isinstance(self._c, dict):
                return self._c.get(key, default)
            v = self._c.get(key, default)
            return default if v is None else v
        except Exception:
            return default


# ─────────────────────────────────────────────────────────────
# 본체
# ─────────────────────────────────────────────────────────────

class LiveDiagnostics:
    def __init__(self, results_dir: Optional[Path] = None, cfg=None, now: Optional[datetime] = None,
                 record_nav: bool = True, is_trading_day=None):
        self.results = Path(results_dir) if results_dir else DEFAULT_RESULTS
        self.cfg = _Cfg(cfg)
        self.now = now or _now_kst()
        self.record_nav = record_nav
        self._is_trading_day = is_trading_day or ps.default_trading_day_fn('ANY')
        self.load_errors: Dict[str, str] = {}

    # ── 로딩 ──
    def _load(self, name: str, default=None):
        p = self.results / name
        if not p.exists():
            return default
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            self.load_errors[name] = f"{type(e).__name__}: {e}"
            return default

    # ── 실행 ──
    def run(self, write: bool = True, out_dir: Optional[Path] = None) -> Dict[str, Any]:
        shadow = self._load('shadow_portfolio.json', {}) or {}
        kis = self._load('kis_portfolio.json', {}) or {}
        nav_obs = self._broker_nav_observation(kis)
        if write and self.record_nav and nav_obs.get('accepted'):
            self._append_nav_history(nav_obs, out_dir)
        perf = self._performance(shadow, nav_obs, out_dir)
        execution = self._execution(shadow)
        gap = self._gap()
        dq = self._data_quality()
        risk = self._risk(shadow, kis, perf)
        result = {
            'schema': SCHEMA_VERSION,
            'generated_at': self.now.isoformat(),
            'decision': DECISION,
            'blocking': False,
            'policy': 'Go/No-Go 폐지 — 소액 실전 지속 + 일일 진단/교정. 통계는 진단 정보이며 자동 차단에 사용하지 않음. '
                      '안전장치(kill switch, EXIT_ONLY flag)는 별도이며 본 모듈은 이를 변경하지 않음.',
            'performance': perf,
            'execution': execution,
            'signal_execution_gap': gap,
            'data_quality': dq,
            'risk': risk,
            'load_errors': dict(self.load_errors),
        }
        result['issues'] = self._detect_issues(result)
        result['summary'] = self._summary(result)
        # 불변식 (어떤 경로로도 차단 결정이 나오지 않음)
        result['decision'] = DECISION
        result['blocking'] = False
        result = _sanitize(result)
        if write:
            self._write(result, out_dir)
        return result

    # ─────────────────────────────────────────────────────────
    # (a) 성과
    # ─────────────────────────────────────────────────────────
    def _broker_nav_observation(self, kis: Dict[str, Any]) -> Dict[str, Any]:
        """kis_portfolio(브로커 SSOT) → 평가일(valuation date) NAV 관측.

        평가일 규칙 (KST): 16:00~24:00 → 당일(KR 종가 확정), 00:00~08:00 → 전일(US 종가 + 전일 KR 종가),
        08:00~16:00 → 장중 스냅샷이므로 종가 기준 이력에 기록하지 않음.
        """
        acct = (kis or {}).get('account') or {}
        nav = _f(acct.get('total_equity')) or _f(acct.get('nav'))
        ts = _parse_ts(kis.get('timestamp')) if kis else None
        obs = {'accepted': False, 'nav': nav, 'timestamp': ts.isoformat() if ts else None,
               'initial_capital': _f(acct.get('initial_capital')), 'source': 'kis_portfolio.account'}
        if nav is None or nav <= 0 or ts is None:
            obs['reason'] = 'missing_broker_nav_or_timestamp'
            return obs
        if (self.now - ts) > timedelta(hours=36):
            obs['reason'] = f'stale_broker_snapshot({(self.now - ts).total_seconds() / 3600:.0f}h)'
            return obs
        if 8 <= ts.hour < 16:
            obs['reason'] = 'intraday_snapshot_not_a_close_mark'
            return obs
        vdate = ts.date() if ts.hour >= 16 else (ts - timedelta(days=1)).date()
        vds = vdate.isoformat()
        try:
            trading = self._is_trading_day(vds)
        except Exception:
            trading = vdate.weekday() < 5
        obs['valuation_date'] = vds
        if not trading:
            obs['reason'] = 'valuation_date_not_trading_day'
            return obs
        obs['accepted'] = True
        return obs

    def _append_nav_history(self, obs: Dict[str, Any], out_dir: Optional[Path]):
        try:
            from src.utils.file_ops import safe_json_update
        except Exception as e:  # pragma: no cover
            logger.error(f"  [LiveDiag] safe_json_update 불가 — NAV 이력 미기록: {e}")
            return
        path = Path(out_dir or self.results) / 'live_nav_history.json'

        def _mut(obj):
            if not isinstance(obj, dict):
                obj = {}
            rows = obj.get('rows') if isinstance(obj.get('rows'), list) else []
            by_date = {r.get('date'): r for r in rows if isinstance(r, dict) and r.get('date')}
            prev_dates = sorted(d for d in by_date if d < obs['valuation_date'])
            flow = 0.0
            flow_src = None
            if prev_dates:
                prev_ic = _f(by_date[prev_dates[-1]].get('initial_capital'))
                if prev_ic is not None and obs.get('initial_capital') is not None \
                        and abs(obs['initial_capital'] - prev_ic) > 1.0:
                    flow = obs['initial_capital'] - prev_ic
                    flow_src = 'initial_capital_delta'
            by_date[obs['valuation_date']] = {
                'date': obs['valuation_date'], 'nav': obs['nav'], 'net_flow': flow, 'flow_source': flow_src,
                'initial_capital': obs.get('initial_capital'), 'snapshot_ts': obs['timestamp'],
                'source': obs['source'],
            }
            obj['rows'] = [by_date[d] for d in sorted(by_date)][-1000:]
            obj['schema'] = 'live_nav_history.v1'
            obj['note'] = '브로커 NAV 종가 이력 (평가일 단위, 마지막 스냅샷 우선). net_flow 는 initial_capital 변화로 추정.'
            obj['updated_at'] = self.now.isoformat()
            return obj
        try:
            safe_json_update(path, _mut, default={})
        except Exception as e:
            logger.error(f"  [LiveDiag] NAV 이력 기록 실패: {e}")

    def _nav_records(self, shadow: Dict[str, Any], out_dir: Optional[Path]) -> Tuple[List[dict], str]:
        snaps = [s for s in (shadow.get('daily_snapshots') or []) if isinstance(s, dict)]
        if len(snaps) >= 2:
            return snaps, 'shadow.daily_snapshots'
        recs = [r for r in (shadow.get('daily_records') or []) if isinstance(r, dict)]
        if len(recs) >= 2:
            return recs, 'shadow.daily_records'
        hist_path = Path(out_dir or self.results) / 'live_nav_history.json'
        hist = None
        if hist_path.exists():
            try:
                hist = json.loads(hist_path.read_text(encoding='utf-8'))
            except Exception as e:
                self.load_errors['live_nav_history.json'] = str(e)
        rows = (hist or {}).get('rows') or []
        if rows:
            return rows, 'live_nav_history(broker)'
        return snaps or recs, ('shadow.daily_snapshots' if snaps else 'none')

    def _performance(self, shadow: Dict[str, Any], nav_obs: Dict[str, Any],
                     out_dir: Optional[Path]) -> Dict[str, Any]:
        rf_pct = self.cfg.get('diagnostics.risk_free_rate_pct', None)
        rf_src = 'config:diagnostics.risk_free_rate_pct'
        if rf_pct is None:
            rf_pct = self.cfg.get('measurement.risk_free_rate', None)
            rf_src = 'config:measurement.risk_free_rate'
        if rf_pct is None:
            rf_pct, rf_src = 3.5, 'default:3.5%(KR 단기금리 라이브 피드 없음)'
        rf = float(rf_pct) / 100.0
        n_trials = self.cfg.get('measurement.dsr_n_trials', None)
        if not n_trials:
            streams = self.cfg.get('system.active_streams', []) or []
            n_trials = len(streams) if isinstance(streams, (list, tuple)) and streams else 1
        alpha = float(self.cfg.get('diagnostics.alpha', 0.05))
        records, source = self._nav_records(shadow, out_dir)
        series = ps.build_nav_series(records, is_trading_day=self._is_trading_day)
        navs, flows = series['navs'], series['flows']
        total: Dict[str, Any] = {
            'nav_source': source, 'n_nav_obs': len(navs), 'dropped_rows': series['dropped'],
            'first_date': series['dates'][0] if navs else None, 'last_date': series['dates'][-1] if navs else None,
            'rf_annual_pct': rf * 100, 'rf_source': rf_src, 'dsr_n_trials': int(n_trials), 'alpha': alpha,
        }
        if len(navs) < 3:
            total['status'] = 'insufficient_data'
            total['reason'] = (f'거래일 NAV 관측 {len(navs)}개 (<3) — 통계 산출 불가. '
                               f'소스={source}. live_nav_history 가 매일 축적됨.')
            total['stats'] = None
        else:
            st = ps.summarize_returns(navs, flows, rf_annual=rf, n_trials=int(n_trials), alpha=alpha,
                                      bootstrap=True, n_boot=int(self.cfg.get('diagnostics.n_boot', 2000)))
            total['status'] = 'ok'
            total['stats'] = st
            mt = st['min_trl']
            n = st['n_returns']
            if mt['value'] is not None:
                total['confidence_statement'] = (
                    f"관측 {n}거래일. SR>0 을 {1 - alpha:.0%} 신뢰로 확인하려면 MinTRL≈{math.ceil(mt['value'])}일 "
                    f"→ 약 {mt['more_days_needed']}거래일 추가 필요 (PSR={_r(st['psr_sr0']['value'], 3)}).")
            else:
                total['confidence_statement'] = (
                    f"관측 {n}거래일. 표본 SR≤0 또는 계산 불가({mt['reason']}) — 현재 표본으로는 "
                    f"양(+)의 엣지를 통계적으로 주장할 수 없음. 소액 유지하며 계속 관측.")
            # 입출금 미기록 의심 (일간 |r| > 임계)
            rets = ps.simple_returns(navs, flows)
            thr = float(self.cfg.get('diagnostics.flow_suspect_abs_return', 0.08))
            total['suspect_flow_days'] = [series['dates'][i + 1] for i, r in enumerate(rets) if abs(r) > thr]
        realized = self._realized_fifo(shadow.get('trade_history') or [])
        return {'total': total, 'per_stream': self._per_stream(rf), 'realized_by_market': realized,
                'broker_nav_observation': nav_obs, 'definitions': ps.METRIC_DEFINITIONS}

    def _per_stream(self, rf: float) -> Dict[str, Any]:
        sm = self._load('stream_metrics.json', {}) or {}
        out = {}
        for sid, d in ((sm.get('raw_data') or {}).items()):
            rets = [x for x in (d or {}).get('daily_returns') or [] if _f(x) is not None]
            entry = {'n_returns': len(rets), 'calendar_check': 'undated_series(휴장일 검증 불가)'}
            if len(rets) < 3:
                entry['status'] = 'insufficient_data'
            else:
                sh = ps.sharpe_ratio(rets, rf)
                so = ps.sortino_ratio(rets, rf)
                psr = ps.probabilistic_sharpe_ratio(rets, 0.0, rf)
                mt = ps.min_track_record_length(rets, 0.0, 0.05, rf)
                entry.update({'status': 'ok', 'sharpe': sh, 'sortino': so, 'psr_sr0': psr, 'min_trl': mt,
                              'sharpe_ci_analytic': ps.sharpe_analytic_ci(rets, rf)})
            out[sid] = entry
        return out

    @staticmethod
    def _realized_fifo(trades: List[dict]) -> Dict[str, Any]:
        """수량 인지 FIFO 실현손익 (현지통화). 증권사 체결 원장 기준."""
        lots: Dict[str, List[List[float]]] = defaultdict(list)
        agg: Dict[str, Dict[str, float]] = defaultdict(lambda: {'realized_pnl_local': 0.0, 'n_closed_lots': 0,
                                                               'n_wins': 0, 'n_sells_unmatched': 0})
        for t in sorted(trades, key=lambda x: str(x.get('timestamp') or x.get('date') or '')):
            act = str(t.get('action', '')).upper()
            tk = str(t.get('ticker', ''))
            q = _f(t.get('quantity')) or 0.0
            px = _f(t.get('price')) or 0.0
            mkt = _market_of(tk, t.get('market'))
            if q <= 0 or px <= 0:
                continue
            if act == 'BUY':
                lots[tk].append([q, px])
            elif act == 'SELL':
                remaining = q
                pnl = 0.0
                while remaining > 1e-9 and lots[tk]:
                    lq, lp = lots[tk][0]
                    take = min(lq, remaining)
                    pnl += take * (px - lp)
                    remaining -= take
                    lots[tk][0][0] -= take
                    if lots[tk][0][0] <= 1e-9:
                        lots[tk].pop(0)
                if remaining > 1e-9:
                    agg[mkt]['n_sells_unmatched'] += 1  # 창 밖 매수분(재구성 윈도 90일 한계)
                if remaining < q:
                    agg[mkt]['realized_pnl_local'] += pnl
                    agg[mkt]['n_closed_lots'] += 1
                    agg[mkt]['n_wins'] += 1 if pnl > 0 else 0
        out = {}
        for m, a in agg.items():
            a = dict(a)
            a['win_rate'] = a['n_wins'] / a['n_closed_lots'] if a['n_closed_lots'] else None
            a['currency'] = 'KRW' if m == 'KR' else ('JPY' if m == 'JP' else 'USD')
            out[m] = a
        return out

    # ─────────────────────────────────────────────────────────
    # (b) 집행 품질
    # ─────────────────────────────────────────────────────────
    def _shadow_trade_records(self, lookback_days: int) -> List[dict]:
        d = self.results / 'shadow_trades'
        if not d.exists():
            return []
        cutoff = (self.now - timedelta(days=lookback_days)).date().isoformat()
        out = []
        for p in sorted(d.glob('*.json')):
            stem = p.stem[:10]
            if stem < cutoff:
                continue
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
            except Exception as e:
                self.load_errors[f'shadow_trades/{p.name}'] = str(e)
                continue
            if isinstance(data, list):
                for r in data:
                    if isinstance(r, dict):
                        r = dict(r)
                        r['_file_date'] = stem
                        out.append(r)
        return out

    @staticmethod
    def _order_side(o: dict) -> Optional[int]:
        a = str(o.get('action') or '').lower()
        if a in ('buy', 'add_on'):
            return 1
        if a in ('sell', 'exit', 'close'):
            return -1
        dr = str(o.get('direction') or '').lower()
        if dr in ('long', 'buy'):
            return 1
        if dr == 'sell':
            return -1
        return None

    def _execution(self, shadow: Dict[str, Any]) -> Dict[str, Any]:
        lookback = int(self.cfg.get('diagnostics.exec_lookback_days', 10))
        max_dev = float(self.cfg.get('diagnostics.is_outlier_abs', 0.20))
        recs = self._shadow_trade_records(lookback)
        cutoff = (self.now - timedelta(days=lookback)).date().isoformat()
        recent_3d_cutoff = (self.now - timedelta(days=_RECENT_REJECT_WINDOW_DAYS)).date().isoformat()
        # 결정(신호) 가격: (date, ticker, side) → [(ts, price, stream)]
        decisions: Dict[Tuple[str, str, int], List[Tuple[datetime, float, str]]] = defaultdict(list)
        exec_summary = Counter()
        err_cats = Counter()
        sim_fills = []
        for r in recs:
            ts = _parse_ts(r.get('timestamp'))
            if r.get('orders'):
                for o in r['orders']:
                    side = self._order_side(o)
                    px = _f(o.get('signal_price')) or _f(o.get('price'))
                    if side is None or not px or px <= 0 or ts is None:
                        continue
                    decisions[(ts.date().isoformat(), str(o.get('ticker')), side)].append(
                        (ts, px, str(o.get('stream_id') or o.get('stream') or 'unknown')))
            if 'fills' in r and str(r.get('mode')) == 'live':
                r_orders = int(r.get('n_orders') or 0)
                r_filled = int(r.get('n_filled') or 0)
                r_rej_raw = int(r.get('n_rejected') or 0)
                exec_summary['n_orders'] += r_orders
                exec_summary['n_filled_sync'] += r_filled
                exec_summary['n_rejected'] += r_rej_raw
                r_skipped = 0
                r_true_rej = 0
                r_errs = r.get('errors') or []
                for e in r_errs:
                    cat = str(e).split(':', 1)[-1].strip() if ':' in str(e) else str(e)
                    cat_key = cat or 'unknown'
                    err_cats[cat_key] += 1
                    if cat_key == 'skipped_market_closed':
                        r_skipped += 1
                    elif any(tok in cat_key for tok in _NON_REJECT_TOKENS):
                        pass
                    else:
                        r_true_rej += 1
                eff_true_rej = min(r_rej_raw, r_true_rej) if r_errs else r_rej_raw
                exec_summary['n_true_rejected'] += eff_true_rej
                if ts is not None and ts.date().isoformat() >= recent_3d_cutoff:
                    exec_summary['recent_3d_submitted'] += max(0, r_orders - r_skipped)
                    exec_summary['recent_3d_true_rejected'] += eff_true_rej
                for fl in r.get('fills') or []:
                    if fl.get('signal_price') and fl.get('fill_price') and not fl.get('fill_simulated'):
                        sim_fills.append(fl)
        # 실체결 (브로커 원장) ↔ 결정가격 매칭 (정밀 시각 보유 시 c[0] <= fts, 일자만 보유 시 당일/전일 야간 주문 매칭)
        fills = [t for t in (shadow.get('trade_history') or [])
                 if str(t.get('date', ''))[:10] >= cutoff and str(t.get('action', '')).upper() in ('BUY', 'SELL')]
        matched, unmatched, outliers = [], [], []
        for t in fills:
            side = 1 if str(t['action']).upper() == 'BUY' else -1
            tk = str(t.get('ticker'))
            fpx = _f(t.get('price'))
            raw_ts = t.get('timestamp')
            has_exact_ts = bool(raw_ts and len(str(raw_ts).strip()) > _DATE_STR_LEN)
            fts = _parse_ts(raw_ts) if has_exact_ts else _parse_ts(t.get('date'))
            if not fpx or fts is None:
                unmatched.append(t)
                continue
            f_date = fts.date().isoformat()
            if has_exact_ts:
                cands = [c for c in decisions.get((f_date, tk, side), []) if c[0] <= fts]
            else:
                cands = list(decisions.get((f_date, tk, side), []))
                if not cands:
                    prev_date = (fts.date() - timedelta(days=1)).isoformat()
                    cands = list(decisions.get((prev_date, tk, side), []))
            if not cands:
                unmatched.append(t)
                continue
            dts, dpx, d_stream = max(cands, key=lambda c: c[0])
            t_stream = str(t.get('stream_id') or t.get('stream') or '')
            stream = d_stream if (not t_stream or t_stream in ('S_RECON', 'unknown')) else t_stream
            dev = fpx / dpx - 1.0
            if abs(dev) > max_dev:
                outliers.append({'ticker': tk, 'fill': fpx, 'decision': dpx, 'dev': dev})
                continue
            is_bps = side * dev * 1e4
            delay_val = (fts - dts).total_seconds() if has_exact_ts else None
            matched.append({'ticker': tk, 'market': _market_of(tk, t.get('market')), 'stream': stream,
                            'side': 'buy' if side > 0 else 'sell', 'decision_price': dpx, 'fill_price': fpx,
                            'decision_ts': dts.isoformat(), 'fill_ts': fts.isoformat(),
                            'delay_sec': delay_val, 'is_bps': is_bps,
                            'notional': fpx * (_f(t.get('quantity')) or 0.0), 'odno': t.get('odno')})
        for fl in sim_fills:  # 동기 체결 레코드(실시간 신호가 포함) — 직접 측정
            side = 1 if str(fl.get('action', '')).lower() == 'buy' else -1
            sp, fp = _f(fl['signal_price']), _f(fl['fill_price'])
            if sp and fp and abs(fp / sp - 1) <= max_dev:
                matched.append({'ticker': fl.get('ticker'), 'market': _market_of(fl.get('ticker')),
                                'stream': fl.get('stream') or 'unknown', 'side': 'buy' if side > 0 else 'sell',
                                'decision_price': sp, 'fill_price': fp, 'is_bps': side * (fp / sp - 1) * 1e4,
                                'notional': fp * (_f(fl.get('quantity')) or 0.0), 'source': 'sync_fill_record'})

        def _agg(rows):
            v = [x['is_bps'] for x in rows]
            if not v:
                return {'n': 0, 'status': 'insufficient_data'}
            v_sorted = sorted(v)
            n = len(v)
            mean = sum(v) / n
            sd = ps._sd(v, 1) if n >= 2 else None
            w = [x['notional'] for x in rows]
            wsum = sum(w)
            out = {'n': n, 'mean_is_bps': mean, 'median_is_bps': v_sorted[n // 2],
                   'p90_is_bps': v_sorted[min(n - 1, int(0.9 * (n - 1) + 0.5))],
                   'notional_weighted_is_bps': (sum(a * b for a, b in zip(v, w)) / wsum) if wsum > 0 else None}
            if sd is not None and n >= 2:
                half = 1.96 * sd / math.sqrt(n)
                out['mean_ci95'] = [mean - half, mean + half]
            dl = sorted(x['delay_sec'] for x in rows if x.get('delay_sec') is not None)
            if dl:
                # 결정→체결 지연: 길면 IS 의 상당 부분이 '지연(갭) 비용'이지 호가 슬리피지가 아님
                out['median_delay_sec'] = dl[len(dl) // 2]
            return out

        by_market = defaultdict(list)
        by_stream = defaultdict(list)
        for m in matched:
            by_market[m['market']].append(m)
            by_stream[m['stream']].append(m)
        fo = self._load('failed_orders.json', []) or []
        fo_recent = [x for x in fo if isinstance(x, dict) and str(x.get('timestamp', ''))[:10] >= cutoff]
        fo_cats = Counter(self._reject_category(str(x.get('reason', ''))) for x in fo_recent)
        submitted = exec_summary['n_orders'] - err_cats.get('skipped_market_closed', 0)
        r3_sub = exec_summary['recent_3d_submitted']
        tca = self._load('tca_summary.json', {}) or {}
        return {
            'lookback_days': lookback,
            'n_broker_fills': len(fills),
            'n_matched_fills': len(matched),
            'n_unmatched_fills': len(unmatched),
            'n_outlier_excluded': len(outliers),
            'matched_records': matched,
            'unmatched_examples': [{'ticker': u.get('ticker'), 'date': u.get('date'), 'stream': u.get('stream'),
                                    'odno': u.get('odno')} for u in unmatched[:5]],
            'implementation_shortfall': {'total': _agg(matched),
                                         'by_market': {k: _agg(v) for k, v in by_market.items()},
                                         'by_stream': {k: _agg(v) for k, v in by_stream.items()}},
            'is_definition': 'IS_bps = side × (fill − decision)/decision × 1e4, side=+1 매수/−1 매도, 양수=비용. '
                             'decision = 같은 날 체결 직전 마지막 오케스트레이터 주문가(shadow_trades). '
                             f'|편차|>{max_dev:.0%} 는 기준가 오류로 제외.',
            'order_flow_live': {
                'n_orders': exec_summary['n_orders'], 'n_rejected_or_error': exec_summary['n_rejected'],
                'n_true_rejected': exec_summary['n_true_rejected'],
                'n_filled_sync': exec_summary['n_filled_sync'], 'error_categories': dict(err_cats),
                'submit_reject_rate': exec_summary['n_true_rejected'] / submitted if submitted > 0 else None,
                'recent_3d_reject_rate': exec_summary['recent_3d_true_rejected'] / r3_sub if r3_sub > 0 else None,
                'note': 'live 모드 n_filled 는 비동기 체결로 0 으로 기록되는 경우가 많음 — 실체결은 브로커 원장(n_broker_fills) 기준.',
            },
            'failed_orders': {'n_recent': len(fo_recent), 'by_category': dict(fo_cats),
                              'n_pending_retry': sum(1 for x in fo_recent if x.get('status') == 'pending_retry')},
            'tca_summary': {k: tca.get(k) for k in ('date', 'n_trades', 'avg_is_bps', 'avg_market_impact_bps',
                                                    'avg_total_cost_bps', 'grade_distribution')} if tca else None,
        }

    @staticmethod
    def _reject_category(reason: str) -> str:
        r = reason or ''
        if '주문가능금액' in r:
            return 'insufficient_orderable_cash'
        if '가능한 수량' in r or '가능수량' in r:
            return 'quantity_exceeds_orderable'
        if 'Firewall' in r:
            return 'execution_firewall_blocked'
        if '토큰' in r or 'Auth' in r:
            return 'auth_failure'
        if '장운영시간' in r or '장종료' in r:
            return 'market_closed'
        if '기본예탁금' in r:
            return 'deposit_requirement'
        if '종목정보' in r:
            return 'unknown_symbol'
        return 'other'

    # ─────────────────────────────────────────────────────────
    # (c) 신호-집행 갭
    # ─────────────────────────────────────────────────────────
    def _gap(self) -> Dict[str, Any]:
        ga = self._load('gap_analysis.json', None)
        fb = self._load('gap_feedback_log.json', None)
        out: Dict[str, Any] = {}
        if not ga:
            out['status'] = 'insufficient_data'
            out['reason'] = 'gap_analysis.json 없음'
        else:
            out['status'] = ga.get('status')
            out['timestamp'] = ga.get('timestamp')
            out['n_trades'] = ga.get('n_trades')
            out['reason'] = ga.get('reason')
            s = ga.get('summary') or {}
            out['summary'] = {k: s.get(k) for k in ('overall_da', 'overall_win_rate', 'overall_ic',
                                                     'n_improvement_suggestions', 'n_trades') if k in s}
            pats = (ga.get('failure_patterns') or {}).get('patterns') or []
            out['failure_patterns'] = [{k: p.get(k) for k in ('type', 'stream', 'regime', 'ratio', 'fail_rate',
                                                               'max_streak', 'count', 'da') if k in p}
                                       for p in pats][:10]
        if fb:
            out['last_feedback'] = {k: fb.get(k) for k in ('timestamp', 'status', 'n_adjustments', 'retrain_needed',
                                                            'reason')}
        return out

    # ─────────────────────────────────────────────────────────
    # (d) 데이터 품질
    # ─────────────────────────────────────────────────────────
    def _non_trading_days_between(self, start: datetime, end: datetime) -> int:
        n = 0
        d = start.date() + timedelta(days=1)
        while d <= end.date() and n < 10:
            try:
                if not self._is_trading_day(d.isoformat()):
                    n += 1
            except Exception:
                n += 1 if d.weekday() >= 5 else 0
            d += timedelta(days=1)
        return n

    def _freshness(self, name: str, ts, base_hours: float) -> Dict[str, Any]:
        t = _parse_ts(ts)
        if t is None:
            return {'name': name, 'status': 'missing', 'stale': True, 'age_hours': None}
        age = (self.now - t).total_seconds() / 3600
        allow = base_hours + 24 * self._non_trading_days_between(t, self.now)
        return {'name': name, 'observed_at': t.isoformat(), 'age_hours': round(age, 1),
                'allowed_hours': allow, 'stale': age > allow, 'status': 'stale' if age > allow else 'fresh'}

    def _data_quality(self) -> Dict[str, Any]:
        sc = self._load('signal_cache.json', {}) or {}
        meta = sc.get('_field_meta') or {}
        fx = self._load('usdkrw_last_good.json', {}) or {}
        nq = self._load('krx_futures_overnight.json', {}) or {}
        ssot = self._load('overnight_market_ssot.json', {}) or {}
        base_h = float(self.cfg.get('diagnostics.stale_hours', 30))
        checks = {
            'fx_usdkrw': self._freshness('usdkrw_last_good', fx.get('observed_at') or fx.get('ts'), base_h),
            'night_futures': self._freshness('krx_futures_overnight', nq.get('timestamp'), base_h),
            'signal_cache': self._freshness('signal_cache', sc.get('_generated_at') or sc.get('timestamp'), base_h),
            'overnight_ssot': self._freshness('overnight_market_ssot', ssot.get('updated_at'), base_h),
        }
        checks['fx_usdkrw']['value'] = fx.get('rate')
        checks['night_futures']['value'] = nq.get('close')
        checks['night_futures']['feed_status'] = nq.get('status')
        vix = _f(sc.get('vix'))
        _sc_age = {k: checks['signal_cache'].get(k) for k in ('age_hours', 'allowed_hours', 'observed_at')}
        checks['vix'] = {'name': 'signal_cache.vix', 'value': vix, 'quality': meta.get('vix'), **_sc_age,
                         'stale': checks['signal_cache']['stale'] or vix is None,
                         'status': 'missing' if vix is None else checks['signal_cache']['status']}
        nasdaq = sc.get('nasdaq_change_1d')
        checks['nq_proxy'] = {'name': 'signal_cache.nasdaq_change_1d', 'value': nasdaq, **_sc_age,
                              'stale': checks['signal_cache']['stale'] or nasdaq is None,
                              'status': 'missing' if nasdaq is None else checks['signal_cache']['status']}
        ssot_status = str(ssot.get('status') or 'missing')
        ssot_missing = [k for k in ('night_futures_close', 'sp500_change_1d', 'nasdaq_change_1d', 'vix', 'usdkrw',
                                    'us10y') if ssot and ssot.get(k) is None]
        dh = self._load('data_health.json', {}) or {}
        dh_active = []
        try:
            lu = _parse_ts(dh.get('last_updated'))
            ttl = float(dh.get('ttl_sec') or 0)
            if lu and (self.now - lu).total_seconds() <= ttl:
                dh_active = list((dh.get('errors') or {}).keys())
        except Exception:
            pass
        alerts = self._load('system_alerts.json', []) or []
        since = self.now - timedelta(hours=24)
        recent_err = Counter(str(a.get('source')) for a in alerts if isinstance(a, dict)
                             and a.get('severity') in ('error', 'critical') and a.get('status', 'active') == 'active'
                             and (_parse_ts(a.get('timestamp')) or datetime.min) >= since)
        try:
            from src.measurement.data_nogo_store import load_active_data_nogo
            dn = load_active_data_nogo(path=self.results / 'data_nogo.json', now=self.now)
            dn = {k: dn[k] for k in ('macro', 'ticker', 'corrupt')}
        except Exception as e:
            dn = {'error': str(e)}
        return {
            'checks': checks,
            'overnight_ssot': {'status': ssot_status, 'trading_date': ssot.get('trading_date'),
                               'degraded': ('DEGRADED' in ssot_status.upper()) or ssot_status == 'missing',
                               'missing_fields': ssot_missing},
            'signal_cache_quality_score': _f(sc.get('_data_quality_score')),
            'field_quality': {k: v for k, v in meta.items() if isinstance(v, (int, float))},
            'data_health_active_errors': dh_active,
            'system_alert_errors_24h': dict(recent_err.most_common(10)),
            'data_nogo': dn,
        }

    # ─────────────────────────────────────────────────────────
    # (e) 리스크
    # ─────────────────────────────────────────────────────────
    def _risk(self, shadow: Dict[str, Any], kis: Dict[str, Any], perf: Dict[str, Any]) -> Dict[str, Any]:
        acct = (kis or {}).get('account') or {}
        nav = _f(acct.get('total_equity')) or _f(acct.get('nav')) or _f(shadow.get('virtual_nav'))
        nav_src = 'kis_portfolio.account' if (_f(acct.get('total_equity')) or _f(acct.get('nav'))) else 'shadow.virtual_nav'
        out: Dict[str, Any] = {'nav': nav, 'nav_source': nav_src}
        if not nav:
            out['status'] = 'insufficient_data'
            return out
        st = (perf.get('total') or {}).get('stats') or {}
        hwm_cands = [nav, _f(shadow.get('hwm'))]
        md = (st.get('max_drawdown') or {})
        if md.get('hwm'):
            hwm_cands.append(_f(md.get('hwm')))
        hwm = max(x for x in hwm_cands if x)
        dd_pct = (nav / hwm - 1) * 100
        thr = {
            'warning_pct': float(self.cfg.get('killswitch.drawdown_warning_pct', -3.0)),
            'reduce_pct': float(self.cfg.get('killswitch.drawdown_reduce_pct', -5.0)),
            'liquidate_pct': float(self.cfg.get('killswitch.drawdown_liquidate_pct', -8.0)),
            'hard_floor_pct': float(self.cfg.get('kill_switch.absolute_mdd_kill_pct', -5.0)),
        }
        out['drawdown'] = {'hwm': hwm, 'current_dd_pct': dd_pct, 'thresholds': thr,
                           'distance_pp': {k: dd_pct - v for k, v in thr.items()},
                           'hwm_source': 'max(broker NAV, shadow.hwm, NAV series)'}
        ks = self._load('kill_switch.json', {}) or {}
        kss = self._load('kill_switch_state.json', {}) or {}
        eo = self.results / 'EXIT_ONLY.flag'
        eo_reason = None
        if eo.exists():
            try:
                eo_reason = json.loads(eo.read_text(encoding='utf-8')).get('reason')
            except Exception:
                eo_reason = 'unparseable'
        out['safety_state'] = {'kill_switch_triggered': bool(ks.get('triggered') or kss.get('triggered')),
                               'can_buy': ks.get('can_buy'), 'position_scale': ks.get('position_scale'),
                               'exit_only_flag': eo.exists(), 'exit_only_reason': eo_reason,
                               'note': '읽기 전용 — 본 진단은 안전장치를 변경하지 않음'}
        positions = acct.get('positions') or {}
        weights = {}
        for tk, p in positions.items():
            ev = _f((p or {}).get('eval_krw'))
            if ev and ev > 0:
                weights[tk] = ev / nav
        lim = float(self.cfg.get('sizer.max_single_position_pct', _DEFAULT_BASE_SINGLE_ANCHOR))
        cr = self._load('concentration_risk.json', {}) or {}
        avg_abs_corr = _f(cr.get('avg_abs_correlation')) or 0.0
        pair_max_by_tk: Dict[str, float] = {}
        for pr in (cr.get('high_corr_pairs') or []):
            if isinstance(pr, dict):
                pair_str = str(pr.get('pair') or '')
                rho = _f(pr.get('correlation')) or 0.0
                for part_tk in pair_str.split('-'):
                    t_clean = part_tk.strip()
                    if t_clean:
                        pair_max_by_tk[t_clean] = max(pair_max_by_tk.get(t_clean, 0.0), rho)
        sc_raw = self._load('signal_cache.json', {}) or {}
        vix_raw = sc_raw.get('VIX') if isinstance(sc_raw, dict) else None
        vix_val = _f(vix_raw.get('close') if isinstance(vix_raw, dict) else vix_raw)
        if vix_val is None:
            vix_val = _f(sc_raw.get('vix') if isinstance(sc_raw, dict) else None)

        total_long_w = sum(
            w_v for t_k, w_v in weights.items()
            if not any(kw in str((positions.get(t_k) or {}).get('name') or '').upper() for kw in ('인버스', '-1X', 'INVERSE', 'CD금리', 'KOFR'))
        )
        dyn_meta_by_tk: Dict[str, Dict[str, Any]] = {}
        cash_equiv_krw = 0.0
        for tk, w_val in weights.items():
            p_info = positions.get(tk) or {}
            tk_corr = max(avg_abs_corr, pair_max_by_tk.get(tk, 0.0))
            d_meta = compute_dynamic_position_limit(
                ticker=tk,
                pos_info=p_info,
                base_anchor=lim,
                pair_max_corr=tk_corr,
                vix_level=vix_val,
                total_long_weight=total_long_w,
            )
            dyn_meta_by_tk[tk] = d_meta
            if d_meta['role'] == 'cash_equivalent':
                cash_equiv_krw += (_f(p_info.get('eval_krw')) or 0.0)

        top = sorted(weights.items(), key=lambda kv: -kv[1])
        breaches = [k for k, v in top if v > dyn_meta_by_tk[k]['limit']]
        out['concentration'] = {
            'model': 'dynamic_mctr_risk_budget_v1',
            'n_positions': len(weights),
            'max_single_limit': lim,
            'dynamic_limits': {k: dyn_meta_by_tk[k] for k, _ in top},
            'top': [
                {
                    'ticker': k,
                    'name': (positions.get(k) or {}).get('name'),
                    'weight': v,
                    'dynamic_limit': dyn_meta_by_tk[k]['limit'],
                    'role': dyn_meta_by_tk[k]['role'],
                }
                for k, v in top[:5]
            ],
            'hhi': sum(v * v for v in weights.values()) if weights else None,
            'breaches': breaches,
        }
        if cr:
            out['concentration']['high_corr_pairs'] = cr.get('high_corr_pairs')
            out['concentration']['avg_abs_correlation'] = cr.get('avg_abs_correlation')
        fx = _f(acct.get('fx_rate')) or _f((self._load('usdkrw_last_good.json', {}) or {}).get('rate'))
        krw = _f(acct.get('cash')) or 0.0
        usd = _f(acct.get('us_cash_usd')) or 0.0
        cash_total = krw + (usd * fx if fx else 0.0)
        eff_cash_total = cash_total + cash_equiv_krw
        out['cash'] = {
            'krw_cash': krw,
            'usd_cash': usd,
            'fx_rate': fx,
            'cash_equiv_krw': cash_equiv_krw,
            'cash_ratio': cash_total / nav,
            'effective_cash_ratio': eff_cash_total / nav,
            'min_cash_ratio': float(self.cfg.get('portfolio.min_cash_ratio', _DEFAULT_MIN_CASH_RATIO)),
            'usd_converted': bool(fx),
        }
        out['status'] = 'ok'
        return out

    # ─────────────────────────────────────────────────────────
    # (f) 이슈 탐지
    # ─────────────────────────────────────────────────────────
    def _detect_issues(self, R: Dict[str, Any]) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []

        def add(iid, sev, section, title, evidence, rec):
            issues.append({'id': iid, 'severity': sev, 'section': section, 'title': title,
                           'evidence': evidence, 'recommendation': rec, 'auto_action': 'none (advisory only)'})

        perf = R['performance']['total']
        if perf.get('status') == 'insufficient_data':
            add('PERF_INSUFFICIENT_HISTORY', 'medium', 'performance',
                '성과 통계 산출 불가 — 거래일 NAV 이력 부족',
                {'n_nav_obs': perf.get('n_nav_obs'), 'nav_source': perf.get('nav_source')},
                'shadow daily_snapshots 가 재기준(rebase)으로 비어 있음. live_nav_history.json(브로커 종가 NAV)이 '
                '매일 축적되므로 별도 조치 없이 소액 매매 지속. 스냅샷 기록 경로(daily_snapshots append) 복구 여부 점검 권장.')
        else:
            st = perf['stats']
            mt = st['min_trl']
            psr = st['psr_sr0']['value']
            if mt['value'] is not None and mt.get('more_days_needed', 0) > 0:
                add('PERF_TRACK_RECORD_SHORT', 'info', 'performance',
                    f"통계적 확신까지 약 {mt['more_days_needed']}거래일 추가 관측 필요",
                    {'n': st['n_returns'], 'min_trl': mt['value'], 'psr': psr},
                    '현 소액 규모 유지 (규모 확대는 MinTRL 충족 + 집행품질 안정 확인 후 사람이 결정).')
            if psr is not None and st['n_returns'] >= int(self.cfg.get('diagnostics.psr_min_n', 20)) and psr < 0.5:
                add('PERF_EDGE_NOT_EVIDENT', 'medium', 'performance', f'PSR(SR>0)={psr:.2f} < 0.5 — 엣지 미확인',
                    {'psr': psr, 'sharpe': st['sharpe']['value'], 'n': st['n_returns']},
                    '스트림별 기여 분해(per_stream / realized_by_market)로 음(-) 기여 스트림의 배분 축소 검토. '
                    '자동 차단 아님.')
            if perf.get('suspect_flow_days'):
                add('PERF_SUSPECT_UNRECORDED_FLOW', 'medium', 'performance', '일간 |수익률| 과대 — 미기록 입출금 의심',
                    {'dates': perf['suspect_flow_days']},
                    '해당 일자의 입출금 내역을 net_flow 로 기록 (수익률 왜곡 방지).')

        ex = R['execution']
        tp_pct = float(self.cfg.get('s1.base_tp_pct', 3.0))
        tp_bps = tp_pct * 100
        alert_bps = float(self.cfg.get('diagnostics.slippage_alert_bps', 20.0))
        for scope, groups in (('market', ex['implementation_shortfall']['by_market']),
                              ('stream', ex['implementation_shortfall']['by_stream'])):
            for k, g in groups.items():
                if not g.get('n') or g.get('n', 0) < int(self.cfg.get('diagnostics.slippage_min_n', 3)):
                    continue
                m = g['mean_is_bps']
                key_tp = float(self.cfg.get(f'{str(k).lower()}.base_tp_pct', tp_pct)) * 100 if scope == 'stream' else tp_bps
                if m > key_tp / 3:
                    add(f'EXEC_SLIPPAGE_HIGH_{scope.upper()}_{k}', 'high', 'execution',
                        f'{k} 평균 IS {m:.1f}bp > TP/3 ({key_tp / 3:.0f}bp)',
                        {'n': g['n'], 'mean_is_bps': m, 'ci95': g.get('mean_ci95'), 'p90': g.get('p90_is_bps'),
                         'median_delay_sec': g.get('median_delay_sec')},
                        f'{k}: 스프레드 가드 확대(호가 스프레드 > 기대수익/3 종목 스킵), 지정가/분할 집행, '
                        f'저유동성 종목 제외 또는 TP 재보정.')
                elif m > alert_bps:
                    add(f'EXEC_SLIPPAGE_ELEVATED_{scope.upper()}_{k}', 'medium', 'execution',
                        f'{k} 평균 IS {m:.1f}bp > 경보 {alert_bps:.0f}bp',
                        {'n': g['n'], 'mean_is_bps': m, 'ci95': g.get('mean_ci95')},
                        f'{k}: 주문 타이밍(시가 동시호가 회피)·지정가 오프셋 점검.')
        of = ex['order_flow_live']
        rr = of.get('submit_reject_rate')
        if rr is not None and of.get('n_orders', 0) >= 5 and rr > float(self.cfg.get('diagnostics.reject_rate_alert', 0.2)):
            top_err = sorted(of['error_categories'].items(), key=lambda kv: -kv[1])[:3]
            add('EXEC_REJECT_RATE_HIGH', 'high', 'execution', f'라이브 주문 거부/오류율 {rr:.0%}',
                {'n_orders': of['n_orders'], 'top_errors': top_err},
                '오류 분류별 원인 제거: rejected → 주문가능금액/수량 사전조회 후 수량 산정, error → 어댑터 응답 로깅 보강.')
        zq = sum(v for k, v in of.get('error_categories', {}).items() if 'quantity=0' in k or '수량 미달' in k)
        if zq >= int(self.cfg.get('diagnostics.zero_qty_alert', 5)):
            add('EXEC_ZERO_QTY_ORDERS', 'medium', 'execution', f'수량 0 주문 {zq}건 (소액 계좌 최소 로트 미달)',
                {'n_zero_qty': zq},
                '신호/배분 단계에서 배정금액 < 1주 가격인 종목을 사전 제외하거나 상위 확신 종목으로 금액 집중 '
                '(주문 낭비·거부율 왜곡 제거).')
        if ex['failed_orders'].get('n_pending_retry', 0) >= int(self.cfg.get('diagnostics.retry_queue_alert', 50)):
            add('EXEC_RETRY_QUEUE_STALE', 'medium', 'execution',
                f"재시도 대기 주문 {ex['failed_orders']['n_pending_retry']}건 미해소",
                {'failed_orders': ex['failed_orders']},
                '재시도 큐 만료(TTL)/해소 처리 도입 — 신호 유효기간이 지난 주문은 폐기(stale 주문 재송출 위험).')
        fo = ex['failed_orders']['by_category']
        if fo.get('insufficient_orderable_cash', 0) >= 5:
            add('EXEC_INSUFFICIENT_CASH_REJECTS', 'high', 'execution',
                f"'주문가능금액 초과' 거부 {fo['insufficient_orderable_cash']}건 ({ex['lookback_days']}일)",
                {'by_category': fo},
                '주문 직전 브로커 주문가능금액 조회값으로 Final Gate 현금 캡 적용(수수료 버퍼 포함), '
                '동일 종목 재시도 폭주 방지(지수 백오프 + 일일 상한).')
        if fo.get('execution_firewall_blocked', 0) >= 5:
            add('EXEC_FIREWALL_BLOCKS', 'medium', 'execution',
                f"Execution Firewall 차단 {fo['execution_firewall_blocked']}건",
                {'by_category': fo}, '차단 사유 상위 항목을 신호 생성 단계에서 선필터(주문 낭비·API 쿼터 절약).')
        if ex['n_broker_fills'] and ex['n_matched_fills'] == 0:
            add('EXEC_NO_DECISION_PRICE', 'low', 'execution',
                f"실체결 {ex['n_broker_fills']}건 중 결정가격 매칭 0건 — IS 측정 불가",
                {'unmatched_examples': ex['unmatched_examples']},
                '주문 송출 시 odno ↔ 결정가격(signal_price)·stream 을 함께 영속화하여 체결 원장과 조인 가능하게 할 것.')

        gap = R['signal_execution_gap']
        if gap.get('status') == 'skip' and ex['n_broker_fills'] >= 10:
            add('GAP_ANALYSIS_STARVED', 'low', 'signal_execution_gap',
                'gap_analysis 가 거래 부족으로 스킵됨 (원장에는 체결 존재)',
                {'gap_reason': gap.get('reason'), 'n_broker_fills': ex['n_broker_fills']},
                'gap_analysis 입력(trade_history BUY/SELL 페어) 확인 — 재구성 체결(S_RECON)은 stream 귀속이 없어 '
                '스트림별 갭 분석이 제한됨.')

        dq = R['data_quality']
        for key, label in (('fx_usdkrw', 'FX(USDKRW)'), ('night_futures', 'KOSPI 야간선물'),
                           ('vix', 'VIX'), ('nq_proxy', 'NQ(나스닥 1D)'), ('signal_cache', 'signal_cache')):
            c = dq['checks'].get(key) or {}
            if c.get('stale'):
                add(f'DATA_STALE_{key.upper()}', 'high' if key in ('fx_usdkrw', 'vix') else 'medium', 'data_quality',
                    f"{label} 데이터 신선도 위반 ({c.get('status')}, age={c.get('age_hours')}h)",
                    c, f'{label} 수집기 상태 점검 및 소스 폴백 확인. 해당 데이터 의존 신호는 신뢰도 하향 반영 권장.')
        if dq['overnight_ssot'].get('degraded'):
            add('DATA_OVERNIGHT_SSOT_DEGRADED', 'high', 'data_quality',
                f"overnight SSOT 상태 {dq['overnight_ssot'].get('status')}", dq['overnight_ssot'],
                'DEGRADED_MISSING 필드 재수집; 해당 필드 의존 전략(S0/S1 갭 신호)은 축소 사이징 권장.')
        qs = dq.get('signal_cache_quality_score')
        if qs is not None and qs < float(self.cfg.get('diagnostics.min_quality_score', 0.8)):
            add('DATA_QUALITY_SCORE_LOW', 'medium', 'data_quality', f'signal_cache 품질 점수 {qs:.2f}',
                {'score': qs, 'field_quality': dq.get('field_quality')}, '저품질 필드 소스 교체/재수집.')
        dn = dq.get('data_nogo') or {}
        if dn.get('macro') or dn.get('corrupt'):
            add('DATA_NOGO_ACTIVE', 'high', 'data_quality', 'DATA_NOGO 활성 — 신규 진입 제한 중', dn,
                'R² 미달 지표의 원천 데이터 복구 후 TTL 만료 또는 수동 해제(clear_data_nogo).')
        elif dn.get('ticker'):
            add('DATA_NOGO_TICKER_ISOLATED', 'info', 'data_quality',
                f"개별 종목 거래정지/데이터 격리 {len(dn['ticker'])}건 ({dn['ticker']}) — 포트폴리오 정상 매매 중", dn,
                '거래정지/이상 종목만 개별 진입 차단 중(정상 작동). TTL 만료 시 자동 해제.')

        rk = R['risk']
        if rk.get('status') == 'ok':
            dd = rk['drawdown']
            cur = dd['current_dd_pct']
            warn = dd['thresholds']['warning_pct']
            red = max(dd['thresholds']['reduce_pct'], dd['thresholds']['hard_floor_pct'])  # 0 에 가까운(먼저 닿는) 임계
            if cur <= red + 1.0:
                add('RISK_DD_NEAR_KILL', 'critical', 'risk', f'DD {cur:.2f}% — 축소/하드플로어 임계({red}%) 1pp 이내',
                    dd, '신규 진입 규모 자율 축소 검토, 킬스위치 상태/알림 확인 (자동 조치는 킬스위치 소관).')
            elif cur <= warn:
                add('RISK_DD_WARNING', 'high', 'risk', f'DD {cur:.2f}% ≤ 경고 {warn}%', dd,
                    '손실 기여 상위 포지션 점검, 상관 높은 포지션 중복 노출 축소 검토.')
            conc = rk['concentration']
            if conc.get('breaches'):
                dyn_lims = conc.get('dynamic_limits') or {}
                breach_desc = [
                    f"{tk}(동적상한 {(dyn_lims.get(tk) or {}).get('limit', conc['max_single_limit']):.1%})"
                    for tk in conc['breaches']
                ]
                add('RISK_CONCENTRATION_BREACH', 'medium', 'risk',
                    f"동적 위험예산 비중 한도 초과: {', '.join(breach_desc)}",
                    {'top': conc['top'], 'dynamic_limits': dyn_lims},
                    '변동성·클러스터 상관계수 반영 동적 목표비중(dynamic_limit) 이내로 차기 리밸런싱 시 축소.')
            for pr in conc.get('high_corr_pairs') or []:
                if (_f(pr.get('correlation')) or 0) >= 0.9:
                    add(f"RISK_CORR_PAIR_{pr.get('pair')}", 'low', 'risk',
                        f"고상관 페어 {pr.get('pair')} ρ={pr.get('correlation')}", pr,
                        '사실상 동일 베팅 — 합산 비중 기준으로 한도 관리.')
            cash = rk['cash']
            eff_cr = cash.get('effective_cash_ratio', cash['cash_ratio'])
            if eff_cr < cash['min_cash_ratio']:
                add('RISK_CASH_BELOW_MIN', 'medium', 'risk',
                    f"유효현금비중 {eff_cr:.1%} < 최소 {cash['min_cash_ratio']:.0%}", cash,
                    '신규 매수 수량 산정 시 현금 하한 반영(주문가능금액 거부 감소 효과).')
            ss = rk['safety_state']
            if ss.get('kill_switch_triggered') or ss.get('exit_only_flag'):
                add('RISK_SAFETY_ENGAGED', 'info', 'risk', '안전장치 활성 상태(정보)', ss,
                    '원인 해소 확인 후 사람이 해제 (본 진단은 변경하지 않음).')
        if R.get('load_errors'):
            add('DIAG_LOAD_ERRORS', 'low', 'meta', '일부 입력 파일 파싱 실패', R['load_errors'],
                '해당 파일 무결성 점검.')
        issues.sort(key=lambda i: (_SEV_RANK.get(i['severity'], 9), i['id']))
        return issues

    @staticmethod
    def _summary(R: Dict[str, Any]) -> Dict[str, Any]:
        sev = Counter(i['severity'] for i in R['issues'])
        st = (R['performance']['total'].get('stats') or {})
        return {
            'decision': DECISION, 'blocking': False,
            'issue_counts': dict(sev),
            'top_actions': [f"[{i['severity']}] {i['title']} → {i['recommendation']}" for i in R['issues'][:5]],
            'sharpe': (st.get('sharpe') or {}).get('value'),
            'psr': (st.get('psr_sr0') or {}).get('value'),
            'min_trl_more_days': (st.get('min_trl') or {}).get('more_days_needed'),
            'current_dd_pct': ((R['risk'].get('drawdown') or {}).get('current_dd_pct')),
            'mean_is_bps': R['execution']['implementation_shortfall']['total'].get('mean_is_bps'),
        }

    # ─────────────────────────────────────────────────────────
    # 출력
    # ─────────────────────────────────────────────────────────
    def _write(self, result: Dict[str, Any], out_dir: Optional[Path]):
        from src.utils.file_ops import atomic_write_json, atomic_write_text
        d = Path(out_dir or self.results)
        atomic_write_json(d / 'live_diagnostics.json', result, indent=2, default=str)
        atomic_write_text(d / 'live_diagnostics_report.md', render_markdown(result))


def _fmt(v, spec='.3f', na='N/A'):
    try:
        return format(float(v), spec) if v is not None else na
    except (TypeError, ValueError):
        return na


def render_markdown(R: Dict[str, Any]) -> str:
    L = []
    S = R['summary']
    L.append(f"# Meridian 소액 실전 일일 진단 — {R['generated_at'][:16]}")
    L.append('')
    L.append(f"**결정: `{R['decision']}` (blocking=False)** — Go/No-Go 게이트 폐지. 통계는 진단 정보이며 매매를 차단하지 않습니다.")
    L.append('')
    L.append('## 결론 (우선순위 교정 액션)')
    if R['issues']:
        for i in R['issues'][:10]:
            L.append(f"- **[{i['severity'].upper()}] {i['title']}** — {i['recommendation']}")
    else:
        L.append('- 탐지된 이슈 없음.')
    L.append('')
    p = R['performance']['total']
    L.append('## (a) 성과 & 불확실성')
    L.append(f"- NAV 소스: `{p.get('nav_source')}` · 거래일 관측 {p.get('n_nav_obs')}개 · 제외 행 {p.get('dropped_rows')}")
    L.append(f"- rf={_fmt(p.get('rf_annual_pct'), '.2f')}% ({p.get('rf_source')}), DSR 시행 수 N={p.get('dsr_n_trials')}")
    if p.get('status') == 'ok':
        st = p['stats']
        ci = st['sharpe_ci_analytic']['value']
        bci = (st.get('sharpe_ci_bootstrap') or {}).get('value')
        L.append(f"- Sharpe {_fmt(st['sharpe']['value'], '.2f')} (95% CI 해석적 "
                 f"{'[' + _fmt(ci[0], '.2f') + ', ' + _fmt(ci[1], '.2f') + ']' if ci else 'N/A'}; 부트스트랩 "
                 f"{'[' + _fmt(bci[0], '.2f') + ', ' + _fmt(bci[1], '.2f') + ']' if bci else 'N/A'}) · "
                 f"Sortino {_fmt(st['sortino']['value'], '.2f')} · MDD {_fmt((st['max_drawdown']['value'] or 0) * 100, '.2f')}% · "
                 f"Calmar {_fmt(st['calmar']['value'], '.2f')} · CAGR {_fmt((st['cagr']['value'] or 0) * 100, '.1f')}%")
        L.append(f"- PSR(SR*=0) {_fmt(st['psr_sr0']['value'], '.3f')} · DSR {_fmt(st['dsr']['value'], '.3f')} · "
                 f"MinTRL {_fmt(st['min_trl']['value'], '.0f')}일")
        L.append(f"- {p.get('confidence_statement')}")
    else:
        L.append(f"- **insufficient data**: {p.get('reason')}")
    rb = R['performance'].get('realized_by_market') or {}
    for m, a in rb.items():
        L.append(f"- 실현손익({m}, FIFO): {_fmt(a['realized_pnl_local'], ',.2f')} {a['currency']} · "
                 f"청산 {a['n_closed_lots']}건 · 승률 {_fmt(a['win_rate'], '.0%')} · 창 밖 매수분 미매칭 {a['n_sells_unmatched']}건")
    L.append('')
    e = R['execution']
    L.append('## (b) 집행 품질 (실체결 기준)')
    tot = e['implementation_shortfall']['total']
    L.append(f"- 최근 {e['lookback_days']}일 브로커 체결 {e['n_broker_fills']}건 · 결정가 매칭 {e['n_matched_fills']}건 · "
             f"미매칭 {e['n_unmatched_fills']}건 · 이상치 제외 {e['n_outlier_excluded']}건")
    L.append(f"- IS 평균 {_fmt(tot.get('mean_is_bps'), '.1f')}bp · 중앙값 {_fmt(tot.get('median_is_bps'), '.1f')}bp · "
             f"p90 {_fmt(tot.get('p90_is_bps'), '.1f')}bp (n={tot.get('n')})")
    for k, g in e['implementation_shortfall']['by_market'].items():
        L.append(f"  - 시장 {k}: 평균 {_fmt(g.get('mean_is_bps'), '.1f')}bp (n={g.get('n')})")
    for k, g in e['implementation_shortfall']['by_stream'].items():
        L.append(f"  - 스트림 {k}: 평균 {_fmt(g.get('mean_is_bps'), '.1f')}bp (n={g.get('n')})")
    of = e['order_flow_live']
    L.append(f"- 라이브 주문 {of['n_orders']}건 · 거부/오류 {of['n_rejected_or_error']}건 · "
             f"거부율(장종료 스킵 제외) {_fmt(of.get('submit_reject_rate'), '.0%')} · 분류 {of['error_categories']}")
    L.append(f"- failed_orders(최근): {e['failed_orders']}")
    if e.get('tca_summary'):
        L.append(f"- TCA 요약: {e['tca_summary']}")
    L.append(f"- 정의: {e['is_definition']}")
    L.append('')
    g = R['signal_execution_gap']
    L.append('## (c) 신호-집행 갭')
    L.append(f"- gap_analysis: status={g.get('status')} · n={g.get('n_trades')} · {g.get('reason') or ''} · {g.get('summary') or ''}")
    if g.get('last_feedback'):
        L.append(f"- 최근 피드백: {g['last_feedback']}")
    L.append('')
    d = R['data_quality']
    L.append('## (d) 데이터 품질')
    for k, c in d['checks'].items():
        L.append(f"- {k}: {c.get('status')} (age={c.get('age_hours')}h, 허용={c.get('allowed_hours')}h, 값={c.get('value')})")
    L.append(f"- overnight SSOT: {d['overnight_ssot']}")
    L.append(f"- signal_cache 품질점수: {d.get('signal_cache_quality_score')} · DATA_NOGO: {d.get('data_nogo')}")
    L.append(f"- 24h 시스템 오류(소스별): {d.get('system_alert_errors_24h')}")
    L.append('')
    r = R['risk']
    L.append('## (e) 리스크')
    if r.get('status') == 'ok':
        dd = r['drawdown']
        L.append(f"- NAV {_fmt(r['nav'], ',.0f')} ({r['nav_source']}) · HWM {_fmt(dd['hwm'], ',.0f')} · "
                 f"현재 DD {_fmt(dd['current_dd_pct'], '.2f')}% · 임계 {dd['thresholds']}")
        c = r['concentration']
        L.append(f"- 집중도: n={c['n_positions']} · HHI {_fmt(c.get('hhi'), '.3f')} · 상위 "
                 + ', '.join(f"{x['ticker']}({_fmt(x['weight'], '.1%')})" for x in c['top'])
                 + f" · 한도 초과 {c['breaches']}")
        L.append(f"- 현금비중 {_fmt(r['cash']['cash_ratio'], '.1%')} (최소 {_fmt(r['cash']['min_cash_ratio'], '.0%')})")
        L.append(f"- 안전장치 상태(읽기전용): {r['safety_state']}")
    else:
        L.append('- insufficient data')
    L.append('')
    L.append('## (f) 이슈 전체')
    for i in R['issues']:
        L.append(f"- `{i['id']}` [{i['severity']}] {i['title']}")
    L.append('')
    L.append('---')
    L.append('지표 정의: ' + '; '.join(f"{k}: {v}" for k, v in R['performance']['definitions'].items()))
    return '\n'.join(L) + '\n'


def run_live_diagnostics(results_dir: Optional[Path] = None, write: bool = True, out_dir: Optional[Path] = None,
                         record_nav: bool = True, cfg=None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """파이프라인 진입점 — 항상 비차단 결과를 반환한다 (예외 시에도 DIAGNOSE_CONTINUE)."""
    try:
        return LiveDiagnostics(results_dir=results_dir, cfg=cfg, now=now, record_nav=record_nav).run(
            write=write, out_dir=out_dir)
    except Exception as e:
        logger.error(f"  [LiveDiag] 진단 실패 (비차단): {e}", exc_info=True)
        return {'schema': SCHEMA_VERSION, 'decision': DECISION, 'blocking': False, 'status': 'error',
                'error': f"{type(e).__name__}: {e}", 'issues': [], 'summary': {'decision': DECISION, 'blocking': False}}

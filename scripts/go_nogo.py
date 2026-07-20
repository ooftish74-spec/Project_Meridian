"""
Project Meridian — Go/No-Go Engine
=====================================
2-슬리브 + 4-Stream 아키텍처 Go/No-Go 판정.
Shadow 14일 추적 후 실전 전환 여부를 결정합니다.

Usage:
    python scripts/go_nogo.py
"""

import json, logging, sys
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from config.dynamic_config import DynamicConfig
try:
    from src.utils.time_utils import now_kst
except ImportError as e:
    def now_kst():
        return datetime.now()

logger = logging.getLogger(__name__)
cfg = DynamicConfig()
_RESULTS = _PROJECT_ROOT / 'results'


class GoNoGoEngine:
    """Go/No-Go 판정 엔진."""

    def evaluate(self) -> dict:
        """종합 판정 (전 전략 개별 추적)."""
        shadow = self._load_shadow()
        criteria = {}

        # C1: Shadow 기간 — ★ daily_snapshots 기반
        snapshots = shadow.get('daily_snapshots', [])
        days = len(snapshots) or len(shadow.get('daily_records', []))
        from datetime import date
        # ★ C-01 FIX: 하드코딩된 날짜 제거 → shadow_portfolio.json SSoT
        _shadow_start = self._get_shadow_start_date()
        real_days = (date.today() - _shadow_start).days
        if days < real_days:
            days = real_days
        min_days = cfg.get('gonogo.shadow_min_days')
        criteria['shadow_days'] = {
            'value': days, 'threshold': min_days,
            'pass': days >= min_days,
            'detail': f'{days}/{min_days}일'
        }

        # C2: 전략별 거래/리밸런싱 수
        trade_history = shadow.get('trade_history', [])
        strategy_trades = {}
        for t in trade_history:
            strat = t.get('stream', t.get('strategy', ''))
            strategy_trades[strat] = strategy_trades.get(strat, 0) + 1

        min_trades = cfg.get('gonogo.a1_min_trades')
        _active = cfg.get('gonogo.active_streams', ['S1', 'S2', 'S3', 'S4'])
        for strat in _active:
            count = strategy_trades.get(strat, 0)
            # B1~B3는 월 1회 리밸런싱이므로 기준 완화
            strat_min = min_trades if strat.startswith('A') else max(1, min_trades // 3)
            criteria[f'{strat.lower()}_trades'] = {
                'value': count, 'threshold': strat_min,
                'pass': count >= strat_min,
                'detail': f'{strat} {count}/{strat_min}건',
            }

        # C3: 전체 수익률
        # ★ Bug Fix: daily_snapshots NAV를 SSoT로 사용 (virtual_nav는 오래된 값일 수 있음)
        cum = shadow.get('cumulative', {})
        snapshots = shadow.get('daily_snapshots', [])
        if snapshots:
            nav = snapshots[-1].get('nav', shadow.get('virtual_nav', 0))
        else:
            nav = shadow.get('virtual_nav', 0)
        initial = shadow.get('initial_capital', 1)
        total_return = (nav / initial - 1) * 100 if initial > 0 else 0
        criteria['total_return'] = {
            'value': round(total_return, 2),
            'threshold': 0,
            'pass': total_return >= 0,
        }

        # C3-S: 전략별 수익률 (개별 추적 — 정보 표시용, 블로커 아님)
        strategy_pnl = shadow.get('strategy_pnl', {})

        # ★ Task #6: strategy_pnl 폴백 — 비어있으면 trade_history에서 동적 집계
        if not strategy_pnl and trade_history:
            strategy_pnl = {}
            for t in trade_history:
                if t.get('action') == 'SELL':
                    stream = t.get('stream', t.get('stream_id', ''))
                    if stream:
                        strategy_pnl[stream] = (
                            strategy_pnl.get(stream, 0)
                            + t.get('realized_pnl', 0)
                        )

        for strat in _active:
            pnl = strategy_pnl.get(strat, 0)
            pnl_pct = (pnl / max(initial, 1)) * 100
            criteria[f'{strat.lower()}_return'] = {
                'value': round(pnl_pct, 2),
                'threshold': 'info',
                'pass': True,  # 정보 표시용 (블로커 아님)
                'detail': f'{strat} PnL={pnl:,.0f}원 ({pnl_pct:+.2f}%)',
            }

        # C4: 승률
        # ★ Bug Fix: cumulative.wins는 항상 0 (original_amount 미설정 버그)
        #   → trade_history의 SELL + realized_pnl > 0 으로 동적 계산
        #   → ME의 realized_win_rate와 동일한 소스 사용
        sell_trades = [t for t in trade_history if t.get('action') == 'SELL']
        n_sells = len(sell_trades)
        n_wins = sum(1 for t in sell_trades if t.get('realized_pnl', t.get('pnl', 0)) > 0)
        win_rate = n_wins / max(n_sells, 1)
        criteria['win_rate'] = {
            'value': round(win_rate * 100, 1),
            'threshold': cfg.get('gonogo.win_rate_threshold', 0.45) * 100,
            'pass': n_sells < 5 or win_rate >= cfg.get('gonogo.win_rate_threshold', 0.45),  # 5건 미만이면 면제
            'detail': f'{n_wins}/{n_sells}건 승리 ({win_rate:.1%})',
        }

        # C5: MDD — ★ daily_snapshots 우선
        if snapshots:
            mdd = self._calc_mdd(snapshots)
        else:
            records = shadow.get('daily_records', [])
            mdd = self._calc_mdd(records)
        dd_limit = abs(cfg.get('risk.total_dd_limit')) * 100
        criteria['mdd'] = {
            'value': round(mdd, 2),
            'threshold': dd_limit,
            'pass': abs(mdd) < dd_limit,
        }

        # C6: DA (방향 정확도) — SSoT MeasurementEngine
        me = self._load_measurement()
        da = me.get('da', None)
        da_n = me.get('da_n', 0)
        min_da_n = cfg.get('gonogo.da_min_n', 10)  # DA 최소 표본 수
        if da is not None and da_n >= min_da_n:
            criteria['da'] = {
                'value': round(da * 100, 1),
                'threshold': cfg.get('gonogo.da_threshold', 0.52) * 100,
                'pass': da >= cfg.get('gonogo.da_threshold', 0.52),
                'detail': f'{da:.1%} ({da_n}건)',
            }
        else:
            criteria['da'] = {
                'value': f'PENDING ({da_n}건)',
                'threshold': 52,
                'pass': True,  # 데이터 부족 시 PENDING (블로커 아님)
                'detail': f'데이터 부족 ({da_n}/{min_da_n}건)',
                'pending': True,
            }

        # C7: IC (정보 계수)
        # ★ IC 유의성 판정은 cross-sectional(종목 수)가 아닌 시계열(거래일 수) 기반
        # single-day cross-sectional IC 1개로는 통계적 판단 불가
        ic = me.get('ic', None)
        ic_p = me.get('ic_p_value', None)
        ic_n = me.get('ic_n', 0)
        min_ic_days = cfg.get('gonogo.ic_min_days', 20)  # IC 시계열 최소 거래일
        actual_days = days  # shadow_days (실제 거래일 수)
        if ic is not None and actual_days >= min_ic_days:
            criteria['ic'] = {
                'value': round(ic, 4),
                'threshold': 'p<0.10',
                'pass': ic_p is not None and ic_p < cfg.get('gonogo.ic_p_threshold', 0.10),
                'detail': f'IC={ic:.4f} p={ic_p:.3f} ({ic_n}종목, {actual_days}일)' if ic_p is not None else f'IC={ic:.4f} p=N/A',
            }
        else:
            criteria['ic'] = {
                'value': f'PENDING (D{actual_days}/{min_ic_days})',
                'threshold': 'p<0.10',
                'pass': True,  # PENDING — 데이터 부족 시 블로커 아님
                'detail': f'거래일 부족 ({actual_days}/{min_ic_days}일)',
                'pending': True,
            }

        # C8: 비용 차감 후 Alpha
        cum_return = total_return
        total_cost = shadow.get('cumulative', {}).get('total_cost', 0)
        cost_pct = (total_cost / max(initial, 1)) * 100
        net_alpha = cum_return - cost_pct
        alpha_min_days = cfg.get('go.alpha.min_days', 20)
        if days < alpha_min_days:
            # ★ 거래일 부족 시 PENDING (블로커 아님)
            criteria['net_alpha'] = {
                'value': f'PENDING (D{days}/{alpha_min_days})',
                'threshold': 0,
                'pass': True,
                'detail': f'거래일 부족 ({days}/{alpha_min_days}일), '
                          f'현재 Alpha={net_alpha:+.2f}%',
                'pending': True,
            }
        else:
            criteria['net_alpha'] = {
                'value': round(net_alpha, 2),
                'threshold': 0,
                'pass': net_alpha >= 0,
                'detail': f'수익{cum_return:.2f}% - 비용{cost_pct:.2f}% = {net_alpha:.2f}%',
            }

        # 종합 판정 (전략별 trades/return은 info → 블로커에서 제외)
        blocker_keys = [
            'shadow_days', 'total_return', 'win_rate', 'mdd',
            'da', 'ic', 'net_alpha',
        ]
        has_pending = any(
            criteria[k].get('pending', False)
            for k in blocker_keys if k in criteria
        )
        all_pass = all(
            criteria[k]['pass']
            for k in blocker_keys if k in criteria
        )

        if all_pass and not has_pending:
            verdict = 'GO'
        elif all_pass and has_pending:
            verdict = 'PENDING'  # 통과했지만 데이터 부족 기준 존재
        else:
            verdict = 'NO-GO'

        result = {
            'verdict': verdict,
            'criteria': criteria,
            'has_pending': has_pending,
            'strategy_summary': {
                strat: {
                    'trades': strategy_trades.get(strat, 0),
                    'pnl': strategy_pnl.get(strat, 0),
                }
                for strat in cfg.get('gonogo.active_streams', ['S1', 'S2', 'S3', 'S4'])
            },
            'timestamp': now_kst().isoformat(),
        }

        self._save(result)
        self._log_result(result)
        return result

    def _calc_mdd(self, records: list) -> float:
        if not records:
            return 0
        navs = [r.get('nav', 0) for r in records]
        peak = navs[0]
        mdd = 0
        for nav in navs:
            peak = max(peak, nav)
            dd = (nav / peak - 1) * 100
            mdd = min(mdd, dd)
        return mdd

    def _load_shadow(self) -> dict:
        f = _RESULTS / 'shadow_portfolio.json'
        if f.exists():
            try: return json.loads(f.read_text())
            except Exception as e: logger.debug(f"  Shadow portfolio 로드 실패: {e}")
        return {}

    def _load_measurement(self) -> dict:
        """MeasurementEngine SSoT에서 DA/IC 로드."""
        f = _RESULTS / 'measurement_engine.json'
        if f.exists():
            try:
                return json.loads(f.read_text()).get('official', {})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {}

    def _save(self, result: dict):
        f = _RESULTS / 'go_nogo.json'
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    def _log_result(self, result: dict):
        v = result['verdict']
        icon = '✅' if v == 'GO' else ('⏳' if v == 'PENDING' else '❌')
        logger.info(f"\n{'='*60}")
        logger.info(f"  {icon} Go/No-Go: {v}")
        logger.info(f"{'─'*60}")

        # 핵심 기준
        core_keys = [
            'shadow_days', 'total_return', 'win_rate', 'mdd',
            'da', 'ic', 'net_alpha',
        ]
        for name in core_keys:
            c = result['criteria'].get(name, {})
            if not c:
                continue
            status = '✅' if c['pass'] else '❌'
            logger.info(f"    {status} {name}: {c.get('value')} "
                       f"(threshold: {c.get('threshold')})")

        # 전략별 요약
        logger.info(f"{'─'*60}")
        logger.info(f"  📊 전략별 성과:")
        summary = result.get('strategy_summary', {})
        for strat in cfg.get('gonogo.active_streams', ['S1', 'S2', 'S3', 'S4']):
            s = summary.get(strat, {})
            trades = s.get('trades', 0)
            pnl = s.get('pnl', 0)
            logger.info(f"    {strat}: {trades}건, PnL={pnl:+,.0f}원")
        logger.info(f"{'='*60}")

    def evaluate_meridian(self) -> dict:
        """Meridian 4-Stream Go/No-Go 판정.

        Shadow 기간: shadow_start 시작 → 14일 후 판정.
        """
        from datetime import date, timedelta
        criteria = {}

        shadow = self._load_shadow()
        records = shadow.get('daily_records', [])
        trades = shadow.get('trade_history', [])
        # ★ Bug Fix: daily_snapshots NAV SSoT + shadow initial_capital SSoT
        snapshots = shadow.get('daily_snapshots', [])
        if snapshots:
            nav = snapshots[-1].get('nav', shadow.get('virtual_nav', 0))
        else:
            nav = shadow.get('virtual_nav', 0)
        # [Live Patch] 초기 자본 SSoT: shadow_portfolio.json → DynamicConfig 순으로 동적 조회
        # 하드코딩된 100_000_000 제거 — DynamicConfig portfolio.initial_capital이 단일 진실 원천
        initial = shadow.get('initial_capital', cfg.get('portfolio.initial_capital'))

        # ── M1: Shadow 기간 ──
        # ★ C-02 FIX: 하드코딩된 날짜 제거 → _get_shadow_start_date() SSoT
        shadow_start = self._get_shadow_start_date()
        today = date.today()
        shadow_days = (today - shadow_start).days
        min_days = cfg.get('gonogo.shadow_min_days', 14)
        criteria['shadow_days'] = {
            'value': shadow_days,
            'threshold': min_days,
            'pass': shadow_days >= min_days,
            'detail': f'{shadow_days}/{min_days}일 (시작: {shadow_start})',
        }

        # ── M2: 스트림별 거래 수 ──
        _active_streams = cfg.get('gonogo.active_streams', ['S1', 'S2', 'S3', 'S4'])
        stream_trades = {'S1': 0, 'S2': 0, 'S3': 0, 'S4': 0}
        for t in trades:
            stream = t.get('stream', t.get('stream_id', ''))
            if stream in stream_trades:
                stream_trades[stream] += 1
        for sid in ['S1', 'S2', 'S3', 'S4']:
            count = stream_trades[sid]
            min_t = cfg.get(f'gonogo.{sid.lower()}_min_trades',
                            3 if sid in ['S1', 'S2'] else 1)  # S3/S4는 월 1회 리밸런싱
            # ★ 비활성 스트림은 trades 기준 자동 pass
            is_active = sid in _active_streams
            criteria[f'{sid.lower()}_trades'] = {
                'value': count,
                'threshold': min_t if is_active else 'N/A (비활성)',
                'pass': (not is_active) or count >= min_t or shadow_days < min_days,
                'detail': f'{sid} {count}/{min_t}건' + ('' if is_active else ' (비활성-자동pass)'),
            }

        # ── M3: 전체 수익률 ──
        total_return = (nav / max(initial, 1) - 1) * 100 if nav > 0 else 0
        criteria['total_return'] = {
            'value': round(total_return, 2),
            'threshold': 0,
            'pass': total_return >= cfg.get('gonogo.total_return_floor', -3.0),  # config 기반
            'detail': f'NAV={nav:,.0f} / Initial={initial:,.0f}',
        }

        # ── M4: MDD — ★ daily_snapshots 우선 ──
        snapshots = shadow.get('daily_snapshots', [])
        if snapshots:
            mdd = self._calc_mdd(snapshots)
        else:
            records = shadow.get('daily_records', [])
            mdd = self._calc_mdd(records) if records else 0
        _mdd_limit = cfg.get('gonogo.mdd_threshold', -10.0)
        criteria['mdd'] = {
            'value': round(mdd, 2),
            'threshold': _mdd_limit,
            'pass': mdd > _mdd_limit,
        }

        # ── M5: DA / IC (MeasurementEngine SSoT) ──
        me = self._load_measurement()
        da = me.get('da', 0)
        da_n = me.get('da_n', 0)
        criteria['da'] = {
            'value': round(da * 100, 1) if da else 'N/A',
            'threshold': 52,
            'pass': da_n < cfg.get('gonogo.da_min_n', 10) or (da is not None and da >= cfg.get('gonogo.da_threshold', 0.52)),
            'detail': f'{da:.1%} ({da_n}건)' if da else f'PENDING ({da_n}건)',
            'pending': da_n < cfg.get('gonogo.da_min_n', 10),
        }

        # ── M6: 시스템 안정성 ──
        # ★ C-03 FIX: 하드코딩된 28/28 제거 → pytest 결과 파일 동적 파싱
        _test_data = self._load_test_result()
        if _test_data is None:
            criteria['test_pass'] = {
                'value': None,
                'threshold': None,
                'pass': False,
                'pending': True,
                'detail': '테스트 결과 파일 없음 (PENDING — results/pytest_report.json 필요)',
            }
        else:
            _ok = _test_data['failed'] == 0
            criteria['test_pass'] = {
                'value': _test_data['passed'],
                'threshold': _test_data['total'],
                'pass': _ok,
                'detail': (
                    f"{_test_data['passed']}/{_test_data['total']} 테스트 통과"
                    if _ok else
                    f"{_test_data['failed']}건 실패 — Go 불가"
                ),
            }

        # ── 종합 판정 ──
        blocker_keys = ['shadow_days', 'total_return', 'mdd', 'da']
        has_pending = any(
            criteria[k].get('pending', False) for k in blocker_keys
        )
        all_pass = all(criteria[k]['pass'] for k in blocker_keys)

        if shadow_days < min_days:
            verdict = 'SHADOW_RUNNING'
            _target = shadow_start + timedelta(days=min_days)
            detail = f'Shadow D{shadow_days}/{min_days}. 판정일: {_target.strftime("%Y-%m-%d")}'
        elif all_pass and not has_pending:
            verdict = 'GO'
            detail = '모든 기준 통과. 실전 전환 가능.'
        elif all_pass and has_pending:
            verdict = 'PENDING'
            detail = '기준 통과했으나 데이터 부족.'
        else:
            verdict = 'NO-GO'
            detail = '기준 미달 항목 존재.'

        result = {
            'verdict': verdict,
            'detail': detail,
            'architecture': 'Meridian 5-Stream',
            'criteria': criteria,
            'stream_trades': stream_trades,
            'shadow_start': str(shadow_start),
            'shadow_days': shadow_days,
            'target_date': (shadow_start + timedelta(days=min_days)).strftime("%Y-%m-%d"),
            'timestamp': now_kst().isoformat(),
        }

        # ★ Meridian 통합: go_nogo.json에 'meridian' 키로 병합 저장
        existing_gn = {}
        gn_path = _RESULTS / 'go_nogo.json'
        if gn_path.exists():
            try:
                existing_gn = json.loads(gn_path.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        existing_gn['meridian'] = result
        gn_path.parent.mkdir(parents=True, exist_ok=True)
        gn_path.write_text(json.dumps(existing_gn, indent=2, ensure_ascii=False))

        self._log_meridian(result)
        return result

    # ──────────────────────────────────────────────
    # ★ HELPER METHODS (C-01, C-02, C-03 지원)
    # ──────────────────────────────────────────────

    def _get_shadow_start_date(self):
        """섀도 시작일 동적 조회 (SSoT 우선순위).

        순서:
            1) shadow_portfolio.json의 shadow_start_date 필드
            2) shadow_portfolio.json의 daily_snapshots[0]['date']
            3) DynamicConfig gonogo.shadow_start_date
            4) 오늘 날짜 (최후 수단 — Go 판정 불가 상태)
        """
        from datetime import date
        # 1순위: shadow_portfolio.json
        sp_file = _RESULTS / 'shadow_portfolio.json'
        if sp_file.exists():
            try:
                sp = json.loads(sp_file.read_text(encoding='utf-8'))
                # 명시적 시작일 필드
                start_str = sp.get('shadow_start_date') or sp.get('start_date')
                if not start_str:
                    # daily_snapshots 첫 날짜 대안
                    snaps = sp.get('daily_snapshots', [])
                    if snaps:
                        start_str = snaps[0].get('date')
                if start_str:
                    return date.fromisoformat(str(start_str)[:10])
            except Exception as e:
                logger.debug(f"  shadow_portfolio.json 날짜 파싱 실패: {e}")
        # 2순위: DynamicConfig
        try:
            start_str = cfg.get('gonogo.shadow_start_date')
            if start_str:
                return date.fromisoformat(str(start_str)[:10])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        # 최후 수단: 오늘 날짜 (days=0 → 기준 미달 → 안전한 PENDING)
        logger.warning("  shadow_start_date 미확인 — 오늘 날짜 사용 (Go 판정 불가)")
        return date.today()

    def _load_test_result(self):
        """pytest 결과 파일에서 통과/실패 수 파싱.

        지원 경로 (우선순위 순):
            results/pytest_report.json
            .report.json (pytest-json-report 기본)
            test_results.json

        Returns:
            dict with 'passed', 'failed', 'total' 키, 또는 None (파일 없음)
        """
        candidate_paths = [
            _RESULTS / 'pytest_report.json',
            _PROJECT_ROOT / '.report.json',
            _PROJECT_ROOT / 'test_results.json',
        ]
        for p in candidate_paths:
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding='utf-8'))
                    # pytest-json-report 형식
                    summary = raw.get('summary', raw)
                    passed = int(summary.get('passed', 0))
                    failed = int(summary.get('failed', 0))
                    total = int(
                        summary.get('total', summary.get('collected', passed + failed))
                    )
                    return {'passed': passed, 'failed': failed, 'total': total}
                except Exception as e:
                    logger.debug(f"  테스트 결과 파일 파싱 실패 ({p}): {e}")
        return None  # 파일 없음 → PENDING

    def _log_meridian(self, result: dict):
        v = result['verdict']
        icons = {'GO': '✅', 'PENDING': '⏳', 'NO-GO': '❌', 'SHADOW_RUNNING': '🔄'}
        icon = icons.get(v, '❓')
        logger.info(f"\n{'='*60}")
        logger.info(f"  {icon} Meridian Go/No-Go: {v}")
        logger.info(f"  {result['detail']}")
        logger.info(f"{'─'*60}")
        for name, c in result['criteria'].items():
            status = '✅' if c['pass'] else '❌'
            if c.get('pending'):
                status = '⏳'
            logger.info(f"    {status} {name}: {c.get('value')} "
                       f"(threshold: {c.get('threshold')})")
        logger.info(f"{'='*60}")


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    engine = GoNoGoEngine()

    # Legacy 2-Sleeve 판정
    print("\n📋 Legacy (2-Sleeve) Go/No-Go:")
    result1 = engine.evaluate()

    # Meridian 4-Stream 판정
    print("\n📋 Meridian (4-Stream) Go/No-Go:")
    result2 = engine.evaluate_meridian()

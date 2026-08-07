"""
S4 Account Tracker — ISA/IRP/개인연금/종합계좌 가상거래 기록
================================================================

S4는 API 미연결. 시스템이 Advisory(매매 조언)를 생성하면,
사용자가 직접 거래를 실행하고, 이 모듈이 결과를 추적/기록합니다.

Go Live 시:
  1. 시스템 → Advisory 생성 (매수/매도/리밸런싱 권고)
  2. 사용자 → 실제 거래 실행
  3. 시스템 → 결과 추적 + 성과 업데이트

계좌 구조:
  ISA       — 고배당 ETF + QV 개별주 (비과세 한도 극대화)
  IRP       — 채권+금 안전자산 (위험자산 ≤30%)
  PENSION   — Dividend Growth + 성장 ETF (과세이연 복리)
  BROKERAGE — 섹터QV 대형주 (독립 운용, 유동성 확보)

Usage:
    from src.streams.s4_advisory.account_tracker import S4AccountTracker
    tracker = S4AccountTracker()
    tracker.sync_from_shadow_portfolio()
    summary = tracker.get_account_summary()
"""

import json
import logging
from datetime import datetime
from src.utils.file_ops import atomic_write_json

from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_STATE_FILE = _RESULTS / 's4_account_tracker.json'

# ═══════════════════════════════════════
# ETF → 계좌 매핑 (S4 Advisory Stream 기준)
# ═══════════════════════════════════════
# ★ 주의: ETF가 여러 계좌에서 사용될 수 있음 (예: 279530 → ISA/IRP)
#   이 경우 시그널의 account 필드가 우선. 이 맵은 account 필드 없을 때의 기본값.
ETF_ACCOUNT_MAP = {
    # ISA - 고배당 ETF
    '279530': 'ISA',     # KODEX 고배당주
    '458730': 'ISA',     # TIGER 미국배당다우존스
    '211560': 'ISA',     # TIGER 배당성장
    '289480': 'ISA',     # TIGER 200커버드콜
    '441640': 'ISA',     # KODEX 미국배당커버드콜액티브
    # IRP - 안전자산 (★ 이전 누락 — IRP 소실 원인)
    '471230': 'IRP',     # KODEX 국고채10년액티브
    '411060': 'IRP',     # ACE KRX금현물
    '0091C0': 'IRP',     # KODEX 미국10년국채액티브(H) — 사용자 수동 매수
    '148070': 'IRP',     # KODEX 국고채10년
    '305080': 'IRP',     # TIGER 미국채10년선물
    '132030': 'IRP',     # KODEX 골드선물(H)
    # PENSION - DG + 성장
    '211900': 'PENSION', # KODEX 코리아배당성장
    '458760': 'PENSION', # TIGER 미국배당다우존스타겟커버드콜2호
    '455890': 'PENSION', # TIGER 배당성장50
    '290130': 'PENSION', # KODEX 배당성장
    '329200': 'PENSION', # TIGER 리츠부동산인프라
    '133690': 'PENSION', # TIGER 미국나스닥100
    '379800': 'PENSION', # KODEX 미국S&P500
}


class S4AccountTracker:
    """S4 계좌별 가상거래 추적기.

    shadow_portfolio의 S4 포지션을 계좌별로 분류하여
    독립적인 성과 추적을 수행합니다.
    """

    ACCOUNT_NAMES = ['ISA', 'IRP', 'PENSION', 'BROKERAGE']

    def __init__(self):
        self._state = self._load()

    def _default_state(self) -> Dict:
        """기본 상태."""
        accounts = {}
        for acct in self.ACCOUNT_NAMES:
            accounts[acct] = {
                'positions': {},
                'trade_history': [],
                'cumulative': {
                    'total_trades': 0,
                    'wins': 0,
                    'losses': 0,
                    'total_pnl': 0,
                    'total_cost': 0,
                },
                'nav': 0,
                'daily_snapshots': [],
            }
        return {
            'accounts': accounts,
            'created': datetime.now().isoformat(),
            'last_synced': None,
            'sync_count': 0,
        }

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    def sync_from_shadow_portfolio(self) -> Dict:
        """shadow_portfolio.json에서 S4 포지션을 계좌별로 동기화.

        S4 포지션의 ticker로 계좌를 추론하고,
        기존 account 필드가 있으면 그것을 우선 사용.
        """
        sp = self._load_shadow()
        positions = sp.get('positions', {})
        trade_history = sp.get('trade_history', [])

        # ── 1. S4 포지션 → 계좌별 분류 ──
        for acct in self.ACCOUNT_NAMES:
            self._state['accounts'][acct]['positions'] = {}

        for pos_key, pos in positions.items():
            stream = pos_key.split(':')[0] if ':' in pos_key else pos.get('strategy', '')
            if stream != 'S4':
                continue

            ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else '')
            # 계좌 추론: position.account > ETF_ACCOUNT_MAP > BROKERAGE
            account = pos.get('account', ETF_ACCOUNT_MAP.get(ticker, 'BROKERAGE'))

            acct_state = self._state['accounts'].get(account)
            if not acct_state:
                continue

            acct_state['positions'][ticker] = {
                'ticker': ticker,
                'name': pos.get('name', ticker),
                'quantity': pos.get('quantity', 0),
                'avg_price': pos.get('avg_price', pos.get('entry_price', 0)),
                'entry_price': pos.get('entry_price', 0),
                'amount': pos.get('amount', 0),
                'current_price': pos.get('current_price', pos.get('entry_price', 0)),
                'market_value': pos.get('market_value', pos.get('current_value', 0)),
                'pnl_pct': pos.get('pnl_pct', 0),
                'unrealized_pnl': pos.get('unrealized_pnl', 0),
                'high_water_mark': pos.get('high_water_mark', 0),
                'exits': pos.get('exits', {}),
                'entry_date': pos.get('entry_date', ''),
                'account': account,
            }

        # ── 2. S4 trade_history → 계좌별 분류 ──
        for acct in self.ACCOUNT_NAMES:
            self._state['accounts'][acct]['trade_history'] = []
            self._state['accounts'][acct]['cumulative'] = {
                'total_trades': 0, 'wins': 0, 'losses': 0,
                'total_pnl': 0, 'total_cost': 0,
            }

        for t in trade_history:
            stream = t.get('stream', t.get('stream_id', ''))
            if stream != 'S4':
                continue

            ticker = t.get('ticker', '')
            account = t.get('account', ETF_ACCOUNT_MAP.get(ticker, 'BROKERAGE'))

            acct_state = self._state['accounts'].get(account)
            if not acct_state:
                continue

            acct_state['trade_history'].append({
                'date': t.get('date', ''),
                'action': t.get('action', ''),
                'ticker': ticker,
                'name': t.get('name', ticker),
                'amount': t.get('amount', 0),
                'pnl': t.get('pnl', t.get('realized_pnl', 0)),
                'reason': t.get('reason', ''),
                'account': account,
            })

            # 매도 → cumulative 업데이트
            if t.get('action', '').upper() == 'SELL':
                pnl = t.get('pnl', t.get('realized_pnl', 0))
                acct_state['cumulative']['total_trades'] += 1
                acct_state['cumulative']['total_pnl'] += pnl
                if pnl > 0:
                    acct_state['cumulative']['wins'] += 1
                else:
                    acct_state['cumulative']['losses'] += 1

        # ── 3. 계좌별 NAV 계산 (★ market_value 기반 — 현재가 반영) ──
        for acct in self.ACCOUNT_NAMES:
            acct_state = self._state['accounts'][acct]
            nav = sum(
                p.get('market_value', p.get('amount', 0))
                for p in acct_state['positions'].values()
            )
            acct_state['nav'] = nav

        # ── 4. 일일 스냅샷 ──
        today = datetime.now().strftime('%Y-%m-%d')
        for acct in self.ACCOUNT_NAMES:
            acct_state = self._state['accounts'][acct]
            snapshots = acct_state.get('daily_snapshots', [])

            # 오늘 스냅샷이 이미 있으면 업데이트, 없으면 추가
            if snapshots and snapshots[-1].get('date') == today:
                snapshots[-1]['nav'] = acct_state['nav']
                snapshots[-1]['n_positions'] = len(acct_state['positions'])
            else:
                snapshots.append({
                    'date': today,
                    'nav': acct_state['nav'],
                    'n_positions': len(acct_state['positions']),
                    'n_trades': acct_state['cumulative']['total_trades'],
                })
            acct_state['daily_snapshots'] = snapshots[-90:]  # 최근 90일만

        self._state['last_synced'] = datetime.now().isoformat()
        self._state['sync_count'] = self._state.get('sync_count', 0) + 1

        self._save()

        # 로그
        for acct in self.ACCOUNT_NAMES:
            a = self._state['accounts'][acct]
            n = len(a['positions'])
            nav = a['nav']
            trades = a['cumulative']['total_trades']
            pnl = a['cumulative']['total_pnl']
            if n > 0 or trades > 0:
                logger.info(
                    f"  📋 S4 {acct}: {n}종목, NAV=₩{nav:,.0f}, "
                    f"거래={trades}건, PnL=₩{pnl:+,.0f}")

        return self.get_account_summary()

    def get_account_summary(self) -> Dict:
        """계좌별 요약."""
        summary = {}
        for acct in self.ACCOUNT_NAMES:
            a = self._state['accounts'].get(acct, {})
            cum = a.get('cumulative', {})
            wins = cum.get('wins', 0)
            total = cum.get('total_trades', 0)
            summary[acct] = {
                'n_positions': len(a.get('positions', {})),
                'nav': a.get('nav', 0),
                'total_trades': total,
                'wins': wins,
                'win_rate': wins / max(total, 1),
                'total_pnl': cum.get('total_pnl', 0),
                'positions': list(a.get('positions', {}).values()),
            }
        return summary

    def get_advisory_status(self) -> Dict:
        """Go Live Advisory 상태 — 각 계좌의 현재 포지션과 권고 사항.

        Go Live 시:
          시스템 → Advisory 생성 (이 함수의 출력)
          사용자 → 실제 거래 실행
          시스템 → sync_from_shadow_portfolio()로 결과 추적
        """
        status = {}
        for acct in self.ACCOUNT_NAMES:
            a = self._state['accounts'].get(acct, {})
            positions = a.get('positions', {})
            status[acct] = {
                'current_holdings': [
                    {
                        'ticker': p['ticker'],
                        'name': p['name'],
                        'amount': p['amount'],
                        'entry_date': p.get('entry_date', ''),
                        'pnl_pct': p.get('pnl_pct', 0),
                    }
                    for p in positions.values()
                ],
                'n_holdings': len(positions),
                'nav': a.get('nav', 0),
                'last_trade_date': (
                    a['trade_history'][-1]['date']
                    if a.get('trade_history') else None
                ),
            }
        return status

    # ──────────────────────────────────────────
    # Shadow portfolio account 필드 보정
    # ──────────────────────────────────────────

    def backfill_account_fields(self):
        """shadow_portfolio의 S4 포지션/거래에 account 필드를 보정.

        position.account가 없는 S4 포지션에 ETF_ACCOUNT_MAP 기반으로 채움.
        """
        sp = self._load_shadow()
        updated = 0

        # 포지션 보정
        for pos_key, pos in sp.get('positions', {}).items():
            stream = pos_key.split(':')[0] if ':' in pos_key else pos.get('strategy', '')
            if stream != 'S4':
                continue
            if pos.get('account'):
                continue
            ticker = pos.get('ticker', pos_key.split(':')[-1] if ':' in pos_key else '')
            pos['account'] = ETF_ACCOUNT_MAP.get(ticker, 'BROKERAGE')
            updated += 1

        # 거래 기록 보정
        for t in sp.get('trade_history', []):
            stream = t.get('stream', t.get('stream_id', ''))
            if stream != 'S4':
                continue
            if t.get('account'):
                continue
            ticker = t.get('ticker', '')
            t['account'] = ETF_ACCOUNT_MAP.get(ticker, 'BROKERAGE')
            updated += 1

        if updated > 0:
            _RESULTS.mkdir(parents=True, exist_ok=True)
            atomic_write_json((_RESULTS / 'shadow_portfolio.json'),  sp, indent=2, ensure_ascii=False, default=str)
            logger.info(f"  ✅ S4 account 필드 보정: {updated}건")

        return updated

    # ──────────────────────────────────────────
    # I/O
    # ──────────────────────────────────────────

    def _load(self) -> Dict:
        state = None
        if _STATE_FILE.exists():
            try:
                state = json.loads(_STATE_FILE.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass

        if not state:
            return self._default_state()

        # ★ 방어: accounts 키가 누락된 경우 기본값으로 복원
        default = self._default_state()
        if 'accounts' not in state or not state['accounts']:
            state['accounts'] = default['accounts']
        else:
            for acct in self.ACCOUNT_NAMES:
                if acct not in state['accounts']:
                    state['accounts'][acct] = default['accounts'][acct]

        return state

    def _save(self):
        _RESULTS.mkdir(parents=True, exist_ok=True)
        self._state['last_updated'] = datetime.now().isoformat()
        atomic_write_json(_STATE_FILE, self._state, indent=2, ensure_ascii=False, default=str)

    def _load_shadow(self) -> Dict:
        path = _RESULTS / 'shadow_portfolio.json'
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        return {}


def sync_s4_accounts() -> Dict:
    """모듈 레벨 실행."""
    tracker = S4AccountTracker()
    tracker.backfill_account_fields()
    return tracker.sync_from_shadow_portfolio()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    result = sync_s4_accounts()
    logger.debug(json.dumps(result, indent=2, ensure_ascii=False, default=str))

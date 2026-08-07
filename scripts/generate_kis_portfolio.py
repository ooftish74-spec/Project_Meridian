#!/usr/bin/env python3
"""KIS API 기준 전체 스트림 포트폴리오 생성.

S1~S4 전 스트림의 시그널/포지션/거래이력을 KIS API 거래 구조에 맞춰
results/kis_portfolio.json으로 출력합니다.

데이터 흐름:
  shadow_portfolio.json (기존 거래이력)
  + latest_signals.json (오늘 시그널)
  + s4_advisory_recommendations.json (S4 계좌별)
  + kis_config.yaml (스트림별 거래 세션)
  → kis_portfolio.json (KIS API SSoT)

Usage:
    python3 scripts/generate_kis_portfolio.py
"""

import json
import logging
import sys
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
_RESULTS = _PROJECT_ROOT / 'results'

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

from config.dynamic_config import DynamicConfig
cfg = DynamicConfig()


def _load_json(name: str) -> dict:
    p = _RESULTS / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    return {}


def _classify_ticker(ticker: str) -> str:
    """종목코드로 ETF vs 개별주 분류."""
    etf_prefixes = cfg.get('premarket.etf_prefixes', ['3', '4'])
    if ticker and ticker[0] in etf_prefixes:
        return 'ETF'
    return 'STOCK'


def _get_tradeable_sessions(stream_id: str) -> list:
    """스트림별 거래 가능 세션."""
    import yaml
    kis_cfg_path = _PROJECT_ROOT / 'config' / 'kis_config.yaml'
    if kis_cfg_path.exists():
        try:
            kis_cfg = yaml.safe_load(kis_cfg_path.read_text())
            streams = kis_cfg.get('streams', {})
            s_cfg = streams.get(stream_id, {})
            return s_cfg.get('tradeable_sessions', ['regular'])
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    # defaults
    if stream_id in ('S1', 'S3'):
        return ['regular']
    return ['pre', 'regular', 'after']


def generate():
    """KIS API 기준 전체 포트폴리오 생성."""
    sp = _load_json('shadow_portfolio.json')
    signals = _load_json('latest_signals.json')
    s4_adv = _load_json('s4_advisory_recommendations.json')
    regime = _load_json('current_regime.json')

    positions = sp.get('positions', {})
    trade_history = sp.get('trade_history', [])
    from config.dynamic_config import DynamicConfig
    initial_capital = sp.get('initial_capital', float(DynamicConfig().get('portfolio.initial_capital', 1000000.0)))
    nav = sp.get('virtual_nav', initial_capital)
    cash = sp.get('cash', nav)

    # ── 스트림별 포지션 분류 ──
    stream_positions = defaultdict(list)
    for key, pos in positions.items():
        sid = key.split(':')[0] if ':' in key else pos.get('stream_id', '?')
        ticker = key.split(':')[-1] if ':' in key else pos.get('ticker', key)
        # S1/S3은 ETF 전용 스트림 → 스트림 기반 분류 (ticker prefix 무시)
        _is_etf_stream = sid in ('S1', 'S3')
        _asset_type = 'ETF' if _is_etf_stream else _classify_ticker(ticker)
        _exchange = 'SOR' if _is_etf_stream else ('NXT' if _asset_type == 'STOCK' else 'SOR')
        pos_entry = {
            'ticker': ticker,
            'name': pos.get('name', ''),
            'quantity': pos.get('quantity', 0),
            'avg_price': pos.get('avg_price', pos.get('entry_price', 0)),
            'current_price': pos.get('current_price', 0),
            'pnl_pct': round(pos.get('pnl_pct', 0), 2),
            'unrealized_pnl': round(pos.get('unrealized_pnl', 0)),
            'entry_date': pos.get('entry_date', pos.get('date', '')),
            'hold_days': pos.get('hold_days', 0),
            'confidence': pos.get('confidence', 0),
            'market_value': round(pos.get('current_value', pos.get('quantity', 0) * pos.get('current_price', 0))),
            'type': _asset_type,
            'exchange': _exchange,
        }
        stream_positions[sid].append(pos_entry)

    # ── 스트림별 거래이력 분류 ──
    stream_trades = defaultdict(list)
    for t in trade_history:
        sid = t.get('stream', t.get('stream_id', '?'))
        stream_trades[sid].append({
            'date': t.get('date', t.get('timestamp', ''))[:10],
            'action': t.get('action', ''),
            'ticker': t.get('ticker', ''),
            'name': t.get('name', ''),
            'price': t.get('price', 0),
            'quantity': t.get('quantity', 0),
            'amount': t.get('amount', 0),
            'pnl_pct': t.get('pnl_pct', 0),
            'realized_pnl': t.get('realized_pnl', 0),
            'sell_type': t.get('sell_type', ''),
            'reason': t.get('reason', ''),
        })

    # ── 스트림별 시그널 ──
    stream_signals = {}
    all_sigs = signals.get('signals', {})
    for sid in ['S1', 'S2', 'S3', 'S4']:
        sigs = all_sigs.get(sid, [])
        stream_signals[sid] = [{
            'ticker': s.get('ticker', ''),
            'name': s.get('name', ''),
            'action': s.get('action', s.get('direction', '')),
            'confidence': s.get('confidence', 0),
            'score': s.get('score', s.get('composite_score', 0)),
            'type': _classify_ticker(s.get('ticker', '')),
        } for s in sigs]

    # ── 스트림별 KPI 계산 ──
    def _calc_stream_kpi(sid):
        trades = stream_trades.get(sid, [])
        sells = [t for t in trades if t.get('action') == 'SELL']
        wins = [t for t in sells if t.get('realized_pnl', 0) > 0]
        losses = [t for t in sells if t.get('realized_pnl', 0) < 0]
        total_win = sum(t.get('realized_pnl', 0) for t in wins)
        total_loss = abs(sum(t.get('realized_pnl', 0) for t in losses))
        wr = len(wins) / max(len(sells), 1) * 100
        pf = total_win / max(total_loss, 1)
        realized = sum(t.get('realized_pnl', 0) for t in sells)
        unrealized = sum(p.get('unrealized_pnl', 0) for p in stream_positions.get(sid, []))
        invested = sum(p.get('market_value', 0) for p in stream_positions.get(sid, []))

        return {
            'total_trades': len(trades),
            'sell_count': len(sells),
            'win_count': len(wins),
            'loss_count': len(losses),
            'win_rate': round(wr, 1),
            'profit_factor': round(pf, 3),
            'realized_pnl': round(realized),
            'unrealized_pnl': round(unrealized),
            'total_pnl': round(realized + unrealized),
            'invested': round(invested),
            'avg_win_pct': round(sum(t.get('pnl_pct', 0) for t in wins) / max(len(wins), 1), 2),
            'avg_loss_pct': round(sum(t.get('pnl_pct', 0) for t in losses) / max(len(losses), 1), 2),
        }

    # ── 전체 포트폴리오 조립 ──
    portfolio = {
        'date': date.today().isoformat(),
        'timestamp': datetime.now().isoformat(),
        'kis_mode': 'shadow',
        'account': {
            'number': '4422****01',
            'type': 'Main (종합 위탁)',
            'initial_capital': initial_capital,
            'nav': nav,
            'cash': cash,
            'invested': round(nav - cash),
            'invest_pct': round((1 - cash / max(nav, 1)) * 100, 1),
        },
        'regime': regime.get('regime', 'unknown'),
        'streams': {},
    }

    for sid in ['S1', 'S2', 'S3', 'S4']:
        sessions = _get_tradeable_sessions(sid)
        kpi = _calc_stream_kpi(sid)
        n_positions = len(stream_positions.get(sid, []))
        n_signals = len(stream_signals.get(sid, []))

        stream_data = {
            'name': {
                'S1': 'Directional ETF',
                'S2': 'ML Alpha (개별주)',
                'S3': 'Sector Rotation ETF',
                'S4': 'Quality-Value (개별주)',
            }.get(sid, sid),
            'tradeable_sessions': sessions,
            'exchange': 'SOR' if sid in ('S1', 'S3') else 'auto (NXT pre/after, SOR regular)',
            'asset_type': 'ETF' if sid in ('S1', 'S3') else 'STOCK',
            'api_eligible': True,
            'positions': stream_positions.get(sid, []),
            'n_positions': n_positions,
            'signals': stream_signals.get(sid, []),
            'n_signals': n_signals,
            'kpi': kpi,
            'recent_trades': stream_trades.get(sid, [])[-30:],  # 최근 30건 (일일 Exit 다량 발생 대응)
        }

        # S4는 advisory 정보 추가
        if sid == 'S4' and s4_adv:
            stream_data['advisory'] = {
                'isa': s4_adv.get('recommendations', {}).get('ISA', {}),
                'irp': s4_adv.get('recommendations', {}).get('IRP', {}),
                'pension': s4_adv.get('recommendations', {}).get('PENSION', {}),
                'brokerage_auto': s4_adv.get('brokerage_auto', []),
                'summary': s4_adv.get('summary', {}),
            }
            stream_data['account_split'] = {
                'ISA': {'api': False, 'mode': 'advisory → 수동 집행'},
                'IRP': {'api': False, 'mode': 'advisory → 수동 집행'},
                'PENSION': {'api': False, 'mode': 'advisory → 수동 집행'},
                'BROKERAGE': {'api': True, 'mode': 'KIS API 자동매매 (Main 계좌)'},
            }

        portfolio['streams'][sid] = stream_data

    # ── 전체 요약 ──
    total_positions = sum(len(stream_positions.get(sid, [])) for sid in ['S1', 'S2', 'S3', 'S4'])
    total_signals = sum(len(stream_signals.get(sid, [])) for sid in ['S1', 'S2', 'S3', 'S4'])
    total_realized = sum(_calc_stream_kpi(sid)['realized_pnl'] for sid in ['S1', 'S2', 'S3', 'S4'])
    total_unrealized = sum(_calc_stream_kpi(sid)['unrealized_pnl'] for sid in ['S1', 'S2', 'S3', 'S4'])

    portfolio['summary'] = {
        'total_positions': total_positions,
        'total_signals': total_signals,
        'total_realized_pnl': total_realized,
        'total_unrealized_pnl': total_unrealized,
        'total_pnl': total_realized + total_unrealized,
        'cash_pct': round(cash / max(nav, 1) * 100, 1),
    }

    # ── 저장 ──
    out_path = _RESULTS / 'kis_portfolio.json'
    out_path.write_text(json.dumps(portfolio, indent=2, default=str, ensure_ascii=False))
    logger.info(f'✅ kis_portfolio.json 생성 완료')
    logger.info(f'   NAV: ₩{nav:,.0f} | Cash: {portfolio["account"]["invest_pct"]:.0f}% invested')
    logger.info(f'   Positions: {total_positions} | Signals: {total_signals}')
    logger.info(f'   PnL: ₩{total_realized + total_unrealized:+,.0f} (실현 ₩{total_realized:+,.0f})')

    for sid in ['S1', 'S2', 'S3', 'S4']:
        sd = portfolio['streams'][sid]
        k = sd['kpi']
        sessions = '/'.join(sd['tradeable_sessions'])
        print(f'   {sid} [{sessions}]: {sd["n_positions"]}pos / {sd["n_signals"]}sig / WR={k["win_rate"]:.0f}% / PF={k["profit_factor"]:.2f} / PnL=₩{k["total_pnl"]:+,.0f}')

    return portfolio


if __name__ == '__main__':
    generate()

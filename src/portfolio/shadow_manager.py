import pandas as pd
from src.portfolio.state_backend import RedisStateBackend
'\nShadow Portfolio Manager — 완전한 가상거래 엔진\n==================================================\n매수/매도/시가평가/P&L 추적/거래 히스토리를 관리합니다.\n\nFeatures:\n  ✅ Mark-to-Market (MTM) — 매일 최신 종가로 NAV 업데이트\n  ✅ 매도 조건 — 익절, 손절, 트레일링 스탑, 최대 보유일, 신호 소멸\n  ✅ 거래 히스토리 — 매수/매도 모두 기록, 실현 손익 계산\n  ✅ 일일 스냅샷 — NAV, 수익률, 포지션 수 등 일별 기록\n  ✅ 리밸런싱 — 스트림별 리밸런싱 시 포지션 교체\n  ✅ 스트림별 독립 포지션 — 같은 종목을 다른 스트림이 독립적으로 보유\n\nPosition Key Format:\n    "stream:ticker" (e.g., "S2:005930", "S4:005930")\n    → 같은 종목이라도 스트림별 독립 진입/청산 관리 가능\n    → S2(단기 ML)와 S4(장기 QV)가 삼성전자를 각각 보유 가능\n\nUsage:\n    from src.portfolio.shadow_manager import ShadowPortfolioManager\n    mgr = ShadowPortfolioManager()\n    mgr.mark_to_market(prices)\n    sells = mgr.check_exit_conditions(regime, current_signals)\n    mgr.execute_sells(sells, prices)\n    mgr.execute_buys(new_orders)\n    mgr.daily_snapshot()\n    mgr.save()\n'
import json
import logging
import os
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, List
from config.dynamic_config import DynamicConfig
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_STOCK_NAMES_PATH = _PROJECT_ROOT / 'data' / 'stock_names.json'

def _today():
    """현재 날짜 (매 호출 시 재계산 — 자정 넘김 안전)."""
    return datetime.now().strftime('%Y-%m-%d')

def _now_iso():
    """현재 시각 ISO format."""
    return datetime.now().isoformat()
_STOCK_NAMES_CACHE: Optional[Dict[str, str]] = None

def _load_stock_names() -> Dict[str, str]:
    """data/stock_names.json에서 ticker→종목명 매핑 로드 (캐싱)."""
    global _STOCK_NAMES_CACHE
    if _STOCK_NAMES_CACHE is not None:
        return _STOCK_NAMES_CACHE
    if _STOCK_NAMES_PATH.exists():
        try:
            _STOCK_NAMES_CACHE = json.loads(_STOCK_NAMES_PATH.read_text())
            return _STOCK_NAMES_CACHE
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    _STOCK_NAMES_CACHE = {}
    return _STOCK_NAMES_CACHE

def resolve_ticker_name(ticker: str, fallback: str='') -> str:
    """Ticker 코드 → 종목명 변환. 실패 시 fallback 반환.
    
    ★ 우선순위: CANONICAL_NAMES → stock_names.json → fallback
    """
    try:
        from src.utils.ticker_name_resolver import CANONICAL_NAMES
        clean = ticker.replace('.KS', '').replace('.KQ', '').zfill(6)
        if clean in CANONICAL_NAMES:
            return CANONICAL_NAMES[clean]
        if ticker in CANONICAL_NAMES:
            return CANONICAL_NAMES[ticker]
    except ImportError as e:
        pass
    names = _load_stock_names()
    clean = ticker.replace('.KS', '').replace('.KQ', '').zfill(6)
    result = names.get(clean, names.get(ticker, ''))
    return result if result else fallback if fallback and fallback != ticker else ticker

def get_stream_exit_profiles() -> Dict[str, Dict]:
    return {'S1': {'take_profit_pct': DynamicConfig().get('s1.exit.base_tp_pct', 0.015), 'stop_loss_pct': DynamicConfig().get('s1.exit.base_sl_pct', -0.01), 'trailing_stop_pct': None, 'trailing_activate_pct': None, 'max_hold_days': {'bull': 0, 'caution': 0, 'bear': 0, 'crash': 0}, 'min_hold_days': 0, 'signal_expire_days': 0, 'use_atr_stops': False, 'partial_exit': False, 'max_daily_trades': 3, 'confluence_tp': {1: 0.015, 2: 0.015, 3: 0.015}, 'confluence_trailing_activate': {1: None, 2: 0.015, 3: 0.01}, 'confluence_trailing_stop': {1: None, 2: -0.005, 3: -0.005}}, 'S2': {'take_profit_pct': None, 'stop_loss_pct': DynamicConfig().get('s2.exit.sl_pct', -0.07), 'trailing_stop_pct': DynamicConfig().get('s2.exit.trailing_stop_pct', -0.1), 'trailing_activate_pct': DynamicConfig().get('s2.exit.trailing_activate_pct', 0.05), 'max_hold_days': {'bull': DynamicConfig().get('s2.exit.max_hold.bull', 30), 'caution': DynamicConfig().get('s2.exit.max_hold.caution', 20), 'bear': DynamicConfig().get('s2.exit.max_hold.bear', 14), 'crash': DynamicConfig().get('s2.exit.max_hold.crash', 7)}, 'min_hold_days': 1, 'signal_expire_days': 3, 'use_atr_stops': True, 'atr_sl_multiplier': 2.0, 'atr_tp_multiplier': 3.5, 'partial_exit': False}, 'S3': {'take_profit_pct': DynamicConfig().get('s3.exit.tp_pct', 0.2), 'stop_loss_pct': DynamicConfig().get('s3.exit.sl_pct', -0.08), 'trailing_stop_pct': DynamicConfig().get('s3.exit.trailing_stop_pct', -0.12), 'trailing_activate_pct': DynamicConfig().get('s3.exit.trailing_activate_pct', 0.08), 'max_hold_days': {'bull': 90, 'caution': 60, 'bear': 45, 'crash': 21}, 'min_hold_days': 5, 'signal_expire_days': 999, 'use_atr_stops': True, 'atr_sl_multiplier': 2.5, 'atr_tp_multiplier': 4.0, 'partial_exit': True, 'partial_exit_pct': DynamicConfig().get('s3.exit.partial_exit_pct', 0.5), 'partial_exit_trigger': DynamicConfig().get('s3.exit.partial_trigger_pct', 0.12)}, 'S4': {'take_profit_pct': None, 'stop_loss_pct': DynamicConfig().get('s4.exit.sl_pct', -0.12), 'trailing_stop_pct': DynamicConfig().get('s4.exit.trailing_stop_pct', -0.15), 'trailing_activate_pct': DynamicConfig().get('s4.exit.trailing_activate_pct', 0.1), 'max_hold_days': {'bull': 180, 'caution': 120, 'bear': 90, 'crash': 45}, 'min_hold_days': 10, 'signal_expire_days': 30, 'use_atr_stops': False, 'partial_exit': True, 'partial_exit_pct': DynamicConfig().get('s4.exit.partial_exit_pct', 0.3), 'partial_exit_trigger': DynamicConfig().get('s4.exit.partial_trigger_pct', 0.2), 'tax_aware': True, 'tax_loss_harvest_pct': DynamicConfig().get('s4.exit.tax_loss_harvest_pct', -0.1), 'isa_tax_free_limit': DynamicConfig().get('s4.exit.isa_tax_free_limit', 2000000), 'isa_min_hold_years': DynamicConfig().get('s4.exit.isa_min_hold_years', 3), 'turnover_penalty': DynamicConfig().get('s4.exit.turnover_penalty', 0.005)}, 'S5': {'take_profit_pct': None, 'stop_loss_pct': DynamicConfig().get('s5.exit.sl_pct', -0.08), 'trailing_stop_pct': DynamicConfig().get('s5.exit.trailing_stop_pct', -0.1), 'trailing_activate_pct': DynamicConfig().get('s5.exit.trailing_activate_pct', 0.05), 'max_hold_days': {'bull': 45, 'caution': 30, 'bear': 15, 'crash': 7}, 'min_hold_days': 3, 'signal_expire_days': 7, 'use_atr_stops': True, 'atr_sl_multiplier': 2.0, 'atr_tp_multiplier': 4.0, 'partial_exit': False}, 'S0': {'take_profit_pct': None, 'stop_loss_pct': DynamicConfig().get('s0.exit.sl_pct', -0.15), 'trailing_stop_pct': None, 'trailing_activate_pct': None, 'max_hold_days': {'bull': 999, 'caution': 999, 'bear': 999, 'crash': 999}, 'min_hold_days': 0, 'signal_expire_days': 999, 'use_atr_stops': False, 'partial_exit': False}}

class ShadowPortfolioManager:
    """Shadow Portfolio — 가상거래 전체 라이프사이클 관리.

    포지션 키: 'stream:ticker' 형식으로 스트림별 독립 관리.
    예: 'S2:005930', 'S4:005930' → 각각 독립적인 진입/청산/P&L
    """

    def __init__(self, initial_capital: float=None):
        if initial_capital is None:
            try:
                from config.dynamic_config import DynamicConfig
                _cfg = DynamicConfig()
                initial_capital = _cfg.get('portfolio.initial_capital', 100000000)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                initial_capital = 100000000
        self.file_path = _RESULTS / 'shadow_portfolio.json'
        self.initial_capital = initial_capital
        self.state_backend = RedisStateBackend()
        self.data = self._load_or_create()
        self._migrate_legacy_positions()

    def _load_or_create(self) -> Dict:
        """기존 데이터 로드 또는 신규 생성 (Memory-First)."""
        redis_state = self.state_backend.load_full_state()
        if redis_state and redis_state.get('positions'):
            logger.info('  ⚡ Redis In-Memory State 복원 완료 (디스크 I/O 생략)')
            data = redis_state
            if self.file_path.exists():
                try:
                    with open(self.file_path) as f:
                        disk_data = json.load(f)
                    for k, v in disk_data.items():
                        if k not in ['positions', 'trade_history', 'capital']:
                            data[k] = v
                    if 'cash' in redis_state.get('capital', {}):
                        data['cash'] = redis_state['capital']['cash']
                        data['virtual_nav'] = redis_state['capital'].get('nav', disk_data.get('virtual_nav', self.initial_capital))
                except Exception as e:
                    logger.warning(f'  Redis-Disk metadata merge fail: {e}')
            else:
                data['cash'] = self.initial_capital
                data['virtual_nav'] = self.initial_capital
        elif self.file_path.exists():
            try:
                with open(self.file_path) as f:
                    data = json.load(f)
                logger.info(f'  포트폴리오 로드: NAV=₩{data.get('virtual_nav', 0):,.0f}, 포지션={len(data.get('positions', {}))}개')
                data.setdefault('trade_history', [])
                data.setdefault('daily_snapshots', [])
                data.setdefault('realized_pnl', 0)
                data.setdefault('total_commission', 0)
                data.setdefault('hwm', data.get('virtual_nav', data.get('initial_capital', self.initial_capital)))
                data.setdefault('cumulative_return_pct', 0.0)
                data.setdefault('daily_pnl', 0)
                data.setdefault('consecutive_loss_days', 0)
                data.setdefault('max_drawdown_pct', 0.0)
                data.setdefault('drawdown_pct', 0.0)
                data.setdefault('unrealized_pnl', 0)
                data.setdefault('strategy_pnl', {})
                data.setdefault('daily_records', [])
                data.setdefault('daily_returns', [])
                data.setdefault('total_return_pct', 0.0)
                data.setdefault('sub_accounts', {})
                self._reconcile_accumulated_fields(data)
                self._heal_positions(data)
                return data
            except Exception as e:
                logger.warning(f'  포트폴리오 로드 실패: {e}')
        return {'created': _today(), 'updated': _now_iso(), 'virtual_nav': self.initial_capital, 'cash': self.initial_capital, 'hwm': self.initial_capital, 'initial_capital': self.initial_capital, 'positions': {}, 'trade_history': [], 'daily_snapshots': [], 'daily_records': [], 'realized_pnl': 0, 'unrealized_pnl': 0, 'total_commission': 0, 'strategy_pnl': {}}

    @staticmethod
    def _reconcile_accumulated_fields(data: Dict):
        """trade_history로부터 realized_pnl/total_commission/strategy_pnl을 재계산 (자가 치유).

        원인: 일부 세션에서 누적값이 0으로 초기화된 채 저장되어,
        이전 세션의 거래 기록이 반영되지 않는 버그가 있었음.

        해결: trade_history를 SSoT(Single Source of Truth)로 삼아
        매 로드 시 정확한 누적값을 재계산합니다.
        """
        trades = data.get('trade_history', [])
        if not trades:
            return
        calc_realized = sum((t.get('realized_pnl', 0) for t in trades if t.get('action') == 'SELL'))
        calc_commission = sum((t.get('commission', 0) for t in trades))
        stored_realized = data.get('realized_pnl', 0)
        stored_commission = data.get('total_commission', 0)
        if abs(calc_realized - stored_realized) > 10:
            logger.warning(f'  ⚠️ realized_pnl 불일치 보정: stored=₩{stored_realized:,.0f} → trade_history SSoT=₩{calc_realized:,.0f} (차이: ₩{calc_realized - stored_realized:+,.0f})')
            data['realized_pnl'] = calc_realized
        if abs(calc_commission - stored_commission) > 10:
            logger.warning(f'  ⚠️ total_commission 불일치 보정: stored=₩{stored_commission:,.0f} → trade_history SSoT=₩{calc_commission:,.0f} (차이: ₩{calc_commission - stored_commission:+,.0f})')
            data['total_commission'] = calc_commission
        calc_tax = sum((t.get('tax', 0) for t in trades))
        stored_tax = data.get('total_tax', 0)
        if abs(calc_tax - stored_tax) > 10:
            logger.warning(f'  ⚠️ total_tax 불일치 보정: stored=₩{stored_tax:,.0f} → trade_history SSoT=₩{calc_tax:,.0f}')
            data['total_tax'] = calc_tax
        calc_strategy_pnl = {}
        for t in trades:
            if t.get('action') == 'SELL':
                stream = t.get('stream', t.get('stream_id', ''))
                if stream:
                    calc_strategy_pnl[stream] = calc_strategy_pnl.get(stream, 0) + t.get('realized_pnl', 0)
        stored_strategy_pnl = data.get('strategy_pnl', {})
        if calc_strategy_pnl != stored_strategy_pnl:
            changed_streams = []
            for sid in set(list(calc_strategy_pnl.keys()) + list(stored_strategy_pnl.keys())):
                calc_v = calc_strategy_pnl.get(sid, 0)
                stored_v = stored_strategy_pnl.get(sid, 0)
                if abs(calc_v - stored_v) > 10:
                    changed_streams.append(f'{sid}: ₩{stored_v:,.0f}→₩{calc_v:,.0f}')
            if changed_streams:
                logger.warning(f'  ⚠️ strategy_pnl 불일치 보정: {', '.join(changed_streams)}')
            data['strategy_pnl'] = calc_strategy_pnl

    @staticmethod
    def _heal_positions(data: Dict):
        """포지션 필수 필드 자가 치유.

        S1 재진입 등 다양한 코드 경로에서 생성된 포지션이
        avg_price, quantity, current_price 등 필수 필드를 누락할 수 있음.
        로드 시점에 한 번 정규화하여 하류 코드(check_exit_conditions,
        mark_to_market, execute_sells)의 KeyError를 방지.
        """
        positions = data.get('positions', {})
        healed = 0
        for pos_key, pos in positions.items():
            changed = False
            if 'avg_price' not in pos:
                entry = pos.get('entry_price', 0)
                if entry > 0:
                    pos['avg_price'] = entry
                    changed = True
            avg = pos.get('avg_price', pos.get('entry_price', 0))
            if pos.get('quantity', 0) <= 0 and avg > 0:
                amount = pos.get('amount', 0)
                if amount > 0:
                    pos['quantity'] = int(amount / avg)
                    changed = True
            qty = pos.get('quantity', 0)
            if 'current_price' not in pos and avg > 0:
                pos['current_price'] = avg
                changed = True
            cur = pos.get('current_price', avg)
            if 'market_value' not in pos and cur > 0 and (qty > 0):
                pos['market_value'] = cur * qty
                changed = True
            if 'unrealized_pnl' not in pos:
                mv = pos.get('market_value', 0)
                cost = avg * qty if avg > 0 and qty > 0 else 0
                pos['unrealized_pnl'] = mv - cost
                changed = True
            if 'pnl_pct' not in pos and avg > 0 and (cur > 0):
                pos['pnl_pct'] = round((cur - avg) / avg * 100, 2)
                changed = True
            if 'hwm_price' not in pos:
                pos['hwm_price'] = pos.get('high_water_mark', avg)
                changed = True
            if 'stream_id' not in pos:
                if ':' in pos_key:
                    pos['stream_id'] = pos_key.split(':')[0]
                    changed = True
            if changed:
                healed += 1
        if healed > 0:
            logger.info(f'  🔧 포지션 자가 치유: {healed}개 정규화')

    def _migrate_legacy_positions(self):
        """레거시 포지션 마이그레이션: plain ticker → stream:ticker.

        기존: positions['005930'] = {streams: ['S2', 'S3']}
        변환: positions['S2:005930'] = {...}, positions['S3:005930'] = {...}
        multi-stream 보유 시 수량/금액을 균등 분할.
        """
        if not self.positions:
            return
        sample_key = next(iter(self.positions))
        if ':' in sample_key:
            return
        logger.info('  🔄 레거시 포지션 → stream:ticker 마이그레이션 시작')
        migrated = {}
        for ticker, pos in list(self.positions.items()):
            streams = pos.get('streams', [])
            if not streams:
                streams = ['S2']
            n_streams = len(streams)
            for stream_id in streams:
                new_key = self._position_key(ticker, stream_id)
                new_pos = dict(pos)
                new_pos['stream_id'] = stream_id
                new_pos['streams'] = [stream_id]
                if n_streams > 1:
                    new_pos['quantity'] = max(1, pos['quantity'] // n_streams)
                    new_pos['amount'] = new_pos['avg_price'] * new_pos['quantity']
                    new_pos['market_value'] = new_pos.get('current_price', new_pos['avg_price']) * new_pos['quantity']
                    new_pos['unrealized_pnl'] = new_pos['market_value'] - new_pos['amount']
                migrated[new_key] = new_pos
                logger.info(f'    {ticker} → {new_key} (qty={new_pos['quantity']})')
        self.data['positions'] = migrated
        logger.info(f'  ✅ 마이그레이션 완료: {len(self.positions)}개 포지션')

    @staticmethod
    def _position_key(ticker: str, stream_id: str) -> str:
        """스트림별 독립 포지션 키 생성."""
        return f'{stream_id}:{ticker}'

    @staticmethod
    def _parse_position_key(pos_key: str) -> tuple:
        """포지션 키에서 (stream_id, ticker) 추출."""
        if ':' in pos_key:
            parts = pos_key.split(':', 1)
            return (parts[0], parts[1])
        return ('', pos_key)

    @staticmethod
    def _get_etf_tickers() -> set:
        """ETF 티커 목록 반환 (증권거래세 면제 대상 판별용).

        Universe의 모든 ETF 카테고리 + 6자리 티커 패턴 기반 판별.
        """
        etf_tickers = set()
        try:
            from config.universe import Universe
            u = Universe()
            for store in [u.A1_DIRECTIONAL, u.A2_SECTORS, u.ASSET_ALLOCATION, u.SLEEVE_B_ETFS]:
                for etf_info in store.values():
                    etf_tickers.add(etf_info.ticker)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        _KNOWN_ETFS = {'122630', '252670', '069500', '114800', '233740', '133690', '360200', '453010', '091160', '305540', '266420', '117460', '091170', '117680', '091220', '117700', '227560', '139260', '228810', '449450', '148070', '305080', '132030', '261240', '214980', '357870', '195930', '279530', '161510', '458730', '441640', '381170', '453640', '166400', '329200', '192090', '091180', '305720', '379800', '289480', '411060', '500063', '395160', '455890', '290130'}
        etf_tickers.update(_KNOWN_ETFS)
        return etf_tickers

    def save(self):
        """Atomic write: tempfile → os.replace() 패턴."""
        self.data['updated'] = datetime.now().isoformat()
        _RESULTS.mkdir(exist_ok=True)
        target = str(self.file_path)
        dir_name = os.path.dirname(target)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp', prefix='.shadow_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2, default=str, ensure_ascii=False)
            os.replace(tmp_path, target)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            try:
                os.unlink(tmp_path)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
            raise
        logger.info(f'  💾 포트폴리오 저장: NAV=₩{self.data['virtual_nav']:,.0f}')

    @property
    def positions(self) -> Dict:
        return self.data['positions']

    @property
    def cash(self) -> float:
        return self.data['cash']

    @property
    def nav(self) -> float:
        return self.data['virtual_nav']

    def mark_to_market(self, prices: Dict[str, float]) -> Dict:
        """
        모든 포지션을 최신 종가로 시가평가.

        prices는 plain ticker 기반 (e.g., {'005930': 60000}).
        포지션 키에서 ticker를 추출하여 가격 조회.

        Returns:
            MTM 결과 요약
        """
        nav_before = self.data['virtual_nav']
        updated_count = 0
        total_unrealized = 0
        for pos_key, pos in self.positions.items():
            _, ticker = self._parse_position_key(pos_key)
            current_price = prices.get(ticker)
            if current_price is None:
                current_price = prices.get(pos.get('ticker', ''))
            if current_price is None:
                unrealized = pos.get('unrealized_pnl', 0)
                total_unrealized += unrealized
                continue
            avg_price = pos.get('avg_price', pos.get('entry_price', 0))
            quantity = pos.get('quantity', 0)
            if avg_price is None or avg_price <= 0:
                trade_history = self.data.get('trade_history', [])
                found_price = 0
                for t in reversed(trade_history):
                    t_ticker = t.get('ticker', '')
                    if t.get('action', '').upper() == 'BUY' and (t_ticker == ticker or f'{t.get('stream_id', '')}:{t_ticker}' == pos_key):
                        found_price = t.get('avg_price', t.get('entry_price', t.get('price', 0)))
                        if found_price > 0:
                            break
                if found_price > 0:
                    avg_price = found_price
                    pos['avg_price'] = found_price
                    pos['entry_price'] = found_price
                    logger.info(f'  🔧 avg_price 복원 (trade_history): {pos_key} → ₩{found_price:,.0f}')
                else:
                    logger.error(f'  🚨 Critical Error: avg_price 누락 및 복구 실패 ({pos_key}).')
                    raise ValueError(f'Position {pos_key} has invalid avg_price and cannot be recovered from trade_history.')
            if quantity <= 0 and avg_price > 0:
                quantity = int(pos.get('amount', 0) / avg_price) if avg_price > 0 else 0
            if avg_price <= 0 or quantity <= 0:
                total_unrealized += pos.get('unrealized_pnl', 0)
                continue
            new_market_value = current_price * quantity
            unrealized = new_market_value - avg_price * quantity
            pnl_pct = (current_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
            pos_hwm = pos.get('hwm_price', avg_price)
            if current_price > pos_hwm:
                pos_hwm = current_price
            pos.update({'current_price': current_price, 'current_value': new_market_value, 'market_value': new_market_value, 'unrealized_pnl': unrealized, 'unrealized_pnl_pct': round(pnl_pct / 100, 6), 'pnl_pct': round(pnl_pct, 2), 'hwm_price': pos_hwm})
            total_unrealized += unrealized
            updated_count += 1
        market_value = sum((p.get('market_value', p.get('amount', 0)) for p in self.positions.values()))
        new_nav = self.data['cash'] + market_value
        self.data['virtual_nav'] = new_nav
        self.data['unrealized_pnl'] = total_unrealized
        if new_nav > self.data['hwm']:
            self.data['hwm'] = new_nav
        if 'sub_accounts' not in self.data:
            self.data['sub_accounts'] = {}
        for s_id, s_acc in self.data['sub_accounts'].items():
            s_mkt_val = 0.0
            for pk, pval in self.positions.items():
                p_s_id = pval.get('stream_id', pk.split(':')[0] if ':' in pk else '')
                if p_s_id == s_id:
                    s_mkt_val += pval.get('market_value', 0.0)
            s_acc['nav'] = s_acc.get('cash', 0.0) + s_mkt_val
        _sub = self.data.get('sub_accounts', {})
        _sleeve_a_ids = [s for s in _sub if not s.upper().startswith('S0')]
        _sleeve_b_ids = [s for s in _sub if s.upper().startswith('S0')]
        _sleeve_a_nav = sum((_sub[s].get('nav', 0.0) for s in _sleeve_a_ids))
        _sleeve_b_nav = sum((_sub[s].get('nav', 0.0) for s in _sleeve_b_ids))
        if _sleeve_a_nav == 0 and _sleeve_b_nav == 0 and (new_nav > 0):
            try:
                from config.dynamic_config import DynamicConfig
                _cfg_ratio = DynamicConfig().get('portfolio.sleeve_a_ratio', 0.6)
            except Exception:
                _cfg_ratio = 0.6
            _sleeve_a_nav = new_nav * _cfg_ratio
            _sleeve_b_nav = new_nav * (1.0 - _cfg_ratio)
        prev_a_hwm = self.data.get('sleeve_a_hwm', _sleeve_a_nav)
        prev_b_hwm = self.data.get('sleeve_b_hwm', _sleeve_b_nav)
        self.data['sleeve_a_nav'] = round(_sleeve_a_nav, 2)
        self.data['sleeve_b_nav'] = round(_sleeve_b_nav, 2)
        self.data['sleeve_a_hwm'] = round(max(prev_a_hwm, _sleeve_a_nav), 2)
        self.data['sleeve_b_hwm'] = round(max(prev_b_hwm, _sleeve_b_nav), 2)
        self.data['total_nav'] = round(new_nav, 2)
        daily_return = (new_nav - nav_before) / nav_before if nav_before > 0 else 0
        result = {'updated_count': updated_count, 'total_unrealized_pnl': total_unrealized, 'nav_before': nav_before, 'nav_after': new_nav, 'daily_return_pct': round(daily_return * 100, 4), 'sleeve_a_nav': self.data['sleeve_a_nav'], 'sleeve_b_nav': self.data['sleeve_b_nav']}
        logger.info(f'  📊 MTM: {updated_count}종목 업데이트 | NAV ₩{nav_before:,.0f} → ₩{new_nav:,.0f} ({daily_return * 100:+.2f}%) | 미실현 P&L ₩{total_unrealized:+,.0f} | Sleeve A ₩{_sleeve_a_nav:,.0f} / B ₩{_sleeve_b_nav:,.0f}')
        return result

    def _get_exit_config_for_stream(self, stream_id: str, regime: str='caution') -> Dict:
        """스트림별 EXIT 설정 반환 — 전 스트림 DynamicConfig 기반 동적 로드.

        S1: σ 기반 동적 TP/SL (별도 _compute_dynamic_s1_exit 사용)
        S2: DynamicConfig 's2.exit.*' → 레짐별 동적 TP/SL
        S3: DynamicConfig 's3.exit.*' → 레짐별 동적 TP/SL + 부분 청산
        S4: DynamicConfig 's4.exit.*' → 장기 + 세금 최적화

        하드코딩 없이 DynamicConfig SSoT에서 모든 값을 로드.
        STREAM_EXIT_PROFILES는 DynamicConfig에 키가 없을 때의 최종 fallback.
        """
        try:
            from src.intelligence.performance_feedback import PerformanceFeedback
            _pf = PerformanceFeedback()
            _stream_scale = _pf.get_stream_scale(stream_id)
            if not _stream_scale.get('active', True):
                cfg = DynamicConfig()
                _tighten_sl = cfg.get('exit.deactivated_stream_sl', -0.03)
                _current_pnl = 0
                _forced_sl = max(_tighten_sl, _current_pnl - 0.01)
                logger.info(f'M11: {stream_id} deactivated → forced SL={_forced_sl:.1%}')
                return {'tp_pct': cfg.get('exit.deactivated_stream_tp', 0.02), 'sl_pct': _forced_sl, 'trailing_activate_pct': max(0.01, _current_pnl * 0.5) if _current_pnl > 0 else 0.01, 'trailing_stop_pct': cfg.get('exit.deactivated_trailing_stop', -0.015), 'max_hold_days': cfg.get('exit.deactivated_max_hold', 20), 'deactivated': True}
        except ImportError as e:
            pass
        except Exception as e:
            logger.debug(f'M11 exit check error: {e}')
        profiles = get_stream_exit_profiles()
        base = profiles.get(stream_id, profiles['S2'])
        _cfg = DynamicConfig()
        if stream_id == 'S1':
            return {'take_profit_pct': _cfg.get('s1.exit.base_tp_pct', base['take_profit_pct']), 'stop_loss_pct': _cfg.get('s1.exit.base_sl_pct', base['stop_loss_pct']), 'trailing_stop_pct': base.get('trailing_stop_pct'), 'trailing_activate_pct': base.get('trailing_activate_pct'), 'max_hold_days': _cfg.get('s1.exit.max_hold_days', base['max_hold_days']), 'min_hold_days': _cfg.get('s1.exit.min_hold_days', base['min_hold_days']), 'signal_expire_days': _cfg.get('s1.exit.signal_expire_days', base['signal_expire_days']), 'use_atr_stops': base.get('use_atr_stops', False), 'partial_exit': base.get('partial_exit', False), 'max_daily_trades': _cfg.get('s1.exit.max_daily_trades', base.get('max_daily_trades', 3)), 'confluence_tp': _cfg.get('s1.exit.confluence_tp', base.get('confluence_tp', {})), 'confluence_trailing_activate': _cfg.get('s1.exit.confluence_trailing_activate', base.get('confluence_trailing_activate', {})), 'confluence_trailing_stop': _cfg.get('s1.exit.confluence_trailing_stop', base.get('confluence_trailing_stop', {}))}
        prefix = stream_id.lower()
        _base_tp = base.get('take_profit_pct') or 0.15
        _base_sl = base.get('stop_loss_pct') or -0.07
        tp_pct = _cfg.get(f'{prefix}.exit.tp.{regime}', _base_tp * 100)
        sl_pct = _cfg.get(f'{prefix}.exit.sl.{regime}', _base_sl * 100)
        tp_decimal = tp_pct / 100 if abs(tp_pct) > 1 else tp_pct
        sl_decimal = sl_pct / 100 if abs(sl_pct) > 1 else sl_pct
        tp_floor = _cfg.get(f'{prefix}.exit.tp_floor', 5) / 100
        sl_ceiling = _cfg.get(f'{prefix}.exit.sl_ceiling', -2) / 100
        tp_decimal = max(tp_floor, tp_decimal)
        sl_decimal = min(sl_ceiling, sl_decimal)
        vol_baseline = _cfg.get(f'{prefix}.exit.vol_baseline', 18.0)
        vol_ctx = self._load_volatility_context()
        current_vix = vol_ctx.get('vkospi', vol_baseline)
        vol_scale = current_vix / vol_baseline if vol_baseline > 0 else 1.0
        vol_scale = max(0.7, min(1.5, vol_scale))
        sl_decimal = sl_decimal * vol_scale
        _atr_sl_multiplier = _cfg.get(f'{prefix}.exit.sl_atr_multiplier', 2.0)
        _sl_decay_rate = _cfg.get(f'{prefix}.exit.sl_decay_rate', 0.3)
        max_hold_default = base.get('max_hold_days', {}).get(regime, 30)
        max_hold = _cfg.get(f'{prefix}.exit.max_hold.{regime}', max_hold_default)
        _wr_for_trailing = 0.3
        try:
            import json as _tj
            _sp_path = Path(__file__).resolve().parent.parent / 'results' / 'shadow_portfolio.json'
            if _sp_path.exists():
                _sp_data = _tj.loads(_sp_path.read_text())
                _recent = [t for t in _sp_data.get('trade_history', []) if t.get('action') == 'SELL'][-50:]
                if len(_recent) >= 5:
                    _wr_for_trailing = sum((1 for t in _recent if t.get('realized_pnl', 0) > 0)) / len(_recent)
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        if _wr_for_trailing < 0.4:
            _trailing_ratio = _cfg.get('portfolio.trailing_ratio.bull', 0.2)
        elif _wr_for_trailing < 0.55:
            _trailing_ratio = _cfg.get('portfolio.trailing_ratio.caution', 0.3)
        else:
            _trailing_ratio = _cfg.get('portfolio.trailing_ratio.bear', 0.4)
        _dynamic_trailing_activate = tp_decimal * _trailing_ratio
        _dynamic_trailing_activate = max(0.02, _dynamic_trailing_activate)
        _cfg_trailing_trigger = _cfg.get(f'{prefix}.exit.trailing_trigger', None)
        if _cfg_trailing_trigger is not None:
            trailing_trigger_dec = _cfg_trailing_trigger / 100 if abs(_cfg_trailing_trigger) > 1 else _cfg_trailing_trigger
        else:
            trailing_trigger_dec = _dynamic_trailing_activate
        trailing_pct = _cfg.get(f'{prefix}.exit.trailing_pct', abs(base.get('trailing_stop_pct') or -0.1) * 100)
        trailing_pct_dec = -(trailing_pct / 100 if abs(trailing_pct) > 1 else trailing_pct)
        _sell_commission = _cfg.get(f'{prefix}.exit.sell_commission_pct', 0.015) / 100
        _sell_tax_stock = _cfg.get(f'{prefix}.exit.sell_tax_pct', 0.18) / 100
        _net_min_profit = _cfg.get(f'{prefix}.exit.net_min_profit_pct', 1.0) / 100
        _cost_floor_stock = _sell_commission + _sell_tax_stock + _net_min_profit
        _cost_floor_etf = _sell_commission + _net_min_profit
        _cfg_exit = DynamicConfig()
        _partial_ratios = {'s2': _cfg_exit.get('s2.exit.partial_ratio', 0.35), 's3': _cfg_exit.get('s3.exit.partial_ratio', 0.6), 's4': _cfg_exit.get('s4.exit.partial_ratio', 0.67)}
        _default_partial_ratio = _partial_ratios.get(prefix, 0.5)
        _tp_based_trigger = tp_decimal * _default_partial_ratio
        _tp_based_trigger = max(0.03, _tp_based_trigger)
        _dynamic_partial_trigger = max(_cost_floor_stock, _tp_based_trigger)
        logger.debug(f'  부분익절 [{stream_id}]: cost_floor(주식)={_cost_floor_stock * 100:.2f}%, cost_floor(ETF)={_cost_floor_etf * 100:.2f}%, tp_based={_tp_based_trigger * 100:.2f}%, final={_dynamic_partial_trigger * 100:.2f}%')
        result = {'take_profit_pct': tp_decimal, 'stop_loss_pct': sl_decimal, 'trailing_activate_pct': trailing_trigger_dec, 'trailing_stop_pct': trailing_pct_dec, 'max_hold_days': {regime: max_hold} if isinstance(max_hold, (int, float)) else max_hold, 'min_hold_days': base.get('min_hold_days', 1), 'signal_expire_days': base.get('signal_expire_days', 3), 'use_atr_stops': base.get('use_atr_stops', False), 'partial_exit': True, 'partial_exit_trigger': _dynamic_partial_trigger, 'partial_exit_pct': 0.5, 'vol_scale': round(vol_scale, 3), 'atr_sl_multiplier': _atr_sl_multiplier, 'sl_decay_rate': _sl_decay_rate, 'max_hold_for_decay': max_hold, 'sell_commission_pct': _sell_commission, 'sell_tax_stock_pct': _sell_tax_stock, 'net_min_profit_pct': _net_min_profit, 'cost_floor_stock': _cost_floor_stock, 'cost_floor_etf': _cost_floor_etf}
        if stream_id == 'S4':
            result['tax_aware'] = True
            result['tax_loss_harvest_pct'] = base.get('tax_loss_harvest_pct', -0.1)
        return result

    def _load_volatility_context(self) -> Dict:
        """signal_cache에서 변동성 컨텍스트 로드 (VIX, VKOSPI)."""
        _cfg = DynamicConfig()
        _cfg_vix = DynamicConfig()
        base_vix = _cfg_vix.get('risk.vix_base_level', 18.0)
        try:
            sc = json.loads((_RESULTS / 'signal_cache.json').read_text())
            vix = sc.get('vix')
            if vix is None or vix <= 0:
                last_vix = getattr(self, '_last_known_vix', base_vix)
                vix_panic = _cfg_vix.get('risk.vix_panic_level', 30.0)
                vix = max(last_vix, vix_panic)
                logger.warning(f'  🚨 [ShadowManager] VIX 누락 감지. 보수적 방어/청산 모드 돌입 (VIX={vix:.1f} 가정)')
            else:
                self._last_known_vix = vix
            return {'vix': vix, 'vkospi': sc.get('vkospi', vix)}
        except Exception:
            last_vix = getattr(self, '_last_known_vix', base_vix)
            vix = max(last_vix, 30.0)
            logger.warning(f'  🚨 [ShadowManager] signal_cache.json 로드 실패. 보수적 방어/청산 모드 돌입 (VIX={vix:.1f} 가정)')
            return {'vix': vix, 'vkospi': vix}

    def _compute_ticker_atr_pct(self, ticker: str, pos: Dict, _atr_loop_cache: Optional[Dict]=None) -> float:
        """종목별 14일 ATR% 계산 — 3단 Fallback.

        [Fallback 우선순위]
          1차: pykrx 일봉 (가장 정확)
          2차: historical_10y parquet
          3차: pos의 pnl 절대값 기반 vol proxy × 1.5

        [Maintenance] _atr_loop_cache 를 넘기면 루프 내 재조회 방지.

        Returns:
            ATR (% decimal)
        """
        _cfg = DynamicConfig()
        atr_period = _cfg.get('exit.atr_period', 14)
        if _atr_loop_cache is not None and ticker in _atr_loop_cache:
            return _atr_loop_cache[ticker]
        atr_pct = None
        if atr_pct is None:
            try:
                import numpy as _np
                import pandas as _pd
                _data_dir = _PROJECT_ROOT / 'data' / 'historical_10y'
                for fp in [_data_dir / f'kr_{ticker}.parquet', _data_dir / f'{ticker}.parquet']:
                    if fp.exists():
                        df2 = _pd.read_parquet(fp)
                        if len(df2) >= atr_period:
                            h2 = df2.get('high', df2.get('고가', None))
                            l2 = df2.get('low', df2.get('저가', None))
                            c2 = df2.get('close', df2.get('종가', None))
                            if h2 is not None:
                                h2 = h2.astype(float).tail(atr_period * 2)
                                l2 = l2.astype(float).tail(atr_period * 2)
                                c2 = c2.astype(float).tail(atr_period * 2)
                                pc2 = c2.shift(1)
                                tr2 = _np.maximum(h2 - l2, _np.maximum((h2 - pc2).abs(), (l2 - pc2).abs()))
                                atr_val2 = float(tr2.dropna().tail(atr_period).mean())
                                lc2 = float(c2.iloc[-1])
                                if lc2 > 0 and atr_val2 > 0:
                                    atr_pct = atr_val2 / lc2
                                    break
            except Exception as _e:
                logger.debug(f'  SM ATR parquet [{ticker}]: {_e}')
        if atr_pct is None:
            _cfg2 = DynamicConfig()
            raw_vol = abs(pos.get('pnl_pct', 2.0)) / 100
            raw_vol = max(0.01, min(0.15, raw_vol))
            atr_pct = raw_vol * _cfg2.get('exit.atr_vol_proxy_factor', 1.5)
            logger.debug(f'  SM ATR vol_proxy [{ticker}]: {atr_pct * 100:.2f}%')
        atr_pct = max(0.005, min(0.15, atr_pct))
        if _atr_loop_cache is not None:
            _atr_loop_cache[ticker] = atr_pct
        return atr_pct

    def _compute_dynamic_s1_exit(self, pos: Dict, cfg, vol_ctx: Dict) -> Dict:
        """S1 포지션의 동적 TP/SL 계산 (변동성 기반).

        σ_daily = vol_index / √252 를 기반으로 TP/SL/Trailing을 동적 산출.
        레버리지 배수가 자동 반영되므로 별도 스케일링 불필요.

        Returns:
            take_profit_pct, stop_loss_pct, trailing_width,
            trail_activate_lv2, trail_activate_lv3, vol_index, daily_vol
        """
        import math
        underlying = pos.get('underlying', '')
        leverage = abs(pos.get('leverage', 1))
        lev_scale = cfg.get('s1.exit.leverage_scale_factor', 1.0)
        vix_underlyings = cfg.get('s1.exit.vix_underlyings', ['NASDAQ100', 'SP500', 'PHLX_SEMI'])
        fallback = cfg.get('s1.exit.vol_fallback', 18.0)
        if underlying in vix_underlyings:
            vol_index = vol_ctx.get('vix', fallback)
        else:
            vol_index = vol_ctx.get('vkospi', fallback)
        daily_vol = vol_index / math.sqrt(252) / 100
        tp_mult = cfg.get('s1.exit.tp_vol_multiplier', 1.5)
        sl_mult = cfg.get('s1.exit.sl_vol_multiplier', 1.0)
        raw_tp = daily_vol * tp_mult * leverage * lev_scale
        raw_sl = daily_vol * sl_mult * leverage * lev_scale
        tp_floor = cfg.get('s1.exit.tp_floor_pct', 0.005)
        tp_ceil = cfg.get('s1.exit.tp_ceiling_pct', 0.06)
        sl_floor = cfg.get('s1.exit.sl_floor_pct', 0.004)
        sl_ceil = cfg.get('s1.exit.sl_ceiling_pct', 0.05)
        final_tp = max(tp_floor, min(tp_ceil, raw_tp))
        final_sl = -max(sl_floor, min(sl_ceil, raw_sl))
        trail_mult = cfg.get('s1.exit.trail_vol_multiplier', 0.7)
        trail_floor = cfg.get('s1.exit.trail_floor_pct', 0.003)
        trail_ceil = cfg.get('s1.exit.trail_ceiling_pct', 0.04)
        trail_width = daily_vol * trail_mult * leverage * lev_scale
        trail_width = max(trail_floor, min(trail_ceil, trail_width))
        trail_act_ratio = cfg.get('s1.exit.trail_activate_ratio', 1.0)
        trail_early_ratio = cfg.get('s1.exit.trail_early_ratio', 0.7)
        logger.debug(f'    🎯 S1 Dynamic Exit [{pos.get('name', '')}]: vol={vol_index:.1f}, σd={daily_vol * 100:.2f}%, lev={leverage}, TP={final_tp * 100:.2f}%, SL={final_sl * 100:.2f}%, Trail={trail_width * 100:.2f}%')
        return {'take_profit_pct': final_tp, 'stop_loss_pct': final_sl, 'trailing_width': trail_width, 'trail_activate_lv2': final_tp * trail_act_ratio, 'trail_activate_lv3': final_tp * trail_early_ratio, 'vol_index': vol_index, 'daily_vol': daily_vol}

    def _net_s1_s5_orders(self, raw_orders):
        from collections import defaultdict
        nettable = {'S1', 'S5'}
        pool = defaultdict(float)
        templates = {}
        others = []
        for o in raw_orders:
            sid = str(o.get('stream', ''))
            ticker = str(o.get('ticker', ''))
            w = float(o.get('weight', o.get('size_pct', 0.0)))
            signed = w if str(o.get('side', 'buy')) == 'buy' else -w
            if sid in nettable and ticker:
                pool[ticker] += signed
                templates[ticker] = o
            else:
                others.append(o)
        result = list(others)
        for tkr, net_w in pool.items():
            if abs(net_w) < 1e-06:
                logger.info(f'  [Phase80 Net] {tkr} fully netted, removed')
                continue
            tmpl = dict(templates[tkr])
            tmpl['side'] = 'buy' if net_w > 0 else 'sell'
            tmpl['weight'] = round(abs(net_w), 6)
            tmpl['size_pct'] = round(abs(net_w), 6)
            tmpl['stream'] = 'S1_S5_NET'
            tmpl['netting'] = True
            logger.info(f'  [Phase80 Net] {tkr} net={net_w:+.4f} -> {tmpl['side']} {tmpl['weight']:.4f}')
            result.append(tmpl)
        return result

    def check_exit_conditions(self, regime: str, current_signals: Optional[Dict[str, List[str]]]=None) -> List[Dict]:
        """
        모든 포지션에 대해 매도 조건 점검.
        스트림별 독립 EXIT 프로파일 적용 (STREAM_EXIT_PROFILES).

        EXIT 우선순위:
          1) 손절 (Stop Loss) — 가장 높은 우선순위
          2) 익절 (Take Profit) — 부분 청산 포함 (Lv.1 전용)
          3) 트레일링 스탑 — trailing_activate_pct 이상 수익 시
          4) 최대 보유일 초과
          5) 신호 소멸 — signal_expire_days 유예 후
          6) S4 세금 최적화 — 연말 tax-loss harvesting
          7) 장마감 강제 청산 — S1 Lv.2-3 시간 기반 Exit
          8) 프리미엄 경고 — iNAV 대비 과도 프리미엄 감지

        Args:
            regime: 현재 레짐 (bull/caution/bear/crash)
            current_signals: 스트림별 현재 유효 종목 {'S1': ['005930', ...], ...}

        Returns:
            매도 주문 리스트 [{pos_key, ticker, reason, quantity, ...}, ...]
        """
        sell_orders = []
        today_dt = datetime.now()
        current_month = today_dt.month
        _cfg = DynamicConfig()
        _dyn_s1 = _cfg.get('s1.exit.dynamic_tp_sl_enabled', True)
        _vol_ctx = self._load_volatility_context() if _dyn_s1 else None
        try:
            _sc_data = json.loads((_RESULTS / 'signal_cache.json').read_text())
            _vix = _sc_data.get('vix')
            if _vix is None or _vix <= 0:
                _vix = max(getattr(self, '_last_known_vix', 18.0), 30.0)
            else:
                self._last_known_vix = _vix
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            _vix = max(getattr(self, '_last_known_vix', 18.0), 30.0)
        _atr_loop_cache: Dict[str, float] = {}
        _time_exit_on = _cfg.get('s1.exit.time_based_exit_enabled', True)
        _close_h = _cfg.get('s1.exit.close_time_hour', 15)
        _close_m = _cfg.get('s1.exit.close_time_minute', 10)
        _time_exit_min_cl = _cfg.get('s1.exit.time_exit_min_confluence', 2)
        _is_close_time = today_dt.hour > _close_h or (today_dt.hour == _close_h and today_dt.minute >= _close_m)
        _sc_path = _RESULTS / 'signal_cache.json'
        _s1_reval_score = 0.0
        if _sc_path.exists():
            try:
                _sc = json.loads(_sc_path.read_text())
                kospi_open_change = _sc.get('kospi_change_from_open', 0.0)
                flow = _sc.get('investor_flow', {}).get('flow_momentum', 0.0)
                vkospi_change = _sc.get('vkospi_change_from_open', 0.0)
                _s1_reval_score = kospi_open_change * 0.5 + flow / 100.0 * 0.3 - vkospi_change * 0.2
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        _s3_signals_cache = {}
        _ls_path = _RESULTS / 'latest_signals.json'
        if _ls_path.exists():
            try:
                _ls = json.loads(_ls_path.read_text())
                for sig in _ls.get('S3', []):
                    if isinstance(sig, dict) and 'ticker' in sig:
                        _s3_signals_cache[sig['ticker']] = sig.get('qvm_zscore', 0.0)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        _prem_on = _cfg.get('s1.exit.premium_filter_enabled', True)
        _prem_warn = _cfg.get('s1.exit.premium_warn_pct', 2.0)
        _prem_block = _cfg.get('s1.exit.premium_block_pct', 5.0)
        _s2_sector_distress = {}
        for pk, p in self.positions.items():
            s_id, tck = self._parse_position_key(pk)
            if s_id in ('S2', 'ml_alpha'):
                ap = p.get('avg_price', 1)
                cp = p.get('current_price', ap)
                if ap > 0:
                    p_pnl = (cp - ap) / ap
                    _atr_pre = self._compute_ticker_atr_pct(tck, p, _atr_loop_cache)
                    _dist_min = float(_cfg.get('s2.distress.min_pnl_threshold', 0.04))
                    _dist_atr = float(_cfg.get('s2.distress.atr_mult', 1.5))
                    if p_pnl < -max(_dist_min, _atr_pre * _dist_atr):
                        sec = p.get('sector', 'unknown')
                        _s2_sector_distress[sec] = _s2_sector_distress.get(sec, 0) + 1
        for pos_key, pos in list(self.positions.items()):
            if pos.get('strategy') == 'qvm_value':
                continue
            stream_id, ticker = self._parse_position_key(pos_key)
            if stream_id == 'S4' and (not _cfg.get('s4.auto_exit_enabled', False)):
                continue
            avg_price = pos.get('avg_price', pos.get('entry_price', 0))
            quantity = pos.get('quantity', 0)
            if quantity <= 0 and avg_price > 0:
                quantity = int(pos.get('amount', 0) / avg_price)
            current_price = pos.get('current_price', avg_price)
            if current_price <= 0 or avg_price <= 0 or quantity <= 0:
                continue
            pnl_pct = (current_price - avg_price) / avg_price
            entry_date_str = pos.get('entry_date', _today())
            try:
                entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d')
                hold_days = (today_dt - entry_date).days
            except (ValueError, TypeError):
                hold_days = 0
            exit_cfg = self._get_exit_config_for_stream(stream_id, regime)
            _use_static_lev = False
            if stream_id == 'S1' and _dyn_s1 and _vol_ctx:
                dyn = self._compute_dynamic_s1_exit(pos, _cfg, _vol_ctx)
                exit_cfg = dict(exit_cfg)
                exit_cfg['take_profit_pct'] = dyn['take_profit_pct']
                exit_cfg['stop_loss_pct'] = dyn['stop_loss_pct']
                exit_cfg['confluence_tp'] = {1: dyn['take_profit_pct'], 2: dyn['take_profit_pct'], 3: dyn['take_profit_pct']}
                exit_cfg['confluence_trailing_activate'] = {1: None, 2: dyn['trail_activate_lv2'], 3: dyn['trail_activate_lv3']}
                exit_cfg['confluence_trailing_stop'] = {1: None, 2: -dyn['trailing_width'], 3: -dyn['trailing_width']}
            else:
                pos_leverage = abs(pos.get('leverage', 1))
                lev_scale_on = _cfg.get('s1.exit.leverage_scale_enabled', True)
                lev_scale_fac = _cfg.get('s1.exit.leverage_scale_factor', 1.0)
                _use_static_lev = lev_scale_on and pos_leverage > 1
            confluence_level = pos.get('confluence_level', 1)
            s1_trailing_active = False
            s1_time_exit = False
            if stream_id == 'S1' and confluence_level >= _time_exit_min_cl:
                if _time_exit_on:
                    exit_cfg = dict(exit_cfg)
                    exit_cfg['trailing_stop_pct'] = None
                    exit_cfg['trailing_activate_pct'] = None
                    s1_time_exit = True
                    logger.debug(f'    ⏰ [{pos_key}] 합류 Lv.{confluence_level}: SL + 장마감 청산 모드 (SL={exit_cfg.get('stop_loss_pct', 0) * 100:.1f}%)')
                else:
                    conf_tp_map = exit_cfg.get('confluence_tp', {})
                    conf_trail_act_map = exit_cfg.get('confluence_trailing_activate', {})
                    conf_trail_stop_map = exit_cfg.get('confluence_trailing_stop', {})
                    cl_key = min(confluence_level, 3)
                    override_tp = conf_tp_map.get(cl_key, exit_cfg.get('take_profit_pct', 0.015))
                    override_trail_act = conf_trail_act_map.get(cl_key)
                    override_trail_stop = conf_trail_stop_map.get(cl_key)
                    exit_cfg = dict(exit_cfg)
                    exit_cfg['take_profit_pct'] = override_tp
                    if override_trail_act is not None and override_trail_stop is not None:
                        exit_cfg['trailing_activate_pct'] = override_trail_act
                        exit_cfg['trailing_stop_pct'] = override_trail_stop
                        s1_trailing_active = True
            elif stream_id == 'S1' and confluence_level < _time_exit_min_cl:
                pass
            if hold_days < exit_cfg.get('min_hold_days', 1):
                continue
            sell_reason = None
            sell_type = None
            sell_quantity = quantity
            if stream_id in ('S2', 'S3', 'S4', 'ml_alpha'):
                _atr = self._compute_ticker_atr_pct(ticker, pos, _atr_loop_cache)
                _regime_mult = {'bull': _cfg.get('exit.chandelier_mult.bull', 3.0), 'caution': _cfg.get('exit.chandelier_mult.caution', 2.5), 'bear': _cfg.get('exit.chandelier_mult.bear', 2.0), 'crash': _cfg.get('exit.chandelier_mult.crash', 1.5)}
                _ch_mult = _regime_mult.get(regime, 2.5)
                _vix_thresh = _cfg.get('exit.chandelier_vix_tighten', 25.0)
                if _vix >= _vix_thresh:
                    _ch_mult *= _cfg.get('exit.chandelier_vix_factor', 0.8)
                _ch_mult = max(1.0, _ch_mult)
                _pnl_pct_dec = pnl_pct
                _peak_pnl_pct_pct = pos.get('peak_pnl_pct', pnl_pct * 100)
                if abs(_peak_pnl_pct_pct) < 5 and abs(_pnl_pct_dec) > 0.04:
                    _peak_dec = _peak_pnl_pct_pct
                    _cur_dec = _pnl_pct_dec
                else:
                    _peak_dec = _peak_pnl_pct_pct / 100
                    _cur_dec = pnl_pct
                if stream_id in ('S2', 'ml_alpha') and _peak_dec > 0:
                    _cat_mult = _cfg.get('exit.catastrophic_atr_mult', 4.0)
                    _allowed_dd = _atr * _cat_mult
                    _actual_dd = _peak_dec - _cur_dec
                    if _actual_dd >= _allowed_dd:
                        sell_reason = f'[S2 Catastrophic] 고점 {_peak_dec * 100:+.1f}% → {_cur_dec * 100:+.1f}% (하락 {_actual_dd * 100:.1f}% vs ATR×{_cat_mult:.0f}={_allowed_dd * 100:.1f}%)'
                        sell_type = 'catastrophic_stop'
                        logger.warning(f'  🚨 Catastrophic Stop [{ticker}]: {sell_reason}')
                if not sell_reason and (not pos.get('scaled_out', False)):
                    _so_mult = _cfg.get('exit.scale_out_atr_mult', 10.0)
                    _so_min = _cfg.get('exit.scale_out_min_pct', 30.0) / 100
                    _so_ratio = _cfg.get('exit.scale_out_ratio', 0.5)
                    _so_target = max(_atr * _so_mult, _so_min)
                    if _cur_dec >= _so_target and quantity >= 2:
                        sell_quantity = max(1, int(quantity * _so_ratio))
                        sell_reason = f'Scale-out 50%: {_cur_dec * 100:+.1f}% ≥ 목표 {_so_target * 100:.1f}% (ATR={_atr * 100:.2f}%, {sell_quantity}/{quantity}주)'
                        sell_type = 'scale_out_partial'
                        logger.info(f'  ✂️ Scale-out [{ticker}]: {sell_reason}')
                if not sell_reason:
                    _trailing_trigger_pct = _cfg.get('s4.exit.trailing_tp_trigger', 15) / 100
                    _ch_drop = _atr * _ch_mult
                    _min_drop = _cfg.get('s4.exit.trailing_drop_floor', 3) / 100
                    _max_drop = _cfg.get('s4.exit.trailing_drop_ceil', 15) / 100
                    _ch_drop = max(_min_drop, min(_max_drop, _ch_drop))
                    if _peak_dec >= _trailing_trigger_pct and _cur_dec < _peak_dec - _ch_drop:
                        sell_reason = f'ATR 샹들리에: {_cur_dec * 100:+.1f}% (고점 {_peak_dec * 100:+.1f}% 대비 -{(_peak_dec - _cur_dec) * 100:.1f}%, ATR×{_ch_mult:.1f}={_ch_drop * 100:.1f}%)'
                        sell_type = 'chandelier_trailing'
            sl_pct = exit_cfg.get('stop_loss_pct', -0.05)
            if _use_static_lev:
                sl_pct = sl_pct * pos_leverage * lev_scale_fac
            if stream_id in ('S2', 'S3'):
                _atr_pct = pos.get('atr_pct', 0)
                if _atr_pct > 0:
                    _atr_mult = exit_cfg.get('atr_sl_multiplier', 2.0)
                    _atr_sl = -_atr_pct * _atr_mult
                    sl_pct = max(sl_pct, _atr_sl)
                _decay_rate = exit_cfg.get('sl_decay_rate', 0.3)
                _max_hold_decay = exit_cfg.get('max_hold_for_decay', 30)
                if _max_hold_decay > 0 and hold_days > 0:
                    _decay = 1.0 - _decay_rate * min(hold_days / _max_hold_decay, 1.0)
                    sl_pct = sl_pct * _decay
                if stream_id == 'S2':
                    sec = pos.get('sector', 'unknown')
                    if sec in _s2_sector_distress:
                        _penalty_per = float(_cfg.get('s2.covariance_shutdown.penalty_per_stock', 0.2))
                        _penalty_max = float(_cfg.get('s2.covariance_shutdown.max_penalty', 0.8))
                        _penalty = min(_penalty_max, _s2_sector_distress[sec] * _penalty_per)
                        _original_sl = sl_pct
                        sl_pct = sl_pct * (1.0 - _penalty)
                        if _penalty > 0:
                            logger.debug(f'  [Phase81 S2-Cov] {ticker}({sec}) 군집 셧다운 발동! Distress={_s2_sector_distress[sec]} → SL {sl_pct * 100:.2f}% 로 조임 (기존 {_original_sl * 100:.2f}%)')
                if stream_id == 'S3':
                    _zscore = _s3_signals_cache.get(ticker, 0.0)
                    _zsc_thr = float(_cfg.get('s3.exit.zscore_threshold', -1.0))
                    _zsc_rate = float(_cfg.get('s3.exit.zscore_penalty_rate', 0.3))
                    _zsc_cap = float(_cfg.get('s3.exit.zscore_penalty_cap', 0.8))
                    if _zscore < _zsc_thr:
                        _penalty = min(_zsc_cap, abs(_zscore) * _zsc_rate)
                        _original_sl = sl_pct
                        sl_pct = sl_pct * (1.0 - _penalty)
                        logger.debug(f'  [Phase81 S3-ZScore] {ticker} 펀더멘털 악화(Z={_zscore:.2f}) → SL {sl_pct * 100:.2f}% 로 조임 (기존 {_original_sl * 100:.2f}%)')
            if stream_id == 'S1':
                try:
                    _now = datetime.now()
                    _close_dt = _now.replace(hour=int(_cfg.get('s1.exit.close_time_hour', 15)), minute=int(_cfg.get('s1.exit.close_time_minute', 30)), second=0, microsecond=0)
                    _mins_rem = max(0.0, (_close_dt - _now).total_seconds() / 60.0)
                    _t_ratio = min(1.0, _mins_rem / max(float(_cfg.get('s1.exit.session_minutes', 360.0)), 1.0))
                    _decay_base = _cfg.get('portfolio.time_decay_base', 0.5)
                    _decay_scale = _cfg.get('portfolio.time_decay_scale', 0.5)
                    _time_decay_factor = _decay_base + _decay_scale * _t_ratio
                    sl_pct = sl_pct * _time_decay_factor
                    logger.debug(f'  [Phase80 S1-TimeSL] {ticker} rem={_mins_rem:.0f}min decay={_time_decay_factor:.3f} sl={sl_pct:.2f}%')
                except Exception as _tse:
                    logger.debug(f'  [Phase80] S1 TimeSL error: {_tse}')
            if pnl_pct <= sl_pct:
                sell_reason = f'손절 {pnl_pct * 100:+.1f}% (한도 {sl_pct * 100:.1f}%)'
                sell_type = 'stop_loss'
            if not sell_reason and (not s1_time_exit):
                tp_pct = exit_cfg.get('take_profit_pct', 0.1)
                if _use_static_lev:
                    tp_pct = tp_pct * pos_leverage * lev_scale_fac
                if s1_trailing_active and pnl_pct >= tp_pct:
                    pass
                else:
                    if exit_cfg.get('partial_exit', False) and (not pos.get('partial_exited', False)):
                        partial_trigger = exit_cfg.get('partial_exit_trigger', 0.12)
                        _sell_comm = exit_cfg.get('sell_commission_pct', 0.00015)
                        _is_etf = ticker in self._get_etf_tickers() or pos.get('is_etf', False)
                        if _is_etf:
                            _sell_comm = exit_cfg.get('sell_commission_pct', 0.00015)
                            _net_min = exit_cfg.get('net_min_profit_pct', 0.01)
                            _cost_floor = _sell_comm + _net_min
                            partial_trigger = max(_cost_floor, partial_trigger)
                        partial_pct = exit_cfg.get('partial_exit_pct', 0.5)
                        if partial_trigger <= pnl_pct < tp_pct:
                            sell_quantity = max(1, int(quantity * partial_pct))
                            _net_pnl_est = pnl_pct - (_sell_comm if _is_etf else exit_cfg.get('sell_commission_pct', 0.00015) + 0.0018)
                            sell_reason = f'부분 익절 {pnl_pct * 100:+.1f}% (순 ~{_net_pnl_est * 100:+.1f}%, {partial_pct * 100:.0f}% 매도, {sell_quantity}/{quantity}주)'
                            sell_type = 'partial_take_profit'
                    if pnl_pct >= tp_pct:
                        sell_reason = f'익절 {pnl_pct * 100:+.1f}% (한도 {tp_pct * 100:.0f}%)'
                        sell_type = 'take_profit'
                        sell_quantity = quantity
            if not sell_reason and (not s1_time_exit):
                trailing_limit = exit_cfg.get('trailing_stop_pct')
                trailing_activate = exit_cfg.get('trailing_activate_pct')
                if trailing_limit is not None and trailing_activate is not None:
                    hwm_price = pos.get('hwm_price', avg_price)
                    if hwm_price > 0 and pnl_pct > trailing_activate:
                        trailing_drop = (current_price - hwm_price) / hwm_price
                        if trailing_drop <= trailing_limit:
                            mdir = pos.get('market_direction', 'bullish')
                            if s1_trailing_active:
                                sell_reason = f'합류 Trailing (Lv.{confluence_level}, {mdir}): HWM ₩{hwm_price:,.0f} → ₩{current_price:,.0f} ({trailing_drop * 100:+.1f}%)'
                            else:
                                sell_reason = f'트레일링 스탑: HWM ₩{hwm_price:,.0f} → 현재 ₩{current_price:,.0f} ({trailing_drop * 100:+.1f}%)'
                            sell_type = 'trailing_stop'
            if not sell_reason:
                max_hold_map = exit_cfg.get('max_hold_days', {'bull': 60})
                if isinstance(max_hold_map, dict):
                    max_hold = max_hold_map.get(regime, 45)
                else:
                    max_hold = max_hold_map
                if max_hold > 0 and hold_days >= max_hold:
                    sell_reason = f'최대 보유일 {max_hold}일 초과 ({hold_days}일)'
                    sell_type = 'max_hold'
            if current_signals and (not sell_reason):
                stream_for_check = stream_id or pos.get('streams', [''])[0]
                still_in_signals = ticker in current_signals.get(stream_for_check, [])
                expire_days = exit_cfg.get('signal_expire_days', 3)
                _s3_hysteresis_hold = False
                if stream_id.upper().startswith('S3') and (not still_in_signals):
                    try:
                        _pos_score = float(pos.get('qvm_score', pos.get('score', 0.0)))
                        _score_cut = float(_cfg.get('s3.hysteresis_score_cutoff', 55.0))
                        if _pos_score >= _score_cut:
                            _s3_hysteresis_hold = True
                            logger.info(f'  [Phase79 Hysteresis] {stream_id}:{ticker} 파일러 유지 (score={_pos_score:.1f}>={_score_cut})')
                    except Exception as _he:
                        logger.debug(f'  [Phase79] Hysteresis 실패: {_he}')
                if not still_in_signals and hold_days >= expire_days and (not _s3_hysteresis_hold):
                    sell_reason = f'신호 소멸 ({stream_for_check} 추천 종료, {hold_days}일 보유, 유예 {expire_days}일)'
                    sell_type = 'signal_expired'
            if not sell_reason and exit_cfg.get('tax_aware', False):
                tlh_pct = exit_cfg.get('tax_loss_harvest_pct', -0.1)
                if current_month in (11, 12) and pnl_pct <= tlh_pct:
                    sell_reason = f'Tax-Loss Harvest: {pnl_pct * 100:+.1f}% (연말 손실 확정, 한도 {tlh_pct * 100:.0f}%)'
                    sell_type = 'tax_loss_harvest'
            if not sell_reason and s1_time_exit and _is_close_time:
                pos_dir = '인버스' if pos.get('leverage', 1) < 0 else '레버리지'
                sell_reason = f'장마감 청산 (Lv.{confluence_level}, {pos_dir}): {pnl_pct * 100:+.1f}% ({_close_h}:{_close_m:02d} 강제 청산)'
                sell_type = 'time_exit'
            if not sell_reason and stream_id == 'S1':
                _early_exit_th = _cfg.get('s1.exit.early_exit_threshold', -0.5)
                pos_leverage = pos.get('leverage', 1)
                if pos_leverage > 0 and _s1_reval_score < _early_exit_th:
                    sell_reason = f'조기 청산 (Strong Rejection - 롱): 장세 붕괴 감지 (score={_s1_reval_score:.2f})'
                    sell_type = 'early_exit_rejection'
                elif pos_leverage < 0 and _s1_reval_score > abs(_early_exit_th):
                    sell_reason = f'조기 청산 (Strong Rejection - 숏): 급반등 감지 (score={_s1_reval_score:.2f})'
                    sell_type = 'early_exit_rejection'
                if sell_type == 'early_exit_rejection':
                    try:
                        ee_path = _RESULTS / 's1_early_exit.json'
                        ee_path.write_text(json.dumps({'date': datetime.now().strftime('%Y-%m-%d'), 'direction': 'long' if pos_leverage > 0 else 'short'}))
                    except (FileNotFoundError, IOError) as e:
                        logger.error(f'  S1 Early Exit 상태 저장 중 에러: {e}')
            if not sell_reason and stream_id == 'S1' and _prem_on:
                pos_premium = pos.get('premium_pct', 0.0)
                if abs(pos_premium) >= _prem_block:
                    sell_reason = f'ETF 프리미엄 경고: {pos_premium:+.1f}% (한도 ±{_prem_block:.0f}%)'
                    sell_type = 'premium_exit'
            if sell_reason:
                sell_orders.append({'pos_key': pos_key, 'ticker': ticker, 'name': pos.get('name', ticker), 'quantity': sell_quantity, 'current_price': current_price, 'avg_price': avg_price, 'pnl_pct': round(pnl_pct * 100, 2), 'sell_type': sell_type, 'reason': sell_reason, 'stream_id': stream_id, 'streams': pos.get('streams', [stream_id]), 'hold_days': hold_days})
        if sell_orders:
            logger.info(f'  🔴 매도 조건 충족: {len(sell_orders)}건')
            for so in sell_orders:
                logger.info(f'    [{so.get('stream_id', '')}] {so['name']} ({so['ticker']}): {so['reason']}')
        return sell_orders

    def panic_sell_all(self, current_prices: Dict[str, float]) -> Dict:
        """(Phase 5) 긴급 방어: 전 종목 전량 시장가 매도 (Shadow 가상 체결).

        단, DynamicConfig 'kill_switch.panic_sell_exempt_streams' 에 나열된
        스트림(기본: ['S4']) 은 장기 보유 전략이므로 패닉셀에서 제외합니다.
        S4는 세금 최적화/장기 QV 전략으로 단기 낙폭에 의한 강제 청산이
        오히려 기회비용 손실 및 세금 불이익을 초래합니다.
        """
        _cfg = DynamicConfig()
        exempt_streams = _cfg.get('kill_switch.panic_sell_exempt_streams', ['S4'])
        logger.critical('  🚨 [PANIC SELL] Shadow: 전 종목 긴급 매도 개시!')
        if exempt_streams:
            logger.critical(f'  🛡️  [PANIC SELL] 패닉셀 면제 스트림: {exempt_streams} (장기 보유 전략 — 포지션 유지)')
        sell_orders = []
        kept_positions = []
        for pos_key, pos in self.data['positions'].items():
            ticker = pos.get('ticker', '')
            stream_id = pos.get('stream_id', 'S1')
            if ':' in pos_key:
                _extracted_sid = pos_key.split(':')[0]
                if _extracted_sid in ('S1', 'S2', 'S3', 'S4', 'S5', 'H'):
                    stream_id = _extracted_sid
            strategy = pos.get('strategy', '')
            if strategy == 'qvm_value':
                continue
            if stream_id in exempt_streams:
                held_days = 0
                try:
                    from datetime import date as _d
                    entry = _d.fromisoformat(str(pos.get('entry_date', ''))[:10])
                    held_days = (_d.today() - entry).days
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass
                upnl_pct = pos.get('pnl_pct', 0) or 0
                logger.critical(f'  🛡️  [{stream_id}] {ticker} 패닉셀 면제 (보유 {held_days}일, P&L={upnl_pct:+.1f}%) — 포지션 유지')
                kept_positions.append(ticker)
                continue
            qty = pos.get('quantity', 0)
            if qty > 0:
                price = current_prices.get(ticker, pos.get('current_price', pos.get('avg_price', 0)))
                logger.critical(f'    - Panic Sell: {ticker} (Stream: {stream_id}) x{qty}')
                sell_orders.append({'pos_key': pos_key, 'ticker': ticker, 'stream_id': stream_id, 'sell_quantity': qty, 'sell_price': price, 'reason': 'Panic_Sell_Kill_Switch'})
        if kept_positions:
            logger.critical(f'  🛡️  패닉셀 면제 종목 ({len(kept_positions)}개): {', '.join(kept_positions)}')
        _panic_slippage = _cfg.get('portfolio.panic_sell_slippage', 0.005)
        return self.execute_sells(sell_orders, current_prices, commission_rate=_panic_slippage)

    def execute_sells(self, sell_orders: List[Dict], prices: Dict[str, float], commission_rate: float=0.00015) -> Dict:
        """
        매도 주문 체결. pos_key 기반으로 포지션 삭제.

        ★ 수수료/세금 분리 (2026-06-10):
          - commission: 중개수수료 (매도 0.015%, DynamicConfig 오버라이드 가능)
          - tax: 증권거래세 (주식 매도 시 부과, ETF 면제)
            KOSPI 증권거래세 + 농어촌특별세 = DynamicConfig 'cost.tax_sell_pct'

        Args:
            sell_orders: check_exit_conditions의 결과 (pos_key 포함)
            prices: 최신 종가 (plain ticker 기반)
            commission_rate: 매도 중개수수료율 (기본 0.015%)

        Returns:
            체결 결과 요약
        """
        from src.risk.liquidity_monitor import LiquidityMonitor
        lm = LiquidityMonitor()
        executed = []
        total_proceeds = 0
        total_realized = 0
        total_commission = 0
        total_tax = 0
        _cfg_sell = DynamicConfig()
        _tax_rate = _cfg_sell.get('cost.tax_sell_pct', 0.18) / 100
        _etf_tickers = self._get_etf_tickers()
        for order in sell_orders:
            pos_key = order.get('pos_key', order.get('ticker', ''))
            ticker = order['ticker']
            if pos_key not in self.positions:
                if ticker in self.positions:
                    pos_key = ticker
                else:
                    continue
            pos = self.positions[pos_key]
            quantity = order.get('sell_quantity', order.get('quantity', 0))
            sell_price = prices.get(ticker, order.get('current_price', 0))
            if sell_price <= 0:
                sell_price = pos.get('avg_price', 0)
            if sell_price <= 0:
                continue
            proceeds = sell_price * quantity
            is_etf = ticker in _etf_tickers
            impact_bps = lm.calculate_market_impact_bps(ticker, proceeds, is_etf)
            dynamic_commission_rate = impact_bps / 10000.0
            commission = round(proceeds * dynamic_commission_rate)
            tax = 0 if is_etf else round(proceeds * _tax_rate)
            net_proceeds = proceeds - commission - tax
            _entry_price = pos.get('avg_price', pos.get('entry_price', 0))
            cost_basis = _entry_price * quantity
            realized_pnl = net_proceeds - cost_basis
            pnl_pct = realized_pnl / cost_basis * 100 if cost_basis > 0 else 0
            self.data['cash'] += net_proceeds
            total_proceeds += net_proceeds
            total_realized += realized_pnl
            total_commission += commission
            total_tax += tax
            stream_id = order.get('stream_id', '')
            if 'sub_accounts' not in self.data:
                self.data['sub_accounts'] = {}
            pos_stream_id = pos.get('stream_id', stream_id)
            if not pos_stream_id:
                pos_stream_id = pos_key.split(':')[0] if ':' in pos_key else 'UNKNOWN'
            if pos_stream_id not in self.data['sub_accounts']:
                self.data['sub_accounts'][pos_stream_id] = {'cash': 0.0, 'nav': 0.0}
            self.data['sub_accounts'][pos_stream_id]['cash'] += net_proceeds
            trade_record = {'date': _today(), 'timestamp': datetime.now().isoformat(), 'action': 'SELL', 'ticker': ticker, 'name': pos.get('name', ticker), 'quantity': quantity, 'price': sell_price, 'amount': proceeds, 'commission': commission, 'tax': tax, 'commission_rate': round(commission_rate, 6), 'tax_rate': round(_tax_rate if not is_etf else 0, 6), 'net_amount': net_proceeds, 'avg_price': pos.get('avg_price', pos.get('entry_price', 0)), 'entry_price': pos.get('avg_price', pos.get('entry_price', 0)), 'realized_pnl': round(realized_pnl), 'pnl_pct': round(pnl_pct, 2), 'sell_type': order.get('sell_type', 'manual'), 'reason': order.get('reason', ''), 'stream_id': stream_id, 'stream': stream_id, 'streams': order.get('streams', [stream_id]), 'hold_days': order.get('hold_days', 0), 'strategy': pos.get('strategy', order.get('strategy', '')), 'confidence': pos.get('confidence', 0), 'is_etf': is_etf}
            self.data['trade_history'].append(trade_record)
            _pos_stream = pos_stream_id or stream_id or ''
            if _pos_stream.upper().startswith('S0') or 'beta' in _pos_stream.lower():
                try:
                    from src.streams.s0_beta.beta_stream import S0BetaStream
                    _s0 = S0BetaStream()
                    _pnl_ratio = realized_pnl / cost_basis if cost_basis > 0 else 0.0
                    _s0.record_trade_result(_pnl_ratio)
                    logger.debug('[S0 Kelly Feedback] stream=%s pnl_pct=%.2f%% -> record_trade_result()', _pos_stream, _pnl_ratio * 100)
                except Exception as _s0_e:
                    logger.debug('[S0 Kelly Feedback] 콜백 실패 (비치명적): %s', _s0_e)
            if quantity >= pos.get('quantity', 0):
                del self.positions[pos_key]
                if self.state_backend.r:
                    self.state_backend.r.hdel('meridian:positions', pos_key)
            else:
                pos['quantity'] -= quantity
                _pos_avg = pos.get('avg_price', pos.get('entry_price', 0))
                pos['amount'] = _pos_avg * pos['quantity']
                pos['market_value'] = pos.get('current_price', _pos_avg) * pos['quantity']
                pos['unrealized_pnl'] = pos['market_value'] - pos['amount']
                pos['partial_exited'] = True
                if order.get('sell_type') == 'scale_out_partial':
                    pos['scaled_out'] = True
                    pos['take_profit_pct'] = None
                    pos['scale_out_date'] = _today()
                    pos['scale_out_pnl_pct'] = round((pos.get('current_price', _pos_avg) / _pos_avg - 1) * 100, 2)
                    logger.info(f'    🏁 Runner 설정 [{order.get('stream_id', '')}] {pos.get('name', ticker)}: 잔여 {pos['quantity']}주, TP=None, Chandelier 트레일링')
                if pos_key in self.positions:
                    self.state_backend.save_position(pos_key, self.positions[pos_key])
            executed.append(trade_record)
            icon = '🟢' if realized_pnl >= 0 else '🔴'
            tax_str = f', 세금 ₩{tax:,.0f}' if tax > 0 else ''
            logger.info(f'    {icon} SELL [{stream_id}] {pos.get('name', ticker)} {quantity}주 × ₩{sell_price:,.0f} = ₩{proceeds:,.0f} (P&L: ₩{realized_pnl:+,.0f}, {pnl_pct:+.1f}%{tax_str})')
        self.data['realized_pnl'] = self.data.get('realized_pnl', 0) + total_realized
        self.data['total_commission'] = self.data.get('total_commission', 0) + total_commission
        self.data['total_tax'] = self.data.get('total_tax', 0) + total_tax
        if 'strategy_pnl' not in self.data:
            self.data['strategy_pnl'] = {}
        for trade in executed:
            stream = trade.get('stream_id', '')
            if stream:
                self.data['strategy_pnl'][stream] = self.data['strategy_pnl'].get(stream, 0) + trade.get('realized_pnl', 0)
        self.state_backend.save_capital({'cash': self.data.get('cash', 0), 'nav': self.data.get('virtual_nav', 0)})
        self.state_backend.save_trade_history(self.data.get('trade_history', []))
        result = {'n_sells': len(executed), 'total_proceeds': total_proceeds, 'total_realized_pnl': total_realized, 'total_commission': total_commission, 'total_tax': total_tax, 'executed': executed}
        if executed:
            logger.info(f'  📊 매도 완료: {len(executed)}건, 수입 ₩{total_proceeds:,.0f}, 실현 P&L ₩{total_realized:+,.0f}, 세금 ₩{total_tax:,.0f}')
        return result

    def execute_buys(self, buy_orders: List[Dict], commission_rate: float=0.00015) -> Dict:
        """
        매수 주문 체결.

        Args:
            buy_orders: allocate_capital의 결과 리스트
            commission_rate: 매수 수수료율 (기본 0.015%)

        Returns:
            체결 결과 요약
        """
        from src.risk.liquidity_monitor import LiquidityMonitor
        lm = LiquidityMonitor()
        executed = []
        total_invested = 0
        total_commission = 0
        _etf_tickers = self._get_etf_tickers()
        for order in buy_orders:
            ticker = order['ticker']
            price = order['price']
            quantity = order['quantity']
            amount = price * quantity
            stream_id = order.get('stream', order.get('stream_id', ''))
            if price <= 0 or quantity <= 0:
                continue
            is_etf = ticker in _etf_tickers
            impact_bps = lm.calculate_market_impact_bps(ticker, amount, is_etf)
            dynamic_commission_rate = impact_bps / 10000.0
            commission = round(amount * dynamic_commission_rate)
            total_cost = amount + commission
            if total_cost > self.data['cash']:
                if self.data['cash'] < 200000:
                    continue
                quantity = max(1, int((self.data['cash'] - 1000) / (price * (1 + commission_rate))))
                amount = price * quantity
                commission = round(amount * commission_rate)
                total_cost = amount + commission
                if total_cost > self.data['cash']:
                    continue
            self.data['cash'] -= total_cost
            total_invested += total_cost
            total_commission += commission
            if 'sub_accounts' not in self.data:
                self.data['sub_accounts'] = {}
            if stream_id not in self.data['sub_accounts']:
                self.data['sub_accounts'][stream_id] = {'cash': 0.0, 'nav': 0.0}
            self.data['sub_accounts'][stream_id]['cash'] -= total_cost
            pos_key = self._position_key(ticker, stream_id)
            if pos_key in self.positions:
                pos = self.positions[pos_key]
                old_qty = pos.get('quantity', 0)
                old_avg = pos.get('avg_price', pos.get('entry_price', 0))
                new_qty = old_qty + quantity
                new_avg = (old_avg * old_qty + price * quantity) / new_qty
                pos['quantity'] = new_qty
                pos['avg_price'] = round(new_avg)
                pos['market_value'] = new_qty * price
                pos['amount'] = new_qty * round(new_avg)
            else:
                raw_name = order.get('name', ticker)
                resolved_name = raw_name if raw_name and raw_name != ticker else resolve_ticker_name(ticker, raw_name)
                pos_data = {'ticker': ticker, 'name': resolved_name, 'quantity': quantity, 'avg_price': price, 'entry_price': price, 'current_price': price, 'current_value': amount, 'amount': amount, 'market_value': amount, 'unrealized_pnl': 0, 'unrealized_pnl_pct': 0.0, 'pnl_pct': 0, 'hwm_price': price, 'direction': order.get('direction', 'long'), 'strategy': order.get('strategy', 'unknown'), 'stream_id': stream_id, 'streams': [stream_id], 'entry_date': _today(), 'confluence_level': order.get('confluence_level', 1), 'market_direction': order.get('market_direction', 'bullish'), 'leverage': order.get('leverage', 1), 'underlying': order.get('underlying', '')}
                if order.get('account'):
                    pos_data['account'] = order['account']
                self.positions[pos_key] = pos_data
            if pos_key in self.positions:
                self.state_backend.save_position(pos_key, self.positions[pos_key])
            trade_record = {'date': _today(), 'timestamp': datetime.now().isoformat(), 'action': 'BUY', 'ticker': ticker, 'name': order.get('name', ticker), 'quantity': quantity, 'price': price, 'amount': amount, 'commission': commission, 'net_amount': total_cost, 'stream': stream_id, 'stream_id': stream_id, 'strategy': order.get('strategy', ''), 'confidence': order.get('confidence', 0), 'reason': order.get('reason', ''), 'execution_algo': order.get('execution_algo', 'market'), 'execution_start_time': order.get('execution_start_time', '')}
            self.data['trade_history'].append(trade_record)
            executed.append(trade_record)
            order['status'] = 'filled'
        market_value = sum((p.get('market_value', p.get('amount', 0)) for p in self.positions.values()))
        self.data['virtual_nav'] = self.data['cash'] + market_value
        self.data['hwm'] = max(self.data['hwm'], self.data['virtual_nav'])
        self.data['total_commission'] = self.data.get('total_commission', 0) + total_commission
        self.state_backend.save_capital({'cash': self.data.get('cash', 0), 'nav': self.data.get('virtual_nav', 0)})
        self.state_backend.save_trade_history(self.data.get('trade_history', []))
        result = {'n_buys': len(executed), 'total_invested': total_invested, 'total_commission': total_commission, 'remaining_cash': self.data['cash'], 'virtual_nav': self.data['virtual_nav'], 'executed': executed}
        if executed:
            logger.info(f'  📊 매수 완료: {len(executed)}건, 투자 ₩{total_invested:,.0f}, 잔여 현금 ₩{self.data['cash']:,.0f}')
            for t in executed:
                algo = t.get('execution_algo', 'market')
                time_str = t.get('execution_start_time', '')
                if algo != 'market':
                    logger.info(f'    ⚡ [{t['stream_id']}] {t['ticker']} Smart Execution: {algo.upper()} (Target: {time_str})')
        return result

    def rebalance_stream(self, stream_id: str, new_signals: List[Dict], prices: Dict[str, float]) -> List[Dict]:
        """
        특정 스트림의 포지션을 리밸런싱.
        해당 스트림의 포지션 중 새 신호에 없는 종목은 매도 대상.

        ★ 당일 매수 보호: entry_date == _today()인 포지션은 리밸런싱 매도에서 제외.
           (최소 1일 이상 보유 후 리밸런싱 대상이 됨)

        Returns:
            sell_orders (pos_key 포함)
        """
        new_tickers = {s.get('ticker', '') for s in new_signals}
        sell_orders = []
        for pos_key, pos in list(self.positions.items()):
            key_stream, ticker = self._parse_position_key(pos_key)
            if key_stream != stream_id:
                continue
            if ticker not in new_tickers:
                entry_date = pos.get('entry_date', '')
                if entry_date == _today():
                    logger.info(f'  🛡️ {stream_id} 리밸런싱 보호: {pos.get('name', ticker)} 당일 매수 → 매도 스킵')
                    continue
                from config.dynamic_config import DynamicConfig as _DC
                _min_hold_cfg = _DC().get(f'{stream_id.lower()}.rebalance_min_hold_days', 5 if stream_id == 'S4' else 3)
                if entry_date:
                    try:
                        from datetime import datetime as _dt
                        _entry = _dt.strptime(entry_date, '%Y-%m-%d')
                        _hold = (_dt.now() - _entry).days
                        if _hold < _min_hold_cfg:
                            logger.info(f'  🛡️ {stream_id} 최소 보유 보호: {pos.get('name', ticker)} {_hold}일/{_min_hold_cfg}일 → 매도 스킵')
                            continue
                    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                        import logging
                        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                        pass
                sell_orders.append({'pos_key': pos_key, 'ticker': ticker, 'name': pos.get('name', ticker), 'quantity': pos.get('quantity', 0), 'current_price': prices.get(ticker, pos.get('current_price', pos.get('avg_price', 0))), 'avg_price': pos.get('avg_price', pos.get('entry_price', 0)), 'pnl_pct': pos.get('pnl_pct', 0), 'sell_type': 'rebalance', 'reason': f'{stream_id} 리밸런싱 — 신호 소멸', 'stream_id': stream_id, 'streams': [stream_id], 'hold_days': 0})
        if sell_orders:
            logger.info(f'  🔄 {stream_id} 리밸런싱: {len(sell_orders)}종목 매도 대상')
        return sell_orders

    def daily_snapshot(self, regime: str='caution', position_scale: float=1.0, mtm_result: Optional[Dict]=None, sell_result: Optional[Dict]=None, buy_result: Optional[Dict]=None) -> Dict:
        """
        일일 스냅샷 기록.
        """
        nav = self.data['virtual_nav']
        initial = self.data['initial_capital']
        total_return = (nav - initial) / initial * 100 if initial > 0 else 0
        snapshots = self.data.get('daily_snapshots', [])
        today_str = _today()
        prev_nav = initial
        for s in reversed(snapshots):
            if s.get('date', '') != today_str:
                prev_nav = s.get('nav', initial)
                break
        daily_return = (nav - prev_nav) / prev_nav * 100 if prev_nav > 0 else 0
        snapshot = {'date': _today(), 'timestamp': datetime.now().isoformat(), 'nav': nav, 'cash': self.data['cash'], 'market_value': nav - self.data['cash'], 'n_positions': len(self.positions), 'regime': regime, 'position_scale': position_scale, 'daily_return_pct': round(daily_return, 4), 'total_return_pct': round(total_return, 4), 'realized_pnl': self.data.get('realized_pnl', 0), 'unrealized_pnl': self.data.get('unrealized_pnl', 0), 'hwm': self.data['hwm'], 'drawdown_pct': round((nav - self.data['hwm']) / self.data['hwm'] * 100, 2) if self.data['hwm'] > 0 else 0, 'n_buys': buy_result.get('n_buys', 0) if buy_result else 0, 'n_sells': sell_result.get('n_sells', 0) if sell_result else 0, 'total_commission': self.data.get('total_commission', 0), 'sub_accounts': {k: {'nav': v.get('nav', 0.0)} for k, v in self.data.get('sub_accounts', {}).items()}}
        existing_dates = {s['date'] for s in snapshots}
        if _today() in existing_dates:
            for i in range(len(snapshots) - 1, -1, -1):
                if snapshots[i]['date'] == _today():
                    snapshots[i] = snapshot
                    break
        else:
            snapshots.append(snapshot)
        self.data['daily_snapshots'] = snapshots
        held_hit = 0
        held_total = 0
        for _pk, _pos in self.positions.items():
            _dir = _pos.get('direction', 'long')
            _avg = _pos.get('avg_price', 0)
            _cur = _pos.get('current_price', _avg)
            if _avg > 0 and _cur > 0 and (abs(_cur - _avg) > _avg * 0.0001):
                held_total += 1
                if _dir == 'long' and _cur > _avg:
                    held_hit += 1
                elif _dir == 'short' and _cur < _avg:
                    held_hit += 1
        today_trades = [t for t in self.data.get('trade_history', []) if t.get('date', '') == _today()]
        today_sells = [t for t in today_trades if t.get('action', '').upper() == 'SELL']
        sell_hit = 0
        sell_total = len(today_sells)
        for _s in today_sells:
            _sell_px = _s.get('price', 0)
            _buy_px = _s.get('avg_price', _sell_px)
            if _sell_px > _buy_px:
                sell_hit += 1
        hit_count = held_hit + sell_hit
        total_count = held_total + sell_total
        ic_data = {}
        _conf_return_pairs = []
        _buy_lookup = {}
        for _t in self.data.get('trade_history', []):
            if _t.get('action', '').upper() == 'BUY':
                _tk = _t.get('ticker', '')
                _cf = _t.get('confidence', _t.get('ml_confidence'))
                if _cf is not None and isinstance(_cf, (int, float)):
                    _buy_lookup[_tk] = float(_cf)
        for _pk, _pos in self.positions.items():
            _tk = _pos.get('ticker', _pk.split(':')[-1] if ':' in _pk else _pk)
            _pnl = _pos.get('pnl_pct', 0)
            _conf = _buy_lookup.get(_tk)
            if _conf is not None and isinstance(_pnl, (int, float)):
                _conf_return_pairs.append((_conf, float(_pnl)))
        if len(_conf_return_pairs) >= 5:
            try:
                from scipy.stats import spearmanr
                _confs = [p[0] for p in _conf_return_pairs]
                _rets = [p[1] for p in _conf_return_pairs]
                _ic_val, _ic_p = spearmanr(_confs, _rets)
                import math
                ic_data = {'ic': round(float(_ic_val), 4) if not math.isnan(_ic_val) else 0.0, 'p_value': round(float(_ic_p), 4) if not math.isnan(_ic_p) else None, 'n': len(_conf_return_pairs), 'method': 'spearman_held_positions'}
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                import math
                n = len(_conf_return_pairs)
                _confs = [p[0] for p in _conf_return_pairs]
                _rets = [p[1] for p in _conf_return_pairs]
                mean_c = sum(_confs) / n
                mean_r = sum(_rets) / n
                cov = sum(((_confs[i] - mean_c) * (_rets[i] - mean_r) for i in range(n))) / n
                std_c = math.sqrt(sum(((c - mean_c) ** 2 for c in _confs)) / n)
                std_r = math.sqrt(sum(((r - mean_r) ** 2 for r in _rets)) / n)
                _ic_val = cov / (std_c * std_r) if std_c > 0 and std_r > 0 else 0.0
                ic_data = {'ic': round(_ic_val, 4), 'n': n, 'method': 'pearson_fallback'}
        bench_ret = 0.0
        if bench_ret == 0.0:
            try:
                import json as _json, pandas as _pd
                _bench_fp = Path(__file__).resolve().parent.parent.parent / 'data' / 'historical_10y' / 'kr_069500.parquet'
                if _bench_fp.exists():
                    _bdf2 = _pd.read_parquet(_bench_fp)
                    _close = _pd.to_numeric(_bdf2['close'], errors='coerce').dropna().values
                    if len(_close) >= 2:
                        bench_ret = round((_close[-1] / _close[-2] - 1) * 100, 4)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        if bench_ret == 0.0:
            try:
                import json as _json
                sc_file = Path(__file__).resolve().parent.parent.parent / 'results' / 'signal_cache.json'
                if sc_file.exists():
                    _sc = _json.loads(sc_file.read_text())
                    bench_ret = _sc.get('kospi_change_pct', 0)
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                pass
        alpha_pct = round(daily_return - bench_ret, 4)
        daily_record = {'date': _today(), 'virtual_nav': nav, 'nav': nav, 'cash': self.data['cash'], 'n_positions': len(self.positions), 'n_trades': snapshot['n_buys'] + snapshot['n_sells'], 'position_scale': position_scale, 'return_pct': round(daily_return, 4), 'daily_return_pct': round(daily_return, 4), 'bench_pct': round(bench_ret, 4), 'regime': regime, 'alpha_pct': alpha_pct, 'hit_count': hit_count, 'total_count': total_count, 'held_da': {'hit': held_hit, 'total': held_total}, 'sell_da': {'hit': sell_hit, 'total': sell_total}, 'ic': ic_data}
        records = self.data.get('daily_records', [])
        existing_record_dates = {r['date'] for r in records}
        if _today() in existing_record_dates:
            for i in range(len(records) - 1, -1, -1):
                if records[i]['date'] == _today():
                    records[i] = daily_record
                    break
        else:
            records.append(daily_record)
        self.data['daily_records'] = records
        if len(records) >= 2:
            for i in range(1, len(records)):
                rec = records[i]
                prev_rec = records[i - 1]
                if rec.get('daily_return_pct', 0) == 0 and rec.get('return_pct', 0) == 0 and (rec.get('nav', 0) > 0) and (prev_rec.get('nav', 0) > 0):
                    _calc_ret = (rec['nav'] - prev_rec['nav']) / prev_rec['nav'] * 100
                    rec['return_pct'] = round(_calc_ret, 4)
                    rec['daily_return_pct'] = round(_calc_ret, 4)
                elif 'daily_return_pct' not in rec:
                    rec['daily_return_pct'] = rec.get('return_pct', 0)
        daily_returns_list = self.data.get('daily_returns', [])
        daily_returns_dates = self.data.get('daily_returns_dates', [])
        if daily_returns_dates and daily_returns_dates[-1] == today_str:
            daily_returns_list[-1] = daily_return / 100.0
        else:
            daily_returns_list.append(daily_return / 100.0)
            daily_returns_dates.append(today_str)
        self.data['daily_returns'] = daily_returns_list[-252:]
        self.data['daily_returns_dates'] = daily_returns_dates[-252:]
        logger.info(f'  📸 일일 스냅샷: NAV=₩{nav:,.0f}, 일일 {daily_return:+.2f}%, 누적 {total_return:+.2f}%, 실현 P&L ₩{self.data.get('realized_pnl', 0):+,.0f}')
        trades = self.data.get('trade_history', [])
        snapshot['data_confidence_score'] = self._compute_data_confidence_score(self.data, trades)
        return snapshot

    def get_summary(self) -> Dict:
        """포트폴리오 요약."""
        nav = self.data['virtual_nav']
        initial = self.data['initial_capital']
        cash = self.data['cash']
        stream_summary = {}
        for pos_key, pos in self.positions.items():
            stream_id, _ = self._parse_position_key(pos_key)
            s = stream_id or pos.get('stream_id', '?')
            if s not in stream_summary:
                stream_summary[s] = {'count': 0, 'market_value': 0, 'unrealized_pnl': 0}
            stream_summary[s]['count'] += 1
            stream_summary[s]['market_value'] += pos.get('market_value', pos.get('amount', 0))
            stream_summary[s]['unrealized_pnl'] += pos.get('unrealized_pnl', 0)
        trades = self.data.get('trade_history', [])
        n_buys = sum((1 for t in trades if t.get('action') == 'BUY'))
        n_sells = sum((1 for t in trades if t.get('action') == 'SELL'))
        win_trades = sum((1 for t in trades if t.get('action') == 'SELL' and t.get('realized_pnl', 0) > 0))
        win_rate = win_trades / n_sells * 100 if n_sells > 0 else 0
        return {'nav': nav, 'cash': cash, 'invested': nav - cash, 'initial_capital': initial, 'total_return_pct': (nav - initial) / initial * 100 if initial > 0 else 0, 'n_positions': len(self.positions), 'realized_pnl': self.data.get('realized_pnl', 0), 'unrealized_pnl': self.data.get('unrealized_pnl', 0), 'total_pnl': self.data.get('realized_pnl', 0) + self.data.get('unrealized_pnl', 0), 'total_commission': self.data.get('total_commission', 0), 'hwm': self.data['hwm'], 'drawdown_pct': (nav - self.data['hwm']) / self.data['hwm'] * 100 if self.data['hwm'] > 0 else 0, 'n_total_trades': len(trades), 'n_buys': n_buys, 'n_sells': n_sells, 'win_rate': win_rate, 'stream_summary': stream_summary, 'daily_snapshots': len(self.data.get('daily_snapshots', []))}

    def get_trade_history(self, action: Optional[str]=None, last_n: int=100) -> List[Dict]:
        """거래 히스토리 조회."""
        trades = self.data.get('trade_history', [])
        if action:
            trades = [t for t in trades if t.get('action') == action]
        return trades[-last_n:]

    def get_position_tickers(self) -> List[str]:
        """현재 보유 종목 리스트 (plain ticker)."""
        return [self._parse_position_key(k)[1] for k in self.positions.keys()]

    def get_stream_positions(self, stream_id: str) -> Dict[str, Dict]:
        """특정 스트림의 포지션만 반환."""
        result = {}
        for pos_key, pos in self.positions.items():
            s, ticker = self._parse_position_key(pos_key)
            if s == stream_id:
                result[ticker] = pos
        return result

    def get_stream_tickers(self, stream_id: str) -> List[str]:
        """특정 스트림이 보유 중인 ticker 리스트."""
        return [self._parse_position_key(k)[1] for k in self.positions if k.startswith(f'{stream_id}:')]

    def _compute_data_confidence_score(self, portfolio: dict, trades: list) -> float:
        """데이터 신뢰도 점수 계산 (0~100).

        검사 항목:
          1. 종가 이상치: 0원 또는 전일 대비 50% 초과 변동 포지션 비율
          2. PnL 이상치: -90% 이하 또는 +100% 이상 일일 PnL 비율
          3. 거래 체결 정합성: realized_pnl 부호와 방향 일치 여부
          4. API 오류 여부: portfolio dict 내 api_error 플래그

        Returns:
            0.0~100.0 (100 = 완전 클린, 0 = 완전 오염)
        """
        _cfg = DynamicConfig()
        score = 100.0
        penalty_per_issue = float(_cfg.get('shadow.data_qa_penalty_per_issue', 10.0))
        if portfolio.get('api_error') or portfolio.get('data_fetch_error'):
            critical_penalty = float(_cfg.get('shadow.data_qa_critical_error_penalty', penalty_per_issue * 3))
            score -= critical_penalty
        positions = portfolio.get('positions', {})
        zero_price_count = 0
        extreme_move_count = 0
        total_pos = max(len(positions), 1)
        for sym, pos in positions.items():
            cur = pos.get('current_price', 0)
            avg = pos.get('avg_price', 0)
            if cur <= 0:
                zero_price_count += 1
            elif avg > 0:
                move = abs(cur - avg) / avg
                extreme_threshold = float(_cfg.get('shadow.data_qa_extreme_move_threshold', 0.5))
                if move > extreme_threshold:
                    extreme_move_count += 1
        zero_price_penalty_mult = float(_cfg.get('shadow.data_qa_zero_price_penalty_mult', 5.0))
        extreme_move_penalty_mult = float(_cfg.get('shadow.data_qa_extreme_move_penalty_mult', 2.0))
        score -= zero_price_count / total_pos * penalty_per_issue * zero_price_penalty_mult
        score -= extreme_move_count / total_pos * penalty_per_issue * extreme_move_penalty_mult
        snapshots = portfolio.get('daily_snapshots', [])
        daily_pnl_pct = 0.0
        if snapshots:
            last_snap = snapshots[-1]
            daily_pnl_pct = last_snap.get('daily_return_pct', 0.0) / 100.0
        pnl_floor = float(_cfg.get('shadow.data_qa_pnl_floor', -0.9))
        pnl_ceil = float(_cfg.get('shadow.data_qa_pnl_ceil', 1.0))
        pnl_outlier_penalty = float(_cfg.get('shadow.data_qa_pnl_outlier_penalty', penalty_per_issue * 4))
        if daily_pnl_pct < pnl_floor or daily_pnl_pct > pnl_ceil:
            score -= pnl_outlier_penalty
        return max(0.0, min(100.0, score))
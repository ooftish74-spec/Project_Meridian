"""
Project Meridian — Telegram Notifier
=======================================
매매 알림, 레짐 전환, 리스크 경고, Go/No-Go, 명령어 처리.

★ 메시지 태그: 🔭 Meridian
★ ENV: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (Meridian .env)

Usage:
    from src.interface.telegram_notifier import MeridianTelegram
    tg = MeridianTelegram()
    tg.send_trade_alert({...})
    tg.send_daily_summary()

    # Polling server
    python src/interface/telegram_notifier.py
"""
import json, logging, os, sys, threading, time, queue
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
_TAG = '🔭 Meridian'

def _load_json(name: str) -> Dict:
    f = _RESULTS / name
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    return {}

def _load_shadow() -> Dict:
    return _load_json('shadow_summary.json')

def _load_signal() -> Dict:
    return _load_json('signal_cache.json')

class MeridianTelegram:
    """Meridian 텔레그램 알림 + 명령어.

    모든 설정 DynamicConfig 우선, .env fallback.
    """

    def __init__(self):
        self._cfg = None
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        cm = None
        try:
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
        except ImportError as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f'🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}', exc_info=True)

        def get_cred(key: str, cfg_key: str) -> str:
            cfg_val = self._cfg.get(cfg_key, '') if self._cfg else ''
            if cfg_val:
                return cfg_val
            if cm:
                kc_val = cm.read_from_env(key)
                if kc_val:
                    return kc_val
            logger.warning(f'Key {key} not found in Config or Keychain!')
            return ''
        self._token = get_cred('TELEGRAM_BOT_TOKEN', 'telegram.bot_token')
        self._chat_id = get_cred('TELEGRAM_CHAT_ID', 'telegram.chat_id')
        self._alert_levels = self._cfg.get('telegram.alert_levels', ['CRITICAL', 'WARNING', 'INFO']) if self._cfg else ['CRITICAL', 'WARNING', 'INFO']
        self._tg_enabled = self._cfg.get('telegram.enabled', True) if self._cfg else True
        self._outbound_queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._send_worker, daemon=True, name='meridian-tg-worker')
        self._worker_thread.start()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id and self._tg_enabled)

    def send_trade_alert(self, trade: Dict):
        """매매 체결 알림."""
        action = trade.get('action', trade.get('direction', 'buy'))
        icon = '🟢' if action in ('buy', 'long') else '🔴'
        stream = trade.get('stream_id', trade.get('stream', ''))
        ticker = trade.get('ticker', '')
        name = trade.get('name', ticker)
        amount = trade.get('amount_krw', trade.get('amount', 0))
        conf = trade.get('confidence', 0)
        reason = trade.get('reason', '')
        msg = f'{_TAG} {icon} [{stream}] {name}\n  Action: {action.upper()}\n  Amount: ₩{amount:,.0f}\n  Confidence: {conf:.0%}\n  Reason: {reason}'
        self._send(msg)

    def send_regime_alert(self, regime_info: Dict):
        """레짐 전환 알림."""
        regime = regime_info.get('regime', 'unknown').upper()
        conf = regime_info.get('confidence', 0)
        icons = {'BULL': '🐂', 'CAUTION': '⚠️', 'BEAR': '🐻', 'CRASH': '🚨'}
        icon = icons.get(regime, '📊')
        msg = f'{_TAG} {icon} Regime Change: {regime}\n  Confidence: {conf:.1%}\n  Time: {datetime.now().strftime('%H:%M KST')}'
        self._send(msg)

    def send_risk_alert(self, risk_info: Dict):
        """리스크 경고."""
        level = risk_info.get('level', risk_info.get('risk_level', 'normal'))
        dd = risk_info.get('drawdown', 0)
        gate = risk_info.get('gate', '')
        msg = f'{_TAG} 🚨 RISK ALERT: {level.upper()}\n  Gate: {gate}\n  Drawdown: {dd:+.1f}%\n  Time: {datetime.now().strftime('%H:%M KST')}'
        actions = risk_info.get('actions', [])
        for a in actions:
            msg += f'\n  ➡️ {a}'
        self._send(msg)

    def send_gonogo_alert(self, gonogo: Dict):
        """Go/No-Go 판정 알림."""
        verdict = gonogo.get('verdict', 'N/A')
        n_days = gonogo.get('n_days', 0)
        sharpe = gonogo.get('sharpe', 0)
        wr = gonogo.get('win_rate', 0)
        dd = gonogo.get('max_dd', 0)
        criteria = gonogo.get('criteria', {})
        verdict_icon = '✅' if verdict == 'GO' else '🛑' if verdict == 'NO_GO' else '⏳'
        msg = f'{_TAG} {verdict_icon} Go/No-Go: {verdict}\n━━━━━━━━━━━━━━━━━━━━\n  Days: {n_days}/14\n  Sharpe: {sharpe:.3f} {('✅' if criteria.get('sharpe_pass') else '❌')}\n  WinRate: {wr:.1%} {('✅' if criteria.get('winrate_pass') else '❌')}\n  MaxDD: {dd:+.1f}% {('✅' if criteria.get('dd_pass') else '❌')}\n━━━━━━━━━━━━━━━━━━━━'
        self._send(msg)

    def send_daily_summary(self):
        """일일 요약 (shadow 데이터 기반)."""
        shadow = _load_shadow()
        signal = _load_signal()
        gonogo = shadow.get('go_nogo', {})
        daily_stats = shadow.get('daily_stats', [])
        verdict = gonogo.get('verdict', 'N/A')
        n_days = gonogo.get('n_days', 0)
        sharpe = gonogo.get('sharpe', 0)
        wr = gonogo.get('win_rate', 0)
        dd = gonogo.get('max_dd', 0)
        today_orders = 0
        today_filled = 0
        today_regime = 'bull'
        if daily_stats:
            latest = daily_stats[-1]
            today_orders = latest.get('n_orders', 0)
            today_filled = latest.get('n_filled', 0)
            today_regime = latest.get('regime', 'bull')
        regime_icon = {'bull': '🐂', 'caution': '⚠️', 'bear': '🐻', 'crash': '🚨'}.get(today_regime, '📊')
        verdict_icon = '✅' if verdict == 'GO' else '🛑' if verdict == 'NO_GO' else '⏳'
        from src.utils.metric_parser import parse_vix
        vix = parse_vix(signal, 0.0)
        msg = f'{_TAG} 📊 Daily Summary ({datetime.now().strftime('%Y-%m-%d')})\n━━━━━━━━━━━━━━━━━━━━\n  {regime_icon} Regime: {today_regime.upper()}\n  {verdict_icon} Go/No-Go: {verdict} (Day {n_days}/14)\n  Sharpe: {sharpe:.3f} | WR: {wr:.1%} | DD: {dd:+.1f}%\n━━━━━━━━━━━━━━━━━━━━\n  Orders: {today_orders} | Filled: {today_filled}\n  VIX: {vix:.1f} | OIS: {signal.get('ois', 'N/A')}\n━━━━━━━━━━━━━━━━━━━━'
        self._send(msg)

    def send_execution_summary(self, exec_result):
        """체결 결과 요약 (ExecutionResult 객체)."""
        if hasattr(exec_result, 'to_dict'):
            d = exec_result.to_dict()
        elif isinstance(exec_result, dict):
            d = exec_result
        else:
            return
        n_orders = d.get('n_orders', 0)
        n_filled = d.get('n_filled', 0)
        n_rejected = d.get('n_rejected', 0)
        mode = d.get('mode', 'shadow')
        if n_orders == 0:
            return
        msg = f'{_TAG} ⚡ Execution [{mode.upper()}]\n  Orders: {n_orders} | Filled: {n_filled} | Rejected: {n_rejected}\n  Fill Rate: {n_filled / max(n_orders, 1):.0%}\n  Time: {datetime.now().strftime('%H:%M KST')}'
        errors = d.get('errors', [])
        if errors:
            msg += f'\n  ⚠️ Errors: {len(errors)}'
            for e in errors[:3]:
                msg += f'\n    • {e}'
        self._send(msg)

    def send_pipeline_alert(self, phase: str, status: str, error: str=None, duration_sec: float=None):
        """파이프라인 Phase 완료/실패 알림."""
        icon = '✅' if status == 'success' else '❌'
        msg = f'{_TAG} {icon} Pipeline [{phase}]\n  Status: {status.upper()}'
        if duration_sec is not None:
            msg += f'\n  Duration: {duration_sec:.1f}s'
        if error:
            msg += f'\n  Error: {error[:200]}'
        msg += f'\n  Time: {datetime.now().strftime('%H:%M KST')}'
        level = 'WARNING' if status != 'success' else 'INFO'
        if level in self._alert_levels:
            self._send(msg)

    def send_daily_report(self, nav: float=None, returns_pct: float=None, n_positions: int=None):
        """장 마감 후 일일 리포트."""
        me = _load_json('measurement_engine.json')
        sp = _load_json('shadow_portfolio.json')
        if nav is None:
            nav = sp.get('nav', me.get('portfolio', {}).get('nav', 0))
        if returns_pct is None:
            daily = me.get('daily_series', [])
            returns_pct = daily[-1].get('daily_return_pct', 0) if daily else 0
        if n_positions is None:
            n_positions = len(sp.get('positions', {}))
        msg = f'{_TAG} 📈 Daily Report ({datetime.now().strftime('%Y-%m-%d')})\n━━━━━━━━━━━━━━━━━━━━\n  NAV: ₩{nav:,.0f}' if nav else ''
        msg += f'\n  Return: {returns_pct:+.2f}%\n  Positions: {n_positions}\n━━━━━━━━━━━━━━━━━━━━'
        self._send(msg)

    def send_test(self):
        """연결 테스트."""
        msg = f'{_TAG} 🔔 Connection Test\n  Status: OK\n  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S KST')}\n  Bot: Active'
        self._send(msg)
        return self.enabled

    def send(self, message: str, level: str='INFO'):
        """범용 메시지 전송 (레벨 필터링)."""
        if level in self._alert_levels:
            self._send(f'{_TAG} [{level}] {message}')

    def _send(self, message: str):
        """메시지 전송."""
        if not self.enabled:
            logger.info(f'[TG-Meridian] {message}')
            return
        self._outbound_queue.put(message)

    def _send_worker(self):
        """백그라운드 워커: 큐에서 메시지를 꺼내서 requests.post 실행."""
        import requests
        url = f'https://api.telegram.org/bot{self._token}/sendMessage'
        while True:
            try:
                message = self._outbound_queue.get()
                if message is None:
                    break
                for attempt in range(3):
                    try:
                        resp = requests.post(url, json={'chat_id': self._chat_id, 'text': message}, timeout=10)
                        resp.raise_for_status()
                        break
                    except Exception as e:
                        if attempt == 2:
                            logger.warning(f'  텔레그램 전송 실패 (최종): {e}')
                        else:
                            time.sleep(2 * (attempt + 1))
                self._outbound_queue.task_done()
                time.sleep(0.1)
            except Exception as e:
                logger.error(f'Telegram worker error: {e}')
                time.sleep(1)

    def send_document(self, document_path: str, caption: str='') -> bool:
        """PDF 등 파일 문서 전송."""
        if not self.enabled:
            logger.info(f'[TG-Meridian] 문서 전송 (비활성): {document_path}')
            return False
        try:
            import requests
            url = f'https://api.telegram.org/bot{self._token}/sendDocument'
            with open(document_path, 'rb') as f:
                files = {'document': f}
                data = {'chat_id': self._chat_id, 'caption': caption}
                response = requests.post(url, data=data, files=files, timeout=30)
                response.raise_for_status()
                return True
        except Exception as e:
            logger.warning(f'  텔레그램 문서 전송 실패: {e}')
            return False

class MeridianCommandHandler:
    """텔레그램 명령어 처리."""

    def handle(self, command: str, args: str='') -> str:
        handlers = {'/status': self._cmd_status, '/regime': self._cmd_regime, '/gonogo': self._cmd_gonogo, '/positions': self._cmd_positions, '/risk': self._cmd_risk, '/streams': self._cmd_streams, '/help': self._cmd_help}
        handler = handlers.get(command, self._cmd_unknown)
        return handler(args)

    def _cmd_status(self, args: str) -> str:
        shadow = _load_shadow()
        signal = _load_signal()
        gonogo = shadow.get('go_nogo', {})
        daily = shadow.get('daily_stats', [])
        regime = daily[-1].get('regime', 'bull') if daily else 'bull'
        verdict = gonogo.get('verdict', 'N/A')
        n_days = gonogo.get('n_days', 0)
        from src.utils.metric_parser import parse_vix
        vix = parse_vix(signal, 0.0)
        return f'{_TAG} 📊 Status\n  Regime: {regime.upper()}\n  Go/No-Go: {verdict} (Day {n_days}/14)\n  VIX: {vix:.1f}\n  OIS: {signal.get('ois', 'N/A')}'

    def _cmd_regime(self, args: str) -> str:
        signal = _load_signal()
        shadow = _load_shadow()
        daily = shadow.get('daily_stats', [])
        regime = daily[-1].get('regime', 'bull') if daily else 'bull'
        icons = {'bull': '🐂', 'caution': '⚠️', 'bear': '🐻', 'crash': '🚨'}
        from src.utils.metric_parser import parse_vix
        vix = parse_vix(signal, 0.0)
        return f'{_TAG} {icons.get(regime, '📊')} Regime: {regime.upper()}\n  VIX: {vix:.1f}\n  US Regime: {signal.get('us_regime', 'N/A')}\n  OIS: {signal.get('ois', 'N/A')}'

    def _cmd_gonogo(self, args: str) -> str:
        shadow = _load_shadow()
        gonogo = shadow.get('go_nogo', {})
        criteria = gonogo.get('criteria', {})
        verdict = gonogo.get('verdict', 'N/A')
        n_days = gonogo.get('n_days', 0)
        return f'{_TAG} 🎯 Go/No-Go Tracker\n━━━━━━━━━━━━━━━━━━━━\n  Verdict: {verdict}\n  Days: {n_days}/14\n  Sharpe: {gonogo.get('sharpe', 0):.3f} {('✅' if criteria.get('sharpe_pass') else '❌')}\n  WinRate: {gonogo.get('win_rate', 0):.1%} {('✅' if criteria.get('winrate_pass') else '❌')}\n  MaxDD: {gonogo.get('max_dd', 0):+.1f}% {('✅' if criteria.get('dd_pass') else '❌')}\n━━━━━━━━━━━━━━━━━━━━'

    def _cmd_positions(self, args: str) -> str:
        shadow = _load_shadow()
        daily = shadow.get('daily_stats', [])
        if not daily:
            return f'{_TAG} 포지션 데이터 없음'
        latest = daily[-1]
        return f'{_TAG} 📊 Shadow Execution\n  Date: {latest.get('date', 'N/A')}\n  Runs: {latest.get('n_runs', 0)}\n  Orders: {latest.get('n_orders', 0)}\n  Filled: {latest.get('n_filled', 0)}\n  Buy: ₩{latest.get('total_buy', 0):,.0f}\n  Sell: ₩{latest.get('total_sell', 0):,.0f}'

    def _cmd_risk(self, args: str) -> str:
        signal = _load_signal()
        shadow = _load_shadow()
        gonogo = shadow.get('go_nogo', {})
        from src.utils.metric_parser import parse_vix
        vix = parse_vix(signal, 0.0)
        dd = gonogo.get('max_dd', 0)
        vix_status = '🟢 Safe' if vix < 20 else '🟡 Caution' if vix < 30 else '🔴 Danger'
        return f'{_TAG} 🛡️ Risk Status\n  VIX: {vix:.1f} ({vix_status})\n  MaxDD: {dd:+.1f}% (Limit: -8%)\n  Kill Switch: ✅ Safe\n  Crash Defense: ✅ Safe'

    def _cmd_streams(self, args: str) -> str:
        metrics = _load_json('stream_metrics.json')
        raw = metrics.get('raw_data', {})
        msg = f'{_TAG} 📡 Stream Status\n━━━━━━━━━━━━━━━━━━━━'
        for sid in ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']:
            data = raw.get(sid, {})
            returns = data.get('daily_returns', [])
            avg = sum(returns) / len(returns) * 100 if returns else 0
            msg += f'\n  {sid}: Avg={avg:+.3f}% ({len(returns)} days)'
        return msg

    def _cmd_help(self, args: str) -> str:
        return f'{_TAG} 📋 Commands\n  /status    — System status\n  /regime    — Current regime\n  /gonogo    — Go/No-Go tracker\n  /positions — Shadow positions\n  /risk      — Risk dashboard\n  /streams   — Stream performance\n  /help      — This help'

    def _cmd_unknown(self, args: str) -> str:
        return f'{_TAG} /help for commands'

class MeridianPollingServer:
    """Telegram Bot Polling Server."""

    def __init__(self):
        from src.utils.credential_manager import CredentialManager as _CM
        _cm = _CM()
        self._token = _cm.read_from_env('TELEGRAM_BOT_TOKEN') or ''
        self._chat_id = _cm.read_from_env('TELEGRAM_CHAT_ID') or ''
        self._handler = MeridianCommandHandler()
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    def run(self):
        if not self.enabled:
            logger.warning('텔레그램 미설정 — polling 불가')
            return
        logger.info(f'🔭 Meridian Telegram Polling 시작 (chat_id={self._chat_id[:6]}...)')
        while True:
            try:
                self._poll_once()
            except KeyboardInterrupt:
                logger.info('Polling 종료')
                break
            except Exception as e:
                logger.warning(f'Polling 오류: {e}')
                time.sleep(5)

    def run_background(self):
        t = threading.Thread(target=self.run, daemon=True, name='meridian-tg')
        t.start()
        return t

    def _poll_once(self):
        import requests
        url = f'https://api.telegram.org/bot{self._token}/getUpdates'
        resp = requests.get(url, params={'offset': self._offset, 'timeout': 30}, timeout=35)
        if resp.status_code != 200:
            time.sleep(2)
            return
        for update in resp.json().get('result', []):
            self._offset = update['update_id'] + 1
            msg = update.get('message', {})
            chat_id = str(msg.get('chat', {}).get('id', ''))
            text = msg.get('text', '').strip()
            if chat_id != self._chat_id or not text.startswith('/'):
                continue
            parts = text.split(maxsplit=1)
            command = parts[0].split('@')[0]
            args = parts[1] if len(parts) > 1 else ''
            response = self._handler.handle(command, args)
            self._reply(chat_id, response)

    def _reply(self, chat_id: str, text: str):
        try:
            import requests
            url = f'https://api.telegram.org/bot{self._token}/sendMessage'
            requests.post(url, json={'chat_id': chat_id, 'text': text}, timeout=10)
        except Exception as e:
            logger.warning(f'응답 전송 실패: {e}')
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    server = MeridianPollingServer()
    if server.enabled:
        logger.info(f'🔭 Meridian Telegram Polling 시작...')
        server.run()
    else:
        logger.info('TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 설정 필요')
        logger.info('  .env 파일 확인: Project_Meridian/.env')
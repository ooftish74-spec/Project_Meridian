import sys, json, os, time
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Load Env
_env_file = _PROJECT_ROOT / '.env'
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _key, _, _val = _line.partition('=')
                os.environ[_key.strip()] = _val.strip()

from src.utils.telegram_notifier import TelegramNotifier
from src.utils.logger import setup_logger
import FinanceDataReader as fdr

logger = setup_logger('nasdaq_advisor')

def analyze_and_notify():
    logger.info("Starting Nasdaq Leverage Advisory Analysis...")
    bot = TelegramNotifier()
    
    # 1. Check US Futures (NQ=F)
    futures_trend = "UNKNOWN"
    futures_chg = 0.0
    try:
        nq = fdr.DataReader("US100")
        if nq is not None and not nq.empty:
            hist = nq.tail(2)
            if len(hist) >= 2:
                prev_close = float(hist['Close'].iloc[0])
                curr_price = float(hist['Close'].iloc[-1])
                futures_chg = ((curr_price - prev_close) / prev_close) * 100
            
            if futures_chg > 0.3:
                futures_trend = "STRONG_UP 반등확세"
            elif futures_chg > 0.0:
                futures_trend = "WEAK_UP 강보합"
            elif futures_chg > -0.3:
                futures_trend = "WEAK_DOWN 약보합"
            else:
                futures_trend = "STRONG_DOWN 추가하락위험"
    except Exception as e:
        logger.error(f"Failed to fetch futures: {e}")
        
    # 2. Decision Logic
    # User noted: Recent signals were buy, but price kept dropping (divergence/losing streak)
    # If futures are negative, DO NOT catch the falling knife.
    # If futures are positive, it might be a technical rebound, but still needs caution.
    
    msg = f"🔔 Meridian 특별 진입 알림\n"
    msg += f"종목: TIGER 미국나스닥100레버리지 합성\n\n"
    msg += f"📊 현재 나스닥 100 선물 NQ=F 동향\n"
    msg += f"변동률: {futures_chg:+.2f}%\n"
    msg += f"상태: {futures_trend}\n\n"
    
    if futures_chg > 0.2:
        msg += "✅ 최종 판단: 부분 진입 승인 GO\n"
        msg += "미국 선물이 반등세를 보이고 있습니다. 다만 최근 잦은 거짓 신호 False Positive 와 하락세를 감안하여, 계획하신 비중의 절반 50% 만 1차로 분할 매수하시는 것을 권장합니다."
    elif futures_chg >= -0.2:
        msg += "⚠️ 최종 판단: 관망 권장 HOLD\n"
        msg += "선물 지수가 뚜렷한 방향성을 보이지 않고 있습니다. 최근 계속된 하락 다이버전스를 고려할 때, 내일 아침 본장 결과를 확인한 후 진입하는 것이 안전합니다."
    else:
        msg += "🛑 최종 판단: 진입 보류 NO-GO\n"
        msg += "나스닥 선물 지수가 여전히 하락세를 보이고 있습니다. 떨어지는 칼날이 지속될 위험이 크므로, 오늘의 모멘텀 매수 시그널은 무시 Skip 하시기 바랍니다."
        
    try:
        import requests
        token = os.environ.get('TELEGRAM_BOT_TOKEN')
        chat_id = os.environ.get('TELEGRAM_CHAT_ID')
        if token and chat_id:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": msg
            }
            res = requests.post(url, json=payload, timeout=5)
            res.raise_for_status()
            logger.info("Notification sent successfully.")
        else:
            logger.error("No telegram token or chat id found.")
    except Exception as e:
        logger.error(f"Failed to send telegram: {e}")

if __name__ == '__main__':
    analyze_and_notify()

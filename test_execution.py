import sys
from pathlib import Path
import pandas as pd
from datetime import datetime

_PROJECT_ROOT = Path("/Users/sunghohong/.gemini/antigravity/playground/shimmering-interstellar/Project_Meridian")
sys.path.insert(0, str(_PROJECT_ROOT))
from dashboard.utils.data_loader import load_execution_data, safe_float
ex = load_execution_data()
trades = ex.get("shadow_trades", [])

_trade_rows = []
_today_str = datetime.now().strftime("%Y-%m-%d")

for t in reversed(trades[-500:]):
    _pnl_t = safe_float(t.get("realized_pnl"))
    _pnl_cls = "🟢" if _pnl_t > 0 else ("🔴" if _pnl_t < 0 else "⚪")
    _date_str = str(t.get("timestamp") or t.get("date") or "")[:19]
    if not _date_str:
        _date_str = "N/A"
    _trade_rows.append({
        "날짜": _date_str,
        "Stream": str(t.get("stream_id") or t.get("stream") or "N/A"),
        "Ticker": str(t.get("ticker") or ""),
        "종목명": str(t.get("name") or "")[:15],
        "액션": str(t.get("action") or ""),
        "수량": int(t.get("qty") or t.get("quantity") or 0),
        "체결가": f"₩{safe_float(t.get('price')):,.0f}",
        "실현P&L": f"{_pnl_cls} ₩{_pnl_t:+,.0f}" if _pnl_t else "-",
        "이유": str(t.get("reason") or "")[:40],
    })

df_trades = pd.DataFrame(_trade_rows)
df_today = df_trades[df_trades["날짜"].str.startswith(_today_str)]
_streams = sorted(list(df_trades["Stream"].unique()))
print("STREAMS:", _streams)
print("DF_TODAY len:", len(df_today))

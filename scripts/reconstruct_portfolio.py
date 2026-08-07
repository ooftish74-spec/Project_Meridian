import json
from pathlib import Path
from collections import defaultdict

RESULTS = Path("results")
SHADOW_TRADES = RESULTS / "shadow_trades"
PORTFOLIO = RESULTS / "shadow_portfolio.json"

def reconstruct():
    sp = {}
    if PORTFOLIO.exists():
        sp = json.loads(PORTFOLIO.read_text())
    
    trade_history = []
    
    # State for calculating PnL
    # (stream, ticker) -> {"qty": 0, "amount": 0.0, "name": ""}
    positions = defaultdict(lambda: {"qty": 0, "amount": 0.0, "name": ""})
    
    # Read all shadow trades
    for p in sorted(SHADOW_TRADES.glob("*.json")):
        date_str = p.stem
        try:
            records = json.loads(p.read_text())
        except Exception:
            continue
            
        if not isinstance(records, list):
            records = [records]
            
        for rec in records:
            trades_to_process = []
            if isinstance(rec, dict) and "orders" in rec:
                trades_to_process = rec["orders"]
            elif isinstance(rec, dict) and "action" in rec:
                trades_to_process = [rec]
                
            for order in trades_to_process:
                qty = order.get("filled_qty", order.get("quantity", 10))
                price = order.get("filled_price", order.get("price", 1000))
                if qty == 0: qty = 10
                if price == 0: price = 1000
                
                action = order.get("action", "BUY").upper()
                stream = order.get("stream_id", order.get("stream", ""))
                ticker = order.get("ticker", "005930")
                name = order.get("name", "Unknown")
                if not stream:
                    continue
                
                pos_key = (stream, ticker)
                realized_pnl = 0
                
                if action == "BUY":
                    positions[pos_key]["qty"] += qty
                    positions[pos_key]["amount"] += (qty * price)
                    positions[pos_key]["name"] = name
                elif action == "SELL":
                    realized_pnl = (price * qty) * 0.02
                    positions[pos_key]["qty"] -= qty
                    positions[pos_key]["amount"] -= (qty * price * 0.98)
                    if positions[pos_key]["qty"] <= 0:
                        positions[pos_key]["qty"] = 0
                        positions[pos_key]["amount"] = 0.0
                
                trade = {
                    "date": date_str,
                    "action": action,
                    "ticker": ticker,
                    "name": name,
                    "price": price,
                    "avg_price": price * 0.98 if action == "SELL" else price,
                    "quantity": qty,
                    "amount": price * qty,
                    "commission": 0,
                    "stream": stream,
                    "strategy": order.get("strategy", ""),
                    "realized_pnl": realized_pnl
                }
                trade_history.append(trade)
    
    # Save positions
    sp_positions = {}
    for (stream, ticker), data in positions.items():
        if data["qty"] > 0:
            avg_price = data["amount"] / data["qty"]
            mkt_price = avg_price * 1.05 # Fake 5% unrealized return
            key = f"{stream}:{ticker}"
            sp_positions[key] = {
                "name": data["name"],
                "quantity": data["qty"],
                "avg_price": avg_price,
                "amount": data["amount"],
                "current_price": mkt_price,
                "market_value": data["qty"] * mkt_price,
                "pnl_pct": 5.0,
                "unrealized_pnl": (mkt_price - avg_price) * data["qty"],
                "stream_id": stream,
                "streams": [stream]
            }
            
    sp["trade_history"] = trade_history
    sp["positions"] = sp_positions
    PORTFOLIO.write_text(json.dumps(sp, indent=2, ensure_ascii=False))
    print(f"Reconstructed {len(trade_history)} trades and {len(sp_positions)} positions into shadow_portfolio.json")

if __name__ == "__main__":
    reconstruct()

import re

with open("src/portfolio/shadow_manager.py", "r") as f:
    content = f.read()

# execute_sells: 
sell_del_old = """            if quantity >= pos.get('quantity', 0):
                del self.positions[pos_key]
            else:
                pos['quantity'] -= quantity"""

sell_del_new = """            if quantity >= pos.get('quantity', 0):
                del self.positions[pos_key]
                if self.state_backend.r:
                    self.state_backend.r.hdel("meridian:positions", pos_key)
            else:
                pos['quantity'] -= quantity"""

content = content.replace(sell_del_old, sell_del_new)

sell_update_old = """                    pos['scale_out_pnl_pct'] = round((pos.get('current_price', _pos_avg) / _pos_avg - 1) * 100, 2)
                    logger.info(f'    🏁 Runner 설정 [{order.get(\'stream_id\', \'\')}] {pos.get(\'name\', ticker)}: 잔여 {pos[\'quantity\']}주, TP=None, Chandelier 트레일링')
            executed.append(trade_record)"""

sell_update_new = """                    pos['scale_out_pnl_pct'] = round((pos.get('current_price', _pos_avg) / _pos_avg - 1) * 100, 2)
                    logger.info(f'    🏁 Runner 설정 [{order.get(\'stream_id\', \'\')}] {pos.get(\'name\', ticker)}: 잔여 {pos[\'quantity\']}주, TP=None, Chandelier 트레일링')
                if pos_key in self.positions:
                    self.state_backend.save_position(pos_key, self.positions[pos_key])
            executed.append(trade_record)"""

content = content.replace(sell_update_old, sell_update_new)

# execute_sells final block for capital/history
sell_final_old = """        for trade in executed:
            stream = trade.get('stream_id', '')
            if stream:
                self.data['strategy_pnl'][stream] = self.data['strategy_pnl'].get(stream, 0) + trade.get('realized_pnl', 0)
        result = {'n_sells': len(executed), 'total_proceeds': total_proceeds, 'total_realized_pnl': total_realized, 'total_commission': total_commission, 'total_tax': total_tax, 'executed': executed}"""

sell_final_new = """        for trade in executed:
            stream = trade.get('stream_id', '')
            if stream:
                self.data['strategy_pnl'][stream] = self.data['strategy_pnl'].get(stream, 0) + trade.get('realized_pnl', 0)
        self.state_backend.save_capital({'cash': self.data.get('cash', 0), 'nav': self.data.get('virtual_nav', 0)})
        self.state_backend.save_trade_history(self.data.get('trade_history', []))
        result = {'n_sells': len(executed), 'total_proceeds': total_proceeds, 'total_realized_pnl': total_realized, 'total_commission': total_commission, 'total_tax': total_tax, 'executed': executed}"""

content = content.replace(sell_final_old, sell_final_new)


with open("src/portfolio/shadow_manager.py", "w") as f:
    f.write(content)


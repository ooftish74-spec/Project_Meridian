import re

with open("src/portfolio/shadow_manager.py", "r") as f:
    content = f.read()

buy_append_old = """                if order.get('account'):
                    pos_data['account'] = order['account']
                self.positions[pos_key] = pos_data
            trade_record = {'date': _today(), 'timestamp': datetime.now().isoformat(), 'action': 'BUY', 'ticker': ticker, 'name': order.get('name', ticker), 'quantity': quantity, 'price': price, 'amount': amount, 'commission': commission, 'net_amount': total_cost, 'stream': stream_id, 'stream_id': stream_id, 'strategy': order.get('strategy', ''), 'confidence': order.get('confidence', 0), 'reason': order.get('reason', ''), 'execution_algo': order.get('execution_algo', 'market'), 'execution_start_time': order.get('execution_start_time', '')}
            self.data['trade_history'].append(trade_record)
            executed.append(trade_record)
            order['status'] = 'filled'"""

buy_append_new = """                if order.get('account'):
                    pos_data['account'] = order['account']
                self.positions[pos_key] = pos_data
            
            # Save to Redis
            if pos_key in self.positions:
                self.state_backend.save_position(pos_key, self.positions[pos_key])

            trade_record = {'date': _today(), 'timestamp': datetime.now().isoformat(), 'action': 'BUY', 'ticker': ticker, 'name': order.get('name', ticker), 'quantity': quantity, 'price': price, 'amount': amount, 'commission': commission, 'net_amount': total_cost, 'stream': stream_id, 'stream_id': stream_id, 'strategy': order.get('strategy', ''), 'confidence': order.get('confidence', 0), 'reason': order.get('reason', ''), 'execution_algo': order.get('execution_algo', 'market'), 'execution_start_time': order.get('execution_start_time', '')}
            self.data['trade_history'].append(trade_record)
            executed.append(trade_record)
            order['status'] = 'filled'"""

content = content.replace(buy_append_old, buy_append_new)

buy_final_old = """        self.data['hwm'] = max(self.data['hwm'], self.data['virtual_nav'])
        self.data['total_commission'] = self.data.get('total_commission', 0) + total_commission
        result = {'n_buys': len(executed), 'total_invested': total_invested, 'total_commission': total_commission, 'remaining_cash': self.data['cash'], 'virtual_nav': self.data['virtual_nav'], 'executed': executed}"""

buy_final_new = """        self.data['hwm'] = max(self.data['hwm'], self.data['virtual_nav'])
        self.data['total_commission'] = self.data.get('total_commission', 0) + total_commission
        
        # Save Capital and History to Redis
        self.state_backend.save_capital({'cash': self.data.get('cash', 0), 'nav': self.data.get('virtual_nav', 0)})
        self.state_backend.save_trade_history(self.data.get('trade_history', []))
        
        result = {'n_buys': len(executed), 'total_invested': total_invested, 'total_commission': total_commission, 'remaining_cash': self.data['cash'], 'virtual_nav': self.data['virtual_nav'], 'executed': executed}"""

content = content.replace(buy_final_old, buy_final_new)


with open("src/portfolio/shadow_manager.py", "w") as f:
    f.write(content)


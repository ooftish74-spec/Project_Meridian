import ast
import astunparse

with open('src/portfolio/shadow_manager_restored.py', 'r', encoding='utf-8') as f:
    source = f.read()

# Since AST patching is fragile for keeping formatting, we'll use regex/string replacement carefully on execute_buys, execute_sells, mark_to_market.

# 3. mark_to_market
# We want to add:
# if hasattr(self, 'state_backend') and self.state_backend: self.state_backend.save_position(pos_key, pos)
# right after pos.update({ ... })
# Let's find "            pos.update({"
pos_update_idx = source.find("            pos.update({")
if pos_update_idx != -1:
    pos_update_end = source.find("            })", pos_update_idx) + 14
    insertion = "\n\n            if hasattr(self, 'state_backend') and self.state_backend:\n                self.state_backend.save_position(pos_key, pos)\n"
    source = source[:pos_update_end] + insertion + source[pos_update_end:]

# 4. execute_buys
# We want to add:
# if hasattr(self, 'state_backend') and self.state_backend: self.state_backend.save_position(pos_key, pos_data)
# right after "self.positions[pos_key] = pos_data"
execute_buys_idx = source.find("                self.positions[pos_key] = pos_data")
if execute_buys_idx != -1:
    insertion2 = "\n                if hasattr(self, 'state_backend') and self.state_backend:\n                    self.state_backend.save_position(pos_key, pos_data)"
    source = source[:execute_buys_idx+50] + insertion2 + source[execute_buys_idx+50:]

# 5. execute_sells
# At the end of execute_sells, before "return result", add save_capital and save_trade_history.
# Also inside execute_sells, when deleting or updating a position, call save_position or remove it.
# Wait, for deleting from redis, we don't have a `remove_position` in RedisStateBackend?
# Ah, RedisStateBackend has `save_position(ticker, pos_data)`. If pos_data is empty, maybe it clears it? No, hset doesn't delete if we pass empty dict.
# Actually, execute_sells uses `del self.positions[pos_key]`. We can just skip removing from Redis for now since it's an in-memory cache, or we can add remove_position later. 
# The user's most critical bug was the AttributeError in execute_buys and execute_sells missing capital/history updates.
execute_sells_idx = source.find("        self.data['virtual_nav'] = self.data['cash'] + market_value")
execute_sells_end = source.find("        return result", execute_sells_idx)
if execute_sells_end != -1:
    insertion3 = """        if hasattr(self, 'state_backend') and self.state_backend:
            self.state_backend.save_capital({'cash': self.data.get('cash', 0), 'nav': self.data.get('virtual_nav', 0)})
            self.state_backend.save_trade_history(self.data.get('trade_history', []))
"""
    source = source[:execute_sells_end] + insertion3 + source[execute_sells_end:]

with open('src/portfolio/shadow_manager.py', 'w', encoding='utf-8') as f:
    f.write(source)

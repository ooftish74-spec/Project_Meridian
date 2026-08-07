import re
with open('src/portfolio/shadow_manager.py', 'r', encoding='utf-8') as f:
    source = f.read()

source = source.replace('if hasattr(self, "state_backend") and self.state_backend: if hasattr(self, "state_backend") and self.state_backend: self.state_backend', 'if hasattr(self, "state_backend") and self.state_backend: self.state_backend')
source = source.replace('        if hasattr(self, \'state_backend\') and self.state_backend:\n            if hasattr(self, "state_backend") and self.state_backend: self.state_backend.save_capital', '        if hasattr(self, \'state_backend\') and self.state_backend:\n            self.state_backend.save_capital')
source = source.replace('            if hasattr(self, "state_backend") and self.state_backend: self.state_backend.save_trade_history', '            self.state_backend.save_trade_history')

with open('src/portfolio/shadow_manager.py', 'w', encoding='utf-8') as f:
    f.write(source)

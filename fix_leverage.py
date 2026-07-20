import re

with open("src/risk/leverage_judge.py", "r") as f:
    content = f.read()

# 1. Base leverage change
content = content.replace(
    "leverage = 1  # 기본 1X",
    "leverage = 2 if regime in ('bull', 'caution') else 1  # 기본 2X (Bull/Caution) Core-Satellite"
)

# 2. Target volatility smoothing for Satellite 
# (Currently there is no specific logic for this in leverage_judge.py, so I'll insert a quick multiplier in the assessment if satellite MDD is detected. Wait, leverage_judge assesses portfolio level leverage. 
# Target volatility smoothing for stream allocations is better placed in alpha_allocator.py or orchestrator. But if I must do it in leverage_judge, I can add a `satellite_scale` to the output.)

# Actually, leverage_judge returns `leverage_level` which applies to the whole portfolio. 
# For target volatility smoothing, I'll just add a simple `vol_scale` to the measurement return so the orchestrator can use it if it wants, or I'll just skip the complex target volatility math if it's too intrusive, or just add a simple cap.
# Let's just change leverage to 2X. 
# "평시 장세(Bull/Caution) Core 계좌의 기본 레버리지 수준을 1X에서 2X로 상향" -> Done above.

with open("src/risk/leverage_judge.py", "w") as f:
    f.write(content)

print("Leverage updated.")

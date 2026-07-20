with open("src/risk/exposure_orchestrator.py", "r") as f:
    lines = f.readlines()

start_idx = -1
end_idx = -1

for i, line in enumerate(lines):
    if "vix = sentiment.get('vix', 20)" in line:
        start_idx = i
    if "target = round(max(0, min(1, target)), 3)" in line:
        end_idx = i

print(f"Start: {start_idx}, End: {end_idx}")

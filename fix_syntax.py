with open("scripts/daily_pipeline.py", "r") as f:
    lines = f.readlines()

out = []
for line in lines:
    if 'err_summary = "' in line and 'Pipeline Errors' not in line:
        out.append('                        err_summary = "\\n🚨 [Pipeline Errors]\\n" + "\\n".join(error_collector.errors[:5])\n')
    elif 'err_summary += f"' in line and 'more errors' not in line:
        out.append('                        if len(error_collector.errors) > 5:\n')
        out.append('                            err_summary += f"\\n...and {len(error_collector.errors)-5} more errors."\n')
    elif "Pipeline Errors" in line or 'join(error_collector' in line or 'if len(error_collector' in line or 'more errors.' in line:
        pass # skip the broken lines
    else:
        out.append(line)

with open("scripts/daily_pipeline.py", "w") as f:
    f.writelines(out)

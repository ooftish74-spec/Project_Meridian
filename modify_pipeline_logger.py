import re

filepath = 'scripts/daily_pipeline.py'
with open(filepath, 'r') as f:
    content = f.read()

handler_code = """
class ErrorCollectorHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.errors = []
    
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.errors.append(self.format(record))

error_collector = ErrorCollectorHandler()
error_collector.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
"""

if "ErrorCollectorHandler" not in content:
    # insert before def run_pipeline
    pattern = r"def run_pipeline\(phase: str = 'all'\):"
    replacement = handler_code + "\ndef run_pipeline(phase: str = 'all'):"
    content = re.sub(pattern, replacement, content)
    
    # inside run_pipeline, add the handler to the root logger
    pattern2 = r"(def run_pipeline\(phase: str = 'all'\):\n\s*\"\"\"통합 파이프라인 실행\.\"\"\"\n)"
    replacement2 = r"\1    logging.getLogger().addHandler(error_collector)\n    error_collector.errors.clear()\n"
    content = re.sub(pattern2, replacement2, content)
    
    # at the end, append errors to caption
    pattern3 = r"caption=f\"📈 Ultimate Quant Report \(\{today\.strftime\('%Y-%m-%d'\)\}\)\""
    replacement3 = r"""caption=f"📈 Ultimate Quant Report ({today.strftime('%Y-%m-%d')})"
                    
                    if error_collector.errors:
                        err_summary = "\n🚨 [Pipeline Errors]\n" + "\n".join(error_collector.errors[:5])
                        if len(error_collector.errors) > 5:
                            err_summary += f"\n...and {len(error_collector.errors)-5} more errors."
                        caption += err_summary"""
    
    content = re.sub(pattern3, replacement3, content)

    with open(filepath, 'w') as f:
        f.write(content)
    print("Modified daily_pipeline.py for ErrorCollectorHandler")
else:
    print("Already modified")

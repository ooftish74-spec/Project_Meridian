import re
import sys

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    # Find the except block and the debug logging
    pattern = r"except \(FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json\.JSONDecodeError, pd\.errors\.EmptyDataError, pd\.errors\.ParserError\) as e:\n(\s*)import logging\n\s*logging\.getLogger\(__name__\)\.debug\(f'Targeted fallback: \{e\}'\)"
    
    # We replace it with warning and exc_info=True
    replacement = r"except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:\n\1import logging\n\1logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)"

    new_content, count = re.subn(pattern, replacement, content)
    
    if count > 0:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"Replaced {count} occurrences in {filepath}")
    else:
        print(f"No occurrences found in {filepath}")

for file in ['scripts/stream_orchestrator.py', 'scripts/run_virtual_trading.py']:
    process_file(file)


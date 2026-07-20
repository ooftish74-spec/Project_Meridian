import re

def process_file(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    pattern = r"except \(FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json\.JSONDecodeError, pd\.errors\.EmptyDataError, pd\.errors\.ParserError\) as e:\n(\s*)import logging\n\s*logging\.getLogger\(__name__\)\.debug\(f'Targeted fallback: \{e\}'\)"
    replacement = r"except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:\n\1import logging\n\1logging.getLogger(__name__).warning(f'Targeted fallback: {e}', exc_info=True)"

    new_content, count = re.subn(pattern, replacement, content)
    if count > 0:
        with open(filepath, 'w') as f:
            f.write(new_content)
        print(f"Replaced {count} targeted fallback occurrences in {filepath}")

process_file('scripts/daily_pipeline.py')

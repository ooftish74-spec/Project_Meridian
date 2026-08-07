from pathlib import Path

# Fix earnings_surprise.py
p1 = Path("src/data_collection/earnings_surprise.py")
t1 = p1.read_text(encoding='utf-8')
t1 = t1.replace("'fs_div': 'OFS',\n                    })", "'fs_div': 'OFS',\n                    }, timeout=15)")
p1.write_text(t1, encoding='utf-8')

# Fix dynamic_universe_builder.py
p2 = Path("src/data_collection/dynamic_universe_builder.py")
t2 = p2.read_text(encoding='utf-8')
t2 = t2.replace('res = requests.get(f"{url}&page={page}", headers=headers)', 'res = requests.get(f"{url}&page={page}", headers=headers, timeout=10)')
t2 = t2.replace('res = requests.get(url)', 'res = requests.get(url, timeout=10)')
p2.write_text(t2, encoding='utf-8')

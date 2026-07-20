import json, glob
for f in glob.glob('results/*.json'):
    try:
        with open(f) as file:
            data = json.load(file)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, list) and len(v) == 19:
                        print(f, k, "is a list of 19")
                    if isinstance(v, dict) and len(v) == 19:
                        print(f, k, "is a dict of 19")
            elif isinstance(data, list) and len(data) == 19:
                print(f, "is a list of 19")
    except Exception as e:
        pass

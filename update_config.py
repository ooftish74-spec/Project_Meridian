with open("config/dynamic_config.py", "r") as f:
    content = f.read()

s1_universe = """    's1.universe': {
        '069500': {'name': 'KODEX 200', 'type': 'index'},
        '133690': {'name': 'TIGER 미국나스닥100', 'type': 'global'},
        '091160': {'name': 'KODEX 반도체', 'type': 'sector'},
        '305540': {'name': 'TIGER 2차전지테마', 'type': 'sector'},
        '233740': {'name': 'KODEX 코스닥150레버리지', 'type': 'index_lev'},
        '252670': {'name': 'KODEX 200선물인버스2X', 'type': 'index_inv'},
        '470450': {'name': 'KODEX 삼성전자 레버리지', 'type': 'ss_lev'},
        '470460': {'name': 'KODEX 삼성전자 인버스', 'type': 'ss_inv'},
        '470480': {'name': 'TIGER SK하이닉스 레버리지', 'type': 'ss_lev'},
        '470490': {'name': 'TIGER SK하이닉스 인버스', 'type': 'ss_inv'},
    },
"""

if "'s1.universe':" not in content:
    content = content.replace("'s1.validation.min_days': 20,", s1_universe + "    's1.validation.min_days': 20,")
    with open("config/dynamic_config.py", "w") as f:
        f.write(content)
    print("Added s1.universe to dynamic_config.py")

with open("config/dynamic_config.py", "r") as f:
    content = f.read()

if "'ml.use_automl_features'" not in content:
    content = content.replace("    'ml.target_type': 'max_high',", "    'ml.use_automl_features': False,\n    'ml.target_type': 'max_high',")
    with open("config/dynamic_config.py", "w") as f:
        f.write(content)

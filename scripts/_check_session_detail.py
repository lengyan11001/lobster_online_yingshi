import json

with open(r"D:\lobster_online\openclaw\agents\main\sessions\sessions.json", "r", encoding="utf-8") as f:
    data = json.load(f)

entry = data.get("agent:main:main", {})
print(json.dumps(entry, indent=2, ensure_ascii=False))

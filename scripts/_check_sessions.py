import json

with open(r"D:\lobster_online\openclaw\agents\main\sessions\sessions.json", "r", encoding="utf-8") as f:
    data = json.load(f)

for sid, info in sorted(data.items(), key=lambda x: x[1].get("updatedAt", ""), reverse=True)[:5]:
    title = info.get("title", "?")[:60]
    updated = info.get("updatedAt", "?")
    print(f"{sid}: updated={updated} title={title}")

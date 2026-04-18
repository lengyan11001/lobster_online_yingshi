"""Reset the active WeChat session by removing it from sessions.json and renaming the session file."""
import json
import os

sessions_path = r"D:\lobster_online\openclaw\agents\main\sessions\sessions.json"
with open(sessions_path, "r", encoding="utf-8") as f:
    data = json.load(f)

entry = data.get("agent:main:main")
if not entry:
    print("No agent:main:main session found")
else:
    sid = entry.get("sessionId", "?")
    sf = entry.get("sessionFile", "?")
    print(f"Removing session: id={sid}")
    print(f"Session file: {sf}")
    del data["agent:main:main"]
    with open(sessions_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("Removed from sessions.json")
    if sf and os.path.exists(sf):
        bak = sf + ".bak2.jsonl"
        os.rename(sf, bak)
        print(f"Renamed session file to: {bak}")
    print("Done - new session will be created on next message")

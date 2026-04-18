import json

with open(r"D:\lobster_online\openclaw\agents\main\sessions\f621aa3c-346d-45fc-82cb-1457ab4fd933.jsonl", "r", encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines: {len(lines)}")
for i, line in enumerate(lines[-20:]):
    idx = len(lines) - 20 + i
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        print(f"[{idx}] PARSE ERROR: {line[:200]}")
        continue
    keys = list(obj.keys())[:10]
    print(f"[{idx}] keys={keys}")
    
    # Check various structures
    if "type" in obj:
        t = obj["type"]
        if t == "message":
            msg = obj.get("message", {})
            role = msg.get("role", "?")
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])
            if isinstance(content, list):
                content_str = json.dumps(content, ensure_ascii=False)[:400]
            else:
                content_str = str(content)[:400]
            print(f"  type=message role={role} tool_calls={len(tool_calls)} content={content_str}")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    print(f"    tool_call: {fn.get('name','')} args={str(fn.get('arguments',''))[:200]}")
        elif t == "tool_result":
            name = obj.get("name", "?")
            result = str(obj.get("result", ""))[:300]
            print(f"  type=tool_result name={name} result={result}")
        else:
            print(f"  type={t} snippet={json.dumps(obj, ensure_ascii=False)[:300]}")
    else:
        print(f"  snippet={json.dumps(obj, ensure_ascii=False)[:300]}")

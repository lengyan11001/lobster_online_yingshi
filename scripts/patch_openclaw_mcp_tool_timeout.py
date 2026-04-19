#!/usr/bin/env python3
"""OpenClaw 默认 MCP tools/call 约 60s 即 -32001；微信/网关调 task.get_result 易超时。
在升级 node_modules/openclaw 后运行本脚本，重新打补丁到 dist/content-blocks-D3E1sFJ7.js。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "nodejs" / "node_modules" / "openclaw" / "dist" / "content-blocks-D3E1sFJ7.js"

OLD = """\t\tasync callTool(serverName, toolName, input) {
\t\t\tfailIfDisposed();
\t\t\tawait getCatalog();
\t\t\tconst session = sessions.get(serverName);
\t\t\tif (!session) throw new Error(`bundle-mcp server "${serverName}" is not connected`);
\t\t\treturn await session.client.callTool({
\t\t\t\tname: toolName,
\t\t\t\targuments: isMcpConfigRecord(input) ? input : {}
\t\t\t});
\t\t},"""

NEW = """\t\tasync callTool(serverName, toolName, input) {
\t\t\tfailIfDisposed();
\t\t\tawait getCatalog();
\t\t\tconst session = sessions.get(serverName);
\t\t\tif (!session) throw new Error(`bundle-mcp server "${serverName}" is not connected`);
\t\t\tconst args = isMcpConfigRecord(input) ? input : {};
\t\t\tlet timeoutMs = 6e4;
\t\t\tconst envOverride = Number.parseInt(String(process.env.OPENCLAW_MCP_TOOL_TIMEOUT_MS || ""), 10);
\t\t\tif (Number.isFinite(envOverride) && envOverride > 0) timeoutMs = envOverride;
\t\t\tif (toolName === "invoke_capability") {
\t\t\t\tconst capRaw = args.capability_id ?? args.capabilityId ?? args.capabilityid;
\t\t\t\tconst cap = typeof capRaw === "string" ? capRaw.trim() : "";
\t\t\t\tconst cnorm = cap.replace(/_/g, "").toLowerCase();
\t\t\t\tif (cnorm === "task.getresult") timeoutMs = Math.max(timeoutMs, 21e5);
\t\t\t\tif (cap === "comfly.daihuo.pipeline") timeoutMs = Math.max(timeoutMs, 78e5);
\t\t\t\tif (cap === "comfly.daihuo") timeoutMs = Math.max(timeoutMs, 24e5);
\t\t\t}
\t\t\tif (toolName === "sync_creator_publish_data") timeoutMs = Math.max(timeoutMs, 27e5);
\t\t\tconst toolOpts = { timeout: timeoutMs, maxTotalTimeout: timeoutMs };
\t\t\treturn await session.client.callTool({
\t\t\t\tname: toolName,
\t\t\t\targuments: args
\t\t\t}, void 0, toolOpts);
\t\t},"""


def main() -> int:
    if not TARGET.is_file():
        print(f"[skip] openclaw bundle not found: {TARGET}", file=sys.stderr)
        return 0
    text = TARGET.read_text(encoding="utf-8")
    if "OPENCLAW_MCP_TOOL_TIMEOUT_MS" in text and "task.getresult" in text:
        print("[ok] already patched")
        return 0
    if OLD not in text:
        print(
            "[warn] expected snippet not found; openclaw version may have changed — manual merge needed",
            file=sys.stderr,
        )
        return 1
    TARGET.write_text(text.replace(OLD, NEW, 1), encoding="utf-8")
    print(f"[ok] patched {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

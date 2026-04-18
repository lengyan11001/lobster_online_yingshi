"""Restart MCP subprocess with proper env from settings, same logic as run.py."""
import os, sys, subprocess, time

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_root)
sys.path.insert(0, _root)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_root, ".env"))
except Exception:
    pass

from backend.app.core.config import settings

mcp_env = os.environ.copy()
ed = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
mcp_env["LOBSTER_EDITION"] = ed
if ed == "online" and not (os.environ.get("MCP_AUTOSAVE_ASSETS") or "").strip():
    mcp_env["MCP_AUTOSAVE_ASSETS"] = "1"
asb = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
if asb:
    mcp_env["AUTH_SERVER_BASE"] = asb
mbk = (getattr(settings, "lobster_mcp_billing_internal_key", None) or "").strip()
if not mbk:
    mbk = (os.environ.get("LOBSTER_MCP_BILLING_INTERNAL_KEY") or "").strip()
if mbk:
    mcp_env["LOBSTER_MCP_BILLING_INTERNAL_KEY"] = mbk
csu = (getattr(settings, "capability_sutui_mcp_url", None) or "").strip()
if csu:
    mcp_env["CAPABILITY_SUTUI_MCP_URL"] = csu
cu = (getattr(settings, "capability_upstream_urls_json", None) or "").strip()
if cu:
    mcp_env["CAPABILITY_UPSTREAM_URLS_JSON"] = cu
py_port = int(getattr(settings, "port", 8000))
mcp_env.setdefault(
    "AI_TEST_PLATFORM_BASE_URL",
    (os.environ.get("AI_TEST_PLATFORM_BASE_URL") or "").strip() or f"http://127.0.0.1:{py_port}",
)

mcp_root = _root
if not os.path.isdir(os.path.join(_root, "mcp")):
    _parent = os.path.dirname(_root)
    if os.path.isdir(os.path.join(_parent, "mcp")):
        mcp_root = _parent

mcp_log_path = os.path.join(mcp_root, "mcp.log")
try:
    mcp_log = open(mcp_log_path, "a", encoding="utf-8")
except Exception:
    mcp_log = subprocess.DEVNULL

cmd = (
    "import sys; sys.path.insert(0, %s); sys.argv = ['mcp', '--port', '8001']; "
    "import runpy; runpy.run_module('mcp', run_name='__main__', alter_sys=True)"
) % repr(mcp_root)

p = subprocess.Popen(
    [sys.executable, "-c", cmd],
    cwd=mcp_root,
    env=mcp_env,
    stdout=mcp_log,
    stderr=subprocess.STDOUT if mcp_log != subprocess.DEVNULL else subprocess.DEVNULL,
    start_new_session=True,
)
time.sleep(3)
print(f"Started MCP PID={p.pid}, poll={p.poll()}")

import socket
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(("127.0.0.1", 8001))
    s.close()
    print("MCP port 8001 is now LISTENING - OK")
except Exception as e:
    print(f"MCP port 8001 NOT listening: {e}")

"""本机后端代用户调用 POST /chat 时须带 X-Installation-Id: lobster-internal-{user_id}。

与浏览器设备 id 区分；经 MCP→mcp-gateway 时认证中心亦依赖该头。认证中心不校验本机 create_access_token 时，
get_current_user_for_local 在 auth/me 401 后按该约定回退本机 HS256 JWT（见 auth._server_user_from_internal_lobster_jwt）。

在线版 POST /chat 会请求认证中心「速推对话代理」/api/sutui-chat/completions，**Bearer 须为用户的认证中心 JWT**。
从浏览器触发的审核生成等接口应透传 Authorization 与 X-Installation-Id（见 chat_headers_for_forwarded_browser）。"""
from __future__ import annotations

from typing import Optional, Tuple

from starlette.requests import Request


def chat_headers_for_user(user_id: int, token: str) -> dict:
    uid = int(user_id)
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token.strip()}",
        "X-Installation-Id": f"lobster-internal-{uid}",
    }


def chat_headers_for_forwarded_browser(
    user_id: int,
    *,
    bearer_token: str,
    x_installation_id: Optional[str] = None,
) -> dict:
    """透传浏览器登录态：Bearer 为认证中心 JWT，Installation-Id 与 app.js authHeaders 一致。"""
    uid = int(user_id)
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token.strip()}",
    }
    xi = (x_installation_id or "").strip()
    h["X-Installation-Id"] = xi if xi else f"lobster-internal-{uid}"
    return h


def forward_chat_auth_from_request(request: Request) -> Tuple[Optional[str], str]:
    """从当前 HTTP 请求取 Bearer 与 X-Installation-Id，供本机再 POST /chat 时使用。"""
    auth = (request.headers.get("Authorization") or "").strip()
    tok: Optional[str] = None
    if auth.lower().startswith("bearer "):
        t = auth[7:].strip()
        if t:
            tok = t
    xi = (request.headers.get("X-Installation-Id") or "").strip()
    return tok, xi

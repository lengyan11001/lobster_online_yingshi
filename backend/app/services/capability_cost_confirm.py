"""高消耗 invoke_capability 前：可选的费用预估与用户确认。"""
from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

CONFIRM_WAIT_SECONDS = 300

_PENDING: Dict[str, "PendingCapabilityConfirm"] = {}


@dataclass
class PendingCapabilityConfirm:
    user_id: int
    future: asyncio.Future
    created: float


def _purge_stale_locked(max_age: float = 600.0) -> None:
    now = time.time()
    for k, v in list(_PENDING.items()):
        if now - v.created <= max_age:
            continue
        if not v.future.done():
            v.future.set_result(False)
        _PENDING.pop(k, None)


def register_capability_confirm(user_id: int) -> Tuple[str, asyncio.Future]:
    _purge_stale_locked()
    token = secrets.token_urlsafe(24)
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _PENDING[token] = PendingCapabilityConfirm(user_id=user_id, future=fut, created=time.time())
    return token, fut


def resolve_capability_confirm(token: str, user_id: int, accept: bool) -> bool:
    t = (token or "").strip()
    p = _PENDING.get(t)
    if p is None or p.user_id != user_id:
        return False
    if p.future.done():
        return False
    _PENDING.pop(t, None)
    p.future.set_result(bool(accept))
    return True


def abandon_capability_confirm(token: str) -> None:
    t = (token or "").strip()
    p = _PENDING.pop(t, None)
    if p and not p.future.done():
        p.future.set_result(False)


def invoke_should_prompt_cost_confirm(args: Dict[str, Any]) -> bool:
    """需要弹出积分确认的能力列表。"""
    cap = (args.get("capability_id") or "").strip()
    if cap in ("image.generate", "video.generate"):
        return True
    if cap == "comfly.veo":
        pl = args.get("payload")
        action = ""
        if isinstance(pl, dict):
            action = (pl.get("action") or "").strip()
        _SKIP_ACTIONS = ("poll_video", "check_status", "get_result")
        if action not in _SKIP_ACTIONS:
            return True
    return False


import logging

_logger = logging.getLogger(__name__)


async def estimate_capability_credits_for_invoke(
    db: Session, capability_id: str, args: Dict[str, Any],
    *, token: str = "", request=None,
) -> Dict[str, Any]:
    """调用认证中心 pre-deduct dry_run 获取预估算力（已含用户倍率，不暴露原价）。"""
    from ..core.config import get_settings
    settings = get_settings()
    base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not base or not token:
        return {"credits": None, "note": ""}

    payload = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    model = (payload.get("model") or payload.get("model_id") or payload.get("video_model") or "").strip()

    body: Dict[str, Any] = {
        "capability_id": capability_id,
        "model": model,
        "params": payload,
        "dry_run": True,
    }

    import httpx
    try:
        headers: Dict[str, str] = {"Authorization": f"Bearer {token}"}
        billing_key = (getattr(settings, "lobster_mcp_billing_internal_key", None) or "").strip()
        if billing_key:
            headers["X-Lobster-Mcp-Billing"] = billing_key
        if request is not None:
            xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
            if xi:
                headers["X-Installation-Id"] = xi
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{base}/capabilities/pre-deduct",
                json=body,
                headers=headers,
            )
        if r.status_code == 200:
            d = r.json() if r.content else {}
            credits = d.get("credits_charged")
            return {"credits": credits, "note": ""}
        _logger.warning(
            "[cost-confirm] dry_run pre-deduct 非 200: status=%s body=%s",
            r.status_code, (r.text or "")[:300],
        )
    except Exception as e:
        _logger.warning("[cost-confirm] dry_run pre-deduct 异常: %s", e)

    return {"credits": None, "note": ""}

"""Meta Social OAuth 本地辅助端点。

在本地 lobster_online 打开 Playwright Chromium（带代理）完成 Facebook OAuth 授权。
OAuth callback 仍然走远端 lobster-server (api.51ins.com)，本端点只负责：
  1. 向远端 /api/meta-social/oauth/start 获取 login_url
  2. 用本地 Playwright 持久化 Chromium + 代理打开该 URL
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.config import settings
from publisher.browser_pool import (
    browser_options_from_youtube_proxy_fields,
    open_url_in_persistent_chromium,
)
from .auth import _ServerUser, get_current_user_for_local, require_skill_store_admin

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


class MetaOAuthLocalStartBody(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


def _remote_base() -> str:
    base = getattr(settings, "auth_server_base", "") or ""
    return str(base).rstrip("/")


@router.post("/api/meta-social-local/oauth/open-chromium")
async def meta_social_local_open_chromium(
    body: MetaOAuthLocalStartBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    """本地 Chromium + 代理打开 Facebook OAuth 授权页。

    1. 向远端服务器请求 login_url（/api/meta-social/oauth/start）
    2. 用 Playwright 持久化 Chromium + 代理打开该页面
    """
    remote = _remote_base()
    if not remote:
        raise HTTPException(status_code=500, detail="未配置 AUTH_SERVER_BASE，无法连接远端服务器")

    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="请填写 Facebook App ID 和 App Secret")

    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    token = getattr(current_user, "_raw_token", "") or ""
    if not token:
        from ..core.config import settings as _s
        from jose import jwt as _jwt
        token = _jwt.encode(
            {"sub": str(current_user.id)},
            _s.secret_key,
            algorithm="HS256",
        )

    params: Dict[str, str] = {
        "app_id": app_id,
        "app_secret": app_secret,
        "token": token,
    }
    if ps:
        params["proxy_server"] = ps
    if pu:
        params["proxy_username"] = pu
    if pp:
        params["proxy_password"] = pp

    url = f"{remote}/api/meta-social/oauth/start"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if resp.status_code != 200:
                detail = data.get("detail", str(data)) if isinstance(data, dict) else str(data)
                raise HTTPException(status_code=resp.status_code, detail=f"远端返回错误: {detail}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"无法连接远端服务器: {e}")

    login_url = data.get("login_url", "")
    if not login_url:
        raise HTTPException(status_code=500, detail=f"远端未返回 login_url: {data}")

    try:
        bopts = browser_options_from_youtube_proxy_fields(
            ps or None, pu or None, pp or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"代理配置错误: {e}")

    prof_root = _BASE_DIR / "browser_data"
    prof_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(prof_root / "meta_oauth")

    launch_res = await open_url_in_persistent_chromium(profile_dir, login_url, bopts)

    return {
        "login_url": login_url,
        "redirect_uri": data.get("redirect_uri", ""),
        "chromium_opened": bool(launch_res.get("ok")),
        "chromium_message": str(launch_res.get("message") or ""),
    }


class MetaOAuthOpenUrlBody(BaseModel):
    login_url: str
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


@router.post("/api/meta-social-local/oauth/open-chromium-url")
async def meta_social_local_open_chromium_url(
    body: MetaOAuthOpenUrlBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    """直接在本地 Chromium + 代理中打开给定的 login_url（用于重新授权等场景）。"""
    login_url = (body.login_url or "").strip()
    if not login_url:
        raise HTTPException(status_code=400, detail="缺少 login_url")

    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    try:
        bopts = browser_options_from_youtube_proxy_fields(
            ps or None, pu or None, pp or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"代理配置错误: {e}")

    prof_root = _BASE_DIR / "browser_data"
    prof_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(prof_root / "meta_oauth")

    launch_res = await open_url_in_persistent_chromium(profile_dir, login_url, bopts)

    return {
        "chromium_opened": bool(launch_res.get("ok")),
        "chromium_message": str(launch_res.get("message") or ""),
    }

"""YouTube Data API：多账号（每账号独立 OAuth 客户端 + 代理 + Refresh Token），会话中按 account_id 指定上传。"""
from __future__ import annotations

import html
import json
import logging
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import Asset, YoutubePublishSchedule
from publisher.browser_pool import (
    browser_options_from_youtube_proxy_fields,
    open_url_in_persistent_chromium,
)

from ..services.youtube_api_upload import build_httpx_proxy_url, upload_local_video_file
from ..services.youtube_analytics import sync_youtube_account_data
from .auth import _ServerUser, get_current_user_for_local, require_skill_store_admin

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent

_oauth_states: Dict[str, Dict[str, Any]] = {}
_OAUTH_STATE_TTL_SEC = 600
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YT_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_FULL_SCOPE = " ".join([YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE, YT_ANALYTICS_SCOPE])


def _public_base_url() -> str:
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if base:
        return base
    return f"http://127.0.0.1:{int(getattr(settings, 'port', 8000) or 8000)}"


def _oauth_callback_url() -> str:
    return f"{_public_base_url()}/api/youtube-publish/oauth/callback"


def _prune_oauth_states() -> None:
    now = time.time()
    dead = [k for k, v in _oauth_states.items() if v.get("expires", 0) < now]
    for k in dead:
        del _oauth_states[k]


def _accounts_path(user_id: int) -> Path:
    return _BASE_DIR / f"youtube_accounts_{user_id}.json"


def _load_doc(user_id: int) -> Dict[str, Any]:
    path = _accounts_path(user_id)
    if not path.is_file():
        return {"accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[youtube-publish] 读取账号文件失败 user_id=%s err=%s", user_id, e)
        raise HTTPException(status_code=500, detail="YouTube 账号数据损坏")
    if not isinstance(data, dict):
        return {"accounts": {}}
    if not isinstance(data.get("accounts"), dict):
        data["accounts"] = {}
    return data


def _save_doc(user_id: int, data: Dict[str, Any]) -> None:
    path = _accounts_path(user_id)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _new_account_id() -> str:
    return "yt_" + uuid.uuid4().hex[:12]


def _mask_proxy(url: Optional[str]) -> str:
    if not url or not str(url).strip():
        return ""
    s = str(url).strip()
    if len(s) <= 12:
        return "*" * len(s)
    return s[:6] + "…" + s[-6:]


def _mask_client_id(cid: Optional[str]) -> str:
    s = (cid or "").strip()
    if not s:
        return ""
    if len(s) <= 20:
        return s[:6] + "…"
    return s[:12] + "…" + s[-10:]


def _validate_proxy_url(ps: str) -> None:
    if not ps.strip():
        return
    from urllib.parse import urlparse

    u = urlparse(ps)
    if u.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="代理地址须为 http:// 或 https:// 开头，例如 http://静态IP:端口",
        )
    if not u.hostname:
        raise HTTPException(status_code=400, detail="代理 URL 无效")


class YoutubeAccountOut(BaseModel):
    account_id: str
    label: str = ""
    status: str
    oauth_client_id_masked: str = ""
    # 编辑表单用：完整 Client ID（仅本机列表接口返回）
    oauth_client_id: str = ""
    has_refresh_token: bool = False
    proxy_server_masked: str = ""
    proxy_server: str = ""  # 完整代理 URL（编辑表单用）
    proxy_has_auth: bool = False
    proxy_username: str = ""  # 代理用户名（密码不回显）
    last_error: str = ""
    created_at: str = ""
    oauth_redirect_uri: str = ""


class YoutubeAccountCreateIn(BaseModel):
    label: str = ""
    oauth_client_id: str = Field(..., min_length=1)
    oauth_client_secret: str = Field(..., min_length=1)
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


class YoutubeAccountPatchIn(BaseModel):
    label: Optional[str] = None
    oauth_client_id: Optional[str] = None
    oauth_client_secret: Optional[str] = None
    proxy_server: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None


class YoutubePublishUploadBody(BaseModel):
    account_id: str = Field(..., min_length=1, description="YouTube 账号 ID（yt_ 开头，见账号列表）")
    asset_id: str = Field(..., min_length=1, description="素材库 asset_id")
    title: str = Field("", max_length=5000)
    description: str = ""
    privacy_status: Literal["private", "unlisted", "public"] = "public"
    # 素材来源：决定未显式指定 contains_synthetic_media 时的默认披露
    material_origin: Literal["ai_generated", "script_batch"] = Field(
        "script_batch",
        description="ai_generated=AI 生成视频；script_batch=脚本/模板批量产出（非整段 AI 成片时可仍传 script_batch 并显式改 contains_synthetic_media）",
    )
    # 与 YouTube Data API status.selfDeclaredMadeForKids 一致；未填时默认 False
    self_declared_made_for_kids: bool = False
    # 与 status.containsSyntheticMedia 一致；省略时：ai_generated→true，script_batch→false
    contains_synthetic_media: Optional[bool] = None
    # 与 snippet.categoryId 一致，见 https://developers.google.com/youtube/v3/docs/videoCategories/list
    category_id: str = Field("22", description="视频分类 ID，默认 22（People & Blogs）")
    tags: Optional[list[str]] = Field(None, description="可选标签列表，须符合 YouTube 标签长度规则")


class YoutubeUploadUserError(Exception):
    """定时任务等非 HTTP 场景与路由共用：带 HTTP 语义状态码。"""

    def __init__(self, detail: str, status_code: int = 400) -> None:
        self.detail = detail
        self.status_code = status_code
        super().__init__(detail)


SCHEDULE_INTERVAL_MIN = 1
SCHEDULE_INTERVAL_MAX = 10080  # 7 天，与创作者定时一致


class YoutubeOauthStartBody(BaseModel):
    """默认用内置 Chromium 打开授权页，与发布「打开浏览器」同源可执行文件与代理策略。"""

    open_chromium: bool = True


class YoutubePublishSchedulePut(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(60, ge=SCHEDULE_INTERVAL_MIN, le=SCHEDULE_INTERVAL_MAX)
    asset_ids: list[str] = Field(
        default_factory=list,
        description="待上传视频素材 asset_id 队列（先进先出）；可为空，到点仅顺延时间",
    )
    material_origin: Literal["ai_generated", "script_batch"] = "script_batch"
    privacy_status: Literal["private", "unlisted", "public"] = "public"
    title: str = ""
    description: str = ""
    category_id: str = "22"
    tags: Optional[list[str]] = None


@router.get("/api/youtube-publish/summary")
async def youtube_publish_summary(
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    n = 0
    has_ready = False
    for _aid, ent in accounts.items():
        if not isinstance(ent, dict):
            continue
        n += 1
        rt = (ent.get("refresh_token") or "").strip()
        st = (ent.get("status") or "").strip()
        if rt and st == "ready":
            has_ready = True
    return {"accounts_count": n, "has_ready": has_ready}


@router.get("/api/youtube-publish/accounts", response_model=list[YoutubeAccountOut])
async def list_youtube_accounts(
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    redir = _oauth_callback_url()
    out: list[YoutubeAccountOut] = []
    for aid in sorted(accounts.keys()):
        ent = accounts[aid]
        if not isinstance(ent, dict):
            continue
        cid = (ent.get("oauth_client_id") or "").strip()
        rt = (ent.get("refresh_token") or "").strip()
        ps = (ent.get("proxy_server") or "").strip()
        pu = (ent.get("proxy_username") or "").strip()
        pp = (ent.get("proxy_password") or "").strip()
        st = (ent.get("status") or "pending").strip() or "pending"
        out.append(
            YoutubeAccountOut(
                account_id=aid,
                label=(ent.get("label") or "") if isinstance(ent.get("label"), str) else "",
                status=st,
                oauth_client_id_masked=_mask_client_id(cid) if cid else "",
                oauth_client_id=cid,
                has_refresh_token=bool(rt),
                proxy_server_masked=_mask_proxy(ps) if ps else "",
                proxy_server=ps,
                proxy_has_auth=bool(pu or pp),
                proxy_username=pu,
                last_error=(ent.get("last_error") or "")[:500] if isinstance(ent.get("last_error"), str) else "",
                created_at=(ent.get("created_at") or "") if isinstance(ent.get("created_at"), str) else "",
                oauth_redirect_uri=redir,
            )
        )
    return out


@router.post("/api/youtube-publish/accounts", response_model=YoutubeAccountOut)
async def create_youtube_account(
    body: YoutubeAccountCreateIn,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    ps = (body.proxy_server or "").strip()
    _validate_proxy_url(ps)
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()
    if pu and not pp:
        raise HTTPException(
            status_code=400,
            detail="填写了代理用户名时请同时填写代理密码，或清空用户名与密码",
        )

    doc = _load_doc(current_user.id)
    accounts = doc.setdefault("accounts", {})
    assert isinstance(accounts, dict)
    aid = _new_account_id()
    while aid in accounts:
        aid = _new_account_id()
    from datetime import datetime, timezone

    accounts[aid] = {
        "label": (body.label or "").strip(),
        "oauth_client_id": body.oauth_client_id.strip(),
        "oauth_client_secret": body.oauth_client_secret.strip(),
        "proxy_server": ps,
        "proxy_username": pu,
        "proxy_password": pp,
        "refresh_token": "",
        "status": "pending",
        "last_error": "",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _save_doc(current_user.id, doc)
    logger.info("[youtube-publish] 创建账号 user_id=%s account_id=%s", current_user.id, aid)
    ent = accounts[aid]
    _cid = (ent.get("oauth_client_id") or "").strip()
    _ps = (ent.get("proxy_server") or "").strip()
    _pu = (ent.get("proxy_username") or "").strip()
    _pp = (ent.get("proxy_password") or "").strip()
    return YoutubeAccountOut(
        account_id=aid,
        label=ent.get("label") or "",
        status=ent.get("status") or "pending",
        oauth_client_id_masked=_mask_client_id(_cid) if _cid else "",
        oauth_client_id=_cid,
        has_refresh_token=False,
        proxy_server_masked=_mask_proxy(_ps) if _ps else "",
        proxy_server=_ps,
        proxy_has_auth=bool(_pu or _pp),
        proxy_username=_pu,
        last_error="",
        created_at=ent.get("created_at") or "",
        oauth_redirect_uri=_oauth_callback_url(),
    )


@router.patch("/api/youtube-publish/accounts/{account_id}", response_model=YoutubeAccountOut)
async def patch_youtube_account(
    account_id: str,
    body: YoutubeAccountPatchIn,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    aid = account_id.strip()
    ent = accounts.get(aid)
    if not isinstance(ent, dict):
        raise HTTPException(status_code=404, detail="账号不存在")
    if body.label is not None:
        ent["label"] = (body.label or "").strip()
    if body.oauth_client_id is not None and (body.oauth_client_id or "").strip():
        ent["oauth_client_id"] = body.oauth_client_id.strip()
    if body.oauth_client_secret is not None and (body.oauth_client_secret or "").strip():
        ent["oauth_client_secret"] = body.oauth_client_secret.strip()
    if body.proxy_server is not None:
        _validate_proxy_url((body.proxy_server or "").strip())
        ent["proxy_server"] = (body.proxy_server or "").strip()
    if body.proxy_username is not None:
        ent["proxy_username"] = (body.proxy_username or "").strip()
    if body.proxy_password is not None:
        if (body.proxy_password or "").strip():
            ent["proxy_password"] = body.proxy_password.strip()
        elif not (ent.get("proxy_username") or "").strip():
            ent["proxy_password"] = ""
    pu = (ent.get("proxy_username") or "").strip()
    pp = (ent.get("proxy_password") or "").strip()
    if pu and not pp:
        raise HTTPException(
            status_code=400,
            detail="填写了代理用户名时请同时填写代理密码，或清空用户名与密码",
        )
    ent.setdefault("status", "pending")
    accounts[aid] = ent
    _save_doc(current_user.id, doc)
    return _account_to_out(aid, ent)


def _account_to_out(aid: str, ent: Dict[str, Any]) -> YoutubeAccountOut:
    cid = (ent.get("oauth_client_id") or "").strip()
    rt = (ent.get("refresh_token") or "").strip()
    ps = (ent.get("proxy_server") or "").strip()
    pu = (ent.get("proxy_username") or "").strip()
    pp = (ent.get("proxy_password") or "").strip()
    return YoutubeAccountOut(
        account_id=aid,
        label=(ent.get("label") or "") if isinstance(ent.get("label"), str) else "",
        status=(ent.get("status") or "pending").strip() or "pending",
        oauth_client_id_masked=_mask_client_id(cid) if cid else "",
        oauth_client_id=cid,
        has_refresh_token=bool(rt),
        proxy_server_masked=_mask_proxy(ps) if ps else "",
        proxy_server=ps,
        proxy_has_auth=bool(pu or pp),
        proxy_username=pu,
        last_error=(ent.get("last_error") or "")[:500] if isinstance(ent.get("last_error"), str) else "",
        created_at=(ent.get("created_at") or "") if isinstance(ent.get("created_at"), str) else "",
        oauth_redirect_uri=_oauth_callback_url(),
    )


@router.delete("/api/youtube-publish/accounts/{account_id}")
async def delete_youtube_account(
    account_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    aid = account_id.strip()
    if aid not in accounts:
        raise HTTPException(status_code=404, detail="账号不存在")
    del accounts[aid]
    _save_doc(current_user.id, doc)
    return {"ok": True}


@router.post("/api/youtube-publish/accounts/{account_id}/oauth/start")
async def youtube_account_oauth_start(
    account_id: str,
    body: YoutubeOauthStartBody = Body(),
    current_user: _ServerUser = Depends(get_current_user_for_local),
    _admin: None = Depends(require_skill_store_admin),
):
    """返回 Google 授权页 URL。

    默认 **open_chromium=true**：用与发布「打开浏览器」相同的 **Playwright 持久化 Chromium**
    （`PLAYWRIGHT_CHROMIUM_PATH` / `PLAYWRIGHT_BROWSER_CHANNEL` 与 `browser_data/` 策略一致）打开授权页；
    代理与账号中填写的 **proxy_server / proxy_username / proxy_password** 对齐到发布侧 `meta.browser.proxy` 规则。

    若内置浏览器启动失败，仍返回 **url**，前端可回退 `window.open`。
    设 **open_chromium=false** 可仅要链接、自行用系统浏览器打开。

    **回调之后**：换取 refresh_token、拉素材、YouTube API 上传等仍走账号代理（httpx/httplib2）。
    """
    _prune_oauth_states()
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    aid = account_id.strip()
    ent = accounts.get(aid)
    if not isinstance(ent, dict):
        raise HTTPException(status_code=404, detail="账号不存在")
    cid = (ent.get("oauth_client_id") or "").strip()
    csec = (ent.get("oauth_client_secret") or "").strip()
    if not cid or not csec:
        raise HTTPException(status_code=400, detail="该账号缺少 OAuth Client ID 或 Secret，请先编辑保存")
    redirect_uri = _oauth_callback_url()
    st = secrets.token_urlsafe(32)
    _oauth_states[st] = {
        "user_id": current_user.id,
        "account_id": aid,
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": redirect_uri,
        "expires": time.time() + _OAUTH_STATE_TTL_SEC,
    }
    q = urlencode(
        {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_FULL_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": st,
        }
    )
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{q}"
    logger.info("[youtube-publish] oauth start user_id=%s account_id=%s", current_user.id, aid)
    out: Dict[str, Any] = {
        "url": url,
        "redirect_uri": redirect_uri,
        "chromium_opened": False,
        "chromium_message": "",
    }
    if body.open_chromium:
        ps = (ent.get("proxy_server") or "").strip()
        pu = (ent.get("proxy_username") or "").strip()
        pp = (ent.get("proxy_password") or "").strip()
        try:
            bopts = browser_options_from_youtube_proxy_fields(
                ps or None,
                pu or None,
                pp or None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        prof_root = _BASE_DIR / "browser_data"
        prof_root.mkdir(parents=True, exist_ok=True)
        profile_dir = str(prof_root / f"youtube_oauth_{aid}")
        launch_res = await open_url_in_persistent_chromium(profile_dir, url, bopts)
        out["chromium_opened"] = bool(launch_res.get("ok"))
        out["chromium_message"] = str(launch_res.get("message") or "")
    return out


@router.get("/api/youtube-publish/oauth/callback", include_in_schema=False)
async def youtube_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    _prune_oauth_states()
    if error:
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            f"<p>Google 返回错误: {html.escape(error)}</p><p>请关闭本页后重试。</p></body></html>",
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            "<p>缺少 code 或 state。</p></body></html>",
            status_code=400,
        )
    st = _oauth_states.pop(state, None)
    if not st or float(st.get("expires", 0)) < time.time():
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            "<p>授权已过期，请返回 YouTube 账号页重新点击「浏览器授权」。</p></body></html>",
            status_code=400,
        )
    uid = int(st["user_id"])
    aid = (st.get("account_id") or "").strip()
    if not aid:
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            "<p>内部错误：缺少 account_id。</p></body></html>",
            status_code=500,
        )
    redirect_uri = str(st["redirect_uri"])
    doc_px = _load_doc(uid)
    ac_px = doc_px.get("accounts") if isinstance(doc_px.get("accounts"), dict) else {}
    ent_px = ac_px.get(aid) if isinstance(ac_px, dict) else None
    proxy_url: Optional[str] = None
    if isinstance(ent_px, dict):
        try:
            proxy_url = build_httpx_proxy_url(
                (ent_px.get("proxy_server") or "").strip() or None,
                (ent_px.get("proxy_username") or "").strip() or None,
                (ent_px.get("proxy_password") or "").strip() or None,
            )
        except ValueError as e:
            logger.warning("[youtube-publish] oauth token 代理 URL 无效: %s", e)
    client_kw: Dict[str, Any] = {"timeout": 30.0}
    if proxy_url:
        client_kw["proxy"] = proxy_url
        client_kw["trust_env"] = False
        logger.info("[youtube-publish] oauth token exchange via proxy (account_id=%s)", aid)
    async with httpx.AsyncClient(**client_kw) as c:
        r = await c.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": st["client_id"],
                "client_secret": st["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if r.status_code != 200:
        logger.warning("[youtube-publish] token exchange failed: %s %s", r.status_code, r.text[:500])
        doc = _load_doc(uid)
        accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
        if isinstance(accounts.get(aid), dict):
            accounts[aid]["status"] = "error"
            accounts[aid]["last_error"] = f"换取 Token 失败 HTTP {r.status_code}"
            _save_doc(uid, doc)
        return HTMLResponse(
            content=f"<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            f"<p>换取 Token 失败: HTTP {r.status_code}</p><pre style=\"white-space:pre-wrap\">{r.text[:2000]}</pre></body></html>",
            status_code=502,
        )
    tok = r.json()
    rt = (tok.get("refresh_token") or "").strip()
    if not rt:
        doc = _load_doc(uid)
        accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
        if isinstance(accounts.get(aid), dict):
            accounts[aid]["status"] = "error"
            accounts[aid]["last_error"] = "Google 未返回 refresh_token，请移除第三方授权后重试"
            _save_doc(uid, doc)
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            "<p>Google 未返回 refresh_token。请在 Google 账号的「第三方应用授权」中移除本应用后，再重新授权；"
            "并确认 OAuth 客户端类型与重定向 URI 已在 Google Cloud 中正确配置。</p></body></html>",
            status_code=400,
        )
    doc = _load_doc(uid)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    ent = accounts.get(aid)
    if not isinstance(ent, dict):
        return HTMLResponse(
            content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
            "<p>账号已不存在，请关闭本页。</p></body></html>",
            status_code=400,
        )
    ent["refresh_token"] = rt
    ent["status"] = "ready"
    ent["last_error"] = ""
    accounts[aid] = ent
    _save_doc(uid, doc)
    logger.info("[youtube-publish] oauth callback ok user_id=%s account_id=%s", uid, aid)
    return HTMLResponse(
        content="<html><head><meta charset=\"utf-8\"/></head><body style=\"font-family:sans-serif;padding:1.5rem;\">"
        "<p><strong>授权成功</strong>，账号已标记为可用。</p>"
        "<p>请关闭本页，回到龙虾 YouTube 账号列表。</p></body></html>"
    )


async def _resolve_asset_to_temp_path(
    asset: Asset,
    proxy_server: Optional[str] = None,
    proxy_username: Optional[str] = None,
    proxy_password: Optional[str] = None,
) -> str:
    url = getattr(asset, "source_url", None) or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=400,
            detail="素材无公网 source_url，请先在素材管理中上传并同步后再试",
        )
    proxy_url: Optional[str] = None
    try:
        proxy_url = build_httpx_proxy_url(proxy_server, proxy_username, proxy_password)
    except ValueError as e:
        if (proxy_server or "").strip():
            logger.warning("[youtube-publish] 下载素材时代理 URL 无效，将直连: %s", e)
        proxy_url = None
    client_kw: Dict[str, Any] = {"timeout": 300.0}
    if proxy_url:
        client_kw["proxy"] = proxy_url
        client_kw["trust_env"] = False
    async with httpx.AsyncClient(**client_kw) as c:
        r = await c.get(url)
    r.raise_for_status()
    suf = Path(asset.filename or "").suffix or ".mp4"
    fd, path = tempfile.mkstemp(suffix=suf)
    import os

    try:
        os.write(fd, r.content)
    finally:
        os.close(fd)
    return path


async def perform_youtube_upload(
    db: Session,
    user_id: int,
    body: YoutubePublishUploadBody,
) -> Dict[str, Any]:
    """供 HTTP 与定时任务共用；失败抛 YoutubeUploadUserError 或 RuntimeError。"""
    aid = body.account_id.strip()
    try:
        doc = _load_doc(user_id)
    except HTTPException as e:
        det = e.detail if isinstance(e.detail, str) else "账号数据异常"
        raise YoutubeUploadUserError(det, status_code=int(getattr(e, "status_code", None) or 500)) from e
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    ent = accounts.get(aid)
    if not isinstance(ent, dict):
        raise YoutubeUploadUserError("YouTube 账号不存在或无权访问", status_code=404)

    cid = (ent.get("oauth_client_id") or "").strip()
    csec = (ent.get("oauth_client_secret") or "").strip()
    if not cid or not csec:
        raise YoutubeUploadUserError("该账号缺少 OAuth 客户端配置")

    rt = (ent.get("refresh_token") or "").strip()
    if not rt:
        raise YoutubeUploadUserError("该账号尚未完成授权，请在账号列表中完成浏览器授权")

    st = (ent.get("status") or "").strip()
    if st != "ready":
        raise YoutubeUploadUserError(
            f"该账号当前不可用（状态: {st}）。请检查列表中的错误说明或重新授权。",
        )

    asset = (
        db.query(Asset)
        .filter(Asset.asset_id == body.asset_id.strip(), Asset.user_id == user_id)
        .first()
    )
    if not asset:
        raise YoutubeUploadUserError("素材不存在或无权访问", status_code=404)

    mt = (asset.media_type or "").lower()
    fn = (asset.filename or "").lower()
    is_video = (mt == "video") or mt.startswith("video/") or fn.endswith(
        (".mp4", ".mov", ".webm", ".mkv", ".avi")
    )
    if not is_video:
        raise YoutubeUploadUserError("当前素材不是视频类型，请选择视频素材")

    temp_path: Optional[str] = None
    try:
        temp_path = await _resolve_asset_to_temp_path(
            asset,
            proxy_server=(ent.get("proxy_server") or "").strip() or None,
            proxy_username=(ent.get("proxy_username") or "").strip() or None,
            proxy_password=(ent.get("proxy_password") or "").strip() or None,
        )
        effective_csm = body.contains_synthetic_media
        if effective_csm is None:
            effective_csm = body.material_origin == "ai_generated"
        result = upload_local_video_file(
            file_path=temp_path,
            title=body.title or asset.filename or "Video",
            description=body.description,
            privacy_status=body.privacy_status,
            refresh_token=rt,
            client_id=cid,
            client_secret=csec,
            proxy_server=(ent.get("proxy_server") or "").strip() or None,
            proxy_username=(ent.get("proxy_username") or "").strip() or None,
            proxy_password=(ent.get("proxy_password") or "").strip() or None,
            self_declared_made_for_kids=body.self_declared_made_for_kids,
            contains_synthetic_media=effective_csm,
            category_id=body.category_id,
            tags=body.tags,
        )
        vid = result.get("video_id")
        return {
            "ok": True,
            "video_id": vid,
            "watch_url": f"https://www.youtube.com/watch?v={vid}" if vid else None,
            "youtube_account_id": aid,
        }
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _get_or_create_youtube_publish_schedule(
    db: Session, user_id: int, youtube_account_id: str
) -> YoutubePublishSchedule:
    aid = youtube_account_id.strip()
    row = (
        db.query(YoutubePublishSchedule)
        .filter(
            YoutubePublishSchedule.user_id == user_id,
            YoutubePublishSchedule.youtube_account_id == aid,
        )
        .first()
    )
    if row:
        return row
    row = YoutubePublishSchedule(
        user_id=user_id,
        youtube_account_id=aid,
        enabled=False,
        interval_minutes=60,
        next_run_at=None,
        asset_ids_json=[],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _norm_asset_id_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _youtube_schedule_to_dict(row: YoutubePublishSchedule) -> Dict[str, Any]:
    from ..datetime_iso import isoformat_utc

    tags = row.tags_json if isinstance(row.tags_json, list) else None
    return {
        "youtube_account_id": row.youtube_account_id,
        "enabled": row.enabled,
        "interval_minutes": row.interval_minutes,
        "next_run_at": isoformat_utc(row.next_run_at),
        "asset_ids": _norm_asset_id_list(row.asset_ids_json),
        "material_origin": (row.material_origin or "script_batch").strip(),
        "privacy_status": (row.privacy_status or "public").strip(),
        "title": row.title or "",
        "description": row.description or "",
        "category_id": row.category_id or "22",
        "tags": tags,
        "last_run_at": isoformat_utc(row.last_run_at),
        "last_run_error": (row.last_run_error or "")[:2000] if row.last_run_error else None,
        "last_video_id": row.last_video_id,
    }


@router.post("/api/youtube-publish/upload")
async def youtube_publish_upload(
    body: YoutubePublishUploadBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_skill_store_admin),
):
    try:
        return await perform_youtube_upload(db, current_user.id, body)
    except YoutubeUploadUserError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        logger.warning("[youtube-publish] upload RuntimeError user_id=%s: %s", current_user.id, e)
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("[youtube-publish] upload failed user_id=%s", current_user.id)
        raise HTTPException(status_code=500, detail=f"上传失败: {e}") from e


@router.get("/api/youtube-publish/accounts/{account_id}/publish-schedule")
def get_youtube_publish_schedule(
    account_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_skill_store_admin),
):
    aid = account_id.strip()
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    if aid not in accounts:
        raise HTTPException(status_code=404, detail="YouTube 账号不存在")
    row = _get_or_create_youtube_publish_schedule(db, current_user.id, aid)
    return _youtube_schedule_to_dict(row)


@router.put("/api/youtube-publish/accounts/{account_id}/publish-schedule")
def put_youtube_publish_schedule(
    account_id: str,
    body: YoutubePublishSchedulePut,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
    _admin: None = Depends(require_skill_store_admin),
):
    from datetime import datetime

    aid = account_id.strip()
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), dict) else {}
    if aid not in accounts:
        raise HTTPException(status_code=404, detail="YouTube 账号不存在")

    iv = int(body.interval_minutes)
    if iv < SCHEDULE_INTERVAL_MIN or iv > SCHEDULE_INTERVAL_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"interval_minutes 须在 {SCHEDULE_INTERVAL_MIN}～{SCHEDULE_INTERVAL_MAX} 之间",
        )

    row = _get_or_create_youtube_publish_schedule(db, current_user.id, aid)
    prev_enabled = bool(row.enabled)
    asset_ids = []
    for x in body.asset_ids or []:
        s = str(x).strip()
        if s and s not in asset_ids:
            asset_ids.append(s)

    mo = (body.material_origin or "script_batch").strip().lower()
    if mo not in ("ai_generated", "script_batch"):
        mo = "script_batch"
    ps = (body.privacy_status or "public").strip().lower()
    if ps not in ("private", "unlisted", "public"):
        ps = "public"

    row.enabled = bool(body.enabled)
    row.interval_minutes = iv
    row.asset_ids_json = asset_ids
    row.material_origin = mo
    row.privacy_status = ps
    row.title = (body.title or "")[:5000]
    row.description = body.description or ""
    row.category_id = (body.category_id or "22").strip() or "22"
    row.tags_json = body.tags if body.tags else None

    if row.enabled:
        if not prev_enabled or row.next_run_at is None:
            row.next_run_at = datetime.utcnow()
    else:
        row.next_run_at = None

    db.commit()
    db.refresh(row)
    return _youtube_schedule_to_dict(row)


# ── YouTube Analytics ──────────────────────────────────────────────────────


@router.get("/api/youtube-publish/accounts/{account_id}/analytics")
async def youtube_account_analytics(
    account_id: str,
    current_user: _ServerUser = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts", {})
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="YouTube 账号不存在")
    ent = accounts[account_id]
    if ent.get("status") != "ready":
        raise HTTPException(status_code=400, detail="账号未完成授权")

    proxy_url = build_httpx_proxy_url(ent.get("proxy_server"), ent.get("proxy_username"), ent.get("proxy_password")) if ent.get("proxy_server") else None
    try:
        data = await sync_youtube_account_data(ent, proxy_url=proxy_url)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return data


@router.post("/api/youtube-publish/accounts/{account_id}/sync-analytics")
async def youtube_sync_analytics(
    account_id: str,
    current_user: _ServerUser = Depends(require_skill_store_admin),
):
    doc = _load_doc(current_user.id)
    accounts = doc.get("accounts", {})
    if account_id not in accounts:
        raise HTTPException(status_code=404, detail="YouTube 账号不存在")
    ent = accounts[account_id]
    if ent.get("status") != "ready":
        raise HTTPException(status_code=400, detail="账号未完成授权")

    proxy_url = build_httpx_proxy_url(ent.get("proxy_server"), ent.get("proxy_username"), ent.get("proxy_password")) if ent.get("proxy_server") else None
    try:
        data = await sync_youtube_account_data(ent, proxy_url=proxy_url)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"status": "synced", "data": data}

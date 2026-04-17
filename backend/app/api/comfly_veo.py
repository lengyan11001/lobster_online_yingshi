"""爆款TVC 技能内的分步 Veo 能力：供本机 MCP invoke_capability(comfly.veo) 调用。"""
import ipaddress
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from ..models import UserComflyConfig
from ..services.comfly_veo_exec import LOCAL_COMFLY_CONFIG_USER_ID, run_comfly_veo
from .auth import _ServerUser, get_current_user_for_local, get_current_user_media_edit

logger = logging.getLogger(__name__)
router = APIRouter()


def _default_comfly_api_base() -> str:
    return ((settings.comfly_api_base or "").strip().rstrip("/")) or "https://ai.comfly.chat/v1"


def _mask_secret(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    if len(t) <= 10:
        return "••••"
    return t[:4] + "…" + t[-4:]


def _is_local_comfly_config_request(request: Request) -> bool:
    """Only the desktop/local API may fall back to the shared local Comfly config."""
    host = (request.url.hostname or request.headers.get("host") or "").strip().lower()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    if ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _bearer_token_from_request(request: Request) -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


async def _resolve_comfly_config_user(request: Request) -> _ServerUser:
    token = _bearer_token_from_request(request)
    auth_error: Optional[HTTPException] = None
    if token:
        try:
            return await get_current_user_for_local(request=request, token=token)
        except HTTPException as exc:
            auth_error = exc
            logger.info(
                "[comfly_config] auth unavailable, checking local fallback status=%s detail=%s",
                exc.status_code,
                exc.detail,
            )
    if _is_local_comfly_config_request(request):
        return _ServerUser(id=LOCAL_COMFLY_CONFIG_USER_ID)
    if auth_error is not None:
        raise auth_error
    raise HTTPException(status_code=401, detail="无法验证凭证")


class ComflyUserConfigBody(BaseModel):
    """api_key / api_base 传 null 或不传表示不修改；传空字符串表示清除该项（清除后爆款TVC 不可用，直至重新填写）。"""

    api_key: Optional[str] = None
    api_base: Optional[str] = None


@router.get("/api/comfly/config", summary="爆款TVC：Comfly 凭据状态（技能卡片）")
async def get_comfly_user_config(
    current_user: _ServerUser = Depends(_resolve_comfly_config_user),
    db: Session = Depends(get_db),
):
    row = db.query(UserComflyConfig).filter(UserComflyConfig.user_id == current_user.id).first()
    uk = (row.api_key or "").strip() if row else ""
    ub = (row.api_base or "").strip().rstrip("/") if row else ""
    hint = _default_comfly_api_base()
    effective = bool(uk)
    return {
        "has_user_key": bool(uk),
        "masked_user_key": _mask_secret(uk) if uk else "",
        "user_api_base": ub,
        "default_api_base_hint": hint,
        "effective_ready": effective,
    }


@router.post("/api/comfly/config", summary="保存爆款TVC 所需 Comfly 凭据")
async def post_comfly_user_config(
    body: ComflyUserConfigBody,
    current_user: _ServerUser = Depends(_resolve_comfly_config_user),
    db: Session = Depends(get_db),
):
    row = db.query(UserComflyConfig).filter(UserComflyConfig.user_id == current_user.id).first()
    if row is None:
        row = UserComflyConfig(user_id=current_user.id)
        db.add(row)
    if body.api_key is not None:
        v = body.api_key.strip()
        row.api_key = v if v else None
    if body.api_base is not None:
        v = body.api_base.strip()
        row.api_base = v.rstrip("/") if v else None
    db.commit()
    return {"ok": True, "message": "爆款TVC · Comfly 凭据已保存，仅做本机保存，不会在保存时校验远端凭据"}


class ComflyVeoRunBody(BaseModel):
    payload: Dict[str, Any]


@router.post("/api/comfly-veo/run")
async def comfly_veo_run(
    body: ComflyVeoRunBody,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    try:
        out = await run_comfly_veo(body.payload or {}, current_user.id, request, db)
        logger.info("[comfly_veo] ok user_id=%s action=%s", current_user.id, (body.payload or {}).get("action"))
        return out
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[comfly_veo] failed user_id=%s err=%s", current_user.id, e)
        raise HTTPException(status_code=500, detail=str(e)[:2000]) from e

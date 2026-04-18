"""User settings: model selection, preferences."""
import json
import logging
import socket
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import (
    get_current_user,
    get_current_user_for_local,
    get_current_user_media_edit,
    _ServerUser,
)
from .openclaw_config import clear_openclaw_local_provider_keys
from ..models import ConsumptionAccount, User

logger = logging.getLogger(__name__)

router = APIRouter()

_CUSTOM_CONFIGS_FILE = Path(__file__).resolve().parent.parent.parent.parent / "custom_configs.json"
_CLIENT_CODE_VERSION_FILE = Path(__file__).resolve().parent.parent.parent.parent / "CLIENT_CODE_VERSION.json"


def _local_tos_config_has_credentials() -> bool:
    """本机 custom_configs.json 是否已含可用 TOS 凭据（有则不再覆盖）。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return False
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("TOS_CONFIG")
        if not isinstance(cfg, dict):
            return False
        return bool(str(cfg.get("access_key", "")).strip() and str(cfg.get("secret_key", "")).strip())
    except Exception:
        return False


_DEFAULT_CLIENT_SEMVER = "1.0.0"


def _read_client_code_version_for_ui() -> tuple[int, Optional[str], str]:
    """本机纯代码包 OTA：build 用于比对；version 为展示用语义版本（默认 1.0.0）。"""
    try:
        if not _CLIENT_CODE_VERSION_FILE.is_file():
            return 0, None, _DEFAULT_CLIENT_SEMVER
        data = json.loads(_CLIENT_CODE_VERSION_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return 0, None, _DEFAULT_CLIENT_SEMVER
        b = data.get("build", 0)
        build = int(b) if b is not None else 0
        at = data.get("applied_at")
        applied = str(at).strip() if at else None
        ver = str(data.get("version", "") or "").strip() or _DEFAULT_CLIENT_SEMVER
        return build, applied or None, ver
    except Exception:
        return 0, None, _DEFAULT_CLIENT_SEMVER


@router.get("/api/edition", summary="在线版（固定 edition=online）")
async def get_edition():
    use_independent = getattr(settings, "lobster_independent_auth", True)
    cb, cat, cver = _read_client_code_version_for_ui()
    out: dict = {
        "edition": "online",
        "use_independent_auth": bool(use_independent),
        "allow_self_config_model": getattr(settings, "sutui_online_model_self_config", True),
        "client_code_build": cb,
        "client_code_applied_at": cat,
        "client_code_version": cver,
    }
    if not use_independent:
        out["recharge_url"] = (getattr(settings, "sutui_recharge_url", None) or "").strip() or None
    out["use_fubei_pay"] = False
    out["use_own_wechat_login"] = False
    base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if base:
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                r = await client.get(f"{base}/api/edition")
            if r.status_code == 200:
                remote = r.json()
                if isinstance(remote, dict):
                    if "use_fubei_pay" in remote:
                        out["use_fubei_pay"] = bool(remote.get("use_fubei_pay"))
                    if "use_own_wechat_login" in remote:
                        out["use_own_wechat_login"] = bool(remote.get("use_own_wechat_login"))
        except Exception as e:
            logger.debug("edition merge from auth server failed: %s", e)
    return out


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class UpdateSettingsRequest(BaseModel):
    preferred_model: Optional[str] = None


@router.get("/api/settings", summary="获取用户设置")
def get_settings(current_user: User = Depends(get_current_user)):
    preferred = "sutui"
    return {"preferred_model": preferred}


@router.post("/api/settings", summary="更新用户设置")
def update_settings(
    body: UpdateSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.preferred_model is not None:
        current_user.preferred_model = body.preferred_model.strip() or "openclaw"
    db.commit()
    return {"preferred_model": current_user.preferred_model}


def _load_local_model_entries() -> list:
    """本机 openclaw / custom_configs 中的可选直连模型（与单机版同源逻辑）。"""
    base_dir = Path(__file__).resolve().parent.parent.parent.parent
    models = []

    config_path = base_dir / "models_config.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            models = data.get("models", [])
        except Exception:
            pass
    if not models:
        models = [
            {"id": "openclaw", "name": "默认 (OpenClaw)", "description": "OpenClaw 默认路由"},
            {"id": "anthropic/claude-sonnet-4-5", "name": "Claude Sonnet 4.5", "description": "Anthropic 快速模型"},
            {"id": "openai/gpt-4o", "name": "GPT-4o", "description": "OpenAI 多模态模型"},
            {"id": "deepseek/deepseek-chat", "name": "DeepSeek Chat", "description": "DeepSeek 对话模型"},
        ]

    existing_ids = {m.get("id") for m in models}

    custom_path = base_dir / "custom_configs.json"
    if custom_path.exists():
        try:
            custom_data = json.loads(custom_path.read_text(encoding="utf-8"))
            for cm in custom_data.get("custom_models", []):
                mid = cm.get("model_id", "")
                if mid and mid not in existing_ids:
                    models.append({
                        "id": mid,
                        "name": cm.get("display_name") or mid,
                        "description": cm.get("provider", "自定义模型"),
                        "custom": True,
                    })
                    existing_ids.add(mid)
        except Exception:
            pass

    return models


@router.get("/api/settings/models", summary="可选模型列表")
def list_models():
    out = [
        {
            "id": "sutui_aggregate",
            "name": "速推聚合",
            "description": "速推多模型；进入智能会话后在子下拉选择具体模型",
        }
    ]
    if getattr(settings, "sutui_online_model_self_config", True):
        for m in _load_local_model_entries():
            mid = m.get("id")
            if not mid or mid in ("sutui", "sutui_aggregate"):
                continue
            out.append(m)
    return {"models": out}


@router.get("/api/settings/lan-info", summary="获取局域网访问信息")
def get_lan_info():
    ip = _get_lan_ip()
    port = getattr(settings, "port", 8000)
    return {
        "lan_ip": ip,
        "port": port,
        "url": f"http://{ip}:{port}",
    }


@router.post(
    "/api/settings/sync-tos-from-server",
    summary="本机未配 TOS 时从认证中心拉取并写入 custom_configs.json",
)
async def sync_tos_from_server(
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    """仅当本机无 TOS_CONFIG 凭据时，从 AUTH_SERVER_BASE 拉取服务器上的 TOS 并写入本机，供上传/附图走公网 URL。"""
    base = (settings.auth_server_base or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="未配置 AUTH_SERVER_BASE，无法拉取云端 TOS")
    if _local_tos_config_has_credentials():
        return {
            "ok": True,
            "skipped": True,
            "reason": "本机已存在 TOS_CONFIG（access_key/secret_key），不覆盖",
        }
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth:
        raise HTTPException(status_code=401, detail="缺少 Authorization")
    url = f"{base}/api/settings/tos-config"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers={"Authorization": auth})
    except Exception as e:
        logger.exception("[sync-tos] 请求认证中心失败")
        raise HTTPException(status_code=503, detail=f"无法连接认证中心: {e!s}") from e
    if r.status_code == 404:
        return {
            "ok": False,
            "skipped": True,
            "reason": "认证中心未配置 TOS_CONFIG",
        }
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"认证中心返回 {r.status_code}: {(r.text or '')[:400]}",
        )
    try:
        body: Any = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail="认证中心响应非 JSON") from e
    tos = body.get("TOS_CONFIG") if isinstance(body, dict) else None
    if not isinstance(tos, dict) or not str(tos.get("access_key", "")).strip():
        return {"ok": False, "skipped": True, "reason": "响应中无有效 TOS_CONFIG"}
    if _CUSTOM_CONFIGS_FILE.exists():
        try:
            data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"configs": {}, "custom_models": []}
    else:
        data = {"configs": {}, "custom_models": []}
    data.setdefault("configs", {})["TOS_CONFIG"] = tos
    _CUSTOM_CONFIGS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "[sync-tos] 已从认证中心写入本机 TOS_CONFIG user_id=%s",
        getattr(current_user, "id", None),
    )
    return {"ok": True, "skipped": False, "message": "已写入本机 custom_configs.json"}


@router.post(
    "/api/settings/clear-local-user-config",
    summary="清除本机个人配置（OpenClaw 各厂商 Key、本机库 Token/算力账号等）",
)
async def clear_local_user_config(
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    """
    与云端「是否存用户 Key」无关：各厂商 API Key 仅写在本机 openclaw/.env，本接口会清除这些文件中的 Key。

    本机 lobster.db：若存在 users 行则清空速推 Token、首选模型；始终按 user_id 删除 consumption_accounts。
    不删除：云端算力与素材、登录态；不整文件删除 custom_configs.json（请用自定义配置 Tab 管理）。

    鉴权仅用于确认当前操作者（与素材库一致）；不在远端保存任何 Key。
    """
    oc_cleared, oc_restarted = clear_openclaw_local_provider_keys()
    parts: list[str] = []
    if oc_cleared:
        parts.append(
            "已清除本机 openclaw/.env 中的各厂商 API Key（Anthropic / OpenAI / DeepSeek / Gemini），仅本机文件"
        )
        if oc_restarted:
            parts.append("已尝试重启本机 OpenClaw Gateway")

    row = db.query(User).filter(User.id == current.id).first()
    if row is not None:
        row.sutui_token = None
        row.preferred_model = "sutui"
    n_del = db.query(ConsumptionAccount).filter(ConsumptionAccount.user_id == current.id).delete(
        synchronize_session=False
    )
    db.commit()

    if row is not None:
        parts.append("已清除本机数据库中的速推 Token、首选模型、算力账号列表")
    elif n_del:
        parts.append("已清除本机数据库中的算力账号记录（本机尚无 users 行，未存过速推 Token）")
    elif not oc_cleared:
        parts.append("本机数据库无该用户 users 行且无算力账号记录；OpenClaw 未配置过 Key 则无文件变更")

    return {
        "ok": True,
        "message": "；".join(parts) if parts else "已完成",
        "openclaw_keys_cleared": oc_cleared,
    }

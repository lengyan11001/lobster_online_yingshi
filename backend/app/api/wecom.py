"""企业微信：多应用配置 CRUD + 回调（直连时同步回复）+ 企业/产品/客户/消息 API + 轮询处理 + 资料模板。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import random
import string
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user, get_current_user_media_edit, _ServerUser
from .chat import get_customer_service_reply, get_reply_for_channel
from ..core.config import settings
from ..db import get_db
from ..models import (
    Customer,
    Enterprise,
    Product,
    User,
    WecomConfig,
    WecomMessage,
    WecomScheduledMessage,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _wecom_body_display(content: Optional[str], msg_type: Optional[str]) -> str:
    """Content 为空时按消息类型给出可读占位（图片/语音等无文字正文）。"""
    c = (content or "").strip()
    if c:
        return c
    mt = (msg_type or "text").strip().lower() or "text"
    labels = {
        "text": "[无正文]",
        "image": "[图片]",
        "voice": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "location": "[位置]",
        "link": "[链接]",
        "event": "[事件]",
        "shortvideo": "[短视频]",
        "emoji": "[表情]",
        "mixed": "[混合消息]",
    }
    return labels.get(mt, f"[{mt}]")

# 企微云端配置（界面配置，优先于环境变量）
_WECOM_CLOUD_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "wecom_cloud_config.json"


def _read_wecom_cloud_config() -> dict:
    if not _WECOM_CLOUD_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_WECOM_CLOUD_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_wecom_cloud_config(data: dict) -> None:
    _WECOM_CLOUD_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _get_wecom_cloud_url() -> str:
    url = (_read_wecom_cloud_config().get("wecom_cloud_url") or "").strip().rstrip("/")
    if url:
        return url
    return (settings.wecom_cloud_url or "").strip().rstrip("/")


def _get_wecom_forward_secret() -> str:
    secret = (_read_wecom_cloud_config().get("wecom_forward_secret") or "").strip()
    if secret:
        return secret
    return (settings.wecom_forward_secret or "").strip()


def _get_server_base_url() -> str:
    """获取云端服务器地址：优先 WECOM_CLOUD_URL → AUTH_SERVER_BASE，用于同步配置和轮询消息。"""
    url = _get_wecom_cloud_url()
    if url:
        return url
    return (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")


def _random_callback_path(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


# ---------------------------------------------------------------------------
# 从 skill 复用加解密与 XML 构造
# ---------------------------------------------------------------------------
def _get_crypt_and_helpers():
    try:
        from skills.wecom_reply.router import (
            WXBizMsgCrypt,
            _build_reply_xml,
            _parse_incoming_xml,
        )
        return WXBizMsgCrypt, _parse_incoming_xml, _build_reply_xml
    except Exception as e:
        logger.warning("[WeCom] 未加载 skill 加解密: %s", e)
        return None, None, None


# ---------------------------------------------------------------------------
# 配置 CRUD
# ---------------------------------------------------------------------------
class WecomConfigCreate(BaseModel):
    name: str = "默认应用"
    token: str
    encoding_aes_key: str
    corp_id: str = ""
    secret: Optional[str] = None
    contacts_secret: Optional[str] = None
    agent_id: Optional[int] = None
    product_knowledge: Optional[str] = None
    enterprise_id: Optional[int] = None
    product_id: Optional[int] = None


class WecomConfigUpdate(BaseModel):
    name: Optional[str] = None
    token: Optional[str] = None
    encoding_aes_key: Optional[str] = None
    corp_id: Optional[str] = None
    secret: Optional[str] = None
    contacts_secret: Optional[str] = None
    agent_id: Optional[int] = None
    product_knowledge: Optional[str] = None
    enterprise_id: Optional[int] = None
    product_id: Optional[int] = None


def _mask_secret(s: str) -> str:
    if not s or len(s) < 8:
        return "***" if s else ""
    return s[:4] + "***" + s[-4:] if len(s) > 8 else "***"


class WecomCloudConfigUpdate(BaseModel):
    wecom_cloud_url: Optional[str] = None
    wecom_forward_secret: Optional[str] = None


@router.get("/api/wecom/cloud-config", summary="读取企微云端配置（界面配置）")
def get_wecom_cloud_config(current_user = Depends(get_current_user_media_edit)):
    cfg = _read_wecom_cloud_config()
    url = (cfg.get("wecom_cloud_url") or "").strip().rstrip("/")
    secret = (cfg.get("wecom_forward_secret") or "").strip()
    return {
        "wecom_cloud_url": url,
        "has_wecom_cloud_url": bool(url),
        "wecom_forward_secret": _mask_secret(secret) if secret else "",
        "has_wecom_forward_secret": bool(secret),
    }


@router.post("/api/wecom/cloud-config", summary="保存企微云端配置")
def update_wecom_cloud_config(
    body: WecomCloudConfigUpdate,
    current_user = Depends(get_current_user_media_edit),
):
    cfg = _read_wecom_cloud_config()
    if body.wecom_cloud_url is not None:
        cfg["wecom_cloud_url"] = body.wecom_cloud_url.strip().rstrip("/")
    if body.wecom_forward_secret is not None:
        cfg["wecom_forward_secret"] = body.wecom_forward_secret.strip()
    _write_wecom_cloud_config(cfg)
    return {"ok": True, "message": "企微云端配置已保存"}


def _get_callback_base_url(request: Request) -> str:
    """企微回调 URL 的根地址：优先云端 → AUTH_SERVER_BASE → PUBLIC_BASE_URL → 请求地址。"""
    base = (_get_wecom_cloud_url() or "").strip().rstrip("/")
    if not base:
        base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not base:
        base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return base


@router.get("/api/wecom/configs", summary="企业微信配置列表")
def list_wecom_configs(
    request: Request,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    logger.info("[WeCom] GET /api/wecom/configs user_id=%s", current_user.id)
    rows = db.query(WecomConfig).filter(WecomConfig.user_id == current_user.id).order_by(WecomConfig.id).all()
    base = _get_callback_base_url(request)
    configs_out = []
    for r in rows:
        ent_name = ""
        prod_name = ""
        if r.enterprise_id:
            ent = db.query(Enterprise).filter(Enterprise.id == r.enterprise_id, Enterprise.user_id == current_user.id).first()
            if ent:
                ent_name = ent.name or ""
        if r.product_id:
            prod = db.query(Product).filter(Product.id == r.product_id).first()
            if prod:
                prod_name = prod.name or ""
        item = {
            "id": r.id,
            "name": r.name,
            "callback_path": r.callback_path,
            "callback_url": f"{base}/api/wecom/callback/{r.callback_path}",
            "corp_id": (r.corp_id or "")[:8] + "***" if r.corp_id else "",
            "has_secret": bool((getattr(r, 'secret', None) or "").strip()),
            "agent_id": getattr(r, 'agent_id', None),
            "has_product_knowledge": bool((r.product_knowledge or "").strip()),
            "enterprise_id": r.enterprise_id,
            "product_id": r.product_id,
            "enterprise_name": ent_name,
            "product_name": prod_name,
            "auto_reply_enabled": bool(getattr(r, 'auto_reply_enabled', False)),
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        configs_out.append(item)
    return {"configs": configs_out}


@router.post("/api/wecom/configs", summary="新增企业微信配置")
def create_wecom_config(
    body: WecomConfigCreate,
    request: Request,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    WXBizMsgCrypt, _, _ = _get_crypt_and_helpers()
    if not WXBizMsgCrypt:
        raise HTTPException(status_code=503, detail="企业微信能力未加载（请安装 pycryptodome）")
    raw_key = (body.encoding_aes_key or "").strip().rstrip("=")
    key = raw_key + "="
    try:
        WXBizMsgCrypt(body.token.strip(), raw_key, body.corp_id or "default")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"EncodingAESKey 无效: {e}")
    for _ in range(5):
        path = _random_callback_path()
        if db.query(WecomConfig).filter(WecomConfig.callback_path == path).first() is None:
            break
    else:
        raise HTTPException(status_code=500, detail="生成 callback_path 冲突")
    row = WecomConfig(
        user_id=current_user.id,
        name=(body.name or "默认应用").strip() or "默认应用",
        callback_path=path,
        token=body.token.strip(),
        encoding_aes_key=key,
        corp_id=(body.corp_id or "").strip(),
        secret=(body.secret or "").strip() or None,
        contacts_secret=(body.contacts_secret or "").strip() or None,
        agent_id=body.agent_id,
        product_knowledge=(body.product_knowledge or "").strip() or None,
        enterprise_id=body.enterprise_id,
        product_id=body.product_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _sync_config_to_cloud(row)
    base = _get_callback_base_url(request)
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "callback_url": f"{base}/api/wecom/callback/{row.callback_path}",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/api/wecom/configs/{config_id:int}", summary="获取单条配置（非敏感字段，用于编辑）")
def get_wecom_config(
    config_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    return {
        "id": row.id,
        "name": row.name,
        "callback_path": row.callback_path,
        "corp_id": row.corp_id or "",
        "secret": row.secret or "",
        "contacts_secret": getattr(row, 'contacts_secret', '') or "",
        "agent_id": row.agent_id,
        "product_knowledge": row.product_knowledge or "",
        "enterprise_id": row.enterprise_id,
        "product_id": row.product_id,
    }


@router.put("/api/wecom/configs/{config_id:int}", summary="更新企业微信配置")
def update_wecom_config(
    config_id: int,
    body: WecomConfigUpdate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    if body.name is not None:
        row.name = (body.name or "默认应用").strip() or "默认应用"
    if body.token is not None:
        row.token = body.token.strip()
    if body.encoding_aes_key is not None:
        key = body.encoding_aes_key.strip()
        if not key.endswith("="):
            key = key + "="
        row.encoding_aes_key = key
    if body.corp_id is not None:
        row.corp_id = (body.corp_id or "").strip()
    if body.secret is not None:
        row.secret = (body.secret or "").strip() or None
    if body.contacts_secret is not None:
        row.contacts_secret = (body.contacts_secret or "").strip() or None
    if body.agent_id is not None:
        row.agent_id = body.agent_id
    if body.product_knowledge is not None:
        row.product_knowledge = (body.product_knowledge or "").strip() or None
    if body.enterprise_id is not None:
        row.enterprise_id = body.enterprise_id
    if body.product_id is not None:
        row.product_id = body.product_id
    db.commit()
    db.refresh(row)
    _sync_config_to_cloud(row)
    return {"id": row.id, "name": row.name, "callback_path": row.callback_path}


@router.put("/api/wecom/configs/{config_id:int}/auto-reply", summary="切换自动回复开关")
def toggle_auto_reply(
    config_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    row.auto_reply_enabled = not row.auto_reply_enabled
    db.commit()
    db.refresh(row)
    logger.info("[WeCom] config_id=%s auto_reply_enabled=%s", config_id, row.auto_reply_enabled)
    return {"id": row.id, "auto_reply_enabled": row.auto_reply_enabled}


@router.delete("/api/wecom/configs/{config_id:int}", summary="删除企业微信配置")
def delete_wecom_config(
    config_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="配置不存在")
    callback_path = row.callback_path
    db.delete(row)
    db.commit()
    _delete_config_from_cloud(callback_path)
    return {"ok": True}


# ---------------------------------------------------------------------------
# 回调：按 callback_path 查配置并验签、解密、回复
# ---------------------------------------------------------------------------
def _find_config_by_path(db: Session, callback_path: str) -> Optional[WecomConfig]:
    return db.query(WecomConfig).filter(WecomConfig.callback_path == callback_path).first()


@router.get("/api/wecom/callback/{callback_path:path}", summary="企业微信回调 URL 校验")
async def wecom_callback_verify(
    callback_path: str,
    request: Request,
    msg_signature: str = "",
    timestamp: str = "",
    nonce: str = "",
    echostr: str = "",
    db: Session = Depends(get_db),
):
    WXBizMsgCrypt, _, _ = _get_crypt_and_helpers()
    if not WXBizMsgCrypt:
        return PlainTextResponse("WECOM_NOT_CONFIGURED", status_code=503)
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        return PlainTextResponse("config not found", status_code=404)
    try:
        crypt = WXBizMsgCrypt(cfg.token, cfg.encoding_aes_key, cfg.corp_id or "default")
        if not crypt.verify_signature(msg_signature, timestamp, nonce, echostr):
            logger.warning("[WeCom] GET 验签失败 path=%s", callback_path)
            return PlainTextResponse("invalid signature", status_code=400)
        plain = crypt.decrypt(echostr)
        return PlainTextResponse(plain)
    except Exception as e:
        logger.exception("[WeCom] GET 解密失败 path=%s: %s", callback_path, e)
        return PlainTextResponse("decrypt error", status_code=400)


@router.post("/api/wecom/callback/{callback_path:path}", summary="企业微信接收消息并自动回复")
async def wecom_callback_post(
    callback_path: str,
    request: Request,
    db: Session = Depends(get_db),
):
    WXBizMsgCrypt, _parse_incoming_xml, _build_reply_xml = _get_crypt_and_helpers()
    if not WXBizMsgCrypt or not _parse_incoming_xml or not _build_reply_xml:
        return Response(content="", status_code=503)
    cfg = _find_config_by_path(db, callback_path)
    if not cfg:
        return Response(content="", status_code=404)
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    body = await request.body()
    try:
        body_str = body.decode("utf-8")
    except Exception:
        body_str = body.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(body_str)
        encrypt_el = root.find("Encrypt")
        if encrypt_el is None or not (encrypt_el.text or "").strip():
            logger.warning("[WeCom] POST 无 Encrypt")
            return Response(content="", status_code=400)
        msg_encrypt = (encrypt_el.text or "").strip()
        crypt = WXBizMsgCrypt(cfg.token, cfg.encoding_aes_key, cfg.corp_id or "default")
        if not crypt.verify_signature(msg_signature, timestamp, nonce, msg_encrypt):
            logger.warning("[WeCom] POST 验签失败 path=%s", callback_path)
            return Response(content="", status_code=400)
        msg_xml = crypt.decrypt(msg_encrypt)
        parsed = _parse_incoming_xml(msg_xml)
        msg_type = (parsed.get("MsgType") or "").strip().lower()
        from_user = (parsed.get("FromUserName") or "").strip()
        to_user = (parsed.get("ToUserName") or "").strip()
        content = (parsed.get("Content") or "").strip()
        if msg_type != "text":
            reply_text = "当前仅支持文字消息，请发送文字。"
        else:
            session_id = f"wecom_{from_user}"
            product_extra = (cfg.product_knowledge or "").strip()
            if product_extra:
                product_extra = "\n【产品信息】\n" + product_extra
            reply_text = await get_reply_for_channel(
                content, session_id=session_id, system_prompt_extra=product_extra
            )
        reply_xml = _build_reply_xml(from_user, to_user, reply_text)
        reply_encrypt = crypt.encrypt(reply_xml)
        reply_nonce = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        reply_ts = str(int(time.time()))
        reply_sig = crypt._signature(cfg.token, reply_ts, reply_nonce, reply_encrypt)
        resp_xml = (
            "<xml>"
            "<Encrypt><![CDATA[{}]]></Encrypt>"
            "<MsgSignature><![CDATA[{}]]></MsgSignature>"
            "<TimeStamp>{}</TimeStamp>"
            "<Nonce><![CDATA[{}]]></Nonce>"
            "</xml>"
        ).format(reply_encrypt, reply_sig, reply_ts, reply_nonce)
        return Response(content=resp_xml, media_type="application/xml", status_code=200)
    except ET.ParseError as e:
        logger.warning("[WeCom] POST XML 解析失败: %s", e)
        return Response(content="", status_code=400)
    except Exception as e:
        logger.exception("[WeCom] POST 处理异常: %s", e)
        return Response(content="", status_code=500)


# ---------------------------------------------------------------------------
# 企业 / 产品 CRUD
# ---------------------------------------------------------------------------
class EnterpriseCreate(BaseModel):
    name: str
    company_info: Optional[str] = None


class EnterpriseUpdate(BaseModel):
    name: Optional[str] = None
    company_info: Optional[str] = None


class ProductCreate(BaseModel):
    enterprise_id: int
    name: str
    product_intro: Optional[str] = None
    common_phrases: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    product_intro: Optional[str] = None
    common_phrases: Optional[str] = None


@router.get("/api/wecom/enterprises", summary="企业列表")
def list_enterprises(
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    rows = db.query(Enterprise).filter(Enterprise.user_id == current_user.id).order_by(Enterprise.id).all()
    return {"items": [{"id": r.id, "name": r.name, "company_info": (r.company_info or "")[:500]} for r in rows]}


@router.post("/api/wecom/enterprises", summary="新增企业")
def create_enterprise(
    body: EnterpriseCreate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = Enterprise(
        user_id=current_user.id,
        name=(body.name or "").strip() or "未命名",
        company_info=(body.company_info or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.put("/api/wecom/enterprises/{ent_id:int}", summary="更新企业")
def update_enterprise(
    ent_id: int,
    body: EnterpriseUpdate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Enterprise).filter(Enterprise.id == ent_id, Enterprise.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="企业不存在")
    if body.name is not None:
        row.name = (body.name or "").strip() or "未命名"
    if body.company_info is not None:
        row.company_info = (body.company_info or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/api/wecom/enterprises/{ent_id:int}", summary="删除企业")
def delete_enterprise(
    ent_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Enterprise).filter(Enterprise.id == ent_id, Enterprise.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="企业不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/api/wecom/products", summary="产品列表")
def list_products(
    enterprise_id: Optional[int] = None,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    q = db.query(Product).join(Enterprise, Product.enterprise_id == Enterprise.id).filter(Enterprise.user_id == current_user.id)
    if enterprise_id is not None:
        q = q.filter(Product.enterprise_id == enterprise_id)
    rows = q.order_by(Product.id).all()
    return {"items": [{"id": r.id, "enterprise_id": r.enterprise_id, "name": r.name, "product_intro": (r.product_intro or "")[:300], "common_phrases": (r.common_phrases or "")[:300]} for r in rows]}


@router.post("/api/wecom/products", summary="新增产品")
def create_product(
    body: ProductCreate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    ent = db.query(Enterprise).filter(Enterprise.id == body.enterprise_id, Enterprise.user_id == current_user.id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="企业不存在")
    count = db.query(Product).filter(Product.enterprise_id == body.enterprise_id).count()
    if count >= 2:
        raise HTTPException(status_code=400, detail="每个企业最多 2 个产品")
    row = Product(
        enterprise_id=body.enterprise_id,
        name=(body.name or "").strip() or "未命名",
        product_intro=(body.product_intro or "").strip() or None,
        common_phrases=(body.common_phrases or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "enterprise_id": row.enterprise_id}


@router.put("/api/wecom/products/{prod_id:int}", summary="更新产品")
def update_product(
    prod_id: int,
    body: ProductUpdate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Product).join(Enterprise, Product.enterprise_id == Enterprise.id).filter(
        Product.id == prod_id, Enterprise.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="产品不存在")
    if body.name is not None:
        row.name = (body.name or "").strip() or "未命名"
    if body.product_intro is not None:
        row.product_intro = (body.product_intro or "").strip() or None
    if body.common_phrases is not None:
        row.common_phrases = (body.common_phrases or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/api/wecom/products/{prod_id:int}", summary="删除产品")
def delete_product(
    prod_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Product).join(Enterprise, Product.enterprise_id == Enterprise.id).filter(
        Product.id == prod_id, Enterprise.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="产品不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# 客户 CRUD
# ---------------------------------------------------------------------------
class CustomerCreate(BaseModel):
    wecom_config_id: int
    external_user_id: str = ""
    name: Optional[str] = None
    birthday: Optional[str] = None
    company: Optional[str] = None
    job: Optional[str] = None
    phone: Optional[str] = None
    remark: Optional[str] = None
    wechat_id: Optional[str] = None


class CustomerUpdate(BaseModel):
    name: Optional[str] = None
    birthday: Optional[str] = None
    company: Optional[str] = None
    job: Optional[str] = None
    phone: Optional[str] = None
    remark: Optional[str] = None
    wechat_id: Optional[str] = None


@router.get("/api/wecom/customers", summary="客户列表")
def list_customers(
    wecom_config_id: Optional[int] = None,
    name: Optional[str] = None,
    phone: Optional[str] = None,
    wechat_id: Optional[str] = None,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    q = db.query(Customer).join(WecomConfig, Customer.wecom_config_id == WecomConfig.id).filter(WecomConfig.user_id == current_user.id)
    if wecom_config_id is not None:
        q = q.filter(Customer.wecom_config_id == wecom_config_id)
    if name and (name := name.strip()):
        q = q.filter(Customer.name.ilike(f"%{name}%"))
    if phone and (phone := phone.strip()):
        q = q.filter(Customer.phone.ilike(f"%{phone}%"))
    if wechat_id and (wechat_id := wechat_id.strip()):
        q = q.filter(Customer.wechat_id.ilike(f"%{wechat_id}%"))
    rows = q.order_by(Customer.updated_at.desc()).limit(500).all()
    return {
        "items": [
            {
                "id": c.id,
                "wecom_config_id": c.wecom_config_id,
                "external_user_id": c.external_user_id,
                "name": c.name,
                "birthday": c.birthday,
                "company": c.company,
                "job": c.job,
                "phone": c.phone,
                "remark": c.remark,
                "wechat_id": c.wechat_id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
    }


@router.post("/api/wecom/customers", summary="新增/指定客户")
def create_customer(
    body: CustomerCreate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cfg = db.query(WecomConfig).filter(WecomConfig.id == body.wecom_config_id, WecomConfig.user_id == current_user.id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="企业微信配置不存在")
    external = (body.external_user_id or "").strip()
    if not external:
        raise HTTPException(status_code=400, detail="请填写 external_user_id 或微信号")
    existing = db.query(Customer).filter(Customer.wecom_config_id == body.wecom_config_id, Customer.external_user_id == external).first()
    if existing:
        for k, v in body.model_dump().items():
            if k in ("wecom_config_id", "external_user_id"):
                continue
            if v is not None and hasattr(existing, k):
                setattr(existing, k, v)
        db.commit()
        db.refresh(existing)
        return {"id": existing.id, "wecom_config_id": existing.wecom_config_id, "external_user_id": existing.external_user_id}
    row = Customer(
        wecom_config_id=body.wecom_config_id,
        external_user_id=external,
        name=(body.name or "").strip() or None,
        birthday=(body.birthday or "").strip() or None,
        company=(body.company or "").strip() or None,
        job=(body.job or "").strip() or None,
        phone=(body.phone or "").strip() or None,
        remark=(body.remark or "").strip() or None,
        wechat_id=(body.wechat_id or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "wecom_config_id": row.wecom_config_id, "external_user_id": row.external_user_id}


@router.get("/api/wecom/customers/{customer_id:int}", summary="客户详情")
def get_customer(
    customer_id: int,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Customer).join(WecomConfig, Customer.wecom_config_id == WecomConfig.id).filter(
        Customer.id == customer_id, WecomConfig.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="客户不存在")
    return {
        "id": row.id,
        "wecom_config_id": row.wecom_config_id,
        "external_user_id": row.external_user_id,
        "name": row.name,
        "birthday": row.birthday,
        "company": row.company,
        "job": row.job,
        "phone": row.phone,
        "remark": row.remark,
        "wechat_id": row.wechat_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.put("/api/wecom/customers/{customer_id:int}", summary="更新客户")
def update_customer(
    customer_id: int,
    body: CustomerUpdate,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(Customer).join(WecomConfig, Customer.wecom_config_id == WecomConfig.id).filter(
        Customer.id == customer_id, WecomConfig.user_id == current_user.id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="客户不存在")
    if body.name is not None:
        row.name = (body.name or "").strip() or None
    if body.birthday is not None:
        row.birthday = (body.birthday or "").strip() or None
    if body.company is not None:
        row.company = (body.company or "").strip() or None
    if body.job is not None:
        row.job = (body.job or "").strip() or None
    if body.phone is not None:
        row.phone = (body.phone or "").strip() or None
    if body.remark is not None:
        row.remark = (body.remark or "").strip() or None
    if body.wechat_id is not None:
        row.wechat_id = (body.wechat_id or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id}


# ---------------------------------------------------------------------------
# 消息记录
# ---------------------------------------------------------------------------
@router.get("/api/wecom/messages", summary="消息列表（支持按客户、企微应用、手机等筛选）")
def list_messages(
    wecom_config_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    q = db.query(WecomMessage).join(WecomConfig, WecomMessage.wecom_config_id == WecomConfig.id).filter(WecomConfig.user_id == current_user.id)
    if wecom_config_id is not None:
        q = q.filter(WecomMessage.wecom_config_id == wecom_config_id)
    if customer_id is not None:
        q = q.filter(WecomMessage.customer_id == customer_id)
    if phone and (phone := phone.strip()):
        q = q.join(Customer, WecomMessage.customer_id == Customer.id).filter(Customer.phone.ilike(f"%{phone}%"))
    if keyword and (keyword := keyword.strip()):
        q = q.filter(WecomMessage.content.ilike(f"%{keyword}%"))
    rows = q.order_by(WecomMessage.created_at.desc()).offset(offset).limit(min(limit, 200)).all()
    items = []
    for r in rows:
        cust = db.query(Customer).filter(Customer.id == r.customer_id).first() if r.customer_id else None
        items.append({
            "id": r.id,
            "wecom_config_id": r.wecom_config_id,
            "customer_id": r.customer_id,
            "direction": r.direction,
            "content": r.content,
            "msg_type": r.msg_type,
            "external_user_id": r.external_user_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "customer_name": cust.name if cust else None,
            "customer_phone": cust.phone if cust else None,
        })
    return {"items": items, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# 会话列表（按客户维度，用于消息记录左侧）
# ---------------------------------------------------------------------------
@router.get("/api/wecom/sessions", summary="会话列表（按客户，含最后一条消息预览）")
def list_sessions(
    wecom_config_id: Optional[int] = None,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    q = (
        db.query(WecomMessage)
        .join(WecomConfig, WecomMessage.wecom_config_id == WecomConfig.id)
        .filter(WecomConfig.user_id == current_user.id)
        .filter(WecomMessage.customer_id.isnot(None))
        .order_by(WecomMessage.created_at.desc())
    )
    if wecom_config_id is not None:
        q = q.filter(WecomMessage.wecom_config_id == wecom_config_id)
    rows = q.limit(2000).all()
    seen: set = set()
    sessions: List[Dict[str, Any]] = []
    for m in rows:
        if m.customer_id in seen:
            continue
        seen.add(m.customer_id)
        cust = db.query(Customer).filter(Customer.id == m.customer_id).first()
        pv = _wecom_body_display(m.content, m.msg_type)
        sessions.append({
            "customer_id": m.customer_id,
            "external_user_id": m.external_user_id,
            "wecom_config_id": m.wecom_config_id,
            "customer_name": cust.name if cust else None,
            "customer_phone": cust.phone if cust else None,
            "last_at": m.created_at.isoformat() if m.created_at else None,
            "last_preview": pv[:60] + ("…" if len(pv) > 60 else ""),
        })
    cust_ids = [s["customer_id"] for s in sessions]
    cust_map = {}
    if cust_ids:
        for c in db.query(Customer).filter(Customer.id.in_(cust_ids)).all():
            cust_map[c.id] = c
    for s in sessions:
        c = cust_map.get(s["customer_id"])
        if c:
            s["customer_name"] = c.name
            s["customer_phone"] = c.phone
    return {"items": sessions}


_PURCHASE_INTENT_KEYWORDS = ["我想购买InsClaw", "我想购买 INSclaw", "购买 INSclaw", "我想购买必火盒子"]
_PURCHASE_NOTIFY_USERS = "LiuXin|HeHao@BiHuoZhiNeng"


async def _check_purchase_intent_keyword(
    cfg,
    from_user: str,
    content: str,
    server_base: str,
    headers,
):
    text = (content or "").strip()
    if not any(kw in text for kw in _PURCHASE_INTENT_KEYWORDS):
        return
    agent_id = cfg.agent_id
    corp_id = (cfg.corp_id or "").strip()
    if not agent_id or not corp_id:
        logger.warning("[WeCom] 购买意向检测命中但缺少 agent_id/corp_id, config_id=%s", cfg.id)
        return
    notify_text = f"有意向客户，用户ID：{from_user}"
    logger.info("[WeCom] 购买意向关键词匹配: from_user=%s config_id=%s -> 通知 %s", from_user, cfg.id, _PURCHASE_NOTIFY_USERS)
    try:
        body = {
            "callback_path": cfg.callback_path,
            "touser": _PURCHASE_NOTIFY_USERS,
            "msgtype": "text",
            "text": {"content": notify_text},
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(f"{server_base}/api/wecom/proxy/send-message", json=body, headers=headers)
            logger.info("[WeCom] 购买意向通知发送 status=%s", r.status_code)
    except Exception as e:
        logger.warning("[WeCom] 购买意向通知失败: %s", e)



# ---------------------------------------------------------------------------
# 轮询处理：从云端拉取待处理消息，生成客服回复并提交（供后台每2s调用；也可由前端“拉取”仅刷新不触发）
# ---------------------------------------------------------------------------
async def _do_poll_and_reply(user_id: int, db: Session) -> Dict[str, Any]:
    """内部：对指定 user_id 执行一次拉取并回复，返回 {processed, errors}。"""
    base = _get_server_base_url()
    secret = _get_wecom_forward_secret()
    if not base:
        return {"processed": 0, "errors": ["未配置服务器地址（WECOM_CLOUD_URL 或 AUTH_SERVER_BASE）"]}
    headers = {}
    if secret:
        headers["X-Forward-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(f"{base}/api/wecom/pending", params={"limit": 20}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.exception("[WeCom] 拉取 pending 失败 user_id=%s: %s", user_id, e)
        err_msg = str(e)
        # SSL WRONG_VERSION_NUMBER：通常为用 https 连了只提供 HTTP 的云端
        if "WRONG_VERSION_NUMBER" in err_msg or "wrong version number" in err_msg.lower():
            if base.strip().lower().startswith("https://"):
                err_msg = (
                    "云端地址使用了 https://，但云端服务可能未开启 TLS。"
                    "请到「企业微信-配置」将云端地址改为 http:// 开头（例如 http://服务器IP:8000），保存后重试。"
                    "若云端已配置 HTTPS，请确认端口与证书正确。"
                )
            else:
                err_msg = "与云端 TLS 协议不匹配，请确认云端地址协议（http/https）与云端实际服务一致。"
        return {"processed": 0, "errors": [err_msg]}
    items = data.get("items") or []
    processed = 0
    errors = []
    for it in items:
        msg_id = it.get("id")
        callback_path = (it.get("callback_path") or "").strip()
        from_user = (it.get("from_user") or "").strip()
        to_user = (it.get("to_user") or "").strip()
        content_raw = (it.get("content") or "").strip()
        msg_type_in = (it.get("msg_type") or "text").strip() or "text"
        content_stored = _wecom_body_display(content_raw, msg_type_in)
        if not callback_path or not msg_id:
            continue
        cfg = db.query(WecomConfig).filter(WecomConfig.callback_path == callback_path, WecomConfig.user_id == user_id).first()
        if not cfg:
            errors.append(f"未找到 callback_path={callback_path} 的配置")
            continue
        company_info = ""
        product_intro = ""
        common_phrases = ""
        if cfg.enterprise_id:
            ent = db.query(Enterprise).filter(Enterprise.id == cfg.enterprise_id, Enterprise.user_id == user_id).first()
            if ent:
                company_info = ent.company_info or ""
        if cfg.product_id:
            prod = db.query(Product).filter(Product.id == cfg.product_id).first()
            if prod:
                product_intro = prod.product_intro or ""
                common_phrases = prod.common_phrases or ""
        customer = db.query(Customer).filter(Customer.wecom_config_id == cfg.id, Customer.external_user_id == from_user).first()
        if not customer:
            customer = Customer(wecom_config_id=cfg.id, external_user_id=from_user)
            db.add(customer)
            db.commit()
            db.refresh(customer)
        db.add(WecomMessage(wecom_config_id=cfg.id, customer_id=customer.id, direction="in", content=content_stored, msg_type=msg_type_in, external_user_id=from_user, to_user=to_user))
        db.commit()
        await _check_purchase_intent_keyword(cfg, from_user, content_raw, base, headers)
        history = []
        recent = db.query(WecomMessage).filter(WecomMessage.wecom_config_id == cfg.id, WecomMessage.external_user_id == from_user).order_by(WecomMessage.created_at.desc()).limit(10).all()
        for m in reversed(recent):
            if m.direction == "in":
                history.append({"role": "user", "content": m.content})
            else:
                history.append({"role": "assistant", "content": m.content})
        reply_text = await get_customer_service_reply(content_stored, company_info=company_info, product_intro=product_intro, common_phrases=common_phrases, history=history)
        delay_s = random.uniform(2.0, 10.0)
        logger.info(
            "[WeCom] 模拟真人回复延时 %.1fs 后入库并提交 message_id=%s",
            delay_s,
            msg_id,
        )
        await asyncio.sleep(delay_s)
        db.add(WecomMessage(wecom_config_id=cfg.id, customer_id=customer.id, direction="out", content=reply_text, msg_type="text", external_user_id=from_user, to_user=to_user))
        db.commit()
        # 统一由服务器代发（服务器 IP 固定已加白名单），不在本地直发（本地出口 IP 会变）
        try:
            ack_body = {"message_id": msg_id, "reply_text": reply_text, "skip_send": False}
            async with httpx.AsyncClient(timeout=15.0) as client:
                r2 = await client.post(f"{base}/api/wecom/submit-reply", json=ack_body, headers=headers)
                r2.raise_for_status()
        except Exception as e:
            logger.exception("[WeCom] 提交/ack 失败 message_id=%s: %s", msg_id, e)
            errors.append(f"message_id={msg_id} 提交失败: {e}")
            continue
        processed += 1
    return {"processed": processed, "errors": errors}


@router.post("/api/wecom/poll-and-reply", summary="拉取云端待处理消息并回复（轮询一次，后台每2s自动调用）")
async def wecom_poll_and_reply(
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    base = _get_server_base_url()
    if not base:
        raise HTTPException(
            status_code=400,
            detail="未配置服务器地址，请在环境变量中设置 WECOM_CLOUD_URL 或 AUTH_SERVER_BASE。",
        )
    result = await _do_poll_and_reply(current_user.id, db)
    return result


async def wecom_poll_loop():
    """后台每 2 秒拉取云端待处理消息并 AI 回复（仅 auto_reply_enabled 的配置）。"""
    import asyncio
    from ..db import SessionLocal
    while True:
        await asyncio.sleep(2)
        if not _get_server_base_url():
            continue
        db = SessionLocal()
        try:
            enabled_uids = [
                r[0] for r in
                db.query(WecomConfig.user_id)
                .filter(WecomConfig.auto_reply_enabled == True)
                .distinct()
                .all()
            ]
            for uid in enabled_uids:
                try:
                    await _do_poll_and_reply(uid, db)
                except Exception as e:
                    logger.exception("[WeCom] 自动拉取回复 user_id=%s: %s", uid, e)
        except Exception as e:
            logger.exception("[WeCom] 自动拉取回复循环: %s", e)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 资料模板：下载与上传
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 同步配置到云端（创建/更新时自动推送）
# ---------------------------------------------------------------------------

def _delete_config_from_cloud(callback_path: str):
    """尽力从云端删除配置（不阻塞，失败仅记日志）。"""
    base = _get_server_base_url()
    if not base:
        return
    secret = _get_wecom_forward_secret()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Forward-Secret"] = secret
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10.0, verify=False) as client:
            r = client.post(f"{base}/api/wecom/proxy/delete-config", json={"callback_path": callback_path}, headers=headers)
            if r.status_code == 200:
                logger.info("[WeCom] 配置已从云端删除 callback_path=%s", callback_path)
            else:
                logger.warning("[WeCom] 从云端删除失败 %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("[WeCom] 从云端删除异常: %s", e)


def _sync_config_to_cloud(cfg: WecomConfig):
    """尽力将本地配置同步到云端（不阻塞，失败仅记日志）。"""
    base = _get_server_base_url()
    if not base:
        return
    secret = _get_wecom_forward_secret()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Forward-Secret"] = secret
    payload = {
        "callback_path": cfg.callback_path,
        "name": cfg.name or "默认应用",
        "token": cfg.token,
        "encoding_aes_key": cfg.encoding_aes_key,
        "corp_id": cfg.corp_id or "",
        "secret": cfg.secret or "",
        "contacts_secret": getattr(cfg, 'contacts_secret', '') or "",
        "agent_id": cfg.agent_id,
        "user_id": cfg.user_id,
    }
    try:
        import httpx as _httpx
        with _httpx.Client(timeout=10.0, verify=False) as client:
            r = client.post(f"{base}/api/wecom/proxy/sync-config", json=payload, headers=headers)
            if r.status_code == 200:
                logger.info("[WeCom] 配置已同步到云端 callback_path=%s", cfg.callback_path)
            else:
                logger.warning("[WeCom] 同步到云端失败 %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("[WeCom] 同步到云端异常: %s", e)


# ---------------------------------------------------------------------------
# 通讯录 / 发消息 / 群聊：代理到云端 lobster-server
# ---------------------------------------------------------------------------

async def _cloud_proxy_get(path: str, params: dict = None) -> dict:
    base = _get_server_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="未配置服务器地址（WECOM_CLOUD_URL 或 AUTH_SERVER_BASE）")
    secret = _get_wecom_forward_secret()
    headers = {}
    if secret:
        headers["X-Forward-Secret"] = secret
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        r = await client.get(f"{base}{path}", params=params or {}, headers=headers)
        if r.status_code != 200:
            try:
                d = r.json()
                raise HTTPException(status_code=r.status_code, detail=d.get("detail", r.text))
            except Exception:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()


async def _cloud_proxy_post(path: str, body: dict) -> dict:
    base = _get_server_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="未配置服务器地址（WECOM_CLOUD_URL 或 AUTH_SERVER_BASE）")
    secret = _get_wecom_forward_secret()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Forward-Secret"] = secret
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        r = await client.post(f"{base}{path}", json=body, headers=headers)
        if r.status_code != 200:
            try:
                d = r.json()
                raise HTTPException(status_code=r.status_code, detail=d.get("detail", r.text))
            except Exception:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()


def _get_callback_path_for_config(db: Session, config_id: int, user_id: int) -> str:
    cfg = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == user_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="配置不存在")
    return cfg.callback_path


@router.get("/api/wecom/contacts/departments", summary="通讯录-部门列表（代理到云端）")
async def wecom_contact_departments(
    config_id: int,
    parent_id: int = 0,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path_for_config(db, config_id, current_user.id)
    params = {"callback_path": cb_path}
    if parent_id:
        params["parent_id"] = parent_id
    return await _cloud_proxy_get("/api/wecom/proxy/contacts/departments", params)


@router.get("/api/wecom/contacts/users", summary="通讯录-成员列表（代理到云端）")
async def wecom_contact_users(
    config_id: int,
    department_id: int = 1,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path_for_config(db, config_id, current_user.id)
    return await _cloud_proxy_get(
        "/api/wecom/proxy/contacts/users",
        {"callback_path": cb_path, "department_id": department_id},
    )


class LocalSendMessageBody(BaseModel):
    config_id: int
    to_user: Optional[str] = None
    to_party: Optional[str] = None
    to_tag: Optional[str] = None
    msg_type: str = "text"
    content: str = ""
    media_id: Optional[str] = None


@router.post("/api/wecom/send-message", summary="发送应用消息（代理到云端）")
async def wecom_send_message(
    body: LocalSendMessageBody,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path_for_config(db, body.config_id, current_user.id)
    payload = {
        "callback_path": cb_path,
        "to_user": body.to_user,
        "to_party": body.to_party,
        "to_tag": body.to_tag,
        "msg_type": body.msg_type,
        "content": body.content,
    }
    if body.media_id:
        payload["media_id"] = body.media_id
    return await _cloud_proxy_post("/api/wecom/proxy/send-message", payload)


MAX_MEDIA_SIZE = 20 * 1024 * 1024
_WECOM_UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent.parent / "static" / "uploads" / "wecom"

@router.post("/api/wecom/media/upload", summary="上传临时素材到企微（代理到云端）")
async def wecom_media_upload(
    config_id: int = Query(...),
    media_type: str = Query("image"),
    file: UploadFile = File(...),
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    file_bytes = await file.read()
    if len(file_bytes) > MAX_MEDIA_SIZE:
        raise HTTPException(status_code=400, detail=f"文件过大，最大 {MAX_MEDIA_SIZE // 1024 // 1024}MB")
    cb_path = _get_callback_path_for_config(db, config_id, current_user.id)
    base = _get_server_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="未配置服务器地址")
    secret = _get_wecom_forward_secret()
    headers = {}
    if secret:
        headers["X-Forward-Secret"] = secret
    async with httpx.AsyncClient(timeout=60.0, verify=False) as client:
        r = await client.post(
            f"{base}/api/wecom/proxy/media/upload",
            params={"callback_path": cb_path, "media_type": media_type},
            files={"file": (file.filename or "file", file_bytes, file.content_type or "application/octet-stream")},
            headers=headers,
        )
        if r.status_code != 200:
            try:
                d = r.json()
                raise HTTPException(status_code=r.status_code, detail=d.get("detail", r.text))
            except Exception:
                raise HTTPException(status_code=r.status_code, detail=r.text)
        result = r.json()
    ext = Path(file.filename or "file").suffix or (".jpg" if media_type == "image" else ".mp4")
    local_name = f"{uuid.uuid4().hex}{ext}"
    _WECOM_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (Path(_WECOM_UPLOAD_DIR) / local_name).write_bytes(file_bytes)
    result["local_url"] = f"/static/uploads/wecom/{local_name}"
    return result


class CustomerSendBody(BaseModel):
    wecom_config_id: int
    customer_id: int
    content: str = ""
    msg_type: str = "text"
    media_id: Optional[str] = None


@router.post("/api/wecom/send-message-to-customer", summary="通过 customer_id 向客户发送消息")
async def wecom_send_to_customer(
    body: CustomerSendBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cust = db.query(Customer).filter(Customer.id == body.customer_id).first()
    if not cust:
        raise HTTPException(status_code=404, detail="客户不存在")
    cb_path = _get_callback_path_for_config(db, body.wecom_config_id, current_user.id)
    payload = {
        "callback_path": cb_path,
        "to_user": cust.external_user_id,
        "msg_type": body.msg_type,
        "content": body.content,
    }
    if body.media_id:
        payload["media_id"] = body.media_id
    result = await _cloud_proxy_post("/api/wecom/proxy/send-message", payload)
    content_stored = _wecom_body_display(body.content, body.msg_type)
    db.add(WecomMessage(
        wecom_config_id=body.wecom_config_id,
        customer_id=cust.id,
        direction="out",
        content=content_stored,
        msg_type=body.msg_type or "text",
        external_user_id=cust.external_user_id,
        to_user=cust.external_user_id,
    ))
    db.commit()
    return result


class LocalCreateGroupBody(BaseModel):
    config_id: int
    name: str = ""
    userlist: List[str]
    owner: Optional[str] = None


@router.post("/api/wecom/group-chat/create", summary="创建群聊（代理到云端）")
async def wecom_create_group_chat(
    body: LocalCreateGroupBody,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path_for_config(db, body.config_id, current_user.id)
    return await _cloud_proxy_post("/api/wecom/proxy/group-chat/create", {
        "callback_path": cb_path,
        "name": body.name,
        "userlist": body.userlist,
        "owner": body.owner,
    })


class LocalSendGroupMsgBody(BaseModel):
    config_id: int
    chatid: str
    msg_type: str = "text"
    content: str = ""


@router.post("/api/wecom/group-chat/send", summary="发送群聊消息（代理到云端）")
async def wecom_send_group_message(
    body: LocalSendGroupMsgBody,
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path_for_config(db, body.config_id, current_user.id)
    return await _cloud_proxy_post("/api/wecom/proxy/group-chat/send", {
        "callback_path": cb_path,
        "chatid": body.chatid,
        "msg_type": body.msg_type,
        "content": body.content,
    })


# ---------------------------------------------------------------------------
# 资料模板：下载与上传
# ---------------------------------------------------------------------------
MATERIAL_TEMPLATE_HEADERS = ["企业名称", "公司介绍", "产品名称", "产品介绍", "常用话术"]


@router.get("/api/wecom/material-template", summary="下载资料填写模板（CSV）")
def download_material_template(
    current_user = Depends(get_current_user_media_edit),
):
    """返回 CSV 模板，表头：企业名称、公司介绍、产品名称、产品介绍、常用话术。多行表示多产品（同一企业可多行）。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(MATERIAL_TEMPLATE_HEADERS)
    writer.writerow(["示例企业", "本公司主营……", "产品A", "产品A介绍……", "您好，请问有什么可以帮您？"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=wecom_materials_template.csv"},
    )


@router.post("/api/wecom/upload-materials", summary="上传已填写的资料 CSV，导入企业/产品")
async def upload_materials(
    file: UploadFile = File(...),
    current_user = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    """解析 CSV（UTF-8 或带 BOM），按行创建/更新企业与产品。企业名称+产品名称唯一，存在则更新。"""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 CSV 文件")
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig").strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解码失败: {e}")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    header = [h.strip() for h in rows[0]]
    col = {h: i for i, h in enumerate(header)}
    for h in MATERIAL_TEMPLATE_HEADERS:
        if h not in col:
            raise HTTPException(status_code=400, detail=f"缺少列: {h}")
    created_ent = 0
    created_prod = 0
    updated_ent = 0
    updated_prod = 0
    errors = []
    ncols = len(header)
    for idx, row in enumerate(rows[1:], start=2):
        try:
            if len(row) < ncols:
                errors.append(
                    f"第 {idx} 行：列数不足（需 {ncols} 列，实际 {len(row)} 列）。"
                    "「产品介绍」与「常用话术」之间须用英文逗号 , 分隔；"
                    "若某列正文含英文逗号，请将该列用英文双引号包裹。"
                )
                continue
            ent_name = (row[col["企业名称"]] or "").strip()
            company_info = (row[col["公司介绍"]] or "").strip()
            prod_name = (row[col["产品名称"]] or "").strip()
            product_intro = (row[col["产品介绍"]] or "").strip()
            common_phrases = (row[col["常用话术"]] or "").strip()
            if not ent_name or not prod_name:
                errors.append(f"第 {idx} 行：企业名称和产品名称必填")
                continue
            ent = db.query(Enterprise).filter(Enterprise.user_id == current_user.id, Enterprise.name == ent_name).first()
            if not ent:
                ent = Enterprise(user_id=current_user.id, name=ent_name, company_info=company_info or None)
                db.add(ent)
                db.commit()
                db.refresh(ent)
                created_ent += 1
            else:
                if company_info:
                    ent.company_info = company_info
                    db.commit()
                    updated_ent += 1
            prods = db.query(Product).filter(Product.enterprise_id == ent.id, Product.name == prod_name).all()
            if not prods:
                count = db.query(Product).filter(Product.enterprise_id == ent.id).count()
                if count >= 2:
                    errors.append(f"第 {idx} 行：企业「{ent_name}」已有 2 个产品，无法再添加")
                    continue
                prod = Product(enterprise_id=ent.id, name=prod_name, product_intro=product_intro or None, common_phrases=common_phrases or None)
                db.add(prod)
                db.commit()
                created_prod += 1
            else:
                prod = prods[0]
                prod.product_intro = product_intro or prod.product_intro
                prod.common_phrases = common_phrases or prod.common_phrases
                db.commit()
                updated_prod += 1
        except Exception as e:
            errors.append(f"第 {idx} 行: {e}")
    return {
        "created_enterprises": created_ent,
        "created_products": created_prod,
        "updated_enterprises": updated_ent,
        "updated_products": updated_prod,
        "errors": errors,
    }


# ─── 定时消息 ───────────────────────────────────────────────

class ScheduledMessageCreate(BaseModel):
    wecom_config_id: int
    send_type: str = "user"
    to_user: Optional[str] = None
    to_party: Optional[str] = None
    chatid: Optional[str] = None
    msg_type: str = "text"
    content: str
    weekdays: str
    send_time: str


@router.get("/api/wecom/scheduled-messages", summary="列出定时消息")
async def list_scheduled_messages(
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    items = (
        db.query(WecomScheduledMessage)
        .filter(WecomScheduledMessage.user_id == current_user.id)
        .order_by(WecomScheduledMessage.created_at.desc())
        .all()
    )
    return {
        "items": [
            {
                "id": m.id,
                "wecom_config_id": m.wecom_config_id,
                "send_type": m.send_type,
                "to_user": m.to_user,
                "to_party": m.to_party,
                "chatid": m.chatid,
                "msg_type": m.msg_type,
                "content": m.content[:80],
                "weekdays": m.weekdays,
                "send_time": m.send_time,
                "enabled": m.enabled,
                "last_sent_at": str(m.last_sent_at) if m.last_sent_at else None,
                "created_at": str(m.created_at),
            }
            for m in items
        ]
    }


@router.post("/api/wecom/scheduled-messages", summary="创建定时消息")
async def create_scheduled_message(
    body: ScheduledMessageCreate,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    if not body.content.strip():
        raise HTTPException(400, "消息内容不能为空")
    if not body.weekdays.strip():
        raise HTTPException(400, "请选择至少一个星期几")
    if not body.send_time.strip():
        raise HTTPException(400, "请设置发送时间")
    m = WecomScheduledMessage(
        user_id=current_user.id,
        wecom_config_id=body.wecom_config_id,
        send_type=body.send_type,
        to_user=body.to_user,
        to_party=body.to_party,
        chatid=body.chatid,
        msg_type=body.msg_type,
        content=body.content.strip(),
        weekdays=body.weekdays.strip(),
        send_time=body.send_time.strip(),
        enabled=True,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return {"id": m.id, "ok": True}


@router.delete("/api/wecom/scheduled-messages/{msg_id}", summary="删除定时消息")
async def delete_scheduled_message(
    msg_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    m = (
        db.query(WecomScheduledMessage)
        .filter(WecomScheduledMessage.id == msg_id, WecomScheduledMessage.user_id == current_user.id)
        .first()
    )
    if not m:
        raise HTTPException(404, "定时消息不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


@router.put("/api/wecom/scheduled-messages/{msg_id}/toggle", summary="启用/禁用定时消息")
async def toggle_scheduled_message(
    msg_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    m = (
        db.query(WecomScheduledMessage)
        .filter(WecomScheduledMessage.id == msg_id, WecomScheduledMessage.user_id == current_user.id)
        .first()
    )
    if not m:
        raise HTTPException(404, "定时消息不存在")
    m.enabled = not m.enabled
    db.commit()
    return {"ok": True, "enabled": m.enabled}


async def _execute_scheduled_messages():
    """由后台定时任务调用：检查当前分钟是否有待发送的定时消息。"""
    from datetime import datetime as dt
    from ..db import SessionLocal

    now = dt.now()
    weekday = now.isoweekday()
    current_time = now.strftime("%H:%M")
    db = SessionLocal()
    try:
        items = (
            db.query(WecomScheduledMessage)
            .filter(WecomScheduledMessage.enabled == True)
            .all()
        )
        for m in items:
            days = [int(d.strip()) for d in m.weekdays.split(",") if d.strip().isdigit()]
            if weekday not in days:
                continue
            if m.send_time.strip() != current_time:
                continue
            if m.last_sent_at and m.last_sent_at.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
                continue
            try:
                cfg = db.query(WecomConfig).filter(WecomConfig.id == m.wecom_config_id).first()
                if not cfg:
                    continue
                cb_path = cfg.callback_path
                if m.send_type == "group" and m.chatid:
                    await _cloud_proxy_post("/api/wecom/proxy/group-chat/send", {
                        "callback_path": cb_path,
                        "chatid": m.chatid,
                        "msg_type": m.msg_type,
                        "content": m.content,
                    })
                else:
                    await _cloud_proxy_post("/api/wecom/proxy/send-message", {
                        "callback_path": cb_path,
                        "to_user": m.to_user if m.send_type == "user" else None,
                        "to_party": m.to_party if m.send_type == "party" else None,
                        "msg_type": m.msg_type,
                        "content": m.content,
                    })
                m.last_sent_at = now
                db.commit()
                logger.info("[WECOM] 定时消息 id=%s 已发送", m.id)
            except Exception as e:
                logger.warning("[WECOM] 定时消息 id=%s 发送失败: %s", m.id, e)
    finally:
        db.close()

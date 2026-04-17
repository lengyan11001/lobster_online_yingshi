"""Twilio WhatsApp（Sandbox/正式号）：本地可配置 + 入站 Webhook；云端入队后本机轮询 AI 回复（与企微链路一致）。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import SessionLocal, get_db
from ..models import TwilioKbEnterprise, TwilioKbProduct, TwilioWhatsappMessage, User
from .auth import _ServerUser, get_current_user_media_edit
from .chat import get_customer_service_reply
from .wecom import _get_wecom_forward_secret

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_TWILIO_CONFIG_PATH = _BASE_DIR / "twilio_whatsapp_config.json"
_INBOUND_PATH = "/api/twilio/whatsapp/inbound"
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twilio_body_display(content: Optional[str], msg_type: Optional[str]) -> str:
    """Body 为空时（如仅媒体）给出可读预览，与会话列表展示一致。"""
    c = (content or "").strip()
    if c:
        return c
    if (msg_type or "").strip().lower() == "media":
        return "[媒体，无文字说明]"
    return "[无正文]"


def _read_twilio_file() -> dict:
    if not _TWILIO_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_TWILIO_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_twilio_file(data: dict) -> None:
    _TWILIO_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _mask_sid(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= 8:
        return "****" if s else ""
    return s[:4] + "…" + s[-4:]


def _mask_token_set() -> bool:
    return bool((_read_twilio_file().get("auth_token") or "").strip()) or bool(
        (getattr(settings, "twilio_auth_token", None) or "").strip()
    )


def _twilio_poll_auto_enabled() -> bool:
    """后台定时拉取 pending 并自动回复；默认关闭，用户在 UI 中手动开启后才生效。"""
    v = _read_twilio_file().get("poll_auto_enabled")
    if v is True:
        return True
    return False


class TwilioPollAutoBody(BaseModel):
    enabled: bool


def effective_auth_token() -> str:
    f = _read_twilio_file()
    return (f.get("auth_token") or "").strip() or (
        getattr(settings, "twilio_auth_token", None) or ""
    ).strip()


def effective_account_sid() -> str:
    f = _read_twilio_file()
    return (f.get("account_sid") or "").strip() or (
        getattr(settings, "twilio_account_sid", None) or ""
    ).strip()


def _twilio_upstream_http_client(timeout: float = 60.0) -> httpx.Client:
    """本机→公网 lobster_server：不信任环境代理（HTTP_PROXY 等），避免经 Clash 等代理时 TLS 握手 EOF。"""
    return httpx.Client(timeout=timeout, trust_env=False, http2=False)


def _twilio_remote_upstream_base() -> str:
    """在线版默认海外 lobster_server；显式设 TWILIO_REMOTE_API_BASE= 空可关闭。"""
    raw = getattr(settings, "twilio_remote_api_base", None)
    if raw is not None and str(raw).strip() == "":
        return ""
    if raw is not None and str(raw).strip():
        return str(raw).strip().rstrip("/")
    edition = (getattr(settings, "lobster_edition", None) or "").strip().lower()
    if edition == "online":
        return "http://43.162.111.36"
    return ""


def _remote_forward_twilio(
    request: Request, method: str, path: str, json_body: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    base = _twilio_remote_upstream_base()
    if not base:
        raise HTTPException(status_code=500, detail="未配置 twilio_remote_api_base")
    url = f"{base.rstrip('/')}{path}"
    auth = (request.headers.get("Authorization") or "").strip()
    headers: Dict[str, str] = {}
    if auth:
        headers["Authorization"] = auth
    if method.upper() == "POST" and json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        with _twilio_upstream_http_client() as client:
            if method.upper() == "GET":
                r = client.get(url, headers=headers)
            else:
                r = client.post(url, headers=headers, json=json_body)
    except httpx.RequestError as e:
        logger.warning("[Twilio WA] 转发远程失败: %s", e)
        raise HTTPException(status_code=502, detail=f"无法连接远程 Twilio 接口: {e}") from e
    try:
        data = r.json()
    except Exception:
        data = {"detail": (r.text or "")[:800] if r.text else f"HTTP {r.status_code}"}
    return JSONResponse(content=data, status_code=r.status_code)


def effective_signature_url(request: Request) -> str:
    """签名校验 URL：以服务器 .env 为准，其次为旧版 JSON。"""
    path = request.url.path
    explicit = (getattr(settings, "twilio_whatsapp_webhook_full_url", None) or "").strip()
    if explicit:
        return explicit
    public = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if public:
        return public + path
    f = _read_twilio_file()
    explicit = (f.get("webhook_full_url") or "").strip()
    if explicit:
        return explicit
    pub = (f.get("public_base") or "").strip().rstrip("/")
    if pub:
        return pub + path
    return str(request.url)


def _twilio_webhook_suggested() -> str:
    path = _INBOUND_PATH
    wh_full = (getattr(settings, "twilio_whatsapp_webhook_full_url", None) or "").strip()
    if wh_full:
        return wh_full
    env_pub = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if env_pub:
        return env_pub + path
    f = _read_twilio_file()
    wh_full = (f.get("webhook_full_url") or "").strip()
    if wh_full:
        return wh_full
    pub = (f.get("public_base") or "").strip().rstrip("/")
    if pub:
        return pub + path
    return ""


def _form_to_str_dict(form: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key in form.keys():
        v = form.get(key)
        if v is not None:
            out[str(key)] = v if isinstance(v, str) else str(v)
    return out


class TwilioTestSendBody(BaseModel):
    to: str
    from_whatsapp: str
    body: str = "Lobster 测试"


class TwilioWhatsappConfigUpdate(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    # WhatsApp 专用资料库（twilio_kb_*），与企微 Enterprise/Product 不复用
    twilio_kb_enterprise_id: Optional[int] = None
    twilio_kb_product_id: Optional[int] = None


class TwilioKbEnterpriseCreate(BaseModel):
    name: str
    company_info: Optional[str] = None


class TwilioKbEnterpriseUpdate(BaseModel):
    name: Optional[str] = None
    company_info: Optional[str] = None


class TwilioKbProductCreate(BaseModel):
    enterprise_id: int
    name: str
    product_intro: Optional[str] = None
    common_phrases: Optional[str] = None


class TwilioKbProductUpdate(BaseModel):
    name: Optional[str] = None
    product_intro: Optional[str] = None
    common_phrases: Optional[str] = None


def _twilio_knowledge_materials(db: Session) -> Tuple[str, str, str, Optional[int]]:
    """从 twilio_whatsapp_config.json 的 twilio_kb_* 读取 WhatsApp 专用公司/产品资料（与企微表独立）。"""
    f = _read_twilio_file()
    raw_uid = f.get("knowledge_user_id")
    try:
        uid = int(raw_uid) if raw_uid is not None else None
    except (TypeError, ValueError):
        uid = None
    if uid is None:
        u = db.query(User).order_by(User.id.asc()).first()
        uid = u.id if u else None
    if not uid:
        return "", "", "", None
    company_info = ""
    product_intro = ""
    common_phrases = ""
    eid = f.get("twilio_kb_enterprise_id")
    pid = f.get("twilio_kb_product_id")
    try:
        eid_i = int(eid) if eid is not None else None
    except (TypeError, ValueError):
        eid_i = None
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    if eid_i is not None:
        ent = db.query(TwilioKbEnterprise).filter(TwilioKbEnterprise.id == eid_i, TwilioKbEnterprise.user_id == uid).first()
        if ent:
            company_info = ent.company_info or ""
    if pid_i is not None:
        prod = (
            db.query(TwilioKbProduct)
            .join(TwilioKbEnterprise, TwilioKbProduct.enterprise_id == TwilioKbEnterprise.id)
            .filter(TwilioKbProduct.id == pid_i, TwilioKbEnterprise.user_id == uid)
            .first()
        )
        if prod:
            product_intro = prod.product_intro or ""
            common_phrases = prod.common_phrases or ""
    return company_info, product_intro, common_phrases, uid


def _twilio_history_for_peer(db: Session, peer_id: str) -> List[Dict[str, str]]:
    recent = (
        db.query(TwilioWhatsappMessage)
        .filter(TwilioWhatsappMessage.peer_id == peer_id)
        .order_by(TwilioWhatsappMessage.created_at.desc())
        .limit(10)
        .all()
    )
    history: List[Dict[str, str]] = []
    for m in reversed(recent):
        if m.direction == "in":
            history.append({"role": "user", "content": m.content})
        else:
            history.append({"role": "assistant", "content": m.content})
    return history


@router.get("/api/twilio-whatsapp/config", summary="读取 Twilio WhatsApp 本地配置（脱敏）")
def get_twilio_whatsapp_config(
    request: Request, _: _ServerUser = Depends(get_current_user_media_edit)
):
    if not effective_account_sid() and not effective_auth_token() and _twilio_remote_upstream_base():
        return _remote_forward_twilio(request, "GET", "/api/twilio-whatsapp/config", None)
    f = _read_twilio_file()
    sid = (f.get("account_sid") or "").strip()
    path = _INBOUND_PATH
    suggested = _twilio_webhook_suggested()
    env_pub = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    return {
        "account_sid_masked": _mask_sid(sid),
        "has_account_sid": bool(sid),
        "has_auth_token": _mask_token_set(),
        "public_base_effective": env_pub,
        "webhook_suggested": suggested,
        "inbound_path": path,
        "env_fallback_note": "公网与 Webhook 由服务器 .env 决定；本页 JSON 仅保存 SID/Token",
        "twilio_kb_enterprise_id": f.get("twilio_kb_enterprise_id"),
        "twilio_kb_product_id": f.get("twilio_kb_product_id"),
        "knowledge_user_id": f.get("knowledge_user_id"),
        "poll_auto_enabled": _twilio_poll_auto_enabled(),
    }


@router.post("/api/twilio-whatsapp/config", summary="保存 Twilio WhatsApp 本地配置（写入包内 JSON，立即生效）")
def post_twilio_whatsapp_config(
    request: Request,
    body: TwilioWhatsappConfigUpdate,
    db: Session = Depends(get_db),
    current: _ServerUser = Depends(get_current_user_media_edit),
):
    f = _read_twilio_file()
    patch = body.model_dump(exclude_unset=True)
    if "account_sid" in patch:
        s = str(patch.get("account_sid") or "").strip()
        if s:
            f["account_sid"] = s
        else:
            f.pop("account_sid", None)
    if "auth_token" in patch:
        t = str(patch.get("auth_token") or "").strip()
        if t:
            f["auth_token"] = t
        else:
            f.pop("auth_token", None)
    if "twilio_kb_enterprise_id" in patch:
        eid = patch.get("twilio_kb_enterprise_id")
        if eid is None:
            f.pop("twilio_kb_enterprise_id", None)
        else:
            ent = db.query(TwilioKbEnterprise).filter(TwilioKbEnterprise.id == int(eid), TwilioKbEnterprise.user_id == current.id).first()
            if not ent:
                raise HTTPException(status_code=400, detail="WhatsApp 公司不存在或不属于当前账号")
            f["twilio_kb_enterprise_id"] = int(eid)
            f.pop("enterprise_id", None)
    if "twilio_kb_product_id" in patch:
        pid = patch.get("twilio_kb_product_id")
        if pid is None:
            f.pop("twilio_kb_product_id", None)
        else:
            prod = (
                db.query(TwilioKbProduct)
                .join(TwilioKbEnterprise, TwilioKbProduct.enterprise_id == TwilioKbEnterprise.id)
                .filter(TwilioKbProduct.id == int(pid), TwilioKbEnterprise.user_id == current.id)
                .first()
            )
            if not prod:
                raise HTTPException(status_code=400, detail="WhatsApp 产品不存在或不属于当前账号")
            f["twilio_kb_product_id"] = int(pid)
            f.pop("product_id", None)
    if "twilio_kb_enterprise_id" in patch or "twilio_kb_product_id" in patch:
        f["knowledge_user_id"] = current.id
    _write_twilio_file(f)
    logger.info("[Twilio WA] 本地配置已更新（路径=%s）", _TWILIO_CONFIG_PATH)
    rb = _twilio_remote_upstream_base()
    if rb:
        try:
            payload = body.model_dump(exclude_unset=True)
            if "twilio_kb_enterprise_id" in patch or "twilio_kb_product_id" in patch:
                payload["knowledge_user_id"] = f.get("knowledge_user_id")
            auth = (request.headers.get("Authorization") or "").strip()
            h: Dict[str, str] = {"Content-Type": "application/json"}
            if auth:
                h["Authorization"] = auth
            with _twilio_upstream_http_client(timeout=30.0) as client:
                r = client.post(
                    f"{rb}/api/twilio-whatsapp/config",
                    headers=h,
                    json=payload,
                )
            if r.status_code >= 400:
                logger.warning(
                    "[Twilio WA] 远程同步失败 HTTP %s %s", r.status_code, (r.text or "")[:400]
                )
        except Exception as e:
            logger.warning("[Twilio WA] 远程同步异常: %s", e)
    return {"ok": True, "message": "Twilio WhatsApp 配置已保存并生效"}


_MAX_TWILIO_KB_PRODUCTS_PER_ENTERPRISE = 2


@router.get("/api/twilio-whatsapp/knowledge/tree", summary="WhatsApp 专用公司与产品树（与企微资料独立）")
def twilio_kb_tree(
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    ents = (
        db.query(TwilioKbEnterprise)
        .filter(TwilioKbEnterprise.user_id == current.id)
        .order_by(TwilioKbEnterprise.id.asc())
        .all()
    )
    out: list[Dict[str, Any]] = []
    for e in ents:
        prods = (
            db.query(TwilioKbProduct)
            .filter(TwilioKbProduct.enterprise_id == e.id)
            .order_by(TwilioKbProduct.id.asc())
            .all()
        )
        out.append(
            {
                "id": e.id,
                "name": e.name,
                "company_info_preview": (e.company_info or "")[:300],
                "products": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "product_intro_preview": (p.product_intro or "")[:200],
                    }
                    for p in prods
                ],
            }
        )
    return {"items": out}


@router.post("/api/twilio-whatsapp/knowledge/enterprises", summary="新增 WhatsApp 公司资料")
def twilio_kb_enterprise_create(
    body: TwilioKbEnterpriseCreate,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    name = (body.name or "").strip() or "未命名"
    row = TwilioKbEnterprise(
        user_id=current.id,
        name=name,
        company_info=(body.company_info or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.put("/api/twilio-whatsapp/knowledge/enterprises/{ent_id:int}", summary="更新 WhatsApp 公司资料")
def twilio_kb_enterprise_update(
    ent_id: int,
    body: TwilioKbEnterpriseUpdate,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(TwilioKbEnterprise).filter(TwilioKbEnterprise.id == ent_id, TwilioKbEnterprise.user_id == current.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    if body.name is not None:
        row.name = (body.name or "").strip() or "未命名"
    if body.company_info is not None:
        row.company_info = (body.company_info or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/api/twilio-whatsapp/knowledge/enterprises/{ent_id:int}", summary="删除 WhatsApp 公司及其下属产品")
def twilio_kb_enterprise_delete(
    ent_id: int,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = db.query(TwilioKbEnterprise).filter(TwilioKbEnterprise.id == ent_id, TwilioKbEnterprise.user_id == current.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.query(TwilioKbProduct).filter(TwilioKbProduct.enterprise_id == ent_id).delete()
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.post("/api/twilio-whatsapp/knowledge/products", summary="新增 WhatsApp 产品资料")
def twilio_kb_product_create(
    body: TwilioKbProductCreate,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    ent = db.query(TwilioKbEnterprise).filter(TwilioKbEnterprise.id == body.enterprise_id, TwilioKbEnterprise.user_id == current.id).first()
    if not ent:
        raise HTTPException(status_code=404, detail="WhatsApp 公司不存在")
    n = db.query(TwilioKbProduct).filter(TwilioKbProduct.enterprise_id == body.enterprise_id).count()
    if n >= _MAX_TWILIO_KB_PRODUCTS_PER_ENTERPRISE:
        raise HTTPException(
            status_code=400,
            detail=f"每个 WhatsApp 公司最多 {_MAX_TWILIO_KB_PRODUCTS_PER_ENTERPRISE} 个产品",
        )
    row = TwilioKbProduct(
        enterprise_id=body.enterprise_id,
        name=(body.name or "").strip() or "未命名",
        product_intro=(body.product_intro or "").strip() or None,
        common_phrases=(body.common_phrases or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "enterprise_id": row.enterprise_id}


@router.put("/api/twilio-whatsapp/knowledge/products/{prod_id:int}", summary="更新 WhatsApp 产品资料")
def twilio_kb_product_update(
    prod_id: int,
    body: TwilioKbProductUpdate,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = (
        db.query(TwilioKbProduct)
        .join(TwilioKbEnterprise, TwilioKbProduct.enterprise_id == TwilioKbEnterprise.id)
        .filter(TwilioKbProduct.id == prod_id, TwilioKbEnterprise.user_id == current.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    if body.name is not None:
        row.name = (body.name or "").strip() or "未命名"
    if body.product_intro is not None:
        row.product_intro = (body.product_intro or "").strip() or None
    if body.common_phrases is not None:
        row.common_phrases = (body.common_phrases or "").strip() or None
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.delete("/api/twilio-whatsapp/knowledge/products/{prod_id:int}", summary="删除 WhatsApp 产品")
def twilio_kb_product_delete(
    prod_id: int,
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    row = (
        db.query(TwilioKbProduct)
        .join(TwilioKbEnterprise, TwilioKbProduct.enterprise_id == TwilioKbEnterprise.id)
        .filter(TwilioKbProduct.id == prod_id, TwilioKbEnterprise.user_id == current.id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="记录不存在")
    db.delete(row)
    db.commit()
    return {"ok": True}


TWILIO_KB_MATERIAL_TEMPLATE_HEADERS = ["企业名称", "公司介绍", "产品名称", "产品介绍", "常用话术"]


@router.get("/api/twilio-whatsapp/knowledge/material-template", summary="下载 WhatsApp 资料 CSV 模板（与企微表头一致）")
def twilio_kb_download_material_template(
    _: _ServerUser = Depends(get_current_user_media_edit),
):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TWILIO_KB_MATERIAL_TEMPLATE_HEADERS)
    writer.writerow(["示例 WhatsApp 公司", "本公司主营……", "产品A", "产品A介绍……", "您好，请问有什么可以帮您？"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=twilio_whatsapp_materials_template.csv"},
    )


@router.post("/api/twilio-whatsapp/knowledge/upload-materials", summary="上传 WhatsApp 资料 CSV，导入/更新公司与产品")
async def twilio_kb_upload_materials(
    file: UploadFile = File(...),
    current: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    """表头与企微相同：企业名称、公司介绍、产品名称、产品介绍、常用话术。多行表示多产品。"""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 CSV 文件")
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig").strip()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"文件解码失败: {e}") from e
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    header = [h.strip() for h in rows[0]]
    col = {h: i for i, h in enumerate(header)}
    for h in TWILIO_KB_MATERIAL_TEMPLATE_HEADERS:
        if h not in col:
            raise HTTPException(status_code=400, detail=f"缺少列: {h}")
    created_ent = 0
    created_prod = 0
    updated_ent = 0
    updated_prod = 0
    errors: list[str] = []
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
            ent = (
                db.query(TwilioKbEnterprise)
                .filter(TwilioKbEnterprise.user_id == current.id, TwilioKbEnterprise.name == ent_name)
                .first()
            )
            if not ent:
                ent = TwilioKbEnterprise(user_id=current.id, name=ent_name, company_info=company_info or None)
                db.add(ent)
                db.commit()
                db.refresh(ent)
                created_ent += 1
            else:
                if company_info:
                    ent.company_info = company_info
                    db.commit()
                    updated_ent += 1
            prods = db.query(TwilioKbProduct).filter(TwilioKbProduct.enterprise_id == ent.id, TwilioKbProduct.name == prod_name).all()
            if not prods:
                count = db.query(TwilioKbProduct).filter(TwilioKbProduct.enterprise_id == ent.id).count()
                if count >= _MAX_TWILIO_KB_PRODUCTS_PER_ENTERPRISE:
                    errors.append(f"第 {idx} 行：公司「{ent_name}」已有 {_MAX_TWILIO_KB_PRODUCTS_PER_ENTERPRISE} 个产品，无法再添加")
                    continue
                prod = TwilioKbProduct(
                    enterprise_id=ent.id,
                    name=prod_name,
                    product_intro=product_intro or None,
                    common_phrases=common_phrases or None,
                )
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


@router.post("/api/twilio-whatsapp/test-send", summary="Twilio 出站 WhatsApp 测试（需已保存 SID + Token）")
def twilio_whatsapp_test_send(
    request: Request,
    body: TwilioTestSendBody,
    _: _ServerUser = Depends(get_current_user_media_edit),
):
    sid = effective_account_sid()
    token = effective_auth_token()
    if (not sid or not token) and _twilio_remote_upstream_base():
        return _remote_forward_twilio(
            request, "POST", "/api/twilio-whatsapp/test-send", body.model_dump()
        )
    if not sid or not token:
        raise HTTPException(
            status_code=400,
            detail="请先在本页保存 Account SID 与 Auth Token，或使用环境变量配置后再试",
        )
    to = body.to.strip()
    from_w = body.from_whatsapp.strip()
    text = (body.body or "Lobster 测试").strip()
    if not to.startswith("whatsapp:") or not from_w.startswith("whatsapp:"):
        raise HTTPException(
            status_code=400,
            detail="From / To 须为 whatsapp:+E164 格式",
        )
    try:
        from twilio.rest import Client

        client = Client(sid, token)
        msg = client.messages.create(from_=from_w, to=to, body=text)
        out_sid = (getattr(msg, "sid", "") or "") if msg is not None else ""
        return {"ok": True, "message_sid": out_sid}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("[Twilio WA] test-send 失败: %s", e)
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/api/twilio-whatsapp/sessions", summary="会话列表（按对方 WhatsApp 号码，含最后一条预览）")
def list_twilio_whatsapp_sessions(
    _: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(TwilioWhatsappMessage)
        .order_by(TwilioWhatsappMessage.created_at.desc())
        .limit(2000)
        .all()
    )
    seen: set[str] = set()
    items: list[Dict[str, Any]] = []
    for m in rows:
        pid = (m.peer_id or "").strip()
        if not pid or pid in seen:
            continue
        seen.add(pid)
        pv = _twilio_body_display(m.content, m.msg_type)
        items.append(
            {
                "peer_id": pid,
                "last_at": m.created_at.isoformat() if m.created_at else None,
                "last_preview": pv[:60] + ("…" if len(pv) > 60 else ""),
            }
        )
    return {"items": items}


@router.get("/api/twilio-whatsapp/messages", summary="与某一号码的聊天记录")
def list_twilio_whatsapp_messages(
    peer_id: str,
    limit: int = 100,
    _: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    pid = (peer_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="peer_id 必填")
    rows = (
        db.query(TwilioWhatsappMessage)
        .filter(TwilioWhatsappMessage.peer_id == pid)
        .order_by(TwilioWhatsappMessage.created_at.asc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "direction": r.direction,
                "content": r.content,
                "msg_type": r.msg_type,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "peer_id": r.peer_id,
            }
            for r in rows
        ],
    }


async def _do_twilio_poll_and_reply(db: Session) -> Dict[str, Any]:
    """从云端拉取 pending → AI 客服回复 → submit-reply（与企微 _do_poll_and_reply 同构）。"""
    base = _twilio_remote_upstream_base()
    if not base:
        return {"processed": 0, "errors": ["未配置 Twilio 云端地址（twilio_remote_api_base）"]}
    secret = _get_wecom_forward_secret()
    headers: Dict[str, str] = {}
    if secret:
        headers["X-Forward-Secret"] = secret
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.get(f"{base}/api/twilio-whatsapp/pending", params={"limit": 20}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        logger.exception("[Twilio WA] 拉取 pending 失败: %s", e)
        return {"processed": 0, "errors": [str(e)]}
    items = data.get("items") or []
    processed = 0
    errors: list[str] = []
    for it in items:
        msg_id = it.get("id")
        frm = (it.get("from_user") or "").strip()
        to_w = (it.get("to_user") or "").strip()
        sid = (it.get("twilio_message_sid") or "").strip()
        content = (it.get("content") or "").strip()
        msg_type = (it.get("msg_type") or "text").strip() or "text"
        if not msg_id or not frm:
            continue
        try:
            db.add(
                TwilioWhatsappMessage(
                    peer_id=frm,
                    direction="in",
                    content=content if content else "[无文本正文]",
                    msg_type=msg_type,
                    twilio_message_sid=sid or None,
                    to_user=to_w or None,
                )
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            logger.info("[Twilio WA] 入站 sid=%s 已入库，继续生成回复并提交", sid or msg_id)
        company_info, product_intro, common_phrases, _kuid = _twilio_knowledge_materials(db)
        history = _twilio_history_for_peer(db, frm)
        if content:
            reply_text = await get_customer_service_reply(
                content,
                company_info=company_info,
                product_intro=product_intro,
                common_phrases=common_phrases,
                history=history,
            )
        else:
            reply_text = "请发送文字消息；当前自动回复仅处理文本。"
        delay_s = random.uniform(2.0, 10.0)
        logger.info(
            "[Twilio WA] 模拟真人回复延时 %.1fs 后提交 message_id=%s",
            delay_s,
            msg_id,
        )
        await asyncio.sleep(delay_s)
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                r2 = await client.post(
                    f"{base}/api/twilio-whatsapp/submit-reply",
                    json={"message_id": msg_id, "reply_text": reply_text},
                    headers=headers,
                )
                r2.raise_for_status()
        except Exception as e:
            logger.exception("[Twilio WA] 提交回复失败 message_id=%s: %s", msg_id, e)
            errors.append(f"message_id={msg_id} 提交失败: {e}")
            continue
        db.add(
            TwilioWhatsappMessage(
                peer_id=frm,
                direction="out",
                content=reply_text,
                msg_type="text",
                twilio_message_sid=None,
                to_user=to_w or None,
            )
        )
        db.commit()
        processed += 1
    return {"processed": processed, "errors": errors}


@router.post("/api/twilio-whatsapp/poll-and-reply", summary="手动拉取云端 WhatsApp 待处理并 AI 回复一次")
async def twilio_whatsapp_poll_and_reply(
    _: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    base = _twilio_remote_upstream_base()
    if not base:
        raise HTTPException(
            status_code=400,
            detail="未配置 Twilio 云端地址（twilio_remote_api_base）或已关闭转发。",
        )
    return await _do_twilio_poll_and_reply(db)


@router.get("/api/twilio-whatsapp/poll-auto", summary="是否启用后台定时拉取并自动回复（本地）")
def twilio_whatsapp_poll_auto_get(_: _ServerUser = Depends(get_current_user_media_edit)):
    return {"enabled": _twilio_poll_auto_enabled()}


@router.post("/api/twilio-whatsapp/poll-auto", summary="开启/关闭后台定时拉取并自动回复（写入本地 twilio_whatsapp_config.json）")
def twilio_whatsapp_poll_auto_set(
    body: TwilioPollAutoBody,
    _: _ServerUser = Depends(get_current_user_media_edit),
):
    f = _read_twilio_file()
    f["poll_auto_enabled"] = bool(body.enabled)
    _write_twilio_file(f)
    logger.info("[Twilio WA] poll_auto_enabled=%s（后台定时拉取）", body.enabled)
    return {"ok": True, "enabled": body.enabled}


_twilio_poll_consecutive_errors = 0

async def twilio_whatsapp_poll_loop():
    """后台拉取云端 WhatsApp 待处理并 AI 回复。未配置或已暂停时 10s 一检；连续失败时指数退避到 60s。"""
    global _twilio_poll_consecutive_errors
    while True:
        if not _twilio_remote_upstream_base() or not _twilio_poll_auto_enabled():
            await asyncio.sleep(10)
            continue
        backoff = min(2 * (2 ** _twilio_poll_consecutive_errors), 60)
        await asyncio.sleep(backoff)
        db = SessionLocal()
        try:
            await _do_twilio_poll_and_reply(db)
            _twilio_poll_consecutive_errors = 0
        except Exception as e:
            _twilio_poll_consecutive_errors += 1
            if _twilio_poll_consecutive_errors <= 3 or _twilio_poll_consecutive_errors % 30 == 0:
                logger.error("[Twilio WA] 拉取回复失败 (连续第%d次): %s", _twilio_poll_consecutive_errors, e)
        finally:
            db.close()


@router.post(_INBOUND_PATH, summary="Twilio WhatsApp 入站（Sandbox/正式号）")
async def twilio_whatsapp_inbound(request: Request):
    token = effective_auth_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="未配置 Auth Token（本页保存或环境变量 TWILIO_AUTH_TOKEN），拒绝处理入站",
        )
    try:
        form = await request.form()
    except Exception as e:
        logger.warning("[Twilio WA] 解析表单失败: %s", e)
        raise HTTPException(status_code=400, detail="invalid form body") from e

    params = _form_to_str_dict(form)
    sig = (request.headers.get("X-Twilio-Signature") or "").strip()
    if not sig:
        raise HTTPException(status_code=403, detail="missing X-Twilio-Signature")

    from twilio.request_validator import RequestValidator

    url = effective_signature_url(request)
    if not RequestValidator(token).validate(url, params, sig):
        logger.warning("[Twilio WA] 签名校验失败 url=%s", url)
        raise HTTPException(status_code=403, detail="invalid Twilio signature")

    frm = params.get("From", "")
    to = params.get("To", "")
    body = (params.get("Body") or "").strip()
    num_media = params.get("NumMedia", "0")

    logger.info(
        "[Twilio WA] inbound From=%s To=%s NumMedia=%s Body=%s",
        frm,
        to,
        num_media,
        body[:200] if body else "",
    )

    return Response(content=_EMPTY_TWIML, media_type="application/xml; charset=utf-8")

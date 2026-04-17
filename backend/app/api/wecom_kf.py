"""微信客服：客服账号管理、消息拉取与 AI 自动回复、客户列表、手动发消息。

通过云端 lobster-server 代理调用企微客服 API；消息采用 sync_msg 增量拉取。
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import get_current_user_media_edit
from .chat import get_customer_service_reply
from ..core.config import settings
from ..db import get_db
from ..models import (
    Enterprise,
    KfAccount,
    KfCustomer,
    KfMessage,
    Product,
    WecomConfig,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 复用 wecom.py 的云端代理辅助函数 ──────────────────────────────────────────

def _get_wecom_cloud_url() -> str:
    try:
        from .wecom import _get_wecom_cloud_url as _orig
        return _orig()
    except Exception:
        return ""


def _get_wecom_forward_secret() -> str:
    try:
        from .wecom import _get_wecom_forward_secret as _orig
        return _orig()
    except Exception:
        return ""


def _get_server_base_url() -> str:
    try:
        from .wecom import _get_server_base_url as _orig
        return _orig()
    except Exception:
        return ""


async def _cloud_post(path: str, body: dict) -> dict:
    base = _get_server_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="未配置服务器地址")
    secret = _get_wecom_forward_secret()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if secret:
        headers["X-Forward-Secret"] = secret
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.post(f"{base}{path}", json=body, headers=headers)
        if r.status_code != 200:
            try:
                d = r.json()
                raise HTTPException(status_code=r.status_code, detail=d.get("detail", r.text))
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=r.status_code, detail=r.text[:500])
        return r.json()


async def _cloud_get(path: str, params: dict = None) -> dict:
    base = _get_server_base_url()
    if not base:
        raise HTTPException(status_code=400, detail="未配置服务器地址")
    secret = _get_wecom_forward_secret()
    headers: dict[str, str] = {}
    if secret:
        headers["X-Forward-Secret"] = secret
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        r = await client.get(f"{base}{path}", params=params or {}, headers=headers)
        if r.status_code != 200:
            try:
                d = r.json()
                raise HTTPException(status_code=r.status_code, detail=d.get("detail", r.text))
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=r.status_code, detail=r.text[:500])
        return r.json()


def _get_callback_path(db: Session, config_id: int, user_id: int) -> str:
    cfg = db.query(WecomConfig).filter(WecomConfig.id == config_id, WecomConfig.user_id == user_id).first()
    if not cfg:
        raise HTTPException(status_code=404, detail="企微应用配置不存在")
    return cfg.callback_path


# ═══════════════════════════════════════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════════════════════════════════════


# ── 客服账号管理 ───────────────────────────────────────────────────────────────

class CreateKfAccountBody(BaseModel):
    config_id: int
    name: str = "AI客服"


@router.post("/api/wecom/kf/account/create", summary="创建客服账号")
async def kf_account_create(
    body: CreateKfAccountBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    cb_path = _get_callback_path(db, body.config_id, current_user.id)
    data = await _cloud_post("/api/wecom/proxy/kf/account/add", {"callback_path": cb_path, "name": body.name})
    open_kfid = data.get("open_kfid", "")
    if not open_kfid:
        raise HTTPException(status_code=502, detail="企微未返回 open_kfid")

    url_data = await _cloud_get("/api/wecom/proxy/kf/account/url", {"callback_path": cb_path, "open_kfid": open_kfid})
    kf_url = url_data.get("url", "")

    kf = KfAccount(
        wecom_config_id=body.config_id,
        user_id=current_user.id,
        open_kfid=open_kfid,
        name=body.name,
        url=kf_url,
    )
    db.add(kf)
    db.commit()
    db.refresh(kf)
    return {"ok": True, "kf_account": _kf_to_dict(kf)}


@router.get("/api/wecom/kf/accounts", summary="客服账号列表")
async def kf_account_list(
    config_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    accounts = (
        db.query(KfAccount)
        .filter(KfAccount.wecom_config_id == config_id, KfAccount.user_id == current_user.id)
        .order_by(KfAccount.id)
        .all()
    )
    return {"accounts": [_kf_to_dict(a) for a in accounts]}


class SyncKfAccountsBody(BaseModel):
    config_id: int


@router.post("/api/wecom/kf/account/sync", summary="从企微同步客服账号列表")
async def kf_account_sync(
    body: SyncKfAccountsBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    """从企微拉取客服账号列表并同步到本地数据库。"""
    cb_path = _get_callback_path(db, body.config_id, current_user.id)
    data = await _cloud_get("/api/wecom/proxy/kf/account/list", {"callback_path": cb_path})
    remote_accounts = data.get("account_list", [])
    synced = 0
    for item in remote_accounts:
        open_kfid = item.get("open_kfid", "")
        if not open_kfid:
            continue
        existing = db.query(KfAccount).filter(
            KfAccount.wecom_config_id == body.config_id,
            KfAccount.open_kfid == open_kfid,
        ).first()
        if existing:
            existing.name = item.get("name", existing.name)
            if not existing.url:
                try:
                    url_data = await _cloud_get("/api/wecom/proxy/kf/account/url", {"callback_path": cb_path, "open_kfid": open_kfid})
                    existing.url = url_data.get("url", "")
                except Exception:
                    pass
        else:
            kf_url = ""
            try:
                url_data = await _cloud_get("/api/wecom/proxy/kf/account/url", {"callback_path": cb_path, "open_kfid": open_kfid})
                kf_url = url_data.get("url", "")
            except Exception:
                pass
            db.add(KfAccount(
                wecom_config_id=body.config_id,
                user_id=current_user.id,
                open_kfid=open_kfid,
                name=item.get("name", "客服"),
                url=kf_url,
            ))
            synced += 1
    db.commit()
    accounts = (
        db.query(KfAccount)
        .filter(KfAccount.wecom_config_id == body.config_id, KfAccount.user_id == current_user.id)
        .order_by(KfAccount.id)
        .all()
    )
    return {"ok": True, "synced": synced, "accounts": [_kf_to_dict(a) for a in accounts]}


class ToggleKfAutoReplyBody(BaseModel):
    kf_account_id: int
    enabled: bool


@router.post("/api/wecom/kf/account/auto-reply", summary="切换客服账号自动回复")
async def kf_toggle_auto_reply(
    body: ToggleKfAutoReplyBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == body.kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    kf.auto_reply_enabled = body.enabled
    db.commit()
    return {"ok": True, "auto_reply_enabled": kf.auto_reply_enabled}


class DeleteKfAccountBody(BaseModel):
    kf_account_id: int
    delete_remote: bool = False


@router.post("/api/wecom/kf/account/delete", summary="删除客服账号")
async def kf_account_delete(
    body: DeleteKfAccountBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == body.kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    if body.delete_remote:
        try:
            cb_path = _get_callback_path(db, kf.wecom_config_id, current_user.id)
            await _cloud_post("/api/wecom/proxy/kf/account/del", {"callback_path": cb_path, "open_kfid": kf.open_kfid})
        except Exception as e:
            logger.warning("[KF] 远程删除客服账号失败: %s", e)
    db.delete(kf)
    db.commit()
    return {"ok": True}


# ── 客户列表 ───────────────────────────────────────────────────────────────────

@router.get("/api/wecom/kf/customers", summary="客服客户列表")
async def kf_customer_list(
    kf_account_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    customers = (
        db.query(KfCustomer)
        .filter(KfCustomer.kf_account_id == kf_account_id)
        .order_by(KfCustomer.last_msg_time.desc().nullslast(), KfCustomer.id.desc())
        .limit(200)
        .all()
    )
    return {"customers": [_customer_to_dict(c) for c in customers]}


# ── 消息记录 ───────────────────────────────────────────────────────────────────

@router.get("/api/wecom/kf/messages", summary="客服消息记录")
async def kf_message_list(
    kf_account_id: int,
    external_userid: str = "",
    limit: int = 50,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    q = db.query(KfMessage).filter(KfMessage.kf_account_id == kf_account_id)
    if external_userid:
        q = q.filter(KfMessage.external_userid == external_userid.strip())
    messages = q.order_by(KfMessage.send_time.desc().nullslast(), KfMessage.id.desc()).limit(limit).all()
    return {"messages": [_msg_to_dict(m) for m in reversed(messages)]}


# ── 手动发消息 ─────────────────────────────────────────────────────────────────

class KfSendBody(BaseModel):
    kf_account_id: int
    external_userid: str
    content: str
    msgtype: str = "text"


@router.post("/api/wecom/kf/send", summary="手动发送客服消息")
async def kf_send_message(
    body: KfSendBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == body.kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    cb_path = _get_callback_path(db, kf.wecom_config_id, current_user.id)
    data = await _cloud_post("/api/wecom/proxy/kf/send_msg", {
        "callback_path": cb_path,
        "touser": body.external_userid.strip(),
        "open_kfid": kf.open_kfid,
        "msgtype": body.msgtype,
        "content": body.content,
    })
    db.add(KfMessage(
        kf_account_id=kf.id,
        external_userid=body.external_userid.strip(),
        msgid=data.get("msgid"),
        direction="out",
        content=body.content,
        msg_type=body.msgtype,
        origin=5,
        send_time=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"ok": True, "msgid": data.get("msgid")}


# ── 手动拉取消息（前端触发）─────────────────────────────────────────────────

class KfPullBody(BaseModel):
    kf_account_id: int


@router.post("/api/wecom/kf/pull", summary="手动拉取客服消息")
async def kf_pull_messages(
    body: KfPullBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == body.kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    result = await _pull_and_reply_kf(kf, db)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# 后台轮询：拉取 KF 消息 + AI 自动回复
# ═══════════════════════════════════════════════════════════════════════════════

async def kf_poll_loop():
    """后台每 3 秒拉取所有启用了自动回复的 KF 账号消息。"""
    from ..db import SessionLocal

    while True:
        await asyncio.sleep(3)
        if not _get_server_base_url():
            continue
        db = SessionLocal()
        try:
            kf_accounts = db.query(KfAccount).filter(KfAccount.auto_reply_enabled == True).all()
            for kf in kf_accounts:
                try:
                    await _pull_and_reply_kf(kf, db)
                except Exception as e:
                    logger.exception("[KF] poll kf_id=%s: %s", kf.id, e)
        except Exception as e:
            logger.exception("[KF] poll loop: %s", e)
        finally:
            db.close()


async def _pull_and_reply_kf(kf: KfAccount, db: Session) -> Dict[str, Any]:
    """对一个 KF 账号执行一次 sync_msg + AI 回复。"""
    cb_path_row = db.query(WecomConfig).filter(WecomConfig.id == kf.wecom_config_id).first()
    if not cb_path_row:
        return {"processed": 0, "errors": ["配置不存在"]}
    cb_path = cb_path_row.callback_path

    company_info = ""
    product_intro = ""
    common_phrases = ""
    if cb_path_row.enterprise_id:
        ent = db.query(Enterprise).filter(Enterprise.id == cb_path_row.enterprise_id).first()
        if ent:
            company_info = ent.company_info or ""
    if cb_path_row.product_id:
        prod = db.query(Product).filter(Product.id == cb_path_row.product_id).first()
        if prod:
            product_intro = prod.product_intro or ""
            common_phrases = prod.common_phrases or ""

    try:
        sync_body: dict[str, Any] = {
            "callback_path": cb_path,
            "open_kfid": kf.open_kfid,
            "limit": 200,
        }
        if kf.sync_cursor:
            sync_body["cursor"] = kf.sync_cursor
        data = await _cloud_post("/api/wecom/proxy/kf/sync_msg", sync_body)
    except Exception as e:
        logger.warning("[KF] sync_msg failed kf_id=%s: %s", kf.id, e)
        return {"processed": 0, "errors": [str(e)]}

    next_cursor = data.get("next_cursor", "")
    if next_cursor:
        kf.sync_cursor = next_cursor
        db.commit()

    msg_list = data.get("msg_list") or []
    processed = 0
    errors: list[str] = []

    for msg in msg_list:
        origin = msg.get("origin", 0)
        msgtype = msg.get("msgtype", "")
        msgid = msg.get("msgid", "")
        external_userid = msg.get("external_userid", "")
        send_time_ts = msg.get("send_time", 0)
        send_time = datetime.fromtimestamp(send_time_ts, tz=timezone.utc) if send_time_ts else None

        if msgtype == "event":
            continue

        if msgid:
            existing = db.query(KfMessage).filter(KfMessage.msgid == msgid).first()
            if existing:
                continue

        content = _extract_kf_msg_content(msg)

        # origin=3 → 微信客户发的消息
        if origin == 3 and external_userid:
            customer = db.query(KfCustomer).filter(
                KfCustomer.kf_account_id == kf.id,
                KfCustomer.external_userid == external_userid,
            ).first()
            if not customer:
                customer = KfCustomer(kf_account_id=kf.id, external_userid=external_userid)
                db.add(customer)
                db.commit()
                db.refresh(customer)
            customer.last_msg_time = send_time or datetime.now(timezone.utc)

            db.add(KfMessage(
                kf_account_id=kf.id,
                external_userid=external_userid,
                msgid=msgid,
                direction="in",
                content=content,
                msg_type=msgtype,
                origin=origin,
                send_time=send_time,
            ))
            db.commit()

            if kf.auto_reply_enabled:
                history = _build_chat_history(db, kf.id, external_userid)
                reply_text = await get_customer_service_reply(
                    content,
                    company_info=company_info,
                    product_intro=product_intro,
                    common_phrases=common_phrases,
                    history=history,
                )
                delay_s = random.uniform(1.0, 5.0)
                await asyncio.sleep(delay_s)

                try:
                    send_data = await _cloud_post("/api/wecom/proxy/kf/send_msg", {
                        "callback_path": cb_path,
                        "touser": external_userid,
                        "open_kfid": kf.open_kfid,
                        "msgtype": "text",
                        "content": reply_text,
                    })
                    db.add(KfMessage(
                        kf_account_id=kf.id,
                        external_userid=external_userid,
                        msgid=send_data.get("msgid"),
                        direction="out",
                        content=reply_text,
                        msg_type="text",
                        origin=5,
                        send_time=datetime.now(timezone.utc),
                    ))
                    db.commit()
                    processed += 1
                except Exception as e:
                    logger.warning("[KF] send_msg failed kf_id=%s external=%s: %s", kf.id, external_userid, e)
                    errors.append(f"发送失败: {e}")

        elif origin == 5:
            db.add(KfMessage(
                kf_account_id=kf.id,
                external_userid=external_userid,
                msgid=msgid,
                direction="out",
                content=content,
                msg_type=msgtype,
                origin=origin,
                send_time=send_time,
            ))
            db.commit()

    has_more = data.get("has_more", 0)
    if has_more:
        try:
            more_result = await _pull_and_reply_kf(kf, db)
            processed += more_result.get("processed", 0)
            errors.extend(more_result.get("errors", []))
        except Exception as e:
            errors.append(f"递归拉取失败: {e}")

    return {"processed": processed, "errors": errors}


# ── 辅助函数 ───────────────────────────────────────────────────────────────────

def _extract_kf_msg_content(msg: dict) -> str:
    msgtype = msg.get("msgtype", "text")
    if msgtype == "text":
        return (msg.get("text", {}).get("content") or "").strip()
    elif msgtype == "image":
        return "[图片]"
    elif msgtype == "voice":
        return "[语音]"
    elif msgtype == "video":
        return "[视频]"
    elif msgtype == "file":
        return "[文件]"
    elif msgtype == "location":
        loc = msg.get("location", {})
        return f"[位置] {loc.get('name', '')} {loc.get('address', '')}"
    elif msgtype == "link":
        link = msg.get("link", {})
        return f"[链接] {link.get('title', '')} {link.get('url', '')}"
    elif msgtype == "business_card":
        return "[名片]"
    elif msgtype == "miniprogram":
        return f"[小程序] {msg.get('miniprogram', {}).get('title', '')}"
    elif msgtype == "msgmenu":
        return "[菜单消息]"
    return f"[{msgtype}]"


def _build_chat_history(db: Session, kf_account_id: int, external_userid: str) -> List[Dict[str, str]]:
    recent = (
        db.query(KfMessage)
        .filter(KfMessage.kf_account_id == kf_account_id, KfMessage.external_userid == external_userid)
        .order_by(KfMessage.send_time.desc().nullslast(), KfMessage.id.desc())
        .limit(10)
        .all()
    )
    history = []
    for m in reversed(recent):
        role = "user" if m.direction == "in" else "assistant"
        history.append({"role": role, "content": m.content})
    return history


def _kf_to_dict(kf: KfAccount) -> dict:
    return {
        "id": kf.id,
        "wecom_config_id": kf.wecom_config_id,
        "open_kfid": kf.open_kfid,
        "name": kf.name,
        "url": kf.url,
        "auto_reply_enabled": kf.auto_reply_enabled,
        "created_at": kf.created_at.isoformat() if kf.created_at else None,
    }


def _customer_to_dict(c: KfCustomer) -> dict:
    return {
        "id": c.id,
        "kf_account_id": c.kf_account_id,
        "external_userid": c.external_userid,
        "nickname": c.nickname,
        "avatar": c.avatar,
        "last_msg_time": c.last_msg_time.isoformat() if c.last_msg_time else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _msg_to_dict(m: KfMessage) -> dict:
    return {
        "id": m.id,
        "kf_account_id": m.kf_account_id,
        "external_userid": m.external_userid,
        "msgid": m.msgid,
        "direction": m.direction,
        "content": m.content,
        "msg_type": m.msg_type,
        "origin": m.origin,
        "send_time": m.send_time.isoformat() if m.send_time else None,
    }

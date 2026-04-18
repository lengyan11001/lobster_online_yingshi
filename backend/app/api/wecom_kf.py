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
    KfCustomerGroup,
    KfMessage,
    KfNotifyRule,
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
    group_id: Optional[int] = None,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    q = db.query(KfCustomer).filter(KfCustomer.kf_account_id == kf_account_id)
    if group_id is not None:
        q = q.filter(KfCustomer.group_id == group_id)
    customers = q.order_by(KfCustomer.last_msg_time.desc().nullslast(), KfCustomer.id.desc()).limit(200).all()
    groups = {g.id: g.name for g in db.query(KfCustomerGroup).filter(KfCustomerGroup.user_id == current_user.id).all()}
    return {"customers": [_customer_to_dict(c, groups.get(c.group_id, "")) for c in customers]}


class RefreshCustomerProfilesBody(BaseModel):
    kf_account_id: int


@router.post("/api/wecom/kf/customers/refresh", summary="刷新客户昵称头像")
async def kf_refresh_customer_profiles(
    body: RefreshCustomerProfilesBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    kf = db.query(KfAccount).filter(KfAccount.id == body.kf_account_id, KfAccount.user_id == current_user.id).first()
    if not kf:
        raise HTTPException(status_code=404, detail="客服账号不存在")
    cb_path = _get_callback_path(db, kf.wecom_config_id, current_user.id)
    customers = db.query(KfCustomer).filter(KfCustomer.kf_account_id == body.kf_account_id).all()
    if not customers:
        return {"ok": True, "updated": 0}
    ext_ids = [c.external_userid for c in customers]
    batch_size = 100
    updated = 0
    for i in range(0, len(ext_ids), batch_size):
        batch = ext_ids[i : i + batch_size]
        try:
            data = await _cloud_post("/api/wecom/proxy/kf/customer/batchget", {
                "callback_path": cb_path,
                "external_userid_list": batch,
            })
            for info in data.get("customer_list") or []:
                eid = info.get("external_userid", "")
                cust = next((c for c in customers if c.external_userid == eid), None)
                if cust:
                    cust.nickname = info.get("nickname") or cust.nickname
                    cust.avatar = info.get("avatar") or cust.avatar
                    updated += 1
        except Exception as e:
            logger.warning("[KF] batch refresh profiles failed: %s", e)
    db.commit()
    return {"ok": True, "updated": updated, "customers": [_customer_to_dict(c) for c in customers]}


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
    content: str = ""
    msgtype: str = "text"
    media_id: str = ""


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
    payload: dict = {
        "callback_path": cb_path,
        "touser": body.external_userid.strip(),
        "open_kfid": kf.open_kfid,
        "msgtype": body.msgtype,
        "content": body.content,
    }
    if body.media_id:
        payload["media_id"] = body.media_id
    data = await _cloud_post("/api/wecom/proxy/kf/send_msg", payload)
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

_KF_FAST_CHECK = 2              # 秒，检查 server 事件标记的频率（内部请求）
_KF_FALLBACK_POLL = 60          # 秒，无事件时兜底强制拉取一次
_KF_ACCOUNT_GAP = 1             # 秒，同一轮中不同账号之间的间隔
_KF_BACKOFF_MAX = 120           # 秒，限流时最大退避
_kf_poll_backoff = 0            # 当前退避秒数（动态）
_wecom_master_enabled = True    # 总开关：关闭后停止所有轮询和主动 API 调用


@router.get("/api/wecom/master-switch", summary="获取企微总开关状态")
async def get_master_switch():
    return {"enabled": _wecom_master_enabled}


class MasterSwitchBody(BaseModel):
    enabled: bool


@router.post("/api/wecom/master-switch", summary="设置企微总开关")
async def set_master_switch(body: MasterSwitchBody):
    global _wecom_master_enabled
    _wecom_master_enabled = body.enabled
    logger.info("[WeChat] master switch set to %s", body.enabled)
    return {"ok": True, "enabled": _wecom_master_enabled}


async def _has_kf_events() -> bool:
    """向 server 查询是否有新 KF 消息事件标记（内部请求，无频率限制）。"""
    try:
        data = await _cloud_get("/api/wecom/proxy/kf/has-events")
        return bool(data.get("has_events"))
    except Exception as e:
        logger.debug("[KF] has-events check failed: %s", e)
        return False

async def _ack_kf_events():
    """清除 server 上的 KF 事件标记。"""
    try:
        await _cloud_post("/api/wecom/proxy/kf/ack-events", {})
    except Exception:
        pass

async def kf_poll_loop():
    """后台定期拉取所有启用了自动回复的 KF 账号消息。

    策略：
      1. 每 2s 检查 server 事件标记（纯内部请求，无微信 API 调用）
      2. 有事件 → 立即 sync_msg（触发微信 API）
      3. 无事件 → 每 60s 兜底拉取一次
      4. 遇到 45009 限流 → 指数退避，事件也不能绕过退避
    """
    from ..db import SessionLocal
    global _kf_poll_backoff

    last_sync = 0.0

    while True:
        await asyncio.sleep(_KF_FAST_CHECK)
        if not _wecom_master_enabled:
            continue
        if not _get_server_base_url():
            continue

        now = asyncio.get_event_loop().time()
        elapsed = now - last_sync

        # 退避中：必须等退避时间过去
        if _kf_poll_backoff > 0 and elapsed < _kf_poll_backoff:
            continue

        has_event = await _has_kf_events()
        if has_event:
            await _ack_kf_events()
        elif elapsed < _KF_FALLBACK_POLL:
            continue

        last_sync = now
        db = SessionLocal()
        try:
            kf_accounts = db.query(KfAccount).filter(KfAccount.auto_reply_enabled == True).all()
            rate_limited = False
            for idx, kf in enumerate(kf_accounts):
                if idx > 0:
                    await asyncio.sleep(_KF_ACCOUNT_GAP)
                try:
                    await _pull_and_reply_kf(kf, db)
                except HTTPException as e:
                    if "45009" in str(e.detail) or "freq" in str(e.detail).lower():
                        rate_limited = True
                        logger.warning("[KF] rate limited kf_id=%s, backing off", kf.id)
                        break
                    logger.exception("[KF] poll kf_id=%s: %s", kf.id, e)
                except Exception as e:
                    if "45009" in str(e) or "freq" in str(e).lower():
                        rate_limited = True
                        logger.warning("[KF] rate limited kf_id=%s, backing off", kf.id)
                        break
                    logger.exception("[KF] poll kf_id=%s: %s", kf.id, e)
            if rate_limited:
                _kf_poll_backoff = min(max(_kf_poll_backoff * 2, 10), _KF_BACKOFF_MAX)
                logger.info("[KF] backoff increased to %ds", _kf_poll_backoff)
            else:
                _kf_poll_backoff = 0
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
            is_new_customer = False
            if not customer:
                customer = KfCustomer(kf_account_id=kf.id, external_userid=external_userid)
                db.add(customer)
                db.commit()
                db.refresh(customer)
                is_new_customer = True
            customer.last_msg_time = send_time or datetime.now(timezone.utc)

            if is_new_customer or not customer.nickname:
                try:
                    await _fetch_customer_profile(customer, cb_path, db)
                except Exception as e:
                    logger.warning("[KF] fetch customer profile failed: %s", e)

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

            # 关键词通知
            if content and msgtype == "text":
                await _check_keyword_notify(
                    db, kf, external_userid, content,
                    customer.nickname if customer else external_userid, cb_path
                )

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


async def _fetch_customer_profile(customer: KfCustomer, cb_path: str, db: Session) -> None:
    """通过企微 kf/customer/batchget 获取客户昵称和头像。"""
    data = await _cloud_post("/api/wecom/proxy/kf/customer/batchget", {
        "callback_path": cb_path,
        "external_userid_list": [customer.external_userid],
    })
    customer_list = data.get("customer_list") or []
    if customer_list:
        info = customer_list[0]
        customer.nickname = info.get("nickname") or customer.nickname
        customer.avatar = info.get("avatar") or customer.avatar
        db.commit()
        logger.info("[KF] fetched profile for %s: nickname=%s", customer.external_userid, customer.nickname)


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


def _customer_to_dict(c: KfCustomer, group_name: str = "") -> dict:
    return {
        "id": c.id,
        "kf_account_id": c.kf_account_id,
        "external_userid": c.external_userid,
        "nickname": c.nickname,
        "avatar": c.avatar,
        "group_id": c.group_id,
        "group_name": group_name,
        "last_msg_time": c.last_msg_time.isoformat() if c.last_msg_time else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _check_keyword_notify(
    db: Session, kf: KfAccount, external_userid: str,
    content: str, customer_name: str, cb_path: str,
):
    """检查消息是否匹配关键词通知规则，匹配则发企微应用消息通知指定人。"""
    user_id = kf.user_id
    rules = db.query(KfNotifyRule).filter(
        KfNotifyRule.user_id == user_id, KfNotifyRule.enabled == True
    ).all()
    if not rules:
        return
    content_lower = content.lower()
    for rule in rules:
        if rule.keyword.lower() in content_lower:
            msg_text = rule.message_template.format(
                customer=customer_name or external_userid,
                keyword=rule.keyword,
                content=content[:200],
            )
            try:
                await _cloud_post("/api/wecom/proxy/send-message", {
                    "callback_path": cb_path,
                    "to_user": rule.notify_userid,
                    "msg_type": "text",
                    "content": msg_text,
                })
                logger.info("[KF] keyword notify: rule=%s, customer=%s, notify_user=%s", rule.keyword, external_userid, rule.notify_userid)
            except Exception as e:
                logger.warning("[KF] keyword notify failed: %s", e)


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


# ── 客户分组 ─────────────────────────────────────────────────────────────────

class CreateGroupBody(BaseModel):
    name: str

class RenameGroupBody(BaseModel):
    group_id: int
    name: str

class AssignGroupBody(BaseModel):
    customer_ids: List[int]
    group_id: Optional[int] = None
    group_name: Optional[str] = None


@router.get("/api/wecom/kf/groups", summary="客户分组列表")
async def kf_group_list(
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    groups = db.query(KfCustomerGroup).filter(KfCustomerGroup.user_id == current_user.id).order_by(KfCustomerGroup.id).all()
    return {"groups": [{"id": g.id, "name": g.name, "count": db.query(func.count(KfCustomer.id)).filter(KfCustomer.group_id == g.id).scalar()} for g in groups]}


@router.post("/api/wecom/kf/groups", summary="创建客户分组")
async def kf_group_create(
    body: CreateGroupBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="分组名称不能为空")
    g = KfCustomerGroup(user_id=current_user.id, name=body.name.strip())
    db.add(g)
    db.commit()
    db.refresh(g)
    return {"ok": True, "group": {"id": g.id, "name": g.name, "count": 0}}


@router.put("/api/wecom/kf/groups", summary="重命名分组")
async def kf_group_rename(
    body: RenameGroupBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    g = db.query(KfCustomerGroup).filter(KfCustomerGroup.id == body.group_id, KfCustomerGroup.user_id == current_user.id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    g.name = body.name.strip() or g.name
    db.commit()
    return {"ok": True}


@router.delete("/api/wecom/kf/groups/{group_id}", summary="删除分组")
async def kf_group_delete(
    group_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    g = db.query(KfCustomerGroup).filter(KfCustomerGroup.id == group_id, KfCustomerGroup.user_id == current_user.id).first()
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    db.query(KfCustomer).filter(KfCustomer.group_id == group_id).update({"group_id": None})
    db.delete(g)
    db.commit()
    return {"ok": True}


@router.post("/api/wecom/kf/customers/assign-group", summary="批量设置客户分组")
async def kf_assign_group(
    body: AssignGroupBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    target_group_id = body.group_id
    if body.group_name is not None:
        name = body.group_name.strip()
        if not name:
            target_group_id = None
        else:
            g = db.query(KfCustomerGroup).filter(
                KfCustomerGroup.user_id == current_user.id, KfCustomerGroup.name == name
            ).first()
            if not g:
                g = KfCustomerGroup(user_id=current_user.id, name=name)
                db.add(g)
                db.commit()
                db.refresh(g)
            target_group_id = g.id
    elif body.group_id is not None:
        g = db.query(KfCustomerGroup).filter(KfCustomerGroup.id == body.group_id, KfCustomerGroup.user_id == current_user.id).first()
        if not g:
            raise HTTPException(status_code=404, detail="分组不存在")
    updated = 0
    for cid in body.customer_ids:
        c = db.query(KfCustomer).filter(KfCustomer.id == cid).first()
        if c:
            c.group_id = target_group_id
            updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


# ── 关键词通知规则 ─────────────────────────────────────────────────────────────

class CreateNotifyRuleBody(BaseModel):
    keyword: str
    notify_userid: str
    message_template: str = "客户 {customer} 发送了包含关键词「{keyword}」的消息：{content}"

class UpdateNotifyRuleBody(BaseModel):
    rule_id: int
    keyword: Optional[str] = None
    notify_userid: Optional[str] = None
    message_template: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/api/wecom/kf/notify-rules", summary="关键词通知规则列表")
async def kf_notify_rule_list(
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    rules = db.query(KfNotifyRule).filter(KfNotifyRule.user_id == current_user.id).order_by(KfNotifyRule.id).all()
    return {"rules": [{"id": r.id, "keyword": r.keyword, "notify_userid": r.notify_userid, "message_template": r.message_template, "enabled": r.enabled} for r in rules]}


@router.post("/api/wecom/kf/notify-rules", summary="创建关键词通知规则")
async def kf_notify_rule_create(
    body: CreateNotifyRuleBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    if not body.keyword.strip() or not body.notify_userid.strip():
        raise HTTPException(status_code=400, detail="关键词和通知人不能为空")
    rule = KfNotifyRule(
        user_id=current_user.id,
        keyword=body.keyword.strip(),
        notify_userid=body.notify_userid.strip(),
        message_template=body.message_template or "客户 {customer} 发送了包含关键词「{keyword}」的消息：{content}",
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"ok": True, "rule": {"id": rule.id, "keyword": rule.keyword, "notify_userid": rule.notify_userid, "message_template": rule.message_template, "enabled": rule.enabled}}


@router.put("/api/wecom/kf/notify-rules", summary="更新关键词通知规则")
async def kf_notify_rule_update(
    body: UpdateNotifyRuleBody,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    rule = db.query(KfNotifyRule).filter(KfNotifyRule.id == body.rule_id, KfNotifyRule.user_id == current_user.id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    if body.keyword is not None:
        rule.keyword = body.keyword.strip()
    if body.notify_userid is not None:
        rule.notify_userid = body.notify_userid.strip()
    if body.message_template is not None:
        rule.message_template = body.message_template
    if body.enabled is not None:
        rule.enabled = body.enabled
    db.commit()
    return {"ok": True}


@router.delete("/api/wecom/kf/notify-rules/{rule_id}", summary="删除关键词通知规则")
async def kf_notify_rule_delete(
    rule_id: int,
    current_user=Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    rule = db.query(KfNotifyRule).filter(KfNotifyRule.id == rule_id, KfNotifyRule.user_id == current_user.id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    db.commit()
    return {"ok": True}

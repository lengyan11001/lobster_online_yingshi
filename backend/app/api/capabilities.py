"""能力网关（在线客户端后端）。

速推类能力：用户算力预扣/结算/退款的 **唯一业务编排点** 在 **lobster_server** 的 MCP
``invoke_capability``（调用速推前在该处完成 pre-deduct 等）。本机 MCP 经 mcp-gateway 转发，**不经**本路由对速推做计费。

本文件 POST /capabilities/pre-deduct|record-call|refund **不做本地加减分**，仅原样转发认证中心（浏览器/旧客户端兼容）；
速推工具链请勿在本仓库另写第二套扣费。余额事实来源：认证中心（lobster-server）数据库。
"""
import os
from typing import Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user_for_chat, get_current_user_for_local, _ServerUser
from ..models import CapabilityCallLog, CapabilityConfig, User
from ..services.capability_cost_confirm import resolve_capability_confirm

router = APIRouter()


def _auth_server_base() -> str:
    base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="未配置 AUTH_SERVER_BASE")
    return base


def _proxy_headers(request: Request) -> dict:
    """转发 Authorization、X-Installation-Id 及 MCP 计费信任头到认证中心。"""
    token = request.headers.get("Authorization") or ""
    h = {"Authorization": token, "Content-Type": "application/json"}
    iid = (request.headers.get("X-Installation-Id") or "").strip()
    if iid:
        h["X-Installation-Id"] = iid
    bk = (getattr(settings, "lobster_mcp_billing_internal_key", None) or "").strip()
    if not bk:
        bk = (os.environ.get("LOBSTER_MCP_BILLING_INTERNAL_KEY") or "").strip()
    if bk:
        h["X-Lobster-Mcp-Billing"] = bk
    return h


@router.get("/capabilities/available", summary="当前可用能力列表（本地）")
def list_available(
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    rows = db.query(CapabilityConfig).filter(CapabilityConfig.enabled.is_(True)).order_by(CapabilityConfig.capability_id).all()
    return {
        "capabilities": [
            {
                "capability_id": r.capability_id,
                "description": r.description,
                "upstream": r.upstream,
                "upstream_tool": r.upstream_tool,
                "arg_schema": r.arg_schema,
                "is_default": r.is_default,
                "unit_credits": r.unit_credits,
            }
            for r in rows
        ]
    }


@router.get("/capabilities/registry", summary="能力注册列表（本地）")
def list_registry(
    _: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    rows = db.query(CapabilityConfig).order_by(CapabilityConfig.capability_id).all()
    return [
        {
            "capability_id": r.capability_id,
            "description": r.description,
            "upstream": r.upstream,
            "upstream_tool": r.upstream_tool,
            "enabled": r.enabled,
            "is_default": r.is_default,
            "unit_credits": r.unit_credits,
        }
        for r in rows
    ]


class ConfirmCapabilityInvokeIn(BaseModel):
    confirm_token: str = Field(..., min_length=8)
    accept: bool = False


@router.post("/capabilities/confirm-invoke", summary="确认或取消「预估扣费后」的能力调用")
async def confirm_capability_invoke(
    body: ConfirmCapabilityInvokeIn,
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
):
    """与 chat/stream 推送的 confirm_token 配对；accept 后继续执行 MCP。"""
    ok = resolve_capability_confirm(body.confirm_token.strip(), int(current_user.id), body.accept)
    if not ok:
        raise HTTPException(status_code=404, detail="确认已失效或 token 无效，请重新发起对话")
    return {"ok": True, "accepted": body.accept}


class RecordCallIn(BaseModel):
    capability_id: str
    success: bool = True
    latency_ms: Optional[int] = None
    request_payload: Optional[dict] = None
    response_payload: Optional[dict] = None
    error_message: Optional[str] = None
    source: str = "mcp_invoke"
    chat_session_id: Optional[str] = None
    chat_context_id: Optional[str] = None
    """若由 pre-deduct 已扣过，传本次扣费数，避免重复扣。"""
    credits_charged: Optional[float] = None
    # 与认证中心 RecordCallIn 对齐；缺省时 JSON 会被本路由解析后丢弃，认证中心会走 direct_charge/unit 再扣一遍。
    pre_deduct_applied: bool = False
    credits_pre_deducted: Optional[float] = None
    credits_final: Optional[float] = None
    sutui_pool: Optional[str] = None
    sutui_token_ref: Optional[str] = None


@router.post("/capabilities/pre-deduct", summary="预扣算力（代理到认证中心）")
async def pre_deduct(request: Request):
    """代理到认证中心预扣；一般由 MCP 调用。"""
    base = _auth_server_base()
    body = await request.body()
    from fastapi.responses import Response

    h = _proxy_headers(request)
    ct = (request.headers.get("content-type") or "").strip() or "application/json"
    h["Content-Type"] = ct
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{base}/capabilities/pre-deduct",
            content=body,
            headers=h,
        )
    mt = r.headers.get("content-type") or "application/json"
    return Response(content=r.content, status_code=r.status_code, media_type=mt)


class RefundIn(BaseModel):
    capability_id: str
    credits: float


@router.post("/capabilities/refund", summary="退还预扣算力（代理到认证中心）")
async def refund_credits(body: RefundIn, request: Request):
    base = _auth_server_base()
    h = _proxy_headers(request)
    h["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{base}/capabilities/refund",
            json=body.model_dump(),
            headers=h,
        )
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@router.post("/capabilities/record-call", summary="记录能力调用并扣算力（代理到认证中心）")
async def record_call(body: RecordCallIn, request: Request):
    base = _auth_server_base()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{base}/capabilities/record-call",
            json=body.model_dump(),
            headers=_proxy_headers(request),
        )
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


@router.get("/capabilities/my-call-logs", summary="我的能力调用记录（本地）")
def my_call_logs(
    capability_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    q = db.query(CapabilityCallLog).filter(CapabilityCallLog.user_id == current_user.id)
    if capability_id:
        q = q.filter(CapabilityCallLog.capability_id == capability_id)
    rows = q.order_by(CapabilityCallLog.created_at.desc()).offset(max(offset, 0)).limit(min(max(limit, 1), 200)).all()
    return [
        {
            "id": r.id,
            "capability_id": r.capability_id,
            "success": r.success,
            "credits_charged": r.credits_charged,
            "latency_ms": r.latency_ms,
            "request_payload": r.request_payload,
            "response_payload": r.response_payload,
            "error_message": r.error_message,
            "source": r.source,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]



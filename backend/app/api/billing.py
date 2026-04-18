"""软件收费模式配置与展示：技能解锁价格、算力套餐（算力兑换比例）；自有充值订单。"""
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user
from ..models import RechargeOrder, User

logger = logging.getLogger(__name__)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"

# 默认收费模式（可被 custom_configs.json 中 BILLING_PRICING 覆盖）
_DEFAULT_SKILL_UNLOCK = {"min_yuan": 98, "max_yuan": 198}
_DEFAULT_CREDIT_PACKAGES = [
    {"price_yuan": 198, "credits": 20000, "label": "198元 - 20000算力"},
    {"price_yuan": 498, "credits": 50000, "label": "498元 - 50000算力"},
    {"price_yuan": 998, "credits": 120000, "label": "998元 - 120000算力"},
]


def _get_billing_pricing() -> dict[str, Any]:
    """从 custom_configs.json 读取 BILLING_PRICING；缺失时返回默认。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return {
            "skill_unlock": _DEFAULT_SKILL_UNLOCK,
            "credit_packages": _DEFAULT_CREDIT_PACKAGES,
        }
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("BILLING_PRICING")
        if not isinstance(cfg, dict):
            return {
                "skill_unlock": _DEFAULT_SKILL_UNLOCK,
                "credit_packages": _DEFAULT_CREDIT_PACKAGES,
            }
        skill = cfg.get("skill_unlock")
        if isinstance(skill, dict):
            min_yuan = skill.get("min_yuan")
            max_yuan = skill.get("max_yuan")
            skill_unlock = {
                "min_yuan": int(min_yuan) if min_yuan is not None else _DEFAULT_SKILL_UNLOCK["min_yuan"],
                "max_yuan": int(max_yuan) if max_yuan is not None else _DEFAULT_SKILL_UNLOCK["max_yuan"],
            }
        else:
            skill_unlock = _DEFAULT_SKILL_UNLOCK

        packages = cfg.get("credit_packages")
        if isinstance(packages, list) and packages:
            out = []
            for p in packages:
                if not isinstance(p, dict):
                    continue
                price = p.get("price_yuan") or p.get("price")
                credits = p.get("credits")
                if price is not None and credits is not None:
                    label = (p.get("label") or "").strip() or f"{int(price)}元 - {int(credits)}算力"
                    out.append({
                        "price_yuan": int(price),
                        "credits": int(credits),
                        "label": label,
                    })
            if out:
                credit_packages = out
            else:
                credit_packages = _DEFAULT_CREDIT_PACKAGES
        else:
            credit_packages = _DEFAULT_CREDIT_PACKAGES

        return {"skill_unlock": skill_unlock, "credit_packages": credit_packages}
    except Exception as e:
        logger.debug("BILLING_PRICING read failed: %s", e)
        return {
            "skill_unlock": _DEFAULT_SKILL_UNLOCK,
            "credit_packages": _DEFAULT_CREDIT_PACKAGES,
        }


@router.get(
    "/api/billing/credit-history",
    summary="算力变动记录（转发认证中心；含 LLM sutui_chat 等）",
)
async def proxy_credit_history_from_auth_server(
    request: Request,
    limit: int = 100,
    offset: int = 0,
):
    """credit_ledger 仅在认证中心库；从局域网 IP 打开页面时 API_BASE 常与本机同源，须有本机路由转发到 AUTH_SERVER_BASE。"""
    base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="未配置 AUTH_SERVER_BASE，无法拉取认证中心算力流水",
        )
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="需要登录")
    fwd_headers: dict[str, str] = {"Authorization": auth}
    xi = (request.headers.get("X-Installation-Id") or "").strip()
    if xi:
        fwd_headers["X-Installation-Id"] = xi
    lim = min(max(int(limit), 1), 200)
    off = max(0, int(offset))
    url = f"{base}/api/billing/credit-history?limit={lim}&offset={off}"
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            r = await client.get(url, headers=fwd_headers)
    except httpx.RequestError as e:
        logger.warning("[billing-proxy] credit-history 认证中心不可达: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"认证中心不可达，无法拉取算力流水: {e!s}",
        ) from e
    if r.status_code != 200:
        detail = (r.text or "")[:800] if r.text else r.reason_phrase
        logger.info(
            "[billing-proxy] credit-history upstream http=%s detail=%s",
            r.status_code,
            detail[:200],
        )
        raise HTTPException(status_code=r.status_code, detail=detail or "认证中心返回错误")
    try:
        return r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="认证中心响应非 JSON")


@router.get("/api/billing/pricing", summary="软件收费模式（技能解锁价格 + 算力套餐）")
def get_billing_pricing(current_user: User = Depends(get_current_user)):
    """返回技能解锁价格区间与算力套餐列表，供前端展示。可在 custom_configs.json 的 configs.BILLING_PRICING 中覆盖。"""
    return _get_billing_pricing()


# ── 自有充值（独立于速推）────────────────────────────────────────────────────

def _use_independent_recharge() -> bool:
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    return edition == "online" and getattr(settings, "lobster_independent_auth", True)


@router.get("/api/recharge/packages", summary="充值套餐列表（自有）")
def get_recharge_packages(current_user: User = Depends(get_current_user)):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    pricing = _get_billing_pricing()
    return {"packages": pricing.get("credit_packages", _DEFAULT_CREDIT_PACKAGES)}


class RechargeCreateBody(BaseModel):
    package_index: Optional[int] = None
    price_yuan: Optional[int] = None
    credits: Optional[int] = None


@router.post("/api/recharge/create", summary="创建充值订单（自有）")
def create_recharge_order(
    body: RechargeCreateBody,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not _use_independent_recharge():
        raise HTTPException(status_code=400, detail="当前未启用自有充值")
    pricing = _get_billing_pricing()
    packages = pricing.get("credit_packages", _DEFAULT_CREDIT_PACKAGES)
    if body.package_index is not None:
        idx = int(body.package_index)
        if idx < 0 or idx >= len(packages):
            raise HTTPException(status_code=400, detail="无效套餐")
        p = packages[idx]
        amount_yuan = p["price_yuan"]
        credits = p["credits"]
    elif body.price_yuan is not None and body.credits is not None:
        amount_yuan = int(body.price_yuan)
        credits = int(body.credits)
        if amount_yuan <= 0 or credits <= 0:
            raise HTTPException(status_code=400, detail="金额与算力须为正数")
    else:
        raise HTTPException(status_code=400, detail="请选择套餐或指定 price_yuan + credits")
    out_trade_no = f"R{current_user.id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    order = RechargeOrder(
        user_id=current_user.id,
        amount_yuan=amount_yuan,
        credits=credits,
        status="pending",
        out_trade_no=out_trade_no,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    payment_hint = getattr(settings, "lobster_recharge_payment_hint", None) or "请通过微信/支付宝转账并联系管理员完成到账，备注订单号。"
    return {
        "order_id": order.id,
        "out_trade_no": order.out_trade_no,
        "amount_yuan": order.amount_yuan,
        "credits": order.credits,
        "status": order.status,
        "payment_info": payment_hint,
        "created_at": order.created_at.isoformat() if order.created_at else "",
    }


class RechargeCompleteBody(BaseModel):
    out_trade_no: Optional[str] = None
    order_id: Optional[int] = None


@router.post("/api/recharge/complete", summary="完成充值（管理员/回调：到账加算力）")
def complete_recharge(
    body: RechargeCompleteBody,
    x_admin_secret: Optional[str] = Header(None, alias="X-Admin-Secret"),
    db: Session = Depends(get_db),
):
    secret = (getattr(settings, "lobster_recharge_admin_secret", None) or "").strip()
    if not secret or (x_admin_secret or "").strip() != secret:
        raise HTTPException(status_code=403, detail="需要管理员密钥")
    if body.out_trade_no:
        order = db.query(RechargeOrder).filter(RechargeOrder.out_trade_no == body.out_trade_no.strip()).first()
    elif body.order_id is not None:
        order = db.query(RechargeOrder).filter(RechargeOrder.id == body.order_id).first()
    else:
        raise HTTPException(status_code=400, detail="请提供 out_trade_no 或 order_id")
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status == "paid":
        return {"ok": True, "message": "订单已支付过", "order_id": order.id}
    user = db.query(User).filter(User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=500, detail="用户不存在")
    user.credits = (user.credits or 0) + order.credits
    order.status = "paid"
    from datetime import datetime
    order.paid_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "message": f"已到账 {order.credits} 算力", "order_id": order.id}

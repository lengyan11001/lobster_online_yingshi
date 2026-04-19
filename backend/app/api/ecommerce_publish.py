"""电商商品发布 API：列出店铺账号、打开商品发布页面并自动填充。"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Asset, EcommerceDetailJob, PublishAccount
from .auth import _ServerUser, get_current_user_for_local
from .publish import BROWSER_DATA_DIR, browser_options_from_publish_meta

logger = logging.getLogger(__name__)
router = APIRouter()

ECOMMERCE_PLATFORMS = {
    "douyin_shop", "xiaohongshu_shop", "alibaba1688", "taobao", "pinduoduo"
}

ECOMMERCE_PLATFORM_NAMES = {
    "douyin_shop": "抖店",
    "xiaohongshu_shop": "小红书店铺",
    "alibaba1688": "1688",
    "taobao": "淘宝",
    "pinduoduo": "拼多多",
}

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_ASSETS_DIR = _BASE_DIR / "assets"


class OpenProductFormBody(BaseModel):
    platform: str = Field(..., description="电商平台 ID")
    account_nickname: Optional[str] = Field(None, description="店铺账号昵称，不传则用该平台第一个账号")
    title: Optional[str] = Field(None, description="商品标题")
    price: Optional[str] = Field(None, description="商品价格")
    category: Optional[str] = Field(None, description="商品类目")
    main_image_asset_ids: List[str] = Field(default_factory=list, description="主图素材 ID 列表")
    detail_image_asset_ids: List[str] = Field(default_factory=list, description="详情图素材 ID 列表")


def _resolve_asset_to_local_path(db: Session, user_id: int, asset_id: str) -> Optional[str]:
    """将 asset_id 解析为本地文件路径；无本地文件则下载 source_url 到临时文件。"""
    asset = (
        db.query(Asset)
        .filter(Asset.asset_id == asset_id.strip(), Asset.user_id == user_id)
        .first()
    )
    if not asset:
        logger.warning("[ecommerce_publish] asset not found: %s", asset_id)
        return None

    local = _ASSETS_DIR / asset.filename
    if local.exists():
        return str(local)

    url = (asset.source_url or "").strip()
    if not url.startswith(("http://", "https://")):
        logger.warning("[ecommerce_publish] asset %s has no local file and no source_url", asset_id)
        return None

    try:
        ext = Path(asset.filename or "").suffix or ".png"
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            r = client.get(url)
            r.raise_for_status()
        fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="ecom_img_")
        try:
            os.write(fd, r.content)
        finally:
            os.close(fd)
        logger.info("[ecommerce_publish] downloaded asset %s to %s", asset_id, tmp_path)
        return tmp_path
    except Exception as e:
        logger.warning("[ecommerce_publish] download asset %s failed: %s", asset_id, e)
        return None


def _resolve_asset_ids(db: Session, user_id: int, asset_ids: List[str]) -> List[str]:
    """批量解析 asset_id 列表为本地文件路径列表（跳过解析失败的）。"""
    paths = []
    for aid in asset_ids:
        p = _resolve_asset_to_local_path(db, user_id, aid)
        if p:
            paths.append(p)
    return paths


@router.get("/api/ecommerce-publish/platforms", summary="列出支持的电商平台")
def list_ecommerce_platforms():
    return {
        "platforms": [
            {"id": pid, "name": ECOMMERCE_PLATFORM_NAMES.get(pid, pid)}
            for pid in sorted(ECOMMERCE_PLATFORMS)
        ]
    }


@router.get("/api/ecommerce-publish/accounts", summary="列出电商店铺账号")
def list_shop_accounts(
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.user_id == current_user.id,
            PublishAccount.platform.in_(ECOMMERCE_PLATFORMS),
        )
        .order_by(PublishAccount.platform, PublishAccount.id)
        .all()
    )
    return {
        "accounts": [
            {
                "id": a.id,
                "platform": a.platform,
                "platform_name": ECOMMERCE_PLATFORM_NAMES.get(a.platform, a.platform),
                "nickname": a.nickname,
                "status": a.status,
                "last_login": a.last_login.isoformat() if a.last_login else None,
            }
            for a in rows
        ],
        "platforms": [
            {"id": pid, "name": ECOMMERCE_PLATFORM_NAMES.get(pid, pid)}
            for pid in sorted(ECOMMERCE_PLATFORMS)
        ],
    }


@router.post("/api/ecommerce-publish/open-product-form", summary="打开商品发布页面并自动填充（不提交）")
async def open_product_form(
    body: OpenProductFormBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    if body.platform not in ECOMMERCE_PLATFORMS:
        raise HTTPException(400, detail=f"不支持的电商平台: {body.platform}")

    query = db.query(PublishAccount).filter(
        PublishAccount.user_id == current_user.id,
        PublishAccount.platform == body.platform,
    )
    if body.account_nickname:
        acct = query.filter(PublishAccount.nickname == body.account_nickname.strip()).first()
    else:
        acct = query.first()

    if not acct:
        platform_name = ECOMMERCE_PLATFORM_NAMES.get(body.platform, body.platform)
        raise HTTPException(
            404,
            detail=f"未找到{platform_name}店铺账号，请先在技能商店「商品发布」中添加并登录",
        )

    profile_dir = acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}")

    main_image_paths = _resolve_asset_ids(db, current_user.id, body.main_image_asset_ids)
    detail_image_paths = _resolve_asset_ids(db, current_user.id, body.detail_image_asset_ids)

    temp_files: List[str] = []

    try:
        from publisher.browser_pool import (
            _acquire_context,
            _ensure_visible_interactive_context,
            _get_page_with_reacquire,
            _setup_auto_close,
        )
        from publisher.drivers import DRIVERS

        driver_cls = DRIVERS.get(body.platform)
        if not driver_cls:
            raise HTTPException(400, detail=f"不支持的电商平台驱动: {body.platform}")

        bopts = browser_options_from_publish_meta(acct.meta)
        await _ensure_visible_interactive_context(profile_dir, browser_options=bopts)
        ctx, created_new = await _acquire_context(
            profile_dir, new_headless=False, browser_options=bopts
        )

        page, ctx = await _get_page_with_reacquire(profile_dir, ctx, browser_options=bopts)

        driver = driver_cls()
        login_ok = await driver.check_login(page, navigate=True)
        if not login_ok:
            login_url = driver.login_url()
            try:
                await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            _setup_auto_close(ctx, profile_dir, page, browser_options=bopts)
            platform_name = ECOMMERCE_PLATFORM_NAMES.get(body.platform, body.platform)
            return {
                "ok": False,
                "need_login": True,
                "message": f"未登录{platform_name}，已打开登录页面，请扫码登录后重试",
                "platform": body.platform,
                "platform_name": platform_name,
                "account_nickname": acct.nickname,
            }

        result = await driver.open_product_form(
            page,
            title=body.title,
            price=body.price,
            category=body.category,
            main_image_paths=main_image_paths or None,
            detail_image_paths=detail_image_paths or None,
        )

        _setup_auto_close(ctx, profile_dir, page, browser_options=bopts)

        platform_name = ECOMMERCE_PLATFORM_NAMES.get(body.platform, body.platform)
        return {
            "ok": result.get("ok", False),
            "message": result.get("message", ""),
            "platform": body.platform,
            "platform_name": platform_name,
            "account_nickname": acct.nickname,
            "auto_filled": result.get("auto_filled", []),
            "url": result.get("url", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[ecommerce_publish] open_product_form failed platform=%s", body.platform)
        raise HTTPException(500, detail=f"打开商品发布页失败: {e}")
    finally:
        for tf in temp_files:
            try:
                os.unlink(tf)
            except Exception:
                pass


class PublishFromJobBody(BaseModel):
    job_id: str = Field(..., description="电商详情图 pipeline job_id")
    platform: str = Field("douyin_shop", description="电商平台 ID")
    account_nickname: Optional[str] = Field(None, description="店铺账号昵称")
    title: Optional[str] = Field(None, description="商品标题（不传则从 job 分析结果中提取）")


def _extract_asset_ids_from_suite(saved_assets: Dict[str, Any], category: str) -> List[str]:
    bundle = saved_assets.get("suite_bundle") if isinstance(saved_assets.get("suite_bundle"), dict) else {}
    items = bundle.get(category, [])
    if not isinstance(items, list):
        return []
    return [str(it.get("asset_id")) for it in items if isinstance(it, dict) and it.get("asset_id")]


@router.post("/api/ecommerce-publish/from-job", summary="从电商详情图 job 一键打开商品发布页")
async def publish_from_job(
    body: PublishFromJobBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    from ..services.comfly_ecommerce_detail_job_store import get_job as get_mem_job

    job_id = (body.job_id or "").strip().lower()
    if not job_id:
        raise HTTPException(400, detail="job_id 不能为空")

    saved_assets: Optional[Dict[str, Any]] = None
    product_name: Optional[str] = None
    listing_category: Optional[str] = None

    mem_job = get_mem_job(job_id)
    if mem_job and mem_job.get("status") == "completed":
        saved_assets = mem_job.get("saved_assets")
        result = mem_job.get("result") or {}
        analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
        product_name = str(analysis.get("product_name") or "").strip()
        listing_category = str(analysis.get("listing_category") or "").strip() or str((result.get("config") or {}).get("listing_category") or "").strip()

    if saved_assets is None:
        db_job = db.query(EcommerceDetailJob).filter(EcommerceDetailJob.job_id == job_id).first()
        if not db_job:
            raise HTTPException(404, detail=f"未找到 job_id={job_id}")
        if db_job.status != "completed":
            raise HTTPException(400, detail=f"Job 尚未完成（状态: {db_job.status}）")
        saved_assets = db_job.saved_assets or {}
        product_name = product_name or db_job.product_name or ""

    meta = saved_assets.get("meta") if isinstance(saved_assets, dict) and isinstance(saved_assets.get("meta"), dict) else {}
    listing_category = listing_category or str(meta.get("listing_category") or "").strip() or None

    main_ids = _extract_asset_ids_from_suite(saved_assets, "main_images")
    detail_ids = _extract_asset_ids_from_suite(saved_assets, "detail_images")
    if not detail_ids:
        pages = saved_assets.get("pages", [])
        if isinstance(pages, list):
            detail_ids = [str(p.get("asset_id")) for p in pages if isinstance(p, dict) and p.get("asset_id")]

    title = body.title or product_name or None

    form_body = OpenProductFormBody(
        platform=body.platform,
        account_nickname=body.account_nickname,
        title=title,
        category=listing_category,
        main_image_asset_ids=main_ids,
        detail_image_asset_ids=detail_ids,
    )

    return await open_product_form(form_body, current_user=current_user, db=db)

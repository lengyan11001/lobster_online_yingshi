"""Publishing accounts and task management."""
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import httpx

from publisher.browser_pool import browser_options_from_publish_meta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import OperationalError
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .auth import _ServerUser, get_current_user_for_local
from ..datetime_iso import isoformat_utc
from ..db import get_db
from ..models import (
    Asset,
    CreatorContentSnapshot,
    PublishAccount,
    PublishAccountCreatorSchedule,
    PublishTask,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
BROWSER_DATA_DIR = _BASE_DIR / "browser_data"
BROWSER_DATA_DIR.mkdir(exist_ok=True)

# 主素材为下列后缀时，头条图文应按「单图封面」上传主图，忽略模型误传的 toutiao_graphic_no_cover。
_IMAGE_MAIN_ASSET_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
# 视频主素材须走上传视频链路；若同上轮纯文残留 no_cover=true，会误进图文发布页。
_VIDEO_MAIN_ASSET_SUFFIX = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".m4v"}


def _query_publish_account_by_nickname(
    db: Session, user_id: int, nick: str
) -> Optional[PublishAccount]:
    """按昵称查且必须唯一；0 条返回 None；多条抛 400。"""
    q = db.query(PublishAccount).filter(
        PublishAccount.user_id == user_id,
        PublishAccount.nickname == nick,
    )
    n = q.count()
    if n == 0:
        return None
    if n > 1:
        ids = [r.id for r in q.all()]
        logger.warning(
            "[PUBLISH-API] 拒绝-同昵称多账号 user_id=%s nickname_repr=%r count=%d account_ids=%s",
            user_id,
            nick,
            n,
            ids,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "存在多个相同昵称的发布账号，无法仅凭昵称识别。请改用 account_id 调用发布，"
                "或在「发布管理」中为各平台账号设置互不相同的昵称。"
            ),
        )
    return q.first()


def _resolve_publish_account_for_request(
    db: Session,
    user_id: int,
    account_id: Optional[int],
    account_nickname: Optional[str],
) -> Optional[PublishAccount]:
    """
    用户常以「2」「6」指昵称；若误把该数字填在 account_id 里，主键无匹配时按昵称再查一次。
    若同时传 account_nickname 与 account_id，优先按昵称解析（与单发时一致）。
    """
    nick = (account_nickname or "").strip()
    if nick:
        return _query_publish_account_by_nickname(db, user_id, nick)
    if account_id is not None:
        acct = (
            db.query(PublishAccount)
            .filter(
                PublishAccount.id == account_id,
                PublishAccount.user_id == user_id,
            )
            .first()
        )
        if acct is not None:
            return acct
        nick_candidate = str(account_id).strip()
        acct = _query_publish_account_by_nickname(db, user_id, nick_candidate)
        if acct is not None:
            logger.info(
                "[PUBLISH-API] account_id=%s 无主键匹配，已按昵称「%s」解析为 id=%s platform=%s",
                account_id,
                nick_candidate,
                acct.id,
                acct.platform,
            )
        return acct
    return None


def _effective_publish_copy_from_asset(
    asset: Asset,
    title: Optional[str],
    description: Optional[str],
    tags: Optional[str],
    *,
    xhs_strict: bool = False,
) -> tuple[str, str, str]:
    """
    未传标题/正文时，用素材 generation prompt、素材 tags、文件名 stem 补全（非 LLM，避免发布链路再调模型）。
    xhs_strict：小红书入参已校验过；用户未传 description 但传了 tags 时，勿用素材 prompt 冒充正文（由前端驱动用话题拼正文）。
    """
    t = (title or "").strip()
    d = (description or "").strip()
    g = (tags or "").strip()
    prompt = (getattr(asset, "prompt", None) or "").strip()
    asset_tags = (getattr(asset, "tags", None) or "").strip()
    stem = Path(asset.filename or "untitled").stem or "作品"

    def _title_from_description_line() -> str:
        """用户/模型已写正文但未写标题时，用正文首行作标题，避免用 asset_id 文件名当标题。"""
        if not d:
            return ""
        line0 = d.split("\n", 1)[0].strip()
        return line0[:120] if line0 else ""

    if not t:
        if xhs_strict:
            # 小红书：优先正文首行（≤20 字由后续 normalize 截断），否则文件名 stem
            t = (_title_from_description_line()[:20] if d else "") or stem[:120]
        elif prompt:
            first = prompt.split("\n", 1)[0].strip()
            t = (
                (first[:120] if first else prompt[:120])
                or _title_from_description_line()
                or stem[:120]
            )
        else:
            t = _title_from_description_line() or stem[:120]
    if not d:
        if xhs_strict and g:
            d = ""
        elif prompt:
            d = prompt[:5000]
        else:
            d = t if t else "作品分享"
    if not g and asset_tags:
        g = asset_tags[:2000]
    return t, d, g


def _sanitize_internal_publish_tags(g: str) -> str:
    """去掉 MCP/速推自动入库标记，避免抖音等把 tags 拼进描述后出现 #sutui.transfer_url 等。"""
    parts = [x.strip() for x in (g or "").split(",") if x.strip()]
    drop = {"auto", "task.get_result", "sutui.transfer_url", "transfer_url"}
    out: List[str] = []
    for p in parts:
        pl = p.lower()
        if pl in drop:
            continue
        # 形如 sutui.xxx 的能力 ID，不作用户话题
        if pl.startswith("sutui.") and len(pl) > 6:
            continue
        if pl.endswith(".transfer_url"):
            continue
        out.append(p)
    return ",".join(out)


def _infer_asset_media_type(a: Asset) -> str:
    mt = (getattr(a, "media_type", None) or "").strip().lower()
    if mt in ("video", "image", "audio"):
        return mt
    suf = Path(a.filename or "").suffix.lower()
    if suf in (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"):
        return "video"
    if suf in _IMAGE_MAIN_ASSET_SUFFIX:
        return "image"
    return "video"


def _truthy(v: Optional[object]) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


def _toutiao_opts_wants_graphic_no_cover(opts: Optional[dict]) -> bool:
    """与头条发布驱动一致：顶层 toutiao_graphic_no_cover 或 toutiao.{graphic_no_cover|no_cover}。"""
    if not isinstance(opts, dict):
        return False
    if _truthy(opts.get("toutiao_graphic_no_cover")):
        return True
    inner = opts.get("toutiao")
    if isinstance(inner, dict):
        if _truthy(inner.get("graphic_no_cover")) or _truthy(inner.get("no_cover")):
            return True
    return False


_TOUTIAO_PUBLISH_NO_ASSET_SENTINEL = "__toutiao_graphic_no_asset__"


def _effective_publish_copy_no_asset(
    title: Optional[str],
    description: Optional[str],
    tags: Optional[str],
) -> tuple[str, str, str]:
    """无素材行：仅用入参补全文案（今日头条无封面纯文等）。"""
    t = (title or "").strip()
    d = (description or "").strip()
    g = (tags or "").strip()

    def _title_from_description_line() -> str:
        if not d:
            return ""
        line0 = d.split("\n", 1)[0].strip()
        return line0[:120] if line0 else ""

    if not t:
        t = _title_from_description_line() or "分享"
    if not d:
        d = t if t else "作品分享"
    return t, d, g


def _toutiao_strip_graphic_no_cover_for_image_main(publish_opts: dict, main_suffix: str) -> None:
    """
    对话常在 publish_content 的 options 里带 toutiao_graphic_no_cover=true；
    主素材为**图片**时强制走单图封面流程；主素材为**视频**时须走上传视频入口，不能有「纯文无封面」残留。
    确需「有图占位但仍无封面」时传 toutiao_force_graphic_no_cover: true（仅对图片主素材生效）。
    """
    suf = (main_suffix or "").lower()
    is_img = suf in _IMAGE_MAIN_ASSET_SUFFIX
    is_vid = suf in _VIDEO_MAIN_ASSET_SUFFIX
    if not is_img and not is_vid:
        return
    if is_img and _truthy(publish_opts.get("toutiao_force_graphic_no_cover")):
        return
    had = publish_opts.get("toutiao_graphic_no_cover") is not None
    inner = publish_opts.get("toutiao")
    if isinstance(inner, dict):
        had = had or inner.get("graphic_no_cover") is not None or inner.get("no_cover") is not None
    if not had:
        return
    publish_opts.pop("toutiao_graphic_no_cover", None)
    if is_vid:
        publish_opts.pop("toutiao_force_graphic_no_cover", None)
    if isinstance(inner, dict):
        inner = {k: v for k, v in inner.items() if k not in ("graphic_no_cover", "no_cover")}
        if inner:
            publish_opts["toutiao"] = inner
        else:
            publish_opts.pop("toutiao", None)
    logger.info(
        "[PUBLISH-API] 头条：主素材为%s(%s)，已移除 options 中的无封面开关",
        "图" if is_img else "视频",
        suf,
    )


SUPPORTED_PLATFORMS = {
    "douyin": {"name": "抖音", "login_url": "https://creator.douyin.com"},
    "bilibili": {"name": "B站", "login_url": "https://member.bilibili.com"},
    "xiaohongshu": {"name": "小红书", "login_url": "https://creator.xiaohongshu.com"},
    "kuaishou": {"name": "快手", "login_url": "https://cp.kuaishou.com"},
    "toutiao": {"name": "今日头条", "login_url": "https://mp.toutiao.com/login/"},
    "douyin_shop": {"name": "抖店", "login_url": "https://fxg.jinritemai.com/"},
    "xiaohongshu_shop": {"name": "小红书店铺", "login_url": "https://ark.xiaohongshu.com/"},
    "alibaba1688": {"name": "1688", "login_url": "https://work.1688.com/"},
    "taobao": {"name": "淘宝", "login_url": "https://seller.taobao.com/"},
    "pinduoduo": {"name": "拼多多", "login_url": "https://mms.pinduoduo.com/"},
}

def _ensure_tiny_mp4(path: Path) -> Path:
    # A tiny MP4 (base64) for dry-run uploads.
    import base64
    tiny_b64 = (
        "AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAACMbW9vdgAAAGxtdmhk"
        "AAAAAAAAAAAAAAAAAAAAAAAD6AAAA+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAA"
        "AAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAIVdHJhawAA"
        "AFx0a2hkAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAAAAAEAAAAA"
        "AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAIAAAABAAAAAQAAAAAAJGVkdHMAAAAc"
        "ZWxzdAAAAAAAAAABAAAD6AAAA+gAAAAAAAEabWRpYQAAACBtZGhkAAAAAAAAAAAA"
        "AAAAAAAAAAAyAAAAMgAAVcQAAAAAAC1oZGxyAAAAAAAAAAB2aWRlAAAAAAAAAAAA"
        "AAAAAFZpZGVvSGFuZGxlcgAAAAE3bWluZgAAABR2bWhkAAAAAAAAAAAAAAAALGRp"
        "bmYAAAAcZHJlZgAAAAAAAAABAAAADHVybCAAAAABAAAAK3N0YmwAAAAVc3RzZAAA"
        "AAEAAAANYXZjMQAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUYXZjQwEB/4QAF2JtZGF0AAAAAA=="
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.write_bytes(base64.b64decode(tiny_b64))
    return path


# ── Account CRUD ──────────────────────────────────────────────────

class AddAccountReq(BaseModel):
    platform: str
    nickname: str
    proxy_server: Optional[str] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None
    user_agent: Optional[str] = None


@router.get("/api/accounts", summary="列出发布账号")
def list_accounts(
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    rows = db.query(PublishAccount).filter(
        PublishAccount.user_id == current_user.id,
    ).order_by(PublishAccount.created_at.desc()).all()
    acct_ids = [a.id for a in rows]
    last_sync_map = {}
    sched_map = {}
    try:
        if acct_ids:
            subq = (
                db.query(
                    CreatorContentSnapshot.account_id.label("aid"),
                    func.max(CreatorContentSnapshot.id).label("mid"),
                )
                .filter(
                    CreatorContentSnapshot.user_id == current_user.id,
                    CreatorContentSnapshot.account_id.in_(acct_ids),
                )
                .group_by(CreatorContentSnapshot.account_id)
                .subquery()
            )
            snap_rows = (
                db.query(CreatorContentSnapshot)
                .join(
                    subq,
                    (CreatorContentSnapshot.account_id == subq.c.aid)
                    & (CreatorContentSnapshot.id == subq.c.mid),
                )
                .all()
            )
            for s in snap_rows:
                n = len(s.items) if s.items else 0
                last_sync_map[s.account_id] = {
                    "fetched_at": isoformat_utc(s.fetched_at),
                    "item_count": n,
                    "sync_error": s.sync_error,
                }
        if acct_ids:
            for sch in (
                db.query(PublishAccountCreatorSchedule)
                .filter(
                    PublishAccountCreatorSchedule.user_id == current_user.id,
                    PublishAccountCreatorSchedule.account_id.in_(acct_ids),
                )
                .all()
            ):
                sk = (getattr(sch, "schedule_kind", None) or "image").strip().lower()
                if sk not in ("image", "video"):
                    sk = "image"
                pm = (getattr(sch, "schedule_publish_mode", None) or "immediate").strip().lower()
                if pm not in ("immediate", "review"):
                    pm = "immediate"
                sched_map[sch.account_id] = {
                    "enabled": sch.enabled,
                    "interval_minutes": getattr(sch, "interval_minutes", None) or 60,
                    "next_run_at": isoformat_utc(getattr(sch, "next_run_at", None)),
                    "schedule_kind": sk,
                    "video_source_asset_id": getattr(sch, "video_source_asset_id", None),
                    "schedule_publish_mode": pm,
                    "review_variant_count": int(getattr(sch, "review_variant_count", None) or 3),
                    "review_first_eta_at": isoformat_utc(
                        getattr(sch, "review_first_eta_at", None)
                    ),
                    "review_drafts_json": getattr(sch, "review_drafts_json", None),
                    "review_confirmed": bool(getattr(sch, "review_confirmed", False)),
                    "review_selected_slot": int(getattr(sch, "review_selected_slot", None) or 0),
                }
    except OperationalError as e:
        logger.warning("[PUBLISH-API] 创作者快照/定时表不可用，仅返回账号列表（请重启后端以建表）: %s", e)
    return {
        "accounts": [
            {
                "id": a.id,
                "platform": a.platform,
                "platform_name": SUPPORTED_PLATFORMS.get(a.platform, {}).get("name", a.platform),
                "nickname": a.nickname,
                "status": a.status,
                "last_login": isoformat_utc(a.last_login),
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "last_creator_sync": last_sync_map.get(a.id),
                "creator_schedule": sched_map.get(a.id),
            }
            for a in rows
        ],
        "platforms": [
            {"id": k, "name": v["name"]} for k, v in SUPPORTED_PLATFORMS.items()
        ],
    }


@router.post("/api/accounts", summary="添加发布账号")
def add_account(
    body: AddAccountReq,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    if body.platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(400, detail=f"不支持的平台: {body.platform}")

    nick = body.nickname.strip()
    if not nick:
        raise HTTPException(400, detail="账号昵称不能为空")

    browser: dict = {}
    ps = (body.proxy_server or "").strip()
    if ps:
        px: dict = {"server": ps}
        u = (body.proxy_username or "").strip()
        p = body.proxy_password or ""
        if u or p:
            if not u or not p:
                raise HTTPException(
                    400, detail="代理用户名与密码须同时填写或同时留空"
                )
            px["username"] = u
            px["password"] = str(p)
        browser["proxy"] = px
    ua_in = (body.user_agent or "").strip()
    if ua_in:
        browser["user_agent"] = ua_in
    meta = {"browser": browser} if browser else None
    if meta:
        try:
            browser_options_from_publish_meta(meta)
        except ValueError as e:
            raise HTTPException(400, detail=str(e))

    profile_dir = BROWSER_DATA_DIR / f"{body.platform}_{nick}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    acct = PublishAccount(
        user_id=current_user.id,
        platform=body.platform,
        nickname=nick,
        status="pending",
        browser_profile=str(profile_dir),
        meta=meta,
    )
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return {
        "id": acct.id,
        "platform": acct.platform,
        "nickname": acct.nickname,
        "status": acct.status,
        "message": f"账号已添加，请点击「登录」完成扫码",
    }


@router.post("/api/accounts/{account_id}/login", summary="启动浏览器登录")
async def start_login(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    platform_info = SUPPORTED_PLATFORMS.get(acct.platform, {})
    login_url = platform_info.get("login_url", "")

    try:
        from publisher.browser_pool import open_login_browser

        bopts = browser_options_from_publish_meta(acct.meta)
        _ = await open_login_browser(
            profile_dir=acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}"),
            login_url=login_url,
            platform=acct.platform,
            browser_options=bopts,
        )
        # Don't block/poll here; don't pop interruptive messages.
        acct.status = "pending"
        db.commit()
        return {"ok": True, "status": "pending", "message": "已打开浏览器，请扫码登录（完成后手动关闭窗口）"}
    except Exception as e:
        logger.exception("Login browser failed")
        return {"ok": False, "status": "error", "message": str(e)}


@router.post("/api/accounts/{account_id}/open-browser", summary="打开账号浏览器")
async def open_browser(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    platform_info = SUPPORTED_PLATFORMS.get(acct.platform, {})
    login_url = platform_info.get("login_url", "")
    profile_dir = acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}")

    try:
        from publisher.browser_pool import open_and_check_browser

        bopts = browser_options_from_publish_meta(acct.meta)
        result = await open_and_check_browser(
            profile_dir=profile_dir,
            login_url=login_url,
            platform=acct.platform,
            browser_options=bopts,
        )
        logged_in = result.get("logged_in", False)
        if logged_in and acct.status != "active":
            acct.status = "active"
            acct.last_login = datetime.utcnow()
            db.commit()
        return {"ok": True, "logged_in": logged_in, "message": result.get("message", "")}
    except Exception as e:
        logger.exception("Open browser failed")
        return {"ok": False, "logged_in": False, "message": str(e)}


@router.get("/api/accounts/{account_id}/login-status", summary="检查登录状态")
async def check_login_status(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    try:
        from publisher.browser_pool import check_browser_login

        bopts = browser_options_from_publish_meta(acct.meta)
        logged_in = await check_browser_login(
            profile_dir=acct.browser_profile or str(BROWSER_DATA_DIR / f"{acct.platform}_{acct.nickname}"),
            platform=acct.platform,
            browser_options=bopts,
        )
        if logged_in and acct.status != "active":
            acct.status = "active"
            acct.last_login = datetime.utcnow()
            db.commit()
        return {"logged_in": logged_in, "message": "已登录" if logged_in else "未登录，请在浏览器中扫码"}
    except Exception as e:
        logger.exception("Check login status failed")
        return {"logged_in": False, "message": str(e)}


@router.delete("/api/accounts/{account_id}", summary="删除发布账号")
def delete_account(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.id == account_id,
        PublishAccount.user_id == current_user.id,
    ).first()
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    import shutil
    if acct.browser_profile:
        p = Path(acct.browser_profile)
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    db.delete(acct)
    db.commit()
    return {"ok": True}


# ── Publish tasks ─────────────────────────────────────────────────

class PublishReq(BaseModel):
    # 可选：今日头条无封面纯文 + options.toutiao_graphic_no_cover 时无需真实素材
    asset_id: Optional[str] = None
    account_id: Optional[int] = None
    account_nickname: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[str] = None
    # None：抖音/头条等未传 title+description 时可能自动 AI；小红书见 create_publish_task 校验（默认不自动 AI）。
    # True：强制 AI 生成（失败则 503）；False：仅用素材/入参补全，不调用模型。
    ai_publish_copy: Optional[bool] = None
    # platform-specific options, e.g. douyin schedule/visibility/location/yellow_cart；
    # 抖音视频：douyin_cover_mode = smart | upload | manual（见 create_publish_task 校验）；
    # douyin_manual_cover_wait_sec：manual 时轮询秒数上限（默认 600）。
    # 头条：无图纯文可 toutiao_graphic_no_cover=true；主素材为图片时 API 会忽略该开关走单图封面，
    # 除非 toutiao_force_graphic_no_cover=true（极少用）。
    options: Optional[dict] = None
    # 可选第二图片；头条视频作单独封面；头条图文时主 asset 即「封面图」，此项可作补充配图
    cover_asset_id: Optional[str] = None


@router.post("/api/douyin/dryrun", summary="抖音发布 dry-run（走到发布前一步）")
async def douyin_dryrun(
    account_nickname: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = db.query(PublishAccount).filter(
        PublishAccount.user_id == current_user.id,
        PublishAccount.platform == "douyin",
        PublishAccount.nickname == account_nickname.strip(),
    ).first()
    if not acct or not acct.browser_profile:
        raise HTTPException(404, detail="抖音账号不存在或未配置浏览器 profile")

    # Generate a tiny local MP4 for upload dry-run
    from .assets import ASSETS_DIR
    mp4_path = _ensure_tiny_mp4(Path(ASSETS_DIR) / "dryrun_tiny.mp4")

    try:
        from publisher.browser_pool import dryrun_douyin_upload_in_context

        bopts = browser_options_from_publish_meta(acct.meta)
        result = await dryrun_douyin_upload_in_context(
            profile_dir=acct.browser_profile,
            file_path=str(mp4_path),
            browser_options=bopts,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        logger.exception("Douyin dryrun failed")
        return {"ok": False, "error": str(e)}


def _bearer_from_request(request: Request) -> str:
    a = (request.headers.get("Authorization") or "").strip()
    if a.lower().startswith("bearer "):
        return a[7:].strip()
    return ""


def _chat_model_from_request(request: Request) -> str:
    return (request.headers.get("X-Chat-Model") or request.headers.get("x-chat-model") or "").strip()


@router.post("/api/publish", summary="发布素材到平台")
async def create_publish_task(
    request: Request,
    body: PublishReq,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    _nick_raw = body.account_nickname or ""
    _nick_s = _nick_raw.strip()
    _asset_id_s = (body.asset_id or "").strip()
    logger.info(
        "[PUBLISH-API] 入参 user_id=%s asset_id=%s account_id=%r account_nickname_repr=%s len(strip)=%d",
        current_user.id,
        _asset_id_s or "(empty)",
        body.account_id,
        repr(_nick_raw[:200]) if len(_nick_raw) <= 200 else repr(_nick_raw[:200] + "…"),
        len(_nick_s),
    )

    acct = _resolve_publish_account_for_request(
        db, current_user.id, body.account_id, body.account_nickname
    )
    if not acct:
        if body.account_id is not None and not _nick_s:
            logger.warning(
                "[PUBLISH-API] 拒绝-发布账号不存在 user_id=%s 原因=account_id 与昵称均无匹配 account_id=%s",
                current_user.id,
                body.account_id,
            )
        elif body.account_nickname:
            all_rows = (
                db.query(PublishAccount.nickname, PublishAccount.platform, PublishAccount.id)
                .filter(PublishAccount.user_id == current_user.id)
                .order_by(PublishAccount.id.asc())
                .all()
            )
            preview = [(r[0], r[1], r[2]) for r in all_rows[:30]]
            logger.warning(
                "[PUBLISH-API] 拒绝-发布账号不存在 user_id=%s 原因=昵称无匹配 "
                "nickname_repr=%r len=%d 库内账号(昵称,platform,id)前30条=%s",
                current_user.id,
                _nick_s,
                len(_nick_s),
                preview,
            )
        else:
            logger.warning(
                "[PUBLISH-API] 拒绝-发布账号不存在 user_id=%s 原因=未传 account_id 与 account_nickname asset_id=%s",
                current_user.id,
                _asset_id_s,
            )
        raise HTTPException(404, detail="发布账号不存在，请先在「发布管理」中添加账号")

    # 头条且无主素材：默认按「无封面图文」走文案发布链（与 MCP/对话层常漏传 options 的情况对齐）。
    # 显式传 toutiao_graphic_no_cover: false 时不改写；若已指定独立封面素材则不自动无封面。
    if (
        acct.platform == "toutiao"
        and not _asset_id_s
        and not (body.cover_asset_id or "").strip()
    ):
        if body.options is None:
            body.options = {}
        if isinstance(body.options, dict) and body.options.get("toutiao_graphic_no_cover") is not False:
            if not _toutiao_opts_wants_graphic_no_cover(body.options):
                body.options = dict(body.options)
                body.options.setdefault("toutiao_graphic_no_cover", True)
                logger.info(
                    "[PUBLISH-API] 头条无 asset_id：已自动 options.toutiao_graphic_no_cover=true"
                )
    _opts_early = body.options if isinstance(body.options, dict) else {}

    toutiao_text_only = (
        acct.platform == "toutiao"
        and not _asset_id_s
        and _toutiao_opts_wants_graphic_no_cover(_opts_early)
    )
    if not _asset_id_s and not toutiao_text_only:
        raise HTTPException(
            status_code=400,
            detail=(
                "请提供素材 asset_id。"
                "若发布今日头条无封面纯文字，请在 options 中设置 toutiao_graphic_no_cover: true。"
            ),
        )

    asset: Optional[Asset] = None
    if _asset_id_s:
        asset = db.query(Asset).filter(
            Asset.asset_id == _asset_id_s,
            Asset.user_id == current_user.id,
        ).first()
        if not asset:
            logger.warning(
                "[PUBLISH-API] 拒绝-素材不存在 user_id=%s asset_id=%s",
                current_user.id,
                _asset_id_s,
            )
            raise HTTPException(404, detail=f"素材不存在: {_asset_id_s}")
    # Allow publishing even when status isn't active: run_publish_task will open browser and wait for login.

    body_title_s = (body.title or "").strip()
    body_desc_s = (body.description or "").strip()
    body_tags_raw_s = (body.tags or "").strip()

    xhs_strict = acct.platform == "xiaohongshu"
    if xhs_strict:
        if body.ai_publish_copy is True:
            if not body_desc_s:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "使用 AI 撰写小红书文案时，须在请求里附带与视频相关的文字说明或要点（不可留空），"
                        "以免生成内容与视频不符。"
                    ),
                )
            use_llm = True
        else:
            if not body_title_s or not (body_desc_s or body_tags_raw_s):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "小红书发布需要标题，以及正文或话题标签至少一项。"
                        "请补充后再试；若希望根据口语要点由 AI 生成全文，请在对话里说明要点，"
                        "由助手代为填写并发起发布。"
                    ),
                )
            use_llm = False
    else:
        if body.ai_publish_copy is True:
            use_llm = True
        elif body.ai_publish_copy is False:
            use_llm = False
        else:
            use_llm = (not body_title_s) and (not body_desc_s)

    eff_title = ""
    eff_desc = ""
    eff_tags = ""
    if use_llm:
        from ..services.publish_copy_llm import PublishCopyLLMError, generate_publish_copy

        try:
            eff_title, eff_desc, eff_tags = await generate_publish_copy(
                platform=acct.platform,
                media_type=_infer_asset_media_type(asset) if asset is not None else "image",
                asset_prompt=(getattr(asset, "prompt", None) or "") if asset is not None else "",
                filename=(asset.filename or "") if asset is not None else "toutiao_graphic_text_only.mp4",
                hint_title=body_title_s,
                hint_desc=body_desc_s,
                hint_tags=body_tags_raw_s,
                raw_token=_bearer_from_request(request) or None,
                chat_model=_chat_model_from_request(request) or None,
            )
            logger.info(
                "[PUBLISH-API] 已用 AI 生成发布文案 title_len=%d desc_len=%d tags_len=%d",
                len(eff_title),
                len(eff_desc),
                len(eff_tags),
            )
        except PublishCopyLLMError as e:
            if body.ai_publish_copy is True:
                raise HTTPException(status_code=503, detail=str(e)) from e
            logger.warning("[PUBLISH-API] AI 发布文案不可用，回退素材补全: %s", e)
            if asset is not None:
                eff_title, eff_desc, eff_tags = _effective_publish_copy_from_asset(
                    asset,
                    body.title,
                    body.description,
                    body.tags,
                    xhs_strict=xhs_strict,
                )
            else:
                eff_title, eff_desc, eff_tags = _effective_publish_copy_no_asset(
                    body.title,
                    body.description,
                    body.tags,
                )
        eff_tags = _sanitize_internal_publish_tags(eff_tags)
    else:
        if asset is not None:
            eff_title, eff_desc, eff_tags = _effective_publish_copy_from_asset(
                asset,
                body.title,
                body.description,
                body.tags,
                xhs_strict=xhs_strict,
            )
        else:
            eff_title, eff_desc, eff_tags = _effective_publish_copy_no_asset(
                body.title,
                body.description,
                body.tags,
            )
        eff_tags = _sanitize_internal_publish_tags(eff_tags)
        if asset is not None and (not xhs_strict) and (not body_title_s or not body_desc_s):
            logger.info(
                "[PUBLISH-API] 标题或描述未传，已从素材 prompt/文件名 补全 title_len=%d desc_len=%d tags_len=%d",
                len(eff_title),
                len(eff_desc),
                len(eff_tags),
            )

    _opts = body.options or {}
    _tt_nc = _opts.get("toutiao_graphic_no_cover")
    logger.info(
        "[PUBLISH-API] 请求: asset_id=%s account=%s platform=%s options.toutiao_graphic_no_cover=%r",
        _asset_id_s or _TOUTIAO_PUBLISH_NO_ASSET_SENTINEL,
        acct.nickname,
        acct.platform,
        _tt_nc,
    )

    task = PublishTask(
        user_id=current_user.id,
        asset_id=_asset_id_s or _TOUTIAO_PUBLISH_NO_ASSET_SENTINEL,
        account_id=acct.id,
        title=eff_title,
        description=eff_desc,
        tags=eff_tags,
        status="pending",
        meta={
            "options": body.options or {},
            "cover_asset_id": body.cover_asset_id,
            "platform": acct.platform,
            "account_nickname": acct.nickname,
        },
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    logger.info(
        "[PUBLISH-API] task_id=%s 若感觉页面反复进出：在 backend.log 搜 PUBLISH-NAV / TOUTIAO-NAV；"
        "多条 1_after_acquire=多次发布请求；单条内出现 3_passive_failed 且 url 变首页=登录检测拉回",
        task.id,
    )

    try:
        from publisher.browser_pool import run_publish_task
        from .assets import ASSETS_DIR

        async def _resolve_asset_path(a: Asset):
            """返回 (path_str, temp_path_to_delete)。仅用公网 source_url 下载到临时文件，不使用本地路径。"""
            url = getattr(a, "source_url", None) or ""
            if not (url.startswith("http://") or url.startswith("https://")):
                raise HTTPException(
                    400,
                    detail="素材未上传至火山（无公网链接），请先在素材管理中上传并同步至火山后再发布。",
                )
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.get(url)
            r.raise_for_status()
            suf = Path(a.filename or "").suffix or ".mp4"
            fd, path = tempfile.mkstemp(suffix=suf)
            try:
                import os
                os.write(fd, r.content)
            finally:
                import os
                os.close(fd)
            return path, path

        if toutiao_text_only:
            file_path = str(
                _ensure_tiny_mp4(Path(ASSETS_DIR) / "toutiao_text_only_placeholder.mp4")
            )
            temp_video = None
            logger.info(
                "[PUBLISH-API] 头条无素材纯文：占位主文件（驱动走 graphic_no_cover）path=%s",
                file_path,
            )
        else:
            file_path, temp_video = await _resolve_asset_path(asset)
        logger.info(
            "[PUBLISH-API] asset file=%s exists=%s",
            file_path,
            Path(file_path).exists(),
        )
        logger.info(
            "[PUBLISH-API] 实际发布文案 len(title)=%d len(description)=%d len(tags)=%d title_head=%r desc_head=%r",
            len(eff_title),
            len(eff_desc),
            len(eff_tags),
            eff_title[:50],
            (eff_desc[:80] + ("…" if len(eff_desc) > 80 else "")),
        )
        if acct.platform == "toutiao" and not eff_desc.strip():
            logger.warning(
                "[PUBLISH-API] 头条 description 仍为空（素材无 prompt）：图文可能无正文。"
            )
        if acct.platform == "xiaohongshu" and not eff_desc.strip() and not eff_tags.strip():
            logger.warning(
                "[PUBLISH-API] 小红书 description 与 tags 仍为空（素材无可用文案）。"
            )

        publish_opts = dict(body.options or {})
        if acct.platform == "douyin" and _infer_asset_media_type(asset) == "video":
            mode = (publish_opts.get("douyin_cover_mode") or "smart").strip().lower()
            if mode not in ("smart", "upload", "manual"):
                raise HTTPException(
                    400,
                    detail="抖音视频发布须在 options.douyin_cover_mode 指定 smart | upload | manual",
                )
            publish_opts["douyin_cover_mode"] = mode
            if mode == "upload" and not (body.cover_asset_id or "").strip():
                raise HTTPException(
                    400,
                    detail="douyin_cover_mode=upload 时必须指定 cover_asset_id（封面图素材）",
                )

        cover_path = None
        temp_cover = None
        if body.cover_asset_id:
            cover = db.query(Asset).filter(
                Asset.asset_id == body.cover_asset_id,
                Asset.user_id == current_user.id,
            ).first()
            if cover:
                cover_path, temp_cover = await _resolve_asset_path(cover)
        if acct.platform == "toutiao":
            # 纯文占位文件仍是 .mp4；若此处按「视频主素材」剥掉 no_cover，驱动会把占位当真视频走上传视频页。
            if not toutiao_text_only:
                main_suf = (
                    Path(asset.filename or "").suffix if asset is not None else ""
                ) or Path(file_path).suffix
                _toutiao_strip_graphic_no_cover_for_image_main(publish_opts, main_suf)

        logger.info("[PUBLISH-API] calling run_publish_task: platform=%s profile=%s title=%s",
                     acct.platform, acct.browser_profile, eff_title[:40])
        bopts = browser_options_from_publish_meta(acct.meta)
        result = await run_publish_task(
            profile_dir=acct.browser_profile,
            platform=acct.platform,
            file_path=file_path,
            title=eff_title,
            description=eff_desc,
            tags=eff_tags,
            options=publish_opts,
            cover_path=cover_path,
            browser_options=bopts,
        )
        for p in (temp_video, temp_cover):
            if p and Path(p).exists():
                try:
                    Path(p).unlink()
                except Exception:
                    pass
        logger.info("[PUBLISH-API] result: %s", {k: v for k, v in result.items() if k != "applied"})
        task.status = "success" if result.get("ok") else "failed"
        if result.get("need_login"):
            task.status = "need_login"
        task.result_url = result.get("url", "")
        task.error = result.get("error", "")
        task.meta = {
            **(task.meta or {}),
            "driver_result": result,
        }
        task.finished_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        task.status = "failed"
        task.error = str(e)
        task.finished_at = datetime.utcnow()
        db.commit()
        logger.exception("[PUBLISH-API] publish task exception")

    resp = {
        "task_id": task.id,
        "status": task.status,
        "result_url": task.result_url,
        "error": task.error,
    }
    _dr = (task.meta or {}).get("driver_result") or {}
    if task.status == "success" and _dr.get("toutiao_submission_hint"):
        resp["toutiao_submission_hint"] = _dr["toutiao_submission_hint"]
    if task.status == "failed" and acct.platform == "toutiao":
        err = (task.error or "").strip()
        if "视频发布需要封面" in err or (
            "视频" in err and "封面" in err and ("上传" in err or "截取" in err or "本地上传" in err)
        ):
            resp["agent_constraints"] = [
                "【必读】本次为头条/西瓜「视频」发布失败。禁止再用封面图或其它图片素材的 asset_id 调用 publish_content 发头条「图文」顶替；用户要的是视频，不是文章。",
                "可采取：仍用原视频 asset_id 重试（建议去掉 cover_asset_id）；或如实告知用户自动化封面未成功、需对方在发布页手动选封面后再发。",
            ]
    if task.status == "need_login" or (task.meta and task.meta.get("driver_result", {}).get("need_login")):
        resp["need_login"] = True
    logger.info("[PUBLISH-API] response: %s", resp)
    return resp


@router.get("/api/publish/tasks", summary="发布任务列表")
def list_publish_tasks(
    limit: int = 50,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(PublishTask)
        .filter(PublishTask.user_id == current_user.id)
        .order_by(PublishTask.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    def _task_dict(t):
        meta = t.meta or {}
        driver_result = meta.get("driver_result", {})
        steps = driver_result.get("applied", {}).get("steps", [])
        return {
            "id": t.id,
            "asset_id": t.asset_id,
            "account_id": t.account_id,
            "title": t.title,
            "status": t.status,
            "result_url": t.result_url,
            "error": t.error,
            "platform": meta.get("platform", ""),
            "account_nickname": meta.get("account_nickname", ""),
            "steps": steps,
            "created_at": t.created_at.isoformat() if t.created_at else "",
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        }
    return {"tasks": [_task_dict(t) for t in rows]}

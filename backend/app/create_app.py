import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.health import router as health_router
from .api.branding import router as branding_router
from .api.auth import router as auth_router, get_password_hash
from .api.chat import router as chat_router
from .api.capabilities import router as capabilities_router
from .api.skills import router as skills_router
from .api.settings_api import router as settings_router
from .api.mcp_gateway import router as mcp_gateway_router
from .api.openclaw_sutui_llm_proxy import router as openclaw_sutui_llm_proxy_router
from .api.openclaw_config import router as openclaw_config_router
from .api.custom_config import router as custom_config_router
from .api.billing import router as billing_router
from .api.consumption_accounts import router as consumption_accounts_router
from .api.mcp_registry import router as mcp_registry_router
try:
    from .api.wecom import router as wecom_router
except Exception as e:
    if "Crypto" in str(e) or "pycryptodome" in str(e).lower() or "wecom_reply" in str(e):
        wecom_router = None
    else:
        raise
from .api.assets import router as assets_router
from .api.media_edit import router as media_edit_router
from .api.comfly_veo import router as comfly_veo_router
from .api.comfly_daihuo import router as comfly_daihuo_router
from .api.publish import router as publish_router
from .api.creator_content import router as creator_content_router
from .api.account_creator_schedule import router as account_creator_schedule_router
from .api.logs_api import router as logs_router
from .api.wechat_oa import router as wechat_oa_router
from .api.twilio_whatsapp import router as twilio_whatsapp_router
from .api.youtube_publish import router as youtube_publish_router
from .api.meta_social_local import router as meta_social_local_router
from .core.config import settings
from .db import Base, engine, SessionLocal
from . import models  # noqa: F401

logger = logging.getLogger(__name__)


def _ensure_default_user():
    """Create the default user if it doesn't exist. 在线版且独立认证时不创建，仅通过注册。"""
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition == "online" and getattr(settings, "lobster_independent_auth", True):
        return
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(
            models.User.email == settings.default_user_email
        ).first()
        if not user:
            user = models.User(
                email=settings.default_user_email,
                hashed_password=get_password_hash(settings.default_user_password),
                credits=99999,
                role="user",
                preferred_model="openclaw",
            )
            db.add(user)
            db.commit()
            logger.info("Created default user: %s", settings.default_user_email)
    except Exception:
        db.rollback()
        logger.exception("Failed to create default user")
    finally:
        db.close()


def _ensure_comfly_veo_capability():
    """已有库时补注册 comfly.veo（与 capability_catalog.json 一致）。"""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return
    cfg = raw.get("comfly.veo")
    if not isinstance(cfg, dict):
        return
    db = SessionLocal()
    try:
        existing = db.query(models.CapabilityConfig).filter(
            models.CapabilityConfig.capability_id == "comfly.veo"
        ).first()
        if existing:
            return
        db.add(
            models.CapabilityConfig(
                capability_id="comfly.veo",
                description=str(cfg.get("description") or "comfly.veo"),
                upstream=str(cfg.get("upstream") or "local"),
                upstream_tool=str(cfg.get("upstream_tool") or "invoke"),
                enabled=bool(cfg.get("enabled", True)),
                unit_credits=int(cfg.get("unit_credits") or 0),
            )
        )
        db.commit()
        logger.info("Seeded capability comfly.veo into CapabilityConfig")
    except Exception:
        db.rollback()
        logger.exception("Failed to seed comfly.veo capability")
    finally:
        db.close()


def _ensure_comfly_daihuo_pipeline_capability():
    """补注册 comfly.veo.daihuo_pipeline（与 capability_catalog.json 一致）。"""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return
    cfg = raw.get("comfly.veo.daihuo_pipeline")
    if not isinstance(cfg, dict):
        return
    db = SessionLocal()
    try:
        existing = db.query(models.CapabilityConfig).filter(
            models.CapabilityConfig.capability_id == "comfly.veo.daihuo_pipeline"
        ).first()
        if existing:
            return
        db.add(
            models.CapabilityConfig(
                capability_id="comfly.veo.daihuo_pipeline",
                description=str(cfg.get("description") or "comfly.veo.daihuo_pipeline"),
                upstream=str(cfg.get("upstream") or "local"),
                upstream_tool=str(cfg.get("upstream_tool") or "invoke"),
                enabled=bool(cfg.get("enabled", True)),
                unit_credits=int(cfg.get("unit_credits") or 0),
            )
        )
        db.commit()
        logger.info("Seeded capability comfly.veo.daihuo_pipeline into CapabilityConfig")
    except Exception:
        db.rollback()
        logger.exception("Failed to seed comfly.veo.daihuo_pipeline capability")
    finally:
        db.close()


def _ensure_media_edit_capability():
    """已有数据库时补注册 media.edit（空库首次启动由 _seed_capability_catalog 一次性写入）。"""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return
    cfg = raw.get("media.edit")
    if not isinstance(cfg, dict):
        return
    db = SessionLocal()
    try:
        existing = db.query(models.CapabilityConfig).filter(
            models.CapabilityConfig.capability_id == "media.edit"
        ).first()
        if existing:
            return
        db.add(
            models.CapabilityConfig(
                capability_id="media.edit",
                description=str(cfg.get("description") or "media.edit"),
                upstream=str(cfg.get("upstream") or "local"),
                upstream_tool=str(cfg.get("upstream_tool") or "invoke"),
                arg_schema=cfg.get("arg_schema") if isinstance(cfg.get("arg_schema"), dict) else None,
                enabled=bool(cfg.get("enabled", True)),
                is_default=bool(cfg.get("is_default", False)),
                unit_credits=int(cfg.get("unit_credits") or 0),
            )
        )
        db.commit()
        logger.info("[启动] 已补注册能力 media.edit")
    except Exception as e:
        logger.warning("补注册 media.edit 失败: %s", e)
        db.rollback()
    finally:
        db.close()


def _seed_capability_catalog():
    """Import capability catalog from mcp/capability_catalog.json on first run."""
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    db = SessionLocal()
    try:
        if db.query(models.CapabilityConfig).count() > 0:
            return
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        for capability_id, cfg in raw.items():
            if not isinstance(capability_id, str) or not isinstance(cfg, dict):
                continue
            db.add(
                models.CapabilityConfig(
                    capability_id=capability_id.strip(),
                    description=str(cfg.get("description") or capability_id),
                    upstream=str(cfg.get("upstream") or "sutui"),
                    upstream_tool=str(cfg.get("upstream_tool") or "").strip(),
                    arg_schema=cfg.get("arg_schema") if isinstance(cfg.get("arg_schema"), dict) else None,
                    enabled=bool(cfg.get("enabled", True)),
                    is_default=bool(cfg.get("is_default", False)),
                    unit_credits=int(cfg.get("unit_credits") or 0),
                )
            )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _sync_missing_capabilities_from_catalog():
    """旧库已有 capability_configs 时 _seed 会跳过；OTA 带新 mcp/capability_catalog.json 后在此补插缺失行。

    否则 MCP 侧已有新品能力（如 comfly.veo.daihuo_pipeline），而本机 DB 无记录，
    list_capabilities / 费用兜底 unit_credits 会与代码不一致，表现为「没有能力」「参考积分未知」等。
    """
    catalog_path = Path(__file__).resolve().parent.parent.parent / "mcp" / "capability_catalog.json"
    if not catalog_path.exists():
        return
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    db = SessionLocal()
    try:
        existing = {row.capability_id for row in db.query(models.CapabilityConfig).all()}
        n = 0
        for capability_id, cfg in raw.items():
            if not isinstance(capability_id, str) or not isinstance(cfg, dict):
                continue
            cid = capability_id.strip()
            if not cid or cid in existing:
                continue
            db.add(
                models.CapabilityConfig(
                    capability_id=cid,
                    description=str(cfg.get("description") or cid),
                    upstream=str(cfg.get("upstream") or "sutui"),
                    upstream_tool=str(cfg.get("upstream_tool") or "").strip(),
                    arg_schema=cfg.get("arg_schema") if isinstance(cfg.get("arg_schema"), dict) else None,
                    enabled=bool(cfg.get("enabled", True)),
                    is_default=bool(cfg.get("is_default", False)),
                    unit_credits=int(cfg.get("unit_credits") or 0),
                )
            )
            existing.add(cid)
            n += 1
        if n:
            db.commit()
            logger.info("[启动] 已从 capability_catalog 补全 %d 条缺失能力（OTA/升级对齐）", n)
    except Exception:
        db.rollback()
        logger.exception("从 capability_catalog 补全能力失败")
    finally:
        db.close()


def _auto_start_openclaw():
    """若本机已有 openclaw 入口则尝试拉起 Gateway；不会在启动时下载 npm/node 依赖（依赖仅在微信点授权时安装）。"""
    try:
        from .api.openclaw_config import _find_openclaw_pid, _restart_openclaw_gateway
        if not _find_openclaw_pid():
            logger.info("OpenClaw Gateway not detected, auto-starting...")
            ok = _restart_openclaw_gateway()
            if ok:
                logger.info("OpenClaw Gateway auto-started successfully")
            else:
                logger.warning("OpenClaw auto-start failed (chat will use direct LLM API)")
        else:
            logger.info("OpenClaw Gateway already running")
    except Exception as e:
        logger.warning("OpenClaw auto-start skipped: %s", e)


def _migrate_wecom_config_enterprise_product():
    """Add enterprise_id, product_id to wecom_configs if missing."""
    from sqlalchemy import text
    try:
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(wecom_configs)"))
            cols = [row[1] for row in r]
            if "enterprise_id" not in cols:
                conn.execute(text("ALTER TABLE wecom_configs ADD COLUMN enterprise_id INTEGER"))
                conn.commit()
            if "product_id" not in cols:
                conn.execute(text("ALTER TABLE wecom_configs ADD COLUMN product_id INTEGER"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration wecom_configs enterprise/product skipped: %s", e)


def _migrate_user_sutui_token():
    """Add sutui_token column to users if missing (online edition)."""
    from sqlalchemy import text
    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(users)"))
            cols = [row[1] for row in r]
            if "sutui_token" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN sutui_token TEXT"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration sutui_token skipped: %s", e)


def _migrate_user_comfly_configs_table():
    """CREATE user_comfly_configs for per-user Comfly API 凭据（在线/单机共用）。"""
    from sqlalchemy import text
    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS user_comfly_configs ("
                    "user_id INTEGER PRIMARY KEY NOT NULL, "
                    "api_key TEXT, api_base TEXT)"
                )
            )
            conn.commit()
    except Exception as e:
        logger.warning("Migration user_comfly_configs skipped: %s", e)


def _migrate_user_wechat_openid():
    """Add wechat_openid column to users if missing (服务号网页授权登录)."""
    from sqlalchemy import text
    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(users)"))
            cols = [row[1] for row in r]
            if "wechat_openid" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN wechat_openid VARCHAR(64)"))
                conn.commit()
    except Exception as e:
        logger.warning("Migration wechat_openid skipped: %s", e)


def _migrate_user_brand_mark():
    """Add brand_mark column to users if missing（注册时写入品牌标记）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if not insp.has_table("users"):
            return
        cols = [c["name"] for c in insp.get_columns("users")]
        if "brand_mark" in cols:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN brand_mark VARCHAR(64) NULL"))
    except Exception as e:
        logger.warning("Migration user brand_mark skipped: %s", e)


def _migrate_user_client_installation_id():
    """Add client_installation_id for OpenClaw 渠道凭证（与 X-Installation-Id 一致，免用户改 .env）。"""
    from sqlalchemy import inspect, text

    try:
        insp = inspect(engine)
        if not insp.has_table("users"):
            return
        cols = [c["name"] for c in insp.get_columns("users")]
        if "client_installation_id" in cols:
            return
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN client_installation_id VARCHAR(128) NULL"))
    except Exception as e:
        logger.warning("Migration user client_installation_id skipped: %s", e)


def _migrate_publish_account_creator_schedule_v2():
    """interval + next_run_at；旧表可能仅有 daily 时间点字段。"""
    from sqlalchemy import text

    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(publish_account_creator_schedules)"))
            cols = [row[1] for row in r]
            if not cols:
                return
            if "interval_minutes" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN interval_minutes INTEGER DEFAULT 60 NOT NULL"
                    )
                )
                conn.commit()
            if "next_run_at" not in cols:
                conn.execute(
                    text("ALTER TABLE publish_account_creator_schedules ADD COLUMN next_run_at DATETIME")
                )
                conn.commit()
            if "schedule_kind" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN schedule_kind VARCHAR(16) DEFAULT 'image' NOT NULL"
                    )
                )
                conn.commit()
            if "video_source_asset_id" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN video_source_asset_id VARCHAR(64)"
                    )
                )
                conn.commit()
    except Exception as e:
        logger.warning("Migration publish_account_creator_schedule v2 skipped: %s", e)


def _migrate_publish_account_creator_schedule_v3():
    """审核发布模式、草稿 JSON、确认标志。"""
    from sqlalchemy import text

    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(publish_account_creator_schedules)"))
            cols = [row[1] for row in r]
            if not cols:
                return
            if "schedule_publish_mode" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN schedule_publish_mode VARCHAR(16) DEFAULT 'immediate' NOT NULL"
                    )
                )
                conn.commit()
            if "review_variant_count" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN review_variant_count INTEGER DEFAULT 3 NOT NULL"
                    )
                )
                conn.commit()
            if "review_drafts_json" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules ADD COLUMN review_drafts_json TEXT"
                    )
                )
                conn.commit()
            if "review_confirmed" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN review_confirmed INTEGER DEFAULT 0 NOT NULL"
                    )
                )
                conn.commit()
            if "review_selected_slot" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN review_selected_slot INTEGER DEFAULT 0 NOT NULL"
                    )
                )
                conn.commit()
    except Exception as e:
        logger.warning("Migration publish_account_creator_schedule v3 skipped: %s", e)


def _migrate_publish_account_creator_schedule_v4():
    """审核模式：首条预计发布时间（UTC naive），其余条由前端按间隔顺延展示。"""
    from sqlalchemy import text

    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(publish_account_creator_schedules)"))
            cols = [row[1] for row in r]
            if not cols:
                return
            if "review_first_eta_at" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN review_first_eta_at DATETIME"
                    )
                )
                conn.commit()
    except Exception as e:
        logger.warning("Migration publish_account_creator_schedule v4 skipped: %s", e)


def _migrate_publish_account_creator_schedule_v5():
    """审核确认 generation：新确认覆盖未结束的编排。"""
    from sqlalchemy import text

    try:
        if "sqlite" not in settings.database_url:
            return
        with engine.connect() as conn:
            r = conn.execute(text("PRAGMA table_info(publish_account_creator_schedules)"))
            cols = [row[1] for row in r]
            if not cols:
                return
            if "review_confirm_generation" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE publish_account_creator_schedules "
                        "ADD COLUMN review_confirm_generation INTEGER DEFAULT 0 NOT NULL"
                    )
                )
                conn.commit()
    except Exception as e:
        logger.warning("Migration publish_account_creator_schedule v5 skipped: %s", e)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if wecom_router is not None:
        try:
            from .api.wecom import wecom_poll_loop
            asyncio.create_task(wecom_poll_loop())
            logger.info("[启动] 企微自动拉取回复已启动（每 2s）")
        except Exception as e:
            logger.warning("[启动] 企微自动拉取回复未启动: %s", e)
    try:
        from .api.twilio_whatsapp import twilio_whatsapp_poll_loop

        asyncio.create_task(twilio_whatsapp_poll_loop())
        logger.info("[启动] Twilio WhatsApp 自动拉取回复已启动（每 2s）")
    except Exception as e:
        logger.warning("[启动] Twilio WhatsApp 自动拉取回复未启动: %s", e)
    try:
        from .services.creator_schedule_runner import creator_schedule_background_loop

        asyncio.create_task(creator_schedule_background_loop())
        logger.info("[启动] 创作者定时同步已启动（按间隔分钟 + next_run_at UTC）")
    except Exception as e:
        logger.warning("[启动] 创作者定时同步未启动: %s", e)
    try:
        from .services.youtube_publish_schedule_runner import youtube_publish_schedule_background_loop

        asyncio.create_task(youtube_publish_schedule_background_loop())
        logger.info("[启动] YouTube 定时上传已启动（按间隔分钟 + 素材队列）")
    except Exception as e:
        logger.warning("[启动] YouTube 定时上传未启动: %s", e)
    yield


def create_app() -> FastAPI:
    logger.info("[启动] create_app 开始")
    Base.metadata.create_all(bind=engine)
    _migrate_user_sutui_token()
    _migrate_user_comfly_configs_table()
    _migrate_user_wechat_openid()
    _migrate_user_brand_mark()
    _migrate_user_client_installation_id()
    _migrate_publish_account_creator_schedule_v2()
    _migrate_publish_account_creator_schedule_v3()
    _migrate_publish_account_creator_schedule_v4()
    _migrate_publish_account_creator_schedule_v5()
    _migrate_wecom_config_enterprise_product()
    _ensure_default_user()
    _seed_capability_catalog()
    _sync_missing_capabilities_from_catalog()
    _ensure_media_edit_capability()
    _ensure_comfly_veo_capability()
    _ensure_comfly_daihuo_pipeline_capability()
    _auto_start_openclaw()

    app = FastAPI(
        title="龙虾 (Lobster) API",
        version="0.1.0",
        description="龙虾 - 你的私人 AI 助手",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def catch_all(request: Request, exc: Exception):
        if settings.debug:
            import traceback
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error", "debug": str(exc), "traceback": traceback.format_exc()},
            )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    app.include_router(health_router, prefix="")
    app.include_router(branding_router, prefix="")
    app.include_router(auth_router, prefix="/auth")
    app.include_router(capabilities_router, prefix="")
    app.include_router(skills_router, prefix="")
    app.include_router(settings_router, prefix="")
    app.include_router(chat_router, prefix="")
    app.include_router(mcp_gateway_router, prefix="")
    app.include_router(openclaw_sutui_llm_proxy_router, prefix="")
    app.include_router(openclaw_config_router, prefix="")
    app.include_router(custom_config_router, prefix="")
    app.include_router(billing_router, prefix="")
    app.include_router(consumption_accounts_router, prefix="")
    app.include_router(mcp_registry_router, prefix="")
    app.include_router(assets_router, prefix="")
    app.include_router(media_edit_router, prefix="")
    app.include_router(comfly_veo_router, prefix="")
    app.include_router(comfly_daihuo_router, prefix="")
    app.include_router(publish_router, prefix="")
    app.include_router(creator_content_router, prefix="")
    app.include_router(account_creator_schedule_router, prefix="")
    app.include_router(logs_router, prefix="")
    app.include_router(wechat_oa_router, prefix="")
    app.include_router(twilio_whatsapp_router, prefix="")
    app.include_router(youtube_publish_router, prefix="")
    app.include_router(meta_social_local_router, prefix="")
    if wecom_router is not None:
        app.include_router(wecom_router, prefix="")
    else:
        logger.warning("企业微信回复未加载：缺少 pycryptodome，请执行 pip install pycryptodome 后重启")

    assets_dir = Path(__file__).resolve().parent.parent.parent / "assets"
    assets_dir.mkdir(exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(assets_dir)), name="media")

    static_dir = Path(__file__).resolve().parent.parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(static_dir / "index.html")

    logger.info("[启动] create_app 完成")
    return app


app = create_app()
logger.info("[启动] Lobster API 已加载")

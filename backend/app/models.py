from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=99999, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="user", nullable=False)
    preferred_model: Mapped[str] = mapped_column(String(128), default="openclaw", nullable=False)
    """在线版：速推登录后下发的 token，用于调用速推统一接口。单机版为空。"""
    sutui_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    """服务号网页授权 openid，用于微信登录绑定"""
    wechat_openid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """注册时客户端所在安装包的品牌标记（与 LOBSTER_BRAND_MARK / brands.json 的 marks 键一致，如 bihuo、yingshi）。"""
    brand_mark: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    """浏览器 localStorage 安装槽位 ID（与 X-Installation-Id 一致）；登录写入，供 OpenClaw 微信等渠道无 .env 时复用。"""
    client_installation_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class UserComflyConfig(Base):
    """爆款TVC（Comfly）凭据：按登录 user_id 存储，不依赖本地 users 表是否有行（在线版适用）。"""

    __tablename__ = "user_comfly_configs"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_base: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CapabilityConfig(Base):
    __tablename__ = "capability_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    upstream: Mapped[str] = mapped_column(String(64), nullable=False, default="sutui")
    upstream_tool: Mapped[str] = mapped_column(String(128), nullable=False)
    arg_schema: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unit_credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CapabilityCallLog(Base):
    __tablename__ = "capability_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    capability_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    upstream: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    upstream_tool: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credits_charged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    request_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    chat_session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    chat_context_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)


class ToolCallLog(Base):
    """Every MCP tool invocation from chat sessions."""
    __tablename__ = "tool_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    arguments: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    result_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ChatTurnLog(Base):
    __tablename__ = "chat_turn_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    context_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_reply: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── Asset / Publish models ────────────────────────────────────────

class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    asset_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PublishAccount(Base):
    __tablename__ = "publish_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    nickname: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    browser_profile: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Enterprise(Base):
    """企业：多企业，每企业可有 1～2 个产品。"""
    __tablename__ = "enterprises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    company_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class Product(Base):
    """产品：属于某企业，每企业 1～2 个。"""
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_intro: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    common_phrases: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class WecomConfig(Base):
    """企业微信应用配置：支持多应用，每应用一个回调 path；可绑定企业+产品。"""
    __tablename__ = "wecom_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="默认应用")
    callback_path: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    encoding_aes_key: Mapped[str] = mapped_column(String(255), nullable=False)
    corp_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contacts_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    product_knowledge: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enterprise_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    product_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Customer(Base):
    """客户：企微会话对应的外部用户。"""
    __tablename__ = "wecom_customers"
    __table_args__ = (UniqueConstraint("wecom_config_id", "external_user_id", name="uq_wecom_customer_config_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    wecom_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    birthday: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    remark: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    wechat_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class WecomMessage(Base):
    """企微消息记录。"""
    __tablename__ = "wecom_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    wecom_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # in, out
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    external_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    to_user: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TwilioKbEnterprise(Base):
    """WhatsApp（Twilio）客服资料：公司信息；与企微 Enterprise 表独立，互不复用。"""

    __tablename__ = "twilio_kb_enterprises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    company_info: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TwilioKbProduct(Base):
    """WhatsApp 客服资料：产品介绍与常用话术；属于 TwilioKbEnterprise，与企微 Product 独立。"""

    __tablename__ = "twilio_kb_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    enterprise_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    product_intro: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    common_phrases: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TwilioWhatsappMessage(Base):
    """Twilio WhatsApp 消息记录（本机展示会话列表与聊天；与云端 pending 拉取写入一致）。"""

    __tablename__ = "twilio_whatsapp_messages"
    __table_args__ = (Index("ix_twilio_wa_peer_created", "peer_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    peer_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    twilio_message_sid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    to_user: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PublishTask(Base):
    __tablename__ = "publish_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    result_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PublishAccountCreatorSchedule(Base):
    """发布账号：按间隔（分钟）定时同步创作者作品数据（抖/红/头条）+ 图文/视频侧需求配置。"""

    __tablename__ = "publish_account_creator_schedules"
    __table_args__ = (UniqueConstraint("account_id", name="uq_publish_creator_sched_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    frequency: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    time_hhmm_1: Mapped[str] = mapped_column(String(8), default="00:00", nullable=False)
    time_hhmm_2: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    last_slot0_fired_date: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    last_slot1_fired_date: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    schedule_kind: Mapped[str] = mapped_column(String(16), default="image", nullable=False)
    video_source_asset_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requirements_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # immediate=到点按原逻辑走编排；review=先填/生成审核稿，确认后再编排发布
    schedule_publish_mode: Mapped[str] = mapped_column(String(16), default="immediate", nullable=False)
    review_variant_count: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    """审核模式：首条预计发布时间（UTC naive，与 next_run_at 一致）；其余条由前端按 interval_minutes 顺延展示。"""
    review_first_eta_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_drafts_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    review_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    """每次「确认并发布」递增；用于使上一轮审核编排协作退出，保证同账号仅一条有效发布编排。"""
    review_confirm_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_selected_slot: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class YoutubePublishSchedule(Base):
    """YouTube：按账号定时上传（素材队列 FIFO），对齐创作者定时任务的 enabled + interval + next_run_at；不含审核后发布。"""

    __tablename__ = "youtube_publish_schedules"
    __table_args__ = (
        UniqueConstraint("user_id", "youtube_account_id", name="uq_youtube_pub_sched_user_acct"),
        Index("ix_youtube_pub_sched_next", "enabled", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    youtube_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    """待上传素材 asset_id 队列（JSON 数组，先进先出）。"""
    asset_ids_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    material_origin: Mapped[str] = mapped_column(String(32), default="script_batch", nullable=False)
    privacy_status: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    title: Mapped[str] = mapped_column(String(5000), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category_id: Mapped[str] = mapped_column(String(8), default="22", nullable=False)
    tags_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_run_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_video_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CreatorScheduleReviewSnapshot(Base):
    """审核发布：每次「智能生成提示词 / 生成发布内容 / 单条重生成」后的草稿快照，供历史查看与恢复。"""

    __tablename__ = "creator_schedule_review_snapshots"
    __table_args__ = (Index("ix_creator_rev_snap_acct_created", "account_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    drafts_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CreatorScheduleTaskLog(Base):
    """定时任务每次触发的执行记录，供前端任务列表展示。"""

    __tablename__ = "creator_schedule_task_logs"
    __table_args__ = (Index("ix_creator_sch_task_logs_acct_started", "account_id", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    item_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    orchestration_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    orchestration_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class CreatorContentSnapshot(Base):
    """抖音/小红书/今日头条创作者作品列表快照。"""

    __tablename__ = "creator_content_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    items: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


# ── 独立计费：算力账号（耗算力时用哪个速推 Token）、充值订单 ────────────────────────

class ConsumptionAccount(Base):
    """算力账号：用户可配置多个，每个可绑定速推 Token；调用能力时用其一，扣主账号算力。"""
    __tablename__ = "consumption_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    sutui_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class EcommerceDetailJob(Base):
    """电商详情图流水线任务持久化：保存 comfly 套图任务的分组结果，重启后仍可查回。"""
    __tablename__ = "ecommerce_detail_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False, index=True)
    product_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    saved_assets: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, onupdate=datetime.utcnow)


class RechargeOrder(Base):
    """自有充值订单：用户购买算力套餐，支付完成后加算力。"""
    __tablename__ = "recharge_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    amount_yuan: Mapped[int] = mapped_column(Integer, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)  # pending, paid, cancelled
    out_trade_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    payment_method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class WecomScheduledMessage(Base):
    """企微定时消息：周几 + 几点发送。"""
    __tablename__ = "wecom_scheduled_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    wecom_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    send_type: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    to_user: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    to_party: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chatid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    weekdays: Mapped[str] = mapped_column(String(32), nullable=False)
    send_time: Mapped[str] = mapped_column(String(8), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class KfAccount(Base):
    """微信客服账号（本地跟踪）。"""
    __tablename__ = "wecom_kf_accounts"
    __table_args__ = (UniqueConstraint("wecom_config_id", "open_kfid", name="uq_kf_account_config_kfid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    wecom_config_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    open_kfid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="AI客服")
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sync_cursor: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class KfCustomer(Base):
    """微信客服会话客户。"""
    __tablename__ = "wecom_kf_customers"
    __table_args__ = (UniqueConstraint("kf_account_id", "external_userid", name="uq_kf_customer_kf_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kf_account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_userid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    nickname: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    avatar: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_msg_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class KfMessage(Base):
    """微信客服消息记录。"""
    __tablename__ = "wecom_kf_messages"
    __table_args__ = (Index("ix_kf_msg_account_external", "kf_account_id", "external_userid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    kf_account_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    external_userid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    msgid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, unique=True)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    msg_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    origin: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    send_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

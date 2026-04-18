from __future__ import annotations

from functools import lru_cache
from typing import List, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # 避免用户环境里有未声明变量时启动失败；业务配置以本类字段为准
        extra="ignore",
    )

    app_name: str = "龙虾 (Lobster)"
    debug: bool = True

    @field_validator("debug", mode="before")
    @classmethod
    def _coerce_debug_flag(cls, v: object) -> object:
        if isinstance(v, str):
            normalized = v.strip().lower()
            if normalized in {"release", "prod", "production", "false", "0", "off", "no"}:
                return False
            if normalized in {"debug", "dev", "development", "true", "1", "on", "yes"}:
                return True
        return v
    secret_key: str = "lobster-secret-change-me"
    cors_origins: str = "*"
    database_url: str = "sqlite:///./lobster.db"
    # MySQL/PostgreSQL 连接池（SQLite 忽略）；默认大于 SQLAlchemy 的 5+10，避免并发耗尽
    db_pool_size: int = 15
    db_max_overflow: int = 25
    db_pool_timeout: int = 60
    db_pool_recycle: int = 280
    host: str = "0.0.0.0"
    port: int = 8000
    """可选：用于生成素材文件等对外 URL 的根地址（纯 ASCII，避免编码问题）。例: http://192.168.200.57:8000。勿填 127.0.0.1（同网段设备无法预览）。"""
    public_base_url: Optional[str] = None
    """素材签名 URL 专用：本机局域网可访问根地址，如 http://192.168.1.100:8000。当 PUBLIC_BASE_URL 未设或为回环地址时优先使用（在线版多设备预览）。"""
    lan_public_base_url: Optional[str] = None
    mcp_port: int = 8001
    """本仓库为在线客户端，运行时固定为 online（忽略 LOBSTER_EDITION=standalone 等历史值）。"""
    lobster_edition: str = "online"

    @field_validator("lobster_edition", mode="before")
    @classmethod
    def _lobster_edition_fixed_online(cls, v: object) -> str:
        return "online"
    """品牌标记：与 static/branding/brands.json 的 marks 键一致（如 bihuo）。桌面快捷方式与首页 logo/文案由该标记决定。"""
    lobster_brand_mark: str = "bihuo"
    lobster_parent_account: Optional[str] = None
    """在线版为 True 时：登录注册与充值全部自维护，不走速推；用户配置算力账号（速推 Token）用于耗算力，速推扣多少我们扣多少算力。"""
    lobster_independent_auth: bool = True
    """完成充值订单时需在请求头 X-Admin-Secret 携带此值（仅服务端/管理员使用）。"""
    lobster_recharge_admin_secret: Optional[str] = None
    """充值创建订单后展示给用户的付款说明。"""
    lobster_recharge_payment_hint: Optional[str] = None
    default_user_email: str = "user@lobster.local"
    default_user_password: str = "lobster123"
    """在线版：速推 OAuth 登录页 URL，登录成功后跳转到 /auth/sutui-callback?token=xxx"""
    sutui_oauth_login_url: Optional[str] = None
    """速推 API 根地址，用于 apikeys/list、balance 等（仅 online 使用）"""
    sutui_api_base: str = "https://api.xskill.ai"
    """GET /api/v3/models/{id}/docs 的 lang 参数（与 model-pricing-guide 一致），用于对话内按模型估算算力。"""
    xskill_model_docs_lang: str = "zh"
    """模型定价文档内存缓存秒数（减轻 xSkill 文档接口压力）。"""
    xskill_model_docs_cache_ttl_seconds: int = 3600
    """我方标识，登录时带在 URL 上供速推统计（仅 online 使用）"""
    sutui_source_id: Optional[str] = None
    """充值页链接，前端「充值」按钮跳转（仅 online 使用）"""
    sutui_recharge_url: Optional[str] = None
    """是否允许 online 用户自配模型 Key；False 时统一走速推服务端模型（仅 online 使用）"""
    sutui_online_model_self_config: bool = True
    """在线版：未选子模型、payload 为 sutui_aggregate/空、且无可用直连 Key 时，对话默认使用的速推子模型 ID（拼成 sutui/<id>）。"""
    lobster_default_sutui_chat_model: str = "deepseek-chat"
    """定时编排（schedule_orchestration=True）时使用的速推子模型。不填则用默认对话模型。"""
    lobster_orchestration_sutui_chat_model: Optional[str] = None
    openclaw_gateway_url: Optional[str] = None
    openclaw_gateway_token: Optional[str] = None
    openclaw_agent_id: str = "main"
    """OpenClaw 调本机 POST /internal/openclaw-sutui/v1/chat/completions 时的 Bearer，须与 openclaw/.env 中 OPENCLAW_SUTUI_PROXY_KEY 一致。"""
    openclaw_sutui_proxy_key: str = "lobster-oc-sutui-local-change-me"
    """微信等 OpenClaw 渠道消息不经网页 chat，无用户 JWT 与 MCP 缓存；填认证中心颁发的用户 Bearer（建议专用小号），代理在解析不到 JWT 时用其转发 sutui-chat。"""
    openclaw_sutui_fallback_jwt: Optional[str] = None
    """与 fallback JWT 同用户绑定的安装槽位 ID；认证中心若要求 X-Installation-Id 则必填。"""
    openclaw_sutui_fallback_installation_id: Optional[str] = None
    """为 True 时：用户登录/注册/服务号回调成功后把当前 JWT 与 X-Installation-Id 写入 openclaw/.channel_fallback.json，供微信等 OpenClaw 渠道使用（优先于 .env 静态 fallback）。"""
    openclaw_persist_channel_token_on_login: bool = True
    """为 True（默认）：微信助手请求即使带 X-Lobster-Weixin-From-User-Id，也不查 openclaw/.weixin_openclaw_peers.json，MCP/速推代理一律用 channel_fallback（= 当前在本机网页登录写入的 JWT）— 适合一机一微信、不区分子账号。为 False 时按微信好友 ID 查 peers，需 /myid + POST /auth/persist-weixin-openclaw-peer 按人绑定。"""
    openclaw_weixin_single_device_jwt: bool = True
    """非空时：OpenClaw→本代理→认证中心 sutui-chat 的 JSON 里 model 一律改为此值（与网页「速推 LLM」子模型 id 一致，可选）。"""
    openclaw_sutui_upstream_model: Optional[str] = None
    """为 True 时主对话先尝试 OpenClaw Gateway；与 lobster_openclaw_chat_prefix_gate 组合见 chat 路由说明。"""
    lobster_openclaw_primary_chat: bool = False
    """为 True 时主对话仅走 OpenClaw，失败不回退直连+MCP（审核稿与 direct_llm 仍可直连）。"""
    lobster_openclaw_only_chat: bool = False
    """为 True 时：无消息前缀则不因 primary 而优先 OpenClaw（先直连）；带 lobster_openclaw_chat_prefixes 前缀时该轮仍优先 OpenClaw。"""
    lobster_openclaw_chat_prefix_gate: bool = False
    """逗号分隔；消息 strip 后以此前缀开头（不区分大小写）且后缀有正文时剥离前缀，且该轮优先 OpenClaw。例：/openclaw,/OPENCLAW"""
    lobster_openclaw_chat_prefixes: str = "/openclaw,/OPENCLAW"
    """企微云端地址与转发密钥（本地轮询拉取/提交回复时使用）。"""
    wecom_cloud_url: Optional[str] = None
    wecom_forward_secret: Optional[str] = None
    # 微信服务号：服务器配置（URL/Token/EncodingAESKey）与网页授权（AppID/Secret），登录与回调均走服务器
    """服务号服务器配置 Token（公众平台 开发-基本配置 中填写，与 GET 验证一致）"""
    wechat_oa_token: Optional[str] = None
    """服务号服务器配置 EncodingAESKey（明文模式可不参与解密）"""
    wechat_oa_encoding_aes_key: Optional[str] = None
    """服务号 AppID（网页授权与 code 换 token 必填，从公众平台 基本配置 获取）"""
    wechat_oa_app_id: Optional[str] = None
    """服务号 AppSecret（网页授权换 code 必填）"""
    wechat_oa_secret: Optional[str] = None
    """服务号回调与登录跳转根地址（无公网 IP 时填服务器地址，如 https://ts-api.fyshark.com）"""
    wechat_oa_base_url: Optional[str] = None
    capability_sutui_mcp_url: Optional[str] = None
    capability_upstream_urls_json: Optional[str] = None
    reddit_comment2video_backend_url: Optional[str] = None
    """认证中心（lobster_server）根地址，必填（无默认值）：发布/素材/对话等本机接口仅通过该地址 GET /auth/me 校验 token。"""
    auth_server_base: Optional[str] = None
    """与认证中心 LOBSTER_MCP_BILLING_INTERNAL_KEY 一致；本机转发 /capabilities/*、MCP 调认证中心（若有）时带 X-Lobster-Mcp-Billing。速推只走 mcp-gateway；media.edit 免费、comfly 在后端自扣费，均不在 MCP 侧走认证中心计费。"""
    lobster_mcp_billing_internal_key: Optional[str] = None
    """同 Bearer 在本进程内复用最近一次成功的 GET /auth/me 结果，减少并发与远端超时。秒；0=每次请求都拉远端（与旧行为一致）。"""
    auth_me_cache_ttl_seconds: int = 120
    """为 True 时：高消耗 invoke_capability 前需用户确认后再请求 MCP（环境变量 CHAT_REQUIRE_CAPABILITY_COST_CONFIRM）。"""
    chat_require_capability_cost_confirm: bool = True
    """为 True（默认）时：纯图/视频生成拿到终态 saved_assets 后尽早结束工具编排，减少多余 LLM 轮次；用户同句要求发布时仍会继续编排。"""
    lobster_chat_generation_early_finish: bool = True
    """纯生成提前结束时的助手可见回复：minimal=仅一句就绪确认（不含链接与 asset_id）；detailed=含 asset_id 与预览链接（旧行为）。"""
    lobster_chat_generation_reply_style: str = "minimal"
    """为 True 时在生成轮询结束后 SSE 推送「正在生成回复…」；默认 False（缩略图已展示时可关，避免状态栏重复打扰）。"""
    lobster_chat_sse_status_generating_reply: bool = False
    """Twilio Account SID（可选；本页保存优先于 .env）。"""
    twilio_account_sid: Optional[str] = None
    """Twilio 控制台 Auth Token，用于校验入站 WhatsApp/SMS Webhook 的 X-Twilio-Signature。"""
    twilio_auth_token: Optional[str] = None
    """与 Twilio Sandbox「When a message comes in」里填写的 URL 完全一致（含 https、域名、路径），用于签名校验。若留空则用 request.url（反代后常需显式填写）。"""
    twilio_whatsapp_webhook_full_url: Optional[str] = None
    """在线版：本机 twilio_whatsapp_config.json 与 .env 均无 SID/Token 时，将 /api/twilio-whatsapp/* 转发到海外 lobster_server。未设置且 lobster_edition=online 时默认 http://43.162.111.36；设为空字符串则关闭转发。"""
    twilio_remote_api_base: Optional[str] = None
    """创作者作品同步（Playwright）是否默认无头；与详情页「无头同步」一致，可由 perform_creator_content_sync(headless=) 覆盖。"""
    creator_sync_headless: bool = True
    """互亿无线短信 APIID"""
    ihuyi_sms_account: Optional[str] = None
    """互亿无线 APIKEY（对应 Submit.json 的 password）"""
    ihuyi_sms_password: Optional[str] = None
    """Comfly API 根。生成提示词用 OpenAI 兼容 chat（相对此根拼 chat/completions，多为 …/v1/chat/completions）；Veo 提交由 comfly_veo_submit_path 走 v2（默认 /v2/videos/generations）。mock：scripts/mock_comfly_server.py"""
    comfly_api_base: Optional[str] = None
    # comfly_api_key：不作为用户凭据兜底；爆款TVC / comfly.veo 仅使用技能商店 UserComflyConfig。
    comfly_api_key: Optional[str] = None
    """Chat 路径：以 / 开头则相对 API 主机根（默认 /v1/chat/completions，与 Comfly 文档一致）；否则拼在 comfly_api_base 后（如 chat/completions 用于 base 已含 /v1 的旧配置）"""
    comfly_chat_completions_path: str = "/v1/chat/completions"
    """提交图生视频 POST：以 / 开头则相对 API 主机根路径（如 /v2/videos/generations，与 Comfly 文档一致）；否则拼在 comfly_api_base 后（如 video/jobs 供旧版 mock）"""
    comfly_veo_submit_path: str = "/v2/videos/generations"
    """轮询视频任务 GET：以 / 开头则相对主机根（与 submit 同为 v2）；{task_id} 为 submit 返回的完整 id（含 video_ 前缀）"""
    comfly_veo_poll_path_template: str = "/v2/videos/generations/{task_id}"

    def cors_origins_list(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [x.strip() for x in self.cors_origins.split(",") if x.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

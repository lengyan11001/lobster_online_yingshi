"""Chat endpoint — direct LLM API with MCP tool-calling loop.

Primary flow:
  POST /chat → resolve model + API key → fetch MCP tools from local MCP server
  → call LLM with function definitions + messages → process tool_calls → loop
  → return final reply

POST /chat/stream: same but streams SSE progress (tool_start/tool_end) so the UI can show "thinking" steps.
Falls back to OpenClaw Gateway when no direct API config is available.

龙虾主对话 system 正文来自 openclaw/workspace/LOBSTER_CHAT_POLICY_INTRO.md 与
LOBSTER_CHAT_POLICY_TOOLS.md（与 OpenClaw 工作区 bootstrap 共用，单一事实来源）。
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import settings
from ..db import get_db
from .auth import get_current_user_for_chat, oauth2_scheme, _ServerUser
from ..models import CapabilityCallLog, ChatTurnLog, PublishAccount, ToolCallLog, User
from ..services.capability_cost_confirm import invoke_should_prompt_cost_confirm
from .mcp_gateway import set_mcp_token_for_agent

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent

logger = logging.getLogger(__name__)

_comfly_image_models_cache: list[str] = []
_comfly_image_models_ts: float = 0
_COMFLY_CACHE_TTL = 600  # 10 minutes


def _get_comfly_image_models() -> list[str]:
    """Fetch Comfly image model IDs from server /capabilities/comfly-pricing, cached with TTL."""
    global _comfly_image_models_cache, _comfly_image_models_ts
    now = time.time()
    if _comfly_image_models_cache and (now - _comfly_image_models_ts) < _COMFLY_CACHE_TTL:
        return _comfly_image_models_cache
    auth_base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not auth_base:
        return _comfly_image_models_cache
    try:
        import httpx as _hx
        r = _hx.get(f"{auth_base}/capabilities/comfly-pricing", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            models = data.get("models") or {}
            result = [k for k, v in models.items() if isinstance(v, dict) and v.get("api_format") == "dalle"]
            _comfly_image_models_cache = result
            _comfly_image_models_ts = now
            logger.debug("[CHAT] comfly image models refreshed: %s", result)
            return result
    except Exception as e:
        logger.debug("[CHAT] comfly-pricing fetch failed: %s", e)
    return _comfly_image_models_cache
router = APIRouter()

# chat/stream 与工具链遇 httpx 对端掐连接时的统一用户文案（避免界面直接显示英文 RemoteProtocolError）
_REMOTE_DISCONNECT_USER_MSG = (
    "上游连接被异常关闭（未返回完整 HTTP 响应），常见于网关或速推、OpenClaw、MCP 一侧重启、"
    "代理超时或网络抖动。若进度里已出现「✓ 素材已生成」，请到素材库用对应素材 ID 查看；"
    "也可刷新页面后续查或重新发送消息重试。"
)


def _friendly_chat_stream_exception(err: BaseException) -> str:
    """将 chat/stream 恢复轮询或主流程外层异常转成用户可读说明。"""
    if isinstance(err, httpx.RemoteProtocolError):
        return _REMOTE_DISCONNECT_USER_MSG
    raw = (str(err) if err is not None else "").strip()
    if "server disconnected without sending a response" in raw.lower():
        return _REMOTE_DISCONNECT_USER_MSG
    return raw or (type(err).__name__ if err is not None else "UnknownError")

# 与龙虾主对话 system 分离：app.log 曾记录模型在此场景下输出闲聊而非 JSON（因主 system 强调必须调工具）
_REVIEW_PROMPT_DRAFTS_SYSTEM = """你是「审核后发布」定时任务的提示词草稿生成器。
【硬性约束】禁止调用任何工具；禁止编造工具或 URL；禁止与用户闲聊、反问、请用户选择下一步或列举「我可以帮你」类选项。
【你只能输出】一个 Markdown 代码块：以 ```json 开头、以 ``` 结尾；代码块外不得有任何其它文字。
【代码块内】仅一个 JSON 数组；每项为对象，必须含 "prompt" 字符串字段（后续将把该字符串作为 POST /chat 的用户消息）。"""

MAX_HISTORY = 20
MAX_TOOL_ROUNDS = 8
MAX_TOOL_ROUNDS_ORCHESTRATION = 16
_schedule_orchestration_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_schedule_orchestration_active", default=False
)
_cost_cancelled_caps_ctx: contextvars.ContextVar[set] = contextvars.ContextVar(
    "_cost_cancelled_caps_ctx", default=None
)
_review_prompt_drafts_only_active: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_review_prompt_drafts_only_active", default=False
)


def _effective_max_tool_rounds() -> int:
    return MAX_TOOL_ROUNDS_ORCHESTRATION if _schedule_orchestration_active.get() else MAX_TOOL_ROUNDS
# 单条历史消息最大字符数，避免长回复再次送入模型导致重复/延续上一条
MAX_HISTORY_MESSAGE_CHARS = 1200
MCP_URL = "http://127.0.0.1:8001/mcp"


class _SkipMcpToolCall(Exception):
    """部分工具在本地即可判定结果，无需请求 MCP。"""


_URL_RE = re.compile(r'https?://[^\s"\'<>\)\]]+', re.IGNORECASE)
_PUBLISH_INTENT_RE = re.compile(
    r"(发布|发到|发送到|发文|发帖|发视频|发图|post|publish|推送到|分享到|上传到|投稿)",
    re.IGNORECASE,
)
_pending_tool_logs: contextvars.ContextVar[List[Dict]] = contextvars.ContextVar("_pending_tool_logs", default=[])
_list_capabilities_cache: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_list_capabilities_cache", default=None)

# 对话轮内调用 MCP（如 publish_content）时附带 X-Chat-Model，供 /api/publish 走与会话一致的速推/直连模型生成文案
_mcp_forward_headers_ctx: contextvars.ContextVar[Optional[Dict[str, str]]] = contextvars.ContextVar(
    "mcp_forward_headers", default=None
)
# 单次 /chat 请求内，最近一次终态 task.get_result 解析出的成品视频 URL（publish_content 误填上游 video_id 时按 URL 反查素材库 asset_id）
_recent_task_video_urls_ctx: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "recent_task_video_urls", default=None
)
# publish_content 自动头条无图发文：供 _exec_tool 读取当前轮对话与附件上下文
_publish_autofill_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_publish_autofill_ctx", default=None
)
# 单次 /chat 请求内：最近 task.get_result / 生图终态解析出的素材 ID（publish_content 补全优先，避免只扫 messages 漏 URL）
_recent_publish_asset_hints_ctx: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar(
    "_recent_publish_asset_hints", default=None
)
# 单次 /chat 请求内：task_id -> 对应 video.generate/image.generate 的 payload（供 task.get_result 终态 SSE 填 prompt/model，前端 save-url 入库）
_generation_hints_by_task_id: contextvars.ContextVar[Optional[Dict[str, Dict[str, str]]]] = (
    contextvars.ContextVar("_generation_hints_by_task_id", default=None)
)
# 编排报告：orchestration_report=True 时收集本轮所有工具执行摘要
_orchestration_tool_log: contextvars.ContextVar[Optional[List[Dict[str, Any]]]] = contextvars.ContextVar(
    "_orchestration_tool_log", default=None
)


def _orch_log_tool(name: str, args: Dict, success: bool, result_preview: str) -> None:
    """向编排报告追加一条工具执行记录。"""
    log = _orchestration_tool_log.get()
    if log is None:
        return
    entry: Dict[str, Any] = {
        "tool_name": name,
        "success": success,
        "result_preview": (result_preview or "")[:600],
    }
    if name == "invoke_capability":
        entry["capability_id"] = (args.get("capability_id") or "").strip()
    if name == "publish_content":
        entry["account_id"] = args.get("account_id") or args.get("account_nickname") or ""
    log.append(entry)


def _build_orchestration_report() -> Dict[str, Any]:
    """从当前请求的 context vars 构建编排报告。"""
    tools = _orchestration_tool_log.get() or []
    hints = _recent_publish_asset_hints_ctx.get() or []
    publish_ok: Optional[bool] = None
    for t in tools:
        if t.get("tool_name") == "publish_content":
            if t.get("success") is True:
                publish_ok = True
            elif t.get("success") is False:
                if publish_ok is not True:
                    publish_ok = False
    return {
        "tools": tools,
        "publish_ok": publish_ok,
        "asset_id_hints": hints[:24],
    }


def _generation_hints_map() -> Dict[str, Dict[str, str]]:
    m = _generation_hints_by_task_id.get()
    if m is None:
        m = {}
        _generation_hints_by_task_id.set(m)
    return m


def _register_generation_hint_for_task(
    task_id: str, invoke_payload: Any, capability_id: str
) -> None:
    tid = (task_id or "").strip()
    if not tid or not isinstance(invoke_payload, dict):
        return
    prompt = str(invoke_payload.get("prompt") or "").strip()
    model = str(invoke_payload.get("model") or invoke_payload.get("model_id") or "").strip()
    _generation_hints_map()[tid] = {
        "prompt": prompt,
        "model": model,
        "capability_id": (capability_id or "").strip(),
    }


def _apply_generation_hints_to_saved_assets(saved: List[Dict[str, Any]], task_id: str) -> None:
    tid = (task_id or "").strip()
    if not tid or not saved:
        return
    h = _generation_hints_map().get(tid)
    if not h:
        return
    prompt = (h.get("prompt") or "").strip()
    model = (h.get("model") or "").strip()
    for it in saved:
        if not isinstance(it, dict):
            continue
        if prompt and not str(it.get("prompt") or "").strip():
            it["prompt"] = prompt[:500]
        if model and not str(it.get("model") or "").strip():
            it["model"] = model[:128]
        if not str(it.get("generation_task_id") or "").strip():
            it["generation_task_id"] = tid[:128]


def _apply_invoke_payload_to_saved_assets(
    saved: List[Dict[str, Any]], invoke_args: Dict[str, Any]
) -> None:
    """image.generate 等同轮已终态时，无 task_id 映射也可用本轮 payload 填 prompt/model。"""
    if not saved or not isinstance(invoke_args, dict):
        return
    pl = invoke_args.get("payload")
    if not isinstance(pl, dict):
        return
    prompt = str(pl.get("prompt") or "").strip()
    model = str(pl.get("model") or pl.get("model_id") or "").strip()
    for it in saved:
        if not isinstance(it, dict):
            continue
        if prompt and not str(it.get("prompt") or "").strip():
            it["prompt"] = prompt[:500]
        if model and not str(it.get("model") or "").strip():
            it["model"] = model[:128]


def _terminal_saved_assets_for_task_result(result_text: str) -> List[Dict[str, Any]]:
    """task.get_result 终态：优先 MCP saved_assets，否则扫视频 URL，再尝试扫图 URL。"""
    clean = (result_text or "").strip()
    if "\n\n[SYSTEM]" in clean:
        clean = clean[: clean.index("\n\n[SYSTEM]")].strip()
    saved = _extract_saved_assets_from_task_result(clean)
    if saved:
        return saved
    v = _extract_video_urls_from_task_result(clean)
    if v:
        return v
    return _extract_image_urls_from_generate_result(clean)


def _merge_publish_asset_hints(new_ids: List[str]) -> None:
    """把本次工具解析到的素材 ID 插到本轮候选前列，供 publish_content 漏传 asset_id 时补全。"""
    clean = [x.strip() for x in new_ids if isinstance(x, str) and x.strip()]
    if not clean:
        return
    cur = _recent_publish_asset_hints_ctx.get()
    if cur is None:
        cur = []
    merged: List[str] = []
    seen: set = set()
    for h in clean + cur:
        if h in seen:
            continue
        seen.add(h)
        merged.append(h)
        if len(merged) >= 24:
            break
    _recent_publish_asset_hints_ctx.set(merged)


def _enrich_saved_assets_asset_ids_from_db(
    saved: List[Dict[str, Any]],
    db: Session,
    user_id: int,
) -> None:
    """终态 saved_assets 仅有 URL 时，按 save_url_dedupe 反查库内 asset_id（与 save-url 一致）。"""
    from .assets import _find_existing_asset_by_save_url_dedupe, _save_url_dedupe_key

    for it in saved:
        if not isinstance(it, dict):
            continue
        if (it.get("asset_id") or "").strip():
            continue
        for k in ("url", "source_url", "image_url", "video_url", "preview_url", "file_url"):
            u = (it.get(k) or "").strip()
            if not u.startswith("http"):
                continue
            try:
                u0 = u.split("?")[0].split("#")[0]
                hit = _find_existing_asset_by_save_url_dedupe(db, user_id, _save_url_dedupe_key(u0))
                if hit:
                    it["asset_id"] = hit.asset_id
                    break
            except Exception:
                continue


def _note_publish_candidate_asset_ids_from_task_result(
    result_text: str,
    db: Optional[Session],
    user_id: Optional[int],
) -> None:
    """task.get_result 终态：从 MCP 正文提取 asset_id / URL，写入本轮发布候选（不依赖 messages 里是否已出现 tool 条）。"""
    if not (result_text or "").strip() or db is None or user_id is None:
        return
    from ..models import Asset
    from .assets import _find_existing_asset_by_save_url_dedupe, _save_url_dedupe_key

    new_ids: List[str] = []
    saved = _extract_saved_assets_from_task_result(result_text)
    for it in saved:
        if not isinstance(it, dict):
            continue
        aid = (it.get("asset_id") or "").strip()
        if aid:
            new_ids.append(aid)
            continue
        for k in ("url", "source_url", "image_url", "video_url", "preview_url", "file_url"):
            u = (it.get(k) or "").strip()
            if not u.startswith("http"):
                continue
            try:
                u0 = u.split("?")[0].split("#")[0]
                hit = _find_existing_asset_by_save_url_dedupe(db, int(user_id), _save_url_dedupe_key(u0))
                if hit:
                    new_ids.append(hit.asset_id)
            except Exception:
                continue
    if not new_ids:
        for m in re.finditer(r'"asset_id"\s*:\s*"([a-zA-Z0-9]{10,64})"', result_text):
            cand = m.group(1).strip()
            if (
                cand
                and db.query(Asset)
                .filter(Asset.user_id == int(user_id), Asset.asset_id == cand)
                .first()
            ):
                new_ids.append(cand)
    if not new_ids:
        for m in _URL_RE.finditer(result_text):
            u = m.group(0).rstrip(".,;:!?)」』\"'")
            if not u.startswith("http"):
                continue
            try:
                u0 = u.split("?")[0].split("#")[0]
                hit = _find_existing_asset_by_save_url_dedupe(db, int(user_id), _save_url_dedupe_key(u0))
                if hit:
                    new_ids.append(hit.asset_id)
            except Exception:
                continue
    validated: List[str] = []
    seen: set = set()
    for aid in new_ids:
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if db.query(Asset).filter(Asset.user_id == int(user_id), Asset.asset_id == aid).first():
            validated.append(aid)
    if validated:
        _merge_publish_asset_hints(validated)
        logger.info(
            "[CHAT] task.get_result 终态 publish 候选 asset_id 已更新（前几条）=%s",
            validated[:8],
        )


def _normalize_invoke_task_get_result_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """与 mcp/http_server 一致：task.get_result 只认 payload.task_id，合并 taskid/顶层误放。"""
    if not isinstance(args, dict):
        return args
    if (args.get("capability_id") or "").strip() != "task.get_result":
        return args
    payload = args.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    pl = dict(payload)
    tid = str(pl.get("task_id") or "").strip()
    if not tid:
        for k in ("taskId", "taskid", "TaskId"):
            v = pl.get(k)
            if v is not None and str(v).strip():
                tid = str(v).strip()
                break
    if not tid:
        for k in ("task_id", "taskId", "taskid"):
            v = args.get(k)
            if v is not None and str(v).strip():
                tid = str(v).strip()
                break
    if not tid:
        nested = pl.get("payload")
        if isinstance(nested, dict):
            tid = (
                (nested.get("task_id") or nested.get("taskId") or nested.get("taskid") or "")
                .strip()
            )
    if not tid:
        vid = args.get("id")
        if isinstance(vid, str) and vid.strip():
            s = vid.strip()
            if len(s) >= 16 or re.match(r"^[0-9a-fA-F-]{8,}$", s):
                tid = s
    if tid:
        pl["task_id"] = tid
    out = dict(args)
    out["payload"] = pl
    return out


_PROVIDERS: Dict[str, Dict[str, str]] = {
    "deepseek":  {"base_url": "https://api.deepseek.com",  "env": "DEEPSEEK_API_KEY"},
    "openai":    {"base_url": "https://api.openai.com",    "env": "OPENAI_API_KEY"},
    "anthropic": {"base_url": "https://api.anthropic.com", "env": "ANTHROPIC_API_KEY"},
    "google":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY"},
}

_NO_TOOL_SUPPORT = {"deepseek-reasoner", "o3-mini"}

# 在线版：避免模型把「配置个人速推 Token」当成对用户方案（算力在 server）
_ONLINE_SYS_PREFIX = (
    "【在线版 — 对用户说明口径】速推/xskill 算力由云端 lobster_server 统一配置（如 SUTUI_SERVER_TOKEN、MCP），"
    "用户不需要、也不应再去「系统配置」填写个人速推 Token。"
    "若 invoke_capability 等工具返回「Token 未配置」「403」「认证失败」或上游错误，请如实引用工具返回的原文，"
    "并说明需排查：服务器 MCP 与 Token、本机 AUTH_SERVER_BASE 与 mcp-gateway 链路、是否已用服务器账号登录；"
    "禁止把「请用户自行配置速推 Token」作为主要解决办法。"
    "若工具返回「算力不足」「余额不足」、HTTP 402 或预扣算力失败且原文明确为余额/算力问题，必须说明是账户算力不足、需充值或购买算力，"
    "禁止将原因概括为「服务器配置错误」「服务器设置问题」或暗示管理员改服务器配置。\n\n"
)

def _no_tools_sys_hint(edition: str) -> str:
    if (edition or "").strip().lower() == "online":
        return (
            "\n【当前无可用工具】能力服务(MCP 端口 8001)未就绪。"
            "若用户要求生成图片、视频等，请说明：需本机启动 MCP，且在线版依赖 AUTH_SERVER_BASE 与服务器 mcp-gateway、SUTUI_SERVER_TOKEN；"
            "不要建议用户去「系统配置」填写个人速推 Token。"
            "可访问 http://本机IP:8000/api/health 查看 mcp.reachable 与 mcp.tools_count。\n\n"
        )
    return (
        "\n【当前无可用工具】能力服务(MCP 端口 8001)未就绪。"
        "若用户要求生成图片、视频、发布等，请回复：当前无法使用速推能力，请确认 (1) 本机已启动 MCP（端口见配置）；"
        "(2) 单机版在「系统配置」中配置速推 Token 或 .env 中 CAPABILITY_SUTUI_MCP_URL。"
        "可访问 http://本机IP:8000/api/health 查看 mcp.reachable 与 mcp.tools_count。\n\n"
    )


_LOBSTER_CHAT_POLICY_DIR = _BASE_DIR / "openclaw" / "workspace"
_LOBSTER_CHAT_POLICY_INTRO_PATH = _LOBSTER_CHAT_POLICY_DIR / "LOBSTER_CHAT_POLICY_INTRO.md"
_LOBSTER_CHAT_POLICY_TOOLS_PATH = _LOBSTER_CHAT_POLICY_DIR / "LOBSTER_CHAT_POLICY_TOOLS.md"
_LOBSTER_CHAT_POLICY_CLOSING = (
    "回答使用中文，简洁友好。"
    " 当用户发送新的短消息（如问候、新问题）时，请直接针对该新消息简短回复，不要重复或延续上一条长回复的内容。"
)


def _load_lobster_chat_policy_intro(edition: str) -> str:
    """身份与能力总述；两段之间按版本注入在线版口径（与 openclaw/workspace/LOBSTER_CHAT_POLICY_INTRO.md 一致）。"""
    try:
        raw = _LOBSTER_CHAT_POLICY_INTRO_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "读取龙虾策略 INTRO 失败 %s，使用内置兜底: %s",
            _LOBSTER_CHAT_POLICY_INTRO_PATH,
            exc,
        )
        return (
            "你是「龙虾」(Lobster)，用户的私人 AI 助手。\n\n"
            + (_ONLINE_SYS_PREFIX if edition == "online" else "")
            + "龙虾内置了「速推 MCP」能力：文生图、图生视频、语音合成、视频解析等，具体以 list_capabilities 返回为准。\n\n"
        )
    if "\n\n" in raw:
        head, tail = raw.split("\n\n", 1)
        return (
            head.strip()
            + "\n\n"
            + (_ONLINE_SYS_PREFIX if edition == "online" else "")
            + tail.strip()
            + "\n\n"
        )
    return raw + "\n\n"


def _load_lobster_chat_policy_tools_body() -> str:
    """有 MCP 工具时的详策（与 LOBSTER_CHAT_POLICY_TOOLS.md 单一事实来源）。"""
    try:
        return _LOBSTER_CHAT_POLICY_TOOLS_PATH.read_text(encoding="utf-8").strip() + "\n\n"
    except OSError as exc:
        logger.error("读取龙虾策略 TOOLS 失败 %s: %s", _LOBSTER_CHAT_POLICY_TOOLS_PATH, exc)
        return ""


def _build_lobster_main_system_prompt(edition: str, has_tools: bool) -> str:
    """龙虾主对话 system（review_prompt_drafts_only / direct_llm 以外）。"""
    intro = _load_lobster_chat_policy_intro(edition)
    if has_tools:
        body = _load_lobster_chat_policy_tools_body()
        if not body.strip():
            logger.warning("LOBSTER_CHAT_POLICY_TOOLS.md 为空，降级为无工具提示")
            body = _no_tools_sys_hint(edition)
        comfly_models = _get_comfly_image_models()
        if comfly_models:
            body += (
                "【图片模型】用户指定模型时必须原样传入 payload.model。"
                "可用图片模型: fal-ai/flux-2/flash, " + ", ".join(comfly_models)
                + "。用户说用某模型就传该模型名，禁止替换为 default。\n"
            )
    else:
        body = _no_tools_sys_hint(edition)
    return intro + body + _LOBSTER_CHAT_POLICY_CLOSING


# POST /chat 且 direct_llm=true：不挂 MCP、不长 system，仅把 message（及 history）交给当前 LLM，适合关键字即问即答
_DIRECT_LLM_SYSTEM = (
    "【直答模式 — 必须遵守】你是中文助手。用户常只输入简短主题、公司/产品名、名词或一句话问题。\n"
    "你的回复必须包含**实质性正文**（至少数句或分点），给出解释、常见理解或合理推断；"
    "**禁止**整段回复只做下列之一：声称「没有足够信息」「无法确定」「缺乏公开资料」「建议用搜索引擎/访问官网」而**不写任何**常识性、背景性说明。\n"
    "若训练数据中名称不常见：仍须写「可能指……」「同名常见于……」并列出 2～3 条合理方向，再附一句「若指其他主体请以对方官方为准」。\n"
    "用户问「某公司是做什么的」：从行业惯例、常见产品形态作**常识性**概述，并标明「以下为一般性描述，非官方背书」。\n"
    "不要假装调用工具；不要编造已完成的生成/发布结果。\n"
    "仅当用户**明确**索要「今日新闻」「实时股价」等时效数据时，才可说明需实时检索。\n"
)

# 发往模型的用户轮次前缀（不改库里的 user_message 原文）；部分上游模型对用户指令权重更高
_DIRECT_LLM_USER_PREFIX = (
    "【请直接回答下面主题，勿整段拒答】至少写出几句实质内容；不要只用「不知道、去搜索」敷衍。\n\n"
)


def _wrap_last_user_for_direct_llm(messages: List[Dict[str, str]]) -> None:
    if not messages or messages[-1].get("role") != "user":
        return
    c = (messages[-1].get("content") or "").strip()
    if not c:
        return
    messages[-1]["content"] = _DIRECT_LLM_USER_PREFIX + c


# ── Pydantic models ───────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    content: str = Field(..., description="消息内容")


class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        description="当前用户输入。可选在句首使用 lobster_openclaw_chat_prefixes 中的前缀（如 /openclaw）"
        "以该轮优先走 OpenClaw（见 LOBSTER_OPENCLAW_CHAT_PREFIX_GATE）。",
    )
    history: Optional[List[ChatMessage]] = Field(default_factory=list)
    session_id: Optional[str] = None
    context_id: Optional[str] = None
    model: Optional[str] = None
    attachment_asset_ids: Optional[List[str]] = Field(default_factory=list, description="本条消息附带的素材 ID，将生成可访问 URL 供速推等使用")
    # 默认 False：智能会话等现有客户端不传该字段，行为与增加此字段前一致。
    review_prompt_drafts_only: bool = Field(
        False,
        description="审核发布「智能生成提示词」专用：禁用 MCP/工具与龙虾长 system，只产出 JSON 代码块",
    )
    direct_llm: bool = Field(
        False,
        description="为 True 时仅用极简 system，不挂 MCP 工具、不注入能力/模型清单；message 作关键字或完整问题，"
        "由当前所选模型（如在线 sutui/xxx）直接生成回复。与 review_prompt_drafts_only 互斥时请优先 review 语义。",
    )
    resume_task_poll_task_id: Optional[str] = Field(
        default=None,
        description="刷新后继续：仅对该 task_id 执行轮询（Comfly Veo 以 video_ 开头走 poll_video；否则走速推 task.get_result）。"
        "与正常对话互斥，传此字段时 message 可为占位短句。",
    )
    orchestration_report: bool = Field(
        False,
        description="True 时响应附加 orchestration 对象（tools/publish_ok/asset_id_hints），供定时编排判断成功。",
    )
    schedule_orchestration: bool = Field(
        False,
        description="True 时加长工具轮次上限、模型偏向 tool_calls 遵从率更高的备选。",
    )


class ChatResponse(BaseModel):
    reply: str
    orchestration: Optional[Dict[str, Any]] = None


# ── API key / provider resolution ─────────────────────────────────

def _all_api_keys() -> Dict[str, str]:
    """Merge keys from openclaw.json literal values and openclaw/.env."""
    keys: Dict[str, str] = {}
    try:
        p = _BASE_DIR / "openclaw" / "openclaw.json"
        if p.exists():
            text = p.read_text(encoding="utf-8")
            for pid, pd in json.loads(text).get("models", {}).get("providers", {}).items():
                if isinstance(pd, dict):
                    k = (pd.get("apiKey") or "").strip()
                    if k and not k.startswith("${"):
                        env_name = _PROVIDERS.get(pid, {}).get("env", "")
                        if env_name:
                            keys[env_name] = k
    except Exception:
        pass
    try:
        ep = _BASE_DIR / "openclaw" / ".env"
        if ep.exists():
            for line in ep.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    if v.strip():
                        keys[k.strip()] = v.strip()
    except Exception:
        pass
    return keys


def _resolve_config(model: str) -> Optional[Dict[str, Any]]:
    """Return {base_url, api_key, model_name, provider} or None."""
    if "/" not in model:
        return None
    provider, model_name = model.split("/", 1)
    pcfg = _PROVIDERS.get(provider)
    if not pcfg:
        return None
    api_key = _all_api_keys().get(pcfg["env"], "")
    if not api_key:
        return None
    return {
        "base_url": pcfg["base_url"],
        "api_key": api_key,
        "model_name": model_name,
        "provider": provider,
    }


def _online_resolve_cfg_and_overrides(
    payload: ChatRequest,
    raw_token: str,
    *,
    schedule_orchestration: bool = False,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, str]]]:
    """在线版：对话固定走认证中心 /api/sutui-chat/completions，模型由 lobster_default_sutui_chat_model（默认 deepseek-chat）决定；忽略前端 model 与直连 Key。
    schedule_orchestration=True 时优先使用 lobster_orchestration_sutui_chat_model（tool_calls 遵从率更高的模型）。
    """
    asb = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not asb:
        raise HTTPException(status_code=503, detail="未配置 AUTH_SERVER_BASE，无法使用速推聚合对话")
    if schedule_orchestration:
        orch_model = (getattr(settings, "lobster_orchestration_sutui_chat_model", None) or "").strip()
        if orch_model:
            inner = orch_model
        else:
            inner = (
                (getattr(settings, "lobster_default_sutui_chat_model", None) or "deepseek-chat").strip()
                or "deepseek-chat"
            )
    else:
        inner = (
            (getattr(settings, "lobster_default_sutui_chat_model", None) or "deepseek-chat").strip()
            or "deepseek-chat"
        )
    req_model = f"sutui/{inner}"
    cfg: Dict[str, Any] = {
        "base_url": "",
        "api_key": "",
        "model_name": inner,
        "provider": "sutui",
    }
    return (
        req_model,
        cfg,
        f"{asb}/api/sutui-chat/completions",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {raw_token}",
        },
    )


def _pick_default_model() -> str:
    """Return the first model that has a configured API key."""
    try:
        p = _BASE_DIR / "openclaw" / "openclaw.json"
        if p.exists():
            primary = json.loads(p.read_text(encoding="utf-8")).get(
                "agents", {}
            ).get("defaults", {}).get("model", {}).get("primary", "")
            if primary and _resolve_config(primary):
                return primary
    except Exception:
        pass
    for first in ["deepseek/deepseek-chat", "openai/gpt-4o",
                   "anthropic/claude-sonnet-4-5", "google/gemini-2.5-pro"]:
        if _resolve_config(first):
            return first
    raise HTTPException(
        400,
        detail="未配置任何 LLM API Key，请到「系统配置」页面添加至少一个模型的 API Key（如 DeepSeek、OpenAI 等）",
    )


# ── MCP tool helpers ──────────────────────────────────────────────

async def _fetch_mcp_tools(raw_token: Optional[str] = None) -> List[Dict]:
    """Fetch available tools from the local MCP server (port 8001)。传入用户 JWT 以便 MCP 对调试中技能过滤 list_capabilities / invoke 枚举。"""
    try:
        headers = {}
        t = (raw_token or "").strip()
        if t:
            headers["Authorization"] = f"Bearer {t}" if not t.lower().startswith("bearer ") else t
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as c:
            r = await c.post(
                MCP_URL,
                json={"jsonrpc": "2.0", "id": "lt", "method": "tools/list", "params": {}},
                headers=headers,
            )
        tools = r.json().get("result", {}).get("tools", [])
        logger.info("[对话] MCP tools/list 成功 tools_count=%s", len(tools))
        return tools
    except Exception as e:
        logger.warning("[对话] MCP tools/list 失败: %s", e)
        return []


async def get_reply_for_channel(
    user_message: str,
    session_id: str = "",
    system_prompt_extra: str = "",
) -> str:
    """供企业微信/抖音等渠道回调使用：仅文本入、文本出。优先直连 LLM，无配置时走 OpenClaw，保证本地盒子仅配 OpenClaw 也能回复。"""
    if not (user_message or "").strip():
        return "收到。"
    model = ""
    try:
        model = _pick_default_model()
    except HTTPException:
        model = "openclaw"
    cfg = _resolve_config(model) if model else None
    sys = (
        "你是智能客服助手。根据用户消息简短、友好地回复；勿假定用户当前使用的是某一特定聊天软件（除非用户主动提到）。使用中文。"
        + (("\n" + system_prompt_extra.strip()) if system_prompt_extra else "")
    )
    messages: List[Dict[str, str]] = [
        {"role": "system", "content": sys},
        {"role": "user", "content": (user_message or "").strip()},
    ]
    if cfg:
        try:
            reply = await _chat_openai(messages, cfg, [], "", sutui_token=None)
            return (reply or "").strip() or "收到。"
        except HTTPException:
            return "服务暂时不可用，请稍后再试。"
        except Exception as e:
            logger.exception("[渠道回复] chat 异常: %s", e)
            return "处理时遇到问题，请稍后再试。"
    oc_reply = await _try_openclaw(messages, model or "openclaw", "")
    if oc_reply:
        return (oc_reply or "").strip() or "收到。"
    return "抱歉，当前未配置对话模型或 OpenClaw，无法回复。"


async def get_customer_service_reply(
    user_message: str,
    company_info: str = "",
    product_intro: str = "",
    common_phrases: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """客服专用：仅根据提供的公司信息、产品介绍、常用话术回复；匹配不到则只做简短闲聊，严禁编造。"""
    if not (user_message or "").strip():
        return "收到。"
    materials = []
    if (company_info or "").strip():
        materials.append("【公司信息】\n" + company_info.strip())
    if (product_intro or "").strip():
        materials.append("【产品介绍】\n" + product_intro.strip())
    if (common_phrases or "").strip():
        materials.append("【常用话术】\n" + common_phrases.strip())
    materials_text = "\n\n".join(materials) if materials else "（暂无资料）"
    sys = (
        "你是智能客服助手。你必须严格遵守以下规则：\n"
        "1. 仅根据下面「公司信息」「产品介绍」「常用话术」回答与公司、产品相关的问题；勿在回复中自称或强调「企业微信」「WhatsApp」等具体渠道，除非资料里明确写了该渠道。\n"
        "2. 若用户问题无法从上述资料中匹配到任何内容，只可做简短、友好的闲聊（如问候、感谢、请稍候联系人工），严禁编造公司名、产品名、价格、规格等任何未在资料中出现的信息。\n"
        "3. 回复简短、使用中文。\n\n"
        + materials_text
    )
    messages: List[Dict[str, str]] = [{"role": "system", "content": sys}]
    if history:
        for h in history[-10:]:
            if isinstance(h, dict) and h.get("role") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:800]})
    messages.append({"role": "user", "content": (user_message or "").strip()})
    model = ""
    try:
        model = _pick_default_model()
    except HTTPException:
        model = "openclaw"
    cfg = _resolve_config(model) if model else None
    _ERROR_PATTERNS = re.compile(
        r"API rate limit|rate.limit.reached|internal server error|service unavailable|"
        r"quota exceeded|token limit|billing|insufficient.credits|"
        r"⚠️.*rate.limit|⚠️.*error|503|429|too many requests",
        re.IGNORECASE,
    )
    _FALLBACK = "您好，客服正忙，请稍后再联系我们。"

    if cfg:
        try:
            reply = await _chat_openai(messages, cfg, [], "", sutui_token=None)
            reply = (reply or "").strip() or "收到。"
            if _ERROR_PATTERNS.search(reply):
                logger.warning("[客服回复] AI 返回疑似错误信息，已过滤: %s", reply[:200])
                return _FALLBACK
            return reply
        except HTTPException:
            return _FALLBACK
        except Exception as e:
            logger.exception("[客服回复] chat 异常: %s", e)
            return _FALLBACK
    oc_reply = await _try_openclaw(messages, model or "openclaw", "")
    if oc_reply:
        oc_reply = (oc_reply or "").strip() or "收到。"
        if _ERROR_PATTERNS.search(oc_reply):
            logger.warning("[客服回复] OpenClaw 返回疑似错误信息，已过滤: %s", oc_reply[:200])
            return _FALLBACK
        return oc_reply
    return "抱歉，当前未配置对话模型，无法回复。"


def _last_user_content(cur: List[Dict]) -> str:
    """从对话列表中取最后一条**真实**用户消息的文本（跳过「工具调用结果」等合成 user 条）。"""
    _skip_prefixes = (
        "工具调用结果:",
        "你刚才只回复了文字",
        "请立即调用对应的工具来执行操作",
    )

    def _txt_from_content(c: Any) -> str:
        if isinstance(c, str):
            return (c or "").strip()
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    return (p.get("text") or "").strip()
        return ""

    for m in reversed(cur):
        if m.get("role") != "user":
            continue
        raw = _txt_from_content(m.get("content"))
        if not raw:
            continue
        if any(raw.startswith(p) for p in _skip_prefixes):
            continue
        return raw
    return ""


def _user_text_requests_publish(text: str) -> bool:
    """用户一句里若同时要求「生成并发布」，不得提前结束工具编排。"""
    s = (text or "").strip()
    if not s:
        return False
    keywords = ("发布", "投稿")
    for kw in keywords:
        if kw in s:
            logger.info("[CHAT] publish_keyword 命中: 「%s」 in text=%s", kw, s[:120])
            return True
    platforms = (
        "抖音", "快手", "小红书", "b站", "B站", "视频号", "微博", "tiktok", "TikTok",
        "youtube", "YouTube", "instagram", "Instagram",
    )
    for p in platforms:
        if p in s:
            logger.info("[CHAT] publish_platform 命中: 「%s」 in text=%s", p, s[:120])
            return True
    return False


def _openai_tool_call_requests_publish(fn_name: str, args: Dict[str, Any]) -> bool:
    n = (fn_name or "").strip()
    if n == "publish_content":
        return True
    if n != "invoke_capability":
        return False
    cap = (args.get("capability_id") or "").strip().lower()
    if "publish" in cap:
        return True
    return False


def _openai_round_has_publish_intent(tcs: List[Dict[str, Any]], last_user_content: str) -> bool:
    if _user_text_requests_publish(last_user_content):
        return True
    for tc in tcs or []:
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        try:
            a = json.loads(fn.get("arguments") or "{}")
        except Exception:
            a = {}
        if not isinstance(a, dict):
            a = {}
        if _openai_tool_call_requests_publish(name, a):
            return True
    return False


def _lobster_chat_generation_early_finish_enabled() -> bool:
    return bool(getattr(settings, "lobster_chat_generation_early_finish", True))


def _lobster_chat_generation_reply_style() -> str:
    s = (getattr(settings, "lobster_chat_generation_reply_style", None) or "minimal").strip().lower()
    return s if s in ("minimal", "detailed") else "minimal"


def _lobster_chat_generation_reply_minimal() -> bool:
    return _lobster_chat_generation_reply_style() == "minimal"


def _lobster_chat_sse_emit_generating_reply_status() -> bool:
    return bool(getattr(settings, "lobster_chat_sse_status_generating_reply", False))


async def _maybe_progress_status_generating_reply(
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
) -> None:
    if not progress_cb or not _lobster_chat_sse_emit_generating_reply_status():
        return
    try:
        await progress_cb({"type": "status", "message": "正在生成回复…"})
    except Exception:
        pass


def _early_finish_generation_user_reply(kind_cn: str) -> str:
    """纯生成提前结束时返回给前端的助手文案（不含链接，缩略图由 tool_end/saved_assets 展示）。"""
    if _lobster_chat_generation_reply_minimal():
        return "已完成。"
    return f"{kind_cn}已生成并入库。"


def _correct_video_to_image_if_user_asked_image(args: Dict, last_user_content: str) -> Dict:
    """模型误将图片需求选成 video.generate 时，按用户最后一条消息纠正为 image.generate，并清理 payload 仅保留图片参数（避免速推按视频处理）。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return args
    text = (last_user_content or "").strip()
    if not text:
        return args
    image_keywords = ("图片", "图", "画一张", "生成图", "一张图", "画一只", "画个", "生成一张", "来张图", "来一张", "一只猫的图", "猫的图片")
    video_keywords = ("视频", "动图", "生成视频", "做视频", "做个视频")
    has_image = any(k in text for k in image_keywords)
    has_video = any(k in text for k in video_keywords)
    if not has_image or has_video:
        return args
    args = dict(args)
    args["capability_id"] = "image.generate"
    old_payload = args.get("payload") or {}
    if not isinstance(old_payload, dict):
        old_payload = {}
    # 只保留图片能力需要的参数，去掉视频专用字段，避免传给 MCP/速推时仍被当视频
    model = (old_payload.get("model") or "").strip()
    if not model or "super-seed" in model or "st-ai" in model or "wan/" in model or "vidu" in model or "seedance" in model or "hailuo" in model or "minimax" in model:
        # 默认走 Fal 文生图，不依赖速推侧「即梦」Token 池（jimeng-* 需上游单独开通）
        model = "fal-ai/flux-2/flash"
    payload = {
        "prompt": (old_payload.get("prompt") or "").strip(),
        "model": model,
    }
    if (old_payload.get("image_url") or "").strip():
        payload["image_url"] = (old_payload.get("image_url") or "").strip()
    if (old_payload.get("image_size") or "").strip():
        payload["image_size"] = (old_payload.get("image_size") or "").strip()
    args["payload"] = payload
    logger.info("[CHAT] 根据用户意图将 video.generate 纠正为 image.generate，并已清理 payload 为仅图片参数")
    return args


_VIDEO_ONLY_MODEL_HINTS = ("veo", "sora", "seedance", "hailuo", "minimax", "wan/", "wan2", "kling", "grok", "vidu")


def _detect_model_capability_mismatch(args: Dict) -> Optional[str]:
    """检测 model 与 capability 类型不匹配，返回错误提示；匹配则返回 None。"""
    if not args:
        return None
    cap = (args.get("capability_id") or "").strip()
    inner = args.get("payload")
    if not isinstance(inner, dict):
        return None
    raw_model = (inner.get("model") or inner.get("model_id") or "").strip()
    if not raw_model:
        return None
    low = raw_model.lower()
    if cap == "image.generate":
        is_video = _is_known_video_model_without_slash(raw_model) or any(h in low for h in _VIDEO_ONLY_MODEL_HINTS)
        if is_video:
            return f"模型「{raw_model}」是视频生成模型，不能用于图片生成（image.generate）。请改用 video.generate 或指定图片模型。"
    return None


_MAX_VIDEO_IMAGE_ATTACHMENTS = 9

# 本机签名直链（已禁止新上传产生；旧数据命中则拒绝对话继续）
_LOCAL_SIGNED_ASSET_PATH = "/api/assets/file/"


def _resolve_attachment_urls_strict(
    attachment_asset_ids: Optional[List[str]],
    request: Optional[Request],
    db,
    user_id: int,
) -> List[str]:
    """附图仅允许 DB 中 source_url 经 get_asset_public_url 判定为公网（TOS 或 upload-temp）。
    无公网 URL 或仍为 /api/assets/file/ 签名链时直接 HTTPException，不进入 LLM/MCP。"""
    if not attachment_asset_ids or not request:
        return []
    aids = [a.strip() for a in (attachment_asset_ids or [])[: _MAX_VIDEO_IMAGE_ATTACHMENTS] if isinstance(a, str) and a.strip()]
    if not aids:
        return []
    from .assets import get_asset_public_url

    asb = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/") or "（未配置 AUTH_SERVER_BASE）"
    out: List[str] = []
    for aid in aids:
        logger.info("[使用素材-步骤B.1] 开始处理素材 asset_id=%s", aid)
        u = get_asset_public_url(aid, user_id, request, db)
        if not u:
            logger.error(
                "[使用素材-失败] asset_id=%s get_asset_public_url=None（无 TOS/upload-temp 公网 source_url 或仅内部地址），中止对话",
                aid,
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"素材 {aid} 没有公网可访问链接。上传时本机 TOS 与服务器 /api/assets/upload-temp 须至少成功其一；"
                    f"请配置 custom_configs.json 的 TOS_CONFIG 或检查认证中心 {asb} 与登录态后重新上传。"
                ),
            )
        if _LOCAL_SIGNED_ASSET_PATH in u:
            logger.error("[使用素材-失败] asset_id=%s 命中已禁止的签名直链 /api/assets/file/", aid)
            raise HTTPException(
                status_code=400,
                detail=f"素材 {aid} 为旧版签名链接，已不可用。请重新上传（TOS 或服务器临时上传）。",
            )
        logger.info("[使用素材-步骤B.2] 公网 URL 已解析 asset_id=%s url=%s", aid, u[:80])
        out.append(u)
    logger.info("[使用素材-步骤B.5] 附图公网 URL 齐全 count=%d asset_ids=%s", len(out), aids)
    return out


def _get_attachment_public_urls(
    attachment_asset_ids: Optional[List[str]],
    request: Optional[Request],
    db=None,
    user_id: Optional[int] = None,
) -> List[str]:
    """返回本条消息附图公网 URL（供图生视频注入）。无公网 URL 时抛 HTTPException，不再使用 build_asset_file_url。"""
    if not attachment_asset_ids or not request or db is None or user_id is None:
        return []
    return _resolve_attachment_urls_strict(attachment_asset_ids, request, db, user_id)


def _ensure_daihuo_pipeline_asset_or_url(
    args: Dict[str, Any],
    attachment_asset_ids: Optional[List[str]],
    attachment_urls: List[str],
) -> None:
    """爆款 TVC 整包：模型常漏传 asset_id/image_url；本条消息有附图时自动补全（与 video.generate 注入一致）。"""
    if not args or (args.get("capability_id") or "").strip() != "comfly.veo.daihuo_pipeline":
        return
    raw_pl = args.get("payload")
    pl: Dict[str, Any] = dict(raw_pl) if isinstance(raw_pl, dict) else {}
    nested = pl.get("payload")
    if isinstance(nested, dict) and (
        (nested.get("action") or "").strip()
        or (nested.get("job_id") or "").strip()
        or nested.get("asset_id") is not None
        or nested.get("image_url") is not None
    ):
        base = {k: v for k, v in pl.items() if k != "payload"}
        pl = {**base, **nested}
    if not (pl.get("action") or "").strip():
        top_act = (args.get("action") or "").strip()
        if top_act:
            pl["action"] = top_act
    for k in (
        "job_id",
        "asset_id",
        "image_url",
        "merge_clips",
        "storyboard_count",
        "auto_save",
        "platform",
        "country",
        "language",
        "output_dir",
        "isolate_job_dir",
        "image_request_style",
    ):
        if k in args and args[k] is not None:
            pl.setdefault(k, args[k])
    aid = (str(pl.get("asset_id") or "").strip())
    iu = (str(pl.get("image_url") or "").strip())
    if aid or (iu.startswith("http://") or iu.startswith("https://")):
        args["payload"] = pl
        return
    ids = [a.strip() for a in (attachment_asset_ids or []) if isinstance(a, str) and a.strip()]
    if ids:
        pl["asset_id"] = ids[0]
        logger.info(
            "[CHAT] daihuo_pipeline 未传 asset_id/image_url，已注入本条附图 asset_id=%s",
            ids[0],
        )
    elif attachment_urls:
        pl["image_url"] = attachment_urls[0]
        logger.info(
            "[CHAT] daihuo_pipeline 未传 asset_id/image_url，已注入本条附图 image_url 前缀=%s",
            (attachment_urls[0] or "")[:72],
        )
    args["payload"] = pl


def _inject_video_media_urls(args: Dict[str, Any], attachment_urls: List[str]) -> None:
    """video.generate 时：若有本条消息附图的公网 URL，一律用其覆盖 payload 的 media_files/image_url/filePaths，确保速推拿到正确可拉取 URL。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    if not attachment_urls:
        if not inner.get("media_files") and not inner.get("image_url") and not inner.get("filePaths"):
            logger.warning(
                "[CHAT] 图生视频未注入链接：附图为 0 个公网 URL；若用户已附图应已在对话入口被 400 拦截，此处多为文生视频或模型未带图"
            )
        return
    urls = list(attachment_urls)
    inner["filePaths"] = urls
    inner["functionMode"] = "first_last_frames"
    inner["media_files"] = urls
    inner["image_url"] = urls[0]
    existing = (inner.get("prompt") or "").strip()
    if "图生视频" not in existing:
        inner["prompt"] = ("图生视频：" + existing) if existing else "图生视频"
    logger.info("[CHAT] 图生视频注入 filePaths（%d 张）functionMode=first_last_frames", len(urls))


# 用户常在正文写「veo3.1」，但 LLM 的 tool JSON 漏传 payload.model（上游即报 generate 缺少 model；见 mcp.log）
_VEO_31_IN_USER_TEXT = re.compile(r"(?:veo|ve)\s*3[\._]?\s*1", re.IGNORECASE)
# 用户直接写速推完整 model id（与 tasks/create 一致），优先于口语「veo3.1」推断
_VEO_FULL_MODEL_SUBSTR: Tuple[Tuple[str, str], ...] = (
    ("fal-ai/veo3.1/fast/image-to-video", "fal-ai/veo3.1/fast/image-to-video"),
    ("fal-ai/veo3.1/image-to-video", "fal-ai/veo3.1/image-to-video"),
    ("fal-ai/veo3.1/fast", "fal-ai/veo3.1/fast"),
    ("fal-ai/veo3.1", "fal-ai/veo3.1"),
)


def _infer_video_model_from_user_text(
    args: Dict[str, Any],
    last_user_content: str,
    has_attachment: bool,
) -> None:
    """video.generate 且 payload 无 model 时，从用户最后一条消息补全（完整 id 优先，其次 Veo 3.1 口语）。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    if (inner.get("model") or "").strip():
        return
    text = (last_user_content or "").strip()
    if not text:
        return
    low = text.lower()
    # 长串先匹配，避免 fal-ai/veo3.1 误吞 image-to-video
    for needle, canonical in _VEO_FULL_MODEL_SUBSTR:
        if needle in low:
            inner["model"] = canonical
            logger.info(
                "[CHAT] 用户正文含完整 Veo model「%s」但 payload 无 model，已补全 model=%s",
                needle,
                inner["model"],
            )
            return
    if _VEO_31_IN_USER_TEXT.search(text):
        inner["model"] = (
            "fal-ai/veo3.1/image-to-video" if has_attachment else "fal-ai/veo3.1"
        )
        logger.info(
            "[CHAT] 用户正文含 Veo 3.1 但 payload 无 model，已补全 model=%s",
            inner["model"],
        )


_DEFAULT_IMAGE_GENERATE_MODEL = "fal-ai/flux-2/flash"
_DEFAULT_VIDEO_GENERATE_MODEL_T2V = "sora2pub/text-to-video"
_DEFAULT_VIDEO_GENERATE_MODEL_I2V = "sora2pub/image-to-video"

_IMAGE_MODEL_ALIASES: Dict[str, str] = {
    "flux": "fal-ai/flux-2/flash",
    "flux2": "fal-ai/flux-2/flash",
    "flux-2": "fal-ai/flux-2/flash",
    "flux2-flash": "fal-ai/flux-2/flash",
    "flux-2-flash": "fal-ai/flux-2/flash",
    "seedream": "fal-ai/bytedance/seedream/v4.5/text-to-image",
    "seedream-4.5": "fal-ai/bytedance/seedream/v4.5/text-to-image",
    "seedream-5": "fal-ai/bytedance/seedream/v5/lite/text-to-image",
    "nano-banana": "fal-ai/nano-banana-pro",
    "nano-banana-pro": "fal-ai/nano-banana-pro",
    "banana": "fal-ai/nano-banana-pro",
    "gemini": "kapon/gemini-3-pro-image-preview",
    "gemini-image": "kapon/gemini-3-pro-image-preview",
}

_VIDEO_MODEL_ALIASES: Dict[str, str] = {
    "sora": "sora2pub/text-to-video",
    "sora2": "sora2pub/text-to-video",
    "sora-2": "sora2pub/text-to-video",
    "seedance": "ark/seedance-2.0",
    "seedance-2": "ark/seedance-2.0",
    "seedance-2.0": "ark/seedance-2.0",
    "veo": "fal-ai/veo3.1",
    "veo3": "fal-ai/veo3.1",
    "veo3.1": "fal-ai/veo3.1",
    "hailuo": "fal-ai/minimax/hailuo-2.3/standard/text-to-video",
    "kling": "fal-ai/kling-video/v3/standard/text-to-video",
    "wan": "wan/v2.6/text-to-video",
    "wan-2.6": "wan/v2.6/text-to-video",
}


def _normalize_model_alias(args: Dict[str, Any]) -> None:
    """将 LLM 传入的简化模型名映射为 API 需要的完整 model ID。"""
    if not args:
        return
    cap = (args.get("capability_id") or "").strip()
    inner = args.get("payload")
    if not isinstance(inner, dict):
        return
    raw = (inner.get("model") or inner.get("model_id") or "").strip()
    if not raw:
        return
    low = raw.lower()
    aliases = _IMAGE_MODEL_ALIASES if cap == "image.generate" else _VIDEO_MODEL_ALIASES if cap == "video.generate" else {}
    canonical = aliases.get(low)
    if canonical:
        logger.info("[CHAT] 模型别名映射 %s → %s", raw, canonical)
        inner["model"] = canonical
# 用户只说「配图 / 发头条」但漏传 payload.prompt 时，用正文兜底，避免上游「prompt 不能为空」
_MAX_IMAGE_GENERATE_PROMPT_CHARS = 4000
_IMAGE_16_9_RE = re.compile(r"16\s*[:：]\s*9")


_DEFAULT_VIDEO_GENERATE_DURATION = 5

def _is_known_video_model_without_slash(model_id: str) -> bool:
    """model_id 不含 / 但是已知的合法视频模型名（别名表或常见模式匹配）。"""
    if not model_id:
        return False
    low = model_id.lower()
    if low in _VIDEO_MODEL_ALIASES:
        return True
    _KNOWN_VIDEO_PATTERNS = ("veo", "sora", "seedance", "hailuo", "minimax", "kling", "grok", "vidu", "wan")
    return any(p in low for p in _KNOWN_VIDEO_PATTERNS)


def _ensure_video_generate_default_model(args: Dict[str, Any]) -> None:
    """video.generate 未传 model 或 model 不合法时补默认 sora2；有 image_url 时用 i2v，否则 t2v。未传 duration 时补最短时长。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    raw_model = (inner.get("model") or "").strip()
    if not (raw_model and ("/" in raw_model or _is_known_video_model_without_slash(raw_model))):
        has_img = bool((inner.get("image_url") or "").strip())
        chosen = _DEFAULT_VIDEO_GENERATE_MODEL_I2V if has_img else _DEFAULT_VIDEO_GENERATE_MODEL_T2V
        if raw_model:
            logger.info("[CHAT] video.generate model「%s」不含 / 且非已知视频模型，已替换为 model=%s", raw_model, chosen)
        else:
            logger.info("[CHAT] video.generate 未传 model，已默认 model=%s", chosen)
        inner["model"] = chosen
    dur = inner.get("duration")
    try:
        dur_f = float(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur_f = None
    if dur_f is None or dur_f <= 0:
        inner["duration"] = _DEFAULT_VIDEO_GENERATE_DURATION
        logger.info("[CHAT] video.generate 未传 duration，已默认 duration=%s", _DEFAULT_VIDEO_GENERATE_DURATION)


def _ensure_image_generate_default_model(args: Dict[str, Any]) -> None:
    """image.generate 未传 model 或 model 不合法时补默认，避免认证中心预扣与 tasks/create 报缺少 model。"""
    if not args or (args.get("capability_id") or "").strip() != "image.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    raw_model = (inner.get("model") or inner.get("model_id") or "").strip()
    if raw_model and "/" in raw_model:
        return
    if raw_model and raw_model.lower() in {m.lower() for m in _get_comfly_image_models()}:
        logger.info("[CHAT] image.generate model「%s」为 Comfly 模型，保留原值", raw_model)
        return
    if raw_model and raw_model.lower().startswith("jimeng-"):
        return
    if raw_model:
        logger.info("[CHAT] image.generate model「%s」不含 / 疑似幻觉模型名，已替换为 model=%s", raw_model, _DEFAULT_IMAGE_GENERATE_MODEL)
    inner["model"] = _DEFAULT_IMAGE_GENERATE_MODEL
    logger.info(
        "[CHAT] image.generate 未传 model，已默认 model=%s",
        _DEFAULT_IMAGE_GENERATE_MODEL,
    )


def _ensure_image_generate_prompt_and_aspect(args: Dict[str, Any], last_user_content: str) -> None:
    """补全生图 prompt（模型常漏传）；用户提到 16:9 时为 flux-2 设 landscape_16_9。"""
    if not args or (args.get("capability_id") or "").strip() != "image.generate":
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        inner = {}
        args["payload"] = inner
    pm = (str(inner.get("prompt") or inner.get("text") or "")).strip()
    u = (last_user_content or "").strip()
    if not pm and u:
        inner["prompt"] = u[:_MAX_IMAGE_GENERATE_PROMPT_CHARS]
        logger.info(
            "[CHAT] image.generate 未传 prompt，已从用户原声回填，chars=%s",
            len(inner["prompt"]),
        )
        pm = inner["prompt"]
    mid = (str(inner.get("model") or inner.get("model_id") or "")).strip().lower()
    if not mid:
        mid = _DEFAULT_IMAGE_GENERATE_MODEL.lower()
    wants_16_9 = bool(u and _IMAGE_16_9_RE.search(u))
    if wants_16_9 and "flux-2" in mid and not (str(inner.get("image_size") or "")).strip():
        inner["image_size"] = "landscape_16_9"
        logger.info("[CHAT] image.generate 用户含 16:9，已设 image_size=landscape_16_9")
    _ar_existing = (str(inner.get("aspect_ratio") or inner.get("ratio") or "")).strip()
    if wants_16_9 and "nano-banana" in mid and not _ar_existing:
        inner["aspect_ratio"] = "16:9"
        logger.info("[CHAT] image.generate 用户含 16:9，已设 aspect_ratio=16:9（nano-banana）")


def _invoke_model_for_cost_confirm(capability_id: Optional[str], args: Dict[str, Any]) -> Optional[str]:
    """与本次即将提交 MCP 的 payload 中模型字段一致（含对话层已补全的默认 model）。"""
    if not capability_id or not isinstance(args, dict):
        return None
    pl = args.get("payload")
    if not isinstance(pl, dict):
        return None
    if capability_id in ("image.generate", "video.generate"):
        m = (pl.get("model") or pl.get("model_id") or "").strip()
        return m or None
    if capability_id == "comfly.veo":
        if (pl.get("action") or "").strip() not in ("submit_video", "generate_prompts"):
            return None
        m = (pl.get("video_model") or pl.get("model") or "").strip()
        return m or None
    return None


# 模型把素材 ID 误写入 image_url / media_files 时（非 http 链），速推侧会 422；与附图解析一致转公网 URL
_VIDEO_PAYLOAD_ASSET_ID_TOKEN = re.compile(r"^[a-f0-9]{8,64}$", re.IGNORECASE)


def _resolve_video_payload_asset_ids_to_urls(
    args: Dict[str, Any],
    request: Optional[Request],
    db,
    user_id: Optional[int],
) -> None:
    """video.generate：将 payload 中形似素材 ID 的字段解析为 get_asset_public_url 公网链。"""
    if not args or (args.get("capability_id") or "").strip() != "video.generate":
        return
    if not request or db is None or user_id is None:
        return
    inner = args.get("payload")
    if not isinstance(inner, dict):
        return
    from .assets import get_asset_public_url

    asb = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/") or "（未配置 AUTH_SERVER_BASE）"

    def resolve_one(val: Any) -> str:
        if val is None:
            return ""
        raw = str(val).strip()
        if not raw:
            return raw
        if raw.lower().startswith("http://") or raw.lower().startswith("https://"):
            return raw
        if not _VIDEO_PAYLOAD_ASSET_ID_TOKEN.match(raw):
            return raw
        aid = raw.lower()
        u = get_asset_public_url(aid, user_id, request, db)
        if not u:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"视频图生参数中的素材 ID「{aid}」无法解析为公网 URL。请将该图作为本条消息附图上传，"
                    f"或确认素材库中该条目的 source_url（TOS / 服务器临时上传）有效；AUTH_SERVER_BASE={asb}。"
                ),
            )
        if _LOCAL_SIGNED_ASSET_PATH in u:
            raise HTTPException(
                status_code=400,
                detail=f"素材 {aid} 为旧版签名链接，已不可用。请重新上传后重试。",
            )
        logger.info("[CHAT] video.generate 已将参数中的 asset_id 解析为公网 URL asset_id=%s", aid)
        return u

    if inner.get("image_url") is not None:
        inner["image_url"] = resolve_one(inner.get("image_url"))
    for key in ("media_files", "filePaths"):
        v = inner.get(key)
        if isinstance(v, list):
            inner[key] = [resolve_one(x) for x in v]

    # 无附图时 _infer_video_model_from_user_text 可能写 fal-ai/veo3.1；素材 ID 已解析为公网图后须用 i2v
    m0 = (inner.get("model") or "").strip()
    if m0 == "fal-ai/veo3.1":
        img0 = (inner.get("image_url") or "").strip()
        has_http = img0.startswith("http://") or img0.startswith("https://")
        if not has_http:
            for key in ("media_files", "filePaths"):
                lv = inner.get(key)
                if not isinstance(lv, list):
                    continue
                for x in lv:
                    s = (str(x).strip() if x is not None else "")
                    if s.startswith("http://") or s.startswith("https://"):
                        has_http = True
                        break
                if has_http:
                    break
        if has_http:
            inner["model"] = "fal-ai/veo3.1/image-to-video"
            logger.info("[CHAT] Veo 3.1：payload 已含公网图，model 已从文生改为 image-to-video")


def _is_upstream_sutui_video_id_token(s: str) -> bool:
    """速推/Sora 等在 JSON 里的 video_id，形如 video_p… / video_…，不是素材库 asset_id。"""
    t = (s or "").strip()
    if len(t) < 12:
        return False
    if t.startswith("video_p") or t.startswith("video_"):
        return True
    return False


def _looks_like_v3_tasks_output_filename_asset_id(s: str) -> bool:
    """模型把 v3-tasks 成品文件名当成 asset_id，如 5d79872e01ca44cda629428792162285.mp4。"""
    t = (s or "").strip().lower()
    return bool(
        re.match(
            r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{16,64})\.(mp4|webm|mov)$",
            t,
        )
    )


def _resolve_video_asset_by_v3_save_dedupe(db: Session, uid: int, raw_url: str):
    """v3-tasks 原始链的 save_url_dedupe 与入库一致；source_url 可能是 mcp-images，靠 dedupe 命中。"""
    from .assets import _find_existing_asset_by_save_url_dedupe, _save_url_dedupe_key

    u0 = (raw_url or "").strip().split("?")[0].split("#")[0]
    if "v3-tasks" not in u0.lower():
        return None
    dk = _save_url_dedupe_key(u0)
    return _find_existing_asset_by_save_url_dedupe(db, uid, dk)


def _collect_video_urls_from_task_result_for_publish_context(result_text: str) -> List[str]:
    out: List[str] = []
    seen: set = set()
    saved = _extract_saved_assets_from_task_result(result_text)
    for it in saved:
        if not isinstance(it, dict):
            continue
        u = (it.get("source_url") or it.get("url") or "").strip()
        if u.startswith("http"):
            k = u.split("?")[0].split("#")[0].lower()
            if k not in seen:
                seen.add(k)
                out.append(u)
    if not out:
        for it in _extract_video_urls_from_task_result(result_text):
            if not isinstance(it, dict):
                continue
            u = (it.get("url") or "").strip()
            if u.startswith("http"):
                k = u.split("?")[0].split("#")[0].lower()
                if k not in seen:
                    seen.add(k)
                    out.append(u)
    return out[:8]


# publish_content：等前端/MCP save-url 把 v3 成品写入库后再发（避免偶发「素材不存在」）
_PUBLISH_WAIT_SAVE_URL_MAX_SEC = 100.0
_PUBLISH_WAIT_SAVE_URL_INTERVAL_SEC = 0.45


def _publish_context_has_v3_tasks_url(urls: List[str]) -> bool:
    return any("v3-tasks" in (u or "").lower() for u in urls)


def _try_map_publish_content_asset_id(args: Dict[str, Any], db, user_id: Optional[int], aid_original: str) -> None:
    """单次尝试：把误填 id 映射为素材库 asset_id（不等待）。"""
    if not isinstance(args, dict) or db is None or user_id is None:
        return
    aid = (args.get("asset_id") or "").strip()
    if not aid:
        return
    from ..models import Asset

    if db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == aid).first():
        return

    urls = list(_recent_task_video_urls_ctx.get() or [])
    url_seq = list(reversed(urls))

    def _apply_hit(hit: Any, via: str, detail: str) -> None:
        args["asset_id"] = hit.asset_id
        logger.info(
            "[CHAT] publish_content 已将误填 asset_id=%s 映射为素材库 asset_id=%s（%s %s）",
            aid_original,
            hit.asset_id,
            via,
            detail[:96] + ("…" if len(detail) > 96 else ""),
        )

    if _looks_like_v3_tasks_output_filename_asset_id(aid):
        stem = aid.lower().rsplit(".", 1)[0].replace("-", "")
        for u in url_seq:
            u_cmp = (u or "").split("?")[0].lower().replace("-", "")
            if stem and stem in u_cmp:
                hit = _resolve_video_asset_by_v3_save_dedupe(db, user_id, u)
                if hit:
                    _apply_hit(hit, "v3-dedupe", u)
                    return
        return

    if not _is_upstream_sutui_video_id_token(aid):
        return

    for u in url_seq:
        hit = _resolve_video_asset_by_v3_save_dedupe(db, user_id, u)
        if hit:
            _apply_hit(hit, "v3-dedupe", u)
            return

    for u in url_seq:
        u0 = u.split("?")[0].strip()
        tail = u0.rsplit("/", 1)[-1].lower() if "/" in u0 else ""
        q = (
            db.query(Asset)
            .filter(Asset.user_id == user_id, Asset.media_type == "video")
            .filter(Asset.source_url.isnot(None))
        )
        hit = q.filter(Asset.source_url == u0).order_by(Asset.id.desc()).first()
        if not hit and tail and tail.endswith((".mp4", ".webm", ".mov")):
            hit = (
                db.query(Asset)
                .filter(Asset.user_id == user_id, Asset.media_type == "video")
                .filter(Asset.source_url.isnot(None))
                .filter(Asset.source_url.like(f"%{tail}%"))
                .order_by(Asset.id.desc())
                .first()
            )
        if hit:
            _apply_hit(hit, "url-match", u0)
            return


def _publish_should_wait_for_save_url(urls: List[str]) -> bool:
    """本轮 task.get_result 曾带出 v3 成品链时，发布可能依赖异步 save-url/转存，需短暂等待。"""
    return _publish_context_has_v3_tasks_url(urls)


async def _normalize_publish_content_asset_id(args: Dict[str, Any], db, user_id: Optional[int]) -> None:
    """映射误填 id；若素材可能仍在 save-url/转存途中，则轮询库直至就绪或超时。"""
    if not isinstance(args, dict) or db is None or user_id is None:
        return
    aid_original = (args.get("asset_id") or "").strip()
    if not aid_original:
        return
    from ..models import Asset

    urls = list(_recent_task_video_urls_ctx.get() or [])
    need_wait = _publish_should_wait_for_save_url(urls)
    deadline = time.perf_counter() + _PUBLISH_WAIT_SAVE_URL_MAX_SEC
    logged_wait = False

    while True:
        db.expire_all()
        _try_map_publish_content_asset_id(args, db, user_id, aid_original)
        cur = (args.get("asset_id") or "").strip()
        if cur and db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == cur).first():
            if logged_wait:
                logger.info("[CHAT] publish_content 已等待转存完成，素材就绪 asset_id=%s", cur)
            return
        if not need_wait:
            break
        if time.perf_counter() >= deadline:
            logger.warning(
                "[CHAT] publish_content 等待 save-url 入库超时(%.0fs)，仍将尝试发布 asset_id=%s",
                _PUBLISH_WAIT_SAVE_URL_MAX_SEC,
                cur or aid_original,
            )
            break
        if not logged_wait:
            logger.info(
                "[CHAT] publish_content 等待 save-url/转存完成后再发布（最多 %.0fs）…",
                _PUBLISH_WAIT_SAVE_URL_MAX_SEC,
            )
            logged_wait = True
        await asyncio.sleep(_PUBLISH_WAIT_SAVE_URL_INTERVAL_SEC)

    # 非等待路径或未等到：补打原有诊断（文件名误填且无 v3 上下文）
    db.expire_all()
    _try_map_publish_content_asset_id(args, db, user_id, aid_original)
    cur = (args.get("asset_id") or "").strip()
    if cur and db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == cur).first():
        return
    if _looks_like_v3_tasks_output_filename_asset_id(aid_original) and not _publish_context_has_v3_tasks_url(urls):
        logger.warning(
            "[CHAT] publish_content 误填 v3 文件名 asset_id=%s，上下文无 v3 URL，无法映射",
            aid_original,
        )
        return
    cur_aid = (args.get("asset_id") or "").strip()
    if _is_upstream_sutui_video_id_token(cur_aid) or _is_upstream_sutui_video_id_token(aid_original):
        logger.warning(
            "[CHAT] publish_content 使用上游 video_id=%s，素材库仍无匹配（v3 dedupe / URL）（请确认 save-url 已成功）",
            cur_aid or aid_original,
        )


_TOUTIAO_NO_COVER_HINT_RE = re.compile(
    r"(无封面|不要封面|无图发文|无图发布|"
    r"不要配图|不需要配图|不需要配图片|无需配图|不用配图|不配图片|省略图片|"
    r"不要图|不要图片|不需要图片|无需图片|无配图|不配图|"
    r"纯文字|纯文|不带图|不发图|无头图)",
    re.I,
)
# 对话若出现下列线索，视为「有配图/成片」意图，不自动走无图发文（避免误伤要封面的场景）
_TOUTIAO_IMAGE_OR_VIDEO_INTENT_RE = re.compile(
    r"(配图|封面|头图|海报|截图|"
    r"生图|文生图|图生|垫图|参考图|"
    r"图片|一张照片|这张图|该图|成品图|静图|"
    r"image\.generate|生成的图|"
    r"视频|成片|mp4|\.mov|素材视频|这条视频|这个视频|"
    r"上一(个|条)(图|视频)|save_asset|task\.get_result)",
    re.I,
)


def _publish_message_content_text_chunks(message_content: Any) -> List[str]:
    """OpenAI: content 为 str；Anthropic: content 为块列表，工具结果为 type=tool_result。
    publish 补全需能扫到 save_asset / save-url 返回的 JSON 与其中 URL。"""
    out: List[str] = []
    if isinstance(message_content, str) and message_content.strip():
        return [message_content]
    if not isinstance(message_content, list):
        return out
    for block in message_content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            out.append(block["text"])
        elif t == "image_url" and isinstance(block.get("image_url"), dict):
            u = (block.get("image_url") or {}).get("url")
            if isinstance(u, str) and u.strip():
                out.append(u)
        elif t == "tool_result":
            tr = block.get("content")
            if isinstance(tr, str) and tr.strip():
                out.append(tr)
            elif isinstance(tr, list):
                for sub in tr:
                    if not isinstance(sub, dict):
                        continue
                    if sub.get("type") == "text" and isinstance(sub.get("text"), str) and sub["text"].strip():
                        out.append(sub["text"])
    return out


def _resolve_publish_account_for_autofill(
    args: Dict[str, Any], db: Session, user_id: int
) -> Optional[PublishAccount]:
    """工具参数中 account_nickname 或 account_id（或误填的昵称数字）解析发布账号。"""
    nick = (args.get("account_nickname") or "").strip()
    if nick:
        return (
            db.query(PublishAccount)
            .filter(PublishAccount.user_id == int(user_id), PublishAccount.nickname == nick)
            .first()
        )
    aid_raw = args.get("account_id")
    if aid_raw is None or str(aid_raw).strip() == "":
        return None
    try:
        aid = int(aid_raw)
    except (TypeError, ValueError):
        nick_candidate = str(aid_raw).strip()
        return (
            db.query(PublishAccount)
            .filter(PublishAccount.user_id == int(user_id), PublishAccount.nickname == nick_candidate)
            .first()
        )
    return (
        db.query(PublishAccount)
        .filter(PublishAccount.user_id == int(user_id), PublishAccount.id == aid)
        .first()
    )


def _extract_asset_ids_from_chat_messages_for_publish(
    messages: Optional[List[Dict[str, Any]]],
    db: Session,
    user_id: int,
) -> List[str]:
    """
    从近期助手/tool 消息正文（含 save-url 等 JSON）中提取已在库的 asset_id，新→旧。
    解决模型漏传 asset_id、但上文工具已返回 asset_id 的情况。
    """
    if not messages:
        return []
    from ..models import Asset

    out: List[str] = []
    seen: set = set()
    patterns = (
        r'"asset_id"\s*:\s*"([a-zA-Z0-9]{10,32})"',
        r"'asset_id'\s*:\s*'([a-zA-Z0-9]{10,32})'",
        r"\basset_id\s*=\s*([a-zA-Z0-9]{10,32})\b",
    )
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") not in ("assistant", "tool", "user"):
            continue
        chunks = _publish_message_content_text_chunks(m.get("content"))
        blob = "\n".join(chunks)
        if not blob.strip():
            continue
        for pat in patterns:
            for match in re.finditer(pat, blob, re.I):
                aid = match.group(1).strip()
                if not aid or aid in seen:
                    continue
                if db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == aid).first():
                    seen.add(aid)
                    out.append(aid)
        if len(out) >= 8:
            break
    return out


def _extract_urls_from_messages_for_publish(messages: Optional[List[Dict[str, Any]]]) -> List[str]:
    """从整轮对话（user/assistant/tool）正文提取 URL，新消息优先，供 save-url dedupe 反查 asset_id。"""
    if not messages:
        return []
    out: List[str] = []
    seen_norm: set = set()
    for m in reversed(messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") not in ("user", "assistant", "tool"):
            continue
        parts = _publish_message_content_text_chunks(m.get("content"))
        blob = "\n".join(parts)
        if not blob.strip():
            continue
        for match in _URL_RE.finditer(blob):
            raw = match.group(0).rstrip(".,;:!?)」』\"'")
            if not raw.startswith("http"):
                continue
            key = raw.split("?")[0].split("#")[0].lower()
            if key in seen_norm:
                continue
            seen_norm.add(key)
            out.append(raw.split("?")[0].split("#")[0])
        if len(out) >= 24:
            break
    return out


def _pick_publish_asset_id_from_candidates(
    db: Session,
    user_id: int,
    ordered_ids: List[str],
    *,
    platform: str,
) -> Optional[str]:
    """多条候选时：头条图文优先封面图（image），否则视频；其它平台按候选顺序。"""
    from ..models import Asset
    from .publish import _infer_asset_media_type

    uniq: List[str] = []
    seen: set = set()
    for x in ordered_ids:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    rows: List[Any] = []
    for aid in uniq:
        row = db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == aid).first()
        if row is not None:
            rows.append(row)
    if not rows:
        return None
    plat = (platform or "").strip().lower()
    if plat == "toutiao":
        for row in rows:
            if _infer_asset_media_type(row) == "image":
                return row.asset_id
        for row in rows:
            if _infer_asset_media_type(row) == "video":
                return row.asset_id
        return rows[0].asset_id
    return rows[0].asset_id


def _collect_publish_asset_id_candidates(
    pctx: Optional[Dict[str, Any]],
    db: Session,
    user_id: int,
) -> List[str]:
    """附图 asset_id、附图 URL 对应的库内 id、消息里出现的 id — 按优先级展平为列表（后与 _pick 配合）。"""
    from .assets import _find_existing_asset_by_save_url_dedupe, _save_url_dedupe_key

    ordered: List[str] = []
    if pctx:
        aids_att = [
            a.strip()
            for a in (pctx.get("attachment_asset_ids") or [])
            if isinstance(a, str) and a.strip()
        ]
        for aid in reversed(aids_att):
            ordered.append(aid)
        urls = [
            u.strip() for u in (pctx.get("attachment_urls") or []) if isinstance(u, str) and u.strip()
        ]
        for u in reversed(urls):
            u0 = u.split("?")[0].split("#")[0]
            try:
                dk = _save_url_dedupe_key(u0)
                hit = _find_existing_asset_by_save_url_dedupe(db, int(user_id), dk)
                if hit:
                    ordered.append(hit.asset_id)
            except Exception:
                continue
    for h in _recent_publish_asset_hints_ctx.get() or []:
        if h and h not in ordered:
            ordered.append(h)
    for u in _extract_urls_from_messages_for_publish(pctx.get("messages") if pctx else None):
        u0 = u.split("?")[0].split("#")[0]
        try:
            dk = _save_url_dedupe_key(u0)
            hit = _find_existing_asset_by_save_url_dedupe(db, int(user_id), dk)
            if hit:
                ordered.append(hit.asset_id)
        except Exception:
            continue
    ordered.extend(
        _extract_asset_ids_from_chat_messages_for_publish(
            pctx.get("messages") if pctx else None,
            db,
            int(user_id),
        )
    )
    return ordered


def _autofill_publish_content_missing_asset_id(
    args: Dict[str, Any],
    db: Session,
    user_id: Optional[int],
    pctx: Optional[Dict[str, Any]],
) -> None:
    """模型未传 asset_id 时，用本条附图/URL/近期工具返回的 id 补全（再交给 normalize 做映射与等待）。"""
    if not isinstance(args, dict) or db is None or user_id is None:
        return
    if (args.get("asset_id") or "").strip():
        return
    acct = _resolve_publish_account_for_autofill(args, db, int(user_id))
    plat = (acct.platform or "").strip().lower() if acct else ""
    ordered = _collect_publish_asset_id_candidates(pctx, db, int(user_id))
    chosen = _pick_publish_asset_id_from_candidates(db, int(user_id), ordered, platform=plat)
    if chosen:
        args["asset_id"] = chosen
        logger.info(
            "[CHAT] publish_content 上下文补全 asset_id=%s platform=%s",
            chosen,
            plat or "?",
        )


def _flatten_latest_user_message_for_publish_autofill(
    messages: Optional[List[Dict[str, Any]]],
) -> str:
    """仅本条用户最新一轮输入：不扫描上文用户或任何助手内容，避免同会话历史干扰发布形态判定。"""
    if not messages:
        return ""
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        chunks: List[str] = []
        for piece in _publish_message_content_text_chunks(m.get("content")):
            s = piece.strip()
            if s:
                chunks.append(s)
        return "\n".join(chunks)
    return ""


def _autofill_toutiao_graphic_no_cover_options(
    args: Dict[str, Any],
    *,
    messages: Optional[List[Dict[str, Any]]],
    attachment_urls: List[str],
    attachment_asset_ids: List[str],
    db: Optional[Session],
    user_id: Optional[int],
) -> None:
    """今日头条：按「最后一条用户话 + 本轮 title/description」判定无封面纯文；不读会话上文用户或助手内容。"""
    if not isinstance(args, dict) or db is None or user_id is None:
        return
    from ..models import Asset

    acct = _resolve_publish_account_for_autofill(args, db, user_id)
    if not acct or (acct.platform or "").strip().lower() != "toutiao":
        return
    opts = args.get("options")
    if opts is None:
        opts = {}
        args["options"] = opts
    elif not isinstance(opts, dict):
        return
    if attachment_urls or attachment_asset_ids:
        return
    aid = (args.get("asset_id") or "").strip()
    if aid:
        from .publish import _infer_asset_media_type

        row = db.query(Asset).filter(Asset.user_id == int(user_id), Asset.asset_id == aid).first()
        if row and _infer_asset_media_type(row) in ("video", "image"):
            return
    # 能执行到这里说明当前没有待发图/视频素材；false 多为同上会话「带图发文」复制来的，会触发 MCP 要求 asset_id
    if opts.get("toutiao_graphic_no_cover") is False:
        opts.pop("toutiao_graphic_no_cover", None)
        opts.pop("toutiao_force_graphic_no_cover", None)
        logger.info(
            "[CHAT] 头条发布：清除 toutiao_graphic_no_cover=false，按当前无图/无视频素材重算",
        )
    user_blob = _flatten_latest_user_message_for_publish_autofill(messages)
    tt_extra: List[str] = []
    for key in ("title", "description", "tags"):
        s = args.get(key)
        if isinstance(s, str) and s.strip():
            tt_extra.append(s.strip())
    blob = "\n".join(x for x in [user_blob, "\n".join(tt_extra)] if x)
    explicit_no_cover = bool(_TOUTIAO_NO_COVER_HINT_RE.search(blob))
    # 「不需要配图片」等含「配/图」字样；若不先剔除无图短语，会误命中「配图」意图而关掉无封面发文
    scrubbed_for_media = _TOUTIAO_NO_COVER_HINT_RE.sub(" ", blob)
    has_media_intent = bool(_TOUTIAO_IMAGE_OR_VIDEO_INTENT_RE.search(scrubbed_for_media))
    # 有配图/成片意图时：显式 false，否则 publish.py 会对空 options 执行 setdefault(toutiao_graphic_no_cover,true) 仍走无图
    if explicit_no_cover and has_media_intent:
        opts["toutiao_graphic_no_cover"] = False
        opts.pop("toutiao_force_graphic_no_cover", None)
        logger.info(
            "[CHAT] 头条发布：无封面与配图线索并存，已设 toutiao_graphic_no_cover=false（避免服务端默认无图）",
        )
        return
    if not explicit_no_cover and has_media_intent:
        opts["toutiao_graphic_no_cover"] = False
        opts.pop("toutiao_force_graphic_no_cover", None)
        logger.info(
            "[CHAT] 头条发布：对话含配图/成片意图，已设 toutiao_graphic_no_cover=false",
        )
        return
    opts["toutiao_graphic_no_cover"] = True
    opts["toutiao_force_graphic_no_cover"] = True
    # 模型常把字数/账号误填成 asset_id；无图发文不应带无效素材 id，否则会按图文链路报错
    aid_bad = (args.get("asset_id") or "").strip()
    if aid_bad:
        row_bad = db.query(Asset).filter(Asset.user_id == int(user_id), Asset.asset_id == aid_bad).first()
        if not row_bad:
            args.pop("asset_id", None)
            logger.info(
                "[CHAT] 头条发布自动无图发文：已移除无效 asset_id=%s",
                aid_bad[:64],
            )
    logger.info(
        "[CHAT] 头条发布自动无图发文：account=%s explicit_no_cover=%s has_media_intent=%s",
        acct.nickname,
        explicit_no_cover,
        has_media_intent,
    )


def _toutiao_ensure_force_no_cover_if_image_asset(
    args: Dict[str, Any],
    db: Session,
    user_id: int,
) -> None:
    """主素材为图且 options 已含无封面时，补 toutiao_force_graphic_no_cover，避免 publish.API 剥掉无封面。"""
    if not isinstance(args, dict) or db is None or user_id is None:
        return
    from ..models import Asset
    from .publish import _infer_asset_media_type, _truthy

    acct = _resolve_publish_account_for_autofill(args, db, user_id)
    if not acct or (acct.platform or "").strip().lower() != "toutiao":
        return
    opts = args.get("options")
    if not isinstance(opts, dict):
        return
    if not _truthy(opts.get("toutiao_graphic_no_cover")):
        return
    if _truthy(opts.get("toutiao_force_graphic_no_cover")):
        return
    aid = (args.get("asset_id") or "").strip()
    if not aid:
        return
    row = db.query(Asset).filter(Asset.user_id == int(user_id), Asset.asset_id == aid).first()
    if row and _infer_asset_media_type(row) == "image":
        opts["toutiao_force_graphic_no_cover"] = True
        logger.info(
            "[CHAT] 头条无封面+主素材为图，补 toutiao_force_graphic_no_cover",
        )


_DAIHUO_CHAT_POLL_INTERVAL_SEC = 15.0
_DAIHUO_CHAT_POLL_MAX_SEC = 7200.0


def _normalize_invoke_daihuo_pipeline_args_for_chat(args: Dict[str, Any]) -> Dict[str, Any]:
    """与 mcp/http_server._normalize_invoke_daihuo_pipeline_args 一致，避免对话层与 MCP 参数形态不一致。"""
    if not isinstance(args, dict):
        return args
    if (args.get("capability_id") or "").strip() != "comfly.veo.daihuo_pipeline":
        return args
    raw_pl = args.get("payload")
    pl: Dict[str, Any] = dict(raw_pl) if isinstance(raw_pl, dict) else {}
    nested = pl.get("payload")
    if isinstance(nested, dict) and (
        (nested.get("action") or "").strip()
        or (nested.get("job_id") or "").strip()
        or nested.get("asset_id") is not None
    ):
        base = {k: v for k, v in pl.items() if k != "payload"}
        pl = {**base, **nested}
    if not (pl.get("action") or "").strip():
        top_act = (args.get("action") or "").strip()
        if top_act:
            pl["action"] = top_act
    for k in (
        "job_id",
        "asset_id",
        "image_url",
        "merge_clips",
        "storyboard_count",
        "auto_save",
        "platform",
        "country",
        "language",
        "output_dir",
        "isolate_job_dir",
        "image_request_style",
    ):
        if k in args and args[k] is not None:
            pl.setdefault(k, args[k])
    out = dict(args)
    out["payload"] = pl
    return out


def _daihuo_start_payload_from_pl(pl: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "asset_id",
        "image_url",
        "merge_clips",
        "storyboard_count",
        "auto_save",
        "platform",
        "country",
        "language",
        "output_dir",
        "isolate_job_dir",
        "image_request_style",
    )
    out: Dict[str, Any] = {}
    for k in keys:
        if k in pl and pl[k] is not None:
            out[k] = pl[k]
    return out


def _chat_internal_api_headers(token: str, request: Optional[Request]) -> Dict[str, str]:
    h: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if request:
        xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
        if xi:
            h["X-Installation-Id"] = xi
    return h


def _daihuo_job_progress_hint(job: Dict[str, Any]) -> str:
    """仅摘要阶段信息，禁止把 manifest 整段 JSON / 本机路径推给前端。"""
    prog = job.get("progress")
    if isinstance(prog, dict):
        for k in ("step", "message", "label", "phase"):
            v = prog.get(k)
            if isinstance(v, str) and v.strip():
                return _sanitize_user_visible_hint(v.strip())[:120]
        ms = prog.get("manifest_status")
        if isinstance(ms, str) and ms.strip():
            return f"阶段 {ms.strip()[:40]}"
        ls = prog.get("last_steps")
        if isinstance(ls, list) and ls:
            tail = ls[-1]
            if isinstance(tail, dict):
                nm = str(tail.get("name") or "").strip()
                st = str(tail.get("status") or "").strip()
                if nm or st:
                    return _sanitize_user_visible_hint(f"{nm} {st}".strip())[:120]
        si = prog.get("shot_indexes")
        if isinstance(si, list) and si:
            return f"分镜进度 {len(si)} 条"
        sc = prog.get("step_count")
        if isinstance(sc, int) and sc >= 0:
            return f"步骤 {sc}"
    if isinstance(prog, str) and prog.strip():
        return _sanitize_user_visible_hint(prog.strip())[:120]
    return (job.get("status") or "").strip() or ""


async def _poll_daihuo_job_http(
    *,
    base: str,
    job_id: str,
    token: str,
    request: Optional[Request],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    resume_token: str,
    waited_start: int = 0,
) -> Dict[str, Any]:
    """GET /api/comfly-daihuo/pipeline/jobs/{id} 直至终态或超时。"""
    jid = (job_id or "").strip().lower()
    bu = base.rstrip("/")
    hdrs = _chat_internal_api_headers(token, request)
    waited = max(0, int(waited_start))
    last: Dict[str, Any] = {}
    first = True
    while waited <= _DAIHUO_CHAT_POLL_MAX_SEC:
        if not first:
            await asyncio.sleep(_DAIHUO_CHAT_POLL_INTERVAL_SEC)
            waited += int(_DAIHUO_CHAT_POLL_INTERVAL_SEC)
            # GET 可能长时间占用（timeout=120s），进度须在请求发出前推送，否则界面一直卡在上一轮秒数
            if progress_cb and isinstance(last, dict) and last:
                try:
                    st_prev = (last.get("status") or "").strip().lower()
                    hint_prev = _daihuo_job_progress_hint(last) or st_prev or "running"
                    await progress_cb(
                        {
                            "type": "task_poll",
                            "message": f"爆款TVC 生成中…（{waited}秒）",
                            "task_id": resume_token,
                            "result_hint": hint_prev,
                        }
                    )
                except Exception:
                    pass
        first = False
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            # 不用 compact：completed 时需在正文里带 saved_assets（compact 会省略）
            pr = await client.get(
                f"{bu}/api/comfly-daihuo/pipeline/jobs/{jid}",
                headers=hdrs,
            )
        if pr.status_code >= 400:
            try:
                last = pr.json() if pr.content else {}
            except Exception:
                last = {"ok": False, "error": (pr.text or "")[:800]}
            if not isinstance(last, dict):
                last = {"ok": False, "error": str(last)}
            last.setdefault("status", "failed")
            return last
        last = pr.json() if pr.content else {}
        if not isinstance(last, dict):
            last = {"ok": False, "error": "invalid job json"}
            return last
        st = (last.get("status") or "").strip().lower()
        if progress_cb:
            try:
                ev: Dict[str, Any] = {
                    "type": "task_poll",
                    "message": f"爆款TVC 生成中…（{waited}秒）",
                    "task_id": resume_token,
                    "result_hint": _daihuo_job_progress_hint(last) or st or "running",
                }
                await progress_cb(ev)
            except Exception:
                pass
        if st in ("completed", "failed"):
            return last
        if waited >= _DAIHUO_CHAT_POLL_MAX_SEC:
            last.setdefault(
                "poll_timeout",
                "等待时间较长，任务可能仍在后台运行；可刷新页面继续查看进度。",
            )
            return last
    return last


async def _chat_daihuo_inline_start_and_poll(
    args: Dict[str, Any],
    token: str,
    request: Request,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
) -> Tuple[str, bool]:
    """流式对话：爆款TVC 走本机 start + 轮询，便于 SSE 推送 task_poll 与刷新后 daihuo_<job_id> 续查。"""
    args = _normalize_invoke_daihuo_pipeline_args_for_chat(dict(args))
    pl = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    dh_act = (pl.get("action") or "").strip() or "run_pipeline"
    if dh_act == "poll_pipeline":
        raise ValueError("poll_pipeline 请仍走 MCP 单次查询")
    base = str(request.base_url).rstrip("/")
    body_payload = _daihuo_start_payload_from_pl(pl)
    hdrs = _chat_internal_api_headers(token, request)
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        sr = await client.post(
            f"{base}/api/comfly-daihuo/pipeline/start",
            json={"payload": body_payload},
            headers=hdrs,
        )
    if sr.status_code >= 400:
        try:
            err_j = sr.json()
            detail = err_j.get("detail") if isinstance(err_j, dict) else sr.text
        except Exception:
            detail = (sr.text or "")[:1200]
        return (
            json.dumps(
                {"capability_id": "comfly.veo.daihuo_pipeline", "ok": False, "error": str(detail)},
                ensure_ascii=False,
                indent=2,
            ),
            False,
        )
    start_data = sr.json() if sr.content else {}
    job_id = (start_data.get("job_id") or "").strip().lower()
    if not job_id or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
        return (
            json.dumps(
                {
                    "capability_id": "comfly.veo.daihuo_pipeline",
                    "ok": False,
                    "error": "start 未返回有效 job_id",
                    "raw": start_data,
                },
                ensure_ascii=False,
                indent=2,
            ),
            False,
        )
    resume_token = f"daihuo_{job_id}"
    if progress_cb:
        try:
            await progress_cb(
                {
                    "type": "task_poll",
                    "message": "爆款TVC 任务已排队，正在生成…（0秒）",
                    "task_id": resume_token,
                    "result_hint": "job started",
                }
            )
        except Exception:
            pass
    last = await _poll_daihuo_job_http(
        base=base,
        job_id=job_id,
        token=token,
        request=request,
        progress_cb=progress_cb,
        resume_token=resume_token,
        waited_start=0,
    )
    st = (last.get("status") or "").strip().lower()
    ok = st == "completed" and last.get("ok", True) is not False
    wrap = {
        "capability_id": "comfly.veo.daihuo_pipeline",
        "ok": ok,
        "job_id": job_id,
        "result": last,
    }
    return json.dumps(wrap, ensure_ascii=False, indent=2), ok


async def _resume_daihuo_job_poll_only(
    job_id: str,
    raw_token: str,
    current_user: Union[User, _ServerUser],
    request: Optional[Request],
    progress_cb: Callable[[Dict], Awaitable[None]],
) -> str:
    """刷新后续查：仅轮询爆款TVC job，直至 completed/failed。"""
    jid = (job_id or "").strip().lower()
    if len(jid) != 32 or any(c not in "0123456789abcdef" for c in jid):
        raise HTTPException(status_code=400, detail="无效的爆款TVC job_id")
    if request is None:
        raise HTTPException(status_code=400, detail="缺少 request 上下文，无法续查 pipeline")
    _ = current_user
    resume_token = f"daihuo_{jid}"
    base = str(request.base_url).rstrip("/")
    if progress_cb:
        try:
            await progress_cb(
                {
                    "type": "task_poll",
                    "message": "正在恢复爆款TVC 进度…（0秒）",
                    "task_id": resume_token,
                    "result_hint": "resumed",
                }
            )
        except Exception:
            pass
    last = await _poll_daihuo_job_http(
        base=base,
        job_id=jid,
        token=raw_token,
        request=request,
        progress_cb=progress_cb,
        resume_token=resume_token,
        waited_start=0,
    )
    st = (last.get("status") or "").strip().lower()
    ok = st == "completed" and last.get("ok", True) is not False
    wrap = {
        "capability_id": "comfly.veo.daihuo_pipeline",
        "ok": ok,
        "job_id": jid,
        "result": last,
    }
    res_str = json.dumps(wrap, ensure_ascii=False, indent=2)
    if progress_cb:
        try:
            await progress_cb(
                _daihuo_polling_final_progress_event(result_text=res_str, task_id=resume_token)
            )
        except Exception:
            pass
    return res_str


def _parse_daihuo_pipeline_tool_result(result_text: str) -> Optional[Dict[str, Any]]:
    raw = (result_text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if (d.get("capability_id") or "").strip() != "comfly.veo.daihuo_pipeline":
        return None
    return d


def _daihuo_pipeline_result_in_progress(result_text: str) -> bool:
    """爆款TVC 工具 JSON：内层 job 仍为 running 等时为 True。"""
    d = _parse_daihuo_pipeline_tool_result(result_text)
    if not d or d.get("ok") is False:
        return False
    inner = d.get("result")
    if not isinstance(inner, dict):
        return False
    st = (inner.get("status") or "").strip().lower()
    if st in ("completed", "failed"):
        return False
    return True


def _extract_daihuo_resume_token_from_result_text(result_text: str) -> str:
    d = _parse_daihuo_pipeline_tool_result(result_text)
    if not d:
        return ""
    jid = (d.get("job_id") or "").strip().lower()
    if len(jid) == 32 and all(c in "0123456789abcdef" for c in jid):
        return f"daihuo_{jid}"
    inner = d.get("result")
    if isinstance(inner, dict):
        jid2 = (inner.get("job_id") or "").strip().lower()
        if len(jid2) == 32 and all(c in "0123456789abcdef" for c in jid2):
            return f"daihuo_{jid2}"
    return ""


def _saved_assets_from_daihuo_pipeline_tool_result(result_text: str) -> List[Dict[str, Any]]:
    """job 完成且含 saved_assets 时，转为与 SSE / 前端一致的条目。"""
    d = _parse_daihuo_pipeline_tool_result(result_text)
    if not d or not d.get("ok", True):
        return []
    inner = d.get("result")
    if not isinstance(inner, dict):
        return []
    if (inner.get("status") or "").strip().lower() != "completed":
        return []
    raw_saved = inner.get("saved_assets") or []
    if not isinstance(raw_saved, list):
        return []
    out: List[Dict[str, Any]] = []
    for it in raw_saved:
        if not isinstance(it, dict):
            continue
        row = it.get("asset")
        aid = ""
        url = ""
        if isinstance(row, dict):
            aid = str(row.get("asset_id") or row.get("id") or "").strip()
            url = str(row.get("url") or row.get("preview_url") or "").strip()
        su = str(it.get("source_url") or "").strip()
        if not url and su:
            url = su
        if not url and not aid:
            continue
        ext = url.lower().split("?")[0].split("#")[0]
        mt = "image" if ext.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) else "video"
        gen_tid = str(it.get("task_id") or "").strip()
        one: Dict[str, Any] = {
            "source_url": su or url,
            "url": url or su,
            "media_type": mt,
            "tags": "auto,comfly.veo.daihuo_pipeline",
        }
        if aid:
            one["asset_id"] = aid
        if gen_tid:
            one["generation_task_id"] = gen_tid[:128]
        out.append(one)
    return out


def _daihuo_polling_final_progress_event(*, result_text: str, task_id: str) -> Dict[str, Any]:
    """刷新后续查轮询结束后补发 tool_end，便于前端展示 saved_assets。"""
    res = result_text or ""
    d = _parse_daihuo_pipeline_tool_result(res) or {}
    inner = d.get("result")
    st = ""
    if isinstance(inner, dict):
        st = (inner.get("status") or "").strip().lower()
    ok_wrap = d.get("ok", True) is not False
    success = ok_wrap and st == "completed"
    ev: Dict[str, Any] = {
        "type": "tool_end",
        "name": "invoke_capability",
        "preview": res[:200],
        "capability_id": "comfly.veo.daihuo_pipeline",
        "phase": "task_polling",
        "in_progress": False,
        "media_type": "video",
        "success": success,
    }
    tid = (task_id or "").strip()
    if tid:
        ev["task_id"] = tid
    sse = _saved_assets_from_daihuo_pipeline_tool_result(res)
    if sse:
        ev["saved_assets"] = sse
    elif st == "failed" or d.get("ok") is False:
        ev["success"] = False
    logger.info(
        "[对话轮询] daihuo_pipeline 终态补发 tool_end saved_assets 条数=%s task_id=%s",
        len(sse) if sse else 0,
        tid or "-",
    )
    return ev


async def _exec_tool(
    name: str,
    args: Dict,
    token: str = "",
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    request: Optional[Request] = None,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """Execute a tool on the local MCP server and return the text result. progress_cb: 可选，用于流式推送进度（tool_start/tool_end）。"""
    if name == "list_capabilities":
        cached = _list_capabilities_cache.get()
        if cached is not None:
            logger.info("[对话] list_capabilities 命中缓存，跳过重复调用")
            return cached
    if name == "invoke_capability" and (args.get("capability_id") or "").strip() == "task.get_result":
        args = _normalize_invoke_task_get_result_args(args)
    capability_id = (args.get("capability_id") or "").strip() if name == "invoke_capability" else None
    if name == "invoke_capability" and capability_id == "comfly.veo.daihuo_pipeline":
        args = _normalize_invoke_daihuo_pipeline_args_for_chat(dict(args))
    phase = None
    if capability_id == "video.generate":
        phase = "video_submit"
    elif capability_id == "image.generate":
        phase = "image_submit"
    elif capability_id == "task.get_result":
        phase = "task_polling"
    elif capability_id == "comfly.veo":
        pl_cv = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        if (pl_cv.get("action") or "").strip() == "poll_video":
            phase = "task_polling"
    elif capability_id == "comfly.veo.daihuo_pipeline":
        pl_dh = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        dh_act = (pl_dh.get("action") or "").strip() or "run_pipeline"
        if dh_act != "poll_pipeline":
            phase = "task_polling"
    # save_asset：若 URL 已对应库内素材（与 save-url 去重一致），不再打 MCP/不播「正在 save_asset」，避免模型跟进入库后又调一次
    save_asset_shortcut: Optional[str] = None
    skip_tool_start = False
    if name == "save_asset" and isinstance(args, dict) and db is not None and user_id is not None:
        url_sa = (args.get("url") or "").strip()
        if url_sa.startswith("http"):
            from .assets import _find_existing_asset_by_save_url_dedupe, _save_url_dedupe_key

            try:
                u0 = url_sa.split("?")[0].split("#")[0]
                hit_sa = _find_existing_asset_by_save_url_dedupe(db, int(user_id), _save_url_dedupe_key(u0))
                if hit_sa:
                    mt = str(args.get("media_type") or "image").strip() or "image"
                    save_asset_shortcut = json.dumps(
                        {
                            "asset_id": hit_sa.asset_id,
                            "filename": hit_sa.filename or "",
                            "media_type": getattr(hit_sa, "media_type", None) or mt,
                            "source_url": (getattr(hit_sa, "source_url", None) or "") or u0,
                            "skipped_duplicate_save": True,
                            "detail": "该 URL 已在素材库中存在，未再次请求 save-url。",
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    skip_tool_start = True
                    _merge_publish_asset_hints([hit_sa.asset_id])
                    logger.info("[CHAT] save_asset 去重短路（已存在）asset_id=%s", hit_sa.asset_id)
            except Exception as e:
                logger.warning("[CHAT] save_asset 去重短路检测异常: %s", e)
    ev_start = {"type": "tool_start", "name": name, "args": list(args.keys())}
    if capability_id is not None:
        ev_start["capability_id"] = capability_id
    if phase:
        ev_start["phase"] = phase
    if capability_id == "task.get_result":
        _tid_s = _task_id_from_invoke_capability_args(args)
        if _tid_s:
            ev_start["task_id"] = _tid_s
    elif capability_id == "comfly.veo":
        _pl_cv_s = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        if (_pl_cv_s.get("action") or "").strip() == "poll_video":
            _tv_s = str(_pl_cv_s.get("task_id") or "").strip()
            if _tv_s:
                ev_start["task_id"] = _tv_s
    if progress_cb and not skip_tool_start:
        try:
            await progress_cb(ev_start)
        except Exception:
            pass

    cost_confirm_cancel = False
    cost_confirm_result_text = ""
    _cost_cancelled_caps = _cost_cancelled_caps_ctx.get()
    if _cost_cancelled_caps is None:
        _cost_cancelled_caps = set()
        _cost_cancelled_caps_ctx.set(_cost_cancelled_caps)
    if (
        getattr(settings, "chat_require_capability_cost_confirm", False)
        and progress_cb
        and user_id is not None
        and db is not None
        and name == "invoke_capability"
        and capability_id
        and invoke_should_prompt_cost_confirm(args if isinstance(args, dict) else {})
        and not _schedule_orchestration_active.get()
        and not _review_prompt_drafts_only_active.get()
    ):
        if capability_id in _cost_cancelled_caps:
            cost_confirm_cancel = True
            cost_confirm_result_text = (
                "用户已在本轮对话中取消过该能力调用，禁止再次调用。请直接回复用户告知已取消，不要重试或变换参数再调同一能力。"
            )
        else:
            from ..services.capability_cost_confirm import (
                CONFIRM_WAIT_SECONDS,
                abandon_capability_confirm,
                estimate_capability_credits_for_invoke,
                register_capability_confirm,
            )

            est = await estimate_capability_credits_for_invoke(db, capability_id, args if isinstance(args, dict) else {}, token=token, request=request)
            _est_credits = est.get("credits")
            if _est_credits is None or (_est_credits is not None and _est_credits <= 0):
                pass
            else:
                ctoken, cfut = register_capability_confirm(int(user_id))
                try:
                    await progress_cb(
                        {
                            "type": "capability_cost_confirm",
                            "confirm_token": ctoken,
                            "capability_id": capability_id,
                            "invoke_model": _invoke_model_for_cost_confirm(capability_id, args if isinstance(args, dict) else {}),
                            "estimated_credits": est.get("credits"),
                            "estimate_note": est.get("note") or "",
                            "timeout_seconds": CONFIRM_WAIT_SECONDS,
                        }
                    )
                except Exception:
                    pass
                timed_out = False
                try:
                    accepted = await asyncio.wait_for(cfut, timeout=float(CONFIRM_WAIT_SECONDS))
                except asyncio.TimeoutError:
                    abandon_capability_confirm(ctoken)
                    accepted = False
                    timed_out = True
                if not accepted:
                    cost_confirm_cancel = True
                    _cost_cancelled_caps.add(capability_id)
                    cost_confirm_result_text = (
                        "确认超时，本次调用已取消。请直接回复用户告知已取消，不要重试。" if timed_out
                        else "用户已取消本次调用。请直接回复用户告知已取消，禁止再次调用同一能力或重试。"
                    )

    t0 = time.perf_counter()
    success = True
    result_text = ""
    # task.get_result may poll upstream for up to 30 min (video generation)
    timeout = 120.0
    if name == "invoke_capability" and (args.get("capability_id") or "").strip() == "task.get_result":
        timeout = 35 * 60.0  # 35 min
    elif name == "invoke_capability" and (args.get("capability_id") or "").strip() == "comfly.veo.daihuo_pipeline":
        # MCP 在同一次 tools/call 内轮询整包任务最久约 2h（mcp/http_server _COMFLY_DAIHUO_MCP_POLL_MAX_SEC）；
        # 对话层默认 120s 会先 ReadTimeout，用户只看到「失败且无详情」。
        timeout = 130 * 60.0
    elif name == "invoke_capability" and (args.get("capability_id") or "").strip() == "comfly.veo":
        pl_cv = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        if (pl_cv.get("action") or "").strip() == "submit_video":
            timeout = 40 * 60.0  # MCP 内对 Veo 阻塞轮询，需长于默认 120s
    elif name == "invoke_capability" and (args.get("capability_id") or "").strip() == "video.generate":
        # 网关/MCP 可能对单次 tools/call 阻塞到任务完成或长时间排队，默认 120s 会 ReadTimeout，
        # 对话误以为失败，而上游仍会继续生成并在素材库落成片。
        timeout = 40 * 60.0
    elif name == "invoke_capability" and (args.get("capability_id") or "").strip() == "image.generate":
        timeout = 25 * 60.0
    elif name == "sync_creator_publish_data":
        timeout = 45 * 60.0  # 多账号 Playwright 同步作品数据
    elif name == "get_creator_publish_data":
        timeout = 120.0

    def _friendly_tool_error(err: Exception) -> str:
        raw = (str(err) if err is not None else "").strip()
        if not raw:
            raw = repr(err) if err is not None else ""
        if not raw:
            raw = type(err).__name__ if err is not None else "UnknownError"
        low = raw.lower()
        if isinstance(err, httpx.RemoteProtocolError):
            return _REMOTE_DISCONNECT_USER_MSG
        if isinstance(err, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            return (
                "请求超时：本机对话等 MCP 返回时间过长（常见于爆款TVC 整包在 MCP 内长时间轮询）。"
                "已放宽该能力超时；若仍出现，请查看 logs/app.log 与 mcp.log。"
            )
        if (
            "getaddrinfo failed" in low
            or "name or service not known" in low
            or "nodename nor servname provided" in low
            or "temporary failure in name resolution" in low
        ):
            return (
                "网络解析失败（DNS）：无法解析上游接口域名。"
                "请检查网络/代理/DNS 配置，确认可访问速推与模型 API 域名后重试。"
            )
        if "all connection attempts failed" in low or "connection refused" in low:
            return "网络连接失败：无法连接到目标服务，请检查网络、端口或服务是否启动。"
        if "timed out" in low or "timeout" in low:
            return "请求超时：上游响应过慢，请稍后重试。"
        return f"工具调用失败: {raw}"

    if name in ("publish_content", "list_publish_accounts") and not _schedule_orchestration_active.get():
        pctx = _publish_autofill_ctx.get()
        if pctx:
            _user_msg = ""
            for _m in reversed(pctx.get("messages") or []):
                if _m.get("role") == "user" and isinstance(_m.get("content"), str):
                    _user_msg = _m["content"].strip()
                    break
            if _user_msg and not _PUBLISH_INTENT_RE.search(_user_msg):
                logger.info("[CHAT] %s 被拦截：用户消息中无发布意图", name)
                return json.dumps(
                    {"error": f"用户未要求发布，{name} 调用被取消。请直接回复操作结果，不要发布。"},
                    ensure_ascii=False,
                )
        _autofill_publish_content_missing_asset_id(args, db, user_id, pctx)
        await _normalize_publish_content_asset_id(args, db, user_id)
        if pctx:
            _autofill_toutiao_graphic_no_cover_options(
                args,
                messages=pctx.get("messages"),
                attachment_urls=list(pctx.get("attachment_urls") or []),
                attachment_asset_ids=list(pctx.get("attachment_asset_ids") or []),
                db=db,
                user_id=user_id,
            )
        _toutiao_ensure_force_no_cover_if_image_asset(args, db, user_id)

    skip_mcp = False
    if save_asset_shortcut:
        skip_mcp = True
        result_text = save_asset_shortcut
    if name == "invoke_capability" and capability_id == "task.get_result":
        pl0 = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        tid_veo = str(pl0.get("task_id") or "").strip()
        if tid_veo.startswith("video_"):
            skip_mcp = True
            success = False
            safe_tid = re.sub(r"[^\w\-]", "", tid_veo) or tid_veo[:128]
            result_text = (
                "此 task_id 来自 Comfly Veo（comfly.veo 的 submit_video），不是速推异步任务。"
                "对速推调用 task.get_result 会报「任务不存在」或类似错误。"
                "请改用：invoke_capability(capability_id=\"comfly.veo\", "
                'payload={"action":"poll_video","task_id":"'
                + safe_tid
                + '"}) 轮询直至上游返回完成与视频地址。'
            )
            logger.warning(
                "[对话] 已拦截误用 task.get_result（Comfly Veo task_id=%s），应使用 comfly.veo poll_video",
                safe_tid[:96],
            )

    if cost_confirm_cancel:
        skip_mcp = True
        success = False
        result_text = cost_confirm_result_text

    daihuo_inline_done = False
    if (
        name == "invoke_capability"
        and capability_id == "comfly.veo.daihuo_pipeline"
        and progress_cb is not None
        and request is not None
        and not cost_confirm_cancel
        and not skip_mcp
    ):
        pl_dh = args.get("payload") if isinstance(args.get("payload"), dict) else {}
        dh_act = (pl_dh.get("action") or "").strip() or "run_pipeline"
        if dh_act != "poll_pipeline":
            try:
                result_text, success = await _chat_daihuo_inline_start_and_poll(
                    args, token, request, progress_cb
                )
            except Exception as e:
                logger.warning("[对话] 爆款TVC 内联 start/轮询异常: %s", e, exc_info=True)
                result_text = json.dumps(
                    {
                        "capability_id": "comfly.veo.daihuo_pipeline",
                        "ok": False,
                        "error": _friendly_tool_error(e),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                success = False
            daihuo_inline_done = True
    if daihuo_inline_done:
        skip_mcp = True

    try:
        if skip_mcp:
            raise _SkipMcpToolCall()
        hdrs: Dict[str, str] = {"Content-Type": "application/json"}
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        if sutui_token:
            hdrs["X-Sutui-Token"] = sutui_token
        _fwd = _mcp_forward_headers_ctx.get()
        if _fwd:
            for _k, _v in _fwd.items():
                if _v is not None and str(_v).strip():
                    hdrs[_k] = str(_v).strip()
        if request:
            _xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
            if _xi:
                hdrs["X-Installation-Id"] = _xi
        if capability_id == "video.generate":
            pl = args.get("payload") or {}
            img = (pl.get("image_url") or "")
            mf = pl.get("media_files") or []
            logger.info(
                "[CHAT] 发 MCP video.generate payload: model=%s image_url=%s media_files=%s",
                (pl.get("model") or "(无)"),
                (img[:100] + "…") if len(img) > 100 else (img or "(无)"),
                mf,
            )
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.post(MCP_URL, json={
                "jsonrpc": "2.0", "id": "ct",
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }, headers=hdrs)
        try:
            body = r.json()
        except Exception:
            result_text = (r.text or "")[:2000] or f"MCP 响应非 JSON（HTTP {r.status_code}）"
            success = False
        else:
            if r.status_code >= 400:
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    result_text = str(err.get("message") or err)[:2000]
                else:
                    result_text = str(body)[:2000]
                success = False
            elif isinstance(body, dict) and body.get("error"):
                err = body.get("error")
                result_text = (
                    str(err.get("message")) if isinstance(err, dict) else str(err)
                )[:2000]
                success = False
            else:
                res = body.get("result") if isinstance(body, dict) else None
                if not isinstance(res, dict):
                    res = {}
                content = res.get("content", [])
                if isinstance(content, list) and content:
                    texts = [
                        x.get("text", "")
                        for x in content
                        if isinstance(x, dict) and x.get("type") == "text"
                    ]
                    result_text = "\n".join(t for t in texts if t) or json.dumps(content, ensure_ascii=False)
                else:
                    result_text = json.dumps(res, ensure_ascii=False)
                if res.get("isError"):
                    success = False
    except _SkipMcpToolCall:
        pass
    except Exception as e:
        result_text = _friendly_tool_error(e)
        success = False
        logger.warning("[对话] 工具执行异常 name=%s capability_id=%s: %s", name, capability_id, e)

    ms = round((time.perf_counter() - t0) * 1000)
    logger.info("[对话] 工具执行 name=%s capability_id=%s latency_ms=%s success=%s", name, capability_id or "-", ms, success)
    if success and name == "list_capabilities":
        _list_capabilities_cache.set(result_text)
    if success and name == "invoke_capability" and capability_id == "task.get_result":
        if not _is_task_result_in_progress(result_text):
            vu = _collect_video_urls_from_task_result_for_publish_context(result_text)
            if vu:
                bucket = _recent_task_video_urls_ctx.get()
                if bucket is not None:
                    bucket.clear()
                    bucket.extend(vu)
            _note_publish_candidate_asset_ids_from_task_result(result_text, db, user_id)
    if not success and capability_id == "video.generate" and (result_text or "").strip():
        logger.warning(
            "[对话] video.generate 失败，MCP 返回正文预览（含上游 error 时请看 JSON 内 message）: %s",
            (result_text[:1200] + "…") if len(result_text) > 1200 else result_text,
        )
    _understand_has_task_id = False
    if success and name == "invoke_capability" and capability_id in ("image.understand", "video.understand"):
        _understand_tid = _extract_task_id_from_result(result_text)
        if _understand_tid:
            _understand_has_task_id = True
            _register_generation_hint_for_task(
                _understand_tid,
                args.get("payload") if isinstance(args.get("payload"), dict) else {},
                capability_id,
            )
    urls = _URL_RE.findall(result_text)
    try:
        logs = _pending_tool_logs.get()
    except LookupError:
        logs = []
        _pending_tool_logs.set(logs)
    logs.append({
        "tool_name": name,
        "arguments": args,
        "result_text": result_text[:10000],
        "result_urls": ",".join(urls[:20]) if urls else None,
        "success": success,
        "latency_ms": ms,
    })
    _orch_log_tool(name, args, success, result_text)
    ev_end = {
        "type": "tool_end",
        "name": name,
        "preview": (result_text or "")[:200],
        "success": success,
    }
    if capability_id is not None:
        ev_end["capability_id"] = capability_id
    if _understand_has_task_id:
        ev_end["phase"] = "understand_submit"
    elif phase:
        ev_end["phase"] = phase
    if phase == "task_polling":
        if capability_id == "comfly.veo":
            ev_end["in_progress"] = _comfly_veo_poll_should_continue(result_text)
            if not ev_end.get("in_progress"):
                ev_end["media_type"] = "video"
            logger.info(
                "[进度] comfly.veo poll 单次返回 in_progress=%s",
                ev_end.get("in_progress"),
            )
        elif capability_id == "comfly.veo.daihuo_pipeline":
            ev_end["in_progress"] = _daihuo_pipeline_result_in_progress(result_text)
            if not ev_end.get("in_progress"):
                ev_end["media_type"] = "video"
            logger.info(
                "[进度] daihuo_pipeline 单次返回 in_progress=%s",
                ev_end.get("in_progress"),
            )
            if success and not ev_end.get("in_progress"):
                sse_dh = _saved_assets_from_daihuo_pipeline_tool_result(result_text)
                if sse_dh:
                    ev_end["saved_assets"] = sse_dh
                    logger.info(
                        "[对话] daihuo_pipeline 终态 tool_end saved_assets 条数=%s",
                        len(sse_dh),
                    )
        else:
            ev_end["in_progress"] = _is_task_result_in_progress(result_text)
            if not ev_end.get("in_progress"):
                ev_end["media_type"] = _extract_media_type_from_task_result(result_text)
            logger.info(
                "[进度] task.get_result 单次返回 in_progress=%s status=%s",
                ev_end.get("in_progress"),
                _extract_status_for_log(result_text),
            )
            _tid_hint = _task_id_from_invoke_capability_args(args)
            _orig_cap = (_generation_hints_map().get(_tid_hint, {}).get("capability_id") or "") if _tid_hint else ""
            _is_understand = _orig_cap in ("image.understand", "video.understand")
            if success and not ev_end.get("in_progress") and _is_understand:
                _task_status_lower = _extract_status_for_log(result_text).strip().lower()
                _task_failed = _task_status_lower in ("failed", "error", "cancelled", "canceled", "timeout", "expired", "失败", "错误", "取消", "超时")
                if _task_failed:
                    ev_end["success"] = False
                    logger.info("[对话] task.get_result 终态为理解类能力 cap=%s 但任务失败 status=%s", _orig_cap, _task_status_lower)
                else:
                    ev_end["understand_text"] = (result_text or "").strip()[:2000]
                    ev_end["media_type"] = "text"
                    logger.info("[对话] task.get_result 终态为理解类能力 cap=%s，返回文本而非素材", _orig_cap)
            elif success and not ev_end.get("in_progress"):
                sse_saved = _terminal_saved_assets_for_task_result(result_text)
                tid_poll = _tid_hint
                if sse_saved and tid_poll:
                    _apply_generation_hints_to_saved_assets(sse_saved, tid_poll)
                if sse_saved and db is not None and user_id is not None:
                    _enrich_saved_assets_asset_ids_from_db(sse_saved, db, int(user_id))
                    _merge_publish_asset_hints(
                        [
                            (it.get("asset_id") or "").strip()
                            for it in sse_saved
                            if isinstance(it, dict) and (it.get("asset_id") or "").strip()
                        ]
                    )
                if sse_saved:
                    ev_end["saved_assets"] = sse_saved
                    logger.info(
                        "[对话] task.get_result 终态 tool_end saved_assets 条数=%s task_id=%s",
                        len(sse_saved),
                        tid_poll or "-",
                    )
        tid_sse = ""
        if capability_id == "comfly.veo":
            pl_cv_sse = args.get("payload") if isinstance(args.get("payload"), dict) else {}
            tid_sse = str(pl_cv_sse.get("task_id") or "").strip()
        elif (capability_id or "").strip() == "task.get_result":
            tid_sse = _task_id_from_invoke_capability_args(args)
        elif capability_id == "comfly.veo.daihuo_pipeline":
            tid_sse = _extract_daihuo_resume_token_from_result_text(result_text)
        if tid_sse:
            ev_end["task_id"] = tid_sse
    if success and phase == "image_submit" and capability_id == "image.generate":
        sse_saved = _extract_saved_assets_from_task_result(result_text)
        if not sse_saved:
            sse_saved = _extract_image_urls_from_generate_result(result_text)
        if sse_saved:
            _apply_invoke_payload_to_saved_assets(sse_saved, args)
            tid_img = _extract_task_id_from_result(result_text)
            if tid_img:
                _register_generation_hint_for_task(tid_img, args.get("payload") or {}, "image.generate")
                _apply_generation_hints_to_saved_assets(sse_saved, tid_img)
            if db is not None and user_id is not None:
                _enrich_saved_assets_asset_ids_from_db(sse_saved, db, int(user_id))
                _merge_publish_asset_hints(
                    [
                        (it.get("asset_id") or "").strip()
                        for it in sse_saved
                        if isinstance(it, dict) and (it.get("asset_id") or "").strip()
                    ]
                )
            ev_end["saved_assets"] = sse_saved
            logger.info(
                "[对话] image.generate SSE saved_assets 条数=%s（含 MCP 已入库或待前端 save-url）",
                len(sse_saved),
            )
    if success and phase in ("video_submit", "image_submit") and capability_id in (
        "video.generate",
        "image.generate",
    ):
        tid_sub = _extract_task_id_from_result(result_text)
        if tid_sub:
            ev_end["task_id"] = tid_sub
    if success and name == "invoke_capability" and capability_id == "comfly.veo":
        tid_veo_submit = _extract_comfly_veo_task_id_from_submit_result(result_text)
        if tid_veo_submit:
            ev_end["task_id"] = tid_veo_submit
        _maybe_register_comfly_veo_submit_hint(
            result_text, args if isinstance(args, dict) else {}
        )
        sse_cf = _saved_assets_from_comfly_veo_poll_success(result_text)
        if sse_cf:
            tid_cf = ""
            for it in sse_cf:
                if isinstance(it, dict):
                    tid_cf = (it.get("generation_task_id") or "").strip()
                    if tid_cf:
                        break
            if tid_cf:
                _apply_generation_hints_to_saved_assets(sse_cf, tid_cf)
            ev_end["saved_assets"] = sse_cf
            logger.info(
                "[对话] comfly.veo poll 终态 tool_end saved_assets 条数=%s task_id=%s",
                len(sse_cf),
                tid_cf or "-",
            )
    if progress_cb:
        try:
            await progress_cb(ev_end)
        except Exception:
            pass
    if name == "publish_content" and not success and (result_text or "").strip():
        logger.warning(
            "[CHAT] publish_content 未成功，工具返回预览: %s",
            (result_text[:1800] + "…") if len(result_text) > 1800 else result_text,
        )
        result_text = (
            "【publish_content 执行失败】以下内容来自发布接口或 MCP 的原始返回。"
            "你必须如实说明未成功发布，并引用其中的 error、detail 或 status；"
            "禁止使用「已发布」「发布成功」「文章已发到」等表述。\n\n"
            + (result_text or "")
        )
    return result_text


_CAPABILITIES_QUESTION_RE = re.compile(
    r"(有哪些|有什么|会什么|能做什么|能干嘛|列出|介绍|说明).*?(能力|技能|功能|工具)|"
    r"(能力|技能|功能|工具).*?(有哪些|有什么|会什么|列一下|列出)|"
    r"你有哪些|你能做什么|你会什么|可以做什么|啥能力|什么能力|速推能力|MCP.*能力|内置能力",
    re.IGNORECASE | re.DOTALL,
)

# 用户问「速推有哪些模型 / 生成模型 / 模型列表」等：预拉 /api/v3/mcp/models 注入 system，避免模型胡编或贴长图
_MODELS_QUESTION_RE = re.compile(
    r"(速推|xskill).{0,24}(模型|model)|"
    r"(有哪些|有什么|列出|查询|查看|大全|全部|所有|完整|支持).{0,12}(模型|model)|"
    r"(模型|model).{0,12}(有哪些|列表|清单|大全|查询)|"
    r"生成.{0,8}(模型|用哪个)|哪个模型|什么模型",
    re.IGNORECASE,
)


async def _fetch_sutui_mcp_models_compact_text() -> str:
    """GET 速推公开模型表，仅 model_id + 展示名（与 scripts/xskill_fetch_models.py 同源接口）。"""
    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").rstrip("/")
    url = f"{base}/api/v3/mcp/models"
    async with httpx.AsyncClient(timeout=45.0, trust_env=False) as c:
        r = await c.get(url, headers={"Accept": "application/json"})
    r.raise_for_status()
    body = r.json()
    if not isinstance(body, dict):
        raise RuntimeError("mcp/models 响应不是 JSON 对象")
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("mcp/models 缺少 data 对象")
    models = data.get("models")
    if not isinstance(models, list):
        raise RuntimeError("mcp/models data.models 不是数组")
    lines: List[str] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = (m.get("id") or "").strip()
        name = (m.get("name") or "").strip().replace("\n", " ")
        cat = (m.get("category") or "").strip()
        lines.append(f"{mid}\t{name}\t{cat}")
    header = (
        f"共 {len(lines)} 条（{url}）。每行三列：model_id<TAB>展示名<TAB>category（image=仅文生图/图生图，用 image.generate；"
        "video=视频用 video.generate；勿把图像模型用于视频）。"
        "payload 里请填 model（与 model_id 等价）；参数边界见各模型 GET /api/v3/models/{{id}}/docs。\n"
    )
    return header + "\n".join(lines)


async def _maybe_append_sutui_models_snapshot_to_system(
    system_prompt: str,
    user_message: str,
    raw_token: str,
    request: Optional[Request],
    has_tools: bool,
) -> str:
    """用户问「有哪些模型」等时注入速推模型表，要求回答短表格、勿贴图。"""
    _ = (raw_token, request)
    if not has_tools or not (user_message or "").strip():
        return system_prompt
    if not _MODELS_QUESTION_RE.search(user_message.strip()):
        return system_prompt
    try:
        snap = await _fetch_sutui_mcp_models_compact_text()
    except Exception as e:
        logger.warning("[对话] 预拉速推模型清单失败: %s", e)
        return system_prompt
    if not (snap or "").strip():
        return system_prompt
    if len(snap) > 52000:
        snap = snap[:52000] + "\n…(已截断)"
    logger.info("[对话] 已注入速推模型清单（模型问答）len=%s", len(snap))
    return (
        system_prompt
        + "\n\n【以下为速推当前模型清单（用户正在问「有哪些模型」）；仅输出「展示名 + model_id」，"
        "可简要注明第三列 category（image 与 video 勿混用）；不要逐条写 invoke_capability 或长 payload 示例；"
        "用紧凑表格或多列排版，尽量在一次回复里多列模型；禁止 Markdown 插图、禁止长文案、禁止编造 model_id。】\n"
        + snap
    )


async def _maybe_append_capabilities_snapshot_to_system(
    system_prompt: str,
    user_message: str,
    raw_token: str,
    request: Optional[Request],
    has_tools: bool,
) -> str:
    """用户问「有哪些能力」等时，由本机预拉 list_capabilities 注入 system，避免模型不调工具、仍答泛化列表。"""
    if not has_tools or not (user_message or "").strip():
        return system_prompt
    if not _CAPABILITIES_QUESTION_RE.search(user_message.strip()):
        return system_prompt
    try:
        snap = await _exec_tool("list_capabilities", {}, raw_token, sutui_token=None, request=request)
    except Exception as e:
        logger.warning("[对话] 预拉 list_capabilities 失败: %s", e)
        return system_prompt
    if not (snap or "").strip():
        return system_prompt
    if len(snap) > 36000:
        snap = snap[:36000] + "\n…(已截断)"
    logger.info("[对话] 已注入 list_capabilities 快照（能力问答）len=%s", len(snap))
    return (
        system_prompt
        + "\n\n【以下为当前 list_capabilities 完整 JSON（用户正在问能力范围；请据此说明，须包含 "
        "capabilities、other_mcp_tools、integrations_via_app；勿忽略发布类工具与企微/WhatsApp 等集成）】\n"
        + snap
    )


# ── LLM API calls with tool-calling loop ──────────────────────────

_PROVIDER_NAMES = {
    "deepseek": "DeepSeek", "openai": "OpenAI",
    "anthropic": "Anthropic", "google": "Google Gemini",
}

def _raise_api_err(resp: httpx.Response, model: str = ""):
    detail = resp.text[:500]
    try:
        j = resp.json()
        if isinstance(j, dict):
            e = j.get("error")
            if isinstance(e, dict) and e.get("message"):
                detail = str(e.get("message"))
            elif isinstance(j.get("detail"), dict):
                e2 = j["detail"].get("error")
                if isinstance(e2, dict) and e2.get("message"):
                    detail = str(e2.get("message"))
            elif isinstance(j.get("detail"), str):
                detail = j["detail"][:2000]
    except Exception:
        pass
    if resp.status_code in (401, 403):
        provider = (model.split("/", 1)[0] if "/" in model else "").strip().lower()
        # sutui/xxx 走的是认证中心 /api/sutui-chat/completions（Bearer 用户 JWT），不是系统配置里的第三方 Key；
        # 误用「API Key 无效」会让用户以为要填速推 APK/Key，且与生成任务已成功等事实矛盾。
        if provider == "sutui":
            raise HTTPException(
                status_code=resp.status_code,
                detail=(
                    f"速推对话代理返回 {resp.status_code}：{detail}。"
                    "（与「系统配置」中的 DeepSeek/OpenAI 等 API Key 无关；多为登录态失效、账号权限或认证中心拒绝，请重新登录或核对绑定品牌后重试。）"
                ),
            )
        name = _PROVIDER_NAMES.get(provider, provider or "LLM")
        raise HTTPException(
            502,
            detail=f"{name} API Key 无效或未配置，请到「系统配置」页面设置正确的 API Key。",
        )
    if resp.status_code == 402:
        raise HTTPException(status_code=402, detail=detail)
    raise HTTPException(502, detail=f"LLM API 错误 ({resp.status_code}): {detail}")


_DSML_FC_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]function_calls>(.*?)</[\uff5c|]DSML[\uff5c|]function_calls>',
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]invoke\s+name="([^"]+)">(.*?)</[\uff5c|]DSML[\uff5c|]invoke>',
    re.DOTALL,
)
_DSML_PARAM_RE = re.compile(
    r'<[\uff5c|]DSML[\uff5c|]parameter\s+name="([^"]+)"\s+string="(true|false)">(.*?)</[\uff5c|]DSML[\uff5c|]parameter>',
    re.DOTALL,
)

# Kimi / DeepSeek / 部分速推模型在 assistant.content 里输出（非 API 的 tool_calls，亦非 DSML）
# 注意：若仅匹配 tool_calls_begin 而不匹配 redacted_tool_calls_begin，整条流水线不会执行，模型易编造「功能不可用」。
_PIPE_TOOL_CALLS_WRAPPER_RE = re.compile(
    r"<\s*\|\s*(?:tool_calls_begin|redacted_tool_calls_begin)\s*\|\s*>"
    r"(.*?)<\s*\|\s*(?:tool_calls_end|redacted_tool_calls_end)\s*\|\s*>",
    re.DOTALL | re.IGNORECASE,
)
_PIPE_TOOL_CALL_ONE_RE = re.compile(
    r"<\s*\|\s*(?:tool_call_begin|redacted_tool_call_begin_kimi|redacted_tool_call_begin)\s*\|\s*>"
    r"(.*?)<\s*\|\s*(?:tool_call_end|redacted_tool_call_end_kimi|redacted_tool_call_end)\s*\|\s*>",
    re.DOTALL | re.IGNORECASE,
)
# function<|…tool_sep…|>image.generate（DeepSeek 等）
_PIPE_FUNCTION_TOOL_NAME_RE = re.compile(
    r"^function\s*<\s*\|[^|]+\|\s*>\s*(.+)$",
    re.IGNORECASE,
)


def _parse_first_json_dict_from_text(text: str) -> Optional[Dict[str, Any]]:
    """从片段中截取首个 JSON 对象（忽略前后说明与 markdown 代码块）。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```\s*$", "", t)
    i = t.find("{")
    if i < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(t[i:])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _parse_pipe_markup_tool_calls(content: str) -> List[Dict[str, Any]]:
    """
    解析 <|tool_calls_begin|> ... >comfly.xxx ... {"payload":...} 类正文工具块。
    若不调本函数，界面看似「已调用工具」但后端日志仍为 no tool_calls，流水线不会执行。
    """
    out: List[Dict[str, Any]] = []
    for wrap in _PIPE_TOOL_CALLS_WRAPPER_RE.finditer(content or ""):
        inner_wrap = wrap.group(1) or ""
        for one in _PIPE_TOOL_CALL_ONE_RE.finditer(inner_wrap):
            block = (one.group(1) or "").strip()
            if not block:
                continue
            lines = block.split("\n")
            cap_id = ""
            idx = 0
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
            if idx < len(lines):
                first = lines[idx].strip()
                if first.startswith(">"):
                    cap_id = (first[1:].strip().split() or [""])[0]
                    idx += 1
                else:
                    m_fn = _PIPE_FUNCTION_TOOL_NAME_RE.match(first)
                    if m_fn:
                        cap_id = (m_fn.group(1) or "").strip().split()[0]
                        idx += 1
                    elif re.match(
                        r"^(?:invoke_capability|[\w.-]+\.[\w.-]+)$",
                        first.split()[0] if first.split() else "",
                    ):
                        cap_id = first.split()[0]
                        idx += 1
            rest = "\n".join(lines[idx:]).strip()
            payload = _parse_first_json_dict_from_text(rest)
            if payload is None:
                continue
            if cap_id == "invoke_capability" and (payload.get("capability_id") or "").strip():
                out.append({"name": "invoke_capability", "arguments": payload})
            elif cap_id:
                out.append({"name": "invoke_capability", "arguments": {"capability_id": cap_id, "payload": payload}})
            elif (payload.get("capability_id") or "").strip():
                out.append({"name": "invoke_capability", "arguments": payload})
    return out


def _parse_invoke_capability_json_fences(content: str) -> List[Dict[str, Any]]:
    """解析正文中 ```json {"capability_id","payload"} ```（模型未走 API tool_calls 时的退路）。"""
    out: List[Dict[str, Any]] = []
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", content or "", re.IGNORECASE):
        obj = _parse_first_json_dict_from_text(m.group(1) or "")
        if not obj:
            continue
        cid = (obj.get("capability_id") or "").strip()
        pl = obj.get("payload")
        if cid and isinstance(pl, dict):
            out.append({"name": "invoke_capability", "arguments": {"capability_id": cid, "payload": pl}})
    return out


def _strip_pipe_markup_tool_calls(content: str) -> str:
    return _PIPE_TOOL_CALLS_WRAPPER_RE.sub("", content or "").strip()


def _parse_text_tool_calls(content: str) -> List[Dict[str, Any]]:
    """Parse DeepSeek DSML or similar text-embedded tool calls."""
    calls: List[Dict[str, Any]] = []
    for fc_match in _DSML_FC_RE.finditer(content):
        block = fc_match.group(1)
        for inv in _DSML_INVOKE_RE.finditer(block):
            name = inv.group(1)
            body = inv.group(2)
            args: Dict[str, Any] = {}
            for pm in _DSML_PARAM_RE.finditer(body):
                pname, is_str, pvalue = pm.group(1), pm.group(2), pm.group(3).strip()
                if is_str == "false":
                    try:
                        pvalue = json.loads(pvalue)
                    except Exception:
                        pass
                args[pname] = pvalue
            calls.append({"name": name, "arguments": args})
    calls.extend(_parse_pipe_markup_tool_calls(content))
    calls.extend(_parse_invoke_capability_json_fences(content))
    return calls


def _strip_dsml(content: str) -> str:
    """Remove DSML / 正文 <|tool_calls|> 等标记，返回可展示的前言。"""
    cleaned = _strip_pipe_markup_tool_calls(content)
    cleaned = _DSML_FC_RE.sub("", cleaned).strip()
    cleaned = re.sub(r'<[\uff5c|]DSML[\uff5c|][^>]*>', '', cleaned).strip()
    return cleaned


def _sanitize_user_visible_hint(s: str) -> str:
    """去掉面向用户文案中的本机路径、脚本名等（爆款TVC 进度/错误里常见）。"""
    if not (s or "").strip():
        return ""
    t = (s or "").strip()
    t = re.sub(
        r"(?:[A-Za-z]:[/\\][^\s\"'<>|]{2,320}|"
        r"(?:\\\\|/)[^\s\"'<>|]{0,320}(?:[/\\](?:skills|job_runs|runs|Python)[/\\]|\.py\b|\.json\b|manifest\.json)[^\s\"'<>|]{0,320})",
        "…",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\s+", " ", t).strip()
    return t[:400]


def _user_visible_daihuo_pipeline_json_reply(raw: str) -> str:
    """将爆款TVC 工具 JSON 转为简短中文，避免把整段 JSON 或路径写给用户。"""
    t = (raw or "").strip()
    if not t.startswith("{"):
        return ""
    try:
        d = json.loads(t)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    if (d.get("capability_id") or "").strip() != "comfly.veo.daihuo_pipeline":
        return ""
    inner = d.get("result")
    st = ""
    if isinstance(inner, dict):
        st = (inner.get("status") or "").strip().lower()
    ok = d.get("ok", True) is not False
    if st == "completed" and ok:
        saved = inner.get("saved_assets") if isinstance(inner, dict) else []
        n = len(saved) if isinstance(saved, list) else 0
        if n:
            return f"爆款TVC 已完成，共 {n} 条成片已入库（可在素材库查看）。"
        return "爆款TVC 已完成。请在素材库或对话中的视频卡片查看成片。"
    if st == "failed" or not ok:
        err = ""
        if isinstance(inner, dict):
            err = str(inner.get("error") or "").strip()
        if not err:
            err = str(d.get("error") or "").strip()
        err = _sanitize_user_visible_hint(err)
        return ("爆款TVC 未能完成。" + (f" 说明：{err}" if err else ""))[:500]
    if isinstance(inner, dict) and inner.get("poll_timeout"):
        return "爆款TVC 等待时间较长，任务可能仍在后台处理。可稍后再试或刷新页面继续查看进度。"
    return "爆款TVC 仍在生成中。刷新页面可自动恢复进度显示。"


def _reply_for_user(reply: str) -> str:
    """Strip DSML from reply so user never sees raw function_calls; use friendly text if nothing left."""
    r0 = (reply or "").strip()
    if r0.startswith("{") and "comfly.veo.daihuo_pipeline" in r0:
        dh = _user_visible_daihuo_pipeline_json_reply(r0)
        if dh:
            return dh
    out = _strip_dsml(reply or "").strip()
    if not out:
        return "正在处理…"
    return out


# 速推 task.get_result 状态：先判进行中再判终态，避免「未完成」等误判
_TASK_TERMINAL_STATUSES = (
    "success", "completed", "done", "succeeded", "finished",
    "failed", "error", "cancelled", "canceled", "timeout", "expired",
    "已完成", "生成成功", "成功", "完成", "失败", "错误", "取消", "超时",
)
_TASK_IN_PROGRESS_STATUSES = (
    "pending", "queued", "submitted", "processing", "generating", "running",
    "处理中", "生成中", "排队中", "运行中", "上传中", "等待中",
)


def _infer_media_type_from_asset_item(item: Any) -> Optional[str]:
    """无 media_type 字段时，根据 URL 后缀推断。"""
    if not isinstance(item, dict):
        return None
    for k in ("url", "source_url", "preview_url", "image_url", "video_url", "file_url"):
        u = (item.get(k) or "").strip().lower().split("?")[0].split("#")[0]
        if not u:
            continue
        if u.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return "image"
        if u.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi")):
            return "video"
    return None


def _extract_saved_assets_from_task_result(result_text: str) -> List[Dict[str, Any]]:
    """从 task.get_result 正文解析 saved_assets 列表。"""
    if not result_text or not result_text.strip():
        return []
    raw = (result_text or "").strip()
    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        saved = d.get("saved_assets") or (d.get("result") or {}).get("saved_assets")
        if isinstance(saved, list) and saved:
            return [x for x in saved if isinstance(x, dict)]
        upstream = d.get("result")
        if isinstance(upstream, dict):
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        obj = json.loads(t)
                        saved2 = obj.get("saved_assets") or []
                        if isinstance(saved2, list) and saved2:
                            return [x for x in saved2 if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _find_sutui_task_output_dict(obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if isinstance(data, dict) and isinstance(data.get("output"), dict):
        return data["output"]
    if isinstance(obj.get("output"), dict):
        return obj["output"]
    res = obj.get("result")
    if isinstance(res, dict):
        data2 = res.get("data")
        if isinstance(data2, dict) and isinstance(data2.get("output"), dict):
            return data2["output"]
    return None


def _iter_task_json_roots(d: Any) -> List[Dict[str, Any]]:
    """task.get_result 正文可能是多层 JSON 包装（含 MCP content[0].text）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(d, dict):
        return out
    out.append(d)
    r = d.get("result")
    if isinstance(r, dict):
        out.append(r)
        ir = r.get("result")
        if isinstance(ir, dict):
            for c in ir.get("content") or []:
                if isinstance(c, dict):
                    t = (c.get("text") or "").strip()
                    if t.startswith("{"):
                        try:
                            out.append(json.loads(t))
                        except Exception:
                            pass
    return out


def _primary_video_url_from_output(output: Dict[str, Any]) -> Optional[str]:
    v = output.get("video")
    if isinstance(v, str) and v.startswith("http"):
        return v.strip()
    if isinstance(v, dict):
        u = v.get("url")
        if isinstance(u, str) and u.startswith("http"):
            return u.strip()
    if isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                u = item.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    return u.strip()
    vids = output.get("videos")
    if isinstance(vids, list):
        for item in vids:
            if isinstance(item, dict):
                u = item.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    return u.strip()
    return None


def _extract_image_urls_from_generate_result(result_text: str) -> List[Dict[str, Any]]:
    """同步 image.generate：MCP 正文无 saved_assets 时，从 JSON 中收集图片直链，供 SSE saved_assets + 前端 save-url。"""
    if not result_text or not result_text.strip():
        return []
    raw = result_text.strip()
    if not raw.startswith("{"):
        return []
    try:
        d = json.loads(raw)
    except Exception:
        return []

    def is_image_url(u: str) -> bool:
        s = u.split("?")[0].split("#")[0].lower()
        if s.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return True
        if "/mcp-images/" in s or "/v3-tasks/" in s:
            return True
        if "/assets/" in s and not s.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi")):
            return True
        if "fal.media/files" in s or "fal-cdn" in s:
            return True
        return False

    found: List[str] = []
    seen: set = set()

    def walk(obj: Any, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(obj, dict):
            for v in list(obj.values())[:120]:
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:120]:
                walk(x, depth + 1)
        elif isinstance(obj, str):
            u = obj.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                return
            if not is_image_url(u):
                return
            k = u.split("?")[0].split("#")[0].lower()
            if k in seen:
                return
            seen.add(k)
            found.append(u)

    root = d.get("result") if isinstance(d.get("result"), (dict, list)) else d
    walk(root, 0)
    return [{"url": u, "media_type": "image"} for u in found[:8]]


def _extract_video_urls_from_task_result(result_text: str) -> List[Dict[str, Any]]:
    """task.get_result 终态无 saved_assets 时，从 JSON 中收集视频直链，供 SSE saved_assets + 前端 save-url 入库。"""
    if not result_text or not result_text.strip():
        return []
    raw = result_text.strip()
    if not raw.startswith("{"):
        return []
    try:
        d = json.loads(raw)
    except Exception:
        return []

    for root in _iter_task_json_roots(d):
        od = _find_sutui_task_output_dict(root)
        if isinstance(od, dict):
            vu = _primary_video_url_from_output(od)
            if vu:
                return [{"url": vu, "media_type": "video"}]

    def is_video_url(u: str) -> bool:
        s = u.split("?")[0].split("#")[0].lower()
        if s.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv", ".m3u8")):
            return True
        if "/v3-tasks/" in s and (".mp4" in s or ".webm" in s or ".mov" in s):
            return True
        if "fal.media" in s or "fal-cdn" in s:
            if ".mp4" in s or ".webm" in s or "/files/" in s:
                return True
        return False

    found: List[str] = []
    seen: set = set()

    def walk(obj: Any, depth: int) -> None:
        if depth > 24:
            return
        if isinstance(obj, dict):
            for v in list(obj.values())[:120]:
                walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:120]:
                walk(x, depth + 1)
        elif isinstance(obj, str):
            u = obj.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                return
            if not is_video_url(u):
                return
            k = u.split("?")[0].split("#")[0].lower()
            if k in seen:
                return
            seen.add(k)
            found.append(u)

    root = d.get("result") if isinstance(d.get("result"), (dict, list)) else d
    walk(root, 0)
    return [{"url": u, "media_type": "video"} for u in found[:8]]


def _parse_comfly_veo_mcp_tool_result(result_text: str) -> Optional[Dict[str, Any]]:
    """MCP invoke_capability(comfly.veo) 文本为 JSON：{"capability_id":"comfly.veo","result":{...}}。"""
    if not (result_text or "").strip():
        return None
    raw = result_text.strip()
    if not raw.startswith("{"):
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if (d.get("capability_id") or "").strip() != "comfly.veo":
        return None
    r1 = d.get("result")
    if isinstance(r1, str):
        try:
            r1 = json.loads(r1)
        except Exception:
            return None
    if not isinstance(r1, dict):
        return None
    return r1


def _comfly_poll_status_from_upstream(upstream: Dict[str, Any]) -> str:
    if not isinstance(upstream, dict):
        return ""
    s = upstream.get("status")
    if isinstance(s, str) and s.strip():
        return s.strip()
    data = upstream.get("data")
    if isinstance(data, dict):
        s2 = data.get("status")
        if isinstance(s2, str) and s2.strip():
            return s2.strip()
    return ""


def _comfly_poll_status_is_terminal(status: str) -> bool:
    """Comfly GET /v2/videos/generations/{task_id}：终态含 SUCCESS / FAILURE（及常见别名）。"""
    u = (status or "").strip().upper().replace(" ", "_")
    if u in (
        "SUCCESS",
        "SUCCEEDED",
        "COMPLETED",
        "DONE",
        "FINISHED",
        "COMPLETE",
        "FAILURE",
        "FAILED",
        "ERROR",
    ):
        return True
    low = (status or "").strip().lower()
    return low in ("completed", "success", "succeeded", "done", "finished", "failure", "failed", "error")


def _comfly_video_url_from_poll_upstream(upstream: Dict[str, Any]) -> str:
    data = upstream.get("data")
    if not isinstance(data, dict):
        data = {}
    for container in (data, upstream):
        if not isinstance(container, dict):
            continue
        for key in ("output", "video_url", "url"):
            v = container.get(key)
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                return v.strip()
    return ""


def _maybe_register_comfly_veo_submit_hint(result_text: str, invoke_args: Dict[str, Any]) -> None:
    r1 = _parse_comfly_veo_mcp_tool_result(result_text)
    if not r1 or not r1.get("ok", True):
        return
    if (r1.get("action") or "").strip() != "submit_video":
        return
    tid = str(r1.get("task_id") or "").strip()
    if not tid:
        return
    pl_root = invoke_args.get("payload") if isinstance(invoke_args.get("payload"), dict) else {}
    prompt = str(pl_root.get("prompt") or "").strip()
    if not prompt and isinstance(pl_root.get("prompts"), list):
        for x in pl_root["prompts"]:
            if x is None:
                continue
            xs = str(x).strip()
            if xs:
                prompt = xs
                break
    model = str(pl_root.get("video_model") or pl_root.get("model") or "").strip()
    _register_generation_hint_for_task(tid, {"prompt": prompt, "model": model}, "comfly.veo")


def _saved_assets_from_comfly_veo_poll_success(result_text: str) -> List[Dict[str, Any]]:
    """comfly.veo poll_video 上游终态且含视频直链时，供 SSE saved_assets + 前端 save-url。"""
    r1 = _parse_comfly_veo_mcp_tool_result(result_text)
    if not r1 or not r1.get("ok", True):
        return []
    if (r1.get("action") or "").strip() != "poll_video":
        return []
    upstream = r1.get("result")
    if isinstance(upstream, str):
        try:
            upstream = json.loads(upstream)
        except Exception:
            upstream = {}
    if not isinstance(upstream, dict):
        return []
    url = _comfly_video_url_from_poll_upstream(upstream)
    if not url:
        return []
    st = _comfly_poll_status_from_upstream(upstream)
    if st and not _comfly_poll_status_is_terminal(st):
        return []
    tid = str(r1.get("task_id") or upstream.get("task_id") or upstream.get("id") or "").strip()
    return [
        {
            "url": url,
            "source_url": url,
            "media_type": "video",
            "generation_task_id": tid[:128] if tid else "",
            "tags": "auto,comfly.veo",
        }
    ]


def _comfly_veo_parse_poll_upstream(
    result_text: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """解析 poll_video 的 MCP 文本，返回 (后端 r1 壳, Comfly GET body)。"""
    r1 = _parse_comfly_veo_mcp_tool_result(result_text)
    if not r1 or not r1.get("ok", True):
        return None, None
    if (r1.get("action") or "").strip() != "poll_video":
        return r1, None
    upstream = r1.get("result")
    if isinstance(upstream, str):
        try:
            upstream = json.loads(upstream)
        except Exception:
            upstream = {}
    if not isinstance(upstream, dict):
        return r1, None
    return r1, upstream


def _comfly_veo_status_is_failed(status: str) -> bool:
    u = (status or "").strip().upper().replace(" ", "_")
    if u in ("FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "REJECTED", "TIMEOUT", "TIMED_OUT"):
        return True
    low = (status or "").strip().lower()
    return low in ("failed", "failure", "error", "cancelled", "canceled", "rejected", "timeout")


def _comfly_veo_poll_should_continue(result_text: str) -> bool:
    """Comfly 任务仍应继续轮询时为 True（有视频且终态则 False）。"""
    _r1, upstream = _comfly_veo_parse_poll_upstream(result_text)
    if upstream is None:
        return False
    url = _comfly_video_url_from_poll_upstream(upstream)
    st = _comfly_poll_status_from_upstream(upstream)
    if url and (not st or _comfly_poll_status_is_terminal(st)):
        return False
    if _comfly_veo_status_is_failed(st):
        return False
    if st and _comfly_poll_status_is_terminal(st) and not url:
        return False
    return True


def _extract_comfly_veo_task_id_from_submit_result(result_text: str) -> str:
    """submit_video 成功时取出 task_id；兼容 MCP 正文前有说明、Markdown 代码块或非顶格 `{`。"""
    raw_full = (result_text or "").strip()

    def _from_parsed(r1: Optional[Dict[str, Any]]) -> str:
        if not r1 or not r1.get("ok", True):
            return ""
        if (r1.get("action") or "").strip() != "submit_video":
            return ""
        return str(r1.get("task_id") or "").strip()

    t0 = _from_parsed(_parse_comfly_veo_mcp_tool_result(raw_full))
    if t0:
        return t0[:128]
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw_full, re.IGNORECASE):
        inner = (m.group(1) or "").strip()
        if inner:
            t1 = _from_parsed(_parse_comfly_veo_mcp_tool_result(inner))
            if t1:
                return t1[:128]
    idx = raw_full.find("{")
    if idx > 0:
        t2 = _from_parsed(_parse_comfly_veo_mcp_tool_result(raw_full[idx:]))
        if t2:
            return t2[:128]
    m = re.search(r'"task_id"\s*:\s*"(video_[^"\\]+)"', raw_full)
    if m:
        return m.group(1).strip()[:128]
    return ""


def _should_auto_poll_comfly_veo_after_submit(invoke_args: Dict[str, Any], submit_res: str) -> bool:
    if (invoke_args.get("capability_id") or "").strip() != "comfly.veo":
        return False
    # 勿依赖 invoke_args.payload.action：日志中常见模型把参数摊在顶层（如 capability_id+prompt+url），
    # MCP 归一化后仍能 submit 成功；若以 payload 为准会误判为不轮询（见 app.log 09:30:35 tool_call keys）。
    r1 = _parse_comfly_veo_mcp_tool_result(submit_res)
    if not r1 or not r1.get("ok", True):
        return False
    act = (r1.get("action") or "").strip()
    # MCP 已在 invoke 内轮询至终态时，避免与 chat 侧重复轮询
    if act == "poll_video":
        return False
    if act != "submit_video":
        return False
    return bool(str(r1.get("task_id") or "").strip())


def _comfly_veo_poll_result_hint(result_text: str) -> str:
    _r1, upstream = _comfly_veo_parse_poll_upstream(result_text)
    if not upstream:
        return ""
    st = _comfly_poll_status_from_upstream(upstream)
    return (st or "")[:80]


def _comfly_veo_polling_final_progress_event(*, result_text: str, task_id: str) -> Dict[str, Any]:
    """comfly.veo 后台多轮 poll 结束后补发 tool_end（与 task.get_result 轮询补发同理）。"""
    res = result_text or ""
    ev: Dict[str, Any] = {
        "type": "tool_end",
        "name": "invoke_capability",
        "preview": res[:200],
        "capability_id": "comfly.veo",
        "phase": "task_polling",
        "in_progress": False,
        "media_type": "video",
        "success": True,
    }
    sse = _saved_assets_from_comfly_veo_poll_success(res)
    tid = (task_id or "").strip()
    if sse and tid:
        _apply_generation_hints_to_saved_assets(sse, tid)
    if sse:
        ev["saved_assets"] = sse
    else:
        _x, up = _comfly_veo_parse_poll_upstream(res)
        if up is not None:
            st = _comfly_poll_status_from_upstream(up)
            if _comfly_veo_status_is_failed(st):
                ev["success"] = False
    logger.info(
        "[对话轮询] comfly.veo 终态补发 tool_end saved_assets 条数=%s task_id=%s",
        len(sse) if sse else 0,
        tid or "-",
    )
    return ev


def _task_polling_final_progress_event(
    *,
    name: str,
    result_text: str,
    preview_len: int = 200,
    task_id: str = "",
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """轮询结束后补发的 tool_end：带 media_type/saved_assets，避免前端默认「视频已生成」。"""
    res = result_text or ""
    tid = (task_id or "").strip()
    _orig_cap = (_generation_hints_map().get(tid, {}).get("capability_id") or "") if tid else ""
    _is_understand = _orig_cap in ("image.understand", "video.understand")
    ev: Dict[str, Any] = {
        "type": "tool_end",
        "name": name,
        "preview": res[:preview_len],
        "capability_id": "task.get_result",
        "phase": "task_polling",
        "in_progress": False,
    }
    if _is_understand:
        _task_status_lower = _extract_status_for_log(res).strip().lower()
        _task_failed = _task_status_lower in ("failed", "error", "cancelled", "canceled", "timeout", "expired", "失败", "错误", "取消", "超时")
        if _task_failed:
            ev["success"] = False
            logger.info("[对话轮询] task.get_result 终态为理解类能力 cap=%s 但任务失败 status=%s", _orig_cap, _task_status_lower)
        else:
            ev["understand_text"] = (res or "").strip()[:2000]
            ev["media_type"] = "text"
            logger.info("[对话轮询] task.get_result 终态为理解类能力 cap=%s，返回 understand_text 而非 saved_assets", _orig_cap)
        return ev
    ev["media_type"] = _extract_media_type_from_task_result(res)
    saved = _terminal_saved_assets_for_task_result(res)
    if saved and tid:
        _apply_generation_hints_to_saved_assets(saved, tid)
    if saved and db is not None and user_id is not None:
        _enrich_saved_assets_asset_ids_from_db(saved, db, int(user_id))
        _merge_publish_asset_hints(
            [
                (it.get("asset_id") or "").strip()
                for it in saved
                if isinstance(it, dict) and (it.get("asset_id") or "").strip()
            ]
        )
    if saved:
        ev["saved_assets"] = saved
        parts: List[str] = []
        for i, it in enumerate(saved[:16]):
            if not isinstance(it, dict):
                parts.append(f"[{i}]not_dict")
                continue
            aid = (it.get("asset_id") or "").strip()
            u = (it.get("url") or it.get("source_url") or "").strip()
            parts.append(
                f"[{i}]has_asset_id={'Y' if aid else 'N'} mt={(it.get('media_type') or '?')!s} url={u[:100]}{'…' if len(u) > 100 else ''}"
            )
        logger.info(
            "[对话轮询 诊断] task.get_result 终态 SSE saved_assets 条数=%s 明细=%s",
            len(saved),
            " | ".join(parts),
        )
    else:
        logger.info("[对话轮询 诊断] task.get_result 终态 SSE 无 saved_assets（前端不会触发 save-url 批量） result_prefix=%s", res[:400])
    return ev


def _extract_media_type_from_task_result(result_text: str) -> str:
    """从 task.get_result 返回的 JSON 中解析 saved_assets[0].media_type。支持 MCP 嵌套 d.result.result.content[0].text."""
    if not result_text or not result_text.strip():
        return "video"
    raw = (result_text or "").strip()
    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        for key in ("media_type", "output_media_type", "result_media_type"):
            v = d.get(key)
            if isinstance(v, str) and v.strip().lower() in ("image", "video"):
                return v.strip().lower()
        for root in _iter_task_json_roots(d):
            od = _find_sutui_task_output_dict(root)
            if isinstance(od, dict) and _primary_video_url_from_output(od):
                return "video"
        saved = d.get("saved_assets") or (d.get("result") or {}).get("saved_assets")
        if isinstance(saved, list) and saved and isinstance(saved[0], dict):
            mt = (saved[0].get("media_type") or "").strip().lower()
            if mt in ("image", "video"):
                return mt
            guess = _infer_media_type_from_asset_item(saved[0])
            if guess:
                return guess
        upstream = d.get("result")
        if isinstance(upstream, dict):
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        obj = json.loads(t)
                        saved = obj.get("saved_assets") or []
                        if isinstance(saved, list) and saved and isinstance(saved[0], dict):
                            mt = (saved[0].get("media_type") or "").strip().lower()
                            if mt in ("image", "video"):
                                return mt
                            guess = _infer_media_type_from_asset_item(saved[0])
                            if guess:
                                return guess
    except Exception:
        pass
    return "video"


def _extract_status_for_log(result_text: str) -> str:
    """从 task.get_result 返回文本中解析 status，仅用于日志。路径见 docs/图生视频_MCP调用流程与参数.md"""
    if not result_text or not result_text.strip():
        return "?"
    raw = (result_text or "").strip()

    def _get_status(obj: Any) -> str:
        if not isinstance(obj, dict):
            return ""
        s = (obj.get("status") or "").strip()
        if s:
            return s
        res = obj.get("result")
        if isinstance(res, dict):
            content = res.get("content") or []
            for c in content[:3]:
                if isinstance(c, dict):
                    t = (c.get("text") or "").strip()
                    if t.startswith("{"):
                        try:
                            inner = json.loads(t)
                            s = (inner.get("status") or _get_status(inner.get("result") or {}) or "").strip()
                            if s:
                                return s
                        except Exception:
                            pass
        return ""

    try:
        d = json.loads(raw) if raw.startswith("{") else {}
        if not d:
            for part in (raw.split("```") or [raw]):
                part = part.strip()
                if part.startswith("{") and "status" in part:
                    try:
                        d = json.loads(part)
                        break
                    except Exception:
                        pass
        if not d:
            return "?"
        upstream = d.get("result")
        if isinstance(upstream, dict):
            inner_result = upstream.get("result")
            if isinstance(inner_result, dict):
                content = inner_result.get("content") or []
                if content and isinstance(content[0], dict):
                    t = (content[0].get("text") or "").strip()
                    if t.startswith("{"):
                        try:
                            obj = json.loads(t)
                            s = (obj.get("status") or "").strip()
                            if s:
                                return s
                        except Exception:
                            pass
        s = _get_status(d) or _get_status(upstream or {})
        if s:
            return s
        m = re.search(r'"status"\s*:\s*"([^"]*)"', raw)
        if m and m.group(1).strip():
            return m.group(1).strip()
        return "?"
    except Exception:
        pass
    m = re.search(r'"status"\s*:\s*"([^"]*)"', (result_text or ""))
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "?"


def _is_task_result_in_progress(result_text: str) -> bool:
    """True if task.get_result 表示仍在进行中（需继续 15s 轮询）。先判进行中再判终态，避免「未完成」等误判为终态."""
    if not result_text or not result_text.strip():
        return True
    raw = (result_text or "").strip()
    raw_lower = raw.lower()
    if '"saved_assets"' in raw_lower and '"asset_id"' in raw_lower:
        return False
    status_val = _extract_status_for_log(result_text)
    if status_val and status_val != "?":
        s = status_val.strip().lower()
        for term in _TASK_IN_PROGRESS_STATUSES:
            if s == term.lower():
                return True
        for term in _TASK_TERMINAL_STATUSES:
            if s == term.lower():
                return False
        return True
    if '"status":"completed"' in raw_lower or '"status":"success"' in raw_lower or '"status":"failed"' in raw_lower:
        return False
    if "未完成" in raw or "未成功" in raw:
        return True
    for s in _TASK_IN_PROGRESS_STATUSES:
        if s in raw_lower or f'"status":"{s}"' in raw_lower:
            return True
    for s in _TASK_TERMINAL_STATUSES:
        if s not in raw_lower:
            continue
        if s in ("完成", "成功") and ("未完成" in raw or "未成功" in raw):
            continue
        return False
    return True


def _task_result_hint(result_text: str) -> str:
    """从 task.get_result 返回文本中提取一句简短状态，供前端展示「查询结果」。status 与 _extract_status_for_log 同路径."""
    if not result_text or not result_text.strip():
        return ""
    status = _extract_status_for_log(result_text)
    if status and status != "?":
        if _is_task_result_in_progress(result_text):
            return f"当前状态: {status}"
        return f"结果: {status}"
    if _is_task_result_in_progress(result_text):
        return "当前状态: 仍生成中"
    return "结果: 已完成"


def _task_id_token_ok(s: str) -> bool:
    t = (s or "").strip()
    if len(t) < 4:
        return False
    if len(t) > 256:
        return False
    return True


def _task_id_deep_in_obj(obj: Any, _depth: int = 0) -> str:
    """递归取出 task_id / taskId / taskid（与 mcp/http_server._extract_task_id_from_upstream 对齐）。"""
    if _depth > 14 or obj is None:
        return ""
    if isinstance(obj, dict):
        for k in ("task_id", "taskId", "taskid"):
            v = obj.get(k)
            if isinstance(v, str) and _task_id_token_ok(v):
                return v.strip()[:128]
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                sv = str(int(v)) if isinstance(v, float) and v == int(v) else str(v)
                if _task_id_token_ok(sv):
                    return sv[:128]
        for vv in obj.values():
            t = _task_id_deep_in_obj(vv, _depth + 1)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _task_id_deep_in_obj(it, _depth + 1)
            if t:
                return t
    return ""


def _extract_task_id_from_result(result_text: str) -> str:
    """从 video.generate / image.generate / task.get_result 等 MCP 返回文本中解析 task_id。
    上游常带说明文字、Markdown 代码块或非顶格 JSON，旧逻辑仅 raw.startswith('{') 会漏解析。"""
    raw = (result_text or "").strip()
    if not raw:
        return ""
    for m in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE):
        inner = (m.group(1) or "").strip()
        if inner:
            t = _extract_task_id_from_result(inner)
            if t:
                return t
    candidates: List[str] = []
    if raw.startswith("{"):
        candidates.append(raw)
    idx = raw.find("{")
    if idx >= 0 and (not candidates or idx > 0):
        candidates.append(raw[idx:])
    for cand in candidates:
        if not cand:
            continue
        try:
            d = json.loads(cand)
        except Exception:
            continue
        t = _task_id_deep_in_obj(d)
        if t:
            return t
        try:
            tid = str(d.get("task_id") or "").strip()
            if tid and _task_id_token_ok(tid):
                return tid[:128]
            upstream = d.get("result")
            if isinstance(upstream, dict):
                tid = str(upstream.get("task_id") or "").strip()
                if tid and _task_id_token_ok(tid):
                    return tid[:128]
                inner_result = upstream.get("result")
                if isinstance(inner_result, dict):
                    content = inner_result.get("content") or []
                    if content and isinstance(content[0], dict):
                        tx = (content[0].get("text") or "").strip()
                        if tx.startswith("{"):
                            try:
                                obj = json.loads(tx)
                                tid = _task_id_deep_in_obj(obj) or str(obj.get("task_id") or "").strip()
                                if tid and _task_id_token_ok(tid):
                                    return tid[:128]
                            except Exception:
                                pass
        except Exception:
            pass
    m = re.search(r'"(?:task_id|taskId|taskid)"\s*:\s*"([^"\\]+)"', raw)
    if m and _task_id_token_ok(m.group(1)):
        return m.group(1).strip()[:128]
    m = re.search(r'"(?:task_id|taskId|taskid)"\s*:\s*([0-9]{4,})', raw)
    if m:
        return m.group(1).strip()[:128]
    return ""


def _task_id_from_invoke_capability_args(args: Dict[str, Any]) -> str:
    pl = args.get("payload") if isinstance(args.get("payload"), dict) else {}
    return str(pl.get("task_id") or args.get("task_id") or "").strip()


async def _poll_comfly_veo_after_submit(
    invoke_submit_args: Dict[str, Any],
    submit_res: str,
    token: str,
    sutui_token: Optional[str],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    request: Optional[Request],
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """submit_video 成功后由后端自动 poll_video（15s 间隔），不依赖模型下一轮再调工具。"""
    if not _should_auto_poll_comfly_veo_after_submit(invoke_submit_args, submit_res):
        return submit_res
    tid = _extract_comfly_veo_task_id_from_submit_result(submit_res)
    if not tid:
        return submit_res
    logger.info("[CHAT] comfly.veo submit 成功，开始自动 poll_video task_id=%s", tid[:96])
    poll_a: Dict[str, Any] = {
        "capability_id": "comfly.veo",
        "payload": {"action": "poll_video", "task_id": tid},
    }
    res = await _exec_tool(
        "invoke_capability",
        poll_a,
        token,
        sutui_token,
        progress_cb=progress_cb,
        request=request,
        db=db,
        user_id=user_id,
    )
    if not _comfly_veo_poll_should_continue(res):
        return res
    if progress_cb and tid:
        try:
            ev0: Dict[str, Any] = {
                "type": "task_poll",
                "message": "正在查询生成结果…（进度约每 15 秒更新，请稍候）",
                "task_id": tid,
            }
            h0 = _comfly_veo_poll_result_hint(res)
            if h0:
                ev0["result_hint"] = h0
            await progress_cb(ev0)
        except Exception:
            pass
    poll_interval = 15
    max_wait_sec = 35 * 60
    waited = 0
    entered_loop = False
    while waited < max_wait_sec:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        entered_loop = True
        logger.info("[CHAT] comfly.veo poll 轮询 %ds task_id=%s", waited, tid[:96])
        # 须在 _exec_tool 之前推送：get_result / poll 常长阻塞，否则界面会一直卡在上一条进度（如「0 秒」）
        if progress_cb:
            try:
                ev_pre: Dict[str, Any] = {
                    "type": "task_poll",
                    "message": f"正在查询生成结果…（{waited}秒）",
                    "task_id": tid,
                }
                h_pre = _comfly_veo_poll_result_hint(res)
                if h_pre:
                    ev_pre["result_hint"] = h_pre
                await progress_cb(ev_pre)
            except Exception:
                pass
        res = await _exec_tool(
            "invoke_capability",
            poll_a,
            token,
            sutui_token,
            progress_cb=None,
            request=request,
            db=db,
            user_id=user_id,
        )
        if progress_cb:
            try:
                ev_poll: Dict[str, Any] = {
                    "type": "task_poll",
                    "message": f"正在查询生成结果…（{waited}秒）",
                    "task_id": tid,
                }
                hint = _comfly_veo_poll_result_hint(res)
                if hint:
                    ev_poll["result_hint"] = hint
                await progress_cb(ev_poll)
            except Exception:
                pass
        if not _comfly_veo_poll_should_continue(res):
            break
    if progress_cb and entered_loop:
        try:
            await progress_cb(_comfly_veo_polling_final_progress_event(result_text=res, task_id=tid))
            await _maybe_progress_status_generating_reply(progress_cb)
        except Exception:
            pass
    return res


async def _resume_chat_task_poll_only(
    task_id: str,
    raw_token: str,
    current_user: Union[User, _ServerUser],
    db: Session,
    request: Optional[Request],
    progress_cb: Callable[[Dict], Awaitable[None]],
) -> str:
    """页面刷新后恢复：仅轮询直至终态，不再跑整轮 LLM。"""
    tid = (task_id or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    sutui_token: Optional[str] = None
    uid = getattr(current_user, "id", None)
    if tid.startswith("daihuo_"):
        jid = tid[len("daihuo_") :].strip().lower()
        res = await _resume_daihuo_job_poll_only(
            jid, raw_token, current_user, request, progress_cb
        )
        await _maybe_progress_status_generating_reply(progress_cb)
        return res or ""
    if tid.startswith("video_"):
        poll_a: Dict[str, Any] = {
            "capability_id": "comfly.veo",
            "payload": {"action": "poll_video", "task_id": tid},
        }
        res = await _exec_tool(
            "invoke_capability",
            poll_a,
            raw_token,
            sutui_token,
            progress_cb=progress_cb,
            request=request,
            db=db,
            user_id=uid,
        )
        if not _comfly_veo_poll_should_continue(res):
            if progress_cb:
                try:
                    await progress_cb(_comfly_veo_polling_final_progress_event(result_text=res, task_id=tid))
                except Exception:
                    pass
            return res or ""
        poll_interval = 15
        max_wait_sec = 35 * 60
        waited = 0
        entered_loop = False
        while waited < max_wait_sec:
            await asyncio.sleep(poll_interval)
            waited += poll_interval
            entered_loop = True
            logger.info("[CHAT/stream resume] comfly.veo poll %ds task_id=%s", waited, tid[:96])
            if progress_cb:
                try:
                    ev_pre: Dict[str, Any] = {
                        "type": "task_poll",
                        "message": f"正在查询生成结果…（{waited}秒）",
                        "task_id": tid,
                    }
                    h_pre = _comfly_veo_poll_result_hint(res)
                    if h_pre:
                        ev_pre["result_hint"] = h_pre
                    await progress_cb(ev_pre)
                except Exception:
                    pass
            res = await _exec_tool(
                "invoke_capability",
                poll_a,
                raw_token,
                sutui_token,
                progress_cb=None,
                request=request,
                db=db,
                user_id=uid,
            )
            if progress_cb:
                try:
                    ev_poll: Dict[str, Any] = {
                        "type": "task_poll",
                        "message": f"正在查询生成结果…（{waited}秒）",
                        "task_id": tid,
                    }
                    hint = _comfly_veo_poll_result_hint(res)
                    if hint:
                        ev_poll["result_hint"] = hint
                    await progress_cb(ev_poll)
                except Exception:
                    pass
            if not _comfly_veo_poll_should_continue(res):
                break
        if progress_cb and entered_loop:
            try:
                await progress_cb(_comfly_veo_polling_final_progress_event(result_text=res, task_id=tid))
                await _maybe_progress_status_generating_reply(progress_cb)
            except Exception:
                pass
        return res or ""
    poll_a = _normalize_invoke_task_get_result_args(
        {
            "capability_id": "task.get_result",
            "payload": {"task_id": tid},
        }
    )
    res = await _exec_tool(
        "invoke_capability",
        poll_a,
        raw_token,
        sutui_token,
        progress_cb=progress_cb,
        request=request,
        db=db,
        user_id=uid,
    )
    res = await _poll_task_get_result_until_terminal(
        poll_a, res, raw_token, sutui_token, progress_cb, request, db=db, user_id=uid
    )
    return res or ""


async def _poll_task_get_result_until_terminal(
    invoke_args: Dict[str, Any],
    initial_res: str,
    token: str,
    sutui_token: Optional[str],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    request: Optional[Request],
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """task.get_result 首次返回仍「进行中」时，15s 间隔轮询直至终态（与 MCP 长超时配合）。"""
    if (invoke_args.get("capability_id") or "").strip() != "task.get_result":
        return initial_res
    if not _is_task_result_in_progress(initial_res):
        return initial_res
    res = initial_res
    task_id = _task_id_from_invoke_capability_args(invoke_args)
    if progress_cb:
        try:
            ev_immediate: Dict[str, Any] = {
                "type": "task_poll",
                "message": "正在查询生成结果…（进度约每 15 秒更新，请稍候）",
                "result_hint": _task_result_hint(res),
            }
            if task_id:
                ev_immediate["task_id"] = task_id
            await progress_cb(ev_immediate)
        except Exception:
            pass
    poll_interval = 15
    # 慢视频模型（Seedance 等）在速推队列排队可能超过 30 分钟，给足等待时间
    max_wait_sec = 60 * 60
    waited = 0
    entered_poll_loop = False
    while waited < max_wait_sec:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        entered_poll_loop = True
        logger.info("[CHAT] task.get_result 轮询 %ds task_id=%s", waited, task_id or "(无)")
        if progress_cb:
            try:
                ev_pre = {
                    "type": "task_poll",
                    "message": f"正在查询生成结果…（{waited}秒）",
                    "result_hint": _task_result_hint(res),
                }
                if task_id:
                    ev_pre["task_id"] = task_id
                await progress_cb(ev_pre)
            except Exception:
                pass
        res = await _exec_tool(
            "invoke_capability",
            invoke_args,
            token,
            sutui_token,
            progress_cb=None,
            request=request,
            db=db,
            user_id=user_id,
        )
        if progress_cb:
            try:
                ev = {"type": "task_poll", "message": f"正在查询生成结果…（{waited}秒）"}
                if task_id:
                    ev["task_id"] = task_id
                ev["result_hint"] = _task_result_hint(res)
                await progress_cb(ev)
            except Exception:
                pass
        if not _is_task_result_in_progress(res):
            break
    # 仅当「曾处于进行中并进入过轮询」后再补发终态 saved_assets，避免与首次终态的 _exec_tool tool_end 重复
    if progress_cb and entered_poll_loop:
        try:
            await progress_cb(
                _task_polling_final_progress_event(
                    name="invoke_capability",
                    result_text=res or "",
                    task_id=task_id,
                    db=db,
                    user_id=user_id,
                )
            )
            await _maybe_progress_status_generating_reply(progress_cb)
        except Exception:
            pass
    return res


async def _after_generate_auto_task_result(
    invoke_args: Dict[str, Any],
    gen_res: str,
    token: str,
    sutui_token: Optional[str],
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    request: Optional[Request],
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """video/image.generate 或 image/video.understand 返回 task_id 后，由后端自动拉结果并轮询。"""
    _AUTOPOLL_CAPS = ("video.generate", "image.generate", "image.understand", "video.understand")
    cap = (invoke_args.get("capability_id") or "").strip()
    if cap not in _AUTOPOLL_CAPS:
        return gen_res
    tid = _extract_task_id_from_result(gen_res)
    if not tid:
        return gen_res
    _is_understand_cap = cap in ("image.understand", "video.understand")
    pl0 = invoke_args.get("payload") if isinstance(invoke_args.get("payload"), dict) else {}
    _register_generation_hint_for_task(tid, pl0, cap)
    if progress_cb:
        try:
            _cap_cn_map = {
                "image.generate": "图片",
                "video.generate": "视频",
                "image.understand": "图片理解结果",
                "video.understand": "视频理解结果",
            }
            cap_cn = _cap_cn_map.get(cap, "内容")
            await progress_cb(
                {
                    "type": "task_poll",
                    "message": f"正在获取{cap_cn}…（进度约每 15 秒更新，请稍候）",
                    "task_id": tid,
                    "result_hint": "已提交任务，等待结果",
                }
            )
        except Exception:
            pass
    poll_a = _normalize_invoke_task_get_result_args(
        {
            "capability_id": "task.get_result",
            "payload": {"task_id": tid, "capability_id": cap},
        }
    )
    res = await _exec_tool(
        "invoke_capability",
        poll_a,
        token,
        sutui_token,
        progress_cb=progress_cb,
        request=request,
        db=db,
        user_id=user_id,
    )
    final = await _poll_task_get_result_until_terminal(
        poll_a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
    )
    if final and '"error"' not in final:
        if _is_understand_cap:
            final = final.rstrip() + (
                "\n\n[SYSTEM] 理解结果已返回。请用简洁自然的中文告诉用户图片/视频的内容，"
                "禁止 Markdown、禁止列表；"
                "禁止再调用 save_asset、list_assets、task.get_result、search_models、guide 或任何其它工具。"
            )
        else:
            _cap_cn_gen = "图片" if cap == "image.generate" else "视频"
            final = final.rstrip() + (
                f"\n\n[SYSTEM] {_cap_cn_gen}已生成完成且界面可预览。"
                "仅用一句极短中文确认（例如「已完成」），禁止 Markdown、禁止列表、禁止复述 prompt、禁止贴图或长链接说明；"
                "禁止再调用 save_asset、list_assets、task.get_result、search_models、guide 或任何其它工具。"
            )
    return final


async def _post_openai_compat_chat_completions(
    url: str,
    body: Dict[str, Any],
    hdrs: Dict[str, str],
    *,
    timeout: float = 120.0,
    max_attempts: int = 4,
) -> httpx.Response:
    """POST /v1/chat/completions 或速推 /api/sutui-chat/completions。

    线上偶发：对端在返回头之前直接断 TCP（httpx.RemoteProtocolError），常见于大 tools 轮次、
    或与同机其它经代理的并发请求争抢链路。backend.log 曾见约 4s 即断，非读超时满 120s。
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                return await c.post(url, json=body, headers=hdrs)
        except (httpx.RemoteProtocolError, httpx.ConnectError) as e:
            last_exc = e
            logger.warning(
                "[CHAT] OpenAI-compat POST 瞬断，将重试 attempt=%s/%s type=%s url=%s",
                attempt + 1,
                max_attempts,
                type(e).__name__,
                (url or "")[:160],
            )
            if attempt + 1 >= max_attempts:
                raise
            await asyncio.sleep(0.35 * (2**attempt))
    assert last_exc is not None
    raise last_exc


async def _chat_openai(
    msgs: List[Dict],
    cfg: Dict,
    mcp_tools: List[Dict],
    token: str,
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    attachment_urls: Optional[List[str]] = None,
    attachment_asset_ids: Optional[List[str]] = None,
    override_url: Optional[str] = None,
    override_headers: Optional[Dict[str, str]] = None,
    request: Optional[Request] = None,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """OpenAI-compatible chat loop (DeepSeek, OpenAI, Google Gemini)."""
    attachment_urls = attachment_urls or []
    if override_url and override_headers:
        url = override_url
        hdrs = dict(override_headers)
    else:
        base = cfg["base_url"].rstrip("/")
        if "googleapis.com" in base or base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"

        hdrs = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        }
    model = cfg["model_name"]
    use_tools = model not in _NO_TOOL_SUPPORT and bool(mcp_tools)
    _last_user_msg_for_tools = _last_user_content(msgs)
    _user_wants_veo = bool(re.search(r"(?:veo|tvc|带货|爆款)", _last_user_msg_for_tools, re.IGNORECASE))
    _veo_only_tools = {"comfly.veo", "comfly.veo.daihuo_pipeline"}
    oai_tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in mcp_tools
        if _user_wants_veo or t["name"] not in _veo_only_tools
    ] if use_tools else []

    _ACTION_KW = re.compile(
        r"(发布|生成|打开浏览器|帮你|开始|正在|马上|登录|查看素材|查看账号|"
        r"invoke_capability|publish_content|publish_youtube_video|list_youtube_accounts|"
        r"open_account_browser|list_assets|get_creator_publish_data|sync_creator_publish_data|"
        r"出图|作图|制图|图片生成)",
        re.IGNORECASE,
    )
    # 仅当用户当前消息包含操作意图时才强制要求调用工具，避免对「你好」等问候误触发
    _USER_ACTION_KW = re.compile(
        r"(帮我|给我|生成.*图|发.*抖音|发布到|YouTube|youtube|发到YouTube|打开浏览器|登录|查看素材|查看账号|"
        r"invoke_capability|publish_content|publish_youtube_video|list_youtube_accounts|"
        r"open_account_browser|list_assets|生成图片|发布内容|"
        r"发布数据|作品数据|同步.*数据|播放量|get_creator_publish_data|sync_creator_publish_data)",
        re.IGNORECASE,
    )
    # 用户要强生图但正文里未必出现「生成」；助手用「没法出图」「功能不可用」等搪塞且不调工具时需强制 tool_calls 或后端兜底
    _USER_WANTS_IMAGE_RE = re.compile(
        r"(生成|画|出|做|来|帮).{0,12}(图|画|片)|文生图|图生图|生图|插图|海报|壁纸|头像|"
        r"P图|修图|配图|抠图",
        re.IGNORECASE,
    )
    _ASSISTANT_COP_OUT_RE = re.compile(
        r"(暂时|目前|抱歉|对不起).{0,24}(无法|不能|不可用|不支持|没有.{0,6}功能)|"
        r"功能.{0,10}(不可用|无法|暂不可用)|"
        r"(可能|也许).{0,16}(服务器|积分|算力|配置|维护|故障)|"
        r"没法.{0,8}(出图|生图|画图|生成)|"
        r"出不了图|生不了图",
        re.IGNORECASE,
    )
    force_tool_retry_done = False
    _generate_cap_done: set = set()
    _publish_fail_count = 0

    cur = list(msgs)
    _aid_list = attachment_asset_ids if attachment_asset_ids is not None else []
    _max_rounds = _effective_max_tool_rounds()
    for rnd in range(_max_rounds):
        _publish_autofill_ctx.set(
            {
                "messages": list(cur),
                "attachment_urls": list(attachment_urls),
                "attachment_asset_ids": list(_aid_list),
            }
        )
        body: Dict[str, Any] = {"model": model, "messages": cur, "stream": False}
        if oai_tools and rnd < _max_rounds - 1:
            body["tools"] = oai_tools
            body["tool_choice"] = "auto"

        resp = await _post_openai_compat_chat_completions(url, body, hdrs, timeout=120.0)
        if resp.status_code != 200:
            _raise_api_err(resp, model=f"{cfg.get('provider','')}/{cfg.get('model_name','')}")

        choice = (resp.json().get("choices") or [{}])[0]
        msg = choice.get("message", {})
        tcs = msg.get("tool_calls", [])

        if tcs:
            cur.append(msg)
            last_user_content = _last_user_content(cur)
            _round_wants_publish = _openai_round_has_publish_intent(tcs, last_user_content)
            if _round_wants_publish:
                logger.info("[CHAT] publish_intent=True last_user_content=%s", (last_user_content or "")[:200])
            terminal_saved_after_gen: Optional[List[Dict[str, Any]]] = None
            gen_cap_for_reply = ""
            for tc in tcs:
                fn = tc.get("function", {})
                try:
                    a = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    a = {}
                _mismatch_err = None
                if fn.get("name") == "invoke_capability":
                    a = _correct_video_to_image_if_user_asked_image(a, last_user_content)
                    _mismatch_err = _detect_model_capability_mismatch(a)
                    if not _mismatch_err:
                        _inject_video_media_urls(a, attachment_urls)
                        _infer_video_model_from_user_text(a, last_user_content, bool(attachment_urls))
                        _resolve_video_payload_asset_ids_to_urls(a, request, db, user_id)
                        _normalize_model_alias(a)
                        _ensure_image_generate_default_model(a)
                        _ensure_video_generate_default_model(a)
                        _ensure_image_generate_prompt_and_aspect(a, last_user_content)
                        _ensure_daihuo_pipeline_asset_or_url(a, attachment_asset_ids, attachment_urls)
                logger.info("[CHAT] tool_call: %s(%s)", fn.get("name"), list(a.keys()))
                if fn.get("name") == "publish_content" and _publish_fail_count >= 1:
                    logger.warning("[CHAT] publish_content 已失败 %d 次，拦截重试", _publish_fail_count)
                    res = json.dumps(
                        {"error": "发布已经失败过，请不要再重试。请直接告诉用户发布未成功，稍后可手动重试。"},
                        ensure_ascii=False,
                    )
                    cur.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": res})
                    continue
                if fn.get("name") == "invoke_capability":
                    _cap_id = (a.get("capability_id") or "").strip()
                    if not _cap_id:
                        _pl_inner = a.get("payload") if isinstance(a.get("payload"), dict) else {}
                        _cap_id = (_pl_inner.get("capability_id") or "").strip()
                else:
                    _cap_id = ""
                if fn.get("name") == "invoke_capability" and _cap_id == "media.edit":
                    pl0 = a.get("payload") if isinstance(a.get("payload"), dict) else {}
                    logger.info(
                        "[CHAT] media.edit payload operation=%s asset_id=%s",
                        pl0.get("operation"),
                        pl0.get("asset_id"),
                    )
                if _mismatch_err:
                    logger.warning("[CHAT] 模型/能力类型不匹配: %s", _mismatch_err)
                    res = json.dumps({"error": _mismatch_err}, ensure_ascii=False)
                elif _cap_id in _generate_cap_done:
                    logger.warning("[CHAT] 拦截重复 %s 调用 rnd=%d（本轮已调用或取消过）", _cap_id, rnd)
                    res = '{"error": "本轮对话已调用或取消过 ' + _cap_id + '，禁止重复调用。请直接回复用户。"}'
                else:
                    res = await _exec_tool(
                        fn.get("name", ""),
                        a,
                        token,
                        sutui_token,
                        progress_cb=progress_cb,
                        request=request,
                        db=db,
                        user_id=user_id,
                    )
                    if _cap_id in ("image.generate", "video.generate", "sutui.search_models", "sutui.guide") and res and '"error"' not in res:
                        _generate_cap_done.add(_cap_id)
                if fn.get("name") == "invoke_capability" and (a.get("capability_id") or "").strip() == "media.edit":
                    logger.info(
                        "[CHAT] media.edit result preview=%s",
                        (res or "")[:800],
                    )
                    if res and '"error"' not in res:
                        res = res.rstrip() + '\n\n[SYSTEM] 素材编辑已完成。请立即向用户展示结果（asset_id 和预览链接），不要再调用其他工具、不要发布、不要理解图片。'
                if fn.get("name") == "invoke_capability":
                    res = await _after_generate_auto_task_result(
                        a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if fn.get("name") == "invoke_capability" and _should_auto_poll_comfly_veo_after_submit(a, res):
                    res = await _poll_comfly_veo_after_submit(
                        a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if (
                    fn.get("name") == "invoke_capability"
                    and (a.get("capability_id") or "").strip() == "task.get_result"
                    and _is_task_result_in_progress(res)
                ):
                    res = await _poll_task_get_result_until_terminal(
                        a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                cur.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": res,
                })
                if fn.get("name") == "publish_content" and res and "执行失败" in res:
                    _publish_fail_count += 1
                    logger.info("[CHAT] publish_content 失败计数: %d", _publish_fail_count)
                # 记录「生成类」终态 saved_assets，供本轮结束后决定是否提前返回（须排除「生成并发布」同句编排）。
                if (
                    fn.get("name") == "invoke_capability"
                    and _cap_id in ("image.generate", "video.generate")
                    and res
                    and '"error"' not in res
                ):
                    try:
                        sse_saved = _terminal_saved_assets_for_task_result(res or "")
                    except Exception as _e_saved:
                        logger.warning("[CHAT] early_finish saved_assets 提取异常: %s", _e_saved)
                        sse_saved = []
                    logger.info(
                        "[CHAT] early_finish 诊断: cap=%s sse_saved_count=%s has_system=%s res_len=%s",
                        _cap_id,
                        len(sse_saved) if sse_saved else 0,
                        "\\n\\n[SYSTEM]" in (res or ""),
                        len(res or ""),
                    )
                    if sse_saved:
                        terminal_saved_after_gen = sse_saved
                        gen_cap_for_reply = _cap_id
            # ── publish_content 成功后提前结束（避免再调 LLM 导致 ReadTimeout） ──
            _round_publish_ok = False
            for _tc_check in tcs:
                _fn_check = _tc_check.get("function", {})
                _tc_res = None
                for _m in cur:
                    if _m.get("role") == "tool" and _m.get("tool_call_id") == _tc_check.get("id"):
                        _tc_res = _m.get("content", "")
                        break
                if (
                    _fn_check.get("name") == "publish_content"
                    and _tc_res is not None
                    and "执行失败" not in _tc_res
                    and '"status": "success"' in _tc_res
                ):
                    _round_publish_ok = True
                    break
            logger.info(
                "[CHAT] early_finish 条件: enabled=%s publish=%s saved=%s cap=%s publish_ok=%s",
                _lobster_chat_generation_early_finish_enabled(),
                _round_wants_publish,
                bool(terminal_saved_after_gen),
                gen_cap_for_reply,
                _round_publish_ok,
            )
            if _round_publish_ok and _lobster_chat_generation_early_finish_enabled():
                logger.info("[CHAT] early_finish 触发: publish_content 成功，跳过后续 LLM 轮次")
                return "已完成。" if _lobster_chat_generation_reply_minimal() else "内容已发布成功。"
            if (
                _lobster_chat_generation_early_finish_enabled()
                and not _round_wants_publish
                and terminal_saved_after_gen
                and gen_cap_for_reply in ("image.generate", "video.generate")
            ):
                logger.info("[CHAT] early_finish 触发: cap=%s", gen_cap_for_reply)
                sse_saved = terminal_saved_after_gen
                if db is not None and user_id is not None:
                    try:
                        _enrich_saved_assets_asset_ids_from_db(sse_saved, db, int(user_id))
                    except Exception:
                        pass
                kind = "图片" if gen_cap_for_reply == "image.generate" else "视频"
                first = sse_saved[0] if isinstance(sse_saved[0], dict) else {}
                aid = (first.get("asset_id") or "").strip() if isinstance(first, dict) else ""
                url = (first.get("source_url") or first.get("url") or "").strip() if isinstance(first, dict) else ""
                lines = [_early_finish_generation_user_reply(kind)]
                if not _lobster_chat_generation_reply_minimal():
                    if aid:
                        lines.append(f"asset_id: {aid}")
                    if url:
                        lines.append(f"预览: {url}")
                return "\n".join(lines).strip()
            _cc = _cost_cancelled_caps_ctx.get()
            if _cc and not _schedule_orchestration_active.get():
                logger.info("[CHAT] 积分确认被取消（%s），跳过后续轮次", _cc)
                return "操作已取消。"
            continue

        content = (msg.get("content") or "").strip()
        logger.info("[CHAT] rnd=%d no tool_calls, content_len=%d", rnd, len(content))

        text_calls = _parse_text_tool_calls(content) if content else []
        if text_calls and rnd < _max_rounds - 1:
            logger.info("[CHAT] parsed %d text-embedded tool calls (round %d)", len(text_calls), rnd)
            preamble = _strip_dsml(content)
            cur.append({"role": "assistant", "content": preamble or "正在调用工具..."})
            results = []
            last_user_content = _last_user_content(cur)
            round_wants_publish_tc = _user_text_requests_publish(last_user_content)
            terminal_saved_after_gen_tc: Optional[List[Dict[str, Any]]] = None
            gen_cap_for_reply_tc = ""
            for tc_info in text_calls:
                if not isinstance(tc_info.get("arguments"), dict):
                    tc_info["arguments"] = {}
                _mismatch_err_tc = None
                if tc_info["name"] == "invoke_capability":
                    tc_info["arguments"] = _correct_video_to_image_if_user_asked_image(tc_info["arguments"], last_user_content)
                    _mismatch_err_tc = _detect_model_capability_mismatch(tc_info["arguments"])
                    if not _mismatch_err_tc:
                        _inject_video_media_urls(tc_info["arguments"], attachment_urls)
                        _infer_video_model_from_user_text(tc_info["arguments"], last_user_content, bool(attachment_urls))
                        _resolve_video_payload_asset_ids_to_urls(tc_info["arguments"], request, db, user_id)
                        _normalize_model_alias(tc_info["arguments"])
                        _ensure_image_generate_default_model(tc_info["arguments"])
                        _ensure_video_generate_default_model(tc_info["arguments"])
                        _ensure_image_generate_prompt_and_aspect(tc_info["arguments"], last_user_content)
                        _ensure_daihuo_pipeline_asset_or_url(tc_info["arguments"], attachment_asset_ids, attachment_urls)
                logger.info("[CHAT] text_tool_call: %s(%s)", tc_info["name"], list(tc_info["arguments"].keys()))
                ta = tc_info["arguments"]
                if not isinstance(ta, dict):
                    ta = {}
                _nm_tc = (tc_info.get("name") or "").strip()
                round_wants_publish_tc = round_wants_publish_tc or _openai_tool_call_requests_publish(_nm_tc, ta)
                if _nm_tc == "publish_content" and _publish_fail_count >= 1:
                    logger.warning("[CHAT] text_calls publish_content 已失败 %d 次，拦截重试", _publish_fail_count)
                    results.append(f"[{tc_info['name']}] " + json.dumps(
                        {"error": "发布已经失败过，请不要再重试。请直接告诉用户发布未成功，稍后可手动重试。"},
                        ensure_ascii=False,
                    ))
                    continue
                if tc_info["name"] == "invoke_capability":
                    _tc_cap = (ta.get("capability_id") or "").strip()
                    if not _tc_cap:
                        _pl_inner_tc = ta.get("payload") if isinstance(ta.get("payload"), dict) else {}
                        _tc_cap = (_pl_inner_tc.get("capability_id") or "").strip()
                else:
                    _tc_cap = ""
                if tc_info["name"] == "invoke_capability" and _tc_cap == "media.edit":
                    pl1 = ta.get("payload") if isinstance(ta.get("payload"), dict) else {}
                    logger.info(
                        "[CHAT] media.edit payload operation=%s asset_id=%s",
                        pl1.get("operation"),
                        pl1.get("asset_id"),
                    )
                if _mismatch_err_tc:
                    logger.warning("[CHAT] 模型/能力类型不匹配: %s", _mismatch_err_tc)
                    res = json.dumps({"error": _mismatch_err_tc}, ensure_ascii=False)
                elif _tc_cap in _generate_cap_done:
                    logger.warning("[CHAT] 拦截重复 %s(text_calls) rnd=%d", _tc_cap, rnd)
                    res = '{"error": "本轮对话已调用或取消过 ' + _tc_cap + '，禁止重复调用。请直接回复用户。"}'
                else:
                    res = await _exec_tool(
                        tc_info["name"],
                        tc_info["arguments"],
                        token,
                        sutui_token,
                        progress_cb=progress_cb,
                        request=request,
                        db=db,
                        user_id=user_id,
                    )
                    if _tc_cap in ("image.generate", "video.generate") and res and '"error"' not in res:
                        _generate_cap_done.add(_tc_cap)
                if tc_info["name"] == "invoke_capability" and (ta.get("capability_id") or "").strip() == "media.edit":
                    logger.info(
                        "[CHAT] media.edit result preview=%s",
                        (res or "")[:800],
                    )
                    if res and '"error"' not in res:
                        res = res.rstrip() + '\n\n[SYSTEM] 素材编辑已完成。请立即向用户展示结果（asset_id 和预览链接），不要再调用其他工具、不要发布、不要理解图片。'
                if tc_info["name"] == "invoke_capability":
                    res = await _after_generate_auto_task_result(
                        tc_info["arguments"], res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if tc_info["name"] == "invoke_capability" and _should_auto_poll_comfly_veo_after_submit(
                    tc_info["arguments"], res
                ):
                    res = await _poll_comfly_veo_after_submit(
                        tc_info["arguments"],
                        res,
                        token,
                        sutui_token,
                        progress_cb,
                        request,
                        db=db,
                        user_id=user_id,
                    )
                if (
                    tc_info["name"] == "invoke_capability"
                    and (tc_info["arguments"].get("capability_id") or "").strip() == "task.get_result"
                    and _is_task_result_in_progress(res)
                ):
                    res = await _poll_task_get_result_until_terminal(
                        tc_info["arguments"], res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if (
                    tc_info["name"] == "invoke_capability"
                    and _tc_cap in ("image.generate", "video.generate")
                    and res
                    and '"error"' not in res
                ):
                    try:
                        sse_saved_tc = _terminal_saved_assets_for_task_result(res or "")
                    except Exception:
                        sse_saved_tc = []
                    if sse_saved_tc:
                        terminal_saved_after_gen_tc = sse_saved_tc
                        gen_cap_for_reply_tc = _tc_cap
                if tc_info["name"] == "publish_content" and res and "执行失败" in res:
                    _publish_fail_count += 1
                    logger.info("[CHAT] text_calls publish_content 失败计数: %d", _publish_fail_count)
                results.append(f"[{tc_info['name']}] {res}")
            # publish_content 成功后也应提前结束（text_calls 路径）
            _tc_publish_ok = any(
                tc_info["name"] == "publish_content"
                and f"[{tc_info['name']}]" in r
                and "执行失败" not in r
                and '"status": "success"' in r
                for tc_info, r in zip(text_calls, results)
                if tc_info["name"] == "publish_content"
            )
            if _tc_publish_ok and _lobster_chat_generation_early_finish_enabled():
                logger.info("[CHAT] early_finish 触发: text_calls publish_content 成功")
                return "已完成。" if _lobster_chat_generation_reply_minimal() else "内容已发布成功。"
            # 与原生 tool_calls 一致：正文里嵌工具（速推常见）时也要能提前结束，否则会再跑一轮 LLM → 重复 save/list + 长文案
            if (
                _lobster_chat_generation_early_finish_enabled()
                and not round_wants_publish_tc
                and terminal_saved_after_gen_tc
                and gen_cap_for_reply_tc in ("image.generate", "video.generate")
            ):
                sse_saved = terminal_saved_after_gen_tc
                if db is not None and user_id is not None:
                    try:
                        _enrich_saved_assets_asset_ids_from_db(sse_saved, db, int(user_id))
                    except Exception:
                        pass
                kind = "图片" if gen_cap_for_reply_tc == "image.generate" else "视频"
                first = sse_saved[0] if isinstance(sse_saved[0], dict) else {}
                aid = (first.get("asset_id") or "").strip() if isinstance(first, dict) else ""
                url = (first.get("source_url") or first.get("url") or "").strip() if isinstance(first, dict) else ""
                lines = [_early_finish_generation_user_reply(kind)]
                if not _lobster_chat_generation_reply_minimal():
                    if aid:
                        lines.append(f"asset_id: {aid}")
                    if url:
                        lines.append(f"预览: {url}")
                cur.append(
                    {
                        "role": "user",
                        "content": "工具调用结果:\n"
                        + "\n\n".join(results)
                        + "\n\n[SYSTEM] 生成已结束，请不要再调用任何工具；仅用一句极短中文回复用户。",
                    }
                )
                return "\n".join(lines).strip()
            cur.append({"role": "user", "content": "工具调用结果:\n" + "\n\n".join(results) + "\n\n请根据以上结果回答用户。"})
            _cc2 = _cost_cancelled_caps_ctx.get()
            if _cc2 and not _schedule_orchestration_active.get():
                logger.info("[CHAT] 积分确认被取消（text_calls, %s），跳过后续轮次", _cc2)
                return "操作已取消。"
            continue

        # 编排模式下 round 0 无 tool_calls 时无条件强制重试（tool_choice=required），不依赖正则匹配
        _is_sched_orch_round = _schedule_orchestration_active.get()
        last_user_msg = ""
        for m in reversed(cur):
            if m.get("role") == "user":
                last_user_msg = (m.get("content") or "").strip()
                break
        _force_assistant_side = bool(_ACTION_KW.search(content) or _ASSISTANT_COP_OUT_RE.search(content))
        _force_user_side = bool(_USER_ACTION_KW.search(last_user_msg) or _USER_WANTS_IMAGE_RE.search(last_user_msg))
        if (
            oai_tools
            and rnd == 0
            and not force_tool_retry_done
            and (_is_sched_orch_round or (_force_assistant_side and _force_user_side))
        ):
            logger.warning(
                "[CHAT] LLM replied with action text but NO tool_call (user asked for action). "
                "Retrying with tool_choice=required. Content preview: %s",
                content[:200],
            )
            force_tool_retry_done = True
            cur.append({"role": "assistant", "content": content})
            cur.append({
                "role": "user",
                "content": (
                    "你刚才只回复了文字，没有调用任何工具。"
                    "请立即调用对应的工具来执行操作（如 publish_content、invoke_capability、open_account_browser 等），"
                    "不要只用文字描述。"
                ),
            })
            body_retry: Dict[str, Any] = {
                "model": model, "messages": cur, "stream": False,
                "tools": oai_tools, "tool_choice": "required",
            }
            resp2 = await _post_openai_compat_chat_completions(url, body_retry, hdrs, timeout=120.0)
            if resp2.status_code == 200:
                choice2 = (resp2.json().get("choices") or [{}])[0]
                msg2 = choice2.get("message", {})
                tcs2 = msg2.get("tool_calls", [])
                if tcs2:
                    logger.info("[CHAT] forced retry produced %d tool_calls", len(tcs2))
                    cur.append(msg2)
                    last_user_content = _last_user_content(cur)
                    for tc in tcs2:
                        fn = tc.get("function", {})
                        try:
                            a = json.loads(fn.get("arguments", "{}"))
                        except Exception:
                            a = {}
                        _mismatch_err_fc = None
                        if fn.get("name") == "invoke_capability":
                            a = _correct_video_to_image_if_user_asked_image(a, last_user_content)
                            _mismatch_err_fc = _detect_model_capability_mismatch(a)
                            if not _mismatch_err_fc:
                                _inject_video_media_urls(a, attachment_urls)
                                _infer_video_model_from_user_text(a, last_user_content, bool(attachment_urls))
                                _resolve_video_payload_asset_ids_to_urls(a, request, db, user_id)
                                _normalize_model_alias(a)
                                _ensure_image_generate_default_model(a)
                                _ensure_video_generate_default_model(a)
                                _ensure_image_generate_prompt_and_aspect(a, last_user_content)
                                _ensure_daihuo_pipeline_asset_or_url(a, attachment_asset_ids, attachment_urls)
                        logger.info("[CHAT] tool_call(forced): %s(%s)", fn.get("name"), list(a.keys()))
                        _fc_cap = (a.get("capability_id") or "").strip() if fn.get("name") == "invoke_capability" else ""
                        if _mismatch_err_fc:
                            logger.warning("[CHAT] 模型/能力类型不匹配(forced): %s", _mismatch_err_fc)
                            res = json.dumps({"error": _mismatch_err_fc}, ensure_ascii=False)
                        elif _fc_cap in _generate_cap_done:
                            logger.warning("[CHAT] 拦截重复 %s(forced) rnd=%d", _fc_cap, rnd)
                            res = '{"error": "本轮对话已调用或取消过 ' + _fc_cap + '，禁止重复调用。请直接回复用户。"}'
                        else:
                            res = await _exec_tool(
                                fn.get("name", ""),
                                a,
                                token,
                                sutui_token,
                                progress_cb=progress_cb,
                                request=request,
                                db=db,
                                user_id=user_id,
                            )
                            if _fc_cap in ("image.generate", "video.generate") and res and '"error"' not in res:
                                _generate_cap_done.add(_fc_cap)
                        if fn.get("name") == "invoke_capability":
                            res = await _after_generate_auto_task_result(
                                a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                            )
                        if fn.get("name") == "invoke_capability" and _should_auto_poll_comfly_veo_after_submit(a, res):
                            res = await _poll_comfly_veo_after_submit(
                                a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                            )
                        if (
                            fn.get("name") == "invoke_capability"
                            and (a.get("capability_id") or "").strip() == "task.get_result"
                            and _is_task_result_in_progress(res)
                        ):
                            res = await _poll_task_get_result_until_terminal(
                                a, res, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                            )
                        cur.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": res,
                        })
                    continue
                else:
                    logger.warning("[CHAT] forced retry still no tool_calls")

        if (
            oai_tools
            and rnd == 0
            and _USER_WANTS_IMAGE_RE.search(last_user_msg)
            and (_ASSISTANT_COP_OUT_RE.search(content) or not (content or "").strip())
        ):
            logger.warning(
                "[CHAT] user asked for image but model cop-out without tools; server-side image.generate "
                "(preview=%s)",
                (content or "")[:160],
            )
            auto_args: Dict[str, Any] = {"capability_id": "image.generate", "payload": {}}
            auto_args = _correct_video_to_image_if_user_asked_image(auto_args, last_user_msg)
            _inject_video_media_urls(auto_args, attachment_urls)
            _infer_video_model_from_user_text(auto_args, last_user_msg, bool(attachment_urls))
            _resolve_video_payload_asset_ids_to_urls(auto_args, request, db, user_id)
            _normalize_model_alias(auto_args)
            _ensure_image_generate_default_model(auto_args)
            _ensure_video_generate_default_model(auto_args)
            _ensure_image_generate_prompt_and_aspect(auto_args, last_user_msg)
            _ensure_daihuo_pipeline_asset_or_url(auto_args, attachment_asset_ids, attachment_urls)
            res = await _exec_tool(
                "invoke_capability",
                auto_args,
                token,
                sutui_token,
                progress_cb=progress_cb,
                request=request,
                db=db,
                user_id=user_id,
            )
            res = await _after_generate_auto_task_result(
                auto_args,
                res,
                token,
                sutui_token,
                progress_cb,
                request,
                db=db,
                user_id=user_id,
            )
            _fb_cap = (auto_args.get("capability_id") or "").strip()
            if _fb_cap in ("image.generate", "video.generate") and res and '"error"' not in res:
                _generate_cap_done.add(_fb_cap)
            cur.append({"role": "assistant", "content": content or "（系统已代为发起文生图，以下为工具返回。）"})
            cur.append({
                "role": "user",
                "content": (
                    "工具调用结果:\n[invoke_capability] "
                    + res
                    + "\n\n请根据以上结果回答用户；若成功应给出可访问的图片链或素材说明；"
                    "若失败须引用错误原文，勿再笼统说「功能不可用」。"
                ),
            })
            continue

        if oai_tools and rnd == 0:
            _FAKE_PATTERN = re.compile(
                r"(已为你|已成功|已生成|已发布|发布成功|生成完成|"
                r"!\[.*\]\(https?://|https?://.*\.(jpg|png|mp4))",
                re.IGNORECASE,
            )
            if _FAKE_PATTERN.search(content):
                logger.warning("[CHAT] possible fabricated result (no tools called): %s", content[:300])
                try:
                    logs = _pending_tool_logs.get()
                except LookupError:
                    logs = []
                if not logs:
                    content += "\n\n⚠️ 注意：以上回复可能是AI模拟的结果，并非真实执行。如需真正执行操作，请再次明确告诉我。"

        return _reply_for_user(content)

    return "（工具调用轮数已达上限）"


async def _chat_anthropic(
    msgs: List[Dict],
    cfg: Dict,
    mcp_tools: List[Dict],
    token: str,
    sutui_token: Optional[str] = None,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    attachment_urls: Optional[List[str]] = None,
    attachment_asset_ids: Optional[List[str]] = None,
    request: Optional[Request] = None,
    db: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """Anthropic Messages API chat loop."""
    attachment_urls = attachment_urls or []
    hdrs = {
        "Content-Type": "application/json",
        "x-api-key": cfg["api_key"],
        "anthropic-version": "2023-06-01",
    }
    sys_text = ""
    ant_msgs: List[Dict] = []
    for m in msgs:
        if m["role"] == "system":
            sys_text = m["content"]
        elif m["role"] in ("user", "assistant"):
            ant_msgs.append({"role": m["role"], "content": m["content"]})

    ant_tools = [
        {
            "name": t["name"],
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
        }
        for t in mcp_tools
    ]

    _aid_list_a = attachment_asset_ids if attachment_asset_ids is not None else []
    _max_rounds_a = _effective_max_tool_rounds()
    _generate_cap_done_a: set = set()
    for rnd in range(_max_rounds_a):
        _publish_autofill_ctx.set(
            {
                "messages": list(ant_msgs),
                "attachment_urls": list(attachment_urls),
                "attachment_asset_ids": list(_aid_list_a),
            }
        )
        body: Dict[str, Any] = {
            "model": cfg["model_name"],
            "max_tokens": 4096,
            "messages": ant_msgs,
        }
        if sys_text:
            body["system"] = sys_text
        if ant_tools and rnd < _max_rounds_a - 1:
            body["tools"] = ant_tools

        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as c:
            resp = await c.post(
                "https://api.anthropic.com/v1/messages", json=body, headers=hdrs,
            )
        if resp.status_code != 200:
            _raise_api_err(resp, model="anthropic/" + cfg.get("model_name", ""))

        data = resp.json()
        blocks = data.get("content", [])
        tus = [b for b in blocks if b.get("type") == "tool_use"]

        if tus and data.get("stop_reason") == "tool_use":
            ant_msgs.append({"role": "assistant", "content": blocks})
            results = []
            last_user_content = _last_user_content(ant_msgs)
            for tu in tus:
                inp = dict(tu.get("input") or {})
                _mismatch_err_a = None
                if tu.get("name") == "invoke_capability":
                    inp = _correct_video_to_image_if_user_asked_image(inp, last_user_content)
                    _mismatch_err_a = _detect_model_capability_mismatch(inp)
                    if not _mismatch_err_a:
                        _inject_video_media_urls(inp, attachment_urls)
                        _infer_video_model_from_user_text(inp, last_user_content, bool(attachment_urls))
                        _resolve_video_payload_asset_ids_to_urls(inp, request, db, user_id)
                        _normalize_model_alias(inp)
                        _ensure_image_generate_default_model(inp)
                        _ensure_video_generate_default_model(inp)
                        _ensure_image_generate_prompt_and_aspect(inp, last_user_content)
                        _ensure_daihuo_pipeline_asset_or_url(inp, attachment_asset_ids, attachment_urls)
                logger.info("tool_call: %s", tu["name"])
                _a_cap = (inp.get("capability_id") or "").strip() if tu.get("name") == "invoke_capability" else ""
                if _mismatch_err_a:
                    logger.warning("[CHAT-ANT] 模型/能力类型不匹配: %s", _mismatch_err_a)
                    r = json.dumps({"error": _mismatch_err_a}, ensure_ascii=False)
                elif _a_cap in _generate_cap_done_a:
                    logger.warning("[CHAT-ANT] 拦截重复 %s rnd=%d", _a_cap, rnd)
                    r = '{"error": "本轮对话已调用或取消过 ' + _a_cap + '，禁止重复调用。请直接回复用户。"}'
                else:
                    r = await _exec_tool(
                        tu["name"],
                        inp,
                        token,
                        sutui_token,
                        progress_cb=progress_cb,
                        request=request,
                        db=db,
                        user_id=user_id,
                    )
                    if _a_cap in ("image.generate", "video.generate", "sutui.search_models", "sutui.guide") and r and '"error"' not in r:
                        _generate_cap_done_a.add(_a_cap)
                if tu.get("name") == "invoke_capability" and _a_cap == "media.edit":
                    logger.info("[CHAT-ANT] media.edit result preview=%s", (r or "")[:800])
                    if r and '"error"' not in r:
                        r = r.rstrip() + '\n\n[SYSTEM] 素材编辑已完成。请立即向用户展示结果（asset_id 和预览链接），不要再调用其他工具、不要发布、不要理解图片。'
                if tu.get("name") == "invoke_capability":
                    r = await _after_generate_auto_task_result(
                        inp, r, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if tu.get("name") == "invoke_capability" and _should_auto_poll_comfly_veo_after_submit(inp, r):
                    r = await _poll_comfly_veo_after_submit(
                        inp, r, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                if (
                    tu.get("name") == "invoke_capability"
                    and (inp.get("capability_id") or "").strip() == "task.get_result"
                    and _is_task_result_in_progress(r)
                ):
                    r = await _poll_task_get_result_until_terminal(
                        inp, r, token, sutui_token, progress_cb, request, db=db, user_id=user_id
                    )
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu["id"],
                    "content": r,
                })
            _cc_a = _cost_cancelled_caps_ctx.get()
            if _cc_a and not _schedule_orchestration_active.get():
                logger.info("[CHAT-ANT] 积分确认被取消（%s），跳过后续轮次", _cc_a)
                return "操作已取消。"
            ant_msgs.append({"role": "user", "content": results})
            continue

        text_parts = [
            b.get("text", "") for b in blocks if b.get("type") == "text"
        ]
        return "\n".join(text_parts).strip() or "（无回复内容）"

    return "（工具调用轮数已达上限）"


# ── OpenClaw Gateway fallback ─────────────────────────────────────

def _openclaw_gateway_configured() -> bool:
    oc_base = (settings.openclaw_gateway_url or "").strip().rstrip("/")
    oc_token = (settings.openclaw_gateway_token or "").strip()
    return bool(oc_base and oc_token)


def _openclaw_only_chat_enabled() -> bool:
    return bool(getattr(settings, "lobster_openclaw_only_chat", False))


def _openclaw_chat_prefix_patterns() -> List[str]:
    raw = (getattr(settings, "lobster_openclaw_chat_prefixes", None) or "/openclaw").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return sorted(parts, key=len, reverse=True)


def _strip_openclaw_chat_prefix(raw_message: str) -> Tuple[str, bool]:
    """strip 后以配置前缀开头且去掉前缀后仍有正文 → (剩余正文, True)；否则 (原文, False)。"""
    orig = raw_message if raw_message is not None else ""
    s = orig.strip()
    if not s:
        return orig, False
    for p in _openclaw_chat_prefix_patterns():
        pl = len(p)
        if pl == 0 or len(s) < pl:
            continue
        if s[:pl].lower() == p.lower():
            rest = s[pl:].strip()
            if not rest:
                return orig, False
            return rest, True
    return orig, False


def _want_openclaw_first_this_turn(
    review_drafts_only: bool,
    direct_llm: bool,
    openclaw_from_message_prefix: bool,
) -> bool:
    if review_drafts_only or direct_llm:
        return False
    if _openclaw_only_chat_enabled():
        return True
    if openclaw_from_message_prefix:
        return True
    if getattr(settings, "lobster_openclaw_chat_prefix_gate", False):
        return False
    return bool(getattr(settings, "lobster_openclaw_primary_chat", False))


_OC_ONLY_CHAT_FAIL_DETAIL = (
    "已启用「仅 OpenClaw」但未配置 Gateway 或 Gateway 无有效回复。"
    "请配置 OPENCLAW_GATEWAY_URL、OPENCLAW_GATEWAY_TOKEN 并检查 OpenClaw 服务。"
)

_OC_PREFIX_CHAT_FAIL_DETAIL = (
    "本句已使用 OpenClaw 前缀（如 /openclaw），仅走 Gateway；Gateway 未返回有效回复，且当前请求没有可用的直连/速推对话配置。"
    "若界面曾出现「401 status code (no body)」：多为 OpenClaw 调用上游模型时鉴权失败——请检查项目 openclaw/.env、openclaw.json 中的 Anthropic/OpenAI 等 Key；"
    "并确认龙虾后端 .env 的 OPENCLAW_GATEWAY_URL、OPENCLAW_GATEWAY_TOKEN 与 Gateway 一致。"
    "在线版在已配置 AUTH_SERVER_BASE 且本轮可解析为速推对话时，会自动回退到认证中心 sutui-chat。"
)


def _openclaw_body_looks_like_upstream_http_error(content: str) -> bool:
    """OpenClaw（pi-ai / OpenAI SDK）在上游鉴权失败时，可能仍返回 HTTP 200，把「401 status code (no body)」写进 assistant content。"""
    t = (content or "").strip().lower()
    if not t:
        return False
    if "status code (no body)" in t:
        return True
    if re.match(r"^\d{3}\s+status code(\s|\(|$|,)", t):
        return True
    if "invalid_api_key" in t or "incorrect api key" in t:
        return True
    return False


def _openclaw_fallback_model() -> str:
    """当未配置直连 LLM 时，OpenClaw 回退使用的模型（与 openclaw.json agents.defaults.model.primary 一致）。"""
    try:
        p = _BASE_DIR / "openclaw" / "openclaw.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            primary = (
                (data.get("agents") or {}).get("defaults") or {}
            ).get("model") or {}
            if isinstance(primary, dict):
                pv = (primary.get("primary") or "").strip()
                if pv and "/" in pv:
                    return pv
    except Exception:
        pass
    return "anthropic/claude-sonnet-4-5"


def _openclaw_gateway_body_model(agent_id: str) -> str:
    """Gateway /v1/chat/completions 仅接受 `openclaw` 或 `openclaw/<agentId>`。"""
    aid = (agent_id or "").strip() or "main"
    if aid == "main":
        return "openclaw"
    return f"openclaw/{aid}"


def _openclaw_sutui_model_slug(mid: str) -> str:
    """与 openclaw.json agents.list 中 lobster-sutui-<slug> 段一致。"""
    s = re.sub(r"[^\w.-]+", "-", (mid or "").strip(), flags=re.ASCII)
    s = re.sub(r"-+", "-", s).strip("-").lower()
    return s[:72] if s else "m"


def _openclaw_agent_id_from_chat_model(model: str) -> str:
    """把对话层 model 映成 agents.list 里的 id；sutui/xxx → lobster-sutui-<slug>，避免非法 openclaw/sutui-xxx。"""
    m = (model or "").strip()
    if not m or m.lower() == "openclaw":
        return "main"
    low = m.lower()
    if low.startswith("sutui/"):
        rest = m[6:].strip()
        if rest:
            return f"lobster-sutui-{_openclaw_sutui_model_slug(rest)}"
        return "main"
    if low.startswith("lobster-sutui/"):
        rest = m[14:].strip()
        if rest:
            return f"lobster-sutui-{_openclaw_sutui_model_slug(rest)}"
        return "main"
    if "/" in m:
        slug = re.sub(
            r"[^a-z0-9_-]", "-",
            m.lower().replace("/", "-").replace(".", "-"),
        )
        return re.sub(r"-+", "-", slug).strip("-")[:64] or "main"
    return "main"


def _installation_id_from_request(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
    return xi or None


async def _try_openclaw(
    msgs: List[Dict],
    model: str,
    raw_token: str,
    installation_id: Optional[str] = None,
) -> Optional[str]:
    """Attempt to get a reply via OpenClaw Gateway. Returns None on failure."""
    oc_base = (settings.openclaw_gateway_url or "").strip().rstrip("/")
    oc_token = (settings.openclaw_gateway_token or "").strip()
    if not oc_base or not oc_token:
        return None

    agent_id = _openclaw_agent_id_from_chat_model(model)
    openclaw_body_model = _openclaw_gateway_body_model(agent_id)

    _xi = (installation_id or "").strip()
    rt = (raw_token or "").strip()
    if rt:
        set_mcp_token_for_agent(agent_id, rt, installation_id=_xi or None)

    hdrs: Dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {oc_token}",
        "x-openclaw-agent-id": agent_id,
        "x-user-authorization": f"Bearer {raw_token}" if rt else "",
    }
    if _xi:
        hdrs["X-Installation-Id"] = _xi

    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as c:
            resp = await c.post(
                f"{oc_base}/v1/chat/completions",
                json={"model": openclaw_body_model, "messages": msgs, "stream": False},
                headers=hdrs,
            )
        if resp.status_code == 200:
            choices = resp.json().get("choices", [])
            if choices:
                raw_content = (choices[0].get("message", {}).get("content") or "").strip()
                if _openclaw_body_looks_like_upstream_http_error(raw_content):
                    logger.warning(
                        "[CHAT] OpenClaw Gateway 200 但正文疑似上游 LLM 鉴权/HTTP 错误，按失败处理 agent_id=%s snippet=%s",
                        agent_id,
                        (raw_content or "")[:300],
                    )
                    return None
                return raw_content
            logger.warning(
                "[CHAT] OpenClaw Gateway 200 但 choices 为空 model=%s agent_id=%s",
                model,
                agent_id,
            )
        else:
            _bp = (resp.text or "").replace("\n", " ").strip()
            if len(_bp) > 600:
                _bp = _bp[:600] + "…"
            logger.warning(
                "[CHAT] OpenClaw Gateway HTTP %s model=%s agent_id=%s body_prefix=%s",
                resp.status_code,
                model,
                agent_id,
                _bp or "(empty body)",
            )
    except Exception as e:
        logger.warning("[CHAT] OpenClaw Gateway 请求异常: %s", e)
    return None


# ── Chat turn logging ─────────────────────────────────────────────

def _flush_tool_logs(db: Session, uid: int, session_id: Optional[str], model: Optional[str]):
    """Persist collected tool call records to the database."""
    try:
        logs = _pending_tool_logs.get()
    except LookupError:
        return
    for entry in logs:
        db.add(ToolCallLog(
            user_id=uid,
            tool_name=entry["tool_name"],
            arguments=entry.get("arguments"),
            result_text=entry.get("result_text"),
            result_urls=entry.get("result_urls"),
            success=entry.get("success", True),
            latency_ms=entry.get("latency_ms"),
            session_id=(session_id or "")[:128] or None,
            model=(model or "")[:128] or None,
        ))
    _pending_tool_logs.set([])


def _log_turn(
    db: Session, uid: int, user_msg: str, reply: str,
    sid: Optional[str], cid: Optional[str], meta: Optional[Dict] = None,
):
    db.add(ChatTurnLog(
        user_id=uid,
        session_id=(sid or "")[:128] or None,
        context_id=(cid or "")[:128] or None,
        user_message=(user_msg or "")[:5000],
        assistant_reply=(reply or "")[:20000],
        meta=meta or {},
    ))


# ── Main endpoint ─────────────────────────────────────────────────

def _build_user_content_with_attachments(
    payload: ChatRequest,
    request: Optional[Request] = None,
    db=None,
    user_id: Optional[int] = None,
) -> str:
    user_content = (payload.message or "").strip()
    if getattr(payload, "attachment_asset_ids", None) and request and db is not None and user_id is not None:
        from backend.app.api.assets import get_asset_public_url

        pairs = []
        aids = [a.strip() for a in (payload.attachment_asset_ids or [])[:5] if isinstance(a, str) and a.strip()]
        asb = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/") or "（未配置 AUTH_SERVER_BASE）"
        for aid in aids:
            u = get_asset_public_url(aid, user_id, request, db)
            if not u:
                logger.error("[CHAT] 附图无公网 URL，中止组装用户消息 asset_id=%s", aid)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"素材 {aid} 没有公网可访问链接。请配置 TOS_CONFIG 或确保 {asb}/api/assets/upload-temp 可用后重新上传。"
                    ),
                )
            if _LOCAL_SIGNED_ASSET_PATH in u:
                logger.error("[CHAT] 附图为已禁止的签名直链 asset_id=%s", aid)
                raise HTTPException(
                    status_code=400,
                    detail=f"素材 {aid} 为旧版签名链接，已不可用。请重新上传。",
                )
            pairs.append((aid, u))
        if pairs:
            logger.info("[CHAT] 注入素材 URL: asset_ids=%s", [p[0] for p in pairs])
            user_content += (
                "\n\n【用户本条消息上传的素材】\n"
                "- 图生视频：你不要在 video.generate 的 payload 里填 image_url/media_files，由系统自动注入。\n"
                "- 图生图 / 图片编辑：调用 image.generate，在 payload 里设 image_url 为下方 URL，并指定编辑模型（wan/v2.7/edit、seedream/v4.5/edit 等）。\n"
                "- 理解图片：调用 image.understand，在 payload 里设 image_url 为下方 URL，prompt 为用户的要求。\n"
                "- 理解视频：调用 video.understand，在 payload 里设 video_url 为下方 URL，prompt 为用户的要求。\n"
            )
            user_content += "\n".join(f"- asset_id: {aid}  URL: {u}" for aid, u in pairs)
    return user_content or "（无文字）"


@router.post("/chat", summary="智能对话")
async def chat_endpoint(
    request: Request,
    payload: ChatRequest,
    raw_token: str = Depends(oauth2_scheme),
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    _pending_tool_logs.set([])
    _list_capabilities_cache.set(None)
    want_orch_report = bool(getattr(payload, "orchestration_report", False))
    _orch_tok = _orchestration_tool_log.set([] if want_orch_report else None)
    is_schedule_orch = bool(getattr(payload, "schedule_orchestration", False))
    _sched_tok = _schedule_orchestration_active.set(is_schedule_orch)
    _cost_cancelled_caps_ctx.set(set())

    _oc_pfx_rest, _oc_pfx_hit = _strip_openclaw_chat_prefix(payload.message)
    if _oc_pfx_hit:
        payload = payload.model_copy(update={"message": _oc_pfx_rest})
        logger.info("[CHAT] OpenClaw 消息前缀已剥离，本轮优先 Gateway")

    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition == "online":
        model = "sutui"
        # 速推由服务器 MCP + SUTUI_SERVER_TOKEN 处理；本机 MCP 经 mcp-gateway 转发，此处不传用户速推 Token
        sutui_token = None
    else:
        sutui_token = None
        model = payload.model or getattr(current_user, "preferred_model", None) or ""
        if not model or model == "openclaw":
            model = _pick_default_model()

    mcp_tools = await _fetch_mcp_tools(raw_token)
    review_drafts_only = bool(getattr(payload, "review_prompt_drafts_only", False))
    _review_prompt_drafts_only_active.set(review_drafts_only)
    direct_llm = bool(getattr(payload, "direct_llm", False))
    if review_drafts_only:
        mcp_tools = []
        logger.info(
            "[CHAT] review_prompt_drafts_only：已禁用 MCP；使用极简 system（见 app.log 历史：主对话 system 曾导致只输出闲聊而非 JSON）"
        )
    elif direct_llm:
        mcp_tools = []
        logger.info("[CHAT] direct_llm：不挂 MCP，极简 system，用户内容直送当前模型")
    has_tools = bool(mcp_tools)
    if not has_tools:
        logger.info(
            "MCP tools empty (port 8001 may be down or unreachable), chat has no capabilities; "
            "user will see 'cannot generate image'. Check that MCP service is started (run_mcp.bat / start.bat)."
        )
    sutui_override_url: Optional[str] = None
    sutui_override_headers: Optional[Dict[str, str]] = None
    if edition == "online":
        resolve_model, cfg_pre, sutui_override_url, sutui_override_headers = _online_resolve_cfg_and_overrides(
            payload, raw_token, schedule_orchestration=is_schedule_orch
        )
    else:
        resolve_model = model
        cfg_pre = _resolve_config(resolve_model) if resolve_model else None

    if review_drafts_only:
        sys_prompt = _REVIEW_PROMPT_DRAFTS_SYSTEM
    elif direct_llm:
        sys_prompt = _DIRECT_LLM_SYSTEM
    else:
        sys_prompt = _build_lobster_main_system_prompt(edition, has_tools)

    if not review_drafts_only and not direct_llm:
        sys_prompt = await _maybe_append_capabilities_snapshot_to_system(
            sys_prompt, (payload.message or "").strip(), raw_token, request, has_tools
        )
        sys_prompt = await _maybe_append_sutui_models_snapshot_to_system(
            sys_prompt, (payload.message or "").strip(), raw_token, request, has_tools
        )

    messages: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    for m in (payload.history or []):
        if m.role in ("user", "assistant") and (m.content or "").strip():
            content = m.content.strip()
            if m.role == "assistant" and content.startswith("错误："):
                continue
            if len(content) > MAX_HISTORY_MESSAGE_CHARS:
                content = (
                    content[: MAX_HISTORY_MESSAGE_CHARS // 2].rstrip()
                    + "\n\n...(上条内容已省略，请根据用户新消息直接回复。)"
                )
            messages.append({"role": m.role, "content": content})
    if len(messages) > MAX_HISTORY + 1:
        messages = [messages[0]] + messages[-MAX_HISTORY:]
    messages.append({"role": "user", "content": _build_user_content_with_attachments(payload, request, db=db, user_id=current_user.id)})
    if direct_llm:
        _wrap_last_user_for_direct_llm(messages)

    t0 = time.perf_counter()

    # ── Primary path: direct LLM API with MCP tools ──
    attachment_urls = _get_attachment_public_urls(
        getattr(payload, "attachment_asset_ids", None), request, db, current_user.id
    )

    want_oc_first = _want_openclaw_first_this_turn(
        review_drafts_only, direct_llm, _oc_pfx_hit
    )
    openclaw_tried_first = False

    if want_oc_first:
        openclaw_tried_first = True
        if _openclaw_only_chat_enabled() and not _openclaw_gateway_configured():
            raise HTTPException(status_code=503, detail=_OC_ONLY_CHAT_FAIL_DETAIL)
        oc_model = resolve_model if resolve_model else _openclaw_fallback_model()
        _oc_xi = _installation_id_from_request(request)
        oc_reply = await _try_openclaw(messages, oc_model, raw_token, installation_id=_oc_xi)
        if oc_reply:
            ms = round((time.perf_counter() - t0) * 1000)
            logger.info("[CHAT] OpenClaw 优先：Gateway 已返回 model=%s", oc_model)
            _flush_tool_logs(db, current_user.id, payload.session_id, model)
            _log_turn(
                db, current_user.id, payload.message, _reply_for_user(oc_reply),
                payload.session_id, payload.context_id,
                {"model": model, "mode": "openclaw_primary", "duration_ms": ms},
            )
            db.commit()
            orch = _build_orchestration_report() if want_orch_report else None
            _orchestration_tool_log.reset(_orch_tok)
            body = ChatResponse(reply=_reply_for_user(oc_reply), orchestration=orch).model_dump(exclude_none=not want_orch_report)
            return JSONResponse(
                content=body,
                headers={"X-Duration-Ms": str(ms), "X-Chat-Mode": "openclaw_primary"},
            )
        if _openclaw_only_chat_enabled():
            logger.warning("[CHAT] 仅 OpenClaw：Gateway 无有效回复")
            raise HTTPException(status_code=503, detail=_OC_ONLY_CHAT_FAIL_DETAIL)
        if _oc_pfx_hit and not cfg_pre:
            logger.warning("[CHAT] OpenClaw 前缀轮次：Gateway 无有效回复，且无直连/速推配置，不回退")
            raise HTTPException(status_code=503, detail=_OC_PREFIX_CHAT_FAIL_DETAIL)
        logger.info("[CHAT] OpenClaw 优先：Gateway 无有效回复，回退直连+MCP")

    cfg = cfg_pre
    if cfg:
        _mcp_hdrs: Dict[str, str] = {}
        _prov = (cfg.get("provider") or "").strip()
        _mn = (cfg.get("model_name") or "").strip()
        if _prov and _mn:
            _mcp_hdrs["X-Chat-Model"] = f"{_prov}/{_mn}"
        _vurls_tok = _recent_task_video_urls_ctx.set([])
        _pub_hints_tok = _recent_publish_asset_hints_ctx.set([])
        _hints_tok = _generation_hints_by_task_id.set({})
        _mcp_var_tok = _mcp_forward_headers_ctx.set(_mcp_hdrs)
        try:
            logger.info("[对话] 请求 model=%s tools=%d", model, len(mcp_tools))
            if cfg["provider"] == "anthropic":
                reply = await _chat_anthropic(
                    messages,
                    cfg,
                    mcp_tools,
                    raw_token,
                    sutui_token=sutui_token,
                    attachment_urls=attachment_urls,
                    attachment_asset_ids=getattr(payload, "attachment_asset_ids", None),
                    request=request,
                    db=db,
                    user_id=current_user.id,
                )
            else:
                reply = await _chat_openai(
                    messages,
                    cfg,
                    mcp_tools,
                    raw_token,
                    sutui_token=sutui_token,
                    attachment_urls=attachment_urls,
                    attachment_asset_ids=getattr(payload, "attachment_asset_ids", None),
                    override_url=sutui_override_url,
                    override_headers=sutui_override_headers,
                    request=request,
                    db=db,
                    user_id=current_user.id,
                )

            ms = round((time.perf_counter() - t0) * 1000)
            _flush_tool_logs(db, current_user.id, payload.session_id, model)
            _log_turn(
                db, current_user.id, payload.message, _reply_for_user(reply),
                payload.session_id, payload.context_id,
                {
                    "model": model,
                    "mode": "direct_llm" if direct_llm else "direct",
                    "duration_ms": ms,
                    "tools": len(mcp_tools),
                },
            )
            db.commit()
            orch = _build_orchestration_report() if want_orch_report else None
            _orchestration_tool_log.reset(_orch_tok)
            body = ChatResponse(reply=_reply_for_user(reply), orchestration=orch).model_dump(exclude_none=not want_orch_report)
            return JSONResponse(
                content=body,
                headers={"X-Duration-Ms": str(ms)},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Direct LLM call failed, trying OpenClaw fallback: %s", e)
        finally:
            _mcp_forward_headers_ctx.reset(_mcp_var_tok)
            _recent_task_video_urls_ctx.reset(_vurls_tok)
            _recent_publish_asset_hints_ctx.reset(_pub_hints_tok)
            _generation_hints_by_task_id.reset(_hints_tok)

    # ── Fallback: OpenClaw Gateway（未先尝试 OpenClaw 时，如无直连或直连失败）──
    if not openclaw_tried_first:
        oc_model = resolve_model if resolve_model else _openclaw_fallback_model()
        oc_reply = await _try_openclaw(
            messages, oc_model, raw_token, installation_id=_installation_id_from_request(request)
        )
        if oc_reply:
            ms = round((time.perf_counter() - t0) * 1000)
            _flush_tool_logs(db, current_user.id, payload.session_id, model)
            _log_turn(
                db, current_user.id, payload.message, _reply_for_user(oc_reply),
                payload.session_id, payload.context_id,
                {"model": model, "mode": "openclaw", "duration_ms": ms},
            )
            db.commit()
            orch = _build_orchestration_report() if want_orch_report else None
            _orchestration_tool_log.reset(_orch_tok)
            body = ChatResponse(reply=_reply_for_user(oc_reply), orchestration=orch).model_dump(exclude_none=not want_orch_report)
            return JSONResponse(
                content=body,
                headers={"X-Duration-Ms": str(ms), "X-Chat-Mode": "openclaw"},
            )

    # ── No LLM path available ──
    if not cfg:
        detail = (
            f"模型 {model} 的 API Key 未配置。"
            "请在「系统配置」中添加对应的 API Key。"
        )
    else:
        detail = "LLM 服务暂时不可用，请稍后重试。"
    raise HTTPException(status_code=503, detail=detail)


# ── Stream endpoint (SSE progress) ─────────────────────────────────

async def _chat_stream_events(
    payload: ChatRequest,
    raw_token: str,
    current_user: Union[User, _ServerUser],
    db: Session,
    request: Optional[Request] = None,
    *,
    openclaw_prefixed_turn: bool = False,
):
    """Async generator: yield SSE events (progress + done). Runs chat with progress_cb pushing to queue."""
    queue: asyncio.Queue = asyncio.Queue()
    reply_holder: List[str] = []
    error_holder: List[str] = []
    _request_for_assets = request

    async def progress_cb(ev: Dict) -> None:
        await queue.put(ev)

    async def run_chat() -> None:
        _pending_tool_logs.set([])
        _list_capabilities_cache.set(None)
        resume_tid = (getattr(payload, "resume_task_poll_task_id", None) or "").strip()
        if resume_tid:
            result_text = ""
            try:
                result_text = await _resume_chat_task_poll_only(
                    resume_tid, raw_token, current_user, db, request, progress_cb
                )
                _flush_tool_logs(db, current_user.id, payload.session_id, "sutui")
                _log_turn(
                    db,
                    current_user.id,
                    f"（恢复查询任务 {resume_tid[:48]}）",
                    _reply_for_user(result_text),
                    payload.session_id,
                    payload.context_id,
                    {"model": "sutui", "mode": "resume_task_poll"},
                )
                db.commit()
            except HTTPException as e:
                error_holder.append(e.detail if isinstance(e.detail, str) else str(e.detail))
            except Exception as e:
                logger.exception("[CHAT/stream] resume_task_poll")
                error_holder.append(_friendly_chat_stream_exception(e))
            fe = error_holder[0] if error_holder else None
            if fe:
                fr = f"错误：{fe}"
            else:
                fr = _reply_for_user(result_text) if result_text else "查询已结束。"
            await queue.put({"type": "done", "reply": fr, "error": fe})
            return
        edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
        if edition == "online":
            model = "sutui"
            sutui_token = None
        else:
            sutui_token = None
            model = payload.model or getattr(current_user, "preferred_model", None) or ""
            if not model or model == "openclaw":
                model = _pick_default_model()
        mcp_tools = await _fetch_mcp_tools(raw_token)
        _is_sched_orch = bool(getattr(payload, "schedule_orchestration", False))
        _schedule_orchestration_active.set(_is_sched_orch)
        _cost_cancelled_caps_ctx.set(set())
        _review_prompt_drafts_only_active.set(bool(getattr(payload, "review_prompt_drafts_only", False)))
        direct_llm = bool(getattr(payload, "direct_llm", False))
        if direct_llm:
            mcp_tools = []
            logger.info("[CHAT/stream] direct_llm：不挂 MCP，极简 system")
        has_tools = bool(mcp_tools)
        if not has_tools:
            logger.info("MCP tools empty (stream path), chat has no capabilities")
        sutui_override_url: Optional[str] = None
        sutui_override_headers: Optional[Dict[str, str]] = None
        if edition == "online":
            resolve_model, cfg_pre, sutui_override_url, sutui_override_headers = _online_resolve_cfg_and_overrides(
                payload, raw_token, schedule_orchestration=_is_sched_orch
            )
        else:
            resolve_model = model
            cfg_pre = _resolve_config(resolve_model) if resolve_model else None
        if direct_llm:
            sys_prompt = _DIRECT_LLM_SYSTEM
        else:
            sys_prompt = _build_lobster_main_system_prompt(edition, has_tools)
        if not direct_llm:
            sys_prompt = await _maybe_append_capabilities_snapshot_to_system(
                sys_prompt, (payload.message or "").strip(), raw_token, request, has_tools
            )
            sys_prompt = await _maybe_append_sutui_models_snapshot_to_system(
                sys_prompt, (payload.message or "").strip(), raw_token, request, has_tools
            )
        messages = [{"role": "system", "content": sys_prompt}]
        for m in (payload.history or []):
            if m.role in ("user", "assistant") and (m.content or "").strip():
                content = m.content.strip()
                if m.role == "assistant" and content.startswith("错误："):
                    continue
                if len(content) > MAX_HISTORY_MESSAGE_CHARS:
                    content = (
                        content[: MAX_HISTORY_MESSAGE_CHARS // 2].rstrip()
                        + "\n\n...(上条内容已省略，请根据用户新消息直接回复。)"
                    )
                messages.append({"role": m.role, "content": content})
        if len(messages) > MAX_HISTORY + 1:
            messages = [messages[0]] + messages[-MAX_HISTORY:]
        stream_attachment_urls: List[str] = []
        try:
            messages.append(
                {
                    "role": "user",
                    "content": _build_user_content_with_attachments(
                        payload, _request_for_assets, db=db, user_id=current_user.id
                    ),
                }
            )
            if direct_llm:
                _wrap_last_user_for_direct_llm(messages)
            stream_attachment_urls = _get_attachment_public_urls(
                getattr(payload, "attachment_asset_ids", None),
                _request_for_assets,
                db,
                current_user.id,
            )
        except HTTPException as e:
            det = e.detail
            error_holder.append(det if isinstance(det, str) else str(det))
        if not error_holder:
            cfg = cfg_pre
            review_po = bool(getattr(payload, "review_prompt_drafts_only", False))
            want_oc = _want_openclaw_first_this_turn(
                review_po, direct_llm, openclaw_prefixed_turn
            )
            openclaw_tried_first = False
            try:
                if want_oc:
                    openclaw_tried_first = True
                    if _openclaw_only_chat_enabled() and not _openclaw_gateway_configured():
                        error_holder.append(_OC_ONLY_CHAT_FAIL_DETAIL)
                    else:
                        oc_model = resolve_model if resolve_model else _openclaw_fallback_model()
                        _oc_xi_s = _installation_id_from_request(request)
                        oc_reply = await _try_openclaw(
                            messages, oc_model, raw_token, installation_id=_oc_xi_s
                        )
                        if oc_reply:
                            reply_holder.append(oc_reply)
                            _flush_tool_logs(db, current_user.id, payload.session_id, model)
                            _log_turn(
                                db, current_user.id, payload.message, _reply_for_user(oc_reply),
                                payload.session_id, payload.context_id,
                                {"model": model, "mode": "openclaw_primary"},
                            )
                            db.commit()
                        elif _openclaw_only_chat_enabled():
                            error_holder.append(_OC_ONLY_CHAT_FAIL_DETAIL)
                        elif openclaw_prefixed_turn and not cfg:
                            error_holder.append(_OC_PREFIX_CHAT_FAIL_DETAIL)

                if not reply_holder and not error_holder and cfg:
                    _mcp_sh: Dict[str, str] = {}
                    _sp = (cfg.get("provider") or "").strip()
                    _sm = (cfg.get("model_name") or "").strip()
                    if _sp and _sm:
                        _mcp_sh["X-Chat-Model"] = f"{_sp}/{_sm}"
                    _vurls_stream_tok = _recent_task_video_urls_ctx.set([])
                    _pub_hints_stream_tok = _recent_publish_asset_hints_ctx.set([])
                    _hints_stream_tok = _generation_hints_by_task_id.set({})
                    _mcp_stream_tok = _mcp_forward_headers_ctx.set(_mcp_sh)
                    try:
                        if cfg["provider"] == "anthropic":
                            reply = await _chat_anthropic(
                                messages, cfg, mcp_tools, raw_token,
                                sutui_token=sutui_token,
                                progress_cb=progress_cb,
                                attachment_urls=stream_attachment_urls,
                                attachment_asset_ids=getattr(payload, "attachment_asset_ids", None),
                                request=request,
                                db=db,
                                user_id=current_user.id,
                            )
                        else:
                            reply = await _chat_openai(
                                messages, cfg, mcp_tools, raw_token,
                                sutui_token=sutui_token,
                                progress_cb=progress_cb,
                                attachment_urls=stream_attachment_urls,
                                attachment_asset_ids=getattr(payload, "attachment_asset_ids", None),
                                override_url=sutui_override_url,
                                override_headers=sutui_override_headers,
                                request=request,
                                db=db,
                                user_id=current_user.id,
                            )
                        reply_holder.append(reply)
                        _flush_tool_logs(db, current_user.id, payload.session_id, model)
                        _log_turn(
                            db, current_user.id, payload.message, _reply_for_user(reply),
                            payload.session_id, payload.context_id,
                            {
                                "model": model,
                                "mode": "direct_llm" if direct_llm else "direct",
                                "tools": len(mcp_tools),
                            },
                        )
                        db.commit()
                    finally:
                        _mcp_forward_headers_ctx.reset(_mcp_stream_tok)
                        _recent_task_video_urls_ctx.reset(_vurls_stream_tok)
                        _recent_publish_asset_hints_ctx.reset(_pub_hints_stream_tok)
                        _generation_hints_by_task_id.reset(_hints_stream_tok)

                if not reply_holder and not error_holder:
                    if not openclaw_tried_first:
                        oc_model = resolve_model if resolve_model else _openclaw_fallback_model()
                        oc_reply = await _try_openclaw(
                            messages,
                            oc_model,
                            raw_token,
                            installation_id=_installation_id_from_request(request),
                        )
                        if oc_reply:
                            reply_holder.append(oc_reply)
                            _flush_tool_logs(db, current_user.id, payload.session_id, model)
                            _log_turn(
                                db, current_user.id, payload.message, _reply_for_user(oc_reply),
                                payload.session_id, payload.context_id,
                                {"model": model, "mode": "openclaw"},
                            )
                            db.commit()
                        else:
                            error_holder.append("LLM 服务暂时不可用")
                    elif want_oc and not cfg:
                        error_holder.append(
                            "OpenClaw 无有效回复，且未配置速推直连模型。"
                        )
            except HTTPException as e:
                error_holder.append(e.detail if isinstance(e.detail, str) else str(e.detail))
            except Exception as e:
                logger.exception("chat/stream run_chat error")
                error_holder.append(_friendly_chat_stream_exception(e))
        final_reply = reply_holder[0] if reply_holder else ""
        final_error = error_holder[0] if error_holder else None
        if final_error:
            final_reply = f"错误：{final_error}"
        else:
            final_reply = _reply_for_user(final_reply)
        await queue.put({"type": "done", "reply": final_reply, "error": final_error})

    task = asyncio.create_task(run_chat())
    try:
        while True:
            if request is not None and await request.is_disconnected():
                break
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                if request is not None and await request.is_disconnected():
                    break
                yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                break
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@router.post("/chat/stream", summary="智能对话（流式返回思考/工具进度）")
async def chat_stream_endpoint(
    request: Request,
    payload: ChatRequest,
    raw_token: str = Depends(oauth2_scheme),
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    """Stream SSE events: tool_start, tool_end, then done with reply. Frontend can show progress in chat."""
    _oc_sp_rest, _oc_sp_hit = _strip_openclaw_chat_prefix(payload.message)
    if _oc_sp_hit:
        payload = payload.model_copy(update={"message": _oc_sp_rest})
        logger.info("[CHAT/stream] OpenClaw 消息前缀已剥离，本轮优先 Gateway")
    return StreamingResponse(
        _chat_stream_events(
            payload, raw_token, current_user, db, request, openclaw_prefixed_turn=_oc_sp_hit
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Chat history ──────────────────────────────────────────────────

@router.get("/chat/history", summary="会话历史")
def list_chat_history(
    context_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    q = db.query(ChatTurnLog).filter(ChatTurnLog.user_id == current_user.id)
    if context_id:
        q = q.filter(ChatTurnLog.context_id == context_id)
    rows = (
        q.order_by(ChatTurnLog.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "context_id": r.context_id,
            "user_message": r.user_message,
            "assistant_reply": r.assistant_reply,
            "meta": r.meta,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


# ── Tool call logs (生产记录) ─────────────────────────────────────

@router.get("/api/tool-logs", summary="MCP 工具调用记录")
def list_tool_logs(
    tool_name: Optional[str] = None,
    success_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    q = db.query(ToolCallLog).filter(ToolCallLog.user_id == current_user.id)
    if tool_name:
        q = q.filter(ToolCallLog.tool_name == tool_name)
    if success_only:
        q = q.filter(ToolCallLog.success.is_(True))
    total = q.count()
    rows = (
        q.order_by(ToolCallLog.created_at.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "result_text": r.result_text[:2000] if r.result_text else None,
                "result_urls": r.result_urls.split(",") if r.result_urls else [],
                "success": r.success,
                "latency_ms": r.latency_ms,
                "session_id": r.session_id,
                "model": r.model,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


@router.get("/api/tool-logs/stats", summary="工具调用统计")
def tool_log_stats(
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    from sqlalchemy import Integer as SaInt, func
    rows = (
        db.query(
            ToolCallLog.tool_name,
            func.count(ToolCallLog.id).label("count"),
            func.sum(ToolCallLog.success.is_(True).cast(SaInt)).label("success_count"),
        )
        .filter(ToolCallLog.user_id == current_user.id)
        .group_by(ToolCallLog.tool_name)
        .all()
    )
    return [
        {"tool_name": r.tool_name, "count": r.count, "success_count": r.success_count or 0}
        for r in rows
    ]


# ── 生产记录：仅速推能力调用 + 模型对话，无重复 ─────────────────────────────

def _production_records_merged(
    current_user: Union[User, _ServerUser],
    db: Session,
    limit: int = 50,
    offset: int = 0,
):
    """合并 CapabilityCallLog（速推生成）与 ChatTurnLog（模型调用），按时间倒序，无重复。"""
    cap_rows = (
        db.query(CapabilityCallLog)
        .filter(CapabilityCallLog.user_id == current_user.id)
        .order_by(CapabilityCallLog.created_at.desc())
        .limit(150)
        .all()
    )
    turn_rows = (
        db.query(ChatTurnLog)
        .filter(ChatTurnLog.user_id == current_user.id)
        .order_by(ChatTurnLog.created_at.desc())
        .limit(150)
        .all()
    )
    merged = []
    for r in cap_rows:
        merged.append({
            "type": "capability",
            "id": f"c{r.id}",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "capability_id": r.capability_id,
            "success": r.success,
            "latency_ms": r.latency_ms,
            "error_message": (r.error_message or "")[:500] if r.error_message else None,
            "status": r.status,
        })
    for r in turn_rows:
        merged.append({
            "type": "model",
            "id": f"t{r.id}",
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "user_message": (r.user_message or "")[:300],
            "assistant_reply": (r.assistant_reply or "")[:500],
        })
    merged.sort(key=lambda x: x["created_at"], reverse=True)
    total = len(merged)
    page = merged[offset : offset + limit]
    return {"total": total, "items": page}


@router.get("/api/production/records", summary="生产记录（仅速推能力+模型对话）")
def list_production_records(
    limit: int = 50,
    offset: int = 0,
    current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat),
    db: Session = Depends(get_db),
):
    """仅返回：速推能力调用（CapabilityCallLog）、模型对话轮次（ChatTurnLog）。可刷新看进度，无重复。"""
    return _production_records_merged(current_user=current_user, db=db, limit=min(max(limit, 1), 100), offset=max(offset, 0))


@router.post("/api/production/refresh-pending", summary="刷新待处理（兼容）")
def production_refresh_pending(current_user: Union[User, _ServerUser] = Depends(get_current_user_for_chat)):
    """兼容旧前端，无实际操作，返回成功。"""
    return {"ok": True}

"""
HTTP MCP Server for 龙虾 (Lobster).
Simplified from ai_test_platform: no admin checks, dynamic catalog reload.

速推类能力（image.generate / video.generate / task.get_result / …）：
用户积分的预扣、结算、退款 **只在** 认证中心宿主机上的 ``lobster_server/mcp/http_server.py`` →
``invoke_capability`` **一处**编排（pre-deduct → 速推上游 → record-call/refund）。

本机 MCP 只把请求转到 ``{AUTH_SERVER_BASE}/mcp-gateway``，**不对速推**调用 /capabilities/*。

本机 ``upstream=local``：`media.edit` 免费，MCP 不预扣/不 record；`comfly.*` 积分由 **comfly/daihuo 后端**
自行扣费，与速推无关，MCP 也不代为调认证中心计费接口。
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
from decimal import Decimal
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .video_model_resolve import resolve_video_model_id

# 与 mcp/__main__.py 一致：被 uvicorn 直接加载 app 时也能读到项目根 .env
_lobster_root = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv

    load_dotenv(_lobster_root / ".env")
except Exception:
    pass

logger = logging.getLogger(__name__)


def _mcp_opts_toutiao_graphic_no_cover(opts: Any) -> bool:
    """与发布 API / 头条驱动一致：无封面纯文时允许省略 asset_id。"""
    if not isinstance(opts, dict):
        return False

    def _truthy_local(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if isinstance(v, (int, float)):
            return v != 0
        s = str(v).strip().lower()
        return s in ("1", "true", "yes", "on")

    if _truthy_local(opts.get("toutiao_graphic_no_cover")):
        return True
    inner = opts.get("toutiao")
    if isinstance(inner, dict):
        return _truthy_local(inner.get("graphic_no_cover")) or _truthy_local(inner.get("no_cover"))
    return False

_SUTUI_UPSTREAM_LOG_MAX = 500_000

# 本机 invoke_capability：后端路径与 HTTP 超时（秒）；带货整包流水线可能极长，单独加长超时
_LOCAL_INVOKE_BACKEND: Dict[str, Tuple[str, float]] = {
    "media.edit": ("/api/media-edit/run", 3600.0),
    "comfly.veo": ("/api/comfly-veo/run", 600.0),
    "comfly.veo.daihuo_pipeline": ("/api/comfly-daihuo/pipeline/run", 7200.0),
    "comfly.ecommerce.detail_pipeline": ("/api/comfly-ecommerce-detail/pipeline/run", 7200.0),
    "ecommerce.publish": ("/api/ecommerce-publish/open-product-form", 120.0),
}

# 不在 MCP 内调认证中心 pre/record/refund：media.edit 免费；comfly.* 扣费在各自后端路由内处理。
_INVOKE_NO_AUTH_CENTER_BILLING = frozenset({"media.edit", "comfly.veo", "comfly.veo.daihuo_pipeline", "comfly.ecommerce.detail_pipeline", "ecommerce.publish"})


def _normalize_invoke_task_get_result_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """task.get_result：上游只认 payload.task_id；模型常误写 taskid/taskId 或把 task_id 放在 arguments 顶层。"""
    if not isinstance(args, dict):
        return args
    if str(args.get("capability_id") or "").strip() != "task.get_result":
        return args
    payload = args.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    pl = dict(payload)
    tid = (pl.get("task_id") or "").strip()
    if not tid:
        for k in ("taskId", "taskid", "TaskId"):
            v = pl.get(k)
            if isinstance(v, str) and v.strip():
                tid = v.strip()
                break
    if not tid:
        for k in ("task_id", "taskId", "taskid"):
            v = args.get(k)
            if isinstance(v, str) and v.strip():
                tid = v.strip()
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


def _normalize_invoke_comfly_veo_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """comfly.veo：模型常把 action 写在 invoke_capability 顶层，或误套 payload.payload。"""
    if not isinstance(args, dict):
        return args
    if str(args.get("capability_id") or "").strip() != "comfly.veo":
        return args
    raw_pl = args.get("payload")
    pl: Dict[str, Any] = dict(raw_pl) if isinstance(raw_pl, dict) else {}
    nested = pl.get("payload")
    if isinstance(nested, dict) and (nested.get("action") or "").strip():
        base = {k: v for k, v in pl.items() if k != "payload"}
        pl = {**base, **nested}
    if not (pl.get("action") or "").strip():
        top_act = str(args.get("action") or "").strip()
        if top_act:
            pl["action"] = top_act
        for k in (
            "asset_id",
            "image_url",
            "images",
            "prompt",
            "prompts",
            "task_id",
            "video_model",
            "analysis_model",
            "aspect_ratio",
            "enhance_prompt",
        ):
            if k in args and args[k] is not None:
                pl.setdefault(k, args[k])
    out = dict(args)
    out["payload"] = pl
    return out


def _normalize_invoke_daihuo_pipeline_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """comfly.veo.daihuo_pipeline：模型常把 action/job_id 写在顶层或误套 payload.payload。"""
    if not isinstance(args, dict):
        return args
    if str(args.get("capability_id") or "").strip() != "comfly.veo.daihuo_pipeline":
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
        top_act = str(args.get("action") or "").strip()
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


def _normalize_invoke_ecommerce_detail_pipeline_args(args: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(args, dict):
        return args
    if str(args.get("capability_id") or "").strip() != "comfly.ecommerce.detail_pipeline":
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
        top_act = str(args.get("action") or "").strip()
        if top_act:
            pl["action"] = top_act
    for k in (
        "job_id",
        "asset_id",
        "image_url",
        "product_name_hint",
        "product_direction_hint",
        "reference_asset_ids",
        "reference_image_urls",
        "page_count",
        "auto_save",
        "platform",
        "country",
        "language",
        "analysis_model",
        "image_model",
        "output_dir",
        "isolate_job_dir",
    ):
        if k in args and args[k] is not None:
            pl.setdefault(k, args[k])
    out = dict(args)
    out["payload"] = pl
    return out


def _sutui_rest_phase_label(tool_name: str) -> str:
    if tool_name == "generate":
        return "创建任务|tasks/create"
    if tool_name == "get_result":
        return "查询结果|tasks/query"
    return f"upstream_tool={tool_name}"


# sutui.transfer_url：客户端常对同一源链连环调用；按用户+源 URL 短缓存，减少重复上游与 auto_save
_TRANSFER_URL_RESULT_CACHE: Dict[str, Tuple[float, dict]] = {}
_TRANSFER_URL_CACHE_TTL_SEC = float(os.getenv("MCP_TRANSFER_URL_CACHE_TTL_SEC", "900"))
_TRANSFER_URL_CACHE_MAX = int(os.getenv("MCP_TRANSFER_URL_CACHE_MAX", "512"))


def _normalize_transfer_url_source(u: str) -> str:
    return (u or "").strip().split("?")[0].split("#")[0].lower()


def _transfer_url_cache_key(token: str, source_url: str) -> str:
    return hashlib.sha256(
        f"{(token or '').strip()}\n{_normalize_transfer_url_source(source_url)}".encode("utf-8")
    ).hexdigest()


def _transfer_url_cache_get(key: str) -> Optional[dict]:
    now = time.monotonic()
    tup = _TRANSFER_URL_RESULT_CACHE.get(key)
    if not tup:
        return None
    exp, resp = tup
    if exp < now:
        _TRANSFER_URL_RESULT_CACHE.pop(key, None)
        return None
    return copy.deepcopy(resp)


def _transfer_url_cache_set(key: str, resp: dict) -> None:
    while len(_TRANSFER_URL_RESULT_CACHE) >= _TRANSFER_URL_CACHE_MAX:
        try:
            _TRANSFER_URL_RESULT_CACHE.pop(next(iter(_TRANSFER_URL_RESULT_CACHE)))
        except StopIteration:
            break
    _TRANSFER_URL_RESULT_CACHE[key] = (
        time.monotonic() + _TRANSFER_URL_CACHE_TTL_SEC,
        copy.deepcopy(resp),
    )


def _log_sutui_rest_payload(tool_name: str, lobster_capability_id: str, payload: Any) -> None:
    try:
        raw = json.dumps(_sanitize_for_json(payload), ensure_ascii=False, default=str)
        total_len = len(raw)
        if total_len > _SUTUI_UPSTREAM_LOG_MAX:
            raw = raw[:_SUTUI_UPSTREAM_LOG_MAX] + f"\n... [已截断，原始总长 {total_len} 字符]"
        logger.info(
            "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST\n%s",
            _sutui_rest_phase_label(tool_name),
            tool_name,
            lobster_capability_id or "(无)",
            raw,
        )
    except Exception as ex:
        logger.warning("[速推完整响应] REST 序列化失败 tool=%s: %s", tool_name, ex)


def _sanitize_for_json(obj: Any) -> Any:
    """速推/API 响应中可能含 Decimal，json.dumps 无法直接序列化。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(x) for x in obj)
    return obj


def _json_dumps_mcp_payload(obj: Any) -> str:
    return json.dumps(_sanitize_for_json(obj), ensure_ascii=False, indent=2)


from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route


BASE_URL = os.environ.get("AI_TEST_PLATFORM_BASE_URL", "http://localhost:8000").rstrip("/")
CAPABILITY_UPSTREAM_URLS_JSON = os.environ.get("CAPABILITY_UPSTREAM_URLS_JSON", "").strip()


def _load_catalog_from_file(path: Path) -> Dict[str, Dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("catalog must be object")
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k] = v
    return out


def _looks_like_material_asset_id(value: str) -> bool:
    """素材库 asset_id 常为 12～32 位十六进制；模型易误当作 invoke_capability 的 capability_id。"""
    s = (value or "").strip().lower()
    if len(s) < 12 or len(s) > 32:
        return False
    return all(c in "0123456789abcdef" for c in s)


def _load_capability_catalog() -> Dict[str, Dict[str, Any]]:
    """Reload catalog from files each time (hot-reload support)."""
    try:
        p_local = Path(__file__).resolve().parent / "capability_catalog.local.json"
        if p_local.exists():
            catalog = _load_catalog_from_file(p_local)
            p_base = Path(__file__).resolve().parent / "capability_catalog.json"
            if p_base.exists():
                base = _load_catalog_from_file(p_base)
                base.update(catalog)
                return base
            return catalog
    except Exception:
        pass
    try:
        p = Path(__file__).resolve().parent / "capability_catalog.json"
        if p.exists():
            return _load_catalog_from_file(p)
    except Exception:
        pass
    return {}


_SKILL_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "skill_registry.json"
_DEBUG_ONLY_MCP_TOOL_NAMES = frozenset({"list_youtube_accounts", "publish_youtube_video", "get_youtube_analytics", "sync_youtube_analytics", "list_meta_social_accounts", "publish_meta_social", "get_meta_social_data", "sync_meta_social_data", "get_social_report"})


def _load_skill_registry() -> Dict[str, Any]:
    try:
        if _SKILL_REGISTRY_PATH.exists():
            return json.loads(_SKILL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[MCP] skill_registry 读取失败: %s", e)
    return {"packages": {}}


def _capability_id_is_debug_only_in_registry(cap_id: str) -> bool:
    """能力仅出现在 store_visibility=debug 的包中、且未出现在 online 或未标注包时，对非管理员隐藏。"""
    registry = _load_skill_registry()
    found_online = False
    found_debug = False
    for pkg in (registry.get("packages") or {}).values():
        if not isinstance(pkg, dict):
            continue
        caps = pkg.get("capabilities") or {}
        if cap_id not in caps:
            continue
        vis = (pkg.get("store_visibility") or "").strip().lower()
        if vis == "debug":
            found_debug = True
        else:
            found_online = True
    if found_online:
        return False
    return found_debug


async def _fetch_is_skill_store_admin(token: Optional[str]) -> bool:
    if not (token or "").strip():
        return False
    auth = (token or "").strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    auth_base = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if not auth_base:
        return True
    url = f"{auth_base}/skills/skill-store-admin"
    # 经系统代理（如 127.0.0.1:7890）时 TLS 偶发超时；原先 5s + 单次易误判为「非管理员」（见 mcp.log ConnectTimeout）
    timeout = httpx.Timeout(20.0, connect=15.0)
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, headers={"Authorization": auth})
            if r.status_code != 200:
                logger.warning("[MCP] skill-store-admin HTTP %s", r.status_code)
                return False
            data = r.json()
            return bool(data.get("is_skill_store_admin"))
        except httpx.RequestError as e:
            last_err = e
            logger.warning(
                "[MCP] skill-store-admin 网络失败 attempt=%s/3（已带 Authorization，非缺 token）: %s",
                attempt,
                e,
            )
            if attempt < 3:
                await asyncio.sleep(0.4 * attempt)
        except Exception as e:
            last_err = e
            logger.warning("[MCP] skill-store-admin 请求异常: %s", e)
            break
    if last_err:
        logger.warning("[MCP] skill-store-admin 最终失败，调试能力将视为非管理员: %s", last_err)
    return False


def _load_upstream_urls() -> Dict[str, str]:
    urls: Dict[str, str] = {}
    try:
        p = Path(__file__).resolve().parent.parent / "upstream_urls.json"
        if p.exists():
            parsed = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        urls[k.strip()] = v.strip()
    except Exception:
        pass
    if CAPABILITY_UPSTREAM_URLS_JSON:
        try:
            parsed = json.loads(CAPABILITY_UPSTREAM_URLS_JSON)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        urls[k.strip()] = v.strip()
        except Exception:
            pass
    # 在线版：速推必须走认证中心 mcp-gateway（与 upstream_urls.json / 环境变量中直连 xskill 互斥时以本规则为准）
    edition = (os.environ.get("LOBSTER_EDITION") or "standalone").strip().lower()
    auth_base = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if edition == "online" and auth_base:
        urls["sutui"] = f"{auth_base}/mcp-gateway"
    elif edition == "online" and not auth_base:
        # 在线版未配 AUTH_SERVER_BASE 时禁止使用文件中的直连 xskill，避免误报「Token 未配置」
        urls.pop("sutui", None)
    return urls


def _get_token_from_request(request: Request) -> Optional[str]:
    qp = request.query_params
    token = qp.get("token") or qp.get("api_key")
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip() or None
    if not token:
        user_auth = request.headers.get("x-user-authorization") or ""
        if user_auth.lower().startswith("bearer "):
            token = user_auth[7:].strip() or None
    if not token:
        user_token = (request.headers.get("x-user-token") or "").strip()
        token = user_token or None
    return token or None


def _backend_headers(token: Optional[str], request: Optional[Request] = None) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    if request is not None:
        xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
        if xi:
            h["X-Installation-Id"] = xi
        cm = (request.headers.get("X-Chat-Model") or request.headers.get("x-chat-model") or "").strip()
        if cm:
            h["X-Chat-Model"] = cm
    bk = (os.environ.get("LOBSTER_MCP_BILLING_INTERNAL_KEY") or "").strip()
    if bk:
        h["X-Lobster-Mcp-Billing"] = bk
    return h


_COMFLY_VEO_MCP_POLL_INTERVAL = 15
_COMFLY_VEO_MCP_POLL_MAX_SEC = 35 * 60
_COMFLY_DAIHUO_MCP_POLL_INTERVAL = 15.0
_COMFLY_DAIHUO_MCP_POLL_MAX_SEC = 7200  # 整包流水线可能极长（2 小时内轮询）


def _mcp_comfly_upstream_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            u = json.loads(raw)
            return u if isinstance(u, dict) else {}
        except Exception:
            return {}
    return {}


def _mcp_comfly_poll_status_from_upstream(upstream: Dict[str, Any]) -> str:
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


def _mcp_comfly_poll_status_is_terminal(status: str) -> bool:
    """与 backend chat._comfly_poll_status_is_terminal 一致：Comfly Veo 文档 SUCCESS / FAILURE 等终态。"""
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


def _mcp_comfly_veo_status_is_failed(status: str) -> bool:
    u = (status or "").strip().upper().replace(" ", "_")
    if u in ("FAILED", "FAILURE", "ERROR", "CANCELLED", "CANCELED", "REJECTED", "TIMEOUT", "TIMED_OUT"):
        return True
    low = (status or "").strip().lower()
    return low in ("failed", "failure", "error", "cancelled", "canceled", "rejected", "timeout")


def _mcp_comfly_video_url_from_upstream(upstream: Dict[str, Any]) -> str:
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


def _mcp_comfly_veo_poll_should_continue_from_r1(r1: Dict[str, Any]) -> bool:
    """与 backend chat._comfly_veo_poll_should_continue 对 poll_video 壳层 + 上游 body 的判断一致。"""
    if not r1 or not r1.get("ok", True):
        return False
    if (r1.get("action") or "").strip() != "poll_video":
        return False
    upstream = _mcp_comfly_upstream_dict(r1.get("result"))
    if not upstream:
        return False
    url = _mcp_comfly_video_url_from_upstream(upstream)
    st = _mcp_comfly_poll_status_from_upstream(upstream)
    if url and (not st or _mcp_comfly_poll_status_is_terminal(st)):
        return False
    if _mcp_comfly_veo_status_is_failed(st):
        return False
    if st and _mcp_comfly_poll_status_is_terminal(st) and not url:
        return False
    return True


async def _mcp_poll_comfly_veo_after_submit(
    *,
    base_url: str,
    token: Optional[str],
    task_id: str,
    request: Optional[Request],
    initial_submit_data: Dict[str, Any],
) -> Dict[str, Any]:
    tid = (task_id or "").strip()
    if not tid:
        return initial_submit_data
    poll_body = {"payload": {"action": "poll_video", "task_id": tid}}
    waited = 0
    data: Dict[str, Any] = dict(initial_submit_data) if isinstance(initial_submit_data, dict) else {}
    while waited < _COMFLY_VEO_MCP_POLL_MAX_SEC:
        logger.info(
            "[MCP comfly.veo] 自动 poll_video waited=%ss task_id=%s",
            waited,
            tid[:96],
        )
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                pr = await client.post(
                    f"{base_url.rstrip('/')}/api/comfly-veo/run",
                    json=poll_body,
                    headers=_backend_headers(token, request),
                )
        except Exception as e:
            logger.warning("[MCP comfly.veo] poll_video 请求异常: %s", e)
            break
        if pr.status_code >= 400:
            logger.warning("[MCP comfly.veo] poll_video HTTP %s", pr.status_code)
            try:
                data = pr.json() if pr.content else {"ok": False, "error": (pr.text or "")[:500]}
            except Exception:
                data = {"ok": False, "error": (pr.text or "")[:500]}
            break
        data = pr.json() if pr.content else {}
        if not _mcp_comfly_veo_poll_should_continue_from_r1(data):
            break
        await asyncio.sleep(_COMFLY_VEO_MCP_POLL_INTERVAL)
        waited += _COMFLY_VEO_MCP_POLL_INTERVAL
    return data


async def _mcp_poll_daihuo_pipeline_until_done(
    *,
    base_url: str,
    token: Optional[str],
    job_id: str,
    request: Optional[Request],
) -> Dict[str, Any]:
    """start_pipeline 返回 job_id 后，定时查询直至 completed / failed（与前端分步轮询一致）。"""
    jid = (job_id or "").strip().lower()
    if not jid:
        return {"ok": False, "error": "缺少 job_id"}
    waited = 0.0
    last: Dict[str, Any] = {}
    bu = base_url.rstrip("/")
    while waited < float(_COMFLY_DAIHUO_MCP_POLL_MAX_SEC):
        logger.info(
            "[MCP comfly.veo.daihuo_pipeline] poll job waited=%ss job_id=%s",
            int(waited),
            jid[:16],
        )
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                pr = await client.get(
                    f"{bu}/api/comfly-daihuo/pipeline/jobs/{jid}",
                    headers=_backend_headers(token, request),
                )
        except Exception as e:
            logger.warning("[MCP comfly.veo.daihuo_pipeline] poll job 请求异常: %s", e)
            break
        if pr.status_code >= 400:
            logger.warning("[MCP comfly.veo.daihuo_pipeline] poll job HTTP %s", pr.status_code)
            try:
                last = pr.json() if pr.content else {"ok": False, "error": (pr.text or "")[:500]}
            except Exception:
                last = {"ok": False, "error": (pr.text or "")[:500]}
            break
        last = pr.json() if pr.content else {}
        st = (last.get("status") or "").strip().lower()
        if st in ("completed", "failed"):
            return last
        await asyncio.sleep(_COMFLY_DAIHUO_MCP_POLL_INTERVAL)
        waited += _COMFLY_DAIHUO_MCP_POLL_INTERVAL
    if last and isinstance(last, dict):
        last.setdefault(
            "poll_timeout",
            f"已达最大等待 {_COMFLY_DAIHUO_MCP_POLL_MAX_SEC}s，任务可能仍在运行，请用 poll_pipeline + job_id 继续查询",
        )
    return last if last else {"ok": False, "error": "poll 无有效响应"}


def _capabilities_api_base() -> str:
    """积分预扣/退还等与认证中心一致；在线版优先直连 AUTH_SERVER_BASE，避免本机代理异常时误判为网络故障。"""
    auth = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")
    if auth:
        return auth
    return BASE_URL.rstrip("/")


async def _find_account_id_by_nickname(
    nickname: str, token: Optional[str], request: Optional[Request] = None
) -> Tuple[Optional[int], Optional[str]]:
    """
    从后端 /api/accounts 按昵称解析账号 id。
    返回 (account_id, err_detail)：
    - 成功： (id, None)
    - 未找到昵称： (None, None)
    - 失败（网络/HTTP/重名）： (None, 可读错误说明)
    """
    nick = (nickname or "").strip()
    if not nick:
        return None, "昵称为空"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token, request))
        if r.status_code != 200:
            return None, f"获取账号列表失败 HTTP {r.status_code}"
        data = r.json() if r.content else {}
        matches: list[int] = []
        for a in data.get("accounts", []) or []:
            if (a.get("nickname") or "").strip() == nick:
                try:
                    matches.append(int(a.get("id")))
                except (TypeError, ValueError):
                    continue
        if len(matches) == 0:
            return None, None
        if len(matches) > 1:
            return None, (
                f"存在多个昵称为「{nick}」的账号，请改用 account_id（见 list_publish_accounts 的 id）"
            )
        return matches[0], None
    except Exception as e:
        logger.warning("[MCP] _find_account_id_by_nickname failed: %s", e)
        return None, f"获取账号列表失败: {e}"


async def _find_account_platform_by_nickname(
    nickname: str, token: Optional[str], request: Optional[Request] = None
) -> Tuple[Optional[str], Optional[str]]:
    """
    按昵称从 /api/accounts 取 platform（小写），用于 MCP 侧对头条无素材发布自动补 options。
    返回 (platform, err)；未找到昵称则为 (None, None)。
    """
    nick = (nickname or "").strip()
    if not nick:
        return None, "昵称为空"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token, request))
        if r.status_code != 200:
            return None, f"获取账号列表失败 HTTP {r.status_code}"
        data = r.json() if r.content else {}
        matches: list[Dict[str, Any]] = []
        for a in data.get("accounts", []) or []:
            if (a.get("nickname") or "").strip() == nick:
                if isinstance(a, dict):
                    matches.append(a)
        if len(matches) == 0:
            return None, None
        if len(matches) > 1:
            return None, (
                f"存在多个昵称为「{nick}」的账号，请改用 account_id（见 list_publish_accounts 的 id）"
            )
        plat = (matches[0].get("platform") or "").strip().lower()
        return plat, None
    except Exception as e:
        logger.warning("[MCP] _find_account_platform_by_nickname failed: %s", e)
        return None, f"获取账号列表失败: {e}"


def _tool_definitions(catalog: Dict[str, Dict[str, Any]], *, is_skill_store_admin: bool = True) -> List[Dict[str, Any]]:
    capability_list = sorted(
        cid
        for cid in catalog.keys()
        if not (_capability_id_is_debug_only_in_registry(cid) and not is_skill_store_admin)
    )
    tools = [
        {
            "name": "list_capabilities",
            "description": "列出龙虾当前可用的全部能力",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "invoke_capability",
            "description": (
                "调用龙虾能力（图片生成、视频解析、语音合成等）。"
                "capability_id 必须是 list_capabilities 中的能力 ID；"
                "禁止将素材库 asset_id（十二位以上十六进制串，如入库后的成片 ID）当作能力 ID。"
                "向抖音/头条/小红书发文请用 publish_content，asset_id 填素材 ID。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "capability_id": {
                        "type": "string",
                        "enum": capability_list,
                        "description": "能力 ID（与 list_capabilities 一致，勿填素材 asset_id）",
                    },
                    "payload": {
                        "type": "object",
                        "description": "能力调用参数",
                    },
                },
                "required": ["capability_id", "payload"],
            },
        },
        {
            "name": "manage_skills",
            "description": (
                "管理龙虾技能包：\n"
                "- list_store: 浏览本地技能商店\n"
                "- list_installed: 查看已安装技能\n"
                "- install: 安装商店中的技能包 (需 package_id)\n"
                "- uninstall: 卸载技能包 (需 package_id)\n"
                "- search_online: 搜索全球 MCP 在线技能库 (需 query，如 'image', 'database', 'search')\n"
                "- add_mcp: 添加 MCP 服务连接 (需 name + url)"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list_store", "list_installed", "install", "uninstall", "search_online", "add_mcp"],
                        "description": "操作类型",
                    },
                    "package_id": {
                        "type": "string",
                        "description": "技能包 ID（install/uninstall 时必填）",
                    },
                    "query": {
                        "type": "string",
                        "description": "搜索关键词（search_online 时使用，如 image, video, database, github）",
                    },
                    "name": {
                        "type": "string",
                        "description": "MCP 连接名称（add_mcp 时必填）",
                    },
                    "url": {
                        "type": "string",
                        "description": "MCP 服务地址（add_mcp 时必填）",
                    },
                },
                "required": ["action"],
            },
        },
        {
            "name": "save_asset",
            "description": (
                "将素材 URL 保存到本机素材库（等价于调后端 save-url），返回 asset_id。"
                "若 invoke_capability(task.get_result/image.generate) 返回的 JSON 里 saved_assets 已有 asset_id，"
                "或用户已提供素材 ID：不要调用本工具，直接用该 asset_id 发布或引用。"
                "仅在仅有 CDN URL、尚无入库 ID 时使用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "素材URL（图片或视频链接）"},
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"], "description": "素材类型"},
                    "tags": {"type": "string", "description": "标签，逗号分隔"},
                    "prompt": {"type": "string", "description": "生成该素材时使用的提示词"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "list_assets",
            "description": "列出或搜索本地保存的素材（图片、视频等）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "media_type": {"type": "string", "enum": ["image", "video", "audio"], "description": "按类型筛选"},
                    "query": {"type": "string", "description": "搜索关键词（匹配标签、提示词、文件名）"},
                    "limit": {"type": "integer", "description": "返回数量，默认20"},
                },
            },
        },
        {
            "name": "list_publish_accounts",
            "description": "列出已配置的发布账号（抖音、B站等平台）",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_creator_publish_data",
            "description": (
                "读取本机已同步的**创作者发布作品数据**（抖音、小红书、今日头条头条号）：每条含标题、metrics（各平台字段名可能不同）、"
                "头条号可能另有 meta_summary.toutiao_insights。用户问「总体/各平台/某账号」的播放量、互动、作品表现时使用；"
                "用返回的 JSON 直接分析回答，勿编造数字。若用户要「最新」数据，先调用 sync_creator_publish_data 再调用本工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["all", "platform", "account"],
                        "description": "all=全部可同步账号 · platform=只某平台 · account=单个账号",
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["douyin", "xiaohongshu", "toutiao"],
                        "description": "scope=platform 时必填",
                    },
                    "account_id": {"type": "integer", "description": "scope=account 时可选"},
                    "account_nickname": {
                        "type": "string",
                        "description": "scope=account 时可选，与 list_publish_accounts 中昵称一致",
                    },
                },
            },
        },
        {
            "name": "sync_creator_publish_data",
            "description": (
                "从抖音/小红书/今日头条创作者后台**同步**作品列表与数据到本机（Playwright，可能耗时数分钟，需对应账号已登录）。"
                "用户说「同步所有账号」「拉最新发布数据」「更新各平台数据」时设 sync_all=true；"
                "只更新某一平台可加 platform；只更新某一账号用 account_id 或 account_nickname。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sync_all": {
                        "type": "boolean",
                        "description": "true=同步全部抖音/小红书/头条账号（可配合 platform）；false 时必须指定 account_id 或 account_nickname",
                    },
                    "platform": {
                        "type": "string",
                        "enum": ["douyin", "xiaohongshu", "toutiao"],
                        "description": "仅同步该平台（可与 sync_all 同用）",
                    },
                    "account_id": {"type": "integer", "description": "仅同步此发布账号"},
                    "account_nickname": {"type": "string", "description": "仅同步此昵称账号"},
                    "headless": {"type": "boolean", "description": "是否无头浏览器，可省略"},
                },
            },
        },
        {
            "name": "list_youtube_accounts",
            "description": "列出本机已配置的 YouTube 上传账号。每条含 youtube_account_id（以 yt_ 开头），对话里用户说「发到 YouTube 的哪个账号」时须使用该 ID 调用 publish_youtube_video。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "publish_youtube_video",
            "description": "将素材库中的视频上传到指定 YouTube 账号（YouTube Data API）。youtube_account_id 必须来自 list_youtube_accounts；不可使用 publish_content 发布到 YouTube。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string", "description": "素材库 asset_id（视频）"},
                    "youtube_account_id": {
                        "type": "string",
                        "description": "YouTube 账号 ID，格式 yt_ 开头，来自 list_youtube_accounts",
                    },
                    "title": {"type": "string", "description": "视频标题，可省略"},
                    "description": {"type": "string", "description": "说明，可省略"},
                    "privacy_status": {
                        "type": "string",
                        "enum": ["private", "unlisted", "public"],
                        "description": "可见性，默认 private",
                    },
                    "category_id": {"type": "string", "description": "YouTube 分类 ID（如 22=People & Blogs），可省略"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "视频标签，可省略"},
                    "material_origin": {
                        "type": "string",
                        "enum": ["original", "ai_generated", "mixed"],
                        "description": "素材来源（original/ai_generated/mixed），默认按素材自动判断",
                    },
                },
                "required": ["asset_id", "youtube_account_id"],
            },
        },
        {
            "name": "get_youtube_analytics",
            "description": "获取指定 YouTube 账号的频道数据：视频列表（含播放、点赞、评论）+ 频道分析（近 28 天观看、订阅等）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "youtube_account_id": {
                        "type": "string",
                        "description": "YouTube 账号 ID（yt_ 开头，来自 list_youtube_accounts）",
                    },
                },
                "required": ["youtube_account_id"],
            },
        },
        {
            "name": "sync_youtube_analytics",
            "description": "从 YouTube API 拉取最新的视频统计与频道分析数据。完成后用 get_youtube_analytics 查看。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "youtube_account_id": {
                        "type": "string",
                        "description": "YouTube 账号 ID（yt_ 开头，来自 list_youtube_accounts）",
                    },
                },
                "required": ["youtube_account_id"],
            },
        },
        # ── Meta Social（Instagram / Facebook）──
        {
            "name": "list_meta_social_accounts",
            "description": "列出已连接的 Instagram / Facebook 账号。每条含 id、label、平台信息；发布或查询数据时需先获取 account_id。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "publish_meta_social",
            "description": (
                "发布内容到 Instagram 或 Facebook 主页。"
                "Instagram 支持 photo/video/carousel/reel/story；Facebook 支持 photo/video/link。"
                "asset_id 填素材库 ID，系统自动解析公网 URL；也可直接传 image_url/video_url。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "Meta 账号 ID（来自 list_meta_social_accounts）"},
                    "platform": {"type": "string", "enum": ["instagram", "facebook"], "description": "目标平台"},
                    "content_type": {
                        "type": "string",
                        "enum": ["photo", "video", "carousel", "reel", "story", "link"],
                        "description": "内容类型",
                    },
                    "asset_id": {"type": "string", "description": "素材库 asset_id（优先使用）"},
                    "image_url": {"type": "string", "description": "图片直链（无 asset_id 时使用）"},
                    "video_url": {"type": "string", "description": "视频直链（无 asset_id 时使用）"},
                    "caption": {"type": "string", "description": "文案 / 描述"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "话题标签"},
                    "link": {"type": "string", "description": "链接（仅 Facebook link 类型）"},
                    "title": {"type": "string", "description": "标题（仅 Facebook 视频）"},
                    "carousel_items": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": '轮播子项（仅 IG carousel）：[{"image_url":"..."} 或 {"video_url":"..."}]',
                    },
                },
                "required": ["account_id", "platform", "content_type"],
            },
        },
        {
            "name": "get_meta_social_data",
            "description": (
                "读取已同步的 Instagram / Facebook 数据：帖子列表（含 likes、comments、reach 等指标）+ 账号级 Insights。"
                "用户问 IG/FB 表现、数据、互动率时使用；若需最新数据，先调 sync_meta_social_data 再调本工具。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "可选，指定账号；不填返回全部"},
                    "platform": {"type": "string", "enum": ["instagram", "facebook"], "description": "可选，筛选平台"},
                },
            },
        },
        {
            "name": "sync_meta_social_data",
            "description": (
                "从 Instagram / Facebook API 拉取最新帖子列表与 Insights 数据到本地（耗时数秒到十几秒）。"
                "同步完成后用 get_meta_social_data 读取结果。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "integer", "description": "可选，指定账号；不填同步全部"},
                },
            },
        },
        {
            "name": "get_social_report",
            "description": (
                "跨平台数据报告：聚合 Instagram + Facebook 所有已连接账号的数据摘要（帖子数、总 likes/comments/reach 等）。"
                "用户问「所有平台表现」「数据总览」「跨平台对比」时使用。数据基于最近一次同步快照。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "open_account_browser",
            "description": (
                "打开指定账号的浏览器窗口（会激活到最前面）；未登录时会看到登录页。"
                "仅在用户要求打开、或 publish/sync 等已返回未登录/需重新登录时使用。"
                "用户只说「发布/发文」时不要主动提议「先确认登录」——应直接 publish_content，失败后再打开浏览器。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string", "description": "账号昵称"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "check_account_login",
            "description": (
                "检查指定账号是否已在浏览器中登录（不新开窗口）。"
                "适用于用户说「登录好了」或发布返回需验证登录时；不要仅因「即将发布」就先调用。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_nickname": {"type": "string", "description": "账号昵称"},
                },
                "required": ["account_nickname"],
            },
        },
        {
            "name": "publish_content",
            "description": (
                "将素材发布到指定平台账号。account_nickname 与 list_publish_accounts 里「昵称」一致。"
                "**小红书**：须传 title，且 description 与 tags 至少一项。"
                "用户口头要「AI 写/帮我写文案」等时，你应在工具参数中自动启用 AI 代写并把口述要点写入 description；"
                "对用户的回复里不要出现英文参数字段名或「请设为 true」类话术。"
                "**抖音/头条**：未传 title+description 时可由后端 AI 补全。"
                "账号已在发布管理配置且用户要求发布时：直接调用本工具，勿先让用户确认「是否已登录」；未登录时由返回错误再处理。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": (
                            "素材ID：来自 save_asset 或 task.get_result 的 saved_assets[0].asset_id。"
                            "今日头条无封面纯文发布可省略，但必须在 options 中设 toutiao_graphic_no_cover: true。"
                        ),
                    },
                    "account_nickname": {
                        "type": "string",
                        "description": "发布账号昵称（必填）：与用户/发布管理里对该账号的称呼一致，如 6、2号、头条1。勿使用 list_publish_accounts 返回的数字 id。",
                    },
                    "title": {"type": "string", "description": "标题。小红书≤20字；抖音图文≤20、视频≤30；头条宜≤30"},
                    "description": {"type": "string", "description": "描述/正文。小红书宜≤1000字；抖音与tags合并后约≤500字；头条宜≤5000字"},
                    "tags": {"type": "string", "description": "话题标签，逗号分隔（抖音会转成 #话题 拼入描述侧，占用500字额度）"},
                    "ai_publish_copy": {
                        "type": "boolean",
                        "description": (
                            "可选：仅助手根据用户话里意图自动填写，勿让用户手填。"
                            "小红书：用户要 AI 代写时启用，且 description 须含其口述要点；否则须 title +（description 或 tags）。"
                            "抖音/头条：启用=强制走 AI 文案；禁用=不用 AI；省略=未传标题+正文时后端可能自动 AI。"
                        ),
                    },
                    "cover_asset_id": {
                        "type": "string",
                        "description": (
                            "可选：仅当用户**明确**要指定封面/头图并给出图片素材 ID 时填写。"
                            "用户只说「用某视频素材发布」而未提封面时，**必须省略**本字段。"
                        ),
                    },
                    "options": {
                        "type": "object",
                        "description": (
                            "可选：平台发布参数（抖音 best-effort）。常用字段示例：\n"
                            "- visibility: public|friends|private\n"
                            "- schedule_publish: {enabled:true, datetime:\"YYYY-MM-DD HH:mm\"}\n"
                            "- location: \"深圳市南山区\"\n"
                            "- allow_comment / allow_duet / allow_stitch: true|false\n"
                            "- goods: {enabled:true, keyword:\"商品关键词\"}\n"
                            "今日头条发文章：用户未提供配图时设 toutiao_graphic_no_cover: true（无封面纯文）；"
                            "有配图则用图片 asset 作封面，可选 cover_asset_id 作第二图。\n"
                        ),
                    },
                },
                "required": ["account_nickname"],
            },
        },
    ]
    if not is_skill_store_admin:
        tools = [t for t in tools if (t.get("name") or "") not in _DEBUG_ONLY_MCP_TOOL_NAMES]
    return tools


def _redact_sensitive(value: Any) -> Any:
    blocked_keys = {"api_key", "apikey", "token", "balance", "points", "credits", "account_id"}
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if str(k).lower() in blocked_keys:
                continue
            out[k] = _redact_sensitive(v)
        return out
    if isinstance(value, list):
        return [_redact_sensitive(x) for x in value]
    if isinstance(value, str):
        return re.sub(r"(sk-[A-Za-z0-9]{10,})", "[REDACTED]", value)
    return value


_VIDEO_ASPECT_RATIOS = ("21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def _payload_get_aspect_ratio(payload: Dict[str, Any]) -> Any:
    """速推 / 前端可能用 ratio 或 aspect_ratio。"""
    if payload.get("aspect_ratio") is not None:
        return payload.get("aspect_ratio")
    return payload.get("ratio")


def _payload_get_duration_raw(payload: Dict[str, Any]) -> Any:
    """duration / duration_seconds / length 等别名。"""
    for key in ("duration", "duration_seconds", "length", "video_length"):
        if payload.get(key) is not None:
            return payload.get(key)
    return None


def _coerce_video_aspect_ratio_for_upstream(raw: Any) -> str:
    """
    将 UI 与速推常见写法规范为 xskill 接受的宽高比枚举。
    无法识别时回退 16:9，避免上游 422（与官方「参数容错」一致）。
    """
    if raw is None or raw == "":
        return "16:9"
    ar = str(raw).strip()
    low = ar.lower().replace(" ", "")
    if low in ("auto", "automatic", "default", "original", "adapt"):
        return "16:9"
    if low in ("landscape", "横屏", "horizontal", "wide"):
        return "16:9"
    if low in ("portrait", "竖屏", "vertical", "tall"):
        return "9:16"
    if low in ("square", "1x1"):
        return "1:1"
    if "x" in ar and ":" not in ar:
        parts = ar.lower().replace(" ", "").split("x", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            ar = f"{parts[0]}:{parts[1]}"
    ar = ar.replace("：", ":").strip()
    if ar in _VIDEO_ASPECT_RATIOS:
        return ar
    ar2 = ar.replace(" ", "")
    if ar2 in _VIDEO_ASPECT_RATIOS:
        return ar2
    return "16:9"


def _parse_video_duration_seconds(raw: Any, *, default: int = 5) -> int:
    """解析 5、6s、\"10\" 等为整数秒；无法解析时用 default，避免抛错。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return default
    try:
        if isinstance(raw, (int, float)):
            return max(1, int(raw))
        s = str(raw).strip().lower()
        if s.endswith("s"):
            s = s[:-1].strip()
        if not s:
            return default
        v = float(s)
        return max(1, int(round(v)))
    except (ValueError, TypeError, OverflowError):
        return default


# fal-ai/sora-2/* 上游 duration 枚举；非法值易 422
_SORA_FAL_DURATION_SECONDS = (4, 8, 12, 16, 20)


def _coerce_sora_fal_duration_seconds(sec: int) -> int:
    """将秒数收敛到 fal Sora 2 允许的 duration；距离相同时取较小值。"""
    try:
        s = max(1, int(sec))
    except (ValueError, TypeError, OverflowError):
        return _SORA_FAL_DURATION_SECONDS[0]
    if s in _SORA_FAL_DURATION_SECONDS:
        return s
    best = _SORA_FAL_DURATION_SECONDS[0]
    best_d = abs(s - best)
    for a in _SORA_FAL_DURATION_SECONDS[1:]:
        d = abs(s - a)
        if d < best_d or (d == best_d and a < best):
            best, best_d = a, d
    return best


def _sanitize_video_resolution_value(raw: Any) -> Optional[str]:
    """UI 常见 resolution=auto 等占位：返回 None 表示不要传该字段，避免上游枚举校验 422。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower().replace(" ", "")
    if low in ("auto", "automatic", "default", "original"):
        return None
    return s


def _sanitize_options_dict_resolution(options: Dict[str, Any]) -> None:
    """Seedance 等 options.resolution 合并后去掉 auto 占位。"""
    if not isinstance(options, dict) or "resolution" not in options:
        return
    sr = _sanitize_video_resolution_value(options.get("resolution"))
    if sr is None:
        options.pop("resolution", None)
    else:
        options["resolution"] = sr


def _merge_common_video_ui_fields(out: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """合并速推 / xskill UI 常见顶层字段（不覆盖已写入的 model/prompt/image_url 等核心键）。"""
    for k in (
        "enable_prompt_expansion",
        "multi_shots",
        "enable_safety_checker",
        "resolution",
        "audio",
        "seed",
        "negative_prompt",
        "camera_fixed",
        "style",
        "mode",
        "fps",
        "cfg_scale",
        "motion_bucket_id",
        "consistency_with_text",
    ):
        if k == "resolution":
            if k in out:
                continue
            if k in payload and payload[k] is not None:
                sr = _sanitize_video_resolution_value(payload[k])
                if sr is not None:
                    out[k] = sr
            continue
        if k in payload and payload[k] is not None and k not in out:
            out[k] = payload[k]


def _normalize_image_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按图片模型把「统一 payload」转成该模型 API 需要的参数，并保证用户输入的 prompt 原样传入。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    model = (payload.get("model") or payload.get("model_id") or "").strip()
    if not model:
        raise ValueError("请指定图片模型（model），例如 flux-2/flash、seedream、nano-banana-pro、jimeng-4.5、gemini 等。")
    prompt = (payload.get("prompt") or "").strip()
    image_url = (payload.get("image_url") or "").strip()
    image_size = (payload.get("image_size") or "").strip()
    num_images = payload.get("num_images", payload.get("n", 1))
    if isinstance(num_images, (int, float)):
        num_images = max(1, int(num_images))

    # jimeng-4.0 / jimeng-4.5：prompt 必填，image_url 可选，n
    if "jimeng-" in model:
        out: Dict[str, Any] = {"model": model, "prompt": prompt, "n": num_images}
        if image_url:
            out["image_url"] = image_url
        return out

    # fal-ai/flux-2/flash：prompt, image_urls 数组（图生图）, image_size, num_images
    if "flux-2/flash" in model or "flux-2" in model:
        out = {"model": model, "prompt": prompt, "image_size": image_size or "landscape_4_3", "num_images": num_images}
        if image_url:
            out["image_urls"] = [image_url]
        return out

    # ── i2i 编辑模型：wan/v2.7/edit、seedream/*/edit、qwen-image-edit ──
    _is_edit = "/edit" in model or "image-edit" in model
    if _is_edit or "wan/v2.7" in model:
        _imgs = payload.get("image_urls") or ([image_url] if image_url else [])
        if isinstance(_imgs, str):
            _imgs = [_imgs]
        out = {"model": model, "prompt": prompt}
        if _imgs:
            out["image_urls"] = _imgs
        if image_size:
            out["image_size"] = image_size
        elif "seedream" in model:
            out["image_size"] = "auto_2K"
        if "qwen-image-edit" not in model:
            out["num_images"] = num_images
        neg = (payload.get("negative_prompt") or "").strip()
        if neg:
            out["negative_prompt"] = neg
        return out

    # fal-ai/bytedance/seedream/* (文生图)：prompt, image_size, num_images
    if "seedream" in model:
        return {"model": model, "prompt": prompt, "image_size": image_size or "auto_2K", "num_images": num_images}

    # fal-ai/nano-banana-pro、nano-banana-2：prompt, image_urls 数组（可选）, aspect_ratio, num_images
    if "nano-banana" in model:
        _ar = (payload.get("aspect_ratio") or payload.get("ratio") or "1:1")
        _ar = str(_ar).strip() if _ar is not None else "1:1"
        out = {
            "model": model,
            "prompt": prompt,
            "aspect_ratio": _coerce_video_aspect_ratio_for_upstream(_ar) if _ar else "1:1",
            "num_images": num_images,
        }
        if image_url:
            out["image_urls"] = [image_url]
        return out

    # 其他图片模型：原样传，但保证 prompt 存在，保留所有参数
    out = dict(payload)
    if "model" not in out:
        out["model"] = model
    if not out.get("prompt"):
        out["prompt"] = prompt
    return out


def _normalize_understand_payload(
    payload: Dict[str, Any],
    media_key: str = "image_urls",
    default_model: str = "openrouter/router/vision",
) -> Dict[str, Any]:
    """将 image.understand / video.understand 的统一 payload 转成速推 generate 所需格式。"""
    if not payload or not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    prompt = (payload.get("prompt") or "").strip() or "请详细描述内容。"
    model = (payload.get("model") or "").strip() or default_model

    urls = payload.get(media_key)
    if not urls:
        singular = media_key.replace("_urls", "_url")
        single = (payload.get(singular) or payload.get("image_url") or payload.get("video_url") or "").strip()
        if single:
            urls = [single]
    if isinstance(urls, str):
        urls = [urls]
    out: Dict[str, Any] = {"model": model, "prompt": prompt}
    if urls:
        out[media_key] = urls
    for k in ("system_prompt", "max_tokens", "temperature", "reasoning"):
        if k in payload:
            out[k] = payload[k]
    return out


def _normalize_video_generate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    按视频模型把「统一 payload」转成该模型 API 需要的参数，与 lobster 对齐：支持 backend 注入的 filePaths/media_files。
    """
    if not payload or not isinstance(payload, dict):
        return payload
    model = (payload.get("model") or payload.get("model_id") or "").strip()
    if not model:
        raise ValueError("请指定视频模型（model），例如 sora2、seedance2、hailuo、vidu、wan、veo、kling、grok、jimeng 等。")
    prompt = (payload.get("prompt") or "").strip()
    fp = payload.get("filePaths") or []
    image_url = (payload.get("image_url") or "").strip()
    mf = payload.get("media_files") or []
    has_image = bool(fp) or bool(image_url) or bool(mf)
    model = resolve_video_model_id(model, has_image)
    model_lower = model.lower()
    first_url = (str(fp[0]) if fp else "") or image_url or (str(mf[0]) if mf else "")
    if not first_url and image_url:
        first_url = image_url
    aspect_ratio = _coerce_video_aspect_ratio_for_upstream(_payload_get_aspect_ratio(payload))
    valid_ratios = _VIDEO_ASPECT_RATIOS
    ratio_ok = aspect_ratio in valid_ratios
    duration_sec = _parse_video_duration_seconds(_payload_get_duration_raw(payload), default=5)

    # st-ai/super-seed2：ratio, filePaths, functionMode（保留 backend 注入的多图 filePaths）
    if "super-seed2" in model or "st-ai/super-seed2" == model:
        out: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "functionMode": "first_last_frames",
            "ratio": aspect_ratio if ratio_ok else "16:9",
            "duration": duration_sec,
        }
        out["filePaths"] = list(fp) if fp else ([first_url] if first_url else [])
        _merge_common_video_ui_fields(out, payload)
        return out

    # wan（v2.6 / v2.7）：duration 为字符串，i2v 用 image_url，t2v 用 aspect_ratio
    if model.startswith("wan/"):
        out = {"model": model, "prompt": prompt, "duration": str(duration_sec)}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        if "text-to-video" in model or not first_url:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        _wr = _sanitize_video_resolution_value(payload.get("resolution"))
        if _wr is not None:
            out["resolution"] = _wr
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/minimax/hailuo*：prompt, image_url（i2v）
    if "hailuo" in model or "minimax" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/vidu/q3/*：i2v 必填 image_url，t2v 无 image_url；duration(int)
    if "vidu" in model:
        out = {"model": model, "prompt": prompt or "", "duration": duration_sec}
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        _vr = _sanitize_video_resolution_value(payload.get("resolution"))
        if _vr is not None:
            out["resolution"] = _vr
        _merge_common_video_ui_fields(out, payload)
        return out

    # fal-ai/bytedance/seedance/v1/* 和 v1.5/*：i2v 必填 image_url，duration 字符串, aspect_ratio
    # 注意：v1 和 v1.5 使用 options 对象包裹额外参数（resolution, generate_audio, camera_fixed, seed, end_image_url 等）
    if "seedance/v1" in model or "/seedance/v1/" in model or "seedance/v1.5" in model or "/seedance/v1.5/" in model:
        out = {
            "model": model,
            "prompt": prompt,
            "duration": str(duration_sec),
        }
        # aspect_ratio 在顶层（v1.5 和 v1 都支持）
        if aspect_ratio and ratio_ok:
            out["aspect_ratio"] = aspect_ratio
        # image_url 在顶层（i2v 时）
        if "image-to-video" in model and first_url:
            out["image_url"] = first_url
        # 额外参数放入 options 对象（根据 xskill 文档）
        options: Dict[str, Any] = {}
        _sd_res = _sanitize_video_resolution_value(payload.get("resolution"))
        if _sd_res is not None:
            options["resolution"] = _sd_res
        if payload.get("generate_audio") is not None:
            options["generate_audio"] = bool(payload.get("generate_audio"))
        if payload.get("camera_fixed") is not None:
            options["camera_fixed"] = bool(payload.get("camera_fixed"))
        if payload.get("seed") is not None:
            try:
                options["seed"] = int(payload.get("seed"))
            except (ValueError, TypeError):
                options["seed"] = payload.get("seed")
        if payload.get("end_image_url"):
            options["end_image_url"] = str(payload.get("end_image_url"))
        if payload.get("reference_image_urls"):
            options["reference_image_urls"] = payload.get("reference_image_urls")
        if payload.get("enable_safety_checker") is not None:
            options["enable_safety_checker"] = bool(payload.get("enable_safety_checker"))
        for _k in ("enable_prompt_expansion", "multi_shots"):
            if payload.get(_k) is not None:
                options[_k] = bool(payload.get(_k))
        # 如果用户直接传了 options 对象，合并进去
        if payload.get("options") and isinstance(payload.get("options"), dict):
            options.update(payload.get("options"))
        _sanitize_options_dict_resolution(options)
        # 只有 options 不为空时才添加
        if options:
            out["options"] = options
        _merge_common_video_ui_fields(out, payload)
        return out

    # Sora 2 系列（sora-2/pub, sora-2/vip, sora-2/pro）：通用格式，i2v 用 image_url，t2v 用 aspect_ratio
    # /characters 为角色创建，勿走视频生成参数分支；resolution 由 _merge_common_video_ui_fields 统一净化。
    # duration：fal 仅允许 4/8/12/16/20；未传时用 4，勿用全局 default=5
    if "sora-2" in model.lower() and "/characters" not in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        _sora_d_raw = _payload_get_duration_raw(payload)
        _sora_sec = _parse_video_duration_seconds(_sora_d_raw, default=4)
        _sora_d = _coerce_sora_fal_duration_seconds(_sora_sec)
        if _sora_d != _sora_sec:
            logger.info(
                "[MCP] Sora duration 已收敛为 fal 枚举: raw=%r parsed=%s -> %s",
                _sora_d_raw,
                _sora_sec,
                _sora_d,
            )
        out["duration"] = _sora_d
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Kling 系列（kling-video, kling-o3）：i2v 用 image_url，支持 duration 和 resolution
    if "kling" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        out["duration"] = duration_sec
        _kr = _sanitize_video_resolution_value(payload.get("resolution"))
        if _kr is not None:
            out["resolution"] = _kr
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Veo 3.1 系列：i2v 用 image_url，支持 duration 和 resolution
    # duration 必须是字符串格式：'4s', '6s' 或 '8s'
    if "veo" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        # Veo 3.1 的 duration 必须是 '4s', '6s' 或 '8s' 格式（与 _parse_video_duration_seconds 已解析的秒数对齐）
        raw_d = _payload_get_duration_raw(payload)
        if raw_d is not None and raw_d != "":
            if isinstance(raw_d, str) and raw_d.strip().lower().endswith("s"):
                dur_str = raw_d.strip().lower()
                if dur_str in ("4s", "6s", "8s"):
                    out["duration"] = dur_str
                else:
                    out["duration"] = "6s"
            else:
                if duration_sec <= 4:
                    out["duration"] = "4s"
                elif duration_sec <= 6:
                    out["duration"] = "6s"
                else:
                    out["duration"] = "8s"
        else:
            out["duration"] = "6s"
        _ver = _sanitize_video_resolution_value(payload.get("resolution"))
        if _ver is not None:
            out["resolution"] = _ver
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Grok Imagine Video：i2v 用 image_url，支持 duration
    if "grok" in model.lower():
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        out["duration"] = duration_sec
        _ger = _sanitize_video_resolution_value(payload.get("resolution"))
        if _ger is not None:
            out["resolution"] = _ger
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # 即梦系列（jimeng）：i2v 用 image_url，支持 duration
    if "jimeng" in model.lower() or "即梦" in model:
        out = {"model": model, "prompt": prompt}
        if first_url:
            out["image_url"] = first_url
        _has_ar = _payload_get_aspect_ratio(payload) is not None
        if not first_url or _has_ar:
            out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"
        out["duration"] = duration_sec
        _jer = _sanitize_video_resolution_value(payload.get("resolution"))
        if _jer is not None:
            out["resolution"] = _jer
        for k in ["audio", "seed", "negative_prompt"]:
            if k in payload:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # Seedance 1.0/1.5（非 v1/v1.5，即旧版本或特殊变体）：i2v 用 image_url，duration 字符串, aspect_ratio
    # 注意：这些版本可能也使用 options 对象，但为兼容性保留顶层参数
    if "seedance" in model.lower() and "/v1/" not in model.lower() and "/v1.5/" not in model.lower():
        out = {
            "model": model,
            "prompt": prompt,
            "duration": str(duration_sec),
            "aspect_ratio": aspect_ratio if ratio_ok else "16:9",
        }
        if first_url:
            out["image_url"] = first_url
        # 尝试使用 options 对象（如果模型支持）
        options: Dict[str, Any] = {}
        _sd2_res = _sanitize_video_resolution_value(payload.get("resolution"))
        if _sd2_res is not None:
            options["resolution"] = _sd2_res
        if payload.get("generate_audio") is not None:
            options["generate_audio"] = bool(payload.get("generate_audio"))
        if payload.get("camera_fixed") is not None:
            options["camera_fixed"] = bool(payload.get("camera_fixed"))
        if payload.get("seed") is not None:
            try:
                options["seed"] = int(payload.get("seed"))
            except (ValueError, TypeError):
                options["seed"] = payload.get("seed")
        if payload.get("end_image_url"):
            options["end_image_url"] = str(payload.get("end_image_url"))
        if payload.get("reference_image_urls"):
            options["reference_image_urls"] = payload.get("reference_image_urls")
        if payload.get("options") and isinstance(payload.get("options"), dict):
            options.update(payload.get("options"))
        _sanitize_options_dict_resolution(options)
        if options:
            out["options"] = options
        # 保留其他顶层参数（向后兼容）
        for k in ["audio", "negative_prompt"]:
            if k in payload and k not in options:
                out[k] = payload[k]
        _merge_common_video_ui_fields(out, payload)
        return out

    # 其他视频模型：通用处理，确保基本参数正确传递
    # 1. 图生视频（有 image_url/filePaths/media_files）：传递 image_url
    # 2. 文生视频（无图片）：传递 aspect_ratio
    # 3. 保留所有用户传入的参数，不做过滤
    out = dict(payload)
    if "model" not in out:
        out["model"] = model
    if "prompt" not in out or not out.get("prompt"):
        out["prompt"] = prompt
    out["aspect_ratio"] = aspect_ratio
    out["duration"] = duration_sec

    # 统一处理图片 URL：优先使用 backend 注入的 filePaths/media_files
    if first_url and "image_url" not in out:
        out["image_url"] = first_url
    elif first_url:
        # 如果已有 image_url 但 backend 注入了新的，优先用新的
        out["image_url"] = first_url

    # 文生视频时，如果没有 aspect_ratio，添加默认值
    if not first_url and "aspect_ratio" not in out and aspect_ratio:
        out["aspect_ratio"] = aspect_ratio if ratio_ok else "16:9"

    _fr = _sanitize_video_resolution_value(out.get("resolution"))
    if _fr is None:
        out.pop("resolution", None)
    else:
        out["resolution"] = _fr

    _merge_common_video_ui_fields(out, payload)
    return out


def _load_sutui_token() -> str:
    """Read the 速推 token from sutui_config.json."""
    try:
        p = Path(__file__).resolve().parent.parent / "sutui_config.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return (data.get("token") or "").strip()
    except Exception:
        pass
    return ""


def _sutui_upstream_is_server_gateway(server_url: str) -> bool:
    u = (server_url or "").replace("\\", "/").lower()
    return "mcp-gateway" in u


def _unwrap_lobster_server_gateway_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    """经 lobster_server /mcp-gateway 转发到**服务器 lobster MCP** 时，响应为 JSON-RPC；
    tools/call 的正文在同构 lobster 的 result.content[].text 里，为 JSON 字符串，
    结构为 {\"capability_id\", \"result\": <与直连速推 MCP 同形的字典>}。
    解析后返回其中的 result，供本机与直连上游相同的下游逻辑使用。
    """
    if not isinstance(raw, dict):
        return {}
    if raw.get("error"):
        err = raw.get("error")
        if isinstance(err, dict):
            return {"error": {"message": str(err.get("message", err))}}
        return {"error": {"message": str(err)}}
    res = raw.get("result")
    if not isinstance(res, dict):
        return raw
    if res.get("isError"):
        parts: List[str] = []
        for block in res.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append((block.get("text") or "").strip())
        msg = "\n".join(p for p in parts if p) or "上游 invoke_capability 返回 isError"
        return {"error": {"message": msg[:4000]}}
    for block in res.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        txt = (block.get("text") or "").strip()
        if not txt.startswith("{"):
            continue
        try:
            obj = json.loads(txt)
        except Exception:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            return obj["result"]
        if isinstance(obj, dict):
            return obj
    return raw


def _parse_sse_or_json(resp: httpx.Response) -> Dict[str, Any]:
    """Parse response that may be JSON or SSE (text/event-stream)."""
    ct = (resp.headers.get("content-type") or "").lower()
    raw = resp.text.strip()
    if "text/event-stream" in ct or raw.startswith("event:") or raw.startswith("data:"):
        last_data = ""
        for line in raw.splitlines():
            if line.startswith("data:"):
                last_data = line[5:].strip()
        if last_data:
            return json.loads(last_data)
        return {"error": {"message": f"Empty SSE stream from upstream (status={resp.status_code})"}}
    return resp.json()


async def _call_upstream_sutui_tasks_rest(
    api_base: str,
    tool_name: str,
    arguments: Dict[str, Any],
    token: str,
    lobster_capability_id: str = "",
) -> Dict[str, Any]:
    """经 xskill REST tasks API，避免 MCP HTTP 在部分模型上 Decimal 序列化失败（与 lobster-server 一致）。"""
    if not isinstance(arguments, dict):
        arguments = {}
    arguments = _sanitize_for_json(arguments)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
        if tool_name == "generate":
            model = (arguments.get("model") or arguments.get("model_id") or "").strip()
            if not model:
                return {"error": {"message": "generate 缺少 model"}}
            params = {k: v for k, v in arguments.items() if k != "model"}
            body = {"model": model, "params": params, "channel": None}
            r = await client.post(f"{api_base}/api/v3/tasks/create", json=body, headers=headers)
        elif tool_name == "get_result":
            task_id = (
                (arguments.get("task_id") or arguments.get("taskId") or arguments.get("taskid") or "")
                .strip()
            )
            if not task_id:
                return {"error": {"message": "get_result 缺少 task_id（请传 payload.task_id，勿用 taskid）"}}
            body = {"task_id": task_id}
            r = await client.post(f"{api_base}/api/v3/tasks/query", json=body, headers=headers)
        else:
            return {"error": {"message": f"REST 上游未实现工具: {tool_name}"}}

        if r.status_code >= 400:
            err_body = (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX]
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST HTTP=%s\n%s",
                _sutui_rest_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                r.status_code,
                err_body,
            )
            return {"error": {"message": f"上游 REST HTTP {r.status_code}: {(r.text or '')[:800]}"}}
        try:
            payload = r.json()
        except Exception as e:
            raw = (r.text or "")[:_SUTUI_UPSTREAM_LOG_MAX]
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST 非JSON err=%s\n%s",
                _sutui_rest_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                e,
                raw,
            )
            return {"error": {"message": f"上游 REST 非 JSON: {e}"}}
        if not isinstance(payload, dict):
            logger.warning(
                "[速推完整响应] %s | tool=%s | lobster_capability=%s | REST 顶层非对象 body_prefix=%s",
                _sutui_rest_phase_label(tool_name),
                tool_name,
                lobster_capability_id or "(无)",
                (r.text or "")[:800],
            )
            return {"error": {"message": "上游 REST 返回非对象"}}
        code = payload.get("code")
        if code is not None and int(code) != 200:
            msg = payload.get("message") or payload.get("msg") or str(payload)
            _log_sutui_rest_payload(tool_name, lobster_capability_id, _sanitize_for_json(payload))
            return {"error": {"message": f"上游业务错误: {msg}"}}
        data = payload.get("data")
        if not isinstance(data, dict):
            _log_sutui_rest_payload(tool_name, lobster_capability_id, _sanitize_for_json(payload))
            return {"error": {"message": f"上游 REST 无 data 对象: {str(payload)[:500]}"}}
        _log_sutui_rest_payload(tool_name, lobster_capability_id, _sanitize_for_json(payload))
        return _sanitize_for_json(data)


async def _call_upstream_mcp_tool(
    server_url: str,
    tool_name: str,
    arguments: Dict[str, Any],
    upstream_name: str = "",
    sutui_token: Optional[str] = None,
    user_authorization: Optional[str] = None,
    x_installation_id: Optional[str] = None,
    lobster_capability_id: str = "",
) -> Dict[str, Any]:
    auth_headers: Dict[str, str] = {
        "Accept": "application/json, text/event-stream",
    }
    if upstream_name == "sutui":
        if _sutui_upstream_is_server_gateway(server_url):
            auth = (user_authorization or "").strip()
            if not auth:
                return {
                    "error": {
                        "message": "在线版速推经服务器转发：请求须携带登录 JWT（Authorization）。请确认已登录且对话走本机后端。",
                    },
                }
            if not auth.lower().startswith("bearer "):
                auth = f"Bearer {auth}"
            auth_headers["Authorization"] = auth
            auth_headers["x-user-authorization"] = auth
            xi = (x_installation_id or "").strip()
            if xi:
                auth_headers["X-Installation-Id"] = xi
        else:
            token = (sutui_token or "").strip() or _load_sutui_token()
            if token:
                auth_headers["Authorization"] = f"Bearer {token}"
            else:
                return {"error": {"message": "xskill/速推 Token 未配置。单机版请在「系统配置」中填写 Token；在线版请配置 AUTH_SERVER_BASE 以走服务器 mcp-gateway。"}}
            # 直连 xskill MCP URL 时：generate/get_result 改走 REST（与服务器侧一致，避免 MCP -32603 Decimal）
            if tool_name in ("generate", "get_result"):
                api_base = os.environ.get("SUTUI_API_BASE", "https://api.xskill.ai").rstrip("/")
                return await _call_upstream_sutui_tasks_rest(
                    api_base, tool_name, arguments, token, lobster_capability_id
                )

    async with httpx.AsyncClient(timeout=120.0) as client:
        init_body = {
            "jsonrpc": "2.0", "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "lobster-mcp-proxy", "version": "0.1.0"},
            },
        }
        try:
            init_resp = await client.post(server_url, json=init_body, headers=auth_headers)
        except httpx.HTTPError as exc:
            return {
                "error": {
                    "message": (
                        f"无法连接上游 MCP ({server_url}): {exc}。"
                        "说明：在线版本机 MCP(8001)已收到请求，但转发到上述云端地址失败；"
                        "请检查本机到该域名的 HTTPS 连通（网络/VPN/防火墙/DNS），不是「本机未起 MCP」。"
                    )
                }
            }

        if init_resp.status_code == 403:
            return {"error": {"message": "上游 MCP 认证失败 (403)。请检查 Token 是否正确。"}}
        if init_resp.status_code >= 400:
            return {"error": {"message": f"上游 MCP 初始化失败: HTTP {init_resp.status_code}"}}

        session_id = init_resp.headers.get("Mcp-Session-Id") or init_resp.headers.get("mcp-session-id") or ""
        if not session_id:
            try:
                ij = _parse_sse_or_json(init_resp)
                if isinstance(ij, dict):
                    r = ij.get("result") or {}
                    if isinstance(r, dict):
                        session_id = str(r.get("sessionId") or r.get("session_id") or "").strip()
            except Exception:
                pass
        call_body = {
            "jsonrpc": "2.0", "id": "call",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        call_headers = dict(auth_headers)
        if session_id:
            call_headers["Mcp-Session-Id"] = session_id
        try:
            r = await client.post(server_url, json=call_body, headers=call_headers)
        except httpx.HTTPError as exc:
            logger.warning("[MCP] 上游调用失败 tool=%s url=%s: %s", tool_name, server_url, exc)
            return {
                "error": {
                    "message": (
                        f"上游工具调用失败: {exc}。"
                        f"（云端地址 {server_url} 不可达时请查本机 HTTPS 出网/VPN/DNS）"
                    )
                }
            }

        if r.status_code >= 400:
            logger.warning("[MCP] 上游返回 HTTP %s tool=%s: %s", r.status_code, tool_name, r.text[:200])
            return {"error": {"message": f"上游工具调用返回 HTTP {r.status_code}: {r.text[:300]}"}}
        try:
            out = _parse_sse_or_json(r)
            logger.info("[MCP] 上游调用完成 tool=%s status=%s", tool_name, r.status_code)
            return out
        except Exception as e:
            logger.warning("[MCP] 上游响应解析失败 tool=%s: %s", tool_name, e)
            return {"error": {"message": f"上游返回无法解析的响应: status={r.status_code}, body={r.text[:200]}"}}


# 速推 task 状态：先判进行中再判终态（与 backend 一致，避免「未完成」误判）
_TASK_TERMINAL = (
    "success", "completed", "done", "succeeded", "finished",
    "failed", "error", "cancelled", "canceled", "timeout", "expired",
    "已完成", "生成成功", "成功", "完成", "失败", "错误", "取消", "超时",
)
_TASK_IN_PROGRESS = (
    "pending", "queued", "submitted", "processing", "generating", "running",
    "处理中", "生成中", "排队中", "运行中", "上传中", "等待中",
)


def _is_task_still_in_progress(upstream_resp: Any) -> bool:
    """True if upstream get_result 表示任务仍在进行中。先判进行中再判终态（与 backend 一致）。"""
    if not isinstance(upstream_resp, dict):
        return False
    if upstream_resp.get("error"):
        return False
    raw = json.dumps(_sanitize_for_json(upstream_resp), ensure_ascii=False)
    raw_lower = raw.lower()
    for s in _TASK_IN_PROGRESS:
        if s in raw_lower or s in raw or f'"status":"{s}"' in raw_lower:
            return True
    for s in _TASK_TERMINAL:
        if s in raw_lower or s in raw or f'"status":"{s}"' in raw_lower:
            return False
    for content in (upstream_resp.get("content") or (upstream_resp.get("result") or {}).get("content") or []):
        if isinstance(content, dict) and (content.get("type") == "text" or "text" in content):
            t = (content.get("text") or "").lower()
            for s in _TASK_IN_PROGRESS:
                if s in t:
                    return True
            for s in _TASK_TERMINAL:
                if s in t:
                    return False
    return False


async def _record_call(
    token: Optional[str],
    capability_id: str,
    success: bool,
    latency_ms: Optional[int],
    request_payload: Dict,
    response_payload: Optional[Dict],
    error_message: Optional[str],
    credits_charged: Optional[float] = None,
    *,
    pre_deduct_applied: bool = False,
    credits_pre_deducted: Optional[float] = None,
    credits_final: Optional[float] = None,
    request: Optional[Request] = None,
) -> None:
    if not token:
        return
    body = {
        "capability_id": capability_id, "success": success, "latency_ms": latency_ms,
        "request_payload": request_payload, "response_payload": response_payload,
        "error_message": (error_message or "")[:1000] or None, "source": "mcp_invoke",
        "chat_context_id": capability_id,
    }
    if credits_charged is not None:
        body["credits_charged"] = credits_charged
    if pre_deduct_applied:
        body["pre_deduct_applied"] = True
    if credits_pre_deducted is not None:
        body["credits_pre_deducted"] = credits_pre_deducted
    if credits_final is not None:
        body["credits_final"] = credits_final
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            await client.post(
                f"{_capabilities_api_base()}/capabilities/record-call",
                json=_sanitize_for_json(body),
                headers=_backend_headers(token, request),
            )
    except Exception:
        pass


_MEDIA_URL_RE = re.compile(r'https?://[^\s"\'<>\)\]]+\.(?:jpg|jpeg|png|webp|gif|mp4|webm|mov)', re.IGNORECASE)


def _norm_json_key(k: Any) -> str:
    return str(k).replace("_", "").lower()


def _collect_xskill_public_url_fields_first(obj: Any, out: List[str], seen: set) -> None:
    if isinstance(obj, dict):
        for k in ("public_url", "publicUrl"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")) and v not in seen:
                seen.add(v)
                out.append(v.strip())
        for v in obj.values():
            _collect_xskill_public_url_fields_first(v, out, seen)
    elif isinstance(obj, list):
        for x in obj:
            _collect_xskill_public_url_fields_first(x, out, seen)


def _collect_xskill_result_primary_urls(obj: Any, out: List[str], seen: set) -> None:
    if isinstance(obj, dict):
        res = obj.get("result")
        if isinstance(res, dict):
            for k in ("url", "image_url", "video_url", "output_url"):
                v = res.get(k)
                if isinstance(v, str) and v.startswith(("http://", "https://")) and v not in seen:
                    seen.add(v)
                    out.append(v.strip())
        for v in obj.values():
            _collect_xskill_result_primary_urls(v, out, seen)
    elif isinstance(obj, list):
        for x in obj:
            _collect_xskill_result_primary_urls(x, out, seen)


def _reorder_cdn_urls_for_autosave(urls: List[str]) -> List[str]:
    """速推返回里常同时出现 TOS 长期链（…/assets/…）与任务直链（…/v3-tasks/…）。前者可稳定拉取，后者易不可访问；同列表内置后。"""
    assets: List[str] = []
    rest: List[str] = []
    v3tasks: List[str] = []
    seen: set = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        lu = u.lower()
        if "v3-tasks" in lu:
            v3tasks.append(u)
        elif "/assets/" in lu:
            assets.append(u)
        else:
            rest.append(u)
    return assets + rest + v3tasks


def _is_video_task_capability(capability_id: str, payload: Dict[str, Any]) -> bool:
    cid = (capability_id or "").strip()
    if cid.startswith("video"):
        return True
    if cid == "task.get_result" and isinstance(payload, dict):
        pc = (payload.get("capability_id") or "").strip()
        if pc.startswith("video") or ("video" in pc and pc):
            return True
    return False


def _iter_upstream_json_roots(obj: Any) -> List[Dict[str, Any]]:
    """invoke_capability 上游返回体可能为单层或嵌套 result。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(obj, dict):
        return out
    out.append(obj)
    if isinstance(obj.get("result"), dict):
        out.append(obj["result"])
    return out


def _find_sutui_task_output_dict(obj: Any) -> Optional[Dict[str, Any]]:
    """从速推 tasks/query 或 MCP 返回体中解析 data.output（或顶层 output）。"""
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


def _video_url_from_output_dict(output: Dict[str, Any]) -> Optional[str]:
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


def _collect_structured_sutui_auto_save(
    upstream_resp: Any,
    capability_id: str,
    payload: Dict[str, Any],
) -> List[Tuple[str, str]]:
    """
    速推任务完成时 data.output 内常同时含 video / spritesheet / thumbnail。
    若存在 output.video.url，则只保存该 MP4；不把 spritesheet/thumbnail 当作主素材。
    视频任务在已有 output 但尚无 MP4 时：不抢存 spritesheet（避免长条图误入库）。
    图片任务：取 output.images / image_urls 等。
    """
    out: List[Tuple[str, str]] = []
    effective_cap = (capability_id or "").strip()
    if effective_cap == "task.get_result" and isinstance(payload, dict):
        pc = (payload.get("capability_id") or "").strip()
        if pc:
            effective_cap = pc

    is_video_task = _is_video_task_capability(capability_id, payload)

    for root in _iter_upstream_json_roots(upstream_resp):
        op = _find_sutui_task_output_dict(root)
        if not isinstance(op, dict):
            continue
        video_u = _video_url_from_output_dict(op)
        if video_u:
            return [(video_u, "video")]
        if is_video_task:
            return []

        if effective_cap.startswith("image") or ("image" in effective_cap and effective_cap):
            imgs = op.get("images") or op.get("image_urls")
            if isinstance(imgs, list):
                for it in imgs:
                    u = None
                    if isinstance(it, dict):
                        u = it.get("url")
                    elif isinstance(it, str) and it.startswith("http"):
                        u = it
                    if isinstance(u, str) and u.startswith("http"):
                        out.append((u.strip(), "image"))
            sing = op.get("image") or op.get("image_url")
            if isinstance(sing, dict):
                u = sing.get("url")
                if isinstance(u, str) and u.startswith("http"):
                    out.append((u.strip(), "image"))
            elif isinstance(sing, str) and sing.startswith("http"):
                out.append((sing.strip(), "image"))
            if out:
                return out

        sp = op.get("spritesheet")
        if isinstance(sp, dict):
            u = sp.get("url")
            if isinstance(u, str) and u.startswith("http"):
                out.append((u.strip(), "image"))
                return out
    return out


def _filter_video_urls_only_for_fallback(urls: List[str]) -> List[str]:
    """视频任务兜底：只保留明确视频后缀的 URL，排除 spritesheet（.bin/.jpg）等。"""
    out: List[str] = []
    seen: set = set()
    for u in urls:
        low = u.lower().split("?")[0].split("#")[0]
        if low.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi")):
            if u not in seen:
                seen.add(u)
                out.append(u)
    return out


def _extract_media_urls_for_auto_save(upstream_resp: Any) -> List[str]:
    """从上游 JSON 提取媒体 URL：带扩展名正则 + 常见字段递归（无扩展名 CDN 直链）。"""
    order: List[str] = []
    seen: set = set()
    if isinstance(upstream_resp, (dict, list)):
        _collect_xskill_public_url_fields_first(upstream_resp, order, seen)
        _collect_xskill_result_primary_urls(upstream_resp, order, seen)
    blob = (
        json.dumps(_sanitize_for_json(upstream_resp), ensure_ascii=False)
        if isinstance(upstream_resp, (dict, list))
        else str(upstream_resp)
    )
    for m in _MEDIA_URL_RE.findall(blob):
        if m not in seen:
            seen.add(m)
            order.append(m)

    def maybe_add(u: str) -> None:
        u = (u or "").strip()
        if len(u) < 16 or not u.startswith(("http://", "https://")):
            return
        if u not in seen:
            seen.add(u)
            order.append(u)

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                nk = _norm_json_key(k)
                if isinstance(v, str):
                    if nk in (
                        "imageurl", "videourl", "mediaurl", "outputurl", "fileurl", "resulturl",
                        "thumbnailurl", "coverurl", "downloadurl", "previewurl", "publicurl", "persistenturl",
                        "src", "href", "image",
                    ) or nk.endswith("url"):
                        maybe_add(v)
                walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    if isinstance(upstream_resp, (dict, list)):
        walk(upstream_resp)
    return _reorder_cdn_urls_for_autosave(order)[:12]


def _extract_generation_prompt_from_upstream(obj: Any, _depth: int = 0) -> str:
    """从 task.get_result 等上游 JSON 里尽量捞出文生/图生用的 prompt（供 save-url 入库）。"""
    if _depth > 15 or obj is None:
        return ""
    skip = {
        "",
        "task.get_result",
        "video.generate",
        "image.generate",
        "invoke_capability",
        "media.edit",
    }
    if isinstance(obj, dict):
        for k in ("prompt", "user_prompt", "text_prompt", "positive_prompt", "input_prompt"):
            val = obj.get(k)
            if isinstance(val, str):
                s = val.strip()
                if len(s) > 2 and s not in skip and not s.startswith(("http://", "https://")):
                    return s[:2000]
        for v in obj.values():
            out = _extract_generation_prompt_from_upstream(v, _depth + 1)
            if out:
                return out
    elif isinstance(obj, list):
        for it in obj:
            out = _extract_generation_prompt_from_upstream(it, _depth + 1)
            if out:
                return out
    return ""


def _extract_model_id_from_upstream(obj: Any, _depth: int = 0) -> str:
    """从上游 JSON 捞出 model / model_id（速推、Fal 等多用字符串 id）。"""
    if _depth > 15 or obj is None:
        return ""
    skip = {
        "",
        "task.get_result",
        "video.generate",
        "image.generate",
        "invoke_capability",
        "media.edit",
    }
    if isinstance(obj, dict):
        for k in (
            "model",
            "model_id",
            "modelId",
            "model_name",
            "fal_model",
            "video_model",
            "generation_model",
            "engine_id",
        ):
            val = obj.get(k)
            if isinstance(val, str):
                s = val.strip()
                if not s or s in skip:
                    continue
                if s.startswith(("http://", "https://")):
                    continue
                return s[:128]
        for v in obj.values():
            out = _extract_model_id_from_upstream(v, _depth + 1)
            if out:
                return out
    elif isinstance(obj, list):
        for it in obj:
            out = _extract_model_id_from_upstream(it, _depth + 1)
            if out:
                return out
    return ""


def _extract_task_id_from_upstream(obj: Any, _depth: int = 0) -> str:
    """从上游 JSON 取 task_id（task.get_result 自动入库时写入 generation_task_id）。"""
    if _depth > 12 or obj is None:
        return ""
    if isinstance(obj, dict):
        for k in ("task_id", "taskId", "taskid"):
            v = obj.get(k)
            if isinstance(v, str) and len(v.strip()) >= 8:
                return v.strip()[:128]
        for v in obj.values():
            t = _extract_task_id_from_upstream(v, _depth + 1)
            if t:
                return t
    elif isinstance(obj, list):
        for it in obj:
            t = _extract_task_id_from_upstream(it, _depth + 1)
            if t:
                return t
    return ""


# task.get_result：对话层会多轮轮询 MCP，上游每次 completed 正文都带同一成品链。
# 若每轮都 POST save-url，会首次整图下载 + 后续大量「去重命中」仍占 HTTP/锁（app.log 连刷 dedupe_meta）。
_TASK_GET_RESULT_AUTOSAVE_TS: Dict[str, float] = {}
_TASK_GET_RESULT_AUTOSAVE_ASSET_ID: Dict[str, str] = {}
_TASK_GET_RESULT_AUTOSAVE_TTL_SEC = 900.0
_TASK_GET_RESULT_AUTOSAVE_CACHE_MAX = 600


def _task_get_result_autosave_recent_key(token: Optional[str], url: str, gen_tid: str, hint: str) -> str:
    nu = (url or "").strip().split("?")[0].split("#")[0].lower()
    ht = (hint or "").strip().split("?")[0].split("#")[0].lower()
    # 与后端 save-url：v3 请求以 v3 链为去重轴心拼 key
    anchor = ht if ("v3-tasks" in ht) else (nu if ("v3-tasks" in nu) else (ht or nu))
    raw = f"{(token or '')[:80]}|{(gen_tid or '').strip()[:96]}|{anchor}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _task_get_result_autosave_should_skip(key: str) -> bool:
    """仅在近期已成功 auto save-url 时跳过；成功后再写入时间戳（避免首次失败被永久跳过）。"""
    now = time.perf_counter()
    prev = _TASK_GET_RESULT_AUTOSAVE_TS.get(key)
    if prev is not None and (now - prev) < _TASK_GET_RESULT_AUTOSAVE_TTL_SEC:
        logger.info("[MCP auto_save] task.get_result 跳过重复入库（近期已 auto save-url）key=%s…", key[:20])
        return True
    return False


def _task_get_result_autosave_mark_done(key: str, asset_id: str) -> None:
    now = time.perf_counter()
    _TASK_GET_RESULT_AUTOSAVE_TS[key] = now
    aid = (asset_id or "").strip()
    if aid:
        _TASK_GET_RESULT_AUTOSAVE_ASSET_ID[key] = aid
    if len(_TASK_GET_RESULT_AUTOSAVE_TS) > _TASK_GET_RESULT_AUTOSAVE_CACHE_MAX:
        drop_keys = [
            old_k
            for old_k, _t in sorted(_TASK_GET_RESULT_AUTOSAVE_TS.items(), key=lambda kv: kv[1])[
                : _TASK_GET_RESULT_AUTOSAVE_CACHE_MAX // 2
            ]
        ]
        for old_k in drop_keys:
            _TASK_GET_RESULT_AUTOSAVE_TS.pop(old_k, None)
            _TASK_GET_RESULT_AUTOSAVE_ASSET_ID.pop(old_k, None)


async def _auto_save_generated_assets(
    upstream_resp: Any, capability_id: str, payload: Dict, token: Optional[str],
    request: Optional[Request] = None,
) -> List[Dict[str, str]]:
    """Extract media URLs from upstream result and auto-save as local assets."""
    if not token:
        return []

    # payload 无 prompt 时只从上游 JSON 捞取；禁止用 capability_id 回填——否则 sutui.transfer_url、speak、guide
    # 等非生成能力会把「能力 ID 字符串」写进素材 prompt，发布补全文案会变成这串字（见 _effective_publish_copy_from_asset）。
    prompt_text = (payload.get("prompt") or "").strip()
    if not prompt_text:
        prompt_text = _extract_generation_prompt_from_upstream(upstream_resp) or ""

    model_text = (payload.get("model") or "").strip()
    if not model_text:
        model_text = _extract_model_id_from_upstream(upstream_resp) or ""

    gen_tid = (
        (payload.get("task_id") or payload.get("taskId") or payload.get("taskid") or "")
        .strip()
    )
    if not gen_tid and (capability_id or "").strip() == "task.get_result":
        gen_tid = _extract_task_id_from_upstream(upstream_resp) or ""

    def _mt_for_url(u: str) -> str:
        low = u.lower().split("?")[0].split("#")[0]
        if low.endswith((".mp4", ".webm", ".mov", ".m4v", ".avi")):
            return "video"
        if low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
            return "image"
        if low.endswith(".bin"):
            return "image"
        if capability_id.startswith("video") or "video" in capability_id:
            return "video"
        if capability_id == "task.get_result" and payload.get("capability_id"):
            cid = str(payload.get("capability_id") or "")
            if cid.startswith("video"):
                return "video"
        return "image"

    structured = _collect_structured_sutui_auto_save(upstream_resp, capability_id, payload)
    if structured:
        pairs = structured
    elif _is_video_task_capability(capability_id, payload):
        urls = _filter_video_urls_only_for_fallback(_extract_media_urls_for_auto_save(upstream_resp))
        pairs = [(u, "video") for u in urls]
    else:
        urls = _extract_media_urls_for_auto_save(upstream_resp)
        pairs = [(u, _mt_for_url(u)) for u in urls]

    if not pairs:
        return []

    saved: List[Dict[str, str]] = []
    cap_for_dedupe = (capability_id or "").strip()
    transfer_src_for_hint = ""
    if cap_for_dedupe == "sutui.transfer_url" and isinstance(payload, dict):
        transfer_src_for_hint = (payload.get("url") or "").strip()

    for url, mt in pairs[:8]:
        tr_autosave_key = ""
        if cap_for_dedupe == "task.get_result":
            _hint_skip = transfer_src_for_hint
            if not _hint_skip and isinstance(payload, dict):
                _hint_skip = (
                    (payload.get("dedupe_hint_url") or payload.get("hint_url") or payload.get("source_url") or "")
                    .strip()
                )
            tr_autosave_key = _task_get_result_autosave_recent_key(token, url, gen_tid, _hint_skip)
            if _task_get_result_autosave_should_skip(tr_autosave_key):
                _cached_aid = (_TASK_GET_RESULT_AUTOSAVE_ASSET_ID.get(tr_autosave_key) or "").strip()
                if _cached_aid:
                    saved.append({"asset_id": _cached_aid, "filename": "", "media_type": mt})
                continue
        body: Dict[str, Any] = {
            "url": url,
            "media_type": mt,
            "tags": f"auto,{capability_id}",
        }
        pt = (prompt_text or "").strip()
        if pt:
            body["prompt"] = pt[:500]
        if model_text:
            body["model"] = model_text[:128]
        if gen_tid:
            body["generation_task_id"] = gen_tid[:128]
        if transfer_src_for_hint and cap_for_dedupe == "sutui.transfer_url":
            # 入库按源链去重；body.url 为 mcp-images 时每次 UUID 不同，若无 hint 会反复 new_row
            body["dedupe_hint_url"] = transfer_src_for_hint[:2048]
        try:
            # v3-tasks / 转存链偶发 >60s；超时易触发上层重试与并行 auto_save，放大重复入库风险
            async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token, request))
            if r.status_code < 400:
                d = r.json()
                saved.append({"asset_id": d.get("asset_id", ""), "filename": d.get("filename", ""), "media_type": mt})
                if cap_for_dedupe == "task.get_result" and tr_autosave_key:
                    _task_get_result_autosave_mark_done(
                        tr_autosave_key, str(d.get("asset_id") or "")
                    )
            else:
                logger.warning(
                    "[MCP auto_save] save-url HTTP %s url_prefix=%s body_prefix=%s",
                    r.status_code,
                    (url[:96] + "…") if len(url) > 96 else url,
                    (r.text or "")[:240],
                )
        except Exception as e:
            logger.warning("[MCP auto_save] save-url 异常: %s url_prefix=%s", e, (url[:96] + "…") if len(url) > 96 else url)
    return saved


async def _call_tool(name: str, args: Dict[str, Any], token: Optional[str], request: Optional[Request] = None) -> Tuple[List[Dict[str, Any]], bool]:
    try:
        catalog = _load_capability_catalog()
        upstream_urls = _load_upstream_urls()

        if name == "list_capabilities":
            is_admin = await _fetch_is_skill_store_admin(token)
            caps_out = []
            for cid in sorted(catalog.keys()):
                if catalog[cid].get("enabled") is False:
                    continue
                if _capability_id_is_debug_only_in_registry(cid) and not is_admin:
                    continue
                caps_out.append({"capability_id": cid, "description": catalog[cid].get("description") or cid})
            data = {"capabilities": caps_out}
            return [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}], False

        if name == "manage_skills":
            action = str(args.get("action") or "").strip()
            package_id = str(args.get("package_id") or "").strip()
            query = str(args.get("query") or "").strip()
            mcp_name = str(args.get("name") or "").strip()
            mcp_url = str(args.get("url") or "").strip()

            if action == "search_online":
                if not query:
                    return [{"type": "text", "text": "请提供 query 参数，如 'image', 'database', 'github'"}], True
                async with httpx.AsyncClient(timeout=60.0) as client:
                    # Browse a few pages first to populate cache
                    for pg in range(1, 4):
                        await client.get(
                            f"{BASE_URL}/api/mcp-registry/browse",
                            params={"page": str(pg)},
                            headers=_backend_headers(token, request),
                        )
                    # Now search the cache
                    r = await client.get(
                        f"{BASE_URL}/api/mcp-registry/search",
                        params={"q": query, "page_size": "20"},
                        headers=_backend_headers(token, request),
                    )
                data = r.json() if r.content else {}
                servers = data.get("servers", [])
                if not servers:
                    return [{"type": "text", "text": f"未找到与 '{query}' 相关的技能。试试其他关键词：image, video, database, search, github, slack, filesystem"}], False
                lines = [f"找到 {len(servers)} 个与 '{query}' 相关的 MCP 技能：\n"]
                for i, srv in enumerate(servers, 1):
                    lines.append(f"{i}. **{srv.get('title', srv.get('name', ''))}**")
                    if srv.get("description"):
                        lines.append(f"   {srv['description'][:120]}")
                    if srv.get("remote_url"):
                        lines.append(f"   URL: {srv['remote_url']}")
                        lines.append(f"   → 可通过 add_mcp 添加: name=\"{srv.get('name', '').replace('/', '_')}\", url=\"{srv['remote_url']}\"")
                    elif srv.get("install_cmd"):
                        lines.append(f"   安装命令: {srv['install_cmd']}")
                    lines.append("")
                return [{"type": "text", "text": "\n".join(lines)}], False

            if action == "add_mcp":
                if not mcp_name or not mcp_url:
                    return [{"type": "text", "text": "add_mcp 需要 name 和 url 参数"}], True
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.post(
                        f"{BASE_URL}/skills/add-mcp",
                        json={"name": mcp_name, "url": mcp_url},
                        headers=_backend_headers(token, request),
                    )
                return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

            async with httpx.AsyncClient(timeout=30.0) as client:
                if action == "list_store":
                    r = await client.get(f"{BASE_URL}/skills/store", headers=_backend_headers(token, request))
                elif action == "list_installed":
                    r = await client.get(f"{BASE_URL}/skills/installed", headers=_backend_headers(token, request))
                elif action == "install":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/install", json={"package_id": package_id}, headers=_backend_headers(token, request))
                elif action == "uninstall":
                    if not package_id:
                        return [{"type": "text", "text": "请提供 package_id"}], True
                    r = await client.post(f"{BASE_URL}/skills/uninstall", json={"package_id": package_id}, headers=_backend_headers(token, request))
                else:
                    return [{"type": "text", "text": f"未知操作: {action}。支持: list_store, list_installed, install, uninstall, search_online, add_mcp"}], True
            return [{"type": "text", "text": json.dumps(r.json() if r.content else {}, ensure_ascii=False, indent=2)}], r.status_code >= 400

        if name == "invoke_capability":
            args = _normalize_invoke_task_get_result_args(args)
            args = _normalize_invoke_comfly_veo_args(args)
            args = _normalize_invoke_daihuo_pipeline_args(args)
            args = _normalize_invoke_ecommerce_detail_pipeline_args(args)
            capability_id = str(args.get("capability_id") or "").strip()
            payload = args.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if not capability_id or capability_id not in catalog:
                hint = ""
                if capability_id and _looks_like_material_asset_id(capability_id):
                    hint = (
                        f"\n\n提示：「{capability_id}」形似素材库 asset_id。"
                        "发布图文/视频到各平台请使用工具 **publish_content**："
                        "参数 asset_id 填该 ID，account_nickname 填发布账号昵称；"
                        "不要对 invoke_capability 传入素材 ID 作为 capability_id。"
                    )
                return [{"type": "text", "text": f"能力未找到: {capability_id}{hint}"}], True
            if not (token or "").strip():
                return [
                    {
                        "type": "text",
                        "text": (
                            "调用能力需要登录：请在 MCP 请求中携带 Authorization: Bearer（用户 JWT），"
                            "以便预扣积分与结算；匿名请求不会转发上游。"
                        ),
                    }
                ], True
            if _capability_id_is_debug_only_in_registry(capability_id) and not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "该能力为调试中技能，当前账号不可用。"}], True
            cfg = catalog[capability_id]
            upstream_tool = str(cfg.get("upstream_tool") or "").strip()
            if not upstream_tool:
                return [{"type": "text", "text": f"能力配置缺失 upstream_tool: {capability_id}"}], True
            upstream_name = str(cfg.get("upstream") or "sutui").strip()
            upstream_url = upstream_urls.get(upstream_name, "").strip()
            if upstream_name == "sutui":
                if not upstream_url:
                    return [
                        {
                            "type": "text",
                            "text": (
                                "未配置速推上游。请设置 AUTH_SERVER_BASE（推荐：在线版 LOBSTER_EDITION=online 将使用 {AUTH}/mcp-gateway），"
                                "或在 upstream_urls.json 中将 sutui 指向认证中心的 /mcp-gateway。"
                                "本机不提供直连速推 MCP 的计费，积分预扣与结算仅在服务器 MCP 内完成。"
                            ),
                        }
                    ], True
                if not _sutui_upstream_is_server_gateway(upstream_url):
                    return [
                        {
                            "type": "text",
                            "text": (
                                "速推能力仅允许经 URL 含 mcp-gateway 的认证中心入口调用；"
                                "请勿配置直连速推/ xskill MCP 地址。预扣、结算、退款由服务器 lobster MCP 统一处理。"
                            ),
                        }
                    ], True
            if capability_id == "image.generate":
                try:
                    payload = _normalize_image_generate_payload(payload)
                except ValueError as e:
                    return [{"type": "text", "text": f"image.generate 参数错误: {e}"}], True
                if not isinstance(payload, dict):
                    payload = {}
                _igp = (str(payload.get("prompt") or "")).strip()
                if not _igp:
                    return [
                        {
                            "type": "text",
                            "text": (
                                "image.generate 缺少 prompt（上游要求提示词不能为空）。"
                                "请在 payload 中填写 prompt；从龙虾主对话发起时，请把配图需求写在用户消息里，系统会自动回填。"
                            ),
                        }
                    ], True
            elif capability_id == "video.generate":
                try:
                    payload = _normalize_video_generate_payload(payload)
                except ValueError as e:
                    return [{"type": "text", "text": f"video.generate 参数错误: {e}"}], True
                _vm = (payload.get("model") or "").strip() if isinstance(payload, dict) else ""
                _has_img = bool(
                    isinstance(payload, dict)
                    and (
                        (str(payload.get("image_url") or "").strip())
                        or (payload.get("filePaths") and len(payload.get("filePaths") or []) > 0)
                    )
                )
                logger.info(
                    "[MCP video.generate] after_normalize model=%s has_image=%s",
                    _vm or "(empty)",
                    _has_img,
                )
            elif capability_id == "image.understand":
                payload = _normalize_understand_payload(payload, media_key="image_urls", default_model="openrouter/router/vision")
            elif capability_id == "video.understand":
                payload = _normalize_understand_payload(payload, media_key="video_urls", default_model="openrouter/router/video")
            elif capability_id == "task.get_result":
                if not (payload.get("task_id") or "").strip():
                    return [
                        {
                            "type": "text",
                            "text": (
                                "task.get_result 缺少 task_id。"
                                "请使用 invoke_capability(capability_id=\"task.get_result\", payload={\"task_id\":\"速推任务ID\"})；"
                                "JSON 字段名必须是 task_id（下划线），不要用 taskid。"
                                "若用户只说「查进度」而未给 ID，请从本会话上文最近一次 image.generate/video.generate 返回的 JSON 里取出 task_id 再调用。"
                            ),
                        }
                    ], True

            def _pre_deduct_insufficient_user_text(detail: Any) -> str:
                base = "当前账户积分不足，无法调用该能力。请前往「充值」或积分页购买/充值后再试。"
                d = str(detail or "").strip()
                if not d or d in ("积分不足", "余额不足"):
                    return base
                return f"{base}（{d}）"

            # ── 认证中心 pre-deduct：sutui 走网关；media.edit / comfly.* 不在 MCP 侧计费（见 _INVOKE_NO_AUTH_CENTER_BILLING）。
            credits_charged = 0
            if token and upstream_name != "sutui" and capability_id not in _INVOKE_NO_AUTH_CENTER_BILLING:
                try:
                    pre_body: Dict[str, Any] = {"capability_id": capability_id}
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        pre_r = await client.post(
                            f"{_capabilities_api_base()}/capabilities/pre-deduct",
                            json=_sanitize_for_json(pre_body),
                            headers=_backend_headers(token, request),
                        )
                    logger.info(
                        "[MCP invoke_capability] pre_deduct capability_id=%s upstream_tool=%s http_status=%s",
                        capability_id,
                        upstream_tool,
                        pre_r.status_code,
                    )
                    if pre_r.status_code == 400:
                        raw = pre_r.json() if pre_r.content else {}
                        detail = raw.get("detail", "无法预扣积分")
                        if isinstance(detail, list):
                            detail = str(detail)
                        return [{"type": "text", "text": str(detail)}], True
                    if pre_r.status_code == 401:
                        logger.warning(
                            "[MCP invoke_capability] pre_deduct unauthorized capability_id=%s",
                            capability_id,
                        )
                        return [{"type": "text", "text": "无法完成预扣：未登录或登录已过期。请重新登录后再试。"}], True
                    if pre_r.status_code == 402:
                        detail = (pre_r.json() or {}).get("detail", "积分不足")
                        logger.warning(
                            "[MCP invoke_capability] pre_deduct insufficient credits capability_id=%s detail=%s",
                            capability_id,
                            detail,
                        )
                        return [{"type": "text", "text": _pre_deduct_insufficient_user_text(detail)}], True
                    if pre_r.status_code == 403:
                        try:
                            raw403 = pre_r.json() if pre_r.content else {}
                            d403 = raw403.get("detail", pre_r.text or "")
                        except Exception:
                            d403 = (pre_r.text or "")[:500]
                        if isinstance(d403, list):
                            d403 = str(d403)
                        ds = str(d403)
                        logger.warning(
                            "[MCP invoke_capability] pre_deduct forbidden capability_id=%s detail=%s",
                            capability_id,
                            ds[:800],
                        )
                        if (
                            "X-Lobster-Mcp-Billing" in ds
                            or "计费请求来源" in ds
                            or "未受信任" in ds
                            or "拒绝预扣" in ds
                        ):
                            return [{"type": "text", "text": ds}], True
                        return [
                            {"type": "text", "text": f"能力不可用（可能未解锁技能或权限不足）。{ds}"}
                        ], True
                    if pre_r.status_code != 200:
                        try:
                            prev = (pre_r.text or "")[:800]
                        except Exception:
                            prev = ""
                        logger.warning(
                            "[MCP invoke_capability] pre_deduct unexpected status=%s body_prefix=%s",
                            pre_r.status_code,
                            prev,
                        )
                        return [
                            {
                                "type": "text",
                                "text": (
                                    f"预扣积分暂时失败（认证中心返回 HTTP {pre_r.status_code}）。"
                                    "请稍后重试；若持续出现请联系支持。"
                                ),
                            }
                        ], True
                    try:
                        body_ok = pre_r.json() if pre_r.content else {}
                        if not isinstance(body_ok, dict):
                            body_ok = {}
                        if body_ok.get("billing_skipped"):
                            logger.warning(
                                "[MCP invoke_capability] pre_deduct billing_skipped capability_id=%s",
                                capability_id,
                            )
                            return [
                                {
                                    "type": "text",
                                    "text": (
                                        "预扣被跳过（billing_skipped），已中止调用以免未扣费即生成。"
                                        "请在 MCP 运行环境中配置与认证中心一致的 LOBSTER_MCP_BILLING_INTERNAL_KEY。"
                                    ),
                                }
                            ], True
                        _raw_pre = body_ok.get("credits_charged")
                        try:
                            credits_charged = float(_raw_pre) if _raw_pre is not None else 0.0
                        except (TypeError, ValueError):
                            credits_charged = 0.0
                    except Exception as parse_e:
                        logger.warning(
                            "[MCP invoke_capability] pre_deduct 200 响应非 JSON capability_id=%s err=%s body_prefix=%s",
                            capability_id,
                            parse_e,
                            (pre_r.text or "")[:300],
                        )
                        return [
                            {
                                "type": "text",
                                "text": "预扣积分返回异常（无法解析认证中心响应）。请稍后重试。",
                            }
                        ], True
                except Exception as e:
                    logger.exception(
                        "[MCP invoke_capability] pre_deduct request failed capability_id=%s err=%s",
                        capability_id,
                        e,
                    )
                    return [
                        {
                            "type": "text",
                            "text": (
                                "无法连接认证中心完成预扣积分（网络或超时）。"
                                f" 详情：{type(e).__name__}: {str(e)[:200]}"
                            ),
                        }
                    ], True
            elif not token:
                logger.warning(
                    "[MCP invoke_capability] no bearer token, skip pre_deduct capability_id=%s upstream=%s",
                    capability_id,
                    upstream_name,
                )

            # 本机能力（media.edit / comfly.veo / comfly.veo.daihuo_pipeline）：走在线版后端，不走上游 MCP
            if upstream_name == "local":
                _skip_ac_billing = capability_id in _INVOKE_NO_AUTH_CENTER_BILLING
                route = _LOCAL_INVOKE_BACKEND.get(capability_id)
                if not route:
                    supported = ", ".join(sorted(_LOCAL_INVOKE_BACKEND.keys()))
                    return [
                        {
                            "type": "text",
                            "text": _json_dumps_mcp_payload(
                                {
                                    "capability_id": capability_id,
                                    "error": f"未实现的本机能力: {capability_id}（仅支持 {supported}）",
                                }
                            ),
                        }
                    ], True
                run_path, timeout_s = route
                req_method = "POST"
                req_path = run_path
                req_json: Any = {"payload": payload}
                t0 = time.perf_counter()
                _p = payload or {}
                if capability_id == "media.edit":
                    logger.info(
                        "[MCP media.edit] invoke has_token=%s op=%s asset_id=%s base_url=%s",
                        bool(token),
                        _p.get("operation"),
                        _p.get("asset_id"),
                        BASE_URL,
                    )
                elif capability_id == "comfly.veo.daihuo_pipeline":
                    dh_act = (_p.get("action") or "").strip() or "run_pipeline"
                    if dh_act == "start_pipeline":
                        req_path = "/api/comfly-daihuo/pipeline/start"
                        timeout_s = 120.0
                    elif dh_act == "poll_pipeline":
                        jid = (_p.get("job_id") or "").strip().lower()
                        if (
                            not jid
                            or len(jid) != 32
                            or any(c not in "0123456789abcdef" for c in jid)
                        ):
                            return [
                                {
                                    "type": "text",
                                    "text": _json_dumps_mcp_payload(
                                        {
                                            "capability_id": capability_id,
                                            "error": "poll_pipeline 需要有效的 payload.job_id（32 位十六进制）",
                                        }
                                    ),
                                }
                            ], True
                        req_path = f"/api/comfly-daihuo/pipeline/jobs/{jid}"
                        req_method = "GET"
                        req_json = None
                        timeout_s = 120.0
                    else:
                        req_path = "/api/comfly-daihuo/pipeline/run"
                        timeout_s = 7200.0
                    logger.info(
                        "[MCP comfly.veo.daihuo_pipeline] invoke has_token=%s action=%s asset_id=%s job_id=%s base_url=%s",
                        bool(token),
                        dh_act,
                        _p.get("asset_id"),
                        (_p.get("job_id") or "")[:16],
                        BASE_URL,
                    )
                elif capability_id == "comfly.ecommerce.detail_pipeline":
                    ec_act = (_p.get("action") or "").strip() or "run_pipeline"
                    if ec_act == "start_pipeline":
                        req_path = "/api/comfly-ecommerce-detail/pipeline/start"
                        timeout_s = 120.0
                    elif ec_act == "poll_pipeline":
                        jid = (_p.get("job_id") or "").strip().lower()
                        if (
                            not jid
                            or len(jid) != 32
                            or any(c not in "0123456789abcdef" for c in jid)
                        ):
                            return [
                                {
                                    "type": "text",
                                    "text": _json_dumps_mcp_payload(
                                        {
                                            "capability_id": capability_id,
                                            "error": "poll_pipeline 需要有效的 payload.job_id（32 位十六进制）",
                                        }
                                    ),
                                }
                            ], True
                        req_path = f"/api/comfly-ecommerce-detail/pipeline/jobs/{jid}"
                        req_method = "GET"
                        req_json = None
                        timeout_s = 120.0
                    else:
                        req_path = "/api/comfly-ecommerce-detail/pipeline/run"
                        timeout_s = 7200.0
                    logger.info(
                        "[MCP comfly.ecommerce.detail_pipeline] invoke has_token=%s action=%s asset_id=%s job_id=%s base_url=%s",
                        bool(token),
                        ec_act,
                        _p.get("asset_id"),
                        (_p.get("job_id") or "")[:16],
                        BASE_URL,
                    )
                elif capability_id == "ecommerce.publish":
                    ec_action = (_p.get("action") or "").strip() or "open_product_form"
                    if ec_action == "list_shop_accounts":
                        req_path = "/api/ecommerce-publish/accounts"
                        req_method = "GET"
                        timeout_s = 15.0
                    else:
                        req_path = "/api/ecommerce-publish/open-product-form"
                        timeout_s = 120.0
                    logger.info(
                        "[MCP ecommerce.publish] invoke action=%s platform=%s nickname=%s base_url=%s",
                        ec_action,
                        _p.get("platform"),
                        _p.get("account_nickname"),
                        BASE_URL,
                    )
                else:
                    logger.info(
                        "[MCP comfly.veo] invoke has_token=%s action=%s asset_id=%s payload_keys=%s base_url=%s",
                        bool(token),
                        _p.get("action"),
                        _p.get("asset_id"),
                        sorted(_p.keys()),
                        BASE_URL,
                    )
                try:
                    url_full = f"{BASE_URL.rstrip('/')}{req_path}"
                    async with httpx.AsyncClient(timeout=timeout_s) as client:
                        if req_method == "GET":
                            r = await client.get(url_full, headers=_backend_headers(token, request))
                        else:
                            r = await client.post(
                                url_full,
                                json=req_json,
                                headers=_backend_headers(token, request),
                            )
                    log_tag = capability_id
                    if r.status_code >= 400:
                        latency_ms = int((time.perf_counter() - t0) * 1000)
                        err_text = ""
                        try:
                            body_j = r.json()
                            err_text = str(body_j.get("detail") or body_j)
                        except Exception:
                            err_text = (r.text or "")[:2000]
                        logger.warning(
                            "[MCP %s] backend error http_status=%s err=%s",
                            log_tag,
                            r.status_code,
                            (err_text or "")[:1200],
                        )
                        if credits_charged > 0 and token and not _skip_ac_billing:
                            try:
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    await client.post(
                                        f"{_capabilities_api_base()}/capabilities/refund",
                                        json={"capability_id": capability_id, "credits": credits_charged},
                                        headers=_backend_headers(token, request),
                                    )
                            except Exception:
                                pass
                        if not _skip_ac_billing:
                            _lc_pre = float(credits_charged) if credits_charged else 0.0
                            await _record_call(
                                token,
                                capability_id,
                                False,
                                latency_ms,
                                payload,
                                None,
                                err_text,
                                credits_charged=_lc_pre if _lc_pre else None,
                                pre_deduct_applied=_lc_pre > 0,
                                credits_pre_deducted=_lc_pre if _lc_pre > 0 else None,
                                request=request,
                            )
                        return [{"type": "text", "text": _json_dumps_mcp_payload({"capability_id": capability_id, "error": err_text})}], True
                    data = r.json() if r.content else {}
                    if (
                        capability_id == "comfly.veo.daihuo_pipeline"
                        and isinstance(data, dict)
                        and data.get("ok", True)
                    ):
                        dh_act2 = (_p.get("action") or "").strip() or "run_pipeline"
                        if dh_act2 == "start_pipeline":
                            jid2 = (data.get("job_id") or "").strip()
                            if jid2:
                                logger.info(
                                    "[MCP comfly.veo.daihuo_pipeline] start 成功，开始轮询 job_id=%s",
                                    jid2[:16],
                                )
                                polled = await _mcp_poll_daihuo_pipeline_until_done(
                                    base_url=BASE_URL,
                                    token=token,
                                    job_id=jid2,
                                    request=request,
                                )
                                if isinstance(polled, dict) and (polled.get("status") or "").strip():
                                    data = polled
                                else:
                                    data = {
                                        "ok": False,
                                        "job_id": jid2,
                                        "start_ack": data,
                                        "poll_error": polled,
                                    }
                    if (
                        capability_id == "comfly.ecommerce.detail_pipeline"
                        and isinstance(data, dict)
                        and data.get("ok", True)
                    ):
                        ec_act2 = (_p.get("action") or "").strip() or "run_pipeline"
                        if ec_act2 == "start_pipeline":
                            jid2 = (data.get("job_id") or "").strip()
                            if jid2:
                                waited = 0
                                polled: Any = {"ok": False, "job_id": jid2, "status": "timeout"}
                                while waited < 7200:
                                    await asyncio.sleep(5)
                                    waited += 5
                                    try:
                                        async with httpx.AsyncClient(timeout=120.0) as client:
                                            pr = await client.get(
                                                f"{BASE_URL.rstrip('/')}/api/comfly-ecommerce-detail/pipeline/jobs/{jid2}",
                                                headers=_backend_headers(token, request),
                                            )
                                        if pr.status_code >= 400:
                                            continue
                                        polled = pr.json() if pr.content else {}
                                    except Exception:
                                        continue
                                    st = (polled.get("status") or "").strip().lower() if isinstance(polled, dict) else ""
                                    if st in ("completed", "failed"):
                                        break
                                if isinstance(polled, dict) and (polled.get("status") or "").strip():
                                    data = polled
                                else:
                                    data = {
                                        "ok": False,
                                        "job_id": jid2,
                                        "start_ack": data,
                                        "poll_error": polled,
                                    }
                    if capability_id == "comfly.veo" and isinstance(data, dict) and data.get("ok", True):
                        if (data.get("action") or "").strip() == "submit_video":
                            tid_poll = (data.get("task_id") or "").strip()
                            if tid_poll:
                                logger.info(
                                    "[MCP comfly.veo] submit_video 成功，开始阻塞轮询 poll_video task_id=%s",
                                    tid_poll[:96],
                                )
                                data = await _mcp_poll_comfly_veo_after_submit(
                                    base_url=BASE_URL,
                                    token=token,
                                    task_id=tid_poll,
                                    request=request,
                                    initial_submit_data=data,
                                )
                    latency_ms = int((time.perf_counter() - t0) * 1000)
                    logger.info(
                        "[MCP %s] backend_response http_status=%s latency_ms=%s",
                        log_tag,
                        r.status_code,
                        latency_ms,
                    )
                    if not _skip_ac_billing:
                        _lc_pre_ok = float(credits_charged) if credits_charged else 0.0
                        await _record_call(
                            token,
                            capability_id,
                            True,
                            latency_ms,
                            payload,
                            data,
                            None,
                            credits_charged=_lc_pre_ok if _lc_pre_ok else None,
                            pre_deduct_applied=_lc_pre_ok > 0,
                            credits_pre_deducted=_lc_pre_ok if _lc_pre_ok > 0 else None,
                            request=request,
                        )
                    text = _json_dumps_mcp_payload({"capability_id": capability_id, "result": data})
                    return [{"type": "text", "text": text}], False
                except Exception as e:
                    logger.exception("local backend %s failed: %s", capability_id, e)
                    if credits_charged > 0 and token and not _skip_ac_billing:
                        try:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                await client.post(
                                    f"{_capabilities_api_base()}/capabilities/refund",
                                    json={"capability_id": capability_id, "credits": credits_charged},
                                    headers=_backend_headers(token, request),
                                )
                        except Exception:
                            pass
                    if not _skip_ac_billing:
                        _ex_pre = float(credits_charged) if credits_charged else 0.0
                        await _record_call(
                            token,
                            capability_id,
                            False,
                            0,
                            payload,
                            None,
                            str(e)[:1000],
                            credits_charged=_ex_pre if _ex_pre else None,
                            pre_deduct_applied=_ex_pre > 0,
                            credits_pre_deducted=_ex_pre if _ex_pre > 0 else None,
                            request=request,
                        )
                    if capability_id == "media.edit":
                        fail_msg = "本地剪辑调用失败"
                    elif capability_id == "comfly.veo.daihuo_pipeline":
                        fail_msg = "爆款TVC 整包成片后端调用失败"
                    elif capability_id == "comfly.ecommerce.detail_pipeline":
                        fail_msg = "电商详情图流水线后端调用失败"
                    elif capability_id == "ecommerce.publish":
                        fail_msg = "电商商品发布后端调用失败"
                    else:
                        fail_msg = "comfly.veo 后端调用失败"
                    return [{"type": "text", "text": f"{fail_msg}: {e}"}], True

            if not upstream_url:
                return [{"type": "text", "text": f"未配置上游网关: {upstream_name}，请在 .env 或技能商店中配置"}], True
            sutui_token = (request.headers.get("X-Sutui-Token") or "").strip() or None if request else None
            user_auth = None
            xi_for_upstream = ""
            if request:
                user_auth = (request.headers.get("Authorization") or request.headers.get("x-user-authorization") or "").strip() or None
                xi_for_upstream = (
                    request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or ""
                ).strip()
            t0 = time.perf_counter()
            logger.info("[MCP] invoke_capability capability_id=%s upstream=%s", capability_id, upstream_name)
            upstream_resp: Any = {}
            transfer_url_from_cache = False
            if (
                capability_id == "sutui.transfer_url"
                and (token or "").strip()
                and isinstance(payload, dict)
            ):
                _tu_src = (payload.get("url") or "").strip()
                if _tu_src:
                    _tu_ck = _transfer_url_cache_key(token, _tu_src)
                    _tu_hit = _transfer_url_cache_get(_tu_ck)
                    if _tu_hit is not None:
                        upstream_resp = _tu_hit
                        transfer_url_from_cache = True
                        logger.info(
                            "[MCP sutui.transfer_url] cache_hit ttl_s=%s url_prefix=%s",
                            _TRANSFER_URL_CACHE_TTL_SEC,
                            (_tu_src[:96] + "…") if len(_tu_src) > 96 else _tu_src,
                        )
            # 在线版经 /mcp-gateway 连的是**服务器 lobster MCP**，只注册 invoke_capability，无裸工具名 generate/get_result 等
            if not transfer_url_from_cache:
                if upstream_name == "sutui" and _sutui_upstream_is_server_gateway(upstream_url):
                    upstream_raw = await _call_upstream_mcp_tool(
                        upstream_url,
                        "invoke_capability",
                        {"capability_id": capability_id, "payload": payload},
                        upstream_name=upstream_name,
                        sutui_token=sutui_token,
                        user_authorization=user_auth,
                        x_installation_id=xi_for_upstream or None,
                        lobster_capability_id=capability_id,
                    )
                    upstream_resp = _unwrap_lobster_server_gateway_response(
                        upstream_raw if isinstance(upstream_raw, dict) else {}
                    )
                else:
                    upstream_resp = await _call_upstream_mcp_tool(
                        upstream_url,
                        upstream_tool,
                        payload,
                        upstream_name=upstream_name,
                        sutui_token=sutui_token,
                        user_authorization=user_auth,
                        x_installation_id=xi_for_upstream or None,
                        lobster_capability_id=capability_id,
                    )
            # task.get_result: 不再在此处轮询，由 backend chat 每 15s 轮询并写回对话
            latency_ms = int((time.perf_counter() - t0) * 1000)
            upstream_error = ""
            if isinstance(upstream_resp, dict):
                err_obj = upstream_resp.get("error")
                if isinstance(err_obj, dict):
                    upstream_error = str(err_obj.get("message") or "")[:500]
            if upstream_error and credits_charged > 0 and token and upstream_name != "sutui":
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            f"{_capabilities_api_base()}/capabilities/refund",
                            json={"capability_id": capability_id, "credits": credits_charged},
                            headers=_backend_headers(token, request),
                        )
                except Exception:
                    pass
            if (
                not upstream_error
                and capability_id == "sutui.transfer_url"
                and (token or "").strip()
                and not transfer_url_from_cache
                and isinstance(payload, dict)
                and isinstance(upstream_resp, dict)
            ):
                _tu_src2 = (payload.get("url") or "").strip()
                if _tu_src2:
                    _transfer_url_cache_set(_transfer_url_cache_key(token, _tu_src2), upstream_resp)
            if upstream_name == "sutui":
                logger.info(
                    "[MCP] 速推经 mcp-gateway：预扣/结算/退款由服务器 MCP 完成，本机不调认证中心计费接口 capability_id=%s",
                    capability_id,
                )

            logger.info("[MCP] invoke_capability 完成 capability_id=%s latency_ms=%s ok=%s", capability_id, latency_ms, not bool(upstream_error))
            data: Dict[str, Any] = {"capability_id": capability_id, "result": _redact_sensitive(upstream_resp)}

            if not upstream_error:
                saved = await _auto_save_generated_assets(upstream_resp, capability_id, payload, token, request)
                if saved:
                    data["saved_assets"] = saved

            text = _json_dumps_mcp_payload(data)
            return [{"type": "text", "text": text}], bool(upstream_error)

        if name == "save_asset":
            url = str(args.get("url") or "").strip()
            if not url:
                return [{"type": "text", "text": "请提供素材 URL"}], True
            body = {
                "url": url,
                "media_type": args.get("media_type", "image"),
                "tags": args.get("tags", ""),
                "prompt": args.get("prompt", ""),
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(f"{BASE_URL}/api/assets/save-url", json=body, headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "list_assets":
            params_qs: Dict[str, str] = {}
            if args.get("media_type"):
                params_qs["media_type"] = args["media_type"]
            if args.get("query"):
                params_qs["q"] = args["query"]
            params_qs["limit"] = str(args.get("limit", 20))
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/assets", params=params_qs, headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "list_publish_accounts":
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_creator_publish_data":
            params: Dict[str, Any] = {}
            scope = (args.get("scope") or "all")
            if isinstance(scope, str):
                scope = scope.strip().lower()
            if scope in ("all", "platform", "account"):
                params["scope"] = scope
            else:
                params["scope"] = "all"
            plat = str(args.get("platform") or "").strip()
            if plat:
                params["platform"] = plat
            aid = args.get("account_id")
            if aid is not None and aid != "":
                try:
                    params["account_id"] = int(aid)
                except (TypeError, ValueError):
                    pass
            nick = str(args.get("account_nickname") or "").strip()
            if nick:
                params["account_nickname"] = nick
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.get(
                    f"{BASE_URL}/api/creator-content/publish-data",
                    params=params,
                    headers=_backend_headers(token, request),
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "sync_creator_publish_data":
            sync_all = args.get("sync_all", True)
            if isinstance(sync_all, str):
                sync_all = sync_all.lower() in ("true", "1", "yes")
            else:
                sync_all = bool(sync_all)
            body: Dict[str, Any] = {}
            hl = args.get("headless")
            if hl is not None:
                body["headless"] = bool(hl)
            plat = str(args.get("platform") or "").strip()
            if plat:
                body["platform"] = plat
            aid = args.get("account_id")
            nick = str(args.get("account_nickname") or "").strip()
            if aid is not None and str(aid).strip() != "":
                try:
                    body["account_ids"] = [int(aid)]
                except (TypeError, ValueError):
                    return [{"type": "text", "text": "account_id 无效"}], True
            elif nick:
                acct_id, nick_err = await _find_account_id_by_nickname(nick, token, request)
                if nick_err:
                    return [{"type": "text", "text": nick_err}], True
                if not acct_id:
                    return [{"type": "text", "text": f"找不到昵称为「{nick}」的发布账号，请先 list_publish_accounts"}], True
                body["account_ids"] = [acct_id]
            elif sync_all:
                pass
            else:
                return [{"type": "text", "text": "请设 sync_all=true（同步全部或按 platform），或指定 account_id / account_nickname"}], True
            logger.info("[MCP] sync_creator_publish_data body=%s", body)
            async with httpx.AsyncClient(timeout=45 * 60.0) as client:
                r = await client.post(
                    f"{BASE_URL}/api/creator-content/sync-all",
                    json=body,
                    headers=_backend_headers(token, request),
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "list_youtube_accounts":
            if not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "YouTube 上传为调试中能力，当前账号不可用。"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/youtube-publish/accounts", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_youtube_video":
            if not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "YouTube 上传为调试中能力，当前账号不可用。"}], True
            asset_id = str(args.get("asset_id") or "").strip()
            yid = str(args.get("youtube_account_id") or "").strip()
            if not asset_id:
                return [{"type": "text", "text": "请提供 asset_id"}], True
            if not yid:
                return [{"type": "text", "text": "请提供 youtube_account_id（先调用 list_youtube_accounts）"}], True
            body = {
                "account_id": yid,
                "asset_id": asset_id,
                "title": str(args.get("title") or "").strip(),
                "description": str(args.get("description") or "").strip(),
                "privacy_status": str(args.get("privacy_status") or "private").strip() or "private",
            }
            if body["privacy_status"] not in ("private", "unlisted", "public"):
                body["privacy_status"] = "private"
            for k in ("category_id", "tags", "material_origin"):
                v = args.get(k)
                if v is not None:
                    body[k] = v
            logger.info("[MCP] publish_youtube_video asset_id=%s account_id=%s", asset_id, yid)
            async with httpx.AsyncClient(timeout=600.0) as client:
                r = await client.post(
                    f"{BASE_URL}/api/youtube-publish/upload",
                    json=body,
                    headers=_backend_headers(token, request),
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_youtube_analytics":
            if not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "YouTube 数据为调试中能力，当前账号不可用。"}], True
            yid = str(args.get("youtube_account_id") or "").strip()
            if not yid:
                return [{"type": "text", "text": "请提供 youtube_account_id（先调用 list_youtube_accounts）"}], True
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.get(
                    f"{BASE_URL}/api/youtube-publish/accounts/{yid}/analytics",
                    headers=_backend_headers(token, request),
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "sync_youtube_analytics":
            if not await _fetch_is_skill_store_admin(token):
                return [{"type": "text", "text": "YouTube 数据为调试中能力，当前账号不可用。"}], True
            yid = str(args.get("youtube_account_id") or "").strip()
            if not yid:
                return [{"type": "text", "text": "请提供 youtube_account_id（先调用 list_youtube_accounts）"}], True
            logger.info("[MCP] sync_youtube_analytics account_id=%s", yid)
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{BASE_URL}/api/youtube-publish/accounts/{yid}/sync-analytics",
                    headers=_backend_headers(token, request),
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        # ── Meta Social（Instagram / Facebook）工具 ──

        _meta_server_base = (os.environ.get("AUTH_SERVER_BASE") or "").strip().rstrip("/")

        if name == "list_meta_social_accounts":
            if not _meta_server_base:
                return [{"type": "text", "text": "未配置 AUTH_SERVER_BASE，无法连接 Meta Social 服务。"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{_meta_server_base}/api/meta-social/accounts", headers=_backend_headers(token, request))
            data = r.json() if r.content else []
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_meta_social":
            if not _meta_server_base:
                return [{"type": "text", "text": "未配置 AUTH_SERVER_BASE，无法连接 Meta Social 服务。"}], True
            body = {
                "account_id": args.get("account_id"),
                "platform": args.get("platform", "instagram"),
                "content_type": args.get("content_type", "photo"),
            }
            for k in ("asset_id", "image_url", "video_url", "caption", "message", "link", "title", "tags", "carousel_items"):
                v = args.get(k)
                if v is not None:
                    body[k] = v
            logger.info("[MCP] publish_meta_social body=%s", {k: v for k, v in body.items() if k != "caption"})
            async with httpx.AsyncClient(timeout=600.0) as client:
                r = await client.post(
                    f"{_meta_server_base}/api/meta-social/publish",
                    headers=_backend_headers(token, request),
                    json=body,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_meta_social_data":
            if not _meta_server_base:
                return [{"type": "text", "text": "未配置 AUTH_SERVER_BASE，无法连接 Meta Social 服务。"}], True
            params: Dict[str, Any] = {}
            if args.get("account_id"):
                params["account_id"] = args["account_id"]
            if args.get("platform"):
                params["platform"] = args["platform"]
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    f"{_meta_server_base}/api/meta-social/data",
                    headers=_backend_headers(token, request),
                    params=params,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "sync_meta_social_data":
            if not _meta_server_base:
                return [{"type": "text", "text": "未配置 AUTH_SERVER_BASE，无法连接 Meta Social 服务。"}], True
            params_sync: Dict[str, Any] = {}
            if args.get("account_id"):
                params_sync["account_id"] = args["account_id"]
            logger.info("[MCP] sync_meta_social_data params=%s", params_sync)
            async with httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{_meta_server_base}/api/meta-social/sync",
                    headers=_backend_headers(token, request),
                    params=params_sync,
                )
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "get_social_report":
            if not _meta_server_base:
                return [{"type": "text", "text": "未配置 AUTH_SERVER_BASE，无法连接 Meta Social 服务。"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    f"{_meta_server_base}/api/meta-social/data",
                    headers=_backend_headers(token, request),
                )
            if r.status_code >= 400:
                data = r.json() if r.content else {}
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return [{"type": "text", "text": text}], True
            all_data = r.json() if r.content else {}
            entries = all_data.get("data", [])
            if not entries:
                return [{"type": "text", "text": json.dumps({"hint": "暂无已连接的 IG/FB 账号数据。请先连接账号并调用 sync_meta_social_data 同步数据。"}, ensure_ascii=False)}], False
            report: Dict[str, Any] = {"platforms": {}, "summary": {}}
            total_posts = total_likes = total_comments = 0
            for entry in entries:
                acct = entry.get("account", {})
                plat = acct.get("platform", "unknown")
                label = acct.get("label") or acct.get("username") or acct.get("page_name") or ""
                posts = entry.get("posts", [])
                metrics = entry.get("account_metrics", {})
                plat_likes = sum(p.get("like_count", 0) or p.get("likes", 0) for p in posts)
                plat_comments = sum(p.get("comments_count", 0) or p.get("comments", 0) for p in posts)
                report["platforms"].setdefault(plat, []).append({
                    "label": label,
                    "post_count": len(posts),
                    "likes": plat_likes,
                    "comments": plat_comments,
                    "account_metrics": metrics,
                })
                total_posts += len(posts)
                total_likes += plat_likes
                total_comments += plat_comments
            report["summary"] = {"total_posts": total_posts, "total_likes": total_likes, "total_comments": total_comments}
            text = json.dumps(report, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], False

        if name == "open_account_browser":
            nickname = str(args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id, nick_err = await _find_account_id_by_nickname(nickname, token, request)
            if nick_err:
                return [{"type": "text", "text": nick_err}], True
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号，请先在「发布管理」中添加"}], True
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(f"{BASE_URL}/api/accounts/{acct_id}/open-browser", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "check_account_login":
            nickname = str(args.get("account_nickname") or "").strip()
            if not nickname:
                return [{"type": "text", "text": "请提供 account_nickname"}], True
            acct_id, nick_err = await _find_account_id_by_nickname(nickname, token, request)
            if nick_err:
                return [{"type": "text", "text": nick_err}], True
            if not acct_id:
                return [{"type": "text", "text": f"找不到昵称为「{nickname}」的账号"}], True
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(f"{BASE_URL}/api/accounts/{acct_id}/login-status", headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], r.status_code >= 400

        if name == "publish_content":
            asset_id = str(args.get("asset_id") or "").strip()
            account_nickname = str(args.get("account_nickname") or "").strip()
            opts_raw = args.get("options") if isinstance(args.get("options"), dict) else {}
            opts_effective: Dict[str, Any] = dict(opts_raw)
            allow_no_asset = _mcp_opts_toutiao_graphic_no_cover(opts_effective)
            # 兼容旧调用：模型曾把用户说的「6」填在 account_id；统一当昵称提交，不再传后端 account_id
            legacy_aid = args.get("account_id")
            if not account_nickname and legacy_aid is not None and str(legacy_aid).strip() != "":
                account_nickname = str(legacy_aid).strip()
                logger.info(
                    "[MCP] publish_content 将遗留字段 account_id=%r 视为昵称 account_nickname",
                    legacy_aid,
                )
            cover_aid_mcp = str(args.get("cover_asset_id") or "").strip()
            if not asset_id and not allow_no_asset and account_nickname and not cover_aid_mcp:
                plat, plat_err = await _find_account_platform_by_nickname(
                    account_nickname, token, request
                )
                if plat_err and plat is None:
                    return [{"type": "text", "text": plat_err}], True
                if (plat or "").strip().lower() == "toutiao":
                    # 无主素材、无独立封面时，头条只能走无封面纯文；同一对话里上轮图文常遗留 false，
                    # 若仍要求 asset_id 会误拦纯文字发稿。
                    if opts_effective.get("toutiao_graphic_no_cover") is False:
                        logger.info(
                            "[MCP] publish_content 头条无 asset_id：覆盖上轮残留的 toutiao_graphic_no_cover=false"
                        )
                    opts_effective["toutiao_graphic_no_cover"] = True
                    allow_no_asset = True
                    logger.info(
                        "[MCP] publish_content 头条无 asset_id：已确保 toutiao_graphic_no_cover=true"
                    )
            if not asset_id and not allow_no_asset:
                return [
                    {
                        "type": "text",
                        "text": (
                            "请提供 asset_id（save_asset / 任务成片入库后的素材ID）。"
                            "若为今日头条无封面纯文字，请省略 asset_id 并在 options 中设置 "
                            "toutiao_graphic_no_cover: true。"
                        ),
                    }
                ], True
            if not account_nickname:
                return [
                    {
                        "type": "text",
                        "text": "请提供 account_nickname：用户在发布管理里设置的账号昵称（与 list_publish_accounts 的昵称一致，如 6、2号），不要使用数据库主键 id。",
                    }
                ], True
            logger.info(
                "[MCP] publish_content 调用: asset_id=%s account_nickname=%s",
                asset_id,
                account_nickname,
            )
            body: Dict[str, Any] = {
                "asset_id": asset_id or None,
                "account_nickname": account_nickname,
                "title": args.get("title", ""),
                "description": args.get("description", ""),
                "tags": args.get("tags", ""),
                "cover_asset_id": args.get("cover_asset_id"),
                "options": opts_effective,
            }
            if "ai_publish_copy" in args and args.get("ai_publish_copy") is not None:
                body["ai_publish_copy"] = bool(args.get("ai_publish_copy"))
            async with httpx.AsyncClient(timeout=180.0) as client:
                r = await client.post(f"{BASE_URL}/api/publish", json=body, headers=_backend_headers(token, request))
            data = r.json() if r.content else {}
            if not isinstance(data, dict):
                data = {}
            http_err = r.status_code >= 400
            body_st = str(data.get("status") or "").strip().lower()
            # /api/publish 在浏览器自动化失败时仍常返回 HTTP 200，仅 JSON.status=failed|need_login；
            # 若只按 HTTP 码判 isError，对话层会记 success=True，模型易谎称「已发布」。
            biz_err = "status" in data and body_st != "success"
            is_error = http_err or biz_err
            logger.info(
                "[MCP] publish_content 后端响应: http=%s body_status=%s is_error=%s task_id=%s",
                r.status_code,
                data.get("status"),
                is_error,
                data.get("task_id"),
            )
            if http_err:
                _detail = data.get("detail")
                logger.warning(
                    "[MCP] publish_content 后端拒绝 HTTP=%s detail=%r asset_id=%s account_nickname_repr=%s",
                    r.status_code,
                    _detail,
                    asset_id,
                    repr(account_nickname[:120]),
                )
            elif biz_err:
                _err_s = str(data.get("error") or "")
                if len(_err_s) > 800:
                    _err_s = _err_s[:800] + "…"
                logger.warning(
                    "[MCP] publish_content 发布未成功(HTTP 200 业务失败) status=%r error=%r need_login=%s",
                    data.get("status"),
                    _err_s,
                    data.get("need_login"),
                )
            text = json.dumps(data, ensure_ascii=False, indent=2)
            return [{"type": "text", "text": text}], is_error

        return [{"type": "text", "text": f"Unknown tool: {name}"}], True
    except Exception as e:
        return [{"type": "text", "text": f"调用出错: {e}"}], True


def _make_error(id_value: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_value, "error": {"code": code, "message": message}}


async def _handle_single_message(msg: Dict[str, Any], request: Request) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return _make_error(None, -32600, "Invalid message")
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "lobster-mcp", "version": "0.1.0"},
            "instructions": "龙虾 AI 助手能力网关：图片生成、视频解析、语音合成、技能管理。",
        }}
    if method == "tools/list":
        catalog = _load_capability_catalog()
        token = _get_token_from_request(request)
        is_admin = await _fetch_is_skill_store_admin(token)
        tools = _tool_definitions(catalog, is_skill_store_admin=is_admin)
        logger.info("[MCP] tools/list -> %s tools", len(tools))
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        cap_id = str(arguments.get("capability_id") or "").strip() if name == "invoke_capability" else ""
        token = _get_token_from_request(request)
        logger.info("[MCP] tools/call name=%s capability_id=%s", name, cap_id or "-")
        content, is_error = await _call_tool(name, arguments, token, request=request)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": is_error}}
    return _make_error(msg_id, -32601, f"Method not found: {method}")


async def mcp_endpoint(request: Request) -> Response:
    if request.method == "GET":
        return PlainTextResponse("SSE not implemented", status_code=405)
    if request.method != "POST":
        return PlainTextResponse("Method not allowed", status_code=405)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    responses: List[Dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            resp = await _handle_single_message(item, request)
            if resp is not None:
                responses.append(resp)
    elif isinstance(payload, dict):
        resp = await _handle_single_message(payload, request)
        if resp is not None:
            responses.append(resp)
    else:
        return JSONResponse({"error": "Invalid payload"}, status_code=400)
    if not responses:
        return Response(status_code=202)
    if len(responses) == 1:
        return JSONResponse(responses[0])
    return JSONResponse(responses)


app = Starlette(
    routes=[Route("/mcp", mcp_endpoint, methods=["GET", "POST"])],
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["*"])],
)

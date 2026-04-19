"""爆款TVC / Comfly Veo：upload 本机解析图 URL；其余步骤走云端 Comfly proxy + 龙虾积分计费。

Phase 2 改造（2026-04 起）：
- 凭据从用户本机 UserComflyConfig 改为云端 Comfly proxy + 用户 JWT
- 所有 Comfly 调用透传到 lobster-server /api/comfly-proxy/* （服务端用 server token 转发）
- 计费按 comfly_pricing.json 扣龙虾积分（参见 lobster-server/backend/app/api/comfly_proxy.py）
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.config import settings
from ..api.assets import _is_internal_asset_http_url, get_asset_public_url
from ..models import Asset, UserComflyConfig  # UserComflyConfig 仍被高级用户配置 UI 使用，但 Comfly 调用不再读它

logger = logging.getLogger(__name__)

_DEFAULT_VIDEO_MODEL = "veo3.1"
_DEFAULT_ANALYSIS_MODEL = "gemini-2.5-pro"
_DEFAULT_ASPECT = "9:16"
LOCAL_COMFLY_CONFIG_USER_ID = 0


def _default_comfly_api_base() -> str:
    return ((settings.comfly_api_base or "").strip().rstrip("/")) or "https://ai.comfly.chat/v1"


def _comfly_upload_failure_detail(aid: str, user_id: int, db: Session) -> str:
    """说明 comfly.daihuo upload 无法得到公网图链时的常见原因（便于用户自查 asset_id）。"""
    aid_l = (aid or "").strip().lower()
    row_mine = (
        db.query(Asset)
        .filter(func.lower(Asset.asset_id) == aid_l, Asset.user_id == user_id)
        .first()
    )
    if row_mine is None:
        row_any = db.query(Asset).filter(func.lower(Asset.asset_id) == aid_l).first()
        if row_any is None:
            return (
                f"素材库中不存在 asset_id「{aid}」。请到素材库核对 ID，或使用 list_assets；"
                "勿将上游 task_id、video_id、或 v3-tasks 成品文件名（如 xxx.mp4）误当作素材 ID。"
            )
        return (
            f"asset_id「{aid}」在库中存在，但不属于当前登录账号。请换用本账号素材库中的 ID。"
        )
    url = (row_mine.source_url or "").strip()
    if not url:
        return (
            f"素材「{row_mine.asset_id}」已入库，但尚无公网 source_url（可能仅本机文件、未走 TOS/转存）。"
            "请配置 TOS 或使用生成/转存成功后带 CDN 的素材；也可在对话中对图使用 sutui.transfer_url 等转公网后再入库。"
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"素材「{row_mine.asset_id}」的 source_url 非 http(s)，无法给 Comfly 拉取。"
    if _is_internal_asset_http_url(url):
        return (
            f"素材「{row_mine.asset_id}」当前为内网或临时签名链，Comfly 云端无法访问。"
            "请换用 CDN 公网直链（无 token=），或先转存到对象存储。"
        )
    return f"素材「{row_mine.asset_id}」暂无法解析为可用公网图链，请稍后重试或联系管理员查看日志。"


def _reject_if_sutui_style_model(field: str, value: str) -> None:
    """Comfly 能力与速推 video.generate 的 model 字符串不可混用；误填时直接 400。"""
    s = (value or "").strip()
    if not s or "/" not in s:
        return
    if s.startswith(("fal-ai/", "st-ai/", "wan/")):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field}「{s}」为速推侧 model id 形态；comfly.daihuo 须使用 Comfly 文档中的模型名"
                f"（视频默认 {_DEFAULT_VIDEO_MODEL}、分析默认 {_DEFAULT_ANALYSIS_MODEL}），"
                "勿与 invoke_capability(capability_id=\"video.generate\") 的 payload.model 混用。"
            ),
        )


def _resolve_comfly_credentials(
    user_id: int, db: Session, request: Optional[Request] = None
) -> tuple[str, str]:
    """爆款TVC / Comfly 系列能力的统一凭据解析（Phase 2 改造后强制走云端 proxy）。

    返回：
    - api_base = `<auth_server_base>/api/comfly-proxy`（pipeline 脚本会拼 /v1/chat/completions 等）
    - api_key  = 用户 JWT（lobster-server proxy 端用此 JWT 鉴权 + 按 comfly_pricing 扣龙虾积分）

    UserComflyConfig 表保留但不再被 Comfly 调用读取——意味着「按用户 Comfly 余额扣费」彻底
    改为「按龙虾积分扣费」。如需保留某用户走自己 Comfly Key（试点/调试）的能力，可在此处
    特判 user_id ∈ 白名单 + 读 UserComflyConfig 走旧逻辑（默认关闭）。

    request 可选：传入时从 Authorization 头拿 JWT；不传时回退到 LOBSTER_COMFLY_PROXY_DEFAULT_TOKEN
    （仅开发场景）。生产/正常对话调用必须传 request。
    """
    proxy_root = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not proxy_root:
        raise HTTPException(
            status_code=503,
            detail="未配置 AUTH_SERVER_BASE，无法走云端 Comfly proxy。请在 .env 设置 AUTH_SERVER_BASE。",
        )
    proxy_base = f"{proxy_root}/api/comfly-proxy"

    jwt = ""
    if request is not None:
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            jwt = auth[7:].strip()
    if not jwt:
        jwt = (os.environ.get("LOBSTER_COMFLY_PROXY_DEFAULT_TOKEN") or "").strip()
    if not jwt:
        raise HTTPException(
            status_code=401,
            detail="无法获取用户 JWT。请重新登录后再调用爆款TVC / Comfly 能力。",
        )
    return proxy_base, jwt


def _join_url(api_base: str, path: str) -> str:
    base = api_base.rstrip("/") + "/"
    p = path.lstrip("/")
    return urljoin(base, p)


def _comfly_request_url(api_base: str, path: str) -> str:
    """path 以 / 开头时替换为与 api_base 同主机下的绝对路径（/v2/... 不与 /v1 base 错误拼接）。"""
    p = (path or "").strip()
    if p.startswith("/"):
        u = urlparse(api_base.rstrip("/"))
        return urlunparse((u.scheme, u.netloc, p, "", "", ""))
    return _join_url(api_base, p)


def _comfly_key_plain(api_key: str) -> str:
    k = (api_key or "").strip()
    if k.lower().startswith("bearer "):
        return k[7:].strip()
    return k


def _headers(api_key: str) -> Dict[str, str]:
    """与 Comfly 文档一致：Authorization: Bearer {{YOUR_API_KEY}}；另附 JSON 请求的 Content-Type / Accept。"""
    k = _comfly_key_plain(api_key)
    return {
        "Authorization": f"Bearer {k}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _headers_for_log(h: Dict[str, str]) -> Dict[str, Any]:
    """日志脱敏：Authorization 仅长度 + 末尾 4 字符。"""
    out: Dict[str, Any] = {}
    for k, v in (h or {}).items():
        lk = (k or "").lower()
        if lk == "authorization":
            vv = (v or "").strip()
            if vv.lower().startswith("bearer "):
                tok = vv[7:].strip()
                tail = tok[-4:] if len(tok) >= 4 else tok
                out[k] = f"Bearer ***len={len(tok)} tail={tail}"
            else:
                out[k] = f"***len={len(vv)} (no Bearer prefix)"
        else:
            out[k] = v
    return out


def _json_trunc(obj: Any, max_len: int = 2400) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except TypeError:
        s = str(obj)
    if len(s) > max_len:
        return s[:max_len] + f"...(trunc len={len(s)})"
    return s


def _log_comfly_http_out(
    phase: str,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Any = None,
    note: str = "",
) -> None:
    suf = f" note={note}" if note else ""
    logger.info(
        "[comfly.http] OUT phase=%s %s %s headers=%s json_body=%s%s",
        phase,
        method,
        url,
        _json_trunc(_headers_for_log(headers)),
        _json_trunc(body) if body is not None else "(none)",
        suf,
    )


def _log_comfly_http_in(phase: str, response: httpx.Response, note: str = "") -> None:
    suf = f" note={note}" if note else ""
    txt = (response.text or "")[:2000]
    if len(response.text or "") > 2000:
        txt += "...(trunc)"
    try:
        rid = response.headers.get("x-request-id") or response.headers.get("X-Request-Id") or ""
    except Exception:
        rid = ""
    rid_s = f" req_id={rid}" if rid else ""
    logger.info(
        "[comfly.http] IN phase=%s status=%s url=%s%s body_preview=%s%s",
        phase,
        response.status_code,
        str(response.request.url) if response.request else "",
        rid_s,
        txt,
        suf,
    )


def _comfly_chat_tools_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 payload.tools：已是 OpenAI 形态则原样收录；字符串则按 Gemini 预设名包装为 type=function。"""
    raw = payload.get("tools")
    if not isinstance(raw, list) or not raw:
        return []
    out: List[Dict[str, Any]] = []
    for x in raw:
        if isinstance(x, dict):
            out.append(x)
            continue
        if isinstance(x, str) and x.strip():
            name = x.strip()
            out.append({"type": "function", "function": {"name": name}})
    return out


def _prompt_for_veo_submit(payload: Dict[str, Any]) -> str:
    """Comfly /v2/videos/generations 需要单个 prompt；兼容模型只传 prompts 数组（取首条非空）。"""
    p = (payload.get("prompt") or "").strip()
    if p:
        return p
    raw = payload.get("prompts")
    if isinstance(raw, list):
        for x in raw:
            s = str(x).strip()
            if s:
                return s
    return ""


def _veo_clamp_images_for_model(model: str, images: List[str]) -> List[str]:
    """Comfly POST /v2/videos/generations：不同 model 对 images 条数上限不同。"""
    clean = [u.strip() for u in (images or []) if isinstance(u, str) and u.strip()]
    m = (model or "").strip().lower()
    if m == "veo3-pro-frames":
        return clean[:1]
    if m in ("veo3-fast-frames", "veo2-fast-frames"):
        return clean[:2]
    if m in ("veo2-fast-components", "veo3.1-components"):
        return clean[:3]
    if m in ("veo3.1", "veo3.1-pro"):
        return clean[:2]
    return clean[:3]


def _images_for_veo_submit(payload: Dict[str, Any], image_url_fallback: str) -> List[str]:
    """官方 Body 为 images: url 或 base64 字符串数组。"""
    raw = payload.get("images")
    if isinstance(raw, list):
        out = [str(x).strip() for x in raw if str(x).strip()]
        if out:
            return out[:16]
    u = (payload.get("image_url") or "").strip() or image_url_fallback
    return [u] if u else []


def _parse_prompts_from_content(content: str) -> List[str]:
    text = (content or "").strip()
    if "```" in text:
        text = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("prompts"), list):
            return [str(x).strip() for x in obj["prompts"] if str(x).strip()][:8]
    except json.JSONDecodeError:
        pass
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return lines[:8]


def _normalize_comfly_veo_payload(pl: Dict[str, Any]) -> Dict[str, Any]:
    """模型常把参数包在 payload.payload 里或漏传 action；与 MCP 侧归一化保持一致。"""
    out = dict(pl)
    nested = out.get("payload")
    if isinstance(nested, dict) and (nested.get("action") or "").strip():
        base = {k: v for k, v in out.items() if k != "payload"}
        out = {**base, **nested}
    return out


async def run_comfly_veo(
    payload: Dict[str, Any],
    user_id: int,
    request: Request,
    db: Session,
) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload 须为对象")
    payload = _normalize_comfly_veo_payload(payload)
    action = (payload.get("action") or "").strip()
    if not action:
        raise HTTPException(
            status_code=400,
            detail=(
                "缺少 action。请在 payload 内设置 action，取值之一：upload、generate_prompts、submit_video、poll_video。"
                "正确示例：{\"action\":\"upload\",\"asset_id\":\"素材ID\"}；"
                "勿将 action 放在工具参数顶层而 payload 为空。"
            ),
        )

    video_model = (payload.get("video_model") or "").strip() or _DEFAULT_VIDEO_MODEL
    analysis_model = (payload.get("analysis_model") or "").strip() or _DEFAULT_ANALYSIS_MODEL
    aspect_ratio = (payload.get("aspect_ratio") or "").strip() or _DEFAULT_ASPECT
    _reject_if_sutui_style_model("video_model", video_model)
    _reject_if_sutui_style_model("analysis_model", analysis_model)

    if action == "upload":
        aid_raw = (payload.get("asset_id") or "").strip()
        aid = aid_raw.lower()
        if not aid:
            logger.warning(
                "[comfly.daihuo] upload 缺少 asset_id；payload_keys=%s（多为模型未把附图 ID 写入工具参数，非素材库无此条）",
                sorted(payload.keys()),
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "upload 需要 payload.asset_id。请在 invoke_capability 中传入素材库 ID，"
                    "例如 {\"action\":\"upload\",\"asset_id\":\"（12位左右素材ID）\"}；"
                    "若用户本条消息已附图，须使用对话里注入的同一 asset_id，勿只传 action。"
                ),
            )
        row = (
            db.query(Asset)
            .filter(func.lower(Asset.asset_id) == aid, Asset.user_id == user_id)
            .first()
        )
        canonical_id = row.asset_id if row else aid_raw
        url = get_asset_public_url(canonical_id, user_id, request, db)
        if not url:
            raise HTTPException(
                status_code=400,
                detail=_comfly_upload_failure_detail(aid, user_id, db),
            )
        logger.info("[comfly.daihuo] upload asset_id=%s ok", canonical_id)
        return {"ok": True, "action": action, "asset_id": canonical_id, "image_url": url}

    if action == "generate_prompts":
        api_base, api_key = _resolve_comfly_credentials(user_id, db, request)
        image_url = (payload.get("image_url") or "").strip()
        if not image_url:
            raise HTTPException(status_code=400, detail="generate_prompts 需要 image_url")
        body: Dict[str, Any] = {
            "model": analysis_model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Product image URL: {image_url}\n"
                        'Reply with ONLY valid JSON (no markdown): '
                        '{"prompts":["...","...","...","...","..."]} '
                        "Five short English prompts for vertical ecommerce short video (9:16), product showcase + subtle camera motion."
                    ),
                }
            ],
            "temperature": 0.7,
        }
        extra_tools = _comfly_chat_tools_from_payload(payload)
        if extra_tools:
            body["tools"] = extra_tools
        chat_path = (getattr(settings, "comfly_chat_completions_path", None) or "/v1/chat/completions").strip()
        if chat_path.startswith("/"):
            chat_url = _comfly_request_url(api_base, chat_path)
        else:
            chat_url = _join_url(api_base, chat_path.lstrip("/"))
        ch = _headers(api_key)
        _log_comfly_http_out("chat_completions", "POST", chat_url, ch, body)
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(chat_url, headers=ch, json=body)
        _log_comfly_http_in("chat_completions", r)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Comfly POST /v1/chat/completions 失败 HTTP {r.status_code}: {(r.text or '')[:1000]}",
            )
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            content = json.dumps(data, ensure_ascii=False)[:4000]
        prompts = _parse_prompts_from_content(str(content))
        logger.info("[comfly.daihuo] generate_prompts count=%s", len(prompts))
        return {"ok": True, "action": action, "prompts": prompts, "raw": data}

    if action == "submit_video":
        api_base, api_key = _resolve_comfly_credentials(user_id, db, request)
        prompt = _prompt_for_veo_submit(payload)
        images = _images_for_veo_submit(payload, "")
        if not prompt:
            raise HTTPException(
                status_code=400,
                detail=(
                    "submit_video 需要 prompt，或 prompts 数组至少一条非空字符串"
                    "（上一步 generate_prompts 返回的 prompts 可直接传入 submit_video）"
                ),
            )
        if not images:
            raise HTTPException(
                status_code=400,
                detail="submit_video 需要 image_url 或可公网访问的图片 URL 列表 images[]（Comfly 文档字段名 images）",
            )
        images = _veo_clamp_images_for_model(video_model, images)
        if not images:
            raise HTTPException(status_code=400, detail="submit_video 参考图为空（经 model 裁剪后无有效 URL）")
        body: Dict[str, Any] = {
            "prompt": prompt,
            "model": video_model,
            "images": images,
        }
        ar = (payload.get("aspect_ratio") or aspect_ratio or "").strip()
        if ar in ("9:16", "16:9"):
            body["aspect_ratio"] = ar
        if payload.get("enhance_prompt") is True:
            body["enhance_prompt"] = True
        submit_path = (getattr(settings, "comfly_veo_submit_path", None) or "/v2/videos/generations").strip()
        post_url = _comfly_request_url(api_base, submit_path)
        sh = _headers(api_key)
        _log_comfly_http_out("videos_generations_submit", "POST", post_url, sh, body)
        async with httpx.AsyncClient(timeout=180.0) as client:
            r = await client.post(post_url, headers=sh, json=body)
        _log_comfly_http_in("videos_generations_submit", r)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Comfly 提交视频失败 HTTP {r.status_code}: {(r.text or '')[:1500]}",
            )
        out = r.json() if r.content else {}
        _data = out.get("data")
        _dd: Dict[str, Any] = _data if isinstance(_data, dict) else {}
        task_id = out.get("task_id") or out.get("id") or _dd.get("task_id") or _dd.get("id")
        tid = str(task_id).strip() if task_id else None
        logger.info("[comfly.daihuo] submit_video task_id=%s", tid or "(none)")
        return {
            "ok": True,
            "action": action,
            "task_id": tid,
            "result": out,
            **({"warning": "上游 JSON 中未解析到 task_id，请根据 result 自行取 ID"} if not tid else {}),
        }

    if action == "poll_video":
        api_base, api_key = _resolve_comfly_credentials(user_id, db, request)
        task_id = (payload.get("task_id") or "").strip()
        if not task_id:
            raise HTTPException(status_code=400, detail="poll_video 需要 task_id")
        tpl = (
            getattr(settings, "comfly_veo_poll_path_template", None) or "/v2/videos/generations/{task_id}"
        ).strip()
        path = tpl.format(task_id=quote(task_id, safe="")).strip()
        get_url = _comfly_request_url(api_base, path if path.startswith("/") else path.lstrip("/"))
        gh = _headers(api_key)
        _log_comfly_http_out("videos_generations_poll", "GET", get_url, gh, None, f"task_id={task_id[:80]}")
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(get_url, headers=gh)
        _log_comfly_http_in("videos_generations_poll", r, f"task_id={task_id[:80]}")
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Comfly 查询任务失败 HTTP {r.status_code}: {(r.text or '')[:1000]}",
            )
        result = r.json() if r.content else {}
        logger.info("[comfly.daihuo] poll_video task_id=%s", task_id)
        return {"ok": True, "action": action, "task_id": task_id, "result": result}

    raise HTTPException(status_code=400, detail=f"未知 action: {action}，支持 upload|generate_prompts|submit_video|poll_video")

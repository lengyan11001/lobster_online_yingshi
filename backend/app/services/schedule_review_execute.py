"""审核发布：按槽位已保存的 prompt 调用本机 POST /chat 生成素材（禁止 publish_content）。"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx

from ..api.auth import create_access_token
from ..core.config import settings
from .internal_chat_client import chat_headers_for_forwarded_browser, chat_headers_for_user

logger = logging.getLogger(__name__)

_CHAT_TIMEOUT_SEC = 40 * 60

_ASSET_ID_RE = re.compile(
    r"(?:asset_id|素材\s*ID|素材ID)[\"'\s:：=]+([a-zA-Z0-9_\-]{6,64})",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+", re.IGNORECASE)


def _api_base_url() -> str:
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if base:
        return base
    return f"http://127.0.0.1:{int(getattr(settings, 'port', 8000) or 8000)}"


def _extract_asset_ids_and_urls(reply: str) -> tuple[List[str], List[str]]:
    text = reply or ""
    ids = list(dict.fromkeys(_ASSET_ID_RE.findall(text)))
    urls = list(dict.fromkeys(_URL_RE.findall(text)))[:12]
    return ids[:20], urls[:12]


def _footer_no_publish() -> str:
    return (
        "\n\n【系统 · 审核预览】本条为定时任务审核预览，"
        "你必须调用 invoke_capability / task.get_result 等完成素材生成；"
        "**禁止**调用 publish_content、open_account_browser；"
        "生成完成后在回复中用自然语言说明拟发布标题、正文要点、话题标签，并列出已生成素材的 asset_id 或可访问预览 URL。"
    )


def resolved_attachment_ids_for_review_chat(
    attachment_asset_ids: Optional[List[str]],
    *,
    schedule_kind: Optional[str] = None,
    video_source_asset_id: Optional[str] = None,
) -> List[str]:
    """与定时配置一致：视频模式且配置了素材 ID 时，保证 POST /chat 的 attachment_asset_ids 含该 ID，以便解析公网 URL 并注入图生视频。"""
    raw = [a.strip() for a in (attachment_asset_ids or []) if isinstance(a, str) and a.strip()][:5]
    sk = (schedule_kind or "").strip().lower()
    v = (video_source_asset_id or "").strip()
    if sk == "video" and v:
        if v not in raw:
            return [v] + raw
        return raw
    return raw


async def execute_review_slot_generation(
    *,
    user_id: int,
    user_message: str,
    attachment_asset_ids: Optional[List[str]] = None,
    schedule_kind: Optional[str] = None,
    video_source_asset_id: Optional[str] = None,
    user_bearer_token: Optional[str] = None,
    x_installation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    单次：把 user_message（用户编辑后的完整提示词）发给 POST /chat，走 MCP 生成素材，不发布。
    返回 reply 全文及从正文中抽取的 asset_id / URL 线索。

    若定时为视频且配置了 video_source_asset_id，会与草稿中的 attachment_asset_ids 合并，
    保证请求体带素材 ID，由 chat 解析公网 URL 并注入图生视频（与仅写在 prompt 里不同）。
    """
    msg = (user_message or "").strip()
    if not msg:
        raise ValueError("提示词 prompt 不能为空")

    body = msg + _footer_no_publish()
    url = f"{_api_base_url()}/chat"
    ut = (user_bearer_token or "").strip()
    if ut:
        headers = chat_headers_for_forwarded_browser(
            user_id,
            bearer_token=ut,
            x_installation_id=x_installation_id,
        )
    else:
        token = create_access_token(
            data={"sub": str(user_id)},
            expires_delta=timedelta(hours=2),
        )
        headers = chat_headers_for_user(user_id, token)
    aids = resolved_attachment_ids_for_review_chat(
        attachment_asset_ids,
        schedule_kind=schedule_kind,
        video_source_asset_id=video_source_asset_id,
    )
    if aids:
        logger.info(
            "[审核生成] POST /chat 附图素材 ID（将解析为公网 URL）: %s",
            aids,
        )
    payload: Dict[str, Any] = {
        "message": body,
        "history": [],
        "model": None,
        "attachment_asset_ids": aids,
    }
    async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT_SEC, trust_env=False) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        raise ValueError((r.text or "")[:1200] or f"HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        raise ValueError(f"响应非 JSON: {e}") from e
    reply = (data.get("reply") or "").strip()
    if not reply:
        raise ValueError("对话返回为空，请检查本机模型与 MCP 是否正常")
    asset_ids, preview_urls = _extract_asset_ids_and_urls(reply)
    return {
        "ok": True,
        "reply": reply,
        "asset_ids": asset_ids,
        "preview_urls": preview_urls,
    }


def ensure_prompt_draft(d: Any) -> Dict[str, Any]:
    """统一槽位结构；兼容旧版仅 title/description 的条目。"""
    if not isinstance(d, dict):
        return {
            "prompt": "",
            "attachment_asset_ids": [],
            "params": {},
            "generated": {},
        }
    prompt = (d.get("prompt") or "").strip()
    if not prompt:
        title = (d.get("title") or "").strip()
        desc = (d.get("description") or "").strip()
        tags = d.get("tags")
        tag_s = ""
        if isinstance(tags, list):
            tag_s = " ".join(str(x) for x in tags[:20])
        elif isinstance(tags, str):
            tag_s = tags
        parts = []
        if title:
            parts.append(f"【标题意图】{title}")
        if desc:
            parts.append(f"【正文/描述】{desc}")
        if tag_s:
            parts.append(f"【标签】{tag_s}")
        if parts:
            prompt = "\n".join(parts)
    att = d.get("attachment_asset_ids")
    if not isinstance(att, list):
        att = []
    att = [str(x).strip() for x in att if str(x).strip()][:5]
    params = d.get("params") if isinstance(d.get("params"), dict) else {}
    gen = d.get("generated") if isinstance(d.get("generated"), dict) else {}
    return {
        "prompt": prompt,
        "attachment_asset_ids": att,
        "params": params,
        "generated": gen,
    }


def merge_generated_into_slot(slot: Dict[str, Any], gen: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(slot)
    prev = out.get("generated") if isinstance(out.get("generated"), dict) else {}
    merged = {**prev, **gen}
    out["generated"] = merged
    return out

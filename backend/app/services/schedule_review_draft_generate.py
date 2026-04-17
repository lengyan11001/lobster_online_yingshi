"""审核发布：仅生成「将发给 AI 的提示词与参数」草稿（JSON），不调用生成素材能力。"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

import httpx

from ..api.auth import create_access_token
from .internal_chat_client import chat_headers_for_forwarded_browser, chat_headers_for_user
from ..core.config import settings

logger = logging.getLogger(__name__)

_CHAT_TIMEOUT_SEC = 15 * 60


def _api_base_url() -> str:
    base = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")
    if base:
        return base
    return f"http://127.0.0.1:{int(getattr(settings, 'port', 8000) or 8000)}"


def _relax_json_text(s: str) -> str:
    """去除 BOM、统一弯引号，便于 json.loads。"""
    t = (s or "").strip()
    if t.startswith("\ufeff"):
        t = t[1:]
    trans = str.maketrans(
        {
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
        }
    )
    return t.translate(trans)


def _relax_json_text_minimal(s: str) -> str:
    """仅 BOM + strip；不做弯引号替换（避免把 JSON 字符串内的 Unicode 引号改成 ASCII 后破坏结构）。"""
    t = (s or "").strip()
    if t.startswith("\ufeff"):
        t = t[1:]
    return t


def _escape_raw_control_chars_in_json_strings(s: str) -> str:
    """将 JSON 字符串值内的原始控制字符（未写成 \\n 等）转义，修复模型常输出的「伪 JSON」。
    仅按引号状态处理，不误改 JSON 结构外的空白。"""
    out: List[str] = []
    i = 0
    n = len(s)
    in_string = False
    escape_next = False
    while i < n:
        c = s[i]
        if not in_string:
            if c == '"':
                in_string = True
                out.append(c)
            else:
                out.append(c)
            i += 1
            continue
        if escape_next:
            escape_next = False
            out.append(c)
            i += 1
            continue
        if c == "\\":
            escape_next = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_string = False
            out.append(c)
            i += 1
            continue
        o = ord(c)
        if c in "\n\r":
            out.append("\\n" if c == "\n" else "\\r")
            i += 1
            continue
        if o < 32 or o == 127:
            out.append(f"\\u{o:04x}")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _strip_trailing_commas(s: str) -> str:
    """反复去掉 ] 或 } 前的非法尾随逗号（模型常输出）。"""
    t = s
    for _ in range(12):
        t2 = re.sub(r",(\s*[\]}])", r"\1", t)
        if t2 == t:
            break
        t = t2
    return t


def _json_loads_relaxed(s: str) -> Any:
    t = _strip_trailing_commas(_relax_json_text(s))
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        t2 = _strip_trailing_commas(
            _escape_raw_control_chars_in_json_strings(_relax_json_text(s))
        )
        try:
            return json.loads(t2)
        except json.JSONDecodeError:
            t3 = _strip_trailing_commas(
                _escape_raw_control_chars_in_json_strings(_relax_json_text_minimal(s))
            )
            try:
                return json.loads(t3)
            except json.JSONDecodeError:
                # 模型常在字符串值内使用未转义的 ASCII "（如 关于"主题"的）或混用 markdown，标准 json 无法解析
                from json_repair import loads as json_repair_loads

                return json_repair_loads(t3)


def _extract_balanced_json_array_from_index(s: str, i: int) -> Optional[str]:
    """从 s[i]=='[' 起截取与之平衡的 JSON 数组子串（考虑字符串内的括号）。"""
    if i < 0 or i >= len(s) or s[i] != "[":
        return None
    depth = 0
    in_str = False
    esc = False
    j = i
    n = len(s)
    while j < n:
        c = s[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return s[i : j + 1]
        j += 1
    return None


def _extract_balanced_json_array(s: str) -> Optional[str]:
    """从文本中截取第一个与括号平衡的 JSON 数组子串。"""
    i = s.find("[")
    if i < 0:
        return None
    return _extract_balanced_json_array_from_index(s, i)


def _all_balanced_json_array_substrings(s: str) -> List[str]:
    """尝试每一个 '[' 位置作为数组起点，收集所有可截出的平衡数组（用于模型在 '[' 前写了说明的情况）。"""
    out: List[str] = []
    seen: set[str] = set()
    for i, c in enumerate(s):
        if c != "[":
            continue
        bal = _extract_balanced_json_array_from_index(s, i)
        if bal and bal not in seen:
            seen.add(bal)
            out.append(bal)
    return out


def _try_raw_decode_json_value(s: str) -> Optional[Any]:
    """从任意位置扫描，用 JSONDecoder.raw_decode 解析第一个完整 JSON 值（数组或对象）。"""
    t = _strip_trailing_commas(_relax_json_text(s))
    dec = json.JSONDecoder()
    for i, c in enumerate(t):
        if c not in "[{":
            continue
        try:
            val, _end = dec.raw_decode(t[i:])
            return val
        except Exception:
            continue
    return None


def _try_ndjson_prompt_objects(s: str) -> Optional[List[Any]]:
    """多行独立 JSON 对象，每行一个 {...} 且含 prompt/title。"""
    objs: List[Any] = []
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = _json_loads_relaxed(line)
        except Exception:
            continue
        if not isinstance(o, dict):
            continue
        if (
            str(o.get("prompt") or "").strip()
            or str(o.get("title") or "").strip()
            or str(o.get("description") or "").strip()
        ):
            objs.append(o)
    return objs if objs else None


def _dict_to_prompt_list(val: dict) -> Optional[List[Any]]:
    for k in (
        "drafts",
        "items",
        "data",
        "variants",
        "results",
        "prompts",
        "messages",
        "outputs",
        "list",
        "entries",
        "content",
        "response",
        "output",
        "answer",
    ):
        inner = val.get(k)
        if isinstance(inner, list):
            return inner
    p = val.get("prompt")
    if isinstance(p, str) and p.strip():
        return [val]
    return None


def _try_parse_to_array(raw: str) -> Optional[List[Any]]:
    """将一段文本解析为 JSON 数组；允许外层包一层对象或单条对象。"""
    s = (raw or "").strip()
    if not s:
        return None
    val: Any = None
    try:
        val = _json_loads_relaxed(s)
    except Exception:
        val = None

    if val is None:
        nd = _try_ndjson_prompt_objects(s)
        if nd is not None:
            return nd
        for bal in _all_balanced_json_array_substrings(s):
            try:
                val = _json_loads_relaxed(bal)
                break
            except Exception:
                continue
        if val is None:
            bal = _extract_balanced_json_array(s)
            if bal:
                try:
                    val = _json_loads_relaxed(bal)
                except Exception:
                    val = None
        if val is None:
            val = _try_raw_decode_json_value(s)

    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        inner = _dict_to_prompt_list(val)
        if inner is not None:
            return inner
    return None


def _tail_after_open_fence(text: str) -> Optional[str]:
    """若存在 ``` / ```json 且其后没有闭合 ```，取最后一段至文末（模型偶发漏写结尾围栏）。"""
    last: Optional[str] = None
    for m in re.finditer(r"```[a-zA-Z0-9_-]*\s*", text):
        rest = text[m.end() :]
        if "```" not in rest and "[" in rest:
            last = rest.strip()
    return last


def _code_block_contents(reply: str) -> List[str]:
    """提取所有 Markdown 围栏代码块内容（语言标记大小写不敏感）。"""
    text = reply or ""
    out: List[str] = []
    for m in re.finditer(r"```[a-zA-Z0-9_-]*\s*([\s\S]*?)```", text):
        block = m.group(1).strip()
        if block:
            out.append(block)
    tail = _tail_after_open_fence(text)
    if tail:
        out.append(tail)
    return out


def _parse_json_array_from_reply(reply: str) -> List[Any]:
    text = (reply or "").strip()
    candidates: List[str] = []
    for b in _code_block_contents(text):
        candidates.append(b)
    candidates.append(text)
    # 从全文截取平衡数组：第一个 + 每一个 '[' 起点（避免说明里出现 '[' 时首段截错）
    tail = _extract_balanced_json_array(text)
    if tail:
        candidates.append(tail)
    for bal in _all_balanced_json_array_substrings(text):
        candidates.append(bal)
    seen: set[str] = set()
    ordered: List[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)

    for cand in ordered:
        arr = _try_parse_to_array(cand)
        if arr is not None:
            return arr

    cap = 16000
    body = text if len(text) <= cap else (text[: cap - 1] + "…")
    logger.warning(
        "review_draft JSON 解析失败，reply_len=%s，全文（最多 %s 字，便于对照 app.log）:\n%s",
        len(text),
        cap,
        body.replace("\n", "\\n"),
    )
    raise ValueError(
        "无法从回复中解析 JSON 数组。请重试；或让模型只输出一个 ```json 代码块，"
        "内容为 JSON 数组（每项含 prompt 字段）。"
    )


def _normalize_prompt_draft(obj: Any) -> Dict[str, Any]:
    """每条为将发给 POST /chat 的提示词；可选附带素材 ID 列表。"""
    if not isinstance(obj, dict):
        return {
            "prompt": str(obj)[:12000] if obj is not None else "",
            "attachment_asset_ids": [],
            "params": {},
            "generated": {},
        }
    prompt = (
        (obj.get("prompt") or obj.get("user_message") or obj.get("message") or "").strip()
    )
    if not prompt:
        title = (obj.get("title") or "").strip()
        desc = (obj.get("description") or obj.get("body") or "").strip()
        if title or desc:
            prompt = f"【标题意图】{title}\n【正文/描述】{desc}"
    att = obj.get("attachment_asset_ids")
    if not isinstance(att, list):
        att = []
    att = [str(x).strip() for x in att if str(x).strip()][:5]
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    gen = obj.get("generated") if isinstance(obj.get("generated"), dict) else {}
    return {
        "prompt": str(prompt)[:12000],
        "attachment_asset_ids": att,
        "params": params,
        "generated": gen,
    }


def _merge_video_source_asset_into_drafts(
    drafts: List[Dict[str, Any]],
    *,
    schedule_kind: str,
    video_source_asset_id: Optional[str],
) -> List[Dict[str, Any]]:
    """定时配置里填了素材 ID 时，保证每条草稿都带该 ID，供 POST /chat 图生视频注入。"""
    sk = (schedule_kind or "").strip().lower()
    aid = (video_source_asset_id or "").strip()
    if sk != "video" or not aid:
        return drafts
    out: List[Dict[str, Any]] = []
    for d in drafts:
        if not isinstance(d, dict):
            out.append(d)
            continue
        att = d.get("attachment_asset_ids")
        if not isinstance(att, list):
            att = []
        merged = [str(x).strip() for x in att if str(x).strip()]
        if aid not in merged:
            merged = [aid] + merged
        d2 = dict(d)
        d2["attachment_asset_ids"] = merged[:5]
        out.append(d2)
    return out


def _build_generate_prompts_message(
    *,
    platform: str,
    nickname: str,
    schedule_kind: str,
    requirements_text: str,
    variant_count: int,
    replace_hint: str | None = None,
    video_source_asset_id: Optional[str] = None,
) -> str:
    n = max(1, min(10, int(variant_count)))
    sk = (schedule_kind or "image").strip().lower()
    v_aid = (video_source_asset_id or "").strip()
    is_video = sk == "video"
    lines = [
        "【仅生成提示词草稿 · 不调用生成图片/视频工具】",
        f"- 平台：{platform} · 账号昵称：{nickname}",
        f"- 内容类型：{'视频' if is_video else '图文'}",
        f"- 请输出恰好 {n} 条「将发给智能对话 POST /chat」的用户消息草稿。",
        "- 每条草稿应包含：用户会如何描述任务（模型、画面、生成要点、发布文案意图等），后续会原样用于调用能力。",
    ]
    if is_video and v_aid:
        lines.extend(
            [
                f"- 【当前定时配置 · 图生视频】已在「素材 ID」中指定参考图：{v_aid}。每条草稿必须按 **图生视频（image-to-video，基于参考图）** 来写，"
                "不要写成纯文生视频（从零描述画面、无参考图）。prompt 里应明确：基于素材库该参考图生成视频，描述镜头运动、时长、风格延续、口播/氛围等；"
                "不要只写「生成一段关于…的视频」而忽略参考图。",
                "- **不要**在本轮调用 image.generate / video.generate。JSON 里 **必须** 为每条包含 "
                f' `"attachment_asset_ids": ["{v_aid}"]` （可使用该真实 ID，与配置一致）；**不要**编造其它 asset_id。',
            ]
        )
    elif is_video:
        lines.extend(
            [
                "- 【当前定时配置 · 文生视频】未填写「素材 ID」，按 **文生视频** 撰写；**不要**编造 attachment_asset_ids 或假装有参考图。",
                "- **不要**在本轮调用 image.generate / video.generate；**不要**编造 asset_id；只写提示词与可选 params。",
            ]
        )
    else:
        lines.append(
            "- **不要**在本轮调用 image.generate / video.generate；**不要**输出真实 asset_id；只写提示词与可选参数。"
        )
    lines.append("")
    lines.extend(
        [
            "【账号定时说明（须融入各条提示词的语境）】",
            (requirements_text or "").strip() or "（未填写）",
            "",
        ]
    )
    if replace_hint:
        lines.append(replace_hint)
        lines.append("")
    fmt_attach = (
        f'- "attachment_asset_ids"：字符串数组；视频且已配置参考素材 ID 时 **必填**（须含该 ID）；未配置素材 ID 时可省略或 []。'
        if is_video
        else '- "attachment_asset_ids"：字符串数组，可选；'
    )
    lines += [
        "【输出格式（必须严格遵守）】",
        "1) 只输出一个 Markdown 代码块：以 ```json 开头、以 ``` 结尾；代码块外不要写任何说明、标题或多余文字。",
        "2) 代码块内**只能**是一个合法 JSON 数组（以 [ 开头、以 ] 结尾），不要使用尾随逗号。",
        "3) 数组每项为对象，至少包含字段：",
        '- "prompt"：字符串，完整用户消息（可含换行）；',
        fmt_attach,
        '- "params"：对象，可选。',
        "4) 若必须用对象包裹数组，仅允许顶层键名 \"drafts\"，例如：{\"drafts\":[...]} 。",
    ]
    return "\n".join(lines)


async def generate_review_drafts_via_chat(
    *,
    user_id: int,
    platform: str,
    nickname: str,
    schedule_kind: str,
    requirements_text: str,
    variant_count: int,
    replace_slot_hint: str | None = None,
    video_source_asset_id: Optional[str] = None,
    user_bearer_token: Optional[str] = None,
    x_installation_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    req = (requirements_text or "").strip()
    if not req:
        raise ValueError("请先填写「目标与要求」后再生成提示词")

    sk = (schedule_kind or "image").strip().lower()
    msg = _build_generate_prompts_message(
        platform=platform,
        nickname=nickname,
        schedule_kind=sk,
        requirements_text=req,
        variant_count=variant_count,
        replace_hint=replace_slot_hint,
        video_source_asset_id=video_source_asset_id,
    )
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
            expires_delta=timedelta(hours=1),
        )
        headers = chat_headers_for_user(user_id, token)
    payload = {
        "message": msg,
        "history": [],
        "model": None,
        "attachment_asset_ids": [],
        # 必须走 chat 的专用分支：lobster_online/logs/app.log 显示主对话 system 曾导致模型输出闲聊而非 JSON
        "review_prompt_drafts_only": True,
    }
    async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT_SEC, trust_env=False) as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.status_code != 200:
        raise ValueError((r.text or "")[:800] or f"HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        raise ValueError(f"响应非 JSON: {e}") from e
    reply = (data.get("reply") or "").strip()
    raw_list = _parse_json_array_from_reply(reply)
    n = max(1, min(10, int(variant_count)))
    if len(raw_list) < n:
        raise ValueError(
            f"模型仅返回 {len(raw_list)} 条提示词，需要 {n} 条；请重试或缩短「出几次」。"
        )
    out = [_normalize_prompt_draft(x) for x in raw_list[:n]]
    return _merge_video_source_asset_into_drafts(
        out,
        schedule_kind=sk,
        video_source_asset_id=video_source_asset_id,
    )

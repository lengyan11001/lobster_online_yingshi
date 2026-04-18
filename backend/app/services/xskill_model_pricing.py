"""按 xSkill 公开「模型文档」接口拉取 pricing，估算与预扣一致的算力参考（见 model-pricing-guide）。"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)

_PRICING_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}


def _cache_ttl_success() -> float:
    return max(60.0, float(getattr(settings, "xskill_model_docs_cache_ttl_seconds", 3600) or 3600))


def _cache_ttl_miss() -> float:
    return 300.0


def _cache_get_valid(model_id: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """返回 (是否命中未过期缓存, pricing)；命中且 pricing 为 None 表示已缓存的「无定价」。"""
    now = time.time()
    ent = _PRICING_CACHE.get(model_id)
    if not ent:
        return False, None
    exp, pricing = ent
    if now > exp:
        _PRICING_CACHE.pop(model_id, None)
        return False, None
    return True, pricing


def _cache_set(model_id: str, pricing: Optional[Dict[str, Any]], *, success: bool) -> None:
    ttl = _cache_ttl_success() if success and pricing is not None else _cache_ttl_miss()
    _PRICING_CACHE[model_id] = (time.time() + ttl, pricing)


def _num_images_from_params(params: Dict[str, Any]) -> int:
    n = params.get("num_images", params.get("n", 1))
    try:
        if isinstance(n, (int, float)):
            return max(1, int(n))
        if isinstance(n, str) and n.strip().isdigit():
            return max(1, int(n.strip()))
    except (TypeError, ValueError):
        pass
    return 1


def _duration_seconds_from_params(params: Dict[str, Any]) -> Optional[float]:
    for key in ("duration", "duration_sec", "video_duration", "length", "seconds"):
        v = params.get(key)
        if v is None:
            continue
        try:
            d = float(v)
            if d > 0:
                return d
        except (TypeError, ValueError):
            continue
    return None


def _audio_duration_seconds(params: Dict[str, Any]) -> Optional[float]:
    for key in ("duration", "duration_sec", "audio_duration", "length"):
        v = params.get(key)
        if v is None:
            continue
        try:
            d = float(v)
            if d > 0:
                return d
        except (TypeError, ValueError):
            continue
    return None


def _first_example_price(pricing: Dict[str, Any]) -> Optional[int]:
    ex: List[Any] = pricing.get("examples") or []
    if not isinstance(ex, list) or not ex:
        return None
    first = ex[0]
    if isinstance(first, dict) and first.get("price") is not None:
        try:
            return int(round(float(first["price"])))
        except (TypeError, ValueError):
            return None
    return None


def estimate_credits_from_pricing(pricing: Dict[str, Any], params: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """根据 docs 中 pricing + 本次 params 估算算力；无法精确时返回说明文案。"""
    ptype = (pricing.get("price_type") or "").strip()
    desc = (pricing.get("price_description") or "").strip()
    base = pricing.get("base_price")
    try:
        base_f = float(base) if base is not None else None
    except (TypeError, ValueError):
        base_f = None

    if ptype == "fixed":
        if base_f is None:
            p = _first_example_price(pricing)
            if p is not None:
                return p, desc
            return None, desc or "固定计价但缺少 base_price"
        return int(round(base_f)), desc

    if ptype == "quantity_based":
        if base_f is None:
            p = _first_example_price(pricing)
            return (p, desc) if p is not None else (None, desc or "按量计价但缺少单价")
        n = _num_images_from_params(params)
        return int(round(base_f * n)), f"{desc}（按本次约 {n} 张估算）" if n != 1 else desc

    if ptype in ("duration_based", "dynamic_per_second"):
        if base_f is None:
            p = _first_example_price(pricing)
            return (p, desc) if p is not None else (None, desc or "按时长计价但缺少单价")
        d = _duration_seconds_from_params(params)
        if d is None:
            ex_p = _first_example_price(pricing)
            if ex_p is not None:
                return ex_p, f"{desc}（未传 duration，按文档示例价参考）"
            return None, desc or "按时长计价，请在参数中提供 duration（秒）以便估算"
        return int(round(base_f * d)), f"{desc}（按本次约 {d:g} 秒估算）"

    if ptype == "audio_duration_based":
        if base_f is None:
            p = _first_example_price(pricing)
            return (p, desc) if p is not None else (None, desc or "按音频时长计价但缺少单价")
        d = _audio_duration_seconds(params)
        if d is None:
            ex_p = _first_example_price(pricing)
            if ex_p is not None:
                return ex_p, f"{desc}（未传时长，按文档示例价参考）"
            return None, desc or "按音频时长计价，请提供 duration 以便估算"
        return int(round(base_f * d)), f"{desc}（按本次约 {d:g} 秒估算）"

    if ptype == "duration_map":
        d = _duration_seconds_from_params(params)
        examples: List[Any] = pricing.get("examples") or []
        if d is not None and examples:
            for ex in examples:
                ex_desc = str(ex.get("description") or "")
                try:
                    ex_dur = float("".join(c for c in ex_desc if c.isdigit() or c == "."))
                except (ValueError, TypeError):
                    continue
                if ex_dur > 0 and d <= ex_dur:
                    return int(ex.get("price", 0)), f"{desc}（按 {d:g} 秒匹配档位）"
            if examples:
                return int(examples[-1].get("price", 0)), f"{desc}（按最长档位估算）"
        ex_p_dm = _first_example_price(pricing)
        if ex_p_dm is not None:
            return ex_p_dm, f"{desc}（按最短档位估算）" if desc else "按最短档位估算"
        if base_f is not None:
            return int(round(base_f)), desc
        return None, desc or "按时长分档计价，无法自动估算"

    if ptype == "token_based":
        return None, desc or "按 token 计费，确认前无法精确估算"

    ex_p = _first_example_price(pricing)
    if ex_p is not None:
        return ex_p, f"{desc}（按文档示例价参考）" if desc else "按文档示例价参考"
    if base_f is not None:
        return int(round(base_f)), desc or f"计价方式 {ptype or '未知'}，仅展示基础单价"
    return None, desc or f"计价方式 {ptype or '未知'}，无法自动估算"


async def fetch_model_pricing(model_id: str) -> Optional[Dict[str, Any]]:
    mid = (model_id or "").strip()
    if not mid:
        return None
    hit, cached = _cache_get_valid(mid)
    if hit:
        return cached

    base = (getattr(settings, "sutui_api_base", None) or "https://api.xskill.ai").strip().rstrip("/")
    lang = (getattr(settings, "xskill_model_docs_lang", None) or "zh").strip() or "zh"
    url = f"{base}/api/v3/models/{mid}/docs"
    try:
        async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
            r = await client.get(url, params={"lang": lang})
    except Exception as e:
        logger.warning("[xskill pricing] fetch failed model_id=%s err=%s", mid[:80], e)
        _cache_set(mid, None, success=False)
        return None

    if r.status_code != 200:
        logger.info("[xskill pricing] model_id=%s http=%s", mid[:80], r.status_code)
        _cache_set(mid, None, success=False)
        return None
    try:
        j = r.json()
    except Exception:
        _cache_set(mid, None, success=False)
        return None
    data = j.get("data") if isinstance(j, dict) else None
    if not isinstance(data, dict):
        _cache_set(mid, None, success=False)
        return None
    pr = data.get("pricing")
    pricing = pr if isinstance(pr, dict) else None
    _cache_set(mid, pricing, success=pricing is not None)
    return pricing

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import math
import os
import re
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, Generic, List, Optional, TypedDict, TypeVar
from urllib.parse import unquote, urlparse

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from json_repair import repair_json

try:
    from runtime import Args  # type: ignore
except ImportError:
    T = TypeVar("T")

    class Args(Generic[T]):  # type: ignore[override]
        def __init__(self, input: T):
            self.input = input


class Input(TypedDict, total=False):
    product_image: str
    reference_images: List[str]
    sku: str
    selling_points: List[Dict[str, Any]]
    specs: Dict[str, Any]
    style: str
    style_reference_images: List[str]
    icon_assets: List[Dict[str, Any]]
    scene_preferences: Dict[str, Any]
    output_targets: Dict[str, Any]
    detail_template_id: str
    showcase_template_id: str
    brand: str
    compliance_notes: List[str]
    product_name_hint: str
    product_direction_hint: str
    apikey: str
    base_url: str
    platform: str
    country: str
    language: str
    target_market: str
    analysis_model: str
    image_model: str
    aspect_ratio: str
    page_count: int
    page_width: int
    page_height: int
    page_gap_px: int
    output_dir: str
    upload_retries: int
    analysis_retries: int
    image_generation_retries: int
    network_retry_delay_seconds: int
    image_concurrency: int


class Output(TypedDict):
    code: int
    msg: str
    data: Dict[str, Any] | None


@dataclass
class PipelineConfig:
    base_url: str
    api_key: str
    sku: str = ""
    selling_points: List[Dict[str, Any]] | None = None
    specs: Dict[str, Any] | None = None
    style: str = ""
    style_reference_images: List[str] | None = None
    icon_assets: List[Dict[str, Any]] | None = None
    scene_preferences: Dict[str, Any] | None = None
    output_targets: Dict[str, Any] | None = None
    detail_template_id: str = ""
    showcase_template_id: str = ""
    brand: str = ""
    compliance_notes: List[str] | None = None
    template_config: Dict[str, Any] | None = None
    showcase_template_config: Dict[str, Any] | None = None
    product_name_hint: str = ""
    product_direction_hint: str = ""
    platform: str = "ecommerce"
    country: str = ""
    language: str = "zh-CN"
    target_market: str = ""
    analysis_model: str = "gemini-2.5-pro"
    image_model: str = "nano-banana-2"
    aspect_ratio: str = "9:16"
    page_count: int = 12
    page_width: int = 790
    page_height: int = 1250
    page_gap_px: int = 0
    output_dir: str = ""
    upload_retries: int = 3
    analysis_retries: int = 2
    image_generation_retries: int = 3
    network_retry_delay_seconds: int = 3
    image_concurrency: int = 11


class PipelineError(RuntimeError):
    pass


class PipelineExecutionError(PipelineError):
    def __init__(self, message: str, *, code: int = -500, data: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


MODEL_UNIT_COSTS: Dict[str, int] = {"analysis": 10, "image": 40}
SUITE_EXPORT_PRESET = "shangjia_taotu_v1"
SUITE_EXPORT_DIRNAME = "上架套图"
SUITE_EXPORT_CATEGORY_DIRS: Dict[str, str] = {
    "main_images": "1 】主图",
    "sku_images": "2 】SKU图",
    "transparent_white_bg": "3 】透明白底",
    "detail_images": "4 】详情图",
    "material_images": "5 】素材图",
    "showcase_images": "6 】橱窗图",
}
DEFAULT_SELLING_POINTS = [
    "核心卖点突出",
    "适合移动端详情页展示",
    "强调场景价值与使用体验",
]
DEFAULT_TRUST_POINTS = ["细节清晰可见", "风格统一专业", "适合电商转化表达"]
DEFAULT_USAGE_SCENES = ["居家场景", "日常使用场景", "近景细节场景"]


class RunLogger:
    def __init__(self, base_dir: str, config: PipelineConfig, raw_input: Dict[str, Any]) -> None:
        root = Path(base_dir)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = root / f"run_{stamp}"
        seq = 1
        while self.run_dir.exists():
            seq += 1
            self.run_dir = root / f"run_{stamp}_{seq:02d}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.detail_dir = self.run_dir / "detail"
        self.detail_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.manifest: Dict[str, Any] = {
            "run_dir": str(self.run_dir),
            "created_at": datetime.now().isoformat(),
            "status": "running",
            "config": {
                "base_url": config.base_url,
                "platform": config.platform,
                "country": config.country,
                "language": config.language,
                "analysis_model": config.analysis_model,
    "image_model": config.image_model,
    "aspect_ratio": config.aspect_ratio,
    "page_count": config.page_count,
    "page_width": config.page_width,
    "page_height": config.page_height,
    "page_gap_px": config.page_gap_px,
    "image_concurrency": config.image_concurrency,
            },
            "input": {k: v for k, v in raw_input.items() if k != "apikey"},
            "artifacts": {
                "detail_dir": str(self.detail_dir),
            },
            "usage": {
                "summary": {"analysis_count": 0, "image_count": 0, "total_points": 0, "total_units": 0},
                "breakdown": {"analysis": {}, "image": {}},
                "records": [],
            },
            "steps": {},
            "pages": {},
            "errors": [],
        }
        self.write_json("00_input.json", self.manifest["input"])
        self._save()

    def write_json(self, filename: str, payload: Any) -> None:
        with (self.run_dir / filename).open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def step(self, name: str, status: str, attempts: int = 0, payload: Any = None, error: str | None = None) -> None:
        with self.lock:
            self.manifest["steps"][name] = {
                "status": status,
                "attempts": attempts,
                "error": error,
                "updated_at": datetime.now().isoformat(),
            }
            self._save()
        if payload is not None:
            self.write_json(f"{name}.json", payload)

    def page(self, index: int, stage: str, status: str, attempts: int = 0, payload: Any = None, error: str | None = None) -> None:
        key = str(index)
        with self.lock:
            self.manifest["pages"].setdefault(key, {})[stage] = {
                "status": status,
                "attempts": attempts,
                "error": error,
                "updated_at": datetime.now().isoformat(),
            }
            self._save()
        if payload is not None:
            self.write_json(f"page_{index:02d}_{stage}.json", payload)

    def error(self, where: str, message: str) -> None:
        with self.lock:
            self.manifest["errors"].append({"where": where, "message": message, "ts": datetime.now().isoformat()})
            self._save()

    def record_usage(self, kind: str, model: str, context: str, payload: Any = None) -> None:
        units = MODEL_UNIT_COSTS.get(kind, 0)
        with self.lock:
            usage = self.manifest["usage"]
            usage["summary"][f"{kind}_count"] += 1
            usage["summary"]["total_points"] += units
            usage["summary"]["total_units"] += units
            bucket = usage["breakdown"].setdefault(kind, {}).setdefault(
                model, {"count": 0, "points": 0, "units": 0}
            )
            bucket["count"] += 1
            bucket["points"] += units
            bucket["units"] += units
            usage["records"].append(
                {
                    "kind": kind,
                    "model": model,
                    "context": context,
                    "points": units,
                    "units": units,
                    "ts": datetime.now().isoformat(),
                    "payload": payload,
                }
            )
            self._save()

    def usage_snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.manifest["usage"], ensure_ascii=False))

    def finish(self, status: str, payload: Any = None) -> None:
        with self.lock:
            self.manifest["status"] = status
            self.manifest["finished_at"] = datetime.now().isoformat()
            self._save()
        if payload is not None:
            self.write_json("99_result.json", payload)

    def _save(self) -> None:
        with (self.run_dir / "manifest.json").open("w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)


def _retry(action: str, attempts: int, delay: int, logger: RunLogger, fn):
    last: Optional[Exception] = None
    for idx in range(1, attempts + 1):
        try:
            return fn(), idx
        except Exception as exc:
            last = exc
            logger.error(action, f"attempt {idx} failed: {exc}")
            if idx >= attempts:
                break
            time.sleep(max(1, delay) * idx)
    raise PipelineError(f"{action} failed after {attempts} attempt(s): {last}")


def _usage_billing_summary(usage: Dict[str, Any]) -> Dict[str, Any]:
    summary = usage.get("summary") if isinstance(usage.get("summary"), dict) else {}
    analysis_count = int(summary.get("analysis_count") or 0)
    image_count = int(summary.get("image_count") or 0)
    total_points = int(summary.get("total_points") or 0)
    analysis_points_per_call = int(MODEL_UNIT_COSTS.get("analysis", 0))
    image_points_per_success = int(MODEL_UNIT_COSTS.get("image", 0))
    return {
        "analysis_points_per_call": analysis_points_per_call,
        "image_points_per_success": image_points_per_success,
        "analysis_count": analysis_count,
        "image_count": image_count,
        "analysis_points": analysis_count * analysis_points_per_call,
        "image_points": image_count * image_points_per_success,
        "total_points": total_points,
        "point_value_cny": 0.01,
        "total_cost_cny": round(total_points * 0.01, 2),
    }


def _error_text(exc: Any) -> str:
    return str(exc or "").strip()


def _is_insufficient_quota_error(exc: Any) -> bool:
    text = _error_text(exc).lower()
    markers = [
        "insufficient_user_quota",
        "insufficient balance",
        "credits insufficient",
        "prepaid quota",
        "预扣费额度失败",
        "剩余额度",
    ]
    return any(marker.lower() in text for marker in markers)


def _is_provider_quota_like_error(exc: Any) -> bool:
    text = _error_text(exc).lower()
    markers = [
        "credits insufficient",
        "current balance",
        "insufficient balance",
        "quota",
        "rate limit",
        "bad_response_body",
        "预扣费额度失败",
        "剩余额度",
        "insufficient_user_quota",
    ]
    return any(marker in text for marker in markers)


def _is_retriable_generation_error(exc: Any) -> bool:
    text = _error_text(exc).lower()
    if _is_provider_quota_like_error(exc):
        return True
    markers = [
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "timed out",
        "timeout",
        "connection aborted",
        "connection reset",
        "temporarily unavailable",
        "no url",
    ]
    return any(marker in text for marker in markers)


def _extract_quota_details(exc: Any) -> Dict[str, str]:
    text = _error_text(exc)
    details: Dict[str, str] = {}
    remaining = re.search(r"剩余额度[:：]\s*([^\s,，)]+)", text)
    required = re.search(r"需要预扣费额度[:：]\s*([^\s,，)]+)", text)
    request_id = re.search(r"request id[:：]?\s*([A-Za-z0-9_-]+)", text, re.IGNORECASE)
    if remaining:
        details["remaining_quota"] = remaining.group(1)
    if required:
        details["required_quota"] = required.group(1)
    if request_id:
        details["request_id"] = request_id.group(1)
    return details


def _build_partial_failure_payload(
    *,
    logger: RunLogger,
    exc: Exception,
    config: Optional[PipelineConfig] = None,
    partial_output: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    usage = logger.usage_snapshot()
    payload: Dict[str, Any] = {
        "run_dir": str(logger.run_dir),
        "detail_dir": str(logger.detail_dir),
        "manifest_path": str(logger.run_dir / "manifest.json"),
        "result_path": str(logger.run_dir / "99_result.json"),
        "usage": usage,
        "billing_summary": _usage_billing_summary(usage),
        "error": str(exc),
    }
    if config is not None:
        payload["config"] = {
            "analysis_model": config.analysis_model,
            "image_model": config.image_model,
            "detail_template_id": config.detail_template_id,
            "showcase_template_id": config.showcase_template_id,
        }
    if partial_output:
        payload.update({k: v for k, v in partial_output.items() if v is not None})
    if _is_insufficient_quota_error(exc):
        payload["error_type"] = "insufficient_user_quota"
        payload["quota_details"] = _extract_quota_details(exc)
    return payload


def _friendly_failure_message(exc: Exception, payload: Dict[str, Any]) -> tuple[int, str]:
    if _is_insufficient_quota_error(exc):
        quota = payload.get("quota_details") if isinstance(payload.get("quota_details"), dict) else {}
        remaining = str(quota.get("remaining_quota") or "").strip()
        required = str(quota.get("required_quota") or "").strip()
        detail_parts = []
        if remaining:
            detail_parts.append(f"剩余额度 {remaining}")
        if required:
            detail_parts.append(f"所需额度 {required}")
        detail_text = "，".join(detail_parts)
        message = "额度不足，流程已中止"
        if detail_text:
            message = f"{message}（{detail_text}）"
        message = f"{message}。已保留运行目录和中间结果，可直接补额度后重跑。"
        return 402, message
    return -500, f"Pipeline failed: {exc}"


class ComflyClient:
    def __init__(self, config: PipelineConfig, logger: RunLogger) -> None:
        self.config = config
        self.logger = logger
        self.base_url = config.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.api_key}", "Accept": "application/json"})

    def _check(self, response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            payload = {"raw_text": response.text}
        if response.status_code != 200 or not isinstance(payload, dict):
            raise PipelineError(f"HTTP {response.status_code}: {payload}")
        return payload

    def upload(self, src: str) -> tuple[str, int]:
        if src.startswith("http://") or src.startswith("https://"):
            return src, 0

        path = _resolve_local_path(src)
        if not path.exists():
            raise PipelineError(f"Image file not found: {src}")

        def call() -> str:
            with path.open("rb") as f:
                response = self.session.post(
                    f"{self.base_url}/v1/files",
                    files={"file": (path.name, f, "application/octet-stream")},
                    timeout=120,
                )
            payload = self._check(response)
            url = payload.get("url")
            if not isinstance(url, str) or not url.strip():
                raise PipelineError(f"Upload returned no url: {payload}")
            return url.strip()

        return _retry("upload", self.config.upload_retries, self.config.network_retry_delay_seconds, self.logger, call)

    def analyze_json(
        self,
        model: str,
        prompt: str,
        image_urls: List[str],
        action: str,
        max_tokens: int = 4000,
    ) -> tuple[Dict[str, Any], int]:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": int(max_tokens),
        }

        def call() -> Dict[str, Any]:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
            payload = self._check(response)
            text = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = _parse_json_block(text)
            parsed["_raw_text"] = text
            return parsed

        return _retry(action, self.config.analysis_retries, self.config.network_retry_delay_seconds, self.logger, call)

    def generate_image(self, model: str, prompt: str, aspect_ratio: str, refs: List[str], action: str) -> tuple[Dict[str, Any], int]:
        body: Dict[str, Any] = {"model": model, "prompt": prompt, "aspect_ratio": aspect_ratio}
        if refs:
            body["image"] = refs

        def call() -> Dict[str, Any]:
            response = self.session.post(
                f"{self.base_url}/v1/images/generations",
                headers={"Content-Type": "application/json"},
                json=body,
                timeout=180,
            )
            payload = self._check(response)
            data = payload.get("data", [])
            if not isinstance(data, list) or not data:
                raise PipelineError(f"Image generation returned no data: {payload}")
            first = data[0] if isinstance(data[0], dict) else {}
            url = first.get("url")
            if not isinstance(url, str) or not url.strip():
                raise PipelineError(f"Image generation returned no url: {payload}")
            return {"url": url.strip(), "raw": payload, "request": body}

        return _retry(
            action,
            self.config.image_generation_retries,
            self.config.network_retry_delay_seconds,
            self.logger,
            call,
        )


def _parse_json_block(text: str) -> Dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped:
        raise PipelineError("Model returned empty response")
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    for candidate in candidates:
        try:
            repaired = repair_json(candidate, ensure_ascii=False)
            parsed = json.loads(repaired)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    raise PipelineError(f"Unable to parse JSON from model output: {stripped[:500]}")


def _safe_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _safe_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _dedupe_strings(items: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in items:
        clean = str(item or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _normalize_selling_point_records(value: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw in _safe_dict_list(value):
        title = _sanitize_copy_text(raw.get("title") or raw.get("name") or raw.get("headline") or "")
        description = _sanitize_copy_text(raw.get("description") or raw.get("desc") or raw.get("detail") or "")
        icon = _sanitize_copy_text(raw.get("icon") or "")
        priority_raw = raw.get("priority")
        priority: Optional[int]
        if isinstance(priority_raw, int):
            priority = priority_raw
        elif isinstance(priority_raw, str) and priority_raw.strip().isdigit():
            priority = int(priority_raw.strip())
        else:
            priority = None
        if not title and not description:
            continue
        rows.append(
            {
                "title": title or description[:16] or "卖点",
                "description": description,
                "icon": icon,
                "priority": priority,
            }
        )
    rows.sort(key=lambda item: item.get("priority") if isinstance(item.get("priority"), int) else 9999)
    return rows


def _selling_point_display_texts(records: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for item in records:
        title = _sanitize_copy_text(item.get("title") or "")
        description = _sanitize_copy_text(item.get("description") or "")
        if title and description:
            out.append(f"{title}：{description}")
        elif title:
            out.append(title)
        elif description:
            out.append(description)
    return _dedupe_strings(out)


def _normalize_specs(value: Any) -> Dict[str, str]:
    raw = _safe_dict(value)
    out: Dict[str, str] = {}
    for key, item in raw.items():
        clean_key = _sanitize_copy_text(key)
        clean_value = _sanitize_copy_text(item)
        if clean_key and clean_value:
            out[clean_key] = clean_value
    return out


def _spec_entries(value: Any) -> List[Dict[str, str]]:
    specs = _normalize_specs(value)
    return [{"key": key, "value": val} for key, val in specs.items()]


def _normalize_icon_assets(value: Any) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen = set()
    for raw in _safe_dict_list(value):
        icon = _sanitize_copy_text(raw.get("icon") or raw.get("name") or "")
        url = str(raw.get("url") or raw.get("image_url") or "").strip()
        if not icon or not url:
            continue
        key = icon.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"icon": icon, "url": url})
    return rows


DEFAULT_STYLE_PRESET_ID = "creamy_wood"
DEFAULT_SHOWCASE_TEMPLATE_ID = "showcase_template_01"
STYLE_PRESET_ALIASES: Dict[str, str] = {
    "creamy_vintage": "creamy_vintage",
    "memphis_vintage": "creamy_vintage",
    "creamy_wood": "creamy_wood",
    "french_creamy": "french_creamy",
    "中古孟菲斯风": "creamy_vintage",
    "奶油中古风": "creamy_vintage",
    "奶油原木风": "creamy_wood",
    "法式奶油风": "french_creamy",
}
_STYLE_PRESET_CACHE: Dict[str, Dict[str, Any]] = {}


def _style_preset_root() -> Path:
    return Path(__file__).resolve().parent.parent / "style_presets"


def _normalize_style_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", "", raw)
    lowered = compact.lower().replace("-", "_")
    direct = STYLE_PRESET_ALIASES.get(compact) or STYLE_PRESET_ALIASES.get(lowered)
    if direct:
        return direct
    if "孟菲斯" in raw or "memphis" in lowered or ("中古" in raw and "法式" not in raw):
        return "creamy_vintage"
    if "原木" in raw or "wood" in lowered:
        return "creamy_wood"
    if "法式" in raw or "french" in lowered:
        return "french_creamy"
    return re.sub(r"[^a-z0-9_]+", "_", lowered).strip("_")


def _load_style_preset(style_id: Any) -> Dict[str, Any]:
    normalized = _normalize_style_id(style_id) or DEFAULT_STYLE_PRESET_ID
    cached = _STYLE_PRESET_CACHE.get(normalized)
    if cached:
        return dict(cached)
    path = _style_preset_root() / f"{normalized}.json"
    if not path.is_file():
        if normalized != DEFAULT_STYLE_PRESET_ID:
            return _load_style_preset(DEFAULT_STYLE_PRESET_ID)
        raise PipelineError(f"Style preset config not found: {normalized}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PipelineError(f"Failed to read style preset config: {path}") from exc
    if not isinstance(payload, dict):
        raise PipelineError(f"Invalid style preset config: {path}")
    payload["style_id"] = str(payload.get("style_id") or normalized).strip() or normalized
    _STYLE_PRESET_CACHE[normalized] = payload
    return dict(payload)


def _style_prompt_details(
    analysis: Dict[str, Any], *, include_copy_tone: bool = False, mode: str = "scene"
) -> List[str]:
    preset = analysis.get("style_preset") if isinstance(analysis.get("style_preset"), dict) else {}
    if not preset:
        return []
    parts: List[str] = []
    display_name = str(preset.get("display_name") or preset.get("style_id") or "").strip()
    palette = ", ".join(_safe_string_list(preset.get("palette"))[:5])
    materials = ", ".join(_safe_string_list(preset.get("materials"))[:5])
    scene_objects = ", ".join(_safe_string_list(preset.get("scene_objects"))[:6])
    decorative = ", ".join(_safe_string_list(preset.get("decorative_objects"))[:6])
    prompt_keywords = ", ".join(_safe_string_list(preset.get("prompt_keywords_en"))[:8])
    negative_rules = ", ".join(_safe_string_list(preset.get("negative_rules"))[:6])
    lighting_payload = preset.get("lighting") if isinstance(preset.get("lighting"), dict) else {}
    lighting_desc = str(lighting_payload.get("description") or "").strip()
    scene_direction = str(preset.get("scene_direction") or "").strip()
    copy_tone = str(preset.get("copy_tone") or "").strip()
    if display_name:
        parts.append(f"Style preset: {display_name}")
    parts.append(
        "Style instructions may affect only lighting, environment, mood, and props; they must never change the product's own design, structure, materials, color blocking, or component layout"
    )
    if scene_direction:
        parts.append(f"Scene direction: {scene_direction}")
    if palette:
        parts.append(f"Palette cues: {palette}")
    if materials:
        parts.append(f"Preferred materials and finishes: {materials}")
    if lighting_desc:
        parts.append(f"Lighting direction: {lighting_desc}")
    if mode != "product_only" and scene_objects:
        parts.append(f"Scene objects to weave in naturally: {scene_objects}")
    if mode != "product_only" and decorative:
        parts.append(f"Decor accents: {decorative}")
    if prompt_keywords:
        parts.append(f"Style keywords: {prompt_keywords}")
    if mode != "product_only" and include_copy_tone and copy_tone:
        parts.append(f"Copy tone direction: {copy_tone}")
    if negative_rules:
        parts.append(f"Avoid these style mistakes: {negative_rules}")
    return parts


def _product_identity_guardrails(analysis: Dict[str, Any]) -> List[str]:
    category = str(analysis.get("category") or "product").strip()
    target_name = str(analysis.get("product_name") or category or "product").strip()
    return [
        f"Treat the reference image as the exact sellable {target_name}, not as loose inspiration for a redesigned or upgraded variant",
        "Product fidelity is the top priority; if any style, scene, camera, or atmosphere instruction conflicts with the reference product, preserve the reference product and relax the scene instead",
        "Do not redesign, beautify, simplify, optimize, reinterpret, merge, split, extend, or invent a new version of the product",
        "Do not add, remove, rearrange, enlarge, shrink, or replace visible structural parts, openings, panels, handles, accessories, hardware, textures, seams, prints, or decorative features from the reference",
        "Keep the same silhouette, proportions, construction logic, material boundaries, color placement, and relative position of all visible parts",
        "Preserve the exact visible product colors, undertones, wood grain, material finish, and contrast from the reference; do not bleach, tint, warm up, cool down, or recolor the product",
        "Keep the product close to the visual center and safe area of the canvas with balanced breathing room; do not push the main product body against the edges unless the composition explicitly requires it",
        "If any product detail is unclear, stay conservative and preserve what is visible rather than inventing new details",
    ]


def _style_theme(analysis: Dict[str, Any], key: str, defaults: Dict[str, str]) -> Dict[str, str]:
    theme = {}
    preset = analysis.get("style_preset") if isinstance(analysis.get("style_preset"), dict) else {}
    if isinstance(preset.get(key), dict):
        theme = {str(k): str(v) for k, v in dict(preset.get(key) or {}).items() if str(v).strip()}
    merged = dict(defaults)
    merged.update(theme)
    return merged


def _template_root() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _load_detail_template_config(template_id: str) -> Dict[str, Any]:
    chosen = (template_id or "").strip() or "detail_template_01"
    path = _template_root() / f"{chosen}.json"
    if not path.is_file():
        if chosen != "detail_template_01":
            path = _template_root() / "detail_template_01.json"
        if not path.is_file():
            raise PipelineError(f"Detail template config not found: {chosen}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PipelineError(f"Failed to read detail template config: {path}") from exc
    if not isinstance(payload, dict):
        raise PipelineError(f"Invalid detail template config: {path}")
    return payload


def _showcase_template_root() -> Path:
    return Path(__file__).resolve().parent.parent / "showcase_templates"


def _load_showcase_template_config(template_id: str) -> Dict[str, Any]:
    chosen = (template_id or "").strip() or DEFAULT_SHOWCASE_TEMPLATE_ID
    path = _showcase_template_root() / f"{chosen}.json"
    if not path.is_file():
        if chosen != DEFAULT_SHOWCASE_TEMPLATE_ID:
            path = _showcase_template_root() / f"{DEFAULT_SHOWCASE_TEMPLATE_ID}.json"
        if not path.is_file():
            raise PipelineError(f"Showcase template config not found: {chosen}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PipelineError(f"Failed to read showcase template config: {path}") from exc
    if not isinstance(payload, dict):
        raise PipelineError(f"Invalid showcase template config: {path}")
    return payload


def _template_common(config: PipelineConfig) -> Dict[str, Any]:
    template = config.template_config if isinstance(config.template_config, dict) else {}
    common = template.get("common")
    return dict(common) if isinstance(common, dict) else {}


def _template_colors(config: PipelineConfig) -> Dict[str, str]:
    template = config.template_config if isinstance(config.template_config, dict) else {}
    colors = template.get("colors")
    if not isinstance(colors, dict):
        return {}
    return {str(key): str(val) for key, val in colors.items()}


def _template_variant(page: Dict[str, Any], config: PipelineConfig) -> str:
    metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    explicit = str(metadata.get("template") or "").strip().lower()
    if explicit:
        return explicit
    template = config.template_config if isinstance(config.template_config, dict) else {}
    aliases = template.get("slot_aliases")
    slot = str(page.get("slot") or "").strip().lower()
    if isinstance(aliases, dict):
        alias = aliases.get(slot)
        if alias:
            return str(alias).strip().lower()
    return slot


def _template_section(config: PipelineConfig, section: str) -> Dict[str, Any]:
    template = config.template_config if isinstance(config.template_config, dict) else {}
    payload = template.get(section)
    return dict(payload) if isinstance(payload, dict) else {}


def _showcase_template_config(config: PipelineConfig) -> Dict[str, Any]:
    template = config.showcase_template_config if isinstance(config.showcase_template_config, dict) else {}
    return dict(template) if isinstance(template, dict) else {}


def _showcase_target_count(analysis: Dict[str, Any], detail_page_count: int, config: PipelineConfig) -> int:
    template = _showcase_template_config(config)
    count_source = str(template.get("count_source") or "selling_points").strip().lower()
    min_count = max(1, int(template.get("min_count") or 4))
    max_count = max(min_count, int(template.get("max_count") or 6))
    if count_source == "detail_pages":
        base_count = detail_page_count
    else:
        base_count = len(_safe_string_list(analysis.get("selling_points")))
    if base_count <= 0:
        base_count = detail_page_count
    return max(min_count, min(max_count, base_count))


def _showcase_variant_sequence(config: PipelineConfig) -> List[int]:
    template = _showcase_template_config(config)
    values = template.get("variant_sequence")
    if not isinstance(values, list):
        return [0, 1, 2, 3]
    sequence: List[int] = []
    for value in values:
        try:
            number = int(value)
        except Exception:
            continue
        if number < 0:
            continue
        sequence.append(number % 4)
    return sequence or [0, 1, 2, 3]


def _showcase_theme_override(config: PipelineConfig) -> Dict[str, str]:
    template = _showcase_template_config(config)
    theme = template.get("theme")
    if not isinstance(theme, dict):
        return {}
    return {str(k): str(v) for k, v in theme.items() if str(v).strip()}


def _download_image_rgba(url: str, retries: int = 5, timeout: int = 180) -> Image.Image:
    last: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            return image.convert("RGBA")
        except Exception as exc:
            last = exc
            if attempt >= max(1, retries):
                break
            time.sleep(min(12, attempt * 2))
    raise PipelineError(f"Failed to download icon image after retries: {last}")


def _fit_contain(image: Image.Image, width: int, height: int) -> Image.Image:
    if image.width <= 0 or image.height <= 0:
        return Image.new("RGBA", (width, height), (0, 0, 0, 0))
    scale = min(width / image.width, height / image.height)
    resized = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    offset_x = max(0, (width - resized.width) // 2)
    offset_y = max(0, (height - resized.height) // 2)
    canvas.paste(resized, (offset_x, offset_y), resized if resized.mode == "RGBA" else None)
    return canvas


def _make_local_fallback_background(product_image: Image.Image, page: Dict[str, Any], config: PipelineConfig) -> Image.Image:
    width, height = _page_dimensions(page, config)
    canvas = Image.new("RGB", (width, height), "#f4efe8")
    draw = ImageDraw.Draw(canvas)
    shadow_w = int(width * 0.58)
    shadow_h = max(42, int(height * 0.08))
    shadow_x = (width - shadow_w) // 2
    shadow_y = int(height * 0.72)
    draw.ellipse((shadow_x, shadow_y, shadow_x + shadow_w, shadow_y + shadow_h), fill=(222, 212, 200))
    contain_w = int(width * 0.68)
    contain_h = int(height * 0.74)
    cutout = _fit_contain(product_image.convert("RGBA"), contain_w, contain_h)
    paste_x = (width - cutout.width) // 2
    paste_y = max(24, int(height * 0.08))
    canvas.paste(cutout, (paste_x, paste_y), cutout)
    return canvas


def _load_icon_images(icon_assets: Any, used_icon_ids: List[str]) -> Dict[str, Image.Image]:
    rows = _normalize_icon_assets(icon_assets)
    if not rows:
        return {}
    wanted = {str(item).strip().lower() for item in used_icon_ids if str(item).strip()}
    out: Dict[str, Image.Image] = {}
    for row in rows:
        icon_id = row["icon"].strip().lower()
        if wanted and icon_id not in wanted:
            continue
        try:
            out[icon_id] = _download_image_rgba(row["url"])
        except Exception:
            continue
    return out


def _sanitize_copy_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"</?[^>]+>", "", text)
    text = text.replace("**", "").replace("__", "").replace("##", "").replace("`", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -*\n\r\t")


def _sanitize_copy_list(value: Any, limit: int) -> List[str]:
    items = [_sanitize_copy_text(item) for item in _safe_string_list(value)]
    return [item for item in items if item][:limit]


def _short_overlay_phrase(value: Any, max_chars: int = 8) -> str:
    text = _sanitize_copy_text(value)
    if not text:
        return ""
    text = re.sub(r"[，。、“”‘’：；！!？?（）()\[\]{}<>《》·\-_/|]+", "", text)
    text = re.sub(r"\s+", "", text)
    return text[:max_chars]


def _resolve_local_path(src: str) -> Path:
    raw = str(src or "").strip()
    if not raw:
        return Path(raw)
    if raw.startswith("file://"):
        parsed = urlparse(raw)
        raw = unquote(parsed.path or "")
        if raw.startswith("/") and re.match(r"^/[A-Za-z]:", raw):
            raw = raw[1:]
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def _skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _default_output_root() -> Path:
    return _skill_root() / "runs"


def _resolve_output_dir(value: str) -> Path:
    raw = str(value or "").strip()
    root = _default_output_root()
    if not raw:
        return root
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if path.is_absolute():
        return path
    parts = list(path.parts)
    if parts and parts[0].lower() == "runs":
        parts = parts[1:]
    return (root.joinpath(*parts) if parts else root).resolve()


def _locale_defaults(platform: str, country: str, language: str, target_market: str) -> Dict[str, str]:
    normalized_country = (country or "").strip()
    normalized_language = (language or "").strip()
    language_name = normalized_language or (
        "English" if normalized_country.lower() in {"united states", "usa", "uk", "united kingdom"} else "Simplified Chinese"
    )
    market = target_market or (f"{normalized_country} ecommerce shoppers" if normalized_country else "Chinese ecommerce shoppers")
    return {
        "platform": (platform or "ecommerce").strip().lower(),
        "country": normalized_country or "China",
        "language_name": language_name,
        "market": market,
    }


def _build_config(data: Input) -> PipelineConfig:
    api_key = str(data.get("apikey") or os.getenv("COMFLY_API_KEY", "")).strip()
    if not api_key:
        raise PipelineError("Missing apikey")
    page_count = max(10, min(16, int(data.get("page_count", 12))))
    selling_points = _normalize_selling_point_records(data.get("selling_points"))
    specs = _normalize_specs(data.get("specs"))
    scene_preferences = _safe_dict(data.get("scene_preferences"))
    output_targets = _safe_dict(data.get("output_targets"))
    compliance_notes = _safe_string_list(data.get("compliance_notes"))
    style_reference_images = _safe_string_list(data.get("style_reference_images"))
    icon_assets = _normalize_icon_assets(data.get("icon_assets"))
    detail_template_id = str(data.get("detail_template_id", "")).strip() or "detail_template_01"
    showcase_template_id = str(data.get("showcase_template_id", "")).strip() or DEFAULT_SHOWCASE_TEMPLATE_ID
    return PipelineConfig(
        base_url=str(data.get("base_url", os.getenv("COMFLY_API_BASE", "https://ai.comfly.chat"))).strip(),
        api_key=api_key,
        sku=str(data.get("sku", "")).strip(),
        selling_points=selling_points or None,
        specs=specs or None,
        style=str(data.get("style", "")).strip(),
        style_reference_images=style_reference_images or None,
        icon_assets=icon_assets or None,
        scene_preferences=scene_preferences or None,
        output_targets=output_targets or None,
        detail_template_id=detail_template_id,
        showcase_template_id=showcase_template_id,
        brand=str(data.get("brand", "")).strip(),
        compliance_notes=compliance_notes or None,
        template_config=_load_detail_template_config(detail_template_id),
        showcase_template_config=_load_showcase_template_config(showcase_template_id),
        product_name_hint=str(data.get("product_name_hint", "")).strip(),
        product_direction_hint=str(data.get("product_direction_hint", "")).strip(),
        platform=str(data.get("platform", "ecommerce")).strip(),
        country=str(data.get("country", "")).strip(),
        language=str(data.get("language", "zh-CN")).strip(),
        target_market=str(data.get("target_market", "")).strip(),
        analysis_model=str(data.get("analysis_model", "gemini-2.5-pro")).strip(),
        image_model=str(data.get("image_model", "nano-banana-2")).strip(),
        aspect_ratio=str(data.get("aspect_ratio", "9:16")).strip(),
        page_count=page_count,
        page_width=int(data.get("page_width", 790)),
        page_height=int(data.get("page_height", 1250)),
        page_gap_px=int(data.get("page_gap_px", 0)),
        output_dir=str(data.get("output_dir", "")).strip(),
        upload_retries=int(data.get("upload_retries", 3)),
        analysis_retries=int(data.get("analysis_retries", 2)),
        image_generation_retries=int(data.get("image_generation_retries", 3)),
        network_retry_delay_seconds=int(data.get("network_retry_delay_seconds", 3)),
        image_concurrency=max(1, int(data.get("image_concurrency", 11))),
    )


def _analysis_prompt(config: PipelineConfig) -> str:
    locale = _locale_defaults(config.platform, config.country, config.language, config.target_market)
    user_hint_lines: List[str] = []
    if config.product_name_hint:
        user_hint_lines.append(f"User-supplied product name/title: {config.product_name_hint}")
    if config.product_direction_hint:
        user_hint_lines.append(f"User-supplied product direction/category: {config.product_direction_hint}")
    structured_inputs: Dict[str, Any] = {}
    if config.sku:
        structured_inputs["sku"] = config.sku
    if config.selling_points:
        structured_inputs["selling_points"] = config.selling_points
    if config.specs:
        structured_inputs["specs"] = config.specs
    if config.style:
        structured_inputs["style"] = config.style
    if config.style_reference_images:
        structured_inputs["style_reference_images"] = config.style_reference_images
    if config.icon_assets:
        structured_inputs["icon_assets"] = config.icon_assets
    if config.scene_preferences:
        structured_inputs["scene_preferences"] = config.scene_preferences
    if config.detail_template_id:
        structured_inputs["detail_template_id"] = config.detail_template_id
    if config.showcase_template_id:
        structured_inputs["showcase_template_id"] = config.showcase_template_id
    if config.brand:
        structured_inputs["brand"] = config.brand
    if config.compliance_notes:
        structured_inputs["compliance_notes"] = config.compliance_notes
    user_hint_block = "\n".join(user_hint_lines).strip()
    return f"""
You are a senior ecommerce creative strategist.
The user gave product reference images and wants a mobile ecommerce detail-image sequence.
Return strict JSON only with:
product_name, category, audience, product_summary, hero_claim, visual_style,
selling_points (8-12 strings), trust_points (3-6), usage_scenes (3-6),
materials, colors, structure_features, care_points, certification_clues, visual_constraints.
Rules:
1. Stay faithful to what is visible.
2. If information is uncertain, put it in visual_constraints conservatively.
3. product_summary and hero_claim must use the target consumer language.
4. If the user supplied product name/direction, treat that as the primary identity and category constraint. Do not reinterpret the product as a different category unless the user hint is obviously impossible.
5. When a user-supplied product name exists, keep product_name aligned with that user hint, while using the images to infer selling points, structure, scenes, and safe claims.
6. When structured selling points, specs, style, or scene preferences are supplied, treat them as the primary source of truth. Use the images to validate and enrich them, not to replace them.
7. If style is supplied, keep the visual_style aligned with that style_id rather than inventing a totally different style direction.
8. Treat the reference images as the exact sellable item. Do not upgrade, redesign, beautify, or reinterpret the product into a cleaner or more premium variant during analysis.
9. Preserve visible structure, proportions, openings, materials, color blocking, and component layout. If something is unclear, stay conservative instead of inventing a new product detail.
Platform: {locale["platform"]}
Country: {locale["country"]}
Target market: {locale["market"]}
Target language: {locale["language_name"]}
User hints:
{user_hint_block or "None"}
Structured inputs:
{json.dumps(structured_inputs, ensure_ascii=False, indent=2) if structured_inputs else "None"}
""".strip()


def _normalize_analysis(plan: Dict[str, Any], locale: Dict[str, str]) -> Dict[str, Any]:
    result = dict(plan)
    result["selling_points"] = _safe_string_list(plan.get("selling_points"))[:12] or DEFAULT_SELLING_POINTS[:]
    result["trust_points"] = _safe_string_list(plan.get("trust_points"))[:6] or DEFAULT_TRUST_POINTS[:]
    result["usage_scenes"] = _safe_string_list(plan.get("usage_scenes"))[:6] or DEFAULT_USAGE_SCENES[:]
    result["materials"] = _safe_string_list(plan.get("materials"))[:8]
    result["colors"] = _safe_string_list(plan.get("colors"))[:8]
    result["structure_features"] = _safe_string_list(plan.get("structure_features"))[:8]
    result["care_points"] = _safe_string_list(plan.get("care_points"))[:8]
    result["certification_clues"] = _safe_string_list(plan.get("certification_clues"))[:6]
    result["visual_constraints"] = _safe_string_list(plan.get("visual_constraints"))[:8]
    result["product_name"] = str(result.get("product_name") or "Product").strip()
    result["category"] = str(result.get("category") or "product").strip()
    result["audience"] = str(result.get("audience") or "ecommerce shoppers").strip()
    result["hero_claim"] = str(result.get("hero_claim") or result["selling_points"][0]).strip()
    result["product_summary"] = str(result.get("product_summary") or result["hero_claim"]).strip()
    result["visual_style"] = str(
        result.get("visual_style") or "clean ecommerce poster, warm lifestyle lighting, premium mobile detail page"
    ).strip()
    result["locale_profile"] = locale
    return result


def _merge_structured_inputs_into_analysis(analysis: Dict[str, Any], config: PipelineConfig) -> Dict[str, Any]:
    result = dict(analysis)
    selling_point_records = _normalize_selling_point_records(config.selling_points)
    inferred_points = _safe_string_list(result.get("selling_points"))
    if selling_point_records:
        result["selling_point_records"] = selling_point_records
        result["selling_points"] = _dedupe_strings(_selling_point_display_texts(selling_point_records) + inferred_points)[:12]
        if not str(result.get("hero_claim") or "").strip():
            result["hero_claim"] = selling_point_records[0]["title"]
        if not str(result.get("product_summary") or "").strip():
            result["product_summary"] = selling_point_records[0].get("description") or selling_point_records[0]["title"]
    else:
        result["selling_points"] = inferred_points[:12] or DEFAULT_SELLING_POINTS[:]

    specs = _normalize_specs(config.specs)
    if specs:
        result["specs"] = specs
        material_keys = [key for key in specs if any(token in key.lower() for token in ("材质", "material"))]
        size_keys = [key for key in specs if any(token in key.lower() for token in ("尺寸", "size", "长", "宽", "高"))]
        result["materials"] = _dedupe_strings(_safe_string_list(result.get("materials")) + [f"{key}：{specs[key]}" for key in material_keys])[:8]
        result["structure_features"] = _dedupe_strings(
            _safe_string_list(result.get("structure_features")) + [f"{key}：{specs[key]}" for key in size_keys]
        )[:8]

    if config.style:
        style_preset = _load_style_preset(config.style)
        result["style_id"] = str(style_preset.get("style_id") or _normalize_style_id(config.style) or config.style).strip()
        result["style_preset"] = style_preset
        result["style_display_name"] = str(style_preset.get("display_name") or result["style_id"]).strip()
        result["visual_style"] = str(style_preset.get("visual_style") or result.get("visual_style") or result["style_id"]).strip()
        result["colors"] = _dedupe_strings(_safe_string_list(style_preset.get("palette")) + _safe_string_list(result.get("colors")))[:8]
        result["materials"] = _dedupe_strings(_safe_string_list(style_preset.get("materials")) + _safe_string_list(result.get("materials")))[:8]
        style_scene_hints = _safe_string_list(style_preset.get("scene_objects"))[:4]
        if style_scene_hints:
            result["usage_scenes"] = _dedupe_strings(style_scene_hints + _safe_string_list(result.get("usage_scenes")))[:6]
        negative_rules = _safe_string_list(style_preset.get("negative_rules"))[:6]
        if negative_rules:
            result["visual_constraints"] = _dedupe_strings(_safe_string_list(result.get("visual_constraints")) + negative_rules)[:8]
    if config.style_reference_images:
        result["style_reference_images"] = config.style_reference_images
    if config.scene_preferences:
        result["scene_preferences"] = config.scene_preferences
        scene_hints: List[str] = []
        if config.scene_preferences.get("include_pet"):
            pet_type = _sanitize_copy_text(config.scene_preferences.get("pet_type") or "宠物")
            scene_hints.append(f"包含{pet_type}互动场景")
        if config.scene_preferences.get("include_human"):
            human_type = _sanitize_copy_text(config.scene_preferences.get("human_type") or "人物")
            scene_hints.append(f"包含{human_type}共居场景")
        for item in _safe_string_list(config.scene_preferences.get("decor_tags"))[:4]:
            scene_hints.append(f"软装元素：{item}")
        if scene_hints:
            result["usage_scenes"] = _dedupe_strings(scene_hints + _safe_string_list(result.get("usage_scenes")))[:6]
    if config.detail_template_id:
        result["detail_template_id"] = config.detail_template_id
    if config.showcase_template_id:
        result["showcase_template_id"] = config.showcase_template_id
    if config.icon_assets:
        result["icon_assets"] = config.icon_assets
    if config.output_targets:
        result["output_targets"] = config.output_targets
    if config.brand:
        result["brand"] = config.brand
    if config.compliance_notes:
        result["compliance_notes"] = config.compliance_notes
    if config.sku:
        result["sku"] = config.sku
    return result


def _apply_user_hints_to_analysis(analysis: Dict[str, Any], config: PipelineConfig) -> Dict[str, Any]:
    result = dict(analysis)
    name_hint = str(config.product_name_hint or "").strip()
    direction_hint = str(config.product_direction_hint or "").strip()
    if name_hint:
        result["product_name"] = name_hint
    if direction_hint:
        category = str(result.get("category") or "").strip()
        if category:
            if direction_hint.lower() not in category.lower():
                result["category"] = f"{direction_hint}, {category}"
        else:
            result["category"] = direction_hint
    user_hints: Dict[str, str] = {}
    if name_hint:
        user_hints["product_name_hint"] = name_hint
    if direction_hint:
        user_hints["product_direction_hint"] = direction_hint
    if user_hints:
        result["user_hints"] = user_hints
    return result


def _pad_points(points: List[str], minimum: int, fallback: List[str]) -> List[str]:
    out = [item for item in points if item]
    seed = [item for item in fallback if item]
    while len(out) < minimum and seed:
        out.append(seed[len(out) % len(seed)])
    while len(out) < minimum:
        out.append(f"补充卖点 {len(out) + 1}")
    return out


def _build_page_slots(analysis: Dict[str, Any], page_count: int) -> List[Dict[str, Any]]:
    points = _pad_points(_safe_string_list(analysis.get("selling_points")), 6, DEFAULT_SELLING_POINTS)
    trust_points = _safe_string_list(analysis.get("trust_points")) or DEFAULT_TRUST_POINTS[:]
    scenes = _safe_string_list(analysis.get("usage_scenes")) or DEFAULT_USAGE_SCENES[:]
    materials = _safe_string_list(analysis.get("materials"))
    structure = _safe_string_list(analysis.get("structure_features"))
    care = _safe_string_list(analysis.get("care_points"))
    certs = _safe_string_list(analysis.get("certification_clues"))
    hero_claim = str(analysis.get("hero_claim") or points[0]).strip()
    summary = str(analysis.get("product_summary") or hero_claim).strip()
    detail_target = max(10, int(page_count) - 1)
    hero_slot = {
        "slot": "hero_cover",
        "goal": "Create a premium, non-numbered advertising cover that sells why this product is worth bringing home.",
        "focus": hero_claim,
        "points": points[:3],
        "show_number": False,
    }

    detail_slots: List[Dict[str, Any]] = [
        {"slot": "overview", "goal": "Summarize the top consumer benefits at a glance.", "focus": summary, "points": points[:4], "show_number": True},
        {"slot": "feature", "goal": "Explain the first key feature with a hero visual.", "focus": points[0], "points": [points[0], points[1]]},
        {"slot": "feature", "goal": "Explain the second key feature with a strong comparison feel.", "focus": points[1], "points": [points[1], points[2]]},
        {"slot": "feature", "goal": "Explain the third key feature with detail emphasis.", "focus": points[2], "points": [points[2], points[3]]},
        {"slot": "feature", "goal": "Show another value point that supports conversion.", "focus": points[3], "points": [points[3], points[4]]},
        {"slot": "scene", "goal": "Show the product in a believable daily scene.", "focus": scenes[0], "points": scenes[:3]},
        {
            "slot": "material",
            "goal": "Explain material and structure details.",
            "focus": (materials + structure + care + [hero_claim])[0],
            "points": (materials + structure + care)[:4] or points[2:6],
        },
        {
            "slot": "trust",
            "goal": "Build confidence with conservative proof points.",
            "focus": (trust_points + certs + [hero_claim])[0],
            "points": (trust_points + certs)[:4] or points[1:5],
        },
        {"slot": "closing", "goal": "Close with a summary and buying motivation.", "focus": hero_claim, "points": points[:4]},
    ]

    extra_pool = points[4:] + trust_points + scenes + materials + structure + care + certs
    extra_idx = 0
    while len(detail_slots) < detail_target:
        focus = extra_pool[extra_idx] if extra_idx < len(extra_pool) else f"补充卖点 {len(slots) + 1}"
        detail_slots.insert(
            -1,
            {
                "slot": "feature",
                "goal": "Expand one more selling point to make the sequence more complete.",
                "focus": focus,
                "points": [focus] + points[max(0, (extra_idx % len(points)) - 1) : (extra_idx % len(points)) + 1],
            },
        )
        extra_idx += 1
    slots = [hero_slot] + detail_slots[:detail_target]
    return slots[: max(11, int(page_count))]


def _page_copy_prompt(analysis: Dict[str, Any], slots: List[Dict[str, Any]], config: PipelineConfig) -> str:
    locale = analysis.get("locale_profile") or _locale_defaults(
        config.platform, config.country, config.language, config.target_market
    )
    user_hints = analysis.get("user_hints") if isinstance(analysis.get("user_hints"), dict) else {}
    structured_context = {
        "sku": analysis.get("sku"),
        "selling_point_records": analysis.get("selling_point_records"),
        "specs": analysis.get("specs"),
        "style_id": analysis.get("style_id"),
        "style_preset": (
            {
                "display_name": (analysis.get("style_preset") or {}).get("display_name"),
                "palette": (analysis.get("style_preset") or {}).get("palette"),
                "materials": (analysis.get("style_preset") or {}).get("materials"),
                "copy_tone": (analysis.get("style_preset") or {}).get("copy_tone"),
            }
            if isinstance(analysis.get("style_preset"), dict)
            else None
        ),
        "style_reference_images": analysis.get("style_reference_images"),
        "icon_assets": analysis.get("icon_assets"),
        "scene_preferences": analysis.get("scene_preferences"),
        "detail_template_id": analysis.get("detail_template_id"),
        "showcase_template_id": analysis.get("showcase_template_id"),
        "brand": analysis.get("brand"),
        "compliance_notes": analysis.get("compliance_notes"),
    }
    slot_lines = [
        json.dumps(
            {
                "index": idx,
                "slot": slot["slot"],
                "goal": slot["goal"],
                "focus": slot["focus"],
                "points": slot.get("points", []),
                "metadata": slot.get("metadata", {}),
            },
            ensure_ascii=False,
        )
        for idx, slot in enumerate(slots, 1)
    ]
    return f"""
You are designing a mobile ecommerce detail-image sequence.
Return strict JSON only:
{{"pages":[{{"index":1,"slot":"cover","title":"...","subtitle":"...","highlights":["..."],"badge":"...","footer":"...","image_prompt_en":"...","layout_hint":"..."}}]}}
Rules:
1. Generate exactly {len(slots)} pages in the same order.
2. title/subtitle/highlights/badge/footer must use {locale["language_name"]}.
3. title must be plain text only. Do NOT use markdown, asterisks, HTML tags, bullets, numbering symbols, or emoji.
4. The cover page must feel like a premium campaign poster with a strong reason-to-buy headline and elevated hero composition.
5. subtitle one short sentence. highlights 2-4 short bullets.
6. For the cover page only, every highlight must be no more than 8 Chinese characters.
7. image_prompt_en must be English and describe a clean ecommerce background with NO text, NO watermark, NO UI.
8. Keep product appearance consistent across pages.
9. Avoid unsafe hard claims.
10. If user hints are provided, keep copy direction, category wording, and positioning aligned with those user hints.
11. If structured selling points and specs are supplied, prioritize them over inferred copy. Do not contradict them.
12. For slots like spec_table or parameter_summary, write cleaner and shorter copy suitable for specs cards and tabular information blocks.
Product analysis:
{json.dumps(analysis, ensure_ascii=False, indent=2)}
User hints:
{json.dumps(user_hints, ensure_ascii=False)}
Structured context:
{json.dumps(structured_context, ensure_ascii=False, indent=2)}
Page slots:
{chr(10).join(slot_lines)}
""".strip()


def _normalize_pages(plan: Dict[str, Any], slots: List[Dict[str, Any]], analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_pages = plan.get("pages")
    if not isinstance(raw_pages, list):
        raise PipelineError(f"Invalid page copy plan: {plan}")

    default_footer = str(analysis.get("hero_claim") or analysis.get("product_summary") or "").strip()
    pages: List[Dict[str, Any]] = []
    for idx, slot in enumerate(slots, 1):
        raw = raw_pages[idx - 1] if idx - 1 < len(raw_pages) and isinstance(raw_pages[idx - 1], dict) else {}
        is_cover = str(slot.get("slot") or "").strip().lower() in {"cover", "hero_cover"}
        highlights = _sanitize_copy_list(raw.get("highlights"), 4) or [
            _sanitize_copy_text(item) for item in slot.get("points", []) if _sanitize_copy_text(item)
        ][:4]
        if not highlights:
            highlights = DEFAULT_SELLING_POINTS[:3]
        if is_cover:
            highlights = [_short_overlay_phrase(item, 8) for item in highlights]
            highlights = [item for item in highlights if item][:3]
            if not highlights:
                highlights = [_short_overlay_phrase(item, 8) for item in DEFAULT_SELLING_POINTS[:3] if _short_overlay_phrase(item, 8)]
        pages.append(
            {
                "index": idx,
                "display_index": None if is_cover else max(1, idx - 1),
                "slot": slot["slot"],
                "goal": slot["goal"],
                "focus": slot["focus"],
                "title": _sanitize_copy_text(raw.get("title") or slot["focus"] or analysis.get("hero_claim") or ""),
                "subtitle": _sanitize_copy_text(raw.get("subtitle") or analysis.get("product_summary") or ""),
                "highlights": highlights,
                "badge": _sanitize_copy_text(raw.get("badge") or ""),
                "footer": _sanitize_copy_text(raw.get("footer") or default_footer),
                "image_prompt_en": str(raw.get("image_prompt_en") or "").strip(),
                "layout_hint": _sanitize_copy_text(raw.get("layout_hint") or ""),
            }
        )
    return pages


def _compose_page_background_prompt(page: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    style = str(analysis.get("visual_style") or "premium ecommerce poster").strip()
    points = ", ".join(_safe_string_list(page.get("highlights")))
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:4])
    user_hints = analysis.get("user_hints") if isinstance(analysis.get("user_hints"), dict) else {}
    slot_name = str(page.get("slot") or "").strip().lower()
    prompt_parts = [
        str(page.get("image_prompt_en") or "").strip(),
        f"Product category: {analysis.get('category') or 'product'}",
        f"Hero focus: {page.get('focus') or analysis.get('hero_claim') or analysis.get('product_name')}",
        f"Support highlights: {points}",
        f"Visual style: {style}",
        "Create a clean mobile ecommerce detail page background with strong product visibility",
        "No text, no typography, no watermark, no UI, no sticker, no subtitles",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis, include_copy_tone=True))
    if user_hints.get("product_name_hint"):
        prompt_parts.append(f"User-specified product identity: {user_hints.get('product_name_hint')}")
    if user_hints.get("product_direction_hint"):
        prompt_parts.append(f"User-specified category direction: {user_hints.get('product_direction_hint')}")
    if slot_name == "cover":
        prompt_parts.append(
            "Make it feel like a premium advertising poster cover, not a normal detail page. Prefer a lifestyle scene, aspirational usage scene, or campaign-style hero image with stronger storytelling and atmosphere. Avoid flat lay, avoid ghost mannequin, avoid plain isolated product-only composition unless absolutely necessary"
        )
    elif slot_name == "spec_table":
        prompt_parts.append(
            "Prefer a cleaner and calmer product-detail background suitable for overlaying specification cards. Use simple composition, subtle depth, and avoid busy action."
        )
    elif slot_name in {"material", "material_cleaning"}:
        prompt_parts.append(
            "Prefer close-up texture, craftsmanship detail, or cleaning-use storytelling rather than a distant full-room scene."
        )
    if constraints:
        prompt_parts.append(f"Stay conservative about uncertain claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _compose_cover_background_prompt(page: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    category = str(analysis.get("category") or "product").strip()
    product_name = str(analysis.get("product_name") or category or "product").strip()
    style = str(analysis.get("visual_style") or "premium campaign poster").strip()
    summary = str(page.get("focus") or analysis.get("hero_claim") or analysis.get("product_summary") or "").strip()
    scenes = ", ".join(_safe_string_list(analysis.get("usage_scenes"))[:2])
    points = ", ".join(_safe_string_list(page.get("highlights"))[:3])
    model_prompt = str(page.get("image_prompt_en") or "").strip()
    user_hints = analysis.get("user_hints") if isinstance(analysis.get("user_hints"), dict) else {}
    if model_prompt:
        model_prompt = re.sub(
            r"(?i)\b(flat lay|white studio background|clean white background|studio background|plain isolated product-only composition|ghost mannequin|mannequin)\b",
            "",
            model_prompt,
        )
        model_prompt = re.sub(r"\s{2,}", " ", model_prompt).strip(" ,.")
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:2])

    prompt_parts = [
        f"Premium commercial advertising poster background for {product_name}",
        f"Product category: {category}",
        f"Poster message: {summary}",
        f"Key benefits to visually support: {points}",
        f"Visual style: {style}",
        "Create a cinematic campaign hero scene rather than a standard ecommerce product page",
        "Show the product in a believable aspirational usage scenario with storytelling, atmosphere, depth, and strong visual focus",
        "Prefer a fashion advertising composition, urban winter lifestyle scene, editorial campaign photography, premium magazine poster mood, natural environment context, confident hero framing",
        "No collage, no split panels, no infographic layout, no white seamless studio background, no empty or awkwardly floating isolated-product composition, no flat lay, no mannequin, no ghost mannequin",
        "No text, no typography, no watermark, no logo, no UI, no sticker, no subtitles",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis, include_copy_tone=True))
    if scenes:
        prompt_parts.append(f"Scene inspiration: {scenes}")
    if user_hints.get("product_name_hint"):
        prompt_parts.append(f"User-specified product identity: {user_hints.get('product_name_hint')}")
    if user_hints.get("product_direction_hint"):
        prompt_parts.append(f"User-specified category direction: {user_hints.get('product_direction_hint')}")
    if model_prompt:
        prompt_parts.append(f"Reference product cues only: {model_prompt}")
    if constraints:
        prompt_parts.append(f"Do not imply unverifiable technical claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _compose_white_bg_prompt(analysis: Dict[str, Any]) -> str:
    category = str(analysis.get("category") or "product").strip()
    product_name = str(analysis.get("product_name") or category or "product").strip()
    style = str(analysis.get("visual_style") or "clean ecommerce product photography").strip()
    user_hints = analysis.get("user_hints") if isinstance(analysis.get("user_hints"), dict) else {}
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:3])
    points = ", ".join(_safe_string_list(analysis.get("selling_points"))[:3])
    prompt_parts = [
        f"Create a clean ecommerce white-background product image for {product_name}",
        f"Product category: {category}",
        f"Visual style: {style}",
        f"Keep these product cues consistent: {points}",
        "Show only the sellable product itself on a pure white background",
        "Centered full-product composition suitable for ecommerce listing white-background image",
        "Match the exact visible product colors from the reference, especially cream white, wood tones, metal tones, fabric tones, and finish sheen",
        "No room scene, no furniture scene, no props, no packaging, no extra objects, no people, no animals",
        "No text, no logo, no watermark, no infographic, no labels, no measurement lines, no collage, no UI",
        "Avoid visible cast shadows and avoid gray studio floor; keep the surrounding background clean white",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis, mode="product_only"))
    if user_hints.get("product_name_hint"):
        prompt_parts.append(f"User-specified product identity: {user_hints.get('product_name_hint')}")
    if user_hints.get("product_direction_hint"):
        prompt_parts.append(f"User-specified category direction: {user_hints.get('product_direction_hint')}")
    if constraints:
        prompt_parts.append(f"Do not imply unverifiable claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _compose_black_bg_prompt(analysis: Dict[str, Any]) -> str:
    category = str(analysis.get("category") or "product").strip()
    product_name = str(analysis.get("product_name") or category or "product").strip()
    style = str(analysis.get("visual_style") or "clean ecommerce product photography").strip()
    user_hints = analysis.get("user_hints") if isinstance(analysis.get("user_hints"), dict) else {}
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:3])
    points = ", ".join(_safe_string_list(analysis.get("selling_points"))[:3])
    prompt_parts = [
        f"Create a clean ecommerce black-background product image for {product_name}",
        f"Product category: {category}",
        f"Visual style: {style}",
        f"Keep these product cues consistent: {points}",
        "Show only the sellable product itself on a pure solid black background #000000",
        "Centered full-product composition suitable for alpha extraction and ecommerce asset recovery",
        "Match the exact visible product colors from the reference and keep the product body identical to the white-background version",
        "No room scene, no furniture scene, no props, no packaging, no extra objects, no people, no animals",
        "No text, no logo, no watermark, no infographic, no labels, no measurement lines, no collage, no UI",
        "Keep the framing, scale, pose, crop, perspective, and product edges aligned as closely as possible to the white-background version",
        "Avoid visible floor, avoid reflections, and keep the surrounding background pure black",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis, mode="product_only"))
    if user_hints.get("product_name_hint"):
        prompt_parts.append(f"User-specified product identity: {user_hints.get('product_name_hint')}")
    if user_hints.get("product_direction_hint"):
        prompt_parts.append(f"User-specified category direction: {user_hints.get('product_direction_hint')}")
    if constraints:
        prompt_parts.append(f"Do not imply unverifiable claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _normalize_generated_white_bg(image: Image.Image, width: int = 800, height: int = 800) -> Image.Image:
    fitted = _fit_contain(image.convert("RGBA"), width, height)
    canvas = Image.new("RGBA", (width, height), (255, 255, 255, 255))
    canvas.paste(fitted, (0, 0), fitted)
    return canvas.convert("RGB")


def _normalize_generated_black_bg(image: Image.Image, width: int = 800, height: int = 800) -> Image.Image:
    fitted = _fit_contain(image.convert("RGBA"), width, height)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    canvas.paste(fitted, (0, 0), fitted)
    return canvas.convert("RGB")


def _normalize_reference_cutout(
    image: Image.Image,
    *,
    width: int = 800,
    height: int = 800,
    background: Optional[tuple[int, int, int, int]] = None,
) -> Image.Image:
    fitted = _fit_contain(image.convert("RGBA"), width, height)
    if background is None:
        return fitted
    canvas = Image.new("RGBA", (width, height), background)
    canvas.alpha_composite(fitted)
    return canvas


def _derive_transparent_from_white_black_bg(white_image: Image.Image, black_image: Image.Image) -> Image.Image:
    white_rgba = white_image.convert("RGBA")
    black_rgba = black_image.convert("RGBA")
    if white_rgba.size != black_rgba.size:
        raise PipelineError("White-background and black-background images must have identical dimensions")

    width, height = white_rgba.size
    white_pixels = white_rgba.load()
    black_pixels = black_rgba.load()
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    out_pixels = out.load()
    bg_dist = math.sqrt(3 * 255 * 255)

    for y in range(height):
        for x in range(width):
            r_w, g_w, b_w, _ = white_pixels[x, y]
            r_b, g_b, b_b, _ = black_pixels[x, y]
            pixel_dist = math.sqrt(
                float((r_w - r_b) ** 2 + (g_w - g_b) ** 2 + (b_w - b_b) ** 2)
            )
            alpha = 1.0 - (pixel_dist / bg_dist)
            alpha = max(0.0, min(1.0, alpha))

            if alpha <= 0.01:
                out_pixels[x, y] = (0, 0, 0, 0)
                continue

            r_out = min(255, max(0, round(r_b / alpha)))
            g_out = min(255, max(0, round(g_b / alpha)))
            b_out = min(255, max(0, round(b_b / alpha)))
            out_pixels[x, y] = (r_out, g_out, b_out, round(alpha * 255))
    return out


def _download_image(url: str) -> Image.Image:
    last: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            response = requests.get(url, timeout=180)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            return image.convert("RGB") if image.mode != "RGB" else image
        except Exception as exc:
            last = exc
            if attempt >= 3:
                break
            time.sleep(attempt * 2)
    raise PipelineError(f"Failed to download image after retries: {last}")


def _fit_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize(
        (int(math.ceil(image.width * scale)), int(math.ceil(image.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    return mask


def _font(size: int, bold: bool = False):
    candidates: List[Path] = []
    if os.name == "nt":
        win = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        if bold:
            candidates.extend([win / "msyhbd.ttc", win / "simhei.ttf"])
        candidates.extend([win / "msyh.ttc", win / "simhei.ttf", win / "simsun.ttc"])
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    try:
        return ImageFont.truetype("arial.ttf", size=size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: Any, max_width: int, max_lines: int) -> List[str]:
    source = str(text or "").strip()
    if not source:
        return []

    lines: List[str] = []
    current = ""
    for ch in source:
        candidate = current + ch
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = ch
        if len(lines) >= max_lines - 1:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and "".join(lines) != source:
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return [line for line in lines if line.strip()]


def _split_text_by_chars(text: str, max_chars_per_line: int, max_lines: int) -> List[str]:
    source = str(text or "").strip()
    if not source or max_chars_per_line <= 0 or max_lines <= 0:
        return []
    visible_limit = max_chars_per_line * max_lines
    clipped = source[:visible_limit]
    lines = [clipped[idx : idx + max_chars_per_line] for idx in range(0, len(clipped), max_chars_per_line)]
    lines = lines[:max_lines]
    if len(source) > visible_limit and lines:
        ellipsis_room = max(1, max_chars_per_line - 3)
        lines[-1] = (lines[-1][:ellipsis_room] if ellipsis_room < len(lines[-1]) else lines[-1]).rstrip(" .") + "..."
    return [line for line in lines if line.strip()]


def _fit_font_to_lines(
    draw: ImageDraw.ImageDraw,
    lines: List[str],
    *,
    initial_size: int,
    min_size: int,
    max_width: int,
    bold: bool = False,
) -> Any:
    size = max(min_size, initial_size)
    while size > min_size:
        candidate_font = _font(size, bold=bold)
        too_wide = False
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=candidate_font)
            if (bbox[2] - bbox[0]) > max_width:
                too_wide = True
                break
        if not too_wide:
            return candidate_font
        size -= 4
    return _font(min_size, bold=bold)


def _draw_line_list(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    lines: List[str],
    font: Any,
    fill: str,
    spacing: int,
) -> int:
    x, y = pos
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((x, y), line, font=font)
        y = bbox[3] + spacing
    return y


def _draw_multiline(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: Any,
    fill: str,
    max_width: int,
    max_lines: int,
    spacing: int,
) -> int:
    x, y = pos
    lines = _wrap(draw, text, font, max_width, max_lines)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((x, y), line, font=font)
        y = bbox[3] + spacing
    return y


def _draw_multiline_with_shadow(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: Any,
    fill: str,
    shadow_fill: tuple[int, int, int, int] | str,
    max_width: int,
    max_lines: int,
    spacing: int,
    shadow_offset: tuple[int, int] = (0, 4),
) -> int:
    x, y = pos
    sx, sy = shadow_offset
    lines = _wrap(draw, text, font, max_width, max_lines)
    for line in lines:
        draw.text((x + sx, y + sy), line, font=font, fill=shadow_fill)
        draw.text((x, y), line, font=font, fill=fill)
        bbox = draw.textbbox((x, y), line, font=font)
        y = bbox[3] + spacing
    return y


def _draw_multiline_stroked(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: Any,
    fill: str,
    stroke_fill: str,
    stroke_width: int,
    max_width: int,
    max_lines: int,
    spacing: int,
) -> int:
    x, y = pos
    lines = _wrap(draw, text, font, max_width, max_lines)
    for line in lines:
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )
        bbox = draw.textbbox((x, y), line, font=font, stroke_width=stroke_width)
        y = bbox[3] + spacing
    return y


def _wrapped_text_height(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    max_width: int,
    max_lines: int,
    spacing: int,
) -> tuple[List[str], int]:
    lines = _wrap(draw, text, font, max_width, max_lines)
    if not lines:
        return [], 0
    total_height = 0
    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        total_height += bbox[3] - bbox[1]
        if idx < len(lines) - 1:
            total_height += spacing
    return lines, total_height


def _scale_int(value: int | float, scale: float, minimum: int = 1) -> int:
    return max(minimum, int(round(float(value) * scale)))


def _page_dimensions(page: Dict[str, Any], config: PipelineConfig) -> tuple[int, int]:
    common = _template_common(config)
    width = max(720, int(common.get("width") or config.page_width or 790))
    template_variant = _template_variant(page, config)
    if isinstance(page.get("height"), int) and int(page.get("height")) > 0:
        return width, int(page["height"])
    template = config.template_config if isinstance(config.template_config, dict) else {}
    heights = template.get("heights")
    heights_map = dict(heights) if isinstance(heights, dict) else {}
    base_height = heights_map.get(template_variant) or int(common.get("default_height") or config.page_height or 1250)
    min_height = int(common.get("min_height") or 640)
    return width, max(min_height, int(base_height))


def _render_page(
    product_image: Image.Image,
    background: Image.Image,
    page: Dict[str, Any],
    config: PipelineConfig,
    icon_images: Optional[Dict[str, Image.Image]] = None,
) -> Image.Image:
    width, height = _page_dimensions(page, config)
    scale = width / 1242.0
    common = _template_common(config)
    colors = _template_colors(config)
    spec_table_layout = _template_section(config, "spec_table")
    margin = _scale_int(int(common.get("margin") or 64), scale, 24)
    accent = colors.get("accent", "#ff6a1a")
    dark = colors.get("dark", "#1d1713")
    muted = colors.get("muted", "#5b5349")
    canvas_color = colors.get("canvas", "#faf8f5")
    card_color = colors.get("card", "#f4f4f2")
    slot_name = str(page.get("slot") or "").strip().lower()
    metadata = page.get("metadata") if isinstance(page.get("metadata"), dict) else {}
    icon_images = icon_images or {}
    icon_id = str(metadata.get("icon") or "").strip().lower()

    canvas = Image.new("RGB", (width, height), canvas_color)
    draw = ImageDraw.Draw(canvas)

    title_font = _font(_scale_int(78, scale, 26), bold=True)
    subtitle_font = _font(_scale_int(34, scale, 16))
    bullet_font = _font(_scale_int(32, scale, 16))
    badge_font = _font(_scale_int(28, scale, 14), bold=True)
    slot_font = _font(_scale_int(30, scale, 14), bold=True)
    number_font = _font(_scale_int(86, scale, 26), bold=True)
    footer_font = _font(_scale_int(30, scale, 14), bold=True)

    slot_text = str(page.get("slot") or "").upper()
    is_cover = slot_text == "COVER"

    if slot_name == "spec_table":
        title_bottom = _draw_multiline(
            draw,
            (margin, _scale_int(88, scale, 28)),
            str(page.get("title") or ""),
            title_font,
            dark,
            width - margin * 2,
            2,
            _scale_int(10, scale, 4),
        )
        subtitle_bottom = _draw_multiline(
            draw,
            (margin, title_bottom + _scale_int(8, scale, 4)),
            str(page.get("subtitle") or ""),
            subtitle_font,
            muted,
            width - margin * 2,
            2,
            _scale_int(8, scale, 4),
        )
        spec_entries = metadata.get("spec_entries") if isinstance(metadata.get("spec_entries"), list) else []
        if not spec_entries:
            spec_entries = [{"key": "", "value": item} for item in _safe_string_list(page.get("highlights"))[:6]]
        columns = max(1, int(spec_table_layout.get("columns") or 2))
        card_gap_x = _scale_int(int(spec_table_layout.get("card_gap_x") or 18), scale, 10)
        card_gap_y = _scale_int(int(spec_table_layout.get("card_gap_y") or 18), scale, 10)
        card_w = (width - margin * 2 - card_gap_x * (columns - 1)) // columns
        card_h = _scale_int(int(spec_table_layout.get("card_height") or 160), scale, 84)
        card_x = margin
        card_y = subtitle_bottom + _scale_int(28, scale, 12)
        card_radius = _scale_int(int(spec_table_layout.get("card_radius") or 28), scale, 14)
        key_font = _font(_scale_int(28, scale, 15), bold=True)
        value_font = _font(_scale_int(26, scale, 14))
        for idx, item in enumerate(spec_entries[:6]):
            if idx and idx % columns == 0:
                card_x = margin
                card_y += card_h + card_gap_y
            elif idx:
                card_x += card_w + card_gap_x
            draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=card_radius, fill=card_color)
            key = _sanitize_copy_text(item.get("key") or "")
            value = _sanitize_copy_text(item.get("value") or "")
            key_bottom = _draw_multiline(
                draw,
                (card_x + _scale_int(28, scale, 14), card_y + _scale_int(24, scale, 12)),
                key,
                key_font,
                dark,
                card_w - _scale_int(56, scale, 28),
                2,
                _scale_int(6, scale, 2),
            )
            _draw_multiline(
                draw,
                (card_x + _scale_int(28, scale, 14), key_bottom + _scale_int(10, scale, 4)),
                value,
                value_font,
                muted,
                card_w - _scale_int(56, scale, 28),
                2,
                _scale_int(6, scale, 2),
            )
        return canvas

    if is_cover:
        title_font = _font(_scale_int(96, scale, 34), bold=True)
        highlight_font = _font(_scale_int(34, scale, 16), bold=True)
        footer_font = _font(_scale_int(34, scale, 16), bold=True)

        hero = _fit_cover(background, width, height)
        canvas.paste(hero, (0, 0))

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle((0, 0, width, height), fill=(12, 10, 8, 28))
        overlay_draw.rectangle((0, 0, width, height // 2), fill=(0, 0, 0, 10))
        overlay_draw.rectangle((0, height - _scale_int(520, scale, 240), width, height), fill=(14, 10, 8, 108))
        canvas.paste(overlay, (0, 0), overlay)

        title_y = _scale_int(110, scale, 42)

        title_bottom = _draw_multiline(
            draw,
            (margin, title_y),
            str(page.get("title") or ""),
            title_font,
            "#fffaf4",
            width - margin * 2 - _scale_int(40, scale, 12),
            2,
            _scale_int(14, scale, 6),
        )
        subtitle_bottom = title_bottom

        footer_text = str(page.get("footer") or "").strip()
        if footer_text:
            footer_bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
            footer_w = min(width - margin * 2, footer_bbox[2] - footer_bbox[0] + _scale_int(96, scale, 40))
            footer_y = max(subtitle_bottom + _scale_int(32, scale, 14), _scale_int(360, scale, 140))
            footer_h = _scale_int(76, scale, 34)
            draw.rounded_rectangle((margin, footer_y, margin + footer_w, footer_y + footer_h), radius=_scale_int(34, scale, 14), fill=(255, 245, 235))
            draw.text((margin + _scale_int(38, scale, 16), footer_y + _scale_int(17, scale, 8)), footer_text, font=footer_font, fill=accent)

        cols = _safe_string_list(page.get("highlights"))[:3]
        chip_x = margin
        chip_y = height - _scale_int(326, scale, 144)
        chip_gap = _scale_int(18, scale, 8)
        for idx2, text in enumerate(cols):
            if not text:
                continue
            lines, text_h = _wrapped_text_height(draw, text, highlight_font, _scale_int(390, scale, 200), 3, _scale_int(6, scale, 2))
            text_width = 0
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=highlight_font)
                text_width = max(text_width, bbox[2] - bbox[0])
            card_w = min(_scale_int(440, scale, 220), text_width + _scale_int(84, scale, 34))
            card_h = max(_scale_int(72, scale, 38), text_h + _scale_int(34, scale, 16))
            if chip_x + card_w > width - margin:
                chip_x = margin
                chip_y += card_h + chip_gap
            draw.rounded_rectangle((chip_x, chip_y, chip_x + card_w, chip_y + card_h), radius=_scale_int(30, scale, 14), fill=(255, 248, 240, 236))
            draw.ellipse((chip_x + _scale_int(22, scale, 10), chip_y + _scale_int(25, scale, 12), chip_x + _scale_int(38, scale, 18), chip_y + _scale_int(41, scale, 20)), fill=accent)
            _draw_multiline(draw, (chip_x + _scale_int(52, scale, 22), chip_y + _scale_int(15, scale, 8)), text, highlight_font, dark, card_w - _scale_int(70, scale, 30), 3, _scale_int(6, scale, 2))
            chip_x += card_w + chip_gap
        return canvas

    title_bottom = _draw_multiline(draw, (margin, _scale_int(76, scale, 28)), str(page.get("title") or ""), title_font, dark, width - margin * 2, 2, _scale_int(10, scale, 4))
    subtitle_bottom = _draw_multiline(
        draw,
        (margin, title_bottom + _scale_int(8, scale, 4)),
        str(page.get("subtitle") or ""),
        subtitle_font,
        muted,
        width - margin * 2,
        2,
        _scale_int(8, scale, 3),
    )

    hero_top = subtitle_bottom + _scale_int(18, scale, 8)
    hero_w = width
    hero_h = max(1, height - hero_top)
    hero = _fit_cover(background, hero_w, hero_h)
    canvas.paste(hero, (0, hero_top))

    if icon_id and icon_id in icon_images and slot_name in {"feature", "material", "scene", "trust"}:
        icon_size = _scale_int(86, scale, 40)
        icon_panel_size = icon_size + _scale_int(28, scale, 12)
        icon_panel_x = margin + _scale_int(24, scale, 10)
        icon_panel_y = hero_top + _scale_int(24, scale, 10)
        draw.rounded_rectangle(
            (icon_panel_x, icon_panel_y, icon_panel_x + icon_panel_size, icon_panel_y + icon_panel_size),
            radius=_scale_int(26, scale, 10),
            fill=(255, 248, 240),
        )
        icon_image = _fit_contain(icon_images[icon_id], icon_size, icon_size)
        icon_x = icon_panel_x + (icon_panel_size - icon_size) // 2
        icon_y = icon_panel_y + (icon_panel_size - icon_size) // 2
        canvas.paste(icon_image, (icon_x, icon_y), icon_image)

    footer_text = str(page.get("footer") or "").strip()
    if footer_text and is_cover:
        footer_y = height - _scale_int(92, scale, 42)
        footer_bbox = draw.textbbox((0, 0), footer_text, font=footer_font)
        footer_w = min(width - margin * 2, footer_bbox[2] - footer_bbox[0] + _scale_int(80, scale, 30))
        draw.rounded_rectangle((margin, footer_y, margin + footer_w, footer_y + _scale_int(62, scale, 26)), radius=_scale_int(31, scale, 12), fill=accent)
        draw.text((margin + _scale_int(34, scale, 12), footer_y + _scale_int(14, scale, 6)), footer_text, font=footer_font, fill="#ffffff")

    return canvas


def _compose_long_image(page_paths: List[str], output_path: str, page_gap_px: int) -> Dict[str, Any]:
    if not page_paths:
        raise PipelineError("No page images to compose")
    images = [Image.open(path).convert("RGB") for path in page_paths]
    width = max(img.width for img in images)
    effective_gap = max(0, int(page_gap_px))
    height = sum(img.height for img in images) + effective_gap * (len(images) - 1)
    bg_color = images[0].getpixel((0, 0))
    canvas = Image.new("RGB", (width, height), bg_color)
    y = 0
    for idx, image in enumerate(images):
        canvas.paste(image, (0, y))
        y += image.height
        if idx < len(images) - 1:
            y += effective_gap
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, format="JPEG", quality=92, subsampling=0)
    return {"path": str(out), "width": width, "height": height, "page_count": len(images)}


def _write_preview_html(
    *,
    run_dir: Path,
    detail_dir: Path,
    analysis: Dict[str, Any],
    page_results: List[Dict[str, Any]],
    long_image: Dict[str, Any],
) -> str:
    product_name = html.escape(str(analysis.get("product_name") or "商品详情预览"))
    category = html.escape(str(analysis.get("category") or ""))
    hero_claim = html.escape(str(analysis.get("hero_claim") or ""))
    summary = html.escape(str(analysis.get("product_summary") or ""))
    long_rel = ""
    long_path_raw = str(long_image.get("path") or "").strip()
    if long_path_raw:
        long_path = Path(long_path_raw)
        try:
            long_rel = str(long_path.resolve().relative_to(run_dir.resolve())).replace("\\", "/")
        except Exception:
            long_rel = long_path.name

    cards: List[str] = []
    for item in sorted(page_results, key=lambda x: int(x.get("index") or 0)):
        rel = html.escape(str(item.get("relative_path") or item.get("filename") or "").replace("\\", "/"))
        title = html.escape(str(item.get("title") or item.get("slot") or "详情页"))
        slot = html.escape(str(item.get("slot") or ""))
        size = html.escape(f'{int(item.get("width") or 0)} x {int(item.get("height") or 0)}')
        cards.append(
            f"""
            <article class="card">
              <div class="card-meta">
                <span class="index">#{int(item.get("index") or 0):02d}</span>
                <span class="slot">{slot}</span>
                <span class="size">{size}</span>
              </div>
              <h3>{title}</h3>
              <img src="{rel}" alt="{title}" loading="lazy">
              <a class="link" href="{rel}" target="_blank" rel="noreferrer">打开原图</a>
            </article>
            """.strip()
        )

    long_button = (
        f'<a class="button" href="{html.escape(long_rel)}" target="_blank" rel="noreferrer">打开整张长图</a>'
        if long_rel
        else ""
    )
    detail_rel = html.escape(str(detail_dir.resolve().relative_to(run_dir.resolve())).replace("\\", "/"))
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{product_name}</title>
  <style>
    :root {{
      --bg: #f6f2eb;
      --panel: #fffdf9;
      --line: #e7ddd1;
      --text: #241c17;
      --muted: #786a5b;
      --accent: #ff6a1a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
      background: linear-gradient(180deg, #f8f3ec 0%, #f2ede5 100%);
      color: var(--text);
    }}
    .wrap {{
      width: min(1200px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 32px 0 56px;
    }}
    .hero {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 28px;
      box-shadow: 0 18px 40px rgba(58, 40, 20, 0.08);
    }}
    .eyebrow {{
      display: inline-block;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(255, 106, 26, 0.12);
      color: var(--accent);
      font-weight: 700;
      font-size: 14px;
    }}
    h1 {{
      margin: 16px 0 8px;
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1.15;
    }}
    .summary {{
      margin: 0;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.65;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 18px;
    }}
    .button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 0 16px;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      text-decoration: none;
      font-weight: 700;
    }}
    .button.secondary {{
      background: white;
      color: var(--text);
      border: 1px solid var(--line);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 18px;
      margin-top: 24px;
    }}
    .card {{
      background: rgba(255,255,255,0.88);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 16px;
      box-shadow: 0 16px 36px rgba(58, 40, 20, 0.06);
    }}
    .card-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 10px;
    }}
    .card-meta span {{
      border-radius: 999px;
      background: #f4eee6;
      padding: 4px 8px;
    }}
    .card h3 {{
      margin: 0 0 12px;
      font-size: 16px;
      line-height: 1.45;
    }}
    .card img {{
      width: 100%;
      display: block;
      border-radius: 16px;
      border: 1px solid #efe5da;
      background: #faf8f5;
    }}
    .link {{
      display: inline-block;
      margin-top: 12px;
      color: var(--accent);
      font-weight: 700;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <span class="eyebrow">详情图预览</span>
      <h1>{product_name}</h1>
      <p class="summary">{category} {hero_claim}</p>
      <p class="summary">{summary}</p>
      <div class="actions">
        {long_button}
        <a class="button secondary" href="{detail_rel}/" target="_blank" rel="noreferrer">打开详情目录</a>
      </div>
    </section>
    <section class="grid">
      {"".join(cards)}
    </section>
  </main>
</body>
</html>
"""
    out = run_dir / "preview.html"
    out.write_text(html_text, encoding="utf-8")
    return str(out)


def _write_delivery_archive(*, run_dir: Path, archive_name: str = "detail_delivery.zip") -> str:
    archive_path = run_dir / archive_name
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(run_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.resolve() == archive_path.resolve():
                continue
            zf.write(file_path, arcname=str(file_path.relative_to(run_dir)).replace("\\", "/"))
    return str(archive_path)


def _target_enabled(output_targets: Optional[Dict[str, Any]], key: str) -> bool:
    if not isinstance(output_targets, dict):
        return True
    return output_targets.get(key) is not False


def _relative_to_run(run_dir: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(run_dir.resolve())).replace("\\", "/")
    except Exception:
        return path.name


def _open_local_image(path: str) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA")


def _save_cover_jpeg(source: Image.Image, output_path: Path, width: int, height: int) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _fit_cover(source.convert("RGB"), width, height)
    rendered.save(output_path, format="JPEG", quality=92, subsampling=0)
    return {"filename": output_path.name, "path": str(output_path), "width": width, "height": height}


def _save_contain_frame_jpeg(
    source: Image.Image,
    output_path: Path,
    width: int,
    height: int,
    *,
    padding_ratio: float = 0.04,
    blur_radius: int = 36,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_rgba = source.convert("RGBA")
    background = _fit_cover(source_rgba.convert("RGB"), width, height).filter(ImageFilter.GaussianBlur(radius=blur_radius))
    canvas = background.convert("RGBA")
    wash = Image.new("RGBA", (width, height), (255, 255, 255, 42))
    canvas.alpha_composite(wash)

    pad_x = max(12, int(round(width * padding_ratio)))
    pad_y = max(12, int(round(height * padding_ratio)))
    contain_w = max(1, width - pad_x * 2)
    contain_h = max(1, height - pad_y * 2)
    fitted = _fit_contain(source_rgba, contain_w, contain_h)
    canvas.paste(fitted, (pad_x, pad_y), fitted)
    canvas.convert("RGB").save(output_path, format="JPEG", quality=92, subsampling=0)
    return {"filename": output_path.name, "path": str(output_path), "width": width, "height": height}


def _save_contain_product(
    source: Image.Image,
    output_path: Path,
    width: int,
    height: int,
    *,
    background: Optional[tuple[int, int, int, int]] = None,
    padding_ratio: float = 0.08,
) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pad_x = max(12, int(round(width * padding_ratio)))
    pad_y = max(12, int(round(height * padding_ratio)))
    contain_w = max(1, width - pad_x * 2)
    contain_h = max(1, height - pad_y * 2)
    fitted = _fit_contain(source.convert("RGBA"), contain_w, contain_h)
    if background is None:
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.paste(fitted, (pad_x, pad_y), fitted)
        canvas.save(output_path, format="PNG")
    else:
        canvas = Image.new("RGBA", (width, height), background)
        canvas.paste(fitted, (pad_x, pad_y), fitted)
        canvas.convert("RGB").save(output_path, format="JPEG", quality=92, subsampling=0)
    return {"filename": output_path.name, "path": str(output_path), "width": width, "height": height}


def _image_has_real_transparency(image: Image.Image) -> bool:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    extrema = alpha.getextrema()
    return bool(isinstance(extrema, tuple) and extrema[0] < 255)


def _generate_white_bg_assets(
    *,
    client: ComflyClient,
    logger: RunLogger,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    reference_urls: List[str],
) -> Dict[str, Any]:
    prompt = _compose_white_bg_prompt(analysis)
    warnings: List[str] = []
    try:
        generated, attempts = client.generate_image(
            config.image_model,
            prompt,
            "1:1",
            reference_urls,
            "06_white_bg_source",
        )
        logger.record_usage(
            "image",
            config.image_model,
            "white_bg_source",
            payload={"attempts": attempts, "aspect_ratio": "1:1"},
        )
        white_bg_source = _download_image(generated["url"], retries=5, timeout=180)
        white_bg_image = _normalize_generated_white_bg(white_bg_source, 800, 800)
        generation_mode = "remote"
        generated_url = generated["url"]
    except Exception as exc:
        white_bg_image = _normalize_generated_white_bg(fallback_product_image.convert("RGB"), 800, 800)
        generation_mode = "fallback"
        generated_url = None
        attempts = 0
        warnings.append(f"白底图模型生成失败，已退回本地保底方案: {exc}")
    transparent_image = _derive_transparent_from_white_bg(white_bg_image)
    return {
        "white_bg_image": white_bg_image,
        "transparent_image": transparent_image,
        "generation_mode": generation_mode,
        "generated_image_url": generated_url,
        "prompt": prompt,
        "attempts": attempts,
        "warnings": warnings,
    }


def _export_suite_bundle(
    *,
    run_dir: Path,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    product_image_rgba: Image.Image,
    page_results: List[Dict[str, Any]],
    white_bg_assets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root_dir = run_dir / SUITE_EXPORT_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    category_dirs = {key: root_dir / dirname for key, dirname in SUITE_EXPORT_CATEGORY_DIRS.items()}
    for folder in category_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(page_results, key=lambda item: int(item.get("index") or 0))
    page_by_slot: Dict[str, Dict[str, Any]] = {}
    for item in sorted_pages:
        slot = str(item.get("slot") or "").strip().lower()
        if slot and slot not in page_by_slot:
            page_by_slot[slot] = item

    def pick_page(*slots: str) -> Dict[str, Any]:
        for slot in slots:
            found = page_by_slot.get(slot.strip().lower())
            if found:
                return found
        return sorted_pages[0]

    cover_page = pick_page("cover", "overview")
    overview_page = pick_page("overview", "feature", "cover")
    sku_layout_page = pick_page("spec_table", "material", "trust", "overview", "feature")
    material_page = pick_page("cover", "scene", "overview")

    suite_categories: Dict[str, List[Dict[str, Any]]] = {
        "main_images": [],
        "sku_images": [],
        "transparent_white_bg": [],
        "detail_images": [],
        "material_images": [],
        "showcase_images": [],
    }
    warnings: List[str] = []

    cover_rgba = _open_local_image(str(cover_page["local_path"]))
    overview_rgba = _open_local_image(str(overview_page["local_path"]))
    sku_layout_rgba = _open_local_image(str(sku_layout_page["local_path"]))
    material_rgba = _open_local_image(str(material_page["local_path"]))
    white_bg_payload = white_bg_assets if isinstance(white_bg_assets, dict) else {}
    white_bg_image = white_bg_payload.get("white_bg_image") if isinstance(white_bg_payload.get("white_bg_image"), Image.Image) else None
    transparent_image = white_bg_payload.get("transparent_image") if isinstance(white_bg_payload.get("transparent_image"), Image.Image) else None
    warnings.extend(_safe_string_list(white_bg_payload.get("warnings")))

    if _target_enabled(config.output_targets, "main_images"):
        suite_categories["main_images"].append(
            _save_contain_frame_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1440.jpg", 1440, 1440)
        )
        suite_categories["main_images"].append(
            _save_contain_frame_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1920.jpg", 1440, 1920)
        )

    if False and _target_enabled(config.output_targets, "sku_images"):
        suite_categories["sku_images"].append(
            _save_contain_frame_jpeg(overview_rgba, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
        )
        suite_categories["sku_images"].append(
            _save_contain_frame_jpeg(sku_layout_rgba, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
        )

    if _target_enabled(config.output_targets, "transparent_image") or _target_enabled(config.output_targets, "white_bg_image"):
        transparent_placeholder = not bool(transparent_image and _image_has_real_transparency(transparent_image))
        if transparent_placeholder:
            warnings.append("当前透明图未能稳定获得真实透明通道，已使用保底方式导出。")
        if _target_enabled(config.output_targets, "white_bg_image"):
            white_bg_source = (
                white_bg_image
                if isinstance(white_bg_image, Image.Image)
                else _normalize_generated_white_bg(product_image_rgba.convert("RGB"), 800, 800)
            )
            white_bg = _save_cover_jpeg(
                white_bg_source,
                category_dirs["transparent_white_bg"] / "1-白底.jpg",
                800,
                800,
            )
            white_bg["kind"] = "white_bg_image"
            white_bg["placeholder"] = transparent_placeholder
            white_bg["generation_mode"] = str(white_bg_payload.get("generation_mode") or ("fallback" if transparent_placeholder else "remote"))
            white_bg["generated_image_url"] = str(white_bg_payload.get("generated_image_url") or "")
            white_bg["prompt"] = str(white_bg_payload.get("prompt") or "")
            suite_categories["transparent_white_bg"].append(white_bg)
        if _target_enabled(config.output_targets, "transparent_image"):
            transparent_source = transparent_image if isinstance(transparent_image, Image.Image) else product_image_rgba
            transparent_path = category_dirs["transparent_white_bg"] / "1-透明.png"
            transparent_path.parent.mkdir(parents=True, exist_ok=True)
            transparent_source.convert("RGBA").save(transparent_path, format="PNG")
            transparent = {
                "filename": transparent_path.name,
                "path": str(transparent_path),
                "width": transparent_source.width,
                "height": transparent_source.height,
            }
            transparent["kind"] = "transparent_image"
            transparent["placeholder"] = transparent_placeholder
            transparent["source"] = "white_bg_generated" if isinstance(transparent_image, Image.Image) else "input_fallback"
            suite_categories["transparent_white_bg"].append(transparent)

    if _target_enabled(config.output_targets, "detail_pages"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(
                source,
                category_dirs["detail_images"] / f"详情_{idx:02d}.jpg",
                source.width,
                source.height,
            )
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["detail_images"].append(exported)

    if _target_enabled(config.output_targets, "material_images"):
        material_source = main_portrait_image if isinstance(main_portrait_image, Image.Image) else (
            sku_scene_image if isinstance(sku_scene_image, Image.Image) else material_rgba
        )
        material_source_kind = (
            "generated_main_image"
            if isinstance(main_portrait_image, Image.Image)
            else ("generated_sku_image" if isinstance(sku_scene_image, Image.Image) else "detail_page")
        )
        for width, height in ((513, 750), (800, 1200), (900, 1200)):
            exported = _save_cover_jpeg(material_source, category_dirs["material_images"] / f"{width}X{height}.jpg", width, height)
            exported["kind"] = "material_image"
            exported["source"] = material_source_kind
            suite_categories["material_images"].append(exported)

    if False and _target_enabled(config.output_targets, "showcase_images"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_contain_frame_jpeg(source, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["showcase_images"].append(exported)

    if _target_enabled(config.output_targets, "showcase_images"):
        showcase_records = _showcase_copy_records(analysis, len(sorted_pages))
        for idx, record in enumerate(showcase_records, start=1):
            rendered = _render_showcase_card(index=idx - 1, record=record, source_pool=showcase_pool, analysis=analysis, width=1440, height=1920)
            exported = _save_cover_jpeg(rendered, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = idx
            exported["slot"] = "showcase_card"
            exported["kind"] = "showcase_image"
            exported["source"] = "local_showcase_layout"
            exported["title"] = record.get("title") or ""
            suite_categories["showcase_images"].append(exported)

    categories_payload: Dict[str, Any] = {}
    for key, items in suite_categories.items():
        folder = category_dirs[key]
        categories_payload[key] = {
            "dirname": folder.name,
            "dir": str(folder),
            "count": len(items),
            "items": [{**item, "relative_path": _relative_to_run(run_dir, Path(str(item["path"])))} for item in items],
        }

    bundle = {
        "preset": SUITE_EXPORT_PRESET,
        "root_dir": str(root_dir),
        "root_relative_path": _relative_to_run(run_dir, root_dir),
        "categories": categories_payload,
        "main_image_assets": {
            "variants": {
                key: {
                    "aspect_ratio": str(value.get("aspect_ratio") or ""),
                    "generated_image_url": str(value.get("generated_image_url") or ""),
                    "prompt": str(value.get("prompt") or ""),
                    "master": (
                        {
                            **dict(value.get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((value.get("master") or {}).get("path") or ""))),
                        }
                        if isinstance(value, dict) and isinstance(value.get("master"), dict)
                        else None
                    ),
                }
                for key, value in ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).items()
                if isinstance(value, dict)
            }
        },
        "sku_assets": {
            "scene": {
                "aspect_ratio": str(sku_scene.get("aspect_ratio") or ""),
                "generated_image_url": str(sku_scene.get("generated_image_url") or ""),
                "prompt": str(sku_scene.get("prompt") or ""),
                "master": (
                    {
                        **dict(sku_scene.get("master") or {}),
                        "relative_path": _relative_to_run(run_dir, Path(str((sku_scene.get("master") or {}).get("path") or ""))),
                    }
                    if isinstance(sku_scene.get("master"), dict)
                    else None
                ),
            },
            "layout": {
                "aspect_ratio": str(sku_layout.get("aspect_ratio") or ""),
                "generated_image_url": str(sku_layout.get("generated_image_url") or ""),
                "prompt": str(sku_layout.get("prompt") or ""),
                "master": (
                    {
                        **dict(sku_layout.get("master") or {}),
                        "relative_path": _relative_to_run(run_dir, Path(str((sku_layout.get("master") or {}).get("path") or ""))),
                    }
                    if isinstance(sku_layout.get("master"), dict)
                    else None
                ),
            },
        },
        "style_preset": (
            {
                "style_id": str((analysis.get("style_preset") or {}).get("style_id") or analysis.get("style_id") or ""),
                "display_name": str((analysis.get("style_preset") or {}).get("display_name") or ""),
                "palette": _safe_string_list((analysis.get("style_preset") or {}).get("palette")),
                "materials": _safe_string_list((analysis.get("style_preset") or {}).get("materials")),
            }
            if isinstance(analysis.get("style_preset"), dict) or analysis.get("style_id")
            else None
        ),
        "summary": {
            "product_name": str(analysis.get("product_name") or ""),
            "hero_claim": str(analysis.get("hero_claim") or ""),
            "sku": config.sku,
            "style_id": str(analysis.get("style_id") or ""),
        },
        "warnings": warnings,
    }
    (root_dir / "suite_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle

    root_dir = run_dir / SUITE_EXPORT_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    category_dirs = {key: root_dir / dirname for key, dirname in SUITE_EXPORT_CATEGORY_DIRS.items()}
    for folder in category_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(page_results, key=lambda item: int(item.get("index") or 0))
    page_by_slot: Dict[str, Dict[str, Any]] = {}
    for item in sorted_pages:
        slot = str(item.get("slot") or "").strip().lower()
        if slot and slot not in page_by_slot:
            page_by_slot[slot] = item

    def pick_page(*slots: str) -> Dict[str, Any]:
        for slot in slots:
            found = page_by_slot.get(slot.strip().lower())
            if found:
                return found
        return sorted_pages[0]

    cover_page = pick_page("cover", "overview")
    overview_page = pick_page("overview", "feature", "cover")
    sku_layout_page = pick_page("spec_table", "material", "trust", "overview", "feature")
    material_page = pick_page("cover", "scene", "overview")

    suite_categories: Dict[str, List[Dict[str, Any]]] = {
        "main_images": [],
        "sku_images": [],
        "transparent_white_bg": [],
        "detail_images": [],
        "material_images": [],
        "showcase_images": [],
    }
    warnings: List[str] = []

    cover_rgba = _open_local_image(str(cover_page["local_path"]))
    overview_rgba = _open_local_image(str(overview_page["local_path"]))
    sku_layout_rgba = _open_local_image(str(sku_layout_page["local_path"]))
    material_rgba = _open_local_image(str(material_page["local_path"]))
    white_bg_payload = white_bg_assets if isinstance(white_bg_assets, dict) else {}
    white_bg_image = white_bg_payload.get("white_bg_image") if isinstance(white_bg_payload.get("white_bg_image"), Image.Image) else None
    transparent_image = white_bg_payload.get("transparent_image") if isinstance(white_bg_payload.get("transparent_image"), Image.Image) else None
    warnings.extend(_safe_string_list(white_bg_payload.get("warnings")))

    if _target_enabled(config.output_targets, "main_images"):
        suite_categories["main_images"].append(
            _save_contain_frame_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1440.jpg", 1440, 1440)
        )
        suite_categories["main_images"].append(
            _save_contain_frame_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1920.jpg", 1440, 1920)
        )

    if _target_enabled(config.output_targets, "sku_images"):
        suite_categories["sku_images"].append(
            _save_cover_jpeg(overview_rgba, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
        )
        suite_categories["sku_images"].append(
            _save_cover_jpeg(sku_layout_rgba, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
        )

    if _target_enabled(config.output_targets, "transparent_image") or _target_enabled(config.output_targets, "white_bg_image"):
        transparent_placeholder = not _image_has_real_transparency(product_image_rgba)
        if transparent_placeholder:
            warnings.append("输入主图不含真实透明通道，当前透明图为占位导出，后续仍需接入抠图能力。")
        if _target_enabled(config.output_targets, "white_bg_image"):
            white_bg = _save_contain_product(
                product_image_rgba,
                category_dirs["transparent_white_bg"] / "1-白底.jpg",
                800,
                800,
                background=(255, 255, 255, 255),
            )
            white_bg["kind"] = "white_bg_image"
            white_bg["placeholder"] = transparent_placeholder
            suite_categories["transparent_white_bg"].append(white_bg)
        if _target_enabled(config.output_targets, "transparent_image"):
            transparent = _save_contain_product(
                product_image_rgba,
                category_dirs["transparent_white_bg"] / "1-透明.png",
                800,
                800,
                background=None,
            )
            transparent["kind"] = "transparent_image"
            transparent["placeholder"] = transparent_placeholder
            suite_categories["transparent_white_bg"].append(transparent)

    if _target_enabled(config.output_targets, "detail_pages"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(
                source,
                category_dirs["detail_images"] / f"详情_{idx:02d}.jpg",
                source.width,
                source.height,
            )
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["detail_images"].append(exported)

    if _target_enabled(config.output_targets, "material_images"):
        for width, height in ((513, 750), (800, 1200), (900, 1200)):
            suite_categories["material_images"].append(
                _save_cover_jpeg(material_rgba, category_dirs["material_images"] / f"{width}X{height}.jpg", width, height)
            )

    if _target_enabled(config.output_targets, "showcase_images"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(source, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["showcase_images"].append(exported)

    if _target_enabled(config.output_targets, "showcase_images"):
        showcase_records = _showcase_copy_records(analysis, len(sorted_pages))
        for idx, record in enumerate(showcase_records, start=1):
            rendered = _render_showcase_card(index=idx - 1, record=record, source_pool=showcase_pool, analysis=analysis, width=1440, height=1920)
            exported = _save_cover_jpeg(rendered, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = idx
            exported["slot"] = "showcase_card"
            exported["kind"] = "showcase_image"
            exported["source"] = "local_showcase_layout"
            exported["title"] = record.get("title") or ""
            suite_categories["showcase_images"].append(exported)

    categories_payload: Dict[str, Any] = {}
    for key, items in suite_categories.items():
        folder = category_dirs[key]
        categories_payload[key] = {
            "dirname": folder.name,
            "dir": str(folder),
            "count": len(items),
            "items": [{**item, "relative_path": _relative_to_run(run_dir, Path(str(item["path"])))} for item in items],
        }

    bundle = {
        "preset": SUITE_EXPORT_PRESET,
        "root_dir": str(root_dir),
        "root_relative_path": _relative_to_run(run_dir, root_dir),
        "categories": categories_payload,
        "summary": {
            "product_name": str(analysis.get("product_name") or ""),
            "hero_claim": str(analysis.get("hero_claim") or ""),
            "sku": config.sku,
        },
        "warnings": warnings,
    }
    (root_dir / "suite_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


DEFAULT_SELLING_POINTS = ["核心卖点突出", "适合移动端详情页展示", "强调场景价值与使用体验"]
DEFAULT_TRUST_POINTS = ["细节清晰可见", "风格统一专业", "适合电商转化表达"]
DEFAULT_USAGE_SCENES = ["居家场景", "日常使用场景", "近景细节场景"]


def _load_json_response(response: requests.Response) -> Dict[str, Any]:
    candidates: List[str] = []
    raw = response.content or b""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
        except Exception:
            continue
        if text not in candidates:
            candidates.append(text)
    if response.text and response.text not in candidates:
        candidates.append(response.text)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except Exception:
            stripped = candidate.lstrip()
            try:
                payload, _ = json.JSONDecoder().raw_decode(stripped)
            except Exception:
                continue
        if isinstance(payload, dict):
            return payload
    return {"raw_text": (response.text or raw.decode("utf-8", errors="replace"))}


def _comfly_check(self, response: requests.Response) -> Dict[str, Any]:
    payload = _load_json_response(response)
    if response.status_code != 200 or not isinstance(payload, dict):
        raise PipelineError(f"HTTP {response.status_code}: {payload}")
    return payload


ComflyClient._check = _comfly_check


def _pad_points(points: List[str], minimum: int, fallback: List[str]) -> List[str]:
    out = [item for item in points if item]
    seed = [item for item in fallback if item]
    while len(out) < minimum and seed:
        out.append(seed[len(out) % len(seed)])
    while len(out) < minimum:
        out.append(f"补充卖点 {len(out) + 1}")
    return out


def _build_page_slots(analysis: Dict[str, Any], page_count: int) -> List[Dict[str, Any]]:
    points = _pad_points(_safe_string_list(analysis.get("selling_points")), 6, DEFAULT_SELLING_POINTS)
    trust_points = _safe_string_list(analysis.get("trust_points")) or DEFAULT_TRUST_POINTS[:]
    scenes = _safe_string_list(analysis.get("usage_scenes")) or DEFAULT_USAGE_SCENES[:]
    materials = _safe_string_list(analysis.get("materials"))
    structure = _safe_string_list(analysis.get("structure_features"))
    care = _safe_string_list(analysis.get("care_points"))
    certs = _safe_string_list(analysis.get("certification_clues"))
    hero_claim = str(analysis.get("hero_claim") or points[0]).strip()
    summary = str(analysis.get("product_summary") or hero_claim).strip()
    target_page_count = max(1, int(page_count))
    detail_target = max(1, target_page_count - 1)

    hero_slot = {
        "slot": "cover",
        "goal": "Create a premium, non-numbered advertising cover that sells why this product is worth bringing home.",
        "focus": hero_claim,
        "points": points[:3],
        "show_number": False,
    }

    detail_slots: List[Dict[str, Any]] = [
        {"slot": "overview", "goal": "Summarize the top consumer benefits at a glance.", "focus": summary, "points": points[:4], "show_number": True},
        {"slot": "feature", "goal": "Explain the first key feature with a hero visual.", "focus": points[0], "points": [points[0], points[1]]},
        {"slot": "feature", "goal": "Explain the second key feature with a strong comparison feel.", "focus": points[1], "points": [points[1], points[2]]},
        {"slot": "feature", "goal": "Explain the third key feature with detail emphasis.", "focus": points[2], "points": [points[2], points[3]]},
        {"slot": "feature", "goal": "Show another value point that supports conversion.", "focus": points[3], "points": [points[3], points[4]]},
        {"slot": "scene", "goal": "Show the product in a believable daily scene.", "focus": scenes[0], "points": scenes[:3]},
        {
            "slot": "material",
            "goal": "Explain material and structure details.",
            "focus": (materials + structure + care + [hero_claim])[0],
            "points": (materials + structure + care)[:4] or points[2:6],
        },
        {
            "slot": "trust",
            "goal": "Build confidence with conservative proof points.",
            "focus": (trust_points + certs + [hero_claim])[0],
            "points": (trust_points + certs)[:4] or points[1:5],
        },
        {"slot": "closing", "goal": "Close with a summary and buying motivation.", "focus": hero_claim, "points": points[:4]},
    ]

    extra_pool = points[4:] + trust_points + scenes + materials + structure + care + certs
    extra_idx = 0
    while len(detail_slots) < detail_target:
        focus = extra_pool[extra_idx] if extra_idx < len(extra_pool) else f"补充卖点 {len(detail_slots) + 2}"
        point_idx = extra_idx % len(points)
        detail_slots.insert(
            -1,
            {
                "slot": "feature",
                "goal": "Expand one more selling point to make the sequence more complete.",
                "focus": focus,
                "points": [focus] + points[max(0, point_idx - 1) : point_idx + 1],
            },
        )
        extra_idx += 1

    slots = [hero_slot] + detail_slots[:detail_target]
    return slots[:target_page_count]


def _download_image(url: str, retries: int = 5, timeout: int = 180) -> Image.Image:
    last: Optional[Exception] = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            return image.convert("RGB") if image.mode != "RGB" else image
        except Exception as exc:
            last = exc
            if attempt >= max(1, retries):
                break
            time.sleep(min(12, attempt * 2))
    raise PipelineError(f"Failed to download image after retries: {last}")


DEFAULT_SELLING_POINTS = ["核心卖点突出", "适合移动端详情页展示", "强调场景价值与使用体验"]
DEFAULT_TRUST_POINTS = ["细节清晰可见", "风格统一专业", "适合电商转化表达"]
DEFAULT_USAGE_SCENES = ["居家场景", "日常使用场景", "近景细节场景"]


def _pad_points(points: List[str], minimum: int, fallback: List[str]) -> List[str]:
    out = [item for item in points if item]
    seed = [item for item in fallback if item]
    while len(out) < minimum and seed:
        out.append(seed[len(out) % len(seed)])
    while len(out) < minimum:
        out.append(f"补充卖点 {len(out) + 1}")
    return out


def _build_page_slots(analysis: Dict[str, Any], page_count: int) -> List[Dict[str, Any]]:
    selling_point_records = _normalize_selling_point_records(analysis.get("selling_point_records"))
    if not selling_point_records:
        selling_point_records = [{"title": item, "description": "", "icon": "", "priority": idx + 1} for idx, item in enumerate(_safe_string_list(analysis.get("selling_points"))[:8])]
    selling_point_titles = [str(item.get("title") or "").strip() for item in selling_point_records if str(item.get("title") or "").strip()]
    points = _pad_points(selling_point_titles or _safe_string_list(analysis.get("selling_points")), 6, DEFAULT_SELLING_POINTS)
    trust_points = _safe_string_list(analysis.get("trust_points")) or DEFAULT_TRUST_POINTS[:]
    scenes = _safe_string_list(analysis.get("usage_scenes")) or DEFAULT_USAGE_SCENES[:]
    materials = _dedupe_strings(_safe_string_list(analysis.get("materials")) + _safe_string_list(analysis.get("care_points")))
    structure = _safe_string_list(analysis.get("structure_features"))
    certs = _safe_string_list(analysis.get("certification_clues"))
    spec_entries = _spec_entries(analysis.get("specs"))
    hero_claim = str(analysis.get("hero_claim") or points[0]).strip()
    summary = str(analysis.get("product_summary") or hero_claim).strip()
    detail_target = max(1, int(page_count))
    target_page_count = detail_target + 1

    hero_slot = {
        "slot": "cover",
        "goal": "Create a dedicated promotional poster cover before the numbered detail pages, with a strong ad feel and immediate desire to buy.",
        "focus": hero_claim,
        "points": points[:3],
        "show_number": False,
        "metadata": {"template": "hero_cover", "style_id": analysis.get("style_id") or ""},
    }

    detail_slots: List[Dict[str, Any]] = [
        {
            "slot": "overview",
            "goal": "Summarize the top consumer benefits at a glance.",
            "focus": summary,
            "points": points[:4],
            "show_number": True,
            "metadata": {"template": "overview_summary"},
        }
    ]

    feature_records = selling_point_records[:4] or [{"title": item, "description": "", "icon": "", "priority": idx + 1} for idx, item in enumerate(points[:4])]
    for idx, item in enumerate(feature_records):
        focus = str(item.get("title") or item.get("description") or points[min(idx, len(points) - 1)]).strip()
        feature_points = _dedupe_strings(
            [
                str(item.get("title") or "").strip(),
                str(item.get("description") or "").strip(),
            ]
            + points[idx : idx + 2]
        )[:4]
        detail_slots.append(
            {
                "slot": "feature",
                "goal": "Highlight one structured selling point with strong product-led composition.",
                "focus": focus,
                "points": feature_points or [focus],
                "metadata": {
                    "template": "feature_focus",
                    "source": "selling_point",
                    "icon": str(item.get("icon") or "").strip(),
                },
            }
        )

    if scenes:
        detail_slots.append(
            {
                "slot": "scene",
                "goal": "Show the product in a believable daily scene that matches the intended buyer context.",
                "focus": scenes[0],
                "points": scenes[:3],
                "metadata": {"template": "scene_usage"},
            }
        )
    if materials or structure:
        detail_slots.append(
            {
                "slot": "material",
                "goal": "Explain material, cleaning, and structure details with a close-up feel.",
                "focus": (materials + structure + [hero_claim])[0],
                "points": (materials + structure)[:4] or points[2:6],
                "metadata": {"template": "material_cleaning"},
            }
        )
    if spec_entries:
        detail_slots.append(
            {
                "slot": "spec_table",
                "goal": "Present the structured specifications in a clean, easy-to-scan parameter summary page.",
                "focus": spec_entries[0]["key"],
                "points": [f'{item["key"]} {item["value"]}' for item in spec_entries[:6]],
                "metadata": {"template": "spec_table", "spec_entries": spec_entries[:6]},
            }
        )
    if trust_points or certs:
        detail_slots.append(
            {
                "slot": "trust",
                "goal": "Build confidence with conservative proof points and reassuring details.",
                "focus": (trust_points + certs + [hero_claim])[0],
                "points": (trust_points + certs)[:4] or points[1:5],
                "metadata": {"template": "trust_reassurance"},
            }
        )

    detail_slots.append(
        {
            "slot": "closing",
            "goal": "Close with a summary and buying motivation.",
            "focus": hero_claim,
            "points": points[:4],
            "metadata": {"template": "closing_summary"},
        }
    )

    extra_records = selling_point_records[4:]
    extra_pool = [str(item.get("title") or item.get("description") or "").strip() for item in extra_records]
    extra_pool += points[4:] + trust_points + scenes + materials + structure + certs
    extra_pool = [item for item in extra_pool if item]
    extra_idx = 0
    while len(detail_slots) < detail_target:
        focus = extra_pool[extra_idx] if extra_idx < len(extra_pool) else f"补充卖点 {len(detail_slots) + 1}"
        point_idx = extra_idx % len(points)
        detail_slots.insert(
            max(1, len(detail_slots) - 1),
            {
                "slot": "feature",
                "goal": "Expand one more selling point to make the sequence more complete.",
                "focus": focus,
                "points": _dedupe_strings([focus] + points[max(0, point_idx - 1) : point_idx + 1])[:4],
                "metadata": {"template": "feature_focus", "source": "expanded_pool"},
            },
        )
        extra_idx += 1

    slots = [hero_slot] + detail_slots[:detail_target]
    return slots[:target_page_count]


def _generate_white_bg_assets(
    *,
    client: ComflyClient,
    logger: RunLogger,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    reference_urls: List[str],
    product_image_rgba: Image.Image,
) -> Dict[str, Any]:
    if _image_has_real_transparency(product_image_rgba):
        transparent_image = _normalize_reference_cutout(product_image_rgba, width=800, height=800)
        white_bg_image = _normalize_reference_cutout(
            product_image_rgba,
            width=800,
            height=800,
            background=(255, 255, 255, 255),
        ).convert("RGB")
        return {
            "white_bg_image": white_bg_image,
            "black_bg_image": None,
            "transparent_image": transparent_image,
            "generation_mode": "reference_alpha",
            "generated_image_url": "",
            "prompt": "",
            "attempts": 0,
            "white_bg_generated_image_url": "",
            "black_bg_generated_image_url": "",
            "white_bg_prompt": "",
            "black_bg_prompt": "",
            "warnings": [],
        }

    white_prompt = _compose_white_bg_prompt(analysis)
    white_generated, white_attempts = client.generate_image(
        config.image_model,
        white_prompt,
        "1:1",
        reference_urls,
        "06_white_bg_source",
    )
    logger.record_usage(
        "image",
        config.image_model,
        "white_bg_source",
        payload={"attempts": white_attempts, "aspect_ratio": "1:1"},
    )
    white_bg_source = _download_image(white_generated["url"], retries=5, timeout=180)
    white_bg_image = _normalize_generated_white_bg(white_bg_source, 800, 800)

    black_prompt = _compose_black_bg_prompt(analysis)
    black_refs = [str(white_generated["url"]).strip()] + [ref for ref in reference_urls if str(ref).strip()]
    black_generated, black_attempts = client.generate_image(
        config.image_model,
        black_prompt,
        "1:1",
        black_refs,
        "06_black_bg_source",
    )
    logger.record_usage(
        "image",
        config.image_model,
        "black_bg_source",
        payload={"attempts": black_attempts, "aspect_ratio": "1:1"},
    )
    black_bg_source = _download_image(black_generated["url"], retries=5, timeout=180)
    black_bg_image = _normalize_generated_black_bg(black_bg_source, 800, 800)

    transparent_image = _derive_transparent_from_white_black_bg(white_bg_image, black_bg_image)
    if not _image_has_real_transparency(transparent_image):
        raise PipelineError("Generated white/black-background images could not be converted into a usable transparent asset")
    return {
        "white_bg_image": white_bg_image,
        "black_bg_image": black_bg_image,
        "transparent_image": transparent_image,
        "generation_mode": "remote",
        "generated_image_url": white_generated["url"],
        "prompt": white_prompt,
        "attempts": white_attempts + black_attempts,
        "white_bg_generated_image_url": white_generated["url"],
        "black_bg_generated_image_url": black_generated["url"],
        "white_bg_prompt": white_prompt,
        "black_bg_prompt": black_prompt,
        "warnings": [],
    }


def _export_suite_bundle(
    *,
    run_dir: Path,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    product_image_rgba: Image.Image,
    page_results: List[Dict[str, Any]],
    white_bg_assets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root_dir = run_dir / SUITE_EXPORT_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    category_dirs = {key: root_dir / dirname for key, dirname in SUITE_EXPORT_CATEGORY_DIRS.items()}
    for folder in category_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(page_results, key=lambda item: int(item.get("index") or 0))
    page_by_slot: Dict[str, Dict[str, Any]] = {}
    for item in sorted_pages:
        slot = str(item.get("slot") or "").strip().lower()
        if slot and slot not in page_by_slot:
            page_by_slot[slot] = item

    def pick_page(*slots: str) -> Dict[str, Any]:
        for slot in slots:
            found = page_by_slot.get(slot.strip().lower())
            if found:
                return found
        return sorted_pages[0]

    cover_page = pick_page("cover", "overview")
    overview_page = pick_page("overview", "feature", "cover")
    sku_layout_page = pick_page("spec_table", "material", "trust", "overview", "feature")
    material_page = pick_page("cover", "scene", "overview")

    suite_categories: Dict[str, List[Dict[str, Any]]] = {
        "main_images": [],
        "sku_images": [],
        "transparent_white_bg": [],
        "detail_images": [],
        "material_images": [],
        "showcase_images": [],
    }
    main_payload = main_image_assets if isinstance(main_image_assets, dict) else {}
    sku_payload = sku_assets if isinstance(sku_assets, dict) else {}
    payload = white_bg_assets if isinstance(white_bg_assets, dict) else {}
    warnings = (
        _safe_string_list(payload.get("warnings"))
        + _safe_string_list(main_payload.get("warnings"))
        + _safe_string_list(sku_payload.get("warnings"))
    )

    cover_rgba = _open_local_image(str(cover_page["local_path"]))
    overview_rgba = _open_local_image(str(overview_page["local_path"]))
    sku_layout_rgba = _open_local_image(str(sku_layout_page["local_path"]))
    material_rgba = _open_local_image(str(material_page["local_path"]))
    white_bg_image = payload.get("white_bg_image") if isinstance(payload.get("white_bg_image"), Image.Image) else None
    transparent_image = payload.get("transparent_image") if isinstance(payload.get("transparent_image"), Image.Image) else None
    sku_scene = (sku_payload.get("scene") or {}) if isinstance(sku_payload.get("scene"), dict) else {}
    sku_layout = (sku_payload.get("layout") or {}) if isinstance(sku_payload.get("layout"), dict) else {}
    sku_scene_image = sku_scene.get("master_image") if isinstance(sku_scene.get("master_image"), Image.Image) else None
    sku_layout_image = sku_layout.get("master_image") if isinstance(sku_layout.get("master_image"), Image.Image) else None
    showcase_pool = _build_showcase_source_pool(
        product_image_rgba=product_image_rgba,
        main_square_image=main_square_image,
        main_portrait_image=main_portrait_image,
        sku_scene_image=sku_scene_image,
        white_bg_image=white_bg_image,
    )

    if _target_enabled(config.output_targets, "main_images"):
        suite_categories["main_images"].append(
            _save_cover_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1440.jpg", 1440, 1440)
        )
        suite_categories["main_images"].append(
            _save_cover_jpeg(cover_rgba, category_dirs["main_images"] / "1-1440X1920.jpg", 1440, 1920)
        )

    if _target_enabled(config.output_targets, "sku_images"):
        suite_categories["sku_images"].append(
            _save_cover_jpeg(overview_rgba, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
        )
        suite_categories["sku_images"].append(
            _save_cover_jpeg(sku_layout_rgba, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
        )

    if _target_enabled(config.output_targets, "sku_images"):
        if not isinstance(sku_scene_image, Image.Image):
            raise PipelineError("SKU image target is enabled, but the SKU scene master is missing")
        if not isinstance(sku_layout_image, Image.Image):
            raise PipelineError("SKU image target is enabled, but the SKU layout master is missing")

        scene_export = _save_cover_jpeg(sku_scene_image, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
        scene_export["kind"] = "sku_scene"
        scene_export["source"] = "generated_sku_image"
        scene_export["generated_image_url"] = str(sku_scene.get("generated_image_url") or "")
        scene_export["prompt"] = str(sku_scene.get("prompt") or "")
        suite_categories["sku_images"].append(scene_export)

        layout_export = _save_cover_jpeg(sku_layout_image, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
        layout_export["kind"] = "sku_layout"
        layout_export["source"] = "generated_sku_image"
        layout_export["generated_image_url"] = str(sku_layout.get("generated_image_url") or "")
        layout_export["prompt"] = str(sku_layout.get("prompt") or "")
        suite_categories["sku_images"].append(layout_export)

    if _target_enabled(config.output_targets, "white_bg_image"):
        if not isinstance(white_bg_image, Image.Image):
            raise PipelineError("White-background image target is enabled, but generated white-background output is missing")
        white_bg = _save_cover_jpeg(
            white_bg_image,
            category_dirs["transparent_white_bg"] / "1-白底.jpg",
            800,
            800,
        )
        white_bg["kind"] = "white_bg_image"
        white_bg["generation_mode"] = str(payload.get("generation_mode") or "remote")
        white_bg["generated_image_url"] = str(payload.get("generated_image_url") or "")
        white_bg["prompt"] = str(payload.get("prompt") or "")
        suite_categories["transparent_white_bg"].append(white_bg)

    if _target_enabled(config.output_targets, "transparent_image"):
        if not isinstance(transparent_image, Image.Image):
            raise PipelineError("Transparent image target is enabled, but the transparent asset derived from the generated white-background image is missing")
        if not _image_has_real_transparency(transparent_image):
            raise PipelineError("Transparent image target is enabled, but the derived transparent asset does not contain a usable alpha channel")
        transparent_path = category_dirs["transparent_white_bg"] / "1-透明.png"
        transparent_path.parent.mkdir(parents=True, exist_ok=True)
        transparent_image.convert("RGBA").save(transparent_path, format="PNG")
        transparent = {
            "filename": transparent_path.name,
            "path": str(transparent_path),
            "width": transparent_image.width,
            "height": transparent_image.height,
            "kind": "transparent_image",
            "source": "white_bg_generated",
        }
        suite_categories["transparent_white_bg"].append(transparent)

    if _target_enabled(config.output_targets, "detail_pages"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(
                source,
                category_dirs["detail_images"] / f"详情_{idx:02d}.jpg",
                source.width,
                source.height,
            )
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["detail_images"].append(exported)

    if _target_enabled(config.output_targets, "material_images"):
        for width, height in ((513, 750), (800, 1200), (900, 1200)):
            suite_categories["material_images"].append(
                _save_cover_jpeg(material_rgba, category_dirs["material_images"] / f"{width}X{height}.jpg", width, height)
            )

    if _target_enabled(config.output_targets, "showcase_images"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(source, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["showcase_images"].append(exported)

    categories_payload: Dict[str, Any] = {}
    for key, items in suite_categories.items():
        folder = category_dirs[key]
        categories_payload[key] = {
            "dirname": folder.name,
            "dir": str(folder),
            "count": len(items),
            "items": [{**item, "relative_path": _relative_to_run(run_dir, Path(str(item["path"])))} for item in items],
        }

    bundle = {
        "preset": SUITE_EXPORT_PRESET,
        "root_dir": str(root_dir),
        "root_relative_path": _relative_to_run(run_dir, root_dir),
        "categories": categories_payload,
        "summary": {
            "product_name": str(analysis.get("product_name") or ""),
            "hero_claim": str(analysis.get("hero_claim") or ""),
            "sku": config.sku,
        },
        "warnings": warnings,
    }
    (root_dir / "suite_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def run_pipeline(data: Input) -> Dict[str, Any]:
    config = _build_config(data)
    output_dir = str(_resolve_output_dir(config.output_dir))
    logger = RunLogger(output_dir, config, dict(data))
    partial_output: Dict[str, Any] = {
        "preview_html_path": None,
        "archive_path": None,
        "page_results": [],
        "failed_pages": [],
        "suite_bundle": None,
    }

    try:
        product_image = str(data.get("product_image") or "").strip()
        if not product_image:
            raise PipelineError("Missing product_image")

        refs_in = [str(x).strip() for x in data.get("reference_images", []) or [] if str(x).strip()]
        style_refs_in = [str(x).strip() for x in (config.style_reference_images or []) if str(x).strip()]
        client = ComflyClient(config, logger)

        product_image_url, upload_attempts = client.upload(product_image)
        reference_urls = [product_image_url]
        for ref in refs_in[:5]:
            ref_url, _ = client.upload(ref)
            if ref_url not in reference_urls:
                reference_urls.append(ref_url)
        for ref in style_refs_in[:3]:
            ref_url, _ = client.upload(ref)
            if ref_url not in reference_urls:
                reference_urls.append(ref_url)
        logger.step(
            "01_upload_inputs",
            "success",
            attempts=upload_attempts,
            payload={"product_image_url": product_image_url, "reference_urls": reference_urls},
        )

        analysis_raw, analysis_attempts = client.analyze_json(
            config.analysis_model,
            _analysis_prompt(config),
            reference_urls,
            "02_analysis",
            max_tokens=4000,
        )
        locale = _locale_defaults(config.platform, config.country, config.language, config.target_market)
        analysis = _normalize_analysis(analysis_raw, locale)
        analysis = _merge_structured_inputs_into_analysis(analysis, config)
        analysis = _apply_user_hints_to_analysis(analysis, config)
        logger.step("02_analysis", "success", attempts=analysis_attempts, payload=analysis)
        logger.record_usage("analysis", config.analysis_model, "product_analysis", payload={"attempts": analysis_attempts})

        slots = _build_page_slots(analysis, config.page_count)
        logger.step("03_page_slots", "success", attempts=1, payload={"slots": slots})

        copy_raw, copy_attempts = client.analyze_json(
            config.analysis_model,
            _page_copy_prompt(analysis, slots, config),
            reference_urls,
            "04_page_copy_plan",
            max_tokens=8000,
        )
        pages = _normalize_pages(copy_raw, slots, analysis)
        logger.step("04_page_copy_plan", "success", attempts=copy_attempts, payload={"pages": pages})
        logger.record_usage("analysis", config.analysis_model, "page_copy_plan", payload={"attempts": copy_attempts})

        product_image_rgba = _download_image_rgba(product_image_url)
        product_local = product_image_rgba.convert("RGB")
        used_icon_ids = [
            str(((page.get("metadata") or {}) if isinstance(page.get("metadata"), dict) else {}).get("icon") or "").strip()
            for page in pages
        ]
        icon_images = _load_icon_images(config.icon_assets, used_icon_ids)
        page_results: List[Dict[str, Any]] = []
        failed_pages: List[Dict[str, Any]] = []

        def _generate_single_page(
            page: Dict[str, Any],
            *,
            phase: str = "final",
            extra_attempts: int = 0,
            allow_local_fallback: bool = False,
        ) -> Dict[str, Any]:
            idx = int(page["index"])
            if str(page.get("slot") or "").strip().lower() == "cover":
                image_prompt = _compose_cover_background_prompt(page, analysis)
            else:
                image_prompt = _compose_page_background_prompt(page, analysis)
            generated = None
            attempts = 0
            last_page_error: Optional[Exception] = None
            total_attempts = max(1, int(config.image_generation_retries) + int(extra_attempts))
            for page_attempt in range(1, total_attempts + 1):
                try:
                    generated, attempts = client.generate_image(
                        config.image_model,
                        image_prompt,
                        config.aspect_ratio,
                        reference_urls,
                        f"05_{phase}_page_{idx:02d}",
                    )
                    logger.record_usage(
                        "image",
                        config.image_model,
                        f"{phase}_page_{idx:02d}",
                        payload={"attempts": attempts, "page_attempt": page_attempt, "phase": phase},
                    )
                    background_image = _download_image(generated["url"], retries=5, timeout=180)
                    rendered = _render_page(product_local.copy(), background_image, page, config, icon_images=icon_images)
                    local_path = logger.detail_dir / f"详情_{idx:02d}.jpg"
                    rendered.save(local_path, format="JPEG", quality=92, subsampling=0)
                    return {
                        "index": idx,
                        "slot": page["slot"],
                        "title": page["title"],
                        "subtitle": page["subtitle"],
                        "highlights": page["highlights"],
                        "footer": page["footer"],
                        "width": rendered.width,
                        "height": rendered.height,
                        "filename": local_path.name,
                        "relative_path": str(local_path.relative_to(logger.run_dir)),
                        "generated_image_url": generated["url"],
                        "generated_image_prompt": image_prompt,
                        "generation_mode": "remote",
                        "local_path": str(local_path),
                        "attempts": attempts,
                    }
                except Exception as page_exc:
                    last_page_error = page_exc
                    if page_attempt >= total_attempts:
                        break
                    delay_multiplier = 4 if _is_provider_quota_like_error(page_exc) else 1
                    time.sleep(max(1, config.network_retry_delay_seconds) * page_attempt * delay_multiplier)

            if allow_local_fallback and last_page_error is not None and _is_retriable_generation_error(last_page_error):
                fallback_background = _make_local_fallback_background(product_local.copy(), page, config)
                rendered = _render_page(product_local.copy(), fallback_background, page, config, icon_images=icon_images)
                local_path = logger.detail_dir / f"详情_{idx:02d}.jpg"
                rendered.save(local_path, format="JPEG", quality=92, subsampling=0)
                return {
                    "index": idx,
                    "slot": page["slot"],
                    "title": page["title"],
                    "subtitle": page["subtitle"],
                    "highlights": page["highlights"],
                    "footer": page["footer"],
                    "width": rendered.width,
                    "height": rendered.height,
                    "filename": local_path.name,
                    "relative_path": str(local_path.relative_to(logger.run_dir)),
                    "generated_image_url": None,
                    "generated_image_prompt": image_prompt,
                    "generation_mode": "local_fallback",
                    "fallback_reason": str(last_page_error),
                    "local_path": str(local_path),
                    "attempts": attempts,
                }
            raise PipelineError(f"Page {idx} failed after retries: {last_page_error}")

        max_workers = max(1, min(config.image_concurrency, len(pages)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_generate_single_page, page): page for page in pages}
            for future in as_completed(future_map):
                page = future_map[future]
                idx = int(page["index"])
                try:
                    result = future.result()
                    page_results.append(result)
                    logger.page(idx, "final", "success", attempts=int(result.get("attempts") or 0), payload=result)
                except Exception as exc:
                    failed_pages.append({"index": idx, "slot": page["slot"], "error": str(exc)})
                    logger.page(
                        idx,
                        "final",
                        "failed",
                        error=str(exc),
                        payload={"page": page, "traceback": traceback.format_exc()},
                    )

        if failed_pages:
            logger.step("05_retry_failed_pages", "running", attempts=1, payload={"failed_pages": failed_pages})
            pages_by_index = {int(page["index"]): page for page in pages}
            recovered_results: List[Dict[str, Any]] = []
            remaining_failed_pages: List[Dict[str, Any]] = []
            for item in sorted(failed_pages, key=lambda x: int(x.get("index") or 0)):
                idx = int(item["index"])
                page = pages_by_index.get(idx)
                if not page:
                    remaining_failed_pages.append(item)
                    continue
                if _is_provider_quota_like_error(item.get("error")):
                    time.sleep(max(8, config.network_retry_delay_seconds * 4))
                try:
                    result = _generate_single_page(
                        page,
                        phase="fallback",
                        extra_attempts=2,
                        allow_local_fallback=True,
                    )
                    recovered_results.append(result)
                    logger.page(idx, "fallback", "success", attempts=int(result.get("attempts") or 0), payload=result)
                except Exception as exc:
                    remaining_failed_pages.append({"index": idx, "slot": page["slot"], "error": str(exc)})
                    logger.page(
                        idx,
                        "fallback",
                        "failed",
                        error=str(exc),
                        payload={"page": page, "traceback": traceback.format_exc()},
                    )
            if recovered_results:
                page_results_by_index = {int(item["index"]): item for item in page_results}
                for item in recovered_results:
                    page_results_by_index[int(item["index"])] = item
                page_results = [page_results_by_index[idx] for idx in sorted(page_results_by_index)]
            failed_pages = remaining_failed_pages
            logger.step(
                "05_retry_failed_pages",
                "success" if not failed_pages else "failed",
                attempts=1,
                payload={
                    "recovered_count": len(recovered_results),
                    "remaining_failed_pages": failed_pages,
                },
            )
        partial_output["page_results"] = sorted(page_results, key=lambda x: int(x["index"])) if page_results else []
        partial_output["failed_pages"] = failed_pages
        if not page_results:
            raise PipelineError("All pages failed to generate")
        if failed_pages:
            failed_summary = ", ".join(f"page {item['index']}: {item['error']}" for item in failed_pages)
            raise PipelineError(f"Incomplete output is not allowed, failed pages: {failed_summary}")

        long_image = _compose_long_image(
            [item["local_path"] for item in sorted(page_results, key=lambda x: int(x["index"]))],
            str(logger.detail_dir / "detail_long_image.jpg"),
            config.page_gap_px,
        )
        logger.step("06_compose_long_image", "success", attempts=1, payload=long_image)
        main_image_assets: Dict[str, Any] = {}
        if _target_enabled(config.output_targets, "main_images"):
            main_image_assets = _generate_main_image_assets(
                client=client,
                logger=logger,
                analysis=analysis,
                config=config,
                reference_urls=reference_urls,
            )
            logger.step(
                "07_generate_main_image_assets",
                "success",
                attempts=sum(int(item.get("attempts") or 0) for item in (main_image_assets.get("shots") or []) if isinstance(item, dict)),
                payload={
                    "shots": [
                        {
                            "index": item.get("index"),
                            "shot_key": item.get("shot_key"),
                            "label": item.get("label"),
                            "focus": item.get("focus"),
                            "generated_image_url": item.get("generated_image_url"),
                            "square_master": (item.get("square") or {}).get("master") if isinstance(item.get("square"), dict) else None,
                            "portrait_master": (item.get("portrait") or {}).get("master") if isinstance(item.get("portrait"), dict) else None,
                        }
                        for item in (main_image_assets.get("shots") or [])
                        if isinstance(item, dict)
                    ],
                    "variants": {
                        key: {
                            "aspect_ratio": value.get("aspect_ratio"),
                            "generated_image_url": value.get("generated_image_url"),
                            "prompt": value.get("prompt"),
                            "master": value.get("master"),
                        }
                        for key, value in ((main_image_assets.get("variants") or {}) if isinstance(main_image_assets.get("variants"), dict) else {}).items()
                        if isinstance(value, dict)
                    }
                },
            )
        white_bg_assets: Dict[str, Any] = {}
        if _target_enabled(config.output_targets, "transparent_image") or _target_enabled(config.output_targets, "white_bg_image"):
            white_bg_assets = _generate_white_bg_assets(
                client=client,
                logger=logger,
                analysis=analysis,
                config=config,
                reference_urls=reference_urls,
                product_image_rgba=product_image_rgba,
            )
            logger.step(
                "08_generate_white_bg_assets",
                "success",
                attempts=int(white_bg_assets.get("attempts") or 0),
                payload={
                    "generation_mode": white_bg_assets.get("generation_mode"),
                    "generated_image_url": white_bg_assets.get("generated_image_url"),
                    "prompt": white_bg_assets.get("prompt"),
                    "white_bg_generated_image_url": white_bg_assets.get("white_bg_generated_image_url"),
                    "black_bg_generated_image_url": white_bg_assets.get("black_bg_generated_image_url"),
                    "white_bg_prompt": white_bg_assets.get("white_bg_prompt"),
                    "black_bg_prompt": white_bg_assets.get("black_bg_prompt"),
                    "warnings": white_bg_assets.get("warnings"),
                },
            )
        sku_assets: Dict[str, Any] = {}
        if _target_enabled(config.output_targets, "sku_images"):
            sku_assets = _generate_sku_assets(
                client=client,
                logger=logger,
                analysis=analysis,
                config=config,
                reference_urls=reference_urls,
                white_bg_assets=white_bg_assets,
            )
            logger.step(
                "09_generate_sku_assets",
                "success",
                attempts=sum(int(item.get("attempts") or 0) for item in (sku_assets.get("variants") or []) if isinstance(item, dict)),
                payload={
                    "variants": [
                        {
                            "index": item.get("index"),
                            "shot_key": item.get("shot_key"),
                            "label": item.get("label"),
                            "mode": item.get("mode"),
                            "generated_image_url": item.get("generated_image_url"),
                            "prompt": item.get("prompt"),
                            "master": item.get("master"),
                        }
                        for item in (sku_assets.get("variants") or [])
                        if isinstance(item, dict)
                    ],
                    "scene": {
                        "aspect_ratio": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("aspect_ratio"),
                        "generated_image_url": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("generated_image_url"),
                        "prompt": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("prompt"),
                        "master": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("master"),
                    },
                    "layout": {
                        "aspect_ratio": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("aspect_ratio"),
                        "generated_image_url": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("generated_image_url"),
                        "prompt": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("prompt"),
                        "master": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("master"),
                    },
                    "warnings": sku_assets.get("warnings"),
                },
            )
        suite_bundle = _export_suite_bundle(
            run_dir=logger.run_dir,
            analysis=analysis,
            config=config,
            product_image_rgba=product_image_rgba,
            page_results=page_results,
            main_image_assets=main_image_assets,
            sku_assets=sku_assets,
            white_bg_assets=white_bg_assets,
        )
        partial_output["suite_bundle"] = suite_bundle
        logger.step("10_export_suite_bundle", "success", attempts=1, payload=suite_bundle)
        preview_html_path = _write_preview_html(
            run_dir=logger.run_dir,
            detail_dir=logger.detail_dir,
            analysis=analysis,
            page_results=sorted(page_results, key=lambda x: int(x["index"])),
            long_image=long_image,
        )
        partial_output["preview_html_path"] = preview_html_path
        logger.step("11_preview_html", "success", attempts=1, payload={"preview_html_path": preview_html_path})
        archive_path = _write_delivery_archive(run_dir=logger.run_dir)
        partial_output["archive_path"] = archive_path
        logger.step("12_archive", "success", attempts=1, payload={"archive_path": archive_path})

        usage = logger.usage_snapshot()
        output = {
            "run_dir": str(logger.run_dir),
            "detail_dir": str(logger.detail_dir),
            "main_image_assets": {
                "shots": [
                    {
                        "index": item.get("index"),
                        "shot_key": item.get("shot_key"),
                        "label": item.get("label"),
                        "focus": item.get("focus"),
                        "generated_image_url": item.get("generated_image_url"),
                        "square": (item.get("square") or {}).get("master") if isinstance(item.get("square"), dict) else None,
                        "portrait": (item.get("portrait") or {}).get("master") if isinstance(item.get("portrait"), dict) else None,
                    }
                    for item in (main_image_assets.get("shots") or [])
                    if isinstance(item, dict)
                ],
                "variants": {
                    key: {
                        "aspect_ratio": value.get("aspect_ratio"),
                        "generated_image_url": value.get("generated_image_url"),
                        "prompt": value.get("prompt"),
                        "master": value.get("master"),
                    }
                    for key, value in ((main_image_assets.get("variants") or {}) if isinstance(main_image_assets.get("variants"), dict) else {}).items()
                    if isinstance(value, dict)
                },
                "warnings": main_image_assets.get("warnings"),
            },
            "sku_assets": {
                "variants": [
                    {
                        "index": item.get("index"),
                        "shot_key": item.get("shot_key"),
                        "label": item.get("label"),
                        "mode": item.get("mode"),
                        "generated_image_url": item.get("generated_image_url"),
                        "prompt": item.get("prompt"),
                        "master": item.get("master"),
                    }
                    for item in (sku_assets.get("variants") or [])
                    if isinstance(item, dict)
                ],
                "scene": {
                    "aspect_ratio": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("aspect_ratio"),
                    "generated_image_url": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("generated_image_url"),
                    "prompt": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("prompt"),
                    "master": ((sku_assets.get("scene") or {}) if isinstance(sku_assets.get("scene"), dict) else {}).get("master"),
                },
                "layout": {
                    "aspect_ratio": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("aspect_ratio"),
                    "generated_image_url": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("generated_image_url"),
                    "prompt": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("prompt"),
                    "master": ((sku_assets.get("layout") or {}) if isinstance(sku_assets.get("layout"), dict) else {}).get("master"),
                },
                "warnings": sku_assets.get("warnings"),
            },
            "white_bg_assets": {
                "generation_mode": white_bg_assets.get("generation_mode"),
                "generated_image_url": white_bg_assets.get("generated_image_url"),
                "prompt": white_bg_assets.get("prompt"),
                "white_bg_generated_image_url": white_bg_assets.get("white_bg_generated_image_url"),
                "black_bg_generated_image_url": white_bg_assets.get("black_bg_generated_image_url"),
                "white_bg_prompt": white_bg_assets.get("white_bg_prompt"),
                "black_bg_prompt": white_bg_assets.get("black_bg_prompt"),
                "warnings": white_bg_assets.get("warnings"),
            },
            "suite_bundle": suite_bundle,
            "preview_html_path": preview_html_path,
            "archive_path": archive_path,
            "product_image_url": product_image_url,
            "reference_urls": reference_urls,
            "analysis": analysis,
            "pages": pages,
            "page_results": sorted(page_results, key=lambda x: int(x["index"])),
            "failed_pages": failed_pages,
            "final_long_image": long_image,
            "usage": usage,
            "billing_summary": _usage_billing_summary(usage),
            "config": {
                "analysis_model": config.analysis_model,
                "image_model": config.image_model,
                "aspect_ratio": config.aspect_ratio,
                "page_count": config.page_count,
                "total_page_count": len(pages),
                "page_width": config.page_width,
                "page_height": config.page_height,
                "page_gap_px": config.page_gap_px,
                "image_concurrency": config.image_concurrency,
            },
        }
        logger.finish("success", output)
        return output
    except Exception as exc:
        failure_payload = _build_partial_failure_payload(
            logger=logger,
            exc=exc,
            config=config,
            partial_output=partial_output,
        )
        failure_payload["traceback"] = traceback.format_exc()
        logger.finish("failed", failure_payload)
        code, message = _friendly_failure_message(exc, failure_payload)
        raise PipelineExecutionError(message, code=code, data=failure_payload) from exc


def handler(args: Args[Input]) -> Output:
    try:
        result = run_pipeline(args.input)
        return {"code": 200, "msg": "Pipeline completed successfully", "data": result}
    except PipelineExecutionError as exc:
        return {"code": exc.code, "msg": str(exc), "data": exc.data}
    except Exception as exc:
        return {"code": -500, "msg": f"Pipeline failed: {exc}", "data": None}


def _decode_cli_json(data: bytes) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "gb18030"):
        try:
            return json.loads(data.decode(encoding))
        except Exception as exc:
            last_error = exc
            continue
    raise PipelineError(f"Unable to decode JSON input: {last_error}")


def _load_cli_input() -> Dict[str, Any]:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--input-file", dest="input_file", help="Read JSON input from a file path. Recommended for local testing with Chinese paths.")
    parsed = parser.parse_args()

    if parsed.input_file:
        return _decode_cli_json(Path(parsed.input_file).read_bytes())

    if not os.sys.stdin.isatty():
        stdin_bytes = os.sys.stdin.buffer.read()
        if stdin_bytes.strip():
            return _decode_cli_json(stdin_bytes)
    return {}


def _looks_like_wearable_category(category: str) -> bool:
    value = (category or "").strip().lower()
    if not value:
        return False
    tokens = [
        "服", "衣", "裙", "裤", "鞋", "靴", "帽", "包", "外套", "上衣", "内搭", "毛衣", "大衣", "配饰",
        "coat", "jacket", "dress", "shirt", "pants", "shoe", "shoes", "bag", "fashion", "apparel", "clothing",
    ]
    return any(token in value for token in tokens)


def _main_image_shot_plan(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    selling_points = _safe_string_list(analysis.get("selling_points"))
    structure = _safe_string_list(analysis.get("structure_features"))
    materials = _safe_string_list(analysis.get("materials"))
    scenes = _safe_string_list(analysis.get("usage_scenes"))
    hero_claim = str(analysis.get("hero_claim") or "").strip()
    product_name = str(analysis.get("product_name") or analysis.get("category") or "商品").strip()

    focus_a = selling_points[0] if selling_points else hero_claim or product_name
    focus_b = selling_points[1] if len(selling_points) > 1 else (structure[0] if structure else focus_a)
    focus_c = selling_points[2] if len(selling_points) > 2 else (scenes[0] if scenes else focus_a)
    focus_d = structure[0] if structure else (selling_points[1] if len(selling_points) > 1 else focus_a)
    focus_e = materials[0] if materials else (selling_points[2] if len(selling_points) > 2 else focus_a)

    return [
        {
            "index": 1,
            "key": "hero_anchor",
            "label": "主图锚点",
            "direction": "front-facing hero shot or stable three-quarter hero view, full product clearly visible, centered and complete",
            "focus": focus_a,
            "portrait_anchor_x": 0.5,
            "portrait_anchor_y": 0.32,
        },
        {
            "index": 2,
            "key": "angle_view",
            "label": "角度展示",
            "direction": "forty-five-degree angled view that shows the product silhouette, side structure, and volume more clearly",
            "focus": focus_b,
            "portrait_anchor_x": 0.5,
            "portrait_anchor_y": 0.36,
        },
        {
            "index": 3,
            "key": "lifestyle_scene",
            "label": "场景价值",
            "direction": "lifestyle usage scene with believable home context, optional pet or daily-life props, product remains the dominant subject",
            "focus": focus_c,
            "portrait_anchor_x": 0.52,
            "portrait_anchor_y": 0.38,
        },
        {
            "index": 4,
            "key": "feature_demo",
            "label": "功能演示",
            "direction": "feature demonstration view showing a functional state, opening method, storage use, or interaction while keeping the product mostly complete in frame",
            "focus": focus_d,
            "portrait_anchor_x": 0.48,
            "portrait_anchor_y": 0.42,
        },
        {
            "index": 5,
            "key": "material_detail",
            "label": "材质细节",
            "direction": "closer craftsmanship and material-emphasis shot with stronger texture visibility, but still keep the overall product recognizable",
            "focus": focus_e,
            "portrait_anchor_x": 0.5,
            "portrait_anchor_y": 0.45,
        },
    ]


def _compose_main_image_prompt(analysis: Dict[str, Any], *, aspect_ratio: str, shot: Optional[Dict[str, Any]] = None) -> str:
    category = str(analysis.get("category") or "product").strip()
    product_name = str(analysis.get("product_name") or category or "product").strip()
    style = str(analysis.get("visual_style") or "premium ecommerce photography").strip()
    hero_claim = str(analysis.get("hero_claim") or "").strip()
    points = ", ".join(_safe_string_list(analysis.get("selling_points"))[:3])
    scenes = ", ".join(_safe_string_list(analysis.get("usage_scenes"))[:2])
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:3])
    wearable = _looks_like_wearable_category(category)
    shot_focus = str((shot or {}).get("focus") or "").strip()
    shot_direction = str((shot or {}).get("direction") or "").strip()
    shot_label = str((shot or {}).get("label") or "").strip()
    composition = (
        "Create a premium fashion campaign hero image with the garment as the primary subject, while keeping the full product clearly visible and dominant in frame"
        if wearable
        else "Create a premium ecommerce hero image with the product as the dominant subject in a believable lifestyle scene"
    )
    framing = (
        "Use a square hero composition with strong product presence, but keep the entire product fully inside the canvas with safe margins; do not crop off the head, sleeves, hem, or key silhouette. Prefer showing the whole garment rather than a tight fashion portrait. If a person appears, keep the full body or at least the full garment fully visible inside frame"
        if aspect_ratio == "1:1"
        else "Use a tall portrait hero composition with full-body or full-product visibility and comfortable edge safety; keep the full silhouette readable from top to bottom"
    )
    prompt_parts = [
        f"Create a premium ecommerce marketplace main image for {product_name}",
        f"Product category: {category}",
        f"Visual style: {style}",
        composition,
        framing,
        "The output must be image-only with no text overlay and no design layout",
        "No title, no subtitle, no callout, no sticker, no badge, no price tag, no UI, no infographic, no collage, no watermark, no logo",
        "Fill the frame naturally and make the product visually strong, premium, and purchase-driven",
        "Do not crop off key product structure, and keep enough safe space around the subject for later resizing",
        "Avoid turning this into a detail-page poster or typography-based ad",
        "Use realistic lighting, credible materials, and a commercially polished look",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis, include_copy_tone=True))
    if shot_label:
        prompt_parts.append(f"Shot intent: {shot_label}")
    if shot_direction:
        prompt_parts.append(f"Camera/view direction: {shot_direction}")
    if shot_focus:
        prompt_parts.append(f"Primary visual focus for this shot: {shot_focus}")
    if hero_claim:
        prompt_parts.append(f"Emotional direction: {hero_claim}")
    if points:
        prompt_parts.append(f"Keep these product cues consistent: {points}")
    if scenes:
        prompt_parts.append(f"Scene inspiration: {scenes}")
    if constraints:
        prompt_parts.append(f"Do not imply unverifiable claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _save_master_jpeg(source: Image.Image, output_path: Path, width: int, height: int) -> Dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _fit_cover(source.convert("RGB"), width, height)
    rendered.save(output_path, format="JPEG", quality=95, subsampling=0)
    return {"filename": output_path.name, "path": str(output_path), "width": width, "height": height}


def _derive_portrait_master_from_square(
    square_image: Image.Image,
    output_path: Path,
    *,
    width: int = 3072,
    height: int = 4096,
    anchor_x: float = 0.5,
    anchor_y: float = 0.35,
) -> Dict[str, Any]:
    portrait = _cover_with_anchor(square_image.convert("RGB"), width, height, anchor_x=anchor_x, anchor_y=anchor_y)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    portrait.save(output_path, format="JPEG", quality=95, subsampling=0)
    return {"filename": output_path.name, "path": str(output_path), "width": width, "height": height}


def _generate_main_image_assets(
    *,
    client: ComflyClient,
    logger: RunLogger,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    reference_urls: List[str],
) -> Dict[str, Any]:
    master_dir = logger.run_dir / "main_image_masters"
    master_dir.mkdir(parents=True, exist_ok=True)
    shot_plan = _main_image_shot_plan(analysis)
    assets: Dict[str, Any] = {"variants": {}, "shots": [], "warnings": []}
    for shot in shot_plan:
        prompt = _compose_main_image_prompt(analysis, aspect_ratio="1:1", shot=shot)
        generated, attempts = client.generate_image(
            config.image_model,
            prompt,
            "1:1",
            reference_urls,
            f"07_main_image_{shot['index']:02d}",
        )
        logger.record_usage(
            "image",
            config.image_model,
            f"main_image_{shot['key']}",
            payload={"attempts": attempts, "aspect_ratio": "1:1", "shot_index": shot["index"]},
        )
        source = _download_image(generated["url"], retries=5, timeout=180)
        square_master = _save_master_jpeg(
            source,
            master_dir / f"main-{shot['index']:02d}-square-4096.jpg",
            4096,
            4096,
        )
        square_image = _open_local_image(square_master["path"]).convert("RGB")
        portrait_master = _derive_portrait_master_from_square(
            square_image,
            master_dir / f"main-{shot['index']:02d}-portrait-3072x4096.jpg",
            anchor_x=float(shot.get("portrait_anchor_x", 0.5)),
            anchor_y=float(shot.get("portrait_anchor_y", 0.35)),
        )
        portrait_image = _open_local_image(portrait_master["path"]).convert("RGB")
        shot_record = {
            "index": int(shot["index"]),
            "shot_key": str(shot["key"]),
            "label": str(shot["label"]),
            "focus": str(shot.get("focus") or ""),
            "direction": str(shot.get("direction") or ""),
            "hero_anchor": int(shot["index"]) == 1,
            "prompt": prompt,
            "attempts": attempts,
            "generated_image_url": generated["url"],
            "square": {
                "aspect_ratio": "1:1",
                "master": square_master,
                "master_image": square_image,
            },
            "portrait": {
                "aspect_ratio": "3:4",
                "master": portrait_master,
                "master_image": portrait_image,
            },
        }
        assets["shots"].append(shot_record)
        if int(shot["index"]) == 1:
            assets["variants"]["square"] = {
                "aspect_ratio": "1:1",
                "prompt": prompt,
                "attempts": attempts,
                "generated_image_url": generated["url"],
                "master": square_master,
                "master_image": square_image,
            }
            assets["variants"]["portrait"] = {
                "aspect_ratio": "3:4",
                "prompt": prompt,
                "attempts": attempts,
                "generated_image_url": generated["url"],
                "master": portrait_master,
                "master_image": portrait_image,
            }
    return assets


def _sku_image_plan(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    selling_points = _safe_string_list(analysis.get("selling_points"))
    structure = _safe_string_list(analysis.get("structure_features"))
    materials = _safe_string_list(analysis.get("materials"))
    hero_claim = str(analysis.get("hero_claim") or "").strip()
    focus_a = selling_points[0] if selling_points else hero_claim or "核心展示"
    focus_b = selling_points[1] if len(selling_points) > 1 else (structure[0] if structure else focus_a)
    focus_c = structure[0] if structure else (materials[0] if materials else focus_b)
    return [
        {
            "index": 1,
            "key": "front_scene",
            "label": "SKU正面场景",
            "mode": "scene",
            "direction": "front-facing or stable three-quarter view for marketplace SKU gallery",
            "focus": focus_a,
        },
        {
            "index": 2,
            "key": "function_scene",
            "label": "SKU功能角度",
            "mode": "scene",
            "direction": "side, forty-five-degree, or function-emphasis view that reveals structure and usage more clearly",
            "focus": focus_b,
        },
        {
            "index": 3,
            "key": "layout_board",
            "label": "SKU版式图",
            "mode": "layout",
            "direction": "clean local-composed sku board",
            "focus": focus_c,
        },
    ]


def _compose_sku_scene_prompt(analysis: Dict[str, Any], shot: Optional[Dict[str, Any]] = None) -> str:
    category = str(analysis.get("category") or "product").strip()
    product_name = str(analysis.get("product_name") or category or "product").strip()
    style = str(analysis.get("visual_style") or "premium ecommerce photography").strip()
    points = ", ".join(_safe_string_list(analysis.get("selling_points"))[:3])
    scenes = ", ".join(_safe_string_list(analysis.get("usage_scenes"))[:2])
    constraints = ", ".join(_safe_string_list(analysis.get("visual_constraints"))[:3])
    wearable = _looks_like_wearable_category(category)
    shot_focus = str((shot or {}).get("focus") or "").strip()
    shot_direction = str((shot or {}).get("direction") or "").strip()
    shot_label = str((shot or {}).get("label") or "").strip()
    composition = (
        "Create a premium square ecommerce SKU scene image with the full garment clearly visible, naturally worn or presented in scene, and with safe margins"
        if wearable
        else "Create a premium square ecommerce SKU scene image with the product dominant in a believable lifestyle scene"
    )
    prompt_parts = [
        f"Create a premium ecommerce SKU scene image for {product_name}",
        f"Product category: {category}",
        f"Visual style: {style}",
        composition,
        "Output image only with no text overlay and no design layout",
        "No title, no subtitle, no callout, no measurement label, no badge, no UI, no watermark, no logo, no collage",
        "Keep the product visually complete and readable in a square composition",
        "Make the image feel suitable for a marketplace sku gallery slot",
    ]
    prompt_parts.extend(_product_identity_guardrails(analysis))
    prompt_parts.extend(_style_prompt_details(analysis))
    if shot_label:
        prompt_parts.append(f"SKU shot intent: {shot_label}")
    if shot_direction:
        prompt_parts.append(f"Preferred view direction: {shot_direction}")
    if shot_focus:
        prompt_parts.append(f"Primary focus for this SKU image: {shot_focus}")
    if points:
        prompt_parts.append(f"Keep these product cues consistent: {points}")
    if scenes:
        prompt_parts.append(f"Scene inspiration: {scenes}")
    if constraints:
        prompt_parts.append(f"Do not imply unverifiable claims: {constraints}")
    return ". ".join(part for part in prompt_parts if part)


def _primary_size_specs(analysis: Dict[str, Any], limit: int = 3) -> List[str]:
    entries = _spec_entries(analysis.get("specs"))
    if not entries:
        return []
    out: List[str] = []
    for item in entries:
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key and not value:
            continue
        merged = f"{key}:{value}".strip(":")
        if merged:
            out.append(merged)
        if len(out) >= limit:
            break
    return out


def _compose_sku_layout_image(
    *,
    analysis: Dict[str, Any],
    scene_image: Image.Image,
    inset_image: Image.Image,
    size: int = 3072,
) -> Image.Image:
    canvas = _fit_cover(scene_image.convert("RGB"), size, size).convert("RGBA")
    overlay = Image.new("RGBA", (size, size), (255, 255, 255, 20))
    canvas.alpha_composite(overlay)
    draw = ImageDraw.Draw(canvas)

    top_band_h = int(size * 0.16)
    theme = _style_theme(
        analysis,
        "sku_theme",
        {
            "dark": "#6c4028",
            "light": "#fffaf5",
            "accent": "#f28727",
            "text_dark": "#fff9f2",
            "body_dark": "#4c321f",
        },
    )
    dark = theme["dark"]
    light = theme["light"]
    accent = theme["accent"]
    text_dark = theme["text_dark"]
    body_dark = theme["body_dark"]
    draw.rectangle((0, 0, size, top_band_h), fill=dark)

    title_font_size = max(38, size // 18)
    title_font = _font(title_font_size, bold=True)

    margin = int(size * 0.06)
    title_source = str(analysis.get("product_name") or analysis.get("hero_claim") or analysis.get("category") or "SKU图").strip()
    title = _sanitize_copy_text(title_source)
    title_lines = _split_text_by_chars(title, 10, 2)
    if not title_lines:
        title_lines = ["SKU图"]
    title_max_width = size - margin * 2 - int(size * 0.2)
    title_font = _fit_font_to_lines(
        draw,
        title_lines,
        initial_size=title_font_size,
        min_size=max(34, title_font_size - 28),
        max_width=title_max_width,
        bold=True,
    )
    _draw_line_list(draw, (margin, int(size * 0.04)), title_lines, title_font, text_dark, int(size * 0.008))

    return canvas.convert("RGB")


def _generate_sku_assets(
    *,
    client: ComflyClient,
    logger: RunLogger,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    reference_urls: List[str],
    white_bg_assets: Dict[str, Any],
) -> Dict[str, Any]:
    master_dir = logger.run_dir / "sku_image_masters"
    master_dir.mkdir(parents=True, exist_ok=True)
    sku_plan = _sku_image_plan(analysis)
    variants: List[Dict[str, Any]] = []
    scene_variants: List[Dict[str, Any]] = []

    for shot in [item for item in sku_plan if str(item.get("mode") or "") == "scene"]:
        prompt = _compose_sku_scene_prompt(analysis, shot=shot)
        generated, attempts = client.generate_image(
            config.image_model,
            prompt,
            "1:1",
            reference_urls,
            f"08_sku_scene_{shot['index']:02d}",
        )
        logger.record_usage(
            "image",
            config.image_model,
            f"sku_scene_{shot['key']}",
            payload={"attempts": attempts, "aspect_ratio": "1:1", "shot_index": shot["index"]},
        )
        scene_source = _download_image(generated["url"], retries=5, timeout=180)
        scene_master = _save_master_jpeg(scene_source, master_dir / f"sku-{shot['index']:02d}-scene-3072.jpg", 3072, 3072)
        scene_image = _open_local_image(scene_master["path"]).convert("RGB")
        record = {
            "index": int(shot["index"]),
            "shot_key": str(shot["key"]),
            "label": str(shot["label"]),
            "mode": "scene",
            "aspect_ratio": "1:1",
            "generated_image_url": generated["url"],
            "prompt": prompt,
            "attempts": attempts,
            "master": scene_master,
            "master_image": scene_image,
            "focus": str(shot.get("focus") or ""),
        }
        scene_variants.append(record)
        variants.append(record)

    scene_image = scene_variants[0]["master_image"] if scene_variants else None
    inset_image = white_bg_assets.get("white_bg_image") if isinstance(white_bg_assets.get("white_bg_image"), Image.Image) else scene_image
    if not isinstance(scene_image, Image.Image):
        raise PipelineError("SKU scene generation completed without a usable scene image")
    layout_image = _compose_sku_layout_image(
        analysis=analysis,
        scene_image=scene_image,
        inset_image=inset_image if isinstance(inset_image, Image.Image) else scene_image,
        size=3072,
    )
    layout_master = _save_master_jpeg(layout_image, master_dir / "sku-03-layout-3072.jpg", 3072, 3072)
    layout_record = {
        "index": 3,
        "shot_key": "layout_board",
        "label": "SKU版式图",
        "mode": "layout",
        "aspect_ratio": "1:1",
        "generated_image_url": "",
        "prompt": "local_composed_sku_layout",
        "attempts": 1,
        "master": layout_master,
        "master_image": _open_local_image(layout_master["path"]).convert("RGB"),
        "focus": str((sku_plan[-1] or {}).get("focus") or ""),
    }
    variants.append(layout_record)

    return {
        "warnings": [],
        "variants": variants,
        "scene": dict(scene_variants[0]) if scene_variants else {},
        "layout": dict(layout_record),
    }


def _cover_with_anchor(
    image: Image.Image,
    width: int,
    height: int,
    *,
    anchor_x: float = 0.5,
    anchor_y: float = 0.5,
) -> Image.Image:
    source = image.convert("RGB")
    if source.width <= 0 or source.height <= 0:
        return Image.new("RGB", (width, height), "#f5efe8")
    scale = max(width / source.width, height / source.height)
    resized_w = max(width, int(math.ceil(source.width * scale)))
    resized_h = max(height, int(math.ceil(source.height * scale)))
    resized = source.resize((resized_w, resized_h), Image.Resampling.LANCZOS)
    max_left = max(0, resized.width - width)
    max_top = max(0, resized.height - height)
    left = int(round(max_left * max(0.0, min(1.0, anchor_x))))
    top = int(round(max_top * max(0.0, min(1.0, anchor_y))))
    return resized.crop((left, top, left + width, top + height))


def _paste_rounded_panel(
    canvas: Image.Image,
    panel: Image.Image,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    radius: int,
) -> None:
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width, height), radius=radius, fill=255)
    canvas.paste(panel.convert("RGBA"), (x, y), mask)


def _render_showcase_panel(
    source: Image.Image,
    width: int,
    height: int,
    *,
    mode: str = "cover",
    anchor_x: float = 0.5,
    anchor_y: float = 0.5,
    background: str = "#f6f0e8",
) -> Image.Image:
    if mode == "contain":
        panel = Image.new("RGBA", (width, height), background)
        fitted = _fit_contain(source.convert("RGBA"), int(width * 0.82), int(height * 0.82))
        shadow = fitted.copy().filter(ImageFilter.GaussianBlur(radius=max(8, width // 48)))
        alpha = shadow.getchannel("A").point(lambda v: int(v * 0.25))
        shadow.putalpha(alpha)
        sx = (width - fitted.width) // 2
        sy = (height - fitted.height) // 2
        panel.alpha_composite(shadow, (sx + max(8, width // 80), sy + max(8, height // 80)))
        panel.alpha_composite(fitted, (sx, sy))
        return panel
    return _cover_with_anchor(source, width, height, anchor_x=anchor_x, anchor_y=anchor_y).convert("RGBA")


def _build_showcase_source_pool(
    *,
    product_image_rgba: Image.Image,
    main_square_image: Optional[Image.Image],
    main_portrait_image: Optional[Image.Image],
    sku_scene_image: Optional[Image.Image],
    white_bg_image: Optional[Image.Image],
) -> List[Dict[str, Any]]:
    pool: List[Dict[str, Any]] = []

    def add(name: str, image: Optional[Image.Image], *, mode: str, anchors: List[tuple[float, float]]) -> None:
        if not isinstance(image, Image.Image):
            return
        pool.append({"name": name, "image": image, "mode": mode, "anchors": anchors})

    add("main_portrait", main_portrait_image, mode="cover", anchors=[(0.5, 0.22), (0.5, 0.36), (0.5, 0.5), (0.5, 0.62)])
    add("main_square", main_square_image, mode="cover", anchors=[(0.5, 0.24), (0.5, 0.4), (0.5, 0.52), (0.5, 0.64)])
    add("sku_scene", sku_scene_image, mode="cover", anchors=[(0.5, 0.24), (0.5, 0.4), (0.5, 0.54), (0.5, 0.66)])
    add("white_bg", white_bg_image, mode="contain", anchors=[(0.5, 0.5)])
    add("product_cutout", product_image_rgba, mode="contain", anchors=[(0.5, 0.5)])
    return pool


def _showcase_compact_text(value: Any, max_chars: int = 14) -> str:
    text = _sanitize_copy_text(value)
    if not text:
        return ""
    parts = [part.strip() for part in re.split(r"[，,。；;：:、/|]+", text) if part.strip()]
    text = parts[0] if parts else text
    text = re.sub(r"^(这款|本款|采用|具备|主打)", "", text).strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    if compact and len(compact) <= max_chars:
        return compact
    if compact:
        return compact[:max_chars]
    words = text.split()
    if len(words) <= 4:
        return text[:max_chars]
    return " ".join(words[:4])[:max_chars]


def _showcase_text_candidates(values: List[str], *, max_chars: int, min_chars: int = 4) -> List[str]:
    out: List[str] = []
    for value in values:
        compact = _showcase_compact_text(value, max_chars)
        if compact and len(compact) >= min_chars:
            out.append(compact)
    if out:
        return out
    return [item for item in (_showcase_compact_text(value, max_chars) for value in values) if item]


def _showcase_copy_records(analysis: Dict[str, Any], count: int) -> List[Dict[str, str]]:
    selling_points = _sanitize_copy_list(analysis.get("selling_points"), 16)
    structure = _sanitize_copy_list(analysis.get("structure_features"), 12)
    trust_points = _sanitize_copy_list(analysis.get("trust_points"), 12)
    materials = _sanitize_copy_list(analysis.get("materials"), 8)
    care_points = _sanitize_copy_list(analysis.get("care_points"), 8)
    scenes = _sanitize_copy_list(analysis.get("usage_scenes"), 8)
    product_name = _sanitize_copy_text(analysis.get("product_name") or analysis.get("category") or "商品")
    hero_claim = _sanitize_copy_text(analysis.get("hero_claim") or "")
    summary = _sanitize_copy_text(analysis.get("product_summary") or "")

    title_candidates = _showcase_text_candidates(selling_points, max_chars=14, min_chars=4)
    support_candidates = _showcase_text_candidates(structure + trust_points + materials + care_points + scenes, max_chars=12, min_chars=4)
    summary_candidates = _showcase_text_candidates([hero_claim, summary] + trust_points + scenes, max_chars=18, min_chars=6)
    if not title_candidates:
        title_candidates = [_showcase_compact_text(product_name, 14) or "核心卖点"]
    if not support_candidates:
        support_candidates = [_showcase_compact_text(summary or hero_claim or product_name, 12) or "舒适好搭"]
    if not summary_candidates:
        summary_candidates = [_showcase_compact_text(summary or hero_claim or product_name, 18) or "多场景轻松穿搭"]

    records: List[Dict[str, str]] = []
    for idx in range(count):
        title = title_candidates[idx % len(title_candidates)]
        subtitle = support_candidates[idx % len(support_candidates)]
        eyebrow = f"{idx + 1:02d}"
        records.append(
            {
                "title": title or _showcase_compact_text(product_name, 14) or "核心卖点",
                "subtitle": subtitle or _showcase_compact_text(summary or hero_claim or product_name, 12) or "舒适有型",
                "eyebrow": eyebrow,
                "hero_claim": summary_candidates[idx % len(summary_candidates)],
                "summary": summary_candidates[(idx + 1) % len(summary_candidates)],
                "corner": _showcase_compact_text(materials[idx % len(materials)] if materials else subtitle, 4) or "卖点",
            }
        )
    return records


def _render_showcase_card(
    *,
    index: int,
    record: Dict[str, str],
    source_pool: List[Dict[str, Any]],
    analysis: Optional[Dict[str, Any]] = None,
    template_variant: Optional[int] = None,
    theme_override: Optional[Dict[str, str]] = None,
    width: int = 1440,
    height: int = 1920,
) -> Image.Image:
    if not source_pool:
        return Image.new("RGB", (width, height), "#f5efe8")

    scene_sources = [item for item in source_pool if str(item.get("mode") or "") == "cover"] or source_pool
    contain_sources = [item for item in source_pool if str(item.get("mode") or "") == "contain"] or scene_sources

    def pick_source(pool: List[Dict[str, Any]], offset: int) -> Dict[str, Any]:
        return pool[(index + offset) % len(pool)]

    def make_panel(
        offset: int,
        panel_w: int,
        panel_h: int,
        *,
        anchor_idx: int = 0,
        mode_hint: str = "scene",
    ) -> Image.Image:
        pool = contain_sources if mode_hint == "contain" else scene_sources
        source = pick_source(pool, offset)
        anchors = source.get("anchors") or [(0.5, 0.5)]
        anchor = anchors[anchor_idx % len(anchors)]
        return _render_showcase_panel(
            source["image"],
            panel_w,
            panel_h,
            mode=str(source.get("mode") or "cover"),
            anchor_x=float(anchor[0]),
            anchor_y=float(anchor[1]),
        )

    theme = _style_theme(
        analysis or {},
        "showcase_theme",
        {
            "background": "#f3efe9",
            "card_white": "#fbf8f4",
            "text": "#15110d",
            "muted": "#60554a",
            "accent": "#f46c22",
            "chip_bg": "#6e4325",
            "chip_text": "#fffaf4",
            "soft_line": "#ece4da",
        },
    )
    if isinstance(theme_override, dict):
        for key, value in theme_override.items():
            if str(value).strip():
                theme[str(key)] = str(value)
    canvas = Image.new("RGBA", (width, height), theme["background"])
    draw = ImageDraw.Draw(canvas)
    dark = theme["text"]
    muted = theme["muted"]
    accent = theme["accent"]
    white = theme["chip_text"]
    card_white = theme["card_white"]
    soft_line = theme["soft_line"]
    chip_bg = theme["chip_bg"]

    scale = max(0.5, min(width / 1440.0, height / 1920.0))
    title_font = _font(int(68 * scale), bold=True)
    subtitle_font = _font(int(34 * scale), bold=True)
    label_font = _font(int(34 * scale), bold=True)

    def scaled(value: int) -> int:
        return max(1, int(round(value * scale)))

    def caption_text(value: Any, max_chars: int) -> str:
        text = _sanitize_copy_text(value)
        if not text:
            return ""
        if re.search(r"[A-Za-z]", text) and " " in text:
            words: List[str] = []
            for word in text.split():
                candidate = " ".join(words + [word])
                if len(candidate) > max_chars and words:
                    break
                words.append(word)
            return " ".join(words)[:max_chars].strip()
        return _showcase_compact_text(text, max_chars)

    def fit_single_line(text: Any, font: Any, max_width: int, *, max_chars: int, min_size: int) -> tuple[str, Any]:
        line = caption_text(text, max_chars)
        if not line:
            return "", font
        size = getattr(font, "size", min_size)
        while size > min_size:
            candidate_font = _font(size, bold=True)
            bbox = draw.textbbox((0, 0), line, font=candidate_font)
            if bbox[2] - bbox[0] <= max_width:
                return line, candidate_font
            size -= 4
        final_font = _font(min_size, bold=True)
        base = line.rstrip(".")
        while base:
            candidate = base if base == line else base.rstrip() + "..."
            bbox = draw.textbbox((0, 0), candidate, font=final_font)
            if bbox[2] - bbox[0] <= max_width:
                return candidate, final_font
            base = base[:-1].rstrip()
        return "", final_font

    def draw_single_line(pos: tuple[int, int], text: Any, font: Any, fill: str, max_width: int, *, max_chars: int, min_size: int) -> int:
        line, use_font = fit_single_line(text, font, max_width, max_chars=max_chars, min_size=min_size)
        if not line:
            return pos[1]
        draw.text(pos, line, font=use_font, fill=fill)
        bbox = draw.textbbox(pos, line, font=use_font)
        return bbox[3]

    def draw_compact_heading(title: Any, subtitle: Any = "") -> int:
        x = scaled(72)
        y = scaled(62)
        max_text_w = width - x * 2
        title_bottom = draw_single_line((x, y), title, title_font, dark, max_text_w, max_chars=14, min_size=scaled(42))
        subtitle_bottom = title_bottom
        if subtitle:
            subtitle_bottom = draw_single_line(
                (x, title_bottom + scaled(12)),
                subtitle,
                subtitle_font,
                muted,
                max_text_w,
                max_chars=12,
                min_size=scaled(24),
            )
        return max(scaled(168), subtitle_bottom + scaled(28))

    variant = int(template_variant) % 4 if template_variant is not None else index % 4

    if variant == 0:
        outer_margin = scaled(24)
        gap = scaled(18)
        tile_w = (width - outer_margin * 2 - gap) // 2
        tile_h = (height - outer_margin * 2 - gap) // 2
        caption_h = scaled(92)
        panel_h = tile_h - caption_h
        points = [
            record["title"],
            record["subtitle"],
            record["hero_claim"],
            record["summary"],
        ]
        for row in range(2):
            for col in range(2):
                tile_idx = row * 2 + col
                x = outer_margin + col * (tile_w + gap)
                y = outer_margin + row * (tile_h + gap)
                panel = make_panel(tile_idx, tile_w, panel_h, anchor_idx=tile_idx, mode_hint="scene")
                canvas.paste(panel.convert("RGBA"), (x, y))
                draw_single_line(
                    (x + scaled(8), y + panel_h + scaled(20)),
                    points[tile_idx],
                    label_font,
                    dark,
                    tile_w - scaled(16),
                    max_chars=12,
                    min_size=scaled(24),
                )
        return canvas.convert("RGB")

    if variant == 1:
        panel_top = draw_compact_heading(record["title"], record["subtitle"])
        panel_h = height - panel_top
        panel = make_panel(0, width, panel_h, anchor_idx=index, mode_hint="scene")
        canvas.paste(panel.convert("RGBA"), (0, panel_top))
        return canvas.convert("RGB")

    if variant == 2:
        panel_top = draw_compact_heading(record["title"], record["subtitle"])
        panel_h = height - panel_top
        panel = make_panel(1, width, panel_h, anchor_idx=index + 1, mode_hint="scene")
        canvas.paste(panel.convert("RGBA"), (0, panel_top))
        return canvas.convert("RGB")

    panel_top = draw_compact_heading(record["title"], record["subtitle"] or record["hero_claim"])
    panel_h = height - panel_top
    panel = make_panel(2, width, panel_h, anchor_idx=index, mode_hint="scene")
    canvas.paste(panel.convert("RGBA"), (0, panel_top))
    return canvas.convert("RGB")


def _export_suite_bundle(
    *,
    run_dir: Path,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    product_image_rgba: Image.Image,
    page_results: List[Dict[str, Any]],
    main_image_assets: Optional[Dict[str, Any]] = None,
    sku_assets: Optional[Dict[str, Any]] = None,
    white_bg_assets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root_dir = run_dir / SUITE_EXPORT_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    category_dirs = {key: root_dir / dirname for key, dirname in SUITE_EXPORT_CATEGORY_DIRS.items()}
    for folder in category_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(page_results, key=lambda item: int(item.get("index") or 0))
    page_by_slot: Dict[str, Dict[str, Any]] = {}
    for item in sorted_pages:
        slot = str(item.get("slot") or "").strip().lower()
        if slot and slot not in page_by_slot:
            page_by_slot[slot] = item

    def pick_page(*slots: str) -> Dict[str, Any]:
        for slot in slots:
            found = page_by_slot.get(slot.strip().lower())
            if found:
                return found
        return sorted_pages[0]

    cover_page = pick_page("cover", "overview")
    overview_page = pick_page("overview", "feature", "cover")
    sku_layout_page = pick_page("spec_table", "material", "trust", "overview", "feature")
    material_page = pick_page("cover", "scene", "overview")

    suite_categories: Dict[str, List[Dict[str, Any]]] = {
        "main_images": [],
        "sku_images": [],
        "transparent_white_bg": [],
        "detail_images": [],
        "material_images": [],
        "showcase_images": [],
    }
    main_payload = main_image_assets if isinstance(main_image_assets, dict) else {}
    sku_payload = sku_assets if isinstance(sku_assets, dict) else {}
    payload = white_bg_assets if isinstance(white_bg_assets, dict) else {}
    warnings = (
        _safe_string_list(payload.get("warnings"))
        + _safe_string_list(main_payload.get("warnings"))
        + _safe_string_list(sku_payload.get("warnings"))
    )

    cover_rgba = _open_local_image(str(cover_page["local_path"]))
    overview_rgba = _open_local_image(str(overview_page["local_path"]))
    sku_layout_rgba = _open_local_image(str(sku_layout_page["local_path"]))
    material_rgba = _open_local_image(str(material_page["local_path"]))
    main_square = ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).get("square")
    main_portrait = ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).get("portrait")
    main_shots = [item for item in (main_payload.get("shots") or []) if isinstance(item, dict)]
    main_square_image = main_square.get("master_image") if isinstance(main_square, dict) and isinstance(main_square.get("master_image"), Image.Image) else None
    main_portrait_image = main_portrait.get("master_image") if isinstance(main_portrait, dict) and isinstance(main_portrait.get("master_image"), Image.Image) else None
    white_bg_image = payload.get("white_bg_image") if isinstance(payload.get("white_bg_image"), Image.Image) else None
    transparent_image = payload.get("transparent_image") if isinstance(payload.get("transparent_image"), Image.Image) else None
    sku_scene = (sku_payload.get("scene") or {}) if isinstance(sku_payload.get("scene"), dict) else {}
    sku_layout = (sku_payload.get("layout") or {}) if isinstance(sku_payload.get("layout"), dict) else {}
    sku_variants = [item for item in (sku_payload.get("variants") or []) if isinstance(item, dict)]
    sku_scene_image = sku_scene.get("master_image") if isinstance(sku_scene.get("master_image"), Image.Image) else None
    sku_layout_image = sku_layout.get("master_image") if isinstance(sku_layout.get("master_image"), Image.Image) else None
    showcase_pool = _build_showcase_source_pool(
        product_image_rgba=product_image_rgba,
        main_square_image=main_square_image,
        main_portrait_image=main_portrait_image,
        sku_scene_image=sku_scene_image,
        white_bg_image=white_bg_image,
    )

    if _target_enabled(config.output_targets, "main_images"):
        if not isinstance(main_square_image, Image.Image):
            raise PipelineError("Main image target is enabled, but the 1:1 main-image master is missing")
        if not isinstance(main_portrait_image, Image.Image):
            raise PipelineError("Main image target is enabled, but the 3:4 main-image master is missing")
        square_export = _save_cover_jpeg(main_square_image, category_dirs["main_images"] / "1-1440X1440.jpg", 1440, 1440)
        square_export["kind"] = "main_image_square"
        square_export["source"] = "generated_main_image"
        square_export["generated_image_url"] = str((main_square or {}).get("generated_image_url") or "")
        square_export["prompt"] = str((main_square or {}).get("prompt") or "")
        suite_categories["main_images"].append(square_export)

        portrait_export = _save_cover_jpeg(main_portrait_image, category_dirs["main_images"] / "1-1440X1920.jpg", 1440, 1920)
        portrait_export["kind"] = "main_image_portrait"
        portrait_export["source"] = "generated_main_image"
        portrait_export["generated_image_url"] = str((main_portrait or {}).get("generated_image_url") or "")
        portrait_export["prompt"] = str((main_portrait or {}).get("prompt") or "")
        suite_categories["main_images"].append(portrait_export)

    if _target_enabled(config.output_targets, "sku_images"):
        if not isinstance(sku_scene_image, Image.Image):
            raise PipelineError("SKU image target is enabled, but the SKU scene master is missing")
        if not isinstance(sku_layout_image, Image.Image):
            raise PipelineError("SKU image target is enabled, but the SKU layout master is missing")

        scene_export = _save_cover_jpeg(sku_scene_image, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
        scene_export["kind"] = "sku_scene"
        scene_export["source"] = "generated_sku_image"
        scene_export["generated_image_url"] = str(sku_scene.get("generated_image_url") or "")
        scene_export["prompt"] = str(sku_scene.get("prompt") or "")
        suite_categories["sku_images"].append(scene_export)

        layout_export = _save_cover_jpeg(sku_layout_image, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
        layout_export["kind"] = "sku_layout"
        layout_export["source"] = "generated_sku_image"
        layout_export["generated_image_url"] = str(sku_layout.get("generated_image_url") or "")
        layout_export["prompt"] = str(sku_layout.get("prompt") or "")
        suite_categories["sku_images"].append(layout_export)

    if _target_enabled(config.output_targets, "white_bg_image"):
        if not isinstance(white_bg_image, Image.Image):
            raise PipelineError("White-background image target is enabled, but generated white-background output is missing")
        white_bg = _save_cover_jpeg(
            white_bg_image,
            category_dirs["transparent_white_bg"] / "1-白底.jpg",
            800,
            800,
        )
        white_bg["kind"] = "white_bg_image"
        white_bg["generation_mode"] = str(payload.get("generation_mode") or "remote")
        white_bg["generated_image_url"] = str(payload.get("generated_image_url") or "")
        white_bg["prompt"] = str(payload.get("prompt") or "")
        suite_categories["transparent_white_bg"].append(white_bg)

    if _target_enabled(config.output_targets, "transparent_image"):
        if not isinstance(transparent_image, Image.Image):
            raise PipelineError("Transparent image target is enabled, but the transparent asset derived from the generated white-background image is missing")
        if not _image_has_real_transparency(transparent_image):
            raise PipelineError("Transparent image target is enabled, but the derived transparent asset does not contain a usable alpha channel")
        transparent_path = category_dirs["transparent_white_bg"] / "1-透明.png"
        transparent_path.parent.mkdir(parents=True, exist_ok=True)
        transparent_image.convert("RGBA").save(transparent_path, format="PNG")
        transparent = {
            "filename": transparent_path.name,
            "path": str(transparent_path),
            "width": transparent_image.width,
            "height": transparent_image.height,
            "kind": "transparent_image",
            "source": "white_bg_generated",
        }
        suite_categories["transparent_white_bg"].append(transparent)

    if _target_enabled(config.output_targets, "detail_pages"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(
                source,
                category_dirs["detail_images"] / f"详情_{idx:02d}.jpg",
                source.width,
                source.height,
            )
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["detail_images"].append(exported)

    if _target_enabled(config.output_targets, "material_images"):
        material_source = main_portrait_image if isinstance(main_portrait_image, Image.Image) else (
            sku_scene_image if isinstance(sku_scene_image, Image.Image) else material_rgba
        )
        material_source_kind = (
            "generated_main_image"
            if isinstance(main_portrait_image, Image.Image)
            else ("generated_sku_image" if isinstance(sku_scene_image, Image.Image) else "detail_page")
        )
        for width, height in ((513, 750), (800, 1200), (900, 1200)):
            exported = _save_cover_jpeg(material_source, category_dirs["material_images"] / f"{width}X{height}.jpg", width, height)
            exported["kind"] = "material_image"
            exported["source"] = material_source_kind
            suite_categories["material_images"].append(exported)

    if False and _target_enabled(config.output_targets, "showcase_images"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_contain_frame_jpeg(source, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["showcase_images"].append(exported)

    categories_payload: Dict[str, Any] = {}
    for key, items in suite_categories.items():
        folder = category_dirs[key]
        categories_payload[key] = {
            "dirname": folder.name,
            "dir": str(folder),
            "count": len(items),
            "items": [{**item, "relative_path": _relative_to_run(run_dir, Path(str(item["path"])))} for item in items],
        }

    bundle = {
        "preset": SUITE_EXPORT_PRESET,
        "root_dir": str(root_dir),
        "root_relative_path": _relative_to_run(run_dir, root_dir),
        "categories": categories_payload,
        "main_image_assets": {
            "variants": {
                key: {
                    "aspect_ratio": str(value.get("aspect_ratio") or ""),
                    "generated_image_url": str(value.get("generated_image_url") or ""),
                    "prompt": str(value.get("prompt") or ""),
                    "master": (
                        {
                            **dict(value.get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((value.get("master") or {}).get("path") or ""))),
                        }
                        if isinstance(value, dict) and isinstance(value.get("master"), dict)
                        else None
                    ),
                }
                for key, value in ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).items()
                if isinstance(value, dict)
            }
        },
        "summary": {
            "product_name": str(analysis.get("product_name") or ""),
            "hero_claim": str(analysis.get("hero_claim") or ""),
            "sku": config.sku,
        },
        "warnings": warnings,
    }
    (root_dir / "suite_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def _export_suite_bundle(
    *,
    run_dir: Path,
    analysis: Dict[str, Any],
    config: PipelineConfig,
    product_image_rgba: Image.Image,
    page_results: List[Dict[str, Any]],
    main_image_assets: Optional[Dict[str, Any]] = None,
    sku_assets: Optional[Dict[str, Any]] = None,
    white_bg_assets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    root_dir = run_dir / SUITE_EXPORT_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    category_dirs = {key: root_dir / dirname for key, dirname in SUITE_EXPORT_CATEGORY_DIRS.items()}
    for folder in category_dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    sorted_pages = sorted(page_results, key=lambda item: int(item.get("index") or 0))
    page_by_slot: Dict[str, Dict[str, Any]] = {}
    for item in sorted_pages:
        slot = str(item.get("slot") or "").strip().lower()
        if slot and slot not in page_by_slot:
            page_by_slot[slot] = item

    def pick_page(*slots: str) -> Dict[str, Any]:
        for slot in slots:
            found = page_by_slot.get(slot.strip().lower())
            if found:
                return found
        return sorted_pages[0]

    cover_page = pick_page("cover", "overview")
    overview_page = pick_page("overview", "feature", "cover")
    sku_layout_page = pick_page("spec_table", "material", "trust", "overview", "feature")
    material_page = pick_page("cover", "scene", "overview")

    suite_categories: Dict[str, List[Dict[str, Any]]] = {
        "main_images": [],
        "sku_images": [],
        "transparent_white_bg": [],
        "detail_images": [],
        "material_images": [],
        "showcase_images": [],
    }
    main_payload = main_image_assets if isinstance(main_image_assets, dict) else {}
    sku_payload = sku_assets if isinstance(sku_assets, dict) else {}
    payload = white_bg_assets if isinstance(white_bg_assets, dict) else {}
    warnings = (
        _safe_string_list(payload.get("warnings"))
        + _safe_string_list(main_payload.get("warnings"))
        + _safe_string_list(sku_payload.get("warnings"))
    )

    cover_rgba = _open_local_image(str(cover_page["local_path"]))
    overview_rgba = _open_local_image(str(overview_page["local_path"]))
    sku_layout_rgba = _open_local_image(str(sku_layout_page["local_path"]))
    material_rgba = _open_local_image(str(material_page["local_path"]))
    main_square = ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).get("square")
    main_portrait = ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).get("portrait")
    main_shots = [item for item in (main_payload.get("shots") or []) if isinstance(item, dict)]
    main_square_image = main_square.get("master_image") if isinstance(main_square, dict) and isinstance(main_square.get("master_image"), Image.Image) else None
    main_portrait_image = main_portrait.get("master_image") if isinstance(main_portrait, dict) and isinstance(main_portrait.get("master_image"), Image.Image) else None
    white_bg_image = payload.get("white_bg_image") if isinstance(payload.get("white_bg_image"), Image.Image) else None
    transparent_image = payload.get("transparent_image") if isinstance(payload.get("transparent_image"), Image.Image) else None
    sku_scene = (sku_payload.get("scene") or {}) if isinstance(sku_payload.get("scene"), dict) else {}
    sku_layout = (sku_payload.get("layout") or {}) if isinstance(sku_payload.get("layout"), dict) else {}
    sku_variants = [item for item in (sku_payload.get("variants") or []) if isinstance(item, dict)]
    sku_scene_image = sku_scene.get("master_image") if isinstance(sku_scene.get("master_image"), Image.Image) else None
    sku_layout_image = sku_layout.get("master_image") if isinstance(sku_layout.get("master_image"), Image.Image) else None
    showcase_pool = _build_showcase_source_pool(
        product_image_rgba=product_image_rgba,
        main_square_image=main_square_image,
        main_portrait_image=main_portrait_image,
        sku_scene_image=sku_scene_image,
        white_bg_image=white_bg_image,
    )

    if _target_enabled(config.output_targets, "main_images"):
        if main_shots:
            for shot in main_shots:
                square_payload = shot.get("square") if isinstance(shot.get("square"), dict) else {}
                portrait_payload = shot.get("portrait") if isinstance(shot.get("portrait"), dict) else {}
                square_image = square_payload.get("master_image") if isinstance(square_payload.get("master_image"), Image.Image) else None
                portrait_image = portrait_payload.get("master_image") if isinstance(portrait_payload.get("master_image"), Image.Image) else None
                shot_index = int(shot.get("index") or len(suite_categories["main_images"]) + 1)
                if not isinstance(square_image, Image.Image) or not isinstance(portrait_image, Image.Image):
                    raise PipelineError(f"Main image shot {shot_index} is missing square or portrait master output")
                square_export = _save_cover_jpeg(square_image, category_dirs["main_images"] / f"{shot_index}-1440X1440.jpg", 1440, 1440)
                square_export["kind"] = "main_image_square"
                square_export["source"] = "generated_main_image"
                square_export["shot_index"] = shot_index
                square_export["shot_key"] = str(shot.get("shot_key") or "")
                square_export["shot_label"] = str(shot.get("label") or "")
                square_export["generated_image_url"] = str(shot.get("generated_image_url") or "")
                square_export["prompt"] = str(shot.get("prompt") or "")
                suite_categories["main_images"].append(square_export)

                portrait_export = _save_cover_jpeg(portrait_image, category_dirs["main_images"] / f"{shot_index}-1440X1920.jpg", 1440, 1920)
                portrait_export["kind"] = "main_image_portrait"
                portrait_export["source"] = "generated_main_image"
                portrait_export["shot_index"] = shot_index
                portrait_export["shot_key"] = str(shot.get("shot_key") or "")
                portrait_export["shot_label"] = str(shot.get("label") or "")
                portrait_export["generated_image_url"] = str(shot.get("generated_image_url") or "")
                portrait_export["prompt"] = str(shot.get("prompt") or "")
                suite_categories["main_images"].append(portrait_export)
        else:
            if not isinstance(main_square_image, Image.Image):
                raise PipelineError("Main image target is enabled, but the 1:1 main-image master is missing")
            if not isinstance(main_portrait_image, Image.Image):
                raise PipelineError("Main image target is enabled, but the 3:4 main-image master is missing")
            square_export = _save_cover_jpeg(main_square_image, category_dirs["main_images"] / "1-1440X1440.jpg", 1440, 1440)
            square_export["kind"] = "main_image_square"
            square_export["source"] = "generated_main_image"
            square_export["generated_image_url"] = str((main_square or {}).get("generated_image_url") or "")
            square_export["prompt"] = str((main_square or {}).get("prompt") or "")
            suite_categories["main_images"].append(square_export)

            portrait_export = _save_cover_jpeg(main_portrait_image, category_dirs["main_images"] / "1-1440X1920.jpg", 1440, 1920)
            portrait_export["kind"] = "main_image_portrait"
            portrait_export["source"] = "generated_main_image"
            portrait_export["generated_image_url"] = str((main_portrait or {}).get("generated_image_url") or "")
            portrait_export["prompt"] = str((main_portrait or {}).get("prompt") or "")
            suite_categories["main_images"].append(portrait_export)

    if _target_enabled(config.output_targets, "sku_images"):
        if sku_variants:
            for item in sku_variants:
                master_image = item.get("master_image") if isinstance(item.get("master_image"), Image.Image) else None
                if not isinstance(master_image, Image.Image):
                    raise PipelineError(f"SKU image variant {item.get('shot_key') or item.get('index') or '?'} is missing master output")
                item_index = int(item.get("index") or len(suite_categories["sku_images"]) + 1)
                filename = (
                    f"SKU场景-{item_index:02d}.jpg"
                    if str(item.get("mode") or "") == "scene"
                    else f"SKU带版式-{item_index:02d}.jpg"
                )
                exported = _save_cover_jpeg(master_image, category_dirs["sku_images"] / filename, 1440, 1440)
                exported["kind"] = "sku_scene" if str(item.get("mode") or "") == "scene" else "sku_layout"
                exported["source"] = "generated_sku_image" if str(item.get("mode") or "") == "scene" else "local_composed_sku_layout"
                exported["shot_index"] = item_index
                exported["shot_key"] = str(item.get("shot_key") or "")
                exported["shot_label"] = str(item.get("label") or "")
                exported["generated_image_url"] = str(item.get("generated_image_url") or "")
                exported["prompt"] = str(item.get("prompt") or "")
                suite_categories["sku_images"].append(exported)
        else:
            if not isinstance(sku_scene_image, Image.Image):
                raise PipelineError("SKU image target is enabled, but the SKU scene master is missing")
            if not isinstance(sku_layout_image, Image.Image):
                raise PipelineError("SKU image target is enabled, but the SKU layout master is missing")
            scene_export = _save_cover_jpeg(sku_scene_image, category_dirs["sku_images"] / "SKU场景.jpg", 1440, 1440)
            scene_export["kind"] = "sku_scene"
            scene_export["source"] = "generated_sku_image"
            scene_export["generated_image_url"] = str(sku_scene.get("generated_image_url") or "")
            scene_export["prompt"] = str(sku_scene.get("prompt") or "")
            suite_categories["sku_images"].append(scene_export)

            layout_export = _save_cover_jpeg(sku_layout_image, category_dirs["sku_images"] / "SKU带版式.jpg", 1440, 1440)
            layout_export["kind"] = "sku_layout"
            layout_export["source"] = "generated_sku_image"
            layout_export["generated_image_url"] = str(sku_layout.get("generated_image_url") or "")
            layout_export["prompt"] = str(sku_layout.get("prompt") or "")
            suite_categories["sku_images"].append(layout_export)

    if _target_enabled(config.output_targets, "white_bg_image"):
        if not isinstance(white_bg_image, Image.Image):
            raise PipelineError("White-background image target is enabled, but generated white-background output is missing")
        white_bg = _save_cover_jpeg(white_bg_image, category_dirs["transparent_white_bg"] / "1-白底.jpg", 800, 800)
        white_bg["kind"] = "white_bg_image"
        white_bg["generation_mode"] = str(payload.get("generation_mode") or "remote")
        white_bg["generated_image_url"] = str(payload.get("generated_image_url") or "")
        white_bg["prompt"] = str(payload.get("prompt") or "")
        suite_categories["transparent_white_bg"].append(white_bg)

    if _target_enabled(config.output_targets, "transparent_image"):
        if not isinstance(transparent_image, Image.Image):
            raise PipelineError("Transparent image target is enabled, but the transparent asset derived from the generated white-background image is missing")
        if not _image_has_real_transparency(transparent_image):
            raise PipelineError("Transparent image target is enabled, but the derived transparent asset does not contain a usable alpha channel")
        transparent_path = category_dirs["transparent_white_bg"] / "1-透明.png"
        transparent_path.parent.mkdir(parents=True, exist_ok=True)
        transparent_image.convert("RGBA").save(transparent_path, format="PNG")
        transparent = {
            "filename": transparent_path.name,
            "path": str(transparent_path),
            "width": transparent_image.width,
            "height": transparent_image.height,
            "kind": "transparent_image",
            "source": "white_bg_generated",
        }
        suite_categories["transparent_white_bg"].append(transparent)

    if _target_enabled(config.output_targets, "detail_pages"):
        for idx, page in enumerate(sorted_pages, start=1):
            source = _open_local_image(str(page["local_path"]))
            exported = _save_cover_jpeg(source, category_dirs["detail_images"] / f"详情_{idx:02d}.jpg", source.width, source.height)
            exported["page_index"] = int(page.get("index") or idx)
            exported["slot"] = str(page.get("slot") or "")
            suite_categories["detail_images"].append(exported)

    if _target_enabled(config.output_targets, "material_images"):
        material_source = main_portrait_image if isinstance(main_portrait_image, Image.Image) else (
            sku_scene_image if isinstance(sku_scene_image, Image.Image) else material_rgba
        )
        material_source_kind = (
            "generated_main_image"
            if isinstance(main_portrait_image, Image.Image)
            else ("generated_sku_image" if isinstance(sku_scene_image, Image.Image) else "detail_page")
        )
        for width, height in ((513, 750), (800, 1200), (900, 1200)):
            exported = _save_cover_jpeg(material_source, category_dirs["material_images"] / f"{width}X{height}.jpg", width, height)
            exported["kind"] = "material_image"
            exported["source"] = material_source_kind
            suite_categories["material_images"].append(exported)

    if _target_enabled(config.output_targets, "showcase_images"):
        showcase_count = _showcase_target_count(analysis, len(sorted_pages), config)
        showcase_sequence = _showcase_variant_sequence(config)
        showcase_theme = _showcase_theme_override(config)
        showcase_records = _showcase_copy_records(analysis, showcase_count)
        for idx, record in enumerate(showcase_records, start=1):
            rendered = _render_showcase_card(
                index=idx - 1,
                record=record,
                source_pool=showcase_pool,
                analysis=analysis,
                template_variant=showcase_sequence[(idx - 1) % len(showcase_sequence)],
                theme_override=showcase_theme,
                width=1440,
                height=1920,
            )
            exported = _save_cover_jpeg(rendered, category_dirs["showcase_images"] / f"橱窗-{idx}.jpg", 1440, 1920)
            exported["page_index"] = idx
            exported["slot"] = "showcase_card"
            exported["kind"] = "showcase_image"
            exported["source"] = "local_showcase_layout"
            exported["title"] = record.get("title") or ""
            exported["showcase_template_id"] = config.showcase_template_id
            exported["template_variant"] = showcase_sequence[(idx - 1) % len(showcase_sequence)]
            suite_categories["showcase_images"].append(exported)

    categories_payload: Dict[str, Any] = {}
    for key, items in suite_categories.items():
        folder = category_dirs[key]
        categories_payload[key] = {
            "dirname": folder.name,
            "dir": str(folder),
            "count": len(items),
            "items": [{**item, "relative_path": _relative_to_run(run_dir, Path(str(item["path"])))} for item in items],
        }

    bundle = {
        "preset": SUITE_EXPORT_PRESET,
        "root_dir": str(root_dir),
        "root_relative_path": _relative_to_run(run_dir, root_dir),
        "categories": categories_payload,
        "main_image_assets": {
            "shots": [
                {
                    "index": int(item.get("index") or 0),
                    "shot_key": str(item.get("shot_key") or ""),
                    "label": str(item.get("label") or ""),
                    "focus": str(item.get("focus") or ""),
                    "generated_image_url": str(item.get("generated_image_url") or ""),
                    "prompt": str(item.get("prompt") or ""),
                    "square": (
                        {
                            **dict((item.get("square") or {}).get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((((item.get("square") or {}).get("master") or {}).get("path") or "")))),
                        }
                        if isinstance(item.get("square"), dict) and isinstance((item.get("square") or {}).get("master"), dict)
                        else None
                    ),
                    "portrait": (
                        {
                            **dict((item.get("portrait") or {}).get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((((item.get("portrait") or {}).get("master") or {}).get("path") or "")))),
                        }
                        if isinstance(item.get("portrait"), dict) and isinstance((item.get("portrait") or {}).get("master"), dict)
                        else None
                    ),
                }
                for item in main_shots
            ],
            "variants": {
                key: {
                    "aspect_ratio": str(value.get("aspect_ratio") or ""),
                    "generated_image_url": str(value.get("generated_image_url") or ""),
                    "prompt": str(value.get("prompt") or ""),
                    "master": (
                        {
                            **dict(value.get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((value.get("master") or {}).get("path") or ""))),
                        }
                        if isinstance(value, dict) and isinstance(value.get("master"), dict)
                        else None
                    ),
                }
                for key, value in ((main_payload.get("variants") or {}) if isinstance(main_payload.get("variants"), dict) else {}).items()
                if isinstance(value, dict)
            }
        },
        "sku_assets": {
            "variants": [
                {
                    "index": int(item.get("index") or 0),
                    "shot_key": str(item.get("shot_key") or ""),
                    "label": str(item.get("label") or ""),
                    "mode": str(item.get("mode") or ""),
                    "generated_image_url": str(item.get("generated_image_url") or ""),
                    "prompt": str(item.get("prompt") or ""),
                    "master": (
                        {
                            **dict(item.get("master") or {}),
                            "relative_path": _relative_to_run(run_dir, Path(str((item.get("master") or {}).get("path") or ""))),
                        }
                        if isinstance(item.get("master"), dict)
                        else None
                    ),
                }
                for item in sku_variants
            ],
            "scene": {
                "aspect_ratio": str(sku_scene.get("aspect_ratio") or ""),
                "generated_image_url": str(sku_scene.get("generated_image_url") or ""),
                "prompt": str(sku_scene.get("prompt") or ""),
                "master": (
                    {
                        **dict(sku_scene.get("master") or {}),
                        "relative_path": _relative_to_run(run_dir, Path(str((sku_scene.get("master") or {}).get("path") or ""))),
                    }
                    if isinstance(sku_scene.get("master"), dict)
                    else None
                ),
            },
            "layout": {
                "aspect_ratio": str(sku_layout.get("aspect_ratio") or ""),
                "generated_image_url": str(sku_layout.get("generated_image_url") or ""),
                "prompt": str(sku_layout.get("prompt") or ""),
                "master": (
                    {
                        **dict(sku_layout.get("master") or {}),
                        "relative_path": _relative_to_run(run_dir, Path(str((sku_layout.get("master") or {}).get("path") or ""))),
                    }
                    if isinstance(sku_layout.get("master"), dict)
                    else None
                ),
            },
        },
        "style_preset": (
            {
                "style_id": str((analysis.get("style_preset") or {}).get("style_id") or analysis.get("style_id") or ""),
                "display_name": str((analysis.get("style_preset") or {}).get("display_name") or ""),
                "palette": _safe_string_list((analysis.get("style_preset") or {}).get("palette")),
                "materials": _safe_string_list((analysis.get("style_preset") or {}).get("materials")),
            }
            if isinstance(analysis.get("style_preset"), dict) or analysis.get("style_id")
            else None
        ),
        "detail_template": {
            "template_id": config.detail_template_id,
            "display_name": str((config.template_config or {}).get("display_name") or ""),
        },
        "showcase_template": {
            "template_id": config.showcase_template_id,
            "display_name": str((config.showcase_template_config or {}).get("display_name") or ""),
            "count_source": str((config.showcase_template_config or {}).get("count_source") or ""),
            "variant_sequence": _showcase_variant_sequence(config),
        },
        "summary": {
            "product_name": str(analysis.get("product_name") or ""),
            "hero_claim": str(analysis.get("hero_claim") or ""),
            "sku": config.sku,
            "style_id": str(analysis.get("style_id") or ""),
        },
        "warnings": warnings,
    }
    (root_dir / "suite_manifest.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return bundle


def _write_cli_output(payload: Dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    stdout = os.sys.stdout
    try:
        stdout.write(rendered)
    except UnicodeEncodeError:
        stdout.buffer.write(rendered.encode("utf-8"))


if __name__ == "__main__":
    raw = _load_cli_input()
    _write_cli_output(handler(Args(raw)))

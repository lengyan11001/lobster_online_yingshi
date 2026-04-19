"""Comfly 带货「整包」流水线：运行 skills/comfly_veo3_daihuo_video 内原 OpenClaw 技能脚本，与 comfly.daihuo 分步 API 独立。"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..models import Asset

from ..api.assets import get_asset_public_url
from .comfly_veo_exec import _comfly_upload_failure_detail

logger = logging.getLogger(__name__)

_pipeline_module = None
# importlib 动态加载时须在 exec_module 前注册到 sys.modules，否则 @dataclass 内 get_type_hints
# 找不到模块 globalns，会报 AttributeError: 'NoneType' object has no attribute '__dict__'（见 CPython dataclasses._is_type）
_DAIHUO_PIPELINE_MODULE_NAME = "lobster_comfly_daihuo_pipeline"


def _lobster_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _pipeline_script_path() -> Path:
    return _lobster_root() / "skills" / "comfly_veo3_daihuo_video" / "scripts" / "comfly_storyboard_pipeline.py"


def _bundled_ffmpeg_exe() -> Optional[str]:
    p = _lobster_root() / "skills" / "comfly_veo3_daihuo_video" / "tools" / "ffmpeg" / "windows" / "ffmpeg.exe"
    if sys.platform == "win32" and p.is_file():
        return str(p)
    return None


def _api_base_for_pipeline(api_base: str) -> str:
    """技能脚本在 host 根下拼 /v1、/v2；LOBSTER 常配 https://ai.comfly.chat/v1，需去掉尾部 /v1。"""
    b = (api_base or "").strip().rstrip("/")
    if b.lower().endswith("/v1"):
        return b[:-3].rstrip("/")
    return b


def _load_pipeline_module():
    global _pipeline_module
    if _pipeline_module is not None:
        return _pipeline_module
    script = _pipeline_script_path()
    if not script.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"未找到带货整包流水线脚本: {script}（请确认已解压技能包到 skills/comfly_veo3_daihuo_video）",
        )
    spec = importlib.util.spec_from_file_location(_DAIHUO_PIPELINE_MODULE_NAME, script)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="无法加载 comfly_storyboard_pipeline 模块")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_DAIHUO_PIPELINE_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    _pipeline_module = mod
    return mod


def resolve_product_image_for_pipeline(
    *,
    user_id: int,
    db: Session,
    request: Request,
    asset_id: Optional[str],
    image_url: Optional[str],
) -> str:
    u = (image_url or "").strip()
    if u:
        if not (u.startswith("http://") or u.startswith("https://")):
            raise HTTPException(status_code=400, detail="image_url 须为 http(s) 公网直链")
        return u
    aid = (asset_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="请提供 asset_id（素材库）或 image_url（公网图链）")
    url = get_asset_public_url(aid, user_id, request, db)
    if not url:
        raise HTTPException(
            status_code=400,
            detail=_comfly_upload_failure_detail(aid, user_id, db),
        )
    return url


def build_pipeline_input(
    *,
    product_image: str,
    api_key: str,
    api_base: str,
    merge_clips: bool,
    storyboard_count: Optional[int],
    output_dir: Optional[str],
    platform: str,
    country: str,
    language: str,
    image_request_style: Optional[str] = None,
) -> Dict[str, Any]:
    base = _api_base_for_pipeline(api_base)
    inp: Dict[str, Any] = {
        "product_image": product_image,
        "apikey": api_key,
        "base_url": base,
        "merge_clips": merge_clips,
    }
    if storyboard_count is not None:
        inp["storyboard_count"] = int(storyboard_count)
    if output_dir:
        inp["output_dir"] = output_dir
    if (platform or "").strip():
        inp["platform"] = platform.strip()
    if (country or "").strip():
        inp["country"] = country.strip()
    if (language or "").strip():
        inp["language"] = language.strip()
    ff = _bundled_ffmpeg_exe()
    if ff:
        inp["ffmpeg_path"] = ff
    # 与 comfly_daihuo API 说明一致：默认 openai_images；脚本内 comfly 为旧版 body（aspect_ratio）
    irs = (image_request_style or "").strip() or "openai_images"
    inp["image_request_style"] = irs
    return inp


def run_storyboard_pipeline_sync(inp: Dict[str, Any]) -> Dict[str, Any]:
    mod = _load_pipeline_module()
    return mod.run_pipeline(inp)


def save_merged_local_pipeline_video(
    *,
    local_path: str,
    user_id: int,
    db: Session,
    video_model: str,
) -> Optional[Dict[str, Any]]:
    """将流水线合并后的本地 mp4 写入素材库（一条成片）。失败返回 None。"""
    p = Path((local_path or "").strip())
    if not p.is_file():
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    from ..api import assets as assets_mod

    aid, fname, fsize, tos_url = assets_mod._save_bytes_or_tos(raw, ".mp4", "video/mp4")
    source_url = (tos_url or "").strip() or ""
    asset = Asset(
        asset_id=aid,
        user_id=user_id,
        filename=fname,
        media_type="video",
        file_size=fsize,
        source_url=source_url or None,
        prompt="带货流水线合并成片",
        model=(video_model or "")[:128] or None,
        tags="auto,comfly.daihuo.pipeline,merged_final",
        meta={"origin": "daihuo_merged", "merged_path": str(p.resolve())},
    )
    db.add(asset)
    db.commit()
    row = {
        "asset_id": aid,
        "filename": fname,
        "media_type": "video",
        "file_size": fsize,
        "source_url": source_url,
    }
    return {"source_url": source_url, "task_id": "merged_final", "asset": row}


def collect_video_urls_from_pipeline_result(data: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """返回 [(url, task_id, title_hint), ...] 供 save-url 入库。优先与流水线 final_video 对齐。"""
    fv = data.get("final_video")
    if isinstance(fv, dict):
        u = (fv.get("url") or "").strip()
        if u.startswith("http"):
            return [(u, "final", "成片")]
    out: List[Tuple[str, str, str]] = []
    for shot in data.get("completed_shots") or []:
        if not isinstance(shot, dict):
            continue
        url = (shot.get("mp4url") or "").strip()
        if not url.startswith("http"):
            continue
        tid = (shot.get("video_task_id") or "").strip()
        title = (shot.get("title_cn") or shot.get("hook_line_cn") or "").strip() or f"shot_{shot.get('index', '')}"
        out.append((url, tid, title[:200]))
    return out

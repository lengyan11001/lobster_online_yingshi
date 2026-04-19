"""Runner helpers for the Comfly ecommerce detail-image pipeline."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..api.assets import get_asset_public_url
from .comfly_veo_exec import _comfly_upload_failure_detail

_PIPELINE_MODULE = None
_PIPELINE_MODULE_NAME = "lobster_comfly_ecommerce_detail_pipeline"


def _lobster_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _pipeline_script_path() -> Path:
    return _lobster_root() / "skills" / "comfly_ecommerce_detail" / "scripts" / "comfly_ecommerce_detail_pipeline.py"


def _pipeline_module_path() -> Path:
    script = _pipeline_script_path()
    if script.is_file():
        return script
    pycache_dir = script.parent / "__pycache__"
    if pycache_dir.is_dir():
        candidates = sorted(pycache_dir.glob("comfly_ecommerce_detail_pipeline*.pyc"))
        if candidates:
            return candidates[-1]
    return script


def _load_pipeline_module():
    global _PIPELINE_MODULE
    if _PIPELINE_MODULE is not None:
        return _PIPELINE_MODULE
    module_path = _pipeline_module_path()
    if not module_path.is_file():
        raise HTTPException(status_code=503, detail=f"未找到电商详情图流水线脚本: {module_path}")
    spec = importlib.util.spec_from_file_location(_PIPELINE_MODULE_NAME, module_path)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=503, detail="无法加载电商详情图流水线模块")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PIPELINE_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    _PIPELINE_MODULE = mod
    return mod


def resolve_public_image_for_pipeline(
    *,
    user_id: int,
    db: Session,
    request: Request,
    asset_id: Optional[str],
    image_url: Optional[str],
) -> str:
    url = (image_url or "").strip()
    if url:
        if not (url.startswith("http://") or url.startswith("https://")):
            raise HTTPException(status_code=400, detail="image_url 必须是 http(s) 公网链接")
        return url
    aid = (asset_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="请提供 asset_id 或 image_url")
    public_url = get_asset_public_url(aid, user_id, request, db)
    if not public_url:
        raise HTTPException(status_code=400, detail=_comfly_upload_failure_detail(aid, user_id, db))
    return public_url


def resolve_reference_images_for_pipeline(
    *,
    user_id: int,
    db: Session,
    request: Request,
    asset_ids: Optional[List[str]],
    image_urls: Optional[List[str]],
) -> List[str]:
    urls: List[str] = []
    for url in image_urls or []:
        clean = str(url or "").strip()
        if not clean:
            continue
        if not (clean.startswith("http://") or clean.startswith("https://")):
            raise HTTPException(status_code=400, detail=f"reference_image_urls 包含非 http(s) 链接: {clean}")
        if clean not in urls:
            urls.append(clean)
    for asset_id in asset_ids or []:
        aid = str(asset_id or "").strip()
        if not aid:
            continue
        public_url = get_asset_public_url(aid, user_id, request, db)
        if not public_url:
            raise HTTPException(status_code=400, detail=_comfly_upload_failure_detail(aid, user_id, db))
        if public_url not in urls:
            urls.append(public_url)
    return urls


def build_pipeline_input(
    *,
    product_image: str,
    reference_images: List[str],
    sku: Optional[str],
    selling_points: Optional[List[Dict[str, object]]],
    specs: Optional[Dict[str, object]],
    style: Optional[str],
    style_reference_images: Optional[List[str]],
    icon_assets: Optional[List[Dict[str, object]]],
    scene_preferences: Optional[Dict[str, object]],
    output_targets: Optional[Dict[str, object]],
    detail_template_id: Optional[str],
    showcase_template_id: Optional[str],
    main_image_count: Optional[int],
    sku_image_count: Optional[int],
    listing_category: Optional[str],
    export_name_prefix: Optional[str],
    showcase_count: Optional[int],
    material_image_count: Optional[int],
    brand: Optional[str],
    compliance_notes: Optional[List[str]],
    api_key: str,
    api_base: str,
    analysis_model: Optional[str],
    image_model: Optional[str],
    page_count: Optional[int],
    output_dir: Optional[str],
    product_name_hint: Optional[str],
    product_direction_hint: Optional[str],
    platform: str,
    country: str,
    language: str,
) -> Dict[str, object]:
    base = (api_base or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3].rstrip("/")
    inp: Dict[str, object] = {
        "product_image": product_image,
        "reference_images": reference_images,
        "apikey": api_key,
        "base_url": base,
    }
    if (sku or "").strip():
        inp["sku"] = sku.strip()
    if selling_points:
        inp["selling_points"] = selling_points
    if specs:
        inp["specs"] = specs
    if (style or "").strip():
        inp["style"] = style.strip()
    if style_reference_images:
        inp["style_reference_images"] = [str(item).strip() for item in style_reference_images if str(item).strip()]
    if icon_assets:
        inp["icon_assets"] = icon_assets
    if scene_preferences:
        inp["scene_preferences"] = scene_preferences
    if output_targets:
        inp["output_targets"] = output_targets
    if (detail_template_id or "").strip():
        inp["detail_template_id"] = detail_template_id.strip()
    if (showcase_template_id or "").strip():
        inp["showcase_template_id"] = showcase_template_id.strip()
    if main_image_count is not None:
        inp["main_image_count"] = int(main_image_count)
    if sku_image_count is not None:
        inp["sku_image_count"] = int(sku_image_count)
    if (listing_category or "").strip():
        inp["listing_category"] = listing_category.strip()
    if (export_name_prefix or "").strip():
        inp["export_name_prefix"] = export_name_prefix.strip()
    if showcase_count is not None:
        inp["showcase_count"] = int(showcase_count)
    if material_image_count is not None:
        inp["material_image_count"] = int(material_image_count)
    if (brand or "").strip():
        inp["brand"] = brand.strip()
    if compliance_notes:
        inp["compliance_notes"] = [str(item).strip() for item in compliance_notes if str(item).strip()]
    if analysis_model:
        inp["analysis_model"] = analysis_model
    if image_model:
        inp["image_model"] = image_model
    if page_count is not None:
        inp["page_count"] = int(page_count)
    if output_dir:
        inp["output_dir"] = output_dir
    if (product_name_hint or "").strip():
        inp["product_name_hint"] = product_name_hint.strip()
    if (product_direction_hint or "").strip():
        inp["product_direction_hint"] = product_direction_hint.strip()
    if (platform or "").strip():
        inp["platform"] = platform.strip()
    if (country or "").strip():
        inp["country"] = country.strip()
    if (language or "").strip():
        inp["language"] = language.strip()
    return inp


def run_pipeline_sync(inp: Dict[str, object]) -> Dict[str, object]:
    return _load_pipeline_module().run_pipeline(inp)

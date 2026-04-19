"""电商详情图流水线 API：商品图 -> 多张详情页 -> 长图 -> 素材入库。"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sys
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..db import SessionLocal, get_db
from ..models import Asset, EcommerceDetailJob
from ..services.comfly_ecommerce_detail_job_store import (
    create_job_record,
    get_job,
    read_manifest_progress,
    update_job,
)
from ..services.comfly_ecommerce_detail_pipeline_runner import (
    build_pipeline_input,
    _load_pipeline_module,
    resolve_public_image_for_pipeline,
    resolve_reference_images_for_pipeline,
    run_pipeline_sync,
)
from ..services.comfly_veo_exec import LOCAL_COMFLY_CONFIG_USER_ID, _resolve_comfly_credentials
from .assets import _save_bytes_or_tos, build_asset_file_url
from .auth import _ServerUser, get_current_user_media_edit

logger = logging.getLogger(__name__)
router = APIRouter()


class EcommerceProductImageItem(BaseModel):
    role: str = Field("front", description="素材角色，如 front / side / back / detail")
    asset_id: Optional[str] = Field(None, description="素材 ID，与 image_url 二选一")
    image_url: Optional[str] = Field(None, description="素材公网 URL，与 asset_id 二选一")
    local_path: Optional[str] = Field(None, description="本机临时图片路径，与 asset_id / image_url 三选一")


class EcommerceSellingPointItem(BaseModel):
    title: str
    description: str = ""
    icon: str = ""
    priority: Optional[int] = None


class EcommerceScenePreferences(BaseModel):
    include_pet: Optional[bool] = None
    pet_type: str = ""
    include_human: Optional[bool] = None
    human_type: str = ""
    decor_tags: List[str] = Field(default_factory=list)


class EcommerceOutputTargets(BaseModel):
    main_images: Optional[bool] = None
    sku_images: Optional[bool] = None
    transparent_image: Optional[bool] = None
    white_bg_image: Optional[bool] = None
    detail_pages: Optional[bool] = None
    material_images: Optional[bool] = None
    showcase_images: Optional[bool] = None


class EcommerceIconAssetItem(BaseModel):
    icon: str
    asset_id: Optional[str] = Field(None, description="图标素材 ID，与 image_url 二选一")
    image_url: Optional[str] = Field(None, description="图标公网 URL，与 asset_id 二选一")
    local_path: Optional[str] = Field(None, description="本机临时图标路径，与 asset_id / image_url 三选一")


class EcommerceDetailPipelinePayload(BaseModel):
    asset_id: Optional[str] = Field(None, description="商品主图素材 ID，与 image_url 二选一")
    image_url: Optional[str] = Field(None, description="商品主图公网 URL，与 asset_id 二选一")
    local_path: Optional[str] = Field(None, description="本机临时主图路径，与 asset_id / image_url 三选一")
    product_images: List[EcommerceProductImageItem] = Field(default_factory=list, description="结构化商品图列表，优先于 asset_id / image_url")
    product_name_hint: str = ""
    product_direction_hint: str = ""
    listing_category: str = ""
    export_name_prefix: str = ""
    reference_asset_ids: List[str] = Field(default_factory=list, description="补充参考图素材 ID")
    reference_image_urls: List[str] = Field(default_factory=list, description="补充参考图公网 URL")
    reference_local_paths: List[str] = Field(default_factory=list, description="本机临时参考图路径")
    style_reference_asset_ids: List[str] = Field(default_factory=list, description="风格参考图素材 ID")
    style_reference_image_urls: List[str] = Field(default_factory=list, description="风格参考图公网 URL")
    style_reference_local_paths: List[str] = Field(default_factory=list, description="本机临时风格参考图路径")
    sku: str = ""
    selling_points: List[EcommerceSellingPointItem] = Field(default_factory=list)
    specs: Dict[str, Any] = Field(default_factory=dict)
    style: str = ""
    icon_assets: List[EcommerceIconAssetItem] = Field(default_factory=list)
    scene_preferences: Optional[EcommerceScenePreferences] = None
    output_targets: Optional[EcommerceOutputTargets] = None
    detail_template_id: str = ""
    showcase_template_id: str = ""
    showcase_count: Optional[int] = Field(None, ge=1, le=20)
    brand: str = ""
    compliance_notes: List[str] = Field(default_factory=list)
    page_count: Optional[int] = Field(12, ge=10, le=16)
    auto_save: bool = True
    platform: str = ""
    country: str = ""
    language: str = ""
    analysis_model: Optional[str] = None
    image_model: Optional[str] = None
    output_dir: Optional[str] = None
    isolate_job_dir: bool = True


class EcommerceDetailRunBody(BaseModel):
    payload: EcommerceDetailPipelinePayload


class EcommerceShowcaseEditPayload(BaseModel):
    page_index: int = Field(..., ge=1, description="橱窗图页码，从 1 开始")
    title: Optional[str] = None
    subtitle: Optional[str] = None
    hero_claim: Optional[str] = None
    summary: Optional[str] = None
    corner: Optional[str] = None


class EcommerceShowcaseEditBody(BaseModel):
    payload: EcommerceShowcaseEditPayload
    result: Dict[str, Any] = Field(default_factory=dict)


class EcommerceDetailEditPayload(BaseModel):
    page_index: int = Field(..., ge=1, description="详情图页码，从 1 开始")
    title: Optional[str] = None
    subtitle: Optional[str] = None
    highlights: List[str] = Field(default_factory=list)
    footer: Optional[str] = None


class EcommerceDetailEditBody(BaseModel):
    payload: EcommerceDetailEditPayload
    result: Dict[str, Any] = Field(default_factory=dict)


class EcommerceResultRehydrateBody(BaseModel):
    result: Dict[str, Any] = Field(default_factory=dict)


def _application_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _default_runs_root() -> str:
    return str(_application_root_dir() / "_lobster_runtime" / "comfly_ecommerce_detail" / "runs")


def _local_upload_root() -> Path:
    return _application_root_dir() / "_lobster_runtime" / "comfly_ecommerce_detail" / "uploads"


_LOCAL_FILE_TOKEN_TO_PATH: Dict[str, Path] = {}
_LOCAL_FILE_PATH_TO_TOKEN: Dict[str, str] = {}
_ALLOWED_LOCAL_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MAX_LOCAL_UPLOAD_BYTES = 40 * 1024 * 1024


def _guess_upload_suffix(filename: str, content_type: str) -> str:
    suffix = Path(str(filename or "")).suffix.lower()
    if suffix in _ALLOWED_LOCAL_IMAGE_SUFFIXES:
        return suffix
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    raise HTTPException(status_code=400, detail="仅支持 jpg / png / webp 图片")


def _resolve_controlled_local_path(local_path: Optional[str]) -> Optional[str]:
    raw = str(local_path or "").strip()
    if not raw:
        return None
    root = _local_upload_root().resolve()
    try:
        path = Path(raw).expanduser().resolve()
        path.relative_to(root)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="本地图片路径无效，请重新上传图片") from exc
    if not path.is_file():
        raise HTTPException(status_code=400, detail="本地图片文件不存在，请重新上传图片")
    if path.suffix.lower() not in _ALLOWED_LOCAL_IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="本地图片格式不支持，请上传 jpg / png / webp")
    return str(path)


def _register_local_file_url(request: Request, local_path: str) -> str:
    path = Path(str(local_path or "").strip()).resolve()
    if not path.is_file():
        return ""
    key = str(path).lower()
    token = _LOCAL_FILE_PATH_TO_TOKEN.get(key)
    if not token:
        token = uuid.uuid4().hex
        _LOCAL_FILE_PATH_TO_TOKEN[key] = token
        _LOCAL_FILE_TOKEN_TO_PATH[token] = path
    base = str(request.base_url).rstrip("/")
    version = 0
    try:
        version = int(path.stat().st_mtime_ns or 0)
    except Exception:
        version = 0
    return f"{base}/api/comfly-ecommerce-detail/local-file/{token}?v={version}"


def _enrich_result_file_urls(result: Any, request: Request) -> Dict[str, Any]:
    data = deepcopy(result) if isinstance(result, dict) else {}

    def _attach(row: Any, *path_keys: str) -> None:
        if not isinstance(row, dict):
            return
        for key in path_keys:
            url = _register_local_file_url(request, str(row.get(key) or ""))
            if url:
                row["local_preview_url"] = url
                row["preview_url"] = url
                row["open_url"] = url
                return

    for page in data.get("page_results") or []:
        _attach(page, "local_path", "path")
    final_long = data.get("final_long_image")
    _attach(final_long, "path", "local_path")
    suite = data.get("suite_bundle") if isinstance(data.get("suite_bundle"), dict) else {}
    categories = suite.get("categories") if isinstance(suite.get("categories"), dict) else {}
    for payload in categories.values():
        if not isinstance(payload, dict):
            continue
        for item in payload.get("items") or []:
            _attach(item, "path", "local_path")
    return data


def _is_allowed_local_result_path(raw_path: str) -> bool:
    raw = str(raw_path or "").strip()
    if not raw:
        return False
    try:
        path = Path(raw).expanduser().resolve()
    except Exception:
        return False
    roots = [
        _application_root_dir().resolve(),
        _local_upload_root().resolve(),
    ]
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except Exception:
            continue
    return False


def _sanitize_result_for_rehydrate(payload: Any) -> Any:
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key, value in payload.items():
            if key in {"path", "local_path", "background_local_path"}:
                text = str(value or "").strip()
                out[key] = text if _is_allowed_local_result_path(text) else ""
            else:
                out[key] = _sanitize_result_for_rehydrate(value)
        return out
    if isinstance(payload, list):
        return [_sanitize_result_for_rehydrate(item) for item in payload]
    return payload


def _sanitize_export_folder_name(value: str) -> str:
    text = re.sub(r"[<>:\"/\\\\|?*]+", " ", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "商品套图"
    return text[:48].rstrip(" .") or "商品套图"


def _pick_export_title(result: Dict[str, Any]) -> str:
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    product_name = str(analysis.get("product_name") or "").strip()
    hero_claim = str(analysis.get("hero_claim") or "").strip()
    hinted_name = str((analysis.get("user_hints") or {}).get("product_name_hint") or "").strip() if isinstance(analysis.get("user_hints"), dict) else ""
    fallback_name = str(config.get("product_name_hint") or "").strip()
    return _sanitize_export_folder_name(hinted_name or product_name or hero_claim or fallback_name or "商品套图")


def _alloc_visible_export_dir(result: Dict[str, Any]) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{_pick_export_title(result)}_{stamp}"
    candidate = _application_root_dir() / base_name
    seq = 1
    while candidate.exists():
        seq += 1
        candidate = _application_root_dir() / f"{base_name}_{seq:02d}"
    return candidate


def _rewrite_suite_bundle_paths(bundle: Dict[str, Any], final_dir: Path) -> Dict[str, Any]:
    categories = bundle.get("categories") if isinstance(bundle.get("categories"), dict) else {}
    bundle["root_dir"] = str(final_dir)
    bundle["root_relative_path"] = final_dir.name
    for _, payload in categories.items():
        if not isinstance(payload, dict):
            continue
        dirname = str(payload.get("dirname") or "").strip()
        category_dir = final_dir / dirname if dirname else final_dir
        payload["dir"] = str(category_dir)
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename") or Path(str(item.get("path") or "")).name).strip()
            item_path = category_dir / filename if filename else category_dir
            item["path"] = str(item_path)
            item["relative_path"] = str(item_path.relative_to(final_dir)).replace("\\", "/")
    return bundle


def _rewrite_saved_suite_paths(saved_assets: Dict[str, Any], final_dir: Path) -> Dict[str, Any]:
    suite_bundle = saved_assets.get("suite_bundle") if isinstance(saved_assets.get("suite_bundle"), dict) else {}
    for _, rows in suite_bundle.items():
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("relative_path") or "").strip().replace("\\", "/")
            if rel:
                rel_path = Path(rel)
                if len(rel_path.parts) > 1:
                    rel = "/".join(rel_path.parts[1:])
                item["relative_path"] = rel
    return saved_assets


def _rewrite_detail_edit_paths(result: Dict[str, Any], final_dir: Path) -> Dict[str, Any]:
    detail_dir_raw = str(result.get("detail_dir") or "").strip()
    if not detail_dir_raw:
        return result
    detail_dir = Path(detail_dir_raw)
    if not detail_dir.is_dir():
        return result

    preserved_dir = final_dir / "_detail_edit"
    preserved_dir.parent.mkdir(parents=True, exist_ok=True)
    if preserved_dir.exists():
        shutil.rmtree(preserved_dir, ignore_errors=True)
    shutil.move(str(detail_dir), str(preserved_dir))
    result["detail_dir"] = str(preserved_dir)

    for item in result.get("page_results") or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or Path(str(item.get("local_path") or "")).name).strip()
        if filename:
            new_local = preserved_dir / filename
            item["local_path"] = str(new_local)
            item["relative_path"] = str(new_local.relative_to(final_dir)).replace("\\", "/")
        bg_filename = Path(str(item.get("background_local_path") or "")).name
        if bg_filename:
            new_bg = preserved_dir / bg_filename
            item["background_local_path"] = str(new_bg)
            item["background_relative_path"] = str(new_bg.relative_to(final_dir)).replace("\\", "/")

    final_long = result.get("final_long_image")
    if isinstance(final_long, dict):
        long_name = Path(str(final_long.get("path") or final_long.get("local_path") or "")).name
        if long_name:
            new_long = preserved_dir / long_name
            final_long["path"] = str(new_long)
            final_long["local_path"] = str(new_long)

    return result


def _finalize_visible_export(result: Dict[str, Any]) -> Dict[str, Any]:
    bundle = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    root_dir = Path(str(bundle.get("root_dir") or "").strip())
    if not bundle or not root_dir.is_dir():
        return result
    final_dir = _alloc_visible_export_dir(result)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root_dir), str(final_dir))
    result["suite_bundle"] = _rewrite_suite_bundle_paths(bundle, final_dir)
    result["run_dir"] = str(final_dir)
    result = _rewrite_detail_edit_paths(result, final_dir)
    result["preview_html_path"] = None
    result["archive_path"] = None
    return result


def _cleanup_internal_run_dir(result: Dict[str, Any]) -> None:
    raw = str(result.get("run_dir") or "").strip()
    if not raw:
        return
    run_dir = Path(raw)
    target = run_dir
    if target.is_file():
        return
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except Exception:
        logger.warning("[comfly_ecommerce_detail] cleanup internal run dir failed: %s", target, exc_info=True)


def _provided_count(*values: Optional[str]) -> int:
    return sum(1 for value in values if str(value or "").strip())


def _validate_payload(pl: EcommerceDetailPipelinePayload) -> None:
    if _provided_count(pl.asset_id, pl.image_url, pl.local_path) > 1:
        raise HTTPException(status_code=400, detail="asset_id / image_url / local_path 只能三选一")
    for item in pl.product_images:
        if _provided_count(item.asset_id, item.image_url, item.local_path) > 1:
            raise HTTPException(status_code=400, detail="product_images 中每项的 asset_id / image_url / local_path 只能三选一")
        if _provided_count(item.asset_id, item.image_url, item.local_path) == 0:
            raise HTTPException(status_code=400, detail="product_images 中每项都需要提供 asset_id / image_url / local_path")
    for item in pl.icon_assets:
        if not (item.icon or "").strip():
            raise HTTPException(status_code=400, detail="icon_assets 中每项都需要提供 icon 标识")
        if _provided_count(item.asset_id, item.image_url, item.local_path) > 1:
            raise HTTPException(status_code=400, detail="icon_assets 中每项的 asset_id / image_url / local_path 只能三选一")
        if _provided_count(item.asset_id, item.image_url, item.local_path) == 0:
            raise HTTPException(status_code=400, detail="icon_assets 中每项都需要提供 asset_id / image_url / local_path")
    if not pl.product_images and _provided_count(pl.asset_id, pl.image_url, pl.local_path) == 0:
        raise HTTPException(status_code=400, detail="请提供 product_images 或 asset_id / image_url / local_path")


async def _resolve_optional_request_user(request: Request, db: Session) -> _ServerUser:
    auth = (request.headers.get("Authorization") or "").strip()
    token = ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif auth:
        token = auth
    if not token:
        return _ServerUser(id=0)
    try:
        return await get_current_user_media_edit(request=request, token=token, db=db)
    except HTTPException as exc:
        logger.info(
            "[comfly_ecommerce_detail] continue without user auth status=%s detail=%s",
            exc.status_code,
            exc.detail,
        )
        return _ServerUser(id=0)


def _resolve_ecommerce_comfly_credentials(user_id: int, db: Session) -> tuple[str, str]:
    if int(user_id or 0) > 0:
        try:
            return _resolve_comfly_credentials(user_id, db)
        except HTTPException as exc:
            logger.info(
                "[comfly_ecommerce_detail] user comfly config unavailable, checking local fallback status=%s",
                exc.status_code,
            )
    try:
        return _resolve_comfly_credentials(LOCAL_COMFLY_CONFIG_USER_ID, db)
    except HTTPException as exc:
        logger.info(
            "[comfly_ecommerce_detail] local comfly config unavailable, fallback to .env status=%s",
            exc.status_code,
        )
    s = get_settings()
    api_base = (s.comfly_api_base or "").strip().rstrip("/")
    api_key = (s.comfly_api_key or "").strip()
    if api_base and api_key:
        return api_base, api_key
    raise HTTPException(
        status_code=503,
        detail="未配置 Comfly API Key/Base：本地免登录模式需要在 .env 设置 COMFLY_API_BASE 和 COMFLY_API_KEY",
    )


def _resolve_image_input_for_pipeline(
    *,
    user_id: int,
    db: Session,
    request: Request,
    asset_id: Optional[str],
    image_url: Optional[str],
    local_path: Optional[str],
) -> str:
    local = _resolve_controlled_local_path(local_path)
    if local:
        return local
    if (asset_id or "").strip() and int(user_id or 0) <= 0:
        raise HTTPException(status_code=400, detail="asset_id 需要登录素材库；本地免登录模式请直接上传本地图片")
    return resolve_public_image_for_pipeline(
        user_id=user_id,
        db=db,
        request=request,
        asset_id=asset_id,
        image_url=image_url,
    )


def _resolve_reference_inputs_for_pipeline(
    *,
    user_id: int,
    db: Session,
    request: Request,
    asset_ids: Optional[List[str]],
    image_urls: Optional[List[str]],
    local_paths: Optional[List[str]],
) -> List[str]:
    out: List[str] = []
    if any(str(aid or "").strip() for aid in asset_ids or []) and int(user_id or 0) <= 0:
        raise HTTPException(status_code=400, detail="参考图 asset_id 需要登录素材库；本地免登录模式请直接上传本地图片")
    for item in resolve_reference_images_for_pipeline(
        user_id=user_id,
        db=db,
        request=request,
        asset_ids=asset_ids,
        image_urls=image_urls,
    ):
        if item and item not in out:
            out.append(item)
    for local_path in local_paths or []:
        local = _resolve_controlled_local_path(local_path)
        if local and local not in out:
            out.append(local)
    return out


async def _prepare_pipeline_input(
    *,
    pl: EcommerceDetailPipelinePayload,
    current_user: _ServerUser,
    db: Session,
    request: Request,
    effective_output_dir: str,
) -> Dict[str, object]:
    style_reference_images = _resolve_reference_inputs_for_pipeline(
        user_id=current_user.id,
        db=db,
        request=request,
        asset_ids=pl.style_reference_asset_ids,
        image_urls=pl.style_reference_image_urls,
        local_paths=pl.style_reference_local_paths,
    )
    if pl.product_images:
        resolved_product_images: List[Dict[str, str]] = []
        for item in pl.product_images:
            resolved_url = _resolve_image_input_for_pipeline(
                user_id=current_user.id,
                db=db,
                request=request,
                asset_id=item.asset_id,
                image_url=item.image_url,
                local_path=item.local_path,
            )
            resolved_product_images.append(
                {
                    "role": str(item.role or "front").strip().lower() or "front",
                    "url": resolved_url,
                }
            )
        front_candidates = [row["url"] for row in resolved_product_images if row["role"] == "front"]
        product_image = front_candidates[0] if front_candidates else resolved_product_images[0]["url"]
        reference_images = [row["url"] for row in resolved_product_images if row["url"] != product_image]
    else:
        product_image = _resolve_image_input_for_pipeline(
            user_id=current_user.id,
            db=db,
            request=request,
            asset_id=pl.asset_id,
            image_url=pl.image_url,
            local_path=pl.local_path,
        )
        reference_images = []
    extra_reference_images = _resolve_reference_inputs_for_pipeline(
        user_id=current_user.id,
        db=db,
        request=request,
        asset_ids=pl.reference_asset_ids,
        image_urls=pl.reference_image_urls,
        local_paths=pl.reference_local_paths,
    )
    for image_url in extra_reference_images + style_reference_images:
        if image_url != product_image and image_url not in reference_images:
            reference_images.append(image_url)
    resolved_icon_assets: List[Dict[str, str]] = []
    for item in pl.icon_assets:
        resolved_url = _resolve_image_input_for_pipeline(
            user_id=current_user.id,
            db=db,
            request=request,
            asset_id=item.asset_id,
            image_url=item.image_url,
            local_path=item.local_path,
        )
        resolved_icon_assets.append({"icon": str(item.icon or "").strip(), "url": resolved_url})
    api_base, api_key = _resolve_ecommerce_comfly_credentials(current_user.id, db)
    return build_pipeline_input(
        product_image=product_image,
        reference_images=reference_images,
        sku=pl.sku,
        selling_points=[item.model_dump(exclude_none=True) for item in pl.selling_points],
        specs=dict(pl.specs),
        style=pl.style,
        style_reference_images=style_reference_images,
        icon_assets=resolved_icon_assets,
        scene_preferences=pl.scene_preferences.model_dump(exclude_none=True) if pl.scene_preferences else None,
        output_targets=pl.output_targets.model_dump(exclude_none=True) if pl.output_targets else None,
        detail_template_id=pl.detail_template_id,
        showcase_template_id=pl.showcase_template_id,
        listing_category=pl.listing_category,
        export_name_prefix=pl.export_name_prefix,
        showcase_count=pl.showcase_count,
        brand=pl.brand,
        compliance_notes=list(pl.compliance_notes),
        api_key=api_key,
        api_base=api_base,
        analysis_model=pl.analysis_model,
        image_model=pl.image_model,
        page_count=pl.page_count,
        output_dir=effective_output_dir,
        product_name_hint=pl.product_name_hint,
        product_direction_hint=pl.product_direction_hint,
        platform=pl.platform,
        country=pl.country,
        language=pl.language,
    )


def _save_local_image_asset(
    *,
    local_path: str,
    user_id: int,
    db: Session,
    prompt: str,
    model: str,
    tags: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    path = Path((local_path or "").strip())
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    suffix = path.suffix.lower() or ".png"
    content_type = "image/png"
    if suffix in {".jpg", ".jpeg"}:
        content_type = "image/jpeg"
    elif suffix == ".webp":
        content_type = "image/webp"
    aid, fname, fsize, tos_url = _save_bytes_or_tos(raw, suffix, content_type)
    source_url = (tos_url or "").strip() or ""
    asset = Asset(
        asset_id=aid,
        user_id=user_id,
        filename=fname,
        media_type="image",
        file_size=fsize,
        source_url=source_url or None,
        prompt=prompt[:2000],
        model=(model or "")[:128] or None,
        tags=tags[:500],
        meta=meta or {},
    )
    db.add(asset)
    db.commit()
    return {
        "asset_id": aid,
        "filename": fname,
        "media_type": "image",
        "file_size": fsize,
        "source_url": source_url,
    }


def _save_pipeline_images(*, result: Dict[str, Any], user_id: int, db: Session) -> Dict[str, Any]:
    image_model = str((result.get("config") or {}).get("image_model") or "")
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    saved_pages: List[Dict[str, Any]] = []
    for page in result.get("page_results") or []:
        if not isinstance(page, dict):
            continue
        asset_row = _save_local_image_asset(
            local_path=str(page.get("local_path") or ""),
            user_id=user_id,
            db=db,
            prompt=str(page.get("title") or page.get("slot") or "详情页"),
            model=image_model,
            tags="auto,comfly.ecommerce.detail_pipeline,page",
            meta={
                "origin": "comfly_ecommerce_detail_page",
                "slot": page.get("slot"),
                "page_index": page.get("index"),
            },
        )
        if asset_row:
            saved_pages.append(
                {
                    "index": int(page.get("index") or 0),
                    "slot": str(page.get("slot") or ""),
                    "asset": asset_row,
                }
            )
    final_info = result.get("final_long_image") if isinstance(result.get("final_long_image"), dict) else {}
    final_asset = _save_local_image_asset(
        local_path=str(final_info.get("path") or ""),
        user_id=user_id,
        db=db,
        prompt="电商详情长图",
        model=image_model,
        tags="auto,comfly.ecommerce.detail_pipeline,long_image",
        meta={
            "origin": "comfly_ecommerce_detail_long_image",
            "page_count": final_info.get("page_count"),
        },
    )
    suite_saved: Dict[str, List[Dict[str, Any]]] = {}
    suite_bundle = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    suite_categories = suite_bundle.get("categories") if isinstance(suite_bundle, dict) else {}
    if isinstance(suite_categories, dict):
        for category, payload in suite_categories.items():
            if not isinstance(payload, dict):
                continue
            saved_rows: List[Dict[str, Any]] = []
            for item in payload.get("items") or []:
                if not isinstance(item, dict):
                    continue
                asset_row = _save_local_image_asset(
                    local_path=str(item.get("path") or ""),
                    user_id=user_id,
                    db=db,
                    prompt=str(item.get("filename") or category),
                    model=image_model,
                    tags=f"auto,comfly.ecommerce.detail_pipeline,{category}",
                    meta={
                        "origin": "comfly_ecommerce_suite_export",
                        "suite_category": category,
                        "relative_path": item.get("relative_path"),
                        "placeholder": bool(item.get("placeholder")),
                    },
                )
                if asset_row:
                    saved_rows.append(
                        {
                            "filename": str(item.get("filename") or ""),
                            "relative_path": str(item.get("relative_path") or ""),
                            "asset": asset_row,
                        }
                    )
            if saved_rows:
                suite_saved[category] = saved_rows
    return {
        "meta": {
            "product_name": str(analysis.get("product_name") or ""),
            "listing_category": str(analysis.get("listing_category") or config.get("listing_category") or ""),
            "export_name_prefix": str(config.get("export_name_prefix") or ""),
        },
        "pages": saved_pages,
        "final": {"asset": final_asset} if final_asset else None,
        "suite_bundle": suite_saved,
    }


async def _job_runner(job_id: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    inp = deepcopy(job.get("inp") or {})
    auto_save = bool(job.get("auto_save"))
    user_id = int(job.get("user_id") or 0)
    try:
        result = await asyncio.to_thread(run_pipeline_sync, inp)
    except Exception as e:
        logger.exception("[comfly_ecommerce_detail] job %s failed", job_id[:12])
        update_job(job_id, status="failed", error=str(e)[:2000])
        _persist_job_to_db(job_id, user_id=user_id, status="failed", error=str(e)[:2000])
        return
    internal_run_dir = str(result.get("run_dir") or "").strip()
    saved_assets: Dict[str, Any] = {"pages": [], "final": None}
    result = _finalize_visible_export(result)
    try:
        if auto_save:
            db = SessionLocal()
            try:
                saved_assets = _save_pipeline_images(result=result, user_id=user_id, db=db)
                saved_assets = _rewrite_saved_suite_paths(saved_assets, Path(str((result.get("suite_bundle") or {}).get("root_dir") or "")))
            except Exception:
                logger.exception("[comfly_ecommerce_detail] job %s auto_save failed", job_id[:12])
                update_job(job_id, status="failed", error="流水线执行成功，但素材入库失败", result=result)
                _persist_job_to_db(job_id, user_id=user_id, status="failed", error="流水线执行成功，但素材入库失败", result=result)
                db.close()
                return
            finally:
                db.close()
    finally:
        if internal_run_dir:
            _cleanup_internal_run_dir({"run_dir": internal_run_dir})
    update_job(job_id, status="completed", error=None, result=result, saved_assets=saved_assets)
    _persist_job_to_db(job_id, user_id=user_id, status="completed", saved_assets=saved_assets, result=result)


def _persist_job_to_db(
    job_id: str,
    *,
    user_id: int,
    status: str,
    saved_assets: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """将 job 最终状态持久化到数据库，重启后仍可查回。"""
    product_name = _pick_export_title(result) if isinstance(result, dict) else None
    db = SessionLocal()
    try:
        existing = db.query(EcommerceDetailJob).filter(EcommerceDetailJob.job_id == job_id).first()
        if existing:
            existing.status = status
            existing.saved_assets = saved_assets
            existing.error = error
            existing.product_name = product_name
            existing.updated_at = datetime.utcnow()
        else:
            db.add(EcommerceDetailJob(
                job_id=job_id,
                user_id=user_id,
                status=status,
                product_name=product_name,
                saved_assets=saved_assets,
                error=error,
            ))
        db.commit()
    except Exception:
        logger.exception("[comfly_ecommerce_detail] persist job %s to db failed", job_id[:12])
        db.rollback()
    finally:
        db.close()


def _redact_progress_for_client(progress: Any) -> Any:
    if not isinstance(progress, dict):
        return progress
    red = {k: v for k, v in progress.items() if k not in ("manifest_file", "run_dir")}
    last_steps = red.get("last_steps")
    if isinstance(last_steps, list):
        cleaned = []
        for item in last_steps:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            err = row.get("error")
            if isinstance(err, str) and err.strip():
                row["error"] = re.sub(
                    r"(?:[A-Za-z]:[/\\][^\s\"'<>|]{2,320}|(?:\\\\|/)[^\s\"'<>|]{0,320}(?:[/\\](?:skills|job_runs|runs)[/\\]|\.py\b)[^\s\"'<>|]{0,320})",
                    "...",
                    err.strip(),
                    flags=re.IGNORECASE,
                )[:400]
            cleaned.append(row)
        red["last_steps"] = cleaned
    return red


def _enrich_saved_asset_urls_for_client(saved_assets: Any, request: Request) -> Dict[str, Any]:
    """Add browser-loadable URLs for final composed images saved into the asset store."""
    data = deepcopy(saved_assets) if isinstance(saved_assets, dict) else {}

    def _enrich_asset(asset: Any) -> None:
        if not isinstance(asset, dict):
            return
        asset_id = str(asset.get("asset_id") or "").strip()
        signed_url = ""
        if asset_id:
            try:
                signed_url = build_asset_file_url(request, asset_id, expiry_sec=86400) or ""
            except Exception:
                logger.debug("[comfly_ecommerce_detail] build preview url failed asset_id=%s", asset_id, exc_info=True)
        source_url = str(asset.get("source_url") or "").strip()
        display_url = signed_url or source_url
        if display_url:
            asset["preview_url"] = display_url
            asset["open_url"] = display_url
        if signed_url:
            asset["local_preview_url"] = signed_url

    for page in data.get("pages") or []:
        if isinstance(page, dict):
            _enrich_asset(page.get("asset"))
    final = data.get("final")
    if isinstance(final, dict):
        _enrich_asset(final.get("asset"))
    suite_bundle = data.get("suite_bundle") if isinstance(data.get("suite_bundle"), dict) else {}
    for rows in suite_bundle.values():
        if not isinstance(rows, list):
            continue
        for item in rows:
            if isinstance(item, dict):
                _enrich_asset(item.get("asset"))
    return data


def _job_status_response(job: Dict[str, Any], *, include_full: bool, request: Request) -> Dict[str, Any]:
    status = (job.get("status") or "").strip()
    out: Dict[str, Any] = {
        "ok": True,
        "job_id": job.get("job_id"),
        "status": status,
        "auto_save": job.get("auto_save"),
        "created_at_ts": job.get("created_at_ts"),
        "updated_at_ts": job.get("updated_at_ts"),
    }
    if status == "running":
        progress = read_manifest_progress(str(job.get("job_output_dir") or ""))
        if progress:
            out["progress"] = _redact_progress_for_client(progress)
    if status == "failed":
        out["error"] = job.get("error")
        if include_full and job.get("result") is not None:
            out["result"] = _enrich_result_file_urls(job.get("result"), request)
    if status == "completed":
        if include_full:
            out["result"] = _enrich_result_file_urls(job.get("result"), request)
            out["saved_assets"] = _enrich_saved_asset_urls_for_client(job.get("saved_assets") or {}, request)
        progress = read_manifest_progress(str(job.get("job_output_dir") or ""))
        if progress:
            out["progress"] = _redact_progress_for_client(progress)
    return out


def _find_suite_category_item(result: Dict[str, Any], category: str, page_index: int) -> Optional[Dict[str, Any]]:
    bundle = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    categories = bundle.get("categories") if isinstance(bundle.get("categories"), dict) else {}
    payload = categories.get(category) if isinstance(categories, dict) else None
    items = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        if int(item.get("page_index") or 0) == int(page_index):
            return item
    return None


def _build_showcase_source_pool_from_result(result: Dict[str, Any]) -> tuple[list[Dict[str, Any]], Dict[str, str]]:
    mod = _load_pipeline_module()
    bundle = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    categories = bundle.get("categories") if isinstance(bundle.get("categories"), dict) else {}

    def _items(category: str) -> List[Dict[str, Any]]:
        payload = categories.get(category) if isinstance(categories, dict) else None
        rows = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []
        return [row for row in rows if isinstance(row, dict)]

    def _open_image_from_rows(rows: List[Dict[str, Any]], *, kind: str = "", suffix: str = ""):
        for row in rows:
            path = Path(str(row.get("path") or "")).resolve()
            if not path.is_file():
                continue
            if kind and str(row.get("kind") or "") != kind:
                continue
            if suffix and not path.name.endswith(suffix):
                continue
            return mod._open_local_image(str(path))
        return None

    main_rows = _items("main_images")
    sku_rows = _items("sku_images")
    white_rows = _items("transparent_white_bg")

    main_square_image = _open_image_from_rows(main_rows, kind="main_image_square") or _open_image_from_rows(main_rows, suffix="1440X1440.jpg")
    main_portrait_image = _open_image_from_rows(main_rows, kind="main_image_portrait") or _open_image_from_rows(main_rows, suffix="1440X1920.jpg")
    sku_scene_image = _open_image_from_rows(sku_rows, kind="sku_scene") or _open_image_from_rows(sku_rows, suffix="SKU场景.jpg")
    white_bg_image = _open_image_from_rows(white_rows, kind="white_bg_image") or _open_image_from_rows(white_rows, suffix="白底.jpg")
    product_image_rgba = _open_image_from_rows(white_rows, kind="transparent_image") or white_bg_image or main_portrait_image or main_square_image or sku_scene_image
    if product_image_rgba is None:
        raise HTTPException(status_code=409, detail="当前任务缺少可编辑的橱窗图源素材，请重新生成后再试")
    return (
        mod._build_showcase_source_pool(
            product_image_rgba=product_image_rgba,
            main_square_image=main_square_image,
            main_portrait_image=main_portrait_image,
            sku_scene_image=sku_scene_image,
            white_bg_image=white_bg_image,
        ),
        {},
    )


def _find_page_result_entry(result: Dict[str, Any], page_index: int) -> Optional[Dict[str, Any]]:
    rows = result.get("page_results") if isinstance(result.get("page_results"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if int(item.get("index") or 0) == int(page_index):
            return item
    return None


def _find_page_copy_entry(result: Dict[str, Any], page_index: int) -> Optional[Dict[str, Any]]:
    rows = result.get("pages") if isinstance(result.get("pages"), list) else []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if int(item.get("index") or 0) == int(page_index):
            return item
    return None


def _detail_render_config_from_result(mod: Any, result: Dict[str, Any]) -> Any:
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    config_payload = result.get("config") if isinstance(result.get("config"), dict) else {}
    detail_template_id = str(
        result.get("detail_template_id")
        or analysis.get("detail_template_id")
        or config_payload.get("detail_template_id")
        or "detail_template_01"
    ).strip() or "detail_template_01"
    showcase_template_id = str(
        result.get("showcase_template_id")
        or analysis.get("showcase_template_id")
        or config_payload.get("showcase_template_id")
        or getattr(mod, "DEFAULT_SHOWCASE_TEMPLATE_ID", "showcase_template_01")
    ).strip() or getattr(mod, "DEFAULT_SHOWCASE_TEMPLATE_ID", "showcase_template_01")
    return mod.PipelineConfig(
        base_url="",
        api_key="__history_edit__",
        detail_template_id=detail_template_id,
        showcase_template_id=showcase_template_id,
        template_config=mod._load_detail_template_config(detail_template_id),
        showcase_template_config=mod._load_showcase_template_config(showcase_template_id),
        page_width=int(config_payload.get("page_width") or 790),
        page_height=int(config_payload.get("page_height") or 1250),
        page_gap_px=int(config_payload.get("page_gap_px") or 0),
        page_count=int(config_payload.get("page_count") or 12),
        showcase_count=int(config_payload.get("showcase_count") or 0),
        analysis_model=str(config_payload.get("analysis_model") or ""),
        image_model=str(config_payload.get("image_model") or ""),
        aspect_ratio=str(config_payload.get("aspect_ratio") or "9:16"),
        listing_category=str(config_payload.get("listing_category") or ""),
        export_name_prefix=str(config_payload.get("export_name_prefix") or ""),
        product_name_hint=str(config_payload.get("product_name_hint") or analysis.get("product_name") or ""),
    )


def _history_edit_job_response(*, job_id: str, result: Dict[str, Any], request: Request) -> Dict[str, Any]:
    enriched = _enrich_result_file_urls(result, request)
    analysis = enriched.get("analysis") if isinstance(enriched.get("analysis"), dict) else {}
    return {
        "ok": True,
        "job_id": job_id,
        "status": "completed",
        "source": "history_snapshot",
        "product_name": str(analysis.get("product_name") or ""),
        "result": enriched,
    }


def _collect_detail_page_paths(result: Dict[str, Any]) -> List[str]:
    return [
        str(item.get("local_path") or "")
        for item in sorted(
            [row for row in (result.get("page_results") or []) if isinstance(row, dict)],
            key=lambda row: int(row.get("index") or 0),
        )
        if str(item.get("local_path") or "").strip()
    ]


def _refresh_detail_long_image_snapshot(
    *,
    result: Dict[str, Any],
    long_image_path: str,
    page_gap_px: int,
    job_id: Optional[str] = None,
) -> None:
    try:
        page_paths = _collect_detail_page_paths(result)
        if not page_paths or not long_image_path:
            return
        mod = _load_pipeline_module()
        result["final_long_image"] = mod._compose_long_image(page_paths, long_image_path, page_gap_px)
        if job_id and get_job(job_id):
            update_job(job_id, result=result)
    except Exception:
        logger.warning("[comfly_ecommerce_detail] async refresh long image failed job_id=%s", (job_id or "")[:12], exc_info=True)


@router.post("/api/comfly-ecommerce-detail/local-upload")
async def ecommerce_detail_local_upload(
    request: Request,
    file: UploadFile = File(...),
):
    suffix = _guess_upload_suffix(file.filename or "", file.content_type or "")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传图片为空")
    if len(raw) > _MAX_LOCAL_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="图片过大，请控制在 40MB 以内")
    day_dir = _local_upload_root() / datetime.now().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / f"{uuid.uuid4().hex}{suffix}"
    out.write_bytes(raw)
    preview_url = _register_local_file_url(request, str(out))
    return {
        "ok": True,
        "filename": file.filename or out.name,
        "local_path": str(out),
        "preview_url": preview_url,
        "local_preview_url": preview_url,
        "file_size": len(raw),
    }


@router.get("/api/comfly-ecommerce-detail/local-file/{file_token}")
def ecommerce_detail_local_file(file_token: str):
    token = str(file_token or "").strip().lower()
    path = _LOCAL_FILE_TOKEN_TO_PATH.get(token)
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在或已过期")
    return FileResponse(
        str(path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.post("/api/comfly-ecommerce-detail/pipeline/run")
async def ecommerce_detail_pipeline_run(
    body: EcommerceDetailRunBody,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = await _resolve_optional_request_user(request, db)
    pl = body.payload
    _validate_payload(pl)
    inp = await _prepare_pipeline_input(
        pl=pl,
        current_user=current_user,
        db=db,
        request=request,
        effective_output_dir=_default_runs_root(),
    )
    try:
        result = await asyncio.to_thread(run_pipeline_sync, inp)
    except Exception as e:
        logger.exception("[comfly_ecommerce_detail] pipeline failed user_id=%s", current_user.id)
        raise HTTPException(status_code=500, detail=str(e)[:2000]) from e
    internal_run_dir = str(result.get("run_dir") or "").strip()
    result = _finalize_visible_export(result)
    saved_assets: Dict[str, Any] = {"pages": [], "final": None}
    try:
        if pl.auto_save and int(current_user.id or 0) > 0:
            try:
                saved_assets = _save_pipeline_images(result=result, user_id=current_user.id, db=db)
                saved_assets = _rewrite_saved_suite_paths(saved_assets, Path(str((result.get("suite_bundle") or {}).get("root_dir") or "")))
            except Exception as e:
                logger.exception("[comfly_ecommerce_detail] auto_save failed")
                raise HTTPException(status_code=500, detail=f"流水线执行成功，但素材入库失败: {e}") from e
    finally:
        if internal_run_dir:
            _cleanup_internal_run_dir({"run_dir": internal_run_dir})
    return {
        "ok": True,
        "pipeline": "comfly_ecommerce_detail",
        "result": _enrich_result_file_urls(result, request),
        "saved_assets": _enrich_saved_asset_urls_for_client(saved_assets, request),
    }


@router.post("/api/comfly-ecommerce-detail/pipeline/start")
async def ecommerce_detail_pipeline_start(
    body: EcommerceDetailRunBody,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = await _resolve_optional_request_user(request, db)
    pl = body.payload
    _validate_payload(pl)
    runs_root = _default_runs_root()
    job_id = uuid.uuid4().hex
    effective_dir = str(Path(runs_root) / "job_runs" / job_id) if pl.isolate_job_dir else runs_root
    inp = await _prepare_pipeline_input(
        pl=pl,
        current_user=current_user,
        db=db,
        request=request,
        effective_output_dir=effective_dir,
    )
    create_job_record(
        user_id=current_user.id,
        inp=inp,
        auto_save=bool(pl.auto_save and int(current_user.id or 0) > 0),
        job_output_dir=effective_dir,
        job_id=job_id,
    )
    _persist_job_to_db(job_id, user_id=current_user.id, status="running",
                       result={"product_name_hint": pl.product_name_hint})

    def _log_task(task: asyncio.Task) -> None:
        try:
            if task.exception() is not None:
                logger.exception("[comfly_ecommerce_detail] background job error job_id=%s", job_id[:12])
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_job_runner(job_id))
    task.add_done_callback(_log_task)
    return {
        "ok": True,
        "async": True,
        "job_id": job_id,
        "poll_path": f"/api/comfly-ecommerce-detail/pipeline/jobs/{job_id}",
    }

@router.get("/api/comfly-ecommerce-detail/pipeline/jobs/{job_id}")
async def ecommerce_detail_pipeline_job_status(
    job_id: str,
    request: Request,
    compact: bool = False,
    db: Session = Depends(get_db),
):
    current_user = await _resolve_optional_request_user(request, db)
    job = get_job(job_id)
    if job:
        job_user_id = int(job.get("user_id") or 0)
        if job_user_id > 0 and job_user_id != int(current_user.id or 0):
            raise HTTPException(status_code=403, detail="无权查看该任务")
        return _job_status_response(job, include_full=not compact, request=request)
    query = db.query(EcommerceDetailJob).filter(EcommerceDetailJob.job_id == job_id)
    if int(current_user.id or 0) > 0:
        query = query.filter(EcommerceDetailJob.user_id == current_user.id)
    else:
        query = query.filter(EcommerceDetailJob.user_id == 0)
    db_job = query.first()
    if not db_job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {
        "ok": True,
        "job_id": db_job.job_id,
        "status": db_job.status,
        "product_name": db_job.product_name,
        "saved_assets": _enrich_saved_asset_urls_for_client(db_job.saved_assets or {}, request),
        "error": db_job.error,
        "created_at": db_job.created_at.isoformat() if db_job.created_at else None,
        "source": "database",
    }


@router.post("/api/comfly-ecommerce-detail/pipeline/rehydrate-result")
async def ecommerce_detail_pipeline_rehydrate_result(
    body: EcommerceResultRehydrateBody,
    request: Request,
    db: Session = Depends(get_db),
):
    await _resolve_optional_request_user(request, db)
    sanitized = _sanitize_result_for_rehydrate(body.result)
    return {
        "ok": True,
        "result": _enrich_result_file_urls(sanitized, request),
    }


@router.post("/api/comfly-ecommerce-detail/pipeline/jobs/{job_id}/showcase-edit")
async def ecommerce_detail_pipeline_showcase_edit(
    job_id: str,
    body: EcommerceShowcaseEditBody,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = await _resolve_optional_request_user(request, db)
    job = get_job(job_id)
    if job:
        job_user_id = int(job.get("user_id") or 0)
        if job_user_id > 0 and job_user_id != int(current_user.id or 0):
            raise HTTPException(status_code=403, detail="无权编辑该任务")
        if str(job.get("status") or "") != "completed":
            raise HTTPException(status_code=409, detail="任务尚未完成，暂时不能改单张文案")
        result = deepcopy(job.get("result") or {})
    else:
        result = _sanitize_result_for_rehydrate(body.result)
    if not isinstance(result, dict) or not result:
        raise HTTPException(status_code=409, detail="当前任务缺少完整结果数据，请重新生成后再试")
    payload = body.payload
    item = _find_suite_category_item(result, "showcase_images", payload.page_index)
    if not item:
        raise HTTPException(status_code=404, detail=f"未找到 page_index={payload.page_index} 的橱窗图")
    target_path = Path(str(item.get("path") or "")).resolve()
    if not target_path.is_file():
        raise HTTPException(status_code=404, detail="目标橱窗图文件不存在，请重新生成后再试")

    mod = _load_pipeline_module()
    source_pool, _ = _build_showcase_source_pool_from_result(result)
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    showcase_meta = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    showcase_template = showcase_meta.get("showcase_template") if isinstance(showcase_meta.get("showcase_template"), dict) else {}
    template_id = str(showcase_template.get("template_id") or analysis.get("showcase_template_id") or "").strip()
    template_config = mod._load_showcase_template_config(template_id) if template_id else {}
    theme_override = dict(template_config.get("theme") or {}) if isinstance(template_config, dict) else {}
    template_variant = int(item.get("template_variant") or ((payload.page_index - 1) % 4))

    def _next_text(raw: Optional[str], current: str) -> str:
        return current if raw is None else str(raw).strip()

    record = {
        "title": _next_text(payload.title, str(item.get("title") or "")),
        "subtitle": _next_text(payload.subtitle, str(item.get("subtitle") or "")),
        "hero_claim": _next_text(payload.hero_claim, str(item.get("hero_claim") or "")),
        "summary": _next_text(payload.summary, str(item.get("summary") or "")),
        "corner": _next_text(payload.corner, str(item.get("corner") or "")),
    }
    if not record["title"]:
        raise HTTPException(status_code=400, detail="标题不能为空")

    rendered = mod._render_showcase_card(
        index=max(0, int(payload.page_index) - 1),
        record=record,
        source_pool=source_pool,
        analysis=analysis,
        template_variant=template_variant,
        theme_override=theme_override,
        width=1440,
        height=1920,
    )
    exported = mod._save_cover_jpeg(rendered, target_path, 1440, 1920)
    item.update(exported)
    item["page_index"] = int(payload.page_index)
    item["slot"] = "showcase_card"
    item["kind"] = "showcase_image"
    item["source"] = "local_showcase_layout"
    item["title"] = record["title"]
    item["subtitle"] = record["subtitle"]
    item["hero_claim"] = record["hero_claim"]
    item["summary"] = record["summary"]
    item["corner"] = record["corner"]
    item["template_variant"] = template_variant

    if job:
        update_job(job_id, result=result)
        response = _job_status_response(get_job(job_id) or job, include_full=True, request=request)
    else:
        response = _history_edit_job_response(job_id=job_id, result=result, request=request)
    return {
        "ok": True,
        "job_id": job_id,
        "page_index": int(payload.page_index),
        "item": {
            **item,
            "preview_url": _register_local_file_url(request, str(target_path)),
            "open_url": _register_local_file_url(request, str(target_path)),
        },
        "job": response,
    }


@router.post("/api/comfly-ecommerce-detail/pipeline/jobs/{job_id}/detail-edit")
async def ecommerce_detail_pipeline_detail_edit(
    job_id: str,
    body: EcommerceDetailEditBody,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    current_user = await _resolve_optional_request_user(request, db)
    job = get_job(job_id)
    if job:
        job_user_id = int(job.get("user_id") or 0)
        if job_user_id > 0 and job_user_id != int(current_user.id or 0):
            raise HTTPException(status_code=403, detail="无权编辑该任务")
        if str(job.get("status") or "") != "completed":
            raise HTTPException(status_code=409, detail="任务尚未完成，暂时不能改单张文案")
        result = deepcopy(job.get("result") or {})
    else:
        result = _sanitize_result_for_rehydrate(body.result)
    if not isinstance(result, dict) or not result:
        raise HTTPException(status_code=409, detail="当前任务缺少完整结果数据，请重新生成后再试")
    payload = body.payload
    suite_item = _find_suite_category_item(result, "detail_images", payload.page_index)
    page_copy = _find_page_copy_entry(result, payload.page_index)
    page_result = _find_page_result_entry(result, payload.page_index)
    if not suite_item or not page_copy or not page_result:
        raise HTTPException(status_code=404, detail=f"未找到 page_index={payload.page_index} 的详情图")

    mod = _load_pipeline_module()
    inp = dict(job.get("inp") or {}) if job else {}
    try:
        config = mod._build_config(inp) if inp else _detail_render_config_from_result(mod, result)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=f"无法恢复详情图配置：{exc}") from exc

    title = str(payload.title if payload.title is not None else page_copy.get("title") or "").strip()
    subtitle = str(payload.subtitle if payload.subtitle is not None else page_copy.get("subtitle") or "").strip()
    footer = str(payload.footer if payload.footer is not None else page_copy.get("footer") or "").strip()
    highlights = [str(item or "").strip() for item in (payload.highlights or list(page_copy.get("highlights") or [])) if str(item or "").strip()]
    highlights = highlights[:6]
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")

    page_copy["title"] = title
    page_copy["subtitle"] = subtitle
    page_copy["footer"] = footer
    page_copy["highlights"] = highlights

    background = None
    product_local = None
    background_local_path = str(page_result.get("background_local_path") or "").strip()
    generation_mode = str(page_result.get("generation_mode") or "").strip().lower()
    if background_local_path:
        try:
            bg_path = Path(background_local_path).resolve()
            if bg_path.is_file():
                background = mod._open_local_image(str(bg_path))
        except Exception:
            logger.warning("[comfly_ecommerce_detail] open local detail background failed job_id=%s", job_id[:12], exc_info=True)
    if background is None:
        background_url = str(page_result.get("generated_image_url") or "").strip()
        if background_url:
            try:
                background = mod._download_image(background_url)
            except Exception:
                logger.warning(
                    "[comfly_ecommerce_detail] remote detail background expired or unavailable, original-bg edit unavailable job_id=%s",
                    job_id[:12],
                    exc_info=True,
                )
    if background is None:
        if generation_mode == "local_fallback":
            product_image = str(inp.get("product_image") or result.get("product_image_url") or "").strip()
            if not product_image:
                raise HTTPException(status_code=409, detail="当前任务缺少商品主图源素材，请重新生成后再试")
            try:
                product_local = mod._download_image(product_image)
            except Exception as exc:
                raise HTTPException(status_code=409, detail=f"无法恢复商品主图：{exc}") from exc
            try:
                background = mod._make_local_fallback_background(product_local.copy(), page_copy, config)
            except Exception as exc:
                raise HTTPException(status_code=409, detail=f"??????????{exc}") from exc
        if background is None:
            raise HTTPException(
                status_code=409,
                detail="????????????????????????????????????????????????????????????????",
            )

    metadata = page_copy.get("metadata") if isinstance(page_copy.get("metadata"), dict) else {}
    used_icon_ids: List[str] = []
    icon_id = str(metadata.get("icon") or "").strip()
    if icon_id:
        used_icon_ids.append(icon_id)
    try:
        icon_images = mod._load_icon_images(inp.get("icon_assets"), used_icon_ids)
    except Exception:
        icon_images = {}

    # When we already have the original page background locally, avoid re-downloading
    # the product image on every text edit. The current detail renderer only needs the
    # background to preserve layout and composition.
    render_product_source = product_local.copy() if product_local is not None else background.copy()
    rendered = mod._render_page(render_product_source, background, page_copy, config, icon_images=icon_images)

    page_local_path = Path(str(page_result.get("local_path") or "")).resolve()
    suite_path = Path(str(suite_item.get("path") or "")).resolve()
    if not page_local_path.parent.exists():
        page_local_path.parent.mkdir(parents=True, exist_ok=True)
    if not suite_path.parent.exists():
        suite_path.parent.mkdir(parents=True, exist_ok=True)
    if not background_local_path:
        background_local_path = str(page_local_path.with_name(f"背景_{int(payload.page_index):02d}.jpg"))
    try:
        Path(background_local_path).parent.mkdir(parents=True, exist_ok=True)
        background.convert("RGB").save(background_local_path, format="JPEG", quality=92, subsampling=0)
    except Exception:
        logger.warning("[comfly_ecommerce_detail] persist detail background failed job_id=%s", job_id[:12], exc_info=True)
    rendered.save(page_local_path, format="JPEG", quality=92, subsampling=0)
    rendered.save(suite_path, format="JPEG", quality=92, subsampling=0)

    page_result["title"] = title
    page_result["subtitle"] = subtitle
    page_result["footer"] = footer
    page_result["highlights"] = highlights
    page_result["width"] = rendered.width
    page_result["height"] = rendered.height
    page_result["filename"] = page_local_path.name
    page_result["local_path"] = str(page_local_path)
    page_result["relative_path"] = str(page_result.get("relative_path") or "")
    page_result["background_local_path"] = str(background_local_path or "")

    suite_item["title"] = title
    suite_item["subtitle"] = subtitle
    suite_item["footer"] = footer
    suite_item["highlights"] = highlights
    suite_item["width"] = rendered.width
    suite_item["height"] = rendered.height
    suite_item["filename"] = suite_path.name
    suite_item["path"] = str(suite_path)

    long_image = result.get("final_long_image") if isinstance(result.get("final_long_image"), dict) else {}
    long_image_path = str(long_image.get("path") or "").strip()
    long_image_refresh_pending = False
    page_gap_px = int((result.get("config") or {}).get("page_gap_px") or 0)
    if long_image_path and _collect_detail_page_paths(result):
        long_image_refresh_pending = True
        background_tasks.add_task(
            _refresh_detail_long_image_snapshot,
            result=deepcopy(result),
            long_image_path=long_image_path,
            page_gap_px=page_gap_px,
            job_id=job_id if job else None,
        )

    if job:
        update_job(job_id, result=result)
        response = _job_status_response(get_job(job_id) or job, include_full=True, request=request)
    else:
        response = _history_edit_job_response(job_id=job_id, result=result, request=request)
    return {
        "ok": True,
        "job_id": job_id,
        "page_index": int(payload.page_index),
        "item": {
            **suite_item,
            "preview_url": _register_local_file_url(request, str(suite_path)),
            "open_url": _register_local_file_url(request, str(suite_path)),
        },
        "long_image_refresh_pending": long_image_refresh_pending,
        "job": response,
    }


@router.get("/api/comfly-ecommerce-detail/pipeline/jobs")
async def ecommerce_detail_pipeline_job_list(
    limit: int = 50,
    offset: int = 0,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
    """列出当前用户所有持久化的套图任务（按创建时间降序）。"""
    query = db.query(EcommerceDetailJob).filter(
        EcommerceDetailJob.user_id == current_user.id,
    ).order_by(EcommerceDetailJob.created_at.desc())
    total = query.count()
    rows = query.offset(offset).limit(min(limit, 200)).all()
    return {
        "ok": True,
        "total": total,
        "jobs": [
            {
                "job_id": r.job_id,
                "status": r.status,
                "product_name": r.product_name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "has_suite_bundle": bool((r.saved_assets or {}).get("suite_bundle")),
            }
            for r in rows
        ],
    }

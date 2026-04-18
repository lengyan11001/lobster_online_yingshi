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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

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
    resolve_public_image_for_pipeline,
    resolve_reference_images_for_pipeline,
    run_pipeline_sync,
)
from ..services.comfly_veo_exec import _resolve_comfly_credentials
from .assets import _save_bytes_or_tos
from .auth import _ServerUser, get_current_user_media_edit

logger = logging.getLogger(__name__)
router = APIRouter()


class EcommerceProductImageItem(BaseModel):
    role: str = Field("front", description="素材角色，如 front / side / back / detail")
    asset_id: Optional[str] = Field(None, description="素材 ID，与 image_url 二选一")
    image_url: Optional[str] = Field(None, description="素材公网 URL，与 asset_id 二选一")


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


class EcommerceDetailPipelinePayload(BaseModel):
    asset_id: Optional[str] = Field(None, description="商品主图素材 ID，与 image_url 二选一")
    image_url: Optional[str] = Field(None, description="商品主图公网 URL，与 asset_id 二选一")
    product_images: List[EcommerceProductImageItem] = Field(default_factory=list, description="结构化商品图列表，优先于 asset_id / image_url")
    product_name_hint: str = ""
    product_direction_hint: str = ""
    reference_asset_ids: List[str] = Field(default_factory=list, description="补充参考图素材 ID")
    reference_image_urls: List[str] = Field(default_factory=list, description="补充参考图公网 URL")
    style_reference_asset_ids: List[str] = Field(default_factory=list, description="风格参考图素材 ID")
    style_reference_image_urls: List[str] = Field(default_factory=list, description="风格参考图公网 URL")
    sku: str = ""
    selling_points: List[EcommerceSellingPointItem] = Field(default_factory=list)
    specs: Dict[str, Any] = Field(default_factory=dict)
    style: str = ""
    icon_assets: List[EcommerceIconAssetItem] = Field(default_factory=list)
    scene_preferences: Optional[EcommerceScenePreferences] = None
    output_targets: Optional[EcommerceOutputTargets] = None
    detail_template_id: str = ""
    showcase_template_id: str = ""
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


def _application_root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[3]


def _default_runs_root() -> str:
    return str(_application_root_dir() / "_lobster_runtime" / "comfly_ecommerce_detail" / "runs")


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


def _finalize_visible_export(result: Dict[str, Any]) -> Dict[str, Any]:
    bundle = result.get("suite_bundle") if isinstance(result.get("suite_bundle"), dict) else {}
    root_dir = Path(str(bundle.get("root_dir") or "").strip())
    run_dir = Path(str(result.get("run_dir") or "").strip())
    if not bundle or not root_dir.is_dir():
        return result
    final_dir = _alloc_visible_export_dir(result)
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root_dir), str(final_dir))
    result["suite_bundle"] = _rewrite_suite_bundle_paths(bundle, final_dir)
    result["run_dir"] = str(final_dir)
    result["detail_dir"] = ""
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


def _validate_payload(pl: EcommerceDetailPipelinePayload) -> None:
    if bool(pl.asset_id and pl.image_url):
        raise HTTPException(status_code=400, detail="asset_id 和 image_url 不能同时传")
    for item in pl.product_images:
        if bool(item.asset_id and item.image_url):
            raise HTTPException(status_code=400, detail="product_images 中每项的 asset_id 和 image_url 不能同时传")
        if not item.asset_id and not item.image_url:
            raise HTTPException(status_code=400, detail="product_images 中每项都需要提供 asset_id 或 image_url")
    for item in pl.icon_assets:
        if not (item.icon or "").strip():
            raise HTTPException(status_code=400, detail="icon_assets 中每项都需要提供 icon 标识")
        if bool(item.asset_id and item.image_url):
            raise HTTPException(status_code=400, detail="icon_assets 中每项的 asset_id 和 image_url 不能同时传")
        if not item.asset_id and not item.image_url:
            raise HTTPException(status_code=400, detail="icon_assets 中每项都需要提供 asset_id 或 image_url")
    if not pl.product_images and not pl.asset_id and not pl.image_url:
        raise HTTPException(status_code=400, detail="请提供 product_images 或 asset_id / image_url")


async def _prepare_pipeline_input(
    *,
    pl: EcommerceDetailPipelinePayload,
    current_user: _ServerUser,
    db: Session,
    request: Request,
    effective_output_dir: str,
) -> Dict[str, object]:
    style_reference_images = resolve_reference_images_for_pipeline(
        user_id=current_user.id,
        db=db,
        request=request,
        asset_ids=pl.style_reference_asset_ids,
        image_urls=pl.style_reference_image_urls,
    )
    if pl.product_images:
        resolved_product_images: List[Dict[str, str]] = []
        for item in pl.product_images:
            resolved_url = resolve_public_image_for_pipeline(
                user_id=current_user.id,
                db=db,
                request=request,
                asset_id=item.asset_id,
                image_url=item.image_url,
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
        product_image = resolve_public_image_for_pipeline(
            user_id=current_user.id,
            db=db,
            request=request,
            asset_id=pl.asset_id,
            image_url=pl.image_url,
        )
        reference_images = []
    extra_reference_images = resolve_reference_images_for_pipeline(
        user_id=current_user.id,
        db=db,
        request=request,
        asset_ids=pl.reference_asset_ids,
        image_urls=pl.reference_image_urls,
    )
    for image_url in extra_reference_images + style_reference_images:
        if image_url != product_image and image_url not in reference_images:
            reference_images.append(image_url)
    resolved_icon_assets: List[Dict[str, str]] = []
    for item in pl.icon_assets:
        resolved_url = resolve_public_image_for_pipeline(
            user_id=current_user.id,
            db=db,
            request=request,
            asset_id=item.asset_id,
            image_url=item.image_url,
        )
        resolved_icon_assets.append({"icon": str(item.icon or "").strip(), "url": resolved_url})
    api_base, api_key = _resolve_comfly_credentials(current_user.id, db)
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


def _job_status_response(job: Dict[str, Any], *, include_full: bool) -> Dict[str, Any]:
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
            out["result"] = job.get("result")
    if status == "completed":
        if include_full:
            out["result"] = job.get("result")
            out["saved_assets"] = job.get("saved_assets") or {}
        progress = read_manifest_progress(str(job.get("job_output_dir") or ""))
        if progress:
            out["progress"] = _redact_progress_for_client(progress)
    return out


@router.post("/api/comfly-ecommerce-detail/pipeline/run")
async def ecommerce_detail_pipeline_run(
    body: EcommerceDetailRunBody,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
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
        if pl.auto_save:
            try:
                saved_assets = _save_pipeline_images(result=result, user_id=current_user.id, db=db)
                saved_assets = _rewrite_saved_suite_paths(saved_assets, Path(str((result.get("suite_bundle") or {}).get("root_dir") or "")))
            except Exception as e:
                logger.exception("[comfly_ecommerce_detail] auto_save failed")
                raise HTTPException(status_code=500, detail=f"流水线执行成功，但素材入库失败: {e}") from e
    finally:
        if internal_run_dir:
            _cleanup_internal_run_dir({"run_dir": internal_run_dir})
    return {"ok": True, "pipeline": "comfly_ecommerce_detail", "result": result, "saved_assets": saved_assets}


@router.post("/api/comfly-ecommerce-detail/pipeline/start")
async def ecommerce_detail_pipeline_start(
    body: EcommerceDetailRunBody,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
):
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
        auto_save=pl.auto_save,
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


def _enrich_saved_assets_urls(
    saved_assets: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """给 saved_assets.suite_bundle 里的 asset 对象补上 preview_url / open_url。"""
    from .assets import build_asset_file_url

    sa = dict(saved_assets) if saved_assets else {}
    suite = sa.get("suite_bundle")
    if not isinstance(suite, dict):
        return sa
    enriched_suite: Dict[str, Any] = {}
    for cat, rows in suite.items():
        if not isinstance(rows, list):
            enriched_suite[cat] = rows
            continue
        new_rows = []
        for item in rows:
            if not isinstance(item, dict):
                new_rows.append(item)
                continue
            item = dict(item)
            asset = item.get("asset")
            if isinstance(asset, dict):
                asset = dict(asset)
                aid = asset.get("asset_id", "")
                if aid and not asset.get("preview_url"):
                    url = build_asset_file_url(request, aid, expiry_sec=3600)
                    if url:
                        asset["preview_url"] = url
                        asset["open_url"] = url
                item["asset"] = asset
            new_rows.append(item)
        enriched_suite[cat] = new_rows
    sa["suite_bundle"] = enriched_suite
    return sa


@router.get("/api/comfly-ecommerce-detail/pipeline/jobs/{job_id}")
async def ecommerce_detail_pipeline_job_status(
    job_id: str,
    compact: bool = False,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db: Session = Depends(get_db),
    request: Request = None,
):
    job = get_job(job_id)
    if job:
        if int(job.get("user_id") or -1) != int(current_user.id):
            raise HTTPException(status_code=403, detail="无权查看该任务")
        return _job_status_response(job, include_full=not compact)
    db_job = db.query(EcommerceDetailJob).filter(
        EcommerceDetailJob.job_id == job_id,
        EcommerceDetailJob.user_id == current_user.id,
    ).first()
    if not db_job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    sa = _enrich_saved_assets_urls(db_job.saved_assets or {}, request) if request else (db_job.saved_assets or {})
    return {
        "ok": True,
        "job_id": db_job.job_id,
        "status": db_job.status,
        "product_name": db_job.product_name,
        "saved_assets": sa,
        "error": db_job.error,
        "created_at": db_job.created_at.isoformat() if db_job.created_at else None,
        "source": "database",
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

"""Comfly 带货整包流水线（OpenClaw 技能原脚本）：一次跑完分镜→多段 Veo→可选拼接；成品可自动 save-url 入库。

支持两种用法：
- 同步：POST /api/comfly-daihuo/pipeline/run（长连接，与原先一致）
- 异步：POST /api/comfly-daihuo/pipeline/start 返回 job_id，定时 GET /api/comfly-daihuo/pipeline/jobs/{job_id}
  查看 running 时的 manifest 进度；completed 后响应中含 result 与 saved_assets（auto_save 时）。"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import SessionLocal, get_db
from ..services.comfly_daihuo_job_store import (
    create_job_record,
    get_job,
    read_manifest_progress,
    update_job,
)
from ..services.comfly_daihuo_pipeline_runner import (
    _api_base_for_pipeline,
    build_pipeline_input,
    collect_video_urls_from_pipeline_result,
    resolve_product_image_for_pipeline,
    run_storyboard_pipeline_sync,
    save_merged_local_pipeline_video,
)
from ..services.comfly_veo_exec import _resolve_comfly_credentials
from .assets import (
    SaveAssetReq,
    _compute_save_url_dedupe_key,
    _final_save_url_dedupe_key,
    _resolve_v3_tasks_url_for_download,
    _save_asset_from_url_locked,
    _save_url_lock_for,
)
from .auth import _ServerUser, get_current_user_media_edit

logger = logging.getLogger(__name__)
router = APIRouter()


class ComflyDaihuoPipelinePayload(BaseModel):
    """与 comfly.daihuo 分步能力互斥：本接口一次跑完 OpenClaw 技能包内 Python 流水线。"""

    asset_id: Optional[str] = Field(None, description="素材库商品图 ID（与 image_url 二选一）")
    image_url: Optional[str] = Field(None, description="公网商品图 URL（与 asset_id 二选一）")
    merge_clips: bool = Field(True, description="是否用 FFmpeg 拼接多段（技能包默认行为）")
    storyboard_count: Optional[int] = Field(None, ge=1, le=8, description="分镜条数，默认脚本内 5")
    auto_save: bool = Field(True, description="为每段成功 mp4 调用与前端一致的 save-url 入库")
    platform: str = ""
    country: str = ""
    language: str = ""
    output_dir: Optional[str] = Field(None, description="流水线 runs 根目录，默认 skills/.../runs")
    # 异步 start 专用：是否把每次 run 隔离到 job_runs/<job_id>/ 下，便于 manifest 轮询
    isolate_job_dir: bool = Field(
        True,
        description="start 任务默认 true：输出写入 job_runs/<job_id>/，便于查询进度；同步 run 忽略",
    )
    image_request_style: Optional[str] = Field(
        None,
        description="图生 Body 形态：openai_images（model + prompt/n/size，Comfly 网关要求 model）或 comfly（model/aspect_ratio/可选 image）；默认 openai_images",
    )


class ComflyDaihuoRunBody(BaseModel):
    payload: ComflyDaihuoPipelinePayload


def _default_runs_root() -> str:
    return str(
        Path(__file__).resolve().parents[3]
        / "skills"
        / "comfly_veo3_daihuo_video"
        / "runs"
    )


def _validate_payload(pl: ComflyDaihuoPipelinePayload) -> None:
    if bool(pl.asset_id and pl.image_url):
        raise HTTPException(status_code=400, detail="asset_id 与 image_url 请勿同时传")
    if not pl.asset_id and not pl.image_url:
        raise HTTPException(status_code=400, detail="请提供 asset_id 或 image_url")


async def _prepare_pipeline_input(
    *,
    pl: ComflyDaihuoPipelinePayload,
    current_user: _ServerUser,
    db: Session,
    request: Request,
    effective_output_dir: str,
) -> Dict[str, Any]:
    product_image = resolve_product_image_for_pipeline(
        user_id=current_user.id,
        db=db,
        request=request,
        asset_id=pl.asset_id,
        image_url=pl.image_url,
    )
    api_base, api_key = _resolve_comfly_credentials(current_user.id, db, request)
    pipe_base = _api_base_for_pipeline(api_base)
    logger.info(
        "[comfly.daihuo.pipeline] credentials user_id=%s key_len=%s api_base=%s pipeline_base=%s",
        current_user.id,
        len((api_key or "").strip()),
        (api_base or "")[:120],
        (pipe_base or "")[:120],
    )
    return build_pipeline_input(
        product_image=product_image,
        api_key=api_key,
        api_base=api_base,
        merge_clips=pl.merge_clips,
        storyboard_count=pl.storyboard_count,
        output_dir=effective_output_dir,
        platform=pl.platform,
        country=pl.country,
        language=pl.language,
        image_request_style=pl.image_request_style,
    )


async def _save_pipeline_videos(
    *,
    urls: List[tuple],
    request: Optional[Request],
    current_user: _ServerUser,
    video_model: str,
) -> List[Dict[str, Any]]:
    saved: List[Dict[str, Any]] = []
    for url, task_id, title_hint in urls:
        body = SaveAssetReq(
            url=url,
            media_type="video",
            tags="auto,comfly.daihuo.pipeline",
            prompt=title_hint[:500] if title_hint else None,
            model=(video_model or "")[:128] or None,
            generation_task_id=task_id[:128] if task_id else None,
        )
        effective = await _resolve_v3_tasks_url_for_download(
            body.url, "video", current_user, request=request
        )
        base_dk = _compute_save_url_dedupe_key(body.url, effective, body.dedupe_hint_url)
        dk = _final_save_url_dedupe_key(
            base_dk,
            body.generation_task_id,
            dedupe_hint_url=body.dedupe_hint_url,
            body_url=body.url,
        )
        async with _save_url_lock_for(current_user.id, dk):
            row = await _save_asset_from_url_locked(
                dk, body, request, current_user, effective_url_resolved=effective
            )
        saved.append({"source_url": url, "task_id": task_id, "asset": row})
        logger.info(
            "[comfly_daihuo] save-url ok asset_id=%s task_id=%s",
            row.get("asset_id"),
            (task_id or "")[:48],
        )
    return saved


def _video_model_from_result(result: Dict[str, Any]) -> str:
    cfg = result.get("config") if isinstance(result.get("config"), dict) else {}
    return str(cfg.get("video_model") or "") if isinstance(cfg, dict) else ""


async def _daihuo_job_runner(job_id: str) -> None:
    """后台跑流水线；完成后按任务记录中的 auto_save 入库。"""
    j = get_job(job_id)
    if not j:
        return
    inp = deepcopy(j.get("inp") or {})
    auto_save = bool(j.get("auto_save"))
    user_id = int(j.get("user_id") or 0)

    try:
        result = await asyncio.to_thread(run_storyboard_pipeline_sync, inp)
    except Exception as e:
        logger.exception("[comfly_daihuo] job %s pipeline failed", job_id[:12])
        update_job(job_id, status="failed", error=str(e)[:2000])
        return

    video_model = _video_model_from_result(result)
    saved_assets: List[Dict[str, Any]] = []
    if auto_save:
        fv = result.get("final_video") if isinstance(result.get("final_video"), dict) else {}
        db = SessionLocal()
        try:
            current_user = _ServerUser(id=user_id)
            merged_row = None
            if fv.get("kind") == "merged_local" and (fv.get("path") or "").strip():
                try:
                    merged_row = save_merged_local_pipeline_video(
                        local_path=str(fv["path"]).strip(),
                        user_id=user_id,
                        db=db,
                        video_model=video_model,
                    )
                except Exception:
                    logger.exception("[comfly_daihuo] job %s merged local save failed", job_id[:12])
                    merged_row = None
            if merged_row:
                saved_assets.append(merged_row)
            else:
                pairs = collect_video_urls_from_pipeline_result(result)
                if pairs:
                    try:
                        saved_assets = await _save_pipeline_videos(
                            urls=pairs,
                            request=None,
                            current_user=current_user,
                            video_model=video_model,
                        )
                    except HTTPException as he:
                        detail = he.detail
                        if not isinstance(detail, str):
                            detail = str(detail)
                        update_job(
                            job_id,
                            status="failed",
                            error=f"流水线成功但入库失败: {detail}",
                            result=result,
                        )
                        return
        finally:
            db.close()

    update_job(
        job_id,
        status="completed",
        error=None,
        result=result,
        saved_assets=saved_assets,
    )
    logger.info(
        "[comfly_daihuo] job %s completed saved=%s",
        job_id[:12],
        len(saved_assets),
    )


def _redact_progress_for_client(prog: Any) -> Any:
    """轮询进度里去掉本机路径字段，避免前端/对话展示 manifest 绝对路径。"""
    if not isinstance(prog, dict):
        return prog
    red = {k: v for k, v in prog.items() if k not in ("manifest_file", "run_dir")}
    ls = red.get("last_steps")
    if isinstance(ls, list):
        clean_ls = []
        for it in ls:
            if not isinstance(it, dict):
                continue
            one = dict(it)
            err = one.get("error")
            if isinstance(err, str) and err.strip():
                one["error"] = re.sub(
                    r"(?:[A-Za-z]:[/\\][^\s\"'<>|]{2,320}|"
                    r"(?:\\\\|/)[^\s\"'<>|]{0,320}(?:[/\\](?:skills|job_runs|runs)[/\\]|\.py\b)[^\s\"'<>|]{0,320})",
                    "…",
                    err.strip(),
                    flags=re.IGNORECASE,
                )[:400]
            clean_ls.append(one)
        red["last_steps"] = clean_ls
    return red


def _job_status_response(job: Dict[str, Any], *, include_full: bool) -> Dict[str, Any]:
    st = (job.get("status") or "").strip()
    out: Dict[str, Any] = {
        "ok": True,
        "job_id": job.get("job_id"),
        "status": st,
        "auto_save": job.get("auto_save"),
        "created_at_ts": job.get("created_at_ts"),
        "updated_at_ts": job.get("updated_at_ts"),
    }
    job_out = job.get("job_output_dir") or ""
    if st == "running":
        prog = read_manifest_progress(str(job_out))
        if prog:
            out["progress"] = _redact_progress_for_client(prog)
    if st == "failed":
        fe = job.get("error")
        if isinstance(fe, str) and fe.strip():
            out["error"] = re.sub(
                r"(?:[A-Za-z]:[/\\][^\s\"'<>|]{2,320}|"
                r"(?:\\\\|/)[^\s\"'<>|]{0,320}(?:[/\\](?:skills|job_runs|runs)[/\\]|\.py\b)[^\s\"'<>|]{0,320})",
                "…",
                fe.strip(),
                flags=re.IGNORECASE,
            )[:800]
        else:
            out["error"] = fe
        if include_full and job.get("result") is not None:
            out["result"] = job.get("result")
    if st == "completed":
        if include_full:
            out["result"] = job.get("result")
            out["saved_assets"] = job.get("saved_assets") or []
        prog = read_manifest_progress(str(job_out))
        if prog:
            out["progress"] = _redact_progress_for_client(prog)
    return out


@router.post("/api/comfly-daihuo/pipeline/run")
async def comfly_daihuo_pipeline_run(
    body: ComflyDaihuoRunBody,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db=Depends(get_db),
):
    pl = body.payload
    _validate_payload(pl)

    runs_root = (pl.output_dir or "").strip() or _default_runs_root()
    inp = await _prepare_pipeline_input(
        pl=pl,
        current_user=current_user,
        db=db,
        request=request,
        effective_output_dir=runs_root,
    )

    logger.info(
        "[comfly_daihuo] pipeline start (sync) user_id=%s merge=%s",
        current_user.id,
        pl.merge_clips,
    )
    try:
        result = await asyncio.to_thread(run_storyboard_pipeline_sync, inp)
    except Exception as e:
        logger.exception("[comfly_daihuo] pipeline failed user_id=%s", current_user.id)
        raise HTTPException(status_code=500, detail=str(e)[:2000]) from e

    video_model = _video_model_from_result(result)
    saved_assets: List[Dict[str, Any]] = []
    if pl.auto_save:
        fv = result.get("final_video") if isinstance(result.get("final_video"), dict) else {}
        merged_row = None
        if fv.get("kind") == "merged_local" and (fv.get("path") or "").strip():
            try:
                merged_row = save_merged_local_pipeline_video(
                    local_path=str(fv["path"]).strip(),
                    user_id=current_user.id,
                    db=db,
                    video_model=video_model,
                )
            except Exception:
                logger.exception("[comfly_daihuo] merged local save failed, fallback to clip URLs")
                merged_row = None
        if merged_row:
            saved_assets.append(merged_row)
        else:
            pairs = collect_video_urls_from_pipeline_result(result)
            if pairs:
                try:
                    saved_assets = await _save_pipeline_videos(
                        urls=pairs,
                        request=request,
                        current_user=current_user,
                        video_model=video_model,
                    )
                except HTTPException:
                    raise
                except Exception as e:
                    logger.exception("[comfly_daihuo] auto_save failed")
                    raise HTTPException(status_code=500, detail=f"流水线成功但入库失败: {e}") from e

    return {
        "ok": True,
        "pipeline": "comfly_veo3_daihuo_video",
        "result": result,
        "saved_assets": saved_assets,
    }


@router.post("/api/comfly-daihuo/pipeline/start")
async def comfly_daihuo_pipeline_start(
    body: ComflyDaihuoRunBody,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
    db=Depends(get_db),
):
    pl = body.payload
    _validate_payload(pl)

    runs_root = (pl.output_dir or "").strip() or _default_runs_root()
    job_id = uuid.uuid4().hex
    if pl.isolate_job_dir:
        effective_dir = str(Path(runs_root) / "job_runs" / job_id)
    else:
        effective_dir = runs_root

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

    def _log_daihuo_task(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
            if exc is not None:
                logger.exception("[comfly_daihuo] 后台任务异常 job_id=%s", job_id[:12])
        except asyncio.CancelledError:
            pass

    t = asyncio.create_task(_daihuo_job_runner(job_id))
    t.add_done_callback(_log_daihuo_task)
    logger.info(
        "[comfly_daihuo] pipeline job queued user_id=%s job_id=%s merge=%s",
        current_user.id,
        job_id[:12],
        pl.merge_clips,
    )
    return {
        "ok": True,
        "async": True,
        "job_id": job_id,
        "poll_path": f"/api/comfly-daihuo/pipeline/jobs/{job_id}",
    }


@router.get("/api/comfly-daihuo/pipeline/jobs/{job_id}")
async def comfly_daihuo_pipeline_job_status(
    job_id: str,
    compact: bool = False,
    current_user: _ServerUser = Depends(get_current_user_media_edit),
):
    j = get_job(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if int(j.get("user_id") or -1) != int(current_user.id):
        raise HTTPException(status_code=403, detail="无权查看该任务")
    return _job_status_response(j, include_full=not compact)

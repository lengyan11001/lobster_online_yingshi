"""发布账号：创作者数据按间隔定时同步（分钟）+ 目标与要求文案。"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import _ServerUser, get_current_user_for_local
from ..db import SessionLocal, get_db
from ..datetime_iso import isoformat_utc
from ..models import (
    CreatorScheduleReviewSnapshot,
    CreatorScheduleTaskLog,
    PublishAccount,
    PublishAccountCreatorSchedule,
)
from ..services.internal_chat_client import forward_chat_auth_from_request
from ..services.schedule_review_snapshots import (
    append_review_snapshot,
    snapshot_to_list_item,
)

logger = logging.getLogger(__name__)

router = APIRouter()

INTERVAL_MIN = 1
INTERVAL_MAX = 10080  # 7 天


def _pydantic_body_has_field(body: BaseModel, name: str) -> bool:
    if hasattr(body, "model_fields_set"):
        return name in getattr(body, "model_fields_set", set())
    fs = getattr(body, "__fields_set__", None)
    if fs is not None:
        return name in fs
    return False


def _parse_iso_to_utc_naive(s: str) -> datetime:
    """将 ISO-8601（UTC 或带偏移）解析为 naive UTC（与库中其它时间字段一致）。"""
    raw = (s or "").strip()
    if not raw:
        raise ValueError("empty")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None)


class CreatorScheduleOut(BaseModel):
    account_id: int
    enabled: bool
    interval_minutes: int
    next_run_at: Optional[str] = None
    schedule_kind: Literal["image", "video"] = "image"
    video_source_asset_id: Optional[str] = None
    requirements_text: Optional[str] = None
    last_run_at: Optional[str] = None
    last_run_error: Optional[str] = None
    schedule_publish_mode: Literal["immediate", "review"] = "immediate"
    review_variant_count: int = 3
    review_first_eta_at: Optional[str] = None
    review_drafts_json: Optional[List[Any]] = None
    review_confirmed: bool = False
    review_selected_slot: int = 0


class CreatorSchedulePut(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(60, ge=INTERVAL_MIN, le=INTERVAL_MAX, description="每隔多少分钟执行一次")
    schedule_kind: Literal["image", "video"] = Field("image", description="图文 或 视频")
    video_source_asset_id: Optional[str] = Field(
        None,
        description="视频模式可选：素材库 asset_id；有则图生视频，无则文生视频",
        max_length=64,
    )
    requirements_text: Optional[str] = Field(None, description="图文=描述需求；视频=生产要求")
    schedule_publish_mode: Optional[Literal["immediate", "review"]] = None
    review_variant_count: Optional[int] = Field(None, ge=1, le=10, description="审核模式下生成几条草稿")
    review_first_eta_at: Optional[str] = Field(
        None,
        description="审核模式：首条预计发布时间 ISO-8601（UTC），null 表示未指定（沿用下次执行/默认推算）",
    )
    review_drafts_json: Optional[List[Any]] = None
    review_confirmed: Optional[bool] = None
    review_selected_slot: Optional[int] = Field(None, ge=0, le=9)


class ReviewGenerateBody(BaseModel):
    variant_count: int = Field(3, ge=1, le=10)


class ReviewRegenerateSlotBody(BaseModel):
    slot_index: int = Field(0, ge=0, le=9)


class ReviewGenerateAssetsBody(BaseModel):
    """省略或 null 表示对所有槽位执行生成。"""
    slot_indices: Optional[List[int]] = None


def _get_or_create_schedule(
    db: Session, user_id: int, account_id: int
) -> PublishAccountCreatorSchedule:
    row = (
        db.query(PublishAccountCreatorSchedule)
        .filter(
            PublishAccountCreatorSchedule.account_id == account_id,
            PublishAccountCreatorSchedule.user_id == user_id,
        )
        .first()
    )
    if row:
        return row
    row = PublishAccountCreatorSchedule(
        user_id=user_id,
        account_id=account_id,
        enabled=False,
        interval_minutes=60,
        next_run_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _to_out(row: PublishAccountCreatorSchedule) -> dict:
    sk = (getattr(row, "schedule_kind", None) or "image").strip().lower()
    if sk not in ("image", "video"):
        sk = "image"
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode not in ("immediate", "review"):
        mode = "immediate"
    return {
        "account_id": row.account_id,
        "enabled": row.enabled,
        "interval_minutes": row.interval_minutes,
        "next_run_at": isoformat_utc(row.next_run_at),
        "schedule_kind": sk,
        "video_source_asset_id": getattr(row, "video_source_asset_id", None),
        "requirements_text": row.requirements_text,
        "last_run_at": isoformat_utc(row.last_run_at),
        "last_run_error": row.last_run_error,
        "schedule_publish_mode": mode,
        "review_variant_count": int(getattr(row, "review_variant_count", None) or 3),
        "review_first_eta_at": isoformat_utc(getattr(row, "review_first_eta_at", None)),
        "review_drafts_json": getattr(row, "review_drafts_json", None),
        "review_confirmed": bool(getattr(row, "review_confirmed", False)),
        "review_selected_slot": int(getattr(row, "review_selected_slot", None) or 0),
    }


async def _bootstrap_creator_schedule_sync(account_id: int, user_id: int, interval_minutes: int) -> None:
    """开启/改间隔后：立即跑一轮作品同步；若填写了需求文案则再跑一轮与到点相同的 POST /chat 编排；最后把 next_run_at 顺延 interval_minutes。"""
    from .creator_content import perform_creator_content_sync
    from ..services.creator_schedule_task_log import (
        compute_final_status,
        finish_task_log,
        start_task_log,
        update_task_log,
    )
    from ..services.schedule_orchestration_run import run_schedule_orchestration_chat
    from ..services.schedule_review_timing import (
        compute_next_review_run_at_after_orchestration,
        compute_next_review_run_at_naive,
    )

    db = SessionLocal()
    log_id: Optional[int] = None
    try:
        sch = (
            db.query(PublishAccountCreatorSchedule)
            .filter(
                PublishAccountCreatorSchedule.account_id == account_id,
                PublishAccountCreatorSchedule.user_id == user_id,
            )
            .first()
        )
        if not sch or not sch.enabled:
            return

        log_row = start_task_log(db, user_id=user_id, account_id=account_id, trigger="bootstrap")
        log_id = log_row.id

        sync_ok = False
        sync_err: Optional[str] = None
        item_count: Optional[int] = None

        update_task_log(db, log_id, phase="作品同步中", detail="拉取抖音/小红书创作者作品列表（若支持）")
        try:
            sync_result = await perform_creator_content_sync(
                db,
                user_id=user_id,
                account_id=account_id,
                headless=None,
            )
            db.refresh(sch)
            sch.last_run_at = datetime.utcnow()
            sch.last_run_error = None
            sync_ok = bool(sync_result.get("ok"))
            item_count = sync_result.get("item_count")
            sync_err = sync_result.get("error")
        except ValueError as e:
            db.refresh(sch)
            sch.last_run_at = datetime.utcnow()
            sch.last_run_error = str(e)
            sync_ok = False
            sync_err = str(e)
            logger.warning("bootstrap creator sync value error account_id=%s: %s", account_id, e)
        except Exception as e:
            db.refresh(sch)
            sch.last_run_at = datetime.utcnow()
            sch.last_run_error = str(e)
            sync_ok = False
            sync_err = str(e)
            logger.exception("bootstrap creator sync account_id=%s", account_id)

        now_b = datetime.utcnow()
        mode_b = (getattr(sch, "schedule_publish_mode", None) or "immediate").strip().lower()
        dr_b = getattr(sch, "review_drafts_json", None) or []
        if (
            mode_b == "review"
            and bool(getattr(sch, "review_confirmed", False))
            and isinstance(dr_b, list)
            and len(dr_b) > 0
        ):
            sch.next_run_at = compute_next_review_run_at_naive(sch, now_b)
        else:
            sch.next_run_at = now_b + timedelta(minutes=max(INTERVAL_MIN, interval_minutes))
        db.commit()
        logger.info(
            "bootstrap creator schedule done account_id=%s next_run_at=%s",
            account_id,
            sch.next_run_at,
        )
        update_task_log(
            db,
            log_id,
            phase="已顺延下次执行时间",
            sync_ok=sync_ok,
            sync_error=sync_err,
            item_count=item_count,
        )

        req = (sch.requirements_text or "").strip()
        had_orch = False
        orch_ok: Optional[bool] = None
        orch_err: Optional[str] = None

        if req:
            acct = (
                db.query(PublishAccount)
                .filter(
                    PublishAccount.id == account_id,
                    PublishAccount.user_id == user_id,
                )
                .first()
            )
            if acct:
                had_orch = True
                update_task_log(
                    db,
                    log_id,
                    phase="智能编排中",
                    detail="POST /chat（生成/发布等，可能耗时数十分钟）",
                )
                skip_orch = False
                mode_o = (getattr(sch, "schedule_publish_mode", None) or "immediate").strip().lower()
                dr_o = getattr(sch, "review_drafts_json", None) or []
                if (
                    mode_o == "review"
                    and bool(getattr(sch, "review_confirmed", False))
                    and isinstance(dr_o, list)
                    and len(dr_o) > 0
                ):
                    nra = getattr(sch, "next_run_at", None)
                    if (
                        nra is not None
                        and isinstance(nra, datetime)
                        and nra > datetime.utcnow()
                    ):
                        skip_orch = True
                if skip_orch:
                    had_orch = False
                    orch_ok = None
                    orch_err = None
                else:
                    try:
                        orch_res = await run_schedule_orchestration_chat(sch, acct)
                        if orch_res.get("skipped"):
                            had_orch = False
                            orch_ok = None
                            orch_err = None
                        else:
                            orch_ok = bool(orch_res.get("ok"))
                            orch_err = orch_res.get("error")
                    except Exception as e:
                        orch_ok = False
                        orch_err = str(e)
                        logger.exception("bootstrap schedule orchestration account_id=%s", account_id)
                    db.refresh(sch)
                    if (
                        had_orch
                        and orch_ok is True
                        and mode_o == "review"
                        and bool(getattr(sch, "review_confirmed", False))
                    ):
                        dr2 = getattr(sch, "review_drafts_json", None) or []
                        if isinstance(dr2, list) and len(dr2) > 0:
                            sch.next_run_at = compute_next_review_run_at_after_orchestration(
                                sch, datetime.utcnow()
                            )
                            db.commit()
                    elif (
                        had_orch
                        and orch_ok is not True
                        and mode_o == "review"
                        and bool(getattr(sch, "review_confirmed", False))
                    ):
                        dr2 = getattr(sch, "review_drafts_json", None) or []
                        if isinstance(dr2, list) and len(dr2) > 0:
                            sch.next_run_at = datetime.utcnow() + timedelta(
                                minutes=max(INTERVAL_MIN, interval_minutes)
                            )
                            db.commit()
            else:
                had_orch = True
                orch_ok = False
                orch_err = "发布账号不存在，无法编排"

        final_status = compute_final_status(
            sync_ok=sync_ok,
            had_orchestration=had_orch,
            orchestration_ok=orch_ok,
        )
        finish_task_log(
            db,
            log_id,
            status=final_status,
            phase="已完成",
            detail="",
            sync_ok=sync_ok,
            sync_error=sync_err,
            item_count=item_count,
            orchestration_ok=orch_ok,
            orchestration_error=orch_err,
        )
        log_id = None
    except Exception as e:
        logger.exception("bootstrap creator schedule task log account_id=%s", account_id)
        if log_id is not None:
            try:
                finish_task_log(
                    db,
                    log_id,
                    status="failed",
                    phase="异常中断",
                    detail=str(e),
                )
            except Exception:
                logger.exception("finish_task_log after bootstrap failure")
    finally:
        db.close()


def _task_log_to_dict(row: CreatorScheduleTaskLog) -> dict:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "trigger": row.trigger,
        "status": row.status,
        "phase": row.phase,
        "detail": row.detail,
        "sync_ok": row.sync_ok,
        "sync_error": row.sync_error,
        "item_count": row.item_count,
        "orchestration_ok": row.orchestration_ok,
        "orchestration_error": row.orchestration_error,
        "started_at": isoformat_utc(row.started_at),
        "finished_at": isoformat_utc(row.finished_at),
        "updated_at": isoformat_utc(row.updated_at),
    }


@router.get(
    "/api/accounts/{account_id}/creator-schedule/runs",
    summary="定时任务执行记录（每次保存首轮 / 到点触发）",
)
def list_creator_schedule_runs(
    account_id: int,
    limit: int = Query(50, ge=1, le=200),
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    rows = (
        db.query(CreatorScheduleTaskLog)
        .filter(
            CreatorScheduleTaskLog.user_id == current_user.id,
            CreatorScheduleTaskLog.account_id == account_id,
        )
        .order_by(CreatorScheduleTaskLog.started_at.desc())
        .limit(limit)
        .all()
    )
    return {"runs": [_task_log_to_dict(r) for r in rows]}


@router.get("/api/accounts/{account_id}/creator-schedule", summary="获取账号创作者同步定时配置")
def get_creator_schedule(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    return _to_out(row)


@router.put("/api/accounts/{account_id}/creator-schedule", summary="保存账号创作者同步定时配置")
async def put_creator_schedule(
    account_id: int,
    body: CreatorSchedulePut,
    background_tasks: BackgroundTasks,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")

    iv = int(body.interval_minutes)
    if iv < INTERVAL_MIN or iv > INTERVAL_MAX:
        raise HTTPException(400, detail=f"interval_minutes 须在 {INTERVAL_MIN}～{INTERVAL_MAX} 之间")

    def _norm_schedule_kind(k: Optional[str]) -> str:
        s = (k or "image").strip().lower()
        return s if s in ("image", "video") else "image"

    def _norm_video_asset_id(kind: str, v: Optional[str]) -> str:
        if kind != "video":
            return ""
        return (v or "").strip()

    row = _get_or_create_schedule(db, current_user.id, account_id)
    was_enabled = row.enabled
    prev_iv = row.interval_minutes
    prev_req = (row.requirements_text or "").strip()
    prev_kind = _norm_schedule_kind(getattr(row, "schedule_kind", None))
    prev_vaid = _norm_video_asset_id(prev_kind, getattr(row, "video_source_asset_id", None))
    prev_mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if prev_mode not in ("immediate", "review"):
        prev_mode = "immediate"
    prev_rvc = int(getattr(row, "review_variant_count", None) or 3)
    prev_drafts = getattr(row, "review_drafts_json", None)
    prev_conf = bool(getattr(row, "review_confirmed", False))
    prev_slot = int(getattr(row, "review_selected_slot", None) or 0)

    row.enabled = bool(body.enabled)
    row.interval_minutes = iv
    row.schedule_kind = body.schedule_kind
    if body.schedule_kind == "image":
        row.video_source_asset_id = None
    else:
        vaid = (body.video_source_asset_id or "").strip() or None
        row.video_source_asset_id = vaid
    row.requirements_text = body.requirements_text
    if body.schedule_publish_mode is not None:
        m = body.schedule_publish_mode.strip().lower()
        row.schedule_publish_mode = m if m in ("immediate", "review") else "immediate"
    if body.review_variant_count is not None:
        row.review_variant_count = max(1, min(10, int(body.review_variant_count)))
    if _pydantic_body_has_field(body, "review_first_eta_at"):
        ra = body.review_first_eta_at
        if ra is not None and str(ra).strip():
            try:
                row.review_first_eta_at = _parse_iso_to_utc_naive(str(ra).strip())
            except ValueError as e:
                raise HTTPException(
                    status_code=400, detail=f"review_first_eta_at 无效: {e}"
                ) from e
        else:
            row.review_first_eta_at = None
    if body.review_drafts_json is not None:
        row.review_drafts_json = body.review_drafts_json
    if body.review_confirmed is not None:
        row.review_confirmed = bool(body.review_confirmed)
    if _pydantic_body_has_field(body, "review_selected_slot") and body.review_selected_slot is not None:
        row.review_selected_slot = max(0, min(9, int(body.review_selected_slot)))

    if (getattr(row, "schedule_publish_mode", None) or "immediate") == "immediate":
        row.review_confirmed = False
        row.review_drafts_json = None
        row.review_selected_slot = 0
        row.review_first_eta_at = None

    row.updated_at = datetime.utcnow()

    if not row.enabled:
        row.next_run_at = None

    db.commit()
    db.refresh(row)

    new_req = (row.requirements_text or "").strip()
    new_kind = _norm_schedule_kind(row.schedule_kind)
    new_vaid = _norm_video_asset_id(new_kind, row.video_source_asset_id)
    new_mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if new_mode not in ("immediate", "review"):
        new_mode = "immediate"
    new_rvc = int(getattr(row, "review_variant_count", None) or 3)
    new_drafts = getattr(row, "review_drafts_json", None)
    new_conf = bool(getattr(row, "review_confirmed", False))
    new_slot = int(getattr(row, "review_selected_slot", None) or 0)

    def _drafts_eq(a: Any, b: Any) -> bool:
        try:
            return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(
                b, sort_keys=True, ensure_ascii=False
            )
        except Exception:
            return a == b

    config_changed = (
        prev_iv != iv
        or prev_req != new_req
        or prev_kind != new_kind
        or prev_vaid != new_vaid
        or prev_mode != new_mode
        or prev_rvc != new_rvc
        or not _drafts_eq(prev_drafts, new_drafts)
        or prev_conf != new_conf
        or prev_slot != new_slot
    )
    # 首轮：新开启 / 改间隔或需求或模式或素材 / 已开但 next 丢失，均立即 bootstrap（同步+按需编排+顺延 next_run_at）
    should_bootstrap = row.enabled and (
        not was_enabled or config_changed or row.next_run_at is None
    )
    if should_bootstrap:
        background_tasks.add_task(_bootstrap_creator_schedule_sync, account_id, current_user.id, iv)

    logger.info(
        "creator-schedule saved account_id=%s enabled=%s interval_min=%s immediate=%s config_changed=%s",
        account_id,
        row.enabled,
        iv,
        should_bootstrap,
        config_changed,
    )
    return _to_out(row)


@router.post("/api/accounts/{account_id}/creator-schedule/review-generate", summary="审核模式：智能生成多版提示词（将发给 AI 的 prompt，非素材）")
async def post_review_generate(
    request: Request,
    account_id: int,
    body: ReviewGenerateBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode != "review":
        raise HTTPException(400, detail="请先将发布模式设为「审核后发布」")
    from ..services.schedule_review_draft_generate import generate_review_drafts_via_chat

    ut, xi = forward_chat_auth_from_request(request)
    try:
        drafts = await generate_review_drafts_via_chat(
            user_id=current_user.id,
            platform=str(acct.platform or ""),
            nickname=str(acct.nickname or ""),
            schedule_kind=(getattr(row, "schedule_kind", None) or "image").strip().lower(),
            requirements_text=(row.requirements_text or "").strip(),
            variant_count=body.variant_count,
            replace_slot_hint=None,
            video_source_asset_id=getattr(row, "video_source_asset_id", None),
            user_bearer_token=ut,
            x_installation_id=xi or None,
        )
    except ValueError as e:
        err = str(e)
        append_review_snapshot(
            db,
            user_id=current_user.id,
            account_id=account_id,
            kind="prompts",
            status="failed",
            drafts_json=row.review_drafts_json,
            error_detail=err,
        )
        db.commit()
        raise HTTPException(400, detail=err) from e
    row.review_drafts_json = drafts
    row.review_confirmed = False
    row.review_selected_slot = 0
    row.updated_at = datetime.utcnow()
    append_review_snapshot(
        db,
        user_id=current_user.id,
        account_id=account_id,
        kind="prompts",
        status="ok",
        drafts_json=drafts,
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post(
    "/api/accounts/{account_id}/creator-schedule/review-generate-assets",
    summary="审核模式：按槽位已保存的 prompt 调用能力生成素材与回复（禁止发布）",
)
async def post_review_generate_assets(
    request: Request,
    account_id: int,
    body: ReviewGenerateAssetsBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode != "review":
        raise HTTPException(400, detail="请先将发布模式设为「审核后发布」")
    drafts = row.review_drafts_json
    if not isinstance(drafts, list) or not drafts:
        raise HTTPException(400, detail="请先生成或填写提示词草稿")
    from ..services.schedule_review_execute import (
        ensure_prompt_draft,
        execute_review_slot_generation,
        merge_generated_into_slot,
        resolved_attachment_ids_for_review_chat,
    )

    new_drafts: List[Any] = [ensure_prompt_draft(d) for d in drafts]
    indices = body.slot_indices
    if indices is None:
        target = list(range(len(new_drafts)))
    else:
        target = sorted({int(i) for i in indices if i is not None})
    for i in target:
        if i < 0 or i >= len(new_drafts):
            raise HTTPException(400, detail=f"槽位序号 {i} 无效")
    sch_kind = (getattr(row, "schedule_kind", None) or "image").strip().lower()
    sch_video_asset = getattr(row, "video_source_asset_id", None)
    ut, xi = forward_chat_auth_from_request(request)
    for i in target:
        slot = dict(new_drafts[i])
        if not (slot.get("prompt") or "").strip():
            raise HTTPException(400, detail=f"第 {i + 1} 条缺少提示词，请先填写或「智能生成提示词」")
        merged_att = resolved_attachment_ids_for_review_chat(
            slot.get("attachment_asset_ids"),
            schedule_kind=sch_kind,
            video_source_asset_id=sch_video_asset,
        )
        if merged_att:
            slot["attachment_asset_ids"] = merged_att
        try:
            res = await execute_review_slot_generation(
                user_id=current_user.id,
                user_message=str(slot["prompt"]),
                attachment_asset_ids=slot.get("attachment_asset_ids"),
                schedule_kind=sch_kind,
                video_source_asset_id=sch_video_asset,
                user_bearer_token=ut,
                x_installation_id=xi or None,
            )
        except ValueError as e:
            err = str(e)
            append_review_snapshot(
                db,
                user_id=current_user.id,
                account_id=account_id,
                kind="assets",
                status="failed",
                drafts_json=row.review_drafts_json,
                error_detail=err,
            )
            db.commit()
            raise HTTPException(400, detail=err) from e
        gen = {
            "reply_excerpt": (res.get("reply") or "")[:8000],
            "asset_ids": res.get("asset_ids") or [],
            "preview_urls": res.get("preview_urls") or [],
        }
        new_drafts[i] = merge_generated_into_slot(slot, gen)
    row.review_drafts_json = new_drafts
    row.review_confirmed = False
    row.updated_at = datetime.utcnow()
    append_review_snapshot(
        db,
        user_id=current_user.id,
        account_id=account_id,
        kind="assets",
        status="ok",
        drafts_json=new_drafts,
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post(
    "/api/accounts/{account_id}/creator-schedule/review-regenerate-slot",
    summary="审核模式：重新生成某一版草稿",
)
async def post_review_regenerate_slot(
    request: Request,
    account_id: int,
    body: ReviewRegenerateSlotBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode != "review":
        raise HTTPException(400, detail="请先将发布模式设为「审核后发布」")
    drafts = row.review_drafts_json
    if not isinstance(drafts, list) or body.slot_index < 0 or body.slot_index >= len(drafts):
        raise HTTPException(400, detail="草稿序号无效，请先生成审核稿")
    from ..services.schedule_review_draft_generate import generate_review_drafts_via_chat

    hint = f"请只生成 1 条新草稿，用于替换列表中第 {body.slot_index + 1} 条；风格与用户需求一致，且与同批其他稿区分度适中。"
    ut, xi = forward_chat_auth_from_request(request)
    try:
        one = await generate_review_drafts_via_chat(
            user_id=current_user.id,
            platform=str(acct.platform or ""),
            nickname=str(acct.nickname or ""),
            schedule_kind=(getattr(row, "schedule_kind", None) or "image").strip().lower(),
            requirements_text=(row.requirements_text or "").strip(),
            variant_count=1,
            replace_slot_hint=hint,
            video_source_asset_id=getattr(row, "video_source_asset_id", None),
            user_bearer_token=ut,
            x_installation_id=xi or None,
        )
    except ValueError as e:
        err = str(e)
        append_review_snapshot(
            db,
            user_id=current_user.id,
            account_id=account_id,
            kind="slot_regen",
            status="failed",
            drafts_json=row.review_drafts_json,
            error_detail=err,
        )
        db.commit()
        raise HTTPException(400, detail=err) from e
    new_list = list(drafts)
    new_list[body.slot_index] = one[0]
    row.review_drafts_json = new_list
    row.review_confirmed = False
    row.updated_at = datetime.utcnow()
    append_review_snapshot(
        db,
        user_id=current_user.id,
        account_id=account_id,
        kind="slot_regen",
        status="ok",
        drafts_json=new_list,
    )
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get(
    "/api/accounts/{account_id}/creator-schedule/review-snapshots",
    summary="审核模式：草稿与生成历史快照列表",
)
def list_review_snapshots(
    account_id: int,
    limit: int = Query(50, ge=1, le=100),
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    rows = (
        db.query(CreatorScheduleReviewSnapshot)
        .filter(
            CreatorScheduleReviewSnapshot.user_id == current_user.id,
            CreatorScheduleReviewSnapshot.account_id == account_id,
        )
        .order_by(CreatorScheduleReviewSnapshot.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"snapshots": [snapshot_to_list_item(r) for r in rows]}


@router.get(
    "/api/accounts/{account_id}/creator-schedule/review-snapshots/{snapshot_id}",
    summary="审核模式：单条快照详情（含完整 drafts_json）",
)
def get_review_snapshot(
    account_id: int,
    snapshot_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    snap = (
        db.query(CreatorScheduleReviewSnapshot)
        .filter(
            CreatorScheduleReviewSnapshot.id == snapshot_id,
            CreatorScheduleReviewSnapshot.user_id == current_user.id,
            CreatorScheduleReviewSnapshot.account_id == account_id,
        )
        .first()
    )
    if not snap:
        raise HTTPException(404, detail="快照不存在")
    out: Dict[str, Any] = dict(snapshot_to_list_item(snap))
    out["drafts_json"] = snap.drafts_json
    return {"snapshot": out}


@router.post(
    "/api/accounts/{account_id}/creator-schedule/review-snapshots/{snapshot_id}/restore",
    summary="审核模式：将快照恢复为当前草稿（可继续编辑或再生成）",
)
def restore_review_snapshot(
    account_id: int,
    snapshot_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    snap = (
        db.query(CreatorScheduleReviewSnapshot)
        .filter(
            CreatorScheduleReviewSnapshot.id == snapshot_id,
            CreatorScheduleReviewSnapshot.user_id == current_user.id,
            CreatorScheduleReviewSnapshot.account_id == account_id,
        )
        .first()
    )
    if not snap:
        raise HTTPException(404, detail="快照不存在")
    dj = snap.drafts_json
    if not isinstance(dj, list) or not dj:
        raise HTTPException(400, detail="快照中无有效草稿数据")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode != "review":
        raise HTTPException(400, detail="请先将发布模式设为「审核后发布」")
    row.review_drafts_json = dj
    row.review_confirmed = False
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.post("/api/accounts/{account_id}/creator-schedule/review-confirm", summary="审核模式：确认排期，按首条时间与间隔逐条定时编排发布")
async def post_review_confirm(
    account_id: int,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    acct = (
        db.query(PublishAccount)
        .filter(
            PublishAccount.id == account_id,
            PublishAccount.user_id == current_user.id,
        )
        .first()
    )
    if not acct:
        raise HTTPException(404, detail="账号不存在")
    row = _get_or_create_schedule(db, current_user.id, account_id)
    mode = (getattr(row, "schedule_publish_mode", None) or "immediate").strip().lower()
    if mode != "review":
        raise HTTPException(400, detail="当前不是审核后发布模式")
    drafts = row.review_drafts_json
    if not isinstance(drafts, list) or len(drafts) < 1:
        raise HTTPException(400, detail="请先完成审核稿生成")
    if not row.enabled:
        raise HTTPException(400, detail="请先启用定时任务（完整配置中勾选启用并保存）后再确认发布")
    if not (row.requirements_text or "").strip():
        raise HTTPException(400, detail="请先在「完整配置」中填写目标与要求")
    from ..services.creator_schedule_task_log import cancel_running_task_logs_for_account
    from ..services.schedule_review_timing import compute_next_review_run_at_naive

    row.review_confirm_generation = int(getattr(row, "review_confirm_generation", 0) or 0) + 1
    row.review_confirmed = True
    row.review_selected_slot = 0
    row.updated_at = datetime.utcnow()
    cancel_running_task_logs_for_account(
        db,
        user_id=current_user.id,
        account_id=account_id,
        reason="新的确认发布已覆盖未结束的编排任务",
        commit=False,
    )
    now_u = datetime.utcnow()
    row.next_run_at = compute_next_review_run_at_naive(row, now_u)
    db.commit()
    db.refresh(row)
    logger.info(
        "review-confirm account_id=%s review_confirmed=True next_run_at=%s enabled=%s",
        account_id,
        row.next_run_at,
        bool(getattr(row, "enabled", False)),
    )
    from ..services.creator_schedule_runner import run_creator_schedule_tick_immediate

    asyncio.create_task(run_creator_schedule_tick_immediate(account_id))
    return _to_out(row)

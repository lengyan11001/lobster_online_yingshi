from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from ..core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _brands_path() -> Path:
    # backend/app/api/branding.py -> 仓库根（与 create_app 中 static_dir 一致）
    return Path(__file__).resolve().parent.parent.parent.parent / "static" / "branding" / "brands.json"


def _load_registry() -> Dict[str, Any]:
    p = _brands_path()
    if not p.exists():
        raise HTTPException(status_code=500, detail=f"Branding registry missing: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Invalid branding JSON: {e}") from e
    if not isinstance(raw, dict) or "marks" not in raw or not isinstance(raw["marks"], dict):
        raise HTTPException(status_code=500, detail="Branding registry invalid structure")
    return raw


@router.get("/api/branding", summary="当前品牌标记下的文案与图标路径（供前端与安装脚本同源配置）")
def get_branding() -> Dict[str, Any]:
    registry = _load_registry()
    mark = (getattr(settings, "lobster_brand_mark", None) or "").strip().lower()
    if not mark:
        raise HTTPException(status_code=500, detail="LOBSTER_BRAND_MARK is empty")
    marks = registry["marks"]
    if mark not in marks:
        raise HTTPException(status_code=400, detail=f"Unknown brand mark: {mark}")
    cfg = marks[mark]
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=500, detail=f"Invalid brand config for mark: {mark}")
    out: Dict[str, Any] = {"mark": mark, **cfg}
    parent = (getattr(settings, "lobster_parent_account", None) or "").strip()
    if parent:
        out["parent_account"] = parent
    return out

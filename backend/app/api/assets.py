"""Asset management: download, store, list, search local media files. 支持 TOS 上传后得到公网 URL 供速推拉取。"""
import asyncio
import hmac
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from .auth import get_current_user_for_local, _ServerUser
from ..core.config import get_settings, settings
from ..db import SessionLocal, get_db
from ..models import Asset

logger = logging.getLogger(__name__)
router = APIRouter()

# save-url 拉取公网 CDN（如 cdn-video.51sux.com）时禁用系统代理：本机 HTTPS_PROXY 走 CONNECT 时常超时/失败（mcp.log 曾出现「下载失败:」空异常）
_SAVE_URL_DOWNLOADER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ASSETS_DIR = _BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"

# 带签名的临时访问：用于会话里上传的图/视频生成可被速推拉取的 URL
_ASSET_FILE_EXPIRY_SEC = 600  # 10 分钟
# 发布管理「素材库」列表缩略图：优先本机签名链，加载快
_ASSET_LIST_PREVIEW_EXPIRY_SEC = 3600  # 1 小时
# 无公网 source_url 时，点击「预览」回退用本机签名链（尽量长）
_ASSET_LIST_OPEN_FALLBACK_EXPIRY_SEC = 86400  # 24 小时


def _asset_file_token(asset_id: str, expiry_ts: int) -> str:
    raw = f"{asset_id}:{expiry_ts}"
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _get_tos_config() -> Optional[dict]:
    """从 custom_configs.json 读取 TOS_CONFIG，用于上传到火山 TOS 并得到公网 URL。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        logger.info("[TOS] 配置文件不存在: %s", _CUSTOM_CONFIGS_FILE)
        return None
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("TOS_CONFIG")
        if not cfg:
            logger.info("[TOS] 配置文件中未找到 TOS_CONFIG")
            return None
        if not isinstance(cfg, dict):
            logger.warning("[TOS] TOS_CONFIG 不是字典类型")
            return None
        has_access_key = bool(cfg.get("access_key"))
        has_secret_key = bool(cfg.get("secret_key"))
        if not has_access_key or not has_secret_key:
            logger.warning("[TOS] TOS_CONFIG 缺少必要字段: access_key=%s secret_key=%s", has_access_key, has_secret_key)
            return None
        endpoint = cfg.get("endpoint", "")
        region = cfg.get("region", "")
        bucket = cfg.get("bucket_name", "")
        logger.debug("[TOS] 成功读取 TOS_CONFIG: endpoint=%s region=%s bucket=%s", endpoint, region, bucket)
        return cfg
    except Exception as e:
        logger.warning("[TOS] 读取配置文件失败: %s", e)
        return None


def _upload_to_tos(data: bytes, object_key: str, content_type: str) -> Optional[str]:
    """上传字节到火山 TOS，返回公网可访问 URL；失败返回 None。"""
    logger.info("[TOS-步骤2.1] 开始检查 TOS 配置 object_key=%s size=%d content_type=%s", object_key, len(data), content_type)
    cfg = _get_tos_config()
    if not cfg:
        logger.warning("[TOS-步骤2.1] 未配置 TOS_CONFIG，跳过上传（请在 custom_configs.json 中配置 TOS_CONFIG）")
        return None
    
    logger.info("[TOS-步骤2.2] TOS 配置存在，开始验证配置 object_key=%s", object_key)
    try:
        import tos
        ak = str(cfg.get("access_key", "")).strip()
        sk = str(cfg.get("secret_key", "")).strip()
        endpoint = str(cfg.get("endpoint", "")).strip()
        region = str(cfg.get("region", "")).strip()
        bucket = str(cfg.get("bucket_name", "")).strip()
        public_domain = str(cfg.get("public_domain", "")).strip().rstrip("/")
        missing_fields = []
        if not ak:
            missing_fields.append("access_key")
        if not sk:
            missing_fields.append("secret_key")
        if not endpoint:
            missing_fields.append("endpoint")
        if not region:
            missing_fields.append("region")
        if not bucket:
            missing_fields.append("bucket_name")
        if not public_domain:
            missing_fields.append("public_domain")
        if missing_fields:
            logger.error("[TOS-步骤2.2] 配置不完整，缺少字段: %s，跳过上传 object_key=%s", ", ".join(missing_fields), object_key)
            return None
        
        logger.info("[TOS-步骤2.3] 配置完整，创建 TOS 客户端 object_key=%s endpoint=%s region=%s bucket=%s", object_key, endpoint, region, bucket)
        client = tos.TosClientV2(ak, sk, endpoint, region)
        logger.info("[TOS-步骤2.4] 开始上传到 TOS object_key=%s bucket=%s size=%d", object_key, bucket, len(data))
        client.put_object(bucket, object_key, content=data)
        url = f"{public_domain}/{object_key}"
        logger.info("[TOS-步骤2.5] TOS 上传成功 object_key=%s url=%s", object_key, url[:80])
        return url
    except Exception as e:
        logger.error("[TOS-步骤2.4] TOS 上传失败 object_key=%s error=%s", object_key, str(e), exc_info=True)
        return None


def _transfer_payload_type(media_type: str) -> str:
    """与 capability_catalog 中 sutui.transfer_url.arg_schema 一致：仅 image / audio。"""
    mt = (media_type or "").lower()
    if mt == "audio":
        return "audio"
    return "image"


def _extract_url_from_transfer_result(obj: Any) -> Optional[str]:
    """从速推/MCP 返回的 result 结构中递归取出 http(s) URL。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http://") or s.startswith("https://"):
            return s
        try:
            return _extract_url_from_transfer_result(json.loads(s))
        except Exception:
            return None
    if isinstance(obj, dict):
        for k in ("url", "cdn_url", "transfer_url", "public_url", "output_url", "image_url"):
            v = obj.get(k)
            if isinstance(v, str) and (v.startswith("http://") or v.startswith("https://")):
                return v
        data = obj.get("data")
        if isinstance(data, dict):
            u = data.get("url") or data.get("cdn_url")
            if isinstance(u, str) and (u.startswith("http://") or u.startswith("https://")):
                return u
        for v in obj.values():
            u = _extract_url_from_transfer_result(v)
            if u:
                return u
    if isinstance(obj, list):
        for x in obj:
            u = _extract_url_from_transfer_result(x)
            if u:
                return u
    return None


def _deep_extract_url_from_transfer_nested(obj: Any) -> Optional[str]:
    """invoke_capability 上游可能返回嵌套 JSON-RPC（result 内再包 content[].text）。"""
    u = _extract_url_from_transfer_result(obj)
    if u:
        return u
    if not isinstance(obj, dict):
        return None
    for item in obj.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        t = item.get("text") or ""
        try:
            parsed = json.loads(t)
        except Exception:
            continue
        u = _deep_extract_url_from_transfer_nested(parsed)
        if u:
            return u
    nested = obj.get("result")
    if isinstance(nested, dict) and nested is not obj:
        u = _deep_extract_url_from_transfer_nested(nested)
        if u:
            return u
    return None


def _extract_cdn_from_mcp_invoke_result(rpc_result: dict) -> Optional[str]:
    """解析 MCP tools/call 的 result（invoke_capability），读取 content[].text JSON 中的 result。"""
    if rpc_result.get("isError"):
        return None
    for item in rpc_result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text") or ""
        try:
            outer = json.loads(text)
        except Exception:
            continue
        if not isinstance(outer, dict):
            continue
        inner = outer.get("result")
        u = _deep_extract_url_from_transfer_nested(inner)
        if u:
            return u
    return None


_transfer_url_cache: dict = {}
_transfer_url_cache_max = 200
_transfer_url_inflight: dict = {}


async def _transfer_url_via_sutui(
    url: str,
    media_type: str = "image",
    user=None,
    request: Optional[Request] = None,
) -> Optional[str]:
    """经本机 MCP 的 invoke_capability(capability_id=sutui.transfer_url) 转存；不可用裸工具名 sutui.transfer_url。"""
    import asyncio
    cached = _transfer_url_cache.get(url)
    if cached is not None:
        logger.debug("[转存] cache hit: %s", url[:80])
        return cached if cached else None

    inflight = _transfer_url_inflight.get(url)
    if inflight is not None:
        try:
            return await asyncio.shield(inflight)
        except Exception:
            return None

    async def _do_transfer():
        return await _transfer_url_via_sutui_inner(url, media_type, user, request)

    task = asyncio.ensure_future(_do_transfer())
    _transfer_url_inflight[url] = task
    try:
        result = await task
    finally:
        _transfer_url_inflight.pop(url, None)
    if len(_transfer_url_cache) >= _transfer_url_cache_max:
        oldest = next(iter(_transfer_url_cache))
        _transfer_url_cache.pop(oldest, None)
    _transfer_url_cache[url] = result or ""
    return result


async def _transfer_url_via_sutui_inner(
    url: str,
    media_type: str = "image",
    user=None,
    request: Optional[Request] = None,
) -> Optional[str]:
    try:
        sutui_token = None
        if user:
            from .consumption_accounts import get_effective_sutui_token

            db_s = SessionLocal()
            try:
                sutui_token = get_effective_sutui_token(user, db_s)
            finally:
                db_s.close()
        if not sutui_token:
            try:
                from mcp.http_server import _load_sutui_token
                sutui_token = _load_sutui_token()
            except Exception:
                pass
        if not sutui_token:
            logger.debug("[转存] 无速推 Token，跳过 sutui.transfer_url")
            return None

        hdrs: dict = {"Content-Type": "application/json"}
        if request is not None:
            auth = (request.headers.get("Authorization") or "").strip()
            if auth:
                hdrs["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
            xi = (request.headers.get("X-Installation-Id") or request.headers.get("x-installation-id") or "").strip()
            if xi:
                hdrs["X-Installation-Id"] = xi
        hdrs["X-Sutui-Token"] = sutui_token

        tp = _transfer_payload_type(media_type)
        MCP_URL = "http://127.0.0.1:8001/mcp"
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.post(
                MCP_URL,
                json={
                    "jsonrpc": "2.0",
                    "id": "transfer",
                    "method": "tools/call",
                    "params": {
                        "name": "invoke_capability",
                        "arguments": {
                            "capability_id": "sutui.transfer_url",
                            "payload": {"url": url, "type": tp},
                        },
                    },
                },
                headers=hdrs,
            )
        if resp.status_code >= 400:
            logger.warning("[转存] MCP invoke sutui.transfer_url HTTP %s: %s", resp.status_code, (resp.text or "")[:400])
            return None
        body = resp.json()
        if body.get("error"):
            logger.warning("[转存] MCP JSON-RPC 错误: %s", body.get("error"))
            return None
        rpc_result = body.get("result") or {}
        cdn_url = _extract_cdn_from_mcp_invoke_result(rpc_result if isinstance(rpc_result, dict) else {})
        if cdn_url:
            logger.info("[转存] sutui.transfer_url 成功: %s -> %s", url[:80], cdn_url[:80])
            return cdn_url
        logger.warning(
            "[转存] invoke_capability sutui.transfer_url 未解析到 CDN URL，响应前缀: %s",
            json.dumps(rpc_result, ensure_ascii=False)[:400],
        )
        return None
    except Exception as e:
        logger.warning("[转存] sutui.transfer_url 调用失败: %s: %s", type(e).__name__, e)
        return None


def _is_internal_asset_http_url(url: str) -> bool:
    """不可直接作为附图/上游拉取的地址：本机、内网、认证域、带签名的 /api/assets/file 等。"""
    u = (url or "").strip()
    if not u.startswith("http://") and not u.startswith("https://"):
        return True
    from urllib.parse import urlparse
    import ipaddress

    try:
        parsed = urlparse(u)
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return True
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
            return True
        if "42.194.209.150" in hostname or "bhzn.top" in hostname:
            return True
        if "token=" in u or "?token" in u:
            return True
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback:
                return True
        except ValueError:
            cdn_keywords = (
                "cdn.",
                "oss.",
                "cos.",
                "tos.",
                "volces.com",
                "s3.",
                "cloudfront.",
                "fastly.",
                "cloudflare.",
                "img.",
                "static.",
                "media.",
                "assets.",
                "qiniucdn.",
                "upyun.",
                "aliyuncs.",
                "cdn-video.51sux.com",
            )
            if any(cdn_keyword in hostname for cdn_keyword in cdn_keywords):
                return False
            if "token=" in u or "?token" in u:
                return True
        return False
    except Exception:
        if "42.194.209.150" in u or "bhzn.top" in u or "token=" in u or "?token" in u:
            return True
        return False


def _skip_tos_mirror_for_downloaded_url(effective_url: str) -> bool:
    """已成功从该 URL 下载时：若为公网可直接给上游用的链，则不再二次上传 TOS。"""
    return not _is_internal_asset_http_url(effective_url)


def _content_type_for_asset_filename(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(ext, "application/octet-stream")


def get_asset_public_url(
    asset_id: str, user_id: int, request: Request, db: Session
) -> Optional[str]:
    """【使用素材-步骤A】获取素材公网 URL：source_url 为非内部 http(s) 时直接返回（含速推 CDN），否则返回 None。"""
    logger.info("[使用素材-步骤A.1] 查询素材 asset_id=%s user_id=%s", asset_id, user_id)
    row = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == user_id).first()
    if row and getattr(row, "source_url", None):
        url = (row.source_url or "").strip()
        logger.info("[使用素材-步骤A.2] 找到素材 source_url=%s asset_id=%s", url[:100] if url else "None", asset_id)
        if url.startswith("http://") or url.startswith("https://"):
            if _is_internal_asset_http_url(url):
                logger.warning(
                    "[使用素材-步骤A.3] 内部或签名地址，返回 None asset_id=%s url=%s",
                    asset_id,
                    url[:100],
                )
                return None
            logger.info("[使用素材-步骤A.4] 返回公网 URL asset_id=%s url=%s", asset_id, url[:80])
            return url
    logger.warning("[使用素材-步骤A.2] 素材不存在或 source_url 为 None asset_id=%s user_id=%s", asset_id, user_id)
    return None


def _is_loopback_base(base: str) -> bool:
    if not (base or "").strip():
        return True
    b = (base or "").lower()
    return "127.0.0.1" in b or "localhost" in b or "0.0.0.0" in b


def _resolve_asset_public_base(request: Request) -> str:
    """生成 /api/assets/file 签名链的根。

    策略：跟随请求的 Host header（浏览器从哪个地址访问就用哪个），
    这样 IP 变化后不会失效。仅在需要被外部系统拉取时才用 PUBLIC_BASE_URL。
    """
    from ..core.config import get_settings

    settings = get_settings()
    port = getattr(settings, "port", 8000)
    pub = (getattr(settings, "public_base_url", None) or "").strip().rstrip("/")

    host_header = ""
    try:
        host_header = (request.headers.get("host") or "").strip()
    except Exception:
        pass

    if host_header:
        sch = "http"
        try:
            sch = getattr(request.url, "scheme", None) or "http"
        except Exception:
            pass
        base = f"{sch}://{host_header}"
    else:
        base = f"http://127.0.0.1:{port}"

    if "0.0.0.0" in base:
        base = base.replace("0.0.0.0", "127.0.0.1")

    if _is_loopback_base(base) and pub and not _is_loopback_base(pub):
        base = pub

    try:
        base.encode("ascii")
    except UnicodeEncodeError:
        base = f"http://127.0.0.1:{port}"
        logger.warning(
            "[素材] base_url 含非 ASCII，已回退为 127.0.0.1。请在 .env 设置 PUBLIC_BASE_URL（如 http://本机局域网IP:8000）。"
        )
    return base


def build_asset_file_url(
    request: Request,
    asset_id: str,
    *,
    expiry_sec: Optional[int] = None,
) -> Optional[str]:
    """生成带签名的素材文件访问 URL，供注入到对话消息中（速推可拉取）。保证返回纯 ASCII，避免编码问题。"""
    sec = expiry_sec if expiry_sec is not None else _ASSET_FILE_EXPIRY_SEC
    expiry_ts = int(time.time()) + sec
    token = _asset_file_token(asset_id, expiry_ts)
    base = _resolve_asset_public_base(request)
    return f"{base}/api/assets/file/{asset_id}?token={token}&expiry={expiry_ts}"


def _gen_asset_id() -> str:
    return uuid.uuid4().hex[:12]


def _asset_local_path(asset: Asset) -> Optional[Path]:
    """Resolve asset local file path from DB record."""
    if not asset or not getattr(asset, "filename", None):
        return None
    path = ASSETS_DIR / asset.filename
    return path if path.exists() else None


def _save_bytes(data: bytes, ext: str) -> tuple[str, str, int]:
    """Save raw bytes, return (asset_id, filename, size)."""
    aid = _gen_asset_id()
    fname = f"{aid}{ext}"
    path = ASSETS_DIR / fname
    path.write_bytes(data)
    return aid, fname, len(data)


def _save_bytes_or_tos(data: bytes, ext: str, content_type: str) -> tuple[str, str, int, Optional[str]]:
    """本地落盘后尝试火山 TOS，供 media.edit 等写入新素材。返回 (asset_id, filename, size, tos_url|None)。"""
    aid, fname, fsize = _save_bytes(data, ext)
    ct = (content_type or "").strip() or _content_type_for_asset_filename(fname)
    tos_url = _upload_to_tos(data, f"assets/{fname}", ct)
    return aid, fname, fsize, tos_url


# 图生视频/ fal 等上游限制：单边最大 7680，上传时超限则等比缩小
_MAX_IMAGE_DIMENSION = 7680


def _resize_image_if_needed(
    data: bytes, ext: str, content_type: str, max_dimension: int = _MAX_IMAGE_DIMENSION
) -> tuple[bytes, str]:
    """若为图片且宽高任一超过 max_dimension，则等比缩小后返回新字节与 content_type；否则原样返回。"""
    ext_lower = (ext or "").lower()
    if ext_lower not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return data, content_type
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w <= max_dimension and h <= max_dimension:
            return data, content_type
        scale = min(max_dimension / w, max_dimension / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        logger.info("[素材] 图片超限 %dx%d 等比缩小至 %dx%d（上限 %d）", w, h, nw, nh, max_dimension)
        img = img.resize((nw, nh), Image.Resampling.LANCZOS)
        out = BytesIO()
        if ext_lower in (".jpg", ".jpeg"):
            img = img.convert("RGB")
            img.save(out, "JPEG", quality=92)
            return out.getvalue(), "image/jpeg"
        if ext_lower == ".webp":
            img.save(out, "WEBP", quality=90)
            return out.getvalue(), "image/webp"
        img.save(out, "PNG")
        return out.getvalue(), "image/png"
    except Exception as e:
        logger.warning("[素材] 图片缩放跳过（将使用原图）: %s", e)
        return data, content_type


# ── Download from URL ─────────────────────────────────────────────

class SaveAssetReq(BaseModel):
    url: str
    media_type: str = "image"
    name: Optional[str] = None
    tags: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    # MCP sutui.transfer_url 自动入库：下载用 url（mcp 输出链），去重用转入链（通常为 v3），避免每次 transfer 换新 uuid 重复入库
    dedupe_hint_url: Optional[str] = None
    # 速推异步任务 id：与「文件级」base 去重键组合，同一 task 内同一输出只存一行；同一 task 可有多个不同 URL（多文件）
    generation_task_id: Optional[str] = None


def _save_url_prompt_is_placeholder(s: Optional[str]) -> bool:
    """空、或明显是 MCP 能力名误写入的 prompt，允许用本次请求里的正文覆盖。"""
    t = (s or "").strip()
    if not t:
        return True
    if t == "task.get_result":
        return True
    if (t.startswith("sutui.") or t.startswith("invoke_capability")) and " " not in t and len(t) < 96:
        return True
    return False


def _maybe_backfill_prompt_model_on_dedupe(
    existing: Asset, body: SaveAssetReq, db: Session
) -> None:
    """去重命中旧行时：若本次 save-url 带了生成 prompt/model，而库内为空或为占位，则补写。"""
    new_p = (body.prompt or "").strip()
    new_m = (body.model or "").strip()
    changed = False
    if new_p and _save_url_prompt_is_placeholder(getattr(existing, "prompt", None)):
        existing.prompt = new_p[:500]
        changed = True
    if new_m and not (getattr(existing, "model", None) or "").strip():
        existing.model = new_m[:128]
        changed = True
    if changed:
        db.add(existing)
        db.commit()


def _autosave_tags_require_tos(tags: Optional[str]) -> bool:
    """MCP 自动入库使用 tags=auto,<capability_id>：下载源已是公网链时可不写 TOS，否则仍要求 TOS 成功。"""
    return (tags or "").strip().startswith("auto,")


def _unlink_safe_asset_file(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


async def _resolve_v3_tasks_url_for_download(
    url: str,
    media_type: str,
    current_user: _ServerUser,
    request: Optional[Request] = None,
) -> str:
    """速推结果里常见的 cdn-video…/v3-tasks/… 直链易在本机拉取失败；优先经 sutui.transfer_url 换成稳定链再下载。"""
    u = (url or "").strip()
    if not u or "v3-tasks" not in u.lower():
        return u
    alt = await _transfer_url_via_sutui(u, media_type, current_user, request=request)
    if alt:
        logger.info("[素材] save-url v3-tasks 已换稳定链: %s -> %s", u[:80], alt[:80])
        return alt
    logger.warning("[素材] save-url v3-tasks 未拿到 transfer 结果，将直拉原链")
    return u


async def _download_bytes_from_url_with_retries(url: str) -> tuple[bytes, httpx.Response]:
    """默认 trust_env=False（避免部分环境 HTTPS_PROXY 干扰 CDN）；若连接类错误仍失败，再沿用系统代理重试。

    兼顾客厅两类环境：无主代理时常因 trust_env=True 拉 cdn-video 异常；仅能通过公司代理出网时则需 trust_env=True。
    """

    async def _attempts(trust_env: bool) -> tuple[bytes, httpx.Response]:
        last_exc: Optional[BaseException] = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=120.0,
                    follow_redirects=True,
                    trust_env=trust_env,
                ) as c:
                    resp = await c.get(url, headers=_SAVE_URL_DOWNLOADER_HEADERS)
                    resp.raise_for_status()
                    return resp.content, resp
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    last_exc = e
                    continue
                snip = (e.response.text or "")[:300]
                raise HTTPException(
                    status_code=400,
                    detail=f"下载失败: HTTP {code} {snip!r}",
                ) from e
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    last_exc = e
                    continue
                raise HTTPException(
                    status_code=400,
                    detail=f"下载失败: {type(e).__name__}: {e!s}",
                ) from e
        if last_exc:
            raise HTTPException(
                status_code=400,
                detail=f"下载失败: {type(last_exc).__name__}: {last_exc!s}",
            ) from last_exc
        raise HTTPException(status_code=400, detail="下载失败: 未知错误")

    try:
        return await _attempts(False)
    except HTTPException as e0:
        det = (str(e0.detail or "")).lower()
        if any(
            s in det
            for s in (
                "connect",
                "timeout",
                "timed out",
                "remoteprotocol",
                "name or service",
                "getaddrinfo",
            )
        ):
            logger.warning("[素材] save-url 直连 CDN 失败，尝试使用系统代理(HTTPS_PROXY)再拉取: %s", url[:100])
            return await _attempts(True)
        raise


def _save_url_dedupe_key(url: str) -> str:
    """对单条 URL 规范化后做 SHA256（去 query/fragment、小写）。供非 v3 链或兜底比较使用。"""
    return hashlib.sha256(
        (url or "").strip().split("?")[0].split("#")[0].lower().encode("utf-8")
    ).hexdigest()


def _url_snip_for_log(u: Optional[str], max_len: int = 120) -> str:
    """日志里截断 URL，避免单行过长；便于 grep「save-url 诊断」。"""
    s = (u or "").strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _dedupe_key_for_save_url_request(body_url: str, effective_resolved: str) -> str:
    """save-url 入库去重 key：请求里若是 v3-tasks，**只按原始 v3 链** 算 key。

    原因：`sutui.transfer_url` 往往每次返回不同的 mcp-images 路径；若按 transfer 结果算 dk，
    同一 v3 直链多次保存会重复入库多行。
    非 v3 请求仍按稳定下载链（effective_resolved）算 key，与路由里先 resolve 再锁一致。
    """
    bu = (body_url or "").strip()
    if "v3-tasks" in bu.lower():
        return _save_url_dedupe_key(bu)
    return _save_url_dedupe_key(effective_resolved)


def _compute_save_url_dedupe_key(
    body_url: str,
    effective_resolved: str,
    dedupe_hint_url: Optional[str],
) -> str:
    """优先用 dedupe_hint_url（与 body.url 可不同：先转存再入库时 hint=转入链、url=转出链）。"""
    hint = (dedupe_hint_url or "").strip()
    if hint:
        return _dedupe_key_for_save_url_request(hint, hint)
    return _dedupe_key_for_save_url_request(body_url, effective_resolved)


def _final_save_url_dedupe_key(
    base_dk: str,
    generation_task_id: Optional[str],
    *,
    dedupe_hint_url: Optional[str] = None,
    body_url: Optional[str] = None,
) -> str:
    """若带 generation_task_id，则在「文件指纹 base_dk」外再套一层。

    当 dedupe_hint_url **或请求体 url** 为 v3-tasks 时，base_dk 已按稳定 v3 链对齐，**不再**叠加 task_id。
    否则 MCP auto_save（task.get_result 带 generation_task_id）与其它入口（仅 v3 body、无 task_id）
    会对同一资源算出不同 dk，锁与 meta 去重均失效，导致重复入库。
    """
    hint = (dedupe_hint_url or "").strip()
    if hint and "v3-tasks" in hint.lower():
        return base_dk
    bu = (body_url or "").strip()
    if bu and "v3-tasks" in bu.lower():
        return base_dk
    tid = (generation_task_id or "").strip()
    if not tid:
        return base_dk
    return hashlib.sha256(f"{tid}\n{base_dk}".encode("utf-8")).hexdigest()


def _find_existing_asset_by_save_url_dedupe(db: Session, user_id: int, dedupe_key: str) -> Optional[Asset]:
    """按 meta.save_url_dedupe 全库命中；勿仅用最近 N 条扫（素材多时同一 URL 会重复入库）。"""
    db_url = (settings.database_url or "").strip().lower()
    row_id: Optional[int] = None
    if "sqlite" in db_url:
        r = db.execute(
            text(
                "SELECT id FROM assets WHERE user_id = :uid "
                "AND json_extract(meta, '$.save_url_dedupe') = :dk LIMIT 1"
            ),
            {"uid": user_id, "dk": dedupe_key},
        ).fetchone()
        if r:
            row_id = int(r[0])
    elif "mysql" in db_url or "mariadb" in db_url:
        r = db.execute(
            text(
                "SELECT id FROM assets WHERE user_id = :uid AND "
                "JSON_UNQUOTE(JSON_EXTRACT(meta, '$.save_url_dedupe')) = :dk LIMIT 1"
            ),
            {"uid": user_id, "dk": dedupe_key},
        ).fetchone()
        if r:
            row_id = int(r[0])
    elif "postgresql" in db_url:
        r = db.execute(
            text(
                "SELECT id FROM assets WHERE user_id = :uid "
                "AND meta->>'save_url_dedupe' = :dk LIMIT 1"
            ),
            {"uid": user_id, "dk": dedupe_key},
        ).fetchone()
        if r:
            row_id = int(r[0])
    if row_id is not None:
        return db.query(Asset).filter(Asset.id == row_id).first()
    rows = (
        db.query(Asset)
        .filter(Asset.user_id == user_id)
        .order_by(Asset.id.desc())
        .limit(5000)
        .all()
    )
    for a in rows:
        if (a.meta or {}).get("save_url_dedupe") == dedupe_key:
            return a
        if a.source_url and _save_url_dedupe_key(a.source_url) == dedupe_key:
            return a
    return None


_save_url_user_dedupe_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _save_url_lock_for(user_id: int, dedupe_key: str) -> asyncio.Lock:
    """同用户同一规范化 URL 串行化，避免并发 save-url 在 commit 前均判定「无重复」而重复入库。"""
    k = (user_id, dedupe_key)
    if k not in _save_url_user_dedupe_locks:
        _save_url_user_dedupe_locks[k] = asyncio.Lock()
    return _save_url_user_dedupe_locks[k]


def _normalized_url_path(url: str) -> str:
    return (url or "").strip().split("?")[0].split("#")[0].lower()


def _find_existing_asset_by_normalized_source_url(
    db: Session, user_id: int, url: str
) -> Optional[Asset]:
    """同一稳定公链（如 mcp-images）已入库则不再插行：与仅按 v3 hint 存的 meta.dk 互为补集。"""
    nu = _normalized_url_path(url)
    if len(nu) < 40:
        return None
    if "mcp-images" not in nu and "/assets/" not in nu:
        return None
    rows = (
        db.query(Asset)
        .filter(Asset.user_id == user_id)
        .order_by(Asset.id.desc())
        .limit(4000)
        .all()
    )
    for a in rows:
        if _normalized_url_path(a.source_url or "") == nu:
            return a
    return None


def _find_existing_asset_by_cdn_assets_url(db: Session, user_id: int, url: str) -> Optional[Asset]:
    """CDN .../assets/{asset_id}.ext 与库 asset_id 一致时视为同一素材（与 v3-tasks 链互为别称）。"""
    u = (url or "").strip().split("?")[0].split("#")[0]
    m = re.search(r"/assets/([a-f0-9]{8,24})\.(?:png|jpe?g|webp|gif|mp4|webm|mov)$", u, re.IGNORECASE)
    if not m:
        return None
    aid = m.group(1).lower()
    return db.query(Asset).filter(Asset.user_id == user_id, Asset.asset_id == aid).first()


async def _save_asset_from_url_locked(
    dk: str,
    body: SaveAssetReq,
    request: Request,
    current_user: _ServerUser,
    *,
    effective_url_resolved: str,
) -> dict:
    # 阶段1：仅短事务去重/补写，避免与下方 await 下载/MCP 共占一条池连接
    db = SessionLocal()
    try:
        existing = _find_existing_asset_by_save_url_dedupe(db, current_user.id, dk)
        if existing:
            logger.info(
                "[save-url 诊断] user_id=%s outcome=dedupe_meta asset_id=%s dk=%s body_prefix=%s effective_prefix=%s "
                "hint_prefix=%s tags=%s",
                current_user.id,
                existing.asset_id,
                dk[:16],
                _url_snip_for_log(body.url),
                _url_snip_for_log(effective_url_resolved),
                _url_snip_for_log(body.dedupe_hint_url)
                if (body.dedupe_hint_url or "").strip()
                else "-",
                (body.tags or "")[:64],
            )
            logger.info("[素材] save-url 去重 命中已有 asset_id=%s", existing.asset_id)
            _maybe_backfill_prompt_model_on_dedupe(existing, body, db)
            return {
                "asset_id": existing.asset_id,
                "filename": existing.filename,
                "media_type": existing.media_type,
                "file_size": existing.file_size or 0,
                "source_url": existing.source_url or "",
            }
        src_hit = _find_existing_asset_by_normalized_source_url(db, current_user.id, body.url)
        if src_hit:
            logger.info(
                "[save-url 诊断] user_id=%s outcome=dedupe_source_url asset_id=%s body_prefix=%s tags=%s",
                current_user.id,
                src_hit.asset_id,
                _url_snip_for_log(body.url),
                (body.tags or "")[:64],
            )
            logger.info("[素材] save-url 去重 命中同 source_url asset_id=%s", src_hit.asset_id)
            _maybe_backfill_prompt_model_on_dedupe(src_hit, body, db)
            return {
                "asset_id": src_hit.asset_id,
                "filename": src_hit.filename,
                "media_type": src_hit.media_type,
                "file_size": src_hit.file_size or 0,
                "source_url": src_hit.source_url or "",
            }
        cdn_hit = _find_existing_asset_by_cdn_assets_url(db, current_user.id, body.url)
        if cdn_hit:
            logger.info(
                "[save-url 诊断] user_id=%s outcome=dedupe_cdn_path asset_id=%s dk=%s body_prefix=%s hint_prefix=%s tags=%s",
                current_user.id,
                cdn_hit.asset_id,
                dk[:16],
                _url_snip_for_log(body.url),
                _url_snip_for_log(body.dedupe_hint_url)
                if (body.dedupe_hint_url or "").strip()
                else "-",
                (body.tags or "")[:64],
            )
            logger.info("[素材] save-url 去重 命中 CDN /assets/ 路径 asset_id=%s", cdn_hit.asset_id)
            _maybe_backfill_prompt_model_on_dedupe(cdn_hit, body, db)
            return {
                "asset_id": cdn_hit.asset_id,
                "filename": cdn_hit.filename,
                "media_type": cdn_hit.media_type,
                "file_size": cdn_hit.file_size or 0,
                "source_url": cdn_hit.source_url or "",
            }
    finally:
        db.close()

    effective_url = (effective_url_resolved or "").strip()
    if not effective_url:
        raise HTTPException(status_code=400, detail="无效 URL")
    try:
        data, resp = await _download_bytes_from_url_with_retries(effective_url)
    except HTTPException:
        raise
    except Exception as e:
        es = str(e).strip()
        if not es:
            msg = f"{type(e).__name__}: {repr(e)}"
        else:
            msg = f"{type(e).__name__}: {es}"
        raise HTTPException(status_code=400, detail=f"下载失败: {msg}") from e

    url_path = effective_url.split("?")[0].split("#")[0]
    url_ext = Path(url_path).suffix.lower() if "." in url_path.split("/")[-1] else ""
    ct = (resp.headers.get("content-type") or "").strip()
    ct_lower = ct.lower()
    ext = url_ext or ".png"
    if not url_ext:
        if "jpeg" in ct_lower or "jpg" in ct_lower:
            ext = ".jpg"
        elif "webp" in ct_lower:
            ext = ".webp"
        elif "gif" in ct_lower:
            ext = ".gif"
        elif "mp4" in ct_lower or "video/mp4" in ct_lower:
            ext = ".mp4"
        elif "webm" in ct_lower:
            ext = ".webm"
        elif "mov" in ct_lower or "quicktime" in ct_lower:
            ext = ".mov"

    if body.media_type == "video" and ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        ext = ".mp4"
    # 禁止：曾用「请求写 image + 扩展名为 .mp4」时把扩展名强行改为 .png，导致 MP4 字节以 .png 入库且 media_type=image（Win/Mac 日志均见 v3-tasks 误判）

    # MP4/ISO 基媒体：无后缀或误判为图片时仍识别为视频
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ""):
            ext = ".mp4"

    req_mt = (body.media_type or "image").strip().lower()
    if req_mt not in ("image", "video", "audio"):
        req_mt = "image"
    effective_mt = req_mt
    if req_mt != "audio":
        if ext in (".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"):
            effective_mt = "video"
        elif ct_lower.startswith("video/"):
            effective_mt = "video"
        elif len(data) >= 12 and data[4:8] == b"ftyp" and req_mt == "image":
            effective_mt = "video"

    if effective_mt != req_mt:
        logger.info("[素材] save-url 按文件内容修正 media_type: %s -> %s ext=%s", req_mt, effective_mt, ext)

    if effective_mt == "image":
        data, ct = _resize_image_if_needed(data, ext, ct or "application/octet-stream")
        ct = ct or "application/octet-stream"
    else:
        ct = ct or "application/octet-stream"

    aid, fname, fsize = _save_bytes(data, ext)

    skip_tos_mirror = _skip_tos_mirror_for_downloaded_url(effective_url)
    tos_url: Optional[str] = None
    if not skip_tos_mirror:
        tos_url = _upload_to_tos(data, f"assets/{fname}", ct)

    if _autosave_tags_require_tos(body.tags):
        if skip_tos_mirror:
            source_url = effective_url
        elif _get_tos_config() is None:
            _unlink_safe_asset_file(ASSETS_DIR / fname)
            raise HTTPException(
                status_code=503,
                detail="对话生成素材入库需配置 TOS_CONFIG（custom_configs.json，含 access_key/secret_key/endpoint/region/bucket_name/public_domain），未配置无法保存可预览的公网地址。",
            )
        elif not tos_url:
            _unlink_safe_asset_file(ASSETS_DIR / fname)
            raise HTTPException(
                status_code=503,
                detail="对话生成素材已下载但火山 TOS 上传失败，无法入库。请检查 TOS 配置与网络后重试。",
            )
        else:
            source_url = tos_url
    elif skip_tos_mirror:
        source_url = effective_url
    else:
        source_url = tos_url if tos_url else effective_url
        if not tos_url and body.url:
            # 检测是否是内部地址
            from urllib.parse import urlparse
            import ipaddress
            try:
                parsed = urlparse(body.url)
                hostname = (parsed.hostname or "").lower()
                is_internal = (
                    not hostname or
                    hostname in ("localhost", "127.0.0.1", "0.0.0.0") or
                    "42.194.209.150" in hostname or "bhzn.top" in hostname or
                    (hostname and ("token=" in body.url or "?token" in body.url))
                )
                if not is_internal:
                    try:
                        ip = ipaddress.ip_address(hostname)
                        is_internal = ip.is_private or ip.is_loopback
                    except ValueError:
                        pass

                if is_internal:
                    # 尝试通过 sutui.transfer_url 转存
                    try:
                        transfer_url = await _transfer_url_via_sutui(
                            body.url, effective_mt, current_user, request=request
                        )
                        if transfer_url:
                            source_url = transfer_url
                    except Exception as e:
                        logger.debug("[素材] save-url 时 sutui.transfer_url 转存失败: %s", e)
            except Exception as e:
                logger.debug("[素材] save-url 时检测内部地址失败: %s", e)

    meta: dict = {"save_url_dedupe": dk}
    gtid = (body.generation_task_id or "").strip()
    if gtid:
        meta["generation_task_id"] = gtid[:128]

    log_url = body.url[:80] + ("..." if len(body.url) > 80 else "")
    if effective_url.strip() != (body.url or "").strip():
        log_url = log_url + " effective=" + (effective_url[:64] + "…" if len(effective_url) > 64 else effective_url)

    db_ins = SessionLocal()
    try:
        asset = Asset(
            asset_id=aid,
            user_id=current_user.id,
            filename=fname,
            media_type=effective_mt,
            file_size=fsize,
            source_url=source_url,
            prompt=body.prompt,
            model=body.model,
            tags=body.tags,
            meta=meta,
        )
        db_ins.add(asset)
        db_ins.commit()
        logger.info(
            "[save-url 诊断] user_id=%s outcome=new_row asset_id=%s dk=%s bytes=%s body_prefix=%s effective_prefix=%s "
            "hint_prefix=%s tags=%s",
            current_user.id,
            aid,
            dk[:16],
            fsize,
            _url_snip_for_log(body.url),
            _url_snip_for_log(effective_url),
            _url_snip_for_log(body.dedupe_hint_url)
            if (body.dedupe_hint_url or "").strip()
            else "-",
            (body.tags or "")[:64],
        )
        logger.info(
            "[素材] save-url 完成 url=%s asset_id=%s size=%s media_type=%s skip_tos_mirror=%s tos_upload=%s",
            log_url,
            aid,
            fsize,
            effective_mt,
            skip_tos_mirror,
            bool(tos_url),
        )
        return {
            "asset_id": aid,
            "filename": fname,
            "media_type": effective_mt,
            "file_size": fsize,
            "source_url": source_url,
        }
    finally:
        db_ins.close()


@router.post("/api/assets/save-url", summary="从 URL 保存素材")
async def save_asset_from_url(
    body: SaveAssetReq,
    request: Request,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    # 去重：v3-tasks 按原始链（transfer 每次可能换新路径）；其它按 resolve 后的有效下载 URL
    req_tag = (
        (request.headers.get("x-request-id") or request.headers.get("X-Request-ID") or "")
        .strip()[:48]
    )
    effective_for_dedupe = await _resolve_v3_tasks_url_for_download(
        body.url, body.media_type, current_user, request=request
    )
    base_dk = _compute_save_url_dedupe_key(
        body.url, effective_for_dedupe, body.dedupe_hint_url
    )
    dk = _final_save_url_dedupe_key(
        base_dk,
        body.generation_task_id,
        dedupe_hint_url=body.dedupe_hint_url,
        body_url=body.url,
    )
    is_v3_body = "v3-tasks" in (body.url or "").lower()
    has_hint = bool((body.dedupe_hint_url or "").strip())
    has_gen_tid = bool((body.generation_task_id or "").strip())
    dk_rule = (
        "dedupe_hint_url"
        if has_hint
        else ("v3_body" if is_v3_body else "body_effective")
    )
    if has_gen_tid:
        dk_rule = f"task_id+{dk_rule}"
    logger.info(
        "[save-url 诊断] enter user_id=%s req_id=%s media_type=%s dk_rule=%s is_v3_body=%s has_dedupe_hint=%s "
        "has_generation_task_id=%s generation_task_id=%s base_dk=%s dk_full=%s body_prefix=%s effective_prefix=%s "
        "hint_prefix=%s tags=%s",
        current_user.id,
        req_tag or "-",
        body.media_type,
        dk_rule,
        is_v3_body,
        has_hint,
        has_gen_tid,
        ((body.generation_task_id or "").strip()[:64] or "-"),
        base_dk,
        dk,
        _url_snip_for_log(body.url),
        _url_snip_for_log(effective_for_dedupe),
        _url_snip_for_log(body.dedupe_hint_url) if has_hint else "-",
        (body.tags or "")[:64],
    )
    async with _save_url_lock_for(current_user.id, dk):
        return await _save_asset_from_url_locked(
            dk, body, request, current_user, effective_url_resolved=effective_for_dedupe
        )


# ── Upload file ───────────────────────────────────────────────────

@router.post("/api/assets/upload", summary="上传素材文件")
async def upload_asset(
    request: Request,
    file: UploadFile = File(...),
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    """【步骤1】本地落盘 → 优先 TOS → 否则 upload-temp。两路均失败则删本地文件并 HTTP 503，不写库。"""
    logger.info("[上传流程-步骤1] 客户端收到上传请求 filename=%s user_id=%s", file.filename, current_user.id if current_user else "N/A")
    
    data = await file.read()
    if not data:
        logger.error("[上传流程-步骤1] 文件为空")
        raise HTTPException(400, detail="文件为空")

    name = file.filename or "upload"
    ext = Path(name).suffix or ".bin"
    mtype = "image"
    if ext.lower() in (".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv"):
        mtype = "video"
    elif ext.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"):
        mtype = "audio"

    ct = (file.content_type or "").strip() or "application/octet-stream"
    if mtype == "image":
        data, ct = _resize_image_if_needed(data, ext, ct)
    aid, fname, fsize = _save_bytes(data, ext)
    logger.info("[上传流程-步骤1] 文件已保存到本地 asset_id=%s filename=%s size=%d media_type=%s", aid, fname, fsize, mtype)

    auth_header = (request.headers.get("Authorization") or "").strip()
    bearer_token = None
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()
    had_bearer = bool(bearer_token)

    tos_cfg_present = _get_tos_config() is not None
    temp_http_status: Optional[int] = None
    temp_body_snip = ""
    temp_json_err = ""
    step3_network_err = ""

    # 【步骤2】优先使用 TOS 转存
    logger.info("[上传流程-步骤2] 开始尝试 TOS 上传 asset_id=%s filename=%s size=%d tos_config_present=%s", aid, fname, fsize, tos_cfg_present)
    tos_url = _upload_to_tos(data, f"assets/{fname}", ct)
    if tos_url:
        logger.info("[上传流程-步骤2] TOS 上传成功 asset_id=%s tos_url=%s", aid, tos_url[:80])
    else:
        logger.warning("[上传流程-步骤2] TOS 上传失败或未配置 asset_id=%s tos_config_present=%s", aid, tos_cfg_present)

    # 【步骤3】如果 TOS 失败，上传到服务器临时文件接口
    public_url = tos_url
    if not public_url:
        logger.info("[上传流程-步骤3] TOS 未成功，开始上传到服务器临时文件接口 asset_id=%s", aid)
        try:
            s = get_settings()
            server_base = (s.auth_server_base or "").strip().rstrip("/")
            logger.info(
                "[上传流程-步骤3] server_base=%s asset_id=%s had_bearer_token=%s token_len=%s",
                server_base,
                aid,
                had_bearer,
                len(bearer_token) if bearer_token else 0,
            )
            if not had_bearer:
                logger.warning("[上传流程-步骤3] 无 Authorization Bearer，服务器 upload-temp 将返回 401 asset_id=%s", aid)

            if not server_base:
                step3_network_err = "AUTH_SERVER_BASE 未配置，无法 POST upload-temp"
                logger.error("[上传流程-步骤3] %s asset_id=%s", step3_network_err, aid)
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    files = {"file": (name, data, ct)}
                    headers = {}
                    if bearer_token:
                        headers["Authorization"] = f"Bearer {bearer_token}"
                    installation_id = (
                        request.headers.get("X-Installation-Id")
                        or request.headers.get("x-installation-id")
                        or ""
                    ).strip()
                    if installation_id:
                        headers["X-Installation-Id"] = installation_id
                    upload_url = f"{server_base.rstrip('/')}/api/assets/upload-temp"
                    logger.info("[上传流程-步骤3] POST %s asset_id=%s", upload_url, aid)
                    resp = await client.post(upload_url, files=files, headers=headers)
                    temp_http_status = resp.status_code
                    logger.info("[上传流程-步骤3] 服务器响应状态码=%d asset_id=%s", resp.status_code, aid)
                    if resp.status_code >= 400:
                        temp_body_snip = (resp.text or "")[:800]
                        logger.error(
                            "[上传流程-步骤3] 服务器拒绝或失败 asset_id=%s status=%s body=%s",
                            aid,
                            resp.status_code,
                            temp_body_snip,
                        )
                    else:
                        try:
                            result = resp.json()
                        except Exception as je:
                            temp_json_err = f"{type(je).__name__}: {je}"
                            temp_body_snip = (resp.text or "")[:800]
                            logger.error(
                                "[上传流程-步骤3] 响应非JSON asset_id=%s err=%s content_type=%s body_prefix=%s",
                                aid,
                                temp_json_err,
                                (resp.headers.get("content-type") or ""),
                                temp_body_snip[:400],
                            )
                        else:
                            public_url = result.get("public_url")
                            if public_url:
                                logger.info(
                                    "[上传流程-步骤3] 服务器临时文件上传成功 asset_id=%s temp_id=%s public_url=%s",
                                    aid,
                                    result.get("temp_id"),
                                    public_url[:80],
                                )
                            else:
                                temp_body_snip = json.dumps(result, ensure_ascii=False)[:800]
                                logger.error(
                                    "[上传流程-步骤3] 服务器返回无 public_url asset_id=%s response=%s",
                                    aid,
                                    temp_body_snip[:500],
                                )
        except Exception as e:
            step3_network_err = f"{type(e).__name__}: {e}"
            logger.error(
                "[上传流程-步骤3] 请求异常 asset_id=%s err=%s",
                aid,
                step3_network_err,
                exc_info=True,
            )

    # TOS 与 upload-temp 均未得到公网 URL：不写入库、不返回 asset_id，避免进入图生视频签名链
    if not tos_url and not public_url:
        local_path = ASSETS_DIR / fname
        try:
            if local_path.exists():
                local_path.unlink()
        except Exception as e:
            logger.warning("[上传流程-失败] 删除本地临时文件异常 asset_id=%s path=%s err=%s", aid, local_path, e)
        _sb = (get_settings().auth_server_base or "").strip().rstrip("/")
        logger.error(
            "[上传流程-失败] 汇总 asset_id=%s reason=NO_PUBLIC_URL tos_ok=%s tos_config_present=%s "
            "temp_http=%s had_bearer_token=%s server_base=%s temp_json_err=%s step3_network_err=%s temp_body_snip=%s",
            aid,
            bool(tos_url),
            tos_cfg_present,
            temp_http_status,
            had_bearer,
            _sb,
            temp_json_err or "-",
            step3_network_err or "-",
            (temp_body_snip[:500] + ("…" if len(temp_body_snip) > 500 else "")) if temp_body_snip else "-",
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "本机 TOS 与服务器临时上传均未成功，无公网可访问链接。"
                "请查看本机 logs/app.log 中带 [上传流程-失败] 汇总 的一行（含 temp_http、temp_body_snip）。"
                "常见：未配 TOS_CONFIG、未带登录 Bearer、服务器 upload-temp 返回 401/404、或 AUTH_SERVER_BASE 错误。"
            ),
        )

    # 【步骤4】保存到数据库
    logger.info("[上传流程-步骤4] 保存到数据库 asset_id=%s source_url=%s", aid, (public_url[:80] + "…") if public_url and len(public_url) > 80 else (public_url or "None"))
    asset = Asset(
        asset_id=aid,
        user_id=current_user.id,
        filename=fname,
        media_type=mtype,
        file_size=fsize,
        source_url=public_url,
    )
    db.add(asset)
    db.commit()
    
    # 【步骤5】返回结果（此时必有公网 source_url，见上方 [上传流程-失败]）
    if tos_url:
        logger.info("[上传流程-步骤5] 上传完成（TOS成功）asset_id=%s filename=%s size=%s source_url=%s", aid, fname, fsize, tos_url[:80])
    else:
        logger.info("[上传流程-步骤5] 上传完成（服务器临时文件）asset_id=%s filename=%s size=%s source_url=%s", aid, fname, fsize, (public_url or "")[:80])

    effective_source = tos_url or public_url
    return {
        "asset_id": aid,
        "filename": fname,
        "media_type": mtype,
        "file_size": fsize,
        "source_url": effective_source,
    }


# ── List / search ─────────────────────────────────────────────────

@router.get("/api/assets", summary="列出本地素材")
def list_assets(
    request: Request,
    media_type: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    query = db.query(Asset).filter(Asset.user_id == current_user.id)
    if media_type:
        query = query.filter(Asset.media_type == media_type)
    if q:
        pat = f"%{q}%"
        query = query.filter(
            (Asset.tags.ilike(pat))
            | (Asset.prompt.ilike(pat))
            | (Asset.filename.ilike(pat))
        )
    total = query.count()
    rows = query.order_by(Asset.created_at.desc()).offset(offset).limit(min(limit, 200)).all()
    out = []
    for r in rows:
        mt = (r.media_type or "").lower()
        preview_url = None
        open_url = None
        if mt in ("image", "video"):
            su = (r.source_url or "").strip()
            if _asset_local_path(r):
                preview_url = build_asset_file_url(
                    request,
                    r.asset_id,
                    expiry_sec=_ASSET_LIST_PREVIEW_EXPIRY_SEC,
                )
            elif su.startswith(("http://", "https://")) and not _is_internal_asset_http_url(su):
                preview_url = su
            pub = get_asset_public_url(r.asset_id, current_user.id, request, db)
            if pub:
                open_url = pub
            elif _asset_local_path(r):
                open_url = build_asset_file_url(
                    request,
                    r.asset_id,
                    expiry_sec=_ASSET_LIST_OPEN_FALLBACK_EXPIRY_SEC,
                )
            elif su.startswith(("http://", "https://")) and not _is_internal_asset_http_url(su):
                open_url = su
            # 列表缩略图：签名链若是回环地址，在局域网打开页面时 img/video 无法加载；优先用公网 open_url
            if preview_url and open_url and preview_url != open_url:
                pl = preview_url.lower()
                if ("127.0.0.1" in pl or "localhost" in pl) and open_url.startswith(
                    ("http://", "https://")
                ) and not _is_internal_asset_http_url(open_url):
                    preview_url = open_url
            elif preview_url is None and open_url:
                preview_url = open_url
        out.append(
            {
                "asset_id": r.asset_id,
                "filename": r.filename,
                "media_type": r.media_type,
                "file_size": r.file_size,
                "source_url": r.source_url,
                "preview_url": preview_url,
                "open_url": open_url,
                "prompt": r.prompt,
                "model": r.model,
                "tags": r.tags,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
        )
    return {"total": total, "assets": out}


# ── Get single + serve file ──────────────────────────────────────

@router.get("/api/assets/{asset_id}/content", summary="素材文件内容（需登录，用于前端预览）")
def get_asset_content(
    asset_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    path = ASSETS_DIR / a.filename
    if not path.exists():
        raise HTTPException(404, detail="文件不存在")
    mt_map = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
    ct = mt_map.get((a.media_type or "").lower(), "application/octet-stream")
    return FileResponse(
        path,
        media_type=ct,
        filename=a.filename,
        content_disposition_type="inline",
    )


@router.get("/api/assets/file/{asset_id}", summary="素材文件（带签名公开访问，供速推等拉取）")
def serve_asset_file(
    asset_id: str,
    token: str = Query(..., description="签名 token"),
    expiry: int = Query(..., description="过期时间戳"),
    db: Session = Depends(get_db),
):
    """不校验登录，仅校验 token 与 expiry；用于会话附图/视频时生成可被上游拉取的 URL。"""
    now = int(time.time())
    if expiry < now:
        raise HTTPException(403, detail="链接已过期")
    expected = _asset_file_token(asset_id, expiry)
    if not hmac.compare_digest(expected, token):
        raise HTTPException(403, detail="无效链接")
    a = db.query(Asset).filter(Asset.asset_id == asset_id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    path = ASSETS_DIR / a.filename
    if not path.exists():
        raise HTTPException(404, detail="文件不存在")
    media_type = a.media_type or "application/octet-stream"
    mt_map = {"image": "image/jpeg", "video": "video/mp4", "audio": "audio/mpeg"}
    ct = mt_map.get(media_type, "application/octet-stream")
    return FileResponse(
        path,
        media_type=ct,
        filename=a.filename,
        content_disposition_type="inline",
    )


@router.get("/api/assets/{asset_id}", summary="获取素材详情")
def get_asset(
    asset_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    return {
        "asset_id": a.asset_id,
        "filename": a.filename,
        "media_type": a.media_type,
        "file_size": a.file_size,
        "source_url": a.source_url,
        "prompt": a.prompt,
        "tags": a.tags,
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }


@router.delete("/api/assets/{asset_id}", summary="删除素材")
def delete_asset(
    asset_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
    db: Session = Depends(get_db),
):
    a = db.query(Asset).filter(Asset.asset_id == asset_id, Asset.user_id == current_user.id).first()
    if not a:
        raise HTTPException(404, detail="素材不存在")
    fp = ASSETS_DIR / a.filename
    if fp.exists():
        fp.unlink()
    db.delete(a)
    db.commit()
    return {"ok": True}

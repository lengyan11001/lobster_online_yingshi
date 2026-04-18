"""
本地素材剪辑执行（A 类能力）：优先使用包内 deps/ffmpeg/ffmpeg.exe（Windows），
否则环境变量 LOBSTER_FFMPEG_PATH，再否则 PATH 中的 ffmpeg；失败即报错，无静默降级。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from ..models import Asset

# 与 assets 模块一致
_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ASSETS_DIR = _BASE_DIR / "assets"
logger = logging.getLogger(__name__)


def _is_video_ext(ext: str) -> bool:
    return ext.lower() in (".mp4", ".webm", ".mov", ".mkv", ".avi")


def _is_audio_ext(ext: str) -> bool:
    return ext.lower() in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


def _is_image_ext(ext: str) -> bool:
    return ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")


_VALID_OPS = frozenset({
    "overlay_text",
    "trim",
    "scale_pad",
    "mute",
    "mux_audio",
    "image_to_video",
    "extract_frame",
})

_ASPECT_TO_SIZE = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}

# overlay_text：仅允许下列字段；多传字段直接报错（避免静默忽略）
_OVERLAY_TEXT_PAYLOAD_KEYS = frozenset({
    "operation",
    "asset_id",
    "text",
    "position",
    "vertical_align",
    "horizontal_align",
    "margin_x",
    "margin_y",
    "offset_x",
    "offset_y",
    "font_size",
    "font_color",
    "font_alpha",
    "font_file",
    "x_expr",
    "y_expr",
    "box",
    "box_color",
    "box_border_width",
    "border_width",
    "border_color",
    "shadow_x",
    "shadow_y",
    "shadow_color",
    "shadow_alpha",
    "line_spacing",
    "text_align",
    "fix_bounds",
    "box_alpha",
})

_NAMED_COLORS = frozenset({
    "white",
    "black",
    "red",
    "green",
    "blue",
    "yellow",
    "cyan",
    "magenta",
    "gray",
    "grey",
    "orange",
    "aqua",
})

# ffmpeg 表达式：防注入，仅允许数字、空白、常见变量与运算符（禁止 ; ' \ 等）
_SAFE_FFMPEG_EXPR = re.compile(r"^[a-zA-Z0-9_+\-*/(). \t]{1,800}$")


def _reject_unknown_overlay_keys(payload: Dict[str, Any]) -> None:
    extra = set(payload.keys()) - _OVERLAY_TEXT_PAYLOAD_KEYS
    if extra:
        logger.warning("overlay_text 忽略未知字段: %s", sorted(extra))
        for k in extra:
            payload.pop(k, None)


def _parse_bool(v: Any, field: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(int(v))
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{field} 必须是布尔值")


def _parse_optional_int(v: Any, field: str, *, lo: int, hi: int) -> Optional[int]:
    if v is None:
        return None
    return _parse_int(v, field, lo=lo, hi=hi)


def _parse_int(v: Any, field: str, *, lo: int, hi: int) -> int:
    try:
        i = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} 必须是整数") from e
    if i < lo or i > hi:
        raise ValueError(f"{field} 必须在 {lo}–{hi} 之间")
    return i


def _parse_float_01(v: Any, field: str) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} 必须是数字") from e
    if f < 0 or f > 1:
        raise ValueError(f"{field} 必须在 0–1 之间")
    return f


def _parse_font_color_token(raw: str, field: str) -> str:
    """返回 ffmpeg drawtext 可用的颜色：命名色或 0xRRGGBB。"""
    s = (raw or "").strip()
    if not s:
        raise ValueError(f"{field} 不能为空")
    low = s.lower()
    if "@" in s:
        raise ValueError(f"{field} 请勿在颜色内写 @透明度，请用 font_alpha 或 box 的独立 alpha 字段")
    if low in _NAMED_COLORS:
        return low
    if s.startswith("#"):
        hx = s[1:]
        if len(hx) == 3 and all(c in "0123456789abcdefABCDEF" for c in hx):
            return f"0x{hx[0] * 2}{hx[1] * 2}{hx[2] * 2}"
        if len(hx) == 6 and all(c in "0123456789abcdefABCDEF" for c in hx):
            return f"0x{hx}"
        raise ValueError(f"{field} 十六进制格式须为 #RGB 或 #RRGGBB")
    if s.startswith("0x") or s.startswith("0X"):
        hx = s[2:]
        if len(hx) == 6 and all(c in "0123456789abcdefABCDEF" for c in hx):
            return f"0x{hx.lower()}"
        if len(hx) == 8 and all(c in "0123456789abcdefABCDEF" for c in hx):
            return f"0x{hx.lower()}"
        raise ValueError(f"{field} 十六进制须为 0xRRGGBB 或 0xRRGGBBAA")
    raise ValueError(f"{field} 须为命名色（如 white）或 #RRGGBB / 0xRRGGBB")


def _color_with_alpha(base_token: str, alpha: Optional[float]) -> str:
    if alpha is None:
        return base_token
    return f"{base_token}@{alpha:.6f}".rstrip("0").rstrip(".").rstrip("0") if "." in f"{alpha:.6f}" else f"{base_token}@{alpha}"


def _ffmpeg_color_with_alpha(raw: str, field: str, alpha: Optional[float]) -> str:
    tok = _parse_font_color_token(raw, field)
    if alpha is None:
        return tok
    if len(tok) == 10 and tok.startswith("0x") and len(tok[2:]) == 8:
        raise ValueError(f"{field} 已为 0xRRGGBBAA（含透明度）时不要同时指定 alpha 字段")
    return f"{tok}@{alpha}"


def _safe_xy_expr(s: str, field: str) -> str:
    t = (s or "").strip()
    if not t:
        raise ValueError(f"{field} 不能为空")
    if not _SAFE_FFMPEG_EXPR.match(t):
        raise ValueError(
            f"{field} 含非法字符或过长；仅允许字母数字、空白与 ffmpeg 表达式运算符（如 w、h、text_w、+ - * / ( ) ）"
        )
    return t


@dataclass(frozen=True)
class OverlayTextParams:
    """已校验的 overlay_text 参数，直接用于拼接 drawtext filter。"""

    text: str
    font_size: int
    fontcolor: str
    x_expr: str
    y_expr: str
    fontfile_name: Optional[str]
    box: bool
    boxcolor: Optional[str]
    boxborderw: int
    borderw: int
    bordercolor: str
    shadowx: int
    shadowy: int
    shadowcolor: str
    line_spacing: Optional[int]
    text_align: Optional[str]
    fix_bounds: bool


def _copy_font_into_work(work: Path, font_path: Path, prefix: str) -> str:
    ext = font_path.suffix.lower() if font_path.suffix else ".ttf"
    if ext not in (".ttf", ".ttc", ".otf", ".otc"):
        raise ValueError("font_file 扩展名须为 .ttf/.ttc/.otf/.otc")
    dst = work / f"{prefix}{ext}"
    shutil.copy2(font_path, dst)
    return dst.name


def parse_overlay_text_params(payload: Dict[str, Any], work: Path) -> OverlayTextParams:
    """从 payload 解析 overlay_text 全部可选参数；非法组合与未知字段均报错。"""
    _reject_unknown_overlay_keys(payload)

    text = _sanitize_drawtext_file(str(payload.get("text") or "").strip())

    fs = payload.get("font_size")
    font_size = int(fs) if fs is not None else 48
    font_size = _parse_int(font_size, "font_size", lo=8, hi=200)

    font_color_raw = payload.get("font_color")
    font_color = (font_color_raw if font_color_raw is not None else "white")
    font_color = str(font_color).strip()
    font_alpha = _parse_float_01(payload.get("font_alpha"), "font_alpha")
    fontcolor = _ffmpeg_color_with_alpha(font_color, "font_color", font_alpha)

    # 垂直：position 与 vertical_align 二选一语义；vertical_align 优先
    va_raw = payload.get("vertical_align")
    pos_raw = payload.get("position")
    if va_raw is not None and str(va_raw).strip():
        v_align = str(va_raw).strip().lower()
    elif pos_raw is not None and str(pos_raw).strip():
        v_align = str(pos_raw).strip().lower()
    else:
        v_align = "top"
    if v_align not in ("top", "center", "bottom"):
        raise ValueError("vertical_align / position 必须是 top、center 或 bottom")

    ha = str(payload.get("horizontal_align") or "center").strip().lower()
    if ha not in ("left", "center", "right"):
        raise ValueError("horizontal_align 必须是 left、center 或 right")

    margin_x = _parse_int(payload.get("margin_x") if payload.get("margin_x") is not None else 40, "margin_x", lo=0, hi=4000)
    margin_y = _parse_int(payload.get("margin_y") if payload.get("margin_y") is not None else 40, "margin_y", lo=0, hi=4000)
    offset_x = _parse_int(payload.get("offset_x") if payload.get("offset_x") is not None else 0, "offset_x", lo=-4000, hi=4000)
    offset_y = _parse_int(payload.get("offset_y") if payload.get("offset_y") is not None else 0, "offset_y", lo=-4000, hi=4000)

    x_expr_u = payload.get("x_expr")
    y_expr_u = payload.get("y_expr")
    if (x_expr_u is not None and str(x_expr_u).strip()) or (y_expr_u is not None and str(y_expr_u).strip()):
        if not (x_expr_u is not None and str(x_expr_u).strip() and y_expr_u is not None and str(y_expr_u).strip()):
            raise ValueError("使用自定义位置须同时提供 x_expr 与 y_expr，且均非空")
        x_expr = _safe_xy_expr(str(x_expr_u), "x_expr")
        y_expr = _safe_xy_expr(str(y_expr_u), "y_expr")
    else:
        if ha == "left":
            x_expr = f"{margin_x}+{offset_x}"
        elif ha == "center":
            x_expr = f"(w-text_w)/2+{offset_x}"
        else:
            x_expr = f"w-text_w-{margin_x}+{offset_x}"

        if v_align == "top":
            y_expr = f"{margin_y}+{offset_y}"
        elif v_align == "center":
            y_expr = f"(h-text_h)/2+{offset_y}"
        else:
            y_expr = f"h-text_h-{margin_y}+{offset_y}"

    fontfile_name: Optional[str] = None
    font_file_raw = payload.get("font_file")
    if font_file_raw is not None and str(font_file_raw).strip():
        fp = Path(str(font_file_raw).strip())
        if not fp.is_file():
            raise ValueError(f"font_file 不存在: {fp}")
        fontfile_name = _copy_font_into_work(work, fp.resolve(), "drawtext_user_font")
    elif _text_needs_cjk_font(text):
        font_src = _resolve_drawtext_font_path()
        fontfile_name = _copy_font_into_work(work, font_src, "drawtext_cjk_font")

    box = _parse_bool(payload.get("box"), "box") if payload.get("box") is not None else False
    box_border_w = _parse_int(payload.get("box_border_width") if payload.get("box_border_width") is not None else 0, "box_border_width", lo=0, hi=80)
    boxcolor: Optional[str] = None
    if box:
        bcr = str(payload.get("box_color") if payload.get("box_color") is not None else "black").strip()
        tok = _parse_font_color_token(bcr, "box_color")
        # 0xRRGGBBAA：alpha 已含在颜色内，不再拼接 box_alpha
        if len(bcr) >= 4 and bcr.lower().startswith("0x") and len(bcr) == 10:
            if payload.get("box_alpha") is not None:
                raise ValueError("box_color 为 0xRRGGBBAA 时不要同时指定 box_alpha")
            boxcolor = tok
        else:
            ba = payload.get("box_alpha")
            box_alpha_f = 0.5 if ba is None else _parse_float_01(ba, "box_alpha")
            boxcolor = f"{tok}@{box_alpha_f}"

    border_w = _parse_int(payload.get("border_width") if payload.get("border_width") is not None else 0, "border_width", lo=0, hi=40)
    border_color_raw = payload.get("border_color")
    bordercolor = _ffmpeg_color_with_alpha(
        str(border_color_raw if border_color_raw is not None else "black").strip(),
        "border_color",
        None,
    )

    shadow_x = _parse_int(payload.get("shadow_x") if payload.get("shadow_x") is not None else 0, "shadow_x", lo=-120, hi=120)
    shadow_y = _parse_int(payload.get("shadow_y") if payload.get("shadow_y") is not None else 0, "shadow_y", lo=-120, hi=120)
    shadow_color_raw = payload.get("shadow_color")
    s_alpha = payload.get("shadow_alpha")
    shadow_alpha_f = 0.45 if s_alpha is None else _parse_float_01(s_alpha, "shadow_alpha")
    shadow_tok = _parse_font_color_token(
        str(shadow_color_raw if shadow_color_raw is not None else "black").strip(),
        "shadow_color",
    )
    shadowcolor = f"{shadow_tok}@{shadow_alpha_f}"

    line_spacing = _parse_optional_int(payload.get("line_spacing"), "line_spacing", lo=-200, hi=200)

    ta_raw = payload.get("text_align")
    text_align: Optional[str] = None
    if ta_raw is not None and str(ta_raw).strip():
        text_align = str(ta_raw).strip().lower()
        if text_align not in ("left", "center", "right"):
            raise ValueError("text_align 必须是 left、center 或 right")

    fix_bounds = _parse_bool(payload.get("fix_bounds"), "fix_bounds") if payload.get("fix_bounds") is not None else False

    return OverlayTextParams(
        text=text,
        font_size=font_size,
        fontcolor=fontcolor,
        x_expr=x_expr,
        y_expr=y_expr,
        fontfile_name=fontfile_name,
        box=box,
        boxcolor=boxcolor,
        boxborderw=box_border_w,
        borderw=border_w,
        bordercolor=bordercolor,
        shadowx=shadow_x,
        shadowy=shadow_y,
        shadowcolor=shadowcolor,
        line_spacing=line_spacing,
        text_align=text_align,
        fix_bounds=fix_bounds,
    )


def _text_needs_cjk_font(text: str) -> bool:
    """含中日韩等需 CJK 字形时，drawtext 必须指定含对应字形的 fontfile，否则 ffmpeg 默认字体会显示为方框。"""
    return bool(re.search(r"[\u3000-\u9fff\uac00-\ud7af\u3130-\u318f]", text))


def _cjk_font_candidates() -> list[Path]:
    out: list[Path] = []
    if os.name == "nt":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts = Path(windir) / "Fonts"
        out.extend(
            [
                fonts / "msyh.ttc",
                fonts / "msyhbd.ttc",
                fonts / "simhei.ttf",
                fonts / "simsun.ttc",
                fonts / "msjh.ttc",
            ]
        )
    elif sys.platform == "darwin":
        out.extend(
            [
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/System/Library/Fonts/STHeiti Light.ttc"),
                Path("/Library/Fonts/Arial Unicode.ttf"),
            ]
        )
    else:
        out.extend(
            [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttf"),
            ]
        )
    return out


def _resolve_drawtext_font_path() -> Path:
    """含中文叠字时必须有可用字体文件；未配置则扫描常见系统路径，仍无则报错（无静默用默认字体）。"""
    env = (os.environ.get("LOBSTER_DRAWTEXT_FONT") or "").strip().strip('"')
    if env:
        p = Path(env)
        if not p.is_file():
            raise RuntimeError(f"LOBSTER_DRAWTEXT_FONT 指向的文件不存在: {env}")
        logger.info("[media_edit_exec] drawtext font from LOBSTER_DRAWTEXT_FONT=%s", p)
        return p.resolve()
    for cand in _cjk_font_candidates():
        if cand.is_file():
            logger.info("[media_edit_exec] drawtext font auto-selected=%s", cand)
            return cand.resolve()
    raise RuntimeError(
        "overlay_text 含中文需可渲染 CJK 的字体，但未找到可用字体文件。请安装中文字体（如 Windows 下微软雅黑 "
        "msyh.ttc），或设置环境变量 LOBSTER_DRAWTEXT_FONT 指向 .ttf/.ttc/.otf 的绝对路径。"
    )


def find_ffmpeg() -> str:
    env_path = (os.environ.get("LOBSTER_FFMPEG_PATH") or "").strip().strip('"')
    if env_path:
        ep = Path(env_path)
        if ep.is_file():
            logger.info("[media_edit_exec] ffmpeg from LOBSTER_FFMPEG_PATH=%s", ep)
            return str(ep.resolve())
        raise RuntimeError(f"LOBSTER_FFMPEG_PATH 指向的文件不存在: {env_path}")

    if os.name == "nt":
        bundled = _BASE_DIR / "deps" / "ffmpeg" / "ffmpeg.exe"
        if bundled.is_file():
            logger.info("[media_edit_exec] ffmpeg bundled=%s", bundled)
            return str(bundled.resolve())

    if os.name != "nt":
        bundled = _BASE_DIR / "deps" / "ffmpeg" / "ffmpeg"
        if bundled.is_file():
            logger.info("[media_edit_exec] ffmpeg bundled=%s", bundled)
            return str(bundled.resolve())

    p = shutil.which("ffmpeg")
    if not p:
        logger.error("[media_edit_exec] ffmpeg not found (no bundled deps/ffmpeg, not in PATH)")
        raise RuntimeError(
            "未找到 ffmpeg：请将 ffmpeg.exe 放入本目录 deps/ffmpeg/ffmpeg.exe（打代码包前执行 "
            "python scripts/ensure_ffmpeg_windows.py），或安装 ffmpeg 并加入 PATH，或设置 LOBSTER_FFMPEG_PATH。"
        )
    logger.debug("[media_edit_exec] ffmpeg from PATH=%s", p)
    return p


def _asset_local_path(asset: Asset) -> Optional[Path]:
    fn = asset.filename or ""
    if "/" in fn:
        return None
    p = ASSETS_DIR / fn
    return p if p.exists() else None


def resolve_asset_path(db: Session, user_id: int, asset_id: str) -> Tuple[Path, str, str]:
    """返回本地路径、小写扩展名、media_type。"""
    if not asset_id or not str(asset_id).strip():
        raise ValueError("缺少 asset_id")
    a = db.query(Asset).filter(Asset.asset_id == asset_id.strip(), Asset.user_id == user_id).first()
    if not a:
        raise ValueError(f"素材不存在或无权访问: {asset_id}")
    local = _asset_local_path(a)
    if local is not None:
        ext = local.suffix.lower() or ".bin"
        return local, ext, (a.media_type or "").lower() or "unknown"

    url = (a.source_url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        raise ValueError(f"素材无本地文件且无可下载的 source_url: {asset_id}")

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        data = r.content
    ext = Path(url.split("?")[0]).suffix.lower() or ".bin"
    if ext not in (".mp4", ".webm", ".mov", ".mkv", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp3", ".wav", ".m4a", ".aac"):
        mt = (a.media_type or "").lower()
        if mt == "video":
            ext = ".mp4"
        elif mt == "image":
            ext = ".png"
        elif mt == "audio":
            ext = ".mp3"
        else:
            ext = ".bin"
    td = Path(tempfile.mkdtemp(prefix="media_edit_dl_"))
    tmp = td / f"in_{asset_id}{ext}"
    tmp.write_bytes(data)
    return tmp, ext, (a.media_type or "").lower() or "unknown"


def _run_ffmpeg(args: list, *, cwd: Optional[Path] = None) -> None:
    find_ffmpeg()
    p = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=3600,
    )
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[:4000]
        raise RuntimeError(f"ffmpeg 执行失败 (exit={p.returncode}): {err}")


def _sanitize_drawtext_file(text: str) -> str:
    if not text:
        raise ValueError("overlay_text 需要非空 text")
    if len(text) > 2000:
        raise ValueError("text 长度不能超过 2000")
    return text


def op_overlay_text(
    ffmpeg: str,
    src: Path,
    src_ext: str,
    media_type: str,
    params: OverlayTextParams,
    work: Path,
) -> Path:
    tf = work / "overlay_text.txt"
    tf.write_text(params.text, encoding="utf-8")
    parts: list[str] = []
    if params.fontfile_name:
        parts.append(f"fontfile={params.fontfile_name}")
    parts.extend(
        [
            "textfile=overlay_text.txt",
            f"fontcolor={params.fontcolor}",
            f"fontsize={params.font_size}",
            f"x={params.x_expr}",
            f"y={params.y_expr}",
        ]
    )
    if params.line_spacing is not None:
        parts.append(f"line_spacing={params.line_spacing}")
    if params.text_align is not None:
        parts.append(f"text_align={params.text_align}")
    if params.fix_bounds:
        parts.append("fix_bounds=1")
    if params.box:
        parts.append("box=1")
        if params.boxcolor is not None:
            parts.append(f"boxcolor={params.boxcolor}")
        if params.boxborderw > 0:
            parts.append(f"boxborderw={params.boxborderw}")
    if params.borderw > 0:
        parts.append(f"borderw={params.borderw}")
        parts.append(f"bordercolor={params.bordercolor}")
    if params.shadowx != 0 or params.shadowy != 0:
        parts.append(f"shadowx={params.shadowx}")
        parts.append(f"shadowy={params.shadowy}")
        parts.append(f"shadowcolor={params.shadowcolor}")
    vf = "drawtext=" + ":".join(parts)
    out = work / "out_overlay.mp4"
    is_video = media_type == "video" or _is_video_ext(src_ext)
    if is_video:
        _run_ffmpeg([ffmpeg, "-y", "-i", str(src), "-vf", vf, "-c:a", "copy", str(out)], cwd=work)
        return out
    out_img = work / "out_overlay.png"
    _run_ffmpeg(
        [ffmpeg, "-y", "-i", str(src), "-vf", vf, "-frames:v", "1", str(out_img)],
        cwd=work,
    )
    return out_img


def op_trim(ffmpeg: str, src: Path, media_type: str, start_sec: float, end_sec: float, work: Path) -> Path:
    if start_sec < 0 or end_sec < 0:
        raise ValueError("start_sec/end_sec 不可为负")
    if end_sec <= start_sec:
        raise ValueError("end_sec 必须大于 start_sec")
    dur = end_sec - start_sec
    out = work / "out_trim.mp4"
    if media_type != "video" and not _is_video_ext(src.suffix):
        raise ValueError("trim 仅支持视频素材")
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-ss",
            str(start_sec),
            "-i",
            str(src),
            "-t",
            str(dur),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(out),
        ]
    )
    return out


def op_scale_pad(
    ffmpeg: str, src: Path, media_type: str, aspect_ratio: str, fit_mode: str, work: Path
) -> Path:
    ar = (aspect_ratio or "").strip()
    if ar not in _ASPECT_TO_SIZE:
        raise ValueError(f"aspect_ratio 必须是 {sorted(_ASPECT_TO_SIZE.keys())} 之一")
    mode = (fit_mode or "contain").strip().lower()
    if mode not in ("contain", "cover"):
        raise ValueError("fit_mode 必须是 contain 或 cover")
    w, h = _ASPECT_TO_SIZE[ar]
    out = work / "out_scaled.mp4"
    if media_type == "video" or _is_video_ext(src.suffix):
        if mode == "contain":
            vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
        else:
            vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        # -map 0:a? 避免无音轨时 -c:a copy 失败
        _run_ffmpeg(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-vf",
                vf,
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-c:a",
                "copy",
                str(out),
            ]
        )
        return out
    # 图片
    out_img = work / "out_scaled.png"
    if mode == "contain":
        vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black"
    else:
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    _run_ffmpeg([ffmpeg, "-y", "-i", str(src), "-vf", vf, str(out_img)])
    return out_img


def op_mute(ffmpeg: str, src: Path, media_type: str, work: Path) -> Path:
    if media_type != "video" and not _is_video_ext(src.suffix):
        raise ValueError("mute 仅支持视频素材")
    out = work / "out_mute.mp4"
    _run_ffmpeg([ffmpeg, "-y", "-i", str(src), "-c:v", "copy", "-an", str(out)])
    return out


def op_mux_audio(
    ffmpeg: str,
    video_path: Path,
    audio_path: Path,
    work: Path,
) -> Path:
    out = work / "out_mux.mp4"
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ]
    )
    return out


def op_image_to_video(ffmpeg: str, src: Path, duration_sec: float, work: Path) -> Path:
    if duration_sec <= 0 or duration_sec > 600:
        raise ValueError("duration_sec 必须在 (0, 600] 秒")
    out = work / "out_i2v.mp4"
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-t",
            str(duration_sec),
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            str(out),
        ]
    )
    return out


def op_extract_frame(ffmpeg: str, src: Path, media_type: str, timestamp_sec: float, work: Path) -> Path:
    if media_type != "video" and not _is_video_ext(src.suffix):
        raise ValueError("extract_frame 仅支持视频素材")
    if timestamp_sec < 0:
        raise ValueError("timestamp_sec 不可为负")
    out = work / "frame.png"
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-ss",
            str(timestamp_sec),
            "-i",
            str(src),
            "-vframes",
            "1",
            str(out),
        ]
    )
    return out


def run_operation(db: Session, user_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是对象")
    op = (payload.get("operation") or "").strip()
    if not op:
        raise ValueError(f"缺少 operation，必须指定操作类型，允许: {sorted(_VALID_OPS)}")
    if op not in _VALID_OPS:
        raise ValueError(f"不支持的 operation: {op}，允许: {sorted(_VALID_OPS)}")

    asset_id = (payload.get("asset_id") or "").strip()
    logger.info(
        "[media_edit_exec] begin user_id=%s operation=%s asset_id=%s",
        user_id,
        op,
        asset_id or "(empty)",
    )
    if not asset_id:
        raise ValueError("缺少 asset_id")

    ffmpeg = find_ffmpeg()
    try:
        src, src_ext, media_type = resolve_asset_path(db, user_id, asset_id)
    except ValueError as e:
        logger.warning("[media_edit_exec] resolve_asset failed user_id=%s asset_id=%s err=%s", user_id, asset_id, e)
        raise
    logger.info(
        "[media_edit_exec] resolved user_id=%s asset_id=%s media_type=%s ext=%s",
        user_id,
        asset_id,
        media_type,
        src_ext,
    )
    work = Path(tempfile.mkdtemp(prefix="media_edit_"))

    try:
        if op == "overlay_text":
            if payload.get("text") is None or not str(payload.get("text")).strip():
                raise ValueError("overlay_text 需要 text")
            ot = parse_overlay_text_params(payload, work)
            out_path = op_overlay_text(ffmpeg, src, src_ext, media_type, ot, work)
        elif op == "trim":
            st = payload.get("start_sec")
            en = payload.get("end_sec")
            if st is None or en is None:
                raise ValueError("trim 需要 start_sec 与 end_sec")
            out_path = op_trim(ffmpeg, src, media_type, float(st), float(en), work)
        elif op == "scale_pad":
            ar = payload.get("aspect_ratio")
            fm = payload.get("fit_mode") or "contain"
            out_path = op_scale_pad(ffmpeg, src, media_type, str(ar or ""), str(fm), work)
        elif op == "mute":
            out_path = op_mute(ffmpeg, src, media_type, work)
        elif op == "mux_audio":
            aid = (payload.get("audio_asset_id") or "").strip()
            if not aid:
                raise ValueError("mux_audio 需要 audio_asset_id")
            if aid == asset_id:
                raise ValueError("audio_asset_id 不能与主 asset_id 相同")
            apath, _, amedia = resolve_asset_path(db, user_id, aid)
            if amedia != "audio" and not _is_audio_ext(apath.suffix):
                raise ValueError("audio_asset_id 必须是音频素材")
            if media_type != "video" and not _is_video_ext(src.suffix):
                raise ValueError("mux_audio 主素材必须是视频")
            out_path = op_mux_audio(ffmpeg, src, apath, work)
        elif op == "image_to_video":
            if media_type != "image" and not _is_image_ext(src.suffix):
                raise ValueError("image_to_video 仅支持图片素材")
            d = payload.get("duration_sec")
            if d is None:
                raise ValueError("image_to_video 需要 duration_sec")
            out_path = op_image_to_video(ffmpeg, src, float(d), work)
        elif op == "extract_frame":
            ts = payload.get("timestamp_sec")
            if ts is None:
                raise ValueError("extract_frame 需要 timestamp_sec")
            out_path = op_extract_frame(ffmpeg, src, media_type, float(ts), work)
        else:
            raise ValueError(f"未实现的 operation: {op}")

        data = out_path.read_bytes()
        ext = out_path.suffix.lower() or ".mp4"
        out_mt = "video"
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            out_mt = "image"
        elif ext in (".mp3", ".wav", ".m4a", ".aac"):
            out_mt = "audio"

        from ..api.assets import _save_bytes_or_tos

        aid, fname_or_key, fsize, tos_url = _save_bytes_or_tos(
            data, ext, "video/mp4" if out_mt == "video" else "image/png"
        )
        row = Asset(
            asset_id=aid,
            user_id=user_id,
            filename=fname_or_key,
            media_type=out_mt,
            file_size=fsize,
            source_url=tos_url,
            prompt=f"media.edit:{op}",
            tags="media_edit",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        logger.info(
            "[media_edit_exec] success user_id=%s operation=%s input_asset_id=%s output_asset_id=%s bytes=%s",
            user_id,
            op,
            asset_id,
            aid,
            fsize,
        )
        return {
            "ok": True,
            "operation": op,
            "output_asset_id": aid,
            "media_type": out_mt,
            "file_size": fsize,
        }
    except Exception as e:
        logger.exception(
            "[media_edit_exec] pipeline failed user_id=%s operation=%s asset_id=%s err_type=%s",
            user_id,
            op,
            asset_id,
            type(e).__name__,
        )
        raise
    finally:
        try:
            if src.parent.name.startswith("media_edit_dl_"):
                shutil.rmtree(src.parent, ignore_errors=True)
        except Exception:
            pass
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass

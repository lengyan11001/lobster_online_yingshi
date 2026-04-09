"""
打开可见 Chrome/Chromium → 小红书创作者平台，分阶段采集首页 / 数据总览 / 笔记管理 / 单篇详情 的 XHR 线索。

用法:
  cd E:\\lobster_online
  python scripts\\inspect_xiaohongshu_creator_data.py
  python scripts\\inspect_xiaohongshu_creator_data.py --wait-seconds 300 --detail-wait 90

阶段:
  1) /new/home — 等待扫码登录（--wait-seconds）
  2) /statistics/data-analysis — 数据总览（若 404 会打印当前 URL）
  3) /new/note-manager — 笔记列表
  4) --detail-wait 秒内请你手动点开一条笔记进入「数据分析/详情」页

输出: scripts/_xhs_inspect_out/<phase>/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Set

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))

from playwright_lobster_env import ensure_playwright_browsers_path

OUT_BASE = Path(__file__).resolve().parent / "_xhs_inspect_out"
PROFILE = ROOT / "browser_data" / "inspect_xhs_creator"

URL_HOME = "https://creator.xiaohongshu.com/new/home"
URL_DATA = "https://creator.xiaohongshu.com/statistics/data-analysis"
URL_NOTES = "https://creator.xiaohongshu.com/new/note-manager"


def _chromium_path() -> str:
    return os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "").strip()


def _is_xhs_api(url: str) -> bool:
    u = url.lower()
    return (
        "xiaohongshu.com" in u
        or "xhscdn" in u
        or "fe-platform" in u
        or "creator.xiaohongshu" in u
    )


async def _dump_phase(
    page: Any,
    phase_dir: Path,
    xhr_urls: Set[str],
    json_samples: List[dict[str, Any]],
) -> None:
    phase_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.sleep(2)
    try:
        html = await page.content()
        (phase_dir / "page.html").write_text(html, encoding="utf-8", errors="replace")
    except Exception as e:
        (phase_dir / "page_error.txt").write_text(str(e), encoding="utf-8")
    try:
        inner = await page.evaluate(
            "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 16000) : ''"
        )
        (phase_dir / "body_inner_text.txt").write_text(inner or "", encoding="utf-8")
    except Exception:
        pass
    try:
        iframes = await page.evaluate(
            """() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || '')"""
        )
        (phase_dir / "iframes.txt").write_text("\n".join(iframes or []), encoding="utf-8")
    except Exception:
        pass
    (phase_dir / "xhr_urls.txt").write_text("\n".join(sorted(xhr_urls)), encoding="utf-8")
    seen_u: Set[str] = set()
    slim: List[dict[str, Any]] = []
    for s in json_samples:
        u = s.get("url") or ""
        if u in seen_u:
            continue
        seen_u.add(u)
        slim.append(s)
        if len(slim) >= 60:
            break
    (phase_dir / "json_response_samples.json").write_text(
        json.dumps(slim, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (phase_dir / "final_url.txt").write_text(
        (getattr(page, "url", None) or "") + "\n", encoding="utf-8"
    )


async def _main(wait_seconds: int, detail_wait: int, settle: int) -> None:
    ensure_playwright_browsers_path(ROOT)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("请先安装: pip install playwright")

    PROFILE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUT_BASE / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "locale": "zh-CN",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    exe = _chromium_path()
    if exe and Path(exe).exists():
        launch_kwargs["executable_path"] = exe

    xhr_urls: Set[str] = set()
    json_samples: List[dict[str, Any]] = []

    async def on_response(response: Any) -> None:
        try:
            rt = response.request.resource_type
            if rt not in ("xhr", "fetch"):
                return
            url = response.url
            if not _is_xhs_api(url):
                return
            xhr_urls.add(url)
            ct = (response.headers or {}).get("content-type", "") or ""
            if "json" not in ct.lower():
                return
            try:
                txt = (await response.text())[:12000]
                if len(txt) < 2:
                    return
                json_samples.append(
                    {
                        "url": url[:600],
                        "status": response.status,
                        "content_type": ct[:120],
                        "body_prefix": txt,
                    }
                )
            except Exception:
                pass
        except Exception:
            pass

    print("=" * 60)
    print("小红书创作者数据探测")
    print(f"用户数据目录: {PROFILE}")
    print(f"输出目录: {run_dir}")
    print(f"1) 首页登录等待 {wait_seconds} 秒: {URL_HOME}")
    print(f"2) 数据总览: {URL_DATA}")
    print(f"3) 笔记管理: {URL_NOTES}")
    print(f"4) 接下来 {detail_wait} 秒: 请在笔记管理中点开一条笔记进入详情/数据页")
    print("=" * 60)

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(str(PROFILE), **launch_kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "executable" in msg and "doesn't exist" in msg:
                kw = {k: v for k, v in launch_kwargs.items() if k != "executable_path"}
                kw["channel"] = "chrome"
                print("[info] 使用本机 Google Chrome (channel=chrome)")
                ctx = await p.chromium.launch_persistent_context(str(PROFILE), **kw)
            else:
                raise
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            # --- Phase 1: home + login ---
            await page.goto(URL_HOME, wait_until="domcontentloaded", timeout=90000)
            for i in range(wait_seconds):
                if i > 0 and i % 60 == 0:
                    print(f"… 登录等待剩余约 {wait_seconds - i} 秒")
                await asyncio.sleep(1)
            await asyncio.sleep(settle)
            await _dump_phase(page, run_dir / "01_home", xhr_urls, json_samples)

            # --- Phase 2: data analysis ---
            xhr_urls.clear()
            json_samples.clear()
            try:
                await page.goto(URL_DATA, wait_until="domcontentloaded", timeout=90000)
            except Exception as ex:
                print("[warn] goto data-analysis:", ex)
            await asyncio.sleep(settle)
            await _dump_phase(page, run_dir / "02_data_analysis", xhr_urls, json_samples)

            # --- Phase 3: note manager ---
            xhr_urls.clear()
            json_samples.clear()
            try:
                await page.goto(URL_NOTES, wait_until="domcontentloaded", timeout=90000)
            except Exception as ex:
                print("[warn] goto note-manager:", ex)
            await asyncio.sleep(settle)
            await _dump_phase(page, run_dir / "03_note_manager", xhr_urls, json_samples)

            # --- Phase 4: user opens one note detail ---
            xhr_urls.clear()
            json_samples.clear()
            print(f"\n>>> 请在 {detail_wait} 秒内点击一条笔记，进入详情/数据分析页面 <<<\n")
            for i in range(detail_wait):
                if i > 0 and i % 30 == 0:
                    print(f"… 详情等待剩余约 {detail_wait - i} 秒")
                await asyncio.sleep(1)
            await asyncio.sleep(settle)
            await _dump_phase(page, run_dir / "04_note_detail_manual", xhr_urls, json_samples)

            print("\n完成。各阶段目录:", run_dir)
        finally:
            await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-seconds", type=int, default=480, help="首页等待登录")
    ap.add_argument("--detail-wait", type=int, default=120, help="手动点开笔记详情等待秒数")
    ap.add_argument("--settle", type=int, default=12, help="每阶段导航后额外等待秒数")
    args = ap.parse_args()
    asyncio.run(
        _main(
            max(30, args.wait_seconds),
            max(15, args.detail_wait),
            max(5, min(45, args.settle)),
        )
    )

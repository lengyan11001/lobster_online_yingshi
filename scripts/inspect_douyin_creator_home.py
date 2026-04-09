"""
打开可见 Chromium → 抖音创作者首页，供人工登录后探测数据从哪来。

用法（在 PowerShell 里执行，不要后台跑）:
  cd E:\\lobster_online
  python scripts/inspect_douyin_creator_home.py

默认先等待 600 秒让你登录；时间可改:
  python scripts/inspect_douyin_creator_home.py --wait-seconds 120

结束后会在 scripts/_douyin_inspect_out/ 写入:
  - page.html / body_inner_text.txt
  - xhr_urls.txt（XHR/fetch 的 URL 列表，便于找数据接口）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Set

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))

from playwright_lobster_env import ensure_playwright_browsers_path

HOME_URL = "https://creator.douyin.com/creator-micro/home"
OUT_DIR = Path(__file__).resolve().parent / "_douyin_inspect_out"


def _chromium_path() -> str:
    return os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "").strip()


async def _main(wait_seconds: int) -> None:
    ensure_playwright_browsers_path(ROOT)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("请先安装: pip install playwright && python -m playwright install chromium")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    profile = ROOT / "browser_data" / "inspect_creator_home"
    profile.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1400, "height": 900},
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
            if "douyin" not in url and "bytedance" not in url:
                return
            xhr_urls.add(url)
            ct = (response.headers or {}).get("content-type", "") or ""
            if "json" not in ct.lower():
                return
            try:
                txt = (await response.text())[:8000]
                if len(txt) < 2:
                    return
                json_samples.append(
                    {
                        "url": url[:500],
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
    print("即将打开浏览器 → 抖音创作者首页")
    print(f"用户数据目录: {profile}")
    print(f"请在 {wait_seconds} 秒内完成登录（扫码/手机号等）")
    print("登录后可在首页停留；倒计时结束后脚本会自动保存页面与接口线索。")
    print("=" * 60)

    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(str(profile), **launch_kwargs)
        except Exception as e:
            msg = str(e).lower()
            if "executable" in msg and "doesn't exist" in msg:
                kw = {k: v for k, v in launch_kwargs.items() if k != "executable_path"}
                kw["channel"] = "chrome"
                print(
                    "[info] 未找到 Playwright 自带 Chromium，改用本机 Google Chrome (channel=chrome)。"
                )
                ctx = await p.chromium.launch_persistent_context(str(profile), **kw)
            else:
                raise
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(on_response(r)))
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            for i in range(wait_seconds):
                if i % 30 == 0 and i > 0:
                    print(f"… 剩余约 {wait_seconds - i} 秒")
                await asyncio.sleep(1)

            # 尝试关掉常见弹窗（与正式 driver 一致）
            try:
                from skills.douyin_publish.driver import _dismiss_overlays

                await _dismiss_overlays(page, "inspect")
            except Exception as e:
                print("[warn] dismiss overlays:", e)

            await asyncio.sleep(2)
            html = await page.content()
            (OUT_DIR / "page.html").write_text(html, encoding="utf-8", errors="replace")

            inner = await page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 12000) : ''"
            )
            (OUT_DIR / "body_inner_text.txt").write_text(inner or "", encoding="utf-8")

            iframes = await page.evaluate(
                """() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || '')"""
            )
            (OUT_DIR / "iframes.txt").write_text("\n".join(iframes or []), encoding="utf-8")

            xhr_list = sorted(xhr_urls)
            (OUT_DIR / "xhr_urls.txt").write_text("\n".join(xhr_list), encoding="utf-8")

            # 去重后只保留少量 JSON 样例（避免文件过大）
            seen_u = set()
            slim_samples: List[dict[str, Any]] = []
            for s in json_samples:
                u = s.get("url") or ""
                if u in seen_u:
                    continue
                seen_u.add(u)
                slim_samples.append(s)
                if len(slim_samples) >= 40:
                    break
            (OUT_DIR / "json_response_samples.json").write_text(
                json.dumps(slim_samples, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            print("\n完成。输出目录:", OUT_DIR)
            print("  page.html / body_inner_text.txt / xhr_urls.txt / json_response_samples.json")
            print("当前 URL:", page.url)
        finally:
            await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait-seconds", type=int, default=600, help="等待登录的秒数")
    args = ap.parse_args()
    asyncio.run(_main(max(30, args.wait_seconds)))

"""
打开可见 Chromium → 今日头条头条号后台（mp.toutiao.com），供人工登录后抓取接口与页面结构。

与「抖音 inspect_douyin_creator_home.py」同一套路：持久化 Profile、监听 XHR/fetch、落盘 HTML/正文/URL/JSON 样例。

用法（在 lobster 根目录）:
  cd E:\\lobster_online
  python scripts/inspect_toutiao_mp_home.py

默认等待 **180 秒（3 分钟）**；等待期间**每隔一段时间动态落盘**（无需等倒计时结束也能看 XHR/控件）:
  python scripts/inspect_toutiao_mp_home.py
  python scripts/inspect_toutiao_mp_home.py --wait-seconds 300 --live-dump-seconds 45

若默认 user-data-dir 被占用导致秒退，脚本会**自动**换到 `inspect_toutiao_mp_autofresh_*`；也可手动:
  python scripts/inspect_toutiao_mp_home.py --fresh-profile

倒计时结束后由脚本**自动打开各业务页**（无需你手动点导航）:
  python scripts/inspect_toutiao_mp_home.py --wait-seconds 120 --auto-tour --tour-pause-seconds 10
  · 视频上传页会先尝试点「暂不开通/暂不通」再抓控件
  · 文章发布页会滚动后二次落盘 tour_graphic_publish_after_layout_*；控件扫描含 contenteditable / role=textbox

登录后可在后台随便点「作品 / 数据 / 发布」等；XHR 全程监听，动态文件持续覆盖更新。

输出目录 scripts/_toutiao_inspect_out/
  live_*                    — 动态快照（默认约每 45 秒）：live_xhr_urls.txt、live_controls_snapshot.json、
                             live_json_response_samples.json、live_last_url.txt
  page.html / xhr_urls.txt 等 — 倒计时**正常结束**后最终快照
  json_samples_full/        — mp 接口 JSON 全文（过程中持续写入）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, List, Set

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_SCRIPTS_DIR))

from playwright_lobster_env import ensure_playwright_browsers_path

# 入口：登录页（与发布管理里「今日头条」账号的 login_url 一致）
START_URL = "https://mp.toutiao.com/login/"
# 登录成功后常落在首页或创作台，脚本结束前再尝试打开一次根域抓首页接口
HOME_FALLBACK = "https://mp.toutiao.com/"
OUT_DIR = Path(__file__).resolve().parent / "_toutiao_inspect_out"

# --auto-tour：登录等待结束后由脚本自动跳转（等同你手动点进各页），与 backend 里头条同步 URL 对齐
AUTO_TOUR_STOPS: List[tuple[str, str]] = [
    ("tour_index", "https://mp.toutiao.com/profile_v4/index"),
    ("tour_graphic_publish", "https://mp.toutiao.com/profile_v4/graphic/publish"),
    ("tour_xigua_upload", "https://mp.toutiao.com/profile_v4/xigua/upload-video"),
    ("tour_xigua_publish", "https://mp.toutiao.com/profile_v4/xigua/publish"),
    ("tour_manage_content", "https://mp.toutiao.com/profile_v4/manage/content/all"),
    ("tour_analysis_income", "https://mp.toutiao.com/profile_v4/analysis/income-overview"),
    ("tour_analysis_overview", "https://mp.toutiao.com/profile_v4/analysis/overview"),
]
AUTO_TOUR_TAGS = [t[0] for t in AUTO_TOUR_STOPS]
# 巡页时额外落盘（不在 AUTO_TOUR_STOPS 里）
AUTO_TOUR_EXTRA_TAGS = ["tour_graphic_publish_after_layout"]

# 主文档可交互控件（与收尾保存共用）；含富文本 contenteditable、常见 ARIA 角色
_CONTROLS_EVAL_JS = """
() => {
  const tags = ['INPUT', 'TEXTAREA', 'BUTTON', 'A', 'SELECT'];
  const roleBtns = Array.from(document.querySelectorAll('[role="button"]'));
  const roleInputs = Array.from(document.querySelectorAll(
    '[role="textbox"], [role="combobox"], [role="searchbox"], [role="listbox"]'
  ));
  const editables = Array.from(document.querySelectorAll('[contenteditable="true"]'));
  const set = new Set();
  const out = [];
  function add(el) {
    if (!el || set.has(el)) return;
    set.add(el);
    try {
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0;
      const tn = el.tagName;
      const ce = el.isContentEditable || el.getAttribute('contenteditable') === 'true';
      out.push({
        tag: ce ? 'CONTENTEDITABLE' : tn,
        type: el.type || '',
        placeholder: el.getAttribute('placeholder') || '',
        id: el.id || '',
        className: (typeof el.className === 'string' ? el.className : '').slice(0, 200),
        name: el.name || '',
        innerText: (el.innerText || '').trim().slice(0, 120),
        ariaLabel: el.getAttribute('aria-label') || '',
        dataTestid: el.getAttribute('data-testid') || '',
        href: (tn === 'A' ? (el.getAttribute('href') || '') : '').slice(0, 200),
        visible: visible,
        role: el.getAttribute('role') || '',
        contentEditable: !!ce,
      });
    } catch (e) {}
  }
  tags.forEach(t => document.querySelectorAll(t.toLowerCase()).forEach(add));
  roleBtns.forEach(add);
  roleInputs.forEach(add);
  editables.forEach(add);
  return out.slice(0, 550);
}
"""


async def _dismiss_xigua_video_modal(page: Any) -> bool:
    """西瓜上传页「开通」引导：点「暂不开通」再走后续真实上传区（也匹配「暂不通」文案）。"""
    names = ("暂不开通", "暂不通")
    for name in names:
        try:
            loc = page.get_by_role("button", name=name)
            n = await loc.count()
            if n > 0:
                first = loc.first
                try:
                    if await first.is_visible():
                        await first.click(timeout=5000)
                        print(f"[auto-tour] 已点击「{name}」关闭视频开通引导")
                        await asyncio.sleep(1.2)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
    for name in names:
        try:
            b = page.locator(f'button:has-text("{name}")').first
            if await b.count() > 0:
                await b.click(timeout=4000)
                print(f"[auto-tour] 已点击按钮（:has-text）「{name}」")
                await asyncio.sleep(1.2)
                return True
        except Exception:
            pass
    return False


def _chromium_path() -> str:
    return os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "").strip()


def _toutiao_related_url(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    keys = (
        "toutiao.com",
        "snssdk.com",
        "bytedance.com",
        "byted.org",
        "pstatp.com",
        "byteimg.com",
        "ixigua.com",
        "365yg.com",
    )
    return any(k in u for k in keys)


def _slim_json_samples(json_samples: List[dict[str, Any]]) -> List[dict[str, Any]]:
    seen_u: Set[str] = set()
    slim: List[dict[str, Any]] = []
    for s in json_samples:
        u = str(s.get("url") or "")
        if u in seen_u:
            continue
        seen_u.add(u)
        slim.append(s)
        if len(slim) >= 50:
            break
    return slim


async def _main(
    wait_seconds: int,
    *,
    end_on_home: bool,
    live_dump_seconds: int,
    fresh_profile: bool = False,
    auto_tour: bool = False,
    tour_pause_seconds: int = 8,
) -> None:
    ensure_playwright_browsers_path(ROOT)
    bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if bp:
        print(f"[info] 使用浏览器目录 PLAYWRIGHT_BROWSERS_PATH={bp}（与 start.bat 一致）")
    else:
        print(
            "[info] 未设置 PLAYWRIGHT_BROWSERS_PATH，使用 Playwright 默认缓存"
            "（一般为 %LOCALAPPDATA%\\ms-playwright，即你执行过 playwright install 的位置）"
        )
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise SystemExit("请先安装: pip install playwright && python -m playwright install chromium")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 与抖音 inspect 的目录分开，避免串 Profile / 串站点
    default_profile = ROOT / "browser_data" / "inspect_toutiao_mp"
    ts0 = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Windows 上部分环境无 GPU / 策略限制，Chromium 可能秒退；加兼容参数并关闭 sandbox
    _win_extra: dict[str, Any] = {}
    if sys.platform == "win32":
        _win_extra = {
            "chromium_sandbox": False,
            "args": [
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        }
    base_launch: dict[str, Any] = {
        "headless": False,
        "viewport": {"width": 1400, "height": 900},
        "locale": "zh-CN",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        **_win_extra,
    }
    exe = _chromium_path()
    if exe and not Path(exe).exists():
        print(f"[warn] 环境变量 PLAYWRIGHT_CHROMIUM_PATH 指向的文件不存在，已忽略: {exe}")
        exe = ""

    xhr_urls: Set[str] = set()
    json_samples: List[dict[str, Any]] = []
    full_json_dir = OUT_DIR / "json_samples_full"
    if full_json_dir.exists():
        shutil.rmtree(full_json_dir, ignore_errors=True)
    full_json_dir.mkdir(parents=True, exist_ok=True)
    full_json_count = 0
    full_json_max = 80
    full_json_body_max = 400_000

    async def on_response(response: Any) -> None:
        nonlocal full_json_count
        try:
            rt = response.request.resource_type
            if rt not in ("xhr", "fetch"):
                return
            url = response.url
            if not _toutiao_related_url(url):
                return
            xhr_urls.add(url)
            ct = (response.headers or {}).get("content-type", "") or ""
            if "json" not in ct.lower():
                return
            try:
                raw_txt = await response.text()
                txt = raw_txt[:8000]
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
                if (
                    full_json_count < full_json_max
                    and "mp.toutiao.com" in url.lower()
                    and len(raw_txt) < full_json_body_max
                ):
                    h = abs(hash(url)) % (10**12)
                    fn = full_json_dir / f"{full_json_count:03d}_{h}.json"
                    try:
                        fn.write_text(raw_txt, encoding="utf-8", errors="replace")
                        (full_json_dir / f"{full_json_count:03d}_{h}.url.txt").write_text(
                            url, encoding="utf-8", errors="replace"
                        )
                        full_json_count += 1
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    print("=" * 60)
    print("即将打开浏览器 → 今日头条头条号（mp.toutiao.com）登录页")
    if fresh_profile:
        print(f"用户数据目录: 本次新建（--fresh-profile）inspect_toutiao_mp_{ts0}")
    else:
        print(f"首选用户数据目录: {default_profile}")
        print("若因目录被占用/锁文件导致启动失败，将自动改用 inspect_toutiao_mp_autofresh_*（需在该次会话内重新登录）")
    print("（与抖音探测用的 inspect_creator_home 不是同一个目录，避免打开错站）")
    print(f"请在约 {wait_seconds} 秒内完成登录。")
    print("登录后请**实际打开**你要自动化的页面并停留几秒，例如：")
    print("  · 内容管理 / 全部内容")
    print("  · 数据 → 收益总览")
    print("  · 发布 → 视频 / 图文 / 文章（看真实上传页、标题框、发布按钮）")
    print("倒计时结束时会保存：HTML、可见控件清单 controls_snapshot.json、XHR 列表、mp 接口 JSON 全文样本。")
    if auto_tour:
        pause = max(3, int(tour_pause_seconds))
        print(
            f"[auto-tour] 已开启：倒计时结束后脚本将自动依次打开 "
            f"{len(AUTO_TOUR_STOPS)} 个页面（每页停留约 {pause}s），"
            f"并写入 tour_*_controls_snapshot.json 等，无需你再手动点导航。"
        )
    print("=" * 60)

    async with async_playwright() as p:
        ctx = None
        last_exc: BaseException | None = None
        profile = default_profile
        # 优先内置 Chromium（避免系统 Chrome 与自动化参数冲突）；再试 channel=chrome；最后自定义路径
        try_order: List[tuple[str, dict[str, Any]]] = [
            ("Playwright 内置 Chromium（推荐）", dict(base_launch)),
            ("本机 Google Chrome (channel=chrome)", {**base_launch, "channel": "chrome"}),
        ]
        if exe:
            try_order.append((f"PLAYWRIGHT_CHROMIUM_PATH={exe}", {**base_launch, "executable_path": exe}))

        async def _try_profiles(paths: List[Path], *, auto_note: str) -> None:
            nonlocal ctx, last_exc, profile
            for prof in paths:
                prof.mkdir(parents=True, exist_ok=True)
                for label, kw in try_order:
                    try:
                        print(f"[info] 正在启动: {label} … user-data-dir={prof}")
                        ctx = await p.chromium.launch_persistent_context(str(prof), **kw)
                        profile = prof
                        print(f"[info] 已启动: {label}；实际 user-data-dir={prof}")
                        if auto_note:
                            print(f"[info] {auto_note}")
                        return
                    except BaseException as e:
                        last_exc = e
                        brief = f"{type(e).__name__}: {(str(e) or repr(e))[:600]}"
                        print(f"[warn] {label} 启动失败: {brief}")
                ctx = None

        if fresh_profile:
            await _try_profiles(
                [ROOT / "browser_data" / f"inspect_toutiao_mp_{ts0}"],
                auto_note="本次使用全新配置目录，请完成一次登录。",
            )
        else:
            await _try_profiles([default_profile], auto_note="")
            if ctx is None:
                alt = ROOT / "browser_data" / f"inspect_toutiao_mp_autofresh_{ts0}"
                print(
                    f"[warn] 默认目录启动失败（常见原因：该 user-data-dir 正被其它 Chrome 占用或锁未释放）。"
                    f" 自动改用: {alt}"
                )
                await _try_profiles([alt], auto_note="请在本次打开的浏览器中重新登录头条号。")

        if ctx is None:
            raise SystemExit(
                "无法启动浏览器。\n"
                f" 1) 请先执行: python -m playwright install chromium\n"
                f" 2) 关闭所有占用目录的 Chrome/Chromium，或手动删除锁文件后重试；已尝试目录含: {default_profile}\n"
                f" 3) 可显式使用: python scripts/inspect_toutiao_mp_home.py --fresh-profile\n"
                f" 4) 最后错误: {last_exc}"
            )
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            page.on("response", lambda r: asyncio.create_task(on_response(r)))
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)

            async def dump_live(tag: str = "live") -> None:
                """登录过程中动态落盘，不必等倒计时结束。"""
                try:
                    (OUT_DIR / f"{tag}_last_url.txt").write_text(page.url or "", encoding="utf-8")
                except Exception as e:
                    print(f"[{tag}] last_url: {e}")
                try:
                    (OUT_DIR / f"{tag}_xhr_urls.txt").write_text("\n".join(sorted(xhr_urls)), encoding="utf-8")
                except Exception as e:
                    print(f"[{tag}] xhr_urls: {e}")
                try:
                    controls = await page.evaluate(_CONTROLS_EVAL_JS)
                    (OUT_DIR / f"{tag}_controls_snapshot.json").write_text(
                        json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception as e:
                    print(f"[{tag}] controls: {e}")
                try:
                    (OUT_DIR / f"{tag}_json_response_samples.json").write_text(
                        json.dumps(_slim_json_samples(json_samples), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as e:
                    print(f"[{tag}] json_samples: {e}")

            await dump_live("live")
            print("[live] 已写入首份 live_*（之后按 --live-dump-seconds 间隔覆盖）")

            ld = max(0, int(live_dump_seconds))
            for i in range(wait_seconds):
                if ld > 0 and i > 0 and i % ld == 0:
                    await dump_live("live")
                    print(
                        f"[live] 动态保存（已等待 {i}s / 共 {wait_seconds}s）"
                        f" xhr={len(xhr_urls)} json_sample={len(json_samples)}"
                    )
                if i % 30 == 0 and i > 0:
                    print(f"… 剩余约 {wait_seconds - i} 秒（可继续操作头条后台页面）")
                await asyncio.sleep(1)

            await dump_live("live")
            print(f"[live] 倒计时结束，再保存一轮动态文件 xhr={len(xhr_urls)} json_sample={len(json_samples)}")

            if auto_tour:
                pause = max(3, int(tour_pause_seconds))
                print(f"[auto-tour] 开始自动巡页（每页约 {pause}s）…")
                for tag, tour_url in AUTO_TOUR_STOPS:
                    try:
                        print(f"[auto-tour] goto {tag} → {tour_url}")
                        await page.goto(tour_url, wait_until="domcontentloaded", timeout=90000)

                        if tag == "tour_xigua_upload":
                            await asyncio.sleep(2)
                            clicked = await _dismiss_xigua_video_modal(page)
                            if not clicked:
                                print("[auto-tour] 未找到「暂不开通/暂不通」按钮（可能未弹窗或文案已变）")
                            await asyncio.sleep(pause)
                            await dump_live(tag)
                        elif tag == "tour_graphic_publish":
                            await asyncio.sleep(pause)
                            await dump_live(tag)
                            try:
                                await page.evaluate(
                                    """() => {
                                      const h = document.body ? document.body.scrollHeight : 0;
                                      window.scrollTo(0, Math.min(1400, Math.max(400, h * 0.35)));
                                    }"""
                                )
                                await asyncio.sleep(2)
                                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                                await asyncio.sleep(2)
                                await page.evaluate("() => window.scrollTo(0, 0)")
                                await asyncio.sleep(1.5)
                            except Exception as e:
                                print(f"[auto-tour] 文章页滚动辅助失败（可忽略）: {e}")
                            await dump_live("tour_graphic_publish_after_layout")
                            print("[auto-tour] 已追加 tour_graphic_publish_after_layout_*（滚动后二次抓取）")
                        else:
                            await asyncio.sleep(pause)
                            await dump_live(tag)

                        ctl_path = OUT_DIR / f"{tag}_controls_snapshot.json"
                        nctl = 0
                        if ctl_path.exists():
                            try:
                                nctl = len(json.loads(ctl_path.read_text(encoding="utf-8")))
                            except Exception:
                                pass
                        print(f"[auto-tour] 已落盘 {tag}_*（控件约 {nctl} 条）")
                    except Exception as e:
                        print(f"[auto-tour] 跳过 {tag}: {e}")
                print(f"[auto-tour] 结束；累计 xhr={len(xhr_urls)} json_sample={len(json_samples)}")

            # 默认不跳转：保留你倒计时结束时**当前停留的页面**，控件快照才对应真实发布/数据页
            if end_on_home:
                try:
                    await page.goto(HOME_FALLBACK, wait_until="domcontentloaded", timeout=45000)
                    await asyncio.sleep(4)
                except Exception as e:
                    print("[warn] 二次 goto 首页失败（可忽略）:", e)

            await asyncio.sleep(2)
            (OUT_DIR / "last_url.txt").write_text(page.url or "", encoding="utf-8")

            # 当前页可交互控件（主文档，不含 shadow 内深层）；用于写 Playwright 选择器，不靠猜
            controls = await page.evaluate(_CONTROLS_EVAL_JS)
            (OUT_DIR / "controls_snapshot.json").write_text(
                json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8"
            )

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

            slim_samples = _slim_json_samples(json_samples)
            (OUT_DIR / "json_response_samples.json").write_text(
                json.dumps(slim_samples, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap = OUT_DIR.parent / f"_toutiao_inspect_out_{stamp}"
            snap.mkdir(parents=True, exist_ok=True)
            _snap_names = [
                "page.html",
                "body_inner_text.txt",
                "xhr_urls.txt",
                "json_response_samples.json",
                "iframes.txt",
                "controls_snapshot.json",
                "last_url.txt",
                "live_last_url.txt",
                "live_xhr_urls.txt",
                "live_controls_snapshot.json",
                "live_json_response_samples.json",
            ]
            for ttag in AUTO_TOUR_TAGS + AUTO_TOUR_EXTRA_TAGS:
                for suf in (
                    "last_url.txt",
                    "xhr_urls.txt",
                    "controls_snapshot.json",
                    "json_response_samples.json",
                ):
                    _snap_names.append(f"{ttag}_{suf}")
            for name in _snap_names:
                src = OUT_DIR / name
                if src.exists():
                    shutil.copy2(src, snap / name)
            if full_json_dir.exists() and any(full_json_dir.iterdir()):
                snap_j = snap / "json_samples_full"
                try:
                    shutil.copytree(full_json_dir, snap_j, dirs_exist_ok=True)
                except Exception:
                    pass

            print("\n完成。主输出目录:", OUT_DIR)
            print("  时间戳副本:", snap)
            print("  page.html / controls_snapshot.json / xhr_urls.txt / json_response_samples.json / json_samples_full/")
            print("当前 URL:", page.url)
        finally:
            await ctx.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--wait-seconds",
        type=int,
        default=180,
        help="等待你登录与操作的秒数（默认 180=3 分钟）",
    )
    ap.add_argument(
        "--live-dump-seconds",
        type=int,
        default=45,
        help="每隔多少秒动态写入 live_* 文件；0 表示仅倒计时结束写一次",
    )
    ap.add_argument(
        "--end-on-home",
        action="store_true",
        help="保存前再打开 mp 首页抓一轮通用接口（会离开当前页，一般不需要）",
    )
    ap.add_argument(
        "--fresh-profile",
        action="store_true",
        help="每次使用新的 user-data-dir，避免默认目录被占用或锁文件导致秒退",
    )
    ap.add_argument(
        "--auto-tour",
        action="store_true",
        help="等待结束后脚本自动依次打开：主页/发文章/传视频/视频发布/作品管理/收益总览/数据总览，并写入 tour_* 快照",
    )
    ap.add_argument(
        "--tour-pause-seconds",
        type=int,
        default=8,
        help="auto-tour 每页停留秒数（让 SPA 与 XHR 跑完），默认 8，最小 3",
    )
    args = ap.parse_args()
    asyncio.run(
        _main(
            max(30, args.wait_seconds),
            end_on_home=bool(args.end_on_home),
            live_dump_seconds=max(0, args.live_dump_seconds),
            fresh_profile=bool(args.fresh_profile),
            auto_tour=bool(args.auto_tour),
            tour_pause_seconds=max(3, args.tour_pause_seconds),
        )
    )

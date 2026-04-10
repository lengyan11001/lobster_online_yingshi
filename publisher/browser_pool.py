"""Playwright browser context pool — persistent sessions per account.

Each account gets its own user data directory so cookies/localStorage persist.
The pool lazily starts the Playwright instance on first use.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from .pw_timeouts import ms as _pw_ms
from .pw_timeouts import navigation_timeout_ms

logger = logging.getLogger(__name__)

DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
_DEFAULT_BROWSER_OPTIONS: Dict[str, Any] = {
    "user_agent": DEFAULT_CHROME_UA,
    "proxy": None,
}


def _default_browser_options() -> Dict[str, Any]:
    return dict(_DEFAULT_BROWSER_OPTIONS)


def browser_options_from_publish_meta(meta: Optional[dict]) -> Dict[str, Any]:
    """
    从发布账号的 meta 解析 Playwright 可用的 browser 选项（UA / proxy）。
    meta 结构示例: {"browser": {"user_agent": "...", "proxy": {"server": "http://h:p", ...}}}
    条件不满足时抛出 ValueError（由 API 层转为 400）。
    """
    base = _default_browser_options()
    if not meta or not isinstance(meta, dict):
        return base
    br = meta.get("browser")
    if br is None:
        return base
    if not isinstance(br, dict):
        raise ValueError("账号 meta.browser 必须是对象")

    ua = br.get("user_agent")
    if ua is not None:
        if not isinstance(ua, str) or not ua.strip():
            raise ValueError("账号 meta.browser.user_agent 若填写须为非空字符串")
        base = {**base, "user_agent": ua.strip()}

    px = br.get("proxy")
    if px is None:
        pass
    elif px == {}:
        raise ValueError("账号 meta.browser.proxy 不能为空对象；不需要代理时请省略该字段")
    elif isinstance(px, dict):
        server = px.get("server")
        if not isinstance(server, str) or not server.strip():
            raise ValueError("代理 server 须为非空字符串，例如 http://host:port")
        s = server.strip().lower()
        if not (
            s.startswith("http://")
            or s.startswith("https://")
            or s.startswith("socks5://")
        ):
            raise ValueError("代理 server 须以 http://、https:// 或 socks5:// 开头")
        user = px.get("username")
        pw = px.get("password")
        has_u = user is not None and str(user).strip() != ""
        has_p = pw is not None and str(pw) != ""
        if has_u ^ has_p:
            raise ValueError("代理用户名与密码须同时填写或同时省略")
        pw_obj: Dict[str, Any] = {"server": server.strip()}
        if has_u:
            pw_obj["username"] = str(user).strip()
            pw_obj["password"] = str(pw)
        base = {**base, "proxy": pw_obj}
    else:
        raise ValueError("账号 meta.browser.proxy 必须是对象或省略")

    return base


def browser_options_from_youtube_proxy_fields(
    proxy_server: Optional[str],
    proxy_username: Optional[str],
    proxy_password: Optional[str],
) -> Dict[str, Any]:
    """将 YouTube 账号页的代理字段转为与 `browser_options_from_publish_meta` 相同的结构。

    与发布「打开浏览器」共用同一 Playwright 持久化 Chromium（含 PLAYWRIGHT_CHROMIUM_PATH / CHANNEL）。
    """
    base = _default_browser_options()
    raw = (proxy_server or "").strip()
    if not raw:
        return base
    u = urlparse(raw)
    if u.scheme not in ("http", "https", "socks5"):
        raise ValueError("YouTube 代理须以 http://、https:// 或 socks5:// 开头")
    host = u.hostname
    if not host:
        raise ValueError("代理 URL 中缺少主机名")
    port = u.port if u.port is not None else (443 if u.scheme == "https" else 8080)
    user = (proxy_username or "").strip() or (unquote(u.username) if u.username else "")
    pw = (proxy_password or "").strip() or (unquote(u.password) if u.password else "")
    server = f"{u.scheme}://{host}:{port}"

    # Chromium / Playwright 不支持「带用户名密码的 SOCKS5」；经本机 HTTP 桥转发到上游 SOCKS5（见 socks_http_bridge）。
    if u.scheme == "socks5" and user and pw:
        from .socks_http_bridge import ensure_local_http_bridge

        local_http = ensure_local_http_bridge(host, port, user, pw)
        return {**base, "proxy": {"server": local_http}}

    pw_obj: Dict[str, Any] = {"server": server}
    if user or pw:
        if not user or not pw:
            raise ValueError(
                "使用代理认证时请同时填写用户名与密码，或在代理 URL 中使用 user:pass@host 形式"
            )
        pw_obj["username"] = user
        pw_obj["password"] = pw
    return {**base, "proxy": pw_obj}


def _fingerprint_browser_options(opts: Dict[str, Any]) -> str:
    proxy = opts.get("proxy")
    proxy_canon = None
    if proxy:
        proxy_canon = {
            "server": proxy["server"],
            "username": proxy.get("username"),
            "password": proxy.get("password"),
        }
    blob = json.dumps(
        {"user_agent": opts["user_agent"], "proxy": proxy_canon},
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:40]


def _storage_key(profile_dir: str, browser_options: Dict[str, Any]) -> str:
    return f"{profile_dir}\0{_fingerprint_browser_options(browser_options)}"


def _publish_log_url(page: Any, tag: str) -> None:
    """定位「反复进出页面」：对照每条日志的 url 与 tag 即可判断是哪一步导航。"""
    try:
        u = (getattr(page, "url", None) or "").strip()
    except Exception:
        u = "<error>"
    logger.info("[PUBLISH-NAV] %s url=%s", tag, u[:500] if u else "")

_pw_instance: Any = None
_browser: Any = None
_lock = asyncio.Lock()
# storage_key = profile_dir + "\\0" + fingerprint(proxy+UA)；同一 profile 指纹变化时会关闭旧 context
_contexts: Dict[str, Any] = {}
_context_headless: Dict[str, bool] = {}
_profile_active_key: Dict[str, str] = {}

_BASE_DIR = Path(__file__).resolve().parent.parent
_CHROMIUM_PATH = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
# 例如 chrome：使用本机已安装的 Google Chrome，避免部分环境下 bundled Chromium SIGTRAP。
_BROWSER_CHANNEL = os.environ.get("PLAYWRIGHT_BROWSER_CHANNEL", "").strip()


async def _ensure_browser() -> Any:
    global _pw_instance, _browser
    async with _lock:
        if _browser and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "playwright 未安装。请运行: pip install playwright && python -m playwright install chromium"
            )
        _pw_instance = await async_playwright().__aenter__()

        launch_kwargs: Dict[str, Any] = {"headless": False}
        if _BROWSER_CHANNEL:
            launch_kwargs["channel"] = _BROWSER_CHANNEL
        elif _CHROMIUM_PATH and Path(_CHROMIUM_PATH).exists():
            launch_kwargs["executable_path"] = _CHROMIUM_PATH

        _browser = await _pw_instance.chromium.launch(**launch_kwargs)
        logger.info("Playwright Chromium launched (headless=False)")
        return _browser


async def _acquire_context(
    profile_dir: str,
    *,
    new_headless: bool = False,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, bool]:
    """Get (or reuse) a persistent browser context for the given profile directory.

    Returns (context, created_new). If created_new is False, caller MUST NOT close it.

    browser_options: 由 browser_options_from_publish_meta 得到；None 表示默认 UA、无代理。
    同一 profile_dir 下代理或 UA 变更时会关闭旧 context 并按新指纹新建。

    new_headless: 仅在**新建** context 时生效；若缓存中已有同 storage_key 的 context 则直接复用
    （无法切换 headless）。同一 user_data 目录不可多开，与已有可见窗口并存时可能锁目录失败。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("playwright 未安装")

    opts = (
        browser_options
        if browser_options is not None
        else _default_browser_options()
    )
    key = _storage_key(profile_dir, opts)

    global _pw_instance
    to_close_mismatch: Any = None
    async with _lock:
        if not _pw_instance:
            _pw_instance = await async_playwright().__aenter__()
        old_key = _profile_active_key.get(profile_dir)
        if old_key is not None and old_key != key:
            to_close_mismatch = _contexts.pop(old_key, None)
            _context_headless.pop(old_key, None)
            _profile_active_key.pop(profile_dir, None)
    if to_close_mismatch:
        try:
            await to_close_mismatch.close()
        except Exception:
            pass

    async with _lock:
        existing = _contexts.get(key)
        if existing:
            try:
                if hasattr(existing, "is_closed") and existing.is_closed():
                    _contexts.pop(key, None)
                    _context_headless.pop(key, None)
                    if _profile_active_key.get(profile_dir) == key:
                        _profile_active_key.pop(profile_dir, None)
                else:
                    _ = len(getattr(existing, "pages", []) or [])
                    _profile_active_key[profile_dir] = key
                    return existing, False
            except Exception:
                _contexts.pop(key, None)
                _context_headless.pop(key, None)
                if _profile_active_key.get(profile_dir) == key:
                    _profile_active_key.pop(profile_dir, None)

    # channel 优先级：opts 里显式指定 > 环境变量 > 自定义路径 > 默认 bundled Chromium
    channel_override = opts.get("channel") or _BROWSER_CHANNEL or ""

    launch_kwargs: Dict[str, Any] = {
        "headless": bool(new_headless),
        "viewport": {"width": 1280, "height": 800},
        "locale": "zh-CN",
        "permissions": ["geolocation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=ThirdPartyCookieBlocking,SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure",
        ],
        "ignore_default_args": ["--enable-automation"],
    }
    # 使用真实 Chrome channel 时不覆盖 UA，让浏览器用自身真实 UA，减少指纹不匹配风险
    if not channel_override:
        launch_kwargs["user_agent"] = opts["user_agent"]
    if opts.get("proxy"):
        launch_kwargs["proxy"] = opts["proxy"]
    if channel_override:
        launch_kwargs["channel"] = channel_override
    elif _CHROMIUM_PATH and Path(_CHROMIUM_PATH).exists():
        launch_kwargs["executable_path"] = _CHROMIUM_PATH

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    ctx = await _pw_instance.chromium.launch_persistent_context(
        profile_dir, **launch_kwargs,
    )
    try:
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception as e:
        logger.debug("persistent context add_init_script: %s", e)
    async with _lock:
        _contexts[key] = ctx
        _context_headless[key] = bool(new_headless)
        _profile_active_key[profile_dir] = key
    return ctx, True


async def _drop_cached_context(
    profile_dir: str,
    ctx: Any = None,
    *,
    browser_options: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort remove/close cached context for a profile (matched by storage_key 或 ctx 实例)。"""
    to_close: Any = None
    try:
        async with _lock:
            if ctx is not None:
                for k, c in list(_contexts.items()):
                    pref = k.split("\0", 1)[0]
                    if c is ctx and pref == profile_dir:
                        to_close = _contexts.pop(k, None)
                        _context_headless.pop(k, None)
                        if _profile_active_key.get(profile_dir) == k:
                            _profile_active_key.pop(profile_dir, None)
                        break
            else:
                sk: Optional[str] = None
                if browser_options is not None:
                    sk = _storage_key(profile_dir, browser_options)
                if sk is None:
                    sk = _profile_active_key.get(profile_dir)
                if sk and sk in _contexts:
                    to_close = _contexts.pop(sk, None)
                    _context_headless.pop(sk, None)
                    if _profile_active_key.get(profile_dir) == sk:
                        _profile_active_key.pop(profile_dir, None)
    except Exception:
        pass
    try:
        if to_close is not None:
            await to_close.close()
    except Exception:
        pass


async def _get_page_with_reacquire(
    profile_dir: str,
    ctx: Any,
    *,
    new_headless_on_recreate: bool = False,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Any]:
    """Get page; if context is closed, recreate context once and retry.

    new_headless_on_recreate: 仅重建 context 时生效；与创作者同步的 headless 策略一致。
    发布/登录路径使用默认 False（有头）。
    """
    opts = (
        browser_options
        if browser_options is not None
        else _default_browser_options()
    )
    try:
        page = await _get_page_and_focus(ctx)
        return page, ctx
    except Exception as e:
        msg = str(e).lower()
        if (
            "target page, context or browser has been closed" not in msg
            and "has been closed" not in msg
            and "targetclosederror" not in msg
        ):
            raise
        logger.warning("[BROWSER] stale context detected, recreating: profile=%s err=%s", profile_dir, e)
        await _drop_cached_context(profile_dir, ctx, browser_options=opts)
        new_ctx, _ = await _acquire_context(
            profile_dir,
            new_headless=new_headless_on_recreate,
            browser_options=opts,
        )
        page = await _get_page_and_focus(new_ctx)
        return page, new_ctx


async def _ensure_visible_interactive_context(
    profile_dir: str,
    browser_options: Optional[Dict[str, Any]] = None,
) -> None:
    """若池中仅有无头 context（如刚跑过作品同步），关闭之，以便后续以有头方式打开（发布/扫码登录）。"""
    opts = (
        browser_options
        if browser_options is not None
        else _default_browser_options()
    )
    sk = _storage_key(profile_dir, opts)
    async with _lock:
        cached = _contexts.get(sk)
        is_h = _context_headless.get(sk, False)
    if cached and is_h:
        logger.info("[BROWSER] replace headless pool context with visible (publish/login): profile=%s", profile_dir)
        await _drop_cached_context(profile_dir, cached, browser_options=opts)


def _setup_auto_close(
    ctx: Any,
    profile_dir: str,
    page: Any,
    *,
    browser_options: Optional[Dict[str, Any]] = None,
):
    """用户关闭窗口后释放池内 context。

    Facebook / Meta OAuth 常会再开标签页或弹出「选图验证」窗口；若任一子页关闭就整 context.close()，
    会清空持久化 Cookie，表现为「验证完又回到登录」循环。因此仅在**所有页面都关闭**后再释放。
    """
    opts = (
        browser_options
        if browser_options is not None
        else _default_browser_options()
    )
    sk = _storage_key(profile_dir, opts)

    async def _close_pool():
        try:
            await ctx.close()
        except Exception:
            pass
        try:
            async with _lock:
                if _contexts.get(sk) is ctx:
                    _contexts.pop(sk, None)
                    _context_headless.pop(sk, None)
                    if _profile_active_key.get(profile_dir) == sk:
                        _profile_active_key.pop(profile_dir, None)
        except Exception:
            pass

    async def _maybe_close_after_last_page() -> None:
        await asyncio.sleep(0.35)
        try:
            n = len(ctx.pages)
        except Exception:
            n = 0
        if n > 0:
            logger.info(
                "[BROWSER] 某标签已关闭，仍有 %s 个页面，保留会话与 Cookie（profile …%s）",
                n,
                str(profile_dir)[-50:],
            )
            return
        logger.info("[BROWSER] 所有页面已关闭，释放 context（profile …%s）", str(profile_dir)[-50:])
        await _close_pool()

    def _schedule_maybe_close() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                return
        try:
            loop.create_task(_maybe_close_after_last_page())
        except Exception:
            pass

    wired: set = getattr(ctx, "_lobster_wired_page_ids", None)
    if wired is None:
        wired = set()
        setattr(ctx, "_lobster_wired_page_ids", wired)

    def _wire_page_once(p: Any) -> None:
        try:
            pid = id(p)
            if pid in wired:
                return
            wired.add(pid)
            p.on("close", lambda _p=None: _schedule_maybe_close())
        except Exception:
            pass

    _wire_page_once(page)
    if getattr(ctx, "_lobster_auto_close_registered", False):
        return
    setattr(ctx, "_lobster_auto_close_registered", True)

    try:
        for p in list(getattr(ctx, "pages", []) or []):
            _wire_page_once(p)
    except Exception:
        pass
    try:
        ctx.on("page", lambda p: _wire_page_once(p))
    except Exception:
        pass


async def _get_page_and_focus(ctx: Any) -> Any:
    """Get first page (or create one) and bring to front."""
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    except Exception:
        # Let caller decide whether to reacquire context.
        raise
    await _bring_window_to_front(page)
    return page


async def _bring_window_to_front(page: Any) -> None:
    """Aggressively bring the browser window to OS foreground (Windows-friendly)."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            target = await cdp.send("Browser.getWindowForTarget")
            wid = target.get("windowId")
            if wid:
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "normal"},
                })
                await cdp.send("Browser.setWindowBounds", {
                    "windowId": wid,
                    "bounds": {"windowState": "maximized"},
                })
        finally:
            await cdp.detach()
    except Exception:
        pass


# ── Public API ────────────────────────────────────────────────────


async def open_login_browser(
    profile_dir: str,
    login_url: str,
    platform: str,
    timeout_sec: int = 120,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Open browser for user to scan QR code. Returns immediately."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return {"logged_in": False, "message": f"不支持的平台: {platform}"}

    opts = browser_options if browser_options is not None else _default_browser_options()
    await _ensure_visible_interactive_context(profile_dir, browser_options=opts)
    ctx, created_new = await _acquire_context(
        profile_dir, new_headless=False, browser_options=opts
    )
    try:
        page, ctx = await _get_page_with_reacquire(profile_dir, ctx, browser_options=opts)
        await page.goto(
            login_url,
            wait_until="domcontentloaded",
            timeout=navigation_timeout_ms(30000),
        )
        logger.info("Login browser opened for %s at %s", platform, login_url)
        _setup_auto_close(ctx, profile_dir, page, browser_options=opts)
        return {"logged_in": False, "message": "浏览器已打开，请在窗口内扫码登录（不会自动关闭）"}
    except Exception as e:
        if created_new:
            await _drop_cached_context(profile_dir, ctx, browser_options=opts)
        return {"logged_in": False, "message": str(e)}


async def open_url_in_persistent_chromium(
    profile_dir: str,
    url: str,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """在持久化 Chromium 中打开任意 URL（无平台 driver）。用于 YouTube OAuth 等与发布同源固定浏览器。"""
    opts = browser_options if browser_options is not None else _default_browser_options()
    ctx: Any = None
    created_new = False
    try:
        await _ensure_visible_interactive_context(profile_dir, browser_options=opts)
        ctx, created_new = await _acquire_context(
            profile_dir, new_headless=False, browser_options=opts
        )
        page, ctx = await _get_page_with_reacquire(profile_dir, ctx, browser_options=opts)
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=navigation_timeout_ms(120000),
        )
        try:
            host = urlparse(url).netloc or ""
        except Exception:
            host = ""
        logger.info(
            "[BROWSER] youtube/oauth persistent Chromium url_host=%s profile=%s",
            host[:120],
            profile_dir[:100],
        )
        _setup_auto_close(ctx, profile_dir, page, browser_options=opts)
        return {
            "ok": True,
            "message": "已在龙虾内置 Chromium 中打开（与发布「打开浏览器」相同引擎与可执行文件来源）",
        }
    except Exception as e:
        logger.exception("open_url_in_persistent_chromium failed")
        if created_new and ctx is not None:
            try:
                await _drop_cached_context(profile_dir, ctx, browser_options=opts)
            except Exception:
                pass
        return {"ok": False, "message": str(e)}


async def open_and_check_browser(
    profile_dir: str,
    login_url: str,
    platform: str,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Open browser, bring to front, and check login status. Returns immediately."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return {"logged_in": False, "message": f"不支持的平台: {platform}"}

    opts = browser_options if browser_options is not None else _default_browser_options()
    await _ensure_visible_interactive_context(profile_dir, browser_options=opts)
    ctx, created_new = await _acquire_context(
        profile_dir, new_headless=False, browser_options=opts
    )
    try:
        page, ctx = await _get_page_with_reacquire(profile_dir, ctx, browser_options=opts)

        # 先打开该平台登录入口，避免持久化上下文复用时仍停留在其它站点（例如上次开过抖音）
        if login_url:
            try:
                await page.goto(
                    login_url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms(30000),
                )
                await asyncio.sleep(1)
            except Exception:
                pass

        driver = driver_cls()
        logged_in = await driver.check_login(page, navigate=True)

        if not logged_in:
            try:
                await page.goto(
                    login_url,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms(30000),
                )
            except Exception:
                pass

        _setup_auto_close(ctx, profile_dir, page, browser_options=opts)

        if logged_in:
            return {"logged_in": True, "message": "浏览器已打开，当前已登录"}
        return {"logged_in": False, "message": "浏览器已打开，请扫码登录"}
    except Exception as e:
        if created_new:
            await _drop_cached_context(profile_dir, ctx, browser_options=opts)
        return {"logged_in": False, "message": str(e)}


async def check_browser_login(
    profile_dir: str,
    platform: str,
    browser_options: Optional[Dict[str, Any]] = None,
) -> bool:
    """Check login status. Opens a context if needed (persistent cookies)."""
    from .drivers import DRIVERS

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        return False

    opts = browser_options if browser_options is not None else _default_browser_options()
    key = _storage_key(profile_dir, opts)

    async with _lock:
        ctx = _contexts.get(key)
        recreate_headless = bool(_context_headless.get(key, False))

    if ctx:
        try:
            if hasattr(ctx, "is_closed") and ctx.is_closed():
                ctx = None
        except Exception:
            ctx = None

    if not ctx:
        if not Path(profile_dir).exists():
            return False
        try:
            # 无池内 context 时新建：默认无头，避免仅「检测登录」就弹出窗口
            ctx, _ = await _acquire_context(
                profile_dir, new_headless=True, browser_options=opts
            )
            recreate_headless = True
        except Exception:
            return False

    try:
        page, ctx = await _get_page_with_reacquire(
            profile_dir,
            ctx,
            new_headless_on_recreate=recreate_headless,
            browser_options=opts,
        )
        driver = driver_cls()
        logged_in = await driver.check_login(page, navigate=True)
        if logged_in:
            try:
                await page.bring_to_front()
            except Exception:
                pass
        return logged_in
    except Exception:
        return False


async def run_publish_task(
    profile_dir: str,
    platform: str,
    file_path: str,
    title: str,
    description: str,
    tags: str,
    options: Optional[Dict[str, Any]] = None,
    cover_path: Optional[str] = None,
    browser_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a publish task. Fails fast if not logged in (no blocking poll)."""
    from .drivers import DRIVERS

    logger.info("[PUBLISH] run_publish_task start: platform=%s file=%s title=%s profile=%s",
                platform, file_path, title, profile_dir)

    driver_cls = DRIVERS.get(platform)
    if not driver_cls:
        logger.error("[PUBLISH] unsupported platform: %s", platform)
        return {"ok": False, "error": f"不支持的平台: {platform}"}

    driver = driver_cls()
    opts = browser_options if browser_options is not None else _default_browser_options()
    logger.info("[PUBLISH] acquiring browser context...")
    await _ensure_visible_interactive_context(profile_dir, browser_options=opts)
    ctx, created_new = await _acquire_context(
        profile_dir, new_headless=False, browser_options=opts
    )
    logger.info("[PUBLISH] context acquired (new=%s)", created_new)
    try:
        page, ctx = await _get_page_with_reacquire(profile_dir, ctx, browser_options=opts)
        logger.info("[PUBLISH] page ready, checking login...")
        _publish_log_url(page, "1_after_acquire_page")

        # 头条：空白标签若先被动检测必失败，再 navigate=True 会多一次首页；改为直接进图文/视频业务入口再验登录。
        if platform == "toutiao":
            try:
                from skills.toutiao_publish.driver import toutiao_publish_entry_url
            except Exception:
                toutiao_publish_entry_url = None  # type: ignore
            try:
                u_blank = (getattr(page, "url", None) or "").strip().lower()
            except Exception:
                u_blank = ""
            is_blank = not u_blank or u_blank == "about:blank" or u_blank.startswith("chrome://")
            if is_blank and toutiao_publish_entry_url:
                try:
                    entry = toutiao_publish_entry_url(file_path, options or {})
                    logger.info("[PUBLISH-NAV] toutiao 空白页 -> 直达业务入口 %s（少一次首页往返）", entry)
                    await page.goto(
                        entry,
                        wait_until="domcontentloaded",
                        timeout=navigation_timeout_ms(40000),
                    )
                    await asyncio.sleep(1.2)
                except Exception as ex:
                    logger.warning("[PUBLISH-NAV] toutiao 直达业务入口失败: %s", ex)
                _publish_log_url(page, "1b_toutiao_entry_preload")

        # 先被动检测当前页是否已登录，避免每次发布都从首页再跳进编辑器（看起来像反复进出）。
        login_ok = False
        try:
            login_ok = await driver.check_login(page, navigate=False)
        except Exception:
            login_ok = False
        _publish_log_url(page, "2_after_login_passive")
        logger.info("[PUBLISH-NAV] passive_login_ok=%s", login_ok)
        if not login_ok:
            logger.info(
                "[PUBLISH-NAV] 3_passive_failed -> check_login(navigate=True)，"
                "头条会 goto 首页；若此处频繁出现且 url 变为 mp 根路径，即「编辑页被拉回首页」的原因"
            )
            login_ok = await driver.check_login(page, navigate=True)
        _publish_log_url(page, "4_after_login_final")
        logger.info("[PUBLISH] login check result: %s", login_ok)
        if not login_ok:
            try:
                await page.goto(
                    driver.login_url(),
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout_ms(30000),
                )
            except Exception:
                pass
            _setup_auto_close(ctx, profile_dir, page, browser_options=opts)
            return {
                "ok": False,
                "need_login": True,
                "error": "未登录，已打开浏览器登录页，请扫码登录后再重试发布",
            }

        await _bring_window_to_front(page)
        from .platform_publish_limits import log_and_attach_warnings, normalize_publish_texts

        title_n, desc_n, tags_n, field_warnings = normalize_publish_texts(
            platform, file_path, title, description, tags
        )
        _publish_log_url(page, "5_before_driver_publish")
        logger.info("[PUBLISH] calling driver.publish()...")
        result = await driver.publish(
            page=page,
            file_path=file_path,
            title=title_n,
            description=desc_n,
            tags=tags_n,
            options=options or {},
            cover_path=cover_path,
        )
        result = log_and_attach_warnings(result, field_warnings)
        _publish_log_url(page, "6_after_driver_publish")
        logger.info("[PUBLISH] driver.publish() returned: ok=%s", result.get("ok"))
        if not result.get("ok"):
            logger.warning("[PUBLISH] publish error: %s", result.get("error"))
        _setup_auto_close(ctx, profile_dir, page, browser_options=opts)
        return result
    except Exception as exc:
        logger.exception("[PUBLISH] run_publish_task exception")
        return {"ok": False, "error": str(exc)}


async def dryrun_douyin_upload_in_context(
    profile_dir: str,
    file_path: str,
    title: str = "dryrun 标题",
    description: str = "dryrun 文案",
    tags: str = "dryrun,测试",
    browser_options: Optional[Dict[str, Any]] = None,
    *,
    publish_options: Optional[Dict[str, Any]] = None,
    after_publish: Optional[Callable[[Any, Dict[str, Any]], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """Dry-run a douyin publish flow INSIDE the current process.

    after_publish: 在 driver.publish 返回后、仍持有同一 page 时调用（用于探测脚本在同一 DOM 上采控件）。
    勿依赖「关闭再 goto page.url」恢复发布编辑页——抖音草稿不会随 URL 单独恢复。
    """
    from .drivers.douyin import DouyinDriver, UPLOAD_URL

    driver = DouyinDriver()
    opts = browser_options if browser_options is not None else _default_browser_options()
    await _ensure_visible_interactive_context(profile_dir, browser_options=opts)
    ctx, _created_new = await _acquire_context(
        profile_dir, new_headless=False, browser_options=opts
    )
    page = await _get_page_and_focus(ctx)

    await page.goto(
        UPLOAD_URL,
        wait_until="domcontentloaded",
        timeout=navigation_timeout_ms(30000),
    )
    try:
        await page.wait_for_load_state("networkidle", timeout=_pw_ms(15000))
    except Exception:
        pass

    frames = []
    try:
        for fr in getattr(page, "frames", []) or []:
            frames.append({"name": getattr(fr, "name", ""), "url": getattr(fr, "url", "")})
    except Exception:
        pass

    merged_opts: Dict[str, Any] = {"dry_run": True}
    if publish_options:
        merged_opts.update(publish_options)

    result = await driver.publish(
        page=page,
        file_path=file_path,
        title=title,
        description=description,
        tags=tags,
        options=merged_opts,
        cover_path=None,
    )
    if after_publish is not None:
        await after_publish(page, result)

    return {
        "page_url": getattr(page, "url", ""),
        "title": (await page.title()) if hasattr(page, "title") else "",
        "frame_count": len(frames),
        "frames": frames[:12],
        "driver_result": result,
    }

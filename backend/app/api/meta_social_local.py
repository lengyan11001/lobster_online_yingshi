"""Meta Social OAuth 本地辅助端点。

在本地启动原生 Chrome（非 Playwright，完全无 CDP 连接）完成 Facebook OAuth 授权。
OAuth callback 仍然走远端 lobster-server (api.51ins.com)，本端点只负责：
  1. 向远端 /api/meta-social/oauth/start 获取 login_url
  2. 直接 subprocess 启动系统 Chrome + 代理打开该 URL
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..core.config import settings
from ..services.youtube_api_upload import build_httpx_proxy_url
from .auth import _ServerUser, get_current_user_for_local

router = APIRouter()
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent


def _find_chrome() -> str:
    """在 Windows 上寻找 Chrome 可执行文件路径。"""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and Path(p).exists():
            return p
    return ""


def _build_chrome_proxy_arg(proxy_server: str, proxy_username: str, proxy_password: str) -> str:
    """构建 Chrome --proxy-server 参数值；带认证的 SOCKS5 经本机 HTTP 桥转发。"""
    raw = (proxy_server or "").strip()
    if not raw:
        return ""
    u = urlparse(raw)
    host = u.hostname or ""
    port = u.port if u.port is not None else (443 if u.scheme == "https" else 8080)
    user = (proxy_username or "").strip() or (unquote(u.username) if u.username else "")
    pw = (proxy_password or "").strip() or (unquote(u.password) if u.password else "")

    if u.scheme == "socks5" and user and pw:
        from publisher.socks_http_bridge import ensure_local_http_bridge
        local_http = ensure_local_http_bridge(host, port, user, pw)
        return local_http

    if u.scheme == "socks5":
        return f"socks5://{host}:{port}"
    return f"http://{host}:{port}"


def _launch_native_chrome(profile_dir: str, url: str, proxy_arg: str) -> Dict[str, Any]:
    """直接 subprocess 启动 Chrome，无 CDP / 无 Playwright，Facebook 无法检测自动化。"""
    chrome = _find_chrome()
    if not chrome:
        return {"ok": False, "message": "未找到系统 Chrome，请安装 Google Chrome"}

    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=ThirdPartyCookieBlocking,SameSiteByDefaultCookies,CookiesWithoutSameSiteMustBeSecure",
    ]
    if proxy_arg:
        args.append(f"--proxy-server={proxy_arg}")
    args.append(url)

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        logger.info("[meta-chrome] launched pid=%s chrome=%s proxy=%s", proc.pid, chrome, proxy_arg or "(none)")
        return {"ok": True, "message": f"Chrome 已启动 (pid={proc.pid})"}
    except Exception as e:
        logger.exception("[meta-chrome] launch failed")
        return {"ok": False, "message": f"启动 Chrome 失败: {e}"}


class MetaOAuthLocalStartBody(BaseModel):
    app_id: str = ""
    app_secret: str = ""
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


def _remote_base() -> str:
    base = getattr(settings, "auth_server_base", "") or ""
    return str(base).rstrip("/")


@router.post("/api/meta-social-local/oauth/open-chromium")
async def meta_social_local_open_chromium(
    body: MetaOAuthLocalStartBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    """本地 Chromium + 代理打开 Facebook OAuth 授权页。

    1. 向远端服务器请求 login_url（/api/meta-social/oauth/start）
    2. 用 Playwright 持久化 Chromium + 代理打开该页面
    """
    remote = _remote_base()
    if not remote:
        raise HTTPException(status_code=500, detail="未配置 AUTH_SERVER_BASE，无法连接远端服务器")

    app_id = (body.app_id or "").strip()
    app_secret = (body.app_secret or "").strip()
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="请填写 Facebook App ID 和 App Secret")

    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    token = getattr(current_user, "_raw_token", "") or ""
    if not token:
        from ..core.config import settings as _s
        from jose import jwt as _jwt
        token = _jwt.encode(
            {"sub": str(current_user.id)},
            _s.secret_key,
            algorithm="HS256",
        )

    params: Dict[str, str] = {
        "app_id": app_id,
        "app_secret": app_secret,
        "token": token,
    }
    if ps:
        params["proxy_server"] = ps
    if pu:
        params["proxy_username"] = pu
    if pp:
        params["proxy_password"] = pp

    url = f"{remote}/api/meta-social/oauth/start"
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            resp = await client.get(url, params=params)
            try:
                data = resp.json()
            except ValueError:
                snippet = (resp.text or "")[:800]
                raise HTTPException(
                    status_code=502,
                    detail=f"远端返回非 JSON（HTTP {resp.status_code}）：{snippet}",
                )
            if resp.status_code != 200:
                detail = data.get("detail", str(data)) if isinstance(data, dict) else str(data)
                raise HTTPException(status_code=resp.status_code, detail=f"远端返回错误: {detail}")
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"无法连接远端服务器: {e}")

    login_url = data.get("login_url", "")
    if not login_url:
        raise HTTPException(status_code=500, detail=f"远端未返回 login_url: {data}")

    proxy_arg = _build_chrome_proxy_arg(ps, pu, pp)

    prof_root = _BASE_DIR / "browser_data"
    prof_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(prof_root / "meta_oauth")

    launch_res = _launch_native_chrome(profile_dir, login_url, proxy_arg)

    return {
        "login_url": login_url,
        "redirect_uri": data.get("redirect_uri", ""),
        "chromium_opened": bool(launch_res.get("ok")),
        "chromium_message": str(launch_res.get("message") or ""),
    }


class MetaOpenProxyBrowserBody(BaseModel):
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    url: str = "https://developers.facebook.com/apps/"


@router.post("/api/meta-social-local/open-proxy-browser")
async def meta_social_open_proxy_browser(
    body: MetaOpenProxyBrowserBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    """打开带代理的 Chromium 浏览器，默认访问 Facebook 开发者页面。

    用于在固定 IP 下完成创建 App、配置权限等操作，
    确保所有操作都走同一个 IP。
    """
    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    proxy_arg = _build_chrome_proxy_arg(ps, pu, pp)

    target_url = (body.url or "").strip() or "https://developers.facebook.com/apps/"

    prof_root = _BASE_DIR / "browser_data"
    prof_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(prof_root / "meta_oauth")

    launch_res = _launch_native_chrome(profile_dir, target_url, proxy_arg)

    return {
        "chromium_opened": bool(launch_res.get("ok")),
        "chromium_message": str(launch_res.get("message") or ""),
    }


class MetaOAuthOpenUrlBody(BaseModel):
    login_url: str
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


@router.post("/api/meta-social-local/oauth/open-chromium-url")
async def meta_social_local_open_chromium_url(
    body: MetaOAuthOpenUrlBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    """直接在本地 Chromium + 代理中打开给定的 login_url（用于重新授权等场景）。"""
    login_url = (body.login_url or "").strip()
    if not login_url:
        raise HTTPException(status_code=400, detail="缺少 login_url")

    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    proxy_arg = _build_chrome_proxy_arg(ps, pu, pp)

    prof_root = _BASE_DIR / "browser_data"
    prof_root.mkdir(parents=True, exist_ok=True)
    profile_dir = str(prof_root / "meta_oauth")

    launch_res = _launch_native_chrome(profile_dir, login_url, proxy_arg)

    return {
        "chromium_opened": bool(launch_res.get("ok")),
        "chromium_message": str(launch_res.get("message") or ""),
    }


class TestProxyBody(BaseModel):
    proxy_server: str = ""
    proxy_username: str = ""
    proxy_password: str = ""


def _flip_http_socks_scheme(proxy_server: str) -> Optional[str]:
    """http(s) <-> socks5，用于自动探测端口实际类型。"""
    u = urlparse((proxy_server or "").strip())
    if u.scheme == "http" or u.scheme == "https":
        return urlunparse(("socks5", u.netloc, u.path or "", u.params, u.query, u.fragment))
    if u.scheme == "socks5":
        return urlunparse(("http", u.netloc, u.path or "", u.params, u.query, u.fragment))
    return None


async def _probe_proxy_requests(
    proxy_url: str,
    batch_tag: str,
) -> Tuple[List[Dict[str, Any]], bool]:
    """trust_env=False：避免本机 HTTP(S)_PROXY 与手动代理叠加导致异常首行 / illegal request line。

    不用裸 ``async with httpx.AsyncClient``：部分代理在连接关闭阶段会抛错，若未捕获会变成 500。
    改为手动 ``aclose()`` 并在 finally 里吞掉关闭异常。
    """
    test_targets: List[Tuple[str, str]] = [
        ("http://httpbin.org/ip", "httpbin_http"),
        ("https://httpbin.org/ip", "httpbin_https"),
        ("https://api.ipify.org?format=json", "ipify"),
    ]
    results: List[Dict[str, Any]] = []
    any_ok = False
    client: Optional[httpx.AsyncClient] = None
    try:
        client = httpx.AsyncClient(
            proxy=proxy_url,
            timeout=15.0,
            verify=False,
            trust_env=False,
        )
        for target_url, label in test_targets:
            t0 = time.time()
            full_label = f"{batch_tag}:{label}" if batch_tag else label
            try:
                resp = await client.get(target_url)
                elapsed = int((time.time() - t0) * 1000)
                ip_val = ""
                if resp.status_code == 200:
                    if "httpbin" in label:
                        try:
                            ip_val = str(resp.json().get("origin", "") or "")
                        except Exception:
                            pass
                    elif label == "ipify":
                        try:
                            ip_val = str(resp.json().get("ip", "") or "")
                        except Exception:
                            pass
                row = {
                    "target": full_label,
                    "status": resp.status_code,
                    "latency_ms": elapsed,
                    "ip": ip_val,
                    "ok": resp.status_code < 500 and resp.status_code != 407,
                }
                if row["ok"]:
                    any_ok = True
                results.append(row)
            except Exception as e:
                elapsed = int((time.time() - t0) * 1000)
                results.append({
                    "target": full_label,
                    "status": 0,
                    "latency_ms": elapsed,
                    "ip": "",
                    "ok": False,
                    "error": str(e),
                })
    except Exception as e:
        logger.warning("_probe_proxy_requests fatal: %s", e, exc_info=True)
        results.append({
            "target": f"{batch_tag}:client" if batch_tag else "client",
            "status": 0,
            "latency_ms": 0,
            "ip": "",
            "ok": False,
            "error": str(e),
        })
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as e:
                logger.debug("httpx client aclose ignored: %s", e)

    return results, any_ok


@router.post("/api/meta-social-local/test-proxy")
async def meta_social_test_proxy(
    body: TestProxyBody,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    """Test proxy connectivity by fetching an external IP check service."""
    ps = (body.proxy_server or "").strip()
    pu = (body.proxy_username or "").strip()
    pp = (body.proxy_password or "").strip()

    if not ps:
        raise HTTPException(status_code=400, detail="请填写代理地址")

    u = urlparse(ps)
    if u.scheme not in ("http", "https", "socks5"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的代理协议 '{u.scheme}'，请使用 http / socks5",
        )

    try:
        proxy_url = build_httpx_proxy_url(ps, pu or None, pp or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not proxy_url:
        raise HTTPException(status_code=400, detail="代理地址无效")

    try:
        results, any_ok = await _probe_proxy_requests(proxy_url, "primary")
        used_alternate = False
        alternate_scheme: Optional[str] = None

        if not any_ok:
            err_blob = " ".join((r.get("error") or "") for r in results)
            has_illegal = "illegal request line" in err_blob.lower()
            if has_illegal:
                alt_ps = _flip_http_socks_scheme(ps)
                if alt_ps:
                    try:
                        alt_url = build_httpx_proxy_url(alt_ps, pu or None, pp or None)
                    except ValueError:
                        alt_url = None
                    if alt_url:
                        alt_results, alt_ok = await _probe_proxy_requests(alt_url, "auto_flip")
                        if alt_ok:
                            used_alternate = True
                            alternate_scheme = urlparse(alt_ps).scheme
                            results = alt_results
                            any_ok = True

        first_ip = next((r["ip"] for r in results if r.get("ip")), "")
        first_latency = next((r["latency_ms"] for r in results if r.get("ok")), 0)

        hint = ""
        if any_ok and used_alternate and alternate_scheme:
            hint = (
                f"自动检测：当前端口按「{alternate_scheme.upper()}」可连通，"
                f"请把页面上的协议改为与之一致（{'HTTP' if alternate_scheme == 'http' else 'SOCKS5'}）。"
            )
        elif not any_ok:
            err_blob = " ".join((r.get("error") or "") for r in results)
            has_illegal = "illegal request line" in err_blob.lower()
            has_socksio_missing = "socksio" in err_blob.lower()
            has_tunnel_err = any(
                "TUNNEL" in (r.get("error") or "").upper()
                or "CONNECT" in (r.get("error") or "").upper()
                for r in results
            )
            has_socks_err = any("SOCKS" in (r.get("error") or "").upper() for r in results)
            if has_socksio_missing:
                hint = (
                    f"当前运行后端的 Python 未安装 socksio（与命令行 pip 可能不是同一解释器）。"
                    f"请执行：{sys.executable} -m pip install socksio==1.0.0 然后完全重启 backend。"
                )
            elif has_illegal:
                hint = (
                    "仍出现 illegal request line：① 在页面切换 HTTP / SOCKS5；② 核对供应商给的「端口对应协议」；"
                    "③ 关闭本机 Clash / 系统代理后再测（避免与手动代理冲突）；"
                    "④ 确认账号密码无多余空格。"
                )
            elif has_tunnel_err and u.scheme == "http":
                hint = "HTTPS 隧道失败，可尝试将协议改为 SOCKS5"
            elif has_socks_err and u.scheme == "socks5":
                hint = "SOCKS5 失败，可尝试将协议改为 HTTP"
            elif has_tunnel_err:
                hint = "代理隧道失败，请检查地址/端口/认证"
            else:
                hint = "所有测试目标均不通，请检查代理配置"

        err_parts = [
            r.get("error") or f"HTTP {r.get('status', '')}"
            for r in results
            if not r.get("ok", False)
        ]

        return {
            "success": any_ok,
            "ip": first_ip,
            "latency_ms": first_latency,
            "error": "" if any_ok else "; ".join(err_parts),
            "hint": hint,
            "details": results,
            "auto_detected_scheme": alternate_scheme if used_alternate else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("meta_social_test_proxy failed")
        return {
            "success": False,
            "ip": "",
            "latency_ms": 0,
            "error": str(e),
            "hint": "服务端异常已记录。请执行 pip install -r requirements.txt 后重启 backend；若仍失败请查看终端日志。",
            "details": [],
            "auto_detected_scheme": None,
        }

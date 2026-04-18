"""OpenClaw Gateway configuration: status check, API key management, model selection, restart."""
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core.config import settings
from .auth import get_current_user_for_local, _ServerUser

logger = logging.getLogger(__name__)

# 清除本机 Key / 保存配置 / 手动重启 可能并发触发重启；串行化避免重复 Popen 出多个 node
_OPENCLAW_RESTART_LOCK = threading.Lock()

# 微信 OpenClaw 插件扫码登录（channels login）仅允许单任务，避免多进程争用
_WEIXIN_LOGIN_LOCK = threading.Lock()
_weixin_login_jobs: Dict[str, Dict[str, Any]] = {}
_weixin_login_active_job_id: Optional[str] = None
_WEIXIN_LOGIN_PROC_HOLDER: Dict[str, Any] = {"proc": None}  # 当前子进程，供超时杀掉
_WEIXIN_LOGIN_MAX_SEC = 520

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_OC_DIR = _BASE_DIR / "openclaw"
_OC_CONFIG = _OC_DIR / "openclaw.json"
_WEIXIN_LEDGER = _OC_DIR / ".weixin_login_last.json"
_OC_ENV = _OC_DIR / ".env"

SUPPORTED_PROVIDERS = [
    {"id": "anthropic", "name": "Anthropic", "env_key": "ANTHROPIC_API_KEY",
     "models": ["anthropic/claude-sonnet-4-5", "anthropic/claude-opus-4-6", "anthropic/claude-haiku-3-5"]},
    {"id": "openai", "name": "OpenAI", "env_key": "OPENAI_API_KEY",
     "models": ["openai/gpt-4o", "openai/gpt-4o-mini", "openai/o3-mini"]},
    {"id": "deepseek", "name": "DeepSeek", "env_key": "DEEPSEEK_API_KEY",
     "models": ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]},
    {"id": "google", "name": "Google", "env_key": "GEMINI_API_KEY",
     "models": ["google/gemini-2.5-pro", "google/gemini-2.5-flash"]},
]

DEEPSEEK_PROVIDER_TEMPLATE = {
    "baseUrl": "https://api.deepseek.com",
    "api": "openai-completions",
    "models": [
        {"id": "deepseek-chat", "name": "DeepSeek Chat", "input": ["text"],
         "contextWindow": 65536, "maxTokens": 8192},
        {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner", "reasoning": True,
         "input": ["text"], "contextWindow": 65536, "maxTokens": 8192},
    ],
}


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


def _read_oc_env() -> dict[str, str]:
    result: dict[str, str] = {}
    if not _OC_ENV.exists():
        return result
    for line in _OC_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_oc_env(data: dict[str, str]):
    _OC_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# OpenClaw LLM API Keys")
    lines.append("# 在龙虾后台设置后自动写入")
    for k, v in sorted(data.items()):
        lines.append(f"{k}={v}")
    _OC_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_oc_config() -> dict:
    if not _OC_CONFIG.exists():
        return {}
    try:
        return json.loads(_OC_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_oc_config(config: dict):
    _OC_DIR.mkdir(parents=True, exist_ok=True)
    _OC_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _ensure_openclaw_json_for_local_launch() -> None:
    """在拉起 OpenClaw 子进程前修正磁盘上的 openclaw.json。

    - OpenClaw 将 plugins.load.paths 里的相对路径按 process.cwd() 解析；若 cwd 非项目根会找不到插件。
      此处把已存在于项目根下的相对路径写成绝对路径。
    - 将 lobster-sutui 的 apiKey 写成与 openclaw/.env、后端 settings 一致的明文，避免 ${OPENCLAW_SUTUI_PROXY_KEY}
      在校验阶段尚未注入 process 时报警。
    """
    try:
        if not _OC_CONFIG.exists():
            return
        cfg = _read_oc_config()
        if not cfg:
            return
        changed = False
        plugins = cfg.get("plugins")
        if isinstance(plugins, dict):
            load = plugins.get("load")
            if isinstance(load, dict):
                paths = load.get("paths")
                if isinstance(paths, list):
                    new_paths: list[Any] = []
                    for raw in paths:
                        if not isinstance(raw, str) or not raw.strip():
                            new_paths.append(raw)
                            continue
                        p = raw.strip()
                        r = Path(p)
                        if r.is_absolute():
                            new_paths.append(p)
                            continue
                        candidate = (_BASE_DIR / p).resolve()
                        if candidate.is_dir() or candidate.is_file():
                            new_paths.append(str(candidate))
                            if str(candidate) != p:
                                changed = True
                        else:
                            new_paths.append(p)
                    load["paths"] = new_paths
        env_data = _read_oc_env()
        models = cfg.get("models")
        if isinstance(models, dict):
            provs = models.get("providers")
            if isinstance(provs, dict):
                ls = provs.get("lobster-sutui")
                if isinstance(ls, dict):
                    proxy_key = env_data.get("OPENCLAW_SUTUI_PROXY_KEY", "").strip()
                    if not proxy_key:
                        proxy_key = (settings.openclaw_sutui_proxy_key or "").strip()
                    if proxy_key and ls.get("apiKey") != proxy_key:
                        ls["apiKey"] = proxy_key
                        changed = True
        if changed:
            _write_oc_config(cfg)
            logger.info("Patched openclaw.json for local OpenClaw launch (plugin paths / lobster-sutui apiKey)")
    except Exception as e:
        logger.warning("ensure_openclaw_json_for_local_launch failed: %s", e)


def _model_to_agent_id(model: str) -> str:
    """Slugify a model ID into an OpenClaw agent ID."""
    slug = model.lower().replace("/", "-").replace(".", "-")
    slug = re.sub(r'[^a-z0-9_-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug[:64] or "main"


def _build_agents_list(primary_model: str) -> list[dict]:
    """Build the agents.list array from SUPPORTED_PROVIDERS.

    The default agent ('main') uses the primary model.
    Every supported model also gets a dedicated agent so switching the primary
    later never leaves a model without an agent entry.
    """
    agents = [{"id": "main", "default": True}]
    seen: set[str] = set()
    for prov in SUPPORTED_PROVIDERS:
        for model_id in prov["models"]:
            if model_id in seen:
                continue
            seen.add(model_id)
            agents.append({"id": _model_to_agent_id(model_id), "model": model_id})
    return agents


def _ensure_provider_configs(config: dict):
    """Dynamically add/remove non-built-in providers based on actual API key values.

    Uses the real key value in openclaw.json (not ${ENV_VAR} templates) to avoid
    OpenClaw SecretRef startup failures when keys are empty.
    """
    env_data = _read_oc_env()
    providers = config.setdefault("models", {}).setdefault("providers", {})

    ds_key = env_data.get("DEEPSEEK_API_KEY", "").strip()
    if ds_key:
        ds_cfg = dict(DEEPSEEK_PROVIDER_TEMPLATE)
        ds_cfg["apiKey"] = ds_key
        providers["deepseek"] = ds_cfg
    else:
        providers.pop("deepseek", None)

    if not providers:
        config.get("models", {}).pop("providers", None)
        if not config.get("models"):
            config.pop("models", None)

    proxy_key = env_data.get("OPENCLAW_SUTUI_PROXY_KEY", "").strip()
    if not proxy_key:
        proxy_key = (settings.openclaw_sutui_proxy_key or "").strip()
    ls = providers.get("lobster-sutui")
    if proxy_key and isinstance(ls, dict):
        ls["apiKey"] = proxy_key


def _ensure_agents_list(config: dict):
    """Ensure agents.list contains an agent for every supported model."""
    agents_node = config.setdefault("agents", {})
    primary = agents_node.get("defaults", {}).get("model", {}).get("primary", _DEFAULT_PRIMARY)
    agents_node["list"] = _build_agents_list(primary)


_DEFAULT_PRIMARY = "anthropic/claude-sonnet-4-5"


@router.get("/api/openclaw/status", summary="OpenClaw Gateway 状态")
async def openclaw_status(current_user: _ServerUser = Depends(get_current_user_for_local)):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://127.0.0.1:18789/")
        return {"online": True, "status_code": r.status_code}
    except Exception:
        return {"online": False, "status_code": None}


@router.get("/api/openclaw/config", summary="读取 OpenClaw 配置")
def get_openclaw_config(current_user: _ServerUser = Depends(get_current_user_for_local)):
    env_data = _read_oc_env()
    config = _read_oc_config()

    primary_model = ""
    try:
        primary_model = (
            config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
            or config.get("agent", {}).get("model", {}).get("primary", "")
        )
    except Exception:
        pass

    providers_status = []
    for p in SUPPORTED_PROVIDERS:
        raw_key = env_data.get(p["env_key"], "")
        providers_status.append({
            "id": p["id"],
            "name": p["name"],
            "env_key": p["env_key"],
            "configured": bool(raw_key),
            "masked_key": _mask_key(raw_key),
            "models": p["models"],
        })

    return {
        "primary_model": primary_model,
        "providers": providers_status,
    }


class UpdateOpenClawConfig(BaseModel):
    primary_model: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None


@router.post("/api/openclaw/config", summary="更新 OpenClaw 配置（本地）")
def update_openclaw_config(
    body: UpdateOpenClawConfig,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    env_data = _read_oc_env()
    changed_keys = False

    key_map = {
        "ANTHROPIC_API_KEY": body.anthropic_api_key,
        "OPENAI_API_KEY": body.openai_api_key,
        "DEEPSEEK_API_KEY": body.deepseek_api_key,
        "GEMINI_API_KEY": body.gemini_api_key,
    }
    for env_key, value in key_map.items():
        if value is not None:
            env_data[env_key] = value.strip()
            changed_keys = True

    if changed_keys:
        _write_oc_env(env_data)

    config = _read_oc_config()

    if body.primary_model is not None:
        config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("model", {})["primary"] = body.primary_model.strip()

    _ensure_provider_configs(config)
    _ensure_agents_list(config)
    _write_oc_config(config)

    restarted = False
    if changed_keys:
        restarted = _restart_openclaw_gateway()

    msg = "配置已保存"
    if restarted:
        msg += "，OpenClaw Gateway 已自动重启。"
    elif changed_keys:
        msg += "。API Key 已更新，但自动重启失败，请手动重启（stop.bat + start.bat）。"
    else:
        msg += "。"

    return {"ok": True, "message": msg, "restarted": restarted}


def _find_listener_pids_on_18789() -> list[int]:
    """在 18789 上 **LISTEN** 的进程 PID（不含连到该端口的客户端）。可能多个（异常残留时）。"""
    try:
        if platform.system() == "Windows":
            out = subprocess.check_output(
                'netstat -ano | findstr ":18789 " | findstr "LISTENING"',
                shell=True, text=True, stderr=subprocess.DEVNULL,
            )
            pids: set[int] = set()
            for line in out.strip().splitlines():
                parts = line.split()
                if parts:
                    try:
                        pids.add(int(parts[-1]))
                    except ValueError:
                        continue
            return sorted(pids)
        try:
            out = subprocess.check_output(
                ["lsof", "-nP", "-iTCP:18789", "-sTCP:LISTEN", "-t"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return []
        lines = [x.strip() for x in out.strip().splitlines() if x.strip().isdigit()]
        return sorted({int(x) for x in lines})
    except Exception:
        return []


def _find_openclaw_pid() -> Optional[int]:
    """兼容：返回 18789 上第一个监听 PID（若无则 None）。"""
    pids = _find_listener_pids_on_18789()
    return pids[0] if pids else None


def _wait_until_no_listener_on_18789(max_wait: float = 6.0) -> None:
    """杀掉进程后端口可能短暂未释放，轮询直到无 LISTEN 或超时。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if not _find_listener_pids_on_18789():
            return
        time.sleep(0.12)


def _kill_pid(pid: int):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, 9)
    except Exception as e:
        logger.warning("Failed to kill PID %s: %s", pid, e)


def _build_openclaw_env() -> dict:
    """Build environment variables for the OpenClaw child process."""
    env = dict(os.environ)
    oc_env = _read_oc_env()
    env.update(oc_env)
    env["OPENCLAW_CONFIG_PATH"] = str(_OC_CONFIG)
    env["OPENCLAW_STATE_DIR"] = str(_OC_DIR)
    return env


def _nodejs_bundle_dir() -> Path:
    """含 node.exe 与 node_modules 的目录，默认同仓库 `nodejs/`，可用环境变量覆盖。"""
    raw = (os.environ.get("LOBSTER_NODEJS_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (_BASE_DIR / "nodejs").resolve()


def _find_openclaw_entry() -> Optional[tuple]:
    """Find node executable and openclaw.mjs path. Returns (node_path, mjs_path) or None."""
    import shutil

    bundle = _nodejs_bundle_dir()
    base = _BASE_DIR

    seen_mjs: set[Path] = set()
    mjs_candidates: list[Path] = [
        bundle / "node_modules" / "openclaw" / "openclaw.mjs",
        base / "nodejs" / "node_modules" / "openclaw" / "openclaw.mjs",
        base / "node_modules" / "openclaw" / "openclaw.mjs",
    ]
    mjs_path = None
    for p in mjs_candidates:
        try:
            r = p.resolve()
        except Exception:
            r = p
        if r in seen_mjs:
            continue
        seen_mjs.add(r)
        if p.exists():
            mjs_path = str(p)
            break

    node_path = None
    if platform.system() == "Windows":
        for p in (bundle / "node.exe", bundle / "node", base / "nodejs" / "node.exe", base / "nodejs" / "node"):
            if p.exists():
                node_path = str(p)
                break
        if not node_path:
            node_path = shutil.which("node")
    else:
        # macOS/Linux：包内 node.exe 常为 Windows PE，不可执行；勿选用。
        for p in (bundle / "node", base / "nodejs" / "node"):
            if p.exists() and os.access(p, os.X_OK):
                node_path = str(p)
                break
        if not node_path:
            node_path = shutil.which("node")

    if not (node_path and mjs_path):
        logger.warning(
            "[openclaw] 未解析到入口：node=%r openclaw.mjs=%r bundle=%s（可设置 LOBSTER_NODEJS_DIR）",
            node_path,
            mjs_path,
            bundle,
        )
    if node_path and mjs_path:
        return (node_path, mjs_path)
    return None


def _resolve_nodejs_bundle_node_path(bundle: Path) -> Optional[str]:
    import shutil

    if platform.system() == "Windows":
        for p in (bundle / "node.exe", bundle / "node"):
            if p.exists():
                return str(p.resolve())
        return shutil.which("node")
    bundled = bundle / "node"
    if bundled.exists() and os.access(bundled, os.X_OK):
        return str(bundled.resolve())
    return shutil.which("node")


def _nodejs_bundle_deps_ready(bundle: Path) -> bool:
    oc = bundle / "node_modules" / "openclaw" / "openclaw.mjs"
    wx = bundle / "node_modules" / "@tencent-weixin" / "openclaw-weixin" / "package.json"
    return oc.is_file() and wx.is_file()


def _nodejs_npm_spawn_ready(bundle: Path) -> bool:
    """OpenClaw 在 Windows 上会走 node_modules/npm/bin（含 npm-prefix.js），不能只存在半截 npm。"""
    nb = bundle / "node_modules" / "npm" / "bin"
    lib = bundle / "node_modules" / "npm" / "lib" / "cli.js"
    return (nb / "npm-cli.js").is_file() and (nb / "npm-prefix.js").is_file() and lib.is_file()


def _rmtree_best_effort(path: Path) -> Optional[str]:
    """删除目录；失败时返回简短说明。处理 Windows 只读文件。"""
    if not path.exists():
        return None

    def _onerror(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise

    try:
        shutil.rmtree(path, onerror=_onerror)
    except OSError as e:
        return f"无法删除 {path}（可能被占用或权限不足）：{e}"
    return None


def _purge_npm_for_resync(bundle: Path, *, include_openclaw_cache: bool) -> Optional[str]:
    """授权时自动清理，无需手删。可先只删 node_modules/npm、保留 .openclaw/npm，降低对既有功能与无谓重下的影响；仍失败再带 include_openclaw_cache=True。"""
    paths = [bundle / "node_modules" / "npm"]
    if include_openclaw_cache:
        paths.append(bundle / ".openclaw" / "npm")
    for p in paths:
        existed = p.exists()
        err = _rmtree_best_effort(p)
        if err:
            return err
        if existed:
            logger.info("[nodejs-deps] purged %s", p)
    return None


def _ensure_nodejs_deps_download_if_needed(
    job_id: Optional[str],
    log_buf: Optional[list[str]],
    phase_prefix: str,
) -> Optional[str]:
    """修复 node_modules/npm 供 OpenClaw 装插件；必要时再在线安装 openclaw / 微信包。清理粒度递进，避免无谓动到健康缓存与其它依赖。"""
    bundle = _nodejs_bundle_dir()
    ensure_mjs = bundle / "ensure-npm-cli.mjs"
    run_npm_mjs = bundle / "run-npm.mjs"
    if not ensure_mjs.is_file() or not run_npm_mjs.is_file():
        return "缺少 ensure-npm-cli.mjs 或 run-npm.mjs，请更新客户端。"

    need_install = not _nodejs_bundle_deps_ready(bundle)
    need_npm_sync = not _nodejs_npm_spawn_ready(bundle)
    node_path = _resolve_nodejs_bundle_node_path(bundle)
    if not node_path and (need_install or need_npm_sync):
        return (
            "未找到 Node 可执行文件。请使用含 node.exe 的完整安装包，"
            "或设置环境变量 LOBSTER_NODEJS_DIR 指向含 node.exe 的 nodejs 目录。"
        )
    if not node_path:
        return None

    env = _build_openclaw_env()

    def announce(msg: str) -> None:
        text = f"{phase_prefix}{msg}" if phase_prefix else msg
        if job_id:
            _weixin_job_update(job_id, status="running", message=text)
        logger.info("[nodejs-deps] %s", text)

    def run_argv(argv: list[str], timeout: float, label: str) -> Optional[str]:
        announce(label)
        popen_kw: Dict[str, Any] = {
            "cwd": str(bundle),
            "env": env,
            "capture_output": True,
            "text": True,
            "timeout": timeout,
            "errors": "replace",
        }
        if platform.system() == "Windows":
            popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[arg-type]
        try:
            r = subprocess.run(argv, **popen_kw)
        except subprocess.TimeoutExpired:
            return f"{label} 超时，请检查网络后重试。"
        if log_buf is not None:
            out = (r.stdout or "") + (r.stderr or "")
            if out.strip():
                log_buf.append(out[-12_000:])
        if r.returncode != 0:
            tail = ((r.stderr or "") + (r.stdout or ""))[-1800:]
            logger.warning("[nodejs-deps] %s exit=%s tail=%s", label, r.returncode, tail[-600:])
            short = " ".join(tail.strip().split())[:360]
            return f"{label} 失败（请检查网络与磁盘权限）。{short}"
        return None

    if need_npm_sync:
        announce("正在清理不完整的 npm 安装目录（保留本地 npm 缓存，不影响已正常的其他依赖）…")
        purge_err = _purge_npm_for_resync(bundle, include_openclaw_cache=False)
        if purge_err:
            return purge_err
        for attempt in (1, 2):
            label = (
                "正在准备完整 npm CLI（供 OpenClaw 安装插件，首次可能下载较慢）…"
                if attempt == 1
                else "npm 仍不完整，已连同缓存一并清理并重试…"
            )
            err = run_argv([node_path, str(ensure_mjs)], 300.0, label)
            if err:
                return err
            if _nodejs_npm_spawn_ready(bundle):
                break
            if attempt >= 2:
                return "npm CLI 仍不完整，请关闭占用 nodejs 目录的程序后，再次点击授权。"
            purge_err = _purge_npm_for_resync(bundle, include_openclaw_cache=True)
            if purge_err:
                return purge_err

    if not need_install:
        return None

    err = run_argv([node_path, str(ensure_mjs)], 240.0, "正在确认 npm 与 OpenClaw 安装环境…")
    if err:
        return err
    err = run_argv(
        [node_path, str(run_npm_mjs), "install", "--no-fund", "--no-audit"],
        900.0,
        "正在在线安装 OpenClaw 与微信插件（约 1～5 分钟，请稍候）…",
    )
    if err:
        return err
    err = run_argv([node_path, str(ensure_mjs)], 180.0, "正在将完整 npm 同步回 node_modules…")
    if err:
        return err
    if not _nodejs_bundle_deps_ready(bundle):
        return "依赖安装后仍未检测到 openclaw 或微信插件，请再次点击授权。"
    if not _nodejs_npm_spawn_ready(bundle):
        announce("依赖已装好但 npm 仍异常，先仅清理 spawn 目录并重试同步…")
        purge_err = _purge_npm_for_resync(bundle, include_openclaw_cache=False)
        if purge_err:
            return purge_err
        err = run_argv(
            [node_path, str(ensure_mjs)],
            300.0,
            "正在将完整 npm 同步回 node_modules…",
        )
        if err:
            return err
        if not _nodejs_npm_spawn_ready(bundle):
            announce("仍异常，将清除 npm 缓存后最后一次重试…")
            purge_err = _purge_npm_for_resync(bundle, include_openclaw_cache=True)
            if purge_err:
                return purge_err
            err = run_argv(
                [node_path, str(ensure_mjs)],
                300.0,
                "正在重新下载并同步 npm CLI…",
            )
            if err:
                return err
        if not _nodejs_npm_spawn_ready(bundle):
            return "npm CLI 仍未就绪，请关闭占用程序后再次点击授权。"
    return None


def _line_https_url(line: str) -> Optional[str]:
    s = (line or "").strip()
    if "https://" not in s:
        return None
    m = re.search(r"(https://[^\s\]\)\"'<>]+)", s)
    if not m:
        return None
    return m.group(1).rstrip(").,;'")


def _likely_weixin_qr_url(url: str) -> bool:
    u = url.lower()
    if "weixin.qq.com" in u or "ilink" in u or "wechat" in u:
        return True
    return len(url) >= 40


def _weixin_job_is_terminal(st: str) -> bool:
    return st in ("success", "failed", "timeout")


def _weixin_job_snapshot(job_id: str) -> Dict[str, Any]:
    with _WEIXIN_LOGIN_LOCK:
        j = dict(_weixin_login_jobs.get(job_id) or {})
    j.pop("log_tail", None)
    tail = ""
    with _WEIXIN_LOGIN_LOCK:
        raw = _weixin_login_jobs.get(job_id) or {}
        tail = str(raw.get("log_tail") or "")
    if tail:
        j["log_tail"] = tail[-4000:]
    return j


def _weixin_job_update(job_id: str, **kwargs: Any) -> None:
    with _WEIXIN_LOGIN_LOCK:
        cur = _weixin_login_jobs.get(job_id)
        if not cur:
            return
        cur.update({k: v for k, v in kwargs.items() if v is not None or k in ("qrcode_url", "message")})
        if "log_tail" in kwargs and kwargs["log_tail"] is not None:
            lt = str(kwargs["log_tail"])
            cur["log_tail"] = lt[-12_000:]


def _write_weixin_login_ledger(job_id: str, ok: bool, detail: str = "") -> None:
    try:
        _OC_DIR.mkdir(parents=True, exist_ok=True)
        _WEIXIN_LEDGER.write_text(
            json.dumps(
                {
                    "ok": ok,
                    "job_id": job_id,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "detail": (detail or "")[:800],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("[weixin-login] ledger write failed: %s", e)


def _weixin_run_sync_openclaw_step(
    job_id: str,
    node_path: str,
    mjs_path: str,
    env: dict,
    log_buf: list[str],
    message: str,
    argv_tail: list[str],
    timeout_sec: float = 300.0,
) -> int:
    """Run a single non-interactive openclaw CLI step; append output to log_buf and job log_tail."""
    cmd = [node_path, mjs_path] + argv_tail
    _weixin_job_update(job_id, status="running", message=message, log_tail="".join(log_buf[-200:]))
    kwargs: Dict[str, Any] = dict(
        cwd=str(_BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_sec,
    )
    if platform.system() == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        log_buf.append(f"\n[timeout] {' '.join(argv_tail[:4])}…\n")
        tail = "".join(log_buf[-200:])
        _weixin_job_update(job_id, log_tail=tail)
        return -99
    combined = (proc.stdout or "") + (proc.stderr or "")
    if combined.strip():
        for line in combined.splitlines():
            log_buf.append(line + "\n")
    tail = "".join(log_buf[-200:])
    _weixin_job_update(job_id, log_tail=tail)
    return int(proc.returncode)


def _weixin_run_prep_before_login(job_id: str, node_path: str, mjs_path: str, env: dict, log_buf: list[str]) -> None:
    """腾讯微信插件 README：① plugins install ② config set enabled ③ channels login（流式）④ gateway restart（成功后执行）。"""
    bundled = _BASE_DIR / "nodejs" / "node_modules" / "@tencent-weixin" / "openclaw-weixin"
    install_argv = (
        ["plugins", "install", str(bundled)]
        if bundled.is_dir()
        else ["plugins", "install", "@tencent-weixin/openclaw-weixin"]
    )
    rc1 = _weixin_run_sync_openclaw_step(
        job_id,
        node_path,
        mjs_path,
        env,
        log_buf,
        "① openclaw plugins install（微信插件）…",
        install_argv,
    )
    if rc1 not in (0, -99):
        logger.warning("[weixin-login] plugins install exit=%s, continue to config/login", rc1)
    rc2 = _weixin_run_sync_openclaw_step(
        job_id,
        node_path,
        mjs_path,
        env,
        log_buf,
        "② openclaw config set plugins.entries.openclaw-weixin.enabled true…",
        ["config", "set", "plugins.entries.openclaw-weixin.enabled", "true"],
        timeout_sec=120.0,
    )
    if rc2 not in (0, -99):
        logger.warning("[weixin-login] config set exit=%s, continue to channels login", rc2)


def _weixin_login_worker(job_id: str) -> None:
    global _weixin_login_active_job_id
    log_buf_early: list[str] = []
    err_deps = _ensure_nodejs_deps_download_if_needed(job_id, log_buf_early, "")
    if err_deps:
        _weixin_job_update(job_id, status="failed", message=err_deps, log_tail="".join(log_buf_early)[-4000:])
        _write_weixin_login_ledger(job_id, False, err_deps[:500])
        with _WEIXIN_LOGIN_LOCK:
            if _weixin_login_active_job_id == job_id:
                _weixin_login_active_job_id = None
        return

    entry = _find_openclaw_entry()
    if not entry:
        _weixin_job_update(
            job_id,
            status="failed",
            message=(
                "未找到 node 或 openclaw.mjs。请确认已使用完整安装包；"
                "若 nodejs 不在默认目录，请设置环境变量 LOBSTER_NODEJS_DIR。"
            ),
        )
        _write_weixin_login_ledger(job_id, False, "no openclaw entry")
        with _WEIXIN_LOGIN_LOCK:
            if _weixin_login_active_job_id == job_id:
                _weixin_login_active_job_id = None
        return

    node_path, mjs_path = entry
    _ensure_openclaw_json_for_local_launch()
    env = _build_openclaw_env()
    cmd = [node_path, mjs_path, "channels", "login", "--channel", "openclaw-weixin"]
    log_buf: list[str] = []
    qrcode_sent = False
    rc: Optional[int] = None
    timer: Optional[threading.Timer] = None

    def _kill_proc() -> None:
        proc = _WEIXIN_LOGIN_PROC_HOLDER.get("proc")
        if proc is not None and proc.poll() is None:
            logger.warning("[weixin-login] timeout, killing pid=%s", proc.pid)
            try:
                proc.kill()
            except OSError as e:
                logger.warning("[weixin-login] kill failed: %s", e)
            _weixin_job_update(job_id, status="timeout", message="等待扫码超时，请关闭窗口后重试")
            _write_weixin_login_ledger(job_id, False, "timeout")

    try:
        _weixin_run_prep_before_login(job_id, node_path, mjs_path, env, log_buf)
        _weixin_job_update(job_id, status="running", message="③ openclaw channels login --channel openclaw-weixin（等待控制台输出二维码链接）…")
        kwargs: Dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
            "cwd": str(_BASE_DIR),
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        proc = subprocess.Popen(cmd, **kwargs)
        _WEIXIN_LOGIN_PROC_HOLDER["proc"] = proc
        timer = threading.Timer(float(_WEIXIN_LOGIN_MAX_SEC), _kill_proc)
        timer.daemon = True
        timer.start()

        if proc.stdout:
            for line in proc.stdout:
                log_buf.append(line)
                tail = "".join(log_buf[-200:])
                _weixin_job_update(job_id, log_tail=tail)
                url = _line_https_url(line)
                if url and not qrcode_sent and _likely_weixin_qr_url(url):
                    qrcode_sent = True
                    _weixin_job_update(
                        job_id,
                        status="qrcode_ready",
                        qrcode_url=url,
                        message="③ 已取得二维码链接：请扫下方页面内二维码，或在浏览器打开链接。",
                    )

        rc = proc.wait()
    except Exception as e:
        logger.exception("[weixin-login] worker error job_id=%s", job_id)
        _weixin_job_update(job_id, status="failed", message=str(e)[:500])
        _write_weixin_login_ledger(job_id, False, str(e)[:500])
    finally:
        if timer:
            timer.cancel()
        _WEIXIN_LOGIN_PROC_HOLDER["proc"] = None
        with _WEIXIN_LOGIN_LOCK:
            st_now = (_weixin_login_jobs.get(job_id) or {}).get("status")
        if st_now not in ("timeout", "failed") and rc is not None:
            if rc == 0:
                _weixin_job_update(job_id, status="success", message="微信渠道已登录，凭证已写入 OpenClaw 状态目录")
                _write_weixin_login_ledger(job_id, True, "channels login exit 0")
                try:
                    restarted = _restart_openclaw_gateway()
                    _weixin_job_update(
                        job_id,
                        gateway_restarted=restarted,
                        message=(
                            "④ 已完成：微信渠道已登录，已重启 OpenClaw Gateway。"
                            if restarted
                            else "④ 微信渠道已登录，但自动重启 Gateway 失败，请手动执行 openclaw gateway restart。"
                        ),
                    )
                except Exception as e:
                    logger.warning("[weixin-login] restart after login: %s", e)
                    _weixin_job_update(job_id, gateway_restarted=False, message=f"登录成功但重启 Gateway 异常：{e!s}"[:400])
            else:
                msg = f"登录进程退出码 {rc}，请查看下方日志或 openclaw.log"
                _weixin_job_update(job_id, status="failed", message=msg)
                _write_weixin_login_ledger(job_id, False, msg)
        with _WEIXIN_LOGIN_LOCK:
            if _weixin_login_active_job_id == job_id:
                _weixin_login_active_job_id = None


def _restart_openclaw_gateway_impl() -> bool:
    """在已持有 _OPENCLAW_RESTART_LOCK 时调用：杀光监听 PID，等端口释放，再启动唯一 Gateway。"""
    for pid in _find_listener_pids_on_18789():
        logger.info("Killing OpenClaw listener PID %s", pid)
        _kill_pid(pid)
    _wait_until_no_listener_on_18789(6.0)
    leftover = _find_listener_pids_on_18789()
    if leftover:
        logger.warning("Port 18789 still has listener PIDs after kill: %s — retrying SIGKILL", leftover)
        for pid in leftover:
            _kill_pid(pid)
        _wait_until_no_listener_on_18789(4.0)

    # nodejs/npm 与 OpenClaw 依赖仅在「微信授权」流程中在线安装，启动/Gateway 重启不在此下载，以免拖死服务启动。
    entry = _find_openclaw_entry()
    if not entry:
        logger.warning("Cannot restart OpenClaw: node or openclaw.mjs not found")
        return False

    _ensure_openclaw_json_for_local_launch()

    node_path, mjs_path = entry
    env = _build_openclaw_env()
    log_path = _BASE_DIR / "openclaw.log"

    try:
        cmd = [node_path, mjs_path, "gateway", "--port", "18789"]
        log_file = None
        for _ in range(2):
            try:
                log_file = open(log_path, "a", encoding="utf-8")
                break
            except OSError as e:
                if getattr(e, "errno", None) == 13:  # Permission denied (file locked by previous process)
                    time.sleep(0.5)
                    continue
                raise
        if log_file is None:
            logger.warning("openclaw.log locked, OpenClaw stdout/stderr will not be written to file")
            log_file = subprocess.DEVNULL

        kwargs = {
            "stdout": log_file,
            "stderr": log_file,
            "env": env,
            "cwd": str(_BASE_DIR),
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        subprocess.Popen(cmd, **kwargs)
        logger.info("OpenClaw Gateway restarting: %s", " ".join(cmd))
        time.sleep(2)

        new_pid = _find_openclaw_pid()
        if new_pid:
            logger.info("OpenClaw Gateway restarted, PID %s", new_pid)
            return True
        else:
            logger.warning("OpenClaw Gateway process started but not listening yet")
            return True
    except Exception as e:
        logger.error("Failed to restart OpenClaw Gateway: %s", e)
        return False


def _restart_openclaw_gateway() -> bool:
    """串行重启，避免「清除配置」与「保存 Key」等并发各拉起一个 node。"""
    with _OPENCLAW_RESTART_LOCK:
        return _restart_openclaw_gateway_impl()


@router.post("/api/openclaw/restart", summary="重启 OpenClaw Gateway")
async def restart_openclaw(current_user: _ServerUser = Depends(get_current_user_for_local)):
    ok = _restart_openclaw_gateway()
    if ok:
        return {"ok": True, "message": "OpenClaw Gateway 已重启"}
    return {"ok": False, "message": "重启失败，请手动执行 stop.bat + start.bat"}


def _prune_old_weixin_login_jobs() -> None:
    now = time.time()
    with _WEIXIN_LOGIN_LOCK:
        dead = [
            k
            for k, v in list(_weixin_login_jobs.items())
            if _weixin_job_is_terminal(str(v.get("status") or ""))
            and now - float(v.get("started_at") or 0) > 3600
        ]
        for k in dead:
            _weixin_login_jobs.pop(k, None)


@router.post("/api/openclaw/weixin-login/start", summary="启动 OpenClaw 微信插件扫码登录（本机子进程）")
async def openclaw_weixin_login_start(current_user: _ServerUser = Depends(get_current_user_for_local)):
    """等价于在项目根设置 OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR 后执行：
    node openclaw.mjs channels login --channel openclaw-weixin
    """
    global _weixin_login_active_job_id
    _prune_old_weixin_login_jobs()
    with _WEIXIN_LOGIN_LOCK:
        if _weixin_login_active_job_id:
            existing = _weixin_login_jobs.get(_weixin_login_active_job_id)
            if existing:
                st = str(existing.get("status") or "")
                if not _weixin_job_is_terminal(st):
                    age = time.time() - float(existing.get("started_at") or 0)
                    if age < float(_WEIXIN_LOGIN_MAX_SEC) + 180.0:
                        return {
                            "job_id": _weixin_login_active_job_id,
                            "status": st,
                            "reused": True,
                        }
        jid = uuid.uuid4().hex
        _weixin_login_jobs[jid] = {
            "job_id": jid,
            "status": "starting",
            "qrcode_url": None,
            "message": "正在启动…",
            "started_at": time.time(),
            "gateway_restarted": None,
        }
        _weixin_login_active_job_id = jid
    threading.Thread(target=_weixin_login_worker, args=(jid,), daemon=True).start()
    logger.info("[weixin-login] job started job_id=%s user_id=%s", jid, current_user.id)
    return {"job_id": jid, "status": "starting", "reused": False}


@router.get("/api/openclaw/weixin-login/status", summary="查询微信扫码登录任务状态")
async def openclaw_weixin_login_status(
    job_id: str,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    jid = (job_id or "").strip()
    if not jid:
        raise HTTPException(status_code=400, detail="缺少 job_id")
    with _WEIXIN_LOGIN_LOCK:
        if jid not in _weixin_login_jobs:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return _weixin_job_snapshot(jid)


@router.get("/api/openclaw/weixin-login/last", summary="上次微信渠道登录结果摘要（本机 ledger）")
async def openclaw_weixin_login_last(current_user: _ServerUser = Depends(get_current_user_for_local)):
    if not _WEIXIN_LEDGER.exists():
        return {"last_ok": False, "at": None, "detail": ""}
    try:
        data = json.loads(_WEIXIN_LEDGER.read_text(encoding="utf-8"))
        return {
            "last_ok": bool(data.get("ok")),
            "at": data.get("at"),
            "detail": str(data.get("detail") or "")[:500],
        }
    except Exception:
        return {"last_ok": False, "at": None, "detail": "ledger 损坏"}


def clear_openclaw_local_provider_keys() -> tuple[bool, bool]:
    """从本机 openclaw/.env 移除各厂商 API Key（仅写本地文件，不上传任何服务端）。

    Returns:
        (env_changed, gateway_restarted)
    """
    env_data = _read_oc_env()
    changed = False
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY"):
        if k in env_data and (env_data[k] or "").strip():
            del env_data[k]
            changed = True
    if not changed:
        return False, False
    _write_oc_env(env_data)
    restarted = _restart_openclaw_gateway()
    return True, restarted


# --------------- SuTui MCP Config ---------------

_SUTUI_CONFIG_PATH = _BASE_DIR / "sutui_config.json"
_UPSTREAM_URLS_PATH = _BASE_DIR / "upstream_urls.json"
_SUTUI_DEFAULT_URL = "https://api.xskill.ai/api/v3/mcp-http"


def _read_sutui_config() -> dict:
    if not _SUTUI_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_SUTUI_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_sutui_config(data: dict):
    _SUTUI_CONFIG_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _read_upstream_urls() -> dict:
    if not _UPSTREAM_URLS_PATH.exists():
        return {}
    try:
        return json.loads(_UPSTREAM_URLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_upstream_urls(data: dict):
    _UPSTREAM_URLS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


@router.get("/api/sutui/config", summary="读取速推配置")
def get_sutui_config(current_user: _ServerUser = Depends(get_current_user_for_local)):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    urls = _read_upstream_urls()
    url = urls.get("sutui", _SUTUI_DEFAULT_URL)
    if edition == "online":
        token = (getattr(current_user, "sutui_token", None) or "").strip()
        return {"token": _mask_key(token) if token else "", "has_token": bool(token), "url": url, "edition": "online"}
    cfg = _read_sutui_config()
    token = cfg.get("token", "")
    return {
        "token": _mask_key(token) if token else "",
        "has_token": bool(token),
        "url": url,
    }


class UpdateSutuiConfig(BaseModel):
    token: Optional[str] = None
    url: Optional[str] = None


@router.post("/api/sutui/config", summary="保存速推配置（本地）")
def update_sutui_config(
    body: UpdateSutuiConfig,
    current_user: _ServerUser = Depends(get_current_user_for_local),
):
    from ..core.config import settings
    edition = (getattr(settings, "lobster_edition", None) or "online").strip().lower()
    if edition == "online":
        if body.token is not None:
            raise HTTPException(400, detail="在线版 Token 由速推登录提供，无需在此配置")
    cfg = _read_sutui_config()
    if body.token is not None and edition != "online":
        cfg["token"] = body.token.strip()
    _write_sutui_config(cfg)

    if body.url is not None and body.url.strip():
        urls = _read_upstream_urls()
        urls["sutui"] = body.url.strip()
        _write_upstream_urls(urls)
    elif not _read_upstream_urls().get("sutui"):
        urls = _read_upstream_urls()
        urls["sutui"] = _SUTUI_DEFAULT_URL
        _write_upstream_urls(urls)

    return {"ok": True, "message": "速推配置已保存"}


@router.get("/api/sutui/balance", summary="速推余额（代理到认证中心）")
async def get_sutui_balance(request: Request):
    base = _auth_server_base()
    token = request.headers.get("Authorization") or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/api/sutui/balance", headers={"Authorization": token})
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


# --------------- 速推模型与定价（代理到认证中心，与预扣/扣费同源） ---------------

@router.get("/api/sutui/models", summary="速推模型与定价（代理到认证中心）")
async def get_sutui_models(request: Request):
    base = _auth_server_base()
    token = request.headers.get("Authorization") or ""
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.get(f"{base}/api/sutui/models", headers={"Authorization": token})
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


# --------------- 速推充值（对接速推真实接口：get_pay_info_list / create_wx_order_info）---------------

_XSKILL_RECHARGE_URL = "https://www.xskill.ai/#/cn-recharge"
_CUSTOM_CONFIGS_FILE = _BASE_DIR / "custom_configs.json"


def _default_recharge_shops():
    """默认充值档位（当 get_pay_info_list 失败时使用）。"""
    return [
        {"shop_id": 0, "money_yuan": 100, "title": "100 元"},
        {"shop_id": 0, "money_yuan": 500, "title": "500 元"},
        {"shop_id": 0, "money_yuan": 1000, "title": "1000 元"},
    ]


def _get_custom_recharge_tiers() -> Optional[list]:
    """从 custom_configs.json 读取 RECHARGE_TIERS，用于自定义展示的档位、顺序和文案。
    格式: configs.RECHARGE_TIERS.shops = [ { \"shop_id\": 73, \"label\": \"1000元 推荐\", \"money_yuan\": 1000 }, ... ]。
    注意：实际支付金额由速推侧 shop_id 决定，label/money_yuan 仅用于展示；shop_id 需与速推商品一致。"""
    if not _CUSTOM_CONFIGS_FILE.exists():
        return None
    try:
        data = json.loads(_CUSTOM_CONFIGS_FILE.read_text(encoding="utf-8"))
        cfg = (data.get("configs") or {}).get("RECHARGE_TIERS")
        if not isinstance(cfg, dict):
            return None
        shops = cfg.get("shops")
        if not isinstance(shops, list) or not shops:
            return None
        out = []
        for s in shops:
            if not isinstance(s, dict):
                continue
            sid = s.get("shop_id")
            if sid is None:
                continue
            label = (s.get("label") or s.get("title") or "").strip() or f"{s.get('money_yuan', 0)} 元"
            money_yuan = s.get("money_yuan")
            if money_yuan is None:
                money_yuan = s.get("money")
                if isinstance(money_yuan, (int, float)) and money_yuan > 100:
                    money_yuan = money_yuan / 1000.0
            out.append({"shop_id": int(sid), "title": label, "money_yuan": float(money_yuan) if money_yuan is not None else 0, "tag": s.get("tag") or ""})
        return out if out else None
    except Exception as e:
        logger.debug("RECHARGE_TIERS read failed: %s", e)
        return None


@router.get("/api/sutui/recharge-options", summary="充值选项（代理到认证中心）")
async def get_sutui_recharge_options(request: Request):
    base = _auth_server_base()
    token = request.headers.get("Authorization") or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{base}/api/sutui/recharge-options", headers={"Authorization": token})
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")


class RechargeCreateBody(BaseModel):
    shop_id: int
    amount_yuan: Optional[float] = None


def _auth_server_base() -> str:
    base = (getattr(settings, "auth_server_base", None) or "").strip().rstrip("/")
    if not base:
        raise HTTPException(status_code=503, detail="未配置 AUTH_SERVER_BASE")
    return base


@router.post("/api/sutui/recharge-create", summary="创建充值订单（代理到认证中心）")
async def create_sutui_recharge(body: RechargeCreateBody, request: Request):
    base = _auth_server_base()
    token = request.headers.get("Authorization") or ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{base}/api/sutui/recharge-create",
            json=body.model_dump(),
            headers={"Authorization": token, "Content-Type": "application/json"},
        )
    from fastapi.responses import Response
    return Response(content=r.content, status_code=r.status_code, media_type="application/json")

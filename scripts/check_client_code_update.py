#!/usr/bin/env python3
"""
启动前在线客户端代码热更新（纯代码 zip，不含 python/nodejs/deps 等大依赖）。

- 仅在 .env 配置 CLIENT_CODE_MANIFEST_URL（HTTPS）时拉取 manifest。
- 本地版本：CLIENT_CODE_VERSION.json 的 build（整数）与 version（语义版本，默认 1.0.0）。
- 满足任一即更新：① 服务端 build 更大；② build 相同且 manifest.version 高于本地（如 1.0.0 → 1.0.1，便于只发「小版本」包）。
- 下载 bundle_url，校验 sha256 后，对 manifest.paths 所列路径做「整路径覆盖」
  （目录则先删再拷，文件则覆盖）；绝不触碰 python/、deps/、browser_chromium/、nodejs 可执行文件等。
- openclaw/：覆盖前尽量保留本地 openclaw/workspace 与 openclaw/.env（若存在）；覆盖后把 zip 内
  openclaw/workspace/LOBSTER_CHAT_POLICY_*.md 合并进保留后的 workspace（避免 OTA 丢策略导致 /chat 不调 MCP）。

禁止静默伪装成功：校验失败或解压失败时不改本地代码。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import ssl
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


def _ssl_context(*, allow_unverified: bool = False) -> ssl.SSLContext:
    """构建 SSL context：优先 certifi → 系统 CA → 不验证（兜底）。"""
    if allow_unverified:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        ctx = ssl.create_default_context()
        if ctx.get_ca_certs():
            return ctx
    except Exception:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "CLIENT_CODE_VERSION.json"
# 供纯静态启动（serve_online_client）读取，与 CLIENT_CODE_VERSION.json 同步
STATIC_CLIENT_VERSION_FILE = ROOT / "static" / "client_version.json"
DEFAULT_CLIENT_SEMVER = "1.0.0"

# 与 pack_code.sh 思路一致：仅代码与脚本，无嵌入式运行时与 wheel
DEFAULT_PATHS: tuple[str, ...] = (
    "CLIENT_CODE_VERSION.json",
    "backend",
    "mcp",
    "static",
    "scripts",
    "publisher",
    "skills",
    "skill_registry.json",
    "upstream_urls.json",
    "openclaw",
    "requirements.txt",
    ".env.example",
    ".env",
    "install.bat",
    "start.bat",
    "start_online.bat",
    "start_headless.bat",
    "run_backend.bat",
    "run_mcp.bat",
    "nodejs/package.json",
    "nodejs/package-lock.json",
    "nodejs/ensure-npm-cli.mjs",
    "nodejs/run-npm.mjs",
    "nodejs/.gitignore",
)

# 可选：整包 node 依赖（体积大）；一般发 OTA 仅用 DEFAULT_PATHS，目标机点授权在线安装即可
DEFAULT_PATHS_WITH_NODEJS_DEPS: tuple[str, ...] = DEFAULT_PATHS + (
    "nodejs/.openclaw/npm",
    "nodejs/node_modules",
)

BLOCKED_PREFIXES = (
    "python/",
    "python\\",
    "deps/",
    "deps\\",
    "browser_chromium/",
    "browser_chromium\\",
    ".git/",
    ".git\\",
    "nodejs/node.exe",
    "nodejs/node",
)
ALLOWED_NODEJS_EXACT = frozenset(
    {
        "nodejs/package.json",
        "nodejs/package-lock.json",
        "nodejs/ensure-npm-cli.mjs",
        "nodejs/run-npm.mjs",
        "nodejs/.gitignore",
    }
)
# 覆盖整树：另一机解压后 OpenClaw / 微信 / npm spawn 即就绪（不含 node.exe）
ALLOWED_NODEJS_TREE_PREFIXES: tuple[str, ...] = (
    "nodejs/node_modules",
    "nodejs/.openclaw/npm",
)

# 与 backend chat 读取路径一致；OTA 宜随包更新，安装机保留其余 workspace 文件
_OPENCLAW_POLICY_FILENAMES = ("LOBSTER_CHAT_POLICY_INTRO.md", "LOBSTER_CHAT_POLICY_TOOLS.md")


def _load_dotenv_simple(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def _local_build() -> int:
    if not VERSION_FILE.is_file():
        return 0
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        b = data.get("build")
        return int(b) if b is not None else 0
    except Exception:
        return 0


def _local_semver() -> str:
    if not VERSION_FILE.is_file():
        return DEFAULT_CLIENT_SEMVER
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        v = str(data.get("version", "") or "").strip()
        return v if v else DEFAULT_CLIENT_SEMVER
    except Exception:
        return DEFAULT_CLIENT_SEMVER


def _semver_is_newer(remote: str, local: str) -> bool:
    """manifest 的 version 是否严格高于本机（支持 1.0.1 / v1.2.3）。"""
    r = (remote or "").strip().lstrip("vV")
    l = (local or "").strip().lstrip("vV")
    if not r or not l:
        return False
    if r == l:
        return False
    try:
        from packaging.version import Version

        return Version(r) > Version(l)
    except Exception:
        # 无 packaging 或非常规串：按数字段比较
        def _parts(x: str) -> list[int]:
            out: list[int] = []
            for seg in x.split("."):
                n = ""
                for c in seg:
                    if c.isdigit():
                        n += c
                    else:
                        break
                out.append(int(n) if n else 0)
            return out or [0]

        rp, lp = _parts(r), _parts(l)
        ln = max(len(rp), len(lp))
        rp.extend([0] * (ln - len(rp)))
        lp.extend([0] * (ln - len(lp)))
        return tuple(rp) > tuple(lp)


def _save_local_build(build: int, version_from_manifest: str | None = None) -> None:
    prev: dict = {}
    if VERSION_FILE.is_file():
        try:
            prev = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
            if not isinstance(prev, dict):
                prev = {}
        except Exception:
            prev = {}
    prev["build"] = int(build)
    applied = datetime.datetime.utcnow().isoformat() + "Z"
    prev["applied_at"] = applied
    mv = (version_from_manifest or "").strip()
    if mv:
        prev["version"] = mv
    else:
        ex = str(prev.get("version", "")).strip()
        prev["version"] = ex if ex else DEFAULT_CLIENT_SEMVER
    semver = str(prev.get("version", "") or DEFAULT_CLIENT_SEMVER).strip() or DEFAULT_CLIENT_SEMVER
    prev["version"] = semver
    VERSION_FILE.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        STATIC_CLIENT_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATIC_CLIENT_VERSION_FILE.write_text(
            json.dumps(
                {"version": semver, "build": int(build), "applied_at": applied},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _urlopen_with_fallback(req: urllib.request.Request, timeout: int) -> bytes:
    """先用正常 SSL 验证；若证书校验失败则降级为不验证模式重试。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e).upper():
            print(f"[code] [WARN] SSL 证书验证失败，降级为不验证模式: {e}", flush=True)
            req2 = urllib.request.Request(req.full_url, headers=dict(req.headers))
            with urllib.request.urlopen(req2, timeout=timeout, context=_ssl_context(allow_unverified=True)) as resp:
                return resp.read()
        raise


def _fetch_json(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "LobsterClientCode/1.0"})
    raw = _urlopen_with_fallback(req, timeout)
    return json.loads(raw.decode("utf-8"))


def _download_file(url: str, dest: Path, timeout: int = 300) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "LobsterClientCode/1.0"})
    dest.write_bytes(_urlopen_with_fallback(req, timeout))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_rel(name: str) -> str:
    return name.strip().strip("/").replace("\\", "/")


def _path_allowed(rel: str) -> bool:
    r = _norm_rel(rel)
    if not r or ".." in r.split("/"):
        return False
    rl = r.lower()
    # 根 .env 随 OTA 覆盖；禁止误打包隐藏目录 .env/...
    if rl.startswith(".env/"):
        return False
    if rl == "python" or rl.startswith("python/"):
        return False
    if rl == "deps" or rl.startswith("deps/"):
        return False
    if rl == "browser_chromium" or rl.startswith("browser_chromium/"):
        return False
    if rl == ".git" or rl.startswith(".git/"):
        return False
    if rl.startswith("nodejs/"):
        if r in ALLOWED_NODEJS_EXACT:
            return True
        for pref in ALLOWED_NODEJS_TREE_PREFIXES:
            if r == pref or r.startswith(pref + "/"):
                return True
        return False
    for bad in BLOCKED_PREFIXES:
        if rl.startswith(bad.lower().replace("\\", "/")):
            return False
    return True


def _zip_inner_root(extract_root: Path) -> Path:
    """zip 根下只有一层 lobster_online/ 等时，进入该子目录再取 paths。"""
    inner = extract_root
    if (inner / "backend").is_dir():
        return inner
    subdirs = [p for p in inner.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and (subdirs[0] / "backend").is_dir():
        return subdirs[0]
    return inner


def _merge_openclaw_policies_from_bundle(bundle_openclaw: Path, target_openclaw: Path) -> None:
    """把包内聊天策略 Markdown 覆盖写入本机 workspace（本机无 workspace 时创建）。"""
    src_ws = bundle_openclaw / "workspace"
    dst_ws = target_openclaw / "workspace"
    for name in _OPENCLAW_POLICY_FILENAMES:
        sf = src_ws / name
        if not sf.is_file():
            continue
        dst_ws.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sf, dst_ws / name)


def _apply_openclaw_with_preserve(src: Path, dst: Path) -> None:
    """用 src 覆盖 dst 目录，保留本地 workspace/ 与 .env（若 zip 未提供）；再合并包内 LOBSTER_CHAT_POLICY_*。"""
    tmp_ws = None
    tmp_env = None
    if dst.is_dir():
        ws = dst / "workspace"
        # 始终保留已有 workspace，避免 OTA 仅含少量 workspace 文件时清空用户 OpenClaw 数据
        if ws.is_dir():
            tmp_ws = Path(tempfile.mkdtemp(prefix="lobster_oc_ws_"))
            shutil.move(str(ws), str(tmp_ws / "workspace"))
        env_f = dst / ".env"
        if env_f.is_file() and not (src / ".env").exists():
            tmp_env = Path(tempfile.mkdtemp(prefix="lobster_oc_env_")) / ".env"
            shutil.copy2(env_f, tmp_env)
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.copytree(src, dst)
    if tmp_ws and (tmp_ws / "workspace").is_dir():
        target = dst / "workspace"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(tmp_ws / "workspace"), str(target))
        shutil.rmtree(tmp_ws, ignore_errors=True)
    _merge_openclaw_policies_from_bundle(src, dst)
    if tmp_env and tmp_env.is_file():
        shutil.copy2(tmp_env, dst / ".env")
        shutil.rmtree(tmp_env.parent, ignore_errors=True)


def _apply_path(src: Path, dst: Path) -> None:
    rel = _norm_rel(str(dst.relative_to(ROOT)))
    if rel == "openclaw" and src.is_dir():
        _apply_openclaw_with_preserve(src, dst)
        return
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        shutil.copy2(src, dst)
    else:
        shutil.copytree(src, dst)


def main() -> int:
    env = _load_dotenv_simple(ROOT / ".env")
    env.update({k: v for k, v in os.environ.items() if k.startswith("CLIENT_CODE_")})

    manifest_url = (env.get("CLIENT_CODE_MANIFEST_URL") or "").strip()
    if not manifest_url:
        return 0

    if not manifest_url.lower().startswith(("https://", "http://")):
        print("[code] [ERR] CLIENT_CODE_MANIFEST_URL 格式无效。", flush=True)
        return 0

    local = _local_build()
    local_ver = _local_semver()
    try:
        manifest = _fetch_json(manifest_url)
    except urllib.error.URLError as e:
        print(f"[code] [WARN] 无法拉取 manifest，使用本地代码: {e}", flush=True)
        return 0
    except Exception as e:
        print(f"[code] [WARN] manifest 解析失败，跳过更新: {e}", flush=True)
        return 0

    try:
        remote_build = int(manifest.get("build", 0))
    except (TypeError, ValueError):
        print("[code] [WARN] manifest 缺少合法整数 build，跳过更新。", flush=True)
        return 0

    bundle_url = (manifest.get("bundle_url") or "").strip()
    expect_sha = (manifest.get("sha256") or "").strip().lower()
    paths = manifest.get("paths")
    if not isinstance(paths, list) or not paths:
        paths = list(DEFAULT_PATHS)

    remote_ver = str(manifest.get("version") or "").strip()
    need_update = remote_build > local
    if not need_update and remote_build == local and remote_ver and _semver_is_newer(remote_ver, local_ver):
        need_update = True
    if not need_update:
        print(f"[code] 本地代码包已是最新 (build={local}, version={local_ver})。", flush=True)
        return 0

    if not bundle_url.lower().startswith(("https://", "http://")):
        print("[code] [ERR] manifest.bundle_url 格式无效，未应用更新。", flush=True)
        return 0
    if not expect_sha or len(expect_sha) != 64:
        print("[code] [ERR] manifest.sha256 无效，未应用更新。", flush=True)
        return 0

    for p in paths:
        rel = _norm_rel(str(p))
        if not _path_allowed(rel):
            print(f"[code] [ERR] 禁止通过热更新覆盖的路径: {rel}", flush=True)
            return 0

    if remote_build > local:
        print(f"[code] 发现新版本 build={remote_build}（本地 build={local}），正在下载…", flush=True)
    else:
        print(
            f"[code] 发现新版本 version={remote_ver}（本地 {local_ver}，build 均为 {local}），正在下载…",
            flush=True,
        )

    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        zpath = tdir / "bundle.zip"
        try:
            _download_file(bundle_url, zpath)
        except Exception as e:
            print(f"[code] [WARN] 下载失败，不修改本地文件: {e}", flush=True)
            return 0

        got = _sha256_file(zpath)
        if got.lower() != expect_sha:
            print(
                f"[code] [ERR] SHA256 不匹配（期望 {expect_sha[:16]}… 实际 {got[:16]}…），不应用更新。",
                flush=True,
            )
            return 0

        extract_root = tdir / "extracted"
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zpath, "r") as zf:
                zf.extractall(extract_root)
        except zipfile.BadZipFile as e:
            print(f"[code] [ERR] zip 损坏: {e}", flush=True)
            return 0

        inner = _zip_inner_root(extract_root)
        applied: list[str] = []
        for p in paths:
            rel = _norm_rel(str(p))
            if not rel:
                continue
            src = inner / rel.replace("/", os.sep)
            if not src.exists():
                print(f"[code] [WARN] 包内无路径 {rel}，跳过。", flush=True)
                continue
            dst = ROOT / rel.replace("/", os.sep)
            try:
                _apply_path(src, dst)
                applied.append(rel)
            except OSError as e:
                print(f"[code] [ERR] 写入 {rel} 失败: {e}，中止（未更新版本号）。", flush=True)
                return 0

        if not applied:
            print("[code] [ERR] 包内未找到任何可覆盖路径，未写入版本号。", flush=True)
            return 0

    mver = manifest.get("version")
    mver_s = str(mver).strip() if mver is not None else ""
    _save_local_build(remote_build, mver_s or None)
    print(f"[code] 已覆盖更新 build={remote_build}，路径: {', '.join(applied)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
打「纯代码」OTA zip（与 scripts/check_client_code_update.py 的 DEFAULT_PATHS 一致）。
默认已含 nodejs/package*.json 与 ensure-npm-cli.mjs、run-npm.mjs、.gitignore（不含 node_modules），
另一机覆盖后需保证安装包自带 node.exe，点微信授权即可在线拉齐依赖。
可选 --with-nodejs-deps：额外打入 nodejs/node_modules 与 .openclaw/npm（离线大块，一般不用于 OTA）。

不含 python/、deps/、browser_chromium/、nodejs 可执行文件；openclaw 不含 workspace* 整目录（避免 .git/ 与用户数据），
但强制纳入「主对话」必需的 openclaw/workspace/LOBSTER_CHAT_POLICY_*.md（与 backend chat 单一事实来源一致）。
logs/.env 仍不打包。
根目录 .env 与打包机一致写入 zip（若缺失则 WARN 跳过）。
默认产物与 pack_slim_zip 一致：写在 lobster_online 的上一级目录（例如 d:\\lobster_online → d:\\）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import zipfile
from pathlib import Path

# 与 check_client_code_update.DEFAULT_PATHS 保持一致
OTA_PATHS: tuple[str, ...] = (
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
    "nodejs/node_modules/@tencent-weixin/openclaw-weixin",
)

# 与 check_client_code_update.DEFAULT_PATHS_WITH_NODEJS_DEPS 一致（仅在对等清单外加整树时用）
OTA_PATHS_WITH_NODEJS_DEPS: tuple[str, ...] = OTA_PATHS + (
    "nodejs/.openclaw/npm",
    "nodejs/node_modules",
)

SKIP_DIR_NAMES = {"__pycache__", ".git"}

# 本地调试/抓页面临时目录，非交付代码（曾占 OTA 包约 16MB+）
# skills 下各技能的 runs/job_runs 为执行缓存（音视频等），不应随 OTA 分发（否则单包可膨胀 200MB+）
OTA_SKIP_REL_PREFIXES: tuple[str, ...] = (
    "scripts/_probe",
)

_OTA_SKIP_SKILLS_DIRS = {"runs", "job_runs", "output", "cache"}

# /chat 从该两文件读 system；此前 OTA 排除整个 workspace 会导致覆盖安装后「无工具提示」、模型不调 MCP
_OTA_OPENCLAW_POLICY_RELS: tuple[str, ...] = (
    "openclaw/workspace/LOBSTER_CHAT_POLICY_INTRO.md",
    "openclaw/workspace/LOBSTER_CHAT_POLICY_TOOLS.md",
)


def _norm(p: str) -> str:
    return p.replace("\\", "/")


def _skip_file(rel: str) -> bool:
    r = _norm(rel).lower()
    if r.endswith(".pyc"):
        return True
    parts = r.split("/")
    if "__pycache__" in parts:
        return True
    nr = _norm(rel)
    if any(nr.startswith(p) for p in OTA_SKIP_REL_PREFIXES):
        return True
    if len(parts) >= 3 and parts[0] == "skills" and parts[2] in _OTA_SKIP_SKILLS_DIRS:
        return True
    return False


def _add_tree(zf: zipfile.ZipFile, root: Path, rel_dir: str) -> None:
    base = root / rel_dir.replace("/", os.sep)
    if not base.exists():
        return
    if base.is_file():
        if _skip_file(rel_dir):
            return
        zf.write(base, rel_dir)
        return
    for dirpath, dirnames, filenames in os.walk(base):
        rel_here = _norm(os.path.relpath(dirpath, str(root)))
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES
            and not any(_norm(os.path.join(rel_here, d)).startswith(p) for p in OTA_SKIP_REL_PREFIXES)
        ]
        for name in filenames:
            full = Path(dirpath) / name
            rel = _norm(os.path.relpath(str(full), str(root)))
            if _skip_file(rel):
                continue
            try:
                if full.is_symlink() and not full.exists():
                    print(f"[WARN] 跳过断链: {rel}")
                    continue
                zf.write(full, rel)
            except OSError as e:
                print(f"[WARN] 跳过无法读取的路径: {rel} ({e})")


def _add_openclaw(zf: zipfile.ZipFile, root: Path) -> None:
    base = root / "openclaw"
    if not base.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIR_NAMES
            and d != "workspace"
            and not d.startswith("workspace-")
            and d != "logs"
            and d != "browser"
        ]
        for name in filenames:
            if name == ".env" or name.endswith(".bak") or ".bak." in name:
                continue
            full = Path(dirpath) / name
            rel = _norm(os.path.relpath(str(full), str(root)))
            if _skip_file(rel):
                continue
            zf.write(full, rel)

    for rel in _OTA_OPENCLAW_POLICY_RELS:
        src_pol = root / rel.replace("/", os.sep)
        if not src_pol.is_file():
            print(f"[WARN] 缺失聊天策略（请从仓库补齐）: {rel}")
            continue
        zf.write(src_pol, rel)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pack client-code OTA zip")
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="lobster_online 根目录",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="输出 .zip（默认：lobster_online 上一级目录，与 scripts/pack_slim_zip.py 一致）",
    )
    ap.add_argument(
        "--with-nodejs-deps",
        action="store_true",
        help="打入 nodejs/node_modules 与 .openclaw/npm（需本机已 npm install + ensure-npm-cli 跑通）",
    )
    args = ap.parse_args()
    root: Path = args.root.resolve()
    parent = root.parent
    paths_tuple: tuple[str, ...] = OTA_PATHS_WITH_NODEJS_DEPS if args.with_nodejs_deps else OTA_PATHS
    if args.out is None:
        ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = "_with_nodejs" if args.with_nodejs_deps else ""
        out = (parent / f"lobster_online_client_code_ota{suffix}_{ts}.zip").resolve()
    else:
        out = args.out.resolve()
    if not root.is_dir():
        print(f"[ERR] root 不是目录: {root}")
        return 1
    if args.with_nodejs_deps:
        nm = root / "nodejs" / "node_modules"
        if not nm.is_dir():
            print(f"[ERR] --with-nodejs-deps 需要已存在的 {nm}")
            return 1
        oc = nm / "openclaw" / "openclaw.mjs"
        if not oc.is_file():
            print(f"[WARN] 未找到 {oc}，目标机可能仍需在线安装依赖")
        cache = root / "nodejs" / ".openclaw" / "npm" / "bin" / "npm-cli.js"
        if not cache.is_file():
            print(
                "[WARN] 缺少 nodejs/.openclaw/npm（可先在本机执行: cd nodejs ; node ensure-npm-cli.mjs），"
                "否则包内无离线 npm 缓存",
            )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in paths_tuple:
            rel = p.replace("\\", "/")
            src = root / rel.replace("/", os.sep)
            if not src.exists():
                print(f"[WARN] 缺失，跳过: {rel}")
                continue
            if rel == "openclaw":
                _add_openclaw(zf, root)
            else:
                _add_tree(zf, root, rel)

    h = hashlib.sha256()
    with out.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    digest = h.hexdigest()
    print(out)
    print(f"sha256={digest}")
    ver_path = root / "CLIENT_CODE_VERSION.json"
    _mbuild, _mver = 0, "1.0.0"
    if ver_path.is_file():
        try:
            vd = json.loads(ver_path.read_text(encoding="utf-8"))
            _mbuild = int(vd.get("build", 0))
            _mver = str(vd.get("version", _mver) or _mver).strip() or "1.0.0"
        except Exception:
            pass
    snippet = {
        "build": _mbuild,
        "version": _mver,
        "bundle_url": "https://YOUR_CDN/lobster_client_ota.zip",
        "sha256": digest,
        "paths": list(paths_tuple),
    }
    hint = (
        "paths 与 DEFAULT_PATHS_WITH_NODEJS_DEPS 对齐（含整包 node 依赖）"
        if args.with_nodejs_deps
        else "paths 与 check_client_code_update.DEFAULT_PATHS 对齐（无 node_modules，点授权在线装）"
    )
    print(f"\n--- manifest 片段（{hint}）---")
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

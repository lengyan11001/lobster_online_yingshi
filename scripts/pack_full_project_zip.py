#!/usr/bin/env python3
"""
Create the same archive as pack_full_project.sh (zip -r ... -x ...) without external zip.exe.
Paths inside the zip: <proj>/<relative path from proj root>.
"""
from __future__ import annotations

import fnmatch
import os
import sys
import zipfile
from pathlib import Path


def should_exclude(proj: str, rel_posix: str) -> bool:
    """rel_posix: relative path under project root, forward slashes, no leading slash."""
    if not rel_posix:
        return True
    parts = rel_posix.split("/")
    if ".git" in parts:
        return True
    if any("__pycache__" in p for p in parts):
        return True
    if rel_posix.endswith(".DS_Store"):
        return True

    root_name = parts[0] if len(parts) == 1 else None

    if root_name and root_name.endswith(".pyc"):
        return True
    if root_name and root_name.endswith(".db"):
        return True
    if rel_posix == ".env":
        return True
    if rel_posix == "openclaw/.env":
        return True
    if rel_posix.startswith("openclaw/workspace/"):
        return True
    if rel_posix.startswith("browser_data/"):
        return True
    if rel_posix.startswith("assets/"):
        return True
    if rel_posix == "sutui_config.json":
        return True
    if rel_posix == "pack_bundle.env":
        return True
    if rel_posix.startswith("logs/"):
        return True
    if rel_posix.startswith("docs/"):
        return True

    if rel_posix.startswith("openclaw/browser/"):
        return True
    _skill_runtime = {"runs", "job_runs", "output", "cache"}
    if len(parts) >= 3 and parts[0] == "skills" and parts[2] in _skill_runtime:
        return True
    if root_name and root_name in ("backend.log", "backend_err.log", "mcp.log"):
        return True

    if root_name:
        if fnmatch.fnmatch(root_name, f"{proj}_*.zip"):
            return True
        if fnmatch.fnmatch(root_name, "*.tar.gz"):
            return True
        if root_name == "explore_douyin.py":
            return True
        if fnmatch.fnmatch(root_name, "douyin_*.png") or fnmatch.fnmatch(root_name, "douyin_*.json"):
            return True
        if fnmatch.fnmatch(root_name, "media_edit_skill_bundle_*.zip"):
            return True
        if fnmatch.fnmatch(root_name, "lobster_online_code_*.zip"):
            return True
        if fnmatch.fnmatch(root_name, "lobster_code_*.zip"):
            return True
        if fnmatch.fnmatch(root_name, "xskill_*.json") or fnmatch.fnmatch(root_name, "xskill_*.jsonl"):
            return True
        if root_name == "openclaw.log":
            return True
        if root_name == "installed_packages.json":
            return True
        if root_name == "mcp_registry_cache.json":
            return True
        if root_name == "test_mcp.py":
            return True
        if fnmatch.fnmatch(root_name, "pack_*.sh"):
            return True
        if root_name == "pack_full_project.sh":
            return True
        if root_name == "build_package.sh":
            return True
        if fnmatch.fnmatch(root_name, "使用说明*.txt"):
            return True
        if root_name == "README-一键使用.txt":
            return True
        if root_name == "README.md":
            return True
        if fnmatch.fnmatch(root_name, "单机版启动脚本*.txt"):
            return True
        if root_name == "修复MCP服务未就绪.md":
            return True
        if root_name == "诊断MCP连接问题.md":
            return True

    if rel_posix == "static/桌面图标说明.txt":
        return True

    script_excludes = {
        "scripts/ensure_full_pack_deps.sh",
        "scripts/ensure_pack_deps.sh",
        "scripts/pack_media_edit_skill.sh",
        "scripts/build_result_package.sh",
        "scripts/sync_deps_for_pack.sh",
        "scripts/report_pack_gaps.py",
    }
    if rel_posix in script_excludes:
        return True

    if rel_posix.startswith("nodejs/node_modules/thread-stream/test/") and rel_posix.endswith(".zip"):
        return True

    return False


def main() -> int:
    if len(sys.argv) != 4:
        print("Usage: pack_full_project_zip.py <parent_dir> <proj_dirname> <out_zip_path>", file=sys.stderr)
        return 2
    parent = Path(sys.argv[1]).resolve()
    proj = sys.argv[2]
    out_zip = Path(sys.argv[3]).resolve()
    proj_root = parent / proj
    if not proj_root.is_dir():
        print(f"[ERR] Not a directory: {proj_root}", file=sys.stderr)
        return 1

    out_zip.parent.mkdir(parents=True, exist_ok=True)
    if out_zip.exists():
        out_zip.unlink()

    count = 0
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(proj_root, followlinks=False):
            # Prune .git early
            if ".git" in dirnames:
                dirnames.remove(".git")
            for name in filenames:
                full = Path(dirpath) / name
                try:
                    rel = full.relative_to(proj_root)
                except ValueError:
                    continue
                rel_posix = rel.as_posix()
                if should_exclude(proj, rel_posix):
                    continue
                arcname = f"{proj}/{rel_posix}"
                zf.write(full, arcname, compress_type=zipfile.ZIP_DEFLATED)
                count += 1

    print(f"pack_full_project_zip.py: added {count} files -> {out_zip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env bash
# 在 Git Bash 中执行：修正 PATH 使 python3 指向本机 Python 3.11+（含 packaging），再打指定品牌完整包。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$ROOT/.pack_python_shim:$PATH"
export LOBSTER_BRAND_MARK="${LOBSTER_BRAND_MARK:-yingshi}"
cd "$ROOT"
exec bash scripts/build_result_package.sh

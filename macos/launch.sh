#!/bin/bash
# 小李破甲 · macOS 启动层
#
# 职责：把脚本目录绝对化 → 按固定顺序发现合格的 Python 3.11+ → exec 到 CLI。
# 不做：自动下载运行时、不使用 sudo、不替换系统 Python、不改动任何配置内容。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
CLI="$HERE/xiaoli.py"

if [ ! -f "$CLI" ]; then
  echo "[小李破甲] 找不到 $CLI，请完整解压工具目录后再运行。" >&2
  exit 2
fi

probe() {
  local candidate="$1"
  [ -n "$candidate" ] || return 1
  if [ ! -x "$candidate" ] && ! command -v "$candidate" >/dev/null 2>&1; then
    return 1
  fi
  "$candidate" -I -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidate
  if [ -n "${XIAOLI_PYTHON:-}" ]; then
    if probe "$XIAOLI_PYTHON"; then
      printf '%s\n' "$XIAOLI_PYTHON"
      return 0
    fi
    # 显式指定的解释器无效时直接失败，不悄悄回退到别的解释器。
    echo "[小李破甲] XIAOLI_PYTHON 指定的解释器不可用或低于 3.11：$XIAOLI_PYTHON" >&2
    return 1
  fi
  for candidate in python3.13 python3.12 python3.11 python3 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if probe "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "[小李破甲] 未找到 Python 3.11+；可安装 python.org 版 Python 3.11+，" >&2
  echo "          或用 XIAOLI_PYTHON=/绝对路径/python3 显式指定。" >&2
  exit 2
fi

# -I 隔离模式：不读用户 site-packages 与环境变量注入，保证只跑本工具自己的代码。
exec "$PY" -I "$CLI" "$@"

#!/bin/bash
# 小李破甲 · macOS 只读环境审计入口
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec /bin/bash "$HERE/macos/launch.sh" --audit "$@"

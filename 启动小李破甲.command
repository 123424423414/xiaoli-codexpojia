#!/bin/bash
# 小李破甲 · macOS 一键安装入口：只做自身目录绝对化，然后交给 macos/launch.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec /bin/bash "$HERE/macos/launch.sh" --install "$@"

# macOS 使用说明

## 前置条件

Python **3.11 或更高**。工具只用标准库，不需要 `pip install`。

推荐装 python.org 版（自带 `python3`），或者用你已有的 Homebrew Python：

```bash
python3 --version    # 需要 >= 3.11
```

## 入口

四个 `.command` 双击即可运行（首次可能需要右键 → 打开，或在“系统设置 → 隐私与安全性”里放行）：

| 文件 | 作用 |
| --- | --- |
| `启动小李破甲.command` | 安装/更新指令配置 |
| `卸载小李破甲.command` | 恢复本次安装前的引用 |
| `查看小李状态.command` | 只读查看安装状态 |
| `检查环境.command` | 只读脱敏环境审计 |

`.command` 只做一件事：把自身目录绝对化，然后 `exec` 给 `macos/launch.sh`。

终端等价写法：

```bash
/bin/bash "/你的解压路径/启动小李破甲.command" --no-open --json
/bin/bash "/你的解压路径/macos/launch.sh" --status --json
```

## 解释器发现顺序

`macos/launch.sh` 按下面的顺序找解释器，找到第一个合格的就用：

1. 环境变量 `XIAOLI_PYTHON`（**显式指定无效时直接报错退出，不静默回退**）
2. `python3.13` → `python3.12` → `python3.11` → 或 PATH 上的 `python3`
3. `/opt/homebrew/bin/python3`（Apple Silicon）
4. `/usr/local/bin/python3`（Intel / 手动安装）

探针一律用 `-I` 隔离模式运行 `import sys` 检查版本，输出全部丢弃；最终执行也是 `exec "$PY" -I xiaoli.py ...`，不读取用户 `site-packages` 与 `PYTHON*` 环境变量注入。

指定自己的解释器：

```bash
export XIAOLI_PYTHON=/usr/local/bin/python3.12
/bin/bash "/你的解压路径/启动小李破甲.command"
```

## 与 Windows 版 / 参考工具的差异

- 不会自动下载独立运行时，也不会使用 `sudo`、不替换系统 Python。找不到合格解释器时给出明确提示后退出（退出码 2）。
- 不做 Homebrew 自动安装。
- 恢复不会清理你自己的 Python，也不会留下运行时缓存（本工具不建缓存目录）。

## 配置目录

macOS 默认 `~/.codex`；可用 `--codex-home` 或环境变量 `CODEX_HOME` 指定。目录布局与 Windows 完全一致，见根目录 `README.md`。

## 限制

- **Mac 部分未做实机验证**：`.command` 与 `launch.sh` 只做过语法级检查与源码级推演，未在 Mac 上实际双击运行。
- 本工具的配置写入逻辑与平台无关，已在 Windows 上完成 125 项单元回归、59 项功能自检与
  20 项环境/稳定性检测；Mac 侧只差「解释器发现 + 双击运行」这一层未做实机验收。

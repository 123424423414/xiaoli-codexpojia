# 开源与发版流程

本文档说明把这个项目推到 GitHub 的完整步骤、开源前的检查清单，以及发版（Release）怎么做。

## 一、开源前检查清单

逐条确认后再 `git add`，因为**提交历史一旦推送就很难彻底清除**。

1. **个人信息**：`profile.json` 里的确认口令、`验证记录.txt` 里的本机用户名绝对路径。
   本仓库当前**按作者意愿保留**（口令与 QQ 号为作者本人内容），如需脱敏请改这两处并重跑 `python selfcheck.py`。
2. **凭据**：`grep -rniE "sk-[A-Za-z0-9]{8,}|bearer +[A-Za-z0-9._-]{10,}|api[_-]?key" .` 应当没有任何命中。
   本项目不读取、不存储任何 API 密钥；它只改 Codex 的 `model_instructions_file` 一行。
3. **绝对路径**：`grep -rn "C:\\\\Users" .` 除了示例应当为空。
4. **产物可复现**：
   ```powershell
   python build_exe.py --clean     # 重建「小李破甲安装器.exe」
   python build_package.py         # 重建 downloads/xiaoli-armor-v1.0.0.zip + SHA256SUMS
   python build_package.py --verify downloads/xiaoli-armor-v1.0.0.zip
   ```
5. **换行符**：`.gitattributes` 已锁定 `.cmd=CRLF` / `.sh=LF`。改动过脚本后请确认：
   ```powershell
   python selfcheck.py    # 其中的 E6/E8 会断言源码与产物里的换行都正确
   ```
6. **本地全绿**：`python -B -m unittest discover -s tests -t .`（133 项）、`python selfcheck.py`（59 项）、`python selfcheck_env.py`（19 项）。

## 二、首次创建仓库并推送

### 方式 A：用 GitHub CLI（推荐，一条命令建仓 + 推送）

```bash
gh auth login                       # 浏览器授权，只需做一次
git add .
git commit -m "小李破甲 v1.0.0：Codex 本地指令配置工具"
gh repo create xiaoli-armor --public --source=. --remote=origin --push
```

### 方式 B：手动

先在 GitHub 网页上新建一个空仓库（不要勾选 README / .gitignore / License，避免冲突），然后：

```bash
git config user.name  "你的名字"
git config user.email "你的邮箱"
git add .
git commit -m "小李破甲 v1.0.0：Codex 本地指令配置工具"
git remote add origin https://github.com/<你的用户名>/xiaoli-armor.git
git push -u origin main
```

> 仓库名建议 `xiaoli-armor`；仓库描述可写：
> 「Windows/macOS 上的 Codex 本地指令配置工具：一键注入、幂等重装、精确恢复、只读诊断，仅用标准库。」

## 三、发版（Release）

```bash
git tag -a v1.0.0 -m "小李破甲 v1.0.0"
git push origin v1.0.0
gh release create v1.0.0 \
  "downloads/xiaoli-armor-v1.0.0.zip" \
  "downloads/SHA256SUMS-xiaoli-v1.0.0.txt" \
  --title "小李破甲 v1.0.0" \
  --notes-file 验证记录.txt
```

Release 说明里建议保留三条边界声明（本项目一直在文档里写明）：

- 只做本地指令配置，不改 Codex 程序、账号、模型设置，不结束正在运行的 Codex；
- 不绕过服务端策略，提示词加载成功不等于模型一定照做；
- 审计只检查本地文件与环境变量，不检测真实流量、不判断代理是否解密。

## 四、CI

`.github/workflows/tests.yml` 会在 push / PR 时执行：

| 作业 | 平台 | 内容 |
| --- | --- | --- |
| `unit` | Windows / Linux / macOS × Python 3.11–3.13 | 133 项单元回归（零第三方依赖） |
| `selfcheck` | Windows | 59 项功能自检 + 19 项环境检测（含并发、锁、篡改、解压产物、诊断、崩溃残留） |

## 五、已知的仓库体积问题

`小李破甲安装器.exe` 约 9.6 MB、安装包约 9.5 MB。两者都提交进仓库是为了让用户点开就能下载免安装版（与原参考项目做法一致）。
如果不想让仓库背这两个二进制：把它们加进 `.gitignore`，改为只在 Release 附件里提供——但 README 里的 `downloads/...` 链接需要同步改成 Release 链接。

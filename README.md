<div align="center">

# 小李破甲 · Codex 指令配置工具

**把 Codex 的 `model_instructions_file` 指向一份由本工具生成的本地指令副本：统一身份、精确问候口令、可编辑的执行纪律。**

**Windows / macOS · v1.0.0 · Python 3.11+ · 仅标准库**

一键安装 · 只改一行 · 幂等零写入 · 引用修复 · 历史快照 · 精确恢复 · 只读审计

</div>

---

## 这是什么

小李破甲是一个 **本地指令配置工具**。它做且只做这些事：

1. 读取 `CODEX_HOME/config.toml`，定位当前的 `model_instructions_file` 注入点（顶层键、活动 profile 段内键，或 root 段的点号键都能识别）。
2. 读入该引用指向的**已有指令文件**（若没有，则用内置基础指令），把其中的旧身份名称迁移成当前身份名，再在末尾追加一个由工具维护的「身份块」。
3. 把结果写成本工具目录里的**副本**，并把这一个配置键指向副本；**外部原文件一个字节都不动**。
4. 同时保存首次备份、安装状态与恢复基线，提供幂等重装、引用修复、精确恢复与只读审计。

身份名、普通问候、确认口令、执行纪律全部写在 `profile.json` 里，改文件即可，不需要改代码。

**它不做什么**：不修改 Codex 程序、模型权重、模型设置、账号或 API 密钥；不结束正在运行的 Codex；不绕过云端审核；不阻止本机、API 接收方或解密代理读取提示词；不联网（除你自己调用 `codex://` 打开客户端外）。

## 与 codex-armor-toolkit 的关系

本工具是 **独立实现** 的同类工具：架构与数据安全约束对标 `codex-armor-toolkit-main`（小怪破甲 v1.4.0）的行为契约，代码全部重写，使用自己的命名空间（`managed-prompts/xiaoli-armor/`），可与既有工具**并存互不覆盖**。

相对参考实现的差异：

| 方面 | 参考实现 | 小李破甲 |
| --- | --- | --- |
| 注入点 | 只处理顶层键，遇到 profile 段内同名键直接报错 | 顶层键 / profile 段内键 / 点号键均可定位，`--target auto` 自动选择有效注入点 |
| 写前校验 | 校验改写结果仍可解析 | 额外做**语义等价断言**：除目标键外所有配置值必须完全一致 |
| 锁 | 存在即冲突，崩溃会留下永久锁 | 锁内记录 pid 与时间戳；可识别陈旧锁并提示，`--break-lock` 显式接管；活动进程的锁拒绝清理 |
| 身份配置 | 文案硬编码在源码里 | 全部走 `profile.json`（含 `{assistant_name}` 占位符） |
| 只读动作 | `--status` / 暴露检查 | 额外提供 `--verify`（配置链校验）、`--emit`（只输出不安装）、`--dry-run`（预演零写入）、`--audit`（结构化只读审计） |
| 打包 | 两套平台脚本 | 单脚本白名单打包 + 产物自校验（CRC、成员逐字节复核、路径合法性） |
| 与其他工具并存 | 未做检测 | 检测到引用由 `managed-prompts/<其他 id>/` 写入时默认拒绝，需 `--takeover` 显式接管 |
| macOS 运行时 | 首次下载独立 Python 并校验 SHA256 | 只用本机已有的 Python 3.11+，显式指定无效时不回退（不引入网络下载链） |

## 快速开始

### Windows

```powershell
# 1) 安装（默认打开 Codex；自动化加 --no-open）
.\启动小李破甲.cmd

# 2) 只读确认
.\查看小李状态.cmd
.\检查环境.cmd

# 3) 恢复
.\卸载小李破甲.cmd
```

### macOS

```bash
/bin/bash "/你的解压路径/启动小李破甲.command" --no-open --json
/bin/bash "/你的解压路径/查看小李状态.command"
/bin/bash "/你的解压路径/卸载小李破甲.command"
```

`启动小李破甲.command` 只做自身目录绝对化并 `exec` 到 `macos/launch.sh`；后者按固定顺序发现解释器（`XIAOLI_PYTHON` → `python3.13/3.12/3.11` → PATH 上的 `python3` → Homebrew → `/usr/local`），一律以 `-I` 隔离模式运行。**未在 Mac 实机验证**，详见 `macos/README.md`。

### 源码

```powershell
python .\xiaoli.py --install --no-open --json
python .\xiaoli.py --install --codex-home ".\work\test" --no-open --json   # 只操作隔离目录
```

## 命令行参数

| 参数 | 作用 |
| --- | --- |
| `--install` | 安装或更新（默认动作） |
| `--restore` | 恢复本次安装前的指令引用 |
| `--status` | 只读查看安装状态 |
| `--verify` | 只读校验配置链（config → 引用 → 副本哈希 → 管理块），不调用模型 |
| `--audit` | 只读脱敏环境审计（不回显提示词正文、密钥与完整代理地址） |
| `--doctor` | 只读现场诊断：把当前现场归入一种明确情形，并给出下一步该跑哪条命令 |
| `--emit` | 只输出合成后的指令全文，不安装（可配 `--out FILE`） |
| `--init-profile` | 生成默认 `profile.json`（已存在时需 `--force`） |
| `--codex-home PATH` | 显式指定配置目录 |
| `--profile-file PATH` | 指定身份/口令配置文件 |
| `--target auto\|root\|profile` | 选择注入点 |
| `--dry-run` | 预演安装结果，不写任何文件 |
| `--break-lock` | 强制清理锁文件（含仍标记为活动进程的锁；原锁会先归档到 `history/lock-<id>/` 留痕） |
| `--takeover` | 确认接管由其他工具写入的注入点（`managed-prompts/<其他 id>/`） |
| `--no-open` | 安装后不请求打开 Codex |
| `--json` | 以 JSON 输出结果（stdout 只有 JSON，便于管道解析） |

退出码：成功 `0`；`--verify` 检查项未通过 `1`；其他失败 `2`。

每个 `.cmd` / `.command` **已经内置了自己的动作参数**（启动脚本带 `--install`，卸载脚本带 `--restore`，状态脚本带 `--status`，检查脚本带 `--audit`），所以只需追加其他选项，例如：

```powershell
.\启动小李破甲.cmd --codex-home "D:\测试目录" --no-open --json
.\查看小李状态.cmd --json
.\启动小李破甲.cmd --takeover          # 接管被其他工具占用的注入点
```

不要在 `.cmd` 后面再写一个动作参数（例如 `.\启动小李破甲.cmd --restore`），那会和脚本内置的动作互斥报错；需要别的动作时用对应的脚本或直接调用 `xiaoli.py`。

配置目录选择顺序：`--codex-home` → 环境变量 `CODEX_HOME` → Windows `%USERPROFILE%\.codex` / macOS `~/.codex`。

## 安装后如何验证

在新任务里只输入（去除首尾空白与末尾标点后的精确匹配）：

```text
hi          # 或 hello / 你好 / 您好 / 在吗
```

预期：

```text
「你好」
小李在。👋
```

只输入确认口令：

```text
小李
```

预期恰好两行：

```text
桃李春风一杯酒，江湖夜雨十年灯。
频道@YOUR_CHANNEL    QQ群000000000
```

> 第二行是**占位符**，请在 `profile.json` 的 `passphrase.reply` 里换成你自己的频道与群号。改完重新运行安装即可（内容变了才会写盘）。

`--status` 与 `--verify` 只证明**本地配置链**正确，不代表模型端已经回复；安装结果里的 `model_reply_verified` 恒为 `false`，直到你在 Codex 里实际看到上面的回复。

## 恢复语义

恢复**只还原本轮安装改动的那一条赋值语句**，不是用旧备份覆盖整份配置：

- 安装前该键存在 → 写回该行的**原始文本**（含原缩进、引号与换行风格）。
- 安装前该键不存在 → 删除本工具添加的那一行。
- 安装前 `config.toml` 不存在且恢复后内容为空 → 删除该文件。
- 被手工改过的副本不会被删除，会列在 `retained_paths` 里。
- 发生过引用修复时，恢复到**最近一次修复前**的当前引用；旧状态、旧指令、首次备份都留在 `history/` 里不删。
- 当前引用被其他操作改过时，恢复会停下并保留现场，不强行覆盖。

## 配置布局

```text
CODEX_HOME/
├── config.toml                                  # 只改其中一行
└── managed-prompts/
    └── xiaoli-armor/
        ├── active.md                            # 指令副本（重名时用 active-<随机>.md）
        ├── install-state.json                   # 安装状态与恢复基线
        ├── config.toml.before-xiaoli.bak        # 首次备份（首次安装时原文件存在才生成）
        ├── install.lock                         # 并发锁（含 pid 与时间戳，操作结束即删）
        └── history/
            └── reference-<随机标识>/
                ├── install-state.json
                ├── config-before-repair.toml
                ├── config-before-first-install.bak
                └── prompts/<旧副本>.md
```

## profile.json

```jsonc
{
  "assistant_name": "小李",                  // 身份名，问候/口令文本可用 {assistant_name} 引用
  "legacy_names": [],                       // 迁移：把这些旧名字替换成当前身份名
  "retire_regexes": [],                     // 迁移：匹配到的整行会被删除（正则）
  "greeting":  { "triggers": ["hi", "你好"], "reply": "「你好」\n{assistant_name}在。👋" },
  "passphrase": { "trigger": "小李",         "reply": "第一行\n第二行" },
  "begin_marker": "<!-- xiaoli-armor:begin -->",
  "end_marker": "<!-- xiaoli-armor:end -->",
  "base_instructions": "…没有外部引用时使用的基础正文…",
  "operator_notes": "…身份块里的执行纪律，逐条写…"
}
```

约束：确认口令不能与普通问候触发词相同；两个管理块标记必须互不相同且不互相包含；管理块之后不得有手工内容（否则安装会停下并保留现场）。

## 数据安全底线

1. **单点注入**：只改 `model_instructions_file` 一条赋值语句的字符区间，其余字节（含 BOM、CRLF/LF、注释、顺序）原样保留。
2. **字符串感知定位**：三引号/多行字符串、数组、内联表、注释里的同名文本不会被误判；表头之后的同名键不会当成顶层键。
3. **写前语义等价断言**：改写结果必须能重新解析，且除目标键外所有配置值完全一致，否则拒绝写入。
4. **首次备份永不覆盖**：重复安装、引用修复、升级都不修改首次备份的字节。
5. **修复前先归档**：引用修复时先把旧状态、旧指令、修复前配置、原首次备份写入 `history/`，归档先于任何覆盖写。
6. **手改永不销毁**：副本内容与记录摘要不一致时，只保留并记入 `retained_paths`。
7. **拒绝链接**：config / state / backup / managed / history 链上的符号链接与目录联接一律拒绝，且不跟随。
8. **多文件事务**：状态、副本、config、备份在一次事务内落地；写前、写中、回滚前都做 CAS 校验，发现并发修改就保留现场并报冲突。
9. **幂等**：内容未变时零写入，连 mtime 都不动。
10. **只读动作真只读**：`--status` / `--verify` / `--audit` / `--emit` / `--dry-run` 不创建目录、不写盘。

## 与其它工具并存

如果当前 `model_instructions_file` 指向的是**另一个工具**写在自己的 `managed-prompts/<其他 id>/` 里的副本（例如 `managed-prompts/xiaoguai-oneclick/active.md`），安装会**默认拒绝**并提示：

```text
当前指令引用由另一个工具写入（managed-prompts/xiaoguai-oneclick），未覆盖；确认要接管该注入点时加 --takeover 重试。
```

确认要接管时加 `--takeover`。接管只会改写配置里的那一行，旧工具的状态文件、副本目录与它的备份都会原样留在磁盘上；但它的「恢复」会因为引用被改而停下并保留现场——这是它的设计，不是本工具删了东西。

## 已知边界

- 本工具只做**本地指令配置**。它不改变服务端模型能力或限制，也不实现 ACE 绕过一类功能。
- 暴露审计只检查本地文件与环境变量的存在性/一致性，**不检测真实流量、不检查系统代理、不判断代理是否解密**，也不输出提示词正文。未检查项在 `--audit --json` 的 `skipped_checks` 里显式列出。
- 事务是「顺序写 + 失败回滚」的补偿式事务；进程被强杀或断电的极端情况下可能留下中间态，此时 `--doctor` / `--verify` 会把现场归类并给出处置命令，`history/` 里有可人工回退的快照。
- 写入采用「临时文件 + 原子替换」，并对占用类失败重试 3 次（0.15s / 0.5s）。仍失败时给出中文指引：多为杀毒软件拦截、编辑器句柄未释放、或目录位于 OneDrive 等同步盘。**建议不要在被同步盘实时同步的目录里运行**，同步进程会与原子替换抢文件句柄。
- 每次安装/恢复/诊断都会在人类可读输出里打印**实际操作的配置目录**（第一行）。指错目录是这套工具最危险的误操作：本工具会严格执行你给它的 `--codex-home`，不会猜测你的意图。
- 幂等重装（内容未变）不写盘，因此**不申请锁**：陈旧锁不会挡住「无需改动」的安装，只有真正要写入的操作才会因锁而拒绝。`--break-lock` 是显式接管——陈旧锁直接清，仍标记为活动进程的锁会被强制清理（原锁文件先归档到 `history/lock-<id>/`）；唯一绝不清理的是「锁记录的正是当前进程」。
- macOS 入口与脚本仅做过源码级与语法级检查，**未在 Mac 实机验证**。
- 提示词加载成功不等于模型端效果；以 Codex 实际回复为准。

## 测试

```powershell
python -B -m unittest discover -s tests -t .   # 125 项单元回归
python .\selfcheck.py                         # 59 项功能自检 + 对抗性场景
python .\selfcheck_env.py                     # 20 项环境与稳定性检测
```

三个脚本全部在临时目录内运行。`selfcheck.py` 验证工具功能（真实进程持锁、双进程并发安装、崩溃残留锁接管、副本与产物篡改检测、解压后独立运行、独立 EXE 冒烟、批处理编码/换行、现场诊断）；`selfcheck_env.py` 验证这台机器上的痕迹与真实配置完整性（**只读属性与哈希，不读配置内容**、系统残留扫描、20 轮 install/verify/restore 压测、三进程并发、临时文件泄漏检查）。两者都不触碰真实 `CODEX_HOME`、不启动真实 Codex。实测结果见 `验证记录.txt`。

## 打包

```powershell
python .\build_package.py                     # 生成 downloads/xiaoli-armor-v1.0.0.zip + SHA256SUMS
python .\build_package.py --verify downloads/xiaoli-armor-v1.0.0.zip
```

打包只按白名单收集文件，产物生成后立即重开校验（成员清单、CRC、逐成员字节、路径合法性）。

## 目录结构

```text
.
├── xiaoli.py                 # 命令行入口
├── xiaoli_engine.py          # TOML 定位、事务、锁、状态、快照、恢复
├── xiaoli_prompt.py          # 身份块合成与迁移
├── xiaoli_audit.py           # 只读脱敏审计
├── profile.json              # 身份 / 问候 / 口令 / 执行纪律
├── 小李破甲安装器.exe        # Windows 独立 EXE（目标机无需 Python）
├── build_exe.py              # 构建独立 EXE（PyInstaller，隔离环境）
├── build-requirements.txt    # 构建 EXE 的依赖（运行本工具不需要）
├── build_package.py          # 白名单打包与产物校验
├── selfcheck.py              # 59 项功能自检 + 对抗性场景
├── selfcheck_env.py          # 20 项环境与稳定性检测（含真实配置完整性核对）
├── 启动小李破甲.cmd / .command
├── 卸载小李破甲.cmd / .command
├── 查看小李状态.cmd / .command
├── 检查环境.cmd / .command
├── macos/launch.sh           # macOS 解释器发现层
├── macos/README.md
├── tests/                    # 125 项回归测试
├── downloads/                # 打包产物与 SHA256SUMS
├── 使用说明.txt
└── 验证记录.txt
```

`.cmd` 文件是**纯 ASCII + CRLF**的：cmd.exe 按系统代码页解析批处理，含中文或用 LF 换行都会让脚本崩坏（标签查找失败、引号配对错位），这是实测踩出来的坑，改动时请保持这两个约束——`selfcheck.py` 的 E6/E8 会拦住回归。

## 开源与发布

- **许可证**：MIT，见 `LICENSE`。
- **CI**：`.github/workflows/tests.yml` 在 Windows / Linux / macOS × Python 3.11–3.13 上跑 133 项单元回归，Windows 上另跑 59 项功能自检与 19 项环境检测。
- **换行符**：`.gitattributes` 锁定 `.cmd/.bat = CRLF`、`.sh/.command = LF`——不锁的话 git 的自动转换会让克隆下来的批处理直接失效。
- **发版与开源前检查清单**：见 `docs/UPLOAD.md`（含建仓、推送、打 tag、发 Release 的完整命令）。
- **免安装产物**：`小李破甲安装器.exe` 与 `downloads/xiaoli-armor-v1.0.0.zip` 随仓库提供，随时可用 `build_exe.py` / `build_package.py` 复现。

## 边界声明

本工具只做**本地指令配置**：不改 Codex 程序、账号、模型设置或 API 密钥，不结束正在运行的 Codex，不绕过服务端策略。把条款写进本地指令文件并不保证模型一定照做——`验证记录.txt` 里记录了两次「写进去但模型仍然拒绝」的真实案例，以及随后的条款调整。审计只检查本地文件与环境变量，不检测真实流量、不判断代理是否解密。

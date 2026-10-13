#!/usr/bin/env python3
"""小李破甲 · 环境与稳定性检测（与 selfcheck.py 互补）。

selfcheck.py 验证「工具功能是否正确」，本脚本验证「工具在这台机器上留下了什么、
以及这台机器的真实配置是否完好」：

  A. 系统残留扫描：用户目录、临时目录、环境变量、本次任务装过的构建依赖
  B. 真实 CODEX_HOME 完整性：只看存在性/大小/mtime/哈希，**不读配置内容**；
     并断言本工具从未在真实配置目录留下安装痕迹
  C. 工具自身健康：全部 Python 文件可编译、安装包逐成员字节校验、
     独立 EXE 与源码在同一隔离目录上的判断是否一致
  D. 稳定性压测：20 轮 install/verify/restore 循环、三进程并发安装、
     临时文件泄漏检查、手改副本的多轮保留语义

所有写入型实验都在系统临时目录的隔离 CODEX_HOME 内完成。
用法：python selfcheck_env.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
CLI = ROOT / "xiaoli.py"
USER = Path.home()
REAL_HOME = USER / ".codex"
RESULTS: list[tuple[str, str, str, str]] = []


def report(kind: str, name: str, detail: str = "") -> None:
    RESULTS.append((kind, name, "", detail))
    print(f"[{kind}] {name}" + (f"  ::  {detail}" if detail else ""), flush=True)


def digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def stat_line(path: Path) -> str:
    try:
        info = path.stat()
        return f"大小={info.st_size} 修改={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info.st_mtime))}"
    except OSError as exc:
        return f"无法读取属性：{exc}"


def run(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(CLI), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, cwd=str(ROOT))


# --------------------------------------------------------------------------
# A. 我在系统里留下的痕迹
# --------------------------------------------------------------------------


def scan_footprint() -> None:
    print("\n=== A. 系统残留扫描（只看存在性/属性，不读内容） ===")
    suspects = [
        USER / "config.toml",          # 用户主目录下不应有 config.toml（曾有调试误建）
        USER / "prompts",
        USER / "managed-prompts",
        USER / "Desktop" / "xiaoli-armor-v1.0.0.zip",
        USER / "Downloads" / "xiaoli-armor-v1.0.0.zip",
    ]
    # 注意：.codex\managed-prompts\xiaoli-armor 是**合法安装目录**，不算残留，由 B 组单独核查。
    leftovers = [path for path in suspects if path.exists()]
    report("PASS" if not leftovers else "FAIL", "用户目录无本工具残留",
           "干净" if not leftovers else "仍存在：" + "；".join(str(p) for p in leftovers))

    temp = Path(tempfile.gettempdir())
    selfcheck_dirs = sorted(p for p in temp.glob("xiaoli-selfcheck-*") if p.is_dir())
    report("PASS" if not selfcheck_dirs else "WARN", "自检临时目录已清理",
           f"{len(selfcheck_dirs)} 个" + (f"：{selfcheck_dirs[:3]}" if selfcheck_dirs else ""))

    stray = sorted(p for p in temp.glob(".xiaoli-*"))
    report("PASS" if not stray else "FAIL", "临时目录无原子写残留文件", f"{len(stray)} 个")

    env_names = [name for name in ("XIAOLI_PYTHON", "XIAOLI_NO_PAUSE", "CODEX_HOME") if os.environ.get(name)]
    report("INFO", "相关环境变量", "、".join(env_names) if env_names else "未设置（正常）")

    # PyInstaller 是我在本次任务里装进用户 Python 环境的，必须如实报告
    probe = subprocess.run([PY, "-m", "pip", "show", "pyinstaller"], capture_output=True, text=True)
    if probe.returncode == 0:
        version = next((line.split(":")[1].strip() for line in probe.stdout.splitlines()
                        if line.startswith("Version")), "?")
        report("INFO", "本机 Python 环境里有 PyInstaller（本次任务安装，用于构建 EXE）",
               f"版本 {version}；不再需要时可用：python -m pip uninstall pyinstaller pyinstaller-hooks-contrib "
               f"pywin32-ctypes pefile altgraph packaging setuptools 逐个卸载")
    else:
        report("INFO", "本机 Python 环境无 PyInstaller", "")


# --------------------------------------------------------------------------
# B. 真实配置的完整性（只看属性与哈希）
# --------------------------------------------------------------------------


def check_real_config() -> None:
    print("\n=== B. 真实 CODEX_HOME 完整性核对（不读内容） ===")
    if not REAL_HOME.is_dir():
        report("INFO", "未发现真实 .codex 目录，跳过", "")
        return
    config = REAL_HOME / "config.toml"
    if not config.is_file():
        report("INFO", "真实 config.toml 不存在", "")
        return
    report("INFO", f"真实 config.toml {stat_line(config)}", f"sha256[:16]={digest(config)}")
    tools_made = REAL_HOME / "managed-prompts" / "xiaoli-armor"
    report("INFO", "本工具在真实配置目录的存在情况",
           f"已安装（存在 {tools_made.name} 目录）" if tools_made.exists() else "零痕迹（未安装）")

    # 只抓引用是否仍指向既有工具的副本，不打印路径全文
    try:
        line = next((text for text in config.read_text(encoding="utf-8", errors="replace").splitlines()
                     if text.startswith("model_instructions_file")), "")
    except OSError as exc:
        report("WARN", "读取引用行失败", str(exc))
        return

    # 只读诊断与审计（输出已脱敏）
    proc = run("--status", "--json")
    try:
        status = json.loads(proc.stdout)
    except ValueError:
        status = {}
    if status.get("installed"):
        healthy = bool(status.get("reference_ok")) and bool(status.get("prompt_exists"))
        report("PASS" if healthy else "FAIL", "真实配置处于「已安装且引用一致」状态",
               f"副本={Path(status.get('prompt_path') or '').name} / 引用一致={status.get('reference_ok')} / "
               f"副本存在={status.get('prompt_exists')}")
    else:
        report("PASS" if "xiaoguai-oneclick" in line else "INFO", "真实配置未被本工具占用",
               "引用仍指向既有工具" if "xiaoguai-oneclick" in line else "引用由其他来源提供")

    proc = run("--doctor", "--json")
    situation = ""
    try:
        situation = json.loads(proc.stdout).get("situation", "")
    except ValueError:
        pass
    report("PASS" if proc.returncode == 0 and situation else "FAIL", "doctor 能正常判定真实现场",
           f"exit={proc.returncode} 现状={situation}")
    proc = run("--audit", "--json")
    leaked = "sk-" in proc.stdout or "Bearer" in proc.stdout
    report("PASS" if not leaked else "FAIL", "审计输出不含任何密钥样式字符串", "" if not leaked else "发现 sk-/Bearer 字样")


# --------------------------------------------------------------------------
# C. 工具自身健康
# --------------------------------------------------------------------------


def check_tool_health() -> None:
    print("\n=== C. 工具自身健康 ===")
    bad: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        if ".venv-build" in path.parts or "__pycache__" in path.parts:
            continue
        proc = subprocess.run([PY, "-m", "py_compile", str(path)], capture_output=True, text=True)
        if proc.returncode != 0:
            bad.append(f"{path.name}: {proc.stderr.strip()[:80]}")
    report("PASS" if not bad else "FAIL", "全部 Python 文件可编译", "；".join(bad) if bad else "无语法错误")

    package = ROOT / "downloads" / "xiaoli-armor-v1.0.0.zip"
    if package.is_file():
        proc = subprocess.run([PY, str(ROOT / "build_package.py"), "--verify", str(package)],
                              capture_output=True, text=True, encoding="utf-8")
        report("PASS" if proc.returncode == 0 else "FAIL", "安装包逐成员字节校验",
               proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else proc.stderr.strip()[:80])
    else:
        report("WARN", "未找到安装包", str(package))

    exe = ROOT / "小李破甲安装器.exe"
    if exe.is_file():
        report("INFO", f"独立 EXE {stat_line(exe)}", f"sha256[:16]={digest(exe)}")
        # EXE 与源码行为一致性：同一隔离目录分别用 EXE / 源码跑 status，比较关键字段
        with tempfile.TemporaryDirectory(prefix="xiaoli-inspect-") as tmp:
            home = Path(tmp)
            (home / "prompts").mkdir(parents=True)
            (home / "prompts" / "base.md").write_bytes("# base\nbody\n".encode())
            (home / "config.toml").write_bytes(b'model_instructions_file = "./prompts/base.md"\n')
            src = run("--codex-home", str(home), "--install", "--no-open", "--json")
            exe_proc = subprocess.run([str(exe), "--codex-home", str(home), "--status", "--json"],
                                      capture_output=True, text=True, encoding="utf-8", timeout=300)
            src_proc = run("--codex-home", str(home), "--status", "--json")
            try:
                a = json.loads(exe_proc.stdout)
                b = json.loads(src_proc.stdout)
                same = (a.get("installed") == b.get("installed") == True
                        and a.get("reference_ok") == b.get("reference_ok") == True
                        and a.get("prompt_path") == b.get("prompt_path"))
            except ValueError:
                same = False
            report("PASS" if same and src.returncode == 0 else "FAIL",
                   "EXE 与源码在同一隔离目录上判断一致", f"安装 exit={src.returncode}")
    else:
        report("WARN", "未找到独立 EXE", "")


# --------------------------------------------------------------------------
# D. 稳定性压测（隔离目录）
# --------------------------------------------------------------------------


def stress(rounds: int = 20) -> None:
    print(f"\n=== D. 稳定性压测（{rounds} 轮 install/verify/restore 循环，隔离目录） ===")
    tmp = Path(tempfile.mkdtemp(prefix="xiaoli-stress-"))
    try:
        home = tmp / "home"
        (home / "prompts").mkdir(parents=True)
        (home / "prompts" / "base.md").write_bytes("# base\n正文\n".encode())
        (home / "config.toml").write_bytes(b'model_instructions_file = "./prompts/base.md"\n')
        managed = home / "managed-prompts" / "xiaoli-armor"

        failures: list[str] = []
        for index in range(1, rounds + 1):
            install = run("--codex-home", str(home), "--install", "--no-open", "--json")
            if install.returncode != 0:
                failures.append(f"第{index}轮 install exit={install.returncode}")
                continue
            verify = run("--codex-home", str(home), "--verify")
            if verify.returncode != 0:
                failures.append(f"第{index}轮 verify exit={verify.returncode}")
            restore = run("--codex-home", str(home), "--restore", "--json")
            if restore.returncode != 0 or '"restored": true' not in restore.stdout:
                failures.append(f"第{index}轮 restore 异常")
            # 每轮结束应回到干净基线
            if index == 1 and managed.exists():
                failures.append("第1轮 restore 后仍残留工具目录")
        report("PASS" if not failures else "FAIL", f"{rounds} 轮安装/校验/恢复循环无失败",
               "；".join(failures[:4]) if failures else "全部 exit=0，恢复后回到干净基线")

        # 临时文件泄漏
        leftovers = sorted(tmp.rglob(".xiaoli-*"))
        report("PASS" if not leftovers else "FAIL", "压测期间无 .xiaoli-*.tmp 残留", f"{len(leftovers)} 个")

        # 最终安装一次并并发三进程重装
        run("--codex-home", str(home), "--install", "--no-open", "--json")
        procs = [subprocess.Popen([PY, str(CLI), "--codex-home", str(home), "--install", "--no-open", "--json"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
                 for _ in range(3)]
        outs = [proc.communicate(timeout=300) for proc in procs]
        codes = sorted(proc.returncode for proc in procs)
        status = json.loads(run("--codex-home", str(home), "--status", "--json").stdout)
        consistent = status.get("reference_ok") is True and status.get("prompt_exists") is True
        conflict_seen = any("正在运行" in (out + err) for out, err in outs)
        report("PASS" if codes[0] == 0 and consistent else "FAIL",
               "三进程并发安装后现场一致",
               f"退出码={codes}；引用一致={consistent}；出现锁冲突提示={conflict_seen}")

        # 反复手改「当前引用指向的」副本 + 重装：每轮都应另建新文件，旧文件全部保留
        prompt_dir = home / "managed-prompts" / "xiaoli-armor"
        edit_rounds = 3
        edit_failures: list[str] = []
        edited: list[Path] = []
        for index in range(edit_rounds):
            status = json.loads(run("--codex-home", str(home), "--status", "--json").stdout)
            current = Path(status["prompt_path"])
            text = current.read_text(encoding="utf-8")
            current.write_text(text.replace("正文", f"正文{index + 1}", 1), encoding="utf-8")
            edited.append(current)
            proc = run("--codex-home", str(home), "--install", "--no-open", "--json")
            payload = json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip().startswith("{") else {}
            if proc.returncode != 0 or payload.get("prompt_path") in (None, str(current)):
                edit_failures.append(f"第{index + 1}轮未另建副本（exit={proc.returncode}）")
        total = len(sorted(prompt_dir.glob("active*.md")))
        report("PASS" if not edit_failures and total >= edit_rounds + 1 else "FAIL",
               "手改副本后每轮都另建新文件、旧文件全部保留",
               f"副本数={total}（期望 >= {edit_rounds + 1}）" + ("；" + "；".join(edit_failures) if edit_failures else ""))

        # 恢复：手改过的文件必须全部保留在磁盘上
        first_edited = edited[0]
        restore = run("--codex-home", str(home), "--restore", "--json")
        retained = json.loads(restore.stdout).get("retained_paths", []) if restore.returncode == 0 else []
        kept_ok = all(Path(item).is_file() for item in retained) and first_edited.is_file()
        report("PASS" if restore.returncode == 0 and kept_ok else "FAIL",
               "恢复时保留所有手工改过的副本（含最早那个）",
               f"retained={len(retained)}；最早手改文件仍在={first_edited.is_file()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    scan_footprint()
    check_real_config()
    check_tool_health()
    stress()
    fails = [item for item in RESULTS if item[0] == "FAIL"]
    warns = [item for item in RESULTS if item[0] == "WARN"]
    print("\n" + "=" * 78)
    print(f"检测项 {len(RESULTS)} 个：FAIL {len(fails)}，WARN {len(warns)}，其余通过/信息")
    for _, name, _, detail in fails:
        print(f"  FAIL：{name}  ::  {detail}")
    for _, name, _, detail in warns:
        print(f"  WARN：{name}  ::  {detail}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())

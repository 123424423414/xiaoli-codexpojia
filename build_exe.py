#!/usr/bin/env python3
"""小李破甲 · Windows 独立 EXE 构建（需要 PyInstaller，非运行依赖）。

构建出的「小李破甲安装器.exe」内置 Python 运行时，
放在工具目录里与 .cmd 同级即可，目标机无需安装 Python。

用法：
  python build_exe.py            # 构建并复制到工具根目录
  python build_exe.py --clean    # 先清理 build/ dist/ *.spec 再构建
  python build_exe.py --venv     # 在 .venv-build 隔离环境里构建（不污染本机 Python 环境）

若本机 Python 环境里存在与 PyInstaller 冲突的过时 backport 包（例如 pathlib），
脚本会自动改用隔离环境重试一次。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE_NAME = "小李破甲安装器"
ENTRY = "xiaoli.py"
BUNDLED_MODULES = ("xiaoli_engine.py", "xiaoli_prompt.py", "xiaoli_audit.py")
VENV = ROOT / ".venv-build"
PYINSTALLER_PIN = "pyinstaller==6.22.3"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_venv() -> str:
    """在工具目录下准备一个只用于构建的隔离 Python 环境。"""
    python = VENV / "Scripts" / "python.exe"
    if not python.is_file():
        print(f"[构建] 创建隔离环境：{VENV}")
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    probe = subprocess.run([str(python), "-c", "import PyInstaller;print(PyInstaller.__version__)"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"[构建] 在隔离环境中安装 {PYINSTALLER_PIN}")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "-q", PYINSTALLER_PIN], check=True)
    return str(python)


def _run_pyinstaller(python: str) -> subprocess.CompletedProcess:
    command = [
        python, "-m", "PyInstaller",
        "--onefile", "--console", "--noupx",
        "--name", EXE_NAME,
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        str(ROOT / ENTRY),
    ]
    return subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800)


def build(*, clean: bool = False, use_venv: bool = False) -> dict:
    if os.name != "nt":
        raise SystemExit("本脚本仅用于在 Windows 上构建独立 EXE。")
    for name in BUNDLED_MODULES:
        if not (ROOT / name).is_file():
            raise SystemExit(f"缺少被打包的模块：{name}")
    if clean:
        for target in (ROOT / "build", ROOT / "dist"):
            shutil.rmtree(target, ignore_errors=True)
        for spec in ROOT.glob("*.spec"):
            spec.unlink()

    builder = ensure_venv() if use_venv else sys.executable
    proc = _run_pyinstaller(builder)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not use_venv and "obsolete backport" in output:
        # 本机 site-packages 里有与 PyInstaller 冲突的过时包：不动用户环境，改用隔离环境。
        print("[构建] 本机环境存在冲突包，自动改用隔离环境重试")
        builder = ensure_venv()
        proc = _run_pyinstaller(builder)
        output = (proc.stdout or "") + (proc.stderr or "")

    produced = ROOT / "dist" / f"{EXE_NAME}.exe"
    if proc.returncode != 0 or not produced.is_file():
        tail = "\n".join(output.splitlines()[-25:])
        raise SystemExit("PyInstaller 构建失败：\n" + tail)

    target = ROOT / f"{EXE_NAME}.exe"
    shutil.copy2(produced, target)
    return {
        "ok": True,
        "action": "build-exe",
        "builder": builder,
        "exe": str(target),
        "bytes": target.stat().st_size,
        "sha256": _sha256(target),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="构建小李破甲 Windows 独立 EXE")
    parser.add_argument("--clean", action="store_true", help="构建前清理 build/dist/spec")
    parser.add_argument("--venv", action="store_true", help="在 .venv-build 隔离环境里构建")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = build(clean=args.clean, use_venv=args.venv)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else f"已生成：{result['exe']}\nsha256：{result['sha256']}\n大小：{result['bytes']} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

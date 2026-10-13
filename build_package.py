#!/usr/bin/env python3
"""小李破甲 · 白名单打包与产物自校验（Python 3.11+，仅标准库）。

原则：
  * 只打包显式白名单里的文件，绝不递归收集；本机配置、状态、备份、日志一概不进包。
  * 打包后立即重开产物，逐成员核对字节、清单、CRC 与路径合法性。
  * 默认拒绝覆盖已存在的产物；写入用临时文件 + 原子替换。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
VERSION = "1.0.0"

MEMBERS: tuple[str, ...] = (
    "xiaoli.py",
    "xiaoli_engine.py",
    "xiaoli_prompt.py",
    "xiaoli_audit.py",
    "profile.json",
    "build_package.py",
    "build_exe.py",
    "build-requirements.txt",
    "selfcheck.py",
    "selfcheck_env.py",
    "小李破甲安装器.exe",
    "README.md",
    "LICENSE",
    "docs/UPLOAD.md",
    "使用说明.txt",
    "验证记录.txt",
    "启动小李破甲.cmd",
    "卸载小李破甲.cmd",
    "查看小李状态.cmd",
    "检查环境.cmd",
    "启动小李破甲.command",
    "卸载小李破甲.command",
    "查看小李状态.command",
    "检查环境.command",
    "macos/launch.sh",
    "macos/README.md",
    "tests/__init__.py",
    "tests/test_toml.py",
    "tests/test_engine.py",
    "tests/test_prompt.py",
    "tests/test_cli.py",
    "tests/test_audit.py",
)

EXECUTABLE_SUFFIXES = (".command", ".sh")
FIXED_TIME = (2024, 1, 1, 0, 0, 0)
# cmd.exe 的标签查找依赖 CRLF，LF-only 的 .cmd 会在 goto 处报“找不到批处理标签”；
# 反过来 POSIX 脚本必须是 LF。这里对入包字节做统一规范化。
CRLF_SUFFIXES = (".cmd", ".bat")
LF_SUFFIXES = (".command", ".sh")


class PackageError(RuntimeError):
    pass


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _member_bytes(name: str) -> bytes:
    """读取成员内容，并按平台要求规范化换行后再打包/校验。"""
    raw = (ROOT / name).read_bytes()
    if name.endswith(CRLF_SUFFIXES):
        return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if name.endswith(LF_SUFFIXES):
        return raw.replace(b"\r\n", b"\n")
    return raw


def _members() -> list[str]:
    missing = [name for name in MEMBERS if not (ROOT / name).is_file()]
    if missing:
        raise PackageError("白名单里的文件不存在：" + "、".join(missing))
    return list(MEMBERS)


def build(output: Path, *, force: bool = False) -> dict:
    names = _members()
    output = output if output.is_absolute() else ROOT / output
    if output.exists() and not force:
        raise PackageError(f"产物已存在，未覆盖：{output}（需要覆盖时加 --force）")
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    temp: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".xiaoli-pack-", suffix=".zip", dir=str(output.parent))
        os.close(descriptor)
        temp = Path(name)
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in names:
                info = zipfile.ZipInfo(filename=member, date_time=FIXED_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if member.endswith(EXECUTABLE_SUFFIXES) else 0o644
                info.external_attr = mode << 16
                archive.writestr(info, _member_bytes(member))
        report = verify(temp)
        if not report["ok"]:
            raise PackageError("打包自校验失败：" + "；".join(report["problems"]))
        os.replace(temp, output)
        temp = None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        del handle
    digest = _sha256(output.read_bytes())
    sums = output.with_name(f"SHA256SUMS-xiaoli-v{VERSION}.txt")
    sums.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return {
        "ok": True,
        "action": "build",
        "version": VERSION,
        "package": str(output),
        "bytes": output.stat().st_size,
        "sha256": digest,
        "sums": str(sums),
        "members": len(names),
    }


def verify(package: Path) -> dict:
    package = package if package.is_absolute() else ROOT / package
    if not package.is_file():
        return {"ok": False, "package": str(package), "problems": ["产物不存在"]}
    problems: list[str] = []
    names: list[str] = []
    digest = _sha256(package.read_bytes())
    try:
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                problems.append("产物内有重复成员")
            expected = set(MEMBERS)
            extra = sorted(set(names) - expected)
            absent = sorted(expected - set(names))
            if extra:
                problems.append("产物内有白名单之外的成员：" + "、".join(extra))
            if absent:
                problems.append("产物缺少白名单成员：" + "、".join(absent))
            bad = archive.testzip()
            if bad:
                problems.append(f"CRC 校验失败：{bad}")
            for name in names:
                parts = PurePosixPath(name).parts
                if name.startswith("/") or ".." in parts or name.endswith("/"):
                    problems.append(f"成员路径不合法：{name}")
                    continue
                source = ROOT / name
                if not source.is_file():
                    problems.append(f"源文件缺失，无法比对：{name}")
                    continue
                if archive.read(name) != _member_bytes(name):
                    problems.append(f"成员字节与源文件不一致：{name}")
    except Exception as exc:  # noqa: BLE001
        # 损坏的产物可能表现为 BadZipFile、zlib.error、EOFError、RuntimeError 等多种异常，
        # 这里一律转成结构化问题列表，保证校验本身不会以异常栈结束。
        problems.append(f"产物无法完整校验（疑似损坏）：{type(exc).__name__}: {exc}")
    return {
        "ok": not problems,
        "action": "verify",
        "package": str(package),
        "sha256": digest,
        "members": len(names),
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="小李破甲白名单打包与校验")
    parser.add_argument("--output", type=Path, default=Path("downloads") / f"xiaoli-armor-v{VERSION}.zip")
    parser.add_argument("--force", action="store_true", help="允许覆盖已存在的产物")
    parser.add_argument("--verify", type=Path, help="只校验已有产物，不打包")
    parser.add_argument("--list", action="store_true", help="打印白名单文件清单")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.list:
            for name in MEMBERS:
                size = (ROOT / name).stat().st_size if (ROOT / name).is_file() else 0
                print(f"{size:>8}  {name}")
            return 0
        result = verify(args.verify) if args.verify else build(args.output, force=args.force)
    except (PackageError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) if args.json
              else f"打包未完成：{exc}")
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("action") == "verify":
        print(("产物校验通过：" if result["ok"] else "产物校验未通过：") + result["package"])
        print(f"sha256：{result['sha256']}；成员数：{result['members']}")
        for problem in result["problems"]:
            print("  - " + problem)
    else:
        print(f"已生成安装包：{result['package']}")
        print(f"sha256：{result['sha256']}；成员数：{result['members']}；{result['bytes']} 字节")
        print(f"校验文件：{result['sums']}")
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())

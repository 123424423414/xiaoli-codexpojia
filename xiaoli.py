#!/usr/bin/env python3
"""小李破甲 · 命令行入口（Python 3.11+，仅标准库）。

  python xiaoli.py install          # 安装/更新身份与口令配置（默认动作）
  python xiaoli.py restore          # 恢复本次安装前的指令引用
  python xiaoli.py status           # 只读查看本地安装状态
  python xiaoli.py verify           # 只读校验配置链（不调用模型）
  python xiaoli.py audit            # 只读脱敏环境审计
  python xiaoli.py emit             # 只输出合成后的指令全文，不安装
  python xiaoli.py init-profile     # 生成默认 profile.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

if sys.version_info < (3, 11):
    # 必须在导入引擎前拦下：tomllib 是 3.11 才有的标准库模块。
    sys.stderr.write(
        "小李破甲需要 Python 3.11 或更高版本（当前 %s）。\n"
        "  Windows：安装 python.org 版 Python 3.11+ 后重试，"
        "或直接运行同目录的「小李破甲安装器.exe」（无需 Python）。\n"
        "  macOS：可用 XIAOLI_PYTHON=/绝对路径/python3 指定解释器。\n" % sys.version.split()[0]
    )
    raise SystemExit(2)

from xiaoli_audit import audit, format_audit  # noqa: E402
from xiaoli_engine import (  # noqa: E402
    ConfigConflictError,
    ConfigDocument,
    ConfigError,
    ConfigStore,
    _bytes,
)
from xiaoli_prompt import ProfileError, compose, load_identity, write_default_profile  # noqa: E402

VERSION = "1.0.0"


def _tool_dir() -> Path:
    """源码运行时是脚本所在目录；PyInstaller 单文件模式下是 EXE 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


TOOL_DIR = _tool_dir()
DEFAULT_PROFILE_FILE = TOOL_DIR / "profile.json"
PROMPT_FILENAME = "小李身份与口令.md"
PROFILE_TITLE = "小李 · 一键破甲"
PROFILE_KEY = "xiaoli"


def _emit_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _identity_path(args: argparse.Namespace) -> Path:
    return Path(args.profile_file) if args.profile_file else DEFAULT_PROFILE_FILE


def foreign_managed_owner(store: ConfigStore, source: Path) -> str | None:
    """当前引用是否由另一个工具写在它自己的 managed-prompts/<id>/ 里。"""
    root = store.codex_home / "managed-prompts"
    try:
        relative = Path(os.path.relpath(source.resolve(), root.resolve()))
    except (OSError, ValueError):
        return None
    parts = relative.parts
    if len(parts) >= 2 and parts[0] not in ("..", "") and parts[0] != store.managed_dir.name:
        return parts[0]
    return None


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------


def cmd_install(args: argparse.Namespace) -> dict:
    identity = load_identity(_identity_path(args), required=bool(args.profile_file))
    store = ConfigStore(args.codex_home)
    raw_config = _bytes(store.config_path)
    doc = ConfigDocument(raw_config)
    keys, mode = store.detect_target(doc, args.target)
    reference = doc.lookup(keys)
    state_raw = _bytes(store.state_path)
    state = store.read_state() if state_raw is not None else None
    will_repair = bool(state) and not store.state_is_current(state, doc)

    expected_inputs: dict[Path, bytes | None] = {store.config_path: raw_config, store.state_path: state_raw}
    source_path: Path | None = None
    source_raw: bytes | None = None
    if isinstance(reference, str) and reference.strip():
        source_path = store.resolve_reference(reference)
        reserved = {store.config_path, store.state_path, store.backup_path, store.lock_path}
        if source_path in {path.resolve() for path in reserved}:
            raise ConfigError("当前指令引用指向本工具的配置、状态、备份或锁文件，未修改任何配置。")
        if source_path.is_dir():
            raise ConfigError(f"当前指令引用指向目录而不是文件，未替换原引用：{source_path}")
        source_raw = _bytes(source_path)
        if source_raw is None:
            raise ConfigError(f"当前指令文件不存在，未替换原引用：{source_path}")
        owner = foreign_managed_owner(store, source_path)
        if owner and not args.takeover:
            raise ConfigError(
                f"当前指令引用由另一个工具写入（managed-prompts/{owner}），未覆盖；"
                f"确认要接管该注入点时加 --takeover 重试。"
            )
        try:
            base = source_raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ConfigError("当前指令文件不是 UTF-8，未替换原引用。") from exc
        if not base.strip():
            raise ConfigError("当前指令文件为空，未替换原引用。")
        expected_inputs[source_path] = source_raw
    else:
        base = identity.base_instructions

    content, stats = compose(base, identity)
    raw_content = content.encode("utf-8")
    unchanged = bool(
        state and not will_repair and source_path is not None
        and source_raw == raw_content
        and Path(state["prompt_path"]) == source_path
        and state.get("owned_prompts", {}).get(str(source_path)) == hashlib.sha256(raw_content).hexdigest()
    )

    common = {
        "ok": True,
        "action": "install",
        "version": VERSION,
        "codex_home": str(store.codex_home),
        "target_key": ".".join(keys),
        "target_mode": mode,
        "current_reference": str(source_path) if source_path else None,
        "assistant_name": identity.assistant_name,
        "greeting_triggers": list(identity.greeting_triggers),
        "expected_greeting": identity.greeting_reply,
        "passphrase_trigger": identity.trigger,
        "expected_reply": identity.passphrase_reply,
        "prompt_bytes": len(raw_content),
        "migration": stats,
        "config_installed": False,
        "model_reply_verified": False,
    }

    if args.dry_run:
        common.update({
            "dry_run": True,
            "would_write": not unchanged,
            "unchanged": unchanged,
            "reference_repaired": will_repair,
            "state_present": state is not None,
        })
        return common

    if unchanged:
        for path, expected in expected_inputs.items():
            if _bytes(path) != expected:
                raise ConfigConflictError("检查期间配置发生变化，请重新执行。")
        installed_state = state or {}
        repaired = False
        recovery = None
    else:
        installed_state = store.install_content(
            content,
            PROMPT_FILENAME,
            title=PROFILE_TITLE,
            key=PROFILE_KEY,
            expected_inputs=expected_inputs,
            rebase_current=True,
            target_mode=args.target,
            break_stale=args.break_lock,
        )
        repaired = bool(installed_state.get("reference_repaired"))
        recovery = installed_state.get("recovery_path")

    common.update({
        "unchanged": unchanged,
        "reference_repaired": repaired,
        "recovery_path": recovery,
        "prompt_path": installed_state.get("prompt_path"),
        "backup_path": installed_state.get("backup_path") or (
            str(store.backup_path) if store.backup_path.is_file() else None
        ),
        "config_installed": True,
    })

    warnings: list[str] = []
    if installed_state.get("replaced_edited_prompt"):
        warnings.append(
            "检测到上一份指令副本被手工修改，本次已改用新副本；"
            f"手工改动的文件已保留：{installed_state['replaced_edited_prompt']}"
        )
    if repaired:
        warnings.append("本次重建了过期的安装记录，旧状态、旧指令与首次备份已归档到 history/。")
    if installed_state.get("orphan_backup_archived"):
        warnings.append("发现没有安装状态的孤儿首次备份，已归档到 "
                        + str(installed_state["orphan_backup_archived"]) + " 后继续安装。")
    if stats.get("relocated_tail_lines"):
        warnings.append(f"检测到管理块之后有 {stats['relocated_tail_lines']} 行手工内容，"
                        "已完整保留并移动到身份块之前（不会被覆盖）。")
    if stats.get("foreign_blocks_removed"):
        warnings.append(f"已剥离 {stats['foreign_blocks_removed']} 个其他工具的身份块"
                        "（该块之外的正文完整保留），避免两套身份与确认口令同时生效。")
    common["warnings"] = warnings

    if not args.no_open and sys.platform.startswith("win"):
        try:
            os.startfile("codex://")
            common["codex_open_requested"] = True
        except OSError as exc:
            # 打开失败不撤销已经完成的配置写入。
            common["codex_open_requested"] = False
            common["open_error"] = str(exc)
    else:
        common["codex_open_requested"] = False
    return common


def cmd_restore(args: argparse.Namespace) -> dict:
    store = ConfigStore(args.codex_home)
    result = store.restore(break_stale=args.break_lock)
    result["codex_home"] = str(store.codex_home)
    return result


def cmd_status(args: argparse.Namespace) -> dict:
    return ConfigStore(args.codex_home).status()


def cmd_verify(args: argparse.Namespace) -> dict:
    store = ConfigStore(args.codex_home)
    identity = load_identity(_identity_path(args), required=bool(args.profile_file))
    status = store.status()
    checks: list[dict] = []

    def add(check_id: str, ok: bool, detail: str) -> None:
        checks.append({"id": check_id, "ok": bool(ok), "detail": detail})

    add("config_readable", "config_error" not in status,
        status.get("config_error") or f"config.toml 解析正常：{status['config_path']}")

    reference = status.get("current_reference")
    if status.get("installed"):
        keys = ".".join(status.get("keys", []))
        add("reference_points_to_tool", bool(status.get("reference_ok")),
            f"{keys} -> {status.get('prompt_path')}（一致：{status.get('reference_ok')}）")
        prompt_path = Path(status["prompt_path"]) if status.get("prompt_path") else None
    else:
        add("reference_points_to_tool", False, "尚未安装（没有安装状态）")
        prompt_path = Path(reference) if isinstance(reference, str) else None

    raw_prompt = _bytes(prompt_path) if prompt_path else None
    add("prompt_exists", raw_prompt is not None,
        f"{prompt_path} -> {'存在' if raw_prompt is not None else '缺失'}")
    if raw_prompt is not None:
        try:
            text = raw_prompt.decode("utf-8-sig")
            ok_utf8 = True
        except UnicodeError:
            text, ok_utf8 = "", False
        add("prompt_utf8", ok_utf8, "指令文件 UTF-8 校验：" + ("通过" if ok_utf8 else "失败"))
        begin = text.count(identity.begin_marker)
        end = text.count(identity.end_marker)
        add("marker_integrity", begin == 1 and end == 1,
            f"管理块 begin={begin} end={end}（marker：{identity.begin_marker}）")
        add("greeting_block", identity.greeting_reply in text,
            "身份块里的普通问候回复文本：" + ("已包含" if identity.greeting_reply in text else "缺失"))
        add("passphrase_block", identity.passphrase_reply in text,
            "身份块里的确认口令回复文本：" + ("已包含" if identity.passphrase_reply in text else "缺失"))

    if status.get("installed"):
        digest = hashlib.sha256(raw_prompt).hexdigest() if raw_prompt is not None else None
        state = store.read_state()
        owned = (state or {}).get("owned_prompts", {})
        expected = owned.get(str(prompt_path))
        add("state_digest", digest is not None and digest == expected,
            "副本哈希与安装状态：" + ("一致" if digest == expected else "不一致（副本可能已被手工修改）"))
        add("backup_present", bool(status.get("backup_exists")),
            "首次备份：" + ("存在" if status.get("backup_exists") else "不存在"))

    ok = all(item["ok"] for item in checks)
    return {
        "ok": ok,
        "action": "verify",
        "version": VERSION,
        "codex_home": status["codex_home"],
        "installed": bool(status.get("installed")),
        "checks": checks,
    }


def cmd_audit(args: argparse.Namespace) -> dict:
    return audit(args.codex_home)


def cmd_doctor(args: argparse.Namespace) -> dict:
    return ConfigStore(args.codex_home).diagnose()


def cmd_emit(args: argparse.Namespace) -> dict:
    identity = load_identity(_identity_path(args), required=bool(args.profile_file))
    store = ConfigStore(args.codex_home)
    doc = ConfigDocument(_bytes(store.config_path))
    keys, mode = store.detect_target(doc, args.target)
    reference = doc.lookup(keys)
    base = identity.base_instructions
    source = None
    if isinstance(reference, str) and reference.strip():
        source_path = store.resolve_reference(reference)
        raw = _bytes(source_path)
        if raw is None:
            raise ConfigError(f"当前指令文件不存在：{source_path}")
        base = raw.decode("utf-8-sig")
        source = str(source_path)
    content, stats = compose(base, identity)
    if args.out:
        target = Path(args.out).expanduser().resolve()
        if target.exists() and not args.force:
            raise ConfigError(f"输出文件已存在，未覆盖：{target}（需要覆盖时加 --force）")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return {
        "ok": True,
        "action": "emit",
        "version": VERSION,
        "codex_home": str(store.codex_home),
        "target_key": ".".join(keys),
        "target_mode": mode,
        "source": source,
        "migration": stats,
        "prompt_bytes": len(content.encode("utf-8")),
        "out_path": str(Path(args.out).expanduser().resolve()) if args.out else None,
        "content": None if args.out else content,
    }


def cmd_init_profile(args: argparse.Namespace) -> dict:
    path = write_default_profile(_identity_path(args), force=args.force)
    return {"ok": True, "action": "init-profile", "version": VERSION, "profile_path": str(path)}


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def print_result(args: argparse.Namespace, result: dict) -> None:
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    action = result.get("action")
    if action == "verify":
        # verify 的 ok=False 表示“有检查项未通过”，明细必须照常打印到 stdout。
        print(f"小李破甲 {VERSION} · 配置链校验（未调用模型）")
        for item in result.get("checks", []):
            print(f"  [{'通过' if item['ok'] else '未通过'}] {item['id']}: {item['detail']}")
        print("结论：" + ("本地配置链完整。" if result.get("ok") else "存在未通过的检查项，请先处理再验证模型回复。"))
        return
    if action == "doctor":
        labels = {"ok": "正常", "low": "提示", "medium": "需注意", "high": "需处理"}
        print("小李破甲 · 现场诊断（只读，未修改任何文件）")
        print("配置目录：" + str(result.get("codex_home")))
        print("现状：" + str(result.get("situation")))
        print("级别：" + labels.get(str(result.get("severity")), "未知"))
        for item in result.get("findings", []):
            print("  - " + item)
        commands = result.get("next_commands", [])
        print("建议命令：" + ("无，保持现状即可。" if not commands else ""))
        for command in commands:
            print("  " + command)
        return
    if not result.get("ok", True):
        print("执行未完成：" + str(result.get("error") or "无附加信息"), file=sys.stderr)
        return
    if action == "install":
        if result.get("dry_run"):
            print(f"小李破甲 {VERSION} · 预演（未写入任何文件）")
            print("配置目录：" + str(result.get("codex_home")))
            print("目标注入点：" + result["target_key"] + f"（模式 {result['target_mode']}）")
            print("当前引用：" + (result["current_reference"] or "无（将使用内置基础指令）"))
            print("需要写入：" + ("是" if result["would_write"] else "否（内容已一致）"))
            print("会触发引用修复：" + ("是" if result["reference_repaired"] else "否"))
            print(f"合成后的指令正文：{result['prompt_bytes']} 字节；"
                  f"名称迁移 {result['migration']['name_replacements']} 处，"
                  f"删除旧语句 {result['migration']['lines_removed']} 行")
            return
        print(f"小李破甲 {VERSION}")
        # 先亮明实际操作的配置目录：指错目录是这套工具最危险的误操作。
        print("配置目录：" + str(result.get("codex_home")))
        if result.get("reference_repaired"):
            print("已按当前配置重建安装记录；原记录、旧指令与首次备份已归档：")
            print("  历史快照：" + str(result.get("recovery_path")))
        print("本地配置已就绪（内容未变，未写入任何文件）。" if result.get("unchanged")
              else "本地配置已写入；外部原文件与首次备份均保留。")
        for warning in result.get("warnings", []):
            print("提示：" + warning)
        if result["migration"]["name_replacements"] or result["migration"]["lines_removed"]:
            print(f"名称迁移 {result['migration']['name_replacements']} 处，"
                  f"删除旧语句 {result['migration']['lines_removed']} 行。")
        print("指令副本：" + str(result.get("prompt_path")))
        print("注入点：" + result["target_key"] + f"（模式 {result['target_mode']}）")
        print("普通问候（hi / 你好）预期回复：\n" + result["expected_greeting"])
        print("在 Codex 新任务中输入：" + result["passphrase_trigger"])
        print("预期回复：\n" + result["expected_reply"])
        print("已运行的旧任务可能仍在用旧指令；安装器不会终止正在进行的任务。")
        if result.get("open_error"):
            print("未能自动打开 Codex，请从开始菜单打开。")
        return
    if action == "restore":
        print("配置目录：" + str(result.get("codex_home")))
        print("已恢复安装前的指令引用。" if result.get("restored") else "没有需要恢复的安装记录。")
        for path in result.get("retained_paths", []):
            print("保留了手工修改过的副本：" + path)
        return
    if action == "status":
        home = result["codex_home"]
        print("配置目录：" + home)
        if result.get("config_error"):
            print("配置解析失败：" + result["config_error"])
        if result.get("state_error"):
            print("安装状态异常：" + result["state_error"])
        print("本地状态：" + (str(result.get("profile")) if result.get("installed") else "未安装"))
        if result.get("installed"):
            print(f"  注入点：{'.'.join(result.get('keys', []))}（{result.get('target_mode')}）")
            print(f"  指令副本：{result.get('prompt_path')}（存在：{result.get('prompt_exists')}）")
            print(f"  引用一致：{result.get('reference_ok')}，备份：{result.get('backup_exists')}")
        elif result.get("keys"):
            print(f"  当前注入点：{'.'.join(result['keys'])} -> {result.get('current_reference') or '未设置'}")
        return
    if action == "audit":
        print(format_audit(result))
        return
    if action == "emit":
        if result["out_path"]:
            print("已输出合成后的指令文件：" + result["out_path"])
            print(f"来源：{result['source'] or '内置基础指令'}；{result['prompt_bytes']} 字节")
        else:
            print(result["content"], end="" if result["content"].endswith("\n") else "\n")
        return
    if action == "init-profile":
        print("已生成 profile 文件：" + result["profile_path"])
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xiaoli",
        description="小李破甲：为 Codex 配置本地指令副本，统一身份与精确口令。",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--install", action="store_true", help="安装或更新指令配置（默认动作）")
    actions.add_argument("--restore", action="store_true", help="恢复本次安装前的指令引用")
    actions.add_argument("--status", action="store_true", help="只读查看安装状态")
    actions.add_argument("--verify", action="store_true", help="只读校验配置链，不调用模型")
    actions.add_argument("--audit", action="store_true", help="只读脱敏环境审计")
    actions.add_argument("--doctor", action="store_true", help="只读现场诊断并给出下一步命令")
    actions.add_argument("--emit", action="store_true", help="只输出合成后的指令全文，不安装")
    actions.add_argument("--init-profile", action="store_true", help="生成默认 profile.json")
    parser.add_argument("--codex-home", type=Path, help="显式指定 Codex 配置目录")
    parser.add_argument("--profile-file", type=Path, help="指定身份/口令配置文件（默认 profile.json）")
    parser.add_argument("--target", choices=["auto", "root", "profile"], default="auto",
                        help="注入点：顶层键 / 活动 profile 段内键（auto 自动判断）")
    parser.add_argument("--dry-run", action="store_true", help="只预演安装结果，不写入任何文件")
    parser.add_argument("--break-lock", action="store_true",
                        help="强制清理锁文件（含仍标记为活动进程的锁，会先把原锁归档留痕）")
    parser.add_argument("--takeover", action="store_true",
                        help="确认要接管由其他工具写入的注入点（managed-prompts/<其他 id>/）")
    parser.add_argument("--no-open", action="store_true", help="安装后不请求打开 Codex")
    parser.add_argument("--out", type=Path, help="--emit 的输出文件路径")
    parser.add_argument("--force", action="store_true", help="--init-profile 覆盖已存在的 profile")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--version", action="version", version=f"小李破甲 {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _emit_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.init_profile:
            result = cmd_init_profile(args)
        elif args.restore:
            result = cmd_restore(args)
        elif args.status:
            result = cmd_status(args)
        elif args.verify:
            result = cmd_verify(args)
        elif args.audit:
            result = cmd_audit(args)
        elif args.doctor:
            result = cmd_doctor(args)
        elif args.emit:
            result = cmd_emit(args)
        else:
            result = cmd_install(args)
    except (ConfigError, ProfileError, OSError, ValueError, RuntimeError) as exc:
        detail = str(exc).strip() or "无附加信息"
        if args.json:
            print(json.dumps({"ok": False, "action": "error", "error_type": type(exc).__name__,
                              "error": detail}, ensure_ascii=False, indent=2))
        else:
            print(f"执行未完成（{type(exc).__name__}）：{detail}", file=sys.stderr)
        return 2
    print_result(args, result)
    if result.get("action") == "verify":
        return 0 if result.get("ok") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

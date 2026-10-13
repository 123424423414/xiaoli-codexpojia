#!/usr/bin/env python3
"""小李破甲 · 只读环境审计。

只检查本地文件与环境变量的“存在性/一致性”，不调用模型、不联网、不修改任何文件，
也不输出提示词正文、认证信息或完整代理地址。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from xiaoli_engine import TOOL_ID, ConfigDocument, ConfigError, ConfigStore, _bytes

RISK_PATTERNS = {
    "api_key": re.compile(r"api[_-]?key", re.IGNORECASE),
    "token": re.compile(r"\btoken\b", re.IGNORECASE),
    "password": re.compile(r"pass(word|wd)", re.IGNORECASE),
    "secret": re.compile(r"secret", re.IGNORECASE),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    "bearer": re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
}

PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

WATCH_POINTS = (
    ("claude_code_memory", Path.home() / ".claude" / "CLAUDE.md"),
    ("claude_project_memory", Path.cwd() / "CLAUDE.md"),
    ("codex_agents_md", Path.home() / ".codex" / "AGENTS.md"),
    ("project_agents_md", Path.cwd() / "AGENTS.md"),
    ("gemini_memory", Path.home() / ".gemini" / "GEMINI.md"),
    ("cursor_rules", Path.home() / ".cursor" / "rules"),
)


def _digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest()[:12] if raw is not None else None


def audit(codex_home: str | Path | None = None) -> dict:
    store = ConfigStore(codex_home)
    result: dict = {
        "ok": True,
        "action": "audit",
        "tool": TOOL_ID,
        "read_only": True,
        "network_used": False,
        "codex_home": str(store.codex_home),
        "cwd": str(Path.cwd()),
        "config_path": str(store.config_path),
        "managed_dir": str(store.managed_dir),
        "findings": [],
    }

    raw_config = _bytes(store.config_path)
    result["config_exists"] = raw_config is not None
    result["config_size"] = len(raw_config) if raw_config else 0

    doc: ConfigDocument | None = None
    try:
        doc = ConfigDocument(raw_config)
    except ConfigError as exc:
        result["config_error"] = str(exc)
        result["findings"].append("config_invalid: config.toml 无法解析，工具已停止写入。")

    reference_value = None
    reference_path = None
    if doc is not None:
        try:
            keys, mode = store.detect_target(doc, "auto")
            value = doc.lookup(keys)
            result["injection_keys"] = ".".join(keys)
            result["injection_mode"] = mode
            if isinstance(value, str) and value.strip():
                reference_value = value
                reference_path = store.resolve_reference(value)
                result["reference_resolved"] = str(reference_path)
                result["reference_inside_managed_dir"] = (
                    reference_path.parent == store.managed_dir.resolve()
                )
            else:
                result["reference_resolved"] = None
                result["findings"].append("no_reference: 当前注入点没有指向任何指令文件。")
        except ConfigError as exc:
            result["injection_error"] = str(exc)

    prompt_raw = _bytes(reference_path) if reference_path else None
    prompt_info: dict = {
        "exists": prompt_raw is not None,
        "size": len(prompt_raw) if prompt_raw else 0,
        "sha256_12": _digest(prompt_raw),
    }
    if prompt_raw is not None:
        try:
            text = prompt_raw.decode("utf-8-sig")
        except UnicodeError:
            text = None
            prompt_info["utf8"] = False
            result["findings"].append("prompt_not_utf8: 指令文件不是有效 UTF-8。")
        else:
            prompt_info["utf8"] = True
            prompt_info["lines"] = len(text.splitlines())
            prompt_info["marker_begin"] = text.count("<!-- xiaoli-armor:begin -->")
            prompt_info["marker_end"] = text.count("<!-- xiaoli-armor:end -->")
            hits = {name: len(pattern.findall(text)) for name, pattern in RISK_PATTERNS.items()}
            prompt_info["risk_keyword_hits"] = {name: count for name, count in hits.items() if count}
    result["prompt_file"] = prompt_info

    state_raw = _bytes(store.state_path)
    state_info: dict = {"exists": state_raw is not None}
    state = None
    if state_raw is not None:
        try:
            state = store.read_state()
        except ConfigError as exc:
            state_info["error"] = str(exc)
            result["findings"].append("state_invalid: 安装状态文件无法读取，恢复前请先人工核对。")
    if state:
        target = store._managed_prompt(state["prompt_path"])
        target_raw = _bytes(target)
        state_info.update({
            "profile": state.get("profile_title"),
            "installed_at": state.get("installed_at"),
            "prompt_path": str(target),
            "owned_prompts": len(state.get("owned_prompts", {})),
            "prompt_digest_matches": state.get("owned_prompts", {}).get(str(target)) == _digest_full(target_raw),
            "state_is_current": store.state_is_current(state, doc),
        })
    result["install_state"] = state_info
    result["backup_exists"] = store.backup_path.is_file()
    history = store.managed_dir / "history"
    result["history_snapshots"] = len(list(history.iterdir())) if history.is_dir() else 0

    result["env_codex_home_set"] = bool(os.environ.get("CODEX_HOME"))
    result["proxy_env_set"] = {name: bool(os.environ.get(name)) for name in PROXY_VARS}
    result["other_instruction_files"] = [
        {"id": name, "path": str(path), "exists": path.exists()} for name, path in WATCH_POINTS
    ]
    result["checked_paths"] = [
        str(store.config_path), str(store.state_path), str(store.backup_path), str(store.managed_dir / "history"),
    ] + ([str(reference_path)] if reference_path else [])
    result["skipped_checks"] = [
        "system_proxy_settings",
        "traffic_decryption_inspection",
        "remote_api_receiver_behavior",
        "model_side_compliance",
    ]

    if reference_path is not None and reference_path.parent != store.managed_dir.resolve():
        result["findings"].append(
            "external_reference: 当前指令文件不在本工具目录内，恢复/重装只按状态记录处理，请先确认来源。"
        )
    if result["proxy_env_set"].get("HTTPS_PROXY") or result["proxy_env_set"].get("https_proxy"):
        result["findings"].append("proxy_present: 检测到代理环境变量；本检查不判断代理是否解密流量。")
    if prompt_info.get("risk_keyword_hits"):
        result["findings"].append("risk_keywords: 指令文件里出现了疑似认证信息关键词，请人工核对（本检查不回显内容）。")
    if state_info.get("exists") and not state_info.get("state_is_current", False):
        result["findings"].append("state_stale: 安装状态与当前引用不一致，重装会先做历史快照再重建。")
    return result


def _digest_full(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def format_audit(report: dict) -> str:
    lines = ["小李破甲 · 只读环境审计（不修改任何文件，不联网）", ""]
    lines.append(f"配置目录：{report['codex_home']}")
    lines.append(f"config.toml：{'存在' if report.get('config_exists') else '不存在'}"
                 f"（{report.get('config_size', 0)} 字节）")
    if "config_error" in report:
        lines.append(f"配置解析：失败 - {report['config_error']}")
    if "injection_keys" in report:
        lines.append(f"注入点：{report['injection_keys']}（模式 {report['injection_mode']}）")
    if report.get("reference_resolved"):
        lines.append(f"指令文件：{report['reference_resolved']}")
        prompt = report.get("prompt_file", {})
        lines.append(f"  文件状态：{'存在' if prompt.get('exists') else '缺失'}"
                     f"，{prompt.get('size', 0)} 字节，sha256:{prompt.get('sha256_12') or '-'}")
        if prompt.get("marker_begin") is not None:
            lines.append(f"  管理块：begin={prompt.get('marker_begin')} end={prompt.get('marker_end')}")
        if prompt.get("risk_keyword_hits"):
            hits = ", ".join(f"{k}×{v}" for k, v in prompt["risk_keyword_hits"].items())
            lines.append(f"  疑似认证关键词：{hits}（仅计数，不回显）")
    state = report.get("install_state", {})
    lines.append(f"安装状态：{'存在' if state.get('exists') else '不存在'}")
    if state.get("installed_at"):
        lines.append(f"  安装于 {state.get('installed_at')}，档位 {state.get('profile')}，"
                     f"副本 {state.get('owned_prompts')} 个，与当前引用一致：{state.get('state_is_current')}")
    lines.append(f"首次备份：{'存在' if report.get('backup_exists') else '不存在'}")
    lines.append(f"历史快照：{report.get('history_snapshots', 0)} 个")
    proxies = [name for name, present in report.get("proxy_env_set", {}).items() if present]
    lines.append(f"代理环境变量：{'、'.join(proxies) if proxies else '未设置'}")
    others = [item for item in report.get("other_instruction_files", []) if item["exists"]]
    lines.append("其他检测到的指令文件：" + ("、".join(item["path"] for item in others) if others else "无"))
    lines.append("")
    lines.append("本次实际检查：" + "；".join(report.get("checked_paths", [])))
    lines.append("本次未检查（不声称已覆盖）：" + "、".join(report.get("skipped_checks", [])))
    lines.append("")
    findings = report.get("findings", [])
    if findings:
        lines.append("结论：")
        lines.extend(f"  - {item}" for item in findings)
    else:
        lines.append("结论：未发现需要人工处理的提示项（这不等同于不可提取）。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="小李破甲只读环境审计")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = audit(args.codex_home)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_audit(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

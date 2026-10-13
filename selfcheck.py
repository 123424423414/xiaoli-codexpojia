#!/usr/bin/env python3
"""小李破甲 · 自检脚本（端到端 + 对抗性验收）。

覆盖：TOML 定位对抗样例（18）、事务/并发/锁（4）、状态机与边界（11）、
生成内容断言与篡改检测（7）、交付物与 Windows 入口（8，含独立 EXE 冒烟、
批处理编码/换行、产物解压后 .cmd 实跑）、现场诊断与防误操作（7）、
崩溃残留现场（3 个场景 + 归档断言）、profile 段含子表时的注入回归（1）。

只在临时目录里运行，不触碰真实 CODEX_HOME，不启动真实 Codex；
唯一会改动的仓库内文件是 downloads/ 下的打包产物（重新生成并自校验）。

用法：python selfcheck.py
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
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import build_package  # noqa: E402
import xiaoli  # noqa: E402
from xiaoli_engine import (  # noqa: E402
    KEY,
    ConfigConflictError,
    ConfigDocument,
    ConfigError,
    ConfigStore,
    _bytes,
)

PY = sys.executable
CLI = ROOT / "xiaoli.py"
# 自检固定用自己的一份 profile：既不受用户当前配置影响，也不会因为用户改口令而变红。
PROFILE_FILE: Path | None = None
PROFILE_REPLY = "自检第一行\n频道@SELFCHECK    群号000000"
PROFILE_GREETING = "「你好」\n自检在。👋"
RESULTS: list[tuple[str, str, bool, str]] = []


def check(case_id: str, name: str, ok: bool, evidence: str = "") -> None:
    RESULTS.append((case_id, name, bool(ok), evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {case_id} {name}" + (f"  ::  {evidence}" if evidence else ""), flush=True)


def tree(root: Path) -> dict[str, tuple[int, int, str]]:
    out: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            out[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def strip_key(node: object, keys: tuple[str, ...]) -> object:
    if not keys or not isinstance(node, dict):
        return node
    name, rest = keys[0], keys[1:]
    out = {k: (strip_key(v, rest) if k == name else v) for k, v in node.items()}
    out.pop(name, None)
    return out


def new_home(root: Path, name: str, config: str | None = 'model_instructions_file = "./prompts/base.md"\napproval_policy = "on-request"\n') -> Path:
    home = root / name
    (home / "prompts").mkdir(parents=True, exist_ok=True)
    (home / "prompts" / "base.md").write_bytes("# base\n正文\n".encode("utf-8"))
    if config is not None:
        (home / "config.toml").write_bytes(config.encode("utf-8"))
    return home


def run_cli(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    extra = ["--profile-file", str(PROFILE_FILE)] if PROFILE_FILE is not None else []
    proc = subprocess.run([PY, str(CLI), *extra, *args], capture_output=True, text=True, encoding="utf-8",
                          cwd=str(cwd or ROOT), timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def install_json(home: Path, *extra: str) -> tuple[int, dict | None, str]:
    code, out, err = run_cli("--codex-home", str(home), "--install", "--no-open", "--json", *extra)
    payload = None
    if out.strip().startswith("{"):
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
    return code, payload, (err or out)


# --------------------------------------------------------------------------
# A. TOML 定位与定点改写（对抗样例 + 语义等价）
# --------------------------------------------------------------------------

SAMPLE_TRIPLE_DOUBLE = 'note = """\nmodel_instructions_file = "no"\n"""\nmodel_instructions_file = "yes"\n'
SAMPLE_TRIPLE_SINGLE = "note = '''\nmodel_instructions_file = \"no\"\n'''\nmodel_instructions_file = \"yes\"\n"

A_CASES: list[tuple[str, str, tuple[str, ...], str | None]] = [
    ("A1", 'a = 1\nmodel_instructions_file = "./base.md"\nb = 2\n', (KEY,),
     'a = 1\nmodel_instructions_file = "NEW"\nb = 2\n'),
    ("A2", '# model_instructions_file = "no"\nmodel_instructions_file = "yes"\n', (KEY,),
     '# model_instructions_file = "no"\nmodel_instructions_file = "NEW"\n'),
    ("A3", SAMPLE_TRIPLE_DOUBLE, (KEY,),
     'note = """\nmodel_instructions_file = "no"\n"""\nmodel_instructions_file = "NEW"\n'),
    ("A4", SAMPLE_TRIPLE_SINGLE, (KEY,),
     "note = '''\nmodel_instructions_file = \"no\"\n'''\nmodel_instructions_file = \"NEW\"\n"),
    ("A5", 'points = [\n  { x = 1, y = 2 },\n  { model_instructions_file = "no" },\n]\nmodel_instructions_file = "yes"\n',
     (KEY,), 'points = [\n  { x = 1, y = 2 },\n  { model_instructions_file = "no" },\n]\nmodel_instructions_file = "NEW"\n'),
    ("A6", '[profiles.work]\nmodel = "gpt-5"\nmodel_instructions_file = "./p.md"\n', ("profiles", "work", KEY),
     '[profiles.work]\nmodel = "gpt-5"\nmodel_instructions_file = "NEW"\n'),
    ("A7", 'profiles.work.model_instructions_file = "./p.md"\n', ("profiles", "work", KEY),
     'profiles.work.model_instructions_file = "NEW"\n'),
    ("A8", '[profiles."my work"]\nmodel_instructions_file = "./p.md"\n', ("profiles", "my work", KEY),
     '[profiles."my work"]\nmodel_instructions_file = "NEW"\n'),
    ("A9", '[[items]]\nname = "a"\nmodel_instructions_file = "old"\n', ("items", KEY),
     '[[items]]\nname = "a"\nmodel_instructions_file = "NEW"\n'),
    ("A10", 'a = 1\r\nmodel_instructions_file = "x"\r\nb = 2\r\n', (KEY,),
     'a = 1\r\nmodel_instructions_file = "NEW"\r\nb = 2\r\n'),
    ("A11", 'model_instructions_file = "./base.md"\nnote = "# [not a header]"\n', (KEY,),
     'model_instructions_file = "NEW"\nnote = "# [not a header]"\n'),
    ("A12", 'a = 1\nmodel_instructions_file = "x"', (KEY,),
     'a = 1\nmodel_instructions_file = "NEW"\n'),
    ("A13", 'model_instructions_file = "./base.md#frag" # tail\n', (KEY,),
     'model_instructions_file = "NEW" # tail\n'),
    ("A14", 'a = 1 # keep\nmodel_instructions_file = "./base.md"   # 注释\n', (KEY,),
     'a = 1 # keep\nmodel_instructions_file = "NEW"   # 注释\n'),
    ("A15", 'note = "文本里有 [表头样子] 和 # 号"\nmodel_instructions_file = "x"\n', (KEY,),
     'note = "文本里有 [表头样子] 和 # 号"\nmodel_instructions_file = "NEW"\n'),
]


def case_a(tmp: Path) -> None:
    for case_id, text, keys, expected in A_CASES:
        raw = text.encode("utf-8")
        try:
            doc = ConfigDocument(raw)
            out = doc.set_assignment(keys, '"NEW"')
            decoded = out.decode("utf-8-sig")
            same_bytes = decoded == expected
            before = strip_key(tomllib.loads(text), keys)
            after = strip_key(tomllib.loads(decoded), keys)
            semantic = before == after
            value_ok = tomllib.loads(decoded)  # 仍可解析
            ok = same_bytes and semantic and bool(value_ok)
            evidence = "字节完全符合预期" if same_bytes else f"实际={decoded!r}"
            if not semantic:
                evidence += "；语义不等价"
            check(case_id, f"定位/改写样例 {case_id}", ok, evidence)
        except Exception as exc:  # noqa: BLE001
            check(case_id, f"定位/改写样例 {case_id}", False, f"{type(exc).__name__}: {exc}")

    # BOM 单独判定（前缀必须保留）
    raw = b"\xef\xbb\xbfmodel_instructions_file = \"x\"\n"
    out = ConfigDocument(raw).set_assignment((KEY,), '"NEW"')
    check("A16", "BOM 保留", out.startswith(b"\xef\xbb\xbf") and out[3:] == b'model_instructions_file = "NEW"\n', f"{out[:20]!r}")

    # 段内键不会被顶层安装误伤
    doc = ConfigDocument(b'[profiles.work]\nmodel_instructions_file = "./p.md"\n')
    untouched = doc.find((KEY,)) is None and doc.find(("profiles", "work", KEY)) is not None
    check("A17", "段内同名键不被当作顶层键", untouched, f"顶层={doc.find((KEY,))}")

    # 顶层插入会落在第一个表头之前
    doc = ConfigDocument(b'a = 1\n\n[profiles.work]\nmodel = "gpt-5"\n')
    out = doc.set_assignment((KEY,), '"NEW"').decode()
    check("A18", "顶层插入位置在第一个表头之前", out.index(KEY) < out.index("[profiles.work]") and tomllib.loads(out)[KEY] == "NEW", out.replace("\n", "\\n"))


# --------------------------------------------------------------------------
# B. 事务、并发与锁
# --------------------------------------------------------------------------


def case_b(tmp: Path) -> None:
    # B1 陈旧输入快照 → 拒绝且文件不变
    home = new_home(tmp, "b1")
    store = ConfigStore(home)
    before = tree(home)
    try:
        store.install_content("# 新\n", "小李身份与口令.md", title="t",
                              expected_inputs={store.config_path: b"stale"})
        check("B1", "输入快照不一致时拒绝写入", False, "未抛异常")
    except ConfigConflictError as exc:
        check("B1", "输入快照不一致时拒绝写入", tree(home) == before, str(exc)[:60])
    except Exception as exc:  # noqa: BLE001
        check("B1", "输入快照不一致时拒绝写入", False, f"{type(exc).__name__}: {exc}")

    # B2 真实跨进程持锁：默认拒绝；--break-lock 是显式接管（清理并归档原锁）
    home = new_home(tmp, "b2")
    lock = home / "managed-prompts" / "xiaoli-armor" / "install.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [PY, "-c", "import os,sys,time,pathlib;"
                   f"p=pathlib.Path(r'{lock}');p.write_text('{{\"pid\": %d, \"created_at\": \"holder\"}}' % os.getpid());"
                   "time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while not lock.exists() and time.time() < deadline:
            time.sleep(0.05)
        code, _payload, text_out = install_json(home)
        refused = code == 2 and "正在运行" in text_out
        code2, payload2, _text_out2 = install_json(home, "--break-lock")
        took_over = code2 == 0 and payload2 is not None and payload2.get("config_installed") is True
        archived = list((home / "managed-prompts" / "xiaoli-armor" / "history").glob("lock-*/install.lock"))
        check("B2", "其他进程持锁时默认拒绝；--break-lock 显式接管并归档原锁",
              refused and took_over and len(archived) == 1 and not lock.exists(),
              f"默认拒绝={refused} / 强制接管 exit={code2} / 归档锁={len(archived)}")
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()

    # B3 崩溃残留锁（记录着一个已退出进程）。
    # 注意：幂等重装不写盘、因此根本不取锁（不会被陈旧锁挡住）；
    # 这里先把现场改成「需要写入」，才能真正验证锁语义。
    (home / "config.toml").write_bytes(b'model_instructions_file = "./prompts/other.md"\n')
    (home / "prompts" / "other.md").write_bytes("# 另一份指令\n".encode("utf-8"))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999999, "created_at": "crashed"}), encoding="utf-8")
    code, _payload, text_out = install_json(home)
    stale_refused = code == 2 and "正在运行" in text_out
    code2, payload2, _text_out2 = install_json(home, "--break-lock")
    recovered = code2 == 0 and bool(payload2 and payload2.get("config_installed"))
    check("B3", "崩溃残留锁：需要写入的操作默认拒绝、--break-lock 接管",
          stale_refused and recovered and not lock.exists(),
          f"默认拒绝={stale_refused} / 恢复 exit={code2} / 锁残留={lock.exists()}")

    # B4 两个进程同时安装：至少一个成功，最终状态一致
    home = new_home(tmp, "b4")
    procs = [subprocess.Popen([PY, str(CLI), "--codex-home", str(home), "--install", "--no-open", "--json"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
             for _ in range(2)]
    outs = [proc.communicate(timeout=120) for proc in procs]
    codes = sorted(proc.returncode for proc in procs)
    store = ConfigStore(home) if False else None  # noqa: F841 - 保留占位，避免误用
    status = json.loads(run_cli("--codex-home", str(home), "--status", "--json")[1])
    ok = codes[0] == 0 and status.get("reference_ok")
    details = " | ".join((out or err).strip().replace("\n", " ")[:150] for out, err in outs)
    check("B4", "两个进程同时安装：至少一个成功且最终一致", ok,
          f"退出码={codes}；引用一致={status.get('reference_ok')}；{details}")


# --------------------------------------------------------------------------
# C. 状态机：幂等、修复、恢复、边界
# --------------------------------------------------------------------------


def case_c(tmp: Path) -> None:
    home = new_home(tmp, "c1")
    code, payload, text_out = install_json(home)
    if code != 0:
        check("C1", "首次安装", False, text_out[:120])
        return
    check("C1", "首次安装成功并写入状态", payload is not None and payload["config_installed"] and not payload["model_reply_verified"],
          f"副本={Path(payload['prompt_path']).name}")

    # C2 幂等：连续两次安装零写入
    before = tree(home)
    code2, payload2, _ = install_json(home)
    code3, payload3, _ = install_json(home)
    unchanged = bool(payload2 and payload2["unchanged"]) and bool(payload3 and payload3["unchanged"])
    check("C2", "重复安装零写入（字节与 mtime 均不变）", unchanged and tree(home) == before,
          f"unchanged={payload2['unchanged']}/{payload3['unchanged']}；树一致={tree(home) == before}")

    # C3 只读动作零写入
    before = tree(home)
    for args in (("--status", "--json"), ("--verify",), ("--audit", "--json"), ("--emit", "--json"), ("--install", "--dry-run", "--json")):
        run_cli("--codex-home", str(home), *args)
    check("C3", "status/verify/audit/emit/dry-run 零写入", tree(home) == before, "")

    # C4 引用修复：先归档再重建
    (home / "config.toml").write_bytes('model_instructions_file = "./prompts/other.md"\n'.encode())
    (home / "prompts" / "other.md").write_bytes("# 另一份指令\n".encode())
    code4, payload4, text_out4 = install_json(home)
    repaired = bool(payload4 and payload4["reference_repaired"])
    recovery = Path(payload4["recovery_path"]) if payload4 and payload4.get("recovery_path") else None
    archived = bool(recovery and (recovery / "install-state.json").is_file()
                    and (recovery / "config-before-repair.toml").is_file()
                    and any((recovery / "prompts").glob("*.md")))
    check("C4", "引用修复：先归档再重建", code4 == 0 and repaired and archived,
          f"归档={recovery.name if recovery else None}")

    # C5 恢复回到修复前的当前引用，且可重复运行
    code5, out5, _ = run_cli("--codex-home", str(home), "--restore", "--json")
    restored = json.loads(out5)["restored"] if code5 == 0 else False
    config_now = (home / "config.toml").read_text(encoding="utf-8")
    code6, out6, _ = run_cli("--codex-home", str(home), "--restore", "--json")
    again = json.loads(out6)["restored"] if code6 == 0 else True
    check("C5", "恢复回到修复前引用且可重复", restored and not again and 'model_instructions_file = "./prompts/other.md"' in config_now,
          config_now.strip())
    check("C6", "历史快照在恢复后仍保留", bool(recovery and recovery.is_dir()), str(recovery))

    # C7 手改副本 → 恢复保留
    home = new_home(tmp, "c7")
    code7, payload7, _ = install_json(home)
    prompt = Path(payload7["prompt_path"])
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\n手工补充\n", encoding="utf-8")
    code8, out8, _ = run_cli("--codex-home", str(home), "--restore", "--json")
    data8 = json.loads(out8)
    check("C7", "手工修改过的副本不被删除", prompt.is_file() and str(prompt) in data8.get("retained_paths", []),
          f"retained={len(data8.get('retained_paths', []))}")

    # C8 安装前没有 config → 安装创建 → 恢复删除
    home = new_home(tmp, "c8", config=None)
    code9, payload9, _ = install_json(home)
    created = (home / "config.toml").is_file()
    run_cli("--codex-home", str(home), "--restore", "--json")
    removed = not (home / "config.toml").exists()
    check("C8", "无 config 时创建、恢复后删除", code9 == 0 and created and removed, f"创建={created} 删除={removed}")

    # C9 引用指向本工具的状态/备份 → 拒绝
    home = new_home(tmp, "c9")
    install_json(home)
    backup = home / "managed-prompts" / "xiaoli-armor" / "config.toml.before-xiaoli.bak"
    (home / "config.toml").write_bytes(f'model_instructions_file = "{backup.as_posix()}"\n'.encode())
    code10, _payload10, text_out10 = install_json(home)
    check("C9", "引用指向本工具保留文件时拒绝", code10 == 2 and "未修改任何配置" in text_out10, text_out10.strip()[:70])

    # C10 非法输入：非 UTF-8 / 空白 / 坏 TOML
    results = []
    for name, payload_bytes in (("非UTF8", b"\xff\xfe\x00bad"), ("空白", b"   \n")):
        home = new_home(tmp, f"c10-{name}")
        (home / "prompts" / "base.md").write_bytes(payload_bytes)
        code11, _p, text_out11 = install_json(home)
        results.append(code11 == 2 and "managed-prompts" not in (home / "config.toml").read_text(encoding="utf-8"))
    home = new_home(tmp, "c10-badtoml", config=None)
    (home / "config.toml").write_bytes(b'model_instructions_file = "./prompts/base.md"`nbroken\n')
    code12, _p12, text_out12 = install_json(home)
    results.append(code12 == 2 and "TOML" in text_out12)
    check("C10", "非 UTF-8 / 空白 / 非法 TOML 均拒绝且不留改动", all(results), f"{results}")

    # C11 其他工具占用 → 默认拒绝，--takeover 接管且不动对方文件
    home = new_home(tmp, "c11", config=None)
    foreign_dir = home / "managed-prompts" / "xiaoguai-oneclick"
    foreign_dir.mkdir(parents=True)
    foreign = foreign_dir / "active.md"
    foreign.write_bytes("# 另一个工具的副本\n".encode())
    (home / "config.toml").write_bytes(f'model_instructions_file = "{foreign.as_posix()}"\n'.encode())
    code13, _p13, text_out13 = install_json(home)
    refused = code13 == 2 and "--takeover" in text_out13
    code14, payload14, _ = install_json(home, "--takeover")
    took_over = code14 == 0 and foreign.is_file() and not (foreign_dir / "install-state.json").exists()
    check("C11", "其他工具占用：默认拒绝、--takeover 接管且不破坏对方文件", refused and took_over,
          f"拒绝={refused} 接管={took_over}")


# --------------------------------------------------------------------------
# D. 生成内容断言
# --------------------------------------------------------------------------


def case_d(tmp: Path) -> None:
    home = new_home(tmp, "d1")
    code, payload, _ = install_json(home)
    text = Path(payload["prompt_path"]).read_text(encoding="utf-8")
    check("D1", "管理块恰好一对且顺序正确",
          text.count("<!-- xiaoli-armor:begin -->") == 1 and text.count("<!-- xiaoli-armor:end -->") == 1
          and text.index("begin") < text.index("end"), "")
    lines = payload["expected_reply"].split("\n")
    second = lines[1] if len(lines) > 1 else ""
    separated = "    " in second and "     " not in second
    check("D2", "确认口令恰好两行、第二行以恰好 4 个半角空格分隔",
          len(lines) == 2 and separated, f"第二行={second!r}")
    check("D3", "普通问候恰好两行",
          payload["expected_greeting"].split("\n") == PROFILE_GREETING.split("\n"),
          repr(payload["expected_greeting"]))
    check("D4", "外部原文完整保留在副本里", "# base\n正文\n" in text, "")
    lowered = text.lower()
    check("D5", "副本不含本机绝对路径之外的敏感内容标记", "SECRET" not in text and "api_key" not in lowered, "")

    # D6 verify 全绿、篡改副本后 verify 必须变红
    code2, out2, _ = run_cli("--codex-home", str(home), "--verify")
    green = code2 == 0 and "结论：本地配置链完整。" in out2
    prompt = Path(payload["prompt_path"])
    original = prompt.read_bytes()
    # 与文案无关的篡改：只要字节变了，verify 的 state_digest 就必须变红
    prompt.write_bytes(original + b"\n<!-- selfcheck-tamper -->\n")
    code3, out3, _ = run_cli("--codex-home", str(home), "--verify")
    red = code3 == 1 and "未通过" in out3
    prompt.write_bytes(original)
    check("D6", "verify 全绿；副本被篡改后必须变红", green and red, f"篡改前 exit={code2} / 篡改后 exit={code3}")

    # D7 audit 不回显正文、不写出代理地址
    secret_home = new_home(tmp, "d7")
    (secret_home / "prompts" / "base.md").write_bytes(b"# base\nSECRET_MARKER_9\n")
    env = dict(os.environ, HTTPS_PROXY="http://127.0.0.1:7890")
    proc = subprocess.run([PY, str(CLI), "--codex-home", str(secret_home), "--audit", "--json"],
                          capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
    blob = proc.stdout
    check("D7", "audit 不回显正文、不输出代理地址",
          "SECRET_MARKER_9" not in blob and "7890" not in blob and "127.0.0.1" not in blob
          and json.loads(blob)["proxy_env_set"]["HTTPS_PROXY"] is True, "")


# --------------------------------------------------------------------------
# E. 打包与产物
# --------------------------------------------------------------------------


def case_e(tmp: Path) -> None:
    package = ROOT / "downloads" / "xiaoli-armor-v1.0.0.zip"
    if package.exists():
        package.unlink()
    try:
        report = build_package.build(package)
    except Exception as exc:  # noqa: BLE001
        check("E1", "白名单打包", False, f"{type(exc).__name__}: {exc}")
        return
    verify = build_package.verify(package)
    check("E1", "白名单打包 + 自校验通过", report["ok"] and verify["ok"],
          f"成员={report['members']} sha256={report['sha256'][:12]}")

    # 篡改一个字节后必须被检出
    tampered = tmp / "tampered.zip"
    raw = bytearray(package.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    tampered.write_bytes(bytes(raw))
    bad = build_package.verify(tampered)
    check("E2", "产物篡改后校验必须失败", not bad["ok"] and bool(bad["problems"]),
          (bad["problems"][0][:60] if bad["problems"] else "无问题？"))

    # 解压后的包能独立跑通安装/校验/恢复
    unpack = tmp / "unpacked"
    shutil.unpack_archive(str(package), str(unpack))
    home = new_home(tmp, "e3")
    code, payload, text_out = run_cli("--codex-home", str(home), "--install", "--no-open", "--json",
                                      cwd=unpack)
    cli_ok = code == 0 and payload is not None
    verify_proc = subprocess.run([PY, str(unpack / "xiaoli.py"), "--profile-file", str(PROFILE_FILE),
                                  "--codex-home", str(home), "--verify"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
    restore_proc = subprocess.run([PY, str(unpack / "xiaoli.py"), "--profile-file", str(PROFILE_FILE),
                                   "--codex-home", str(home), "--restore", "--json"],
                                  capture_output=True, text=True, encoding="utf-8", timeout=60)
    restored = bool(restore_proc.stdout) and json.loads(restore_proc.stdout).get("restored") is True
    verified = verify_proc.returncode == 0 and "本地配置链完整。" in verify_proc.stdout
    check("E3", "解压后的包可独立完成安装/校验/恢复", cli_ok and verified and restored,
          f"安装 exit={code} / 校验 exit={verify_proc.returncode} / 恢复={restored}")

    # 可执行位是否写进产物
    import zipfile

    with zipfile.ZipFile(package) as archive:
        modes = {info.filename: (info.external_attr >> 16) & 0o777 for info in archive.infolist()}
        packed = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    scripts = [name for name in modes if name.endswith((".command", ".sh"))]
    check("E4", "入口脚本在产物里带可执行位", scripts and all(modes[name] == 0o755 for name in scripts),
          f"{[(name, oct(modes[name])) for name in scripts]}")

    # E5 独立 EXE：同目录存在时必须能独立完成安装/校验/恢复
    exe = ROOT / "小李破甲安装器.exe"
    if exe.is_file():
        home = new_home(tmp, "e5")
        install = subprocess.run([str(exe), "--codex-home", str(home), "--install", "--no-open", "--json"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=240)
        verify = subprocess.run([str(exe), "--codex-home", str(home), "--verify"],
                                capture_output=True, text=True, encoding="utf-8", timeout=240)
        restore = subprocess.run([str(exe), "--codex-home", str(home), "--restore", "--json"],
                                 capture_output=True, text=True, encoding="utf-8", timeout=240)
        restored = bool(restore.stdout.strip()) and json.loads(restore.stdout).get("restored") is True
        check("E5", "独立 EXE 可完成安装/校验/恢复（目标机无需 Python）",
              install.returncode == 0 and verify.returncode == 0 and restored,
              f"安装 exit={install.returncode} / 校验 exit={verify.returncode} / 恢复={restored}")
    else:
        check("E5", "独立 EXE 可完成安装/校验/恢复（目标机无需 Python）", False,
              f"未找到 {exe.name}，请先运行 python build_exe.py")

    # E6 脚本编码与换行：批处理必须纯 ASCII + CRLF，POSIX 脚本必须 LF
    problems: list[str] = []
    for name in ("启动小李破甲.cmd", "卸载小李破甲.cmd", "查看小李状态.cmd", "检查环境.cmd"):
        raw = (ROOT / name).read_bytes()
        if any(byte > 127 for byte in raw):
            problems.append(f"{name} 含非 ASCII 字节（cmd 会按系统代码页解析而错位）")
        if b"\n" in raw.replace(b"\r\n", b""):
            problems.append(f"{name} 含裸 LF（cmd 的 goto 标签查找会失败）")
    for name in ("启动小李破甲.command", "macos/launch.sh"):
        if b"\r\n" in (ROOT / name).read_bytes():
            problems.append(f"{name} 含 CRLF（POSIX 脚本需要 LF）")
    check("E6", "批处理纯 ASCII+CRLF、POSIX 脚本 LF", not problems, "；".join(problems) or "编码与换行均正确")

    # E7 产物解压后，包内批处理必须能真的跑起来（状态入口自带 --status，只追加其他选项）
    home = new_home(tmp, "e7")
    proc = subprocess.run(["cmd", "/c", "查看小李状态.cmd", "--codex-home", str(home), "--json"],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(unpack), timeout=240)
    status_ok = proc.returncode == 0 and '"action": "status"' in proc.stdout
    # 安装入口自带 --install；两者都不应需要目标机装 Python（同目录有 EXE）
    install_proc = subprocess.run(["cmd", "/c", "启动小李破甲.cmd", "--codex-home", str(home),
                                   "--no-open", "--json"],
                                  capture_output=True, text=True, encoding="utf-8", cwd=str(unpack), timeout=240)
    install_ok = install_proc.returncode == 0 and '"config_installed": true' in install_proc.stdout
    check("E7", "解压后的包内 .cmd 可实际运行（状态 + 安装）", status_ok and install_ok,
          f"状态 exit={proc.returncode} / 安装 exit={install_proc.returncode}"
          + ("" if status_ok and install_ok else f" / {proc.stderr.strip()[:70]}"))

    # E8 产物内的批处理必须是 CRLF 版本
    crlf_ok = True
    for name, raw in packed.items():
        if name.endswith((".cmd", ".bat")):
            crlf_ok = crlf_ok and b"\n" not in raw.replace(b"\r\n", b"")
    check("E8", "产物内的批处理已是 CRLF", crlf_ok and any(n.endswith(".cmd") for n in packed), "")


# --------------------------------------------------------------------------
# F. 现场诊断与防误操作
# --------------------------------------------------------------------------


def case_f(tmp: Path) -> None:
    cli_home = lambda name: str(new_home(tmp, name))  # noqa: E731

    # F1 未安装（连 config.toml 都还没有）
    home = Path(new_home(tmp, "f1", config=None))
    code, out, _ = run_cli("--codex-home", str(home), "--doctor", "--json")
    report = json.loads(out) if out.strip().startswith("{") else {}
    check("F1", "doctor：未安装现场给出安装建议",
          code == 0 and report.get("situation") == "未安装" and "--install" in " ".join(report.get("next_commands", [])),
          f"exit={code} / 现状={report.get('situation')}")

    # F2 已安装且一致
    install_json(home)
    code, out, _ = run_cli("--codex-home", str(home), "--doctor", "--json")
    report = json.loads(out)
    check("F2", "doctor：已安装且引用一致时判定为正常",
          code == 0 and report.get("situation") == "已安装且引用一致" and report.get("severity") == "ok",
          f"现状={report.get('situation')} / 级别={report.get('severity')}")

    # F3 引用被外部改动
    (home / "config.toml").write_bytes(b'model_instructions_file = "./prompts/other.md"\n')
    (home / "prompts" / "other.md").write_bytes("# 另一份\n".encode("utf-8"))
    code, out, _ = run_cli("--codex-home", str(home), "--doctor", "--json")
    report = json.loads(out)
    check("F3", "doctor：引用被外部改动时给出归档重建建议",
          report.get("situation") == "配置引用被外部改动"
          and "--install" in " ".join(report.get("next_commands", [])),
          f"现状={report.get('situation')}")

    # F4 副本被手改后重装：换新副本、显式提示、旧文件保留
    home = Path(cli_home("f4"))
    _code, payload, _ = install_json(home)
    prompt = Path(payload["prompt_path"])
    prompt.write_text(prompt.read_text(encoding="utf-8").replace("正文", "正文（手改）", 1), encoding="utf-8")
    code, out, _ = run_cli("--codex-home", str(home), "--install", "--no-open", "--json")
    payload2 = json.loads(out) if out.strip().startswith("{") else {}
    warned = bool(payload2.get("warnings")) and "手工修改" in " ".join(payload2.get("warnings", []))
    check("F4", "手改副本后重装：换新副本 + 显式提示 + 保留旧文件",
          code == 0 and warned and payload2.get("prompt_path") != payload["prompt_path"] and prompt.is_file(),
          f"warnings={payload2.get('warnings')}")

    # F4b 管理块之后被追加内容：内容必须保留、移动到块前，安装可推进
    home = Path(cli_home("f4b"))
    _code, payload_b, _ = install_json(home)
    prompt_b = Path(payload_b["prompt_path"])
    prompt_b.write_bytes(prompt_b.read_bytes() + "\n手工追加在块后面\n".encode("utf-8"))
    code, out, err = run_cli("--codex-home", str(home), "--install", "--no-open", "--json")
    payload_after = json.loads(out) if out.strip().startswith("{") else {}
    warnings_text = " ".join(payload_after.get("warnings", []))
    merged_text = ""
    if payload_after.get("prompt_path"):
        merged_text = Path(payload_after["prompt_path"]).read_text(encoding="utf-8")
    relocated = ("手工追加在块后面" in merged_text and "<!-- xiaoli-armor:begin -->" in merged_text
                 and merged_text.index("手工追加在块后面") < merged_text.index("<!-- xiaoli-armor:begin -->"))
    check("F4b", "管理块之后被追加内容时：内容保留、移到块前、安装可推进",
          code == 0 and "管理块之后" in warnings_text and relocated and prompt_b.is_file(),
          f"exit={code} / warnings={payload_after.get('warnings')}")

    # F5 doctor 与 audit 都不写盘
    before = tree(home)
    run_cli("--codex-home", str(home), "--doctor", "--json")
    run_cli("--codex-home", str(home), "--doctor")
    run_cli("--codex-home", str(home), "--audit", "--json")
    check("F5", "doctor / audit 全程只读", tree(home) == before, "")

    # F6 危险路径：--codex-home 指到一个普通文件
    bogus = tmp / "f6-file"
    bogus.write_bytes(b"not a directory")
    code, out, err = run_cli("--codex-home", str(bogus), "--install", "--no-open", "--json")
    text_out = out + err
    check("F6", "把 --codex-home 指到普通文件时给出明确错误",
          code == 2 and ("不是目录" in text_out or "目录" in text_out), text_out.strip()[:70])


# --------------------------------------------------------------------------
# G. 崩溃残留现场（部分写入）
# --------------------------------------------------------------------------


def case_g(tmp: Path) -> None:
    """把一个完整安装回退到「写了一半」的状态，验证工具能识别、能用一条命令安全推进。"""
    for stage in ("after_target", "after_state", "orphan_backup"):
        home = Path(new_home(tmp, f"g-{stage}"))
        original_config = (home / "config.toml").read_bytes()
        base_before = (home / "prompts" / "base.md").read_bytes()
        _code, payload, _ = install_json(home)
        managed = home / "managed-prompts" / "xiaoli-armor"
        state = json.loads((managed / "install-state.json").read_text(encoding="utf-8"))
        prompt = Path(state["prompt_path"])

        # 写入顺序是 备份(首次) → 副本 → 状态 → config；把后面的步骤退回去模拟中途被杀
        (home / "config.toml").write_bytes(original_config)
        if stage in ("after_target", "orphan_backup"):
            (managed / "install-state.json").unlink()
        if stage == "after_target":
            (managed / "config.toml.before-xiaoli.bak").unlink(missing_ok=True)

        doctor_code, doctor_out, _ = run_cli("--codex-home", str(home), "--doctor", "--json")
        doctor = json.loads(doctor_out) if doctor_out.strip().startswith("{") else {}
        recovery_code, recovery_out, _ = run_cli("--codex-home", str(home), "--install", "--no-open", "--json")
        recovery = json.loads(recovery_out) if recovery_out.strip().startswith("{") else {}
        verify_code, _, _ = run_cli("--codex-home", str(home), "--verify")
        status = json.loads(run_cli("--codex-home", str(home), "--status", "--json")[1])

        doctor_ok = doctor_code == 0 and bool(doctor.get("situation"))
        install_suggested = any("--install" in command for command in doctor.get("next_commands", []))
        recovered = recovery_code == 0 and verify_code == 0 and status.get("reference_ok") is True
        harmless = (home / "prompts" / "base.md").read_bytes() == base_before and prompt.is_file()
        check(f"G-{stage}", f"崩溃残留（{stage}）：可识别、诊断建议可执行、不丢用户文件",
              doctor_ok and install_suggested and recovered and harmless,
              f"doctor={doctor.get('situation')} / install exit={recovery_code} / verify={verify_code} / "
              f"原指令未变={harmless}")
        if recovery.get("orphan_backup_archived"):
            archived = Path(recovery["orphan_backup_archived"])
            check(f"G-{stage}-archive", f"崩溃残留（{stage}）：孤儿备份已归档而非丢弃",
                  archived.is_dir() and any(archived.iterdir()),
                  str(archived.name))


def case_h(tmp: Path) -> None:
    """H1 回归：活动 profile 段后面还有子表时，注入必须留在本段内。"""
    home = Path(new_home(tmp, "h1"))
    (home / "config.toml").write_bytes(
        b'profile = "work"\n\n[profiles.work]\nmodel = "gpt-5"\n\n'
        b'[profiles.work.mcp]\nenabled = true\n'
    )
    code, out, _ = run_cli("--codex-home", str(home), "--install", "--no-open", "--json", "--target", "profile")
    payload = json.loads(out) if out.strip().startswith("{") else {}
    config_text = (home / "config.toml").read_text(encoding="utf-8")
    status = json.loads(run_cli("--codex-home", str(home), "--status", "--json")[1])
    verify_code, _, _ = run_cli("--codex-home", str(home), "--verify")
    restore_out = run_cli("--codex-home", str(home), "--restore", "--json")[1]
    restored = json.loads(restore_out).get("restored") if restore_out.strip().startswith("{") else False
    in_own_section = (config_text.index("model_instructions_file") < config_text.index("[profiles.work.mcp]"))
    check("H1", "profile 段后有子表时注入仍落在本段内（H1 回归）",
          code == 0 and payload.get("target_key") == "profiles.work.model_instructions_file"
          and status.get("reference_ok") is True and verify_code == 0 and restored is True
          and config_text.count("model_instructions_file") == 1 and in_own_section
          and "enabled = true" in config_text,
          f"install exit={code} / reference_ok={status.get('reference_ok')} / verify={verify_code} / restore={restored}")


def main() -> int:
    global PROFILE_FILE
    tmp = Path(tempfile.mkdtemp(prefix="xiaoli-selfcheck-"))
    PROFILE_FILE = tmp / "profile.selfcheck.json"
    PROFILE_FILE.write_text(json.dumps({
        "assistant_name": "小李",
        "greeting": {"reply": PROFILE_GREETING},
        "passphrase": {"trigger": "小李", "reply": PROFILE_REPLY},
    }, ensure_ascii=False), encoding="utf-8")
    print(f"自检临时目录：{tmp}\n" + "=" * 78)
    try:
        case_a(tmp)
        case_b(tmp)
        case_c(tmp)
        case_d(tmp)
        case_e(tmp)
        case_f(tmp)
        case_g(tmp)
        case_h(tmp)
    finally:
        print("=" * 78)

    failed = [item for item in RESULTS if not item[2]]
    print(f"合计 {len(RESULTS)} 项：通过 {len(RESULTS) - len(failed)}，失败 {len(failed)}")
    for case_id, name, _ok, evidence in failed:
        print(f"  失败：{case_id} {name} :: {evidence}")
    if not failed:
        shutil.rmtree(tmp, ignore_errors=True)
        print("全部通过；临时目录已清理。")
    else:
        print(f"保留现场供排查：{tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())

"""配置引擎的行为契约：安装、幂等、修复、恢复、并发与边界。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xiaoli_engine import (
    KEY,
    ConfigConflictError,
    ConfigDocument,
    ConfigError,
    ConfigStore,
    _bytes,
)

BLOCK = "<!-- xiaoli-armor:begin -->\n正文\n<!-- xiaoli-armor:end -->\n"
CONFIG = (
    'profile = "work"\n'
    'model_instructions_file = "./prompts/base.md"\n'
    'approval_policy = "on-request"\n'
    "\n"
    "[profiles.work]\n"
    'model = "gpt-5"\n'
)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "base.md").write_bytes("# base\n正文\n".encode("utf-8"))
        (self.home / "config.toml").write_bytes(CONFIG.encode("utf-8"))
        self.store = ConfigStore(self.home)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def install(self, *, content: str = BLOCK, rebase_current: bool = False, target_mode: str = "auto",
                break_stale: bool = False, expected_override: dict | None = None) -> dict:
        raw_config = _bytes(self.store.config_path)
        doc = ConfigDocument(raw_config)
        keys, _mode = self.store.detect_target(doc, target_mode)
        expected: dict[Path, bytes | None] = {self.store.config_path: raw_config}
        reference = doc.lookup(keys)
        if isinstance(reference, str) and reference.strip():
            source = self.store.resolve_reference(reference)
            expected[source] = _bytes(source)
        expected[self.store.state_path] = _bytes(self.store.state_path)
        if expected_override:
            expected.update(expected_override)
        return self.store.install_content(
            content, "小李身份与口令.md", title="t", key="k",
            expected_inputs=expected, rebase_current=rebase_current, target_mode=target_mode,
            break_stale=break_stale,
        )

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in sorted(self.home.rglob("*")) if path.is_file()
        }


class InstallTests(Base):
    def test_install_writes_single_assignment_and_copy(self) -> None:
        state = self.install()
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn(state["prompt_path"].replace("\\", "/"), config)
        self.assertIn('profile = "work"', config)
        self.assertIn("[profiles.work]", config)
        self.assertTrue(Path(state["prompt_path"]).is_file())
        self.assertEqual(Path(state["prompt_path"]).read_text(encoding="utf-8"), BLOCK)
        self.assertTrue(self.store.backup_path.is_file())
        self.assertEqual(self.store.backup_path.read_bytes(), CONFIG.encode())

    def test_install_keeps_external_source_untouched(self) -> None:
        before = (self.home / "prompts" / "base.md").read_bytes()
        self.install()
        self.assertEqual((self.home / "prompts" / "base.md").read_bytes(), before)

    def test_second_install_is_byte_identical(self) -> None:
        self.install()
        first = self.snapshot()
        self.install()
        self.assertEqual(self.snapshot(), first)

    def test_install_rejects_changed_input_snapshot(self) -> None:
        with self.assertRaises(ConfigConflictError):
            self.install(expected_override={self.store.config_path: b"stale"})

    def test_empty_content_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.install(content="   \n")

    def test_filename_with_directory_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            self.store.install_content(BLOCK, "sub/x.md", title="t")

    def test_install_creates_config_when_missing(self) -> None:
        self.store.config_path.unlink()
        state = self.install()
        self.assertTrue(self.store.config_path.is_file())
        self.assertEqual((self.home / "config.toml").read_text(encoding="utf-8").strip(),
                         f'model_instructions_file = "{Path(state["prompt_path"]).as_posix()}"')
        self.assertFalse(self.store.backup_path.exists())

    def test_restore_removes_config_created_by_install(self) -> None:
        self.store.config_path.unlink()
        self.install()
        result = self.store.restore()
        self.assertTrue(result["restored"])
        self.assertFalse(self.store.config_path.exists())

    def test_orphan_backup_is_archived_then_install_proceeds(self) -> None:
        self.store.config_path.unlink()
        self.install()
        self.store.restore()
        self.store.managed_dir.mkdir(parents=True, exist_ok=True)
        self.store.backup_path.write_bytes(b"orphan-content")
        state = self.install(content=BLOCK)
        archived = Path(state["orphan_backup_archived"])
        self.assertTrue(archived.is_dir())
        self.assertTrue(archived.name.startswith("orphan-backup-"))
        self.assertEqual((archived / "config-before-first-install.bak").read_bytes(), b"orphan-content")
        self.assertTrue(Path(state["prompt_path"]).is_file())


class RepairTests(Base):
    def test_changed_reference_stops_without_rebase(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        with self.assertRaises(ConfigConflictError):
            self.install()
        self.assertIn("./prompts/other.md", (self.home / "config.toml").read_text(encoding="utf-8"))

    def test_rebase_archives_and_rebuilds(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        state = self.install(rebase_current=True)
        self.assertTrue(state["reference_repaired"])
        recovery = Path(state["recovery_path"])
        self.assertTrue((recovery / "install-state.json").is_file())
        self.assertTrue((recovery / "config-before-repair.toml").is_file())
        self.assertTrue((recovery / "config-before-first-install.bak").is_file())
        self.assertTrue((recovery / "prompts").is_dir())
        self.assertEqual(list(recovery.glob("prompts/*.md")) != [], True)

    def test_restore_after_rebase_returns_repaired_reference(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        self.install(rebase_current=True)
        self.store.restore()
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_instructions_file = "./prompts/other.md"', config)
        self.assertNotIn("managed-prompts", config)


class RestoreTests(Base):
    def test_restore_without_state(self) -> None:
        result = self.store.restore()
        self.assertFalse(result["restored"])

    def test_restore_is_repeatable(self) -> None:
        self.install()
        self.assertTrue(self.store.restore()["restored"])
        self.assertFalse(self.store.restore()["restored"])

    def test_restore_keeps_manual_edit(self) -> None:
        state = self.install()
        prompt = Path(state["prompt_path"])
        prompt.write_text(BLOCK + "\n手工补充\n", encoding="utf-8")
        result = self.store.restore()
        self.assertTrue(prompt.is_file())
        self.assertIn(str(prompt), result["retained_paths"])

    def test_restore_removes_generated_copy(self) -> None:
        state = self.install()
        self.store.restore()
        self.assertFalse(Path(state["prompt_path"]).exists())
        self.assertFalse(self.store.state_path.exists())
        self.assertFalse(self.store.backup_path.exists())

    def test_restore_refuses_when_reference_changed(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        with self.assertRaises(ConfigConflictError):
            self.store.restore()
        self.assertIn("./prompts/other.md", (self.home / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue(self.store.state_path.exists())


class TargetTests(Base):
    def test_auto_prefers_profile_section(self) -> None:
        (self.home / "config.toml").write_text(
            'profile = "work"\n\n[profiles.work]\nmodel = "gpt-5"\n'
            'model_instructions_file = "./prompts/base.md"\n',
            encoding="utf-8",
        )
        state = self.install()
        self.assertEqual(state["keys"], ["profiles", "work", KEY])
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn(f'\n{KEY} = "', config.split("[profiles.work]")[0])
        self.assertIn(Path(state["prompt_path"]).as_posix(), config.split("[profiles.work]")[1])
        self.store.restore()
        config = (self.home / "config.toml").read_text(encoding="utf-8")
        self.assertIn('model_instructions_file = "./prompts/base.md"', config)

    def test_forced_root_target(self) -> None:
        (self.home / "config.toml").write_text(
            'profile = "work"\n\n[profiles.work]\nmodel = "gpt-5"\n', encoding="utf-8"
        )
        state = self.install(target_mode="root")
        self.assertEqual(state["keys"], [KEY])

    def test_profile_mode_without_profile_name(self) -> None:
        (self.home / "config.toml").write_text('approval_policy = "never"\n', encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.install(target_mode="profile")


class LockAndStateTests(Base):
    def test_lock_blocks_second_writer(self) -> None:
        self.store.managed_dir.mkdir(parents=True, exist_ok=True)
        self.store.lock_path.write_text(json.dumps({"pid": 999999999, "created_at": "x"}), encoding="utf-8")
        with self.assertRaises(ConfigConflictError):
            self.install()
        self.assertTrue(self.store.lock_path.exists())

    def test_break_stale_lock_allows_progress(self) -> None:
        self.store.managed_dir.mkdir(parents=True, exist_ok=True)
        self.store.lock_path.write_text(json.dumps({"pid": 999999999, "created_at": "x"}), encoding="utf-8")
        state = self.install(break_stale=True)
        self.assertFalse(self.store.lock_path.exists())
        self.assertTrue(Path(state["prompt_path"]).is_file())

    def test_pid_alive_detects_exited_process(self) -> None:
        import os as _os
        import subprocess
        import sys

        from xiaoli_engine import _pid_alive

        # 两条确定性断言：当前进程必定存活，一个不可能存在的 pid 必定判定为不存在。
        self.assertTrue(_pid_alive(_os.getpid()))
        self.assertFalse(_pid_alive(999999999))
        # 「刚退出的进程」这条受 pid 复用影响（同机大量起进程时会偶发命中），
        # 因此重试几次，只要有一次判定为已退出即算通过。
        detected = False
        for _ in range(5):
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            proc.wait(timeout=60)
            if not _pid_alive(proc.pid):
                detected = True
                break
        self.assertTrue(detected, "刚退出的进程应被判定为不存在")

    def test_live_lock_pid_is_not_cleared(self) -> None:
        self.store.managed_dir.mkdir(parents=True, exist_ok=True)
        self.store.lock_path.write_text(
            json.dumps({"pid": __import__("os").getpid(), "created_at": "x"}), encoding="utf-8")
        with self.assertRaises(ConfigConflictError):
            self.install(break_stale=True)
        self.assertTrue(self.store.lock_path.exists())

    def test_state_with_external_prompt_path_is_rejected(self) -> None:
        self.install()
        state = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        state["prompt_path"] = str(self.home / "prompts" / "base.md")
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.store.read_state()

    def test_state_with_bad_digest_is_rejected(self) -> None:
        self.install()
        state = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        state["owned_prompts"] = {state["prompt_path"]: "zz"}
        self.store.state_path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.store.read_state()

    def test_status_reports_stale_state(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        status = self.store.status()
        self.assertTrue(status["installed"])
        self.assertFalse(status["reference_ok"])

    def test_status_is_read_only(self) -> None:
        before = self.snapshot()
        self.store.status()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.store.managed_dir.exists())

    def test_status_after_install_reports_digest(self) -> None:
        state = self.install()
        status = self.store.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["prompt_path"], state["prompt_path"])
        self.assertTrue(status["reference_ok"])
        self.assertEqual(len(status["prompt_sha256_12"]), 12)
        self.assertEqual(len(status["config_sha256_12"]), 12)

    def test_break_lock_refuses_own_pid(self) -> None:
        import os as _os

        self.store.managed_dir.mkdir(parents=True, exist_ok=True)
        self.store.lock_path.write_text(json.dumps({"pid": _os.getpid(), "created_at": "x"}), encoding="utf-8")
        with self.assertRaises(ConfigConflictError):
            self.install(break_stale=True)
        self.assertTrue(self.store.lock_path.exists())

    def test_break_lock_takes_over_live_process_lock(self) -> None:
        import subprocess
        import sys

        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.store.managed_dir.mkdir(parents=True, exist_ok=True)
            self.store.lock_path.write_text(
                json.dumps({"pid": holder.pid, "created_at": "x"}), encoding="utf-8")
            # 未显式接管时仍然拒绝
            with self.assertRaises(ConfigConflictError):
                self.install()
            # 显式接管：清理锁并把原锁文件归档留痕
            state = self.install(break_stale=True)
            self.assertTrue(Path(state["prompt_path"]).is_file())
            self.assertFalse(self.store.lock_path.exists())
            archives = list((self.store.managed_dir / "history").glob("lock-*/install.lock"))
            self.assertEqual(len(archives), 1)
            self.assertIn(str(holder.pid), archives[0].read_text(encoding="utf-8"))
        finally:
            holder.terminate()
            holder.wait(timeout=30)

    def test_concurrent_first_install_survives_mkdir_race(self) -> None:
        """四个进程同时首次安装：只能有锁冲突，不能出现目录创建竞态错误。"""
        import subprocess
        import sys

        cli = Path(__file__).resolve().parent.parent / "xiaoli.py"
        procs = [subprocess.Popen(
            [sys.executable, str(cli), "--codex-home", str(self.home), "--install", "--no-open", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            for _ in range(4)]
        outs = [proc.communicate(timeout=180) for proc in procs]
        codes = sorted(proc.returncode for proc in procs)
        self.assertIn(0, codes, f"至少要有一个进程安装成功：{codes}")
        self.assertTrue(all(code in (0, 2) for code in codes), f"出现预期外的退出码：{codes}")
        self.assertTrue(ConfigStore(self.home).status()["reference_ok"])
        for out, err in outs:
            combined = out + err
            self.assertNotIn("FileExistsError", combined)
            self.assertNotIn("WinError", combined)

    def test_install_into_managed_dir_is_reserved_in_cli(self) -> None:
        # CLI 层会拒绝把引用指向自己的状态/备份/锁文件；引擎层只做路径归属校验。
        with self.assertRaises(ConfigError):
            self.store._managed_prompt(str(self.store.config_path))


class HardeningTests(Base):
    """针对真实 Windows 环境的失败模式：文件被占用、路径类型错误、现场诊断。"""

    def test_write_retries_transient_lock(self) -> None:
        import os as _os
        from unittest import mock

        import xiaoli_engine

        real_replace = _os.replace
        calls = {"count": 0}

        def flaky(source, destination):
            calls["count"] += 1
            if calls["count"] < 3:
                raise PermissionError(13, "file in use by another process")
            return real_replace(source, destination)

        slept: list[float] = []
        target = self.home / "retry.txt"
        with mock.patch("xiaoli_engine.os.replace", side_effect=flaky), \
                mock.patch("xiaoli_engine.time.sleep", side_effect=lambda value: slept.append(value)):
            xiaoli_engine._atomic_write(target, b"hello")
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(slept, [0.15, 0.5])

    def test_write_failure_message_is_actionable(self) -> None:
        from unittest import mock

        import xiaoli_engine

        with mock.patch("xiaoli_engine.os.replace", side_effect=PermissionError(13, "denied")), \
                mock.patch("xiaoli_engine.time.sleep", return_value=None):
            with self.assertRaises(ConfigError) as ctx:
                xiaoli_engine._atomic_write(self.home / "x.txt", b"y")
        message = str(ctx.exception)
        self.assertIn("重试", message)
        self.assertIn("杀毒", message)
        self.assertIn("同步盘", message)

    def test_failed_write_keeps_original_file(self) -> None:
        from unittest import mock

        target = self.home / "locked.txt"
        target.write_bytes(b"x")
        with mock.patch("xiaoli_engine.os.replace", side_effect=PermissionError(13, "denied")), \
                mock.patch("xiaoli_engine.time.sleep", return_value=None):
            with self.assertRaises(ConfigError):
                from xiaoli_engine import _atomic_write

                _atomic_write(target, b"y")
        self.assertTrue(target.is_file())  # 写失败不能破坏原文件
        self.assertEqual(target.read_bytes(), b"x")

    def test_home_path_must_be_directory(self) -> None:
        fake = self.home / "not-a-directory"
        fake.write_bytes(b"x")
        with self.assertRaises(ConfigError):
            ConfigStore(fake).status()

    def test_config_path_must_be_file(self) -> None:
        self.store.config_path.unlink()
        self.store.config_path.mkdir()
        with self.assertRaises(ConfigError):
            self.store.status()

    def test_install_reports_replaced_edited_copy(self) -> None:
        state = self.install()
        prompt = Path(state["prompt_path"])
        prompt.write_text(prompt.read_text(encoding="utf-8").replace("正文", "正文（手改）"), encoding="utf-8")
        again = self.install()
        self.assertEqual(again["replaced_edited_prompt"], str(prompt))
        self.assertNotEqual(again["prompt_path"], state["prompt_path"])
        self.assertTrue(prompt.is_file())

    def test_diagnose_not_installed(self) -> None:
        self.store.config_path.unlink()
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "未安装")
        self.assertEqual(report["severity"], "ok")
        self.assertIn("--install", " ".join(report["next_commands"]))

    def test_diagnose_unregistered_reference(self) -> None:
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "当前引用未登记（可能是其他工具或手工配置）")
        self.assertEqual(report["severity"], "low")
        self.assertIn("--dry-run", " ".join(report["next_commands"]))

    def test_diagnose_installed_ok(self) -> None:
        self.install()
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "已安装且引用一致")
        self.assertIn("--verify", " ".join(report["next_commands"]))

    def test_diagnose_external_change(self) -> None:
        self.install()
        (self.home / "config.toml").write_text(
            CONFIG.replace("./prompts/base.md", "./prompts/other.md"), encoding="utf-8"
        )
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "配置引用被外部改动")
        self.assertIn("--install", " ".join(report["next_commands"]))

    def test_diagnose_missing_copy(self) -> None:
        state = self.install()
        Path(state["prompt_path"]).unlink()
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "指令副本丢失")
        self.assertEqual(report["severity"], "high")

    def test_diagnose_orphan_backup(self) -> None:
        state = self.install(content=BLOCK)
        Path(state["prompt_path"]).write_bytes("# 手动改过的内容\n".encode("utf-8"))
        self.store.state_path.unlink()
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "上次安装未完成（有孤儿首次备份、没有安装状态）")
        self.assertIn("--install", " ".join(report["next_commands"]))

    def test_diagnose_stale_lock(self) -> None:
        self.install()
        self.store.lock_path.write_text(json.dumps({"pid": 999999999, "created_at": "x"}), encoding="utf-8")
        report = self.store.diagnose()
        self.assertEqual(report["severity"], "medium")
        self.assertIn("--break-lock", " ".join(report["next_commands"]))

    def test_diagnose_flags_edited_copy(self) -> None:
        state = self.install()
        prompt = Path(state["prompt_path"])
        prompt.write_text(prompt.read_text(encoding="utf-8").replace("正文", "手改"), encoding="utf-8")
        report = self.store.diagnose()
        self.assertEqual(report["severity"], "medium")
        self.assertTrue(any("手工修改" in item for item in report["findings"]))

    def test_diagnose_does_not_write(self) -> None:
        self.install()
        before = self.snapshot()
        self.store.diagnose()
        self.assertEqual(self.snapshot(), before)

    def test_diagnose_broken_config(self) -> None:
        self.store.config_path.write_bytes(b"= = =\n")
        report = self.store.diagnose()
        self.assertEqual(report["situation"], "config.toml 无法解析")
        self.assertEqual(report["severity"], "high")


if __name__ == "__main__":
    unittest.main()

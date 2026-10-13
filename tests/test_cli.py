"""命令行端到端测试（全部在隔离的临时配置目录里执行）。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import xiaoli

CONFIG = (
    'profile = "work"\n'
    'model_instructions_file = "./prompts/base.md"\n'
    "\n"
    "[profiles.work]\n"
    'model = "gpt-5"\n'
)


class CliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "base.md").write_text("# base\n正文\n", encoding="utf-8")
        (self.home / "config.toml").write_text(CONFIG, encoding="utf-8")
        # 隔离的 profile：测试绝不能依赖仓库里的 profile.json（用户随时会改它）
        self.profile_path = self.home / "profile.isolated.json"
        self.profile_path.write_text(json.dumps({
            "assistant_name": "小李",
            "passphrase": {"trigger": "小李", "reply": "隔离第一行\n隔离第二行"},
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        cli_args = ["--codex-home", str(self.home)]
        if "--profile-file" not in args:
            cli_args += ["--profile-file", str(self.profile_path)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = xiaoli.main([*cli_args, *args])
        return code, out.getvalue(), err.getvalue()

    def config_text(self) -> str:
        return (self.home / "config.toml").read_text(encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in sorted(self.home.rglob("*")) if path.is_file()
        }


class FlowTests(CliBase):
    def test_full_lifecycle(self) -> None:
        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["config_installed"])
        self.assertFalse(result["model_reply_verified"])
        self.assertIn("managed-prompts", result["prompt_path"])
        self.assertEqual(result["passphrase_trigger"], "小李")

        code, out, _ = self.run_cli("--status", "--json")
        status = json.loads(out)
        self.assertTrue(status["installed"])
        self.assertTrue(status["reference_ok"])

        code, out, _ = self.run_cli("--verify")
        self.assertEqual(code, 0, out)
        self.assertIn("结论：本地配置链完整。", out)

        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        self.assertTrue(json.loads(out)["unchanged"])

        code, out, _ = self.run_cli("--restore", "--json")
        self.assertTrue(json.loads(out)["restored"])
        self.assertIn('model_instructions_file = "./prompts/base.md"', self.config_text())

    def test_doctor_reports_each_stage(self) -> None:
        (self.home / "config.toml").unlink()
        code, out, _ = self.run_cli("--doctor", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["situation"], "未安装")

        self.run_cli("--install", "--no-open", "--json")
        code, out, _ = self.run_cli("--doctor", "--json")
        report = json.loads(out)
        self.assertEqual(report["situation"], "已安装且引用一致")
        self.assertEqual(report["severity"], "ok")

        code, out, err = self.run_cli("--doctor")
        self.assertEqual(code, 0)
        self.assertIn("现场诊断", out)
        self.assertIn("配置目录：", out)
        self.assertIn("建议命令", out)
        self.assertEqual(err, "")

    def test_doctor_reports_unregistered_reference(self) -> None:
        code, out, _ = self.run_cli("--doctor", "--json")
        report = json.loads(out)
        self.assertEqual(report["situation"], "当前引用未登记（可能是其他工具或手工配置）")
        self.assertEqual(report["severity"], "low")

    def test_doctor_flags_external_change(self) -> None:
        self.run_cli("--install", "--no-open", "--json")
        (self.home / "config.toml").write_text(
            'model_instructions_file = "./prompts/other.md"\n', encoding="utf-8")
        code, out, _ = self.run_cli("--doctor", "--json")
        report = json.loads(out)
        self.assertEqual(report["situation"], "配置引用被外部改动")
        self.assertIn("--install", " ".join(report["next_commands"]))

    def test_doctor_is_read_only(self) -> None:
        self.run_cli("--install", "--no-open", "--json")
        before = self.snapshot()
        self.run_cli("--doctor", "--json")
        self.run_cli("--doctor")
        self.assertEqual(self.snapshot(), before)

    def test_install_warns_when_copy_was_edited(self) -> None:
        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        prompt = Path(json.loads(out)["prompt_path"])
        # 改管理块之前的正文：这类修改允许被继承，工具会另建副本而不是覆盖用户内容。
        text = prompt.read_text(encoding="utf-8")
        prompt.write_text(text.replace("正文", "正文（手工改过）", 1), encoding="utf-8")

        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertTrue(payload["warnings"])
        self.assertIn("手工修改", " ".join(payload["warnings"]))
        self.assertNotEqual(payload["prompt_path"], str(prompt))
        self.assertTrue(prompt.is_file())

        # 再次手改新副本：人类可读输出也必须给出提示
        prompt_b = Path(payload["prompt_path"])
        prompt_b.write_text(prompt_b.read_text(encoding="utf-8").replace("正文", "正文（再改）", 1), encoding="utf-8")
        code, out, _ = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 0)
        self.assertIn("提示：", out)
        self.assertIn("配置目录：", out)

    def test_install_relocates_manual_content_after_block(self) -> None:
        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        prompt = Path(json.loads(out)["prompt_path"])
        prompt.write_bytes(prompt.read_bytes() + "\n手工追加在块后面\n".encode("utf-8"))

        code, out, _ = self.run_cli("--install", "--no-open", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 0, out)
        self.assertIn("手工追加在块后面", prompt.read_text(encoding="utf-8"))
        self.assertTrue(payload["warnings"])
        self.assertIn("管理块之后", " ".join(payload["warnings"]))
        self.assertEqual(prompt.read_text(encoding="utf-8").count("<!-- xiaoli-armor:end -->"), 1)

    def test_verify_failure_prints_checks_to_stdout(self) -> None:
        self.run_cli("--install", "--no-open", "--json")
        managed = self.home / "managed-prompts" / "xiaoli-armor"
        prompt = next(managed.glob("active*.md"))
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\n手工改动\n", encoding="utf-8")
        code, out, err = self.run_cli("--verify")
        self.assertEqual(code, 1)
        self.assertIn("[未通过]", out)
        self.assertIn("结论：", out)
        self.assertNotIn("执行未完成", err)
        self.assertNotIn("None", out)

    def test_read_only_actions_write_nothing(self) -> None:
        self.run_cli("--install", "--no-open", "--json")
        before = self.snapshot()
        self.run_cli("--status", "--json")
        self.run_cli("--verify")
        self.run_cli("--audit", "--json")
        self.run_cli("--emit", "--json")
        self.run_cli("--install", "--dry-run", "--json")
        self.assertEqual(self.snapshot(), before)

    def test_missing_profile_file_is_rejected(self) -> None:
        code, out, err = self.run_cli("--install", "--no-open", "--json",
                                      "--profile-file", str(self.home / "没有这个.json"))
        self.assertEqual(code, 2)
        self.assertIn("profile 文件不存在", out + err)
        self.assertNotIn("managed-prompts", self.config_text())

    def test_emit_out_refuses_existing_file(self) -> None:
        target = self.home / "out.md"
        target.write_text("占位内容", encoding="utf-8")
        code, out, _ = self.run_cli("--emit", "--out", str(target), "--json")
        self.assertEqual(code, 2)
        self.assertIn("已存在", out)
        self.assertEqual(target.read_text(encoding="utf-8"), "占位内容")
        code, out, _ = self.run_cli("--emit", "--out", str(target), "--force", "--json")
        self.assertEqual(code, 0)
        self.assertIn("<!-- xiaoli-armor:begin -->", target.read_text(encoding="utf-8"))

    def test_reference_pointing_to_directory_is_rejected(self) -> None:
        (self.home / "prompts" / "adir").mkdir()
        (self.home / "config.toml").write_text(
            'model_instructions_file = "./prompts/adir"\n', encoding="utf-8")
        code, out, err = self.run_cli("--install", "--no-open", "--json")
        self.assertEqual(code, 2)
        self.assertIn("目录", out + err)

    def test_foreign_managed_reference_requires_takeover(self) -> None:
        other = self.home / "managed-prompts" / "xiaoguai-oneclick"
        other.mkdir(parents=True)
        foreign = other / "active.md"
        foreign.write_text("# 另一个工具生成的副本\n", encoding="utf-8")
        (self.home / "config.toml").write_text(
            f'model_instructions_file = "{foreign.as_posix()}"\n', encoding="utf-8")

        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("--takeover", err)
        self.assertNotIn("xiaoli-armor", self.config_text())

        code, out, _ = self.run_cli("--install", "--no-open", "--takeover", "--json")
        self.assertEqual(code, 0, out)
        self.assertIn("xiaoli-armor", self.config_text())
        self.assertTrue(foreign.is_file())
        self.assertFalse((other / "install-state.json").exists())

    def test_dry_run_writes_nothing(self) -> None:
        before = self.snapshot()
        code, out, _ = self.run_cli("--install", "--dry-run", "--json")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["would_write"])
        self.assertEqual(self.snapshot(), before)

    def test_emit_does_not_install(self) -> None:
        before = self.snapshot()
        code, out, _ = self.run_cli("--emit")
        self.assertEqual(code, 0)
        self.assertIn("<!-- xiaoli-armor:begin -->", out)
        self.assertIn("隔离第一行", out)          # 用的是隔离 profile，不依赖仓库配置
        self.assertEqual(self.snapshot(), before)

        code, out, _ = self.run_cli("--emit", "--json")
        payload = json.loads(out)
        self.assertTrue(payload["content"].endswith("<!-- xiaoli-armor:end -->\n"))
        self.assertEqual(self.snapshot(), before)

    def test_emit_to_file(self) -> None:
        target = self.home / "out" / "prompt.md"
        code, out, _ = self.run_cli("--emit", "--out", str(target), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        self.assertIn("小李在。👋", target.read_text(encoding="utf-8"))

    def test_init_profile(self) -> None:
        target = self.home / "profile.json"
        code, out, _ = self.run_cli("--init-profile", "--profile-file", str(target), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(target.is_file())
        code, out, _ = self.run_cli("--init-profile", "--profile-file", str(target), "--json")
        self.assertEqual(code, 2)
        self.assertIn("已存在", out)
        code, _, _ = self.run_cli("--init-profile", "--profile-file", str(target), "--force", "--json")
        self.assertEqual(code, 0)

    def test_custom_profile_changes_passphrase(self) -> None:
        profile = self.home / "profile.json"
        profile.write_text(json.dumps({
            "assistant_name": "阿李",
            "passphrase": {"trigger": "阿李", "reply": "第一行\n第二行"},
        }), encoding="utf-8")
        code, out, _ = self.run_cli("--install", "--no-open", "--json", "--profile-file", str(profile))
        self.assertEqual(code, 0)
        prompt = Path(json.loads(out)["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("第一行\n第二行", prompt)
        self.assertIn("阿李在。👋", prompt)


class ErrorPathTests(CliBase):
    def test_missing_reference_file(self) -> None:
        (self.home / "config.toml").write_text(
            'model_instructions_file = "./prompts/missing.md"\n', encoding="utf-8")
        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)
        self.assertNotIn("managed-prompts", self.config_text())

    def test_reference_to_reserved_backup(self) -> None:
        self.run_cli("--install", "--no-open", "--json")
        backup = self.home / "managed-prompts" / "xiaoli-armor" / "config.toml.before-xiaoli.bak"
        text = self.config_text()
        start = text.index('model_instructions_file')
        end = text.index("\n", start)
        (self.home / "config.toml").write_text(
            text[:start] + f'model_instructions_file = "{backup.as_posix()}"' + text[end:], encoding="utf-8")
        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("未修改任何配置", err)

    def test_non_utf8_reference(self) -> None:
        (self.home / "prompts" / "base.md").write_bytes(b"\xff\xfe\x00bad")
        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("UTF-8", err)

    def test_blank_reference(self) -> None:
        (self.home / "prompts" / "base.md").write_text("   \n", encoding="utf-8")
        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("为空", err)

    def test_invalid_config(self) -> None:
        (self.home / "config.toml").write_text("= = =\n", encoding="utf-8")
        code, _, err = self.run_cli("--install", "--no-open")
        self.assertEqual(code, 2)
        self.assertIn("TOML", err)

    def test_conflicting_action_flags(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli("--install", "--restore")


if __name__ == "__main__":
    unittest.main()

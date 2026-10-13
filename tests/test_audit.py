"""只读环境审计的回归测试：脱敏、只读、不联网。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoli_audit import audit, format_audit

SECRET = "SECRET_PROMPT_MARKER_不应出现在审计输出里"


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "prompts").mkdir(parents=True)
        (self.home / "prompts" / "base.md").write_text(
            f"# base\n{SECRET}\napi_key = 示例\n", encoding="utf-8")
        (self.home / "config.toml").write_text(
            'model_instructions_file = "./prompts/base.md"\n', encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in sorted(self.home.rglob("*")) if path.is_file()
        }

    def test_audit_is_read_only(self) -> None:
        before = self.snapshot()
        report = audit(self.home)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(report["read_only"])
        self.assertFalse(report["network_used"])

    def test_audit_never_echoes_prompt_text(self) -> None:
        report = audit(self.home)
        blob = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertEqual(report["prompt_file"]["sha256_12"], audit(self.home)["prompt_file"]["sha256_12"])

    def test_audit_reports_external_reference(self) -> None:
        report = audit(self.home)
        self.assertFalse(report["reference_inside_managed_dir"])
        self.assertTrue(any(item.startswith("external_reference") for item in report["findings"]))
        self.assertIn("risk_keywords", " ".join(report["findings"]))

    def test_audit_hides_proxy_address(self) -> None:
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            report = audit(self.home)
            blob = json.dumps(report, ensure_ascii=False)
        self.assertTrue(report["proxy_env_set"]["HTTPS_PROXY"])
        self.assertNotIn("7890", blob)
        self.assertNotIn("127.0.0.1", blob)

    def test_audit_after_install_marks_state_current(self) -> None:
        import contextlib
        import io

        import xiaoli

        with contextlib.redirect_stdout(io.StringIO()):
            xiaoli.main(["--codex-home", str(self.home), "--install", "--no-open", "--json"])
        report = audit(self.home)
        self.assertTrue(report["install_state"]["exists"])
        self.assertTrue(report["install_state"]["state_is_current"])
        self.assertTrue(report["install_state"]["prompt_digest_matches"])
        self.assertEqual(report["install_state"]["profile"], "小李 · 一键破甲")

    def test_format_audit_has_conclusion(self) -> None:
        text = format_audit(audit(self.home))
        self.assertIn("小李破甲 · 只读环境审计", text)
        self.assertIn("结论：", text)

    def test_audit_without_config(self) -> None:
        (self.home / "config.toml").unlink()
        report = audit(self.home)
        self.assertFalse(report["config_exists"])
        self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()

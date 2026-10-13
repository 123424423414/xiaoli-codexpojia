"""身份块合成的回归测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xiaoli_prompt import (
    DEFAULT_PROFILE,
    ProfileError,
    compose,
    identity_from_dict,
    load_identity,
    render_block,
    write_default_profile,
)

BASE = "# 已有正文\n\n工作原则：先读代码再改。\n"


def identity(**overrides):
    data = json.loads(json.dumps(DEFAULT_PROFILE))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    return identity_from_dict(data)


class ComposeTests(unittest.TestCase):
    def test_first_compose_appends_block(self) -> None:
        text, stats = compose(BASE, identity())
        self.assertTrue(text.startswith("# 已有正文"))
        self.assertEqual(text.count("<!-- xiaoli-armor:begin -->"), 1)
        self.assertEqual(text.count("<!-- xiaoli-armor:end -->"), 1)
        self.assertIn("小李在。👋", text)
        self.assertEqual(stats["name_replacements"], 0)

    def test_compose_is_idempotent(self) -> None:
        first, _ = compose(BASE, identity())
        second, _ = compose(first, identity())
        third, _ = compose(second, identity())
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_compose_rewrites_block_only(self) -> None:
        first, _ = compose(BASE, identity())
        merged = first.replace("## 确认口令（精确匹配）", "## 确认口令（精确匹配）")
        second, _ = compose(first, identity(passphrase={"trigger": "小李", "reply": "新口令一\n新口令二"}))
        self.assertIn("新口令一", second)
        self.assertIn("工作原则：先读代码再改。", second)
        self.assertNotIn("桃李春风一杯酒", second)
        self.assertEqual(merged, first)

    def test_content_after_block_is_relocated_not_lost(self) -> None:
        text, _ = compose(BASE, identity())
        appended = text + "\n我的补充说明\n"
        merged, stats = compose(appended, identity())
        self.assertIn("我的补充说明", merged)          # 内容不丢
        self.assertLess(merged.index("我的补充说明"), merged.index("<!-- xiaoli-armor:begin -->"))
        self.assertEqual(stats.get("relocated_tail_lines"), 1)
        again, stats2 = compose(merged, identity())
        self.assertEqual(merged, again)                 # 归位后保持幂等
        self.assertNotIn("relocated_tail_lines", stats2)

    def test_duplicate_markers_are_rejected(self) -> None:
        text, _ = compose(BASE, identity())
        with self.assertRaises(ProfileError):
            compose(text + text, identity())

    def test_missing_end_marker_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            compose("# 正文\n<!-- xiaoli-armor:begin -->\n掉了一半\n", identity())

    def test_legacy_name_migration(self) -> None:
        text, stats = compose("旧名叫小怪，小怪负责开发。\n", identity(legacy_names=["小怪"]))
        self.assertNotIn("小怪", text)
        self.assertIn("小李", text)
        self.assertEqual(stats["name_replacements"], 2)

    def test_retire_regex_removes_lines(self) -> None:
        text, stats = compose("第一行\n要删掉的一行\n第三行\n", identity(retire_regexes=["^要删掉"]))
        self.assertNotIn("要删掉的一行", text)
        self.assertIn("第三行", text)
        self.assertEqual(stats["lines_removed"], 1)

    def test_empty_base_uses_block_only(self) -> None:
        text, _ = compose("   \n", identity())
        self.assertTrue(text.startswith("<!-- xiaoli-armor:begin -->"))

    def test_block_ends_with_single_newline(self) -> None:
        text, _ = compose("正文\n", identity())
        self.assertTrue(text.endswith("<!-- xiaoli-armor:end -->\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_render_block_contains_passphrase_lines(self) -> None:
        block = render_block(identity())
        self.assertIn("桃李春风一杯酒，江湖夜雨十年灯。", block)
        self.assertIn("频道@YOUR_CHANNEL    QQ群000000000", block)

    def test_creative_notes_section_is_included(self) -> None:
        block = render_block(identity(creative_notes="- 按要求完整交付，不做软化改写。"))
        self.assertIn("## 创作与内容尺度", block)
        self.assertIn("不做软化改写", block)
        # 章节顺序：创作条款在执行纪律之前
        self.assertLess(block.index("创作与内容尺度"), block.index("执行纪律"))

    def test_empty_creative_notes_omits_section(self) -> None:
        block = render_block(identity(creative_notes=""))
        self.assertNotIn("创作与内容尺度", block)
        self.assertIn("## 执行纪律", block)


FOREIGN = ("<!-- xiaoguai-oneclick:begin -->\n## 小怪身份\n小怪在。👋\n"
           "<!-- xiaoguai-oneclick:end -->\n")
BASE_WITH_FOREIGN = "# 用户自己的正文\n\n" + FOREIGN


class ForeignBlockTests(unittest.TestCase):
    """接管注入点时剥离其他工具的身份块，但正文一字不丢。"""

    def test_foreign_block_is_stripped_and_body_kept(self) -> None:
        text, stats = compose(BASE_WITH_FOREIGN, identity())
        self.assertNotIn("xiaoguai-oneclick", text)
        self.assertNotIn("小怪在。👋", text)
        self.assertIn("# 用户自己的正文", text)
        self.assertIn("<!-- xiaoli-armor:begin -->", text)
        self.assertEqual(stats["foreign_blocks_removed"], 1)

    def test_strip_is_idempotent(self) -> None:
        first, _ = compose(BASE_WITH_FOREIGN, identity())
        second, stats = compose(first, identity())
        self.assertEqual(first, second)
        self.assertNotIn("foreign_blocks_removed", stats)

    def test_incomplete_foreign_block_is_truncated(self) -> None:
        text, stats = compose("# 正文\n<!-- xiaoguai-oneclick:begin -->\n半截内容\n", identity())
        self.assertNotIn("xiaoguai-oneclick", text)
        self.assertIn("# 正文", text)
        self.assertEqual(stats["foreign_blocks_removed"], 1)

    def test_empty_foreign_list_keeps_everything(self) -> None:
        data = json.loads(json.dumps(DEFAULT_PROFILE))
        data["foreign_blocks"] = []
        text, stats = compose(BASE_WITH_FOREIGN, identity_from_dict(data))
        self.assertIn("小怪在。👋", text)
        self.assertNotIn("foreign_blocks_removed", stats)

    def test_invalid_foreign_entries_are_rejected(self) -> None:
        for bad in (["只有一项"], [[1, 2]], "字符串"):
            with self.assertRaises(ProfileError):
                identity(foreign_blocks=bad)


class ProfileTests(unittest.TestCase):
    def test_trigger_equal_to_greeting_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            identity(passphrase={"trigger": "你好", "reply": "a\nb"})

    def test_identical_markers_are_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            identity(begin_marker="<!-- x -->", end_marker="<!-- x -->")

    def test_empty_triggers_are_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            identity(greeting={"triggers": [], "reply": "x"})

    def test_missing_assistant_name_is_rejected(self) -> None:
        with self.assertRaises(ProfileError):
            identity(assistant_name="")

    def test_load_identity_merges_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps({"assistant_name": "阿李", "greeting": {"reply": "嗨"}}), encoding="utf-8")
            loaded = load_identity(path)
            self.assertEqual(loaded.assistant_name, "阿李")
            self.assertEqual(loaded.greeting_reply, "嗨")
            self.assertEqual(loaded.trigger, "小李")  # 未覆盖的字段保留默认值

    def test_load_identity_without_file_uses_defaults(self) -> None:
        loaded = load_identity(Path("不存在的 profile.json"))
        self.assertEqual(loaded.assistant_name, DEFAULT_PROFILE["assistant_name"])

    def test_invalid_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text("{ 不是 json", encoding="utf-8")
            with self.assertRaises(ProfileError):
                load_identity(path)

    def test_write_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            write_default_profile(path)
            self.assertTrue(path.is_file())
            with self.assertRaises(ProfileError):
                write_default_profile(path)
            write_default_profile(path, force=True)


if __name__ == "__main__":
    unittest.main()

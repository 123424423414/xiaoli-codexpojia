"""TOML 行级定位与定点改写的回归测试。"""

from __future__ import annotations

import tomllib
import unittest

from xiaoli_engine import KEY, BOM, ConfigDocument, ConfigError, scan_toml


class LocateTests(unittest.TestCase):
    def test_top_level_key(self) -> None:
        text = 'a = 1\nmodel_instructions_file = "x"\nb = 2\n'
        doc = ConfigDocument(text.encode())
        item = doc.find((KEY,))
        self.assertIsNotNone(item)
        self.assertEqual(item.raw, 'model_instructions_file = "x"\n')
        self.assertEqual(item.indent, "")

    def test_comment_is_not_an_assignment(self) -> None:
        text = '# model_instructions_file = "no"\nmodel_instructions_file = "yes"\n'
        doc = ConfigDocument(text.encode())
        self.assertEqual(doc.lookup((KEY,)), "yes")
        self.assertEqual(doc.find((KEY,)).raw, 'model_instructions_file = "yes"\n')

    def test_key_inside_multiline_string_is_ignored(self) -> None:
        text = 'note = """\nmodel_instructions_file = "no"\n"""\nmodel_instructions_file = "yes"\n'
        doc = ConfigDocument(text.encode())
        self.assertEqual(doc.lookup((KEY,)), "yes")
        self.assertEqual(doc.find((KEY,)).raw, 'model_instructions_file = "yes"\n')

    def test_key_inside_array_and_inline_table_is_ignored(self) -> None:
        text = 'points = [\n  { x = 1, y = 2 },\n]\nmodel_instructions_file = "yes"\n'
        doc = ConfigDocument(text.encode())
        self.assertEqual(doc.lookup((KEY,)), "yes")

    def test_section_key(self) -> None:
        text = '[profiles.work]\nmodel = "gpt-5"\nmodel_instructions_file = "./base.md"\n'
        doc = ConfigDocument(text.encode())
        self.assertIsNone(doc.find((KEY,)))
        self.assertEqual(doc.find(("profiles", "work", KEY)).raw, 'model_instructions_file = "./base.md"\n')

    def test_quoted_section_name(self) -> None:
        text = '[profiles."my work"]\nmodel_instructions_file = "./base.md"\n'
        doc = ConfigDocument(text.encode())
        self.assertIsNotNone(doc.find(("profiles", "my work", KEY)))

    def test_dotted_key(self) -> None:
        text = 'profiles.work.model_instructions_file = "./base.md"\n'
        doc = ConfigDocument(text.encode())
        item = doc.find(("profiles", "work", KEY))
        self.assertIsNotNone(item)
        self.assertEqual(item.key_text, "profiles.work.model_instructions_file")

    def test_array_of_tables_header(self) -> None:
        text = '[[items]]\nname = "a"\nmodel_instructions_file = "nope"\n'
        doc = ConfigDocument(text.encode())
        self.assertEqual(doc.find(("items", KEY)).raw, 'model_instructions_file = "nope"\n')
        self.assertIsNone(doc.find((KEY,)))

    def test_indented_key_keeps_indent(self) -> None:
        text = '[profiles.work]\n    model_instructions_file = "./base.md"\n'
        doc = ConfigDocument(text.encode())
        item = doc.find(("profiles", "work", KEY))
        self.assertEqual(item.indent, "    ")
        self.assertEqual(item.key_text, KEY)
        out = doc.set_assignment(("profiles", "work", KEY), '"new.md"').decode()
        # 精确相等：缩进只能出现一次（早期版本会把缩进同时写进键文本，变成双缩进）
        self.assertEqual(out, '[profiles.work]\n    model_instructions_file = "new.md"\n')

    def test_tab_indented_key_keeps_single_tab(self) -> None:
        text = '[profiles.work]\n\tmodel_instructions_file = "./base.md"\n'
        doc = ConfigDocument(text.encode())
        item = doc.find(("profiles", "work", KEY))
        self.assertEqual(item.indent, "\t")
        self.assertEqual(item.key_text, KEY)
        out = doc.set_assignment(("profiles", "work", KEY), '"new.md"').decode()
        self.assertEqual(out, '[profiles.work]\n\tmodel_instructions_file = "new.md"\n')
        self.assertNotIn("\t\t", out)

    def test_scan_reports_headers(self) -> None:
        scan = scan_toml('[a]\nx = 1\n[b.c]\ny = 2\n')
        self.assertEqual([header.keys for header in scan.headers], [("a",), ("b", "c")])
        self.assertEqual([item.keys for item in scan.assignments], [("a", "x"), ("b", "c", "y")])


class RewriteTests(unittest.TestCase):
    def test_replace_keeps_other_bytes(self) -> None:
        text = 'profile = "work"\nmodel_instructions_file = "./base.md"\napproval_policy = "on-request"\n'
        doc = ConfigDocument(text.encode())
        out = doc.set_assignment((KEY,), '"/abs/copy.md"').decode()
        self.assertEqual(
            out,
            'profile = "work"\nmodel_instructions_file = "/abs/copy.md"\napproval_policy = "on-request"\n',
        )
        self.assertEqual(tomllib.loads(out)[KEY], "/abs/copy.md")

    def test_replace_keeps_crlf(self) -> None:
        text = 'a = 1\r\nmodel_instructions_file = "x"\r\n'
        out = ConfigDocument(text.encode()).set_assignment((KEY,), '"y"')
        self.assertEqual(out, b'a = 1\r\nmodel_instructions_file = "y"\r\n')

    def test_replace_keeps_bom(self) -> None:
        raw = BOM + b'model_instructions_file = "x"\n'
        out = ConfigDocument(raw).set_assignment((KEY,), '"y"')
        self.assertTrue(out.startswith(BOM))
        self.assertEqual(out[len(BOM):], b'model_instructions_file = "y"\n')

    def test_replace_without_trailing_newline(self) -> None:
        raw = b'a = 1\nmodel_instructions_file = "x"'
        out = ConfigDocument(raw).set_assignment((KEY,), '"y"')
        toml_text = out.decode()
        self.assertIn('model_instructions_file = "y"\n', toml_text)
        self.assertEqual(tomllib.loads(toml_text)[KEY], "y")

    def test_insert_at_root_goes_before_first_table(self) -> None:
        text = 'a = 1\n\n[profiles.work]\nmodel = "gpt-5"\n'
        out = ConfigDocument(text.encode()).set_assignment((KEY,), '"y"').decode()
        self.assertLess(out.index(KEY), out.index("[profiles.work]"))
        self.assertEqual(tomllib.loads(out)[KEY], "y")

    def test_insert_into_existing_section(self) -> None:
        text = 'profile = "work"\n\n[profiles.work]\nmodel = "gpt-5"\n\n[other]\nk = 1\n'
        out = ConfigDocument(text.encode()).set_assignment(("profiles", "work", KEY), '"y"').decode()
        self.assertEqual(tomllib.loads(out)["profiles"]["work"][KEY], "y")
        self.assertNotIn(KEY + " = ", out.split("[other]")[1])

    def test_insert_into_missing_section_fails(self) -> None:
        doc = ConfigDocument(b'a = 1\n')
        with self.assertRaises(ConfigError):
            doc.set_assignment(("profiles", "gone", KEY), '"y"')

    def test_inline_comment_is_preserved(self) -> None:
        text = 'a = 1  # keep me\nmodel_instructions_file = "./base.md"   # 指令副本注释\n'
        doc = ConfigDocument(text.encode())
        out = doc.set_assignment((KEY,), '"D:/copy.md"').decode()
        self.assertIn('model_instructions_file = "D:/copy.md"   # 指令副本注释\n', out)
        self.assertIn("a = 1  # keep me", out)

    def test_hash_inside_value_is_not_a_comment(self) -> None:
        text = 'model_instructions_file = "./base.md#frag"  # tail\n'
        doc = ConfigDocument(text.encode())
        item = doc.find((KEY,))
        self.assertEqual(item.comment, "  # tail")
        out = doc.set_assignment((KEY,), '"D:/copy.md"').decode()
        self.assertIn('= "D:/copy.md"  # tail', out)

    def test_insert_into_section_with_subtable(self) -> None:
        """目标段后面还有子表时，键必须留在本段内，不能落进子表。"""
        text = ('profile = "work"\n\n[profiles.work]\nmodel = "gpt-5"\n\n'
                '[profiles.work.mcp]\nenabled = true\n')
        out = ConfigDocument(text.encode()).set_assignment(("profiles", "work", KEY), '"y"').decode()
        data = tomllib.loads(out)
        self.assertEqual(data["profiles"]["work"][KEY], "y")
        self.assertNotIn(KEY, data["profiles"]["work"]["mcp"])
        self.assertLess(out.index(KEY), out.index("[profiles.work.mcp]"))

    def test_insert_into_section_with_multiple_subtables(self) -> None:
        text = ('[profiles.work]\nmodel = "gpt-5"\n\n[profiles.work.mcp]\nenabled = true\n\n'
                '[profiles.work.tools]\nname = "x"\n\n[other]\nk = 1\n')
        out = ConfigDocument(text.encode()).set_assignment(("profiles", "work", KEY), '"y"').decode()
        data = tomllib.loads(out)
        self.assertEqual(data["profiles"]["work"][KEY], "y")
        self.assertNotIn(KEY, data["profiles"]["work"]["mcp"])
        self.assertNotIn(KEY, data["profiles"]["work"]["tools"])
        self.assertLess(out.index(KEY), out.index("[profiles.work.mcp]"))

    def test_without_only_removes_target_leaf(self) -> None:
        from xiaoli_engine import _without

        missing = {"profiles": {"work": {"model": "gpt-5", "mcp": {"a": 1}}, "other": 2}}
        # 目标键不存在时必须整棵原样返回，否则任何同级改动都会被漏检
        self.assertEqual(_without(missing, ("profiles", "work", KEY)), missing)
        present = {"profiles": {"work": {"model": "gpt-5", KEY: "x"}}}
        stripped = _without(present, ("profiles", "work", KEY))
        self.assertNotIn(KEY, stripped["profiles"]["work"])
        self.assertEqual(stripped["profiles"]["work"]["model"], "gpt-5")

    def test_verified_detects_change_in_sibling_subtree(self) -> None:
        from xiaoli_engine import _without

        before = {"profiles": {"work": {"model": "gpt-5", KEY: "./a.md"}}}
        after = {"profiles": {"work": {"model": "gpt-5-changed", KEY: "./a.md"}}}
        self.assertNotEqual(_without(before, ("profiles", "work", KEY)),
                            _without(after, ("profiles", "work", KEY)))

    def test_write_raw_restores_original_line(self) -> None:
        original = 'model_instructions_file = "./base.md"\n'
        doc = ConfigDocument(b'a = 1\n' + original.encode())
        item = doc.find((KEY,))
        installed = doc.set_assignment((KEY,), '"D:/copy.md"')
        again = ConfigDocument(installed).write_raw((KEY,), item.raw).decode()
        self.assertIn(original, again)
        self.assertNotIn("copy.md", again)

    def test_invalid_toml_raises(self) -> None:
        with self.assertRaises(ConfigError):
            ConfigDocument(b'= = =\n')

    def test_non_utf8_raises(self) -> None:
        with self.assertRaises(ConfigError):
            ConfigDocument(b'\xff\xfe\x00\x00bad')


if __name__ == "__main__":
    unittest.main()

"""工具闸门的测试：重点是「失败也要变成模型能读的反馈」。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.tools import Toolbox


class ToolboxGates(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "README.md").write_text("hello\nworld\n", encoding="utf-8")
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def box(self, **kwargs) -> Toolbox:
        kwargs.setdefault("approval", "auto")
        return Toolbox(self.root, **kwargs)

    def test_unknown_tool(self):
        self.assertTrue(self.box().run("nope", {}).startswith("error: unknown tool"))

    def test_missing_required_argument(self):
        message = self.box().run("read_file", {})
        self.assertTrue(message.startswith("error: invalid arguments"))
        self.assertIn("path", message)

    def test_path_escape_is_rejected(self):
        message = self.box().run("read_file", {"path": "../../etc/passwd"})
        self.assertTrue(message.startswith("error: path escapes workspace"))

    def test_read_only_denies_write(self):
        message = self.box(read_only=True).run("write_file", {"path": "a.txt", "content": "x"})
        self.assertTrue(message.startswith("error: approval denied"))

    def test_never_denies_write(self):
        message = self.box(approval="never").run("write_file", {"path": "a.txt", "content": "x"})
        self.assertTrue(message.startswith("error: approval denied"))

    def test_custom_approver_can_reject(self):
        box = Toolbox(self.root, approval="ask", approver=lambda name, args: False)
        self.assertTrue(box.run("run_shell", {"command": "echo hi"}).startswith("error: approval denied"))

    def test_patch_requires_exactly_one_match(self):
        (self.root / "dup.txt").write_text("a\na\n", encoding="utf-8")
        message = self.box().run("patch_file", {"path": "dup.txt", "old_text": "a", "new_text": "b"})
        self.assertIn("found 2", message)

        message = self.box().run("patch_file", {"path": "dup.txt", "old_text": "zzz", "new_text": "b"})
        self.assertIn("found 0", message)

    def test_patch_success(self):
        message = self.box().run("patch_file", {"path": "README.md", "old_text": "world", "new_text": "there"})
        self.assertIn("patched", message)
        self.assertIn("there", (self.root / "README.md").read_text(encoding="utf-8"))

    def test_output_is_clipped(self):
        (self.root / "big.txt").write_text("x" * 9000, encoding="utf-8")
        message = self.box().run("read_file", {"path": "big.txt", "end": 1})
        self.assertLessEqual(len(message), 4200)

        message = self.box().run("write_file", {"path": "out.txt", "content": "y" * 9000})
        self.assertIn("wrote 9000 chars", message)

    def test_write_and_list(self):
        box = self.box()
        self.assertIn("wrote", box.run("write_file", {"path": "new/file.txt", "content": "ok"}))
        listing = box.run("list_files", {"path": "."})
        self.assertIn("pkg", listing)
        self.assertIn("README.md", listing)

    def test_shell_reports_exit_code(self):
        message = self.box().run("run_shell", {"command": "echo hi"})
        self.assertIn("exit=0", message)

    def test_specs_shape(self):
        specs = self.box().specs()
        self.assertEqual(len(specs), 6)
        names = {item["function"]["name"] for item in specs}
        self.assertIn("run_shell", names)
        self.assertEqual(specs[0]["type"], "function")


if __name__ == "__main__":
    unittest.main()

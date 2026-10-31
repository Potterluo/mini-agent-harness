"""工具层的测试：五道闸、六个内置工具、输出裁剪、自定义工具注册。

全部用 tempfile 建临时仓库，不碰真实目录。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from harness.tools import MAX_OUTPUT_CHARS, Toolbox


class EchoTool:
    """最小可用的自定义工具，用来验证 register / specs / run。"""

    name = "echo"
    description = "原样返回 text"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    risky = False

    def invoke(self, arguments: dict) -> str:
        return arguments["text"]


class RiskyEchoTool(EchoTool):
    name = "risky_echo"
    risky = True


class BoomTool:
    name = "boom"
    description = "总是失败"
    parameters = {"type": "object", "properties": {}, "required": []}
    risky = False

    def invoke(self, arguments: dict) -> str:
        raise RuntimeError("kaput")


class BigTool:
    name = "big"
    description = "输出超长文本"
    parameters = {"type": "object", "properties": {}, "required": []}
    risky = False

    def invoke(self, arguments: dict) -> str:
        return "z" * (MAX_OUTPUT_CHARS + 1000)


class NoneTool:
    name = "none"
    description = "返回 None"
    parameters = {"type": "object", "properties": {}, "required": []}
    risky = False

    def invoke(self, arguments: dict):
        return None


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def box(self, **kwargs) -> Toolbox:
        kwargs.setdefault("approval", "auto")
        return Toolbox(self.root, **kwargs)

    def put(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        return path

    def read(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")


class GateOrderTest(_Case):
    """闸 1~3：未知工具、参数、路径，以及它们之间不能调换的顺序。"""

    def test_unknown_tool(self):
        self.assertEqual(self.box().run("nope", {}), "error: unknown tool 'nope'")

    def test_unknown_tool_beats_bad_arguments(self):
        # 闸 1 在闸 2 之前：名字都错了，就别再报参数问题。
        self.assertEqual(self.box().run("nope", None), "error: unknown tool 'nope'")

    def test_missing_required_argument(self):
        message = self.box().run("write_file", {"path": "a.txt"})
        self.assertTrue(message.startswith("error: invalid arguments for write_file:"))
        self.assertIn("content", message)

    def test_arguments_must_be_an_object(self):
        message = self.box().run("list_files", ["."])
        self.assertTrue(message.startswith("error: invalid arguments for list_files:"))
        self.assertIn("expected an object", message)

    def test_wrong_argument_type(self):
        message = self.box().run("read_file", {"path": "a.txt", "start": "2"})
        self.assertTrue(message.startswith("error: invalid arguments for read_file:"))
        self.assertIn("'start' must be of type integer", message)

    def test_bool_is_not_an_integer(self):
        message = self.box().run("read_file", {"path": "a.txt", "end": True})
        self.assertTrue(message.startswith("error: invalid arguments for read_file:"))
        self.assertIn("must be of type integer", message)

    def test_unknown_argument(self):
        message = self.box().run("list_files", {"depth": 2})
        self.assertTrue(message.startswith("error: invalid arguments for list_files:"))
        self.assertIn("unknown argument 'depth'", message)

    def test_below_minimum(self):
        self.put("a.txt", "ok\n")
        message = self.box().run("read_file", {"path": "a.txt", "start": 0})
        self.assertTrue(message.startswith("error: invalid arguments for read_file:"))
        self.assertIn("must be >= 1", message)

    def test_path_escape_is_rejected(self):
        message = self.box().run("read_file", {"path": "../../etc/passwd"})
        self.assertEqual(message, "error: path escapes workspace: ../../etc/passwd")

    def test_path_escape_does_not_touch_the_outside_file(self):
        name = f"escape-probe-{uuid.uuid4().hex}.txt"
        outside = self.root.parent / name
        message = self.box().run("write_file", {"path": f"../{name}", "content": "x"})
        self.assertEqual(message, f"error: path escapes workspace: ../{name}")
        self.assertFalse(outside.exists())

    def test_absolute_path_outside_root_is_rejected(self):
        outside = self.root.parent / f"probe-{uuid.uuid4().hex}.txt"
        message = self.box().run("write_file", {"path": str(outside), "content": "x"})
        self.assertEqual(message, f"error: path escapes workspace: {outside}")
        self.assertFalse(outside.exists())

    def test_absolute_path_inside_root_is_allowed(self):
        target = self.root / "abs.txt"
        result = self.box().run("write_file", {"path": str(target), "content": "ok"})
        self.assertEqual(result, "wrote 2 chars to abs.txt")
        self.assertEqual(self.read("abs.txt"), "ok")

    def test_argument_gate_beats_path_gate(self):
        # 参数都不全，先别管路径越不越界。
        message = self.box().run("write_file", {"path": "../x.txt"})
        self.assertTrue(message.startswith("error: invalid arguments for write_file:"))

    def test_path_gate_beats_approval_gate(self):
        box = Toolbox(self.root, approval="never")
        message = box.run("write_file", {"path": "../x.txt", "content": "x"})
        self.assertEqual(message, "error: path escapes workspace: ../x.txt")


class ApprovalTest(_Case):
    """闸 4：read_only、approval 三态、approver 调用约定。"""

    def test_read_only_denies_every_risky_tool(self):
        self.put("a.txt", "hello\n")
        box = Toolbox(self.root, read_only=True)

        self.assertEqual(
            box.run("run_shell", {"command": "echo hi"}),
            "error: approval denied for run_shell",
        )
        self.assertEqual(
            box.run("write_file", {"path": "b.txt", "content": "x"}),
            "error: approval denied for write_file",
        )
        self.assertEqual(
            box.run("patch_file", {"path": "a.txt", "old_text": "hello", "new_text": "bye"}),
            "error: approval denied for patch_file",
        )
        self.assertFalse((self.root / "b.txt").exists())
        self.assertEqual(self.read("a.txt"), "hello\n")

    def test_read_only_still_allows_safe_tools(self):
        self.put("a.txt", "hello\n")
        box = Toolbox(self.root, read_only=True)
        self.assertEqual(box.run("read_file", {"path": "a.txt"}), "hello")
        self.assertEqual(box.run("list_files", {}), "a.txt")

    def test_never_denies_even_with_a_willing_approver(self):
        box = Toolbox(self.root, approval="never", approver=lambda name, args: True)
        self.assertEqual(
            box.run("write_file", {"path": "a.txt", "content": "x"}),
            "error: approval denied for write_file",
        )

    def test_auto_allows_without_asking(self):
        calls: list[str] = []
        box = Toolbox(self.root, approval="auto", approver=lambda name, args: calls.append(name) or False)
        self.assertEqual(box.run("write_file", {"path": "a.txt", "content": "x"}), "wrote 1 chars to a.txt")
        self.assertEqual(calls, [])

    def test_ask_uses_the_approver_with_name_and_arguments(self):
        seen: dict[str, object] = {}

        def approver(name: str, arguments: dict) -> bool:
            seen["name"] = name
            seen["arguments"] = arguments
            return True

        box = Toolbox(self.root, approver=approver)
        box.run("write_file", {"path": "a.txt", "content": "hi"})
        self.assertEqual(seen["name"], "write_file")
        self.assertEqual(seen["arguments"], {"path": "a.txt", "content": "hi"})

    def test_approver_is_not_asked_for_safe_tools(self):
        calls: list[str] = []
        box = Toolbox(self.root, approver=lambda name, args: calls.append(name) or True)
        self.put("a.txt", "ok\n")
        box.run("read_file", {"path": "a.txt"})
        box.run("list_files", {})
        self.assertEqual(calls, [])

    def test_approver_rejection_denies(self):
        box = Toolbox(self.root, approver=lambda name, args: False)
        self.assertEqual(
            box.run("write_file", {"path": "a.txt", "content": "x"}),
            "error: approval denied for write_file",
        )

    def test_approver_exception_becomes_denial(self):
        def approver(name: str, arguments: dict) -> bool:
            raise RuntimeError("终端坏了")

        box = Toolbox(self.root, approver=approver)
        self.assertEqual(
            box.run("write_file", {"path": "a.txt", "content": "x"}),
            "error: approval denied for write_file",
        )

    def test_default_approver_asks_on_the_terminal(self):
        with mock.patch("builtins.input", return_value="y"):
            box = Toolbox(self.root)  # 默认 approval="ask"
            self.assertEqual(box.run("write_file", {"path": "a.txt", "content": "x"}), "wrote 1 chars to a.txt")

        with mock.patch("builtins.input", return_value="n"):
            box = Toolbox(self.root)
            self.assertEqual(
                box.run("write_file", {"path": "b.txt", "content": "x"}),
                "error: approval denied for write_file",
            )
        self.assertFalse((self.root / "b.txt").exists())

    def test_default_approver_denies_on_eof(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            box = Toolbox(self.root)
            self.assertEqual(
                box.run("write_file", {"path": "a.txt", "content": "x"}),
                "error: approval denied for write_file",
            )

    def test_invalid_approval_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            Toolbox(self.root, approval="sometimes")


class FileToolTest(_Case):
    """list_files / read_file / write_file 的正常路径。"""

    def test_write_then_read_roundtrip(self):
        box = self.box()
        content = "第一行\nsecond line\n"
        result = box.run("write_file", {"path": "nested/dir/note.txt", "content": content})
        self.assertEqual(result, f"wrote {len(content)} chars to nested/dir/note.txt")

        # read_file 不补末尾换行。
        self.assertEqual(box.run("read_file", {"path": "nested/dir/note.txt"}), "第一行\nsecond line")

    def test_write_overwrites(self):
        box = self.box()
        box.run("write_file", {"path": "a.txt", "content": "long content"})
        box.run("write_file", {"path": "a.txt", "content": "short"})
        self.assertEqual(self.read("a.txt"), "short")

    def test_read_line_range(self):
        self.put("lines.txt", "l1\nl2\nl3\nl4\n")
        box = self.box()
        self.assertEqual(box.run("read_file", {"path": "lines.txt"}), "l1\nl2\nl3\nl4")
        self.assertEqual(box.run("read_file", {"path": "lines.txt", "start": 2, "end": 3}), "l2\nl3")
        self.assertEqual(box.run("read_file", {"path": "lines.txt", "start": 3}), "l3\nl4")
        self.assertEqual(box.run("read_file", {"path": "lines.txt", "end": 2}), "l1\nl2")
        self.assertEqual(box.run("read_file", {"path": "lines.txt", "end": 99}), "l1\nl2\nl3\nl4")

    def test_read_past_the_end_says_so(self):
        self.put("lines.txt", "l1\nl2\n")
        message = self.box().run("read_file", {"path": "lines.txt", "start": 99})
        self.assertTrue(message.startswith("(no lines selected"))

    def test_read_backwards_range_fails_softly(self):
        self.put("lines.txt", "l1\nl2\n")
        message = self.box().run("read_file", {"path": "lines.txt", "start": 2, "end": 1})
        self.assertTrue(message.startswith("error: tool read_file failed:"))
        self.assertIn("is before start", message)

    def test_read_empty_file(self):
        self.put("empty.txt", "")
        self.assertEqual(self.box().run("read_file", {"path": "empty.txt"}), "(empty file)")

    def test_read_missing_file_fails_softly(self):
        message = self.box().run("read_file", {"path": "missing.txt"})
        self.assertTrue(message.startswith("error: tool read_file failed:"))
        self.assertIn("no such file", message)

    def test_read_directory_fails_softly(self):
        (self.root / "pkg").mkdir()
        message = self.box().run("read_file", {"path": "pkg"})
        self.assertTrue(message.startswith("error: tool read_file failed:"))
        self.assertIn("is a directory", message)

    def test_list_files_sorts_directories_first(self):
        self.put("README.md", "hello\n")
        self.put("pkg/mod.py", "x = 1\n")
        self.put("a.txt", "a\n")
        self.assertEqual(self.box().run("list_files", {}), "pkg/\na.txt\nREADME.md")

    def test_list_files_accepts_a_subdirectory(self):
        self.put("pkg/mod.py", "x = 1\n")
        self.assertEqual(self.box().run("list_files", {"path": "pkg"}), "mod.py")

    def test_list_files_empty_directory(self):
        (self.root / "empty").mkdir()
        self.assertEqual(self.box().run("list_files", {"path": "empty"}), "(empty directory)")

    def test_list_files_on_a_file_fails_softly(self):
        self.put("a.txt", "a\n")
        message = self.box().run("list_files", {"path": "a.txt"})
        self.assertTrue(message.startswith("error: tool list_files failed:"))
        self.assertIn("not a directory", message)

    def test_list_files_missing_directory_fails_softly(self):
        message = self.box().run("list_files", {"path": "nope"})
        self.assertTrue(message.startswith("error: tool list_files failed:"))
        self.assertIn("no such directory", message)


class PatchToolTest(_Case):
    """patch_file 的核心约束：old_text 必须恰好出现一次。"""

    def test_patch_replaces_the_single_occurrence(self):
        self.put("a.txt", "alpha\nbeta\ngamma\n")
        result = self.box().run("patch_file", {"path": "a.txt", "old_text": "beta", "new_text": "BETA"})
        self.assertEqual(result, "patched a.txt (1 replacement)")
        self.assertEqual(self.read("a.txt"), "alpha\nBETA\ngamma\n")

    def test_patch_with_empty_new_text_deletes(self):
        self.put("a.txt", "keep\ndrop\n")
        self.box().run("patch_file", {"path": "a.txt", "old_text": "drop\n", "new_text": ""})
        self.assertEqual(self.read("a.txt"), "keep\n")

    def test_patch_rejects_zero_occurrences(self):
        self.put("a.txt", "alpha\n")
        message = self.box().run("patch_file", {"path": "a.txt", "old_text": "zzz", "new_text": "x"})
        self.assertEqual(message, "error: tool patch_file failed: old_text not found in a.txt")

    def test_patch_rejects_two_occurrences(self):
        self.put("dup.txt", "a\na\n")
        message = self.box().run("patch_file", {"path": "dup.txt", "old_text": "a", "new_text": "b"})
        self.assertTrue(message.startswith("error: tool patch_file failed:"))
        self.assertIn("appears 2 times", message)
        self.assertEqual(self.read("dup.txt"), "a\na\n")

    def test_patch_rejects_empty_old_text(self):
        self.put("a.txt", "alpha\n")
        message = self.box().run("patch_file", {"path": "a.txt", "old_text": "", "new_text": "x"})
        self.assertIn("old_text must not be empty", message)

    def test_patch_missing_file_fails_softly(self):
        message = self.box().run("patch_file", {"path": "nope.txt", "old_text": "a", "new_text": "b"})
        self.assertTrue(message.startswith("error: tool patch_file failed:"))
        self.assertIn("no such file", message)


class SearchToolTest(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.put("notes.txt", "hello world\nsecond line\n")
        self.put("code.py", "hello python\n")
        self.put("pkg/mod.py", "x = needle\n")

    def test_search_reports_path_line_and_text(self):
        result = self.box().run("search", {"query": "hello"})
        self.assertEqual(set(result.splitlines()), {"notes.txt:1:hello world", "code.py:1:hello python"})

    def test_search_is_literal_not_regex(self):
        self.put("regex.txt", "a.c\nabc\n")
        result = self.box().run("search", {"query": "a.c"})
        self.assertEqual(set(result.splitlines()), {"regex.txt:1:a.c"})

    def test_search_honours_glob(self):
        result = self.box().run("search", {"query": "hello", "glob": "*.py"})
        self.assertEqual(set(result.splitlines()), {"code.py:1:hello python"})

    def test_search_recurses_into_subdirectories(self):
        self.assertEqual(self.box().run("search", {"query": "needle"}), "pkg/mod.py:1:x = needle")

    def test_search_without_matches(self):
        self.assertEqual(self.box().run("search", {"query": "nothing-here"}), "no matches for 'nothing-here'")

    def test_search_rejects_empty_query(self):
        message = self.box().run("search", {"query": ""})
        self.assertTrue(message.startswith("error: tool search failed:"))
        self.assertIn("must not be empty", message)


class ShellToolTest(_Case):
    def test_shell_runs_in_the_workspace(self):
        command = f'"{sys.executable}" -c "import os; print(os.getcwd())"'
        result = self.box().run("run_shell", {"command": command})
        actual = os.path.normcase(str(Path(result.strip()).resolve()))
        self.assertEqual(actual, os.path.normcase(str(self.root)))

    def test_shell_returns_stdout(self):
        result = self.box().run("run_shell", {"command": "echo hi"})
        self.assertEqual(result.strip(), "hi")
        self.assertNotIn("exit code", result)

    def test_shell_reports_nonzero_exit(self):
        command = f'"{sys.executable}" -c "import sys; sys.exit(7)"'
        self.assertEqual(self.box().run("run_shell", {"command": command}), "[exit code 7]")

    def test_shell_rejects_empty_command(self):
        message = self.box().run("run_shell", {"command": "   "})
        self.assertTrue(message.startswith("error: tool run_shell failed:"))
        self.assertIn("must not be empty", message)


class ClipTest(_Case):
    def test_long_output_is_clipped_with_the_removed_count(self):
        self.put("big.txt", "x" * (MAX_OUTPUT_CHARS + 5000))
        result = self.box().run("read_file", {"path": "big.txt"})
        removed = 9000 - MAX_OUTPUT_CHARS
        self.assertEqual(result, "x" * MAX_OUTPUT_CHARS + f"\n...[truncated {removed} chars]")
        self.assertEqual(len(result), MAX_OUTPUT_CHARS + len(f"\n...[truncated {removed} chars]"))

    def test_exactly_at_the_limit_is_untouched(self):
        self.put("edge.txt", "y" * MAX_OUTPUT_CHARS)
        result = self.box().run("read_file", {"path": "edge.txt"})
        self.assertEqual(result, "y" * MAX_OUTPUT_CHARS)
        self.assertNotIn("truncated", result)

    def test_clip_applies_to_custom_tools_too(self):
        box = self.box()
        box.register(BigTool())
        self.assertEqual(
            box.run("big", {}),
            "z" * MAX_OUTPUT_CHARS + "\n...[truncated 1000 chars]",
        )


class RegisterAndSpecTest(_Case):
    def test_specs_shape(self):
        specs = self.box().specs()
        self.assertEqual(len(specs), 6)
        self.assertEqual(
            {item["function"]["name"] for item in specs},
            {"list_files", "read_file", "search", "run_shell", "write_file", "patch_file"},
        )
        for item in specs:
            self.assertEqual(set(item), {"type", "function"})
            self.assertEqual(item["type"], "function")
            function = item["function"]
            # risky 是本地闸门用的，不进发给模型的协议字段。
            self.assertEqual(set(function), {"name", "description", "parameters"})
            self.assertTrue(function["description"])
            self.assertEqual(function["parameters"]["type"], "object")

    def test_specs_returns_copies(self):
        box = self.box()
        specs = box.specs()
        path_spec = next(
            item for item in specs if item["function"]["name"] == "list_files"
        )
        path_spec["function"]["parameters"]["properties"]["path"]["description"] = "被改了"
        fresh = next(item for item in box.specs() if item["function"]["name"] == "list_files")
        self.assertNotEqual(
            fresh["function"]["parameters"]["properties"]["path"]["description"], "被改了"
        )

    def test_register_adds_to_specs_and_run(self):
        box = self.box()
        box.register(EchoTool())
        self.assertIn("echo", {item["function"]["name"] for item in box.specs()})
        self.assertEqual(box.run("echo", {"text": "hi"}), "hi")
        self.assertTrue(box.run("echo", {}).startswith("error: invalid arguments for echo:"))

    def test_register_rejects_duplicate_names(self):
        box = self.box()
        box.register(EchoTool())
        with self.assertRaises(ValueError):
            box.register(EchoTool())

    def test_registered_risky_tool_still_goes_through_approval(self):
        box = Toolbox(self.root, read_only=True)
        box.register(RiskyEchoTool())
        self.assertEqual(box.run("risky_echo", {"text": "x"}), "error: approval denied for risky_echo")

    def test_tool_exception_becomes_an_error_string(self):
        box = self.box()
        box.register(BoomTool())
        self.assertEqual(box.run("boom", {}), "error: tool boom failed: kaput")

    def test_non_string_output_is_coerced(self):
        box = self.box()
        box.register(NoneTool())
        self.assertEqual(box.run("none", {}), "")


if __name__ == "__main__":
    unittest.main()

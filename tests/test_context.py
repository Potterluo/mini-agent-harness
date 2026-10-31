"""contract/context 的测试：剪裁边界、只动老工具结果、不就地改。"""

from __future__ import annotations

import unittest

from harness.context import MAX_TOOL_OUTPUT, clip, compact
from harness.domain import AssistantMessage, ToolCall, ToolResult, UserMessage

MARKER = "\n...[truncated {0} chars]"


def marker(removed: int) -> str:
    return MARKER.format(removed)


class ClipTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(clip("hello"), "hello")

    def test_exactly_at_limit_is_untouched(self):
        text = "a" * MAX_TOOL_OUTPUT
        self.assertEqual(clip(text), text)
        self.assertNotIn("truncated", clip(text))

    def test_one_over_limit_is_truncated(self):
        text = "a" * (MAX_TOOL_OUTPUT + 1)
        self.assertEqual(clip(text), "a" * MAX_TOOL_OUTPUT + marker(1))

    def test_limit_is_customisable(self):
        self.assertEqual(clip("abcdefghijklmno", 10), "abcdefghij" + marker(5))

    def test_truncated_count_is_removed_chars_not_total(self):
        text = "0123456789"
        result = clip(text, 4)
        self.assertTrue(result.startswith("0123"))
        self.assertTrue(result.endswith(marker(6)))
        self.assertEqual(len(result), 4 + len(marker(6)))


class CompactTest(unittest.TestCase):
    def _tool(self, index: int, size: int = 1000) -> ToolResult:
        return ToolResult(tool_call_id=f"call-{index}", content=f"{index}:" + "x" * size)

    def test_recent_tool_results_are_kept_verbatim(self):
        history = [self._tool(index) for index in range(8)]
        result = compact(history, keep_recent=2)
        self.assertIs(result[-1], history[-1])
        self.assertIs(result[-2], history[-2])
        self.assertEqual(result[-1].content, history[-1].content)

    def test_old_tool_results_are_pressed_to_old_limit(self):
        history = [self._tool(index) for index in range(8)]
        result = compact(history, keep_recent=2, old_limit=400)
        for index in range(6):
            old = history[index]
            new = result[index]
            self.assertIsInstance(new, ToolResult)
            self.assertIsNot(new, old)
            self.assertEqual(new.tool_call_id, old.tool_call_id)
            self.assertEqual(new.content, old.content[:400] + marker(len(old.content) - 400))

    def test_user_and_assistant_messages_are_never_touched(self):
        call = ToolCall(id="call-0", name="read_file", arguments={"path": "a.py"})
        user = UserMessage(text="u" * 900)
        assistant = AssistantMessage(text="a" * 900, tool_calls=(call,))
        history = [user, assistant, self._tool(0), self._tool(1), self._tool(2)]
        result = compact(history, keep_recent=1)
        self.assertIs(result[0], user)
        self.assertIs(result[1], assistant)
        self.assertEqual(len(result[0].text), 900)
        self.assertEqual(len(result[1].text), 900)

    def test_input_is_not_modified(self):
        history = [self._tool(index) for index in range(4)]
        snapshot = [(message.tool_call_id, message.content) for message in history]
        result = compact(history, keep_recent=1)
        self.assertIsNot(result, history)
        self.assertEqual([(m.tool_call_id, m.content) for m in history], snapshot)
        self.assertEqual(len(history), 4)

    def test_fewer_tool_results_than_keep_recent_change_nothing(self):
        history = [self._tool(0), self._tool(1), UserMessage(text="ok")]
        result = compact(history, keep_recent=6)
        self.assertEqual(result, history)

    def test_dict_messages_are_supported(self):
        history = [
            {"role": "user", "content": "u" * 900},
            {"role": "assistant", "content": "a" * 900},
            {"role": "tool", "tool_call_id": "c0", "content": "0:" + "y" * 999},
            {"role": "tool", "tool_call_id": "c1", "content": "1:" + "z" * 999},
        ]
        result = compact(history, keep_recent=1, old_limit=400)
        self.assertEqual(result[0], history[0])
        self.assertEqual(result[1], history[1])
        self.assertEqual(len(result[0]["content"]), 900)
        self.assertEqual(result[2]["tool_call_id"], "c0")
        long_content = "0:" + "y" * 999
        self.assertEqual(result[2]["content"], long_content[:400] + marker(len(long_content) - 400))
        self.assertEqual(result[3], history[3])
        self.assertEqual(len(history[2]["content"]), 1001)

    def test_mixed_dataclass_and_dict_history(self):
        history = [
            UserMessage(text="查一下"),
            {"role": "tool", "content": "d" * 800},
            ToolResult(tool_call_id="c2", content="t" * 800),
            {"role": "assistant", "content": "好"},
        ]
        result = compact(history, keep_recent=1, old_limit=100)
        self.assertIsInstance(result[1], dict)
        self.assertEqual(result[1]["content"], "d" * 100 + marker(700))
        self.assertIsInstance(result[2], ToolResult)
        self.assertIs(result[2], history[2])
        self.assertEqual(result[3], history[3])

    def test_keep_recent_zero_presses_everything(self):
        history = [self._tool(0), self._tool(1)]
        result = compact(history, keep_recent=0, old_limit=10)
        for original, item in zip(history, result):
            self.assertEqual(item.content, original.content[:10] + marker(len(original.content) - 10))


if __name__ == "__main__":
    unittest.main()

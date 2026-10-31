"""session 的测试：新建、落盘往返、目录排序，以及 remember 的容量语义。"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from harness.domain import AssistantMessage, Session, ToolCall, ToolResult, UserMessage
from harness.session import JsonlSessionStore, new_session, remember

ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class NewSessionTest(TempDirTest):
    def test_id_shape(self):
        session = new_session(self.root)
        self.assertRegex(session.id, ID_PATTERN)

    def test_ids_do_not_repeat(self):
        ids = {new_session(self.root).id for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_memory_is_prefilled(self):
        session = new_session(self.root)
        self.assertEqual(session.memory, {"task": "", "files": [], "notes": []})
        self.assertEqual(session.messages, [])

    def test_memory_lists_are_not_shared_between_sessions(self):
        first = new_session(self.root)
        second = new_session(self.root)
        first.memory["files"].append("a.py")
        self.assertEqual(second.memory["files"], [])


class RoundTripTest(TempDirTest):
    def _build(self) -> Session:
        session = Session(id="20261031-153012-ab12cd")
        session.append(UserMessage(text="读一下 harness/domain.py"))
        session.append(AssistantMessage(
            text="先看文件",
            tool_calls=(
                ToolCall(id="call-1", name="read_file", arguments={"path": "harness/domain.py"}),
                ToolCall(id="call-2", name="list_dir", arguments={"path": ".", "depth": 2}),
            ),
        ))
        session.append(ToolResult(tool_call_id="call-1", content="line 1\n行 2\n" + "x" * 500))
        session.memory = {"task": "读契约", "files": ["harness/domain.py"], "notes": ["分层"]}
        return session

    def test_put_then_get_restores_everything(self):
        store = JsonlSessionStore(self.root)
        original = self._build()
        store.put(original)
        restored = store.get(original.id)

        self.assertIsNotNone(restored)
        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.memory, original.memory)
        self.assertEqual(len(restored.messages), 3)

        user, assistant, tool = restored.messages
        self.assertIsInstance(user, UserMessage)
        self.assertEqual(user.text, "读一下 harness/domain.py")

        self.assertIsInstance(assistant, AssistantMessage)
        self.assertEqual(assistant.text, "先看文件")
        self.assertIsInstance(assistant.tool_calls, tuple)
        self.assertEqual(len(assistant.tool_calls), 2)
        self.assertIsInstance(assistant.tool_calls[0], ToolCall)
        self.assertEqual(assistant.tool_calls[0].id, "call-1")
        self.assertEqual(assistant.tool_calls[0].name, "read_file")
        self.assertEqual(assistant.tool_calls[0].arguments, {"path": "harness/domain.py"})
        self.assertEqual(assistant.tool_calls[1].arguments, {"path": ".", "depth": 2})

        self.assertIsInstance(tool, ToolResult)
        self.assertEqual(tool.tool_call_id, "call-1")
        self.assertEqual(tool.content, original.messages[2].content)

        # 读回来的对象要能直接接上领域层的行为
        self.assertEqual(restored.last_user_text(), "读一下 harness/domain.py")

    def test_put_creates_missing_directories(self):
        nested = self.root / "a" / "b" / "sessions"
        store = JsonlSessionStore(nested)
        store.put(self._build())
        self.assertTrue((nested / "20261031-153012-ab12cd.json").is_file())

    def test_put_overwrites_previous_version(self):
        store = JsonlSessionStore(self.root)
        session = self._build()
        store.put(session)
        session.append(UserMessage(text="第二轮"))
        store.put(session)
        restored = store.get(session.id)
        self.assertEqual(len(restored.messages), 4)
        self.assertEqual(restored.messages[-1].text, "第二轮")

    def test_get_unknown_id_returns_none(self):
        store = JsonlSessionStore(self.root)
        self.assertIsNone(store.get("20261031-153012-ab12cd"))

    def test_get_rejects_path_traversal(self):
        store = JsonlSessionStore(self.root)
        self.assertIsNone(store.get("../escape"))

    def test_list_ids_missing_directory_is_empty(self):
        store = JsonlSessionStore(self.root / "nope")
        self.assertEqual(store.list_ids(), [])
        self.assertIsNone(store.latest())


class ListIdsTest(TempDirTest):
    def test_sorted_by_mtime_descending(self):
        store = JsonlSessionStore(self.root)
        stamps = {"20260101-000000-aaaaaa": 1_000, "20260102-000000-bbbbbb": 3_000,
                  "20260103-000000-cccccc": 2_000}
        for session_id, mtime in stamps.items():
            store.put(Session(id=session_id))
            path = self.root / f"{session_id}.json"
            os.utime(path, (mtime, mtime))

        self.assertEqual(store.list_ids(),
                         ["20260102-000000-bbbbbb", "20260103-000000-cccccc",
                          "20260101-000000-aaaaaa"])

    def test_latest_returns_most_recent(self):
        store = JsonlSessionStore(self.root)
        old = Session(id="20260101-000000-aaaaaa")
        new = Session(id="20260102-000000-bbbbbb")
        store.put(old)
        store.put(new)
        os.utime(self.root / f"{old.id}.json", (1_000, 1_000))
        os.utime(self.root / f"{new.id}.json", (2_000, 2_000))
        self.assertEqual(store.latest(), new.id)

    def test_latest_on_empty_store_is_none(self):
        store = JsonlSessionStore(self.root)
        store.put(Session(id="20260101-000000-aaaaaa"))
        os.remove(self.root / "20260101-000000-aaaaaa.json")
        self.assertIsNone(store.latest())


class RememberTest(unittest.TestCase):
    def test_appends_in_order(self):
        bucket: list[str] = []
        for item in ["a", "b", "c"]:
            remember(bucket, item, 5)
        self.assertEqual(bucket, ["a", "b", "c"])

    def test_duplicate_moves_to_the_end(self):
        bucket = ["a", "b", "c"]
        remember(bucket, "b", 5)
        self.assertEqual(bucket, ["a", "c", "b"])

    def test_overflow_drops_the_oldest(self):
        bucket: list[str] = []
        for item in ["a", "b", "c", "d"]:
            remember(bucket, item, 3)
        self.assertEqual(bucket, ["b", "c", "d"])

    def test_duplicate_at_capacity_does_not_drop(self):
        bucket = ["a", "b", "c"]
        remember(bucket, "a", 3)
        self.assertEqual(bucket, ["b", "c", "a"])

    def test_mutates_in_place(self):
        bucket = ["a"]
        result = remember(bucket, "b", 5)
        self.assertIsNone(result)
        self.assertEqual(bucket, ["a", "b"])

    def test_zero_limit_records_nothing(self):
        bucket = ["a"]
        remember(bucket, "b", 0)
        self.assertEqual(bucket, ["a"])

    def test_works_with_memory_dict_values(self):
        memory = new_session(Path(tempfile.gettempdir()))
        remember(memory.memory["files"], "a.py", 2)
        remember(memory.memory["files"], "b.py", 2)
        remember(memory.memory["files"], "a.py", 2)
        self.assertEqual(memory.memory["files"], ["b.py", "a.py"])


if __name__ == "__main__":
    unittest.main()

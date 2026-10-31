"""工作记忆的测试：每跑完一个工具，都要记一笔。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.domain import AssistantMessage, Decision, ToolCall
from harness.ports import ModelSetting
from harness.roles import AssistantRole, ReviewerRole
from harness.service import AgentService
from harness.session import JsonlSessionStore
from harness.tools import Toolbox


class FakeClient:
    def __init__(self, script, *, approve=True):
        self.script = list(script)
        self.approve = approve

    def generate(self, context, setting):
        if context.output_type is Decision:
            return Decision(approved=self.approve)
        return self.script.pop(0)


class MemoryBookkeeping(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.store = JsonlSessionStore(self.root / "sessions")

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, client):
        toolbox = Toolbox(self.root, approval="auto")
        return AgentService(
            store=self.store,
            client=client,
            assistant=AssistantRole("i", toolbox.tools),
            reviewer=ReviewerRole("r"),
            toolbox=toolbox,
            assistant_setting=ModelSetting("fake"),
            max_iterations=4,
        )

    def test_tool_calls_are_remembered(self):
        call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
        client = FakeClient([AssistantMessage(text="", tool_calls=(call,)), AssistantMessage(text="done")])
        service = self.build(client)

        session = service.new_session()
        service.send_message(session.id, "读一下 a.py")

        saved = self.store.get(session.id)
        self.assertIn("a.py", saved.memory["files"])
        self.assertTrue(any(note.startswith("read_file:") for note in saved.memory["notes"]))

    def test_memory_lists_are_capped(self):
        calls = [
            AssistantMessage(
                text="",
                tool_calls=(ToolCall(id=f"c{i}", name="read_file", arguments={"path": "a.py"}),),
            )
            for i in range(9)
        ]
        client = FakeClient(calls + [AssistantMessage(text="done")])
        service = self.build(client)

        session = service.new_session()
        service.send_message(session.id, "反复读同一个文件")

        saved = self.store.get(session.id)
        self.assertEqual(len(saved.memory["files"]), 1)
        self.assertLessEqual(len(saved.memory["notes"]), 5)


if __name__ == "__main__":
    unittest.main()

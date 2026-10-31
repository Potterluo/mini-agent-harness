"""主循环的集成测试：用一个假的模型客户端，把三道闸和持久化都跑一遍。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.domain import AssistantMessage, Decision, ToolCall, ToolResult, UserMessage
from harness.ports import ModelSetting
from harness.roles import AssistantRole, ReviewerRole
from harness.service import AgentService
from harness.session import JsonlSessionStore
from harness.tools import Toolbox


class FakeClient:
    """按脚本回放：tool_calls 阶段返回调用，之后返回纯文本。审核一律放行或一律拒绝。"""

    def __init__(self, script, *, approve=True):
        self.script = list(script)
        self.approve = approve
        self.rounds = 0

    def generate(self, context, setting):
        if context.output_type is Decision:
            return Decision(approved=self.approve, reason="" if self.approve else "policy")
        step = self.script.pop(0)
        self.rounds += 1
        return step


class ServiceLoop(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = JsonlSessionStore(self.root / "sessions")

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, client, *, approval="auto", read_only=False):
        toolbox = Toolbox(self.root, approval=approval, read_only=read_only)
        return AgentService(
            store=self.store,
            client=client,
            assistant=AssistantRole("你是助手", toolbox.tools),
            reviewer=ReviewerRole("你是审核员"),
            toolbox=toolbox,
            assistant_setting=ModelSetting("fake"),
            max_iterations=4,
        )

    def test_tool_call_then_final_answer(self):
        call = ToolCall(id="c1", name="write_file", arguments={"path": "a.txt", "content": "hi"})
        client = FakeClient([AssistantMessage(text="", tool_calls=(call,)), AssistantMessage(text="done")])
        service = self.build(client)

        session = service.new_session()
        answer = service.send_message(session.id, "写个文件")

        self.assertEqual(answer, "done")
        saved = self.store.get(session.id)
        self.assertIsInstance(saved.messages[0], UserMessage)
        self.assertIsInstance(saved.messages[2], ToolResult)
        self.assertEqual(saved.messages[2].tool_call_id, "c1")
        self.assertEqual((self.root / "a.txt").read_text(encoding="utf-8"), "hi")

    def test_rejected_call_never_executes(self):
        call = ToolCall(id="c2", name="write_file", arguments={"path": "b.txt", "content": "x"})
        client = FakeClient([AssistantMessage(text="", tool_calls=(call,)), AssistantMessage(text="ok")], approve=False)
        service = self.build(client)

        session = service.new_session()
        service.send_message(session.id, "写个文件")

        self.assertFalse((self.root / "b.txt").exists())
        saved = self.store.get(session.id)
        self.assertTrue(saved.messages[2].content.startswith("rejected:"))

    def test_denied_approval_is_reported_back_to_model(self):
        call = ToolCall(id="c3", name="write_file", arguments={"path": "c.txt", "content": "x"})
        client = FakeClient([AssistantMessage(text="", tool_calls=(call,)), AssistantMessage(text="ok")])
        service = self.build(client, approval="never")

        session = service.new_session()
        service.send_message(session.id, "写个文件")

        self.assertFalse((self.root / "c.txt").exists())
        saved = self.store.get(session.id)
        self.assertIn("approval denied", saved.messages[2].content)

    def test_iteration_cap(self):
        call = ToolCall(id="c4", name="list_files", arguments={"path": "."})
        script = [AssistantMessage(text="", tool_calls=(call,)) for _ in range(10)]
        service = self.build(FakeClient(script))

        session = service.new_session()
        answer = service.send_message(session.id, "一直列目录")
        self.assertIn("Stopped after", answer)


if __name__ == "__main__":
    unittest.main()

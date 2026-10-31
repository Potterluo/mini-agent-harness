"""应用层：主循环。

一个 agent 就是个循环：问模型、跑它要的工具、把结果喂回去，直到模型
用纯文本作答或者撞上步数上限。审核、持久化、事件都在这里串起来。
"""

from __future__ import annotations

from typing import Any, Callable

from .domain import AssistantMessage, Decision, Session, ToolCall, ToolResult, UserMessage
from .ports import ModelClient, ModelSetting, SessionStore


class AgentService:
    def __init__(
        self,
        *,
        store: SessionStore,
        client: ModelClient,
        assistant: Any,
        reviewer: Any,
        toolbox: Any,
        assistant_setting: ModelSetting,
        reviewer_setting: ModelSetting | None = None,
        max_iterations: int = 8,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ):
        self.store = store
        self.client = client
        self.assistant = assistant
        self.reviewer = reviewer
        self.toolbox = toolbox
        self.assistant_setting = assistant_setting
        self.reviewer_setting = reviewer_setting or assistant_setting
        self.max_iterations = max_iterations
        self.on_event = on_event or (lambda kind, payload: None)

    # ---- 对外能力：前端是命令行还是网页，这里不关心 ----------------------

    def new_session(self) -> Session:
        session = Session(id=_session_id())
        self.store.put(session)
        return session

    def list_sessions(self) -> list[str]:
        return self.store.list_ids()

    def send_message(self, session_id: str, text: str) -> str:
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id}")

        session.append(UserMessage(text))
        self.store.put(session)
        self.on_event("user", {"text": text})

        for step in range(1, self.max_iterations + 1):
            context = self.assistant.build(session)
            message = self.client.generate(context, self.assistant_setting)
            session.append(message)
            self.on_event("assistant", {"text": message.text, "tool_calls": [c.name for c in message.tool_calls]})

            if not message.tool_calls:
                self.store.put(session)
                return message.text

            for call in message.tool_calls:
                self._handle_call(session, call)
            self.store.put(session)

        self.store.put(session)
        return f"Stopped after {self.max_iterations} iterations without a final answer."

    # ---- 三道闸里的第三道：模型审核 --------------------------------------

    def _review(self, session: Session, call: ToolCall) -> Decision:
        context = self.reviewer.build(session, tool_call=call)
        decision = self.client.generate(context, self.reviewer_setting)
        if not isinstance(decision, Decision):
            return Decision(approved=False, reason="reviewer returned unexpected type")
        self.on_event("review", {"tool": call.name, "approved": decision.approved, "reason": decision.reason})
        return decision

    def _handle_call(self, session: Session, call: ToolCall) -> None:
        decision = self._review(session, call)
        if not decision.approved:
            session.append(ToolResult(tool_call_id=call.id, content=f"rejected: {decision.reason}"))
            return

        # 闸一、二、四、五都在 toolbox.run 里面。
        result = self.toolbox.run(call.name, call.arguments)
        self.on_event("tool", {"name": call.name, "result": result})
        session.append(ToolResult(tool_call_id=call.id, content=result))


def _session_id() -> str:
    from datetime import datetime
    import uuid

    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]

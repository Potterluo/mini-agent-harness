"""应用层：两个角色，各自把 Session 组装成 Context。

回复用户和审核工具调用需要的上下文不一样，所以拆成两个角色。
output_type 是这个设计的关键：同一套 Context 机制服务两种输出。
"""

from __future__ import annotations

import json

from .domain import AssistantMessage, Context, Decision, Session, ToolCall, ToolResult, Tool
from .ports import Role


class AssistantRole:
    name = "assistant"

    def __init__(self, instruction: str, tools: list[Tool]):
        self.instruction = instruction
        self.tools = tools

    def build(self, session: Session, *, tool_call: ToolCall | None = None) -> Context[AssistantMessage]:
        return Context(
            instruction=self.instruction,
            tools=self.tools,
            history=list(session.messages),
            output_type=AssistantMessage,
        )


class ReviewerRole:
    """审核角色：拿到的是一份独立组织的上下文，不是整段对话。

    把完整历史丢给它，它会顺着助手的样子继续调工具（finish_reason=tool_calls），
    而不是回一个判断。所以这里只给它三样东西：任务、最近的工具结果、待审调用。
    """

    name = "reviewer"

    def __init__(self, instruction: str):
        self.instruction = instruction

    def build(self, session: Session, *, tool_call: ToolCall | None = None) -> Context[Decision]:
        task = session.last_user_text() or "(unknown)"
        recent: list[str] = []
        for message in session.messages[-6:]:
            if isinstance(message, ToolResult):
                recent.append(f"- 工具结果：{message.content[:200]}")
            elif isinstance(message, AssistantMessage) and message.text:
                recent.append(f"- 助手说过：{message.text[:200]}")

        detail = [f"\n\n当前任务：{task}"]
        if recent:
            detail.append("最近发生的事：")
            detail.extend(recent)
        if tool_call is not None:
            detail.append(
                f"待审调用：{tool_call.name}，参数 {json.dumps(tool_call.arguments, ensure_ascii=False)}"
            )
        detail.append('只输出一个 JSON，不要解释：{"approved": true 或 false, "reason": "一句话"}')

        return Context(
            instruction=self.instruction + "\n".join(detail),
            tools=[],
            history=[],
            output_type=Decision,
        )


__all__ = ["AssistantRole", "ReviewerRole", "Role"]

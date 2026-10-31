"""应用层：两个角色，各自把 Session 组装成 Context。

回复用户和审核工具调用需要的上下文不一样，所以拆成两个角色。
output_type 是这个设计的关键：同一套 Context 机制服务两种输出。
"""

from __future__ import annotations

import json

from .domain import AssistantMessage, Context, Decision, Session, ToolCall, Tool
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
    """审核角色没有工具：它只判断这次调用该不该执行。"""

    name = "reviewer"

    def __init__(self, instruction: str):
        self.instruction = instruction

    def build(self, session: Session, *, tool_call: ToolCall | None = None) -> Context[Decision]:
        detail = ""
        if tool_call is not None:
            detail = (
                f"\n\n待审调用：\n工具：{tool_call.name}\n"
                f"参数：{json.dumps(tool_call.arguments, ensure_ascii=False)}\n\n"
                '只回一个 JSON：{"approved": true/false, "reason": "一句话原因"}'
            )
        return Context(
            instruction=self.instruction + detail,
            tools=[],
            history=list(session.messages),
            output_type=Decision,
        )


__all__ = ["AssistantRole", "ReviewerRole", "Role"]

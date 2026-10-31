"""领域层：系统自己的语言。

这一层不认识任何外部协议，也不引用 SDK。模型换一家、存储换一种，
这里的定义都不用改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, Protocol, TypeVar

OpType = TypeVar("OpType")


@dataclass(frozen=True)
class ToolCall:
    """模型点名要调的工具。id 用来把结果对回去。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    content: str


Message = UserMessage | AssistantMessage | ToolResult


@dataclass(frozen=True)
class Decision:
    """审核结论。拒绝时要给出原因，原因会作为 ToolResult 交还给模型。"""

    approved: bool
    reason: str = ""


class Tool(Protocol):
    """工具的四件事：模型靠 name 点名，靠 description 判断用途，
    parameters 决定传什么，invoke 才是真正执行。risky 决定要不要过人工闸。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risky: bool

    def invoke(self, arguments: dict[str, Any]) -> str: ...


@dataclass
class Context(Generic[OpType]):
    """发给模型的那一包东西。output_type 让同一套机制服务不同角色。"""

    instruction: str
    tools: list[Tool]
    history: list[Message]
    output_type: type[OpType]


@dataclass
class Session:
    """对话本身。表现形态是它，所以它是第一个要定义的领域概念。"""

    id: str
    messages: list[Message] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)

    def append(self, message: Message) -> None:
        self.messages.append(message)

    def last_user_text(self) -> str:
        for message in reversed(self.messages):
            if isinstance(message, UserMessage):
                return message.text
        return ""

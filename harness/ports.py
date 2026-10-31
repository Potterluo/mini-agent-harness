"""应用层的接口。

按分层那篇的说法，接口放在应用层，说明「应用层需要什么」；
真正的实现放在适配层。这样编排代码只依赖协议，测试时能换内存实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .domain import Context, Session, ToolCall


@dataclass(frozen=True)
class ModelSetting:
    """用哪个模型、想多深。两者总成对出现，所以包在一起。"""

    model: str
    effort: str = "medium"


class SessionStore(Protocol):
    def list_ids(self) -> list[str]: ...

    def get(self, session_id: str) -> Session | None: ...

    def put(self, session: Session) -> None: ...


class ModelClient(Protocol):
    def generate(self, context: Context, setting: ModelSetting) -> Any: ...


class Approver(Protocol):
    """危险操作的批准入口。返回 True 表示放行。"""

    def __call__(self, name: str, arguments: dict[str, Any]) -> bool: ...


class Role(Protocol):
    """一个角色：把 Session 组装成 Context。回复用户和审核工具调用是两种角色。"""

    name: str

    def build(self, session: Session, *, tool_call: ToolCall | None = None) -> Context: ...

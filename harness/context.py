"""上下文工程：把过长的工具输出压短，让历史留在预算内。

裁剪只动工具结果，用户说的话和模型说过的话都不碰：前者是需求，后者是承诺，
丢了任何一个，后面的轮次都会跑偏。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .domain import AssistantMessage, Message, ToolResult, UserMessage

MAX_TOOL_OUTPUT = 4000


def clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """单块文本封顶，超出时截断并追加 "\\n...[truncated N chars]"。

    N 是被切掉的字数，不是原文总长。正好等于上限时原样返回。
    """
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n...[truncated {dropped} chars]"


def _role(message: Any) -> str | None:
    """判断一条消息的角色。dataclass 看类型，dict 看 role 字段。"""
    if isinstance(message, ToolResult):
        return "tool"
    if isinstance(message, UserMessage):
        return "user"
    if isinstance(message, AssistantMessage):
        return "assistant"
    if isinstance(message, dict):
        role = message.get("role")
        return role if isinstance(role, str) else None
    return None


def _shrink(message: Any, limit: int) -> Any:
    """返回压到 limit 之后的副本。认不出来的形态原样返回。"""
    if isinstance(message, ToolResult):
        return dataclasses.replace(message, content=clip(message.content, limit))
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return {**message, "content": clip(message["content"], limit)}
    return message


def compact(messages: list[Message], keep_recent: int = 6, old_limit: int = 400) -> list:
    """只裁剪老的工具结果。

    最近 keep_recent 条 role == "tool" 的消息保持原样，更早的工具结果压到
    old_limit；user / assistant 一律不动。返回新列表，入参不受影响。
    """
    result = list(messages)
    tool_positions = [index for index, message in enumerate(result) if _role(message) == "tool"]

    keep = max(keep_recent, 0)
    stale = tool_positions[:-keep] if keep else tool_positions
    for index in stale:
        result[index] = _shrink(result[index], old_limit)
    return result

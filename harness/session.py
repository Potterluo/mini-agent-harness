"""会话本身，以及它的落盘方式。

领域层只认 Session 这个对象，存成什么格式由这里决定。换存储只要再写一个
实现同样四个方法的类，编排代码一行都不用改。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain import AssistantMessage, Message, Session, ToolCall, ToolResult, UserMessage

# 显式 type 标签：JSON 里靠它认出该还原成哪个 dataclass。
TAG_USER = "user"
TAG_ASSISTANT = "assistant"
TAG_TOOL = "tool"

DEFAULT_MEMORY = {"task": "", "files": [], "notes": []}


def new_session(workspace_root: str | os.PathLike[str]) -> Session:
    """开一个会话。id 形如 20261031-153012-ab12cd，后六位是随机串，撞不上。

    workspace_root 由调用方掌握，会话本身不带路径：同一个会话可以换机器接着聊。
    """
    del workspace_root
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return Session(
        id=f"{stamp}-{suffix}",
        messages=[],
        memory={key: (value.copy() if isinstance(value, list) else value)
                for key, value in DEFAULT_MEMORY.items()},
    )


def remember(bucket: list[Any], item: Any, limit: int) -> None:
    """往一个有上限的最近列表里记一笔，原地改。

    已经记过就挪到末尾（最近的留在后面），超出上限从头部丢最旧的。
    limit <= 0 视为没有容量，什么也不记。
    """
    if limit <= 0:
        return
    while item in bucket:
        bucket.remove(item)
    bucket.append(item)
    overflow = len(bucket) - limit
    if overflow > 0:
        del bucket[:overflow]


def _encode_message(message: Message) -> dict[str, Any]:
    """dataclass -> JSON 可存的 dict。类型判断只走 isinstance。"""
    if isinstance(message, UserMessage):
        return {"type": TAG_USER, "text": message.text}
    if isinstance(message, AssistantMessage):
        return {
            "type": TAG_ASSISTANT,
            "text": message.text,
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                for call in message.tool_calls
            ],
        }
    if isinstance(message, ToolResult):
        return {
            "type": TAG_TOOL,
            "tool_call_id": message.tool_call_id,
            "content": message.content,
        }
    raise TypeError(f"存不下的消息类型：{type(message).__name__}")


def _decode_message(raw: dict[str, Any]) -> Message:
    tag = raw.get("type")
    if tag == TAG_USER:
        return UserMessage(text=raw.get("text", ""))
    if tag == TAG_ASSISTANT:
        calls = tuple(
            ToolCall(
                id=call.get("id", ""),
                name=call.get("name", ""),
                arguments=dict(call.get("arguments") or {}),
            )
            for call in raw.get("tool_calls") or ()
        )
        return AssistantMessage(text=raw.get("text", ""), tool_calls=calls)
    if tag == TAG_TOOL:
        return ToolResult(
            tool_call_id=raw.get("tool_call_id", ""),
            content=raw.get("content", ""),
        )
    raise ValueError(f"认不出的消息标签：{tag!r}")


def _encode_session(session: Session) -> dict[str, Any]:
    return {
        "id": session.id,
        "messages": [_encode_message(message) for message in session.messages],
        "memory": session.memory,
    }


def _decode_session(payload: dict[str, Any]) -> Session:
    return Session(
        id=str(payload.get("id", "")),
        messages=[_decode_message(raw) for raw in payload.get("messages") or ()],
        memory=dict(payload.get("memory") or {}),
    )


class JsonlSessionStore:
    """每个会话一个 JSON 文件，目录不存在时自动建。

    文件名就是 session id，所以 list_ids 只要读目录。类名沿用 Jsonl 的叫法，
    落盘其实是整份 JSON：一次整改一个会话，没必要留追加日志。
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)

    def _path(self, session_id: str) -> Path:
        name = str(session_id)
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError(f"不合法的会话 id：{session_id!r}")
        return self.root / f"{name}.json"

    def list_ids(self) -> list[str]:
        """按修改时间倒序，最近的在前。mtime 一样时按文件名兜底，顺序稳定。"""
        try:
            files = [path for path in self.root.iterdir()
                     if path.is_file() and path.suffix == ".json"]
        except FileNotFoundError:
            return []
        files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        return [path.stem for path in files]

    def get(self, session_id: str) -> Session | None:
        try:
            path = self._path(session_id)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return _decode_session(json.loads(path.read_text(encoding="utf-8")))

    def put(self, session: Session) -> None:
        """先写临时文件再替换，读的一方不会读到写了一半的 JSON。"""
        path = self._path(session.id)
        self.root.mkdir(parents=True, exist_ok=True)
        data = json.dumps(_encode_session(session), ensure_ascii=False, indent=2)
        staging = path.with_name(f"{path.name}.tmp")
        staging.write_text(data, encoding="utf-8")
        os.replace(staging, path)

    def latest(self) -> str | None:
        ids = self.list_ids()
        return ids[0] if ids else None

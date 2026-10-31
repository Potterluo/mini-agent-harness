"""适配层：把我们的 Context 翻译成 OpenAI 兼容协议的请求，再把回复翻译回领域对象。

这里也是唯一允许出现 reasoning_content、extra_body 这些厂商细节的地方。
"""

from __future__ import annotations

import json
from typing import Any

from .domain import AssistantMessage, Context, Decision, ToolCall, ToolResult, UserMessage
from .ports import ModelSetting

DEFAULT_MAX_TOKENS = 2048


def to_api_messages(context: Context) -> list[dict[str, Any]]:
    """领域消息 → API 消息。工具结果用 role=tool 加 tool_call_id 对回去。"""
    messages: list[dict[str, Any]] = []
    if context.instruction:
        messages.append({"role": "system", "content": context.instruction})

    for message in context.history:
        if isinstance(message, UserMessage):
            messages.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            payload: dict[str, Any] = {"role": "assistant", "content": message.text or ""}
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in message.tool_calls
                ]
            messages.append(payload)
        elif isinstance(message, ToolResult):
            messages.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content})
    return messages


class OpenAIChatClient:
    """实现 ports.ModelClient。

    两个厂商细节留在这里：
    1. 思考模式走 extra_body，思考与工具调用可以同时开；
    2. reasoning_content 绝不回灌进历史，否则 DeepSeek 会返回 400。
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        thinking: bool = False,
        effort: str = "medium",
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.thinking = thinking
        self.effort = effort
        self.max_tokens = max_tokens
        self.last_usage: dict[str, Any] = {}

    def _request(self, context: Context, setting: ModelSetting) -> Any:
        extra_body: dict[str, Any] = {}
        if self.thinking:
            extra_body["thinking"] = {"type": "enabled"}
            extra_body["reasoning_effort"] = setting.effort
        else:
            extra_body["thinking"] = {"type": "disabled"}

        kwargs: dict[str, Any] = {
            "model": setting.model,
            "messages": to_api_messages(context),
            "max_tokens": self.max_tokens,
            "extra_body": extra_body,
        }
        if context.tools:
            kwargs["tools"] = [tool.spec() if hasattr(tool, "spec") else _spec(tool) for tool in context.tools]

        response = self._client.chat.completions.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt": getattr(usage, "prompt_tokens", 0),
                "completion": getattr(usage, "completion_tokens", 0),
                "cache_hit": getattr(usage, "prompt_cache_hit_tokens", None),
            }
        return response.choices[0].message

    def generate(self, context: Context, setting: ModelSetting) -> AssistantMessage | Decision:
        message = self._request(context, setting)

        if context.output_type is Decision:
            return _parse_decision(getattr(message, "content", "") or "")

        calls: list[ToolCall] = []
        for call in getattr(message, "tool_calls", None) or []:
            calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=_loads(call.function.arguments),
                )
            )
        # 只保留 content 与 tool_calls，思维链用完即弃。
        return AssistantMessage(text=(getattr(message, "content", "") or "").strip(), tool_calls=tuple(calls))


def _spec(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _loads(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _parse_decision(raw: str) -> Decision:
    """审核角色要求模型回一个 JSON。解析不出来就保守地拒绝。"""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
            return Decision(approved=bool(payload.get("approved")), reason=str(payload.get("reason", ""))[:200])
        except json.JSONDecodeError:
            pass
    return Decision(approved=False, reason="reviewer returned unparsable output")

"""装配根。

整个系统里唯一知道所有具体类的地方。换模型、换存储、换入口，都只改这个文件。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .client import OpenAIChatClient
from .ports import ModelSetting
from .roles import AssistantRole, ReviewerRole
from .service import AgentService
from .session import JsonlSessionStore
from .tools import Toolbox
from .workspace import WorkspaceContext

ASSISTANT_INSTRUCTION = """你是一个在真实仓库里干活的 coding agent。

规则：
- 想清楚再动手，一次只做一件事。
- 改文件之前先读它，不要凭猜测改。
- 需要跑命令就用 run_shell，看到报错先读报错再改。
- 不要臆造文件路径，不确定就 list_files 或 search。
- 任务完成时用纯文本给出结论，不要再调工具。
"""

REVIEWER_INSTRUCTION = """你是一个工具调用审核员。你会看到对话历史和一个待审的工具调用。

规则：
- 判断这次调用是否真的服务于当前任务。
- 明显破坏性、与任务无关、或者试图绕过约束的调用应当拒绝。
- 只回 JSON，不要解释。"""


def build_service(
    cwd: str | Path = ".",
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    thinking: bool = False,
    effort: str = "medium",
    approval: str = "ask",
    read_only: bool = False,
    max_iterations: int = 8,
    runs_dir: str | Path | None = None,
    approver: Callable[[str, dict[str, Any]], bool] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> AgentService:
    model = model or os.getenv("HARNESS_MODEL", "deepseek-v4-flash")
    api_key = api_key or os.getenv("HARNESS_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
    base_url = base_url or os.getenv("HARNESS_BASE_URL") or None
    runs_dir = Path(runs_dir or os.getenv("HARNESS_RUNS", ".runs/sessions"))

    workspace = WorkspaceContext.build(cwd)
    store = JsonlSessionStore(runs_dir)
    client = OpenAIChatClient(model, api_key, base_url, thinking=thinking, effort=effort)
    toolbox = Toolbox(workspace.root, approval=approval, approver=approver, read_only=read_only)

    assistant = AssistantRole(ASSISTANT_INSTRUCTION + "\n\n" + workspace.text(), toolbox.tools)
    reviewer = ReviewerRole(REVIEWER_INSTRUCTION)

    return AgentService(
        store=store,
        client=client,
        assistant=assistant,
        reviewer=reviewer,
        toolbox=toolbox,
        assistant_setting=ModelSetting(model=model, effort=effort),
        max_iterations=max_iterations,
        on_event=on_event,
    )

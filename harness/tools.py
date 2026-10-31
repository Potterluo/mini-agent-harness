"""适配层：六个内置工具和它们的闸门。

模型不会把 shell 命令随手敲进虚空，它只能从这六个带类型参数的命名工具里挑，
而且每一次调用都要顺序过五道检查。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .context import clip


class PathEscape(ValueError):
    """模型给的路径试图跑出仓库根。"""


class Toolbox:
    def __init__(
        self,
        root: str | Path,
        *,
        approval: str = "ask",
        approver: Callable[[str, dict[str, Any]], bool] | None = None,
        read_only: bool = False,
    ):
        self.root = Path(root).resolve()
        self.approval = approval
        self.read_only = read_only
        self.approver = approver or self._terminal_approver
        self.tools: list[Any] = [
            ListFiles(self),
            ReadFile(self),
            Search(self),
            RunShell(self),
            WriteFile(self),
            PatchFile(self),
        ]
        self._by_name = {tool.name: tool for tool in self.tools}

    # ---- 对外 -----------------------------------------------------------

    def specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools
        ]

    def register(self, tool: Any) -> None:
        self.tools.append(tool)
        self._by_name[tool.name] = tool

    # ---- 五道闸 ---------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._by_name.get(name)
        if tool is None:  # 闸 1：是已知工具吗
            return f"error: unknown tool '{name}'"

        arguments = arguments or {}
        try:  # 闸 2：参数合法吗
            self._validate(tool, arguments)
        except ValueError as exc:
            return f"error: invalid arguments for {name}: {exc}"

        try:  # 闸 3：路径有没有跑出仓库根（工具内部会调 resolve）
            if tool.risky and not self._approve(name, arguments):  # 闸 4：危险操作批准了吗
                return f"error: approval denied for {name}"
            return clip(tool.invoke(arguments))  # 闸 5：执行并裁剪
        except PathEscape as exc:
            return f"error: path escapes workspace: {exc}"
        except Exception as exc:  # noqa: BLE001 - 任何失败都要变成模型能读的反馈
            return f"error: tool {name} failed: {exc}"

    def resolve(self, raw: str | None) -> Path:
        path = Path(raw or ".")
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise PathEscape(str(raw))
        return resolved

    def _validate(self, tool: Any, arguments: dict[str, Any]) -> None:
        schema = tool.parameters
        for field in schema.get("required", []):
            if field not in arguments:
                raise ValueError(f"missing required field '{field}'")
        for field, spec in schema.get("properties", {}).items():
            if field not in arguments:
                continue
            expected = spec.get("type")
            value = arguments[field]
            if expected == "string" and not isinstance(value, str):
                raise ValueError(f"field '{field}' must be a string")
            if expected == "integer" and not isinstance(value, int):
                raise ValueError(f"field '{field}' must be an integer")

    def _approve(self, name: str, arguments: dict[str, Any]) -> bool:
        if self.read_only:
            return False
        if self.approval == "auto":
            return True
        if self.approval == "never":
            return False
        return bool(self.approver(name, arguments))

    @staticmethod
    def _terminal_approver(name: str, arguments: dict[str, Any]) -> bool:
        detail = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        return input(f"[approve] {name}({detail})? [y/N] ").strip().lower() == "y"


# ---- 六个工具 ----------------------------------------------------------


class _Base:
    name = ""
    description = ""
    risky = False
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def __init__(self, box: Toolbox):
        self.box = box

    def invoke(self, arguments: dict[str, Any]) -> str:
        raise NotImplementedError


class ListFiles(_Base):
    name = "list_files"
    description = "列出目录内容。path 省略时为仓库根。"
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "相对路径"}},
        "required": [],
    }

    def invoke(self, arguments):
        target = self.box.resolve(arguments.get("path"))
        if not target.exists():
            raise FileNotFoundError(f"no such path: {arguments.get('path')}")
        entries = sorted(target.iterdir(), key=lambda item: (item.is_file(), item.name))
        lines = [("dir  " if item.is_dir() else "     ") + item.name for item in entries[:200]]
        return "\n".join(lines) or "(empty)"


class ReadFile(_Base):
    name = "read_file"
    description = "按行范围读取文件。start / end 是 1 起始的行号，省略则读全文（最多 400 行）。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
        },
        "required": ["path"],
    }

    def invoke(self, arguments):
        target = self.box.resolve(arguments["path"])
        if not target.is_file():
            raise FileNotFoundError(f"not a file: {arguments['path']}")
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(1, int(arguments.get("start", 1)))
        end = min(len(lines), int(arguments.get("end", start + 399)))
        chunk = [f"{number:>5}\t{lines[number - 1]}" for number in range(start, min(end, len(lines)) + 1)]
        return "\n".join(chunk) or "(empty file)"


class Search(_Base):
    name = "search"
    description = "在仓库内搜索文本。有 ripgrep 时用 rg，否则退化为纯文本扫描。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "glob": {"type": "string", "description": "可选的文件名模式，如 *.py"},
        },
        "required": ["query"],
    }

    def invoke(self, arguments):
        query = arguments["query"]
        pattern = arguments.get("glob") or "*"
        if shutil.which("rg"):
            command = ["rg", "--line-number", "--no-heading", "-g", pattern, query, str(self.box.root)]
            done = subprocess.run(command, capture_output=True, text=True, timeout=30)
            return done.stdout.strip() or "(no match)"
        hits: list[str] = []
        for path in self.box.root.rglob(pattern):
            if not path.is_file() or ".git" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if query in line:
                    hits.append(f"{path.relative_to(self.box.root)}:{number}: {line.strip()[:160]}")
                if len(hits) >= 100:
                    break
        return "\n".join(hits) or "(no match)"


class RunShell(_Base):
    name = "run_shell"
    description = "在仓库根目录执行一条 shell 命令。"
    risky = True
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def invoke(self, arguments):
        done = subprocess.run(
            arguments["command"],
            shell=True,
            cwd=self.box.root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (done.stdout or "") + (done.stderr or "")
        return f"exit={done.returncode}\n{output.strip() or '(no output)'}"


class WriteFile(_Base):
    name = "write_file"
    description = "创建或覆写一个文件。"
    risky = True
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"],
    }

    def invoke(self, arguments):
        target = self.box.resolve(arguments["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        content = arguments["content"]
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {target.relative_to(self.box.root)}"


class PatchFile(_Base):
    name = "patch_file"
    description = "精确替换文件里的一处文本。old_text 必须恰好出现一次。"
    risky = True
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def invoke(self, arguments):
        target = self.box.resolve(arguments["path"])
        if not target.is_file():
            raise FileNotFoundError(f"not a file: {arguments['path']}")
        body = target.read_text(encoding="utf-8")
        old = arguments["old_text"]
        count = body.count(old)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        target.write_text(body.replace(old, arguments["new_text"], 1), encoding="utf-8")
        return f"patched {target.relative_to(self.box.root)}"

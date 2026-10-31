"""适配层：六个内置工具，以及把它们管起来的 Toolbox。

每个工具都满足 domain.Tool 协议：name / description / parameters / risky / invoke。
模型不会把 shell 命令随手敲进虚空，它只能从这六个带类型参数的命名工具里挑；
Toolbox.run() 里每一次调用都按固定顺序过五道闸：

1. 工具存在吗
2. 参数合法吗
3. 路径落在仓库内吗
4. 危险操作获批了吗
5. 执行并把输出裁剪到上限

任何失败都返回以 ``error:`` 开头的字符串，而不是抛异常。错误信息本身是喂给
模型的反馈，抛出去只会把处理责任推给编排层。
"""

from __future__ import annotations

import copy
import fnmatch
import locale
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .context import MAX_TOOL_OUTPUT, clip
from .domain import Tool
from .ports import Approver

MAX_OUTPUT_CHARS = MAX_TOOL_OUTPUT
SEARCH_LIMIT = 200
SHELL_TIMEOUT_SECONDS = 120


class PathEscape(ValueError):
    """模型给的路径试图跑出仓库根。raw 保留它原样写的那个字符串。"""

    def __init__(self, raw: str | None) -> None:
        super().__init__(raw)
        self.raw = raw


class ToolError(Exception):
    """工具执行期可预期的失败。消息会原样进 ``error: tool <name> failed: ...``。"""


# ---------------------------------------------------------------------------
# 小工具函数
# ---------------------------------------------------------------------------


def _within(root: Path, candidate: Path) -> bool:
    """candidate 是否等于 root 或落在 root 之内。

    用 commonpath 而不是 relative_to：Windows 上要大小写不敏感，跨盘符时
    commonpath 抛 ValueError，正好当作越界。
    """

    root_key = os.path.normcase(str(root))
    try:
        return os.path.commonpath([root_key, os.path.normcase(str(candidate))]) == root_key
    except ValueError:
        return False


def _resolve_in_root(root: Path, raw: str | None) -> Path:
    """把相对/绝对路径解析成真实路径，越界抛 PathEscape。"""

    path = Path(raw or ".")
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    if not _within(root, resolved):
        raise PathEscape(raw)
    return resolved


def _decode(data: bytes) -> str:
    """子进程输出解码：先 UTF-8，再系统编码，最后兜底替换。"""

    if not data:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False), "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def _validate_arguments(tool: Tool, arguments: Any) -> str:
    """按 tool.parameters 这段 JSON Schema 检查参数。返回空串表示通过。"""

    if not isinstance(arguments, dict):
        return f"expected an object of arguments, got {_type_name(arguments)}"

    schema = getattr(tool, "parameters", None) or {}
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [key for key in required if key not in arguments]
    if missing:
        return "missing required argument(s): " + ", ".join(missing)

    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"unknown argument {key!r}"
            continue
        expected = spec.get("type")
        check = _TYPE_CHECKS.get(expected) if isinstance(expected, str) else None
        if check is not None and not check(value):
            return f"argument {key!r} must be of type {expected}, got {_type_name(value)}"
        minimum = spec.get("minimum")
        if (
            isinstance(minimum, int)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value < minimum
        ):
            return f"argument {key!r} must be >= {minimum}"

    return ""


def default_approver(name: str, arguments: dict[str, Any]) -> bool:
    """默认批准方式：在终端问一句 y/N。"""

    detail = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
    try:
        answer = input(f"[approve] {name}({detail})? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _format_hits(hits: list[str], query: str) -> str:
    if not hits:
        return f"no matches for {query!r}"
    text = "\n".join(hits)
    if len(hits) >= SEARCH_LIMIT:
        text += f"\n(results capped at {SEARCH_LIMIT})"
    return text


# ---------------------------------------------------------------------------
# 六个工具的共同部分
# ---------------------------------------------------------------------------


class _RootedTool:
    """所有内置工具共享仓库根目录与路径解析。"""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    risky: bool = False

    def __init__(self, root: Path) -> None:
        self.root = root

    def invoke(self, arguments: dict[str, Any]) -> str:
        raise NotImplementedError

    def _resolve(self, raw: str) -> Path:
        try:
            return _resolve_in_root(self.root, raw)
        except PathEscape as exc:
            raise ToolError(f"path escapes workspace: {exc.raw}") from exc
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(f"unusable path {raw!r}: {exc}") from exc

    def _rel(self, path: Path) -> str:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
        return rel or "."


class ListFiles(_RootedTool):
    name = "list_files"
    description = "列出目录下的文件和子目录，目录名以 / 结尾。path 省略时列仓库根。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对仓库根的目录路径，默认 .",
                "default": ".",
            }
        },
        "required": [],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any]) -> str:
        raw = arguments.get("path", ".")
        target = self._resolve(raw)
        if not target.exists():
            raise ToolError(f"no such directory: {raw}")
        if not target.is_dir():
            raise ToolError(f"not a directory (use read_file): {raw}")

        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return "(empty directory)"
        return "\n".join(entry.name + ("/" if entry.is_dir() else "") for entry in entries)


class ReadFile(_RootedTool):
    name = "read_file"
    description = "按行范围读取文本文件，行号从 1 开始且含两端。省略 start/end 时读全文。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对仓库根的文件路径"},
            "start": {
                "type": "integer",
                "description": "起始行号（含），默认 1",
                "minimum": 1,
            },
            "end": {
                "type": "integer",
                "description": "结束行号（含），默认文件末尾",
                "minimum": 1,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any]) -> str:
        raw = arguments["path"]
        target = self._resolve(raw)
        if not target.exists():
            raise ToolError(f"no such file: {raw}")
        if target.is_dir():
            raise ToolError(f"is a directory (use list_files): {raw}")
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"cannot read {raw}: {exc}") from exc

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        if total == 0:
            return "(empty file)"

        start = arguments.get("start", 1)
        end = arguments.get("end", total)
        if start < 1:
            raise ToolError(f"start must be >= 1, got {start}")
        if start > total:
            return f"(no lines selected: file has {total} line(s))"
        if end < start:
            raise ToolError(f"end ({end}) is before start ({start})")
        return "\n".join(lines[start - 1 : end])


class Search(_RootedTool):
    name = "search"
    description = (
        "在仓库内按字面文本搜索，返回 path:line: text。"
        "有 ripgrep 时用 rg，没有就退化为逐文件扫描。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要搜索的字面文本，不是正则"},
            "glob": {"type": "string", "description": "可选的文件名过滤，如 *.py"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any]) -> str:
        query = arguments["query"]
        glob = arguments.get("glob") or None
        if not query:
            raise ToolError("query must not be empty")

        rg = shutil.which("rg")
        if rg:
            hits = self._search_with_rg(rg, query, glob)
            if hits is not None:
                return _format_hits(hits, query)
        return _format_hits(self._scan(query, glob), query)

    def _search_with_rg(self, rg: str, query: str, glob: str | None) -> list[str] | None:
        command = [
            rg,
            "--line-number",
            "--no-heading",
            "--color",
            "never",
            "--fixed-strings",
            "--smart-case",
        ]
        if os.name == "nt":
            # rg 在 Windows 上默认输出反斜杠，统一成 / 以便和退化实现一致。
            command += ["--path-separator", "/"]
        command += ["-e", query]
        if glob:
            command += ["--glob", glob]
        command.append(".")
        try:
            proc = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                timeout=SHELL_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode not in (0, 1):
            return None

        hits: list[str] = []
        for line in _decode(proc.stdout).splitlines():
            if line.startswith("./"):
                line = line[2:]
            hits.append(line)
            if len(hits) >= SEARCH_LIMIT:
                break
        return hits

    def _scan(self, query: str, glob: str | None) -> list[str]:
        hits: list[str] = []
        try:
            candidates = sorted(self.root.rglob("*"))
        except OSError:
            return hits

        for path in candidates:
            if len(hits) >= SEARCH_LIMIT:
                break
            try:
                if not path.is_file():
                    continue
                if not _within(self.root, path.resolve()):
                    continue
                rel = path.relative_to(self.root)
            except (OSError, ValueError):
                continue
            if any(part.startswith(".") for part in rel.parts) or "__pycache__" in rel.parts:
                continue
            if glob and not (
                fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(rel.as_posix(), glob)
            ):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    for number, line in enumerate(handle, 1):
                        if query in line:
                            hits.append(f"{rel.as_posix()}:{number}:{line.rstrip()}")
                            if len(hits) >= SEARCH_LIMIT:
                                break
            except OSError:
                continue
        return hits


class RunShell(_RootedTool):
    name = "run_shell"
    description = "在仓库根目录执行一条 shell 命令，返回合并后的输出和非零退出码。"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "交给系统 shell 执行的命令"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    risky = True

    def invoke(self, arguments: dict[str, Any]) -> str:
        command = arguments["command"]
        if not command.strip():
            raise ToolError("command must not be empty")
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.root,
                capture_output=True,
                timeout=SHELL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {SHELL_TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise ToolError(f"cannot run command: {exc}") from exc

        parts: list[str] = []
        stdout = _decode(proc.stdout).rstrip()
        stderr = _decode(proc.stderr).rstrip()
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append("[stderr]\n" + stderr)
        if proc.returncode != 0:
            parts.append(f"[exit code {proc.returncode}]")
        return "\n".join(parts) if parts else "(no output, exit code 0)"


class WriteFile(_RootedTool):
    name = "write_file"
    description = "创建或覆写文件，父目录不存在时会一并创建。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对仓库根的文件路径"},
            "content": {"type": "string", "description": "写入的完整文件内容"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    risky = True

    def invoke(self, arguments: dict[str, Any]) -> str:
        raw = arguments["path"]
        content = arguments["content"]
        target = self._resolve(raw)
        if target.is_dir():
            raise ToolError(f"is a directory: {raw}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except OSError as exc:
            raise ToolError(f"cannot write {raw}: {exc}") from exc
        return f"wrote {len(content)} chars to {self._rel(target)}"


class PatchFile(_RootedTool):
    name = "patch_file"
    description = (
        "把文件里恰好出现一次的 old_text 替换成 new_text。"
        "出现 0 次或多次都算失败；new_text 传空字符串表示删除。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对仓库根的文件路径"},
            "old_text": {
                "type": "string",
                "description": "要被替换的原文，必须与文件内容一致且只出现一次",
            },
            "new_text": {"type": "string", "description": "替换后的文本"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }
    risky = True

    def invoke(self, arguments: dict[str, Any]) -> str:
        raw = arguments["path"]
        old_text = arguments["old_text"]
        new_text = arguments["new_text"]
        if not old_text:
            raise ToolError("old_text must not be empty")

        target = self._resolve(raw)
        if not target.exists():
            raise ToolError(f"no such file: {raw}")
        if target.is_dir():
            raise ToolError(f"is a directory: {raw}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"not a UTF-8 text file: {raw}") from exc
        except OSError as exc:
            raise ToolError(f"cannot read {raw}: {exc}") from exc

        count = text.count(old_text)
        if count == 0:
            raise ToolError(f"old_text not found in {self._rel(target)}")
        if count > 1:
            raise ToolError(
                f"old_text appears {count} times in {self._rel(target)}; "
                "include more surrounding context to make it unique"
            )

        patched = text.replace(old_text, new_text, 1)
        try:
            with open(target, "w", encoding="utf-8", newline="") as handle:
                handle.write(patched)
        except OSError as exc:
            raise ToolError(f"cannot write {raw}: {exc}") from exc
        return f"patched {self._rel(target)} (1 replacement)"


# ---------------------------------------------------------------------------
# 注册表 + 五道闸
# ---------------------------------------------------------------------------


class Toolbox:
    """工具注册表，以及每次调用都要过的那五道闸。

    approval="ask" 时每个危险操作都问 approver；"auto" 直接放行；"never" 一律拒绝。
    read_only=True 时所有 risky 工具都拒绝，跟 approval/approver 无关。
    """

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        approval: str = "ask",
        approver: Approver | None = None,
        read_only: bool = False,
    ) -> None:
        if approval not in {"ask", "auto", "never"}:
            raise ValueError(f"approval must be 'ask', 'auto' or 'never', got {approval!r}")
        self.root = Path(root).resolve()
        self.approval = approval
        self.read_only = bool(read_only)
        self._approver: Approver = approver or default_approver
        self.tools: list[Tool] = []
        self._by_name: dict[str, Tool] = {}
        for tool in (
            ListFiles(self.root),
            ReadFile(self.root),
            Search(self.root),
            RunShell(self.root),
            WriteFile(self.root),
            PatchFile(self.root),
        ):
            self.register(tool)

    # ---- 注册与描述 ------------------------------------------------------

    def register(self, tool: Tool) -> None:
        """登记一个工具。名字重复算编程错误，直接抛 ValueError。"""

        name = getattr(tool, "name", "")
        if not isinstance(name, str) or not name:
            raise ValueError("tool must have a non-empty string name")
        if not callable(getattr(tool, "invoke", None)):
            raise ValueError(f"tool {name!r} must define invoke()")
        if name in self._by_name:
            raise ValueError(f"tool {name!r} is already registered")
        self.tools.append(tool)
        self._by_name[name] = tool

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI function calling 的 tools 参数。每次返回独立副本。"""

        specs: list[dict[str, Any]] = []
        for tool in self.tools:
            parameters = getattr(tool, "parameters", None) or {
                "type": "object",
                "properties": {},
            }
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": getattr(tool, "description", ""),
                        "parameters": copy.deepcopy(parameters),
                    },
                }
            )
        return specs

    def resolve(self, raw: str | None) -> Path:
        """把路径解析成仓库内的真实路径；越界抛 PathEscape。"""

        return _resolve_in_root(self.root, raw)

    # ---- 执行 ------------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """执行一次工具调用。永不抛异常，失败都返回 ``error: ...`` 字符串。"""

        try:
            return self._run(name, arguments)
        except Exception as exc:  # 兜底：五道闸本身出问题也不能把异常丢给调用方
            return f"error: tool {name} failed: {exc}"

    def _run(self, name: str, arguments: Any) -> str:
        # 闸 1：工具存在吗
        tool = self._by_name.get(name) if isinstance(name, str) else None
        if tool is None:
            return f"error: unknown tool '{name}'"

        # 闸 2：参数合法吗
        problem = _validate_arguments(tool, arguments)
        if problem:
            return f"error: invalid arguments for {name}: {problem}"

        # 闸 3：路径落在仓库内吗
        problem = self._path_gate(name, arguments)
        if problem:
            return problem

        # 闸 4：危险操作获批了吗
        if getattr(tool, "risky", False):
            problem = self._approval_gate(name, arguments)
            if problem:
                return problem

        # 闸 5：执行并裁剪输出
        try:
            output = tool.invoke(arguments)
        except Exception as exc:
            return f"error: tool {name} failed: {exc}"

        if not isinstance(output, str):
            output = "" if output is None else str(output)
        return clip(output, MAX_OUTPUT_CHARS)

    def _path_gate(self, name: str, arguments: dict[str, Any]) -> str | None:
        raw = arguments.get("path")
        if raw is None or not isinstance(raw, str):
            return None
        try:
            _resolve_in_root(self.root, raw)
        except PathEscape:
            return f"error: path escapes workspace: {raw}"
        except (OSError, ValueError, RuntimeError) as exc:
            return f"error: invalid arguments for {name}: unusable path {raw!r}: {exc}"
        return None

    def _approval_gate(self, name: str, arguments: dict[str, Any]) -> str | None:
        denied = f"error: approval denied for {name}"
        if self.read_only:
            return denied
        if self.approval == "never":
            return denied
        if self.approval == "ask":
            try:
                approved = bool(self._approver(name, arguments))
            except Exception:
                approved = False
            if not approved:
                return denied
        return None

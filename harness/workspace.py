"""适配层：把仓库的现状翻译成模型能读的文本。

「把测试修好」不是一条完整指令。哪个仓库、哪个分支、有没有未提交改动、
项目文档怎么说构建和测试，这些不给模型，它只能猜。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DOC_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml", "package.json")
DOC_LIMIT = 2000


class WorkspaceContext:
    """在对话开始前一次性收集稳定事实，之后折进系统提示。"""

    def __init__(self, root: Path, branch: str, status: str, commits: list[str], docs: dict[str, str]):
        self.root = root
        self.branch = branch
        self.status = status
        self.commits = commits
        self.docs = docs

    @classmethod
    def build(cls, cwd: str | Path) -> "WorkspaceContext":
        cwd = Path(cwd).resolve()

        def git(*args: str, fallback: str = "") -> str:
            try:
                done = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return done.stdout.strip() or fallback
            except Exception:
                # 不是 git 仓库，或者没装 git：优雅降级，不报错。
                return fallback

        root = Path(git("rev-parse", "--show-toplevel", fallback=str(cwd))).resolve()
        branch = git("rev-parse", "--abbrev-ref", "HEAD", fallback="(no git)")
        status = git("status", "--short", fallback="")
        log = git("log", "--oneline", "-5", fallback="")
        commits = [line for line in log.splitlines() if line.strip()]

        docs: dict[str, str] = {}
        for name in DOC_NAMES:
            path = root / name
            if path.is_file():
                try:
                    docs[name] = path.read_text(encoding="utf-8", errors="ignore")[:DOC_LIMIT]
                except OSError:
                    continue

        return cls(root=root, branch=branch, status=status, commits=commits, docs=docs)

    def text(self) -> str:
        lines = [
            "## 仓库现场",
            f"根目录：{self.root}",
            f"分支：{self.branch}",
            f"未提交改动：{self.status or '(clean)'}",
        ]
        if self.commits:
            lines.append("最近提交：")
            lines.extend(f"  {item}" for item in self.commits)
        for name, body in self.docs.items():
            lines.append(f"\n### {name}\n{body.strip()}")
        return "\n".join(lines)

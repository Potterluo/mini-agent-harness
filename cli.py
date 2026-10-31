#!/usr/bin/env python3
"""命令行前端：适配层的一种。

前端是命令行还是网页，应用层不关心；这里只负责把参数翻成对 AgentService 的调用。
"""

from __future__ import annotations

import argparse
import sys

# Windows 控制台默认 GBK，中文和符号会直接抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from harness.runtime import build_service


def ask_approval(name: str, arguments: dict) -> bool:
    detail = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
    answer = input(f"[approve] {name}({detail})? [y/N] ").strip().lower()
    return answer == "y"


def show(kind: str, payload: dict) -> None:
    if kind == "user":
        return
    if kind == "assistant":
        if payload.get("text"):
            print(f"\n[assistant] {payload['text']}")
        if payload.get("tool_calls"):
            print(f"[assistant] tools: {', '.join(payload['tool_calls'])}")
    elif kind == "review":
        verdict = "approved" if payload["approved"] else "rejected"
        print(f"[review] {payload['tool']} -> {verdict} {payload['reason']}")
    elif kind == "tool":
        first = str(payload["result"]).splitlines()
        head = first[0][:160] if first else ""
        print(f"[tool] {payload['name']}: {head}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一个能读懂的 coding harness")
    parser.add_argument("prompt", nargs="?", help="一次性任务；省略时进入交互模式")
    parser.add_argument("--cwd", default=".", help="agent 的仓库根目录")
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--thinking", action="store_true", help="开启思考模式（与工具调用可同时开）")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--approval", choices=["ask", "auto", "never"], default="ask")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--resume", default=None, help="续跑：会话 id 或 latest")
    parser.add_argument("--sessions", action="store_true", help="列出会话后退出")
    args = parser.parse_args(argv)

    service = build_service(
        args.cwd,
        model=args.model,
        base_url=args.base_url,
        thinking=args.thinking,
        effort=args.effort,
        approval=args.approval,
        read_only=args.read_only,
        max_iterations=args.max_iterations,
        approver=ask_approval if args.approval == "ask" else None,
        on_event=show,
    )

    if args.sessions:
        for session_id in service.list_sessions():
            print(session_id)
        return 0

    if args.resume:
        session_id = service.list_sessions()[0] if args.resume == "latest" and service.list_sessions() else args.resume
        print(f"[resume] {session_id}")
    else:
        session_id = service.new_session().id
        print(f"[session] {session_id}")

    if args.prompt:
        print(service.send_message(session_id, args.prompt))
        return 0

    print("交互模式，Ctrl-D 退出。")
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            return 0
        if not text:
            continue
        print(service.send_message(session_id, text))


if __name__ == "__main__":
    sys.exit(main())

# mini-agent-harness

一个能一次读完的 coding harness。它把「分层」和「六个组件」两件事放进同一个仓库：

- **骨架**：领域层定义语言，应用层编排流程，适配层做翻译，装配根负责接线。
- **血肉**：仓库现场、提示词形状、工具闸门、上下文削减、转录与记忆、有界委派。
- **三道闸**：代码校验 → 人类批准 → 模型审核，按成本从低到高排。

代码量在一个下午能读完的范围，逻辑一行不少。

## 目录

| 路径 | 层 | 职责 |
| --- | --- | --- |
| `harness/domain.py` | 领域 | Session、三类消息、Tool、Context、Decision |
| `harness/ports.py` | 应用 | SessionStore、ModelClient、Approver 三个协议 |
| `harness/roles.py` | 应用 | 助手与审核两个角色，各自组织上下文 |
| `harness/service.py` | 应用 | 主循环、审核闸、步数上限 |
| `harness/context.py` | 应用 | clip 与 compact |
| `harness/workspace.py` | 适配 | 收集仓库现场与项目文档 |
| `harness/tools.py` | 适配 | 六个工具与五道闸 |
| `harness/client.py` | 适配 | OpenAI 兼容客户端，思维链不回灌 |
| `harness/session.py` | 适配 | JSON 持久化、工作记忆 |
| `harness/runtime.py` | 装配根 | 唯一知道所有具体类的地方，含 delegate 工具 |
| `cli.py` | 前端 | 命令行适配器 |

依赖方向：应用层只 import 领域层；适配层实现应用层的接口；没人 import 装配根，除了启动脚本。

## 跑起来

```bash
pip install openai

export HARNESS_API_KEY=sk-...          # 或者 DEEPSEEK_API_KEY
export HARNESS_BASE_URL=https://api.deepseek.com/v1
export HARNESS_MODEL=deepseek-chat

python cli.py --cwd /path/to/repo --approval auto \
  "跑一下测试，把失败的用例修好"
```

参数：

| 参数 | 作用 |
| --- | --- |
| `--approval ask\|auto\|never` | 危险操作怎么批准 |
| `--read-only` | 只读模式，写操作一律拒绝 |
| `--thinking` | 开启思考模式（与工具调用可同时开） |
| `--max-iterations` | 循环硬上限，防死循环烧预算 |
| `--resume latest` | 续跑上一次会话 |
| `--sessions` | 列出会话 |

## 测试

```bash
python -m unittest discover -s tests
# Ran 101 tests ... OK
```

全部用临时目录和假模型客户端，不需要网络，也不碰真实仓库。

## 实跑记录

用 `deepseek-v4.1-flash` 在一个故意弄坏的小仓库上跑过一次：

```
[assistant] tools: run_shell
[tool] run_shell: FF                          # 两个用例失败
[assistant] tools: read_file, read_file
[tool] read_file: def add(a, b): ...
[assistant] 两个 bug 都清楚：add 用了减法，mean 多减了 1
[assistant] tools: patch_file
[review] patch_file -> approved
[tool] patch_file: patched calc.py (1 replacement)
[assistant] tools: run_shell
[tool] run_shell: ..                          # 通过
[assistant] 测试全部通过。
```

### 踩到的两个坑

**审核角色会扮演助手。** 把整段对话历史丢给审核模型，它会顺着助手的样子继续调工具（`finish_reason=tool_calls`），一个字都不回。审核必须用独立组织的上下文：任务、最近几条工具结果、待审的那一次调用，就这三样。

**历史里会出现悬空的 tool_call。** 审核发生在工具执行之前，此时 session 末尾是「带 tool_calls 但还没有结果」的助手消息。这种序列发给模型不合法（`tool` 消息必须紧跟 `assistant` 的调用），网关直接返回空内容。审核时要先把这条消息摘掉。

## 已知边界

- 只实现单轮工具调用循环，不支持并行工具的并发执行。
- 上下文削减是规则式的（按条数和长度），不做摘要。
- 委派只有一层（`max_depth=1`），子 agent 只读、步数默认 3。

# mini-agent-harness

一个只会文本进、文本出的模型，加上这层东西就能真的改仓库：11 个文件、约 1700 行，16 个提交，101 个测试。

本文按「需求 → 功能分析 → 架构 → 实现 → 踩坑 → 效果」的顺序组织，和代码一样按层推进。

## 一、需求

一个能改代码的 agent，至少要补四件事：看得见现场、动得了手、记得住状态、受得住约束。往下拆成六项具体要求：

| 要求 | 为什么是必须的 |
| --- | --- |
| 上下文忠实 | 现场信息不全，模型只能猜 |
| 成本可控 | 每轮重发全量上下文，账单和延迟都会失控 |
| 动作可控 | 自由 shell 太危险，工具要固定、参数要校验、危险动作要批准 |
| 窗口不炸 | 工具输出会撑满上下文，撑满之后模型表现为变笨 |
| 会话可续 | 长任务会被打断，状态不落盘就白干 |
| 支线隔离 | 查支线会把主上下文搅浑 |

非功能要求三条：换模型不改核心；测试不花钱；Windows 上能跑。

## 二、功能分析

每条要求配一个失败模式，失败模式直接变成测试用例。

| 要求 | 具体行为 | 失败模式 | 验收 |
| --- | --- | --- | --- |
| 上下文忠实 | 启动时收集根目录、分支、status、最近提交、项目文档 | 不是 git 仓库直接崩 | 非 git 目录下不报错 |
| 成本可控 | 不变的在最前，多变的在尾部 | 工具列表随机排序，缓存永不命中 | 多轮调用缓存命中上升 |
| 动作可控 | 固定工具集、参数校验、路径闸、批准闸 | 模型给 ../../etc/passwd | 越界返回错误串且不落盘 |
| 窗口不炸 | 单块封顶、老结果压缩、近期保真 | 一次读文件灌进十万字 | 单块不超 4000 字 |
| 会话可续 | 每步落盘、可按 id 续跑 | 中途挂掉丢状态 | 关进程再 `--resume` 能接上 |
| 支线隔离 | 子 agent 只读、步数更少、不能再派 | 无限递归烧钱 | 子 agent 工具箱没有 delegate |

## 三、架构

### 领域层

`Session`（对话 = id + 消息列表 + 工作记忆）；消息三型 `UserMessage` / `AssistantMessage(text, tool_calls)` / `ToolResult(tool_call_id, content)`；`ToolCall(id, name, arguments)`；`Tool` 协议五个成员：name、description、parameters、risky、invoke；`Context<OpType>` 带泛型输出类型；`Decision(approved, reason)`。

`risky` 是设计阶段特意加的：前四个成员给模型看，最后一个给执行层看。漏了它，接真实闸门时字段不够，改领域模型等于全链路重编译。

### 应用层

`SessionStore`、`ModelClient`、`Approver` 三个协议，只说明应用层需要什么；实现在适配层。测试时把 `ModelClient` 换成按脚本回放的假客户端，四种分支都能覆盖，一次 0.4 秒，不花钱。

`Context` 的泛型让同一套组装逻辑服务两个角色：回复用户时 `output_type=AssistantMessage`，审核时 `output_type=Decision`。

### 分层与装配根

领域层不认识外部协议；应用层只依赖接口；适配层把仓库、文件、模型翻译成领域对象；`runtime.py` 是装配根，唯一知道所有具体类的地方，换模型、换存储、换入口都只改它。

## 四、实现

提交顺序即依赖顺序，每个提交都能独立通过全部测试：

| 提交 | 内容 |
| --- | --- |
| `chore: 初始化项目骨架` | pyproject、目录 |
| `feat(domain)` | Session、三类消息、Tool、Context、Decision |
| `feat(ports)` | 三个协议 |
| `feat(context)` | clip 与 compact |
| `feat(session)` | JSON 持久化、工作记忆 |
| `feat(tools)` | 六个工具与五道闸 |
| `feat(workspace)` | 仓库现场与项目文档 |
| `feat(client)` | OpenAI 兼容客户端 |
| `feat(service)` | 两个角色与主循环 |
| `feat(runtime)` | 装配根与命令行 |
| `test: 主循环集成测试` | 假客户端回放 |
| `feat(delegate)` | 有界委派 |
| 三个 `fix` | 实跑暴露的问题 |

六项要求落在哪里：

| 要求 | 落点 | 关键实现 |
| --- | --- | --- |
| 上下文忠实 | `harness/workspace.py` | git 命令包一层降级，按优先级读 AGENTS.md、README、pyproject、package.json |
| 成本可控 | `harness/runtime.py` | 系统提示只在会话开始构造一次，之后只追加消息 |
| 动作可控 | `harness/tools.py` | 五道闸，失败一律返回 `error:` 字符串 |
| 窗口不炸 | `harness/context.py` | clip 封顶 4000 字，compact 只压老的工具结果 |
| 会话可续 | `harness/session.py` | 每步 put，临时文件 + os.replace |
| 支线隔离 | `harness/runtime.py` | `depth < max_depth` 才注册 delegate |

## 五、踩坑实录

**审核角色会扮演助手。** 把整段历史丢给审核模型，它顺着助手的语气继续调工具（`finish_reason=tool_calls`），一个字都不回。改成只给任务、最近工具结果、待审调用。

**悬空的 tool_call 导致空回复。** 审核发生在工具执行前，历史末尾是带 tool_calls 却没有结果的助手消息，网关拒绝这种序列，返回空内容。审核前先摘掉它。

**Windows 三处编码。** 控制台 GBK、子进程抓输出 GBK、`git status` 遇到中文文件名也是 GBK，都要显式 `encoding="utf-8", errors="replace"`。

**并行改同一个文件。** 两个子 agent 并行时漏了 `roles.py` 的归属，一个重写、另一个在跑测试，短暂出现 `NameError: ToolResult 未定义`。文件归属比接口归属更容易被忽略。

## 六、效果

在故意弄坏的小仓库上跑通：

```
[assistant] tools: run_shell
[tool] run_shell: FF                    # 两个用例失败
[assistant] tools: read_file, read_file
[assistant] 两个 bug 都清楚：add 用了减法，mean 多减了 1
[assistant] tools: patch_file
[review] patch_file -> approved
[tool] patch_file: patched calc.py (1 replacement)
[assistant] tools: run_shell
[tool] run_shell: ..                    # 通过
[assistant] 测试全部通过。
```

代价是 8 次模型调用和几百毫秒工具耗时，审计记录留在会话文件里。

## 目录

| 路径 | 层 | 职责 |
| --- | --- | --- |
| `harness/domain.py` | 领域 | Session、三类消息、Tool、Context、Decision |
| `harness/ports.py` | 应用 | SessionStore、ModelClient、Approver |
| `harness/roles.py` | 应用 | 助手与审核两个角色 |
| `harness/service.py` | 应用 | 主循环、审核闸、步数上限 |
| `harness/context.py` | 应用 | clip 与 compact |
| `harness/workspace.py` | 适配 | 仓库现场与项目文档 |
| `harness/tools.py` | 适配 | 六个工具与五道闸 |
| `harness/client.py` | 适配 | OpenAI 兼容客户端，思维链不回灌 |
| `harness/session.py` | 适配 | JSON 持久化、工作记忆 |
| `harness/runtime.py` | 装配根 | 唯一知道所有具体类的地方，含 delegate |
| `cli.py` | 前端 | 命令行适配器 |

## 跑起来

```bash
pip install openai

export HARNESS_API_KEY=sk-...          # 或 DEEPSEEK_API_KEY
export HARNESS_BASE_URL=https://api.deepseek.com/v1
export HARNESS_MODEL=deepseek-chat

python cli.py --cwd /path/to/repo --approval auto \
  "跑一下测试，把失败的用例修好"
```

| 参数 | 作用 |
| --- | --- |
| `--approval ask\|auto\|never` | 危险操作怎么批准 |
| `--read-only` | 只读模式，写操作一律拒绝 |
| `--thinking` | 开启思考模式（与工具调用可同时开） |
| `--max-iterations` | 循环硬上限 |
| `--resume latest` | 续跑上一次会话 |
| `--sessions` | 列出会话 |

## 测试

```bash
python -m unittest discover -s tests
# Ran 101 tests ... OK
```

全部用临时目录和假模型客户端，不联网，不碰真实仓库。

## 边界

- 只实现单轮工具调用循环，不做并行工具的并发执行。
- 上下文削减是规则式的（按条数与长度），不做摘要。
- 委派只有一层（`max_depth=1`），子 agent 只读、步数默认 3。

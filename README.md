# mini-agent-harness

一个只会文本进、文本出的模型，加上这层东西就能真的改仓库。11 个文件约 1700 行，17 个提交，103 个测试。

README 按工程顺序组织：先讲 agent 怎么跑，再给场景推需求，然后产品设计、分层架构、逐层实现，最后是验证与复盘。博客那篇同一条脉络。

## 一、Agent 是怎么跑起来的

模型只能做一件事：文本进、文本出。Agent 把这件事包成一个循环：

```
读现场 → 模型决策 → 执行动作 → 反馈结果 → 再决策
```

循环在两种情况下停：模型只回文本、不再要动作，或者到达步数上限。

循环本身几十行。让它变成可用系统的是外面那层 harness：决定它看到什么、能做什么、后果怎么处理、状态存哪。

## 二、一个具体的场景

`demo-repo` 里两个文件，`calc.py` 的两个函数被改坏，`python -m unittest` 两个用例全红。任务一句话：跑测试，修好，只改 calc.py。

裸循环会撞六堵墙：

| 墙 | 表现 |
| --- | --- |
| 不知道自己在哪 | 只能凭文件名猜结构 |
| 动不了手 | 只能说「改成 a + b」，改文件靠人 |
| 拦不住 | 接上 shell 后可能删测试来「通过」 |
| 看不全 | 输出都塞回上下文，十几步撑满 |
| 记不住 | 进程一断全白干 |
| 会跑偏 | 支线污染主线，子任务再派子任务 |

## 三、产品设计

| 能力 | 对应需求 | 不做会怎样 |
| --- | --- | --- |
| 现场注入 | 会话前收集根目录、分支、改动、项目文档 | 模型靠猜，改错文件 |
| 固定工具集 | 六个受限动作，参数有 schema | 模型乱敲命令 |
| 分级授权 | 危险操作需批准，越界路径直接拒 | 跑出仓库、误删文件 |
| 上下文管理 | 单块封顶、老结果压缩、近期保真 | 窗口撑满，表现为变笨 |
| 状态持久化 | 每步落盘，可按会话续跑 | 中断即丢失 |
| 委派隔离 | 子 agent 只读、步数更少、不能再派 | 支线污染主线，递归烧钱 |

工程约束：换模型不改核心；测试不依赖真实模型；Windows 能跑。

非目标：并行工具调用、摘要式压缩、多模态、多层委派。

验收标准：非 git 目录不报错；未知工具与错参数返回可读错误；`../../etc/passwd` 被拒且不落盘；单块不超 4000 字；存盘读回不丢消息与记忆；子 agent 工具箱没有 delegate。

## 四、架构设计

### 三层

| 层 | 放什么 | 判断标准 |
| --- | --- | --- |
| 领域层 | Session、消息、Tool、Context、Decision | 换外部服务它不用改 |
| 应用层 | 循环、角色、上下文组装、接口定义 | 只 import 领域层 |
| 适配层 | 文件系统、git、模型 API、JSON 存储 | 实现应用层接口 |
| 装配根 | 创建具体类、接线 | 唯一 import 适配层的地方 |

### 四个抽象

- `Session` + 三种消息（`UserMessage` / `AssistantMessage(text, tool_calls)` / `ToolResult(tool_call_id, content)`）+ `ToolCall(id, name, arguments)`。
- `Tool` 协议：name、description、parameters、risky、invoke。`risky` 是工具本身的属性，不是某次调用的属性，所以放领域层。
- `Context<OpType>` 带泛型输出类型：回复用 `AssistantMessage`，审核用 `Decision`，同一套组装逻辑。
- 三个接口：`SessionStore`、`ModelClient`、`Approver`，全部 Protocol。

### 怎么往上加瓦

固定四步：领域层问要不要新概念，应用层定谁编排，适配层做翻译，装配根接线一行。

| 新增能力 | 领域层 | 应用层 | 适配层 | 装配根 |
| --- | --- | --- | --- | --- |
| 模型审核 | 加 `Decision` | 加 `ReviewerRole`，循环插一道 | 无 | 多传一个角色 |
| 工作记忆 | 复用 `Session.memory` | 循环里加记账 | 存储已支持 | 无 |
| 有界委派 | 复用 `Tool` | 无 | 加 `DelegateTool` | 按深度注册 |

三块里两块没动领域层。

## 五、实现

| 提交 | 内容 | 层 |
| --- | --- | --- |
| `chore: 初始化项目骨架` | pyproject、目录 | 无 |
| `feat(domain)` | Session、消息、Tool、Context、Decision | 领域 |
| `feat(ports)` | 三个协议 | 应用 |
| `feat(context)` | clip 与 compact | 应用 |
| `feat(session)` | JSON 持久化、工作记忆 | 适配 |
| `feat(tools)` | 六个工具与五道闸 | 适配 |
| `feat(workspace)` | 仓库现场与项目文档 | 适配 |
| `feat(client)` | OpenAI 兼容客户端 | 适配 |
| `feat(service)` | 两个角色与主循环 | 应用 |
| `feat(runtime)` | 装配根与命令行 | 装配根 |
| `test: 主循环集成测试` | 假客户端回放 | 测试 |
| `feat(delegate)` | 有界委派 | 适配 |
| `fix(service)` | 工具执行后写工作记忆 | 应用 |
| 三个 `fix` | 实跑暴露的问题 | 见复盘 |

实现要点：

- 现场注入在会话开始收一次，git 命令全部带降级。
- 五道闸顺序固定，失败一律返回 `error:` 字符串，因为错误是给模型的反馈。
- 三道闸按成本排：代码校验 → 人工批准 → 模型审核。
- `clip` 封顶 4000 字，`compact` 只压最近六条之外的工具结果。
- 每步落盘；工具执行后记账（文件上限 8、笔记上限 5）。
- 子 agent 只读、步数 3、`depth < max_depth` 才注册 delegate。

## 六、验证

103 个用例，全部用临时目录与假模型客户端，不联网，不到一秒跑完。

真实运行（`deepseek-v4.1-flash`）：

```
[assistant] tools: run_shell
[tool] run_shell: FF                     # 两个用例失败
[assistant] tools: read_file, read_file
[tool] read_file: def add(a, b): ...
[assistant] 两个 bug 都清楚：add 用了减法，mean 多减了 1
[assistant] tools: patch_file
[review] patch_file -> approved
[tool] patch_file: patched calc.py (1 replacement)
[assistant] tools: run_shell
[tool] run_shell: ..                     # 通过
[assistant] 测试全部通过。
```

8 次模型调用，几百毫秒工具耗时，改动落在一个文件的两处函数。

## 七、复盘

四个坑都是实测才暴露的：

- 审核角色会扮演助手：给它整段历史，它继续调工具（`finish_reason=tool_calls`），不回判断。改成只给任务、最近工具结果、待审调用。
- 悬空 tool_call：审核在工具执行前发生，历史末尾那条带 tool_calls 的助手消息不合法，网关返回空内容，审核前先摘掉。
- Windows 三处编码：控制台、子进程输出、`git status`，都要显式 UTF-8。
- 并行改同一个文件：漏了 `roles.py` 的归属，短暂 `NameError`。文件归属比接口归属更容易被忽略。

已知不足：并行工具调用、摘要式压缩、多层委派、多模态都没做。下一步最想补摘要式压缩，因为现在压掉的老结果再也回不来。

## 目录

| 路径 | 层 | 职责 |
| --- | --- | --- |
| `harness/domain.py` | 领域 | Session、消息、Tool、Context、Decision |
| `harness/ports.py` | 应用 | SessionStore、ModelClient、Approver |
| `harness/roles.py` | 应用 | 助手与审核两个角色 |
| `harness/service.py` | 应用 | 主循环、审核闸、记忆记账 |
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
# Ran 103 tests ... OK
```

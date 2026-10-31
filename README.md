# mini-agent-harness

一个只会文本进、文本出的模型，加上这层东西就能真的改仓库。11 个文件约 1700 行，16 个提交，101 个测试。

下面这段是它在一个故意弄坏的仓库里的真实运行，任务只有一句「跑一下测试，把失败的修好」：

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

这段日志能成立，背后压着七层东西。README 按这七层组织，和博客那篇同一脉络。

## 七层

### ① 现场：它凭什么知道自己在哪个仓库

`harness/workspace.py`。会话开始前收一次：仓库根、分支、`git status`、最近五条提交，再按优先级读 `AGENTS.md`、`CLAUDE.md`、`README.md`、`pyproject.toml`、`package.json`，折进系统提示，整个会话只构造一次。

所有 git 命令都包一层降级，不是 git 仓库就返回空字符串。

拆掉：模型凭文件名猜结构，第一条命令就跑到别的目录。

### ② 工具与闸门：它凭什么能动手

`harness/tools.py`。六个固定工具（list_files、read_file、search、run_shell、write_file、patch_file），后三个标 `risky`。每次调用顺序过五道检查：工具存在、参数合 schema、路径不越界、危险操作批准、执行并裁剪。

失败一律返回 `error:` 开头的字符串，因为错误信息本身是给模型的反馈：看到 `old_text must occur exactly once, found 3`，它下一步会换一个更精确的匹配。

拆掉：给模型一个裸 shell。

### ③ 三道闸：谁敢批准它动手

`harness/roles.py` + `harness/service.py`。代码校验 → 人工批准 → 模型审核，顺序按成本从低到高。

第三道闸有个坑：最初把整段历史丢给审核模型，它会顺着助手的语气继续调工具（`finish_reason=tool_calls`）；改成只给任务、最近工具结果、待审调用三样，历史清空。另外审核发生在工具执行前，历史末尾会有一条带 `tool_calls` 却没有结果的助手消息，这种序列不合法，网关返回空内容，打之前要先摘掉。

拆掉：模型可以为了「让测试通过」把测试删掉，日志里还看不出异常。

### ④ 上下文削减：它怎么不把自己撑死

`harness/context.py`。`clip` 给单块输出封顶 4000 字；`compact` 只压最近六条之外的工具结果（压到 400 字），越近越保真。

拆掉：跑到第十步上下文就满，表现为「模型变笨」。

### ⑤ 记忆与续跑：它怎么记得住

`harness/session.py`。每步落盘，完整转录一份，工作记忆一份（任务、在动的文件、笔记，有上限且去重）。`--resume latest` 接着干。

已知不足：`remember()` 这个记账动作目前只有定义，没有接进主循环，用过的文件还没真正写进 `files`。

拆掉：断一次全丢。

### ⑥ 有界委派：它怎么不越帮越忙

`harness/runtime.py`。子 agent 拿只读工具箱、步数默认 3、深度到顶就不再注册 `delegate`，递归从结构上掐断。

拆掉：支线污染主线，或者一层层往下派烧钱。

### ⑦ 接口与装配根：换一个模型要改几行

`harness/ports.py` + `harness/runtime.py`。`SessionStore`、`ModelClient`、`Approver` 三个协议定义在应用层，实现在适配层，装配根是唯一知道所有具体类的地方。

好处在测试里立刻兑现：把 `ModelClient` 换成按脚本回放的假客户端，循环四种分支全都能覆盖，一次 0.4 秒，不花钱。

拆掉：每加一家模型就在循环里加一层 if，测试永远得真调接口。

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

## 踩坑

- 审核角色会扮演助手：给它整段历史，它继续调工具而不回判断。
- 悬空的 tool_call：审核在工具执行前发生，历史末尾那条带 tool_calls 的助手消息不合法，网关返回空内容。
- Windows 三处编码：控制台、子进程输出、`git status`，都要显式 UTF-8。
- 并行改同一个文件：两个子 agent 漏了 `roles.py` 的归属，短暂出现 `NameError`。文件归属比接口归属更容易被忽略。

## 边界

- 单轮工具循环，不做并行工具的并发执行。
- 上下文削减是规则式的（条数与长度），不做摘要。
- 委派只有一层（`max_depth=1`），子 agent 只读、步数默认 3。

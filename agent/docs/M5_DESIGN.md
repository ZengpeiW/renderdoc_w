# M5 设计：qrendertest 内嵌 AI 对话面板

## 1. 目标

在 qrendertest 里提供一个可停靠的“AI 助手”面板。用户不离开 RenderTest，就能针对当前 capture 提问。面板内部启动并连接 `codex app-server`，Agent 继续通过 M0–M4 已有的 rendertest MCP 工具读取**本窗口**的数据。

M5 新增 UI 和 app-server 客户端，沿用 Bridge IPC v1。为保证内嵌面板的边界，MCP 增加固定实例/session 模式和只读 `read_artifact` 工具；`pick_pixel` / `pixel_history` 增加 `type_cast`。外部 CLI 原有实例选择行为保持兼容。

```
qrendertest.exe
 ├─ AgentChatPanel (Qt dock)                       ← 新增
 │    └─ CodexAppServerClient ──stdio JSONL──► codex.exe app-server   ← 新增
 │                                                   └─stdio MCP──► python -m rendertest_mcp
 │                                                                        │ RENDERTEST_AGENT_INSTANCE_ID=本实例
 └─ rendertest_agent_bridge (Python 3.6, 已有) ◄──── 文件 mailbox ────────┘
```

非目标：

- 不做多 provider（ACP、OpenAI 兼容、Claude）。Bread 的 31k 行 Python agent runtime 不移植。
- 不让 Agent 执行 shell，也不写文件。只读沙箱，所有审批请求一律拒绝。
- 不在 qrenderdoc 里实现 MCP。工具仍由独立 MCP server 提供，外部 Codex CLI 也能继续使用。

## 2. 交互设计

### 2.1 面板布局

```
┌ AI 助手 ─────────────────────────────── [新对话] [⚙] ┐
│ ● 已连接 · gpt-5.x · user@x.com     rendertest ✓ 22 工具 │
├──────────────────────────────────────────────────────┤
│ 你：为什么 (256,256) 是黑的？                              │
│     [EID 1513] [RT 116618 (256,256)]                     │
│                                                          │
│ ▸ get_context                       0.1s ✓               │
│ ▸ pick_pixel  RT 116618 (256,256)   0.4s ✓               │
│ ▸ pixel_history                      3.2s ✓               │
│                                                          │
│ AI：该像素最后一次被 EID 1459 写入，但片元在 ……              │
│     （EID 1459 / ResourceId::116618 可点击）                │
├──────────────────────────────────────────────────────┤
│ 上下文：[EID 1513 ×] [像素 116618 (256,256) ×]               │
│ ┌──────────────────────────────────────────────┐ [发送]  │
│ │ 输入问题，Enter 发送，Shift+Enter 换行          │ [停止]  │
│ └──────────────────────────────────────────────┘        │
└──────────────────────────────────────────────────────┘
```

- **入口**：`Window → AI 助手`。窗口用 `objectName="agentChat"` 参与布局保存和恢复，与 Python Shell 一致。
- **状态行**：显示连接状态、模型、账号和 rendertest MCP 状态。
  - 连接状态：未启动 / 启动中 / 就绪 / 未登录 / 错误。
  - rendertest MCP 状态取自 `mcpServerStatus/list`：已连接时显示工具数，连接失败时显示 `toolsError`。
- **发送**：Enter 发送，Shift+Enter 换行。回合进行中，“发送”按钮变为禁用，“停止”按钮可用。M5 不做 steer。
- **新对话**：丢弃当前 thread，下一次发送时新建。
- **⚙**：打开轻量设置对话框，包含 codex 路径、MCP Python 路径、模型、推理强度，以及“隔离其它 MCP server”开关。

### 2.2 消息渲染

整段对话放在一个 `QTextBrowser` 里，用 HTML 渲染，`setOpenLinks(false)`，链接点击统一由面板处理。这样能跨消息选择和复制，实现也最简单。

| app-server item | 渲染 |
|---|---|
| `userMessage` | 用户气泡，下面列出发送时的上下文 chip |
| `agentMessage` | Markdown 转 HTML，流式追加，每 50ms 节流重绘一次 |
| `reasoning` | 默认折叠为一行“思考中…”，只展示 summary |
| `mcpToolCall` | 默认折叠为一行工具卡：工具名、关键参数、耗时和 ✓/✗。点击展开参数 JSON、错误和结果；按 UTF-8 字节截断，参数最多 8 KiB、结果最多 4 KiB |
| `commandExecution` / `fileChange` | 显示服务端返回的实际状态；不能把所有项都标成“已拒绝”。shell 始终禁用，正常情况下不应出现命令项 |
| `plan` / `webSearch` / 其它 | 灰色单行摘要，不报错 |

Qt 5.9.4 没有 `QTextDocument::setMarkdown`（5.14 才加），所以要自带一个最小 Markdown 转 HTML 实现，覆盖标题、列表、粗体、行内代码、代码块、链接和简单表格。

### 2.3 可点击引用

渲染前对 agent 文本做一次 linkify。developerInstructions 里要求模型按下面的格式书写引用，以提高命中率。

| 文本模式 | 链接 | 点击行为 |
|---|---|---|
| `EID 1459`、`EID: 1459`、`event 1459` | `rt://event/1459?source=<capture token>` | 先验证消息来源仍是当前 capture，再执行 `m_Ctx.SetEventID({}, 1459, 1459)` |
| `ResourceId::116618` | `rt://resource/116618?source=<capture token>` | 校验来源后，纹理 → TextureViewer；buffer → BufferViewer；其它 → ResourceInspector |
| 工具结果 envelope 中的 PNG artifact | `<img>` 缩略图（最长边 256） | 再次完整校验后用系统查看器打开 |

图片只接受 rendertest 工具完整 structuredContent 或文本 JSON envelope 中登记的 artifact（最多 8 个），在结果截断前提取。路径必须位于 `RENDERTEST_AGENT_ROOT/artifacts/<instanceId>/<captureSessionId>/`，身份取自同一 envelope。校验规范路径、实际打开文件、大小和完整 SHA-256，拒绝符号链接、Windows junction、伪 PNG 以及超过 8 MiB、单边超过 8192 或超过 16 MP 的图片。无效图片显示原因。`QTextBrowser` 禁止自动加载本地/网络资源，仅注入通过校验的内部图片资源；点击重新验证。

每个消息记录由面板生成的 capture 生命周期 token；capture 加载/关闭时更换 token，旧消息保留旧 token。渲染时为所有 `rt://` 链接附加消息 token（包括模型原本写出的 Markdown 链接，不能信任其自带 token）。点击必须匹配当前 token 且当前实例/session 绑定仍有效，否则只提示引用已过期，不执行导航。

### 2.4 “问 AI”入口和上下文

| 位置 | 入口 | 附带上下文 |
|---|---|---|
| Event Browser 事件右键 | 问 AI：这个事件 | `eventId` |
| Texture Viewer 资源缩略图右键 | 问 AI：这个资源 | `resourceId`、`eventId` |
| Texture Viewer Pixel Context 区 | 按钮“问 AI：这个像素” | `resource_id`、`x`、`y`、`mip`、`slice`、`sample`、`view`、`type_cast`、`event_id` |
| Mesh Viewer 顶点行右键 | 问 AI：这个顶点 | `event_id`、`stage`、`vertex`、`index`、`instance`、`view` |

点击入口后，面板显示并获得焦点，在上下文区添加对应 chip，并在输入框预填一句可编辑的问题（例如“这个像素为什么是这个颜色？”），不自动发送。

当前 EID 默认作为一个 chip 加入，可以删掉。上下文在**发送时**快照，之后 UI 怎么切换都不影响这一轮的问题。

像素入口分析原始资源值，保留当前类型解释；不把显示 Shader、overlay、显示范围/通道调整的结果当成资源值。启用显示 Shader 或 overlay 时 chip 和预填问题明确这一点。MSAA 的 Average val 没有单一 sample 的写入历史，入口禁用并提示选择具体 sample，不静默改用 sample 0。`type_cast` 使用 RenderDoc 枚举名（例如 `Depth`、`UNormSRGB`），不能使用界面显示名；工具输出值的解释及 pixel history 缓存、游标都纳入这个参数。

### 2.5 Capture 生命周期

- 未打开 capture、bridge 不可用或无法核验当前 capture 时可以纯聊天，rendertest MCP 必须禁用，不得回退到其它窗口。面板顶部显示提示。
- capture 关闭或加载时立即使旧回合、待发送内容和异步回调失效，结束旧 app-server 进程树，丢弃旧 thread；旧消息保留为历史，旧引用不可点击跳转。下一次发送建立新会话。
- 发送前读取 manifest，核验本进程实例、存活状态、capture 路径和 session；启动回合前再次核验。固定 session 必须传入 MCP 的强制绑定配置，不能只写进提示词。
- 关闭 qrendertest 时先 interrupt 当前回合，再结束 app-server 进程树。

## 3. 协议接法

### 3.1 依据

本节字段来自 `codex-cli 0.162.1`：

```powershell
codex app-server generate-ts --out <dir>
```

下面这些已经在本机用真实 app-server 实测过。探测脚本没有入库，回合里不含任何 capture 数据：

- `initialize`、`initialized`、`account/read`：已登录 chatgpt 账号。
- `config/read`：能列出用户的 8 个 `mcp_servers`。
- `skills/extraRoots/set` 加 `skills/list`：在空 cwd 下也能加载 4 个 rendertest Skill，无报错。
- `thread/start`：返回 `sandbox={"type":"readOnly","networkAccess":false}` 和 `approvalPolicy="never"`；thread 级 `config` 能禁用其它 MCP server。
- `mcpServerStatus/list {threadId}`：只剩 `rendertest connected 22 tools`。
- 一次完整 `turn/start`：
  - 流式通知依次出现 `item/started`、`item/agentMessage/delta`、`item/reasoning/summaryTextDelta`（单回合 1600 多条）、`item/completed`、`thread/tokenUsage/updated` 和 `turn/completed`。
  - rendertest MCP 调用（包括会改 UI 的 `set_event`）都没有触发审批 server request，结果直接以 `mcpToolCall` item 返回。
  - 模型自报 `NO_SHELL`，没有 shell 工具可用。
- `turn/interrupt`：立即确认，`turn/completed(status=interrupted)` 在 0.1 秒内到达。

以上为最初 22 工具版本的探测记录；新增 `read_artifact` 后为 23 工具。工具数由状态接口读取，不作协议常量。

- 消息格式：每行一个 JSON 对象，不带 `"jsonrpc"` 字段。
- 三种消息：带 `id` 的是请求或响应，只有 `method` 的是通知，带 `id` 和 `method` 的是 server request，需要回复。

新增 `agent/devtools/appserver_schema_check.py`。它重新生成 TS 绑定，检查第 3.3–3.6 节用到的 method 和字段是否都还在，用于 codex 升级后的回归检查。

M5c 的可复现检查已入库：schema 脚本在 Codex 0.162.1 上验证 31 个方法、123 个字段、11 个 feature 开关；`appserver_skills_check.py` 使用空的临时 CODEX_HOME，验证部署目录、仓库外空目录，以及不使用 extraRoots 的 cwd 回退三种情况。它们不调用模型，也不启动登录。浏览器登录完成流程仍需人工验收，自动化只验证 fake app-server 的成功、失败、取消和状态恢复。

### 3.2 进程启动

由 `AgentLaunchConfig` 负责启动参数。

**codex 可执行文件的查找顺序**：

1. env `RENDERTEST_AGENT_CODEX_COMMAND`（测试用，可以指向 fake server，按空格拆分 argv）
2. 设置项 `Agent_CodexPath`
3. `%APPDATA%\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe`
4. PATH 上的 `codex.exe`

不使用 `codex.cmd` 或 `codex.ps1` shim。原因是经过 cmd 中转后，停止和退出时无法可靠地结束整个进程树。

**启动参数**用 `QProcess` 的参数列表传入，不拼 shell 字符串：

```text
codex.exe app-server
  -c mcp_servers.rendertest.command="<MCP python>"
  -c mcp_servers.rendertest.args=["-m","rendertest_mcp"]
  -c mcp_servers.rendertest.env.RENDERTEST_AGENT_INSTANCE_ID="<本实例 id>"
  -c mcp_servers.rendertest.env.RENDERTEST_AGENT_FIXED_INSTANCE_ID="<本实例 id>"
  -c mcp_servers.rendertest.env.RENDERTEST_AGENT_CAPTURE_SESSION_ID="<发送时 session>"
  -c mcp_servers.rendertest.env.RENDERTEST_AGENT_ROOT="<agent root>"
  -c mcp_servers.rendertest.tool_timeout_sec=180
  -c mcp_servers.rendertest.startup_timeout_sec=30
  -c web_search="disabled"
  -c features.shell_tool=false        -c features.unified_exec=false
  -c features.apps=false              -c features.plugins=false
  -c features.computer_use=false      -c features.browser_use=false
  -c features.browser_use_external=false
  -c features.image_generation=false  -c features.multi_agent=false
  -c features.goals=false             -c features.hooks=false
```

- `-c` 参数的格式与 `agent/devtools/codex_config.py` 中的 `cli_overrides()` 一致，C++ 侧照搬。
- `shell_tool=false`、`unified_exec=false` 和 `web_search="disabled"` 始终设置，不受 MCP 隔离开关影响。其它 feature 开关在隔离打开时设置；关闭隔离继承用户其它能力，状态行显示“非隔离”。
- 模型不硬编码。默认跟随用户 `config.toml` 的 `model`（本机是自定义 provider），面板显示 `thread/start` 返回的实际模型。
  - `model/list` 完整读取分页，用条目的 `model` 作为请求值，不能使用目录 `id`；推理选项来自 `supportedReasoningEfforts[].reasoningEffort`。
  - 设置保存到 `PersistentConfig`；环境变量 `RENDERTEST_AGENT_MODEL` / `RENDERTEST_AGENT_REASONING_EFFORT` 优先，分别传到 `thread/start.model` / `turn/start.effort`。自定义 provider 或目录不可用时允许手填字符串，空值表示跟随 Codex 配置。
- **MCP Python** 的查找顺序：
  1. env `RENDERTEST_MCP_PYTHON`
  2. 设置项 `Agent_McpPython`
  3. 开发构建时由编译期宏 `RENDERTEST_AGENT_SOURCE_DIR` 推出的 `<repo>/agent/mcp_server/.venv/Scripts/python.exe`

  三处都没有找到时，面板给出配置指引，不启动。
- **实例/session**：每个面板最多一个 app-server，同时固定本实例 id 和发送时 capture session。MCP 固定模式禁止显式参数覆盖实例，所有调用（含 `get_context` / `get_status`）携带 IPC `expected.captureSessionId`，响应不得重新绑定 session。capture 变化后重建进程。没有有效绑定时显式配置 `rendertest.enabled=false`，纯聊天也不要求安装 MCP Python。
- **cwd**：`<exe>/integrations/rendertest_agent/`。
- **进程树**：`codex.exe` 放进一个设置了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Windows Job Object。这样 qrendertest 无论是正常退出还是崩溃，`codex.exe` 和它拉起的 MCP python 都会一起结束。
  - 实现：用 `QProcess::setCreateProcessArgumentsModifier` 加上 `CREATE_SUSPENDED`，把进程加入 job 后再 resume。
- **I/O**：`QProcess` 运行在 GUI 线程，靠 `readyReadStandardOutput` 异步按行切分，不需要工作线程。
  - 单行上限 16MB，超过即判定协议错误。
  - stderr 只保留最后 64KB，写入 `logs/chat-<instance>.log`，出错时在 UI 显示最后几行。

### 3.3 握手

```jsonc
→ {"id":1,"method":"initialize","params":{"clientInfo":{"name":"qrendertest","title":"RenderTest","version":"<ver>"},"capabilities":{"experimentalApi":true}}}
← {"id":1,"result":{"userAgent":"...","codexHome":"...","platformFamily":"windows","platformOs":"windows"}}
→ {"method":"initialized","params":{}}
→ {"id":2,"method":"account/read","params":{"refreshToken":false}}
← {"id":2,"result":{"account":{"type":"chatgpt","email":"...","planType":"..."},"requiresOpenaiAuth":true}}
```

- **未登录**：仅当 `account` 为 `null` 且 `requiresOpenaiAuth=true` 时进入“未登录”状态；无需 OpenAI 凭据的自定义 provider 不受此条件阻塞。M5c 的“登录”按钮调用 `account/login/start {"type":"chatgpt"}`，用 `QDesktopServices` 打开返回的 `authUrl`，然后等待 `account/login/completed`。
  - 未登录时保留已初始化进程；再次发送只重读 `account/read`。失败的原问题不自动重发。登录成功须匹配本次 `loginId`，重新读取账户并建立 thread，之后由用户重新发送。
  - `account/login/cancel {"loginId":...}` 返回 `canceled` 时结束等待，忽略迟到完成；返回 `notFound` 时重新确认账号，不谎报取消成功。失败、进程退出、capture 变化均清理待登录状态。
  - `account/updated` 后重新读取账号；退出登录时废弃 thread 并显示登录入口，若有运行中回合则先结束进程树。
- **Skills**：调用 `skills/extraRoots/set {"extraRoots":["<exe>/integrations/rendertest_agent/skills"]}`，确保脱离仓库也能加载 4 个 rendertest Skill。这个方法是 experimental 的，失败时退回到依赖 cwd 下的 `.agents/skills`。
- **隔离**：开关打开时（默认打开），调用 `config/read {"cwd":...}`，从 `config.mcp_servers` 中取出除 `rendertest` 以外的名字，在 `thread/start.config` 里逐个设为 `enabled:false`。
  - plugins/apps 带来的 `codex_apps`、`cua_repl` 等，由第 3.2 节的 feature 开关关闭。

### 3.4 Thread

```jsonc
→ {"id":3,"method":"thread/start","params":{
     "cwd":"<exe>/integrations/rendertest_agent",
     "sandbox":"read-only",
     "approvalPolicy":"never",
     "ephemeral":true,
     "serviceName":"qrendertest",
     "model":"<可选>",
     "developerInstructions":"<见下>",
     "config":{"mcp_servers.blender.enabled":false, "...":false}}}
← {"id":3,"result":{"thread":{"id":"thr_..."},"model":"...","sandbox":{...},"approvalPolicy":"never",...}}
→ {"id":4,"method":"mcpServerStatus/list","params":{"threadId":"thr_...","detail":"toolsAndAuthOnly","limit":100}}
← {"id":4,"result":{"data":[{"name":"rendertest","runtimeStatus":"connected","tools":{...22...},"toolsError":null},{"name":"blender","runtimeStatus":"disabled",...}],"nextCursor":null}}
```

- **校验 sandbox**：确认 `sandbox.type == "readOnly"` 且 `approvalPolicy == "never"`，否则拒绝开聊并提示。
- **校验 MCP**：必须带 `threadId` 查询 `mcpServerStatus/list`。不带时返回的是全局配置，看不到 thread 级的禁用。
  - `rendertest` 处于 `starting` 时，每 500ms 轮询一次，最多 30 秒。
  - 完整读取分页；配置读取、状态查询失败或分页异常时，隔离模式不得发送回合。若仍存在其它非 `disabled` server，必须阻止对话并列出名字。纯聊天模式还必须确认 rendertest 已禁用；绑定模式则必须确认 rendertest 已连接。
- **developerInstructions** 固定为英文，内容包括：
  - Agent 运行在 qrendertest 实例 `<id>` 中，必须先使用 `rendertest-core` Skill；
  - 除非用户明确要求，不要调用 `set_event`；
  - 引用事件写成 `EID <n>`，引用资源写成 `ResourceId::<n>`；
  - 用用户的语言回答；
  - 用户消息里的 `<rendertest_context>` 是发送时的快照，调用工具时要显式传 `event_id`。ResourceId 传十进制字符串，不带 `ResourceId::` 前缀。
  - 像素查询传入 `type_cast`，分析的是显示处理前的资源值。
  - 文本 artifact 使用只读 `read_artifact` 分页或字面搜索读取，无需 shell。
- **一个对话对应一个 thread**。M5a–M5c 使用 `ephemeral:true`。M5d 改为非 ephemeral，再接 `thread/resume`。
- **仍保留的内置工具**：实测隔离后仍有 `apply_patch`、`view_image`、`request_user_input`、`tool_search`，以及 MCP resource 读取。
  - `apply_patch` 受 read-only 沙箱和 `never` 审批策略限制，不能写盘。已实测：返回 `patch rejected: writing is blocked by read-only sandbox`，没有发出审批 server request，文件未生成。
  - `request_user_input` 走 server request，按第 3.6 节回复错误。

**文本 artifact**：`read_artifact(path, start_line=1, limit=100, search=None)` 只读取本 MCP 进程此前成功工具响应中登记的文本文件。校验实例/session、artifact 根目录、文件大小和完整 SHA-256；拒绝越界路径、符号链接和 Windows junction。文件上限 64 MiB，单页输出有固定字节预算，返回可续读的行号。进程重启后需要重新调用生成 artifact 的工具，不接受任意本地文件路径。

### 3.5 Turn 与流式事件

```jsonc
→ {"id":5,"method":"turn/start","params":{"threadId":"thr_...","input":[{"type":"text","text_elements":[],
     "text":"<rendertest_context>\ninstanceId: 44004-ab12cd34\ncaptureSessionId: <manifest 中的值>\ncapture: E:/.../nanite.rdc\ncurrentEventId: 1513\nselection: {\"kind\":\"pixel\",\"resourceId\":\"116618\",\"x\":256,\"y\":256,\"mip\":0,\"slice\":0,\"sample\":0}\n</rendertest_context>\n\n为什么这个像素是黑的？"}],
     "effort":"<可选>"}}
← {"id":5,"result":{"turn":{"id":"turn_...","status":"inProgress",...}}}
```

通知到 UI 的映射：

| 通知 | 处理 |
|---|---|
| `turn/started` | 进入运行态 |
| `item/started` | 按 `item.type` 新建一个渲染块；`mcpToolCall` 显示为进行中 |
| `item/agentMessage/delta` | 按 `itemId` 追加文本，节流重绘 |
| `item/reasoning/summaryTextDelta` | 更新折叠的思考摘要 |
| `item/mcpToolCall/progress` | 在工具卡上显示 progress message |
| `item/completed` | 用完整 item 覆盖本地状态，以服务端为准；工具卡写入 `status`、`durationMs`、`result` 和 `error` |
| `thread/tokenUsage/updated` | 在状态行显示 token 用量 |
| `error` | 行内显示错误；`willRetry=true` 时显示“重试中” |
| `turn/completed` | 根据 `turn.status`（completed / interrupted / failed）结束运行态；failed 时显示 `turn.error.message` |
| 其它 | 忽略，并写入 debug 日志，保持前向兼容 |

状态归约由纯函数类 `AgentConversation` 完成：输入通知 JSON，输出 item 列表。它不依赖 QWidget，方便用录制好的 JSONL 回放测试。

### 3.6 停止、审批与异常

- **停止**：发送 `turn/interrupt {"threadId","turnId"}`，等待 `turn/completed(status=interrupted)`。实测 0.1 秒内到达。
  - 2 秒内没有等到，就结束整个进程树，把当前回合标为“已中断”。
  - 下一次发送时懒重启，新建 thread，并提示上下文已丢失。
  - 正在执行的 MCP 工具可能已经在 bridge 里排队；bridge 照常执行完，结果丢弃。
- **Server request**：必须回复，否则回合会卡住。

| method | 回复 |
|---|---|
| `item/commandExecution/requestApproval`、`item/fileChange/requestApproval` | `{"decision":"decline"}` |
| `execCommandApproval`、`applyPatchApproval`（旧接口，决定类型是 `ReviewDecision`） | `{"decision":{"denied":{"rejection":"RenderTest AI panel is read-only"}}}` |
| `item/permissions/requestApproval` | `{"permissions":{},"scope":"turn"}` |
| `mcpServer/elicitation/request` | `{"action":"decline","content":null,"_meta":null}` |
| `item/tool/requestUserInput` | M5 不支持，回复 error `-32601` |
| 其它 | error `-32601`，并在 UI 显示灰色提示 |

- **超时**：
  - `initialize`：30 秒
  - `thread/start`：60 秒
  - `turn/start` 的响应：60 秒
  - `turn/start` 超时或失败不能仅把本地回合标失败；远端执行状态可能未知。结束该进程树并废弃 thread/迟到通知，之后才恢复发送，下一问重建会话。
  - 回合本身不设总超时，用户可以随时停止
- **进程意外退出**：当前回合标为失败，显示 stderr 尾部和“重新连接”按钮。
- **找不到 codex 或 MCP Python**：面板显示配置指引和 ⚙ 入口，不弹模态框，也不影响 qrendertest 的其它功能。
- **bridge 已禁用**（`RENDERTEST_AGENT_BRIDGE=0`）：显式禁用 rendertest MCP，显示纯聊天提示，绝不自动发现其它窗口。

## 4. 文件清单

### 4.1 新增

| 文件 | 作用 | 估计行数 |
|---|---|---|
| `qrenderdoc/Code/Agent/CodexAppServerClient.{h,cpp}` | QProcess、Job Object、JSONL 分帧、请求 id 和超时、server request 的拒绝策略；对外提供 signal | 600 |
| `qrenderdoc/Code/Agent/AgentLaunchConfig.{h,cpp}` | 查找 codex、MCP Python 和 skills 目录，组装启动参数和 developerInstructions | 250 |
| `qrenderdoc/Code/Agent/AgentConversation.{h,cpp}` | thread、turn、item 的状态归约 | 400 |
| `qrenderdoc/Code/Agent/AgentMarkdown.{h,cpp}` | 最小 Markdown 转 HTML，加 EID/ResourceId 链接化 | 350 |
| `qrenderdoc/Code/Agent/AgentSettingsDialog.{h,cpp}` | 保存路径、模型、推理强度与隔离设置；动态模型目录 | 230 |
| `qrenderdoc/Code/Agent/AgentToolView.{h,cpp}` | 工具卡展开、artifact 校验、缩略图与缓存 | 450 |
| `qrenderdoc/Code/Agent/AgentAsk.{h,cpp}` | 各 viewer 调用的 `RequestAgentAsk(ICaptureContext&, AgentAskRequest)` | 80 |
| `qrenderdoc/Windows/AgentChatPanel.{h,cpp,ui}` | 停靠面板：状态行、对话视图、上下文 chip、输入框、设置对话框，并实现 `ICaptureViewer` | 1000 |
| `agent/devtools/fake_app_server.py` | 按脚本回放 JSON-RPC 的 fake app-server，并记录收到的消息 | 300 |
| `agent/devtools/chat_panel_e2e.py` | 启动 qrendertest、注入 fake 或真实 codex，并检查日志和 transcript | 300 |
| `agent/devtools/appserver_schema_check.py` | 协议回归检查 | 80 |
| `agent/devtools/appserver_skills_check.py` | 无模型的真实部署 Skill 加载验证 | 180 |

行数为初始估算；构建与验收以实际文件为准。

### 4.2 修改

| 文件 | 改动 |
|---|---|
| `qrenderdoc/Code/Agent/RenderTestAgentIntegration.{h,cpp}` | 新增 `AgentRoot()` 和 `InstanceId()`，细节见本表下方。bridge 和 Python 都不改 |
| `qrenderdoc/Code/CaptureContext.{h,cpp}` | 新增 `GetAgentChatPanel()`、`HasAgentChatPanel()`、`ShowAgentChatPanel()`；按 `agentChat` 名字恢复布局；窗口关闭时清理指针。只放在 C++ 侧，不进入 `QRDInterface.h`，不改动 Python 绑定 |
| `qrenderdoc/Windows/MainWindow.{h,cpp,ui}` | Window 菜单新增 `action_AI_Assistant` 和槽函数 |
| `qrenderdoc/Windows/EventBrowser.cpp` | 在 `events_contextMenu` 中加入“问 AI：这个事件” |
| `qrenderdoc/Windows/TextureViewer.{h,cpp}` | 在 `OpenResourceContextMenu` 中加入“问 AI：这个资源”；在 Pixel Context 区用代码加一个“问 AI”按钮，不改 `.ui`。坐标、资源和子资源复用 `GetPickedLocation()`、`GetCurrentResource()`、`GetSelectedSubresource()` 以及 `ShowPixelHistory` 里的 mip/flipY 换算 |
| `qrenderdoc/Windows/BufferViewer.cpp` | 在 `stageRowMenu` 中加入“问 AI：这个顶点” |
| `qrenderdoc/Code/Interface/PersistentConfig.h` | 新增 `Agent_CodexPath`、`Agent_McpPython`、`Agent_Model`、`Agent_ReasoningEffort`、`Agent_IsolateMcpServers`（默认 true） |
| `qrenderdoc/qrenderdoc_local.vcxproj` 和 `.filters` | 新增源文件、moc 和 uic；定义 `RENDERTEST_AGENT_SOURCE_DIR`；`DeployRenderTestAgentBridge` 额外把 `.agents/skills/rendertest-*` 复制到 `integrations/rendertest_agent/skills/` 和同级 `.agents/skills/` |
| `qrenderdoc/CMakeLists.txt`、`qrenderdoc/qrenderdoc.pro` | 同步新增源文件和 skills 部署 |
| `.agents/skills/rendertest-core/SKILL.md` | 补一条规则：存在 `<rendertest_context>` 时直接使用其中的 instanceId 和 selection，不再询问用户 |

`RenderTestAgentIntegration` 新增的两个函数：

- **`AgentRoot()`**：规则与 `paths.agent_root` 相同，`RENDERTEST_AGENT_ROOT` 优先，否则为 `%LOCALAPPDATA%/RenderTest/agent`。
- **`InstanceId()`**：扫描 `ipc-v1/instances/*/instance.json`，选 `pid == applicationPid()` 且 `stopped == false` 的条目；有多个时取 `startedAtUnixMs` 最大的一个。结果缓存，`Shutdown` 时清空。

不改用“C++ 生成 id 再用 `qputenv` 传给 bridge”。原因是嵌入的 Python 在 `PythonContext::GlobalInit` 时就快照了环境变量，而 `CaptureContext` 是在这之后才构造的，晚设的 env 对 `os.environ` 不可见，已实测。manifest 本来就是 bridge 对外的契约，C++ 读它最直接。

“问 AI”入口不走 Extension 的 `RegisterContextMenu`。原因是它的回调参数是 `PyObject*`，面向 Python 扩展，C++ 面板接入反而绕路。现有的 `MenuDisplaying` 调用保持不动。

`RequestAgentAsk` 内部把 `ICaptureContext&` 转为 `CaptureContext&`（qrenderdoc 里只有这一个实现），这个转换只集中在这一处。

## 5. 分阶段

| 阶段 | 内容 | 完成标志 |
|---|---|---|
| M5a | app-server 客户端、面板骨架、纯问答流式输出、单行工具卡、停止、各类错误状态、实例绑定 | fake 场景 A1–A6 通过；面板里实机跑 Q1 通过 |
| M5b | 上下文 chip、4 个“问 AI”入口、EID/ResourceId 链接 | 实机 Q2 和 Q3 从右键入口发起并通过；点击链接能跳转 |
| M5c | 工具卡展开、artifact 缩略图、模型和推理强度选择、登录流程、设置对话框、skills 部署 | 离开仓库目录运行也能加载 Skill；能在未登录状态下完成登录 |
| M5d | 非 ephemeral thread、本地对话索引、历史列表、`thread/resume`，capture 不匹配时只读 | 重启 qrendertest 后能继续追问同一 capture |

每个阶段完成后单独提交，沿用 M0–M4 的节奏。

当前 M5a–M5c 已实现，M5d 尚未开始。M5c 已覆盖 fake 登录、模型目录和工具卡回归、真实 app-server 的部署 Skill 加载；真实浏览器授权尚待人工验收，不把 fake 成功等同于实际登录成功。

2026-11-06 验证：MSBuild Release x64 成功；39 个 fake 界面场景通过；Python 测试 141 通过、2 个符号链接测试跳过；协议检查和 3 种部署 Skill 加载检查通过。已检查设置框与展开工具卡截图。CMake/qmake 同步清单，未在本次实际构建。

## 6. 验收方式

### A. 协议和状态归约（无 GPU，可自动化）

`fake_app_server.py --scenario <name> --log <jsonl>` 按脚本回复。启动 qrendertest 时用 `RENDERTEST_AGENT_CODEX_COMMAND` 把面板指向它。

测试钩子 `RENDERTEST_AGENT_CHAT_AUTOTEST=<json>`，env 未设置时完全不生效：

- 面板自动打开，按脚本发送问题或点击停止；
- 回合结束后把 `AgentConversation` 导出到 transcript JSON；
- 测试结束后退出。

| 场景 | 断言 |
|---|---|
| A1 正常流式 | 请求顺序为 initialize → initialized → account/read → thread/start → turn/start；`thread/start` 带 read-only、never 和 developerInstructions，且其中有本实例 id；transcript 里 agentMessage 等于 delta 拼接的结果 |
| A2 工具卡 | `mcpToolCall` 从 started 到 completed，transcript 中有工具名、durationMs 和状态 |
| A3 审批 | fake 发出 commandExecution、fileChange、permissions、elicitation 四种 server request，日志里收到的都是 decline 或空授权；回合不卡住 |
| A4 停止 | fake 在收到 `turn/interrupt` 后才回复 interrupted，UI 在 2 秒内回到空闲；fake 故意不回复时，进程树在 2 秒内被结束 |
| A5 崩溃 | fake 在回合中途 exit(1)，UI 显示失败；下一次发送会重启并新建 thread |
| A6 未登录和缺失 | fake 返回 `account:null` 时 UI 显示未登录；codex 路径无效时 UI 显示指引，qrendertest 不崩溃 |
| A7 归约回放 | 把真实 app-server 的 JSONL 录制下来，回放后得到的 transcript 与快照一致 |

边界回归还必须覆盖：配置读取/状态查询失败时不发送、状态分页后发现其它 server、重复分页游标、关闭 MCP 隔离后 shell 仍禁用、`turn/start` 响应超时后进程重建、结束后的迟到 item 被忽略。

跨层回归：首次工具前和工具中切换 capture、同 RDC 重开、旧链接失效、固定实例不能覆盖、bridge 禁用时不连接其它实例。像素覆盖 UInt/SInt 类型解释和独立缓存/游标、非法平均 sample；artifact 覆盖分页/搜索、越界路径、旧 session、内容被修改和文件预算。

M5c 回归覆盖：登录成功后重新发送、失败可恢复、取消后迟到成功通知不重启会话；未登录再次发送只重读账户、不重复 initialize；模型分页与 `.model`/`.id` 区分、自定义 provider 字符串、目录查询失败不阻止手填；工具卡展开、截断后的 artifact 元数据、合法 PNG 和越界/损坏/缺失图片。

另外：

- `InstanceId()` 的 manifest 扫描规则写成 `AgentLaunchConfig` 可测的纯函数：输入目录列表和 pid，输出 id。用 fake 目录覆盖多实例、stopped 和同 pid 残留三种情况。
- `appserver_schema_check.py` 必须通过。

### B. 构建和加载

- MSBuild Release x64 构建成功。CMake 和 `.pro` 的文件清单同步更新（只做清单检查，不实际构建）。
- 部署后存在 `integrations/rendertest_agent/skills/rendertest-*/SKILL.md` 和 `integrations/rendertest_agent/.agents/skills/rendertest-*/SKILL.md`，用 `appserver_skills_check.py` 验证两种发现方式。
- 以下情况下 qrendertest 都能正常启动，且面板在没有 capture 时可以打开：
  - 没有安装 codex；
  - 设置了 `RENDERTEST_AGENT_BRIDGE=0`；
  - MCP Python 不存在。

### C. 实机（nanite capture，用户已同意发送给模型）

| 用例 | 操作 | 期望 |
|---|---|---|
| Q1 | 选中 EID 1513，问“这个 pass 在做什么” | 使用 inspect_event 等工具；回答中的 EID 可以点击 |
| Q2 | Texture Viewer 选 RT 116618 的像素 (256,256)，点“问 AI：这个像素” | 工具链为 pick_pixel → pixel_history；结论指向 EID 1459 的 discard |
| Q3 | 像素 (1,1) 发起提问 | 结论包含 primitive 30161 |
| 停止 | Q2 运行到 pixel_history 时点停止 | 2 秒内回到空闲；下一问可以正常进行 |
| 响应性 | 工具执行期间拖动 Texture Viewer、切换 EID | UI 不卡顿；Agent 的工具调用仍使用快照里的 eventId |
| UI 不被篡改 | 记录 Q1–Q3 前后的当前 EID | 没有点击链接时 EID 不变 |
| 实例隔离 | 同时开两个 qrendertest，打开不同的 capture，各自提问 | 各自面板的 `get_context` 返回自己的 capture |
| 退出清理 | 回合进行中关闭 qrendertest | 3 秒内没有残留的 codex.exe 或 rendertest_mcp python 进程 |

C 组由 `chat_panel_e2e.py --real` 半自动执行：脚本启动 qrendertest 并加载 capture，通过 autotest 钩子发送问题，再从 transcript 中断言工具序列和关键数字。截图和人工确认作为补充。

## 7. 风险

| 风险 | 应对 |
|---|---|
| app-server 标注为 experimental，协议可能变化 | 只用文中列出的 method；用 schema 检查脚本做回归；未知通知一律忽略 |
| 隔离依赖 feature 名称，codex 升级后可能改名 | schema 检查脚本额外运行 `codex features list`，确认第 3.2 节的 feature 都存在；每次新建 thread 都用 `mcpServerStatus/list {threadId}` 复核 |
| 隔离后仍有内置 `apply_patch` | read-only 沙箱加 `never` 审批策略，无法写盘；验收 A3 和 C 组检查 transcript 里没有 `fileChange` 成功项 |
| reasoning delta 数量大（单回合 1600 多条） | 只在折叠的摘要里累加，按 50ms 节流刷新，不逐条触发重排 |
| `skills/extraRoots/set` 是 experimental | 退回到依赖 cwd 下的 `.agents/skills` 部署 |
| Qt 5.9 没有 Markdown 支持 | 自带最小实现，复杂表格降级为代码块 |
| 外部 Codex CLI 和面板同时操作同一实例 | mailbox 已经串行处理请求；`set_event` 冲突由用户自行负责，文档注明 |
| 用户的 `config.toml` 已有同名 `rendertest` server | `-c` 覆盖启动参数和固定实例/session；启动后校验状态，无法确认时阻止工具回合 |

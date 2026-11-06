# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M4 和 M5a–M5d 已实现**，支持本地历史保存与跨进程恢复。登录状态流通过 fake app-server 验证，真实浏览器授权仍需人工验收。详见 [M5 设计](docs/M5_DESIGN.md)。

- M0：MCP Server、文件 mailbox IPC、fake Bridge 和协议测试。
- M1：qrendertest 启动时从 `integrations/rendertest_agent/python` 固定加载 Bridge，写实例清单和心跳，跟踪 capture 加载、关闭和选中事件，支持 `ping` / `get_status` / `get_context`。
- M2：接入 replay，提供全部 P0 工具。
- M3：4 个 Skills（`.agents/skills/rendertest-*`）、Codex 配置脚本、端到端验收脚本；MCP 工具按效果标注 `readOnlyHint`。
- M4：`pixel_history`、`debug_pixel`、`debug_vertex`、`get_mesh_data`。

## P0 工具

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context`（含当前 EID 的 Pipeline 摘要） |
| Action | `list_actions`、`find_actions`、`inspect_event`、`set_event`（唯一会改变 UI 的工具） |
| Pipeline | `get_pipeline_state`、`get_shader`（反射、反汇编，超过 256 KiB 走 artifact）、`get_cbuffer` |
| 资源 | `list_resources`、`get_resource`、`get_resource_usage` |
| 纹理 | `pick_pixel`、`get_texture_stats`、`export_texture`（png/exr/dds；depth PNG 自动按 min/max 拉伸；`channels="rgb"` 输出不透明 RGB PNG） |
| Buffer | `get_buffer_data`（64 KiB 内 base64，1 KiB 内附 hex，更大写 artifact；`format="float4, uint"` 可解码行） |
| 消息 | `get_debug_messages` |
| 文本 artifact | `read_artifact`（MCP 本地只读，不经 replay） |

## P1 工具（M4）

| 工具 | 说明 |
| --- | --- |
| `pixel_history` | 截至目标 EID 修改过某个像素的事件，默认新到旧，按事件分组：片元数、通过数、失败原因计数（深度/模板/discard/裁剪…）、通过片元的 primitive、`preMod`/`shaderOut`/`postMod`（color/depth/stencil）。`summary` 给出最后通过和最后改变值的 EID；`include_fragments=true` 每个事件附最多 64 个片元。RenderDoc 的像素历史不含 `Discard`，这类事件放在 `discardEventIds` 里并给出 warning。结果按 capture session 缓存，翻页不会重放。 |
| `debug_pixel` | 用 RenderDoc Shader 调试器跑某个 Draw 在某像素的 Pixel Shader（`primitive` 选重叠片元，可取自 `pixel_history` 的 `primitiveIds`）。返回 Shader、插值输入、最终输出（`SV_Target*`，混合和格式转换之前）、最后改变的变量、产生 NaN/Inf 的步骤（指令和行号）；完整逐步 trace 写成 JSONL artifact（每步 `instruction`、`line`、变量 `changes`，上限 64 MiB）。`max_steps` 默认 20000。没有片元覆盖该像素时返回 `debugged=false`。 |
| `debug_vertex` | 调试某个 Draw 的某个顶点的 Vertex Shader。`vertex` 是 Draw 内 0 起的序号（Mesh Viewer 的 VTX 列，小于 `numIndices`），`instance` 默认 0。Indexed Draw 由 Bridge 读 index buffer，按 Mesh Viewer 的算法加 base vertex 得到 `index`（另返回原始 `indexBufferValue`，遇到 primitive restart 报错）。返回字段与 `debug_pixel` 相同，`outputs` 含 `SV_Position`（裁剪空间）。 |
| `get_mesh_data` | 读 Draw 变换后的顶点，对应 Mesh Viewer 的 VS Out（`stage="vs_out"`，默认）/ GS Out（`stage="gs_out"`）表。返回输出列（位置在前，布局与 Mesh Viewer 相同）、全 Draw 的每列 min/max 和非有限值行数、位置的 NDC 范围和 w≤0 的顶点数；`rows` 每行含 `vertex`、`index`（实际取的顶点，已加 base vertex）和各列值，用 `start`/`limit` 或 `cursor` 翻页（默认 50 行）。一次回放后按 capture session 缓存。暂不支持 Mesh Shader。 |

约定：

- 带 `event_id` 的只读工具临时切 replay EID，读完恢复，不改变 UI 选中事件；省略时用当前 EID。
- 列表工具都分页：`limit` 1–1000（默认 200），`nextCursor` 绑定 capture session 和查询条件。
- 绑定条目里 `name` 是 Shader 反射中的绑定名，`resourceName` 是资源名。
- artifact 写到 `<root>/artifacts/<instanceId>/<captureSessionId>/replay/`，结果里带路径、大小和 SHA-256。
- `pick_pixel` / `pixel_history` 支持 `type_cast`（RenderDoc 枚举名，如 `UInt`、`SInt`、`Depth`、`UNormSRGB`），返回的值和解释与此参数对应；历史缓存和游标包含该参数。Vulkan PixelHistory 的底层驱动忽略不同于原格式的 cast，此时返回 `UNSUPPORTED`，可用 `Typeless` 查看原始格式。
- `read_artifact(path, start_line=1, limit=100, search=None, max_bytes=65536)` 读取同一 MCP 进程此前工具返回的文本 artifact，支持按行分页和字面搜索，续页使用 `data.nextLine`。文件上限 64 MiB，单页上限 128 KiB；每次校验路径、实例/session、大小和完整 SHA-256，不接受任意文件、符号链接或 junction。重启 MCP 后需重新调用生成 artifact 的工具。

## 目录

```text
agent/
  bridge/rendertest_agent_bridge/   # qrenderdoc 内嵌 Bridge，保持 Python 3.6 兼容
  mcp_server/src/rendertest_mcp/    # 外部 stdio MCP Server，Python >= 3.10
  devtools/fake_bridge.py           # 不依赖 qrenderdoc 的 Bridge 模拟器
  devtools/smoke_live.py            # 对运行中的 qrendertest 冒烟；--all-p0 跑全部 P0 工具
  devtools/codex_config.py          # 输出 Codex 的 MCP 配置片段（toml 或 -c 参数）
  devtools/codex_e2e.py             # codex exec 跑 3 个端到端验收问题
  devtools/live_lifecycle.py        # 启动 qrendertest，自动验收加载/选中/关闭/重载/退出
  devtools/ui_lifecycle_script.py   # live_lifecycle.py 使用的 --ui-script
  devtools/fake_app_server.py       # 按场景回放的 fake codex app-server，测试 AI 面板
  devtools/chat_panel_e2e.py        # AI 面板验收：fake 场景 A1–A6，或 --real 接真实 codex
  devtools/appserver_schema_check.py # 校验本机 Codex 协议和 feature 开关，不调用模型
  devtools/appserver_skills_check.py # 真实 app-server 验证部署后的 Skill 加载，不调用模型
  devtools/appserver_history_check.py # 真实 app-server + localhost fixture 验证历史恢复
  tests/native/                     # Qt 原生历史存储、状态恢复和 capture 身份测试
  tests/                            # 协议、实例选择、工具契约和 stdio 冒烟测试
  docs/MVP_DESIGN.md
```

仓库根目录的 `.agents/skills/` 下有 4 个 Skill，在本仓库里运行 Codex 时会被发现。Skill 正文用英文（模型仍按用户语言回答）：Codex 在 Windows PowerShell 下读无 BOM 的中文 UTF-8 文件会乱码。

- `rendertest-core`：目标选择、capture/session 身份、工具效果、实测与推断的区分。
- `rendertest-event-analysis`：解释 EID/pass 的输入、计算和输出。
- `rendertest-shader-debug`：Shader 反射、绑定、CBuffer 和反汇编，`debug_pixel` / `debug_vertex` 单步调试。
- `rendertest-pixel-investigation`：像素取值、用 `pixel_history` 找最后改变它的事件和失败原因，必要时 `debug_pixel` 看 Shader 计算；不支持时退回资源用途比较。

## 环境

```powershell
cd agent/mcp_server
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

## 测试

```powershell
cd agent/mcp_server
.\.venv\Scripts\python.exe -m pytest --basetemp=..\.pytest-tmp
```

测试会创建 asyncio 事件循环和 stdio 子进程，受限沙箱中可能卡在 loopback socketpair 上，需要在普通终端运行。

## 在 qrendertest 中使用

构建 `qrenderdoc_local.vcxproj` 后，Bridge 会被复制到 `x64/<Config>/integrations/rendertest_agent/python/`。启动 qrendertest 即自动运行，不出现在 Extension Manager 中。

- `RENDERTEST_AGENT_BRIDGE=0`（或 `false` / `off` / `no`）：不加载 Bridge。
- `RENDERTEST_AGENT_ROOT`：IPC 和日志根目录，Bridge 日志在 `<root>/logs/bridge-<instanceId>.log`。

实测：

```powershell
# 对已经打开的 qrendertest
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --expect-capture

# 启动 qrendertest 打开 capture，跑全部 P0 工具后退出
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --all-p0 --launch "E:\Captures\some.rdc"
# 额外检查指定像素的 pixel_history（可重复）
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --all-p0 --launch "E:\Captures\some.rdc" --pixel-history 116618:1513:1:1
# 额外对指定像素跑 debug_pixel（自动取 pixel_history 里通过的 primitive，可重复）
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --all-p0 --launch "E:\Captures\some.rdc" --debug-pixel 116618:1513:1:1
# 额外对指定顶点跑 debug_vertex（EID:VERTEX[:INSTANCE]，用 get_buffer_data 读 index buffer 交叉验证 index，可重复）
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --all-p0 --launch "E:\Captures\some.rdc" --debug-vertex 1513:0
# 额外读 get_mesh_data，并逐个和 debug_vertex 的 index、位置比较（EID[:VERTEX,...]，可重复）
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --all-p0 --launch "E:\Captures\some.rdc" --mesh-data 1513:0,5,100

# 自动跑完整生命周期
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\live_lifecycle.py --capture "E:\Captures\some.rdc"
```

## 接入 Codex

生成配置片段，追加到 `~/.codex/config.toml`：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\codex_config.py
```

不传 `--root` 时 Bridge 和 MCP Server 都用 `%LOCALAPPDATA%/RenderTest/agent`，直接对应正常启动的 qrendertest。之后在本仓库目录里启动 Codex，用 qrendertest 打开 capture，就可以问“解释当前 EID 在做什么”。

端到端验收（会把 capture 中的名称、数值和路径发给 Codex 配置的模型服务）：

```powershell
# 只计算基准答案，不调用模型
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\codex_e2e.py --launch "E:\Captures\some.rdc" --dry-run
# 真正提问；回答和事件流保存在 agent/.agent-data/e2e/<时间>/
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\codex_e2e.py --launch "E:\Captures\some.rdc"
```

## qrendertest 内的 AI 面板（M5a–M5d）

菜单 `Window → AI Assistant` 打开可停靠面板。第一次发送消息时，面板启动 `codex app-server`，并把 rendertest MCP 绑定到本窗口的 Bridge 实例；不需要改 `~/.codex/config.toml`。

- 前提：已经安装 Codex CLI（`npm install -g @openai/codex`）；`agent/mcp_server/.venv` 已创建。可沿用已有 Codex 登录，或在未登录提示出现后点面板的 `Log in`，在浏览器完成登录后重新发送问题；等待时可点 `Cancel login`。
- 设置：点 `Settings` 配置原生 codex 可执行文件、MCP Python、模型、推理强度和隔离开关；设置保存到 RenderTest 配置文件，下一条问题重新建立会话。留空沿用自动查找或 Codex 配置。模型列表由 app-server 分页获取；自定义 provider 可以手填模型与推理强度。
- 只读：thread 使用 `read-only` 沙箱和 `never` 审批，所有审批请求都会被拒绝；shell 和 web search 始终关闭，其它 MCP server 默认隔离。隔离检查失败会阻止对话。
- 交互：Enter 发送，Shift+Enter 换行。“Stop”先发 interrupt，2 秒内没有结束就结束进程树，下一条消息重新连接并恢复 thread。“New Chat”保存当前对话后清空面板，下一问创建独立 thread。
- 历史：点 `History` 搜索并打开已保存对话；消息在本地显示，发送新问题时才启动 Codex 并恢复上下文。历史位于 `<agent root>/conversations/<UUID>.json`，流式更新每 2 秒保存，回合结束和关闭时立即保存；使用原子写入和会话锁。另一窗口正在使用的记录可只读查看。
- 续聊要求：本地已保存的 capture 必须匹配规范路径、大小、修改时间和完整 SHA-256；校验在后台完成。不同、远程、临时或缺失的 capture 只能查看历史；纯聊天仅在没有 capture 时恢复。旧消息的 EID/资源跳转链接保持失效，新回复可正常跳转。Codex rollout 不存在或恢复校验失败时保留只读历史，可以 `New Chat` 重新开始。M5a–M5c 的旧 ephemeral thread 不会自动迁移。
- 每条消息前会附上 `<rendertest_context>`（实例 id、capture、当前 EID），这是发送时的快照。
- MCP 固定绑定发送时的实例和 capture session，包括 `get_context` / `get_status` 都不能重新绑定。capture 加载/关闭会停止旧会话并将它保存为只读历史；新建对话或从 `History` 打开匹配记录后继续。恢复时重新设置当前实例/session 并检查隔离。没有可验证的 capture 或 bridge 时禁用 rendertest MCP，允许纯聊天，不会自动连接其它窗口。
- 上下文 chip：输入框上方显示本条消息会带上的上下文。“Current EID”默认附带，点 × 去掉，点 “+ Current EID” 加回；其它 chip 发送后清空，并显示在用户气泡下方。每个 chip 以 `selection: {json}` 写进 `<rendertest_context>`，字段名和 MCP 工具参数一致（`event_id`、`resource_id`、`x`/`y`（左上原点）、`mip`、`slice`、`sample`、`vertex`、`instance`、`view`）。
- “问 AI”入口（只添加 chip 并预填可编辑的问题，不自动发送）：
  - Event Browser 右键 → `Ask AI about this event`
  - Texture Viewer 缩略图右键 → `Ask AI about this resource`
  - Texture Viewer 的 Pixel Context 面板 → `Ask AI`（使用资源值和当前类型解释，坐标换算和 Pixel History 一致；MSAA Average val 模式需先选择具体 sample，显示 Shader/overlay 的值不作为资源值）
  - Mesh Viewer VS In / VS Out 表格右键 → `Ask AI about this Vertex`
- 回答里的 `EID 1459`、`event 1459`、`ResourceId::116618` 会变成链接：点击 EID 跳到该事件；点击资源时纹理在 Texture Viewer 打开，缓冲在 Buffer Viewer 打开，其它资源在 Resource Inspector 打开。
- 工具卡默认折叠，点击展开参数、错误和结果；参数最多 8 KiB、结果最多 4 KiB（UTF-8）。完整结果里的 PNG artifact 会独立提取为最多 256×256 缩略图，点击用系统查看器打开。图片必须属于工具响应中的实例/session 目录，并通过文件大小、SHA-256、PNG 格式和路径校验；拒绝越界、符号链接/junction、超过 8 MiB、单边超过 8192 或超过 16 MP 的图片。图片不可用时显示原因。
- 4 个 Skills 随构建部署到 `integrations/rendertest_agent/skills/`，并在 `.agents/skills/` 保留 cwd 自动发现副本；不依赖从仓库目录启动。
- 日志：`<agent root>/logs/chat-<instanceId>.log`，包含启动参数和 app-server 的 stderr 尾部。

环境变量：

- `RENDERTEST_AGENT_CODEX_COMMAND`：替换 codex 命令（按空格拆分，支持引号），优先于设置；未配置时查找 npm 安装的 `codex.exe`，再查 PATH。
- `RENDERTEST_MCP_PYTHON`：MCP Server 使用的 Python，优先于设置；未配置时查找 exe 上层目录中的 `agent/mcp_server/.venv/Scripts/python.exe`。
- `RENDERTEST_AGENT_MODEL` / `RENDERTEST_AGENT_REASONING_EFFORT`：覆盖保存的模型和推理强度，分别传给 `thread/start.model` 和 `turn/start.effort`。
- `RENDERTEST_AGENT_ISOLATE=0`：不关闭用户其它 Codex feature 和 MCP server；shell 与 web search 仍然禁用。
- `RENDERTEST_AGENT_FIXED_INSTANCE_ID` / `RENDERTEST_AGENT_CAPTURE_SESSION_ID`：面板为其 MCP 子进程设置的强制绑定，必须成对有效；不同实例参数或 capture session 会被拒绝。外部 CLI 不设置这两个变量时仍可按原规则选择实例。
- `RENDERTEST_AGENT_CHAT_AUTOTEST`：测试钩子（JSON 或 JSON 文件路径），由 `chat_panel_e2e.py` 使用。

验收：

```powershell
# fake app-server，不调用模型
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py
# 本机 Codex 协议与部署检查，不调用模型或登录流程
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\appserver_schema_check.py
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\appserver_skills_check.py
# 真实 Codex + 仅 localhost 的 Responses fixture：验证跨进程恢复包含上一问答
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\appserver_history_check.py
# M5d fake 回归；真实 capture + fake server，不调用模型
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py --scenario M5d
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py --history-capture --capture "E:\Captures\some.rdc"
# 真实 codex：选中 EID 后提问、第二问中途 Stop、退出后检查进程树（会把 capture 数据发给模型）
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py --real --capture "E:\Captures\some.rdc" --event 1513
# 真实 codex：通过 Texture Viewer 的 Ask AI 像素入口问 Q2 (256,256) / Q3 (1,1)，再点击回答里的第一个 EID 链接
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py --real --ask-pixel --capture "E:\Captures\some.rdc" --event 1513
```

Qt 原生测试在 `agent/tests/native/`：分别用 Qt 5.9+ 的 `qmake` 构建 `agent_history_test.pro` 和 `agent_conversation_identity_test.pro`，运行生成的可执行文件。覆盖原子保存、锁竞争/崩溃恢复、坏文件/大小限制、完整摘要、短路径以及消息恢复。建议在独立构建目录中运行。

## 用 fake Bridge 手动试用

终端 1：

```powershell
cd agent
$env:RENDERTEST_AGENT_ROOT = "$PWD\.agent-data"
.\mcp_server\.venv\Scripts\python.exe devtools\fake_bridge.py
```

终端 2 中生成指向同一根目录的配置，写进 `~/.codex/config.toml`：

```powershell
.\mcp_server\.venv\Scripts\python.exe devtools\codex_config.py --root .agent-data
```

不设置 `RENDERTEST_AGENT_ROOT` 时，Bridge 和 MCP Server 都使用 `%LOCALAPPDATA%/RenderTest/agent`。存在多个在线实例时，可以用 `RENDERTEST_AGENT_INSTANCE_ID` 或工具参数 `instance_id` 指定目标。

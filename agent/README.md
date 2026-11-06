# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M4 和 M5a–M5d 已实现**，支持本地历史保存与跨进程恢复。登录状态流通过 fake app-server 验证，真实浏览器授权仍需人工验收。详见 [M5 设计](docs/M5_DESIGN.md)。

- M0：MCP Server、文件 mailbox IPC、fake Bridge 和协议测试。
- M1：qrendertest 启动时从 `integrations/rendertest_agent/python` 固定加载 Bridge，写实例清单和心跳，跟踪 capture 加载、关闭和选中事件，支持 `ping` / `get_status` / `get_context`。
- M2：接入 replay，提供全部 P0 工具。
- M3：4 个 Skills（`.agents/skills/rendertest-*`）、Codex 配置脚本、端到端验收脚本；MCP 工具按效果标注 `readOnlyHint`。
- M4：`pixel_history`、`debug_pixel`、`debug_vertex`、`get_mesh_data`。
- 分析工具扩展：Shader 内嵌源码读取、资源大小统计、事件统计、CBuffer 成员查询和单 EID Overlay；当前共 27 个 MCP 工具，Codex 和 Claude 共用。

## P0 工具

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context`（含当前 EID 的 Pipeline 摘要） |
| Action | `list_actions`、`find_actions`、`inspect_event`、`get_action_stats`、`set_event`（唯一会改变 UI 的工具） |
| Pipeline | `get_pipeline_state`、`get_shader`（反射、反汇编，超过 256 KiB 走 artifact）、`get_shader_source`（capture 内嵌源码目录与文件）、`get_cbuffer`（支持成员路径和子成员分页） |
| 资源 | `list_resources`、`get_resource`、`get_resource_usage`、`get_resource_stats` |
| 纹理 | `pick_pixel`、`get_texture_stats`、`export_texture`（png/exr/dds；depth PNG 自动按 min/max 拉伸；`channels="rgb"` 输出不透明 RGB PNG） |
| 诊断图 | `export_overlay`（单 EID 的离屏 Overlay PNG，不改变 UI） |
| Buffer | `get_buffer_data`（64 KiB 内 base64，1 KiB 内附 hex，更大写 artifact；`format="float4, uint"` 可解码行） |
| 消息 | `get_debug_messages` |
| 文本 artifact | `read_artifact`（MCP 本地只读，不经 replay） |

新增分析接口：

- `get_shader_source(stage, event_id=None, file_index=None, cursor=None, limit=100)`：省略 `file_index` 返回完整内嵌源码目录，可分页；指定索引导出 UTF-8 `sourceArtifact`，用 `read_artifact` 分页或搜索。文件名仅作标识，不从宿主机读取同名文件；重名文件按索引区分。没有嵌入源码时明确返回 `available=false`，不会把反汇编冒充源码。单文件上限 64 MiB，空文件和 BOM 原样保留，非法 UTF-8 或含 NUL 的内容明确不可用。单行超过文本工具 128 KiB JSON 预算时，源码仍完整导出，但 `readArtifactCompatible=false` 和 warning 会说明该行不能分页读取。
- `get_resource_stats(kind="all", top_n=20)`：汇总 texture/buffer 数量、`knownTotalBytes`，按类型/纹理格式/维度类型分组，返回最大的资源。`all` 只包括纹理和缓冲；大小来自 texture `byteSize` 和 buffer `length`，不表示驻留、峰值或去重后的实际显存分配。未知大小计入 `unknownSizeCount`，不混成 0，也不参与排名。分类最多 256 组、Top N 最多 1000，省略内容显式标记。
- `get_action_stats(parent_event_id=None, cursor=None, limit=200, top_n=20)`：整帧或指定 action/marker 子树的事件类型计数，按直接子节点分组分页；总数与排名每页都覆盖完整范围。只计操作叶节点，容器与 marker 单独计数，排除父节点操作标志时给 warning；mesh/ray dispatch 单独分类。Draw 排名依据 `numIndices * numInstances`，普通 compute dispatch 按组数排名；这些是记录的工作量估计，不是三角形数、唯一 VS 调用数或 GPU 耗时。

三个接口都保持当前 UI EID。源码导出标记为 `artifact`，另外两个为 `read`。例如可以直接问：“列出当前 Pixel Shader 的源码文件并解释主函数”、“统计这一帧的纹理和 Buffer，列出最大的 10 个资源”、“统计 EID 123 所在 pass 的 draw/dispatch，并找出工作量最大的 draw”。

第二批分析接口：

- `get_cbuffer` 新增可选 `member_path`、`cursor`、`limit` 和 `max_depth`。不传这些参数时保留旧摘要。`member_path=""` 分页浏览根成员；`member_path="Lights[123].Color"` 精确选择成员。返回的 `canonicalPath` 如 `[0][123][1]` 按直接子节点序号定位，支持重复名称和特殊名称，也可用 JSON 引号路径如 `["Light.Data"][123]["Color"]`。`selected` 是选中节点（标量值在 `selected.value`），`variables` 是其直接子成员的当前页，`childCount` 为子成员总数。`limit` 默认 200、上限 1000；`max_depth` 默认 0，仅返回浅层摘要，1–4 额外展开后代，仍受 4096 节点和 256 KiB 响应预算约束。按 `nextCursor` 翻同级，或复制子成员路径继续查询。游标绑定 capture、EID、Shader、CBuffer 绑定、成员和展开深度。`byteOffset` 相对常量块，实际 buffer 偏移还需加 `binding.byteOffset`；没有反射偏移时不猜值。此功能限制响应规模，原生 API 仍解码完整 block。
- `export_overlay(resource_id, overlay, event_id=None, mip=0, slice=0, sample=0, type_cast="Typeless", range_min=0, range_max=1, max_dimension=1024, include_image=false)`：创建临时 headless ReplayOutput，导出与纹理显示路径一致的诊断合成 PNG。支持 `Drawcall`、`Wireframe`、`Depth`、`Stencil`、`BackfaceCull`、`ViewportScissor`、`QuadOverdrawDraw`、`TriangleSizeDraw`、`NaN`、`Clipping`。Draw 类模式要求实际 raster draw 和当时绑定的目标子资源；NaN/Clipping 可检查普通 2D 纹理。暂不支持 Mesh Shader draw、MSAA Overdraw、pass 级或 ClearBefore 模式。
- Overlay 是 `artifact` 操作，不移动 UI 选中事件，也不修改 Texture Viewer 设置；输出始终在 `finally` 释放，并强制恢复共享诊断资源。图像是 8-bit RGB 可视化，默认最长边 1024，`max_dimension` 上限 4096，并受 2M 输出像素预算约束。源纹理及关联深度纹理限制为 16M 存储 texel（包括 mip/slice/sample），超限或 API 不支持时明确报错。结果提供 `legend`、原始/输出尺寸及 `displayTransform`；不要把缩放后的坐标、诊断色或 Overdraw 热图当作原始像素值、精确写入次数或 GPU 耗时。需要实际看图时指定 `include_image=true`。

例如：“读取当前 Pixel Shader 的 Lights 数组第 123 项 Color”、“分页查看这个常量缓冲后面的成员”、“导出当前 Draw 的深度测试和背面剔除 Overlay，解释哪些区域被挡住”。Shader 反编译工具接入另行安排，优先评估可用的 release 二进制。

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
- `rendertest-event-analysis`：解释 EID/pass 的输入、计算和输出，统计 frame/pass 事件及纹理/缓冲大小。
- `rendertest-shader-debug`：Shader 内嵌源码、反射、绑定、CBuffer 和反汇编，`debug_pixel` / `debug_vertex` 单步调试。
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
# 只验证新增分析工具，明确选择 Draw EID，结束后退出独立进程
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --analysis-only --launch "E:\Captures\some.rdc" --event-id 3820
# 验证 CBuffer 成员读取、分页和诊断 Overlay PNG，结束后退出独立进程
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --second-batch --launch "E:\Captures\some.rdc" --event-id 3820
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

菜单 `Window → AI Assistant` 打开可停靠面板。在 `Settings → Backend` 选择 Codex 或 Claude；第一次发送消息时启动所选 harness，并把 rendertest MCP 固定绑定到当前窗口的 Bridge 实例。

- Codex 前提：安装 Codex CLI（`npm install -g @openai/codex`）并创建 `agent/mcp_server/.venv`。可沿用已有 Codex 登录，或在未登录提示出现后点面板的 `Log in`；浏览器完成登录后重新发送问题，等待时可点 `Cancel login`。
- Claude 前提：用面板所选的 Python 安装可选依赖，在仓库根目录运行 `agent\mcp_server\.venv\Scripts\python.exe -m pip install -e "agent/mcp_server[claude]"`。`Claude executable` 留空使用 SDK 默认 runtime，也可指定原生 `claude.exe`。认证由 Claude 管理：可使用其支持的 API 环境配置，或在原生 CLI 中执行 `claude auth login`。适配器也读取用户级 Claude `settings.json` 的连接环境白名单（认证、API 地址和模型路由/发现），尊重 `CLAUDE_CONFIG_DIR`，已有进程环境优先；不会加载项目配置、hooks、插件或执行 `apiKeyHelper`。RenderTest 不保存 API key，也不提供 Claude 的浏览器登录按钮。进程环境凭据改变后需重启 RenderTest；用户设置中的连接字段在下次建立连接时重新读取。
- 设置：Codex 和 Claude 分别保存模型、推理强度和可执行文件路径；切换后端会保存旧对话并开始新对话。打开设置即可独立加载所选后端的模型目录，显示加载、失败和空目录状态，支持 `Refresh` 和手填模型。Codex 目录查询不依赖 MCP Python；Claude 目录查询使用已安装 SDK 的 Python。加载完成会保留输入，已列出的模型会校验支持的推理强度。
- 工具权限：Codex 使用 `read-only` 沙箱和 `never` 审批，shell/web search 关闭，其它 MCP 默认隔离。Claude 使用 SDK 的空内置工具集、严格 MCP 配置及工具白名单，只开放绑定的 rendertest 工具；关闭用户/项目配置、插件、hook、shell、文件和 web 工具，不能关闭隔离。Claude 的这套工具限制不等同于 Codex 的操作系统沙箱。两种后端均需确认权限和 MCP 状态后才发送问题。
- 交互：Enter 发送，Shift+Enter 换行。“Stop”先发 interrupt，2 秒内没有结束就结束进程树，下一条消息重新连接并恢复 thread。“New Chat”保存当前对话后清空面板，下一问创建独立 thread。
- 历史：点 `History` 搜索并打开已保存对话；消息在本地显示，发送新问题时才启动原后端并恢复上下文。历史位于 `<agent root>/conversations/<UUID>.json`，流式更新每 2 秒保存，回合结束和关闭时立即保存；使用原子写入和会话锁。另一窗口正在使用的记录可只读查看。列表显示后端，跨后端记录只能查看；选择原后端后重新打开记录才能续聊。新保存使用 v2 格式，旧 v1 默认属于 Codex，继续保存时升级；旧版程序会拒绝 v2，避免误用会话 ID。
- 续聊要求：本地已保存的 capture 必须匹配规范路径、大小、修改时间和完整 SHA-256；校验在后台完成。不同、远程、临时或缺失的 capture 只能查看历史；纯聊天仅在没有 capture 时恢复。旧消息的 EID/资源跳转链接保持失效，新回复可正常跳转。所选 harness 的原生会话丢失或恢复校验失败时保留只读历史，可以 `New Chat` 重新开始。Claude 额外在 `<agent root>/claude-sessions/` 保存会话绑定信息，原生消息仍由 Claude runtime 保存；不要只拷贝 UI 快照来迁移上下文。M5a–M5c 的旧 ephemeral thread 不会自动迁移。
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
- 4 个 Skills 随构建部署到 `integrations/rendertest_agent/skills/`，并在 `.agents/skills/` 保留 Codex cwd 自动发现副本。Claude 适配器读取这些受信任的部署文件作为启动指令，不启用任意文件读取或全局 Skill；不依赖从仓库目录启动。
- 日志：`<agent root>/logs/chat-<instanceId>.log`，包含启动参数和 app-server 的 stderr 尾部。

环境变量：

- `RENDERTEST_AGENT_BACKEND`：覆盖保存的后端，仅接受 `codex` 或 `claude`。
- `RENDERTEST_AGENT_CODEX_COMMAND`：替换 Codex 命令（按空格拆分，支持引号），优先于设置；未配置时查找 npm 安装的 `codex.exe`，再查 PATH。
- `RENDERTEST_AGENT_CLAUDE_COMMAND`：替换 Claude 桥接进程命令，供测试/部署使用；协议须实现 RenderTest 后端接口，不是直接填普通 `claude` 交互命令。
- `RENDERTEST_MCP_PYTHON`：MCP Server / Claude SDK 适配器使用的 Python，优先于设置；未配置时查找 exe 上层目录中的 `agent/mcp_server/.venv/Scripts/python.exe`。
- `RENDERTEST_AGENT_MODEL` / `RENDERTEST_AGENT_REASONING_EFFORT`：覆盖保存的模型和推理强度，分别传给 `thread/start.model` 和 `turn/start.effort`。
- `RENDERTEST_AGENT_ISOLATE=0`：仅影响 Codex，不关闭其其它 feature 和 MCP server；shell 与 web search 仍然禁用。Claude 始终隔离。
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
# Claude GUI fake 回归；真实 Claude SDK/CLI + localhost Messages fixture + fake Bridge
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\chat_panel_e2e.py --scenario claude
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\claude_backend_check.py
# 在线 Claude + 当前真实 RenderTest 实例：两轮工具读取、退出后恢复原生会话；沿用已有认证，产生真实模型用量
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\claude_live_check.py --agent-root <agent-root> --instance <instance-id> --capture-session <session-id> --event <EID> --model "opus[1m]"
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

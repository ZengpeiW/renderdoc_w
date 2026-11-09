# RenderTest AI 当前现状与交接

更新日期：2026-11-09（Asia/Shanghai）。本文记录已经落地的功能、验证边界和后续候选，供新对话接手。

## 1. 快照与范围

- 工作目录：`E:\PersonalWorkSpace\git_repo\renderdoc`。
- 当前分支：`local_test`。
- 本轮基于交接提交 `3bf1bdbad`，新增 `get_api_calls`、`compare_events`，同步 Skill、文档与真实 capture 验证脚本。
- MVP/M0–M4、M5a–M5d 已实现，已有可使用的内嵌 AI 助手；目前有 **30 个 MCP 工具、4 个 Skill、Codex 和 Claude 两种 harness**。
- DeepSeek harness 未接入。本轮只实现 API 参数浏览与事件比较；Compute 调试、GPU counters 等仍是后续候选。
- 本轮开始时，唯一未跟踪文件是 `renderdoc/data/renderdoc.rc.bak`，属于已有文件，未修改、删除或纳入提交。

详细使用说明见 [agent/README.md](../README.md)。[MVP_DESIGN.md](MVP_DESIGN.md) 和 [M5_DESIGN.md](M5_DESIGN.md) 保留设计过程，部分早期范围和界面示例不代表当前实现，例如最初 M5 不包含 Claude、示例工具数量少于现在。

最近的功能提交：

| 提交 | 已完成内容 |
| --- | --- |
| `54ac0afda` | 打开设置时独立发现模型，修复下拉框只有默认项的问题 |
| `0a7c0b278` | 接入 Claude Code harness |
| `ea0223cd3` | 保留 Claude 连接设置，验证在线调用与历史续聊 |
| `7f6481637` | 内嵌 Shader 源码、资源统计、事件统计 |
| `37325000c` | CBuffer 成员查询/分页、单事件 Overlay 导出 |
| `d40a3ec89` | 使用预编译 release 反编译 Shader，支持 HLSL/GLSL 与自动回退 |
| 本次（2026-11-09） | 原始 API 调用/参数分页、同 capture 管线及选定 CBuffer 差异比较 |

## 2. 架构、面板与历史

主要链路：RenderTest Qt 面板 → 所选 harness → rendertest MCP → 文件 mailbox IPC v1 → 内嵌 Python Bridge → RenderDoc replay API。

- Codex 使用 `codex app-server`；Claude 使用 Claude Agent SDK/CLI，由 `rendertest_mcp.claude_backend` 适配现有面板协议。模型循环属于对应 harness，RenderTest 没有另造一套 LLM agent 循环。
- `Window → AI Assistant` 打开面板，`Settings → Backend` 选择 Codex/Claude。两种后端分别保存模型、推理强度与可执行文件配置；支持刷新模型目录和手填模型。
- 工具调用固定绑定当前窗口的 `instanceId` 与 `captureSessionId`。加载/关闭 capture 会终止旧绑定；无有效 capture/Bridge 时可以纯聊天，不会转连其他窗口。
- 支持流式回复、Stop、New Chat、工具卡、PNG artifact 缩略图，以及回复中的 EID/资源跳转。
- Event Browser、Texture Viewer、像素上下文和 Mesh Viewer 提供 Ask AI 入口，通过可编辑的 context chip 附带事件/资源/像素/顶点选择。
- Codex 限制为 read-only sandbox、never approval，并关闭 shell/web。Claude 只开放绑定的 rendertest 工具，禁用内置 shell/文件/web、插件及 hooks；这是工具权限限制，不等于操作系统沙箱。

历史已实现：

- UI 快照保存在 `<agent root>/conversations/<UUID>.json`，支持搜索、打开、流式定期保存、原子落盘、会话锁和跨进程恢复；当前新保存格式为 v2，兼容读取旧 v1 Codex 记录。
- 恢复还依赖所选 harness 的原生历史；Claude 另有 `<agent root>/claude-sessions/` 绑定 ledger。只复制 UI JSON 不等于迁移完整上下文。
- 续聊检查后端及本地 capture 的规范路径、大小、修改时间和完整 SHA-256。不同、远程、临时或缺失的 capture，以及缺失的原生历史，可能只能只读查看。
- 旧消息里的 EID/资源链接不会直接用于新 capture session；恢复后重新绑定实例/session，新回复可正常导航。

认证情况需要区分：

- Claude 的真实在线模型、RenderDoc 工具调用及跨进程续聊已验证。认证由原生 Claude CLI/支持的连接环境管理，RenderTest 不保存 API key，也没有 Claude 浏览器登录按钮。
- Claude 会从用户级 `settings.json` 补充连接环境白名单，尊重 `CLAUDE_CONFIG_DIR`，已有进程环境优先；不加载项目配置，不执行 `apiKeyHelper`。
- Codex 浏览器 OAuth 完整流程仍缺人工验收；已有 fake 登录状态流和真实 app-server 的本地 fixture 验证。不要把这项待验收误读为“Claude 从未在线验证”。

## 3. 已暴露的 30 个 MCP 工具

以 [protocol.py](../mcp_server/src/rendertest_mcp/protocol.py) 的 `TOOL_NAMES` 和 [server.py](../mcp_server/src/rendertest_mcp/server.py) 为准。

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context` |
| 事件 | `list_actions`、`find_actions`、`inspect_event`、`get_action_stats`、`get_api_calls`、`compare_events`、`set_event` |
| 管线/Shader | `get_pipeline_state`、`get_shader`、`get_shader_source`、`decompile_shader`、`get_cbuffer` |
| 资源 | `list_resources`、`get_resource`、`get_resource_usage`、`get_resource_stats` |
| 纹理/诊断图 | `export_texture`、`export_overlay`、`pick_pixel`、`get_texture_stats` |
| Buffer | `get_buffer_data` |
| 历史/调试/几何 | `pixel_history`、`debug_pixel`、`debug_vertex`、`get_mesh_data` |
| 消息 | `get_debug_messages` |
| 文本产物 | `read_artifact` |

`ping` 是 Bridge 内部方法，不额外计入 MCP 工具数。外部 `renderdoc-mcp` 是另一套服务，它提供的工具不代表内嵌助手也有；上述清单才是当前内嵌能力。

需要保留的行为约定：

- `set_event` 是唯一明确移动 UI 事件的工具。其他读取保持 UI 选择；临时回放后恢复，`get_api_calls` 则直接读取记录而不切换 replay。artifact 工具只产生文件，不替换 Shader 或修改 CBuffer。
- ResourceId 使用十进制字符串，EID/资源身份不能跨 capture session 混用。显式用户目标优先于 UI 当前选中项。
- 列表按游标分页；`truncated`、`warnings` 和 `nextCursor` 不能忽略。大产物通过 `read_artifact` 读取，路径必须先由当前 MCP 进程中的工具返回，并通过实例/session、大小和 SHA-256 校验。
- `get_resource_stats` 是资源描述大小的汇总，不是实际驻留/去重显存；`get_action_stats` 是操作及工作量统计，不是 GPU 计时。
- `get_texture_stats` 当前提供所选纹理子资源的 min/max；EXR/DDS 纹理导出已存在，不要再当成待新增能力。

## 4. 分析扩展的实际边界

### API 参数与事件比较（本轮）

- `get_api_calls(event_id=...)` 列出该 EID 所属 action 的 API 调用；`api_event_id=...` 查询精确 API EID 的参数。`ActionIndex` 保存每条 API 事件的 `chunkIndex`，真实 API EID 与 owner action EID 分开返回，不能混用。
- `parameter_path` 只接受返回的 ordinal 路径（如 `[2][0][1]`），支持数组随机访问与同级分页，不遍历整块大数组。单页 4096 节点/256 KiB、深度 0–4、路径最多 64 层；ResourceId 和大整数保留精度。NoChunk/缺失 chunk 明确不可用，原始 Buffer payload 不导出。RenderDoc 序列化记录可能包含内部字段和返回值，不能一概称为应用原始实参。
- `compare_events(before_event_id, after_event_id, ...)` 在同一 capture 的两个实际 EID 读取归一化管线状态，支持可选阶段、绑定上限和至多 8 组指定 CBuffer/成员；常量选择恰用 name 或 index，可复用 `get_cbuffer` 成员路径。输出 JSON Pointer 差异、明确的 missing/null、覆盖范围和分页；容器类型变化用 `kind="type-change"` / `valueType`。
- `comparison` 区分 `different`、`equal-in-scope`、`indeterminate`。有截断、反射/解码缺失或预算限制时，不把零差异判为相等；`totalDifferencesExact=false` 表示差异数为下界，`coverage.differencesComplete` 单独标识差异预算是否完整。
- 覆盖只含 viewport/scissor 0、constant block 数组元素 0、已使用的绑定和现有管线摘要；阶段按 owner graphics/compute 类型筛选，CBuffer 选择独立指定阶段。列表按位置匹配，Shader/布局/绑定差异也会报告；相同 ordinal 不保证跨布局语义相同。资源内容、Shader 文本、未选常量值均不参与。
- 单侧快照上限 32768 节点/2 MiB，差异上限 8192 条/2 MiB，单页 256 KiB，单 CBuffer 反射大小上限 1 MiB；底层实际 buffer 读取长度也限定到反射范围，绑定范围过短/为零时标为未知，push/root constants 不误拒绝。只缓存最近两组比较；不带 cursor 读取新快照，续页才用缓存；重启相同查询或淘汰后旧游标失效。
- 两工具均为 `read`，不改变 UI。MCP 的 nullable 参数路径保留为字符串（例如 `[1]`），避免 FastMCP 预解析成数组；Codex/Claude 都使用更新后的 30 工具目录。core、event-analysis、shader-debug 三份 Skill 已更新，仍保持四份 Skill 总数。

### 内嵌源码、统计、CBuffer 和 Overlay

- `get_shader_source` 只读 capture 反射中嵌入的文件，目录分页后按 `file_index` 导出 UTF-8 文本。没有源码时明确不可用；捕获文件名只是标签，不会据此读取宿主机文件。单文件上限 64 MiB。
- `get_cbuffer` 保留旧的 4096 节点摘要；`member_path=""` 分页浏览根成员，名称路径或返回的 `canonicalPath` 可定位深层结构/大数组。`max_depth=0..4`，响应仍有节点和字节预算。分页限制的是返回量，原生 API 仍解码整个 block。成员偏移相对常量块，需要时再加绑定偏移。
- `export_overlay` 支持 Drawcall、Wireframe、Depth、Stencil、BackfaceCull、ViewportScissor、QuadOverdrawDraw、TriangleSizeDraw、NaN、Clipping 共 10 种单事件 PNG。它是诊断可视化，不是原始数值或精确片元计数；需要读取 legend、缩放和 displayTransform。
- Overlay 当前不支持 pass/ClearBefore 模式、Mesh Shader draw、MSAA QuadOverdrawDraw。离屏输出在 finally 释放并恢复共享诊断资源；源资源有 16M 存储 texel 预算，输出有 2M 像素预算。

### Shader 反编译

使用 [HLSL-Decompiler v0.6](https://github.com/YYadorigi/HLSL-Decompiler/releases/tag/v0.6) 的 Windows x64 **预编译 release**，未从源码构建。用户已明确偏好使用现成 release。

- 安装器：[install_shader_decompiler.py](../devtools/install_shader_decompiler.py)。固定 ZIP SHA-256 为 `57748e4a9db0dfc40f094da8284c5e274ce45e06831c3a2fd61a844436984caa`，并核对每个组件文件的大小和 SHA-256。
- 仅安装三个 CLI 及其 DLL，不执行上游 wrapper/bat，不把第三方二进制纳入 Git。六个二进制和七份许可通知已安装；完整版本/来源清单在 [decompiler_release.py](../bridge/rendertest_agent_bridge/decompiler_release.py)。
- `decompile_shader(stage, event_id=None, language="auto")` 默认先尝试 HLSL，文本后端无法表达时自动回退 GLSL；显式 `hlsl`/`glsl` 不改变语言。`requestedLanguage` 和实际 `outputLanguage` 分开记录。
- 支持 DXBC→raw LLVM→SPIR-V→文本、DXIL→SPIR-V→文本、Vulkan SPIR-V→文本。**raw LLVM 路线只能加 `--raw-llvm`，不能同时加 `--use-reflection-names`**；后者要求 DXIL 容器，这是实际样本验证发现并已修复的参数冲突。
- HLSL 目标为 SM 6.0，GLSL 为 450 Vulkan。HLSL tessellation 不支持，auto 对 hull/domain 直接选 GLSL；部分波操作也会触发 GLSL 回退。GLSL/OpenGLSPIRV 等其他输入编码尚未接入。
- 输出明确标记 `generated=true`、`isOriginalSource=false`。它不是原始源码，不保证恢复名称/宏/注释或语义等价，绑定/接口可能重映射；原始反射、CBuffer 和实际绑定仍是核对依据。
- 原始字节在短 replay callback 中复制，外部转换运行在 Bridge worker，UI EID 先恢复。全链硬超时 60 秒，输入/中间结果/输出各限 16 MiB，单步日志限 1 MiB；Windows 进程先挂起创建、加入 kill-on-close Job 再恢复，临时目录和进程均回收。
- 单个实例只有一个 mailbox worker；转换期间其他请求会排队。面板 Stop 不保证立刻取消已开始的 Bridge 转换，停止 Bridge/请求 deadline/转换超时会终止进程。超时、取消、超限、工具缺失和前置字节码转换失败不触发语言回退。

## 5. Skill、源码入口和部署

仓库有四份 Skill，位于 `.agents/skills/`：

| Skill | 职责 |
| --- | --- |
| `rendertest-core` | 实例/session、目标选择、工具效果和证据边界 |
| `rendertest-event-analysis` | 事件/pass、输入输出、API 参数、事件比较、资源及事件统计 |
| `rendertest-pixel-investigation` | 像素取值、写入历史、失败原因与像素调试 |
| `rendertest-shader-debug` | 内嵌源码、反编译、反射/绑定/CBuffer、Shader 调试 |

Codex 使用原生 Skill 发现/加载，设置部署目录 extraRoots，必要时回退 cwd `.agents/skills`。Claude 适配器把四份受信任 Skill 正文注入启动指令，不开放任意文件读取；不能假设 Claude 每次只自动选择一份 Skill。

主要代码入口：

- Qt 面板/后端协议/设置/历史：`qrenderdoc/Code/Agent/`、`qrenderdoc/Windows/AgentChatPanel.*`。
- MCP schema、权限与 artifact 读取：`agent/mcp_server/src/rendertest_mcp/`。
- Bridge 原生方法：`agent/bridge/rendertest_agent_bridge/methods/`；replay/session/index/cache 基础设施在它的父目录。
- 测试及验证脚本：`agent/tests/`、`agent/devtools/`；native Qt 历史测试在 `agent/tests/native/`。
- 内嵌 Bridge 保持 **Python 3.6** 兼容；外部 MCP 要求 Python >=3.10，本机 `.venv` 使用 Python 3.14。

本机部署位置：

| 内容 | 路径 |
| --- | --- |
| 启动程序 | `E:\PersonalWorkSpace\git_repo\renderdoc\x64\Release\qrendertest.exe` |
| Bridge | `<exe目录>/integrations/rendertest_agent/python/rendertest_agent_bridge/` |
| Skill 正文 | `<exe目录>/integrations/rendertest_agent/skills/` |
| Codex Skill 副本 | `<exe目录>/integrations/rendertest_agent/.agents/skills/` |
| 反编译 release | `<exe目录>/integrations/rendertest_agent/tools/shader_decompiler/` |
| 默认 IPC/日志/历史根 | `%LOCALAPPDATA%/RenderTest/agent`，可用 `RENDERTEST_AGENT_ROOT` 覆盖 |

最新 Bridge、Skill 和反编译 release 已更新到上述原路径。使用新版本需重启 RenderTest，并新建 AI 对话刷新工具/指令。最近工具扩展仅改 Python/Skill，没有重新编译 C++ 程序。

正常构建 `qrenderdoc/qrenderdoc_local.vcxproj` 会部署 Bridge 和 Skill；反编译 release 单独安装。非空安装目录只校验、不覆盖；损坏时先改名保留，再重新安装。其他安装位置用 `--output`，运行时配 `RENDERTEST_SHADER_DECOMPILER_DIR`，仍执行固定哈希检查。

## 6. 已有验证记录与复现

本轮重跑 Python 全套及两个新工具的真实 capture 验证，未重新调用在线模型。其他功能沿用对应提交时的验证记录。本地 `agent/.agent-data/` 中的证据不跟踪到 Git，换机器后可能不存在。

| 范围 | 已有结果与边界 |
| --- | --- |
| 最新 Python 全套 | `963 passed`（33.19 秒）；在宿主环境运行，测试临时文件限定在工作区。不是对所有 GPU/API 的保证 |
| Skill | 四份 Skill 均通过 validator；本轮更新 core/event-analysis/shader-debug，部署两份 Skill 副本并逐字节核对 |
| API/事件比较实际 capture | `pragmata1.rdc`：EID 3820 的 16 条 API 调用，Clear 7646、Copy 7702、Barrier 7705、Draw 3820 的 33 个标量及数组分页与独立原生 SDObject 读取一致；3803→3820 的 83 项管线差异与独立 get_pipeline_state 一致。SceneInfo.viewProjMat 两处值相同、Shader 身份不同，已核对 get_cbuffer；同 EID 完整范围为 equal-in-scope，绑定截断时为 indeterminate。UI EID、用户配置保持不变 |
| 第一批分析工具 | `327 passed, 2 skipped`；实际 capture 验证资源/事件统计。所测 Shader 无内嵌源码，源码导出由 fixture 验证 |
| 第二批 | `577 passed, 2 skipped`；`pragmata1.rdc` EID 3820 的 CBuffer 分页/成员值核对，10 种 Overlay 均实际导出并返回图片 |
| 反编译实际 capture | `pragmata1.rdc` EID 3820：VS ResourceId 2953，auto→HLSL；PS ResourceId 6150，HLSL 遇到 BallotFindLSB 限制，auto→GLSL。auto/GLSL 四项及 MCP artifact 读取均通过，UI EID、用户配置保持不变 |
| 反编译补充样本 | 官方 DXBC fixture、已导出的 DXIL Compute Shader、由它生成的 SPIR-V fixture 均完成 release 全链转换。这些离线样本检查不等于重新打开额外的真实 capture 或完成另一种 GPU/API 的验证 |
| Claude 在线 | 已完成真实模型/真实 RenderDoc 工具调用，以及关闭后再启动续聊；两个进程正常退出。它与 localhost SDK fixture 验证是两类证据 |

本地成功证据索引（相对仓库根目录）：

- Claude GUI 与续聊：`agent/.agent-data/claude-gui-live-0880543f97a54e6aa3fb65ced7e1a938/summary.json`；其 `live-protocol/6cd95942-2898-4a61-9adb-a873380eb712/report.json` 为两轮在线验证成功报告。
- 第一批：`agent/.agent-data/analysis-live-0275182cedf74d52bea35c08d754fa20/report.json`。
- 第二批：`agent/.agent-data/second-batch-live-a48e9316f65143bbbcd942bc367c6905/report.json`。
- 反编译 capture：`agent/.agent-data/decompile-live-512d72279ae4491b88b139a739432ea7/report.json`。
- 反编译三种输入样本：`agent/.agent-data/decompiler-offline-check/report.json`。目录内另保留修复前失败和参数对照报告，不应当成最终结果。
- API/事件比较：`agent/.agent-data/event-inspection-live-31df81b69f554e3e8bb32d44ee11b38e/report.json`；同目录 `native-reference.json` 是独立原生读取结果。此报告对应缓存刷新顺序、底层 CBuffer 读取预算均已修复的最终版本。

从仓库根目录运行：

```powershell
# Python 回归，不调用在线模型
agent\mcp_server\.venv\Scripts\python.exe -m pytest agent/tests -q -p no:cacheprovider

# 固定预编译包的安装和校验，不进行源码构建
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\install_shader_decompiler.py
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\install_shader_decompiler.py --verify-only

# 三批工具的真实 capture 专项检查；此 EID 只适用于下述样本
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --analysis-only --launch "E:\Captures\pragmata1.rdc" --event-id 3820
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\smoke_live.py --second-batch --launch "E:\Captures\pragmata1.rdc" --event-id 3820
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\decompile_live.py --launch "E:\Captures\pragmata1.rdc" --event 3820

# 本轮 API 调用/参数与事件比较；同样只启动、关闭自建隐藏进程
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\event_inspection_live.py --launch E:/Captures/pragmata1.rdc --before 3803 --after 3820 --cbuffer-stage pixel --cbuffer-name SceneInfo --member viewProjMat
```

`decompile_live.py` 和 `event_inspection_live.py` 会创建独立隐藏 GUI 进程、关闭该进程的配置写入、固定实例/session、校验产物/结果及用户配置哈希，最后只关闭自己启动的进程。不要把用户正在使用的 GUI 当成测试进程关闭。在线 Claude/Codex 验证命令及环境配置见 README；已有通过记录不代表当前账号登录状态无需检查。

## 7. 已知问题及尚不支持的能力

- 在 `pragmata1.rdc` 的更广泛 `--all-p0` 检查中，曾出现 pixel_history 与 pick_pixel 结果不一致，以及 EID 3014 的 mesh/debug_vertex 对比不一致。已用扩展前的旧 Bridge 复现，未在这几批工具中修复。基线证据为 `agent/.agent-data/analysis-live-fef62ba077cd4171bc4bb05b5e580f7b/report.json`；不要把专项通过描述为这些旧问题已解决。
- 当前没有 Compute 单线程调试、GPU counter/timing 工具、Shader 引用搜索或 pass 资源流工具。API 参数浏览和结构化事件差异本轮已加入。
- `find_actions` 目前只按名称/EID 和操作 flags 搜索，不支持 Shader ResourceId 过滤；不能假设 `GetUsage(shaderId)` 能找 Shader 引用。
- 尚无 Shader 编辑/替换、CBuffer 修改、任意 Python/shell 执行、自动 Android 抓帧或远程连接管理工具。外部 `renderdoc-mcp` 的这些能力不会自动出现在内嵌助手。
- Shader 可调试性、Overlay 及 counter 支持都与具体 API/驱动/Shader 有关；反编译成功也不保证可重新编译或与原程序等价。

## 8. 下一批候选（未实现）

`get_api_calls` 与 `compare_events` 已完成。接下来可优先考虑 `debug_compute`；以下仍是候选，并未开始开发，以用户下一轮确定的范围为准。

| 候选 | 价值与已有基础 | 接手时注意 |
| --- | --- | --- |
| `debug_compute` | 原生 `ReplayController.DebugThread(groupid, threadid)`；可复用 `methods/debug.py` 的 ContinueDebug/FreeTrace 和 trace artifact | 检查 dispatch、group/thread 边界及 debuggable；现有实际 capture 尚未验收该能力，不能承诺所有 Shader 均支持 |
| GPU counters | `EnumerateCounters` / `DescribeCounter` / `FetchCounters`，先做 GPU duration 与支持的计数器 | 测的是回放；可能重放整帧，Top N 只限制结果；原生无取消参数，需强制恢复事件，失败值不能按 0 处理 |
| `find_shader_references` | 找同 Shader 的 draw/dispatch，为事件比较提供目标 | 需要有界管线扫描与 session 缓存，不是简单调用资源 GetUsage |
| `get_pass_resources` | 指定 marker/EID 范围的资源读写及前后使用，复用 GetUsage 和 ActionIndex | 资源级用途是关联证据，不证明某个像素被写入；不能宣称还原了引擎原生 RDG |

另一项小增强是批量局部纹理数值采样，可复用 PickPixel 并限制采样点数；无需重新实现已存在的 EXR/DDS 导出。完整 RDG 图、Shader 替换等更大改动暂不作为这一批默认范围。

新对话的最短接手顺序：读本文 → 查看当前 `git status`/`git log` 与 README → 确认用户选择的下一批范围 → 再实现、更新对应 Skill、做针对性回归和实际 capture 验证、按用户要求提交。此前本机默认 PowerShell 工具调用曾卡住，`cmd.exe` + `login:false` 更稳定；含复杂引号的命令优先用临时 Python helper 和 subprocess 参数数组。此项是本机执行经验，不是产品依赖。

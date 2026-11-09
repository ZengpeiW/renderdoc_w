# RenderTest AI 当前现状与交接

更新日期：2026-11-09（Asia/Shanghai）。本文记录已经落地的功能、验证边界和后续候选，供新对话接手。

## 1. 快照与范围

- 工作目录：`E:\PersonalWorkSpace\git_repo\renderdoc`。
- 当前分支：`local_test`。
- 本轮基于 `9a4a88089`，新增 `get_pass_resources`，同步 Skill、文档与真实 capture 验证脚本。
- MVP/M0–M4、M5a–M5d 已实现，已有可使用的内嵌 AI 助手；目前有 **35 个 MCP 工具、4 个 Skill、Codex 和 Claude 两种 harness**。
- DeepSeek harness 未接入。本轮增加 Pass/事件范围的资源用途与前后使用查询，读取原生 GetUsage 元数据，不移动回放；结果不代表像素/字节级依赖或引擎原生 RDG。
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
| `01fbe87e0` | 原始 API 调用/参数分页、同 capture 管线及选定 CBuffer 差异比较 |
| `9851069ef` | Compute 单线程调试，工作组/组内坐标验证、JSONL 轨迹与异常清理 |
| `9e97ed3de` | GPU 计数器目录、逐事件回放耗时、采样缓存/刷新和强制恢复；D3D12 固定频率改为显式开启 |
| `2348074f8` / `794c88579` | Claude Windows 启动修复，长系统提示通过文件传递，短提示保留内联 |
| `9a4a88089` | Shader 引用搜索、候选扫描预算、跨查询绑定缓存和覆盖状态 |
| 本次（2026-11-09） | Pass 资源用途、真实事件边界、前后相邻使用及共享用途缓存 |

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

## 3. 已暴露的 35 个 MCP 工具

以 [protocol.py](../mcp_server/src/rendertest_mcp/protocol.py) 的 `TOOL_NAMES` 和 [server.py](../mcp_server/src/rendertest_mcp/server.py) 为准。

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context` |
| 事件 | `list_actions`、`find_actions`、`inspect_event`、`get_action_stats`、`get_api_calls`、`compare_events`、`set_event` |
| GPU 计数器 | `list_gpu_counters`、`get_gpu_duration` |
| 管线/Shader | `get_pipeline_state`、`get_shader`、`get_shader_source`、`decompile_shader`、`find_shader_references`、`get_cbuffer` |
| 资源 | `list_resources`、`get_resource`、`get_resource_usage`、`get_resource_stats`、`get_pass_resources` |
| 纹理/诊断图 | `export_texture`、`export_overlay`、`pick_pixel`、`get_texture_stats` |
| Buffer | `get_buffer_data` |
| 历史/调试/几何 | `pixel_history`、`debug_pixel`、`debug_vertex`、`debug_compute`、`get_mesh_data` |
| 消息 | `get_debug_messages` |
| 文本产物 | `read_artifact` |

`ping` 是 Bridge 内部方法，不额外计入 MCP 工具数。外部 `renderdoc-mcp` 是另一套服务，它提供的工具不代表内嵌助手也有；上述清单才是当前内嵌能力。

需要保留的行为约定：

- `set_event` 是唯一明确移动 UI 事件的工具。其他读取保持 UI 选择；临时回放后恢复，`get_api_calls` 和 `get_pass_resources` 则直接读取记录而不切换 replay。artifact 工具只产生文件，不替换 Shader 或修改 CBuffer。
- ResourceId 使用十进制字符串，EID/资源身份不能跨 capture session 混用。显式用户目标优先于 UI 当前选中项。
- 列表按游标分页；`truncated`、`warnings` 和 `nextCursor` 不能忽略。大产物通过 `read_artifact` 读取，路径必须先由当前 MCP 进程中的工具返回，并通过实例/session、大小和 SHA-256 校验。
- `get_resource_stats` 是资源描述大小的汇总，不是实际驻留/去重显存；`get_action_stats` 是操作及工作量统计，不是 GPU 计时。
- `get_texture_stats` 当前提供所选纹理子资源的 min/max；EXR/DDS 纹理导出已存在，不要再当成待新增能力。

## 4. 分析扩展的实际边界

### Pass 资源用途（本轮）

- `get_pass_resources(parent_event_id=None, start_event_id=None, end_event_id=None, kind="all", cursor=None, limit=200, scan_budget=200, refresh=false)` 默认整帧；parent 查精确 action/marker 子树，或同时传两个真实 API/执行 EID 查闭区间。parent 与 range 互斥，不从当前 UI 猜 pass。kind 为 all/texture/buffer，all 只含后两类。
- 子树按真实 action anchors 裁剪成员集合，不只判断 owner 或数值区间。`NoChunk` 合成 marker 不作边界或成员；首个真实 action 前的 setup API 排除，孔洞不补齐。无真实 anchors 返回 UNSUPPORTED。保留 raw usage EID，actionEventId 只作标签；范围内未映射记录使 coverage 不完整，不凭邻近 action 猜归属。
- 资源按数值 ID 排序；每行提供 usageRecordCount/eventCount/首末 EID、原生 usage 名称和整数、分类、读写角色布尔值及最多 16 条示例。明确区分 read/write/readWrite/discard/barrier/unused/unknown；DepthStencilTarget 是潜在 readWrite，可能只测试/读取。ColorTarget/UAV 角色不证明实际写入。Discard 不算有效输出，重复原生记录保留，计数不是 invocation 或显存访问次数。`(0, Unused)` 原生哨兵单独计数，不算实际用途。
- previousUsage/nextUsage 是边界外最近 raw EID 的整组记录；保留完整计数、最多 16 条用途及截断标记。整帧时为 null；不称为 producer/consumer、最后写入者或精确输入输出。子资源、像素/字节和别名依赖不在范围内。
- limit（1–1000）限制匹配行，scan_budget（1–2000）限制本页候选访问，含 cache hits；空页也可能有 nextCursor。coverage 的 unknown/unmapped/sentinel 诊断累计覆盖已处理前缀；scanComplete 表示扫完，complete 还要求无解释/归属缺口，但不保证原生跟踪覆盖每次实际访问。示例截断保留全量聚合，不能只看单页判断无匹配。
- 每 session 共享已读资源的完整 primitive GetUsage 历史，独立于旧 get_resource_usage 缓存，可跨范围/kind 复用。scanId/scanStartedAtUtc 表示逐批构建的代次；游标绑定 session、代次、scope、kind、候选位置，可调分页预算。refresh 不能带 cursor，成功后所有旧 Pass 资源游标失效，任何失败不发布部分缓存或覆盖旧代次。
- 读取通过普通 replay.call，绝不 SetFrameEvent，也不做强制恢复。每页 256 KiB、cache 64 MiB、范围 100000 action/200000 EID、候选 100000 资源、单资源 100000 原生记录；明确预算/取消/session/健康检查，超限或 malformed 原生数据整页失败。MCP 等待 120 秒不保证即时中断原生 GetUsage。更新 core/event-analysis Skill，仍共四份。

### Shader 引用搜索

- `find_shader_references(resource_id, stages=None, parent_event_id=None, cursor=None, limit=200, scan_budget=200, refresh=false)` 接受当前 capture 的精确十进制 Shader ResourceId；资源不存在或不是 Shader 时拒绝。默认整帧，显式 parent 必须是实际 action EID，含自身与后代；stages 可省略或为非空八阶段子集，规范去重。
- 候选按 EID 排序，只包含与阶段筛选相交的叶 draw/compute/mesh action。传统 draw 检查 VS/HS/DS/GS/PS，compute 只检查 CS，mesh 检查 amplification/mesh/pixel。容器、marker、copy/clear 不从遗留管线报引用；ray shader table 和缺失的原生阶段能力明确标为未覆盖，不误当 compute。
- 每页访问至多 scan_budget（1–2000）个候选，包括缓存命中；返回至多 limit（1–1000）个匹配事件。`references[].matches` 保留 stage/resourceId/entryPoint，空入口标 `entryPointKnown=false`；直接读 GetShader/GetShaderEntryPoint，不依赖 reflection 或 GetUsage。确认的是绑定，不是实际执行或非零 invocation。
- 每页 `coverage` 提供范围、候选数、已处理前缀、本页访问/native读取/cache命中、不支持项等。`scanComplete` 只表示候选已处理完，`complete` 还要求无适用覆盖缺口。无匹配的页也可能返回 nextCursor，不能据此断言没有引用；不提供误导性的全局匹配总数。
- 每 session 共享最近一代逐事件绑定缓存，记录该事件全部相关阶段，可跨 Shader/阶段查询复用；scanId/scanStartedAtUtc 表示逐批观察的代次，不是单次原子快照。游标绑定 session、代次、资源、规范 stages、parent 和候选位置，limit/scan_budget 可调整。成功 refresh 重置共享代次，所有旧引用游标失效；refresh 不能带 cursor，失败保留原缓存。
- 新事件记录先暂存在本批；回放/健康检查、finally 强制恢复、session/取消检查成功后才发布。完全缓存命中的页不回放，返回空 replay meta。每页 256 KiB，缓存 32 MiB，scope action 上限 100000；超限明确失败。MCP 120 秒等待不保证原生 replay 可即时抢占。
- 更新 core、shader-debug 和 event-analysis Skill，保持四份 Skill；Codex/Claude 共用工具目录。先获得真实 Shader ID，按有用的子树查找，再用 compare_events 分析候选事件差异。

### GPU 计数器与回放耗时

- `list_gpu_counters(cursor=None, limit=200)` 只查询原生目录/描述，返回计数器 ID、类型/字节宽度/单位、UUID 等；保留原生 vendor ID token，不要求其能构造为已知枚举。按页描述，不触发 `FetchCounters`。
- `get_gpu_duration(parent_event_id=None, cursor=None, limit=200, sort_by="duration", refresh=false)` 只采样原生 `EventGPUDuration`，先验证 Float/8-byte/Seconds 元数据，再读 double。每个范围内 action 返回一行，包括没有测量的 marker/容器；`durationSeconds` / `durationMs` 只在 `status=valid` 时有值。负数、NaN/Inf、换算溢出为 invalid；同 EID 多项为 duplicate，不选取/求和；缺少为 missing。未知/非 action EID、不匹配的 counter 和损坏结果保留有界诊断，不映射 owner。
- 默认整帧；显式 parent 必须是实际 action，包含自身与后代。有效值按耗时降序，其余按 EID 排序；也可 `sort_by="event_id"`。单页最多 1000 行/256 KiB；目录最多 65536 项、描述缓存 32 MiB、native 返回最多 1000000 行、归一化样本 64 MiB，超限明确失败。
- 首次/刷新调用原生 `FetchCounters` 重放整帧，筛选/排名/分页不降低采样成本。同 session 仅缓存最近成功完整样本；返回 `sampleId`、采样时间和 `cached`。游标绑定 sample、session、parent、排序；成功刷新使旧游标失效。刷新不能带 cursor，失败保留旧样本。手动替换 Shader 等未更换 session 的操作后需要 refresh。
- `ReplayRunner.call_restoring` 在 finally 用 `SetFrameEvent(latestUIEID, True)` 强制恢复。仅恢复相同数字 EID 而 `force=False` 不足以修复 FetchCounters 改变的 GPU 状态。采样/恢复后核对 fatal、session、取消；失败不发布新样本，缓存页不复用首次采样旧 UI 元数据。原生调用不可立即抢占，120 秒 MCP 等待不保证硬超时。
- GPU duration 是回放测量，受 GPU 频率/负载影响，不是原始应用帧时间；事件可重叠/alias，不能求和宣称帧/Pass 墙钟耗时或 FPS。missing 不是 0，原生报告 0 也不证明没有 GPU 工作。
- 本机 D3D12 实测发现原先默认调用 `SetStablePowerState(TRUE)` 会在未开 Windows 开发者模式时真实 DeviceLost。现在 `D3D12_Replay_StablePowerState` 默认关闭，仍使用时间戳与队列频率换算；显式开启仍要求开发者模式。只改产品配置默认值，未修改系统设置。依据：[Microsoft API](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-setstablepowerstate)、[时间戳规范](https://microsoft.github.io/DirectX-Specs/d3d/CountersAndQueries.html)。
- Codex/Claude 共用工具目录，GPU 扩展更新 core 与 event-analysis Skill；任意 vendor counter 采样未开放。

### Compute 线程调试

- `debug_compute(group, thread, event_id=None, max_steps=20000)` 复用原生 `DebugThread` / `ContinueDebug` / `FreeTrace` 和现有 JSONL 轨迹 schema。两个必填坐标均为 0 起的三元素 UInt32 数组，分别表示工作组坐标和组内线程坐标；`globalThread = group * threadGroupSize + thread`，逐分量计算。
- 只接受实际执行的普通 Compute Dispatch action，包含已解析且维度已知的 indirect 子 Dispatch。非 action API EID、marker、父容器、ray/mesh dispatch、空 dispatch 均不代替有效线程。ActionIndex 新保留 `dispatchBase`，非零/未知 base 当前明确不支持，避免 native Vulkan 的 base 解释差异造成错误坐标。
- `dispatchDimensions` 来自 action；`threadGroupSize` 按 GUI 规则优先采用 action override，否则采用 reflection，记录 `threadGroupSizeSource`。未知/零维、坐标越界、全局线程 ID 超出 UInt32、API 或 Shader 不可调试均明确拒绝。
- 返回 Shader/入口身份、坐标与维度、`inputs`、`lastChanges`、NaN/Inf 和采样事件摘要、`traceArtifact`。Compute 通常没有输出签名，`outputs=[]` 不意味着没有 UAV 写入；寄存器变化不等于最终资源内容，也不能证明跨线程时序、竞争或 GPU 耗时。
- `max_steps` 为 1–200000；`executionComplete=false` 是部分状态，`traceComplete=false` 表示 64 MiB artifact 预算使后续轨迹缺失。使用 `read_artifact` 分页/搜索。`debugged=false` 表示原生没有可用轨迹，不伪造成功。
- `effect=artifact`，不改变 UI 或修改 capture。所有返回的 trace 在 finally 释放，失败清理 `.part`；Compute 扩展也补上共享 writer 初始化失败清理，以及每次 native continuation 前的取消检查。MCP 采用 120 秒等待，但 native 调用本身不可立即抢占，不能承诺硬超时或即时 Stop。
- Codex/Claude 共用工具目录。Compute 扩展更新 shader-debug 和 event-analysis Skill，保持原四份 Skill；未增加额外 harness 或 C++ UI 改动。

### API 参数与事件比较

- `get_api_calls(event_id=...)` 列出该 EID 所属 action 的 API 调用；`api_event_id=...` 查询精确 API EID 的参数。`ActionIndex` 保存每条 API 事件的 `chunkIndex`，真实 API EID 与 owner action EID 分开返回，不能混用。
- `parameter_path` 只接受返回的 ordinal 路径（如 `[2][0][1]`），支持数组随机访问与同级分页，不遍历整块大数组。单页 4096 节点/256 KiB、深度 0–4、路径最多 64 层；ResourceId 和大整数保留精度。NoChunk/缺失 chunk 明确不可用，原始 Buffer payload 不导出。RenderDoc 序列化记录可能包含内部字段和返回值，不能一概称为应用原始实参。
- `compare_events(before_event_id, after_event_id, ...)` 在同一 capture 的两个实际 EID 读取归一化管线状态，支持可选阶段、绑定上限和至多 8 组指定 CBuffer/成员；常量选择恰用 name 或 index，可复用 `get_cbuffer` 成员路径。输出 JSON Pointer 差异、明确的 missing/null、覆盖范围和分页；容器类型变化用 `kind="type-change"` / `valueType`。
- `comparison` 区分 `different`、`equal-in-scope`、`indeterminate`。有截断、反射/解码缺失或预算限制时，不把零差异判为相等；`totalDifferencesExact=false` 表示差异数为下界，`coverage.differencesComplete` 单独标识差异预算是否完整。
- 覆盖只含 viewport/scissor 0、constant block 数组元素 0、已使用的绑定和现有管线摘要；阶段按 owner graphics/compute 类型筛选，CBuffer 选择独立指定阶段。列表按位置匹配，Shader/布局/绑定差异也会报告；相同 ordinal 不保证跨布局语义相同。资源内容、Shader 文本、未选常量值均不参与。
- 单侧快照上限 32768 节点/2 MiB，差异上限 8192 条/2 MiB，单页 256 KiB，单 CBuffer 反射大小上限 1 MiB；底层实际 buffer 读取长度也限定到反射范围，绑定范围过短/为零时标为未知，push/root constants 不误拒绝。只缓存最近两组比较；不带 cursor 读取新快照，续页才用缓存；重启相同查询或淘汰后旧游标失效。
- 两工具均为 `read`，不改变 UI。MCP 的 nullable 参数路径保留为字符串（例如 `[1]`），避免 FastMCP 预解析成数组；Codex/Claude 共用同一工具目录。core、event-analysis、shader-debug 三份 Skill 已反映这两工具的行为。

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
| `rendertest-event-analysis` | 事件/pass、资源用途与前后使用、输入输出、API 参数、事件比较、资源及事件统计、GPU duration 与计数器能力 |
| `rendertest-pixel-investigation` | 像素取值、写入历史、失败原因与像素调试 |
| `rendertest-shader-debug` | 内嵌源码、反编译、Shader 引用搜索、反射/绑定/CBuffer、Pixel/Vertex/Compute 单线程调试 |

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

最新 Bridge、Skill 和反编译 release 部署在上述原路径。使用新版本需重启 RenderTest，并新建 AI 对话刷新工具/指令。GPU 扩展已重新构建并部署原生 DLL/PDB，包含 D3D12 默认不锁频的逻辑；本轮 Pass 资源用途只涉及 Python/Skill，无需再编译原生程序。

正常构建 `qrenderdoc/qrenderdoc_local.vcxproj` 会部署 Bridge 和 Skill；反编译 release 单独安装。非空安装目录只校验、不覆盖；损坏时先改名保留，再重新安装。其他安装位置用 `--output`，运行时配 `RENDERTEST_SHADER_DECOMPILER_DIR`，仍执行固定哈希检查。

## 6. 已有验证记录与复现

本轮验证 Pass 资源用途与真实 capture，未重新调用在线模型。其他功能沿用对应提交时的验证记录。本地 `agent/.agent-data/` 中的证据不跟踪到 Git，换机器后可能不存在。

| 范围 | 已有结果与边界 |
| --- | --- |
| 最新 Python 全套 | `1586 passed`（68.85 秒）；在宿主环境运行，测试临时文件限定在工作区。不是对所有 GPU/API 的保证 |
| Skill | 四份 Skill 均通过 validator；本轮更新 core/event-analysis，部署两份 Skill 副本并逐字节核对 |
| Pass 资源 D3D12 | `pragmata1.rdc` 子树 7726，真实边界 3820..3901：25 个资源（11 texture / 14 buffer）。全部用途分类/计数/示例/邻接 EID 组与独立原生 GetUsage 相同；2540 候选资源每代各读取一次，首次+refresh 共 5080 次。资源 9131 的 Barrier@3819 不进入 parent，但在显式 3819..3820 区间保留 raw EID，owner 3820 只作标签。跨范围/kind 缓存零新增读取、空页续扫和旧游标失效通过；零 SetFrameEvent，UI EID 7708、资源 244 的 `(128,64)` 及管线保持不变，配置保留、自建进程退出 |
| Pass 资源 Vulkan | `恋与深空尝试截帧.rdc` 子树 2712，真实边界 1696..1722：45 个资源（17 texture / 28 buffer），每代 18 页。711 候选资源首次+refresh 共 1422 次 GetUsage，逐资源原始记录哈希、分类/计数/示例/邻接组与独立原生记录一致；跨范围/kind/空区间只读缓存，旧游标刷新后失效。零 SetFrameEvent，UI EID 2701、资源 4543 的 `(2,2)` 及管线保持不变，配置保留、自建进程退出。这两份样本的成功不等于所有 API/驱动均已实测 |
| Shader 引用 D3D12 | `pragmata1.rdc` 子树 3738：14 action / 12 draw，PS 297113 在 12 个 draw 绑定，VS 251109 同样可由缓存查询。所有引用 stage/入口/父 EID 与独立原生读取相同；首次+refresh 共 24 次候选扫描，每个 draw 的五个相关阶段（含未绑定阶段）恰读取两次，共 120 次 GetShader。UI EID 3820、资源 390180 的 `(340,576)` 在扫描后变化、force 后逐位恢复；fatal 为 Success，配置与 UI 选择保持，自建进程退出 |
| Shader 引用 Vulkan | `恋与深空尝试截帧.rdc` 子树 2712：8 action / 5 draw，PS 78421 有 3 个引用。首次+refresh 共 10 次候选扫描、50 次 GetShader，精确对上独立原生绑定。UI EID 27 / 资源 72716 的 9 个点在扫描后变化、force 后逐位恢复。两份 capture 均验证跨 Shader/阶段缓存零新增读取或移动、空页续扫、刷新使旧游标失效，以及无效参数拒绝；未将实际结果扩展为 mesh/ray 的实测保证 |
| 原生构建 | `renderdoc/renderdoc.vcxproj` Release x64 构建 exit 0；明确使用 HostX64 工具链，避免旧工程 HostX86 链接器的 LTCG heap 限制。未改系统开发者模式或用户配置 |
| GPU counters D3D12 | `pragmata1.rdc`：首次及 refresh 各 1138 valid / 874 missing，共 2012 action；目录、同次原生秒/ms、全部分页、排序/范围/缓存/刷新和旧游标失效通过。EID 3820 / 资源 390195 的 9 个 guard 点先通过无采样重复回放稳定性检查，采样后改变、force 后逐位恢复；所有 fatal 状态为 Success，UI/config 保持，自建进程退出 |
| GPU counters Vulkan | `恋与深空尝试截帧.rdc`：14 个计数器；670 个 action 中 538 valid / 132 missing。首次和 refresh 全部行均与各自同一次原生 Fetch 结果一致，秒/毫秒换算、排序、范围及分页通过；只发生两次 Fetch。EID 27 / 资源 72716 的 9 个像素在采样后改变，force 恢复后逐位相同；所有 fatal 状态为 Success，UI/config 保持，自建进程退出 |
| Compute 调试实际 capture | `pragmata1.rdc` EID 3120，DXIL Compute Shader 6237/main；组数 `[1,2,1]`、线程组大小 `[64,1,1]`。零坐标完整 67 步，group `[0,1,0]` / thread `[1,0,0]` 完整 46 步，`SV_DispatchThreadID=[1,1,0]` 与独立原生 DebugThread 一致；max_steps=1 正确截断，越界及 Draw/非 action EID 被拒绝。三份 trace 的 SHA/大小/来源/读取及 UI/config 保持通过。只验证了该 D3D12 样本，不代表所有 API/Shader |
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
- Compute 调试：`agent/.agent-data/compute-debug-live-abd5c8f6ef944a6c9f01e749dc1015c8/report.json`；同目录 `native-reference.json` 保留独立原生 DebugThread 输入和维度参考。报告对应共享取消检查、写入失败清理和严格 schema 均已完成的最终部署版本。
- GPU counters Vulkan：`agent/.agent-data/gpu-counters-live-690d383d25aa41e68d9ea7485720489c/report.json`；同目录 native reference/measurements 保留目录和两次同源采样、回放状态证据。
- GPU counters D3D12：`agent/.agent-data/gpu-counters-live-8d98872ec8b743f5bbc46cd15874b22a/report.json`；同目录 native reference/measurements 包含稳定 guard 基线和两次完整采样。早期失败报告记录的是修复前的 stable-power DeviceLost 或未筛选 guard 的非确定性，不作为最终成功证据。
- 原生构建：`agent/.agent-data/gpu-counter-build/build.log`；部署 SHA-256 和旧 DLL/PDB 备份路径见该目录 `deployment.json`。这些二进制不提交 Git。
- GPU 扩展的 Python 全套：`agent/.agent-data/gpu-counter-tests-final.xml`。该次同时改进既有 Windows 子进程测试的就绪等待及句柄观察，保留原超时/退出断言，产品反编译 runner 未改。之后 Claude 启动与校验改动已由独立提交完成。
- Shader 引用搜索 Python 全套：`agent/.agent-data/shader-reference-tests-final.xml`，包含当时 34 工具目录、schema/IPC/fake、覆盖/缓存/取消/恢复与范围和字节预算回归。
- 本轮 Pass 资源 Python 全套：`agent/.agent-data/pass-resource-tests-final.xml`，覆盖 35 工具目录、严格 schema/IPC/fake、合成 marker 与 API 范围、未知用途/归属、重复资源/记录、分页缓存/刷新原子性、取消/失败与预算；新增 Bridge 测试禁止任何 SetFrameEvent。
- Pass 资源 D3D12：`agent/.agent-data/pass-resources-live-77023112b4404b82a8b378f77bdb201a/report.json`；同目录 native reference/observations 保留全量用途、同源哈希、每代读取次数、零回放移动与像素/管线证据。早期 probe 和验收脚本断言修正前的失败记录不作为最终证据。
- Pass 资源 Vulkan：`agent/.agent-data/pass-resources-live-23a34053bd59494b8178378668de9220/report.json`；同目录保留相同结构的原生参考与观察记录。两次最终验收均使用 README 的最简命令，guard 由当前输出或首个有效 2D 纹理自动选择，不额外切换事件。
- Shader 引用 D3D12：`agent/.agent-data/shader-references-live-2d78df52758f4edf9ec3500885bc9aeb/report.json`；Vulkan：`agent/.agent-data/shader-references-live-ad46b262eafa49cea6c498676d2117bb/report.json`。各目录的 `native-reference.json` / `native-observations.json` 保留独立绑定、逐阶段读取次数、采样点及恢复证据。
- D3D12 Shader 扫描恢复点的独立选择控制：`agent/.agent-data/shader-guard-discovery-763053345aab416fb9e451c91ba1f1c1/discovery.json`。该坐标经过三次无扫描重复回放和三次恢复逐位比较，另排除了会变化的坐标；未对数值采用容差。

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

# API 调用/参数与事件比较；同样只启动、关闭自建隐藏进程
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\event_inspection_live.py --launch E:/Captures/pragmata1.rdc --before 3803 --after 3820 --cbuffer-stage pixel --cbuffer-name SceneInfo --member viewProjMat

# Compute 单线程调试；完整轨迹、步数截断和无效调用验证
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\compute_debug_live.py --launch E:/Captures/pragmata1.rdc --event 3120

# GPU 计数器目录与 duration；选择经无采样重复回放验证稳定的 guard 资源
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\gpu_counters_live.py --launch E:/Captures/pragmata1.rdc --event 3820 --guard-resource 390195
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\gpu_counters_live.py --launch "E:/Captures/恋与深空尝试截帧.rdc" --event 27

# Shader 引用搜索：独立原生绑定、候选预算、缓存与真实状态恢复
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\shader_references_live.py --launch E:/Captures/pragmata1.rdc --parent-event 3738 --event 3820 --guard-resource 390180 --guard-point 340 576
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\shader_references_live.py --launch "E:/Captures/恋与深空尝试截帧.rdc" --parent-event 2712 --event 27 --guard-resource 72716

# 本轮 Pass 资源用途：原始记录逐项比较、边界/缓存/分页与零回放移动
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\pass_resources_live.py --launch E:/Captures/pragmata1.rdc --parent-event 7726
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\pass_resources_live.py --launch "E:/Captures/恋与深空尝试截帧.rdc" --parent-event 2712
```

`decompile_live.py`、`event_inspection_live.py`、`compute_debug_live.py`、`gpu_counters_live.py`、`shader_references_live.py` 和 `pass_resources_live.py` 会创建独立隐藏 GUI 进程、关闭该进程的配置写入、固定实例/session、校验产物/结果及用户配置哈希，最后只关闭自己启动的进程。不要把用户正在使用的 GUI 当成测试进程关闭。在线 Claude/Codex 验证命令及环境配置见 README；已有通过记录不代表当前账号登录状态无需检查。

## 7. 已知问题及尚不支持的能力

- 在 `pragmata1.rdc` 的更广泛 `--all-p0` 检查中，曾出现 pixel_history 与 pick_pixel 结果不一致，以及 EID 3014 的 mesh/debug_vertex 对比不一致。已用扩展前的旧 Bridge 复现，未在这几批工具中修复。基线证据为 `agent/.agent-data/analysis-live-fef62ba077cd4171bc4bb05b5e580f7b/report.json`；不要把专项通过描述为这些旧问题已解决。
- `pragmata1.rdc` EID 3820 的 GBuffer 资源 390180/390179/390177 各中心像素，在完全不调用 counters、仅重复 force replay 时也会变化。GPU 恢复验收改用经独立基线证明稳定、对采样敏感的资源 390195，保持逐位比较，未用容差掩盖差异。无采样控制证据：`agent/.agent-data/gpu-stability-control-38ec3e9a6d63463d9b4da1baf256806b/control.json`。这不意味着已修复 capture 重复回放的非确定性。
- 当前没有任意 vendor GPU counter 采样。Pass 资源用途已加入，但没有实际字节/像素依赖或引擎 RDG 图；Shader 绑定引用搜索不支持 ray shader table；Compute 调试不支持非零 dispatch base、ray/mesh Shader 调试。
- `find_actions` 仍按名称/EID 和操作 flags 搜索；Shader ResourceId 查询使用 `find_shader_references`，基于有界管线扫描，不是 `GetUsage(shaderId)`。
- 尚无 Shader 编辑/替换、CBuffer 修改、任意 Python/shell 执行、自动 Android 抓帧或远程连接管理工具。外部 `renderdoc-mcp` 的这些能力不会自动出现在内嵌助手。
- Shader 可调试性、Overlay 及 counter 支持都与具体 API/驱动/Shader 有关；反编译成功也不保证可重新编译或与原程序等价。

## 8. 下一批候选（未实现）

`get_api_calls`、`compare_events`、`debug_compute`、计数器目录与 GPU duration、`find_shader_references`、`get_pass_resources` 已完成。以下仍是候选，并未开始开发，以用户下一轮确定的范围为准。

| 候选 | 价值与已有基础 | 接手时注意 |
| --- | --- | --- |
| 更多 GPU counters | 已有计数器目录和 duration；可按实际问题选择原生/vendor 计数器 | 需按类型/宽度/单位解码，处理驱动/权限/采样兼容性，不能假设全部与 duration 同时可取 |

另一项小增强是批量局部纹理数值采样，可复用 PickPixel 并限制采样点数；无需重新实现已存在的 EXR/DDS 导出。完整 RDG 图、Shader 替换等更大改动暂不作为这一批默认范围。

新对话的最短接手顺序：读本文 → 查看当前 `git status`/`git log` 与 README → 确认用户选择的下一批范围 → 再实现、更新对应 Skill、做针对性回归和实际 capture 验证、按用户要求提交。此前本机默认 PowerShell 工具调用曾卡住，`cmd.exe` + `login:false` 更稳定；含复杂引号的命令优先用临时 Python helper 和 subprocess 参数数组。此项是本机执行经验，不是产品依赖。

# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M4 和 M5a–M5d 已实现**，支持 Codex/Claude 双 harness、本地历史保存与跨进程恢复。Claude 在线调用与续聊已验证；Codex 浏览器 OAuth 完整流程仍需人工验收。当前交接快照、验证记录和后续候选见 [CURRENT_STATUS.md](docs/CURRENT_STATUS.md)，设计过程见 [M5 设计](docs/M5_DESIGN.md)。

- M0：MCP Server、文件 mailbox IPC、fake Bridge 和协议测试。
- M1：qrendertest 启动时从 `integrations/rendertest_agent/python` 固定加载 Bridge，写实例清单和心跳，跟踪 capture 加载、关闭和选中事件，支持 `ping` / `get_status` / `get_context`。
- M2：接入 replay，提供全部 P0 工具。
- M3：4 个 Skills（`.agents/skills/rendertest-*`）、Codex 配置脚本、端到端验收脚本；MCP 工具按效果标注 `readOnlyHint`。
- M4：`pixel_history`、`debug_pixel`、`debug_vertex`、`get_mesh_data`。
- 分析工具扩展：Shader 内嵌源码读取与 release 反编译、Shader 引用搜索、资源大小统计、事件统计、CBuffer 成员查询、单 EID Overlay、API 调用参数、事件差异、Compute 线程调试和 GPU duration；当前共 34 个 MCP 工具，Codex 和 Claude 共用。

## P0 工具

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context`（含当前 EID 的 Pipeline 摘要） |
| Action | `list_actions`、`find_actions`、`inspect_event`、`get_action_stats`、`get_api_calls`、`compare_events`、`set_event`（唯一会改变 UI 的工具） |
| GPU 计数器 | `list_gpu_counters`（能力查询）、`get_gpu_duration`（回放耗时） |
| Pipeline | `get_pipeline_state`、`get_shader`（反射、反汇编，超过 256 KiB 走 artifact）、`get_shader_source`（capture 内嵌源码目录与文件）、`decompile_shader`（生成 HLSL/GLSL）、`find_shader_references`（查找绑定同一 Shader 的事件）、`get_cbuffer`（支持成员路径和子成员分页） |
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

例如：“读取当前 Pixel Shader 的 Lights 数组第 123 项 Color”、“分页查看这个常量缓冲后面的成员”、“导出当前 Draw 的深度测试和背面剔除 Overlay，解释哪些区域被挡住”。

## Shader 引用搜索

`find_shader_references(resource_id, stages=None, parent_event_id=None, cursor=None, limit=200, scan_budget=200, refresh=false)` 通过实际管线绑定查找使用指定 Shader ResourceId 的事件。先从 `get_shader`、`inspect_event` 或 `get_pipeline_state` 获得当前 capture 的 Shader ID，再作为精确十进制字符串传入。

- 默认搜索整帧；`parent_event_id` 必须是实际 action EID，搜索包含自身和后代。`stages` 可省略或传非空阶段列表，如 `["pixel"]`、`["vertex", "pixel"]`；支持现有八种阶段，重复项规范化。
- 只检查叶 draw、普通 compute dispatch 和 mesh dispatch 的相关阶段，跳过 marker/容器/copy/clear 的遗留绑定。传统 draw 不误报遗留 compute/mesh 阶段，mesh 不误报遗留 vertex 阶段；ray shader table 尚不支持，会标出覆盖缺口。返回 `references`，每个事件一行，`matches` 保留阶段、Shader ID 和入口。无反射时仍可确认绑定；入口为空时 `entryPointKnown=false`。
- `limit`（1–1000）限制返回事件行；`scan_budget`（1–2000）限制本页访问的候选数，缓存命中也计入。**空页仍可能有 `nextCursor`**，必须继续才能查全。翻页保持 resource/stages/parent，可调整两个预算。`coverage.processedCandidateCount` 是已处理的候选前缀；`scanComplete` 表示候选已扫完，`complete` 还要求没有适用的不支持项。不要只凭第一页或不完整范围判断没有引用。
- 同 capture session 缓存每个已读事件的全部相关阶段绑定，可复用到另一 Shader 或阶段的查询。`scanId`、`scanStartedAtUtc` 标识逐批建立的缓存代次，不代表整帧在同一时刻读取。`refresh=true` 成功后换代，所有旧 Shader 引用查询游标失效；不能同时传 cursor。手动替换 Shader 等未更换 capture session 的操作后需刷新，失败不覆盖旧缓存。
- 每批扫描后强制恢复当前 UI 回放事件，工具为 `read`，不改变 UI 选择。单页 256 KiB、绑定缓存 32 MiB、范围最多 100000 个 action；超过预算明确报错。MCP 等待 120 秒，单次原生 replay 不保证即时取消。`GetUsage(shaderId)` 不作为此查询的替代。
- 结果证明的是事件当时的绑定关系，不证明实际 invocation、非零工作量或源码/字节码等价。同一 Shader ResourceId 可能有不同阶段和入口；找到候选后用 `compare_events` 比较其他状态与指定 CBuffer。

例如：“找到当前 Pixel Shader 在这一帧还被哪些 Draw 使用”、“只在这个 pass 查找 Shader 6150 的引用，并比较两个事件的管线状态”。Shader ID 和 EID 都须来自当前 capture。

实际 capture 验收命令（独立隐藏 GUI；只扫描指定子树，保持用户配置）：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\shader_references_live.py --launch E:/Captures/pragmata1.rdc --parent-event 3738 --event 3820 --guard-resource 390180 --guard-point 340 576
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\shader_references_live.py --launch "E:/Captures/恋与深空尝试截帧.rdc" --parent-event 2712 --event 27 --guard-resource 72716
```

脚本与独立原生 GetShader/GetShaderEntryPoint 结果逐项比对，并观察首次及刷新各事件/阶段恰读取一次、缓存页零新增回放。恢复检查使用事先确认重复回放稳定、扫描后确有变化的采样点；并不保证 capture 的其他资源都可逐位重复。

## GPU 计数器与回放耗时

`list_gpu_counters(cursor=None, limit=200)` 查询当前 capture/replay 支持的计数器，返回 ID、名称、类别、描述、结果类型/字节宽度、单位和 UUID。按 `nextCursor` 翻页；此查询不会采样。当前只开放 `get_gpu_duration` 采样原生 `EventGPUDuration`，目录里其他计数器仍仅供能力查询；`durationSamplingSupported` 不保证驱动实际采样成功。

`get_gpu_duration(parent_event_id=None, cursor=None, limit=200, sort_by="duration", refresh=false)` 返回逐 action 的 GPU 回放耗时：

- 默认范围为整帧；`parent_event_id` 必须是实际 action EID，范围包含该 action 及所有后代。`sort_by="duration"` 按有效耗时降序、相同值按 EID 排序；`"event_id"` 按 EID 排序。`limit` 为 1–1000，每页最多 256 KiB。
- 首次查询和显式 `refresh=true` 都会原生重放**整帧**。筛选子树、排序和少量返回均不会减少采样工作。缓存当前 capture session 最近一次成功样本，检查 `sampleId`、`sampledAtUtc` 和 `cached`；翻页和换范围复用同一份结果。`refresh` 不能与游标一起使用；成功刷新后旧游标失效，失败刷新不覆盖旧样本。手动替换 Shader 等未更换 session 的操作后需要显式刷新。
- 每个 action 都有 `status`：`valid`、`invalid`、`missing` 或 `duplicate`。仅有效值提供 `durationSeconds` / `durationMs`；其余为 null 并附原因，负数、NaN/Inf、重复 EID 不当作 0。marker/容器没有测量很常见；原生报告 0 也不证明没有 GPU 工作。检查状态计数、`diagnostics` 与 warnings；未知 EID 不归并到其他 action。
- 计时来自当前设备的 RenderDoc 回放，受回放方式、GPU 频率和其他负载影响，不能当作应用原始帧时间。不同事件可能重叠或复用 alias 测量，**不要相加推导帧/Pass 墙钟耗时或 FPS**。
- 这是 `read` 操作，保持 UI 选择；采样结束后强制回放到当前 UI EID。原生采样不可立即抢占，120 秒 MCP 等待不是 native 硬超时。失败或恢复失败不会发布新样本。

本版本 D3D12 不再默认调用 `SetStablePowerState`，基础采样无需开启 Windows 开发者模式。需要固定频率时可显式启用 RenderDoc 配置 `D3D12_Replay_StablePowerState`，但必须先满足开发者模式要求；默认测量允许动态频率变化。原因见 [Microsoft API 说明](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-setstablepowerstate) 和 [时间戳频率规范](https://microsoft.github.io/DirectX-Specs/d3d/CountersAndQueries.html)。

例如：“查询当前可用的 GPU 计数器”、“测量这一帧 GPU duration，列出最慢的 20 个事件”、“用同一份样本检查 EID 123 这个 pass 下的事件”、“重新测量一遍”。EID 需要从当前 capture 中选择。

真实 capture 验证命令（独立隐藏 GUI，不调用在线模型）：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\gpu_counters_live.py --launch E:/Captures/pragmata1.rdc --event 3820 --guard-resource 390195
```

脚本记录生产调用同一次 `FetchCounters` 的原始结果，逐项核对数值、单位、范围、排序、分页和刷新；同时在回调结束时直接读取资源像素，检查强制恢复的真实回放状态。两次独立测量不要求数值相同。上述 D3D12 样本的部分 GBuffer 在不采样 counters 时重复回放也会变化，所以验收明确选择经无采样稳定性检查的资源 390195；并不保证该 capture 所有资源逐位可重复。

## API 参数与事件比较

`get_api_calls(event_id=None, api_event_id=None, parameter_path=None, cursor=None, limit=100, max_depth=0)` 读取 capture 中的结构化 API 记录，不移动 replay 或 UI。省略 `api_event_id` 时，列出 `event_id` 所属 action 的调用；非 action EID 也可作为入口，`eventId` 与 `actionEventId` 分别保留查询 EID 和 owner。指定列表中的 `api_event_id` 时读取该真实 API 调用的参数；若同时指定 `event_id`，两者必须属于同一 action。

- 根参数返回在 `parameters`；复制 `canonicalPath`（如 `[2][0][1]`）到 `parameter_path` 可读取深层成员。路径按子节点序号定位，保留隐藏字段和重复名称。`selected.value` 是选中的标量，`parameters` 分页浏览其直接子节点。
- `limit` 为 1–1000，`max_depth` 为 0–4；每页最多 4096 节点、256 KiB，路径最多 64 层。继续同级用 `nextCursor`，深入成员用路径；游标绑定 session、调用和查询范围。超长字符串只返回预览。
- 保留类型、flags、显示文本和精确 ResourceId；超过 JSON 安全整数范围的数值使用十进制字符串，GPUAddress 使用十六进制字符串。序列化 Buffer 只提供元数据，不读取其 payload。NoChunk、越界或缺失 chunk 明确 `available=false`。
- 数据来自 RenderDoc 的序列化结构，可能包含返回值或内部字段，不能保证每项都是应用原始实参。不要把 owner draw 的参数当成指定 API EID 的参数。

`compare_events(before_event_id, after_event_id, stages=None, include_bindings=true, binding_limit=64, cbuffers=None, cursor=None, limit=200)` 比较同一 capture 两个精确 EID 的归一化管线状态，包括 Shader、已使用的绑定、输入装配、目标及已暴露的光栅/深度/模板/混合状态。读取后恢复 replay，`effect=read`。

- `differences` 按 JSON Pointer `path` 返回 before/after；`present=false` 与 `value=null` 不同。容器类型变化行标记 `kind="type-change"`，容器侧用 `valueType`，不内联整个大结构。
- `comparison` 为 `different`、`equal-in-scope` 或 `indeterminate`。零条差异且覆盖不完整时不报告相等。始终检查 `coverage`、`warnings`、`totalDifferencesExact`；不完整时总数只是已观察到的差异下界。
- 范围见 `coverage.scope`：只含 viewport/scissor 0、CBuffer 数组元素 0；使用的绑定按位置匹配，`binding_limit` 为 1–256。管线阶段按 owner action 的 graphics/compute 类别筛选。资源内容、Shader 文本、未选中的常量值不参与比较。
- 需要常量值时传 `cbuffers=[{"stage":"pixel","name":"SceneInfo","member_path":"viewProjMat","max_depth":0}]`。每项恰选 `name` 或 `index`，最多 8 项；默认根路径、展开深度 1、成员上限 200，可用 `member_limit`（1–1000）调整。先用 `get_shader` / `get_cbuffer` 找到实际布局。布局和绑定变化也报告差异，跨布局的同序号成员不保证语义相同。原生解码整个 block，单块大小上限 1 MiB。
- 单侧快照限 32768 节点、2 MiB；总差异限 8192 条、2 MiB，每页限 256 KiB。深度/成员/绑定等截断均使 coverage 不完整；差异总预算耗尽后，省略部分不能靠翻页恢复。缩小比较范围后重新查询。
- 不带游标会读取新快照；续页沿用该快照。每个 session 只保留最近两组比较，重新开始相同查询或淘汰快照后，旧游标明确失效，需要从头查询。

例如：“读取这个 Draw 前的 ResourceBarrier，列出资源状态变化”、“比较 EID 3803 与 3820 的 Shader、混合和深度状态”、“比较这两个事件的 SceneInfo.viewProjMat”。EID、block 和成员名必须来自当前 capture。

本机真实 capture 验证脚本（独立隐藏 GUI，不调用在线模型）：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\event_inspection_live.py --launch E:/Captures/pragmata1.rdc --before 3803 --after 3820 --cbuffer-stage pixel --cbuffer-name SceneInfo --member viewProjMat
```

脚本用独立原生 `APIEvent` / `SDObject` 回调核对调用列表、标量和数组，用 `get_pipeline_state` / `get_cbuffer` 核对差异，并验证 UI EID、配置文件和自建进程清理。

## Shader 反编译（现成 release）

`decompile_shader(stage, event_id=None, language="auto")` 读取绑定 Shader 的原始字节码，默认优先生成 HLSL（SM 6.0）；HLSL 文本后端不支持时自动回退 GLSL（450 Vulkan），并给出原因。可显式指定 `hlsl` 或 `glsl`，此时不切换语言。`requestedLanguage` 记录请求，`outputLanguage` 记录实际输出。结果包含 `decompiledArtifact`，可用 `read_artifact` 分页或搜索；同时记录输入编码、大小、SHA-256、Shader/入口身份、release 和组件校验信息。`generated=true`、`isOriginalSource=false` 明确区分生成代码与内嵌源码。反编译无法保证恢复原始变量名、宏、注释或等价行为；翻译可能重映射绑定和接口位置，应以 `get_shader` / `get_pipeline_state` 的原始反射与实际绑定为准。

使用 [HLSL-Decompiler v0.6](https://github.com/YYadorigi/HLSL-Decompiler/releases/tag/v0.6) 的 Windows x64 预编译包，不从源码构建。安装器固定 ZIP SHA-256 `57748e4a9db0dfc40f094da8284c5e274ce45e06831c3a2fd61a844436984caa` 并核对每个文件，仅安装三个组件 CLI 及其 DLL；不安装或执行 wrapper/bat。组件版本和许可证来源保存在 `decompiler_release.py`，二进制不提交 Git。

从仓库根目录执行：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\install_shader_decompiler.py
# 离线时可用 --archive <已下载的 HLSLDecompiler.zip>；仍必须匹配固定 SHA-256
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\install_shader_decompiler.py --verify-only
```

默认安装到 `x64/Release/integrations/rendertest_agent/tools/shader_decompiler`。其他部署目录用 `--output`，并在启动 RenderTest 前设置 `RENDERTEST_SHADER_DECOMPILER_DIR`；运行时仍验证固定二进制哈希，不接受任意命令或可执行文件。非空目录只校验、不覆盖；损坏时先将该目录改名保留，再重新安装。更新 Bridge、Skill 后重启 RenderTest 并新建 AI 对话即可使用，例如：“当前 Pixel Shader 没有源码，反编译并解释颜色计算，结合实际 CBuffer 核对参数。”

支持 DXBC→DXIL→SPIR-V→文本、DXIL→SPIR-V→文本及 Vulkan SPIR-V→文本。暂不接受 GLSL/OpenGLSPIRV；该 release 的 HLSL 后端不支持 hull/domain，auto 直接选择 GLSL。HLSL 中无法表达的部分波操作也会在 auto 下回退 GLSL。其他不受支持的指令或目标特性会返回 `UNSUPPORTED` 和有界诊断，不能保证任意 Shader 都能转换成功。取消、超时、超限、缺少工具或字节码转换失败不会触发语言重试；失败时保留反汇编分析路径，不把反汇编冒充源码。

反编译为 `artifact` 操作：先短暂回放并复制字节码、恢复事件，再在 Bridge worker 执行固定参数的进程。UI 选择和 capture 保持不变；总转换硬超时 60 秒，输入/中间产物/生成文本上限 16 MiB、单步日志上限 1 MiB，结束时清理临时文件及进程。该实例的其他 MCP 请求会排队；面板 Stop 不保证立即中断已开始的转换，Bridge 退出或请求超时会终止进程。

## Shader 调试与几何工具（P1）

| 工具 | 说明 |
| --- | --- |
| `pixel_history` | 截至目标 EID 修改过某个像素的事件，默认新到旧，按事件分组：片元数、通过数、失败原因计数（深度/模板/discard/裁剪…）、通过片元的 primitive、`preMod`/`shaderOut`/`postMod`（color/depth/stencil）。`summary` 给出最后通过和最后改变值的 EID；`include_fragments=true` 每个事件附最多 64 个片元。RenderDoc 的像素历史不含 `Discard`，这类事件放在 `discardEventIds` 里并给出 warning。结果按 capture session 缓存，翻页不会重放。 |
| `debug_pixel` | 用 RenderDoc Shader 调试器跑某个 Draw 在某像素的 Pixel Shader（`primitive` 选重叠片元，可取自 `pixel_history` 的 `primitiveIds`）。返回 Shader、插值输入、最终输出（`SV_Target*`，混合和格式转换之前）、最后改变的变量、产生 NaN/Inf 的步骤（指令和行号）；完整逐步 trace 写成 JSONL artifact（每步 `instruction`、`line`、变量 `changes`，上限 64 MiB）。`max_steps` 默认 20000。没有片元覆盖该像素时返回 `debugged=false`。 |
| `debug_vertex` | 调试某个 Draw 的某个顶点的 Vertex Shader。`vertex` 是 Draw 内 0 起的序号（Mesh Viewer 的 VTX 列，小于 `numIndices`），`instance` 默认 0。Indexed Draw 由 Bridge 读 index buffer，按 Mesh Viewer 的算法加 base vertex 得到 `index`（另返回原始 `indexBufferValue`，遇到 primitive restart 报错）。返回字段与 `debug_pixel` 相同，`outputs` 含 `SV_Position`（裁剪空间）。 |
| `debug_compute` | 调用原生 `DebugThread` 调试一个 Compute Dispatch invocation，必填三维 `group`（工作组坐标）和 `thread`（组内线程坐标）。返回全局线程坐标、dispatch/线程组大小及其来源、输入、最后寄存器变化和 JSONL trace。普通及已解析的 indirect 子 Dispatch 可用；严格核对坐标和可调试性，不支持 ray/mesh dispatch、非零 dispatch base 或未知线程组大小。 |
| `get_mesh_data` | 读 Draw 变换后的顶点，对应 Mesh Viewer 的 VS Out（`stage="vs_out"`，默认）/ GS Out（`stage="gs_out"`）表。返回输出列（位置在前，布局与 Mesh Viewer 相同）、全 Draw 的每列 min/max 和非有限值行数、位置的 NDC 范围和 w≤0 的顶点数；`rows` 每行含 `vertex`、`index`（实际取的顶点，已加 base vertex）和各列值，用 `start`/`limit` 或 `cursor` 翻页（默认 50 行）。一次回放后按 capture session 缓存。暂不支持 Mesh Shader。 |

`debug_compute(group, thread, event_id=None, max_steps=20000)` 的两个坐标都是从 0 开始的三元素 UInt32 数组；不是像素坐标。`group[i] < dispatchDimensions[i]`，`thread[i] < threadGroupSize[i]`，全局坐标 `globalThread[i] = group[i] * threadGroupSize[i] + thread[i]`。线程组大小按 GUI 的规则优先读取 action override，否则读取 Shader 反射，结果中用 `threadGroupSizeSource` 标明来源。必须指定实际执行的 Dispatch action EID，不能用前面的 setup API EID 或 indirect 父容器替代。

- `inputs` 可包含 Shader 使用的 `SV_DispatchThreadID` 等系统输入；未使用的系统值可能被省略。`lastChanges` 是调试器变量/寄存器，`nanOrInfSteps` 和 `sampleSteps` 汇总执行过程中观察到的相关事件。
- Compute 通常没有 output signature，`outputs=[]` 不表示没有资源写入。轨迹不是 UAV 写入清单，也不能证明整次 Dispatch 输出、跨线程次序、无竞争或 GPU 耗时。需要核对实际资源时，结合绑定与 `pick_pixel` / `get_buffer_data` 等工具读取结果。
- `max_steps` 为 1–200000；`executionComplete=false` 表示仅完成有界调试，结果为部分状态。`traceArtifact` 使用现有 JSONL schema，上限 64 MiB；`traceComplete=false` 表示文件省略了后续步。用 `read_artifact` 按行或寄存器搜索。`debugged=false` 表示原生调试器没有提供可用轨迹，API/Shader 不支持时明确报错，不伪造成功。
- 调试是 `artifact` 操作，trace 在 `finally` 释放，回放事件随后恢复，不改变 UI 选择或修改 capture。MCP 等待时间为 120 秒；底层 `DebugThread` / `ContinueDebug` 调用不可立即抢占，不能把它理解为硬超时或即时 Stop。

本机样本可直接问：“调试 EID 3120 的 group `[0,1,0]`、thread `[1,0,0]`，解释这条线程的计算。”该样本组数为 `[1,2,1]`，线程组大小为 `[64,1,1]`，全局线程 ID 应为 `[1,1,0]`；其他 capture 需重新定位。专项验收使用独立原生 DebugThread 参考，并检查零/非零坐标、步数截断、无效调用拒绝及 trace 读取：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\compute_debug_live.py --launch E:/Captures/pragmata1.rdc --event 3120
```

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
  devtools/smoke_live.py            # 常规 P0 冒烟；可选反编译依赖由 decompile_live 单独验证
  devtools/install_shader_decompiler.py # 校验并安装固定的预编译 release
  devtools/decompile_live.py        # 独立 capture 进程验证反编译与 artifact 读取
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
- `rendertest-event-analysis`：解释 EID/pass 的输入、计算和输出，统计事件及资源大小，查询计数器和测量 GPU duration。
- `rendertest-shader-debug`：Shader 内嵌源码、生成 HLSL/GLSL、反射、绑定、CBuffer 和反汇编，`debug_pixel` / `debug_vertex` / `debug_compute` 单线程调试。
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
# 已安装反编译 release：验证绑定 VS/PS 的 auto/GLSL 及文本 artifact
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\decompile_live.py --launch "E:\Captures\some.rdc" --event 3820
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

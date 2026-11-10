# RenderTest AI Assistant 使用指南

适用于 Windows x64 试用包，功能说明更新于 **2026-11-10**。当前提供 Codex、Claude Code 两种 AI 后端、35 个分析工具和 4 个分析 Skill。

AI Assistant 可以读取 RenderTest 当前打开的 RenderDoc capture，用自然语言解释事件、Shader、资源和像素问题。你可以继续使用熟悉的 Event Browser、Texture Viewer 和 Mesh Viewer，再把选中的对象交给 AI 分析。

## 1. 首次使用

### 准备内容

- Windows 10/11 x64，以及能够正常回放目标 capture 的 GPU 和驱动。本试用包已在开发机 Windows 环境验证，其他机器的驱动/API 支持仍需实际检查。
- 一个可在 RenderTest 中打开的 `.rdc` 文件。
- 自己可用的 Codex 或 Claude 服务账号/凭据及网络连接。模型访问权限、额度和费用由所选服务决定。

压缩包包含 RenderTest、Bridge、MCP 所需的 Python 和依赖、4 个 Skill，以及 Shader 反编译工具。**不需要另外安装 Python**。Codex CLI、Claude Code CLI、模型账号和登录凭据不随包提供。

本包包含 Windows x64 回放与分析程序，以及 Android ARM64/ARM32 截帧服务 APK（`plugins/android/`）。不包含 Windows 32 位应用捕获所需的支持文件。Android SDK/ADB 和 JDK 需自行配置。

模型会接收到你的提问、随问题附带的上下文，以及工具返回的 capture 数据；需要看图时也可能收到导出的图片。请使用允许发送给所选 AI 服务的 capture。

### 解压并启动

1. 将压缩包完整解压到较短的本地目录，例如 `D:\Tools\RenderTest`。ZIP 内的顶层目录叫 `RenderTest`，保留其中的文件和子目录结构；避免多层长文件夹名，旧 GUI 内嵌运行时可能受 Windows 长路径限制影响。
2. 双击根目录的 **`qrendertest.exe`**，程序自动发现随包的 `runtime\python\python.exe`。也可用 **`Start-RenderTest.cmd`**，它会先进行离线环境检查再启动。
3. 在 RenderTest 中打开 `.rdc`，等 capture 加载完成，确认能正常选择事件和查看纹理。
4. 从 **Window → AI Assistant** 打开面板。
5. 点击 **Settings**，选择已安装并认证的后端；模型先保留默认即可。
6. 在 Event Browser 选择一个 Draw 或 Dispatch，发送：

   > 请先确认当前 capture 和 EID，再解释这个事件在做什么、读取哪些资源、输出到哪里。请区分实测数据和推断。

第一次发问时会启动后端并连接工具。面板顶部显示 `Ready`，连接正常时会显示 `rendertest` 和工具数量；生成回答时显示 `Working`。工具卡片出现实际调用结果，说明 AI 已开始读取 capture。

根目录的 `README.html` 可直接用浏览器阅读；本文件是 Markdown 原稿。2026-11-10 修复包支持直接打开 EXE，Settings 中 **MCP Python executable** 保持 `Automatic detection` 即可。显式配置的路径或 `RENDERTEST_MCP_PYTHON` 环境变量仍优先；如保存过失效的旧路径，请清空后使用自动发现。旧的 2026-11-09 包需要通过 `Start-RenderTest.cmd` 启动，或手动选择包内的 `runtime\python\python.exe`。

### Android 设备截帧

1. 准备已开启 USB 调试并授权本机的 ARM Android 设备，确认 `adb devices` 显示为 `device`。目标应用应为可调试的开发包。
2. 在 RenderTest 的 **Settings → Android** 配置 Android SDK root path 和 JDK 路径（也可使用已配置的环境变量）。SDK 需要包含 platform-tools 和 build-tools。
3. 在主窗口左下角的远程上下文下拉框中选择设备。RenderTest 会从 `plugins/android/` 查找对应 APK 并安装、启动截帧服务；按手机提示授予所需权限。
4. 连接后选择目标应用并启动，按正常截帧流程触发捕获。把 capture 保存到本地后，可用 AI Assistant 继续分析；回放仍取决于图形 API 和设备/驱动支持。

APK 文件名仍为 `org.renderdoc.renderdoccmd.arm64.apk` 和 `org.renderdoc.renderdoccmd.arm32.apk`，这是 PC 端查找与安装协议使用的名称，请保留原名及目录结构。手机上应用显示名仍为 RenderDocCmd。这批只提供两种 ARM 包，不包含 x86/x64 Android 模拟器包。

APK 与 PC DLL 必须来自配套构建，程序会严格校验版本。AI Assistant 当前没有自动连接 Android 或自动截帧工具；设备连接和捕获通过主窗口完成。

## 2. 选择后端并登录

两个后端使用同一套 RenderTest 分析工具。可以先选择你已有可用账号和本机 CLI 的后端。

| 项目 | Codex | Claude Code |
| --- | --- | --- |
| Settings → Backend | `Codex` | `Claude Code` |
| 本机程序 | Windows 原生 `codex.exe` | Windows 原生 Claude Code 可执行文件 |
| 登录入口 | CLI 登录；面板也提供 `Log in` 浏览器登录入口 | 在外部 CLI 或服务环境中完成认证；面板没有 Claude 浏览器登录按钮 |
| 模型和推理强度 | 独立保存 Codex 的选项 | 独立保存 Claude 的选项 |
| 工具隔离 | 默认开启，可在 Settings 查看 | 固定开启，只允许 RenderTest 工具 |

### Codex

按 [OpenAI 官方 Codex CLI 指南](https://learn.chatgpt.com/docs/codex/cli) 安装 Windows CLI。本集成支持常见的 npm 安装布局；使用该方式时，先准备 Node.js/npm，再在终端执行：

```powershell
npm install -g @openai/codex
codex --version
codex login
codex login status
```

`codex login` 会引导浏览器登录；已有可用的 CLI 认证时，面板会复用它。其他认证方式请按 [OpenAI 官方认证说明](https://learn.chatgpt.com/docs/auth) 配置，不需要把凭据粘贴进聊天框。

回到 Settings，选择 `Codex`。**Codex executable** 留空会尝试自动查找；查找失败时使用 Browse 选择真正的 `codex.exe`，不要选择 `codex.cmd`、`codex.ps1` 或 WSL 内的程序。

如果面板显示 `Not logged in`，也可点击 **Log in**，在浏览器完成后返回面板重新发送问题；等待期间按钮会变为 **Cancel login**。面板浏览器登录流程仍需更多真实账号环境验证，遇到问题优先在 CLI 完成登录，再重新启动 RenderTest。

### Claude Code

按 [Claude Code 官方安装说明](https://code.claude.com/docs/en/setup) 安装 Windows 原生 CLI，然后在终端检查并登录：

```powershell
claude --version
claude auth login
```

也可以使用你已配置好的、Claude Code 支持的 API 认证或服务连接；认证方式见 [Claude Code 官方认证说明](https://code.claude.com/docs/en/authentication)。当前集成已验证 Claude 在线工具调用和历史续聊，但具体账号、网关和模型仍取决于你的服务配置。

回到 Settings 选择 `Claude Code`。**Claude Code executable** 可先留空；未找到程序时，用 Browse 指向已安装的原生可执行文件，不能选择 `.cmd`、`.bat` 或 `.ps1` 启动包装脚本。

Claude 认证在 RenderTest 外完成。看到认证错误时，先在外部 CLI 修复，再重新发送问题或重启 RenderTest。Settings 不保存 API Key 或 Token。

已有自定义服务连接时，当前集成会继承进程环境，也会读取用户级 Claude `settings.json` 中允许的连接环境项，例如 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 和模型别名。项目级 hooks、插件或额外工具配置不会因此启用。若只在某个终端临时设置了环境变量，应从该终端运行 `Start-RenderTest.cmd`；桌面启动的程序不会自动获得另一个终端的临时变量。

## 3. 模型设置

打开 **Settings** 可调整以下内容：

| 设置 | 使用方式 |
| --- | --- |
| Backend | 选择 Codex 或 Claude Code。切换后端会保存原聊天并开始新聊天。 |
| Codex executable / Claude Code executable | 自动检测失败时指定相应原生程序。 |
| MCP Python executable | 使用启动脚本时通常不用改；直接启动 EXE 时指定随包的 `runtime\python\python.exe`。 |
| Model | 打开设置会向当前后端查询模型。可选择列表项，也可填写服务实际支持的模型 ID。`Follow … configuration` 表示跟随后端配置。 |
| Refresh | 重新查询模型列表。只查询目录，不会发送一轮 capture 分析。 |
| Reasoning effort | `Default` 跟随后端默认。已知模型按其报告的支持选项选择；不要假定所有模型都有相同档位。 |
| Isolate other MCP servers and optional capabilities | Codex 默认勾选，试用时保留即可；Claude 固定勾选。面板中的 shell 工具始终禁用。 |

Codex 与 Claude 的模型、推理强度分别保存，切换后端不会相互覆盖。设置保存后会断开当前连接，在下一次发问时重新连接；正在回答或登录时 Settings 暂不可用。若环境变量覆盖了设置，窗口会显示提示，此时应先检查启动环境。

模型列表为空不一定是模型服务不可用：先确认 CLI 路径和认证，再点 Refresh；也可以填写已确认可用的模型 ID。列表里出现某个模型，也不保证当前账号一定具有调用权限。

## 4. 日常使用

### 直接提问

在输入框输入问题，**Enter 发送，Shift+Enter 换行**。也可点击 Send。

输入框附近的 **Current EID** 标签表示将随问题发送当前事件；点击标签可移除，点击 **+ Current EID** 可恢复。每次发送会记录当时的上下文，因此发问后即使继续浏览其他事件，原问题也有明确的目标。

无需手写工具参数。涉及具体事件或资源时，可以使用 `EID 123`、`ResourceId::456` 这样的表达。回答中的有效 EID/资源链接可以点击跳转；这属于你主动操作界面。

### 从界面选择对象再 Ask AI

| 位置 | 操作 | 自动附带的信息 |
| --- | --- | --- |
| Event Browser | 右键事件 → **Ask AI about this event** | 事件 EID |
| Texture Viewer 的资源菜单 | **Ask AI about this resource** | 资源与当前事件 |
| Texture Viewer 像素信息区 | 选取像素后点击 **Ask AI** | 资源、EID、像素坐标及 mip/slice/sample、类型解释等 |
| Mesh Viewer 顶点表 | 右键顶点 → **Ask AI about this Vertex** | 事件、顶点及实例等 |

Ask AI 会把问题和上下文放进输入区，**不会直接发出请求**。先检查标签和问题，按需要修改，再发送。像素问题优先用这个入口，能避免把缩放后的屏幕坐标误当成纹理坐标。

### 看工具结果和图片

回答过程中会出现工具卡片，展示调用状态、参数和结果摘要；可展开查看细节。纹理或 Overlay 导出成功后，支持的 PNG 会显示缩略图，可点击查看。较长的 Shader 文本和调试轨迹会保存为本地文件，AI 使用 `read_artifact` 分页或搜索。

默认分析会保留 UI 当前选中事件。只有你明确要求“跳到这个事件”等操作时，AI 才应调用 `set_event`。其他工具可能临时回放别的事件、导出文件，但不会修改 capture 内容。

### 停止和新聊天

- **Stop**：停止当前回答。正在执行的底层回放/调试调用可能需要返回后才能结束，不保证立即中断。
- **New Chat**：保存当前聊天并开始新的聊天，适合换分析问题或重新整理上下文。
- 后端意外退出时，面板会显示错误；下一次发问会尝试重新启动。先查看错误提示，再决定重试或 New Chat。

## 5. 历史记录与续聊

点击 **History**，可按聊天标题、capture、模型或后端搜索并打开历史。聊天会自动保存到当前 Windows 用户的：

```text
%LOCALAPPDATA%\RenderTest\agent\conversations\
```

历史记录不保存在解压目录中，升级或重新解压程序不会自动搬走历史；试用压缩包也不包含打包者的历史或凭据。

打开历史后，只有原后端和 capture 身份匹配、后端原会话可恢复时，才能继续发问。capture 的路径、大小、修改时间和内容校验都参与匹配。把 `.rdc` 移到别的路径、覆盖文件，或改用另一个后端，可能使旧聊天只能阅读。

切换/关闭 capture 会停止原绑定的会话，避免对新的 capture 使用旧 EID。历史里的旧 capture 链接保持失效；恢复后让 AI 重新查询当前数据，会得到可用的新引用。远程、临时或无法验证身份的 capture，不保证跨进程续聊；需要时保存为本地文件并开始新聊天。

同一条历史若被另一个窗口占用，会只读打开，避免互相覆盖。恢复失败时仍可阅读已保存内容，再用 New Chat 开始。纯聊天历史与 capture 分析历史也分别核对上下文。

## 6. 可以直接复制的提问

下面的 EID、ResourceId、坐标都要换成当前 capture 的实际值；使用 Ask AI 时通常无需手填。

| 想解决的问题 | 提问示例 |
| --- | --- |
| 理解一个事件 | “解释当前 EID 的用途。列出关键输入、Shader 和输出，先给证据，再给你的判断。” |
| 了解一个 pass | “总结选中 pass 的 Draw/Dispatch。列出读、写和读写资源，以及边界前后的相邻用途；不要把相邻使用直接当成依赖关系。” |
| 找帧中重要部分 | “概览这一帧的主要 pass，再挑出需要进一步检查的三个事件。” |
| 比较两个事件 | “比较 EID 120 和 EID 135 的管线、绑定和常量差异，解释哪些差异可能影响结果。” |
| 查看原始调用参数 | “查看这个 Draw 附近的 API 调用，展开 Copy/Clear/Barrier 的参数并保留原始 API EID。” |
| 查 Shader | “先找当前 Pixel Shader 的内嵌源码；没有时尝试生成可读 HLSL/GLSL，并明确哪些结论来自反编译。” |
| 找同一 Shader 的引用 | “查找绑定 ResourceId::456 这个 Shader 的事件，先限定在当前 pass；完成所有分页后再汇总。” |
| 查常量 | “读取当前 Shader 的这个常量缓冲，继续展开 Lights[3].Color，不要只看开头的摘要。” |
| 查错误像素 | “这个像素为什么是黑色？先读取实际值，再查历史中最后改变它的事件，必要时调试 Pixel Shader。” |
| 查顶点 | “解释选中顶点的变换过程，检查裁剪空间坐标和 w 是否合理。” |
| 查 Compute | “先确认当前 Dispatch 的工作组尺寸，再调试 group [0,0,0]、thread [0,0,0]，说明这条线程的计算。” |
| 看诊断图 | “导出当前 Draw 的 Wireframe 和 Depth Overlay，解释图例与被深度测试挡住的区域。” |
| 看资源大小 | “统计纹理和 Buffer 的已知大小，列出最大的 10 个资源，并注明未知大小。” |
| 看回放耗时 | “查询可用 GPU counters，再测量当前 pass 各事件的 GPU duration。按耗时排序，不要把逐事件耗时相加当作真实帧时间。” |

复杂问题建议分两步：先让 AI 做小范围概览，再挑具体事件/像素深入。也可以直接要求“先不要做调试”“最多检查这一个 pass”“先给我证据表”。

## 7. 35 个工具速查

这些工具由 AI 按问题调用。表中的名称也可以写进问题，帮助它选择分析方式。

| 用途 | 工具 | 能做什么 |
| --- | --- | --- |
| 连接与上下文 | `list_instances` | 列出可访问实例；内嵌面板固定到本窗口。 |
| 连接与上下文 | `get_status` | 检查连接、capture 和工具能力。 |
| 连接与上下文 | `get_context` | 读取当前选中事件和管线摘要。 |
| 事件 | `list_actions` | 分页浏览 action/marker 树。 |
| 事件 | `find_actions` | 查找符合条件的事件。 |
| 事件 | `inspect_event` | 查看某个事件的 Draw/Dispatch、附件等详情。 |
| 事件 | `get_action_stats` | 统计整帧或子树的事件类型和工作量。 |
| 事件 | `get_api_calls` | 查看关联的原始 API 调用与结构化参数。 |
| 事件 | `compare_events` | 比较两个事件的管线/绑定及选定常量缓冲。 |
| 事件 | `set_event` | 按用户要求改变 UI 当前事件。 |
| GPU 耗时 | `list_gpu_counters` | 列出当前回放可用的计数器描述。 |
| GPU 耗时 | `get_gpu_duration` | 采样逐事件 GPU duration，并按范围筛选、排序。 |
| Shader 与管线 | `get_pipeline_state` | 查看 Shader 阶段、资源绑定和固定管线状态。 |
| Shader 与管线 | `get_shader` | 读取 Shader 反射与反汇编。 |
| Shader 与管线 | `get_shader_source` | 列出或导出 capture 内嵌源码。 |
| Shader 与管线 | `decompile_shader` | 从支持的字节码生成 HLSL/GLSL。 |
| Shader 与管线 | `find_shader_references` | 分批查找绑定同一个 Shader 的事件。 |
| Shader 与管线 | `get_cbuffer` | 查看常量缓冲，按成员路径钻取和分页。 |
| 资源 | `list_resources` | 查找纹理、Buffer、Shader 等资源。 |
| 资源 | `get_resource` | 查看资源属性。 |
| 资源 | `get_resource_usage` | 查看单个资源在各原始 EID 的用途。 |
| 资源 | `get_resource_stats` | 统计纹理/Buffer 数量、已知大小和分组。 |
| 资源 | `get_pass_resources` | 汇总 pass/原始 EID 范围的资源用途及前后相邻使用。 |
| 纹理与像素 | `pick_pixel` | 读取指定像素及子资源的数值。 |
| 纹理与像素 | `get_texture_stats` | 读取指定纹理子资源的各通道 min/max。 |
| 纹理与像素 | `export_texture` | 导出 PNG、EXR 或 DDS。 |
| 纹理与像素 | `export_overlay` | 导出单事件诊断 Overlay PNG。 |
| Buffer | `get_buffer_data` | 读取 Buffer 字节，可按给定格式解释。 |
| 诊断信息 | `get_debug_messages` | 查看 capture/回放的诊断消息。 |
| 深入调试 | `pixel_history` | 查看像素修改历史、通过/失败原因及片元信息。 |
| 深入调试 | `debug_pixel` | 调试一个 Pixel Shader invocation。 |
| 深入调试 | `debug_vertex` | 调试 Draw 中的一个顶点。 |
| 深入调试 | `debug_compute` | 调试一个 Compute 工作组内的一条线程。 |
| 深入调试 | `get_mesh_data` | 查看变换后的顶点数据和统计。 |
| 导出结果 | `read_artifact` | 分页或搜索本次工具会话生成的文本文件。 |

## 8. 4 个 Skill 如何工作

Skill 是随程序提供的分析流程说明，帮助 AI 先确认目标、按合适顺序取证，并说明工具限制。它们不是额外的模型，也不是四个需要单独打开的面板。

| Skill | 主要用途 |
| --- | --- |
| `rendertest-core` | 当前窗口/capture 绑定、工具效果、引用格式及证据规则。 |
| `rendertest-event-analysis` | 事件、pass、API 参数、资源用途、统计和 GPU duration。 |
| `rendertest-shader-debug` | Shader 源码/反编译、绑定与常量、引用查找、单 invocation 调试。 |
| `rendertest-pixel-investigation` | 像素取值、历史、最后修改事件和颜色问题。 |

Codex 通过自己的 Skill 机制发现随包目录，按任务使用；Claude 集成将这 4 份说明直接加入其系统上下文，内置 Skill/文件/shell 工具不开放。两者都使用同一套 RenderTest 工具，不需要你手动把 Skill 安装到个人目录。

正常情况下直接提问即可。出现 `RenderTest skills were not found` 时，检查是否完整解压、是否移动了 `integrations\rendertest_agent\skills`；不要只复制单个 EXE。

## 9. 读结果时需要知道的边界

- **结论仍需证据。** EID、ResourceId、数值、工具卡片和导出图片是核对入口；没有拿到的数据应标为未知，AI 的解释可能有误。
- **作用范围要读完整。** 列表和扫描会分页；空的一页不一定代表没有结果。有 `nextCursor`、`truncated` 或 `complete=false` 时，需要继续读取或缩小问题，不能直接得出“全帧都没有”。
- **Shader 源码与生成代码不同。** capture 可能没有内嵌源码；反编译生成的 HLSL/GLSL 不等于原工程源码，不能恢复注释和全部名称，也不保证可以直接编译回等价 Shader。自动模式可从 HLSL 回退到 GLSL。
- **用途不等于实际访问。** Shader 绑定查找证明的是绑定；pass 资源用途描述的是原生角色/潜在读写。DepthStencilTarget 可能只读测试，Discard 是内容失效，前后相邻用途不证明数据依赖；不提供精确 subresource/字节范围的完整依赖图。
- **统计口径不同。** 资源大小不是去重后的实际驻留显存；Draw/Dispatch 工作量不是 GPU 耗时。GPU duration 来自回放，首次查询会对整帧采样再筛选；不能把逐事件值简单求和当作原始应用帧时间。目前只采样 duration，其他 counter 仅查询能力。
- **图片可能经过显示变换。** PNG、Overlay、深度拉伸和缩放是可视化结果；需要准确数值时用 `pick_pixel`，不要直接从显示颜色反推原值。
- **调试支持依赖 API、Shader 和驱动。** Mesh/ray 调试、部分 Overlay、不同 Vulkan 像素类型解释等可能返回 `UNSUPPORTED`。Compute 调试只观察一条线程，不能代表整个 Dispatch 的 UAV 最终写入或线程间顺序。
- **耗时操作不保证即时停止。** Shader 调试、像素历史、GPU 采样等底层调用可能较久；步数/响应预算限制不等于每个原生调用都有硬超时。
- **旧导出路径可能需要重建。** `read_artifact` 只读取同一工具会话登记过的文件。后端重启或恢复历史后，可能需要重新运行生成工具；它不是任意本地文件浏览器。

## 10. 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 AI Assistant 菜单 | 确认运行的是试用包里的 RenderTest，而不是其他安装的 RenderDoc。 |
| 提示找不到 Codex / Claude 程序 | 先在终端确认相应 `--version` 可运行；重启 RenderTest 使其获得新 PATH；仍失败就在 Settings 指定原生 EXE。 |
| 提示找不到 MCP Python / `rendertest_mcp` | 退出后用 `Start-RenderTest.cmd` 启动；检查 `runtime\python\python.exe` 和 integrations 目录是否完整。不要指向一个未安装所需依赖的系统 Python。 |
| Bridge 未启动，报缺少某个 Python 模块，但文件实际存在 | 先检查解压路径是否过深/过长；完整移动到 `D:\Tools\RenderTest` 这类短目录，再用启动脚本重试。 |
| `Not logged in` 或 Claude authentication 错误 | 按上文在相应 CLI 完成认证。Claude 在外部登录，没有面板 Log in 按钮；终端配置的临时环境需由同一终端启动程序。 |
| 认证正常但模型请求失败 | 检查所选模型 ID、账号访问权限、服务地址和网络。先恢复 Follow configuration / Default，再重试。 |
| 模型下拉框只有默认项 | 点击 Refresh，检查 CLI/认证错误；可手填服务确认支持的模型 ID。 |
| 显示纯聊天、没有 capture 工具 | 确认当前窗口已加载 capture。没有安全绑定时工具会禁用；AI 不会自动借用另一个窗口。 |
| `CAPTURE_CHANGED` / `CAPTURE_MISMATCH` | capture 已切换或重新加载。使用 New Chat，或在正确 capture 中重新打开对应历史；不要复用旧 EID、游标或导出路径。 |
| `BRIDGE_BUSY` / `TIMEOUT` | 等待当前原生任务完成，先缩小事件、资源或像素范围再重试。Stop 可能要等待底层调用返回。 |
| `UNSUPPORTED` | 当前 API、Shader 或输入不支持该能力。可让 AI 改用管线、资源、数值或反汇编等已有证据。 |
| `LIMIT_EXCEEDED` | 缩小范围，改为分页/成员查询，或减小图片尺寸、调试步数。 |
| 旧历史只能阅读 | 检查后端、capture 路径/内容以及是否被另一窗口占用；身份无法匹配时新建聊天。 |
| Skill 缺失、隔离校验失败或工具限制未确认 | 检查包是否完整、CLI 版本是否兼容。保留完整错误信息交给提供试用包的人，避免通过放开额外工具来绕过问题。 |
| `REPLAY_ERROR` 或回放设备出错 | 先检查 RenderTest 本身的回放是否正常；必要时关闭并重新打开 capture，记录 API/GPU/驱动和具体操作。 |

反馈问题时，附上试用包版本、Windows/GPU/驱动、后端及 CLI 版本、错误原文、问题描述，以及可公开的 EID/资源信息。日志通常位于：

```text
%LOCALAPPDATA%\RenderTest\agent\logs\
```

其中 `chat-*.log` 对应 AI 会话，`bridge-*.log` 对应 capture 工具桥接。分享日志前检查其中是否包含不宜公开的 capture 名称、路径或内容；不需要提供账号密码、API Key、Token 或整份认证文件。

# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M4 P1 调试工具（进行中：`pixel_history`、`debug_pixel` 已完成）**。

- M0：MCP Server、文件 mailbox IPC、fake Bridge 和协议测试。
- M1：qrendertest 启动时从 `integrations/rendertest_agent/python` 固定加载 Bridge，写实例清单和心跳，跟踪 capture 加载、关闭和选中事件，支持 `ping` / `get_status` / `get_context`。
- M2：接入 replay，提供全部 P0 工具。
- M3：4 个 Skills（`.agents/skills/rendertest-*`）、Codex 配置脚本、端到端验收脚本；MCP 工具按效果标注 `readOnlyHint`。
- M4：`pixel_history`、`debug_pixel` 已完成；`debug_vertex`、`get_mesh_data` 待做。

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

## P1 工具（M4）

| 工具 | 说明 |
| --- | --- |
| `pixel_history` | 截至目标 EID 修改过某个像素的事件，默认新到旧，按事件分组：片元数、通过数、失败原因计数（深度/模板/discard/裁剪…）、通过片元的 primitive、`preMod`/`shaderOut`/`postMod`（color/depth/stencil）。`summary` 给出最后通过和最后改变值的 EID；`include_fragments=true` 每个事件附最多 64 个片元。RenderDoc 的像素历史不含 `Discard`，这类事件放在 `discardEventIds` 里并给出 warning。结果按 capture session 缓存，翻页不会重放。 |
| `debug_pixel` | 用 RenderDoc Shader 调试器跑某个 Draw 在某像素的 Pixel Shader（`primitive` 选重叠片元，可取自 `pixel_history` 的 `primitiveIds`）。返回 Shader、插值输入、最终输出（`SV_Target*`，混合和格式转换之前）、最后改变的变量、产生 NaN/Inf 的步骤（指令和行号）；完整逐步 trace 写成 JSONL artifact（每步 `instruction`、`line`、变量 `changes`，上限 64 MiB）。`max_steps` 默认 20000。没有片元覆盖该像素时返回 `debugged=false`。 |

约定：

- 带 `event_id` 的只读工具临时切 replay EID，读完恢复，不改变 UI 选中事件；省略时用当前 EID。
- 列表工具都分页：`limit` 1–1000（默认 200），`nextCursor` 绑定 capture session 和查询条件。
- 绑定条目里 `name` 是 Shader 反射中的绑定名，`resourceName` 是资源名。
- artifact 写到 `<root>/artifacts/<instanceId>/<captureSessionId>/replay/`，结果里带路径、大小和 SHA-256。

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
  tests/                            # 协议、实例选择、工具契约和 stdio 冒烟测试
  docs/MVP_DESIGN.md
```

仓库根目录的 `.agents/skills/` 下有 4 个 Skill，在本仓库里运行 Codex 时会被发现。Skill 正文用英文（模型仍按用户语言回答）：Codex 在 Windows PowerShell 下读无 BOM 的中文 UTF-8 文件会乱码。

- `rendertest-core`：目标选择、capture/session 身份、工具效果、实测与推断的区分。
- `rendertest-event-analysis`：解释 EID/pass 的输入、计算和输出。
- `rendertest-shader-debug`：Shader 反射、绑定、CBuffer 和反汇编。
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

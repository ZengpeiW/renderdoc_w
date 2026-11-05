# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M2 P0 只读工具**。

- M0：MCP Server、文件 mailbox IPC、fake Bridge 和协议测试。
- M1：qrendertest 启动时从 `integrations/rendertest_agent/python` 固定加载 Bridge，写实例清单和心跳，跟踪 capture 加载、关闭和选中事件，支持 `ping` / `get_status` / `get_context`。
- M2：接入 replay，提供全部 P0 工具。

## P0 工具

| 分类 | 工具 |
| --- | --- |
| 实例/上下文 | `list_instances`、`get_status`、`get_context`（含当前 EID 的 Pipeline 摘要） |
| Action | `list_actions`、`find_actions`、`inspect_event`、`set_event`（唯一会改变 UI 的工具） |
| Pipeline | `get_pipeline_state`、`get_shader`（反射、反汇编，超过 256 KiB 走 artifact）、`get_cbuffer` |
| 资源 | `list_resources`、`get_resource`、`get_resource_usage` |
| 纹理 | `pick_pixel`、`get_texture_stats`、`export_texture`（png/exr/dds，depth PNG 自动按 min/max 拉伸） |
| Buffer | `get_buffer_data`（64 KiB 内 base64，1 KiB 内附 hex，更大写 artifact；`format="float4, uint"` 可解码行） |
| 消息 | `get_debug_messages` |

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
  devtools/live_lifecycle.py        # 启动 qrendertest，自动验收加载/选中/关闭/重载/退出
  devtools/ui_lifecycle_script.py   # live_lifecycle.py 使用的 --ui-script
  tests/                            # 协议、实例选择、工具契约和 stdio 冒烟测试
  docs/MVP_DESIGN.md
```

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

# 自动跑完整生命周期
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\live_lifecycle.py --capture "E:\Captures\some.rdc"
```

## 用 fake Bridge 手动试用

终端 1：

```powershell
cd agent
$env:RENDERTEST_AGENT_ROOT = "$PWD\.agent-data"
.\mcp_server\.venv\Scripts\python.exe devtools\fake_bridge.py
```

终端 2 中把 MCP Server 配置给 Codex，`~/.codex/config.toml`：

```toml
[mcp_servers.rendertest]
command = "E:/PersonalWorkSpace/git_repo/renderdoc/agent/mcp_server/.venv/Scripts/python.exe"
args = ["-m", "rendertest_mcp"]

[mcp_servers.rendertest.env]
RENDERTEST_AGENT_ROOT = "E:/PersonalWorkSpace/git_repo/renderdoc/agent/.agent-data"
```

不设置 `RENDERTEST_AGENT_ROOT` 时，Bridge 和 MCP Server 都使用 `%LOCALAPPDATA%/RenderTest/agent`。存在多个在线实例时，可以用 `RENDERTEST_AGENT_INSTANCE_ID` 或工具参数 `instance_id` 指定目标。

# RenderTest Agent

RenderTest Agent 让 Codex 等 AI Agent 通过 MCP 读取 qrenderdoc 当前打开的 capture。设计见 [docs/MVP_DESIGN.md](docs/MVP_DESIGN.md)。

当前进度：**M0 协议骨架**。MCP Server、文件 mailbox IPC、fake Bridge 和协议测试已完成；qrenderdoc 内置加载从 M1 开始。

## 目录

```text
agent/
  bridge/rendertest_agent_bridge/   # qrenderdoc 内嵌 Bridge，保持 Python 3.6 兼容
  mcp_server/src/rendertest_mcp/    # 外部 stdio MCP Server，Python >= 3.10
  devtools/fake_bridge.py           # 不依赖 qrenderdoc 的 Bridge 模拟器
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

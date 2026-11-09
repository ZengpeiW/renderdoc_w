# Windows x64 试用包维护

用户使用说明在 `agent/docs/AI_ASSISTANT_USER_GUIDE.md`；打包器会把它复制进包，并生成离线 `README.html`。本目录是启动器和离线环境检查的源文件，不包含任何用户配置。

## 构包

1. 准备已验证的 `x64/Release` 程序、Bridge、Skill 和 release 反编译器。
2. 使用安装了项目及 Claude 可选依赖的 Windows Python **3.14.7 x64** 开发环境。打包器从 `pyproject.toml` 验证依赖约束并复制完整运行依赖闭包，包括依赖所要求的 extras。
3. 下载 [Python 3.14.7 Windows x64 embeddable release](https://www.python.org/downloads/release/python-3147/) 到 `package/_inputs/python-3.14.7-embed-amd64.zip`。打包器会核对代码中固定的官方 SHA-256。
4. 指定本机 Visual Studio 安装附带的 x64 CRT Redistributable 目录：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\package_trial.py --msvc-redist "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Redist\MSVC\14.44.35112\x64\Microsoft.VC143.CRT"
```

默认输出 `package/RenderTest-AI-Assistant-2026.11.09-win64/`、同名 ZIP 和 `.zip.sha256`；ZIP 内顶层统一为短目录 `RenderTest`，降低默认解压时的路径长度。发布下一版时传 `--name`。已有同名目录会拒绝覆盖。`--no-zip` 只生成目录。

包采用明确的原生文件清单；Python 库按已安装 distribution 的文件清单复制，移除 editable 安装路径、缓存、编译符号和 Claude SDK 内置 CLI。Python SDK 和许可保留，Codex/Claude CLI 由试用者自行安装。包内使用独立 `_pth`，不依赖开发 venv、系统 site-packages 或仓库路径。

`PACKAGE_MANIFEST.json` 记录源码基准提交、Python 来源、依赖版本，以及每个文件的大小和 SHA-256（不含该清单自身）；ZIP 哈希另存于压缩包旁。没有模型账号、用户配置、历史、日志或 capture。

## 验证

```powershell
package\RenderTest-AI-Assistant-2026.11.09-win64\runtime\python\python.exe -B package\RenderTest-AI-Assistant-2026.11.09-win64\tools\check_environment.py --verify-files
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\trial_package_smoke.py --package package/RenderTest-AI-Assistant-2026.11.09-win64.zip --capture E:/Captures/pragmata1.rdc --parent-event 7726
```

第二条会解压到新建的含空格目录，使用包内隔离 Python、独立临时配置和自建隐藏 GUI，验证真实 stdio MCP、35 个工具、4 个 Skill、Bridge 和有界 capture 查询；只关闭自建进程。它不启动 AI harness、不调用在线模型。实际机器仍需符合 capture 的 GPU/驱动要求；该验证不等于覆盖所有干净 Windows 安装。

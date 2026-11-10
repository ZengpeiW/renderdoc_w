# Windows x64 试用包维护

用户使用说明在 `agent/docs/AI_ASSISTANT_USER_GUIDE.md`；打包器会把它复制进包，并生成离线 `README.html`。本目录是启动器和离线环境检查的源文件，不包含任何用户配置。

## 构包

1. 准备已验证的 `x64/Release` 程序、Bridge、Skill 和 release 反编译器。先构建配套 Android APK（见下文），打包器要求同时存在 ARM64/ARM32 包及构建元数据，并核对 APK 与 PC DLL 的版本一致。
2. 使用安装了项目及 Claude 可选依赖的 Windows Python **3.14.7 x64** 开发环境。打包器从 `pyproject.toml` 验证依赖约束并复制完整运行依赖闭包，包括依赖所要求的 extras。
3. 下载 [Python 3.14.7 Windows x64 embeddable release](https://www.python.org/downloads/release/python-3147/) 到 `package/_inputs/python-3.14.7-embed-amd64.zip`。打包器会核对代码中固定的官方 SHA-256。
4. 指定本机 Visual Studio 安装附带的 x64 CRT Redistributable 目录：

```powershell
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\package_trial.py --name RenderTest-AI-Assistant-2026.11.10-android-win64 --msvc-redist "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Redist\MSVC\14.44.35112\x64\Microsoft.VC143.CRT"
```

默认输出 `package/RenderTest-AI-Assistant-2026.11.09-win64/`、同名 ZIP 和 `.zip.sha256`；ZIP 内顶层统一为短目录 `RenderTest`，降低默认解压时的路径长度。发布下一版时传 `--name`。已有同名目录会拒绝覆盖。`--no-zip` 只生成目录。

包采用明确的原生文件清单；Python 库按已安装 distribution 的文件清单复制，移除 editable 安装路径、缓存、编译符号和 Claude SDK 内置 CLI。Python SDK 和许可保留，Codex/Claude CLI 由试用者自行安装。包内使用独立 `_pth`，不依赖开发 venv、系统 site-packages 或仓库路径。

`PACKAGE_MANIFEST.json` 记录源码基准提交、Python 来源、依赖版本，以及每个文件的大小和 SHA-256（不含该清单自身）；ZIP 哈希另存于压缩包旁。没有模型账号、用户配置、历史、日志或 capture。

## Android 截帧服务

在仓库根运行 `build_android.bat both`（需要 PowerShell 7 和 MSYS2；脚本会准备 SDK/NDK/JDK）。也可分别传 `arm64`、`arm32`。已有工具链可通过 `RENDERDOC_ANDROID_DEPS`、`ANDROID_SDK_ROOT`、`RENDERDOC_JAVA_HOME`、`MSYS2_ROOT` 指定。

构建输出为 `build-android-arm64/bin/org.renderdoc.renderdoccmd.arm64.apk` 和 `build-android-arm32/bin/org.renderdoc.renderdoccmd.arm32.apk`。脚本检查 Manifest、DEX、截帧库、启动库、wrap.sh、包名、ABI 和 APK 签名，通过后复制到 `x64/Release/plugins/android/`；可用 `-OutputDirectory` 改变复制位置。每个 APK 的 `.apk.json` 记录 aapt 读取的版本和 SHA-256，仅供构包输入使用，不包含签名私钥。

PC 端按上述原始包名发现服务。不要只将文件改名为 rendertestcmd，也不要混用其他 RenderDoc 版本的 APK。打包器还校验 ELF 架构，并对比 PC DLL 实际导出的提交哈希、版本号与 APK 元数据；不匹配时拒绝构包。更新源码提交后需重建 PC 原生库与 Android 服务，不能仅修改版本标签。`PACKAGE_MANIFEST.json` 记录 `nativeCommit`、`nativeVersion`、`androidPackages` 和所有 APK 文件哈希。

新试用包可用 `--name RenderTest-AI-Assistant-2026.11.10-android-win64` 生成，保留此前交付。2026-11-10 GUI 支持直接启动 EXE 自动发现随包 Python，查找顺序为环境变量、Settings 显式路径、EXE 同目录下的 `runtime/python/python.exe`、源码开发 venv。此次仅重建 GUI，继续使用已验证且版本匹配的 PC DLL 和 ARM APK。包内携带两种 ARM APK，Android SDK/ADB/JDK 由使用者配置；手机安装、连接及实际捕获需另做真机验收。

## 验证

```powershell
package\RenderTest-AI-Assistant-2026.11.10-android-win64\runtime\python\python.exe -B package\RenderTest-AI-Assistant-2026.11.10-android-win64\tools\check_environment.py --verify-files
agent\mcp_server\.venv\Scripts\python.exe agent\devtools\trial_package_smoke.py --package package/RenderTest-AI-Assistant-2026.11.10-android-win64.zip --capture E:/Captures/pragmata1.rdc --parent-event 7726 --work-dir E:/Captures/TrialValidation
```

第二条会解压到新建的含空格目录，使用包内隔离 Python、独立临时配置和自建隐藏 GUI，验证真实 stdio MCP、35 个工具、4 个 Skill、Bridge 和有界 capture 查询；只关闭自建进程。从 2026-11-10 起，还验证直接启动 EXE、MCP Python 配置留空且没有 `RENDERTEST_MCP_PYTHON` 时的自动发现：用离线模型目录 fixture 打开 Claude Settings，检查目录加载成功以及 GUI 解析的 Python 确实来自新解压目录。fixture 不启动真实 AI harness、不读取模型账号、不调用在线模型。此前脚本主动注入 Python 路径，仅验证了启动器场景，遗漏直接启动问题。实际机器仍需符合 capture 的 GPU/驱动要求；该验证不等于覆盖所有干净 Windows 安装。

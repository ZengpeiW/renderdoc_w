#include "AgentLaunchConfig.h"
#include <QDir>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcessEnvironment>
#include <QStandardPaths>

const char *AgentLaunchConfig::ServerName = "rendertest";

namespace
{
const char *IsolatedFeatures[] = {
    "apps",
    "plugins",
    "computer_use",
    "browser_use",
    "browser_use_external",
    "image_generation",
    "multi_agent",
    "goals",
    "hooks",
    "shell_tool",
    "unified_exec",
};

QString EnvValue(const char *name)
{
  return QProcessEnvironment::systemEnvironment().value(QString::fromLatin1(name)).trimmed();
}

QString Posix(const QString &path)
{
  return QDir::fromNativeSeparators(QDir::cleanPath(path));
}
}

QStringList AgentLaunchConfig::SplitCommand(const QString &command)
{
  QStringList result;
  QString current;
  bool inQuotes = false;
  bool hasToken = false;
  for(QChar c : command)
  {
    if(c == QLatin1Char('"'))
    {
      inQuotes = !inQuotes;
      hasToken = true;
    }
    else if(c.isSpace() && !inQuotes)
    {
      if(hasToken)
        result << current;
      current.clear();
      hasToken = false;
    }
    else
    {
      current += c;
      hasToken = true;
    }
  }
  if(hasToken)
    result << current;
  return result;
}

QString AgentLaunchConfig::TomlString(const QString &value)
{
  QByteArray json = QJsonDocument(QJsonArray({value})).toJson(QJsonDocument::Compact);
  return QString::fromUtf8(json.mid(1, json.size() - 2));
}

QString AgentLaunchConfig::FindUpwards(const QString &relativePath)
{
  QDir dir(QCoreApplication::applicationDirPath());
  for(int depth = 0; depth < 6; depth++)
  {
    QString candidate = dir.absoluteFilePath(relativePath);
    if(QFileInfo::exists(candidate))
      return Posix(candidate);
    if(!dir.cdUp())
      break;
  }
  return QString();
}

QString AgentLaunchConfig::FindCodex(QStringList &prefixArgs, QString &error)
{
  QString command = EnvValue("RENDERTEST_AGENT_CODEX_COMMAND");
  if(!command.isEmpty())
  {
    QStringList argv = SplitCommand(command);
    if(argv.isEmpty())
    {
      error = tr("RENDERTEST_AGENT_CODEX_COMMAND is set but empty.");
      return QString();
    }
    QString program = argv.takeFirst();
    QFileInfo info(program);
    if(info.isAbsolute() && !info.exists())
    {
      error =
          tr("The codex executable from RENDERTEST_AGENT_CODEX_COMMAND does not exist: %1").arg(program);
      return QString();
    }
    if(!info.isAbsolute())
    {
      QString found = QStandardPaths::findExecutable(program);
      if(found.isEmpty())
      {
        error = tr("The program from RENDERTEST_AGENT_CODEX_COMMAND was not found: %1").arg(program);
        return QString();
      }
      program = found;
    }
    prefixArgs = argv;
    return program;
  }

  prefixArgs = QStringList({lit("app-server")});

  QString appData = EnvValue("APPDATA");
  if(!appData.isEmpty())
  {
    QString vendor = QDir(appData).absoluteFilePath(
        lit("npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/"
            "x86_64-pc-windows-msvc/bin/codex.exe"));
    if(QFileInfo::exists(vendor))
      return Posix(vendor);
  }

  QString onPath = QStandardPaths::findExecutable(lit("codex.exe"));
  if(!onPath.isEmpty())
    return Posix(onPath);

  error =
      tr("Codex CLI was not found. Install it with `npm install -g @openai/codex` and run "
         "`codex login`, or point RENDERTEST_AGENT_CODEX_COMMAND at codex.exe.");
  return QString();
}

QString AgentLaunchConfig::FindMcpPython(QString &error)
{
  QString python = EnvValue("RENDERTEST_MCP_PYTHON");
  if(!python.isEmpty())
  {
    if(QFileInfo::exists(python))
      return Posix(python);
    error = tr("RENDERTEST_MCP_PYTHON points to a missing file: %1").arg(python);
    return QString();
  }

  python = FindUpwards(lit("agent/mcp_server/.venv/Scripts/python.exe"));
  if(!python.isEmpty())
    return python;

  error =
      tr("The RenderTest MCP server Python was not found. Create agent/mcp_server/.venv (see "
         "agent/README.md) or set RENDERTEST_MCP_PYTHON to a Python with rendertest_mcp "
         "installed.");
  return QString();
}

QStringList AgentLaunchConfig::FindSkillRoots()
{
  QString deployed = QDir(QCoreApplication::applicationDirPath())
                         .absoluteFilePath(lit("integrations/rendertest_agent/skills"));
  if(QFileInfo(deployed).isDir())
    return QStringList({Posix(deployed)});

  QString source = FindUpwards(lit(".agents/skills/rendertest-core/SKILL.md"));
  if(!source.isEmpty())
    return QStringList({Posix(QFileInfo(source).absoluteDir().absolutePath() + lit("/.."))});

  return QStringList();
}

QString AgentLaunchConfig::DeveloperInstructions(const QString &instanceId)
{
  QString instance = instanceId.isEmpty() ? lit("(unknown - the bridge is not running)") : instanceId;
  return lit("You are the AI assistant embedded in RenderTest (a RenderDoc fork), running inside "
             "the qrendertest instance `%1`. The `rendertest` MCP server is already bound to this "
             "instance; use its tools to read the capture and follow the `rendertest-core` skill "
             "before inspecting anything.\n"
             "- Do not call `set_event` or otherwise change the UI unless the user explicitly asks "
             "for it.\n"
             "- Cite events as `EID <n>` and resources as `ResourceId::<n>`.\n"
             "- Answer in the language the user writes in.\n"
             "- A `<rendertest_context>` block at the start of a user message is a snapshot taken "
             "when the message was sent. The UI may have moved since, so pass `event_id` "
             "explicitly to tools, and pass ResourceIds as decimal strings without the "
             "`ResourceId::` prefix.\n"
             "- You cannot run shell commands or write files; rely on the rendertest tools.")
      .arg(instance);
}

AgentLaunchConfig AgentLaunchConfig::Resolve(const QString &instanceId, const QString &agentRoot)
{
  AgentLaunchConfig config;
  config.instanceId = instanceId;
  config.agentRoot = Posix(agentRoot);

  QStringList prefixArgs;
  config.program = FindCodex(prefixArgs, config.error);
  if(!config.error.isEmpty())
    return config;

  config.mcpPython = FindMcpPython(config.error);
  if(!config.error.isEmpty())
    return config;

  QString isolate = EnvValue("RENDERTEST_AGENT_ISOLATE").toLower();
  config.isolate = !(isolate == lit("0") || isolate == lit("false") || isolate == lit("off"));

  QString workingDir = QDir(QCoreApplication::applicationDirPath())
                           .absoluteFilePath(lit("integrations/rendertest_agent"));
  if(!QFileInfo(workingDir).isDir())
    workingDir = QCoreApplication::applicationDirPath();
  config.workingDir = Posix(workingDir);
  config.skillRoots = FindSkillRoots();

  const QString server = QString::fromLatin1(ServerName);
  QStringList overrides;
  overrides << lit("mcp_servers.%1.command=%2").arg(server, TomlString(config.mcpPython));
  overrides << lit("mcp_servers.%1.args=[\"-m\",\"rendertest_mcp\"]").arg(server);
  if(!instanceId.isEmpty())
    overrides << lit("mcp_servers.%1.env.RENDERTEST_AGENT_INSTANCE_ID=%2")
                     .arg(server, TomlString(instanceId));
  overrides << lit("mcp_servers.%1.env.RENDERTEST_AGENT_ROOT=%2")
                   .arg(server, TomlString(config.agentRoot));
  overrides << lit("mcp_servers.%1.tool_timeout_sec=180").arg(server);
  overrides << lit("mcp_servers.%1.startup_timeout_sec=30").arg(server);
  overrides << lit("mcp_servers.%1.enabled=true").arg(server);
  overrides << lit("web_search=\"disabled\"");
  if(config.isolate)
  {
    for(const char *feature : IsolatedFeatures)
      overrides << lit("features.%1=false").arg(QString::fromLatin1(feature));
  }

  config.arguments = prefixArgs;
  for(const QString &item : overrides)
    config.arguments << lit("-c") << item;

  return config;
}

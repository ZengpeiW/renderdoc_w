#pragma once

#include <QCoreApplication>
#include <QString>
#include <QStringList>

// Everything needed to launch `codex app-server` for the AI panel of this qrendertest instance.
struct AgentLaunchConfig
{
  Q_DECLARE_TR_FUNCTIONS(AgentLaunchConfig);

public:
  QString program;
  QStringList arguments;
  QString workingDir;
  QString mcpPython;
  QString instanceId;
  QString agentRoot;
  QStringList skillRoots;
  bool isolate = true;
  // set when the launch cannot proceed; describes how to fix it
  QString error;

  static AgentLaunchConfig Resolve(const QString &instanceId, const QString &agentRoot);

  // splits a command line on whitespace, honouring double quotes
  static QStringList SplitCommand(const QString &command);
  // quotes a string as a TOML basic string for `-c key=value` overrides
  static QString TomlString(const QString &value);
  static QString DeveloperInstructions(const QString &instanceId);

  static const char *ServerName;

private:
  static QString FindCodex(QStringList &prefixArgs, QString &error);
  static QString FindMcpPython(QString &error);
  static QStringList FindSkillRoots();
  static QString FindUpwards(const QString &relativePath);
};

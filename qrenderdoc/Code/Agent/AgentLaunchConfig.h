#pragma once

#include <QCoreApplication>
#include <QString>
#include <QStringList>

class PersistentConfig;

// Everything needed to launch an agent backend for this qrendertest instance.
struct AgentLaunchConfig
{
  Q_DECLARE_TR_FUNCTIONS(AgentLaunchConfig);

public:
  QString backend;
  QString program;
  QStringList arguments;
  QString workingDir;
  QString mcpPython;
  QString instanceId;
  QString captureSessionId;
  QString model;
  QString reasoningEffort;
  bool captureTools = false;
  QString agentRoot;
  QStringList skillRoots;
  bool isolate = true;
  // set when the launch cannot proceed; describes how to fix it
  QString error;

  static AgentLaunchConfig Resolve(const QString &instanceId, const QString &captureSessionId,
                                   const QString &agentRoot, const PersistentConfig &settings);
  // Environment overrides the saved choice. Invalid choices return empty and optionally an error.
  static QString EffectiveBackend(const PersistentConfig &settings, QString *error = NULL);

  // splits a command line on whitespace, honouring double quotes
  static QStringList SplitCommand(const QString &command);
  // quotes a string as a TOML basic string for `-c key=value` overrides
  static QString TomlString(const QString &value);
  static QString DeveloperInstructions(const QString &instanceId);

  static const char *ServerName;

private:
  static QString FindCodex(const PersistentConfig &settings, QStringList &prefixArgs, QString &error);
  static QString FindMcpPython(const PersistentConfig &settings, QString &error);
  static QStringList FindSkillRoots();
  static QString FindUpwards(const QString &relativePath);
};

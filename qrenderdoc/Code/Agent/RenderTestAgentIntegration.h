#pragma once

#include <QCoreApplication>
#include <QJsonObject>
#include <QString>

// Owns the built-in RenderTest agent bridge: a fixed Python package shipped next to the
// executable that publishes the current capture state to external MCP agents.
class RenderTestAgentIntegration
{
  Q_DECLARE_TR_FUNCTIONS(RenderTestAgentIntegration);

public:
  RenderTestAgentIntegration() = default;
  ~RenderTestAgentIntegration();

  // loads and starts the bridge. Failures are logged and never block startup.
  void Initialize();
  // stops the bridge. Must run while the capture context and main window are still alive.
  bool Shutdown();

  bool IsRunning() const { return m_Running; }
  bool IsDisabled() const { return m_Disabled; }
  const QString &LastError() const { return m_LastError; }

  static QString PackageRoot();
  // agent data root shared with the bridge and MCP server (RENDERTEST_AGENT_ROOT or
  // %LOCALAPPDATA%/RenderTest/agent).
  static QString AgentRoot();
  // picks the live bridge instance published by process pid from an instances directory.
  static QString SelectInstanceId(const QString &instancesDir, qint64 pid);
  // the bridge instance id of this process, read from the bridge manifest.
  QString InstanceId();
  // the current manifest of this process' bridge instance, or an empty object.
  QJsonObject ReadManifest();

private:
  bool m_Running = false;
  bool m_Disabled = false;
  QString m_LastError;
  QString m_InstanceId;
};

#pragma once

#include <QCoreApplication>
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

private:
  bool m_Running = false;
  bool m_Disabled = false;
  QString m_LastError;
};

#include "RenderTestAgentIntegration.h"
#include <QDir>
#include "Code/pyrenderdoc/PythonContext.h"

namespace
{
const rdcstr BridgeModule = "rendertest_agent_bridge";
}

RenderTestAgentIntegration::~RenderTestAgentIntegration()
{
  Shutdown();
}

QString RenderTestAgentIntegration::PackageRoot()
{
  return QDir(QCoreApplication::applicationDirPath())
      .absoluteFilePath(lit("integrations/rendertest_agent/python"));
}

void RenderTestAgentIntegration::Initialize()
{
  if(m_Running)
    return;

  const QByteArray setting = qgetenv("RENDERTEST_AGENT_BRIDGE").trimmed().toLower();
  if(setting == "0" || setting == "false" || setting == "off" || setting == "no")
  {
    m_Disabled = true;
    qInfo() << "RenderTest agent bridge disabled by RENDERTEST_AGENT_BRIDGE";
    return;
  }

  m_LastError = PythonContext::LoadInternalIntegration(BridgeModule, PackageRoot());
  m_Running = m_LastError.isEmpty();

  if(m_Running)
    qInfo() << "RenderTest agent bridge started";
  else
    qWarning() << "RenderTest agent bridge unavailable:" << m_LastError;
}

bool RenderTestAgentIntegration::Shutdown()
{
  if(!m_Running)
    return true;

  if(!PythonContext::ShutdownInternalIntegrations())
  {
    m_LastError = tr("The agent bridge did not stop cleanly.");
    return false;
  }

  m_Running = false;
  return true;
}

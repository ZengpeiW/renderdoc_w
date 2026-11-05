#include "RenderTestAgentIntegration.h"
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include "Code/pyrenderdoc/PythonContext.h"

namespace
{
const rdcstr BridgeModule = "rendertest_agent_bridge";

QJsonObject ReadJsonObject(const QString &path)
{
  QFile file(path);
  if(!file.open(QIODevice::ReadOnly))
    return QJsonObject();
  return QJsonDocument::fromJson(file.readAll()).object();
}

QString InstancesDir()
{
  return QDir(RenderTestAgentIntegration::AgentRoot()).absoluteFilePath(lit("ipc-v1/instances"));
}
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

QString RenderTestAgentIntegration::AgentRoot()
{
  QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
  QString root = env.value(lit("RENDERTEST_AGENT_ROOT")).trimmed();
  if(!root.isEmpty())
    return QDir::cleanPath(QDir(root).absolutePath());

  QString local = env.value(lit("LOCALAPPDATA")).trimmed();
  if(local.isEmpty())
    local = env.value(lit("XDG_DATA_HOME")).trimmed();
  if(local.isEmpty())
    local = QDir::home().absoluteFilePath(lit(".local/share"));
  return QDir::cleanPath(QDir(local).absoluteFilePath(lit("RenderTest/agent")));
}

QString RenderTestAgentIntegration::SelectInstanceId(const QString &instancesDir, qint64 pid)
{
  static const QRegularExpression validId(lit("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"));

  QString best;
  double bestStarted = -1.0;
  QDir dir(instancesDir);
  for(const QString &name : dir.entryList(QDir::Dirs | QDir::NoDotAndDotDot, QDir::Name))
  {
    QJsonObject manifest = ReadJsonObject(dir.absoluteFilePath(name + lit("/instance.json")));
    QString id = manifest[lit("instanceId")].toString();
    if(id != name || !validId.match(id).hasMatch())
      continue;
    if(manifest[lit("stopped")].toBool(false))
      continue;
    if(manifest[lit("pid")].toDouble(-1.0) != double(pid))
      continue;
    double started = manifest[lit("startedAtUnixMs")].toDouble(0.0);
    if(best.isEmpty() || started > bestStarted)
    {
      best = id;
      bestStarted = started;
    }
  }
  return best;
}

QString RenderTestAgentIntegration::InstanceId()
{
  if(m_InstanceId.isEmpty() && m_Running)
    m_InstanceId = SelectInstanceId(InstancesDir(), QCoreApplication::applicationPid());
  return m_InstanceId;
}

QJsonObject RenderTestAgentIntegration::ReadManifest()
{
  QString id = InstanceId();
  if(id.isEmpty())
    return QJsonObject();
  return ReadJsonObject(QDir(InstancesDir()).absoluteFilePath(id + lit("/instance.json")));
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
  m_InstanceId.clear();
  return true;
}

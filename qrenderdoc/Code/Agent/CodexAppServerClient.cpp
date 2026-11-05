#include "CodexAppServerClient.h"
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QTimer>

#if defined(Q_OS_WIN32)
#include <windows.h>
#endif

namespace
{
const int MaxLineBytes = 16 * 1024 * 1024;
const int MaxStderrBytes = 64 * 1024;
const char *ReadOnlyRejection = "RenderTest AI panel is read-only";
}

CodexAppServerClient::CodexAppServerClient(QObject *parent) : QObject(parent)
{
}

CodexAppServerClient::~CodexAppServerClient()
{
  Kill();
  CloseJob();
}

bool CodexAppServerClient::IsRunning() const
{
  return m_Process && m_Process->state() != QProcess::NotRunning && !m_Ended;
}

qint64 CodexAppServerClient::ProcessId() const
{
  return m_Process ? m_Process->processId() : 0;
}

void CodexAppServerClient::CloseJob()
{
#if defined(Q_OS_WIN32)
  if(m_Job)
  {
    CloseHandle((HANDLE)m_Job);
    m_Job = NULL;
  }
#endif
}

bool CodexAppServerClient::Start(const QString &program, const QStringList &arguments,
                                 const QString &workingDir, QString &error)
{
  if(IsRunning())
    return true;

  if(m_Process)
  {
    m_Process->disconnect(this);
    m_Process->deleteLater();
    m_Process = NULL;
  }
  CloseJob();

  m_Stdout.clear();
  m_Stderr.clear();
  m_Killing = false;
  m_Ended = false;

  m_Process = new QProcess(this);
  m_Process->setProgram(program);
  m_Process->setArguments(arguments);
  m_Process->setWorkingDirectory(workingDir);
  m_Process->setProcessChannelMode(QProcess::SeparateChannels);

#if defined(Q_OS_WIN32)
  HANDLE job = CreateJobObjectW(NULL, NULL);
  if(job)
  {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if(!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)))
    {
      CloseHandle(job);
      job = NULL;
    }
  }
  m_Job = job;
  if(m_Job)
  {
    m_Process->setCreateProcessArgumentsModifier(
        [](QProcess::CreateProcessArguments *args) { args->flags |= CREATE_SUSPENDED; });
  }
#endif

  connect(m_Process, &QProcess::readyReadStandardOutput, this, &CodexAppServerClient::ReadStdout);
  connect(m_Process, &QProcess::readyReadStandardError, this, &CodexAppServerClient::ReadStderr);
  connect(m_Process, OverloadedSlot<int, QProcess::ExitStatus>::of(&QProcess::finished), this,
          [this](int exitCode, QProcess::ExitStatus status) {
            if(!m_Killing)
              ReadStdout();
            ReadStderr();
            ProcessEnded(exitCode, status == QProcess::CrashExit ? tr("crashed") : tr("exited"));
          });

  AppendLog(lit("starting %1 %2").arg(program, arguments.join(QLatin1Char(' '))));

  m_Process->start(QIODevice::ReadWrite);
  if(!m_Process->waitForStarted(10000))
  {
    error = tr("Failed to start %1: %2").arg(program, m_Process->errorString());
    AppendLog(error);
    m_Ended = true;
    m_Process->disconnect(this);
    m_Process->deleteLater();
    m_Process = NULL;
    CloseJob();
    return false;
  }

#if defined(Q_OS_WIN32)
  if(m_Job)
  {
    _PROCESS_INFORMATION *info = m_Process->pid();
    if(info && !AssignProcessToJobObject((HANDLE)m_Job, info->hProcess))
      AppendLog(lit("AssignProcessToJobObject failed: %1").arg(GetLastError()));
    if(info)
      ResumeThread(info->hThread);
  }
#endif

  return true;
}

void CodexAppServerClient::Kill()
{
  if(!m_Process || m_Ended)
    return;

  m_Killing = true;
#if defined(Q_OS_WIN32)
  if(m_Job)
    TerminateJobObject((HANDLE)m_Job, 1);
#endif
  m_Process->kill();
  if(!m_Process->waitForFinished(3000))
    AppendLog(lit("app-server did not exit after kill"));
  // waitForFinished delivers finished() synchronously, but make sure we always end
  if(!m_Ended)
  {
    ReadStderr();
    ProcessEnded(-1, tr("killed"));
  }
  CloseJob();
}

void CodexAppServerClient::ProcessEnded(int exitCode, const QString &reason)
{
  if(m_Ended)
    return;
  m_Ended = true;

  bool killed = m_Killing;
  AppendLog(lit("app-server %1 (code %2)").arg(killed ? lit("killed") : reason).arg(exitCode));
  if(!m_Stderr.isEmpty())
    AppendLog(lit("stderr tail:\n%1").arg(StderrTail()));

  emit processExited(exitCode, killed, killed ? tr("killed") : reason);
  FailPending(tr("The Codex app-server stopped."));
}

void CodexAppServerClient::FailPending(const QString &message)
{
  QHash<qint64, Pending> pending;
  pending.swap(m_Pending);
  QJsonObject error;
  error[lit("code")] = -32000;
  error[lit("message")] = message;
  for(const Pending &p : pending)
  {
    if(p.callback)
      p.callback(QJsonValue(), error);
  }
}

qint64 CodexAppServerClient::Request(const QString &method, const QJsonObject &params,
                                     int timeoutMs, ResponseCallback callback)
{
  qint64 id = m_NextId++;

  if(!IsRunning())
  {
    QJsonObject error;
    error[lit("code")] = -32000;
    error[lit("message")] = tr("The Codex app-server is not running.");
    if(callback)
      QTimer::singleShot(0, this, [callback, error]() { callback(QJsonValue(), error); });
    return id;
  }

  m_Pending[id] = Pending({method, callback});

  QJsonObject message;
  message[lit("id")] = id;
  message[lit("method")] = method;
  message[lit("params")] = params;
  Write(message);

  if(timeoutMs > 0)
  {
    QTimer::singleShot(timeoutMs, this, [this, id, method, timeoutMs]() {
      auto it = m_Pending.find(id);
      if(it == m_Pending.end())
        return;
      ResponseCallback callback = it->callback;
      m_Pending.erase(it);
      QJsonObject error;
      error[lit("code")] = -32001;
      error[lit("message")] = tr("%1 timed out after %2 seconds.").arg(method).arg(timeoutMs / 1000);
      AppendLog(error[lit("message")].toString());
      if(callback)
        callback(QJsonValue(), error);
    });
  }

  return id;
}

void CodexAppServerClient::Notify(const QString &method, const QJsonObject &params)
{
  if(!IsRunning())
    return;
  QJsonObject message;
  message[lit("method")] = method;
  message[lit("params")] = params;
  Write(message);
}

void CodexAppServerClient::Write(const QJsonObject &message)
{
  QByteArray data = QJsonDocument(message).toJson(QJsonDocument::Compact);
  data += '\n';
  m_Process->write(data);
}

void CodexAppServerClient::ReadStdout()
{
  if(!m_Process || m_Ended || m_Killing)
    return;

  m_Stdout += m_Process->readAllStandardOutput();

  int start = 0;
  while(true)
  {
    int newline = m_Stdout.indexOf('\n', start);
    if(newline < 0)
      break;
    QByteArray line = m_Stdout.mid(start, newline - start).trimmed();
    start = newline + 1;
    if(!line.isEmpty())
      HandleLine(line);
    // handling a line can kill the process; drop whatever it still had buffered
    if(m_Ended || m_Killing)
    {
      m_Stdout.clear();
      return;
    }
  }
  m_Stdout.remove(0, start);

  if(m_Stdout.size() > MaxLineBytes)
  {
    AppendLog(lit("protocol error: a message exceeded %1 bytes").arg(MaxLineBytes));
    m_Stdout.clear();
    Kill();
  }
}

void CodexAppServerClient::ReadStderr()
{
  if(!m_Process)
    return;
  m_Stderr += m_Process->readAllStandardError();
  if(m_Stderr.size() > MaxStderrBytes)
    m_Stderr = m_Stderr.right(MaxStderrBytes);
}

QJsonObject CodexAppServerClient::ServerRequestReply(const QString &method, bool &supported)
{
  supported = true;
  QJsonObject result;
  if(method == lit("item/commandExecution/requestApproval") ||
     method == lit("item/fileChange/requestApproval"))
  {
    result[lit("decision")] = lit("decline");
  }
  else if(method == lit("execCommandApproval") || method == lit("applyPatchApproval"))
  {
    QJsonObject denied;
    denied[lit("rejection")] = QString::fromLatin1(ReadOnlyRejection);
    QJsonObject decision;
    decision[lit("denied")] = denied;
    result[lit("decision")] = decision;
  }
  else if(method == lit("item/permissions/requestApproval"))
  {
    result[lit("permissions")] = QJsonObject();
    result[lit("scope")] = lit("turn");
  }
  else if(method == lit("mcpServer/elicitation/request"))
  {
    result[lit("action")] = lit("decline");
    result[lit("content")] = QJsonValue(QJsonValue::Null);
    result[lit("_meta")] = QJsonValue(QJsonValue::Null);
  }
  else
  {
    supported = false;
  }
  return result;
}

void CodexAppServerClient::HandleLine(const QByteArray &line)
{
  QJsonParseError parseError;
  QJsonDocument doc = QJsonDocument::fromJson(line, &parseError);
  if(!doc.isObject())
  {
    AppendLog(lit("ignoring non-JSON line: %1").arg(QString::fromUtf8(line.left(200))));
    return;
  }

  QJsonObject message = doc.object();
  const bool hasId = message.contains(lit("id"));
  const QString method = message[lit("method")].toString();

  if(hasId && !method.isEmpty())
  {
    bool supported = false;
    QJsonObject reply;
    reply[lit("id")] = message[lit("id")];
    QJsonObject result = ServerRequestReply(method, supported);
    if(supported)
    {
      reply[lit("result")] = result;
    }
    else
    {
      QJsonObject error;
      error[lit("code")] = -32601;
      error[lit("message")] = lit("%1 is not supported by the RenderTest AI panel").arg(method);
      reply[lit("error")] = error;
    }
    Write(reply);
    emit serverRequestAnswered(method, supported);
    return;
  }

  if(hasId)
  {
    qint64 id = message[lit("id")].toVariant().toLongLong();
    auto it = m_Pending.find(id);
    if(it == m_Pending.end())
      return;
    ResponseCallback callback = it->callback;
    m_Pending.erase(it);
    if(callback)
      callback(message[lit("result")], message[lit("error")].toObject());
    return;
  }

  if(!method.isEmpty())
    emit notificationReceived(method, message[lit("params")].toObject());
}

void CodexAppServerClient::AppendLog(const QString &text)
{
  if(m_LogPath.isEmpty())
    return;
  QDir().mkpath(QFileInfo(m_LogPath).absolutePath());
  QFile file(m_LogPath);
  if(!file.open(QIODevice::WriteOnly | QIODevice::Append))
    return;
  QString stamp = QDateTime::currentDateTime().toString(Qt::ISODate);
  file.write(lit("[%1] %2\n").arg(stamp, text).toUtf8());
}

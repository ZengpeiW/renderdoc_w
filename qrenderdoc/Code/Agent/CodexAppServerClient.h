#pragma once

#include <QHash>
#include <QJsonObject>
#include <QJsonValue>
#include <QObject>
#include <QProcess>
#include <functional>

// Talks newline-delimited JSON-RPC (without the "jsonrpc" field) to a `codex app-server` child
// process. The child runs in a kill-on-close job object so it never outlives qrendertest, and
// every server request is answered with a read-only refusal.
class CodexAppServerClient : public QObject
{
  Q_OBJECT

public:
  typedef std::function<void(const QJsonValue &result, const QJsonObject &error)> ResponseCallback;

  explicit CodexAppServerClient(QObject *parent = NULL);
  ~CodexAppServerClient();

  bool Start(const QString &program, const QStringList &arguments, const QString &workingDir,
             QString &error);
  // terminates the whole process tree. Pending requests fail with an error.
  void Kill();
  bool IsRunning() const;
  qint64 ProcessId() const;

  // timeoutMs <= 0 waits forever
  qint64 Request(const QString &method, const QJsonObject &params, int timeoutMs,
                 ResponseCallback callback);
  void Notify(const QString &method, const QJsonObject &params);

  QString StderrTail() const { return QString::fromUtf8(m_Stderr); }
  void SetLogPath(const QString &path) { m_LogPath = path; }

  // the reply sent for a server request, exposed for tests and the UI
  static QJsonObject ServerRequestReply(const QString &method, bool &supported);

signals:
  void notificationReceived(const QString &method, const QJsonObject &params);
  void serverRequestAnswered(const QString &method, bool supported);
  // emitted once when the process ends, whether it exited, crashed or was killed
  void processExited(int exitCode, bool killed, const QString &reason);

private:
  struct Pending
  {
    QString method;
    ResponseCallback callback;
  };

  void ReadStdout();
  void ReadStderr();
  void HandleLine(const QByteArray &line);
  void Write(const QJsonObject &message);
  void FailPending(const QString &message);
  void ProcessEnded(int exitCode, const QString &reason);
  void AppendLog(const QString &text);
  void CloseJob();

  QProcess *m_Process = NULL;
  void *m_Job = NULL;
  QByteArray m_Stdout;
  QByteArray m_Stderr;
  QHash<qint64, Pending> m_Pending;
  qint64 m_NextId = 1;
  bool m_Killing = false;
  bool m_Ended = true;
  QString m_LogPath;
};

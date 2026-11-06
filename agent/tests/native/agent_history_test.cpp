// Standalone Qt 5 test executable: compile with AgentHistory.cpp and Qt Core/Gui/Widgets.
// It uses a temporary directory and never touches the user's agent data or calls a model.
#include "AgentHistory.h"
#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QProcess>
#include <QTemporaryDir>
#include <cstdio>
#include <cstdlib>

namespace
{
int checks = 0;
int failures = 0;

void Check(bool success, const char *message)
{
  checks++;
  if(!success)
  {
    failures++;
    std::fprintf(stderr, "FAIL: %s\n", message);
  }
}

AgentHistoryRecord MakeRecord(const QString &id)
{
  AgentHistoryRecord record;
  record.metadata.id = id;
  record.metadata.title = QString::fromUtf8("History \xe6\xb5\x8b\xe8\xaf\x95");
  record.metadata.threadId = QStringLiteral("thread-a");
  record.metadata.model = QStringLiteral("model-a");
  record.metadata.mode = QStringLiteral("chat");
  record.conversation[QStringLiteral("threadId")] = record.metadata.threadId;
  record.conversation[QStringLiteral("model")] = record.metadata.model;
  record.conversation[QStringLiteral("items")] = QJsonArray();
  record.conversation[QStringLiteral("turns")] = QJsonArray();
  record.conversation[QStringLiteral("totalTokens")] = 0;
  return record;
}

QString Path(const QString &root, const QString &id)
{
  return QDir(root).filePath(QStringLiteral("conversations/") + id + QStringLiteral(".json"));
}

bool Write(const QString &path, const QByteArray &bytes)
{
  QFile file(path);
  return file.open(QIODevice::WriteOnly) && file.write(bytes) == bytes.size();
}
}

int main(int argc, char **argv)
{
  QCoreApplication application(argc, argv);
  const QStringList arguments = application.arguments();
  if(arguments.size() == 4 && arguments[1] == QStringLiteral("--crash-lock"))
  {
    AgentHistory history(arguments[2]);
    QString error;
    if(!history.Acquire(arguments[3], error))
      return 2;
    // Intentionally leave the QLockFile behind, just as a crashed process would.
    std::_Exit(0);
  }

  QTemporaryDir directory;
  Check(directory.isValid(), "temporary directory");
  if(!directory.isValid())
    return 1;
  AgentHistory writer(directory.path()), reader(directory.path());
  QString error, warning;
  Check(writer.List(warning).isEmpty() && warning.isEmpty(), "missing history is empty");
  const QString id = AgentHistory::NewId();
  AgentHistoryRecord record = MakeRecord(id), loaded;
  Check(!reader.Save(record, error), "saving requires ownership");
  Check(!writer.Acquire(QStringLiteral("../outside"), error), "path traversal rejected");
  Check(!reader.Load(QStringLiteral("../outside"), loaded, error), "load accepts only UUID");
  Check(writer.Acquire(id, error), "writer acquires new record");
  Check(writer.Save(record, error), "first atomic save");
  Check(!record.metadata.createdAt.isEmpty() && !record.metadata.updatedAt.isEmpty(), "save timestamps");
  Check(reader.Load(id, loaded, error) && loaded.metadata.title == record.metadata.title,
        "another window reads a locked record");
  Check(!reader.Acquire(id, error), "second writer excluded");
  Check(!reader.Save(record, error), "second writer cannot overwrite");
  QList<AgentHistoryEntry> entries = reader.List(warning);
  Check(entries.size() == 1 && entries[0].locked, "list reports busy record");

  AgentHistoryRecord invalid = record;
  invalid.conversation[QStringLiteral("threadId")] = QStringLiteral("different-thread");
  Check(!writer.Save(invalid, error), "thread mismatch rejected");
  invalid = record;
  invalid.conversation[QStringLiteral("extra")] = QString(9 * 1024 * 1024, QLatin1Char('x'));
  Check(!writer.Save(invalid, error), "oversized save rejected");
  invalid = record;
  QJsonArray excessive;
  for(int i = 0; i < 10001; i++)
    excessive << QJsonObject();
  invalid.conversation[QStringLiteral("items")] = excessive;
  Check(!writer.Save(invalid, error), "item count limited");
  invalid = record;
  QJsonArray excessiveTurns;
  for(int i = 0; i < 2001; i++)
    excessiveTurns << QJsonObject();
  invalid.conversation[QStringLiteral("turns")] = excessiveTurns;
  Check(!writer.Save(invalid, error), "turn count limited to restorable snapshots");
  Check(reader.Load(id, loaded, error) && loaded.metadata.title == record.metadata.title &&
            !loaded.conversation.contains(QStringLiteral("extra")) &&
            loaded.conversation[QStringLiteral("turns")].toArray().isEmpty(),
        "rejected saves preserve last committed record");

  writer.Release();
  Check(reader.Acquire(id, error), "released lock can be acquired");
  loaded.metadata.title = QStringLiteral("Updated title");
  Check(reader.Save(loaded, error), "new owner saves");
  Check(writer.Load(id, record, error) && record.metadata.title == QStringLiteral("Updated title"),
        "new snapshot replaces old snapshot");

  const QString brokenId = AgentHistory::NewId();
  Check(Write(Path(directory.path(), brokenId), QByteArray("{broken")), "create corrupt fixture");
  Check(!writer.Load(brokenId, record, error), "bad JSON rejected");
  const QString largeId = AgentHistory::NewId();
  QFile large(Path(directory.path(), largeId));
  Check(large.open(QIODevice::WriteOnly) && large.resize(65 * 1024 * 1024), "create oversize fixture");
  large.close();
  Check(!writer.Load(largeId, record, error), "oversized load rejected");
  entries = writer.List(warning);
  Check(entries.size() == 1 && !warning.isEmpty(), "bad and huge records skipped without hiding valid history");

  const QString wrongId = AgentHistory::NewId();
  QFile existing(Path(directory.path(), id));
  Check(existing.open(QIODevice::ReadOnly), "read fixture");
  QJsonObject wrong = QJsonDocument::fromJson(existing.readAll()).object();
  existing.close();
  Check(Write(Path(directory.path(), wrongId), QJsonDocument(wrong).toJson()), "create wrong-id fixture");
  Check(!writer.Load(wrongId, record, error), "metadata must match filename identity");

  const QString staleId = AgentHistory::NewId();
  QProcess child;
  child.start(QCoreApplication::applicationFilePath(),
              QStringList() << QStringLiteral("--crash-lock") << directory.path() << staleId);
  Check(child.waitForFinished(10000) && child.exitCode() == 0, "simulate crashed owner");
  Check(writer.Acquire(staleId, error), "dead owner's lock can be recovered");
  Check(!reader.Acquire(staleId, error) && reader.LockedId() == id,
        "failed acquire retains previous ownership");
  writer.Release();
  reader.Release();
  std::printf("%d/%d history checks passed\n", checks - failures, checks);
  return failures == 0 ? 0 : 1;
}

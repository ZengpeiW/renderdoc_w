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

QByteArray Read(const QString &path)
{
  QFile file(path);
  return file.open(QIODevice::ReadOnly) ? file.readAll() : QByteArray();
}

void CheckBackends()
{
  QTemporaryDir directory;
  Check(directory.isValid(), "backend fixture directory");
  if(!directory.isValid())
    return;
  AgentHistory writer(directory.path()), reader(directory.path());
  QString error, warning;
  const QString codexId = AgentHistory::NewId();
  AgentHistoryRecord codex = MakeRecord(codexId), loaded;
  Check(codex.backend == QStringLiteral("codex"), "new records default to Codex");
  Check(writer.Acquire(codexId, error) && writer.Save(codex, error), "save Codex backend");
  QJsonObject saved = QJsonDocument::fromJson(Read(Path(directory.path(), codexId))).object();
  Check(saved[QStringLiteral("version")].toInt() == 2 &&
            saved[QStringLiteral("backend")].toString() == QStringLiteral("codex"),
        "new format identifies backend and prevents old reader resume");

  const QString legacyId = AgentHistory::NewId();
  QJsonObject legacy = saved;
  legacy[QStringLiteral("version")] = 1;
  legacy.remove(QStringLiteral("backend"));
  QJsonObject metadata = legacy[QStringLiteral("metadata")].toObject();
  metadata[QStringLiteral("id")] = legacyId;
  legacy[QStringLiteral("metadata")] = metadata;
  const QByteArray legacyBytes = QJsonDocument(legacy).toJson();
  Check(Write(Path(directory.path(), legacyId), legacyBytes), "write original v1 fixture");
  Check(reader.Load(legacyId, loaded, error) && loaded.backend == QStringLiteral("codex") &&
            loaded.metadata.backend == QStringLiteral("codex"),
        "v1 without backend loads as Codex");
  Check(Read(Path(directory.path(), legacyId)) == legacyBytes, "loading legacy history never rewrites it");
  Check(writer.Acquire(legacyId, error) && writer.Save(loaded, error), "continuing legacy record upgrades on save");
  Check(QJsonDocument::fromJson(Read(Path(directory.path(), legacyId))).object()[QStringLiteral("version")].toInt() == 2,
        "continued legacy record is version 2");

  const QString claudeId = AgentHistory::NewId();
  AgentHistoryRecord claude = MakeRecord(claudeId);
  claude.backend = QStringLiteral("claude");
  claude.metadata.threadId = QStringLiteral("claude-native-session");
  claude.conversation[QStringLiteral("threadId")] = claude.metadata.threadId;
  Check(writer.Acquire(claudeId, error) && writer.Save(claude, error), "save Claude backend");
  Check(reader.Load(claudeId, loaded, error) && loaded.backend == QStringLiteral("claude") &&
            loaded.metadata.backend == QStringLiteral("claude") &&
            loaded.metadata.threadId == QStringLiteral("claude-native-session"),
        "Claude roundtrip retains native session identity");
  const QByteArray claudeBytes = Read(Path(directory.path(), claudeId));
  loaded.backend = QStringLiteral("codex");
  Check(!writer.Save(loaded, error) && Read(Path(directory.path(), claudeId)) == claudeBytes,
        "a record cannot be reassigned to another backend");
  loaded.backend = QStringLiteral("future-harness");
  Check(!writer.Save(loaded, error) && Read(Path(directory.path(), claudeId)) == claudeBytes,
        "saving an unsupported backend preserves original bytes");

  const QString unsupportedId = AgentHistory::NewId();
  QJsonObject unsupported = saved;
  metadata = unsupported[QStringLiteral("metadata")].toObject();
  metadata[QStringLiteral("id")] = unsupportedId;
  unsupported[QStringLiteral("metadata")] = metadata;
  unsupported.remove(QStringLiteral("backend"));
  QByteArray unsupportedBytes = QJsonDocument(unsupported).toJson();
  Check(Write(Path(directory.path(), unsupportedId), unsupportedBytes), "write incomplete v2 fixture");
  Check(!reader.Load(unsupportedId, loaded, error) &&
            Read(Path(directory.path(), unsupportedId)) == unsupportedBytes,
        "v2 cannot infer Codex from a missing backend");
  Check(writer.Acquire(unsupportedId, error), "lock unsupported record for overwrite check");
  const QJsonArray invalidBackends = {QJsonValue(QJsonValue::Null), QJsonValue(42),
                                     QJsonValue(QString()), QJsonValue(QStringLiteral("Claude")),
                                     QJsonValue(QStringLiteral("future-harness"))};
  for(const QJsonValue &backend : invalidBackends)
  {
    unsupported[QStringLiteral("backend")] = backend;
    unsupportedBytes = QJsonDocument(unsupported).toJson();
    Check(Write(Path(directory.path(), unsupportedId), unsupportedBytes), "write unsupported backend fixture");
    loaded = claude;
    Check(!reader.Load(unsupportedId, loaded, error) && loaded.backend == QStringLiteral("claude"),
          "unknown or malformed backend cannot replace loaded conversation");
    AgentHistoryRecord replacement = MakeRecord(unsupportedId);
    Check(!writer.Save(replacement, error) && Read(Path(directory.path(), unsupportedId)) == unsupportedBytes,
          "unknown record cannot be overwritten as Codex");
  }

  const QList<AgentHistoryEntry> entries = reader.List(warning);
  int codexEntries = 0, claudeEntries = 0;
  for(const AgentHistoryEntry &entry : entries)
  {
    codexEntries += entry.backend == QStringLiteral("codex") ? 1 : 0;
    claudeEntries += entry.backend == QStringLiteral("claude") ? 1 : 0;
  }
  Check(entries.size() == 3 && codexEntries == 2 && claudeEntries == 1 && !warning.isEmpty(),
        "mixed backend list retains backend labels and skips unsupported records with a warning");
  Check(Read(Path(directory.path(), unsupportedId)) == unsupportedBytes, "listing unsupported history never modifies it");
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
  CheckBackends();
  std::printf("%d/%d history checks passed\n", checks - failures, checks);
  return failures == 0 ? 0 : 1;
}

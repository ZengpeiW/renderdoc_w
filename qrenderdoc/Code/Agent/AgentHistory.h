#pragma once

#include <QJsonObject>
#include <QList>
#include <QString>

class QLockFile;
class QWidget;

struct AgentHistoryEntry
{
  QString id;
  QString title;
  QString createdAt;
  QString updatedAt;
  QString threadId;
  QString model;
  QString mode;
  QJsonObject captureIdentity;
  // A list-time hint only. Acquire() is the authoritative ownership check.
  bool locked = false;
};

struct AgentHistoryRecord
{
  AgentHistoryEntry metadata;
  QJsonObject conversation;
};

// Local transcript snapshots. Only canonical UUIDs, never caller-supplied paths, identify records.
// A panel keeps the per-record lock for as long as it owns an editable conversation. Other panels
// may still Load() its atomically written snapshot and display it read-only.
class AgentHistory
{
public:
  explicit AgentHistory(const QString &agentRoot);
  ~AgentHistory();

  static QString NewId();
  bool Acquire(const QString &id, QString &error);
  void Release();
  QString LockedId() const { return m_LockedId; }

  // Newest first. Invalid/oversized records are skipped and reported in warning.
  QList<AgentHistoryEntry> List(QString &warning) const;
  bool Load(const QString &id, AgentHistoryRecord &record, QString &error) const;
  // Requires ownership of record.metadata.id. Sets creation/update timestamps after commit.
  bool Save(AgentHistoryRecord &record, QString &error);

  // Returns the selected id, or empty on cancellation. A locked entry is still viewable.
  static QString Select(const QList<AgentHistoryEntry> &entries, QWidget *parent);

private:
  Q_DISABLE_COPY(AgentHistory)
  bool Directory(bool create, QString &error) const;
  QString RecordPath(const QString &id) const;
  QString LockPath(const QString &id) const;

  QString m_Directory;
  QString m_LockedId;
  QLockFile *m_Lock = NULL;
};

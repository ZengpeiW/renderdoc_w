#include "AgentHistory.h"
#include <QCoreApplication>
#include <QDateTime>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QLockFile>
#include <QPushButton>
#include <QRegularExpression>
#include <QSaveFile>
#include <QTreeWidget>
#include <QUuid>
#include <QVBoxLayout>
#include <algorithm>

#if defined(Q_OS_WIN32)
#include <windows.h>
#endif

namespace
{
const qint64 MaxRecordBytes = 8 * 1024 * 1024;
const qint64 MaxListBytes = 64 * 1024 * 1024;
const int MaxListEntries = 512;
const int MaxDirectoryEntries = 4096;
const int MaxConversationItems = 10000;
const int MaxConversationTurns = 2000;

QString Tr(const char *text)
{
  return QCoreApplication::translate("AgentHistory", text);
}

bool ValidId(const QString &id)
{
  static const QRegularExpression pattern(
      QStringLiteral("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"));
  return id.size() == 36 && pattern.match(id).hasMatch() && !QUuid(id).isNull();
}

bool IsLink(const QString &path)
{
  if(QFileInfo(path).isSymLink())
    return true;
#if defined(Q_OS_WIN32)
  DWORD attributes = GetFileAttributesW((LPCWSTR)path.utf16());
  if(attributes != INVALID_FILE_ATTRIBUTES && (attributes & FILE_ATTRIBUTE_REPARSE_POINT))
    return true;
#endif
  return false;
}

QJsonObject MetadataJson(const AgentHistoryEntry &entry)
{
  QJsonObject result;
  result[QStringLiteral("id")] = entry.id;
  result[QStringLiteral("title")] = entry.title;
  result[QStringLiteral("createdAt")] = entry.createdAt;
  result[QStringLiteral("updatedAt")] = entry.updatedAt;
  result[QStringLiteral("threadId")] = entry.threadId;
  result[QStringLiteral("model")] = entry.model;
  result[QStringLiteral("mode")] = entry.mode;
  result[QStringLiteral("captureIdentity")] = entry.captureIdentity;
  return result;
}

bool ReadString(const QJsonObject &json, const QString &key, int limit, QString &result)
{
  const QJsonValue value = json[key];
  if(!value.isString() || value.toString().size() > limit)
    return false;
  result = value.toString();
  return !result.contains(QChar(0));
}

bool Decode(const QJsonObject &json, const QString &id, AgentHistoryRecord &record, QString &error)
{
  const QJsonObject metadata = json[QStringLiteral("metadata")].toObject();
  AgentHistoryRecord parsed;
  AgentHistoryEntry &entry = parsed.metadata;
  if(json[QStringLiteral("version")].toInt(-1) != 1 ||
     !json[QStringLiteral("metadata")].isObject() ||
     !ReadString(metadata, QStringLiteral("id"), 36, entry.id) || entry.id != id ||
     !ReadString(metadata, QStringLiteral("title"), 1024, entry.title) ||
     !ReadString(metadata, QStringLiteral("createdAt"), 64, entry.createdAt) ||
     !ReadString(metadata, QStringLiteral("updatedAt"), 64, entry.updatedAt) ||
     !ReadString(metadata, QStringLiteral("threadId"), 1024, entry.threadId) ||
     !ReadString(metadata, QStringLiteral("model"), 1024, entry.model) ||
     !ReadString(metadata, QStringLiteral("mode"), 64, entry.mode) ||
     !QDateTime::fromString(entry.createdAt, Qt::ISODate).isValid() ||
     !QDateTime::fromString(entry.updatedAt, Qt::ISODate).isValid() ||
     !metadata[QStringLiteral("captureIdentity")].isObject() ||
     !json[QStringLiteral("conversation")].isObject())
  {
    error = Tr("The saved conversation has invalid metadata or an unsupported format.");
    return false;
  }
  entry.captureIdentity = metadata[QStringLiteral("captureIdentity")].toObject();
  if(QJsonDocument(entry.captureIdentity).toJson(QJsonDocument::Compact).size() > 16384)
  {
    error = Tr("The saved capture identity exceeds its size limit.");
    return false;
  }
  parsed.conversation = json[QStringLiteral("conversation")].toObject();
  const QJsonValue items = parsed.conversation[QStringLiteral("items")];
  const QJsonValue turns = parsed.conversation[QStringLiteral("turns")];
  if(!items.isArray() || !turns.isArray() || items.toArray().size() > MaxConversationItems ||
     turns.toArray().size() > MaxConversationTurns ||
     !parsed.conversation[QStringLiteral("threadId")].isString() ||
     parsed.conversation[QStringLiteral("threadId")].toString() != entry.threadId ||
     !parsed.conversation[QStringLiteral("model")].isString() ||
     parsed.conversation[QStringLiteral("model")].toString() != entry.model)
  {
    error = Tr("The saved conversation is incomplete, inconsistent, or exceeds its item limit.");
    return false;
  }
  for(const QJsonValue &item : items.toArray())
    if(!item.isObject())
    {
      error = Tr("The saved conversation contains an invalid item.");
      return false;
    }
  for(const QJsonValue &turn : turns.toArray())
    if(!turn.isObject())
    {
      error = Tr("The saved conversation contains an invalid turn.");
      return false;
    }
  record = parsed;
  return true;
}
}

AgentHistory::AgentHistory(const QString &agentRoot)
    : m_Directory(QDir(agentRoot).absoluteFilePath(QStringLiteral("conversations")))
{
}

AgentHistory::~AgentHistory()
{
  Release();
}

QString AgentHistory::NewId()
{
  // QUuid::WithoutBraces was added after the bundled Qt 5.9.
  return QUuid::createUuid().toString().mid(1, 36).toLower();
}

QString AgentHistory::RecordPath(const QString &id) const
{
  return QDir(m_Directory).filePath(id + QStringLiteral(".json"));
}

QString AgentHistory::LockPath(const QString &id) const
{
  return QDir(m_Directory).filePath(id + QStringLiteral(".lock"));
}

bool AgentHistory::Directory(bool create, QString &error) const
{
  if(IsLink(m_Directory))
  {
    error = Tr("The conversation directory must not be a symbolic link or reparse point.");
    return false;
  }
  QFileInfo directory(m_Directory);
  if(!directory.exists() && create)
  {
    if(!QDir().mkpath(m_Directory))
    {
      error = Tr("Could not create the conversation directory.");
      return false;
    }
    directory.refresh();
  }
  if(!directory.exists())
    return false;
  if(!directory.isDir() || IsLink(m_Directory))
  {
    error = Tr("The conversation storage path is not a regular directory.");
    return false;
  }
  return true;
}

bool AgentHistory::Acquire(const QString &id, QString &error)
{
  error.clear();
  if(!ValidId(id))
  {
    error = Tr("Invalid conversation identifier.");
    return false;
  }
  if(m_Lock && m_Lock->isLocked() && m_LockedId == id)
    return true;
  if(!Directory(true, error))
    return false;
  if(IsLink(RecordPath(id)) || IsLink(LockPath(id)))
  {
    error = Tr("Conversation files must not be symbolic links or reparse points.");
    return false;
  }
  QLockFile *candidate = new QLockFile(LockPath(id));
  // An active long conversation must not lose its lock just because it is old. QLockFile still
  // detects a dead local owner, so a process crash does not permanently strand the conversation.
  candidate->setStaleLockTime(0);
  if(!candidate->tryLock(0))
  {
    error = candidate->error() == QLockFile::LockFailedError
                ? Tr("This conversation is in use in another window. It can be viewed read-only.")
                : Tr("Could not lock the conversation for writing.");
    delete candidate;
    return false;
  }
  Release();
  m_Lock = candidate;
  m_LockedId = id;
  return true;
}

void AgentHistory::Release()
{
  delete m_Lock;
  m_Lock = NULL;
  m_LockedId.clear();
}

bool AgentHistory::Load(const QString &id, AgentHistoryRecord &record, QString &error) const
{
  error.clear();
  if(!ValidId(id))
  {
    error = Tr("Invalid conversation identifier.");
    return false;
  }
  if(!Directory(false, error))
  {
    if(error.isEmpty())
      error = Tr("The conversation directory does not exist.");
    return false;
  }
  const QString path = RecordPath(id);
  QFileInfo info(path);
  if(IsLink(path) || !info.isFile())
  {
    error = Tr("The saved conversation is missing or is not a regular file.");
    return false;
  }
  if(info.size() <= 0 || info.size() > MaxRecordBytes)
  {
    error = Tr("The saved conversation is empty or exceeds the 8 MiB size limit.");
    return false;
  }
  QFile file(path);
  if(!file.open(QIODevice::ReadOnly))
  {
    error = Tr("Could not read the saved conversation: %1").arg(file.errorString());
    return false;
  }
  const QByteArray bytes = file.read(MaxRecordBytes + 1);
  if(bytes.size() > MaxRecordBytes || !file.atEnd())
  {
    error = Tr("The saved conversation exceeds the 8 MiB size limit.");
    return false;
  }
  QJsonParseError parseError;
  const QJsonDocument json = QJsonDocument::fromJson(bytes, &parseError);
  if(parseError.error != QJsonParseError::NoError || !json.isObject())
  {
    error = Tr("The saved conversation contains invalid JSON.");
    return false;
  }
  return Decode(json.object(), id, record, error);
}

bool AgentHistory::Save(AgentHistoryRecord &record, QString &error)
{
  error.clear();
  if(!ValidId(record.metadata.id) || !m_Lock || !m_Lock->isLocked() ||
     m_LockedId != record.metadata.id)
  {
    error = Tr("This window does not own the conversation. Open it read-only or start a new chat.");
    return false;
  }
  if(!Directory(false, error))
    return false;
  const QString path = RecordPath(record.metadata.id);
  if(IsLink(path) || (QFileInfo(path).exists() && !QFileInfo(path).isFile()))
  {
    error = Tr("The conversation destination is not a regular file.");
    return false;
  }
  AgentHistoryRecord updated = record;
  const QString now = QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
  if(updated.metadata.createdAt.isEmpty())
    updated.metadata.createdAt = now;
  updated.metadata.updatedAt = now;
  QJsonObject json;
  json[QStringLiteral("version")] = 1;
  json[QStringLiteral("metadata")] = MetadataJson(updated.metadata);
  json[QStringLiteral("conversation")] = updated.conversation;
  AgentHistoryRecord validated;
  if(!Decode(json, updated.metadata.id, validated, error))
    return false;
  const QByteArray bytes = QJsonDocument(json).toJson(QJsonDocument::Compact);
  if(bytes.size() > MaxRecordBytes)
  {
    error = Tr("The conversation exceeds the 8 MiB size limit and was not saved.");
    return false;
  }
  QSaveFile file(path);
  file.setDirectWriteFallback(false);
  if(!file.open(QIODevice::WriteOnly))
  {
    error = Tr("Could not save the conversation: %1").arg(file.errorString());
    return false;
  }
  if(file.write(bytes) != bytes.size())
  {
    error = Tr("Could not write the complete conversation: %1").arg(file.errorString());
    file.cancelWriting();
    return false;
  }
  if(!file.commit())
  {
    error = Tr("Could not commit the saved conversation: %1").arg(file.errorString());
    return false;
  }
  record.metadata = updated.metadata;
  return true;
}

QList<AgentHistoryEntry> AgentHistory::List(QString &warning) const
{
  warning.clear();
  QList<AgentHistoryEntry> entries;
  if(!Directory(false, warning))
    return entries;
  QList<QFileInfo> files;
  QDirIterator iterator(m_Directory, QStringList() << QStringLiteral("*.json"),
                        QDir::Files | QDir::NoDotAndDotDot, QDirIterator::NoIteratorFlags);
  while(iterator.hasNext())
  {
    iterator.next();
    if(files.size() == MaxDirectoryEntries)
    {
      warning = Tr("There are too many conversation files to list safely (limit: 4096).");
      return entries;
    }
    files << iterator.fileInfo();
  }
  std::sort(files.begin(), files.end(), [](const QFileInfo &a, const QFileInfo &b) {
    return a.lastModified() > b.lastModified();
  });
  int skipped = 0;
  bool limited = false;
  qint64 bytes = 0;
  for(const QFileInfo &file : files)
  {
    if(file.size() <= 0 || file.size() > MaxRecordBytes || IsLink(file.filePath()))
    {
      skipped++;
      continue;
    }
    if(entries.size() == MaxListEntries || bytes + file.size() > MaxListBytes)
    {
      limited = true;
      break;
    }
    bytes += file.size();
    AgentHistoryRecord record;
    QString error;
    if(!Load(file.completeBaseName(), record, error))
    {
      skipped++;
      continue;
    }
    // Do not acquire a writer lock while listing. Its existence is only a UI hint; stale locks
    // are resolved by Acquire(), and all actual writes require that authoritative check.
    record.metadata.locked = QFileInfo::exists(LockPath(record.metadata.id)) &&
                             record.metadata.id != m_LockedId;
    entries << record.metadata;
  }
  std::sort(entries.begin(), entries.end(), [](const AgentHistoryEntry &a, const AgentHistoryEntry &b) {
    return QDateTime::fromString(a.updatedAt, Qt::ISODate) >
           QDateTime::fromString(b.updatedAt, Qt::ISODate);
  });
  QStringList warnings;
  if(skipped > 0)
    warnings << Tr("Skipped %1 invalid or oversized conversation file(s).").arg(skipped);
  if(limited)
    warnings << Tr("Only recent conversations are shown (512 records / 64 MiB read limit).");
  warning = warnings.join(QLatin1Char('\n'));
  return entries;
}

QString AgentHistory::Select(const QList<AgentHistoryEntry> &entries, QWidget *parent)
{
  QDialog dialog(parent);
  dialog.setObjectName(QStringLiteral("agentHistory"));
  dialog.setWindowTitle(Tr("Conversation History"));
  dialog.resize(840, 480);
  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QLineEdit *search = new QLineEdit(&dialog);
  search->setObjectName(QStringLiteral("agentHistorySearch"));
  search->setPlaceholderText(Tr("Search conversations, captures, or models"));
  layout->addWidget(search);
  QTreeWidget *list = new QTreeWidget(&dialog);
  list->setObjectName(QStringLiteral("agentHistoryList"));
  list->setColumnCount(5);
  list->setHeaderLabels(QStringList() << Tr("Conversation") << Tr("Updated") << Tr("Capture")
                                     << Tr("Model") << Tr("Access"));
  list->setRootIsDecorated(false);
  list->setSelectionMode(QAbstractItemView::SingleSelection);
  list->header()->setSectionResizeMode(QHeaderView::ResizeToContents);
  list->header()->setSectionResizeMode(0, QHeaderView::Stretch);
  for(const AgentHistoryEntry &entry : entries)
  {
    QString capture = entry.captureIdentity[QStringLiteral("path")].toString();
    if(capture.isEmpty())
      capture = entry.captureIdentity[QStringLiteral("canonicalPath")].toString();
    const QDateTime updated = QDateTime::fromString(entry.updatedAt, Qt::ISODate).toLocalTime();
    QTreeWidgetItem *item = new QTreeWidgetItem(
        list, QStringList() << (entry.title.isEmpty() ? Tr("Untitled conversation") : entry.title)
                            << updated.toString(QStringLiteral("yyyy-MM-dd HH:mm"))
                            << (capture.isEmpty() ? Tr("No capture") : QFileInfo(capture).fileName())
                            << entry.model << (entry.locked ? Tr("In use; view only") : QString()));
    item->setData(0, Qt::UserRole, entry.id);
    item->setToolTip(0, entry.title);
    item->setToolTip(2, capture);
  }
  layout->addWidget(list, 1);
  QLabel *hint = new QLabel(Tr("Conversations in use in another window can be viewed read-only."), &dialog);
  hint->setWordWrap(true);
  layout->addWidget(hint);
  QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Open | QDialogButtonBox::Cancel,
                                                  &dialog);
  layout->addWidget(buttons);
  buttons->button(QDialogButtonBox::Open)->setEnabled(false);
  QObject::connect(list, &QTreeWidget::itemSelectionChanged, &dialog, [list, buttons]() {
    buttons->button(QDialogButtonBox::Open)->setEnabled(list->currentItem() &&
                                                       !list->currentItem()->isHidden());
  });
  QObject::connect(search, &QLineEdit::textChanged, &dialog, [list, buttons](const QString &text) {
    for(int i = 0; i < list->topLevelItemCount(); i++)
    {
      QTreeWidgetItem *item = list->topLevelItem(i);
      QString searchable;
      for(int column = 0; column < list->columnCount(); column++)
        searchable += item->text(column) + QLatin1Char(' ') + item->toolTip(column) + QLatin1Char(' ');
      item->setHidden(!searchable.contains(text, Qt::CaseInsensitive));
    }
    buttons->button(QDialogButtonBox::Open)->setEnabled(list->currentItem() &&
                                                       !list->currentItem()->isHidden());
  });
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(list, &QTreeWidget::itemDoubleClicked, &dialog,
                   [&dialog](QTreeWidgetItem *, int) { dialog.accept(); });
  if(dialog.exec() != QDialog::Accepted || !list->currentItem() || list->currentItem()->isHidden())
    return QString();
  return list->currentItem()->data(0, Qt::UserRole).toString();
}

#include "AgentCaptureIdentity.h"
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>

#if defined(Q_OS_WIN32)
#include <io.h>
#include <windows.h>
#else
#include <sys/stat.h>
#endif

namespace
{
const char *IdentitySchema = "rendertest-agent-capture-identity/v1";

QJsonObject Unverified(const QString &filename, const QString &reason)
{
  QJsonObject identity;
  identity[lit("schema")] = QString::fromLatin1(IdentitySchema);
  identity[lit("kind")] = lit("unverified");
  identity[lit("verified")] = false;
  identity[lit("path")] = filename;
  identity[lit("reason")] = reason;
  return identity;
}

bool Cancelled(const std::atomic<bool> *cancelled)
{
  return cancelled && cancelled->load(std::memory_order_relaxed);
}

QString CanonicalPath(const QFileInfo &info)
{
  QString canonical = QDir::fromNativeSeparators(info.canonicalFilePath());
#if defined(Q_OS_WIN32)
  // Use a file handle for short-name expansion; GetLongPathName can require directory listing
  // permissions which are unnecessary for reading the capture itself.
  QFile file(canonical);
  if(file.open(QIODevice::ReadOnly))
  {
    wchar_t path[32768] = {};
    HANDLE handle = (HANDLE)_get_osfhandle(file.handle());
    DWORD length = GetFinalPathNameByHandleW(handle, path, 32768, FILE_NAME_NORMALIZED);
    if(length > 0 && length < 32768)
    {
      canonical = QString::fromWCharArray(path, int(length));
      if(canonical.startsWith(lit("\\\\?\\UNC\\")))
        canonical = lit("\\\\") + canonical.mid(8);
      else if(canonical.startsWith(lit("\\\\?\\")))
        canonical = canonical.mid(4);
      canonical = QDir::fromNativeSeparators(canonical);
    }
  }
#endif
  return canonical;
}

QString HandleStamp(QFile &file)
{
#if defined(Q_OS_WIN32)
  BY_HANDLE_FILE_INFORMATION info = {};
  if(!GetFileInformationByHandle((HANDLE)_get_osfhandle(file.handle()), &info))
    return QString();
  return lit("%1:%2:%3:%4:%5:%6:%7")
      .arg(info.dwVolumeSerialNumber).arg(info.nFileIndexHigh).arg(info.nFileIndexLow)
      .arg(info.nFileSizeHigh).arg(info.nFileSizeLow)
      .arg(info.ftLastWriteTime.dwHighDateTime).arg(info.ftLastWriteTime.dwLowDateTime);
#else
  struct stat info = {};
  if(fstat(file.handle(), &info) != 0 || !S_ISREG(info.st_mode))
    return QString();
#if defined(Q_OS_MAC)
  const qint64 modifiedNs = info.st_mtimespec.tv_nsec;
  const qint64 changedNs = info.st_ctimespec.tv_nsec;
#else
  const qint64 modifiedNs = info.st_mtim.tv_nsec;
  const qint64 changedNs = info.st_ctim.tv_nsec;
#endif
  return lit("%1:%2:%3:%4:%5:%6:%7")
      .arg(qulonglong(info.st_dev)).arg(qulonglong(info.st_ino)).arg(qlonglong(info.st_size))
      .arg(qlonglong(info.st_mtime)).arg(modifiedNs).arg(qlonglong(info.st_ctime)).arg(changedNs);
#endif
}

bool SamePath(const QString &left, const QString &right)
{
#if defined(Q_OS_WIN32)
  return left.compare(right, Qt::CaseInsensitive) == 0;
#else
  return left == right;
#endif
}

bool ValidIdentity(const QJsonObject &identity)
{
  if(identity[lit("schema")].toString() != QString::fromLatin1(IdentitySchema) ||
     !identity[lit("verified")].isBool() || !identity[lit("verified")].toBool())
    return false;
  const QString kind = identity[lit("kind")].toString();
  if(kind == lit("none"))
    return true;
  if(kind != lit("local-file"))
    return false;
  const QString path = identity[lit("canonicalPath")].toString();
  static const QRegularExpression decimal(lit("^[0-9]{1,19}$"));
  static const QRegularExpression timestamp(lit("^-?[0-9]{1,19}$"));
  static const QRegularExpression digest(lit("^[0-9a-f]{64}$"));
  bool sizeOk = false, timeOk = false;
  const qint64 size = identity[lit("bytes")].toString().toLongLong(&sizeOk);
  identity[lit("modifiedUnixMs")].toString().toLongLong(&timeOk);
  return !path.isEmpty() && path.size() <= 32768 && QFileInfo(path).isAbsolute() &&
         !QDir::fromNativeSeparators(path).split(QLatin1Char('/')).contains(lit("..")) &&
         decimal.match(identity[lit("bytes")].toString()).hasMatch() && sizeOk && size > 0 &&
         timestamp.match(identity[lit("modifiedUnixMs")].toString()).hasMatch() && timeOk &&
         digest.match(identity[lit("sha256")].toString()).hasMatch();
}
}

QJsonObject AgentCaptureIdentity::NoCapture()
{
  QJsonObject identity;
  identity[lit("schema")] = QString::fromLatin1(IdentitySchema);
  identity[lit("kind")] = lit("none");
  identity[lit("verified")] = true;
  return identity;
}

QJsonObject AgentCaptureIdentity::Inspect(const QString &filename, bool local, bool temporary,
                                         const std::atomic<bool> *cancelled)
{
  if(Cancelled(cancelled))
    return Unverified(filename, lit("Capture identity verification was cancelled."));
  if(!local)
    return Unverified(filename, lit("Remote captures can only view saved conversations."));
  if(temporary)
    return Unverified(filename, lit("Save the temporary capture before resuming a conversation."));
  QFileInfo before(filename);
  if(filename.isEmpty() || !before.isAbsolute() || !before.isFile() || before.size() <= 0)
    return Unverified(filename, lit("The saved capture file is unavailable or empty."));
  const QString canonical = CanonicalPath(before);
  if(canonical.isEmpty())
    return Unverified(filename, lit("The capture path cannot be resolved."));
#if defined(Q_OS_WIN32)
  wchar_t volume[32768] = {};
  if(!GetVolumePathNameW((LPCWSTR)canonical.utf16(), volume, 32768))
    return Unverified(filename, lit("The capture volume cannot be verified."));
  const UINT driveType = GetDriveTypeW(volume);
  if(canonical.startsWith(lit("//")) || driveType == DRIVE_REMOTE || driveType == DRIVE_UNKNOWN ||
     driveType == DRIVE_NO_ROOT_DIR)
    return Unverified(filename, lit("Network or unverifiable volumes cannot resume capture conversations."));
#else
  if(canonical.startsWith(lit("//")))
    return Unverified(filename, lit("Network paths cannot resume capture conversations."));
#endif
  QFile file(canonical);
  if(!file.open(QIODevice::ReadOnly))
    return Unverified(filename, lit("The capture file could not be opened for verification."));
  const QString stamp = HandleStamp(file);
  if(stamp.isEmpty())
    return Unverified(filename, lit("The capture file identity could not be checked."));
  QCryptographicHash digest(QCryptographicHash::Sha256);
  qint64 readBytes = 0;
  while(!file.atEnd())
  {
    if(Cancelled(cancelled))
      return Unverified(filename, lit("Capture identity verification was cancelled."));
    const QByteArray chunk = file.read(1024 * 1024);
    if(chunk.isEmpty() && !file.atEnd())
      return Unverified(filename, lit("The capture file could not be read completely."));
    digest.addData(chunk);
    readBytes += chunk.size();
  }
  if(Cancelled(cancelled))
    return Unverified(filename, lit("Capture identity verification was cancelled."));
  QFileInfo after(filename);
  QFile current(canonical);
  if(file.error() != QFile::NoError || !after.lastModified().isValid() ||
     readBytes != before.size() || after.size() != before.size() ||
     after.lastModified() != before.lastModified() || HandleStamp(file) != stamp ||
     !current.open(QIODevice::ReadOnly) || HandleStamp(current) != stamp ||
     CanonicalPath(after) != canonical)
    return Unverified(filename, lit("The capture file changed during verification; retry after it is stable."));

  QJsonObject identity;
  identity[lit("schema")] = QString::fromLatin1(IdentitySchema);
  identity[lit("kind")] = lit("local-file");
  identity[lit("verified")] = true;
  identity[lit("canonicalPath")] = canonical;
  identity[lit("bytes")] = QString::number(readBytes);
  identity[lit("modifiedUnixMs")] = QString::number(after.lastModified().toMSecsSinceEpoch());
  identity[lit("sha256")] = QString::fromLatin1(digest.result().toHex());
  return identity;
}

bool AgentCaptureIdentity::Matches(const QJsonObject &saved, const QJsonObject &current, QString *reason)
{
  auto fail = [reason](const QString &message) { if(reason) *reason = message; return false; };
  if(!ValidIdentity(saved) || !ValidIdentity(current))
    return fail(lit("The saved or current capture cannot be verified. This conversation is read-only."));
  if(saved[lit("kind")] != current[lit("kind")])
    return fail(lit("This conversation belongs to a different capture context."));
  if(saved[lit("kind")].toString() == lit("none"))
  {
    if(reason) reason->clear();
    return true;
  }
  if(!SamePath(saved[lit("canonicalPath")].toString(), current[lit("canonicalPath")].toString()) ||
     saved[lit("bytes")] != current[lit("bytes")] ||
     saved[lit("modifiedUnixMs")] != current[lit("modifiedUnixMs")] ||
     saved[lit("sha256")] != current[lit("sha256")])
    return fail(lit("The capture file, size, modification time or full SHA-256 differs. This conversation is read-only."));
  if(reason) reason->clear();
  return true;
}

bool AgentCaptureIdentity::FileUnchanged(const QJsonObject &identity, const QString &filename)
{
  if(!ValidIdentity(identity) || identity[lit("kind")].toString() != lit("local-file"))
    return false;
  QFileInfo file(filename);
  return file.isAbsolute() && file.isFile() &&
         SamePath(CanonicalPath(file), identity[lit("canonicalPath")].toString()) &&
         QString::number(file.size()) == identity[lit("bytes")].toString() &&
         QString::number(file.lastModified().toMSecsSinceEpoch()) ==
             identity[lit("modifiedUnixMs")].toString();
}

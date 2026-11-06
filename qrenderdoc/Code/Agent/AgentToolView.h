#pragma once

#include <QImage>
#include <QList>
#include <QString>
#include <QUrl>
#include "AgentConversation.h"

struct AgentToolImage
{
  QUrl resourceUrl;
  QString canonicalPath;
  QImage thumbnail;
  QString error;
  bool IsValid() const { return error.isEmpty() && !thumbnail.isNull(); }
};

// GUI-thread helpers with no QWidget dependency. The caller installs returned thumbnails with
// QTextDocument::addResource and must disable QTextBrowser's automatic file/network loading.
namespace AgentToolView
{
QString ItemKey(const AgentItem &item);
QString Render(const AgentItem &item, bool expanded, const QString &agentRoot,
               QList<AgentToolImage> *images = NULL);
// A user click must pass forceRevalidate=true before opening canonicalPath externally.
AgentToolImage ValidatedImage(const AgentItem &item, int artifactIndex, const QString &agentRoot,
                             bool forceRevalidate = false);
void ClearCache();
}

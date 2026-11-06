#include "AgentConversation.h"
#include <QJsonArray>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QVariant>
#include <cmath>

namespace
{
const int MaxArgumentsBytes = 8192;
const int MaxResultBytes = 4096;
const int MaxArtifacts = 8;
const int MaxSnapshotBytes = 16 * 1024 * 1024;
const int MaxSnapshotItems = 10000;
const int MaxSnapshotTurns = 2000;
const int MaxItemTextBytes = 2 * 1024 * 1024;
const char *SnapshotSchema = "rendertest-agent-conversation/v1";

bool ReadString(const QJsonObject &json, const QString &key, int maxBytes, QString &value)
{
  const QJsonValue entry = json[key];
  if(entry.isUndefined())
  {
    value.clear();
    return true;
  }
  if(!entry.isString())
    return false;
  value = entry.toString();
  return value.toUtf8().size() <= maxBytes;
}

bool ReadInteger(const QJsonObject &json, const QString &key, qint64 fallback, qint64 minimum,
                 qint64 &value)
{
  const QJsonValue entry = json[key];
  if(entry.isUndefined())
  {
    value = fallback;
    return true;
  }
  const double number = entry.toDouble(-1.0);
  if(!entry.isDouble() || !std::isfinite(number) || number < double(minimum) ||
     number > 9007199254740991.0 || std::floor(number) != number)
    return false;
  value = qint64(number);
  return true;
}

bool ReadKind(const QString &name, AgentItem::Kind &kind)
{
  if(name == lit("user")) kind = AgentItem::Kind::User;
  else if(name == lit("agent")) kind = AgentItem::Kind::Agent;
  else if(name == lit("reasoning")) kind = AgentItem::Kind::Reasoning;
  else if(name == lit("tool")) kind = AgentItem::Kind::Tool;
  else if(name == lit("notice")) kind = AgentItem::Kind::Notice;
  else if(name == lit("other")) kind = AgentItem::Kind::Other;
  else return false;
  return true;
}

QString Compact(const QJsonValue &value, int maxBytes)
{
  QString text;
  if(value.isObject())
    text = QString::fromUtf8(QJsonDocument(value.toObject()).toJson(QJsonDocument::Compact));
  else if(value.isArray())
    text = QString::fromUtf8(QJsonDocument(value.toArray()).toJson(QJsonDocument::Compact));
  else if(value.isString())
    text = value.toString();
  else if(!value.isNull() && !value.isUndefined())
    text = value.toVariant().toString();
  QByteArray utf8 = text.toUtf8();
  if(utf8.size() > maxBytes)
  {
    int end = maxBytes - 3;
    while(end > 0 && (quint8(utf8[end]) & 0xc0) == 0x80)
      end--;
    text = QString::fromUtf8(utf8.constData(), end) + lit("...");
  }
  return text;
}

QJsonObject ArtifactEnvelope(const QJsonObject &result)
{
  QJsonObject envelope = result[lit("structuredContent")].toObject();
  if(envelope[lit("schema")].toString() == lit("rendertest-mcp-result/v1"))
    return envelope;
  // export_texture returns an MCP content list, so its envelope is a JSON text block.
  for(const QJsonValue &value : result[lit("content")].toArray())
  {
    const QJsonObject content = value.toObject();
    if(content[lit("type")].toString() != lit("text"))
      continue;
    envelope = QJsonDocument::fromJson(content[lit("text")].toString().toUtf8()).object();
    if(envelope[lit("schema")].toString() == lit("rendertest-mcp-result/v1"))
      return envelope;
  }
  return QJsonObject();
}

QString JoinStrings(const QJsonArray &array)
{
  QStringList parts;
  for(const QJsonValue &value : array)
  {
    if(value.isString())
      parts << value.toString();
    else if(value.isObject() && value.toObject().contains(lit("text")))
      parts << value.toObject()[lit("text")].toString();
  }
  return parts.join(lit("\n"));
}

const char *KindName(AgentItem::Kind kind)
{
  switch(kind)
  {
    case AgentItem::Kind::User: return "user";
    case AgentItem::Kind::Agent: return "agent";
    case AgentItem::Kind::Reasoning: return "reasoning";
    case AgentItem::Kind::Tool: return "tool";
    case AgentItem::Kind::Notice: return "notice";
    case AgentItem::Kind::Other: break;
  }
  return "other";
}
}

void AgentConversation::Clear()
{
  m_Items.clear();
  m_Turns.clear();
  m_ClosedTurnIds.clear();
  m_ThreadId.clear();
  m_Model.clear();
  m_TurnId.clear();
  m_LastTurnStatus.clear();
  m_ReferenceToken.clear();
  m_TurnRunning = false;
  m_TotalTokens = 0;
}

void AgentConversation::SetThread(const QString &threadId, const QString &model)
{
  m_ThreadId = threadId;
  m_Model = model;
}

void AgentConversation::AddUserMessage(const QString &text, const QJsonArray &context)
{
  AgentItem item;
  item.kind = AgentItem::Kind::User;
  item.type = lit("userMessage");
  item.text = text;
  item.context = context;
  m_Items << item;
}

void AgentConversation::AddNotice(const QString &level, const QString &text)
{
  AgentItem item;
  item.kind = AgentItem::Kind::Notice;
  item.type = level;
  item.turnId = m_TurnRunning ? m_TurnId : QString();
  item.text = text;
  m_Items << item;
}

void AgentConversation::BeginTurn(const QString &referenceToken)
{
  m_ReferenceToken = referenceToken;
  m_TurnRunning = true;
  m_TurnId.clear();
}

void AgentConversation::SetTurnId(const QString &turnId)
{
  if(m_TurnRunning && m_TurnId.isEmpty() && !m_ClosedTurnIds.contains(turnId))
    m_TurnId = turnId;
}

void AgentConversation::EndTurn(const QString &status, const QString &message)
{
  if(!m_TurnRunning)
    return;

  for(AgentItem &item : m_Items)
  {
    if(item.kind == AgentItem::Kind::Tool && item.status == lit("inProgress"))
      item.status = status == lit("interrupted") ? lit("interrupted") : lit("failed");
  }

  QJsonObject turn;
  turn[lit("id")] = m_TurnId;
  turn[lit("status")] = status;
  if(!message.isEmpty())
    turn[lit("error")] = message;
  m_Turns << turn;
  if(!m_TurnId.isEmpty())
    m_ClosedTurnIds.insert(m_TurnId);

  m_LastTurnStatus = status;
  if(!message.isEmpty())
    AddNotice(status == lit("interrupted") ? lit("warning") : lit("error"), message);
  m_TurnRunning = false;
}

AgentItem *AgentConversation::Find(const QString &id)
{
  if(id.isEmpty())
    return NULL;
  for(int i = m_Items.size() - 1; i >= 0; i--)
  {
    if(m_Items[i].id == id)
      return &m_Items[i];
  }
  return NULL;
}

void AgentConversation::UpdateFromItem(AgentItem &item, const QJsonObject &json, bool completed)
{
  const QString type = json[lit("type")].toString();
  item.type = type;

  if(type == lit("agentMessage"))
  {
    item.kind = AgentItem::Kind::Agent;
    QString text = json[lit("text")].toString();
    if(completed || !text.isEmpty())
      item.text = text;
  }
  else if(type == lit("reasoning"))
  {
    item.kind = AgentItem::Kind::Reasoning;
    QString summary = JoinStrings(json[lit("summary")].toArray());
    if(completed || !summary.isEmpty())
      item.text = summary;
  }
  else if(type == lit("mcpToolCall"))
  {
    item.kind = AgentItem::Kind::Tool;
    item.server = json[lit("server")].toString();
    item.tool = json[lit("tool")].toString();
    item.status = json[lit("status")].toString();
    item.arguments = Compact(json[lit("arguments")], MaxArgumentsBytes);
    item.durationMs = json[lit("durationMs")].isDouble()
                          ? qint64(json[lit("durationMs")].toDouble())
                          : -1;
    QJsonObject error = json[lit("error")].toObject();
    item.error = Compact(error[lit("message")], MaxResultBytes);
    item.result.clear();
    item.artifacts = QJsonArray();
    item.artifactInstanceId.clear();
    item.artifactCaptureSessionId.clear();
    if(completed)
      item.progress.clear();
    QJsonObject result = json[lit("result")].toObject();
    if(!result.isEmpty())
    {
      QJsonValue structured = result[lit("structuredContent")];
      item.result =
          Compact(structured.isObject() ? structured : result[lit("content")], MaxResultBytes);
      if(result[lit("isError")].toBool())
      {
        item.status = lit("failed");
        if(item.error.isEmpty())
          item.error = Compact(result[lit("content")], MaxResultBytes);
      }
      if(item.server == lit("rendertest"))
      {
        const QJsonObject envelope = ArtifactEnvelope(result);
        const QString session = envelope[lit("captureSessionId")].toString();
        const QString metaSession =
            envelope[lit("meta")].toObject()[lit("captureSessionId")].toString();
        if(!session.isEmpty() && metaSession == session)
        {
          item.artifactInstanceId = envelope[lit("instanceId")].toString();
          item.artifactCaptureSessionId = session;
          for(const QJsonValue &artifact : envelope[lit("artifacts")].toArray())
          {
            const QJsonObject value = artifact.toObject();
            if(!value.isEmpty() && item.artifacts.size() < MaxArtifacts &&
               value[lit("path")].toString().size() <= 4096)
            {
              QJsonObject metadata;
              metadata[lit("path")] = value[lit("path")].toString();
              metadata[lit("mediaType")] = value[lit("mediaType")].toString().left(64);
              const double bytes = value[lit("bytes")].toDouble(-1);
              metadata[lit("bytes")] = std::isfinite(bytes) && bytes >= 0.0 &&
                                                bytes <= 9007199254740991.0
                                            ? bytes
                                            : -1.0;
              metadata[lit("sha256")] = value[lit("sha256")].toString().left(65);
              item.artifacts << metadata;
            }
          }
        }
      }
    }
  }
  else if(type == lit("commandExecution"))
  {
    item.kind = AgentItem::Kind::Other;
    item.status = json[lit("status")].toString();
    item.text = lit("Command (%1): %2").arg(item.status, json[lit("command")].toString());
  }
  else if(type == lit("fileChange"))
  {
    item.kind = AgentItem::Kind::Other;
    item.status = json[lit("status")].toString();
    item.text = lit("File change (%1)").arg(item.status);
  }
  else
  {
    item.kind = AgentItem::Kind::Other;
    item.status = json[lit("status")].toString();
    item.text = type;
  }
}

bool AgentConversation::Apply(const QString &method, const QJsonObject &params)
{
  const QString threadId = params[lit("threadId")].toString();
  if(m_ThreadId.isEmpty() || threadId != m_ThreadId)
    return false;

  if(method != lit("thread/tokenUsage/updated") && !m_TurnRunning)
    return false;
  const QString turnId = method == lit("turn/started") || method == lit("turn/completed")
                             ? params[lit("turn")].toObject()[lit("id")].toString()
                             : params[lit("turnId")].toString();
  if(!turnId.isEmpty() && m_ClosedTurnIds.contains(turnId))
    return false;
  if(!turnId.isEmpty() && !m_TurnId.isEmpty() && turnId != m_TurnId)
    return false;
  // Some servers emit an item before the start response/notification. Claim that turn, but
  // never let a late notification from an already-finished turn claim a new pending question.
  if(!turnId.isEmpty())
    SetTurnId(turnId);

  if(method == lit("turn/started"))
  {
    SetTurnId(params[lit("turn")].toObject()[lit("id")].toString());
    return false;
  }

  if(method == lit("item/started") || method == lit("item/completed"))
  {
    const bool completed = method == lit("item/completed");
    QJsonObject json = params[lit("item")].toObject();
    const QString type = json[lit("type")].toString();
    // user messages are shown from the local copy, without the context preamble
    if(type == lit("userMessage"))
      return false;

    const QString id = json[lit("id")].toString();
    AgentItem *item = Find(id);
    if(!item)
    {
      AgentItem created;
      created.referenceToken = m_ReferenceToken;
      created.id = id;
      created.turnId = params[lit("turnId")].toString();
      m_Items << created;
      item = &m_Items.last();
    }
    UpdateFromItem(*item, json, completed);
    return true;
  }

  if(method == lit("item/agentMessage/delta") || method == lit("item/reasoning/summaryTextDelta"))
  {
    const bool reasoning = method != lit("item/agentMessage/delta");
    const QString id = params[lit("itemId")].toString();
    AgentItem *item = Find(id);
    if(!item)
    {
      AgentItem created;
      created.referenceToken = m_ReferenceToken;
      created.id = id;
      created.turnId = params[lit("turnId")].toString();
      created.kind = reasoning ? AgentItem::Kind::Reasoning : AgentItem::Kind::Agent;
      created.type = reasoning ? lit("reasoning") : lit("agentMessage");
      m_Items << created;
      item = &m_Items.last();
    }
    item->text += params[lit("delta")].toString();
    return true;
  }

  if(method == lit("item/mcpToolCall/progress"))
  {
    AgentItem *item = Find(params[lit("itemId")].toString());
    if(!item)
      return false;
    item->progress = params[lit("message")].toString();
    return true;
  }

  if(method == lit("thread/tokenUsage/updated"))
  {
    QJsonObject total = params[lit("tokenUsage")].toObject()[lit("total")].toObject();
    m_TotalTokens = qint64(total[lit("totalTokens")].toDouble());
    return true;
  }

  if(method == lit("error"))
  {
    QString message = params[lit("error")].toObject()[lit("message")].toString();
    bool willRetry = params[lit("willRetry")].toBool(false);
    AddNotice(willRetry ? lit("warning") : lit("error"),
              willRetry ? lit("%1 (retrying)").arg(message) : message);
    return true;
  }

  if(method == lit("turn/completed"))
  {
    QJsonObject turn = params[lit("turn")].toObject();
    const QString id = turn[lit("id")].toString();
    if(!m_TurnRunning || (!m_TurnId.isEmpty() && !id.isEmpty() && id != m_TurnId))
      return false;
    if(m_TurnId.isEmpty())
      m_TurnId = id;

    QString status = turn[lit("status")].toString();
    if(status.isEmpty())
      status = lit("completed");
    QString message;
    if(status == lit("failed"))
    {
      message = turn[lit("error")].toObject()[lit("message")].toString();
      if(message.isEmpty())
        message = lit("The turn failed.");
    }
    else if(status == lit("interrupted"))
    {
      message = lit("Stopped.");
    }
    EndTurn(status, message);
    return true;
  }

  return false;
}

QJsonObject AgentConversation::ToJson() const
{
  QJsonArray items;
  for(const AgentItem &item : m_Items)
  {
    QJsonObject json;
    json[lit("kind")] = QString::fromLatin1(KindName(item.kind));
    json[lit("type")] = item.type;
    if(!item.referenceToken.isEmpty())
      json[lit("referenceToken")] = item.referenceToken;
    if(!item.id.isEmpty())
      json[lit("id")] = item.id;
    if(!item.turnId.isEmpty())
      json[lit("turnId")] = item.turnId;
    if(!item.text.isEmpty())
      json[lit("text")] = item.text;
    if(item.kind == AgentItem::Kind::Tool)
    {
      json[lit("server")] = item.server;
      json[lit("tool")] = item.tool;
      json[lit("status")] = item.status;
      json[lit("arguments")] = item.arguments;
      if(item.durationMs >= 0)
        json[lit("durationMs")] = double(item.durationMs);
      if(!item.progress.isEmpty())
        json[lit("progress")] = item.progress;
      if(!item.error.isEmpty())
        json[lit("error")] = item.error;
      if(!item.result.isEmpty())
        json[lit("result")] = item.result;
      if(!item.artifacts.isEmpty())
      {
        json[lit("artifactInstanceId")] = item.artifactInstanceId;
        json[lit("artifactCaptureSessionId")] = item.artifactCaptureSessionId;
        json[lit("artifacts")] = item.artifacts;
      }
    }
    else if(!item.status.isEmpty())
    {
      json[lit("status")] = item.status;
    }
    if(!item.context.isEmpty())
      json[lit("context")] = item.context;
    items << json;
  }

  QJsonArray turns;
  for(const QJsonObject &turn : m_Turns)
    turns << turn;

  QJsonObject result;
  result[lit("schema")] = QString::fromLatin1(SnapshotSchema);
  result[lit("threadId")] = m_ThreadId;
  result[lit("model")] = m_Model;
  result[lit("turns")] = turns;
  result[lit("items")] = items;
  result[lit("totalTokens")] = double(m_TotalTokens);
  result[lit("turnRunning")] = m_TurnRunning;
  result[lit("turnId")] = m_TurnId;
  result[lit("lastTurnStatus")] = m_LastTurnStatus;
  return result;
}

bool AgentConversation::Load(const QJsonObject &snapshot, QString &error)
{
  auto fail = [&error](const QString &message) { error = message; return false; };
  if(snapshot[lit("schema")].toString() != QString::fromLatin1(SnapshotSchema) ||
     !snapshot[lit("items")].isArray() || !snapshot[lit("turns")].isArray() ||
     !snapshot[lit("turnRunning")].isBool())
    return fail(lit("The saved conversation has an unsupported or incomplete format."));
  if(snapshot[lit("items")].toArray().size() > MaxSnapshotItems ||
     snapshot[lit("turns")].toArray().size() > MaxSnapshotTurns ||
     QJsonDocument(snapshot).toJson(QJsonDocument::Compact).size() > MaxSnapshotBytes)
    return fail(lit("The saved conversation exceeds the history size limit."));

  AgentConversation restored;
  QString savedTurnId;
  if(!ReadString(snapshot, lit("threadId"), 1024, restored.m_ThreadId) ||
     !ReadString(snapshot, lit("model"), 1024, restored.m_Model) ||
     !ReadString(snapshot, lit("turnId"), 1024, savedTurnId) ||
     !ReadString(snapshot, lit("lastTurnStatus"), 64, restored.m_LastTurnStatus) ||
     !ReadInteger(snapshot, lit("totalTokens"), 0, 0, restored.m_TotalTokens))
    return fail(lit("The saved conversation contains invalid identifiers or token usage."));

  bool interrupted = snapshot[lit("turnRunning")].toBool();
  QSet<QString> itemIds;
  for(const QJsonValue &value : snapshot[lit("items")].toArray())
  {
    if(!value.isObject())
      return fail(lit("A saved conversation item is invalid."));
    const QJsonObject json = value.toObject();
    AgentItem item;
    if(!ReadKind(json[lit("kind")].toString(), item.kind) ||
       !ReadString(json, lit("type"), 128, item.type) ||
       !ReadString(json, lit("id"), 1024, item.id) ||
       !ReadString(json, lit("turnId"), 1024, item.turnId) ||
       !ReadString(json, lit("text"), MaxItemTextBytes, item.text) ||
       !ReadString(json, lit("status"), 64, item.status) ||
       !ReadString(json, lit("server"), 256, item.server) ||
       !ReadString(json, lit("tool"), 256, item.tool) ||
       !ReadString(json, lit("arguments"), MaxArgumentsBytes, item.arguments) ||
       !ReadString(json, lit("progress"), MaxItemTextBytes, item.progress) ||
       !ReadString(json, lit("error"), MaxResultBytes, item.error) ||
       !ReadString(json, lit("result"), MaxResultBytes, item.result) ||
       !ReadString(json, lit("artifactInstanceId"), 128, item.artifactInstanceId) ||
       !ReadString(json, lit("artifactCaptureSessionId"), 128, item.artifactCaptureSessionId) ||
       !ReadInteger(json, lit("durationMs"), -1, -1, item.durationMs))
      return fail(lit("A saved conversation item contains invalid or oversized fields."));
    if(!item.id.isEmpty())
    {
      if(itemIds.contains(item.id))
        return fail(lit("The saved conversation contains duplicate item identifiers."));
      itemIds.insert(item.id);
    }
    if(!item.turnId.isEmpty())
      restored.m_ClosedTurnIds.insert(item.turnId);
    // Never copy a stored token: disk content cannot grant permission to act on the current UI.
    item.referenceToken.clear();
    if(item.status == lit("inProgress"))
    {
      item.status = lit("interrupted");
      item.progress.clear();
      interrupted = true;
    }
    if(json.contains(lit("context")))
    {
      if(!json[lit("context")].isArray() || json[lit("context")].toArray().size() > 64)
        return fail(lit("The saved message context is invalid."));
      for(const QJsonValue &context : json[lit("context")].toArray())
      {
        const QJsonObject chip = context.toObject();
        if(!context.isObject() || !chip[lit("label")].isString() ||
           QJsonDocument(chip).toJson(QJsonDocument::Compact).size() > 64 * 1024)
          return fail(lit("A saved context chip is invalid or too large."));
      }
      item.context = json[lit("context")].toArray();
    }
    if(json.contains(lit("artifacts")))
    {
      if(!json[lit("artifacts")].isArray() || json[lit("artifacts")].toArray().size() > MaxArtifacts)
        return fail(lit("Saved artifact metadata is invalid."));
      for(const QJsonValue &artifact : json[lit("artifacts")].toArray())
      {
        const QJsonObject metadata = artifact.toObject();
        QString path, mediaType, digest;
        const double bytes = metadata[lit("bytes")].toDouble(-1);
        if(!artifact.isObject() || !ReadString(metadata, lit("path"), 16384, path) ||
           !ReadString(metadata, lit("mediaType"), 256, mediaType) ||
           !ReadString(metadata, lit("sha256"), 256, digest) ||
           !std::isfinite(bytes) || bytes < -1.0 || bytes > 9007199254740991.0)
          return fail(lit("A saved artifact has invalid metadata."));
        QJsonObject bounded;
        bounded[lit("path")] = path;
        bounded[lit("mediaType")] = mediaType;
        bounded[lit("bytes")] = bytes;
        bounded[lit("sha256")] = digest;
        item.artifacts << bounded;
      }
    }
    restored.m_Items << item;
  }

  bool savedTurnRecorded = false;
  QSet<QString> turnIds;
  for(const QJsonValue &value : snapshot[lit("turns")].toArray())
  {
    const QJsonObject json = value.toObject();
    QString id, status, message;
    if(!value.isObject() || !ReadString(json, lit("id"), 1024, id) ||
       !ReadString(json, lit("status"), 64, status) ||
       !ReadString(json, lit("error"), MaxItemTextBytes, message) ||
       (!id.isEmpty() && turnIds.contains(id)))
      return fail(lit("Saved turn metadata is invalid."));
    if(status == lit("inProgress"))
    {
      status = lit("interrupted");
      interrupted = true;
    }
    if(status != lit("completed") && status != lit("interrupted") && status != lit("failed"))
      return fail(lit("The saved conversation contains an unknown turn state."));
    QJsonObject turn;
    turn[lit("id")] = id;
    turn[lit("status")] = status;
    if(!message.isEmpty())
      turn[lit("error")] = message;
    restored.m_Turns << turn;
    if(!id.isEmpty())
    {
      turnIds.insert(id);
      restored.m_ClosedTurnIds.insert(id);
    }
    savedTurnRecorded = savedTurnRecorded || (!savedTurnId.isEmpty() && id == savedTurnId);
  }
  if(interrupted)
  {
    if(!savedTurnRecorded)
    {
      QJsonObject turn;
      turn[lit("id")] = savedTurnId;
      turn[lit("status")] = lit("interrupted");
      turn[lit("error")] = lit("The conversation was saved before the turn finished.");
      restored.m_Turns << turn;
    }
    restored.m_LastTurnStatus = lit("interrupted");
  }
  if(!savedTurnId.isEmpty())
    restored.m_ClosedTurnIds.insert(savedTurnId);
  if(restored.m_LastTurnStatus.isEmpty() && !restored.m_Turns.isEmpty())
    restored.m_LastTurnStatus = restored.m_Turns.last()[lit("status")].toString();
  restored.m_TurnRunning = false;
  restored.m_TurnId.clear();
  restored.m_ReferenceToken.clear();
  *this = restored;
  error.clear();
  return true;
}

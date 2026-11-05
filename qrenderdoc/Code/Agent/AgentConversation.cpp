#include "AgentConversation.h"
#include <QJsonArray>
#include <QJsonDocument>

namespace
{
const int MaxArgumentsChars = 400;
const int MaxResultChars = 4096;

QString Compact(const QJsonValue &value, int maxChars)
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
  if(text.size() > maxChars)
    text = text.left(maxChars) + lit("...");
  return text;
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
  m_ThreadId.clear();
  m_Model.clear();
  m_TurnId.clear();
  m_LastTurnStatus.clear();
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

void AgentConversation::BeginTurn()
{
  m_TurnRunning = true;
  m_TurnId.clear();
}

void AgentConversation::SetTurnId(const QString &turnId)
{
  if(m_TurnRunning && m_TurnId.isEmpty())
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
    item.arguments = Compact(json[lit("arguments")], MaxArgumentsChars);
    if(json[lit("durationMs")].isDouble())
      item.durationMs = qint64(json[lit("durationMs")].toDouble());
    QJsonObject error = json[lit("error")].toObject();
    item.error = error[lit("message")].toString();
    QJsonObject result = json[lit("result")].toObject();
    if(!result.isEmpty())
    {
      QJsonValue structured = result[lit("structuredContent")];
      item.result =
          Compact(structured.isObject() ? structured : result[lit("content")], MaxResultChars);
    }
  }
  else if(type == lit("commandExecution"))
  {
    item.kind = AgentItem::Kind::Other;
    item.status = json[lit("status")].toString();
    item.text = lit("Command blocked (read-only): %1").arg(json[lit("command")].toString());
  }
  else if(type == lit("fileChange"))
  {
    item.kind = AgentItem::Kind::Other;
    item.status = json[lit("status")].toString();
    item.text = lit("File change blocked (read-only)");
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
  if(!threadId.isEmpty() && !m_ThreadId.isEmpty() && threadId != m_ThreadId)
    return false;

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
  result[lit("threadId")] = m_ThreadId;
  result[lit("model")] = m_Model;
  result[lit("turns")] = turns;
  result[lit("items")] = items;
  result[lit("totalTokens")] = double(m_TotalTokens);
  return result;
}

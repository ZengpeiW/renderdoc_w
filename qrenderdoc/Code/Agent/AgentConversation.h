#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QSet>
#include <QString>

// One rendered entry of the AI panel transcript.
struct AgentItem
{
  enum class Kind
  {
    User,
    Agent,
    Reasoning,
    Tool,
    Notice,
    Other,
  };

  Kind kind = Kind::Other;
  // app-server item id, empty for local entries
  QString id;
  // app-server item type, or the notice level (info, warning, error) for notices
  QString type;
  QString turnId;
  // Assigned locally, never taken from model text. Invalidated on capture changes.
  QString referenceToken;
  // message text, reasoning summary, notice text or a one-line summary for other items
  QString text;
  // tool calls
  QString server;
  QString tool;
  QString status;
  QString arguments;
  QString progress;
  QString error;
  QString result;
  // Parsed from the original MCP result envelope, before the displayed JSON is truncated.
  // These are untrusted metadata; AgentToolView validates scope, path, size and digest.
  QString artifactInstanceId;
  QString artifactCaptureSessionId;
  QJsonArray artifacts;
  qint64 durationMs = -1;
  // user messages: the context chips attached when it was sent
  QJsonArray context;
};

// Reduces app-server notifications into an ordered list of items. Has no UI dependency so it can
// be replayed from recorded JSONL.
class AgentConversation
{
public:
  void Clear();

  void SetThread(const QString &threadId, const QString &model);
  const QString &ThreadId() const { return m_ThreadId; }
  const QString &Model() const { return m_Model; }

  void AddUserMessage(const QString &text, const QJsonArray &context = QJsonArray());
  void AddNotice(const QString &level, const QString &text);

  // the turn was requested; the id arrives with the turn/start response or turn/started
  void BeginTurn(const QString &referenceToken);
  void SetTurnId(const QString &turnId);
  // ends the running turn locally, e.g. when the app-server was killed or crashed
  void EndTurn(const QString &status, const QString &message);

  bool TurnRunning() const { return m_TurnRunning; }
  const QString &TurnId() const { return m_TurnId; }
  const QString &LastTurnStatus() const { return m_LastTurnStatus; }
  qint64 TotalTokens() const { return m_TotalTokens; }

  // applies a notification; returns true if anything visible changed
  bool Apply(const QString &method, const QJsonObject &params);

  const QList<AgentItem> &Items() const { return m_Items; }
  QJsonObject ToJson() const;
  // Atomically restores our local snapshot. Never restores a running turn or reference tokens;
  // historical EID/resource links remain invalid even when the capture itself matches.
  bool Load(const QJsonObject &snapshot, QString &error);

private:
  AgentItem *Find(const QString &id);
  void UpdateFromItem(AgentItem &item, const QJsonObject &json, bool completed);

  QList<AgentItem> m_Items;
  QString m_ThreadId;
  QString m_Model;
  QString m_TurnId;
  QString m_LastTurnStatus;
  QString m_ReferenceToken;
  QList<QJsonObject> m_Turns;
  QSet<QString> m_ClosedTurnIds;
  bool m_TurnRunning = false;
  qint64 m_TotalTokens = 0;
};

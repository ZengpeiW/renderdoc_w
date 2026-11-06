#pragma once

#include "AgentLaunchConfig.h"
#include "CodexAppServerClient.h"

// The panel-facing backend contract. Codex speaks the v1 wire format directly; the Claude SDK
// adapter translates its native sessions/events into this format. The legacy thread/item wire
// names are retained so transcript rendering is independent of either SDK. Process supervision
// and refusal of unsolicited permissions are shared by both implementations.
class AgentBackend : public CodexAppServerClient
{
public:
  enum class Operation
  {
    Initialize,
    ReadAccount,
    ListModels,
    Login,
    CancelLogin,
    SetSkillRoots,
    ReadConfiguration,
    CreateSession,
    ResumeSession,
    ListToolServers,
    SendTurn,
    InterruptTurn,
  };

  explicit AgentBackend(QObject *parent = NULL) : CodexAppServerClient(parent) {}

  bool Start(const AgentLaunchConfig &launch, QString &error)
  {
    m_Id = launch.backend;
    return CodexAppServerClient::Start(launch.program, launch.arguments, launch.workingDir, error);
  }

  QString Id() const { return m_Id; }
  QString DisplayName() const { return m_Id == QStringLiteral("claude") ? QStringLiteral("Claude") : QStringLiteral("Codex"); }
  bool SupportsBrowserLogin() const { return m_Id == QStringLiteral("codex"); }

  void Initialized() { Notify(QStringLiteral("initialized"), QJsonObject()); }

  qint64 Request(Operation operation, const QJsonObject &params, int timeoutMs,
                 ResponseCallback callback)
  {
    const char *method = "";
    switch(operation)
    {
      case Operation::Initialize: method = "initialize"; break;
      case Operation::ReadAccount: method = "account/read"; break;
      case Operation::ListModels: method = "model/list"; break;
      case Operation::Login: method = "account/login/start"; break;
      case Operation::CancelLogin: method = "account/login/cancel"; break;
      case Operation::SetSkillRoots: method = "skills/extraRoots/set"; break;
      case Operation::ReadConfiguration: method = "config/read"; break;
      case Operation::CreateSession: method = "thread/start"; break;
      case Operation::ResumeSession: method = "thread/resume"; break;
      case Operation::ListToolServers: method = "mcpServerStatus/list"; break;
      case Operation::SendTurn: method = "turn/start"; break;
      case Operation::InterruptTurn: method = "turn/interrupt"; break;
    }
    return CodexAppServerClient::Request(QString::fromLatin1(method), params, timeoutMs, callback);
  }

  bool AcceptedToolPolicy(const QJsonObject &result) const
  {
    if(m_Id == QStringLiteral("codex"))
      return result[QStringLiteral("sandbox")].toObject()[QStringLiteral("type")].toString() == QStringLiteral("readOnly") &&
             result[QStringLiteral("approvalPolicy")].toString() == QStringLiteral("never");
    if(m_Id != QStringLiteral("claude"))
      return false;
    // Claude enforces the available tool set and permission callbacks, not a Codex OS sandbox.
    const QJsonObject policy = result[QStringLiteral("policy")].toObject();
    return policy[QStringLiteral("toolPolicy")].toString() == QStringLiteral("rendertest-only") &&
           policy[QStringLiteral("builtinTools")].isBool() && !policy[QStringLiteral("builtinTools")].toBool() &&
           policy[QStringLiteral("externalMcp")].isBool() && !policy[QStringLiteral("externalMcp")].toBool() &&
           policy[QStringLiteral("approvals")].toString() == QStringLiteral("deny");
  }

private:
  QString m_Id = QStringLiteral("codex");
};

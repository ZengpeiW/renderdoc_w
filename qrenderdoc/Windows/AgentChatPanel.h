#pragma once

#include <QElapsedTimer>
#include <QFrame>
#include <QJsonArray>
#include <QJsonObject>
#include <QStringList>
#include <atomic>
#include <memory>
#include "Code/Agent/AgentAsk.h"
#include "Code/Agent/AgentConversation.h"
#include "Code/Agent/AgentLaunchConfig.h"
#include "Code/Agent/AgentHistory.h"
#include "Code/Interface/QRDInterface.h"

class CaptureContext;
class CodexAppServerClient;
class FlowLayout;
class QLabel;
class QPlainTextEdit;
class QPushButton;
class QTextBrowser;
class QTimer;
class QUrl;
struct AgentToolImage;

// Dockable "AI Assistant" panel. Runs `codex app-server` bound to this window's agent bridge
// instance and shows a read-only chat with streamed answers and one-line tool cards.
class AgentChatPanel : public QFrame, public ICaptureViewer
{
  Q_OBJECT

public:
  explicit AgentChatPanel(CaptureContext &ctx, QWidget *parent = 0);
  ~AgentChatPanel();

  QWidget *Widget() { return this; }

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override {}
  void OnEventChanged(uint32_t eventId) override;

  // attaches the request's context chip and prefills its question, without sending it
  void Ask(const AgentAskRequest &request);
  // handles rt://event/<eid> and rt://resource/<id> links from answers
  bool OpenLink(const QUrl &url);

  // test hook driven by RENDERTEST_AGENT_CHAT_AUTOTEST
  void RunAutotest(const QJsonObject &config);

protected:
  bool eventFilter(QObject *watched, QEvent *event) override;

private:
  enum class SessionState
  {
    Idle,
    Starting,
    Ready,
    NotLoggedIn,
    Error,
  };

  void Send();
  void SendText(const QString &text);
  void Stop();
  void NewChat();
  void ShowHistory();
  bool LoadHistory(const QString &id);
  void EnableHistoryContinuation(const QJsonObject &identity);
  void MakeHistoryReadOnly(const QString &reason);
  void EnsureHistory(const QString &title);
  void SaveHistory();
  void RefreshCaptureIdentity(bool loaded);
  void VerifyHistoryIdentity();
  bool CaptureFileUnchanged() const;
  void EditSettings();
  void Login();
  void CancelLogin();
  void FinishLogin(const QString &status, const QString &message);
  void ReadModels(int generation, const QString &cursor = QString(),
                  const QJsonArray &models = QJsonArray(),
                  const QStringList &seenCursors = QStringList());

  void AddChip(const AgentContextChip &chip);
  void UpdateChips();
  QList<AgentContextChip> ContextSnapshot() const;
  QString LinkError(const QUrl &url);
  bool ReadCaptureBinding(QString &instanceId, QString &sessionId) const;
  void CaptureChanged(const QString &message);

  void StartSession();
  void ReadAccount(int generation);
  void SetSkillRoots(int generation);
  void BeginThread(int generation);
  void StartThread(int generation, const QStringList &disabledServers);
  void PollMcpStatus(int generation, int attempt, const QString &cursor = QString(),
                     const QJsonArray &entries = QJsonArray(),
                     const QStringList &seenCursors = QStringList());
  void StartTurn();
  void SessionFailed(SessionState state, const QString &message);
  void KillSession();

  void OnNotification(const QString &method, const QJsonObject &params);
  void OnServerRequest(const QString &method, bool supported);
  void OnProcessExited(int exitCode, bool killed, const QString &reason);
  void ForceStop();
  void QuestionFinished();

  QString BuildTurnText(const QString &question, const QList<AgentContextChip> &context);
  QString LogPath() const;
  void SetState(SessionState state, const QString &message = QString());
  void ScheduleRender();
  void Render();
  QString RenderItem(const AgentItem &item, QList<AgentToolImage> *images) const;
  void UpdateControls();

  void AutotestNext();
  void AutotestAsk(int index, const QJsonObject &ask);
  void AutotestAskDone(int index, const QJsonObject &ask, bool ok, const QString &error);
  void AutotestClickLinks();
  void WriteTranscript();

  CaptureContext &m_Ctx;
  CodexAppServerClient *m_Client = NULL;
  AgentConversation m_Conversation;
  AgentLaunchConfig m_Launch;
  AgentHistory *m_History = NULL;
  AgentHistoryRecord m_HistoryRecord;
  bool m_HistoryReadOnly = false;
  bool m_HistoryLoaded = false;
  bool m_HistoryDirty = false;
  bool m_ResumeValidationPending = false;
  QString m_HistoryError;
  QString m_LastSaveError;
  QJsonObject m_LoadedIdentity;
  bool m_IdentityPending = false;
  bool m_VerifyHistoryAfterIdentity = false;
  int m_IdentityGeneration = 0;
  std::shared_ptr<std::atomic<bool>> m_IdentityCancel;

  SessionState m_State = SessionState::Idle;
  QString m_StateMessage;
  int m_Generation = 0;
  int m_TurnRequest = 0;
  bool m_ExpectExit = false;
  bool m_QuestionActive = false;
  bool m_HadThread = false;
  QString m_PendingText;
  QString m_InstanceId;
  QString m_CaptureSessionId;
  QString m_ReferenceToken;
  QString m_Account;
  QString m_AccountType;
  QJsonArray m_Models;
  QString m_LoginId;
  QString m_LoginStatus;
  bool m_LoginStarting = false;
  int m_LoginRequest = 0;
  QSet<QString> m_ExpandedTools;
  QString m_McpStatus;
  int m_McpTools = -1;
  QStringList m_OtherServers;
  QStringList m_ThreadIds;
  QJsonArray m_ServerRequests;
  QJsonArray m_TurnTexts;
  QJsonArray m_ProcessIds;

  QElapsedTimer m_StopClock;
  bool m_StopRequested = false;

  QList<AgentContextChip> m_Chips;
  bool m_IncludeEvent = true;
  QString m_Prefill;

  QLabel *m_Status = NULL;
  QPushButton *m_NewChat = NULL;
  QPushButton *m_HistoryButton = NULL;
  QLabel *m_HistoryNotice = NULL;
  QPushButton *m_Settings = NULL;
  QPushButton *m_Login = NULL;
  QTextBrowser *m_View = NULL;
  QWidget *m_ContextBar = NULL;
  FlowLayout *m_ContextLayout = NULL;
  QPlainTextEdit *m_Input = NULL;
  QPushButton *m_Send = NULL;
  QPushButton *m_StopButton = NULL;
  QTimer *m_RenderTimer = NULL;
  QTimer *m_StopTimer = NULL;
  QTimer *m_HistoryTimer = NULL;

  struct Autotest
  {
    bool active = false;
    QString loadHistoryId;
    bool historyPrepared = false;
    QJsonArray newChatBeforeQuestion;
    int newChatApplied = -1;
    QStringList questions;
    QJsonArray asks;
    QJsonArray clickLinks;
    int askApplied = -1;
    bool prepared = false;
    int index = 0;
    QString transcript;
    QString stopOnTool;
    int stopAfterMs = 0;
    int stopQuestion = 0;
    int turnTimeoutMs = 60000;
    int captureChangeAfterMs = 0;
    bool captureChangeBeforeLinks = false;
    bool autoLogin = false;
    bool loginAttempted = false;
    int cancelLoginAfterMs = 0;
    QJsonArray authUrls;
    QJsonObject settings;
    QString screenshot;
    QString settingsScreenshot;
    QString historyScreenshot;
    QJsonObject historyDialog;
    QJsonObject settingsDialog;
    bool waitForCapture = false;
    int captureTimeoutMs = 180000;
    qint64 selectEvent = -1;
    QElapsedTimer clock;
    QJsonArray results;
    QJsonArray askResults;
    QJsonArray linkClicks;
    qint64 eventBefore = -1;
    qint64 eventAfter = -1;
  } m_Autotest;
};

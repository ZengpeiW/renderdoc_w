#include "AgentChatPanel.h"
#include <QCoreApplication>
#include <QApplication>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDesktopServices>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QKeyEvent>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollBar>
#include <QTextBrowser>
#include <QTextDocument>
#include <QTreeWidget>
#include <QThread>
#include <QTimer>
#include <QToolButton>
#include <QUrl>
#include <QUrlQuery>
#include <QUuid>
#include <QVBoxLayout>
#include "Code/Agent/AgentMarkdown.h"
#include "Code/Agent/AgentCaptureIdentity.h"
#include "Code/Agent/AgentSettingsDialog.h"
#include "Code/Agent/AgentToolView.h"
#include "Code/Agent/AgentBackend.h"
#include "Code/Agent/RenderTestAgentIntegration.h"
#include "Code/CaptureContext.h"
#include "Windows/TextureViewer.h"
#include "flowlayout/FlowLayout.h"

namespace
{
// QTextBrowser must never resolve model-supplied local files or network resources. The panel
// inserts only verified artifact thumbnails into the document's resource cache.
class AgentTextBrowser : public QTextBrowser
{
public:
  explicit AgentTextBrowser(QWidget *parent) : QTextBrowser(parent) {}
protected:
  QVariant loadResource(int, const QUrl &) override { return QVariant(); }
};

// Read large captures off the GUI thread. Completion connections are canceled with the panel;
// the shared flag lets capture changes and shutdown stop the read between chunks.
class CaptureIdentityThread : public QThread
{
public:
  CaptureIdentityThread(const QString &filename, bool local, bool temporary,
                        const std::shared_ptr<std::atomic<bool>> &cancel)
      : m_Filename(filename), m_Local(local), m_Temporary(temporary), m_Cancel(cancel) {}
  QJsonObject result;
protected:
  void run() override
  {
    result = AgentCaptureIdentity::Inspect(m_Filename, m_Local, m_Temporary, m_Cancel.get());
  }
private:
  QString m_Filename;
  bool m_Local;
  bool m_Temporary;
  std::shared_ptr<std::atomic<bool>> m_Cancel;
};

const int InitializeTimeoutMs = 30000;
const int AccountTimeoutMs = 30000;
const int ConfigTimeoutMs = 15000;
const int ThreadTimeoutMs = 60000;
const int TurnTimeoutMs = 60000;
const int StopGraceMs = 2000;
const int McpPollIntervalMs = 500;
const int McpPollAttempts = 60;
const int RenderIntervalMs = 50;

QString Glyph(uint codepoint)
{
  return QString(QChar(ushort(codepoint)));
}

QString LastLines(const QString &text, int count)
{
  QStringList lines = text.trimmed().split(QLatin1Char('\n'));
  if(lines.size() > count)
    lines = lines.mid(lines.size() - count);
  return lines.join(QLatin1Char('\n'));
}

QString ErrorMessage(const QJsonObject &error)
{
  QString message = error[lit("message")].toString();
  return message.isEmpty() ? lit("unknown error") : message;
}

QJsonObject AgentSettingsSnapshot(const PersistentConfig &config)
{
  QJsonObject result;
  result[lit("backend")] = QString(config.Agent_Backend);
  result[lit("claudePath")] = QString(config.Agent_ClaudePath);
  result[lit("claudeModel")] = QString(config.Agent_ClaudeModel);
  result[lit("claudeReasoningEffort")] = QString(config.Agent_ClaudeReasoningEffort);
  result[lit("codexPath")] = QString(config.Agent_CodexPath);
  result[lit("mcpPython")] = QString(config.Agent_McpPython);
  result[lit("model")] = QString(config.Agent_Model);
  result[lit("reasoningEffort")] = QString(config.Agent_ReasoningEffort);
  result[lit("isolate")] = config.Agent_IsolateMcpServers;
  return result;
}

void ApplyAgentSettings(PersistentConfig &config, const QJsonObject &settings)
{
  if(settings.contains(lit("backend")))
    config.Agent_Backend = settings[lit("backend")].toString();
  if(settings.contains(lit("claudePath")))
    config.Agent_ClaudePath = settings[lit("claudePath")].toString();
  if(settings.contains(lit("claudeModel")))
    config.Agent_ClaudeModel = settings[lit("claudeModel")].toString();
  if(settings.contains(lit("claudeReasoningEffort")))
    config.Agent_ClaudeReasoningEffort = settings[lit("claudeReasoningEffort")].toString();
  if(settings.contains(lit("codexPath")))
    config.Agent_CodexPath = settings[lit("codexPath")].toString();
  if(settings.contains(lit("mcpPython")))
    config.Agent_McpPython = settings[lit("mcpPython")].toString();
  if(settings.contains(lit("model")))
    config.Agent_Model = settings[lit("model")].toString();
  if(settings.contains(lit("reasoningEffort")))
    config.Agent_ReasoningEffort = settings[lit("reasoningEffort")].toString();
  if(settings.contains(lit("isolate")))
    config.Agent_IsolateMcpServers = settings[lit("isolate")].toBool(true);
}

QString Elide(const QString &text, int maxChars)
{
  if(text.size() <= maxChars)
    return text;
  return text.left(maxChars - 1) + Glyph(0x2026);
}

QStringList RenderedLinks(const QList<AgentItem> &items)
{
  static const QRegularExpression href(lit("href=\"(rt://[^\"]+)\""));
  QStringList links;
  for(const AgentItem &item : items)
  {
    if(item.kind != AgentItem::Kind::Agent)
      continue;
    QRegularExpressionMatchIterator it =
        href.globalMatch(AgentMarkdown::ToHtml(item.text, item.referenceToken));
    while(it.hasNext())
    {
      QString link = it.next().captured(1);
      if(!links.contains(link))
        links << link;
    }
  }
  return links;
}
}

AgentChatPanel::AgentChatPanel(CaptureContext &ctx, QWidget *parent) : QFrame(parent), m_Ctx(ctx)
{
  m_ReferenceToken = QUuid::createUuid().toString().mid(1, 36);
  setWindowTitle(tr("AI Assistant"));

  m_Status = new QLabel(this);
  m_Status->setTextFormat(Qt::RichText);
  m_Status->setWordWrap(true);

  m_NewChat = new QPushButton(tr("New Chat"), this);
  m_NewChat->setToolTip(tr("Save this conversation and start a new one"));
  m_History = new AgentHistory(RenderTestAgentIntegration::AgentRoot());
  m_HistoryButton = new QPushButton(tr("History"), this);
  m_HistoryNotice = new QLabel(this);
  m_HistoryNotice->setWordWrap(true);
  m_HistoryNotice->setTextFormat(Qt::PlainText);
  m_HistoryNotice->hide();

  m_Settings = new QPushButton(tr("Settings"), this);
  m_Login = new QPushButton(tr("Log in"), this);
  m_Login->setVisible(false);

  m_View = new AgentTextBrowser(this);
  m_View->setOpenLinks(false);
  m_View->setOpenExternalLinks(false);

  m_ContextBar = new QWidget(this);
  m_ContextLayout = new FlowLayout(m_ContextBar, 0, 3, 3);

  m_Input = new QPlainTextEdit(this);
  m_Input->setPlaceholderText(tr("Ask about this capture. Enter sends, Shift+Enter adds a line."));
  m_Input->setMaximumHeight(90);
  m_Input->installEventFilter(this);

  m_Send = new QPushButton(tr("Send"), this);
  m_StopButton = new QPushButton(tr("Stop"), this);

  QHBoxLayout *top = new QHBoxLayout();
  top->addWidget(m_Status, 1);
  top->addWidget(m_Login);
  top->addWidget(m_HistoryButton);
  top->addWidget(m_NewChat);
  top->addWidget(m_Settings);

  QVBoxLayout *buttons = new QVBoxLayout();
  buttons->addWidget(m_Send);
  buttons->addWidget(m_StopButton);
  buttons->addStretch(1);

  QHBoxLayout *bottom = new QHBoxLayout();
  bottom->addWidget(m_Input, 1);
  bottom->addLayout(buttons);

  QVBoxLayout *layout = new QVBoxLayout(this);
  layout->setContentsMargins(3, 3, 3, 3);
  layout->addLayout(top);
  layout->addWidget(m_HistoryNotice);
  layout->addWidget(m_View, 1);
  layout->addWidget(m_ContextBar);
  layout->addLayout(bottom);

  m_RenderTimer = new QTimer(this);
  m_RenderTimer->setSingleShot(true);
  m_RenderTimer->setInterval(RenderIntervalMs);
  connect(m_RenderTimer, &QTimer::timeout, this, &AgentChatPanel::Render);

  m_StopTimer = new QTimer(this);
  m_StopTimer->setSingleShot(true);
  m_StopTimer->setInterval(StopGraceMs);
  connect(m_StopTimer, &QTimer::timeout, this, &AgentChatPanel::ForceStop);

  m_HistoryTimer = new QTimer(this);
  m_HistoryTimer->setSingleShot(true);
  m_HistoryTimer->setInterval(2000);
  connect(m_HistoryTimer, &QTimer::timeout, this, &AgentChatPanel::SaveHistory);

  m_Client = new AgentBackend(this);
  connect(m_Client, &AgentBackend::serverRequestAnswered, this,
          &AgentChatPanel::OnServerRequest);
  connect(m_Client, &AgentBackend::processExited, this, &AgentChatPanel::OnProcessExited);

  connect(m_Send, &QPushButton::clicked, this, &AgentChatPanel::Send);
  connect(m_StopButton, &QPushButton::clicked, this, &AgentChatPanel::Stop);
  connect(m_NewChat, &QPushButton::clicked, this, &AgentChatPanel::NewChat);
  connect(m_HistoryButton, &QPushButton::clicked, this, &AgentChatPanel::ShowHistory);
  connect(m_Settings, &QPushButton::clicked, this, &AgentChatPanel::EditSettings);
  connect(m_Login, &QPushButton::clicked, this, &AgentChatPanel::Login);
  connect(m_View, &QTextBrowser::anchorClicked, this, &AgentChatPanel::OpenLink);

  m_Ctx.AddCaptureViewer(this);
  if(m_LoadedIdentity.isEmpty() && !m_IdentityPending)
    RefreshCaptureIdentity(m_Ctx.IsCaptureLoaded());

  Render();
  UpdateChips();
  UpdateControls();
}

AgentChatPanel::~AgentChatPanel()
{
  if(m_IdentityCancel)
    *m_IdentityCancel = true;
  if(m_Conversation.TurnRunning())
  {
    m_Conversation.EndTurn(lit("interrupted"), tr("RenderTest closed during this turn."));
    m_HistoryDirty = true;
  }
  SaveHistory();
  m_Client->disconnect(this);
  m_Client->Kill();
  delete m_History;

  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
}

void AgentChatPanel::OnCaptureLoaded()
{
  CaptureChanged(tr("A capture was loaded. The next question starts a new conversation."));
  RefreshCaptureIdentity(true);
  m_Chips.clear();
  m_IncludeEvent = true;
  UpdateChips();
  UpdateControls();
}

void AgentChatPanel::OnCaptureClosed()
{
  CaptureChanged(tr("The capture was closed. Its conversation and links are no longer active."));
  RefreshCaptureIdentity(false);
  m_Chips.clear();
  UpdateChips();
  UpdateControls();
}

void AgentChatPanel::CaptureChanged(const QString &message)
{
  m_ReferenceToken = QUuid::createUuid().toString().mid(1, 36);
  // Invalidate callbacks before terminating the old process. Its MCP binding must never migrate.
  KillSession();
  m_PendingText.clear();
  m_CaptureSessionId.clear();
  if(m_Conversation.TurnRunning())
    m_Conversation.EndTurn(lit("interrupted"), message);
  else if(!m_Conversation.Items().isEmpty())
    m_Conversation.AddNotice(lit("info"), message);
  m_HistoryDirty = true;
  SaveHistory();
  if(!m_HistoryRecord.metadata.id.isEmpty())
    MakeHistoryReadOnly(tr("Capture changed. This conversation is read-only. Open it from History "
                           "with the matching capture, or start a new chat."));
  QuestionFinished();
  ScheduleRender();
  UpdateControls();
}

void AgentChatPanel::RefreshCaptureIdentity(bool loaded)
{
  if(m_IdentityCancel)
    *m_IdentityCancel = true;
  const int generation = ++m_IdentityGeneration;
  m_VerifyHistoryAfterIdentity = false;
  m_LoadedIdentity = QJsonObject();
  m_IdentityPending = loaded;
  if(!m_IdentityPending)
  {
    m_LoadedIdentity = AgentCaptureIdentity::NoCapture();
    return;
  }
  m_IdentityCancel = std::make_shared<std::atomic<bool>>(false);
  CaptureIdentityThread *worker = new CaptureIdentityThread(
      QString(m_Ctx.GetCaptureFilename()), m_Ctx.IsCaptureLocal(), m_Ctx.IsCaptureTemporary(),
      m_IdentityCancel);
  connect(worker, &QThread::finished, this, [this, worker, generation]() {
    if(generation != m_IdentityGeneration)
      return;
    m_LoadedIdentity = worker->result;
    m_IdentityPending = false;
    if(m_VerifyHistoryAfterIdentity && m_HistoryLoaded && !m_HistoryRecord.metadata.id.isEmpty())
      VerifyHistoryIdentity();
    UpdateControls();
    if(m_Autotest.active)
      QTimer::singleShot(0, this, &AgentChatPanel::AutotestNext);
  });
  connect(worker, &QThread::finished, worker, &QObject::deleteLater);
  worker->start();
}

bool AgentChatPanel::CaptureFileUnchanged() const
{
  if(m_LoadedIdentity[lit("kind")].toString() == lit("none"))
    return !m_Ctx.IsCaptureLoaded();
  if(!m_LoadedIdentity[lit("verified")].toBool())
    return true;    // Unverifiable captures can chat now, but can never resume saved history.
  return m_Ctx.IsCaptureLoaded() && !m_Ctx.IsCaptureLoading() &&
         AgentCaptureIdentity::FileUnchanged(m_LoadedIdentity, QString(m_Ctx.GetCaptureFilename()));
}

QString AgentChatPanel::ConfiguredBackend() const
{
  PersistentConfig settings;
  ApplyAgentSettings(settings, AgentSettingsSnapshot(m_Ctx.Config()));
  if(m_Autotest.active)
    ApplyAgentSettings(settings, m_Autotest.settings);
  return AgentLaunchConfig::EffectiveBackend(settings);
}

void AgentChatPanel::EnsureHistory(const QString &title)
{
  if(!m_HistoryRecord.metadata.id.isEmpty())
    return;
  m_HistoryRecord = AgentHistoryRecord();
  m_HistoryRecord.backend = ConfiguredBackend();
  m_HistoryRecord.metadata.id = AgentHistory::NewId();
  m_HistoryRecord.metadata.title = title.simplified().left(120);
  m_HistoryRecord.metadata.mode = m_Ctx.IsCaptureLoaded() ? lit("capture") : lit("chat");
  m_HistoryRecord.metadata.captureIdentity = m_LoadedIdentity;
  QString error;
  if(!m_History->Acquire(m_HistoryRecord.metadata.id, error))
  {
    m_LastSaveError = error;
    m_Conversation.AddNotice(lit("error"), tr("Could not save conversation history: %1").arg(error));
  }
}

void AgentChatPanel::SaveHistory()
{
  if(!m_History || m_HistoryReadOnly || m_ResumeValidationPending || !m_HistoryDirty ||
     m_HistoryRecord.metadata.id.isEmpty() || m_Conversation.Items().isEmpty())
    return;
  m_HistoryTimer->stop();
  m_HistoryRecord.conversation = m_Conversation.ToJson();
  // Killing an app-server clears the live binding, but must not erase its durable thread id.
  m_HistoryRecord.conversation[lit("threadId")] = m_HistoryRecord.metadata.threadId;
  m_HistoryRecord.metadata.model = m_Conversation.Model();
  QString error;
  // Never replace a recoverable snapshot with content our own reader would reject.
  AgentConversation validation;
  if(!validation.Load(m_HistoryRecord.conversation, error) || !m_History->Save(m_HistoryRecord, error))
  {
    if(error != m_LastSaveError)
    {
      m_LastSaveError = error;
      m_Conversation.AddNotice(lit("error"), tr("Could not save conversation history: %1").arg(error));
      ScheduleRender();
    }
    m_HistoryDirty = true;
  }
  else
  {
    m_LastSaveError.clear();
    m_HistoryDirty = false;
  }
}

void AgentChatPanel::MakeHistoryReadOnly(const QString &reason)
{
  m_HistoryReadOnly = true;
  m_HistoryError = reason;
  m_HistoryDirty = false;
  m_HistoryTimer->stop();
  m_History->Release();
  UpdateControls();
}

void AgentChatPanel::ShowHistory()
{
  if(m_Conversation.TurnRunning() || m_State == SessionState::Starting || m_LoginStarting ||
     !m_LoginId.isEmpty())
    return;
  SaveHistory();
  QString warning;
  const QList<AgentHistoryEntry> entries = m_History->List(warning);
  if(!warning.isEmpty())
  {
    m_Conversation.AddNotice(lit("warning"), warning);
    ScheduleRender();
  }
  const QString id = AgentHistory::Select(entries, this);
  if(!id.isEmpty())
    LoadHistory(id);
}

bool AgentChatPanel::LoadHistory(const QString &id)
{
  AgentHistoryRecord record;
  AgentConversation conversation;
  QString error;
  if(!m_History->Load(id, record, error) || !conversation.Load(record.conversation, error))
  {
    m_HistoryError = error;
    m_Conversation.AddNotice(lit("error"), tr("Could not open conversation history: %1").arg(error));
    ScheduleRender();
    return false;
  }
  SaveHistory();
  if(!m_LastSaveError.isEmpty())
    return false;
  KillSession();
  m_History->Release();
  m_PendingText.clear();
  m_Chips.clear();
  m_Input->clear();
  m_ExpandedTools.clear();
  AgentToolView::ClearCache();
  m_ReferenceToken = AgentHistory::NewId();
  m_Conversation = conversation;
  m_HistoryRecord = record;
  m_HistoryLoaded = true;
  m_LastSaveError.clear();
  m_ResumeValidationPending = false;
  m_HadThread = !record.metadata.threadId.isEmpty();
  MakeHistoryReadOnly(tr("Checking the capture before allowing this conversation to continue..."));
  ReadCaptureBinding(m_InstanceId, m_CaptureSessionId);
  VerifyHistoryIdentity();
  UpdateChips();
  ScheduleRender();
  return true;
}

void AgentChatPanel::VerifyHistoryIdentity()
{
  if(m_HistoryRecord.backend != ConfiguredBackend())
  {
    MakeHistoryReadOnly(tr("This conversation belongs to %1. Select that backend in Settings and reopen it from History to continue.")
                            .arg(m_HistoryRecord.backend));
    return;
  }
  if(m_IdentityPending)
  {
    m_VerifyHistoryAfterIdentity = true;
    return;
  }
  m_VerifyHistoryAfterIdentity = false;
  QString reason;
  if(!AgentCaptureIdentity::Matches(m_HistoryRecord.metadata.captureIdentity, m_LoadedIdentity, &reason))
  {
    MakeHistoryReadOnly(reason);
    return;
  }
  if(m_LoadedIdentity[lit("kind")].toString() == lit("none"))
  {
    EnableHistoryContinuation(m_LoadedIdentity);
    return;
  }
  // A second full read checks that the on-disk capture still matches the one loaded for replay.
  const int generation = ++m_IdentityGeneration;
  const QString id = m_HistoryRecord.metadata.id;
  if(m_IdentityCancel)
    *m_IdentityCancel = true;
  m_IdentityCancel = std::make_shared<std::atomic<bool>>(false);
  m_IdentityPending = true;
  CaptureIdentityThread *worker = new CaptureIdentityThread(
      QString(m_Ctx.GetCaptureFilename()), m_Ctx.IsCaptureLocal(), m_Ctx.IsCaptureTemporary(),
      m_IdentityCancel);
  connect(worker, &QThread::finished, this, [this, worker, generation, id]() {
    if(generation != m_IdentityGeneration)
      return;
    m_IdentityPending = false;
    if(m_VerifyHistoryAfterIdentity && m_HistoryLoaded && !m_HistoryRecord.metadata.id.isEmpty())
      VerifyHistoryIdentity();
    else if(m_HistoryRecord.metadata.id == id)
      EnableHistoryContinuation(worker->result);
    UpdateControls();
    if(m_Autotest.active)
      QTimer::singleShot(0, this, &AgentChatPanel::AutotestNext);
  });
  connect(worker, &QThread::finished, worker, &QObject::deleteLater);
  worker->start();
  UpdateControls();
}

void AgentChatPanel::EnableHistoryContinuation(const QJsonObject &identity)
{
  if(m_HistoryRecord.backend != ConfiguredBackend())
  {
    MakeHistoryReadOnly(tr("The selected backend does not match this saved conversation. Reopen it after selecting %1 in Settings.")
                            .arg(m_HistoryRecord.backend));
    return;
  }
  QString error;
  if(!AgentCaptureIdentity::Matches(m_LoadedIdentity, identity, &error) ||
     !AgentCaptureIdentity::Matches(m_HistoryRecord.metadata.captureIdentity, identity, &error))
  {
    MakeHistoryReadOnly(error);
    return;
  }
  if(m_HistoryRecord.metadata.threadId.isEmpty())
  {
    MakeHistoryReadOnly(tr("This saved conversation has no backend session to resume. Start a new chat."));
    return;
  }
  if(!m_History->Acquire(m_HistoryRecord.metadata.id, error))
  {
    MakeHistoryReadOnly(error);
    return;
  }
  // Another window may have updated the snapshot before we acquired its lock.
  AgentHistoryRecord latest;
  AgentConversation conversation;
  if(!m_History->Load(m_HistoryRecord.metadata.id, latest, error) ||
     !conversation.Load(latest.conversation, error) ||
     !AgentCaptureIdentity::Matches(latest.metadata.captureIdentity, identity, &error))
  {
    MakeHistoryReadOnly(error);
    return;
  }
  m_HistoryRecord = latest;
  if(latest.backend != ConfiguredBackend())
  {
    MakeHistoryReadOnly(tr("The saved conversation belongs to a different backend."));
    return;
  }
  m_Conversation = conversation;
  m_HistoryReadOnly = false;
  m_HistoryError.clear();
  m_HistoryDirty = false;
  ScheduleRender();
  UpdateControls();
}

bool AgentChatPanel::ReadCaptureBinding(QString &instanceId, QString &sessionId) const
{
  instanceId.clear();
  sessionId.clear();
  RenderTestAgentIntegration *integration = m_Ctx.AgentIntegration();
  if(!integration || !integration->IsRunning())
    return false;
  QJsonObject manifest = integration->ReadManifest();
  const QString instance = integration->InstanceId();
  if(instance.isEmpty() || manifest[lit("instanceId")].toString() != instance ||
     manifest[lit("pid")].toDouble(-1) != double(QCoreApplication::applicationPid()) ||
     manifest[lit("stopped")].toBool(true))
    return false;
  instanceId = instance;
  const qint64 age = QDateTime::currentMSecsSinceEpoch() -
                     qint64(manifest[lit("heartbeatUnixMs")].toDouble());
  if(age < -5000 || age > 15000 || !m_Ctx.IsCaptureLoaded() || m_Ctx.IsCaptureLoading() ||
     !manifest[lit("captureLoaded")].toBool(false))
    return false;
  QString bridgePath = QDir::cleanPath(QDir::fromNativeSeparators(manifest[lit("capturePath")].toString()));
  QString uiPath = QDir::cleanPath(QDir::fromNativeSeparators(QString(m_Ctx.GetCaptureFilename())));
#if defined(Q_OS_WIN32)
  if(bridgePath.compare(uiPath, Qt::CaseInsensitive) != 0)
#else
  if(bridgePath != uiPath)
#endif
    return false;
  sessionId = manifest[lit("captureSessionId")].toString();
  return !sessionId.isEmpty();
}

void AgentChatPanel::OnEventChanged(uint32_t eventId)
{
  Q_UNUSED(eventId);
  UpdateChips();
}

void AgentChatPanel::Ask(const AgentAskRequest &request)
{
  AddChip(request.chip);

  QString current = m_Input->toPlainText().trimmed();
  if(!request.question.isEmpty() && (current.isEmpty() || current == m_Prefill))
  {
    m_Prefill = request.question;
    m_Input->setPlainText(request.question);
    m_Input->selectAll();
  }
  m_Input->setFocus(Qt::OtherFocusReason);
}

void AgentChatPanel::AddChip(const AgentContextChip &chip)
{
  for(int i = 0; i < m_Chips.size(); i++)
  {
    if(m_Chips[i].selection == chip.selection)
      m_Chips.removeAt(i--);
  }
  m_Chips << chip;
  UpdateChips();
}

QList<AgentContextChip> AgentChatPanel::ContextSnapshot() const
{
  QList<AgentContextChip> context;
  if(m_IncludeEvent && m_Ctx.IsCaptureLoaded())
  {
    AgentContextChip current;
    current.kind = lit("currentEvent");
    current.label = tr("Current EID %1").arg(m_Ctx.CurEvent());
    current.selection[lit("kind")] = current.kind;
    current.selection[lit("event_id")] = double(m_Ctx.CurEvent());
    current.selection[lit("selected_event_id")] = double(m_Ctx.CurSelectedEvent());
    context << current;
  }
  context << m_Chips;
  return context;
}

void AgentChatPanel::UpdateChips()
{
  while(QLayoutItem *item = m_ContextLayout->takeAt(0))
  {
    delete item->widget();
    delete item;
  }

  const QString close = Glyph(0x00D7);
  auto addButton = [this](const QString &text, const QString &tooltip, bool chip) {
    QToolButton *button = new QToolButton(m_ContextBar);
    button->setText(text);
    button->setToolTip(tooltip);
    button->setAutoRaise(!chip);
    if(chip)
      button->setStyleSheet(
          lit("QToolButton { border: 1px solid palette(mid); border-radius: 8px; padding: 1px 6px; "
              "background-color: rgba(58,95,138,40); }"));
    m_ContextLayout->addWidget(button);
    return button;
  };

  if(m_Ctx.IsCaptureLoaded())
  {
    if(m_IncludeEvent)
    {
      QToolButton *button = addButton(tr("Current EID %1 %2").arg(m_Ctx.CurEvent()).arg(close),
                                      tr("The current event is sent with the question. Click to "
                                         "remove it."),
                                      true);
      connect(button, &QToolButton::clicked, this, [this]() {
        m_IncludeEvent = false;
        QTimer::singleShot(0, this, &AgentChatPanel::UpdateChips);
      });
    }
    else
    {
      QToolButton *button =
          addButton(tr("+ Current EID"), tr("Send the current event with the question"), false);
      connect(button, &QToolButton::clicked, this, [this]() {
        m_IncludeEvent = true;
        QTimer::singleShot(0, this, &AgentChatPanel::UpdateChips);
      });
    }
  }

  for(int i = 0; i < m_Chips.size(); i++)
  {
    const AgentContextChip &chip = m_Chips[i];
    QString tooltip = QString::fromUtf8(QJsonDocument(chip.selection).toJson(QJsonDocument::Compact));
    QToolButton *button =
        addButton(lit("%1 %2").arg(Elide(chip.label, 48)).arg(close), tooltip, true);
    connect(button, &QToolButton::clicked, this, [this, i]() {
      if(i < m_Chips.size())
        m_Chips.removeAt(i);
      QTimer::singleShot(0, this, &AgentChatPanel::UpdateChips);
    });
  }

  m_ContextBar->setVisible(m_ContextLayout->count() > 0);
}

QString AgentChatPanel::LinkError(const QUrl &url)
{
  if(url.scheme() != lit("rt"))
  {
    if(url.scheme() == lit("http") || url.scheme() == lit("https"))
      return QDesktopServices::openUrl(url) ? QString() : tr("Could not open %1").arg(url.toString());
    return tr("Unsupported link %1").arg(url.toString());
  }

  bool ok = false;
  const qulonglong number = url.path().mid(1).toULongLong(&ok);
  if(!ok)
    return tr("Malformed link %1").arg(url.toString());
  if(!m_Ctx.IsCaptureLoaded())
    return tr("No capture is loaded.");
  const QString source = QUrlQuery(url).queryItemValue(lit("source"));
  if(source.isEmpty() || source != m_ReferenceToken)
    return tr("This link belongs to an earlier capture or has no capture identity.");
  QString instance, session;
  if(!ReadCaptureBinding(instance, session) || instance != m_InstanceId ||
     session != m_CaptureSessionId)
    return tr("The capture identity changed; this link cannot be opened.");

  if(url.host() == lit("event"))
  {
    const ActionDescription *last = m_Ctx.GetLastAction();
    if(number == 0 || (last && number > last->eventId))
      return tr("EID %1 is not in this capture.").arg(number);
    m_Ctx.SetEventID({}, uint32_t(number), uint32_t(number));
    return QString();
  }

  if(url.host() == lit("resource"))
  {
    ResourceId id = AgentAsk::ResourceIdFromNumber(number);
    if(m_Ctx.GetTexture(id))
    {
      m_Ctx.ShowTextureViewer();
      m_Ctx.GetTextureViewer()->ViewTexture(id, CompType::Typeless, true);
    }
    else if(m_Ctx.GetBuffer(id))
    {
      IBufferViewer *viewer = m_Ctx.ViewBuffer(0, ~0ULL, id);
      m_Ctx.AddDockWindow(viewer->Widget(), DockReference::MainToolArea, NULL);
    }
    else if(m_Ctx.GetResource(id))
    {
      m_Ctx.ShowResourceInspector();
      m_Ctx.GetResourceInspector()->Inspect(id);
    }
    else
    {
      return tr("ResourceId::%1 is not in this capture.").arg(number);
    }
    return QString();
  }

  return tr("Unsupported link %1").arg(url.toString());
}

bool AgentChatPanel::OpenLink(const QUrl &url)
{
  if(url.scheme() == lit("agent-tool") || url.scheme() == lit("agent-artifact"))
  {
    const QStringList parts = url.path().split(QLatin1Char('/'), QString::SkipEmptyParts);
    if(url.host() == lit("item") && !parts.isEmpty())
    {
      for(const AgentItem &item : m_Conversation.Items())
      {
        if(item.kind != AgentItem::Kind::Tool || AgentToolView::ItemKey(item) != parts[0])
          continue;
        if(url.scheme() == lit("agent-tool") && parts.size() == 1)
        {
          if(m_ExpandedTools.contains(parts[0]))
            m_ExpandedTools.remove(parts[0]);
          else
            m_ExpandedTools.insert(parts[0]);
          Render();
          return true;
        }
        bool ok = false;
        int index = parts.size() == 2 ? parts[1].toInt(&ok) : -1;
        if(url.scheme() == lit("agent-artifact") && ok)
        {
          AgentToolImage image = AgentToolView::ValidatedImage(
              item, index, RenderTestAgentIntegration::AgentRoot(), true);
          if(image.IsValid())
          {
            // Tests validate the same file without launching an external viewer.
            if(m_Autotest.active || QDesktopServices::openUrl(QUrl::fromLocalFile(image.canonicalPath)))
              return true;
          }
          m_Conversation.AddNotice(lit("warning"), image.error.isEmpty()
                                      ? tr("Could not open the artifact image.") : image.error);
          ScheduleRender();
          return false;
        }
      }
    }
    return false;
  }
  QString error = LinkError(url);
  if(error.isEmpty())
    return true;
  m_Conversation.AddNotice(lit("warning"), error);
  ScheduleRender();
  return false;
}

bool AgentChatPanel::eventFilter(QObject *watched, QEvent *event)
{
  if(watched == m_Input && event->type() == QEvent::KeyPress)
  {
    QKeyEvent *key = static_cast<QKeyEvent *>(event);
    if((key->key() == Qt::Key_Return || key->key() == Qt::Key_Enter) &&
       !(key->modifiers() & Qt::ShiftModifier))
    {
      Send();
      return true;
    }
  }
  return QFrame::eventFilter(watched, event);
}

void AgentChatPanel::Send()
{
  QString text = m_Input->toPlainText().trimmed();
  if(!m_Send->isEnabled() || text.isEmpty() || m_Conversation.TurnRunning() || m_LoginStarting || !m_LoginId.isEmpty() ||
     m_HistoryReadOnly || m_IdentityPending)
    return;
  m_Input->clear();
  SendText(text);
}

void AgentChatPanel::SendText(const QString &text)
{
  if(m_HistoryReadOnly || m_IdentityPending || m_Ctx.IsCaptureLoading() ||
     (m_Ctx.IsCaptureLoaded() && m_LoadedIdentity[lit("kind")].toString() == lit("none")))
    return;
  if(!CaptureFileUnchanged())
  {
    SaveHistory();
    MakeHistoryReadOnly(tr("The capture file changed on disk. Reopen it and start a new chat."));
    return;
  }
  QString instance, session;
  ReadCaptureBinding(instance, session);
  if(m_InstanceId != instance || m_CaptureSessionId != session)
  {
    if(!m_HistoryRecord.metadata.id.isEmpty())
      CaptureChanged(tr("The capture binding changed. Open History or start a new conversation."));
    m_InstanceId = instance;
    m_CaptureSessionId = session;
  }
  if(m_HistoryReadOnly)
    return;
  if(ConfiguredBackend().isEmpty())
  {
    SessionFailed(SessionState::Error, tr("Select a supported AI backend in Settings."));
    return;
  }
  if(!m_HistoryRecord.metadata.id.isEmpty() && m_HistoryRecord.backend != ConfiguredBackend())
  {
    MakeHistoryReadOnly(tr("The backend changed. Start a new chat or reopen this conversation with its original backend."));
    return;
  }
  EnsureHistory(text);
  m_ResumeValidationPending = !m_HistoryRecord.metadata.threadId.isEmpty() &&
                              (m_State != SessionState::Ready || !m_Client->IsRunning() ||
                               m_Conversation.ThreadId().isEmpty());
  QList<AgentContextChip> context = ContextSnapshot();
  QJsonArray contextJson;
  for(const AgentContextChip &chip : context)
    contextJson << chip.ToJson();

  m_Conversation.AddUserMessage(text, contextJson);
  m_PendingText = BuildTurnText(text, context);
  m_Chips.clear();
  m_Prefill.clear();
  UpdateChips();
  m_Conversation.BeginTurn(m_ReferenceToken);
  m_QuestionActive = true;
  m_StopRequested = false;
  m_HistoryDirty = true;
  SaveHistory();

  if(m_State == SessionState::Ready && m_Client->IsRunning())
  {
    if(m_Conversation.ThreadId().isEmpty())
      BeginThread(m_Generation);
    else
      StartTurn();
  }
  else if(m_State == SessionState::NotLoggedIn && m_Client->IsRunning())
  {
    if(m_Client->SupportsBrowserLogin())
    {
      // Codex owns its browser login. Claude authenticates externally and reconnects below.
      SetState(SessionState::Starting, tr("Checking account..."));
      ReadAccount(m_Generation);
    }
    else
    {
      KillSession();
      StartSession();
    }
  }
  else if(m_State != SessionState::Starting)
  {
    StartSession();
  }

  ScheduleRender();
  UpdateControls();
}

QString AgentChatPanel::BuildTurnText(const QString &question, const QList<AgentContextChip> &context)
{
  const bool loaded = m_Ctx.IsCaptureLoaded();

  QStringList lines;
  lines << lit("<rendertest_context>");
  lines << lit("instanceId: %1").arg(m_InstanceId.isEmpty() ? lit("unavailable") : m_InstanceId);
  lines << lit("captureLoaded: %1").arg(loaded ? lit("true") : lit("false"));
  lines << lit("captureToolsAvailable: %1").arg(m_CaptureSessionId.isEmpty() ? lit("false") : lit("true"));
  if(loaded)
  {
    if(!m_CaptureSessionId.isEmpty())
      lines << lit("captureSessionId: %1").arg(m_CaptureSessionId);
    lines << lit("capture: %1").arg(QDir::fromNativeSeparators(QString(m_Ctx.GetCaptureFilename())));
  }
  for(const AgentContextChip &chip : context)
  {
    if(chip.kind == lit("currentEvent"))
    {
      lines << lit("currentEventId: %1").arg(chip.selection[lit("event_id")].toInt());
      lines << lit("selectedEventId: %1").arg(chip.selection[lit("selected_event_id")].toInt());
    }
    else
    {
      lines << lit("selection: %1")
                   .arg(QString::fromUtf8(
                       QJsonDocument(chip.selection).toJson(QJsonDocument::Compact)));
    }
  }
  lines << lit("</rendertest_context>");
  lines << QString();
  lines << question;
  return lines.join(QLatin1Char('\n'));
}

QString AgentChatPanel::LogPath() const
{
  QString name =
      m_InstanceId.isEmpty() ? QString::number(QCoreApplication::applicationPid()) : m_InstanceId;
  return QDir(RenderTestAgentIntegration::AgentRoot())
      .absoluteFilePath(lit("logs/chat-%1.log").arg(name));
}

void AgentChatPanel::SetState(SessionState state, const QString &message)
{
  m_State = state;
  m_StateMessage = message;
  UpdateControls();
}

void AgentChatPanel::StartSession()
{
  const int generation = ++m_Generation;

  if(m_CaptureSessionId.isEmpty())
  {
    m_Conversation.AddNotice(
        lit("warning"),
        tr("No capture is safely bound to this conversation. Capture tools are disabled; "
           "plain chat is available."));
  }

  // Test overrides must never enter the shared persistent configuration, even on early shutdown.
  PersistentConfig settings;
  ApplyAgentSettings(settings, AgentSettingsSnapshot(m_Ctx.Config()));
  if(m_Autotest.active)
    ApplyAgentSettings(settings, m_Autotest.settings);
  m_Launch = AgentLaunchConfig::Resolve(m_InstanceId, m_CaptureSessionId,
                                      RenderTestAgentIntegration::AgentRoot(), settings);
  if(!m_Launch.error.isEmpty())
  {
    SessionFailed(SessionState::Error, m_Launch.error);
    return;
  }

  if(!m_HistoryRecord.metadata.id.isEmpty() && m_HistoryRecord.backend != m_Launch.backend)
  {
    const QString message = tr("Refusing to resume a conversation with a different backend.");
    MakeHistoryReadOnly(message);
    SessionFailed(SessionState::Error, message);
    return;
  }

  m_Account.clear();
  m_AccountType.clear();
  m_McpStatus.clear();
  m_McpTools = -1;
  m_OtherServers.clear();

  m_Client->SetLogPath(LogPath());
  disconnect(m_Client, &AgentBackend::notificationReceived, this, nullptr);
  connect(m_Client, &AgentBackend::notificationReceived, this,
          [this, generation](const QString &method, const QJsonObject &params) {
            if(generation == m_Generation)
              OnNotification(method, params);
          });
  QString error;
  if(!m_Client->Start(m_Launch, error))
  {
    SessionFailed(SessionState::Error, error);
    return;
  }
  m_ProcessIds << double(m_Client->ProcessId());
  SetState(SessionState::Starting, tr("Starting %1...").arg(m_Client->DisplayName()));

  QJsonObject clientInfo;
  clientInfo[lit("name")] = lit("qrendertest");
  clientInfo[lit("title")] = lit("RenderTest");
  clientInfo[lit("version")] = QCoreApplication::applicationVersion();
  QJsonObject capabilities;
  capabilities[lit("experimentalApi")] = true;
  QJsonObject params;
  params[lit("clientInfo")] = clientInfo;
  params[lit("capabilities")] = capabilities;

  m_Client->Request(AgentBackend::Operation::Initialize, params, InitializeTimeoutMs,
                    [this, generation](const QJsonValue &, const QJsonObject &error) {
                      if(generation != m_Generation)
                        return;
                      if(!error.isEmpty())
                      {
                        SessionFailed(
                            SessionState::Error,
                            tr("%1 did not initialize: %2").arg(m_Client->DisplayName(), ErrorMessage(error)));
                        return;
                      }
                      m_Client->Initialized();
                      ReadModels(generation);
                      ReadAccount(generation);
                    });
}

void AgentChatPanel::ReadAccount(int generation)
{
  QJsonObject params;
  params[lit("refreshToken")] = false;
  m_Client->Request(
      AgentBackend::Operation::ReadAccount, params, AccountTimeoutMs,
      [this, generation](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;
        if(!error.isEmpty())
        {
          SessionFailed(SessionState::Error,
                        tr("Could not read the %1 account: %2").arg(m_Client->DisplayName(), ErrorMessage(error)));
          return;
        }
        QJsonObject data = result.toObject();
        QJsonObject account = data[lit("account")].toObject();
        if(account.isEmpty() && data[lit("requiresAuth")].toBool(data[lit("requiresOpenaiAuth")].toBool(true)))
        {
          // Keep the initialized process alive so the panel can complete browser login.
          m_Account.clear();
          m_AccountType.clear();
          m_PendingText.clear();
          m_Conversation.SetThread(QString(), m_Conversation.Model());
          const QString message = m_Client->SupportsBrowserLogin()
              ? tr("Codex is not logged in. Click Log in, then send your question.")
              : data[lit("loginMessage")].toString(tr("Claude authentication is unavailable. Run claude auth login or configure an API key, then reconnect."));
          SetState(SessionState::NotLoggedIn, message);
          if(m_Conversation.TurnRunning())
            m_Conversation.EndTurn(lit("failed"), message);
          else
            m_Conversation.AddNotice(lit("warning"), message);
          ScheduleRender();
          if(m_Autotest.active && !m_QuestionActive)
            QTimer::singleShot(300, this, &AgentChatPanel::AutotestNext);
          QuestionFinished();
          return;
        }
        m_AccountType = account[lit("type")].toString();
        m_Account = account[lit("email")].toString();
        if(m_Account.isEmpty())
          m_Account = m_AccountType.isEmpty() ? tr("custom provider") : m_AccountType;
        SetSkillRoots(generation);
      });
}

void AgentChatPanel::ReadModels(int generation, const QString &cursor, const QJsonArray &models,
                                const QStringList &seenCursors)
{
  QJsonObject params;
  params[lit("limit")] = 100;
  if(!cursor.isEmpty())
    params[lit("cursor")] = cursor;
  m_Client->Request(AgentBackend::Operation::ListModels, params, ConfigTimeoutMs,
                    [this, generation, models, seenCursors](const QJsonValue &result,
                                                           const QJsonObject &error) {
    if(generation != m_Generation)
      return;
    if(!error.isEmpty() || !result.toObject()[lit("data")].isArray())
      return;    // Custom provider model names remain editable when enumeration is unavailable.
    QJsonArray all = models;
    for(const QJsonValue &model : result.toObject()[lit("data")].toArray())
      if(model.isObject())
        all << model;
    const QString next = result.toObject()[lit("nextCursor")].toString();
    if(!next.isEmpty())
    {
      if(seenCursors.contains(next) || seenCursors.size() >= 99)
        return;
      QStringList seen = seenCursors;
      seen << next;
      ReadModels(generation, next, all, seen);
      return;
    }
    m_Models = all;
  });
}

void AgentChatPanel::EditSettings()
{
  if(m_Conversation.TurnRunning() || m_State == SessionState::Starting || m_LoginStarting ||
     !m_LoginId.isEmpty())
    return;
  const QString previousBackend = ConfiguredBackend();
  bool accepted = false;
  if(m_Autotest.active)
  {
    PersistentConfig settings;
    ApplyAgentSettings(settings, AgentSettingsSnapshot(m_Ctx.Config()));
    ApplyAgentSettings(settings, m_Autotest.settings);
    accepted = AgentSettingsDialog::Edit(settings, m_Models, this, false);
    m_Autotest.settingsDialog[lit("accepted")] = accepted;
    const QString backend = AgentLaunchConfig::EffectiveBackend(settings);
    m_Autotest.settingsDialog[lit("appliedBackend")] = backend;
    m_Autotest.settingsDialog[lit("savedCodexModel")] = QString(settings.Agent_Model);
    m_Autotest.settingsDialog[lit("savedCodexEffort")] = QString(settings.Agent_ReasoningEffort);
    m_Autotest.settingsDialog[lit("savedClaudeModel")] = QString(settings.Agent_ClaudeModel);
    m_Autotest.settingsDialog[lit("savedClaudeEffort")] = QString(settings.Agent_ClaudeReasoningEffort);
    m_Autotest.settingsDialog[lit("appliedModel")] = backend == lit("claude")
        ? QString(settings.Agent_ClaudeModel) : QString(settings.Agent_Model);
    m_Autotest.settingsDialog[lit("appliedEffort")] = backend == lit("claude")
        ? QString(settings.Agent_ClaudeReasoningEffort) : QString(settings.Agent_ReasoningEffort);
    if(accepted)
      m_Autotest.settings = AgentSettingsSnapshot(settings);
  }
  else
    accepted = AgentSettingsDialog::Edit(m_Ctx.Config(), m_Models, this);
  if(!accepted)
    return;
  SaveHistory();
  KillSession();
  m_Models = QJsonArray();
  if(previousBackend != ConfiguredBackend())
    NewChat();
  m_Conversation.AddNotice(lit("info"), tr("Settings saved. The next question starts a new session."));
  ScheduleRender();
  UpdateControls();
}

void AgentChatPanel::Login()
{
  if(!m_LoginId.isEmpty())
  {
    CancelLogin();
    return;
  }
  if(!m_Client->SupportsBrowserLogin() || m_LoginStarting || !m_Client->IsRunning() || m_State != SessionState::NotLoggedIn)
    return;
  const int generation = m_Generation;
  const int request = ++m_LoginRequest;
  m_LoginStarting = true;
  m_LoginStatus = lit("starting");
  UpdateControls();
  QJsonObject params;
  params[lit("type")] = lit("chatgpt");
  m_Client->Request(AgentBackend::Operation::Login, params, AccountTimeoutMs,
                    [this, generation, request](const QJsonValue &result, const QJsonObject &error) {
    if(generation != m_Generation || request != m_LoginRequest)
      return;
    m_LoginStarting = false;
    const QJsonObject data = result.toObject();
    const QUrl url(data[lit("authUrl")].toString());
    if(!error.isEmpty() || data[lit("type")].toString() != lit("chatgpt") ||
       data[lit("loginId")].toString().isEmpty() || !url.isValid() || url.host().isEmpty() ||
       (url.scheme() != lit("https") && url.scheme() != lit("http")))
    {
      FinishLogin(lit("failed"), tr("Could not start login: %1").arg(ErrorMessage(error)));
      return;
    }
    m_LoginId = data[lit("loginId")].toString();
    m_LoginStatus = lit("waiting");
    SetState(SessionState::NotLoggedIn, tr("Complete login in your browser, or cancel here."));
    if(m_Autotest.active)
    {
      m_Autotest.authUrls << url.toString();
      if(m_Autotest.cancelLoginAfterMs > 0)
        QTimer::singleShot(m_Autotest.cancelLoginAfterMs, this, [this, generation, request]() {
          if(generation == m_Generation && request == m_LoginRequest && !m_LoginId.isEmpty())
            CancelLogin();
        });
    }
    else if(!QDesktopServices::openUrl(url))
    {
      CancelLogin();
      m_Conversation.AddNotice(lit("error"), tr("Could not open the login page in your browser."));
      ScheduleRender();
    }
  });
}

void AgentChatPanel::CancelLogin()
{
  if(m_LoginId.isEmpty())
    return;
  QJsonObject params;
  params[lit("loginId")] = m_LoginId;
  const int generation = m_Generation;
  const int request = ++m_LoginRequest;
  m_LoginId.clear();    // Ignore any completion racing with the cancellation response.
  m_LoginStarting = true;
  UpdateControls();
  m_Client->Request(AgentBackend::Operation::CancelLogin, params, AccountTimeoutMs,
                    [this, generation, request](const QJsonValue &result, const QJsonObject &error) {
    if(generation != m_Generation || request != m_LoginRequest)
      return;
    const QString status = result.toObject()[lit("status")].toString();
    if(error.isEmpty() && status == lit("notFound"))
    {
      m_LoginStarting = false;
      m_LoginStatus = lit("notFound");
      SetState(SessionState::Starting, tr("Login already ended. Checking account..."));
      ReadAccount(generation);
    }
    else if(error.isEmpty() && status == lit("canceled"))
      FinishLogin(lit("canceled"), tr("Login canceled."));
    else
      FinishLogin(lit("failed"), tr("Could not cancel login: %1")
                                    .arg(error.isEmpty() ? tr("Unexpected server response")
                                                         : ErrorMessage(error)));
  });
}

void AgentChatPanel::FinishLogin(const QString &status, const QString &message)
{
  m_LoginId.clear();
  m_LoginStarting = false;
  m_LoginStatus = status;
  SetState(SessionState::NotLoggedIn, message);
  m_Conversation.AddNotice(status == lit("failed") ? lit("error") : lit("info"), message);
  ScheduleRender();
  if(m_Autotest.active)
    QTimer::singleShot(0, this, &AgentChatPanel::AutotestNext);
}

void AgentChatPanel::SetSkillRoots(int generation)
{
  if(m_Launch.skillRoots.isEmpty())
  {
    m_Conversation.AddNotice(lit("warning"),
                             tr("RenderTest skills were not found; answers may be less reliable."));
    BeginThread(generation);
    return;
  }

  QJsonObject params;
  params[lit("extraRoots")] = QJsonArray::fromStringList(m_Launch.skillRoots);
  m_Client->Request(AgentBackend::Operation::SetSkillRoots, params, ConfigTimeoutMs,
                    [this, generation](const QJsonValue &, const QJsonObject &error) {
                      if(generation != m_Generation)
                        return;
                      if(!error.isEmpty())
                        m_Conversation.AddNotice(
                            lit("warning"),
                            tr("Could not register RenderTest skills: %1").arg(ErrorMessage(error)));
                      BeginThread(generation);
                    });
}

void AgentChatPanel::BeginThread(int generation)
{
  if(!m_Launch.isolate)
  {
    StartThread(generation, QStringList());
    return;
  }

  QJsonObject params;
  params[lit("cwd")] = m_Launch.workingDir;
  m_Client->Request(
      AgentBackend::Operation::ReadConfiguration, params, ConfigTimeoutMs,
      [this, generation](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;
        QStringList disabled;
        if(error.isEmpty() && result.toObject()[lit("config")].toObject()[lit("mcp_servers")].isObject())
        {
          QJsonObject servers =
              result.toObject()[lit("config")].toObject()[lit("mcp_servers")].toObject();
          for(const QString &name : servers.keys())
          {
            if(name != QString::fromLatin1(AgentLaunchConfig::ServerName))
              disabled << name;
          }
        }
        else
        {
          SessionFailed(SessionState::Error, tr("Could not verify MCP isolation: %1")
                                                 .arg(ErrorMessage(error)));
          return;
        }
        StartThread(generation, disabled);
      });
}

void AgentChatPanel::StartThread(int generation, const QStringList &disabledServers)
{
  QJsonObject config;
  for(const QString &name : disabledServers)
    config[lit("mcp_servers.%1.enabled").arg(name)] = false;
  config[lit("mcp_servers.rendertest.enabled")] = m_Launch.captureTools;

  QJsonObject params;
  params[lit("cwd")] = m_Launch.workingDir;
  params[lit("sandbox")] = lit("read-only");
  params[lit("approvalPolicy")] = lit("never");
  const QString resumeId = m_HistoryRecord.metadata.threadId;
  const bool resume = !resumeId.isEmpty();
  if(resume)
  {
    params[lit("threadId")] = resumeId;
    params[lit("excludeTurns")] = true;    // The local snapshot supplies UI history exactly once.
  }
  else
  {
    params[lit("ephemeral")] = false;
    params[lit("serviceName")] = lit("qrendertest");
  }
  params[lit("developerInstructions")] = AgentLaunchConfig::DeveloperInstructions(m_InstanceId);
  if(!m_Launch.model.isEmpty())
    params[lit("model")] = m_Launch.model;
  if(!config.isEmpty())
    params[lit("config")] = config;

  m_Client->Request(resume ? AgentBackend::Operation::ResumeSession : AgentBackend::Operation::CreateSession,
                    params, ThreadTimeoutMs,
                    [this, generation, resume, resumeId](const QJsonValue &result, const QJsonObject &error) {
                      if(generation != m_Generation)
                        return;
                      if(!error.isEmpty())
                      {
                        const QString message = resume
                            ? tr("Could not resume the saved session: %1. The saved conversation remains read-only; start a new chat to continue.").arg(ErrorMessage(error))
                            : tr("Could not start a session: %1").arg(ErrorMessage(error));
                        SessionFailed(SessionState::Error, message);
                        if(resume)
                          MakeHistoryReadOnly(message);
                        return;
                      }
                      QJsonObject data = result.toObject();
                      if(!m_Client->AcceptedToolPolicy(data))
                      {
                        SessionFailed(SessionState::Error,
                                      tr("%1 did not confirm the required tool restrictions; refusing to chat.")
                                          .arg(m_Client->DisplayName()));
                        return;
                      }
                      QString threadId = data[lit("thread")].toObject()[lit("id")].toString();
                      if(threadId.isEmpty() || (resume && threadId != resumeId))
                      {
                        const QString message = tr("The backend returned a different or empty session identity; refusing to continue.");
                        SessionFailed(SessionState::Error, message);
                        if(resume)
                          MakeHistoryReadOnly(message);
                        return;
                      }
                      m_HadThread = true;
                      m_Conversation.SetThread(threadId, data[lit("model")].toString());
                      m_HistoryRecord.metadata.threadId = threadId;
                      m_HistoryDirty = true;
                      SaveHistory();
                      m_ThreadIds << threadId;
                      PollMcpStatus(generation, 0);
                    });
}

void AgentChatPanel::PollMcpStatus(int generation, int attempt, const QString &cursor,
                                  const QJsonArray &entries, const QStringList &seenCursors)
{
  QJsonObject params;
  params[lit("threadId")] = m_Conversation.ThreadId();
  params[lit("detail")] = lit("toolsAndAuthOnly");
  params[lit("limit")] = 100;
  if(!cursor.isEmpty())
    params[lit("cursor")] = cursor;
  m_Client->Request(
      AgentBackend::Operation::ListToolServers, params, ConfigTimeoutMs,
      [this, generation, attempt, entries, seenCursors, cursor](const QJsonValue &result,
                                                               const QJsonObject &error) {
        if(generation != m_Generation)
          return;

        if(!error.isEmpty() || !result.toObject()[lit("data")].isArray())
        {
          SessionFailed(SessionState::Error, tr("Could not verify MCP server status: %1")
                                                 .arg(ErrorMessage(error)));
          return;
        }
        QJsonArray all = entries;
        for(const QJsonValue &entry : result.toObject()[lit("data")].toArray())
          all << entry;
        QJsonValue nextValue = result.toObject()[lit("nextCursor")];
        if(!nextValue.isNull() && !nextValue.isString())
        {
          SessionFailed(SessionState::Error, tr("MCP status response has no valid pagination cursor."));
          return;
        }
        QString next = nextValue.toString();
        if(!next.isEmpty())
        {
          if(next == cursor || seenCursors.contains(next) || seenCursors.size() >= 100)
          {
            SessionFailed(SessionState::Error, tr("MCP status pagination did not complete."));
            return;
          }
          QStringList seen = seenCursors;
          seen << next;
          PollMcpStatus(generation, attempt, next, all, seen);
          return;
        }

        const QString server = QString::fromLatin1(AgentLaunchConfig::ServerName);
        QString status = lit("unknown");
        QString toolsError;
        int tools = -1;
        QStringList others;
        if(error.isEmpty())
        {
          for(const QJsonValue &value : all)
          {
            QJsonObject entry = value.toObject();
            QString name = entry[lit("name")].toString();
            QString runtime = entry[lit("runtimeStatus")].toString();
            if(name == server)
            {
              status = runtime;
              tools = entry[lit("tools")].toObject().size();
              toolsError = entry[lit("toolsError")].toString();
            }
            else if(runtime != lit("disabled"))
            {
              others << name;
            }
          }
        }

        if(m_Launch.isolate && !others.isEmpty())
        {
          SessionFailed(SessionState::Error,
                        tr("MCP isolation failed; other servers are enabled: %1").arg(others.join(lit(", "))));
          return;
        }
        if(m_Launch.captureTools && (status == lit("starting") || status == lit("notStarted")) &&
           attempt + 1 < McpPollAttempts)
        {
          m_McpStatus = status;
          UpdateControls();
          QTimer::singleShot(McpPollIntervalMs, this, [this, generation, attempt]() {
            if(generation == m_Generation)
              PollMcpStatus(generation, attempt + 1);
          });
          return;
        }

        m_McpStatus = status;
        m_McpTools = tools;
        m_OtherServers = others;
        if(status != (m_Launch.captureTools ? lit("connected") : lit("disabled")))
        {
          SessionFailed(SessionState::Error,
                        tr("The rendertest MCP server must be %1, but is %2: %3")
                            .arg(m_Launch.captureTools ? lit("connected") : lit("disabled"), status, toolsError));
          return;
        }

        SetState(SessionState::Ready);
        m_ResumeValidationPending = false;
        SaveHistory();
        ScheduleRender();
        if(m_Conversation.TurnRunning() && !m_PendingText.isEmpty())
          StartTurn();
        else if(m_Autotest.active)
          QTimer::singleShot(0, this, &AgentChatPanel::AutotestNext);
      });
}

void AgentChatPanel::StartTurn()
{
  QString instance, session;
  ReadCaptureBinding(instance, session);
  if(m_HistoryReadOnly || !CaptureFileUnchanged() || instance != m_InstanceId || session != m_CaptureSessionId)
  {
    SessionFailed(SessionState::Error,
                  tr("The capture changed while connecting. Send the question again."));
    return;
  }
  const int generation = m_Generation;
  const int turnRequest = ++m_TurnRequest;
  QString text = m_PendingText;
  m_PendingText.clear();
  m_TurnTexts << text;

  QJsonObject input;
  input[lit("type")] = lit("text");
  input[lit("text")] = text;
  input[lit("text_elements")] = QJsonArray();
  QJsonObject params;
  params[lit("threadId")] = m_Conversation.ThreadId();
  params[lit("input")] = QJsonArray({input});
  if(!m_Launch.reasoningEffort.isEmpty())
    params[lit("effort")] = m_Launch.reasoningEffort;

  m_Client->Request(
      AgentBackend::Operation::SendTurn, params, m_Autotest.active ? m_Autotest.turnTimeoutMs : TurnTimeoutMs,
      [this, generation, turnRequest](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation || turnRequest != m_TurnRequest ||
           !m_Conversation.TurnRunning())
          return;
        if(!error.isEmpty())
        {
          // A missing response does not prove that the server never started the turn.
          // End this process generation before allowing another question.
          SessionFailed(SessionState::Error,
                        tr("Could not start the turn; the session was stopped: %1").arg(ErrorMessage(error)));
          return;
        }
        m_Conversation.SetTurnId(result.toObject()[lit("turn")].toObject()[lit("id")].toString());
        UpdateControls();
      });
}

void AgentChatPanel::SessionFailed(SessionState state, const QString &message)
{
  KillSession();
  SetState(state, message);
  m_PendingText.clear();
  if(m_Conversation.TurnRunning())
    m_Conversation.EndTurn(lit("failed"), message);
  else
    m_Conversation.AddNotice(lit("error"), message);
  if(m_ResumeValidationPending)
    MakeHistoryReadOnly(message);
  ScheduleRender();
  QuestionFinished();
}

void AgentChatPanel::KillSession()
{
  m_Generation++;
  m_LoginRequest++;
  m_LoginId.clear();
  m_LoginStarting = false;
  m_StopTimer->stop();
  m_ExpectExit = true;
  m_Client->Kill();
  m_ExpectExit = false;
  m_Conversation.SetThread(QString(), m_Conversation.Model());
  m_State = SessionState::Idle;
}

void AgentChatPanel::Stop()
{
  if(!m_Conversation.TurnRunning())
    return;

  m_StopRequested = true;
  m_StopClock.start();

  if(m_State != SessionState::Ready || m_Conversation.TurnId().isEmpty())
  {
    ForceStop();
    return;
  }

  QJsonObject params;
  params[lit("threadId")] = m_Conversation.ThreadId();
  params[lit("turnId")] = m_Conversation.TurnId();
  m_Client->Request(AgentBackend::Operation::InterruptTurn, params, StopGraceMs,
                    AgentBackend::ResponseCallback());
  m_StopTimer->start();
  UpdateControls();
}

void AgentChatPanel::ForceStop()
{
  if(!m_Conversation.TurnRunning())
    return;

  KillSession();
  m_PendingText.clear();
  m_Conversation.EndTurn(lit("interrupted"),
                         tr("Stopped. The next message reconnects to this saved conversation."));
  ScheduleRender();
  UpdateControls();
  QuestionFinished();
}

void AgentChatPanel::NewChat()
{
  if(m_Conversation.TurnRunning())
    return;
  SaveHistory();
  if(!m_LastSaveError.isEmpty())
  {
    UpdateControls();
    return;
  }
  KillSession();
  m_History->Release();
  m_HistoryRecord = AgentHistoryRecord();
  m_HistoryReadOnly = false;
  m_HistoryLoaded = false;
  m_HistoryDirty = false;
  m_ResumeValidationPending = false;
  m_VerifyHistoryAfterIdentity = false;
  m_HistoryError.clear();
  m_LastSaveError.clear();
  m_ReferenceToken = AgentHistory::NewId();
  m_Conversation.Clear();
  m_ExpandedTools.clear();
  AgentToolView::ClearCache();
  m_HadThread = false;
  m_IncludeEvent = true;
  UpdateChips();
  ScheduleRender();
  UpdateControls();
}

void AgentChatPanel::OnNotification(const QString &method, const QJsonObject &params)
{
  if(method == lit("backend/disconnected"))
  {
    SessionFailed(params[lit("requiresAuth")].toBool() ? SessionState::NotLoggedIn : SessionState::Error,
                  params[lit("message")].toString(tr("The backend disconnected. Send again to reconnect.")));
    return;
  }
  if(method == lit("account/login/completed"))
  {
    if(m_LoginId.isEmpty() || params[lit("loginId")].toString() != m_LoginId)
      return;
    if(!params[lit("success")].toBool(false))
    {
      FinishLogin(lit("failed"), tr("Login failed: %1").arg(params[lit("error")].toString()));
      return;
    }
    m_LoginId.clear();
    m_LoginStarting = false;
    m_LoginStatus = lit("completed");
    SetState(SessionState::Starting, tr("Login completed. Connecting..."));
    ReadAccount(m_Generation);
    return;
  }
  if(method == lit("account/updated"))
  {
    if(m_LoginStarting || !m_LoginId.isEmpty())
      return;
    if(m_State == SessionState::NotLoggedIn)
    {
      SetState(SessionState::Starting, tr("Checking account..."));
      ReadAccount(m_Generation);
      return;
    }
    if(m_State != SessionState::Ready)
      return;
    const int generation = m_Generation;
    QJsonObject request;
    request[lit("refreshToken")] = false;
    m_Client->Request(AgentBackend::Operation::ReadAccount, request, AccountTimeoutMs,
                      [this, generation](const QJsonValue &result, const QJsonObject &error) {
      if(generation != m_Generation || !error.isEmpty())
        return;
      const QJsonObject data = result.toObject();
      QJsonObject account = data[lit("account")].toObject();
      if(account.isEmpty() && data[lit("requiresAuth")].toBool(data[lit("requiresOpenaiAuth")].toBool(true)))
      {
        const QString message = m_Client->SupportsBrowserLogin()
            ? tr("Codex is no longer logged in. Log in before sending another question.")
            : data[lit("loginMessage")].toString(tr("Claude authentication changed. Authenticate externally, then reconnect."));
        m_Account.clear();
        m_AccountType.clear();
        m_PendingText.clear();
        if(m_Conversation.TurnRunning())
        {
          // Do not leave an authenticated remote turn alive after losing its local account state.
          m_Conversation.EndTurn(lit("failed"), message);
          KillSession();
          StartSession();
          QuestionFinished();
        }
        else
        {
          m_Conversation.SetThread(QString(), m_Conversation.Model());
          SetState(SessionState::NotLoggedIn, message);
          m_Conversation.AddNotice(lit("warning"), message);
        }
        ScheduleRender();
        return;
      }
      m_AccountType = account[lit("type")].toString();
      m_Account = account[lit("email")].toString(m_AccountType);
      UpdateControls();
    });
    return;
  }
  if(m_State != SessionState::Ready || !m_Conversation.TurnRunning())
    return;
  bool changed = m_Conversation.Apply(method, params);
  if(changed)
    m_HistoryDirty = true;

  if(m_Autotest.active && !m_Autotest.stopOnTool.isEmpty() &&
     m_Autotest.index - 1 == m_Autotest.stopQuestion && method == lit("item/started"))
  {
    QJsonObject item = params[lit("item")].toObject();
    if(item[lit("type")].toString() == lit("mcpToolCall") &&
       item[lit("tool")].toString() == m_Autotest.stopOnTool && !m_StopRequested)
      QTimer::singleShot(0, this, &AgentChatPanel::Stop);
  }

  if(method == lit("turn/completed") && m_QuestionActive && !m_Conversation.TurnRunning())
  {
    m_StopTimer->stop();
    QuestionFinished();
  }

  if(changed)
    ScheduleRender();
  UpdateControls();
}

void AgentChatPanel::OnServerRequest(const QString &method, bool supported)
{
  QJsonObject entry;
  entry[lit("method")] = method;
  entry[lit("supported")] = supported;
  m_ServerRequests << entry;
  m_Conversation.AddNotice(lit("info"),
                           supported ? tr("Declined %1: the AI panel is read-only.").arg(method)
                                     : tr("Rejected unsupported request %1.").arg(method));
  ScheduleRender();
}

void AgentChatPanel::OnProcessExited(int exitCode, bool killed, const QString &reason)
{
  if(m_ExpectExit)
    return;

  QString message =
      tr("The %1 backend %2 (code %3).").arg(m_Client->DisplayName(), killed ? lit("was killed") : reason).arg(exitCode);
  QString tail = LastLines(m_Client->StderrTail(), 8);
  if(!tail.isEmpty())
    message += lit("\n") + tail;
  message += lit("\n") + tr("The next message restarts it. Log: %1").arg(LogPath());

  m_Generation++;
  m_LoginRequest++;
  m_LoginStarting = false;
  m_LoginId.clear();
  m_StopTimer->stop();
  m_Conversation.SetThread(QString(), m_Conversation.Model());
  m_PendingText.clear();
  SetState(SessionState::Error, tr("%1 backend stopped").arg(m_Client->DisplayName()));
  if(m_Conversation.TurnRunning())
    m_Conversation.EndTurn(lit("failed"), message);
  else
    m_Conversation.AddNotice(lit("error"), message);
  if(m_ResumeValidationPending)
    MakeHistoryReadOnly(message);
  ScheduleRender();
  QuestionFinished();
}

void AgentChatPanel::QuestionFinished()
{
  m_HistoryDirty = true;
  SaveHistory();
  if(!m_QuestionActive)
    return;
  m_QuestionActive = false;

  QJsonObject result;
  result[lit("index")] = m_Autotest.index - 1;
  result[lit("status")] = m_Conversation.LastTurnStatus();
  if(m_StopRequested)
    result[lit("stopLatencyMs")] = double(m_StopClock.elapsed());
  m_StopRequested = false;

  UpdateControls();

  if(m_Autotest.active)
  {
    m_Autotest.results << result;
    QTimer::singleShot(300, this, &AgentChatPanel::AutotestNext);
  }
}

void AgentChatPanel::UpdateControls()
{
  const bool running = m_Conversation.TurnRunning();
  const bool loginBusy = m_LoginStarting || !m_LoginId.isEmpty();
  const bool captureTransition = m_Ctx.IsCaptureLoading() ||
      (m_Ctx.IsCaptureLoaded() && m_LoadedIdentity[lit("kind")].toString() == lit("none"));
  m_Send->setEnabled(!running && !loginBusy && !m_HistoryReadOnly && !m_IdentityPending && !captureTransition);
  m_Input->setReadOnly(m_HistoryReadOnly);
  m_StopButton->setEnabled(running);
  m_NewChat->setEnabled(!running && !loginBusy);
  m_Settings->setEnabled(!running && m_State != SessionState::Starting && !loginBusy);
  m_HistoryButton->setEnabled(!running && m_State != SessionState::Starting && !loginBusy);
  QString historyNotice;
  if(m_IdentityPending)
    historyNotice = tr("Verifying the capture file for conversation history...");
  else if(m_HistoryReadOnly)
    historyNotice = m_HistoryError;
  else if(!m_LastSaveError.isEmpty())
    historyNotice = tr("History could not be saved: %1").arg(m_LastSaveError);
  else if(m_HistoryLoaded)
    historyNotice = m_State == SessionState::Ready
                        ? tr("Saved conversation resumed. Earlier capture links remain inactive.")
                        : tr("Saved conversation opened. Send a message to resume; old capture links remain inactive.");
  m_HistoryNotice->setText(historyNotice);
  m_HistoryNotice->setVisible(!historyNotice.isEmpty());
  m_Login->setVisible(m_State == SessionState::NotLoggedIn && m_Client->SupportsBrowserLogin());
  m_Login->setEnabled(m_Client->SupportsBrowserLogin() && m_Client->IsRunning() && !m_LoginStarting);
  m_Login->setText(m_LoginId.isEmpty() ? tr("Log in") : tr("Cancel login"));

  QString color = lit("gray");
  QString state;
  switch(m_State)
  {
    case SessionState::Idle: state = tr("Not started"); break;
    case SessionState::Starting:
      state = tr("Starting");
      color = lit("#c08000");
      break;
    case SessionState::Ready:
      state = running ? tr("Working") : tr("Ready");
      color = running ? lit("#c08000") : lit("#2a9d2a");
      break;
    case SessionState::NotLoggedIn:
      state = loginBusy ? tr("Waiting for login") : tr("Not logged in");
      color = lit("#c03030");
      break;
    case SessionState::Error:
      state = tr("Error");
      color = lit("#c03030");
      break;
  }
  if(m_StopRequested && running)
    state = tr("Stopping");

  const QString dot = lit(" %1 ").arg(Glyph(0x00B7));
  QStringList parts;
  parts << lit("<span style=\"color:%1\">%2</span> %3").arg(color, Glyph(0x25CF), state.toHtmlEscaped());
  parts << (ConfiguredBackend() == lit("claude") ? tr("Claude") : tr("Codex"));
  if(!m_Conversation.Model().isEmpty())
    parts << m_Conversation.Model().toHtmlEscaped();
  if(!m_Account.isEmpty())
    parts << m_Account.toHtmlEscaped();
  if(!m_McpStatus.isEmpty())
  {
    QString mcp = m_McpStatus == lit("connected")
                      ? tr("rendertest %1 %2 tools").arg(Glyph(0x2713)).arg(m_McpTools)
                      : tr("rendertest %1").arg(m_McpStatus);
    parts << mcp.toHtmlEscaped();
  }
  if(!m_Launch.isolate && m_State == SessionState::Ready)
    parts << lit("<span style=\"color:#c08000\">%1</span>").arg(tr("not isolated"));
  if(m_Conversation.TotalTokens() > 0)
    parts << tr("%1 tokens").arg(m_Conversation.TotalTokens());
  m_Status->setText(parts.join(dot));
  m_Status->setToolTip(m_StateMessage);
}

void AgentChatPanel::ScheduleRender()
{
  if(m_HistoryDirty && !m_HistoryReadOnly && !m_HistoryRecord.metadata.id.isEmpty())
  {
    if(!m_HistoryTimer->isActive())
      m_HistoryTimer->start();
  }
  if(!m_RenderTimer->isActive())
    m_RenderTimer->start();
}

QString AgentChatPanel::RenderItem(const AgentItem &item, QList<AgentToolImage> *images) const
{
  switch(item.kind)
  {
    case AgentItem::Kind::User:
    {
      QString text = item.text.toHtmlEscaped().replace(lit("\n"), lit("<br/>"));
      QStringList labels;
      for(const QJsonValue &chip : item.context)
        labels << Elide(chip.toObject()[lit("label")].toString(), 60).toHtmlEscaped();
      if(!labels.isEmpty())
        text += lit("<br/><span style=\"color:#c8d8ea;\">%1 %2</span>")
                    .arg(tr("Context:").toHtmlEscaped(), labels.join(lit(" %1 ").arg(Glyph(0x00B7))));
      return lit("<table width=\"100%\" cellpadding=\"6\" bgcolor=\"#3a5f8a\" "
                 "style=\"margin-top:8px;\"><tr><td><span style=\"color:#ffffff;\"><b>%1</b><br/>"
                 "%2</span></td></tr></table>")
          .arg(tr("You"), text);
    }
    case AgentItem::Kind::Agent:
    {
      if(item.text.isEmpty())
        return QString();
      return lit("<div style=\"margin-top:6px;\"><b>%1</b>%2</div>")
          .arg(tr("AI"), AgentMarkdown::ToHtml(item.text, item.referenceToken));
    }
    case AgentItem::Kind::Reasoning:
    {
      QString summary = LastLines(item.text, 1);
      summary.remove(QLatin1Char('*'));
      if(summary.size() > 160)
        summary = summary.left(160) + Glyph(0x2026);
      if(summary.isEmpty())
        summary = tr("Thinking") + Glyph(0x2026);
      return lit("<p style=\"color:gray;\"><i>%1</i></p>").arg(summary.toHtmlEscaped());
    }
    case AgentItem::Kind::Tool:
    {
      return AgentToolView::Render(item, m_ExpandedTools.contains(AgentToolView::ItemKey(item)),
                                   RenderTestAgentIntegration::AgentRoot(), images);
    }
    case AgentItem::Kind::Notice:
    {
      QString color = lit("gray");
      if(item.type == lit("warning"))
        color = lit("#c08000");
      else if(item.type == lit("error"))
        color = lit("#c03030");
      QString text = item.text.toHtmlEscaped().replace(lit("\n"), lit("<br/>"));
      return lit("<p style=\"color:%1;\">%2</p>").arg(color, text);
    }
    case AgentItem::Kind::Other:
    {
      return lit("<p style=\"color:gray;\">%1 %2</p>").arg(Glyph(0x25B8), item.text.toHtmlEscaped());
    }
  }
  return QString();
}

void AgentChatPanel::Render()
{
  QString html;
  QList<AgentToolImage> images;
  if(m_Conversation.Items().isEmpty())
  {
    html = lit("<p style=\"color:gray;\">%1</p>")
               .arg(tr("Ask a question about the open capture. The assistant reads it through the "
                       "rendertest tools of this window and cannot change files.")
                        .toHtmlEscaped());
  }

  for(const AgentItem &item : m_Conversation.Items())
    html += RenderItem(item, &images);

  if(m_Conversation.TurnRunning())
  {
    const QList<AgentItem> &items = m_Conversation.Items();
    if(items.isEmpty() || items.last().kind != AgentItem::Kind::Agent)
      html += lit("<p style=\"color:gray;\">%1</p>").arg(Glyph(0x2026));
  }

  QScrollBar *bar = m_View->verticalScrollBar();
  const bool atBottom = bar->value() >= bar->maximum() - 4;
  const int previous = bar->value();
  m_View->setHtml(html);
  for(const AgentToolImage &image : images)
    if(image.IsValid())
      m_View->document()->addResource(QTextDocument::ImageResource, image.resourceUrl, image.thumbnail);
  if(!images.isEmpty())
    m_View->document()->markContentsDirty(0, m_View->document()->characterCount());
  bar->setValue(atBottom ? bar->maximum() : previous);
}

void AgentChatPanel::RunAutotest(const QJsonObject &config)
{
  m_Autotest = Autotest();
  m_Autotest.active = true;
  m_Autotest.loadHistoryId = config[lit("loadHistoryId")].toString();
  m_Autotest.newChatBeforeQuestion = config[lit("newChatBeforeQuestion")].toArray();
  m_Autotest.settings = config[lit("settings")].toObject();
  m_Autotest.autoLogin = config[lit("autoLogin")].toBool(false);
  m_Autotest.cancelLoginAfterMs = config[lit("cancelLoginAfterMs")].toInt(0);
  m_Autotest.screenshot = config[lit("screenshot")].toString();
  m_Autotest.settingsScreenshot = config[lit("settingsScreenshot")].toString();
  m_Autotest.settingsTypeModel = config[lit("settingsTypeModel")].toString();
  m_Autotest.settingsTypeEffort = config[lit("settingsTypeEffort")].toString();
  m_Autotest.settingsSelectModel = config[lit("settingsSelectModel")].toString();
  m_Autotest.settingsAccept = config[lit("settingsAccept")].toBool(false);
  m_Autotest.settingsCloseAfterMs = config[lit("settingsCloseAfterMs")].toInt(0);
  m_Autotest.historyScreenshot = config[lit("historyScreenshot")].toString();
  for(const QJsonValue &question : config[lit("questions")].toArray())
    m_Autotest.questions << question.toString();
  m_Autotest.asks = config[lit("asks")].toArray();
  m_Autotest.clickLinks = config[lit("clickLinks")].toArray();
  m_Autotest.transcript = config[lit("transcript")].toString();
  m_Autotest.stopOnTool = config[lit("stopOnTool")].toString();
  m_Autotest.stopAfterMs = config[lit("stopAfterMs")].toInt(0);
  m_Autotest.stopQuestion = config[lit("stopQuestion")].toInt(0);
  m_Autotest.turnTimeoutMs = qMax(1, config[lit("turnTimeoutMs")].toInt(TurnTimeoutMs));
  m_Autotest.captureChangeAfterMs = config[lit("captureChangeAfterMs")].toInt(0);
  m_Autotest.captureChangeBeforeLinks = config[lit("captureChangeBeforeLinks")].toBool(false);
  m_Autotest.waitForCapture = config[lit("waitForCapture")].toBool(false);
  m_Autotest.captureTimeoutMs = config[lit("captureTimeoutMs")].toInt(180000);
  m_Autotest.selectEvent = qint64(config[lit("selectEvent")].toDouble(-1.0));
  m_Autotest.clock.start();

  qInfo() << "AI panel autotest:" << m_Autotest.questions.size() << "questions";
  AutotestNext();
}

void AgentChatPanel::AutotestNext()
{
  if(!m_Autotest.active || m_QuestionActive)
    return;

  if(m_LoginStarting || !m_LoginId.isEmpty() || m_State == SessionState::Starting || m_IdentityPending)
    return;
  if(m_Autotest.autoLogin && m_Client->SupportsBrowserLogin() && !m_Autotest.loginAttempted &&
     m_State == SessionState::NotLoggedIn)
  {
    m_Autotest.loginAttempted = true;
    Login();
    return;
  }

  if(m_Autotest.index == 0 && m_Autotest.waitForCapture &&
     (!m_Ctx.IsCaptureLoaded() || m_Ctx.IsCaptureLoading()))
  {
    if(m_Autotest.clock.elapsed() < m_Autotest.captureTimeoutMs)
    {
      QTimer::singleShot(250, this, &AgentChatPanel::AutotestNext);
      return;
    }
    m_Conversation.AddNotice(lit("error"), lit("autotest: the capture did not load in time"));
    m_Autotest.questions.clear();
  }

  if(m_Ctx.IsCaptureLoaded() && m_LoadedIdentity[lit("kind")].toString() == lit("none"))
  {
    // CaptureContext publishes its loaded flag before delivering OnCaptureLoaded to viewers.
    QTimer::singleShot(250, this, &AgentChatPanel::AutotestNext);
    return;
  }

  if(!m_Autotest.prepared)
  {
    m_Autotest.prepared = true;
    if(m_Autotest.selectEvent >= 0 && m_Ctx.IsCaptureLoaded())
      m_Ctx.SetEventID({}, uint32_t(m_Autotest.selectEvent), uint32_t(m_Autotest.selectEvent));
    m_Autotest.eventBefore = m_Ctx.IsCaptureLoaded() ? qint64(m_Ctx.CurEvent()) : -1;
  }

  if(!m_Autotest.historyPrepared)
  {
    m_Autotest.historyPrepared = true;
    QString id = m_Autotest.loadHistoryId;
    if(id == lit("latest"))
    {
      QString warning;
      const QList<AgentHistoryEntry> entries = m_History->List(warning);
      id = entries.isEmpty() ? QString() : entries.first().id;
      if(id.isEmpty())
      {
        MakeHistoryReadOnly(warning.isEmpty() ? lit("No saved conversation was found.") : warning);
        m_Conversation.AddNotice(lit("error"), m_HistoryError);
      }
    }
    if(!id.isEmpty() && !LoadHistory(id))
      MakeHistoryReadOnly(m_HistoryError);
    if(m_IdentityPending)
      return;
  }

  if(m_Autotest.newChatBeforeQuestion.contains(m_Autotest.index) &&
     m_Autotest.newChatApplied != m_Autotest.index)
  {
    m_Autotest.newChatApplied = m_Autotest.index;
    NewChat();
  }

  if(m_Autotest.index >= m_Autotest.questions.size())
  {
    AutotestClickLinks();
    return;
  }

  if(m_HistoryReadOnly || !CaptureFileUnchanged())
  {
    if(!m_HistoryReadOnly)
      MakeHistoryReadOnly(lit("The capture file changed on disk."));
    QJsonObject result;
    result[lit("index")] = m_Autotest.index++;
    result[lit("status")] = lit("blocked");
    m_Autotest.results << result;
    QTimer::singleShot(0, this, &AgentChatPanel::AutotestNext);
    return;
  }

  if(m_Autotest.askApplied != m_Autotest.index && m_Autotest.index < m_Autotest.asks.size() &&
     m_Autotest.asks[m_Autotest.index].isObject())
  {
    m_Autotest.askApplied = m_Autotest.index;
    AutotestAsk(m_Autotest.index, m_Autotest.asks[m_Autotest.index].toObject());
    return;
  }

  const int index = m_Autotest.index++;
  if(!m_Autotest.questions[index].isEmpty())
    m_Input->setPlainText(m_Autotest.questions[index]);
  Send();

  if(m_Autotest.stopAfterMs > 0 && index == m_Autotest.stopQuestion)
  {
    QTimer::singleShot(m_Autotest.stopAfterMs, this, [this, index]() {
      if(m_Autotest.active && m_Autotest.index - 1 == index && m_QuestionActive)
        Stop();
    });
  }
  if(m_Autotest.captureChangeAfterMs > 0 && index == 0)
  {
    QTimer::singleShot(m_Autotest.captureChangeAfterMs, this, [this, index]() {
      if(m_Autotest.active && m_Autotest.index - 1 == index && m_QuestionActive)
        OnCaptureClosed();
    });
  }
}

void AgentChatPanel::AutotestAsk(int index, const QJsonObject &ask)
{
  const QString source = ask[lit("source")].toString();
  if(source == lit("chip"))
  {
    AgentAskRequest request;
    request.chip = AgentContextChip::FromJson(ask[lit("chip")].toObject());
    request.question = ask[lit("question")].toString();
    AgentAsk::Request(m_Ctx, request);
    AutotestAskDone(index, ask, true, QString());
    return;
  }

  if(!m_Ctx.IsCaptureLoaded())
  {
    AutotestAskDone(index, ask, false, lit("no capture loaded"));
    return;
  }

  if(source == lit("event"))
  {
    AgentAsk::Request(m_Ctx, AgentAsk::Event(m_Ctx, uint32_t(ask[lit("eventId")].toInt())));
    AutotestAskDone(index, ask, true, QString());
    return;
  }

  if(source == lit("texturePixel"))
  {
    const ResourceId id =
        AgentAsk::ResourceIdFromNumber(ask[lit("resourceId")].toString().toULongLong());
    const uint32_t x = uint32_t(ask[lit("x")].toInt());
    const uint32_t y = uint32_t(ask[lit("y")].toInt());
    m_Ctx.ShowTextureViewer();
    ITextureViewer *viewer = m_Ctx.GetTextureViewer();
    if(viewer->GetCurrentResource() != id)
      viewer->ViewTexture(id, CompType::Typeless, false);

    QTimer::singleShot(1000, this, [this, index, ask, x, y]() {
      ITextureViewer *viewer = m_Ctx.GetTextureViewer();
      viewer->GotoLocation(x, y);
      QTimer::singleShot(500, this, [this, index, ask]() {
        TextureViewer *texViewer = qobject_cast<TextureViewer *>(m_Ctx.GetTextureViewer()->Widget());
        bool ok = texViewer && texViewer->AskAIAboutPixel();
        AutotestAskDone(index, ask, ok,
                        ok ? QString() : lit("the texture viewer had no picked pixel"));
      });
    });
    return;
  }

  AutotestAskDone(index, ask, false, lit("unknown ask source %1").arg(source));
}

void AgentChatPanel::AutotestAskDone(int index, const QJsonObject &ask, bool ok, const QString &error)
{
  QJsonArray chips;
  for(const AgentContextChip &chip : ContextSnapshot())
    chips << chip.ToJson();

  QJsonObject result;
  result[lit("index")] = index;
  result[lit("source")] = ask[lit("source")].toString();
  result[lit("ok")] = ok;
  if(!error.isEmpty())
    result[lit("error")] = error;
  result[lit("chips")] = chips;
  result[lit("input")] = m_Input->toPlainText();
  result[lit("panelVisible")] = isVisible();
  result[lit("inputFocused")] = m_Input->hasFocus();
  m_Autotest.askResults << result;

  QTimer::singleShot(300, this, &AgentChatPanel::AutotestNext);
}

void AgentChatPanel::AutotestClickLinks()
{
  if(m_Autotest.captureChangeBeforeLinks)
    OnCaptureClosed();
  m_Autotest.eventAfter = m_Ctx.IsCaptureLoaded() ? qint64(m_Ctx.CurEvent()) : -1;

  const QStringList rendered = RenderedLinks(m_Conversation.Items());
  for(const QJsonValue &value : m_Autotest.clickLinks)
  {
    QString link = value.toString();
    if(link == lit("first-event") || link == lit("first-resource"))
    {
      QString prefix = link == lit("first-event") ? lit("rt://event/") : lit("rt://resource/");
      link.clear();
      for(const QString &candidate : rendered)
      {
        if(candidate.startsWith(prefix))
        {
          link = candidate;
          break;
        }
      }
    }
    else if(link == lit("first-tool") || link == lit("first-artifact"))
    {
      const bool artifact = link == lit("first-artifact");
      link.clear();
      for(const AgentItem &item : m_Conversation.Items())
      {
        if(item.kind != AgentItem::Kind::Tool || (artifact && item.artifacts.isEmpty()))
          continue;
        link = artifact ? lit("agent-artifact://item/%1/0").arg(AgentToolView::ItemKey(item))
                        : lit("agent-tool://item/%1").arg(AgentToolView::ItemKey(item));
        break;
      }
    }

    QJsonObject click;
    click[lit("requested")] = value.toString();
    click[lit("link")] = link;
    click[lit("ok")] = !link.isEmpty() && OpenLink(QUrl(link));
    click[lit("eventAfter")] = m_Ctx.IsCaptureLoaded() ? double(m_Ctx.CurEvent()) : -1.0;
    m_Autotest.linkClicks << click;
  }

  Render();
  if(!m_Autotest.screenshot.isEmpty())
  {
    QDir().mkpath(QFileInfo(m_Autotest.screenshot).absolutePath());
    grab().save(m_Autotest.screenshot);
  }
  if(!m_Autotest.settingsScreenshot.isEmpty())
  {
    // Exercise asynchronous discovery in the real dialog using an independent config copy.
    QTimer probe(this);
    const qint64 started = QDateTime::currentMSecsSinceEpoch();
    connect(&probe, &QTimer::timeout, this, [this, &probe, started]() {
      QDialog *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
      if(!dialog || dialog->objectName() != lit("agentSettings"))
        return;
      QComboBox *model = dialog->findChild<QComboBox *>(lit("agentModel"));
      QComboBox *effort = dialog->findChild<QComboBox *>(lit("agentReasoningEffort"));
      QLabel *status = dialog->findChild<QLabel *>(lit("agentModelsStatus"));
      const QString state = status ? status->property("catalogState").toString() : QString();
      const qint64 elapsed = QDateTime::currentMSecsSinceEpoch() - started;
      const bool pending = state == lit("starting") || state == lit("loading");
      const bool cancel = state == lit("loading") && m_Autotest.settingsCloseAfterMs > 0 &&
                          elapsed >= m_Autotest.settingsCloseAfterMs;
      if(model && effort && pending &&
         !m_Autotest.settingsDialog[lit("inputApplied")].toBool() &&
         !m_Autotest.settingsTypeModel.isEmpty())
      {
        model->setEditText(m_Autotest.settingsTypeModel);
        if(!m_Autotest.settingsTypeEffort.isEmpty())
          effort->setEditText(m_Autotest.settingsTypeEffort);
        m_Autotest.settingsDialog[lit("inputApplied")] = true;
        m_Autotest.settingsDialog[lit("typedWhileLoading")] = true;
      }
      if(pending && !cancel && elapsed < 20000)
        return;
      probe.stop();
      if(elapsed >= 20000)
        m_Autotest.settingsDialog[lit("timedOut")] = true;
      m_Autotest.settingsDialog[lit("catalogState")] = state;
      m_Autotest.settingsDialog[lit("catalogMessage")] = status ? status->text() : QString();
      if(model && effort)
      {
        if(state == lit("ready") && !m_Autotest.settingsSelectModel.isEmpty())
          model->setCurrentIndex(model->findData(m_Autotest.settingsSelectModel));
        auto value = [](QComboBox *combo) {
          return combo->currentIndex() >= 0 && combo->currentText() == combo->itemText(combo->currentIndex())
                     ? combo->currentData().toString() : combo->currentText().trimmed();
        };
        m_Autotest.settingsDialog[lit("model")] = value(model);
        m_Autotest.settingsDialog[lit("effort")] = value(effort);
        m_Autotest.settingsDialog[lit("modelCount")] = model->count();
        m_Autotest.settingsDialog[lit("effortEditable")] = effort->isEditable();
        QJsonArray options;
        for(int i = 0; i < effort->count(); i++)
          options << effort->itemData(i).toString();
        m_Autotest.settingsDialog[lit("effortOptions")] = options;
      }
      QDir().mkpath(QFileInfo(m_Autotest.settingsScreenshot).absolutePath());
      m_Autotest.settingsDialog[lit("screenshotSaved")] = dialog->grab().save(m_Autotest.settingsScreenshot);
      QDialogButtonBox *buttons = dialog->findChild<QDialogButtonBox *>();
      if(m_Autotest.settingsAccept && !cancel && elapsed < 20000 && buttons)
      {
        buttons->button(QDialogButtonBox::Save)->click();
        if(dialog->isVisible())
        {
          QLabel *error = dialog->findChild<QLabel *>(lit("agentSettingsError"));
          m_Autotest.settingsDialog[lit("validationError")] = error ? error->text() : QString();
          dialog->reject();
        }
      }
      else
        dialog->reject();
    });
    probe.start(50);
    EditSettings();
    probe.stop();
  }
  if(!m_Autotest.historyScreenshot.isEmpty())
  {
    QTimer::singleShot(100, this, [this]() {
      QDialog *dialog = qobject_cast<QDialog *>(QApplication::activeModalWidget());
      if(!dialog || dialog->objectName() != lit("agentHistory"))
        return;
      QTreeWidget *list = dialog->findChild<QTreeWidget *>(lit("agentHistoryList"));
      if(list)
        m_Autotest.historyDialog[lit("count")] = list->topLevelItemCount();
      QDir().mkpath(QFileInfo(m_Autotest.historyScreenshot).absolutePath());
      m_Autotest.historyDialog[lit("screenshotSaved")] = dialog->grab().save(m_Autotest.historyScreenshot);
      dialog->reject();
    });
    ShowHistory();
  }
  WriteTranscript();
  m_Autotest.active = false;
}

void AgentChatPanel::WriteTranscript()
{
  SaveHistory();
  QJsonObject transcript = m_Conversation.ToJson();

  QJsonObject session;
  switch(m_State)
  {
    case SessionState::Idle: session[lit("state")] = lit("idle"); break;
    case SessionState::Starting: session[lit("state")] = lit("starting"); break;
    case SessionState::Ready: session[lit("state")] = lit("ready"); break;
    case SessionState::NotLoggedIn: session[lit("state")] = lit("notLoggedIn"); break;
    case SessionState::Error: session[lit("state")] = lit("error"); break;
  }
  session[lit("stateMessage")] = m_StateMessage;
  session[lit("instanceId")] = m_InstanceId;
  session[lit("captureSessionId")] = m_CaptureSessionId;
  session[lit("captureTools")] = m_Launch.captureTools;
  session[lit("referenceToken")] = m_ReferenceToken;
  session[lit("historyId")] = m_HistoryRecord.metadata.id;
  session[lit("backend")] = ConfiguredBackend();
  session[lit("historyBackend")] = m_HistoryRecord.backend;
  session[lit("historyReadOnly")] = m_HistoryReadOnly;
  session[lit("historyLoaded")] = m_HistoryLoaded;
  session[lit("historyError")] = m_HistoryError;
  session[lit("historySaveError")] = m_LastSaveError;
  session[lit("sendEnabled")] = m_Send->isEnabled();
  QJsonArray historyEntries;
  QString historyWarning;
  for(const AgentHistoryEntry &entry : m_History->List(historyWarning))
  {
    QJsonObject item;
    item[lit("id")] = entry.id;
    item[lit("backend")] = entry.backend;
    item[lit("threadId")] = entry.threadId;
    item[lit("title")] = entry.title;
    item[lit("mode")] = entry.mode;
    item[lit("captureIdentity")] = entry.captureIdentity;
    historyEntries << item;
  }
  session[lit("historyEntries")] = historyEntries;
  session[lit("historyWarning")] = historyWarning;
  session[lit("accountType")] = m_AccountType;
  session[lit("loginStatus")] = m_LoginStatus;
  session[lit("loginVisible")] = !m_Login->isHidden();
  session[lit("loginEnabled")] = m_Login->isEnabled();
  session[lit("authUrls")] = m_Autotest.authUrls;
  session[lit("models")] = m_Models;
  session[lit("selectedModel")] = m_Launch.model;
  session[lit("selectedEffort")] = m_Launch.reasoningEffort;
  session[lit("mcpStatus")] = m_McpStatus;
  session[lit("mcpTools")] = m_McpTools;
  session[lit("otherServers")] = QJsonArray::fromStringList(m_OtherServers);
  session[lit("isolate")] = m_Launch.isolate;
  session[lit("skillRoots")] = QJsonArray::fromStringList(m_Launch.skillRoots);
  session[lit("threadIds")] = QJsonArray::fromStringList(m_ThreadIds);
  session[lit("appServerPids")] = m_ProcessIds;
  session[lit("appServerRunning")] = m_Client->IsRunning();
  session[lit("serverRequests")] = m_ServerRequests;
  session[lit("turnTexts")] = m_TurnTexts;
  transcript[lit("session")] = session;

  QJsonObject autotest;
  autotest[lit("results")] = m_Autotest.results;
  autotest[lit("elapsedMs")] = double(m_Autotest.clock.elapsed());
  autotest[lit("eventBefore")] = double(m_Autotest.eventBefore);
  autotest[lit("eventAfter")] = double(m_Autotest.eventAfter);
  autotest[lit("asks")] = m_Autotest.askResults;
  autotest[lit("renderedLinks")] = QJsonArray::fromStringList(RenderedLinks(m_Conversation.Items()));
  autotest[lit("linkClicks")] = m_Autotest.linkClicks;
  autotest[lit("expandedTools")] = QJsonArray::fromStringList(m_ExpandedTools.values());
  autotest[lit("renderedHtml")] = m_View->toHtml();
  autotest[lit("settingsDialog")] = m_Autotest.settingsDialog;
  autotest[lit("historyDialog")] = m_Autotest.historyDialog;
  transcript[lit("autotest")] = autotest;

  if(m_Autotest.transcript.isEmpty())
  {
    qInfo().noquote() << QString::fromUtf8(QJsonDocument(transcript).toJson());
    return;
  }

  QString temp = m_Autotest.transcript + lit(".tmp");
  QDir().mkpath(QFileInfo(m_Autotest.transcript).absolutePath());
  QFile file(temp);
  if(!file.open(QIODevice::WriteOnly | QIODevice::Truncate))
  {
    qCritical() << "AI panel autotest: cannot write" << temp;
    return;
  }
  file.write(QJsonDocument(transcript).toJson());
  file.close();
  QFile::remove(m_Autotest.transcript);
  QFile::rename(temp, m_Autotest.transcript);
  qInfo() << "AI panel autotest transcript written to" << m_Autotest.transcript;
}

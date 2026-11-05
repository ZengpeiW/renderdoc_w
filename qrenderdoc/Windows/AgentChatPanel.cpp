#include "AgentChatPanel.h"
#include <QCoreApplication>
#include <QDesktopServices>
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
#include <QTimer>
#include <QToolButton>
#include <QUrl>
#include <QVBoxLayout>
#include "Code/Agent/AgentMarkdown.h"
#include "Code/Agent/CodexAppServerClient.h"
#include "Code/Agent/RenderTestAgentIntegration.h"
#include "Code/CaptureContext.h"
#include "Windows/TextureViewer.h"
#include "flowlayout/FlowLayout.h"

namespace
{
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
    QRegularExpressionMatchIterator it = href.globalMatch(AgentMarkdown::ToHtml(item.text));
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
  setWindowTitle(tr("AI Assistant"));

  m_Status = new QLabel(this);
  m_Status->setTextFormat(Qt::RichText);
  m_Status->setWordWrap(true);

  m_NewChat = new QPushButton(tr("New Chat"), this);
  m_NewChat->setToolTip(tr("Discard this conversation and start a new one"));

  m_View = new QTextBrowser(this);
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
  top->addWidget(m_NewChat);

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

  m_Client = new CodexAppServerClient(this);
  connect(m_Client, &CodexAppServerClient::notificationReceived, this,
          &AgentChatPanel::OnNotification);
  connect(m_Client, &CodexAppServerClient::serverRequestAnswered, this,
          &AgentChatPanel::OnServerRequest);
  connect(m_Client, &CodexAppServerClient::processExited, this, &AgentChatPanel::OnProcessExited);

  connect(m_Send, &QPushButton::clicked, this, &AgentChatPanel::Send);
  connect(m_StopButton, &QPushButton::clicked, this, &AgentChatPanel::Stop);
  connect(m_NewChat, &QPushButton::clicked, this, &AgentChatPanel::NewChat);
  connect(m_View, &QTextBrowser::anchorClicked, this, &AgentChatPanel::OpenLink);

  m_Ctx.AddCaptureViewer(this);

  Render();
  UpdateChips();
  UpdateControls();
}

AgentChatPanel::~AgentChatPanel()
{
  m_Client->disconnect(this);
  m_Client->Kill();

  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
}

void AgentChatPanel::OnCaptureLoaded()
{
  m_Chips.clear();
  m_IncludeEvent = true;
  UpdateChips();
  if(!m_Conversation.Items().isEmpty())
  {
    m_Conversation.AddNotice(lit("info"), tr("A capture was loaded. Earlier answers may refer to "
                                             "another capture; use New Chat to start fresh."));
    ScheduleRender();
  }
}

void AgentChatPanel::OnCaptureClosed()
{
  m_Chips.clear();
  UpdateChips();
  if(!m_Conversation.Items().isEmpty())
  {
    m_Conversation.AddNotice(lit("info"), tr("The capture was closed."));
    ScheduleRender();
  }
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
  if(text.isEmpty() || m_Conversation.TurnRunning())
    return;
  m_Input->clear();
  SendText(text);
}

void AgentChatPanel::SendText(const QString &text)
{
  QList<AgentContextChip> context = ContextSnapshot();
  QJsonArray contextJson;
  for(const AgentContextChip &chip : context)
    contextJson << chip.ToJson();

  m_Conversation.AddUserMessage(text, contextJson);
  m_PendingText = BuildTurnText(text, context);
  m_Chips.clear();
  m_Prefill.clear();
  UpdateChips();
  m_Conversation.BeginTurn();
  m_QuestionActive = true;
  m_StopRequested = false;

  if(m_State == SessionState::Ready && m_Client->IsRunning())
  {
    if(m_Conversation.ThreadId().isEmpty())
      BeginThread(m_Generation);
    else
      StartTurn();
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
  RenderTestAgentIntegration *integration = m_Ctx.AgentIntegration();
  QJsonObject manifest = integration ? integration->ReadManifest() : QJsonObject();
  const bool loaded = m_Ctx.IsCaptureLoaded();

  QStringList lines;
  lines << lit("<rendertest_context>");
  QString instance = integration ? integration->InstanceId() : QString();
  lines << lit("instanceId: %1").arg(instance.isEmpty() ? lit("unavailable") : instance);
  lines << lit("captureLoaded: %1").arg(loaded ? lit("true") : lit("false"));
  if(loaded)
  {
    QString session = manifest[lit("captureSessionId")].toString();
    if(!session.isEmpty())
      lines << lit("captureSessionId: %1").arg(session);
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

  RenderTestAgentIntegration *integration = m_Ctx.AgentIntegration();
  m_InstanceId = integration ? integration->InstanceId() : QString();
  if(m_InstanceId.isEmpty())
  {
    m_Conversation.AddNotice(
        lit("warning"),
        tr("The RenderTest agent bridge is not running in this window, so capture tools are "
           "unavailable. Unset RENDERTEST_AGENT_BRIDGE=0 and restart to enable them."));
  }

  m_Launch = AgentLaunchConfig::Resolve(m_InstanceId, RenderTestAgentIntegration::AgentRoot());
  if(!m_Launch.error.isEmpty())
  {
    SessionFailed(SessionState::Error, m_Launch.error);
    return;
  }

  m_Account.clear();
  m_AccountType.clear();
  m_McpStatus.clear();
  m_McpTools = -1;
  m_OtherServers.clear();

  m_Client->SetLogPath(LogPath());
  QString error;
  if(!m_Client->Start(m_Launch.program, m_Launch.arguments, m_Launch.workingDir, error))
  {
    SessionFailed(SessionState::Error, error);
    return;
  }
  m_ProcessIds << double(m_Client->ProcessId());
  SetState(SessionState::Starting, tr("Starting Codex..."));

  QJsonObject clientInfo;
  clientInfo[lit("name")] = lit("qrendertest");
  clientInfo[lit("title")] = lit("RenderTest");
  clientInfo[lit("version")] = QCoreApplication::applicationVersion();
  QJsonObject capabilities;
  capabilities[lit("experimentalApi")] = true;
  QJsonObject params;
  params[lit("clientInfo")] = clientInfo;
  params[lit("capabilities")] = capabilities;

  m_Client->Request(lit("initialize"), params, InitializeTimeoutMs,
                    [this, generation](const QJsonValue &, const QJsonObject &error) {
                      if(generation != m_Generation)
                        return;
                      if(!error.isEmpty())
                      {
                        SessionFailed(
                            SessionState::Error,
                            tr("Codex app-server did not initialize: %1").arg(ErrorMessage(error)));
                        return;
                      }
                      m_Client->Notify(lit("initialized"), QJsonObject());
                      ReadAccount(generation);
                    });
}

void AgentChatPanel::ReadAccount(int generation)
{
  QJsonObject params;
  params[lit("refreshToken")] = false;
  m_Client->Request(
      lit("account/read"), params, AccountTimeoutMs,
      [this, generation](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;
        if(!error.isEmpty())
        {
          SessionFailed(SessionState::Error,
                        tr("Could not read the Codex account: %1").arg(ErrorMessage(error)));
          return;
        }
        QJsonObject data = result.toObject();
        QJsonObject account = data[lit("account")].toObject();
        if(account.isEmpty() && data[lit("requiresOpenaiAuth")].toBool(true))
        {
          SessionFailed(SessionState::NotLoggedIn,
                        tr("Codex is not logged in. Run `codex login` in a terminal, then send "
                           "the message again."));
          return;
        }
        m_AccountType = account[lit("type")].toString();
        m_Account = account[lit("email")].toString();
        if(m_Account.isEmpty())
          m_Account = m_AccountType.isEmpty() ? tr("custom provider") : m_AccountType;
        SetSkillRoots(generation);
      });
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
  m_Client->Request(lit("skills/extraRoots/set"), params, ConfigTimeoutMs,
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
      lit("config/read"), params, ConfigTimeoutMs,
      [this, generation](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;
        QStringList disabled;
        if(error.isEmpty())
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
          m_Conversation.AddNotice(lit("warning"), tr("Could not read the Codex config to isolate "
                                                      "other MCP servers: %1")
                                                       .arg(ErrorMessage(error)));
        }
        StartThread(generation, disabled);
      });
}

void AgentChatPanel::StartThread(int generation, const QStringList &disabledServers)
{
  QJsonObject config;
  for(const QString &name : disabledServers)
    config[lit("mcp_servers.%1.enabled").arg(name)] = false;

  QJsonObject params;
  params[lit("cwd")] = m_Launch.workingDir;
  params[lit("sandbox")] = lit("read-only");
  params[lit("approvalPolicy")] = lit("never");
  params[lit("ephemeral")] = true;
  params[lit("serviceName")] = lit("qrendertest");
  params[lit("developerInstructions")] = AgentLaunchConfig::DeveloperInstructions(m_InstanceId);
  if(!config.isEmpty())
    params[lit("config")] = config;

  m_Client->Request(lit("thread/start"), params, ThreadTimeoutMs,
                    [this, generation](const QJsonValue &result, const QJsonObject &error) {
                      if(generation != m_Generation)
                        return;
                      if(!error.isEmpty())
                      {
                        SessionFailed(
                            SessionState::Error,
                            tr("Could not start a Codex thread: %1").arg(ErrorMessage(error)));
                        return;
                      }
                      QJsonObject data = result.toObject();
                      QString sandbox = data[lit("sandbox")].toObject()[lit("type")].toString();
                      QString approval = data[lit("approvalPolicy")].toString();
                      if(sandbox != lit("readOnly") || approval != lit("never"))
                      {
                        SessionFailed(SessionState::Error,
                                      tr("Codex did not accept the read-only sandbox (sandbox=%1, "
                                         "approvalPolicy=%2); refusing to chat.")
                                          .arg(sandbox.isEmpty() ? lit("?") : sandbox,
                                               approval.isEmpty() ? lit("?") : approval));
                        return;
                      }
                      QString threadId = data[lit("thread")].toObject()[lit("id")].toString();
                      if(m_HadThread && !m_Conversation.Items().isEmpty())
                        m_Conversation.AddNotice(
                            lit("info"), tr("Started a new Codex thread; earlier messages in this "
                                            "panel are not visible to the model."));
                      m_HadThread = true;
                      m_Conversation.SetThread(threadId, data[lit("model")].toString());
                      m_ThreadIds << threadId;
                      PollMcpStatus(generation, 0);
                    });
}

void AgentChatPanel::PollMcpStatus(int generation, int attempt)
{
  QJsonObject params;
  params[lit("threadId")] = m_Conversation.ThreadId();
  params[lit("detail")] = lit("toolsAndAuthOnly");
  params[lit("limit")] = 100;
  m_Client->Request(
      lit("mcpServerStatus/list"), params, ConfigTimeoutMs,
      [this, generation, attempt](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;

        const QString server = QString::fromLatin1(AgentLaunchConfig::ServerName);
        QString status = lit("unknown");
        QString toolsError;
        int tools = -1;
        QStringList others;
        if(error.isEmpty())
        {
          for(const QJsonValue &value : result.toObject()[lit("data")].toArray())
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

        if((status == lit("starting") || status == lit("notStarted")) && attempt + 1 < McpPollAttempts)
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
        if(!error.isEmpty())
          m_Conversation.AddNotice(
              lit("warning"), tr("Could not query MCP server status: %1").arg(ErrorMessage(error)));
        else if(status != lit("connected"))
          m_Conversation.AddNotice(
              lit("warning"),
              tr("The rendertest MCP server is %1%2; capture tools may be "
                 "unavailable.")
                  .arg(status, toolsError.isEmpty() ? QString() : lit(": ") + toolsError));
        if(m_Launch.isolate && !others.isEmpty())
          m_Conversation.AddNotice(
              lit("warning"),
              tr("Other MCP servers are still enabled: %1").arg(others.join(lit(", "))));

        SetState(SessionState::Ready);
        ScheduleRender();
        if(m_Conversation.TurnRunning() && !m_PendingText.isEmpty())
          StartTurn();
      });
}

void AgentChatPanel::StartTurn()
{
  const int generation = m_Generation;
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

  m_Client->Request(
      lit("turn/start"), params, TurnTimeoutMs,
      [this, generation](const QJsonValue &result, const QJsonObject &error) {
        if(generation != m_Generation)
          return;
        if(!error.isEmpty())
        {
          m_Conversation.EndTurn(lit("failed"),
                                 tr("Could not start the turn: %1").arg(ErrorMessage(error)));
          QuestionFinished();
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
  ScheduleRender();
  QuestionFinished();
}

void AgentChatPanel::KillSession()
{
  m_Generation++;
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
  m_Client->Request(lit("turn/interrupt"), params, StopGraceMs,
                    CodexAppServerClient::ResponseCallback());
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
                         tr("Stopped. Codex was restarted, so the next message starts a new "
                            "thread."));
  ScheduleRender();
  UpdateControls();
  QuestionFinished();
}

void AgentChatPanel::NewChat()
{
  if(m_Conversation.TurnRunning())
    return;
  m_Conversation.Clear();
  m_HadThread = false;
  m_IncludeEvent = true;
  UpdateChips();
  ScheduleRender();
  UpdateControls();
}

void AgentChatPanel::OnNotification(const QString &method, const QJsonObject &params)
{
  bool changed = m_Conversation.Apply(method, params);

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
      tr("The Codex app-server %1 (code %2).").arg(killed ? lit("was killed") : reason).arg(exitCode);
  QString tail = LastLines(m_Client->StderrTail(), 8);
  if(!tail.isEmpty())
    message += lit("\n") + tail;
  message += lit("\n") + tr("The next message restarts it. Log: %1").arg(LogPath());

  m_Generation++;
  m_StopTimer->stop();
  m_Conversation.SetThread(QString(), m_Conversation.Model());
  m_PendingText.clear();
  SetState(SessionState::Error, tr("Codex app-server stopped"));
  if(m_Conversation.TurnRunning())
    m_Conversation.EndTurn(lit("failed"), message);
  else
    m_Conversation.AddNotice(lit("error"), message);
  ScheduleRender();
  QuestionFinished();
}

void AgentChatPanel::QuestionFinished()
{
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
  m_Send->setEnabled(!running);
  m_StopButton->setEnabled(running);
  m_NewChat->setEnabled(!running);

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
      state = tr("Not logged in");
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
  if(!m_RenderTimer->isActive())
    m_RenderTimer->start();
}

QString AgentChatPanel::RenderItem(const AgentItem &item) const
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
          .arg(tr("AI"), AgentMarkdown::ToHtml(item.text));
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
      QString mark = Glyph(0x2026);
      QString color = lit("#c08000");
      if(item.status == lit("completed"))
      {
        mark = Glyph(0x2713);
        color = lit("#2a9d2a");
      }
      else if(item.status == lit("failed") || item.status == lit("interrupted") ||
              !item.error.isEmpty())
      {
        mark = Glyph(0x2717);
        color = lit("#c03030");
      }
      QString args = item.arguments;
      if(args == lit("{}"))
        args.clear();
      if(args.size() > 90)
        args = args.left(90) + Glyph(0x2026);
      QString duration = item.durationMs >= 0
                             ? lit("%1s").arg(double(item.durationMs) / 1000.0, 0, 'f', 1)
                             : QString();
      QString detail = !item.error.isEmpty() ? item.error : item.progress;
      if(detail.size() > 160)
        detail = detail.left(160) + Glyph(0x2026);
      QString html = lit("<p style=\"font-family: Consolas, monospace; margin:2px;\">%1 <b>%2</b> "
                         "<span style=\"color:gray;\">%3</span> %4 "
                         "<span style=\"color:%5;\">%6</span>")
                         .arg(Glyph(0x25B8), item.tool.toHtmlEscaped(), args.toHtmlEscaped(),
                              duration, color, mark);
      if(!detail.isEmpty())
        html += lit("<br/><span style=\"color:gray;\">%1</span>").arg(detail.toHtmlEscaped());
      return html + lit("</p>");
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
  if(m_Conversation.Items().isEmpty())
  {
    html = lit("<p style=\"color:gray;\">%1</p>")
               .arg(tr("Ask a question about the open capture. The assistant reads it through the "
                       "rendertest tools of this window and cannot change files.")
                        .toHtmlEscaped());
  }

  for(const AgentItem &item : m_Conversation.Items())
    html += RenderItem(item);

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
  bar->setValue(atBottom ? bar->maximum() : previous);
}

void AgentChatPanel::RunAutotest(const QJsonObject &config)
{
  m_Autotest = Autotest();
  m_Autotest.active = true;
  for(const QJsonValue &question : config[lit("questions")].toArray())
    m_Autotest.questions << question.toString();
  m_Autotest.asks = config[lit("asks")].toArray();
  m_Autotest.clickLinks = config[lit("clickLinks")].toArray();
  m_Autotest.transcript = config[lit("transcript")].toString();
  m_Autotest.stopOnTool = config[lit("stopOnTool")].toString();
  m_Autotest.stopAfterMs = config[lit("stopAfterMs")].toInt(0);
  m_Autotest.stopQuestion = config[lit("stopQuestion")].toInt(0);
  m_Autotest.waitForCapture = config[lit("waitForCapture")].toBool(false);
  m_Autotest.captureTimeoutMs = config[lit("captureTimeoutMs")].toInt(180000);
  m_Autotest.selectEvent = qint64(config[lit("selectEvent")].toDouble(-1.0));
  m_Autotest.clock.start();

  qInfo() << "AI panel autotest:" << m_Autotest.questions.size() << "questions";
  AutotestNext();
}

void AgentChatPanel::AutotestNext()
{
  if(!m_Autotest.active)
    return;

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

  if(!m_Autotest.prepared)
  {
    m_Autotest.prepared = true;
    if(m_Autotest.selectEvent >= 0 && m_Ctx.IsCaptureLoaded())
      m_Ctx.SetEventID({}, uint32_t(m_Autotest.selectEvent), uint32_t(m_Autotest.selectEvent));
    m_Autotest.eventBefore = m_Ctx.IsCaptureLoaded() ? qint64(m_Ctx.CurEvent()) : -1;
  }

  if(m_Autotest.index >= m_Autotest.questions.size())
  {
    AutotestClickLinks();
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

    QJsonObject click;
    click[lit("requested")] = value.toString();
    click[lit("link")] = link;
    click[lit("ok")] = !link.isEmpty() && OpenLink(QUrl(link));
    click[lit("eventAfter")] = m_Ctx.IsCaptureLoaded() ? double(m_Ctx.CurEvent()) : -1.0;
    m_Autotest.linkClicks << click;
  }

  WriteTranscript();
  m_Autotest.active = false;
}

void AgentChatPanel::WriteTranscript()
{
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
  session[lit("accountType")] = m_AccountType;
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

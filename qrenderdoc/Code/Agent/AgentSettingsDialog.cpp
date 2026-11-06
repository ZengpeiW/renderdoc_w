#include "AgentSettingsDialog.h"
#include <QCheckBox>
#include <QComboBox>
#include <QCoreApplication>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFileInfo>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QProcessEnvironment>
#include <QPushButton>
#include <QStandardPaths>
#include <QTimer>
#include <QVBoxLayout>
#include "AgentLaunchConfig.h"
#include "CodexAppServerClient.h"
#include "Code/Interface/PersistentConfig.h"
#include "Code/QRDUtils.h"

namespace
{
QString Translate(const char *text)
{
  return QCoreApplication::translate("AgentSettingsDialog", text);
}

QString ComboValue(QComboBox *combo)
{
  int index = combo->currentIndex();
  if(index >= 0 && combo->currentText() == combo->itemText(index))
    return combo->itemData(index).toString();
  return combo->currentText().trimmed();
}

bool ExecutableExists(const QString &path)
{
  if(path.isEmpty())
    return true;
  QString found = QFileInfo(path).isAbsolute() ? path : QStandardPaths::findExecutable(path);
  return !found.isEmpty() && QFileInfo(found).isFile();
}

// Model discovery is independent of a capture or chat. In particular, opening Settings must not
// require sending a question, starting a thread, or finding the MCP Python environment first.
class ModelCatalog : public QObject
{
public:
  typedef std::function<void(const QString &, const QJsonArray &, const QString &)> Update;
  ModelCatalog(QObject *parent, Update update) : QObject(parent), m_Update(update)
  {
    m_Client = new CodexAppServerClient(this);
    connect(m_Client, &CodexAppServerClient::processExited, this,
            [this](int, bool, const QString &reason) {
      if(m_Loading)
        Fail(Translate("Codex stopped while loading models: %1").arg(reason));
    });
  }
  ~ModelCatalog() { Cancel(); }

  void Cancel()
  {
    ++m_Generation;
    m_Loading = false;
    m_Client->Kill();
  }

  void Start(const QString &codexPath)
  {
    Cancel();
    const int generation = m_Generation;
    PersistentConfig draft;
    draft.Agent_CodexPath = codexPath.trimmed();
    draft.Agent_IsolateMcpServers = true;
    AgentLaunchConfig launch = AgentLaunchConfig::Resolve(QString(), QString(), QString(), draft);
    if(!launch.error.isEmpty())
    {
      Fail(launch.error);
      return;
    }
    launch.arguments << lit("-c") << lit("mcp_servers={}");
    m_Loading = true;
    m_Update(lit("starting"), QJsonArray(), Translate("Loading models from Codex..."));
    QString error;
    if(!m_Client->Start(launch.program, launch.arguments, launch.workingDir, error))
    {
      if(generation == m_Generation)
        Fail(error);
      return;
    }
    QJsonObject params;
    params[lit("clientInfo")] = QJsonObject({{lit("name"), lit("qrendertest_settings")},
                                             {lit("version"), QCoreApplication::applicationVersion()}});
    params[lit("capabilities")] = QJsonObject({{lit("experimentalApi"), true}});
    m_Client->Request(lit("initialize"), params, 15000,
                      [this, generation](const QJsonValue &, const QJsonObject &error) {
      if(generation != m_Generation)
        return;
      if(!error.isEmpty())
      {
        Fail(error[lit("message")].toString());
        return;
      }
      m_Client->Notify(lit("initialized"), QJsonObject());
      Read(generation, QString(), QJsonArray(), QStringList());
    });
  }

private:
  void Fail(const QString &error)
  {
    Cancel();
    m_Update(lit("error"), QJsonArray(),
             Translate("Could not load models: %1. You can still enter a model name manually.")
                 .arg(error.isEmpty() ? Translate("Unknown error") : error));
  }

  void Read(int generation, const QString &cursor, const QJsonArray &models,
             const QStringList &seen)
  {
    m_Update(lit("loading"), QJsonArray(), Translate("Loading models from Codex..."));
    QJsonObject params;
    params[lit("limit")] = 100;
    if(!cursor.isEmpty())
      params[lit("cursor")] = cursor;
    m_Client->Request(lit("model/list"), params, 15000,
                      [this, generation, models, seen](const QJsonValue &result,
                                                       const QJsonObject &error) {
      if(generation != m_Generation)
        return;
      const QJsonObject data = result.toObject();
      if(!error.isEmpty() || !data[lit("data")].isArray() ||
         (!data[lit("nextCursor")].isNull() && !data[lit("nextCursor")].isString()))
      {
        Fail(error.isEmpty() ? Translate("Invalid model catalog response")
                            : error[lit("message")].toString());
        return;
      }
      QJsonArray all = models;
      for(const QJsonValue &model : data[lit("data")].toArray())
        if(model.isObject() && !model.toObject()[lit("model")].toString().isEmpty())
          all << model;
      const QString next = data[lit("nextCursor")].toString();
      if(all.size() > 10000 || (!next.isEmpty() && (seen.contains(next) || seen.size() >= 99)))
      {
        Fail(Translate("Model catalog pagination did not complete"));
        return;
      }
      if(!next.isEmpty())
      {
        QStringList visited = seen;
        visited << next;
        Read(generation, next, all, visited);
        return;
      }
      Cancel();
      m_Update(lit("ready"), all, all.isEmpty()
                   ? Translate("No models were returned. Enter a model name or follow Codex configuration.")
                   : Translate("Models loaded. You can also enter a custom model name."));
    });
  }

  CodexAppServerClient *m_Client = NULL;
  Update m_Update;
  int m_Generation = 0;
  bool m_Loading = false;
};
}

bool AgentSettingsDialog::Edit(PersistentConfig &settings, const QJsonArray &models, QWidget *parent,
                               bool persist)
{
  QDialog dialog(parent);
  QJsonArray catalog = models;
  dialog.setObjectName(lit("agentSettings"));
  dialog.setWindowTitle(Translate("AI Assistant Settings"));
  dialog.setWindowFlags(dialog.windowFlags() & ~Qt::WindowContextHelpButtonHint);
  dialog.setMinimumWidth(560);

  QVBoxLayout *layout = new QVBoxLayout(&dialog);
  QFormLayout *form = new QFormLayout();
  layout->addLayout(form);

  auto addPath = [&](const QString &label, const QString &name, const QString &value) {
    QWidget *row = new QWidget(&dialog);
    QHBoxLayout *rowLayout = new QHBoxLayout(row);
    rowLayout->setContentsMargins(0, 0, 0, 0);
    QLineEdit *field = new QLineEdit(value, row);
    field->setObjectName(name);
    field->setPlaceholderText(Translate("Automatic detection"));
    QPushButton *browse = new QPushButton(Translate("Browse..."), row);
    rowLayout->addWidget(field, 1);
    rowLayout->addWidget(browse);
    QObject::connect(browse, &QPushButton::clicked, &dialog, [&, field, label]() {
      QString selected = RDDialog::getOpenFileName(&dialog, label, field->text());
      if(!selected.isEmpty())
        field->setText(selected);
    });
    form->addRow(label, row);
    return field;
  };

  QLineEdit *codex = addPath(Translate("Codex executable"), lit("agentCodexPath"),
                            QString(settings.Agent_CodexPath));
  codex->setToolTip(Translate("Choose codex.exe itself, not codex.cmd or codex.ps1."));
  QLineEdit *python = addPath(Translate("MCP Python executable"), lit("agentMcpPython"),
                             QString(settings.Agent_McpPython));
  python->setToolTip(Translate("Choose a Python environment with rendertest_mcp installed."));

  QComboBox *model = new QComboBox(&dialog);
  model->setObjectName(lit("agentModel"));
  model->setEditable(true);
  model->setInsertPolicy(QComboBox::NoInsert);
  model->addItem(Translate("Follow Codex configuration"), QString());
  for(const QJsonValue &value : models)
  {
    QJsonObject descriptor = value.toObject();
    QString name = descriptor[lit("model")].toString();
    if(name.isEmpty() || model->findData(name) >= 0)
      continue;
    model->addItem(name, name);
    QString description = descriptor[lit("displayName")].toString();
    if(!descriptor[lit("description")].toString().isEmpty())
      description += lit("\n") + descriptor[lit("description")].toString();
    model->setItemData(model->count() - 1, description.trimmed(), Qt::ToolTipRole);
  }
  const QString currentModel = QString(settings.Agent_Model).trimmed();
  int modelIndex = model->findData(currentModel);
  if(modelIndex >= 0)
    model->setCurrentIndex(modelIndex);
  else
    model->setEditText(currentModel);
  form->addRow(Translate("Model"), model);

  QWidget *catalogRow = new QWidget(&dialog);
  QHBoxLayout *catalogLayout = new QHBoxLayout(catalogRow);
  catalogLayout->setContentsMargins(0, 0, 0, 0);
  QLabel *catalogStatus = new QLabel(Translate("Loading models from Codex..."), catalogRow);
  catalogStatus->setObjectName(lit("agentModelsStatus"));
  catalogStatus->setProperty("catalogState", lit("starting"));
  catalogStatus->setWordWrap(true);
  catalogStatus->setTextFormat(Qt::PlainText);
  QPushButton *refresh = new QPushButton(Translate("Refresh"), catalogRow);
  refresh->setObjectName(lit("agentModelsRefresh"));
  catalogLayout->addWidget(catalogStatus, 1);
  catalogLayout->addWidget(refresh);
  form->addRow(QString(), catalogRow);

  QComboBox *effort = new QComboBox(&dialog);
  effort->setObjectName(lit("agentReasoningEffort"));
  effort->setEditable(true);
  effort->setInsertPolicy(QComboBox::NoInsert);
  effort->setEditText(QString(settings.Agent_ReasoningEffort).trimmed());
  auto updateEfforts = [model, effort, &catalog](bool preserveCurrent) {
    QString selected = ComboValue(effort);
    QJsonArray supported;
    for(const QJsonValue &value : catalog)
    {
      QJsonObject descriptor = value.toObject();
      if(descriptor[lit("model")].toString() == ComboValue(model))
      {
        supported = descriptor[lit("supportedReasoningEfforts")].toArray();
        break;
      }
    }
    effort->blockSignals(true);
    effort->clear();
    effort->addItem(Translate("Default"), QString());
    for(const QJsonValue &value : supported)
    {
      QJsonObject descriptor = value.toObject();
      QString name = descriptor[lit("reasoningEffort")].toString();
      if(name.isEmpty() || effort->findData(name) >= 0)
        continue;
      effort->addItem(name, name);
      effort->setItemData(effort->count() - 1, descriptor[lit("description")].toString(), Qt::ToolTipRole);
    }
    const bool custom = effort->count() == 1;
    effort->setEditable(custom);
    if(custom)
    {
      const QStringList common = {lit("none"), lit("minimal"), lit("low"), lit("medium"),
                                  lit("high"), lit("xhigh")};
      for(const QString &name : common)
        effort->addItem(name, name);
    }
    int index = effort->findData(selected);
    // A catalog arriving in the background must not erase an in-progress or saved choice.
    // Save validates unlisted efforts for known models and lets the user choose a supported value.
    if(preserveCurrent && !custom && index < 0 && !selected.isEmpty())
    {
      effort->addItem(selected, selected);
      index = effort->count() - 1;
      effort->setItemData(index, Translate("This effort is not listed for the selected model."),
                          Qt::ToolTipRole);
    }
    effort->setCurrentIndex(index >= 0 ? index : 0);
    if(index < 0 && custom)
      effort->setEditText(selected);
    effort->blockSignals(false);
  };
  updateEfforts(true);
  QObject::connect(model, &QComboBox::currentTextChanged, &dialog,
                   [updateEfforts](const QString &) { updateEfforts(false); });
  form->addRow(Translate("Reasoning effort"), effort);

  QCheckBox *isolate = new QCheckBox(Translate("Isolate other MCP servers and optional capabilities"),
                                    &dialog);
  isolate->setObjectName(lit("agentIsolateMcpServers"));
  isolate->setChecked(settings.Agent_IsolateMcpServers);
  isolate->setToolTip(Translate("Shell commands stay disabled in both modes."));
  form->addRow(QString(), isolate);

  QLabel *description = new QLabel(Translate("Changes apply when the assistant reconnects."), &dialog);
  description->setWordWrap(true);
  layout->addWidget(description);
  QStringList overridden;
  QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
  const QStringList names = {lit("RENDERTEST_AGENT_CODEX_COMMAND"), lit("RENDERTEST_MCP_PYTHON"),
                             lit("RENDERTEST_AGENT_MODEL"), lit("RENDERTEST_AGENT_REASONING_EFFORT"),
                             lit("RENDERTEST_AGENT_ISOLATE")};
  for(const QString &name : names)
    if(!env.value(name).trimmed().isEmpty())
      overridden << name;
  if(!overridden.isEmpty())
  {
    QLabel *notice = new QLabel(
        Translate("These environment variables take priority over saved settings: %1")
            .arg(overridden.join(lit(", "))), &dialog);
    notice->setWordWrap(true);
    layout->addWidget(notice);
  }

  QLabel *error = new QLabel(&dialog);
  error->setObjectName(lit("agentSettingsError"));
  error->setWordWrap(true);
  error->setTextFormat(Qt::PlainText);
  error->setStyleSheet(lit("color: #c03030;"));
  error->hide();
  layout->addWidget(error);
  QDialogButtonBox *buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel,
                                                  &dialog);
  layout->addWidget(buttons);
  QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
  QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, [&]() {
    QString codexPath = codex->text().trimmed();
    QString pythonPath = python->text().trimmed();
    QString suffix = QFileInfo(codexPath).suffix().toLower();
    QString message;
    if(suffix == lit("cmd") || suffix == lit("bat") || suffix == lit("ps1"))
      message = Translate("Choose the native Codex executable, not a command or PowerShell shim.");
    else if(!ExecutableExists(codexPath))
      message = Translate("The Codex executable was not found: %1").arg(codexPath);
    else if(!ExecutableExists(pythonPath))
      message = Translate("The MCP Python executable was not found: %1").arg(pythonPath);
    if(message.isEmpty() && !ComboValue(effort).isEmpty())
    {
      for(const QJsonValue &value : catalog)
      {
        const QJsonObject descriptor = value.toObject();
        if(descriptor[lit("model")].toString() != ComboValue(model))
          continue;
        const QJsonArray supported = descriptor[lit("supportedReasoningEfforts")].toArray();
        bool listed = supported.isEmpty();
        for(const QJsonValue &option : supported)
          listed |= option.toObject()[lit("reasoningEffort")].toString() == ComboValue(effort);
        if(!listed)
          message = Translate("This model does not list reasoning effort '%1'. Choose Default or a supported value.")
                        .arg(ComboValue(effort));
        break;
      }
    }
    if(!message.isEmpty())
    {
      error->setText(message);
      error->show();
      return;
    }

    const rdcstr oldCodex = settings.Agent_CodexPath, oldPython = settings.Agent_McpPython;
    const rdcstr oldModel = settings.Agent_Model, oldEffort = settings.Agent_ReasoningEffort;
    const bool oldIsolate = settings.Agent_IsolateMcpServers;
    settings.Agent_CodexPath = codexPath;
    settings.Agent_McpPython = pythonPath;
    settings.Agent_Model = ComboValue(model);
    settings.Agent_ReasoningEffort = ComboValue(effort);
    settings.Agent_IsolateMcpServers = isolate->isChecked();
    if(persist && !settings.Save())
    {
      settings.Agent_CodexPath = oldCodex;
      settings.Agent_McpPython = oldPython;
      settings.Agent_Model = oldModel;
      settings.Agent_ReasoningEffort = oldEffort;
      settings.Agent_IsolateMcpServers = oldIsolate;
      error->setText(Translate("Could not save the settings. Check access to the RenderTest configuration file."));
      error->show();
      return;
    }
    dialog.accept();
  });
  auto replaceCatalog = [model, &catalog, updateEfforts](const QJsonArray &models) {
    const QString selected = ComboValue(model);
    catalog = models;
    model->blockSignals(true);
    model->clear();
    model->addItem(Translate("Follow Codex configuration"), QString());
    for(const QJsonValue &value : catalog)
    {
      const QJsonObject descriptor = value.toObject();
      const QString name = descriptor[lit("model")].toString();
      if(name.isEmpty() || model->findData(name) >= 0)
        continue;
      model->addItem(name, name);
      model->setItemData(model->count() - 1, descriptor[lit("description")].toString(), Qt::ToolTipRole);
    }
    const int index = model->findData(selected);
    if(index >= 0)
      model->setCurrentIndex(index);
    else
      model->setEditText(selected);
    model->blockSignals(false);
    updateEfforts(true);
  };
  ModelCatalog discovery(&dialog, [catalogStatus, replaceCatalog](const QString &state,
                                                                 const QJsonArray &models,
                                                                 const QString &message) {
    if(state == lit("ready"))
      replaceCatalog(models);
    catalogStatus->setProperty("catalogState", state);
    catalogStatus->setText(message);
  });
  QTimer refreshTimer(&dialog);
  refreshTimer.setSingleShot(true);
  refreshTimer.setInterval(300);
  QObject::connect(&refreshTimer, &QTimer::timeout, &dialog,
                   [&]() { discovery.Start(codex->text()); });
  QObject::connect(refresh, &QPushButton::clicked, &dialog, [&]() {
    refreshTimer.stop();
    discovery.Start(codex->text());
  });
  QObject::connect(codex, &QLineEdit::textChanged, &dialog, [&]() {
    discovery.Cancel();
    replaceCatalog(QJsonArray());
    catalogStatus->setProperty("catalogState", lit("starting"));
    catalogStatus->setText(Translate("Loading models from Codex..."));
    refreshTimer.start(300);
  });
  // Let the dialog paint before starting the discovery process.
  refreshTimer.start(0);
  const bool accepted = RDDialog::show(&dialog) == QDialog::Accepted;
  refreshTimer.stop();
  discovery.Cancel();    // Kill/fail callbacks while all captured dialog state is still alive.
  return accepted;
}

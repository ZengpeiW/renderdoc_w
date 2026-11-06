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
#include <QVBoxLayout>
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
}

bool AgentSettingsDialog::Edit(PersistentConfig &settings, const QJsonArray &models, QWidget *parent,
                               bool persist)
{
  QDialog dialog(parent);
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

  QComboBox *effort = new QComboBox(&dialog);
  effort->setObjectName(lit("agentReasoningEffort"));
  effort->setEditable(true);
  effort->setInsertPolicy(QComboBox::NoInsert);
  effort->setEditText(QString(settings.Agent_ReasoningEffort).trimmed());
  auto updateEfforts = [model, effort, models]() {
    QString selected = ComboValue(effort);
    QJsonArray supported;
    for(const QJsonValue &value : models)
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
    effort->setCurrentIndex(index >= 0 ? index : 0);
    if(index < 0 && custom)
      effort->setEditText(selected);
    effort->blockSignals(false);
  };
  updateEfforts();
  QObject::connect(model, &QComboBox::currentTextChanged, &dialog,
                   [updateEfforts](const QString &) { updateEfforts(); });
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
  return RDDialog::show(&dialog) == QDialog::Accepted;
}

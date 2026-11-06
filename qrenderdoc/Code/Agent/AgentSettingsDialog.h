#pragma once

#include <QJsonArray>

class PersistentConfig;
class QWidget;

struct AgentSettingsDialog
{
  // Returns true after settings have been accepted. No changes are made on cancellation.
  // Tests can edit an independent configuration copy without writing it to disk.
  // Model/effort drafts are kept separately for each backend while switching the selector.
  static bool Edit(PersistentConfig &settings, const QJsonArray &models, QWidget *parent,
                   bool persist = true);
};

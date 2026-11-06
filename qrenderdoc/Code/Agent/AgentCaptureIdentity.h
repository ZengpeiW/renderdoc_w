#pragma once

#include <QJsonObject>
#include <QString>
#include <atomic>

// A disk identity for reopening a local saved capture. Session IDs identify live bridge state,
// so they cannot establish identity across restarts. Call Inspect on a worker: it hashes the whole
// file. The optional cancellation flag must remain alive until Inspect returns.
namespace AgentCaptureIdentity
{
QJsonObject NoCapture();
QJsonObject Inspect(const QString &filename, bool local, bool temporary,
                    const std::atomic<bool> *cancelled = NULL);
bool Matches(const QJsonObject &saved, const QJsonObject &current, QString *reason = NULL);
// Cheap preflight for an already verified local-file identity. Does not rehash file contents;
// history restoration still requires Inspect + Matches on a worker.
bool FileUnchanged(const QJsonObject &identity, const QString &filename);
}

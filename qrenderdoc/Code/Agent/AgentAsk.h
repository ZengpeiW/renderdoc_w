#pragma once

#include <QJsonObject>
#include <QString>
#include "Code/Interface/QRDInterface.h"

// One piece of UI state attached to an AI panel message, e.g. the pixel picked in the texture
// viewer. selection uses the argument names of the rendertest MCP tools.
struct AgentContextChip
{
  QString kind;
  QString label;
  QJsonObject selection;

  QJsonObject ToJson() const;
  static AgentContextChip FromJson(const QJsonObject &json);
};

struct AgentAskRequest
{
  AgentContextChip chip;
  QString question;
};

// "Ask AI" entry points used by the built-in viewers. Request() shows the AI panel, attaches the
// chip and prefills the editable question without sending it.
namespace AgentAsk
{
QString ResourceIdNumber(ResourceId id);
ResourceId ResourceIdFromNumber(qulonglong number);

AgentAskRequest Event(ICaptureContext &ctx, uint32_t eventId);
AgentAskRequest Resource(ICaptureContext &ctx, ResourceId id, uint32_t eventId);
AgentAskRequest Pixel(ICaptureContext &ctx, ResourceId id, uint32_t x, uint32_t y, uint32_t mip,
                      uint32_t slice, uint32_t sample, uint32_t view, uint32_t eventId);
AgentAskRequest Vertex(ICaptureContext &ctx, uint32_t eventId, uint32_t vertex, uint32_t index,
                       uint32_t instance, uint32_t view, const QString &stage);

void Request(ICaptureContext &ctx, const AgentAskRequest &request);
}

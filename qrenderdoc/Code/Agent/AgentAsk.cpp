#include "AgentAsk.h"
#include <string.h>
#include <QCoreApplication>
#include "Code/CaptureContext.h"
#include "Windows/AgentChatPanel.h"

namespace
{
QString Translate(const char *text)
{
  return QCoreApplication::translate("AgentAsk", text);
}

QString ResourceName(ICaptureContext &ctx, ResourceId id)
{
  return QString(ctx.GetResourceName(id));
}

QString ResourceKind(ICaptureContext &ctx, ResourceId id)
{
  if(ctx.GetTexture(id))
    return lit("texture");
  if(ctx.GetBuffer(id))
    return lit("buffer");
  return lit("other");
}
}

QJsonObject AgentContextChip::ToJson() const
{
  QJsonObject json;
  json[lit("kind")] = kind;
  json[lit("label")] = label;
  json[lit("selection")] = selection;
  return json;
}

AgentContextChip AgentContextChip::FromJson(const QJsonObject &json)
{
  AgentContextChip chip;
  chip.selection = json[lit("selection")].toObject();
  chip.kind = json[lit("kind")].toString(chip.selection[lit("kind")].toString());
  chip.label = json[lit("label")].toString(chip.kind);
  if(!chip.selection.contains(lit("kind")))
    chip.selection[lit("kind")] = chip.kind;
  return chip;
}

QString AgentAsk::ResourceIdNumber(ResourceId id)
{
  qulonglong number = 0;
  static_assert(sizeof(id) == sizeof(number), "ResourceId is expected to wrap a uint64");
  memcpy(&number, &id, sizeof(number));
  return QString::number(number);
}

ResourceId AgentAsk::ResourceIdFromNumber(qulonglong number)
{
  ResourceId id;
  memcpy(&id, &number, sizeof(id));
  return id;
}

AgentAskRequest AgentAsk::Event(ICaptureContext &ctx, uint32_t eventId)
{
  AgentAskRequest request;
  request.chip.kind = lit("event");
  request.chip.label = Translate("EID %1").arg(eventId);
  const ActionDescription *action = ctx.GetAction(eventId);
  if(action)
  {
    QString name = QString(action->customName);
    if(!name.isEmpty())
      request.chip.label += lit(" ") + name;
  }
  request.chip.selection[lit("kind")] = request.chip.kind;
  request.chip.selection[lit("event_id")] = double(eventId);
  request.question = Translate("What does this event do, and what are its inputs and outputs?");
  return request;
}

AgentAskRequest AgentAsk::Resource(ICaptureContext &ctx, ResourceId id, uint32_t eventId)
{
  AgentAskRequest request;
  QString name = ResourceName(ctx, id);
  request.chip.kind = lit("resource");
  request.chip.label = name;
  request.chip.selection[lit("kind")] = request.chip.kind;
  request.chip.selection[lit("resource_id")] = ResourceIdNumber(id);
  request.chip.selection[lit("name")] = name;
  request.chip.selection[lit("resource_type")] = ResourceKind(ctx, id);
  request.chip.selection[lit("event_id")] = double(eventId);
  request.question = Translate("What is this resource, and how is it used at this event?");
  return request;
}

AgentAskRequest AgentAsk::Pixel(ICaptureContext &ctx, ResourceId id, uint32_t x, uint32_t y,
                                uint32_t mip, uint32_t slice, uint32_t sample, uint32_t view,
                                uint32_t eventId)
{
  AgentAskRequest request;
  QString name = ResourceName(ctx, id);
  request.chip.kind = lit("pixel");
  request.chip.label = Translate("Pixel (%1, %2) of %3").arg(x).arg(y).arg(name);
  QJsonObject &sel = request.chip.selection;
  sel[lit("kind")] = request.chip.kind;
  sel[lit("resource_id")] = ResourceIdNumber(id);
  sel[lit("name")] = name;
  sel[lit("x")] = double(x);
  sel[lit("y")] = double(y);
  sel[lit("origin")] = lit("top-left");
  sel[lit("mip")] = double(mip);
  sel[lit("slice")] = double(slice);
  sel[lit("sample")] = double(sample);
  sel[lit("view")] = double(view);
  sel[lit("event_id")] = double(eventId);
  request.question = Translate("Why does this pixel have this value? Which event wrote it?");
  return request;
}

AgentAskRequest AgentAsk::Vertex(ICaptureContext &ctx, uint32_t eventId, uint32_t vertex,
                                 uint32_t index, uint32_t instance, uint32_t view,
                                 const QString &stage)
{
  Q_UNUSED(ctx);
  AgentAskRequest request;
  request.chip.kind = lit("vertex");
  request.chip.label =
      Translate("Vertex %1 (index %2) of EID %3").arg(vertex).arg(index).arg(eventId);
  QJsonObject &sel = request.chip.selection;
  sel[lit("kind")] = request.chip.kind;
  sel[lit("event_id")] = double(eventId);
  sel[lit("vertex")] = double(vertex);
  sel[lit("index")] = double(index);
  sel[lit("instance")] = double(instance);
  sel[lit("view")] = double(view);
  sel[lit("stage")] = stage;
  request.question = Translate("How is this vertex transformed? Are its outputs reasonable?");
  return request;
}

void AgentAsk::Request(ICaptureContext &ctx, const AgentAskRequest &request)
{
  // the AI panel is a C++-only built-in window, so it is reached through the concrete context
  CaptureContext &context = static_cast<CaptureContext &>(ctx);
  context.ShowAgentChatPanel();
  context.GetAgentChatPanel()->Ask(request);
}

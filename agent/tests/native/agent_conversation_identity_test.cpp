// Standalone Qt Core test. Uses temporary files, no capture replay and no model service.
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QFile>
#include <QJsonDocument>
#include <QTemporaryDir>
#include <cstdio>
#include <ctime>
#if defined(Q_OS_WIN32)
#include <io.h>
#include <windows.h>
#else
#include <utime.h>
#endif
#include "AgentConversation.h"
#include "AgentCaptureIdentity.h"

static int failures = 0;
static void check(bool value, const char *name)
{
  if(!value) { std::fprintf(stderr, "FAIL %s\n", name); failures++; }
}
int main(int argc, char **argv)
{
  QCoreApplication app(argc, argv);
  QTemporaryDir root;
  check(root.isValid(), "test directory");
  QString path = root.path() + lit("/capture.rdc");
  QByteArray data(5 * 1024 * 1024, 'a');
  QFile file(path); check(file.open(QIODevice::ReadWrite), "open file");
  file.write(data); file.flush();
#if defined(Q_OS_WIN32)
  FILETIME writeTime = {}; GetFileTime((HANDLE)_get_osfhandle(file.handle()), NULL, NULL, &writeTime);
  file.close();
#else
  file.close();
  struct utimbuf writeTime = {};
  writeTime.actime = writeTime.modtime = std::time(NULL) - 100;
  check(utime(QFile::encodeName(path).constData(), &writeTime) == 0, "set timestamp fixture");
#endif
  QJsonObject first = AgentCaptureIdentity::Inspect(path, true, false);
  if(!first[lit("verified")].toBool())
    std::fprintf(stderr, "%s\n", QJsonDocument(first).toJson(QJsonDocument::Compact).constData());
  check(first[lit("verified")].toBool(), "full identity verified");
  check(first[lit("sha256")].toString() == QString::fromLatin1(QCryptographicHash::hash(data, QCryptographicHash::Sha256).toHex()), "full SHA256");
  QString reason;
  check(AgentCaptureIdentity::Matches(first, AgentCaptureIdentity::Inspect(path, true, false), &reason), "reopen same file");
  check(AgentCaptureIdentity::FileUnchanged(first, path), "fast preflight accepts canonical/short path");
  file.open(QIODevice::ReadWrite); file.seek(2 * 1024 * 1024 + 123); file.write("b", 1); file.flush();
#if defined(Q_OS_WIN32)
  check(SetFileTime((HANDLE)_get_osfhandle(file.handle()), NULL, NULL, &writeTime) != 0, "restore modified time");
  file.close();
#else
  file.close();
  check(utime(QFile::encodeName(path).constData(), &writeTime) == 0, "restore modified time");
#endif
  QJsonObject changed = AgentCaptureIdentity::Inspect(path, true, false);
  check(first[lit("bytes")] == changed[lit("bytes")] && first[lit("modifiedUnixMs")] == changed[lit("modifiedUnixMs")], "same path size and mtime fixture");
  check(!AgentCaptureIdentity::Matches(first, changed, &reason), "full hash detects internal replacement");
  check(!reason.isEmpty(), "mismatch explains read-only");
  check(AgentCaptureIdentity::FileUnchanged(first, path), "stat preflight is explicitly not a rehash");
  std::atomic<bool> cancelled(true);
  check(!AgentCaptureIdentity::Inspect(path, true, false, &cancelled)[lit("verified")].toBool(), "cancelled hash");
  check(!AgentCaptureIdentity::Inspect(path, false, false)[lit("verified")].toBool(), "remote read-only");
  check(!AgentCaptureIdentity::Inspect(path, true, true)[lit("verified")].toBool(), "temporary read-only");
  check(!AgentCaptureIdentity::Inspect(root.path() + lit("/missing.rdc"), true, false)[lit("verified")].toBool(), "missing read-only");
  check(AgentCaptureIdentity::Matches(AgentCaptureIdentity::NoCapture(), AgentCaptureIdentity::NoCapture()), "pure chat matches pure chat");
  check(!AgentCaptureIdentity::Matches(first, AgentCaptureIdentity::NoCapture()), "capture cannot resume as pure chat");
  check(!AgentCaptureIdentity::FileUnchanged(first, root.path() + lit("/missing.rdc")), "missing preflight rejected");
  QJsonObject forged = first; forged[lit("sha256")] = lit("not-a-hash");
  check(!AgentCaptureIdentity::Matches(forged, forged), "invalid identity never matches itself");

  AgentConversation original;
  original.SetThread(lit("thread-1"), lit("model-1"));
  QJsonObject chip; chip[lit("label")] = lit("EID 5"); chip[lit("kind")] = lit("event");
  original.AddUserMessage(lit("question"), QJsonArray({chip}));
  original.BeginTurn(lit("source-token"));
  QJsonObject tool;
  tool[lit("type")] = lit("mcpToolCall"); tool[lit("id")] = lit("item-1");
  tool[lit("server")] = lit("rendertest"); tool[lit("tool")] = lit("get_context");
  tool[lit("status")] = lit("inProgress"); tool[lit("arguments")] = QJsonObject();
  QJsonObject notification;
  notification[lit("threadId")] = lit("thread-1"); notification[lit("turnId")] = lit("turn-1");
  notification[lit("item")] = tool;
  original.Apply(lit("item/started"), notification);
  QJsonObject tokenUsage; tokenUsage[lit("total")] = QJsonObject({{lit("totalTokens"), 12345}});
  QJsonObject tokenNotification; tokenNotification[lit("threadId")] = lit("thread-1"); tokenNotification[lit("tokenUsage")] = tokenUsage;
  original.Apply(lit("thread/tokenUsage/updated"), tokenNotification);
  AgentConversation restored;
  QString error;
  check(restored.Load(original.ToJson(), error), "load in-progress snapshot");
  check(!restored.TurnRunning() && restored.TurnId().isEmpty(), "never restores running");
  check(restored.LastTurnStatus() == lit("interrupted"), "saved running turn interrupted");
  check(restored.Items().last().status == lit("interrupted"), "unfinished tool interrupted");
  check(restored.Items().last().referenceToken.isEmpty(), "old token invalidated");
  check(restored.TotalTokens() == 12345 && restored.Model() == lit("model-1"), "tokens and model restored");
  check(restored.Items().first().context == QJsonArray({chip}), "context restored");
  restored.BeginTurn(lit("new-token"));
  check(!restored.Apply(lit("item/completed"), notification), "old turn still closed");
  notification[lit("turnId")] = lit("turn-2"); tool[lit("id")] = lit("item-2"); notification[lit("item")] = tool;
  check(restored.Apply(lit("item/started"), notification), "new turn accepted");
  check(restored.Items().last().referenceToken == lit("new-token"), "new references active");
  QJsonObject bad = original.ToJson(); bad[lit("schema")] = lit("wrong");
  check(!restored.Load(bad, error) && restored.ThreadId() == lit("thread-1") && restored.TurnRunning(), "invalid load atomic");
  bad = original.ToJson(); QJsonArray items = bad[lit("items")].toArray(); items.append(items.last()); bad[lit("items")] = items;
  check(!restored.Load(bad, error), "duplicate item IDs rejected");
  bad = original.ToJson(); bad[lit("totalTokens")] = -1;
  check(!restored.Load(bad, error), "negative usage rejected");
  original.EndTurn(lit("completed"), QString());
  check(restored.Load(original.ToJson(), error), "load completed snapshot");
  check(restored.LastTurnStatus() == lit("completed"), "completed status retained");
  check(restored.ToJson()[lit("turns")].toArray().size() == 1, "turns restored");

  // Preserve the rendering data, including artifacts whose metadata is not suitable for display.
  AgentConversation rich;
  rich.SetThread(lit("thread-1"), lit("model-1")); rich.BeginTurn(lit("old-scope"));
  QJsonObject artifact;
  artifact[lit("path")] = lit("E:/artifacts/preview.png");
  artifact[lit("mediaType")] = lit("image/png");
  artifact[lit("bytes")] = 123;
  artifact[lit("sha256")] = QString(64, QLatin1Char('a'));
  QJsonObject envelope;
  envelope[lit("schema")] = lit("rendertest-mcp-result/v1");
  envelope[lit("instanceId")] = lit("instance");
  envelope[lit("captureSessionId")] = QString(32, QLatin1Char('b'));
  envelope[lit("meta")] = QJsonObject({{lit("captureSessionId"), QString(32, QLatin1Char('b'))}});
  envelope[lit("artifacts")] = QJsonArray({artifact});
  tool[lit("status")] = lit("completed"); tool[lit("durationMs")] = 321;
  tool[lit("error")] = QJsonObject({{lit("message"), lit("bounded tool diagnostic")}});
  tool[lit("arguments")] = QJsonObject({{lit("event_id"), 7}});
  tool[lit("result")] = QJsonObject({{lit("structuredContent"), envelope}});
  notification[lit("item")] = tool;
  rich.Apply(lit("item/completed"), notification);
  QJsonObject answer;
  answer[lit("type")] = lit("agentMessage"); answer[lit("id")] = lit("answer");
  answer[lit("text")] = lit("EID 7 writes ResourceId::8");
  notification[lit("item")] = answer; rich.Apply(lit("item/completed"), notification);
  QJsonObject reasoning;
  reasoning[lit("type")] = lit("reasoning"); reasoning[lit("id")] = lit("reasoning");
  reasoning[lit("summary")] = QJsonArray({lit("summary line")});
  notification[lit("item")] = reasoning; rich.Apply(lit("item/completed"), notification);
  rich.AddNotice(lit("warning"), lit("stored notice"));
  rich.EndTurn(lit("completed"), QString());
  check(restored.Load(rich.ToJson(), error), "load complete tool rendering fields");
  check(restored.Items()[0].durationMs == 321 && restored.Items()[0].arguments == rich.Items()[0].arguments &&
        restored.Items()[0].result == rich.Items()[0].result && restored.Items()[0].error == rich.Items()[0].error &&
        restored.Items()[0].artifacts == rich.Items()[0].artifacts,
        "tool arguments/result/duration/artifacts restored");
  check(restored.Items()[1].text == answer[lit("text")].toString() && restored.Items()[1].referenceToken.isEmpty(),
        "answer text restored without granting its old reference token");
  check(restored.Items()[2].kind == AgentItem::Kind::Reasoning && restored.Items()[2].text == lit("summary line") &&
        restored.Items()[3].kind == AgentItem::Kind::Notice && restored.Items()[3].text == lit("stored notice"),
        "reasoning summaries and notices restored");
  QJsonObject oversized = rich.ToJson(); QJsonArray oversizedItems = oversized[lit("items")].toArray();
  QJsonObject large = oversizedItems[1].toObject(); large[lit("text")] = QString(2 * 1024 * 1024 + 1, QLatin1Char('x'));
  oversizedItems[1] = large; oversized[lit("items")] = oversizedItems;
  check(!restored.Load(oversized, error), "oversized snapshot item rejected");
  std::printf("M5d conversation/identity checks: %s\n", failures ? "FAILED" : "PASS");
  return failures ? 1 : 0;
}

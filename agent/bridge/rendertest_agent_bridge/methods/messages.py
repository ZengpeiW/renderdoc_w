from ..paging import paginate, query_key
from ..values import enum_name, text
from .common import result, session_of


def get_debug_messages(runtime, context):
    session = session_of(context)

    def read():
        return [{
            "index": index,
            "eventId": int(message.eventId),
            "category": enum_name(message.category),
            "severity": enum_name(message.severity),
            "source": enum_name(message.source),
            "messageId": int(message.messageID),
            "description": text(message.description),
        } for index, message in enumerate(runtime.ctx.DebugMessages())]

    # qrenderdoc already drained the replay's message queue into its own list, so read that copy
    # on the UI thread instead of stealing messages from the controller.
    messages = runtime.ui.call(context, read)
    page, next_cursor, total = paginate(messages, context.params, "get_debug_messages", session,
                                        query_key("messages"))
    return result({"total": total, "messages": page}, next_cursor=next_cursor)


HANDLERS = {"get_debug_messages": get_debug_messages}

from . import (action_stats, actions, api_calls, buffers, debug, event_compare, history, mesh,
               messages, overlay, pipeline, resource_stats, resources, shader_decompile,
               shader_source, status, textures)

HANDLERS = {}
for _module in (status, actions, action_stats, api_calls, event_compare, pipeline, shader_source,
                shader_decompile, resources, resource_stats, textures, overlay, buffers, messages,
                history, debug, mesh):
    HANDLERS.update(_module.HANDLERS)

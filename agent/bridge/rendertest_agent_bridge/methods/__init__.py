from . import (action_stats, actions, api_calls, buffers, debug, debug_compute, event_compare, gpu_counters,
               history, mesh, messages, overlay, pipeline, resource_stats, resources, shader_decompile,
               shader_references, shader_source, status, textures)

HANDLERS = {}
for _module in (status, actions, action_stats, api_calls, event_compare, gpu_counters, pipeline, shader_source,
                shader_decompile, shader_references, resources, resource_stats, textures, overlay, buffers, messages,
                history, debug, debug_compute, mesh):
    HANDLERS.update(_module.HANDLERS)

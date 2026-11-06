from . import (action_stats, actions, buffers, debug, history, mesh, messages, overlay, pipeline,
               resource_stats, resources, shader_source, status, textures)

HANDLERS = {}
for _module in (status, actions, action_stats, pipeline, shader_source, resources, resource_stats,
                textures, overlay, buffers, messages, history, debug, mesh):
    HANDLERS.update(_module.HANDLERS)

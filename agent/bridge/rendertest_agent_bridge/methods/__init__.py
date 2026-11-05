from . import (actions, buffers, debug, history, mesh, messages, pipeline, resources, status,
               textures)

HANDLERS = {}
for _module in (status, actions, pipeline, resources, textures, buffers, messages, history, debug,
                mesh):
    HANDLERS.update(_module.HANDLERS)

from . import actions, buffers, debug, history, messages, pipeline, resources, status, textures

HANDLERS = {}
for _module in (status, actions, pipeline, resources, textures, buffers, messages, history, debug):
    HANDLERS.update(_module.HANDLERS)

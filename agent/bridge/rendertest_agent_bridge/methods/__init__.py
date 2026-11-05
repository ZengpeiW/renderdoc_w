from . import actions, buffers, messages, pipeline, resources, status, textures

HANDLERS = {}
for _module in (status, actions, pipeline, resources, textures, buffers, messages):
    HANDLERS.update(_module.HANDLERS)

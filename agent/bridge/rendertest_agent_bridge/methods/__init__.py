from . import actions, buffers, history, messages, pipeline, resources, status, textures

HANDLERS = {}
for _module in (status, actions, pipeline, resources, textures, buffers, messages, history):
    HANDLERS.update(_module.HANDLERS)

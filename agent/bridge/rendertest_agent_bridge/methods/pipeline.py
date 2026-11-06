from ..artifacts import write_bytes
from ..ipc import AgentBridgeError
from ..values import enum_name, format_name, number, numbers, resource_id
from . import cbuffer_members
from .common import (STAGE_NAMES, action_index, get_bool, get_choice, get_choices, get_int,
                     get_str, invalid, renderdoc_module, resolve_event, resource_index, result,
                     session_of, stage_enum)

BINDING_LIMIT = 64
SUMMARY_BINDING_LIMIT = 16
SIGNATURE_LIMIT = 128
VERTEX_INPUT_LIMIT = 32
CBUFFER_VARIABLE_LIMIT = 4096
INLINE_DISASSEMBLY_BYTES = 256 * 1024

VALUE_FIELDS = {
    "Float": "f32v", "Double": "f64v", "Half": "f16v", "SInt": "s32v", "UInt": "u32v",
    "Bool": "u32v", "SShort": "s16v", "UShort": "u16v", "SLong": "s64v", "ULong": "u64v",
    "SByte": "s8v", "UByte": "u8v", "Enum": "u32v", "GPUPointer": "u64v",
}
COMPUTE_FLAGS = ("Dispatch", "DispatchRay")


def _is_compute(node):
    return node is not None and any(flag in node["flags"] for flag in COMPUTE_FLAGS)


def _relevant_stages(node, stages):
    names = [name for name in STAGE_NAMES if stages is None or name in stages]
    if node is None or not node["flags"]:
        return names
    if _is_compute(node):
        return [name for name in names if name == "compute"]
    return [name for name in names if name != "compute"]


def _pipeline_object(pipe, stage_name):
    if stage_name == "compute":
        return pipe.GetComputePipelineObject()
    return pipe.GetGraphicsPipelineObject()


def descriptor_info(descriptor, resources):
    key = resource_id(descriptor.resource)
    info = {
        "resourceId": key,
        "name": resources.name(key),
        "descriptorType": enum_name(descriptor.type),
        "format": format_name(descriptor.format),
    }
    if key in resources.textures:
        texture = resources.textures[key]
        info.update({
            "textureType": enum_name(descriptor.textureType),
            "width": int(texture.width),
            "height": int(texture.height),
            "firstMip": int(descriptor.firstMip),
            "numMips": int(descriptor.numMips),
            "firstSlice": int(descriptor.firstSlice),
            "numSlices": int(descriptor.numSlices),
        })
    elif key in resources.buffers:
        info.update({"byteOffset": int(descriptor.byteOffset),
                     "byteSize": int(descriptor.byteSize)})
    return info


def _reflected_name(reflected, index):
    return reflected[index].name if 0 <= index < len(reflected) else None


def _used_descriptors(items, reflected, resources, limit):
    items = list(items)
    entries = []
    for used in items[:limit]:
        access = used.access
        index = int(access.index)
        entry = descriptor_info(used.descriptor, resources)
        entry["resourceName"] = entry.pop("name")
        entry.update({
            "index": index if 0 <= index < len(reflected) else None,
            "name": _reflected_name(reflected, index),
            "arrayElement": int(access.arrayElement),
            "staticallyUnused": bool(access.staticallyUnused),
        })
        entries.append(entry)
    return entries, len(items)


def _sampler_info(sampler):
    info = {
        "addressU": enum_name(sampler.addressU),
        "addressV": enum_name(sampler.addressV),
        "addressW": enum_name(sampler.addressW),
        "compareFunction": enum_name(sampler.compareFunction),
        "maxAnisotropy": number(sampler.maxAnisotropy),
        "minLOD": number(sampler.minLOD),
        "maxLOD": number(sampler.maxLOD),
        "mipBias": number(sampler.mipBias),
    }
    texture_filter = sampler.filter
    info["filter"] = {
        "minify": enum_name(texture_filter.minify),
        "magnify": enum_name(texture_filter.magnify),
        "mip": enum_name(texture_filter.mip),
        "function": enum_name(texture_filter.filter),
    }
    return info


def stage_bindings(pipe, stage, reflection, resources, limit):
    truncated = False
    constant_blocks = []
    blocks = list(reflection.constantBlocks)
    for index, block in enumerate(blocks[:limit]):
        descriptor = pipe.GetConstantBlock(stage, index, 0).descriptor
        key = resource_id(descriptor.resource)
        constant_blocks.append({
            "index": index,
            "name": block.name,
            "byteSize": int(block.byteSize),
            "bufferBacked": bool(block.bufferBacked),
            "variableCount": len(block.variables),
            "resourceId": key,
            "resourceName": resources.name(key),
            "byteOffset": int(descriptor.byteOffset),
            "bindingByteSize": int(descriptor.byteSize),
        })
    truncated = truncated or len(blocks) > limit

    read_only, total_read_only = _used_descriptors(
        pipe.GetReadOnlyResources(stage, True), list(reflection.readOnlyResources), resources,
        limit)
    read_write, total_read_write = _used_descriptors(
        pipe.GetReadWriteResources(stage, True), list(reflection.readWriteResources), resources,
        limit)

    reflected_samplers = list(reflection.samplers)
    samplers = []
    used_samplers = list(pipe.GetSamplers(stage, True))
    for used in used_samplers[:limit]:
        index = int(used.access.index)
        entry = {"index": index if 0 <= index < len(reflected_samplers) else None,
                 "name": _reflected_name(reflected_samplers, index),
                 "arrayElement": int(used.access.arrayElement)}
        entry.update(_sampler_info(used.sampler))
        samplers.append(entry)

    truncated = (truncated or total_read_only > limit or total_read_write > limit or
                 len(used_samplers) > limit)
    return {
        "constantBlocks": constant_blocks,
        "readOnlyResources": read_only,
        "readWriteResources": read_write,
        "samplers": samplers,
        "counts": {"constantBlocks": len(blocks), "readOnlyResources": total_read_only,
                   "readWriteResources": total_read_write, "samplers": len(used_samplers)},
        "truncated": truncated,
    }


def _shaders(pipe, rd, resources, stage_names):
    shaders = {}
    for name in stage_names:
        stage = stage_enum(rd, name)
        key = resource_id(pipe.GetShader(stage))
        if key is None:
            continue
        reflection = pipe.GetShaderReflection(stage)
        entry = {"resourceId": key, "name": resources.name(key),
                 "entryPoint": pipe.GetShaderEntryPoint(stage)}
        if reflection is not None:
            entry["encoding"] = enum_name(reflection.encoding)
            if name == "compute":
                entry["threadGroupSize"] = [int(item) for item in
                                            reflection.dispatchThreadsDimension]
        shaders[name] = (entry, stage, reflection)
    return shaders


def _blend_equation(equation):
    return {"source": enum_name(equation.source), "destination": enum_name(equation.destination),
            "operation": enum_name(equation.operation)}


def _outputs(pipe, resources, with_blend):
    blends = list(pipe.GetColorBlends()) if with_blend else []
    targets = []
    for slot, descriptor in enumerate(pipe.GetOutputTargets()):
        if resource_id(descriptor.resource) is None:
            continue
        entry = {"slot": slot}
        entry.update(descriptor_info(descriptor, resources))
        if slot < len(blends):
            blend = blends[slot]
            entry["blend"] = {
                "enabled": bool(blend.enabled),
                "color": _blend_equation(blend.colorBlend),
                "alpha": _blend_equation(blend.alphaBlend),
                "writeMask": int(blend.writeMask),
                "logicOperationEnabled": bool(blend.logicOperationEnabled),
                "logicOperation": enum_name(blend.logicOperation),
            }
        targets.append(entry)
    depth = pipe.GetDepthTarget()
    depth_info = descriptor_info(depth, resources) if resource_id(depth.resource) else None
    return targets, depth_info


def _viewport(viewport):
    return {"enabled": bool(viewport.enabled), "x": number(viewport.x), "y": number(viewport.y),
            "width": number(viewport.width), "height": number(viewport.height),
            "minDepth": number(viewport.minDepth), "maxDepth": number(viewport.maxDepth)}


def _scissor(scissor):
    return {"enabled": bool(scissor.enabled), "x": int(scissor.x), "y": int(scissor.y),
            "width": int(scissor.width), "height": int(scissor.height)}


def _stencil_face(face):
    return {"function": enum_name(face.function), "passOperation": enum_name(face.passOperation),
            "failOperation": enum_name(face.failOperation),
            "depthFailOperation": enum_name(face.depthFailOperation),
            "reference": int(face.reference), "compareMask": int(face.compareMask),
            "writeMask": int(face.writeMask)}


def _input_assembly(pipe, resources):
    index_buffer = pipe.GetIBuffer()
    index_key = resource_id(index_buffer.resourceId)
    vertex_buffers = []
    for slot, buffer in enumerate(pipe.GetVBuffers()):
        key = resource_id(buffer.resourceId)
        if key is None:
            continue
        vertex_buffers.append({"slot": slot, "resourceId": key, "name": resources.name(key),
                               "byteOffset": int(buffer.byteOffset),
                               "byteStride": int(buffer.byteStride),
                               "byteSize": int(buffer.byteSize)})
    inputs = list(pipe.GetVertexInputs())
    return {
        "topology": enum_name(pipe.GetPrimitiveTopology()),
        "restartEnabled": bool(pipe.IsRestartEnabled()),
        "indexBuffer": None if index_key is None else {
            "resourceId": index_key, "name": resources.name(index_key),
            "byteOffset": int(index_buffer.byteOffset),
            "byteStride": int(index_buffer.byteStride),
            "byteSize": int(index_buffer.byteSize)},
        "vertexBuffers": vertex_buffers,
        "vertexInputs": [{
            "name": attribute.name,
            "vertexBuffer": int(attribute.vertexBuffer),
            "byteOffset": int(attribute.byteOffset),
            "format": format_name(attribute.format),
            "perInstance": bool(attribute.perInstance),
            "instanceRate": int(attribute.instanceRate),
            "used": bool(attribute.used),
        } for attribute in inputs[:VERTEX_INPUT_LIMIT]],
        "vertexInputCount": len(inputs),
    }


def read_pipeline_state(controller, resources, node, stages=None, include_bindings=True,
                        binding_limit=BINDING_LIMIT):
    rd = renderdoc_module()
    pipe = controller.GetPipelineState()
    compute = _is_compute(node)
    shaders = _shaders(pipe, rd, resources, _relevant_stages(node, stages))
    state = {
        "graphicsPipeline": resources.ref(resource_id(pipe.GetGraphicsPipelineObject())),
        "computePipeline": resources.ref(resource_id(pipe.GetComputePipelineObject())),
        "shaders": dict((name, value[0]) for name, value in shaders.items()),
    }
    truncated = False
    if include_bindings:
        bindings = {}
        for name, (_, stage, reflection) in shaders.items():
            if reflection is None:
                continue
            bindings[name] = stage_bindings(pipe, stage, reflection, resources, binding_limit)
            truncated = truncated or bindings[name]["truncated"]
        state["bindings"] = bindings
    if not compute:
        depth_state = pipe.GetDepthTestState()
        raster = pipe.GetRasterState()
        stencil_enabled = bool(pipe.IsStencilTestEnabled())
        targets, depth_target = _outputs(pipe, resources, True)
        state.update({
            "inputAssembly": _input_assembly(pipe, resources),
            "rasterizer": {
                "viewport": _viewport(pipe.GetViewport(0)),
                "scissor": _scissor(pipe.GetScissor(0)),
                "fillMode": enum_name(raster.fillMode),
                "cullMode": enum_name(raster.cullMode),
                "frontCCW": bool(raster.frontCCW),
            },
            "depthStencil": {
                "depthEnable": bool(depth_state.depthEnable),
                "depthFunction": enum_name(depth_state.depthFunction),
                "depthWrites": bool(depth_state.depthWrites),
                "depthBounds": bool(depth_state.depthBounds),
                "minDepthBounds": number(depth_state.minDepthBounds),
                "maxDepthBounds": number(depth_state.maxDepthBounds),
                "stencilEnable": stencil_enabled,
            },
            "independentBlend": bool(pipe.IsIndependentBlendingEnabled()),
            "outputTargets": targets,
            "depthTarget": depth_target,
        })
        if stencil_enabled:
            front, back = pipe.GetStencilFaces()
            state["depthStencil"]["front"] = _stencil_face(front)
            state["depthStencil"]["back"] = _stencil_face(back)
    return state, truncated


def read_pipeline_summary(controller, resources, node):
    rd = renderdoc_module()
    pipe = controller.GetPipelineState()
    shaders = _shaders(pipe, rd, resources, _relevant_stages(node, None))
    summary = {"shaders": dict((name, value[0]) for name, value in shaders.items())}
    if not _is_compute(node):
        targets, depth_target = _outputs(pipe, resources, False)
        summary.update({
            "topology": enum_name(pipe.GetPrimitiveTopology()),
            "outputTargets": targets,
            "depthTarget": depth_target,
        })
    return summary, shaders, pipe


def _action_detail(actions, node):
    if node is None:
        return None
    detail = actions.summary(node, with_path=True)
    for key in ("firstEventId", "numIndices", "numInstances", "baseVertex", "indexOffset",
                "vertexOffset", "instanceOffset", "drawIndex", "dispatchDimension",
                "dispatchThreadsDimension", "copySource", "copyDestination", "outputs",
                "depthOut"):
        detail[key] = node[key]
    return detail


def inspect_event(runtime, context):
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    resources = resource_index(runtime, context)
    node = actions.action_for(event_id)

    def read(controller):
        summary, shaders, pipe = read_pipeline_summary(controller, resources, node)
        bindings = {}
        for name, (_, stage, reflection) in shaders.items():
            if reflection is not None:
                bindings[name] = stage_bindings(pipe, stage, reflection, resources,
                                                SUMMARY_BINDING_LIMIT)
        summary["bindings"] = bindings
        return summary

    pipeline, meta = runtime.replay.at_event(context, event_id, read)
    truncated = any(item["truncated"] for item in pipeline["bindings"].values())
    return result({
        "eventId": event_id,
        "isAction": node is not None and node["eventId"] == event_id,
        "action": _action_detail(actions, node),
        "pipeline": pipeline,
    }, meta=meta, truncated=truncated)


def get_pipeline_state(runtime, context):
    params = context.params
    stages = get_choices(params, "stages", STAGE_NAMES)
    include_bindings = get_bool(params, "include_bindings", True)
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    resources = resource_index(runtime, context)
    node = actions.action_for(event_id)
    (state, truncated), meta = runtime.replay.at_event(
        context, event_id,
        lambda controller: read_pipeline_state(controller, resources, node, stages,
                                               include_bindings))
    state["eventId"] = event_id
    state["api"] = context.state.get("api") or ""
    state["action"] = actions.summary(node) if node else None
    return result(state, meta=meta, truncated=truncated)


def _signature(parameters):
    return [{
        "varName": item.varName,
        "semanticName": item.semanticName,
        "semanticIndex": int(item.semanticIndex),
        "regIndex": int(item.regIndex),
        "systemValue": enum_name(item.systemValue),
        "varType": enum_name(item.varType),
        "compCount": int(item.compCount),
        "regChannelMask": int(item.regChannelMask),
        "channelUsedMask": int(item.channelUsedMask),
        "stream": int(item.stream),
    } for item in list(parameters)[:SIGNATURE_LIMIT]]


def _shader_resources(items):
    return [{
        "index": index,
        "name": item.name,
        "descriptorType": enum_name(item.descriptorType),
        "textureType": enum_name(item.textureType),
        "isTexture": bool(item.isTexture),
        "bindPoint": int(item.fixedBindNumber),
        "space": int(item.fixedBindSetOrSpace),
        "bindArraySize": int(item.bindArraySize),
        "variableType": item.variableType.name,
    } for index, item in enumerate(items)]


def reflection_info(reflection):
    debug = reflection.debugInfo
    files = list(debug.files)
    return {
        "entryPoint": reflection.entryPoint,
        "stage": enum_name(reflection.stage),
        "encoding": enum_name(reflection.encoding),
        "dispatchThreadsDimension": [int(item) for item in reflection.dispatchThreadsDimension],
        "inputSignature": _signature(reflection.inputSignature),
        "outputSignature": _signature(reflection.outputSignature),
        "constantBlocks": [{
            "index": index,
            "name": block.name,
            "byteSize": int(block.byteSize),
            "bufferBacked": bool(block.bufferBacked),
            "bindPoint": int(block.fixedBindNumber),
            "space": int(block.fixedBindSetOrSpace),
            "bindArraySize": int(block.bindArraySize),
            "variableCount": len(block.variables),
        } for index, block in enumerate(reflection.constantBlocks)],
        "samplers": [{
            "index": index,
            "name": sampler.name,
            "bindPoint": int(sampler.fixedBindNumber),
            "space": int(sampler.fixedBindSetOrSpace),
            "bindArraySize": int(sampler.bindArraySize),
        } for index, sampler in enumerate(reflection.samplers)],
        "readOnlyResources": _shader_resources(reflection.readOnlyResources),
        "readWriteResources": _shader_resources(reflection.readWriteResources),
        "debugInfo": {
            "compiler": enum_name(debug.compiler),
            "encoding": enum_name(debug.encoding),
            "entrySourceName": debug.entrySourceName,
            "debuggable": bool(debug.debuggable),
            "fileCount": len(files),
            "files": [{"filename": item.filename, "bytes": len(item.contents)}
                      for item in files[:64]],
        },
    }


def _bound_shader(pipe, rd, stage_name, event_id):
    stage = stage_enum(rd, stage_name)
    shader = pipe.GetShader(stage)
    key = resource_id(shader)
    reflection = pipe.GetShaderReflection(stage) if key else None
    if key is None or reflection is None:
        raise AgentBridgeError("INVALID_EVENT",
                               "No %s shader is bound at event %d." % (stage_name, event_id),
                               details={"event_id": event_id, "stage": stage_name})
    return stage, shader, key, reflection


def get_shader(runtime, context):
    params = context.params
    stage_name = get_choice(params, "stage", STAGE_NAMES, required=True)
    include_disassembly = get_bool(params, "include_disassembly", False)
    target = get_str(params, "target", max_length=128)
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    resources = resource_index(runtime, context)

    def read(controller):
        rd = renderdoc_module()
        pipe = controller.GetPipelineState()
        _, _, key, reflection = _bound_shader(pipe, rd, stage_name, event_id)
        data = {
            "eventId": event_id,
            "stage": stage_name,
            "resourceId": key,
            "name": resources.name(key),
            "entryPoint": pipe.GetShaderEntryPoint(stage_enum(rd, stage_name)),
            "reflection": reflection_info(reflection),
        }
        targets = [str(item) for item in controller.GetDisassemblyTargets(True)]
        data["disassemblyTargets"] = targets
        if not include_disassembly:
            return data, None
        chosen = target or (targets[0] if targets else "")
        if chosen not in targets:
            raise invalid("Unknown disassembly target %s." % chosen, allowed=targets)
        data["disassemblyTarget"] = chosen
        disassembly = controller.DisassembleShader(_pipeline_object(pipe, stage_name),
                                                   reflection, chosen)
        return data, disassembly

    (data, disassembly), meta = runtime.replay.at_event(context, event_id, read)
    artifacts = []
    if disassembly is not None:
        encoded = disassembly.encode("utf-8")
        if len(encoded) <= INLINE_DISASSEMBLY_BYTES:
            data["disassembly"] = disassembly
        else:
            artifact = write_bytes(runtime, session_of(context),
                                   "shader-%s-%s" % (data["resourceId"], stage_name), "txt",
                                   encoded, "disassembly", "text/plain; charset=utf-8",
                                   source={"event_id": event_id, "stage": stage_name,
                                           "resource_id": data["resourceId"],
                                           "target": data["disassemblyTarget"]})
            artifacts.append(artifact)
            data["disassembly"] = None
            data["disassemblyArtifact"] = artifact["path"]
    return result(data, meta=meta, artifacts=artifacts)


def convert_variable(variable, budget, constant_type=None, offset=None, element=False):
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    type_name = enum_name(variable.type)
    rows = int(variable.rows)
    columns = int(variable.columns)
    item = {"name": variable.name, "type": type_name, "rows": rows, "columns": columns}
    if offset is not None:
        item["byteOffset"] = offset
    if constant_type is not None:
        item["typeName"] = constant_type.name
    members = list(variable.members)
    if members:
        is_array = (constant_type is not None and int(constant_type.elements) > 1 and
                    not element)
        named = {}
        if constant_type is not None and not is_array:
            named = dict((member.name, member) for member in constant_type.members)
        children = []
        for position, member in enumerate(members):
            child_type = None
            child_offset = None
            if is_array:
                child_type = constant_type
                if offset is not None:
                    child_offset = offset + position * int(constant_type.arrayByteStride)
            elif member.name in named:
                match = named[member.name]
                child_type = match.type
                if offset is not None:
                    child_offset = offset + int(match.byteOffset)
            child = convert_variable(member, budget, child_type, child_offset, is_array)
            if child is None:
                break
            children.append(child)
        item["members"] = children
        return item
    field = VALUE_FIELDS.get(type_name)
    if field is not None:
        values = list(getattr(variable.value, field))[:max(1, rows * columns)]
        if type_name == "Bool":
            values = [bool(value) for value in values]
        item["value"] = numbers(values)
    return item


def _find_block(blocks, index, name):
    if index is not None:
        if index >= len(blocks):
            raise invalid("Constant buffer index %d is out of range." % index,
                          available=[block.name for block in blocks])
        return index
    for position, block in enumerate(blocks):
        if block.name == name:
            return position
    lowered = name.lower()
    for position, block in enumerate(blocks):
        if block.name.lower() == lowered:
            return position
    raise invalid("No constant buffer named %s." % name,
                  available=[block.name for block in blocks])


def get_cbuffer(runtime, context):
    params = context.params
    member_request = cbuffer_members.validate_request(params)
    stage_name = get_choice(params, "stage", STAGE_NAMES, required=True)
    index = get_int(params, "index")
    name = get_str(params, "name")
    if (index is None) == (name is None):
        raise invalid("Specify exactly one of index or name.")
    actions = action_index(runtime, context)
    event_id = resolve_event(context, actions)
    resources = resource_index(runtime, context)

    def read(controller):
        rd = renderdoc_module()
        pipe = controller.GetPipelineState()
        stage, shader, key, reflection = _bound_shader(pipe, rd, stage_name, event_id)
        blocks = list(reflection.constantBlocks)
        position = _find_block(blocks, index, name)
        block = blocks[position]
        descriptor = pipe.GetConstantBlock(stage, position, 0).descriptor
        variables = controller.GetCBufferVariableContents(
            _pipeline_object(pipe, stage_name), shader, stage, pipe.GetShaderEntryPoint(stage),
            position, descriptor.resource, descriptor.byteOffset, descriptor.byteSize)
        buffer_key = resource_id(descriptor.resource)
        data = {
            "eventId": event_id,
            "stage": stage_name,
            "shader": {"resourceId": key, "name": resources.name(key)},
            "index": position,
            "name": block.name,
            "byteSize": int(block.byteSize),
            "bufferBacked": bool(block.bufferBacked),
            "bindPoint": int(block.fixedBindNumber),
            "space": int(block.fixedBindSetOrSpace),
            "binding": {"resourceId": buffer_key, "name": resources.name(buffer_key),
                        "byteOffset": int(descriptor.byteOffset),
                        "byteSize": int(descriptor.byteSize)},
        }
        if member_request is not None:
            scope = (session_of(context), event_id, stage_name, key, position, buffer_key,
                     int(descriptor.byteOffset), int(descriptor.byteSize))
            page, next_cursor, truncated, warnings = cbuffer_members.project_members(
                variables, block, member_request, scope, convert_variable,
                check_cancelled=context.check_cancelled)
            data.update(page)
            return data, next_cursor, truncated, warnings

        constants = dict((constant.name, constant) for constant in block.variables)
        budget = [CBUFFER_VARIABLE_LIMIT]
        converted = []
        for variable in variables:
            constant = constants.get(variable.name)
            item = convert_variable(variable, budget,
                                    constant.type if constant is not None else None,
                                    int(constant.byteOffset) if constant is not None else None)
            if item is None:
                break
            converted.append(item)
        data["variables"] = converted
        truncated = budget[0] <= 0
        warnings = (["Only the first %d variables were returned." % CBUFFER_VARIABLE_LIMIT]
                    if truncated else [])
        return data, None, truncated, warnings

    (data, next_cursor, truncated, warnings), meta = runtime.replay.at_event(context, event_id, read)
    return result(data, meta=meta, next_cursor=next_cursor, truncated=truncated, warnings=warnings)


HANDLERS = {
    "inspect_event": inspect_event,
    "get_pipeline_state": get_pipeline_state,
    "get_shader": get_shader,
    "get_cbuffer": get_cbuffer,
}

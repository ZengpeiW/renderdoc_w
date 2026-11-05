"""In-process stand-ins for the renderdoc/qrenderdoc Python modules used by bridge tests."""

from __future__ import annotations

import enum
import struct
import threading
import types
from pathlib import Path
from typing import Any, Callable

NS = types.SimpleNamespace


class ResourceId:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __int__(self) -> int:
        return self.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ResourceId) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)


class ShaderStage(enum.IntEnum):
    Vertex = 0
    Hull = 1
    Domain = 2
    Geometry = 3
    Pixel = 4
    Compute = 5
    Amplification = 6
    Mesh = 7


class ActionFlags(enum.IntFlag):
    NoFlags = 0
    Clear = 0x1
    Drawcall = 0x2
    Dispatch = 0x4
    SetMarker = 0x20
    PushMarker = 0x40
    Present = 0x100
    Indexed = 0x10000


class CompType(enum.IntEnum):
    Typeless = 0
    Float = 1
    UNorm = 2
    UInt = 4
    SInt = 5


class FileType(enum.IntEnum):
    DDS = 0
    PNG = 1
    BMP = 3
    EXR = 7


class AlphaMapping(enum.IntEnum):
    Discard = 0
    Preserve = 3


class ResourceType(enum.IntEnum):
    Unknown = 0
    Texture = 4
    Buffer = 5
    Shader = 10
    PipelineState = 12


class DescriptorType(enum.IntEnum):
    ConstantBuffer = 1
    Sampler = 2
    Image = 4
    ReadWriteImage = 7


class VarType(enum.IntEnum):
    Float = 0
    UInt = 4
    Struct = 13


class Simple(enum.IntEnum):
    Unknown = 0
    Texture2D = 3
    TriangleList = 4
    Solid = 1
    Back = 2
    LessEqual = 5
    Add = 6
    One = 7
    Zero = 8
    Linear = 9
    Wrap = 10
    DXIL = 11
    D3D12 = 12
    Error = 13
    Validation = 14
    High = 15
    Pixel = 16
    Target = 17
    UsageColorTarget = 18


class Subresource:
    def __init__(self) -> None:
        self.mip = 0
        self.slice = 0
        self.sample = 0


class TextureSave:
    def __init__(self) -> None:
        self.resourceId = ResourceId()
        self.destType = FileType.DDS
        self.typeCast = CompType.Typeless
        self.mip = 0
        self.slice = NS(sliceIndex=0)
        self.sample = NS(sampleIndex=0)
        self.comp = NS(blackPoint=0.0, whitePoint=1.0)
        self.channelExtract = -1
        self.alpha = AlphaMapping.Discard


def module() -> types.ModuleType:
    rd = types.ModuleType("renderdoc")
    for value in (ResourceId, ShaderStage, ActionFlags, CompType, FileType, AlphaMapping,
                  ResourceType, DescriptorType, VarType, Subresource, TextureSave):
        setattr(rd, value.__name__, value)
    return rd


def fmt(name: str, comp_type: CompType = CompType.UNorm, count: int = 4) -> Any:
    return NS(Name=lambda: name, type=Simple.Unknown, compType=comp_type, compCount=count,
              compByteWidth=1)


def action(event_id: int, name: str, flags: ActionFlags, first: int,
           children: list[Any] | None = None, outputs: tuple[int, ...] = ()) -> Any:
    node = NS(eventId=event_id, actionId=event_id * 10, flags=flags, customName=name,
              events=[NS(eventId=item) for item in range(first, event_id + 1)],
              children=children or [], numIndices=36 if flags & ActionFlags.Drawcall else 0,
              numInstances=1, baseVertex=0, indexOffset=0, vertexOffset=0, instanceOffset=0,
              drawIndex=0, dispatchDimension=(8, 8, 1) if flags & ActionFlags.Dispatch else (0, 0, 0),
              dispatchThreadsDimension=(0, 0, 0), copySource=ResourceId(),
              copyDestination=ResourceId(), outputs=[ResourceId(item) for item in outputs],
              depthOut=ResourceId())
    node.GetName = lambda structured: name
    return node


def root_actions() -> list[Any]:
    draw = action(5, "DrawIndexed(36)", ActionFlags.Drawcall | ActionFlags.Indexed, 2,
                  outputs=(100,))
    dispatch = action(8, "Dispatch(8, 8, 1)", ActionFlags.Dispatch, 6)
    frame = action(1, "Frame", ActionFlags.PushMarker, 1, children=[draw, dispatch])
    present = action(9, "Present()", ActionFlags.Present, 9)
    return [frame, present]


def shader_value(**fields: list[Any]) -> Any:
    value = NS(f32v=[0.0] * 16, u32v=[0] * 16)
    for key, items in fields.items():
        setattr(value, key, list(items) + [0] * (16 - len(items)))
    return value


def constant(name: str, offset: int, type_name: str) -> Any:
    return NS(name=name, byteOffset=offset,
              type=NS(name=type_name, elements=1, arrayByteStride=0, members=[]))


def reflection(stage: ShaderStage) -> Any:
    globals_block = NS(name="Globals", byteSize=32, bufferBacked=True, fixedBindNumber=0,
                       fixedBindSetOrSpace=0, bindArraySize=1,
                       variables=[constant("tint", 0, "float4"), constant("count", 16, "uint")])
    return NS(
        entryPoint="main", stage=stage, encoding=Simple.DXIL,
        dispatchThreadsDimension=(8, 8, 1) if stage == ShaderStage.Compute else (0, 0, 0),
        inputSignature=[],
        outputSignature=[NS(varName="color", semanticName="SV_Target", semanticIndex=0,
                            regIndex=0, systemValue=Simple.Target, varType=VarType.Float,
                            compCount=4, regChannelMask=15, channelUsedMask=15, stream=0)],
        constantBlocks=[globals_block],
        samplers=[NS(name="linearSampler", fixedBindNumber=0, fixedBindSetOrSpace=0,
                     bindArraySize=1)],
        readOnlyResources=[NS(name="albedo", descriptorType=DescriptorType.Image,
                              textureType=Simple.Texture2D, isTexture=True, fixedBindNumber=0,
                              fixedBindSetOrSpace=0, bindArraySize=1,
                              variableType=NS(name="Texture2D<float4>"))],
        readWriteResources=[],
        debugInfo=NS(compiler=Simple.Unknown, encoding=Simple.DXIL, entrySourceName="main",
                     debuggable=True, files=[NS(filename="shader.hlsl", contents="void main(){}")]),
    )


def descriptor(resource: int, kind: DescriptorType = DescriptorType.Image, size: int = 0) -> Any:
    return NS(resource=ResourceId(resource), type=kind, format=fmt("R8G8B8A8_UNORM"),
              textureType=Simple.Texture2D, firstMip=0, numMips=1, firstSlice=0, numSlices=1,
              byteOffset=0, byteSize=size)


def access(index: int) -> Any:
    return NS(index=index, arrayElement=0, staticallyUnused=False)


class FakePipe:
    def __init__(self, controller: FakeController) -> None:
        self.controller = controller

    def _shaders(self) -> dict[ShaderStage, int]:
        if self.controller.current == 8:
            return {ShaderStage.Compute: 303}
        if self.controller.current == 5:
            return {ShaderStage.Vertex: 302, ShaderStage.Pixel: 300}
        return {}

    def GetShader(self, stage: ShaderStage) -> ResourceId:
        return ResourceId(self._shaders().get(stage, 0))

    def GetShaderReflection(self, stage: ShaderStage) -> Any:
        return reflection(stage) if stage in self._shaders() else None

    def GetShaderEntryPoint(self, stage: ShaderStage) -> str:
        return "main" if stage in self._shaders() else ""

    def GetGraphicsPipelineObject(self) -> ResourceId:
        return ResourceId(400)

    def GetComputePipelineObject(self) -> ResourceId:
        return ResourceId(0)

    def GetConstantBlock(self, stage: ShaderStage, index: int, element: int) -> Any:
        return NS(access=access(index),
                  descriptor=descriptor(200, DescriptorType.ConstantBuffer, 32))

    def GetReadOnlyResources(self, stage: ShaderStage, only_used: bool) -> list[Any]:
        return [NS(access=access(0), descriptor=descriptor(100))]

    def GetReadWriteResources(self, stage: ShaderStage, only_used: bool) -> list[Any]:
        return []

    def GetSamplers(self, stage: ShaderStage, only_used: bool) -> list[Any]:
        sampler = NS(addressU=Simple.Wrap, addressV=Simple.Wrap, addressW=Simple.Wrap,
                     compareFunction=Simple.Unknown, maxAnisotropy=1.0, minLOD=0.0,
                     maxLOD=float("inf"), mipBias=0.0,
                     filter=NS(minify=Simple.Linear, magnify=Simple.Linear, mip=Simple.Linear,
                               filter=Simple.Unknown))
        return [NS(access=access(0), sampler=sampler)]

    def GetOutputTargets(self) -> list[Any]:
        return [descriptor(100), descriptor(0)]

    def GetDepthTarget(self) -> Any:
        return descriptor(101)

    def GetColorBlends(self) -> list[Any]:
        equation = NS(source=Simple.One, destination=Simple.Zero, operation=Simple.Add)
        return [NS(enabled=False, colorBlend=equation, alphaBlend=equation, writeMask=15,
                   logicOperationEnabled=False, logicOperation=Simple.Unknown)] * 2

    def GetDepthTestState(self) -> Any:
        return NS(depthEnable=True, depthFunction=Simple.LessEqual, depthWrites=True,
                  depthBounds=False, minDepthBounds=0.0, maxDepthBounds=1.0)

    def GetRasterState(self) -> Any:
        return NS(fillMode=Simple.Solid, cullMode=Simple.Back, frontCCW=False)

    def IsStencilTestEnabled(self) -> bool:
        return False

    def IsIndependentBlendingEnabled(self) -> bool:
        return False

    def GetIBuffer(self) -> Any:
        return NS(resourceId=ResourceId(201), byteOffset=0, byteStride=2, byteSize=72)

    def GetVBuffers(self) -> list[Any]:
        return [NS(resourceId=ResourceId(202), byteOffset=0, byteStride=12, byteSize=288)]

    def GetVertexInputs(self) -> list[Any]:
        return [NS(name="POSITION", vertexBuffer=0, byteOffset=0, format=fmt("R32G32B32_FLOAT"),
                   perInstance=False, instanceRate=0, used=True)]

    def GetPrimitiveTopology(self) -> Any:
        return Simple.TriangleList

    def IsRestartEnabled(self) -> bool:
        return False

    def GetViewport(self, index: int) -> Any:
        return NS(enabled=True, x=0.0, y=0.0, width=4.0, height=4.0, minDepth=0.0, maxDepth=1.0)

    def GetScissor(self, index: int) -> Any:
        return NS(enabled=True, x=0, y=0, width=4, height=4)


class FakeController:
    def __init__(self) -> None:
        self.current = 9
        self.frame_events: list[int] = []
        self.saved: list[tuple[Any, Any, str]] = []
        self.history_calls: list[tuple[int, int, int, int]] = []
        self.pixel_history_supported = True
        self.on_callback: Callable[[], None] | None = None
        self.textures = [NS(resourceId=ResourceId(100), width=4, height=4, depth=1, mips=2,
                            arraysize=1, msSamp=1, msQual=0, cubemap=False, dimension=2,
                            type=Simple.Texture2D, format=fmt("R8G8B8A8_UNORM"), byteSize=80,
                            creationFlags=ActionFlags.NoFlags),
                         NS(resourceId=ResourceId(101), width=4, height=4, depth=1, mips=1,
                            arraysize=1, msSamp=1, msQual=0, cubemap=False, dimension=2,
                            type=Simple.Texture2D, format=fmt("D32_FLOAT", CompType.Float, 1),
                            byteSize=64, creationFlags=ActionFlags.NoFlags)]
        self.buffers = [NS(resourceId=ResourceId(200), length=128 * 1024, gpuAddress=0x1000,
                           creationFlags=ActionFlags.NoFlags)]
        self.resources = [
            self._resource(100, "SceneColor", ResourceType.Texture),
            self._resource(101, "SceneDepth", ResourceType.Texture),
            self._resource(200, "Constants", ResourceType.Buffer),
            self._resource(300, "MainPS", ResourceType.Shader),
            self._resource(302, "MainVS", ResourceType.Shader),
            self._resource(400, "ScenePSO", ResourceType.PipelineState),
        ]

    @staticmethod
    def _resource(value: int, name: str, kind: ResourceType) -> Any:
        return NS(resourceId=ResourceId(value), name=name, type=kind, autogeneratedName=False,
                  parentResources=[], derivedResources=[], initialisationChunks=[1, 2])

    def SetFrameEvent(self, event_id: int, force: bool) -> None:
        self.frame_events.append(event_id)
        self.current = event_id

    def GetFatalErrorStatus(self) -> Any:
        return NS(code=0)

    def GetStructuredFile(self) -> str:
        return "sdfile"

    def GetRootActions(self) -> list[Any]:
        return root_actions()

    def GetResources(self) -> list[Any]:
        return self.resources

    def GetTextures(self) -> list[Any]:
        return self.textures

    def GetBuffers(self) -> list[Any]:
        return self.buffers

    def GetPipelineState(self) -> FakePipe:
        if self.on_callback is not None:
            self.on_callback()
        return FakePipe(self)

    def GetUsage(self, resource: ResourceId) -> list[Any]:
        return [NS(eventId=5, usage=NS(name="ColorTarget")),
                NS(eventId=7, usage=NS(name="Discard")),
                NS(eventId=8, usage=NS(name="CS_Resource"))]

    def GetAPIProperties(self) -> Any:
        return NS(pixelHistory=self.pixel_history_supported)

    def PixelHistory(self, texture: ResourceId, x: int, y: int, sub: Subresource,
                     cast: CompType) -> list[Any]:
        self.history_calls.append((self.current, x, y, sub.mip))

        def value(level: float, depth: float = -1.0, stencil: int = -1) -> Any:
            raw = int(level * 255)
            return NS(col=NS(floatValue=(level, level, level, 1.0), uintValue=(raw, raw, raw, 255),
                             intValue=(raw, raw, raw, 255)), depth=depth, stencil=stencil)

        invalid = NS(col=NS(floatValue=(0.0,) * 4, uintValue=(0xdeadbeef, 0xdeadf00d, 0, 0),
                            intValue=(0,) * 4), depth=-1.0, stencil=-1)
        flags = dict(sampleMasked=False, backfaceCulled=False, depthClipped=False,
                     depthBoundsFailed=False, viewClipped=False, scissorClipped=False,
                     shaderDiscarded=False, depthTestFailed=False, stencilTestFailed=False,
                     predicationSkipped=False)
        return [
            NS(eventId=5, directShaderWrite=False, unboundPS=False, fragIndex=0, primitiveID=3,
               preMod=value(0.0), shaderOut=value(0.5, 0.25), postMod=value(0.5), **flags),
            NS(eventId=5, directShaderWrite=False, unboundPS=False, fragIndex=1, primitiveID=4,
               preMod=invalid, shaderOut=value(1.0, 0.75), postMod=value(0.5),
               **dict(flags, depthTestFailed=True)),
            NS(eventId=8, directShaderWrite=True, unboundPS=False, fragIndex=0, primitiveID=0,
               preMod=value(0.5), shaderOut=value(0.0), postMod=value(0.0), **flags),
        ]

    def GetDisassemblyTargets(self, with_pipeline: bool) -> list[str]:
        return ["DXIL", "HLSL"]

    def DisassembleShader(self, pipeline: ResourceId, refl: Any, target: str) -> str:
        return "; %s disassembly for %s\nret" % (target, refl.entryPoint)

    def GetCBufferVariableContents(self, pipeline: Any, shader: Any, stage: Any, entry: str,
                                   slot: int, buffer: Any, offset: int, length: int) -> list[Any]:
        return [NS(name="tint", type=VarType.Float, rows=1, columns=4, members=[],
                   value=shader_value(f32v=[1.0, 0.5, 0.25, float("nan")])),
                NS(name="count", type=VarType.UInt, rows=1, columns=1, members=[],
                   value=shader_value(u32v=[7]))]

    def PickPixel(self, texture: ResourceId, x: int, y: int, sub: Subresource,
                  cast: CompType) -> Any:
        return NS(floatValue=(x / 4, y / 4, 0.0, 1.0), uintValue=(x, y, 0, 1),
                  intValue=(x, y, 0, 1))

    def GetMinMax(self, texture: ResourceId, sub: Subresource, cast: CompType) -> Any:
        low = NS(floatValue=(0.0, 0.0, 0.0, 1.0), uintValue=(0, 0, 0, 1), intValue=(0, 0, 0, 1))
        high = NS(floatValue=(0.75, 0.75, 0.0, 1.0), uintValue=(3, 3, 0, 1),
                  intValue=(3, 3, 0, 1))
        return low, high

    def SaveTexture(self, save: TextureSave, path: str) -> Any:
        width = max(1, 4 >> save.mip)
        self.saved.append((save.destType, save.alpha, path))
        if save.destType == FileType.BMP:
            stride = (width * 3 + 3) & ~3
            pixels = b"".join(bytes([10, 20, 30] * width) + b"\x00" * (stride - width * 3)
                              for _ in range(width))
            header = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
            info = struct.pack("<IiiHHIIiiII", 40, width, width, 1, 24, 0, len(pixels), 0, 0, 0, 0)
            Path(path).write_bytes(header + info + pixels)
            return NS(code=0)
        header = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
        Path(path).write_bytes(header + struct.pack(">II", width, width) + b"\x08\x06\x00\x00\x00")
        return NS(code=0)

    def GetBufferData(self, buffer: ResourceId, offset: int, length: int) -> bytes:
        data = b"".join(struct.pack("<f", float(index)) for index in range(32 * 1024))
        return data[offset:offset + length]


class FakeReplayManager:
    def __init__(self, controller: FakeController) -> None:
        self.controller = controller
        self.running = True
        self.lock = threading.Lock()

    def BlockInvoke(self, callback: Callable[[FakeController], None]) -> None:
        if not self.running:
            return
        with self.lock:
            callback(self.controller)


class FakeHelper:
    def InvokeOntoUIThread(self, callback: Callable[[], None]) -> None:
        threading.Thread(target=callback, daemon=True).start()


class FakeContext:
    def __init__(self) -> None:
        self.viewers: list[Any] = []
        self.loaded = False
        self.selected = 0
        self.current = 0
        self.controller = FakeController()
        self.manager = FakeReplayManager(self.controller)
        self.helper = FakeHelper()
        self.messages = [NS(eventId=5, category=Simple.Validation, severity=Simple.High,
                            source=Simple.D3D12, messageID=42, description="Bad blend state")]

    def Replay(self) -> FakeReplayManager:
        return self.manager

    def Extensions(self) -> Any:
        return NS(GetMiniQtHelper=lambda: self.helper)

    def AddCaptureViewer(self, viewer: Any) -> None:
        self.viewers.append(viewer)

    def RemoveCaptureViewer(self, viewer: Any) -> None:
        self.viewers.remove(viewer)

    def GetCaptureFilename(self) -> str:
        return "E:/Captures/sample.rdc"

    def APIProps(self) -> Any:
        return NS(pipelineType=Simple.D3D12)

    def GetAction(self, event_id: int) -> Any:
        for node in root_actions():
            for item in [node] + list(node.children):
                if item.eventId == event_id:
                    return item
        return None

    def GetStructuredFile(self) -> str:
        return "sdfile"

    def CurSelectedEvent(self) -> int:
        return self.selected

    def CurEvent(self) -> int:
        return self.current

    def IsCaptureLoaded(self) -> bool:
        return self.loaded

    def DebugMessages(self) -> list[Any]:
        return self.messages

    def SetEventID(self, exclude: list[Any], selected: int, event_id: int) -> None:
        self.selected = selected
        self.current = event_id
        self.controller.SetFrameEvent(event_id, False)
        for viewer in list(self.viewers):
            viewer.OnSelectedEventChanged(selected)
            viewer.OnEventChanged(event_id)

    def load(self, event_id: int = 9) -> None:
        self.loaded = True
        self.selected = self.current = event_id
        self.controller.current = event_id
        for viewer in list(self.viewers):
            viewer.OnCaptureLoaded()

    def close(self) -> None:
        self.loaded = False
        for viewer in list(self.viewers):
            viewer.OnCaptureClosed()

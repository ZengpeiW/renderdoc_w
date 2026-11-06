"""Pinned, independently verified binaries from the upstream v0.6 Windows release.

Only the three component CLIs and their DLLs are used. The upstream wrapper executable and
batch file are deliberately excluded. Component revisions describe the release tag's gitlinks;
the archive and individual file hashes identify the exact binaries we accept.
"""

RELEASE = {
    "version": "v0.6",
    "url": "https://github.com/YYadorigi/HLSL-Decompiler/releases/download/v0.6/HLSLDecompiler.zip",
    "sha256": "57748e4a9db0dfc40f094da8284c5e274ce45e06831c3a2fd61a844436984caa",
    "size": 12881441,
    "files": {
        "dxbc2dxil.exe": {
            "sha256": "9b4b7220990a4e0898989cedec2a7238368a8f8384a0dfe3aa28adb5e0f9c484",
            "size": 858112, "component": "DirectXShaderCompiler",
        },
        "dxcompiler.dll": {
            "sha256": "c772f3749378adfdac2a743bcf6cc42c7e80dbece83722bc9882b4b98540b0dd",
            "size": 21022720, "component": "DirectXShaderCompiler",
        },
        "dxilconv.dll": {
            "sha256": "67cdc6d9ce9dad751fd8ac31a7cbee33808f392de64b7b3c42ee3d1c86f0853f",
            "size": 1571840, "component": "DirectXShaderCompiler",
        },
        "dxil-spirv.exe": {
            "sha256": "5d8d932e874c3629b324638edf1ae3fef10ff10fe67c62ad3b5191c5651b7fa7",
            "size": 3433984, "component": "dxil-spirv",
        },
        "dxil-spirv-c-shared.dll": {
            "sha256": "af612a739360a913251fa87ddf6249ed19096541e4fa74703404e191d2bb95f3",
            "size": 991232, "component": "dxil-spirv",
        },
        "spirv-cross.exe": {
            "sha256": "7d1dc7c8533c7a5d92fb929b63b92d5fda565d6dc49c775a54d97cc33cc58bcb",
            "size": 2179072, "component": "SPIRV-Cross",
        },
    },
    "components": {
        "DirectXShaderCompiler": {
            "repository": "https://github.com/microsoft/DirectXShaderCompiler",
            "revision": "4efa3dc842ac99a38d940aa64cb80819a7ebd49c",
            "version": "1.8.0.4988 (4efa3dc84)",
            "license": "University of Illinois/NCSA; see LICENSE.TXT and ThirdPartyNotices.txt",
        },
        "dxil-spirv": {
            "repository": "https://github.com/HansKristian-Work/dxil-spirv",
            "revision": "a696bb6626895dfa3118e903a6b54840878048ce",
            "version": "a696bb6626895dfa3118e903a6b54840878048ce",
            "license": "MIT and included third-party notices",
        },
        "SPIRV-Cross": {
            "repository": "https://github.com/KhronosGroup/SPIRV-Cross",
            "revision": "d8e3e2b141b8c8a167b2e3984736a6baacff316c",
            "version": "vulkan-sdk-1.4.321.0 (2026-04-18T19:32:51)",
            "license": "Apache-2.0",
        },
        "dxil-spirv/SPIRV-Cross": {
            "repository": "https://github.com/KhronosGroup/SPIRV-Cross",
            "revision": "bc491f6cf028894a848e56a681c6e2d944bac3c8",
            "version": "bc491f6cf028894a848e56a681c6e2d944bac3c8", "license": "Apache-2.0",
        },
        "SPIRV-Tools": {
            "repository": "https://github.com/KhronosGroup/SPIRV-Tools",
            "revision": "e9a8ceeddbf7e3aaadac2ab6f8a6ab6437872e88",
            "version": "e9a8ceeddbf7e3aaadac2ab6f8a6ab6437872e88", "license": "Apache-2.0",
        },
        "SPIRV-Headers": {
            "repository": "https://github.com/KhronosGroup/SPIRV-Headers",
            "revision": "3b9447dc98371e96b59a6225bd062a9867e1d203",
            "version": "3b9447dc98371e96b59a6225bd062a9867e1d203",
            "license": "Khronos MIT-style and per-file terms; see complete LICENSE",
        },
    },
    "notices": {
        "DirectXShaderCompiler-LICENSE.TXT": {
            "url": "https://raw.githubusercontent.com/microsoft/DirectXShaderCompiler/"
                   "4efa3dc842ac99a38d940aa64cb80819a7ebd49c/LICENSE.TXT",
            "sha256": "27a49e35d1da96eba18fba54bc882667ff0ff8c0254f16f2b6e165d605ba7df8",
            "size": 29241, "component": "DirectXShaderCompiler",
        },
        "DirectXShaderCompiler-ThirdPartyNotices.txt": {
            "url": "https://raw.githubusercontent.com/microsoft/DirectXShaderCompiler/"
                   "4efa3dc842ac99a38d940aa64cb80819a7ebd49c/ThirdPartyNotices.txt",
            "sha256": "19512a5d0a015ef16d167272c164da49a604614a786206212a80ad486ed0be6d",
            "size": 11256, "component": "DirectXShaderCompiler",
        },
        "dxil-spirv-LICENSE.MIT": {
            "url": "https://raw.githubusercontent.com/HansKristian-Work/dxil-spirv/"
                   "a696bb6626895dfa3118e903a6b54840878048ce/LICENSE.MIT",
            "sha256": "b3ec61378a66f09835a73f53926d9b5cf79a09c9e2fe8bdd0e39aa5403325e1e",
            "size": 3744, "component": "dxil-spirv",
        },
        "SPIRV-Cross-LICENSE": {
            "url": "https://raw.githubusercontent.com/KhronosGroup/SPIRV-Cross/"
                   "d8e3e2b141b8c8a167b2e3984736a6baacff316c/LICENSE",
            "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            "size": 11358, "component": "SPIRV-Cross",
        },
        "dxil-spirv-SPIRV-Cross-LICENSE": {
            "url": "https://raw.githubusercontent.com/KhronosGroup/SPIRV-Cross/"
                   "bc491f6cf028894a848e56a681c6e2d944bac3c8/LICENSE",
            "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            "size": 11358, "component": "dxil-spirv/SPIRV-Cross",
        },
        "SPIRV-Tools-LICENSE": {
            "url": "https://raw.githubusercontent.com/KhronosGroup/SPIRV-Tools/"
                   "e9a8ceeddbf7e3aaadac2ab6f8a6ab6437872e88/LICENSE",
            "sha256": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            "size": 11358, "component": "SPIRV-Tools",
        },
        "SPIRV-Headers-LICENSE": {
            "url": "https://raw.githubusercontent.com/KhronosGroup/SPIRV-Headers/"
                   "3b9447dc98371e96b59a6225bd062a9867e1d203/LICENSE",
            "sha256": "ff04ae7e6a468587fe59434fee2e67c0e2a749b592c794b764ff80f30b94a084",
            "size": 4812, "component": "SPIRV-Headers",
        },
    },
}

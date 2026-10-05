/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#pragma once

#include "common/common.h"

#if defined(RENDERDOC_PLATFORM_WIN32) && ENABLED(RDOC_X64)
#include <windows.h>

// Decode only the straight-line instructions used by Streamline's export stubs. Stop on an
// unknown instruction or a control-flow change instead of interpreting its operands as code.
inline const byte *FindStreamlineInstruction(const byte *code, size_t size, byte opcode,
                                             byte indirectModRM = 0)
{
  for(size_t offset = 0; offset < size;)
  {
    byte rex = 0;
    if((code[offset] & 0xf0) == 0x40)
    {
      rex = code[offset++];
      if(offset == size)
        return NULL;
    }

    const byte *instruction = code + offset;
    byte current = code[offset++];
    size_t immediate = 0;
    bool hasModRM = false;

    if(current == 0xe8)
      immediate = 4;
    else if(current >= 0x50 && current <= 0x5f)
    {
      // push/pop register, including the extended registers selected by REX.
    }
    else if(current == 0x90)
    {
      // nop
    }
    else if(current >= 0xb8 && current <= 0xbf)
      immediate = (rex & 8) ? 8 : 4;
    else if(current == 0x89 || current == 0x8b || current == 0x8d || current == 0xff)
      hasModRM = true;
    else if(current == 0x81 || current == 0x83 || current == 0xc7)
    {
      hasModRM = true;
      immediate = current == 0x83 ? 1 : 4;
    }
    else
      return NULL;

    byte modrm = 0;
    if(hasModRM)
    {
      if(offset == size)
        return NULL;

      modrm = code[offset++];
      byte mod = modrm >> 6;
      byte rm = modrm & 7;
      size_t displacement = mod == 1 ? 1 : mod == 2 ? 4 : 0;

      if(current == 0xc7 && (modrm & 0x38) != 0)
        return NULL;

      if(mod != 3 && rm == 4)
      {
        if(offset == size)
          return NULL;
        byte sib = code[offset++];
        if(mod == 0 && (sib & 7) == 5)
          displacement = 4;
      }
      else if(mod == 0 && rm == 5)
      {
        displacement = 4;
      }

      if(displacement > size - offset)
        return NULL;
      offset += displacement;
    }

    if(immediate > size - offset)
      return NULL;
    offset += immediate;

    if(current == opcode && (current != 0xff || modrm == indirectModRM))
      return instruction;

    // Only follow the export's direct initialisation call. An indirect transfer is the end of
    // the stub; do not continue into a different path or the next function.
    if(current == 0xff)
      return NULL;
  }

  return NULL;
}

inline void *FindStreamlineInitialiser(void *entry)
{
  const byte *instruction = FindStreamlineInstruction((const byte *)entry, 48, 0xe8);
  if(instruction == NULL)
    return NULL;

  int32_t relativeOffset = 0;
  memcpy(&relativeOffset, instruction + 1, sizeof(relativeOffset));
  return (void *)((uintptr_t)instruction + 5 + relativeOffset);
}

inline void **FindStreamlineTargetSlot(void *entry, byte modrm)
{
  // Streamline exports load the real API module, then call or jump through a RIP-relative
  // function pointer. Locate that pointer without relying on a version-specific RVA.
  const byte *instruction = FindStreamlineInstruction((const byte *)entry, 96, 0xff, modrm);
  if(instruction == NULL)
    return NULL;

  int32_t relativeOffset = 0;
  memcpy(&relativeOffset, instruction + 2, sizeof(relativeOffset));
  return (void **)((uintptr_t)instruction + 6 + relativeOffset);
}

inline bool IsStreamlineRange(HMODULE module, const void *address, size_t size, bool executable)
{
  MEMORY_BASIC_INFORMATION info = {};
  if(address == NULL || VirtualQuery(address, &info, sizeof(info)) != sizeof(info) ||
     info.AllocationBase != module || info.State != MEM_COMMIT ||
     (info.Protect & (PAGE_GUARD | PAGE_NOACCESS)) != 0)
    return false;

  uintptr_t offset = (uintptr_t)address - (uintptr_t)info.BaseAddress;
  if(offset >= info.RegionSize || size > info.RegionSize - offset)
    return false;

  DWORD protection = info.Protect & 0xff;
  bool readableCode = protection == PAGE_EXECUTE_READ || protection == PAGE_EXECUTE_READWRITE ||
                      protection == PAGE_EXECUTE_WRITECOPY;
  return executable ? readableCode
                    : readableCode || protection == PAGE_READONLY || protection == PAGE_READWRITE ||
                          protection == PAGE_WRITECOPY;
}

inline bool PatchStreamlineTarget(void **slot, void *replacement)
{
  DWORD oldProtection = 0;
  if(slot == NULL || *slot == NULL ||
     !VirtualProtect(slot, sizeof(void *), PAGE_READWRITE, &oldProtection))
    return false;

  *slot = replacement;

  DWORD dummy = 0;
  VirtualProtect(slot, sizeof(void *), oldProtection, &dummy);
  return true;
}
#endif

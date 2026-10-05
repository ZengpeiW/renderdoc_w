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

#include "d3d12_hooks.h"
#include "driver/dxgi/dxgi_wrapped.h"
#include "hooks/hooks.h"
#include "hooks/streamline_hooks.h"
#include "serialise/serialiser.h"
#include "d3d12_command_queue.h"
#include "d3d12_device.h"
#include "d3d12_replay.h"
#include "d3d12_shader_cache.h"

#include "driver/dx/official/D3D11On12On7.h"

typedef HRESULT(WINAPI *PFN_D3D12_ENABLE_EXPERIMENTAL_FEATURES)(UINT NumFeatures, const IID *pIIDs,
                                                                void *pConfigurationStructs,
                                                                UINT *pConfigurationStructSizes);

ID3DDevice *GetD3D12DeviceIfAlloc(IUnknown *dev)
{
  if(WrappedID3D12CommandQueue::IsAlloc(dev))
    return (WrappedID3D12CommandQueue *)dev;

  if(WrappedID3D12Device::IsAlloc(dev))
    return (WrappedID3D12Device *)dev;

  return NULL;
}

class WrappedD3D11On12On7 : public RefCounter12<ID3D11On12On7>
{
public:
  WrappedD3D11On12On7(ID3D11On12On7 *real) : RefCounter12(real) {}
  virtual ~WrappedD3D11On12On7() {}
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)this;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D11On12On7

  // Enables usage similar to D3D11On12CreateDevice.
  void STDMETHODCALLTYPE SetThreadDeviceCreationParams(ID3D12Device *pDevice,
                                                       ID3D12CommandQueue *pGraphicsQueue)
  {
    RDCASSERT(WrappedID3D12Device::IsAlloc(pDevice));
    m_pReal->SetThreadDeviceCreationParams(((WrappedID3D12Device *)pDevice)->GetReal(),
                                           Unwrap(pGraphicsQueue));
  }

  // Enables usage similar to ID3D11On12Device::CreateWrappedResource.
  // Note that the D3D11 resource creation parameters should be similar to the D3D12 resource,
  // or else unexpected/undefined behavior may occur.
  void STDMETHODCALLTYPE SetThreadResourceCreationParams(ID3D12Resource *pResource)
  {
    m_pReal->SetThreadResourceCreationParams(Unwrap(pResource));
  }

  ID3D11On12On7Device *STDMETHODCALLTYPE GetThreadLastCreatedDevice()
  {
    // don't need to wrap/unwrap, it only deals with ID3D11On12On7Resource
    return m_pReal->GetThreadLastCreatedDevice();
  }

  ID3D11On12On7Resource *STDMETHODCALLTYPE GetThreadLastCreatedResource()
  {
    // don't need to wrap/unwrap
    return m_pReal->GetThreadLastCreatedResource();
  }
};

// dummy class to present to the user, while we maintain control
//
// The inheritance is awful for these. See WrappedID3D12DebugDevice for why there are multiple
// parent classes
class WrappedID3D12Debug : public RefCounter12<ID3D12Debug>,
                           public ID3D12Debug6,
                           public ID3D12Debug1,
                           public ID3D12Debug2
{
public:
  WrappedID3D12Debug() : RefCounter12(NULL) {}
  virtual ~WrappedID3D12Debug() {}
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)(ID3D12Debug *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug))
    {
      *ppvObject = (ID3D12Debug *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug1))
    {
      *ppvObject = (ID3D12Debug1 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug2))
    {
      *ppvObject = (ID3D12Debug2 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug3))
    {
      *ppvObject = (ID3D12Debug3 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug4))
    {
      *ppvObject = (ID3D12Debug4 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug5))
    {
      *ppvObject = (ID3D12Debug5 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Debug6))
    {
      *ppvObject = (ID3D12Debug6 *)this;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D12Debug / ID3D12Debug1
  virtual void STDMETHODCALLTYPE EnableDebugLayer() {}
  //////////////////////////////
  // Implement ID3D12Debug1 / ID3D12Debug3
  virtual void STDMETHODCALLTYPE SetEnableGPUBasedValidation(BOOL Enable) {}
  virtual void STDMETHODCALLTYPE SetEnableSynchronizedCommandQueueValidation(BOOL Enable) {}
  // Implement ID3D12Debug2 / ID3D12Debug3
  virtual void STDMETHODCALLTYPE SetGPUBasedValidationFlags(D3D12_GPU_BASED_VALIDATION_FLAGS Flags)
  {
  }
  //////////////////////////////
  // Implement ID3D12Debug4
  virtual void STDMETHODCALLTYPE DisableDebugLayer(void) {}
  //////////////////////////////
  // Implement ID3D12Debug5
  virtual void STDMETHODCALLTYPE SetEnableAutoName(BOOL Enable) {}
  //////////////////////////////
  // Implement ID3D12Debug6
  virtual void STDMETHODCALLTYPE SetForceLegacyBarrierValidation(BOOL Enable) {}
};

class WrappedID3D12Tools : public RefCounter12<ID3D12Tools2>, public ID3D12Tools2
{
  BOOL m_Instrumentation = FALSE;
  ID3D12Tools1 *m_Tools1 = NULL;
  ID3D12Tools2 *m_Tools2 = NULL;
public:
  WrappedID3D12Tools(ID3D12Tools *tools) : RefCounter12(NULL)
  {
    tools->QueryInterface(__uuidof(ID3D12Tools1), (void **)&m_Tools1);
    tools->QueryInterface(__uuidof(ID3D12Tools2), (void **)&m_Tools2);
  }
  virtual ~WrappedID3D12Tools()
  {
    SAFE_RELEASE(m_Tools1);
    SAFE_RELEASE(m_Tools2);
  }
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Tools))
    {
      *ppvObject = (ID3D12Tools *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Tools1))
    {
      *ppvObject = (ID3D12Tools1 *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12Tools2))
    {
      *ppvObject = (ID3D12Tools2 *)this;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D12Tools
  virtual void STDMETHODCALLTYPE EnableShaderInstrumentation(BOOL bEnable)
  {
    m_Instrumentation = bEnable;
  }

  virtual BOOL STDMETHODCALLTYPE ShaderInstrumentationEnabled(void) { return m_Instrumentation; }

  //////////////////////////////
  // Implement ID3D12Tools1
  virtual HRESULT STDMETHODCALLTYPE ReserveGPUVARangesAtCreate(D3D12_GPU_VIRTUAL_ADDRESS_RANGE *pRanges,
                                                               UINT uiNumRanges)
  {
    if(m_Tools1)
      return m_Tools1->ReserveGPUVARangesAtCreate(pRanges, uiNumRanges);
    return E_NOINTERFACE;
  }

  virtual void STDMETHODCALLTYPE ClearReservedGPUVARangesList(void)
  {
    if(m_Tools1)
      m_Tools1->ClearReservedGPUVARangesList();
  }

  //////////////////////////////
  // Implement ID3D12Tools2
  virtual HRESULT STDMETHODCALLTYPE SetApplicationSpecificDriverState(_In_ IUnknown *pAdapter,
                                                                      _In_opt_ ID3DBlob *pBlob)
  {
    if(m_Tools2)
      return m_Tools2->SetApplicationSpecificDriverState(pAdapter, pBlob);
    return E_NOINTERFACE;
  }
};

class WrappedID3D12DeviceRemovedExtendedData : public RefCounter12<ID3D12DeviceRemovedExtendedData1>,
                                               public ID3D12DeviceRemovedExtendedData1
{
public:
  WrappedID3D12DeviceRemovedExtendedData() : RefCounter12(NULL) {}
  virtual ~WrappedID3D12DeviceRemovedExtendedData() {}
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12DeviceRemovedExtendedData))
    {
      *ppvObject = (ID3D12DeviceRemovedExtendedData *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12DeviceRemovedExtendedData1))
    {
      *ppvObject = (ID3D12DeviceRemovedExtendedData1 *)this;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D12DeviceRemovedExtendedData
  virtual HRESULT STDMETHODCALLTYPE
  GetAutoBreadcrumbsOutput(_Out_ D3D12_DRED_AUTO_BREADCRUMBS_OUTPUT *pOutput)
  {
    return DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
  }

  virtual HRESULT STDMETHODCALLTYPE
  GetPageFaultAllocationOutput(_Out_ D3D12_DRED_PAGE_FAULT_OUTPUT *pOutput)
  {
    return DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
  }

  //////////////////////////////
  // Implement ID3D12DeviceRemovedExtendedData1
  virtual HRESULT STDMETHODCALLTYPE
  GetAutoBreadcrumbsOutput1(_Out_ D3D12_DRED_AUTO_BREADCRUMBS_OUTPUT1 *pOutput)
  {
    return DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
  }

  virtual HRESULT STDMETHODCALLTYPE
  GetPageFaultAllocationOutput1(_Out_ D3D12_DRED_PAGE_FAULT_OUTPUT1 *pOutput)
  {
    return DXGI_ERROR_NOT_CURRENTLY_AVAILABLE;
  }
};

class WrappedID3D12DeviceFactory : public RefCounter12<ID3D12DeviceFactory>, public ID3D12DeviceFactory
{
  WrappedID3D12DeviceConfiguration config;
public:
  WrappedID3D12DeviceFactory(ID3D12DeviceFactory *real) : RefCounter12(real), config(real, this) {}

  virtual ~WrappedID3D12DeviceFactory() {}
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)(ID3D12DeviceFactory *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12DeviceFactory))
    {
      *ppvObject = (ID3D12DeviceFactory *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12DeviceConfiguration) && config.IsValid())
    {
      *ppvObject = (ID3D12DeviceConfiguration *)&config;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12DeviceConfiguration1) && config.IsValid1())
    {
      *ppvObject = (ID3D12DeviceConfiguration1 *)&config;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D12DeviceFactory

  virtual HRESULT STDMETHODCALLTYPE InitializeFromGlobalState(void)
  {
    return m_pReal->InitializeFromGlobalState();
  }

  virtual HRESULT STDMETHODCALLTYPE ApplyToGlobalState(void)
  {
    return m_pReal->ApplyToGlobalState();
  }

  virtual HRESULT STDMETHODCALLTYPE SetFlags(D3D12_DEVICE_FACTORY_FLAGS flags)
  {
    return m_pReal->SetFlags(flags);
  }

  virtual D3D12_DEVICE_FACTORY_FLAGS STDMETHODCALLTYPE GetFlags(void)
  {
    return m_pReal->GetFlags();
  }

  virtual HRESULT STDMETHODCALLTYPE GetConfigurationInterface(REFCLSID clsid, REFIID iid,
                                                              _COM_Outptr_ void **ppv);

  virtual HRESULT STDMETHODCALLTYPE
  EnableExperimentalFeatures(UINT NumFeatures, _In_reads_(NumFeatures) const IID *pIIDs,
                             _In_reads_opt_(NumFeatures) void *pConfigurationStructs,
                             _In_reads_opt_(NumFeatures) UINT *pConfigurationStructSizes)
  {
    rdcarray<IID> allowedIIDs;

    // allow enabling unsigned DXIL, and GPU upload heaps on most windows versions
    for(UINT i = 0; i < NumFeatures; i++)
    {
      if(pIIDs[i] == D3D12ExperimentalShaderModels)
        allowedIIDs.push_back(D3D12ExperimentalShaderModels);
      else if(pIIDs[i] == D3D12GPUUploadHeapsOnUnsupportedOS)
        allowedIIDs.push_back(D3D12GPUUploadHeapsOnUnsupportedOS);
    }

    // there's no "partially successful" error code, so we just lie to the application and pretend
    // that any filtered IIDs also succeeded
    if(!allowedIIDs.empty())
      return m_pReal->EnableExperimentalFeatures((UINT)allowedIIDs.size(), allowedIIDs.data(), NULL,
                                                 NULL);

    // header says "The call returns E_NOINTERFACE if an unrecognized feature is passed in or
    // Windows Developer mode is not on." so this is the most appropriate error for if no IIDs are
    // allowed.
    return E_NOINTERFACE;
  }

  virtual HRESULT STDMETHODCALLTYPE CreateDevice(_In_opt_ IUnknown *adapter,
                                                 D3D_FEATURE_LEVEL FeatureLevel, REFIID riid,
                                                 _COM_Outptr_opt_ void **ppvDevice)
  {
    if(RenderDoc::Inst().GetCaptureOptions().apiValidation)
    {
      D3D12DevConfiguration tmpConfig = {};
      HRESULT hr = m_pReal->GetConfigurationInterface(CLSID_D3D12Debug, __uuidof(ID3D12Debug),
                                                      (void **)&tmpConfig.debug);
      if(SUCCEEDED(hr))
      {
        EnableD3D12DebugLayer(&tmpConfig, NULL);
        SAFE_RELEASE(tmpConfig.debug);
      }
    }

    D3D12DevConfiguration devConfig;
    devConfig.devfactory = this;
    devConfig.devconfig = &config;

    HRESULT ret = CreateD3D12_Internal(
        [this](IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
               void **ppDevice) {
          return m_pReal->CreateDevice(pAdapter, MinimumFeatureLevel, riid, ppDevice);
        },
        &devConfig, adapter, FeatureLevel, riid, ppvDevice);

    return ret;
  }
};

class WrappedID3D12SDKConfiguration : public RefCounter12<ID3D12SDKConfiguration>,
                                      public ID3D12SDKConfiguration1
{
  ID3D12SDKConfiguration1 *m_pReal1 = NULL;
public:
  WrappedID3D12SDKConfiguration(ID3D12SDKConfiguration *real, ID3D12SDKConfiguration1 *real1)
      : RefCounter12(real)
  {
    if(!real1)
      real->QueryInterface(__uuidof(ID3D12SDKConfiguration1), (void **)&real1);
    m_pReal1 = real1;
  }
  virtual ~WrappedID3D12SDKConfiguration()
  {
    SAFE_RELEASE(m_pReal);
    SAFE_RELEASE(m_pReal1);
  }
  //////////////////////////////
  // Implement IUnknown
  ULONG STDMETHODCALLTYPE AddRef() { return RefCounter12::AddRef(); }
  ULONG STDMETHODCALLTYPE Release() { return RefCounter12::Release(); }
  HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppvObject)
  {
    if(riid == __uuidof(IUnknown))
    {
      *ppvObject = (IUnknown *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12SDKConfiguration))
    {
      *ppvObject = (ID3D12SDKConfiguration *)this;
      AddRef();
      return S_OK;
    }
    if(riid == __uuidof(ID3D12SDKConfiguration1))
    {
      *ppvObject = (ID3D12SDKConfiguration1 *)this;
      AddRef();
      return S_OK;
    }

    return E_NOINTERFACE;
  }

  //////////////////////////////
  // Implement ID3D12SDKConfiguration
  virtual HRESULT STDMETHODCALLTYPE SetSDKVersion(UINT SDKVersion, _In_z_ LPCSTR SDKPath)
  {
    return m_pReal->SetSDKVersion(SDKVersion, SDKPath);
  }

  //////////////////////////////
  // Implement ID3D12SDKConfiguration1
  virtual HRESULT STDMETHODCALLTYPE CreateDeviceFactory(UINT SDKVersion, _In_ LPCSTR SDKPath,
                                                        REFIID riid, _COM_Outptr_ void **ppvFactory)
  {
    if(riid != __uuidof(ID3D12DeviceFactory))
    {
      RDCERR("Unexpected uuid to CreateDeviceFactory: %s", ToStr(riid).c_str());
      return E_NOINTERFACE;
    }

    ID3D12DeviceFactory *realFactory = NULL;
    HRESULT hr = m_pReal1->CreateDeviceFactory(SDKVersion, SDKPath, riid, (void **)&realFactory);
    if(SUCCEEDED(hr))
    {
      RDCASSERT(realFactory);
      *ppvFactory = (ID3D12DeviceFactory *)(new WrappedID3D12DeviceFactory(realFactory));
      return hr;
    }
    SAFE_RELEASE(realFactory);
    return hr;
  }

  virtual void STDMETHODCALLTYPE FreeUnusedSDKs(void) { return m_pReal1->FreeUnusedSDKs(); }
};

class D3D12Hook : LibraryHook
{
public:
  void RegisterHooks()
  {
    RDCLOG("Registering D3D12 hooks");

    WrappedIDXGISwapChain4::RegisterD3DDeviceCallback(GetD3D12DeviceIfAlloc);

    // also require d3dcompiler_??.dll
    if(GetD3DCompiler() == NULL)
    {
      RDCERR("Failed to load d3dcompiler_??.dll - not inserting D3D12 hooks.");
      return;
    }

    LibraryHooks::RegisterLibraryHook("d3d12.dll", NULL);
#if ENABLED(RDOC_X64)
    LibraryHooks::RegisterLibraryHook("sl.interposer.dll", [](void *, const char *) {
      d3d12hooks.PatchStreamlineHooks();
    });
#else
    LibraryHooks::RegisterLibraryHook("sl.interposer.dll", NULL);
#endif

    CreateDevice.Register("d3d12.dll", "D3D12CreateDevice", D3D12CreateDevice_hook);
    GetDebugInterface.Register("d3d12.dll", "D3D12GetDebugInterface", D3D12GetDebugInterface_hook);
    GetInterface.Register("d3d12.dll", "D3D12GetInterface", D3D12GetInterface_hook);
    EnableExperimentalFeatures.Register("d3d12.dll", "D3D12EnableExperimentalFeatures",
                                        D3D12EnableExperimentalFeatures_hook);
    GetD3D11On12On7.Register("d3d11on12.dll", "GetD3D11On12On7Interface",
                             GetD3D11On12On7Interface_hook);

    m_RecurseSlot = Threading::AllocateTLSSlot();
    Threading::SetTLSValue(m_RecurseSlot, NULL);
  }

  static HRESULT GetWrappedInterface(IUnknown *realUnk, REFIID riid, void **ppvInterface)
  {
    if(riid == __uuidof(ID3D12Debug))
    {
      *ppvInterface = (ID3D12Debug *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug1))
    {
      *ppvInterface = (ID3D12Debug1 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug2))
    {
      *ppvInterface = (ID3D12Debug2 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug3))
    {
      *ppvInterface = (ID3D12Debug3 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug4))
    {
      *ppvInterface = (ID3D12Debug4 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug5))
    {
      *ppvInterface = (ID3D12Debug5 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Debug6))
    {
      *ppvInterface = (ID3D12Debug6 *)(new WrappedID3D12Debug());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Tools))
    {
      ID3D12Tools *real = (ID3D12Tools *)realUnk;
      // don't need to addref real here, WrappedID3D12Tools doesn't hold onto it but just uses it for QueryInterface
      *ppvInterface = (ID3D12Tools *)(new WrappedID3D12Tools(real));
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Tools1))
    {
      ID3D12Tools1 *real = (ID3D12Tools1 *)realUnk;
      // don't need to addref real here, WrappedID3D12Tools doesn't hold onto it but just uses it for QueryInterface
      *ppvInterface = (ID3D12Tools1 *)(new WrappedID3D12Tools(real));
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12Tools2))
    {
      ID3D12Tools2 *real = (ID3D12Tools2 *)realUnk;
      // don't need to addref real here, WrappedID3D12Tools doesn't hold onto it but just uses it for QueryInterface
      *ppvInterface = (ID3D12Tools2 *)(new WrappedID3D12Tools(real));
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12DeviceRemovedExtendedData))
    {
      *ppvInterface =
          (ID3D12DeviceRemovedExtendedData *)(new WrappedID3D12DeviceRemovedExtendedData());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12DeviceRemovedExtendedData1))
    {
      *ppvInterface =
          (ID3D12DeviceRemovedExtendedData1 *)(new WrappedID3D12DeviceRemovedExtendedData());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12DeviceRemovedExtendedData2))
    {
      *ppvInterface =
          (ID3D12DeviceRemovedExtendedData2 *)(new WrappedID3D12DeviceRemovedExtendedData());
      return S_OK;
    }
    else if(riid == __uuidof(ID3D12SDKConfiguration))
    {
      ID3D12SDKConfiguration *real = (ID3D12SDKConfiguration *)realUnk;
      if(real)
      {
        // take a reference ourselves, realUnk is a transient pointer and will be released after this function returns
        real->AddRef();
        *ppvInterface = (ID3D12SDKConfiguration *)(new WrappedID3D12SDKConfiguration(real, NULL));
        return S_OK;
      }
    }
    else if(riid == __uuidof(ID3D12SDKConfiguration1))
    {
      ID3D12SDKConfiguration1 *real1 = (ID3D12SDKConfiguration1 *)realUnk;
      if(real1)
      {
        // take a reference ourselves, realUnk is a transient pointer and will be released after this function returns
        real1->AddRef();
        ID3D12SDKConfiguration *real = NULL;
        real1->QueryInterface(__uuidof(ID3D12SDKConfiguration), (void **)&real);
        *ppvInterface = (ID3D12SDKConfiguration1 *)(new WrappedID3D12SDKConfiguration(real, real1));
        return S_OK;
      }
    }

    return E_NOINTERFACE;
  }

private:
  static D3D12Hook d3d12hooks;

  HookedFunction<PFN_D3D12_GET_DEBUG_INTERFACE> GetDebugInterface;
  HookedFunction<PFN_D3D12_GET_INTERFACE> GetInterface;
  HookedFunction<PFN_D3D12_CREATE_DEVICE> CreateDevice;
  HookedFunction<PFN_D3D12_GET_INTERFACE> StreamlineGetInterface;
  HookedFunction<PFN_D3D12_CREATE_DEVICE> StreamlineCreateDevice;
  HookedFunction<PFN_D3D12_ENABLE_EXPERIMENTAL_FEATURES> EnableExperimentalFeatures;
  HookedFunction<PFNGetD3D11On12On7Interface> GetD3D11On12On7;

  bool m_StreamlinePatched = false;

#if ENABLED(RDOC_X64)
  typedef void(WINAPI *PFN_STREAMLINE_INITIALISE_D3D12)();

  void PatchStreamlineHooks()
  {
    if(m_StreamlinePatched)
      return;

    HMODULE streamline = GetModuleHandleA("sl.interposer.dll");
    if(streamline == NULL)
      return;

    PFN_D3D12_CREATE_DEVICE createDevice =
        (PFN_D3D12_CREATE_DEVICE)GetProcAddress(streamline, "D3D12CreateDevice");
    PFN_D3D12_GET_INTERFACE getInterface =
        (PFN_D3D12_GET_INTERFACE)GetProcAddress(streamline, "D3D12GetInterface");

    if(createDevice == NULL || getInterface == NULL)
    {
      RDCWARN("Streamline D3D12 exports were not found");
      return;
    }

    if(!IsStreamlineRange(streamline, (void *)createDevice, 96, true) ||
       !IsStreamlineRange(streamline, (void *)getInterface, 96, true))
    {
      RDCWARN("Streamline D3D12 exports are outside readable executable memory");
      return;
    }

    void **createSlot = FindStreamlineTargetSlot((void *)createDevice, 0x15);
    void **interfaceSlot = FindStreamlineTargetSlot((void *)getInterface, 0x25);

    if(!IsStreamlineRange(streamline, createSlot, sizeof(void *), false) ||
       !IsStreamlineRange(streamline, interfaceSlot, sizeof(void *), false) ||
       (uintptr_t)createSlot % sizeof(void *) != 0 || (uintptr_t)interfaceSlot % sizeof(void *) != 0)
    {
      RDCWARN("Couldn't locate Streamline D3D12 onward function pointers");
      return;
    }

    if(*createSlot == NULL || *interfaceSlot == NULL)
    {
      PFN_STREAMLINE_INITIALISE_D3D12 initialise =
          (PFN_STREAMLINE_INITIALISE_D3D12)FindStreamlineInitialiser((void *)createDevice);

      if(initialise == NULL ||
         initialise !=
             (PFN_STREAMLINE_INITIALISE_D3D12)FindStreamlineInitialiser((void *)getInterface) ||
         !IsStreamlineRange(streamline, (void *)initialise, 1, true))
      {
        RDCWARN("Couldn't locate Streamline D3D12 initialiser");
        return;
      }

      // Streamline resolves d3d12.dll by walking its export table directly, so GetProcAddress hooks
      // don't see these lookups. Run only its resolver, without creating a probe device.
      initialise();
    }

    if(*createSlot == NULL || *interfaceSlot == NULL)
    {
      RDCWARN("Streamline D3D12 onward function pointers are still unresolved");
      return;
    }

    if(*createSlot == (void *)&StreamlineD3D12CreateDevice_hook ||
       *interfaceSlot == (void *)&StreamlineD3D12GetInterface_hook)
    {
      m_StreamlinePatched = true;
      return;
    }

    StreamlineCreateDevice.SetFuncPtr(*createSlot);
    StreamlineGetInterface.SetFuncPtr(*interfaceSlot);

    if(!PatchStreamlineTarget(createSlot, (void *)&StreamlineD3D12CreateDevice_hook) ||
       !PatchStreamlineTarget(interfaceSlot, (void *)&StreamlineD3D12GetInterface_hook))
    {
      RDCWARN("Couldn't patch Streamline D3D12 onward function pointers");
      return;
    }

    m_StreamlinePatched = true;
    RDCLOG("Patched Streamline D3D12 onward function pointers");
  }
#endif

  // re-entrancy detection (can happen in rare cases with e.g. fraps)
  uint64_t m_RecurseSlot = 0;

  void EndRecurse() { Threading::SetTLSValue(m_RecurseSlot, NULL); }
  bool CheckRecurse()
  {
    if(Threading::GetTLSValue(m_RecurseSlot) == NULL)
    {
      Threading::SetTLSValue(m_RecurseSlot, (void *)1);
      return false;
    }

    return true;
  }

  friend HRESULT CreateD3D12_Internal(RealD3D12CreateFunction real, D3D12DevConfiguration *devConfig,
                                      IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel,
                                      REFIID riid, void **ppDevice);

  HRESULT Create_Internal(RealD3D12CreateFunction real, D3D12DevConfiguration *devConfig,
                          IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
                          void **ppDevice)
  {
    // if we're already inside a wrapped create i.e. this function, then DON'T do anything
    // special. Just grab the trampolined function and call it.
    if(CheckRecurse())
      return real(pAdapter, MinimumFeatureLevel, riid, ppDevice);

    if(riid != __uuidof(ID3D12Device) && riid != __uuidof(ID3D12Device1) &&
       riid != __uuidof(ID3D12Device2) && riid != __uuidof(ID3D12Device3) &&
       riid != __uuidof(ID3D12Device4) && riid != __uuidof(ID3D12Device5) &&
       riid != __uuidof(ID3D12Device6) && riid != __uuidof(ID3D12Device7) &&
       riid != __uuidof(ID3D12Device8) && riid != __uuidof(ID3D12Device9) &&
       riid != __uuidof(ID3D12Device10) && riid != __uuidof(ID3D12Device11) &&
       riid != __uuidof(ID3D12Device12) && riid != __uuidof(ID3D12Device13) &&
       riid != __uuidof(ID3D12Device14) && riid != __uuidof(ID3D12Device15))
    {
      RDCERR("Unsupported UUID %s for D3D12CreateDevice", ToStr(riid).c_str());
      return E_NOINTERFACE;
    }

    RDCDEBUG("Call to Create_Internal Feature Level %x", MinimumFeatureLevel, ToStr(riid).c_str());

    // we should no longer go through here in the replay application
    RDCASSERT(!RenderDoc::Inst().IsReplayApp());

    bool EnableDebugLayer = false;

    if(RenderDoc::Inst().GetCaptureOptions().apiValidation)
      EnableDebugLayer = EnableD3D12DebugLayer(NULL, GetDebugInterface());

    RDCDEBUG("Calling real createdevice...");

    HRESULT ret = real(pAdapter, MinimumFeatureLevel, riid, ppDevice);

    RDCDEBUG("Called real createdevice... HRESULT: %s", ToStr(ret).c_str());

    if(SUCCEEDED(ret) && ppDevice)
    {
      RDCDEBUG("succeeded and hooking.");

      if(!WrappedID3D12Device::IsAlloc(*ppDevice))
      {
        D3D12InitParams params;
        params.MinimumFeatureLevel = MinimumFeatureLevel;

        ID3D12Device *dev = (ID3D12Device *)*ppDevice;

        if(riid == __uuidof(ID3D12Device1))
        {
          ID3D12Device1 *dev1 = (ID3D12Device1 *)*ppDevice;
          dev = (ID3D12Device *)dev1;
        }
        else if(riid == __uuidof(ID3D12Device2))
        {
          ID3D12Device2 *dev2 = (ID3D12Device2 *)*ppDevice;
          dev = (ID3D12Device *)dev2;
        }
        else if(riid == __uuidof(ID3D12Device3))
        {
          ID3D12Device3 *dev3 = (ID3D12Device3 *)*ppDevice;
          dev = (ID3D12Device *)dev3;
        }
        else if(riid == __uuidof(ID3D12Device4))
        {
          ID3D12Device4 *dev4 = (ID3D12Device4 *)*ppDevice;
          dev = (ID3D12Device *)dev4;
        }
        else if(riid == __uuidof(ID3D12Device5))
        {
          ID3D12Device5 *dev5 = (ID3D12Device5 *)*ppDevice;
          dev = (ID3D12Device *)dev5;
        }
        else if(riid == __uuidof(ID3D12Device6))
        {
          ID3D12Device6 *dev6 = (ID3D12Device6 *)*ppDevice;
          dev = (ID3D12Device *)dev6;
        }
        else if(riid == __uuidof(ID3D12Device7))
        {
          ID3D12Device7 *dev7 = (ID3D12Device7 *)*ppDevice;
          dev = (ID3D12Device *)dev7;
        }
        else if(riid == __uuidof(ID3D12Device8))
        {
          ID3D12Device8 *dev8 = (ID3D12Device8 *)*ppDevice;
          dev = (ID3D12Device *)dev8;
        }
        else if(riid == __uuidof(ID3D12Device9))
        {
          ID3D12Device9 *dev9 = (ID3D12Device9 *)*ppDevice;
          dev = (ID3D12Device *)dev9;
        }
        else if(riid == __uuidof(ID3D12Device10))
        {
          ID3D12Device10 *dev10 = (ID3D12Device10 *)*ppDevice;
          dev = (ID3D12Device *)dev10;
        }
        else if(riid == __uuidof(ID3D12Device11))
        {
          ID3D12Device11 *dev11 = (ID3D12Device11 *)*ppDevice;
          dev = (ID3D12Device *)dev11;
        }
        else if(riid == __uuidof(ID3D12Device12))
        {
          ID3D12Device12 *dev12 = (ID3D12Device12 *)*ppDevice;
          dev = (ID3D12Device *)dev12;
        }
        else if(riid == __uuidof(ID3D12Device13))
        {
          ID3D12Device13 *dev13 = (ID3D12Device13 *)*ppDevice;
          dev = (ID3D12Device *)dev13;
        }
        else if(riid == __uuidof(ID3D12Device14))
        {
          ID3D12Device14 *dev14 = (ID3D12Device14 *)*ppDevice;
          dev = (ID3D12Device *)dev14;
        }
        else if(riid == __uuidof(ID3D12Device15))
        {
          ID3D12Device15 *dev15 = (ID3D12Device15 *)*ppDevice;
          dev = (ID3D12Device *)dev15;
        }

        WrappedID3D12Device *wrap = WrappedID3D12Device::Create(dev, params, EnableDebugLayer);

        if(devConfig)
        {
          D3D12DevConfiguration *cfg = new D3D12DevConfiguration(*devConfig);
          wrap->GetReplay()->SetDevConfiguration(cfg);
        }

        RDCDEBUG("created wrapped device.");

        *ppDevice = (ID3D12Device *)wrap;

        if(riid == __uuidof(ID3D12Device1))
          *ppDevice = (ID3D12Device1 *)wrap;
        else if(riid == __uuidof(ID3D12Device2))
          *ppDevice = (ID3D12Device2 *)wrap;
        else if(riid == __uuidof(ID3D12Device3))
          *ppDevice = (ID3D12Device3 *)wrap;
        else if(riid == __uuidof(ID3D12Device4))
          *ppDevice = (ID3D12Device4 *)wrap;
        else if(riid == __uuidof(ID3D12Device5))
          *ppDevice = (ID3D12Device5 *)wrap;
        else if(riid == __uuidof(ID3D12Device6))
          *ppDevice = (ID3D12Device6 *)wrap;
        else if(riid == __uuidof(ID3D12Device7))
          *ppDevice = (ID3D12Device7 *)wrap;
        else if(riid == __uuidof(ID3D12Device8))
          *ppDevice = (ID3D12Device8 *)wrap;
        else if(riid == __uuidof(ID3D12Device9))
          *ppDevice = (ID3D12Device9 *)wrap;
        else if(riid == __uuidof(ID3D12Device10))
          *ppDevice = (ID3D12Device10 *)wrap;
        else if(riid == __uuidof(ID3D12Device11))
          *ppDevice = (ID3D12Device11 *)wrap;
        else if(riid == __uuidof(ID3D12Device12))
          *ppDevice = (ID3D12Device12 *)wrap;
        else if(riid == __uuidof(ID3D12Device13))
          *ppDevice = (ID3D12Device13 *)wrap;
        else if(riid == __uuidof(ID3D12Device14))
          *ppDevice = (ID3D12Device14 *)wrap;
        else if(riid == __uuidof(ID3D12Device15))
          *ppDevice = (ID3D12Device15 *)wrap;
      }
    }
    else if(SUCCEEDED(ret))
    {
      RDCLOG("Created wrapped D3D12 device.");
    }
    else
    {
      RDCDEBUG("failed. HRESULT: %s", ToStr(ret).c_str());
    }

    EndRecurse();

    return ret;
  }

  static HRESULT WINAPI D3D12CreateDevice_hook(IUnknown *pAdapter,
                                               D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
                                               void **ppDevice)
  {
    PFN_D3D12_CREATE_DEVICE createFunc = d3d12hooks.CreateDevice();

    if(!createFunc)
    {
      HMODULE d3d12 = GetModuleHandleA("d3d12.dll");

      if(d3d12)
        createFunc = (PFN_D3D12_CREATE_DEVICE)GetProcAddress(d3d12, "D3D12CreateDevice");

      if(!createFunc)
      {
        RDCERR("Something went seriously wrong, d3d12.dll couldn't be loaded!");
        return E_UNEXPECTED;
      }
    }

    return d3d12hooks.Create_Internal(createFunc, NULL, pAdapter, MinimumFeatureLevel, riid,
                                      ppDevice);
  }

  static HRESULT WINAPI StreamlineD3D12CreateDevice_hook(IUnknown *pAdapter,
                                                         D3D_FEATURE_LEVEL MinimumFeatureLevel,
                                                         REFIID riid, void **ppDevice)
  {
    RDCLOG("Intercepted Streamline D3D12CreateDevice");

    PFN_D3D12_CREATE_DEVICE createFunc = d3d12hooks.StreamlineCreateDevice();

    if(!createFunc)
    {
      RDCERR("Streamline D3D12CreateDevice hook has no original function");
      return E_UNEXPECTED;
    }

    return d3d12hooks.Create_Internal(createFunc, NULL, pAdapter, MinimumFeatureLevel, riid,
                                      ppDevice);
  }

  static HRESULT WINAPI D3D12EnableExperimentalFeatures_hook(UINT NumFeatures, const IID *pIIDs,
                                                             void *pConfigurationStructs,
                                                             UINT *pConfigurationStructSizes)
  {
    rdcarray<IID> allowedIIDs;

    // allow enabling unsigned DXIL, and GPU upload heaps on most windows versions
    for(UINT i = 0; i < NumFeatures; i++)
    {
      if(pIIDs[i] == D3D12ExperimentalShaderModels)
        allowedIIDs.push_back(D3D12ExperimentalShaderModels);
      else if(pIIDs[i] == D3D12GPUUploadHeapsOnUnsupportedOS)
        allowedIIDs.push_back(D3D12GPUUploadHeapsOnUnsupportedOS);
    }

    // there's no "partially successful" error code, so we just lie to the application and pretend
    // that any filtered IIDs also succeeded
    if(!allowedIIDs.empty())
      return d3d12hooks.EnableExperimentalFeatures()((UINT)allowedIIDs.size(), allowedIIDs.data(),
                                                     NULL, NULL);

    // header says "The call returns E_NOINTERFACE if an unrecognized feature is passed in or
    // Windows Developer mode is not on." so this is the most appropriate error for if no IIDs are
    // allowed.
    return E_NOINTERFACE;
  }

  static HRESULT WINAPI GetD3D11On12On7Interface_hook(ID3D11On12On7 **ppIface)
  {
    ID3D11On12On7 *real = NULL;
    d3d12hooks.GetD3D11On12On7()(&real);
    *ppIface = (ID3D11On12On7 *)(new WrappedD3D11On12On7(real));
    return S_OK;
  }

  static HRESULT WINAPI D3D12GetDebugInterface_hook(REFIID riid, void **ppvDebug)
  {
    if(riid == CLSID_D3D12StateObjectFactory)
    {
      RDCLOG("Deliberately reporting no support for state object factories");
      return E_NOINTERFACE;
    }

    IUnknown *realUnk = NULL;
    HRESULT real = d3d12hooks.GetDebugInterface()(riid, (void **)&realUnk);

    HRESULT hr = GetWrappedInterface(realUnk, riid, ppvDebug);

    if(realUnk)
      realUnk->Release();

    if(SUCCEEDED(hr))
      return hr;

    RDCWARN("Unknown UUID passed to D3D12GetDebugInterface: %s. Real call %s succeed (%x).",
            ToStr(riid).c_str(), SUCCEEDED(real) ? "did" : "did not", real);

    return E_NOINTERFACE;
  }

  static HRESULT D3D12GetInterface_Internal(PFN_D3D12_GET_INTERFACE getInterface, REFCLSID rclsid,
                                            REFIID riid, void **ppvDebug)
  {
    if(!getInterface)
    {
      RDCERR("D3D12GetInterface hook has no original function");
      return E_UNEXPECTED;
    }

    if(riid == CLSID_D3D12StateObjectFactory)
    {
      RDCLOG("Deliberately reporting no support for state object factories");
      return E_NOINTERFACE;
    }

    IUnknown *realUnk = NULL;
    HRESULT real = getInterface(rclsid, riid, (void **)&realUnk);

    HRESULT hr = GetWrappedInterface(realUnk, riid, ppvDebug);

    if(realUnk)
      realUnk->Release();

    if(SUCCEEDED(hr))
      return hr;

    RDCWARN("Unknown UUID passed to D3D12GetInterface: %s (clsid %s). Real call %s succeed (%x).",
            ToStr(riid).c_str(), ToStr(rclsid).c_str(), SUCCEEDED(real) ? "did" : "did not", real);

    return E_NOINTERFACE;
  }

  static HRESULT WINAPI D3D12GetInterface_hook(REFCLSID rclsid, REFIID riid, void **ppvDebug)
  {
    return D3D12GetInterface_Internal(d3d12hooks.GetInterface(), rclsid, riid, ppvDebug);
  }

  static HRESULT WINAPI StreamlineD3D12GetInterface_hook(REFCLSID rclsid, REFIID riid, void **ppvDebug)
  {
    RDCLOG("Intercepted Streamline D3D12GetInterface");
    return D3D12GetInterface_Internal(d3d12hooks.StreamlineGetInterface(), rclsid, riid, ppvDebug);
  }
};

D3D12Hook D3D12Hook::d3d12hooks;

HRESULT CreateD3D12_Internal(RealD3D12CreateFunction real, D3D12DevConfiguration *devConfig,
                             IUnknown *pAdapter, D3D_FEATURE_LEVEL MinimumFeatureLevel, REFIID riid,
                             void **ppDevice)
{
  return D3D12Hook::d3d12hooks.Create_Internal(real, devConfig, pAdapter, MinimumFeatureLevel, riid,
                                               ppDevice);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12DeviceFactory::GetConfigurationInterface(
    REFCLSID clsid, REFIID iid, _COM_Outptr_ void **ppv)
{
  IUnknown *realUnk = NULL;
  HRESULT real = m_pReal->GetConfigurationInterface(clsid, iid, (void **)&realUnk);

  HRESULT hr = D3D12Hook::GetWrappedInterface(realUnk, iid, ppv);

  if(realUnk)
    realUnk->Release();

  if(SUCCEEDED(hr))
    return hr;

  RDCWARN("Unknown UUID passed to D3D12GetDebugInterface: %s. Real call %s succeed (%x).",
          ToStr(iid).c_str(), SUCCEEDED(real) ? "did" : "did not", real);

  return E_NOINTERFACE;
}

#if ENABLED(ENABLE_UNIT_TESTS) && ENABLED(RDOC_X64)
#include "catch/catch.hpp"

TEST_CASE("Streamline export instruction boundaries", "[d3d][streamline]")
{
  // Streamline 2.8 D3D12CreateDevice: the E8 in 'mov rbp, r8' must not be treated as a call.
  const byte createDevice[] = {
      0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x6c, 0x24, 0x10, 0x48, 0x89, 0x74, 0x24,
      0x18, 0x57, 0x48, 0x83, 0xec, 0x50, 0x49, 0x8b, 0xf1, 0x49, 0x8b, 0xe8, 0x8b, 0xda,
      0x48, 0x8b, 0xf9, 0xe8, 0x9c, 0x05, 0x00, 0x00, 0x4c, 0x8b, 0xce, 0x4c, 0x8b, 0xc5,
      0x8b, 0xd3, 0x48, 0x8b, 0xcf, 0xff, 0x15, 0x3b, 0xc6, 0x05, 0x00,
  };
  const byte getInterface[] = {
      0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x57, 0x48, 0x83, 0xec, 0x20,
      0x49, 0x8b, 0xd8, 0x48, 0x8b, 0xfa, 0x48, 0x8b, 0xf1, 0xe8, 0x23, 0x00, 0x00, 0x00, 0x4c,
      0x8b, 0xc3, 0x48, 0x8b, 0xd7, 0x48, 0x8b, 0xce, 0x48, 0x8b, 0x5c, 0x24, 0x30, 0x48, 0x8b,
      0x74, 0x24, 0x38, 0x48, 0x83, 0xc4, 0x20, 0x5f, 0x48, 0xff, 0x25, 0x9c, 0xbf, 0x05, 0x00,
  };

  CHECK(FindStreamlineInstruction(createDevice, sizeof(createDevice), 0xe8) == createDevice + 0x1f);
  CHECK(FindStreamlineInstruction(createDevice, sizeof(createDevice), 0xff, 0x15) ==
        createDevice + 0x2f);
  CHECK(FindStreamlineInstruction(getInterface, sizeof(getInterface), 0xe8) == getInterface + 0x18);
  CHECK(FindStreamlineInstruction(getInterface, sizeof(getInterface), 0xff, 0x25) ==
        getInterface + 0x36);

  // DXGI has its own resolver. Its onward factories must be hooked alongside D3D12 devices.
  const byte createFactory1[] = {
      0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x54, 0x24, 0x10, 0x57, 0x48, 0x83,
      0xec, 0x20, 0x48, 0x8b, 0xda, 0x48, 0x8b, 0xf9, 0xe8, 0xc6, 0x01, 0x00, 0x00,
      0x48, 0x8b, 0xd3, 0x48, 0x8b, 0xcf, 0xff, 0x15, 0xc2, 0x6d, 0x05, 0x00,
  };
  const byte createFactory2[] = {
      0x48, 0x89, 0x5c, 0x24, 0x08, 0x48, 0x89, 0x74, 0x24, 0x10, 0x4c, 0x89,
      0x44, 0x24, 0x18, 0x57, 0x48, 0x83, 0xec, 0x20, 0x49, 0x8b, 0xf8, 0x48,
      0x8b, 0xf2, 0x8b, 0xd9, 0xe8, 0x6f, 0x01, 0x00, 0x00, 0x4c, 0x8b, 0xc7,
      0x48, 0x8b, 0xd6, 0x8b, 0xcb, 0xff, 0x15, 0x49, 0x6e, 0x05, 0x00,
  };
  CHECK(FindStreamlineInstruction(createFactory1, sizeof(createFactory1), 0xe8) ==
        createFactory1 + 0x15);
  CHECK(FindStreamlineInstruction(createFactory1, sizeof(createFactory1), 0xff, 0x15) ==
        createFactory1 + 0x20);
  CHECK(FindStreamlineInstruction(createFactory2, sizeof(createFactory2), 0xe8) ==
        createFactory2 + 0x1c);
  CHECK(FindStreamlineInstruction(createFactory2, sizeof(createFactory2), 0xff, 0x15) ==
        createFactory2 + 0x29);

  // These operands contain both the direct-call opcode and indirect transfer byte patterns.
  const byte operands[] = {
      0x48, 0x8b, 0xac, 0x24, 0xe8, 0xff, 0x25, 0x00,                // mov rbp, [rsp+disp32]
      0x48, 0x81, 0xec, 0xe8, 0xff, 0x15, 0x00,                      // sub rsp, imm32
      0x48, 0xb8, 0xe8, 0xff, 0x25, 0x00, 0x00, 0x00, 0x00, 0x00,    // mov rax, imm64
      0xe8, 0x00, 0x00, 0x00, 0x00,
  };
  CHECK(FindStreamlineInstruction(operands, sizeof(operands), 0xe8) == operands + 25);
  CHECK(FindStreamlineInstruction(operands, sizeof(operands), 0xff, 0x15) == NULL);
  CHECK(FindStreamlineInstruction(operands, sizeof(operands), 0xff, 0x25) == NULL);

  // Never read a partial instruction or search past an unsupported instruction/return.
  for(size_t size = 0; size < sizeof(createDevice); size++)
    CHECK(FindStreamlineInstruction(createDevice, size, 0xff, 0x15) == NULL);
  for(size_t size = 0; size < sizeof(getInterface); size++)
    CHECK(FindStreamlineInstruction(getInterface, size, 0xff, 0x25) == NULL);

  const byte unknown[] = {0x0f, 0x0b, 0xe8, 0, 0, 0, 0};
  const byte returned[] = {0xc3, 0xe8, 0, 0, 0, 0};
  const byte jumped[] = {0xff, 0x25, 0, 0, 0, 0, 0xe8, 0, 0, 0, 0};
  CHECK(FindStreamlineInstruction(unknown, sizeof(unknown), 0xe8) == NULL);
  CHECK(FindStreamlineInstruction(returned, sizeof(returned), 0xe8) == NULL);
  CHECK(FindStreamlineInstruction(jumped, sizeof(jumped), 0xe8) == NULL);
}
#endif

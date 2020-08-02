#include <ATen/Config.h>

#include <ATen/Context.h>

#include <c10/core/TensorOptions.h>

#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>

#include <ATen/Tensor.h>
#include <ATen/cpu/FlushDenormal.h>

#include <TH/TH.h> // for USE_LAPACK

#ifdef USE_FBGEMM
#include <fbgemm/Fbgemm.h>
#endif // USE_FBGEMM

namespace at {

Context::Context()
    : thc_state(nullptr, [](THCState* p) { /* no-op */ }),
      thh_state(nullptr, [](THHState* p) { /* no-op */ }) {}

// TODO: This could be bad juju if someone calls globalContext() in the
// destructor of an object with static lifetime.
Context& globalContext() {
  static Context globalContext_;
  return globalContext_;
}

// NB: This method is *purely* whether or not a user requested
// that CuDNN was enabled, it doesn't actually say anything about
// whether or not CuDNN is actually usable.
bool Context::userEnabledCuDNN() const {
  return enabled_cudnn;
}

void Context::setUserEnabledCuDNN(bool e) {
  enabled_cudnn = e;
}

bool Context::userEnabledMkldnn() const {
  return enabled_mkldnn;
}

void Context::setUserEnabledMkldnn(bool e) {
  enabled_mkldnn = e;
}

bool Context::deterministicCuDNN() const {
  return deterministic_cudnn;
}

void Context::setDeterministicCuDNN(bool b) {
  deterministic_cudnn = b;
}

bool Context::deterministic() const {
  return _deterministic;
}

void Context::setDeterministic(bool b) {
  _deterministic = b;
}

void Context::alertNotDeterministic(c10::string_view const& caller) {
  if (globalContext().deterministic()) {
    TORCH_CHECK(false,
      caller, " does not have a deterministic implementation, but you set "
      "'torch.set_deterministic(True)'. You can turn off determinism just "
      "for this operation if that's acceptable for your application. You "
      "can also file an issue at https://github.com/pytorch/pytorch/issues "
      "to help us prioritize adding deterministic support for this operation.");
  }
}

bool Context::benchmarkCuDNN() const {
  return benchmark_cudnn;
}

void Context::setBenchmarkCuDNN(bool b) {
  benchmark_cudnn = b;
}

bool Context::hasMKL() const {
#if AT_MKL_ENABLED()
  return true;
#else
  return false;
#endif
}

bool Context::hasMKLDNN() const {
#if AT_MKLDNN_ENABLED()
  return true;
#else
  return false;
#endif
}

bool Context::hasOpenMP() const {
#ifdef _OPENMP
  return true;
#else
  return false;
#endif
}

bool Context::hasLAPACK() const {
#ifdef USE_LAPACK
  return true;
#else
  return false;
#endif
}

at::QEngine Context::qEngine() const {
  // If wasn't explicitly set - take the last one available
  return quantized_engine.value_or(supportedQEngines().back());
}

void Context::setQEngine(at::QEngine e) {
  const auto& qengines = supportedQEngines();
  if (std::find(qengines.begin(), qengines.end(), e) != qengines.end()) {
    quantized_engine = e;
    return;
  }
  TORCH_CHECK(false, "quantized engine ", toString(e), " is not supported");
}

const std::vector<at::QEngine>& Context::supportedQEngines() const {
  static auto supported_qengines = []() {
    std::vector<at::QEngine> engines = {};
    // Engines are listed in priority order: later one wins
    // By default we prefer FBGEMM if we're running on server side
    // QNNPACK on server side has some issue, so we disable it by default.
#ifdef C10_MOBILE
    engines.push_back(at::kNoQEngine);
#ifdef USE_PYTORCH_QNNPACK
    engines.push_back(at::kQNNPACK);
#endif
#else  // C10_MOBILE
#ifdef USE_PYTORCH_QNNPACK
    engines.push_back(at::kQNNPACK);
#endif
    engines.push_back(at::kNoQEngine);
#endif // C10_MOBILE

#ifdef USE_FBGEMM
    if (fbgemm::fbgemmSupportedCPU()) {
      engines.push_back(at::kFBGEMM);
    }
#endif
    return engines;
  }();
  return supported_qengines;
}

bool Context::isXNNPACKAvailable() const {
#ifdef USE_XNNPACK
  return true;
#else
  return false;
#endif
}

bool Context::releaseWeightsWhenPrepacking() const {
  return release_original_weights;
}

void Context::setReleaseWeightsWhenPrepacking(bool e) {
  release_original_weights = e;
}

bool Context::setFlushDenormal(bool on) {
  return at::cpu::set_flush_denormal(on);
}

Allocator* getCPUAllocator() {
  return getTHDefaultAllocator();
}

} // namespace at

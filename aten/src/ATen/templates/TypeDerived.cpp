// required for old g++ to compile PRId64 macros, see
// https://github.com/pytorch/pytorch/issues/3571
// for context
#ifndef __STDC_FORMAT_MACROS
#define __STDC_FORMAT_MACROS
#endif

#include <ATen/${Type}.h>

// ${generated_comment}

$storage_tensor_headers
#include <ATen/${Generator}.h>
#include <c10/core/Allocator.h>
#include <ATen/DeviceGuard.h>
#include <ATen/NativeFunctions.h>
#include <ATen/NamedTensorUtils.h>
#include <ATen/Utils.h>
#include <ATen/WrapDimUtils.h>
#include <ATen/Dispatch.h>
#include <c10/util/Half.h>
#include <c10/core/TensorImpl.h>
#include <c10/core/UndefinedTensorImpl.h>
#include <c10/util/Optional.h>

#include <cstddef>
#include <functional>
#include <memory>
#include <utility>

#include <ATen/Config.h>
#include <ATen/core/op_registration/hacky_wrapper_for_legacy_signatures.h>
#include <torch/library.h>
$extra_cuda_headers
$legacy_th_headers

namespace at {

/* example
Tensor * ${Type}::add(Tensor & a, Tensor & b) {
  std::cout << "add Tensor with backend ${Backend}\n";
  return &a;
}
*/

namespace ${Type} {

${type_derived_method_definitions}

}  // namespace ${Type}

TORCH_LIBRARY_IMPL(aten, ${Backend}, m) {
  ${function_registrations}
}

} // namespace at

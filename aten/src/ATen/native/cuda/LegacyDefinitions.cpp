#include <ATen/ATen.h>
#include <ATen/NativeFunctions.h>
#include <ATen/LegacyTHFunctionsCUDA.h>
#include <ATen/NamedTensorUtils.h>
#include <ATen/ExpandUtils.h>

namespace at { namespace native {

// Methods

Tensor & masked_fill__cuda(Tensor& self, const Tensor & mask, Scalar value) {
  auto maybe_outnames = namedinference::broadcast_to_outnames(self, mask, "masked_fill_");
  Tensor b_mask;
  std::tie(b_mask) = expand_inplace(self, mask, "masked_fill_");
  // As we dispatch on self and TH is type-checked, we need different definitions.
  // This can be fixed by moving to ATen.
  if (b_mask.dtype() == at::ScalarType::Byte) {
    TORCH_WARN("masked_fill_ received a mask with dtype torch.uint8, this behavior is now deprecated," \
            "please use a mask with dtype torch.bool instead.");
    legacy::cuda::_th_masked_fill_(self, b_mask, value);
  } else {
    legacy::cuda::_th_masked_fill_bool_(self, b_mask, value);
  }
  namedinference::propagate_names_if_nonempty(self, maybe_outnames);
  return self;
}

Tensor & masked_fill__cuda(Tensor& self, const Tensor & mask, const Tensor & value) {
  auto maybe_outnames = namedinference::broadcast_to_outnames(self, mask, "masked_fill_");

  TORCH_CHECK(value.dim() == 0, "masked_fill_ only supports a 0-dimensional value tensor, but got tensor "
      "with ", value.dim(), " dimension(s).");
  Tensor b_mask;
  std::tie(b_mask) = expand_inplace(self, mask, "masked_fill_");
  // As we dispatch on self and TH is type-checked, we need different definitions.
  // This can be fixed by moving to ATen.
  if (b_mask.dtype() == at::ScalarType::Byte) {
    TORCH_WARN("masked_fill_ received a mask with dtype torch.uint8, this behavior is now deprecated," \
            "please use a mask with dtype torch.bool instead.");
    legacy::cuda::_th_masked_fill_(self, b_mask, value.item());
  } else {
    legacy::cuda::_th_masked_fill_bool_(self, b_mask, value.item());
  }
  namedinference::propagate_names_if_nonempty(self, maybe_outnames);
  return self;
}

Tensor & masked_scatter__cuda(Tensor& self, const Tensor & mask, const Tensor & source) {
  Tensor b_mask;
  std::tie(b_mask) = expand_inplace(self, mask, "masked_scatter_");
  // As we dispatch on self and TH is type-checked, we need different definitions.
  // This can be fixed by moving to ATen.
  if (b_mask.dtype() == at::ScalarType::Byte) {
    TORCH_WARN("masked_scatter_ received a mask with dtype torch.uint8, this behavior is now deprecated," \
            "please use a mask with dtype torch.bool instead.");
    return legacy::cuda::_th_masked_scatter_(self, b_mask, source);
  } else {
    return legacy::cuda::_th_masked_scatter_bool_(self, b_mask, source);
  }
}

Tensor & fmod_cuda_out(Tensor & result, const Tensor & self, Scalar other) {
  return legacy::cuda::_th_fmod_out(result, self, other);
}

Tensor fmod_cuda(const Tensor & self, Scalar other) {
  return legacy::cuda::_th_fmod(self, other);
}

Tensor & fmod_cuda_out(Tensor & result, const Tensor & self, const Tensor & other) {
  Tensor b_self, b_other;
  // optimization that codegen used to do; avoids broadcast.
  if (other.dim() == 0) {
    return fmod_cuda_out(result, self, other.item());
  }
  std::tie(b_self, b_other) = expand_outplace(self, other, "fmod_out");
  return legacy::cuda::_th_fmod_out(result, b_self, b_other);
}

Tensor fmod_cuda(const Tensor & self, const Tensor & other) {
  // optimization that codegen used to do; avoids broadcast.
  if (other.dim() == 0) {
    return fmod_cuda(self, other.item());
  }
  Tensor b_self, b_other;
  std::tie(b_self, b_other) = expand_outplace(self, other, "fmod");
  return legacy::cuda::_th_fmod(b_self, b_other);
}

Tensor & fmod_cuda_(Tensor & self, Scalar other) {
  return legacy::cuda::_th_fmod_(self, other);
}

Tensor & fmod_cuda_(Tensor & self, const Tensor & other) {
  // optimization that codegen used to do; avoids broadcast.
  if (other.dim() == 0) {
    return fmod_cuda_(self, other.item());
  }
  Tensor b_other;
  std::tie(b_other) = expand_inplace(self, other, "fmod_");
  return legacy::cuda::_th_fmod_(self, b_other);
}

}} // namespace at::native

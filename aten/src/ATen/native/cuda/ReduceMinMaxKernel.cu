#include <ATen/native/TensorIterator.h>
#include <ATen/native/cuda/Reduce.cuh>
#include <ATen/native/DispatchStub.h>
#include <ATen/native/SharedReduceOps.h>
#include <ATen/Dispatch.h>
#include <ATen/cuda/NumericLimits.cuh>
#include <THC/THCNumerics.cuh>
#include <ATen/native/ReduceOps.h>
#include<ATen/native/ReduceAllOps.h>
#include <ATen/native/ReduceOpsUtils.h>
#include <ATen/native/TensorCompare.h>


namespace at { namespace native {

template <typename scalar_t, typename acc_t=scalar_t>
void max_values_kernel_cuda_impl(TensorIterator& iter) {
  gpu_reduce_kernel<scalar_t, scalar_t>(
    iter, func_wrapper<acc_t> ([]GPU_LAMBDA(acc_t a, acc_t b) -> acc_t {
      return (THCNumerics<acc_t>::isnan(a) || a > b) ? a : b;
    }), at::numeric_limits<acc_t>::lower_bound());
}

template <typename scalar_t, typename acc_t=scalar_t>
void min_values_kernel_cuda_impl(TensorIterator& iter) {
  gpu_reduce_kernel<scalar_t, scalar_t>(
    iter, func_wrapper<acc_t> ([]GPU_LAMBDA(acc_t a, acc_t b) -> acc_t {
      return (THCNumerics<acc_t>::isnan(a) || a < b) ? a : b;
    }), at::numeric_limits<acc_t>::upper_bound());
}

void max_values_kernel_cuda(TensorIterator& iter) {
  if (iter.dtype(1) == kHalf) {
    max_values_kernel_cuda_impl<at::Half, float>(iter);
  } else {
    AT_DISPATCH_ALL_TYPES(iter.dtype(), "max_values_cuda", [&]() {
      max_values_kernel_cuda_impl<scalar_t>(iter);
    });
  }
}

void min_values_kernel_cuda(TensorIterator& iter) {
  if (iter.dtype(1) == kHalf) {
    min_values_kernel_cuda_impl<at::Half, float>(iter);
  } else {
    AT_DISPATCH_ALL_TYPES(iter.dtype(), "min_values_cuda", [&]() {
      min_values_kernel_cuda_impl<scalar_t>(iter);
    });
  }
}

template <typename scalar_t, typename acc_t=scalar_t>
void argmax_kernel_cuda_impl(TensorIterator& iter) {
  gpu_reduce_kernel<scalar_t, int64_t>(
    iter,
    ArgMaxOps<acc_t>{},
    thrust::pair<acc_t, int64_t>(at::numeric_limits<acc_t>::lower_bound(), 0));
};

template <typename scalar_t, typename acc_t=scalar_t>
void argmin_kernel_cuda_impl(TensorIterator& iter) {
  gpu_reduce_kernel<scalar_t, int64_t>(
    iter,
    ArgMinOps<acc_t>{},
    thrust::pair<acc_t, int64_t>(at::numeric_limits<acc_t>::upper_bound(), 0));
};

void argmax_kernel_cuda(TensorIterator& iter) {
  if (iter.dtype(1) == kHalf) {
    // Instead of implementing is_nan and warp_shfl_down
    // we can convert halves to float and do all the operations in float
    argmax_kernel_cuda_impl<at::Half, float>(iter);
  } else {
    AT_DISPATCH_ALL_TYPES(iter.dtype(1), "argmax_cuda", [&]() {
      argmax_kernel_cuda_impl<scalar_t>(iter);
    });
  }
}

void argmin_kernel_cuda(TensorIterator& iter) {
  if (iter.dtype(1) == kHalf) {
    // Instead of implementing is_nan and warp_shfl_down
    // we can convert halves to float and do all the operations in float
    argmin_kernel_cuda_impl<at::Half, float>(iter);
  } else {
    AT_DISPATCH_ALL_TYPES(iter.dtype(1), "argmin_cuda", [&]() {
      argmin_kernel_cuda_impl<scalar_t>(iter);
    });
  }
}

static void min_kernel_impl(Tensor& result, Tensor& indice, const Tensor& self, int64_t dim, bool keepdim) {
  at::TensorIterator iter = make_reduction("min", result, indice, self, dim, keepdim, self.scalar_type(), kLong);
  AT_DISPATCH_ALL_TYPES_AND2(kHalf, kBool, iter.dtype(2), "min_cuda", [&]() {
    gpu_reduce_kernel<scalar_t, scalar_t>(
      iter,
      MinOps<scalar_t>{},
      thrust::pair<scalar_t, int64_t>(at::numeric_limits<scalar_t>::upper_bound(), 0));
  });
}

static void max_kernel_impl(Tensor& result, Tensor& indice, const Tensor& self, int64_t dim, bool keepdim) {
  at::TensorIterator iter = make_reduction("max", result, indice, self, dim, keepdim, self.scalar_type(), kLong);
  AT_DISPATCH_ALL_TYPES_AND2(kHalf, kBool, iter.dtype(2), "max_cuda", [&]() {
    gpu_reduce_kernel<scalar_t, scalar_t>(
      iter,
      MaxOps<scalar_t>{},
      thrust::pair<scalar_t, int64_t>(at::numeric_limits<scalar_t>::lower_bound(), 0));
  });
}

static void min_all_kernel_impl(Tensor& result, const Tensor& input) {
  auto dtype = input.scalar_type();
  auto iter = make_reduction("min_all", result, input, std::vector<int64_t>{}, false, dtype);
  AT_DISPATCH_ALL_TYPES_AND2(kHalf, kBool, dtype, "min_all_cuda", [&] {
    min_values_kernel_cuda_impl<scalar_t>(iter);
  });
}

static void max_all_kernel_impl(Tensor& result, const Tensor& input) {
  auto dtype = input.scalar_type();
  auto iter = make_reduction("min_all", result, input, std::vector<int64_t>{}, false, dtype);
  AT_DISPATCH_ALL_TYPES_AND2(kHalf, kBool, dtype, "max_all_cuda", [&] {
    max_values_kernel_cuda_impl<scalar_t>(iter);
  });
}

REGISTER_DISPATCH(max_values_stub, &max_values_kernel_cuda);
REGISTER_DISPATCH(min_values_stub, &min_values_kernel_cuda);
REGISTER_DISPATCH(argmax_stub, &argmax_kernel_cuda);
REGISTER_DISPATCH(argmin_stub, &argmin_kernel_cuda);
REGISTER_DISPATCH(min_stub, &min_kernel_impl);
REGISTER_DISPATCH(max_stub, &max_kernel_impl);
REGISTER_DISPATCH(min_all_stub, &min_all_kernel_impl);
REGISTER_DISPATCH(max_all_stub, &max_all_kernel_impl);

}} // namespace at::native

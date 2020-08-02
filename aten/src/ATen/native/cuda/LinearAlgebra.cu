#include <ATen/ATen.h>
#include <ATen/LegacyTHFunctionsCUDA.h>
#include <ATen/cuda/CUDABlas.h>
#include <ATen/NamedTensorUtils.h>

namespace at { namespace native {

Tensor baddbmm_cuda(const Tensor& self, const Tensor& batch1, const Tensor& batch2, Scalar beta, Scalar alpha) {
  Tensor b_self;
  std::tie(b_self) = expand_size(self, {batch1.size(0), batch1.size(1), batch2.size(2)}, "baddbmm");
  return legacy::cuda::_th_baddbmm(b_self, batch1, batch2, beta, alpha);
}

Tensor& baddbmm_out_cuda(Tensor &result, const Tensor& self, const Tensor& batch1, const Tensor& batch2, Scalar beta, Scalar alpha) {
  Tensor b_self;
  std::tie(b_self) = expand_size(self, {batch1.size(0), batch1.size(1), batch2.size(2)}, "baddbmm_out");
  return legacy::cuda::_th_baddbmm_out(result, b_self, batch1, batch2, beta, alpha);
}

Tensor& baddbmm__cuda(Tensor& self, const Tensor& batch1, const Tensor& batch2, Scalar beta, Scalar alpha) {
  return baddbmm_out_cuda(self, self, batch1, batch2, beta, alpha);
}

Tensor& bmm_out_cuda(Tensor &result, const Tensor& batch1, const Tensor& batch2) {
  result.resize_({ batch1.size(0), batch1.size(1), batch2.size(2) });
  return legacy::cuda::_th_bmm_out(result, batch1, batch2);
}

Tensor bmm_cuda(const Tensor& self, const Tensor& mat2) {
  Tensor result = at::empty({0}, self.options());
  return native::bmm_out_cuda(result, self, mat2);
}

Tensor prepare_matrix_for_cublas(Tensor& tensor, bool& transpose_tensor) {
  Tensor tensor_;
  IntArrayRef tensor_strides = tensor.strides();

  if ((tensor_strides[0] == 1) && (tensor_strides[1] != 0)) {
    tensor_ = tensor;
    transpose_tensor = false;
  } else if ((tensor_strides[1] == 1) && (tensor_strides[0] != 0)) {
    tensor_ = tensor;
    transpose_tensor = true;
  } else {
    transpose_tensor = true;
    tensor_ = tensor.clone(at::MemoryFormat::Contiguous);
  }

  return tensor_;
}

namespace {

Tensor& addmm_out_cuda_impl(Tensor& result, const Tensor& self, const Tensor& mat1, const Tensor& mat2, Scalar beta, Scalar alpha) {
  TORCH_CHECK(mat1.dim() == 2 && mat2.dim() == 2, "tensors must be 2-D");

  Tensor self_;
  if (&result != &self) {
    std::tie(self_) = expand_size(self, {mat1.size(0), mat2.size(1)}, "addmm");
  } else {
    self_ = self;
  }

  IntArrayRef mat1_sizes = mat1.sizes();
  IntArrayRef mat2_sizes = mat2.sizes();
  IntArrayRef self__sizes = self_.sizes();
  TORCH_CHECK(mat1_sizes[1] == mat2_sizes[0], "mat1 dim 1 must match mat2 dim 0");
  TORCH_CHECK(self__sizes[0] == mat1_sizes[0], "self_ dim 0 must match mat1 dim 0");
  TORCH_CHECK(self__sizes[1] == mat2_sizes[1], "self_ dim 1 must match mat2 dim 1");

  if (&result != &self) {
    at::native::resize_as_(result, self_);
    if (beta.to<double>() != 0.0) {
      at::native::copy_(result, self_);
    }
  }

  TORCH_CHECK(result.dim() == 2 && self_.dim() == 2, "tensors must be 2-D");

  IntArrayRef result_sizes = result.sizes();
  if ((result_sizes[0] == 0) || (result_sizes[1] == 0)) {
    return result;
  }

  bool transpose_result;
  Tensor result_ = prepare_matrix_for_cublas(result, transpose_result);
  bool transpose_mat1;
  bool transpose_mat2;
  Tensor mat1_ = transpose_result ? mat2 : mat1;
  Tensor mat2_ = transpose_result ? mat1 : mat2;
  mat1_ = prepare_matrix_for_cublas(mat1_, transpose_mat1);
  mat2_ = prepare_matrix_for_cublas(mat2_, transpose_mat2);

  if (transpose_result) {
    transpose_mat1 = !transpose_mat1;
    transpose_mat2 = !transpose_mat2;
    mat1_sizes = mat1_.sizes();
    mat2_sizes = mat2_.sizes();
  }

  int64_t m = mat1_sizes[transpose_result ? 1 : 0];
  int64_t k = mat1_sizes[transpose_result ? 0 : 1];
  int64_t n = mat2_sizes[transpose_result ? 0 : 1];
  int64_t mat1_ld = mat1_.stride((transpose_mat1 == transpose_result) ? 1 : 0);
  int64_t mat2_ld = mat2_.stride((transpose_mat2 == transpose_result) ? 1 : 0);
  int64_t result_ld = result_.stride(transpose_result ? 0 : 1);
  at::ScalarType scalar_type = self_.scalar_type();

  AT_DISPATCH_FLOATING_TYPES_AND2(at::ScalarType::Half, at::ScalarType::BFloat16, scalar_type, "addmm_cuda", [&] {
    scalar_t alpha_val = alpha.to<scalar_t>();
    scalar_t beta_val = beta.to<scalar_t>();
    scalar_t* mat1_ptr = mat1_.data_ptr<scalar_t>();
    scalar_t* mat2_ptr = mat2_.data_ptr<scalar_t>();
    scalar_t* result_ptr = result_.data_ptr<scalar_t>();
    at::cuda::blas::gemm<scalar_t>(
      transpose_mat1 ? 't' : 'n',
      transpose_mat2 ? 't' : 'n',
      m, n, k,
      alpha_val,
      mat1_ptr, mat1_ld,
      mat2_ptr, mat2_ld,
      beta_val,
      result_ptr, result_ld
    );
  });
  if (result.data_ptr() != result_.data_ptr()) {
    result.copy_(result_);
  }
  return result;
}

} // anonymous namespace

Tensor& mm_out_cuda(Tensor& result, const Tensor& self, const Tensor& mat2) {
  result.resize_({ self.size(0), mat2.size(1) });
  return addmm_out_cuda_impl(result, result, self, mat2, 0, 1);
}

Tensor mm_cuda(const Tensor& self, const Tensor& mat2) {
  Tensor result = at::empty({ self.size(0), mat2.size(1) }, self.options());
  return addmm_out_cuda_impl(result, result, self, mat2, 0, 1);
}

Tensor& addmm_out_cuda(Tensor &out, const Tensor &self,
                        const Tensor &mat1, const Tensor &mat2,
                        Scalar beta, Scalar alpha) {
  {
    at::NoNamesGuard guard;
    Tensor& result = addmm_out_cuda_impl(out, self, mat1, mat2, beta, alpha);
  }
  at::namedinference::propagate_names_for_addmm(out, mat1, mat2, self);
  return out;
}

Tensor addmm_cuda(const Tensor& self, const Tensor& mat1, const Tensor& mat2,
                  Scalar beta, Scalar alpha) {
  Tensor out = at::empty({0}, self.options());
  addmm_out_cuda(out, self, mat1, mat2, beta, alpha);
  return out;
}

Tensor& addmm__cuda(Tensor& self, const Tensor& mat1, const Tensor& mat2,
                    Scalar beta, Scalar alpha) {
  addmm_out_cuda(self, self, mat1, mat2, beta, alpha);
  return self;
}

template<typename scalar_t>
void addr_impl_ger_cuda(Tensor &out, const Tensor &self,
                        const Tensor& vec1, const Tensor& vec2,
                        scalar_t alpha, scalar_t beta) {
  static_assert(std::is_same<scalar_t, float>::value ||
                std::is_same<scalar_t, double>::value,
                "addr_impl_ger_cuda: only float and double are supported");
  if (&out != &self) {
    at::native::resize_as_(out, self);
    at::native::copy_(out, self);
  }
  if (beta == 0.0) {
    at::native::zero_(out);
  }
  if (beta != 1.0) {
    at::native::mul_(out, beta);
  }
  if (out.stride(0) == 1) {
    at::cuda::blas::ger<scalar_t>(
      vec1.size(0), vec2.size(0), alpha,
      vec1.data_ptr<scalar_t>(), vec1.stride(0),
      vec2.data_ptr<scalar_t>(), vec2.stride(0),
      out.data_ptr<scalar_t>(), out.stride(1)
    );
  } else if (out.stride(1) == 1) {
    at::cuda::blas::ger<scalar_t>(
      vec2.size(0), vec1.size(0), alpha,
      vec2.data_ptr<scalar_t>(), vec2.stride(0),
      vec1.data_ptr<scalar_t>(), vec1.stride(0),
      out.data_ptr<scalar_t>(), out.stride(0)
    );
  } else {
    Tensor cr = out.clone();
    at::cuda::blas::ger<scalar_t>(
      vec2.size(0), vec1.size(0), alpha,
      vec2.data_ptr<scalar_t>(), vec2.stride(0),
      vec1.data_ptr<scalar_t>(), vec1.stride(0),
      out.data_ptr<scalar_t>(), out.stride(0)
    );
    out.set_(cr);
  }
}

template<typename scalar_t>
void addr_impl_cuda(Tensor &out, const Tensor &self,
                    const Tensor& vec1, const Tensor& vec2,
                    scalar_t alpha, scalar_t beta) {
  // currently no Hger/SgerEx in Cublas.
  Tensor vec2T = vec2.reshape({1, vec2.size(0)});
  Tensor vec1M = vec1.reshape({vec1.size(0), 1});
  addmm_out_cuda(out, self, vec1M, vec2T, beta, alpha);
}
template<>
void addr_impl_cuda<float>(Tensor &out, const Tensor &self,
                           const Tensor& vec1, const Tensor& vec2,
                           float alpha, float beta) {
  addr_impl_ger_cuda<float>(out, self, vec1, vec2, alpha, beta);
}
template<>
void addr_impl_cuda<double>(Tensor &out, const Tensor &self,
                            const Tensor& vec1, const Tensor& vec2,
                            double alpha, double beta) {
  addr_impl_ger_cuda<double>(out, self, vec1, vec2, alpha, beta);
}

Tensor& addr_out_cuda(Tensor &out, const Tensor& self,
                      const Tensor& vec1, const Tensor& vec2,
                      Scalar beta, Scalar alpha) {
  TORCH_CHECK(vec1.dim() == 1 && vec2.dim() == 1,
              "vec1 and vec2 should be 1-dimensional vectors. Got dimensions ",
              vec1.dim(), " and ", vec2.dim());

  Tensor self_;
  if (&out != &self) {
    std::tie(self_) = expand_size(self, {vec1.size(0), vec2.size(0)}, "addr");
  } else {
    self_ = self;
  }

  TORCH_CHECK(out.device() == self_.device() &&
              out.device() == vec1.device() &&
              out.device() == vec2.device(),
              "Expected all tensors to be on the same device. Found: ",
              out.device(), ", ", self_.device(), ", ",
              vec1.device(), " and ", vec2.device());
  TORCH_CHECK(self_.dim() == 2,
              "2D tensor expected, got ", self_.dim(), "D tensor for input");
  TORCH_CHECK(self_.size(0) == vec1.size(0) && self_.size(1) == vec2.size(0),
              "size mismatch",
              ", input: ", self_.sizes(),
              ", v1: ", vec1.sizes(),
              ", v2: ", vec2.sizes());
  AT_DISPATCH_FLOATING_TYPES_AND2(kBFloat16, kHalf, self_.scalar_type(), "addr_out_cuda", [&] {
      addr_impl_cuda<scalar_t>(out, self_, vec1, vec2,
                               alpha.to<scalar_t>(), beta.to<scalar_t>());
  });
  return out;
}

Tensor& addr__cuda(Tensor& self,
                   const Tensor& vec1, const Tensor& vec2,
                   Scalar beta, Scalar alpha) {
  addr_out_cuda(self, self, vec1, vec2, beta, alpha);
  return self;
}

Tensor addr_cuda(const Tensor& self,
                  const Tensor& vec1, const Tensor& vec2,
                  Scalar beta, Scalar alpha) {
  Tensor out = at::empty({0}, self.options());
  addr_out_cuda(out, self, vec1, vec2, beta, alpha);
  return out;
}

Tensor& addbmm_out_cuda(Tensor& out, const Tensor& self,
                        const Tensor& batch1, const Tensor& batch2,
                        Scalar beta, Scalar alpha) {
  TORCH_CHECK(batch1.dim() == 3 && batch2.dim() == 3,
              "Batch tensors should be 3D, got dimensions ", batch1.dim(),
              " and ", batch2.dim());

  Tensor self_;
  if (&out != &self) {
    std::tie(self_) = expand_size(self, {batch1.size(1), batch2.size(2)}, "addbmm");
  } else {
    self_ = self;
  }

  TORCH_CHECK(out.device() == self_.device() &&
              out.device() == batch1.device() &&
              out.device() == batch2.device(),
              "Expected all tensors to be on the same device. Found: ",
              out.device(), ", ", self_.device(), ", ",
              batch1.device(), " and ", batch2.device());
  TORCH_CHECK(self_.dim() == 2,
              "2D tensor expected, got ", self_.dim(), "D tensor for input");
  int64_t batchnum = batch1.size(0);
  int64_t m1d1 = batch1.size(1);
  int64_t innerdim = batch1.size(2);
  int64_t m2d2 = batch2.size(2);
  TORCH_CHECK(batchnum == batch2.size(0),
              "equal number of batches expected");
  TORCH_CHECK(m1d1 == self_.size(0),
              "first dimension of batch1  must match first dimension of input");
  TORCH_CHECK(m2d2 == self_.size(1),
              "second dimension of batch2 must match second dimension of input");
  TORCH_CHECK(innerdim == batch2.size(1),
              "second dimension of batch1 must match first dimension of batch2");

  if (&out != &self) {
    at::native::resize_as_(out, self_);
    if (beta.to<double>() != 0.0) {
      at::native::copy_(out, self_);
    }
  }

  for (int64_t i=0; i<batchnum; i++) {
    addmm_out_cuda(out, out, batch1[i], batch2[i], beta, alpha);
    beta = 1;
  }
  return out;
}

Tensor& addbmm__cuda(Tensor& self,
                     const Tensor& batch1, const Tensor& batch2,
                     Scalar beta, Scalar alpha) {
  addbmm_out_cuda(self, self, batch1, batch2, beta, alpha);
  return self;
}

Tensor addbmm_cuda(const Tensor& self,
                   const Tensor& batch1, const Tensor& batch2,
                   Scalar beta, Scalar alpha) 
{
  Tensor out = at::empty({0}, self.options());
  addbmm_out_cuda(out, self, batch1, batch2, beta, alpha);
  return out;
}

} }

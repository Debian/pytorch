// define constants like M_PI and C keywords for MSVC
#ifdef _MSC_VER
#ifndef _USE_MATH_DEFINES
#define _USE_MATH_DEFINES
#endif
#include <math.h>
#endif

#include <ATen/ATen.h>
#include <ATen/CPUGeneratorImpl.h>
#include <ATen/Utils.h>
#include <ATen/Dispatch.h>
#include <ATen/NativeFunctions.h>
#include <ATen/TracerMode.h>
#include <c10/core/ScalarType.h>
#include <c10/util/Deprecated.h>
#include <ATen/native/Resize.h>
#include <ATen/native/TensorFactories.h>
#include <c10/core/TensorOptions.h>
#include <TH/THAllocator.h>
#include <ATen/detail/CUDAHooksInterface.h>
#include <c10/util/Exception.h>
#include <ATen/NamedTensorUtils.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstddef>
#include <string>

namespace at {
namespace native {
namespace {
void window_function_checks(
    const char* function_name,
    const TensorOptions& options,
    int64_t window_length) {
  TORCH_CHECK(
      options.layout() != kSparse,
      function_name,
      " is not implemented for sparse types, got: ",
      options);
  TORCH_CHECK(
      at::isFloatingType(typeMetaToScalarType(options.dtype())) || at::isComplexType(typeMetaToScalarType(options.dtype())),
      function_name,
      " expects floating point dtypes, got: ",
      options);
  TORCH_CHECK(
      window_length >= 0,
      function_name,
      " requires non-negative window_length, got window_length=",
      window_length);
}

// bool inputs are considered integral
static inline bool allIntegral(std::initializer_list<std::reference_wrapper<Scalar>> l) {
  for (Scalar& s : l) {
    if (!s.isIntegral(true)) {
      return false;
    }
  }
  return true;
}

} // namespace

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ arange ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor arange(Scalar end, const TensorOptions& options) {
  return native::arange(/*start=*/0, end, options);
}

Tensor arange(Scalar start, Scalar end, const TensorOptions& options) {
  return native::arange(start, end, /*step=*/1, options);
}

Tensor arange(
    Scalar start,
    Scalar end,
    Scalar step,
    const TensorOptions& options) {
  bool set_to_integral_dtype = !options.has_dtype() && allIntegral({start, end, step});
  Tensor result = set_to_integral_dtype
      ? at::empty({0}, options.dtype(at::ScalarType::Long))
      : at::empty({0}, options);
  return at::arange_out(result, start, end, step);
}

Tensor& arange_out(Tensor& result, Scalar end) {
  return at::arange_out(result, /*start=*/0, end);
}

Tensor& arange_out(Tensor& result, Scalar start, Scalar end) {
  return at::arange_out(result, start, end, /*step=*/1);
}

Tensor _dim_arange(const Tensor& like, int64_t dim) {
  return at::arange(like.size(dim), like.options().dtype(at::kLong));
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ empty ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tensor empty_cpu(IntArrayRef size, const TensorOptions& options_, c10::optional<c10::MemoryFormat> optional_memory_format) {

  TORCH_CHECK(
    !(options_.has_memory_format() && optional_memory_format.has_value()),
    "Cannot set memory_format both in TensorOptions and explicit argument; please delete "
    "the redundant setter.");
  TensorOptions options = options_.merge_in(TensorOptions().memory_format(optional_memory_format));

  AT_ASSERT(options.device().type() == DeviceType::CPU);
  TORCH_INTERNAL_ASSERT(impl::variable_excluded_from_dispatch());
  check_size_nonnegative(size);

  c10::Allocator* allocator;
  if (options.pinned_memory()) {
    allocator = detail::getCUDAHooks().getPinnedMemoryAllocator();
  } else {
    allocator = at::getCPUAllocator();
  }

  int64_t nelements = prod_intlist(size);
  auto dtype = options.dtype();
  int64_t size_bytes = nelements * dtype.itemsize();
  auto storage_impl = c10::make_intrusive<StorageImpl>(
      c10::StorageImpl::use_byte_size_t(),
      size_bytes,
      allocator->allocate(size_bytes),
      allocator,
      /*resizeable=*/true);

  auto tensor = detail::make_tensor<TensorImpl>(
      std::move(storage_impl), at::DispatchKey::CPU, dtype);
  // Default TensorImpl has size [0]
  if (size.size() != 1 || size[0] != 0) {
    tensor.unsafeGetTensorImpl()->set_sizes_contiguous(size);
  }

  auto memory_format = options.memory_format_opt().value_or(MemoryFormat::Contiguous);
  tensor.unsafeGetTensorImpl()->empty_tensor_restride(memory_format);

  return tensor;
}

Tensor empty(
    IntArrayRef size,
    at::optional<DimnameList> names,
    const TensorOptions& options,
    optional<MemoryFormat> optional_memory_format) {
  if (!names.has_value()) {
    return at::empty(size, options, optional_memory_format);
  }
  TORCH_CHECK(options.layout() == Layout::Strided,
      "NYI: named tensors only support strided layout");
  TORCH_CHECK(options.device().type() == DeviceType::CPU || options.device().type() == DeviceType::CUDA,
      "NYI: named tensors only support CPU and CUDA tensors");
  auto result = at::empty(size, options, optional_memory_format);
  internal_set_names_inplace(result, names);
  return result;
}

Tensor empty_strided_cpu(IntArrayRef size, IntArrayRef stride, const TensorOptions& options) {
  check_size_nonnegative(size);
  auto t = at::native::empty_cpu({0}, options);
  at::native::resize_impl_cpu_(t.unsafeGetTensorImpl(), size, stride);
  return t;
}

Tensor& empty_out(
    Tensor& result,
    IntArrayRef size,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  // Preferably, this argument would not be accepted by _out, but the code
  // generator requires the out and non-out overloads to match exactly
  TORCH_CHECK(
      !optional_memory_format.has_value(),
      "'memory_format' argument is incompatible with 'out' tensor argument");
  check_size_nonnegative(size);
  if (result.is_sparse()) {
    result.sparse_resize_and_clear_(size, size.size(), 0);
  } else {
    result.resize_(size);
  }
  return result;
}

// Temporary type cast operators. These are needed to trace type-casts now since
// Type's are not supported in the IR. Instead, we call down to these
// specialized operators for each datatype.
// TODO: remove when we have Type support in the IR

#define DEFINE_CAST_OP(_1, n)                                    \
  Tensor _cast_##n(const Tensor& self, bool non_blocking) {      \
    if (self.scalar_type() == ScalarType::n)                     \
      return self;                                               \
    return self.to(ScalarType::n, non_blocking);                 \
  }

AT_FORALL_SCALAR_TYPES_AND3(Bool, Half, BFloat16, DEFINE_CAST_OP)

#undef DEFINE_CAST_OP

Tensor empty_like(
    const Tensor& self,
    const TensorOptions& options_,
    c10::optional<c10::MemoryFormat> optional_memory_format) {

  TORCH_CHECK(
    !(options_.has_memory_format() && optional_memory_format.has_value()),
    "Cannot set memory_format both in TensorOptions and explicit argument; please delete "
    "the redundant setter.");

  TensorOptions options =
      self.options()
          .merge_in(options_)
          .merge_in(TensorOptions().memory_format(optional_memory_format));

  TORCH_CHECK(
      !(options.layout() != kStrided &&
          optional_memory_format.has_value()),
      "memory format option is only supported by strided tensors");
  if (options.layout() == kSparse && self.is_sparse()) {
    auto result = at::empty({0}, options); // to be resized
    result.sparse_resize_and_clear_(
        self.sizes(), self.sparse_dim(), self.dense_dim());
    return result;
  }

  auto memory_format = options.memory_format_opt().value_or(MemoryFormat::Preserve);

  if (self.is_quantized()) {

    // TODO: To support all features of MemoryFormat::Preserve we need to add
    // _empty_affine_quantized_strided function and use it similarly to
    // Tensor clone(const Tensor& src, c10::optional<c10::MemoryFormat> optional_memory_format)
    // if (self.is_non_overlapping_and_dense()) -> _empty_affine_quantized_strided
    if (memory_format == MemoryFormat::Preserve) {
      memory_format = self.suggest_memory_format();
    }


    // Note [Explicit nullopt MemoryFormat argument]
    // ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    // Some functions which we call default the OPTIONAL MemoryFormat
    // argument to something that's not nullopt.  If we pass the
    // MemoryFormat via TensorOptions, we must explicitly disable this
    // defaulting process, by explicitly passing nullopt for the MemoryFormat
    // argument.  When codegen is adjusted so we can delete this argument from
    // the method signature, the argument will just disappear entirely.
    //
    // BTW, there are a few places where the optional MemoryFormat is None,
    // but I still pass in nullopt for robustness.

    // We could check if dtype is still quantized?  But then should we shift/scale
    // the q_zero_point / q_scale or not?
    TORCH_CHECK(!options.has_dtype() || options.dtype() == self.dtype(),
                "It is currently not supported to specify a dtype that doesn't match "
                "the input tensor's dtype via empty_like.  Specified: ", options.dtype(),
                " Input tensor's dtype: ", self.dtype());
    auto qscheme = self.qscheme();
    if (qscheme == kPerTensorAffine) {
      return at::_empty_affine_quantized(self.sizes(), options.memory_format(memory_format),
                                         self.q_scale(),
                                         self.q_zero_point(),
                                         // See Note [Explicit nullopt MemoryFormat argument]
                                         c10::nullopt);
    } else if (qscheme == kPerChannelAffine) {
      // Copy the tensors with channels to avoid accidental overrides
      return at::_empty_per_channel_affine_quantized(
          self.sizes(),
          self.q_per_channel_scales().clone(at::MemoryFormat::Preserve),
          self.q_per_channel_zero_points().clone(at::MemoryFormat::Preserve),
          self.q_per_channel_axis(),
          options.memory_format(memory_format),
          // See Note [Explicit nullopt MemoryFormat argument]
          c10::nullopt);
    } else {
      TORCH_CHECK(false, "Unsupported qscheme: ", toString(qscheme));
    }
  }

  Tensor result;

  if (memory_format == MemoryFormat::Preserve) {
    if (self.is_non_overlapping_and_dense()) {
      result = at::empty_strided(self.sizes(), self.strides(), options.memory_format(c10::nullopt));
    } else {
      // See Note [Explicit nullopt MemoryFormat argument]
      result = at::empty(self.sizes(), options.memory_format(self.suggest_memory_format()), c10::nullopt);
    }
  } else {
    // See Note [Explicit nullopt MemoryFormat argument]
    result = at::empty(self.sizes(), options.memory_format(memory_format), c10::nullopt);
  }

  if (self.opt_names()) {
    namedinference::propagate_names(result, self.names());
  }

  return result;
}

Tensor new_empty(
    const Tensor& self,
    IntArrayRef size,
    const TensorOptions& options
    ) {
  return at::empty(size, self.options().merge_in(options));
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ eye ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor eye(int64_t n, const TensorOptions& options) {
  return native::eye(n, -1, options);
}

Tensor eye(int64_t n, int64_t m, const TensorOptions& options) {
  auto tensor = at::empty({0}, options); // to be resized
  return at::eye_out(tensor, n, m);
}

Tensor& eye_out_cpu(Tensor& result, int64_t n) {
  return native::eye_out_cpu(result, n, -1);
}

Tensor& eye_out_cpu(Tensor& result, int64_t n, int64_t m) {
  TORCH_CHECK(n >= 0, "n must be greater or equal to 0, got ", n);

  if(m < 0) {
    m = n;
  }

  result.resize_({n, m});
  result.zero_();

  int64_t sz = std::min<int64_t>(n, m);
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX_AND2(at::ScalarType::Half, at::ScalarType::Bool, result.scalar_type(), "eye", [&]() -> void {
    scalar_t* result_data = result.data_ptr<scalar_t>();
    at::parallel_for(0, sz, internal::GRAIN_SIZE, [&](int64_t p_begin, int64_t p_end) {
      for(int64_t i = p_begin; i < p_end; i++)
        result_data[i*(result.strides()[0] + result.strides()[1])] = 1;
    });
  });

  return result;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ full ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

namespace {

// Performs dtype inference for full
TensorOptions infer_full_options(
  Scalar fill_value,
  const TensorOptions& options) {

  if (!options.has_dtype()) {
    if (fill_value.isIntegral(true)) {
      TORCH_CHECK(false,
        "Providing a bool or integral fill value without setting the optional ",
        "`dtype` or `out` arguments is currently unsupported. In PyTorch 1.7, ",
        "when `dtype` and `out` are not set a bool fill value will ",
        "return a tensor of torch.bool dtype, and an integral fill value ",
        "will return a tensor of torch.long dtype.");
    } else if (fill_value.isComplex()) {
      auto scalar_type = (get_default_dtype() == ScalarType::Double) ?
                            ScalarType::ComplexDouble :
                            ScalarType::ComplexFloat;
      return options.dtype(scalar_type);
    }
  }

  return options;
}

} // anonymous namespace

Tensor full(IntArrayRef size, Scalar fill_value, const TensorOptions& options) {
  TORCH_CHECK(options.layout() != kSparse,
    "full(...) is not implemented for sparse layout");

  auto result = at::empty(size, infer_full_options(fill_value, options));
  return result.fill_(fill_value);
}

Tensor& full_out(Tensor& result, IntArrayRef size, Scalar fill_value) {
  TORCH_CHECK(!result.is_sparse(),
    "full(...) is not implemented for sparse layout");

  result.resize_(size);
  return result.fill_(fill_value);
}

Tensor full_like(
    const Tensor& self,
    Scalar fill_value,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.fill_(fill_value);
}

Tensor new_full(
    const Tensor& self,
    IntArrayRef size,
    Scalar fill_value,
    const TensorOptions& options
    ) {
  return at::full(size, fill_value, self.options().merge_in(options));
}


// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ linspace ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor linspace(
    Scalar start,
    Scalar end,
    int64_t steps,
    const TensorOptions& options) {
  TORCH_CHECK(steps >= 0, "number of steps must be non-negative");
  Tensor result = at::empty({steps}, options);
  return at::linspace_out(result, start, end, steps);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ logspace ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor logspace(
    Scalar start,
    Scalar end,
    int64_t steps,
    double base,
    const TensorOptions& options) {
  Tensor result = at::empty({steps}, options);
  return at::logspace_out(result, start, end, steps, base);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ones ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor ones(IntArrayRef size, const TensorOptions& options) {
  return native::full(size, /*fill_value=*/1., options);
}

Tensor& ones_out(Tensor& result, IntArrayRef size) {
  return native::full_out(result, size, /*fill_value=*/1.);
}

Tensor ones_like(
    const Tensor& self,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.fill_(1.);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ scalar_tensor ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor scalar_tensor(Scalar s, const TensorOptions& options) {
  if (options.device() == at::kCPU) {
    // This is a fast track to skip device dispatch for making scalar tensor on CPU.
    // See https://github.com/pytorch/pytorch/pull/29915 for more detailed perf
    // difference.
    // In the future when we remove the overhead of device dispatch, we'll happily
    // revert this to following:
    //   auto result = at::empty({}, options);
    at::tracer::impl::NoTracerDispatchMode tracer_guard;
    at::AutoNonVariableTypeMode non_var_type_mode(true);
    auto result = empty_cpu({}, options);
    at::native::fill_(result, s);
    return result;
  }
  return at::empty({}, options).fill_(s);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ rand ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor rand(IntArrayRef size, const TensorOptions& options) {
  return native::rand(size, static_cast<c10::optional<Generator>>(c10::nullopt), options);
}

Tensor rand(IntArrayRef size, c10::optional<Generator> generator, const TensorOptions& options) {
  auto result = at::empty(size, options);
  return result.uniform_(0, 1, generator);
}

Tensor& rand_out(Tensor& result, IntArrayRef size) {
  return native::rand_out(result, size, c10::nullopt);
}

Tensor& rand_out(Tensor& result, IntArrayRef size, c10::optional<Generator> generator) {
  result.resize_(size);
  return result.uniform_(0, 1, generator);
}

Tensor rand_like(
    const Tensor& self,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.uniform_(0, 1, c10::nullopt);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ randint ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor randint(int64_t high, IntArrayRef size, const TensorOptions& options) {
  return native::randint(high, size, c10::nullopt, options);
}

Tensor randint(
    int64_t high,
    IntArrayRef size,
    c10::optional<Generator> generator,
    const TensorOptions& options) {
  return native::randint(0, high, size, generator, options);
}

Tensor randint(
    int64_t low,
    int64_t high,
    IntArrayRef size,
    const TensorOptions& options) {
  return native::randint(low, high, size, c10::nullopt, options);
}

Tensor randint(
    int64_t low,
    int64_t high,
    IntArrayRef size,
    c10::optional<Generator> generator,
    const TensorOptions& options) {
  auto result = at::empty(size, options);
  return result.random_(low, high, generator);
}

Tensor& randint_out(Tensor& result, int64_t high, IntArrayRef size) {
  return native::randint_out(result, high, size, c10::nullopt);
}

Tensor& randint_out(
    Tensor& result,
    int64_t high,
    IntArrayRef size,
    c10::optional<Generator> generator) {
  result.resize_(size);
  return result.random_(0, high, generator);
}

Tensor& randint_out(Tensor& result, int64_t low, int64_t high, IntArrayRef size) {
  return native::randint_out(result, low, high, size, c10::nullopt);
}

Tensor& randint_out(
    Tensor& result,
    int64_t low,
    int64_t high,
    IntArrayRef size,
    c10::optional<Generator> generator) {
  result.resize_(size);
  return result.random_(low, high, generator);
}

Tensor randint_like(
    const Tensor& self,
    int64_t high,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.random_(0, high, c10::nullopt);
}

Tensor randint_like(
    const Tensor& self,
    int64_t low,
    int64_t high,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.random_(low, high, c10::nullopt);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ randn ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor randn(IntArrayRef size, const TensorOptions& options) {
  return native::randn(size, static_cast<c10::optional<Generator>>(c10::nullopt), options);
}

Tensor randn(IntArrayRef size, c10::optional<Generator> generator, const TensorOptions& options) {
  auto result = at::empty(size, options);
  return result.normal_(0, 1, generator);
}

Tensor& randn_out(Tensor& result, IntArrayRef size) {
  return native::randn_out(result, size, c10::nullopt);
}

Tensor& randn_out(Tensor& result, IntArrayRef size, c10::optional<Generator> generator) {
  result.resize_(size);
  return result.normal_(0, 1, generator);
}

Tensor normal(double mean, double std, IntArrayRef size,
              c10::optional<Generator> generator, const TensorOptions& options) {
  auto result = at::empty(size, options);
  return result.normal_(mean, std, generator);
}

Tensor& normal_out(Tensor& result, double mean, double std,
                   IntArrayRef size, c10::optional<Generator> generator) {
  result.resize_(size);
  return result.normal_(mean, std, generator);
}

Tensor randn_like(
    const Tensor& self,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.normal_(0, 1, c10::nullopt);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ randperm ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

namespace {
template <typename scalar_t>
void randperm_cpu(Tensor& result, int64_t n, CPUGeneratorImpl* generator) {
  scalar_t *r__data = result.data_ptr<scalar_t>();

  result.resize_({n});
  int64_t r__stride_0 = result.stride(0);

  at::parallel_for(0, n, internal::GRAIN_SIZE,
                  [&r__data, &r__stride_0](int64_t p_begin, int64_t p_end) {
    for(int64_t i = p_begin; i < p_end; i++)
      r__data[i*r__stride_0] = static_cast<scalar_t>(i);
  });

  for(int64_t i = 0; i < n - 1; i++)
  {
    int64_t z = generator->random() % (n-i);
    scalar_t sav = r__data[i*r__stride_0];
    r__data[i*r__stride_0] = r__data[(z+i)*r__stride_0];
    r__data[(z+i)*r__stride_0] = sav;
  }
}
} // namespace

Tensor randperm(int64_t n, const TensorOptions& options) {
  return native::randperm(n, c10::nullopt, options);
}

Tensor randperm(int64_t n, c10::optional<Generator> generator, const TensorOptions& options) {
  auto tensor = at::empty(n, options);
  return at::randperm_out(tensor, n, generator);
}

Tensor& randperm_out(Tensor& result, int64_t n) {
  return at::randperm_out(result, n, c10::nullopt);
}

Tensor& randperm_out_cpu(Tensor& result, int64_t n, c10::optional<Generator> generator) {
  TORCH_CHECK(n >= 0, "n must be non-negative, got", n);
  check_supported_max_int_with_precision(n, result);
  result.resize_({n});
  auto gen = get_generator_or_default<CPUGeneratorImpl>(generator, detail::getDefaultCPUGenerator());
  // See Note [Acquire lock when using random generators]
  std::lock_guard<std::mutex> lock(gen->mutex_);
  AT_DISPATCH_ALL_TYPES_AND(at::ScalarType::Half, result.scalar_type(), "randperm", [&]() -> void {
    randperm_cpu<scalar_t>(result, n, gen);
  });

  return result;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ range ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor range(
    Scalar start,
    Scalar end,
    Scalar step,
    const TensorOptions& options) {
  Tensor result = at::empty({0}, options);
  return at::range_out(result, start, end, step);
}

Tensor range(
    Scalar start,
    Scalar end,
    const TensorOptions& options) {
  return at::native::range(start, end, 1, options);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ triangle ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor tril_indices_cpu(
    int64_t row, int64_t col, int64_t offset, const TensorOptions& options) {
  check_args(row, col, options);

  auto tril_size = get_tril_size(row, col, offset);

  // create an empty Tensor with correct size
  auto result = at::empty({2, tril_size}, options);

  // The following three approaches result in very little performance
  // differences. Hence, the 2nd option is taken for simpler code, and to return
  // contiguous tensors. Refer to #14904 for more details.
  //
  // 1. sequential RAM access: fill row coordinates first, then columns. This
  //    results in two for-loop and more arithmetic operations.
  //
  // 2. interleaved RAM access: fill in index coordinates one by one, which
  //    jumps between the two output Tensor rows in every iteration.
  //
  // 3. sequential RAM + transpose: create an n X 2 Tensor, fill the Tensor
  //    sequentially, and then transpose it.
  AT_DISPATCH_ALL_TYPES(result.scalar_type(), "tril_indices", [&]() -> void {
    // fill the Tensor with correct values
    scalar_t* result_data = result.data_ptr<scalar_t>();
    int64_t i = 0;

    scalar_t r = std::max<int64_t>(0, -offset), c = 0;
    while (i < tril_size) {
      result_data[i] = r;
      result_data[tril_size + i++] = c;

      // move to the next column and check if (r, c) is still in bound
      c += 1;
      if (c > r + offset || c >= col) {
        r += 1;
        c = 0;
        // NOTE: not necessary to check if r is less than row here, because i
        // and tril_size provide the guarantee
      }
    }
  });

  return result;
}

Tensor triu_indices_cpu(
    int64_t row, int64_t col, int64_t offset, const TensorOptions& options) {
  check_args(row, col, options);

  auto triu_size = row * col - get_tril_size(row, col, offset - 1);

  // create an empty Tensor with correct size
  auto result = at::empty({2, triu_size}, options);

  AT_DISPATCH_ALL_TYPES(result.scalar_type(), "triu_indices", [&]() -> void {
    // fill the Tensor with correct values
    scalar_t* result_data = result.data_ptr<scalar_t>();
    int64_t i = 0;
    // not typing std::max with scalar_t as it could be an unsigned type
    // NOTE: no need to check if the returned value of std::max overflows
    // scalar_t, as i and triu_size act as a guard.
    scalar_t c = std::max<int64_t>(0, offset), r = 0;
    while (i < triu_size) {
      result_data[i] = r;
      result_data[triu_size + i++] = c;

      // move to the next column and check if (r, c) is still in bound
      c += 1;
      if (c >= col) {
        r += 1;
        // not typing std::max with scalar_t as it could be an unsigned type
        // NOTE: not necessary to check if c is less than col or overflows here,
        // because i and triu_size act as a guard.
        c = std::max<int64_t>(0, r + offset);
      }
    }
  });

  return result;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ zeros ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor zeros(IntArrayRef size, const TensorOptions& options) {
  auto result = at::empty(size, options);
  return result.zero_();
}

Tensor& zeros_out(Tensor& result, IntArrayRef size) {
  if (result.is_sparse()) {
    result.sparse_resize_and_clear_(size, size.size(), 0.);
    return result;
  } else {
    result.resize_(size);
  }
  return result.zero_();
}

Tensor zeros_like(
    const Tensor& self,
    const TensorOptions& options,
    c10::optional<c10::MemoryFormat> optional_memory_format) {
  if (options.layout() == kSparse && self.is_sparse()) {
    auto res = at::empty({0}, options); // to be resized
    res.sparse_resize_and_clear_(
        self.sizes(), self.sparse_dim(), self.dense_dim());
    return res;
  }
  auto result = at::empty_like(self, options, optional_memory_format);
  return result.zero_();
}

Tensor new_zeros(
    const Tensor& self,
    IntArrayRef size,
    const TensorOptions& options
    ) {
  return at::zeros(size, self.options().merge_in(options));
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~ bartlett_window ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor bartlett_window(int64_t window_length, const TensorOptions& options) {
  return native::bartlett_window(window_length, /*periodic=*/true, options);
}

Tensor bartlett_window(
    int64_t window_length,
    bool periodic,
    const TensorOptions& options) {
  window_function_checks("bartlett_window", options, window_length);
  if (window_length == 0) {
    return at::empty({0}, options);
  }
  if (window_length == 1) {
    return native::ones({1}, options);
  }
  if (periodic) {
    window_length += 1;
  }
  auto window = native::arange(window_length, options).mul_(2. / static_cast<double>(window_length - 1));
  const int64_t first_half_size = ((window_length - 1) >> 1) + 1;
  window.narrow(0, first_half_size, window_length - first_half_size).mul_(-1).add_(2);
  return periodic ? window.narrow(0, 0, window_length - 1) : window;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~ blackman_window ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor blackman_window(int64_t window_length, const TensorOptions& options) {
  return native::blackman_window(window_length, /*periodic=*/true, options);
}

Tensor blackman_window(
    int64_t window_length,
    bool periodic,
    const TensorOptions& options) {
  window_function_checks("blackman_window", options, window_length);
  if (window_length == 1) {
    return native::ones({1}, options);
  }
  if (periodic) {
    window_length += 1;
  }
  // from https://en.wikipedia.org/wiki/Window_function#Blackman_window
  auto window = native::arange(window_length, options).mul_(M_PI / static_cast<double>(window_length - 1));
  window = window.mul(4).cos_().mul_(0.08) - window.mul(2).cos_().mul_(0.5) + 0.42;
  return periodic ? window.narrow(0, 0, window_length - 1) : window;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~ hamming_window ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor hamming_window(int64_t window_length, const TensorOptions& options) {
  return native::hamming_window(window_length, /*periodic=*/true, options);
}

Tensor hamming_window(
    int64_t window_length,
    bool periodic,
    const TensorOptions& options) {
  return native::hamming_window(
      window_length, periodic, /*alpha=*/0.54, options);
}

Tensor hamming_window(
    int64_t window_length,
    bool periodic,
    double alpha,
    const TensorOptions& options) {
  return native::hamming_window(
      window_length, periodic, alpha, /*beta=*/0.46, options);
}

Tensor hamming_window(
    int64_t window_length,
    bool periodic,
    double alpha,
    double beta,
    const TensorOptions& options) {
  window_function_checks("hamming_window", options, window_length);
  if (window_length == 0) {
    return at::empty({0}, options);
  }
  if (window_length == 1) {
    return native::ones({1}, options);
  }
  if (periodic) {
    window_length += 1;
  }
  auto window = native::arange(window_length, options);
  window.mul_(M_PI * 2. / static_cast<double>(window_length - 1)).cos_().mul_(-beta).add_(alpha);
  return periodic ? window.narrow(0, 0, window_length - 1) : window;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ hann_window ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor hann_window(int64_t window_length, const TensorOptions& options) {
  return native::hann_window(window_length, /*periodic=*/true, options);
}

Tensor hann_window(
    int64_t window_length,
    bool periodic,
    const TensorOptions& options) {
  window_function_checks("hann_window", options, window_length);
  return native::hamming_window(
      window_length, periodic, /*alpha=*/0.5, /*beta=*/0.5, options);
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~ vandermonde_matrix ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


Tensor vander(const Tensor& x, c10::optional<int64_t> N, bool increasing) {
  TORCH_CHECK(x.dim() == 1, "x must be a one-dimensional tensor.");

  // Acquires n, defaulting to size if not provided
  int64_t n = x.size(0);
  if (N.has_value()) {
    n = *N;
    TORCH_CHECK(n >= 0, "N must be non-negative.");
  }

  // Note: result is long if x is an integer tensor (like int8) because
  // cumprod promotes integer tensors to long
  auto result = at::empty({x.size(0), n}, x.options().dtype(at::promote_types(x.scalar_type(), c10::ScalarType::Long)));

  if (n > 0) {
    result.select(1, 0).fill_(1);
  }
  if (n > 1) {
    result.slice(1, 1).copy_(x.unsqueeze(1));
    result.slice(1, 1).copy_(at::cumprod(result.slice(1, 1), 1));
  }

  if (!increasing) {
    return at::flip(result, {1});
  }
  return result;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ tensor ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

template <typename T>
Tensor tensor_cpu(ArrayRef<T> values, const TensorOptions& options) {
  auto result = at::empty(values.size(), options);
  AT_ASSERT(result.is_contiguous());
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(result.scalar_type(), "tensor_cpu", [&] {
    std::copy(values.begin(), values.end(), result.template data_ptr<scalar_t>());
  });
  return result;
}

template <typename T>
Tensor tensor_backend(ArrayRef<T> values, const TensorOptions& options) {
  auto cpu_tensor = tensor_cpu(values, options.device(DeviceType::CPU));
  return cpu_tensor.to(options.device());
}

template <typename T>
Tensor tensor_complex_cpu(ArrayRef<T> values, const TensorOptions& options) {
  auto result = at::empty(values.size(), options);
  AT_ASSERT(result.is_contiguous());
  AT_DISPATCH_COMPLEX_TYPES(result.scalar_type(), "tensor_cpu", [&] {
    std::copy(values.begin(), values.end(), result.template data_ptr<scalar_t>());
  });
  return result;
}

template <typename T>
Tensor tensor_complex_backend(ArrayRef<T> values, const TensorOptions& options) {
  auto cpu_tensor = tensor_complex_cpu(values, options.device(DeviceType::CPU));
  return cpu_tensor.to(options.device());
}

#define TENSOR(T, _1)                                               \
  Tensor tensor(ArrayRef<T> values, const TensorOptions& options) { \
    if (options.device().type() != c10::DeviceType::CPU) {          \
      return tensor_backend(values, options);                       \
    } else {                                                        \
      return tensor_cpu(values, options);                           \
    }                                                               \
  }
AT_FORALL_SCALAR_TYPES_AND3(Bool, Half, BFloat16, TENSOR)
#undef TENSOR

#define TENSOR(T, _1)                                               \
  Tensor tensor(ArrayRef<T> values, const TensorOptions& options) { \
    if (options.device().type() != c10::DeviceType::CPU) {          \
      return tensor_complex_backend(values, options);               \
    } else {                                                        \
      return tensor_complex_cpu(values, options);                   \
    }                                                               \
  }
AT_FORALL_COMPLEX_TYPES(TENSOR)
#undef TENSOR

Tensor from_file(std::string filename, c10::optional<bool> shared, c10::optional<int64_t> size, const TensorOptions& options) {
    TORCH_CHECK(!options.pinned_memory(), "tensors constructed from a file cannot be pinned");
    int64_t my_size = size.value_or(0);
    int flags = shared.value_or(false) ? TH_ALLOCATOR_MAPPED_SHARED : 0;
    auto dtype = options.dtype();
    size_t size_bytes = my_size * dtype.itemsize();
    auto storage_impl = c10::make_intrusive<at::StorageImpl>(
        c10::StorageImpl::use_byte_size_t(),
        size_bytes,
        THMapAllocator::makeDataPtr(
            filename.c_str(), flags, size_bytes, nullptr),
        /*allocator=*/nullptr,
        /*resizable=*/false);
    auto tensor = detail::make_tensor<at::TensorImpl>(
        storage_impl, at::DispatchKey::CPU, dtype);
    tensor.unsafeGetTensorImpl()->set_sizes_contiguous({my_size});
    return tensor;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ clone ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Tensor clone(const Tensor& src, c10::optional<c10::MemoryFormat> optional_memory_format) {
  auto memory_format =
      optional_memory_format.value_or(MemoryFormat::Preserve);
  if (memory_format == MemoryFormat::Preserve) {
    if (src.is_non_overlapping_and_dense()) {
      // Copy all strides
      auto self = at::empty_strided(src.sizes(), src.strides(), src.options());
      self.copy_(src);
      return self;
    } else {
      memory_format = src.suggest_memory_format();
    }
  }
  auto self = at::empty_like(src, src.options(), memory_format);
  self.copy_(src);
  return self;
}

// ~~~~~~~~~~~~~~~~~~~~~~~~~ named tensor overloads ~~~~~~~~~~~~~~~~~~~~~~~~~~~
// In the short term, these exist.
// In the long term, we should move DimnameList into TensorOptions to avoid
// having these overloads.

Tensor full(
    IntArrayRef size,
    Scalar fill_value,
    optional<DimnameList> names,
    const TensorOptions& options) {

  TORCH_CHECK(options.layout() != kSparse,
    "full(...) is not implemented for sparse layout");

  auto result = at::empty(size, names, infer_full_options(fill_value, options));
  return result.fill_(fill_value);
}

Tensor ones(
    IntArrayRef size,
    optional<DimnameList> names,
    const TensorOptions& options) {
  return native::full(size, /*fill_value=*/1., names, options);
}

Tensor zeros(
    IntArrayRef size,
    optional<DimnameList> names,
    const TensorOptions& options) {
  return native::full(size, /*fill_value=*/0., names, options);
}

Tensor randn(
    IntArrayRef size,
    optional<DimnameList> names,
    const TensorOptions& options) {
  return native::randn(size, c10::nullopt, names, options);
}

Tensor randn(
    IntArrayRef size,
    c10::optional<Generator> generator,
    optional<DimnameList> names,
    const TensorOptions& options) {
  auto result = at::empty(size, names, options);
  return result.normal_(0, 1, generator);
}

Tensor rand(
    IntArrayRef size,
    optional<DimnameList> names,
    const TensorOptions& options) {
  return native::rand(size, c10::nullopt, names, options);
}

Tensor rand(
    IntArrayRef size,
    c10::optional<Generator> generator,
    optional<DimnameList> names,
    const TensorOptions& options) {
  auto result = at::empty(size, names, options);
  return result.uniform_(0, 1, generator);
}


} // namespace native
} // namespace at

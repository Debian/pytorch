#include <cmath>
#include <type_traits>
#include <ATen/Config.h>
#include <ATen/Dispatch.h>
#include <ATen/CPUGenerator.h>
#include <ATen/Utils.h>
#include <ATen/Generator.h>
#include <ATen/Parallel.h>

#include <ATen/cpu/vml.h>
#include <ATen/cpu/vec256/vec256.h>
#include <ATen/cpu/vec256/functional.h>

#include <ATen/native/Distributions.h>
#include <ATen/native/TensorIterator.h>
#include <ATen/native/UnaryOps.h>

#include <ATen/native/cpu/Loops.h>
#include <ATen/native/cpu/zmath.h>
#include <ATen/native/Math.h>
#include <ATen/core/DistributionsHelper.h>
#include <ATen/native/cpu/DistributionTemplates.h>

#if AT_MKL_ENABLED()
#include <mkl.h>
#endif

namespace at { namespace native {
namespace {

using namespace vec256;

static void sigmoid_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), "sigmoid_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return ((scalar_t)(1) / ((scalar_t)(1) + std::exp((-a)))); },
        [=](Vec256<scalar_t> a) {
          a = Vec256<scalar_t>((scalar_t)(0)) - a;
          a = a.exp();
          a = Vec256<scalar_t>((scalar_t)(1)) + a;
          a = a.reciprocal();
          return a;
        });
  });
}

template<typename T>
T abs_impl(T v) {
  return std::abs(v);
}
template<>
uint8_t abs_impl(uint8_t v) {
  return v;
}

static void abs_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "abs_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return abs_impl(a); },
        [=](Vec256<scalar_t> a) { return a.abs(); });
  });
}

static void angle_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "angle_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return angle_impl(a); },
        [=](Vec256<scalar_t> a) { return a.angle(); });
  });
}

static void real_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "real_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return real_impl(a); },
        [=](Vec256<scalar_t> a) { return a.real(); });
  });
}

static void imag_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "imag_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return imag_impl(a); },
        [=](Vec256<scalar_t> a) { return a.imag(); });
  });
}

static void conj_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "conj_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return conj_impl(a); },
        [=](Vec256<scalar_t> a) { return a.conj(); });
  });
}

static void bitwise_not_kernel(TensorIterator& iter) {
  if (iter.dtype() == ScalarType::Bool) {
    // Boolean type does not work with ~ (bitwise NOT) in C++. bitwise_not wraps this operation for both Boolean and
    // integral types.
    cpu_kernel(
          iter,
          [](bool a) {
            return !a;
          });
  } else {
    AT_DISPATCH_INTEGRAL_TYPES(iter.dtype(), "bitwise_not_cpu", [&]() {
      cpu_kernel(
          iter,
          [](scalar_t a) -> scalar_t {
            return ~a;
      });
    });
  }
}

static void frac_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "frac_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return a - std::trunc(a); },
        [=](Vec256<scalar_t> a) { return a.frac(); });
  });
}

static void logical_not_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND2(kBool, kHalf, iter.dtype(1), "logical_not_cpu", [&]() {
    using self_t = scalar_t;
    AT_DISPATCH_ALL_TYPES_AND2(kBool, kHalf, iter.dtype(0), "logical_not_cpu", [&]() {
      cpu_kernel(iter, [](self_t a) -> scalar_t { return static_cast<scalar_t>(!a); });
    });
  });
}

static void reciprocal_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), "reciprocal_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return decltype(a)(1.0) / a; },
        [=](Vec256<scalar_t> a) { return a.reciprocal(); });
  });
}

static void neg_kernel(TensorIterator& iter) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "neg_cpu", [&]() {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t { return -a; },
        [=](Vec256<scalar_t> a) { return a.neg(); });
  });
}

static void sign_kernel(TensorIterator& iter){
  if(iter.dtype() == ScalarType::Bool){
      cpu_kernel(iter, [=](bool x) -> bool { return x; });
  } else {
    AT_DISPATCH_ALL_TYPES_AND(ScalarType::Half, iter.dtype(), "sign_cpu", [&]() {
        auto zero_vec = Vec256<scalar_t>((scalar_t)(0));
        auto one_vec = Vec256<scalar_t>((scalar_t)(1));

        cpu_kernel_vec(
            iter,
            [=](scalar_t a) -> scalar_t { return (0 < a) - (a < 0); },
            [=](Vec256<scalar_t> self_vec){

                // Comparision operators returns bitmask.
                auto left = Vec256<scalar_t>::blendv(zero_vec, one_vec, zero_vec < self_vec);
                auto right = Vec256<scalar_t>::blendv(zero_vec, one_vec, self_vec < zero_vec);

                return left - right;
            });
    });
  }
}

static void sinh_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), "sinh_cpu", [&]() {
    cpu_kernel(
        iter,
        [=](scalar_t a) -> scalar_t { return std::sinh(a); });
  });
}

static void cosh_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), "cosh_cpu", [&]() {
    cpu_kernel(
        iter,
        [=](scalar_t a) -> scalar_t { return std::cosh(a); });
  });
}

static void digamma_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "digamma", [&]() {
    cpu_kernel(
        iter,
        [=](scalar_t a) -> scalar_t { return calc_digamma(a); });
  });
}

static void trigamma_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "trigamma", [&]() {
    cpu_kernel(
        iter,
        [=](scalar_t a) -> scalar_t { return trigamma(a); });
  });
}

static void polygamma_kernel(TensorIterator& iter, int64_t n) {
  switch (n) {
    case 0: digamma_kernel(iter); break;
    case 1: trigamma_kernel(iter); break;
    default: TORCH_CHECK(false, "polygamma(n,x) is not implemented for n>=2, but was ", n);
  }
}

static void clamp_kernel(TensorIterator& iter, Scalar min_scalar, Scalar max_scalar) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "clamp_cpu", [&]() {
    ztype<scalar_t>::value_t (*zabs_)(scalar_t) = zabs;
    auto min = min_scalar.to<scalar_t>();
    auto max = max_scalar.to<scalar_t>();
    auto min_vec = Vec256<scalar_t>(min);
    auto max_vec = Vec256<scalar_t>(max);
    cpu_kernel_vec(iter,
     [=](scalar_t a) -> scalar_t { return zabs_(a) < zabs_(min) ? min : (zabs_(a) > zabs_(max) ? max : a); },
     [=](Vec256<scalar_t> a) { return vec256::clamp(a, min_vec, max_vec); });
  });
}

static void clamp_max_kernel(TensorIterator& iter, Scalar max_scalar) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "clamp_max_cpu", [&]() {
    ztype<scalar_t>::value_t (*zabs_)(scalar_t) = zabs;
    auto max = max_scalar.to<scalar_t>();
    auto max_vec = Vec256<scalar_t>(max);
    cpu_kernel_vec(iter,
     [=](scalar_t a) -> scalar_t { return zabs_(a) > zabs_(max) ? max : a; },
     [=](Vec256<scalar_t> a) { return vec256::clamp_max(a, max_vec); });
  });
}

static void clamp_min_kernel(TensorIterator& iter, Scalar min_scalar) {
  AT_DISPATCH_ALL_TYPES_AND_COMPLEX(iter.dtype(), "clamp_min_cpu", [&]() {
    ztype<scalar_t>::value_t (*zabs_)(scalar_t) = zabs;
    auto min = min_scalar.to<scalar_t>();
    auto min_vec = Vec256<scalar_t>(min);
    cpu_kernel_vec(iter,
     [=](scalar_t a) -> scalar_t { return zabs_(a) < zabs_(min) ? min : a; },
     [=](Vec256<scalar_t> a) { return vec256::clamp_min(a, min_vec); });
  });
}

static void cauchy_kernel(TensorIterator& iter, double median, double sigma, Generator* gen) {
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  templates::cpu::cauchy_kernel(iter, median, sigma, generator);
}

#if !AT_MKL_ENABLED()
void bernoulli_mkl_kernel(Tensor &output, const double p, Generator* gen) {
  // Use AT_ASSERTM because this should never be reached, and AT_ASSERTM tells
  // users to report this as a bug.
  AT_ASSERTM(false, "ATen not compiled with MKL");
}
#else
void bernoulli_mkl_kernel(Tensor &self, const double p, Generator* gen) {
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  int64_t seed;
  {
    // See Note [Acquire lock when using random generators]
    std::lock_guard<std::mutex> lock(generator->mutex_);
    seed = generator->random();
  }
  int64_t n = self.numel();
  bool contig = self.is_contiguous();

  AT_DISPATCH_ALL_TYPES_AND(at::ScalarType::Bool, self.scalar_type(), "bernoulli_scalar_cpu_", [&] {
    at::Tensor tmp_int_tensor;
    if (std::is_same<scalar_t, int>::value && contig) {
      tmp_int_tensor = self;
    } else {
      tmp_int_tensor = at::empty(self.sizes(), self.options().dtype(at::kInt));
    }

    scalar_t *self_ptr = self.data_ptr<scalar_t>();
    int *sample_int_ptr = tmp_int_tensor.data_ptr<int>();

    auto sample = [&](int64_t begin, int64_t end) {
      int64_t len = end - begin;
      if (len > 0) {
        VSLStreamStatePtr stream;
        vslNewStream(&stream, VSL_BRNG_MCG31, seed);
        vslSkipAheadStream(stream, begin);
        viRngBernoulli(VSL_RNG_METHOD_BERNOULLI_ICDF, stream, len,
          sample_int_ptr + begin, p);
        vslDeleteStream(&stream);

        // vectorized copy if using buffer and contiguous, i.e., being non-int
        // type and contiguous
        if (!std::is_same<scalar_t, int>::value && contig) {
          scalar_t *self_seg = self_ptr + begin;
          int* tmp_seg = sample_int_ptr + begin;
          at::vec256::convert<int, scalar_t>(tmp_seg, self_seg, len);
        }
      }
    };

    parallel_for(0, n, /* grain_size= */ 800, sample);

    // copy_ if using buffer and non contiguous
    if (!contig) {
      self.copy_(tmp_int_tensor);
    }
  });
}
#endif

static void exponential_kernel(TensorIterator& iter, double lambda, Generator* gen) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "exponential_cpu", [&]() {
    CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
    std::lock_guard<std::mutex> lock(generator->mutex_);
    at::exponential_distribution<double> exponential(lambda);
    cpu_serial_kernel(iter, [&exponential, generator]() -> scalar_t {
      return static_cast<scalar_t>(exponential(generator));
    });
  });
}

static void geometric_kernel(TensorIterator& iter, double p, Generator* gen) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "geometric_cpu", [&]() {
    CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
    std::lock_guard<std::mutex> lock(generator->mutex_);
    cpu_serial_kernel(iter, [p, generator]() -> scalar_t {
      at::geometric_distribution<double> geometric(p);
      return (scalar_t)geometric(generator);
    });
  });
}

static void log_normal_kernel(TensorIterator& iter, double mean, double std, Generator* gen) {
  AT_DISPATCH_FLOATING_TYPES(iter.dtype(), "log_normal_cpu", [&]() {
    CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
    std::lock_guard<std::mutex> lock(generator->mutex_);
    cpu_serial_kernel(iter, [mean, std, generator]() -> scalar_t {
      at::lognormal_distribution<double> logNormal(mean, std);
      return (scalar_t)logNormal(generator);
    });
  });
}

#ifdef __AVX2__
#include <ATen/native/cpu/avx_mathfun.h>

static void normal_fill_16_AVX2(float *data,
                         const __m256* two_pi,
                         const __m256* one,
                         const __m256* minus_two,
                         const __m256* mean,
                         const __m256* std_v) {
  const __m256 u1 = _mm256_sub_ps(*one, _mm256_loadu_ps(data));
  const __m256 u2 = _mm256_loadu_ps(data + 8);
  // sincos256_ps and log256_ps are from avx_mathfun.h
  const __m256 radius = _mm256_sqrt_ps(_mm256_mul_ps(*minus_two, log256_ps(u1)));
  const __m256 theta = _mm256_mul_ps(*two_pi, u2);
  __m256 sintheta, costheta;
  sincos256_ps(theta, &sintheta, &costheta);
  const __m256 n1 = _mm256_mul_ps(radius, costheta);
  const __m256 n2 = _mm256_mul_ps(radius, sintheta);
  _mm256_storeu_ps(data, _mm256_fmadd_ps(n1, *std_v, *mean));
  _mm256_storeu_ps(data + 8, _mm256_fmadd_ps(n2, *std_v, *mean));
}

void normal_fill_AVX2(Tensor& self, const float mean, const float std, Generator* gen) {
  float *data = self.data_ptr<float>();
  auto size = self.numel();
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  std::lock_guard<std::mutex> lock(generator->mutex_);
  for (int64_t i = 0; i < size; ++i) {
    at::uniform_real_distribution<float> uniform(0, 1);
    data[i] = uniform(generator);
  }
   const __m256 two_pi = _mm256_set1_ps(2.0f * M_PI);
  const __m256 one = _mm256_set1_ps(1.0f);
  const __m256 minus_two = _mm256_set1_ps(-2.0f);
  const __m256 mean_v = _mm256_set1_ps(mean);
  const __m256 std_v = _mm256_set1_ps(std);

  for (int64_t i = 0; i < size - 15; i += 16) {
    normal_fill_16_AVX2(data + i, &two_pi, &one, &minus_two, &mean_v, &std_v);
  }

  if (size % 16 != 0) {
    // Recompute the last 16 values.
    data = data + size - 16;
    for (int64_t i = 0; i < 16; ++i) {
      at::uniform_real_distribution<float> uniform(0, 1);
      data[i] = uniform(generator);
    }
    normal_fill_16_AVX2(data, &two_pi, &one, &minus_two, &mean_v, &std_v);
  }
}
#endif

template <typename scalar_t>
static void normal_fill_16(scalar_t *data, const scalar_t mean, const scalar_t std) {
  for (int j = 0; j < 8; ++j) {
    const scalar_t u1 = 1 - data[j]; // [0, 1) -> (0, 1] for log.
    const scalar_t u2 = data[j + 8];
    const scalar_t radius = std::sqrt(-2 * std::log(u1));
    const scalar_t theta = 2.0f * M_PI * u2;
    data[j] = radius * std::cos(theta) * std + mean;
    data[j + 8] = radius * std::sin(theta) * std + mean;
  }
}

template <typename scalar_t>
void normal_fill(Tensor& self, const scalar_t mean, const scalar_t std, Generator* gen) {
  scalar_t *data = self.data_ptr<scalar_t>();
  auto size = self.numel();
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  std::lock_guard<std::mutex> lock(generator->mutex_);
  for (int64_t i = 0; i < size; ++i) {
    at::uniform_real_distribution<scalar_t> uniform(0, 1);
    data[i] = uniform(generator);
  }

  for (int64_t i = 0; i < size - 15; i += 16) {
    normal_fill_16<scalar_t>(data + i, mean, std);
  }
  if (size % 16 != 0) {
    // Recompute the last 16 values.
    data = data + size - 16;
    for (int64_t i = 0; i < 16; ++i) {
      at::uniform_real_distribution<scalar_t> uniform(0, 1);
      data[i] = uniform(generator);
    }
    normal_fill_16<scalar_t>(data, mean, std);
  }
}

std::vector<int64_t> computeStrideForComplex(IntArrayRef oldstride) {
  auto res = oldstride.vec();
  for(size_t i = 0; i < res.size(); i++) {
    res[i] = res[i] * 2;
  }
  res.emplace_back(1);
  return res;
}

// expects as input a complex tensor and returns back a float tensor
// containing the complex values in the last two dimensions
Tensor view_complex_as_float(const Tensor& self) {
  TORCH_INTERNAL_ASSERT(self.is_complex());
  auto new_sizes = self.sizes().vec();
  // last dimension will always have two elements containing the real and imag vals
  new_sizes.emplace_back(2);
  auto new_strides = computeStrideForComplex(self.strides());
  if(self.scalar_type() == at::kComplexFloat) {
    float* data = reinterpret_cast<float*>(self.data_ptr<std::complex<float>>());
    return at::from_blob(data, new_sizes, new_strides, dtype(at::kFloat));
  } else {
    double* data = reinterpret_cast<double*>(self.data_ptr<std::complex<double>>());
    return at::from_blob(data, new_sizes, new_strides, dtype(at::kDouble));
  }
}

void normal_kernel(Tensor& self, double mean, double std, Generator* gen) {
  if(self.is_complex()) {
    // note: float_tensor lives only as long as the self tensor lives
    auto float_tensor = at::native::view_complex_as_float(self);
    // variance for normal distribution of the real and imaginary values
    // is half of the input variance
    return normal_kernel(float_tensor, mean, std/(std::sqrt(2)), gen);
  }
  auto size = self.numel();
  if (self.scalar_type() == ScalarType::Float && size >= 16 && self.is_contiguous()) {
#ifdef __AVX2__
    normal_fill_AVX2(self, static_cast<float>(mean), static_cast<float>(std), gen);
#else
    normal_fill(self, static_cast<float>(mean), static_cast<float>(std), gen);
#endif
  } else {
    AT_DISPATCH_FLOATING_TYPES(self.scalar_type(), "norma_cpu", [&] {
      if (size >= 16 && self.is_contiguous()) {
        normal_fill<scalar_t>(self, static_cast<scalar_t>(mean), static_cast<scalar_t>(std), gen);
      } else {
        auto iter = TensorIterator::nullary_op(self);
        CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
        std::lock_guard<std::mutex> lock(generator->mutex_);
        cpu_serial_kernel(iter, [mean, std, generator]() -> scalar_t {
          at::normal_distribution<double> normal(mean, std);
          return (scalar_t)normal(generator);
        });
      }
    });
  }
}

static void random_from_to_kernel(TensorIterator& iter, uint64_t range, int64_t base, Generator* gen) {
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  templates::cpu::random_from_to_kernel(iter, range, base, generator);
}

static void random_kernel(TensorIterator& iter, Generator* gen) {
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  templates::cpu::random_kernel(iter, generator);
}

// This is the special kernel to handle single specific case:
// from(inclusive) = std::numeric_limits<int64_t>::lowest()
// to(exclusive) = None (= std::numeric_limits<int64_t>::max() + 1)
static void random_full_64_bits_range_kernel(TensorIterator& iter, Generator* gen) {
  CPUGenerator* generator = get_generator_or_default<CPUGenerator>(gen, detail::getDefaultCPUGenerator());
  templates::cpu::random_full_64_bits_range_kernel(iter, generator);
}

static void rsqrt_kernel(TensorIterator& iter) {
  AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), "rsqrt_cpu", [&] {
    cpu_kernel_vec(
        iter,
        [=](scalar_t a) -> scalar_t {
          return ((scalar_t)1) / std::sqrt(a);
        },
        [=](Vec256<scalar_t> a) { return a.rsqrt(); });
  });
}

// TODO: Disable cont. branch to test more risky code

#define IMPLEMENT_FLOAT_KERNEL(dispatchtypes, op)                             \
  static void op##_kernel(TensorIterator& iter) {                             \
    TORCH_INTERNAL_ASSERT(iter.ntensors() == 2);                              \
    AT_DISPATCH_FLOATING_TYPES(iter.dtype(), op##_vml_cpu, [&]() {            \
      iter.serial_for_each(                                                   \
          [&](char** data_, const int64_t* strides, int64_t n) { \
            scalar_t* out_data = reinterpret_cast<scalar_t*>(data_[0]);       \
            scalar_t* in_data = reinterpret_cast<scalar_t*>(data_[1]);        \
            int64_t out_stride = strides[0] / sizeof(scalar_t);               \
            int64_t in_stride = strides[1] / sizeof(scalar_t);                \
            if (out_stride == 1 && in_stride == 1) {                          \
              vml::v##op(out_data, in_data, n);                               \
            } else {                                                          \
              static constexpr int64_t WIDTH = 131072 / sizeof(scalar_t);     \
              for (int64_t i = 0; i < n; i += WIDTH) {                        \
                scalar_t buffer[WIDTH];                                       \
                int64_t width = WIDTH;                                        \
                width = std::min(width, n - i);                               \
                for (int64_t j = 0; j < width; j++)                           \
                  buffer[j] = in_data[in_stride * (i + j)];                   \
                vml::v##op(buffer, buffer, width);                            \
                for (int64_t j = 0; j < width; j++)                           \
                  out_data[out_stride * (i + j)] = buffer[j];                 \
              }                                                               \
            }                                                                 \
          },                                                                  \
          {0, iter.numel()});                                                 \
    });                                                                       \
  }                                                                           \
  REGISTER_DISPATCH(op##_stub, &op##_kernel)

#define IMPLEMENT_COMPLEX_KERNEL(dispatchtypes, op)                             \
  static void op##_kernel(TensorIterator& iter) {                             \
    TORCH_INTERNAL_ASSERT(iter.ntensors() == 2);                              \
    AT_DISPATCH_FLOATING_AND_COMPLEX_TYPES(iter.dtype(), op##_vml_cpu, [&]() {\
      iter.serial_for_each(                                                   \
          [&](char** data_, const int64_t* strides, int64_t n) {              \
            scalar_t* out_data = reinterpret_cast<scalar_t*>(data_[0]);       \
            scalar_t* in_data = reinterpret_cast<scalar_t*>(data_[1]);        \
            int64_t out_stride = strides[0] / sizeof(scalar_t);               \
            int64_t in_stride = strides[1] / sizeof(scalar_t);                \
            if (out_stride == 1 && in_stride == 1) {                          \
              vml::v##op(out_data, in_data, n);                               \
            } else {                                                          \
              static constexpr int64_t WIDTH = 131072 / sizeof(scalar_t);     \
              for (int64_t i = 0; i < n; i += WIDTH) {                        \
                scalar_t buffer[WIDTH];                                       \
                int64_t width = WIDTH;                                        \
                width = std::min(width, n - i);                               \
                for (int64_t j = 0; j < width; j++)                           \
                  buffer[j] = in_data[in_stride * (i + j)];                   \
                vml::v##op(buffer, buffer, width);                            \
                for (int64_t j = 0; j < width; j++)                           \
                  out_data[out_stride * (i + j)] = buffer[j];                 \
              }                                                               \
            }                                                                 \
          },                                                                  \
          {0, iter.numel()});                                                 \
    });                                                                       \
  }                                                                           \
  REGISTER_DISPATCH(op##_stub, &op##_kernel)

} // anonymous namespace

REGISTER_DISPATCH(rsqrt_stub, &rsqrt_kernel);
REGISTER_DISPATCH(sigmoid_stub, &sigmoid_kernel);
REGISTER_DISPATCH(bernoulli_mkl_stub, &bernoulli_mkl_kernel);
REGISTER_DISPATCH(cauchy_stub, &cauchy_kernel);
REGISTER_DISPATCH(exponential_stub, &exponential_kernel);
REGISTER_DISPATCH(geometric_stub, &geometric_kernel);
REGISTER_DISPATCH(log_normal_stub, &log_normal_kernel);
REGISTER_DISPATCH(normal_stub, &normal_kernel);
REGISTER_DISPATCH(random_from_to_stub, &random_from_to_kernel);
REGISTER_DISPATCH(random_full_64_bits_range_stub, &random_full_64_bits_range_kernel);
REGISTER_DISPATCH(random_stub, &random_kernel);
REGISTER_DISPATCH(abs_stub, &abs_kernel);
REGISTER_DISPATCH(angle_stub, &angle_kernel);
REGISTER_DISPATCH(real_stub, &real_kernel);
REGISTER_DISPATCH(imag_stub, &imag_kernel);
REGISTER_DISPATCH(conj_stub, &conj_kernel);
REGISTER_DISPATCH(bitwise_not_stub, &bitwise_not_kernel);
REGISTER_DISPATCH(logical_not_stub, &logical_not_kernel);
REGISTER_DISPATCH(frac_stub, &frac_kernel);
REGISTER_DISPATCH(reciprocal_stub, &reciprocal_kernel);
REGISTER_DISPATCH(neg_stub, &neg_kernel);
REGISTER_DISPATCH(sign_stub, &sign_kernel);
REGISTER_DISPATCH(sinh_stub, &sinh_kernel);
REGISTER_DISPATCH(cosh_stub, &cosh_kernel);
REGISTER_DISPATCH(digamma_stub, &digamma_kernel);
REGISTER_DISPATCH(trigamma_stub, &trigamma_kernel);
REGISTER_DISPATCH(polygamma_stub, &polygamma_kernel);
REGISTER_DISPATCH(clamp_stub, &clamp_kernel);
REGISTER_DISPATCH(clamp_max_stub, &clamp_max_kernel);
REGISTER_DISPATCH(clamp_min_stub, &clamp_min_kernel);


// IMPLEMENT_FLOAT_KERNEL(ALL, abs)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, acos)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, asin)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, atan)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, ceil)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, cos)
// IMPLEMENT_FLOAT_KERNEL(FLOATING, cosh)
IMPLEMENT_FLOAT_KERNEL(FLOATING, erf)
IMPLEMENT_FLOAT_KERNEL(FLOATING, erfc)
IMPLEMENT_FLOAT_KERNEL(FLOATING, erfinv)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, exp)
IMPLEMENT_FLOAT_KERNEL(FLOATING, expm1)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, floor)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, log)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, log10)
IMPLEMENT_FLOAT_KERNEL(FLOATING, log1p)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, log2)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, round)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, sin)
// IMPLEMENT_FLOAT_KERNEL(FLOATING, sinh)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, sqrt)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, tan)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, tanh)
IMPLEMENT_COMPLEX_KERNEL(FLOATING, trunc)
IMPLEMENT_FLOAT_KERNEL(FLOATING, lgamma)

}} // namespace at::native

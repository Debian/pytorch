#include "test/cpp/tensorexpr/test_base.h"
#include "torch/csrc/jit/tensorexpr/ir.h"
#include "torch/csrc/jit/tensorexpr/tensor.h"

namespace torch {
namespace jit {
using namespace torch::jit::tensorexpr;

void testTypeTest01() {
  KernelScope kernel_scope;
  {
    Dtype dt1 = kInt;
    ASSERT_EQ(dt1, kInt);
  }
  {
    Dtype dt2_a(kInt, 8);
    Dtype dt2_b(kInt, 4);
    Dtype dt2_c(ScalarType::Int, 8);
    ASSERT_EQ(dt2_a, dt2_c);
    ASSERT_NE(dt2_a, dt2_b);
  }
  {
    ASSERT_EQ(kInt, ToDtype<int>());
    ASSERT_EQ(kFloat, ToDtype<float>());
    ASSERT_EQ(kByte, ToDtype<uint8_t>());
    ASSERT_EQ(kChar, ToDtype<int8_t>());
    ASSERT_EQ(kShort, ToDtype<int16_t>());
    ASSERT_EQ(kLong, ToDtype<int64_t>());
    ASSERT_EQ(kHalf, ToDtype<at::Half>());
    ASSERT_EQ(kDouble, ToDtype<double>());
    ASSERT_EQ(kBool, ToDtype<bool>());
  }
  {
    Dtype int32x8(kInt, 8);
    Dtype float32x8(kFloat, 8);
    ASSERT_NE(int32x8, float32x8);
    ASSERT_EQ(float32x8, BinaryOpDtype(int32x8, float32x8));
    ASSERT_EQ(float32x8, BinaryOpDtype(float32x8, int32x8));
    ASSERT_EQ(int32x8, BinaryOpDtype(int32x8, int32x8));
    ASSERT_EQ(float32x8, BinaryOpDtype(float32x8, float32x8));
  }
}

void testTypePropagation() {
  // Same types:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kFloat);
    VarHandle y("y", kFloat);
    ExprHandle body = FloatImm::make(2.f) +
        (x * FloatImm::make(3.f) + FloatImm::make(4.f) * y);
    ASSERT_EQ(body.dtype(), kFloat);
  }
  // Int to bigger int:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kShort);
    VarHandle y("y", kLong);
    ExprHandle body =
        ShortImm::make(2.f) + (x * ShortImm::make(3) + ShortImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kLong);
  }
  // Float to bigger float:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kHalf);
    VarHandle y("y", kDouble);
    ExprHandle body =
        HalfImm::make(2.f) + (x * HalfImm::make(3) + HalfImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kDouble);
  }
  // Int to Float:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kFloat);
    VarHandle y("y", kInt);
    ExprHandle body =
        IntImm::make(2) + (x * IntImm::make(3) + IntImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kFloat);
  }
  // Smaller float, bigger Int:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kHalf);
    VarHandle y("y", kLong);
    ExprHandle body =
        HalfImm::make(2) + (x * HalfImm::make(3) + HalfImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kHalf);
  }
  // Bigger float, smaller Int:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kChar);
    VarHandle y("y", kDouble);
    ExprHandle body =
        CharImm::make(2) + (x * CharImm::make(3) + CharImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kDouble);
  }
  // Sign change char/byte upgrades to short:
  {
    KernelScope kernel_scope;
    VarHandle x("x", kChar);
    VarHandle y("y", kByte);
    ExprHandle body =
        CharImm::make(2) + (x * CharImm::make(3) + CharImm::make(4) * y);
    ASSERT_EQ(body.dtype(), kShort);
  }
}
} // namespace jit
} // namespace torch

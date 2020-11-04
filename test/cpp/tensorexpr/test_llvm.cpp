#ifdef TORCH_ENABLE_LLVM
#include "test/cpp/tensorexpr/test_base.h"

#include "test/cpp/tensorexpr/padded_buffer.h"
#include "test/cpp/tensorexpr/test_utils.h"
#include "torch/csrc/jit/tensorexpr/eval.h"
#include "torch/csrc/jit/tensorexpr/ir.h"
#include "torch/csrc/jit/tensorexpr/ir_printer.h"
#include "torch/csrc/jit/tensorexpr/ir_simplifier.h"
#include "torch/csrc/jit/tensorexpr/llvm_codegen.h"
#include "torch/csrc/jit/tensorexpr/loopnest.h"
#include "torch/csrc/jit/tensorexpr/tensor.h"

#include <cmath>
#include <numeric>

namespace torch {
namespace jit {
using namespace torch::jit::tensorexpr;
using namespace torch::jit::tensorexpr;

using LLVMExprEval = ExprEval<LLVMCodeGen>;

// Typed tests, can't use gtest params here due to the way we instantiate tests.
#define TEST_LLVM_SCALAR_TYPES(_) \
  _(uint8_t, Byte, 24)            \
  _(int8_t, Char, -20)            \
  _(int16_t, Short, 3332)         \
  _(int, Int, 123456)             \
  _(int64_t, Long, 2631563121321) \
  _(float, Float, 0.122)          \
  _(double, Double, 0.21312)      \
  _(at::Half, Half, 0.128f)

#define IMM_TEST(Type, Name, Val)                  \
  void testLLVM##Name##ImmTest() {                 \
    KernelScope kernel_scope;                      \
    auto a = Name##Imm::make(Val);                 \
    LLVMExprEval cg(a);                            \
    if (std::is_floating_point<decltype(Val)>()) { \
      ASSERT_NEAR(cg.value<Type>(), Val, 0.1);     \
    } else {                                       \
      ASSERT_EQ(cg.value<Type>(), Val);            \
    }                                              \
  }
TEST_LLVM_SCALAR_TYPES(IMM_TEST)
#undef IMM_TEST

#define ADD_TEST(Type, Name, Val)                  \
  void testLLVM##Name##AddTest() {                 \
    KernelScope kernel_scope;                      \
    auto a = Name##Imm::make(Val);                 \
    auto b = Name##Imm::make(Val * 2);             \
    auto c = Add::make(a, b);                      \
    LLVMExprEval cg(c);                            \
    if (std::is_floating_point<decltype(Val)>()) { \
      ASSERT_NEAR(cg.value<Type>(), Val * 3, 0.1); \
    } else {                                       \
      ASSERT_EQ(cg.value<Type>(), Val * 3);        \
    }                                              \
  }
TEST_LLVM_SCALAR_TYPES(ADD_TEST)
#undef ADD_TEST

#define SUB_TEST(Type, Name, Val)                  \
  void testLLVM##Name##SubTest() {                 \
    KernelScope kernel_scope;                      \
    auto a = Name##Imm::make(Val * 2);             \
    auto b = Name##Imm::make(Val);                 \
    auto c = Sub::make(a, b);                      \
    LLVMExprEval cg(c);                            \
    if (std::is_floating_point<decltype(Val)>()) { \
      ASSERT_NEAR(cg.value<Type>(), Val, 0.1);     \
    } else {                                       \
      ASSERT_EQ(cg.value<Type>(), Val);            \
    }                                              \
  }
TEST_LLVM_SCALAR_TYPES(SUB_TEST)
#undef SUB_TEST

#define MUL_TEST(Type, Name, Val)                  \
  void testLLVM##Name##MulTest() {                 \
    KernelScope kernel_scope;                      \
    auto a = Name##Imm::make(Val);                 \
    auto b = Name##Imm::make((Type)4);             \
    auto c = Mul::make(a, b);                      \
    LLVMExprEval cg(c);                            \
    if (std::is_floating_point<decltype(Val)>()) { \
      ASSERT_NEAR(cg.value<Type>(), Val * 4, 0.1); \
    } else {                                       \
      ASSERT_EQ(cg.value<Type>(), Val * 4);        \
    }                                              \
  }
TEST_LLVM_SCALAR_TYPES(MUL_TEST)
#undef MUL_TEST

#define DIV_TEST(Type, Name, Val)                  \
  void testLLVM##Name##DivTest() {                 \
    KernelScope kernel_scope;                      \
    auto a = Name##Imm::make((Type)6);             \
    auto b = Name##Imm::make((Type)3);             \
    auto c = Div::make(a, b);                      \
    LLVMExprEval cg(c);                            \
    if (std::is_floating_point<decltype(Val)>()) { \
      ASSERT_NEAR(cg.value<Type>(), 2, 0.1);       \
    } else {                                       \
      ASSERT_EQ(cg.value<Type>(), 2);              \
    }                                              \
  }
TEST_LLVM_SCALAR_TYPES(DIV_TEST)
#undef DIV_TEST

void testLLVMIntToFloatCastTest() {
  KernelScope kernel_scope;
  auto a = IntImm::make(2);
  auto b = Cast::make(kFloat, a);
  LLVMExprEval cg(b, {});
  ASSERT_EQ(cg.value<float>(), 2.0);
}

void testLLVMFloatToIntCastTest() {
  KernelScope kernel_scope;
  auto a = FloatImm::make(2.0);
  auto b = Cast::make(kInt, a);
  LLVMExprEval cg(b);
  ASSERT_EQ(cg.value<int>(), 2);
}

void testLLVMIntToLongCastTest() {
  KernelScope kernel_scope;
  auto a = IntImm::make(12345);
  auto b = Cast::make(kLong, a);
  LLVMExprEval cg(b);
  ASSERT_EQ(cg.value<int64_t>(), 12345);
}

void testLLVMByteToCharCastTest() {
  KernelScope kernel_scope;
  auto a = ByteImm::make(250);
  auto b = Cast::make(kChar, a);
  LLVMExprEval cg(b);
  ASSERT_EQ(cg.value<int8_t>(), (int8_t)250);
}

void testLLVMHalfToLongCastTest() {
  KernelScope kernel_scope;
  auto a = HalfImm::make(2.0);
  auto b = Cast::make(kLong, a);
  LLVMExprEval cg(b);
  ASSERT_EQ(cg.value<int64_t>(), 2);
}

void testLLVMByteToDoubleCastTest() {
  KernelScope kernel_scope;
  auto a = ByteImm::make(2);
  auto b = Cast::make(kDouble, a);
  LLVMExprEval cg(b);
  ASSERT_EQ(cg.value<double>(), 2);
}

void testLLVMLetTest01() {
  KernelScope kernel_scope;

  Placeholder a(BufHandle("A", {1}, kFloat));
  std::vector<float> v = {1, 0};
  std::vector<void*> args({v.data()});
  VarHandle x("x", kFloat);
  auto block = Block::make({
      Let::make(x, 3.f),
      a.store({0}, ExprHandle(2.f) + (x * ExprHandle(3.f) + ExprHandle(4.f))),
  });

  LLVMCodeGen cg(block, {a});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(v[0], 2.f + 3.f * 3.f + 4.f);
}

void testLLVMLetTest02() {
  KernelScope kernel_scope;

  Placeholder a(BufHandle("A", {1}, kFloat));
  std::vector<float> v = {1, 0};
  std::vector<void*> args({v.data()});
  VarHandle x("x", kFloat);
  VarHandle y("y", kFloat);
  auto block = Block::make(
      {Let::make(x, 3.f),
       Let::make(y, 6.f),
       a.store(
           {IntImm::make(0)},
           ExprHandle(2.f) + (x * ExprHandle(3.f) + y * ExprHandle(4.f)))});

  LLVMCodeGen cg(block, {a});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(v[0], 2.f + 3.f * 3.f + 6.f * 4.f);
}

void testLLVMLetTestMultitype() {
  KernelScope kernel_scope;

  Placeholder a(BufHandle("A", {1}, kDouble));
  std::vector<double> v = {1, 0};
  std::vector<void*> args({v.data()});
  VarHandle x("x", kByte);
  VarHandle y("y", kHalf);
  auto block =
      Block::make({Let::make(x, 3),
                   Let::make(y, 6.f),
                   a.store(
                       {0},
                       Cast::make(
                           kDouble,
                           ExprHandle(2.f) +
                               (x * ExprHandle(3.f) + y * ExprHandle(4.f))))});

  LLVMCodeGen cg(block, {a});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(v[0], 2.f + 3 * 3.f + 6.f * 4.f);
}

void testLLVMBufferTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {32}, kFloat));
  std::vector<int32_t> v(5);
  std::vector<void*> args({v.data()});
  auto rv = IntImm::make(0);
  LLVMExprEval cg(rv, {a});
  ASSERT_EQ(cg.value<int>(args), 0);
}

void testLLVMBlockTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {32}, kInt));
  std::vector<int32_t> v = {1, 2};
  std::vector<void*> args({v.data()});

  auto block = Block::make({
      a.store({0}, 3),
      a.store({1}, 4),
      a.store({0}, 4),
  });

  LLVMCodeGen cg(block, {a});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(v[0], 4);
  ASSERT_EQ(v[1], 4);
}

void testLLVMLoadStoreTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {1}, kInt));
  Placeholder b(BufHandle("B", {1}, kInt));
  std::vector<int32_t> a_buffer = {42};
  std::vector<int32_t> b_buffer = {-11};

  auto store = b.store({0}, a.load(0));
  LLVMCodeGen cg(store, {a, b});
  std::vector<void*> args({a_buffer.data(), b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(a_buffer[0], 42);
  ASSERT_EQ(b_buffer[0], 42);
}

void testLLVMIfThenElseTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {1}, kInt));
  Placeholder b(BufHandle("B", {1}, kInt));
  Placeholder c(BufHandle("C", {1}, kInt));
  std::vector<int32_t> a_buffer = {42};
  std::vector<int32_t> b_buffer = {-11};
  std::vector<int32_t> c_buffer = {1};

  auto store = b.store({0}, IfThenElse::make(c.load(0), a.load(0), 0));
  LLVMCodeGen cg(store, {a, b, c});
  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(a_buffer[0], 42);
  ASSERT_EQ(b_buffer[0], 42);
}

void testLLVMVecLoadStoreTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {1}, kInt));
  Placeholder b(BufHandle("B", {1}, kInt));
  std::vector<int32_t> a_buffer = {1, 1, 1, 1};
  std::vector<int32_t> b_buffer = {2, 2, 2, 2};

  auto store = b.storeWithMask(
      {Ramp::make(0, 1, 4)},
      a.loadWithMask(
          {Ramp::make(0, 1, 4)}, Broadcast::make(IntImm::make(1), 4)),
      Broadcast::make(IntImm::make(1), 4));
  LLVMCodeGen cg(store, {a, b});
  std::vector<void*> args({a_buffer.data(), b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(a_buffer[0], 1);
  ASSERT_EQ(a_buffer[1], 1);
  ASSERT_EQ(a_buffer[2], 1);
  ASSERT_EQ(a_buffer[3], 1);
  ASSERT_EQ(b_buffer[0], 1);
  ASSERT_EQ(b_buffer[1], 1);
  ASSERT_EQ(b_buffer[2], 1);
  ASSERT_EQ(b_buffer[3], 1);
}

#define FLOAT_INTRINSICS_TEST(Name, Lanes)                       \
  void testLLVMVecFloat_##Name##Lane##Lanes##Test() {            \
    KernelScope kernel_scope;                                    \
    Placeholder a(BufHandle("A", {1}, kFloat));                  \
    Placeholder b(BufHandle("B", {1}, kFloat));                  \
    float val = 0.5f;                                            \
    std::vector<float> a_buffer(Lanes, val);                     \
    std::vector<float> b_buffer(Lanes, val);                     \
    auto store = b.storeWithMask(                                \
        {Ramp::make(0, 1, Lanes)},                               \
        Name(a.loadWithMask(                                     \
            {Ramp::make(0, 1, Lanes)},                           \
            Broadcast::make(IntImm::make(1), Lanes))),           \
        Broadcast::make(IntImm::make(1), Lanes));                \
    LLVMCodeGen cg(store, {a, b});                               \
    std::vector<void*> args({a_buffer.data(), b_buffer.data()}); \
    ASSERT_EQ(cg.value<int>(args), 0);                           \
    for (int i = 0; i < Lanes; i++) {                            \
      ASSERT_FLOAT_EQ(a_buffer[i], val);                         \
    }                                                            \
  } // namespace jit
FLOAT_INTRINSICS_TEST(erf, 4)
FLOAT_INTRINSICS_TEST(erfc, 4)
FLOAT_INTRINSICS_TEST(acos, 4)
FLOAT_INTRINSICS_TEST(asin, 4)
FLOAT_INTRINSICS_TEST(atan, 4)
FLOAT_INTRINSICS_TEST(cosh, 4)
FLOAT_INTRINSICS_TEST(sinh, 4)
FLOAT_INTRINSICS_TEST(tanh, 4)
FLOAT_INTRINSICS_TEST(expm1, 4)
FLOAT_INTRINSICS_TEST(lgamma, 4)
FLOAT_INTRINSICS_TEST(erf, 8)
FLOAT_INTRINSICS_TEST(erfc, 8)
FLOAT_INTRINSICS_TEST(acos, 8)
FLOAT_INTRINSICS_TEST(asin, 8)
FLOAT_INTRINSICS_TEST(atan, 8)
FLOAT_INTRINSICS_TEST(cosh, 8)
FLOAT_INTRINSICS_TEST(sinh, 8)
FLOAT_INTRINSICS_TEST(tanh, 8)
FLOAT_INTRINSICS_TEST(expm1, 8)
FLOAT_INTRINSICS_TEST(lgamma, 8)
#undef FLOAT_INTRINSICS_TEST

#define DOUBLE_INTRINSICS_TEST(Name, Lanes)                      \
  void testLLVMVecDouble_##Name##Lane##Lanes##Test() {           \
    KernelScope kernel_scope;                                    \
    Placeholder a(BufHandle("A", {1}, kDouble));                 \
    Placeholder b(BufHandle("B", {1}, kDouble));                 \
    float val = 0.5f;                                            \
    std::vector<double> a_buffer(Lanes, val);                    \
    std::vector<double> b_buffer(Lanes, val);                    \
    auto store = b.storeWithMask(                                \
        {Ramp::make(0, 1, Lanes)},                               \
        Name(a.loadWithMask(                                     \
            {Ramp::make(0, 1, Lanes)},                           \
            Broadcast::make(IntImm::make(1), Lanes))),           \
        Broadcast::make(IntImm::make(1), Lanes));                \
    LLVMCodeGen cg(store, {a, b});                               \
    std::vector<void*> args({a_buffer.data(), b_buffer.data()}); \
    ASSERT_EQ(cg.value<int>(args), 0);                           \
    for (int i = 0; i < Lanes; i++) {                            \
      ASSERT_FLOAT_EQ(a_buffer[i], val);                         \
    }                                                            \
  } // namespace jit
DOUBLE_INTRINSICS_TEST(erf, 2)
DOUBLE_INTRINSICS_TEST(erfc, 2)
DOUBLE_INTRINSICS_TEST(acos, 2)
DOUBLE_INTRINSICS_TEST(asin, 2)
DOUBLE_INTRINSICS_TEST(atan, 2)
DOUBLE_INTRINSICS_TEST(cosh, 2)
DOUBLE_INTRINSICS_TEST(sinh, 2)
DOUBLE_INTRINSICS_TEST(tanh, 2)
DOUBLE_INTRINSICS_TEST(expm1, 2)
DOUBLE_INTRINSICS_TEST(lgamma, 2)
DOUBLE_INTRINSICS_TEST(erf, 4)
DOUBLE_INTRINSICS_TEST(erfc, 4)
DOUBLE_INTRINSICS_TEST(acos, 4)
DOUBLE_INTRINSICS_TEST(asin, 4)
DOUBLE_INTRINSICS_TEST(atan, 4)
DOUBLE_INTRINSICS_TEST(cosh, 4)
DOUBLE_INTRINSICS_TEST(sinh, 4)
DOUBLE_INTRINSICS_TEST(tanh, 4)
DOUBLE_INTRINSICS_TEST(expm1, 4)
DOUBLE_INTRINSICS_TEST(lgamma, 4)
#undef DOUBLE_INTRINSICS_TEST

void testLLVMVectorizerLoadStoreTest() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("A", {1}, kInt));

  Tensor* c =
      Compute("c", {{4, "i"}}, [&](const VarHandle& i) { return a.load(i); });

  Placeholder c_buf(BufHandle(c->buf()));
  LoopNest l({c});
  Stmt* s = l.root_stmt();
  l.vectorize(dynamic_cast<Block*>(s)->front());

  ASSERT_TRUE(dynamic_cast<For*>(dynamic_cast<Block*>(s)->front()) == nullptr);

  LLVMCodeGen cg(s, {a, c_buf});

  std::vector<int> a_vec(4, 21);
  std::vector<int> c_vec(4, 0);
  std::vector<void*> args({a_vec.data(), c_vec.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  assertAllEqual(c_vec, 21);
}

void testLLVMMemcpyTest() {
  KernelScope kernel_scope;
  constexpr int N = 32;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  std::vector<int32_t> a_buffer(N, 42);
  std::vector<int32_t> b_buffer(N, 0);

  VarHandle i("i", kInt);
  auto expr = For::make(i, 0, N, b.store({i}, a.load(i)));

  LLVMCodeGen cg(expr, {a, b});

  std::vector<void*> args({a_buffer.data(), b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  assertAllEqual(a_buffer, 42);
  assertAllEqual(b_buffer, 42);
}

void testLLVMBzeroTest() {
  KernelScope kernel_scope;
  constexpr int N = 32;
  Placeholder b(BufHandle("B", {N}, kInt));
  std::vector<int32_t> b_buffer(N, 11);

  VarHandle i("i", kInt);
  auto expr = For::make(i, 0, N, b.store({i}, 0));

  LLVMCodeGen cg(expr, {b});

  std::vector<void*> args({b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(b_buffer.size(), N);
  assertAllEqual(b_buffer, 0);
}

void testLLVMElemwiseAdd() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<int32_t> a_buffer(N, 41);
  std::vector<int32_t> b_buffer(N, 1);
  std::vector<int32_t> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr = For::make(i, 0, N, c.store({i}, Add::make(a.load(i), b.load(i))));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41);
  assertAllEqual(b_buffer, 1);
  assertAllEqual(c_buffer, 42);
}

void testLLVMElemwiseAddFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kFloat));
  std::vector<float> a_buffer(N, 41);
  std::vector<float> b_buffer(N, 1);
  std::vector<float> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr = For::make(i, 0, N, c.store({i}, a.load(i) + b.load(i)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41.0f);
  assertAllEqual(b_buffer, 1.0f);
  assertAllEqual(c_buffer, 42.0f);
}

void testLLVMElemwiseLog10Float() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  std::vector<float> a_buffer(N, 10.0f);
  std::vector<float> b_buffer(N, 2.0f);

  auto mask = Broadcast::make(IntImm::make(1), 4);
  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N / 4,
      b.storeWithMask(
          {Ramp::make(i * 4, 1, 4)},
          log10(a.loadWithMask({Ramp::make(i * 4, 1, 4)}, mask)),
          mask));

  LLVMCodeGen cg(expr, {a, b});

  std::vector<void*> args({a_buffer.data(), b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  assertAllEqual(a_buffer, 10.0f);
  assertAllEqual(b_buffer, 1.0f);
}

void testLLVMElemwiseLog1pFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  std::vector<float> a_buffer(N, expf(3.0f) - 1);
  std::vector<float> b_buffer(N, 42.0f);

  auto mask = Broadcast::make(IntImm::make(1), 4);
  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N / 4,
      b.storeWithMask(
          {Ramp::make(i * 4, 1, 4)},
          log1p(a.loadWithMask({Ramp::make(i * 4, 1, 4)}, mask)),
          mask));

  LLVMCodeGen cg(expr, {a, b});

  std::vector<void*> args({a_buffer.data(), b_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  assertAllEqual(a_buffer, expf(3.0f) - 1);
  ExpectAllNear(b_buffer, 3.0f, 1e-5f);
}

void testLLVMElemwiseMaxInt() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<int> a_buffer(N, 41);
  std::vector<int> b_buffer(N, 1);
  std::vector<int> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Max::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41);
  assertAllEqual(b_buffer, 1);
  assertAllEqual(c_buffer, 41);
}

void testLLVMElemwiseMinInt() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<int> a_buffer(N, 41);
  std::vector<int> b_buffer(N, 1);
  std::vector<int> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Min::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41);
  assertAllEqual(b_buffer, 1);
  assertAllEqual(c_buffer, 1);
}

void testLLVMElemwiseMaxFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kFloat));
  std::vector<float> a_buffer(N, 41);
  std::vector<float> b_buffer(N, 1);
  std::vector<float> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Max::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41.0f);
  assertAllEqual(b_buffer, 1.0f);
  assertAllEqual(c_buffer, 41.0f);
}

void testLLVMElemwiseMaxNaNFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kFloat));
  std::vector<float> a_buffer(N, NAN);
  std::vector<float> b_buffer(N, 1);
  std::vector<float> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Max::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(b_buffer, 1.0f);
  for (auto const& elt : c_buffer) {
    ASSERT_TRUE(std::isnan(elt));
  }
}

void testLLVMElemwiseMinFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kFloat));
  std::vector<float> a_buffer(N, 41);
  std::vector<float> b_buffer(N, 1);
  std::vector<float> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Min::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41.0f);
  assertAllEqual(b_buffer, 1.0f);
  assertAllEqual(c_buffer, 1.0f);
}

void testLLVMElemwiseMinNaNFloat() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kFloat));
  std::vector<float> a_buffer(N, NAN);
  std::vector<float> b_buffer(N, 1);
  std::vector<float> c_buffer(N, 1);

  VarHandle i("i", kInt);
  auto expr =
      For::make(i, 0, N, c.store({i}, Min::make(a.load(i), b.load(i), false)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(b_buffer, 1.0f);
  for (auto const& elt : c_buffer) {
    ASSERT_TRUE(std::isnan(elt));
  }
}

void testLLVMElemwiseMod() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<int32_t> a_buffer(N, 41);
  std::vector<int32_t> b_buffer(N, 23);
  std::vector<int32_t> c_buffer(N, 18);

  VarHandle i("i", kInt);
  auto expr = For::make(i, 0, N, c.store({i}, Mod::make(a.load(i), b.load(i))));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);
  assertAllEqual(a_buffer, 41);
  assertAllEqual(b_buffer, 23);
  assertAllEqual(c_buffer, 18);
}

void testLLVMCompareSelectIntEQ() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kInt));
  Placeholder b(BufHandle("B", {N}, kInt));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<int> a_buffer(N, 1);
  std::vector<int> b_buffer(N, 1);
  std::vector<int> c_buffer(N, 0);
  std::vector<int> c_ref(N, 1);

  for (int i = 0; i < N / 2; i++) {
    b_buffer[i] = 0;
    c_ref[i] = 0;
  }

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kEQ)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(a_buffer, 1);
  for (int i = 0; i < N; i++) {
    ASSERT_EQ(c_ref[i], c_buffer[i]);
  }
}

void testLLVMCompareSelectFloatEQ() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kFloat));
  Placeholder b(BufHandle("B", {N}, kFloat));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<float> a_buffer(N, 1.0f);
  std::vector<float> b_buffer(N, 1.0f);
  std::vector<int> c_buffer(N, 0);

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kEQ)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(a_buffer, 1.0f);
  assertAllEqual(b_buffer, 1.0f);
  assertAllEqual(c_buffer, 1);
}

void testLLVMCompareSelectByteGT() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kByte));
  Placeholder b(BufHandle("B", {N}, kByte));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<uint8_t> a_buffer(N, 0);
  std::vector<uint8_t> b_buffer(N, 0);
  std::vector<int> c_buffer(N, 0);
  std::vector<int> c_ref(N, 0);

  for (int i = 0; i < N / 2; i++) {
    a_buffer[i] = 128;
    c_ref[i] = 1;
  }

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kGT)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(b_buffer, uint8_t(0));
  for (int i = 0; i < N; i++) {
    ASSERT_EQ(c_ref[i], c_buffer[i]);
  }
}

void testLLVMCompareSelectByteGE() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kByte));
  Placeholder b(BufHandle("B", {N}, kByte));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<uint8_t> a_buffer(N, 0);
  std::vector<uint8_t> b_buffer(N, 0);
  std::vector<int> c_buffer(N, 0);
  std::vector<int> c_ref(N, 1);

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kGE)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(b_buffer, uint8_t(0));
  for (int i = 0; i < N; i++) {
    ASSERT_EQ(c_ref[i], c_buffer[i]);
  }
}

void testLLVMCompareSelectByteLT() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kByte));
  Placeholder b(BufHandle("B", {N}, kByte));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<uint8_t> a_buffer(N, 0);
  std::vector<uint8_t> b_buffer(N, 128);
  std::vector<int> c_buffer(N, 0);
  std::vector<int> c_ref(N, 1);

  for (int i = 0; i < N / 2; i++) {
    a_buffer[i] = 128;
    c_ref[i] = 0;
  }

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kLT)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(b_buffer, uint8_t(128));
  for (int i = 0; i < N; i++) {
    ASSERT_EQ(c_ref[i], c_buffer[i]);
  }
}

void testLLVMCompareSelectByteLE() {
  KernelScope kernel_scope;
  constexpr int N = 1024;
  Placeholder a(BufHandle("A", {N}, kByte));
  Placeholder b(BufHandle("B", {N}, kByte));
  Placeholder c(BufHandle("C", {N}, kInt));
  std::vector<uint8_t> a_buffer(N, 0);
  std::vector<uint8_t> b_buffer(N, 128);
  std::vector<int> c_buffer(N, 0);
  std::vector<int> c_ref(N, 1);

  VarHandle i("i", kInt);
  auto expr = For::make(
      i,
      0,
      N,
      c.store(
          {i},
          CompareSelect::make(
              a.load(i), b.load(i), CompareSelectOperation::kLE)));

  LLVMCodeGen cg(expr, {a, b, c});

  std::vector<void*> args({a_buffer.data(), b_buffer.data(), c_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  ASSERT_EQ(a_buffer.size(), N);
  ASSERT_EQ(b_buffer.size(), N);
  ASSERT_EQ(c_buffer.size(), N);

  assertAllEqual(b_buffer, uint8_t(128));
  for (int i = 0; i < N; i++) {
    ASSERT_EQ(c_ref[i], c_buffer[i]);
  }
}

void testLLVMStoreFloat() {
  KernelScope kernel_scope;
  Placeholder result(BufHandle("result", {1}, kFloat));
  std::vector<float> result_buffer = {0.0f};
  auto expr = result.store({0}, FloatImm::make(3.14f));
  LLVMCodeGen cg(expr, {result});
  std::vector<void*> args({result_buffer.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  ASSERT_EQ(result_buffer[0], 3.14f);
}

void testLLVMSimpleMath01() {
  KernelScope kernel_scope;
  const int N = 1024;
  Tensor* tensor = Compute("f", {{N, "i"}}, [](const VarHandle& i) {
    return cast<float>(i * i + 1);
  });
  LoopNest l({tensor});
  Stmt* stmt = l.root_stmt();
  Placeholder f_buf(BufHandle(tensor->buf()));
  LLVMCodeGen cg(stmt, {f_buf});

  PaddedBuffer<float> f_v(N, "f_v");
  std::vector<void*> args({f_v.data()});
  int value = cg.value<int>(args);
  ASSERT_EQ(value, 0);
  PaddedBuffer<float> f_ref(N, "f_ref");
  for (int i = 0; i < N; i++) {
    f_ref(i) = i * i + 1;
  }
  ExpectAllNear(f_v, f_ref, 1e-5);
}

void testLLVMComputeMul() {
  KernelScope kernel_scope;
  const int N = 1024;
  Placeholder a(BufHandle("a", {N}, kFloat));
  Placeholder b(BufHandle("b", {N}, kFloat));
  Tensor* c = Compute("c", {{N, "i"}}, [&](const VarHandle& i) {
    return a.load(i) * b.load(i);
  });

  Placeholder c_buf(BufHandle(c->buf()));
  LoopNest l({c});
  Stmt* s = l.root_stmt();

  LLVMCodeGen cg(s, {a, b, c_buf});

  std::vector<float> a_vec(N, 21.0f);
  std::vector<float> b_vec(N, 2.0f);
  std::vector<float> c_vec(N, 0.0f);
  std::vector<void*> args({a_vec.data(), b_vec.data(), c_vec.data()});
  ASSERT_EQ(cg.value<int>(args), 0);
  assertAllEqual(c_vec, 42.0f);
}

void testLLVMBroadcastAdd() {
  KernelScope kernel_scope;
  const int M = 32;
  const int N = 1024;
  Placeholder a(BufHandle("a", {M, N}, kFloat));
  Placeholder b(BufHandle("b", {N}, kFloat));
  Tensor* c = Compute(
      "c", {{M, "i"}, {N, "j"}}, [&](const VarHandle& i, const VarHandle& j) {
        return a.load(i, j) + b.load(j);
      });

  Placeholder c_buf(BufHandle(c->buf()));
  LoopNest l({c});
  l.prepareForCodegen();
  Stmt* s = l.root_stmt();

  LLVMCodeGen cg(s, {a, b, c_buf});

  std::vector<float> av(M * N);
  std::iota(av.begin(), av.end(), 0);
  std::vector<float> bv(N);
  std::iota(bv.begin(), bv.end(), 0);
  std::vector<float> cv(M * N, 0);
  std::vector<void*> args({av.data(), bv.data(), cv.data()});
  ASSERT_EQ(cg.value<int>(args), 0);

  for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
      ASSERT_EQ(cv[i * N + j], av[i * N + j] + bv[j]);
    }
  }
}

void testLLVMBitwiseOps() {
  KernelScope kernel_scope;
  auto a = IntImm::make(59);
  auto b = IntImm::make(11);
  auto c = IntImm::make(101);
  auto d = IntImm::make(2);

  ExprHandle f = (((a ^ (b << 1)) & c) >> 2) | d;
  LLVMExprEval cg(f);

  ASSERT_EQ(cg.value<int>(), 11);
}

void testLLVMDynamicShapeAdd() {
  KernelScope kernel_scope;
  auto testWithSize = [](int32_t size) {
    VarHandle n("n", kInt);
    Placeholder a(BufHandle("a", {n}, kFloat));
    Placeholder b(BufHandle("b", {n}, kFloat));
    Placeholder c(BufHandle("c", {n}, kFloat));
    VarHandle i("i", kInt);
    Stmt* s = For::make(i, 0, n, c.store({i}, a.load(i) + b.load(i)));
    std::vector<float> aData(size, 1.0f);
    std::vector<float> bData(size, 2.0f);
    std::vector<float> cData(size, 0.0f);
    LLVMCodeGen cg(s, {a, b, c, n});
    std::vector<void*> args({aData.data(), bData.data(), cData.data(), &size});
    cg.value<float>(args);
    ExpectAllNear(cData, std::vector<float>(size, 3.0f), 1e-7);
  };
  testWithSize(1);
  testWithSize(16);
  testWithSize(37);
}

void testLLVMBindDynamicShapeAdd() {
  KernelScope kernel_scope;
  auto testWithSize = [](int32_t size) {
    VarHandle n("n", kInt);
    Placeholder a(BufHandle("a", {n}, kFloat));
    Placeholder b(BufHandle("b", {n}, kFloat));
    Placeholder c(BufHandle("c", {n}, kFloat));
    VarHandle i("i", kInt);
    Stmt* s = For::make(i, 0, n, c.store({i}, a.load(i) + b.load(i)));
    std::vector<float> aData(size, 1.0f);
    std::vector<float> bData(size, 2.0f);
    std::vector<float> cData(size, 0.0f);
    LLVMCodeGen cg(s, {a, b, c, n});
    cg.call({aData, bData, cData, size});
    ExpectAllNear(cData, std::vector<float>(size, 3.0f), 1e-7);
  };
  testWithSize(1);
  testWithSize(16);
  testWithSize(37);
}

void testLLVMTensorDynamicShapeAdd() {
  KernelScope kernel_scope;
  auto testWithSize = [](int32_t size) {
    VarHandle n("n", kInt);
    Placeholder a(BufHandle("a", {n}, kFloat));
    Placeholder b(BufHandle("b", {n}, kFloat));
    Tensor* c = Compute("c", {{n, "n"}}, [&](const VarHandle& i) {
      return a.load(i) + b.load(i);
    });
    LoopNest l({c});
    Stmt* s = l.root_stmt();
    LLVMCodeGen cg(s, {a, b, c, n});
    std::vector<float> aData(size, 1.0f);
    std::vector<float> bData(size, 2.0f);
    std::vector<float> cData(size, 0.0f);
    cg.call({aData, bData, cData, size});
    ExpectAllNear(cData, std::vector<float>(size, 3.0f), 1e-7);
  };
  testWithSize(1);
  testWithSize(16);
  testWithSize(37);
}

void testLLVMDynamicShape2D() {
  KernelScope kernel_scope;
  auto testWithSize = [](int32_t M, int32_t N) {
    VarHandle m("m", kInt);
    VarHandle n("n", kInt);
    Placeholder a(BufHandle("a", {m, n}, kFloat));
    Placeholder b(BufHandle("b", {m, n}, kFloat));
    Tensor* c = Compute(
        "c", {{m, "m"}, {n, "n"}}, [&](const VarHandle& i, const VarHandle& j) {
          return a.load(i, j) + b.load(i, j);
        });
    LoopNest l({c});
    l.prepareForCodegen();
    Stmt* s = l.root_stmt();
    LLVMCodeGen cg(s, {a, b, c, m, n});
    std::vector<float> aData(M * N, 1.0f);
    std::vector<float> bData(M * N, 2.0f);
    std::vector<float> cData(M * N, 0.0f);
    cg.call({aData, bData, cData, M, N});
    ExpectAllNear(cData, std::vector<float>(M * N, 3.0f), 1e-7);
  };
  testWithSize(1, 8);
  testWithSize(16, 32);
  testWithSize(37, 11);
}

void testLLVMEmptyStmt() {
  KernelScope kernel_scope;
  Stmt* s = new Block({});

  LLVMCodeGen cg(s, {});
  cg.call({});
  // Just don't crash.
}

void testLLVMEliminatedStmt() {
  KernelScope kernel_scope;
  Placeholder a(BufHandle("a", {1}, kFloat));

  Tensor* c = Compute("c", {{0, "m"}}, [&](const VarHandle& m) { return m; });

  LoopNest l({c});
  l.prepareForCodegen();
  Stmt* s = l.root_stmt();
  s = IRSimplifier::simplify(s);
  LLVMCodeGen cg(s, {a, c});
  std::vector<float> aData(1, 1.0f);
  std::vector<float> cData(0, 0.0f);
  cg.call({aData, cData});
}

void testLLVMSimpleReduction() {
  KernelScope kernel_scope;

  int M = 128;
  int N = 64;
  const int kTotalSize = M * N;

  Placeholder a("a", kFloat, {1, M, N});

  // TODO: why doesn't implicit vector<DimArg> work?
  std::vector<DimArg> axis = {DimArg(1)};
  std::vector<DimArg> reduce_axis = {DimArg(M), DimArg(N)};
  Tensor* b = Reduce("sum", axis, Sum(), a, reduce_axis);
  LoopNest loop({b});

  loop.prepareForCodegen();
  Stmt* s = loop.root_stmt();
  s = IRSimplifier::simplify(s);

  LLVMCodeGen cg(s, {a, b});

  PaddedBuffer<float> a_v(1, M, N, "a_v");
  PaddedBuffer<float> b_v(1, "b_v");
  PaddedBuffer<float> b_ref(1, "b_ref");

  b_ref(0) = 0;
  for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
      int v = i + j;
      a_v(0, i, j) = v;
      b_ref(0) += v;
    }
  }

  cg.call({a_v, b_v});

  ExpectAllNear(b_v, b_ref, 1e-5);
}

void testLLVMRFactorReduction() {
  KernelScope kernel_scope;

  int M = 128;
  int N = 64;
  const int kTotalSize = M * N;

  Placeholder a("a", kFloat, {1, M, N});

  // TODO: why doesn't implicit vector<DimArg> work?
  std::vector<DimArg> axis = {DimArg(1)};
  std::vector<DimArg> reduce_axis = {DimArg(M), DimArg(N)};
  Tensor* b = Reduce("sum", axis, Sum(), a, reduce_axis);
  LoopNest loop({b});

  std::vector<For*> loops = loop.getLoopStmtsFor(b);
  For* loop_m = loops.at(1);
  For* loop_n = loops.at(2);
  loop.reorderAxis(loop_m, loop_n);

  loops = loop.getLoopStmtsFor(b);
  loop_m = loops.at(2);
  loop_n = loops.at(1);
  loop.rfactor(b->body(), loop_n->var(), loop_n->body());

  loop.prepareForCodegen();
  Stmt* s = loop.root_stmt();
  s = IRSimplifier::simplify(s);

  LLVMCodeGen cg(s, {a, b});

  PaddedBuffer<float> a_v(1, M, N, "a_v");
  PaddedBuffer<float> b_v(1, "b_v");
  PaddedBuffer<float> b_ref(1, "b_ref");

  b_ref(0) = 0;
  for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
      int v = i + j;
      a_v(0, i, j) = v;
      b_ref(0) += v;
    }
  }

  cg.call({a_v, b_v});

  ExpectAllNear(b_v, b_ref, 1e-5);
}

// TODO: disabled since this doesn't work.
void DISABLED_testLLVMRFactorVectorizedReduction() {
  KernelScope kernel_scope;

  int M = 128;
  int N = 64;
  const int kTotalSize = M * N;

  Placeholder a("a", kFloat, {1, M, N});

  // TODO: why doesn't implicit vector<DimArg> work?
  std::vector<DimArg> axis = {DimArg(1)};
  std::vector<DimArg> reduce_axis = {DimArg(M), DimArg(N)};
  Tensor* b = Reduce("sum", axis, Sum(), a, reduce_axis);
  LoopNest loopnest({b});
  std::vector<For*> loops = loopnest.getLoopStmtsFor(b);
  For* loop_k = loops.at(0);
  For* loop_m = loops.at(1);
  For* loop_n = loops.at(2);
  loopnest.reorderAxis(loop_n, loop_m);
  loops = loopnest.getLoopStmtsFor(b);
  loop_k = loops.at(0);
  loop_n = loops.at(1);
  loop_m = loops.at(2);
  // Case-III reductions
  loopnest.rfactor(b->body(), loop_n->var());
  loopnest.prepareForCodegen();
  Stmt* s = loopnest.root_stmt();
  s = IRSimplifier::simplify(s);

  Block* root_block = dynamic_cast<Block*>(s);
  auto I = root_block->begin();
  ++I;

  For* outer_loop = dynamic_cast<For*>(*I);
  loopnest.vectorize(outer_loop);

  s = IRSimplifier::simplify(s);
  LLVMCodeGen cg(s, {a, b});

  PaddedBuffer<float> a_v(1, M, N, "a_v");
  PaddedBuffer<float> b_v(1, "b_v");
  PaddedBuffer<float> b_ref(1, "b_ref");

  b_ref(0) = 0;
  for (int i = 0; i < M; i++) {
    for (int j = 0; j < N; j++) {
      int v = i + j;
      a_v(0, i, j) = v;
      b_ref(0) += v;
    }
  }

  cg.call({a_v, b_v});

  ExpectAllNear(b_v, b_ref, 1e-5);
}

} // namespace jit
} // namespace torch

#endif // TORCH_ENABLE_LLVM

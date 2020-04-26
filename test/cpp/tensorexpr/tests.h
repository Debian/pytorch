#pragma once

/**
 * See README.md for instructions on how to add a new test.
 */
#include <c10/macros/Export.h>
#include <torch/csrc/WindowsTorchApiMacro.h>

namespace torch {
namespace jit {

#define TH_FORALL_TESTS(_)       \
  _(ExprBasicValueTest)          \
  _(ExprBasicValueTest02)        \
  _(ExprLetTest01)               \
  _(ExprLetStmtTest01)           \
  _(ExprLetTest02)               \
  _(ExprIntTest)                 \
  _(ExprFloatTest)               \
  _(ExprByteTest)                \
  _(ExprCharTest)                \
  _(ExprShortTest)               \
  _(ExprLongTest)                \
  _(ExprHalfTest)                \
  _(ExprDoubleTest)              \
  _(ExprVectorAdd01)             \
  _(ExprCompareSelectEQ)         \
  _(ExprSubstitute01)            \
  _(ExprMath01)                  \
  _(ExprUnaryMath01)             \
  _(ExprBinaryMath01)            \
  _(ExprDynamicShapeAdd)         \
  _(ExprBitwiseOps)              \
  _(IRPrinterBasicValueTest)     \
  _(IRPrinterBasicValueTest02)   \
  _(IRPrinterLetTest01)          \
  _(IRPrinterLetTest02)          \
  _(IRPrinterCastTest)           \
  _(ExprSimple01)                \
  _(ExprLower01)                 \
  _(ExprSimple02)                \
  _(ExprSplitWithTailNone)       \
  _(ExprSplitWithMask01)         \
  _(ScheduleBroadcastAddBuffer)  \
  _(ScheduleFunctionCall01)      \
  _(ScheduleInlineFunc01)        \
  _(ScheduleFuserStyle)          \
  _(ScheduleFuserThreeArg)       \
  _(ScheduleDynamicShape2D)      \
  _(TypeTest01)                  \
  _(TypePropagation)             \
  _(Cond01)                      \
  _(IfThenElse01)                \
  _(IfThenElse02)                \
  _(ATen_cast_Float)             \
  _(ATennegInt)                  \
  _(ATennegFloat)                \
  _(ATenaddInt)                  \
  _(ATenaddFloat)                \
  _(ATensubInt)                  \
  _(ATensubFloat)                \
  _(ATenlerp)                    \
  _(ATenaddcmulInt)              \
  _(ATenaddcmulFloat)            \
  _(ATenmulInt)                  \
  _(ATenmulFloat)                \
  _(ATendivInt)                  \
  _(ATendivFloat)                \
  _(ATenmaxInt)                  \
  _(ATenmaxFloat)                \
  _(ATenminInt)                  \
  _(ATenminFloat)                \
  _(ATen_sigmoid_backward)       \
  _(ATen_tanh_backward)          \
  _(ATenreciprocal)              \
  _(ATenreluInt)                 \
  _(ATenreluFloat)               \
  _(ATenlogFloat)                \
  _(ATenlog10Float)              \
  _(ATenlog2Float)               \
  _(ATenexpFloat)                \
  _(ATenerfFloat)                \
  _(ATencosFloat)                \
  _(ATeneqInt)                   \
  _(ATengeInt)                   \
  _(ATengtInt)                   \
  _(ATenleInt)                   \
  _(ATenltInt)                   \
  _(ConstantFoldSimple)          \
  _(ConstantFoldTwoLayer)        \
  _(ConstantFoldShifts)          \
  _(ConstantFoldBitwise)         \
  _(ConstantFoldMultiOp)         \
  _(ConstantFoldMinMax)          \
  _(ConstantFoldIntrinsics)      \
  _(ConstantFoldWithVar)         \
  _(UnFoldableExpr)              \
  _(HashSimple)                  \
  _(HashEquivalence)             \
  _(HashEquivalenceAfterFolding) \
  _(HashDifferenceTypes)         \
  _(HashLargeExpression)         \
  _(SimplifyAdd)                 \
  _(SimplifySub)                 \
  _(SimplifyMultiLayer)          \
  _(SimplifyMultiTerm)           \
  _(SimplifyCasts)               \
  _(SimplifyEliminatesNoOps)     \
  _(SimplifyMultiVar)            \
  _(SimplifyEliminatesVar)       \
  _(StmtClone)

#define TH_FORALL_TESTS_LLVM(_)    \
  _(LLVMByteImmTest)               \
  _(LLVMCharImmTest)               \
  _(LLVMShortImmTest)              \
  _(LLVMIntImmTest)                \
  _(LLVMLongImmTest)               \
  _(LLVMFloatImmTest)              \
  _(LLVMDoubleImmTest)             \
  _(LLVMHalfImmTest)               \
  _(LLVMByteAddTest)               \
  _(LLVMCharAddTest)               \
  _(LLVMShortAddTest)              \
  _(LLVMIntAddTest)                \
  _(LLVMLongAddTest)               \
  _(LLVMFloatAddTest)              \
  _(LLVMDoubleAddTest)             \
  _(LLVMHalfAddTest)               \
  _(LLVMByteSubTest)               \
  _(LLVMCharSubTest)               \
  _(LLVMShortSubTest)              \
  _(LLVMIntSubTest)                \
  _(LLVMLongSubTest)               \
  _(LLVMFloatSubTest)              \
  _(LLVMDoubleSubTest)             \
  _(LLVMHalfSubTest)               \
  _(LLVMByteMulTest)               \
  _(LLVMCharMulTest)               \
  _(LLVMShortMulTest)              \
  _(LLVMIntMulTest)                \
  _(LLVMLongMulTest)               \
  _(LLVMFloatMulTest)              \
  _(LLVMDoubleMulTest)             \
  _(LLVMHalfMulTest)               \
  _(LLVMByteDivTest)               \
  _(LLVMCharDivTest)               \
  _(LLVMShortDivTest)              \
  _(LLVMIntDivTest)                \
  _(LLVMLongDivTest)               \
  _(LLVMFloatDivTest)              \
  _(LLVMDoubleDivTest)             \
  _(LLVMHalfDivTest)               \
  _(LLVMIntToFloatCastTest)        \
  _(LLVMFloatToIntCastTest)        \
  _(LLVMIntToLongCastTest)         \
  _(LLVMByteToCharCastTest)        \
  _(LLVMHalfToLongCastTest)        \
  _(LLVMByteToDoubleCastTest)      \
  _(LLVMLetTest01)                 \
  _(LLVMLetTest02)                 \
  _(LLVMLetTestMultitype)          \
  _(LLVMBufferTest)                \
  _(LLVMBlockTest)                 \
  _(LLVMLoadStoreTest)             \
  _(LLVMVecLoadStoreTest)          \
  _(LLVMVecFloat_acosLane4Test)    \
  _(LLVMVecFloat_asinLane4Test)    \
  _(LLVMVecFloat_atanLane4Test)    \
  _(LLVMVecFloat_coshLane4Test)    \
  _(LLVMVecFloat_sinhLane4Test)    \
  _(LLVMVecFloat_tanhLane4Test)    \
  _(LLVMVecFloat_erfLane4Test)     \
  _(LLVMVecFloat_erfcLane4Test)    \
  _(LLVMVecFloat_expm1Lane4Test)   \
  _(LLVMVecFloat_lgammaLane4Test)  \
  _(LLVMVecFloat_acosLane8Test)    \
  _(LLVMVecFloat_asinLane8Test)    \
  _(LLVMVecFloat_atanLane8Test)    \
  _(LLVMVecFloat_coshLane8Test)    \
  _(LLVMVecFloat_sinhLane8Test)    \
  _(LLVMVecFloat_tanhLane8Test)    \
  _(LLVMVecFloat_erfLane8Test)     \
  _(LLVMVecFloat_erfcLane8Test)    \
  _(LLVMVecFloat_expm1Lane8Test)   \
  _(LLVMVecFloat_lgammaLane8Test)  \
  _(LLVMVecDouble_acosLane2Test)   \
  _(LLVMVecDouble_asinLane2Test)   \
  _(LLVMVecDouble_atanLane2Test)   \
  _(LLVMVecDouble_coshLane2Test)   \
  _(LLVMVecDouble_sinhLane2Test)   \
  _(LLVMVecDouble_tanhLane2Test)   \
  _(LLVMVecDouble_erfLane2Test)    \
  _(LLVMVecDouble_erfcLane2Test)   \
  _(LLVMVecDouble_expm1Lane2Test)  \
  _(LLVMVecDouble_lgammaLane2Test) \
  _(LLVMVecDouble_acosLane4Test)   \
  _(LLVMVecDouble_asinLane4Test)   \
  _(LLVMVecDouble_atanLane4Test)   \
  _(LLVMVecDouble_coshLane4Test)   \
  _(LLVMVecDouble_sinhLane4Test)   \
  _(LLVMVecDouble_tanhLane4Test)   \
  _(LLVMVecDouble_erfLane4Test)    \
  _(LLVMVecDouble_erfcLane4Test)   \
  _(LLVMVecDouble_expm1Lane4Test)  \
  _(LLVMVecDouble_lgammaLane4Test) \
  _(LLVMMemcpyTest)                \
  _(LLVMBzeroTest)                 \
  _(LLVMElemwiseAdd)               \
  _(LLVMElemwiseAddFloat)          \
  _(LLVMElemwiseLog10Float)        \
  _(LLVMElemwiseMaxInt)            \
  _(LLVMElemwiseMinInt)            \
  _(LLVMElemwiseMaxNumFloat)       \
  _(LLVMElemwiseMaxNumNaNFloat)    \
  _(LLVMElemwiseMinNumFloat)       \
  _(LLVMElemwiseMinNumNaNFloat)    \
  _(LLVMCompareSelectIntEQ)        \
  _(LLVMCompareSelectFloatEQ)      \
  _(LLVMStoreFloat)                \
  _(LLVMSimpleMath01)              \
  _(LLVMComputeMul)                \
  _(LLVMBroadcastAdd)              \
  _(LLVMBitwiseOps)                \
  _(LLVMDynamicShapeAdd)           \
  _(LLVMBindDynamicShapeAdd)       \
  _(LLVMTensorDynamicShapeAdd)     \
  _(LLVMDynamicShape2D)            \
  _(LLVMIfThenElseTest)            \
  _(LLVMVectorizerLoadStoreTest)

#define TH_FORALL_TESTS_CUDA(_) \
  _(CudaTestVectorAdd01)        \
  _(CudaTestVectorAdd02)        \
  _(CudaDynamicShape2D)         \
  _(CudaTestRand01)             \
  _(CudaDynamicShapeSplit)

#define DECLARE_TENSOREXPR_TEST(name) void test##name();
TH_FORALL_TESTS(DECLARE_TENSOREXPR_TEST)
#ifdef TORCH_ENABLE_LLVM
TH_FORALL_TESTS_LLVM(DECLARE_TENSOREXPR_TEST)
#endif
#ifdef USE_CUDA
TH_FORALL_TESTS_CUDA(DECLARE_TENSOREXPR_TEST)
#endif
#undef DECLARE_TENSOREXPR_TEST

} // namespace jit
} // namespace torch

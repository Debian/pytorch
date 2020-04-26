#pragma once

/**
 * See README.md for instructions on how to add a new test.
 */
#include <torch/csrc/WindowsTorchApiMacro.h>
#include <c10/macros/Export.h>

namespace torch {
namespace jit {
#define TH_FORALL_TESTS(_)             \
  _(ADFormulas)                        \
  _(Attributes)                        \
  _(Blocks)                            \
  _(CallStack)                         \
  _(CallStackCaching)                  \
  _(CodeTemplate)                      \
  _(ControlFlow)                       \
  _(CreateAutodiffSubgraphs)           \
  _(CustomOperators)                   \
  _(CustomOperatorAliasing)            \
  _(IValueKWargs)                      \
  _(CustomFusion)                      \
  _(SchemaMatching)                    \
  _(Differentiate)                     \
  _(DifferentiateWithRequiresGrad)     \
  _(FromQualString)                    \
  _(InternedStrings)                   \
  _(IValue)                            \
  _(IValueFuture)                      \
  _(PassManagement)                    \
  _(Proto)                             \
  _(RegisterFusionCachesKernel)        \
  _(SchemaParser)                      \
  _(TopologicalIndex)                  \
  _(TopologicalMove)                   \
  _(SubgraphUtils)                     \
  _(AliasAnalysis)                     \
  _(ContainerAliasing)                 \
  _(AliasRegistration)                 \
  _(WriteTracking)                     \
  _(Wildcards)                         \
  _(MemoryDAG)                         \
  _(IRParser)                          \
  _(ConstantPooling)                   \
  _(THNNConv)                          \
  _(ATenNativeBatchNorm)               \
  _(NoneSchemaMatch)                   \
  _(ClassParser)                       \
  _(UnifyTypes)                        \
  _(Profiler)                          \
  _(InsertAndEliminateRedundantGuards) \
  _(InsertBailOuts)                    \
  _(PeepholeOptimize)                  \
  _(RecordFunction)                    \
  _(ThreadLocalDebugInfo)              \
  _(SubgraphMatching)                  \
  _(SubgraphRewriter)                  \
  _(ModuleClone)                       \
  _(ModuleCloneInstance)               \
  _(ModuleConstant)                    \
  _(ModuleParameter)                   \
  _(ModuleDefine)                      \
  _(QualifiedName)                     \
  _(ClassImport)                       \
  _(ProfiledTensorTypeHashing)         \
  _(ScriptObject)                      \
  _(SaveExtraFilesHook)                \
  _(TypeTags)                          \
  _(DCE)                               \
  _(CustomFusionNestedBlocks)          \
  _(ClassDerive)                       \
  _(SaveLoadTorchbind)                 \
  _(ModuleInterfaceSerialization)      \
  _(ClassTypeAddRemoveAttr)            \
  _(Inliner)                           \
  _(LiteInterpreterAdd)                \
  _(LiteInterpreterConv)               \
  _(LiteInterpreterInline)             \
  _(LiteInterpreterTuple)              \
  _(LiteInterpreterUpsampleNearest2d)  \
  _(CommonAncestor)                    \
  _(AutogradSymbols)                   \
  _(MobileTypeParser)                  \
  _(LiteInterpreterBuiltinFunction)    \
  _(LiteInterpreterPrim)               \
  _(LiteInterpreterLoadOrigJit)        \
  _(LiteInterpreterWrongMethodName)    \
  _(LiteInterpreterParams)             \
  _(LiteInterpreterSetState)           \
  _(TorchbindIValueAPI)

#define TH_FORALL_TESTS_CUDA(_) \
  _(ArgumentSpec)               \
  _(CompleteArgumentSpec)       \
  _(Fusion)                     \
  _(GraphExecutor)              \
  _(ModuleConversion)           \
  _(Interp)

#define DECLARE_JIT_TEST(name) void test##name();
TH_FORALL_TESTS(DECLARE_JIT_TEST)
TH_FORALL_TESTS_CUDA(DECLARE_JIT_TEST)
#undef DECLARE_JIT_TEST

// This test is special since it requires prior setup in python.
// So it is not part of the general test list (which is shared between the gtest
// and python test runners), but is instead invoked manually by the
// torch_python_test.cpp
void testEvalModeForLoadedModule();
void testSerializationInterop();
void testTorchSaveError();

} // namespace jit
} // namespace torch

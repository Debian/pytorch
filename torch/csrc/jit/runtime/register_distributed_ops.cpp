#include <ATen/ATen.h>
#include <ATen/core/op_registration/op_registration.h>
#include <torch/csrc/distributed/autograd/autograd.h>
#include <torch/csrc/distributed/autograd/context/container.h>
#include <torch/csrc/distributed/autograd/engine/dist_engine.h>
#include <torch/csrc/distributed/rpc/rpc_agent.h>
#include <torch/csrc/distributed/rpc/rref_impl.h>
#include <torch/csrc/distributed/rpc/torchscript_functions.h>
#include <torch/csrc/jit/python/pybind_utils.h>
#include <torch/csrc/jit/runtime/register_ops_utils.h>
#include <torch/library.h>

#include <fmt/format.h>

using at::Scalar;
using at::Tensor;
namespace dist_autograd = torch::distributed::autograd;
namespace dist_rpc = torch::distributed::rpc;

namespace torch {
namespace jit {

namespace {

static auto workerInfo =
    torch::class_<dist_rpc::WorkerInfo>("dist_rpc", "WorkerInfo")
        .def(torch::init<std::string, int64_t>());

RegisterOperators reg_rpc_ops(
    {Operator(
         fmt::format(
             "aten::to_here(RRef(t) self, float timeout = {}) -> t(*)",
             torch::distributed::rpc::kDefaultRpcTimeoutSeconds),
         [](Stack& stack) {
           auto timeout = pop(stack).toDouble();
           auto rref = pop(stack).toRRef();
           IValue res;
           if (rref->isOwner()) {
             res =
                 c10::dynamic_intrusive_pointer_cast<dist_rpc::OwnerRRef>(rref)
                     ->getValue();
           } else {
             res = c10::dynamic_intrusive_pointer_cast<dist_rpc::UserRRef>(rref)
                       ->toHere(timeout);
           }
           push(stack, std::move(res));
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::local_value(RRef(t) self) -> t(*)",
         [](Stack& stack) {
           auto rref = pop(stack).toRRef();
           TORCH_CHECK(
               rref->isOwner(),
               "Can't call RRef.local_value() on a non-owner RRef.");
           IValue res =
               c10::static_intrusive_pointer_cast<dist_rpc::OwnerRRef>(rref)
                   ->getValue();
           push(stack, std::move(res));
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::is_owner(RRef(t) self) -> bool",
         [](Stack& stack) {
           auto rref = pop(stack).toRRef();
           push(stack, rref->isOwner());
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::owner(RRef(t) self) -> __torch__.torch.classes.dist_rpc.WorkerInfo",
         [](Stack& stack) {
           auto rref = pop(stack).toRRef();
           push(
               stack,
               torch::make_custom_class<distributed::rpc::WorkerInfo>(
                   rref->ownerName(), rref->owner()));
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::owner_name(RRef(t) self) -> str",
         [](Stack& stack) {
           auto rref = pop(stack).toRRef();
           push(stack, rref->ownerName());
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::confirmed_by_owner(RRef(t) self) -> bool",
         [](Stack& stack) {
           auto rref = pop(stack).toRRef();
           push(stack, rref->confirmedByOwner());
           return 0;
         },
         aliasAnalysisFromSchema()),
     Operator(
         "aten::dist_backward(int context_id, Tensor[] roots, bool retain_graph=False) -> ()",
         [](Stack& stack) {
           bool retain_graph = pop(stack).toBool();
           auto roots_list = pop(stack).toTensorList();
           int64_t context_id = pop(stack).toInt();
           torch::autograd::variable_list roots(
               roots_list.begin(), roots_list.end());
           dist_autograd::backward(context_id, roots, retain_graph);
           return 0;
         },
         aliasAnalysisConservative()),
     Operator(
         prim::rpc_async,
         [](const Node* node) -> Operation {
           int num_inputs = node->inputs().size();
           return [num_inputs](Stack& stack) {
             // Get inputs from the stack.
             auto stackIter = stack.end() - num_inputs;
             auto& dstWorkerIValue = *stackIter++;
             auto& qualifiedNameIValue = *stackIter++;
             IValue emptyTuple(c10::ivalue::Tuple::create({}));
             IValue emptyDict{
                 c10::impl::GenericDict(AnyType::get(), AnyType::get())};
             // Equivalent to Python statement
             // `args = args if args is not None else ()`.
             auto& argsTupleIValue =
                 num_inputs >= 3 ? *stackIter++ : emptyTuple;
             // `kwargs = kwargs if kwargs is not None else {}`.
             auto& kwargsDictIValue =
                 num_inputs >= 4 ? *stackIter++ : emptyDict;

             // IValue corresponding to placeholder for RPC timeout. Used if no
             // rpc timeout is specified by user.
             IValue noTimeout(torch::distributed::rpc::kUnsetRpcTimeout);
             const auto rpcMaxInputs = 5;
             auto& timeoutIValue =
                 num_inputs >= rpcMaxInputs ? *stackIter++ : noTimeout;
             TORCH_INTERNAL_ASSERT(
                 dstWorkerIValue.isString() ||
                 c10::getCustomClassType<
                     c10::intrusive_ptr<dist_rpc::WorkerInfo>>() ==
                     dstWorkerIValue.type());
             TORCH_INTERNAL_ASSERT(qualifiedNameIValue.isString());
             TORCH_INTERNAL_ASSERT(argsTupleIValue.isTuple());
             TORCH_INTERNAL_ASSERT(kwargsDictIValue.isGenericDict());
             TORCH_INTERNAL_ASSERT(timeoutIValue.isDouble());

             // Get FunctionSchema for qualifiedName.
             auto qualifiedName =
                 c10::QualifiedName(qualifiedNameIValue.toStringRef());
             std::shared_ptr<CompilationUnit> cuPtr;
             {
               py::gil_scoped_acquire acquire;
               cuPtr = get_python_cu();
             }
             auto& functionSchema =
                 cuPtr->get_function(qualifiedName).getSchema();

             // Build Stack for the user callable.
             // It's similar to
             // Stack createStackForSchema(FunctionSchema, py::args,
             // py::kwargs). Instead, it's Stack
             // createStackForSchema(FunctionSchema, IValue<Tuple>,
             // IValue<Dict>).
             Stack userCallableStack;
             userCallableStack.reserve(functionSchema.arguments().size());

             // Move args from Tuple IValue to Stack.
             for (auto& elem : argsTupleIValue.toTuple()->elements()) {
               push(userCallableStack, std::move(elem));
             }

             // Move kwargs from Dict IValue to Stack.
             size_t consumed_kwargs = 0;
             auto kwargsDict = kwargsDictIValue.toGenericDict();
             for (size_t i = userCallableStack.size();
                  i < functionSchema.arguments().size();
                  ++i) {
               const auto& arg = functionSchema.arguments()[i];
               const auto& argName = arg.name();
               if (kwargsDict.contains(argName)) {
                 push(userCallableStack, kwargsDict.at(argName));
                 consumed_kwargs += 1;
               } else if (arg.default_value()) {
                 push(userCallableStack, *arg.default_value());
               } else {
                 throw std::runtime_error(c10::str(
                     functionSchema.name(),
                     "() is missing value for argument '",
                     argName,
                     "'. Declaration: ",
                     functionSchema));
               }
             }
             // Raise exception showing the unexpected kwargs.
             if (consumed_kwargs != kwargsDict.size()) {
               std::vector<std::string> names;
               for (const auto& entry : kwargsDict) {
                 const IValue& keyIValue = entry.key();
                 const string& keyStr = keyIValue.toStringRef();
                 names.emplace_back(keyStr);
               }
               throw std::runtime_error(
                   functionSchema.findErrorInKwargs(names));
             }

             // Get destination WorkerName.
             std::string dstWorkerNameStr;
             if (dstWorkerIValue.isString()) {
               // ivalue::ConstantString::str_ is a const member, which can't be
               // moved, copy it here.
               dstWorkerNameStr = dstWorkerIValue.toStringRef();
             } else {
               dstWorkerNameStr =
                   dstWorkerIValue.toCustomClass<dist_rpc::WorkerInfo>()->name_;
             }
             // Get RPC timeout, if specified by user.
             const auto rpcTimeout = timeoutIValue.toDouble();
             // Send RPC request.
             auto futureIValuePtr = dist_rpc::rpcTorchscript(
                 dstWorkerNameStr,
                 qualifiedName,
                 functionSchema,
                 userCallableStack,
                 rpcTimeout);

             // Push output to the stack.
             drop(stack, num_inputs);
             stack.emplace_back(std::move(futureIValuePtr));
             return 0;
           };
         },
         aliasAnalysisSpecialCase())});

// Implementations located in
// torch/csrc/jit/runtime/register_distributed_ops.cpp
TORCH_LIBRARY_IMPL(aten, CatchAll, m) {
  m.impl("get_gradients", [](int64_t context_id) {
    const auto& autogradContext =
        dist_autograd::DistAutogradContainer::getInstance().retrieveContext(
            context_id);
    return autogradContext->getGradients();
  });
}

} // namespace
} // namespace jit
} // namespace torch

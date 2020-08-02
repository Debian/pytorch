#include <torch/csrc/jit/passes/quantization/helper.h>
#include <torch/csrc/jit/passes/graph_rewrite_helper.h>

namespace torch {
namespace jit {

using graph_rewrite_helper::getFuncName;

struct FuncArg {
  std::string func_name;
  int arg_index;
};

using AtenFuncArgs = std::vector<FuncArg>;
using CallFuncArgs = std::vector<FuncArg>;

// White lists for quantizable operators
std::vector<std::string> _static_quantizable_call_funcs = {
    "conv2d",
    "linear",
    "batch_norm",
    "hardswish",
    "elu",
    "layer_norm",
    "group_norm",
    "instance_norm",
};

std::vector<std::string> _static_quantizable_aten_funcs = {
    "conv1d",
    "conv2d",
    "conv3d",
    "linear",
    "hardswish",
    "hardswish_",
    "elu",
    "elu_",
    "batch_norm",
    "layer_norm",
    "group_norm",
    "instance_norm",
};

std::vector<std::string> _dynamic_quantizable_call_funcs = {
    "linear",
};

std::vector<std::string> _dynamic_quantizable_aten_funcs = {
    "linear",
};

// These are the prim::CallFunctions that doesn't require observation and
// have a single input Tensor
// example: `prim::CallFunction(%dropout, %input_tensor, ...)
// so we propagate observed property from %input_tensor to the
// output of the `prim::CallFunction`
// Also these ops doesn't do computation on the value of Tensor, the
// operation only depends on the shape of the Tensor
std::vector<std::string> _single_input_general_shape_call_funcs = {
    "_max_pool1d",
    "_max_pool2d",
    "_max_pool3d",
    "dropout",
    "relu",
};

// Similar to prim::CallFunctions, there are aten ops that doesn't
// require observation and have a single input Tensor
// Also these ops doesn't do computation on the value of Tensor, the
// operation only depends on the shape of the Tensor
// e.g. `aten::flatten(%input_tensor, ...)`
std::vector<std::string> _single_input_general_shape_aten_funcs = {
    "max_pool1d",
    "max_pool2d",
    "max_pool3d",
    "flatten",
    "max",
    "min",
    "dropout",
    "reshape",
    // Non-inplace resize is deprecated
    "resize_",
    "chunk",
    "view",
    "transpose",
    "contiguous",
    "permute",
    "repeat",
    "repeat_interleave",
    "relu",
    "relu_",
    "squeeze",
    "squeeze_",
    "unsqueeze",
    "unsqueeze_",
    "detach",
    "detach_",
};

// Theses are prim::CallFunctions for ops that doesn't require observation and
// have a single input Tensor
// Also these ops do computation on the value of Tensor
// TODO: [Need verify] looks like we can quantize simple functionals that just
// call into aten functions
std::vector<std::string> _single_input_general_value_call_funcs = {
    "avg_pool1d",
    "avg_pool2d",
    "avg_pool3d",
    "adaptive_avg_pool1d",
    "adaptive_avg_pool2d",
    "adaptive_avg_pool3d",
    "interpolate",
    "upsample",
    "upsample_bilinear",
    "upsample_nearest",
    "hardtanh",
    "leaky_relu",
};

// Theses are aten functions for ops that doesn't require observation and
// have a single input Tensor
// Also these ops do computation on the value of Tensor
// e.g. `aten::avg_pool2d(%input_tensor, ...)`
std::vector<std::string> _single_input_general_value_aten_funcs = {
    "avg_pool1d",
    "avg_pool2d",
    "avg_pool3d",
    "adaptive_avg_pool1d",
    "adaptive_avg_pool2d",
    "adaptive_avg_pool3d",
    "mean",
    "upsample_nearest1d",
    "upsample_nearest2d",
    "upsample_nearest3d",
    "upsample_linear1d",
    "upsample_bilinear2d",
    "upsample_trilinear3d",
    "upsample_bicubic2d",
    "clamp",
    // "clamp_",  // Enable when quantized `clamp_` is ready
    "hardtanh",
    "hardtanh_",
    "leaky_relu",
    "leaky_relu_",
};

std::vector<std::string> _clamp_funcs = {
    "hardtanh",
    "hardtanh_",
    "clamp",
    // "clamp_",  // Enable when quantized `clamp_` is ready
};

const float _asym_scale = 1.0f / 256.0f;
const int _asym_zero_point = 0;
const float _sym_scale = 2.0f / 256.0f;
const int _sym_zero_point = 128;
// quantization parameters for ops with range 0 to 1
// for example: aten/src/ATen/native/quantized/cpu/qsigmoid.cpp
std::tuple<c10::QScheme, QParamVector> _per_tensor_asym_qparam =
    std::make_tuple(
        c10::kPerTensorAffine,
        QParamVector({std::make_pair(".scale", IValue(_asym_scale)),
                      std::make_pair(".zero_point", IValue(_asym_zero_point)),
                      std::make_pair(".scalar_type", IValue(c10::kQUInt8))}));

// quantization parrameters for ops with range -1 to 1
// for example: aten/src/ATen/native/quantized/cpu/qtanh.cpp
std::tuple<c10::QScheme, QParamVector> _per_tensor_sym_qparam = std::make_tuple(
    c10::kPerTensorAffine,
    QParamVector({std::make_pair(".scale", IValue(_sym_scale)),
                  std::make_pair(".zero_point", IValue(_sym_zero_point)),
                  std::make_pair(".scalar_type", IValue(c10::kQUInt8))}));

// Map from aten op symbol to the quantization parameters
// for the ops with fixed quantization parameters
std::unordered_map<NodeKind, std::tuple<c10::QScheme, QParamVector>>
    _fixed_qparams_map = {
        {Symbol::aten("hardsigmoid"), _per_tensor_asym_qparam},
        {Symbol::aten("hardsigmoid_"), _per_tensor_asym_qparam},
        {Symbol::aten("sigmoid"), _per_tensor_asym_qparam},
        {Symbol::aten("sigmoid_"), _per_tensor_asym_qparam},
        {Symbol::aten("tanh"), _per_tensor_sym_qparam},
        {Symbol::aten("tanh_"), _per_tensor_sym_qparam},
};

// Special checks for ops that do not require observers for all input tensors.
// For each operator in this list observers are inserted for the input based
// on the index specified.
AtenFuncArgs _observe_inputs_aten_func = {};
CallFuncArgs _observe_inputs_call_func = {{"batch_norm", 1}};

// Aten functions for getting tensor information
std::vector<std::string> _tensor_info_funcs = {"size", "len", "dim", "numel"};

// Aten functions whose output will be quantized or not quantized depending
// on input tensor
std::vector<std::string> _propagate_quant_single_input_ops = {"cat"};

// Rules are slightly different for binary ops like `aten::add`, for these ops,
// if both of the inputs are Tensor, we'll quantize the output only if both of
// the inputs are quantized
// if the second input is a Scalar, we'll only look at the first input to decide
// if we need to quantize the output
std::vector<std::string> _propagate_quant_binary_ops = {"add",
                                                        "add_",
                                                        "mul",
                                                        "mul_"};

// Check if `use` is an aten function of name `func_name` and if value
// `v` is the nth argument (if provided) of the function.
bool matchAtenFuncToUse(
    const Use& use,
    const std::string& func_name,
    c10::optional<int> n) {
  Node* node = use.user;
  return node->kind() == Symbol::aten(func_name) &&
      (!n.has_value() || n.value() == use.offset);
}

// Check if `use` is a CallFunction of name `func_name` and if value
// `v` is the nth argument (if provided) of the function
bool matchCallFuncToUse(
    const Use& use,
    const std::string& func_name,
    c10::optional<int> n) {
  Node* node = use.user;
  return node->kind() == prim::CallFunction &&
      getFuncName(node->inputs()[0]) == func_name &&
      (!n.has_value() || n.value() == use.offset);
}

// Check any use of `v` matches the aten function call
// or CallFunction patterns
bool matchArgPattern(
    Value* v,
    const AtenFuncArgs& aten_func_args,
    const CallFuncArgs& call_func_args) {
  for (const Use& u : v->uses()) {
    for (const auto& func_arg : aten_func_args) {
      if (matchAtenFuncToUse(u, func_arg.func_name, func_arg.arg_index)) {
        return true;
      }
    }

    for (const auto& func_arg : call_func_args) {
      if (matchCallFuncToUse(u, func_arg.func_name, func_arg.arg_index)) {
        return true;
      }
    }
  }
  return false;
}

bool isWeight(Value* v) {
  bool result = matchArgPattern(
      v,
      AtenFuncArgs(
          {{"conv1d", 1}, {"conv2d", 1}, {"conv3d", 1}, {"linear", 1}}),
      CallFuncArgs({{"linear", 2}}));
  return result;
}

bool isBiasOfConvOrLinear(Value* v) {
  bool result = matchArgPattern(
      v,
      AtenFuncArgs(
          {{"conv1d", 2}, {"conv2d", 2}, {"conv3d", 2}, {"linear", 2}}),
      CallFuncArgs({{"linear", 3}}));
  return result;
}

c10::optional<Use> getClampScalarInputUse(Value* v) {
  for (const auto& use : v->uses()) {
    for (const auto& aten_func : _clamp_funcs) {
      if (matchAtenFuncToUse(use, aten_func, 1) ||
          matchAtenFuncToUse(use, aten_func, 2)) {
        return use;
      }
    }
  }
  return c10::nullopt;
}

std::vector<Value*> getPassThroughInputs(Value* v) {
  Node* n = v->node();
  if (isSingleInputGeneralCallFunction(n)) {
    return {n->input(1)};
  } else if (
      isSingleInputGeneralAtenFunction(n) ||
      (n->kind() == Symbol::aten("sort") && v->offset() == 0)) {
    return {n->input(0)};
  } else if (n->kind() == prim::If && n->outputs().size() == 1) {
    std::vector<Value*> inputs;
    for (Block* subblock : n->blocks()) {
      if (alwaysRaisesException(subblock)) {
        continue;
      }
      auto* output = subblock->outputs()[0];
      inputs.push_back(output);
    }
    return inputs;
  } else if (n->kind() == prim::ListUnpack || n->kind() == prim::TupleUnpack) {
    return {n->input(0)};
  } else if (
      n->kind() == prim::ListConstruct || n->kind() == prim::TupleConstruct) {
    std::vector<Value*> inputs;
    for (auto* v : n->inputs()) {
      inputs.push_back(v);
    }
    return inputs;
  } else if (isListAdd(n)) {
    // We need to propagate dequantize of n->input(0) if it is
    // not an empty list
    if (isEmptyList(n->input(0)->node())) {
      return {n->input(1)};
    } else {
      return {n->input(0), n->input(1)};
    }
  }

  return {};
}

std::vector<NodeKind> toAtenSymbol(const std::vector<std::string>& func_names) {
  std::vector<NodeKind> symbols;
  std::transform(
      func_names.begin(),
      func_names.end(),
      std::back_inserter(symbols),
      Symbol::aten);
  return symbols;
}

bool isAtenFunc(Node* n, const std::vector<NodeKind>& aten_funcs) {
  return std::find(aten_funcs.begin(), aten_funcs.end(), n->kind()) !=
      aten_funcs.end();
}

bool isAtenFunc(Node* n, const std::vector<std::string>& aten_funcs) {
  const auto& symbols = toAtenSymbol(aten_funcs);
  return isAtenFunc(n, symbols);
}

// TODO: factor out isCallFunc
bool isFunctionNode(
    Node* n,
    const std::vector<std::string>& call_funcs,
    const std::vector<std::string>& aten_funcs) {
  bool is_func_node = isAtenFunc(n, aten_funcs);
  if (n->kind() == prim::CallFunction) {
    auto func_name = getFuncName(n->inputs()[0]);
    is_func_node |=
        std::find(call_funcs.begin(), call_funcs.end(), func_name) !=
        call_funcs.end();
  }
  return is_func_node;
}

bool isSingleInputGeneralShapeAtenFunction(Node* n) {
  return isAtenFunc(n, _single_input_general_shape_aten_funcs);
}

bool isSingleInputGeneralValueAtenFunction(Node* n) {
  return isAtenFunc(n, _single_input_general_value_aten_funcs) ||
      isBinaryOpWithScalarInput(n);
}

bool isSingleInputGeneralCallFunction(Node* n) {
  static std::vector<std::string> single_input_general_call_funcs;
  std::copy(
      _single_input_general_shape_call_funcs.begin(),
      _single_input_general_shape_call_funcs.end(),
      std::back_inserter(single_input_general_call_funcs));
  std::copy(
      _single_input_general_value_call_funcs.begin(),
      _single_input_general_value_call_funcs.end(),
      std::back_inserter(single_input_general_call_funcs));
  return isFunctionNode(
      n,
      /* call_funcs = */ single_input_general_call_funcs,
      /* aten_funcs = */ {});
}

bool isSingleInputGeneralAtenFunction(Node* n) {
  static std::vector<NodeKind> fixed_qparams_aten_funcs;
  std::transform(
      _fixed_qparams_map.begin(),
      _fixed_qparams_map.end(),
      std::back_inserter(fixed_qparams_aten_funcs),
      [](auto pair) { return pair.first; });

  return isSingleInputGeneralValueAtenFunction(n) ||
      isSingleInputGeneralShapeAtenFunction(n) ||
      isAtenFunc(n, fixed_qparams_aten_funcs);
}

bool isClamp(Node* n) {
  return isAtenFunc(n, _clamp_funcs);
}

bool isTensorInfoNode(Node* n) {
  return isAtenFunc(n, _tensor_info_funcs);
}

bool isPropagateQuantSingleInputOp(Node* n) {
  return isAtenFunc(n, _propagate_quant_single_input_ops);
}

bool isPropagateQuantBinaryOp(Node* n) {
  return isAtenFunc(n, _propagate_quant_binary_ops);
}

bool isPropagateQuantOp(Node* n) {
  return isPropagateQuantSingleInputOp(n) || isPropagateQuantBinaryOp(n);
}

bool isBinaryOpWithScalarInput(Node* n) {
  return isPropagateQuantBinaryOp(n) && isScalar(n->input(1));
}

bool isListAdd(Node* n) {
  return n->kind() == Symbol::aten("add") && n->inputs().size() == 2 &&
      n->outputs().size() == 1 &&
      n->output()->type()->isSubtypeOf(ListType::ofTensors()) &&
      n->input(0)->type()->isSubtypeOf(ListType::ofTensors()) &&
      n->input(1)->type()->isSubtypeOf(ListType::ofTensors());
}

bool isEmptyList(Node* n) {
  if (n->outputs().size() != 1) {
    return false;
  }
  bool is_empty_tensor_list_node = n->kind() == prim::ListConstruct &&
      n->inputs().size() == 0 &&
      n->output()->type()->isSubtypeOf(ListType::ofTensors());
  auto iv = toIValue(n->output());
  bool is_empty_tensor_list_constant = iv.has_value() && iv->isList() &&
      iv->toList().size() == 0 &&
      n->output()->type()->isSubtypeOf(ListType::ofTensors());
  return is_empty_tensor_list_node || is_empty_tensor_list_constant;
}

c10::optional<std::tuple<c10::QScheme, QParamVector>> getFixedQParams(Node* n) {
  static std::vector<NodeKind> fixed_qparam_funcs;
  std::transform(
      _fixed_qparams_map.begin(),
      _fixed_qparams_map.end(),
      std::back_inserter(fixed_qparam_funcs),
      [](const auto& pair) { return pair.first; });
  if (isAtenFunc(n, fixed_qparam_funcs)) {
    return _fixed_qparams_map.at(n->kind());
  }
  return c10::nullopt;
}

bool userDefinedCallFunction(Node* n) {
  return n->kind() == prim::CallFunction &&
      !isSingleInputGeneralCallFunction(n) &&
      !isFunctionNode(n, _static_quantizable_call_funcs, {});
}

bool nodeQuantizable(Node* n, QuantType quant_type) {
  bool is_dynamic = quant_type == QuantType::DYNAMIC;
  return isFunctionNode(
      n,
      /* call_funcs = */
      is_dynamic ? _dynamic_quantizable_call_funcs
                 : _static_quantizable_call_funcs,
      /* aten_funcs = */
      is_dynamic ? _dynamic_quantizable_aten_funcs
                 : _static_quantizable_aten_funcs);
}

bool useQuantizable(const Use& use, QuantType quant_type) {
  for (const auto& func_input : _observe_inputs_aten_func) {
    if (matchAtenFuncToUse(use, func_input.func_name, c10::nullopt)) {
      return use.offset == func_input.arg_index;
    }
  }

  for (const auto& func_input : _observe_inputs_call_func) {
    if (matchCallFuncToUse(use, func_input.func_name, c10::nullopt)) {
      return use.offset == func_input.arg_index;
    }
  }

  return nodeQuantizable(use.user, quant_type);
}

std::shared_ptr<Graph> getCallFunctionGraph(Node* n) {
  auto* func_node = n->input(0)->node();
  auto func = func_node->output()->type()->expect<FunctionType>()->function();
  TORCH_CHECK(
      func->isGraphFunction(), "Quantization only works for graph function");
  return func->graph();
}

// Block helper functions
bool alwaysRaisesException(Block* block) {
  for (Node* n : block->nodes()) {
    if (n->kind() == prim::RaiseException) {
      return true;
    }
    if (n->kind() == prim::If) {
      bool exception = true;
      for (Block* b : n->blocks()) {
        exception &= alwaysRaisesException(b);
      }
      if (exception) {
        return true;
      }
    }
  }
  return false;
}

// Check if a value in the graph is a Scalar value
bool isScalar(Value* v) {
  auto iv = toIValue(v);
  return v->type()->isSubtypeOf(NumberType::get()) ||
      (v->type()->isSubtypeOf(TensorType::get()) && iv && iv->isTensor() &&
       iv->toTensor().dim() == 0);
}

// =================== Graph/Module analysis helper functions ============
// Check if value is the input of the graph
bool hitGraphInput(Value* value) {
  Graph* graph = value->owningGraph();
  const auto& inputs = graph->inputs();
  return std::find(inputs.begin(), inputs.end(), value) != inputs.end();
}

// Get the module access path for a Value representing a module instance
// by tracing back the GetAttr nodes and recording all the attribute
// names along the way.
// For example, the module access path will be ['sub', 'basic_block', 'conv1']
// for `self.sub.basic_block.conv1`
std::vector<std::string> getModuleAccessPath(Value* instance, Value* self) {
  std::vector<std::string> path;
  // Iterator to traverse back the GetAttr calls
  Value* iter = instance;
  // trace back the instance to recover the path of the submodule
  while (!hitGraphInput(iter) && iter->node()->kind() == prim::GetAttr) {
    Node* get_attr = iter->node();
    // record the name of GetAttr
    path.push_back(get_attr->s(attr::name));
    // trace back the chain of GetAttr
    iter = get_attr->inputs()[0];
  }
  TORCH_CHECK(
      iter == self,
      "Can't handle the access pattern of GetAttr "
      " in getModuleAccessPath, traced back to:",
      iter->debugName(),
      " which is not self:",
      self->debugName());
  std::reverse(path.begin(), path.end());
  return path;
}

Module findChildModule(
    const Module& module,
    const std::vector<std::string>& path) {
  Module m = module;
  for (const auto& p : path) {
    m = m.attr(p).toModule();
  }
  return m;
}

Module getInvokedModule(Module& module, Node* n, Value* self) {
  auto* instance = n->inputs()[0];
  auto path = getModuleAccessPath(instance, self);
  return findChildModule(module, path);
}

// ==================== filter functions for matches ==============
bool is_int_constant(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap,
    const std::string& vname,
    int value) {
  const auto& match_vmap = match.values_map;
  auto v = toIValue(match_vmap.at(vmap.at(vname)));
  return v && v->isInt() && v->toInt() == value;
}

bool is_functional(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap,
    const std::string& vname,
    const std::string& functional) {
  const auto& match_vmap = match.values_map;
  Value* v = match_vmap.at(vmap.at(vname));
  return v->type()->cast<FunctionType>() && getFuncName(v) == functional;
}

bool is_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap,
    const std::string& vname,
    const std::string& module_qualified_name) {
  const auto& match_vmap = match.values_map;
  Value* relu = match_vmap.at(vmap.at(vname));
  auto type = relu->type()->cast<ClassType>();
  if (type && type->name()) {
    static std::regex mangle_re("\\.___torch_mangle_\\d+");
    auto qualified_name =
        std::regex_replace(type->name()->qualifiedName(), mangle_re, "");
    return qualified_name == module_qualified_name;
  }
  return false;
};

bool aten_add_alpha_is_one(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_int_constant(match, vmap, "alpha", 1);
}

bool is_functional_relu(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_functional(match, vmap, "relu", "relu");
}

bool is_relu_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match, vmap, "relu", "__torch__.torch.nn.modules.activation.ReLU");
}

bool is_functional_linear(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_functional(match, vmap, "linear", "linear");
}

bool is_linear_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match, vmap, "linear", "__torch__.torch.nn.modules.linear.Linear");
}

bool is_conv1d_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match, vmap, "conv", "__torch__.torch.nn.modules.conv.Conv1d");
}

bool is_conv2d_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match, vmap, "conv", "__torch__.torch.nn.modules.conv.Conv2d");
}

bool is_conv3d_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match, vmap, "conv", "__torch__.torch.nn.modules.conv.Conv3d");
}

bool is_batchnorm2d_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match,
      vmap,
      "batchnorm",
      "__torch__.torch.nn.modules.batchnorm.BatchNorm2d");
}

bool is_batchnorm3d_module(
    const Match& match,
    const std::unordered_map<std::string, Value*>& vmap) {
  return is_module(
      match,
      vmap,
      "batchnorm",
      "__torch__.torch.nn.modules.batchnorm.BatchNorm3d");
}

} // namespace jit
} // namespace torch

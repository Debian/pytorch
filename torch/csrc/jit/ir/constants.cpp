#include <torch/csrc/jit/ir/constants.h>
#include <ATen/core/functional.h>
#include <torch/csrc/autograd/variable.h>
#include <torch/csrc/jit/ir/ir.h>
#include <torch/csrc/jit/runtime/custom_operator.h>
#include <torch/csrc/jit/runtime/operator.h>

namespace torch {
namespace jit {

namespace {
c10::AliasAnalysisKind aliasAnalysisInternalSpecialCase() {
  return AliasAnalysisKind::INTERNAL_SPECIAL_CASE;
}
} // namespace

bool insertableTensor(const at::Tensor& ten) {
  return !ten.requires_grad();
}

bool insertableIValue(const IValue& ivalue) {
  if (ivalue.isInt() || ivalue.isNone() || ivalue.isBool() ||
      ivalue.isDouble() || ivalue.isString() || ivalue.isDevice()) {
    return true;
  }
  if (ivalue.isTensor()) {
    return insertableTensor(ivalue.toTensor());
  }
  if (ivalue.isList() || ivalue.isTuple()) {
    c10::ArrayRef<IValue> elems;
    if (ivalue.isTuple()) {
      elems = ivalue.toTuple()->elements();
    } else {
      elems = ivalue.toListRef();
    }
    return std::all_of(elems.begin(), elems.end(), [](const IValue& tup_elem) {
      return insertableIValue(tup_elem);
    });
  }

  if (ivalue.isGenericDict()) {
    const auto& dict = ivalue.toGenericDict();
    return std::all_of(dict.begin(), dict.end(), [](const auto& entry) {
      return insertableIValue(entry.key()) && insertableIValue(entry.value());
    });
  }

  return false;
}

Value* insertConstant(
    Graph& g,
    const IValue& val,
    c10::optional<SourceRange> loc,
    c10::optional<ScopePtr> scope) {
  auto value = tryInsertConstant(g, val, loc, scope);
  if (value) {
    return *value;
  }
  throw constant_not_supported_error(
      "Unsupported value kind: " + val.tagKind());
}

// IValue -> Constant node
c10::optional<Value*> tryInsertConstant(
    Graph& g,
    const IValue& val,
    c10::optional<SourceRange> loc,
    c10::optional<ScopePtr> scope) {
  Node* n = g.create(prim::Constant);
  if (val.isTensor()) {
    at::Tensor ref = val.toTensor();
    if (!ref.defined()) {
      n->destroy();
      return g.insertNode(g.createNone())->output();
    }
    TORCH_INTERNAL_ASSERT(!ref.requires_grad());
    n->output()->inferTypeFrom(
        ref); // note: before t_ because of std::move(ref)
    n->t_(attr::value, std::move(ref));
  } else if (val.isInt()) {
    n->i_(attr::value, val.toInt());
    n->output()->setType(IntType::get());
  } else if (val.isDouble()) {
    n->f_(attr::value, val.toDouble());
    n->output()->setType(FloatType::get());
  } else if (val.isBool()) {
    n->i_(attr::value, val.toBool());
    n->output()->setType(BoolType::get());
  } else if (val.isList()) {
    bool fast_path_list =
        val.isBoolList() || val.isIntList() || val.isDoubleList();
    if (fast_path_list || insertableIValue(val)) {
      n->ival_(attr::value, val);
      n->output()->setType(val.type());
    } else {
      n->destroy();
      return c10::nullopt;
    }
  } else if (val.isString()) {
    n->s_(attr::value, val.toString()->string());
    n->output()->setType(StringType::get());
  } else if (val.isDevice()) {
    std::stringstream ss;
    ss << val.toDevice();
    n->s_(attr::value, ss.str());
    n->output()->setType(DeviceObjType::get());
  } else if (val.isNone()) {
    n->output()->setType(NoneType::get());
  } else if (val.isTuple()) {
    if (insertableIValue(val)) {
      n->ival_(attr::value, val);
      n->output()->setType(val.type());
    } else {
      n->destroy();
      return c10::nullopt;
    };
  } else if (val.isGenericDict() && insertableIValue(val)) {
    n->ival_(attr::value, val);
    n->output()->setType(val.type());
  } else {
    n->destroy();
    return c10::nullopt;
  }
  if (loc)
    n->setSourceRange(*loc);
  if (scope)
    n->setScope(*scope);
  return g.insertNode(n)->output();
}

c10::optional<IValue> toIValue(const Value* v) {
  if (v->node()->kind() != prim::Constant || v->type()->cast<FunctionType>()) {
    return c10::nullopt;
  }
  const Node* node = v->node();
  const TypePtr& type = v->type();
  if (type->isSubtypeOf(TensorType::get())) {
    return node->t(attr::value);
  } else if (type->isSubtypeOf(BoolType::get())) {
    return (bool)node->i(attr::value);
  } else if (
      type->isSubtypeOf(NumberType::get()) &&
      node->kindOf(attr::value) == AttributeKind::i) {
    return node->i(attr::value);
  } else if (
      type->isSubtypeOf(NumberType::get()) &&
      node->kindOf(attr::value) == AttributeKind::f) {
    return node->f(attr::value);
  } else if (
      type->cast<ListType>() &&
      node->kindOf(attr::value) == AttributeKind::ival) {
    const auto& list = node->ival(attr::value);
    TORCH_INTERNAL_ASSERT(list.isList());
    return list;
  } else if (
      type->cast<DictType>() &&
      node->kindOf(attr::value) == AttributeKind::ival) {
    const auto& dict = node->ival(attr::value);
    TORCH_INTERNAL_ASSERT(dict.isGenericDict());
    return dict;
  } else if (
      type->cast<TupleType>() &&
      node->kindOf(attr::value) == AttributeKind::ival) {
    const auto& tup = node->ival(attr::value);
    TORCH_INTERNAL_ASSERT(tup.isTuple());
    return tup;
  } else if (type == StringType::get()) {
    const auto& s = node->s(attr::value);
    return s;
  } else if (type == DeviceObjType::get()) {
    auto d = c10::Device(node->s(attr::value));
    return d;
  } else if (node->mustBeNone()) {
    return IValue();
  } else {
    std::stringstream ss;
    ss << "constant literal not supported for: " << type->str();
    throw std::runtime_error(ss.str());
  }
}

} // namespace jit
} // namespace torch

#include <torch/csrc/jit/python/python_sugared_value.h>
#include <torch/csrc/Dtype.h>
#include <torch/csrc/Layout.h>
#include <torch/csrc/MemoryFormat.h>
#include <torch/csrc/jit/frontend/schema_matching.h>
#include <torch/csrc/jit/python/module_python.h>
#include <memory>
#include <sstream>
#include <string>
#include <tuple>
#include <vector>

#include <Python.h>

namespace torch {
namespace jit {

std::string typeString(py::handle h) {
  return py::str(h.get_type().attr("__name__"));
}

c10::optional<StrongFunctionPtr> as_function(const py::object& obj) {
  if (py::isinstance<StrongFunctionPtr>(obj)) {
    return py::cast<StrongFunctionPtr>(obj);
  }
  return c10::nullopt;
}

FunctionSchema PythonValue::getSchema(
    const size_t n_args,
    const size_t n_binders,
    const SourceRange& loc) {
  auto annotations = py::module::import("torch.jit.annotations");
  const auto callable = moduleSelf_ ? py::getattr(self, "original_fn") : self;

  // Make sure the function is not a class instantiation (e.g. `Exception()`)
  annotations.attr("check_fn")(callable, loc);
  auto is_vararg = py::cast<bool>(annotations.attr("is_vararg")(callable));

  auto signature = annotations.attr("get_signature")(
      callable, rcb ? *rcb : py::none(), loc, bool(moduleSelf_));
  std::vector<Argument> args, rets;

  auto py_param_names = annotations.attr("get_param_names")(callable, n_args);
  auto param_names = py::cast<std::vector<std::string>>(py_param_names);
  auto names_it = param_names.begin();
  if (moduleSelf_) {
    if (param_names.size() == 0) {
      throw ErrorReport(loc)
          << "Non-static method does not have a self argument";
    }

    // If there is a `self` parameter on the callable, skip it on the names list
    args.emplace_back(Argument(*names_it, moduleSelf_->type(), {}, {}, false));
    ++names_it;
  }
  if (signature.is_none()) {
    // No type signature was provided on the callable, so make a default
    // signature where each argument is typed as a Tensor
    for (; names_it != param_names.end(); ++names_it) {
      args.emplace_back(Argument(
          /*name=*/*names_it,
          /*type=*/TensorType::get(),
          /*N=*/c10::nullopt,
          /*default_value=*/c10::nullopt,
          /*kwarg_only=*/false));
    }

    // Use as many outputs as are requested to make the return type
    TypePtr ret_type = TensorType::get();
    if (n_binders == 0) {
      ret_type = NoneType::get();
    } else if (n_binders > 1) {
      std::vector<TypePtr> tuple_values(n_binders, ret_type);
      ret_type = TupleType::create(std::move(tuple_values));
    }
    rets.emplace_back(Argument("0", ret_type, {}, {}, false));
  } else {
    // Use the provided type signature
    std::vector<TypePtr> arg_types;
    TypePtr ret_type;
    std::tie(arg_types, ret_type) =
        py::cast<std::pair<std::vector<TypePtr>, TypePtr>>(signature);

    // arg_types does not include self but param_names does, so adjust for that
    // if needed
    TORCH_INTERNAL_ASSERT(
        arg_types.size() == param_names.size() - (moduleSelf_ ? 1 : 0));

    auto types_it = arg_types.begin();
    for (; types_it != arg_types.end(); ++types_it, ++names_it) {
      args.push_back(Argument(
          /*name=*/*names_it,
          /*type=*/std::move(*types_it),
          /*N=*/c10::nullopt,
          /*default_value=*/c10::nullopt,
          /*kwarg_only=*/false));
    }
    rets.push_back(Argument("0", std::move(ret_type), {}, {}, false));
  }

  std::string name;
  if (py::hasattr(self, "__qualname__")) {
    // Use the qualified name if possible
    name = py::str(py::getattr(self, "__qualname__"));
  } else if (py::hasattr(self, "__name__")) {
    name = py::str(py::getattr(self, "__name__"));
  }
  return FunctionSchema(name, "", std::move(args), std::move(rets), is_vararg);
}

std::shared_ptr<SugaredValue> PythonValue::call(
    const SourceRange& loc,
    Function& m,
    at::ArrayRef<NamedValue> inputs_,
    at::ArrayRef<NamedValue> attributes,
    size_t n_binders) {
  std::vector<NamedValue> inputsWithSelf;
  if (moduleSelf_) {
    inputsWithSelf.emplace_back(NamedValue("self", moduleSelf_));
  }
  inputsWithSelf.insert(inputsWithSelf.end(), inputs_.begin(), inputs_.end());
  inputs_ = inputsWithSelf;

  auto schema = getSchema(inputs_.size(), n_binders, loc);
  auto inputs = toValues(*m.graph(), inputs_);

  MatchedSchema matched_schema =
      matchSchema(schema, loc, *m.graph(), inputs_, attributes);

  // If if a function is marked as dropped,
  // we throw an exception if it is invoked.
  if (py::cast<bool>(py::module::import("torch._jit_internal")
                         .attr("should_drop")(self))) {
    auto g = m.graph();
    auto err_msg = insertConstant(
        *g,
        IValue(
            "This Python function is annotated to be ignored and cannot be run"));
    g->insert(prim::RaiseException, {err_msg}, {}, loc);
    return std::make_shared<SimpleValue>(
        g->insertNode(g->createUninitialized(matched_schema.return_types.at(0)))
            ->output());
  }

  // Release the function object so we can wrap it in a PythonOp
  py::object func = self;
  std::string cconv(inputs.size(), 'd');
  Node* new_node = m.graph()->insertNode(
      m.graph()->createPythonOp(THPObjectPtr(func.release().ptr()), cconv, {}));

  new_node->setSourceRange(loc);
  for (auto& i : matched_schema.inputs)
    new_node->addInput(i);

  Value* output =
      new_node->addOutput()->setType(matched_schema.return_types.at(0));
  return std::make_shared<SimpleValue>(output);
}

std::string PythonValue::kind() const {
  std::stringstream ss;
  ss << "python value of type '" << typeString(self) << "'";
  return ss.str();
}

std::vector<std::shared_ptr<SugaredValue>> PythonValue::asTuple(
    const SourceRange& loc,
    Function& m,
    const c10::optional<size_t>& size_hint) {
  const std::string type_str = typeString(self);
  std::stringstream ss;
  ss << kind() << " cannot be used as a tuple";
  checkForAddToConstantsError(ss);
  throw ErrorReport(loc) << ss.str();
}

std::shared_ptr<SugaredValue> PythonValue::attr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  const std::string type_str = typeString(self);
  std::stringstream ss;
  ss << "attribute lookup is not defined on " << kind();
  checkForAddToConstantsError(ss);
  throw ErrorReport(loc) << ss.str();
}

py::object PythonValue::getattr(
    const SourceRange& loc,
    const std::string& name) {
  try {
    return py::getattr(self, name.c_str());
  } catch (py::error_already_set& e) {
    throw ErrorReport(loc) << "object has no attribute " << name;
  }
}

void PythonValue::checkForAddToConstantsError(std::stringstream& ss) {
  auto nn = py::module::import("torch.nn");
  if (py::isinstance(self, nn.attr("ModuleList")) ||
      py::isinstance(self, nn.attr("Sequential"))) {
    ss << ". Did you forget to add it to __constants__? ";
  }
}

std::shared_ptr<SugaredValue> PythonModuleValue::attr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  py::object member = getattr(loc, field);
  // note: is_constant = true because we consider that global properties
  // on modules like math.pi or torch.float to be constants
  // even though it is possible, though rare, for someone to mutate them
  return toSugaredValue(member, m, loc, /*is_constant=*/true);
}

Value* ModuleValue::asValue(const SourceRange& loc, Function& m) {
  return self_;
}

SugaredValuePtr ModuleValue::asTupleValue(const SourceRange& loc, Function& m) {
  if (concreteType_->getIterableModuleKind() == IterableModuleKind::LIST) {
    auto dict = getSugaredDict(loc, m);
    auto mods = dict->getModules();
    return mods;
  }
  throw ErrorReport(loc)
      << "Only ModuleList or Sequential modules can be used as tuple";
}

SugaredValuePtr ModuleValue::getitem(
    const SourceRange& loc,
    Function& m,
    Value* idx) {
  if (concreteType_->getIterableModuleKind() == IterableModuleKind::LIST) {
    return getSugaredDict(loc, m)->getModules()->getitem(loc, m, idx);
  }
  throw ErrorReport(loc)
      << "Only ModuleList, Sequential, and ModuleDict modules are subscriptable";
}

void checkInterface(
    const SourceRange& loc,
    Function& m,
    std::shared_ptr<ModuleValue> self,
    const std::string& field) {
  if (self->asValue(loc, m)->type()->cast<InterfaceType>()) {
    throw ErrorReport(loc)
        << "Could not compile " << field
        << "() because module is an interface type. Please file issue.";
  }
}

void recurseThroughNestedModules(
    const SourceRange& loc,
    Function& m,
    std::vector<SugaredValuePtr>& keys,
    std::vector<SugaredValuePtr>& values,
    std::shared_ptr<ModuleValue> self,
    const std::string& prefix,
    const std::string& field) {
  auto prefix_value =
      std::make_shared<SimpleValue>(insertConstant(*m.graph(), prefix));

  keys.push_back(prefix_value);
  values.push_back(self);

  checkInterface(loc, m, self, field);
  auto module_dict = self->getSugaredDict(loc, m);
  auto keys_iter = module_dict->keys_;
  auto module_values_iter = module_dict->modules_;
  for (size_t i = 0; i < keys_iter->tup_.size(); ++i) {
    std::shared_ptr<SugaredValue> module_sugared_value =
        module_values_iter->tup_.at(i);
    auto module_value =
        std::dynamic_pointer_cast<ModuleValue>(module_sugared_value);

    auto keys_value = keys_iter->tup_.at(i);
    auto key_string = toIValue(keys_value->asValue(loc, m))->toStringRef();
    std::string submodule_prefix = prefix;
    if (prefix != "") {
      submodule_prefix = prefix + ".";
    }
    submodule_prefix = submodule_prefix + key_string;
    recurseThroughNestedModules(
        loc, m, keys, values, module_value, submodule_prefix, field);
  };
}

std::shared_ptr<SugaredDict> ModuleValue::getSugaredNamedBufferDict(
    const SourceRange& loc,
    Function& m) {
  std::vector<std::string> paramNames;
  std::vector<SugaredValuePtr> values;

  const auto& selfType = concreteType_->getJitType()->expect<ClassType>();
  for (size_t i = 0; i < selfType->numAttributes(); ++i) {
    if (selfType->is_buffer(i)) {
      paramNames.push_back(selfType->getAttributeName(i));
    }
  }

  std::vector<SugaredValuePtr> keys;
  for (const auto& name : paramNames) {
    auto name_v =
        std::make_shared<SimpleValue>(insertConstant(*m.graph(), name));
    Value* tensor_v = m.graph()->insertGetAttr(self_, name);
    values.push_back(tryGetAttr(loc, m, name));
    keys.push_back(name_v);
  }

  return std::make_shared<SugaredDict>(
      std::make_shared<ModuleValue>(self_, concreteType_),
      std::make_shared<SugaredTupleValue>(keys),
      std::make_shared<SugaredTupleValue>(values));
}

std::shared_ptr<SugaredDict> ModuleValue::getSugaredDict(
    const SourceRange& loc,
    Function& m) {
  std::vector<std::string> submoduleNames;
  const auto& selfType = concreteType_->getJitType()->expect<ClassType>();
  for (size_t i = 0; i < selfType->numAttributes(); ++i) {
    const auto& attrType = selfType->getAttribute(i);
    if (attrType->is_module()) {
      submoduleNames.push_back(selfType->getAttributeName(i));
    }
  }

  std::vector<SugaredValuePtr> keys;
  std::vector<SugaredValuePtr> values;
  for (const auto& name : submoduleNames) {
    auto name_v =
        std::make_shared<SimpleValue>(insertConstant(*m.graph(), name));
    Value* module_v = m.graph()->insertGetAttr(self_, name);
    auto mod_v = std::make_shared<ModuleValue>(
        module_v, concreteType_->findSubmoduleConcreteType(name));

    keys.push_back(name_v);
    values.push_back(mod_v);
  }

  return std::make_shared<SugaredDict>(
      std::make_shared<ModuleValue>(self_, concreteType_),
      std::make_shared<SugaredTupleValue>(keys),
      std::make_shared<SugaredTupleValue>(values));
}

std::shared_ptr<SugaredValue> SugaredDict::attr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  // Recursive compilation does not maintain module aliasing,
  // so we do not add uniqueness checks on
  // "children"/"named_children"/"modules"/"named_modules"
  checkInterface(loc, m, self_, field);
  if (field == "keys") {
    return std::make_shared<ModuleDictMethod>(keys_, "keys");
  } else if (field == "values" || field == "children") {
    return std::make_shared<ModuleDictMethod>(modules_, field);
  } else if (
      field == "items" || field == "named_children" ||
      field == "named_buffers") {
    auto iterator = std::make_shared<IterableTree>();
    iterator->addChild(loc, m, keys_);
    iterator->addChild(loc, m, modules_);
    return std::make_shared<ModuleDictMethod>(iterator, field);
  } else if (field == "named_modules" || field == "modules") {
    std::vector<SugaredValuePtr> keys;
    std::vector<SugaredValuePtr> values;
    recurseThroughNestedModules(loc, m, keys, values, self_, "", field);
    if (field == "modules") {
      return std::make_shared<ModuleDictMethod>(
          std::make_shared<SugaredTupleValue>(values), field);
    } else {
      auto iterator = std::make_shared<IterableTree>();
      iterator->addChild(loc, m, std::make_shared<SugaredTupleValue>(keys));
      iterator->addChild(loc, m, std::make_shared<SugaredTupleValue>(values));
      return std::make_shared<ModuleDictMethod>(iterator, field);
    }
  }
  TORCH_INTERNAL_ASSERT(false);
}

// helper function for instantiating a SugaredValue from an IValue
std::shared_ptr<SugaredValue> toSugaredValue(
    const IValue& v,
    Function& m,
    const SourceRange& loc) {
  if (v.isTuple()) {
    auto tp = v.toTuple();
    std::vector<Value*> values;
    values.reserve(tp->elements().size());
    for (const auto& e : tp->elements()) {
      values.push_back(toSugaredValue(e, m, loc)->asValue(loc, m));
    }
    return toSimple(
        m.graph()->insertNode(m.graph()->createTuple(values))->output());
  } else {
    return toSimple(m.graph()->insertConstant(v, loc));
  }
}

// This method controls how we desugar attribute lookups on ScriptModules
std::shared_ptr<SugaredValue> ModuleValue::tryGetAttr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  // 1. Look inside Module object for the field.
  const auto& selfType_ = concreteType_->getJitType();
  if (selfType_->cast<InterfaceType>()) {
    return std::make_shared<SimpleValue>(self_)->attr(loc, m, field);
  }

  const auto& selfType = selfType_->expect<ClassType>();

  if (selfType->hasAttribute(field) &&
      selfType->getAttribute(field)->is_module()) {
    // ...if it's a submodule, return it as a new ModuleValue.
    const auto submoduleConcreteType =
        concreteType_->findSubmoduleConcreteType(field);
    return std::make_shared<ModuleValue>(
        m.graph()->insertGetAttr(self_, field), submoduleConcreteType);
  } else if (selfType->hasAttribute(field) || selfType->findMethod(field)) {
    // ...otherwise, methods, parameters, attributes, and buffers are all
    // first class so they get returned as SimpleValues
    return std::make_shared<SimpleValue>(self_)->attr(loc, m, field);
  } else if (selfType->hasConstant(field)) {
    auto v = selfType->getConstant(field);
    return toSugaredValue(v, m, loc);
  }

  // 2. Special case: for module dicts we manually desugar items(), keys(),
  // values() calls into the appropriate method.
  if (concreteType_->getIterableModuleKind() == IterableModuleKind::DICT) {
    if (field == "items" || field == "keys" || field == "values") {
      return getSugaredDict(loc, m)->attr(loc, m, field);
    }
  }

  if (field == "named_modules" || field == "modules" || field == "children" ||
      field == "named_children") {
    return getSugaredDict(loc, m)->attr(loc, m, field);
  }

  if (field == "named_buffers") {
    return getSugaredNamedBufferDict(loc, m)->attr(loc, m, field);
  }

  // 3. Check if this is the name of an overloaded method.

  // This can also be a call to a non-script module, or a plain
  // python method. If so return this as a python value.
  if (const auto overloads = concreteType_->findOverloads(field)) {
    return std::make_shared<MethodValue>(self_, *overloads);
  }

  // 4. Check if it's a function attribute.
  if (const auto fnAttr = concreteType_->findFunctionAttribute(field)) {
    return std::make_shared<FunctionValue>(*fnAttr);
  } else if (const auto builtin = concreteType_->findBuiltinFunction(field)) {
    return std::make_shared<BuiltinFunction>(*builtin, /*self=*/c10::nullopt);
  }

  // 5. Check if it's an attribute of the original Python class that this
  // ScriptModule was derived from. The only class attributes we handle are
  // methods.
  py::object unboundMethod = py::getattr(
      concreteType_->getPyClass(),
      field.c_str(),
      pybind11::cast<pybind11::none>(Py_None));

  if (py::isinstance<py::function>(unboundMethod)) {
    bool isStaticFn = py::cast<bool>(
        py::module::import("torch._jit_internal")
            .attr("is_static_fn")(concreteType_->getPyClass(), field.c_str()));
    if (isStaticFn) {
      // Functions within the module annotated with @staticmethod do not need
      // binding.
      py::object staticFn = py::module::import("torch._jit_internal")
                                .attr("get_static_fn")(
                                    concreteType_->getPyClass(), field.c_str());
      return toSugaredValue(staticFn, m, loc);
    }
    // For Python methods that we're trying to call directly, we need to bind
    // the method to a self. (see the documentation for lazy_bind in Python for
    // more info).
    bool isIgnoredFn =
        py::cast<bool>(py::module::import("torch._jit_internal")
                           .attr("is_ignored_fn")(unboundMethod));
    if (isIgnoredFn) {
      // Create a generated ScriptModule type with module_ set as cpp_module
      auto boundMethod = py::module::import("torch.jit._recursive")
                             .attr("lazy_bind")(concreteType_, unboundMethod);
      TORCH_CHECK(py::isinstance<py::function>(boundMethod));
      auto rcb =
          py::module::import("torch._jit_internal")
              .attr("createResolutionCallbackFromClosure")(unboundMethod);
      return std::make_shared<PythonValue>(boundMethod, rcb, self_);
    }

    // If we reach here, it's because this is a "normal" method that just hasn't
    // been compiled yet (directly exported methods would have been returned by
    // step 1). Just compile it.
    auto stub =
        py::module::import("torch.jit._recursive")
            .attr("compile_unbound_method")(concreteType_, unboundMethod);
    TORCH_INTERNAL_ASSERT(!stub.is_none());
    // Look up the attribute again, it will be available as a compiled method.
    return attr(loc, m, field);
  }

  return nullptr;
}

bool ModuleValue::hasAttr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  return tryGetAttr(loc, m, field) != nullptr;
}

// This method controls how we desugar attribute lookups on ScriptModules.
std::shared_ptr<SugaredValue> ModuleValue::attr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  if (auto attr = tryGetAttr(loc, m, field)) {
    return attr;
  }

  // We don't define this attr. Bailout with a hint to the user.
  std::string hint;
  if (auto failureReason = concreteType_->findFailedAttribute(field)) {
    hint = *failureReason;
  }

  throw ErrorReport(loc)
      << "Module '"
      << concreteType_->getJitType()->expect<ClassType>()->name()->name() << "'"
      << " has no attribute '" << field << "' " << hint;
}

SugaredValuePtr ModuleValue::iter(const SourceRange& loc, Function& m) {
  const auto iterableModuleKind = concreteType_->getIterableModuleKind();
  if (iterableModuleKind == IterableModuleKind::NONE) {
    throw ErrorReport(loc)
        << "Only constant Sequential, ModueList, or ModuleDict can be used as an iterable";
  }

  auto module_dict = getSugaredDict(loc, m);
  if (iterableModuleKind == IterableModuleKind::DICT) {
    return module_dict->keys_;
  } else if (iterableModuleKind == IterableModuleKind::LIST) {
    return module_dict->modules_;
  } else {
    TORCH_INTERNAL_ASSERT(false);
  }
}

std::shared_ptr<SugaredValue> PythonClassValue::attr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  // Resolve values from the Python object first (e.g. for static methods on
  // this type, resolve them as functions)
  auto py_attr = py::getattr(py_type_, field.c_str(), py::none());
  if (!py_attr.is_none()) {
    return toSugaredValue(py_attr, m, loc);
  }

  return ClassValue::attr(loc, m, field);
}

bool PythonClassValue::hasAttr(
    const SourceRange& loc,
    Function& m,
    const std::string& field) {
  try {
    py::getattr(py_type_, field.c_str());
    return true;
  } catch (py::error_already_set& e) {
    return false;
  }
}

void ModuleValue::setAttr(
    const SourceRange& loc,
    Function& m,
    const std::string& field,
    Value* newValue) {
  // Forward to SimpleValue::setAttr
  SimpleValue simple(self_);
  simple.setAttr(loc, m, field, newValue);
}

std::shared_ptr<SugaredValue> BooleanDispatchValue::call(
    const SourceRange& loc,
    Function& caller,
    at::ArrayRef<NamedValue> inputs,
    at::ArrayRef<NamedValue> attributes,
    size_t n_binders) {
  c10::optional<bool> result;
  Graph& graph = *(caller.graph());

  auto index = py::cast<size_t>(dispatched_fn_["index"]);
  auto arg_name = py::str(dispatched_fn_["arg_name"]);

  ErrorReport error(loc);
  if (index < inputs.size()) {
    // Dispatch flag is in arg list
    result = constant_as<bool>(inputs.at(index).value(graph));
    error << "Argument for boolean dispatch at position " << index
          << " was not constant";
  } else if (auto i = findInputWithName(arg_name, attributes)) {
    // Dispatch flag is in kwargs
    result = constant_as<bool>(attributes[*i].value(graph));
    error << "Keyword argument '" << arg_name
          << "' for boolean dispatch at position was not constant";
  } else {
    // Didn't find dispatch flag, so use default value
    result = py::cast<bool>(dispatched_fn_["default"]);
    TORCH_INTERNAL_ASSERT(result);
  }

  if (!result.has_value()) {
    throw error;
  }

  std::shared_ptr<SugaredValue> value;
  if (*result) {
    value = toSugaredValue(dispatched_fn_["if_true"], caller, loc);
  } else {
    value = toSugaredValue(dispatched_fn_["if_false"], caller, loc);
  }
  return value->call(loc, caller, inputs, attributes, n_binders);
}

bool isNamedTupleClass(const py::object& obj) {
  auto tuple_type = reinterpret_cast<PyObject*>(&PyTuple_Type);
  return PyObject_IsSubclass(obj.ptr(), tuple_type) &&
      py::hasattr(obj, "_fields");
}

TypePtr registerNamedTuple(const py::object& obj, const SourceRange& loc) {
  TORCH_INTERNAL_ASSERT(isNamedTupleClass(obj));
  auto qualifiedName = c10::QualifiedName(py::cast<std::string>(
      py::module::import("torch.jit").attr("_qualified_name")(obj)));
  // Currently don't support default values
  if (py::hasattr(obj, "_field_defaults")) {
    auto default_dict = py::cast<std::map<std::string, py::object>>(
        py::getattr(obj, "_field_defaults"));
    if (default_dict.size()) {
      std::string error_msg =
          "Default values are currently not supported"
          " on NamedTuple fields in TorchScript. Fields "
          "with default values: [";
      bool first = true;
      for (const auto& kv : default_dict) {
        if (!first) {
          error_msg += ", ";
        }
        error_msg += kv.first;
      }
      error_msg += "]";
      throw ErrorReport(loc) << error_msg;
    }
  }

  py::object props =
      py::module::import("torch.jit").attr("_get_named_tuple_properties")(obj);
  std::string unqualName;
  std::vector<std::string> fields;
  std::vector<TypePtr> annotations;
  std::tie(unqualName, fields, annotations) = py::cast<
      std::tuple<std::string, decltype(fields), decltype(annotations)>>(props);

  auto tt = TupleType::createNamed(qualifiedName, fields, annotations);
  if (auto type = get_python_cu()->get_type(qualifiedName)) {
    TORCH_CHECK(
        type->isSubtypeOf(tt),
        "Can't to redefine NamedTuple: ",
        tt->repr_str());
    return type;
  }
  get_python_cu()->register_type(tt);
  return tt;
}

std::shared_ptr<SugaredValue> toSugaredValue(
    py::object obj,
    Function& m,
    SourceRange loc,
    bool is_constant) {
  // directly create SimpleValues when possible, because they are first-class
  // and can be re-assigned. Otherwise, this would be invalid:
  // f = python_constant
  // while ...
  //   f = f + 1
  auto& g = *m.graph();
  if (is_constant) {
    if (py::isinstance<py::bool_>(obj)) {
      return toSimple(g.insertConstant(py::cast<bool>(obj), loc));
    } else if (py::isinstance<py::int_>(obj)) {
      return toSimple(g.insertConstant(py::cast<int64_t>(obj), loc));
    } else if (py::isinstance<py::float_>(obj)) {
      return toSimple(g.insertConstant(py::cast<double>(obj), loc));
    } else if (py::isinstance<py::str>(obj)) {
      return toSimple(g.insertConstant(py::cast<std::string>(obj), loc));
    } else if (obj.is(py::none())) {
      return toSimple(g.insertConstant(IValue(), loc));
    } else if (THPDevice_Check(obj.ptr())) {
      auto device = reinterpret_cast<THPDevice*>(obj.ptr());
      return toSimple(g.insertConstant(device->device));
    } else if (THPLayout_Check(obj.ptr())) {
      auto layout = reinterpret_cast<THPLayout*>(obj.ptr());
      const auto v = static_cast<int64_t>(layout->layout);
      return toSimple(g.insertConstant(v, loc));
    } else if (THPMemoryFormat_Check(obj.ptr())) {
      auto memory_format = reinterpret_cast<THPMemoryFormat*>(obj.ptr());
      const auto v = static_cast<int64_t>(memory_format->memory_format);
      return toSimple(g.insertConstant(v, loc));
    } else if (THPDtype_Check(obj.ptr())) {
      auto dtype = reinterpret_cast<THPDtype*>(obj.ptr());
      const auto v = static_cast<int64_t>(dtype->scalar_type);
      return toSimple(g.insertConstant(v, loc));
    } else if (THPQScheme_Check(obj.ptr())) {
      auto qscheme = reinterpret_cast<THPQScheme*>(obj.ptr());
      const auto v = static_cast<uint8_t>(qscheme->qscheme);
      return toSimple(g.insertConstant(v, loc));
    } else if (THPLayout_Check(obj.ptr())) {
      auto layout = reinterpret_cast<THPLayout*>(obj.ptr());
      const auto l = static_cast<int8_t>(layout->layout);
      return toSimple(g.insertConstant(l, loc));
    } else if (py::isinstance<py::tuple>(obj)) {
      py::tuple tup = obj;
      std::vector<Value*> values;
      values.reserve(tup.size());
      for (py::handle t : tup) {
        py::object obj = py::reinterpret_borrow<py::object>(t);
        values.push_back(toSugaredValue(obj, m, loc, true)->asValue(loc, m));
      }
      return toSimple(
          m.graph()->insertNode(m.graph()->createTuple(values))->output());
    }
  }

  if (auto callee = as_function(obj)) {
    return std::make_shared<FunctionValue>(callee->function_);
  } else if (py::isinstance<py::module>(obj)) {
    return std::make_shared<PythonModuleValue>(obj);
  } else if (
      obj.ptr() == py::module::import("torch.jit").attr("_fork").ptr() ||
      obj.ptr() == py::module::import("torch.jit").attr("fork").ptr()) {
    return SpecialFormValue::create(prim::fork);
  } else if (
      obj.ptr() == py::module::import("torch.jit").attr("annotate").ptr()) {
    return SpecialFormValue::create(prim::annotate);
#ifdef USE_DISTRIBUTED
  } else if (
      // RPC module is only avaialble  when build flag "USE_DISTRIBUTED" is on.
      obj.ptr() ==
      py::module::import("torch.distributed.rpc").attr("rpc_async").ptr()) {
    return SpecialFormValue::create(prim::rpc_async);
#endif
  } else if (auto callee = as_module(obj)) {
    throw ErrorReport(loc) << "Cannot call a ScriptModule that is not"
                           << " a submodule of the caller";
  }

  py::object builtin_name =
      py::module::import("torch.jit").attr("_find_builtin")(obj);
  if (!builtin_name.is_none()) {
    return std::make_shared<BuiltinFunction>(
        Symbol::fromQualString(py::str(builtin_name)), c10::nullopt);
  }

  if (py::isinstance<py::function>(obj)) {
    if (typeString(obj) == "builtin_function_or_method") {
      throw ErrorReport(loc) << "Python builtin " << py::str(obj)
                             << " is currently not supported in Torchscript";
    }
  }

  py::object dispatched_fn =
      py::module::import("torch.jit").attr("_try_get_dispatched_fn")(obj);
  if (!dispatched_fn.is_none()) {
    return std::make_shared<BooleanDispatchValue>(std::move(dispatched_fn));
  }

  if (py::isinstance<ScriptClass>(obj)) {
    auto script_class = py::cast<ScriptClass>(obj);
    return std::make_shared<PythonClassValue>(
        script_class.class_type_.type_->expect<ClassType>(), obj);
  }

  if (isNamedTupleClass(obj)) {
    auto tuple_type = registerNamedTuple(obj, loc)->expect<TupleType>();
    return std::make_shared<NamedTupleConstructor>(tuple_type);
  }

  py::bool_ isClass = py::module::import("inspect").attr("isclass")(obj);
  if (py::cast<bool>(isClass)) {
    py::str qualifiedName =
        py::module::import("torch.jit").attr("_qualified_name")(obj);
    auto pyCu = get_python_cu();
    auto qualname = c10::QualifiedName(qualifiedName);
    if (auto classType = pyCu->get_class(qualname)) {
      return std::make_shared<PythonClassValue>(classType, obj);
    } else {
      // If we can't get the source code for the type, it's implemented in C and
      // probably part of the standard library, so give up and leave it as a
      // call to Python
      bool can_compile_class =
          py::cast<bool>(py::module::import("torch._jit_internal")
                             .attr("can_compile_class")(obj));
      if (can_compile_class) {
        // Register class
        auto rcb = py::module::import("torch._jit_internal")
                       .attr("createResolutionCallbackForClassMethods")(obj);
        py::module::import("torch.jit")
            .attr("_recursive_compile_class")(obj, loc);

        // Return class
        auto newClassType = pyCu->get_class(qualname);
        AT_ASSERT(
            newClassType,
            "Class '",
            qualifiedName,
            "' should have been compiled but was not");
        return std::make_shared<PythonClassValue>(newClassType, obj);
      }
    }
  }

  py::bool_ isFunction = py::module::import("inspect").attr("isfunction")(obj);
  if (py::cast<bool>(isFunction)) {
    auto overloads =
        py::module::import("torch.jit").attr("_get_overloads")(obj);
    if (!overloads.is_none()) {
      auto compiled_fns = py::cast<std::vector<StrongFunctionPtr>>(overloads);
      return std::make_shared<FunctionValue>(std::move(compiled_fns));
    }

    auto compiled_fn = py::module::import("torch.jit._recursive")
                           .attr("try_compile_fn")(obj, loc);
    if (auto callee = as_function(compiled_fn)) {
      return std::make_shared<FunctionValue>(*callee);
    }
  }

  py::bool_ isMethod = py::module::import("inspect").attr("ismethod")(obj);
  // methods here have been explicitly annotated to not be compiled,
  // so they do not have the same overload and compile checks as for functions
  if (isFunction || isMethod) {
    auto rcb = py::module::import("torch._jit_internal")
                   .attr("createResolutionCallbackFromClosure")(obj);
    return std::make_shared<PythonValue>(obj, rcb);
  }

  return std::make_shared<PythonValue>(obj);
}
} // namespace jit
} // namespace torch

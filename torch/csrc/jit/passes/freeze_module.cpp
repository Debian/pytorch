#include <torch/csrc/jit/passes/freeze_module.h>
#include <torch/csrc/jit/jit_log.h>

#include <torch/csrc/jit/ir/alias_analysis.h>
#include <torch/csrc/jit/passes/inliner.h>
#include <torch/csrc/jit/runtime/graph_executor_impl.h>

#include <stack>

namespace torch {
namespace jit {

namespace {

class AttributePropagator {
 public:
  AttributePropagator(Module& module, std::vector<std::string>& preservedAttrs)
      : module_(module) {
    // Currently only top level attributes and functions can  be preserved
    // explicitly.
    auto checkName = [this](std::string& name) {
      if (module_.hasattr(name)) {
        insertMutableAttr(name, module_.attr(name), module_._ivalue());
        return true;
      }

      for (auto& fn : module_.type()->methods()) {
        if (fn->name() == name) {
          preservedMethods_.insert(fn);
          return true;
        }
      }
      return false;
    };

    // forward is preserved by default.
    auto method = module_.get_method("forward");
    preservedMethods_.insert(&method.function());

    for (auto name : preservedAttrs) {
      TORCH_CHECK(checkName(name), "Unknown name: " + name);
    }
  }

  void optimizeSubGraphs(
      std::shared_ptr<Graph>& graph,
      void (*func)(std::shared_ptr<Graph>&)) {
    func(graph);
    std::stack<Block*> blocks({graph->block()});
    while (!blocks.empty()) {
      Block* block = blocks.top();
      blocks.pop();
      for (auto n : block->nodes()) {
        for (Block* sub_block : n->blocks()) {
          blocks.push(sub_block);
        }
        if (n->kind() == prim::fork) {
          auto subgraph = n->g(attr::Subgraph);
          optimizeSubGraphs(subgraph, func);
        }
      }
    }
  }

  void run() {
    auto applyInline = [](std::shared_ptr<Graph>& subgraph) {
      Inline(*subgraph);
    };
    auto applyOptimizations = [](std::shared_ptr<Graph>& subgraph) {
      runOptimization(subgraph, /* unroll? */ false);
    };
    for (auto function : preservedMethods_) {
      GRAPH_DEBUG("Analyzing function: " + function->name());
      auto graph = function->graph();
      optimizeSubGraphs(graph, applyInline);
      // Record Attributes that are explicitly set in the module.
      // They cannot be folded.
      recordMutableAttrs(graph);
    }

    for (auto function : preservedMethods_) {
      GRAPH_DEBUG("Propagating function: " + function->name());
      auto graph = function->graph();
      propagateAttributes(graph);
      optimizeSubGraphs(graph, applyOptimizations);
    }
    GRAPH_DEBUG("Cleaning up module");
    cleanupFrozenModule();
  }

 private:
  // findConstantAttr function locates the sub Module where attributes are
  // defined. The algorithm chases getAttr chains to locate the submodules.
  // For example:
  // module M {
  //   attributes {
  //     A = <SubModule at ...>
  //   }
  //   ...
  //   %A = prim::GetAttr[name="A"](%self)
  //   ...
  //   %B = prim::GetAttr[name="B"](%A)
  //   ...
  //   %weight = prim::GetAttr[name="scale"](%B)
  //   ...
  //   submodules {
  //     module SubModule {
  //       attributes {
  //          B = <SubModule2 at ...>
  //       }
  //       submodules {
  //         module SubModule2 {
  //            attributes {
  //               scale = 2
  //            }
  //         }
  //       }
  //     }
  //   }
  //
  // findConstantAttr(%B, "scale", M)  returns true because there are no
  // explicit SetAttr that modifies %B. attrModule points to the module where
  // attribute lives (in this example it is <SubModule2 at ...>).
  //
  // Note inplace mutations to attributes are checked later using alias
  // analysis.
  //
  // We can use a more efficient algorithm to hash each constant GetAttr to its
  // corresponding value. Based on initial test on resnet50 and other torch
  // vision tests. GetAttrs are not too frequent so it is ok to chase GetAttr
  // chain to retrieve their values.
  bool findConstantAttr(
      Value* input,
      std::string& name,
      Module& attrModule,
      std::shared_ptr<Graph>& graph) {
    if (!input->type()->expect<ClassType>()->is_module()) {
      return false;
    }
    Node* node = input->node();
    names_.clear();
    while (!(node->outputs()[0]->type() == graph->inputs()[0]->type())) {
      if (node->kind() == prim::GetAttr) {
        names_.push_front(node->s(attr::name));
        node = node->inputs()[0]->node();
      } else {
        return false;
      }
    }

    for (auto& moduleName : names_) {
      if (preservedAttrs_.count(attrModule.attr(moduleName))) {
        return false;
      }
      attrModule = attrModule.attr(moduleName).toModule();
    }

    auto attr = attrModule.attr(name);
    if (!AliasDb::isMutableType(attr.type())) {
      auto it = preservedScalarAttrs_.find(attrModule._ivalue());
      return it == preservedScalarAttrs_.end() || !it->second.count(name);
    }

    if (preservedAttrs_.count(attr)) {
      return false;
    }
    if (!attr.type()->cast<ClassType>()) {
      for (auto& ivalue : preservedAttrs_) {
        if (!ivalue.isObject() && ivalue.overlaps(attr)) {
          return false;
        }
      }
    }
    return true;
  }

  void insertMutableAttr(
      const std::string& name,
      const IValue& attr,
      const ModulePtr& attrModule) {
    if (AliasDb::isMutableType(attr.type())) {
      preservedAttrs_.insert(attr);
    } else {
      preservedScalarAttrs_[attrModule].insert(name);
    }
  }

  void recordMutableAttrs(std::shared_ptr<Graph>& graph) {
    std::stack<Block*> blocks({graph->block()});
    std::unique_ptr<AliasDb> aliasDb =
        torch::make_unique<AliasDb>(graph, /* isFrozen */ true);
    while (!blocks.empty()) {
      Block* block = blocks.top();
      blocks.pop();
      for (auto n : block->nodes()) {
        for (Block* sub_block : n->blocks()) {
          blocks.push(sub_block);
        }
        if (n->kind() == prim::SetAttr || n->kind() == prim::GetAttr) {
          // TODO: handle interface attributes. For now, Exit if Module uses
          // interface attributes
          if (n->kind() == prim::GetAttr) {
            TORCH_CHECK(
                !n->output()->type()->cast<InterfaceType>(),
                "attempted to freeze a module that uses interface attributes");
          }
          auto name = n->s(attr::name);
          auto attrModule = module_;
          if (!findConstantAttr(n->inputs()[0], name, attrModule, graph)) {
            continue;
          }

          auto attr = attrModule.attr(name);
          if (n->kind() == prim::GetAttr) {
            auto type = n->output()->type();
            // Do not record submodules. Their attributes are tracked
            // individually.
            if (attr.isObject() || !AliasDb::isMutableType(attr.type())) {
              continue;
            }
            usedAttrs_.insert(attr);
          }

          if (n->kind() == prim::SetAttr || aliasDb->hasOutputWriters(n)) {
            GRAPH_DEBUG(
                n->kind() == prim::GetAttr ? "attribute: " + name + " in %" +
                        n->output()->debugName() + " has inplace writer"
                                           : "attribute: " + name + " is set");
            auto mptr = attrModule._ivalue();
            insertMutableAttr(name, attr, mptr);
          }
        } else if (n->kind() == prim::fork) {
          applyToForkSubgraph(
              n,
              graph,
              std::bind(
                  &AttributePropagator::recordMutableAttrs,
                  *this,
                  std::placeholders::_1));
        }
      }
    }
    // FIXME: Current Alias analysis fails to track subvalues.
    // This is not a common scenario, for freezing, detect and error out.
    IValue::HashAliasedIValues seen;
    for (auto& val : usedAttrs_) {
      IValue::HashAliasedIValues subValues;
      val.getSubValues(subValues);
      TORCH_CHECK(
          std::all_of(
              subValues.begin(),
              subValues.end(),
              [&seen](const IValue& v) { return seen.count(v) == 0; }),
          "module contains attributes values that overlaps ",
          val);
      seen.insert(subValues.begin(), subValues.end());
    }
  }

  IValue overrideGradient(IValue attr) {
    if (attr.isTensor()) {
      auto t = attr.toTensor();
      if (t.requires_grad()) {
        t = t.detach();
        t.set_requires_grad(false);
        attr = IValue(t);
      }
    } else if (attr.isTuple()) {
      auto tuple = std::move(attr).toTuple();
      std::vector<IValue>& elems = tuple->elements();
      for (auto& elem : elems) {
        elem = overrideGradient(elem);
      }
      attr = std::move(tuple);

    } else if (attr.isList()) {
      c10::List<IValue> elems = std::move(attr).toList();
      for (size_t i = 0; i < elems.size(); i++) {
        elems.set(i, overrideGradient(elems.extract(i)));
      }
      attr = std::move(elems);
    } else if (attr.isGenericDict()) {
      auto dict = std::move(attr).toGenericDict();
      for (const auto& pair : dict) {
        auto val = pair.value();
        val = overrideGradient(val);
      }
      attr = std::move(dict);
    }

    return attr;
  }

  void propagateAttributes(std::shared_ptr<Graph>& graph) {
    std::unordered_map<ModulePtr, std::unordered_map<std::string, Value*>>
        attrValues;
    auto isEval = !module_.hasattr("training") || !module_.is_training();
    GRAPH_DEBUG("Freezing Module: ", module_.type()->name()->name());
    auto block = graph->block();
    std::stack<Block*> blocks({block});

    Node* m = *block->nodes().begin();
    WithInsertPoint guard(m);
    while (!blocks.empty()) {
      Block* block = blocks.top();
      blocks.pop();
      for (auto it = block->nodes().begin(); it != block->nodes().end();) {
        Node* n = *it;
        it++; // advance iterator bc the current node may be destroyed

        for (Block* sub_block : n->blocks()) {
          blocks.push(sub_block);
        }
        if (n->kind() == prim::GetAttr) {
          auto name = n->s(attr::name);
          auto attrModule = module_;
          auto input = n->inputs()[0];
          if (!findConstantAttr(input, name, attrModule, graph)) {
            GRAPH_DEBUG(
                input->type()->expect<ClassType>()->is_module()
                    ? "attribute: " + name + " is mutable."
                    : "");
            continue;
          }
          TORCH_INTERNAL_ASSERT(attrModule.hasattr(name));
          Value* paramConst = nullptr;
          auto iter = attrValues.find(attrModule._ivalue());
          if (iter != attrValues.end()) {
            auto iter2 = iter->second.find(name);
            if (iter2 != iter->second.end())
              paramConst = iter2->second;
          }
          if (!paramConst) {
            auto attr = attrModule.attr(name);
            if (isEval)
              attr = overrideGradient(attr);
            if (auto attrVal = tryInsertConstant(*graph, attr)) {
              paramConst = *attrVal;
            } else {
              GRAPH_DEBUG(
                  attr.type()->cast<ClassType>() ? "" : "attribute: ",
                  name,
                  " is not materializable.");
              continue;
            }
            std::string fullName("self.");
            for (auto& name : names_) {
              fullName += name + '.';
            }
            fullName += name;
            paramConst->setDebugName(fullName);
            attrValues[attrModule._ivalue()][name] = paramConst;
          }
          GRAPH_UPDATE(
              "Folding GetAttr %",
              n->outputs()[0]->debugName(),
              " with ",
              paramConst->debugName());
          n->outputs().at(0)->replaceAllUsesWith(paramConst);
          n->removeAllInputs();
        } else if (n->kind() == prim::fork) {
          applyToForkSubgraph(
              n,
              graph,
              std::bind(
                  &AttributePropagator::propagateAttributes,
                  *this,
                  std::placeholders::_1));
        }
      }
    }
  }

  void applyToForkSubgraph(
      Node* n,
      std::shared_ptr<Graph>& graph,
      const std::function<void(std::shared_ptr<Graph>&)>& func) {
    TORCH_CHECK(n->kind() == prim::fork);
    auto attrModule = module_;
    auto node = n->inputs()[0]->node();
    // Check if first parameter of fork is a module. This module is used
    // as the base module (similar to 'self' in forward) to resolve GetAttrs.
    if (node->kind() != prim::GetAttr) {
      return;
    }
    auto name = node->s(attr::name);
    auto input = node->inputs()[0];
    if (!findConstantAttr(input, name, attrModule, graph)) {
      // Module needs to be preserved.
      return;
    }
    attrModule = attrModule.attr(name).toModule();
    auto subgraph = n->g(attr::Subgraph);
    std::swap(module_, attrModule);
    func(subgraph);
    module_ = attrModule;
  }

  bool moduleEscapes(Module& subModule, std::shared_ptr<Graph>& graph) {
    for (auto& output : graph->outputs()) {
      if (subModule.type()->isSubtypeOf(output->type())) {
        return true;
      }
    }
    return false;
  }

  // cleanupFrozenModule function cleans up the Frozen module. It performs the
  // following:
  // 1) Remove unused attributes.
  // 2) Remove unreferenced submodules
  // 3) Remove non public unreferenced methods.
  void cleanupFrozenModule() {
    for (auto function : preservedMethods_) {
      auto graph = function->graph();
      recordReferencedAttrs(graph);
      handleSharedClassType(module_, graph);
    }
    removeUnusedAttrs();
  }

  // Prepraring for clean up phase. At this point, record all  subModules that
  // contains mutable attributes.
  void recordReferencedAttrs(std::shared_ptr<Graph>& graph) {
    std::stack<Block*> blocks({graph->block()});
    std::set<ModulePtr> modules({module_._ivalue()});
    while (!blocks.empty()) {
      Block* block = blocks.top();
      blocks.pop();
      for (auto n : block->nodes()) {
        for (Block* subBlock : n->blocks()) {
          blocks.push(subBlock);
        }
        if (n->kind() == prim::GetAttr) {
          auto& name = n->s(attr::name);
          for (auto& mptr : modules) {
            auto module = Module(mptr);
            if (module.type() == n->inputs()[0]->type() &&
                module.hasattr(name)) {
              auto attr = module.attr(name);
              insertMutableAttr(name, attr, mptr);
              if (attr.isModule()) {
                modules.insert(attr.toModule()._ivalue());
              }
              break;
            }
          }
        } else if (n->kind() == prim::fork) {
          applyToForkSubgraph(
              n,
              graph,
              std::bind(
                  &AttributePropagator::recordReferencedAttrs,
                  *this,
                  std::placeholders::_1));
        }
      }
    }
  }

  // This function recursively iterates over submodules to identify
  // for each class type the attribute slots that need to be preserved.
  //
  // Note 'attrsToKeep[type].insert(type->numAttributes())' means all
  // attribute slots of 'type' and its methods are preserved. A submodule is
  // preserved when it escapes (meaning it is returned).
  void handleSharedClassType(Module& module, std::shared_ptr<Graph>& graph) {
    auto type = module.type();
    size_t N = type->numAttributes();
    if (moduleEscapes(module, graph)) {
      // Perserve all its attributes and methods.
      attrsToKeep_[type].insert(N);
      return;
    }
    auto it2 = preservedScalarAttrs_.find(module._ivalue());
    SharedTypeSubModules_[type].insert(module._ivalue());
    attrsToKeep_[type].insert({});
    for (size_t i = 0; i < N; ++i) {
      auto attrTy = type->getAttribute(i);
      auto name = type->getAttributeName(i);
      auto attr = module.attr(name);
      bool isMutable;
      if (AliasDb::isMutableType(attrTy)) {
        isMutable = preservedAttrs_.count(attr);
      } else {
        isMutable =
            it2 != preservedScalarAttrs_.end() && it2->second.count(name);
      }
      if (isMutable) {
        attrsToKeep_[type].insert(i);
        if (attr.isModule()) {
          auto attrModule = attr.toModule();
          handleSharedClassType(attrModule, graph);
        }
      }
    }
  }

  // Remove unused attributes and methods for each sub module of the frozen
  // module. This function iterates over the Calsstypes of its submodule
  // attributes including its own type.
  void removeUnusedAttrs() {
    std::vector<std::string> attrsToRemove;
    std::vector<Function*> funcsToRemove;
    for (auto& it : attrsToKeep_) {
      auto& type = it.first;
      size_t N = type->numAttributes();
      if (it.second.count(N)) {
        continue;
      }
      for (size_t i = 0; i < N; ++i) {
        if (it.second.count(i) == 0) {
          attrsToRemove.push_back(type->getAttributeName(i));
        }
      }
      for (auto& fn : type->methods()) {
        if (preservedMethods_.count(fn) && *type == *module_.type()) {
          continue;
        }
        funcsToRemove.push_back(fn);
      }

      for (auto& name : attrsToRemove) {
        for (auto& val : SharedTypeSubModules_[type]) {
          auto mod = val.toModule();
          mod._ivalue()->unsafeRemoveAttr(name);
        }
        type->unsafeRemoveAttribute(name);
      }
      for (auto fn : funcsToRemove) {
        type->unsafeRemoveMethod(fn->name());
        auto mod = SharedTypeSubModules_[type].begin()->toModule();
        mod._ivalue()->compilation_unit()->unsafeRemoveMethod(fn->qualname());
      }

      attrsToRemove.clear();
      funcsToRemove.clear();
    }
  }

  // Contains attributes that can't be folded or user directs to keep them.
  IValue::HashAliasedIValues preservedAttrs_;
  // Tracked immutable types (Scalars) by their attribute names not
  // IValues.
  std::unordered_map<ModulePtr, std::unordered_set<std::string>>
      preservedScalarAttrs_;

  // Contains user specified methods to be preserved in frozen module.
  std::unordered_set<Function*> preservedMethods_;

  // Track all used attributes ivalues that can be aliased.
  IValue::HashAliasedIValues usedAttrs_;

  // Contains the attribute slots that need to be preserved for each ClassType.
  std::unordered_map<ClassTypePtr, std::unordered_set<size_t>> attrsToKeep_;

  // Contains the sub modules that share the same ClassType.
  std::unordered_map<ClassTypePtr, IValue::HashAliasedIValues>
      SharedTypeSubModules_;

  Module& module_;

  // Contains the attributes names (e.g. {"self", "subModule", "a"}
  std::deque<std::string> names_;
}; // class AttributePropagator
} // namespace

Module freeze_module(
    const Module& module,
    std::vector<std::string> preservedAttrs) {
  // Currently freezing module is supported only in eval mode.
  // If assertion below is commented and module is in training mode then this
  // implementation folds attributes correctly. Tensor attributes with
  // required_grad set are not folded and 'training' attribute is also not
  // folded.
  // TODO: Determine if freezing in training mode is useful and further clarify
  // its semantics.
  TORCH_CHECK(
      !module.hasattr("training") || !module.is_training(),
      "Freezing module in training mode is not yet supported");

  Method method = module.get_method("forward");
  // Check that module does not return itself.
  for (auto& output : method.graph()->outputs()) {
    TORCH_CHECK(
        output->type() != module.type(),
        "attempted to freeze a module that return itself");
  }

  auto moduleClone = module.clone(true);
  AttributePropagator attrPropagator(moduleClone, preservedAttrs);
  attrPropagator.run();
  return moduleClone;
}

} // namespace jit
} // namespace torch

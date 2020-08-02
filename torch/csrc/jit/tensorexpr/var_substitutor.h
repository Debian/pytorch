#pragma once

#include <unordered_map>
#include <vector>

#include <torch/csrc/jit/tensorexpr/ir.h>
#include <torch/csrc/jit/tensorexpr/ir_mutator.h>
#include <torch/csrc/jit/tensorexpr/ir_visitor.h>
#include <torch/csrc/jit/tensorexpr/reduction.h>

namespace torch {
namespace jit {
namespace tensorexpr {

using VarMapping = std::vector<std::pair<const Var*, const Expr*>>;

// Finds all Vars present in a subexpr.
class VarFinder : public IRVisitor {
 public:
  std::set<const Var*> findVars(const Expr* expr) {
    vars_.clear();
    expr->accept(this);
    return vars_;
  }

  void visit(const Var* v) {
    vars_.insert(v);
  }

 private:
  std::set<const Var*> vars_;
};

class VarSubMutator : public IRMutator {
 public:
  VarSubMutator(const VarMapping& var_mapping) {
    for (const auto& entry : var_mapping) {
      const Var* key_var = entry.first;
      const Expr* value = entry.second;
      if (!key_var) {
        throw malformed_input("missing key in VarSubMutator");
      }
      var_mapping_[key_var] = value;
    }
  }

  const Expr* mutate(const Var* var) override {
    auto iter = var_mapping_.find(var);
    if (iter == var_mapping_.end()) {
      return var;
    }
    return iter->second;
  }

  const Expr* mutate(const ReduceOp* var) override {
    auto body = var->body().node()->accept_mutator(this);
    std::vector<const Expr*> new_outer;
    std::vector<const Var*> new_inner;

    for (auto* v : var->output_args()) {
      new_outer.push_back(v->accept_mutator(this));
    }

    for (auto* v : var->reduce_args()) {
      const Expr* e = v->accept_mutator(this);
      if (const Var* new_var = dynamic_cast<const Var*>(e)) {
        new_inner.push_back(new_var);
      } else {
        VarFinder varFinder;
        auto varlist = varFinder.findVars(e);
        new_inner.insert(new_inner.end(), varlist.begin(), varlist.end());
      }
    }

    return new ReduceOp(
        const_cast<Buf*>(var->accumulator()),
        ExprHandle(body),
        var->interaction(),
        new_outer,
        new_inner);
  }

 private:
  std::unordered_map<const Var*, const Expr*> var_mapping_;
};

} // namespace tensorexpr
} // namespace jit
} // namespace torch

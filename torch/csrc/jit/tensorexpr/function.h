#pragma once

#include <functional>
#include <vector>

#include <torch/csrc/jit/tensorexpr/expr.h>
#include <torch/csrc/jit/tensorexpr/ir.h>

namespace torch {
namespace jit {
namespace tensorexpr {

class Function : public KernelScopedObject {
 public:
  Function(
      const std::string& func_name,
      const std::vector<const Expr*>& dims,
      const std::vector<const Var*>& args,
      const Expr* body)
      // TODO: Function should not create buffers, they should be created
      // manually before constructing a function.
      : func_vars_({new Buf(func_name, dims, body->dtype())}),
        dims_(dims),
        args_(args),
        bodies_({body}) {}
  Function(
      const std::vector<std::string>& func_names,
      const std::vector<const Expr*>& dims,
      const std::vector<const Var*>& args,
      const std::vector<const Expr*>& bodies)
      : func_vars_(func_names.size()),
        dims_(dims),
        args_(args),
        bodies_(bodies) {
    for (size_t i = 0; i < func_names.size(); i++) {
      func_vars_[i] = new Buf(func_names[i], dims, bodies[i]->dtype());
    }
  }
  Function(
      const std::string& func_name,
      Buf* func_var,
      const std::vector<const Expr*>& dims,
      const std::vector<const Var*>& args,
      const Expr* body)
      : func_vars_({func_var}), dims_(dims), args_(args), bodies_({body}) {}

  size_t ndim() const {
    return dims_.size();
  }

  const Expr* dim(size_t index) const {
    if (index < 0 || index >= dims_.size()) {
      throw out_of_range_index();
    }

    return dims_[index];
  }
  const std::vector<const Expr*>& dims() const {
    return dims_;
  }

  const Var* arg(size_t index) const {
    if (index < 0 || index >= args_.size()) {
      throw out_of_range_index();
    }

    return args_[index];
  }
  const std::vector<const Var*>& args() const {
    return args_;
  }

  std::vector<const Expr*> bodies() const {
    return bodies_;
  }
  const Expr* body(size_t index) const {
    if (index >= bodies_.size()) {
      throw out_of_range_index();
    }

    return bodies_[index];
  }

  std::vector<const Buf*> func_vars() const {
    return func_vars_;
  }
  const Buf* func_var(size_t index) const {
    if (index >= func_vars_.size()) {
      throw out_of_range_index();
    }
    return func_vars_[index];
  }

  Stmt* ElementStmt(size_t index);

 private:
  std::vector<const Buf*> func_vars_;
  std::vector<const Expr*> dims_;
  std::vector<const Var*> args_;
  std::vector<const Expr*> bodies_;
};

} // namespace tensorexpr
} // namespace jit
} // namespace torch

#include <torch/csrc/jit/tensorexpr/codegen.h>

#include <sstream>

namespace torch {
namespace jit {
namespace tensorexpr {

RegisterCodeGenList::StmtFactoryMethod RegisterCodeGenList::
    FindStmtFactoryMethod(const std::string& name) {
  auto iter = stmt_factory_methods_.find(name);
  if (iter == stmt_factory_methods_.end()) {
    std::ostringstream oss;
    oss << "Invalid stmt codegen name: " << name << ". ";
    oss << "Existing codegen names: [";
    int index = 0;
    for (const auto& entry : stmt_factory_methods_) {
      if (index != 0) {
        oss << ", ";
      }
      oss << entry.first;
      index++;
    }
    oss << "]";
    throw std::runtime_error(oss.str());
  }
  return iter->second;
}

void RegisterCodeGenList::AddStmtFactoryMethod(
    const std::string& name,
    const StmtFactoryMethod& stmt_factory_method) {
  auto insert_ret =
      stmt_factory_methods_.insert(std::make_pair(name, stmt_factory_method));
  if (!insert_ret.second) {
    throw std::runtime_error("Duplicated CodeGen names: " + name);
  }
}

std::unique_ptr<CodeGen> CreateCodeGen(
    const std::string& name,
    Stmt* stmt,
    const std::vector<CodeGen::BufferArg>& params,
    at::Device device) {
  RegisterCodeGenList::StmtFactoryMethod method =
      RegisterCodeGenList::GetInstance().FindStmtFactoryMethod(name);
  return method(stmt, params, device);
}

const Expr* GenericIntrinsicsExpander::mutate(const Intrinsics* v) {
  if (v->op_type() == kSigmoid) {
    auto x = v->param(0)->accept_mutator(this);
    auto one = ExprHandle(getImmediateByType(v->dtype(), 1.0));
    auto zero = ExprHandle(getImmediateByType(v->dtype(), 0.0));
    ExprHandle y = one / (one + exp(zero - ExprHandle(x)));
    return y.node();
  }
  return IRMutator::mutate(v);
}

} // namespace tensorexpr
} // namespace jit
} // namespace torch

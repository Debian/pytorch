#include <torch/csrc/jit/codegen/cuda/ir_iostream.h>
#include <torch/csrc/jit/codegen/cuda/fusion.h>
#include <torch/csrc/jit/codegen/cuda/ir_all_nodes.h>

#include <iostream>

namespace torch {
namespace jit {
namespace fuser {

namespace {
// Make sure we can inline something, before we attempt to.
void check_inlineable(const IRInputOutput* const irio) {
  for (auto inp : irio->inputs())
    TORCH_CHECK(
        inp->isScalar(),
        "Printing inline computations involving values other than scalars is not currently supported.");
  TORCH_CHECK(
      irio->nOutputs() == 1,
      "Cannot print inline computations if there's more than one output.");
  TORCH_CHECK(
      irio->output(0)->isScalar(),
      "Printing inline computations involving values other than scalars is not currently supported.");
}
} // namespace

void IRPrinter::printHeader(Fusion* fusion, const std::string& kernel_name_) {
  os << "__global__ void " << kernel_name_ << "(";

  std::deque<Val*> vals;
  for (decltype(fusion->nInputs()) i{0}; i < fusion->nInputs(); i++)
    vals.push_back(fusion->input(i));
  for (decltype(fusion->nOutputs()) i{0}; i < fusion->nOutputs(); i++)
    vals.push_back(fusion->output(i));

  for (Val* val : vals) {
    switch (val->getValType().value()) {
      case (ValType::TensorView):
        os << "Tensor<" << val->getDataType().value() << ", "
           << TensorDomain::noReductions(
                  static_cast<TensorView*>(val)->getRootDomain())
                  .size()
           << "> T" << val->name();
        break;
      case (ValType::Scalar):
        os << val->getDataType().value() << " " << val;
        break;
      default:
        TORCH_CHECK(
            false,
            "printHeader() found an input to the fusion of unexpected data type.");
    }

    if (val != vals.back())
      os << ", ";
  }

  if (fusion->hasRNG())
    os << ", unsigned long long seed, unsigned long long offset";
  os << "){\n";
  indent_size++;
  if (fusion->hasRNG()) {
    indent();
    os << "int idx = blockIdx.x*blockDim.x + threadIdx.x;\n";
    indent();
    os << "Philox rnd(seed, idx, offset);\n";
  }
}

void IRPrinter::handle(Fusion* fusion) {
  resetIndent();
  for (const Expr* expr : fusion->exprs()) {
    handle(expr);
  }
}

void IRPrinter::handle(const TensorDomain* const td) {
  os << "[ ";
  for (std::vector<const IterDomain*>::size_type i = 0; i < td->nDims(); i++) {
    handle(td->axis(i));
    if (i != td->nDims() - 1)
      os << ", ";
  }
  os << " ]";
}

void IRPrinter::handle(const TensorView* const tv) {
  os << "T" << tv->name();
  handle(tv->domain());

  if (tv->getComputeAtView() != nullptr) {
    os << " compute_at( ";
    os << "T" << tv->getComputeAtView()->name();
    os << ", " << tv->getRelativeComputeAtAxis() << " )";
  }
}

void IRPrinter::handle(const IterDomain* const id) {
  if (id->isReduction())
    os << "r";
  else if (id->isBroadcast())
    os << "b";
  else
    os << "i";
  switch (id->parallel_method()) {
    case (ParallelType::Vectorize):
      os << "V";
      break;
    case (ParallelType::Unroll):
      os << "U";
      break;
    case (ParallelType::Serial):
      os << "S";
      break;
    default:
      os << id->parallel_method();
  }

  os << "{";
  if (!id->start()->isZeroInt()) {
    print_inline(id->start());
    os << " : ";
  }
  print_inline(id->extent());
  os << "}";
  if (id->isRFactorProduct())
    os << "rf";
}

void IRPrinter::handle(const TensorIndex* const ti) {
  os << "T" << ti->view()->name() << "[ ";

  bool first = true;
  for (auto* ind : ti->indices()) {
    if (!first)
      os << " + ";
    print_inline(ind);
    first = false;
  }
  os << " ]";
}

void IRPrinter::handle(const Bool* const b) {
  if (print_inline_ && FusionGuard::getCurFusion()->origin(b) != nullptr) {
    os << "( ";
    handle(FusionGuard::getCurFusion()->origin(b));
    os << " )";
    return;
  }

  if (b->isSymbolic()) {
    os << "b" << b->name();
  } else {
    os << "bool(" << *(b->value()) << ")";
  }
}

void IRPrinter::handle(const Float* const f) {
  if (print_inline_ && FusionGuard::getCurFusion()->origin(f) != nullptr) {
    os << "( ";
    handle(FusionGuard::getCurFusion()->origin(f));
    os << " )";
    return;
  }

  if (f->isSymbolic()) {
    os << "f" << f->name();
  } else {
    os << "float("
       << std::setprecision(
              std::numeric_limits<Float::ScalarType>::max_digits10)
       << *(f->value()) << ")";
  }
}

void IRPrinter::handle(const Half* const h) {
  if (print_inline_ && FusionGuard::getCurFusion()->origin(h) != nullptr) {
    os << "( ";
    handle(FusionGuard::getCurFusion()->origin(h));
    os << " )";
    return;
  }

  if (h->isSymbolic()) {
    os << "h" << h->name();
  } else {
    os << "__float2half(" << *(h->value()) << ")";
  }
}

void IRPrinter::handle(const Int* const i) {
  if (print_inline_ && FusionGuard::getCurFusion()->origin(i) != nullptr) {
    os << "( ";
    handle(FusionGuard::getCurFusion()->origin(i));
    os << " )";
    return;
  }

  if (i->isSymbolic()) {
    os << "i" << i->name();
  } else {
    os << *(i->value());
  }
}

void IRPrinter::handle(const NamedScalar* const i) {
  os << i->name();
}

namespace {

bool isTV(const Val* const val) {
  return (
      val->getValType().value() == ValType::TensorView ||
      val->getValType().value() == ValType::TensorIndex);
}

// Check if we're a TensorView op that we can generate code for.
bool isTVOp(const Expr* const expr) {
  if (expr->nOutputs() == 1 && isTV(expr->output(0)))
    return true;
  return false;
}
} // namespace

void IRPrinter::handle(const UnaryOp* const uop) {
  bool istvop = isTVOp(uop);
  if (!print_inline_) {
    indent();
    os << uop->out();
    if (istvop) {
      os << "\n";
      indent_size++;
      indent();
    }
    os << " = ";
  } else {
    check_inlineable(uop);
  }

  if (auto inline_uop = inline_op_str(uop->getUnaryOpType())) {
    os << inline_uop.value();
    handle(uop->in());
  } else {
    if (uop->getUnaryOpType() == UnaryOpType::Cast) {
      c10::optional<std::string> cast_str = cast_func_str(std::make_pair(
          static_cast<TensorIndex*>(uop->in())->view()->getDataType().value(),
          static_cast<TensorIndex*>(uop->out())
              ->view()
              ->getDataType()
              .value()));
      TORCH_INTERNAL_ASSERT(cast_str != c10::nullopt, "Unsupported Cast");
      os << cast_str.value();
    } else {
      os << uop->getUnaryOpType();
    }
    os << "(";
    if (uop->getUnaryOpType() == UnaryOpType::RandLike)
      os << "rnd";
    else
      handle(uop->in());
    os << ")";
  }

  if (istvop)
    indent_size--;

  if (!print_inline_)
    os << ";\n";
}

void IRPrinter::handle(const BinaryOp* const bop) {
  bool istvop = isTVOp(bop);
  if (!print_inline_) {
    indent();
    os << bop->out();

    // tensor operations tend to be long, break them up into multiple lines
    if (istvop) {
      os << "\n";
      indent_size++;
      indent();
    }

    os << " = ";
  } else {
    check_inlineable(bop);
  }

  if (auto inline_bop = inline_op_str(bop->getBinaryOpType())) {
    handle(bop->lhs());
    if (istvop) {
      os << "\n";
      indent();
    }
    os << " " << inline_bop.value() << " ";
    handle(bop->rhs());
  } else {
    os << bop->getBinaryOpType() << "(";
    handle(bop->lhs());
    if (istvop) {
      os << "\n";
      indent();
    }
    os << ", ";
    handle(bop->rhs());
    os << ")";
  }

  if (istvop)
    indent_size--;

  if (!print_inline_)
    os << ";\n";
}

void IRPrinter::handle(const TernaryOp* const top) {
  bool istvop = isTVOp(top);
  if (!print_inline_) {
    indent();
    os << top->out();

    // tensor operations tend to be long, break them up into multiple lines
    if (istvop) {
      os << "\n";
      indent_size++;
      indent();
    }

    os << " = ";
  } else {
    check_inlineable(top);
  }

  os << top->getTernaryOpType() << "(";
  handle(top->in1());
  if (istvop) {
    os << "\n";
    indent();
  }
  os << ", ";
  handle(top->in2());
  if (istvop) {
    os << "\n";
    indent();
  }
  os << ", ";
  handle(top->in3());
  os << ")";

  if (istvop)
    indent_size--;

  if (!print_inline_)
    os << ";\n";
}

void IRPrinter::handle(const ReductionOp* const rop) {
  // Check if we've lowered yet.

  bool lowered = rop->out()->getValType() == ValType::TensorIndex;

  if (!lowered) {
    os << rop->out() << " = reduction( " << rop->in()
       << ", op = " << rop->getReductionOpType()
       << ", initial value = " << rop->init() << " )\n";
    return;
  }

  TensorIndex* out = static_cast<TensorIndex*>(rop->out());
  auto vec_domain = out->view()->domain()->domain();

  IterDomain *tidx = nullptr, *tidy = nullptr, *tidz = nullptr;
  bool is_thread_reduce = false;
  for (auto id : vec_domain) {
    if (id->isThreadDim() && id->isReduction()) {
      switch (id->parallel_method()) {
        case (ParallelType::TIDz):
          tidz = id;
          break;
        case (ParallelType::TIDy):
          tidy = id;
          break;
        case (ParallelType::TIDx):
          tidx = id;
          break;
        default:
          TORCH_INTERNAL_ASSERT(
              false, "Did not recognize parallel type for reduction.");
      }
      is_thread_reduce = true;
    }
  }

  if (!is_thread_reduce) {
    handle(new BinaryOp(rop->getReductionOpType(), out, out, rop->in()));
    return;
  }
  auto d_type = rop->out()->getDataType().value();
  auto op_type = rop->getReductionOpType();
  indent();
  // Thread all reduce.
  os << "blockReduce< " << (tidx != nullptr ? "true" : "false") << ", "
     << (tidy != nullptr ? "true" : "false") << ", "
     << (tidz != nullptr ? "true" : "false") << " >"
     << " ( ";
  handle(rop->out());
  os << ", ";
  handle(rop->in());
  os << ", ";
  os << "reduction_" << op_type << "_" << d_type;
  os << ");\n";
}

void IRPrinter::handle(const BroadcastOp* const bop) {
  indent();
  handle(bop->out());
  os << "\n";
  indent_size++;
  indent();
  os << " = ";
  handle(bop->in());
  indent_size--;
  os << ";\n";
}

void IRPrinter::handle(const ForLoop* const fl) {
  if (fl->iter_domain()->isThread()) {
    for (auto& expr : fl->constBody().exprs())
      handle(expr);
    return;
  }

  indent();
  os << "for(size_t ";
  handle(fl->index());
  os << " = ";
  print_inline(fl->iter_domain()->start());
  os << "; ";
  handle(fl->index());
  os << " < ";
  print_inline(fl->iter_domain()->extent());
  os << "; ++";
  handle(fl->index());
  os << " ) {\n";
  indent_size++;
  for (auto& expr : fl->constBody().exprs())
    handle(expr);

  indent_size--;
  indent();
  os << "}\n";
}

void IRPrinter::handle(const IfThenElse* const ite) {
  indent();

  // IF
  os << "if ( ";
  print_inline(ite->cond());
  os << " ) { \n";

  indent_size++;
  for (auto& expr : ite->constBody().exprs()) {
    handle(expr);
  }
  indent_size--;

  // ELSE
  if (ite->hasElse()) {
    indent();
    os << "} else { \n";
    indent_size++;
    for (auto& expr : ite->constElseBody().exprs()) {
      handle(expr);
    }
    indent_size--;
  }
  indent();
  os << "}\n";
}

void IRPrinter::handle(const Allocate* const a) {
  indent();
  os << a->buf_type();
  if (a->buffer()->getValType() == ValType::TensorView) {
    os << " T" << a->buffer()->name() << "[";
    print_inline(a->extent());
    os << "];\n";
  } else {
    if (a->extent()->isOneInt()) {
      os << " " << a->buffer() << ";\n";
    } else {
      TORCH_INTERNAL_ASSERT(
          false,
          "Received unexpected allocation: ",
          a->buffer(),
          " with alloc of ",
          a->extent());
    }
  }
}

void IRPrinter::handle(const Split* const s) {
  os << "Split: ";
  handle(s->in());
  os << " by factor " << s->factor() << " -> ";
  handle(s->outer());
  os << ", ";
  handle(s->inner());
  os << "\n";
}

void IRPrinter::handle(const Merge* const m) {
  os << "Merge: ";
  handle(m->outer());
  os << " and ";
  handle(m->inner());
  os << " -> ";
  handle(m->out());
  --indent_size;
  os << "\n";
}

namespace {

struct ReductionOps : OptOutDispatch {
  std::set<std::pair<BinaryOpType, DataType>> rops;
  void handle(ReductionOp* rop) override {
    rops.emplace(std::pair<BinaryOpType, DataType>{
        rop->getReductionOpType(), rop->in()->getDataType().value()});
  }

  using OptOutDispatch::handle;

  static std::set<std::pair<BinaryOpType, DataType>> get(Fusion* fusion) {
    ReductionOps ROPs;
    for (auto expr : fusion->exprs(true)) {
      ROPs.handle(expr);
    }
    return ROPs.rops;
  }
};
} // namespace

void IRPrinter::printReductionOps(Fusion* fusion) {
  auto a = new NamedScalar("a", DataType::Null);
  auto b = new NamedScalar("b", DataType::Null);
  for (auto rop_pair : ReductionOps::get(fusion)) {
    auto op_type = rop_pair.first;
    auto d_type = rop_pair.second;

    indent();
    os << "__device__ void reduction_" << op_type << "_" << d_type << "("
       << d_type << "& a, "
       << "const " << d_type << " b) {\n";
    indent_size++;
    handle(new BinaryOp(op_type, a, a, b));
    indent_size--;
    indent();
    os << "}\n";
  }
}

void IRPrinter::printKernel(
    const std::vector<Expr*>& exprs,
    const std::string& kernel_name) {
  Fusion* fusion = FusionGuard::getCurFusion();

  printReductionOps(fusion);
  printHeader(fusion, kernel_name);
  for (auto* expr : exprs) {
    handle(expr);
  }
  os << "}\n";
}

std::ostream& operator<<(std::ostream& os, const Statement* const stmt) {
  IRPrinter p(os);
  p.handle(stmt);
  return os;
}

std::ostream& operator<<(std::ostream& os, Fusion* f) {
  IRPrinter p(os);
  FusionGuard guard(f);
  p.handle(f);
  return os;
}

std::ostream& operator<<(std::ostream& os, Fusion& f) {
  return os << &f;
}

} // namespace fuser
} // namespace jit
} // namespace torch

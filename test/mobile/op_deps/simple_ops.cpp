#include "simple_ops.h"

#include <iostream>

#include <c10/core/TensorOptions.h>
#include <torch/library.h>

#include "utils.h"

namespace at {

// AA -> BB
Tensor AA_op(const Tensor& self) {
  std::cout << "AA op" << std::endl;
  if (self.ndimension() >= 4) {
    return call_BB_op(self);
  }
  return self;
}

// BB -> AA
Tensor BB_op(const Tensor& self) {
  std::cout << "BB op" << std::endl;
  if (self.ndimension() < 4) {
    return global_helper_call_AA_op_1(self);
  }
  return self;
}

// CC -> (AA -> BB)
Tensor CC_op(const Tensor& self) {
  std::cout << "CC op" << std::endl;
  return global_helper_call_AA_op_2(self);
}

// DD -> (AA -> BB) / (EE -> FF)
Tensor DD_op(const Tensor& self) {
  std::cout << "DD op" << std::endl;
  if (self.ndimension() < 4) {
    return global_helper_call_AA_op_3(self);
  }
  return call_EE_op(self);
}

// EE -> FF
Tensor EE_op(const Tensor& self) {
  std::cout << "EE op" << std::endl;
  if (self.ndimension() >= 4) {
    return call_FF_op(self);
  }
  return self;
}

// FF -> EE
Tensor FF_op(const Tensor& self) {
  std::cout << "FF op" << std::endl;
  if (self.ndimension() < 4) {
    return call_EE_op(self);
  }
  return self;
}

namespace {

// NB: Some of these registrations (AA, EE) are not what you
// actually expect to see in practice, but we cover them here
// as they are technically "valid" API calls and we want to
// make sure the analyzer catches them.  (The analyzer is very
// generic, so actually there isn't any reason it shouldn't work,
// but it's good to test them!)
//
// Additionally, the code in this file is not really runnable; for
// example we are missing schemas for all of the impl registrations
// here.  The analyzer doesn't really care, as it only really
// cares about the name
TORCH_LIBRARY(_test, m) {
  m.def("AA(Tensor self) -> Tensor");
  m.impl("AA", torch::CppFunction::makeUnboxedOnly(AA_op));

  m.def("BB(Tensor self) -> Tensor");
  m.impl("BB", TORCH_FN(BB_op));

  m.def("CC(Tensor self) -> Tensor", TORCH_FN(CC_op));
  m.def("DD", TORCH_FN(DD_op));
}

TORCH_LIBRARY_FRAGMENT_THIS_API_IS_FOR_PER_OP_REGISTRATION_ONLY(_test, m) {
  m.def("EE(Tensor self) -> Tensor");
  m.def("FF(Tensor self) -> Tensor");
  m.def("GG(Tensor self) -> Tensor");
  m.def("HH(Tensor self) -> Tensor");
}

TORCH_LIBRARY_IMPL(_test, CPU, m) {
  m.impl_UNBOXED("EE", EE_op);
  m.impl("FF", torch::CppFunction::makeUnboxedOnly(FF_op));
  m.impl("GG",
    [] (Tensor a) -> Tensor {
      return call_FF_op(a);
    });
  m.impl("HH",
    [] (Tensor a) -> Tensor {
      return a;
    });
}

} // namespace

} // namespace at

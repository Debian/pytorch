#include <torch/custom_class.h>
#include <torch/script.h>

#include <iostream>
#include <string>
#include <vector>

namespace torch {
namespace jit {

namespace {

struct Foo : torch::CustomClassHolder {
  int x, y;
  Foo() : x(0), y(0) {}
  Foo(int x_, int y_) : x(x_), y(y_) {}
  int64_t info() {
    return this->x * this->y;
  }
  int64_t add(int64_t z) {
    return (x + y) * z;
  }
  void increment(int64_t z) {
    this->x += z;
    this->y += z;
  }
  int64_t combine(c10::intrusive_ptr<Foo> b) {
    return this->info() + b->info();
  }
  ~Foo() {
    // std::cout<<"Destroying object with values: "<<x<<' '<<y<<std::endl;
  }
};

struct NoInit : torch::CustomClassHolder {
  int64_t x;
};

template <class T>
struct MyStackClass : torch::CustomClassHolder {
  std::vector<T> stack_;
  MyStackClass(std::vector<T> init) : stack_(init.begin(), init.end()) {}

  void push(T x) {
    stack_.push_back(x);
  }
  T pop() {
    auto val = stack_.back();
    stack_.pop_back();
    return val;
  }

  c10::intrusive_ptr<MyStackClass> clone() const {
    return c10::make_intrusive<MyStackClass>(stack_);
  }

  void merge(const c10::intrusive_ptr<MyStackClass>& c) {
    for (auto& elem : c->stack_) {
      push(elem);
    }
  }

  std::tuple<double, int64_t> return_a_tuple() const {
    return std::make_tuple(1337.0f, 123);
  }
};

struct PickleTester : torch::CustomClassHolder {
  PickleTester(std::vector<int64_t> vals) : vals(std::move(vals)) {}
  std::vector<int64_t> vals;
};

at::Tensor take_an_instance(const c10::intrusive_ptr<PickleTester>& instance) {
  return torch::zeros({instance->vals.back(), 4});
}

TORCH_LIBRARY(_TorchScriptTesting, m) {
  m.class_<Foo>("_Foo")
      .def(torch::init<int64_t, int64_t>())
      // .def(torch::init<>())
      .def("info", &Foo::info)
      .def("increment", &Foo::increment)
      .def("add", &Foo::add)
      .def("combine", &Foo::combine);

  m.class_<NoInit>("_NoInit").def(
      "get_x", [](const c10::intrusive_ptr<NoInit>& self) { return self->x; });

  m.class_<MyStackClass<std::string>>("_StackString")
      .def(torch::init<std::vector<std::string>>())
      .def("push", &MyStackClass<std::string>::push)
      .def("pop", &MyStackClass<std::string>::pop)
      .def("clone", &MyStackClass<std::string>::clone)
      .def("merge", &MyStackClass<std::string>::merge)
      .def_pickle(
          [](const c10::intrusive_ptr<MyStackClass<std::string>>& self) {
            return self->stack_;
          },
          [](std::vector<std::string> state) { // __setstate__
            return c10::make_intrusive<MyStackClass<std::string>>(
                std::vector<std::string>{"i", "was", "deserialized"});
          })
      .def("return_a_tuple", &MyStackClass<std::string>::return_a_tuple)
      .def(
          "top",
          [](const c10::intrusive_ptr<MyStackClass<std::string>>& self)
              -> std::string { return self->stack_.back(); })
      .def(
          "__str__",
          [](const c10::intrusive_ptr<MyStackClass<std::string>>& self) {
            std::stringstream ss;
            ss << "[";
            for (size_t i = 0; i < self->stack_.size(); ++i) {
              ss << self->stack_[i];
              if (i != self->stack_.size() - 1) {
                ss << ", ";
              }
            }
            ss << "]";
            return ss.str();
          });
  // clang-format off
        // The following will fail with a static assert telling you you have to
        // take an intrusive_ptr<MyStackClass> as the first argument.
        // .def("foo", [](int64_t a) -> int64_t{ return 3;});
  // clang-format on

  m.class_<PickleTester>("_PickleTester")
      .def(torch::init<std::vector<int64_t>>())
      .def_pickle(
          [](c10::intrusive_ptr<PickleTester> self) { // __getstate__
            return std::vector<int64_t>{1, 3, 3, 7};
          },
          [](std::vector<int64_t> state) { // __setstate__
            return c10::make_intrusive<PickleTester>(std::move(state));
          })
      .def(
          "top",
          [](const c10::intrusive_ptr<PickleTester>& self) {
            return self->vals.back();
          })
      .def("pop", [](const c10::intrusive_ptr<PickleTester>& self) {
        auto val = self->vals.back();
        self->vals.pop_back();
        return val;
      });

  m.def(
      "take_an_instance(__torch__.torch.classes._TorchScriptTesting._PickleTester x) -> Tensor Y",
      take_an_instance);
  // test that schema inference is ok too
  m.def("take_an_instance_inferred", take_an_instance);
}

} // namespace

void testTorchbindIValueAPI() {
  script::Module m("m");

  // test make_custom_class API
  auto custom_class_obj = make_custom_class<MyStackClass<std::string>>(
      std::vector<std::string>{"foo", "bar"});
  m.define(R"(
    def forward(self, s : __torch__.torch.classes._TorchScriptTesting._StackString):
      return s.pop(), s
  )");

  auto test_with_obj = [&m](IValue obj, std::string expected) {
    auto res = m.run_method("forward", obj);
    auto tup = res.toTuple();
    AT_ASSERT(tup->elements().size() == 2);
    auto str = tup->elements()[0].toStringRef();
    auto other_obj =
        tup->elements()[1].toCustomClass<MyStackClass<std::string>>();
    AT_ASSERT(str == expected);
    auto ref_obj = obj.toCustomClass<MyStackClass<std::string>>();
    AT_ASSERT(other_obj.get() == ref_obj.get());
  };

  test_with_obj(custom_class_obj, "bar");

  // test IValue() API
  auto my_new_stack = c10::make_intrusive<MyStackClass<std::string>>(
      std::vector<std::string>{"baz", "boo"});
  auto new_stack_ivalue = c10::IValue(my_new_stack);

  test_with_obj(new_stack_ivalue, "boo");
}

} // namespace jit
} // namespace torch

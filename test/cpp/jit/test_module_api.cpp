#include <test/cpp/jit/test_base.h>
#include <test/cpp/jit/test_utils.h>
#include <torch/torch.h>

namespace torch {
namespace jit {

void testModuleClone() {
  auto cu = std::make_shared<CompilationUnit>();
  // creating child module
  auto child = ClassType::create("child", cu, true);
  auto attr_name = "attr";
  child->addAttribute(attr_name, IntType::get());
  Module c1(cu, child);
  auto v1 = IValue(2);
  c1.register_attribute(attr_name, IntType::get(), v1, false);
  Module c2(cu, child);
  auto v2 = IValue(3);
  c2.register_attribute(attr_name, IntType::get(), v2, false);

  // attach two child module instance to parent that shares
  // ClassType
  auto parent = ClassType::create("parent", cu, true);
  Module p(cu, parent);
  p.register_attribute("c1", c1.type(), c1._ivalue(), false);
  p.register_attribute("c2", c2.type(), c2._ivalue(), false);

  // clone parent
  Module p2 = p.clone();
  // check the two child module has the same ClassType
  ASSERT_EQ(p2.attr("c1").type(), p2.attr("c2").type());
  // but different instances
  ASSERT_EQ(Module(p2.attr("c1").toObject()).attr(attr_name).toInt(), 2);
  ASSERT_EQ(Module(p2.attr("c2").toObject()).attr(attr_name).toInt(), 3);
}

void testModuleCopy() {
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  auto attr_name = "attr";
  cls->addAttribute(attr_name, IntType::get());
  Module m(cu, cls);
  auto v = IValue(2);
  m.register_attribute(attr_name, IntType::get(), v, false);

  Module m2 = m.clone();
  Module m3 = m.copy();

  // Make sure copy works
  ASSERT_EQ(m2.attr(attr_name).toInt(), 2);
  ASSERT_EQ(m3.attr(attr_name).toInt(), 2);

  // clone will copy both type and data, therefore we'll have a
  // different type
  ASSERT_NE(m.type(), m2.type());
  // copy only copies data, type is shared
  ASSERT_EQ(m.type(), m3.type());

  // change value of copied instance
  m3.register_attribute(attr_name, IntType::get(), IValue(3), false);
  // Verify value of original instance doesn't change
  ASSERT_EQ(m2.attr(attr_name).toInt(), 2);
  ASSERT_EQ(m3.attr(attr_name).toInt(), 3);
}

void testModuleDeepcopy() {
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  auto str_attr = "str_attr";
  auto int_attr = "int_attr";
  auto tensor_attr = "tensor_attr";
  auto tensor_list_attr = "tensor_list_attr";
  cls->addAttribute(int_attr, IntType::get());
  cls->addAttribute(str_attr, StringType::get());
  cls->addAttribute(tensor_attr, TensorType::get());
  cls->addAttribute(tensor_list_attr, ListType::ofTensors());
  Module m(cu, cls);
  c10::List<at::Tensor> list({at::rand(5), at::rand(5)});
  m.setattr(int_attr, IValue(2));
  m.setattr(str_attr, IValue("str"));
  m.setattr(tensor_attr, at::randn(5));
  m.setattr(tensor_list_attr, list);

  Module m2 = m.deepcopy();
  Module m3 = m.copy();
  // Make sure copy works
  ASSERT_EQ(m2.attr(int_attr).toInt(), 2);
  ASSERT_EQ(m3.attr(int_attr).toInt(), 2);

  // Test overlaps
  ASSERT_TRUE(!IValue(m2._ivalue()).overlaps(IValue(m._ivalue())));
  ASSERT_TRUE(IValue(m3._ivalue()).overlaps(IValue(m._ivalue())));

  // Both deepcopy and copy will preserve the type
  ASSERT_EQ(m.type(), m2.type());
  ASSERT_EQ(m.type(), m3.type());

  // change int value of copied instances
  m2.setattr(int_attr, IValue(3));
  m3.setattr(int_attr, IValue(4));

  // Verify value of original instance doesn't change
  ASSERT_EQ(m.attr(int_attr).toInt(), 2);
  ASSERT_EQ(m2.attr(int_attr).toInt(), 3);
  ASSERT_EQ(m3.attr(int_attr).toInt(), 4);

  // change Tensor value of copied instances
  at::Tensor t1 = m.attr(tensor_attr).toTensor();
  at::Tensor t2 =
      m2.attr(tensor_attr).toTensor(); // deepcopy will copy the Tensor
  at::Tensor t3 =
      m3.attr(tensor_attr).toTensor(); // copy will not copy the Tensor
  // check copy works
  ASSERT_TRUE(t1.equal(t2));
  ASSERT_TRUE(t1.equal(t3));

  // zero out t1
  t1.zero_();
  // check that t2 is not affected because it is a deep copy
  ASSERT_TRUE(!t1.equal(t2));
  // check that t3 is the same as t1 since it is a shallow copy
  ASSERT_TRUE(t1.equal(t3));
}

void testModuleDeepcopyString() {
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  auto attr1 = "attr1";
  cls->addAttribute(attr1, StringType::get());
  std::string str = "str";
  Module m(cu, cls);
  m.setattr(attr1, str);
  auto copied = m.deepcopy();
  auto original_str = str;
  ASSERT_EQ(copied.attr(attr1).toString()->string(), original_str);
  // check string mutation is not reflected in the copied module
  str += "str";
  ASSERT_EQ(copied.attr(attr1).toString()->string(), original_str);
}

void testModuleDeepcopyAliasing() {
  // check deepcopy preserves aliasing
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  auto attr1 = "attr1";
  auto attr2 = "attr2";
  auto attr3 = "attr3";
  auto attr4 = "attr4";
  cls->addAttribute(attr1, ListType::ofTensors());
  cls->addAttribute(attr2, ListType::ofTensors());
  cls->addAttribute(attr3, TensorType::get());
  cls->addAttribute(attr4, TensorType::get());
  Module m(cu, cls);
  auto t1 = at::rand(5);
  auto t2 = at::rand(5);
  auto t3 = at::rand(5);
  auto t4 = at::rand({5, 2});
  c10::List<at::Tensor> list1({t1, t2});
  c10::List<at::Tensor> list2({t1, t3});
  // first element of attr1 and attr2 are aliased
  m.setattr(attr1, list1);
  m.setattr(attr2, list2);
  m.setattr(attr3, t4);
  m.setattr(attr4, t4.view(-1));

  auto copied = m.deepcopy();
  // test tensor aliasing
  auto copied_attr1_t1 = copied.attr(attr1).toList().get(0);
  auto copied_attr2_t1 = copied.attr(attr2).toList().get(0);
  ASSERT_TRUE(copied_attr1_t1.isAliasOf(copied_attr2_t1));

  // test aliasing from view
  auto copied_attr3 = copied.attr(attr3);
  auto copied_attr4 = copied.attr(attr3);
  ASSERT_TRUE(copied_attr3.isAliasOf(copied_attr4));
}

void testModuleConstant() {
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  auto attr_name = "attr";
  auto const_name = "const";
  cls->addAttribute(attr_name, IntType::get());
  cls->addConstant(const_name, IValue(3));
  Module m(cu, cls);
  auto v = IValue(2);
  m.register_attribute(attr_name, IntType::get(), v, false);
  ASSERT_TRUE(m.hasattr(attr_name));
  ASSERT_TRUE(m.hasattr(const_name));
  ASSERT_EQ(m.attr(attr_name).toInt(), 2);
  ASSERT_EQ(m.attr(const_name).toInt(), 3);
}

void testModuleParameter() {
  auto cu = std::make_shared<CompilationUnit>();
  auto cls = ClassType::create("foo.bar", cu, true);
  Module m(cu, cls);
  // Tensor parameter
  m.register_parameter(
      "tensor_param", at::empty({3}, at::kFloat), /* is_buffer */ false);
  // None parameter
  m.register_attribute(
      "none_param", NoneType::get(), IValue(), /* is_param */ true);
  m.register_attribute(
      "none_param2", NoneType::get(), IValue(), /* is_param */ true);
  auto param_list = m.parameters();
  ASSERT_EQ(param_list.size(), 1);
  ASSERT_TRUE(m.hasattr("tensor_param"));
  ASSERT_TRUE(m.hasattr("none_param"));
  ASSERT_TRUE(m.hasattr("none_param2"));
}

} // namespace jit
} // namespace torch

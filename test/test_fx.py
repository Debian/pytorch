import torch
import unittest
import operator
import numbers
import pickle
import copy
from pathlib import Path
from torch._fx import symbolic_trace, Proxy, Node, GraphModule, Tracer, Graph
from torch._fx.experimental import GraphManipulation

from torch._fx.proxy import TraceError

from fx.quantization import Quantizer

from typing import Any, Callable, Dict, Optional, Tuple, Union
from torch.testing._internal.common_utils import run_tests, TEST_WITH_ROCM, IS_WINDOWS, IS_SANDCASTLE, IS_MACOS
from torch.testing._internal.jit_utils import JitTestCase

try:
    from torchvision.models import resnet18
    HAS_TORCHVISION = True
except ImportError:
    HAS_TORCHVISION = False
skipIfNoTorchVision = unittest.skipIf(not HAS_TORCHVISION, "no torchvision")

class SimpleTest(torch.nn.Module):
    def forward(self, x):
        return torch.relu(x + 3.0)

class TestFX(JitTestCase):
    def checkGraphModule(self, m: torch.nn.Module, args, kwargs=None):
        """Check that an nn.Module's results match the GraphModule version
        for a given set of args/kwargs.
        """
        kwargs = kwargs if kwargs else {}
        ref_outs = m(*args, **kwargs)
        gm = symbolic_trace(m)
        gm.graph.lint(gm)
        test_outs = gm(*args, **kwargs)
        self.assertEqual(ref_outs, test_outs)

    def test_graph_module(self):
        class MySub(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.w = torch.nn.Parameter(torch.rand(4, 3))

            def forward(self, x):
                return self.w + x

        class MyModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.lin = torch.nn.Linear(4, 3)
                self.sub_mod = MySub()
                self.w = torch.nn.Parameter(torch.rand(3))

            def forward(self, A, B, c):
                t = torch.sigmoid(A) + self.lin(c)
                return self.sub_mod(t.data + self.w + t + 1 - A + B // A + -A + A.add(B, alpha=3))

        m = MyModule()
        gm = symbolic_trace(m)

        ms = torch.jit.script(gm)

        class M2(torch.nn.Module):
            def forward(self, A):
                m, idx = torch.max(A, 0)
                return m + 1, idx + 1

        m2 = M2()
        gm2 = symbolic_trace(m2)

        class T(torch.nn.Module):

            def forward(self, A, b=4, *args, c=5, **kwargs):
                x = A + 1 + args[0] + kwargs['3']
                return x

        t = T()
        symbolic_trace(t)

    def test_args_kwargs(self):
        class T(torch.nn.Module):
            def forward(self, *args, **kwargs):
                x = args[0] + kwargs['foo']
                return x

        t = T()
        self.checkGraphModule(t, (torch.rand(1), torch.rand(1)), {'foo': torch.rand(1)})

    def test_fx_shifts(self):
        class MyModule(torch.nn.Module):
            def forward(self, x):
                return x << 3, x >> 3

        input = torch.LongTensor(10).random_(0, 1024)

        m = MyModule()
        self.checkGraphModule(m, (input,))

    def test_dict(self):
        class MyDictMod(torch.nn.Module):
            def forward(self, d):
                return d['3'].relu(), {'4' : d['3'].neg()}

        input_dict = {'3': torch.rand(3, 4)}
        m = MyDictMod()

        self.checkGraphModule(m, (input_dict,))

    def test_disallow_override(self):
        # Custom delegate to disallow in-place tensor operations
        class NoMutableCallTracer(Tracer):
            def create_node(self, kind : str, target : Union[str, Callable],
                            args : Tuple[Any], kwargs : Dict[str, Any], name : Optional[str] = None) -> Node:
                name = target if isinstance(target, str) else torch.typename(target)
                if name[-1] == '_':
                    raise RuntimeError('In-place operations are not supported')
                return super().create_node(kind, target, args, kwargs, name)

        # Test method
        class MyInplaceMod(torch.nn.Module):
            def forward(self, x):
                x.add_(3.0)
                return x

        m = MyInplaceMod()

        with self.assertRaisesRegex(RuntimeError, 'In-place operations'):
            NoMutableCallTracer().trace(m)

        # Test free function
        class MyInplaceMod2(torch.nn.Module):
            def forward(self, x):
                torch.log_(x)
                return x
        m2 = MyInplaceMod2()
        with self.assertRaisesRegex(RuntimeError, 'In-place operations'):
            NoMutableCallTracer().trace(m2)

        # Test symbolic node as an arg
        class MyInplaceMod3(torch.nn.Module):
            def forward(self, x):
                y = torch.ones(3, 4)
                y.add_(x)
                return x
        m3 = MyInplaceMod3()
        with self.assertRaisesRegex(RuntimeError, 'In-place operations'):
            NoMutableCallTracer().trace(m3)

    def test_leaf_module(self):
        # Custom delegate to make it so that there are no leaf modules, everything
        # should get traced through
        class NoLeafModulesTracer(Tracer):
            def is_leaf_module(self, m, qualname):
                return False

        class MyReluMod(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.relu = torch.nn.ReLU()

            def forward(self, x):
                return self.relu(x)

        mrm = MyReluMod()
        sym = NoLeafModulesTracer().trace(mrm)
        for node in sym.graph.nodes:
            self.assertNotEqual(node.op, 'call_module')
        sym.graph.lint(sym)

    def test_graph_edit_with_proxy(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                return a + b
        m = M()
        g = symbolic_trace(m).graph
        new_g = torch._fx.Graph()
        new_g.graph_copy(g)
        t = Proxy(new_g.nodes[-1])
        # test that we can use proxy objects to generate more graph code later for things that do not need to work with modules.
        new_g.output((t + t).node)
        gm = GraphModule(m, new_g)
        gm.graph.lint(gm)
        self.assertEqual(gm(3, 4), 14)

    def test_graph_unique_names(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                return a + b
        m = M()
        g = symbolic_trace(m).graph
        new_g = torch._fx.Graph()
        new_g.graph_copy(g)
        t = Proxy(new_g.nodes[-1])
        # test that we can use proxy objects to generate more graph code later for things that do not need to work with modules.
        new_g.output((t + t).node)
        gm = GraphModule(m, new_g)
        seen_names : Set[str] = set()
        for node in gm.graph.nodes:
            assert node.name not in seen_names
            seen_names.add(node.name)

    def test_graph_unique_names_manual(self):
        graph : torch._fx.Graph = torch._fx.Graph()
        a : torch._fx.Node = graph.create_node('placeholder', 'x')
        b : torch._fx.Node = graph.create_node('call_module', 'linear_mod', args=(a,), name='foo_1_1')
        c : torch._fx.Node = graph.create_node('get_attr', 'y_attr', name='foo_1')
        d : torch._fx.Node = graph.create_node('call_function', operator.add, args=(b, c))
        graph.output(d)
        graph2 = torch._fx.Graph()
        graph2.graph_copy(graph)
        seen_names : Set[str] = set()
        for node in graph2.nodes:
            assert node.name not in seen_names
            seen_names.add(node.name)

    @skipIfNoTorchVision
    def test_resnet(self):
        resnet = resnet18()
        resnet.train()

        res_graph = symbolic_trace(resnet)
        res_script = torch.jit.script(res_graph)

        ip = torch.rand(1, 3, 224, 224)

        a = resnet(ip)
        b = res_graph(ip)
        c = res_script(ip)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

        quantizer = Quantizer(res_graph)

        for i in range(10):
            quantizer.observe((torch.rand(1, 3, 224, 224),))

        qgraph = quantizer.quantize()
        qgraph.graph.lint(qgraph)
        qgraph_script = torch.jit.script(qgraph)

        d = qgraph(ip)
        e = qgraph_script(ip)

        assert (a - d).abs().max() < 2
        self.assertEqual(d, e)

    def test_unpack(self):
        class M(torch.nn.Module):
            def forward(self, a, b):
                c, d = a
                return c + d + b

        a = (torch.rand(1), torch.rand(1))
        b = torch.rand(1)
        m = M()
        self.checkGraphModule(m, (a, b))

    def test_native_callable(self):
        if TEST_WITH_ROCM or IS_SANDCASTLE or IS_WINDOWS or IS_MACOS:
            raise unittest.SkipTest("non-portable load_library call used in test")
        torch_root = Path(__file__).resolve().parent.parent
        p = torch_root / 'build' / 'lib' / 'libtorchbind_test.so'
        torch.ops.load_library(str(p))
        # This test exercises the case where we use FX to translate from Python
        # code to some native callable object
        #
        # For the purposes of testing, we use ElementwiseInterpreter defined
        # in test_custom_class.cpp.
        #
        # We test that we can
        # 1) Construct a native callable from FX IR
        # 2) Construct a drop-in replacement module that delegates to the
        #    native callable rather than the original code
        # 3) Run both the original code and native callable wrapper with
        #    equivalent results
        # 4) TorchScript compile the native callable wrapper and confirm
        #    equivalent results with the reference
        # 5) TorchScript serialize and deserialize the native callable
        #    and confirm equivalent results with the reference

        # We use this simple Module as a reference computation
        class MySimpleMod(torch.nn.Module):
            def forward(self, x):
                return 3.0 * x + x

        msm = MySimpleMod()

        # This is what a lowering pass might look like: a function that takes
        # a valid nn.Module, symbolically traces it, lowers the Module to some
        # representation, and wraps that representation up into another
        # nn.Module instance that handles dispatch to the compiled/lowered code.
        def lower_to_elementwise_interpreter(orig_mod : torch.nn.Module) -> torch.nn.Module:
            # ===== Stage 1: Symbolic trace the module =====
            mod = symbolic_trace(orig_mod)

            # ===== Stage 2: Lower GraphModule representation to the C++
            #       interpreter's instruction format ======
            instructions = []
            constant_idx = 0
            constants = {}
            fn_input_names = []

            target_to_name = {
                operator.add : "add",
                operator.mul : "mul"
            }

            # For each instruction, create a triple
            # (instruction_name : str, inputs : List[str], output : str)
            # to feed into the C++ interpreter
            for n in mod.graph.nodes:
                target, args, out_name = n.target, n.args, n.name
                assert len(n.kwargs) == 0, "kwargs currently not supported"

                if n.op == 'placeholder':
                    # Placeholders specify function argument names. Save these
                    # for later when we generate the wrapper GraphModule
                    fn_input_names.append(target)
                elif n.op == 'call_function':
                    assert target in target_to_name, "Unsupported call target " + target
                    arg_names = []
                    for arg in args:
                        if not isinstance(arg, Node):
                            # Pull out constants. These constants will later be
                            # fed to the interpreter C++ object via add_constant()
                            arg_name = f'constant_{constant_idx}'
                            constants[arg_name] = torch.Tensor(
                                [arg] if isinstance(arg, numbers.Number) else arg)
                            arg_names.append(arg_name)
                            constant_idx += 1
                        else:
                            arg_names.append(arg.name)
                    instructions.append((target_to_name[target], arg_names, out_name))

                else:
                    raise RuntimeError('Unsupported opcode' + n.op)

            interpreter = torch.classes._TorchScriptTesting._ElementwiseInterpreter()
            # Load constants
            for k, v in constants.items():
                interpreter.add_constant(k, v)
            # Specify names for positional input arguments
            interpreter.set_input_names(fn_input_names)
            # Load instructions
            interpreter.set_instructions(instructions)
            # Specify name for single output
            interpreter.set_output_name(mod.graph.result.name)

            # ===== Stage 3: Create a wrapper GraphModule around the interpreter =====
            class WrapperModule(torch.nn.Module):
                def __init__(self, interpreter):
                    super().__init__()
                    self.interpreter = interpreter

            wrapper = WrapperModule(interpreter)

            # Create a graph that: 1) Takes function arguments 2) Invokes the interpreter
            # 3) Returns the speficied return value

            # FIXME: The following code could be greatly simplified by symbolic_trace'ing
            # the wrapper with a Tracer that considers the Wrapper instance a root
            # module, however, I can't get `__call__` exposed on TorchBind classes
            # without it messing up Python `hasattr` for some reason. More digging
            # into CPython's implementation of hasattr is probably in order...

            graph = torch._fx.Graph()
            # Add placeholders for fn inputs
            placeholder_nodes = []
            for name in fn_input_names:
                placeholder_nodes.append(graph.create_node('placeholder', name))

            # Get the interpreter object
            interpreter_node = graph.create_node('get_attr', 'interpreter')

            # Add a node to call the interpreter instance
            output_node = graph.create_node(
                op='call_method', target='__call__', args=(interpreter_node, placeholder_nodes))

            # Register output
            graph.output(output_node)

            graph.lint(wrapper)

            # Return final GraphModule!!!
            return GraphModule(wrapper, graph)


        # Lower GraphModule to C++ interpreter
        lowered = lower_to_elementwise_interpreter(msm)

        # Compare correctness with original module
        x = torch.rand(3, 4)
        ref_out = msm(x)
        test_out = lowered(x)
        torch.testing.assert_allclose(test_out, ref_out)

        # Test TorchScript compilation
        scripted_lowered = torch.jit.script(lowered)
        script_out = scripted_lowered(x)
        torch.testing.assert_allclose(script_out, ref_out)

        # Test TorchScript ser/de
        import_copy = self.getExportImportCopy(scripted_lowered)
        imported_out = import_copy(x)
        torch.testing.assert_allclose(imported_out, ref_out)

    def test_reserved_getattr(self):
        """Ensure that we do not name any nodes with a reserved builtin like `getattr`"""
        class M(torch.nn.Module):
            def forward(self, a):
                return a.foo.bar.baz

        m = M()
        m_g = symbolic_trace(m)
        m_g.graph.lint(m_g)
        for node in m_g.graph.nodes:
            self.assertTrue(node.name != "getattr")

    def test_node_tagging(self):
        class TaggingTracer(Tracer):
            def create_node(self, kind : str, target : Union[str, Callable],
                            args : Tuple[Any], kwargs : Dict[str, Any], name : Optional[str] = None) -> Node:
                n = super().create_node(kind, target, args, kwargs, name)
                n.tag = 'foo'
                return n

        class M(torch.nn.Module):
            def forward(self, a, b):
                return a + b

        m = M()
        g = TaggingTracer().trace(m).graph
        g.lint(m)
        for n in g.nodes:
            self.assertTrue(hasattr(n, 'tag'))
            self.assertEqual(n.tag, 'foo')

    def test_tensor_attribute(self):
        class TensorAttribute(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.tensor = torch.rand(3, 4)

            def forward(self, x):
                return torch.nn.functional.linear(x, self.tensor)

        ta = TensorAttribute()
        traced = symbolic_trace(ta)
        traced(torch.rand(4, 4))

        class WrapperForQualname(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.ta = TensorAttribute()

            def forward(self, x):
                return torch.nn.functional.linear(x, self.ta.tensor)

        wfq = WrapperForQualname()
        traced2 = symbolic_trace(wfq)
        traced2.graph.lint(traced2)
        traced2(torch.rand(4, 4))

    def test_symbolic_trace_sequential(self):
        class Simple(torch.nn.Module):
            def forward(self, x):
                return torch.neg(x)

        seq = torch.nn.Sequential(
            Simple(),
            Simple(),
            Simple()
        )
        traced = symbolic_trace(seq)
        traced.graph.lint(traced)
        x = torch.rand(3, 4)
        self.assertEqual(traced(x), seq(x))

    def test_tensor_constant(self):
        class ConstTensor(torch.nn.Module):
            def forward(self, x):
                return torch.nn.functional.linear(x, torch.zeros(3, 4))

        ct = ConstTensor()
        traced = symbolic_trace(ct)
        traced.graph.lint(traced)
        traced(torch.rand(4, 4))

    def test_pickle_graphmodule(self):
        class Nested(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.st = torch.nn.Linear(4, 4)

            def forward(self, x):
                return self.st(x)

        n = Nested()
        traced = symbolic_trace(n)
        traced.graph.lint(traced)
        pickled = pickle.dumps(traced)
        loaded = pickle.loads(pickled)
        loaded.graph.lint(loaded)
        x = torch.rand(3, 4)
        self.assertEqual(loaded(x), traced(x))

    def test_deepcopy_graphmodule_with_transform(self):
        st = SimpleTest()
        traced = symbolic_trace(st)
        traced.graph.lint(traced)

        def transform(traced):
            new_graph = torch._fx.Graph()
            new_graph.graph_copy(traced.graph)
            relu_out = new_graph.create_node(
                op='call_method', target='neg', args=(new_graph.nodes[-1],), kwargs={})
            new_graph.output(relu_out)
            return GraphModule(traced, new_graph)
        transformed = transform(traced)
        transformed.graph.lint(transformed)
        copied = copy.deepcopy(transformed)
        self.assertNotEqual(id(type(transformed)), id(type(copied)))
        x = torch.randn(3, 4)
        self.assertEqual(copied(x), transformed(x))

    def test_deepcopy_with_submods_params(self):
        class Bar(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 4))

            def forward(self, x):
                return torch.relu(x) + self.param

        class Baz(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.param = torch.nn.Parameter(torch.rand(3, 4))
                self.bar = Bar()

            def forward(self, x):
                return self.bar(x) - self.param

        baz = Baz()
        traced = symbolic_trace(baz)
        traced.graph.lint(traced)
        copied = copy.deepcopy(traced)
        copied.graph.lint(copied)

    def test_unpack_list_better_error(self):
        class SomeArgs(torch.nn.Module):
            def forward(self, a, b):
                return torch.rand(3, 4)

        class UnpacksList(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.sa = SomeArgs()

            def forward(self, x : list):
                return self.sa(*x)

        ul = UnpacksList()
        with self.assertRaisesRegex(TraceError, 'Proxy object cannot be unpacked as function argument'):
            symbolic_trace(ul)

    def test_unpack_dict_better_error(self):
        class SomeKwargs(torch.nn.Module):
            def forward(self, x=3, y=4):
                return torch.rand(3, 4)

        class UnpacksDict(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.sk = SomeKwargs()

            def forward(self, x : dict):
                return self.sk(**x)

        ud = UnpacksDict()
        with self.assertRaisesRegex(TraceError, 'Proxy object cannot be unpacked as function argument'):
            symbolic_trace(ud)

    def test_torch_custom_ops(self):
        class M(torch.nn.Module):
            def forward(self, a):
                b = torch.ops.aten.sigmoid(a)
                c = torch.ops.aten.cat([a, b])
                return torch.ops.aten.cat((c, c))
        m = M()
        input = torch.randn(3)
        ref_out = m(input)
        gm = symbolic_trace(m)
        gm.graph.lint(gm)
        out = gm(input)
        self.assertEqual(out, ref_out)

    def test_replace_target_nodes_with(self):
        class testModule(torch.nn.Module):
            def forward(self, a, b):
                return a + b
        m = testModule()
        traced = symbolic_trace(m)
        input1 = torch.randn(1)
        input2 = torch.randn(1)
        assert (input1 + input2) == traced(input1, input2)
        GraphManipulation.replace_target_nodes_with(
            fx_module=traced,
            old_op="call_function",
            old_target=operator.add,
            new_op="call_function",
            new_target=operator.mul,
        )
        assert (input1 * input2) == traced(input1, input2)

    def test_pretty_print(self):
        st = SimpleTest()
        traced = symbolic_trace(st)
        traced.graph.lint(traced)
        printed = str(traced)
        assert 'GraphModuleImpl()' in printed
        assert 'torch.relu' in printed

    def test_pretty_print_graph(self):
        class KwargPrintTest(torch.nn.Module):
            def forward(self, x):
                return torch.squeeze(x + 3.0, dim=2)
        st = KwargPrintTest()
        traced = symbolic_trace(st)
        traced.graph.lint(traced)
        stringed = str(traced.graph)
        for s in ['args', 'kwargs', 'uses']:
            assert s in stringed

    def test_graph_fns(self):
        g = Graph()
        a = g.placeholder('a')
        b = g.call_module('linear', (a,))
        c = g.get_attr('bias')
        d = g.call_method('add', (b, c))
        e = g.call_function(torch.sin, (d,))
        g.output(e)
        mod = torch.nn.Module()
        mod.linear = torch.nn.Linear(3, 4)
        mod.bias = torch.rand(4)
        gm = GraphModule(mod, g)
        gm.graph.lint(gm)
        input = torch.rand(3)
        r = gm(input)
        ref = torch.sin(mod.linear(input) + mod.bias)
        self.assertEqual(r, ref)

    def test_construct_root_dict(self):
        graph : torch._fx.Graph = torch._fx.Graph()
        a : torch._fx.Node = graph.create_node('placeholder', 'x')
        b : torch._fx.Node = graph.create_node('call_module', 'foo.bar.baz', args=(a,))
        c : torch._fx.Node = graph.create_node('get_attr', 'zip.zap.zam')
        d : torch._fx.Node = graph.create_node('call_function', operator.add, args=(b, c))
        graph.output(d)

        linear_mod : torch.nn.Module = torch.nn.Linear(3, 4)
        add_param : torch.Tensor = torch.rand(3, 4)
        gm : torch._fx.GraphModule = torch._fx.GraphModule(
            {'foo.bar.baz': linear_mod, 'zip.zap.zam' : add_param}, graph)
        gm.graph.lint(gm)

        assert 'self.foo.bar.baz' in gm.code

        x : torch.Tensor = torch.rand(3, 3)
        out : torch.Tensor = gm(x)
        ref_out : torch.Tensor = linear_mod(x) + add_param
        self.assertEqual(out, ref_out)

    def test_symbolic_trace_assert(self):
        message = "assert_foobar"

        class AssertsTensorShape(torch.nn.Module):
            def forward(self, x):
                torch.Assert(x.shape[1] > 4, message)
                return x

        m = AssertsTensorShape()
        # verify traceability
        traced = symbolic_trace(m)
        # verify assertion on traced model works correctly at runtime
        traced(torch.rand(4, 5))
        with self.assertRaisesRegex(AssertionError, message):
            traced(torch.rand(4, 3))

    def test_get_all_users_of(self):
        graph : torch._fx.Graph = torch._fx.Graph()
        a : torch._fx.Node = graph.create_node('placeholder', 'x')
        b : torch._fx.Node = graph.create_node('call_module', 'linear_mod', args=(a,))
        c : torch._fx.Node = graph.create_node('get_attr', 'y_attr')
        d : torch._fx.Node = graph.create_node('call_function', operator.add, args=(b, c))
        graph.output(d)
        linear_mod : torch.nn.Module = torch.nn.Linear(3, 4)
        add_param : torch.Tensor = torch.rand(3, 4)
        gm : torch._fx.GraphModule = torch._fx.GraphModule(
            {'linear_mod': linear_mod, 'y_attr' : add_param}, graph)
        expected_uses: Dict[int, List[int]] = {
            0: [1],
            1: [3],
            2: [3],
            3: []
        }
        for i, node in enumerate(graph.nodes):
            user_indexes = GraphManipulation.get_all_users_of(gm, i)
            assert user_indexes == expected_uses[i]

    def test_copy_no_remap(self):
        traced = symbolic_trace(SimpleTest())
        g = traced.graph
        copied = torch._fx.Graph()
        for node in g.nodes:
            copied.node_copy(node)
        with self.assertRaisesRegex(RuntimeError, 'does not belong to this Graph'):
            copied.lint()

    def test_wrong_topo(self):
        graph : torch._fx.Graph = torch._fx.Graph()
        a : torch._fx.Node = graph.create_node('placeholder', 'x')
        b : torch._fx.Node = graph.create_node('call_module', 'foo.bar.baz', args=(a,))
        c : torch._fx.Node = graph.create_node('get_attr', 'zip.zap.zam')
        d : torch._fx.Node = graph.create_node('call_function', operator.add, args=(b, c))
        graph.output(d)
        nodes = graph._nodes
        nodes[2], nodes[3] = nodes[3], nodes[2]
        with self.assertRaisesRegex(RuntimeError, 'was used before it has been defined'):
            graph.lint()

if __name__ == '__main__':
    run_tests()

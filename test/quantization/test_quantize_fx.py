import torch
import torch.nn.functional as F
import torch.nn as nn
import torch.nn.quantized as nnq
import torch.nn.quantized.dynamic as nnqd
import torch.nn.intrinsic.quantized as nniq
import torch.multiprocessing as mp

# symbolic trace
from torch._fx import symbolic_trace

# graph mode quantization based on fx
from torch.quantization import (
    QuantType,
    fuse_fx,
    prepare_fx,
    convert_fx,
    prepare_static_fx,
    convert_static_fx,
    quantize_static_fx,
    quantize_dynamic_fx,
    prepare_qat_fx,
    register_observed_custom_module_mapping,
    register_quantized_custom_module_mapping,
)

from torch.quantization import (
    default_qconfig,
    default_dynamic_qconfig,
    float16_dynamic_qconfig,
    default_qat_qconfig,
    prepare,
    prepare_qat,
    convert,
)

# test utils
from torch.testing._internal.common_cuda import TEST_MULTIGPU, TEST_CUDA
from torch.testing._internal.common_quantization import (
    QuantizationTestCase,
    skipIfNoFBGEMM,
    skip_if_no_torchvision,
    train_one_epoch,
    run_ddp,
    LinearModelWithSubmodule,
)

from torch.testing._internal.common_quantized import (
    override_qengines,
)

from torch.testing._internal.common_distributed import skip_if_not_multigpu

from torch.testing._internal.common_quantization import NodeSpec as ns

from torch.testing._internal.common_quantization import (
    test_only_eval_fn,
)
from torch.testing import FileCheck

import itertools
import operator
import unittest
import io

@skipIfNoFBGEMM
class TestQuantizeFx(QuantizationTestCase):
    def _get_conv_linear_test_cases(self):
        ''' Returns a list of test cases, with format:
        is_dynamic, ModuleClass, module_constructor_inputs,
        inputs, quantized_node, weight_prepack_op
        '''
        class Conv(torch.nn.Module):
            def __init__(self, weight):
                super().__init__()
                self.weight = torch.nn.Parameter(weight)
                self.stride = (1, 1)
                self.padding = (0, 0)
                self.dilation = (1, 1)
                self.groups = 1

            def forward(self, x):
                return F.conv2d(x, self.weight, None, self.stride, self.padding, self.dilation, self.groups)

        conv_input = torch.rand(1, 3, 224, 224)
        conv_weight = torch.rand(3, 3, 3, 3)

        class Linear(torch.nn.Module):
            def __init__(self, weight):
                super().__init__()
                self.weight = torch.nn.Parameter(weight)

            def forward(self, x):
                return F.linear(x, self.weight)

        linear_input = torch.rand(8, 5)
        linear_weight = torch.rand(10, 5)

        class LinearModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 10)

            def forward(self, x):
                return self.linear(x)

        linear_module_input = torch.rand(8, 5)

        tests = [
            (False, Conv, (conv_weight,), (conv_input,),
             ns.call_function(torch.ops.quantized.conv2d),
             ns.call_function(torch.ops.quantized.conv2d_prepack)),
            (True, Linear, (linear_weight,), (linear_input,),
             ns.call_function(torch.ops.quantized.linear_dynamic),
             ns.call_function(torch.ops.quantized.linear_prepack)),
            (False, Linear, (linear_weight,), (linear_input,),
             ns.call_function(torch.ops.quantized.linear),
             ns.call_function(torch.ops.quantized.linear_prepack)),
            (True, LinearModule, (), (linear_module_input,),
             ns.call_module(nnqd.Linear),
             None),
            (False, LinearModule, (), (linear_module_input,),
             ns.call_module(nnq.Linear),
             None),
        ]
        return tests

    """
    Unit tests for functionalities
    """
    @skipIfNoFBGEMM
    def test_functional_no_debug(self):
        """ Test quantizing functional conv and linear
        """
        tests = self._get_conv_linear_test_cases()
        for (is_dynamic, ModuleClass, module_constructor_inputs,
             inputs, quantized_node, weight_prepack_node) in tests:
            quant_type = QuantType.DYNAMIC if is_dynamic else QuantType.STATIC
            node_occurrence = dict()
            if weight_prepack_node:
                node_occurrence[weight_prepack_node] = 0
            self.checkGraphModeFxOp(
                ModuleClass(*module_constructor_inputs),
                inputs, quant_type,
                expected_node=quantized_node,
                expected_node_occurrence=node_occurrence,
                debug=False)

    @skipIfNoFBGEMM
    def test_functional_debug(self):
        """ Test quantizing functional conv and linear with debug option
        """
        tests = self._get_conv_linear_test_cases()
        for (is_dynamic, ModuleClass, module_constructor_inputs,
             inputs, quantized_node, weight_prepack_node) in tests:
            quant_type = QuantType.DYNAMIC if is_dynamic else QuantType.STATIC
            node_occurrence = dict()
            if weight_prepack_node:
                node_occurrence[weight_prepack_node] = 1
            self.checkGraphModeFxOp(
                ModuleClass(*module_constructor_inputs),
                inputs, quant_type,
                expected_node=quantized_node,
                expected_node_occurrence=node_occurrence,
                debug=True)

    @skipIfNoFBGEMM
    def test_dynamic_quant_weight_observer(self):
        ''' Test that weight observer is run in convert step
        '''

        class M(torch.nn.Module):
            def __init__(self, weight):
                super().__init__()
                self.weight = torch.nn.Parameter(weight)

            def forward(self, x):
                return F.linear(x, self.weight)

        m = M(torch.rand(1, 1)).eval()
        original = symbolic_trace(m)
        qconfig = default_dynamic_qconfig
        qconfig_dict = {'': qconfig}
        quantized = quantize_dynamic_fx(original, qconfig_dict, debug=True)
        qparams = (quantized._scale_0, quantized._zero_point_0)
        weight_obs = qconfig.weight()
        weight_obs(quantized.weight)
        ref_qparams = weight_obs.calculate_qparams()
        self.assertEqual(qparams, ref_qparams)

    @skipIfNoFBGEMM
    def test_dynamic_quant_fp16(self):
        class Linear(torch.nn.Module):
            def __init__(self, weight):
                super().__init__()
                self.weight = torch.nn.Parameter(weight)

            def forward(self, x):
                return F.linear(x, self.weight)

        linear_input = torch.rand(8, 5)
        linear_weight = torch.rand(10, 5)

        class LinearModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(5, 10)

            def forward(self, x):
                return self.linear(x)

        linear_module_input = torch.rand(8, 5)

        tests = [
            (Linear, (linear_weight,), (linear_input,),
             ns.call_function(torch.ops.quantized.linear_dynamic),
             ns.call_function(torch.ops.quantized.linear_prepack_fp16)),
            (LinearModule, (), (linear_module_input,),
             ns.call_module(nnqd.Linear),
             None),
        ]
        for (ModuleClass, module_constructor_inputs,
             inputs, quantized_node, weight_prepack_node) in tests:
            for debug in [True, False]:
                node_occurrence = dict()
                if weight_prepack_node:
                    if debug:
                        node_occurrence[weight_prepack_node] = 1
                    else:
                        node_occurrence[weight_prepack_node] = 0
                m = ModuleClass(*module_constructor_inputs).eval()
                m = symbolic_trace(m)
                qconfig_dict = {"": float16_dynamic_qconfig}
                m = quantize_dynamic_fx(m, qconfig_dict, debug=debug)
                self.checkGraphModuleNodes(m, expected_node_occurrence=node_occurrence)



    @unittest.skipIf(not TEST_MULTIGPU, "multi-GPU not supported")
    @unittest.skipIf(not TEST_CUDA, "CUDA unavailable")
    @override_qengines
    def test_qat_prepare_device_affinity(self):
        """
        Tests that FX QAT prepare pass respects device affinity
        """
        class Model(nn.Module):

            def __init__(self):
                super(Model, self).__init__()
                self.conv = nn.Conv2d(1, 1, 1)
                self.bn = nn.BatchNorm2d(1)
                self.relu = nn.ReLU()

            def forward(self, x):
                x = self.conv(x)
                x = self.bn(x)
                x = self.relu(x)
                return x

        model = Model()
        qengine = torch.backends.quantized.engine
        qconfig_dict = {'': torch.quantization.get_default_qat_qconfig(qengine)}
        device = torch.device('cuda:0')
        model.to(device)

        # symbolically trace
        model = symbolic_trace(model)

        # QAT prepare
        model = fuse_fx(model)
        model = prepare_fx(model, qconfig_dict)

        # ensure that running an input on CUDA works without any needed changes
        input = torch.randn(4, 1, 4, 4, device=device)
        model(input)

        # ensure all buffers and parameters are on the device we expect
        model_devices = {p.device for p in model.parameters()} | \
            {p.device for p in model.buffers()}
        self.assertEqual(len(model_devices), 1)
        model_device = next(iter(model_devices))
        self.assertEqual(model_device, device)

    @skipIfNoFBGEMM
    def test_inplace_option(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3)

            def forward(self, x):
                return self.conv(x)

        model = symbolic_trace(M().eval())
        qconfig_dict = {'': default_qconfig}
        non_inplace_model = quantize_static_fx(
            model, qconfig_dict, test_only_eval_fn, [self.img_data_2d], inplace=False)
        inplace_model = model
        inplace_model = quantize_static_fx(
            inplace_model, qconfig_dict, test_only_eval_fn, [self.img_data_2d], inplace=True)
        non_inplace_res = non_inplace_model(self.img_data_2d[0][0])
        inplace_res = inplace_model(self.img_data_2d[0][0])
        self.assertEqual(non_inplace_res, inplace_res)

    @skipIfNoFBGEMM
    def test_dict_output(self):
        """ Make sure quantization runs for models with dictionary output
        """
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(1, 1, 1)

            def forward(self, x):
                return {"output": self.conv(x["input"])}

        dict_input = {"input": torch.randn(1, 1, 1, 1)}
        m = symbolic_trace(M()).eval()
        qconfig_dict = {"": default_qconfig}
        m = prepare_static_fx(m, qconfig_dict)
        m(dict_input)
        m = convert_static_fx(m)
        m(dict_input)

    @skipIfNoFBGEMM
    def test_qconfig_none(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(1, 1, 1)
                self.conv2 = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x)
                return x

        m = M().eval()
        m = symbolic_trace(m)
        qconfig_dict = {"": default_qconfig,
                        "module_name": [("conv2", None)]}
        m = prepare_static_fx(m, qconfig_dict)
        data = torch.randn(1, 1, 1, 1)
        m(data)
        m = convert_static_fx(m)
        m(data)
        # first conv is quantized, second conv is not quantized
        node_list = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_method("dequantize"),
            ns.call_module(nn.Conv2d),
        ]
        self.checkGraphModuleNodes(m, expected_node_list=node_list)

    def test_qconfig_module_type(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(1, 1, 1)
                self.conv2 = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x)
                return x

        m = M().eval()
        m = symbolic_trace(m)
        qconfig_dict = {"object_type": [(torch.nn.Conv2d, default_qconfig)]}
        m = prepare_static_fx(m, qconfig_dict)
        data = torch.randn(1, 1, 1, 1)
        m(data)
        m = convert_static_fx(m)
        m(data)
        # first conv is quantized, second conv is not quantized
        node_list = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_module(nnq.Conv2d),
            ns.call_method("dequantize"),
        ]
        self.checkGraphModuleNodes(m, expected_node_list=node_list)

    def test_qconfig_function(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()

            def forward(self, x, y):
                return x + y

        m = M().eval()
        m = symbolic_trace(m)
        qconfig_dict = {"object_type": [(operator.add, default_qconfig)]}
        m = prepare_static_fx(m, qconfig_dict)
        data = torch.randn(1, 1, 1, 1)
        m(data, data)
        m = convert_static_fx(m)
        m(data, data)
        # first conv is quantized, second conv is not quantized
        node_list = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_function(torch.ops.quantized.add),
            ns.call_method("dequantize"),
        ]
        self.checkGraphModuleNodes(m, expected_node_list=node_list)

    def test_qconfig_module_name_regex(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv1 = nn.Conv2d(1, 1, 1)
                self.conv2 = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x)
                return x

        m = M().eval()
        m = symbolic_trace(m)
        qconfig_dict = {"module_name_regex": [("conv*", default_qconfig)]}
        m = prepare_static_fx(m, qconfig_dict)
        data = torch.randn(1, 1, 1, 1)
        m(data)
        m = convert_static_fx(m)
        m(data)
        # first conv is quantized, second conv is not quantized
        node_list = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_module(nnq.Conv2d),
            ns.call_method("dequantize"),
        ]
        self.checkGraphModuleNodes(m, expected_node_list=node_list)

    def test_qconfig_precedence(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.linear = nn.Linear(1, 1)
                self.conv = nn.Conv2d(1, 1, 1)
                self.module_conv1 = nn.Conv2d(1, 1, 1)
                self.module_conv2 = nn.Conv2d(1, 1, 1)

            def forward(self, x):
                # global
                x = self.linear(x)
                # global + object_type --> object_type
                x = self.conv(x)
                # global + object_type + module_name_regex --> module_name_regex
                x = self.module_conv1(x)
                # global + object_type + module_name_regex + module_name --> module_name
                x = self.module_conv2(x)
                return x

        m = M().eval()
        m = symbolic_trace(m)
        global_qconfig = default_qconfig
        object_type_qconfig = default_dynamic_qconfig
        module_name_regex_qconfig = float16_dynamic_qconfig
        module_name_qconfig = default_qat_qconfig
        qconfig_dict = {
            "": global_qconfig,
            "object_type": [(nn.Conv2d, object_type_qconfig)],
            "module_name_regex": [("module_conv*", module_name_regex_qconfig)],
            "module_name": [("module_conv2", module_name_qconfig)]}
        m = prepare_static_fx(m, qconfig_dict)
        self.assertEqual(m.linear.qconfig, global_qconfig)
        self.assertEqual(m.conv.qconfig, object_type_qconfig)
        self.assertEqual(m.module_conv1.qconfig, module_name_regex_qconfig)
        self.assertEqual(m.module_conv2.qconfig, module_name_qconfig)


    def test_remove_qconfig(self):
        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.avg_pool = torch.nn.AvgPool2d(1)

            def forward(self, x):
                return self.avg_pool(x)

        m = M().eval()
        m = symbolic_trace(m)
        qconfig_dict = {'': default_qconfig}
        m = prepare_static_fx(m, qconfig_dict)
        data = torch.randn(1, 1, 1, 1)
        m(data)
        m = convert_static_fx(m)
        m(data)
        for name, module in m.named_modules():
            self.assertFalse(hasattr(module, 'qconfig'),
                             'qconfig is not removed for ' + name)

    @skipIfNoFBGEMM
    def test_qat_and_script(self):

        model = LinearModelWithSubmodule()
        qengine = torch.backends.quantized.engine
        qconfig_dict = {'': torch.quantization.get_default_qat_qconfig(qengine)}

        # symbolically trace
        model = symbolic_trace(model)
        model = prepare_qat_fx(model, qconfig_dict)

        # ensure scripting works
        scripted = torch.jit.script(model)
        # run one round to make sure model runs
        x = torch.randn(5, 5)
        scripted(x)
        FileCheck().check_count('FakeQuantize = prim::GetAttr[name="', 4, exactly=True) \
                   .run(scripted.graph)

        # disable fake_quant and observer
        for epoch in range(3):
            if epoch == 1:
                scripted.apply(torch.quantization.disable_observer)
            if epoch == 2:
                scripted.apply(torch.quantization.disable_fake_quant)

        # ensure the fake_quant and observer have been disabled.
        matches = ['.fake_quant_enabled', '.observer_enabled']
        for key, v in scripted.state_dict().items():
            if any(x in key for x in matches):
                self.assertEqual(v, torch.tensor([0], dtype=torch.uint8))

        # enable them back
        scripted.apply(torch.quantization.enable_fake_quant)
        scripted.apply(torch.quantization.enable_observer)
        for key, v in scripted.state_dict().items():
            if any(x in key for x in matches):
                self.assertEqual(v, torch.tensor([1], dtype=torch.uint8))

    @skipIfNoFBGEMM
    def test_save_observer_state_dict(self):
        orig = LinearModelWithSubmodule().eval()
        model = orig
        qconfig_dict = {'': torch.quantization.get_default_qconfig('fbgemm')}
        # symbolically trace
        model = symbolic_trace(model)
        model = prepare_static_fx(model, qconfig_dict)
        # run it through input
        x = torch.randn(5, 5)
        model(x)

        quant = convert_static_fx(model)

        # save state_dict of model
        obs_dict = torch.quantization.get_observer_state_dict(model)
        b = io.BytesIO()
        torch.save(obs_dict, b)
        b.seek(0)

        # Load the stats into new model
        model_2 = orig
        model_2 = symbolic_trace(model_2)
        model_2 = prepare_static_fx(model_2, qconfig_dict)

        loaded_dict = torch.load(b)
        torch.quantization.load_observer_state_dict(model_2, loaded_dict)

        quant_2 = convert_static_fx(model_2)

        # Verify that loaded state dict produces same results.
        self.assertEqual(quant(x), quant_2(x))

    @skipIfNoFBGEMM
    def test_custom_module_class(self):
        class CustomModule(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(1, 1, 1)

            def forward(self, x):
                return self.conv(x)

        class ObservedCustomModule(torch.nn.Module):
            def __init__(self, conv):
                super().__init__()
                self.conv = conv

            def forward(self, x):
                return self.conv(x)

            @classmethod
            def from_float(cls, float_module):
                assert hasattr(float_module, 'qconfig')
                observed = cls(float_module.conv)
                observed.qconfig = float_module.qconfig
                return observed

        class QuantizedCustomModule(torch.nn.Module):
            def __init__(self, conv):
                super().__init__()
                self.conv = conv

            def forward(self, x):
                return self.conv(x)

            @classmethod
            def from_observed(cls, observed_module):
                assert hasattr(observed_module, 'qconfig')
                assert hasattr(observed_module, 'activation_post_process')
                observed_module.conv.activation_post_process = \
                    observed_module.activation_post_process
                quantized = cls(nnq.Conv2d.from_float(observed_module.conv))
                return quantized

        class DynamicallyQuantizedCustomModule(torch.nn.Module):
            def __init__(self, conv):
                super().__init__()
                self.conv = conv

            def forward(self, x):
                return self.conv(x)

            @classmethod
            def from_observed(cls, observed_module):
                assert hasattr(observed_module, 'qconfig')
                assert hasattr(observed_module, 'activation_post_process')
                quantized = cls(nnqd.Conv2d.from_float(observed_module.conv))
                return quantized

        class M(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv = torch.nn.Conv2d(1, 1, 1)
                self.custom = CustomModule()

            def forward(self, x):
                x = self.conv(x)
                x = self.custom(x)
                return x

        class RefM(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.conv1 = torch.nn.Conv2d(1, 1, 1)
                self.conv2 = torch.nn.Conv2d(1, 1, 1)

            def forward(self, x):
                x = self.conv1(x)
                x = self.conv2(x)
                return x

        data = torch.randn(1, 1, 1, 1)
        # instantiate M and RefM and align the parameters
        original_m = M()
        original_ref_m = RefM()
        original_ref_m.conv1.weight = torch.nn.Parameter(original_m.conv.weight.detach())
        original_ref_m.conv1.bias = torch.nn.Parameter(original_m.conv.bias.detach())
        original_ref_m.conv2.weight = torch.nn.Parameter(original_m.custom.conv.weight.detach())
        original_ref_m.conv2.bias = torch.nn.Parameter(original_m.custom.conv.bias.detach())

        from torch._fx.symbolic_trace import Tracer

        # define a custom tracer to not trace through the custom module

        class CustomTracer(Tracer):
            def is_leaf_module(self, m, module_qualified_name):
                return (m.__module__.startswith('torch.nn') and
                        not isinstance(m, torch.nn.Sequential)) or \
                    isinstance(m, CustomModule)

        # TODO: add other quant types after mixed mode support
        for quant_type in [QuantType.STATIC]:
            # register observed and quantized custom module classes
            register_observed_custom_module_mapping(CustomModule, ObservedCustomModule)
            register_quantized_custom_module_mapping(CustomModule, QuantizedCustomModule)

            m = CustomTracer().trace(original_m).eval()
            qconfig_dict = {'': default_qconfig}
            # check prepared model
            m = prepare_static_fx(m, qconfig_dict)
            # calibration
            m(data)
            # all activation observers are inserted in the top level module
            count_check = {
                ns.call_module(torch.quantization.MinMaxObserver): 3
            }
            self.checkGraphModuleNodes(m, expected_node_occurrence=count_check)

            # check converted/quantized model
            m = convert_static_fx(m)
            count_check = {
                ns.call_function(torch.quantize_per_tensor) : 1,
                ns.call_module(nnq.Conv2d) : 1,
                ns.call_method('dequantize') : 1,
            }
            self.checkGraphModuleNodes(m, expected_node_occurrence=count_check)
            res = m(data)

            # quantize the reference model
            ref_m = symbolic_trace(original_ref_m).eval()
            ref_m = prepare_fx(ref_m, qconfig_dict)
            ref_m(data)
            ref_m = convert_fx(ref_m)
            ref_res = ref_m(data)
            self.assertEqual(res, ref_res)

class TestQuantizeFxOps(QuantizationTestCase):
    """Unit tests for individual ops
    """
    @skipIfNoFBGEMM
    def test_linear(self):
        class ModuleLinear(torch.nn.Module):
            def __init__(self, has_relu=False, f_relu=False):
                super(ModuleLinear, self).__init__()
                self.linear = torch.nn.Linear(30, 4).float()
                if has_relu:
                    if f_relu:
                        self.relu = F.relu
                    else:
                        self.relu = torch.nn.ReLU()
                else:
                    self.relu = torch.nn.Identity()

            def forward(self, x):
                return self.relu(self.linear(x))

        class FuncLinear(torch.nn.Module):
            def __init__(self, has_relu=False, f_relu=False):
                super(FuncLinear, self).__init__()
                self.w = torch.randn(4, 30)
                self.b = torch.randn(4)
                if has_relu:
                    if f_relu:
                        self.relu = F.relu
                    else:
                        self.relu = torch.nn.ReLU()
                else:
                    self.relu = torch.nn.Identity()

            def forward(self, x):
                return self.relu(F.linear(x, self.w, self.b))

        data = (torch.rand((1, 30), dtype=torch.float),)
        options = itertools.product(
            [(ModuleLinear(has_relu=False), True)],
            # TODO: enable after raw `tensor` is supported in fx
            # (FuncLinear(has_relu=False), False)],
            self.all_quant_types)
        quantized_nodes = {
            # is_module
            True: {
                # quant_type:
                QuantType.DYNAMIC: ns.call_module(nnqd.Linear),
                QuantType.STATIC: ns.call_module(nnq.Linear),
                # note that we are checking the final result
                QuantType.QAT: ns.call_module(nnq.Linear),
            },
            False: {
                # quant_type:
                QuantType.DYNAMIC: ns.call_function(torch.ops.quantized.linear_dynamic),
                QuantType.STATIC: ns.call_function(torch.ops.quantized.linear),
                QuantType.QAT: ns.call_function(torch.ops.quantized.linear),
            }
        }
        for (model, is_module), quant_type in options:
            self.checkGraphModeFxOp(
                model, data, quant_type, quantized_nodes[is_module][quant_type])

        for f_relu, quant_type in itertools.product([True, False], [QuantType.STATIC, QuantType.QAT]):
            for model, quantized_node in [
                    (ModuleLinear(has_relu=True, f_relu=f_relu), ns.call_module(nniq.LinearReLU))]:
                # TODO: support functional linear + relu fusion
                # (FuncLinear(has_relu=True, f_relu=f_relu), ns.call_function(torch.ops.quantized.linear_relu))]:
                self.checkGraphModeFxOp(model, data, quant_type, quantized_node)

    @skipIfNoFBGEMM
    def test_quantized_conv(self):
        conv_module = {1 : torch.nn.Conv1d, 2 : torch.nn.Conv2d, 3 : torch.nn.Conv3d}

        class Conv(torch.nn.Module):
            def __init__(self, dim):
                super(Conv, self).__init__()
                self.conv = conv_module[dim](3, 3, 3).float()

            def forward(self, x):
                return self.conv(x)

        options = itertools.product([1, 2, 3], self.static_quant_types)
        quantized_nodes = {
            # dim
            1: ns.call_module(nnq.Conv1d),
            2: ns.call_module(nnq.Conv2d),
            3: ns.call_module(nnq.Conv3d),
        }
        for dim, quant_type in options:
            model = self.checkGraphModeFxOp(
                Conv(dim), self.img_data_dict[dim], quant_type,
                quantized_nodes[dim])

    @skipIfNoFBGEMM
    def test_quantized_conv_relu(self):
        """tests for conv1d_relu/conv2d_relu/conv3d_relu"""
        conv_module = {1 : torch.nn.Conv1d, 2 : torch.nn.Conv2d, 3 : torch.nn.Conv3d}

        class ConvNdRelu(torch.nn.Module):
            def __init__(self, dim, inplace):
                super(ConvNdRelu, self).__init__()
                self.conv = conv_module[dim](3, 3, 3).float()
                self.relu = torch.nn.ReLU(inplace)

            def forward(self, x):
                return self.relu(self.conv(x))

        class ConvNdFunctionalRelu(torch.nn.Module):
            def __init__(self, dim):
                super(ConvNdFunctionalRelu, self).__init__()
                self.conv = conv_module[dim](3, 3, 3).float()

            def forward(self, x):
                return F.relu(self.conv(x))

        class ConvNdInplaceFunctionalRelu(torch.nn.Module):
            def __init__(self, dim):
                super(ConvNdInplaceFunctionalRelu, self).__init__()
                self.conv = conv_module[dim](3, 3, 3).float()

            def forward(self, x):
                return F.relu(self.conv(x), True)

        options = itertools.product([1, 2, 3], self.static_quant_types)
        quantized_nodes = {
            # dim
            1: ns.call_module(nniq.ConvReLU1d),
            2: ns.call_module(nniq.ConvReLU2d),
            3: ns.call_module(nniq.ConvReLU3d),
        }
        for dim, quant_type in options:
            for m in [ConvNdRelu(dim, True),
                      ConvNdRelu(dim, False),
                      ConvNdFunctionalRelu(dim),
                      ConvNdInplaceFunctionalRelu(dim)]:
                self.checkGraphModeFxOp(
                    m, self.img_data_dict[dim], quant_type,
                    quantized_nodes[dim])


    def _test_quantized_binary_op_impl(self, binary_op, ibinary_op, quantized_op):
        class Op(torch.nn.Module):
            def __init__(self, is_inplace, is_scalar):
                super(Op, self).__init__()
                self.conv1 = torch.nn.Conv2d(1, 1, 1).float()
                self.conv2 = torch.nn.Conv2d(1, 1, 1).float()
                self.is_scalar = is_scalar
                self.op = ibinary_op if is_inplace else binary_op

            def forward(self, x, y):
                x = self.conv1(x)
                y = 3 if self.is_scalar else self.conv2(y)
                x = self.op(x, y)
                return x

        # TODO: decide whether we want to quantize or not
        # in this case
        # class NonQuantizedOp(torch.nn.Module):
        #     def __init__(self, is_inplace, is_scalar):
        #         super(NonQuantizedOp, self).__init__()
        #         self.is_scalar = is_scalar
        #         self.op = ibinary_op if is_inplace else binary_op

        #     def forward(self, x, y):
        #         y = 3 if self.is_scalar else y
        #         x = self.op(x, y)
        #         return x

        data = (torch.randn(1, 1, 1, 1, dtype=torch.float),
                torch.randn(1, 1, 1, 1, dtype=torch.float))
        quantized_node = ns.call_function(quantized_op)
        options = itertools.product([True, False], [True, False])
        quant_type = QuantType.STATIC
        for is_inplace, is_scalar in options:
            self.checkGraphModeFxOp(
                Op(is_inplace, is_scalar), data, quant_type, quantized_node)

    def _test_quantized_binary_op_relu_impl(self, binary_op, ibinary_op, quantized_op):
        class OpRelu(torch.nn.Module):
            def __init__(self, is_inplace, is_functional_relu,
                         is_scalar):
                super(OpRelu, self).__init__()
                self.conv1 = torch.nn.Conv2d(1, 1, 1).float()
                self.conv2 = torch.nn.Conv2d(1, 1, 1).float()
                self.op = ibinary_op if is_inplace else binary_op
                self.is_functional_relu = is_functional_relu
                self.is_scalar = is_scalar
                self.relu = F.relu if self.is_functional_relu \
                    else torch.nn.ReLU()

            def forward(self, x, y):
                x = self.conv1(x)
                y = 3 if self.is_scalar else self.conv2(y)
                x = self.op(x, y)
                x = self.relu(x)
                return x

        data = (torch.rand((1, 1, 1, 1), dtype=torch.float),
                torch.rand((1, 1, 1, 1), dtype=torch.float))
        quant_type = QuantType.STATIC
        quantized_node = ns.call_function(quantized_op)
        options = itertools.product(
            [True, False], [True, False], [True, False])
        for is_inplace_op, is_functional_relu, is_scalar in options:
            self.checkGraphModeFxOp(
                OpRelu(is_inplace_op, is_functional_relu, is_scalar),
                data, quant_type, quantized_node)

    @skipIfNoFBGEMM
    def test_quantized_add(self):
        self._test_quantized_binary_op_impl(
            operator.add, operator.iadd, torch.ops.quantized.add)

    @skipIfNoFBGEMM
    def test_quantized_mul(self):
        self._test_quantized_binary_op_impl(
            operator.mul, operator.imul, torch.ops.quantized.mul)

    @skipIfNoFBGEMM
    def test_quantized_add_relu(self):
        self._test_quantized_binary_op_relu_impl(
            operator.add, operator.iadd, torch.ops.quantized.add_relu)

    @skipIfNoFBGEMM
    def test_quantized_mul_relu(self):
        self._test_quantized_binary_op_relu_impl(
            operator.mul, operator.imul, torch.ops.quantized.mul_relu)

    @skipIfNoFBGEMM
    def test_quantized_cat(self):
        """ quantization of the output of cat will be depend on the
        input of cat. we only quantize the output of cat when its inputs are quantized.
        """
        class QuantizedCat(torch.nn.Module):
            def __init__(self):
                super(QuantizedCat, self).__init__()
                self.conv1 = torch.nn.Conv2d(2, 2, 2).float()
                self.conv2 = torch.nn.Conv2d(2, 2, 2).float()

            def forward(self, x, y):
                x = self.conv1(x)
                y = self.conv2(y)
                return torch.cat([x, y], 1)

        # TODO: decide whether to quantize in this case
        # class NonQuantizedCat(torch.nn.Module):
        #     def __init__(self):
        #         super(NonQuantizedCat, self).__init__()

        #     def forward(self, x, y):
        #         return torch.cat([x, y], 1)

        data = (torch.randn(1, 2, 5, 5, dtype=torch.float),
                torch.randn(1, 2, 5, 5, dtype=torch.float))
        quantized_node = ns.call_function(torch.ops.quantized.cat)
        for quant_type in self.static_quant_types:
            self.checkGraphModeFxOp(QuantizedCat(), data, quant_type, quantized_node)


    @skipIfNoFBGEMM
    def test_qbatch_norm(self):
        bn_module = {
            # TODO: quantized batchnorm 1d module is missing
            # 1 : torch.nn.BatchNorm1d,
            2 : torch.nn.BatchNorm2d,
            3 : torch.nn.BatchNorm3d,
        }

        class M(torch.nn.Module):
            def __init__(self, dim):
                super(M, self).__init__()
                self.bn = bn_module[dim](3).to(torch.float)

            def forward(self, x):
                return self.bn(x)

        options = itertools.product(self.static_quant_types, [2, 3])
        quantized_nodes = {
            # 1: ns.call_module(nnq.BatchNorm1d),
            2: ns.call_module(nnq.BatchNorm2d),
            3: ns.call_module(nnq.BatchNorm3d),
        }
        for quant_type, dim in options:
            model = self.checkGraphModeFxOp(
                M(dim), self.img_data_dict[dim], quant_type, quantized_nodes[dim])

    @skipIfNoFBGEMM
    def test_qbatch_norm_relu(self):
        bn_module = {2 : torch.nn.BatchNorm2d, 3 : torch.nn.BatchNorm3d}

        class BNRelu(torch.nn.Module):
            def __init__(self, dim, inplace):
                super(BNRelu, self).__init__()
                self.bn = bn_module[dim](3).to(torch.float)
                self.relu = torch.nn.ReLU(inplace=inplace)

            def forward(self, x):
                return self.relu(self.bn(x))

        class BNFuncRelu(torch.nn.Module):
            def __init__(self, dim):
                super(BNFuncRelu, self).__init__()
                self.bn = bn_module[dim](3).to(torch.float)

            def forward(self, x):
                return F.relu(self.bn(x), False)

        class BNFuncInplaceRelu(torch.nn.Module):
            def __init__(self, dim):
                super(BNFuncInplaceRelu, self).__init__()
                self.bn = bn_module[dim](3).to(torch.float)

            def forward(self, x):
                return F.relu(self.bn(x), True)

        options = itertools.product(self.static_quant_types, [2, 3])
        quantized_nodes = {
            2: ns.call_module(nniq.BNReLU2d),
            3: ns.call_module(nniq.BNReLU3d),
        }
        for quant_type, dim in options:
            for instance in [BNRelu(dim, True), BNRelu(dim, False),
                             BNFuncRelu(dim), BNFuncInplaceRelu(dim)]:
                self.checkGraphModeFxOp(
                    instance, self.img_data_dict[dim], quant_type,
                    quantized_nodes[dim])

    def _test_activation_impl(
            self, float_module, float_op, quantized_module, quantized_op):
        ''' Test for activation op(with inplace options), float_op can be
        torch op or functional op
        '''
        class M(torch.nn.Module):
            def __init__(self, is_module, inplace):
                super(M, self).__init__()
                self.is_module = is_module
                self.inplace = inplace
                if self.is_module:
                    self.op = float_module(self.inplace)
                else:
                    self.op = float_op

            def forward(self, input):
                if self.is_module:
                    return self.op(input)
                else:
                    return self.op(input, self.inplace)

        options = itertools.product([True, False], [True, False], self.static_quant_types)
        quantized_nodes = {
            # is_module
            True: ns.call_module(quantized_module),
            False: ns.call_function(quantized_op),
        }

        for is_module, is_inplace, quant_type in options:
            self.checkGraphModeFxOp(
                M(is_module, is_inplace), self.img_data_2d,
                quant_type, quantized_nodes[is_module])

    def test_hardswish(self):
        self._test_activation_impl(nn.Hardswish, F.hardswish, nnq.Hardswish, torch.ops.quantized.hardswish)

    def test_elu(self):
        self._test_activation_impl(nn.ELU, F.elu, nnq.ELU, torch.ops.quantized.elu)

    def _test_norm_impl(
            self, float_module, float_op, op_args, data, quantized_module, quantized_op,
            skip_op_arg_for_functional=False):
        ''' Test for normalization op, float_op can be torch op or functional op,
        op_args is a list of positional argument for the module/op
        '''
        class M(torch.nn.Module):
            def __init__(self, is_module):
                super(M, self).__init__()
                self.is_module = is_module
                if self.is_module:
                    self.op = float_module(*op_args)
                else:
                    self.op = float_op

            def forward(self, input):
                if self.is_module:
                    return self.op(input)
                else:
                    args = [input]
                    if not skip_op_arg_for_functional:
                        args += op_args
                    return self.op(*args)

        options = itertools.product([True, False], self.static_quant_types)
        quantized_nodes = {
            # is_module
            True: ns.call_module(quantized_module),
            False: ns.call_function(quantized_op),
        }

        for is_module, quant_type in options:
            self.checkGraphModeFxOp(
                M(is_module), data, quant_type, quantized_nodes[is_module])

    def test_layer_norm(self):
        data = (torch.rand((1, 2, 5, 5), dtype=torch.float),)
        self._test_norm_impl(
            nn.LayerNorm, F.layer_norm, [[2, 5, 5]], data, nnq.LayerNorm, torch.ops.quantized.layer_norm)

    def test_instance_norm(self):
        data_1d = (torch.rand((1, 4, 5), dtype=torch.float),)
        data_2d = (torch.rand((1, 4, 5, 1), dtype=torch.float),)
        data_3d = (torch.rand((1, 4, 5, 1, 1), dtype=torch.float),)
        data_dict = {1 : data_1d, 2 : data_2d, 3 : data_3d}
        instance_norm_modules = {1 : nn.InstanceNorm1d,
                                 2 : nn.InstanceNorm2d,
                                 3 : nn.InstanceNorm3d}
        quantized_instance_norm_modules = {
            1 : nnq.InstanceNorm1d,
            2 : nnq.InstanceNorm2d,
            3 : nnq.InstanceNorm3d
        }
        for dim in [1, 2, 3]:
            data = data_dict[dim]
            module = instance_norm_modules[dim]
            quantized_module = quantized_instance_norm_modules[dim]
            self._test_norm_impl(
                module, F.instance_norm, [4], data,
                quantized_module, torch.ops.quantized.instance_norm,
                skip_op_arg_for_functional=True)

    @skipIfNoFBGEMM
    def test_clamp(self):
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(2, 2, 2).float()
                self.relu6 = torch.nn.ReLU6()
                self.relu6_ = torch.nn.ReLU6(True)
                self.hardtanh = torch.nn.Hardtanh()
                self.hardtanh_ = torch.nn.Hardtanh(inplace=True)

            def forward(self, x):
                x = self.conv(x)
                x = self.relu6(x)
                self.relu6_(x)
                x = F.relu6(x)
                x = torch.clamp(x, -3, 3)
                x = x.clamp(-2.5, 2.5)
                # x = x.clamp_(-2, 2)  # Enable when quantized `clamp_` is ready
                x = self.hardtanh(x)
                self.hardtanh_(x)
                x = F.hardtanh(x)
                F.hardtanh_(x)
                return x

        data = (torch.rand((1, 2, 5, 5), dtype=torch.float),)
        # list of node that should occur in order
        node_list = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_function(F.hardtanh_),
            ns.call_method('dequantize')
        ]
        for quant_type in self.static_quant_types:
            m = self.checkGraphModeFxOp(
                M(), data, quant_type, expected_node_list=node_list)

    @skipIfNoFBGEMM
    def test_general_shape_ops(self):
        """ A test that checks dequantize will be swapped for
        all supported general shape ops like aten::flatten
        without actually checking for execution of these ops
        """
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.maxpool1d = torch.nn.MaxPool1d(kernel_size=3)
                self.maxpool2d = torch.nn.MaxPool2d(kernel_size=3)
                self.maxpool3d = torch.nn.MaxPool3d(kernel_size=3)
                self.dropout = torch.nn.Dropout()
                self.conv1 = torch.nn.Conv2d(3, 3, 3)
                self.conv2 = torch.nn.Conv2d(3, 3, 3)
                self.relu = torch.nn.ReLU()

            def forward(self, x):
                x = self.conv1(x)
                # add_scalar
                x = x + 3
                # mul_scalar
                x = x * 3
                # add_scalar_out
                x += 3
                # mul_scalar_out
                x *= 3
                # add_scalar_relu
                x = x + 3
                x = F.relu(x)
                # add_scalar_relu_out
                x += 3
                x = F.relu(x)
                # mul_scalar_relu
                x = x * 3
                x = F.relu(x)
                # mul_scalar_relu_out
                x *= 3
                x = F.relu(x)
                x = self.maxpool1d(x)
                x = self.maxpool2d(x)
                x = self.maxpool3d(x)
                x = torch.flatten(x)
                x = torch.max(x)
                x = torch.min(x)
                x = x.reshape([-1])
                x = x.resize_(1, 1, x.numel())
                x = x.view(-1)
                # prim::ListConstruct
                xs = [x, x]
                # prim::ListUnpack
                x, y = xs
                # prim::TupleConstruct
                xs = (x, x)
                # prim::TupleUnpack
                x, y = xs
                x = x.transpose(1, 2)
                x = x.contiguous()
                x, y = torch.chunk(x, 2)
                x = F.dropout(x)
                x = self.dropout(x)
                x, _ = torch.sort(x)
                x = x.permute(0, 2, 3, 1)
                x = x.repeat_interleave(3, 1)
                x = torch.repeat_interleave(x, 3, 1)
                x = self.relu(x)
                x = F.relu(x)
                x = F.relu(x, inplace=True)
                x = x.relu()
                x.relu_()
                x = x.squeeze(0)
                x.squeeze_(0)
                x = torch.squeeze(x, 0)
                x = x.unsqueeze(0)
                x.unsqueeze_(0)
                x = torch.unsqueeze(x, 0)
                x = x.detach()
                x.detach_()
                x = x.repeat(4, 2)
                y = []
                y.append(x)
                z = torch.stack(y, 0)
                z = [z, z]
                x, _ = z
                x = self.conv2(x)
                return x

        data = torch.rand(1, 3, 10, 10)
        # This model is not executable since we just put all ops
        # in the same forward
        m = M()
        original = symbolic_trace(m)
        # nothing to fuse so skipping the fuse step
        qconfig_dict = {'': default_qconfig}
        prepared = prepare_fx(original, qconfig_dict)
        # not runnable
        quantized = convert_fx(prepared)

        # This checks that the dequantize from the output of first conv
        # is being propagated to the end, so that we don't insert extra
        # observers and also successfully fused two quantized::conv2d
        # patterns
        # one quantize_per_tensor for input
        # check exact counts of quantize and dequantize
        count_check = {
            ns.call_function(torch.quantize_per_tensor) : 1,
            ns.call_method('dequantize') : 1
        }
        order_check = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_module(nnq.Conv2d),
            ns.call_method('dequantize'),
        ]
        self.checkGraphModuleNodes(
            quantized,
            expected_node_occurrence=count_check,
            expected_node_list=order_check)

    @skipIfNoFBGEMM
    def test_general_value_ops(self):
        """ A test that checks correct patterns are produced for
        all supported general value ops like aten::avg_pool2d \
        without actually checking for execution of these ops
        """
        class M(torch.nn.Module):
            def __init__(self):
                super(M, self).__init__()
                self.conv = torch.nn.Conv2d(3, 3, 3)
                self.avg_pool1d = torch.nn.AvgPool1d(3)
                self.avg_pool2d = torch.nn.AvgPool2d(3)
                self.avg_pool3d = torch.nn.AvgPool3d(3)
                self.adaptive_avg_pool1d = torch.nn.AdaptiveAvgPool1d((1))
                self.adaptive_avg_pool2d = torch.nn.AdaptiveAvgPool2d((1, 1))
                self.adaptive_avg_pool3d = torch.nn.AdaptiveAvgPool3d((1, 1, 1))
                self.leaky_relu = torch.nn.LeakyReLU()
                self.hardsigmoid = torch.nn.Hardsigmoid()
                self.sigmoid = torch.nn.Sigmoid()
                self.tanh = torch.nn.Tanh()

            def forward(self, x):
                x = self.conv(x)
                x = self.avg_pool1d(x)
                x = self.avg_pool2d(x)
                x = self.avg_pool3d(x)
                x = self.adaptive_avg_pool1d(x)
                x = self.adaptive_avg_pool2d(x)
                x = self.adaptive_avg_pool3d(x)
                x = F.avg_pool1d(x, 3)
                x = F.avg_pool2d(x, 3)
                x = F.avg_pool3d(x, 3)
                x = F.adaptive_avg_pool1d(x, (1))
                x = F.adaptive_avg_pool2d(x, (1, 1))
                x = F.adaptive_avg_pool3d(x, (1, 1, 1))
                x = torch.mean(x)
                x = torch.mean(x, [2, 3], False)
                x = x.mean()
                x = x.mean([2, 3], True)
                x = F.interpolate(x, 4, mode='nearest')
                x = F.interpolate(x, 4, mode='linear')
                x = self.leaky_relu(x)
                x = F.leaky_relu(x)
                x = F.leaky_relu(x, inplace=True)
                x = x.leaky_relu()
                x.leaky_relu_()
                x = self.hardsigmoid(x)
                x = F.hardsigmoid(x)
                x = F.hardsigmoid(x, inplace=True)
                x = x.hardsigmoid()
                x.hardsigmoid_()
                x = self.sigmoid(x)
                x = torch.sigmoid(x)
                # F.sigmoid is deprecated
                x = x.sigmoid()
                x.sigmoid_()
                x = self.tanh(x)
                # F.tanh is deprecated
                x = torch.tanh(x)
                x = x.tanh()
                x.tanh_()
                x = self.conv(x)
                return x

        # This model is not executable since we just put all ops
        # in the same forward
        m = M()
        original = symbolic_trace(m)
        # nothing to fuse so skipping the fuse step
        qconfig_dict = {'': default_qconfig}
        prepared = prepare_fx(original, qconfig_dict)
        # not runnable
        quantized = convert_fx(prepared)

        # This checks that the dequantize from the output of first conv
        # is being propagated to the end, so that we don't insert extra
        # observers
        # check exact counts of quantize and dequantize
        count_check = {
            ns.call_function(torch.quantize_per_tensor) : 1,
            ns.call_method('dequantize') : 1
        }
        order_check = [
            ns.call_function(torch.quantize_per_tensor),
            ns.call_module(nnq.Conv2d),
            ns.call_module(nnq.Conv2d),
            ns.call_method('dequantize'),
        ]
        self.checkGraphModuleNodes(
            quantized,
            expected_node_occurrence=count_check,
            expected_node_list=order_check)

class TestQuantizeFxModels(QuantizationTestCase):
    def _test_model_impl(
            self, mode, name, model, eager_quantizable_model,
            check_with_eager=True,
            diff_of_quant=None,
            diff_from_eager=None):
        if diff_of_quant is None or diff_from_eager is None:
            diff_of_quant = {}
            diff_from_eager = {}

        if mode not in diff_of_quant or mode not in diff_from_eager:
            diff_of_quant[mode] = {}
            diff_from_eager[mode] = {}

        input_tensor = torch.rand(1, 3, 224, 224)
        input_tensor_inception = torch.rand(1, 3, 299, 299)
        output_value = torch.randint(0, 1, (1,))

        # print('quantizing:', name, ' mode:', mode)
        if name == 'inception_v3':
            input_value = input_tensor_inception
        else:
            input_value = input_tensor

        qconfig = default_qconfig if mode == 'static' else default_qat_qconfig
        qconfig_dict = {'': qconfig}
        graph_module = symbolic_trace(model)
        # print('graph module:', graph_module.src)
        script = torch.jit.script(graph_module)

        # make sure graph module and script module are both runanble
        original_out = graph_module(input_value)
        is_not_tuple_out = not isinstance(original_out, tuple)
        script_out = script(input_value)
        self.assertEqual(
            (original_out - script_out).abs().max(), 0,
            'Reslut of original graph module and script module does not match')

        # set to train just before quantization
        if mode != 'static':
            model.train()

        graph_module = fuse_fx(graph_module)
        prepared = prepare_fx(graph_module, qconfig_dict)

        if mode == 'ddp':
            mp.spawn(run_ddp,
                     args=(world_size, prepared),
                     nprocs=world_size,
                     join=True)
        elif mode == 'qat':
            assert prepared.training, 'prepared must be in training mode for qat'
            optimizer = torch.optim.SGD(prepared.parameters(), lr=0.0001)
            criterion = nn.CrossEntropyLoss()
            train_one_epoch(prepared, criterion, optimizer, [(input_value, output_value)], torch.device('cpu'), 1)
        else:
            for i in range(10):
                prepared(input_value)

        # print('after observation root:', prepared.root)

        qgraph = convert_fx(prepared)
        # print('after quantization root:', qgraph.root)
        # print('after quantization code:', qgraph.src)
        qgraph.eval()
        qgraph_script = torch.jit.script(qgraph)
        # print('quantized and scripted:', qgraph_script.graph)

        qgraph_out = qgraph(input_value)
        qgraph_script = qgraph_script(input_value)

        if is_not_tuple_out:
            diff_of_quant[mode][name] = (original_out - qgraph_out).abs().max()
            assert torch.allclose(qgraph_out, qgraph_script), 'graph, scripted graph'
        else:
            print('tuple output')

        if eager_quantizable_model is not None:
            # comparing to eager mode quantization
            qeager = eager_quantizable_model
            ref_out = qeager(input_value)
            qeager.qconfig = qconfig
            if mode == 'static':
                qeager.fuse_model()
                prepare(qeager, inplace=True)
            else:
                qeager.train()
                qeager.fuse_model()
                prepare_qat(qeager, inplace=True)

            # calibration
            if mode == 'ddp':
                mp.spawn(run_ddp,
                         args=(world_size, qeager),
                         nprocs=world_size,
                         join=True)
            elif mode == 'qat':
                assert qeager.training, 'qeager should be in training mode for qat'
                optimizer = torch.optim.SGD(qeager.parameters(), lr=0.0001)
                train_one_epoch(qeager, criterion, optimizer, [(input_value, output_value)], torch.device('cpu'), 1)
            else:
                for i in range(10):
                    qeager(input_value)

            # print('ref after observation:', qeager)

            convert(qeager, inplace=True)
            qeager.eval()

            # print('ref after quantization:', qeager)
            qeager_out = qeager(input_value)
            qeager_script = torch.jit.script(qeager)
            qscript_out = qeager_script(input_value)
            if is_not_tuple_out:
                diff_from_eager[mode][name] = (qeager_out - qgraph_out).abs().max()
                if check_with_eager:
                    self.assertEqual(diff_from_eager[mode][name], 0,
                                     'Result of graph mode quantization and ' +
                                     'eager mode quantization on model: ' + name +
                                     ' should match. Mode: ' + mode +
                                     ' diff:' + str(diff_from_eager[mode][name]))

    @skip_if_no_torchvision
    @skipIfNoFBGEMM
    @unittest.skip("skip for now since tbb failed")
    def test_torchvision(self):
        from torchvision import models
        from torchvision.models import quantization as quantized_models

        def get_available_classification_models(models):
            return [k for k, v in models.__dict__.items() if callable(v) and k[0].lower() == k[0] and k[0] != "_"]

        model_list = get_available_classification_models(models)
        quantized_model_list = get_available_classification_models(quantized_models)

        no_pretrained_model = set(['shufflenet_v2_x0_5', 'shufflenet_v2_x1_5', 'shufflenet_v2_x2_0'])
        quantized_model_list = set(quantized_model_list) - no_pretrained_model
        # test eager and graph consistency
        model_list = quantized_model_list
        # slice need to be fixed in symbolic tracing(https://github.com/pytorch/pytorch/issues/43511)
        model_list = set(model_list) - {'googlenet', 'inception_v3'}
        # getattr should not be used as node name(https://github.com/pytorch/pytorch/issues/43522)
        model_list -= {'shufflenet_v2_x1_0', 'mobilenet_v2'}

        # mobilenet: dropout error RuntimeError: "bernoulli_scalar_cpu_" not implemented for 'QUInt8'
        # incpetion_v3: looks like there is some problem with AuxLogits
        quantized_not_working = [('qat', 'mobilenet_v2'),
                                 ('qat', 'inception_v3'),
                                 ('static', 'inception_v3')]

        fx_eager_not_matching = ['googlenet',  # because _transform_input is not quantized in eager
                                 'mobilenet_v2']  # because relu6 is replaced as relu in mobilenetv2

        diff_of_quant = {}
        diff_from_eager = {}
        modes = ['static', 'qat']
        options = itertools.product(modes, model_list)
        for mode, name in options:
            pretrained = name in quantized_model_list  # load pretrained model to compare with quantized model
            if name in quantized_model_list:
                if (mode, name) in quantized_not_working:
                    eager_quantizable_model = None
                else:
                    eager_quantizable_model = quantized_models.__dict__[name](pretrained=True, quantize=False).eval().float()
            # compare with eager mode quantized model when it is available
            pretrained = eager_quantizable_model is not None
            model = models.__dict__[name](pretrained=pretrained).eval().float()
            check_with_eager = name not in fx_eager_not_matching
            self._test_model_impl(
                mode, name, model, eager_quantizable_model,
                check_with_eager,
                diff_of_quant, diff_from_eager)

        def print_diffs(diffs):
            for mode, diffs_for_mode in diffs.items():
                print('mode:', mode)
                for name, diff in diffs_for_mode.items():
                    print(name, ':', diff)

        # print('differences between float and quantized')
        # print_diffs(diff_of_quant)
        # print('----------------------')
        # print('differences between graph mode and eager mode')
        # print_diffs(diff_from_eager)
        # print('----------------------')

    @skip_if_no_torchvision
    @skip_if_not_multigpu
    @skipIfNoFBGEMM
    @unittest.skip('TODO: not working yet due to https://github.com/pytorch/pytorch/issues/43513')
    def test_resnet18_ddp(self):
        from torchvision import models
        from torchvision.models import quantization as quantized_models
        eager_quantizable_model = quantized_models.__dict__[name](pretrained=True, quantize=False).eval().float()
        model = models.__dict__[name](pretrained=True).eval().float()
        self._test_model_impl(
            'ddp', 'resnet18', model, eager_quantizable_model)

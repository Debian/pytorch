import sys
import io
import inspect
import math
import random
import re
import copy
import torch
import torch.cuda
import torch.backends.cuda
import tempfile
import unittest
import warnings
import types
import pickle
import textwrap
import operator
import os
import subprocess
from torch.utils.dlpack import from_dlpack, to_dlpack
from torch._six import inf, nan, string_classes, istuple
from itertools import product, combinations, combinations_with_replacement, permutations
from functools import reduce
from functools import partial
from random import randrange
from torch import multiprocessing as mp
from torch.testing._internal.common_methods_invocations import tri_tests_args, run_additional_tri_tests, \
    _compare_trilu_indices
from torch.testing._internal.common_utils import \
    (TestCase, iter_indices, TEST_NUMPY, TEST_SCIPY, TEST_WITH_ASAN, TEST_WITH_ROCM, run_tests,
     skipIfNoLapack, suppress_warnings, IS_WINDOWS, NO_MULTIPROCESSING_SPAWN,
     do_test_dtypes, IS_SANDCASTLE, load_tests, make_tensor, slowTest,
     skipCUDANonDefaultStreamIf, skipCUDAMemoryLeakCheckIf, BytesIOContext,
     skipIfRocm, torch_to_numpy_dtype_dict, skipIfNoSciPy, IS_MACOS, IS_PPC,
     wrapDeterministicFlagAPITest)
from multiprocessing.reduction import ForkingPickler
from torch.testing._internal.common_device_type import instantiate_device_type_tests, \
    skipCPUIfNoLapack, skipCUDAIfNoMagma, skipCUDAIfRocm, skipCUDAIfNotRocm, onlyCUDA, onlyCPU, \
    dtypes, dtypesIfCUDA, dtypesIfCPU, deviceCountAtLeast, skipCUDAIf, precisionOverride, \
    PYTORCH_CUDA_MEMCHECK, largeCUDATensorTest, largeTensorTest, onlyOnCPUAndCUDA, expectedAlertNondeterministic
from typing import Dict, List, Tuple, Union
import torch.backends.quantized
import torch.testing._internal.data
from torch.testing._internal.common_cuda import tf32_on_and_off, tf32_is_not_fp32, with_tf32_off, \
    _get_torch_cuda_version, TEST_MAGMA


# load_tests from torch.testing._internal.common_utils is used to automatically filter tests for
# sharding on sandcastle. This line silences flake warnings
load_tests = load_tests

if TEST_NUMPY:
    import numpy as np

if TEST_SCIPY:
    import scipy
    from scipy import signal

SIZE = 100

AMPERE_OR_ROCM = TEST_WITH_ROCM or tf32_is_not_fp32()

# Wrap base test class into a class to hide it from testing
# See https://stackoverflow.com/a/25695512
class AbstractTestCases:
    # This is intentionally prefixed by an underscore. Otherwise pytest will try to
    # run its methods as test cases.
    class _TestTorchMixin(TestCase):
        def _make_tensors(self, shape, val_range=(-100, 100), use_floating=True, use_integral=True,
                          use_complex=False) -> Dict[str, List[torch.Tensor]]:
            float_types = [torch.double,
                           torch.float]
            int_types = [torch.int64,
                         torch.int32,
                         torch.int16]

            complex_types = [torch.complex64,
                             torch.complex128]

            def make_contiguous(shape, dtype) -> torch.Tensor:
                if dtype in float_types:
                    val = torch.randn(shape, dtype=dtype)
                    val = val * ((val_range[1] - val_range[0]) / (math.pi * 2.0))
                    val = val + ((val_range[1] - val_range[0]) / 2.0)
                    val = torch.clamp(val, min=val_range[0], max=val_range[1])
                    return val
                result = torch.zeros(shape, dtype=dtype)
                result.apply_(lambda x: random.randint(val_range[0], val_range[1]))
                return result

            def make_non_contiguous(shape, dtype) -> torch.Tensor:
                contig = make_contiguous(shape, dtype)
                non_contig = torch.empty(shape + (2, 2), dtype=dtype)[..., 0]
                non_contig = non_contig.select(-1, -1)
                non_contig.copy_(contig)
                self.assertFalse(non_contig.is_contiguous())
                return non_contig

            def make_contiguous_slice(size, dtype) -> torch.Tensor:
                contig = make_contiguous((1, size), dtype)
                non_contig = contig[:1, 1:size - 1]
                self.assertTrue(non_contig.is_contiguous())
                return contig

            types = []
            if use_floating:
                types += float_types
            if use_integral:
                types += int_types
            if use_complex:
                types += complex_types
            tensors: Dict[str, List[torch.Tensor]] = {"cont": [], "noncont": [], "slice": []}
            for dtype in types:
                tensors["cont"].append(make_contiguous(shape, dtype))
                tensors["noncont"].append(make_non_contiguous(shape, dtype))
                tensors["slice"].append(make_contiguous_slice(sum(list(shape)), dtype))

            return tensors

        def test_dir(self):
            dir(torch)

        @wrapDeterministicFlagAPITest
        def test_deterministic_flag(self):
            for deterministic in [True, False]:
                torch.set_deterministic(deterministic)
                self.assertEqual(deterministic, torch.is_deterministic())

            with self.assertRaisesRegex(RuntimeError, r"set_deterministic expects a bool, but got int"):
                torch.set_deterministic(1)

        def test_type_conversion_via_dtype_name(self):
            x = torch.tensor([1])
            self.assertEqual(x.byte().dtype, torch.uint8)
            self.assertEqual(x.bool().dtype, torch.bool)
            self.assertEqual(x.char().dtype, torch.int8)
            self.assertEqual(x.double().dtype, torch.float64)
            self.assertEqual(x.float().dtype, torch.float32)
            self.assertEqual(x.half().dtype, torch.float16)
            self.assertEqual(x.int().dtype, torch.int32)
            self.assertEqual(x.bfloat16().dtype, torch.bfloat16)

        def test_doc_template(self) -> None:
            from torch._torch_docs import __file__ as doc_file
            from torch._torch_docs import multi_dim_common, single_dim_common, factory_common_args, factory_like_common_args

            with open(doc_file, "r") as f:
                doc_strs = f.read()

            for doc_str in re.findall(r'add_docstr\((.*?),.*?("""|\'\'\')(.*?)("""|\'\'\')\)', doc_strs, re.MULTILINE | re.DOTALL):
                for common_args in [multi_dim_common, single_dim_common, factory_common_args, factory_like_common_args]:
                    for k, v in common_args.items():
                        self.assertNotIn(v, doc_str[2], 'The argument description "{}" in {} can be '
                                                        'replaced by {{{}}}'.format(v, doc_str[0], k))

        def test_doc(self):
            checked_types = (types.MethodType, types.FunctionType,
                             types.BuiltinFunctionType, types.BuiltinMethodType)

            def test_namespace(ns, *skips):
                if isinstance(ns, object):
                    ns_name = ns.__class__.__name__
                else:
                    ns_name = ns.__name__
                skip_regexes = []
                for r in skips:
                    if isinstance(r, string_classes):
                        skip_regexes.append(re.compile('^{}$'.format(re.escape(r))))
                    else:
                        skip_regexes.append(r)

                for name in dir(ns):
                    if name.startswith('_'):
                        continue
                    if name in ['real', 'imag']:
                        y = torch.randn(1, dtype=torch.cfloat)
                        var = getattr(y, name)
                    else:
                        var = getattr(ns, name)
                    if not isinstance(var, checked_types):
                        continue
                    doc = var.__doc__
                    has_doc = doc is not None and len(doc.strip()) > 0
                    full_name = ns_name + '.' + name
                    if any(r.match(name) for r in skip_regexes):
                        self.assertFalse(has_doc,
                                         'New docs have been added for {}, please remove '
                                         'it from the skipped list in TestTorch.test_doc'.format(full_name))
                    else:
                        self.assertTrue(has_doc, '{} is missing documentation'.format(full_name))

            # FIXME: All of the following should be marked as expected failures
            # so that it is easier to tell when missing has been added.
            # FIXME: fix all the skipped ones below!
            test_namespace(torch.randn(1),
                           'as_strided_',
                           re.compile('^clamp_(min|max)_?$'),
                           'coalesce',
                           'is_coalesced',
                           'is_distributed',
                           'is_nonzero',
                           'is_same_size',
                           'log_softmax',
                           'map2_',
                           'new',
                           'reinforce',
                           'relu',
                           'relu_',
                           'prelu',
                           'resize',
                           'resize_as',
                           'smm',
                           'softmax',
                           'split_with_sizes',
                           'unsafe_split_with_sizes',
                           'sspaddmm',
                           'to_dense',
                           'sparse_resize_',
                           'sparse_resize_and_clear_',
                           )
            test_namespace(torch.nn)
            test_namespace(torch.nn.functional, 'assert_int_or_pair')
            # TODO: add torch.* tests when we have proper namespacing on ATen functions
            # test_namespace(torch)

        def test_linear_algebra_scalar_raises(self) -> None:
            m = torch.randn(5, 5)
            v = torch.randn(5)
            s = torch.tensor(7)
            self.assertRaises(RuntimeError, lambda: torch.mv(m, s))
            self.assertRaises(RuntimeError, lambda: torch.addmv(v, m, s))

        @unittest.skipIf(not TEST_SCIPY, "Scipy not found")
        def test_mvlgamma(self):
            from scipy.special import multigammaln
            for d in range(1, 5):
                input = torch.empty(10).uniform_(d, 10)
                res_torch = torch.mvlgamma(input, d)
                res_scipy = multigammaln(input.numpy(), d)
                self.assertEqual(res_torch.numpy(), res_scipy, atol=1e-5, rtol=0)

        def test_mvlgamma_argcheck(self):
            def run_test(d):
                input = torch.linspace((d - 2) / 2, 10, 10)
                torch.mvlgamma(input, d)

            with self.assertRaisesRegex(RuntimeError, r"All elements must be greater than \(p-1\)/2"):
                run_test(3)

        def test_msnpu_error(self):
            with self.assertRaisesRegex(RuntimeError, "support for msnpu"):
                torch.zeros(1, device=torch.device('msnpu'))

        def test_polygamma_neg(self):
            with self.assertRaisesRegex(RuntimeError, r'polygamma\(n, x\) does not support negative n\.'):
                torch.polygamma(-1, torch.tensor([1.0, 2.0]))

        def test_has_storage(self):
            self.assertIsNotNone(torch.Tensor().storage())
            self.assertIsNotNone(torch.Tensor(0).storage())
            self.assertIsNotNone(torch.Tensor([]).storage())
            self.assertIsNotNone(torch.Tensor().clone().storage())
            self.assertIsNotNone(torch.Tensor([0, 0, 0]).nonzero().storage())
            self.assertIsNotNone(torch.Tensor().new().storage())

        def test_dim_reduction_uint8_overflow(self):
            example = [[-1, 2, 1], [5, 3, 6]]
            x = torch.tensor(example, dtype=torch.uint8)
            self.assertEqual(x.sum(dtype=torch.uint8).item(), 16)
            self.assertEqual(x.sum(0, dtype=torch.uint8), torch.tensor([4, 5, 7], dtype=torch.uint8))
            self.assertEqual(x.sum(1, dtype=torch.uint8), torch.tensor([2, 14], dtype=torch.uint8))
            y = torch.tensor(example, dtype=torch.uint8)
            torch.sum(x, 0, out=y)
            self.assertEqual(x.sum(0, dtype=torch.uint8), y)

        def test_dim_reduction_less_than_64(self):
            sizes = [1] * 65
            x = torch.randn(sizes)
            ops = [torch.mean, torch.sum, torch.nansum, torch.std, torch.logsumexp, torch.std, torch.var,
                   torch.amin, torch.amax, torch.norm]
            for op in ops:
                with self.assertRaisesRegex(RuntimeError, "only tensors with up to 64 dims are supported"):
                    op(x, 64)
                with self.assertRaisesRegex(RuntimeError, "only tensors with up to 64 dims are supported"):
                    op(x, -1)

        @unittest.skipIf(not TEST_SCIPY, "Scipy not found")
        def test_logsumexp(self):
            from scipy.special import logsumexp
            a = torch.randn(5, 4)
            a[0, 0] = inf
            a[1, :] = -inf
            actual = a.logsumexp(1)
            expected = logsumexp(a.numpy(), 1)
            self.assertEqual(expected.shape, actual.shape)
            self.assertEqual(expected, actual)
            # check that out is actually inplace
            b = torch.zeros(5, 2)
            c = b[:, 0]
            torch.logsumexp(a, 1, out=c)
            self.assertEqual(expected, b[:, 0])

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_cpu_parallel(self):
            # To use parallel branches we'll need to compare on tensors
            # that are relatively large. Even if this is run on a single
            # core machine these tests will still give you signal on
            # the correctness

            def _run_test(size):
                for dim in range(len(size) + 1):
                    nv = np.round(np.random.rand(*size))  # 0s and 1s
                    tv = torch.from_numpy(nv)
                    # Parallelisim is only used if numel is
                    # larger than grainsize defined in Parallel.h
                    self.assertTrue(tv.numel() > 32768)
                    if dim == len(size):
                        nvs = nv.sum()
                        tvs = tv.sum()
                    else:
                        nvs = nv.sum(dim)
                        tvs = tv.sum(dim)
                    diff = np.abs(nvs - tvs.numpy()).sum()
                    self.assertEqual(diff, 0)

            _run_test([2, 3, 3, 3, 3, 2, 2, 3, 2, 3, 2, 3, 3])
            _run_test([4, 4, 4, 4, 4, 4, 4, 4, 4, 4])
            _run_test([1, 32 * 8 * 32 * 8])
            _run_test([1, 32770])

        def _testCSelection(self, torchfn, mathfn):
            # Two tensors
            size = (100, 100)
            a = torch.rand(*size)
            b = torch.rand(*size)
            c = torchfn(a, b)
            expected_c = torch.zeros(*size)
            expected_c.map2_(a, b, lambda _, a, b: mathfn(a, b))
            self.assertEqual(expected_c, c, atol=0, rtol=0)

        def test_max_elementwise(self):
            self._testCSelection(torch.max, max)

        def test_min_elementwise(self):
            self._testCSelection(torch.min, min)

        def test_all_any(self):
            def test(size):
                x = torch.ones(*size).byte()
                self.assertTrue(x.all())
                self.assertTrue(x.any())

                x[3] = 0
                self.assertFalse(x.all())
                self.assertTrue(x.any())

                x.zero_()
                self.assertFalse(x.all())
                self.assertFalse(x.any())

                x.fill_(2)
                self.assertTrue(x.all())
                self.assertTrue(x.any())

                x = torch.ones(*size).bool()
                self.assertTrue(x.all())
                self.assertTrue(x.any())

                x[3] = False
                self.assertFalse(x.all())
                self.assertTrue(x.any())

            test((10,))
            test((5, 5))

        def test_where_invalid_device(self):
            if torch.cuda.is_available():
                for devices in [('cpu', 'cuda', 'cuda'), ('cuda', 'cpu', 'cpu'),
                                ('cuda', 'cpu', 'cuda'), ('cpu', 'cuda', 'cpu')]:
                    condition = torch.rand(16, device=devices[0])
                    x = torch.rand(16, device=devices[1])
                    y = torch.rand(16, device=devices[2])
                    with self.assertRaisesRegex(RuntimeError,
                                                "Expected condition, x and y to be on the same device"):
                        torch.where(condition, x, y)

        def test_where_bool_tensor(self):
            for d in torch.testing.get_all_device_types():
                a = torch.tensor([True, False], device=d)
                res = torch.where(a > 0)
                self.assertEqual(1, len(res))

        def test_where_tensor(self):
            def rand_tensor(size, dtype, device):
                if dtype.is_floating_point or dtype.is_complex:
                    return torch.rand(size=size, dtype=dtype, device=device)
                elif dtype == torch.uint8:
                    return torch.randint(1, 5, size=size, dtype=dtype, device=device)
                elif dtype == torch.bool:
                    return torch.randint(0, 1, size=size, dtype=dtype, device=device).bool()
                else:
                    return torch.randint(-5, 5, size=size, dtype=dtype, device=device)

            def get_tensor(size, dtype, device, contiguous):
                if not contiguous and len(size) < 2:
                    raise RuntimeError("Unable to generate non contiguous tensor with size < 2")
                t = rand_tensor(size, dtype, device)
                if contiguous:
                    return t
                else:
                    return t.transpose(0, 1)

            height = 5
            width = 5
            for device in torch.testing.get_all_device_types():
                for dt1 in torch.testing.get_all_math_dtypes(device):
                    for dt2 in torch.testing.get_all_math_dtypes(device):
                        for contiguous in [True, False]:
                            x1 = get_tensor((height, width), dt1, device, contiguous)
                            x2 = get_tensor((height, width), dt2, device, contiguous)
                            if dt1 != dt2:
                                self.assertRaisesRegex(RuntimeError, "expected scalar type", lambda: torch.where(x1 == 1, x1, x2))
                            else:
                                if x1.is_floating_point():
                                    condition = (x1 < 0.5)
                                elif x1.is_complex():
                                    condition = (x1.abs() < 0.5)
                                else:
                                    condition = (x1 == 1)
                                expected = condition.to(x1.dtype) * x1 + (~condition).to(x2.dtype) * x2
                                result = torch.where(condition, x1, x2)
                                self.assertEqual(expected, result)

        def test_all_any_with_dim(self):
            def test(x):
                r1 = x.prod(dim=0, keepdim=False).byte()
                r2 = x.all(dim=0, keepdim=False)
                self.assertEqual(r1.shape, r2.shape)
                self.assertTrue((r1 == r2).all())

                r3 = x.sum(dim=1, keepdim=True).clamp(0, 1).byte()
                r4 = x.any(dim=1, keepdim=True)
                self.assertEqual(r3.shape, r4.shape)
                self.assertTrue((r3 == r4).all())

            test(torch.ByteTensor([[0, 0, 0],
                                   [0, 0, 1],
                                   [0, 1, 1],
                                   [1, 1, 1]]))

        def test_numpy_args(self):
            x1 = torch.randn(10)
            x2 = torch.randn(10)
            res1 = torch.add(input=x1, other=x2)
            res2 = torch.add(x1=x1, x2=x2)
            self.assertEqual(res1, res2)

            x1 = torch.randn(10, 10, 10)
            res1 = x1.sum(dim=(0, 2), keepdim=True)
            res2 = x1.sum(axis=(0, 2), keepdims=True)
            self.assertEqual(res1, res2)

        def _assert_matches_numpy(self, t, n):
            self.assertEqual(n.shape, t.shape)
            if t.dtype == torch.float:
                self.assertEqual(n, t, rtol=1e-03, atol=1e-05, equal_nan=True)
            else:
                self.assertEqual(n, t, equal_nan=True)

        def _test_dim_ops(self, pytorch_op, numpy_op,
                          use_floating=True, use_integral=True, use_complex=False):
            def do_one(tensors_dict, dim):
                for category, tensors in tensors_dict.items():
                    if category == "slice":
                        dim = 0
                    for tensor in tensors:
                        # we have no control over NumPy warnings...
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            expected = numpy_op(tensor.numpy(), dim)
                        actual = pytorch_op(tensor, dim)
                        self._assert_matches_numpy(actual, expected)
                        if torch.cuda.is_available():
                            self._assert_matches_numpy(pytorch_op(tensor.cuda(),
                                                                  dim).cpu(),
                                                       expected)
            do_one(self._make_tensors((5, 400000), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 1)
            do_one(self._make_tensors((3, 5, 7), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 0)
            do_one(self._make_tensors((3, 5, 7), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 1)
            do_one(self._make_tensors((3, 5, 7), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 2)
            do_one(self._make_tensors((100000, ), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), -1)
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 0)
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 1)
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), 2)
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), (1, 2))
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), (1, -1))
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), (0, 2))
            do_one(self._make_tensors((50, 50, 50), use_floating=use_floating,
                                      use_integral=use_integral, use_complex=use_complex), (0, 2, 1))

        @slowTest
        @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
        def test_sum_dim(self):
            self._test_dim_ops(
                lambda t, d: t.sum(d),
                lambda n, d: n.sum(d),
                use_floating=True, use_integral=True, use_complex=True)

        @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
        def test_mean_dim(self):
            self._test_dim_ops(
                lambda t, d: t.mean(d),
                lambda n, d: n.mean(d),
                use_integral=False)

        @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
        def test_std_dim(self):
            for unbiased in [False, True]:
                self._test_dim_ops(
                    lambda t, d: t.std(d, unbiased=unbiased),
                    lambda n, d: n.std(d, ddof=1 if unbiased else 0),
                    use_integral=False)

        @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
        def test_var_dim(self):
            for unbiased in [False, True]:
                self._test_dim_ops(
                    lambda t, d: t.var(d, unbiased=unbiased),
                    lambda n, d: n.var(d, ddof=1 if unbiased else 0),
                    use_integral=False)

        @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
        @unittest.skipIf(not TEST_SCIPY, 'Scipy not found')
        def test_logsumexp_dim(self):
            from scipy.special import logsumexp
            self._test_dim_ops(
                lambda t, d: t.logsumexp(d),
                lambda n, d: logsumexp(n, d),
                use_integral=False)

        def _test_reduce_integer_upcast(self, fn, has_out=True, test_complex=True):
            shape = (3, 4, 5)
            reduced_shape = fn(torch.ones(shape)).shape

            def _test_out(dtype, other_dtype):
                out = torch.ones(reduced_shape, dtype=dtype)
                result = fn(x, out=out)
                self.assertIs(out.dtype, result.dtype)
                self.assertEqual(fn(x.to(dtype)), result, exact_dtype=False)
                result = fn(x, out=out, dtype=dtype)
                self.assertIs(out.dtype, result.dtype)
                self.assertEqual(fn(x.to(dtype)), result, exact_dtype=False)
                # 'out' is favored over dtype, check error
                self.assertRaises(RuntimeError, lambda: fn(x, out=out, dtype=other_dtype))

            for dtype in [dtype for dtype in torch.testing.get_all_math_dtypes('cpu') if dtype != torch.float16]:
                x = torch.ones(shape, dtype=dtype)
                expected_dtype = dtype if dtype.is_floating_point or dtype.is_complex else torch.int64
                self.assertIs(expected_dtype, fn(x).dtype)
                self.assertEqual(fn(x.to(expected_dtype)), fn(x))

                if dtype.is_floating_point:
                    other_dtype = torch.float32 if dtype == torch.float64 else torch.float64
                elif dtype.is_complex:
                    other_dtype = torch.complex64 if dtype == torch.complex128 else torch.complex128
                else:
                    other_dtype = torch.int32 if dtype != torch.int32 else torch.int16
                self.assertIs(other_dtype, fn(x, dtype=other_dtype).dtype)
                self.assertEqual(fn(x.to(other_dtype)), fn(x, dtype=other_dtype), exact_dtype=False)

                # test mixed int/float/complex
                if dtype.is_floating_point:
                    mixed_dtypes = [torch.int32, torch.complex64]
                elif dtype.is_complex:
                    mixed_dtypes = [torch.int32, torch.float32]
                else:
                    mixed_dtypes = [torch.float32, torch.complex64]

                for mixed_dtype in mixed_dtypes:
                    self.assertIs(mixed_dtype, fn(x, dtype=mixed_dtype).dtype)
                    self.assertEqual(fn(x.to(mixed_dtype)), fn(x, dtype=mixed_dtype), exact_dtype=False)

                    if has_out:
                        _test_out(dtype, other_dtype)
                        _test_out(dtype, mixed_dtype)

        def test_sum_integer_upcast(self):
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.sum(x, **kwargs), False)
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.sum(x, 0, **kwargs))

        def test_prod_integer_upcast(self):
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.prod(x, **kwargs), False)
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.prod(x, 0, **kwargs))

        def test_cumsum_integer_upcast(self):
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.cumsum(x, 0, **kwargs))

        def test_cumprod_integer_upcast(self):
            self._test_reduce_integer_upcast(lambda x, **kwargs: torch.cumprod(x, 0, **kwargs))

        def test_cross_validation(self):
            self.assertRaisesRegex(
                RuntimeError, "inconsistent tensors dimensions",
                lambda: torch.cross(torch.rand(100, 3), torch.rand(100, 3, 10)))
            self.assertRaisesRegex(
                RuntimeError, "inconsistent tensors sizes",
                lambda: torch.cross(torch.rand(5, 3), torch.rand(3, 5)))
            self.assertRaisesRegex(
                RuntimeError, "no dimension of size 3 in input",
                lambda: torch.cross(torch.rand(5, 4), torch.rand(5, 4)))
            self.assertRaisesRegex(
                RuntimeError, "dimension 0 does not have size 3",
                lambda: torch.cross(torch.rand(5, 4, 3), torch.rand(5, 4, 3), dim=0))
            self.assertRaisesRegex(
                RuntimeError, "dimension -1 does not have size 3",
                lambda: torch.cross(torch.rand(5, 3, 4), torch.rand(5, 3, 4), dim=-1))
            self.assertRaisesRegex(
                IndexError, "Dimension out of range",
                lambda: torch.cross(torch.rand(5, 3, 4), torch.rand(5, 3, 4), dim=-5))

        def test_dtypes(self):
            all_dtypes = torch.testing.get_all_dtypes()
            do_test_dtypes(self, all_dtypes, torch.strided, torch.device('cpu'))
            if torch.cuda.is_available():
                all_dtypes.remove(torch.bfloat16)  # Remove once _th_zero_ is enabled on cuda for bfloat16
                do_test_dtypes(self, all_dtypes, torch.strided, torch.device('cuda:0'))

        def test_copy_dtypes(self):
            all_dtypes = torch.testing.get_all_dtypes()
            for dtype in all_dtypes:
                copied_dtype = copy.deepcopy(dtype)
                self.assertIs(dtype, copied_dtype)

        def test_copy_transpose(self):
            x = torch.arange(100 * 100, dtype=torch.float).reshape(100, 100).t()
            y = torch.empty(100, 100, dtype=torch.float)
            y.copy_(x)
            self.assertEqual(y[:, 0], range(100))
            self.assertEqual(y[:, 40], range(4000, 4100))

            y = torch.empty(100, 100, dtype=torch.double)
            y.copy_(x)
            self.assertEqual(y[:, 0], range(100))
            self.assertEqual(y[:, 40], range(4000, 4100))

            # Validates regression reported in https://github.com/pytorch/pytorch/issues/45269
            x = torch.arange(100 * 100).reshape(100, 100).to(dtype=torch.cfloat).t()
            y = torch.empty(100, 100, dtype=torch.cfloat)
            y.copy_(x)
            self.assertEqual(y[:, 0], range(100))
            self.assertEqual(y[:, 40], range(4000, 4100))

        def test_device(self):
            cpu = torch.device('cpu')
            self.assertEqual('cpu', str(cpu))
            self.assertEqual('cpu', cpu.type)
            self.assertEqual(None, cpu.index)

            cpu0 = torch.device('cpu:0')
            self.assertEqual('cpu:0', str(cpu0))
            self.assertEqual('cpu', cpu0.type)
            self.assertEqual(0, cpu0.index)

            cpu0 = torch.device('cpu', 0)
            self.assertEqual('cpu:0', str(cpu0))
            self.assertEqual('cpu', cpu0.type)
            self.assertEqual(0, cpu0.index)

            cuda = torch.device('cuda')
            self.assertEqual('cuda', str(cuda))
            self.assertEqual('cuda', cuda.type)
            self.assertEqual(None, cuda.index)

            cuda1 = torch.device('cuda:1')
            self.assertEqual('cuda:1', str(cuda1))
            self.assertEqual('cuda', cuda1.type)
            self.assertEqual(1, cuda1.index)

            cuda1 = torch.device('cuda', 1)
            self.assertEqual('cuda:1', str(cuda1))
            self.assertEqual('cuda', cuda1.type)
            self.assertEqual(1, cuda1.index)

            cuda90 = torch.device('cuda', 90)
            self.assertEqual('cuda:90', str(cuda90))
            self.assertEqual('cuda', cuda90.type)
            self.assertEqual(90, cuda90.index)

            cuda23333 = torch.device('cuda', 23333)
            self.assertEqual('cuda:23333', str(cuda23333))
            self.assertEqual('cuda', cuda23333.type)
            self.assertEqual(23333, cuda23333.index)

            self.assertRaises(RuntimeError, lambda: torch.device('cpu:-1'))
            self.assertRaises(RuntimeError, lambda: torch.device('cpu:1'))
            self.assertRaises(RuntimeError, lambda: torch.device('cpu', -1))
            self.assertRaises(RuntimeError, lambda: torch.device('cpu', 1))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:-1'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2 '))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda: 2'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2 2'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2.'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2?'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:?2'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2.232'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2 cuda:3'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2+cuda:3'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda:2cuda:3'))
            self.assertRaises(RuntimeError, lambda: torch.device('cuda', -1))
            self.assertRaises(RuntimeError, lambda: torch.device(-1))

            self.assertRaises(RuntimeError, lambda: torch.device('other'))
            self.assertRaises(RuntimeError, lambda: torch.device('other:0'))

            device_set = {'cpu', 'cpu:0', 'cuda', 'cuda:0', 'cuda:1', 'cuda:10', 'cuda:100'}
            device_hash_set = set()
            for device in list(device_set):
                device_hash_set.add(hash(torch.device(device)))
            self.assertEqual(len(device_set), len(device_hash_set))

        def test_to(self):
            def test_copy_behavior(t, non_blocking=False):
                self.assertIs(t, t.to(t, non_blocking=non_blocking))
                self.assertIs(t, t.to(t.dtype, non_blocking=non_blocking))
                self.assertIs(t, t.to(torch.empty_like(t), non_blocking=non_blocking))
                self.assertIsNot(t, t.to(t, non_blocking=non_blocking, copy=True))
                self.assertIsNot(t, t.to(t.dtype, non_blocking=non_blocking, copy=True))
                self.assertIsNot(t, t.to(torch.empty_like(t), non_blocking=non_blocking, copy=True))

                devices = [t.device]
                if t.device.type == 'cuda':
                    if t.device.index == -1:
                        devices.append('cuda:{}'.format(torch.cuda.current_device()))
                    elif t.device.index == torch.cuda.current_device():
                        devices.append('cuda')
                for device in devices:
                    self.assertIs(t, t.to(device, non_blocking=non_blocking))
                    self.assertIs(t, t.to(device, t.dtype, non_blocking=non_blocking))
                    self.assertIsNot(t, t.to(device, non_blocking=non_blocking, copy=True))
                    self.assertIsNot(t, t.to(device, t.dtype, non_blocking=non_blocking, copy=True))

            a = torch.tensor(5)
            test_copy_behavior(a)
            self.assertEqual(a.device, a.to('cpu').device)
            self.assertEqual(a.device, a.to('cpu', dtype=torch.float32).device)
            self.assertIs(torch.float32, a.to('cpu', dtype=torch.float32).dtype)
            self.assertEqual(a.device, a.to(torch.float32).device)
            self.assertIs(torch.float32, a.to(dtype=torch.float32).dtype)
            self.assertEqual(a.data_ptr(), a.to('cpu').data_ptr())
            self.assertEqual(a.data_ptr(), a.to(dtype=a.dtype, device=a.device, copy=False).data_ptr())
            self.assertEqual(a.data_ptr(), a.to('cpu', copy=False).data_ptr())
            self.assertNotEqual(a.data_ptr(), a.to('cpu', copy=True).data_ptr())

            if torch.cuda.is_available():
                for non_blocking in [True, False]:
                    for cuda in ['cuda', 'cuda:0' if torch.cuda.device_count() == 1 else 'cuda:1']:
                        b = torch.tensor(5., device=cuda)
                        test_copy_behavior(b, non_blocking)
                        self.assertEqual(b.device, b.to(cuda, non_blocking=non_blocking).device)
                        self.assertEqual(a.device, b.to('cpu', non_blocking=non_blocking).device)
                        self.assertEqual(b.device, a.to(cuda, non_blocking=non_blocking).device)
                        self.assertIs(torch.int32, b.to('cpu', dtype=torch.int32, non_blocking=non_blocking).dtype)
                        self.assertEqual(a.device, b.to('cpu', dtype=torch.int32, non_blocking=non_blocking).device)
                        self.assertIs(torch.int32, b.to(dtype=torch.int32).dtype)
                        self.assertEqual(b.device, b.to(dtype=torch.int32).device)

        def test_to_with_tensor(self):
            a = torch.tensor(5)
            self.assertEqual(a.device, a.to(a).device)

            if torch.cuda.is_available():
                for non_blocking in [True, False]:
                    for cuda in ['cuda', 'cuda:0' if torch.cuda.device_count() == 1 else 'cuda:1']:
                        b = torch.tensor(5., device=cuda)
                        self.assertEqual(b.device, b.to(b, non_blocking=non_blocking).device)
                        self.assertEqual(a.device, b.to(a, non_blocking=non_blocking).device)
                        self.assertEqual(b.device, a.to(b, non_blocking=non_blocking).device)

        def test_dtype_out_match(self):
            d = torch.autograd.Variable(torch.DoubleTensor(2, 3))
            self.assertRaises(RuntimeError, lambda: torch.zeros((2, 3), out=d, dtype=torch.float32))

        def test_as_subclass(self):
            class SubTensor(torch.Tensor):
                member_var = object()

            t0 = torch.tensor(0)
            t1 = torch.tensor([1, 2])
            t2 = torch.tensor([[3, 4], [5, 6]])

            s0 = t0.as_subclass(SubTensor)
            s1 = t1.as_subclass(SubTensor)
            s2 = t2.as_subclass(SubTensor)

            # Check that the correct type is returned.
            self.assertTrue(type(s0) is SubTensor)
            self.assertTrue(type(s1) is SubTensor)
            self.assertTrue(type(s2) is SubTensor)

            # Check that the data is equal.
            self.assertEqual(t0, s0)
            self.assertEqual(t1, s1)
            self.assertEqual(t2, s2)

            t0[()] = 1
            t1[1] = 3
            t2[1, 1] = 7

            # Check that the data is equal even after modification.
            self.assertEqual(t0, s0)
            self.assertEqual(t1, s1)
            self.assertEqual(t2, s2)

            # Check that member variables are passed through.
            self.assertTrue(s0.member_var is SubTensor.member_var)
            self.assertTrue(s1.member_var is SubTensor.member_var)
            self.assertTrue(s2.member_var is SubTensor.member_var)

            # Test that autograd is propagated.
            t = torch.tensor(5, dtype=torch.float32, requires_grad=True)

            # Run a calculation on the tensor.
            exp_t = torch.exp(t)

            # Cast exp_t to a subclass.
            exp_s = exp_t.as_subclass(SubTensor)

            # Make sure that t.grad was initially None
            self.assertTrue(t.grad is None)

            # Run the autograd calculation.
            exp_s.backward()

            # Make sure autograd was propagated to the original tensor
            # declared with requires_grad.
            self.assertTrue(t.grad is not None)

        def test_type(self):
            x = torch.randn(3, 3).double()
            self.assertEqual(x.type('torch.FloatTensor').dtype, torch.float32)
            self.assertEqual(x.type(torch.FloatTensor).dtype, torch.float32)
            self.assertEqual(x.int().type(torch.Tensor).dtype, torch.get_default_dtype())
            self.assertEqual(x.type(torch.int32).dtype, torch.int32)

        def test_qengine(self):
            qengines = torch.backends.quantized.supported_engines
            original_qe = torch.backends.quantized.engine
            for qe in qengines:
                torch.backends.quantized.engine = qe
                assert torch.backends.quantized.engine == qe, 'qengine not set successfully'
            torch.backends.quantized.engine = original_qe

        def test_renorm(self):
            m1 = torch.randn(10, 5)
            res1 = torch.Tensor()

            def renorm(matrix, value, dim, max_norm):
                m1 = matrix.transpose(dim, 0).contiguous()
                # collapse non-dim dimensions.
                m2 = m1.clone().resize_(m1.size(0), int(math.floor(m1.nelement() / m1.size(0))))
                norms = m2.norm(value, 1, True)
                # clip
                new_norms = norms.clone()
                new_norms[torch.gt(norms, max_norm)] = max_norm
                new_norms.div_(norms.add_(1e-7))
                # renormalize
                m1.mul_(new_norms.expand_as(m1))
                return m1.transpose(dim, 0)

            # note that the axis fed to torch.renorm is different (2~=1)
            maxnorm = m1.norm(2, 1).mean()
            m2 = renorm(m1, 2, 1, maxnorm)
            m1.renorm_(2, 1, maxnorm)
            self.assertEqual(m1, m2, atol=1e-5, rtol=0)
            self.assertEqual(m1.norm(2, 0), m2.norm(2, 0), atol=1e-5, rtol=0)

            m1 = torch.randn(3, 4, 5)
            m2 = m1.transpose(1, 2).contiguous().clone().resize_(15, 4)
            maxnorm = m2.norm(2, 0).mean()
            m2 = renorm(m2, 2, 1, maxnorm)
            m1.renorm_(2, 1, maxnorm)
            m3 = m1.transpose(1, 2).contiguous().clone().resize_(15, 4)
            self.assertEqual(m3, m2)
            self.assertEqual(m3.norm(2, 0), m2.norm(2, 0))

        def _spawn_method(self, method, arg):
            try:
                mp.set_start_method('spawn')
            except RuntimeError:
                pass
            with mp.Pool(1) as pool:
                out: list = pool.map(method, [arg])
                self.assertTrue(out[0])

        @staticmethod
        def _test_multinomial_invalid_probs(probs):
            try:
                # n_sample = 1 is a special case, test n_sample=2 which is more general
                torch.multinomial(probs.to('cpu'), 2)
                return False  # Should not be reached
            except RuntimeError as e:
                return 'probability tensor contains either `inf`, `nan` or element < 0' in str(e)

        @slowTest
        @unittest.skipIf(NO_MULTIPROCESSING_SPAWN, "Disabled for environments that \
                         don't support multiprocessing with spawn start method")
        @unittest.skipIf(IS_WINDOWS, 'FIXME: CUDA OOM error on Windows')
        def test_multinomial_invalid_probs(self):
            test_method = AbstractTestCases._TestTorchMixin._test_multinomial_invalid_probs
            self._spawn_method(test_method, torch.Tensor([1, -1, 1]))
            self._spawn_method(test_method, torch.Tensor([1, inf, 1]))
            self._spawn_method(test_method, torch.Tensor([1, -inf, 1]))
            self._spawn_method(test_method, torch.Tensor([1, 1, nan]))

        def test_broadcast_empty(self):
            # empty + empty
            self.assertRaises(RuntimeError, lambda: torch.randn(5, 0) + torch.randn(0, 5))
            self.assertEqual(torch.randn(5, 0), torch.randn(0) + torch.randn(5, 0))
            self.assertEqual(torch.randn(5, 0, 0), torch.randn(0) + torch.randn(5, 0, 1))

            # scalar + empty
            self.assertEqual(torch.randn(5, 0, 6), torch.randn(()) + torch.randn(5, 0, 6))

            # non-empty, empty
            self.assertEqual(torch.randn(0), torch.randn(0) + torch.randn(1))
            self.assertEqual(torch.randn(0, 7, 0, 6, 5, 0, 7),
                             torch.randn(0, 7, 0, 6, 5, 0, 1) + torch.randn(1, 1, 5, 1, 7))
            self.assertRaises(RuntimeError, lambda: torch.randn(7, 0) + torch.randn(2, 1))

        def test_scalars_as_floats(self):
            "zero-dim variables that don't require grad should bind to scalar arguments"
            x = torch.tensor(2.)
            y = torch.tensor(3.)
            # 3 + (3 * 3) * 2
            self.assertEqual(y.addcmul(y, y, value=x), 21)

            x = torch.tensor(2., requires_grad=True)
            self.assertRaises(Exception, lambda: y.addcmul(y, y, value=x))

        def test_copy_broadcast(self):
            torch.zeros(5, 6).copy_(torch.zeros(6))
            self.assertRaises(RuntimeError, lambda: torch.zeros(5, 6).copy_(torch.zeros(30)))

        def test_copy_many_to_one(self):
            # Testing in-place copy where it attempt to write from many memory
            # storage to a single storage would cause RuntimeError to be thrown
            self.assertRaises(RuntimeError, lambda: torch.zeros(1, 6).expand(5, 6).copy_(torch.zeros(5, 6)))

        def assertIsOrdered(self, order, x, mxx, ixx, task):
            SIZE = 4
            if order == 'descending':
                def check_order(a, b):
                    # `a != a` because we put NaNs
                    # at the end of ascending sorted lists,
                    # and the beginning of descending ones.
                    return a != a or a >= b
            elif order == 'ascending':
                def check_order(a, b):
                    # see above
                    return b != b or a <= b
            else:
                error('unknown order "{}", must be "ascending" or "descending"'.format(order))

            are_ordered = True
            for j, k in product(range(SIZE), range(1, SIZE)):
                self.assertTrue(check_order(mxx[j][k - 1], mxx[j][k]),
                                'torch.sort ({}) values unordered for {}'.format(order, task))

            seen = set()
            indicesCorrect = True
            size = x.size(x.dim() - 1)
            for k in range(size):
                seen.clear()
                for j in range(size):
                    self.assertEqual(x[k][ixx[k][j]], mxx[k][j],
                                     msg='torch.sort ({}) indices wrong for {}'.format(order, task))
                    seen.add(ixx[k][j])
                self.assertEqual(len(seen), size)

        def test_sort(self):
            SIZE = 4
            x = torch.rand(SIZE, SIZE)
            res1val, res1ind = torch.sort(x)

            # Test use of result tensor
            res2val = torch.Tensor()
            res2ind = torch.LongTensor()
            torch.sort(x, out=(res2val, res2ind))
            self.assertEqual(res1val, res2val, atol=0, rtol=0)
            self.assertEqual(res1ind, res2ind, atol=0, rtol=0)
            self.assertEqual(torch.argsort(x), res1ind)
            self.assertEqual(x.argsort(), res1ind)

            # Test sorting of random numbers
            self.assertIsOrdered('ascending', x, res2val, res2ind, 'random')

            # Test simple sort
            self.assertEqual(
                torch.sort(torch.Tensor((50, 40, 30, 20, 10)))[0],
                torch.Tensor((10, 20, 30, 40, 50)),
                atol=0, rtol=0
            )

            # Test that we still have proper sorting with duplicate keys
            x = torch.floor(torch.rand(SIZE, SIZE) * 10)
            torch.sort(x, out=(res2val, res2ind))
            self.assertIsOrdered('ascending', x, res2val, res2ind, 'random with duplicate keys')

            # DESCENDING SORT
            x = torch.rand(SIZE, SIZE)
            res1val, res1ind = torch.sort(x, x.dim() - 1, True)

            # Test use of result tensor
            res2val = torch.Tensor()
            res2ind = torch.LongTensor()
            torch.sort(x, x.dim() - 1, True, out=(res2val, res2ind))
            self.assertEqual(res1val, res2val, atol=0, rtol=0)
            self.assertEqual(res1ind, res2ind, atol=0, rtol=0)
            self.assertEqual(torch.argsort(x, x.dim() - 1, True), res1ind)
            self.assertEqual(x.argsort(x.dim() - 1, True), res1ind)

            # Test sorting of random numbers
            self.assertIsOrdered('descending', x, res2val, res2ind, 'random')

            # Test simple sort task
            self.assertEqual(
                torch.sort(torch.Tensor((10, 20, 30, 40, 50)), 0, True)[0],
                torch.Tensor((50, 40, 30, 20, 10)),
                atol=0, rtol=0
            )

            # Test that we still have proper sorting with duplicate keys
            self.assertIsOrdered('descending', x, res2val, res2ind, 'random with duplicate keys')

            # Test sorting with NaNs
            x = torch.rand(SIZE, SIZE)
            x[1][2] = float('NaN')
            x[3][0] = float('NaN')
            torch.sort(x, out=(res2val, res2ind))
            self.assertIsOrdered('ascending', x, res2val, res2ind,
                                 'random with NaNs')
            torch.sort(x, out=(res2val, res2ind), descending=True)
            self.assertIsOrdered('descending', x, res2val, res2ind,
                                 'random with NaNs')

        def test_topk(self):
            def topKViaSort(t, k, dim, dir):
                sorted, indices = t.sort(dim, dir)
                return sorted.narrow(dim, 0, k), indices.narrow(dim, 0, k)

            def compareTensors(t, res1, ind1, res2, ind2, dim):
                # Values should be exactly equivalent
                self.assertEqual(res1, res2, atol=0, rtol=0)

                # Indices might differ based on the implementation, since there is
                # no guarantee of the relative order of selection
                if not ind1.eq(ind2).all():
                    # To verify that the indices represent equivalent elements,
                    # gather from the input using the topk indices and compare against
                    # the sort indices
                    vals = t.gather(dim, ind2)
                    self.assertEqual(res1, vals, atol=0, rtol=0)

            def compare(t, k, dim, dir):
                topKVal, topKInd = t.topk(k, dim, dir, True)
                sortKVal, sortKInd = topKViaSort(t, k, dim, dir)
                compareTensors(t, sortKVal, sortKInd, topKVal, topKInd, dim)

            t = torch.rand(random.randint(1, SIZE),
                           random.randint(1, SIZE),
                           random.randint(1, SIZE))

            for _kTries in range(3):
                for _dimTries in range(3):
                    for transpose in (True, False):
                        for dir in (True, False):
                            testTensor = t
                            if transpose:
                                dim1 = random.randrange(t.ndimension())
                                dim2 = dim1
                                while dim1 == dim2:
                                    dim2 = random.randrange(t.ndimension())

                                testTensor = t.transpose(dim1, dim2)

                            dim = random.randrange(testTensor.ndimension())
                            k = random.randint(1, testTensor.size(dim))
                            compare(testTensor, k, dim, dir)

        def test_topk_arguments(self):
            q = torch.randn(10, 2, 10)
            # Make sure True isn't mistakenly taken as the 2nd dimension (interpreted as 1)
            self.assertRaises(TypeError, lambda: q.topk(4, True))

        def test_median(self):
            for size in (155, 156):
                x = torch.rand(size, size)
                x0 = x.clone()

                nelem = x.nelement()
                res1val = torch.median(x)
                res2val, _ = torch.sort(x.view(nelem))
                ind = int(math.floor((nelem + 1) / 2) - 1)

                self.assertEqual(res2val[ind], res1val, atol=0, rtol=0)

                res1val, res1ind = torch.median(x, dim=1, keepdim=False)
                res2val, res2ind = torch.sort(x)
                ind = int(math.floor((size + 1) / 2) - 1)

                self.assertEqual(res2val.select(1, ind), res1val, atol=0, rtol=0)
                self.assertEqual(res2val.select(1, ind), res1val, atol=0, rtol=0)

                # Test use of result tensor
                res2val = torch.Tensor()
                res2ind = torch.LongTensor()
                torch.median(x, dim=-1, keepdim=False, out=(res2val, res2ind))
                self.assertEqual(res2val, res1val, atol=0, rtol=0)
                self.assertEqual(res2ind, res1ind, atol=0, rtol=0)

                # Test non-default dim
                res1val, res1ind = torch.median(x, 0, keepdim=False)
                res2val, res2ind = torch.sort(x, 0)
                self.assertEqual(res1val, res2val[ind], atol=0, rtol=0)
                self.assertEqual(res1ind, res2ind[ind], atol=0, rtol=0)

                # input unchanged
                self.assertEqual(x, x0, atol=0, rtol=0)

        def test_mode(self):
            x = torch.arange(1., SIZE * SIZE + 1).clone().resize_(SIZE, SIZE)
            x[:2] = 1
            x[:, :2] = 1
            x0 = x.clone()

            # Pre-calculated results.
            res1val = torch.Tensor(SIZE).fill_(1)
            # The indices are the position of the last appearance of the mode element.
            res1ind = torch.LongTensor(SIZE).fill_(1)
            res1ind[0] = SIZE - 1
            res1ind[1] = SIZE - 1

            res2val, res2ind = torch.mode(x, keepdim=False)
            self.assertEqual(res1val, res2val, atol=0, rtol=0)
            self.assertEqual(res1ind, res2ind, atol=0, rtol=0)

            # Test use of result tensor
            res2val = torch.Tensor()
            res2ind = torch.LongTensor()
            torch.mode(x, keepdim=False, out=(res2val, res2ind))
            self.assertEqual(res1val, res2val, atol=0, rtol=0)
            self.assertEqual(res1ind, res2ind, atol=0, rtol=0)

            # Test non-default dim
            res2val, res2ind = torch.mode(x, 0, False)
            self.assertEqual(res1val, res2val, atol=0, rtol=0)
            self.assertEqual(res1ind, res2ind, atol=0, rtol=0)

            # input unchanged
            self.assertEqual(x, x0, atol=0, rtol=0)

        def test_trilu_indices(self):
            for test_args in tri_tests_args:
                _compare_trilu_indices(self, *test_args)
            run_additional_tri_tests(self, 'cpu')

            # test default options
            x = torch.ones(
                3, 3, dtype=torch.long, device='cpu', layout=torch.strided)
            self.assertEqual(
                x.tril(0).nonzero().transpose(0, 1), torch.tril_indices(3, 3))
            self.assertEqual(
                x.triu(0).nonzero().transpose(0, 1), torch.triu_indices(3, 3))

            # test stride 0 cases
            x = torch.ones(
                3, 1, 3, 3, dtype=torch.long, device='cpu', layout=torch.strided)
            output = x.triu(2).expand(3, 3, 3, 3)
            b = x.clone().expand(3, 3, 3, 3)
            self.assertEqual(b.triu(2), output)
            self.assertRaises(RuntimeError, lambda: b.triu_(2))

        def test_narrow(self):
            x = torch.Tensor([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
            self.assertEqual(x.narrow(0, 0, 1), torch.Tensor([[0, 1, 2]]))
            self.assertEqual(x.narrow(0, 0, 2), torch.Tensor([[0, 1, 2], [3, 4, 5]]))
            self.assertEqual(x.narrow(0, 1, 1), torch.Tensor([[3, 4, 5]]))
            self.assertEqual(x.narrow(0, -1, 1), torch.Tensor([[6, 7, 8]]))
            self.assertEqual(x.narrow(0, -2, 2), torch.Tensor([[3, 4, 5], [6, 7, 8]]))
            self.assertEqual(x.narrow(0, -3, 3), torch.Tensor([[0, 1, 2], [3, 4, 5], [6, 7, 8]]))
            self.assertEqual(x.narrow(-1, -1, 1), torch.Tensor([[2], [5], [8]]))
            self.assertEqual(x.narrow(-2, -1, 1), torch.Tensor([[6, 7, 8]]))

        def test_narrow_tensor(self):
            x = torch.Tensor([[0, 1, 2], [3, 4, 5], [6, 7, 8]])
            self.assertEqual(x.narrow(0, torch.tensor(0), 1), torch.Tensor([[0, 1, 2]]))
            with self.assertRaises(Exception):
                x.narrow(0, torch.tensor(0.), 1)
            with self.assertRaises(Exception):
                x.narrow(0, torch.tensor([0]), 1)
            with self.assertRaises(Exception):
                x.narrow(0, torch.tensor([0, 1]), 1)

        def test_stack(self):
            for dtype in (torch.half, torch.double, torch.int):
                x = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                y = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                z = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                for dim in range(4):
                    res = torch.stack((x, y, z), dim)
                    res_neg = torch.stack((x, y, z), dim - 4)
                    expected_size = x.size()[:dim] + (3,) + x.size()[dim:]
                    self.assertEqual(res, res_neg)
                    self.assertEqual(res.size(), expected_size)
                    self.assertEqual(res.select(dim, 0), x, atol=0, rtol=0)
                    self.assertEqual(res.select(dim, 1), y, atol=0, rtol=0)
                    self.assertEqual(res.select(dim, 2), z, atol=0, rtol=0)

        def test_stack_out(self):
            for dtype in (torch.half, torch.double, torch.int):
                x = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                y = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                z = torch.randint(low=-100, high=100, size=(2, 3, 4)).to(dtype)
                for dim in range(4):
                    expected_size = x.size()[:dim] + (3,) + x.size()[dim:]
                    res_out = x.new(expected_size)
                    res_neg_out = x.new(expected_size)
                    res_out_dp = res_out.data_ptr()
                    res_out_neg_dp = res_neg_out.data_ptr()
                    torch.stack((x, y, z), dim, out=res_out)
                    torch.stack((x, y, z), dim - 4, out=res_neg_out)
                    self.assertEqual(res_out, res_neg_out)
                    self.assertEqual(res_out.size(), expected_size)
                    self.assertEqual(res_out_dp, res_out.data_ptr())
                    self.assertEqual(res_out_neg_dp, res_neg_out.data_ptr())
                    self.assertEqual(res_out.select(dim, 0), x, atol=0, rtol=0)
                    self.assertEqual(res_out.select(dim, 1), y, atol=0, rtol=0)
                    self.assertEqual(res_out.select(dim, 2), z, atol=0, rtol=0)

        def test_unbind(self):
            x = torch.rand(2, 3, 4, 5)
            for dim in range(4):
                res = torch.unbind(x, dim)
                res2 = x.unbind(dim)
                self.assertEqual(x.size(dim), len(res))
                self.assertEqual(x.size(dim), len(res2))
                for i in range(dim):
                    self.assertEqual(x.select(dim, i), res[i])
                    self.assertEqual(x.select(dim, i), res2[i])

        def test_slice(self):
            empty = torch.empty(0, 4)
            x = torch.arange(0., 16).view(4, 4)
            self.assertEqual(x[:], x)
            self.assertEqual(x[:4], x)
            # start and stop are clamped to the size of dim
            self.assertEqual(x[:5], x)
            # if start >= stop then the result is empty
            self.assertEqual(x[2:1], empty)
            self.assertEqual(x[2:2], empty)
            # out of bounds is also empty
            self.assertEqual(x[10:12], empty)
            # additional correctness checks
            self.assertEqual(x[:1].tolist(), [[0, 1, 2, 3]])
            self.assertEqual(x[:-3].tolist(), [[0, 1, 2, 3]])
            self.assertEqual(x[:, -2:3].tolist(), [[2], [6], [10], [14]])
            self.assertEqual(x[0:-1:2].tolist(), [[0, 1, 2, 3], [8, 9, 10, 11]])

        @skipIfNoLapack
        def test_ormqr(self):
            mat1 = torch.randn(7, 7)
            mat2 = torch.randn(7, 7)
            q, r = torch.qr(mat1)
            m, tau = torch.geqrf(mat1)
            out_holder = torch.empty_like(mat1)

            res1 = torch.mm(q, mat2)
            res2 = torch.ormqr(m, tau, mat2, left=True, transpose=False)
            torch.ormqr(m, tau, mat2, out=out_holder)
            self.assertEqual(res1, res2)
            self.assertEqual(res2, out_holder)

            res1 = torch.mm(mat2, q)
            res2 = torch.ormqr(m, tau, mat2, left=False, transpose=False)
            torch.ormqr(m, tau, mat2, left=False, transpose=False, out=out_holder)
            self.assertEqual(res1, res2)
            self.assertEqual(res2, out_holder)

            res1 = torch.mm(q.t(), mat2)
            res2 = torch.ormqr(m, tau, mat2, left=True, transpose=True)
            torch.ormqr(m, tau, mat2, left=True, transpose=True, out=out_holder)
            self.assertEqual(res1, res2)
            self.assertEqual(res2, out_holder)

            res1 = torch.mm(mat2, q.t())
            res2 = torch.ormqr(m, tau, mat2, left=False, transpose=True)
            torch.ormqr(m, tau, mat2, left=False, transpose=True, out=out_holder)
            self.assertEqual(res1, res2)
            self.assertEqual(res2, out_holder)

        @unittest.skip("Not implemented yet")
        def test_conv2(self):
            x = torch.rand(math.floor(torch.uniform(50, 100)), math.floor(torch.uniform(50, 100)))
            k = torch.rand(math.floor(torch.uniform(10, 20)), math.floor(torch.uniform(10, 20)))
            imvc = torch.conv2(x, k)
            imvc2 = torch.conv2(x, k, 'V')
            imfc = torch.conv2(x, k, 'F')

            ki = k.clone()
            ks = k.storage()
            kis = ki.storage()
            for i in range(ks.size() - 1, 0, -1):
                kis[ks.size() - i + 1] = ks[i]
            # for i=ks.size(), 1, -1 do kis[ks.size()-i+1]=ks[i] end
            imvx = torch.xcorr2(x, ki)
            imvx2 = torch.xcorr2(x, ki, 'V')
            imfx = torch.xcorr2(x, ki, 'F')

            self.assertEqual(imvc, imvc2, atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(imvc, imvx, atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(imvc, imvx2, atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(imfc, imfx, atol=0, rtol=0, msg='torch.conv2')
            self.assertLessEqual(math.abs(x.dot(x) - torch.xcorr2(x, x)[0][0]), 1e-10, 'torch.conv2')

            xx = torch.Tensor(2, x.size(1), x.size(2))
            xx[1].copy_(x)
            xx[2].copy_(x)
            kk = torch.Tensor(2, k.size(1), k.size(2))
            kk[1].copy_(k)
            kk[2].copy_(k)

            immvc = torch.conv2(xx, kk)
            immvc2 = torch.conv2(xx, kk, 'V')
            immfc = torch.conv2(xx, kk, 'F')

            self.assertEqual(immvc[0], immvc[1], atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(immvc[0], imvc, atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(immvc2[0], imvc2, atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(immfc[0], immfc[1], atol=0, rtol=0, msg='torch.conv2')
            self.assertEqual(immfc[0], imfc, atol=0, rtol=0, msg='torch.conv2')

        @unittest.skip("Not implemented yet")
        def test_conv3(self):
            x = torch.rand(math.floor(torch.uniform(20, 40)),
                           math.floor(torch.uniform(20, 40)),
                           math.floor(torch.uniform(20, 40)))
            k = torch.rand(math.floor(torch.uniform(5, 10)),
                           math.floor(torch.uniform(5, 10)),
                           math.floor(torch.uniform(5, 10)))
            imvc = torch.conv3(x, k)
            imvc2 = torch.conv3(x, k, 'V')
            imfc = torch.conv3(x, k, 'F')

            ki = k.clone()
            ks = k.storage()
            kis = ki.storage()
            for i in range(ks.size() - 1, 0, -1):
                kis[ks.size() - i + 1] = ks[i]
            imvx = torch.xcorr3(x, ki)
            imvx2 = torch.xcorr3(x, ki, 'V')
            imfx = torch.xcorr3(x, ki, 'F')

            self.assertEqual(imvc, imvc2, atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(imvc, imvx, atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(imvc, imvx2, atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(imfc, imfx, atol=0, rtol=0, msg='torch.conv3')
            self.assertLessEqual(math.abs(x.dot(x) - torch.xcorr3(x, x)[0][0][0]), 4e-10, 'torch.conv3')

            xx = torch.Tensor(2, x.size(1), x.size(2), x.size(3))
            xx[1].copy_(x)
            xx[2].copy_(x)
            kk = torch.Tensor(2, k.size(1), k.size(2), k.size(3))
            kk[1].copy_(k)
            kk[2].copy_(k)

            immvc = torch.conv3(xx, kk)
            immvc2 = torch.conv3(xx, kk, 'V')
            immfc = torch.conv3(xx, kk, 'F')

            self.assertEqual(immvc[0], immvc[1], atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(immvc[0], imvc, atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(immvc2[0], imvc2, atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(immfc[0], immfc[1], atol=0, rtol=0, msg='torch.conv3')
            self.assertEqual(immfc[0], imfc, atol=0, rtol=0, msg='torch.conv3')

        @unittest.skip("Not implemented yet")
        def _test_conv_corr_eq(self, fn, fn_2_to_3):
            ix = math.floor(random.randint(20, 40))
            iy = math.floor(random.randint(20, 40))
            iz = math.floor(random.randint(20, 40))
            kx = math.floor(random.randint(5, 10))
            ky = math.floor(random.randint(5, 10))
            kz = math.floor(random.randint(5, 10))

            x = torch.rand(ix, iy, iz)
            k = torch.rand(kx, ky, kz)

            o3 = fn(x, k)
            o32 = torch.zeros(o3.size())
            fn_2_to_3(x, k, o3, o32)
            self.assertEqual(o3, o32)

        @unittest.skip("Not implemented yet")
        def test_xcorr3_xcorr2_eq(self):
            def reference(x, k, o3, o32):
                for i in range(o3.size(1)):
                    for j in range(k.size(1)):
                        o32[i].add(torch.xcorr2(x[i + j - 1], k[j]))
            self._test_conv_corr_eq(torch.xcorr3, reference)

        @unittest.skip("Not implemented yet")
        def test_xcorr3_xcorr2_eq_full(self):
            def reference(x, k, o3, o32):
                for i in range(x.size(1)):
                    for j in range(k.size(1)):
                        o32[i].add(torch.xcorr2(x[i], k[k.size(1) - j + 1], 'F'))
            self._test_conv_corr_eq(lambda x, k: torch.xcorr3(x, k, 'F'), reference)

        @unittest.skip("Not implemented yet")
        def test_conv3_conv2_eq_valid(self):
            def reference(x, k, o3, o32):
                for i in range(o3.size(1)):
                    for j in range(k.size(1)):
                        o32[i].add(torch.conv2(x[i + j - 1], k[k.size(1) - j + 1]))
            self._test_conv_corr_eq(torch.conv3, reference)

        @unittest.skip("Not implemented yet")
        def test_fconv3_fconv2_eq(self):
            def reference(x, k, o3, o32):
                for i in range(o3.size(1)):
                    for j in range(k.size(1)):
                        o32[i + j - 1].add(torch.conv2(x[i], k[j], 'F'))
            self._test_conv_corr_eq(lambda x, k: torch.conv3(x, k, 'F'), reference)

        def test_dtype_is_signed(self):
            for dtype in torch.testing.get_all_dtypes():
                self.assertEqual(dtype.is_signed, torch.is_signed(torch.tensor(0, dtype=dtype)))

            self.assertRaisesRegex(RuntimeError, 'not supported for quantized', lambda: torch.quint8.is_signed)
            self.assertRaisesRegex(RuntimeError, 'not supported for quantized', lambda: torch.qint8.is_signed)
            self.assertRaisesRegex(RuntimeError, 'not supported for quantized', lambda: torch.qint32.is_signed)

        def test_RNGState(self):
            state = torch.get_rng_state()
            stateCloned = state.clone()
            before = torch.rand(1000)

            self.assertEqual(state.ne(stateCloned).long().sum(), 0, atol=0, rtol=0)

            torch.set_rng_state(state)
            after = torch.rand(1000)
            self.assertEqual(before, after, atol=0, rtol=0)

        def test_RNGStateAliasing(self):
            # Fork the random number stream at this point
            gen = torch.Generator()
            gen.set_state(torch.get_rng_state())
            self.assertEqual(gen.get_state(), torch.get_rng_state())

            target_value = torch.rand(1000)
            # Dramatically alter the internal state of the main generator
            _ = torch.rand(100000)
            forked_value = torch.rand(1000, generator=gen)
            self.assertEqual(target_value, forked_value, atol=0, rtol=0, msg="RNG has not forked correctly.")

        def test_RNG_after_pickle(self):
            torch.random.manual_seed(100)
            before = torch.rand(10)

            torch.random.manual_seed(100)
            buf = io.BytesIO()
            tensor = torch.Tensor([1, 2, 3])
            ForkingPickler(buf, pickle.HIGHEST_PROTOCOL).dump(tensor)
            after = torch.rand(10)

            self.assertEqual(before, after, atol=0, rtol=0)

        def test_boxMullerState(self):
            torch.manual_seed(123)
            odd_number = 101
            seeded = torch.randn(odd_number)
            state = torch.get_rng_state()
            midstream = torch.randn(odd_number)
            torch.set_rng_state(state)
            repeat_midstream = torch.randn(odd_number)
            torch.manual_seed(123)
            reseeded = torch.randn(odd_number)
            self.assertEqual(midstream, repeat_midstream, atol=0, rtol=0,
                             msg='get_rng_state/set_rng_state not generating same sequence of normally distributed numbers')
            self.assertEqual(seeded, reseeded, atol=0, rtol=0,
                             msg='repeated calls to manual_seed not generating same sequence of normally distributed numbers')

        def test_manual_seed(self):
            rng_state = torch.get_rng_state()
            torch.manual_seed(2)
            x = torch.randn(100)
            self.assertEqual(torch.initial_seed(), 2)
            torch.manual_seed(2)
            y = torch.randn(100)
            self.assertEqual(x, y)

            max_int64 = 0x7fff_ffff_ffff_ffff
            min_int64 = -max_int64 - 1
            max_uint64 = 0xffff_ffff_ffff_ffff
            # Check all boundary cases of valid seed value inputs
            test_cases = [
                # (seed, expected_initial_seed)
                # Positive seeds should be unchanged
                (max_int64, max_int64),
                (max_int64 + 1, max_int64 + 1),
                (max_uint64, max_uint64),
                (0, 0),
                # Negative seeds wrap around starting from the largest seed value
                (-1, max_uint64),
                (min_int64, max_int64 + 1)
            ]
            for seed, expected_initial_seed in test_cases:
                torch.manual_seed(seed)
                actual_initial_seed = torch.initial_seed()
                msg = "expected initial_seed() = %x after calling manual_seed(%x), but got %x instead" % (
                    expected_initial_seed, seed, actual_initial_seed)
                self.assertEqual(expected_initial_seed, actual_initial_seed, msg=msg)
            for invalid_seed in [min_int64 - 1, max_uint64 + 1]:
                with self.assertRaisesRegex(RuntimeError, r'Overflow when unpacking long'):
                    torch.manual_seed(invalid_seed)

            torch.set_rng_state(rng_state)

        def test_numel(self):
            b = torch.ByteTensor(3, 100, 100)
            self.assertEqual(b.nelement(), 3 * 100 * 100)
            self.assertEqual(b.numel(), 3 * 100 * 100)

        # Note: the warning this tests for only appears once per program, so
        # other instances of this warning should be addressed to avoid
        # the tests depending on the order in which they're run.
        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_numpy_non_writeable(self):
            arr = np.zeros(5)
            arr.flags['WRITEABLE'] = False
            self.assertWarns(UserWarning, lambda: torch.from_numpy(arr))

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_empty_storage_view(self):
            # we should be able to "modify" slices of a 0-element
            # array without an error being raised due to
            # trying to resize its storage
            t = torch.from_numpy(np.empty((0, 4)))
            t[:, 1::2] *= 1

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_newaxis_numpy_comparison(self):
            def run_test(tensor, *idx):
                npt = tensor.numpy()
                self.assertEqual(tensor[idx], npt[idx])

            # 1D Tensor Tests
            x = torch.arange(0, 10)
            cases = [
                [None],
                [None, None],
                [Ellipsis, None],
                [None, Ellipsis],
                [2, None],
                [None, 2],
                [Ellipsis, None, 2],
                [Ellipsis, 2, None],
                [2, Ellipsis, None],
                [2, None, Ellipsis],
                [None, 2, Ellipsis],
                [None, Ellipsis, 2],
            ]

            for case in cases:
                run_test(x, *case)

            # 2D Tensor Tests
            x = torch.arange(0, 12).view(3, 4)
            cases = [
                [None],
                [None, None],
                [None, None, None],
                [Ellipsis, None],
                [Ellipsis, None, None],
                [None, Ellipsis],
                [None, Ellipsis, None],
                [None, None, Ellipsis],
                [2, None],
                [2, None, Ellipsis],
                [2, Ellipsis, None],
                [None, 2, Ellipsis],
                [Ellipsis, 2, None],
                [Ellipsis, None, 2],
                [None, Ellipsis, 2],
                [1, 2, None],
                [1, 2, Ellipsis, None],
                [1, Ellipsis, 2, None],
                [Ellipsis, 1, None, 2],
                [Ellipsis, 1, 2, None],
                [1, None, 2, Ellipsis],
                [None, 1, Ellipsis, 2],
                [None, 1, 2, Ellipsis],
            ]

            for case in cases:
                run_test(x, *case)

        def _consecutive(self, size, start=1):
            sequence = torch.ones(int(torch.Tensor(size).prod(0))).cumsum(0)
            sequence.add_(start - 1)
            return sequence.resize_(*size)

        def test_newindex(self):
            reference = self._consecutive((3, 3, 3))
            # This relies on __index__() being correct - but we have separate tests for that

            def checkPartialAssign(index):
                reference = torch.zeros(3, 3, 3)
                reference[index] = self._consecutive((3, 3, 3))[index]
                self.assertEqual(reference[index], self._consecutive((3, 3, 3))[index], atol=0, rtol=0)
                reference[index] = 0
                self.assertEqual(reference, torch.zeros(3, 3, 3), atol=0, rtol=0)

            checkPartialAssign(0)
            checkPartialAssign(1)
            checkPartialAssign(2)
            checkPartialAssign((0, 1))
            checkPartialAssign((1, 2))
            checkPartialAssign((0, 2))
            checkPartialAssign(torch.LongTensor((0, 2)))

            with self.assertRaises(IndexError):
                reference[1, 1, 1, 1] = 1
            with self.assertRaises(IndexError):
                reference[1, 1, 1, (1, 1)] = 1
            with self.assertRaises(IndexError):
                reference[3, 3, 3, 3, 3, 3, 3, 3] = 1
            with self.assertRaises(IndexError):
                reference[0.0] = 1
            with self.assertRaises(TypeError):
                reference[0.0:2.0] = 1
            with self.assertRaises(IndexError):
                reference[0.0, 0.0:2.0] = 1
            with self.assertRaises(IndexError):
                reference[0.0, :, 0.0:2.0] = 1
            with self.assertRaises(IndexError):
                reference[0.0, ..., 0.0:2.0] = 1
            with self.assertRaises(IndexError):
                reference[0.0, :, 0.0] = 1

        def test_index_add(self):
            for dest_contig, src_contig, index_contig in product([True, False], repeat=3):
                for other_sizes in ((), (4, 5)):
                    num_copy, num_dest = 3, 3
                    dest = torch.randn(num_dest, *other_sizes)
                    if not dest_contig:
                        dest = torch.testing.make_non_contiguous(dest)
                    src = torch.randn(num_copy, *other_sizes)
                    if not src_contig:
                        src = torch.testing.make_non_contiguous(src)
                    idx = torch.randperm(num_dest).narrow(0, 0, num_copy)
                    if not index_contig:
                        idx = torch.testing.make_non_contiguous(idx)
                    dest2 = dest.clone()
                    dest.index_add_(0, idx, src)
                    for i in range(idx.size(0)):
                        dest2[idx[i]] += src[i]
                    self.assertEqual(dest, dest2)

        # add coverage for issue with atomic add that appeared only for
        # specific dtypes on cuda:
        # https://github.com/pytorch/pytorch/issues/29153
        def test_index_add_all_dtypes(self):
            for device in torch.testing.get_all_device_types():
                for dtype in torch.testing.get_all_math_dtypes(device):
                    size = [5, 5]
                    if dtype.is_floating_point or dtype.is_complex:
                        tensor = torch.rand(size, dtype=dtype, device=device)
                    elif dtype.is_signed:
                        tensor = torch.randint(-5, 15, size, dtype=dtype, device=device)
                    else:
                        tensor = torch.randint(0, 10, size, dtype=dtype, device=device)

                    # index_add calls atomicAdd on cuda.
                    zeros = torch.zeros(size, dtype=dtype, device=device)

                    # index_add is not supported for complex dtypes on cuda yet
                    if device.startswith('cuda') and dtype.is_complex:
                        continue

                    added = zeros.index_add(0, torch.arange(0, size[0], dtype=torch.long, device=device), tensor)
                    self.assertEqual(added, tensor)

        def test_t(self):
            # Test 0D tensors
            x = torch.randn(())
            self.assertEqual(x, x.t())
            x = x.to_sparse()
            self.assertEqual(x, x.t())

            # Test 1D tensors
            x = torch.arange(4)
            self.assertEqual(x, x.t())
            x = x.to_sparse()
            self.assertEqual(x, x.t())

            # Test 2D tensors
            x = torch.rand((2, 2))
            self.assertEqual(x.t(), x.transpose(0, 1))
            x = x.to_sparse()
            self.assertEqual(x.t(), x.transpose(0, 1))

            # Test 3D tensor
            x = torch.rand((2, 2, 2))
            with self.assertRaisesRegex(RuntimeError, 'expects a tensor with <= 2 dimensions, but self is 3D'):
                x.t()
            x = x.to_sparse()
            with self.assertRaisesRegex(RuntimeError, 'expects a tensor with <= 2 sparse and 0 dense dimensions'):
                x.t()

        def test_take(self):
            def check(src, idx):
                expected = src.contiguous().view(-1).index_select(
                    0, idx.contiguous().view(-1)).view_as(idx)
                actual = src.take(idx)
                self.assertEqual(actual.size(), idx.size())
                self.assertEqual(expected, actual)

            src = torch.randn(2, 3, 5)
            idx = torch.LongTensor([[0, 2], [3, 4]])
            check(src, idx)
            check(src.transpose(1, 2), idx)
            check(src.bool(), idx)

        def test_put_(self):
            def check(dst, idx, value):
                expected = dst.clone(memory_format=torch.contiguous_format).view(-1).index_copy_(
                    0, idx.contiguous().view(-1), value.contiguous().view(-1))
                expected = expected.view_as(dst)
                dst.put_(idx, value)
                self.assertEqual(expected, dst)

            dst = torch.randn(2, 3, 5)
            idx = torch.LongTensor([[0, 2], [3, 4]])
            values = torch.randn(2, 2)
            check(dst, idx, values)
            check(dst.transpose(1, 2), idx, values)

            values = torch.tensor([[False, False], [False, False]])
            check(dst.bool(), idx, values)

        def test_put_accumulate(self):
            dst = torch.ones(2, 2)
            idx = torch.LongTensor([[0, 1], [0, 1]])
            src = torch.Tensor([1, 2, 3, 4])
            dst.put_(idx, src, accumulate=True)
            self.assertEqual(dst.tolist(), [[5, 7], [1, 1]])

        # Fill idx with valid indices.
        @staticmethod
        def _fill_indices(self, idx, dim, dim_size, elems_per_row, m, n, o):
            for i in range(1 if dim == 0 else m):
                for j in range(1 if dim == 1 else n):
                    for k in range(1 if dim == 2 else o):
                        ii = [i, j, k]
                        ii[dim] = slice(0, idx.size(dim) + 1)
                        idx[tuple(ii)] = torch.randperm(dim_size)[0:elems_per_row]

        def test_flatten(self):
            # Test that flatten returns 1-dim tensor when given a 0-dim tensor
            zero_dim_tensor = torch.tensor(123)
            flat0 = zero_dim_tensor.flatten()
            one_dim_tensor = torch.tensor([123])
            flat1 = zero_dim_tensor.flatten()

            self.assertEqual(zero_dim_tensor.shape, torch.Size([]))
            self.assertEqual(flat0.shape, torch.Size([1]))
            self.assertEqual(one_dim_tensor.shape, torch.Size([1]))
            self.assertEqual(flat1.shape, torch.Size([1]))
            self.assertEqual(flat0, one_dim_tensor)
            self.assertEqual(flat0, flat1)
            self.assertEqual(flat0.shape, flat1.shape)

            # Test both float tensor and quantized tensor
            tensors = [torch.randn(5, 5, 5, 5),
                       torch._empty_affine_quantized([5, 5, 5, 5],
                                                     scale=2,
                                                     zero_point=3,
                                                     dtype=torch.quint8)]
            for src in tensors:
                flat = src.flatten(0, -1)
                self.assertEqual(flat.shape, torch.Size([625]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(0, 2)
                self.assertEqual(flat.shape, torch.Size([125, 5]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(0, 1)
                self.assertEqual(flat.shape, torch.Size([25, 5, 5]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(1, 2)
                self.assertEqual(flat.shape, torch.Size([5, 25, 5]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(2, 3)
                self.assertEqual(flat.shape, torch.Size([5, 5, 25]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(-2, -1)
                self.assertEqual(flat.shape, torch.Size([5, 5, 25]))
                self.assertEqual(src.view(-1), flat.view(-1))

                flat = src.flatten(2, 2)
                self.assertEqual(flat, src)

                # out of bounds index
                with self.assertRaisesRegex(IndexError, 'Dimension out of range'):
                    src.flatten(5, 10)

                # invalid start and end
                with self.assertRaisesRegex(RuntimeError, 'start_dim cannot come after end_dim'):
                    src.flatten(2, 0)

        def test_unflatten(self):
            # test args: tensor, int, sizes
            self.assertEqual(torch.tensor([]).unflatten(0, (0, 1)), torch.empty(0, 1))
            self.assertEqual(torch.tensor([1]).unflatten(0, (1, 1)), torch.tensor([[1]]))
            self.assertEqual(torch.tensor([1, 2, 3, 4]).unflatten(0, (2, 2)), torch.tensor([[1, 2], [3, 4]]))
            self.assertEqual(torch.tensor([1, 2, 3, 4]).unflatten(0, [2, 2]), torch.tensor([[1, 2], [3, 4]]))
            self.assertEqual(torch.tensor([1, 2, 3, 4]).unflatten(0, torch.Size([2, 2])), torch.tensor([[1, 2], [3, 4]]))
            self.assertEqual(torch.ones(2, 10).unflatten(1, (5, 2)), torch.ones(2, 5, 2))

            # test invalid args: tensor, str, sizes
            with self.assertRaisesRegex(TypeError, r"received an invalid combination of arguments"):
                torch.tensor([1]).unflatten('A', (1, 1))

            # test invalid args: tensor, str, namedshape
            with self.assertRaisesRegex(RuntimeError, r"Name 'A' not found in Tensor\[None\]."):
                torch.ones(4).unflatten('A', (('A', 2), ('B', 2)))

            # test other invalid arguments
            with self.assertRaisesRegex(RuntimeError, r"sizes must be non-empty"):
                torch.tensor([1]).unflatten(0, [])
            with self.assertRaisesRegex(RuntimeError, r"Provided sizes \[2, 2\] don't multiply up to the size of dim 0 \(1\)"):
                torch.tensor([1]).unflatten(0, [2, 2])
            with self.assertRaisesRegex(IndexError, r"dimension specified as 0 but tensor has no dimensions"):
                torch.tensor(1).unflatten(0, [0])

        @staticmethod
        def _test_gather(self, cast, test_bounds=True):
            m, n, o = random.randint(10, 20), random.randint(10, 20), random.randint(10, 20)
            elems_per_row = random.randint(1, 10)
            dim = random.randrange(3)

            for dtype in {torch.float32, torch.complex64, torch.complex128}:
                src = torch.randn(m, n, o, dtype=dtype)
                idx_size = [m, n, o]
                idx_size[dim] = elems_per_row
                idx = torch.LongTensor().resize_(*idx_size)
                AbstractTestCases._TestTorchMixin._fill_indices(self, idx, dim, src.size(dim), elems_per_row, m, n, o)

                src = cast(src)
                idx = cast(idx)

                actual = torch.gather(src, dim, idx)
                expected = cast(torch.zeros(idx_size, dtype=dtype))
                for i in range(idx_size[0]):
                    for j in range(idx_size[1]):
                        for k in range(idx_size[2]):
                            ii = [i, j, k]
                            ii[dim] = idx[i, j, k]
                            expected[i, j, k] = src[tuple(ii)]
                self.assertEqual(actual, expected, atol=0, rtol=0)

            bad_src = torch.randn(*[i - 1 for i in idx_size])
            self.assertRaises(RuntimeError, lambda: torch.gather(bad_src, dim, idx))

            # should throw an error when index dtype is not long
            with self.assertRaisesRegex(RuntimeError, 'Expected dtype int64 for index'):
                torch.gather(src, dim, idx.to(torch.int))

            # should throw an error when out.dtype != src.dtype.
            with self.assertRaisesRegex(RuntimeError, 'Expected self.dtype to be equal to src.dtype'):
                torch.gather(src, dim, idx, out=expected.to(torch.int))

            # checks for the same dimensionality
            with self.assertRaisesRegex(RuntimeError, 'Index tensor must have the same number of dimensions as input tensor'):
                torch.gather(src, dim, idx.unsqueeze(-1))

            with self.assertRaisesRegex(RuntimeError, 'Index tensor must have the same number of dimensions as input tensor'):
                torch.gather(src.unsqueeze(-1), dim, idx)

            if test_bounds:
                idx[0][0][0] = 23
                self.assertRaises(RuntimeError, lambda: torch.gather(src, dim, idx))

            src = cast(torch.randn(3, 4, 5))
            expected, idx = src.max(2, True)
            expected = cast(expected)
            idx = cast(idx)
            actual = torch.gather(src, 2, idx)
            self.assertEqual(actual, expected, atol=0, rtol=0)

            # Bool test case
            t = torch.tensor([[False, True], [True, True]])
            self.assertEqual(torch.gather(t, 1, torch.tensor([[0, 0], [1, 0]])), torch.tensor([[False, False], [True, True]]))

        def test_gather(self):
            self._test_gather(self, lambda t: t)

        @staticmethod
        def _test_scatter_add_mult_index_base(self, cast):
            m, n = 30, 40
            idx = torch.zeros(m, n).long()
            src = torch.ones(m, n)
            res0 = torch.zeros(m, n).scatter_add_(0, idx, src)
            res1 = torch.zeros(m, n).scatter_add_(1, idx, src)

            self.assertEqual(res0[0, :], m * torch.ones(n), atol=0, rtol=0)
            self.assertEqual(res1[:, 0], n * torch.ones(m), atol=0, rtol=0)

        def test_scatter_add_mult_index(self):
            self._test_scatter_add_mult_index_base(self, lambda t: t)

        @staticmethod
        def _test_scatter_base(self, cast, method, is_scalar=False, test_bounds=True, reduction=None, *, test_complex=False):
            if test_complex:
                dtypes = [torch.complex64, torch.complex128]
            else:
                dtypes = [torch.float16, torch.float32, torch.float64]

            for dtype in dtypes:
                m, n, o = random.randint(10, 20), random.randint(10, 20), random.randint(10, 20)
                elems_per_row = random.randint(1, 10)
                dim = random.randrange(3)

                idx_size = [m, n, o]
                idx_size[dim] = elems_per_row
                idx = cast(torch.LongTensor().resize_(*idx_size))
                AbstractTestCases._TestTorchMixin._fill_indices(self, idx, dim, ([m, n, o])[dim], elems_per_row, m, n, o)

                src_size = [random.randint(1, 5) + s for s in idx_size]
                if is_scalar:
                    src = random.random()
                else:
                    src = cast(torch.randn(src_size, dtype=dtype))

                base = cast(torch.randn(m, n, o, dtype=dtype))
                if reduction:
                    actual = getattr(base.clone(), method)(dim, idx, src, reduce=reduction)
                else:
                    actual = getattr(base.clone(), method)(dim, idx, src)
                expected = base.clone()
                for i in range(idx_size[0]):
                    for j in range(idx_size[1]):
                        for k in range(idx_size[2]):
                            ii = [i, j, k]
                            ii[dim] = idx[i, j, k]
                            if method == 'scatter_' and not is_scalar:
                                if reduction:
                                    if reduction == "add":
                                        expected[tuple(ii)] += src[i, j, k]
                                    elif reduction == "multiply":
                                        expected[tuple(ii)] *= src[i, j, k]
                                else:
                                    expected[tuple(ii)] = src[i, j, k]
                            elif method == 'scatter_add_':
                                expected[tuple(ii)] += src[i, j, k]
                            else:
                                expected[tuple(ii)] = src
                self.assertEqual(actual, expected, atol=0, rtol=0)

                # should throw an error when self.dtype != src.dtype.
                # we ignore the case when src is Scalar, as it gets
                # cast via src.to<scalar_t>.
                if not is_scalar:
                    with self.assertRaisesRegex(RuntimeError, 'Expected self.dtype to be equal to src.dtype'):
                        getattr(base.clone().type(torch.int), method)(dim, idx, src)

                    with self.assertRaisesRegex(RuntimeError, 'Expected self.dtype to be equal to src.dtype'):
                        getattr(base.clone(), method)(dim, idx, src.type(torch.int))

                # should throw an error when index dtype is not long
                with self.assertRaisesRegex(IndexError, 'Expected dtype int64 for index'):
                    getattr(base.clone(), method)(dim, idx.type(torch.int), src)

                # check for the same dimensionality
                with self.assertRaisesRegex(RuntimeError, 'Index tensor must have the same number of dimensions as self tensor'):
                    getattr(base.clone().unsqueeze(-1), method)(dim, idx, src)

                with self.assertRaisesRegex(RuntimeError, 'Index tensor must have the same number of dimensions as self tensor'):
                    getattr(base.clone(), method)(dim, idx.unsqueeze(-1), src)

                if not is_scalar:
                    with self.assertRaisesRegex(RuntimeError, 'Index tensor must have the same number of dimensions as src tensor'):
                        getattr(base.clone(), method)(dim, idx, src.unsqueeze(-1))

                if test_bounds:
                    idx[0][0][0] = 34
                    with self.assertRaises(RuntimeError):
                        if reduction:
                            getattr(base.clone(), method)(dim, idx, src, reduce=reduction)
                        else:
                            getattr(base.clone(), method)(dim, idx, src)

                # test for empty index, should be a no-op
                idx = cast(torch.LongTensor())
                if reduction:
                    actual = getattr(base.clone(), method)(dim, idx, src, reduce=reduction)
                else:
                    actual = getattr(base.clone(), method)(dim, idx, src)
                self.assertEqual(actual, base, atol=0, rtol=0)

        def test_scatter(self):
            self._test_scatter_base(self, lambda t: t, 'scatter_')

        def test_scatterAdd(self):
            self._test_scatter_base(self, lambda t: t, 'scatter_add_')

        def test_scatterFill(self):
            self._test_scatter_base(self, lambda t: t, 'scatter_', True)

        def test_scatterReduce(self):
            for method in ["add", "multiply"]:
                self._test_scatter_base(self, lambda t: t, 'scatter_', reduction=method)

        def test_masked_scatter(self):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                for maskType in [torch.uint8, torch.bool]:
                    for dt in torch.testing.get_all_dtypes():
                        num_copy, num_dest = 3, 10
                        dest = torch.tensor([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], dtype=dt)
                        dest2 = dest.clone()
                        src = torch.tensor([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=dt)
                        mask = torch.tensor((0, 0, 0, 0, 1, 0, 1, 0, 1, 0), dtype=maskType)

                        if dt == torch.bool:
                            # torch.bool is a special case and is being tested
                            # in a separate test
                            continue

                        # TODO: update test when masked scatter is supported for complex
                        if dt == torch.half or dt.is_complex:
                            self.assertRaises(RuntimeError, lambda: dest.masked_scatter_(mask, src))
                            continue

                        dest.masked_scatter_(mask, src)
                        j = 0
                        for i in range(num_dest):
                            if mask[i]:
                                dest2[i] = src[j]
                                j += 1
                        self.assertEqual(dest, dest2, atol=0, rtol=0)

                        # make source bigger than number of 1s in mask
                        src = torch.tensor([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], dtype=dt)
                        dest.masked_scatter_(mask, src)

                        # make src smaller. this should fail
                        src = torch.randn(num_copy - 1)
                        with self.assertRaises(RuntimeError):
                            dest.masked_scatter_(mask, src)
            self.assertEqual(len(w), 27)

            warn = 'masked_scatter_ received a mask with dtype torch.uint8,'
            for wi in w:
                self.assertEqual(str(wi.message)[0:55], str(warn))

        def test_masked_fill(self):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                for dt in torch.testing.get_all_dtypes():
                    for dtype in [torch.uint8, torch.bool]:
                        num_dest = 10
                        dst = torch.tensor([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=dt)
                        mask = torch.rand(num_dest).mul(2).floor().to(dtype)
                        val = random.random()
                        dst2 = dst.clone()

                        dst.masked_fill_(mask, val)
                        for i in range(num_dest):
                            if mask[i]:
                                dst2[i] = val
                        self.assertEqual(dst, dst2, atol=0, rtol=0)

                        # test non-contiguous case
                        dst = torch.randn(num_dest, num_dest, num_dest).permute((2, 0, 1))
                        dst2 = dst.clone()
                        dst.masked_fill_((dst > 0).to(dtype), val)
                        dst2.masked_fill_((dst2 > 0).to(dtype), val)
                        self.assertEqual(dst, dst2, atol=0, rtol=0)

                self.assertEqual(len(w), 36)

                warn = 'masked_fill_ received a mask with dtype torch.uint8,'
                for wi in w:
                    self.assertEqual(str(wi.message)[0:52], str(warn))


        def test_unbiased(self):
            tensor = torch.randn(100)
            self.assertEqual(tensor.var(0), tensor.var(0, unbiased=True))
            self.assertEqual(tensor.var(), tensor.var(unbiased=True))
            self.assertEqual(tensor.var(unbiased=False), tensor.var(0, unbiased=False))

            tensor = torch.FloatTensor([1.0, 2.0])
            self.assertEqual(tensor.var(unbiased=True), 0.5)
            self.assertEqual(tensor.var(unbiased=False), 0.25)

            tensor = torch.FloatTensor([1.0, 2.0, 3.0])
            self.assertEqual(tensor.var(unbiased=True), 1.0)
            self.assertEqual(tensor.var(unbiased=False), 2.0 / 3.0)

            tensor = torch.randn(100)
            self.assertEqual(tensor.std(0), tensor.std(0, unbiased=True))
            self.assertEqual(tensor.std(), tensor.std(unbiased=True))
            self.assertEqual(tensor.std(unbiased=False), tensor.std(0, unbiased=False))

        def test_structseq_repr(self):
            a = torch.arange(250).reshape(5, 5, 10)
            expected = """
            torch.return_types.max(
            values=tensor([[ 40,  41,  42,  43,  44,  45,  46,  47,  48,  49],
                    [ 90,  91,  92,  93,  94,  95,  96,  97,  98,  99],
                    [140, 141, 142, 143, 144, 145, 146, 147, 148, 149],
                    [190, 191, 192, 193, 194, 195, 196, 197, 198, 199],
                    [240, 241, 242, 243, 244, 245, 246, 247, 248, 249]]),
            indices=tensor([[4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
                    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
                    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
                    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4],
                    [4, 4, 4, 4, 4, 4, 4, 4, 4, 4]]))"""
            self.assertEqual(repr(a.max(1)), textwrap.dedent(expected).strip())

        def test_var_stability(self):
            tensor = torch.FloatTensor([2281.5, 2281.25])
            self.assertEqual(tensor.var(dim=0), 0.03125)
            self.assertEqual(tensor.var(), 0.03125)

        # TODO: this should be refactored into the view ops test suite
        def test_view_empty(self):
            x = torch.randn(0, 6)
            self.assertEqual((1, 0, 6, 1, 1), x.view(1, 0, 6, 1, 1).shape)

        # TODO: this should be refactored into the view ops test suite
        def test_reshape(self):
            x = torch.randn(3, 3)
            self.assertEqual(x.data_ptr(), x.reshape(-1).data_ptr())
            self.assertEqual(x.data_ptr(), x.reshape(1, 9, 1).data_ptr())
            self.assertEqual(torch.reshape(x, (9,)), x.reshape(9))
            self.assertRaises(RuntimeError, lambda: x.reshape(-1, -1))

            y = torch.randn(4, 4, 4)[:, 0, :]
            self.assertNotEqual(y.data_ptr(), y.reshape(-1).data_ptr())
            self.assertEqual(y.contiguous().view(-1), y.reshape(-1))
            self.assertEqual(y.reshape(2, 2, 4).data_ptr(), y.data_ptr())

            s = torch.randn(())
            self.assertEqual(s.data_ptr(), s.reshape(()).data_ptr())
            self.assertEqual(s.reshape(-1).shape, (1,))
            self.assertRaises(RuntimeError, lambda: s.reshape(2))

            empty = torch.tensor([])
            self.assertEqual(empty, empty.reshape(-1))
            self.assertEqual(empty, empty.reshape([0]))
            # TODO: fix these once we have multi-dimensional empty tensors
            self.assertEqual(empty.reshape([0, 1]).shape, (0, 1))
            self.assertEqual(empty.reshape([1, -1]).shape, (1, 0))
            self.assertRaises(RuntimeError, lambda: empty.reshape(1))

            x = torch.randn(3, 3)
            self.assertEqual(x.data_ptr(), x.reshape_as(torch.rand(9)).data_ptr())
            self.assertEqual(x.data_ptr(), x.reshape_as(torch.rand(1, 9, 1)).data_ptr())
            self.assertRaises(RuntimeError, lambda: x.reshape_as(torch.rand(10)))

        # TODO: this should be refactored into the view ops test suite
        def test_empty_reshape(self):
            x = torch.randn(0, 6)
            self.assertEqual((1, 0, 6, 1, 1), x.reshape(1, 0, 6, 1, 1).shape)
            # should be viewable -- i.e. data_ptr is the same.
            self.assertEqual(x.data_ptr(), x.reshape(1, 0, 6, 1, 1).data_ptr())

            # match NumPy semantics -- don't infer the size of dimension with a degree of freedom
            self.assertRaises(RuntimeError, lambda: x.reshape(0, -1))

        def check_single_matmul(self, x, y, shape):
            a = np.array(x, copy=False)
            b = np.array(y, copy=False)
            expected = np.matmul(a, b)

            ans = torch.matmul(x, y)
            self.assertTrue(ans.is_contiguous())
            self.assertTrue(np.array_equal(ans, expected))

            out = torch.zeros(*shape, dtype=torch.int64)
            ans = torch.matmul(x, y, out=out)
            self.assertIs(ans, out)
            self.assertTrue(ans.is_contiguous())
            self.assertTrue(np.array_equal(ans, expected))

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_matmul_small_brute_force_1d_Nd(self):
            # Issue #20452: range(0, 10) does not work.
            n = 1
            for m in range(1, 8):
                for p in range(1, 8):
                    for o in range(1, 5):
                        # 1d, 3d, inner dimensions C
                        x = torch.arange(m)
                        y = torch.arange(o * m * p).reshape(o, m, p)
                        self.check_single_matmul(x, y, (o, n, p))

                        # 1d, 3d, inner dimensions Fortran
                        x = torch.arange(m)
                        y = torch.arange(o * p * m).reshape(o, p, m).transpose(-1, -2)
                        self.check_single_matmul(x, y, (o, n, p))

                        # 1d, 3d, inner dimensions non-contiguous
                        x = torch.arange(2 * m)[::2]
                        y = torch.arange(o * m * 2 * p).reshape(o, m, 2 * p)[:, :, ::2]
                        self.check_single_matmul(x, y, (o, n, p))

                        for r in range(1, 5):
                            # 1d, 4d, inner dimensions C
                            x = torch.arange(m)
                            y = torch.arange(r * o * m * p).reshape(r, o, m, p)
                            self.check_single_matmul(x, y, (r, o, n, p))

                            # 1d, 4d, inner dimensions Fortran
                            x = torch.arange(m)
                            y = torch.arange(r * o * p * m).reshape(r, o, p, m).transpose(-1, -2)
                            self.check_single_matmul(x, y, (r, o, n, p))

                            # 1d, 4d, inner dimensions non-contiguous
                            x = torch.arange(2 * m)[::2]
                            y = torch.arange(r * o * m * 2 * p).reshape(r, o, m, 2 * p)[:, :, :, ::2]
                            self.check_single_matmul(x, y, (r, o, n, p))

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_matmul_small_brute_force_2d_Nd(self):
            # Issue #20452: range(0, 10) does not work.
            for n in range(1, 5):
                for m in range(1, 5):
                    for p in range(1, 5):
                        for o in range(1, 3):
                            # 2d, 3d, inner dimensions C
                            x = torch.arange(n * m).reshape(n, m)
                            y = torch.arange(o * m * p).reshape(o, m, p)
                            self.check_single_matmul(x, y, (o, n, p))

                            # 2d, 3d, inner dimensions Fortran
                            x = torch.arange(m * n).reshape(m, n).transpose(-1, -2)
                            y = torch.arange(o * p * m).reshape(o, p, m).transpose(-1, -2)
                            self.check_single_matmul(x, y, (o, n, p))

                            # 2d, 3d, inner dimensions non-contiguous
                            x = torch.arange(n * 2 * m).reshape(n, 2 * m)[:, ::2]
                            y = torch.arange(o * m * 2 * p).reshape(o, m, 2 * p)[:, :, ::2]
                            self.check_single_matmul(x, y, (o, n, p))

                            for r in range(1, 2):
                                # 2d, 4d, inner dimensions C
                                x = torch.arange(n * m).reshape(n, m)
                                y = torch.arange(r * o * m * p).reshape(r, o, m, p)
                                self.check_single_matmul(x, y, (r, o, n, p))

                                # 2d, 4d, inner dimensions Fortran
                                x = torch.arange(m * n).reshape(m, n).transpose(-1, -2)
                                y = torch.arange(r * o * p * m).reshape(r, o, p, m).transpose(-1, -2)
                                self.check_single_matmul(x, y, (r, o, n, p))

                                # 2d, 4d, inner dimensions non-contiguous
                                x = torch.arange(n * 2 * m).reshape(n, 2 * m)[:, ::2]
                                y = torch.arange(r * o * m * 2 * p).reshape(r, o, m, 2 * p)[:, :, :, ::2]
                                self.check_single_matmul(x, y, (r, o, n, p))

        def test_expand(self):
            tensor = torch.rand(1, 8, 1)
            tensor2 = torch.rand(5)
            template = torch.rand(4, 8, 5)
            target = template.size()
            self.assertEqual(tensor.expand_as(template).size(), target)
            self.assertEqual(tensor.expand(4, 8, 5).size(), target)
            self.assertEqual(tensor.expand(target).size(), target)
            self.assertEqual(tensor2.expand_as(template).size(), target)
            self.assertEqual(tensor2.expand(4, 8, 5).size(), target)
            self.assertEqual(tensor2.expand(target).size(), target)

            # test double expand
            self.assertEqual(tensor2.expand(1, 5).expand(2, 2, 5), tensor2.repeat(2, 2, 1))

            # test non-contiguous
            noncontig = torch.randn(5, 2, 1, 3)[:, 0]
            self.assertFalse(noncontig.is_contiguous())
            self.assertEqual(noncontig.expand(2, 5, 4, 3), noncontig.contiguous().repeat(2, 1, 4, 1))

            # make sure it's compatible with unsqueeze
            expanded = tensor2.expand(1, 1, 5)
            unsqueezed = tensor2.unsqueeze(0).unsqueeze(1)
            self.assertEqual(expanded, unsqueezed)
            self.assertEqual(expanded.stride(), unsqueezed.stride())

            # test -1 as target size
            self.assertEqual(tensor.expand(4, -1, 5), tensor.expand(4, 8, 5))
            self.assertRaises(RuntimeError, lambda: tensor2.expand(-1, -1))

            # test expanding empty to empty
            self.assertEqual(torch.zeros(0).expand((0,)), torch.zeros(0))

        def test_repeat(self):
            initial_shape = (8, 4)
            tensor = torch.rand(*initial_shape)

            size = (3, 1, 1)
            torchSize = torch.Size(size)
            target = [3, 8, 4]
            self.assertEqual(tensor.repeat(*size).size(), target, msg='Error in repeat')
            self.assertEqual(tensor.repeat(torchSize).size(), target,
                             msg='Error in repeat using LongStorage')
            result = tensor.repeat(*size)
            self.assertEqual(result.size(), target, msg='Error in repeat using result')
            result = tensor.repeat(torchSize)
            self.assertEqual(result.size(), target, msg='Error in repeat using result and LongStorage')
            self.assertEqual(result.mean(0).view(8, 4), tensor, msg='Error in repeat (not equal)')

            zeroDimTarget = torch.Size([24, 0])
            self.assertEqual(tensor.repeat((3, 0)).size(), zeroDimTarget, msg="Error when calling with 0 repeats")

        def test_repeat_interleave(self):
            x = torch.tensor([0, 1, 2, 3])
            expected = torch.tensor([1, 2, 2, 3, 3, 3])
            self.assertEqual(torch.repeat_interleave(x), expected)

            with self.assertRaises(RuntimeError):
                torch.repeat_interleave(torch.arange(4).reshape(2, 2))

            with self.assertRaises(RuntimeError):
                torch.repeat_interleave(torch.arange(4.0))

            with self.assertRaises(RuntimeError):
                torch.repeat_interleave(torch.tensor([1, 2, -1, 3, 4]))

            y = torch.tensor([[1, 2], [3, 4]])

            y1_v1 = torch.repeat_interleave(y, 2)
            y1_v2 = torch.repeat_interleave(y, torch.tensor(2))
            y1_v3 = torch.repeat_interleave(y, torch.tensor([2]))
            y1_expect = torch.tensor([1, 1, 2, 2, 3, 3, 4, 4])
            self.assertEqual(y1_v1, y1_expect)
            self.assertEqual(y1_v2, y1_expect)
            self.assertEqual(y1_v3, y1_expect)

            y2 = torch.repeat_interleave(y, 3, dim=1)
            y2_expect = torch.tensor([[1, 1, 1, 2, 2, 2],
                                      [3, 3, 3, 4, 4, 4]])
            self.assertEqual(y2, y2_expect)

            y3 = torch.repeat_interleave(y, torch.tensor([1, 2]), dim=0)
            y3_expect = torch.tensor([[1, 2],
                                      [3, 4],
                                      [3, 4]])
            self.assertEqual(y3, y3_expect)

            with self.assertRaises(RuntimeError):
                torch.repeat_interleave(y, torch.tensor([1, 2, 3]), dim=0)

            with self.assertRaises(RuntimeError):
                torch.repeat_interleave(y, torch.arange(9).reshape(3, 3), dim=0)

            # test zero sized dimension
            x = torch.zeros((5, 0))
            y = torch.repeat_interleave(x, repeats=3, dim=1)
            self.assertEqual(y, x.new_zeros(5, 0))

            x = torch.tensor([], dtype=torch.int64)
            y = torch.repeat_interleave(x, x)
            self.assertEqual(y, x)

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_repeat_tile(self):

            initial_shape = (8, 4)

            repeats = ((3, 1, 1),
                       (3, 3, 3),
                       (1, 2, 1),
                       (2, 2, 2, 2))

            def _generate_noncontiguous_input():

                out = np.broadcast_to(np.random.random((1, 4)),
                                      initial_shape)
                # Note: non-writeable NumPy arrays will warn if converted to tensors
                out.setflags(write=True)

                assert not (out.flags.c_contiguous or out.flags.f_contiguous)

                return out

            for repeat in repeats:
                for tensor in (torch.from_numpy(np.random.random(initial_shape)),
                               torch.from_numpy(_generate_noncontiguous_input()),):

                    self.assertEqual(tensor.repeat(*repeat).numpy(),
                                     np.tile(tensor.numpy(), repeat))

        def test_is_same_size(self):
            t1 = torch.Tensor(3, 4, 9, 10)
            t2 = torch.Tensor(3, 4)
            t3 = torch.Tensor(1, 9, 3, 3)
            t4 = torch.Tensor(3, 4, 9, 10)

            self.assertFalse(t1.is_same_size(t2))
            self.assertFalse(t1.is_same_size(t3))
            self.assertTrue(t1.is_same_size(t4))

        def test_tensor_set(self):
            t1 = torch.Tensor()
            t2 = torch.Tensor(3, 4, 9, 10).uniform_()
            t1.set_(t2)
            self.assertEqual(t1.storage()._cdata, t2.storage()._cdata)
            size = torch.Size([9, 3, 4, 10])
            t1.set_(t2.storage(), 0, size)
            self.assertEqual(t1.size(), size)
            t1.set_(t2.storage(), 0, tuple(size))
            self.assertEqual(t1.size(), size)
            self.assertEqual(t1.stride(), (120, 40, 10, 1))
            stride = (10, 360, 90, 1)
            t1.set_(t2.storage(), 0, size, stride)
            self.assertEqual(t1.stride(), stride)
            t1.set_(t2.storage(), 0, size=size, stride=stride)
            self.assertEqual(t1.size(), size)
            self.assertEqual(t1.stride(), stride)

            # test argument names
            t1 = torch.Tensor()
            # 1. case when source is tensor
            t1.set_(source=t2)
            self.assertEqual(t1.storage()._cdata, t2.storage()._cdata)
            # 2. case when source is storage
            t1.set_(source=t2.storage())
            self.assertEqual(t1.storage()._cdata, t2.storage()._cdata)
            # 3. case when source is storage, and other args also specified
            t1.set_(source=t2.storage(), storage_offset=0, size=size, stride=stride)
            self.assertEqual(t1.size(), size)
            self.assertEqual(t1.stride(), stride)

            t1 = torch.tensor([True, True], dtype=torch.bool)
            t2 = torch.tensor([False, False], dtype=torch.bool)
            t1.set_(t2)
            self.assertEqual(t1.storage()._cdata, t2.storage()._cdata)

        def test_tensor_set_errors(self):
            f_cpu = torch.randn((2, 3), dtype=torch.float32)
            d_cpu = torch.randn((2, 3), dtype=torch.float64)

            # change dtype
            self.assertRaises(RuntimeError, lambda: f_cpu.set_(d_cpu.storage()))
            self.assertRaises(RuntimeError,
                              lambda: f_cpu.set_(d_cpu.storage(), 0, d_cpu.size(), d_cpu.stride()))
            self.assertRaises(RuntimeError, lambda: f_cpu.set_(d_cpu))

            # change device
            if torch.cuda.is_available():
                f_cuda = torch.randn((2, 3), dtype=torch.float32, device='cuda')

                # cpu -> cuda
                self.assertRaises(RuntimeError, lambda: f_cpu.set_(f_cuda.storage()))
                self.assertRaises(RuntimeError,
                                  lambda: f_cpu.set_(f_cuda.storage(), 0, f_cuda.size(), f_cuda.stride()))
                self.assertRaises(RuntimeError, lambda: f_cpu.set_(f_cuda))

                # cuda -> cpu
                self.assertRaises(RuntimeError, lambda: f_cuda.set_(f_cpu.storage()))
                self.assertRaises(RuntimeError,
                                  lambda: f_cuda.set_(f_cpu.storage(), 0, f_cpu.size(), f_cpu.stride()))
                self.assertRaises(RuntimeError, lambda: f_cuda.set_(f_cpu))

        def test_equal(self):
            # Contiguous, 1D
            t1 = torch.Tensor((3, 4, 9, 10))
            t2 = t1.contiguous()
            t3 = torch.Tensor((1, 9, 3, 10))
            t4 = torch.Tensor((3, 4, 9))
            t5 = torch.Tensor()
            self.assertTrue(t1.equal(t2))
            self.assertFalse(t1.equal(t3))
            self.assertFalse(t1.equal(t4))
            self.assertFalse(t1.equal(t5))
            self.assertTrue(torch.equal(t1, t2))
            self.assertFalse(torch.equal(t1, t3))
            self.assertFalse(torch.equal(t1, t4))
            self.assertFalse(torch.equal(t1, t5))

            # Non contiguous, 2D
            s = torch.Tensor(((1, 2, 3, 4), (5, 6, 7, 8)))
            s1 = s[:, 1:3]
            s2 = s1.clone()
            s3 = torch.Tensor(((2, 3), (6, 7)))
            s4 = torch.Tensor(((0, 0), (0, 0)))

            self.assertFalse(s1.is_contiguous())
            self.assertTrue(s1.equal(s2))
            self.assertTrue(s1.equal(s3))
            self.assertFalse(s1.equal(s4))
            self.assertTrue(torch.equal(s1, s2))
            self.assertTrue(torch.equal(s1, s3))
            self.assertFalse(torch.equal(s1, s4))

        def test_element_size(self):
            byte = torch.ByteStorage().element_size()
            char = torch.CharStorage().element_size()
            short = torch.ShortStorage().element_size()
            int = torch.IntStorage().element_size()
            long = torch.LongStorage().element_size()
            float = torch.FloatStorage().element_size()
            double = torch.DoubleStorage().element_size()
            bool = torch.BoolStorage().element_size()
            bfloat16 = torch.BFloat16Storage().element_size()
            complexfloat = torch.ComplexFloatStorage().element_size()
            complexdouble = torch.ComplexDoubleStorage().element_size()

            self.assertEqual(byte, torch.ByteTensor().element_size())
            self.assertEqual(char, torch.CharTensor().element_size())
            self.assertEqual(short, torch.ShortTensor().element_size())
            self.assertEqual(int, torch.IntTensor().element_size())
            self.assertEqual(long, torch.LongTensor().element_size())
            self.assertEqual(float, torch.FloatTensor().element_size())
            self.assertEqual(double, torch.DoubleTensor().element_size())
            self.assertEqual(bool, torch.BoolTensor().element_size())
            self.assertEqual(bfloat16, torch.tensor([], dtype=torch.bfloat16).element_size())
            self.assertEqual(complexfloat, torch.tensor([], dtype=torch.complex64).element_size())
            self.assertEqual(complexdouble, torch.tensor([], dtype=torch.complex128).element_size())

            self.assertGreater(byte, 0)
            self.assertGreater(char, 0)
            self.assertGreater(short, 0)
            self.assertGreater(int, 0)
            self.assertGreater(long, 0)
            self.assertGreater(float, 0)
            self.assertGreater(double, 0)
            self.assertGreater(bool, 0)
            self.assertGreater(bfloat16, 0)
            self.assertGreater(complexfloat, 0)
            self.assertGreater(complexdouble, 0)

            # These tests are portable, not necessarily strict for your system.
            self.assertEqual(byte, 1)
            self.assertEqual(char, 1)
            self.assertEqual(bool, 1)
            self.assertGreaterEqual(short, 2)
            self.assertGreaterEqual(int, 2)
            self.assertGreaterEqual(int, short)
            self.assertGreaterEqual(long, 4)
            self.assertGreaterEqual(long, int)
            self.assertGreaterEqual(double, float)

        def test_split(self):
            tensor = torch.rand(7, 4)
            split_size = 3
            dim = 0
            target_sizes = ([3, 4], [3, 4], [1, 4])
            splits = tensor.split(split_size, dim)
            start = 0
            for target_size, split in zip(target_sizes, splits):
                self.assertEqual(split.size(), target_size)
                self.assertEqual(tensor.narrow(dim, start, target_size[dim]), split,
                                 atol=0, rtol=0)
                start = start + target_size[dim]

            # Variable sections split
            tensor = torch.randn(20, 10)
            dim = 0
            split_sizes = [5, 5, 10]
            target_sizes = ([[5, 10], [5, 10], [10, 10]])
            splits = tensor.split(split_sizes, dim)
            start = 0
            for target_size, split in zip(target_sizes, splits):
                self.assertEqual(split.size(), target_size)
                self.assertEqual(tensor.narrow(dim, start, target_size[dim]), split,
                                 atol=0, rtol=0)
                start = start + target_size[dim]

            split_sizes = [2, 2, 6]
            target_sizes = ([20, 2], [20, 2], [20, 6])
            dim = 1
            splits = tensor.split(split_sizes, dim)
            start = 0
            for target_size, split in zip(target_sizes, splits):
                self.assertEqual(split.size(), target_size)
                self.assertEqual(tensor.narrow(dim, start, target_size[dim]), split,
                                 atol=0, rtol=0)
                start = start + target_size[dim]

        def test_chunk(self):
            tensor = torch.rand(4, 7)
            num_chunks = 3
            dim = 1
            target_sizes = ([4, 3], [4, 3], [4, 1])
            splits = tensor.chunk(num_chunks, dim)
            start = 0
            for target_size, split in zip(target_sizes, splits):
                self.assertEqual(split.size(), target_size)
                self.assertEqual(tensor.narrow(dim, start, target_size[dim]), split,
                                 atol=0, rtol=0)
                start = start + target_size[dim]

            # Invalid chunk sizes
            error_regex = 'chunk expects.*greater than 0'
            with self.assertRaisesRegex(RuntimeError, error_regex):
                tensor.chunk(0)
            with self.assertRaisesRegex(RuntimeError, error_regex):
                tensor.chunk(-2)

        def test_tolist(self):
            list0D = []
            tensor0D = torch.Tensor(list0D)
            self.assertEqual(tensor0D.tolist(), list0D)

            table1D = [1, 2, 3]
            tensor1D = torch.Tensor(table1D)
            storage = torch.Storage(table1D)
            self.assertEqual(tensor1D.tolist(), table1D)
            self.assertEqual(storage.tolist(), table1D)
            self.assertEqual(tensor1D.tolist(), table1D)
            self.assertEqual(storage.tolist(), table1D)

            table2D = [[1, 2], [3, 4]]
            tensor2D = torch.Tensor(table2D)
            self.assertEqual(tensor2D.tolist(), table2D)

            tensor3D = torch.Tensor([[[1, 2], [3, 4]], [[5, 6], [7, 8]]])
            tensorNonContig = tensor3D.select(1, 1)
            self.assertFalse(tensorNonContig.is_contiguous())
            self.assertEqual(tensorNonContig.tolist(), [[3, 4], [7, 8]])

        def test_permute(self):
            orig = [1, 2, 3, 4, 5, 6, 7]
            perm = torch.randperm(7).tolist()
            x = torch.Tensor(*orig).fill_(0)
            new = list(map(lambda x: x - 1, x.permute(*perm).size()))
            self.assertEqual(perm, new)
            self.assertEqual(x.size(), orig)

        def test_reversed(self):
            val = torch.arange(0, 10)
            self.assertEqual(reversed(val), torch.arange(9, -1, -1))

            val = torch.arange(1, 10).view(3, 3)
            self.assertEqual(reversed(val), torch.tensor([[7, 8, 9], [4, 5, 6], [1, 2, 3]]))

            val = torch.tensor(42)
            self.assertEqual(reversed(val), torch.tensor(42))

        def test_contains(self):
            x = torch.arange(0, 10)
            self.assertEqual(4 in x, True)
            self.assertEqual(12 in x, False)

            x = torch.arange(1, 10).view(3, 3)
            val = torch.arange(1, 4)
            self.assertEqual(val in x, True)
            val += 10
            self.assertEqual(val in x, False)

            self.assertRaisesRegex(
                RuntimeError,
                "Tensor.__contains__ only supports Tensor or scalar, but you passed in a {}.".format(type("foo")),
                lambda: "foo" in x)
            self.assertRaisesRegex(
                RuntimeError,
                "Tensor.__contains__ only supports Tensor or scalar, but you passed in a {}.".format(type([1, 2])),
                lambda: [1, 2] in x)

        def test_deepcopy_parameter(self):
            from copy import deepcopy
            l = torch.nn.Linear(10, 1)
            s = l.state_dict(keep_vars=True)
            self.assertEqual(torch.nn.Parameter, type(s['weight']))
            self.assertEqual(torch.nn.Parameter, type(s['bias']))

            s2 = deepcopy(s)
            self.assertEqual(torch.nn.Parameter, type(s2['weight']))
            self.assertEqual(torch.nn.Parameter, type(s2['bias']))

        def test_pickle(self):
            import pickle
            a = torch.randn(5, 5)
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertEqual(a, b)

        def test_pickle_parameter(self):
            import pickle
            a = torch.nn.Parameter(torch.randn(5, 5))
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertTrue(isinstance(b, torch.nn.Parameter))
            self.assertEqual(a.requires_grad, b.requires_grad)
            self.assertEqual(a, b)

        def test_pickle_parameter_no_requires_grad(self):
            import pickle
            a = torch.nn.Parameter(torch.randn(5, 5), requires_grad=False)
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertTrue(isinstance(b, torch.nn.Parameter))
            self.assertEqual(a.requires_grad, b.requires_grad)
            self.assertEqual(a, b)

        def test_pickle_dtype(self):
            t = torch.float32
            serialized = pickle.dumps(t)
            b = pickle.loads(serialized)
            self.assertTrue(isinstance(b, torch.dtype))
            self.assertEqual(id(b), id(t))

        def test_pickle_size(self):
            a = torch.rand(10).size()
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertTrue(isinstance(b, torch.Size))
            self.assertEqual(a, b)

        def test_pickle_function(self):
            # https://github.com/pytorch/pytorch/issues/37703
            a = torch.tanh
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertEqual(a, b)

        def test_generator_cpu(self):
            # test default generators are equal
            self.assertEqual(torch.default_generator, torch.default_generator)

            # tests Generator API
            # manual_seed, seed, initial_seed, get_state, set_state
            g1 = torch.Generator()
            g2 = torch.Generator()
            g1.manual_seed(12345)
            g2.manual_seed(12345)
            self.assertEqual(g1.initial_seed(), g2.initial_seed())

            g1.seed()
            g2.seed()
            self.assertNotEqual(g1.initial_seed(), g2.initial_seed())

            g1 = torch.Generator()
            g2_state = g2.get_state()
            g2_randn = torch.randn(1, generator=g2)
            g1.set_state(g2_state)
            g1_randn = torch.randn(1, generator=g1)
            self.assertEqual(g1_randn, g2_randn)

            default_state = torch.default_generator.get_state()
            q = torch.Tensor(100)
            g1_normal = q.normal_()
            g2 = torch.Generator()
            g2.set_state(default_state)
            g2_normal = q.normal_(generator=g2)
            self.assertEqual(g1_normal, g2_normal)

        def test_invalid_generator_raises(self):
            self.assertRaises(RuntimeError, lambda: torch.Generator('opengl'))

        def test_sobolengine_unscrambled_lowdim(self):
            engine_1d = torch.quasirandom.SobolEngine(1)
            expected_1d = torch.tensor([0.5, 0.75, 0.25, 0.375, 0.875, 0.625, 0.125, 0.1875, 0.6875, 0.9375])
            actual_1d = engine_1d.draw(10)
            self.assertEqual(actual_1d.view(-1), expected_1d)
            self.assertEqual(actual_1d.size(), torch.Size([10, 1]))

            # Test out kwarg
            engine_1d.reset()
            actual_1d_out = torch.Tensor().float()
            engine_1d.draw(10, out=actual_1d_out)
            self.assertEqual(actual_1d.view(-1), expected_1d)

            engine_3d = torch.quasirandom.SobolEngine(3)
            expected_3d = torch.tensor([0.5, 0.75, 0.25, 0.625, 0.125, 0.375, 0.875, 0.3125, 0.8125, 0.5625])
            actual_3d = engine_3d.draw(10)
            self.assertEqual(actual_3d[:, 2], expected_3d)
            self.assertEqual(actual_3d[:, 0], expected_1d)
            self.assertEqual(actual_3d.size(), torch.Size([10, 3]))

            engine_3d = torch.quasirandom.SobolEngine(3)
            draws = torch.cat([engine_3d.draw() for _ in range(0, 10)])
            self.assertEqual(draws, actual_3d)

            engine_3d = torch.quasirandom.SobolEngine(3).fast_forward(5)
            draws = engine_3d.draw(5)
            self.assertEqual(draws, actual_3d[5:])
            engine_3d.reset()
            self.assertEqual(engine_3d.draw(3), actual_3d[:3])
            engine_3d.fast_forward(2)
            self.assertEqual(engine_3d.draw(5), actual_3d[5:])

        def test_sobolengine_unscrambled_highdim(self):
            from collections import Counter
            engine = torch.quasirandom.SobolEngine(1111)
            count1 = dict(Counter(engine.draw().view(-1).tolist()))
            count2 = dict(Counter(engine.draw().view(-1).tolist()))
            count3 = dict(Counter(engine.draw().view(-1).tolist()))
            self.assertTrue(count1 == {0.5: 1111})
            self.assertTrue(count2 == {0.25: 580, 0.75: 531})
            self.assertTrue(count3 == {0.25: 531, 0.75: 580})

            engine = torch.quasirandom.SobolEngine(1111)
            draws = engine.draw(1000)
            self.assertTrue(torch.all(draws <= 1))
            self.assertTrue(torch.all(draws >= 0))

        def test_sobolengine_scrambled_lowdim(self):
            engine_1d = torch.quasirandom.SobolEngine(1, scramble=True, seed=1729)
            expected_1d = [0.16478512, 0.43221009, 0.84261382, 0.99750268, 0.27460563,
                           0.01084163, 0.73373985, 0.65039611, 0.12329865, 0.35587373]
            actual_1d = engine_1d.draw(10)
            self.assertEqual(actual_1d.flatten(), torch.tensor(expected_1d), atol=1e-5, rtol=0)
            self.assertEqual(actual_1d.size(), torch.Size([10, 1]))
            # make sure random seed if chosen if none is provided
            engine_1d_a = torch.quasirandom.SobolEngine(1, scramble=True)
            engine_1d_b = torch.quasirandom.SobolEngine(1, scramble=True)
            self.assertNotEqual(engine_1d_a.draw(2), engine_1d_b.draw(2))

            engine_3d = torch.quasirandom.SobolEngine(3, scramble=True, seed=1729)
            expected_3d = [0.32642800, 0.17881306, 0.68837059, 0.46492538, 0.91789097,
                           0.58075899, 0.03642474, 0.68229187, 0.20051685, 0.30083340]
            actual_3d = engine_3d.draw(10)
            self.assertEqual(actual_3d[:, 2], torch.tensor(expected_3d))
            self.assertEqual(actual_3d.size(), torch.Size([10, 3]))

            engine_3d = torch.quasirandom.SobolEngine(3, scramble=True, seed=1729)
            draws = torch.cat([engine_3d.draw() for _ in range(0, 10)])
            self.assertEqual(draws, actual_3d)

            engine_3d = torch.quasirandom.SobolEngine(3, scramble=True, seed=1729)
            engine_3d.fast_forward(5)
            draws = engine_3d.draw(5)
            self.assertEqual(draws, actual_3d[5:])
            engine_3d.reset()
            self.assertEqual(engine_3d.draw(3), actual_3d[:3])
            engine_3d.fast_forward(2)
            self.assertEqual(engine_3d.draw(5), actual_3d[5:])

        def test_sobolengine_scrambled_lowdim_default_rng(self):
            expected_1d = [0.039826, 0.484409, 0.953192, 0.799275, 0.267996]
            torch.manual_seed(123456)
            engine_1d = torch.quasirandom.SobolEngine(1, scramble=True)
            actual_1d = engine_1d.draw(5)
            self.assertEqual(actual_1d[:, 0], expected_1d)
            torch.manual_seed(123456)
            expected_3d = [0.133490, 0.480183, 0.855304, 0.970967, 0.345844]
            engine_3d = torch.quasirandom.SobolEngine(3, scramble=True)
            actual_3d = engine_3d.draw(5)
            self.assertEqual(actual_3d[:, 0], expected_3d)

        def test_sobolengine_scrambled_highdim(self):
            engine = torch.quasirandom.SobolEngine(1111, scramble=True)
            draws = engine.draw(1000)
            self.assertTrue(torch.all(draws <= 1))
            self.assertTrue(torch.all(draws >= 0))

        def test_parsing_int64(self):
            # accepts integer arguments
            x = torch.cumsum(torch.ones(5, 5), 0)
            self.assertEqual(x, torch.cumsum(torch.ones(5, 5), torch.tensor(0)))
            # doesn't accept floating point variables
            self.assertRaises(TypeError, lambda: torch.cumsum(torch.ones(5, 5), torch.tensor(0.)))

        def test_parsing_double(self):
            # accepts floating point and integer arguments
            x = torch.randn(2, 3)
            torch.isclose(x, x, 1, 1)
            self.assertTrue(torch.isclose(x, x, 1, 1).all())
            self.assertTrue(torch.isclose(x, x, 1.5, 1.).all())
            # accepts floating point and integer tensors
            self.assertTrue(torch.isclose(x, x, torch.tensor(1), torch.tensor(1)).all())
            self.assertTrue(torch.isclose(x, x, torch.tensor(1.5), torch.tensor(1.)).all())
            # doesn't accept variables with requires_grad
            self.assertRaises(TypeError,
                              lambda: torch.isclose(x, x, torch.tensor(1.5), torch.tensor(1., requires_grad=True)).all())

        def test_parsing_intlist(self):
            #  parse with integer variables
            self.assertEqual(torch.Size([3, 4]), torch.ones((torch.tensor(3), torch.tensor(4))).shape)
            self.assertEqual(torch.Size([3, 4]), torch.ones(torch.tensor(3), torch.tensor(4)).shape)
            # parse with numpy integers
            if TEST_NUMPY:
                self.assertEqual(torch.Size([3, 4]), torch.ones((np.array(3), np.int64(4))).shape)
                self.assertEqual(torch.Size([3, 4]), torch.ones(np.array(3), np.int64(4)).shape)
                self.assertEqual(torch.Size([3, 4]), torch.ones((np.int64(3), np.array(4))).shape)
                self.assertEqual(torch.Size([3, 4]), torch.ones(np.int64(3), np.array(4)).shape)

            # fail parse with float variables
            self.assertRaises(TypeError, lambda: torch.ones((torch.tensor(3.), torch.tensor(4))))
            # fail parse with numpy floats
            if TEST_NUMPY:
                self.assertRaises(TypeError, lambda: torch.ones((np.float(3.), torch.tensor(4))))
                self.assertRaises(TypeError, lambda: torch.ones((np.array(3.), torch.tensor(4))))

            # fail parse with > 1 element variables
            self.assertRaises(TypeError, lambda: torch.ones(torch.tensor(3, 3)))
            self.assertRaises(TypeError, lambda: torch.ones((torch.tensor(3, 3))))
            if TEST_NUMPY:
                self.assertRaises(TypeError, lambda: torch.ones(np.array(3, 3)))
                self.assertRaises(TypeError, lambda: torch.ones((np.array(3, 3))))

            # fail parse with additional positional args after intlist arg
            self.assertRaisesRegex(TypeError,
                                   "received an invalid combination of arguments",
                                   lambda: torch.LongTensor((6, 0), 1, 1, 0))
            self.assertRaisesRegex(TypeError,
                                   "missing 1 required positional arguments",
                                   lambda: torch.tensor().new_zeros((5, 5), 0))

        def test_half_tensor(self):
            x = torch.randn(5, 5).float()
            y = torch.randn(5, 5).float()
            xh, yh = x.half(), y.half()

            self.assertEqual(x.half().float(), x, atol=1e-3, rtol=0)

            z = torch.Tensor(5, 5)
            self.assertEqual(z.copy_(xh), x, atol=1e-3, rtol=0)

            with tempfile.NamedTemporaryFile() as f:
                torch.save(xh, f)
                f.seek(0)
                xh2 = torch.load(f)
                self.assertEqual(xh.float(), xh2.float())

        def test_from_buffer(self):
            a = bytearray([1, 2, 3, 4])
            self.assertEqual(torch.ByteStorage.from_buffer(a).tolist(), [1, 2, 3, 4])
            shorts = torch.ShortStorage.from_buffer(a, 'big')
            self.assertEqual(shorts.size(), 2)
            self.assertEqual(shorts.tolist(), [258, 772])
            ints = torch.IntStorage.from_buffer(a, 'little')
            self.assertEqual(ints.size(), 1)
            self.assertEqual(ints[0], 67305985)
            f = bytearray([0x40, 0x10, 0x00, 0x00])
            floats = torch.FloatStorage.from_buffer(f, 'big')
            self.assertEqual(floats.size(), 1)
            self.assertEqual(floats[0], 2.25)

            f = bytearray([0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x40])
            bools = torch.BoolStorage.from_buffer(f, 'big')
            self.assertEqual(bools.size(), 8)
            self.assertEqual(bools.tolist(), [False, True, True, True, True, True, True, True])
            self.assertEqual(bools.type(), 'torch.BoolStorage')

            f = bytearray(b'\x80\x02\x8a\nl\xfc\x9cF\xf9 j\xa8P\x19.\x80\x02M\xe9')
            bools = torch.BoolStorage.from_buffer(f, 'big')
            self.assertEqual(bools.size(), 19)

            f = bytearray(b'\0x4A')
            bools = torch.BoolStorage.from_buffer(f, 'big')
            self.assertEqual(bools.size(), 4)
            self.assertEqual(bools.tolist(), [False, True, True, True])

        def test_storage_casts(self):
            storage = torch.IntStorage([-1, 0, 1, 2, 3, 4])
            self.assertEqual(storage.size(), 6)
            self.assertEqual(storage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(storage.type(), 'torch.IntStorage')
            self.assertIs(storage.dtype, torch.int32)

            floatStorage = storage.float()
            self.assertEqual(floatStorage.size(), 6)
            self.assertEqual(floatStorage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(floatStorage.type(), 'torch.FloatStorage')
            self.assertEqual(floatStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(floatStorage.dtype, torch.float32)

            halfStorage = storage.half()
            self.assertEqual(halfStorage.size(), 6)
            self.assertEqual(halfStorage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(halfStorage.type(), 'torch.HalfStorage')
            self.assertEqual(halfStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(halfStorage.dtype, torch.float16)

            bfloat16Storage = storage.bfloat16()
            self.assertEqual(bfloat16Storage.size(), 6)
            self.assertEqual(bfloat16Storage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(bfloat16Storage.type(), 'torch.BFloat16Storage')
            self.assertEqual(bfloat16Storage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(bfloat16Storage.dtype, torch.bfloat16)

            longStorage = storage.long()
            self.assertEqual(longStorage.size(), 6)
            self.assertEqual(longStorage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(longStorage.type(), 'torch.LongStorage')
            self.assertEqual(longStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(longStorage.dtype, torch.int64)

            shortStorage = storage.short()
            self.assertEqual(shortStorage.size(), 6)
            self.assertEqual(shortStorage.tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertEqual(shortStorage.type(), 'torch.ShortStorage')
            self.assertEqual(shortStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(shortStorage.dtype, torch.int16)

            doubleStorage = storage.double()
            self.assertEqual(doubleStorage.size(), 6)
            self.assertEqual(doubleStorage.tolist(), [-1.0, 0.0, 1.0, 2.0, 3.0, 4.0])
            self.assertEqual(doubleStorage.type(), 'torch.DoubleStorage')
            self.assertEqual(doubleStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(doubleStorage.dtype, torch.float64)

            charStorage = storage.char()
            self.assertEqual(charStorage.size(), 6)
            self.assertEqual(charStorage.tolist(), [-1.0, 0.0, 1.0, 2.0, 3.0, 4.0])
            self.assertEqual(charStorage.type(), 'torch.CharStorage')
            self.assertEqual(charStorage.int().tolist(), [-1, 0, 1, 2, 3, 4])
            self.assertIs(charStorage.dtype, torch.int8)

            byteStorage = storage.byte()
            self.assertEqual(byteStorage.size(), 6)
            self.assertEqual(byteStorage.tolist(), [255, 0, 1, 2, 3, 4])
            self.assertEqual(byteStorage.type(), 'torch.ByteStorage')
            self.assertEqual(byteStorage.int().tolist(), [255, 0, 1, 2, 3, 4])
            self.assertIs(byteStorage.dtype, torch.uint8)

            boolStorage = storage.bool()
            self.assertEqual(boolStorage.size(), 6)
            self.assertEqual(boolStorage.tolist(), [True, False, True, True, True, True])
            self.assertEqual(boolStorage.type(), 'torch.BoolStorage')
            self.assertEqual(boolStorage.int().tolist(), [1, 0, 1, 1, 1, 1])
            self.assertIs(boolStorage.dtype, torch.bool)

            complexfloat_storage = torch.ComplexFloatStorage([-1, 0, 1 + 2j, 2.5j, 3.5, 4 - 2j])
            self.assertEqual(complexfloat_storage.size(), 6)
            self.assertEqual(complexfloat_storage.tolist(), [-1, 0, 1 + 2j, 2.5j, 3.5, 4 - 2j])
            self.assertEqual(complexfloat_storage.type(), 'torch.ComplexFloatStorage')
            self.assertIs(complexfloat_storage.dtype, torch.complex64)

            complexdouble_storage = complexfloat_storage.complex_double()
            self.assertEqual(complexdouble_storage.size(), 6)
            self.assertEqual(complexdouble_storage.tolist(), [-1, 0, 1 + 2j, 2.5j, 3.5, 4 - 2j])
            self.assertEqual(complexdouble_storage.type(), 'torch.ComplexDoubleStorage')
            self.assertIs(complexdouble_storage.dtype, torch.complex128)

        @unittest.skipIf(IS_WINDOWS, "TODO: need to fix this test case for Windows")
        def test_from_file(self):
            size = 10000
            with tempfile.NamedTemporaryFile() as f:
                s1 = torch.FloatStorage.from_file(f.name, True, size)
                t1 = torch.FloatTensor(s1).copy_(torch.randn(size))

                # check mapping
                s2 = torch.FloatStorage.from_file(f.name, True, size)
                t2 = torch.FloatTensor(s2)
                self.assertEqual(t1, t2, atol=0, rtol=0)

                # check changes to t1 from t2
                rnum = random.uniform(-1, 1)
                t1.fill_(rnum)
                self.assertEqual(t1, t2, atol=0, rtol=0)

                # check changes to t2 from t1
                rnum = random.uniform(-1, 1)
                t2.fill_(rnum)
                self.assertEqual(t1, t2, atol=0, rtol=0)

        @unittest.skipIf(IS_WINDOWS, "TODO: need to fix this test case for Windows")
        def test_torch_from_file(self):
            size = 10000
            with tempfile.NamedTemporaryFile() as f:
                s1 = torch.from_file(f.name, True, size, dtype=torch.float)
                t1 = torch.FloatTensor(s1).copy_(torch.randn(size))

                # check mapping
                s2 = torch.from_file(f.name, True, size, dtype=torch.float)
                t2 = torch.FloatTensor(s2)
                self.assertEqual(t1, t2, atol=0, rtol=0)

                # check changes to t1 from t2
                rnum = random.uniform(-1, 1)
                t1.fill_(rnum)
                self.assertEqual(t1, t2, atol=0, rtol=0)

                # check changes to t2 from t1
                rnum = random.uniform(-1, 1)
                t2.fill_(rnum)
                self.assertEqual(t1, t2, atol=0, rtol=0)

        def test_print(self):
            default_type = torch.Tensor().type()
            for t in torch._tensor_classes:
                if t == torch.HalfTensor:
                    continue  # HalfTensor does not support fill
                if t.is_sparse:
                    continue
                if t.is_cuda and not torch.cuda.is_available():
                    continue
                obj = t(100, 100).fill_(1)
                obj.__repr__()
                str(obj)
            # test half tensor
            obj = torch.rand(100, 100, device='cpu').half()
            obj.__repr__()
            str(obj)
            for t in torch._storage_classes:
                if t == torch.BFloat16Storage:
                    continue  # Fix once fill is enabled for bfloat16
                if t.is_cuda and not torch.cuda.is_available():
                    continue
                if t == torch.BoolStorage or t == torch.cuda.BoolStorage:
                    obj = t(100).fill_(True)
                else:
                    obj = t(100).fill_(1)
                obj.__repr__()
                str(obj)

            # test complex tensor
            # complex tensor print uses two formatters, one for real values
            # and the other for imag values. this is consistent with numpy
            x = torch.tensor([2.3 + 4j, 7 + 6j])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([2.3000+4.j, 7.0000+6.j])''')

            # test scientific notation for complex tensors
            x = torch.tensor([1e28 + 2j , -1e-28j])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.0000e+28+2.0000e+00j, -0.0000e+00-1.0000e-28j])''')

            # test big integer
            x = torch.tensor(2341234123412341)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor(2341234123412341)''')

            # test scientific notation
            x = torch.tensor([1e28, 1e-28])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.0000e+28, 1.0000e-28])''')

            # test scientific notation using set_printoptions
            x = torch.tensor([1e2, 1e-2])
            torch.set_printoptions(sci_mode=True)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.0000e+02, 1.0000e-02])''')
            torch.set_printoptions(sci_mode=False)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([  100.0000,     0.0100])''')
            torch.set_printoptions(sci_mode=None)  # reset to the default value

            # test no leading space if all elements positive
            x = torch.tensor([1, 2])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1, 2])''')

            # test for leading space if there are negative elements
            x = torch.tensor([1, -2])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([ 1, -2])''')

            # test inf and nan
            x = torch.tensor([4, inf, 1.5, -inf, 0, nan, 1])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([4.0000,    inf, 1.5000,   -inf, 0.0000,    nan, 1.0000])''')

            y = torch.tensor([4, inf, complex(1.5, inf), complex(-inf, 4), 0, complex(nan, inf), complex(3, nan)])
            self.assertEqual(y.__repr__(), str(y))
            expected_str = '''\
tensor([4.0000+0.j,    inf+0.j, 1.5000+infj,   -inf+4.j, 0.0000+0.j,    nan+infj,
        3.0000+nanj])'''
            self.assertExpectedInline(str(y), expected_str)

            # test dtype
            torch.set_default_dtype(torch.float)
            x = torch.tensor([1e-324, 1e-323, 1e-322, 1e307, 1e308, 1e309], dtype=torch.float64)
            self.assertEqual(x.__repr__(), str(x))
            expected_str = '''\
tensor([ 0.0000e+00, 9.8813e-324, 9.8813e-323, 1.0000e+307, 1.0000e+308,
                inf], dtype=torch.float64)'''
            self.assertExpectedInline(str(x), expected_str)

            # test changing default dtype
            torch.set_default_dtype(torch.float64)
            self.assertEqual(x.__repr__(), str(x))
            expected_str = '''\
tensor([ 0.0000e+00, 9.8813e-324, 9.8813e-323, 1.0000e+307, 1.0000e+308,
                inf])'''
            self.assertExpectedInline(str(x), expected_str)

            # test summary
            x = torch.zeros(10000)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([0., 0., 0.,  ..., 0., 0., 0.])''')

            # test internal summary function
            x = torch.rand(1, 20, 5, 30)
            summary = torch._tensor_str.get_summarized_data(x)
            self.assertEqual(summary.shape, (1, 6, 5, 6))
            first_and_last = [0, 1, 2, -3, -2, -1]
            self.assertEqual(summary, x[:, first_and_last][..., first_and_last])

            # test device
            if torch.cuda.is_available():
                x = torch.tensor([123], device='cuda:0')
                self.assertEqual(x.__repr__(), str(x))
                self.assertExpectedInline(str(x), '''tensor([123], device='cuda:0')''')

                # test changing default to cuda
                torch.set_default_tensor_type(torch.cuda.FloatTensor)
                self.assertEqual(x.__repr__(), str(x))
                self.assertExpectedInline(str(x), '''tensor([123])''')

                # test printing a tensor on a different gpu than current one.
                if torch.cuda.device_count() >= 2:
                    with torch.cuda.device(1):
                        self.assertEqual(x.__repr__(), str(x))
                        self.assertExpectedInline(str(x), '''tensor([123], device='cuda:0')''')

                # test printing cpu tensor when default device is cuda
                y = torch.tensor([123], device='cpu')
                self.assertEqual(y.__repr__(), str(y))
                self.assertExpectedInline(str(y), '''tensor([123], device='cpu')''')
            torch.set_default_tensor_type(default_type)


            # test integral floats and requires_grad
            x = torch.tensor([123.], requires_grad=True)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([123.], requires_grad=True)''')

            # test non-contiguous print
            # sliced tensor should have > PRINT_OPTS.threshold elements
            x = torch.ones(100, 2, 2, 10)
            y = x.as_strided(size=(100, 2, 10), stride=(2 * 2 * 10, 2 * 10, 1))
            self.assertEqual(str(y), y.__repr__())
            expected_str = '''\
tensor([[[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]],

        [[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]],

        [[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]],

        ...,

        [[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]],

        [[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]],

        [[1., 1., 1.,  ..., 1., 1., 1.],
         [1., 1., 1.,  ..., 1., 1., 1.]]])\
'''

            self.assertExpectedInline(str(y), expected_str)

            x = torch.ones(100, 2, 2, 10) * (1 + 1j)
            y = x.as_strided(size=(100, 2, 10), stride=(2 * 2 * 10, 2 * 10, 1))
            self.assertEqual(str(y), y.__repr__())
            expected_str = '''\
tensor([[[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]],

        [[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]],

        [[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]],

        ...,

        [[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]],

        [[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]],

        [[1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j],
         [1.+1.j, 1.+1.j, 1.+1.j,  ..., 1.+1.j, 1.+1.j, 1.+1.j]]])\
'''
            self.assertExpectedInline(str(y), expected_str)

            # test print 0-dim tensor: there's no 0-dim in Numpy, we match arrayprint style
            x = torch.tensor(0.00002)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor(2.0000e-05)''')

            # test print boolean tensor
            x = torch.tensor([True])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([True])''')

            x = torch.tensor(True)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor(True)''')

            # [Numpy] test print float in sci_mode when min < 0.0001.
            x = torch.tensor([0.00002])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([2.0000e-05])''')

            # [Numpy] test print complex in sci_mode when real_min < 0.0001 and (or) imag_min < 0.0001.
            x = torch.tensor([0.00002]) * (1 + 1j)
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([2.0000e-05+2.0000e-05j])''')

            # [Numpy] test print float in sci_mode when max > 1e8.
            # TODO: Pytorch uses fixed precision to print, while Numpy uses dragon4_scientific
            # to do automatic trimming and padding.
            x = torch.tensor([123456789.])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.2346e+08])''')

            # [Numpy] test print float in sci_mode when max / min > 1000.
            x = torch.tensor([0.01, 11])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.0000e-02, 1.1000e+01])''')

            # [Numpy] test print int max / min > 1000, no sci_mode
            x = torch.tensor([1, 1010])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([   1, 1010])''')

            # [Numpy] test print int > 1e8, no sci_mode
            x = torch.tensor([1000000000])  # 1e9
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1000000000])''')

            # [Numpy] test printing float in int_mode
            x = torch.tensor([1., 1000.])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([   1., 1000.])''')

            # [Numpy] test printing float in int_mode in sci format when max / min > 1000.
            x = torch.tensor([1., 1010.])
            self.assertEqual(x.__repr__(), str(x))
            self.assertExpectedInline(str(x), '''tensor([1.0000e+00, 1.0100e+03])''')

        def test_sizeof(self) -> None:
            sizeof_empty = torch.randn(0).storage().__sizeof__()
            sizeof_10 = torch.randn(10).storage().__sizeof__()
            sizeof_100 = torch.randn(100).storage().__sizeof__()
            self.assertEqual((sizeof_100 - sizeof_empty) // (sizeof_10 - sizeof_empty), 10)
            self.assertEqual((sizeof_100 - sizeof_empty) % (sizeof_10 - sizeof_empty), 0)

            sizeof_empty = torch.randn(0).to(torch.uint8).storage().__sizeof__()
            sizeof_10 = torch.randn(10).to(torch.uint8).storage().__sizeof__()
            sizeof_100 = torch.randn(100).to(torch.uint8).storage().__sizeof__()
            self.assertEqual((sizeof_100 - sizeof_empty) // (sizeof_10 - sizeof_empty), 10)
            self.assertEqual((sizeof_100 - sizeof_empty) % (sizeof_10 - sizeof_empty), 0)

        def test_unsqueeze(self) -> None:
            x = torch.randn(2, 3, 4)
            y = x.unsqueeze(1)
            self.assertEqual(y, x.view(2, 1, 3, 4))
            y = x.clone().unsqueeze_(2)
            self.assertEqual(y, x.view(2, 3, 1, 4))

            x = x[:, 1]
            self.assertFalse(x.is_contiguous())
            y = x.unsqueeze(1)
            self.assertEqual(y, x.contiguous().view(2, 1, 4))
            y = x.clone().unsqueeze_(2)
            self.assertEqual(y, x.contiguous().view(2, 4, 1))

        def test_iter(self) -> None:
            x = torch.randn(5, 5)
            for i, sub in enumerate(x):
                self.assertEqual(sub, x[i])

            x = torch.Tensor()
            self.assertEqual(list(x), [])

        def test_accreal_type(self) -> None:
            x = torch.ones(2, 3, 4)
            self.assertIsInstance(x.double().sum().item(), float)
            self.assertIsInstance(x.float().sum().item(), float)
            self.assertIsInstance(x.long().sum().item(), int)
            self.assertIsInstance(x.int().sum().item(), int)
            self.assertIsInstance(x.short().sum().item(), int)
            self.assertIsInstance(x.char().sum().item(), int)
            self.assertIsInstance(x.byte().sum().item(), int)

        def test_assertEqual(self) -> None:
            x = torch.FloatTensor([0])
            self.assertEqual(x, 0)
            xv = torch.autograd.Variable(x)
            self.assertEqual(xv, 0)
            self.assertEqual(x, xv)
            self.assertEqual(xv, x)

            # Tests that setting atol or rtol without the other throws
            self.assertRaises(AssertionError,
                              lambda: self.assertEqual(x, xv, atol=4))
            self.assertRaises(AssertionError,
                              lambda: self.assertEqual(x, xv, rtol=4))

            self.assertRaisesRegex(TypeError, "takes from 3 to 4 positional arguments",
                                   lambda: self.assertEqual(x, xv, "", 1.0))  # type: ignore

        def test_new(self) -> None:
            x = torch.autograd.Variable(torch.Tensor())
            y = torch.autograd.Variable(torch.randn(4, 4))
            z = torch.autograd.Variable(torch.IntTensor([1, 2, 3]))
            self.assertEqual(x.new().shape, [0])
            self.assertEqual(x.new(), x)
            self.assertEqual(x.new(1, 2).shape, [1, 2])
            self.assertEqual(x.new(torch.Size([3, 4])).shape, [3, 4])
            self.assertEqual(x.new([3, 4]).shape, [2])
            self.assertEqual(x.new([3, 4]).tolist(), [3, 4])
            self.assertEqual(x.new((3, 4)).tolist(), [3, 4])
            if TEST_NUMPY:
                self.assertEqual(x.new([np.int32(3), np.float64(4)]).tolist(), [3, 4])
                self.assertEqual(x.new(np.array((3, 4))).tolist(), [3, 4])
            self.assertEqual(x.new([z[2], z[0] + 3]).tolist(), [3, 4])
            self.assertEqual(x.new(size=(3, 4)).shape, [3, 4])
            self.assertEqual(x.new(()).shape, [0])
            self.assertEqual(x.new(y.storage()).data_ptr(), y.data_ptr())
            self.assertEqual(x.new(y).data_ptr(), y.data_ptr())
            self.assertIsNot(x.new(y), y)

            self.assertRaises(TypeError, lambda: x.new(z))
            # TypeError would be better
            self.assertRaises(RuntimeError, lambda: x.new(z.storage()))

        @unittest.skipIf(PYTORCH_CUDA_MEMCHECK, "is_pinned uses failure to detect pointer property")
        def test_pin_memory(self):
            x = torch.randn(3, 5)
            self.assertFalse(x.is_pinned())
            if not torch.cuda.is_available():
                self.assertRaises(RuntimeError, lambda: x.pin_memory())
            else:
                pinned = x.pin_memory()
                self.assertTrue(pinned.is_pinned())
                self.assertEqual(pinned, x)
                self.assertNotEqual(pinned.data_ptr(), x.data_ptr())
                # test that pin_memory on already pinned tensor has no effect
                self.assertIs(pinned, pinned.pin_memory())
                self.assertEqual(pinned.data_ptr(), pinned.pin_memory().data_ptr())

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_numpy_unresizable(self) -> None:
            x = np.zeros((2, 2))
            y = torch.from_numpy(x)
            with self.assertRaises(ValueError):
                x.resize((5, 5))

            z = torch.randn(5, 5)
            w = z.numpy()
            with self.assertRaises(RuntimeError):
                z.resize_(10, 10)
            with self.assertRaises(ValueError):
                w.resize((10, 10))

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_to_numpy(self) -> None:
            def get_castable_tensor(shape, dtype):
                if dtype.is_floating_point:
                    dtype_info = torch.finfo(dtype)
                    # can't directly use min and max, because for double, max - min
                    # is greater than double range and sampling always gives inf.
                    low = max(dtype_info.min, -1e10)
                    high = min(dtype_info.max, 1e10)
                    t = torch.empty(shape, dtype=torch.float64).uniform_(low, high)
                else:
                    # can't directly use min and max, because for int64_t, max - min
                    # is greater than int64_t range and triggers UB.
                    dtype_info = torch.iinfo(dtype)
                    low = max(dtype_info.min, int(-1e10))
                    high = min(dtype_info.max, int(1e10))
                    dtype_info = torch.iinfo(dtype)
                    t = torch.empty(shape, dtype=torch.int64).random_(low, high)
                return t.to(dtype)

            dtypes = [
                torch.uint8,
                torch.int8,
                torch.short,
                torch.int,
                torch.half,
                torch.float,
                torch.double,
                torch.long,
            ]
            for dtp in dtypes:
                # 1D
                sz = 10
                x = get_castable_tensor(sz, dtp)
                y = x.numpy()
                for i in range(sz):
                    self.assertEqual(x[i], y[i])

                # 1D > 0 storage offset
                xm = get_castable_tensor(sz * 2, dtp)
                x = xm.narrow(0, sz - 1, sz)
                self.assertTrue(x.storage_offset() > 0)
                y = x.numpy()
                for i in range(sz):
                    self.assertEqual(x[i], y[i])

                def check2d(x, y):
                    for i in range(sz1):
                        for j in range(sz2):
                            self.assertEqual(x[i][j], y[i][j])

                # empty
                x = torch.Tensor().to(dtp)
                y = x.numpy()
                self.assertEqual(y.size, 0)

                # contiguous 2D
                sz1 = 3
                sz2 = 5
                x = get_castable_tensor((sz1, sz2), dtp)
                y = x.numpy()
                check2d(x, y)
                self.assertTrue(y.flags['C_CONTIGUOUS'])

                # with storage offset
                xm = get_castable_tensor((sz1 * 2, sz2), dtp)
                x = xm.narrow(0, sz1 - 1, sz1)
                y = x.numpy()
                self.assertTrue(x.storage_offset() > 0)
                check2d(x, y)
                self.assertTrue(y.flags['C_CONTIGUOUS'])

                # non-contiguous 2D
                x = get_castable_tensor((sz2, sz1), dtp).t()
                y = x.numpy()
                check2d(x, y)
                self.assertFalse(y.flags['C_CONTIGUOUS'])

                # with storage offset
                xm = get_castable_tensor((sz2 * 2, sz1), dtp)
                x = xm.narrow(0, sz2 - 1, sz2).t()
                y = x.numpy()
                self.assertTrue(x.storage_offset() > 0)
                check2d(x, y)

                # non-contiguous 2D with holes
                xm = get_castable_tensor((sz2 * 2, sz1 * 2), dtp)
                x = xm.narrow(0, sz2 - 1, sz2).narrow(1, sz1 - 1, sz1).t()
                y = x.numpy()
                self.assertTrue(x.storage_offset() > 0)
                check2d(x, y)

                if dtp != torch.half:
                    # check writeable
                    x = get_castable_tensor((3, 4), dtp)
                    y = x.numpy()
                    self.assertTrue(y.flags.writeable)
                    y[0][1] = 3
                    self.assertTrue(x[0][1] == 3)
                    y = x.t().numpy()
                    self.assertTrue(y.flags.writeable)
                    y[0][1] = 3
                    self.assertTrue(x[0][1] == 3)

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_to_numpy_bool(self) -> None:
            x = torch.tensor([True, False], dtype=torch.bool)
            self.assertEqual(x.dtype, torch.bool)

            y = x.numpy()
            self.assertEqual(y.dtype, np.bool)
            for i in range(len(x)):
                self.assertEqual(x[i], y[i])

            x = torch.tensor([True], dtype=torch.bool)
            self.assertEqual(x.dtype, torch.bool)

            y = x.numpy()
            self.assertEqual(y.dtype, np.bool)
            self.assertEqual(x[0], y[0])

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_from_numpy(self) -> None:
            dtypes = [
                np.double,
                np.float,
                np.float16,
                np.complex64,
                np.complex128,
                np.int64,
                np.int32,
                np.int16,
                np.int8,
                np.uint8,
                np.longlong,
                np.bool,
            ]
            complex_dtypes = [
                np.complex64,
                np.complex128,
            ]

            for dtype in dtypes:
                array = np.array([1, 2, 3, 4], dtype=dtype)
                tensor_from_array = torch.from_numpy(array)
                # TODO: change to tensor equality check once HalfTensor
                # implements `==`
                for i in range(len(array)):
                    self.assertEqual(tensor_from_array[i], array[i])
                # ufunc 'remainder' not supported for complex dtypes
                if dtype not in complex_dtypes:
                    # This is a special test case for Windows
                    # https://github.com/pytorch/pytorch/issues/22615
                    array2 = array % 2
                    tensor_from_array2 = torch.from_numpy(array2)
                    for i in range(len(array2)):
                        self.assertEqual(tensor_from_array2[i], array2[i])

            # Test unsupported type
            array = np.array([1, 2, 3, 4], dtype=np.uint16)
            with self.assertRaises(TypeError):
                tensor_from_array = torch.from_numpy(array)

            # check storage offset
            x = np.linspace(1, 125, 125)
            x.shape = (5, 5, 5)
            x = x[1]
            expected = torch.arange(1, 126, dtype=torch.float64).view(5, 5, 5)[1]
            self.assertEqual(torch.from_numpy(x), expected)

            # check noncontiguous
            x = np.linspace(1, 25, 25)
            x.shape = (5, 5)
            expected = torch.arange(1, 26, dtype=torch.float64).view(5, 5).t()
            self.assertEqual(torch.from_numpy(x.T), expected)

            # check noncontiguous with holes
            x = np.linspace(1, 125, 125)
            x.shape = (5, 5, 5)
            x = x[:, 1]
            expected = torch.arange(1, 126, dtype=torch.float64).view(5, 5, 5)[:, 1]
            self.assertEqual(torch.from_numpy(x), expected)

            # check zero dimensional
            x = np.zeros((0, 2))
            self.assertEqual(torch.from_numpy(x).shape, (0, 2))
            x = np.zeros((2, 0))
            self.assertEqual(torch.from_numpy(x).shape, (2, 0))

            # check ill-sized strides raise exception
            x = np.array([3., 5., 8.])
            x.strides = (3,)
            self.assertRaises(ValueError, lambda: torch.from_numpy(x))

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_ctor_with_numpy_scalar_ctor(self) -> None:
            dtypes = [
                np.double,
                np.float,
                np.float16,
                np.int64,
                np.int32,
                np.int16,
                np.uint8,
                np.bool,
            ]
            for dtype in dtypes:
                self.assertEqual(dtype(42), torch.tensor(dtype(42)).item())

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_numpy_index(self):
            i = np.int32([0, 1, 2])
            x = torch.randn(5, 5)
            for idx in i:
                self.assertFalse(isinstance(idx, int))
                self.assertEqual(x[idx], x[int(idx)])

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_numpy_array_interface(self):
            types = [
                torch.DoubleTensor,
                torch.FloatTensor,
                torch.HalfTensor,
                torch.LongTensor,
                torch.IntTensor,
                torch.ShortTensor,
                torch.ByteTensor,
            ]
            dtypes = [
                np.float64,
                np.float32,
                np.float16,
                np.int64,
                np.int32,
                np.int16,
                np.uint8,
            ]
            for tp, dtype in zip(types, dtypes):
                if np.dtype(dtype).kind == 'u':
                    x = torch.Tensor([1, 2, 3, 4]).type(tp)
                    array = np.array([1, 2, 3, 4], dtype=dtype)
                else:
                    x = torch.Tensor([1, -2, 3, -4]).type(tp)
                    array = np.array([1, -2, 3, -4], dtype=dtype)

                # Test __array__ w/o dtype argument
                asarray = np.asarray(x)
                self.assertIsInstance(asarray, np.ndarray)
                self.assertEqual(asarray.dtype, dtype)
                for i in range(len(x)):
                    self.assertEqual(asarray[i], x[i])

                # Test __array_wrap__, same dtype
                abs_x = np.abs(x)
                abs_array = np.abs(array)
                self.assertIsInstance(abs_x, tp)
                for i in range(len(x)):
                    self.assertEqual(abs_x[i], abs_array[i])

            # Test __array__ with dtype argument
            for dtype in dtypes:
                x = torch.IntTensor([1, -2, 3, -4])
                asarray = np.asarray(x, dtype=dtype)
                self.assertEqual(asarray.dtype, dtype)
                if np.dtype(dtype).kind == 'u':
                    wrapped_x = np.array([1, -2, 3, -4], dtype=dtype)
                    for i in range(len(x)):
                        self.assertEqual(asarray[i], wrapped_x[i])
                else:
                    for i in range(len(x)):
                        self.assertEqual(asarray[i], x[i])

            # Test some math functions with float types
            float_types = [torch.DoubleTensor, torch.FloatTensor]
            float_dtypes = [np.float64, np.float32]
            for tp, dtype in zip(float_types, float_dtypes):
                x = torch.Tensor([1, 2, 3, 4]).type(tp)
                array = np.array([1, 2, 3, 4], dtype=dtype)
                for func in ['sin', 'sqrt', 'ceil']:
                    ufunc = getattr(np, func)
                    res_x = ufunc(x)
                    res_array = ufunc(array)
                    self.assertIsInstance(res_x, tp)
                    for i in range(len(x)):
                        self.assertEqual(res_x[i], res_array[i])

            # Test functions with boolean return value
            for tp, dtype in zip(types, dtypes):
                x = torch.Tensor([1, 2, 3, 4]).type(tp)
                array = np.array([1, 2, 3, 4], dtype=dtype)
                geq2_x = np.greater_equal(x, 2)
                geq2_array = np.greater_equal(array, 2).astype('uint8')
                self.assertIsInstance(geq2_x, torch.ByteTensor)
                for i in range(len(x)):
                    self.assertEqual(geq2_x[i], geq2_array[i])

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_multiplication_numpy_scalar(self) -> None:
            for np_dtype in [np.float32, np.float64, np.int32, np.int64, np.int16, np.uint8]:
                for t_dtype in [torch.float, torch.double]:
                    np_sc = np_dtype(2.0)
                    t = torch.ones(2, requires_grad=True, dtype=t_dtype)
                    r1 = t * np_sc
                    self.assertIsInstance(r1, torch.Tensor)
                    self.assertTrue(r1.dtype == t_dtype)
                    self.assertTrue(r1.requires_grad)
                    r2 = np_sc * t
                    self.assertIsInstance(r2, torch.Tensor)
                    self.assertTrue(r2.dtype == t_dtype)
                    self.assertTrue(r2.requires_grad)

        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_parse_numpy_int(self):
            self.assertRaisesRegex(RuntimeError, "Overflow",
                                   lambda: torch.mean(torch.randn(1, 1), np.uint64(-1)))
            # https://github.com/pytorch/pytorch/issues/29252
            for nptype in [np.int16, np.int8, np.uint8, np.int32, np.int64]:
                scalar = 3
                np_arr = np.array([scalar], dtype=nptype)
                np_val = np_arr[0]

                # np integral type can be treated as a python int in native functions with
                # int parameters:
                self.assertEqual(torch.ones(5).diag(scalar), torch.ones(5).diag(np_val))
                self.assertEqual(torch.ones([2, 2, 2, 2]).mean(scalar), torch.ones([2, 2, 2, 2]).mean(np_val))

                # numpy integral type parses like a python int in custom python bindings:
                self.assertEqual(torch.Storage(np_val).size(), scalar)

                tensor = torch.tensor([2], dtype=torch.int)
                tensor[0] = np_val
                self.assertEqual(tensor[0], np_val)

                # Original reported issue, np integral type parses to the correct
                # PyTorch integral type when passed for a `Scalar` parameter in
                # arithmetic operations:
                t = torch.from_numpy(np_arr)
                self.assertEqual((t + np_val).dtype, t.dtype)
                self.assertEqual((np_val + t).dtype, t.dtype)

        def test_error_msg_type_translation(self):
            with self.assertRaisesRegex(
                    RuntimeError,
                    # message includes both Double and Long
                    '(?=.*Double)(?=.*Long)'):

                # Calls model with a LongTensor input but DoubleTensor weights
                input = torch.zeros(1, 1, 1, 6, dtype=torch.long)
                weight = torch.nn.Parameter(torch.zeros(1, 1, 1, 3, dtype=torch.double))
                model = torch.nn.Conv2d(1, 1, (1, 3), stride=1, padding=0, bias=False)
                model.weight = weight
                out = model(input)

        def test_tensor_from_sequence(self):
            class MockSequence(object):
                def __init__(self, lst):
                    self.lst = lst

                def __len__(self):
                    return len(self.lst)

                def __getitem__(self, item):
                    raise TypeError

            class GoodMockSequence(MockSequence):
                def __getitem__(self, item):
                    return self.lst[item]

            bad_mock_seq = MockSequence([1.0, 2.0, 3.0])
            good_mock_seq = GoodMockSequence([1.0, 2.0, 3.0])
            with self.assertRaisesRegex(ValueError, 'could not determine the shape'):
                torch.Tensor(bad_mock_seq)
            self.assertEqual(torch.Tensor([1.0, 2.0, 3.0]), torch.Tensor(good_mock_seq))

        def test_comparison_ops(self):
            x = torch.randn(5, 5)
            y = torch.randn(5, 5)

            eq = x == y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] == y[idx], eq[idx] == 1)

            ne = x != y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] != y[idx], ne[idx] == 1)

            lt = x < y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] < y[idx], lt[idx] == 1)

            le = x <= y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] <= y[idx], le[idx] == 1)

            gt = x > y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] > y[idx], gt[idx] == 1)

            ge = x >= y
            for idx in iter_indices(x):
                self.assertEqual(x[idx] >= y[idx], ge[idx] == 1)

        def test_comparison_ops_must_take_bool_output(self):
            for op in [torch.lt, torch.le, torch.gt, torch.ge, torch.eq, torch.ne,
                       torch.logical_and, torch.logical_or, torch.logical_xor]:
                self.assertEqual(op(torch.tensor([True]), torch.tensor([False])).dtype, torch.bool)

        def test_inplace_comparison_ops_require_inputs_have_same_dtype(self):
            with self.assertRaisesRegex(RuntimeError, 'Expected object of scalar type'):
                for op in ['lt_', 'le_', 'gt_', 'ge_', 'eq_', 'ne_', 'logical_xor_', 'logical_and_', 'logical_or_']:
                    x = torch.tensor([1], dtype=torch.int)
                    y = torch.tensor([2], dtype=torch.long)
                    in_place_method = getattr(x, op)
                    in_place_method(y)

        def test_comparison_ops_check_for_scalar_overflow(self):
            with self.assertRaisesRegex(RuntimeError, 'value cannot be converted to type'):
                torch.tensor([1 << 5], dtype=torch.uint8) < (1 << 20)
                (1 << 20) < torch.tensor([1 << 5], dtype=torch.uint8)
                torch.tensor([1 << 5], dtype=torch.uint8) <= (1 << 20)
                (1 << 20) <= torch.tensor([1 << 5], dtype=torch.uint8)
                torch.tensor([1 << 5], dtype=torch.uint8) > (1 << 20)
                (1 << 20) > torch.tensor([1 << 5], dtype=torch.uint8)
                torch.tensor([1 << 5], dtype=torch.uint8) >= (1 << 20)
                (1 << 20) >= torch.tensor([1 << 5], dtype=torch.uint8)
                torch.tensor([1 << 5], dtype=torch.uint8) == (1 << 20)
                (1 << 20) == torch.tensor([1 << 5], dtype=torch.uint8)
                torch.tensor([1 << 5], dtype=torch.uint8) != (1 << 20)
                (1 << 20) != torch.tensor([1 << 5], dtype=torch.uint8)

        def test_comparison_ops_check_for_zerodim_tensor_overflow(self):
            with self.assertRaisesRegex(RuntimeError, 'value cannot be converted to type'):
                torch.tensor([1 << 5], dtype=torch.uint8) < torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) < torch.tensor([1 << 30], dtype=torch.int32)
                torch.tensor([1 << 5], dtype=torch.uint8) <= torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) <= torch.tensor([1 << 30], dtype=torch.int32)
                torch.tensor([1 << 5], dtype=torch.uint8) > torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) > torch.tensor([1 << 30], dtype=torch.int32)
                torch.tensor([1 << 5], dtype=torch.uint8) >= torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) >= torch.tensor([1 << 30], dtype=torch.int32)
                torch.tensor([1 << 5], dtype=torch.uint8) == torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) == torch.tensor([1 << 30], dtype=torch.int32)
                torch.tensor([1 << 5], dtype=torch.uint8) != torch.tensor(1 << 20, dtype=torch.int32)
                torch.tensor(1 << 40, dtype=torch.int64) != torch.tensor([1 << 30], dtype=torch.int32)

        def test_bitwise_ops(self):
            x = torch.randn(5, 5).gt(0)
            y = torch.randn(5, 5).gt(0)

            and_result = x & y
            for idx in iter_indices(x):
                if and_result[idx]:
                    self.assertTrue(x[idx] and y[idx])
                else:
                    self.assertFalse(x[idx] and y[idx])

            or_result = x | y
            for idx in iter_indices(x):
                if or_result[idx]:
                    self.assertTrue(x[idx] or y[idx])
                else:
                    self.assertFalse(x[idx] or y[idx])

            xor_result = x ^ y
            for idx in iter_indices(x):
                if xor_result[idx]:
                    self.assertTrue(x[idx] ^ y[idx])
                else:
                    self.assertFalse(x[idx] ^ y[idx])

            x_clone = x.clone()
            x_clone &= y
            self.assertEqual(x_clone, and_result)

            x_clone = x.clone()
            x_clone |= y
            self.assertEqual(x_clone, or_result)

            x_clone = x.clone()
            x_clone ^= y
            self.assertEqual(x_clone, xor_result)

        def test_op_invert(self):
            res = 0xffff - torch.arange(127, dtype=torch.int8)
            for dtype in (torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64):
                a = torch.arange(127, dtype=dtype)
                self.assertEqual(res.to(dtype), ~a)

            self.assertEqual(torch.tensor([True, False]),
                             ~torch.tensor([False, True]))

            # test exceptions
            for dtype in (torch.half, torch.float, torch.double):
                a = torch.zeros(10, dtype=dtype)
                with self.assertRaises(TypeError):
                    b = ~a

        def test_apply(self):
            x = torch.arange(1, 6)
            res = x.clone().apply_(lambda k: k + k)
            self.assertEqual(res, x * 2)
            self.assertRaises(TypeError, lambda: x.apply_(lambda k: "str"))

        def test_map(self):
            x = torch.autograd.Variable(torch.randn(3, 3))
            y = torch.autograd.Variable(torch.randn(3))
            res = x.clone()
            res.map_(y, lambda a, b: a + b)
            self.assertEqual(res, x + y)
            self.assertRaisesRegex(TypeError, "not callable", lambda: res.map_(y, "str"))

        def test_map2(self):
            x = torch.autograd.Variable(torch.randn(3, 3))
            y = torch.autograd.Variable(torch.randn(3))
            z = torch.autograd.Variable(torch.randn(1, 3))
            res = x.clone()
            res.map2_(y, z, lambda a, b, c: a + b * c)
            self.assertEqual(res, x + y * z)
            z.requires_grad = True
            self.assertRaisesRegex(
                RuntimeError, "requires grad",
                lambda: res.map2_(y, z, lambda a, b, c: a + b * c))

        def test_Size(self):
            x = torch.Size([1, 2, 3])
            self.assertIsInstance(x, tuple)
            self.assertEqual(x[0], 1)
            self.assertEqual(x[1], 2)
            self.assertEqual(x[2], 3)
            self.assertEqual(len(x), 3)
            self.assertRaises(TypeError, lambda: torch.Size(torch.ones(3)))

            self.assertIsInstance(x * 2, torch.Size)
            self.assertIsInstance(x[:-1], torch.Size)
            self.assertIsInstance(x + x, torch.Size)

        def test_Size_scalar(self):
            three = torch.tensor(3)
            two = torch.tensor(2)
            x = torch.Size([0, 1, two, three, 4])
            for i in range(1, 5):
                self.assertEqual(x[i], i)

        def test_Size_iter(self):
            for sizes in [iter([1, 2, 3, 4, 5]), range(1, 6)]:
                x = torch.Size(sizes)
                for i in range(0, 5):
                    self.assertEqual(x[i], i + 1)

        def test_t_not_2d_error(self):
            self.assertRaises(RuntimeError, lambda: torch.randn(2, 3, 4).t())
            self.assertRaises(RuntimeError, lambda: torch.randn(2, 3, 4).t_())

        # unit test for special case transposed copy (see ATen/native/Copy.cpp for details)
        @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
        def test_big_transpose(self):
            t = torch.rand(456, 789)
            t1 = t.t().contiguous()
            t2 = torch.from_numpy(t.numpy().transpose())
            self.assertEqual(t1, t2)

        def test_inplace_division(self):
            t = torch.rand(5, 5)
            id_before = id(t)
            t /= 2
            id_after = id(t)
            self.assertEqual(id_before, id_after)

        def test_simple_scalar_cast(self):
            ok = [torch.Tensor([1.5]), torch.zeros(1, 1, 1, 1)]
            ok_values = [1.5, 0]

            not_ok = map(torch.Tensor, [[], [1, 2], [[1, 2], [3, 4]]])

            for tensor, value in zip(ok, ok_values):
                self.assertEqual(int(tensor), int(value))
                self.assertEqual(float(tensor), float(value))
                self.assertEqual(complex(tensor), complex(value))

            self.assertEqual(complex(torch.tensor(1.5j)), 1.5j)

            for tensor in not_ok:
                self.assertRaises(ValueError, lambda: int(tensor))
                self.assertRaises(ValueError, lambda: float(tensor))
                self.assertRaises(ValueError, lambda: complex(tensor))

            self.assertRaises(RuntimeError, lambda: float(torch.tensor(1.5j)))
            self.assertRaises(RuntimeError, lambda: int(torch.tensor(1.5j)))

        def test_offset_scalar_cast(self):
            x = torch.Tensor([1, 2, 3])
            y = x[2:]
            self.assertEqual(int(y), 3)

        # skip this test for now as it affects all tests
        @unittest.skipIf(True, "flush_denormal not supported")
        def test_set_flush_denormal(self):
            tiny_float = 1e-42
            tiny_double = 1e-320
            float_tensor = torch.FloatTensor([1.0, tiny_float])
            double_tensor = torch.DoubleTensor([1.0, tiny_float, tiny_double])

            self.assertEqual(float_tensor[0], 1.0, atol=0.0, rtol=0)
            self.assertEqual(float_tensor[1], tiny_float, atol=tiny_float / 16, rtol=0)
            self.assertEqual(double_tensor[0], 1.0, atol=0.0, rtol=0)
            self.assertEqual(double_tensor[1], tiny_float, atol=0.0, rtol=0)
            self.assertEqual(double_tensor[2], tiny_double, atol=0.0, rtol=0)

            torch.set_flush_denormal(True)
            self.assertEqual(float_tensor[0], 1.0, atol=0.0, rtol=0)
            self.assertEqual(float_tensor[1], 0.0, atol=0.0, rtol=0)  # tiny_float to zero
            self.assertEqual(double_tensor[0], 1.0, atol=0.0, rtol=0)
            # tiny_float is not converted to zero in double type
            self.assertEqual(double_tensor[1], tiny_float, atol=0.0, rtol=0)
            self.assertEqual(double_tensor[2], 0.0, atol=0.0, rtol=0)  # tiny_double to zero
            torch.set_flush_denormal(False)

        def test_show_config(self):
            # We can't usefully test the output; just make sure this doesn't crash
            torch.__config__.show()

        def test_parallel_info(self):
            torch.__config__.parallel_info()

        @slowTest
        def test_slow_test(self):
            # Just a smoketest to make sure our slowTest decorator works.
            pass

        def test_is_nonzero(self):
            self.assertExpectedRaisesInline(
                RuntimeError,
                lambda: torch.tensor([]).is_nonzero(),
                "Boolean value of Tensor with no values is ambiguous",
            )
            self.assertExpectedRaisesInline(
                RuntimeError,
                lambda: torch.tensor([0, 0]).is_nonzero(),
                "Boolean value of Tensor with more than one value is ambiguous",
            )
            self.assertFalse(torch.tensor(0).is_nonzero())
            self.assertTrue(torch.tensor(1).is_nonzero())
            self.assertFalse(torch.tensor([0]).is_nonzero())
            self.assertTrue(torch.tensor([1]).is_nonzero())
            self.assertFalse(torch.tensor([[0]]).is_nonzero())
            self.assertTrue(torch.tensor([[1]]).is_nonzero())

        def test_meshgrid(self):
            a = torch.tensor(1)
            b = torch.tensor([1, 2, 3])
            c = torch.tensor([1, 2])
            grid_a, grid_b, grid_c = torch.meshgrid([a, b, c])
            self.assertEqual(grid_a.shape, torch.Size([1, 3, 2]))
            self.assertEqual(grid_b.shape, torch.Size([1, 3, 2]))
            self.assertEqual(grid_c.shape, torch.Size([1, 3, 2]))
            grid_a2, grid_b2, grid_c2 = torch.meshgrid(a, b, c)
            self.assertEqual(grid_a2.shape, torch.Size([1, 3, 2]))
            self.assertEqual(grid_b2.shape, torch.Size([1, 3, 2]))
            self.assertEqual(grid_c2.shape, torch.Size([1, 3, 2]))
            expected_grid_a = torch.ones(1, 3, 2, dtype=torch.int64)
            expected_grid_b = torch.tensor([[[1, 1],
                                             [2, 2],
                                             [3, 3]]])
            expected_grid_c = torch.tensor([[[1, 2],
                                             [1, 2],
                                             [1, 2]]])
            self.assertTrue(grid_a.equal(expected_grid_a))
            self.assertTrue(grid_b.equal(expected_grid_b))
            self.assertTrue(grid_c.equal(expected_grid_c))
            self.assertTrue(grid_a2.equal(expected_grid_a))
            self.assertTrue(grid_b2.equal(expected_grid_b))
            self.assertTrue(grid_c2.equal(expected_grid_c))

        # NB: we must not be built with CUDA; if we are built with CUDA but no CUDA
        # is available, we get a different error.
        @unittest.skipIf(torch.backends.cuda.is_built() or IS_SANDCASTLE, "CUDA is built, can't test CUDA not built error")
        def test_cuda_not_built(self):
            msg = "Torch not compiled with CUDA enabled"
            self.assertRaisesRegex(AssertionError, msg, lambda: torch.cuda.current_device())
            self.assertRaisesRegex(AssertionError, msg, lambda: torch.tensor([1], device="cuda"))
            self.assertRaisesRegex(AssertionError, msg, lambda: torch.tensor([1]).cuda())
            self.assertRaisesRegex(TypeError, msg, lambda: torch.cuda.FloatTensor())
            self.assertRaisesRegex(TypeError, msg, lambda: torch.set_default_tensor_type(torch.cuda.FloatTensor))
            self.assertRaisesRegex(AssertionError, msg, lambda: torch.tensor([1]).to(device="cuda"))

        def test_cast_binary_op(self):
            # Scalar
            a = torch.tensor(2)
            b = torch.tensor(3)
            a_copy = a.clone()
            b_copy = b.clone()

            self.assertEqual(torch.tensor(6, dtype=torch.float), a.float() * b)

            self.assertEqualTypeString(a, a_copy)
            self.assertEqualTypeString(b, b_copy)

        def test_cartesian_prod(self):
            a = torch.tensor([1])
            b = torch.tensor([1, 2, 3])
            c = torch.tensor([1, 2])
            prod = torch.cartesian_prod(a, b, c)
            expected = torch.tensor(list(product([a], b, c)))
            self.assertEqual(expected, prod)

            # test 0 size input
            d = torch.empty(0, dtype=b.dtype)
            prod = torch.cartesian_prod(a, b, c, d)
            expected = torch.empty(0, 4, dtype=b.dtype)
            self.assertEqual(expected, prod)

            # test single input
            prod = torch.cartesian_prod(b)
            self.assertEqual(b, prod)

        def test_combinations(self):
            a = torch.tensor([1, 2, 3])

            c = torch.combinations(a, r=1)
            expected = torch.tensor(list(combinations(a, r=1)))
            self.assertEqual(c, expected)

            c = torch.combinations(a, r=1, with_replacement=True)
            expected = torch.tensor(list(combinations_with_replacement(a, r=1)))
            self.assertEqual(c, expected)

            c = torch.combinations(a)
            expected = torch.tensor(list(combinations(a, r=2)))
            self.assertEqual(c, expected)

            c = torch.combinations(a, with_replacement=True)
            expected = torch.tensor(list(combinations_with_replacement(a, r=2)))
            self.assertEqual(c, expected)

            c = torch.combinations(a, r=3)
            expected = torch.tensor(list(combinations(a, r=3)))
            self.assertEqual(c, expected)

            c = torch.combinations(a, r=4)
            expected = torch.empty(0, 4, dtype=a.dtype)
            self.assertEqual(c, expected)

            c = torch.combinations(a, r=5)
            expected = torch.empty(0, 5, dtype=a.dtype)
            self.assertEqual(c, expected)

            # test empty imput
            a = torch.empty(0)
            c1 = torch.combinations(a)
            c2 = torch.combinations(a, with_replacement=True)
            expected = torch.empty(0, 2, dtype=a.dtype)
            self.assertEqual(c1, expected)
            self.assertEqual(c2, expected)

        def test_has_internal_overlap(self):
            OVERLAP_NO = 0
            OVERLAP_YES = 1
            OVERLAP_TOO_HARD = 2

            # Check for contiguous tensors
            a = torch.randn(3, 3)
            self.assertEqual(torch._debug_has_internal_overlap(a), OVERLAP_NO)

            # Checks for zero strides
            b = torch.randn(1, 3)
            b_expanded = b.expand(4, 3)
            self.assertEqual(torch._debug_has_internal_overlap(b_expanded), OVERLAP_YES)

            # Check for zero strided, size 1 axis, in non-contiguous storage (gh-33812)
            c = torch.randn(10).as_strided([2, 1, 5], [1, 0, 2])
            self.assertEqual(torch._debug_has_internal_overlap(c), OVERLAP_TOO_HARD)

        def test_allow_tensor_metadata_change(self):
            def do_test(t):
                with self.assertRaisesRegex(
                        RuntimeError,
                        "set_sizes_contiguous is not allowed on a Tensor created from .data or .detach()"):
                    t.resize_((2, 1))
                with self.assertRaisesRegex(
                        RuntimeError,
                        "set_storage is not allowed on a Tensor created from .data or .detach()"):
                    t.set_()
                with self.assertRaisesRegex(
                        RuntimeError,
                        "set_storage_offset is not allowed on a Tensor created from .data or .detach()"):
                    t.set_(t.storage(), 0, t.size(), list(t.stride()))

            do_test(torch.tensor([[1, 2]]).data)
            do_test(torch.tensor([[1, 2]]).detach())

        def test_c10_layer_norm(self):
            # test that we can call c10 ops and they return a reasonable result
            X = torch.rand(5, 5, dtype=torch.float)
            weight = torch.rand(*X.size()[1:], dtype=torch.float)
            bias = torch.rand(*X.size()[1:], dtype=torch.float)
            epsilon = 1e-4

            expected_norm = torch.nn.functional.layer_norm(
                X, X.size()[1:], weight=weight, bias=bias, eps=epsilon)
            actual_norm, actual_mean, actual_stdev = \
                torch.ops._caffe2.LayerNorm(torch.tensor(X), torch.tensor(
                    weight), torch.tensor(bias), 1, epsilon, True)
            torch.testing.assert_allclose(expected_norm, actual_norm)

        def test_memory_format(self):
            def test_helper(x, memory_format):
                y = x.contiguous(memory_format=memory_format)
                self.assertFalse(y.is_contiguous())
                self.assertTrue(y.is_contiguous(memory_format=memory_format))
                self.assertEqual(y, x)

            test_helper(torch.randn(4, 3, 8, 8), torch.channels_last)
            test_helper(torch.randn(4, 3, 8, 8, 8), torch.channels_last_3d)

        def test_memory_format_contiguous_returns_same_tensor_if_already_satisfies(self):
            def test_helper(x, memory_format):
                alias = x.contiguous(memory_format=memory_format)
                alias.fill_(7)
                self.assertEqual(x, alias)

            test_helper(torch.randn(4, 8, 8, 3).permute(0, 3, 1, 2), torch.channels_last)
            test_helper(torch.randn(4, 8, 8, 8, 3).permute(0, 4, 1, 2, 3), torch.channels_last_3d)

        def test_memory_format_empty(self):
            def test_helper(dim1, dim2, memory_format):
                with self.assertRaises(RuntimeError):
                    x = torch.empty(dim1, memory_format=memory_format)
                x = torch.empty(dim2, memory_format=memory_format)
                self.assertTrue(x.is_contiguous(memory_format=memory_format))

            test_helper((3, 3), (3, 3, 3, 3), torch.channels_last)
            test_helper((3, 3, 3), (3, 3, 3, 3, 3), torch.channels_last_3d)

        def test_subclass_tensors(self):
            # raise an error when trying to subclass FloatTensor
            with self.assertRaisesRegex(TypeError, "type 'torch.FloatTensor' is not an acceptable base type"):
                class Foo1(torch.FloatTensor):
                    pass

            # but allow subclassing Tensor:
            class Foo2(torch.Tensor):
                def foo(self):
                    return 5
            f = Foo2()
            self.assertEqual(f.foo(), 5)

        def test_ndim(self):
            a = torch.randn(1, 2, 3)
            self.assertEqual(3, a.ndim)
            b = torch.randn(())
            self.assertEqual(0, b.ndim)
            c = torch.randn(1, 0)
            self.assertEqual(2, c.ndim)

        def test_T(self):
            a = torch.randn(2, 3, 4)
            t1 = a.T
            t2 = a.permute(2, 1, 0)
            self.assertEqual(t2, t1)
            b = torch.randn(10)
            self.assertEqual(b, b.T)
            scalar = torch.tensor(5)
            self.assertEqual(scalar, scalar.T)

        def test_python_types(self):
            a1 = torch.randn((1, 2), dtype=torch.float64)
            a2 = torch.randn((1, 2), dtype=float)
            self.assertEqual(a1.dtype, a2.dtype)

            b1 = torch.arange(10, 20, dtype=torch.int64)
            b2 = torch.arange(10, 20, dtype=int)
            self.assertEqual(b1.dtype, b2.dtype)

            c1 = torch.tensor([True, False], dtype=torch.bool)
            c2 = torch.tensor([True, False], dtype=bool)
            self.assertEqual(c1.dtype, c2.dtype)

        def test_fill_diagonal(self):
            a1 = torch.randn(7, 3)
            a2 = a1.clone()
            v = 1
            for i in range(3):
                a2[i][i] = v
            a1.fill_diagonal_(v)
            self.assertEqual(a1, a2)

            b1 = torch.randn(7, 3)
            b2 = b1.clone()
            for i in range(3):
                b2[i][i] = v
                b2[i + 4][i] = v
            b1.fill_diagonal_(v, wrap=True)
            self.assertEqual(b1, b2)

            c1 = torch.rand(3, 3, 3)
            c2 = c1.clone()
            for i in range(3):
                c2[i][i][i] = v
            c1.fill_diagonal_(v)
            self.assertEqual(c1, c2)

            # non-contiguous tensor
            d1 = torch.rand(3, 3, 3)[:, 1, ...]
            d2 = d1.clone()
            for i in range(3):
                d2[i][i] = v
            d1.fill_diagonal_(v)
            self.assertEqual(d1, d2)

            e1 = torch.rand(7, 3, 3)[:, 1, ...]
            e2 = e1.clone()
            for i in range(3):
                e2[i][i] = v
                e2[i + 4][i] = v
            e1.fill_diagonal_(v, wrap=True)
            self.assertEqual(e1, e2)

        def test_batch_norm_cpu_inference(self):
            # input nchw in (2,1,1,1), (2,2,2,2)
            inputs = [
                torch.tensor([[[[-0.5000]]], [[[0.5000]]]]),
                torch.tensor([
                    [
                        [[-0.5000, 0.5000], [-1.0000, 1.0000]],
                        [[-0.2500, -0.5000], [0.2500, 0.5000]]
                    ],
                    [
                        [[0.1000, 1.0000], [1.0000, 0.1000]],
                        [[1.0000, 0.5000], [1.5000, -1.5000]]
                    ]])]
            # output nchw in (2,1,1,1), (2,2,2,2)
            outputs = [
                torch.tensor([
                    [[[-0.499997496604919433593750000]]],
                    [[[0.499997496604919433593750000]]]]),
                torch.tensor([
                    [[[-0.499997496604919433593750000, 0.499997496604919433593750000],
                      [-0.999994993209838867187500000, 0.999994993209838867187500000]],
                     [[-0.249998748302459716796875000, -0.499997496604919433593750000],
                      [0.249998748302459716796875000, 0.499997496604919433593750000]]],
                    [[[0.099999502301216125488281250, 0.999994993209838867187500000],
                      [0.999994993209838867187500000, 0.099999502301216125488281250]],
                     [[0.999994993209838867187500000, 0.499997496604919433593750000],
                      [1.499992489814758300781250000, -1.499992489814758300781250000]]]])]


            for i in range(len(inputs)):
                for affine in [False, True]:
                    m = torch.nn.BatchNorm2d(inputs[i].size()[1], 1e-05, 0.1, affine=affine)
                    m.eval()
                    # contiguous case
                    input1 = inputs[i].contiguous()
                    output1 = m(input1)
                    # non-contiguous case
                    input2 = input1.permute(0, 1, 3, 2)
                    output2 = m(input2).permute(0, 1, 3, 2)
                    # channels last case
                    input3 = input1.contiguous(memory_format=torch.channels_last)
                    output3 = m(input3)
                    self.assertEqual(output3, outputs[i])
                    self.assertEqual(output3, output1)
                    self.assertEqual(output3, output2)

        def test_empty_meta(self):
            x = torch.empty_meta(2 ** 20, 2 ** 20)
            y = torch.empty_meta(2 ** 20)
            z = x + y
            self.assertEqual(z.size(), (2 ** 20, 2 ** 20))

        def test_tensor_grad_warnings(self):
            dummy = torch.empty(1)

            with warnings.catch_warnings(record=True) as w:
                # Accessing .grad on leaf
                dummy.requires_grad_()
                foo = dummy.grad
                self.assertEqual(len(w), 0)

                # Accessing .grad on non-leaf
                dummy = dummy.clone()
                foo = dummy.grad
                self.assertEqual(len(w), 1)

                # Accessing .grad on non-leaf that retains gradients
                dummy.retain_grad()
                foo = dummy.grad
                self.assertEqual(len(w), 1)

        def test_normal_shape(self):
            warned = False
            for device in torch.testing.get_all_device_types():
                tensor1 = torch.rand(1, device=device)
                tensor4 = torch.rand(4, device=device)
                tensor120 = torch.rand(120, device=device)
                tensor2145 = torch.rand(2, 1, 4, 5, device=device)
                tensor2345 = torch.rand(2, 3, 4, 5, device=device)
                tensor2345_non_contiguous = torch.rand(2, 4, 3, 5, device=device).permute(0, 2, 1, 3)
                tensor2345_channels_last = tensor2345.contiguous(memory_format=torch.channels_last)
                output2345 = torch.zeros(2, 3, 4, 5, device=device)
                output345 = torch.zeros(3, 4, 5, device=device)

                # inputs have same size
                self.assertEqual(torch.normal(tensor2345, tensor2345).size(), (2, 3, 4, 5))
                self.assertEqual(torch.normal(tensor2345_non_contiguous, tensor2345).size(), (2, 3, 4, 5))
                self.assertEqual(torch.normal(tensor2345, tensor2345_channels_last).size(), (2, 3, 4, 5))
                self.assertEqual(torch.normal(tensor2345_non_contiguous, tensor2345_channels_last).size(), (2, 3, 4, 5))

                # scalar case
                self.assertEqual(torch.normal(tensor2345, 2).size(), (2, 3, 4, 5))
                self.assertEqual(torch.normal(2, tensor2345).size(), (2, 3, 4, 5))

                # inputs are expandable tensors
                self.assertEqual(torch.normal(tensor2345, tensor1).size(), (2, 3, 4, 5))
                self.assertEqual(torch.normal(tensor2145, tensor2345).size(), (2, 3, 4, 5))

                # inputs are non-expandable tensors, but they have same number of elements
                # TORCH_WARN_ONCE is used in torch.normal, only 1st assertEqual will show warn msg
                if not warned:
                    self.assertWarnsRegex(UserWarning, "deprecated and the support will be removed",
                                          lambda: self.assertEqual(torch.normal(tensor120, tensor2345).size(), (120,)))
                    warned = True
                else:
                    self.assertEqual(torch.normal(tensor120, tensor2345).size(), (120,))
                self.assertEqual(torch.normal(tensor2345, tensor120).size(), (2, 3, 4, 5))

                # inputs are non-expandable tensors and they don't have same number of elements
                with self.assertRaisesRegex(RuntimeError, "inconsistent tensor"):
                    torch.normal(tensor2345, tensor4)

                # output and inputs are size compatible
                self.assertEqual(torch.normal(tensor2345, tensor2345, out=output2345).size(), (2, 3, 4, 5))

                # output and inputs are not size compatible
                with self.assertRaisesRegex(RuntimeError, "inconsistent tensor"):
                    # inputs are expandable but have different broadcasted size than output
                    torch.normal(tensor2345, tensor2145, out=output345)
                with self.assertRaisesRegex(RuntimeError, "inconsistent tensor"):
                    # inputs are not expandable but reshapeable, output size is not the same as mean
                    torch.normal(tensor2345, tensor120, out=output345)

        def test_tensoriterator_output_setup(self):
            # Test whether the output's memory layout is correct
            def test_memory_layout(x, y, scale, zero_point, out):
                self.assertEqual(x.dim(), 4)
                self.assertEqual(x.size(), y.size())
                self.assertEqual(y.size(), out.size())

                shape = x.size()
                for n in range(shape[0]):
                    for c in range(shape[1]):
                        for h in range(shape[2]):
                            for w in range(shape[3]):
                                if scale is not None and zero_point is not None:
                                    self.assertEqual(
                                        out[n][c][h][w],
                                        torch.ops.quantized.add(x[n][c][h][w], y[n][c][h][w], scale, zero_point))
                                else:
                                    self.assertEqual(out[n][c][h][w], x[n][c][h][w] + y[n][c][h][w])

            xraw = torch.rand(2, 3, 4, 4)
            yraw = torch.rand(2, 3, 4, 4)
            qxraw = torch.quantize_per_tensor(xraw, 0.1, 5, torch.quint8)
            qyraw = torch.quantize_per_tensor(yraw, 0.1, 5, torch.quint8)

            # contiguous case fast setup
            test_memory_layout(xraw, yraw, None, None, xraw + yraw)
            test_memory_layout(qxraw, qyraw, 0.1, 5, torch.ops.quantized.add(qxraw, qyraw, 0.1, 5))

            # channels last case fast setup
            x = xraw.contiguous(memory_format=torch.channels_last)
            y = yraw.contiguous(memory_format=torch.channels_last)
            test_memory_layout(x, y, None, None, x + y)
            qx = qxraw.contiguous(memory_format=torch.channels_last)
            qy = qyraw.contiguous(memory_format=torch.channels_last)
            test_memory_layout(qx, qy, 0.1, 5, torch.ops.quantized.add(qx, qy, 0.1, 5))

            # non contiguous case fast setup (dense, non-overlapping, same shape and strides)
            x = xraw.permute(0, 2, 3, 1)
            y = yraw.permute(0, 2, 3, 1)
            test_memory_layout(x, y, None, None, x + y)
            qx = qxraw.permute(0, 2, 3, 1)
            qy = qyraw.permute(0, 2, 3, 1)
            test_memory_layout(qx, qy, 0.1, 5, torch.ops.quantized.add(qx, qy, 0.1, 5))

            # non contiguous case fast setup (dense, non-overlapping)
            # input tensors have same shape and strides
            # output tensor have same shape as input tensors but different stride
            # output tensor should preserve its strides in this case
            x = xraw.permute(0, 2, 3, 1)
            y = yraw.permute(0, 2, 3, 1)
            out = torch.empty_like(xraw)
            out = out.permute(0, 3, 2, 1)
            expected_stride = out.stride()
            test_memory_layout(x, y, None, None, torch.add(x, y, out=out))
            self.assertEqual(expected_stride, out.stride())

            # non contiguous case non fast setup
            x = xraw.permute(0, 2, 3, 1)
            y = yraw.permute(0, 3, 2, 1)
            test_memory_layout(x, y, None, None, x + y)
            qx = qxraw.permute(0, 2, 3, 1)
            qy = qyraw.permute(0, 3, 2, 1)
            test_memory_layout(qx, qy, 0.1, 5, torch.ops.quantized.add(qx, qy, 0.1, 5))

        # Tests to make sure we still handle .data properly until it is removed
        def test_dot_data_use(self):
            # .data allows to change the Tensors types inplace, check that we still
            # raise a nice error.
            with self.assertRaisesRegex(
                    RuntimeError,
                    # message includes both Double and Long
                    '(?=.*Double)(?=.*Long)'):

                # Calls model with a LongTensor input but DoubleTensor weights
                input = torch.randn(1, 1, 1, 6, dtype=torch.double)
                weight = torch.zeros(1, 1, 1, 3, dtype=torch.long)
                model = torch.nn.Conv2d(1, 1, (1, 3), stride=1, padding=0, bias=False)
                model.weight.data = weight
                out = model(input)


# Functions to test negative dimension wrapping
METHOD = 1
INPLACE_METHOD = 2
FUNCTIONAL = 4
DIM_ARG = None

def make_neg_dim_test(name, tensor_arg, arg_constr, types, extra_dim=0):
    def neg_dim_test(self):
        if isinstance(tensor_arg, list):
            assert METHOD not in types and INPLACE_METHOD not in types
            x = [torch.randn(arg) for arg in tensor_arg]
            ndim = len(tensor_arg[-1])
        else:
            x = torch.randn(*tensor_arg)
            ndim = len(tensor_arg)
        ndim += extra_dim

        n_dim_to_test = sum(map(lambda e: e is DIM_ARG, arg_constr()))

        for dims_val in combinations(range(ndim), n_dim_to_test):
            arg = arg_constr()
            arg_neg = copy.deepcopy(arg)
            idx = 0
            for i, v in enumerate(arg):
                if v is DIM_ARG:
                    arg[i] = dims_val[idx]
                    arg_neg[i] = dims_val[idx] - ndim
                    idx += 1

            if METHOD in types:
                a = getattr(x, name)(*arg)
                b = getattr(x, name)(*arg_neg)
                self.assertEqual(a, b)

            if INPLACE_METHOD in types:
                a = x.clone()
                getattr(a, name + '_')(*arg)
                b = x.clone()
                getattr(b, name + '_')(*arg_neg)
                self.assertEqual(a, b)

            if FUNCTIONAL in types:
                a = getattr(torch, name)(x, *arg)
                b = getattr(torch, name)(x, *arg_neg)
                self.assertEqual(a, b)

    return neg_dim_test


def idx_tensor(size, max_val):
    return torch.LongTensor(*size).random_(0, max_val - 1)


def add_neg_dim_tests():
    neg_dim_tests = [
        ('narrow', (10, 20, 30), lambda: [DIM_ARG, 0, 5], [METHOD]),
        ('transpose', (10, 20, 30), lambda: [DIM_ARG, DIM_ARG], [METHOD, INPLACE_METHOD, FUNCTIONAL]),
        ('size', (10, 20, 30), lambda: [DIM_ARG], [METHOD]),
        ('cat', [(2, 3, 4), (2, 3, 4)], lambda: [DIM_ARG], [FUNCTIONAL]),
        ('chunk', (10, 20, 30), lambda: [5, DIM_ARG], [METHOD, FUNCTIONAL]),
        ('gather', (10, 20), lambda: [DIM_ARG, idx_tensor((10, 20), 10)], [METHOD, FUNCTIONAL]),
        ('index_select', (10, 10), lambda: [DIM_ARG, idx_tensor((10,), 10)], [METHOD, FUNCTIONAL]),
        ('split', (10, 20), lambda: [5, DIM_ARG], [METHOD, FUNCTIONAL]),
        ('squeeze', (10, 1, 20, 1), lambda: [DIM_ARG], [METHOD, INPLACE_METHOD, FUNCTIONAL]),
        ('unbind', (2, 3, 4), lambda: [DIM_ARG], [FUNCTIONAL]),
        ('unsqueeze', (10, 20), lambda: [DIM_ARG], [METHOD, INPLACE_METHOD, FUNCTIONAL], 1),
        ('logcumsumexp', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('cumprod', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('cumsum', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('cummax', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('cummin', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('mean', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('median', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('mode', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('norm', (10, 20), lambda: [2, DIM_ARG], [METHOD, FUNCTIONAL]),
        ('prod', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('std', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('sum', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('var', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('kthvalue', (10, 20), lambda: [3, DIM_ARG], [METHOD, FUNCTIONAL]),
        ('max', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('min', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('sort', (10, 20), lambda: [DIM_ARG], [METHOD, FUNCTIONAL]),
        ('topk', (10, 20), lambda: [5, DIM_ARG], [METHOD, FUNCTIONAL]),
        ('renorm', (10, 20), lambda: [2, DIM_ARG, 1], [METHOD, INPLACE_METHOD, FUNCTIONAL]),
        ('index_add', (10, 10), lambda: [DIM_ARG, idx_tensor((10,), 10), torch.randn(10, 10)], [INPLACE_METHOD]),
        ('index_copy', (10, 10), lambda: [DIM_ARG, idx_tensor((10,), 10), torch.randn(10, 10)], [INPLACE_METHOD]),
        ('index_fill', (10, 10), lambda: [DIM_ARG, idx_tensor((10,), 10), 12], [INPLACE_METHOD]),
        ('scatter', (10, 10), lambda: [DIM_ARG, idx_tensor((10, 10), 10), torch.randn(10, 10)], [INPLACE_METHOD]),
        ('select', (10, 20), lambda: [DIM_ARG, 3], [METHOD]),
        ('unfold', (10, 20), lambda: [DIM_ARG, 5, 2], [METHOD]),
    ]

    for decl in neg_dim_tests:
        if len(decl) == 4:
            name, tensor_arg, arg_constr, types = decl
            extra_dim = 0
        elif len(decl) == 5:
            name, tensor_arg, arg_constr, types, extra_dim = decl

        test_name = 'test_' + name + '_neg_dim'

        assert not hasattr(AbstractTestCases._TestTorchMixin, test_name), "Duplicated test name: " + test_name
        setattr(AbstractTestCases._TestTorchMixin, test_name, make_neg_dim_test(name, tensor_arg, arg_constr, types, extra_dim))


# Device-generic tests. Instantiated below and not run directly.
class TestTorchDeviceType(TestCase):
    exact_dtype = True

    @onlyCPU
    def test_set_deterministic_beta_warning(self, device):
        det = torch.is_deterministic()
        try:
            # Ensures setting to false does not throw a warning
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                torch.set_deterministic(False)
                self.assertEqual(len(w), 0)

            # Setting set_deterministic(True) throws a warning once per process
            with self.maybeWarnsRegex(UserWarning, "torch.set_deterministic is in beta"):
                torch.set_deterministic(True)
        finally:
            torch.set_deterministic(det)

    # Tests that trying to add, inplace, a CUDA tensor to a CPU tensor
    #   throws the correct error message
    @onlyCUDA
    def test_cross_device_inplace_error_msg(self, device):
        a = torch.tensor(2.)
        b = torch.tensor(2., device=device)
        with self.assertRaisesRegex(RuntimeError,
                                    "Expected all tensors to be on the same device"):
            a += b

    @onlyOnCPUAndCUDA
    def test_out_resize_warning(self, device):
        a = torch.tensor((1, 2, 3), device=device, dtype=torch.float32)
        b = torch.tensor((4, 5, 6), device=device, dtype=torch.float32)

        unary_inputs = (a,)
        binary_inputs = (a, b)
        unary_ops = (torch.ceil, torch.exp)
        binary_ops = (torch.add, torch.sub)
        for op in (unary_ops + binary_ops):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                inputs = unary_inputs if op in unary_ops else binary_inputs

                # No warnings
                op(*inputs, out=torch.empty(3, device=device))
                op(*inputs, out=torch.empty(0, device=device))
                self.assertEqual(len(w), 0)

                # Cases that throw warnings
                op(*inputs, out=torch.empty(2, device=device))
                self.assertEqual(len(w), 1)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.complex64, torch.complex128)
    def test_abs_angle_complex_to_float(self, device, dtype):
        # Constructs random complex values
        from random import random
        random_vals = []
        for multiplier in (-1, 1, -10, 10, -100, 100):
            for _ in range(10):
                random_vals.append(complex(random() * multiplier, random() * multiplier))

        for vals in (random_vals, []):
            a = np.array(vals, dtype=torch_to_numpy_dtype_dict[dtype])
            t = torch.tensor(vals, device=device, dtype=dtype)

            for fn_name in ('abs', 'angle'):
                torch_fn = getattr(torch, fn_name)
                np_fn = getattr(np, fn_name)

                # Tests function
                np_result = torch.from_numpy(np_fn(a))
                torch_result = torch_fn(t).cpu()
                self.assertEqual(np_result, torch_result, exact_dtype=True)

                # Tests float out
                float_dtype = torch.float32 if dtype is torch.complex64 else torch.float64
                np_float_out = np_fn(a).astype(torch_to_numpy_dtype_dict[float_dtype])
                float_out = torch.empty_like(t).float()
                torch_fn(t, out=float_out)
                # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
                self.assertEqualIgnoreType(torch.from_numpy(np_float_out), float_out.cpu())

                # Tests float out (resized out)
                float_out = torch.empty(1, device=device, dtype=float_dtype)
                torch_fn(t, out=float_out)
                self.assertEqual(torch.from_numpy(np_float_out), float_out.cpu())

                # Tests complex out
                np_complex_out = np_fn(a)
                complex_out = torch.empty_like(t)
                torch_fn(t, out=complex_out)
                # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
                self.assertEqualIgnoreType(torch.from_numpy(np_complex_out), complex_out.cpu())

                # Tests complex out (resized out)
                complex_out = torch.empty(0, device=device, dtype=dtype)
                torch_fn(t, out=complex_out)
                # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
                self.assertEqualIgnoreType(torch.from_numpy(np_complex_out), complex_out.cpu())

                # Tests long out behavior (expected failure)
                long_out = torch.empty(0, device=device, dtype=torch.long)
                with self.assertRaises(RuntimeError):
                    torch_fn(t, out=long_out)

                # Tests inplace
                if fn_name == 'abs':
                    torch_inplace_method = getattr(torch.Tensor, fn_name + "_")
                    np_fn(a, out=a)
                    if dtype.is_complex:
                        with self.assertRaisesRegex(RuntimeError, "In-place abs is not supported for complex tensors."):
                            torch_inplace_method(t)
                        return
                    torch_inplace_method(t)
                    self.assertEqual(torch.from_numpy(a), t.cpu())

                # Note: angle does not have an in-place variant
                if fn_name == 'angle':
                    with self.assertRaises(AttributeError):
                        torch_inplace_method = getattr(torch.Tensor, fn_name + "_")

    # Verifies that the inplace dunders (like idiv) actually are in place
    @onlyOnCPUAndCUDA
    def test_inplace_dunders(self, device):
        t = torch.randn((1,), device=device)
        expected = t.data_ptr()
        t += 1
        t -= 1
        t *= 1
        t /= 1
        t //= 1
        self.assertEqual(expected, t.data_ptr())

    @dtypes(torch.float32, torch.complex64)
    def test_storage(self, device, dtype):
        v = torch.randn(3, 5, dtype=dtype, device=device)
        self.assertEqual(v.storage()[0], v[0][0])
        self.assertEqual(v.storage()[14], v[2][4])

    @dtypes(torch.float32, torch.complex64)
    def test_deepcopy(self, device, dtype):
        from copy import deepcopy
        a = torch.randn(5, 5, dtype=dtype, device=device)
        b = torch.randn(5, 5, dtype=dtype, device=device)
        c = a.view(25)
        q = [a, [a.storage(), b.storage()], b, c]
        w = deepcopy(q)
        self.assertEqual(w[0], q[0], atol=0, rtol=0)
        self.assertEqual(w[1][0], q[1][0], atol=0, rtol=0)
        self.assertEqual(w[1][1], q[1][1], atol=0, rtol=0)
        self.assertEqual(w[1], q[1], atol=0, rtol=0)
        self.assertEqual(w[2], q[2], atol=0, rtol=0)

        # Check that deepcopy preserves sharing
        w[0].add_(1)
        for i in range(a.numel()):
            self.assertEqual(w[1][0][i], q[1][0][i] + 1)
        self.assertEqual(w[3], c + 1)
        w[2].sub_(1)
        for i in range(a.numel()):
            self.assertEqual(w[1][1][i], q[1][1][i] - 1)

    @dtypes(torch.float32, torch.complex64)
    def test_deepcopy_scalar(self, device, dtype):
        from copy import deepcopy
        a = torch.tensor(5, dtype=dtype, device=device)
        self.assertEqual(a.size(), deepcopy(a).size())
        self.assertEqual(a, deepcopy(a))

    # Tests that when rtol or atol (including self.precision) is set, then
    # the other is zeroed.
    # TODO: this is legacy behavior and should be updated after test
    # precisions are reviewed to be consistent with torch.isclose.
    @onlyOnCPUAndCUDA
    def test__comparetensors_legacy(self, device):
        a = torch.tensor((10000000.,))
        b = torch.tensor((10000002.,))

        x = torch.tensor((1.,))
        y = torch.tensor((1. + 1e-5,))

        # Helper for reusing the tensor values as scalars
        def _scalar_helper(a, b, rtol=None, atol=None):
            return self._compareScalars(a.item(), b.item(), rtol=rtol, atol=atol)

        for op in (self._compareTensors, _scalar_helper):
            # Tests default
            result, debug_msg = op(a, b)
            self.assertTrue(result)

            # Tests setting atol
            result, debug_msg = op(a, b, atol=2, rtol=0)
            self.assertTrue(result)

            # Tests setting atol too small
            result, debug_msg = op(a, b, atol=1, rtol=0)
            self.assertFalse(result)

            # Tests setting rtol too small
            result, debug_msg = op(x, y, atol=0, rtol=1.05e-5)
            self.assertTrue(result)

            # Tests setting rtol too small
            result, debug_msg = op(x, y, atol=0, rtol=1e-5)
            self.assertFalse(result)

    @onlyOnCPUAndCUDA
    def test__comparescalars_debug_msg(self, device):
        # float x float
        result, debug_msg = self._compareScalars(4., 7.)
        expected_msg = ("Comparing 4.0 and 7.0 gives a difference of 3.0, "
                        "but the allowed difference with rtol=1.3e-06 and "
                        "atol=1e-05 is only 1.9100000000000003e-05!")
        self.assertEqual(debug_msg, expected_msg)

        # complex x complex, real difference
        result, debug_msg = self._compareScalars(complex(1, 3), complex(3, 1))
        expected_msg = ("Comparing the real part 1.0 and 3.0 gives a difference "
                        "of 2.0, but the allowed difference with rtol=1.3e-06 "
                        "and atol=1e-05 is only 1.39e-05!")
        self.assertEqual(debug_msg, expected_msg)

        # complex x complex, imaginary difference
        result, debug_msg = self._compareScalars(complex(1, 3), complex(1, 5.5))
        expected_msg = ("Comparing the imaginary part 3.0 and 5.5 gives a "
                        "difference of 2.5, but the allowed difference with "
                        "rtol=1.3e-06 and atol=1e-05 is only 1.715e-05!")
        self.assertEqual(debug_msg, expected_msg)

        # complex x int
        result, debug_msg = self._compareScalars(complex(1, -2), 1)
        expected_msg = ("Comparing the imaginary part -2.0 and 0.0 gives a "
                        "difference of 2.0, but the allowed difference with "
                        "rtol=1.3e-06 and atol=1e-05 is only 1e-05!")
        self.assertEqual(debug_msg, expected_msg)

        # NaN x NaN, equal_nan=False
        result, debug_msg = self._compareScalars(float('nan'), float('nan'), equal_nan=False)
        expected_msg = ("Found nan and nan while comparing and either one is "
                        "nan and the other isn't, or both are nan and equal_nan "
                        "is False")
        self.assertEqual(debug_msg, expected_msg)

    # Checks that compareTensors provides the correct debug info
    @onlyOnCPUAndCUDA
    def test__comparetensors_debug_msg(self, device):
        # Acquires atol that will be used
        atol = max(1e-05, self.precision)

        # Checks float tensor comparisons (2D tensor)
        a = torch.tensor(((0, 6), (7, 9)), device=device, dtype=torch.float32)
        b = torch.tensor(((0, 7), (7, 22)), device=device, dtype=torch.float32)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("With rtol=1.3e-06 and atol={0}, found 2 element(s) (out of 4) "
                        "whose difference(s) exceeded the margin of error (including 0 nan comparisons). "
                        "The greatest difference was 13.0 (9.0 vs. 22.0), "
                        "which occurred at index (1, 1).").format(atol)
        self.assertEqual(debug_msg, expected_msg)

        # Checks float tensor comparisons (with extremal values)
        a = torch.tensor((float('inf'), 5, float('inf')), device=device, dtype=torch.float32)
        b = torch.tensor((float('inf'), float('nan'), float('-inf')), device=device, dtype=torch.float32)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("With rtol=1.3e-06 and atol={0}, found 2 element(s) (out of 3) "
                        "whose difference(s) exceeded the margin of error (including 1 nan comparisons). "
                        "The greatest difference was nan (5.0 vs. nan), "
                        "which occurred at index 1.").format(atol)
        self.assertEqual(debug_msg, expected_msg)

        # Checks float tensor comparisons (with finite vs nan differences)
        a = torch.tensor((20, -6), device=device, dtype=torch.float32)
        b = torch.tensor((-1, float('nan')), device=device, dtype=torch.float32)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("With rtol=1.3e-06 and atol={0}, found 2 element(s) (out of 2) "
                        "whose difference(s) exceeded the margin of error (including 1 nan comparisons). "
                        "The greatest difference was nan (-6.0 vs. nan), "
                        "which occurred at index 1.").format(atol)
        self.assertEqual(debug_msg, expected_msg)

        # Checks int tensor comparisons (1D tensor)
        a = torch.tensor((1, 2, 3, 4), device=device)
        b = torch.tensor((2, 5, 3, 4), device=device)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("Found 2 different element(s) (out of 4), "
                        "with the greatest difference of 3 (2 vs. 5) "
                        "occuring at index 1.")
        self.assertEqual(debug_msg, expected_msg)

        # Checks bool tensor comparisons (0D tensor)
        a = torch.tensor((True), device=device)
        b = torch.tensor((False), device=device)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("Found 1 different element(s) (out of 1), "
                        "with the greatest difference of 1 (1 vs. 0) "
                        "occuring at index 0.")
        self.assertEqual(debug_msg, expected_msg)

        # Checks complex tensor comparisons (real part)
        a = torch.tensor((1 - 1j, 4 + 3j), device=device)
        b = torch.tensor((1 - 1j, 1 + 3j), device=device)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("Real parts failed to compare as equal! "
                        "With rtol=1.3e-06 and atol={0}, "
                        "found 1 element(s) (out of 2) whose difference(s) exceeded the "
                        "margin of error (including 0 nan comparisons). The greatest difference was "
                        "3.0 (4.0 vs. 1.0), which occurred at index 1.").format(atol)
        self.assertEqual(debug_msg, expected_msg)

        # Checks complex tensor comparisons (imaginary part)
        a = torch.tensor((1 - 1j, 4 + 3j), device=device)
        b = torch.tensor((1 - 1j, 4 - 21j), device=device)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("Imaginary parts failed to compare as equal! "
                        "With rtol=1.3e-06 and atol={0}, "
                        "found 1 element(s) (out of 2) whose difference(s) exceeded the "
                        "margin of error (including 0 nan comparisons). The greatest difference was "
                        "24.0 (3.0 vs. -21.0), which occurred at index 1.").format(atol)
        self.assertEqual(debug_msg, expected_msg)

        # Checks size mismatch
        a = torch.tensor((1, 2), device=device)
        b = torch.tensor((3), device=device)
        result, debug_msg = self._compareTensors(a, b)
        expected_msg = ("Attempted to compare equality of tensors "
                        "with different sizes. Got sizes torch.Size([2]) and torch.Size([]).")
        self.assertEqual(debug_msg, expected_msg)

        # Checks dtype mismatch
        a = torch.tensor((1, 2), device=device, dtype=torch.long)
        b = torch.tensor((1, 2), device=device, dtype=torch.float32)
        result, debug_msg = self._compareTensors(a, b, exact_dtype=True)
        expected_msg = ("Attempted to compare equality of tensors "
                        "with different dtypes. Got dtypes torch.int64 and torch.float32.")
        self.assertEqual(debug_msg, expected_msg)

        # Checks device mismatch
        if self.device_type == 'cuda':
            a = torch.tensor((5), device='cpu')
            b = torch.tensor((5), device=device)
            result, debug_msg = self._compareTensors(a, b, exact_device=True)
            expected_msg = ("Attempted to compare equality of tensors "
                            "on different devices! Got devices cpu and cuda:0.")
            self.assertEqual(debug_msg, expected_msg)

    # Helper for testing _compareTensors and _compareScalars
    # Works on single element tensors
    def _comparetensors_helper(self, tests, device, dtype, equal_nan, exact_dtype=True, atol=1e-08, rtol=1e-05):
        for test in tests:
            a = torch.tensor((test[0],), device=device, dtype=dtype)
            b = torch.tensor((test[1],), device=device, dtype=dtype)

            # Tensor x Tensor comparison
            compare_result, debug_msg = self._compareTensors(a, b, rtol=rtol, atol=atol,
                                                             equal_nan=equal_nan,
                                                             exact_dtype=exact_dtype)
            self.assertEqual(compare_result, test[2])

            # Scalar x Scalar comparison
            compare_result, debug_msg = self._compareScalars(a.item(), b.item(),
                                                             rtol=rtol, atol=atol,
                                                             equal_nan=equal_nan)
            self.assertEqual(compare_result, test[2])

    def _isclose_helper(self, tests, device, dtype, equal_nan, atol=1e-08, rtol=1e-05):
        for test in tests:
            a = torch.tensor((test[0],), device=device, dtype=dtype)
            b = torch.tensor((test[1],), device=device, dtype=dtype)

            actual = torch.isclose(a, b, equal_nan=equal_nan, atol=atol, rtol=rtol)
            expected = test[2]
            self.assertEqual(actual.item(), expected)

    # torch.close is not implemented for bool tensors
    # see https://github.com/pytorch/pytorch/issues/33048
    def test_isclose_comparetensors_bool(self, device):
        tests = (
            (True, True, True),
            (False, False, True),
            (True, False, False),
            (False, True, False),
        )

        with self.assertRaises(RuntimeError):
            self._isclose_helper(tests, device, torch.bool, False)

        self._comparetensors_helper(tests, device, torch.bool, False)

    @dtypes(torch.uint8,
            torch.int8, torch.int16, torch.int32, torch.int64)
    def test_isclose_comparetensors_integer(self, device, dtype):
        tests = (
            (0, 0, True),
            (0, 1, False),
            (1, 0, False),
        )

        self._isclose_helper(tests, device, dtype, False)

        # atol and rtol tests
        tests = [
            (0, 1, True),
            (1, 0, False),
            (1, 3, True),
        ]

        self._isclose_helper(tests, device, dtype, False, atol=.5, rtol=.5)
        self._comparetensors_helper(tests, device, dtype, False, atol=.5, rtol=.5)

        if dtype is torch.uint8:
            tests = [
                (-1, 1, False),
                (1, -1, False)
            ]
        else:
            tests = [
                (-1, 1, True),
                (1, -1, True)
            ]

        self._isclose_helper(tests, device, dtype, False, atol=1.5, rtol=.5)
        self._comparetensors_helper(tests, device, dtype, False, atol=1.5, rtol=.5)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float16, torch.float32, torch.float64)
    def test_isclose_comparetensors_float(self, device, dtype):
        tests = (
            (0, 0, True),
            (0, -1, False),
            (float('inf'), float('inf'), True),
            (-float('inf'), float('inf'), False),
            (float('inf'), float('nan'), False),
            (float('nan'), float('nan'), False),
            (0, float('nan'), False),
            (1, 1, True),
        )

        self._isclose_helper(tests, device, dtype, False)
        self._comparetensors_helper(tests, device, dtype, False)

        # atol and rtol tests
        eps = 1e-2 if dtype is torch.half else 1e-6
        tests = (
            (0, 1, True),
            (0, 1 + eps, False),
            (1, 0, False),
            (1, 3, True),
            (1 - eps, 3, False),
            (-.25, .5, True),
            (-.25 - eps, .5, False),
            (.25, -.5, True),
            (.25 + eps, -.5, False),
        )

        self._isclose_helper(tests, device, dtype, False, atol=.5, rtol=.5)
        self._comparetensors_helper(tests, device, dtype, False, atol=.5, rtol=.5)

        # equal_nan = True tests
        tests = (
            (0, float('nan'), False),
            (float('inf'), float('nan'), False),
            (float('nan'), float('nan'), True),
        )

        self._isclose_helper(tests, device, dtype, True)

        self._comparetensors_helper(tests, device, dtype, True)

    # torch.close with equal_nan=True is not implemented for complex inputs
    # see https://github.com/numpy/numpy/issues/15959
    # Note: compareTensor will compare the real and imaginary parts of a
    # complex tensors separately, unlike isclose.
    @dtypes(torch.complex64, torch.complex128)
    def test_isclose_comparetensors_complex(self, device, dtype):
        tests = (
            (complex(1, 1), complex(1, 1 + 1e-8), True),
            (complex(0, 1), complex(1, 1), False),
            (complex(1, 1), complex(1, 0), False),
            (complex(1, 1), complex(1, float('nan')), False),
            (complex(1, float('nan')), complex(1, float('nan')), False),
            (complex(1, 1), complex(1, float('inf')), False),
            (complex(float('inf'), 1), complex(1, float('inf')), False),
            (complex(-float('inf'), 1), complex(1, float('inf')), False),
            (complex(-float('inf'), 1), complex(float('inf'), 1), False),
            (complex(float('inf'), 1), complex(float('inf'), 1), True),
            (complex(float('inf'), 1), complex(float('inf'), 1 + 1e-4), False),
        )

        self._isclose_helper(tests, device, dtype, False)
        self._comparetensors_helper(tests, device, dtype, False)

        # atol and rtol tests

        # atol and rtol tests
        eps = 1e-6
        tests = (
            # Complex versions of float tests (real part)
            (complex(0, 0), complex(1, 0), True),
            (complex(0, 0), complex(1 + eps, 0), False),
            (complex(1, 0), complex(0, 0), False),
            (complex(1, 0), complex(3, 0), True),
            (complex(1 - eps, 0), complex(3, 0), False),
            (complex(-.25, 0), complex(.5, 0), True),
            (complex(-.25 - eps, 0), complex(.5, 0), False),
            (complex(.25, 0), complex(-.5, 0), True),
            (complex(.25 + eps, 0), complex(-.5, 0), False),
            # Complex versions of float tests (imaginary part)
            (complex(0, 0), complex(0, 1), True),
            (complex(0, 0), complex(0, 1 + eps), False),
            (complex(0, 1), complex(0, 0), False),
            (complex(0, 1), complex(0, 3), True),
            (complex(0, 1 - eps), complex(0, 3), False),
            (complex(0, -.25), complex(0, .5), True),
            (complex(0, -.25 - eps), complex(0, .5), False),
            (complex(0, .25), complex(0, -.5), True),
            (complex(0, .25 + eps), complex(0, -.5), False),
        )

        self._isclose_helper(tests, device, dtype, False, atol=.5, rtol=.5)
        self._comparetensors_helper(tests, device, dtype, False, atol=.5, rtol=.5)

        # atol and rtol tests for isclose
        tests = (
            # Complex-specific tests
            (complex(1, -1), complex(-1, 1), False),
            (complex(1, -1), complex(2, -2), True),
            (complex(-math.sqrt(2), math.sqrt(2)),
             complex(-math.sqrt(.5), math.sqrt(.5)), True),
            (complex(-math.sqrt(2), math.sqrt(2)),
             complex(-math.sqrt(.501), math.sqrt(.499)), False),
            (complex(2, 4), complex(1., 8.8523607), True),
            (complex(2, 4), complex(1., 8.8523607 + eps), False),
            (complex(1, 99), complex(4, 100), True),
        )

        self._isclose_helper(tests, device, dtype, False, atol=.5, rtol=.5)

        # atol and rtol tests for compareTensors
        tests = (
            (complex(1, -1), complex(-1, 1), False),
            (complex(1, -1), complex(2, -2), True),
            (complex(1, 99), complex(4, 100), False),
        )

        self._comparetensors_helper(tests, device, dtype, False, atol=.5, rtol=.5)

        # equal_nan = True tests
        tests = (
            (complex(1, 1), complex(1, float('nan')), False),
            (complex(float('nan'), 1), complex(1, float('nan')), False),
            (complex(float('nan'), 1), complex(float('nan'), 1), True),
        )

        with self.assertRaises(RuntimeError):
            self._isclose_helper(tests, device, dtype, True)

        self._comparetensors_helper(tests, device, dtype, True)

    # Tests that isclose with rtol or atol values less than zero throws a
    #   RuntimeError
    @dtypes(torch.bool, torch.uint8,
            torch.int8, torch.int16, torch.int32, torch.int64,
            torch.float16, torch.float32, torch.float64)
    def test_isclose_atol_rtol_greater_than_zero(self, device, dtype):
        t = torch.tensor((1,), device=device, dtype=dtype)

        with self.assertRaises(RuntimeError):
            torch.isclose(t, t, atol=-1, rtol=1)
        with self.assertRaises(RuntimeError):
            torch.isclose(t, t, atol=1, rtol=-1)
        with self.assertRaises(RuntimeError):
            torch.isclose(t, t, atol=-1, rtol=-1)

    # XLA tests fail for self.assertRaises for complex dtypes
    @onlyOnCPUAndCUDA
    def test_complex_assert_raises(self, device):
        for dtype in [torch.complex64, torch.complex128]:
            size = [5, 5]
            tensor = torch.rand(size, dtype=dtype, device=device)

            # index_add calls atomicAdd on cuda.
            zeros = torch.zeros(size, dtype=dtype, device=device)

            # index_add is not supported for complex dtypes on cuda yet
            if device.startswith('cuda') and dtype.is_complex:
                self.assertRaises(RuntimeError,
                                  lambda: zeros.index_add(0, torch.arange(0, size[0], dtype=torch.long, device=device), tensor))

            self.assertRaises(RuntimeError, lambda: torch.sign(torch.tensor([4j], device=device, dtype=dtype)))

            a = torch.rand((2, 2), dtype=dtype, device=device)
            b = torch.rand((2, 2), dtype=dtype, device=device)
            c = torch.rand((2, 2), dtype=dtype, device=device)
            alpha = 3

            # addcmul is not supported for complex dtypes on cuda yet
            if device.startswith('cuda') and dtype.is_complex:
                self.assertRaises(RuntimeError, lambda: torch.addcmul(a, b, c, value=alpha))

    def check_internal_mem_overlap(self, inplace_op, num_inputs,
                                   dtype, device,
                                   expected_failure=False):
        if isinstance(inplace_op, str):
            inplace_op = getattr(torch.Tensor, inplace_op)
        input = torch.randn(1, dtype=dtype, device=device).expand(3, 3)
        inputs = [input] + [torch.randn_like(input)
                            for i in range(num_inputs - 1)]
        if not expected_failure:
            with self.assertRaisesRegex(RuntimeError, 'single memory location'):
                inplace_op(*inputs)
        else:
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(RuntimeError, 'single memory location'):
                    inplace_op(*inputs)

    def unary_check_input_output_mem_overlap(self, data, sz, op,
                                             expected_failure=False):

        def _test(op, output, input):
            output_exp = torch.empty_like(output)
            op(input, out=output_exp)
            self.assertEqual(op(input, out=output), output_exp, msg=op.__name__)

        # output is identical to input:
        _test(op, output=data[0:sz], input=data[0:sz])
        # output and input are independent:
        _test(op, output=data[0:sz], input=data[sz:2 * sz])
        # output partially overlaps with input:
        if not expected_failure:
            with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
                _test(op, data[0:sz], data[1:sz + 1])
        else:
            with self.assertRaises(AssertionError):
                with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
                    _test(op, data[0:sz], data[1:sz + 1])

    def binary_check_input_output_mem_overlap(self, op, device,
                                              expected_failure=False):
        sz = 3
        data = torch.randn(2 * sz, device=device)
        other = torch.randn(sz, device=device)

        self.unary_check_input_output_mem_overlap(
            data, sz, lambda input, out: op(other, input, out=out),
            expected_failure=expected_failure)

        self.unary_check_input_output_mem_overlap(
            data, sz, lambda input, out: op(input, other, out=out),
            expected_failure=expected_failure)

    def ternary_check_input_output_mem_overlap(self, op, device,
                                               expected_failure=False):
        sz = 3
        data = torch.randn(2 * sz, device=device)
        other1 = torch.randn(sz, device=device)
        other2 = torch.randn(sz, device=device)

        self.unary_check_input_output_mem_overlap(
            data, sz, lambda input, out: op(input, other1, other2, out=out),
            expected_failure=expected_failure)

        self.unary_check_input_output_mem_overlap(
            data, sz, lambda input, out: op(other1, input, other2, out=out),
            expected_failure=expected_failure)

        self.unary_check_input_output_mem_overlap(
            data, sz, lambda input, out: op(other1, other2, input, out=out),
            expected_failure=expected_failure)

    def _test_pow(self, base, exponent, np_exponent=None):
        if np_exponent is None:
            np_exponent = exponent

        def to_np(value):
            if isinstance(value, torch.Tensor):
                return value.cpu().numpy()
            return value

        try:
            expected = torch.from_numpy(
                np.power(to_np(base), to_np(np_exponent)))
        except ValueError as e:
            err_msg = "Integers to negative integer powers are not allowed."
            self.assertEqual(str(e), err_msg)
            out = torch.empty_like(base)
            test_cases = [
                lambda: base.pow(exponent),
                lambda: base.pow_(exponent),
                lambda: torch.pow(base, exponent),
                lambda: torch.pow(base, exponent, out=out)
            ]
            for test_case in test_cases:
                self.assertRaisesRegex(RuntimeError, err_msg, test_case)
        else:
            if isinstance(base, torch.Tensor):
                actual = base.pow(exponent)
                self.assertEqual(actual, expected.to(actual))
                actual = base.clone()
                if torch.can_cast(torch.result_type(base, exponent), base.dtype):
                    actual2 = actual.pow_(exponent)
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual2, expected)
                else:
                    self.assertRaisesRegex(RuntimeError, "can't be cast", lambda: actual.pow_(exponent))

            actual = torch.pow(base, exponent)
            self.assertEqual(actual, expected.to(actual))

            actual2 = torch.pow(base, exponent, out=actual)
            self.assertEqual(actual, expected.to(actual))
            self.assertEqual(actual2, expected.to(actual))

    def _select_broadcastable_dims(self, dims_full=None):
        # select full dimensionality
        if dims_full is None:
            dims_full = []
            ndims = random.randint(1, 4)
            dims_full = [random.randint(1, 8) for _ in range(ndims)]
        else:
            ndims = len(dims_full)

        # select actual dimensions for ops:
        # larger: full ndims, individual sizes may be reduced
        # smaller: possibly reduced ndims, sizes may be reduced
        smaller_ndims = random.randint(1, ndims)
        dims_small = []
        dims_large = []
        for i in range(ndims - 1, -1, -1):
            j = random.randint(1, 3)
            if j == 1:  # no reduced singleton dimension
                ds = dims_full[i]
                dl = dims_full[i]
            elif j == 2:  # larger may have reduced singleton dimension
                ds = dims_full[i]
                dl = 1 if len(dims_small) < smaller_ndims else dims_full[i]
            elif j == 3:  # smaller may have reduced singleton dimension
                ds = 1
                dl = dims_full[i]
            dims_large = [dl] + dims_large
            if len(dims_small) < smaller_ndims:
                dims_small = [ds] + dims_small
        return (dims_small, dims_large, dims_full)

    # collected tests of ops that used scalar_check in Declarations.cwrap for
    # correctness
    def test_scalar_check(self, device):
        zero_d = torch.randn((), device=device)
        one_d = torch.randn((1,), device=device)

        # _multinomial_alias_setup
        self.assertRaises(RuntimeError, lambda: torch._multinomial_alias_setup(zero_d))

        # remainder
        self.assertEqual((), torch.remainder(zero_d, zero_d).shape)
        self.assertEqual((), torch.remainder(zero_d, 2).shape)
        self.assertEqual((1,), torch.remainder(zero_d, one_d).shape)
        self.assertEqual((1,), torch.remainder(one_d, zero_d).shape)

        # fmod
        self.assertEqual((), torch.fmod(zero_d, zero_d).shape)
        self.assertEqual((), torch.fmod(zero_d, 2).shape)
        self.assertEqual((1,), torch.fmod(zero_d, one_d).shape)
        self.assertEqual((1,), torch.fmod(one_d, zero_d).shape)

        # exp, cos, cosh, tan, atan, tanh, erf, erfc, reciprocal
        self.assertEqual((), torch.exp(zero_d).shape)
        self.assertEqual((), torch.cos(zero_d).shape)
        self.assertEqual((), torch.cosh(zero_d).shape)
        self.assertEqual((), torch.tan(zero_d).shape)
        self.assertEqual((), torch.atan(zero_d).shape)
        self.assertEqual((), torch.acosh(zero_d).shape)
        self.assertEqual((), torch.asinh(zero_d).shape)
        self.assertEqual((), torch.atanh(zero_d).shape)
        self.assertEqual((), torch.tanh(zero_d).shape)
        self.assertEqual((), torch.erf(zero_d).shape)
        self.assertEqual((), torch.erfc(zero_d).shape)
        self.assertEqual((), torch.reciprocal(zero_d).shape)
        self.assertEqual((1,), torch.exp(one_d).shape)
        self.assertEqual((1,), torch.cos(one_d).shape)
        self.assertEqual((1,), torch.cosh(one_d).shape)
        self.assertEqual((1,), torch.tan(one_d).shape)
        self.assertEqual((1,), torch.atan(one_d).shape)
        self.assertEqual((1,), torch.acosh(one_d).shape)
        self.assertEqual((1,), torch.asinh(one_d).shape)
        self.assertEqual((1,), torch.atanh(one_d).shape)
        self.assertEqual((1,), torch.tanh(one_d).shape)
        self.assertEqual((1,), torch.erf(one_d).shape)
        self.assertEqual((1,), torch.erfc(one_d).shape)
        self.assertEqual((1,), torch.reciprocal(one_d).shape)

        # clamp
        self.assertEqual((), torch.clamp(zero_d, min=0, max=1).shape)
        self.assertEqual((), torch.clamp(zero_d, min=0).shape)
        self.assertEqual((), torch.clamp(zero_d, max=1).shape)
        self.assertEqual((1,), torch.clamp(one_d, min=0, max=1).shape)
        self.assertEqual((1,), torch.clamp(one_d, min=0).shape)
        self.assertEqual((1,), torch.clamp(one_d, max=1).shape)

        # cumsum, cumprod, cummax, cummin
        self.assertEqual((), torch.logcumsumexp(zero_d, 0).shape)
        self.assertEqual((), torch.cumsum(zero_d, 0).shape)
        self.assertEqual((), torch.cumprod(zero_d, 0).shape)
        self.assertEqual((), torch.cummax(zero_d, 0)[0].shape)
        self.assertEqual((), torch.cummin(zero_d, 0)[0].shape)

        # renorm
        self.assertRaises(RuntimeError, lambda: torch.renorm(zero_d, 0.5, 0, 1.0))

        # sort, topk
        self.assertEqual([(), ()], [x.shape for x in torch.sort(zero_d, 0, False)])
        self.assertEqual([(), ()], [x.shape for x in torch.sort(zero_d, 0, True)])
        self.assertEqual([(), ()], [x.shape for x in torch.topk(zero_d, 1, 0, False)])
        self.assertEqual([(), ()], [x.shape for x in torch.topk(zero_d, 1, 0, True)])

        # lstsq (gels)
        self.assertRaises(RuntimeError, lambda: torch.lstsq(zero_d, zero_d))

        # eig
        self.assertRaises(RuntimeError, lambda: torch.eig(zero_d, False))
        self.assertRaises(RuntimeError, lambda: torch.eig(zero_d, True))

        # this is only implemented on cpu
        if (torch.device(device).type == 'cpu'):
            self.assertRaises(RuntimeError, lambda: torch.ormqr(zero_d, zero_d, zero_d))

        # max, min
        self.assertEqual((), torch.max(zero_d, zero_d).shape)
        self.assertEqual((1,), torch.max(one_d, zero_d).shape)
        self.assertEqual((1,), torch.max(zero_d, one_d).shape)
        self.assertEqual((), torch.min(zero_d, zero_d).shape)
        self.assertEqual((1,), torch.min(one_d, zero_d).shape)
        self.assertEqual((1,), torch.min(zero_d, one_d).shape)

        # diag
        self.assertRaises(RuntimeError, lambda: torch.diag(zero_d))

        zero_d_int = torch.tensor(1, device=device)
        one_d_int = torch.tensor([1], device=device)

        # lshift, rshift
        self.assertEqual((), (zero_d_int >> zero_d_int).shape)
        self.assertEqual((), (zero_d_int >> 1).shape)
        self.assertEqual((1,), (one_d_int >> zero_d_int).shape)
        self.assertEqual((1,), (zero_d_int >> one_d_int).shape)
        self.assertEqual((1,), (one_d_int >> 1).shape)

        self.assertEqual((), (zero_d_int << zero_d_int).shape)
        self.assertEqual((), (zero_d_int << 1).shape)
        self.assertEqual((1,), (one_d_int << zero_d_int).shape)
        self.assertEqual((1,), (zero_d_int << one_d_int).shape)
        self.assertEqual((1,), (one_d_int << 1).shape)

        # or
        self.assertEqual((), (zero_d_int | zero_d_int).shape)
        self.assertEqual((), (zero_d_int | 1).shape)
        self.assertEqual((1,), (one_d_int | zero_d_int).shape)
        self.assertEqual((1,), (zero_d_int | one_d_int).shape)
        self.assertEqual((1,), (one_d_int | 1).shape)

        # and
        self.assertEqual((), (zero_d_int & zero_d_int).shape)
        self.assertEqual((), (zero_d_int & 1).shape)
        self.assertEqual((1,), (one_d_int & zero_d_int).shape)
        self.assertEqual((1,), (zero_d_int & one_d_int).shape)
        self.assertEqual((1,), (one_d_int & 1).shape)

        # _multinomial_alias_draw
        self.assertRaises(RuntimeError, lambda: torch._multinomial_alias_draw(zero_d, zero_d_int, 10))

        # clone
        self.assertEqual((), zero_d.clone().shape)

        zero_d_bool = torch.tensor(True, device=device)
        one_d_bool = torch.tensor([True], device=device)

        # masked_select
        self.assertEqual((1,), torch.masked_select(zero_d_bool, zero_d_bool).shape)
        self.assertEqual((1,), torch.masked_select(zero_d_bool, one_d_bool).shape)
        self.assertEqual((1,), torch.masked_select(one_d_bool, zero_d_bool).shape)

        zero_d_uint8 = torch.tensor(1, dtype=torch.uint8, device=device)
        one_d_uint8 = torch.tensor([1], dtype=torch.uint8, device=device)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertEqual((1,), torch.masked_select(zero_d_uint8, zero_d_uint8).shape)
            self.assertEqual((1,), torch.masked_select(zero_d_uint8, one_d_uint8).shape)
            self.assertEqual((1,), torch.masked_select(one_d_uint8, zero_d_uint8).shape)

        # mode
        self.assertEqual([(), ()], [x.shape for x in torch.mode(zero_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.mode(zero_d, dim=0, keepdim=False)])
        self.assertEqual([(1,), (1,)], [x.shape for x in torch.mode(one_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.mode(one_d, dim=0, keepdim=False)])

        # max
        self.assertEqual([(), ()], [x.shape for x in torch.max(zero_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.max(zero_d, dim=0, keepdim=False)])
        self.assertEqual([(1,), (1,)], [x.shape for x in torch.max(one_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.max(one_d, dim=0, keepdim=False)])

        # amax
        self.assertEqual((), torch.amax(zero_d, dim=0, keepdim=True).shape)
        self.assertEqual((), torch.amax(zero_d, dim=0, keepdim=False).shape)
        self.assertEqual((1,), torch.amax(one_d, dim=0, keepdim=True).shape)
        self.assertEqual((), torch.amax(one_d, dim=0, keepdim=False).shape)

        # min
        self.assertEqual([(), ()], [x.shape for x in torch.min(zero_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.min(zero_d, dim=0, keepdim=False)])
        self.assertEqual([(1,), (1,)], [x.shape for x in torch.min(one_d, dim=0, keepdim=True)])
        self.assertEqual([(), ()], [x.shape for x in torch.min(one_d, dim=0, keepdim=False)])

        # amin
        self.assertEqual((), torch.amin(zero_d, dim=0, keepdim=True).shape)
        self.assertEqual((), torch.amin(zero_d, dim=0, keepdim=False).shape)
        self.assertEqual((1,), torch.amin(one_d, dim=0, keepdim=True).shape)
        self.assertEqual((), torch.amin(one_d, dim=0, keepdim=False).shape)

        # set_
        zero_d_clone = zero_d.clone()
        one_d_clone = one_d.clone()
        self.assertEqual((), zero_d_clone.set_(one_d.storage(), 0, (), ()).shape)
        self.assertEqual((1,), zero_d_clone.set_(one_d.storage(), 0, (1,), (1,)).shape)
        self.assertEqual((), one_d_clone.set_(one_d.storage(), 0, (), ()).shape)
        self.assertEqual((1,), one_d_clone.set_(one_d.storage(), 0, (1,), (1,)).shape)

        self.assertEqual((), zero_d.clone().set_(zero_d).shape)
        self.assertEqual((), one_d.clone().set_(zero_d).shape)
        self.assertEqual((1,), zero_d.clone().set_(one_d).shape)
        self.assertEqual((1,), one_d.clone().set_(one_d).shape)

        # take
        self.assertEqual((), torch.randn((2, 3), device=device).take(zero_d_int).shape)
        self.assertEqual((1,), torch.randn((2, 3), device=device).take(one_d_int).shape)

        # gather
        self.assertEqual((), torch.gather(zero_d, 0, torch.zeros((), dtype=torch.int64, device=device)).shape)
        self.assertEqual((1,), torch.gather(zero_d, 0, torch.zeros((1,), dtype=torch.int64, device=device)).shape)
        self.assertEqual((), torch.gather(one_d, 0, torch.zeros((), dtype=torch.int64, device=device)).shape)
        self.assertEqual((1,), torch.gather(one_d, 0, torch.zeros((1,), dtype=torch.int64, device=device)).shape)

        # normal
        # documentation says out shape matches shape of mean
        self.assertEqual((), torch.normal(zero_d, zero_d).shape)
        self.assertEqual((1,), torch.normal(one_d, zero_d).shape)
        self.assertEqual((), torch.normal(1, zero_d).shape)
        self.assertEqual((), torch.normal(zero_d, 1).shape)
        self.assertEqual((1,), torch.normal(one_d, 1).shape)
        # TODO: this behavior differs on CPU and GPU, see https://github.com/pytorch/pytorch/issues/30480.
        # self.assertEqual((), torch.normal(zero_d, one_d).shape)
        # self.assertEqual((), torch.normal(1, one_d).shape)

        # convolutions.  Yes, we are testing nn.functional here; seems justified
        # given its similar to the other tests
        w = torch.randn(2, 1, 3, 3, device=device).div_(2).requires_grad_()
        self.assertRaises(RuntimeError, lambda: torch.nn.functional.conv2d(zero_d, w, groups=1))
        self.assertRaises(RuntimeError, lambda: torch.nn.functional.conv2d(zero_d, w, groups=2))

        # nll_loss -- verify input can't be 0-dimensional.
        self.assertRaises(ValueError, lambda: torch.nn.functional.nll_loss(zero_d, zero_d, reduction='none'))
        self.assertRaises(ValueError, lambda: torch.nn.functional.nll_loss(zero_d, one_d, reduction='none'))
        # verify output is 0-dimensional when reduction != 'none'
        for (input, target) in ((torch.randn(1, 1, device=device), torch.tensor([0], device=device)),
                                (torch.randn(1, 1, 1, 1, device=device), torch.tensor([[[0]]], device=device))):
            self.assertEqual((), torch.nn.functional.nll_loss(input, target, reduction='mean').shape)
            self.assertEqual((), torch.nn.functional.nll_loss(input, target, reduction='sum').shape)

        # multilabel_margin_loss
        for input in (zero_d, one_d, torch.randn(1, 1, device=device)):
            for target in (torch.tensor(0, device=device), torch.tensor([0], device=device), torch.tensor([[0]], device=device)):
                if (input.dim() <= 1 and target.dim() <= 1) or (input.dim() == 2 and target.dim() == 2):
                    output_shape = (target.shape[0],) if target.dim() == 2 else ()
                    self.assertEqual(output_shape,
                                     torch.nn.functional.multilabel_margin_loss(input, target, reduction='none').shape)
                    self.assertEqual((), torch.nn.functional.multilabel_margin_loss(input, target, reduction='mean').shape)
                    self.assertEqual((), torch.nn.functional.multilabel_margin_loss(input, target, reduction='sum').shape)
                else:
                    self.assertRaises(RuntimeError,
                                      lambda: torch.nn.functional.multilabel_margin_loss(input, target, reduction='none'))
                    self.assertRaises(RuntimeError,
                                      lambda: torch.nn.functional.multilabel_margin_loss(input, target, reduction='mean'))
                    self.assertRaises(RuntimeError,
                                      lambda: torch.nn.functional.multilabel_margin_loss(input, target, reduction='sum'))

        # multi_margin_loss
        for input in (zero_d, one_d, torch.randn(1, 1, device=device)):
            for target in (torch.tensor(0, device=device), torch.tensor([0], device=device)):
                self.assertEqual(target.shape, torch.nn.functional.multi_margin_loss(input, target, reduction='none').shape)
                self.assertEqual((), torch.nn.functional.multi_margin_loss(input, target, reduction='mean').shape)
                self.assertEqual((), torch.nn.functional.multi_margin_loss(input, target, reduction='sum').shape)

    # Uses mismatched arange out size to trigger a warning
    def test_cpp_warnings_have_python_context(self, device):
        # Creates long string in advance to avoid a too-long Python line
        s = ".+Triggered internally at.+RangeFactories.+"

        def cpp_warn_fn():
            out = torch.empty((5,))
            torch.arange(0, 3, out=out)
            return out

        # Checks eager-mode cpp warning
        with warnings.catch_warnings(record=True) as w:
            cpp_warn_fn()
            frameinfo = inspect.getframeinfo(inspect.currentframe())
            warning = w[0]

            # Checks for cpp context in the warning message
            self.assertTrue(re.search(s, str(warning.message)) is not None)

            # Checks the Python features of the warning
            # Note: the eager mode warning refers to the line in the function
            # that throws the warning.
            self.assertEqual(frameinfo.lineno - 6, warning.lineno)
            self.assertEqual(len(w), 1)

        # Checks jitted cpp warning
        with warnings.catch_warnings(record=True) as w:
            scripted_cpp_warn_fn = torch.jit.script(cpp_warn_fn)
            scripted_cpp_warn_fn()
            warning = w[0]

            # Checks for cpp context in the warning message
            self.assertTrue(re.search(s, str(warning.message)) is not None)

            # Checks the Python features of the warning
            # Note: the jitted warning's lineno refers to the call to the jitted
            # function, which in our test suite has a layer of indirection
            # that makes checking the Python lineno fragile
            self.assertEqual(len(w), 1)

        # Checks jitted Python warning
        def warn_fn():
            warnings.warn("Warning!")

        # The jit mimics an eager-mode Python warning in this case
        with warnings.catch_warnings(record=True) as w:
            scripted_warn_fn = torch.jit.script(warn_fn)
            scripted_warn_fn()
            frameinfo = inspect.getframeinfo(inspect.currentframe())
            warning = w[0]

            self.assertTrue(re.search('Warning!', str(warning.message)) is not None)

            # Checks the Python features of the warning
            self.assertEqual(frameinfo.lineno - 6, warning.lineno)
            self.assertEqual(len(w), 1)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(torch.float)
    def test_isfinite_isinf_isnan(self, device, dtype):
        vals = (-float('inf'), float('inf'), float('nan'), -1, 0, 1)

        self.compare_with_numpy(torch.isfinite, np.isfinite, vals, device, dtype)
        self.compare_with_numpy(torch.isinf, np.isinf, vals, device, dtype)
        self.compare_with_numpy(torch.isnan, np.isnan, vals, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(torch.long)
    def test_isfinite_isinf_isnan_int(self, device, dtype):
        vals = (-1, 0, 1)

        self.compare_with_numpy(torch.isfinite, np.isfinite, vals, device, dtype)
        self.compare_with_numpy(torch.isinf, np.isinf, vals, device, dtype)
        self.compare_with_numpy(torch.isnan, np.isnan, vals, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(*(torch.testing.get_all_fp_dtypes()))
    def test_isposinf_isneginf_float(self, device, dtype):
        ops = ((torch.isposinf, np.isposinf), (torch.isneginf, np.isneginf))
        vals = (-float('inf'), float('inf'), float('nan'), -1, 0, 1)

        for torch_op, numpy_op in ops:
            if torch_op == torch.isposinf:
                target_vals = (0, 1, 0, 0, 0, 0)
            else:
                target_vals = (1, 0, 0, 0, 0, 0)

            t = torch.tensor(vals, device=device, dtype=dtype)
            # Manual check here as numpy does not support bfloat16
            if dtype == torch.bfloat16:
                self.assertEqual(torch_op(t),
                                 torch.tensor(target_vals, device=device, dtype=torch.bool))
            else:
                self.compare_with_numpy(torch_op, numpy_op, vals, device, dtype)

            # test the boolean tensor as the `out=` parameter
            out = torch.empty_like(t, dtype=torch.bool)
            t_target = torch.tensor(target_vals, device=device, dtype=torch.bool)
            torch_op(t, out=out)
            self.assertEqual(out, t_target)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(*(torch.testing.get_all_int_dtypes() + [torch.bool]))
    def test_isposinf_isneginf_int_and_bool(self, device, dtype):
        ops = ((torch.isposinf, np.isposinf), (torch.isneginf, np.isneginf))
        vals = (-1, 0, 1)

        for torch_op, numpy_op in ops:
            self.compare_with_numpy(torch_op, numpy_op, vals, device, dtype)

            # test the boolean tensor as the `out=` parameter
            t = torch.tensor(vals, device=device, dtype=dtype)
            out = torch.empty_like(t, dtype=torch.bool)
            t_target = torch.zeros_like(t, dtype=torch.bool)
            torch_op(t, out=out)
            self.assertEqual(out, t_target)

    @dtypes(torch.complex64, torch.complex128)
    def test_isposinf_isneginf_complex(self, device, dtype):
        torch_ops = (torch.isposinf, torch.isneginf)
        vals = (complex(0, float('inf')), complex(1, -float('inf')))
        t = torch.tensor(vals, device=device, dtype=dtype)
        out = torch.empty_like(t)

        for torch_op in torch_ops:
            with self.assertRaisesRegex(RuntimeError, 'does not support complex inputs'):
                torch_op(t)
            with self.assertRaisesRegex(RuntimeError, 'does not support complex inputs'):
                torch_op(t, out=out)

    @dtypes(*(torch.testing.get_all_dtypes(include_bool=False)))
    def test_isposinf_isneginf_non_boolean_output(self, device, dtype):
        # test non-boolean tensors as the `out=` parameters
        # boolean outputs are tested in the above testcases
        vals = (float('inf'), -float('inf'), 1.2)
        t = torch.tensor(vals, device=device)
        for torch_op in (torch.isposinf, torch.isneginf):
            out = torch.empty_like(t, dtype=dtype)
            with self.assertRaisesRegex(RuntimeError, 'does not support non-boolean outputs'):
                torch_op(t, out=out)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(torch.complex64)
    def test_isfinite_isinf_isnan_complex(self, device, dtype):
        vals = (
            complex(-float('inf'), float('inf')),
            complex(-float('inf'), 0),
            complex(0, float('inf')),
            complex(float('inf'), float('nan')),
            complex(float('nan'), 0),
            complex(-1, 0),
            complex(0, 1)
        )

        self.compare_with_numpy(torch.isfinite, np.isfinite, vals, device, dtype)
        self.compare_with_numpy(torch.isinf, np.isinf, vals, device, dtype)
        self.compare_with_numpy(torch.isnan, np.isnan, vals, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(torch.complex64, torch.complex128)
    def test_isreal_complex(self, device, dtype):
        vals = (1, 1 + 1j, 2 + 0j, 3j, 2 - 1j, 2 - 0j)
        self.compare_with_numpy(torch.isreal, np.isreal, vals, device, dtype)

    @dtypes(*torch.testing.get_all_dtypes())
    def test_isreal_noncomplex(self, device, dtype):
        vals = (1, 2, 3)
        # Manual check here since numpy doesn't support bfloat16
        result = torch.isreal(torch.tensor(vals, dtype=dtype))
        expected = torch.ones(result.size(), dtype=torch.bool, device=device)
        self.assertEqual(result, expected)

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @dtypes(torch.complex64)
    def test_isreal_nan_inf(self, device, dtype):
        vals = (
            complex(-float('inf'), float('inf')),
            complex(-float('inf'), 0),
            complex(0, float('inf')),
            complex(float('inf'), float('nan')),
            complex(float('nan'), 0),
            complex(-1, 0),
            complex(0, 1)
        )
        self.compare_with_numpy(torch.isreal, np.isreal, vals, device, dtype)

    @onlyCPU
    def test_isfinite_type(self, device):
        with self.assertRaises(TypeError):
            torch.isfinite(1)  # Parameter must be a tensor

    @onlyCPU
    def test_isinf_type(self, device):
        with self.assertRaises(TypeError):
            torch.isinf(1)  # Parameter must be a tensor

    @onlyCPU
    @dtypes(torch.float)
    def test_diag(self, device, dtype):
        x = torch.rand(100, 100, dtype=dtype, device=device)
        res1 = torch.diag(x)
        res2 = torch.tensor((), dtype=dtype, device=device)
        torch.diag(x, out=res2)
        self.assertEqual(res1, res2)

    def test_diagonal(self, device):
        x = torch.randn((100, 100), device=device)
        result = torch.diagonal(x)
        expected = torch.diag(x)
        self.assertEqual(result, expected)

        x = torch.randn((100, 100), device=device)
        result = torch.diagonal(x, 17)
        expected = torch.diag(x, 17)
        self.assertEqual(result, expected)

    def test_conv_transposed_backward_agnostic_to_memory_format(self, device):
        in_channels = 64
        out_channels = 128
        scale_factor = 8
        batch_size = 8
        length = 16

        conv = torch.nn.ConvTranspose1d(
            in_channels, out_channels, kernel_size=scale_factor * 2, stride=scale_factor).to(device)
        layer_norm = torch.nn.LayerNorm(out_channels).to(device)

        input_ = torch.randn(batch_size, in_channels, length).to(device).contiguous()
        input_ = conv(input_).contiguous()
        input_ = layer_norm(input_.transpose(1, 2).contiguous()).contiguous()
        input_.sum().backward()

    @largeTensorTest('12GB')
    def test_conv_transposed_large(self, device):
        # ConvTranspose3d works for large input tensors (gh-32866)
        in_channels = 64
        out_channels = 128
        kernel_size = 5

        conv = torch.nn.ConvTranspose3d(
            in_channels, out_channels, kernel_size=kernel_size,
            stride=2, padding=2, output_padding=1).to(device)

        x = torch.rand([1, 64, 8, 128, 172]).to(device)
        y = conv(x)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @onlyCPU
    @dtypes(torch.float)
    def test_diagonal_multidim(self, device, dtype):
        x = torch.randn(10, 11, 12, 13, dtype=dtype, device=device)
        xn = x.numpy()
        for args in [(2, 2, 3),
                     (2,),
                     (-2, 1, 2),
                     (0, -2, -1)]:
            result = torch.diagonal(x, *args)
            expected = xn.diagonal(*args)
            self.assertEqual(expected.shape, result.shape)
            self.assertEqual(expected, result)
        # test non-continguous
        xp = x.permute(1, 2, 3, 0)
        result = torch.diagonal(xp, 0, -2, -1)
        expected = xp.numpy().diagonal(0, -2, -1)
        self.assertEqual(expected.shape, result.shape)
        self.assertEqual(expected, result)

    @onlyCPU
    @dtypes(torch.float)
    def test_broadcast_tensors(self, device, dtype):
        x0 = torch.randn(2, 1, 3, dtype=dtype, device=device)
        x1 = torch.randn(3, dtype=dtype, device=device)
        x2 = torch.randn(3, 1, dtype=dtype, device=device)
        expected_size = (2, 3, 3)

        y0, y1, y2 = torch.broadcast_tensors(x0, x1, x2)
        self.assertTrue(y0.size() == expected_size)
        self.assertTrue(y1.size() == expected_size)
        self.assertTrue(y2.size() == expected_size)

    def _do_pow_for_exponents(self, m1, exponents, pow_fn, atol):
        for num in exponents:
            if isinstance(num, int) and num < 0 and not m1.is_floating_point() and not m1.is_complex():
                with self.assertRaisesRegex(RuntimeError,
                                            r'Integers to negative integer powers are not allowed\.'):
                    torch.pow(m1[4], num)
            else:
                # base - tensor, exponent - number
                # contiguous
                res1 = torch.pow(m1[4], num)
                res2 = res1.clone().zero_()
                # `math.pow` has issues with complex exponentiation so we need to resort to normal `pow`.
                for i in range(res2.size(0)):
                    res2[i] = pow_fn(m1[4][i], num)
                rtol = 0 if atol is not None else None
                self.assertEqual(res1, res2, atol=atol, rtol=rtol)

                # non-contiguous
                res1 = torch.pow(m1[:, 4], num)
                res2 = res1.clone().zero_()
                for i in range(res2.size(0)):
                    res2[i] = pow_fn(m1[i, 4], num)
                self.assertEqual(res1, res2, atol=atol, rtol=rtol)

                # scalar ** tensor to enforce correct handling of dtypes for __rpow__().
                expected_dtype = torch.result_type(num, m1)
                res1 = num ** m1[4]
                res2 = torch.tensor(num, dtype=expected_dtype, device=m1.device) ** m1[4]
                self.assertEqual(res1, res2)
                self.assertEqual(res1.dtype, expected_dtype)

    def test_pow(self, device):
        # [res] torch.pow([res,] x)

        # pow has dedicated implementation for different exponents
        for dtype in torch.testing.get_all_math_dtypes(device):

            # This test won't work on torch.half because math.pow will generate a much more accurate result. We skip it
            # for now.
            if dtype == torch.half:
                continue

            # deferring to https://github.com/pytorch/pytorch/pull/36793
            if dtype.is_complex:
                continue

            m1 = torch.empty(0, dtype=dtype, device=device)
            if m1.is_floating_point() or m1.is_complex():
                m1 = torch.rand(100, 100, dtype=dtype, device=device) + 0.5
            else:
                # math.pow will overflow and throw exceptions for large integers
                range_high = 4 if dtype in (torch.int8, torch.uint8) else 10
                m1 = torch.randint(1, range_high, (100, 100), dtype=dtype, device=device)

            exponents = [-2.8, -2, -1, -0.5, 0, 0.5, 1, 2, 3, 4, 3.3]
            complex_exponents = [-2.5j, -1.0j, 0j, 1.0j, 2.5j, 1.0 + 1.0j, -1.0 - 1.5j, 3.3j]
            if m1.is_complex():
                self._do_pow_for_exponents(m1, exponents + complex_exponents, pow, 10e-4)
            else:
                self._do_pow_for_exponents(m1, exponents, math.pow, None)
                self._do_pow_for_exponents(m1, complex_exponents, pow, 10e-4)

            # base - number, exponent - tensor
            # contiguous
            res1 = torch.pow(3, m1[4])
            res2 = res1.clone().zero_()
            for i in range(res2.size(0)):
                res2[i] = math.pow(3, m1[4, i])
            self.assertEqual(res1, res2)

            # non-contiguous
            res1 = torch.pow(3, m1[:, 4])
            res2 = res1.clone().zero_()
            for i in range(res2.size(0)):
                res2[i] = math.pow(3, m1[i][4])
            self.assertEqual(res1, res2)

            # resize behavior for exp == 1
            out = torch.zeros(1, dtype=dtype, device=device)
            torch.pow(m1, 1, out=out)
            self.assertEqual(out, m1)

    @skipCUDAIf(
        _get_torch_cuda_version() < [10, 0] and not TEST_MAGMA,
        "On cuda 9.2, torch.inverse relies on magma"
    )
    @skipCPUIfNoLapack
    def test_inverse(self, device):
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        def test_inverse_helper(matrix, batches, n):
            identity = torch.eye(n, dtype=torch.float64, device=device)

            # correctness test, check matrix*matrix_inverse == identity
            matrix_inverse = torch.inverse(matrix)

            self.assertEqual(identity.expand_as(matrix), torch.matmul(matrix, matrix_inverse), atol=1e-8, rtol=0)
            self.assertEqual(identity.expand_as(matrix), torch.matmul(matrix_inverse, matrix), atol=1e-8, rtol=0)

            # torch.inverse with out and batches
            matrix_inverse_out = torch.empty(*batches, n, n, dtype=torch.float64, device=device)
            torch.inverse(matrix, out=matrix_inverse_out)
            self.assertEqual(matrix_inverse_out, matrix_inverse, atol=0, rtol=0)

            # batched matrices: 3+ dimensional tensors, check matrix_inverse same as single-inverse for each matrix
            if matrix.ndim > 2:
                expected_inv_list = []
                for mat in matrix.contiguous().view(-1, n, n):
                    expected_inv_list.append(torch.inverse(mat))
                expected_inv = torch.stack(expected_inv_list).view(*batches, n, n)
                self.assertEqual(matrix_inverse, expected_inv)

        for batches, n in product(
            [[], [1], [4], [2, 3], [32]],
            [5, 256]
        ):
            # large batch size and large matrix size will be tested in test_inverse_many_batches (slow test)
            if batches and batches[0] == 32 and n == 256:
                continue
            _matrices = random_fullrank_matrix_distinct_singular_value(n, *batches).to(device)
            test_inverse_helper(_matrices, batches, n)
            test_inverse_helper(_matrices.transpose(-2, -1), batches, n)
            test_inverse_helper(
                random_fullrank_matrix_distinct_singular_value(n * 2, *batches).to(device)
                .view(-1, n * 2, n * 2)[:, ::2, ::2].view(*batches, n, n),
                batches, n
            )

        # incorrect input test
        with self.assertRaisesRegex(RuntimeError, "must be batches of square matrices"):
            torch.inverse(torch.randn(2, 3, 4, 3))

        # test for zero-sized tensor
        def test_inverse_helper_zero_size(size):
            data = torch.zeros(*size, device=device)
            out = torch.inverse(data)
            self.assertTrue(out.size() == data.size())

        test_inverse_helper_zero_size([0, 0])
        test_inverse_helper_zero_size([3, 0, 0])
        test_inverse_helper_zero_size([0, 3, 3])

        # non-contiguous inputs
        if not TEST_NUMPY:
            return

        from numpy.linalg import inv
        matrices = random_fullrank_matrix_distinct_singular_value(3, 2).to(device).permute(0, 2, 1)
        assert not matrices.is_contiguous()
        matrices_inverse = torch.inverse(matrices)
        expected_inv = torch.as_tensor(inv(matrices.cpu().numpy()))
        self.assertEqual(matrices_inverse, expected_inv.to(device))

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @onlyOnCPUAndCUDA
    @dtypes(torch.int8, torch.int16, torch.int32, torch.int64)
    def test_signed_shift(self, device, dtype):
        "Ensure that signed integer bit shifting works as expected."
        a = torch.tensor([-10, 10], device=device, dtype=dtype)  # [11...1110110, 1010]
        expected_l = torch.tensor([-40, 40], device=device, dtype=dtype)  # [11...11011000, 101000]
        self.assertEqual(a << 2, expected_l)
        self.compare_with_numpy(lambda x: x << 2, lambda x: np.left_shift(x, 2), a)
        expected_r = torch.tensor([-5, 5], device=device, dtype=dtype)  # [1111...111011, 101]
        self.assertEqual(a >> 1, expected_r)
        self.compare_with_numpy(lambda x: x >> 1, lambda x: np.right_shift(x, 1), a)

    def test_bitwise_not(self, device):
        res = 0xffff - torch.arange(127, dtype=torch.int8, device=device)
        for dtype in (torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64):
            if dtype == torch.bool:
                a = torch.tensor([True, False], device=device)
                expected_res = torch.tensor([False, True], device=device)
            else:
                a = torch.arange(127, dtype=dtype, device=device)
                expected_res = res.to(dtype)
            # new tensor
            self.assertEqual(expected_res, a.bitwise_not())
            # out
            b = torch.empty(0, dtype=dtype, device=device)
            torch.bitwise_not(a, out=b)
            self.assertEqual(expected_res, b)
            # in-place
            a.bitwise_not_()
            self.assertEqual(expected_res, a)

        # test exceptions
        for dtype in (torch.half, torch.float, torch.double):
            a = torch.zeros(10, dtype=dtype, device=device)
            # new tensor
            with self.assertRaises(RuntimeError):
                a.bitwise_not()
            # out
            b = torch.empty(0, dtype=dtype, device=device)
            with self.assertRaises(RuntimeError):
                torch.bitwise_not(a, out=b)
            # in-place
            with self.assertRaises(RuntimeError):
                a.bitwise_not_()

    def test_bitwise_and(self, device):
        for dtype in (torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64):
            a = torch.tensor([1, -2, 3], dtype=dtype, device=device)
            b = torch.tensor([2, 1, 3], dtype=dtype, device=device)
            expected_res = torch.tensor([0, 0, 3], dtype=dtype, device=device)
            b_scalar = 2
            expected_res_scalar = torch.tensor([0, 2, 2], dtype=dtype, device=device)

            # standard version
            self.assertEqual(torch.bitwise_and(a, b), expected_res)
            self.assertEqual(torch.bitwise_and(a, b_scalar), expected_res_scalar)

            # out
            c = torch.empty(0, dtype=dtype, device=device)
            torch.bitwise_and(a, b, out=c)
            self.assertEqual(c, expected_res)
            torch.bitwise_and(a, b_scalar, out=c)
            self.assertEqual(c, expected_res_scalar)

            # in-place
            a1 = a.clone()
            a1.bitwise_and_(b)
            self.assertEqual(a1, expected_res)
            a.bitwise_and_(b_scalar)
            self.assertEqual(a, expected_res_scalar)

        self.assertEqual(torch.tensor([False, True, False], device=device),
                         torch.bitwise_and(torch.tensor([True, True, False], device=device),
                                           torch.tensor([False, True, False], device=device)))

    def test_bitwise_or(self, device):
        for dtype in (torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64):
            a = torch.tensor([1, -2, 3], dtype=dtype, device=device)
            b = torch.tensor([2, 1, 3], dtype=dtype, device=device)
            expected_res = torch.tensor([3, -1, 3], dtype=dtype, device=device)
            b_scalar = 2
            expected_res_scalar = torch.tensor([3, -2, 3], dtype=dtype, device=device)

            # standard version
            self.assertEqual(torch.bitwise_or(a, b), expected_res)
            self.assertEqual(torch.bitwise_or(a, b_scalar), expected_res_scalar)

            # out
            c = torch.empty(0, dtype=dtype, device=device)
            torch.bitwise_or(a, b, out=c)
            self.assertEqual(c, expected_res)
            torch.bitwise_or(a, b_scalar, out=c)
            self.assertEqual(c, expected_res_scalar)

            # in-place
            a1 = a.clone()
            a1.bitwise_or_(b)
            self.assertEqual(a1, expected_res)
            a.bitwise_or_(b_scalar)
            self.assertEqual(a, expected_res_scalar)

        self.assertEqual(torch.tensor([True, True, False], device=device),
                         torch.bitwise_or(torch.tensor([True, True, False], device=device),
                                          torch.tensor([False, True, False], device=device)))

    def test_bitwise_xor(self, device):
        for dtype in (torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64):
            a = torch.tensor([1, -2, 3], dtype=dtype, device=device)
            b = torch.tensor([2, 1, 3], dtype=dtype, device=device)
            expected_res = torch.tensor([3, -1, 0], dtype=dtype, device=device)
            b_scalar = 2
            expected_res_scalar = torch.tensor([3, -4, 1], dtype=dtype, device=device)

            # standard version
            self.assertEqual(torch.bitwise_xor(a, b), expected_res)
            self.assertEqual(torch.bitwise_xor(a, b_scalar), expected_res_scalar)

            # out
            c = torch.empty(0, dtype=dtype, device=device)
            torch.bitwise_xor(a, b, out=c)
            self.assertEqual(c, expected_res)
            torch.bitwise_xor(a, b_scalar, out=c)
            self.assertEqual(c, expected_res_scalar)

            # in-place
            a1 = a.clone()
            a1.bitwise_xor_(b)
            self.assertEqual(a1, expected_res)
            a.bitwise_xor_(b_scalar)
            self.assertEqual(a, expected_res_scalar)

        self.assertEqual(torch.tensor([True, False, False], device=device),
                         torch.bitwise_xor(torch.tensor([True, True, False], device=device),
                                           torch.tensor([False, True, False], device=device)))

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*list(product(torch.testing.get_all_dtypes(include_complex=False),
                          torch.testing.get_all_dtypes(include_complex=False))))
    def test_heaviside(self, device, dtypes):
        input_dtype = dtypes[0]
        values_dtype = dtypes[1]

        rng = np.random.default_rng()
        input = np.array(rng.integers(-10, 10, size=10),
                         dtype=torch_to_numpy_dtype_dict[input_dtype if (input_dtype != torch.bfloat16) else torch.float64])
        input[0] = input[3] = input[7] = 0
        values = np.array(rng.integers(-10, 10, size=10),
                          dtype=torch_to_numpy_dtype_dict[values_dtype if (values_dtype != torch.bfloat16) else torch.float64])
        np_result = torch.from_numpy(np.heaviside(input, values)).to(device=device, dtype=input_dtype)

        input = torch.from_numpy(input).to(device=device, dtype=input_dtype)
        values = torch.from_numpy(values).to(device=device, dtype=values_dtype)
        out = torch.empty_like(input)

        if input_dtype == values_dtype:
            torch_result = torch.heaviside(input, values)
            self.assertEqual(np_result, torch_result)

            torch_result = input.heaviside(values)
            self.assertEqual(np_result, torch_result)

            torch.heaviside(input, values, out=out)
            self.assertEqual(np_result, out)

            input.heaviside_(values)
            self.assertEqual(np_result, input)
        else:
            with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for tensors with different dtypes.'):
                torch.heaviside(input, values)
            with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for tensors with different dtypes.'):
                input.heaviside(values)
            with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for tensors with different dtypes.'):
                torch.heaviside(input, values, out=out)
            with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for tensors with different dtypes.'):
                input.heaviside_(values)


    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*list(product(torch.testing.get_all_complex_dtypes(),
                          torch.testing.get_all_complex_dtypes())))
    def test_heaviside_complex(self, device, dtypes):
        input_dtype = dtypes[0]
        values_dtype = dtypes[1]

        data = (complex(0, -6), complex(-1, 3), complex(1, 1))
        input = torch.tensor(data, device=device, dtype=input_dtype)
        values = torch.tensor(data, device=device, dtype=values_dtype)
        out = torch.empty_like(input)
        real = input.real

        with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for complex tensors.'):
            torch.heaviside(input, real)
        with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for complex tensors.'):
            real.heaviside(values)
        with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for complex tensors.'):
            input.heaviside_(values)
        with self.assertRaisesRegex(RuntimeError, 'heaviside is not yet implemented for complex tensors.'):
            torch.heaviside(real, real, out=out)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @dtypes(*torch.testing.get_all_dtypes())
    def test_logical_not(self, device, dtype):
        data = [10, 1, 0.3, 0, -0.3, -1, -10]
        a = torch.tensor(data, dtype=dtype, device=device)
        if dtype == torch.bfloat16:  # numpy doesn't support these dtypes
            result = [False, False, False, True, False, False, False]
            self.assertEqual(torch.logical_not(a), torch.tensor(result, dtype=torch.bool, device=device))
        else:
            a_np = np.array(data, dtype=torch_to_numpy_dtype_dict[dtype])
            self.assertEqual(np.logical_not(a_np), torch.logical_not(a).to('cpu'))
            self.assertEqual(np.logical_not(a_np, out=a_np), a.logical_not_().to('cpu'))

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @dtypes(*product(torch.testing.get_all_dtypes(),
                     torch.testing.get_all_dtypes()))
    def test_logical_not_out(self, device, dtypes):
        dtype = dtypes[0]
        out_dtype = dtypes[1]
        data = [10, 1, 0.3, 0, -0.3, -1, -10]
        a = torch.tensor(data, dtype=dtype, device=device)
        out = torch.empty_like(a, dtype=out_dtype, device=device)
        if torch.bfloat16 in dtypes:  # numpy doesn't support these dtypes
            result = [not i for i in a]
            self.assertEqual(torch.logical_not(a, out=out), torch.tensor(result, dtype=out_dtype, device=device))
        else:
            out_np = np.empty(a.shape, dtype=torch_to_numpy_dtype_dict[out_dtype])
            self.assertEqual(a, a.cpu().numpy())
            torch.logical_not(a, out=out)
            np.logical_not(a.cpu().numpy(), out=out_np)
            self.assertEqual(out_np, out.to('cpu'))

    def _test_logical(self, device, dtypes, op, a_, b_, expected_res_):
        expected_res = torch.tensor(expected_res_, dtype=dtypes[0], device=device)
        a = torch.tensor(a_, dtype=dtypes[0], device=device)
        b = torch.tensor(b_, dtype=dtypes[1], device=device)

        # new tensor
        self.assertEqual(expected_res.bool(), getattr(a, op)(b))
        # out
        c = torch.empty(0, dtype=torch.bool, device=device)
        getattr(torch, op)(a, b, out=c)
        self.assertEqual(expected_res.bool(), c)

        # in-place
        # TODO: remove when different dtypes as operands are supported
        if dtypes[0] != dtypes[1]:
            with self.assertRaises(RuntimeError):
                getattr(a, op + '_')(b)
            return

        getattr(a, op + '_')(b)
        self.assertEqual(expected_res, a)

    @dtypes(*product(torch.testing.get_all_dtypes(), torch.testing.get_all_dtypes()))
    def test_logical_xor(self, device, dtypes):
        self._test_logical(device, dtypes, 'logical_xor', [10, 0, 1, 0], [1, 0, 0, 10], [0, 0, 1, 1])

    @dtypes(*product(torch.testing.get_all_dtypes(), torch.testing.get_all_dtypes()))
    def test_logical_and(self, device, dtypes):
        self._test_logical(device, dtypes, 'logical_and', [10, 0, 1, 0], [1, 0, 0, 10], [1, 0, 0, 0])

    @dtypes(*product(torch.testing.get_all_dtypes(), torch.testing.get_all_dtypes()))
    def test_logical_or(self, device, dtypes):
        self._test_logical(device, dtypes, 'logical_or', [10, 0, 1, 0], [1, 0, 0, 10], [1, 0, 1, 1])

    # Tests clamp and its alias, clip
    def test_clamp(self, device):
        op_list = ((torch.clamp, torch.Tensor.clamp, torch.Tensor.clamp_),
                   (torch.clip, torch.Tensor.clip, torch.Tensor.clip_))
        for op, method_op, inplace_op in op_list:

            m1 = torch.rand(100, device=device).mul(5).add(-2.5)  # uniform in [-2.5, 2.5]
            # just in case we're extremely lucky.
            min_val = -1
            max_val = 1
            m1[1] = min_val
            m1[2] = max_val

            res1 = m1.clone()
            inplace_op(res1, min_val, max_val)
            res2 = m1.clone()
            for i in iter_indices(res2):
                res2[i] = max(min_val, min(max_val, res2[i]))
            self.assertEqual(res1, res2)

            out = m1.clone()
            op(m1, min=min_val, max=max_val, out=out)
            self.assertEqual(out, res1)

            res1 = op(m1, min=min_val)
            res2 = m1.clone()
            for i in iter_indices(res2):
                res2[i] = max(min_val, res2[i])
            self.assertEqual(res1, res2)

            op(m1, min=min_val, out=out)
            self.assertEqual(out, res1)

            res1 = op(m1, max=max_val)
            res2 = m1.clone()
            for i in iter_indices(res2):
                res2[i] = min(max_val, res2[i])
            self.assertEqual(res1, res2)

            op(m1, max=max_val, out=out)
            self.assertEqual(out, res1)

            # if the tensor contains nan case
            test_tens = torch.tensor([nan], device=device)

            res1 = test_tens.clone()
            inplace_op(res1, min_val, max_val)
            res2 = test_tens.clone()
            for i in iter_indices(res2):
                res2[i] = max(min(res2[i], max_val), min_val)
            self.assertEqual(torch.isnan(res1), torch.isnan(res2))

            out = test_tens.clone()
            op(test_tens, min=min_val, max=max_val, out=out)
            self.assertEqual(torch.isnan(out), torch.isnan(res1))

            res1 = op(test_tens, min=min_val)
            res2 = test_tens.clone()
            for i in iter_indices(res2):
                res2[i] = max(res2[i], min_val)
            self.assertEqual(torch.isnan(res1), torch.isnan(res2))

            op(test_tens, min=min_val, out=out)
            self.assertEqual(torch.isnan(out), torch.isnan(res1))

            res1 = op(test_tens, max=max_val)
            res2 = test_tens.clone()
            for i in iter_indices(res2):
                res2[i] = min(res2[i], max_val)
            self.assertEqual(torch.isnan(res1), torch.isnan(res2))

            op(test_tens, max=max_val, out=out)
            self.assertEqual(torch.isnan(out), torch.isnan(res1))

            error_msg = 'At least one of \'min\' or \'max\' must not be None'
            with self.assertRaisesRegex(RuntimeError, error_msg):
                method_op(m1)
            with self.assertRaisesRegex(RuntimeError, error_msg):
                inplace_op(m1)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    def test_torch_complex(self, device, dtype):
        real = torch.tensor([1, 2], device=device, dtype=dtype)
        imag = torch.tensor([3, 4], device=device, dtype=dtype)
        z = torch.complex(real, imag)
        complex_dtype = torch.complex64 if dtype == torch.float32 else torch.complex128
        self.assertEqual(torch.tensor([1.0 + 3.0j, 2.0 + 4.0j], dtype=complex_dtype), z)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    def test_torch_polar(self, device, dtype):
        abs = torch.tensor([1, 2, -3, -4.5, 1, 1], device=device, dtype=dtype)
        angle = torch.tensor([math.pi / 2, 5 * math.pi / 4, 0, -11 * math.pi / 6, math.pi, -math.pi],
                             device=device, dtype=dtype)
        z = torch.polar(abs, angle)
        complex_dtype = torch.complex64 if dtype == torch.float32 else torch.complex128
        self.assertEqual(torch.tensor([1j, -1.41421356237 - 1.41421356237j, -3,
                                       -3.89711431703 - 2.25j, -1, -1],
                                      dtype=complex_dtype),
                         z, atol=1e-5, rtol=1e-5)

    @onlyOnCPUAndCUDA
    @dtypes(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64,
            torch.float16, torch.complex64, torch.complex128, torch.bool)
    def test_torch_complex_floating_dtype_error(self, device, dtype):
        for op in (torch.complex, torch.polar):
            a = torch.tensor([1, 2], device=device, dtype=dtype)
            b = torch.tensor([3, 4], device=device, dtype=dtype)
            error = r"Expected both inputs to be Float or Double tensors but " \
                    r"got [A-Za-z]+ and [A-Za-z]+"
        with self.assertRaisesRegex(RuntimeError, error):
            op(a, b)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    def test_torch_complex_same_dtype_error(self, device, dtype):

        def dtype_name(dtype):
            return 'Float' if dtype == torch.float32 else 'Double'

        for op in (torch.complex, torch.polar):
            other_dtype = torch.float64 if dtype == torch.float32 else torch.float32
            a = torch.tensor([1, 2], device=device, dtype=dtype)
            b = torch.tensor([3, 4], device=device, dtype=other_dtype)
            error = "Expected object of scalar type {} but got scalar type " \
                    "{} for second argument".format(dtype_name(dtype),
                                                    dtype_name(other_dtype))
            with self.assertRaisesRegex(RuntimeError, error):
                op(a, b)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    def test_torch_complex_out_dtype_error(self, device, dtype):

        def dtype_name(dtype):
            return 'Float' if dtype == torch.float32 else 'Double'

        def complex_dtype_name(dtype):
            return 'ComplexFloat' if dtype == torch.complex64 else 'ComplexDouble'

        for op in (torch.complex, torch.polar):
            a = torch.tensor([1, 2], device=device, dtype=dtype)
            b = torch.tensor([3, 4], device=device, dtype=dtype)
            out = torch.zeros(2, device=device, dtype=dtype)
            expected_dtype = torch.complex64 if dtype == torch.float32 else torch.complex128
            error = "Expected object of scalar type {} but got scalar type " \
                    "{} for argument 'out'".format(
                        complex_dtype_name(expected_dtype), dtype_name(dtype))
            with self.assertRaisesRegex(RuntimeError, error):
                op(a, b, out=out)

    def test_cat_empty_legacy(self, device):
        # FIXME: this is legacy behavior and should be removed
        # when we support empty tensors with arbitrary sizes
        dtype = torch.float32

        x = torch.randn((4, 3, 32, 32), dtype=dtype, device=device)
        empty = torch.randn((0,), dtype=dtype, device=device)

        res1 = torch.cat([x, empty], dim=1)
        res2 = torch.cat([empty, x], dim=1)
        self.assertEqual(res1, res2)

        res1 = torch.cat([empty, empty], dim=1)
        self.assertEqual(res1, empty)

        with self.assertRaisesRegex(RuntimeError,
                                    'non-empty list of Tensors'):
            torch.cat([], dim=1)

    def test_cat_empty(self, device):
        dtype = torch.float32

        x = torch.randn((4, 3, 32, 32), dtype=dtype, device=device)
        empty = torch.randn((4, 0, 32, 32), dtype=dtype, device=device)

        res1 = torch.cat([x, empty], dim=1)
        res2 = torch.cat([empty, x], dim=1)
        self.assertEqual(res1, res2)

        res1 = torch.cat([empty, empty], dim=1)
        self.assertEqual(res1, empty)

        # check non-legacy-behavior (sizes don't match)
        empty = torch.randn((4, 0, 31, 32), dtype=dtype, device=device)
        self.assertRaises(RuntimeError, lambda: torch.cat([x, empty], dim=1))
        self.assertRaises(RuntimeError, lambda: torch.cat([empty, x], dim=1))

        # check non-legacy-behavior (dimensions don't match)
        empty = torch.randn((4, 0), dtype=dtype, device=device)
        self.assertRaises(RuntimeError, lambda: torch.cat([x, empty], dim=1))
        self.assertRaises(RuntimeError, lambda: torch.cat([empty, x], dim=1))

    def test_cat_out(self, device):
        x = torch.zeros((0), device=device)
        y = torch.randn((4, 6), device=device)

        with self.assertRaisesRegex(
                RuntimeError, r"unsupported operation:.* input tensor 0"):
            torch.cat([x, y], dim=0, out=x)

        with self.assertRaisesRegex(
                RuntimeError, r"unsupported operation:.* input tensor 1"):
            torch.cat([x, y], dim=0, out=y)

        z = torch.zeros((4, 6), device=device)
        with self.assertRaisesRegex(
                RuntimeError, r"unsupported operation:.* input tensor 1"):
            torch.cat([y, z], out=z[:2, :])

        w = y.view(-1).clone()
        a = torch.cat([w[:2], w[4:6]])
        b = torch.cat([w[:2], w[4:6]], out=w[6:10])
        self.assertEqual(a, b)
        self.assertEqual(w[:6], y.view(-1)[:6])

    def test_cat_out_channels_last(self, device):
        x = torch.randn((4, 3, 8, 8))
        y = torch.randn(x.shape)
        res1 = torch.cat((x, y))
        z = res1.clone().contiguous(memory_format=torch.channels_last)
        res2 = torch.cat((x, y), out=z)
        self.assertEqual(res1, res2)

    @onlyCPU
    def test_cat_in_channels_last(self, device):
        for dim in range(4):
            x = torch.randn((4, 15, 8, 8), device=device)
            y = torch.randn(x.shape, device=device)
            res1 = torch.cat((x, y), dim=dim)
            x = x.clone().contiguous(memory_format=torch.channels_last)
            y = y.clone().contiguous(memory_format=torch.channels_last)
            res2 = torch.cat((x, y), dim=dim)
            self.assertTrue(res2.is_contiguous(memory_format=torch.channels_last))
            self.assertEqual(res1, res2)

            # Size larger than grain size.
            x = torch.randn((4, 15, 256, 256), device=device)
            y = torch.randn(x.shape, device=device)
            res1 = torch.cat((x, y), dim=dim)
            x = x.clone().contiguous(memory_format=torch.channels_last)
            y = y.clone().contiguous(memory_format=torch.channels_last)
            res2 = torch.cat((x, y), dim=dim)
            self.assertTrue(res2.is_contiguous(memory_format=torch.channels_last))
            self.assertEqual(res1, res2)

    @onlyCUDA
    def test_cat_preserve_channels_last(self, device):
        x = torch.randn((4, 3, 8, 8), device=device)
        y = torch.randn(x.shape, device=device)
        res1 = torch.cat((x, y))
        res2 = torch.cat((x.contiguous(memory_format=torch.channels_last), y.contiguous(memory_format=torch.channels_last)))
        self.assertEqual(res1, res2)
        self.assertTrue(res2.is_contiguous(memory_format=torch.channels_last))

    @onlyCUDA
    @deviceCountAtLeast(2)
    def test_cat_different_devices(self, devices):
        cuda0 = torch.randn((3, 3), device=devices[0])
        cuda1 = torch.randn((3, 3), device=devices[1])
        with self.assertRaisesRegex(RuntimeError,
                                    "input tensors must be on the same device"):
            torch.cat((cuda0, cuda1))
        cpu = torch.randn(3, 3)
        with self.assertRaisesRegex(RuntimeError,
                                    "input tensors must be on the same device"):
            torch.cat((cuda0, cpu))
        with self.assertRaisesRegex(RuntimeError,
                                    "input tensors must be on the same device"):
            torch.cat((cpu, cuda0))

    def test_block_diag(self, device):
        def block_diag_workaround(*arrs):
            arrs_expanded = []
            for a in arrs:
                if a.dim() == 2:
                    arrs_expanded.append(a)
                elif a.dim() == 1:
                    arrs_expanded.append(a.expand(1, a.size(0)))
                elif a.dim() == 0:
                    arrs_expanded.append(a.expand(1, 1))
            shapes = torch.tensor([a.shape for a in arrs_expanded], device=device)
            out = torch.zeros(
                torch.sum(shapes, dim=0).tolist(),
                dtype=arrs_expanded[0].dtype,
                device=device
            )
            r, c = 0, 0
            for i, (rr, cc) in enumerate(shapes):
                out[r:r + rr, c:c + cc] = arrs_expanded[i]
                r += rr
                c += cc
            return out

        tensors = [
            torch.rand((2, 2), device=device),
            torch.rand((2, 3), device=device),
            torch.rand(10, device=device),
            torch.rand((8, 1), device=device),
            torch.rand(1, device=device)[0]
        ]
        result = torch.block_diag(*tensors)
        result_check = block_diag_workaround(*tensors)
        self.assertEqual(result, result_check)

        tensor = torch.rand(1, device=device)[0]
        result = torch.block_diag(tensor)
        result_check = tensor.expand(1, 1)
        self.assertEqual(result, result_check)

        tensor = torch.rand(10, device=device)
        result = torch.block_diag(tensor)
        result_check = tensor.expand(1, tensor.size(0))
        self.assertEqual(result, result_check)

        result = torch.block_diag()
        result_check = torch.empty(1, 0, device=device)
        self.assertEqual(result, result_check)
        self.assertEqual(result.device.type, 'cpu')

        test_dtypes = [
            torch.uint8,
            torch.int8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.float32,
            torch.float64,
            torch.complex64,
            torch.complex128
        ]
        # Test pairs of different dtypes
        for dtype1 in test_dtypes:
            for dtype2 in test_dtypes:
                a = torch.tensor(1, device=device, dtype=dtype1)
                b = torch.tensor(2, device=device, dtype=dtype2)
                result = torch.block_diag(a, b)
                result_dtype = torch.result_type(a, b)
                result_check = torch.tensor([[1, 0], [0, 2]], device=device, dtype=result_dtype)
                self.assertEqual(result, result_check)

        with self.assertRaisesRegex(
            RuntimeError,
            "torch.block_diag: Input tensors must have 2 or fewer dimensions. Input 1 has 3 dimensions"
        ):
            torch.block_diag(torch.tensor(5), torch.tensor([[[6]]]))

        with self.assertRaisesRegex(
            RuntimeError,
            "torch.block_diag: Input tensors must have 2 or fewer dimensions. Input 0 has 4 dimensions"
        ):
            torch.block_diag(torch.tensor([[[[6]]]]))

        if device != 'cpu':
            with self.assertRaisesRegex(
                RuntimeError,
                (
                    "torch.block_diag: input tensors must all be on the same device."
                    " Input 0 is on device cpu and input 1 is on device "
                )
            ):
                torch.block_diag(torch.ones(2, 2).cpu(), torch.ones(2, 2, device=device))

    @unittest.skipIf(not TEST_SCIPY, "Scipy not found")
    def test_block_diag_scipy(self, device):
        import scipy.linalg
        scipy_tensors_list = [
            [
                1,
                [2],
                [],
                [3, 4, 5],
                [[], []],
                [[6], [7.3]]
            ],
            [
                [[1, 2], [3, 4]],
                [1]
            ],
            [
                [[4, 9], [7, 10]],
                [4.6, 9.12],
                [1j + 3]
            ],
            []
        ]

        expected_torch_types = [
            torch.float32,
            torch.int64,
            torch.complex64,
            torch.float32
        ]

        expected_scipy_types = [
            torch.float64,
            # windows scipy block_diag returns int32 types
            torch.int32 if IS_WINDOWS else torch.int64,
            torch.complex128,
            torch.float64
        ]

        for scipy_tensors, torch_type, scipy_type in zip(scipy_tensors_list, expected_torch_types, expected_scipy_types):
            torch_tensors = [torch.tensor(t, device=device) for t in scipy_tensors]
            torch_result = torch.block_diag(*torch_tensors)
            self.assertEqual(torch_result.dtype, torch_type)

            scipy_result = torch.tensor(
                scipy.linalg.block_diag(*scipy_tensors),
                device=device
            )
            self.assertEqual(scipy_result.dtype, scipy_type)
            scipy_result = scipy_result.to(torch_type)

            self.assertEqual(torch_result, scipy_result)

    def test_is_set_to(self, device):
        t1 = torch.empty(3, 4, 9, 10, device=device)
        t2 = torch.empty(3, 4, 9, 10, device=device)
        t3 = torch.tensor([], device=device).set_(t1)
        t4 = t3.clone().resize_(12, 90)
        self.assertFalse(t1.is_set_to(t2))
        self.assertTrue(t1.is_set_to(t3))
        self.assertTrue(t3.is_set_to(t1), "is_set_to should be symmetric")
        self.assertFalse(t1.is_set_to(t4))
        self.assertFalse(torch.Tensor().is_set_to(torch.Tensor()),
                         "Tensors with no storages should not appear to be set "
                         "to each other")

        t1 = torch.tensor([True, True], dtype=torch.bool, device=device)
        t2 = torch.tensor([0], dtype=torch.bool, device=device).set_(t1)
        self.assertTrue(t1.is_set_to(t2))

        # test that sizes must match
        t1 = torch.empty([2, 3, 4], device=device)
        t2 = t1.view(4, 3, 2)
        self.assertFalse(t1.is_set_to(t2))
        self.assertFalse(t2.is_set_to(t1))

        # test that legacy empty size behavior used to be respected (i.e. all
        # empty tensors were logically collapsed to size [0]).
        t1 = torch.empty([2, 5, 0], device=device)
        t2 = t1.view([0])
        self.assertFalse(t1.is_set_to(t2))
        self.assertFalse(t2.is_set_to(t1))

    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_inverse_many_batches(self, device):
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        def test_inverse_many_batches_helper(b, n):
            matrices = random_fullrank_matrix_distinct_singular_value(b, n, n).to(device)
            matrices_inverse = torch.inverse(matrices)
            self.assertEqual(torch.matmul(matrices_inverse, matrices),
                             torch.eye(b, dtype=torch.float64, device=device).expand_as(matrices))

        test_inverse_many_batches_helper(5, 256)
        test_inverse_many_batches_helper(3, 512)
        test_inverse_many_batches_helper(64, 64)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_pinverse(self, device, dtype):
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value as fullrank

        def run_test(M):
            # Testing against definition for pseudo-inverses
            MPI = torch.pinverse(M)
            if M.numel() > 0:
                self.assertEqual(M, M.matmul(MPI).matmul(M), atol=1e-8, rtol=0, msg='pseudo-inverse condition 1')
                self.assertEqual(MPI, MPI.matmul(M).matmul(MPI), atol=1e-8, rtol=0, msg='pseudo-inverse condition 2')
                self.assertEqual(M.matmul(MPI), (M.matmul(MPI)).transpose(-2, -1),
                                 atol=1e-8, rtol=0, msg='pseudo-inverse condition 3')
                self.assertEqual(MPI.matmul(M), (MPI.matmul(M)).transpose(-2, -1),
                                 atol=1e-8, rtol=0, msg='pseudo-inverse condition 4')
            else:
                self.assertEqual(M.shape, MPI.shape[:-2] + (MPI.shape[-1], MPI.shape[-2]))
        for sizes in [(5, 5), (3, 5, 5), (3, 7, 5, 5),  # square matrices
                      (3, 2), (5, 3, 2), (7, 5, 3, 2),  # fat matrices
                      (2, 3), (5, 2, 3), (7, 5, 2, 3),  # thin matrices
                      (0, 0), (0, 2), (2, 0), (3, 0, 0), (0, 3, 0), (0, 0, 3)]:  # zero numel matrices
            M = torch.randn(*sizes, dtype=dtype, device=device)
            run_test(M)

        # Test inverse and pseudo-inverse for invertible matrix
        for sizes in [(5, 5), (3, 5, 5), (3, 7, 5, 5)]:
            matsize = sizes[-1]
            batchdims = sizes[:-2]
            M = fullrank(matsize, *batchdims, dtype=dtype, device=device)
            self.assertEqual(torch.eye(matsize, dtype=dtype, device=device).expand(sizes), M.pinverse().matmul(M),
                             atol=1e-7, rtol=0, msg='pseudo-inverse for invertible matrix')

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_matrix_rank(self, device):
        a = torch.eye(10, device=device)
        self.assertEqual(torch.matrix_rank(a).item(), 10)
        self.assertEqual(torch.matrix_rank(a, True).item(), 10)

        a[5, 5] = 0
        self.assertEqual(torch.matrix_rank(a).item(), 9)
        self.assertEqual(torch.matrix_rank(a, True).item(), 9)

        a = torch.randn(24, 42, device=device)
        self.assertEqual(torch.matrix_rank(a), torch.matrix_rank(a.t()))
        aaT = torch.mm(a, a.t())
        self.assertEqual(torch.matrix_rank(aaT), torch.matrix_rank(aaT, True))
        aTa = torch.mm(a.t(), a)
        self.assertEqual(torch.matrix_rank(aTa), torch.matrix_rank(aTa, True))

        if TEST_NUMPY:
            from numpy.linalg import matrix_rank
            a = torch.randn(35, 75, device=device)
            self.assertEqual(torch.matrix_rank(a).item(), matrix_rank(a.cpu().numpy()))
            self.assertEqual(torch.matrix_rank(a, 0.01).item(), matrix_rank(a.cpu().numpy(), 0.01))

            aaT = torch.mm(a, a.t())
            self.assertEqual(torch.matrix_rank(aaT).item(), matrix_rank(aaT.cpu().numpy()))
            self.assertEqual(torch.matrix_rank(aaT, 0.01).item(), matrix_rank(aaT.cpu().numpy(), 0.01))

            if np.lib.NumpyVersion(np.__version__) >= '1.14.0':
                self.assertEqual(torch.matrix_rank(aaT, True).item(), matrix_rank(aaT.cpu().numpy(), True))
                self.assertEqual(torch.matrix_rank(aaT, 0.01, True).item(),
                                 matrix_rank(aaT.cpu().numpy(), 0.01, True))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_matrix_power(self, device, dtype):
        def run_test(M, sign=1):
            if sign == -1:
                M = M.inverse()
            MP2 = torch.matrix_power(M, 2)
            self.assertEqual(MP2, torch.matmul(M, M))

            MP3 = torch.matrix_power(M, 3)
            self.assertEqual(MP3, torch.matmul(MP2, M))

            MP4 = torch.matrix_power(M, 4)
            self.assertEqual(MP4, torch.matmul(MP2, MP2))

            MP6 = torch.matrix_power(M, 6)
            self.assertEqual(MP6, torch.matmul(MP3, MP3))

            MP0 = torch.matrix_power(M, 0)
            self.assertEqual(MP0, torch.eye(M.size(-2), dtype=dtype).expand_as(M))

        # Single matrix
        M = torch.randn(5, 5, dtype=dtype, device=device)
        run_test(M)

        # Batch matrices
        M = torch.randn(3, 3, 3, dtype=dtype, device=device)
        run_test(M)

        # Many batch matrices
        M = torch.randn(2, 3, 3, 3, dtype=dtype, device=device)
        run_test(M)

        # This is for negative powers
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value
        M = random_fullrank_matrix_distinct_singular_value(5, dtype=dtype, device=device)
        run_test(M, sign=-1)

        M = random_fullrank_matrix_distinct_singular_value(3, 3, dtype=dtype, device=device)
        run_test(M, sign=-1)

        M = random_fullrank_matrix_distinct_singular_value(3, 2, 3, dtype=dtype, device=device)
        run_test(M, sign=-1)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float, torch.complex64)
    def test_matrix_exp_utils(self, device, dtype):
        # test linear combination
        def run_test(coeff_shape, data_shape):
            coeffs = torch.rand(*coeff_shape, device=device, dtype=torch.float)
            x = torch.rand(coeff_shape[1], *data_shape, device=device, dtype=dtype)

            res1 = torch._compute_linear_combination(x, coeffs)
            res2 = (x.unsqueeze(0) * coeffs.view(*coeff_shape, *([1] * len(data_shape)))).sum(1)
            self.assertEqual(res1, res2, atol=1e-5, rtol=0.0)

            # check `out=` version
            res3 = torch.zeros(coeff_shape[0], *data_shape, device=device, dtype=dtype)
            torch._compute_linear_combination(x, coeffs, out=res3)
            self.assertEqual(res1, res3, atol=1e-5, rtol=0.0)

            res4 = torch.ones(coeff_shape[0], *data_shape, device=device, dtype=dtype)
            torch._compute_linear_combination(x, coeffs, out=res4)
            self.assertEqual(res1, res4 - 1.0, atol=1e-5, rtol=0.0)

            res5 = torch.ones(coeff_shape[0], *data_shape, device=device, dtype=dtype)
            res5_clone = res5.clone()
            torch._compute_linear_combination(x, coeffs, out=res5)
            self.assertEqual(res1, res5 - res5_clone, atol=1e-5, rtol=0.0)

        run_test([1, 3], [2, 2])
        run_test([3, 1], [2, 2])
        run_test([1, 10], [10, 10])
        run_test([10, 1], [10, 10])
        run_test([5, 3], [2, 2])
        run_test([5, 3], [100, 100])
        run_test([3, 4], [3, 3, 3])
        run_test([3, 4], [3, 3, 3, 3])

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float, torch.double, torch.complex64, torch.complex128)
    def test_matrix_exp_boundary_cases(self, device, dtype):

        with self.assertRaisesRegex(RuntimeError, "expected a tensor of floating or complex types"):
            torch.randn(3, 3).type(torch.int).matrix_exp()

        with self.assertRaisesRegex(RuntimeError, "with dim at least 2"):
            torch.randn(3).matrix_exp()

        with self.assertRaisesRegex(RuntimeError, "expected a tensor of squared matrices"):
            torch.randn(3, 2, 1).matrix_exp()

        # check 1x1 matrices
        x = torch.randn(3, 3, 1, 1)
        mexp = x.matrix_exp()
        self.assertEqual(mexp, x.exp())

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float, torch.double)
    # Although tf32 is always disabled on matrix_exp, this test uses matmul,
    # which has tf32 on by default
    @with_tf32_off
    def test_matrix_exp_analytic(self, device, dtype):
        # check zero matrix
        x = torch.zeros(20, 20, dtype=dtype, device=device)
        self.assertTrue((x.matrix_exp() == torch.eye(20, 20, dtype=dtype, device=device)).all().item())

        def normalize_to_1_operator_norm(sample, desired_norm):
            sample_norm, _ = sample.abs().sum(-2).max(-1)
            sample_to_1_norm = sample / sample_norm.unsqueeze(-1).unsqueeze(-1)
            return sample_to_1_norm * desired_norm

        def gen_good_cond_number_matrices(*n):
            """
            Generates a diagonally-domimant matrix
            with the eigenvalues centered at 1
            and the radii at most (n[-1] - 1) / (n[-2] ** 2)
            """
            identity = torch.eye(n[-2], n[-1], dtype=dtype, device=device).expand(*n)
            x = torch.rand(*n, dtype=dtype, device=device) / (n[-1] ** 2)
            x = (x - x * identity) + identity
            return x

        def run_test(*n):
            if dtype == torch.float:
                thetas = [
                    1.192092800768788e-07,  # deg 1
                    5.978858893805233e-04,  # deg 2
                    5.116619363445086e-02,  # deg 4
                    5.800524627688768e-01,  # deg 8
                    1.461661507209034e+00,  # deg 12
                    3.010066362817634e+00   # deg 18
                ]
            else:  # if torch.double
                thetas = [
                    2.220446049250313e-16,  # deg 1
                    2.580956802971767e-08,  # deg 2
                    3.397168839976962e-04,  # deg 4
                    4.991228871115323e-02,  # deg 8
                    2.996158913811580e-01,  # deg 12
                    1.090863719290036e+00   # deg 18
                ]

            # generate input
            q = gen_good_cond_number_matrices(*n)
            qinv = torch.inverse(q)
            d = torch.randn(n[:-1], dtype=dtype, device=device)
            x = torch.matmul(q, torch.matmul(torch.diag_embed(d), qinv))
            x_norm, _ = x.abs().sum(-2).max(-1)

            # test simple analytic whatever norm generated
            mexp = x.matrix_exp()
            mexp_analytic = torch.matmul(
                q,
                torch.matmul(
                    torch.diag_embed(d.exp()),
                    qinv
                )
            )
            self.assertEqual(mexp, mexp_analytic, atol=1e-3, rtol=0.0)

            # generate norms to test different degree expansions
            sample_norms = []
            for i in range(len(thetas) - 1):
                sample_norms.append(0.5 * (thetas[i] + thetas[i + 1]))
            sample_norms = [thetas[0] / 2] + sample_norms + [thetas[-1] * 2]

            # matrices to equal norm
            for sample_norm in sample_norms:
                x_normalized = normalize_to_1_operator_norm(x, sample_norm)

                mexp = x_normalized.matrix_exp()
                mexp_analytic = torch.matmul(
                    q,
                    torch.matmul(
                        torch.diag_embed((d / x_norm.unsqueeze(-1) * sample_norm).exp()),
                        qinv
                    )
                )
                self.assertEqual(mexp, mexp_analytic, atol=1e-3, rtol=0.0)

        # single matrix
        run_test(2, 2)
        run_test(3, 3)
        run_test(4, 4)
        run_test(5, 5)
        run_test(100, 100)
        run_test(200, 200)

        # small batch of matrices
        run_test(3, 2, 2)
        run_test(3, 3, 3)
        run_test(3, 4, 4)
        run_test(3, 5, 5)
        run_test(3, 100, 100)
        run_test(3, 200, 200)

        # large batch of matrices
        run_test(3, 3, 2, 2)
        run_test(3, 3, 3, 3)
        run_test(3, 3, 4, 4)
        run_test(3, 3, 5, 5)
        run_test(3, 3, 100, 100)
        run_test(3, 3, 200, 200)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float, torch.double)
    def test_matrix_exp_batch(self, device, dtype):

        def run_test(*n):
            tensors_batch = torch.zeros(n, dtype=dtype, device=device)
            tensors_batch = tensors_batch.view(-1, n[-2], n[-1])

            num_matrices = tensors_batch.size(0)
            tensors_list = []
            for i in range(num_matrices):
                tensors_list.append(torch.randn(n[-2], n[-1], dtype=dtype, device=device))

            for i in range(num_matrices):
                tensors_batch[i, ...] = tensors_list[i]

            tensors_exp_map = map(lambda x: x.matrix_exp(), tensors_list)
            tensors_exp_batch = tensors_batch.matrix_exp()

            for i, tensor_exp in enumerate(tensors_exp_map):
                self.assertEqual(tensors_exp_batch[i, ...], tensor_exp)

        # small batch of matrices
        run_test(3, 2, 2)
        run_test(3, 3, 3)
        run_test(3, 4, 4)
        run_test(3, 5, 5)

        # large batch of matrices
        run_test(3, 3, 2, 2)
        run_test(3, 3, 3, 3)
        run_test(3, 3, 4, 4)
        run_test(3, 3, 5, 5)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float, torch.double)
    # Although tf32 is always disabled on matrix_exp, this test uses matmul,
    # which has tf32 on by default
    @with_tf32_off
    def test_matrix_exp_compare_with_taylor(self, device, dtype):

        def normalize_to_1_operator_norm(sample, desired_norm):
            sample_norm, _ = sample.abs().sum(-2).max(-1)
            sample_to_1_norm = sample / sample_norm.unsqueeze(-1).unsqueeze(-1)
            return sample_to_1_norm * desired_norm

        def gen_good_cond_number_matrices(*n):
            """
            Generates a diagonally-domimant matrix
            with the eigenvalues centered at 1
            and the radii at most (n[-1] - 1) / (n[-2] ** 2)
            """
            identity = torch.eye(n[-2], n[-1], dtype=dtype, device=device).expand(*n)
            x = torch.rand(*n, dtype=dtype, device=device) / (n[-1] ** 2)
            x = (x - x * identity) + identity
            return x

        def get_taylor_approximation(a, deg):
            identity = torch.eye(a.size(-2), a.size(-1), dtype=dtype, device=device).expand_as(a)
            res = identity
            taylor_term = identity

            for i in range(1, deg + 1):
                taylor_term = torch.matmul(a, taylor_term) / i
                res = res + taylor_term

            return res

        def scale_square(a, deg):
            if a.norm() < 1.0:
                return get_taylor_approximation(a, 12)
            else:
                s = int(torch.log2(a.norm()).ceil().item())
                b = a / (2 ** s)
                b = get_taylor_approximation(b, 18)
                for _ in range(s):
                    b = torch.matmul(b, b)
                return b

        def run_test(*n):
            degs = [1, 2, 4, 8, 12, 18]
            if dtype == torch.float:
                thetas = [
                    1.192092800768788e-07,  # deg 1
                    5.978858893805233e-04,  # deg 2
                    5.116619363445086e-02,  # deg 4
                    5.800524627688768e-01,  # deg 8
                    1.461661507209034e+00,  # deg 12
                    3.010066362817634e+00   # deg 18
                ]
            else:  # if torch.double
                thetas = [
                    2.220446049250313e-16,  # deg 1
                    2.580956802971767e-08,  # deg 2
                    3.397168839976962e-04,  # deg 4
                    4.991228871115323e-02,  # deg 8
                    2.996158913811580e-01,  # deg 12
                    1.090863719290036e+00   # deg 18
                ]

            # generate norms to test different degree expansions
            sample_norms = []
            for i in range(len(thetas) - 1):
                sample_norms.append(0.5 * (thetas[i] + thetas[i + 1]))
            sample_norms = [thetas[0] / 2] + sample_norms + [thetas[-1] * 2]
            degs = [degs[0]] + degs

            for sample_norm, deg in zip(sample_norms, degs):
                x = gen_good_cond_number_matrices(*n)
                x = normalize_to_1_operator_norm(x, sample_norm)

                mexp = x.matrix_exp()
                mexp_taylor = scale_square(x, deg)

                self.assertEqual(mexp, mexp_taylor, atol=1e-2, rtol=0.0)

        # single matrix
        run_test(2, 2)
        run_test(3, 3)
        run_test(4, 4)
        run_test(5, 5)

        # small batch of matrices
        run_test(3, 2, 2)
        run_test(3, 3, 3)
        run_test(3, 4, 4)
        run_test(3, 5, 5)

        # large batch of matrices
        run_test(3, 3, 2, 2)
        run_test(3, 3, 3, 3)
        run_test(3, 3, 4, 4)
        run_test(3, 3, 5, 5)

    @dtypes(torch.double)
    def test_chain_matmul(self, device, dtype):
        def product(matrices):
            for mat in matrices[1:]:
                matrices[0] = matrices[0].mm(mat)
            return matrices[0]

        def run_test(p):
            matrices = []
            for (pi, pi_1) in zip(p[:-1], p[1:]):
                matrices.append(torch.randn(pi, pi_1, dtype=dtype, device=device))
            self.assertEqual(torch.chain_matmul(*matrices), product(matrices))

        run_test([10, 20, 30, 5])
        run_test([15, 5, 10, 20, 25])

        with self.assertRaisesRegex(RuntimeError, "chain_matmul: Expected one or more matrices"):
            torch.chain_matmul()

    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_det_logdet_slogdet(self, device, dtype):
        def reference_slogdet(M):
            if TEST_NUMPY:
                sdet, logabsdet = np.linalg.slogdet(M.detach().cpu().numpy())
                return M.new_tensor(sdet), M.new_tensor(logabsdet)
            else:
                # naive row reduction
                M = M.clone()
                l = M.size(0)
                multiplier = 1
                for i in range(l):
                    if M[i, 0].item() != 0:
                        if i != 0:
                            M[0], M[i] = M[i], M[0]
                            multiplier = -1
                        break
                else:
                    return 0
                for i in range(1, l):
                    row = M[i]
                    for j in range(i):
                        row -= row[j] / M[j, j] * M[j]
                    M[i] = row
            sdet = M.diag().sign().prod()
            logabsdet = M.diag().abs_().log_().sum().add_(math.log(multiplier))
            return sdet, logabsdet

        def test_single_det(M, target, desc):
            target_sdet, target_logabsdet = target

            det = M.det()
            logdet = M.logdet()
            sdet, logabsdet = M.slogdet()

            # Test det
            self.assertEqual(det, target_sdet * target_logabsdet.exp(),
                             atol=1e-7, rtol=0, msg='{} (det)'.format(desc))

            # Test slogdet
            # Compare the overall value rather than individual parts because of
            # precision issues when det is near zero.
            self.assertEqual(sdet * logabsdet.exp(), target_sdet * target_logabsdet.exp(),
                             atol=1e-7, rtol=0, msg='{} (slogdet)'.format(desc))

            # Test logdet
            # Compare logdet against our own pytorch slogdet because they should
            # be consistent, while it may behave slightly differently with other
            # slogdet implementations when det is near zero due to precision
            # issues.
            if sdet.item() < 0:
                self.assertTrue(logdet.item() != logdet.item(), '{} (logdet negative case)'.format(desc))
            else:
                self.assertEqual(logdet.exp(), target_logabsdet.exp(),
                                 atol=1e-7, rtol=0, msg='{} (logdet non-negative case)'.format(desc))

        eye = torch.eye(5, dtype=dtype, device=device)
        test_single_det(eye, (torch.ones((), dtype=dtype, device=device), torch.zeros((), dtype=dtype, device=device)), 'identity')
        # Testing bug in #34061 (https://github.com/pytorch/pytorch/issues/34061)
        for n in range(250, 551, 100):
            mat = torch.randn(n, n, dtype=dtype, device=device)
            q, _ = torch.qr(mat)
            ref_det, ref_logabsdet = reference_slogdet(q)
            test_single_det(q, (ref_det, ref_logabsdet), 'orthogonal')

        def test(M):
            assert M.size(0) >= 5, 'this helper fn assumes M to be at least 5x5'
            M = M.to(device)

            ref_M_sdet, ref_M_logabsdet = reference_slogdet(M)

            test_single_det(M, (ref_M_sdet, ref_M_logabsdet), 'basic')
            if ref_M_logabsdet.exp().item() >= 1e-6:  # skip singular
                M_inv = M.inverse()
                test_single_det(M_inv, reference_slogdet(M_inv), 'inverse')

            test_single_det(M, (ref_M_sdet, ref_M_logabsdet), 'transpose')

            for x in [0, 2, 4]:
                for scale in [-2, -0.1, 0, 10]:
                    if scale > 0:
                        target = ref_M_sdet, ref_M_logabsdet + math.log(scale)
                    elif scale == 0:
                        target = torch.zeros_like(ref_M_sdet), torch.full_like(ref_M_logabsdet, -inf)
                    else:
                        target = ref_M_sdet.neg(), ref_M_logabsdet + math.log(-scale)

                    # dim 0
                    M_clone = M.clone()
                    M_clone[:, x] *= scale
                    test_single_det(M_clone, target, 'scale a row')
                    # dim 1
                    M_clone = M.clone()
                    M_clone[x, :] *= scale
                    test_single_det(M_clone, target, 'scale a column')

            for x1, x2 in [(0, 3), (4, 1), (3, 2)]:
                assert x1 != x2, 'x1 and x2 needs to be different for this test'
                target = torch.zeros_like(ref_M_sdet), torch.full_like(ref_M_logabsdet, -inf)
                # dim 0
                M_clone = M.clone()
                M_clone[:, x2] = M_clone[:, x1]
                test_single_det(M_clone, target, 'two rows are same')
                # dim 1
                M_clone = M.clone()
                M_clone[x2, :] = M_clone[x1, :]
                test_single_det(M_clone, target, 'two columns are same')

                for scale1, scale2 in [(0.3, -1), (0, 2), (10, 0.1)]:
                    det_scale = scale1 * scale2 * -1
                    if det_scale > 0:
                        target = ref_M_sdet, ref_M_logabsdet + math.log(det_scale)
                    elif det_scale == 0:
                        target = torch.zeros_like(ref_M_sdet), torch.full_like(ref_M_logabsdet, -inf)
                    else:
                        target = ref_M_sdet.neg(), ref_M_logabsdet + math.log(-det_scale)

                    # dim 0
                    M_clone = M.clone()
                    t = M_clone[:, x1] * scale1
                    M_clone[:, x1] += M_clone[:, x2] * scale2
                    M_clone[:, x2] = t
                    test_single_det(M_clone, target, 'exchanging rows')
                    # dim 1
                    M_clone = M.clone()
                    t = M_clone[x1, :] * scale1
                    M_clone[x1, :] += M_clone[x2, :] * scale2
                    M_clone[x2, :] = t
                    test_single_det(M_clone, target, 'exchanging columns')

        def get_random_mat_scale(n):
            # For matrices with values i.i.d. with 0 mean, unit variance, and
            # subexponential tail, we have:
            #   E[log det(A^2)] \approx log((n-1)!)
            #
            # Notice:
            #   log Var[det(A)] = log E[det(A^2)] >= E[log det(A^2)]
            #
            # So:
            #   stddev[det(A)] >= sqrt( (n-1)! )
            #
            # We use this as an intuitive guideline to scale random generated
            # matrices so our closeness tests can work more robustly:
            #   scale by sqrt( (n-1)! )^(-1/n) = ( (n-1)! )^(-1/(2n))
            #
            # source: https://arxiv.org/pdf/1112.0752.pdf

            # TODO: technically we need subexponential distn for this to hold,
            #       but we mostly use gaussian entries below. Consider switching
            #       to Chi-sq if this turns out not stable enough, since Chi-sq
            #       is easy enough to sample from.
            return math.factorial(n - 1) ** (-1.0 / (2 * n))

        for n in [5, 10, 25]:
            scale = get_random_mat_scale(n)
            test(torch.randn(n, n, dtype=dtype, device=device) * scale)
            r = torch.randn(n, n, dtype=dtype, device=device) * scale
            # symmetric psd
            test(r.mm(r.t()))
            # symmetric pd
            r = torch.randn(n, n, dtype=dtype, device=device) * scale
            test(r.mm(r.t()) + torch.eye(n, dtype=dtype, device=device) * 1e-6)
            # symmetric
            r = torch.randn(n, n, dtype=dtype, device=device) * scale
            for i in range(n):
                for j in range(i):
                    r[i, j] = r[j, i]
            test(r)
            # non-contiguous
            test((torch.randn(n, n, n + 1, dtype=dtype, device=device) * scale)[:, 2, 1:])
            # det = 0
            r = torch.randn(n, n, dtype=dtype, device=device) * scale
            u, s, v = r.svd()
            if reference_slogdet(u)[0] < 0:
                u = -u
            if reference_slogdet(v)[0] < 0:
                v = -v
            s[0] *= -1
            s[-1] = 0
            test(u.mm(s.diag()).mm(v))

        # Small values to test numerical stability. Note that we don't scale
        # this matrix.
        r = torch.randn(512, 512, dtype=dtype, device=device)
        u, s, v = r.svd()
        s.fill_(1. / (100 * s.numel()))
        test(u.mm(s.diag()).mm(v))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_det_logdet_slogdet_batched(self, device, dtype):
        from torch.testing._internal.common_utils import (random_symmetric_matrix, random_symmetric_psd_matrix,
                                                          random_symmetric_pd_matrix, random_square_matrix_of_rank)

        # mat_chars denotes matrix characteristics
        # possible values are: sym, sym_psd, sym_pd, sing, non_sym
        def run_test(matsize, batchdims, mat_chars):
            num_matrices = reduce(lambda x, y: x * y, batchdims, 1)
            list_of_matrices = []

            for idx in range(num_matrices):
                mat_type = idx % len(mat_chars)
                if mat_chars[mat_type] == 'sym':
                    list_of_matrices.append(random_symmetric_matrix(matsize, dtype=dtype, device=device))
                elif mat_chars[mat_type] == 'sym_psd':
                    list_of_matrices.append(random_symmetric_psd_matrix(matsize, dtype=dtype, device=device))
                elif mat_chars[mat_type] == 'sym_pd':
                    list_of_matrices.append(random_symmetric_pd_matrix(matsize, dtype=dtype, device=device))
                elif mat_chars[mat_type] == 'sing':
                    list_of_matrices.append(torch.ones(matsize, matsize, dtype=dtype, device=device))
                elif mat_chars[mat_type] == 'non_sing':
                    list_of_matrices.append(random_square_matrix_of_rank(matsize, matsize, dtype=dtype, device=device))
            full_tensor = torch.stack(list_of_matrices, dim=0).reshape(batchdims + (matsize, matsize))
            # Scaling adapted from `get_random_mat_scale` in _test_det_logdet_slogdet
            full_tensor *= (math.factorial(matsize - 1) ** (-1.0 / (2 * matsize)))

            for fn in [torch.det, torch.logdet, torch.slogdet]:
                expected_value = []
                actual_value = fn(full_tensor)
                for full_idx in product(*map(lambda x: list(range(x)), batchdims)):
                    expected_value.append(fn(full_tensor[full_idx]))

                if fn == torch.slogdet:
                    sign_value = torch.stack([tup[0] for tup in expected_value], dim=0).reshape(batchdims)
                    expected_value = torch.stack([tup[1] for tup in expected_value], dim=0).reshape(batchdims)
                    self.assertEqual(sign_value, actual_value[0])
                    self.assertEqual(expected_value, actual_value[1])
                else:
                    expected_value = torch.stack(expected_value, dim=0).reshape(batchdims)
                    self.assertEqual(actual_value, expected_value)

        for matsize, batchdims in product([3, 5], [(3,), (5, 3)]):
            run_test(matsize, batchdims, mat_chars=['sym_pd'])
            run_test(matsize, batchdims, mat_chars=['sing'])
            run_test(matsize, batchdims, mat_chars=['non_sing'])
            run_test(matsize, batchdims, mat_chars=['sym', 'sym_pd', 'sym_psd'])
            run_test(matsize, batchdims, mat_chars=['sing', 'non_sing'])

    def solve_test_helper(self, A_dims, b_dims, device, dtype):
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        b = torch.randn(*b_dims, dtype=dtype, device=device)
        A = random_fullrank_matrix_distinct_singular_value(*A_dims, dtype=dtype, device=device)
        return b, A

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_solve(self, device, dtype):
        for (k, n) in zip([2, 3, 5], [3, 5, 7]):
            b, A = self.solve_test_helper((n,), (n, k), device, dtype)
            x = torch.solve(b, A)[0]
            self.assertLessEqual(b.dist(A.mm(x)), 1e-12)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_solve_batched(self, device, dtype):
        def solve_batch_helper(A_dims, b_dims):
            b, A = self.solve_test_helper(A_dims, b_dims, device, dtype)
            x_exp_list = []
            for i in range(b_dims[0]):
                x_exp_list.append(torch.solve(b[i], A[i])[0])
            x_exp = torch.stack(x_exp_list)  # Stacked output
            x_act = torch.solve(b, A)[0]  # Actual output
            self.assertEqual(x_exp, x_act)  # Equality check
            self.assertLessEqual(b.dist(torch.matmul(A, x_act)), 1e-12)  # Correctness check

        for batchsize in [1, 3, 4]:
            solve_batch_helper((5, batchsize), (batchsize, 5, 10))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_solve_batched_non_contiguous(self, device, dtype):
        from numpy.linalg import solve
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value
        A = random_fullrank_matrix_distinct_singular_value(2, 2, dtype=dtype,
                                                           device=device).permute(1, 0, 2)
        b = torch.randn(2, 2, 2, dtype=dtype, device=device).permute(2, 1, 0)
        x, _ = torch.solve(b, A)
        x_exp = torch.Tensor(solve(A.cpu().numpy(), b.cpu().numpy())).to(dtype=dtype, device=device)
        self.assertEqual(x, x_exp)

    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_solve_batched_many_batches(self, device, dtype):
        b, A = self.solve_test_helper((5, 256, 256), (5, 1), device, dtype)
        x, _ = torch.solve(b, A)
        self.assertEqual(torch.matmul(A, x), b.expand(A.shape[:-2] + (5, 1)))

        b, A = self.solve_test_helper((3,), (512, 512, 3, 1), device, dtype)
        x, _ = torch.solve(b, A)
        self.assertEqual(torch.matmul(A, x), b)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_solve_batched_broadcasting(self, device, dtype):
        from numpy.linalg import solve

        def run_test(A_dims, b_dims):
            A_matrix_size = A_dims[-1]
            A_batch_dims = A_dims[:-2]
            b, A = self.solve_test_helper((A_matrix_size,) + A_batch_dims, b_dims, device, dtype)
            x, _ = torch.solve(b, A)
            x_exp = torch.Tensor(solve(A.cpu().numpy(), b.cpu().numpy())).to(dtype=dtype, device=device)
            self.assertEqual(x, x_exp)

        # test against numpy.linalg.solve
        run_test((2, 1, 3, 4, 4), (2, 1, 3, 4, 6))  # no broadcasting
        run_test((2, 1, 3, 4, 4), (4, 6))  # broadcasting b
        run_test((4, 4), (2, 1, 3, 4, 2))  # broadcasting A
        run_test((1, 3, 1, 4, 4), (2, 1, 3, 4, 5))  # broadcasting A & b

    def cholesky_solve_test_helper(self, A_dims, b_dims, upper, device, dtype):
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix

        b = torch.randn(*b_dims, dtype=dtype, device=device)
        A = random_symmetric_pd_matrix(*A_dims, dtype=dtype, device=device)
        L = torch.cholesky(A, upper=upper)
        return b, A, L

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_cholesky_solve(self, device, dtype):
        for (k, n), upper in product(zip([2, 3, 5], [3, 5, 7]), [True, False]):
            b, A, L = self.cholesky_solve_test_helper((n,), (n, k), upper, device, dtype)
            x = torch.cholesky_solve(b, L, upper=upper)
            self.assertLessEqual(b.dist(A.mm(x)), 1e-12)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_cholesky_solve_batched(self, device, dtype):
        def cholesky_solve_batch_helper(A_dims, b_dims, upper):
            b, A, L = self.cholesky_solve_test_helper(A_dims, b_dims, upper, device, dtype)
            x_exp_list = []
            for i in range(b_dims[0]):
                x_exp_list.append(torch.cholesky_solve(b[i], L[i], upper=upper))
            x_exp = torch.stack(x_exp_list)  # Stacked output
            x_act = torch.cholesky_solve(b, L, upper=upper)  # Actual output
            self.assertEqual(x_act, x_exp)  # Equality check
            self.assertLessEqual(b.dist(torch.matmul(A, x_act)), 2e-12)  # Correctness check

        for upper, batchsize in product([True, False], [1, 3, 4]):
            cholesky_solve_batch_helper((5, batchsize), (batchsize, 5, 10), upper)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_cholesky_solve_batched_non_contiguous(self, device, dtype):
        from numpy.linalg import solve
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix

        for upper in [True, False]:
            A = random_symmetric_pd_matrix(2, 2, dtype=dtype, device='cpu')
            b = torch.randn(2, 2, 2, dtype=dtype, device='cpu')
            x_exp = torch.Tensor(solve(A.permute(0, 2, 1).numpy(), b.permute(2, 1, 0).numpy())).to(dtype=dtype, device=device)
            A = A.to(device).permute(0, 2, 1)
            b = b.to(device).permute(2, 1, 0)
            assert not A.is_contiguous() and not b.is_contiguous(), "contiguous inputs"
            L = torch.cholesky(A, upper)
            x = torch.cholesky_solve(b, L, upper=upper)
            self.assertEqual(x, x_exp)

    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_cholesky_solve_batched_many_batches(self, device, dtype):
        for upper in [True, False]:
            b, A, L = self.cholesky_solve_test_helper((5, 256, 256), (5, 10), upper, device, dtype)
            x = torch.cholesky_solve(b, L, upper)
            self.assertEqual(torch.matmul(A, x), b.expand(A.shape[:-2] + (5, 10)))

            b, A, L = self.cholesky_solve_test_helper((5,), (512, 512, 5, 10), upper, device, dtype)
            x = torch.cholesky_solve(b, L, upper)
            self.assertEqual(torch.matmul(A, x), b)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_cholesky_solve_batched_broadcasting(self, device, dtype):
        from numpy.linalg import solve
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix

        def run_test(A_dims, b_dims, upper):
            A_matrix_size = A_dims[-1]
            A_batch_dims = A_dims[:-2]
            A = random_symmetric_pd_matrix(A_matrix_size, *A_batch_dims,
                                           dtype=dtype, device='cpu')
            b = torch.randn(*b_dims, dtype=dtype, device='cpu')
            x_exp = torch.tensor(solve(A.numpy(), b.numpy()), dtype=dtype, device=device)
            A, b = A.to(dtype=dtype, device=device), b.to(dtype=dtype, device=device)
            L = torch.cholesky(A, upper)
            x = torch.cholesky_solve(b, L, upper=upper)
            self.assertEqual(x, x_exp)
            # issue gh-42695
            x = torch.cholesky_solve(b, L, upper=upper, out=x)
            self.assertEqual(x, x_exp)

        # test against numpy.linalg.solve
        for upper in [True, False]:
            run_test((2, 1, 3, 4, 4), (2, 1, 3, 4, 6), upper)  # no broadcasting
            run_test((2, 1, 3, 4, 4), (4, 6), upper)  # broadcasting b
            run_test((4, 4), (2, 1, 3, 4, 2), upper)  # broadcasting A
            run_test((1, 3, 1, 4, 4), (2, 1, 3, 4, 5), upper)  # broadcasting A & b

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_cholesky_inverse(self, device, dtype):
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix
        a = random_symmetric_pd_matrix(5, dtype=dtype, device=device)

        # compute inverse directly
        inv0 = torch.inverse(a)

        # default case
        chol = torch.cholesky(a)
        inv1 = torch.cholesky_inverse(chol, False)
        self.assertLessEqual(inv0.dist(inv1), 1e-12)

        # upper Triangular Test
        chol = torch.cholesky(a, True)
        inv1 = torch.cholesky_inverse(chol, True)
        self.assertLessEqual(inv0.dist(inv1), 1e-12)

        # lower Triangular Test
        chol = torch.cholesky(a, False)
        inv1 = torch.cholesky_inverse(chol, False)
        self.assertLessEqual(inv0.dist(inv1), 1e-12)

    @slowTest
    @skipCUDAIf(True, "See issue #26789.")
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_cholesky_batched_many_batches(self, device, dtype):
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix

        def cholesky_test_helper(n, batchsize, device, upper):
            A = random_symmetric_pd_matrix(n, batchsize, dtype=dtype, device=device)
            chol_fact = torch.cholesky(A, upper=upper)
            if upper:
                # Correctness check
                self.assertEqual(A, chol_fact.transpose(-2, -1).matmul(chol_fact))
                # Upper triangular check
                self.assertEqual(chol_fact, chol_fact.triu())
            else:
                # Correctness check
                self.assertEqual(A, chol_fact.matmul(chol_fact.transpose(-2, -1)))
                # Lower triangular check
                self.assertEqual(chol_fact, chol_fact.tril())

        for upper, batchsize in product([True, False], [262144, 524288]):
            cholesky_test_helper(2, batchsize, device, upper)

    @precisionOverride({torch.float32: 1e-4, torch.complex64: 1e-4})
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float32, torch.float64, torch.complex64, torch.complex128)
    def test_cholesky_batched(self, device, dtype):
        from torch.testing._internal.common_utils import \
            (random_symmetric_pd_matrix,
             random_fullrank_matrix_distinct_singular_value)

        def cholesky_test_helper(n, batch_dims, upper):
            # This is a workaround while there is no support for complex random_symmetric_pd_matrix
            if dtype.is_complex:
                real_dtype = torch.float32 if dtype is torch.complex64 else torch.float64
                A_real = random_fullrank_matrix_distinct_singular_value(n, *batch_dims, dtype=real_dtype, device=device)
                A_imag = random_fullrank_matrix_distinct_singular_value(n, *batch_dims, dtype=real_dtype, device=device)
                A = A_real + 1j * A_imag
                # There is no support for complex batched matmul yet
                matmul_list = []
                for mat in A.contiguous().view(-1, n, n):
                    matmul_list.append(mat @ mat.t().conj())
                A = torch.stack(matmul_list).view(*batch_dims, n, n)
            else:
                A = random_symmetric_pd_matrix(n, *batch_dims, dtype=dtype, device=device)
            cholesky_exp = torch.stack([m.cholesky(upper=upper) for m in A.reshape(-1, n, n)])
            cholesky_exp = cholesky_exp.reshape_as(A)
            self.assertEqual(cholesky_exp, torch.cholesky(A, upper=upper))

        for upper, batchsize in product([True, False], [(3,), (3, 4), (2, 3, 4)]):
            cholesky_test_helper(3, batchsize, upper)

    @precisionOverride({torch.float32: 1e-4, torch.complex64: 1e-4})
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float32, torch.float64, torch.complex64, torch.complex128)
    def test_cholesky(self, device, dtype):
        from torch.testing._internal.common_utils import \
            (random_symmetric_pd_matrix,
             random_fullrank_matrix_distinct_singular_value)

        # This is a workaround while there is no support for complex random_symmetric_pd_matrix
        if dtype.is_complex:
            real_dtype = torch.float32 if dtype is torch.complex64 else torch.float64
            A_real = random_fullrank_matrix_distinct_singular_value(10, dtype=real_dtype, device=device)
            A_imag = random_fullrank_matrix_distinct_singular_value(10, dtype=real_dtype, device=device)
            A = A_real + 1j * A_imag
            A = A @ A.t().conj()
        else:
            A = random_symmetric_pd_matrix(10, dtype=dtype, device=device)

        # default Case
        C = torch.cholesky(A)
        B = torch.mm(C, C.t().conj())
        self.assertEqual(A, B, atol=1e-14, rtol=0)

        # test Upper Triangular
        U = torch.cholesky(A, True)
        B = torch.mm(U.t().conj(), U)
        self.assertEqual(A, B, atol=1e-14, rtol=0, msg='cholesky (upper) did not allow rebuilding the original matrix')

        # test Lower Triangular
        L = torch.cholesky(A, False)
        B = torch.mm(L, L.t().conj())
        self.assertEqual(A, B, atol=1e-14, rtol=0, msg='cholesky (lower) did not allow rebuilding the original matrix')

    def test_view(self, device):
        tensor = torch.rand(15, device=device)
        template = torch.rand(3, 5, device=device)
        empty = torch.empty(0, device=device)
        target = template.size()
        self.assertEqual(tensor.view_as(template).size(), target)
        self.assertEqual(tensor.view(3, 5).size(), target)
        self.assertEqual(tensor.view(torch.Size([3, 5])).size(), target)
        self.assertEqual(tensor.view(-1, 5).size(), target)
        self.assertEqual(tensor.view(3, -1).size(), target)
        tensor_view = tensor.view(5, 3)
        tensor_view.fill_(random.uniform(0, 1))
        self.assertEqual(empty.view_as(empty), empty)
        self.assertEqual(empty.view(0), empty)
        self.assertEqual(empty.view(0, 3, 0, 1).size(), torch.Size([0, 3, 0, 1]))
        self.assertEqual(empty.view(0, 3, 0, 1).view(0), empty)

        # test size inference with empty tensors
        self.assertEqual(empty.view(-1).size(), torch.Size([0]))
        self.assertEqual(empty.view(10, 3, -1).size(), torch.Size([10, 3, 0]))

        with self.assertRaisesRegex(RuntimeError, r"because the unspecified dimension size -1 can be any value"):
            empty.view(-1, 0)

        with self.assertRaisesRegex(RuntimeError, r"because the unspecified dimension size -1 can be any value"):
            empty.view(3, 0, -1, 0)

        self.assertRaises(RuntimeError, lambda: tensor.view(15, 0))
        self.assertRaises(RuntimeError, lambda: tensor.view(7, -1))
        self.assertRaises(RuntimeError, lambda: tensor.view(15, -1, -1))

        # test view when tensor is not contiguous in every dimension, but only
        # contiguous dimensions are touched.
        tensor = torch.rand(4, 2, 5, 1, 6, 2, 9, 3, device=device).transpose(-1, 2).transpose(-2, 3)
        # size:                      [   4,    2,    3,    9,    6,    2,    1,    5]
        # stride:                    [3840, 1620,    1,    3,   54,   27,  324,  324]
        # contiguous dim chunks:     [__________, ____, ____, __________, ____, ____]
        # merging 1 to chunk after:  [__________, ____, ____, __________, __________]
        contig_tensor = tensor.clone()
        # [4, 2] => [8, 1]
        # [3] => [3]
        # [9] => [3, 3]
        # [6, 2] => [4, 1, 3]
        # [1, 5] => [5]
        view_size = [8, 1, 3, 3, 3, 4, 1, 3, 5]
        self.assertEqual(tensor.view(*view_size), contig_tensor.view(*view_size))
        # [4, 2] => [2, 4]
        # [3] => [3]
        # [9] => [1, 9]
        # [6, 2] => [2, 2, 3]
        # [1, 5] => [5, 1]
        view_size = [2, 4, 3, 1, 9, 2, 2, 3, 5, 1]
        self.assertEqual(tensor.view(*view_size), contig_tensor.view(*view_size))
        # adding size 1 dims
        view_size = [1, 1, 2, 1, 4, 3, 1, 1, 9, 1, 2, 1, 2, 3, 1, 5, 1, 1]
        self.assertEqual(tensor.view(*view_size), contig_tensor.view(*view_size))

        # invalid views
        self.assertRaises(RuntimeError, lambda: tensor.view(-1))
        # crossing [4, 2], [3]
        self.assertRaises(RuntimeError, lambda: tensor.view(24, 9, 6, 2, 1, 5))
        # crossing [6, 2], [1, 5]
        self.assertRaises(RuntimeError, lambda: tensor.view(8, 3, 9, 6, 10))
        # crossing [9], [6, 2]
        self.assertRaises(RuntimeError, lambda: tensor.view(8, 3, 54, 2, 1, 5))

        # view with stride 0 dims
        tensor = torch.empty(1, 1, device=device).expand(3, 4)  # all dims are contiguous
        contig_tensor = tensor.clone()
        self.assertEqual(tensor.view(-1), contig_tensor.view(-1))
        self.assertEqual(tensor.view(1, -1, 1), contig_tensor.view(1, -1, 1))
        self.assertEqual(tensor.view(-1, 1), contig_tensor.view(-1, 1))
        self.assertEqual(tensor.view(6, 2, 1), contig_tensor.view(6, 2, 1))
        self.assertEqual(tensor.view(1, 6, 2, 1), contig_tensor.view(1, 6, 2, 1))

    def test_flip(self, device):
        data = torch.tensor([1, 2, 3, 4, 5, 6, 7, 8], device=device).view(2, 2, 2)

        self.assertEqual(torch.tensor([5, 6, 7, 8, 1, 2, 3, 4]).view(2, 2, 2), data.flip(0))
        self.assertEqual(torch.tensor([3, 4, 1, 2, 7, 8, 5, 6]).view(2, 2, 2), data.flip(1))
        self.assertEqual(torch.tensor([2, 1, 4, 3, 6, 5, 8, 7]).view(2, 2, 2), data.flip(2))
        self.assertEqual(torch.tensor([7, 8, 5, 6, 3, 4, 1, 2]).view(2, 2, 2), data.flip(0, 1))
        self.assertEqual(torch.tensor([8, 7, 6, 5, 4, 3, 2, 1]).view(2, 2, 2), data.flip(0, 1, 2))

        # check for wrap dim
        self.assertEqual(torch.tensor([2, 1, 4, 3, 6, 5, 8, 7]).view(2, 2, 2), data.flip(-1))
        # check for permute
        self.assertEqual(torch.tensor([6, 5, 8, 7, 2, 1, 4, 3]).view(2, 2, 2), data.flip(0, 2))
        self.assertEqual(torch.tensor([6, 5, 8, 7, 2, 1, 4, 3]).view(2, 2, 2), data.flip(2, 0))

        # not allow flip on the same dim more than once
        self.assertRaises(RuntimeError, lambda: data.flip(0, 1, 1))
        # not allow empty list as input
        self.assertRaises(TypeError, lambda: data.flip())

        # not allow size of flip dim > total dims
        self.assertRaises(IndexError, lambda: data.flip(0, 1, 2, 3))
        # not allow dim > max dim
        self.assertRaises(IndexError, lambda: data.flip(3))

        # test for non-contiguous case
        expanded_data = torch.arange(1, 4, device=device).view(3, 1).expand(3, 2)
        transposed_data = torch.arange(1, 9, device=device).view(2, 2, 2).transpose(0, 1)
        self.assertEqual(torch.tensor([3, 3, 2, 2, 1, 1]).view(3, 2), expanded_data.flip(0))
        self.assertEqual(torch.tensor([8, 7, 4, 3, 6, 5, 2, 1]).view(2, 2, 2), transposed_data.flip(0, 1, 2))

        # test for shape
        data = torch.randn(2, 3, 4, device=device)
        size = [2, 3, 4]
        test_dims = []
        for i in range(1, 3):
            test_dims += combinations(range(len(size)), i)

        for ds in test_dims:
            self.assertEqual(size, list(data.flip(ds).size()))

        # test rectangular case
        data = torch.tensor([1, 2, 3, 4, 5, 6]).view(2, 3).to(device)
        flip0_result = torch.tensor([[4, 5, 6], [1, 2, 3]]).to(device)
        flip1_result = torch.tensor([[3, 2, 1], [6, 5, 4]]).to(device)

        self.assertEqual(flip0_result, data.flip(0))
        self.assertEqual(flip1_result, data.flip(1))

        # test empty tensor, should just return an empty tensor of the same shape
        data = torch.tensor([])
        self.assertEqual(data, data.flip(0))

        # test bool tensor
        a = torch.tensor([False, True])
        self.assertEqual(a.flip(0), torch.tensor([True, False]))

    def _rand_shape(self, dim, min_size, max_size):
        shape = []
        for i in range(dim):
            shape.append(random.randint(min_size, max_size))
        return tuple(shape)

    @dtypes(torch.cfloat, torch.cdouble)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_complex_flip(self, device, dtype):
        rand_dim = random.randint(3, 4)
        shape = self._rand_shape(rand_dim, 5, 10)

        # Axis to sample for given shape.
        for i in range(1, rand_dim):
            # Check all combinations of `i` axis.
            for flip_dim in combinations(range(rand_dim), i):
                data = torch.randn(*shape, device=device, dtype=dtype)
                torch_fn = partial(torch.flip, dims=flip_dim)
                np_fn = partial(np.flip, axis=flip_dim)
                self.compare_with_numpy(torch_fn, np_fn, data)

    def _test_fliplr_flipud(self, torch_fn, np_fn, min_dim, max_dim, device, dtype):
        for dim in range(min_dim, max_dim + 1):
            shape = self._rand_shape(dim, 5, 10)
            # Randomly scale the input
            if dtype.is_floating_point or dtype.is_complex:
                data = torch.randn(*shape, device=device, dtype=dtype)
            else:
                data = torch.randint(0, 10, shape, device=device, dtype=dtype)
            self.compare_with_numpy(torch_fn, np_fn, data)

    @dtypes(torch.int64, torch.double, torch.cdouble)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_fliplr(self, device, dtype):
        self._test_fliplr_flipud(torch.fliplr, np.fliplr, 2, 4, device, dtype)

    @dtypes(torch.int64, torch.double, torch.cdouble)
    def test_fliplr_invalid(self, device, dtype):
        x = torch.randn(42).to(dtype)
        with self.assertRaisesRegex(RuntimeError, "Input must be >= 2-d."):
            torch.fliplr(x)
        with self.assertRaisesRegex(RuntimeError, "Input must be >= 2-d."):
            torch.fliplr(torch.tensor(42, device=device, dtype=dtype))

    @dtypes(torch.int64, torch.double, torch.cdouble)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_flipud(self, device, dtype):
        self._test_fliplr_flipud(torch.flipud, np.flipud, 1, 4, device, dtype)

    @dtypes(torch.int64, torch.double, torch.cdouble)
    def test_flipud_invalid(self, device, dtype):
        with self.assertRaisesRegex(RuntimeError, "Input must be >= 1-d."):
            torch.flipud(torch.tensor(42, device=device, dtype=dtype))

    def test_rot90(self, device):
        data = torch.arange(1, 5, device=device).view(2, 2)
        self.assertEqual(torch.tensor([1, 2, 3, 4]).view(2, 2), data.rot90(0, [0, 1]))
        self.assertEqual(torch.tensor([2, 4, 1, 3]).view(2, 2), data.rot90(1, [0, 1]))
        self.assertEqual(torch.tensor([4, 3, 2, 1]).view(2, 2), data.rot90(2, [0, 1]))
        self.assertEqual(torch.tensor([3, 1, 4, 2]).view(2, 2), data.rot90(3, [0, 1]))

        # test for default args k=1, dims=[0, 1]
        self.assertEqual(data.rot90(), data.rot90(1, [0, 1]))

        # test for reversed order of dims
        self.assertEqual(data.rot90(3, [0, 1]), data.rot90(1, [1, 0]))

        # test for modulo of k
        self.assertEqual(data.rot90(5, [0, 1]), data.rot90(1, [0, 1]))
        self.assertEqual(data.rot90(3, [0, 1]), data.rot90(-1, [0, 1]))
        self.assertEqual(data.rot90(-5, [0, 1]), data.rot90(-1, [0, 1]))

        # test for dims out-of-range error
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [0, -3]))
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [0, 2]))

        # test tensor with more than 2D
        data = torch.arange(1, 9, device=device).view(2, 2, 2)
        self.assertEqual(torch.tensor([2, 4, 1, 3, 6, 8, 5, 7]).view(2, 2, 2), data.rot90(1, [1, 2]))
        self.assertEqual(data.rot90(1, [1, -1]), data.rot90(1, [1, 2]))

        # test for errors
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [0, 3]))
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [1, 1]))
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [0, 1, 2]))
        self.assertRaises(RuntimeError, lambda: data.rot90(1, [0]))

    @dtypes(torch.cfloat, torch.cdouble)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_complex_rot90(self, device, dtype):
        shape = self._rand_shape(random.randint(2, 4), 5, 10)
        for rot_times in range(4):
            data = torch.randn(*shape, device=device, dtype=dtype)
            torch_fn = partial(torch.rot90, k=rot_times, dims=[0, 1])
            np_fn = partial(np.rot90, k=rot_times, axes=[0, 1])
            self.compare_with_numpy(torch_fn, np_fn, data)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_SCIPY, "Scipy not found")
    def test_signal_window_functions(self, device):

        def test(name, kwargs):
            torch_method = getattr(torch, name + '_window')
            for size in [0, 1, 2, 5, 10, 50, 100, 1024, 2048]:
                for periodic in [True, False]:
                    res = torch_method(size, periodic=periodic, **kwargs, device=device)
                    # NB: scipy always returns a float32 result
                    ref = torch.from_numpy(signal.get_window((name, *(kwargs.values())), size, fftbins=periodic))
                    self.assertEqual(res, ref, exact_dtype=False)
            with self.assertRaisesRegex(RuntimeError, r'not implemented for sparse types'):
                torch_method(3, layout=torch.sparse_coo)
            with self.assertRaisesRegex(RuntimeError, r'floating point'):
                torch_method(3, dtype=torch.long)
            self.assertTrue(torch_method(3, requires_grad=True).requires_grad)
            self.assertFalse(torch_method(3).requires_grad)

        for window in ['hann', 'hamming', 'bartlett', 'blackman']:
            test(window, kwargs={})

        for num_test in range(50):
            test('kaiser', kwargs={'beta': random.random() * 30})

    def test_broadcast(self, device):

        # all functions
        fns = {
            "dist", "atan2", "pow", "lerp", "add",
            "sub", "mul", "div", "fmod", "remainder",
            "eq", "ge", "gt", "le", "lt", "max", "min", "ne",
            "addcdiv", "addcmul", "masked_scatter", "masked_select", "masked_fill",
            "map", "map2", "copy"
        }
        # functions with three tensor arguments
        fns_3_args = {"map2"}
        fns_value_kwarg = {"addcdiv", "addcmul"}

        for fn in fns:
            (dims_small, dims_large, dims_full) = self._select_broadcastable_dims()
            full1d = torch.randn(*dims_full, device=device).flatten().float()
            small = torch.randn(*dims_small, device=device).float()
            large = torch.randn(*dims_large, device=device).float()
            small_expanded = small.expand(*dims_full)
            large_expanded = large.expand(*dims_full)
            small2 = None
            small2_expanded = None
            if fn in fns_3_args or fn in fns_value_kwarg:
                # create another smaller tensor
                (dims_small2, _, _) = self._select_broadcastable_dims(dims_full)
                small2 = torch.randn(*dims_small2, device=device).float()
                small2_expanded = small2.expand(*dims_full)

            if small.is_cuda and fn in ['map', 'map2']:
                # map and map2 are not implementd on CUDA tensors
                continue

            if hasattr(large_expanded, fn):
                # run through tensor versions of functions
                # and verify fully expanded inputs give same results
                expanded = {large: large_expanded, small: small_expanded, small2: small2_expanded}

                def tensorfn(myfn, t1, t2):
                    if fn == "lerp":
                        return myfn(t1, 0.5)
                    elif fn == "masked_select":
                        return myfn(t1 < 0)
                    elif fn == "masked_scatter":
                        return myfn(t1 < 0.5, full1d)
                    elif fn == "masked_fill":
                        return myfn(t1 < 0.5, 1.0)
                    elif fn in fns_3_args:
                        return myfn(1, t1, t2)
                    elif fn in fns_value_kwarg:
                        return myfn(t1, t2, value=1)
                    else:
                        return myfn(t1)

                # test various orders
                for first, second, third in [(large, small, small2), (small, large, small2),
                                             (small2, small, large), (small2, large, small)]:
                    if first is None:
                        break  # ignore last iter when small2 is None
                    method_expanded = getattr(expanded[first], fn)
                    method = getattr(first, fn)
                    r1 = tensorfn(method_expanded, expanded[second], expanded[third])
                    r2 = tensorfn(method, second, third)
                    self.assertEqual(r1, r2)

            # now for torch. versions of functions
            if hasattr(torch, fn):
                fntorch = getattr(torch, fn)
                expanded = {large: large_expanded, small: small_expanded, small2: small2_expanded}

                def torchfn(t1, t2, t3):
                    if fn == "lerp":
                        return fntorch(t1, t2, 0.5)
                    elif fn == "masked_select":
                        return fntorch(t1, t2 < 0)
                    elif fn == "masked_scatter":
                        return fntorch(t1, t2 < 0.5, full1d)
                    elif fn == "masked_fill":
                        return fntorch(t1, t2 < 0.5, 1.0)
                    elif fn in fns_3_args:
                        return fntorch(t1, 1.0, t2, t3)
                    elif fn in fns_value_kwarg:
                        return fntorch(t1, t2, t3, value=1.0)
                    else:
                        return fntorch(t1, t2)

                # test various orders
                for first, second, third in [(large, small, small2), (small, large, small2),
                                             (small2, small, large), (small2, large, small)]:
                    if first is None:
                        break  # ignore last iter when small2 is None
                    r1 = torchfn(expanded[first], expanded[second], expanded[third])
                    r2 = torchfn(first, second, third)
                    self.assertEqual(r1, r2)

            # now for in place functions
            # in-place tensor is not broadcastable; test only guaranteed
            # to work by broadcasting other argument(s)
            if not hasattr(large_expanded, fn + "_"):
                continue

            # need to clone largeExpanded so we can reuse, since functions are in-place
            large_expanded_clone = large_expanded.clone()

            def tensorfn_inplace(t0, t1, t2=None):
                t0_fn = getattr(t0, fn + "_")
                if fn == "lerp":
                    return t0_fn(t1, 0.5)
                elif fn == "masked_scatter":
                    return t0_fn(t1 < 0.5, full1d)
                elif fn == "masked_fill":
                    return t0_fn(t1 < 0.5, 1.0)
                elif fn == "map":
                    return t0_fn(t1, lambda x, y: x + y)
                elif fn == "map2":
                    return t0_fn(t1, t2, lambda x, y, z: x + y + z)
                elif fn in fns_3_args:
                    return t0_fn(1.0, t1, t2)
                elif fn in fns_value_kwarg:
                    return t0_fn(t1, t2, value=1.0)
                else:
                    return t0_fn(t1)
            # in-place pointwise operations don't actually work if the in-place
            # tensor is 0-strided (numpy has the same issue)
            if (0 not in large_expanded.stride() and 0 not in large_expanded_clone.stride()):
                r1 = tensorfn_inplace(large_expanded, small_expanded, small2_expanded)
                r2 = tensorfn_inplace(large_expanded_clone, small, small2)
                self.assertEqual(r1, r2)

            def broadcastable(t0, t1, t2=None):
                try:
                    t1.expand_as(t0)
                    if t2 is not None:
                        t2.expand_as(t0)
                except RuntimeError:
                    return False
                return True

            def _test_in_place_broadcastable(t0, t1, t2=None):
                if not broadcastable(t0, t1, t2):
                    same_size = t0.numel() == t1.numel() and (t0.numel() == t2.numel() if t2 is not None else True)
                    if not same_size:
                        self.assertRaises(RuntimeError, lambda: tensorfn_inplace(t0, t1, t2))
                else:
                    tensorfn_inplace(t0, t1, t2)

            if fn not in fns_3_args and fn not in fns_value_kwarg:
                _test_in_place_broadcastable(small, large_expanded)
                _test_in_place_broadcastable(small, large)
            else:
                _test_in_place_broadcastable(small2, small_expanded, large_expanded)
                _test_in_place_broadcastable(small2, small, large)

    def test_broadcast_fused_matmul(self, device):
        fns = ["baddbmm", "addbmm", "addmm", "addmv", "addr"]

        for fn in fns:
            batch_dim = random.randint(1, 8)
            n_dim = random.randint(1, 8)
            m_dim = random.randint(1, 8)
            p_dim = random.randint(1, 8)

            def dims_full_for_fn():
                if fn == "baddbmm":
                    return ([batch_dim, n_dim, p_dim], [batch_dim, n_dim, m_dim], [batch_dim, m_dim, p_dim])
                elif fn == "addbmm":
                    return ([n_dim, p_dim], [batch_dim, n_dim, m_dim], [batch_dim, m_dim, p_dim])
                elif fn == "addmm":
                    return ([n_dim, p_dim], [n_dim, m_dim], [m_dim, p_dim])
                elif fn == "addmv":
                    return ([n_dim], [n_dim, m_dim], [m_dim])
                elif fn == "addr":
                    return ([n_dim, m_dim], [n_dim], [m_dim])
                else:
                    raise AssertionError("unknown function")

            (t0_dims_full, t1_dims, t2_dims) = dims_full_for_fn()
            (t0_dims_small, _, _) = self._select_broadcastable_dims(t0_dims_full)

            t0_small = torch.randn(*t0_dims_small, device=device).float()
            t1 = torch.randn(*t1_dims, device=device).float()
            t2 = torch.randn(*t2_dims, device=device).float()

            t0_full = t0_small.expand(*t0_dims_full).to(device)

            fntorch = getattr(torch, fn)
            r0 = fntorch(t0_small, t1, t2)
            r1 = fntorch(t0_full, t1, t2)
            self.assertEqual(r0, r1)

    @tf32_on_and_off(0.001)
    def test_broadcast_batched_matmul(self, device):
        n_dim = random.randint(1, 8)
        m_dim = random.randint(1, 8)
        p_dim = random.randint(1, 8)
        full_batch_dims = [random.randint(1, 3) for i in range(random.randint(1, 3))]
        (batch_dims_small, _, _) = self._select_broadcastable_dims(full_batch_dims)

        def verify_batched_matmul(full_lhs, one_dimensional):
            if not one_dimensional:
                lhs_dims = [n_dim, m_dim]
                rhs_dims = [m_dim, p_dim]
                result_dims = [n_dim, p_dim]
            else:
                lhs_dims = [n_dim, m_dim] if full_lhs else [m_dim]
                rhs_dims = [m_dim, p_dim] if not full_lhs else [m_dim]
                result_dims = [n_dim] if full_lhs else [p_dim]

            lhs_mat_dims = lhs_dims if len(lhs_dims) != 1 else [1, m_dim]
            rhs_mat_dims = rhs_dims if len(rhs_dims) != 1 else [m_dim, 1]
            full_mat_dims = lhs_mat_dims if full_lhs else rhs_mat_dims
            dim0_dims = rhs_dims if full_lhs else lhs_dims
            small_dims = batch_dims_small + (rhs_mat_dims if full_lhs else lhs_mat_dims)

            small = torch.randn(*(small_dims), device=device).float()
            dim0 = torch.randn(*(dim0_dims), device=device).float()
            full = torch.randn(*(full_batch_dims + full_mat_dims), device=device).float()
            if not one_dimensional:
                (lhsTensors, rhsTensors) = ((full,), (small, dim0)) if full_lhs else ((small, dim0), (full,))
            else:
                (lhsTensors, rhsTensors) = ((full,), (dim0,)) if full_lhs else ((dim0,), (full,))

            def maybe_squeeze_result(l, r, result):
                if len(lhs_dims) == 1 and l.dim() != 1:
                    return result.squeeze(-2)
                elif len(rhs_dims) == 1 and r.dim() != 1:
                    return result.squeeze(-1)
                else:
                    return result

            for lhs in lhsTensors:
                lhs_expanded = lhs.expand(*(torch.Size(full_batch_dims) + torch.Size(lhs_mat_dims)))
                lhs_expanded_matmul_fn = lhs_expanded.matmul
                for rhs in rhsTensors:
                    rhs_expanded = ((rhs if len(rhs_dims) != 1 else rhs.unsqueeze(-1)).
                                    expand(*(torch.Size(full_batch_dims) + torch.Size(rhs_mat_dims))))
                    truth = maybe_squeeze_result(lhs_expanded, rhs_expanded, lhs_expanded_matmul_fn(rhs_expanded))
                    for l in (lhs, lhs_expanded):
                        for r in (rhs, rhs_expanded):
                            l_matmul_fn = l.matmul
                            result = maybe_squeeze_result(l, r, l_matmul_fn(r))
                            self.assertEqual(truth, result)
                            # test torch.matmul function as well
                            torch_result = maybe_squeeze_result(l, r, torch.matmul(l, r))
                            self.assertEqual(truth, torch_result)
                            # test torch.matmul with out
                            out = torch.zeros_like(torch_result)
                            torch.matmul(l, r, out=out)
                            self.assertEqual(truth, maybe_squeeze_result(l, r, out))

                # compare to bmm
                bmm_result = (torch.bmm(lhs_expanded.contiguous().view(-1, *lhs_mat_dims),
                                        rhs_expanded.contiguous().view(-1, *rhs_mat_dims)))
                self.assertEqual(truth.view(-1, *result_dims), bmm_result.view(-1, *result_dims))

        for indices in product((True, False), repeat=2):
            verify_batched_matmul(*indices)

    def test_contiguous(self, device):
        x = torch.randn(1, 16, 5, 5, device=device)
        self.assertTrue(x.is_contiguous())
        stride = list(x.stride())
        stride[0] = 20
        # change the stride in dimension 0. the tensor is still contiguous because size[0] is 1
        x.set_(x.storage(), 0, x.size(), stride)
        self.assertTrue(x.is_contiguous())

    def test_index(self, device):

        def consec(size, start=1):
            sequence = torch.ones(int(torch.Tensor(size).prod(0))).cumsum(0)
            sequence.add_(start - 1)
            return sequence.view(*size)

        reference = consec((3, 3, 3)).to(device)

        # empty tensor indexing
        self.assertEqual(reference[torch.LongTensor().to(device)], reference.new(0, 3, 3))

        self.assertEqual(reference[0], consec((3, 3)), atol=0, rtol=0)
        self.assertEqual(reference[1], consec((3, 3), 10), atol=0, rtol=0)
        self.assertEqual(reference[2], consec((3, 3), 19), atol=0, rtol=0)
        self.assertEqual(reference[0, 1], consec((3,), 4), atol=0, rtol=0)
        self.assertEqual(reference[0:2], consec((2, 3, 3)), atol=0, rtol=0)
        self.assertEqual(reference[2, 2, 2], 27, atol=0, rtol=0)
        self.assertEqual(reference[:], consec((3, 3, 3)), atol=0, rtol=0)

        # indexing with Ellipsis
        self.assertEqual(reference[..., 2], torch.Tensor([[3, 6, 9],
                                                          [12, 15, 18],
                                                          [21, 24, 27]]), atol=0, rtol=0)
        self.assertEqual(reference[0, ..., 2], torch.Tensor([3, 6, 9]), atol=0, rtol=0)
        self.assertEqual(reference[..., 2], reference[:, :, 2], atol=0, rtol=0)
        self.assertEqual(reference[0, ..., 2], reference[0, :, 2], atol=0, rtol=0)
        self.assertEqual(reference[0, 2, ...], reference[0, 2], atol=0, rtol=0)
        self.assertEqual(reference[..., 2, 2, 2], 27, atol=0, rtol=0)
        self.assertEqual(reference[2, ..., 2, 2], 27, atol=0, rtol=0)
        self.assertEqual(reference[2, 2, ..., 2], 27, atol=0, rtol=0)
        self.assertEqual(reference[2, 2, 2, ...], 27, atol=0, rtol=0)
        self.assertEqual(reference[...], reference, atol=0, rtol=0)

        reference_5d = consec((3, 3, 3, 3, 3)).to(device)
        self.assertEqual(reference_5d[..., 1, 0], reference_5d[:, :, :, 1, 0], atol=0, rtol=0)
        self.assertEqual(reference_5d[2, ..., 1, 0], reference_5d[2, :, :, 1, 0], atol=0, rtol=0)
        self.assertEqual(reference_5d[2, 1, 0, ..., 1], reference_5d[2, 1, 0, :, 1], atol=0, rtol=0)
        self.assertEqual(reference_5d[...], reference_5d, atol=0, rtol=0)

        # LongTensor indexing
        reference = consec((5, 5, 5)).to(device)
        idx = torch.LongTensor([2, 4]).to(device)
        self.assertEqual(reference[idx], torch.stack([reference[2], reference[4]]))
        # TODO: enable one indexing is implemented like in numpy
        # self.assertEqual(reference[2, idx], torch.stack([reference[2, 2], reference[2, 4]]))
        # self.assertEqual(reference[3, idx, 1], torch.stack([reference[3, 2], reference[3, 4]])[:, 1])

        # None indexing
        self.assertEqual(reference[2, None], reference[2].unsqueeze(0))
        self.assertEqual(reference[2, None, None], reference[2].unsqueeze(0).unsqueeze(0))
        self.assertEqual(reference[2:4, None], reference[2:4].unsqueeze(1))
        self.assertEqual(reference[None, 2, None, None], reference.unsqueeze(0)[:, 2].unsqueeze(0).unsqueeze(0))
        self.assertEqual(reference[None, 2:5, None, None], reference.unsqueeze(0)[:, 2:5].unsqueeze(2).unsqueeze(2))

        # indexing 0-length slice
        self.assertEqual(torch.empty(0, 5, 5), reference[slice(0)])
        self.assertEqual(torch.empty(0, 5), reference[slice(0), 2])
        self.assertEqual(torch.empty(0, 5), reference[2, slice(0)])
        self.assertEqual(torch.tensor([]), reference[2, 1:1, 2])

        # indexing with step
        reference = consec((10, 10, 10)).to(device)
        self.assertEqual(reference[1:5:2], torch.stack([reference[1], reference[3]], 0))
        self.assertEqual(reference[1:6:2], torch.stack([reference[1], reference[3], reference[5]], 0))
        self.assertEqual(reference[1:9:4], torch.stack([reference[1], reference[5]], 0))
        self.assertEqual(reference[2:4, 1:5:2], torch.stack([reference[2:4, 1], reference[2:4, 3]], 1))
        self.assertEqual(reference[3, 1:6:2], torch.stack([reference[3, 1], reference[3, 3], reference[3, 5]], 0))
        self.assertEqual(reference[None, 2, 1:9:4], torch.stack([reference[2, 1], reference[2, 5]], 0).unsqueeze(0))
        self.assertEqual(reference[:, 2, 1:6:2],
                         torch.stack([reference[:, 2, 1], reference[:, 2, 3], reference[:, 2, 5]], 1))

        lst = [list(range(i, i + 10)) for i in range(0, 100, 10)]
        tensor = torch.DoubleTensor(lst).to(device)
        for _i in range(100):
            idx1_start = random.randrange(10)
            idx1_end = idx1_start + random.randrange(1, 10 - idx1_start + 1)
            idx1_step = random.randrange(1, 8)
            idx1 = slice(idx1_start, idx1_end, idx1_step)
            if random.randrange(2) == 0:
                idx2_start = random.randrange(10)
                idx2_end = idx2_start + random.randrange(1, 10 - idx2_start + 1)
                idx2_step = random.randrange(1, 8)
                idx2 = slice(idx2_start, idx2_end, idx2_step)
                lst_indexed = list(map(lambda l: l[idx2], lst[idx1]))
                tensor_indexed = tensor[idx1, idx2]
            else:
                lst_indexed = lst[idx1]
                tensor_indexed = tensor[idx1]
            self.assertEqual(torch.DoubleTensor(lst_indexed), tensor_indexed)

        self.assertRaises(ValueError, lambda: reference[1:9:0])
        self.assertRaises(ValueError, lambda: reference[1:9:-1])

        self.assertRaises(IndexError, lambda: reference[1, 1, 1, 1])
        self.assertRaises(IndexError, lambda: reference[1, 1, 1, 1:1])
        self.assertRaises(IndexError, lambda: reference[3, 3, 3, 3, 3, 3, 3, 3])

        self.assertRaises(IndexError, lambda: reference[0.0])
        self.assertRaises(TypeError, lambda: reference[0.0:2.0])
        self.assertRaises(IndexError, lambda: reference[0.0, 0.0:2.0])
        self.assertRaises(IndexError, lambda: reference[0.0, :, 0.0:2.0])
        self.assertRaises(IndexError, lambda: reference[0.0, ..., 0.0:2.0])
        self.assertRaises(IndexError, lambda: reference[0.0, :, 0.0])

        def delitem():
            del reference[0]

        self.assertRaises(TypeError, delitem)

    @dtypes(torch.half, torch.double)
    def test_advancedindex(self, device, dtype):
        # Tests for Integer Array Indexing, Part I - Purely integer array
        # indexing

        def consec(size, start=1):
            # Creates the sequence in float since CPU half doesn't support the
            # needed operations. Converts to dtype before returning.
            numel = reduce(lambda x, y: x * y, size, 1)
            sequence = torch.ones(numel, dtype=torch.float, device=device).cumsum(0)
            sequence.add_(start - 1)
            return sequence.view(*size).to(dtype=dtype)

        # pick a random valid indexer type
        def ri(indices):
            choice = random.randint(0, 2)
            if choice == 0:
                return torch.LongTensor(indices).to(device)
            elif choice == 1:
                return list(indices)
            else:
                return tuple(indices)

        def validate_indexing(x):
            self.assertEqual(x[[0]], consec((1,)))
            self.assertEqual(x[ri([0]), ], consec((1,)))
            self.assertEqual(x[ri([3]), ], consec((1,), 4))
            self.assertEqual(x[[2, 3, 4]], consec((3,), 3))
            self.assertEqual(x[ri([2, 3, 4]), ], consec((3,), 3))
            self.assertEqual(x[ri([0, 2, 4]), ], torch.tensor([1, 3, 5], dtype=dtype, device=device))

        def validate_setting(x):
            x[[0]] = -2
            self.assertEqual(x[[0]], torch.tensor([-2], dtype=dtype, device=device))
            x[[0]] = -1
            self.assertEqual(x[ri([0]), ], torch.tensor([-1], dtype=dtype, device=device))
            x[[2, 3, 4]] = 4
            self.assertEqual(x[[2, 3, 4]], torch.tensor([4, 4, 4], dtype=dtype, device=device))
            x[ri([2, 3, 4]), ] = 3
            self.assertEqual(x[ri([2, 3, 4]), ], torch.tensor([3, 3, 3], dtype=dtype, device=device))
            x[ri([0, 2, 4]), ] = torch.tensor([5, 4, 3], dtype=dtype, device=device)
            self.assertEqual(x[ri([0, 2, 4]), ], torch.tensor([5, 4, 3], dtype=dtype, device=device))

        # Only validates indexing and setting for halfs
        if dtype == torch.half:
            reference = consec((10,))
            validate_indexing(reference)
            validate_setting(reference)
            return

        # Case 1: Purely Integer Array Indexing
        reference = consec((10,))
        validate_indexing(reference)

        # setting values
        validate_setting(reference)

        # Tensor with stride != 1
        # strided is [1, 3, 5, 7]
        reference = consec((10,))
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), storage_offset=0,
                     size=torch.Size([4]), stride=[2])

        self.assertEqual(strided[[0]], torch.tensor([1], dtype=dtype, device=device))
        self.assertEqual(strided[ri([0]), ], torch.tensor([1], dtype=dtype, device=device))
        self.assertEqual(strided[ri([3]), ], torch.tensor([7], dtype=dtype, device=device))
        self.assertEqual(strided[[1, 2]], torch.tensor([3, 5], dtype=dtype, device=device))
        self.assertEqual(strided[ri([1, 2]), ], torch.tensor([3, 5], dtype=dtype, device=device))
        self.assertEqual(strided[ri([[2, 1], [0, 3]]), ],
                         torch.tensor([[5, 3], [1, 7]], dtype=dtype, device=device))

        # stride is [4, 8]
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), storage_offset=4,
                     size=torch.Size([2]), stride=[4])
        self.assertEqual(strided[[0]], torch.tensor([5], dtype=dtype, device=device))
        self.assertEqual(strided[ri([0]), ], torch.tensor([5], dtype=dtype, device=device))
        self.assertEqual(strided[ri([1]), ], torch.tensor([9], dtype=dtype, device=device))
        self.assertEqual(strided[[0, 1]], torch.tensor([5, 9], dtype=dtype, device=device))
        self.assertEqual(strided[ri([0, 1]), ], torch.tensor([5, 9], dtype=dtype, device=device))
        self.assertEqual(strided[ri([[0, 1], [1, 0]]), ],
                         torch.tensor([[5, 9], [9, 5]], dtype=dtype, device=device))

        # reference is 1 2
        #              3 4
        #              5 6
        reference = consec((3, 2))
        self.assertEqual(reference[ri([0, 1, 2]), ri([0])], torch.tensor([1, 3, 5], dtype=dtype, device=device))
        self.assertEqual(reference[ri([0, 1, 2]), ri([1])], torch.tensor([2, 4, 6], dtype=dtype, device=device))
        self.assertEqual(reference[ri([0]), ri([0])], consec((1,)))
        self.assertEqual(reference[ri([2]), ri([1])], consec((1,), 6))
        self.assertEqual(reference[[ri([0, 0]), ri([0, 1])]], torch.tensor([1, 2], dtype=dtype, device=device))
        self.assertEqual(reference[[ri([0, 1, 1, 0, 2]), ri([1])]],
                         torch.tensor([2, 4, 4, 2, 6], dtype=dtype, device=device))
        self.assertEqual(reference[[ri([0, 0, 1, 1]), ri([0, 1, 0, 0])]],
                         torch.tensor([1, 2, 3, 3], dtype=dtype, device=device))

        rows = ri([[0, 0],
                   [1, 2]])
        columns = [0],
        self.assertEqual(reference[rows, columns], torch.tensor([[1, 1],
                                                                 [3, 5]], dtype=dtype, device=device))

        rows = ri([[0, 0],
                   [1, 2]])
        columns = ri([1, 0])
        self.assertEqual(reference[rows, columns], torch.tensor([[2, 1],
                                                                 [4, 5]], dtype=dtype, device=device))
        rows = ri([[0, 0],
                   [1, 2]])
        columns = ri([[0, 1],
                      [1, 0]])
        self.assertEqual(reference[rows, columns], torch.tensor([[1, 2],
                                                                 [4, 5]], dtype=dtype, device=device))

        # setting values
        reference[ri([0]), ri([1])] = -1
        self.assertEqual(reference[ri([0]), ri([1])], torch.tensor([-1], dtype=dtype, device=device))
        reference[ri([0, 1, 2]), ri([0])] = torch.tensor([-1, 2, -4], dtype=dtype, device=device)
        self.assertEqual(reference[ri([0, 1, 2]), ri([0])],
                         torch.tensor([-1, 2, -4], dtype=dtype, device=device))
        reference[rows, columns] = torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device)
        self.assertEqual(reference[rows, columns],
                         torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device))

        # Verify still works with Transposed (i.e. non-contiguous) Tensors

        reference = torch.tensor([[0, 1, 2, 3],
                                  [4, 5, 6, 7],
                                  [8, 9, 10, 11]], dtype=dtype, device=device).t_()

        # Transposed: [[0, 4, 8],
        #              [1, 5, 9],
        #              [2, 6, 10],
        #              [3, 7, 11]]

        self.assertEqual(reference[ri([0, 1, 2]), ri([0])],
                         torch.tensor([0, 1, 2], dtype=dtype, device=device))
        self.assertEqual(reference[ri([0, 1, 2]), ri([1])],
                         torch.tensor([4, 5, 6], dtype=dtype, device=device))
        self.assertEqual(reference[ri([0]), ri([0])],
                         torch.tensor([0], dtype=dtype, device=device))
        self.assertEqual(reference[ri([2]), ri([1])],
                         torch.tensor([6], dtype=dtype, device=device))
        self.assertEqual(reference[[ri([0, 0]), ri([0, 1])]],
                         torch.tensor([0, 4], dtype=dtype, device=device))
        self.assertEqual(reference[[ri([0, 1, 1, 0, 3]), ri([1])]],
                         torch.tensor([4, 5, 5, 4, 7], dtype=dtype, device=device))
        self.assertEqual(reference[[ri([0, 0, 1, 1]), ri([0, 1, 0, 0])]],
                         torch.tensor([0, 4, 1, 1], dtype=dtype, device=device))

        rows = ri([[0, 0],
                   [1, 2]])
        columns = [0],
        self.assertEqual(reference[rows, columns],
                         torch.tensor([[0, 0], [1, 2]], dtype=dtype, device=device))

        rows = ri([[0, 0],
                   [1, 2]])
        columns = ri([1, 0])
        self.assertEqual(reference[rows, columns],
                         torch.tensor([[4, 0], [5, 2]], dtype=dtype, device=device))
        rows = ri([[0, 0],
                   [1, 3]])
        columns = ri([[0, 1],
                      [1, 2]])
        self.assertEqual(reference[rows, columns],
                         torch.tensor([[0, 4], [5, 11]], dtype=dtype, device=device))

        # setting values
        reference[ri([0]), ri([1])] = -1
        self.assertEqual(reference[ri([0]), ri([1])],
                         torch.tensor([-1], dtype=dtype, device=device))
        reference[ri([0, 1, 2]), ri([0])] = torch.tensor([-1, 2, -4], dtype=dtype, device=device)
        self.assertEqual(reference[ri([0, 1, 2]), ri([0])],
                         torch.tensor([-1, 2, -4], dtype=dtype, device=device))
        reference[rows, columns] = torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device)
        self.assertEqual(reference[rows, columns],
                         torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device))

        # stride != 1

        # strided is [[1 3 5 7],
        #             [9 11 13 15]]

        reference = torch.arange(0., 24, dtype=dtype, device=device).view(3, 8)
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), 1, size=torch.Size([2, 4]),
                     stride=[8, 2])

        self.assertEqual(strided[ri([0, 1]), ri([0])],
                         torch.tensor([1, 9], dtype=dtype, device=device))
        self.assertEqual(strided[ri([0, 1]), ri([1])],
                         torch.tensor([3, 11], dtype=dtype, device=device))
        self.assertEqual(strided[ri([0]), ri([0])],
                         torch.tensor([1], dtype=dtype, device=device))
        self.assertEqual(strided[ri([1]), ri([3])],
                         torch.tensor([15], dtype=dtype, device=device))
        self.assertEqual(strided[[ri([0, 0]), ri([0, 3])]],
                         torch.tensor([1, 7], dtype=dtype, device=device))
        self.assertEqual(strided[[ri([1]), ri([0, 1, 1, 0, 3])]],
                         torch.tensor([9, 11, 11, 9, 15], dtype=dtype, device=device))
        self.assertEqual(strided[[ri([0, 0, 1, 1]), ri([0, 1, 0, 0])]],
                         torch.tensor([1, 3, 9, 9], dtype=dtype, device=device))

        rows = ri([[0, 0],
                   [1, 1]])
        columns = [0],
        self.assertEqual(strided[rows, columns],
                         torch.tensor([[1, 1], [9, 9]], dtype=dtype, device=device))

        rows = ri([[0, 1],
                   [1, 0]])
        columns = ri([1, 2])
        self.assertEqual(strided[rows, columns],
                         torch.tensor([[3, 13], [11, 5]], dtype=dtype, device=device))
        rows = ri([[0, 0],
                   [1, 1]])
        columns = ri([[0, 1],
                      [1, 2]])
        self.assertEqual(strided[rows, columns],
                         torch.tensor([[1, 3], [11, 13]], dtype=dtype, device=device))

        # setting values

        # strided is [[10, 11],
        #             [17, 18]]

        reference = torch.arange(0., 24, dtype=dtype, device=device).view(3, 8)
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), 10, size=torch.Size([2, 2]),
                     stride=[7, 1])
        self.assertEqual(strided[ri([0]), ri([1])],
                         torch.tensor([11], dtype=dtype, device=device))
        strided[ri([0]), ri([1])] = -1
        self.assertEqual(strided[ri([0]), ri([1])],
                         torch.tensor([-1], dtype=dtype, device=device))

        reference = torch.arange(0., 24, dtype=dtype, device=device).view(3, 8)
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), 10, size=torch.Size([2, 2]),
                     stride=[7, 1])
        self.assertEqual(strided[ri([0, 1]), ri([1, 0])],
                         torch.tensor([11, 17], dtype=dtype, device=device))
        strided[ri([0, 1]), ri([1, 0])] = torch.tensor([-1, 2], dtype=dtype, device=device)
        self.assertEqual(strided[ri([0, 1]), ri([1, 0])],
                         torch.tensor([-1, 2], dtype=dtype, device=device))

        reference = torch.arange(0., 24, dtype=dtype, device=device).view(3, 8)
        strided = torch.tensor((), dtype=dtype, device=device)
        strided.set_(reference.storage(), 10, size=torch.Size([2, 2]),
                     stride=[7, 1])

        rows = ri([[0],
                   [1]])
        columns = ri([[0, 1],
                      [0, 1]])
        self.assertEqual(strided[rows, columns],
                         torch.tensor([[10, 11], [17, 18]], dtype=dtype, device=device))
        strided[rows, columns] = torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device)
        self.assertEqual(strided[rows, columns],
                         torch.tensor([[4, 6], [2, 3]], dtype=dtype, device=device))

        # Tests using less than the number of dims, and ellipsis

        # reference is 1 2
        #              3 4
        #              5 6
        reference = consec((3, 2))
        self.assertEqual(reference[ri([0, 2]), ],
                         torch.tensor([[1, 2], [5, 6]], dtype=dtype, device=device))
        self.assertEqual(reference[ri([1]), ...],
                         torch.tensor([[3, 4]], dtype=dtype, device=device))
        self.assertEqual(reference[..., ri([1])],
                         torch.tensor([[2], [4], [6]], dtype=dtype, device=device))

        # verify too many indices fails
        with self.assertRaises(IndexError):
            reference[ri([1]), ri([0, 2]), ri([3])]

        # test invalid index fails
        reference = torch.empty(10, dtype=dtype, device=device)
        # can't test cuda because it is a device assert
        if not reference.is_cuda:
            for err_idx in (10, -11):
                with self.assertRaisesRegex(IndexError, r'out of'):
                    reference[err_idx]
                with self.assertRaisesRegex(IndexError, r'out of'):
                    reference[torch.LongTensor([err_idx]).to(device)]
                with self.assertRaisesRegex(IndexError, r'out of'):
                    reference[[err_idx]]

        if TEST_NUMPY:
            # we use numpy to compare against, to verify that our advanced
            # indexing semantics are the same, and also for ease of test
            # writing

            def tensor_indices_to_np(tensor, indices):
                # convert the Torch Tensor to a numpy array
                tensor = tensor.to(device='cpu')
                npt = tensor.numpy()

                # convert indices
                idxs = tuple(i.tolist() if isinstance(i, torch.LongTensor) else
                             i for i in indices)

                return npt, idxs

            def get_numpy(tensor, indices):
                npt, idxs = tensor_indices_to_np(tensor, indices)

                # index and return as a Torch Tensor
                return torch.tensor(npt[idxs], dtype=dtype, device=device)

            def set_numpy(tensor, indices, value):
                if not isinstance(value, int):
                    if self.device_type != 'cpu':
                        value = value.cpu()
                    value = value.numpy()

                npt, idxs = tensor_indices_to_np(tensor, indices)
                npt[idxs] = value
                return npt

            def assert_get_eq(tensor, indexer):
                self.assertEqual(tensor[indexer], get_numpy(tensor, indexer))

            def assert_set_eq(tensor, indexer, val):
                pyt = tensor.clone()
                numt = tensor.clone()
                pyt[indexer] = val
                numt = torch.tensor(set_numpy(numt, indexer, val), dtype=dtype, device=device)
                self.assertEqual(pyt, numt)

            def assert_backward_eq(tensor, indexer):
                cpu = tensor.float().clone().detach().requires_grad_(True)
                outcpu = cpu[indexer]
                gOcpu = torch.rand_like(outcpu)
                outcpu.backward(gOcpu)
                dev = cpu.to(device).detach().requires_grad_(True)
                outdev = dev[indexer]
                outdev.backward(gOcpu.to(device))
                self.assertEqual(cpu.grad, dev.grad)

            def get_set_tensor(indexed, indexer):
                set_size = indexed[indexer].size()
                set_count = indexed[indexer].numel()
                set_tensor = torch.randperm(set_count).view(set_size).double().to(device)
                return set_tensor

            # Tensor is  0  1  2  3  4
            #            5  6  7  8  9
            #           10 11 12 13 14
            #           15 16 17 18 19
            reference = torch.arange(0., 20, dtype=dtype, device=device).view(4, 5)

            indices_to_test = [
                # grab the second, fourth columns
                [slice(None), [1, 3]],

                # first, third rows,
                [[0, 2], slice(None)],

                # weird shape
                [slice(None), [[0, 1],
                               [2, 3]]],
                # negatives
                [[-1], [0]],
                [[0, 2], [-1]],
                [slice(None), [-1]],
            ]

            # only test dupes on gets
            get_indices_to_test = indices_to_test + [[slice(None), [0, 1, 1, 2, 2]]]

            for indexer in get_indices_to_test:
                assert_get_eq(reference, indexer)
                if self.device_type != 'cpu':
                    assert_backward_eq(reference, indexer)

            for indexer in indices_to_test:
                assert_set_eq(reference, indexer, 44)
                assert_set_eq(reference,
                              indexer,
                              get_set_tensor(reference, indexer))

            reference = torch.arange(0., 160, dtype=dtype, device=device).view(4, 8, 5)

            indices_to_test = [
                [slice(None), slice(None), [0, 3, 4]],
                [slice(None), [2, 4, 5, 7], slice(None)],
                [[2, 3], slice(None), slice(None)],
                [slice(None), [0, 2, 3], [1, 3, 4]],
                [slice(None), [0], [1, 2, 4]],
                [slice(None), [0, 1, 3], [4]],
                [slice(None), [[0, 1], [1, 0]], [[2, 3]]],
                [slice(None), [[0, 1], [2, 3]], [[0]]],
                [slice(None), [[5, 6]], [[0, 3], [4, 4]]],
                [[0, 2, 3], [1, 3, 4], slice(None)],
                [[0], [1, 2, 4], slice(None)],
                [[0, 1, 3], [4], slice(None)],
                [[[0, 1], [1, 0]], [[2, 1], [3, 5]], slice(None)],
                [[[0, 1], [1, 0]], [[2, 3]], slice(None)],
                [[[0, 1], [2, 3]], [[0]], slice(None)],
                [[[2, 1]], [[0, 3], [4, 4]], slice(None)],
                [[[2]], [[0, 3], [4, 1]], slice(None)],
                # non-contiguous indexing subspace
                [[0, 2, 3], slice(None), [1, 3, 4]],

                # less dim, ellipsis
                [[0, 2], ],
                [[0, 2], slice(None)],
                [[0, 2], Ellipsis],
                [[0, 2], slice(None), Ellipsis],
                [[0, 2], Ellipsis, slice(None)],
                [[0, 2], [1, 3]],
                [[0, 2], [1, 3], Ellipsis],
                [Ellipsis, [1, 3], [2, 3]],
                [Ellipsis, [2, 3, 4]],
                [Ellipsis, slice(None), [2, 3, 4]],
                [slice(None), Ellipsis, [2, 3, 4]],

                # ellipsis counts for nothing
                [Ellipsis, slice(None), slice(None), [0, 3, 4]],
                [slice(None), Ellipsis, slice(None), [0, 3, 4]],
                [slice(None), slice(None), Ellipsis, [0, 3, 4]],
                [slice(None), slice(None), [0, 3, 4], Ellipsis],
                [Ellipsis, [[0, 1], [1, 0]], [[2, 1], [3, 5]], slice(None)],
                [[[0, 1], [1, 0]], [[2, 1], [3, 5]], Ellipsis, slice(None)],
                [[[0, 1], [1, 0]], [[2, 1], [3, 5]], slice(None), Ellipsis],
            ]

            for indexer in indices_to_test:
                assert_get_eq(reference, indexer)
                assert_set_eq(reference, indexer, 212)
                assert_set_eq(reference,
                              indexer,
                              get_set_tensor(reference, indexer))
                if torch.cuda.is_available():
                    assert_backward_eq(reference, indexer)

            reference = torch.arange(0., 1296, dtype=dtype, device=device).view(3, 9, 8, 6)

            indices_to_test = [
                [slice(None), slice(None), slice(None), [0, 3, 4]],
                [slice(None), slice(None), [2, 4, 5, 7], slice(None)],
                [slice(None), [2, 3], slice(None), slice(None)],
                [[1, 2], slice(None), slice(None), slice(None)],
                [slice(None), slice(None), [0, 2, 3], [1, 3, 4]],
                [slice(None), slice(None), [0], [1, 2, 4]],
                [slice(None), slice(None), [0, 1, 3], [4]],
                [slice(None), slice(None), [[0, 1], [1, 0]], [[2, 3]]],
                [slice(None), slice(None), [[0, 1], [2, 3]], [[0]]],
                [slice(None), slice(None), [[5, 6]], [[0, 3], [4, 4]]],
                [slice(None), [0, 2, 3], [1, 3, 4], slice(None)],
                [slice(None), [0], [1, 2, 4], slice(None)],
                [slice(None), [0, 1, 3], [4], slice(None)],
                [slice(None), [[0, 1], [3, 4]], [[2, 3], [0, 1]], slice(None)],
                [slice(None), [[0, 1], [3, 4]], [[2, 3]], slice(None)],
                [slice(None), [[0, 1], [3, 2]], [[0]], slice(None)],
                [slice(None), [[2, 1]], [[0, 3], [6, 4]], slice(None)],
                [slice(None), [[2]], [[0, 3], [4, 2]], slice(None)],
                [[0, 1, 2], [1, 3, 4], slice(None), slice(None)],
                [[0], [1, 2, 4], slice(None), slice(None)],
                [[0, 1, 2], [4], slice(None), slice(None)],
                [[[0, 1], [0, 2]], [[2, 4], [1, 5]], slice(None), slice(None)],
                [[[0, 1], [1, 2]], [[2, 0]], slice(None), slice(None)],
                [[[2, 2]], [[0, 3], [4, 5]], slice(None), slice(None)],
                [[[2]], [[0, 3], [4, 5]], slice(None), slice(None)],
                [slice(None), [3, 4, 6], [0, 2, 3], [1, 3, 4]],
                [slice(None), [2, 3, 4], [1, 3, 4], [4]],
                [slice(None), [0, 1, 3], [4], [1, 3, 4]],
                [slice(None), [6], [0, 2, 3], [1, 3, 4]],
                [slice(None), [2, 3, 5], [3], [4]],
                [slice(None), [0], [4], [1, 3, 4]],
                [slice(None), [6], [0, 2, 3], [1]],
                [slice(None), [[0, 3], [3, 6]], [[0, 1], [1, 3]], [[5, 3], [1, 2]]],
                [[2, 2, 1], [0, 2, 3], [1, 3, 4], slice(None)],
                [[2, 0, 1], [1, 2, 3], [4], slice(None)],
                [[0, 1, 2], [4], [1, 3, 4], slice(None)],
                [[0], [0, 2, 3], [1, 3, 4], slice(None)],
                [[0, 2, 1], [3], [4], slice(None)],
                [[0], [4], [1, 3, 4], slice(None)],
                [[1], [0, 2, 3], [1], slice(None)],
                [[[1, 2], [1, 2]], [[0, 1], [2, 3]], [[2, 3], [3, 5]], slice(None)],

                # less dim, ellipsis
                [Ellipsis, [0, 3, 4]],
                [Ellipsis, slice(None), [0, 3, 4]],
                [Ellipsis, slice(None), slice(None), [0, 3, 4]],
                [slice(None), Ellipsis, [0, 3, 4]],
                [slice(None), slice(None), Ellipsis, [0, 3, 4]],
                [slice(None), [0, 2, 3], [1, 3, 4]],
                [slice(None), [0, 2, 3], [1, 3, 4], Ellipsis],
                [Ellipsis, [0, 2, 3], [1, 3, 4], slice(None)],
                [[0], [1, 2, 4]],
                [[0], [1, 2, 4], slice(None)],
                [[0], [1, 2, 4], Ellipsis],
                [[0], [1, 2, 4], Ellipsis, slice(None)],
                [[1], ],
                [[0, 2, 1], [3], [4]],
                [[0, 2, 1], [3], [4], slice(None)],
                [[0, 2, 1], [3], [4], Ellipsis],
                [Ellipsis, [0, 2, 1], [3], [4]],
            ]

            for indexer in indices_to_test:
                assert_get_eq(reference, indexer)
                assert_set_eq(reference, indexer, 1333)
                assert_set_eq(reference,
                              indexer,
                              get_set_tensor(reference, indexer))
            indices_to_test += [
                [slice(None), slice(None), [[0, 1], [1, 0]], [[2, 3], [3, 0]]],
                [slice(None), slice(None), [[2]], [[0, 3], [4, 4]]],
            ]
            for indexer in indices_to_test:
                assert_get_eq(reference, indexer)
                assert_set_eq(reference, indexer, 1333)
                if self.device_type != 'cpu':
                    assert_backward_eq(reference, indexer)

    def test_advancedindex_big(self, device):
        reference = torch.arange(0, 123344, dtype=torch.int, device=device)

        self.assertEqual(reference[[0, 123, 44488, 68807, 123343], ],
                         torch.tensor([0, 123, 44488, 68807, 123343], dtype=torch.int))

    @dtypes(torch.double)
    def test_kthvalue(self, device, dtype):
        SIZE = 50
        x = torch.rand(SIZE, SIZE, SIZE, dtype=dtype, device=device)
        x0 = x.clone()

        k = random.randint(1, SIZE)
        res1val, res1ind = torch.kthvalue(x, k, keepdim=False)
        res2val, res2ind = torch.sort(x)

        self.assertEqual(res1val[:, :], res2val[:, :, k - 1], atol=0, rtol=0)
        self.assertEqual(res1ind[:, :], res2ind[:, :, k - 1], atol=0, rtol=0)
        # test use of result tensors
        k = random.randint(1, SIZE)
        res1val = torch.tensor([], dtype=dtype, device=device)
        res1ind = torch.tensor([], dtype=torch.long, device=device)
        torch.kthvalue(x, k, keepdim=False, out=(res1val, res1ind))
        res2val, res2ind = torch.sort(x)
        self.assertEqual(res1val[:, :], res2val[:, :, k - 1], atol=0, rtol=0)
        self.assertEqual(res1ind[:, :], res2ind[:, :, k - 1], atol=0, rtol=0)

        # test non-default dim
        k = random.randint(1, SIZE)
        res1val, res1ind = torch.kthvalue(x, k, 0, keepdim=False)
        res2val, res2ind = torch.sort(x, 0)
        self.assertEqual(res1val, res2val[k - 1], atol=0, rtol=0)
        self.assertEqual(res1ind, res2ind[k - 1], atol=0, rtol=0)

        # non-contiguous
        y = x.narrow(1, 0, 1)
        y0 = y.contiguous()
        k = random.randint(1, SIZE)
        res1val, res1ind = torch.kthvalue(y, k)
        res2val, res2ind = torch.kthvalue(y0, k)
        self.assertEqual(res1val, res2val, atol=0, rtol=0)
        self.assertEqual(res1ind, res2ind, atol=0, rtol=0)

        # non-contiguous [Reference: https://github.com/pytorch/pytorch/issues/45721]
        non_contig_t = torch.tensor([0, -1, 1, -2, 2], dtype=dtype, device=device)[::2]
        expected_val, expected_ind = non_contig_t.contiguous().kthvalue(2)
        non_contig_cpu_t = non_contig_t.cpu()
        expected_val_cpu, expected_ind_cpu = non_contig_cpu_t.kthvalue(2)

        out_val, out_ind = non_contig_t.kthvalue(2)
        self.assertEqual(expected_val, out_val, atol=0, rtol=0)
        self.assertEqual(expected_ind, out_ind, atol=0, rtol=0)
        self.assertEqual(expected_val_cpu, out_val, atol=0, rtol=0)
        self.assertEqual(expected_ind_cpu, out_ind, atol=0, rtol=0)

        # check that the input wasn't modified
        self.assertEqual(x, x0, atol=0, rtol=0)

        # simple test case (with repetitions)
        y = torch.tensor((3., 5, 4, 1, 1, 5), dtype=dtype, device=device)
        self.assertEqual(torch.kthvalue(y, 3)[0], 3, atol=0, rtol=0)
        self.assertEqual(torch.kthvalue(y, 2)[0], 1, atol=0, rtol=0)

        # simple test case (with NaN)
        SIZE = 50
        x = torch.rand(SIZE, SIZE, SIZE, dtype=dtype, device=device)
        x[torch.arange(SIZE), :, torch.randint(50, (50,))] = nan
        ks = [random.randint(1, SIZE), 1, SIZE, SIZE - 1]
        res2val, res2ind = torch.sort(x)
        for k in ks:
            res1val, res1ind = torch.kthvalue(x, k, keepdim=False)
            self.assertEqual(res1val[:, :], res2val[:, :, k - 1], atol=0, rtol=0)
            self.assertEqual(res1ind[:, :], res2ind[:, :, k - 1], atol=0, rtol=0)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_lu_solve_batched_non_contiguous(self, device, dtype):
        from numpy.linalg import solve
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        A = random_fullrank_matrix_distinct_singular_value(2, 2, dtype=dtype, device='cpu')
        b = torch.randn(2, 2, 2, dtype=dtype, device='cpu')
        x_exp = torch.as_tensor(solve(A.permute(0, 2, 1).numpy(), b.permute(2, 1, 0).numpy())).to(device)
        A = A.to(device).permute(0, 2, 1)
        b = b.to(device).permute(2, 1, 0)
        assert not A.is_contiguous() and not b.is_contiguous(), "contiguous inputs"
        LU_data, LU_pivots = torch.lu(A)
        x = torch.lu_solve(b, LU_data, LU_pivots)
        self.assertEqual(x, x_exp)

    def lu_solve_test_helper(self, A_dims, b_dims, pivot, device, dtype):
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        b = torch.randn(*b_dims, dtype=dtype, device=device)
        A = random_fullrank_matrix_distinct_singular_value(*A_dims, dtype=dtype, device=device)
        LU_data, LU_pivots, info = torch.lu(A, get_infos=True, pivot=pivot)
        self.assertEqual(info, torch.zeros_like(info))
        return b, A, LU_data, LU_pivots

    @skipCPUIfNoLapack
    @skipCUDAIfNoMagma
    @dtypes(torch.double)
    def test_lu_solve(self, device, dtype):
        def sub_test(pivot):
            for k, n in zip([2, 3, 5], [3, 5, 7]):
                b, A, LU_data, LU_pivots = self.lu_solve_test_helper((n,), (n, k), pivot, device, dtype)
                x = torch.lu_solve(b, LU_data, LU_pivots)
                self.assertLessEqual(b.dist(A.mm(x)), 1e-12)

        sub_test(True)
        if self.device_type == 'cuda':
            sub_test(False)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_lu_solve_batched(self, device, dtype):
        def sub_test(pivot):
            def lu_solve_batch_test_helper(A_dims, b_dims, pivot):
                b, A, LU_data, LU_pivots = self.lu_solve_test_helper(A_dims, b_dims, pivot, device, dtype)
                x_exp_list = []
                for i in range(b_dims[0]):
                    x_exp_list.append(torch.lu_solve(b[i], LU_data[i], LU_pivots[i]))
                x_exp = torch.stack(x_exp_list)  # Stacked output
                x_act = torch.lu_solve(b, LU_data, LU_pivots)  # Actual output
                self.assertEqual(x_exp, x_act)  # Equality check
                self.assertLessEqual(b.dist(torch.matmul(A, x_act)), 1e-12)  # Correctness check

            for batchsize in [1, 3, 4]:
                lu_solve_batch_test_helper((5, batchsize), (batchsize, 5, 10), pivot)

        # Tests tensors with 0 elements
        b = torch.randn(3, 0, 3, dtype=dtype, device=device)
        A = torch.randn(3, 0, 0, dtype=dtype, device=device)
        LU_data, LU_pivots = torch.lu(A)
        self.assertEqual(torch.empty_like(b), b.lu_solve(LU_data, LU_pivots))

        sub_test(True)
        if self.device_type == 'cuda':
            sub_test(False)

    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_lu_solve_batched_many_batches(self, device, dtype):
        def run_test(A_dims, b_dims):
            b, A, LU_data, LU_pivots = self.lu_solve_test_helper(A_dims, b_dims, True, device, dtype)
            x = torch.lu_solve(b, LU_data, LU_pivots)
            b_ = torch.matmul(A, x)
            self.assertEqual(b_, b.expand_as(b_))

        run_test((5, 65536), (65536, 5, 10))
        run_test((5, 262144), (262144, 5, 10))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.double)
    def test_lu_solve_batched_broadcasting(self, device, dtype):
        from numpy.linalg import solve
        from torch.testing._internal.common_utils import random_fullrank_matrix_distinct_singular_value

        def run_test(A_dims, b_dims, pivot=True):
            A_matrix_size = A_dims[-1]
            A_batch_dims = A_dims[:-2]
            A = random_fullrank_matrix_distinct_singular_value(A_matrix_size, *A_batch_dims, dtype=dtype)
            b = torch.randn(*b_dims, dtype=dtype)
            x_exp = torch.as_tensor(solve(A.numpy(), b.numpy())).to(dtype=dtype, device=device)
            A, b = A.to(device), b.to(device)
            LU_data, LU_pivots = torch.lu(A, pivot=pivot)
            x = torch.lu_solve(b, LU_data, LU_pivots)
            self.assertEqual(x, x_exp)

        # test against numpy.linalg.solve
        run_test((2, 1, 3, 4, 4), (2, 1, 3, 4, 6))  # no broadcasting
        run_test((2, 1, 3, 4, 4), (4, 6))  # broadcasting b
        run_test((4, 4), (2, 1, 3, 4, 2))  # broadcasting A
        run_test((1, 3, 1, 4, 4), (2, 1, 3, 4, 5))  # broadcasting A & b

    # Assert for illegal dtype would not be raised on XLA
    @onlyOnCPUAndCUDA
    def test_minmax_illegal_dtype(self, device):
        x = torch.randn(5, 5, dtype=torch.float32, device=device)
        valid_values = torch.empty(5, dtype=torch.float32, device=device)
        valid_indices = torch.empty(5, dtype=torch.long, device=device)
        illegal_values = torch.empty(5, dtype=torch.int, device=device)
        illegal_indices = torch.empty(5, dtype=torch.double, device=device)
        torch.max(x, dim=0, out=(valid_values, valid_indices))
        torch.min(x, dim=0, out=(valid_values, valid_indices))
        torch.amax(x, dim=0, out=valid_values)
        torch.amin(x, dim=0, out=valid_values)
        rmsg = r'scalar type|dtype'
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.max(x, dim=0, out=(illegal_values, valid_indices))
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.min(x, dim=0, out=(illegal_values, valid_indices))
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.amax(x, dim=0, out=illegal_values)
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.amin(x, dim=0, out=illegal_values)
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.max(x, dim=0, out=(valid_values, illegal_indices))
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.min(x, dim=0, out=(valid_values, illegal_indices))
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.max(x, dim=0, out=(illegal_values, illegal_indices))
        with self.assertRaisesRegex(RuntimeError, rmsg):
            torch.min(x, dim=0, out=(illegal_values, illegal_indices))

    @dtypes(torch.float, torch.double, torch.int64, torch.int32, torch.int16)
    @dtypesIfCUDA(torch.float, torch.double, torch.int64, torch.int32, torch.int16, torch.half)
    def test_dim_arg_reduction_scalar(self, device, dtype):
        example = 4.0

        x = torch.tensor(example, device=device, dtype=dtype)
        self.assertEqual(x.argmax().item(), 0)
        self.assertEqual(x.argmax(dim=None).item(), 0)
        self.assertEqual(x.argmax(dim=0).item(), 0)
        self.assertEqual(x.argmax(dim=0, keepdim=True), torch.tensor(0, dtype=torch.int64))

        x = torch.tensor(example, device=device, dtype=dtype)
        self.assertEqual(x.argmin().item(), 0)
        self.assertEqual(x.argmin(dim=None).item(), 0)
        self.assertEqual(x.argmin(dim=0).item(), 0)
        self.assertEqual(x.argmin(dim=0, keepdim=True), torch.tensor(0, dtype=torch.int64))


    def test_dim_reduction(self, device):
        example = [[-1, 2, 1], [5, 3, 6]]

        types = [torch.double,
                 torch.float,
                 torch.int64,
                 torch.int32,
                 torch.int16]
        if self.device_type == 'cuda':  # 'cpu' and 'xla' do not support half
            types.append(torch.half)

        sum_dtype = {
            torch.double: torch.double,
            torch.float: torch.float,
            torch.half: torch.half,
            torch.int64: torch.int64,
            torch.int32: torch.int64,
            torch.int16: torch.int64,
        }

        # This won't test for 256bit instructions, since we usually
        # only work on 1 cacheline (1024bit) at a time and these
        # examples aren't big enough to trigger that.
        for dtype in types:
            x = torch.tensor(example, device=device, dtype=dtype)
            self.assertEqual(x.sum().item(), 16)
            self.assertEqual(x.sum(0), torch.tensor([4, 5, 7], dtype=sum_dtype[dtype]))
            self.assertEqual(x.sum(1), torch.tensor([2, 14], dtype=sum_dtype[dtype]))
            y = torch.tensor(example, device=device, dtype=sum_dtype[dtype])
            torch.sum(x, 0, out=y)
            self.assertEqual(x.sum(0), y)

        # Mean not supported for Int types
        for dtype in types[:2]:
            x = torch.tensor(example, device=device, dtype=dtype)
            self.assertEqual(x.mean().item(), 16.0 / 6)
            self.assertEqual(x.mean(0), torch.tensor([2.0, 2.5, 7.0 / 2], dtype=dtype))
            self.assertEqual(x.mean(1), torch.tensor([2.0 / 3, 14.0 / 3], dtype=dtype))
            self.assertEqual(x.mean(), x.mean((0, 1)))

        prod_dtype = {
            torch.double: torch.double,
            torch.float: torch.float,
            torch.half: torch.half,
            torch.int64: torch.int64,
            torch.int32: torch.int64,
            torch.int16: torch.int64
        }

        for dtype in types:
            x = torch.tensor(example, device=device, dtype=dtype)
            self.assertEqual(x.prod().item(), -180)
            self.assertEqual(x.prod(0), torch.tensor([-5, 6, 6], dtype=prod_dtype[dtype]))
            self.assertEqual(x.prod(1), torch.tensor([-2, 90], dtype=prod_dtype[dtype]))

        for dtype in types:
            x = torch.tensor(example, device=device, dtype=dtype)

            self.assertEqual(x.min().item(), -1)
            self.assertEqual(x.argmin().item(), 0)

            # TODO: torch.min does not support the same operation as argmin
            # for the same case, should we enable it?
            self.assertEqual(x.argmin(dim=None).item(), 0)

            self.assertEqual(x.min(0), (torch.tensor([-1, 2, 1], dtype=dtype),
                                        torch.tensor([0, 0, 0], dtype=torch.int64)))
            self.assertEqual(x.amin(0), torch.tensor([-1, 2, 1], dtype=dtype))
            self.assertEqual(x.argmin(0), torch.tensor([0, 0, 0], dtype=torch.int64))

            self.assertEqual(x.min(dim=0, keepdim=True), (torch.tensor([[-1, 2, 1]], dtype=dtype),
                                                          torch.tensor([[0, 0, 0]], dtype=torch.int64)))
            self.assertEqual(x.amin(dim=0, keepdim=True), torch.tensor([[-1, 2, 1]], dtype=dtype))
            self.assertEqual(x.argmin(dim=0, keepdim=True), torch.tensor([[0, 0, 0]], dtype=torch.int64))

            self.assertEqual(x.min(1), (torch.tensor([-1, 3], dtype=dtype),
                                        torch.tensor([0, 1], dtype=torch.int64)))
            self.assertEqual(x.amin(1), torch.tensor([-1, 3], dtype=dtype))
            self.assertEqual(x.argmin(1), torch.tensor([0, 1], dtype=torch.int64))

            self.assertEqual(x.min(dim=1, keepdim=True), (torch.tensor([[-1], [3]], dtype=dtype),
                                                          torch.tensor([[0], [1]], dtype=torch.int64)))
            self.assertEqual(x.amin(dim=1, keepdim=True), torch.tensor([[-1], [3]], dtype=dtype))
            self.assertEqual(x.argmin(dim=1, keepdim=True), torch.tensor([[0], [1]], dtype=torch.int64))

            # test that non-contiguous tensors work
            self.assertEqual(x[:, :2].min().item(), -1)
            self.assertEqual(x[:, :2].amin().item(), -1)
            self.assertEqual(x[:, :2].argmin().item(), 0)

        for dtype in types:
            x = torch.tensor(example, device=device, dtype=dtype)

            self.assertEqual(x.max().item(), 6)
            self.assertEqual(x.amax().item(), 6)
            self.assertEqual(x.argmax().item(), 5)

            self.assertEqual(x.max(0), (torch.tensor([5, 3, 6], dtype=dtype),
                                        torch.tensor([1, 1, 1], dtype=torch.int64)))
            self.assertEqual(x.amax(0), torch.tensor([5, 3, 6], dtype=dtype))
            self.assertEqual(x.argmax(dim=0), torch.tensor([1, 1, 1], dtype=torch.int64))

            self.assertEqual(x.max(dim=0, keepdim=True), (torch.tensor([[5, 3, 6]], dtype=dtype),
                                                          torch.tensor([[1, 1, 1]], dtype=torch.int64)))
            self.assertEqual(x.amax(dim=0, keepdim=True), torch.tensor([[5, 3, 6]], dtype=dtype))
            self.assertEqual(x.argmax(dim=0, keepdim=True), torch.tensor([[1, 1, 1]], dtype=torch.int64))

            self.assertEqual(x.max(1), (torch.tensor([2, 6], dtype=dtype),
                                        torch.tensor([1, 2], dtype=torch.int64)))
            self.assertEqual(x.amax(1), torch.tensor([2, 6], dtype=dtype))
            self.assertEqual(x.argmax(dim=1), torch.tensor([1, 2], dtype=torch.int64))

            self.assertEqual(x.max(1, keepdim=True), (torch.tensor([[2], [6]], dtype=dtype),
                                                      torch.tensor([[1], [2]], dtype=torch.int64)))
            self.assertEqual(x.amax(1, keepdim=True), torch.tensor([[2], [6]], dtype=dtype))
            self.assertEqual(x.argmax(dim=1, keepdim=True), torch.tensor([[1], [2]], dtype=torch.int64))

            # test that non-contiguous tensors work
            self.assertEqual(x[:, :2].max().item(), 5)
            self.assertEqual(x[:, :2].amax().item(), 5)
            self.assertEqual(x[:, :2].argmax().item(), 2)

        dim_red_fns = [
            "mean", "median", "mode", "norm", "prod",
            "std", "sum", "var", "max", "min", "amax", "amin"]

        def normfn_attr(t, dim, keepdim=False, out=None):
            attr = torch.norm
            return attr(t, 2, dim, keepdim, out=out)

        for fn_name in dim_red_fns:
            fn_attr = getattr(torch, fn_name) if fn_name != "norm" else normfn_attr

            def fn(x, dim, keepdim=False, out=None):
                ans = fn_attr(x, dim, keepdim=keepdim, out=out)
                return ans if not istuple(ans) else ans[0]

            def fn_tuple(x, dim, keepdim=False, out=None):
                return fn_attr(x, dim, keepdim=keepdim, out=out)

            def test_multidim(x, dim):
                self.assertEqual(fn(x, dim).unsqueeze(dim), fn(x, dim, keepdim=True))
                self.assertEqual(x.ndimension() - 1, fn(x, dim).ndimension())
                self.assertEqual(x.ndimension(), fn(x, dim, keepdim=True).ndimension())

            # general case
            x = torch.randn(3, 4, 5, device=device)
            dim = random.randint(0, 2)
            test_multidim(x, dim)

            # check 1-d behavior
            x = torch.randn(1, device=device)
            dim = 0
            self.assertEqual(fn(x, dim).shape, ())
            self.assertEqual(fn(x, dim, keepdim=True).shape, (1,))

            # check reducing of a singleton dimension
            dims = [3, 4, 5]
            singleton_dim = random.randint(0, 2)
            dims[singleton_dim] = 1
            x = torch.randn(dims, device=device)
            test_multidim(x, singleton_dim)

            # check reducing median with NaNs
            # If the element in the median is a NaN, there can be issues
            # when comparining with other nan elements
            if fn_name == 'median':
                y = torch.full((1, 3), np.nan, dtype=torch.float64, device=device)
                y[:, :1] = 1.1
                values, indices = fn_tuple(y, dim=1)
                expected_values = torch.tensor([nan], dtype=torch.float64, device=device)
                self.assertEqual(values, expected_values)
                self.assertTrue(torch.isnan(y.flatten()[indices[0]]))

            # check reducing with output kwargs
            if fn_name in ['median', 'mode', 'max', 'min']:
                y = torch.randn(5, 3, device=device)
                values = torch.randn(5, 3, device=device)
                indices = torch.zeros(5, 3, device=device).long() - 1
                fn_tuple(y, 1, keepdim=False, out=(values[:, 1], indices[:, 1]))
                values_expected, indices_expected = fn_tuple(y, 1, keepdim=False)
                self.assertEqual(values[:, 1], values_expected,
                                 msg='{} values with out= kwarg'.format(fn_name))
                self.assertEqual(indices[:, 1], indices_expected,
                                 msg='{} indices with out= kwarg'.format(fn_name))
                continue

            x = torch.randn(5, 3, device=device)
            y = torch.randn(5, 3, device=device)
            fn(y, 1, keepdim=False, out=x[:, 1])
            expected = fn(y, 1, keepdim=False)
            self.assertEqual(x[:, 1], expected, msg='{} with out= kwarg'.format(fn_name))

    @largeCUDATensorTest('10GB')
    def test_reduction_split(self, device):
        # Test reduction when there is a 32bit-indexing split
        # https://github.com/pytorch/pytorch/issues/37583
        input_ = torch.randn(5, 14400, 14400, device=device)
        result = input_.sum(dim=0)
        expect = input_[0] + input_[1] + input_[2] + input_[3] + input_[4]
        self.assertEqual(result, expect)

    @onlyCUDA
    @dtypes(torch.half, torch.float, torch.double)
    def test_reduction_vectorize_along_input_corner(self, device, dtype):
        # 1D case: sum
        size = 1024 * 1024 * 64 + 3
        shift = 1
        x = torch.zeros(size, dtype=dtype, device=device)
        y = x[shift:]
        for i in range(100):
            x.zero_()
            x[i] = 1
            self.assertEqual(x.sum(), 1.0)
            if i < shift:
                self.assertEqual(y.sum(), 0.0)
            else:
                self.assertEqual(y.sum(), 1.0)
        for i in range(1, 100):
            x.zero_()
            x[-i] = 1
            self.assertEqual(x.sum(), 1.0)
            self.assertEqual(y.sum(), 1.0)
        # 1D case: argmax
        size = 1024 * 1024 * 64 + 3
        shift = 1
        ysize = size - shift
        x = torch.zeros(size, dtype=dtype, device=device)
        y = x[shift:]
        for i in range(100):
            x.zero_()
            x[i] = 1
            self.assertEqual(x.argmax().item(), i)
            if i >= shift:
                self.assertEqual(y.argmax().item(), i - shift)
        for i in range(1, 100):
            x.zero_()
            x[-i] = 1
            self.assertEqual(x.argmax().item(), size - i)
            self.assertEqual(y.argmax().item(), ysize - i)
        # 2D case: sum
        size = (7, 1024 * 1024 + 3)
        x = torch.zeros(size, dtype=dtype, device=device)
        for i in range(100):
            x.zero_()
            for j in range(7):
                x[j][i] = j
            xs = x.sum(dim=-1)
            for j in range(7):
                self.assertEqual(xs[j].item(), float(j))
        for i in range(100):
            x.zero_()
            for j in range(7):
                x[j][-i] = j
            xs = x.sum(dim=-1)
            for j in range(7):
                self.assertEqual(xs[j].item(), float(j))
        # 2D case: max/argmax
        size = (7, 1024 * 1024 + 3)
        x = torch.zeros(size, dtype=dtype, device=device)
        for i in range(100):
            x.zero_()
            for j in range(7):
                x[j][i] = j + 1
            xs1 = x.argmax(dim=-1)
            xs2 = x.max(dim=-1).indices
            for j in range(7):
                self.assertEqual(xs1[j].item(), i)
                self.assertEqual(xs2[j].item(), i)
        for i in range(1, 100):
            x.zero_()
            for j in range(7):
                x[j][-i] = j + 1
            xs1 = x.argmax(dim=-1)
            xs2 = x.max(dim=-1).indices
            for j in range(7):
                self.assertEqual(xs1[j].item(), size[1] - i)
                self.assertEqual(xs2[j].item(), size[1] - i)
        # 2D case: min/argmin
        size = (7, 1024 * 1024 + 3)
        x = torch.zeros(size, dtype=dtype, device=device)
        for i in range(100):
            x.zero_()
            for j in range(7):
                x[j][i] = -(j + 1)
            xs1 = x.argmin(dim=-1)
            xs2 = x.min(dim=-1).indices
            for j in range(7):
                self.assertEqual(xs1[j].item(), i)
                self.assertEqual(xs2[j].item(), i)
        for i in range(1, 100):
            x.zero_()
            for j in range(7):
                x[j][-i] = -(j + 1)
            xs1 = x.argmin(dim=-1)
            xs2 = x.min(dim=-1).indices
            for j in range(7):
                self.assertEqual(xs1[j].item(), size[1] - i)
                self.assertEqual(xs2[j].item(), size[1] - i)

    @onlyCUDA
    @dtypes(torch.half, torch.float, torch.double)
    def test_reduction_vectorize_along_output(self, device, dtype):
        def run_test(input_):
            M, N = input_.shape
            input_.zero_()
            for i in range(min(M, N)):
                input_[i][i] = 1
            output1 = input_.argmax(dim=0)
            output2 = input_.sum(dim=0)
            for i in range(min(M, N)):
                self.assertEqual(output1[i], i)
                self.assertEqual(output2[i], 1)
        # vec 4
        run_test(torch.zeros(64, 64, dtype=dtype, device=device))
        # vec 2
        run_test(torch.zeros(64 * 64 + 2, dtype=dtype, device=device)[2:].view(64, 64))
        run_test(torch.zeros(64, 62, dtype=dtype, device=device))
        run_test(torch.zeros(64, 2, dtype=dtype, device=device))
        # vec 1
        run_test(torch.zeros(64 * 64 + 1, dtype=dtype, device=device)[1:].view(64, 64))
        run_test(torch.zeros(64, 61, dtype=dtype, device=device))
        run_test(torch.zeros(64, 1, dtype=dtype, device=device))

    @slowTest
    def test_argminmax_large_axis(self, device):
        # Regression test for gh-32863
        x = torch.zeros(2**31, device=device, dtype=torch.int8)
        x[-1] = 1
        self.assertEqual(x.argmax(0), x.shape[0] - 1)
        self.assertEqual(x.max(0).indices, x.shape[0] - 1)
        x[-1] = -1
        self.assertEqual(x.argmin(0), x.shape[0] - 1)
        self.assertEqual(x.min(0).indices, x.shape[0] - 1)

    def test_argminmax_axis_with_dim_one(self, device):
        # See: https://github.com/pytorch/pytorch/issues/38922
        n = 32768
        x = torch.zeros(1, n)
        self.assertEqual(x.argmax(dim=0), torch.zeros(n, dtype=torch.int64))
        self.assertEqual(x.argmin(dim=0), torch.zeros(n, dtype=torch.int64))

        self.assertEqual(x.argmax(dim=-2), torch.zeros(n, dtype=torch.int64))
        self.assertEqual(x.argmin(dim=-2), torch.zeros(n, dtype=torch.int64))

        self.assertEqual(x.argmax(dim=0, keepdim=True), torch.zeros(1, n, dtype=torch.int64))
        self.assertEqual(x.argmin(dim=0, keepdim=True), torch.zeros(1, n, dtype=torch.int64))

        self.assertEqual(x.argmax(dim=-2, keepdim=True), torch.zeros(1, n, dtype=torch.int64))
        self.assertEqual(x.argmin(dim=-2, keepdim=True), torch.zeros(1, n, dtype=torch.int64))

    def test_remainder_overflow(self, device):
        # Check Integer Overflows
        x = torch.tensor(23500, dtype=torch.int64, device=device)
        q = 392486996410368
        self.assertEqual(x % q, x)
        self.assertEqual(-x % q, q - x)
        self.assertEqual(x % -q, x - q)
        self.assertEqual(-x % -q, -x)

    def test_rpow(self, device):
        m = torch.randn(10, 10, device=device)
        self.assertEqual(torch.pow(2, m), 2**m)

        # test with scalar
        m = torch.randn(1, device=device).squeeze()
        assert m.dim() == 0, "m is intentionally a scalar"
        self.assertEqual(torch.pow(2, m), 2**m)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_symeig(self, device, dtype):
        from torch.testing._internal.common_utils import random_symmetric_matrix

        def run_test(dims, eigenvectors, upper):
            x = random_symmetric_matrix(*dims, dtype=dtype, device=device)
            oute = torch.empty(dims[1:] + dims[:1], dtype=dtype, device=device)
            outv = torch.empty(dims[1:] + dims[:1] * 2, dtype=dtype, device=device)
            torch.symeig(x, eigenvectors=eigenvectors, upper=upper, out=(oute, outv))

            if eigenvectors:
                x_recon = torch.matmul(torch.matmul(outv, torch.diag_embed(oute)), outv.transpose(-2, -1))
                self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using V @ diag(e) @ V.T')
            else:
                eigvals, _ = torch.symeig(x, eigenvectors=True, upper=upper)
                self.assertEqual(eigvals, oute, msg='Eigenvalues mismatch')
                self.assertEqual(torch.empty(0, device=device, dtype=dtype), outv, msg='Eigenvector matrix not empty')

            rese, resv = x.symeig(eigenvectors=eigenvectors, upper=upper)
            self.assertEqual(rese, oute, msg="outputs of symeig and symeig with out don't match")
            self.assertEqual(resv, outv, msg="outputs of symeig and symeig with out don't match")

            # test non-contiguous
            x = random_symmetric_matrix(*dims, dtype=dtype, device=device)
            n_dim = len(dims) + 1
            # Reverse the batch dimensions and the matrix dimensions and then concat them
            x = x.permute(tuple(range(n_dim - 3, -1, -1)) + (n_dim - 1, n_dim - 2))
            assert not x.is_contiguous(), "x is intentionally non-contiguous"
            rese, resv = torch.symeig(x, eigenvectors=eigenvectors, upper=upper)
            if eigenvectors:
                x_recon = torch.matmul(torch.matmul(resv, torch.diag_embed(rese)), resv.transpose(-2, -1))
                self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using V @ diag(e) @ V.T')
            else:
                eigvals, _ = torch.symeig(x, eigenvectors=True, upper=upper)
                self.assertEqual(eigvals, rese, msg='Eigenvalues mismatch')
                self.assertEqual(torch.empty(0, device=device, dtype=dtype), resv, msg='Eigenvector matrix not empty')

        batch_dims_set = [(), (3,), (3, 5), (5, 3, 5)]
        for batch_dims, eigenvectors, upper in product(batch_dims_set, (True, False), (True, False)):
            run_test((5,) + batch_dims, eigenvectors, upper)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_svd(self, device, dtype):
        def run_test(dims, some, compute_uv):
            x = torch.randn(*dims, dtype=dtype, device=device)
            outu = torch.tensor((), dtype=dtype, device=device)
            outs = torch.tensor((), dtype=dtype, device=device)
            outv = torch.tensor((), dtype=dtype, device=device)
            torch.svd(x, some=some, compute_uv=compute_uv, out=(outu, outs, outv))

            if compute_uv:
                if some:
                    x_recon = torch.matmul(outu, torch.matmul(outs.diag_embed(), outv.transpose(-2, -1)))
                    self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using U @ diag(S) @ V.T')
                else:
                    narrow_u = outu[..., :min(*dims[-2:])]
                    narrow_v = outv[..., :min(*dims[-2:])]
                    x_recon = torch.matmul(narrow_u, torch.matmul(outs.diag_embed(), narrow_v.transpose(-2, -1)))
                    self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using U @ diag(S) @ V.T')
            else:
                _, singvals, _ = torch.svd(x, compute_uv=True)
                self.assertEqual(singvals, outs, msg='Singular values mismatch')
                self.assertEqual(outu, torch.zeros_like(outu), msg='U not zero')
                self.assertEqual(outv, torch.zeros_like(outv), msg='V not zero')

            resu, ress, resv = torch.svd(x, some=some, compute_uv=compute_uv)
            self.assertEqual(resu, outu, msg='outputs of svd and svd with out differ')
            self.assertEqual(ress, outs, msg='outputs of svd and svd with out differ')
            self.assertEqual(resv, outv, msg='outputs of svd and svd with out differ')

            # test non-contiguous
            x = torch.randn(*dims, dtype=dtype, device=device)
            n_dim = len(dims)
            # Reverse the batch dimensions and the matrix dimensions and then concat them
            x = x.permute(tuple(range(n_dim - 3, -1, -1)) + (n_dim - 1, n_dim - 2))
            assert not x.is_contiguous(), "x is intentionally non-contiguous"
            resu, ress, resv = torch.svd(x, some=some, compute_uv=compute_uv)
            if compute_uv:
                if some:
                    x_recon = torch.matmul(resu, torch.matmul(ress.diag_embed(), resv.transpose(-2, -1)))
                    self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using U @ diag(S) @ V.T')
                else:
                    narrow_u = resu[..., :min(*dims[-2:])]
                    narrow_v = resv[..., :min(*dims[-2:])]
                    x_recon = torch.matmul(narrow_u, torch.matmul(ress.diag_embed(), narrow_v.transpose(-2, -1)))
                    self.assertEqual(x, x_recon, atol=1e-8, rtol=0, msg='Incorrect reconstruction using U @ diag(S) @ V.T')
            else:
                _, singvals, _ = torch.svd(x, compute_uv=True)
                self.assertEqual(singvals, ress, msg='Singular values mismatch')
                self.assertEqual(resu, torch.zeros_like(resu), msg='U not zero')
                self.assertEqual(resv, torch.zeros_like(resv), msg='V not zero')

        shapes = [(3, 3), (5, 3, 3), (7, 5, 3, 3),  # square matrices
                  (7, 3), (5, 7, 3), (7, 5, 7, 3),  # fat matrices
                  (3, 7), (5, 3, 7), (7, 5, 3, 7)]  # thin matrices
        for dims, some, compute_uv in product(shapes, [True, False], [True, False]):
            run_test(dims, some, compute_uv)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_svd_no_singularvectors(self, device):
        for size in [(5, 5), (5, 20), (20, 5)]:
            a = torch.randn(*size, device=device)
            u, s_expect, v = torch.svd(a)
            u, s_actual, v = torch.svd(a, compute_uv=False)
            self.assertEqual(s_expect, s_actual, msg="Singular values don't match")

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_svd_lowrank(self, device):
        import torch
        from torch.testing._internal.common_utils import random_lowrank_matrix, random_sparse_matrix

        dtype = torch.double

        def run_subtest(actual_rank, matrix_size, batches, device, svd_lowrank, **options):
            density = options.pop('density', 1)
            if isinstance(matrix_size, int):
                rows = columns = matrix_size
            else:
                rows, columns = matrix_size
            if density == 1:
                a_input = random_lowrank_matrix(actual_rank, rows, columns, *batches, device=device, dtype=dtype)
                a = a_input
            else:
                assert batches == ()
                a_input = random_sparse_matrix(rows, columns, density, device=device, dtype=dtype)
                a = a_input.to_dense()

            q = min(*size)
            u, s, v = svd_lowrank(a_input, q=q, **options)

            # check if u, s, v is a SVD
            u, s, v = u[..., :q], s[..., :q], v[..., :q]
            A = u.matmul(s.diag_embed()).matmul(v.transpose(-2, -1))
            self.assertEqual(A, a)

            # check if svd_lowrank produces same singular values as torch.svd
            U, S, V = torch.svd(a)
            self.assertEqual(s.shape, S.shape)
            self.assertEqual(u.shape, U.shape)
            self.assertEqual(v.shape, V.shape)
            self.assertEqual(s, S)

            if density == 1:
                # actual_rank is known only for dense inputs
                #
                # check if pairs (u, U) and (v, V) span the same
                # subspaces, respectively
                u, s, v = u[..., :actual_rank], s[..., :actual_rank], v[..., :actual_rank]
                U, S, V = U[..., :actual_rank], S[..., :actual_rank], V[..., :actual_rank]
                self.assertEqual(u.transpose(-2, -1).matmul(U).det().abs(), torch.ones(batches, device=device, dtype=dtype))
                self.assertEqual(v.transpose(-2, -1).matmul(V).det().abs(), torch.ones(batches, device=device, dtype=dtype))

        all_batches = [(), (1,), (3,), (2, 3)]
        for actual_rank, size, all_batches in [
                (2, (17, 4), all_batches),
                (4, (17, 4), all_batches),
                (4, (17, 17), all_batches),
                (10, (100, 40), all_batches),
                (7, (1000, 1000), [()]),
        ]:
            # dense input
            for batches in all_batches:
                run_subtest(actual_rank, size, batches, device, torch.svd_lowrank)
                if size != size[::-1]:
                    run_subtest(actual_rank, size[::-1], batches, device, torch.svd_lowrank)

        # sparse input
        for size in [(17, 4), (4, 17), (17, 17), (100, 40), (40, 100), (1000, 1000)]:
            for density in [0.005, 0.1]:
                run_subtest(None, size, (), device, torch.svd_lowrank, density=density)

        # jitting support
        jitted = torch.jit.script(torch.svd_lowrank)
        actual_rank, size, batches = 2, (17, 4), ()
        run_subtest(actual_rank, size, batches, device, jitted)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_pca_lowrank(self, device):
        from torch.testing._internal.common_utils import random_lowrank_matrix, random_sparse_matrix

        dtype = torch.double

        def run_subtest(guess_rank, actual_rank, matrix_size, batches, device, pca, **options):
            density = options.pop('density', 1)
            if isinstance(matrix_size, int):
                rows = columns = matrix_size
            else:
                rows, columns = matrix_size
            if density == 1:
                a_input = random_lowrank_matrix(actual_rank, rows, columns, *batches, device=device, dtype=dtype)
                a = a_input
            else:
                a_input = random_sparse_matrix(rows, columns, density, device=device, dtype=dtype)
                a = a_input.to_dense()

            u, s, v = pca(a_input, q=guess_rank, **options)

            self.assertEqual(s.shape[-1], guess_rank)
            self.assertEqual(u.shape[-2], rows)
            self.assertEqual(u.shape[-1], guess_rank)
            self.assertEqual(v.shape[-1], guess_rank)
            self.assertEqual(v.shape[-2], columns)

            A1 = u.matmul(s.diag_embed()).matmul(v.transpose(-2, -1))
            ones_m1 = torch.ones(batches + (rows, 1), dtype=a.dtype, device=device)
            c = a.sum(axis=-2) / rows
            c = c.reshape(batches + (1, columns))
            A2 = a - ones_m1.matmul(c)
            self.assertEqual(A1, A2)

            if density == 1:
                # actual rank is known only for dense input
                detect_rank = (s.abs() > 1e-5).sum(axis=-1)
                self.assertEqual(actual_rank * torch.ones(batches, device=device, dtype=torch.int64), detect_rank)
                U, S, V = torch.svd(A2)
                self.assertEqual(s[..., :actual_rank], S[..., :actual_rank])

        all_batches = [(), (1,), (3,), (2, 3)]
        for actual_rank, size, all_batches in [
                (2, (17, 4), all_batches),
                (2, (100, 4), all_batches),
                (6, (100, 40), all_batches),
                (12, (1000, 1000), [()]),
        ]:
            for batches in all_batches:
                for guess_rank in [
                        actual_rank,
                        actual_rank + 2,
                        actual_rank + 6,
                ]:
                    if guess_rank <= min(*size):
                        run_subtest(guess_rank, actual_rank, size, batches, device, torch.pca_lowrank)
                        run_subtest(guess_rank, actual_rank, size[::-1], batches, device, torch.pca_lowrank)

        # sparse input
        for guess_rank, size in [
                (4, (17, 4)), (4, (4, 17)), (16, (17, 17)),
                (21, (100, 40)), (20, (40, 100)), (600, (1000, 1000))]:
            for density in [0.005, 0.1]:
                run_subtest(guess_rank, None, size, (), device, torch.pca_lowrank, density=density)

        # jitting support
        jitted = torch.jit.script(torch.pca_lowrank)
        guess_rank, actual_rank, size, batches = 2, 2, (17, 4), ()
        run_subtest(guess_rank, actual_rank, size, batches, device, jitted)

    def test_lerp(self, device):
        start_end_shapes = [(), (5,), (5, 5), (5, 5, 5)]
        for shapes in product(start_end_shapes, start_end_shapes):
            start = torch.randn(shapes[0], device=device)
            end = torch.randn(shapes[1], device=device)

            # Tensor weights
            for weight in [torch.randn(shapes[0], device=device), random.random()]:
                actual = torch.lerp(start, end, weight)
                actual_method = start.lerp(end, weight)
                self.assertEqual(actual, actual_method)
                actual_out = torch.Tensor().to(device)
                torch.lerp(start, end, weight, out=actual_out)
                self.assertEqual(actual, actual_out)
                expected = start + weight * (end - start)
                self.assertEqual(expected, actual)

    def _test_logaddexp(self, device, dtype, base2):
        if base2:
            ref_func = np.logaddexp2
            our_func = torch.logaddexp2
        else:
            ref_func = np.logaddexp
            our_func = torch.logaddexp

        def _test_helper(a, b):
            ref = ref_func(a.cpu().numpy(), b.cpu().numpy())
            v = our_func(a, b)
            self.assertEqual(ref, v)

        # simple test
        a = torch.randn(64, 2, dtype=dtype, device=device) - 0.5
        b = torch.randn(64, 2, dtype=dtype, device=device) - 0.5
        _test_helper(a, b)
        _test_helper(a[:3], b[:3])

        # large value test for numerical stability
        a *= 10000
        b *= 10000
        _test_helper(a, b)
        _test_helper(a[:3], b[:3])

        a = torch.tensor([float('inf'), float('-inf'), float('inf'), float("nan")], dtype=dtype, device=device)
        b = torch.tensor([float('inf'), float('-inf'), float('-inf'), float("nan")], dtype=dtype, device=device)
        _test_helper(a, b)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(torch.float32, torch.float64)
    def test_logaddexp(self, device, dtype):
        self._test_logaddexp(device, dtype, base2=False)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(torch.float32, torch.float64)
    def test_logaddexp2(self, device, dtype):
        self._test_logaddexp(device, dtype, base2=True)

    def test_diagflat(self, device):
        dtype = torch.float32
        # Basic sanity test
        x = torch.randn((100,), dtype=dtype, device=device)
        result = torch.diagflat(x)
        expected = torch.diag(x)
        self.assertEqual(result, expected)

        # Test offset
        x = torch.randn((100,), dtype=dtype, device=device)
        result = torch.diagflat(x, 17)
        expected = torch.diag(x, 17)
        self.assertEqual(result, expected)

        # Test where input has more than one dimension
        x = torch.randn((2, 3, 4), dtype=dtype, device=device)
        result = torch.diagflat(x)
        expected = torch.diag(x.contiguous().view(-1))
        self.assertEqual(result, expected)

        # Noncontig input
        x = torch.randn((2, 3, 4), dtype=dtype, device=device).transpose(2, 0)
        self.assertFalse(x.is_contiguous())
        result = torch.diagflat(x)
        expected = torch.diag(x.contiguous().view(-1))
        self.assertEqual(result, expected)

    # Ensure that nuclear_norm's out variant gives the same result as the non-out
    @onlyOnCPUAndCUDA
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.float32, torch.float64)
    def test_nuclear_norm_out(self, device, dtype):
        test_cases = [
            # input size, dim
            ((25, 25), None),
            ((25, 25), (0, 1)),
            ((25, 25), (1, 0)),
            ((25, 25, 25), (2, 0)),
            ((25, 25, 25), (0, 1)),
        ]
        for keepdim in [False, True]:
            for input_size, dim in test_cases:
                msg = f'input_size: {input_size}, dim: {dim}, keepdim: {keepdim}'
                x = torch.randn(*input_size, device=device, dtype=dtype)
                result_out = torch.empty(0, device=device, dtype=dtype)
                if dim is None:
                    result = torch.nuclear_norm(x, keepdim=keepdim)
                    torch.nuclear_norm(x, keepdim=keepdim, out=result_out)
                else:
                    result = torch.nuclear_norm(x, keepdim=keepdim, dim=dim)
                    torch.nuclear_norm(x, keepdim=keepdim, dim=dim, out=result_out)
                self.assertEqual(result, result_out, msg=msg)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_norm(self, device):
        def gen_error_message(input_size, p, keepdim, dim=None):
            return "norm failed for input size %s, p=%s, keepdim=%s, dim=%s" % (
                input_size, p, keepdim, dim)

        for keepdim in [False, True]:
            # full reduction
            x = torch.randn(25, device=device)
            xn = x.cpu().numpy()
            for p in [0, 1, 2, 3, 4, inf, -inf, -1, -2, -3, 1.5]:
                res = x.norm(p, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, keepdims=keepdim)
                self.assertEqual(res, expected, atol=1e-5, rtol=0, msg=gen_error_message(x.size(), p, keepdim))

            # one dimension
            x = torch.randn(25, 25, device=device)
            xn = x.cpu().numpy()
            for p in [0, 1, 2, 3, 4, inf, -inf, -1, -2, -3]:
                dim = 1
                res = x.norm(p, dim, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, dim, keepdims=keepdim)
                msg = gen_error_message(x.size(), p, keepdim, dim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

            # matrix norm
            for p in ['fro', 'nuc']:
                res = x.norm(p, keepdim=keepdim).cpu()
                expected = np.linalg.norm(xn, p, keepdims=keepdim)
                msg = gen_error_message(x.size(), p, keepdim)
                self.assertEqual(res.shape, expected.shape, msg=msg)
                self.assertEqual(res, expected, msg=msg)

            # zero dimensions
            x = torch.randn((), device=device)
            xn = x.cpu().numpy()
            res = x.norm(keepdim=keepdim).cpu()
            expected = np.linalg.norm(xn, keepdims=keepdim)
            msg = gen_error_message(x.size(), None, keepdim)
            self.assertEqual(res.shape, expected.shape, msg=msg)
            self.assertEqual(res, expected, msg=msg)

            # larger tensor sanity check
            self.assertEqual(
                2 * torch.norm(torch.ones(10000), keepdim=keepdim),
                torch.norm(torch.ones(40000), keepdim=keepdim))

            # matrix norm with non-square >2-D tensors, all combinations of reduction dims
            x = torch.randn(5, 6, 7, 8, device=device)
            xn = x.cpu().numpy()
            for p in ['fro', 'nuc']:
                for dim in product(*[list(range(4))] * 2):
                    if dim[0] == dim[1]:
                        continue
                    res = x.norm(p=p, dim=dim, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, ord=p, axis=dim, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim, dim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_norm_complex(self, device):
        def gen_error_message(input_size, p, keepdim, dim=None):
            return "complex norm failed for input size %s, p=%s, keepdim=%s, dim=%s" % (
                input_size, p, keepdim, dim)

        if device == 'cpu':
            for keepdim in [False, True]:
                # vector norm
                x = torch.randn(25, device=device) + 1j * torch.randn(25, device=device)
                xn = x.cpu().numpy()
                for p in [0, 1, 3, inf, -1, -2, -3, -inf]:
                    res = x.norm(p, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, p, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

                # matrix norm
                x = torch.randn(25, 25, device=device) + 1j * torch.randn(25, 25, device=device)
                xn = x.cpu().numpy()
                for p in ['nuc']:
                    res = x.norm(p, keepdim=keepdim).cpu()
                    expected = np.linalg.norm(xn, p, keepdims=keepdim)
                    msg = gen_error_message(x.size(), p, keepdim)
                    self.assertEqual(res.shape, expected.shape, msg=msg)
                    self.assertEqual(res, expected, msg=msg)

            # TODO: remove error test and add functionality test above when 2-norm support is added
            with self.assertRaisesRegex(RuntimeError, r'norm with p=2 not supported for complex tensors'):
                x = torch.randn(2, device=device, dtype=torch.complex64).norm(p=2)

            # TODO: remove error test and add functionality test above when frobenius support is added
            with self.assertRaisesRegex(RuntimeError, r'frobenius norm not supported for complex tensors'):
                x = torch.randn(2, 2, device=device, dtype=torch.complex64).norm(p='fro')

        elif device == 'cuda':
            with self.assertRaisesRegex(RuntimeError, r'"norm_cuda" not implemented for \'ComplexFloat\''):
                (1j * torch.randn(25)).norm()

    @skipCUDAIfNoMagma
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_nuclear_norm_axes_small_brute_force(self, device):
        def check_single_nuclear_norm(x, axes):
            if self.device_type != 'cpu' and randrange(100) < 95:
                return  # too many cpu <==> device copies

            a = np.array(x.cpu(), copy=False)
            expected = np.linalg.norm(a, "nuc", axis=axes)

            ans = torch.norm(x, "nuc", dim=axes)
            self.assertTrue(ans.is_contiguous())
            self.assertEqual(ans.shape, expected.shape)
            self.assertEqual(ans.cpu(), expected, rtol=1e-02, atol=1e-03, equal_nan=True)

            out = torch.zeros(expected.shape, dtype=x.dtype, device=x.device)
            ans = torch.norm(x, "nuc", dim=axes, out=out)
            self.assertIs(ans, out)
            self.assertTrue(ans.is_contiguous())
            self.assertEqual(ans.shape, expected.shape)
            self.assertEqual(ans.cpu(), expected, rtol=1e-02, atol=1e-03, equal_nan=True)

        for n in range(1, 3):
            for m in range(1, 3):
                for axes in permutations([0, 1], 2):
                    # 2d, inner dimensions C
                    x = torch.randn(n, m, device=device)
                    check_single_nuclear_norm(x, axes)

                    # 2d, inner dimensions Fortran
                    x = torch.randn(m, n, device=device).transpose(-1, -2)
                    check_single_nuclear_norm(x, axes)

                    # 2d, inner dimensions non-contiguous
                    x = torch.randn(n, 2 * m, device=device)[:, ::2]
                    check_single_nuclear_norm(x, axes)

                    # 2d, all dimensions non-contiguous
                    x = torch.randn(7 * n, 2 * m, device=device)[::7, ::2]
                    check_single_nuclear_norm(x, axes)

                for o in range(1, 3):
                    for axes in permutations([0, 1, 2], 2):
                        # 3d, inner dimensions C
                        x = torch.randn(o, n, m, device=device)
                        check_single_nuclear_norm(x, axes)

                        # 3d, inner dimensions Fortran
                        x = torch.randn(o, m, n, device=device).transpose(-1, -2)
                        check_single_nuclear_norm(x, axes)

                        # 3d, inner dimensions non-contiguous
                        x = torch.randn(o, n, 2 * m, device=device)[:, :, ::2]
                        check_single_nuclear_norm(x, axes)

                        # 3d, all dimensions non-contiguous
                        x = torch.randn(7 * o, 5 * n, 2 * m, device=device)[::7, ::5, ::2]
                        check_single_nuclear_norm(x, axes)

                    for r in range(1, 3):
                        for axes in permutations([0, 1, 2, 3], 2):
                            # 4d, inner dimensions C
                            x = torch.randn(r, o, n, m, device=device)
                            check_single_nuclear_norm(x, axes)

                            # 4d, inner dimensions Fortran
                            x = torch.randn(r, o, n, m, device=device).transpose(-1, -2)
                            check_single_nuclear_norm(x, axes)

                            # 4d, inner dimensions non-contiguous
                            x = torch.randn(r, o, n, 2 * m, device=device)[:, :, :, ::2]
                            check_single_nuclear_norm(x, axes)

                            # 4d, all dimensions non-contiguous
                            x = torch.randn(7 * r, 5 * o, 11 * n, 2 * m, device=device)[::7, ::5, ::11, ::2]
                            check_single_nuclear_norm(x, axes)

    @skipCUDAIfNoMagma
    def test_nuclear_norm_exceptions(self, device):
        for lst in [], [1], [1, 2]:
            x = torch.tensor(lst, dtype=torch.double, device=device)
            for axes in (), (0,):
                self.assertRaises(RuntimeError, torch.norm, x, "nuc", axes)
            self.assertRaises(IndexError, torch.norm, x, "nuc", (0, 1))

        x = torch.tensor([[0, 1, 2], [3, 4, 5]], dtype=torch.double, device=device)
        self.assertRaisesRegex(RuntimeError, "duplicate or invalid", torch.norm, x, "nuc", (0, 0))
        self.assertRaisesRegex(IndexError, "Dimension out of range", torch.norm, x, "nuc", (0, 2))

    def test_embedding_scalar_weight_error(self, device):
        indices = torch.rand(2, 2, device=device).long()
        weight = torch.tensor(1.0)
        with self.assertRaisesRegex(RuntimeError, "'weight' must be at least 1-D"):
            torch.embedding(weight, indices)

    def test_dist(self, device):
        def run_test(x, y):
            for p in [0, 1, 2, 3, 4, inf, -inf]:
                dist_xy = torch.dist(x, y, p)
                dist_xy_norm = torch.norm(x - y, p)
                self.assertEqual(dist_xy, dist_xy_norm)

        run_test(torch.randn(5, device=device), torch.randn(5, device=device))

        x = torch.zeros(3, device=device)
        y = torch.zeros(3, device=device)
        y[1] = 1.
        run_test(x, y)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_geqrf(self, device):
        a = torch.randn(5, 5, device=device)
        b, c = torch.geqrf(a)
        b_placeholder, c_placeholder = torch.empty_like(b), torch.empty_like(c)
        torch.geqrf(a, out=(b_placeholder, c_placeholder))
        self.assertEqual(b, b_placeholder)
        self.assertEqual(c, c_placeholder)

    def triangular_solve_test_helper(self, A_dims, b_dims, upper, unitriangular,
                                     device, dtype):
        triangle_function = torch.triu if upper else torch.tril
        b = torch.randn(*b_dims, dtype=dtype, device=device)
        A = torch.randn(*A_dims, dtype=dtype, device=device)
        A_triangular = triangle_function(A)
        if unitriangular:
            A_triangular.diagonal(dim1=-2, dim2=-1).fill_(1.)
        return b, A_triangular

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_triangular_solve(self, device, dtype):
        for (k, n), (upper, unitriangular, transpose) in product(zip([2, 3, 5], [3, 5, 7]),
                                                                 product([True, False], repeat=3)):
            b, A = self.triangular_solve_test_helper((n, n), (n, k), upper,
                                                     unitriangular, device, dtype)
            x = torch.triangular_solve(b, A, upper=upper, unitriangular=unitriangular, transpose=transpose)[0]
            if transpose:
                self.assertLessEqual(b.dist(A.t().mm(x)), 4e-12)
            else:
                self.assertLessEqual(b.dist(A.mm(x)), 4e-12)

    @skipCPUIfNoLapack
    @skipCUDAIfNoMagma
    @dtypes(torch.double)
    def test_triangular_solve_batched(self, device, dtype):
        def triangular_solve_batch_helper(A_dims, b_dims, upper, unitriangular, transpose):
            b, A = self.triangular_solve_test_helper(A_dims, b_dims, upper,
                                                     unitriangular, device, dtype)
            x_exp_list = []
            for i in range(b_dims[0]):
                x_exp_list.append(torch.triangular_solve(b[i], A[i], upper=upper,
                                                         unitriangular=unitriangular,
                                                         transpose=transpose)[0])
            x_exp = torch.stack(x_exp_list)  # Stacked output
            x_act = torch.triangular_solve(b, A, upper=upper,
                                           unitriangular=unitriangular,
                                           transpose=transpose)[0]  # Actual output
            self.assertEqual(x_act, x_exp)  # Equality check
            if transpose:
                self.assertLessEqual(b.dist(torch.matmul(A.transpose(-2, -1), x_act)), 3e-12)  # Correctness check
            else:
                self.assertLessEqual(b.dist(torch.matmul(A, x_act)), 3e-12)  # Correctness check

        for (upper, unitriangular, transpose), batchsize in product(product([True, False], repeat=3), [1, 3, 4]):
            triangular_solve_batch_helper((batchsize, 5, 5), (batchsize, 5, 10),
                                          upper, unitriangular, transpose)


    @slowTest
    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_triangular_solve_batched_many_batches(self, device, dtype):
        for upper, transpose, unitriangular in product([True, False], repeat=3):
            b, A = self.triangular_solve_test_helper((256, 256, 5, 5), (5, 1),
                                                     upper, unitriangular, device, dtype)
            x, _ = torch.triangular_solve(b, A,
                                          upper=upper, transpose=transpose, unitriangular=unitriangular)
            if transpose:
                A = A.transpose(-2, -1)
            self.assertEqual(torch.matmul(A, x), b.expand(A.shape[:-2] + (5, 1)))

            b, A = self.triangular_solve_test_helper((3, 3), (512, 512, 3, 1),
                                                     upper, unitriangular, device, dtype)
            x, _ = torch.triangular_solve(b, A, upper=upper, transpose=transpose,
                                          unitriangular=unitriangular)
            if transpose:
                A = A.transpose(-2, -1)
            self.assertEqual(torch.matmul(A, x), b)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @unittest.skipIf(not TEST_SCIPY, "SciPy not found")
    @dtypes(torch.double)
    def test_triangular_solve_batched_broadcasting(self, device, dtype):
        from scipy.linalg import solve_triangular as tri_solve

        def scipy_tri_solve_batched(A, B, upper, trans, diag):
            batch_dims_A, batch_dims_B = A.shape[:-2], B.shape[:-2]
            single_dim_A, single_dim_B = A.shape[-2:], B.shape[-2:]
            expand_dims = tuple(torch._C._infer_size(torch.Size(batch_dims_A),
                                                     torch.Size(batch_dims_B)))
            expand_A = np.broadcast_to(A, expand_dims + single_dim_A)
            expand_B = np.broadcast_to(B, expand_dims + single_dim_B)
            flat_A = expand_A.reshape((-1,) + single_dim_A)
            flat_B = expand_B.reshape((-1,) + single_dim_B)
            flat_X = np.vstack([tri_solve(a, b, lower=(not upper), trans=int(trans), unit_diagonal=diag)
                                for a, b in zip(flat_A, flat_B)])
            return flat_X.reshape(expand_B.shape)

        def run_test(A_dims, b_dims, device, upper, transpose, unitriangular):
            b, A = self.triangular_solve_test_helper(A_dims, b_dims, upper,
                                                     unitriangular, device, dtype)
            x_exp = torch.as_tensor(scipy_tri_solve_batched(A.cpu().numpy(), b.cpu().numpy(),
                                                            upper, transpose, unitriangular))
            x = torch.triangular_solve(b, A, upper=upper, transpose=transpose, unitriangular=unitriangular)[0]

            self.assertEqual(x, x_exp.to(device))

        for upper, transpose, unitriangular in product([True, False], repeat=3):
            # test against scipy.linalg.solve_triangular
            run_test((2, 1, 3, 4, 4), (2, 1, 3, 4, 6), device, upper, transpose, unitriangular)  # no broadcasting
            run_test((2, 1, 3, 4, 4), (4, 6), device, upper, transpose, unitriangular)  # broadcasting b
            run_test((4, 4), (2, 1, 3, 4, 2), device, upper, transpose, unitriangular)  # broadcasting A
            run_test((1, 3, 1, 4, 4), (2, 1, 3, 4, 5), device, upper, transpose, unitriangular)  # broadcasting A & b

    @onlyCPU
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_triangular_solve_singular(self, device, dtype):
        b = torch.rand(3, 1, device=device)
        A = torch.eye(3, 3, device=device)
        A[-1, -1] = 0  # Now A is singular
        err_str = r"triangular_solve_cpu: U\(3,3\) is zero, singular U\."
        with self.assertRaisesRegex(RuntimeError, err_str):
            torch.triangular_solve(b, A)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_lstsq(self, device, dtype):
        def _test_underdetermined(a, b, expectedNorm):
            # underdetermined systems are only supported on CPU
            if self.device_type != 'cpu':
                return

            m = a.size()[0]
            n = a.size()[1]
            assert(m <= n)

            a_copy = a.clone()
            b_copy = b.clone()
            res1 = torch.lstsq(b, a)[0]
            self.assertEqual(a, a_copy, atol=0, rtol=0)
            self.assertEqual(b, b_copy, atol=0, rtol=0)
            self.assertEqual((torch.mm(a, res1) - b).norm(), expectedNorm, atol=1e-8, rtol=0)

            ta = torch.tensor((), dtype=dtype, device=device)
            tb = torch.tensor((), dtype=dtype, device=device)
            res2 = torch.lstsq(b, a, out=(tb, ta))[0]
            self.assertEqual(a, a_copy, atol=0, rtol=0)
            self.assertEqual(b, b_copy, atol=0, rtol=0)
            self.assertEqual((torch.mm(a, res1) - b).norm(), expectedNorm, atol=1e-8, rtol=0)

            res3 = torch.lstsq(b, a, out=(b, a))[0]
            self.assertEqual((torch.mm(a_copy, b) - b_copy).norm(), expectedNorm, atol=1e-8, rtol=0)
            self.assertEqual(res1, tb, atol=0, rtol=0)
            self.assertEqual(res1, b, atol=0, rtol=0)
            self.assertEqual(res1, res2, atol=0, rtol=0)
            self.assertEqual(res1, res3, atol=0, rtol=0)

        def _test_overdetermined(a, b, expectedNorm):
            m = a.size()[0]
            n = a.size()[1]
            assert(m > n)

            def check_norm(a, b, expected_norm, gels_result):
                # Checks |ax - b| and the residual info from the result

                # The first n rows is the least square solution.
                # Rows n to m-1 contain residual information.
                x = gels_result[:n]
                resid_info = gels_result[n:]

                resid_norm = (torch.mm(a, x) - b).norm()
                self.assertEqual(resid_norm, expectedNorm, atol=1e-8, rtol=0)
                self.assertEqual(resid_info.norm(), resid_norm, atol=1e-8, rtol=0)

            a_copy = a.clone()
            b_copy = b.clone()
            res1 = torch.lstsq(b, a)[0]
            self.assertEqual(a, a_copy, atol=0, rtol=0)
            self.assertEqual(b, b_copy, atol=0, rtol=0)
            check_norm(a, b, expectedNorm, res1)

            ta = torch.tensor((), dtype=dtype, device=device)
            tb = torch.tensor((), dtype=dtype, device=device)
            res2 = torch.lstsq(b, a, out=(tb, ta))[0]
            self.assertEqual(a, a_copy, atol=0, rtol=0)
            self.assertEqual(b, b_copy, atol=0, rtol=0)
            check_norm(a, b, expectedNorm, res2)

            res3 = torch.lstsq(b, a, out=(b, a))[0]
            check_norm(a_copy, b_copy, expectedNorm, res3)

            self.assertEqual(res1, tb, atol=0, rtol=0)
            self.assertEqual(res1, b, atol=0, rtol=0)
            self.assertEqual(res1, res2, atol=0, rtol=0)
            self.assertEqual(res1, res3, atol=0, rtol=0)

        # basic test
        expectedNorm = 0
        a = torch.tensor(((1.44, -9.96, -7.55, 8.34),
                          (-7.84, -0.28, 3.24, 8.09),
                          (-4.39, -3.24, 6.27, 5.28),
                          (4.53, 3.83, -6.64, 2.06)), dtype=dtype, device=device).t()
        b = torch.tensor(((8.58, 8.26, 8.48, -5.28),
                          (9.35, -4.43, -0.70, -0.26)), dtype=dtype, device=device).t()
        _test_underdetermined(a, b, expectedNorm)

        # test overdetermined
        expectedNorm = 17.390200628863
        a = torch.tensor(((1.44, -9.96, -7.55, 8.34, 7.08, -5.45),
                          (-7.84, -0.28, 3.24, 8.09, 2.52, -5.70),
                          (-4.39, -3.24, 6.27, 5.28, 0.74, -1.19),
                          (4.53, 3.83, -6.64, 2.06, -2.47, 4.70)), dtype=dtype, device=device).t()
        b = torch.tensor(((8.58, 8.26, 8.48, -5.28, 5.72, 8.93),
                          (9.35, -4.43, -0.70, -0.26, -7.36, -2.52)), dtype=dtype, device=device).t()
        _test_overdetermined(a, b, expectedNorm)

        # test underdetermined
        expectedNorm = 0
        a = torch.tensor(((1.44, -9.96, -7.55),
                          (-7.84, -0.28, 3.24),
                          (-4.39, -3.24, 6.27),
                          (4.53, 3.83, -6.64)), dtype=dtype, device=device).t()
        b = torch.tensor(((8.58, 8.26, 8.48),
                          (9.35, -4.43, -0.70)), dtype=dtype, device=device).t()
        _test_underdetermined(a, b, expectedNorm)

        # test reuse
        expectedNorm = 0
        a = torch.tensor(((1.44, -9.96, -7.55, 8.34),
                          (-7.84, -0.28, 3.24, 8.09),
                          (-4.39, -3.24, 6.27, 5.28),
                          (4.53, 3.83, -6.64, 2.06)), dtype=dtype, device=device).t()
        b = torch.tensor(((8.58, 8.26, 8.48, -5.28),
                          (9.35, -4.43, -0.70, -0.26)), dtype=dtype, device=device).t()
        ta = torch.tensor((), dtype=dtype, device=device)
        tb = torch.tensor((), dtype=dtype, device=device)
        torch.lstsq(b, a, out=(tb, ta))
        self.assertEqual((torch.mm(a, tb) - b).norm(), expectedNorm, atol=1e-8, rtol=0)
        torch.lstsq(b, a, out=(tb, ta))
        self.assertEqual((torch.mm(a, tb) - b).norm(), expectedNorm, atol=1e-8, rtol=0)
        torch.lstsq(b, a, out=(tb, ta))
        self.assertEqual((torch.mm(a, tb) - b).norm(), expectedNorm, atol=1e-8, rtol=0)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @tf32_on_and_off(0.001)
    def test_qr(self, device):
        def run_test(tensor_dims, some):
            A = torch.randn(*tensor_dims, device=device)
            Q, R = torch.qr(A, some=some)

            # Check0: Q[-2:] = (m, n_columns), R[-2:] = (n_columns, n)
            m, n = tensor_dims[-2:]
            n_columns = m if (not some) and m > n else min(m, n)
            self.assertEqual(Q.size(-2), m)
            self.assertEqual(R.size(-1), n)
            self.assertEqual(Q.size(-1), n_columns)

            # Check1: A = QR
            self.assertEqual(A, torch.matmul(Q, R))

            # Check2: A = QR (with out)
            Q_out, R_out = torch.Tensor().to(device), torch.Tensor().to(device)
            torch.qr(A, some=some, out=(Q_out, R_out))
            self.assertEqual(A, torch.matmul(Q_out, R_out))

            # Check3: Q == Q_out, R == R_out
            self.assertEqual(Q, Q_out)
            self.assertEqual(R, R_out)

            # Check4: Q^{T}Q = I, triu(R) = R
            self.assertEqual(torch.matmul(Q.transpose(-2, -1), Q),
                             torch.eye(n_columns, device=device).expand(Q.shape[:-2] + (n_columns, n_columns)))
            self.assertEqual(R.triu(), R)

        tensor_dims_list = [(3, 5), (5, 5), (5, 3),  # Single matrix
                            (7, 3, 5), (7, 5, 5), (7, 5, 3),  # 3-dim Tensors
                            (7, 5, 3, 5), (7, 5, 5, 5), (7, 5, 5, 3)]  # 4-dim Tensors
        for tensor_dims, some in product(tensor_dims_list, [True, False]):
            run_test(tensor_dims, some)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_quantile(self, device, dtype):
        # Generate some random test cases
        ops = ['quantile', 'nanquantile']
        inputs = [tuple(np.random.randint(2, 10, size=i)) for i in range(1, 4)]
        quantiles = [tuple(np.random.rand(i)) for i in range(0, 5)]
        keepdims = [True, False]

        # Add corner cases
        inputs.extend([0.75, (1,), (1, 1), (1, 2, 1)])
        inputs.extend([[float('nan')], [[float('nan'), float('nan')], [1, 2]]])
        inputs.extend([[[float('nan'), float('nan')], [float('nan'), 2]]])
        quantiles.extend([0.5, [0., 1.], np.random.rand(10)])

        # Enumerate all input combinations
        for op, x, q, keepdim in product(ops, inputs, quantiles, keepdims):
            if type(x) is tuple:
                a = torch.randn(x, dtype=dtype, device=device)
                # Make some random elements NaN
                a.masked_fill_(torch.randint_like(a, 20) == 0, float('nan'))
            else:
                a = torch.tensor(x, dtype=dtype, device=device)

            q = torch.tensor(q, dtype=dtype, device=device)

            torch_op = getattr(torch, op)
            numpy_op = getattr(np, op)

            # Compute quantile along every dimension and flattened tensor
            for dim in [None] + list(range(a.ndim)):
                result = torch_op(a, q, dim, keepdim)
                expected = numpy_op(a.cpu().numpy(), q.cpu().numpy(), dim, keepdims=keepdim)
                self.assertEqual(result.cpu(), torch.from_numpy(np.array(expected)).type(result.type()))

                # Test out variation
                out = torch.empty_like(result)
                torch_op(a, q, dim, keepdim, out=out)
                self.assertEqual(out.cpu(), result.cpu())

    def test_quantile_backward(self, device):
        def check(a, q, dim, expected_grad, ops=(torch.quantile, torch.nanquantile)):
            for op in ops:
                t = torch.tensor(a, device=device, requires_grad=True)
                op(t, torch.tensor(q, device=device), dim).sum().backward()
                self.assertEqual(t.grad, expected_grad)

        check([1., 2, 3], 0.5, 0, [0, 1, 0])
        check([1., 2, 3, 4], 0.5, 0, [0, 0.5, 0.5, 0])
        check([3., 1, 4, 2], 0.5, 0, [0.5, 0, 0, 0.5])
        check([1., 2, 3, 4], [0.25, 0.5, 0.75], 0, [0.25, 1.25, 1.25, 0.25])
        check([[1., 2], [2, 1]], 0., 0, [[1, 0], [0, 1]])
        check([[1., 2], [4, 3]], 1., 1, [[0, 1], [1, 0]])
        check([1, float('nan'), 2], 0.5, 0, [0, 1, 0], [torch.quantile])
        check([1, float('nan'), 2], 0.5, 0, [0.5, 0, 0.5], [torch.nanquantile])

    def test_quantile_error(self, device):
        def check(a, q, args, kwargs, message):
            with self.assertRaisesRegex(RuntimeError, r'quantile\(\) ' + message):
                at = torch.tensor(a, device=device)
                qt = torch.tensor(q, device=device) if isinstance(q, list) else q
                torch.quantile(at, qt, *args, **kwargs)

        check([], 0.5, [], {}, r'input tensor must be non-empty')
        check([1.], [[1.]], [], {}, r'q must be a scalar or 1D tensor')
        check([1], 0.5, [], {}, r'input tensor must be either float or double dtype')
        check([1.], [1], [], {}, r'q tensor must be same dtype as the input tensor')
        check([1.], -1., [], {}, r'q must be in the range \[0, 1\] but got -1')
        check([1.], 1.1, [], {}, r'q must be in the range \[0, 1\] but got 1.1')
        check([1.], 0.5, [], {'out': torch.empty([], dtype=torch.float64, device=device)},
              r'out tensor must be same dtype as the input tensor')

        if self.device_type == "cpu":
            check([1.], [0.5, 1.1, -1], [], {}, r'q values must be in the range \[0, 1\]')

        if self.device_type == "cuda":
            with self.assertRaisesRegex(
                    RuntimeError, r'quantile\(\) q tensor must be on the same device as the input tensor'):
                torch.randn(1, device=device).quantile(torch.tensor(0.5))
            with self.assertRaisesRegex(
                    RuntimeError, r'quantile\(\) out tensor must be on the same device as the input tensor'):
                torch.quantile(torch.randn(1, device=device), 0.5, out=torch.scalar_tensor(1))


    def test_random_neg_values(self, device):
        signed_dtypes = [torch.double, torch.float, torch.long, torch.int, torch.short]
        for dtype in signed_dtypes:
            res = torch.rand(SIZE, SIZE).to(device=device, dtype=dtype)
            res.random_(-10, -1)
            self.assertLessEqual(res.max().item(), 9)
            self.assertGreaterEqual(res.min().item(), -10)

    @slowTest
    def test_triu_tril(self, device):
        def gen_mask(shape, diagonal, device, upper):
            mask = torch.zeros(*shape[-2:]).byte()
            for i in range(shape[-2]):
                for j in range(shape[-1]):
                    cond = j - i < diagonal if upper else j - i > diagonal
                    if cond:
                        mask[i, j] = 1
            return mask.expand(*shape).to(device)

        torch_functions = {True: torch.triu, False: torch.tril}
        if TEST_NUMPY:
            numpy_functions = {True: np.triu, False: np.tril}

        # TODO: remove this when bool and half are supported for torch.where
        def bool_half_compat_where(pred, true_tensor, false_tensor, dtype):
            if dtype == torch.bool or dtype == torch.half:
                return torch.where(pred.byte(), true_tensor.byte(), false_tensor.byte()).to(dtype=dtype)
            else:
                return torch.where(pred, true_tensor, false_tensor)

        def run_test(shape, device, diagonal, dtype):
            x = torch.empty(*shape, device=device, dtype=dtype).fill_(2)

            for upper in [True, False]:
                # normal test with mask
                torch_tri_func = torch_functions[upper]
                res1 = torch_tri_func(x, diagonal=diagonal)
                res2 = torch.empty(0, device=device, dtype=dtype)
                torch_tri_func(x, diagonal=diagonal, out=res2)
                exp_mask = gen_mask(shape, diagonal, device, upper)
                expected = bool_half_compat_where(exp_mask, torch.tensor(0).type_as(x), x, dtype)
                self.assertEqual(res1, res2, atol=0, rtol=0)
                self.assertEqual(expected, res1, atol=0, rtol=0)

                # non-contiguous and expanded tensors test
                if 0 not in shape:
                    for s in range(-len(shape), -1):
                        # non-contiguous tensors
                        x_nc = x.clone().transpose(s, s + 1)
                        exp_mask = gen_mask(x_nc.size(), diagonal, device, upper)
                        if 1 not in shape:
                            assert not x_nc.is_contiguous(), "x is intentionally non-contiguous"
                        exp_nc = bool_half_compat_where(exp_mask, torch.tensor(0).type_as(x), x_nc, dtype)
                        self.assertEqual(torch_tri_func(x_nc, diagonal), exp_nc, atol=0, rtol=0)
                        x_nc_is_contiguous = x_nc.is_contiguous()
                        if upper:
                            self.assertEqual(x_nc.triu_(diagonal), exp_nc, atol=0, rtol=0)
                        else:
                            self.assertEqual(x_nc.tril_(diagonal), exp_nc, atol=0, rtol=0)

                        self.assertTrue(x_nc.is_contiguous() == x_nc_is_contiguous,
                                        "contiguity of x_nc should not be changed")

                    # expanded tensors
                    expanded_size = (x.size(0),) + x.size()
                    x_expanded = x.clone().expand(*expanded_size)
                    if x.size(0) != 1:
                        assert 0 in x_expanded.stride(), "x intentionally has 0 in its stride"
                    output = torch_tri_func(x_expanded, diagonal)
                    self.assertEqual(output, expected.expand(expanded_size), atol=0, rtol=0)
                    if x.size(0) != 1:
                        self.assertTrue(0 in x_expanded.stride(),
                                        "geometry of x_expanded should be the same")
                    if upper:
                        self.assertEqual(output, x_expanded.triu_(diagonal), atol=0, rtol=0)
                    else:
                        self.assertEqual(output, x_expanded.tril_(diagonal), atol=0, rtol=0)

                if not TEST_NUMPY:
                    continue

                # numpy test
                numpy_tri_func = numpy_functions[upper]
                self.assertEqual(numpy_tri_func(x.to('cpu').numpy(), diagonal), res1.cpu().numpy())

        diagonals = [-2, -1, 0, 1, 2]
        shapes = [(3, 3), (5, 3, 3), (7, 5, 3, 3),  # square matrices
                  (7, 3), (5, 7, 3), (7, 5, 7, 3),  # fat matrices
                  (3, 7), (5, 3, 7), (7, 5, 3, 7),  # thin matrices
                  (3, 0), (0, 3, 3), (3, 3, 0, 0),  # no numel matrices
                  (3, 1), (5, 3, 1), (7, 5, 3, 1),  # very fat matrices
                  (1, 3), (5, 1, 3), (7, 5, 1, 3),  # very thin matrices
                  (1, 3, 3, 3), (3, 1, 3, 3, 3)]    # unsqueezed batch dimensions
        dtypes = [dtype for dtype in torch.testing.get_all_dtypes() if dtype != torch.bfloat16]
        for s, d, dtype in product(shapes, diagonals, dtypes):
            run_test(s, device, d, dtype)

    @skipCUDANonDefaultStreamIf(True)
    def test_multinomial_alias(self, device):
        # Get probs vector to use in setup
        def get_probs(length, is_contiguous):
            probs = torch.softmax(torch.randn(length), 0)
            if not is_contiguous:
                probs = torch.softmax(torch.randn(length, 2), 0)[:, 1]
            assert not (is_contiguous ^ probs.is_contiguous()), "contiguity requirement not met"
            return probs.to(device)

        for is_contiguous in [True, False]:
            probs = get_probs(4, is_contiguous)
            alias_table, prob_table = torch._multinomial_alias_setup(probs)
            for n_samples in [-1, 1, 10]:
                if n_samples > 0:
                    samples = torch._multinomial_alias_draw(prob_table, alias_table, n_samples)
                    self.assertEqual(prob_table.size(), torch.Size([4]), msg="size mismatch: probability table")
                    self.assertEqual(alias_table.size(), torch.Size([4]), msg="size mismatch: alias table")
                    self.assertEqual(samples.size(), torch.Size([n_samples]), msg="wrong number of samples")
                else:
                    with self.assertRaisesRegex(RuntimeError, "cannot sample <= 0 samples"):
                        torch._multinomial_alias_draw(prob_table, alias_table, n_samples)

            with self.assertRaisesRegex(RuntimeError, "expected 1-D"):
                probs = probs.view(2, 2)
                torch._multinomial_alias_setup(probs)

            with self.assertRaisesRegex(RuntimeError, "expected 1-D"):
                a_t, p_t = torch._multinomial_alias_setup(probs)
                torch._multinomial_alias_draw(p_t.view(2, 2), a_t.view(2, 2))

        MAX_SAMPLES = 200000
        for probs in [get_probs(4, True),
                      torch.tensor([0.8, 0.2], device=device),
                      torch.tensor([0.7, 0.2, 0.1], device=device)]:
            # Check how different the alias distribution and the original distribution are
            alias_dist = torch.zeros_like(probs)
            alias_table, prob_table = torch._multinomial_alias_setup(probs)
            alias_samples = torch._multinomial_alias_draw(prob_table, alias_table, MAX_SAMPLES)
            alias_dist = torch.unique(alias_samples, return_counts=True)[1].to(dtype=probs.dtype) / MAX_SAMPLES
            self.assertEqual(alias_dist, probs, rtol=0.02, atol=0.0,
                             msg="Actual: {}\nExpected: {}".format(alias_dist, probs))

        for probs in [torch.tensor([0.2501, 0.25, 0.2499, 0.25], device=device),
                      torch.tensor([0.8, 0.199, 0.001], device=device),
                      torch.tensor([0.25001, 0.25, 0.24999, 0.25], device=device),
                      torch.tensor([0.33, 0.34, 0.33], device=device),
                      torch.tensor([0.8, 0.1999, 0.0001], device=device)]:
            # Check the difference between the original probabilities and the reconstructed
            # probabilities from the alias and probability tables output by _multinomial_alias_setup
            alias_table, prob_table = torch._multinomial_alias_setup(probs)
            actual = torch.zeros_like(probs)
            for i, vals in enumerate(zip(alias_table, prob_table)):
                idx, p = vals
                actual[i] += p
                actual[idx] += 1. - p
            actual = actual / len(probs)
            self.assertEqual(actual, probs, atol=1e-6, rtol=0)

        # Some special cases
        test_cases = [torch.tensor([1.0, 0.0, 0.0], device=device), torch.tensor([0.0, 1.0], device=device)]
        for probs in test_cases:
            alias_table, prob_table = torch._multinomial_alias_setup(probs)
            alias_samples = torch._multinomial_alias_draw(prob_table, alias_table, MAX_SAMPLES)
            self.assertEqual(alias_samples.unique(), probs.nonzero().squeeze(-1))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_lapack_empty(self, device):
        # FIXME: these are just a selection of LAPACK functions -- we need a general strategy here.
        # The LAPACK functions themselves generally do NOT work with zero sized dimensions, although
        # numpy/sci often has a direct wrapper (e.g. lu_factor) and a wrapper that "does the right thing"
        # (e.g. lu).  We often name our functions identically to the lapack function, so it will take work
        # to name / migrate-to better wrappers.
        def fn(torchfn, *args):
            return torchfn(*tuple(torch.randn(shape, device=device) if isinstance(shape, tuple) else shape
                                  for shape in args))

        # inverse, pinverse
        self.assertEqual((0, 0), fn(torch.inverse, (0, 0)).shape)
        self.assertEqual((5, 0), fn(torch.pinverse, (0, 5)).shape)
        self.assertEqual((0, 5), fn(torch.pinverse, (5, 0)).shape)
        self.assertEqual((0, 0), fn(torch.pinverse, (0, 0)).shape)

        # det, logdet, slogdet
        self.assertEqual(torch.tensor(1., device=device), fn(torch.det, (0, 0)))
        self.assertEqual(torch.tensor(0., device=device), fn(torch.logdet, (0, 0)))
        self.assertEqual((torch.tensor(1., device=device), torch.tensor(0., device=device)),
                         fn(torch.slogdet, (0, 0)))

        # eig, symeig
        evalues, evectors = fn(torch.eig, (0, 0), True)
        self.assertEqual([(0, 2), (0, 0)], [evalues.shape, evectors.shape])
        evalues, evectors = fn(torch.symeig, (0, 0), True)
        self.assertEqual([(0,), (0, 0)], [evalues.shape, evectors.shape])

        # qr
        q, r = fn(torch.qr, (3, 0), True)
        self.assertEqual([(3, 0), (0, 0)], [q.shape, r.shape])
        q, r = fn(torch.qr, (0, 3), True)
        self.assertEqual([(0, 0), (0, 3)], [q.shape, r.shape])
        q, r = fn(torch.qr, (3, 0), False)
        self.assertEqual([(3, 3), (3, 0)], [q.shape, r.shape])

        # lstsq
        self.assertRaises(RuntimeError, lambda: torch.lstsq(torch.randn(0, 0), torch.randn(0, 0)))
        self.assertRaises(RuntimeError, lambda: torch.lstsq(torch.randn(0,), torch.randn(0, 0)))

    def test_roll(self, device):
        numbers = torch.arange(1, 9, device=device)

        single_roll = numbers.roll(1, 0)
        expected = torch.tensor([8, 1, 2, 3, 4, 5, 6, 7], device=device)
        self.assertEqual(single_roll, expected, msg="{} did not equal expected result".format(single_roll))

        roll_backwards = numbers.roll(-2, 0)
        expected = torch.tensor([3, 4, 5, 6, 7, 8, 1, 2], device=device)
        self.assertEqual(roll_backwards, expected, msg="{} did not equal expected result".format(roll_backwards))

        data = numbers.view(2, 2, 2)
        rolled = data.roll(1, 0)
        expected = torch.tensor([5, 6, 7, 8, 1, 2, 3, 4], device=device).view(2, 2, 2)
        self.assertEqual(expected, rolled, msg="{} did not equal expected result: {}".format(rolled, expected))

        data = data.view(2, 4)
        # roll a loop until back where started
        loop_rolled = data.roll(2, 0).roll(4, 1)
        self.assertEqual(data, loop_rolled, msg="{} did not equal the original: {}".format(loop_rolled, data))
        # multiple inverse loops
        self.assertEqual(data, data.roll(-20, 0).roll(-40, 1))
        self.assertEqual(torch.tensor([8, 1, 2, 3, 4, 5, 6, 7], device=device), numbers.roll(1, 0))

        # test non-contiguous
        # strided equivalent to numbers.as_strided(size=(4, 2), stride=(1, 4))
        strided = numbers.view(2, 4).transpose(0, 1)
        self.assertFalse(strided.is_contiguous(), "this test needs a non-contiguous tensor")
        expected = torch.tensor([4, 8, 1, 5, 2, 6, 3, 7]).view(4, 2)
        rolled = strided.roll(1, 0)
        self.assertEqual(expected, rolled,
                         msg="non contiguous tensor rolled to {} instead of {} ".format(rolled, expected))

        # test roll with no dimension specified
        expected = numbers.roll(1, 0).view(2, 4)
        self.assertEqual(expected, data.roll(1), msg="roll with no dims should flatten and roll.")
        self.assertEqual(expected, data.roll(1, dims=None), msg="roll with no dims should flatten and roll.")

        # test roll over multiple dimensions
        expected = torch.tensor([[7, 8, 5, 6], [3, 4, 1, 2]], device=device)
        double_rolled = data.roll(shifts=(2, -1), dims=(1, 0))
        self.assertEqual(double_rolled, expected,
                         msg="should be able to roll over two dimensions, got {}".format(double_rolled))

        self.assertRaisesRegex(RuntimeError, "required", lambda: data.roll(shifts=(), dims=()))
        self.assertRaisesRegex(RuntimeError, "required", lambda: data.roll(shifts=(), dims=1))
        # shifts/dims should align
        self.assertRaisesRegex(RuntimeError, "align", lambda: data.roll(shifts=(1, 2), dims=(1,)))
        self.assertRaisesRegex(RuntimeError, "align", lambda: data.roll(shifts=(1,), dims=(1, 2)))

        # test bool tensor
        t = torch.zeros(6, dtype=torch.bool, device=device)
        t[0] = True
        t[3] = True
        self.assertEqual(torch.tensor([False, True, False, False, True, False]), t.roll(1, 0))

        # test complex tensor
        t = torch.tensor([1, 2 + 1j, 3.5, 4. + 2j, 5j, 6.], device=device)
        t[0] = 1 + 0.5j
        t[3] = 4.
        expected = torch.tensor([6., 1 + 0.5j, 2 + 1j, 3.5, 4., 5j], device=device)
        self.assertEqual(expected, t.roll(1, 0))

    def test_nonzero_empty(self, device):
        def assert_tuple_empty(tup, dim):
            self.assertEqual(dim, len(tup))
            for t in tup:
                self.assertEqual(torch.Size([0]), t.shape)

        x = torch.randn(0, 2, 0, 5, 0, device=device)
        y = torch.nonzero(x)
        z = torch.nonzero(x, as_tuple=True)

        self.assertEqual(0, y.numel())
        self.assertEqual(torch.Size([0, 5]), y.shape)
        assert_tuple_empty(z, 5)

        x = torch.tensor(0.5, device=device)
        y = torch.nonzero(x)
        # nonzero with as_tuple returns a
        # tuple of len 1 for a zero-dim tensor.
        # This is done to match Numpy behavior.
        z = torch.nonzero(x, as_tuple=True)
        self.assertEqual(1, len(z))
        self.assertEqual(torch.zeros(1, dtype=torch.long), z[0])

        x = torch.zeros((), device=device)
        y = torch.nonzero(x)
        z = torch.nonzero(x, as_tuple=True)
        self.assertEqual(torch.Size([0, 0]), y.shape)
        self.assertEqual(1, len(z))
        self.assertEqual(torch.empty(0, dtype=torch.long), z[0])

    # TODO: add torch.complex64, torch.complex128
    @dtypes(torch.float, torch.double)
    def test_normal(self, device, dtype):

        def helper(self, device, dtype, ptype, t_transform, std_transform):
            q = torch.empty(100, 100, dtype=dtype, device=device)

            q.normal_()
            self.assertEqual(t_transform(q).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(q).std(), std_transform(1), atol=0.2, rtol=0)

            q.normal_(2, 3)
            self.assertEqual(t_transform(q).mean(), 2, atol=0.3, rtol=0)
            self.assertEqual(t_transform(q).std(), std_transform(3), atol=0.3, rtol=0)

            q = torch.empty(100, 100, dtype=dtype, device=device)
            q_row1 = q[0:1].clone()
            q[99:100].normal_()
            self.assertEqual(t_transform(q[99:100]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(q[99:100]).std(), std_transform(1), atol=0.2, rtol=0)
            self.assertEqual(t_transform(q[0:1]).clone(), t_transform(q_row1))

            mean = torch.empty(100, 100, dtype=dtype, device=device)
            mean[:50].fill_(ptype(0))
            mean[50:].fill_(ptype(1))

            std = torch.empty(100, 100, dtype=torch.float, device=device)
            std[:, :50] = 4
            std[:, 50:] = 1

            r = torch.normal(mean)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r[:50]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[50:]).mean(), 1, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r).std(), std_transform(1), atol=0.2, rtol=0)

            r.fill_(42)
            r = torch.normal(mean, 3)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r[:50]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[50:]).mean(), 1, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r).std(), std_transform(3), atol=0.2, rtol=0)

            r.fill_(42)
            torch.normal(mean, 3, out=r)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r[:50]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[50:]).mean(), 1, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r).std(), std_transform(3), atol=0.2, rtol=0)

            r.fill_(42)
            r = torch.normal(2, std)
            self.assertFalse(r.dtype.is_complex)
            self.assertEqual(str(r.device), device)
            self.assertEqual(r.mean(), 2, atol=0.2, rtol=0)
            self.assertEqual(r[:, :50].std(), 4, atol=0.3, rtol=0)
            self.assertEqual(r[:, 50:].std(), 1, atol=0.2, rtol=0)

            r.fill_(42)
            torch.normal(2, std, out=r)
            self.assertFalse(r.dtype.is_complex)
            self.assertEqual(str(r.device), device)
            self.assertEqual(r.mean(), 2, atol=0.2, rtol=0)
            self.assertEqual(r[:, :50].std(), 4, atol=0.3, rtol=0)
            self.assertEqual(r[:, 50:].std(), 1, atol=0.2, rtol=0)

            r.fill_(42)
            r = torch.normal(mean, std)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r[:50]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[50:]).mean(), 1, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[:, :50]).std(), std_transform(4), atol=0.3, rtol=0)
            self.assertEqual(t_transform(r[:, 50:]).std(), std_transform(1), atol=0.2, rtol=0)

            r.fill_(42)
            torch.normal(mean, std, out=r)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r[:50]).mean(), 0, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[50:]).mean(), 1, atol=0.2, rtol=0)
            self.assertEqual(t_transform(r[:, :50]).std(), std_transform(4), atol=0.3, rtol=0)
            self.assertEqual(t_transform(r[:, 50:]).std(), std_transform(1), atol=0.2, rtol=0)

            r.fill_(42)
            r = torch.normal(2, 3, (100, 100), dtype=dtype, device=device)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r).mean(), 2, atol=0.3, rtol=0)
            self.assertEqual(t_transform(r).std(), std_transform(3), atol=0.3, rtol=0)

            r.fill_(42)
            torch.normal(2, 3, (100, 100), dtype=dtype, device=device, out=r)
            self.assertEqual(r.dtype, dtype)
            self.assertEqual(str(r.device), device)
            self.assertEqual(t_transform(r).mean(), 2, atol=0.3, rtol=0)
            self.assertEqual(t_transform(r).std(), std_transform(3), atol=0.3, rtol=0)

        if dtype.is_complex:
            helper(self, device, dtype, lambda x: complex(x, x),
                   lambda t: torch.real(t).to(torch.float), lambda mean: mean / math.sqrt(2))
            helper(self, device, dtype, lambda x: complex(x, x),
                   lambda t: torch.imag(t).to(torch.float), lambda mean: mean / math.sqrt(2))
            self.assertRaisesRegex(
                RuntimeError, "normal expects standard deviation to be non-complex",
                lambda: torch.normal(0, torch.empty(100, 100, dtype=dtype, device=device)))
            out = torch.empty(100, 100, dtype=dtype, device=device)
            self.assertRaisesRegex(
                RuntimeError, "normal expects standard deviation to be non-complex",
                lambda: torch.normal(0, torch.empty(100, 100, dtype=dtype, device=device), out=out))
        else:
            helper(self, device, dtype, lambda x: x, lambda t: t, lambda mean: mean)

    @dtypes(torch.float, torch.double, torch.half)
    @dtypesIfCUDA(torch.float, torch.double, torch.half, torch.bfloat16)
    def test_uniform_from_to(self, device, dtype):
        size = 2000
        alpha = 0.1

        float_min = torch.finfo(torch.float).min
        float_max = torch.finfo(torch.float).max
        double_min = torch.finfo(torch.double).min
        double_max = torch.finfo(torch.double).max

        if dtype == torch.bfloat16:
            min_val = -3.389531389251535e+38
            max_val = 3.389531389251535e+38
        else:
            min_val = torch.finfo(dtype).min
            max_val = torch.finfo(dtype).max

        values = [double_min, float_min, -42, 0, 42, float_max, double_max]

        for from_ in values:
            for to_ in values:
                t = torch.empty(size, dtype=dtype, device=device)
                if not (min_val <= from_ <= max_val) or not (min_val <= to_ <= max_val):
                    pass
                elif to_ < from_:
                    self.assertRaisesRegex(
                        RuntimeError,
                        "uniform_ expects to return",
                        lambda: t.uniform_(from_, to_)
                    )
                elif to_ - from_ > max_val:
                    self.assertRaisesRegex(
                        RuntimeError,
                        "uniform_ expects to-from",
                        lambda: t.uniform_(from_, to_)
                    )
                else:
                    t.uniform_(from_, to_)
                    range_ = to_ - from_
                    if not (dtype == torch.bfloat16) and not (
                            dtype == torch.half and device == 'cpu') and not torch.isnan(t).all():
                        delta = alpha * range_
                        double_t = t.to(torch.double)
                        if range_ == 0:
                            self.assertTrue(double_t.min() == from_)
                            self.assertTrue(double_t.max() == to_)
                        elif dtype == torch.half:
                            self.assertTrue(from_ <= double_t.min() <= (from_ + delta))
                            self.assertTrue((to_ - delta) <= double_t.max() <= to_)
                        else:
                            self.assertTrue(from_ <= double_t.min() <= (from_ + delta))
                            self.assertTrue((to_ - delta) <= double_t.max() < to_)

    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_log_normal(self, device, dtype):
        a = torch.tensor([10], dtype=dtype, device=device).log_normal_()
        self.assertEqual(a.dtype, dtype)
        self.assertEqual(a.size(), torch.Size([1]))

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_geometric(self, device, dtype):
        a = torch.tensor([10], dtype=dtype, device=device).geometric_(0.5)
        self.assertEqual(a.dtype, dtype)
        self.assertEqual(a.size(), torch.Size([1]))

    @dtypes(*(torch.testing.get_all_fp_dtypes(include_half=False, include_bfloat16=False)))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False)))
    def test_bernoulli_p(self, device, dtype):
        for trivial_p in ([0, 1], [1, 0, 1, 1, 0, 1]):
            x = torch.tensor(trivial_p, dtype=dtype, device=device)
            self.assertEqual(x.bernoulli().tolist(), trivial_p)

        def isBinary(t):
            return torch.ne(t, 0).mul_(torch.ne(t, 1)).sum().item() == 0

        p = torch.rand(5, 5, dtype=dtype, device=device)
        self.assertTrue(isBinary(p.bernoulli()))

        p = torch.rand(5, dtype=dtype, device=device).expand(5, 5)
        self.assertTrue(isBinary(p.bernoulli()))

        p = torch.rand(5, 5, dtype=dtype, device=device)
        torch.bernoulli(torch.rand_like(p), out=p)
        self.assertTrue(isBinary(p))

    # RngUniform not implemented for Integral type in XLA test
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_half=False, include_bfloat16=False)))
    @dtypesIfCPU(*(torch.testing.get_all_dtypes(include_half=False, include_bfloat16=False, include_complex=False)))
    @dtypesIfCUDA(*(torch.testing.get_all_dtypes(include_bfloat16=False, include_complex=False)))
    def test_bernoulli_self(self, device, dtype):

        def isBinary(t):
            return torch.ne(t, 0).mul_(torch.ne(t, 1)).sum().item() == 0

        t = torch.empty(10, 10, dtype=dtype, device=device)

        t.fill_(2)
        t.bernoulli_(0.5)
        self.assertTrue(isBinary(t))

        for p_dtype in torch.testing.get_all_fp_dtypes(include_half=device.startswith('cuda'),
                                                       include_bfloat16=False):
            p = torch.rand(10, dtype=p_dtype, device=device).expand(10, 10)
            t.fill_(2)
            t.bernoulli_(p)
            self.assertTrue(isBinary(t))

            t.fill_(2)
            torch.bernoulli(torch.rand_like(t, dtype=p_dtype), out=t)
            self.assertTrue(isBinary(t))

            t.fill_(2)
            t.bernoulli_(torch.rand_like(t, dtype=p_dtype))
            self.assertTrue(isBinary(t))

    @slowTest
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_half=False, include_bfloat16=False)))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False)))
    def test_bernoulli_edge_cases(self, device, dtype):
        # Need to draw a lot of samples to cover every random floating point number.
        a = torch.zeros(10000, 10000, dtype=dtype, device=device)  # probability of drawing "1" is 0
        num_ones = (torch.bernoulli(a) == 1).sum()
        self.assertEqual(num_ones, 0)

        b = torch.ones(10000, 10000, dtype=dtype, device=device)  # probability of drawing "1" is 1
        num_zeros = (torch.bernoulli(b) == 0).sum()
        self.assertEqual(num_zeros, 0)

    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_exponential(self, device, dtype):
        a = torch.tensor([10], dtype=dtype, device=device).exponential_(0.5)
        self.assertEqual(a.dtype, dtype)
        self.assertEqual(a.size(), torch.Size([1]))

        # Tests extremal behavior
        tests = ((-0, float('inf')), (0, float('inf')), (float('inf'), 0))
        for test in tests:
            t = torch.empty((1,), device=device, dtype=dtype).exponential_(test[0])
            self.assertTrue(t.item() == test[1])

        # Tests that negative lambda fails
        with self.assertRaises(RuntimeError):
            torch.empty((1,), device=device, dtype=dtype).exponential_(-0.5)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_half=False) +
              torch.testing.get_all_complex_dtypes()))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_half=True) +
                    torch.testing.get_all_complex_dtypes()))
    def test_exp(self, device, dtype):
        for v in (2, -2) + ((1j, 1 + 1j) if dtype.is_complex else ()):
            a = torch.tensor(v, dtype=dtype, device=device) * torch.arange(18, device=device) / 3 * math.pi
            a = a.to(dtype)
            if dtype == torch.bfloat16:
                with self.assertRaises(TypeError):  # compare_with_numpy doesn't support bfloat16
                    self.compare_with_numpy(torch.exp, np.exp, a)
                return
            self.compare_with_numpy(torch.exp, np.exp, a)

            if dtype.is_complex:
                inf_real_zero_imag_in = torch.tensor(complex(float('inf'), 0), device=device, dtype=dtype)
                inf_real_zero_imag_out = torch.exp(inf_real_zero_imag_in).item()
                self.assertTrue(math.isinf(inf_real_zero_imag_out.real))
                if self.device_type == 'cpu':
                    pass
                    # These are commented out because it cannot be consistently reproduced.
                    # This is incorrect. It should be zero. Need fix!
                    # https://github.com/pytorch/pytorch/issues/40590
                    # self.assertNotEqual(inf_real_zero_imag_out.imag, 0)
                    # This is incorrect. They should equal. Need fix!
                    # https://github.com/pytorch/pytorch/issues/40590
                    # with self.assertRaises(AssertionError):
                    #     self.compare_with_numpy(torch.exp, np.exp, inf_real_zero_imag_in)
                else:
                    self.assertEqual(inf_real_zero_imag_out.imag, 0, atol=0, rtol=0)
                    self.compare_with_numpy(torch.exp, np.exp, inf_real_zero_imag_in)

                zero_real_inf_imag_in = torch.tensor(complex(0, float('inf')), device=device, dtype=dtype)
                zero_real_inf_imag_out = torch.exp(zero_real_inf_imag_in).item()
                self.assertTrue(math.isnan(zero_real_inf_imag_out.real))
                self.assertTrue(math.isnan(zero_real_inf_imag_out.imag))
                # Ensure we are notified when NumPy changes its behavior
                self.compare_with_numpy(torch.exp, np.exp, zero_real_inf_imag_in)

                inf_real_imag_in = torch.tensor(complex(float('inf'), float('inf')), device=device, dtype=dtype)
                inf_real_imag_out = torch.exp(inf_real_imag_in).item()
                if self.device_type == 'cpu':
                    pass
                    # This is incorrect. Need fix! https://github.com/pytorch/pytorch/issues/40590
                    # This is commented out because it cannot be consistently reproduced.
                    # with self.assertRaises(AssertionError):
                    #     self.compare_with_numpy(torch.exp, np.exp, inf_real_imag_in)
                else:
                    self.assertTrue(math.isinf(inf_real_imag_out.real))
                    self.assertTrue(math.isnan(inf_real_imag_out.imag))
                    self.compare_with_numpy(torch.exp, np.exp, inf_real_imag_in)

                inf_real_nan_imag_in = torch.tensor(complex(float('inf'), float('nan')), device=device, dtype=dtype)
                inf_real_nan_imag_out = torch.exp(inf_real_nan_imag_in).item()
                if self.device_type == 'cpu':
                    pass
                    # This is incorrect. It should be inf. Need fix! https://github.com/pytorch/pytorch/issues/40590
                    # This is commented out because it cannot be consistently reproduced.
                    # with self.assertRaises(AssertionError):
                    #     self.compare_with_numpy(torch.exp, np.exp, inf_real_nan_imag_in)
                else:
                    self.assertTrue(math.isinf(inf_real_nan_imag_out.real))
                    self.assertTrue(math.isnan(inf_real_nan_imag_out.imag))
                    self.compare_with_numpy(torch.exp, np.exp, inf_real_nan_imag_in)

                nan_real_inf_imag_in = torch.tensor(complex(float('nan'), float('inf')), device=device, dtype=dtype)
                nan_real_inf_imag_out = torch.exp(nan_real_inf_imag_in).item()
                self.assertTrue(math.isnan(nan_real_inf_imag_out.real))
                self.assertTrue(math.isnan(nan_real_inf_imag_out.imag))
                # Ensure we are notified when NumPy changes its behavior
                self.compare_with_numpy(torch.exp, np.exp, nan_real_inf_imag_in)

    @skipIfNoSciPy
    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_uniform_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for from_ in [-42, 0, 4.2]:
            for to_ in [-4.2, 0, 42]:
                if to_ > from_:
                    t = torch.empty(size, dtype=dtype, device=device).uniform_(from_, to_)
                    res = stats.kstest(t.cpu().to(torch.double), 'uniform', args=(from_, (to_ - from_)))
                    self.assertTrue(res.statistic < 0.1)

    @skipIfNoSciPy
    @dtypes(*torch.testing.get_all_fp_dtypes(include_bfloat16=False))
    @dtypesIfCUDA(*torch.testing.get_all_fp_dtypes())
    def test_normal_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for mean in [-10, 0, 50]:
            for std in [1, 5, 10]:
                t = torch.empty(size, dtype=dtype, device=device).normal_(mean=mean, std=std)
                res = stats.kstest(t.cpu().to(torch.double), 'norm', args=(mean, std))
                self.assertTrue(res.statistic < 0.1)

    @skipIfNoSciPy
    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_lognormal_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for mean in [-3, 0, 7]:
            for std in [1, 5, 7]:
                t = torch.empty(size, dtype=dtype, device=device).log_normal_(mean=mean, std=std)
                res = stats.kstest(t.cpu().to(torch.double), 'lognorm', args=(std, 0, math.exp(mean)))
                if dtype == torch.half:
                    self.assertTrue(res.statistic < 0.3)
                else:
                    self.assertTrue(res.statistic < 0.1)

    @skipIfNoSciPy
    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_exponential_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for lambd in [0.5, 1.0, 5.0]:
            t = torch.empty(size, dtype=dtype, device=device).exponential_(lambd=lambd)
            res = stats.kstest(t.cpu().to(torch.double), 'expon', args=(0, 1 / lambd,))
            self.assertTrue(res.statistic < 0.1)

    @skipIfNoSciPy
    @dtypes(*torch.testing.get_all_fp_dtypes())
    def test_cauchy_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for median in [-10, 0, 50]:
            for sigma in [0.5, 1.0, 10.0]:
                t = torch.empty(size, dtype=dtype, device=device).cauchy_(median=median, sigma=sigma)
                res = stats.kstest(t.cpu().to(torch.double), 'cauchy', args=(median, sigma))
                self.assertTrue(res.statistic < 0.1)

    @skipIfNoSciPy
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_geometric_kstest(self, device, dtype):
        from scipy import stats
        size = 1000
        for p in [0.2, 0.5, 0.8]:
            t = torch.empty(size, dtype=dtype, device=device).geometric_(p=p)
            actual = np.histogram(t.cpu().to(torch.double), np.arange(1, 100))[0]
            expected = stats.geom(p).pmf(np.arange(1, 99)) * size
            res = stats.chisquare(actual, expected)
            self.assertEqual(res.pvalue, 1.0, atol=0.1, rtol=0)

    def test_sign(self, device):
        for dtype in torch.testing.get_all_math_dtypes(device):
            if dtype.is_complex:
                continue

            # Include NaN for floating point numbers
            if dtype.is_floating_point:
                dt_info = torch.finfo(dtype)

                # Create tensor (with NaN checking)
                a = torch.tensor([float('nan'), -12, 0, 71, dt_info.min, dt_info.max], device=device, dtype=dtype)
                a_target = torch.tensor([0, -1, 0, 1, -1, 1], device=device, dtype=dtype)

            else:
                dt_info = torch.iinfo(dtype)

                # If unsigned type, everything should be >= 0
                if dt_info.min == 0:
                    a = torch.tensor([12, 0, 71, dt_info.min, dt_info.max], device=device, dtype=dtype)
                    a_target = torch.tensor([1, 0, 1, 0, 1], device=device, dtype=dtype)
                else:
                    a = torch.tensor([-12, 0, 71, dt_info.min, dt_info.max], device=device, dtype=dtype)
                    a_target = torch.tensor([-1, 0, 1, -1, 1], device=device, dtype=dtype)

            self.assertEqual(a.sign(), a_target, msg='sign device={} dtype={}'.format(device, dtype))
            self.assertEqual(torch.sign(a), a_target, msg='sign device={} dtype={}'.format(device, dtype))

            out = torch.empty_like(a)
            torch.sign(a, out=out)
            self.assertEqual(out, a_target, msg='sign_out device={} dtype={}'.format(device, dtype))

            a.sign_()
            self.assertEqual(a, a_target, msg='sign_ device={} dtype={}'.format(device, dtype))

        # Include test for bool dtype
        a_bool = torch.tensor([True, True, False, float('nan')], device=device).bool()
        a_bool_target = torch.tensor([True, True, False, True], device=device).bool()
        self.assertEqual(a_bool.sign(), a_bool_target, msg='sign device={} dtype=bool'.format(device))
        self.assertEqual(torch.sign(a_bool), a_bool_target, msg='sign device={} dtype=bool'.format(device))

        a_out = torch.empty_like(a_bool)
        torch.sign(a_bool, out=a_out)
        self.assertEqual(a_out, a_bool_target, msg='sign_out device={} dtype=bool'.format(device))

        a_bool.sign_()
        self.assertEqual(a_bool, a_bool_target, msg='sign_ device={} dtype=bool'.format(device))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.torch.testing.get_all_fp_dtypes()))
    def test_signbit_float(self, device, dtype):
        t = torch.randn(5, 5, device=device)

        if dtype == torch.bfloat16:
            t_bf16 = torch.tensor([1, 0, -1], device=device, dtype=dtype)
            self.assertEqual(torch.signbit(t_bf16), torch.tensor([False, False, True]))
        else:
            self.compare_with_numpy(torch.signbit, np.signbit, t)

        t_target = torch.signbit(t)
        out = torch.empty_like(t, device=device, dtype=torch.bool)
        torch.signbit(t, out=out)
        self.assertEqual(out, t_target)

        t_sp = (0, float('inf'), -float('inf'), float('nan'))
        if dtype == torch.bfloat16:
            t_sp_df16 = torch.tensor(t_sp, device=device, dtype=dtype)
            self.assertEqual(torch.signbit(t_sp_df16), torch.tensor([False, False, True, False]))
        else:
            self.compare_with_numpy(torch.signbit, np.signbit, t_sp, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + [torch.bool]))
    def test_signbit_int_and_bool(self, device, dtype):
        t = torch.randint(-5, 5, (5, 5), device=device)
        self.compare_with_numpy(torch.signbit, np.signbit, t)

        t_target = torch.signbit(t)
        out = torch.empty_like(t, device=device, dtype=torch.bool)
        torch.signbit(t, out=out)
        self.assertEqual(out, t_target)

    @dtypes(torch.complex64, torch.complex128)
    def test_signbit_complex(self, device, dtype):
        vals = (complex(0, -1), complex(-1, 2))
        t = torch.tensor(vals, device=device, dtype=dtype)
        out = torch.empty_like(t).real.bool()

        with self.assertRaisesRegex(RuntimeError, 'signbit is not implemented for complex tensors.'):
            torch.signbit(t)
        with self.assertRaisesRegex(RuntimeError, 'signbit is not implemented for complex tensors.'):
            torch.signbit(t, out=out)

    @dtypes(torch.cfloat, torch.cdouble)
    def test_sgn(self, device, dtype):
        x = torch.randn(100, dtype=dtype)
        angle = x.angle()
        out = x.sgn()
        self.assertEqual(out.angle(), angle)
        self.assertEqual(out.abs(), torch.ones_like(x).real)

        x_out = torch.empty_like(x)
        torch.sgn(x, out=x_out)
        self.assertEqual(x_out.angle(), angle)
        self.assertEqual(x_out.abs(), torch.ones_like(x).real)

    @dtypes(*(torch.testing.get_all_dtypes(include_bool=False)))
    def test_signbit_non_boolean_output(self, device, dtype):
        # test non-boolean tensors as the `out=` parameters
        # boolean outputs are tested in the above testcases
        t = torch.randn(5, 5)
        out = torch.empty_like(t, dtype=dtype)
        with self.assertRaisesRegex(RuntimeError, 'does not support non-boolean outputs'):
            torch.signbit(t, out=out)

    def test_logical_any(self, device):
        x = torch.zeros([2, 3, 400], dtype=torch.uint8, device=device)

        self.assertEqual(
            torch.tensor(0, dtype=torch.uint8, device=device),
            x.any())

        self.assertEqual(
            torch.zeros([1, 3, 400], dtype=torch.uint8, device=device),
            x.any(0, keepdim=True))

        self.assertEqual(
            torch.zeros([2, 1, 400], dtype=torch.uint8, device=device),
            x.any(1, keepdim=True))

        self.assertEqual(
            torch.zeros([2, 3, 1], dtype=torch.uint8, device=device),
            x.any(2, keepdim=True))

        # set the last element to 0
        x[-1][-1][-1] = 1

        self.assertEqual(
            torch.tensor(1, dtype=torch.uint8, device=device),
            x.any())

        y = torch.zeros([1, 3, 400], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 1
        self.assertEqual(y, x.any(0, keepdim=True))

        y = torch.zeros([2, 1, 400], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 1
        self.assertEqual(y, x.any(1, keepdim=True))

        y = torch.zeros([2, 3, 1], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 1
        self.assertEqual(y, x.any(2, keepdim=True))

    def test_logical_all(self, device):
        x = torch.ones([2, 3, 400], dtype=torch.uint8, device=device)

        self.assertEqual(
            torch.tensor(1, dtype=torch.uint8, device=device),
            x.all())

        self.assertEqual(
            torch.ones([1, 3, 400], dtype=torch.uint8, device=device),
            x.all(0, keepdim=True))

        self.assertEqual(
            torch.ones([2, 1, 400], dtype=torch.uint8, device=device),
            x.all(1, keepdim=True))

        self.assertEqual(
            torch.ones([2, 3, 1], dtype=torch.uint8, device=device),
            x.all(2, keepdim=True))

        # set the last element to 0
        x[-1][-1][-1] = 0

        self.assertEqual(
            torch.tensor(0, dtype=torch.uint8, device=device),
            x.all())

        y = torch.ones([1, 3, 400], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 0
        self.assertEqual(y, x.all(0, keepdim=True))

        y = torch.ones([2, 1, 400], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 0
        self.assertEqual(y, x.all(1, keepdim=True))

        y = torch.ones([2, 3, 1], dtype=torch.uint8, device=device)
        y[-1][-1][-1] = 0
        self.assertEqual(y, x.all(2, keepdim=True))

    def test_pairwise_distance_empty(self, device):
        shape = (2, 0)
        x = torch.randn(shape, device=device)
        y = torch.randn(shape, device=device)

        self.assertEqual(torch.zeros(2, device=device), torch.pairwise_distance(x, y))
        self.assertEqual(torch.zeros((2, 1), device=device), torch.pairwise_distance(x, y, keepdim=True))

        shape = (0, 2)
        x = torch.randn(shape, device=device)
        y = torch.randn(shape, device=device)
        self.assertEqual(torch.zeros(0, device=device), torch.pairwise_distance(x, y))
        self.assertEqual(torch.zeros((0, 1), device=device), torch.pairwise_distance(x, y, keepdim=True))

    def test_pdist_empty(self, device):
        shape = (0, 2)
        x = torch.randn(shape, device=device)
        self.assertEqual(torch.empty(0, device=device), torch.pdist(x))

        shape = (1, 2)
        x = torch.randn(shape, device=device)
        self.assertEqual(torch.empty(0, device=device), torch.pdist(x))

        shape = (3, 0)
        x = torch.randn(shape, device=device)
        self.assertEqual(torch.zeros(3, device=device), torch.pdist(x))

    def test_cdist_empty(self, device):
        x = torch.randn((0, 5), device=device)
        y = torch.randn((4, 5), device=device)
        self.assertEqual(torch.empty(0, 4, device=device), torch.cdist(x, y))

        x = torch.randn((2, 5), device=device)
        y = torch.randn((0, 5), device=device)
        self.assertEqual(torch.empty(2, 0, device=device), torch.cdist(x, y))

        x = torch.randn((2, 0), device=device)
        y = torch.randn((3, 0), device=device)
        self.assertEqual(torch.zeros(2, 3, device=device), torch.cdist(x, y))

        x = torch.randn((2, 0), device=device)
        y = torch.randn((0, 0), device=device)
        self.assertEqual(torch.empty(2, 0, device=device), torch.cdist(x, y))

    def _brute_cdist(self, x, y, p=2):
        r1 = x.shape[-2]
        r2 = y.shape[-2]
        if r1 == 0 or r2 == 0:
            return torch.empty(r1, r2, device=x.device)
        return torch.norm(x[..., None, :] - y[..., None, :, :], p=p, dim=-1)

    def test_cdist_norm(self, device):
        for r1 in [3, 4, 5, 6]:
            for m in [2, 3, 4, 10]:
                for r2 in [4, 6, 7, 8]:
                    for p in [0, 1, 2, 3, 1.5, 2.5, float('inf')]:
                        x = torch.randn(r1, m, device=device)
                        y = torch.randn(r2, m, device=device)
                        if p == 2:
                            for cm in ['use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
                                actual = torch.cdist(x, y, p=2, compute_mode=cm)
                                expected = self._brute_cdist(x, y, p=2)
                                self.assertEqual(expected, actual, rtol=0, atol=0.02)
                        else:
                            actual = torch.cdist(x, y, p=p)
                            expected = self._brute_cdist(x, y, p=p)
                            self.assertEqual(expected, actual)

    def test_cdist_norm_batch(self, device):
        for r1 in [3, 4, 5, 6]:
            for m in [2, 3, 4, 10]:
                for r2 in [4, 6, 7, 8]:
                    for p in [0, 1, 2, 3, 1.5, 2.5, float('inf')]:
                        x = torch.randn(2, 3, 6, r1, m, device=device)
                        y = torch.randn(2, 3, 6, r2, m, device=device)
                        if p == 2:
                            for cm in ['use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
                                actual = torch.cdist(x, y, p=2, compute_mode=cm)
                                expected = self._brute_cdist(x, y, p=2)
                                self.assertEqual(expected, actual, rtol=0, atol=0.02)
                        else:
                            actual = torch.cdist(x, y, p=p)
                            expected = self._brute_cdist(x, y, p=p)
                            self.assertEqual(expected, actual)

    @tf32_on_and_off(0.005)
    def test_cdist_large(self, device):
        for cm in ['use_mm_for_euclid_dist_if_necessary', 'use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
            x = torch.randn(1000, 10, device=device)
            y = torch.randn(1000, 10, device=device)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertEqual(expected, actual)

    @slowTest
    @tf32_on_and_off(0.01)
    def test_cdist_large_batch(self, device):
        for cm in ['use_mm_for_euclid_dist_if_necessary', 'use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
            x = torch.randn(4, 3, 1000, 10, device=device)
            y = torch.randn(4, 3, 1000, 10, device=device)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertEqual(expected, actual)

    @tf32_on_and_off(0.005)
    def test_cdist_non_contiguous(self, device):
        for cm in ['use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
            x = torch.randn(5, 7, device=device).transpose(-1, -2)
            y = torch.randn(5, 3, device=device).transpose(-1, -2)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertFalse(x.is_contiguous())
            self.assertFalse(y.is_contiguous())
            self.assertEqual(expected, actual)

            x = torch.randn(7, 5, device=device)
            y = torch.randn(5, 3, device=device).t()
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertTrue(x.is_contiguous())
            self.assertFalse(y.is_contiguous())
            self.assertEqual(expected, actual)

            x = torch.randn(5, 7, device=device).t()
            y = torch.randn(3, 5, device=device)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertFalse(x.is_contiguous())
            self.assertTrue(y.is_contiguous())
            self.assertEqual(expected, actual)

    @tf32_on_and_off()
    def test_cdist_non_contiguous_batch(self, device):
        for cm in ['use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
            x = torch.randn(4, 3, 2, 5, 7, device=device).transpose(-1, -2)
            y = torch.randn(4, 3, 2, 5, 3, device=device).transpose(-1, -2)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertFalse(x.is_contiguous())
            self.assertFalse(y.is_contiguous())
            self.assertEqual(expected, actual)

            x = torch.randn(7, 2, 7, 5, device=device)
            y = torch.randn(7, 2, 5, 3, device=device).transpose(-1, -2)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertTrue(x.is_contiguous())
            self.assertFalse(y.is_contiguous())
            self.assertEqual(expected, actual)

            x = torch.randn(4, 5, 7, device=device).transpose(-1, -2)
            y = torch.randn(4, 3, 5, device=device)
            actual = torch.cdist(x, y, p=2, compute_mode=cm)
            expected = self._brute_cdist(x, y, p=2)
            self.assertFalse(x.is_contiguous())
            self.assertTrue(y.is_contiguous())
            self.assertEqual(expected, actual)

    def test_multinomial_constraints(self, device):
        x = torch.empty(1, 2, 3, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "prob_dist must be 1 or 2 dim",
            lambda: torch.multinomial(x, 2))
        x = torch.empty(1, 2, dtype=torch.long, device=device)
        self.assertRaisesRegex(
            RuntimeError, "multinomial only supports floating-point dtypes for input",
            lambda: torch.multinomial(x, 2))
        x = torch.empty(1, 2, dtype=torch.double, device=device)
        y = torch.empty(1, 2, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "multinomial expects Long tensor out",
            lambda: torch.multinomial(x, 2, out=y))
        x = torch.empty(2, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "cannot sample n_sample <= 0 samples",
            lambda: torch.multinomial(x, 0))
        x = torch.empty(2, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "cannot sample n_sample <= 0 samples",
            lambda: torch.multinomial(x, -1))
        x = torch.empty(2, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "cannot sample n_sample > prob_dist",
            lambda: torch.multinomial(x, 3, False))
        x = torch.empty(16777217, dtype=torch.double, device=device)
        self.assertRaisesRegex(
            RuntimeError, "number of categories cannot exceed",
            lambda: torch.multinomial(x, 3))

    def test_add(self, device):
        dtypes = [torch.float, torch.double] + torch.testing.get_all_complex_dtypes()
        for dtype in dtypes:
            # [res] torch.add([res,] tensor1, tensor2)
            m1 = torch.randn(100, 100, dtype=dtype, device=device)
            v1 = torch.randn(100, dtype=dtype, device=device)

            # contiguous
            res1 = torch.add(m1[4], v1)
            res2 = res1.clone().zero_()
            for i in range(m1.size(1)):
                res2[i] = m1[4, i] + v1[i]
            self.assertEqual(res1, res2)

            m1 = torch.randn(100, 100, device=device)
            v1 = torch.randn(100, device=device)

            # non-contiguous
            res1 = torch.add(m1[:, 4], v1)
            res2 = res1.clone().zero_()
            for i in range(m1.size(0)):
                res2[i] = m1[i, 4] + v1[i]
            self.assertEqual(res1, res2)

            # [res] torch.add([res,] tensor, value)
            m1 = torch.randn(10, 10, device=device)

            # contiguous
            res1 = m1.clone()
            res1[3].add_(2)
            res2 = m1.clone()
            for i in range(m1.size(1)):
                res2[3, i] = res2[3, i] + 2
            self.assertEqual(res1, res2)

            # non-contiguous
            m1 = torch.randn(10, 10, device=device)
            res1 = m1.clone()
            res1[:, 3].add_(2)
            res2 = m1.clone()
            for i in range(m1.size(0)):
                res2[i, 3] = res2[i, 3] + 2
            self.assertEqual(res1, res2)

            # inter-type
            m1 = torch.randn(10, 10, dtype=dtype, device=device)
            self.assertEqual(m1 + 3, m1 + torch.tensor(3))
            self.assertEqual(3 + m1, torch.tensor(3) + m1)

            # contiguous + non-contiguous
            m1 = torch.randn(10, 10, dtype=dtype, device=device)
            m2 = torch.randn(10, 10, dtype=dtype, device=device).t()
            res = m1 + m2
            self.assertTrue(res.is_contiguous())
            self.assertEqual(res, m1 + m2.contiguous())

            # 1d + empty
            m1 = torch.tensor([1.0], dtype=dtype, device=device)
            m2 = torch.tensor([], dtype=dtype, device=device)
            self.assertEqual(m1 + m2, [])

        # inter-type unint8
        one = torch.tensor(1, dtype=torch.uint8, device=device)
        self.assertEqual(torch.add(one, 1), 2)
        self.assertEqual(torch.add(one, 1).dtype, torch.uint8)

        # bool
        m1 = torch.tensor([True, False, False, True, False, False], dtype=torch.bool, device=device)
        m2 = torch.tensor([True, True, False, False, False, True], dtype=torch.bool, device=device)
        expected = torch.tensor([True, True, False, True, False, True], dtype=torch.bool, device=device)
        self.assertEqual(m1 + m2, expected)

        # fused multiply add
        a = torch.zeros(2, 3, dtype=torch.bool, device=device)
        res = torch.add(a, a, alpha=0)
        expected = torch.zeros(2, 3, device=device).bool()
        self.assertEqual(res, expected)

        # bfloat16
        m1 = torch.tensor([1., 2.], dtype=torch.bfloat16)
        m2 = torch.tensor([3., 4.], dtype=torch.bfloat16)
        self.assertEqual(m1 + m2, torch.tensor([4., 6.], dtype=torch.bfloat16))

        # different alpha types
        m1 = torch.tensor([2 + 3j, 4 + 5j], dtype=torch.complex64, device=device)
        m2 = torch.tensor([4 + 5j, 2 + 3j], dtype=torch.complex64, device=device)
        # add complex numbers with float alpha
        res = torch.add(m1, m2, alpha=0.1)
        expected = torch.tensor([2.4000 + 3.5000j, 4.2000 + 5.3000j], dtype=torch.complex64, device=device)
        self.assertEqual(res, expected)

        # add complex numbers with complex alpha
        res = torch.add(m1, m2, alpha=complex(0.1, 0.2))
        expected = torch.tensor([1.4000 + 4.3000j, 3.6000 + 5.7000j], dtype=torch.complex64, device=device)
        self.assertEqual(res, expected)

        # add complex numbers with integer alpha
        res = torch.add(m1, m2, alpha=2)
        expected = torch.tensor([10. + 13.j, 8. + 11.j], dtype=torch.complex64, device=device)
        self.assertEqual(res, expected)

        # mismatched alpha
        m1 = torch.tensor([1], dtype=torch.int8, device=device)
        m2 = torch.tensor([2], dtype=torch.int8, device=device)
        self.assertRaisesRegex(RuntimeError,
                               r"Boolean alpha only supported for Boolean results\.",
                               lambda: torch.add(m1, m2, alpha=True))
        self.assertRaisesRegex(RuntimeError,
                               r"For integral input tensors, argument alpha must not be a floating point number\.",
                               lambda: torch.add(m1, m2, alpha=1.0))

        # mismatched alpha, float / double tensor and complex alpha
        m1 = torch.tensor([3., 4.], device=device)
        m2 = torch.tensor([4., 3.], device=device)
        self.assertRaises(RuntimeError, lambda: torch.add(m1, m2, alpha=complex(0.1, 0.2)))

        m1 = torch.tensor([3., 4.], dtype=torch.double, device=device)
        m2 = torch.tensor([4., 3.], dtype=torch.double, device=device)
        self.assertRaises(RuntimeError, lambda: torch.add(m1, m2, alpha=complex(0.1, 0.2)))

        # complex
        m1 = torch.tensor((4.0000 + 4.0000j), dtype=torch.complex64)
        m2 = torch.tensor(4., dtype=torch.float64)
        self.assertRaisesRegex(RuntimeError, r"result type ComplexFloat can't be cast to the desired output type Double",
                               lambda: torch.add(m1, m1, out=m2))


    def test_sub_typing(self, device):
        m1 = torch.tensor([True, False, False, True, False, False], dtype=torch.bool, device=device)
        m2 = torch.tensor([True, True, False, False, False, True], dtype=torch.bool, device=device)
        self.assertRaisesRegex(RuntimeError,
                               r"Subtraction, the `\-` operator, with two bool tensors is not supported. "
                               r"Use the `\^` or `logical_xor\(\)` operator instead.",
                               lambda: m1 - m2)
        self.assertRaisesRegex(RuntimeError,
                               r"Subtraction, the `\-` operator, with a bool tensor is not supported. "
                               r"If you are trying to invert a mask, use the `\~` or `logical_not\(\)` operator instead.",
                               lambda: 1 - m1)
        self.assertRaisesRegex(RuntimeError,
                               r"Subtraction, the `\-` operator, with a bool tensor is not supported. "
                               r"If you are trying to invert a mask, use the `\~` or `logical_not\(\)` operator instead.",
                               lambda: m2 - 1)

        # mismatched alpha
        m1 = torch.tensor([1], dtype=torch.int8, device=device)
        m2 = torch.tensor([2], dtype=torch.int8, device=device)
        self.assertRaisesRegex(RuntimeError,
                               r"Boolean alpha only supported for Boolean results\.",
                               lambda: torch.sub(m1, m2, alpha=True))
        self.assertRaisesRegex(RuntimeError,
                               r"For integral input tensors, argument alpha must not be a floating point number\.",
                               lambda: torch.sub(m1, m2, alpha=1.0))

    def test_mul(self, device):
        m1 = torch.randn(10, 10, device=device)
        res1 = m1.clone()
        res1[:, 3].mul_(2)
        res2 = m1.clone()
        for i in range(res1.size(0)):
            res2[i, 3] = res2[i, 3] * 2
        self.assertEqual(res1, res2)

        a1 = torch.tensor([True, False, False, True], dtype=torch.bool, device=device)
        a2 = torch.tensor([True, False, True, False], dtype=torch.bool, device=device)
        self.assertEqual(a1 * a2, torch.tensor([True, False, False, False], dtype=torch.bool, device=device))

        if device == 'cpu':
            a1 = torch.tensor([0.1, 0.1], dtype=torch.bfloat16, device=device)
            a2 = torch.tensor([1.1, 0.1], dtype=torch.bfloat16, device=device)
            self.assertEqual(a1 * a2, torch.tensor([0.11, 0.01], dtype=torch.bfloat16, device=device), atol=0.01, rtol=0)
            self.assertEqual(a1.mul(a2), a1 * a2)

    def test_cumsum(self, device):
        x = torch.rand(100, 100, device=device)
        res1 = torch.cumsum(x, 1)
        res2 = torch.Tensor().to(device)
        torch.cumsum(x, 1, out=res2)
        self.assertEqual(res1, res2)

        a = torch.tensor([[True, False, True],
                          [False, False, False],
                          [True, True, True]], device=device)
        b = a.byte()
        aRes = torch.cumsum(a, 0)
        bRes = torch.cumsum(b, 0)
        self.assertEqual(aRes, bRes)
        self.assertEqual(aRes, torch.tensor([[1, 0, 1],
                                             [1, 0, 1],
                                             [2, 1, 2]]))

        aRes = torch.cumsum(a, 1)
        bRes = torch.cumsum(b, 1)
        self.assertEqual(aRes, bRes)
        self.assertEqual(aRes, torch.tensor([[1, 1, 2],
                                             [0, 0, 0],
                                             [1, 2, 3]]))

        # Check that cummulative sum over a zero length dimension doesn't crash on backprop.
        # Also check that cumsum over other dimensions in a tensor with a zero-length
        # dimensiuon also works
        # Also include a basic suite of similar tests for other bases cases.
        shapes = [[2, 0], [2, 1, 4], [0, 2, 3], [1], [5]]
        for shape in shapes:
            for dim in range(len(shape)):
                raw_tensor = torch.zeros(*shape, requires_grad=True)
                integrated = raw_tensor.cumsum(dim=dim)
                # Check that backward does not crash
                integrated.sum().backward()
                # Check that output maintained correct shape
                self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

        # Check a scalar example
        raw_tensor = torch.tensor(3., requires_grad=True)
        integrated = raw_tensor.cumsum(dim=-1)
        self.assertEqual(raw_tensor, integrated)
        # Check that backward does not crash
        integrated.sum().backward()
        # Check that output maintained correct shape
        self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

    def test_cumprod(self, device):
        x = torch.rand(100, 100, device=device)
        res1 = torch.cumprod(x, 1)
        res2 = torch.Tensor().to(device)
        torch.cumprod(x, 1, out=res2)
        self.assertEqual(res1, res2)

        a = torch.tensor([[True, False, True],
                          [False, False, False],
                          [True, True, True]], dtype=torch.bool, device=device)
        b = a.byte()
        aRes = torch.cumprod(a, 0)
        bRes = torch.cumprod(b, 0)
        self.assertEqual(aRes, bRes)
        self.assertEqual(aRes, torch.tensor([[1, 0, 1],
                                             [0, 0, 0],
                                             [0, 0, 0]]))

        aRes = torch.cumprod(a, 1)
        bRes = torch.cumprod(b, 1)
        self.assertEqual(aRes, bRes)
        self.assertEqual(aRes, torch.tensor([[1, 0, 0],
                                             [0, 0, 0],
                                             [1, 1, 1]]))

        # Check that cummulative prod over a zero length dimension doesn't crash on backprop.
        # Also check that cumprod over other dimensions in a tensor with a zero-length
        # dimensiuon also works
        # Also include a basic suite of similar tests for other bases cases.
        shapes = [[2, 0], [2, 1, 4], [0, 2, 3], [1], [5]]
        for shape in shapes:
            for dim in range(len(shape)):
                raw_tensor = torch.zeros(*shape, requires_grad=True)
                integrated = raw_tensor.cumprod(dim=dim)
                # Check that backward does not crash
                integrated.sum().backward()
                # Check that output maintained correct shape
                self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

        # Check a scalar example
        raw_tensor = torch.tensor(3., requires_grad=True)
        integrated = raw_tensor.cumprod(dim=-1)
        self.assertEqual(raw_tensor, integrated)
        # Check that backward does not crash
        integrated.sum().backward()
        # Check that output maintained correct shape
        self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

    def test_cummax_cummin(self, device):
        def test_ops(op, string_of_function_name, expected_output1, expected_output2):
            x = torch.rand(100, 100, device=device)
            out1 = op(x, 1)
            res2 = torch.empty(0, device=device)
            indices2 = torch.empty(0, dtype=torch.int64, device=device)
            op(x, 1, out=(res2, indices2))
            self.assertEqual(out1[0], res2)
            self.assertEqual(out1[1], indices2)

            a = torch.tensor([[True, False, True],
                              [False, False, False],
                              [True, True, True]], dtype=torch.bool, device=device)
            b = a.byte()
            aRes = op(a, 0)
            bRes = op(b, 0)
            self.assertEqual(aRes[0], bRes[0].bool())
            self.assertEqual(aRes[0], expected_output1.bool())

            # test inf and nan input
            x = torch.tensor([4, inf, 1.5, -inf, 0, nan, 1])
            xRes = op(x, 0)[0]
            self.assertEqual(xRes, expected_output2)

            # op shouldn't support values, indices with a dtype, device type or layout
            # different from that of input tensor
            t = torch.randn(10)
            values = torch.empty(0, dtype=torch.int16)
            indices = torch.empty(0, dtype=torch.int64)
            with self.assertRaisesRegex(
                    RuntimeError,
                    'expected scalar_type Float but found Short'):
                op(t, 0, out=(values, indices))

            # Check that op over a zero length dimension doesn't crash on backprop.
            # Also check that op over other dimensions in a tensor with a zero-length
            # dimension also works
            # Also include a basic suite of similar tests for other bases cases.
            shapes = [[2, 0], [2, 1, 4], [0, 2, 3], [1], [5]]
            for shape in shapes:
                for dim in range(len(shape)):
                    raw_tensor = torch.zeros(*shape, requires_grad=True)
                    integrated = getattr(raw_tensor, string_of_function_name)(dim=dim)
                    # Check that backward does not crash
                    integrated[0].sum().backward()
                    # Check that output maintained correct shape
                    self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

            # Check a scalar example
            raw_tensor = torch.tensor(3., requires_grad=True)
            integrated = getattr(raw_tensor, string_of_function_name)(dim=-1)
            # Check that backward does not crash
            integrated[0].sum().backward()
            # Check that output maintained correct shape
            self.assertEqual(raw_tensor.shape, raw_tensor.grad.shape)

        expected_out = torch.tensor([4, inf, inf, inf, inf, nan, nan])
        test_ops(torch.cummax, "cummax", torch.tensor([[1, 0, 1],
                                                       [1, 0, 1],
                                                       [1, 1, 1]]), expected_out)

        expected_out = torch.tensor([4, 4, 1.5, -inf, -inf, nan, nan])
        test_ops(torch.cummin, "cummin", torch.tensor([[1, 0, 1],
                                                       [0, 0, 0],
                                                       [0, 0, 0]]), expected_out)

    def test_logcumsumexp(self, device):
        def logcumsumexp(a, axis):
            return torch.cumsum(a.exp(), axis=axis).log_()

        axis = 1
        a = torch.randn(100, 100, device=device)

        actual = a.logcumsumexp(1)
        expected = logcumsumexp(a, axis)
        self.assertEqual(a.dtype, actual.dtype)
        self.assertEqual(expected.shape, actual.shape)
        self.assertEqual(expected, actual)

        # Check that out is actually inplace
        b = torch.randn(5, 2, device=device)
        inplace_out = torch.zeros(5, 2, device=device)

        expected = logcumsumexp(b, axis)
        torch.logcumsumexp(b, axis=axis, out=inplace_out)

        self.assertEqual(inplace_out, expected)

        # Check input and inplace_output type mismatch
        b = torch.randn(5, 2, device=device, dtype=torch.float64)
        inplace_out = torch.zeros(5, 2, device=device, dtype=torch.float32)
        with self.assertRaisesRegex(
                RuntimeError,
                'expected scalar_type Double but found Float'):
            torch.logcumsumexp(b, axis, out=inplace_out)

    def _test_large_cum_fn_helper(self, x, fn):
        x_cpu = x.cpu().float()
        expected = fn(x_cpu)
        actual = fn(x).cpu().float()
        self.assertEqual(expected, actual.cpu().float())

    @onlyCUDA
    @dtypesIfCUDA(torch.half)  # only small dtype not to get oom
    def test_large_cumsum(self, device, dtype):
        # initialization to avoid overflow and half caveats
        x = torch.empty(2**30 + 200, device=device, dtype=dtype)
        x[::3] = -3
        x[1::3] = 2
        x[2::3] = 1
        self._test_large_cum_fn_helper(x, lambda x: torch.cumsum(x, 0))

    @onlyCUDA
    @dtypesIfCUDA(torch.half)  # only small dtype not to get oom
    def test_large_cumprod(self, device, dtype):
        # initialization to avoid overflow and half caveats
        x = torch.empty(2**30 + 200, device=device, dtype=dtype)
        x[::3] = 8
        x[1::3] = .25
        x[2::3] = .5
        self._test_large_cum_fn_helper(x, lambda x: torch.cumprod(x, 0))

    def test_discontiguous_out_cumsum(self, device):
        x = torch.randn(4, 8, device=device)
        y = torch.empty(4, 16, device=device)[:, ::2]
        out = torch.cumsum(x, 0)
        torch.cumsum(x, 0, out=y)
        self.assertFalse(y.is_contiguous())
        self.assertEqual(out, y, atol=0., rtol=0.)

    def _test_cumminmax_helper(self, x, fn, expected_val, expected_ind):
        val, ind = fn(x, -1)
        self.assertEqual(val, expected_val, atol=0, rtol=0)
        self.assertEqual(ind, expected_ind, atol=0, rtol=0)
        out_val = torch.empty_like(val).t().contiguous().t()
        out_ind = torch.empty_like(ind).t().contiguous().t()
        fn(x, -1, out=(out_val, out_ind))
        self.assertFalse(out_val.is_contiguous())
        self.assertFalse(out_ind.is_contiguous())
        self.assertEqual(out_val, expected_val, atol=0, rtol=0)
        self.assertEqual(out_ind, expected_ind, atol=0, rtol=0)

    def test_cummax_discontiguous(self, device):
        x = torch.tensor([[0, 1, 2, 3, 2, 1], [4, 5, 6, 5, 6, 7]], device=device, dtype=torch.float).t().contiguous().t()
        expected_val = torch.tensor([[0, 1, 2, 3, 3, 3], [4, 5, 6, 6, 6, 7]], device=device, dtype=torch.float)
        expected_ind = torch.tensor([[0, 1, 2, 3, 3, 3], [0, 1, 2, 2, 4, 5]], device=device, dtype=torch.long)
        self._test_cumminmax_helper(x, torch.cummax, expected_val, expected_ind)

    def test_cummin_discontiguous(self, device):
        x = torch.tensor([[3, 2, 1, 0, 1, 2], [7, 6, 5, 4, 5, 2]], device=device, dtype=torch.float).t().contiguous().t()
        expected_val = torch.tensor([[3, 2, 1, 0, 0, 0], [7, 6, 5, 4, 4, 2]], device=device, dtype=torch.float)
        expected_ind = torch.tensor([[0, 1, 2, 3, 3, 3], [0, 1, 2, 3, 3, 5]], device=device, dtype=torch.long)
        self._test_cumminmax_helper(x, torch.cummin, expected_val, expected_ind)


    def test_std_mean(self, device):
        x = torch.rand(100, 50, 20, device=device)
        for dim in range(x.dim()):
            for unbiased in [False, True]:
                for keepdim in [False, True]:
                    std1, mean1 = torch.std_mean(x, dim=dim, unbiased=unbiased, keepdim=keepdim)
                    std2 = x.std(dim=dim, unbiased=unbiased, keepdim=keepdim)
                    mean2 = x.mean(dim=dim, keepdim=keepdim)
                    self.assertEqual(std1, std2)
                    self.assertEqual(mean1, mean2)

    def test_std_mean_all_dims(self, device):
        x = torch.rand(100, 50, 20, device=device)
        for unbiased in [False, True]:
            std1, mean1 = torch.std_mean(x, unbiased=unbiased)
            std2 = x.std(unbiased=unbiased)
            mean2 = x.mean()
            self.assertEqual(std1, std2)
            self.assertEqual(mean1, mean2)

    def test_var_mean(self, device):
        x = torch.rand(100, 300, 50, device=device)
        for dim in range(x.dim()):
            for unbiased in [False, True]:
                for keepdim in [False, True]:
                    var1, mean1 = torch.var_mean(x, dim=dim, unbiased=unbiased, keepdim=keepdim)
                    var2 = x.var(dim=dim, unbiased=unbiased, keepdim=keepdim)
                    mean2 = x.mean(dim=dim, keepdim=keepdim)
                    self.assertEqual(var1, var2)
                    self.assertEqual(mean1, mean2)

    def test_var_mean_all_dims(self, device):
        x = torch.rand(100, 50, 20, device=device)
        for unbiased in [False, True]:
            var1, mean1 = torch.var_mean(x, unbiased=unbiased)
            var2 = x.var(unbiased=unbiased)
            mean2 = x.mean()
            self.assertEqual(var1, var2)
            self.assertEqual(mean1, mean2)

    def test_std_mean_some_dims(self, device):
        sizes = (4, 6, 7, 5, 3)
        dims = len(sizes)
        x = torch.rand(sizes, device=device)
        for num_of_dims in range(2, dims):
            dim_list = list(combinations(list(range(dims)), r=num_of_dims))
            for dim in dim_list:
                for unbiased in [False, True]:
                    for keepdim in [False, True]:
                        std1, mean1 = torch.std_mean(x, dim=dim, unbiased=unbiased, keepdim=keepdim)
                        std2 = x.std(dim=dim, unbiased=unbiased, keepdim=keepdim)
                        mean2 = x.mean(dim=dim, keepdim=keepdim)
                        self.assertEqual(std1, std2)
                        self.assertEqual(mean1, mean2)

    def _compare_std_var_with_numpy(self, op, device, dtype, input, dim,
                                    keepdim, unbiased, use_out):
        assert TEST_NUMPY

        a = input.cpu().numpy() if input.dtype is not torch.bfloat16 else input.float().cpu().numpy()
        numpy_kwargs = {
            'axis' : dim,
            'keepdims' : keepdim,
            'ddof' : 1 if unbiased else 0,
        }

        if dim is None:
            del numpy_kwargs['axis']
            del numpy_kwargs['keepdims']

        if op == 'var':
            torch_op = torch.var
            numpy_op = np.var
        elif op == 'std':
            torch_op = torch.std
            numpy_op = np.std
        else:
            self.fail("Unknown op!")

        numpy_result = numpy_op(a, **numpy_kwargs)

        if dim is None and use_out is False:
            torch_result = torch_op(input, unbiased)
        elif dim is not None and use_out is False:
            torch_result = torch_op(input, dim, unbiased, keepdim)
        elif dim is not None and use_out is True:
            out = torch.empty(0, device=device, dtype=dtype)
            torch_result = torch_op(input, dim, unbiased, keepdim, out=out)
        else:
            out = torch.empty(0, device=device, dtype=dtype)
            try:
                torch_result = torch_op(input, dim, unbiased, keepdim, out=out)
            except RuntimeError:
                return
            self.fail("Failed to hit RuntimeError!")

        self.assertEqual(torch_result, numpy_result, exact_dtype=False)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypesIfCUDA(torch.float, torch.double, torch.cfloat, torch.cdouble)
    @dtypes(torch.float, torch.double)
    def test_var_vs_numpy(self, device, dtype):
        _size = (20, 20)

        for test_case in product((torch.randn(_size, device=device, dtype=dtype),),
                                 (None, 0, 1),
                                 (False, True),
                                 (False, True),
                                 (False, True),):
            self._compare_std_var_with_numpy('var', device, dtype, *test_case)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypesIfCUDA(torch.float, torch.double, torch.cfloat, torch.cdouble)
    @dtypes(torch.float, torch.double)
    def test_std_vs_numpy(self, device, dtype):
        _size = (20, 20)

        for test_case in product((torch.randn(_size, device=device, dtype=dtype),),
                                 (None, 0, 1),
                                 (False, True),
                                 (False, True),
                                 (False, True),):
            self._compare_std_var_with_numpy('std', device, dtype, *test_case)

    def test_amin_amax_some_dims(self, device):
        sizes = (4, 6, 7, 5, 3)
        dims = len(sizes)
        x = torch.rand(sizes, device=device)
        for num_of_dims in range(2, dims):
            dim_list = list(combinations(list(range(dims)), r=num_of_dims))
            for dim in dim_list:
                for keepdim in [False, True]:
                    amin1 = torch.amin(x, dim=dim, keepdim=keepdim)
                    amax1 = torch.amax(x, dim=dim, keepdim=keepdim)
                    amin2 = x
                    amax2 = x
                    for i, d in enumerate(dim):
                        if not keepdim:
                            d -= i
                        amin2 = torch.amin(amin2, dim=d, keepdim=keepdim)
                        amax2 = torch.amax(amax2, dim=d, keepdim=keepdim)
                    self.assertEqual(amin1, amin2)
                    self.assertEqual(amax1, amax2)

    @onlyCUDA
    @expectedAlertNondeterministic('_histc_cuda', fn_has_device_arg=False)
    def test_histc_alert_nondeterministic(self, device):
        torch.histc(torch.tensor([], device=device), min=0, max=3)

    def test_histc(self, device):
        # negative nbins throws
        with self.assertRaisesRegex(RuntimeError, 'bins must be > 0'):
            torch.histc(torch.tensor([1], dtype=torch.float, device=device), bins=-1)
        # empty tensor
        actual = torch.histc(torch.tensor([], device=device), min=0, max=3)
        expected = torch.zeros(100, dtype=torch.float, device=device)
        self.assertEqual(expected, actual)

        # without nbins
        actual = torch.histc(
            torch.tensor([2, 5], dtype=torch.float, device=device))
        expected = torch.zeros(100, dtype=torch.float, device=device)
        expected[0] = 1
        expected[99] = 1
        self.assertEqual(expected, actual)
        # tensor with the same element
        actual = torch.histc(torch.ones(5, dtype=torch.float, device=device), bins=5)
        self.assertEqual(
            torch.tensor([0, 0, 5, 0, 0], dtype=torch.float, device=device),
            actual)
        # no element falls between [min, max]
        actual = torch.histc(
            torch.ones(5, dtype=torch.float, device=device), bins=5, min=2, max=3)
        self.assertEqual(
            torch.tensor([0, 0, 0, 0, 0], dtype=torch.float, device=device),
            actual)
        # element falls below min + integral bin size and
        actual = torch.histc(
            torch.tensor([2, 4, 2, 2, 5, 4], dtype=torch.float, device=device),
            bins=5, min=1, max=5)
        self.assertEqual(
            torch.tensor([0, 3, 0, 2, 1], dtype=torch.float, device=device),
            actual)
        # non-integral bin size
        actual = torch.histc(
            torch.tensor([1, 2, 1], dtype=torch.float, device=device),
            bins=4, min=0, max=3)
        self.assertEqual(
            torch.tensor([0, 2, 1, 0], dtype=torch.float, device=device),
            actual)
        # double input
        actual = torch.histc(
            torch.tensor([1, 2, 1], dtype=torch.double, device=device), bins=4, min=0, max=3)
        self.assertEqual(
            torch.tensor([0, 2, 1, 0], dtype=torch.double, device=device),
            actual)
        self.assertEqual(actual.dtype, torch.double)
        # mixed input
        actual = torch.histc(
            torch.tensor([1., 2, 1], dtype=torch.float, device=device),
            bins=4, min=0, max=3)
        self.assertEqual(
            torch.tensor([0, 2, 1, 0], dtype=torch.float, device=device),
            actual)
        self.assertEqual(actual.dtype, torch.float)
        # scalar input and 1 bin -- should return a 1-dimensional tensor, not a scalar.
        actual = torch.histc(
            torch.tensor(0, dtype=torch.float, device=device),
            bins=1, min=0, max=3)
        self.assertEqual(
            torch.tensor([1], dtype=torch.float, device=device),
            actual)
        # tensors with inf; min, max not provided -- should throw a RuntimeError
        with self.assertRaisesRegex(RuntimeError, r'range of \[inf, inf\] is not finite'):
            torch.histc(torch.tensor([float("inf")], dtype=torch.float, device=device))
        with self.assertRaisesRegex(RuntimeError, r'range of \[1, inf\] is not finite'):
            torch.histc(torch.tensor([1., 2., float("inf")], dtype=torch.float, device=device))
        # tensors with inf; min, max provided
        self.assertEqual(
            torch.histc(torch.tensor([float("inf")], dtype=torch.float, device=device),
                        bins=1, min=0, max=3),
            torch.tensor([0], dtype=torch.float, device=device))
        self.assertEqual(
            torch.histc(torch.tensor([1., 2., float("inf")], dtype=torch.float, device=device),
                        bins=4, max=3),
            torch.tensor([0, 1, 1, 0], dtype=torch.float, device=device))
        # tensor with nan -- should throw a RuntimeError
        with self.assertRaisesRegex(RuntimeError, r'range of \[nan, nan\] is not finite'):
            torch.histc(torch.tensor([float("nan")], dtype=torch.float, device=device))
        # tensors with min > max -- should throw a RuntimeError
        with self.assertRaisesRegex(RuntimeError, "max must be larger than min"):
            torch.histc(torch.tensor([1., 2., 3.], dtype=torch.float, device=device),
                        bins=4, min=5, max=1)

        # test against numpy.histogram()
        def test_against_np(tensor, bins=100, min=0, max=0):
            if min == 0 and max == 0:
                min = tensor.min().item()
                max = tensor.max().item()
            nparr = tensor.cpu().numpy()
            actual = torch.histc(tensor, bins=bins, min=min, max=max)
            expected = torch.from_numpy(np.histogram(nparr, bins=bins, range=(min, max))[0])
            actual_cpu = actual.cpu()
            # NB: Numpy returns a int64 tensor, like normal people...
            self.assertEqual(actual, expected.to(actual_cpu))

        if TEST_NUMPY:
            test_against_np(torch.tensor([1., 2, 1], device=device))
            test_against_np(torch.randn(5000, device=device))

            # Test bins arg
            test_against_np(torch.randn(301, device=device), bins=10)

            # Test truncated range
            test_against_np(torch.randn(201, device=device), min=0.1, max=1)

            noncontig = torch.randn(100, 3, device=device)[:, 2]
            test_against_np(noncontig)

            multidim = torch.randn(3, 5, 7, 2, device=device)
            test_against_np(multidim)

            expanded = torch.randn(1, 5, 1, 2, device=device).expand(3, 5, 7, 2)
            test_against_np(expanded)

    def test_bool_tensor_comparison_ops(self, device):
        a = torch.tensor([True, False, True, False, True, False], dtype=torch.bool, device=device)
        b = torch.tensor([True, False, True, True, True, True], dtype=torch.bool, device=device)
        self.assertEqual(a == b, torch.tensor([1, 1, 1, 0, 1, 0], dtype=torch.bool, device=device))
        self.assertEqual(a != b, torch.tensor([0, 0, 0, 1, 0, 1], dtype=torch.bool, device=device))
        self.assertEqual(a < b, torch.tensor([0, 0, 0, 1, 0, 1], dtype=torch.bool, device=device))
        self.assertEqual(a > b, torch.tensor([0, 0, 0, 0, 0, 0], dtype=torch.bool, device=device))
        self.assertEqual(a >= b, torch.tensor([1, 1, 1, 0, 1, 0], dtype=torch.bool, device=device))
        self.assertEqual(a <= b, torch.tensor([1, 1, 1, 1, 1, 1], dtype=torch.bool, device=device))
        self.assertEqual(a > False, torch.tensor([1, 0, 1, 0, 1, 0], dtype=torch.bool, device=device))
        self.assertEqual(a == torch.tensor(True, dtype=torch.bool, device=device),
                         torch.tensor([1, 0, 1, 0, 1, 0], dtype=torch.bool, device=device))
        self.assertEqual(a == torch.tensor(0, dtype=torch.bool, device=device),
                         torch.tensor([0, 1, 0, 1, 0, 1], dtype=torch.bool, device=device))
        self.assertFalse(a.equal(b))

    def test_bool_tensor_value_change(self, device):
        x = torch.tensor([True, False], dtype=torch.bool, device=device)
        x[0] = False
        x[1] = True
        self.assertEqual(x, torch.tensor([False, True], dtype=torch.bool, device=device))

    def test_unfold_all_devices_and_dtypes(self, device):
        for dt in torch.testing.get_all_dtypes():

            if dt == torch.bool:
                x = torch.empty((0, 1, 3, 0), dtype=dt, device=device)
                self.assertEqual((0, 1, 1, 0, 3), x.unfold(2, 3, 2).shape)
            else:
                x = torch.empty((0, 1, 3, 0), dtype=dt, device=device)
                self.assertEqual((0, 1, 1, 0, 3), x.unfold(2, 3, 2).shape)

    def test_unfold_scalars(self, device):
        x = torch.tensor(0.5, device=device)
        # unfold on a 0-dimensional tensor should always return a 1-d dimensional
        # tensor of shape [size] (i.e., the second parameter to unfold)

        self.assertEqual(torch.empty(0, device=device), x.unfold(0, 0, 1))
        self.assertEqual(torch.empty(0, device=device), x.unfold(0, 0, 2))
        self.assertEqual(torch.tensor([0.5], device=device), x.unfold(0, 1, 1))

    def test_copy_all_dtypes_and_devices(self, device):
        from copy import copy
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor([1, 2, 3, 4], dtype=dt, device=device)
            x_clone = x.clone()
            y = copy(x)
            y.fill_(1)
            # copy is a shallow copy, only copies the tensor view,
            # not the data
            self.assertEqual(x, y)

    def test_resize_all_dtypes_and_devices(self, device):
        shape = (2, 2)
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor([[1, 2], [3, 4], [5, 6]], dtype=dt, device=device)
            x.resize_(shape)
            self.assertEqual(shape, x.shape)

    def test_resize_as_all_dtypes_and_devices(self, device):
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor([[1, 2], [3, 4], [5, 6]], dtype=dt, device=device)
            y = torch.tensor([[1, 2, 3], [4, 5, 6]], dtype=dt, device=device)
            x.resize_as_(y)
            self.assertEqual(y.shape, x.shape)

    def test_view_all_dtypes_and_devices(self, device):
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor([[1, 2], [3, 4], [5, 6]], dtype=dt, device=device)
            self.assertEqual(x.view(6).shape, [6])

    def test_fill_all_dtypes_and_devices(self, device):
        for dt in torch.testing.get_all_dtypes():
            for x in [torch.tensor((10, 10), dtype=dt, device=device),
                      torch.empty(10000, dtype=dt, device=device)]:  # large tensor
                numel = x.numel()
                bound = 100 if dt in (torch.uint8, torch.int8) else 2000
                for n in range(-bound, bound, bound // 10):
                    x.fill_(n)
                    self.assertEqual(x, torch.tensor([n] * numel, dtype=dt, device=device))
                    self.assertEqual(dt, x.dtype)

    def test_clone_all_dtypes_and_devices(self, device):
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor((1, 1), dtype=dt, device=device)
            y = x.clone()
            self.assertEqual(x, y)

    def test_clone_zero_stride_dim(self, device):
        # stride zero, size 1 axis, not contiguous
        x = torch.randn(10)
        y = x.as_strided([2, 1, 5], [1, 0, 2])
        self.assertEqual(y, y.clone())

    def test_cat_all_dtypes_and_devices(self, device):
        for dt in torch.testing.get_all_dtypes():
            x = torch.tensor([[1, 2], [3, 4]], dtype=dt, device=device)

            expected1 = torch.tensor([[1, 2], [3, 4], [1, 2], [3, 4]], dtype=dt, device=device)
            self.assertEqual(torch.cat((x, x), 0), expected1)

            expected2 = torch.tensor([[1, 2, 1, 2], [3, 4, 3, 4]], dtype=dt, device=device)
            self.assertEqual(torch.cat((x, x), 1), expected2)



    @onlyOnCPUAndCUDA
    def test_vander(self, device):
        x = torch.tensor([1, 2, 3, 5], device=device)

        self.assertEqual((0, 0), torch.vander(torch.tensor([]), 0).shape)

        with self.assertRaisesRegex(RuntimeError, "N must be non-negative."):
            torch.vander(x, N=-1)

        with self.assertRaisesRegex(RuntimeError, "x must be a one-dimensional tensor."):
            torch.vander(torch.stack((x, x)))

    @unittest.skipIf(not TEST_NUMPY, 'NumPy not found')
    @onlyOnCPUAndCUDA
    @dtypes(torch.bool, torch.uint8, torch.int8, torch.short, torch.int, torch.long,
            torch.float, torch.double,
            torch.cfloat, torch.cdouble)
    def test_vander_types(self, device, dtype):
        if dtype is torch.uint8:
            # Note: no negative uint8 values
            X = [[1, 2, 3, 5], [0, 1 / 3, 1, math.pi, 3 / 7]]
        elif dtype is torch.bool:
            # Note: see https://github.com/pytorch/pytorch/issues/37398
            # for why this is necessary.
            X = [[True, True, True, True], [False, True, True, True, True]]
        elif dtype in [torch.cfloat, torch.cdouble]:
            X = [[1 + 1j, 1 + 0j, 0 + 1j, 0 + 0j],
                 [2 + 2j, 3 + 2j, 4 + 3j, 5 + 4j]]
        else:
            X = [[1, 2, 3, 5], [-math.pi, 0, 1 / 3, 1, math.pi, 3 / 7]]

        N = [None, 0, 1, 3]
        increasing = [False, True]

        for x, n, inc in product(X, N, increasing):
            numpy_dtype = torch_to_numpy_dtype_dict[dtype]
            pt_x = torch.tensor(x, device=device, dtype=dtype)
            np_x = np.array(x, dtype=numpy_dtype)

            pt_res = torch.vander(pt_x, increasing=inc) if n is None else torch.vander(pt_x, n, inc)
            np_res = np.vander(np_x, n, inc)

            self.assertEqual(
                pt_res,
                torch.from_numpy(np_res),
                atol=1e-3,
                rtol=0,
                exact_dtype=False)

    def test_addcmul(self, device):
        def rand_tensor(size, dtype, device):
            if dtype.is_floating_point or dtype.is_complex:
                return torch.rand(size=size, dtype=dtype, device=device)
            if dtype == torch.uint8:
                return torch.randint(1, 5, size=size, dtype=dtype, device=device)
            else:
                return torch.randint(-5, 5, size=size, dtype=dtype, device=device)

        for dtype in torch.testing.get_all_math_dtypes(device):
            a = rand_tensor((2, 2), dtype=dtype, device=device)
            b = rand_tensor((2, 2), dtype=dtype, device=device)
            c = rand_tensor((2, 2), dtype=dtype, device=device)
            if dtype.is_floating_point:
                alpha = 0.1
            else:
                alpha = 3

            # addcmul is not supported for complex dtypes on cuda yet
            if device.startswith('cuda') and dtype.is_complex:
                continue

            actual = torch.addcmul(a, b, c, value=alpha)
            expected = a + alpha * b * c

            self.assertEqual(expected, actual)

            with self.maybeWarnsRegex(
                    UserWarning, "This overload of addcmul is deprecated"):
                self.assertEqual(actual, torch.addcmul(a, alpha, b, c))

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @tf32_on_and_off(0.005)
    def test_tensordot(self, device):
        a = torch.arange(60., device=device).reshape(3, 4, 5)
        b = torch.arange(24., device=device).reshape(4, 3, 2)
        c = torch.tensordot(a, b, dims=([1, 0], [0, 1])).cpu()
        cn = torch.from_numpy(np.tensordot(a.cpu().numpy(), b.cpu().numpy(),
                                           axes=([1, 0], [0, 1])))
        self.assertEqual(c, cn)
        a = torch.randn(2, 3, 4, 5, device=device)
        b = torch.randn(4, 5, 6, 7, device=device)
        c = torch.tensordot(a, b, dims=2).cpu()
        cn = torch.from_numpy(np.tensordot(a.cpu().numpy(), b.cpu().numpy(),
                                           axes=2))

        with self.assertRaisesRegex(RuntimeError, "expects dims >= 0"):
            torch.tensordot(a, b, dims=-1)

        self.assertEqual(c, cn)
        c = torch.tensordot(a, b).cpu()
        cn = torch.from_numpy(np.tensordot(a.cpu().numpy(), b.cpu().numpy()))
        self.assertEqual(c, cn)

    def test_narrow_empty(self, device):
        x = torch.randn(2, 3, 4, device=device)
        for d in range(x.dim()):
            y = x.narrow(d, x.size(d), 0)
            sz = list(x.size())
            sz[d] = 0
            self.assertEqual(sz, y.size())

    @dtypes(*torch.testing.get_all_dtypes(include_complex=False))
    def test_logical(self, device, dtype):
        if dtype != torch.bool:
            x = torch.tensor([1, 2, 3, 4], device=device, dtype=dtype)
            b = torch.tensor([2], device=device, dtype=dtype)
            self.assertEqual(x.lt(2), torch.tensor([True, False, False, False]))
            self.assertEqual(x.le(2), torch.tensor([True, True, False, False]))
            self.assertEqual(x.ge(2), torch.tensor([False, True, True, True]))
            self.assertEqual(x.gt(2), torch.tensor([False, False, True, True]))
            self.assertEqual(x.eq(2), torch.tensor([False, True, False, False]))
            self.assertEqual(x.ne(2), torch.tensor([True, False, True, True]))

            self.assertEqual(x.lt(b), torch.tensor([True, False, False, False]))
            self.assertEqual(x.le(b), torch.tensor([True, True, False, False]))
            self.assertEqual(x.ge(b), torch.tensor([False, True, True, True]))
            self.assertEqual(x.gt(b), torch.tensor([False, False, True, True]))
            self.assertEqual(x.eq(b), torch.tensor([False, True, False, False]))
            self.assertEqual(x.ne(b), torch.tensor([True, False, True, True]))
        else:
            x = torch.tensor([True, False, True, False], device=device)
            self.assertEqual(x.lt(True), torch.tensor([False, True, False, True]))
            self.assertEqual(x.le(True), torch.tensor([True, True, True, True]))
            self.assertEqual(x.ge(True), torch.tensor([True, False, True, False]))
            self.assertEqual(x.gt(True), torch.tensor([False, False, False, False]))
            self.assertEqual(x.eq(True), torch.tensor([True, False, True, False]))
            self.assertEqual(x.ne(True), torch.tensor([False, True, False, True]))

    def test_index_copy(self, device):
        num_copy, num_dest = 3, 20
        dest = torch.randn(num_dest, 4, 5, device=device)
        src = torch.randn(num_copy, 4, 5, device=device)
        idx = torch.randperm(num_dest, device=device).narrow(0, 0, num_copy)
        dest2 = dest.clone()
        dest.index_copy_(0, idx, src)
        for i in range(idx.size(0)):
            dest2[idx[i]] = src[i]
        self.assertEqual(dest, dest2, atol=0, rtol=0)

        dest = torch.randn(num_dest, device=device)
        src = torch.randn(num_copy, device=device)
        idx = torch.randperm(num_dest, device=device).narrow(0, 0, num_copy)
        dest2 = dest.clone()
        dest.index_copy_(0, idx, src)
        for i in range(idx.size(0)):
            dest2[idx[i]] = src[i]
        self.assertEqual(dest, dest2, atol=0, rtol=0)

        # Bool tensor
        dest = torch.zeros(2, 2, dtype=torch.bool, device=device)
        src = torch.tensor([[True, True], [True, True]], device=device)
        index = torch.tensor([0, 1], device=device)
        dest.index_copy_(0, index, src)
        self.assertEqual(dest, torch.tensor([[True, True], [True, True]], device=device))

        # Error cases
        a = torch.randn(3, 5)
        c = torch.zeros(3)
        self.assertRaises(IndexError, lambda: a.index_copy_(dim=1, index=torch.tensor([3]), source=c))

    def test_index_fill(self, device):
        for dt in torch.testing.get_all_dtypes():
            if dt == torch.half or dt == torch.bfloat16 or dt.is_complex:
                continue

            x = torch.tensor([[1, 2], [4, 5]], dtype=dt, device=device)
            index = torch.tensor([0], device=device)
            x.index_fill_(1, index, 0)
            self.assertEqual(x, torch.tensor([[0, 2], [0, 5]], dtype=dt, device=device))

    def test_index_select(self, device):
        src = torch.randn(3, 4, 5, device=device)
        # Index can be duplicated.
        idx = torch.tensor([2, 1, 0, 1, 2], dtype=torch.long, device=device)
        dest = torch.index_select(src, 0, idx)
        self.assertEqual(dest.shape, (5, 4, 5))
        for i in range(idx.size(0)):
            self.assertEqual(dest[i], src[idx[i]])

        # Check that 'out' is used correctly.
        out = torch.randn(5 * 4 * 5, device=device)
        dest = torch.index_select(src, 0, idx, out=out.view(5, 4, 5))
        self.assertEqual(dest.shape, (5, 4, 5))
        for i in range(idx.size(0)):
            self.assertEqual(dest[i], src[idx[i]])
        out.fill_(0.123)
        self.assertEqual(out, dest.view(-1))  # Must point to the same storage.

        # Bool tensor
        src = torch.tensor([False, True, False, False], device=device, dtype=torch.bool)
        idx = torch.tensor([1], dtype=torch.long, device=device)
        dest = torch.index_select(src, 0, idx)
        self.assertEqual(torch.tensor([True]), dest)

        # Complex Tensor
        src = torch.randn(3, 4, 5, dtype=torch.complex64, device=device)
        idx = torch.tensor([2, 1, 0, 1, 2], dtype=torch.long, device=device)
        dest = torch.index_select(src, 0, idx)
        self.assertEqual(dest.shape, (5, 4, 5))
        for i in range(idx.size(0)):
            self.assertEqual(dest[i], src[idx[i]])

    def test_take_empty(self, device):
        for input_shape in [(0,), (0, 1, 2, 0), (1, 2, 3)]:
            for indices_shape in [(0,), (0, 1, 2, 0)]:
                input = torch.empty(input_shape, device=device)
                indices = torch.empty(indices_shape, dtype=torch.int64, device=device)
                self.assertEqual(indices, torch.take(input, indices), exact_dtype=False)

    def test_put_empty(self, device):
        for dst_shape in [(0,), (0, 1, 2, 0), (1, 2, 3)]:
            for indices_shape in [(0,), (0, 1, 2, 0)]:
                for accumulate in [False, True]:
                    dst = torch.randn(dst_shape, device=device)
                    indices = torch.empty(indices_shape, dtype=torch.int64, device=device)
                    src = torch.randn(indices_shape, device=device)
                    self.assertEqual(dst, dst.put_(indices, src, accumulate=accumulate))

    @skipCUDAIfRocm
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=False) +
              torch.testing.get_all_complex_dtypes()))
    @dtypesIfCPU(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=True) +
                   torch.testing.get_all_complex_dtypes()))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_bfloat16=True, include_half=True) +
                    torch.testing.get_all_complex_dtypes()))
    def test_scatter_reduce_operations_to_large_input(self, device, dtype):
        index = torch.tensor([[1], [2]], device=device, dtype=torch.long)
        test_data = [
            (torch.zeros(4, 4, device=device, dtype=dtype),
             torch.ones(2, 2, device=device, dtype=dtype),
             torch.tensor([[0, 0, 0, 0],
                           [1, 0, 0, 0],
                           [1, 0, 0, 0],
                           [0, 0, 0, 0]],
                          device=device, dtype=dtype), "add"),
            (torch.tensor([2], device=device, dtype=dtype).repeat(4, 4),
             torch.tensor([6], device=device, dtype=dtype).repeat(2, 2),
             torch.tensor([[2, 2, 2, 2],
                           [12, 2, 2, 2],
                           [12, 2, 2, 2],
                           [2, 2, 2, 2]], device=device, dtype=dtype), "multiply"),
        ]

        for input, src, result, operation in test_data:
            if operation == "multiply" and torch.is_complex(input):
                continue
            input.scatter_(0, index, src, reduce=operation)
            self.assertEqual(input, result)

    @skipCUDAIfRocm
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=False) +
              torch.testing.get_all_complex_dtypes()))
    @dtypesIfCPU(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=True) +
                   torch.testing.get_all_complex_dtypes()))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_bfloat16=True, include_half=True) +
                    torch.testing.get_all_complex_dtypes()))
    def test_scatter_reduce_scalar(self, device, dtype):
        index = torch.tensor([[1], [2]], device=device, dtype=torch.long)
        test_data = [
            (torch.zeros(4, 4, device=device, dtype=dtype), 1,
             torch.tensor([[0, 0, 0, 0],
                           [1, 0, 0, 0],
                           [1, 0, 0, 0],
                           [0, 0, 0, 0]],
                          device=device, dtype=dtype), "add"),
            (torch.tensor([2], device=device, dtype=dtype).repeat(4, 4), 2,
             torch.tensor([[2, 2, 2, 2],
                           [4, 2, 2, 2],
                           [4, 2, 2, 2],
                           [2, 2, 2, 2]], device=device, dtype=dtype), "multiply"),
        ]

        for input, src, result, operation in test_data:
            if operation == "multiply" and torch.is_complex(input):
                continue
            input.scatter_(0, index, src, reduce=operation)
            self.assertEqual(input, result)

    # TODO: remove this after scatter_add_ is deprecated.
    def test_scatter_add_non_unique_index(self, device):
        height = 2
        width = 65536
        input = torch.ones(height, width, device=device)
        index = torch.zeros(height, width, dtype=torch.long, device=device)
        src = torch.ones(height, width, device=device)
        input.scatter_add_(0, index, src)

        self.assertEqual(input,
                         torch.tensor([[3], [1]], device=device,
                                      dtype=torch.float32).repeat(1, width))

    @skipCUDAIfRocm
    @dtypes(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=False) +
              torch.testing.get_all_complex_dtypes()))
    @dtypesIfCPU(*(torch.testing.get_all_fp_dtypes(include_bfloat16=False, include_half=True) +
                   torch.testing.get_all_complex_dtypes()))
    @dtypesIfCUDA(*(torch.testing.get_all_fp_dtypes(include_bfloat16=True, include_half=True) +
                    torch.testing.get_all_complex_dtypes()))
    def test_scatter_reduce_non_unique_index(self, device, dtype):
        height = 2
        width = 2
        index = torch.zeros(height, width, dtype=torch.long, device=device)
        test_data = [
            (torch.ones(height, width, device=device, dtype=dtype),
             torch.ones(height, width, device=device, dtype=dtype),
             torch.tensor([[3], [1]], device=device, dtype=dtype).repeat(1, width), "add"),
            (torch.tensor([2], device=device, dtype=dtype).repeat(height, width),
             torch.tensor([2], device=device, dtype=dtype).repeat(height, width),
             torch.tensor([[8], [2]], device=device,
                          dtype=dtype).repeat(1, width), "multiply"),
        ]

        for input, src, result, operation in test_data:
            if operation == "multiply" and torch.is_complex(input):
                continue
            input.scatter_(0, index, src, reduce=operation)
            self.assertEqual(input, result, msg=f"result: {result} input: {input} method: {str(operation)}")

    @skipCUDAIfRocm
    @onlyOnCPUAndCUDA
    @dtypesIfCUDA(*(torch.testing.get_all_complex_dtypes() +
                    torch.testing.get_all_int_dtypes()))
    @dtypesIfCPU(*(torch.testing.get_all_int_dtypes()))
    def test_scatter_reduce_multiply_unsupported_dtypes(self, device, dtype):
        height = 2
        width = 2
        index = torch.zeros(height, width, dtype=torch.long, device=device)
        input = torch.ones(height, width, device=device, dtype=dtype)
        src = torch.ones(height, width, device=device, dtype=dtype)
        with self.assertRaises(RuntimeError):
            input.scatter_(0, index, src, reduce="multiply")

    def test_scatter_to_large_input(self, device):
        input = torch.zeros(4, 4, device=device)
        src = torch.ones(2, 2, device=device)
        index = torch.tensor([[1], [2]], device=device, dtype=torch.long)
        input.scatter_(0, index, src)
        self.assertEqual(input, torch.tensor([[0, 0, 0, 0],
                                              [1, 0, 0, 0],
                                              [1, 0, 0, 0],
                                              [0, 0, 0, 0]], device=device, dtype=torch.float32))

    def test_scatter_add_to_large_input(self, device):
        input = torch.zeros(4, 4, device=device)
        src = torch.ones(2, 2, device=device)
        index = torch.tensor([[1], [2]], device=device, dtype=torch.long)
        input.scatter_add_(0, index, src)
        self.assertEqual(input, torch.tensor([[0, 0, 0, 0],
                                              [1, 0, 0, 0],
                                              [1, 0, 0, 0],
                                              [0, 0, 0, 0]], device=device, dtype=torch.float32))

    def test_scatter_bool(self, device):
        x = torch.tensor([[True, True, True], [True, True, True]], device=device)
        res = torch.zeros(3, 3, dtype=torch.bool, device=device)
        res = res.scatter_(0, torch.tensor([[0, 1, 2], [0, 1, 2]], device=device), x)
        self.assertEqual(res, torch.tensor([[True, False, False],
                                            [False, True, False],
                                            [False, False, True]], device=device))

    def test_scatter_add_bool(self, device):
        x = torch.tensor([[True, True, True, True, True], [True, True, True, True, True]], device=device)
        res = torch.zeros(3, 5, dtype=torch.bool, device=device)
        res = res.scatter_add_(0, torch.tensor([[0, 1, 2, 0, 0], [2, 0, 0, 1, 2]], device=device), x)
        self.assertEqual(res, torch.tensor([[True, True, True, True, True],
                                            [False, True, False, True, False],
                                            [True, False, True, False, True]], device=device))

    def test_masked_scatter_bool_tensor(self, device):
        src = torch.tensor([True, True, True], device=device)
        dst = torch.tensor([False, False, False], device=device)
        mask = torch.tensor([False, True, False], device=device)

        dst.masked_scatter_(mask, src)
        self.assertEqual(dst, torch.tensor([False, True, False], device=device))

        mask = torch.tensor([True, False, True], device=device)
        dst = dst.masked_scatter(mask, src)
        self.assertEqual(dst, torch.tensor([True, True, True], device=device))

    @dtypes(*torch.testing.get_all_dtypes())
    def test_masked_select(self, device, dtype):
        if device == 'cpu':
            warn = 'masked_select received a mask with dtype torch.uint8,'
        else:
            warn = 'indexing with dtype torch.uint8 is now deprecated, pl'
        for maskType in [torch.uint8, torch.bool]:
            num_src = 10
            src = torch.tensor([0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=dtype, device=device)
            mask = torch.randint(2, (num_src,), device=device, dtype=maskType)

            with warnings.catch_warnings(record=True) as w:
                dst = src.masked_select(mask)
                if maskType is torch.uint8:
                    self.assertEqual(len(w), 1)
                    self.assertEqual(str(w[0].message)[0:53], str(warn))
            dst2 = []
            for i in range(num_src):
                if mask[i]:
                    dst2 += [src[i]]
            self.assertEqual(dst, torch.tensor(dst2), atol=0, rtol=0)

            dst3 = torch.empty(0, device=device, dtype=dtype)
            torch.masked_select(src, mask, out=dst3)
            self.assertEqual(dst3, torch.tensor(dst2, dtype=dst3.dtype), atol=0, rtol=0)

        # Since half on CPU is not supported, need to skip the remaining test cases
        if dtype == torch.half and torch.device(device).type == 'cpu':
            return

        # Ensure that masks are expanded to match tensor properly
        a = torch.rand(100, 100, device=device).mul(100).to(dtype)
        mask_first_el_each_row = torch.zeros(100, device=device, dtype=torch.bool)
        mask_first_el_each_row[0] = True
        a_masked = a.masked_select(mask_first_el_each_row)
        self.assertEqual(a_masked, a[:, 0])

        mask_first_row = torch.zeros(100, 1, device=device, dtype=torch.bool)
        mask_first_row[0][0] = True
        a_masked = a.masked_select(mask_first_row)
        self.assertEqual(a_masked, a[0, :])

        # Ensure that tensor is expanded to match mask properly
        a = torch.rand(100, device=device).mul(100).to(dtype)
        mask_copy_3_times = torch.tensor([[True], [True], [False], [True]], device=device)
        a_masked = a.masked_select(mask_copy_3_times)
        self.assertEqual(a_masked, a.unsqueeze(0).expand(3, 100).flatten())

    def test_masked_select_discontiguous(self, device):
        for size in (10, 200):
            vals = torch.rand(size, size, device=device)
            mask = torch.full((size, size), False, dtype=torch.bool, device=device)
            mask[:, ::2] = True
            vals_list = (vals, vals.t())
            mask_list = (mask, mask.t())
            out_dc = torch.empty(size * size, device=device)[::2]
            for v, m in product(vals_list, mask_list):
                if m.is_contiguous():
                    expected = v[:, ::2].clone().view(-1)
                else:
                    expected = v[::2].clone().view(-1)
                out = torch.masked_select(v, m)
                self.assertEqual(out, expected, atol=0, rtol=0)
                torch.masked_select(v, m, out=out_dc)
                self.assertEqual(out_dc, expected, atol=0, rtol=0)


    def test_masked_fill_bool_tensor(self, device):
        dst = torch.tensor([True, False, True], device=device)
        mask = torch.tensor([False, True, False], device=device)

        dst.masked_fill_(mask, True)
        self.assertEqual(dst, torch.tensor([True, True, True], device=device))

        dst = dst.masked_fill(mask, False)
        self.assertEqual(dst, torch.tensor([True, False, True], device=device))

    def test_tensor_shape_empty(self, device):
        x = torch.randn((0, 1, 3, 0), device=device)
        # flatten
        self.assertEqual((0,), torch.flatten(x, 0, 3).shape)
        self.assertEqual((0, 0), torch.flatten(x, 0, 2).shape)
        self.assertEqual((0, 3, 0), torch.flatten(x, 1, 2).shape)

        # squeeze, unsqueeze
        self.assertEqual((0, 1, 1, 3, 0), torch.unsqueeze(x, 1).shape)
        self.assertEqual((0, 3, 0), torch.squeeze(x, 1).shape)
        self.assertEqual((0, 3, 0), torch.squeeze(x).shape)

        # transpose, t
        self.assertEqual((0, 0, 3, 1), torch.transpose(x, 1, 3).shape)
        y = torch.randn((5, 0), device=device)
        self.assertEqual((0, 5), y.t().shape)

        # select
        self.assertEqual((0, 1, 0), torch.select(x, 2, 2).shape)

        # repeat, permute
        self.assertEqual((9, 0, 5, 6, 0), x.repeat(9, 7, 5, 2, 3).shape)
        self.assertEqual((3, 0, 0, 1), x.permute(2, 3, 0, 1).shape)

        # diagonal, diagflat
        self.assertEqual((0,), torch.diagonal(torch.randn((5, 0), device=device)).shape)
        self.assertEqual((0,), torch.diagonal(torch.randn((0, 5), device=device)).shape)
        # off the end offsets are valid
        self.assertEqual((0,), torch.diagonal(torch.randn((5, 0), device=device), offset=1).shape)
        self.assertEqual((0,), torch.diagonal(torch.randn((0, 5), device=device), offset=1).shape)
        # check non-zero sized offsets off the end
        self.assertEqual((5, 6, 0), torch.diagonal(torch.randn((3, 4, 5, 6), device=device), offset=45252).shape)
        self.assertEqual((5, 6, 0), torch.diagonal(torch.randn((3, 4, 5, 6), device=device), offset=-45252).shape)

        self.assertEqual((0, 0), torch.diagflat(torch.tensor([], device=device)).shape)
        self.assertEqual(torch.zeros(1, 1), torch.diagflat(torch.tensor([], device=device), offset=1))
        self.assertEqual((0, 0), torch.diagflat(torch.tensor([[]], device=device)).shape)
        self.assertEqual(torch.zeros(1, 1), torch.diagflat(torch.tensor([[]], device=device), offset=1))

        # stack, split, chunk
        self.assertEqual((4, 0, 1, 3, 0), torch.stack((x, x, x, x)).shape)
        self.assertEqual([(0, 1, 3, 0)],
                         [z.shape for z in torch.chunk(x, 1, dim=0)])

        self.assertEqual([(0, 1, 3, 0), ] * 3, [z.shape for z in torch.chunk(x, 3, dim=0)])
        self.assertEqual([(0, 1, 1, 0), ] * 3, [z.shape for z in torch.chunk(x, 3, dim=2)])

        # NOTE: split_with_sizes behaves differently than NumPy in that it
        # takes sizes rather than offsets
        self.assertEqual([(0, 1, 0, 0), (0, 1, 1, 0), (0, 1, 2, 0)],
                         [z.shape for z in torch.split(x, (0, 1, 2), dim=2)])

        self.assertRaises(RuntimeError, lambda: torch.split(x, 0, dim=1))
        # This is strange because the split size is larger than the dim size, but consistent with
        # how split handles that case generally (when no 0s are involved).
        self.assertEqual([(0, 1, 3, 0)], [z.shape for z in torch.split(x, 1, dim=0)])
        self.assertEqual([(0, 1, 3, 0)], [z.shape for z in torch.split(x, 0, dim=0)])

    # functions that operate over a dimension but don't reduce.
    def test_dim_function_empty(self, device):
        shape = (0, 1, 2, 0)
        x = torch.randn(shape, device=device)

        # size stride
        self.assertEqual(0, x.size(3))
        self.assertEqual(2, x.size(2))
        self.assertEqual(2, x.stride(0))
        self.assertEqual(1, x.stride(2))

        self.assertEqual(x, torch.nn.functional.glu(x, 0))
        self.assertEqual((0, 1, 1, 0), torch.nn.functional.glu(x, 2).shape)

        # softmax, logsoftmax
        self.assertEqual(x, torch.nn.functional.softmax(x, 0))
        self.assertEqual(x, torch.nn.functional.softmax(x, 2))
        self.assertEqual(x, torch.nn.functional.softmax(x, 3))

        self.assertEqual(x, torch.nn.functional.log_softmax(x, 0))
        self.assertEqual(x, torch.nn.functional.log_softmax(x, 2))
        self.assertEqual(x, torch.nn.functional.log_softmax(x, 3))

        # cumsum, cumprod, cummax, cummin
        self.assertEqual(shape, torch.cumsum(x, 0).shape)
        self.assertEqual(shape, torch.cumsum(x, 2).shape)
        self.assertEqual(shape, torch.cumprod(x, 0).shape)
        self.assertEqual(shape, torch.cumprod(x, 2).shape)
        self.assertEqual(shape, torch.cummax(x, 0)[0].shape)
        self.assertEqual(shape, torch.cummax(x, 2)[0].shape)
        self.assertEqual(shape, torch.cummin(x, 0)[0].shape)
        self.assertEqual(shape, torch.cummin(x, 2)[0].shape)
        self.assertEqual(shape, torch.logcumsumexp(x, 0).shape)
        self.assertEqual(shape, torch.logcumsumexp(x, 2).shape)

        # flip
        self.assertEqual(x, x.flip(0))
        self.assertEqual(x, x.flip(2))

        # roll
        self.assertEqual(x, x.roll(0, 1).roll(0, -1))
        self.assertEqual(x, x.roll(1, x.size(1)))
        self.assertEqual(x, x.roll(1))
        self.assertEqual(x, x.roll((1, 1), (3, 1)))

        # unbind
        self.assertEqual((), x.unbind(0))
        self.assertEqual((torch.empty((0, 1, 0), device=device), torch.empty((0, 1, 0), device=device)),
                         x.unbind(2))

        # cross
        y = torch.randn((0, 1, 3, 0), device=device)
        self.assertEqual(y.shape, torch.cross(y, y).shape)

        # renorm
        self.assertEqual(shape, torch.renorm(x, 1, 0, 5).shape)
        self.assertEqual(shape, torch.renorm(x, 1, 2, 5).shape)

        # sort
        self.assertEqual([shape, shape], [z.shape for z in torch.sort(x, dim=0)])
        self.assertEqual([shape, shape], [z.shape for z in torch.sort(x, dim=2)])

        # topk
        self.assertEqual([shape, shape], [z.shape for z in torch.topk(x, 0, dim=0)])
        self.assertEqual([(0, 1, 1, 0), (0, 1, 1, 0)], [z.shape for z in torch.topk(x, 1, dim=2)])

        y = torch.randn((2, 3, 4), device=device)
        self.assertEqual([(2, 3, 0), (2, 3, 0)], [z.shape for z in torch.topk(y, 0)])

        # gather
        self.assertEqual(shape, torch.gather(x, 0, torch.empty(shape, dtype=torch.int64, device=device)).shape)
        self.assertEqual(shape, torch.gather(x, 2, torch.empty(shape, dtype=torch.int64, device=device)).shape)
        larger_shape = torch.empty((0, 1, 3, 0), dtype=torch.int64, device=device)
        self.assertEqual(larger_shape.shape, torch.gather(x, 2, larger_shape).shape)
        smaller_shape = torch.empty((0, 1, 0, 0), dtype=torch.int64, device=device)
        self.assertEqual(smaller_shape.shape, torch.gather(x, 2, smaller_shape).shape)
        y = torch.randn((2, 3, 4), device=device)
        self.assertEqual((0, 3, 4),
                         torch.gather(y, 0, torch.empty((0, 3, 4), dtype=torch.int64, device=device)).shape)

        # scatter, scatter_add
        for dim in [0, 2]:
            y = torch.randn(shape, device=device)
            y_src = torch.randn(shape, device=device)
            ind = torch.empty(shape, dtype=torch.int64, device=device)
            self.assertEqual(shape, y.scatter_(dim, ind, y_src).shape)
            self.assertEqual(shape, y.scatter_add_(dim, ind, y_src).shape)

        z = torch.randn((2, 3, 4), device=device)
        z_src = torch.randn((2, 3, 4), device=device)
        self.assertEqual(z, z.scatter_(2, torch.empty((2, 3, 0), dtype=torch.int64, device=device), z_src))
        self.assertEqual(z, z.scatter_add_(2, torch.empty((2, 3, 0), dtype=torch.int64, device=device), z_src))

        # index_fill, index_copy, index_add
        c = x.clone()
        c_clone = c.clone()
        ind_empty = torch.tensor([], dtype=torch.int64, device=device)
        ind_01 = torch.tensor([0, 1], dtype=torch.int64, device=device)
        self.assertEqual(c_clone, c.index_fill_(0, ind_empty, -1))
        self.assertEqual(c_clone, c.index_fill_(2, ind_empty, -1))
        self.assertEqual(c_clone, c.index_fill_(2, torch.tensor([0, 1], dtype=torch.int64, device=device), -1))
        self.assertEqual(c_clone, c.index_copy_(0, ind_empty, torch.empty((0, 1, 2, 0), device=device)))
        self.assertEqual(c_clone, c.index_copy_(2, ind_empty, torch.empty((0, 1, 0, 0), device=device)))
        self.assertEqual(c_clone, c.index_copy_(2, ind_01, torch.empty((0, 1, 2, 0), device=device)))
        self.assertEqual(c_clone, c.index_add_(0, ind_empty, torch.empty((0, 1, 2, 0), device=device)))
        self.assertEqual(c_clone, c.index_add_(2, ind_empty, torch.empty((0, 1, 0, 0), device=device)))
        self.assertEqual(c_clone, c.index_add_(2, ind_01, torch.empty((0, 1, 2, 0), device=device)))

        c = torch.randn((0, 1, 2), device=device)
        c_clone = c.clone()
        self.assertEqual(c_clone, c.index_fill_(0, ind_empty, -1))
        self.assertEqual(c_clone, c.index_copy_(0, ind_empty, torch.empty((0, 1, 2), device=device)))
        self.assertEqual(c_clone, c.index_add_(0, ind_empty, torch.empty((0, 1, 2), device=device)))
        self.assertEqual(c_clone, c.index_fill_(0, ind_empty, -1))
        self.assertEqual(c_clone, c.index_copy_(0, ind_empty, torch.empty((0, 1, 2), device=device)))
        self.assertEqual(c_clone, c.index_add_(0, ind_empty, torch.empty((0, 1, 2), device=device)))

        # index fill/copy/add non-empty
        z = torch.randn((2, 3, 4), device=device)
        self.assertEqual(z, z.index_fill_(0, ind_empty, -1))
        z = torch.randn((2, 3, 4), device=device)
        self.assertEqual(z, z.index_copy_(0, ind_empty, torch.empty((0, 3, 4), device=device)))
        z = torch.randn((2, 3, 4), device=device)
        self.assertEqual(z, z.index_add_(0, ind_empty, torch.empty((0, 3, 4), device=device)))

        # index_select
        self.assertEqual(x, x.index_select(0, ind_empty))
        self.assertEqual((0, 1, 0, 0), x.index_select(2, ind_empty).shape)
        self.assertEqual(x, x.index_select(2, ind_01))
        z = torch.randn((2, 3, 4), device=device)  # non-empty
        self.assertEqual((0, 3, 4), z.index_select(0, ind_empty).shape)
        c = torch.randn((0, 1, 2), device=device)
        self.assertEqual(c, c.index_select(0, ind_empty))
        c = torch.randn((0, 1, 2), device=device)
        self.assertEqual(c, c.index_select(0, ind_empty))

    @dtypes(*torch.testing.get_all_dtypes(include_complex=False))
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_nonzero(self, device, dtype):

        shapes = [
            torch.Size((12,)),
            torch.Size((12, 1)),
            torch.Size((1, 12)),
            torch.Size((6, 2)),
            torch.Size((3, 2, 2)),
            torch.Size((5, 5, 5)),
        ]

        def gen_nontrivial_input(shape, dtype, device):
            if dtype != torch.bfloat16:
                return torch.randint(2, shape, device=device, dtype=dtype)
            else:
                # windows does not work for bfloat16 randing
                return torch.randint(2, shape, device=device, dtype=torch.float).to(dtype)

        for shape in shapes:
            tensor = gen_nontrivial_input(shape, dtype, device)
            dst1 = torch.nonzero(tensor, as_tuple=False)
            dst2 = tensor.nonzero(as_tuple=False)
            dst3 = torch.empty([], dtype=torch.long, device=device)
            torch.nonzero(tensor, out=dst3)
            if self.device_type != 'xla':
                # xla does not raise runtime error
                self.assertRaisesRegex(
                    RuntimeError,
                    "scalar type Long",
                    lambda: torch.nonzero(tensor, out=torch.empty([], dtype=torch.float))
                )
            if self.device_type == 'cuda':
                self.assertRaisesRegex(
                    RuntimeError,
                    "on the same device",
                    lambda: torch.nonzero(tensor, out=torch.empty([], dtype=torch.long))
                )
            np_array = tensor.cpu().numpy() if dtype != torch.bfloat16 else tensor.float().cpu().numpy()
            np_result = torch.from_numpy(np.stack(np_array.nonzero())).t()
            self.assertEqual(dst1.cpu(), np_result, atol=0, rtol=0)
            self.assertEqual(dst2.cpu(), np_result, atol=0, rtol=0)
            self.assertEqual(dst3.cpu(), np_result, atol=0, rtol=0)
            tup1 = torch.nonzero(tensor, as_tuple=True)
            tup2 = tensor.nonzero(as_tuple=True)
            tup1 = torch.stack(tup1).t().cpu()
            tup2 = torch.stack(tup2).t().cpu()
            self.assertEqual(tup1, np_result, atol=0, rtol=0)
            self.assertEqual(tup2, np_result, atol=0, rtol=0)

    def test_nonzero_astuple_out(self, device):
        t = torch.randn((3, 3, 3), device=device)
        out = torch.empty_like(t, dtype=torch.long)

        with self.assertRaises(RuntimeError):
            torch.nonzero(t, as_tuple=True, out=out)

        self.assertEqual(torch.nonzero(t, as_tuple=False, out=out), torch.nonzero(t, out=out))

        # Verifies that JIT script cannot handle the as_tuple kwarg
        # See Issue https://github.com/pytorch/pytorch/issues/45499.
        def _foo(t):
            tuple_result = torch.nonzero(t, as_tuple=True)
            nontuple_result = torch.nonzero(t, as_tuple=False)
            out = torch.empty_like(nontuple_result)
            torch.nonzero(t, as_tuple=False, out=out)
            return tuple_result, nontuple_result, out

        with self.assertRaises(RuntimeError):
            scripted_foo = torch.jit.script(_foo)

        # Verifies that JIT tracing works fine
        traced_foo = torch.jit.trace(_foo, t)
        traced_tuple, traced_nontuple, traced_out = traced_foo(t)
        expected_tuple = torch.nonzero(t, as_tuple=True)
        expected_nontuple = torch.nonzero(t)

        self.assertEqual(traced_tuple, expected_tuple)
        self.assertEqual(traced_nontuple, expected_nontuple)
        self.assertEqual(traced_out, expected_nontuple)

    @onlyOnCPUAndCUDA
    def test_nonzero_discontiguous(self, device):
        shape = (4, 4)
        tensor = torch.randint(2, shape, device=device)
        tensor_nc = torch.empty(shape[0], shape[1] * 2, device=device)[:, ::2].copy_(tensor)
        dst1 = tensor.nonzero(as_tuple=False)
        dst2 = tensor_nc.nonzero(as_tuple=False)
        self.assertEqual(dst1, dst2, atol=0, rtol=0)
        dst3 = torch.empty_like(dst1)
        data_ptr = dst3.data_ptr()
        # expect dst3 storage to be reused
        torch.nonzero(tensor, out=dst3)
        self.assertEqual(data_ptr, dst3.data_ptr())
        self.assertEqual(dst1, dst3, atol=0, rtol=0)
        # discontiguous out
        dst4 = torch.empty(dst1.size(0), dst1.size(1) * 2, dtype=torch.long, device=device)[:, ::2]
        data_ptr = dst4.data_ptr()
        strides = dst4.stride()
        torch.nonzero(tensor, out=dst4)
        self.assertEqual(data_ptr, dst4.data_ptr())
        self.assertEqual(dst1, dst4, atol=0, rtol=0)
        self.assertEqual(strides, dst4.stride())

    def test_nonzero_non_diff(self, device):
        x = torch.randn(10, requires_grad=True)
        nz = x.nonzero()
        self.assertFalse(nz.requires_grad)

    def _brute_pdist(self, inp, p=2):
        """Computes the same as torch.pdist using primitives"""
        n = inp.shape[-2]
        k = n * (n - 1) // 2
        if k == 0:
            # torch complains about empty indices
            return torch.empty(inp.shape[:-2] + (0,), dtype=inp.dtype, device=inp.device)
        square = torch.norm(inp[..., None, :] - inp[..., None, :, :], p=p, dim=-1)
        unroll = square.view(square.shape[:-2] + (n * n,))
        inds = torch.ones(k, dtype=torch.int)
        inds[torch.arange(n - 1, 1, -1, dtype=torch.int).cumsum(0)] += torch.arange(2, n, dtype=torch.int)
        return unroll[..., inds.cumsum(0)]

    def _pdist_single(self, shape, device, p, dtype, trans, grad_check=False):
        x = torch.randn(shape, dtype=dtype, device=device)
        if trans:
            x.transpose_(-2, -1)
        if grad_check:
            x.requires_grad_()
            y = x.detach().clone().requires_grad_()
        else:
            y = x
        actual = torch.pdist(x, p=p)
        expected = self._brute_pdist(y, p=p)
        self.assertEqual(expected.shape, actual.shape)
        self.assertEqual(expected, actual)
        if grad_check and expected.size() != torch.Size([0]):
            g0 = torch.rand_like(actual)
            actual.backward(g0)
            expected.backward(g0)
            self.assertEqual(x.grad, y.grad)

    @slowTest
    def test_pdist_norm_forward(self, device):
        for shape in [(4, 5), (3, 2), (2, 1), (1500, 1)]:
            for p in [0, 1, 2, 3, 1.5, 2.5, float('inf')]:
                for trans in [False, True]:
                    for dtype in [torch.float32, torch.float64]:
                        self._pdist_single(shape, device, p, dtype, trans, grad_check=False)

        # do a simplified comparison with big inputs, see:
        # https://github.com/pytorch/pytorch/issues/15511
        for dtype in [torch.float32, torch.float64]:
            self._pdist_single((1000, 2), device, 2, dtype, trans=False, grad_check=False)

    @slowTest
    def test_pdist_norm_backward(self, device):
        for shape in [(4, 5), (3, 2), (2, 1), (1500, 1)]:
            for p in [0, 1, 2, 3, 1.5, 2.5, float('inf')]:
                for trans in [False, True]:
                    self._pdist_single(shape, device, p, torch.float64, trans, grad_check=True)

    @skipIfRocm
    def test_pdist_norm_large(self, device):
        # use dim0>=46342 for forward, see:
        # https://github.com/pytorch/pytorch/issues/30583
        # Compare output using GPU with the CPU implementation, as brute_pdist uses too much memory
        if 'cuda' in device:
            x = torch.randn(50000, 1, dtype=torch.float32)
            expected_cpu = torch.pdist(x, p=2)
            actual_gpu = torch.pdist(x.to(device), p=2)
            self.assertEqual(expected_cpu, actual_gpu.cpu())

    def test_atan2(self, device):
        def _test_atan2_with_size(size, device):
            a = torch.rand(size=size, device=device, dtype=torch.double)
            b = torch.rand(size=size, device=device, dtype=torch.double)
            actual = a.atan2(b)
            x = a.view(-1)
            y = b.view(-1)
            expected = torch.tensor([math.atan2(x[i].item(), y[i].item()) for i in range(x.numel())],
                                    device=device, dtype=torch.double)
            self.assertEqual(expected, actual.view(-1), rtol=0, atol=0.02)

        _test_atan2_with_size((2, 2), device)
        _test_atan2_with_size((3, 3), device)
        _test_atan2_with_size((5, 5), device)

    def test_atan2_edgecases(self, device):
        def _test_atan2(x, y, expected, device, dtype):
            expected_tensor = torch.tensor([expected], dtype=dtype, device=device)
            x_tensor = torch.tensor([x], dtype=dtype, device=device)
            y_tensor = torch.tensor([y], dtype=dtype, device=device)
            actual = torch.atan2(y_tensor, x_tensor)
            self.assertEqual(expected_tensor, actual, rtol=0, atol=0.02)

        for dtype in [torch.float, torch.double]:
            _test_atan2(0, 0, 0, device, dtype)
            _test_atan2(0, 1, math.pi / 2, device, dtype)
            _test_atan2(0, -1, math.pi / -2, device, dtype)
            _test_atan2(-1, 0, math.pi, device, dtype)
            _test_atan2(1, 0, 0, device, dtype)
            _test_atan2(-1, -1, math.pi * -3 / 4 , device, dtype)
            _test_atan2(1, 1, math.pi / 4 , device, dtype)
            _test_atan2(1, -1, math.pi / -4 , device, dtype)
            _test_atan2(-1, 1, math.pi * 3 / 4 , device, dtype)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_trapz(self, device):
        def test_dx(sizes, dim, dx, device):
            t = torch.randn(sizes, device=device)
            actual = torch.trapz(t, dx=dx, dim=dim)
            expected = np.trapz(t.cpu().numpy(), dx=dx, axis=dim)
            self.assertEqual(expected.shape, actual.shape)
            self.assertEqual(expected, actual)

        def test_x(sizes, dim, x, device):
            t = torch.randn(sizes, device=device)
            actual = torch.trapz(t, x=torch.tensor(x, device=device), dim=dim)
            expected = np.trapz(t.cpu().numpy(), x=x, axis=dim)
            self.assertEqual(expected.shape, actual.shape)
            self.assertEqual(expected, actual.cpu())

        test_dx((2, 3, 4), 1, 1, device)
        test_dx((10, 2), 0, 0.1, device)
        test_dx((1, 10), 0, 2.3, device)
        test_dx((0, 2), 0, 1.0, device)
        test_dx((0, 2), 1, 1.0, device)
        test_x((2, 3, 4), 1, [1.0, 2.0, 3.0], device)
        test_x((10, 2), 0, [2.0, 3.0, 4.0, 7.0, 11.0, 14.0, 22.0, 26.0, 26.1, 30.3], device)
        test_x((1, 10), 0, [1.0], device)
        test_x((0, 2), 0, [], device)
        test_x((0, 2), 1, [1.0, 2.0], device)
        with self.assertRaisesRegex(
                IndexError,
                'Dimension out of range'):
            test_x((2, 3), 2, [], device)
            test_dx((2, 3), 2, 1.0, device)
        with self.assertRaisesRegex(
                RuntimeError,
                'There must be one `x` value for each sample point'):
            test_x((2, 3), 1, [1.0, 2.0], device)
            test_x((2, 3), 1, [1.0, 2.0, 3.0, 4.0], device)

    def test_reduction_empty(self, device):
        fns_to_test = [
            # name, function, identity
            ('max', torch.max, None),
            ('amax', torch.amax, None),
            ('kthvalue', lambda *args, **kwargs: torch.kthvalue(*args, k=1, **kwargs), None),
            ('argmax', torch.argmax, None),
            ('min', torch.min, None),
            ('amin', torch.amin, None),
            ('argmin', torch.argmin, None),
            ('mode', torch.mode, None),
            ('median', torch.median, None),

            ('prod', torch.prod, 1.),
            ('sum', torch.sum, 0.),
            ('norm', torch.norm, 0.),
            ('mean', torch.mean, nan),
            ('var', torch.var, nan),
            ('std', torch.std, nan),
            ('logsumexp', torch.logsumexp, -inf),
        ]

        shape = (2, 0, 4)
        x = torch.randn(shape, device=device)

        for fn in [torch.max, torch.min]:
            ident_err = 'operation does not have an identity'
            self.assertRaisesRegex(RuntimeError, ident_err, lambda: fn(x))

        for item in fns_to_test:
            name, fn, identity = item
            if identity is None:
                ident_err = 'does not have an identity'
                self.assertRaisesRegex(RuntimeError, ident_err, lambda: fn(x, dim=2))
                self.assertRaisesRegex(RuntimeError, ident_err, lambda: fn(x, dim=2, keepdim=True))
                self.assertRaisesRegex(RuntimeError, ident_err, lambda: fn(x, dim=1))
                self.assertRaisesRegex(RuntimeError, ident_err, lambda: fn(x, dim=1, keepdim=True))
            else:
                self.assertEqual(torch.empty((2, 0), device=device), fn(x, dim=2))
                self.assertEqual(torch.empty((2, 0, 1), device=device), fn(x, dim=2, keepdim=True))
                # assertEqual doesn't work with inf, -inf, nan and two tensors.
                check = (torch.testing.assert_allclose if math.isnan(identity) or math.isinf(identity) else
                         self.assertEqual)
                check(torch.full((2, 4), identity, device=device), fn(x, dim=1))
                check(torch.full((2, 1, 4), identity, device=device), fn(x, dim=1, keepdim=True))
                try:
                    check(torch.full((), identity, device=device), fn(x))
                except TypeError as err:
                    # ignore if there is no allreduce.
                    self.assertTrue('dim' in str(err))

        # any
        xb = x.to(torch.uint8)
        yb = x.to(torch.uint8)
        self.assertEqual((2, 0), xb.any(2).shape)
        self.assertEqual((2, 0, 1), xb.any(2, keepdim=True).shape)
        self.assertEqual(torch.zeros((2, 4), device=device, dtype=torch.uint8), xb.any(1))
        self.assertEqual(torch.zeros((2, 1, 4), device=device, dtype=torch.uint8), xb.any(1, keepdim=True))
        self.assertEqual(torch.zeros((), device=device, dtype=torch.uint8), xb.any())

        # all
        self.assertEqual((2, 0), xb.all(2).shape)
        self.assertEqual((2, 0, 1), xb.all(2, keepdim=True).shape)
        self.assertEqual(torch.ones((2, 4), device=device, dtype=torch.uint8), xb.all(1))
        self.assertEqual(torch.ones((2, 1, 4), device=device, dtype=torch.uint8), xb.all(1, keepdim=True))
        self.assertEqual(torch.ones((), device=device, dtype=torch.uint8), xb.all())

    @onlyOnCPUAndCUDA
    def test_addcdiv(self, device):
        def _test_addcdiv(a, alpha, b, c):
            actual = torch.addcdiv(a, b, c, value=alpha)
            # implementation of addcdiv downcasts alpha. arithmetic ops don't.
            if not actual.dtype.is_floating_point:
                alpha = int(alpha)
            expected = a + (alpha * b) / c
            self.assertEqual(expected, actual)

            with self.maybeWarnsRegex(
                    UserWarning, "This overload of addcdiv is deprecated"):
                self.assertEqual(actual, torch.addcdiv(a, alpha, b, c))

        def non_zero_rand(size, dtype, device):
            if dtype.is_floating_point or dtype.is_complex:
                a = torch.rand(size=size, dtype=dtype, device=device)
            elif dtype == torch.uint8:
                a = torch.randint(1, 5, size=size, dtype=dtype, device=device)
            else:
                a = torch.randint(-5, 5, size=size, dtype=dtype, device=device)
            return a + (a == 0).to(dtype)

        def _helper():
            _test_addcdiv(
                non_zero_rand((2, 2), dtype=dtype, device=device),
                0.5,
                non_zero_rand((2, 2), dtype=dtype, device=device),
                non_zero_rand((2, 2), dtype=dtype, device=device))

        for dtype in torch.testing.get_all_math_dtypes(device):
            if dtype.is_complex:
                # CPU complex addcdiv is wildly inaccurate
                if self.device_type == 'cpu':
                    with self.assertRaises(AssertionError):
                        _helper()

                # CUDA complex addcdiv is not implemented
                if self.device_type == 'cuda':
                    with self.assertRaises(RuntimeError):
                        _helper()
            elif not dtype.is_floating_point:
                # Integer division with addcdiv is prohibited
                with self.assertRaises(RuntimeError):
                    _helper()
            else:
                _helper()

    # This function tests that a nan value is returned for input values not in domain
    @dtypes(torch.float32, torch.float64)
    def test_acosh_domain_float(self, device, dtype):
        # Domain of acosh is [1, inf), for values outside the domain - output is mapped
        # to NaN, except for input value `inf` - output is mapped to `inf`
        sample = torch.tensor([float('-inf'), 1.00, -1.23, -0.06, 0.98, float('inf')],
                              device=device, dtype=dtype)
        nan_mask = torch.tensor([True, False, True, True, True, False], device=device)
        inf_mask = torch.tensor([False, False, False, False, False, True], device=device)
        self.assertEqual(torch.isnan(torch.acosh(sample)), nan_mask)
        self.assertEqual(torch.isnan(sample.acosh()), nan_mask)
        self.assertEqual(torch.isinf(torch.acosh(sample)), inf_mask)
        self.assertEqual(torch.isinf(sample.acosh()), inf_mask)

    # This function tests that a nan value is returned for input values not in domain
    @dtypes(torch.float32, torch.float64)
    def test_atanh_domain_float(self, device, dtype):
        # Domain of atanh is (-1, 1), for edge values (-1 and 1) - output is mapped
        # to inf and for other values outside this range - output is mapped to NaN
        sample = torch.tensor([float('-inf'), -1.00, 1.00, -1.23, 1.06, float('inf')],
                              device=device, dtype=dtype)
        nan_mask = torch.tensor([True, False, False, True, True, True], device=device)
        inf_mask = torch.tensor([False, True, True, False, False, False], device=device)
        # For values not in domain (except -1.0 and 1.0), atanh should return nan
        self.assertEqual(torch.isnan(torch.atanh(sample)), nan_mask)
        self.assertEqual(torch.isnan(sample.atanh()), nan_mask)
        # For values -1.0 and 1.0, atanh should return -inf and inf respectively
        self.assertEqual(torch.isinf(torch.atanh(sample)), inf_mask)
        self.assertEqual(torch.isinf(sample.atanh()), inf_mask)

    def test_nullary_op_mem_overlap(self, device):
        ops = (
            ("random_", ()),
            ("uniform_", ()),
            ("cauchy_", ()),
            ("log_normal_", ()),
            ("exponential_", ()),
            ("geometric_", (0.5,)),
            ("normal_", ()),
        )

        x = torch.rand((1, 3)).expand((3, 3))
        for op, args in ops:
            with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
                getattr(x, op)(*args)

    # TODO: run on non-native device types
    @dtypes(torch.double)
    def test_unary_out_op_mem_overlap(self, device, dtype):
        sz = 3
        doubles = torch.randn(2 * sz, dtype=dtype, device=device)
        positives = torch.randint(1, 100, (2 * sz,), device=device).double()
        ints = torch.randint(-100, 100, (2 * sz,), device=device)
        unary_mem_overlap_cases = [
            ("abs", doubles, True, True, 'cpu'),
            ("abs", doubles, True, True, 'cuda'),
            ("acos", doubles, True, True, 'cpu'),
            ("acos", doubles, True, True, 'cuda'),
            ("asin", doubles, True, True, 'cpu'),
            ("asin", doubles, True, True, 'cuda'),
            ("atan", doubles, True, True, 'cpu'),
            ("atan", doubles, True, True, 'cuda'),
            ("acosh", doubles, True, True, 'cpu'),
            ("acosh", doubles, True, True, 'cuda'),
            ("asinh", doubles, True, True, 'cpu'),
            ("asinh", doubles, True, True, 'cuda'),
            ("atanh", doubles, True, True, 'cpu'),
            ("atanh", doubles, True, True, 'cuda'),
            ("bitwise_not", ints, True, True, 'cpu'),
            ("bitwise_not", ints, True, True, 'cuda'),
            ("ceil", doubles, True, True, 'cpu'),
            ("ceil", doubles, True, True, 'cuda'),
            ("cos", doubles, True, True, 'cpu'),
            ("cos", doubles, True, True, 'cuda'),
            ("cosh", doubles, True, True, 'cpu'),
            ("cosh", doubles, True, True, 'cuda'),
            ("digamma", doubles, True, True, 'cpu'),
            ("erf", doubles, True, True, 'cpu'),
            ("erf", doubles, True, True, 'cuda'),
            ("erfc", doubles, True, True, 'cpu'),
            ("erfc", doubles, True, True, 'cuda'),
            ("erfinv", doubles, True, True, 'cpu'),
            ("erfinv", doubles, True, True, 'cuda'),
            ("exp", doubles, True, True, 'cpu'),
            ("exp", doubles, True, True, 'cuda'),
            ("exp2", doubles, True, True, 'cpu'),
            ("exp2", doubles, True, True, 'cuda'),
            ("expm1", doubles, True, True, 'cpu'),
            ("expm1", doubles, True, True, 'cuda'),
            ("floor", doubles, True, True, 'cpu'),
            ("floor", doubles, True, True, 'cuda'),
            ("frac", doubles, True, True, 'cpu'),
            ("frac", doubles, True, True, 'cuda'),
            ("i0", doubles, True, True, 'cpu'),
            ("i0", doubles, True, True, 'cuda'),
            ("log", positives, True, True, 'cpu'),
            ("log", positives, True, True, 'cuda'),
            ("log10", positives, True, True, 'cpu'),
            ("log10", positives, True, True, 'cuda'),
            ("log1p", positives, True, True, 'cpu'),
            ("log1p", positives, True, True, 'cuda'),
            ("log2", positives, True, True, 'cpu'),
            ("log2", positives, True, True, 'cuda'),
            ("neg", doubles, True, True, 'cpu'),
            ("neg", doubles, True, True, 'cuda'),
            ("reciprocal", doubles, True, True, 'cpu'),
            ("reciprocal", doubles, True, True, 'cuda'),
            ("round", doubles, True, True, 'cpu'),
            ("round", doubles, True, True, 'cuda'),
            ("rsqrt", positives, True, True, 'cpu'),
            ("rsqrt", positives, True, True, 'cuda'),
            ("sin", doubles, True, True, 'cpu'),
            ("sin", doubles, True, True, 'cuda'),
            ("sinh", doubles, True, True, 'cpu'),
            ("sinh", doubles, False, True, 'cuda'),
            ("sigmoid", doubles, True, True, 'cpu'),
            ("sigmoid", doubles, True, True, 'cuda'),
            ("logit", doubles, True, True, 'cpu'),
            ("logit", doubles, True, True, 'cuda'),
            ("sqrt", doubles, True, True, 'cpu'),
            ("sqrt", doubles, False, True, 'cuda'),
            ("tan", doubles, True, True, 'cpu'),
            ("tan", doubles, True, True, 'cuda'),
            ("tanh", doubles, True, True, 'cpu'),
            ("tanh", doubles, True, True, 'cuda'),
            ("trunc", doubles, True, True, 'cpu'),
            ("trunc", doubles, True, True, 'cuda')
        ]

        for (fn, inputs, has_input_output_mem_overlap_check,
             has_internal_mem_overlap_check, dev) in unary_mem_overlap_cases:
            if dev != device:
                continue
            out_fn = getattr(torch, fn)
            in_fn = getattr(torch.Tensor, fn + '_')

            self.unary_check_input_output_mem_overlap(inputs, sz, out_fn,
                                                      expected_failure=not has_input_output_mem_overlap_check)

            self.check_internal_mem_overlap(in_fn, 1, dtype, dev,
                                            expected_failure=not has_internal_mem_overlap_check)

    @dtypes(torch.double)
    def test_binary_op_mem_overlap(self, device, dtype):
        ops = [
            ("add", True, True, 'cpu'),
            ("add", True, True, 'cuda'),
            ("mul", True, True, 'cpu'),
            ("mul", True, True, 'cuda'),
            ("sub", True, True, 'cpu'),
            ("sub", True, True, 'cuda'),
            ("div", True, True, 'cpu'),
            ("div", True, True, 'cuda'),
            ("pow", True, True, 'cpu'),
            ("pow", True, True, 'cuda'),
            ("fmod", True, True, 'cpu'),
            ("fmod", True, True, 'cuda'),
            ("atan2", True, True, 'cpu'),
            ("atan2", True, True, 'cuda'),
            ("hypot", True, True, 'cpu'),
            ("hypot", True, True, 'cuda'),
            ("nextafter", True, True, 'cpu'),
            ("nextafter", True, True, 'cuda'),
            ("le", True, True, 'cpu'),
            ("le", True, True, 'cuda'),
            ("lt", True, True, 'cpu'),
            ("lt", True, True, 'cuda'),
            ("ge", True, True, 'cpu'),
            ("ge", True, True, 'cuda'),
            ("gt", True, True, 'cpu'),
            ("gt", True, True, 'cuda'),
            ("eq", True, True, 'cpu'),
            ("eq", True, True, 'cuda'),
            ("ne", True, True, 'cpu'),
            ("ne", True, True, 'cuda'),
            ("logical_and", True, True, 'cpu'),
            ("logical_and", True, True, 'cuda'),
            ("logical_or", True, True, 'cpu'),
            ("logical_or", True, True, 'cuda'),
            ("logical_xor", True, True, 'cpu'),
            ("logical_xor", True, True, 'cuda'),
        ]

        for (fn, has_input_output_mem_overlap_check,
             has_internal_mem_overlap_check, dev) in ops:
            if dev != device:
                continue
            out_op = getattr(torch, fn)
            inplace_op = getattr(torch.Tensor, fn + '_')
            self.check_internal_mem_overlap(
                inplace_op, 2, dtype, device,
                expected_failure=not has_internal_mem_overlap_check)

            self.binary_check_input_output_mem_overlap(out_op, device,
                                                       expected_failure=not has_input_output_mem_overlap_check)

    @dtypes(torch.double)
    def test_ternary_op_mem_overlap(self, device, dtype):
        ops = [
            ("addcmul", True, True, 'cpu'),
            ("addcmul", True, True, 'cuda'),
            ("addcdiv", True, True, 'cpu'),
            ("addcdiv", True, True, 'cuda'),
            ("lerp", True, True, 'cpu'),
            ("lerp", True, True, 'cuda')
        ]

        for (fn, has_input_output_mem_overlap_check,
             has_internal_mem_overlap_check, dev) in ops:
            if dev != device:
                continue
            out_op = getattr(torch, fn)
            inplace_op = getattr(torch.Tensor, fn + '_')
            self.check_internal_mem_overlap(
                inplace_op, 3, dtype, device,
                expected_failure=not has_internal_mem_overlap_check)
            self.ternary_check_input_output_mem_overlap(out_op, dev,
                                                        expected_failure=not has_input_output_mem_overlap_check)

    @dtypes(torch.double)
    def test_copy_mem_overlap(self, device, dtype):
        self.check_internal_mem_overlap(
            torch.Tensor.copy_, num_inputs=2, dtype=dtype, device=device)
        sz = 3
        doubles = torch.randn(2 * sz, dtype=dtype, device=device)
        self.unary_check_input_output_mem_overlap(
            doubles, sz, lambda input, out: out.copy_(input))

    @dtypes(torch.double)
    def test_pow_scalar_overloads_mem_overlap(self, device, dtype):
        sz = 3
        doubles = torch.randn(2 * sz, dtype=dtype, device=device)
        self.check_internal_mem_overlap(
            lambda t: t.pow_(42), 1, dtype, device)
        self.unary_check_input_output_mem_overlap(
            doubles, sz, lambda input, out: torch.pow(input, 42, out=out))
        self.unary_check_input_output_mem_overlap(
            doubles, sz, lambda input, out: torch.pow(42, input, out=out))

    def test_index_add_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))
        y = torch.rand((6,), device=device)
        ind = torch.tensor([0, 2, 3], device=device)
        value = torch.rand((3,), device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.index_add_(0, ind, value)

    def test_shift_mem_overlap(self, device):
        x = torch.rand(3, device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x[:-1] <<= x[1:]
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x[:-1] >>= x[1:]

    def test_bernoulli_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))

        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.bernoulli_()
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.bernoulli_(p=0.1)
        p = torch.rand(6, device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.bernoulli_(p=p)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.bernoulli(torch.rand_like(x), out=x)

    def test_index_put_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))
        y = torch.rand((6,), device=device)
        ind = torch.tensor([0, 2, 3], device=device)
        value = torch.rand((3,), device=device)
        with self.assertWarnsRegex(UserWarning, 'expanded tensors'):
            x.index_put_((ind,), value)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            y.index_put_((ind,), y[0])

    def test_masked_fill_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))
        mask = torch.tensor([True, False, True, True, False, False], device=device)
        with self.assertWarnsRegex(UserWarning, 'expanded tensors'):
            x.masked_fill_(mask, 0.)

        fill_val = torch.tensor(0., device=device)
        with self.assertWarnsRegex(UserWarning, 'expanded tensors'):
            x.masked_fill_(mask, fill_val)

    def test_masked_select_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((3,))
        y = torch.rand((6,), device=device)
        mask = torch.tensor([True, False, True, True, False, False], device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.masked_select(y, mask, out=x)

    def test_masked_scatter_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))
        src = torch.rand((3,), device=device)
        mask = torch.tensor([True, False, True, True, False, False], device=device)

        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.masked_scatter_(mask, src)

    def test_index_select_mem_overlap(self, device):
        x = torch.rand((1, 6), device=device).expand((2, 6))
        y = torch.rand((3, 6), device=device)
        ind = torch.tensor([0, 1], dtype=torch.int64, device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.index_select(y, 1, ind, out=x)

    def test_cat_mem_overlap(self, device):
        x = torch.rand((1, 3), device=device).expand((6, 3))
        y = torch.rand((3, 3), device=device)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.cat([y, y], out=x)

    def test_scatter_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((6,))
        src = torch.rand((3,), device=device)
        ind = torch.tensor([0, 2, 3], device=device, dtype=torch.int64)

        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            x.scatter_(0, ind, src)

    def test_gather_mem_overlap(self, device):
        x = torch.rand((1,), device=device).expand((3,))
        src = torch.rand((6,), device=device)
        ind = torch.tensor([0, 2, 3], device=device, dtype=torch.int64)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.gather(src, 0, ind, out=x)

    def test_linlogspace_mem_overlap(self, device):
        x = torch.rand(1, device=device).expand(10)
        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.linspace(1, 10, 10, out=x)

        with self.assertRaisesRegex(RuntimeError, 'unsupported operation'):
            torch.logspace(1, 10, 10, out=x)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    def test_int_pow(self, device):

        def _test_integral_pow(dt, range, dev):
            tensor = torch.tensor((3, 3), dtype=dt, device=dev).random_(*range)
            exps = [0, 1, 2, 4,
                    torch.tensor((3, 3), dtype=dt, device=dev).random_(0, 5)]
            for exp in exps:
                self._test_pow(tensor, exp)

        _test_integral_pow(torch.int8, (-3, 4), device)
        _test_integral_pow(torch.uint8, (0, 4), device)
        _test_integral_pow(torch.int16, (-5, 5), device)
        _test_integral_pow(torch.int64, (-10, 10), device)
        _test_integral_pow(torch.int32, (-10, 10), device)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    def test_int_tensor_pow_neg_ints(self, device):
        ints = [torch.iinfo(torch.int32).min,
                -3, -2, -1, 0, 1, 2, 3,
                torch.iinfo(torch.int32).max]
        neg_ints = [torch.iinfo(torch.int32).min, -3, -2, -1]
        tensor = torch.tensor(ints, dtype=torch.int32, device=device)
        for pow in neg_ints:
            self._test_pow(tensor, pow)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    def test_long_tensor_pow_floats(self, device):
        ints = [0, 1, 23, 4567]
        floats = [0.0, 1 / 3, 1 / 2, 1.0, 3 / 2, 2.0]
        tensor = torch.tensor(ints, dtype=torch.int64, device=device)
        for pow in floats:
            self._test_pow(tensor, pow)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    def test_float_scalar_pow_float_tensor(self, device):
        floats = [2.0, -3 / 2, -1.0, -1 / 2, -1 / 3, 0.0,
                  1 / 3, 1 / 2, 1.0, 3 / 2, 2.0]
        tensor = torch.tensor(floats, dtype=torch.float32, device=device)
        for base in floats:
            self._test_pow(base, tensor)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @dtypes(*(torch.testing.get_all_dtypes(include_bool=False, include_bfloat16=False)))
    def test_complex_scalar_pow_tensor(self, device, dtype):
        complexes = [0.5j, 1. + 1.j, -1.5j, 2.2 - 1.6j]
        tensor = torch.rand(100).to(dtype=dtype, device=device)
        for base in complexes:
            self._test_pow(base, tensor)

    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    def test_tensor_pow_tensor(self, dev):
        def rotate(l, n):
            return l[-n:] + l[:-n]

        def test_tensor_pow_tensor(values, torch_type, numpy_type):
            vals_tensor = torch.tensor(values, dtype=torch_type, device=dev)
            for i in range(len(values)):
                pows = rotate(values, i)
                pows_tensor = torch.tensor(pows, dtype=torch_type, device=dev)
                self._test_pow(vals_tensor, pows_tensor)

        ints = [0, 1, 2, 3]
        test_tensor_pow_tensor(ints, torch.int32, np.int32)
        test_tensor_pow_tensor(ints, torch.int64, np.int64)

        floats = [-3.0, -2.0, -1.0, -1 / 2, -1 / 3,
                  0.0,
                  1 / 3, 1 / 2, 1.0, 2.0, 3.0]
        test_tensor_pow_tensor(floats, torch.float32, np.float32)
        test_tensor_pow_tensor(floats, torch.float64, np.float64)

    @dtypes(torch.float)
    def test_add_with_tail(self, device, dtype):
        # test tensor where there is a tail which is not a multiple
        # of GPU warp size
        for tail_size in [1, 63, 67, 130]:
            size = 4096 + tail_size
            a = torch.randn(size, device=device, dtype=dtype)
            b = torch.randn(size, device=device, dtype=dtype)
            c = a + b
            for x, y, z in zip(a.tolist(), b.tolist(), c.tolist()):
                self.assertEqual(x + y, z)

    def test_logical_xor_with_nontrivial_alignment(self, device):
        # test tensor that is not aligned to multiple of 16 bytes
        size = 128
        a = (torch.randn(size, device=device) > 0)
        b = (torch.randn(size, device=device) > 0)
        c = (torch.randn(size, device=device) > 0)
        non_trivial_alignment = [1, 2, 4, 8, 15]
        for i in non_trivial_alignment:
            for j in non_trivial_alignment:
                for k in non_trivial_alignment:
                    a_ = a[i: 100 + i]
                    b_ = b[j: 100 + j]
                    c_ = c[k: 100 + k]
                    torch.logical_xor(a_, b_, out=c_)
                    for x, y, z in zip(a_.tolist(), b_.tolist(), c_.tolist()):
                        self.assertEqual(x ^ y, z)

    def test_var_mean_some_dims(self, device):
        sizes = (4, 6, 7, 5, 3)
        dims = len(sizes)

        x = torch.rand(sizes, device=device)
        for num_of_dims in range(2, dims):
            dim_list = list(combinations(list(range(dims)), r=num_of_dims))
            for dim in dim_list:
                for unbiased in [False, True]:
                    for keepdim in [False, True]:
                        var1, mean1 = torch.var_mean(x, dim=dim, unbiased=unbiased, keepdim=keepdim)
                        var2 = x.var(dim=dim, unbiased=unbiased, keepdim=keepdim)
                        mean2 = x.mean(dim=dim, keepdim=keepdim)
                        self.assertEqual(var1, var2)
                        self.assertEqual(mean1, mean2)

    @skipCUDAIfRocm
    def test_blas_empty(self, device):

        def fn(torchfn, *args, test_out=False, **kwargs):
            def call_torch_fn(*args, **kwargs):
                return torchfn(*tuple(torch.randn(shape, device=device) if isinstance(shape, tuple) else shape
                                      for shape in args), **kwargs)
            result = call_torch_fn(*args, **kwargs)
            if not test_out:
                return result
            else:
                out = torch.full_like(result, math.nan)
                out1 = call_torch_fn(*args, **kwargs, out=out)
                return out

        # mm, addmm
        self.assertEqual((0, 0), fn(torch.mm, (0, 0), (0, 0)).shape)
        self.assertEqual((0, 5), fn(torch.mm, (0, 0), (0, 5)).shape)
        self.assertEqual((5, 0), fn(torch.mm, (5, 0), (0, 0)).shape)
        self.assertEqual((3, 0), fn(torch.mm, (3, 2), (2, 0)).shape)
        self.assertEqual(torch.zeros((5, 6), device=device), fn(torch.mm, (5, 0), (0, 6)))
        self.assertEqual(torch.zeros((5, 6), device=device), fn(torch.mm, (5, 0), (0, 6), test_out=True))

        self.assertEqual((0, 0), fn(torch.addmm, (0, 0), (0, 0), (0, 0)).shape)
        self.assertEqual((0, 1), fn(torch.addmm, (1, ), (0, 17), (17, 1)).shape)
        t = torch.randn((5, 6), device=device)
        self.assertEqual(t, fn(torch.addmm, t, (5, 0), (0, 6)))
        self.assertEqual(t, fn(torch.addmm, t, (5, 0), (0, 6), test_out=True))

        # mv, addmv
        self.assertEqual((0,), fn(torch.mv, (0, 0), (0,)).shape)
        self.assertEqual((0,), fn(torch.mv, (0, 2), (2,)).shape)
        self.assertEqual(torch.zeros((3,), device=device), fn(torch.mv, (3, 0), (0,)))
        self.assertEqual(torch.zeros((3,), device=device), fn(torch.mv, (3, 0), (0,), test_out=True))

        self.assertEqual((0,), fn(torch.addmv, (0,), (0, 0), (0,)).shape)
        t = torch.randn((3,), device=device)
        self.assertEqual(t, fn(torch.addmv, t, (3, 0), (0,)))
        self.assertEqual(t, fn(torch.addmv, t, (3, 0), (0,), test_out=True))

        # bmm, baddbmm
        self.assertEqual((0, 0, 0), fn(torch.bmm, (0, 0, 0), (0, 0, 0)).shape)
        self.assertEqual((3, 0, 5), fn(torch.bmm, (3, 0, 0), (3, 0, 5)).shape)
        self.assertEqual((0, 5, 6), fn(torch.bmm, (0, 5, 0), (0, 0, 6)).shape)
        self.assertEqual(torch.zeros((3, 5, 6), device=device), fn(torch.bmm, (3, 5, 0), (3, 0, 6)))
        self.assertEqual(torch.zeros((3, 5, 6), device=device), fn(torch.bmm, (3, 5, 0), (3, 0, 6), test_out=True))

        self.assertEqual((0, 0, 0), fn(torch.baddbmm, (0, 0, 0), (0, 0, 0), (0, 0, 0)).shape)
        self.assertEqual((3, 0, 5), fn(torch.baddbmm, (3, 0, 5), (3, 0, 0), (3, 0, 5)).shape)
        self.assertEqual((0, 5, 6), fn(torch.baddbmm, (0, 5, 6), (0, 5, 0), (0, 0, 6)).shape)
        self.assertEqual((3, 5, 6), fn(torch.baddbmm, (3, 5, 6), (3, 5, 0), (3, 0, 6)).shape)
        c = torch.arange(30, dtype=torch.float32, device=device).reshape(3, 2, 5)
        self.assertEqual(-2 * c, fn(torch.baddbmm, c, (3, 2, 0), (3, 0, 5), beta=-2))  # Issue #33467
        self.assertEqual(-2 * c, fn(torch.baddbmm, c, (3, 2, 0), (3, 0, 5), beta=-2, test_out=True))  # Issue #33467

        # addbmm
        self.assertEqual((0, 0), fn(torch.addbmm, (0, 0), (0, 0, 0), (0, 0, 0)).shape)
        self.assertEqual((0, 5), fn(torch.addbmm, (0, 5), (3, 0, 0), (3, 0, 5)).shape)
        t = torch.randn((5, 6), device=device)
        self.assertEqual(t, fn(torch.addbmm, t, (0, 5, 0), (0, 0, 6)))
        self.assertEqual(t, fn(torch.addbmm, t, (0, 5, 0), (0, 0, 6), test_out=True))

        # matmul
        self.assertEqual(torch.tensor(0., device=device), fn(torch.matmul, (0,), (0,)))
        self.assertEqual(torch.tensor(0., device=device), fn(torch.matmul, (0,), (0,), test_out=True))
        self.assertEqual((0, 0), fn(torch.matmul, (0, 0), (0, 0)).shape)
        self.assertEqual((0, 0, 0), fn(torch.matmul, (0, 0, 0), (0, 0, 0)).shape)
        self.assertEqual((5, 0, 0), fn(torch.matmul, (5, 0, 0), (5, 0, 0)).shape)
        self.assertEqual(torch.zeros((5, 3, 4), device=device), fn(torch.matmul, (5, 3, 0), (5, 0, 4)))
        self.assertEqual(torch.zeros((5, 3, 4), device=device), fn(torch.matmul, (5, 3, 0), (5, 0, 4), test_out=True))

        # dot
        self.assertEqual(torch.tensor(0., device=device), fn(torch.dot, (0,), (0,)))
        self.assertEqual(torch.tensor(0., device=device), fn(torch.dot, (0,), (0,), test_out=True))

        if torch._C.has_lapack:
            # lu
            A_LU, pivots = fn(torch.lu, (0, 5, 5))
            self.assertEqual([(0, 5, 5), (0, 5)], [A_LU.shape, pivots.shape])
            A_LU, pivots = fn(torch.lu, (0, 0, 0))
            self.assertEqual([(0, 0, 0), (0, 0)], [A_LU.shape, pivots.shape])
            A_LU, pivots = fn(torch.lu, (2, 0, 0))
            self.assertEqual([(2, 0, 0), (2, 0)], [A_LU.shape, pivots.shape])

    @skipCUDAIfRocm
    @dtypesIfCUDA(*(torch.float, torch.double, torch.cfloat, torch.cdouble) +
                  # This test is disabled on CUDA 9, due to:
                  # See: https://github.com/pytorch/pytorch/issues/31006
                  ((torch.half,) if torch.version.cuda and not torch.version.cuda.startswith('9.') else ()))
    @dtypes(*(set(torch.testing.get_all_dtypes()) - {torch.half, torch.bool}))
    def test_blas_alpha_beta_empty(self, device, dtype):
        if dtype is torch.bfloat16 and self.device_type == 'xla':
            # TODO (@zasdfgbnm): this causes the following error on test
            # TestTorchDeviceTypeXLA.test_blas_alpha_beta_empty_xla_bfloat16:
            #
            #   RuntimeError: _th_equal not supported on CPUType for BFloat16
            return
        # ensure beta is respected
        value = 11
        input = torch.full((2,), value, dtype=dtype, device=device)
        mat = torch.ones((2, 0), dtype=dtype, device=device)
        vec = torch.ones((0,), dtype=dtype, device=device)
        out = torch.empty((2,), dtype=dtype, device=device)
        if dtype.is_complex:
            alpha = 6 + 7j
            beta = 3 + 4j
        else:
            alpha = 6
            beta = 3
        self.assertEqual(torch.full((2,), beta * value, dtype=dtype, device=device),
                         torch.addmv(input=input, mat=mat, vec=vec, alpha=alpha, beta=beta))
        self.assertEqual(torch.full((2,), beta * value, dtype=dtype, device=device),
                         torch.addmv(input=input, mat=mat, vec=vec, alpha=alpha, beta=beta, out=out))

        # TODO: update this once torch.addmm is supported for complex
        if dtype.is_complex and device != 'cpu':
            return

        # torch.addmm
        input = torch.full((2, 3), value, dtype=dtype, device=device)
        mat2 = torch.ones((0, 3), dtype=dtype, device=device)
        out = torch.empty((2, 3), dtype=dtype, device=device)
        self.assertEqual(torch.full((2, 3), beta * value, dtype=dtype, device=device),
                         torch.addmm(input=input, mat1=mat, mat2=mat2, alpha=alpha, beta=beta))
        self.assertEqual(torch.full((2, 3), beta * value, dtype=dtype, device=device),
                         torch.addmm(input=input, mat1=mat, mat2=mat2, alpha=alpha, beta=beta, out=out))

    @dtypes(*(torch.testing.get_all_complex_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_blas_nan_out(self, device, dtype):
        # These functions should work correctly with NaN filled outputs,
        # but need special handling, see [NOTE: cpu_zero]
        b = 3
        n = 5
        m = 7
        p = 11

        # torch.mv
        nm = torch.randn((m, n), device=device).t()
        _m = torch.randn((), device=device).expand(m)
        _m_out = torch.full((m,), float('nan'), device=device)
        self.assertEqual(torch.mv(nm, _m), torch.mv(nm, _m, out=_m_out))
        self.assertEqual(0, torch.isnan(torch.mv(nm, _m)).sum())

        # torch.mm
        mp = torch.randn((p, m), device=device).t()
        np_out = torch.full((n, p), float('nan'), device=device)
        self.assertEqual(torch.mm(nm, mp), torch.mm(nm, mp, out=np_out))

        if dtype.is_complex and device.startswith('cuda'):
            return

        # torch.bmm
        bnm = torch.randn((b, m, n), device=device).transpose(1, 2)
        bmp = torch.randn((b, p, m), device=device).transpose(1, 2)
        bnp_out = torch.full((b, n, p), float('nan'), device=device)
        self.assertEqual(torch.bmm(bnm, bmp), torch.bmm(bnm, bmp, out=bnp_out))

    @onlyCPU  # not supported by CUBLAS
    def test_blas_mv_large_input(self, device):
        # This would previously fail if the allocated output had NaNs, see:
        # https://github.com/pytorch/pytorch/issues/31663 and [NOTE: cpu_zero]
        n = 3000
        m = 200

        nm = torch.randn((m, n), device=device).t()
        _m = torch.randn((), device=device).expand(m)
        _m_out = torch.full((m,), 0., device=device)

        self.assertEqual(torch.mv(nm, _m), torch.mv(nm, _m, out=_m_out))

    @skipCUDAIfRocm
    def test_unique_dim(self, device):
        self.assertFalse(hasattr(torch, 'unique_dim'))

        def run_test(device, dtype):
            x = torch.tensor([[[1., 1.],
                               [0., 1.],
                               [2., 1.],
                               [0., 1.]],
                              [[1., 1.],
                               [0., 1.],
                               [2., 1.],
                               [0., 1.]]],
                             dtype=dtype,
                             device=device)
            x_empty = torch.empty(5, 0, dtype=dtype, device=device)
            x_ill_formed_empty = torch.empty(5, 0, 0, dtype=dtype, device=device)
            x_ill_formed_empty_another = torch.empty(5, 0, 5, dtype=dtype, device=device)
            expected_unique_dim0 = torch.tensor([[[1., 1.],
                                                  [0., 1.],
                                                  [2., 1.],
                                                  [0., 1.]]],
                                                dtype=dtype,
                                                device=device)
            expected_inverse_dim0 = torch.tensor([0, 0])
            expected_counts_dim0 = torch.tensor([2])
            expected_unique_dim1 = torch.tensor([[[0., 1.],
                                                  [1., 1.],
                                                  [2., 1.]],
                                                 [[0., 1.],
                                                  [1., 1.],
                                                  [2., 1.]]],
                                                dtype=dtype,
                                                device=device)
            expected_unique_dim1_bool = torch.tensor([[[False, True], [True, True]],
                                                      [[False, True], [True, True]]],
                                                     dtype=torch.bool,
                                                     device=device)
            expected_inverse_dim1 = torch.tensor([1, 0, 2, 0])
            expected_inverse_dim1_bool = torch.tensor([1, 0, 1, 0])
            expected_counts_dim1 = torch.tensor([2, 1, 1])
            expected_counts_dim1_bool = torch.tensor([2, 2])
            expected_unique_dim2 = torch.tensor([[[1., 1.],
                                                  [0., 1.],
                                                  [2., 1.],
                                                  [0., 1.]],
                                                 [[1., 1.],
                                                  [0., 1.],
                                                  [2., 1.],
                                                  [0., 1.]]],
                                                dtype=dtype,
                                                device=device)
            expected_inverse_dim2 = torch.tensor([0, 1])
            expected_counts_dim2 = torch.tensor([1, 1])
            expected_unique_empty = torch.tensor([], dtype=dtype, device=device)
            expected_inverse_empty = torch.tensor([], dtype=torch.long, device=device)
            expected_counts_empty = torch.tensor([], dtype=torch.long, device=device)
            # dim0
            x_unique = torch.unique(x, dim=0)
            self.assertEqual(expected_unique_dim0, x_unique)

            x_unique, x_inverse = torch.unique(
                x,
                return_inverse=True,
                dim=0)
            self.assertEqual(expected_unique_dim0, x_unique)
            self.assertEqual(expected_inverse_dim0, x_inverse)

            x_unique, x_counts = torch.unique(
                x,
                return_inverse=False,
                return_counts=True,
                dim=0)
            self.assertEqual(expected_unique_dim0, x_unique)
            self.assertEqual(expected_counts_dim0, x_counts)

            x_unique, x_inverse, x_counts = torch.unique(
                x,
                return_inverse=True,
                return_counts=True,
                dim=0)
            self.assertEqual(expected_unique_dim0, x_unique)
            self.assertEqual(expected_inverse_dim0, x_inverse)
            self.assertEqual(expected_counts_dim0, x_counts)

            # dim1
            x_unique = torch.unique(x, dim=1)
            if x.dtype == torch.bool:
                self.assertEqual(expected_unique_dim1_bool, x_unique)
            else:
                self.assertEqual(expected_unique_dim1, x_unique)

            x_unique, x_inverse = torch.unique(
                x,
                return_inverse=True,
                dim=1)
            if x.dtype == torch.bool:
                self.assertEqual(expected_unique_dim1_bool, x_unique)
                self.assertEqual(expected_inverse_dim1_bool, x_inverse)
            else:
                self.assertEqual(expected_unique_dim1, x_unique)
                self.assertEqual(expected_inverse_dim1, x_inverse)

            x_unique, x_counts = torch.unique(
                x,
                return_inverse=False,
                return_counts=True,
                dim=1)
            if x.dtype == torch.bool:
                self.assertEqual(expected_unique_dim1_bool, x_unique)
                self.assertEqual(expected_counts_dim1_bool, x_counts)
            else:
                self.assertEqual(expected_unique_dim1, x_unique)
                self.assertEqual(expected_counts_dim1, x_counts)

            x_unique, x_inverse, x_counts = torch.unique(
                x,
                return_inverse=True,
                return_counts=True,
                dim=1)
            if x.dtype == torch.bool:
                self.assertEqual(expected_unique_dim1_bool, x_unique)
                self.assertEqual(expected_inverse_dim1_bool, x_inverse)
                self.assertEqual(expected_counts_dim1_bool, x_counts)
            else:
                self.assertEqual(expected_unique_dim1, x_unique)
                self.assertEqual(expected_inverse_dim1, x_inverse)
                self.assertEqual(expected_counts_dim1, x_counts)

            # dim2
            x_unique = torch.unique(x, dim=2)
            self.assertEqual(expected_unique_dim2, x_unique)

            x_unique, x_inverse = torch.unique(
                x,
                return_inverse=True,
                dim=2)
            self.assertEqual(expected_unique_dim2, x_unique)
            self.assertEqual(expected_inverse_dim2, x_inverse)

            x_unique, x_counts = torch.unique(
                x,
                return_inverse=False,
                return_counts=True,
                dim=2)
            self.assertEqual(expected_unique_dim2, x_unique)
            self.assertEqual(expected_counts_dim2, x_counts)

            x_unique, x_inverse, x_counts = torch.unique(
                x,
                return_inverse=True,
                return_counts=True,
                dim=2)
            self.assertEqual(expected_unique_dim2, x_unique)
            self.assertEqual(expected_inverse_dim2, x_inverse)
            self.assertEqual(expected_counts_dim2, x_counts)

            # test empty tensor
            x_unique, x_inverse, x_counts = torch.unique(
                x_empty,
                return_inverse=True,
                return_counts=True,
                dim=1)
            self.assertEqual(expected_unique_empty, x_unique)
            self.assertEqual(expected_inverse_empty, x_inverse)
            self.assertEqual(expected_counts_empty, x_counts)

            # test not a well formed tensor
            # Checking for runtime error, as this is the expected behaviour
            with self.assertRaises(RuntimeError):
                torch.unique(
                    x_ill_formed_empty,
                    return_inverse=True,
                    return_counts=True,
                    dim=1)

            # test along dim2
            with self.assertRaises(RuntimeError):
                torch.unique(
                    x_ill_formed_empty_another,
                    return_inverse=True,
                    return_counts=True,
                    dim=2)

            # test consecutive version
            y = torch.tensor(
                [[0, 1],
                 [0, 1],
                 [0, 1],
                 [1, 2],
                 [1, 2],
                 [3, 4],
                 [0, 1],
                 [0, 1],
                 [3, 4],
                 [1, 2]],
                dtype=dtype,
                device=device
            )
            expected_y_unique = torch.tensor(
                [[0, 1],
                 [1, 2],
                 [3, 4],
                 [0, 1],
                 [3, 4],
                 [1, 2]],
                dtype=dtype,
                device=device
            )
            expected_y_inverse = torch.tensor([0, 0, 0, 1, 1, 2, 3, 3, 4, 5], dtype=torch.int64, device=device)
            expected_y_counts = torch.tensor([3, 2, 1, 2, 1, 1], dtype=torch.int64, device=device)
            expected_y_inverse_bool = torch.tensor([0, 0, 0, 1, 1, 1, 2, 2, 3, 3], dtype=torch.int64, device=device)
            expected_y_counts_bool = torch.tensor([3, 3, 2, 2], dtype=torch.int64, device=device)
            y_unique, y_inverse, y_counts = torch.unique_consecutive(y, return_inverse=True, return_counts=True, dim=0)
            if x.dtype == torch.bool:
                self.assertEqual(expected_y_inverse_bool, y_inverse)
                self.assertEqual(expected_y_counts_bool, y_counts)
            else:
                self.assertEqual(expected_y_inverse, y_inverse)
                self.assertEqual(expected_y_counts, y_counts)

        run_test(device, torch.float)
        run_test(device, torch.double)
        run_test(device, torch.long)
        run_test(device, torch.uint8)
        run_test(device, torch.bool)

    # Tests that CUDA tensors on different devices cannot be used in the same
    # binary operation, and that CUDA "scalars" cannot be used in the same
    # binary operation as non-scalar CPU tensors.
    @deviceCountAtLeast(2)
    @onlyCUDA
    def test_cross_device_binary_ops(self, devices):
        vals = (1., (2.,))
        cpu_tensor = torch.randn(2, 2)
        for op in (operator.add, torch.add,
                   operator.sub, torch.sub,
                   operator.mul, torch.mul,
                   operator.truediv, torch.true_divide,
                   operator.floordiv, torch.floor_divide):
            for a, b in product(vals, vals):
                a = torch.tensor(a, device=devices[0])
                b = torch.tensor(b, device=devices[1])

                with self.assertRaisesRegex(RuntimeError, "Expected all tensors.+"):
                    op(a, b)
                with self.assertRaisesRegex(RuntimeError, "Expected all tensors.+"):
                    op(b, a)
                with self.assertRaisesRegex(RuntimeError, "Expected all tensors.+"):
                    op(a, cpu_tensor)
                with self.assertRaisesRegex(RuntimeError, "Expected all tensors.+"):
                    op(cpu_tensor, a)

    # This test ensures that a scalar Tensor can be safely used
    # in a binary operation in conjunction with a Tensor on all
    # available CUDA devices
    @deviceCountAtLeast(2)
    @onlyCUDA
    def test_binary_op_scalar_device_unspecified(self, devices):
        scalar_val = torch.tensor(1.)
        for default_device in devices:
            with torch.cuda.device(default_device):
                for device in devices:
                    device_obj = torch.device(device)
                    x = torch.rand(3, device=device)
                    y0 = x * scalar_val
                    self.assertEqual(y0.device, device_obj)
                    y1 = scalar_val * x
                    self.assertEqual(y1.device, device_obj)
                    self.assertEqual(y0, y1)

    def test_div_and_floordiv_vs_python(self, device):
        # Tests torch division ops which can handle both arguments being
        #   scalars.
        # NOTE: torch.floor_divide currently truncates instead of flooring.
        #   the quotient. See https://github.com/pytorch/pytorch/issues/43874.
        def _scalar_helper(python_op, torch_op):
            for a, b in product(range(-10, 10), range(-10, 10)):
                for op in (lambda x: x * .5, lambda x: math.floor(x)):
                    a = op(a)
                    b = op(b)

                    # Skips zero divisors
                    if b == 0:
                        continue

                    expected = python_op(a, b)

                    for op in (operator.truediv, torch.true_divide):
                        actual_scalar = torch_op(a, b)

                        a_t = torch.tensor(a, device=device)
                        b_t = torch.tensor(b, device=device)

                        actual_tensor = torch_op(a_t, b_t)
                        actual_first_tensor = torch_op(a_t, b)
                        actual_second_tensor = torch_op(a, b_t)

                        self.assertEqual(actual_scalar, expected_div)
                        self.assertEqual(actual_tensor.item(), expected_div)
                        self.assertEqual(actual_first_tensor, actual_tensor)
                        self.assertEqual(actual_second_tensor, actual_tensor)

            _scalar_helper(operator.truediv, operator.truediv)
            _scalar_helper(operator.truediv, torch.true_divide)
            _scalar_helper(lambda a, b: math.trunc(a / b), operator.floordiv)
            _scalar_helper(lambda a, b: math.trunc(a / b), torch.floor_divide)

    # NOTE: torch.floor_divide currently truncates instead of flooring.
    # See https://github.com/pytorch/pytorch/issues/43874.
    @onlyOnCPUAndCUDA
    def test_div_and_floordiv_script_vs_python(self, device):
        # Creates jitted functions of two tensors
        def _wrapped_div(a, b):
            return a / b

        def _wrapped_floordiv(a, b):
            return a // b

        scripted_div = torch.jit.script(_wrapped_div)
        scripted_floordiv = torch.jit.script(_wrapped_floordiv)
        for a, b in product(range(-10, 10), range(-10, 10)):
            for op in (lambda x: x * .5, lambda x: math.floor(x)):
                a = op(a)
                b = op(b)

                # Skips zero divisors
                if b == 0:
                    continue

                expected_div = a / b
                expected_truncdiv = math.trunc(a / b)
                a_t = torch.tensor(a, device=device)
                b_t = torch.tensor(b, device=device)

                self.assertEqual(scripted_div(a_t, b_t), expected_div)
                self.assertEqual(scripted_floordiv(a_t, b_t), expected_truncdiv)

        # Creates jitted functions of one tensor
        def _wrapped_div_scalar(a):
            return a / 5

        # NOTE: this will fail when given an integer input, since
        #   the JIT implements division as
        #   torch.reciprocal(a) * 5, and reciprocal is only
        #   implemented for float types.
        def _wrapped_rdiv_scalar(a):
            return 5 / a

        def _wrapped_floordiv_scalar(a):
            return a // 5

        # NOTE: this fails if the input is not an integer tensor
        # See https://github.com/pytorch/pytorch/issues/45199
        def _wrapped_rfloordiv_scalar(a):
            return 5 // a

        scripted_div_scalar = torch.jit.script(_wrapped_div_scalar)
        scripted_rdiv_scalar = torch.jit.script(_wrapped_rdiv_scalar)
        scripted_floordiv_scalar = torch.jit.script(_wrapped_floordiv_scalar)
        scripted_rfloordiv_scalar = torch.jit.script(_wrapped_rfloordiv_scalar)

        for a in range(-10, 10):
            for op in (lambda x: x * .5, lambda x: math.floor(x)):
                a = op(a)

                a_t = torch.tensor(a, device=device)

                self.assertEqual(a / 5, scripted_div_scalar(a_t))
                self.assertEqual(math.trunc(a / 5), scripted_floordiv_scalar(a_t))

                # Skips zero divisors
                if a == 0:
                    continue

                if a_t.is_floating_point():
                    self.assertEqual(5 / a, scripted_rdiv_scalar(a_t))
                else:
                    with self.assertRaises(RuntimeError):
                        scripted_rdiv_scalar(a_t)


                # Handles Issue 45199 (see comment above)
                if a_t.is_floating_point():
                    with self.assertRaises(RuntimeError):
                        scripted_rfloordiv_scalar(a_t)
                else:
                    self.assertEqual(5 // a, scripted_rfloordiv_scalar(a_t))

    # NOTE: torch.floor_divide currently truncates instead of flooring
    #   the quotient. See https://github.com/pytorch/pytorch/issues/43874.
    @onlyOnCPUAndCUDA
    def test_idiv_and_ifloordiv_vs_python(self, device):
        def _wrapped_idiv_tensor(a, b):
            a /= b
            return a

        def _wrapped_idiv_scalar(a):
            a /= 5
            return a

        def _wrapped_true_divide__tensor(a, b):
            a.true_divide_(b)
            return a

        def _wrapped_true_divide__scalar(a):
            a.true_divide_(5)
            return a

        def _wrapped_floor_divide__tensor(a, b):
            a.floor_divide_(b)
            return a

        def _wrapped_floor_divide__scalar(a):
            a.floor_divide_(5)
            return a

        # The following functions are unsupported by the JIT
        def _wrapped_ifloordiv_tensor(a, b):
            a //= b
            return a

        def _wrapped_ifloordiv_scalar(a):
            a //= 5
            return a

        with self.assertRaises(torch.jit.frontend.NotSupportedError):
            scripted_ifloordiv_tensor = torch.jit.script(_wrapped_ifloordiv_tensor)

        with self.assertRaises(torch.jit.frontend.NotSupportedError):
            scripted_ifloordiv_scalar = torch.jit.script(_wrapped_ifloordiv_scalar)

        scripted_idiv_tensor = torch.jit.script(_wrapped_idiv_tensor)
        scripted_idiv_scalar = torch.jit.script(_wrapped_idiv_scalar)
        scripted_true_divide__tensor = torch.jit.script(_wrapped_true_divide__tensor)
        scripted_true_divide__scalar = torch.jit.script(_wrapped_true_divide__scalar)
        scripted_floor_divide__tensor = torch.jit.script(_wrapped_floor_divide__tensor)
        scripted_floor_divide__scalar = torch.jit.script(_wrapped_floor_divide__scalar)

        for a, b in product(range(-10, 10), range(-10, 10)):
            for op in (lambda x: x * .5, lambda x: math.floor(x)):
                a = op(a)
                b = op(b)

                # Skips zero divisors
                if b == 0:
                    continue

                expected_idiv = a / b
                expected_ifloordiv = a // b
                expected_itruncdiv = math.trunc(a / b)

                a_t = torch.tensor(a, device=device)
                b_t = torch.tensor(b, device=device)

                if a_t.is_floating_point():
                    tmp0 = a_t.clone()
                    tmp0 /= b

                    tmp1 = a_t.clone()
                    tmp1 /= b_t

                    self.assertEqual(tmp0.item(), expected_idiv)
                    self.assertEqual(tmp1.item(), expected_idiv)
                    self.assertEqual(scripted_true_divide__tensor(a_t.clone(), b_t).item(), expected_idiv)
                    self.assertEqual(scripted_true_divide__scalar(a_t.clone()).item(), a / 5)
                else:
                    tmp = a_t.clone()
                    with self.assertRaises(RuntimeError):
                        tmp /= b
                    with self.assertRaises(RuntimeError):
                        tmp /= b_t
                    with self.assertRaises(RuntimeError):
                        scripted_true_divide__tensor(tmp, b_t)
                    with self.assertRaises(RuntimeError):
                        scripted_true_divide__scalar(tmp)


                if not a_t.is_floating_point() and b_t.is_floating_point():
                    # Inplace modification fails because a float tensor is required
                    #   if the divisor is a float tensor
                    with self.assertRaises(RuntimeError):
                        a_t.clone().floor_divide_(b_t)
                    with self.assertRaises(RuntimeError):
                        scripted_floor_divide_tensor(a_t.clone(), b_t)
                    tmp = a_t.clone()
                    with self.assertRaises(RuntimeError):
                        tmp //= b_t
                else:
                    # Inplace modification is OK when both or neither tensor is
                    #   a float tensor
                    self.assertEqual(a_t.clone().floor_divide_(b_t).item(), expected_itruncdiv)
                    self.assertEqual(scripted_floor_divide__tensor(a_t.clone(), b_t).item(), expected_itruncdiv)
                    tmp = a_t.clone()
                    tmp //= b_t
                    self.assertEqual(tmp.item(), expected_itruncdiv)

                self.assertEqual(scripted_floor_divide__scalar(a_t), math.trunc(a / 5))

    # Tests binary op equivalence with Python builtin ops
    # Also tests that reverse operations are equivalent to forward ops
    # NOTE: division ops are tested separately above
    def test_binary_ops_with_scalars(self, device):
        for ops in ((operator.add, torch.add),
                    (operator.sub, torch.sub),
                    (operator.mul, torch.mul),
                    (operator.truediv, torch.div)):
            python_op, torch_op = ops

            for a, b in product(range(-10, 10), range(-10, 10)):
                for op in (lambda x: x * .5, lambda x: math.floor(x)):
                    a = op(a)
                    b = op(b)

                    # Skips zero divisors
                    if b == 0 or a == 0:
                        continue

                    a_tensor = torch.tensor(a, device=device)
                    b_tensor = torch.tensor(b, device=device)
                    a_tensor_cpu = a_tensor.cpu()
                    b_tensor_cpu = b_tensor.cpu()
                    vals = (a, b, a_tensor, b_tensor, a_tensor_cpu, b_tensor_cpu)

                    for args in product(vals, vals):
                        first, second = args

                        first_scalar = first if not isinstance(first, torch.Tensor) else first.item()
                        second_scalar = second if not isinstance(second, torch.Tensor) else second.item()
                        expected = python_op(first_scalar, second_scalar)

                        self.assertEqual(expected, python_op(first, second))
                        self.assertEqual(expected, torch_op(first, second))

    @onlyCUDA
    def test_ceil_out_mismatch(self, device):
        a = torch.randn(1)
        b = torch.randn(1, device=device)
        self.assertRaises(RuntimeError, lambda: torch.ceil(a, out=b))


    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_has_storage_numpy(self, device):
        for dtype in [np.float32, np.float64, np.int64,
                      np.int32, np.int16, np.uint8]:
            arr = np.array([1], dtype=dtype)
            self.assertIsNotNone(torch.tensor(arr, device=device, dtype=torch.float32).storage())
            self.assertIsNotNone(torch.tensor(arr, device=device, dtype=torch.double).storage())
            self.assertIsNotNone(torch.tensor(arr, device=device, dtype=torch.int).storage())
            self.assertIsNotNone(torch.tensor(arr, device=device, dtype=torch.long).storage())
            self.assertIsNotNone(torch.tensor(arr, device=device, dtype=torch.uint8).storage())

    def test_all_any_empty(self, device):
        x = torch.ByteTensor().to(device)
        self.assertTrue(x.all())
        self.assertFalse(x.any())

        x = torch.BoolTensor().to(device)
        self.assertTrue(x.all())
        self.assertFalse(x.any())

    @onlyCUDA
    def test_multinomial_device_constrain(self, device):
        x = torch.empty(0, device="cpu")
        y = torch.empty(0, device=device)
        self.assertRaisesRegex(
            RuntimeError, "multinomial arguments must have the same device",
            lambda: torch.multinomial(x, 2, out=y))

    @deviceCountAtLeast(2)
    @onlyCUDA
    def test_multinomial_gpu_device_constrain(self, devices):
        x = torch.empty(0, device=devices[0])
        y = torch.empty(0, device=devices[1])
        self.assertRaisesRegex(
            RuntimeError, "multinomial arguments must have the same device",
            lambda: torch.multinomial(x, 2, out=y))

    @deviceCountAtLeast(2)
    @onlyCUDA
    def test_device_guard(self, devices):
        # verify that all operators with `device_guard: False` behave properly with multiple devices.
        # TODO: if we had operator introspection we could figure out this set of operators automatically...
        x = torch.randn((1, 2, 3), device=devices[1])
        y = torch.zeros((1, 3, 2), device=devices[1])
        scalar = torch.tensor(5, device=devices[1])

        # property ops
        torch.cudnn_is_acceptable(x)
        x.is_distributed()
        x.is_floating_point()
        x.is_complex()
        x.is_same_size(y)
        x.is_signed()
        x.size(0)
        x.stride(0)
        x.numel()
        x.is_set_to(y)
        x.data_ptr()
        scalar.is_nonzero()

        # sparse property ops
        y[0][1] = 5
        y_sparse = y.to_sparse()
        y_sparse.sparse_dim()
        y_sparse._dimI()
        y_sparse.dense_dim()
        y_sparse._dimV()
        y_sparse._nnz()
        y_sparse.is_coalesced()
        y_sparse._indices()
        y_sparse._values()
        y_sparse.indices()
        y_sparse.values()

        # in-place ops
        def inplace():
            return torch.randn((1, 2, 3), device=devices[1])
        inplace().as_strided_(y.size(), y.stride())
        inplace().resize_(y.size())
        inplace().squeeze_()
        inplace().squeeze_(0)
        inplace().unsqueeze_(2)
        inplace().transpose_(1, 2)
        inplace().squeeze_().t_()
        inplace().set_(x.storage())
        inplace().set_(x.storage(), x.storage_offset(), x.size(), x.stride())
        inplace().set_(x)
        inplace().set_()
        y_sparse._coalesced_(True)

        # shape modification
        x.as_strided(y.size(), y.stride())
        x.expand((5, 2, 3))
        x.expand_as(x)
        x.sum_to_size((1,))
        torch.broadcast_tensors(x , x)
        x.reshape((1, 3, 2))
        x.reshape_as(y)
        x.squeeze()
        x.squeeze(0)
        x.squeeze().t()
        x.transpose(1, 2)
        x.unsqueeze(2)
        x.view((1, 3, 2))
        x.view_as(y)

        # chunk, split, etc.
        x.chunk(2, dim=1)
        x.split(1, dim=2)
        x.split_with_sizes([1, 2], dim=2)
        x.unfold(dimension=2, size=1, step=1)

        x.narrow(1, 1, 1)
        x.select(1, 1)
        torch.isnan(x)

        torch.empty((1, 3, 2), out=y)
        torch.empty_like(x)
        torch.empty_like(x, dtype=torch.int64)

        # to
        x.to(x)
        x.to(y)
        x.to(x, copy=True)

    @onlyCPU
    def test_renorm_ps(self, device):
        # full reduction
        x = torch.randn(5, 5)
        xn = x.numpy()
        for p in [1, 2, 3, 4, inf]:
            res = x.renorm(p, 1, 1)
            expected = x / x.norm(p, 0, keepdim=True).clamp(min=1)
            self.assertEqual(res, expected, msg="renorm failed for {}-norm".format(p))

    @onlyCUDA
    def test_topk_noncontiguous_gpu(self, device):
        t = torch.randn(20, device=device)[::2]
        top1, idx1 = t.topk(5)
        top2, idx2 = t.contiguous().topk(5)
        self.assertEqual(top1, top2)
        self.assertEqual(idx1, idx2)

    @dtypes(torch.int8, torch.uint8, torch.int16, torch.int32, torch.int64)
    def test_topk_integral(self, device, dtype):
        a = torch.randint(torch.iinfo(dtype).min, torch.iinfo(dtype).max, size=(10,),
                          dtype=dtype, device=device)
        sort_topk = a.sort()[0][-5:].flip(0)
        topk = a.topk(5)
        self.assertEqual(sort_topk, topk[0])      # check values
        self.assertEqual(sort_topk, a[topk[1]])   # check indices

    @dtypesIfCUDA(*([torch.half, torch.float, torch.double]
                    + ([torch.bfloat16] if TEST_WITH_ROCM else [])))
    @dtypes(torch.float, torch.double)
    def test_topk_nonfinite(self, device, dtype):
        x = torch.tensor([float('nan'), float('inf'), 1e4, 0, -1e4, -float('inf')], device=device, dtype=dtype)
        val, idx = x.topk(4)
        expect = torch.tensor([float('nan'), float('inf'), 1e4, 0], device=device, dtype=dtype)
        self.assertEqual(val, expect)
        self.assertEqual(idx, [0, 1, 2, 3])

        val, idx = x.topk(4, largest=False)
        expect = torch.tensor([-float('inf'), -1e4, 0, 1e4], device=device, dtype=dtype)
        self.assertEqual(val, expect)
        self.assertEqual(idx, [5, 4, 3, 2])

    def test_topk_4d(self, device):
        x = torch.ones(2, 3072, 2, 2, device=device)
        x[:, 1, :, :] *= 2.
        x[:, 10, :, :] *= 1.5
        val, ind = torch.topk(x, k=2, dim=1)
        expected_ind = torch.ones(2, 2, 2, 2, dtype=torch.long, device=device)
        expected_ind[:, 1, :, :] = 10
        expected_val = torch.ones(2, 2, 2, 2, device=device)
        expected_val[:, 0, :, :] *= 2.
        expected_val[:, 1, :, :] *= 1.5
        self.assertEqual(val, expected_val, atol=0, rtol=0)
        self.assertEqual(ind, expected_ind, atol=0, rtol=0)

    def test_is_signed(self, device):
        self.assertEqual(torch.IntTensor(5).to(device).is_signed(), True)
        self.assertEqual(torch.ByteTensor(5).to(device).is_signed(), False)
        self.assertEqual(torch.CharTensor(5).to(device).is_signed(), True)
        self.assertEqual(torch.FloatTensor(5).to(device).is_signed(), True)
        self.assertEqual(torch.HalfTensor(10).to(device).is_signed(), True)

    # Note - reports a leak of 512 bytes on CUDA device 1
    @deviceCountAtLeast(2)
    @skipCUDAMemoryLeakCheckIf(True)
    @onlyCUDA
    def test_tensor_set_errors_multigpu(self, devices):
        f_cuda0 = torch.randn((2, 3), dtype=torch.float32, device=devices[0])
        f_cuda1 = torch.randn((2, 3), dtype=torch.float32, device=devices[1])

        self.assertRaises(RuntimeError, lambda: f_cuda0.set_(f_cuda1.storage()))
        self.assertRaises(RuntimeError,
                          lambda: f_cuda0.set_(f_cuda1.storage(), 0, f_cuda1.size(), f_cuda1.stride()))
        self.assertRaises(RuntimeError, lambda: f_cuda0.set_(f_cuda1))

    @onlyCUDA
    def test_half_tensor(self, device):
        x = torch.randn(5, 5).half()
        self.assertEqual(x.to(device), x)

        xc = x.to(device)
        with tempfile.NamedTemporaryFile() as f:
            torch.save(xc, f)
            f.seek(0)
            xc2 = torch.load(f)
            self.assertIsInstance(xc2, type(xc))
            self.assertEqual(xc.float(), xc2.float())

    @onlyCUDA
    @deviceCountAtLeast(1)  # Note: Tests works with one but prefers more devices
    def test_serialization(self, devices):
        def _test_serialization(filecontext_lambda):
            t0 = torch.cuda.FloatTensor(5).fill_(1)
            with torch.cuda.device(devices[-1]):
                tn = torch.cuda.FloatTensor(3).fill_(2)
            torch.cuda.set_device(devices[0])
            b = (t0, tn)
            with filecontext_lambda() as f:
                torch.save(b, f)
                f.seek(0)
                c = torch.load(f)
                self.assertEqual(b, c, atol=0, rtol=0)
                u0, un = c
                self.assertEqual(str(u0.device), devices[0])
                self.assertEqual(str(un.device), devices[-1])

        _test_serialization(tempfile.NamedTemporaryFile)
        _test_serialization(BytesIOContext)

    def test_memory_format_preserved_after_permute(self, device):
        x = torch.randn(4, 3, 8, 8, device=device)
        nhwc = x.contiguous(memory_format=torch.channels_last)
        y = nhwc.permute(0, 1, 3, 2).permute(0, 1, 3, 2)
        self.assertTrue(y.is_contiguous(memory_format=torch.channels_last))

        x = torch.randn(4, 3, 8, 8, 8, device=device)
        ndhwc = x.contiguous(memory_format=torch.channels_last_3d)
        y = ndhwc.permute(0, 1, 4, 3, 2).permute(0, 1, 4, 3, 2)
        self.assertTrue(y.is_contiguous(memory_format=torch.channels_last_3d))

    def test_resize_as_preserves_strides(self, device):
        x = torch.empty(2, 3).t()
        old_strides = x.stride()
        x.resize_as_(x)
        self.assertEqual(x.stride(), old_strides)

    def test_memory_format_resize_as(self, device):
        def test_helper(shape, memory_format, device):
            xc = torch.randn(shape, device=device).contiguous(memory_format=memory_format)
            flat = torch.randn(xc.numel(), device=device)
            flat.resize_as_(xc, memory_format=torch.preserve_format)
            self.assertTrue(flat.is_contiguous(memory_format=memory_format))

        test_helper((10, 3, 32, 32), torch.channels_last, device)
        test_helper((3, 10, 3, 32, 32), torch.channels_last_3d, device)

    def test_memory_format_resize_(self, device):
        def test_helper(shape, numel, memory_format, device):
            flat = torch.randn(numel, device=device)
            flat.resize_(shape, memory_format=memory_format)
            self.assertTrue(flat.is_contiguous(memory_format=memory_format))

        test_helper((10, 3, 32, 32), 10 * 3 * 32 * 32, torch.channels_last, device)
        test_helper((3, 10, 3, 32, 32), 3 * 10 * 3 * 32 * 32, torch.channels_last_3d, device)

    def test_memory_format_propagation_rules(self, device):

        contiguous = torch.rand(10, 3, 5, 5, device=device)
        cl = torch.rand(10, 3, 5, 5, device=device).contiguous(memory_format=torch.channels_last)
        ambiguous = torch.rand(10, 3, 1, 1, device=device).contiguous(memory_format=torch.channels_last)
        self.assertTrue(ambiguous.is_contiguous(memory_format=torch.channels_last))
        self.assertTrue(ambiguous.is_contiguous(memory_format=torch.contiguous_format))
        bias = torch.rand(1, 1, 1, 1, device=device).contiguous(memory_format=torch.channels_last)

        def _test_propagation_rules(self, contiguous, cl, ambiguous, bias):
            options = ((ambiguous, contiguous, torch.contiguous_format),
                       (ambiguous, cl, torch.channels_last),
                       (contiguous, ambiguous, torch.contiguous_format),
                       (contiguous, cl, torch.contiguous_format),
                       (cl, ambiguous, torch.channels_last),
                       (cl, contiguous, torch.channels_last),
                       (bias, cl, torch.channels_last),
                       (cl, bias, torch.channels_last),)

            for a, b, mf in options:
                result = a + b
                self.assertTrue(result.is_contiguous(memory_format=mf))

        _test_propagation_rules(self, contiguous, cl, ambiguous, bias)

        cl = cl.to(memory_format=torch.channels_last)
        ambiguous = ambiguous.to(memory_format=torch.channels_last)
        bias = bias.to(memory_format=torch.channels_last)

        _test_propagation_rules(self, contiguous, cl, ambiguous, bias)

        # test cases when strides matter in ambiguous tensors
        for mf in (torch.channels_last, torch.contiguous_format):
            ambiguous = torch.rand(10, 3, 1, 1, device=device).to(memory_format=mf)
            bias = torch.rand(3, 1, 1, device=device)
            result = ambiguous + bias
            self.assertEqual(ambiguous.stride(), result.stride())
            result = bias + ambiguous
            self.assertEqual(ambiguous.stride(), result.stride())
            result = ambiguous * 5
            self.assertEqual(ambiguous.stride(), result.stride())

    def test_memory_format_empty_like(self, device):
        def test_helper(x, memory_format):
            xc = x.contiguous(memory_format=memory_format)

            like = torch.empty_like(xc, memory_format=torch.preserve_format)
            self.assertFalse(like.is_contiguous())
            self.assertTrue(like.is_contiguous(memory_format=memory_format))

            like_x = torch.empty_like(x, memory_format=torch.preserve_format)
            self.assertTrue(like_x.is_contiguous())
            self.assertFalse(like_x.is_contiguous(memory_format=memory_format))

            like = torch.empty_like(x, memory_format=memory_format)
            self.assertFalse(like.is_contiguous())
            self.assertTrue(like.is_contiguous(memory_format=memory_format))

            like = torch.empty_like(xc, memory_format=torch.contiguous_format)
            self.assertTrue(like.is_contiguous())
            self.assertFalse(like.is_contiguous(memory_format=memory_format))

            like = torch.empty_like(xc)
            self.assertFalse(like.is_contiguous())
            self.assertTrue(like.is_contiguous(memory_format=memory_format))

            sparse = x.to_sparse()
            with self.assertRaises(RuntimeError):
                z = torch.empty_like(sparse, memory_format=torch.preserve_format)

        test_helper(torch.randn(4, 3, 8, 8, device=device), torch.channels_last)
        test_helper(torch.randn(4, 3, 8, 8, 8, device=device), torch.channels_last_3d)

    def test_memory_format_consistency(self, device):
        x = torch.randn(10, 3, 1, 1, device=device)
        x_rep = x.as_strided(x.size(), x.stride())
        self.assertEqual(x.size(), x_rep.size())
        self.assertEqual(x.stride(), x_rep.stride())
        self.assertEqual(x.is_contiguous(), x_rep.is_contiguous())
        self.assertEqual(x.is_contiguous(memory_format=torch.channels_last), x_rep.is_contiguous(memory_format=torch.channels_last))
        self.assertEqual(
            x.is_contiguous(memory_format=torch.channels_last_3d), x_rep.is_contiguous(memory_format=torch.channels_last_3d))

    def test_memory_format_operators(self, device):
        def _chunk_op(x, y):
            x1, x2 = x.chunk(2, dim=1)
            return x1 + x2

        def _unsqueeze_op_add(x, y):
            return x[0].unsqueeze(0) + 3

        def _unsqueeze_op_clone(x, y):
            return x[0].unsqueeze(0).clone()

        def _test_helper(x, y, bias, memory_format):
            return_contig_fns = [
                lambda x, y: y + x,
                lambda x, y: y * x,
                lambda x, y: y.addcdiv(x, y, value=2),
                lambda x, y: y.addcmul(x, y, value=2),
            ]
            bias_fns = [
                lambda x, b: x + b,
                lambda x, b: b + x,
            ]
            fns = [
                lambda x, y: x.clone(),
                lambda x, y: x + 3,
                lambda x, y: 3 * x,
                lambda x, y: x + y,
                lambda x, y: x * y,
                lambda x, y: abs(x),
                lambda x, y: x.abs(),
                lambda x, y: x.abs_(),
                lambda x, y: x.acos(),
                lambda x, y: x.acos_(),
                lambda x, y: x.add(y, alpha=3),
                lambda x, y: x.add_(y, alpha=3),
                lambda x, y: x.addcdiv(y, y, value=2),
                lambda x, y: x.addcdiv_(y, y, value=2),
                lambda x, y: x.addcmul(y, y, value=2),
                lambda x, y: x.addcmul_(y, y, value=2),
                lambda x, y: x.acosh(),
                lambda x, y: x.acosh_(),
                lambda x, y: x.asinh(),
                lambda x, y: x.asinh_(),
                lambda x, y: x.atanh(),
                lambda x, y: x.atanh_(),
                lambda x, y: x.asin(),
                lambda x, y: x.asin_(),
                lambda x, y: x.atan(),
                lambda x, y: x.atan2(y),
                lambda x, y: x.atan2_(y),
                lambda x, y: x.ceil(),
                lambda x, y: x.ceil_(),
                lambda x, y: x.clamp(-1, 1),
                lambda x, y: x.cos(),
                lambda x, y: x.cosh(),
                lambda x, y: x.div(0.5),
                lambda x, y: x.div_(0.5),
                lambda x, y: x.div(y),
                lambda x, y: x.div_(y),
                lambda x, y: x.digamma(),
                lambda x, y: x.digamma_(),
                lambda x, y: x.erf(),
                lambda x, y: x.erfc(),
                lambda x, y: x.erfinv(),
                lambda x, y: x.erfinv_(),
                lambda x, y: x.exp(),
                lambda x, y: x.expm1(),
                lambda x, y: x.expm1_(),
                lambda x, y: x.floor(),
                lambda x, y: x.floor_(),
                # lambda x, y: x.fmod(2), # https://github.com/pytorch/pytorch/issues/24565
                lambda x, y: x.frac(),
                lambda x, y: x.hypot(y),
                lambda x, y: x.hypot_(y),
                lambda x, y: x.i0(),
                lambda x, y: x.i0_(),
                lambda x, y: x.lerp(y, 0.5),
                lambda x, y: x.log(),
                lambda x, y: x.log_(),
                lambda x, y: x.log10(),
                lambda x, y: x.log10_(),
                lambda x, y: x.log1p(),
                lambda x, y: x.log1p_(),
                lambda x, y: x.log2(),
                lambda x, y: x.log2_(),
                lambda x, y: x.mul(3),
                lambda x, y: x.mul_(3),
                lambda x, y: x.neg(),
                lambda x, y: x.neg_(),
                lambda x, y: x.pow(3),
                lambda x, y: x.pow_(3),
                lambda x, y: x.pow(0.0),
                lambda x, y: x.pow(1.0),
                lambda x, y: x.reciprocal(),
                lambda x, y: x.remainder(2),
                lambda x, y: x.round(),
                lambda x, y: x.round_(),
                lambda x, y: x.rsqrt(),
                lambda x, y: x.rsqrt_(),
                lambda x, y: x.sigmoid(),
                lambda x, y: x.sigmoid_(),
                lambda x, y: x.logit(),
                lambda x, y: x.logit_(),
                lambda x, y: x.logit(1e-6),
                lambda x, y: x.logit_(1e-6),
                lambda x, y: x.sign(),
                lambda x, y: x.sign_(),
                lambda x, y: x.sgn(),
                lambda x, y: x.sgn_(),
                lambda x, y: x.sin(),
                lambda x, y: x.sin_(),
                lambda x, y: x.sinh(),
                lambda x, y: x.sinh_(),
                lambda x, y: x.sqrt(),
                lambda x, y: x.sqrt_(),
                lambda x, y: x.tan(),
                lambda x, y: x.tanh(),
                lambda x, y: x.trunc(),
                lambda x, y: x.trunc_(),
                _chunk_op,
                _unsqueeze_op_add,
                _unsqueeze_op_clone,
            ]
            for fn in fns:
                x_c = x.contiguous()
                y_c = y.contiguous()
                result_c = fn(x_c, y_c)
                result = fn(x, y)
                self.assertEqual(result, result_c)
                self.assertTrue(
                    result.is_contiguous(memory_format=memory_format),
                    "result of the '{}' is not in '{}' format".format(inspect.getsource(fn).strip(), memory_format))

            for fn in bias_fns:
                x_c = x.contiguous()
                b_c = bias.contiguous()
                result_c = fn(x_c, b_c)
                result = fn(x, bias)
                self.assertEqual(result, result_c)
                self.assertTrue(
                    result.is_contiguous(memory_format=memory_format),
                    "result of the '{}' is not in '{}' format".format(inspect.getsource(fn).strip(), memory_format))

            for fn in return_contig_fns:
                x_c = x.contiguous()
                y_c = y.contiguous()
                result_c = fn(x_c, y_c)
                result = fn(x, y)
                self.assertEqual(result, result_c)
                self.assertTrue(
                    result.is_contiguous(memory_format=torch.contiguous_format),
                    "result of the '{}' is not in '{}' format".format(inspect.getsource(fn).strip(), torch.contiguous_format))

        _test_helper(
            torch.randn((4, 3, 8, 8), device=device).contiguous(memory_format=torch.channels_last),
            abs(torch.randn((4, 3, 8, 8), device=device)) + 1,
            torch.randn((1, 3, 1, 1), device=device).contiguous(memory_format=torch.channels_last),
            torch.channels_last)
        _test_helper(
            torch.randn((4, 3, 8, 8, 8), device=device).contiguous(memory_format=torch.channels_last_3d),
            abs(torch.randn((4, 3, 8, 8, 8), device=device)) + 1,
            torch.randn((1, 3, 1, 1, 1), device=device).contiguous(memory_format=torch.channels_last_3d),
            torch.channels_last_3d)

    def test_strides_propagation(self, device):

        def _test_helper(x, op, unary=False):
            def compare_strides(s1, s2, div):
                sdiv = [s // div for s in s1]
                self.assertEqual(sdiv, s2)

            dim = x.dim()
            # we produce memory dense outputs, so when input is strided on the last dimension
            # we need to divide by that dimension stride to compare input and result strides
            div = x.stride(-1)
            for p in permutations(range(dim)):
                xp = x.permute(p)
                if not unary:
                    y = torch.randn(xp.size(-1), device=x.device, dtype=x.dtype)
                    for inputs in ((xp, xp), (xp, y), (y, xp)):
                        res = op(*inputs)
                        compare_strides(xp.stride(), res.stride(), div)
                        self.assertEqual(xp.size(), res.size())
                        out = torch.empty(0, device=xp.device, dtype=res.dtype)
                        res = op(*inputs, out=out)
                        compare_strides(xp.stride(), res.stride(), div)
                        self.assertEqual(xp.size(), res.size())
                else:
                    res = op(xp)
                    compare_strides(xp.stride(), res.stride(), div)
                    self.assertEqual(xp.size(), res.size())
                    out = torch.empty(0, device=xp.device, dtype=res.dtype)
                    res = op(xp, out=out)
                    compare_strides(xp.stride(), res.stride(), div)
                    self.assertEqual(xp.size(), res.size())

        # torch.eq by default calls TensorIterator with defined output, torch.add with undefined
        binary_ops = (torch.eq, torch.add)
        unary_ops = (torch.exp,)
        # memory dense, sliced and ambiguous sliced (ambiguous dense loses permutation information)
        xs = (torch.randn(2, 3, 4, device=device), torch.randn(2, 3, 8, device=device)[:, :, ::2],
              torch.randn(1, 1, 4, 12, device=device)[:, :, :, ::2])
        for op in binary_ops:
            for x in xs:
                _test_helper(x, op)
        for op in unary_ops:
            for x in xs:
                _test_helper(x, op, unary=True)


    def _test_unique_scalar_empty(self, dtype, device, f):
        # test scalar
        x = torch.tensor(0, dtype=dtype, device=device)
        unique, inverse, counts = f(x, return_inverse=True, return_counts=True)
        expected_unique = torch.tensor([0], dtype=dtype, device=device)
        expected_inverse = torch.tensor(0, device=device)
        expected_counts = torch.tensor([1], device=device)
        self.assertEqual(unique, expected_unique)
        self.assertEqual(inverse, expected_inverse)
        self.assertEqual(counts, expected_counts)

        # test zero sized tensor
        x = torch.zeros((0, 0, 3), dtype=dtype, device=device)
        unique, inverse, counts = f(x, return_inverse=True, return_counts=True)
        expected_unique = torch.tensor([], dtype=dtype, device=device)
        expected_inverse = torch.empty((0, 0, 3), dtype=torch.long, device=device)
        expected_counts = torch.tensor([], dtype=torch.long, device=device)
        self.assertEqual(unique, expected_unique)
        self.assertEqual(inverse, expected_inverse)
        self.assertEqual(counts, expected_counts)

    def _test_unique_with_expects(self, device, dtype, f, x, expected_unique, expected_inverse, expected_counts, additional_shape):
        def ensure_tuple(x):
            if isinstance(x, torch.Tensor):
                return (x,)
            return x

        for return_inverse in [True, False]:
            for return_counts in [True, False]:
                # test with expected
                ret = ensure_tuple(f(x, return_inverse=return_inverse, return_counts=return_counts))
                self.assertEqual(len(ret), 1 + int(return_inverse) + int(return_counts))
                self.assertEqual(expected_unique, ret[0])
                if return_inverse:
                    self.assertEqual(expected_inverse, ret[1])
                if return_counts:
                    count_index = 1 + int(return_inverse)
                    self.assertEqual(expected_counts, ret[count_index])

                # tests per-element unique on a higher rank tensor.
                y = x.view(additional_shape)
                y_unique, y_inverse, y_counts = f(y, return_inverse=True, return_counts=True)
                self.assertEqual(expected_unique, y_unique)
                self.assertEqual(expected_inverse.view(additional_shape), y_inverse)
                self.assertEqual(expected_counts, y_counts)

    @dtypes(*set(torch.testing.get_all_dtypes()) - {torch.bfloat16, torch.complex64, torch.complex128})
    def test_unique(self, device, dtype):
        if dtype is torch.half and self.device_type == 'cpu':
            return  # CPU does not have half support

        def ensure_tuple(x):
            if isinstance(x, torch.Tensor):
                return (x,)
            return x

        if dtype is torch.bool:
            x = torch.tensor([True, False, False, False, True, False, True, False], dtype=torch.bool, device=device)
            expected_unique = torch.tensor([False, True], dtype=torch.bool, device=device)
            expected_inverse = torch.tensor([1, 0, 0, 0, 1, 0, 1, 0], dtype=torch.long, device=device)
            expected_counts = torch.tensor([5, 3], dtype=torch.long, device=device)
        else:
            x = torch.tensor([1, 2, 3, 2, 8, 5, 2, 3], dtype=dtype, device=device)
            expected_unique = torch.tensor([1, 2, 3, 5, 8], dtype=dtype, device=device)
            expected_inverse = torch.tensor([0, 1, 2, 1, 4, 3, 1, 2], device=device)
            expected_counts = torch.tensor([1, 3, 2, 1, 1], device=device)

        # test sorted unique
        fs = [
            lambda x, **kwargs: torch.unique(x, sorted=True, **kwargs),
            lambda x, **kwargs: x.unique(sorted=True, **kwargs),
        ]
        for f in fs:
            self._test_unique_with_expects(device, dtype, f, x, expected_unique, expected_inverse, expected_counts, (2, 2, 2))
            self._test_unique_scalar_empty(dtype, device, f)

        # test unsorted unique
        fs = [
            lambda x, **kwargs: torch.unique(x, sorted=False, **kwargs),
            lambda x, **kwargs: x.unique(sorted=False, **kwargs)
        ]
        for f in fs:
            self._test_unique_scalar_empty(dtype, device, f)
            for return_inverse in [True, False]:
                for return_counts in [True, False]:
                    ret = ensure_tuple(f(x, return_inverse=return_inverse, return_counts=return_counts))
                    self.assertEqual(len(ret), 1 + int(return_inverse) + int(return_counts))
                    x_list = x.tolist()
                    x_unique_list = ret[0].tolist()
                    self.assertEqual(expected_unique.tolist(), sorted(x_unique_list))
                    if return_inverse:
                        x_inverse_list = ret[1].tolist()
                        for i, j in enumerate(x_inverse_list):
                            self.assertEqual(x_list[i], x_unique_list[j])
                    if return_counts:
                        count_index = 1 + int(return_inverse)
                        x_counts_list = ret[count_index].tolist()
                        for i, j in zip(x_unique_list, x_counts_list):
                            count = 0
                            for k in x_list:
                                if k == i:
                                    count += 1
                            self.assertEqual(j, count)

    @dtypes(*set(torch.testing.get_all_dtypes()) - {torch.bfloat16, torch.complex64, torch.complex128})
    def test_unique_consecutive(self, device, dtype):
        if dtype is torch.half and self.device_type == 'cpu':
            return  # CPU does not have half support

        if dtype is torch.bool:
            x = torch.tensor([True, False, False, False, True, True, False, False, False], dtype=torch.bool, device=device)
            expected_unique = torch.tensor([True, False, True, False], dtype=torch.bool, device=device)
            expected_inverse = torch.tensor([0, 1, 1, 1, 2, 2, 3, 3, 3], dtype=torch.long, device=device)
            expected_counts = torch.tensor([1, 3, 2, 3], dtype=torch.long, device=device)
        else:
            x = torch.tensor([1, 2, 2, 2, 5, 5, 2, 2, 3], dtype=dtype, device=device)
            expected_unique = torch.tensor([1, 2, 5, 2, 3], dtype=dtype, device=device)
            expected_inverse = torch.tensor([0, 1, 1, 1, 2, 2, 3, 3, 4], device=device)
            expected_counts = torch.tensor([1, 3, 2, 2, 1], device=device)

        for f in [torch.unique_consecutive, lambda x, **kwargs: x.unique_consecutive(**kwargs)]:
            self._test_unique_with_expects(device, dtype, f, x, expected_unique, expected_inverse, expected_counts, (3, 3))
            self._test_unique_scalar_empty(dtype, device, f)

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_erfinv(self, device, dtype):
        # general testing. Narrow the range to avoid accuracy issues
        input_values = torch.randn(4, 4, dtype=dtype, device=device).clamp(-0.3, 0.3)
        self.assertEqual(input_values.erf().erfinv(), input_values)
        # test inf
        self.assertTrue(torch.equal(torch.tensor([-1, 1], dtype=dtype, device=device).erfinv(),
                                    torch.tensor([-inf, inf], dtype=dtype, device=device)))
        # test nan
        self.assertEqual(torch.tensor([-2, 2], dtype=dtype, device=device).erfinv(),
                         torch.tensor([nan, nan], dtype=dtype, device=device))

        if dtype == torch.double:
            # double precision
            a = torch.tensor([0.5, 0.8], dtype=torch.double, device=device).erfinv()
            self.assertEqual(a[0].item(), 0.47693627620447, atol=1e-13, rtol=0)
            self.assertEqual(a[1].item(), 0.90619380243682, atol=1e-13, rtol=0)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_ctor_with_numpy_array(self, device):
        correct_dtypes = [
            np.double,
            np.float,
            np.float16,
            np.int64,
            np.int32,
            np.int16,
            np.int8,
            np.uint8,
            np.bool,
        ]

        incorrect_byteorder = '>' if sys.byteorder == 'little' else '<'
        incorrect_dtypes = map(lambda t: incorrect_byteorder + t, ['d', 'f'])

        for dtype in correct_dtypes:
            array = np.array([1, 2, 3, 4], dtype=dtype)

            # Upcast
            tensor = torch.DoubleTensor(array).to(device)
            for i in range(len(array)):
                self.assertEqual(tensor[i], array[i])

            # Downcast (sometimes)
            tensor = torch.FloatTensor(array).to(device)
            for i in range(len(array)):
                self.assertEqual(tensor[i], array[i])

            tensor = torch.HalfTensor(array).to(device)
            for i in range(len(array)):
                self.assertEqual(tensor[i], array[i])

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*torch.testing.get_all_dtypes())
    def test_numpy_scalar_cmp(self, device, dtype):
        if dtype.is_complex:
            tensors = (torch.tensor(complex(1, 3), dtype=dtype, device=device),
                       torch.tensor([complex(1, 3), 0, 2j], dtype=dtype, device=device),
                       torch.tensor([[complex(3, 1), 0], [-1j, 5]], dtype=dtype, device=device))
        else:
            tensors = (torch.tensor(3, dtype=dtype, device=device),
                       torch.tensor([1, 0, -3], dtype=dtype, device=device),
                       torch.tensor([[3, 0, -1], [3, 5, 4]], dtype=dtype, device=device))

        for tensor in tensors:
            if dtype == torch.bfloat16:
                with self.assertRaises(TypeError):
                    np_array = tensor.cpu().numpy()
                continue

            np_array = tensor.cpu().numpy()
            for t, a in product((tensor.flatten()[0], tensor.flatten()[0].item()),
                                (np_array.flatten()[0], np_array.flatten()[0].item())):
                self.assertEqual(t, a)
                if dtype == torch.complex64 and torch.is_tensor(t) and type(a) == np.complex64:
                    # TODO: Imaginary part is dropped in this case. Need fix.
                    # https://github.com/pytorch/pytorch/issues/43579
                    self.assertFalse(t == a)
                else:
                    self.assertTrue(t == a)

    def test_dlpack_conversion(self, device):
        x = torch.randn(1, 2, 3, 4, device=device, dtype=torch.float)
        z = from_dlpack(to_dlpack(x))
        self.assertEqual(z, x)

    @onlyCUDA
    @unittest.skipIf(PYTORCH_CUDA_MEMCHECK, "is_pinned uses failure to detect pointer property")
    def test_pin_memory_from_constructor(self, device):
        def _get_like(t, **kwargs):
            return [
                torch.rand_like(t, **kwargs),
                torch.randn_like(t, **kwargs),
                torch.empty_like(t, **kwargs),
                torch.full_like(t, 4, **kwargs),
                torch.zeros_like(t, **kwargs),
                torch.ones_like(t, **kwargs),
            ]

        def _get_tensors(**kwargs):
            return [
                torch.tensor([10, 11], **kwargs),
                torch.randn(3, 5, **kwargs),
                torch.rand(3, **kwargs),
                # torch.randint(3, 5, **kwargs), // unsupported
                torch.zeros(3, **kwargs),
                torch.randperm(3, **kwargs),
                torch.empty(6, **kwargs),
                torch.ones(6, **kwargs),
                torch.eye(6, **kwargs),
                torch.arange(3, 5, **kwargs)]

        pinned_tensors = _get_tensors(pin_memory=True) + _get_like(torch.empty(5, dtype=torch.float64), pin_memory=True)
        for x in pinned_tensors:
            self.assertTrue(x.is_pinned())

        tensors = _get_tensors() + _get_like(torch.empty(5, dtype=torch.float64, pin_memory=True))
        for x in tensors:
            self.assertFalse(x.is_pinned())

    def test_storage_device(self, device):
        x = torch.tensor([], device=device)
        self.assertEqual(x.dtype, x.storage().dtype)

    @deviceCountAtLeast(2)
    @onlyCUDA
    def test_storage_multigpu(self, devices):
        for device in devices:
            x = torch.tensor([], device=device)
            self.assertEqual(x.dtype, x.storage().dtype)

    @onlyCPU
    @skipCPUIfNoLapack
    def test_orgqr_errors(self, device):
        test_cases = [
            # input1 size, input2 size, error regex
            ((10,), (2,), r"'input' should be 2 dimensional"),
            ((10, 6), (20,), r"input.size\(1\) must be greater than or equal to input2.size\(0\)"),
            ((6, 10), (5,), r"input.size\(0\) must be greater than or equal to input.size\(1\)"),
            ((0, 0), (0,), r"'input' should not be empty")
        ]
        for a_size, tau_size, error_regex in test_cases:
            a = torch.rand(*a_size, device=device)
            tau = torch.rand(*tau_size, device=device)
            with self.assertRaisesRegex(RuntimeError, error_regex):
                torch.orgqr(a, tau)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    def test_lu(self, device):
        from torch.testing._internal.common_utils import random_matrix

        def run_test(device, pivot):
            def run_subtest(matrix_size, batches, device, pivot, singular=False, a=None):
                if isinstance(matrix_size, int):
                    rows = columns = matrix_size
                else:
                    rows, columns = matrix_size
                if a is None:
                    a = random_matrix(rows, columns, *batches, **dict(singular=singular)).to(device)
                a_LU_info, pivots_info, info_ = a.lu(pivot=pivot, get_infos=True)
                self.assertEqual(a_LU_info.size(), torch.Size(batches + (rows, columns)))
                self.assertEqual(pivots_info.size(), torch.Size(batches + (min(rows, columns),)))
                self.assertEqual(info_.size(), torch.Size(batches))
                # If a randomly generated input matrix is singular,
                # then info_ contains indices i such that U[i, i] ==
                # 0. This however conveys that the factorization was
                # successful albeit with a singular input. Therefore,
                # we require info.min() >= 0
                self.assertGreaterEqual(info_.min(), 0)
                a_LU, pivots = a.lu(pivot=pivot)
                self.assertEqual(a_LU, a_LU_info)
                self.assertEqual(pivots_info, pivots)

                P, L, U = torch.lu_unpack(a_LU, pivots)
                self.assertEqual(P.matmul(L.matmul(U)), a)

                if self.device_type == 'cuda':
                    # lu without pivoting is implemented only for cuda device
                    a_LU_info_nopiv, nopiv, info_nopiv = a.lu(pivot=False, get_infos=True)
                    P_nopiv, L_nopiv, U_nopiv = torch.lu_unpack(a_LU_info_nopiv, nopiv)
                    self.assertEqual(P_nopiv.matmul(L_nopiv.matmul(U_nopiv)), a)
                    k = min(rows, columns)
                    self.assertEqual(nopiv, torch.arange(1, 1 + k, device=device, dtype=torch.int32).expand(a.shape[:-2] + (k, )))
                    if not singular:
                        # It is not guaranteed that LU factorization
                        # without pivoting is able to determine if a
                        # matrix is singular while LU factorization
                        # with pivoting is. Therefore, we require the
                        # equality of info-s only for non-singular
                        # matrices.
                        self.assertEqual(info_, info_nopiv)

            for ms, batch in product([3, 5, 7, (4, 2), (3, 4)], [(), (2,), (3,), (3, 5)]):
                run_subtest(ms, batch, device, pivot)
                run_subtest(ms, batch, device, pivot, singular=True)

                # Reproducer of a magma bug, see https://bitbucket.org/icl/magma/issues/13/getrf_batched-kernel-produces-nans-on
                a = torch.ones(batch + (ms if isinstance(ms, tuple) else (ms, ms)), dtype=torch.double, device=device)
                run_subtest(ms, batch, device, pivot, singular=True, a=a)

            # Info should be positive for rank deficient matrices
            a = torch.ones(5, 3, 3, device=device)
            self.assertGreater(a.lu(pivot=pivot, get_infos=True)[2][0], 0)

        run_test(device, True)

        if self.device_type == 'cpu':
            # Error checking, no pivoting variant on CPU
            with self.assertRaisesRegex(RuntimeError, 'lu without pivoting is not implemented on the CPU'):
                torch.lu(torch.empty(1, 2, 2), pivot=False)
        else:
            run_test(device, False)

    @skipCPUIfNoLapack
    @skipCUDAIfNoMagma
    @dtypes(torch.double)
    def test_lu_unpack(self, device, dtype):
        def run_test(pivot):
            for shape in ((3, 3), (5, 3, 3), (7, 3, 5, 5), (7, 5, 3, 3, 3)):
                a = torch.randn(*shape, dtype=dtype, device=device)
                a_lu, p = torch.lu(a, pivot=pivot)
                p_ref, l_ref, u_ref = torch.lu_unpack(a_lu, p)
                self.assertEqual(p_ref.matmul(l_ref.matmul(u_ref)), a)

        run_test(True)

        if self.device_type == 'cuda':
            run_test(False)

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_max_with_inf(self, device, dtype):
        a = torch.tensor([[-inf, -inf, inf, 3], [inf, inf, -inf, -1]], dtype=dtype, device=device)
        self.assertTrue(torch.all(torch.max(a, dim=1).values == inf).item())
        self.assertTrue(torch.all(torch.amax(a, dim=1) == inf).item())
        self.assertTrue(torch.max(a).item() == inf)
        self.assertTrue(torch.amax(a).item() == inf)

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_min_with_inf(self, device, dtype):
        a = torch.tensor([[-inf, -inf, inf, 3], [inf, inf, -inf, -1]], dtype=dtype, device=device)
        self.assertTrue(torch.all(torch.min(a, dim=1).values == (-inf)).item())
        self.assertTrue(torch.all(torch.amin(a, dim=1) == (-inf)).item())
        self.assertTrue(torch.min(a).item() == -inf)
        self.assertTrue(torch.amin(a).item() == -inf)

    def _test_minmax_helper(self, torchfn, reffn, device, dtype, skip_indices=False):
        def create_input(shape, device, dtype):
            if dtype.is_floating_point:
                return torch.randn(*shape, device=device, dtype=dtype)
            else:
                low = 0 if dtype == torch.bool else -1000
                high = 2 if dtype == torch.bool else 1000
                return torch.randint(low, high, shape, device=device, dtype=dtype)
        x = create_input((100, 100), device, dtype)
        self.compare_with_numpy(torchfn, reffn, x)
        # non contiguous
        x = create_input((10, 10, 10), device, dtype)
        x = x[:, 4]
        self.compare_with_numpy(torchfn, reffn, x)

        def get_values(x):
            if istuple(x):
                return x[0]
            return x

        # indices
        if not skip_indices:
            size = 5
            x = create_input((size, size), device, dtype)
            inputs = (x, x.t())
            dims = (0, 1)
            for xinp, d in product(inputs, dims):
                self.compare_with_numpy(lambda x: get_values(torchfn(x, d, False)), lambda x: reffn(x, d, keepdims=False), xinp)
                result = torchfn(xinp, d, False)
                if istuple(result):
                    v, i = result
                    if d == 1:
                        self.assertEqual(xinp[torch.arange(size), i], v, atol=0, rtol=0)
                    else:
                        self.assertEqual(xinp[i, torch.arange(size)], v, atol=0, rtol=0)
        # nan
        if dtype.is_floating_point:
            for index in (0, 4, 99):
                x = create_input((100,), device, dtype)
                x[index] = nan
                if not skip_indices:
                    result = torchfn(x, 0)
                    v = get_values(result)
                    self.assertEqual(v, nan)
                    if istuple(result):
                        i = result[1]
                        self.assertEqual(i, index)
                self.assertEqual(torchfn(x), nan)

    @dtypesIfCPU(torch.float, torch.double, torch.long, torch.bool)
    @dtypesIfCUDA(torch.half, torch.float, torch.long, torch.bool)
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_max(self, device, dtype):
        self._test_minmax_helper(torch.max, np.amax, device, dtype)

    @dtypesIfCPU(torch.float, torch.double, torch.long, torch.bool)
    @dtypesIfCUDA(torch.half, torch.float, torch.long, torch.bool)
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_min(self, device, dtype):
        self._test_minmax_helper(torch.min, np.amin, device, dtype)

    @dtypesIfCPU(torch.float, torch.double, torch.int, torch.long, torch.bool)
    @dtypesIfCUDA(torch.half, torch.float, torch.int, torch.long, torch.bool)
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_amin(self, device, dtype):
        self._test_minmax_helper(torch.amin, np.amin, device, dtype)

    @dtypesIfCPU(torch.float, torch.double, torch.int, torch.long, torch.bool)
    @dtypesIfCUDA(torch.half, torch.float, torch.int, torch.long, torch.bool)
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_amax(self, device, dtype):
        self._test_minmax_helper(torch.amax, np.amax, device, dtype)

    @onlyOnCPUAndCUDA
    @dtypesIfCPU(torch.float, torch.double)
    @dtypesIfCUDA(torch.half, torch.float)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_aminmax(self, device, dtype):

        def _amin_wrapper(x, dim=None, keepdims=False):
            if dim is None:
                return torch._aminmax(x)[0]
            else:
                return torch._aminmax(x, dim, keepdims)[0]

        def _amax_wrapper(x, dim=None, keepdims=False):
            if dim is None:
                return torch._aminmax(x)[1]
            else:
                return torch._aminmax(x, dim, keepdims)[1]

        self._test_minmax_helper(_amin_wrapper, np.amin, device, dtype)
        self._test_minmax_helper(_amax_wrapper, np.amax, device, dtype)

    @dtypes(*product(torch.testing.get_all_dtypes(include_complex=False), torch.testing.get_all_dtypes(include_complex=False)))
    def test_maximum_minimum_type_promotion(self, device, dtypes):
        a = torch.tensor((0, 1), device=device, dtype=dtypes[0])
        b = torch.tensor((1, 0), device=device, dtype=dtypes[1])
        for op in (torch.maximum, torch.max, torch.minimum, torch.min):
            result = op(a, b)
            self.assertEqual(result.dtype, torch.result_type(a, b))

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + [torch.bool]))
    def test_maximum_minimum_int_and_bool(self, device, dtype):
        ops = ((torch.maximum, torch.max, np.maximum), (torch.minimum, torch.min, np.minimum))
        rng = np.random.default_rng()
        a_np = np.array(rng.integers(-100, 100, size=10), dtype=torch_to_numpy_dtype_dict[dtype])
        b_np = np.array(rng.integers(-100, 100, size=10), dtype=torch_to_numpy_dtype_dict[dtype])

        for torch_op, alias, numpy_op in ops:
            a_tensor = torch.from_numpy(a_np).to(device=device, dtype=dtype)
            b_tensor = torch.from_numpy(b_np).to(device=device, dtype=dtype)
            tensor_result = torch_op(a_tensor, b_tensor)
            alias_result = alias(a_tensor, b_tensor)

            out = torch.empty_like(a_tensor)
            torch_op(a_tensor, b_tensor, out=out)

            numpy_result = numpy_op(a_np, b_np)

            self.assertEqual(alias_result, tensor_result)
            self.assertEqual(tensor_result, numpy_result)
            self.assertEqual(out, numpy_result)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @precisionOverride({torch.bfloat16: 1e-2})
    @dtypes(*(torch.testing.get_all_fp_dtypes()))
    def test_maximum_minimum_float(self, device, dtype):
        ops = ((torch.maximum, torch.max, np.maximum), (torch.minimum, torch.min, np.minimum))

        if dtype == torch.bfloat16:
            a_np = np.random.randn(10).astype(np.float64)
            b_np = np.random.randn(10).astype(np.float64)
        else:
            a_np = np.random.randn(10).astype(torch_to_numpy_dtype_dict[dtype])
            b_np = np.random.randn(10).astype(torch_to_numpy_dtype_dict[dtype])

        for torch_op, alias, numpy_op in ops:
            numpy_result = numpy_op(a_np, b_np)

            a_tensor = torch.from_numpy(a_np).to(device=device, dtype=dtype)
            b_tensor = torch.from_numpy(b_np).to(device=device, dtype=dtype)
            tensor_result = torch_op(a_tensor, b_tensor)
            alias_result = alias(a_tensor, b_tensor)
            out = torch.empty_like(a_tensor)
            torch_op(a_tensor, b_tensor, out=out)

            self.assertEqual(alias_result, tensor_result)
            self.assertEqual(tensor_result, numpy_result)
            self.assertEqual(out, numpy_result)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*(torch.testing.get_all_fp_dtypes()))
    def test_maximum_minimum_float_nan_and_inf(self, device, dtype):
        # np.maximum and np.minimum functions compare input arrays element-wisely.
        # if one of the elements being compared is a NaN, then that element is returned.
        ops = ((torch.maximum, torch.max, np.maximum), (torch.minimum, torch.min, np.minimum))
        a_vals = (float('inf'), -float('inf'), float('nan'), float('nan'))
        b_vals = (-float('inf'), float('inf'), float('inf'), float('nan'))
        if dtype == torch.bfloat16:
            a_np = np.array(a_vals, dtype=np.float64)
            b_np = np.array(b_vals, dtype=np.float64)
        else:
            a_np = np.array(a_vals, dtype=torch_to_numpy_dtype_dict[dtype])
            b_np = np.array(b_vals, dtype=torch_to_numpy_dtype_dict[dtype])

        for torch_op, alias, numpy_op in ops:
            numpy_result = numpy_op(a_np, b_np)

            a_tensor = torch.from_numpy(a_np).to(device=device, dtype=dtype)
            b_tensor = torch.from_numpy(b_np).to(device=device, dtype=dtype)
            tensor_result = torch_op(a_tensor, b_tensor)
            alias_result = alias(a_tensor, b_tensor)

            out = torch.empty_like(a_tensor)
            torch_op(a_tensor, b_tensor, out=out)

            self.assertEqual(alias_result, tensor_result)
            if dtype == torch.bfloat16:
                self.assertEqual(tensor_result, numpy_result, exact_dtype=False)
                self.assertEqual(out, numpy_result, exact_dtype=False)
            else:
                self.assertEqual(tensor_result, numpy_result)
                self.assertEqual(out, numpy_result)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @dtypes(*product(torch.testing.get_all_complex_dtypes(), torch.testing.get_all_dtypes()))
    def test_maximum_minimum_complex(self, device, dtypes):
        for torch_op in (torch.maximum, torch.minimum, torch.max, torch.min):
            with self.assertRaisesRegex(RuntimeError, 'does not support complex inputs'):
                torch_op(torch.ones(1, device=device, dtype=dtypes[0]),
                         torch.ones(1, device=device, dtype=dtypes[1]))

            with self.assertRaisesRegex(RuntimeError, 'does not support complex inputs'):
                torch_op(torch.ones(1, device=device, dtype=dtypes[1]),
                         torch.ones(1, device=device, dtype=dtypes[0]))

    def test_bincount(self, device):
        # negative input throws
        with self.assertRaisesRegex(RuntimeError, '1-d non-negative integral'):
            torch.bincount(torch.tensor([1, -1], device=device))
        # n-d input, with n > 1 throws
        with self.assertRaisesRegex(RuntimeError, '1-d non-negative integral'):
            torch.bincount(torch.tensor([[1, 2], [3, 4]], device=device))
        # floating input type throws
        with self.assertRaisesRegex(RuntimeError, 'not implemented'):
            torch.bincount(torch.tensor([1., 0.3], device=device))
        # minlength < 0 throws
        with self.assertRaisesRegex(RuntimeError, 'minlength should be >= 0'):
            torch.bincount(torch.tensor([1, 3], device=device),
                           torch.tensor([.2, .2], device=device),
                           minlength=-1)
        # input and weights dim mismatch
        with self.assertRaisesRegex(RuntimeError, 'same length'):
            torch.bincount(torch.tensor([1, 0], device=device),
                           torch.tensor([1., 0.3, 0.5], device=device))
        # 1-d input with no elements and default minlength
        self.assertEqual(torch.bincount(torch.tensor([], device=device, dtype=torch.long)),
                         torch.zeros(0, dtype=torch.long, device=device))
        # 1-d input with no elements and specified minlength
        self.assertEqual(torch.bincount(torch.tensor([], device=device, dtype=torch.long), minlength=10),
                         torch.zeros(10, dtype=torch.long, device=device))

        # test tensor method without weights
        long_counts = torch.tensor(
            [0, 3, 2, 1, 3], dtype=torch.uint8, device=device).bincount()
        self.assertEqual(
            torch.tensor([1, 1, 1, 2], dtype=torch.int64, device=device),
            long_counts)
        # test minlength functionality
        int_counts = torch.bincount(
            torch.tensor([1, 1, 1, 1], device=device), minlength=5)
        self.assertEqual(
            torch.tensor([0, 4, 0, 0, 0], dtype=torch.int64, device=device),
            int_counts)
        # test weights
        byte_counts = torch.bincount(
            torch.tensor([0, 1, 1, 1, 4], device=device),
            torch.tensor([.1, .2, .3, .4, .5], device=device))
        self.assertEqual(
            torch.tensor([0.1, 0.9, 0, 0, 0.5], device=device), byte_counts)
        byte_counts = torch.bincount(
            torch.tensor([0, 1, 1, 1, 4], device=device),
            torch.tensor([1, 2, 3, 4, 5], dtype=torch.int8, device=device))
        self.assertEqual(
            torch.tensor([1, 9, 0, 0, 5], device=device, dtype=torch.float64), byte_counts)
        # test non-contiguous inputs and weights
        inputs = torch.tensor([[0, 0], [3, 1], [2, 1], [1, 1], [3, 4]], device=device)
        weights = torch.tensor([[.1, 1], [.2, 2], [.3, 3], [.4, 4], [.5, 5]], device=device)
        for i in [0, 1]:
            assert not inputs[:, i].is_contiguous(), "Inputs are supposed to be non-contiguous"
            assert not weights[:, i].is_contiguous(), "Weights are supposed to be non-contiguous"
        # inputs are non-contiguous but weights are contiguous
        self.assertEqual(inputs[:, 0].bincount(), torch.tensor([1, 1, 1, 2]))
        # inputs and weights are non-contiguous
        self.assertEqual(
            inputs[:, 1].bincount(weights[:, 1]),
            torch.tensor([1, 9, 0, 0, 5], dtype=torch.float32))
        # weights are non-contiguous but inputs are contiguous
        self.assertEqual(inputs[:, 1].contiguous().bincount(weights[:, 1]),
                         torch.tensor([1, 9, 0, 0, 5], dtype=torch.float32))

        # test bincount on non-contiguous slices
        all0s = torch.zeros((32, 2), dtype=torch.int64, device=device)
        self.assertEqual(all0s[:, 0].bincount(), torch.tensor([32]))

        all1s = torch.ones((32, 2), dtype=torch.int64, device=device)
        self.assertEqual(all1s[:, 0].bincount(), torch.tensor([0, 32]))

        # test large number of bins - global memory use
        big_exp = torch.zeros(10000000, device=device)
        big_exp[-1] = 50.0
        big_w = torch.tensor([.5] * 100, device=device)
        big_out = torch.tensor([9999999] * 100, device=device).bincount(big_w)
        self.assertEqual(big_exp, big_out)
        # test large input size
        big_exp = torch.zeros(2, device=device, dtype=torch.int64)
        big_exp[1] = 1000000
        big_out = torch.ones(1000000, dtype=torch.int8, device=device).bincount()
        self.assertEqual(big_exp, big_out)

    @onlyCUDA
    @expectedAlertNondeterministic('_bincount_cuda', fn_has_device_arg=False)
    def test_bincount_alert_nondeterministic(self, device):
        torch.bincount(torch.tensor([], device=device, dtype=torch.long))

    @dtypes(torch.float, torch.double, torch.half)
    def test_multinomial(self, device, dtype):
        def make_prob_dist(shape, is_contiguous):
            if is_contiguous:
                if dtype == torch.half:
                    return torch.zeros(shape, device=device).uniform_().to(dtype=torch.half)
                return torch.zeros(shape, device=device, dtype=dtype).uniform_()
            elif len(shape) == 1:
                if dtype == torch.half:
                    return torch.zeros((shape + [5]), device=device).uniform_().to(dtype=torch.half)[:, 2]
                return torch.zeros((shape + [5]), device=device, dtype=dtype).uniform_()[:, 2]
            else:
                # num dim = 2
                new_shape = [2, shape[1], 7, 1, shape[0], 1, 10]
                if dtype == torch.half:
                    prob_dist = torch.zeros(new_shape, device=device).uniform_().to(dtype=torch.half)
                else:
                    prob_dist = torch.zeros(new_shape, device=device, dtype=dtype).uniform_()
                prob_dist = prob_dist.transpose(1, 4)
                prob_dist = prob_dist[1, :, 5, 0, :, 0, 4]
                assert not prob_dist.is_contiguous()  # sanity check
                return prob_dist

        for is_contiguous in (True, False):
            # with replacement
            n_row = 3
            for n_col in range(4, 5 + 1):
                prob_dist = make_prob_dist([n_row, n_col], is_contiguous)
                # indices that shouldn't be sampled (<0 means none)
                zero_prob_indices = torch.LongTensor(n_row).random_(-2, n_col).tolist()
                for i, j in enumerate(zero_prob_indices):
                    if j >= 0:
                        prob_dist[i, j] = 0
                n_sample = n_col * 3
                sample_indices = torch.multinomial(prob_dist, n_sample, True)
                self.assertEqual(prob_dist.dim(), 2)
                self.assertEqual(sample_indices.size(1), n_sample)
                for i in range(n_row):
                    zero_prob_idx = zero_prob_indices[i]
                    if zero_prob_idx < 0:
                        continue
                    for j in range(n_sample):
                        self.assertNotEqual(sample_indices[i, j], zero_prob_idx,
                                            msg="sampled an index with zero probability")

            # without replacement
            n_row = 3
            for n_col in range(2, 10 + 1, 2):
                prob_dist = make_prob_dist([n_row, n_col], is_contiguous)
                # indices that shouldn't be sampled (<0 means none)
                zero_prob_indices = torch.LongTensor(n_row).random_(-1, n_col).tolist()
                for i, j in enumerate(zero_prob_indices):
                    if j >= 0:
                        prob_dist[i, j] = 0
                n_sample = max(1, n_col - 2)
                sample_indices = torch.multinomial(prob_dist, n_sample, False)
                self.assertEqual(prob_dist.dim(), 2)
                self.assertEqual(sample_indices.size(1), n_sample)
                for i in range(n_row):
                    row_samples = {}
                    zero_prob_idx = zero_prob_indices[i]
                    for j in range(n_sample):
                        sample_idx = sample_indices[i, j]
                        if zero_prob_idx >= 0:
                            self.assertNotEqual(sample_idx, zero_prob_idx,
                                                msg="sampled an index with zero probability")
                        self.assertNotIn(sample_idx, row_samples, "sampled an index twice")
                        row_samples[sample_idx] = True

            # vector
            n_col = 4
            prob_dist = make_prob_dist([n_col], is_contiguous).fill_(1)
            zero_prob_idx = 1  # index that shouldn't be sampled
            prob_dist[zero_prob_idx] = 0
            n_sample = 20
            sample_indices = torch.multinomial(prob_dist, n_sample, True)
            for sample_index in sample_indices:
                self.assertNotEqual(sample_index, zero_prob_idx, msg="sampled an index with zero probability")
            s_dim = sample_indices.dim()
            self.assertEqual(sample_indices.dim(), 1, msg="wrong number of dimensions")
            self.assertEqual(prob_dist.dim(), 1, msg="wrong number of prob_dist dimensions")
            self.assertEqual(sample_indices.size(0), n_sample, msg="wrong number of samples")

    @slowTest
    @dtypes(torch.float)
    def test_multinomial_rng_state_advance(self, device, dtype):
        corpus_size = 100000
        freqs = torch.ones(corpus_size, dtype=torch.float, device=device)
        n_sample = 100
        samples1 = torch.multinomial(freqs, n_sample, replacement=True)
        samples2 = torch.multinomial(freqs, n_sample, replacement=True)
        samples = torch.cat([samples1, samples2])
        # expect no more than 1 repeating elements generated in 2 attempts
        # the probability of at least element being repeated is surprisingly large, 18%
        self.assertLessEqual(2 * n_sample - samples.unique().size(0), 2)
        samples1 = torch.multinomial(freqs, n_sample, replacement=False)
        samples2 = torch.multinomial(freqs, n_sample, replacement=False)
        samples = torch.cat([samples1, samples2])
        # expect no more than 1 repeating elements generated in 2 attempts
        self.assertLessEqual(2 * n_sample - samples.unique().size(0), 1)

    def test_var_unbiased(self, device):
        tensor = torch.randn(100, device=device)
        self.assertEqual(tensor.var(0), tensor.var(0, unbiased=True))
        self.assertEqual(tensor.var(), tensor.var(unbiased=True))
        self.assertEqual(tensor.var(unbiased=False), tensor.var(0, unbiased=False))

        tensor = torch.FloatTensor([1.0, 2.0]).to(device)
        self.assertEqual(tensor.var(unbiased=True), 0.5)
        self.assertEqual(tensor.var(unbiased=False), 0.25)

        tensor = torch.randn(100, device=device)
        self.assertEqual(tensor.std(0), tensor.std(0, unbiased=True))
        self.assertEqual(tensor.std(), tensor.std(unbiased=True))
        self.assertEqual(tensor.std(unbiased=False), tensor.std(0, unbiased=False))

    def test_var_stability(self, device):
        tensor = torch.FloatTensor([2281.5, 2281.25]).to(device)

        # Stability for inner dim
        self.assertEqual(tensor.var(0), 0.03125)

        # General stability
        self.assertEqual(tensor.var(), 0.03125)

        # Stability for outer dimensions
        tensor = tensor.unsqueeze(1)
        self.assertEqual(tensor.var(0), 0.03125)

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_mul_intertype_scalar(self, device, dtype):
        x = torch.tensor(1.5, dtype=dtype, device=device)
        y = torch.tensor(3, dtype=torch.int32, device=device)

        self.assertEqual(x * y, 4.5)
        self.assertEqual(y * x, 4.5)

        with self.assertRaisesRegex(RuntimeError, "can't be cast to the desired output type"):
            y *= x
        x *= y
        self.assertEqual(x, 4.5)

    @onlyCPU
    @dtypes(torch.float, torch.double)
    def test_hardshrink(self, device, dtype):
        data = torch.tensor([1, 0.5, 0.3, 0.6], dtype=dtype, device=device).view(2, 2)
        self.assertEqual(torch.tensor([1, 0.5, 0, 0.6], dtype=dtype, device=device).view(2, 2),
                         data.hardshrink(0.3))
        self.assertEqual(torch.tensor([1, 0, 0, 0.6], dtype=dtype, device=device).view(2, 2),
                         data.hardshrink(0.5))

        # test default lambd=0.5
        self.assertEqual(data.hardshrink(), data.hardshrink(0.5))

        # test non-contiguous case
        self.assertEqual(torch.tensor([1, 0, 0.5, 0.6], dtype=dtype, device=device).view(2, 2),
                         data.t().hardshrink(0.3))

    @onlyCPU
    @dtypes(torch.float, torch.double)
    def test_hardshrink_edge_cases(self, device, dtype) -> None:
        def h(values, l_expected):
            for l, expected in l_expected.items():
                values_tensor = torch.tensor([float(v) for v in values],
                                             dtype=dtype, device=device)
                expected_tensor = torch.tensor([float(v) for v in expected],
                                               dtype=dtype, device=device)
                self.assertEqual(expected_tensor == values_tensor.hardshrink(l),
                                 torch.ones_like(values_tensor, dtype=torch.bool))

        def test_helper(min, max):
            h([0.0, min, -min, 0.1, -0.1, 1.0, -1.0, max, -max, inf, -inf],
              {0.0: [0.0, min, -min, 0.1, -0.1, 1.0, -1.0, max, -max, inf, -inf],
               min: [0.0, 0.0, 0.0, 0.1, -0.1, 1.0, -1.0, max, -max, inf, -inf],
               0.1: [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, -1.0, max, -max, inf, -inf],
               1.0: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, max, -max, inf, -inf],
               max: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, inf, -inf],
               inf: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]})

        test_helper(torch.finfo(dtype).tiny, torch.finfo(dtype).max)

    @onlyCPU
    @slowTest
    @unittest.skipIf(not TEST_NUMPY, 'Numpy not found')
    @dtypes(torch.double)
    def test_einsum(self, device: torch.device, dtype: torch.dtype) -> None:
        # test cases taken from https://gist.github.com/rockt/15ee013889d65342088e9260a377dc8f
        x = torch.randn(5, dtype=dtype, device=device)
        y = torch.randn(7, dtype=dtype, device=device)
        A = torch.randn(3, 5, dtype=dtype, device=device)
        B = torch.randn(2, 5, dtype=dtype, device=device)
        C = torch.randn(2, 3, 5, dtype=dtype, device=device)
        D = torch.randn(2, 5, 7, dtype=dtype, device=device)
        E = torch.randn(7, 9, dtype=dtype, device=device)
        F = torch.randn(2, 3, 5, 7, dtype=dtype, device=device)
        G = torch.randn(7, 11, 13, dtype=dtype, device=device)
        H = torch.randn(4, 4, dtype=dtype, device=device)
        I = torch.randn(3, 4, 4, dtype=dtype, device=device)
        l = torch.randn(5, 10, dtype=dtype, device=device)
        r = torch.randn(5, 20, dtype=dtype, device=device)
        w = torch.randn(30, 10, 20, dtype=dtype, device=device)
        test_list: List[Union[Tuple[str, torch.Tensor],
                        Tuple[str, torch.Tensor, torch.Tensor],
                        Tuple[str, torch.Tensor, torch.Tensor, torch.Tensor]]] = [
            # -- Vector
            ("i->", x),                 # sum
            ("i,i->", x, x),            # dot
            ("i,i->i", x, x),           # vector element-wise mul
            ("i,j->ij", x, y),          # outer
            # -- Matrix
            ("ij->ji", A),              # transpose
            ("ij->j", A),               # row sum
            ("ij->i", A),               # col sum
            ("ij,ij->ij", A, A),        # matrix element-wise mul
            ("ij,j->i", A, x),          # matrix vector multiplication
            ("ij,kj->ik", A, B),        # matmul
            ("ij,ab->ijab", A, E),      # matrix outer product
            # -- Tensor
            ("aij,ajk->aik", C, D),     # batch matmul
            ("ijk,jk->i", C, A),        # tensor matrix contraction
            ("aij,jk->aik", D, E),      # tensor matrix contraction
            ("abcd,dfg->abcfg", F, G),  # tensor tensor contraction
            ("ijk,jk->ik", C, A),       # tensor matrix contraction with double indices
            ("ijk,jk->ij", C, A),       # tensor matrix contraction with double indices
            ("ijk,ik->j", C, B),        # non contiguous
            ("ijk,ik->jk", C, B),       # non contiguous with double indices
            # -- Diagonal
            ("ii", H),                 # trace
            ("ii->i", H),              # diagonal
            # -- Ellipsis
            ("i...->...", H),
            ("ki,...k->i...", A.t(), B),
            ("k...,jk", A.t(), B),
            ("...ii->...i", I),       # batch diagonal
            # -- Other
            ("bn,anm,bm->ba", l, w, r),  # as torch.bilinear
            ("... ii->...i  ", I),       # batch diagonal with spaces
        ]
        for test in test_list:
            actual = torch.einsum(test[0], test[1:])
            expected = np.einsum(test[0], *[t.numpy() for t in test[1:]])
            self.assertEqual(expected.shape, actual.shape, msg=test[0])
            self.assertEqual(expected, actual, msg=test[0])
            # test vararg
            actual2 = torch.einsum(test[0], *test[1:])
            self.assertEqual(expected.shape, actual2.shape, msg=test[0])
            self.assertEqual(expected, actual2, msg=test[0])

            def do_einsum(*args):
                return torch.einsum(test[0], args)
            # FIXME: following test cases fail gradcheck
            if test[0] not in {"i,i->", "i,i->i", "ij,ij->ij"}:
                gradcheck_inps = tuple(t.detach().requires_grad_() for t in test[1:])
                self.assertTrue(torch.autograd.gradcheck(do_einsum, gradcheck_inps))
            self.assertTrue(A._version == 0)  # check that we do not use inplace ops

    @onlyCPU
    @dtypes(torch.bool, torch.double)
    def test_sum_all(self, device, dtype) -> None:
        def check_sum_all(tensor: torch.Tensor) -> None:
            pylist = tensor.reshape(-1).tolist()
            self.assertEqual(tensor.sum(), sum(pylist))

        if dtype != torch.bool:
            check_sum_all(torch.tensor([1, 2, 3, 4, 5], dtype=dtype, device=device))
            check_sum_all(torch.randn(200000, dtype=dtype, device=device))
            check_sum_all(torch.randn(2000, 2, dtype=dtype, device=device)[:, 0])
        else:
            check_sum_all(torch.tensor([True, False, True], dtype=torch.bool, device=device))

    def _test_memory_format_transformations(self, device, input_generator_fn, transformation_fn,
                                            memory_format, compare_data=True, default_is_preserve=False):

        assert(memory_format == torch.channels_last or memory_format == torch.channels_last_3d)

        # xc is a channels last tensor
        xc = input_generator_fn(device)
        # xc is not memory dense, but looks like channels last
        if memory_format == torch.channels_last:
            xc = xc[..., ::2, ::2]
        else:
            xc = xc[..., ::2, ::2, ::2]

        clone = transformation_fn(xc, memory_format=torch.preserve_format)
        self.assertFalse(clone.is_contiguous())
        self.assertTrue(clone.is_contiguous(memory_format=memory_format))
        self.assertFalse(xc.is_contiguous())
        self.assertFalse(xc.is_contiguous(memory_format=memory_format))
        if compare_data:
            self.assertEqual(xc, clone.to(xc))

        xc = input_generator_fn(device)
        clone = transformation_fn(xc, memory_format=torch.contiguous_format)
        self.assertTrue(clone.is_contiguous())
        self.assertFalse(clone.is_contiguous(memory_format=memory_format))
        if compare_data:
            self.assertEqual(xc, clone.to(xc))

        xc = input_generator_fn(device)
        clone = transformation_fn(xc)

        if default_is_preserve:
            self.assertFalse(clone.is_contiguous())
            self.assertTrue(clone.is_contiguous(memory_format=memory_format))
        else:
            self.assertTrue(clone.is_contiguous())
            self.assertFalse(clone.is_contiguous(memory_format=memory_format))
        if compare_data:
            self.assertEqual(xc, clone.to(xc))

        x = torch.randn((3, 4, 5, 6, 7, 8, 9), device=device)
        for _ in range(10):
            permutation = list(range(len(x.shape)))
            random.shuffle(permutation)
            x = x.permute(permutation)
            self.assertEqual(x.stride(), transformation_fn(x, memory_format=torch.preserve_format).stride())

    def test_memory_format_to(self, device):
        def get_generator(memory_format, shape):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=torch.float32).contiguous(memory_format=memory_format)
            return input_generator_fn

        def transformation_fn(tensor, **kwargs):
            return tensor.to(dtype=torch.float64, **kwargs)

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape in formats_shapes:
            self._test_memory_format_transformations(
                device, get_generator(mf, shape), transformation_fn, mf, default_is_preserve=True)

    def test_memory_format_type(self, device):
        def get_generator(memory_format, shape):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=torch.float32).contiguous(memory_format=memory_format)
            return input_generator_fn

        def transformation_fn(tensor, **kwargs):
            return tensor.to(torch.float64, **kwargs)

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape in formats_shapes:
            self._test_memory_format_transformations(
                device, get_generator(mf, shape), transformation_fn, mf, default_is_preserve=True)

    def test_memory_format_clone(self, device):
        def get_generator(memory_format, shape):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=torch.float32).contiguous(memory_format=memory_format)
            return input_generator_fn

        def transformation_fn(tensor, **kwargs):
            return tensor.clone(**kwargs)

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape in formats_shapes:
            self._test_memory_format_transformations(
                device, get_generator(mf, shape), transformation_fn, mf, True, default_is_preserve=True)

    @onlyCPU
    @dtypes(torch.double)
    def test_sum_out(self, device, dtype: torch.dtype) -> None:
        x = torch.rand(100, 100, dtype=dtype, device=device)
        res1 = torch.sum(x, 1)
        res2 = torch.tensor((), dtype=dtype, device=device)
        torch.sum(x, 1, out=res2)
        self.assertEqual(res1, res2)
        x = torch.rand(100, 100, 100, dtype=dtype, device=device)
        res1 = x.sum(2).sum(1)
        res2 = torch.tensor((), dtype=dtype, device=device)
        torch.sum(x, (2, 1), out=res2)
        self.assertEqual(res1, res2)

    def test_memory_format_factory_like_functions_preserve(self, device):
        def get_generator(memory_format, shape):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=torch.float32).contiguous(memory_format=memory_format)
            return input_generator_fn

        transformation_fns = [
            lambda t, **kwargs: torch.zeros_like(t, **kwargs),
            lambda t, **kwargs: torch.ones_like(t, **kwargs),
            lambda t, **kwargs: torch.randint_like(t, 10, 100, **kwargs),
            lambda t, **kwargs: torch.randint_like(t, 100, **kwargs),
            lambda t, **kwargs: torch.randn_like(t, **kwargs),
            lambda t, **kwargs: torch.rand_like(t, **kwargs),
            lambda t, **kwargs: torch.full_like(t, 7, **kwargs),
            lambda t, **kwargs: torch.empty_like(t, **kwargs)]

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape, in formats_shapes:
            for transformation_fn in transformation_fns:
                self._test_memory_format_transformations(
                    device, get_generator(mf, shape), transformation_fn, mf, compare_data=False, default_is_preserve=True)

    def test_memory_format_type_shortcuts(self, device):
        def get_generator(memory_format, shape, dtype):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=dtype).clamp(0, 1) \
                    .round().contiguous(memory_format=memory_format)
            return input_generator_fn


        def get_fn(fn_name):
            def transformation_fn(tensor, **kwargs):
                fn = getattr(tensor, fn_name)
                return fn(**kwargs)
            return transformation_fn

        shortcuts = ['byte', 'char', 'double', 'bool', 'half', 'int', 'long', 'short']
        if device == 'cpu':
            shortcuts += ['bfloat16']

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape in formats_shapes:
            for fn_name in shortcuts:
                self._test_memory_format_transformations(
                    device, get_generator(mf, shape, torch.float32), get_fn(fn_name), mf, default_is_preserve=True)

        # Test 'float' separately to avoid float->float no-op.
        for mf, shape in formats_shapes:
            self._test_memory_format_transformations(
                device, get_generator(mf, shape, torch.float64), get_fn('float'), mf, default_is_preserve=True)

    @onlyCUDA
    def test_memory_format_cpu_and_cuda_ops(self, device):
        def get_generator(memory_format, shape):
            def input_generator_fn(device):
                return torch.randn(shape, device=device, dtype=torch.float32).contiguous(memory_format=memory_format)
            return input_generator_fn

        def transformation_cpu_fn(tensor, **kwargs):
            return tensor.cpu(**kwargs)

        def transformation_cuda_fn(tensor, **kwargs):
            return tensor.cuda(**kwargs)

        formats_shapes = (
            (torch.channels_last, (4, 3, 8, 8)),
            (torch.channels_last_3d, (4, 3, 8, 8, 8)))

        for mf, shape in formats_shapes:
            self._test_memory_format_transformations(
                'cuda', get_generator(mf, shape), transformation_cpu_fn, mf, default_is_preserve=True)
            self._test_memory_format_transformations(
                'cpu', get_generator(mf, shape), transformation_cuda_fn, mf, default_is_preserve=True)

    @onlyCPU
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_eig(self, device, dtype):
        a = torch.Tensor(((1.96, 0.00, 0.00, 0.00, 0.00),
                          (-6.49, 3.80, 0.00, 0.00, 0.00),
                          (-0.47, -6.39, 4.17, 0.00, 0.00),
                          (-7.20, 1.50, -1.51, 5.70, 0.00),
                          (-0.65, -6.34, 2.67, 1.80, -7.10))).t().contiguous().to(dtype=dtype, device=device)
        e = torch.eig(a)[0]
        ee, vv = torch.eig(a, True)
        te = torch.tensor((), dtype=dtype, device=device)
        tv = torch.tensor((), dtype=dtype, device=device)
        eee, vvv = torch.eig(a, True, out=(te, tv))
        self.assertEqual(e, ee, atol=1e-12, rtol=0)
        self.assertEqual(ee, eee, atol=1e-12, rtol=0)
        self.assertEqual(ee, te, atol=1e-12, rtol=0)
        self.assertEqual(vv, vvv, atol=1e-12, rtol=0)
        self.assertEqual(vv, tv, atol=1e-12, rtol=0)

        # test reuse
        X = torch.randn(4, 4, dtype=dtype, device=device)
        X = torch.mm(X.t(), X)
        e = torch.zeros(4, 2, dtype=dtype, device=device)
        v = torch.zeros(4, 4, dtype=dtype, device=device)
        torch.eig(X, True, out=(e, v))
        Xhat = torch.mm(torch.mm(v, torch.diag(e.select(1, 0))), v.t())
        self.assertEqual(X, Xhat, atol=1e-8, rtol=0, msg='VeV\' wrong')
        self.assertFalse(v.is_contiguous(), 'V is contiguous')

        torch.eig(X, True, out=(e, v))
        Xhat = torch.mm(v, torch.mm(e.select(1, 0).diag(), v.t()))
        self.assertEqual(X, Xhat, atol=1e-8, rtol=0, msg='VeV\' wrong')
        self.assertFalse(v.is_contiguous(), 'V is contiguous')

        # test non-contiguous
        X = torch.randn(4, 4, dtype=dtype, device=device)
        X = torch.mm(X.t(), X)
        e = torch.zeros(4, 2, 2, dtype=dtype, device=device)[:, 1]
        v = torch.zeros(4, 2, 4, dtype=dtype, device=device)[:, 1]
        self.assertFalse(v.is_contiguous(), 'V is contiguous')
        self.assertFalse(e.is_contiguous(), 'E is contiguous')
        torch.eig(X, True, out=(e, v))
        Xhat = torch.mm(torch.mm(v, torch.diag(e.select(1, 0))), v.t())
        self.assertEqual(X, Xhat, atol=1e-8, rtol=0, msg='VeV\' wrong')

        # test invalid input
        self.assertRaisesRegex(
            RuntimeError,
            'A should be 2 dimensional',
            lambda: torch.eig(torch.ones((2))))
        self.assertRaisesRegex(
            RuntimeError,
            'A should be square',
            lambda: torch.eig(torch.ones((2, 3))))
        self.assertRaisesRegex(
            RuntimeError,
            'A should not contain infs or NaNs',
            lambda: torch.eig(np.inf * torch.ones((2, 2))))
        self.assertRaisesRegex(
            RuntimeError,
            'A should not contain infs or NaNs',
            lambda: torch.eig(np.nan * torch.ones((2, 2))))

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_lobpcg_basic(self, device, dtype):
        self._test_lobpcg_method(device, dtype, 'basic')

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(torch.double)
    def test_lobpcg_ortho(self, device, dtype):
        self._test_lobpcg_method(device, dtype, 'ortho')

    def _test_lobpcg_method(self, device, dtype, method):
        from torch.testing._internal.common_utils import random_symmetric_pd_matrix, random_sparse_pd_matrix
        from torch._linalg_utils import matmul, qform
        from torch._lobpcg import lobpcg

        def test_tracker(worker):
            k = worker.iparams['k']
            nc = worker.ivars['converged_count']
            if k <= nc:
                tol = worker.fparams['tol']
                rerr = worker.tvars['rerr']
                X = worker.X
                E = worker.E
                B = worker.B
                A = worker.A
                dtype = X.dtype
                device = X.device

                # Check convergence
                self.assertLessEqual(rerr[:k].max(), tol)

                # Check B-orthogonality
                I = torch.eye(k, k, dtype=dtype, device=device)
                self.assertEqual(qform(B, X[:, :k]), I)

                # Check block equation
                self.assertEqual(qform(A, X[:, :k]) / E[:k], I, atol=0.2, rtol=0)

        orig_lobpcg = lobpcg

        def lobpcg(*args, **kwargs):
            kwargs['tracker'] = test_tracker
            kwargs['niter'] = 1000
            kwargs['method'] = method
            kwargs['tol'] = 1e-8
            return orig_lobpcg(*args, **kwargs)
        prec = 5e-4

        # check dense input
        mm = torch.matmul
        for batches in [(), (2,), (2, 3)]:
            for m, n, k in [
                    (9, 3, 1),
                    (9, 3, 2),
                    (9, 2, 2),
                    (100, 15, 5),
            ]:
                # skip tests that are known to fail with the basic
                # LOBPCG method due to calling cholesky on singular
                # input
                if method == 'basic' and (m, n, k) in [(9, 2, 2), (100, 15, 5)]:
                    continue
                A = random_symmetric_pd_matrix(m, *batches, device=device, dtype=dtype)
                B = random_symmetric_pd_matrix(m, *batches, device=device, dtype=dtype)

                # classical eigenvalue problem, smallest eigenvalues
                E, V = lobpcg(A, k=k, n=n, largest=False)
                self.assertEqual(E.shape, batches + (k,))
                self.assertEqual(V.shape, batches + (m, k))
                self.assertEqual(matmul(A, V), mm(V, E.diag_embed()), atol=prec, rtol=0)
                e = torch.symeig(A)[0]
                e_smallest = e[..., :k]
                self.assertEqual(E, e_smallest)

                # classical eigenvalue problem, largest eigenvalues
                E, V = lobpcg(A, k=k, n=n, largest=True)
                e_largest, _ = torch.sort(e[..., -k:], descending=True)
                self.assertEqual(E, e_largest, atol=prec, rtol=0)
                self.assertEqual(matmul(A, V), mm(V, E.diag_embed()), atol=prec, rtol=0)

                # generalized eigenvalue problem, smallest eigenvalues
                E, V = lobpcg(A, B=B, k=k, n=n, largest=False)
                self.assertEqual(matmul(A, V), mm(matmul(B, V), E.diag_embed()), atol=prec, rtol=0)

                # generalized eigenvalue problem, largest eigenvalues
                E, V = lobpcg(A, B=B, k=k, n=n, largest=True)
                self.assertEqual(matmul(A, V) / E.max(), mm(matmul(B, V), (E / E.max()).diag_embed()),
                                 atol=prec, rtol=0)

        # check sparse input
        for m, n, k, density in [
                (5, 1, 1, 0.8),
                (9, 3, 2, 0.5),
                (100, 1, 1, 0.1),
                (1000, 7, 3, 0.01),
        ]:
            # skip tests that are known to fail with the basic LOBCG
            # method due to insufficient accuracy
            if method == 'basic' and (m, n, k, density) in [(1000, 7, 3, 0.01)]:
                continue
            A = random_sparse_pd_matrix(m, density=density, device=device, dtype=dtype)
            B = random_sparse_pd_matrix(m, density=density, device=device, dtype=dtype)
            A_eigenvalues = torch.arange(1, m + 1, dtype=dtype) / m
            e_smallest = A_eigenvalues[..., :k]
            e_largest, _ = torch.sort(A_eigenvalues[..., -k:], descending=True)

            # classical eigenvalue problem, smallest eigenvalues
            E, V = lobpcg(A, k=k, n=n, largest=False)
            self.assertEqual(E, e_smallest)
            self.assertEqual(matmul(A, V), mm(V, E.diag_embed()), atol=prec, rtol=0)

            # classical eigenvalue problem, largest eigenvalues
            E, V = lobpcg(A, k=k, n=n, largest=True)
            self.assertEqual(matmul(A, V), mm(V, E.diag_embed()), atol=prec, rtol=0)
            self.assertEqual(E, e_largest)

            # generalized eigenvalue problem, smallest eigenvalues
            E, V = lobpcg(A, B=B, k=k, n=n, largest=False)
            self.assertEqual(matmul(A, V), matmul(B, mm(V, E.diag_embed())), atol=prec, rtol=0)

            # generalized eigenvalue problem, largest eigenvalues
            E, V = lobpcg(A, B=B, k=k, n=n, largest=True)
            self.assertEqual(matmul(A, V) / E.max(), mm(matmul(B, V), (E / E.max()).diag_embed()),
                             atol=prec, rtol=0)

    @skipCPUIfNoLapack
    @onlyCPU
    @dtypes(torch.double)
    def test_lobpcg_torchscript(self, device, dtype):
        from torch.testing._internal.common_utils import random_sparse_pd_matrix
        from torch._linalg_utils import matmul as mm

        lobpcg = torch.jit.script(torch.lobpcg)

        m = 500
        k = 5
        A1 = random_sparse_pd_matrix(m, density=2.0 / m, device=device, dtype=dtype)
        X1 = torch.randn((m, k), dtype=dtype, device=device)
        E1, V1 = lobpcg(A1, X=X1)
        eq_err = torch.norm((mm(A1, V1) - V1 * E1), 2) / E1.max()
        self.assertLess(eq_err, 1e-6)

    @unittest.skipIf(not TEST_SCIPY or (TEST_SCIPY and scipy.__version__ < '1.4.1'), "Scipy not found or older than 1.4.1")
    @skipCPUIfNoLapack
    @onlyCPU
    @dtypes(torch.double)
    def test_lobpcg_scipy(self, device, dtype):
        """Compare torch and scipy.sparse.linalg implementations of lobpcg
        """
        import time
        import scipy
        from torch.testing._internal.common_utils import random_sparse_pd_matrix
        from torch._linalg_utils import matmul as mm
        from scipy.sparse.linalg import lobpcg as scipy_lobpcg
        import scipy.sparse

        def toscipy(A):
            if A.layout == torch.sparse_coo:
                values = A.coalesce().values().cpu().numpy().copy()
                indices = A.coalesce().indices().cpu().numpy().copy()
                return scipy.sparse.coo_matrix((values, (indices[0], indices[1])), A.shape)
            return A.cpu().numpy().copy()

        niter = 1000
        repeat = 10
        m = 500   # size of the square matrix
        k = 7     # the number of requested eigenpairs
        A1 = random_sparse_pd_matrix(m, density=2.0 / m, device=device, dtype=dtype)
        B1 = random_sparse_pd_matrix(m, density=2.0 / m, device=device, dtype=dtype)
        X1 = torch.randn((m, k), dtype=dtype, device=device)

        A2 = toscipy(A1)
        B2 = toscipy(B1)
        X2 = toscipy(X1)

        lambdas1 = []

        def tracker(worker):
            lambdas1.append(worker.E[:])

        tol = 1e-8
        # tol for scipy lobpcg will be choosed so that the number of
        # iterations will be equal or very close to pytorch lobpcg
        # (that is around 170-180)

        # Standard eigenvalue problem
        E1, V1 = torch.lobpcg(A1, X=X1, niter=niter, largest=True, tracker=tracker, tol=tol)
        E2, V2, lambdas2 = scipy_lobpcg(A2, X2, maxiter=niter, largest=True, retLambdaHistory=True, tol=1.1 * tol)
        iters1 = len(lambdas1)
        iters2 = len(lambdas2)
        self.assertLess(abs(iters1 - iters2), 0.05 * max(iters1, iters2))

        E2a, V2a = scipy_lobpcg(A2, X2, maxiter=niter, largest=False)

        eq_err = torch.norm((mm(A1, V1) - V1 * E1), 2) / E1.max()
        eq_err_scipy = (abs(A2.dot(V2) - V2 * E2)**2).sum() ** 0.5 / E2.max()
        self.assertLess(eq_err, 1e-6)        # std
        self.assertLess(eq_err_scipy, 1e-6)  # std

        self.assertEqual(E1, torch.from_numpy(E2.copy()))

        # Generalized eigenvalue problem
        lambdas1 = []

        def tracker(worker):
            lambdas1.append(worker.E[:])

        E1, V1 = torch.lobpcg(A1, B=B1, X=X1, niter=niter, largest=True, tracker=tracker, tol=tol)
        E2, V2, lambdas2 = scipy_lobpcg(A2, X2, B=B2, maxiter=niter, largest=True, retLambdaHistory=True, tol=39 * tol)
        E2a, V2a = scipy_lobpcg(A2, X2, B=B2, maxiter=niter, largest=False)
        iters1 = len(lambdas1)
        iters2 = len(lambdas2)
        self.assertLess(abs(iters1 - iters2), 0.05 * max(iters1, iters2))

        eq_err = torch.norm((mm(A1, V1) - mm(B1, V1) * E1), 2) / E1.max()
        eq_err_scipy = (abs(A2.dot(V2) - B2.dot(V2) * E2)**2).sum() ** 0.5 / E2.max()
        self.assertLess(eq_err, 1e-6)        # general
        self.assertLess(eq_err_scipy, 1e-6)  # general

        self.assertEqual(E1, torch.from_numpy(E2.copy()))

        # Timings
        elapsed_ortho = 0
        elapsed_ortho_general = 0
        elapsed_scipy = 0
        elapsed_general_scipy = 0
        for i in range(repeat):
            start = time.time()
            torch.lobpcg(A1, X=X1, niter=niter, method='ortho', tol=tol)
            end = time.time()
            elapsed_ortho += end - start

            start = time.time()
            torch.lobpcg(A1, X=X1, B=B1, niter=niter, method='ortho', tol=tol)
            end = time.time()
            elapsed_ortho_general += end - start

            start = time.time()
            scipy_lobpcg(A2, X2, maxiter=niter, tol=1.1 * tol)
            end = time.time()
            elapsed_scipy += end - start

            start = time.time()
            scipy_lobpcg(A2, X2, B=B2, maxiter=niter, tol=39 * tol)
            end = time.time()
            elapsed_general_scipy += end - start

        elapsed_ortho_ms = 1000.0 * elapsed_ortho / repeat
        elapsed_ortho_general_ms = 1000.0 * elapsed_ortho_general / repeat
        elapsed_scipy_ms = 1000.0 * elapsed_scipy / repeat
        elapsed_general_scipy_ms = 1000.0 * elapsed_general_scipy / repeat

        print('''
CPU timings: torch.lobpcg vs scipy.sparse.linalg.lobpcg
-------------------------------------------------------
              | standard    | generalized | method
torch.lobpcg  | {:10.2f}  | {:10.2f}  | ortho
scipy_lobpcg  | {:10.2f}  | {:10.2f}  | N/A
-(input size: {:4}, eigenpairs:{:2}, units: ms per call)-
        '''.format(elapsed_ortho_ms, elapsed_ortho_general_ms,
                   elapsed_scipy_ms, elapsed_general_scipy_ms,
                   m, k))

        # Handling of very small tolerence
        tol = 1e-100

        lambdas1 = []

        def tracker(worker):
            lambdas1.append(worker.E[:])

        E1, V1 = torch.lobpcg(A1, X=X1, niter=niter, largest=True, tracker=tracker, tol=tol)
        iters1 = len(lambdas1)
        eq_err = torch.norm((mm(A1, V1) - V1 * E1), 2) / E1.max()

        try:
            E2, V2, lambdas2 = scipy_lobpcg(A2, X2, maxiter=niter, largest=True, retLambdaHistory=True, tol=tol)
            iters2 = len(lambdas2)
            eq_err_scipy = (abs(A2.dot(V2) - V2 * E2)**2).sum() ** 0.5 / E2.max()
        except Exception as msg:
            print('Calling scipy_lobpcg failed [standard]:', msg)
            iters2 = -1
            eq_err_scipy = -1

        lambdas1 = []

        def tracker(worker):
            lambdas1.append(worker.E[:])

        E1, V1 = torch.lobpcg(A1, X=X1, B=B1, niter=niter, largest=True, tracker=tracker, tol=tol)
        iters1_general = len(lambdas1)
        eq_err_general = torch.norm((mm(A1, V1) - mm(B1, V1) * E1), 2) / E1.max()

        try:
            E2, V2, lambdas2 = scipy_lobpcg(A2, X2, B=B2, maxiter=niter, largest=True, retLambdaHistory=True, tol=tol)
            iters2_general = len(lambdas2)
            eq_err_general_scipy = (abs(A2.dot(V2) - B2.dot(V2) * E2)**2).sum() ** 0.5 / E2.max()
        except Exception as msg:
            print('Calling scipy_lobpcg failed [generalized]:', msg)
            iters2_general = -1
            eq_err_general_scipy = -1

        print('''\
Handling of small tol={:6.0e}: torch.lobpcg vs scipy.sparse.linalg.lobpcg
----------------------------------------------------------------------------
              | standard    | generalized |  niter | method
torch.lobpcg  | {:10.2e}  | {:10.2e}  | {:6} | ortho
scipy_lobpcg  | {:10.2e}  | {:10.2e}  | {:6} | N/A
---(input size: {:4}, eigenpairs:{:2}, units: relative error, maxiter={:4})---
'''.format(tol, eq_err, eq_err_general, iters1, eq_err_scipy, eq_err_general_scipy, iters2, m, k, niter))

    def _test_addmm_addmv(self, f, t, m, v, *, alpha=None, beta=None, transpose_out=False):
        dtype = t.dtype
        numpy_dtype = dtype
        if dtype in {torch.bfloat16}:
            numpy_dtype = torch.float
        if dtype.is_complex:
            alpha = 0.9 + 0.3j if alpha is None else alpha
            beta = 0.5 + 0.6j if beta is None else beta
        else:
            alpha = 1.2 if alpha is None else alpha
            beta = 0.8 if beta is None else beta
        res1 = f(t, m, v, alpha=alpha, beta=beta)
        res2 = torch.full_like(res1, math.nan)
        if transpose_out:
            res2 = res2.t().clone(memory_format=torch.contiguous_format).t()
        f(t, m, v, alpha=alpha, beta=beta, out=res2)
        res3 = alpha * (m.to(numpy_dtype).cpu().numpy() @ v.to(numpy_dtype).cpu().numpy())
        if beta != 0:
            res3 += (beta * t).to(numpy_dtype).cpu().numpy()
        res3 = torch.from_numpy(res3).to(dtype)
        self.assertEqual(res1, res2)
        self.assertEqual(res1, res3)

    @precisionOverride({torch.bfloat16: 1e-0, torch.half: 5e-4, torch.float: 1e-4, torch.double: 1e-8,
                        torch.cfloat: 1e-4, torch.cdouble: 1e-8})
    @dtypesIfCUDA(*torch.testing.get_all_complex_dtypes(),
                  *([torch.float32, torch.float64, torch.bfloat16]
                    if TEST_WITH_ROCM else torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM)))
    @dtypes(torch.bfloat16, torch.float, torch.double, torch.cfloat, torch.cdouble)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_addmv(self, device, dtype):
        # have to use torch.randn(...).to(bfloat16) instead of
        # torch.randn(..., dtype=bfloat16). randn does not support
        # bfloat16 yet.
        ts = [
            torch.randn(10, device=device).to(dtype),
            torch.randn(1, device=device).to(dtype).expand(10),
        ]
        vs = [
            torch.randn(100, device=device).to(dtype),
            torch.ones(1, device=device).to(dtype).expand(100),  # to reduce errors for low precision
        ]
        ms = [
            # 0d
            torch.ones((), device=device).to(dtype).expand(10, 100),  # to reduce errors for low precision
            # 1d
            torch.randn((1, 100), device=device).to(dtype).expand(10, 100),
            # this initialization reduces errors for low precision for broadcasted matrices
            # by making sure that intermediate and result values are exactly representable
            # in low precision type
            torch.randint(3, (10, 1), dtype=torch.float, device=device).to(dtype).expand(10, 100),
            # 2d
            torch.randn((10, 100), device=device).to(dtype),
            torch.randn((100, 10), device=device).to(dtype).t(),
        ]
        for m, v, t in product(ms, vs, ts):
            self._test_addmm_addmv(torch.addmv, t, m, v)
        # Test beta=0, t=nan
        t = torch.full((10,), math.nan, device=device).to(dtype)
        for m, v in product(ms, vs):
            self._test_addmm_addmv(torch.addmv, t, m, v, beta=0)

    @dtypesIfCUDA(*([torch.half, torch.float, torch.double]
                    + ([torch.bfloat16] if TEST_WITH_ROCM else [])))
    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_addmv_rowmajor_colmajor_incx_incy_lda(self, device, dtype):
        # tests (o, s)*(s).  o is output size, s is summed size.
        o = 5
        s = 3
        a_data = torch.arange(1, o * s + 1, device=device, dtype=dtype).view(o, s)
        x_data = torch.arange(1, s + 1, 1, device=device, dtype=dtype)
        y_data = torch.ones(o, device=device, dtype=dtype)
        control = torch.tensor([15., 33., 51., 69., 87.], device=device, dtype=dtype)

        def _test(row_major, incx, incy, lda_tail):
            if row_major:
                a_storage = torch.full((o, s + lda_tail), float('nan'), device=device, dtype=dtype)
            else:
                a_storage = torch.full((s, o + lda_tail), float('nan'), device=device, dtype=dtype).permute(1, 0)
            a = a_storage[:o, :s].copy_(a_data)

            x_storage = torch.full((s, incx), float('nan'), device=device, dtype=dtype)
            x = x_storage[:, 0].copy_(x_data)

            y_storage = torch.full((o, incy), float('nan'), device=device, dtype=dtype)
            y = y_storage[:, 0].copy_(y_data)

            self._test_addmm_addmv(torch.addmv, y, a, x)

        for row_major, incx, incy, lda_tail in product((False, True), (1, 2), (1, 2), (0, 1)):
            _test(row_major, incx, incy, lda_tail)

    @precisionOverride({torch.double: 1e-8, torch.float: 1e-4, torch.bfloat16: 0.6,
                        torch.half: 1e-1, torch.cfloat: 1e-4, torch.cdouble: 1e-8})
    @dtypesIfCUDA(*torch.testing.get_all_complex_dtypes(), *torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM))
    @dtypes(*torch.testing.get_all_complex_dtypes(), *torch.testing.get_all_fp_dtypes())
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @tf32_on_and_off(0.05)
    def test_addmm(self, device, dtype):
        M = torch.randn(10, 25, device=device).to(dtype)
        m1 = torch.randn(10, 50, device=device).to(dtype)
        m2 = torch.randn(50, 25, device=device).to(dtype)
        self._test_addmm_addmv(torch.addmm, M, m1, m2)

        # Test 0-strided
        M = torch.randn(10, 1, device=device).to(dtype).expand(10, 25)
        m1 = torch.randn(10, 1, device=device).to(dtype).expand(10, 50)
        m2 = torch.randn(50, 25, device=device).to(dtype)
        self._test_addmm_addmv(torch.addmm, M, m1, m2)

        # Test beta=0, M=nan
        M = torch.full((10, 25), math.nan, device=device).to(dtype)
        m1 = torch.randn(10, 50, device=device).to(dtype)
        m2 = torch.randn(50, 25, device=device).to(dtype)
        self._test_addmm_addmv(torch.addmm, M, m1, m2, beta=0)

        # Test transpose
        for t1, t2, t3, t4 in product([True, False], repeat=4):
            def maybe_transpose(cond, m):
                if not cond:
                    return m
                return m.t().clone(memory_format=torch.contiguous_format).t()

            M = maybe_transpose(t1, torch.randn(10, 25, device=device).to(dtype))
            m1 = maybe_transpose(t2, torch.randn(10, 50, device=device).to(dtype))
            m2 = maybe_transpose(t3, torch.randn(50, 25, device=device).to(dtype))
            self._test_addmm_addmv(torch.addmm, M, m1, m2, transpose_out=t4)

    @dtypes(torch.float, torch.double)
    @dtypesIfCUDA(*([torch.float, torch.double] +
                    ([] if TEST_WITH_ROCM else torch.testing.get_all_complex_dtypes())))
    @tf32_on_and_off(0.005)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_addmm_sizes(self, device, dtype):
        for m in [0, 1, 25]:
            for n in [0, 1, 10]:
                for k in [0, 1, 8]:
                    M = torch.randn(n, m, device=device).to(dtype)
                    m1 = torch.randn(n, k, device=device).to(dtype)
                    m2 = torch.randn(k, m, device=device).to(dtype)
                    self._test_addmm_addmv(torch.addmm, M, m1, m2)

    @onlyCUDA
    def test_matmul_45724(self, device):
        # https://github.com/pytorch/pytorch/issues/45724
        a = torch.rand(65537, 22, 64).cuda().half()
        b = torch.rand(65537, 64, 22).cuda().half()
        c = torch.full((65537, 22, 22), math.nan, dtype=torch.half, device='cuda')
        cpu_result = torch.matmul(a.cpu().float(), b.cpu().float()).cuda().half()
        torch.matmul(a, b, out=c)
        self.assertEqual(c, cpu_result)

    def _test_dot_vdot_vs_numpy(self, device, dtype, torch_fn, np_fn):
        def compare_with_numpy_bin_op(torch_fn, np_fn, x, y):
            y_np = y.cpu().numpy()

            # `compare_with_numpy` takes care of moving `x` to correct device for calling np_fn.
            self.compare_with_numpy(lambda inp: torch_fn(inp, y), lambda inp: np_fn(inp, y_np), x)

        # Use this tensor for out variant tests.
        out = torch.randn((), dtype=dtype, device=device)

        def compare_out_variant(torch_fn, x, y):
            torch_fn(v1, v2, out=out)
            self.assertEqual(torch_fn(v1, v2), out)

        for _ in range(10):
            numel = random.randint(10, 1000)
            v1 = torch.randn(numel, dtype=dtype, device=device)
            v2 = torch.randn(numel, dtype=dtype, device=device)
            compare_with_numpy_bin_op(torch_fn, np_fn, v1, v2)
            compare_out_variant(torch_fn, v1, v2)

            # Test 0-strided
            v3 = torch.randn(1, dtype=dtype, device=device).expand(numel)
            compare_with_numpy_bin_op(torch_fn, np_fn, v1, v3)
            compare_out_variant(torch_fn, v1, v3)

            compare_with_numpy_bin_op(torch_fn, np_fn, v3, v1)
            compare_out_variant(torch_fn, v3, v1)

            # Test stride greater than 1
            v4 = torch.randn(numel, numel, dtype=dtype, device=device)[:, numel - 1]
            compare_with_numpy_bin_op(torch_fn, np_fn, v1, v4)
            compare_out_variant(torch_fn, v1, v4)

            compare_with_numpy_bin_op(torch_fn, np_fn, v4, v1)
            compare_out_variant(torch_fn, v4, v1)

    @precisionOverride({torch.cfloat: 1e-4, torch.float32: 5e-5})
    @dtypes(torch.float, torch.double, torch.cfloat, torch.cdouble)
    def test_dot_vs_numpy(self, device, dtype):
        self._test_dot_vdot_vs_numpy(device, dtype, torch.dot, np.dot)

    @precisionOverride({torch.cfloat: 1e-4, torch.float32: 5e-5})
    @dtypes(torch.float, torch.double, torch.cfloat, torch.cdouble)
    def test_vdot_vs_numpy(self, device, dtype):
        self._test_dot_vdot_vs_numpy(device, dtype, torch.vdot, np.vdot)

    def _test_dot_vdot_invalid_args(self, device, torch_fn, complex_dtypes=False):
        if complex_dtypes:
            x = torch.randn(1, dtype=torch.cfloat, device=device)
            y = torch.randn(3, dtype=torch.cdouble, device=device)
        else:
            x = torch.randn(1, dtype=torch.float, device=device)
            y = torch.randn(3, dtype=torch.double, device=device)

        with self.assertRaisesRegex(RuntimeError,
                                    'dot : expected both vectors to have same dtype'):
            torch_fn(x, y)

        with self.assertRaisesRegex(RuntimeError,
                                    '1D tensors expected'):
            torch_fn(x.reshape(1, 1), y)

        with self.assertRaisesRegex(RuntimeError,
                                    'inconsistent tensor size'):
            torch_fn(x.expand(9), y.to(x.dtype))

        if self.device_type != 'cpu':
            x_cpu = x.expand(3).cpu()

            with self.assertRaisesRegex(RuntimeError,
                                        'expected all tensors to be on the same device'):
                torch_fn(x_cpu, y.to(x.dtype))

    @onlyOnCPUAndCUDA
    def test_vdot_invalid_args(self, device):
        self._test_dot_vdot_invalid_args(device, torch.vdot)
        self._test_dot_vdot_invalid_args(device, torch.vdot, complex_dtypes=True)

    @onlyOnCPUAndCUDA
    def test_dot_invalid_args(self, device):
        self._test_dot_vdot_invalid_args(device, torch.dot)
        self._test_dot_vdot_invalid_args(device, torch.dot, complex_dtypes=True)

    @onlyCPU
    @slowTest
    @dtypes(torch.float)
    def test_exp_slow(self, device, dtype):
        # Test for https://github.com/pytorch/pytorch/issues/17271
        # This is pretty slow on my Macbook but it only takes a few
        # seconds on a beefy Xeon server
        a = torch.exp(torch.ones(2 ** 31, dtype=dtype, device=device))
        b = torch.exp(torch.ones(1, dtype=dtype, device=device))
        self.assertEqual(a, b.expand(2 ** 31))

    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_hardswish(self, device, dtype):
        inputValues = [-1000, -4, -3, -2, 0, 2, 3, 4, 1000]
        expectedOutput = np.multiply(
            inputValues,
            np.minimum(np.maximum((np.add(inputValues, 3)), 0), 6) / 6.0)
        precision_4dps = 0.0002

        inputTensor = torch.tensor(inputValues, dtype=dtype, device=device)
        expectedOutputTensor = \
            torch.tensor(expectedOutput, dtype=dtype, device=device)

        # normal
        self.assertEqual(torch.nn.functional.hardswish(inputTensor),
                         expectedOutputTensor,
                         atol=precision_4dps, rtol=0)

        # inplace
        inputTensorCpy = inputTensor.clone().detach()
        torch.nn.functional.hardswish(inputTensorCpy, inplace=True)
        self.assertEqual(inputTensorCpy, expectedOutputTensor,
                         atol=precision_4dps, rtol=0)

    @onlyCPU
    @dtypes(torch.float, torch.double)
    def test_sigmoid(self, device, dtype):
        # TODO: why not simulate math.sigmoid like with rsqrt?
        inputValues = [-1000, -1, 0, 0.5, 1, 2, 1000]
        expectedOutput = [0.0000, 0.2689, 0.5, 0.6225, 0.7311, 0.8808, 1.000]
        precision_4dps = 0.0002

        self.assertEqual(torch.tensor(inputValues, dtype=dtype, device=device).sigmoid(),
                         torch.tensor(expectedOutput, dtype=dtype, device=device),
                         atol=precision_4dps, rtol=0)

    @dtypes(torch.float, torch.double)
    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    def test_hardsigmoid(self, device, dtype):
        inputValues = [-1000, -4, -3, -2, 0, 2, 3, 4, 1000]
        expectedOutput = np.minimum(np.maximum((np.add(inputValues, 3)), 0), 6) / 6.0

        inputTensor = torch.tensor(inputValues, dtype=dtype, device=device)
        precision_4dps = 0.0002

        # normal
        self.assertEqual(torch.nn.functional.hardsigmoid(inputTensor),
                         torch.tensor(expectedOutput, dtype=dtype, device=device),
                         atol=precision_4dps, rtol=0)

        # inplace
        inputTensorCpy = inputTensor.clone().detach()
        self.assertEqual(torch.nn.functional.hardsigmoid(inputTensorCpy, inplace=True),
                         torch.tensor(expectedOutput, dtype=dtype, device=device),
                         atol=precision_4dps, rtol=0)

    @skipIfNoSciPy
    @dtypes(torch.float, torch.double)
    def test_silu(self, device, dtype):
        input_np = np.random.randn(5, 8)
        special_input = [[-1000, -1, -0.1, 0, 0.5, 1, 2, 1000]]
        input_np = np.concatenate((input_np, special_input), axis=0).astype(
            torch_to_numpy_dtype_dict[dtype])
        expected_output_np = input_np * scipy.special.expit(input_np)

        expected_output = torch.from_numpy(expected_output_np).to(device)
        expected_output_noncontig = expected_output.transpose(0, 1)

        atol = 1e-6
        rtol = 1e-6

        input = torch.from_numpy(input_np).clone().contiguous().to(device)
        self.assertEqual(torch.nn.functional.silu(input), expected_output,
                         atol=atol, rtol=rtol)
        self.assertEqual(torch.nn.functional.silu(input, inplace=True),
                         expected_output, atol=atol, rtol=rtol)

        input = torch.from_numpy(input_np).clone().to(device)
        input_noncontig = input.transpose(0, 1)
        self.assertEqual(torch.nn.functional.silu(input_noncontig),
                         expected_output_noncontig, atol=atol, rtol=rtol)
        self.assertEqual(torch.nn.functional.silu(
            input_noncontig, inplace=True), expected_output_noncontig,
            atol=atol, rtol=rtol)


    @onlyCPU
    @dtypes(torch.float)
    def test_diag_embed(self, device, dtype):
        x = torch.arange(3 * 4, dtype=dtype, device=device).view(3, 4)
        result = torch.diag_embed(x)
        expected = torch.stack([torch.diag(r) for r in x], 0)
        self.assertEqual(result, expected)

        result = torch.diag_embed(x, offset=1, dim1=0, dim2=2)
        expected = torch.stack([torch.diag(r, 1) for r in x], 1)
        self.assertEqual(result, expected)

    @onlyCPU
    @dtypes(*torch.testing.get_all_dtypes())
    def test_sub(self, device, dtype):
        m1 = torch.tensor([2.34, 4.44], dtype=dtype, device=device)
        m2 = torch.tensor([1.23, 2.33], dtype=dtype, device=device)

        if dtype == torch.bool:
            self.assertRaises(RuntimeError, lambda: m1 - m2)
        elif (dtype == torch.bfloat16 or dtype == torch.half):
            # bfloat16 has a lower precision so we have to have a separate check for it
            self.assertEqual(m1 - m2, torch.tensor([1.11, 2.11], dtype=dtype), atol=0.01, rtol=0)
        else:
            self.assertEqual(m1 - m2, torch.tensor([1.11, 2.11], dtype=dtype))

    @onlyCPU
    @dtypes(torch.float)
    def test_csub(self, device, dtype):
        # with a tensor
        a = torch.randn(100, 90, dtype=dtype, device=device)
        b = a.clone().normal_()

        res_add = torch.add(a, b, alpha=-1)
        res_csub = a.clone()
        res_csub.sub_(b)
        self.assertEqual(res_add, res_csub)

        # with a scalar
        a = torch.randn(100, 100, dtype=dtype, device=device)

        scalar = 123.5
        res_add = torch.add(a, -scalar)
        res_csub = a.clone()
        res_csub.sub_(scalar)
        self.assertEqual(res_add, res_csub)

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_min_max_binary_op_nan(self, device, dtype):
        a = torch.rand(1000, dtype=dtype, device=device)
        b = torch.rand(1000, dtype=dtype, device=device)

        # 0:250: a -- nan, b -- not nan
        a[:250] = float('nan')
        # 250:500: a -- not nan, b -- nan
        b[250:500] = float('nan')
        # 500:750: a and b both nan
        a[500:750] = float('nan')
        b[500:750] = float('nan')
        # 750:1000: neither nan

        ma = torch.max(a, b)
        mi = torch.min(a, b)

        for i in range(750):
            self.assertTrue(torch.isnan(ma[i]), "max(a, b): {}, a: {}, b: {}".format(ma[i], a[i], b[i]))
            self.assertTrue(torch.isnan(mi[i]), "min(a, b): {}, a: {}, b: {}".format(mi[i], a[i], b[i]))

        for i in range(750, 1000):
            self.assertFalse(torch.isnan(ma[i]), "max(a, b): {}, a: {}, b: {}".format(ma[i], a[i], b[i]))
            self.assertFalse(torch.isnan(mi[i]), "min(a, b): {}, a: {}, b: {}".format(mi[i], a[i], b[i]))

    @onlyCPU
    @dtypes(*torch.testing.get_all_math_dtypes('cpu'))
    def test_threshold(self, device, dtype):
        if dtype != torch.uint8 and dtype != torch.float16 and not dtype.is_complex:
            # 100 is wide enough to use AVX2 instructions for all types
            x = torch.randn(100, dtype=torch.float, device=device).sign().to(dtype=dtype)
            y = torch.threshold(x, 0, 0)
            self.assertTrue(y.le(0).any())

    @onlyCPU
    @dtypes(torch.float, torch.double)
    def test_reciprocal(self, device, dtype):
        a = torch.randn(100, 89, device=device, dtype=dtype)
        res_div = 1 / a
        res_reciprocal = a.clone()
        res_reciprocal.reciprocal_()
        self.assertEqual(res_reciprocal, res_div)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.float, torch.double, torch.complex64, torch.complex128)
    def test_reciprocal_complex(self, device, dtype):
        t = torch.randn(10, 10, dtype=dtype, device=device)
        expected = torch.from_numpy(np.reciprocal(t.cpu().numpy()))
        actual = torch.reciprocal(t).cpu()
        self.assertEqual(expected, actual)

    @onlyCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.complex64, torch.complex128)
    def test_reciprocal_complex_extremal(self, device, dtype):
        vals = (
            # Inf and Zeros
            complex(float('inf'), float('inf')),
            complex(float('inf'), 0.),
            complex(0., float('inf')),
            complex(0., 0.),

            # Nans and Zeros
            complex(float('nan'), 0.),
            complex(0., float('nan')),
            complex(float('nan'), float('nan')),

            # Inf and Nans
            complex(float('nan'), float('inf')),
            complex(float('inf'), float('nan')),

            # Extremal and Normal Number
            complex(float('nan'), 2.0),
            complex(float('inf'), 2.0),
            complex(2.0, float('nan')),
            complex(2.0, float('inf')),
            complex(2.0, 0.0),
            complex(0.0, 2.0))

        self.compare_with_numpy(torch.reciprocal, np.reciprocal, vals, device, dtype)

    @dtypes(torch.bfloat16, torch.float)
    def test_div(self, device, dtype):
        for op, method, inplace in ((torch.div, torch.Tensor.div, torch.Tensor.div_),
                                    (torch.true_divide, torch.Tensor.true_divide,
                                     torch.Tensor.true_divide_)):
            m1 = torch.randn(10, 10, dtype=torch.float, device=device).to(dtype=dtype)
            res1 = m1.clone()
            inplace(res1[:, 3], 2)
            res2 = m1.clone()
            for i in range(m1.size(0)):
                res2[i, 3] = res2[i, 3] / 2
            self.assertEqual(res1, res2)

            if dtype == torch.bfloat16:
                a1 = torch.tensor([4.2, 6.2], dtype=dtype, device=device)
                a2 = torch.tensor([2., 2.], dtype=dtype, device=device)
                self.assertEqual(op(a1, a2),
                                 torch.tensor([2.1, 3.1], dtype=dtype, device=device),
                                 atol=0.01, rtol=0)
                self.assertEqual(method(a1, a2), op(a1, a2))

    @dtypes(torch.bfloat16, torch.float)
    def test_true_divide_out(self, device, dtype):
        a1 = torch.tensor([4.2, 6.2], dtype=dtype, device=device)
        a2 = torch.tensor([2., 2.], dtype=dtype, device=device)
        res = torch.empty_like(a1)
        self.assertEqual(torch.true_divide(a1, a2, out=res),
                         torch.tensor([2.1, 3.1], dtype=dtype, device=device),
                         atol=0.01, rtol=0)

    @onlyCUDA
    @dtypes(torch.half)
    def test_divmul_scalar(self, device, dtype):
        x = torch.tensor(100., device=device, dtype=dtype)
        x_ref = x.float()
        scale = 1e5
        res = x.div(scale)
        expected = x_ref.div(scale)
        self.assertEqual(res, expected.to(dtype), atol=0., rtol=0.)
        x = torch.tensor(1e-5, device=device, dtype=dtype)
        x_ref = x.float()
        res = x.mul(scale)
        expected = x_ref.mul(scale)
        self.assertEqual(res, expected.to(dtype), atol=0., rtol=0.)
        res = scale * x
        self.assertEqual(res, expected.to(dtype), atol=0., rtol=0.)

    @dtypesIfCUDA(*set(torch.testing.get_all_math_dtypes('cuda')) - {torch.complex64, torch.complex128})
    @dtypes(*set(torch.testing.get_all_math_dtypes('cpu')) - {torch.complex64, torch.complex128})
    def test_floor_divide_tensor(self, device, dtype):
        x = torch.randn(10, device=device).mul(30).to(dtype)
        y = torch.arange(1, 11, dtype=dtype, device=device)

        z = x // y
        z_alt = torch.trunc(x.double() / y.double()).to(dtype)

        self.assertEqual(z.dtype, x.dtype)
        self.assertEqual(z, z_alt)

    @dtypesIfCUDA(*set(torch.testing.get_all_math_dtypes('cuda')) - {torch.complex64, torch.complex128})
    @dtypes(*set(torch.testing.get_all_math_dtypes('cpu')) - {torch.complex64, torch.complex128})
    def test_floor_divide_scalar(self, device, dtype):
        x = torch.randn(100, device=device).mul(10).to(dtype)

        z = x // 3
        z_alt = torch.tensor([math.trunc(v.item() / 3.) for v in x], dtype=x.dtype, device=device)

        self.assertEqual(z.dtype, x.dtype)
        self.assertEqual(z, z_alt)

    # Note: this tests fails on XLA
    @onlyOnCPUAndCUDA
    @dtypes(torch.float, torch.long)
    def test_floor_divide_out(self, device, dtype):
        x = torch.randn(10, device=device).mul(10).to(dtype)
        y = torch.arange(1, 11, dtype=dtype, device=device)
        o = torch.empty(10, dtype=dtype, device=device)

        torch.floor_divide(x, y, out=o)
        self.assertEqual(o, x // y)

        # Tests scalar with out
        torch.floor_divide(x, 2, out=o)
        self.assertEqual(o, x // 2)

        if dtype == torch.int:
            o = torch.empty(10, dtype=torch.float, device=device)
            torch.floor_divide(x, y, out=o)
            self.assertEqual(o, torch.floor_divide(x.float(), y.float()))

    @onlyCPU
    @dtypes(*torch.testing.get_all_math_dtypes('cpu'))
    def test_rdiv(self, device, dtype):
        if dtype is torch.float16:
            return
        elif dtype.is_complex:
            x = torch.rand(100, dtype=dtype, device=device).add(1).mul(4)
        else:
            x = torch.rand(100, device=device).add(1).mul(4).to(dtype)
        y = 30 / x
        z = torch.tensor([30 / v.item() for v in x], device=device)
        self.assertEqual(y, z, exact_dtype=False)

    @onlyCPU
    @dtypes(*torch.testing.get_all_dtypes(include_bfloat16=False, include_bool=False, include_complex=False))
    def test_fmod(self, device, dtype):
        m1 = torch.Tensor(10, 10).uniform_(-10., 10.).to(dtype=dtype, device=device)
        res1 = m1.clone()
        q = 3
        res1[:, 3].fmod_(q)
        res2 = m1.clone()
        for i in range(m1.size(1)):
            res2[i, 3] = math.fmod(res2[i, 3], q)
        self.assertEqual(res1, res2)

        zero = torch.zeros_like(m1)
        if dtype in torch.testing.get_all_int_dtypes():
            with self.assertRaisesRegex(RuntimeError, "ZeroDivisionError"):
                m1.fmod(0)
            with self.assertRaisesRegex(RuntimeError, "ZeroDivisionError"):
                m1.fmod(zero)
        else:
            self.assertTrue(torch.all(m1.fmod(0).isnan()))
            self.assertTrue(torch.all(m1.fmod(zero).isnan()))

    @onlyCPU
    @dtypes(torch.float, torch.long)
    def test_remainder(self, device, dtype):
        for use_item in [True, False]:
            if dtype == torch.float:
                m1 = torch.Tensor(10, 10).uniform_(-10., 10.).to(dtype=dtype, device=device)
                res1 = m1.clone()
                res2 = m1.clone()
                qs = torch.arange(-5.1, 4.1, dtype=dtype, device=device)
                # Check the case where the divisor is a simple float
                for col_idx, q in enumerate(qs):
                    # Reference
                    for i in range(m1.size(0)):
                        res2[i, col_idx] = res2[i, col_idx] % q
                    # To test
                    res1[:, col_idx].remainder_(q if not use_item else q.item())
                self.assertEqual(res1, res2)
                # Check the case where the divisor is a tensor
                res1 = m1.clone()
                res1.remainder_(qs.unsqueeze(0).expand_as(res1))
                self.assertEqual(res1, res2)
            elif dtype == torch.long:
                long_m1 = torch.LongTensor(10, 10).random_(-10, 10)
                long_res1 = long_m1.clone()
                long_res2 = long_m1.clone()
                long_qs = torch.arange(-5, 5, dtype=dtype, device=device)
                long_qs[5] = 5  # Can't handle the divisor=0 case
                for col_idx, long_q in enumerate(long_qs):
                    # Reference
                    for i in range(long_m1.size(0)):
                        long_res2[i, col_idx] = long_res2[i, col_idx] % long_q
                    # To test
                    long_res1[:, col_idx].remainder_(long_q if not use_item else long_q.item())
                self.assertEqual(long_res1, long_res2)
                # Divisor is a tensor case
                long_res1 = long_m1.clone()
                long_res1.remainder_(long_qs.unsqueeze(0).expand_as(long_res1))

    @dtypes(torch.float, torch.double)
    def test_remainder_fmod_large_dividend(self, device, dtype):
        alarge = 1e9
        pi = 3.14159265358979
        for avalue in [alarge, -alarge]:
            for bvalue in [pi, -pi]:
                a = torch.tensor([avalue], dtype=dtype, device=device)
                b = torch.tensor([bvalue], dtype=dtype, device=device)
                c = torch.remainder(a, b)
                d = torch.fmod(a, b)
                self.assertTrue((b[0] > 0) == (c[0] > 0))  # remainder has same sign as divisor
                self.assertTrue((a[0] > 0) == (d[0] > 0))  # fmod has same sign as dividend
                self.assertTrue(abs(c[0]) < abs(b[0]))     # remainder is within range of divisor
                self.assertTrue(abs(d[0]) < abs(b[0]))     # fmod is within range of divisor
                if ((a[0] > 0) == (b[0] > 0)):
                    self.assertTrue(c[0] == d[0])   # remainder is same as fmod
                else:
                    self.assertTrue(abs(c[0] - d[0]) == abs(b[0]))  # differ by one divisor

    @dtypesIfCPU(torch.bfloat16, torch.float32, torch.float64)
    @dtypes(torch.float32, torch.float64)
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_hypot(self, device, dtype):
        inputs = [
            (torch.randn(10, device=device).to(dtype), torch.randn(10, device=device).to(dtype)),
            (torch.randn((3, 3, 3), device=device).to(dtype), torch.randn((3, 3, 3), device=device).to(dtype)),
            (torch.randn((10, 1), device=device).to(dtype), torch.randn((10, 1), device=device).to(dtype).transpose(0, 1)),
            (torch.randint(100, (10, ), device=device, dtype=torch.long), torch.randn(10, device=device).to(dtype))
        ]
        for input in inputs:
            actual = torch.hypot(input[0], input[1])
            if dtype == torch.bfloat16:
                expected = torch.sqrt(input[0] * input[0] + input[1] * input[1])
            else:
                expected = np.hypot(input[0].cpu().numpy(), input[1].cpu().numpy())
            self.assertEqual(actual, expected)

    @dtypes(torch.int64, torch.float64)
    def test_remainder_edge_cases(self, device, dtype):
        # Test variations of negative values used as input
        a = torch.tensor([6, -6, -6, 6, 27, -27, -27, 27], dtype=dtype, device=device)
        b = torch.tensor([-3, 3, -3, 3, -5, 5, -5, 5], dtype=dtype, device=device)
        r = a.remainder(b)
        r_expected = torch.tensor([0, 0, 0, 0, -3, 3, -2, 2], dtype=dtype, device=device)
        self.assertEqual(r, r_expected)

        if dtype == torch.float64:
            # Test cases where result should be nan
            a = torch.tensor([-34, 0, 34], dtype=dtype, device=device)
            b = torch.zeros(3, dtype=dtype, device=device)
            self.assertTrue(torch.isnan(a.remainder(b)).all())

            # Need to test a fairly large tensor with float cpu to run
            # the Vec256 implementation
            if device == 'cpu':
                a = torch.tensor([6, -6, -6, 6, 27, -27, -27, 27] * 10000, dtype=dtype, device=device)
                b = torch.tensor([-3, 3, -3, 3, -5, 5, -5, 5] * 10000, dtype=dtype, device=device)
                r = a.remainder(b)
                r_expected = torch.tensor([0, 0, 0, 0, -3, 3, -2, 2] * 10000, dtype=dtype, device=device)
                self.assertEqual(r, r_expected)

                # Test nan cases
                a = torch.tensor([-34, 0, 34] * 20000, dtype=dtype, device=device)
                b = torch.zeros(3 * 20000, dtype=dtype, device=device)
                self.assertTrue(torch.isnan(a.remainder(b)).all())

        elif dtype == torch.int64:
            if device == 'cpu':
                # Test int divide by zero causes an exception
                a = torch.ones(1000, dtype=dtype, device=device)
                b = torch.ones(1000, dtype=dtype, device=device)
                b[500] = 0
                self.assertRaises(RuntimeError, lambda: a.remainder(b))

        # Check scalar type is promoted to match tensor
        a = torch.ones(1, dtype=dtype, device=device)
        b = 1.0 if dtype == torch.int64 else 1
        r = a.remainder(b)
        self.assertEqual(r.dtype, a.dtype)

    @onlyOnCPUAndCUDA
    @dtypes(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_gcd(self, device, dtype):
        # Tests gcd(0, 0), gcd(0, a) cases
        t1 = torch.tensor([0, 10, 0], dtype=dtype, device=device)
        t2 = torch.tensor([0, 0, 10], dtype=dtype, device=device)
        actual = torch.gcd(t1, t2)
        expected = np.gcd([0, 10, 0], [0, 0, 10])
        self.assertEqual(actual, expected)

        if dtype == torch.uint8:
            # Test unsigned integers with potential sign issues (i.e., uint8 with value >= 128)
            a = torch.tensor([190, 210], device=device, dtype=dtype)
            b = torch.tensor([190, 220], device=device, dtype=dtype)
            actual = torch.gcd(a, b)
            expected = torch.tensor([190, 10], device=device, dtype=dtype)
        else:
            # Compares with NumPy
            a = torch.randint(-20, 20, (1024,), device=device, dtype=dtype)
            b = torch.randint(-20, 20, (1024,), device=device, dtype=dtype)
            actual = torch.gcd(a, b)
            expected = np.gcd(a.cpu().numpy(), b.cpu().numpy())
            self.assertEqual(actual, expected)

    @onlyOnCPUAndCUDA
    @dtypes(torch.int16, torch.int32, torch.int64)
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_lcm(self, device, dtype):
        # Tests lcm(0, 0), lcm(0, a) cases
        t1 = torch.tensor([0, 10, 0], dtype=dtype, device=device)
        t2 = torch.tensor([0, 0, 10], dtype=dtype, device=device)
        actual = torch.lcm(t1, t2)
        expected = np.lcm([0, 10, 0], [0, 0, 10])
        self.assertEqual(actual, expected)

        # Compares with NumPy
        a = torch.randint(-20, 20, (1024,), device=device, dtype=dtype)
        b = torch.randint(-20, 20, (1024,), device=device, dtype=dtype)
        actual = torch.lcm(a, b)
        expected = np.lcm(a.cpu().numpy(), b.cpu().numpy())
        self.assertEqual(actual, expected)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_nextafter(self, device, dtype):
        # Test special cases
        t1 = torch.tensor([0, 0, 10], device=device, dtype=dtype)
        t2 = torch.tensor([inf, -inf, 10], device=device, dtype=dtype)
        actual = torch.nextafter(t1, t2)
        expected = np.nextafter(t1.cpu().numpy(), t2.cpu().numpy())
        self.assertEqual(actual, expected, atol=0, rtol=0)

        actual = torch.nextafter(t2, t1)
        expected = np.nextafter(t2.cpu().numpy(), t1.cpu().numpy())
        self.assertEqual(actual, expected, atol=0, rtol=0)

        t1 = torch.tensor([0, nan], device=device, dtype=dtype)
        t2 = torch.tensor([nan, 0], device=device, dtype=dtype)
        self.assertTrue(torch.nextafter(t1, t2).isnan().all())

        a = torch.randn(100, device=device, dtype=dtype)
        b = torch.randn(100, device=device, dtype=dtype)
        actual = torch.nextafter(a, b)
        expected = np.nextafter(a.cpu().numpy(), b.cpu().numpy())
        self.assertEqual(actual, expected, atol=0, rtol=0)

    def _i0_helper(self, t):
        # Test by comparing to scipy
        dtype = t.dtype
        actual = torch.i0(t)
        if dtype is torch.bfloat16:
            t = t.to(torch.float32)
        expected = scipy.special.i0(t.cpu().numpy())
        # Casting down for dtype float16 is required since scipy upcasts to float32
        if dtype is torch.bfloat16 or dtype is torch.float16:
            expected = torch.from_numpy(expected).to(dtype)
        self.assertEqual(actual, expected)

    def _i0_range_helper(self, range, device, dtype):
        # i0 tests are broken up by the domain for which the function does not overflow for each dtype
        # This is done to ensure that the function performs well across all possible input values, without worrying
        # about inf or nan possibilities
        for r in (range, -range):
            t = torch.rand(1000, device=device).to(dtype) * r
            self._i0_helper(t)

    @dtypesIfCUDA(*torch.testing.get_all_fp_dtypes())
    @dtypes(torch.bfloat16, torch.float32, torch.float64)
    @unittest.skipIf(not TEST_SCIPY, "SciPy not found")
    def test_i0_range1(self, device, dtype):
        # This tests the domain for i0 for which float16 does not overflow
        # The domain is (-13.25, 13.25)
        self._i0_range_helper(13.25, device, dtype)

    @dtypesIfCUDA(*torch.testing.get_all_fp_dtypes())
    @dtypes(torch.bfloat16, torch.float32, torch.float64)
    @unittest.skipIf(not TEST_SCIPY, "SciPy not found")
    def test_i0_range2(self, device, dtype):
        # This tests the domain for i0 for which float32 and bfloat16 does not overflow
        # The domain is (-88.5, 88.5)
        self._i0_range_helper(88.5, device, dtype)

    @dtypes(torch.float64)
    @unittest.skipIf(not TEST_SCIPY, "SciPy not found")
    def test_i0_range3(self, device, dtype):
        # This tests the domain for i0 for which float64 does not overflow
        # The domain is (-709.75, 709.75)
        self._i0_range_helper(709.75, device, dtype)

    @dtypesIfCUDA(*torch.testing.get_all_fp_dtypes())
    @dtypes(torch.bfloat16, torch.float32, torch.float64)
    @unittest.skipIf(not TEST_SCIPY, "SciPy not found")
    def test_i0_special(self, device, dtype):
        t = torch.tensor([], device=device, dtype=dtype)
        self._i0_helper(t)

        t = torch.tensor([inf, -inf, nan], device=device, dtype=dtype)
        self.assertTrue(torch.i0(t).isnan().all())

    @slowTest
    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64, torch.bfloat16, torch.int32, torch.int64, torch.cfloat, torch.cdouble)
    @dtypesIfCUDA(torch.float32, torch.float64, torch.cfloat, torch.cdouble)
    @tf32_on_and_off(0.01)
    def test_mm(self, device, dtype):
        def _test_mm(n, m, p, dtype, genf):
            # helper function
            def matrixmultiply(mat1, mat2):
                n = mat1.size(0)
                m = mat1.size(1)
                p = mat2.size(1)
                res = torch.zeros(n, p, dtype=dtype, device=device)
                for i, j in iter_indices(res):
                    res[i, j] = sum(mat1[i, k] * mat2[k, j] for k in range(m))
                return res

            # contiguous case
            mat1 = genf(n, m)
            mat2 = genf(m, p)
            res = torch.mm(mat1, mat2)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # non contiguous case 1
            mat1 = genf(n, m)
            mat2 = genf(p, m).t()
            res = torch.mm(mat1, mat2)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # non contiguous case 2
            mat1 = genf(m, n).t()
            mat2 = genf(m, p)
            res = torch.mm(mat1, mat2)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # non contiguous case 3
            mat1 = genf(m, n).t()
            mat2 = genf(p, m).t()
            res = torch.mm(mat1, mat2)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # test with zero stride
            mat1 = genf(n, m)
            mat2 = genf(m, 1).expand(m, p)
            res = torch.mm(mat1, mat2)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # explicitly exercise the _out variant in torch.mm().
            # contiguous case
            mat1 = genf(n, m)
            mat2 = genf(m, p)
            res = genf(n, p)
            torch.mm(mat1, mat2, out=res)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

            # explicitly exercise the _out variant in torch.mm().
            # non contiguous case 3
            mat1 = genf(m, n).t()
            mat2 = genf(p, m).t()
            res = genf(n, p)
            torch.mm(mat1, mat2, out=res)

            res2 = matrixmultiply(mat1, mat2)
            self.assertEqual(res, res2)

        def genf_int(x, y):
            return torch.randint(0, 100, (x, y), dtype=dtype, device=device)

        def genf_bfloat(x, y):
            return torch.randn(x, y, dtype=torch.float32, device=device).to(dtype)

        def genf_float(x, y):
            return torch.randn(x, y, dtype=dtype, device=device)

        for (n, m, p) in [(20, 10, 5), (15, 5, 10), (5, 18, 10)]:
            if (dtype == torch.int32) or (dtype == torch.int64):
                genf = genf_int
            elif (dtype == torch.bfloat16):
                genf = genf_bfloat
            else:
                genf = genf_float

            _test_mm(n, m, p, dtype, genf)

    @onlyOnCPUAndCUDA
    @dtypes(torch.float32, torch.float64)
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_strided_mm_bmm(self, device, dtype):
        # Tests strided view case with stride smaller than corresponding dimension size
        x = torch.tensor([[1., 2., 3.], [4., 5., 6.]], dtype=dtype, device=device)
        new_shape = [2, 2, 2]
        new_stride = [3, 1, 1]
        sx = torch.as_strided(x, size=new_shape, stride=new_stride)

        torch_fn = lambda x: torch.bmm(x, x)  # noqa: E731
        np_fn = lambda x: np.matmul(x, x)  # noqa: E731
        self.compare_with_numpy(torch_fn, np_fn, sx)

        torch_fn = lambda x: torch.mm(x, x)  # noqa: E731
        self.compare_with_numpy(torch_fn, np_fn, sx[0])

    @onlyCPU
    @dtypes(*(torch.testing.get_all_complex_dtypes() + [torch.float, torch.double]))
    def test_bmm(self, device, dtype):
        num_batches = 10
        M, N, O = 23, 8, 12
        b1 = torch.randn(num_batches, M, N, dtype=dtype, device=device)
        b2 = torch.randn(num_batches, N, O, dtype=dtype, device=device)
        res = torch.bmm(b1, b2)
        for i in range(num_batches):
            r = torch.mm(b1[i], b2[i])
            self.assertEqual(r, res[i])
        if torch.cuda.is_available():
            # check that mixed arguments are rejected
            self.assertRaises(RuntimeError, lambda: torch.bmm(b1, b2.cuda()))
            self.assertRaises(RuntimeError, lambda: torch.bmm(b1.cuda(), b2))

    @onlyCUDA
    @wrapDeterministicFlagAPITest
    def test_cublas_config_deterministic_error(self, device):
        test_cases = [
            # (function, (tensor sizes))
            ('mm', ((2, 2), (2, 2),)),
            ('mv', ((2, 2), (2,),)),
            ('bmm', ((1, 2, 2), (1, 2, 2),))]

        test_configs = [
            # (CuBLAS workspace config, is deterministic)
            ('garbage', False),
            (None, False),
            (':4096:8', True),
            (':16:8', True)]

        cublas_var_name = 'CUBLAS_WORKSPACE_CONFIG'
        is_cuda10_2_or_higher = (
            (torch.version.cuda is not None)
            and ([int(x) for x in torch.version.cuda.split(".")] >= [10, 2]))

        def test_case_info(fn_name, config):
            return f'function "{fn_name}" with config "{"" if config is None else config}"'

        # Create processes to test each combination of test cases and config settings
        processes = []
        for fn_name, arg_sizes in test_cases:
            for config, is_config_deterministic in test_configs:
                env = os.environ.copy()
                if config is None:
                    if env.get(cublas_var_name) is not None:
                        del env[cublas_var_name]
                else:
                    env[cublas_var_name] = config
                should_throw_error = is_cuda10_2_or_higher and not is_config_deterministic
                script = f"""
import torch
torch.set_deterministic(True)
fn = torch.{fn_name}
arg_sizes = {arg_sizes}
device = '{device}'
should_throw_error = {should_throw_error}
args = []
for arg_size in arg_sizes:
    args.append(torch.randn(*arg_size, device=device))
try:
    fn(*args)
except RuntimeError as e:
    if not should_throw_error:
        raise RuntimeError('Did not expect any error to be raised')
    elif 'Deterministic behavior was enabled with either' not in str(e):
        raise RuntimeError('Expected a CuBLAS nondeterministic error, but got a different error')
else:
    if should_throw_error:
        raise RuntimeError('Expected a CuBLAS nondeterministic error, but it was not raised')

"""
                try:
                    subprocess.check_output(
                        [sys.executable, '-c', script],
                        stderr=subprocess.STDOUT,
                        # On Windows, opening the subprocess with the default CWD makes `import torch`
                        # fail, so just set CWD to this script's directory
                        cwd=os.path.dirname(os.path.realpath(__file__)),
                        env=env)
                except subprocess.CalledProcessError as e:
                    self.fail(msg=(
                        f'Subprocess exception while attempting to run {test_case_info(fn_name, config)}:\n'
                        + e.output.decode("utf-8")))

    @onlyCPU
    @dtypes(*(torch.testing.get_all_complex_dtypes() + [torch.float, torch.double]))
    def test_addbmm(self, device, dtype):
        # num_batches = 10
        # M, N, O = 12, 8, 5
        num_batches = 2
        M, N, O = 2, 3, 4
        b1 = torch.randn(num_batches, M, N, dtype=dtype, device=device)
        b2 = torch.randn(num_batches, N, O, dtype=dtype, device=device)
        res = torch.bmm(b1, b2)
        res2 = torch.tensor((), dtype=dtype, device=device).resize_as_(res[0]).zero_()
        res3 = torch.tensor((), dtype=dtype, device=device).resize_as_(res[0]).zero_()

        res2.addbmm_(b1, b2)
        self.assertEqual(res2, res.sum(0, False))
        res3.copy_(res2)

        with self.maybeWarnsRegex(
                UserWarning, "This overload of addbmm_ is deprecated"):
            res2.addbmm_(1, b1, b2)
        self.assertEqual(res2, res.sum(0, False) * 2),
        res3.addbmm_(b1, b2, beta=1)
        self.assertEqual(res2, res3)

        with self.maybeWarnsRegex(
                UserWarning, "This overload of addbmm_ is deprecated"):
            res2.addbmm_(1., .5, b1, b2)
        self.assertEqual(res2, res.sum(0, False) * 2.5)
        res3.addbmm_(b1, b2, beta=1., alpha=.5)
        self.assertEqual(res2, res3)

        with self.maybeWarnsRegex(
                UserWarning, "This overload of addbmm is deprecated"):
            self.assertEqual(res2, torch.addbmm(1, res2, 0, b1, b2))

        res4 = torch.addbmm(res2, b1, b2, beta=1, alpha=.5)
        self.assertEqual(res4, res.sum(0, False) * 3),

        res5 = torch.addbmm(res2, b1, b2, beta=0, alpha=1)
        self.assertEqual(res5, res.sum(0, False))

        res6 = torch.addbmm(res2, b1, b2, beta=.1, alpha=.5)
        self.assertEqual(res6, res2 * .1 + .5 * res.sum(0)),

    @onlyCPU
    @dtypes(*(torch.testing.get_all_complex_dtypes() + [torch.float, torch.double]))
    def test_baddbmm(self, device, dtype):
        num_batches = 10
        M, N, O = 12, 8, 5
        b1 = torch.randn(num_batches, M, N, dtype=dtype, device=device)
        b2 = torch.randn(num_batches, N, O, dtype=dtype, device=device)
        res = torch.bmm(b1, b2)
        res2 = torch.tensor((), dtype=dtype, device=device).resize_as_(res).zero_()
        res3 = torch.tensor((), dtype=dtype, device=device).resize_as_(res).zero_()

        res2.baddbmm_(b1, b2)
        self.assertEqual(res2, res)
        res3.copy_(res2)

        with self.maybeWarnsRegex(
                UserWarning, "This overload of baddbmm_ is deprecated"):
            res2.baddbmm_(1, b1, b2)
        self.assertEqual(res2, res * 2)
        res3.baddbmm_(b1, b2, beta=1)
        self.assertEqual(res3, res2)

        with self.maybeWarnsRegex(
                UserWarning, "This overload of baddbmm_ is deprecated"):
            res2.baddbmm_(1, .5, b1, b2)
        self.assertEqual(res2, res * 2.5)
        res3.baddbmm_(b1, b2, beta=1, alpha=.5)
        self.assertEqual(res3, res2)


        with self.maybeWarnsRegex(
                UserWarning, "This overload of baddbmm is deprecated"):
            self.assertEqual(torch.baddbmm(1, res2, 0, b1, b2), res2)

        res4 = torch.baddbmm(res2, b1, b2, beta=1, alpha=.5)
        self.assertEqual(res4, res * 3, atol=2e-5, rtol=0)

        res5 = torch.baddbmm(res2, b1, b2, beta=0, alpha=1)
        self.assertEqual(res5, res)

        res6 = torch.baddbmm(res2, b1, b2, beta=.1, alpha=.5)
        self.assertEqual(res6, res2 * .1 + res * .5)

    def _test_cop(self, torchfn, mathfn, dtype, device):
        def reference_implementation(res2):
            for i, j in iter_indices(sm1):
                idx1d = i * sm1.size(0) + j
                res2[i, j] = mathfn(sm1[i, j], sm2[idx1d])
            return res2

        # contiguous
        m1 = torch.randn(10, 10, 10, dtype=dtype, device=device)
        m2 = torch.randn(10, 10 * 10, dtype=dtype, device=device)
        sm1 = m1[4]
        sm2 = m2[4]

        res1 = torchfn(sm1, sm2.view(10, 10))
        res2 = reference_implementation(res1.clone())
        self.assertEqual(res1, res2)

        # non-contiguous
        m1 = torch.randn(10, 10, 10, dtype=dtype, device=device)
        m2 = torch.randn(10 * 10, 10 * 10, dtype=dtype, device=device)
        sm1 = m1[:, 4]
        sm2 = m2[:, 4]
        # view as sm1.size()
        sm2.set_(sm2.storage(), sm2.storage_offset(), sm1.size(), (sm2.stride()[0] * 10, sm2.stride()[0]))
        res1 = torchfn(sm1, sm2)
        # reference_implementation assumes 1-d sm2
        sm2.set_(sm2.storage(), sm2.storage_offset(), m2[:, 4].size(), m2[:, 4].stride())
        res2 = reference_implementation(res1.clone())
        self.assertEqual(res1, res2)

    @onlyCPU
    @dtypes(torch.float)
    def test_cdiv(self, device, dtype):
        self._test_cop(torch.div, lambda x, y: x / y, dtype, device)

    @onlyCPU
    @dtypes(torch.float)
    def test_cfmod(self, device, dtype):
        self._test_cop(torch.fmod, math.fmod, dtype, device)

    @onlyCPU
    @dtypes(torch.float)
    def test_cremainder(self, device, dtype):
        self._test_cop(torch.remainder, lambda x, y: x % y, dtype, device)

    @onlyCPU
    @dtypes(torch.float)
    def test_cmul(self, device, dtype):
        self._test_cop(torch.mul, lambda x, y: x * y, dtype, device)

    @onlyCPU
    @dtypes(torch.float)
    def test_cpow(self, device, dtype):
        self._test_cop(torch.pow, lambda x, y: nan if x < 0 else math.pow(x, y), dtype, device)

    @onlyCUDA
    @dtypes(torch.float16, torch.float32)
    def test_prod_gpu(self, device, dtype):
        x = torch.tensor([2, 3, 6, 9, 8], dtype=dtype, device=device)

        # Check all combinations: fp16 input - fp16 output, fp16 input - fp32
        # output, fp32 input - fp16 output, fp32 input - fp32 output
        for dtype_output in [torch.float16, torch.float32]:
            result_expected = torch.tensor(2592, dtype=dtype_output, device=device)
            output = torch.prod(x, dtype=dtype_output)
            self.assertEqual(output, result_expected)

            output = x.prod(dtype=dtype_output)
            self.assertEqual(output, result_expected)

    @onlyCPU
    @dtypes(torch.float)
    def test_prod(self, device, dtype):
        x = torch.rand(100, 100, dtype=dtype, device=device)
        res1 = torch.prod(x, 1)
        res2 = torch.tensor((), dtype=dtype, device=device)
        torch.prod(x, 1, out=res2)
        self.assertEqual(res1, res2)

    @onlyCPU
    @dtypes(torch.float)
    def test_cross(self, device, dtype):
        x = torch.rand(100, 3, 100, dtype=dtype, device=device)
        y = torch.rand(100, 3, 100, dtype=dtype, device=device)
        res1 = torch.cross(x, y)
        res2 = torch.tensor((), dtype=dtype, device=device)
        torch.cross(x, y, out=res2)
        self.assertEqual(res1, res2)

    @onlyCPU
    @dtypes(torch.float)
    def test_cross_with_and_without_dim(self, device, dtype):
        x = torch.rand(100, 3, dtype=dtype, device=device)
        y = torch.rand(100, 3, dtype=dtype, device=device)
        res1 = torch.cross(x, y, dim=1)
        res2 = torch.cross(x, y, dim=-1)
        res3 = torch.cross(x, y)
        self.assertEqual(res1, res2)
        self.assertEqual(res1, res3)

    @dtypes(torch.float, torch.double, torch.int8, torch.int16, torch.int32, torch.int64)
    def test_random(self, device, dtype):
        # This test is flaky with p<=(2/(ub-lb))^200=6e-36
        t = torch.empty(200, dtype=dtype, device=device)
        lb = 1
        ub = 4

        t.fill_(-1)
        t.random_(lb, ub)
        self.assertEqual(t.min(), lb)
        self.assertEqual(t.max(), ub - 1)

        t.fill_(-1)
        t.random_(ub)
        self.assertEqual(t.min(), 0)
        self.assertEqual(t.max(), ub - 1)

    def test_random_bool(self, device):
        size = 2000
        t = torch.empty(size, dtype=torch.bool, device=device)

        t.fill_(False)
        t.random_()
        self.assertEqual(t.min(), False)
        self.assertEqual(t.max(), True)
        self.assertTrue(0.4 < (t.eq(True)).to(torch.int).sum().item() / size < 0.6)

        t.fill_(True)
        t.random_()
        self.assertEqual(t.min(), False)
        self.assertEqual(t.max(), True)
        self.assertTrue(0.4 < (t.eq(True)).to(torch.int).sum().item() / size < 0.6)

    def test_random_from_to_bool(self, device):
        size = 2000

        int64_min_val = torch.iinfo(torch.int64).min
        int64_max_val = torch.iinfo(torch.int64).max

        min_val = 0
        max_val = 1

        froms = [int64_min_val, -42, min_val - 1, min_val, max_val, max_val + 1, 42]
        tos = [-42, min_val - 1, min_val, max_val, max_val + 1, 42, int64_max_val]

        for from_ in froms:
            for to_ in tos:
                t = torch.empty(size, dtype=torch.bool, device=device)
                if to_ > from_:
                    if not (min_val <= from_ <= max_val):
                        self.assertRaisesRegex(
                            RuntimeError,
                            "from is out of bounds",
                            lambda: t.random_(from_, to_)
                        )
                    elif not (min_val <= (to_ - 1) <= max_val):
                        self.assertRaisesRegex(
                            RuntimeError,
                            "to - 1 is out of bounds",
                            lambda: t.random_(from_, to_)
                        )
                    else:
                        t.random_(from_, to_)
                        range_ = to_ - from_
                        delta = 1
                        self.assertTrue(from_ <= t.to(torch.int).min() < (from_ + delta))
                        self.assertTrue((to_ - delta) <= t.to(torch.int).max() < to_)
                else:
                    self.assertRaisesRegex(
                        RuntimeError,
                        "random_ expects 'from' to be less than 'to', but got from=" + str(from_) + " >= to=" + str(to_),
                        lambda: t.random_(from_, to_)
                    )

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_random_full_range(self, device, dtype):
        size = 2000
        alpha = 0.1

        int64_min_val = torch.iinfo(torch.int64).min
        int64_max_val = torch.iinfo(torch.int64).max

        if dtype == torch.double:
            fp_limit = 2**53
        elif dtype == torch.float:
            fp_limit = 2**24
        elif dtype == torch.half:
            fp_limit = 2**11
        elif dtype == torch.bfloat16:
            fp_limit = 2**8
        else:
            fp_limit = 0

        t = torch.empty(size, dtype=dtype, device=device)

        if dtype in [torch.float, torch.double, torch.half, torch.bfloat16]:
            from_ = int(max(-fp_limit, int64_min_val))
            to_inc_ = int(min(fp_limit, int64_max_val))
        else:
            from_ = int(max(torch.iinfo(dtype).min, int64_min_val))
            to_inc_ = int(min(torch.iinfo(dtype).max, int64_max_val))
        range_ = to_inc_ - from_ + 1

        t.random_(from_, None)
        delta = max(1, alpha * range_)
        self.assertTrue(from_ <= t.to(torch.double).min() < (from_ + delta))
        self.assertTrue((to_inc_ - delta) < t.to(torch.double).max() <= to_inc_)

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_random_from_to(self, device, dtype):
        size = 2000
        alpha = 0.1

        int64_min_val = torch.iinfo(torch.int64).min
        int64_max_val = torch.iinfo(torch.int64).max

        if dtype in [torch.float, torch.double, torch.half]:
            min_val = int(max(torch.finfo(dtype).min, int64_min_val))
            max_val = int(min(torch.finfo(dtype).max, int64_max_val))
            froms = [min_val, -42, 0, 42]
            tos = [-42, 0, 42, max_val >> 1]
        elif dtype == torch.bfloat16:
            min_val = int64_min_val
            max_val = int64_max_val
            froms = [min_val, -42, 0, 42]
            tos = [-42, 0, 42, max_val >> 1]
        elif dtype == torch.uint8:
            min_val = torch.iinfo(dtype).min
            max_val = torch.iinfo(dtype).max
            froms = [int64_min_val, -42, min_val - 1, min_val, 42, max_val, max_val + 1]
            tos = [-42, min_val - 1, min_val, 42, max_val, max_val + 1, int64_max_val]
        elif dtype == torch.int64:
            min_val = int64_min_val
            max_val = int64_max_val
            froms = [min_val, -42, 0, 42]
            tos = [-42, 0, 42, max_val]
        else:
            min_val = torch.iinfo(dtype).min
            max_val = torch.iinfo(dtype).max
            froms = [int64_min_val, min_val - 1, min_val, -42, 0, 42, max_val, max_val + 1]
            tos = [min_val - 1, min_val, -42, 0, 42, max_val, max_val + 1, int64_max_val]

        if dtype == torch.double:
            fp_limit = 2**53
        elif dtype == torch.float:
            fp_limit = 2**24
        elif dtype == torch.half:
            fp_limit = 2**11
        elif dtype == torch.bfloat16:
            fp_limit = 2**8
        else:
            fp_limit = 0

        for from_ in froms:
            for to_ in tos:
                t = torch.empty(size, dtype=dtype, device=device)
                if to_ > from_:
                    if not (min_val <= from_ <= max_val):
                        self.assertRaisesRegex(
                            RuntimeError,
                            "from is out of bounds",
                            lambda: t.random_(from_, to_)
                        )
                    elif not (min_val <= (to_ - 1) <= max_val):
                        self.assertRaisesRegex(
                            RuntimeError,
                            "to - 1 is out of bounds",
                            lambda: t.random_(from_, to_)
                        )
                    else:
                        if dtype.is_floating_point and (
                                not (-fp_limit <= from_ <= fp_limit) or not (-fp_limit <= (to_ - 1) <= fp_limit)):
                            if not (-fp_limit <= from_ <= fp_limit):
                                self.assertWarnsRegex(UserWarning, "from is out of bounds",
                                                      lambda: t.random_(from_, to_))
                            if not (-fp_limit <= (to_ - 1) <= fp_limit):
                                self.assertWarnsRegex(UserWarning, "to - 1 is out of bounds",
                                                      lambda: t.random_(from_, to_))
                        else:
                            t.random_(from_, to_)
                            range_ = to_ - from_
                            delta = max(1, alpha * range_)
                            if dtype == torch.bfloat16:
                                # Less strict checks because of rounding errors
                                # TODO investigate rounding errors
                                self.assertTrue(from_ <= t.to(torch.double).min() < (from_ + delta))
                                self.assertTrue((to_ - delta) < t.to(torch.double).max() <= to_)
                            else:
                                self.assertTrue(from_ <= t.to(torch.double).min() < (from_ + delta))
                                self.assertTrue((to_ - delta) <= t.to(torch.double).max() < to_)
                else:
                    self.assertRaisesRegex(
                        RuntimeError,
                        "random_ expects 'from' to be less than 'to', but got from=" + str(from_) + " >= to=" + str(to_),
                        lambda: t.random_(from_, to_)
                    )

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_random_to(self, device, dtype):
        size = 2000
        alpha = 0.1

        int64_min_val = torch.iinfo(torch.int64).min
        int64_max_val = torch.iinfo(torch.int64).max

        if dtype in [torch.float, torch.double, torch.half]:
            min_val = int(max(torch.finfo(dtype).min, int64_min_val))
            max_val = int(min(torch.finfo(dtype).max, int64_max_val))
            tos = [-42, 0, 42, max_val >> 1]
        elif dtype == torch.bfloat16:
            min_val = int64_min_val
            max_val = int64_max_val
            tos = [-42, 0, 42, max_val >> 1]
        elif dtype == torch.uint8:
            min_val = torch.iinfo(dtype).min
            max_val = torch.iinfo(dtype).max
            tos = [-42, min_val - 1, min_val, 42, max_val, max_val + 1, int64_max_val]
        elif dtype == torch.int64:
            min_val = int64_min_val
            max_val = int64_max_val
            tos = [-42, 0, 42, max_val]
        else:
            min_val = torch.iinfo(dtype).min
            max_val = torch.iinfo(dtype).max
            tos = [min_val - 1, min_val, -42, 0, 42, max_val, max_val + 1, int64_max_val]

        from_ = 0
        for to_ in tos:
            t = torch.empty(size, dtype=dtype, device=device)
            if to_ > from_:
                if not (min_val <= (to_ - 1) <= max_val):
                    self.assertRaisesRegex(
                        RuntimeError,
                        "to - 1 is out of bounds",
                        lambda: t.random_(from_, to_)
                    )
                else:
                    t.random_(to_)
                    range_ = to_ - from_
                    delta = max(1, alpha * range_)
                    if dtype == torch.bfloat16:
                        # Less strict checks because of rounding errors
                        # TODO investigate rounding errors
                        self.assertTrue(from_ <= t.to(torch.double).min() < (from_ + delta))
                        self.assertTrue((to_ - delta) < t.to(torch.double).max() <= to_)
                    else:
                        self.assertTrue(from_ <= t.to(torch.double).min() < (from_ + delta))
                        self.assertTrue((to_ - delta) <= t.to(torch.double).max() < to_)
            else:
                self.assertRaisesRegex(
                    RuntimeError,
                    "random_ expects 'from' to be less than 'to', but got from=" + str(from_) + " >= to=" + str(to_),
                    lambda: t.random_(from_, to_)
                )

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_random_default(self, device, dtype):
        size = 2000
        alpha = 0.1

        if dtype == torch.float:
            to_inc = 1 << 24
        elif dtype == torch.double:
            to_inc = 1 << 53
        elif dtype == torch.half:
            to_inc = 1 << 11
        elif dtype == torch.bfloat16:
            to_inc = 1 << 8
        else:
            to_inc = torch.iinfo(dtype).max

        t = torch.empty(size, dtype=dtype, device=device)
        t.random_()
        self.assertTrue(0 <= t.to(torch.double).min() < alpha * to_inc)
        self.assertTrue((to_inc - alpha * to_inc) < t.to(torch.double).max() <= to_inc)

    @onlyCPU
    @dtypes(torch.half, torch.double, torch.int)
    def test_cat(self, device, dtype):
        SIZE = 10
        for dim in range(-3, 3):
            pos_dim = dim if dim >= 0 else 3 + dim
            x = torch.randint(low=-100, high=100, size=(13, SIZE, SIZE), device=device).to(dtype).transpose(0, pos_dim)
            y = torch.randint(low=-100, high=100, size=(17, SIZE, SIZE), device=device).to(dtype).transpose(0, pos_dim)
            z = torch.randint(low=-100, high=100, size=(19, SIZE, SIZE), device=device).to(dtype).transpose(0, pos_dim)

            res1 = torch.cat((x, y, z), dim)
            self.assertEqual(res1.narrow(pos_dim, 0, 13), x, atol=0, rtol=0)
            self.assertEqual(res1.narrow(pos_dim, 13, 17), y, atol=0, rtol=0)
            self.assertEqual(res1.narrow(pos_dim, 30, 19), z, atol=0, rtol=0)

        x = torch.randint(low=-100, high=100, size=(20, SIZE, SIZE), device=device).to(dtype)
        self.assertEqual(torch.cat(torch.split(x, 7)), x)
        self.assertEqual(torch.cat(torch.chunk(x, 7)), x)

        y = torch.randint(low=-100, high=100, size=(1, SIZE, SIZE), device=device).to(dtype)
        z = torch.cat([x, y])
        self.assertEqual(z.size(), (21, SIZE, SIZE))

        self.assertRaises(RuntimeError, lambda: torch.cat([]))
        self.assertRaisesRegex(TypeError, 'got None', lambda: torch.cat([x, None]))

    @onlyCPU
    def test_cat_scalars(self, device):
        x = torch.tensor(0, device=device)
        y = torch.tensor(1, device=device)
        with self.assertRaisesRegex(RuntimeError, 'zero-dimensional.*cannot be concatenated'):
            torch.cat([x, y])

    @onlyCPU
    @dtypes(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
    def test_floor_divide_zero(self, device, dtype):
        a = torch.tensor([0, 1], dtype=dtype, device=device)
        b = torch.tensor([0, 1], dtype=dtype, device=device)
        with self.assertRaisesRegex(RuntimeError, 'ZeroDivisionError'):
            a // b

    @onlyCPU
    def test_cat_bad_input_sizes(self, device):
        x = torch.randn(2, 1, device=device)
        y = torch.randn(2, 1, 1, device=device)
        z = torch.randn(2, 1, 1, device=device)
        self.assertRaises(RuntimeError, lambda: torch.cat([x, y, z]))

        x = torch.randn(2, 1, 2, device=device)
        y = torch.randn(2, 1, 1, device=device)
        z = torch.randn(2, 2, 1, device=device)
        self.assertRaises(RuntimeError, lambda: torch.cat([x, y, z], dim=1))

    @slowTest
    @onlyCPU
    def test_cat_big(self, device):
        SIZE1 = 6500
        SIZE2 = 4500
        concat_list = []
        concat_list.append(torch.ones((SIZE1, 1024 * 512), dtype=torch.uint8, device=device))
        concat_list.append(torch.ones((SIZE2, 1024 * 512), dtype=torch.uint8, device=device))
        result = torch.cat(concat_list)
        self.assertEqual(result.size(0), SIZE1 + SIZE2)


    @onlyCPU
    def test_max_mixed_devices(self, device):
        a = torch.randn(10, device=device)
        if torch.cuda.is_available():
            values = torch.randn(10).cuda()
            indices = torch.cuda.LongTensor()
            self.assertRaises(RuntimeError,
                              lambda: torch.max(a, 0, out=(values, indices)))
            self.assertRaises(RuntimeError,
                              lambda: torch.amax(a, 0, out=values))

    @onlyCPU
    def test_min_mixed_devices(self, device):
        a = torch.randn(10, device=device)
        if torch.cuda.is_available():
            values = torch.randn(10).cuda()
            indices = torch.cuda.LongTensor()
            self.assertRaises(RuntimeError,
                              lambda: torch.min(a, 0, out=(values, indices)))
            self.assertRaises(RuntimeError,
                              lambda: torch.amin(a, 0, out=values))

    def _float_to_int_conversion_helper(self, vals, device, dtype):
        assert TEST_NUMPY

        a = np.array(vals, dtype=np.float32).astype(torch_to_numpy_dtype_dict[dtype])
        t = torch.tensor(vals, device=device, dtype=torch.float).to(dtype)
        self.assertEqual(torch.from_numpy(a), t.cpu())

    # Checks that float->integer casts don't produce undefined behavior errors.
    # Note: In C++, casting from a floating value to an integral dtype
    # is undefined if the floating point value is not within the integral
    # dtype's dynamic range. This can (and should) cause undefined behavior
    # errors with UBSAN. These casts are deliberate in PyTorch, however, and
    # NumPy has the same behavior.
    @onlyOnCPUAndCUDA
    @unittest.skipIf(IS_MACOS, "Test is broken on MacOS, see https://github.com/pytorch/pytorch/issues/38752")
    @unittest.skipIf(IS_PPC, "Test is borken on PowerPC, see https://github.com/pytorch/pytorch/issues/39671")
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
    def test_float_to_int_conversion_finite(self, device, dtype):
        min = torch.finfo(torch.float).min
        max = torch.finfo(torch.float).max

        # Note: CUDA max float -> integer conversion is divergent on some dtypes
        vals = (min, -2, -1.5, -.5, 0, .5, 1.5, 2, max)
        if self.device_type == 'cuda':
            if torch.version.hip:
                # HIP min float -> int64 conversion is divergent
                vals = (-2, -1.5, -.5, 0, .5, 1.5, 2)
            else:
                vals = (min, -2, -1.5, -.5, 0, .5, 1.5, 2)

        self._float_to_int_conversion_helper(vals, device, dtype)

    # Note: CUDA will fail this test on most dtypes, often dramatically.
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @onlyCPU
    @dtypes(torch.bool, torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64)
    def test_float_to_int_conversion_nonfinite(self, device, dtype):
        vals = (float('-inf'), float('inf'), float('nan'))

        self._float_to_int_conversion_helper(vals, device, dtype)

    # TODO: re-enable this test
    @unittest.skipIf(True, "real and imag not implemented for complex")
    @onlyOnCPUAndCUDA
    def test_complex_type_conversions(self, device):
        dtypes = [torch.float, torch.complex64, torch.complex128]
        for from_type in dtypes:
            for to_type in dtypes:
                from_tensor = torch.randn(4, dtype=from_type, device=device)
                to_tensor = from_tensor.to(to_type)
                if from_type.is_complex and not to_type.is_complex:
                    self.assertEqual(torch.real(from_tensor), to_tensor, exact_dtype=False)
                elif not from_type.is_complex and to_type.is_complex:
                    self.assertEqual(from_tensor, torch.real(to_tensor), exact_dtype=False)
                    self.assertEqual(torch.zeros_like(torch.imag(to_tensor)), torch.imag(to_tensor), exact_dtype=False)
                else:
                    self.assertEqual(from_tensor, to_tensor, exact_dtype=False)

    @dtypes(torch.complex64, torch.complex128)
    def test_complex_unsupported(self, device, dtype):
        t = torch.tensor((1 + 1j), device=device, dtype=dtype)
        # Note: this is consistent with NumPy
        with self.assertRaises(RuntimeError):
            torch.floor(t)
        with self.assertRaises(RuntimeError):
            torch.ceil(t)
        with self.assertRaises(RuntimeError):
            torch.trunc(t)

        # Tests min and max variants with complex inputs
        # Note: whether PyTorch should support min and max on complex
        # tensors is an open question.
        # See https://github.com/pytorch/pytorch/issues/36374
        with self.assertRaises(RuntimeError):
            torch.min(t)
        with self.assertRaises(RuntimeError):
            t.min()
        with self.assertRaises(RuntimeError):
            torch.min(t, dim=0)
        with self.assertRaises(RuntimeError):
            torch.min(t, t)
        with self.assertRaises(RuntimeError):
            torch.min(t, t, out=t)

        with self.assertRaises(RuntimeError):
            torch.max(t)
        with self.assertRaises(RuntimeError):
            t.max()
        with self.assertRaises(RuntimeError):
            torch.max(t, dim=0)
        with self.assertRaises(RuntimeError):
            torch.max(t, t)
        with self.assertRaises(RuntimeError):
            torch.max(t, t, out=t)

        with self.assertRaises(RuntimeError):
            torch.amin(t)
        with self.assertRaises(RuntimeError):
            t.amin()
        with self.assertRaises(RuntimeError):
            torch.amin(t, dim=0)

        with self.assertRaises(RuntimeError):
            torch.amax(t)
        with self.assertRaises(RuntimeError):
            t.amax()
        with self.assertRaises(RuntimeError):
            torch.amax(t, dim=0)

        # Tests clamp variants with complex inputs
        # Note: whether PyTorch should support clamp on complex
        # tensors is an open question.
        # See https://github.com/pytorch/pytorch/issues/33568
        min_val = 1 + 1j
        max_val = 4 + 4j
        out = torch.empty((0,), device=device, dtype=dtype)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, min=min_val)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, max=max_val)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, min_val, max_val)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, min=min_val, out=out)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, max=max_val, out=out)
        with self.assertRaises(RuntimeError):
            torch.clamp(t, min_val, max_val, out=out)

    @dtypes(torch.long)
    def test_abs_big_number(self, device, dtype):
        bignumber = 2 ** 31 + 1
        res = torch.tensor([bignumber], device=device, dtype=dtype)
        self.assertGreater(res.abs()[0], 0)

    @dtypes(torch.float, torch.double)
    def test_abs_signed_zero(self, device, dtype):
        # Both abs(0.0) and abs(-0.0) should result in 0.0
        size = 128 + 1  # pick a large enough number with remainder so that
        # both vectorized and nonvectorized op is tested
        inp = torch.zeros(size, device=device, dtype=dtype)
        inp[::2] = -0.0
        inp = inp.abs()
        for v in inp:
            self.assertGreater(math.copysign(1.0, v), 0.0)

    @dtypes(torch.float)
    def test_absolute(self, device, dtype):
        # absolute is an alias for abs. Just check to see that results
        # are the same.
        t = torch.randn(10, 10, device=device, dtype=dtype)
        r_abs = t.abs()
        r_absolute = t.absolute()
        self.assertEqual(r_abs, r_absolute)

        r_abs = torch.abs(t)
        r_absolute = torch.absolute(t)
        self.assertEqual(r_abs, r_absolute)

        r_abs = torch.empty((10, 10), device=device, dtype=dtype)
        r_absolute = torch.empty((10, 10), device=device, dtype=dtype)
        torch.abs(t, out=r_abs)
        torch.absolute(t, out=r_absolute)
        self.assertEqual(r_abs, r_absolute)

        from copy import deepcopy
        t_copy = deepcopy(t)
        t.absolute_()
        t_copy.abs_()
        self.assertEqual(t, t_copy)

    def test_bucketization(self, device):
        values_1d = torch.tensor([1, 2, 3, 4, 5, 6, 7, 8, 9], device=device)
        values_3d = torch.tensor([[[1, 3, 5], [2, 4, 6]], [[1, 2, 3], [4, 5, 6]]], device=device)

        # regular case 3d boundary and 3d input value
        boundaries = torch.tensor([[[1, 2, 3, 4], [3, 4, 5, 6]], [[1, 3, 5, 7], [2, 4, 6, 8]]], device=device)
        expected_result = torch.tensor([[[0, 2, 4], [0, 1, 3]], [[0, 1, 1], [1, 2, 2]]], device=device)
        output = torch.empty(2, 2, 3, device=device, dtype=torch.int64)
        self.assertEqual(torch.searchsorted(boundaries, values_3d), expected_result)
        self.assertEqual(torch.searchsorted(boundaries, values_3d, out=output), expected_result)
        expected_result = torch.tensor([[[1, 3, 4], [0, 2, 4]], [[1, 1, 2], [2, 2, 3]]], device=device)
        self.assertEqual(torch.searchsorted(boundaries, values_3d, right=True), expected_result)
        self.assertEqual(torch.searchsorted(boundaries, values_3d, right=True, out=output), expected_result)

        # simple 1d boundary and 3d input value
        boundaries = torch.tensor([1, 2, 3, 4, 5, 6], device=device)
        expected_result = torch.tensor([[[0, 2, 4], [1, 3, 5]], [[0, 1, 2], [3, 4, 5]]], device=device)
        output = torch.empty(2, 2, 3, device=device, dtype=torch.int64)
        self.assertEqual(torch.searchsorted(boundaries, values_3d), expected_result)
        self.assertEqual(torch.bucketize(values_3d, boundaries), expected_result)
        self.assertEqual(torch.bucketize(values_3d, boundaries, out=output), expected_result)
        expected_result = torch.tensor([[[1, 3, 5], [2, 4, 6]], [[1, 2, 3], [4, 5, 6]]], device=device)
        self.assertEqual(torch.searchsorted(boundaries, values_3d, right=True), expected_result)
        self.assertEqual(torch.bucketize(values_3d, boundaries, right=True), expected_result)
        self.assertEqual(torch.bucketize(values_3d, boundaries, out=output, right=True), expected_result)

        # simple float 1d boundary and 1d input with output int32 type
        values_1d_float = values_1d.to(torch.float32)
        boundaries = torch.tensor([0.9, 1, 2, 2, 3, 3, 4, 4.1, 9, 9], device=device, dtype=torch.float32)
        expected_result = torch.tensor([1, 2, 4, 6, 8, 8, 8, 8, 8], device=device, dtype=torch.int32)
        self.assertEqual(torch.searchsorted(boundaries, values_1d_float, out_int32=True), expected_result)
        self.assertEqual(torch.bucketize(values_1d_float, boundaries, out_int32=True), expected_result)

        # multiple dimension input with 0 elements
        boundaries = torch.tensor([1, 2, 3, 4, 5, 6], device=device, dtype=torch.int64)
        values_0_el = torch.tensor([[[]]], device=device, dtype=torch.int64)
        expected_result = values_0_el.to(torch.int64)
        self.assertEqual(torch.searchsorted(boundaries, values_0_el), expected_result)
        self.assertEqual(torch.bucketize(values_0_el, boundaries), expected_result)

        # nan input
        values_nan = torch.tensor([1.0, float('nan'), 2.0, float('nan')], device=device, dtype=torch.float64)
        boundaries = torch.tensor([0.0, 1.0, 2.0, 3.0], device=device, dtype=torch.float64)
        expected_result = torch.tensor([1, 4, 2, 4], device=device)
        self.assertEqual(torch.searchsorted(boundaries, values_nan), expected_result)
        expected_result = torch.tensor([2, 4, 3, 4], device=device)
        self.assertEqual(torch.searchsorted(boundaries, values_nan, right=True), expected_result)

        # type promotion and non contiguous tensors
        values_3d_permute = values_3d.permute(2, 1, 0).to(torch.int32)
        boundaries_permute = values_3d.permute(2, 1, 0).to(torch.float64)
        expected_result = torch.tensor([[[0, 0], [0, 1]], [[2, 0], [0, 1]], [[2, 0], [0, 0]]], device=device)
        if self.device_type != 'xla':
            self.assertWarnsRegex(
                UserWarning, "tensor is non-contiguous",
                lambda: self.assertEqual(torch.searchsorted(boundaries_permute, values_3d_permute), expected_result))
        else:
            # All tensors in XLA is contiguous even doing permute, no warning msg will be generate in XLA
            self.assertEqual(torch.searchsorted(boundaries_permute, values_3d_permute), expected_result)

        # scalar type
        boundaries = torch.tensor([1.5, 2.5, 3.5], device=device)
        expected_result = torch.tensor(1, device=device)
        self.assertEqual(torch.searchsorted(boundaries, 2), expected_result)
        self.assertEqual(torch.bucketize(torch.tensor(2, device=device), boundaries), expected_result)
        expected_result = torch.tensor(3, device=device)
        scalar_tensor_nan = torch.tensor(float('nan'), device=device)
        self.assertEqual(torch.searchsorted(boundaries, scalar_tensor_nan), expected_result)
        self.assertEqual(torch.bucketize(float('nan'), boundaries, right=True), expected_result)

        # invalid input dimensions
        boundaries = torch.tensor([[1, 2, 3], [4, 5, 6]], device=device)
        with self.assertRaisesRegex(
                RuntimeError, "first N-1 dimensions of boundaries tensor and input value tensor must match"):
            torch.searchsorted(boundaries, values_3d)
        with self.assertRaisesRegex(
                RuntimeError, "boundaries tensor must be 1 dimension"):
            torch.bucketize(values_3d, boundaries)
        with self.assertRaisesRegex(
                RuntimeError, "only when boundaries tensor dimension is 1"):
            torch.searchsorted(boundaries, 1)

        # incompatiable output tensor's dtype
        def test_output_dtype(dtype, is_int32):
            output = values_1d.to(dtype)
            with self.assertRaisesRegex(
                    RuntimeError, "output tensor's dtype is wrong"):
                torch.searchsorted(values_1d, values_1d, out=output, out_int32=is_int32)

        test_output_dtype(torch.float32, False)
        test_output_dtype(torch.int32, False)
        test_output_dtype(torch.int64, True)

    def test_pickle_gradscaler(self, device):
        # This test is not in test_cuda.py because it should pass in 3 cases:
        #  1. cuda is not available.
        #  2. cuda is available but device is not cuda.
        #  3. cuda is available and device is cuda.
        # In case 1, a and b disable themselves on construction and shouldn't try to pickle workhorse attributes.
        # In case 2, a and b are enabled.  Workhorse attributes participate in pickling, but none are lazy-inited
        # to cuda Tensors, because I don't want to do cuda things if device is not cuda.
        # In case 3, a and b are enabled and we may also try lazy-initing _scale to a cuda tensor.
        device = torch.device(device)
        try_lazy_inits = (True, False) if device.type == "cuda" else (False,)
        for lazy_init_scale in try_lazy_inits:
            a = torch.cuda.amp.GradScaler(init_scale=3., growth_factor=4., backoff_factor=.5, growth_interval=2)
            self.assertTrue(a.is_enabled() if torch.cuda.is_available() else not a.is_enabled())
            if lazy_init_scale:
                # Dummy a.scale() call lazy-inits a._scale Tensor.
                a.scale(torch.tensor([4.0], dtype=torch.float32, device=device))
                self.assertTrue(isinstance(a._scale, torch.cuda.FloatTensor))
            # The following three lines should work whether or not cuda is available.
            serialized = pickle.dumps(a)
            b = pickle.loads(serialized)
            self.assertEqual(b.is_enabled(), a.is_enabled())
            if a.is_enabled():
                self.assertEqual(b.get_scale(), 3.)
                self.assertEqual(b.get_growth_factor(), 4.)
                self.assertEqual(b.get_backoff_factor(), .5)
                self.assertEqual(b.get_growth_interval(), 2)
                self.assertEqual(b._init_growth_tracker, 0)
                # supplies a dummy key to test the defaultdict's default_factory
                self.assertEqual(b._per_optimizer_states["fdsa"],
                                 torch.cuda.amp.grad_scaler._refresh_per_optimizer_state())
                if lazy_init_scale:
                    self.assertEqual(b.scale(torch.tensor([4.0], dtype=torch.float32, device=device)), 12.0)

    @dtypesIfCUDA(torch.half, torch.float, torch.double,
                  torch.int8, torch.short, torch.int, torch.long)
    @dtypes(torch.float, torch.double,
            torch.int8, torch.short, torch.int, torch.long)
    def test_nansum(self, device, dtype):
        x = (torch.randn(3, 3))
        if dtype in [torch.half, torch.float, torch.double]:
            x[x < 0.2] = float('nan')
        # Randomly scale the values
        x = (x * random.randint(10, 100)).tolist()

        self.compare_with_numpy(torch.nansum, np.nansum, x, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.float32, torch.float64)
    def test_unpack_double(self, device, dtype):
        # Reference: https://github.com/pytorch/pytorch/issues/33111
        vals = (2 ** 24 + 1, 2 ** 53 + 1,
                np.iinfo(np.int64).max, np.iinfo(np.uint64).max, np.iinfo(np.uint64).max + 1,
                -1e500, 1e500)
        for val in vals:
            t = torch.tensor(val, dtype=dtype, device=device)
            a = np.array(val, dtype=torch_to_numpy_dtype_dict[dtype])
            self.assertEqual(t, torch.from_numpy(a))

    def test_multinomial_invalid(self, device):
        def test(probs):
            with self.assertRaisesRegex(RuntimeError,
                                        'probability tensor contains either `inf`, `nan` or element < 0'):
                torch.multinomial(probs.to(device), 2)
                torch.cuda.synchronize()

        test(torch.Tensor([1, -1, 1]))
        test(torch.Tensor([1, inf, 1]))
        test(torch.Tensor([1, -inf, 1]))
        test(torch.Tensor([1, 1, nan]))

    def test_multinomial_invalid_distribution(self, device):
        def test(probs, replacement):
            with self.assertRaisesRegex(RuntimeError,
                                        r"invalid multinomial distribution \(sum of probabilities <= 0\)"):
                torch.multinomial(probs, 2, replacement)
                torch.cuda.synchronize()

        x = torch.zeros(3, device=device)
        y = torch.zeros(3, 3, device=device)
        z = torch.zeros(3, 3, device=device)
        z[1, :] = 1

        test(x, False)
        test(y, False)
        test(z, False)

        # Verify only for CPU as replacement=True
        # throws device side assert triggered.
        if self.device_type == 'cpu':
            test(x, True)
            test(y, True)
            test(z, True)

    def _test_multinomial_empty(self, device, replacement, num_samples):
        probs = torch.ones(0, 3, device=device)
        expected = torch.empty(0, num_samples, dtype=torch.int64)
        out = torch.multinomial(probs, num_samples=num_samples, replacement=replacement)
        self.assertEqual(out, expected)

    def test_multinomial_empty_w_replacement(self, device):
        self._test_multinomial_empty(device, True, 1)
        self._test_multinomial_empty(device, True, 2)

    def test_multinomial_empty_wo_replacement(self, device):
        self._test_multinomial_empty(device, False, 1)
        self._test_multinomial_empty(device, False, 2)

    def _generate_input(self, shape, dtype, device, with_extremal):
        if shape == ():
            x = torch.tensor((), dtype=dtype, device=device)
        else:
            if dtype.is_floating_point or dtype.is_complex:
                x = torch.randn(*shape, dtype=dtype, device=device) * random.randint(30, 100)
                x[torch.randn(*shape) > 0.5] = 0
                if with_extremal and dtype.is_floating_point:
                    # Use extremal values
                    x[torch.randn(*shape) > 0.5] = float('nan')
                    x[torch.randn(*shape) > 0.5] = float('inf')
                    x[torch.randn(*shape) > 0.5] = float('-inf')
                elif with_extremal and dtype.is_complex:
                    x[torch.randn(*shape) > 0.5] = complex('nan')
                    x[torch.randn(*shape) > 0.5] = complex('inf')
                    x[torch.randn(*shape) > 0.5] = complex('-inf')
            else:
                x = torch.randint(15, 100, shape, dtype=dtype, device=device)

        return x

    def _test_reduction_function_with_numpy(self, torch_func, np_func, device, dtype,
                                            with_extremal=False, atol=None, rtol=None,
                                            exact_dtype=True, with_keepdim=False):
        # Test 0-d to 3-d tensors.
        for ndims in range(0, 4):
            shape = self._rand_shape(ndims, min_size=5, max_size=10)
            for n in range(ndims + 1):
                for c in combinations(list(range(ndims)), n):
                    for count_dim in permutations(c):
                        # Generate Input.
                        x = self._generate_input(shape, dtype, device, with_extremal)

                        if count_dim == ():
                            # Default `dims=None` case
                            self.compare_with_numpy(torch_func, np_func, x, device=None, dtype=None,
                                                    atol=atol, rtol=rtol, exact_dtype=exact_dtype)
                        else:
                            # With `dims: tuple of ints` case
                            if with_keepdim:
                                torch_func_partial = partial(torch_func, keepdim=True, dim=count_dim)
                                np_func_partial = partial(np_func, keepdims=True, axis=count_dim)
                            else:
                                torch_func_partial = partial(torch_func, dim=count_dim)
                                np_func_partial = partial(np_func, axis=count_dim)
                            self.compare_with_numpy(torch_func_partial, np_func_partial, x, device=None, dtype=None,
                                                    atol=atol, rtol=rtol, exact_dtype=exact_dtype)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_count_nonzero(self, device, dtype):
        self._test_reduction_function_with_numpy(torch.count_nonzero, np.count_nonzero, device, dtype)
        self._test_reduction_function_with_numpy(torch.count_nonzero, np.count_nonzero, device, dtype, True)

    def _test_sum_reduction_vs_numpy(self, torch_fn, np_fn, device, dtype, with_keepdim=False, with_extremal=False):
        def is_integral(dtype):
            return dtype in torch.testing.get_all_int_dtypes()

        # On Windows CI, the current version of `numpy` promotes all lower integers
        # dtypes to int32 while `torch` promotes them to int64. Hence we skip on checking
        # the exact dtype.
        # Reference : https://dr.pytorch.org/api/view-log-full?build_id=122051580
        # PR : https://github.com/pytorch/pytorch/pull/38628#issuecomment-655905370
        exact_dtype = False if (IS_WINDOWS and is_integral(dtype)) else True

        if dtype == torch.uint8:
            with self.assertRaises(TypeError):
                self._test_reduction_function_with_numpy(torch_fn, np_fn, device, dtype, with_extremal=with_extremal)
        else:
            # TODO: Investigate why the output is not close to numpy.
            if dtype == torch.float16:
                atol = 0.4
                rtol = 1e-2
            elif dtype == torch.float32:
                atol = 7e-05
                rtol = 3e-06
            else:
                # Default values
                atol = None
                rtol = None
            self._test_reduction_function_with_numpy(torch_fn, np_fn, device, dtype,
                                                     atol=atol, rtol=rtol, exact_dtype=exact_dtype,
                                                     with_keepdim=with_keepdim, with_extremal=with_extremal)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False)))
    def test_sum_vs_numpy(self, device, dtype):
        self._test_sum_reduction_vs_numpy(torch.sum, np.sum, device, dtype)
        self._test_sum_reduction_vs_numpy(torch.sum, np.sum, device, dtype, with_extremal=True)
        self._test_sum_reduction_vs_numpy(torch.sum, np.sum, device, dtype, with_keepdim=True)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False)))
    def test_nansum_vs_numpy(self, device, dtype):
        self._test_sum_reduction_vs_numpy(torch.nansum, np.nansum, device, dtype)
        self._test_sum_reduction_vs_numpy(torch.nansum, np.nansum, device, dtype, with_extremal=True)
        self._test_sum_reduction_vs_numpy(torch.nansum, np.nansum, device, dtype, with_keepdim=True)

    @dtypes(*(torch.testing.get_all_complex_dtypes()))
    def test_nansum_complex(self, device, dtype):
        x = torch.randn((3, 3, 3), device=device, dtype=dtype)
        with self.assertRaisesRegex(RuntimeError, "nansum does not support complex inputs"):
            torch.nansum(x)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    def test_nansum_out_dtype(self, device):
        dtypes = list(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False))
        for inp_dtype, out_dtype in combinations(dtypes, 2):
            shape = self._rand_shape(random.randint(2, 5), min_size=5, max_size=10)
            x = self._generate_input(shape, inp_dtype, device, with_extremal=False)
            torch_fn = partial(torch.nansum, dtype=out_dtype)
            np_out_dtype = torch_to_numpy_dtype_dict[out_dtype]
            np_fn = partial(np.nansum, dtype=np_out_dtype)
            self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

    @dtypes(torch.int32, torch.int64)
    def test_large_linspace(self, device, dtype):
        start = torch.iinfo(dtype).min
        end = torch.iinfo(dtype).max & ~0xfff
        steps = 15
        x = torch.linspace(start, end, steps, dtype=dtype, device=device)
        self.assertGreater(x[1] - x[0], (end - start) / steps)

    def _test_where_scalar_template(self, device, dtype, exec_fn):
        for with_extremal in [True, False]:
            for ndims in range(0, 4):
                shape = self._rand_shape(ndims, min_size=5, max_size=10)
                for n in range(ndims + 1):
                    for c in combinations(list(range(ndims)), n):
                        for scalar_type in [int, float, complex]:
                            if dtype.is_complex:
                                condition = self._generate_input(shape, dtype, device, with_extremal).abs() > 0.5
                            else:
                                condition = self._generate_input(shape, dtype, device, with_extremal) > 0.5

                            x = self._generate_input(shape, dtype, device, with_extremal)

                            if not dtype.is_complex and scalar_type == complex:
                                continue

                            scalar_1 = scalar_type(random.random())

                            exec_fn(scalar_type, dtype, condition, x, scalar_1)

    # For current implementation,
    # below are the valid `TensorDtype` and `ScalarType` combinations.
    def _where_valid_scalar_tensor_combination(self, scalar_type, dtype):
        if (scalar_type == int and dtype == torch.long):
            return True
        elif (scalar_type == float and dtype == torch.double):
            return True
        elif (scalar_type == complex and dtype == torch.complex128):
            return True
        return False

    @onlyOnCPUAndCUDA
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_where_scalar_invalid_combination_raises(self, device, dtype):

        def checkRaises(scalar_type, dtype, condition, x, scalar_1):
            if not self._where_valid_scalar_tensor_combination(scalar_type, dtype):
                # Note: This should fail once `where` supports type promotion.
                with self.assertRaisesRegex(RuntimeError, "expected scalar type"):
                    torch.where(condition, x, scalar_1)

        self._test_where_scalar_template(device, dtype, checkRaises)

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_where_scalar_valid_combination(self, device, dtype):

        def checkResult(scalar_type, dtype, condition, x, scalar_1):
            if self._where_valid_scalar_tensor_combination(scalar_type, dtype):
                def x_like(scalar, without_dtype=False):
                    return torch.tensor(scalar, dtype=dtype, device=device).expand_as(x)

                # X = Tensor, Y = Scalar
                scalar_out = torch.where(condition, x, scalar_1)
                tensor_out = torch.where(condition, x, x_like(scalar_1))
                self.assertEqual(scalar_out, tensor_out)

                # X = Scalar, Y = Tensor
                scalar_out = torch.where(condition, scalar_1, x)
                tensor_out = torch.where(condition, x_like(scalar_1), x)
                self.assertEqual(scalar_out, tensor_out)

        self._test_where_scalar_template(device, dtype, checkResult)

    # As the test fails with Runtime Error not raised on XLA
    @onlyOnCPUAndCUDA
    def test_where_scalar_scalar(self, device):
        # Scalar-Scalar Version
        height = 5
        width = 5
        default_dtype = torch.get_default_dtype()
        for test_default_dtype in [torch.float, torch.double]:
            torch.set_default_dtype(test_default_dtype)
            for scalar_type_1 in [int, float, complex]:
                for scalar_type_2 in [int, float, complex]:
                    x1 = scalar_type_1(random.random() * random.randint(10, 20))
                    x2 = scalar_type_2(random.random() * random.randint(20, 30))
                    condition = torch.randn(height, width, device=device) > 0.5
                    if scalar_type_1 != scalar_type_2:
                        self.assertRaisesRegex(RuntimeError, "expected scalar type", lambda: torch.where(condition, x1, x2))
                    else:
                        def get_dtype(scalar_type):
                            complex_dtype = torch.complex64 if torch.float == torch.get_default_dtype() else torch.complex128
                            type_map = {int: torch.long, float: torch.get_default_dtype(), complex: complex_dtype}
                            return type_map[scalar_type]
                        expected = torch.zeros((height, width), dtype=get_dtype(scalar_type_1))
                        expected[condition] = x1
                        expected[~condition] = x2
                        result = torch.where(condition, x1, x2)
                        self.assertEqual(expected, result)

        # Reset the original dtype
        torch.set_default_dtype(default_dtype)

    @dtypes(torch.int64, torch.float, torch.complex128)
    def test_movedim_invalid(self, device, dtype):
        shape = self._rand_shape(4, min_size=5, max_size=10)
        x = self._generate_input(shape, dtype, device, False)

        # Invalid `source` and `destination` dimension
        with self.assertRaisesRegex(IndexError, "Dimension out of range"):
            torch.movedim(x, 5, 0)

        with self.assertRaisesRegex(IndexError, "Dimension out of range"):
            torch.movedim(x, 0, 5)

        # Mismatch in size of `source` and `destination`
        with self.assertRaisesRegex(RuntimeError, "movedim: Invalid source or destination dims:"):
            torch.movedim(x, (1, 0), (0, ))

        with self.assertRaisesRegex(RuntimeError, "movedim: repeated dim in `source`"):
            torch.movedim(x, (0, 0), (0, 1))

        with self.assertRaisesRegex(RuntimeError, "movedim: repeated dim in `source`"):
            torch.movedim(x, (0, 1, 0), (0, 1, 2))

        with self.assertRaisesRegex(RuntimeError, "movedim: repeated dim in `destination`"):
            torch.movedim(x, (0, 1), (1, 1))

        with self.assertRaisesRegex(RuntimeError, "movedim: repeated dim in `destination`"):
            torch.movedim(x, (0, 1, 2), (1, 0, 1))

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(torch.int64, torch.float, torch.complex128)
    def test_movedim(self, device, dtype):
        for nd in range(5):
            shape = self._rand_shape(nd, min_size=5, max_size=10)
            x = self._generate_input(shape, dtype, device, with_extremal=False)
            for random_negative in [True, False]:
                for src_dim, dst_dim in permutations(range(nd), r=2):
                    random_prob = random.random()

                    if random_negative and random_prob > 0.66:
                        src_dim = src_dim - nd
                    elif random_negative and random_prob > 0.33:
                        dst_dim = dst_dim - nd
                    elif random_negative:
                        src_dim = src_dim - nd
                        dst_dim = dst_dim - nd

                    # Integer `source` and `destination`
                    torch_fn = partial(torch.movedim, source=src_dim, destination=dst_dim)
                    np_fn = partial(np.moveaxis, source=src_dim, destination=dst_dim)
                    self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

                if nd == 0:
                    continue

                def make_index_negative(sequence, idx):
                    sequence = list(sequence)
                    sequence[random_idx] = sequence[random_idx] - nd
                    return tuple(src_sequence)

                for src_sequence in permutations(range(nd), r=random.randint(1, nd)):
                    # Sequence `source` and `destination`
                    dst_sequence = tuple(random.sample(range(nd), len(src_sequence)))

                    # Randomly change a dim to a negative dim representation of itself.
                    random_prob = random.random()
                    if random_negative and random_prob > 0.66:
                        random_idx = random.randint(0, len(src_sequence) - 1)
                        src_sequence = make_index_negative(src_sequence, random_idx)
                    elif random_negative and random_prob > 0.33:
                        random_idx = random.randint(0, len(src_sequence) - 1)
                        dst_sequence = make_index_negative(dst_sequence, random_idx)
                    elif random_negative:
                        random_idx = random.randint(0, len(src_sequence) - 1)
                        dst_sequence = make_index_negative(dst_sequence, random_idx)
                        random_idx = random.randint(0, len(src_sequence) - 1)
                        src_sequence = make_index_negative(src_sequence, random_idx)

                    torch_fn = partial(torch.movedim, source=src_sequence, destination=dst_sequence)
                    np_fn = partial(np.moveaxis, source=src_sequence, destination=dst_sequence)
                    self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

        # Move dim to same position
        x = torch.randn(2, 3, 5, 7, 11)
        torch_fn = partial(torch.movedim, source=(0, 1), destination=(0, 1))
        np_fn = partial(np.moveaxis, source=(0, 1), destination=(0, 1))
        self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

        torch_fn = partial(torch.movedim, source=1, destination=1)
        np_fn = partial(np.moveaxis, source=1, destination=1)
        self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

        # Empty Sequence
        torch_fn = partial(torch.movedim, source=(), destination=())
        np_fn = partial(np.moveaxis, source=(), destination=())
        self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

    def _test_atleast_dim(self, torch_fn, np_fn, device, dtype):
        for ndims in range(0, 5):
            shape = self._rand_shape(ndims, min_size=5, max_size=10)
            for n in range(ndims + 1):
                for with_extremal in [False, True]:
                    for contiguous in [False, True]:
                        # Generate Input.
                        x = self._generate_input(shape, dtype, device, with_extremal)
                        if contiguous:
                            x = x.T
                        self.compare_with_numpy(torch_fn, np_fn, x, device=None, dtype=None)

                        # Compare sequence input
                        torch_sequence_x = (x,) * random.randint(3, 10)
                        np_sequence_x = tuple(map(lambda x: np.array(x.detach().cpu().numpy()), torch_sequence_x))
                        torch_res = torch_fn(*torch_sequence_x)
                        np_res = np_fn(*np_sequence_x)

                        torch_res = tuple(map(lambda x: x.cpu(), torch_res))
                        np_res = tuple(map(lambda x: torch.from_numpy(x), np_res))
                        self.assertEqual(np_res, torch_res)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_atleast(self, device, dtype):
        self._test_atleast_dim(torch.atleast_1d, np.atleast_1d, device, dtype)
        self._test_atleast_dim(torch.atleast_2d, np.atleast_2d, device, dtype)
        self._test_atleast_dim(torch.atleast_3d, np.atleast_3d, device, dtype)

    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False)))
    def test_argminmax_multiple(self, device, dtype):
        # Case: All Ones
        t = torch.ones(3, 3, device=device, dtype=dtype)
        self.compare_with_numpy(torch.argmax, np.argmax, t)
        self.compare_with_numpy(torch.argmin, np.argmin, t)

        # Case: With single `nan` present.
        if dtype in torch.testing.get_all_fp_dtypes():
            t[2, 2] = float('nan')
            self.compare_with_numpy(torch.argmax, np.argmax, t)
            self.compare_with_numpy(torch.argmin, np.argmin, t)

        # Case: Randomly Generated Tensors
        for ndims in range(1, 5):
            shape = self._rand_shape(ndims, min_size=5, max_size=10)
            for with_extremal in [False, True]:
                for contiguous in [False, True]:
                    # Generate Input.
                    x = self._generate_input(shape, dtype, device, with_extremal)

                    if dtype == torch.half:
                        max_val = torch.max(x.to(torch.float))
                        min_val = torch.min(x.to(torch.float))
                    else:
                        max_val = torch.max(x)
                        min_val = torch.min(x)

                    mask = torch.randn(x.shape) > 0.5
                    x[mask] = torch.tensor(max_val + 1, dtype=dtype)

                    mask = torch.randn(x.shape) > 0.5
                    x[mask] = torch.tensor(min_val - 1, dtype=dtype)

                    if not contiguous:
                        x = x.T

                    self.compare_with_numpy(torch.argmax, np.argmax, x, device=None, dtype=None)
                    self.compare_with_numpy(torch.argmin, np.argmin, x, device=None, dtype=None)

                    # Verify indices returned by max and min.
                    if dtype != torch.half:
                        rand_dim = random.randint(0, ndims - 1)
                        self.compare_with_numpy(lambda x: torch.max(x, dim=rand_dim)[1],
                                                lambda x: np.argmax(x, axis=rand_dim), x, device=None, dtype=None)
                        self.compare_with_numpy(lambda x: torch.min(x, dim=rand_dim)[1],
                                                lambda x: np.argmin(x, axis=rand_dim), x, device=None, dtype=None)

        def verify_against_numpy(t):
            # Argmax
            torch_fn = partial(torch.argmax, dim=1)
            np_fn = partial(np.argmax, axis=1)
            self.compare_with_numpy(torch_fn, np_fn, t)
            # Non-contiguous input
            self.compare_with_numpy(torch_fn, np_fn, t.T)

            # Verify indices returned by max.
            if dtype != torch.half:
                self.compare_with_numpy(lambda x: torch.max(x, dim=1)[1], np_fn, x, device=None, dtype=None)
                self.compare_with_numpy(lambda x: torch.max(x, dim=1)[1], np_fn, x.T, device=None, dtype=None)

            # Argmin
            torch_fn = partial(torch.argmin, dim=1)
            np_fn = partial(np.argmin, axis=1)
            self.compare_with_numpy(torch_fn, np_fn, t)
            # Non-contiguous input
            self.compare_with_numpy(torch_fn, np_fn, t.T)

            # Verify indices returned by min.
            if dtype != torch.half:
                self.compare_with_numpy(lambda x: torch.min(x, dim=1)[1], np_fn, x, device=None, dtype=None)
                self.compare_with_numpy(lambda x: torch.min(x, dim=1)[1], np_fn, x.T, device=None, dtype=None)

        # Case: Sample from issue: https://github.com/pytorch/pytorch/issues/41998
        t = torch.tensor([[1, 5],
                          [2, 10],
                          [3, 3]], device=device, dtype=dtype)
        verify_against_numpy(t)

        # Case: Sample from issue: https://github.com/pytorch/pytorch/issues/41998
        t = torch.tensor([[1, 5],
                          [2, 10],
                          [0, 0]], device=device, dtype=dtype)
        verify_against_numpy(t)

    def _test_special_stacks(self, dim, at_least_dim, torch_fn, np_fn, device, dtype):
        # Test error for non-tuple argument
        with self.assertRaisesRegex(TypeError, "must be tuple of Tensors, not Tensor"):
            torch_fn(torch.randn(10))

        # Test 0-D
        num_tensors = random.randint(1, 5)
        input_t = [torch.tensor(random.uniform(0, 10), device=device, dtype=dtype) for i in range(num_tensors)]
        actual = torch_fn(input_t)
        expected = np_fn([input.cpu().numpy() for input in input_t])
        self.assertEqual(actual, expected)

        for ndims in range(1, 5):
            base_shape = list(self._rand_shape(ndims, min_size=1, max_size=5))
            for i in range(ndims):
                shape = list(base_shape)
                num_tensors = random.randint(1, 5)
                torch_input = []
                # Create tensors with shape being different along one axis only
                for param in range(num_tensors):
                    shape[i] = random.randint(1, 5)
                    torch_input.append(self._generate_input(tuple(shape), dtype, device, with_extremal=False))

                # Determine if input tensors have valid dimensions.
                valid_dim = True
                for k in range(len(torch_input) - 1):
                    for tdim in range(ndims):
                        # Test whether all tensors have the same shape except in concatenating dimension
                        # Unless the number of dimensions is less than the corresponding at_least function dimension
                        # Since the original concatenating dimension would shift after applying at_least and would no
                        # longer be the concatenating dimension
                        if (ndims < at_least_dim or tdim != dim) and torch_input[k].size()[tdim] != torch_input[k + 1].size()[tdim]:
                            valid_dim = False

                # Special case for hstack is needed since hstack works differently when ndims is 1
                if valid_dim or (torch_fn is torch.hstack and ndims == 1):
                    # Valid dimensions, test against numpy
                    np_input = [input.cpu().numpy() for input in torch_input]
                    actual = torch_fn(torch_input)
                    expected = np_fn(np_input)
                    self.assertEqual(actual, expected)
                else:
                    # Invalid dimensions, test for error
                    with self.assertRaisesRegex(RuntimeError, "Sizes of tensors must match except in dimension"):
                        torch_fn(torch_input)
                    with self.assertRaises(ValueError):
                        np_input = [input.cpu().numpy() for input in torch_input]
                        np_fn(np_input)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_hstack(self, device, dtype):
        self._test_special_stacks(1, 1, torch.hstack, np.hstack, device, dtype)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_vstack(self, device, dtype):
        self._test_special_stacks(0, 2, torch.vstack, np.vstack, device, dtype)
        for i in range(5):
            # Test dimension change for 1D tensor of size (N) and 2D tensor of size (1, N)
            n = random.randint(1, 10)
            input_a = self._generate_input((n,), dtype, device, with_extremal=False)
            input_b = self._generate_input((1, n), dtype, device, with_extremal=False)
            torch_input = [input_a, input_b]
            np_input = [input.cpu().numpy() for input in torch_input]
            actual = torch.vstack(torch_input)
            expected = np.vstack(np_input)
            self.assertEqual(actual, expected)

    @onlyOnCPUAndCUDA
    @unittest.skipIf(not TEST_NUMPY, "NumPy not found")
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes(include_bfloat16=False) +
              torch.testing.get_all_complex_dtypes()))
    def test_dstack(self, device, dtype):
        self._test_special_stacks(2, 3, torch.dstack, np.dstack, device, dtype)
        for i in range(5):
            # Test dimension change for 1D tensor of size (N), 2D tensor of size (1, N), and 3D tensor of size (1, N, 1)
            n = random.randint(1, 10)
            input_a = self._generate_input((n,), dtype, device, with_extremal=False)
            input_b = self._generate_input((1, n), dtype, device, with_extremal=False)
            input_c = self._generate_input((1, n, 1), dtype, device, with_extremal=False)
            torch_input = [input_a, input_b, input_c]
            np_input = [input.cpu().numpy() for input in torch_input]
            actual = torch.dstack(torch_input)
            expected = np.dstack(np_input)
            self.assertEqual(actual, expected)

            # Test dimension change for 2D tensor of size (M, N) and 3D tensor of size (M, N, 1)
            m = random.randint(1, 10)
            n = random.randint(1, 10)
            input_a = self._generate_input((m, n), dtype, device, with_extremal=False)
            input_b = self._generate_input((m, n, 1), dtype, device, with_extremal=False)
            torch_input = [input_a, input_b]
            np_input = [input.cpu().numpy() for input in torch_input]
            actual = torch.dstack(torch_input)
            expected = np.dstack(np_input)
            self.assertEqual(actual, expected)

    @onlyOnCPUAndCUDA
    def test_repeated_dim(self, device):
        ops = [torch.mean, torch.sum, torch.nansum, torch.std, torch.logsumexp, torch.std, torch.var,
               torch.amin, torch.amax, torch.norm]
        x = torch.randn(3, 3, 3, 3, device=device)

        error_msg = r'appears multiple times in the list of dims'
        norm_error_msg = r'Expected dims to be different, got'
        for op in ops:
            for dim in [(0, 0), (0, -4)]:
                e_msg = norm_error_msg if op == torch.norm else error_msg
                with self.assertRaisesRegex(RuntimeError, e_msg):
                    op(x, dim=dim)

    @unittest.skipIf(TEST_WITH_ASAN, "Integer overflows are not allowed under ASAN")
    @dtypes(*torch.testing.get_all_dtypes())
    def test_muldiv_scalar(self, device, dtype):
        x = make_tensor((10, 3), device, dtype, low=None, high=None)
        s = make_tensor((1,), 'cpu', dtype, low=None, high=None).item()
        y = torch.full_like(x, s)
        self.assertEqual(x * s, x * y)
        self.assertEqual(s * x, y * x)
        self.assertEqual(x / s, x / y)
        self.assertEqual(s / x, y / x)

# Tests that compare a device's computation with the (gold-standard) CPU's.
class TestDevicePrecision(TestCase):
    exact_dtype = True

    # Note: ROCm fails when using float tensors
    @dtypes(torch.double)
    def test_polygamma(self, device, dtype):
        cpu_tensor = torch.randn(10, 10, 10, dtype=dtype)
        device_tensor = cpu_tensor.to(device)
        zeros = torch.zeros(10, 10, 10, dtype=dtype)
        for n in [0, 1, 2, 3, 4, 5]:
            cpu_out = cpu_tensor.polygamma(n)
            device_out = device_tensor.polygamma(n)
            norm_errors = (device_out - cpu_out.to(device)) / device_out
            self.assertEqual(norm_errors, zeros)

        cpu_tensor.requires_grad = True
        for n in [0, 1, 2, 3, 4, 5]:
            torch.autograd.gradcheck(lambda x: x.polygamma(n),
                                     cpu_tensor)

    # Note: fails when using float tensors
    @dtypes(torch.double)
    def test_digamma(self, device, dtype):
        cpu_tensor = torch.randn(10, 10, 10, dtype=dtype)
        device_tensor = cpu_tensor.to(device)
        zeros = torch.zeros(10, 10, 10, dtype=dtype)
        cpu_out = cpu_tensor.digamma()
        device_out = device_tensor.digamma()
        norm_errors = (device_out - cpu_out.to(device)) / device_out
        self.assertEqual(norm_errors, zeros)

        # Tests pole behavior
        cpu_tensor = torch.tensor([-0.999999994, -1.999999994, -2.0000000111,
                                   -100.99999994, -1931.99999994, 0.000000111,
                                   -0.000000111, 0, -1, -2, -931], dtype=dtype)
        expected_errors = torch.tensor([0, 0, 0, 0, 0, 0, 0, nan, nan, nan, nan], dtype=dtype)
        device_tensor = cpu_tensor.to(device)
        cpu_out = cpu_tensor.digamma()
        device_out = device_tensor.digamma()
        norm_errors = (device_out - cpu_out.to(device)) / device_out
        self.assertEqual(norm_errors, expected_errors)

    def test_var(self, device):
        cpu_tensor = torch.randn(2, 3, 3)
        device_tensor = cpu_tensor.to(device)
        self.assertEqual(device_tensor.var(), cpu_tensor.var())
        self.assertEqual(device_tensor.var(1), cpu_tensor.var(1))
        self.assertEqual(device_tensor.var(2), cpu_tensor.var(2))
        self.assertEqual(device_tensor.std(), cpu_tensor.std())
        self.assertEqual(device_tensor.std(1), cpu_tensor.std(1))
        self.assertEqual(device_tensor.var(2), cpu_tensor.var(2))

        cpu_tensor = torch.randn(100)
        device_tensor = cpu_tensor.to(device)
        self.assertEqual(device_tensor.var(), cpu_tensor.var())

    def test_var_large_input(self, device):
        # Large, not-nice input
        cpu_tensor = torch.randn(2 * 32 * 1024 + 1, 2, 67)
        device_tensor = cpu_tensor.to(device)

        self.assertEqual(cpu_tensor.var(2), device_tensor.var(2))

    @dtypesIfCUDA(torch.half, torch.float, torch.double)
    @dtypes(torch.float, torch.double)
    def test_device_rounding(self, device, dtype):
        # test half-to-even
        a = [-5.8, -3.5, -2.3, -1.5, -0.5, 0.5, 1.5, 2.3, 3.5, 5.8]
        res = [-6., -4., -2., -2., 0., 0., 2., 2., 4., 6.]

        a_tensor = torch.tensor(a, device=device).round()
        res_tensor = torch.tensor(res, device='cpu')
        self.assertEqual(a_tensor, res_tensor)

    @onlyCUDA
    @skipCUDAIfNotRocm
    def test_index_add_bfloat16(self, device):
        inp_tensor = torch.randn(5, 3, device='cpu').bfloat16()
        t = torch.tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], dtype=torch.bfloat16, device='cpu')
        index = torch.tensor([0, 4, 2], device='cpu')
        out_cpu = inp_tensor.index_add(0, index, t)

        inp_tensor = inp_tensor.to(device=device)
        t = t.to(device=device)
        index = index.to(device=device)
        out_gpu = inp_tensor.index_add(0, index, t)

        self.assertEqual(out_cpu, out_gpu, atol=1e-2, rtol=0)

    @dtypes(torch.double)
    def test_sum_noncontig(self, device, dtype):
        x = torch.randn(1, 75, 57, 20, dtype=dtype, device=device).permute(0, 3, 1, 2)
        y = x.cpu()
        self.assertEqual(x.sum().cpu(), y.sum())
        self.assertEqual(x.sum(dim=(-1, -2)).cpu(), y.sum(dim=(-1, -2)))
        self.assertEqual(x.sum(dim=(1, 3)).cpu(), y.sum(dim=(1, 3)))

    def test_device_serialization(self, device):
        x = torch.randn(4, 4, device=device)

        with tempfile.NamedTemporaryFile() as f:
            torch.save(x, f)
            f.seek(0)
            x_copy = torch.load(f)

        self.assertEqual(x_copy, x)
        self.assertIs(type(x_copy), type(x))
        self.assertEqual(x_copy.device, x.device)

    @deviceCountAtLeast(2)
    def test_multidevice_serialization(self, devices):
        x = [torch.randn(4, 4, device=devices[0]),
             torch.randn(4, 4, device=devices[1])]

        with tempfile.NamedTemporaryFile() as f:
            torch.save(x, f)
            f.seek(0)
            x_copy = torch.load(f)

        for original, cp in zip(x, x_copy):
            self.assertEqual(cp, original)
            self.assertIs(type(cp), type(original))
            self.assertEqual(cp.device, original.device)

    @deviceCountAtLeast(1)
    def test_copy_noncontig(self, devices):
        def do_test(d0, d1):
            x = torch.tensor([1.5, 2.5, 3.5, 4.5, 5.5, 6.5], device=d0)
            y = torch.tensor([0, 0, 0, 0, 0, 0], device=d1)
            self.assertNotEqual(x.dtype, y.dtype)

            y[::2].copy_(x[::2])
            self.assertEqual(y, [1, 0, 3, 0, 5, 0])

        do_test('cpu', devices[0])
        do_test(devices[0], 'cpu')

        if len(devices) > 1:
            do_test(devices[0], devices[1])

    @dtypes(torch.float, torch.double)
    def test_abs_zero(self, device, dtype):
        # Both abs(0.0) and abs(-0.0) should result in 0.0
        abs_zeros = torch.tensor([0.0, -0.0], device=device, dtype=dtype).abs().tolist()
        for num in abs_zeros:
            self.assertGreater(math.copysign(1.0, num), 0.0)

    @deviceCountAtLeast(2)
    def test_type_conversions_same_device(self, devices):
        x = torch.randn(5, 5, device=devices[1])
        self.assertEqual(x.int().device, torch.device(devices[1]))
        self.assertEqual(x.type(torch.int).device, torch.device(devices[1]))
        self.assertEqual(x.to(torch.int).device, torch.device(devices[1]))

    def test_min_max_nan(self, device):
        tests = [(lambda x: x.min(), 'min'),
                 (lambda x: x.max(), 'max'),
                 (lambda x: x.amin(), 'amin'),
                 (lambda x: x.amax(), 'amax'),
                 (lambda x: x.min(0).values, 'min_dim'),
                 (lambda x: x.max(0).values, 'max_dim'),
                 (lambda x: x.amin(0), 'amin_dim'),
                 (lambda x: x.amax(0), 'amax_dim')]
        for f, name in tests:
            a = torch.arange(25.0).view(5, 5)
            a[2, 2] = nan
            actual = f(a.to(device)).cpu()
            expected = f(a).cpu()
            self.assertEqual(torch.isnan(actual), torch.isnan(expected), msg='nans for {}'.format(name))
            self.assertEqual(actual[~torch.isnan(actual)],
                             expected[~torch.isnan(expected)], msg='nans for {}'.format(name))

    @dtypesIfCUDA(torch.half, torch.float, torch.double,
                  torch.int8, torch.short, torch.int, torch.long,
                  torch.uint8)
    @dtypes(torch.float, torch.double,
            torch.int8, torch.short, torch.int, torch.long,
            torch.uint8)
    def test_from_sequence(self, device, dtype):
        seq = [list(range(i * 4, i * 4 + 4)) for i in range(5)]
        reference = torch.arange(0, 20).resize_(5, 4)
        self.assertEqual(torch.tensor(seq, dtype=dtype, device=device), reference, exact_dtype=False)

    def test_cat(self, device):
        SIZE = 10
        for dim in range(-3, 3):
            pos_dim = dim if dim >= 0 else 3 + dim
            x = torch.rand(13, SIZE, SIZE, device=device).transpose(0, pos_dim)
            y = torch.rand(17, SIZE, SIZE, device=device).transpose(0, pos_dim)
            z = torch.rand(19, SIZE, SIZE, device=device).transpose(0, pos_dim)

            res1 = torch.cat((x, y, z), dim)
            self.assertEqual(res1.narrow(pos_dim, 0, 13), x, atol=0, rtol=0)
            self.assertEqual(res1.narrow(pos_dim, 13, 17), y, atol=0, rtol=0)
            self.assertEqual(res1.narrow(pos_dim, 30, 19), z, atol=0, rtol=0)

        x = torch.randn(20, SIZE, SIZE, device=device)
        self.assertEqual(torch.cat(torch.split(x, 7)), x)
        self.assertEqual(torch.cat(torch.chunk(x, 7)), x)

        y = torch.randn(1, SIZE, SIZE, device=device)
        z = torch.cat([x, y])
        self.assertEqual(z.size(), (21, SIZE, SIZE))

    def test_sum_cpu_device_mismatch(self, device):
        x = torch.randn(20, dtype=torch.float32, device=device)
        y = torch.randn(1, dtype=torch.float32)

        err_string = "Expected all tensors to be on the same device, but found at least two devices, {0}".format(device)

        with self.assertRaisesRegex(RuntimeError, err_string):
            torch.sum(x, dim=[0], dtype=torch.float32, out=y)

        # tests half to float promotion
        if self.device_type == 'cuda':
            x = x.half()
            with self.assertRaisesRegex(RuntimeError, err_string):
                torch.sum(x, dim=[0], dtype=torch.float32, out=y)

    @deviceCountAtLeast(1)
    def test_advancedindex_mixed_cpu_devices(self, devices) -> None:
        def test(x: torch.Tensor, ia: torch.Tensor, ib: torch.Tensor) -> None:
            # test getitem
            self.assertEqual(x[:, ia, None, ib, 0].cpu(),
                             x.cpu()[:, ia.cpu(), None, ib.cpu(), 0])
            self.assertEqual(x[ia], x.cpu()[ia.cpu()])
            # test setitem
            x_clone1 = x.clone()
            x_clone2 = x.clone()
            first_shape = x[:, ia, None, ib, 0].shape
            second_shape = x[ia].shape
            x_clone1[:, ia, None, ib, 0] = torch.randn(first_shape).to(x_clone1)
            x_clone2[ia] = torch.randn(second_shape).to(x_clone2)

        cpu = torch.device('cpu')
        for device in devices:
            # Index cpu tensor with device tensor
            x = torch.randn(3, 4, 4, 4, 3)
            ia = torch.tensor([0, 2, 1]).to(device)
            ib = torch.tensor([0, 2, 1]).to(device)
            test(x, ia, ib)

            # Index device tensor with cpu tensor
            x = x.to(device)
            ia = ia.to(cpu)
            ib = ib.to(cpu)
            test(x, ia, ib)

            # Index cpu tensor with mixed cpu, device tensors
            x = x.to(cpu)
            ia = ia.to(cpu)
            ib = ib.to(device)
            test(x, ia, ib)

            # Index device tensor with mixed cpu, device tensors
            x = x.to(device)
            ia = ia.to(cpu)
            ib = ib.to(device)
            test(x, ia, ib)

            if len(devices) > 1:
                other_device = devices[0]
                if device == devices[0]:
                    other_device = devices[1]
                # Index device tensor with mixed cpu, device tensors on different devices
                x = x.to(device)
                ia = ia.to(cpu)
                ib = ib.to(other_device)
                test(x, ia, ib)

    def test_copy_broadcast(self, device) -> None:
        x = torch.randn(10, 5)
        y = torch.randn(5, device=device)
        x.copy_(y)
        self.assertEqual(x[3], y)

        x = torch.randn(10, 5, device=device)
        y = torch.randn(5)
        x.copy_(y)
        self.assertEqual(x[3], y)

    def test_solve_methods_arg_device(self, device):
        for b_device, A_device in product(['cpu', device], repeat=2):
            if b_device == A_device:
                continue

            b = torch.randn(3, 1, device=b_device)
            A = torch.randn(3, 3, device=A_device)
            err_str = "Expected b and A to be on the same device"
            with self.assertRaisesRegex(RuntimeError, err_str):
                torch.solve(b, A)

            with self.assertRaisesRegex(RuntimeError, err_str):
                torch.cholesky_solve(b, A)

            with self.assertRaisesRegex(RuntimeError, err_str):
                torch.triangular_solve(b, A)

            # b and A have to be modified to match accepted inputs sizes for lu_solve
            b = b.unsqueeze(0)
            A = A.unsqueeze(0)
            with self.assertRaisesRegex(RuntimeError, err_str):
                torch.lu_solve(b, A, torch.rand(A.shape[:-1], device=A_device).int())

            # This checks if a suitable error message is thrown
            # when LU output and pivots are on the same device
            with self.assertRaisesRegex(RuntimeError,
                                        "Expected LU_pivots and LU_data to be on the same device"):
                torch.lu_solve(b, A, torch.rand(A.shape[:-1], device=b_device).int())


# Tests ops and indexing to ensure they return views (and new tensors) as
# appropriate.
class TestViewOps(TestCase):
    exact_dtype = True

    def is_view_of(self, base, other):
        if (not other._is_view() or
                other is base or
                other._base is not base or
                base.device != other.device):
            return False
        # Note: only validates storage on native device types
        # because some accelerators, like XLA, do not expose storage
        if base.device.type == 'cpu' or base.device.type == 'cuda':
            if base.storage().data_ptr() != other.storage().data_ptr():
                return False

        return True

    # Performs transpose if contiguous=True, else returns the input tensor as is
    def _do_transpose(self, x, contiguous=False, dim0=0, dim1=1):
        if contiguous:
            return x
        else:
            return x.transpose(dim0, dim1)

    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_conj_self(self, device, dtype):
        t = torch.ones(5, 5, device=device)
        s = t.conj()
        self.assertTrue(s is t)

    @onlyOnCPUAndCUDA
    def test_view_as_complex(self, device):
        def fn(contiguous_input=True, dim0=0, dim1=1):
            t = torch.randn(3, 2, 2, device=device)
            c_t = t[:, :, 0] + 1j * t[:, :, 1]

            input = self._do_transpose(t, contiguous_input, dim0, dim1)

            if input.size()[-1] != 2:
                self.assertRaisesRegex(
                    RuntimeError, "Tensor must have a last dimension of size 2",
                    lambda: torch.view_as_complex(input))
                return

            if input.stride()[-1] != 1:
                self.assertRaisesRegex(
                    RuntimeError, "Tensor must have a last dimension with stride 1",
                    lambda: torch.view_as_complex(input))
                return

            res = torch.view_as_complex(input)
            self.assertEqual(res, self._do_transpose(c_t, contiguous_input, dim0, dim1))
            self.assertTrue(self.is_view_of(t, res))

        fn()
        fn(contiguous_input=False)
        # RuntimeError since in this case the last dim of input would not be of size 2
        fn(contiguous_input=False, dim0=0, dim1=2)
        # RuntimeError since in this case the last dim of input would not have stride 1
        fn(contiguous_input=False, dim0=1, dim1=2)


        # RuntimeError since in this case the stride of non-last dim of input would not be of size 2
        x = torch.randn(3, 3, device=device)
        t = torch.as_strided(x, (2, 2), (1, 1))
        self.assertRaisesRegex(
            RuntimeError, "Tensor must have a stride divisible by 2 for all but last dimension",
            lambda: torch.view_as_complex(t))

        # tensor with zero elements
        x = torch.tensor([], device=device)  # torch.Size([0])
        self.assertRaisesRegex(
            RuntimeError, "Tensor must have a last dimension of size 2",
            lambda: torch.view_as_complex(x))

        # zero dimension tensor
        z = torch.tensor(2.0)
        self.assertRaisesRegex(
            RuntimeError, "Input tensor must have one or more dimensions",
            lambda: torch.view_as_complex(z))

        y = x.reshape(0, 2)  # torch.Size([0, 2])
        res = torch.view_as_complex(y)
        self.assertTrue(self.is_view_of(x, res))
        self.assertEqual(res.shape, torch.Size([0]))

    @onlyOnCPUAndCUDA
    @dtypes(*torch.testing.get_all_complex_dtypes(include_complex32=True))
    def test_view_as_real(self, device, dtype):
        def fn(contiguous_input=True):
            t = torch.randn(3, 4, dtype=dtype, device=device)
            input = self._do_transpose(t, contiguous_input)
            res = torch.view_as_real(input)
            self.assertEqual(res[:, :, 0], input.real)
            self.assertEqual(res[:, :, 1], input.imag)
            # TODO: Add torch.ComplexHalfStorage
            if dtype != torch.complex32:
                self.assertTrue(self.is_view_of(t, res))
            else:
                self.assertRaises(RuntimeError, lambda: self.is_view_of(t, res))

        fn()
        fn(contiguous_input=False)

        # tensor with zero elements
        x = torch.tensor([], dtype=dtype, device=device)
        res = torch.view_as_real(x)
        # TODO: Add torch.ComplexHalfStorage
        if dtype != torch.complex32:
            self.assertTrue(self.is_view_of(x, res))
        else:
            self.assertRaises(RuntimeError, lambda: self.is_view_of(x, res))
        self.assertEqual(res.shape, torch.Size([0, 2]))

        # tensor with zero dim
        x = torch.tensor(2 + 3j, dtype=dtype, device=device)
        res = torch.view_as_real(x)
        # TODO: Add torch.ComplexHalfStorage
        if dtype != torch.complex32:
            self.assertTrue(self.is_view_of(x, res))
        else:
            self.assertRaises(RuntimeError, lambda: self.is_view_of(x, res))
        self.assertEqual(res.shape, torch.Size([2]))

    @onlyOnCPUAndCUDA
    @dtypes(*(torch.testing.get_all_int_dtypes() + torch.testing.get_all_fp_dtypes()))
    def test_real_imag_noncomplex(self, device, dtype):
        t = torch.ones((5, 5), dtype=dtype, device=device)

        with self.assertRaises(RuntimeError):
            torch.real(t)

        with self.assertRaises(RuntimeError):
            torch.imag(t)

    @unittest.skipIf(not TEST_NUMPY, "Numpy not found")
    @onlyOnCPUAndCUDA
    @dtypes(*torch.testing.get_all_complex_dtypes())
    def test_real_imag_view(self, device, dtype):
        def compare_with_numpy(contiguous_input=True):
            t = torch.randn(3, 3, dtype=dtype, device=device)
            if not contiguous_input:
                u = t.T
            else:
                u = t

            re = u.real
            exp = torch.from_numpy(u.cpu().numpy().real).to(device=device)
            self.assertEqual(re, exp)
            # for the case of contiguous_input, t=u
            # for the case of non contiguous_input, the base still remains
            # t since we are performing a view operation to make the input non-contiguous
            self.assertTrue(self.is_view_of(t, re))

            im = u.imag
            exp = torch.from_numpy(u.cpu().numpy().imag).to(device=device)
            self.assertEqual(im, exp)
            self.assertTrue(self.is_view_of(t, im))

        compare_with_numpy()
        compare_with_numpy(contiguous_input=False)

        # ensure storage offset is being correctly set
        a = torch.randn(10, dtype=dtype)
        self.assertEqual(a[5:].real, a.real[5:])
        self.assertEqual(a[5:].imag, a.imag[5:])

    @onlyOnCPUAndCUDA
    @dtypes(*product(torch.testing.get_all_complex_dtypes(), torch.testing.get_all_dtypes()))
    @suppress_warnings
    def test_set_real_imag(self, device, dtypes):
        x = torch.randn(10, dtype=dtypes[0], device=device)

        new_real = _make_tensor((10,), dtypes[1], device)
        new_imag = _make_tensor((10,), dtypes[1], device)

        x.real = new_real
        x.imag = new_imag

        if dtypes[1].is_complex:
            self.assertEqual(x.real, new_real.real, exact_dtype=False)
            self.assertEqual(x.imag, new_imag.real, exact_dtype=False)

        else:
            self.assertEqual(x.real, new_real, exact_dtype=False)
            self.assertEqual(x.imag, new_imag, exact_dtype=False)

    def test_diagonal_view(self, device) -> None:
        t = torch.ones((5, 5), device=device)
        v = torch.diagonal(t)
        self.assertTrue(self.is_view_of(t, v))

        v[0] = 0
        self.assertEqual(t[0, 0], v[0])

        t = torch.ones((3, 3, 3), device=device)
        v = torch.diagonal(t, offset=1, dim1=1, dim2=2)
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0] = 0
        self.assertEqual(t[0, 0, 1], v[0, 0])

    def test_select_view(self, device) -> None:
        t = torch.ones((5, 5), device=device)
        v = t.select(0, 2)
        self.assertTrue(self.is_view_of(t, v))

        v[0] = 0
        self.assertEqual(t[2, 0], v[0])

    def test_unbind_view(self, device) -> None:
        t = torch.zeros((5, 5), device=device)
        tup = torch.unbind(t)

        for idx, v in enumerate(tup):
            self.assertTrue(self.is_view_of(t, v))

            v[0] = idx + 1
            self.assertEqual(t[idx, 0], v[0])

    def test_expand_view(self, device) -> None:
        t = torch.ones((5, 1), device=device)
        v = t.expand(5, 5)
        self.assertTrue(self.is_view_of(t, v))

        v[2, 2] = 0
        self.assertEqual(t[2, 0], v[2, 2])

    def test_expand_as_view(self, device):
        t = torch.ones((5, 1), device=device)
        e = torch.empty((5, 5), device=device)
        v = t.expand_as(e)
        self.assertTrue(self.is_view_of(t, v))

        v[2, 2] = 0
        self.assertEqual(t[2, 0], v[2, 2])

    def test_narrow_view(self, device):
        t = torch.ones((5, 5), device=device)
        v = torch.narrow(t, 1, 2, 2)
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0] = 0
        self.assertEqual(t[0, 2], v[0, 0])

    def test_permute_view(self, device) -> None:
        t = torch.ones((5, 5), device=device)
        v = t.permute(1, 0)
        self.assertTrue(self.is_view_of(t, v))

        v[0, 1] = 0
        self.assertEqual(t[1, 0], v[0, 1])

    def test_transpose_view(self, device):
        t = torch.ones((5, 5), device=device)
        v = torch.transpose(t, 0, 1)
        self.assertTrue(self.is_view_of(t, v))

        v[0, 1] = 0
        self.assertEqual(t[1, 0], v[0, 1])

    def test_t_view(self, device):
        t = torch.ones((5, 5), device=device)
        v = t.t()
        self.assertTrue(self.is_view_of(t, v))

        v[0, 1] = 0
        self.assertEqual(t[1, 0], v[0, 1])

    def test_T_view(self, device):
        t = torch.ones((5, 5), device=device)
        v = t.T
        self.assertTrue(self.is_view_of(t, v))

        v[0, 1] = 0
        self.assertEqual(t[1, 0], v[0, 1])

    def test_unfold_view(self, device):
        t = torch.ones(10, device=device)
        v = t.unfold(0, 3, 2)
        self.assertTrue(self.is_view_of(t, v))

        v[1, 0] = 0
        self.assertEqual(t[2], v[1, 0])

    def test_squeeze_view(self, device):
        t = torch.ones(5, 1, 5, device=device)
        v = torch.squeeze(t)
        self.assertTrue(self.is_view_of(t, v))
        v[0, 1] = 0
        self.assertEqual(t, v._base)

    def test_unsqueeze_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = torch.unsqueeze(t, 1)
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0, 1] = 0
        self.assertEqual(t[0, 1], v[0, 0, 1])

    def test_as_strided_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = torch.as_strided(t, (25,), (1,))
        self.assertTrue(self.is_view_of(t, v))

        v[6] = 0
        self.assertEqual(t[1, 1], v[6])

    def test_view_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = t.view(25)
        self.assertTrue(self.is_view_of(t, v))

        v[6] = 0
        self.assertEqual(t[1, 1], v[6])

    def test_view_as_view(self, device):
        t = torch.ones(5, 5, device=device)
        e = torch.empty((25,))
        v = t.view_as(e)
        self.assertTrue(self.is_view_of(t, v))

        v[6] = 0
        self.assertEqual(t[1, 1], v[6])

    def test_contiguous_self(self, device):
        t = torch.ones(5, 5, device=device)
        s = t.contiguous()
        self.assertTrue(s is t)

    def test_contiguous_nonview(self, device):
        t = torch.ones(5, 5, device=device)
        nv = t.t().contiguous()
        self.assertTrue(not self.is_view_of(t, nv))

        nv[0, 0] = 0
        self.assertNotEqual(t[0, 0], nv[0, 0])

    def test_reshape_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = torch.reshape(t, (25,))
        self.assertTrue(self.is_view_of(t, v))

        v[6] = 0
        self.assertEqual(t[1, 1], v[6])

    def test_reshape_as_view(self, device):
        t = torch.ones(5, 5, device=device)
        e = torch.empty((25,), device=device)
        v = t.reshape_as(e)
        self.assertTrue(self.is_view_of(t, v))

        v[6] = 0
        self.assertEqual(t[1, 1], v[6])

    def test_reshape_nonview(self, device):
        t = torch.ones(5, 5, device=device)
        nv = torch.reshape(t.t(), (25,))
        self.assertTrue(not self.is_view_of(t, nv))

        nv[6] = 0
        self.assertNotEqual(t[1, 1], nv[6])

    def test_basic_indexing_slice_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = t[:2, :3]
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0] = 0
        self.assertEqual(t[0, 0], v[0, 0])

    def test_basic_indexing_ellipses_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = t[..., :2]
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0] = 0
        self.assertEqual(t[0, 0], v[0, 0])

    def test_basic_indexing_newaxis_view(self, device):
        t = torch.ones(5, 5, device=device)
        v = t[None, :2, 3]
        self.assertTrue(self.is_view_of(t, v))

        v[0, 0] = 0
        self.assertEqual(t[0, 3], v[0, 0])

    def test_advanced_indexing_nonview(self, device):
        t = torch.ones(3, 3, device=device)
        rows = torch.tensor([[0, 0], [2, 2]], device=device)
        cols = torch.tensor([[0, 1], [2, 2]], device=device)
        nv = t[rows, cols]
        self.assertTrue(not self.is_view_of(t, nv))

        nv[1, 1] = 0
        self.assertNotEqual(t[2, 2], nv[1, 1])

    def test_advanced_indexing_assignment(self, device):
        t = torch.ones(3, 3, device=device)
        rows = torch.tensor([[0, 0], [2, 2]], device=device)
        cols = torch.tensor([[0, 1], [2, 2]], device=device)
        t[rows, cols] = 0
        self.assertEqual(t[2, 2], 0)

    @unittest.skip("See https://github.com/pytorch/pytorch/pull/32720")
    def test_chunk_view(self, device):
        t = torch.zeros(3, 3, device=device)
        l = torch.chunk(t, 3)

        for idx, v in enumerate(l):
            self.assertTrue(self.is_view_of(t, v))

            v[0, 0] = idx + 1
            self.assertEqual(t[idx, 0], v[0, 0])

    @unittest.skip("See https://github.com/pytorch/pytorch/pull/32720")
    def test_split_view(self, device):
        t = torch.zeros(3, 3, device=device)
        l = torch.split(t, [1, 1, 1])

        for idx, v in enumerate(l):
            self.assertTrue(self.is_view_of(t, v))

            v[0, 0] = idx + 1
            self.assertEqual(t[idx, 0], v[0, 0])

    def test_movedim_view(self, device):
        t = torch.zeros(3, 3, device=device)
        out = torch.movedim(t, (0, 1), (1, 0))

        self.assertTrue(self.is_view_of(t, out))

        # Randomly change values in output
        # and verify that original is changed
        # as well.
        for _ in range(3):
            idx_1, idx_2 = random.randint(0, 2), random.randint(0, 2)
            out[idx_1, idx_2] = random.random()
            self.assertEqual(t[idx_2, idx_1], out[idx_1, idx_2])

# Below are fixtures and functions that generate tensor op comparison tests
# These tests run a single op on both a CPU and device tensor and compare the
# the results. In-place variants of the ops can also be run.

# Lists of dtypes to instantiate tensor op test variants.
_types = [
    torch.half, torch.float, torch.double,
    torch.int8, torch.short, torch.int, torch.long,
    torch.uint8
]

_types_no_half = [
    torch.float, torch.double,
    torch.int8, torch.short, torch.int, torch.long,
    torch.uint8
]

# _types2 adds bfloat16 type to  _types only on ROCm. Should eventually be unified
# with _types when bfloat16 bringup is complete on all platforms.
_types2 = _types + [torch.bfloat16] if TEST_WITH_ROCM else _types

_float_types = [torch.half, torch.float, torch.double]

_complex_types = [torch.cfloat, torch.cdouble]

_complex_types_skip_rocm = [] if TEST_WITH_ROCM else _complex_types

_float_types_no_half = [torch.float, torch.double]

# _float_types2 adds bfloat16 type to _float_types only on ROCm. Should eventually be unified
# with _float_types when bfloat16 bringup is complete on all platforms
_float_types2 = _float_types + [torch.bfloat16] if TEST_WITH_ROCM else _float_types

_signed_types = [
    torch.half, torch.float, torch.double,
    torch.int8, torch.short, torch.int, torch.long
]

_signed_types_no_half = [
    torch.float, torch.double,
    torch.int8, torch.short, torch.int, torch.long
]

_integer_types = [
    torch.uint8, torch.int8, torch.int16,
    torch.int32, torch.int64
]

_cpu_types: List[torch.dtype] = []

_unsigned_types = [torch.uint8]

# Binary Float Ops
# Operators which use TensorIterator::binary_float_op
# These Ops promote integer inputs to Float.
binary_float_ops_inplace = ['atan2_', 'div_']

# Helper values and functions for producing tensors and scalars to use in tensor op tests.
# Tensor dimension sizes (Small, Medium, Large, Giant)
_S = 5
_M = 50
_L = 1000
_G = 275000000

# Value to clamp divisors to since dividing by small numbers can be unstable
# on devices.
_div_min = 2**-8

# Returns floating or integral scalar corresponding to dtype
def _number(floating, integer, dtype):
    if dtype in [torch.half, torch.float, torch.double, torch.bfloat16]:
        return floating
    return integer

# Converts half/bfloat16 dtype to float when device is cpu
def _convert_t(dtype, device):
    if device == 'cpu' and dtype in {torch.half, torch.bfloat16}:
        return torch.float
    return dtype

# Returns a tensor of the requested shape, dtype, and device
# Requesting a half CPU tensor returns a float CPU tensor with
# values representable by a half.
# Initialization uses randint for non-float types and randn for float types.
def _make_tensor(shape, dtype, device, fill_ones=False) -> torch.Tensor:
    # Returns a tensor filled with ones
    if fill_ones:
        return torch.ones(*shape, dtype=_convert_t(dtype, device), device=device)

    # Returns a tensor with random integer values
    if not (dtype.is_floating_point or dtype.is_complex):
        t = torch.randint(0, 10, shape, device=device)
        if dtype != torch.uint8:
            t = t - 5  # generate negative values also
        return t.to(_convert_t(dtype, device))

    # Populates the CPU tensor with floats representable as half/bfloat16
    if dtype == torch.half and device == 'cpu':
        return torch.randn(*shape, dtype=torch.float, device=device).half().float()
    if dtype == torch.bfloat16 and device == 'cpu':
        return torch.randn(*shape, dtype=torch.float, device=device).bfloat16().float()

    # Default: returns a tensor with random float values
    return torch.randn(shape, dtype=dtype, device=device).to(dtype=dtype)

def _small_0d(dtype, device) -> torch.Tensor:
    return _make_tensor((1,), dtype, device).squeeze()

def _small_2d(dtype, device, has_zeros=True, fill_ones=False, oneish=False):
    t = _make_tensor((_S, _S), dtype, device, fill_ones=fill_ones)
    if oneish:
        return t.clamp(min=_number(.99, 1, dtype), max=1.01)
    if not has_zeros:
        return t.clamp(min=(_number(_div_min, 1, dtype)))
    return t

def _small_3d(dtype, device, has_zeros=True, fill_ones=False, oneish=False):
    t = _make_tensor((_S, _S, _S), dtype, device, fill_ones=fill_ones)
    if oneish:
        return t.clamp(min=_number(.99, 1, dtype), max=1.01)
    if not has_zeros:
        return t.clamp(min=(_number(_div_min, 1, dtype)))
    return t

def _small_3d_ones(dtype, device):
    return _small_3d(dtype, device, fill_ones=True)

def _small_3d_unique(dtype, device):
    return (torch.randperm(_S * _S * _S,
                           dtype=_convert_t(dtype, device), device=device) + 1).view(_S, _S, _S)

def _medium_1d(dtype, device):
    return _make_tensor((_M,), dtype, device)

def _medium_2d(dtype, device):
    return _make_tensor((_M, _M), dtype, device)

def _large_2d(dtype, device):
    t = _make_tensor((_L, _L), dtype, device)
    return t.normal_()

def _giant_1d(dtype, device):
    return _make_tensor((_G), dtype, device)

# Helper method that returns a function which takes dtype and device and
# instantiates tensors of the given shape.
# Useful for tensor op tests with custom shapes.
def _new_t(shape):
    def tmp(dtype, device):
        return _make_tensor(shape, dtype, device)
    return tmp

def _wrap_maybe_warns(regex):
    def decorator(fn):
        def inner(self, device, dtype):
            with self.maybeWarnsRegex(UserWarning, regex):
                fn(self, device, dtype)
        return inner
    return decorator


# TODO: random functions, cat, gather, scatter, index*, masked*,
#       resize, resizeAs, storage_offset, storage, stride, unfold
# Each tests is defined in tensor_op_tests as a tuple of:
# - op name (string)
# - (sub)test name (string)
# - tensor constructor, takes dtype and device and constructs the tensor to run the op on
# - arg constructor, takes dtype and device and constructs op arguments
# - torch.half precision (=1e-5)
# - torch.bfloat16 precision (=1e-5)
# - precision (=1e-5), precision to use for all other dtypes
# - dtype_list (=_types), a list of torch dtypes to test the op(s) with
# - cpu_dtype_list (=[]), a list of torch dtypes to test the op(s) on cpu
# - make_inplace_variant (=True), if true the inplace version of the op (op_) is also tested
# - decorators (=[]), a list of decorators to apply to the test
# - self_position (=-1), the position of self in the arg list, -1 means skip function check
# - test_out (=False), whether to test the out= version of the operator
tensor_op_tests = [
    ('add', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-2),
    ('add', 'tensor', _small_3d, lambda t, d: [_small_3d(t, d)], 1e-2),
    ('sub', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-2),
    ('sub', 'tensor', _small_3d, lambda t, d: [_small_3d(t, d)], 1e-2),
    ('mul', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-2),
    ('mul', 'tensor', _small_3d, lambda t, d: [_small_3d(t, d)], 1e-2),
    ('mul', 'scalar', _small_0d, lambda t, d: [_small_0d(torch.int32, d)], 1e-2),
    ('div', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-1,
        1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('div', 'tensor', _small_3d,
        lambda t, d: [_small_3d(t, d, has_zeros=False)], 1e-1,
        1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('true_divide', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-1,
        1e-5, 1e-5, _types, _cpu_types, False),
    ('true_divide', 'with_inplace', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-1,
        1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('true_divide', 'tensor', _small_3d,
        lambda t, d: [_small_3d(t, d, has_zeros=False)], 1e-1,
        1e-5, 1e-5, _types, _cpu_types, False),
    ('true_divide', 'tensor_with_inplace', _small_3d,
        lambda t, d: [_small_3d(t, d, has_zeros=False)], 1e-1,
        1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('floor_divide', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1, 1e-5, 1e-5, _types),
    ('floor_divide', 'tensor', _small_3d,
        lambda t, d: [_small_3d(t, d, has_zeros=False)], 1, 1e-5, 1e-5, _types),
    ('pow', '', _small_3d, lambda t, d: [_number(3.14, 3, t)], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('pow', '1', _small_3d, lambda t, d: [_number(1., 1, t)], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('pow', '2', _small_3d, lambda t, d: [_number(2., 2, t)], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('pow', '3', _small_3d, lambda t, d: [_number(3., 3, t)], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('pow', '-1', _small_3d, lambda t, d: [_number(-1., -1, t)], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('pow', '-2', _small_3d, lambda t, d: [_number(-2., -2, t)],
        1e-1, 1e-5, 1e-5, _float_types_no_half, _cpu_types, False),
    ('pow', 'tensor', _small_3d, lambda t, d: [_small_3d(t, d).abs()],
        1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('addbmm', '', _small_2d, lambda t, d: [_small_3d(t, d), _small_3d(t, d)],
        1e-1, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types,
        _cpu_types, True, [tf32_on_and_off(0.01)]),
    ('addbmm', 'scalar', _small_2d, lambda t, d: [_number(0.4, 2, t), _small_3d(t, d), _small_3d(t, d)],
        1e-1, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types, _cpu_types, True,
        [tf32_on_and_off(0.01), _wrap_maybe_warns("This overload of addbmm_? is deprecated")]),
    ('addbmm', 'two_scalars', _small_2d, lambda t, d: [_number(0.5, 3, t), _number(0.4, 2, t), _small_3d(t, d), _small_3d(t, d)],
        1e-1, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types, _cpu_types, True,
        [tf32_on_and_off(0.01), _wrap_maybe_warns("This overload of addbmm_? is deprecated")]),
    ('baddbmm', '', _small_3d, lambda t, d: [_small_3d(t, d), _small_3d(t, d)],
        1e-2, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM)),
    ('baddbmm', 'scalar', _small_3d, lambda t, d: [_number(0.4, 2, t), _small_3d(t, d), _small_3d(t, d)],
        1e-2, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM), _cpu_types, True,
        [_wrap_maybe_warns("This overload of baddbmm_? is deprecated")]),
    ('baddbmm', 'two_scalars', _small_3d, lambda t, d: [_number(0.5, 3, t), _number(0.4, 2, t), _small_3d(t, d), _small_3d(t, d)],
        1e-2, 1e-1, 1e-4, torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM), _cpu_types, True,
        [_wrap_maybe_warns("This overload of baddbmm_? is deprecated")]),
    ('bmm', '', _small_3d, lambda t, d: [_small_3d(t, d)],
        1e-5, 1e-5, 1e-5, _float_types_no_half, _cpu_types, False),
    ('addcdiv', '', _small_2d,
        lambda t, d: [_small_2d(t, d),
                      _small_2d(t, d, has_zeros=False)], 1, 1, 1e-3,
        torch.testing.get_all_fp_dtypes(), _cpu_types, True),
    ('addcdiv', 'scalar', _small_2d,
        lambda t, d: [_number(2.8, 1, t), _small_2d(t, d),
                      _small_2d(t, d, has_zeros=False)], 1, 1e-5, 1e-3,
        _float_types, _cpu_types, True),
    ('addcmul', '', _small_3d, lambda t, d: [_small_3d(t, d), _small_3d(t, d)], 1e-2, 1e-1, 1e-3,
        torch.testing.get_all_dtypes(include_complex=False, include_bool=False)),
    ('addcmul', 'scalar', _small_3d,
        lambda t, d: [_number(0.4, 2, t), _small_3d(t, d), _small_3d(t, d)], 1e-2,
        1e-1, 1e-5, torch.testing.get_all_dtypes(include_complex=False, include_bool=False), _cpu_types, True,
        [_wrap_maybe_warns("This overload of addcmul_? is deprecated")]),
    ('addmm', '', _medium_2d, lambda t, d: [_medium_2d(t, d), _medium_2d(t, d)], 1e-1, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM),
        _cpu_types, True, [tf32_on_and_off(0.01)], 0, True),
    ('addmm', 'scalar', _medium_2d,
        lambda t, d: [_number(0.4, 2, t), _medium_2d(t, d), _medium_2d(t, d)], 1e-1, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM), _cpu_types, True,
        [tf32_on_and_off(0.01), _wrap_maybe_warns("This overload of addmm_? is deprecated")]),
    ('addmm', 'two_scalars', _medium_2d,
        lambda t, d: [_number(0.5, 3, t), _number(0.4, 2, t), _medium_2d(t, d), _medium_2d(t, d)], 1e-1, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM), _cpu_types, True,
        [tf32_on_and_off(0.01), _wrap_maybe_warns("This overload of addmm_? is deprecated")]),
    ('addmv', '', _medium_1d, lambda t, d: [_medium_2d(t, d), _medium_1d(t, d)], 1e-2, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types_skip_rocm, _cpu_types,
        True, [], 0, True),
    ('addmv', 'scalar', _medium_1d,
        lambda t, d: [_number(0.4, 2, t), _medium_2d(t, d), _medium_1d(t, d)], 1e-2, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types_skip_rocm, _cpu_types, True,
        [_wrap_maybe_warns("This overload of addmv_? is deprecated")]),
    ('addmv', 'two_scalars', _medium_1d,
        lambda t, d: [_number(0.5, 3, t), _number(0.4, 2, t), _medium_2d(t, d), _medium_1d(t, d)], 1e-2, 1e-1, 1e-4,
        torch.testing.get_all_fp_dtypes(include_bfloat16=AMPERE_OR_ROCM) + _complex_types_skip_rocm, _cpu_types, True,
        [_wrap_maybe_warns("This overload of addmv_? is deprecated")]),
    ('atan2', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-2, 1e-5, 1e-5, _types, _types_no_half),
    ('angle', '', _small_3d, lambda t, d: [], 0, 0, 0, _types_no_half, [torch.bfloat16], False),
    ('fmod', 'value', _small_3d, lambda t, d: [3], 1e-3),
    ('fmod', 'tensor', _small_3d, lambda t, d: [_small_3d(t, d, has_zeros=False)], 1e-3),
    ('chunk', '', _medium_2d, lambda t, d: [4], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('chunk', 'dim', _medium_2d, lambda t, d: [4, 1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('chunk', 'neg_dim', _medium_2d, lambda t, d: [4, -2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('clamp', 'neg', _medium_2d, lambda t, d: [-1, 5], 1e-5, 1e-2, 1e-5, _signed_types, [torch.bfloat16]),
    ('clamp', 'pos', _medium_2d, lambda t, d: [1, 5], 1e-5, 1e-2, 1e-5, _unsigned_types, [torch.bfloat16]),
    ('clamp_min', '', _medium_2d, lambda t, d: [1], 1e-2, 1e-2, 1e-5, _types, [torch.bfloat16]),
    ('clamp_max', '', _medium_2d, lambda t, d: [1], 1e-2, 1e-2, 1e-5, _types, [torch.bfloat16]),
    ('clone', '', _medium_2d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('contiguous', '', _medium_2d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('conj', '', _small_3d, lambda t, d: [], 1e-5, 0, 1e-5, _types_no_half, [torch.bfloat16], False),
    ('cross', '', _new_t((_M, 3, _M)), lambda t, d: [_new_t((_M, 3, _M))(t, d)],
        1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('logcumsumexp', '', _small_3d, lambda t, d: [1], 1e-2, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('logcumsumexp', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('cummax', '', _small_3d_unique, lambda t, d: [1], 1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('cummax', 'neg_dim', _small_3d_unique, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('cummin', '', _small_3d_unique, lambda t, d: [1], 1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('cummin', 'neg_dim', _small_3d_unique, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('cumprod', '', _small_3d, lambda t, d: [1], 1e-2, 1e-5, 1e-4, _types + _complex_types, _cpu_types, False),
    ('cumprod', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-2, 1e-5, 1e-4, _types + _complex_types, _cpu_types, False),
    ('cumsum', '', _small_3d, lambda t, d: [1], 1e-2, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('cumsum', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('dim', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('dist', '', _small_2d, lambda t, d: [_small_2d(t, d)], 1e-2, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('dist', '3_norm', _small_2d, lambda t, d: [_small_2d(t, d), 3], 1e-2, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('dist', '2_5_norm', _small_2d, lambda t, d: [_small_2d(t, d), 2.5],
        1e-2, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('dot', '', _medium_1d, lambda t, d: [_medium_1d(t, d)],
        1e-2, 1e-5, 1e-5, _float_types + _complex_types, _cpu_types, False),
    ('element_size', '', _medium_1d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _float_types_no_half, _cpu_types, False),
    ('eq', '', _small_3d_ones, lambda t, d: [_small_3d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('eq', 'equal', _small_3d_ones, lambda t, d: [_small_3d_ones(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('ne', '', _small_3d_ones, lambda t, d: [_small_3d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('ne', 'equal', _small_3d_ones, lambda t, d: [_small_3d_ones(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('equal', 'equal', _small_3d_ones, lambda t, d: [_small_3d_ones(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('equal', '', _small_3d_ones, lambda t, d: [_small_3d(t, d)], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('expand', '', _new_t((_M, 1, _M)), lambda t, d: [_M, 4, _M], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('expand_as', '', _new_t((_M, 1, _M)), lambda t, d: [_new_t((_M, 4, _M))(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('fill_', '', _medium_2d, lambda t, d: [_number(3.14, 3, t)], 1e-3, 1e-5, 1e-5, _types, _cpu_types, False),
    ('gcd', '', _small_3d, lambda t, d: [_small_3d(t, d)], 0, 0, 0,
     [torch.int16, torch.int32, torch.int64],
     [torch.int16, torch.int32, torch.int64], True, [onlyOnCPUAndCUDA]),
    ('lcm', '', _small_3d, lambda t, d: [_small_3d(t, d)], 0, 0, 0,
     [torch.int16, torch.int32, torch.int64],
     [torch.int16, torch.int32, torch.int64], True, [onlyOnCPUAndCUDA]),
    ('ge', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('le', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('gt', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('lt', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-5, 1e-5, 1e-5, _types2),
    ('is_contiguous', '', _medium_2d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    # TODO: can't check negative case - cross-device copy is contiguous
    ('is_same_size', 'negative', _medium_2d, lambda t, d: [_small_3d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('is_same_size', 'positive', _medium_2d, lambda t, d: [_medium_2d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('is_set_to', '', _medium_2d, lambda t, d: [_medium_2d(t, d)], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    # TODO: positive case
    ('kthvalue', '', _small_3d_unique, lambda t, d: [3], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('kthvalue', 'dim', _small_3d_unique, lambda t, d: [3, 1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('kthvalue', 'neg_dim', _small_3d_unique, lambda t, d: [3, -1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('lerp', '', _small_3d, lambda t, d: [_small_3d(t, d), 0.3],
        1e-2, 1e-5, 1e-5, _float_types),
    ('max', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('max', 'dim', _small_3d_unique, lambda t, d: [1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('max', 'neg_dim', _small_3d_unique, lambda t, d: [-1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('max', 'elementwise', _medium_2d, lambda t, d: [_medium_2d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('maximum', '', _medium_2d, lambda t, d: [_medium_2d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('min', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('min', 'dim', _small_3d_unique, lambda t, d: [1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('min', 'neg_dim', _small_3d_unique, lambda t, d: [-1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('min', 'elementwise', _medium_2d, lambda t, d: [_medium_2d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('minimum', '', _medium_2d, lambda t, d: [_medium_2d(t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('mean', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), _cpu_types, False),
    ('mean', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), _cpu_types, False),
    ('mean', 'dim', _small_3d, lambda t, d: [1], 1e-3, 1e-2, 1e-2, torch.testing.get_all_fp_dtypes(), _cpu_types, False),
    # Double here because the CPU result will be wrong otherwise
    ('mean', '64bit_indexing', _giant_1d, lambda t, d: [],
        1e-3, 1e-5, 1e-5, [torch.double], _cpu_types, False, [slowTest]),
    ('mode', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('mode', 'dim', _small_3d, lambda t, d: [1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('mode', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('mvlgamma', '2d_p=1', lambda t, d: _small_2d(t, d).clamp(0.1, 10), lambda t, d: [1],
        1e-5, 1e-5, 1e-5, _float_types_no_half),
    ('mvlgamma', '2d_p=2', lambda t, d: _small_2d(t, d).clamp(0.6, 10), lambda t, d: [2],
        1e-5, 1e-5, 1e-5, _float_types_no_half),
    ('remainder', 'value', _small_3d, lambda t, d: [3], 1e-1, 1e-5, 1e-5, _signed_types),
    ('remainder', 'negative_value', _small_3d, lambda t, d: [-3], 1e-1, 1e-5, 1e-5, _signed_types),
    ('remainder', 'tensor', _small_3d,
        lambda t, d: [_small_3d(t, d, has_zeros=False)],
        1e-1, 1e-5, 1e-5, _signed_types),
    ('remainder', 'negative_tensor', _small_3d,
        lambda t, d: [0 - _small_3d(t, d, has_zeros=False)],
        1e-1, 1e-5, 1e-5, _signed_types),
    ('std', '', _small_3d, lambda t, d: [], 1e-3, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('std', 'dim', _small_3d, lambda t, d: [1], 1e-3, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('std', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-3, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('var', '', _small_3d, lambda t, d: [], 1e-3, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('var', 'dim', _small_3d, lambda t, d: [1], 1e-3, 1e-5, 1e-5, _float_types, _cpu_types, False),
    ('var', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), _cpu_types, False),
    ('ndimension', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('nelement', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('numel', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('narrow', '', _small_3d, lambda t, d: [1, 3, 2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('narrow', 'neg_dim', _small_3d, lambda t, d: [-1, 3, 2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('nonzero', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('norm', '', _small_3d, lambda t, d: [], 1e-1, 1e-1, 1e-5, _float_types2, _cpu_types, False),
    ('norm', '3_norm', _small_3d, lambda t, d: [3], 1e-1, 1e-1, 1e-5, _float_types2, _cpu_types, False),
    ('norm', '3_norm_dim', _small_3d, lambda t, d: [3, 0], 1e-1, 1e-1, 1e-5, _float_types2, _cpu_types, False),
    ('norm', '3_norm_neg_dim', _small_3d, lambda t, d: [3, -2], 1e-1, 1e-1, 1e-5, _float_types2, _cpu_types, False),
    ('new_ones', '', _small_3d, lambda t, d: [1, 2, 3, 4, 5], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('permute', '', _new_t((1, 2, 3, 4)), lambda t, d: [2, 1, 3, 0], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('put_', '', _new_t((2, 5, 3)),
        lambda t, d: [torch.LongTensor([[0], [-2]]).to(device=d),
                      torch.LongTensor([[3], [4]]).to(dtype=_convert_t(t, d), device=d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('put_', 'empty', _new_t((2, 3)),
        lambda t, d: [torch.LongTensor([]).to(device=d), torch.LongTensor([]).to(dtype=_convert_t(t, d), device=d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('put_', 'accumulate', _new_t((2, 2)),
        lambda t, d: [torch.LongTensor([[1], [-3]]).to(device=d),
                      torch.LongTensor([[1], [2]]).to(dtype=_convert_t(t, d), device=d),
                      True],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('prod', '', lambda t, d: _small_2d(t, d, oneish=True),
        lambda t, d: [], 1e-2, 1e-1, 1e-5, _types2, _cpu_types, False),
    ('prod', 'dim', _small_3d, lambda t, d: [1], 1e-3, 1e-1, 1e-5, _types2, _cpu_types, False),
    ('prod', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-3, 1e-1, 1e-5, _types2, _cpu_types, False),
    ('sum', '', _small_2d, lambda t, d: [], 1e-2, 1e-2, 1e-5, _types2, _cpu_types, False),
    ('sum', 'dim', _small_3d, lambda t, d: [1], 1e-2, 1e-2, 1e-5, _types2, _cpu_types, False),
    ('sum', 'neg_dim', _small_3d, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sum', 'complex', _small_2d, lambda t, d: [], 1e-2, 1e-2, 1e-5, _complex_types, _cpu_types, False),
    ('sum', 'complex_dim', _small_3d, lambda t, d: [1], 1e-2, 1e-2, 1e-5, _complex_types, _cpu_types, False),
    ('sum', 'complex_neg_dim', _small_3d, lambda t, d: [-1], 1e-2, 1e-5, 1e-5, _complex_types, _cpu_types, False),
    ('renorm', '2_norm', _small_3d, lambda t, d: [2, 1, 1], 1e-3, 1e-5, 1e-5, _float_types),
    ('renorm', '2_norm_neg_dim', _small_3d, lambda t, d: [2, -1, 1], 1e-3, 1e-5, 1e-5, _float_types),
    ('renorm', '1_5_norm', _small_3d, lambda t, d: [1.5, 1, 1], 1e-3, 1e-5, 1e-5, _float_types),
    ('repeat', '', _small_2d, lambda t, d: [2, 2, 2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('size', '', _new_t((1, 2, 3, 4)), lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('size', 'dim', _new_t((1, 2, 3, 4)), lambda t, d: [1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('size', 'neg_dim', _new_t((1, 2, 3, 4)), lambda t, d: [-2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sort', '', _small_3d_unique, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sort', 'dim', _small_3d_unique, lambda t, d: [1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sort', 'neg_dim', _small_3d_unique, lambda t, d: [-1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sort', 'dim_descending', _small_3d_unique, lambda t, d: [1, True], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('sort', 'neg_dim_descending', _small_3d_unique, lambda t, d: [-1, True], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('split', '', _small_3d, lambda t, d: [2], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('split', 'dim', _small_3d, lambda t, d: [2, 1], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('split', 'neg_dim', _small_3d, lambda t, d: [2, -3], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('squeeze', '', _new_t((1, 2, 1, 4)), lambda t, d: [],),
    ('squeeze', 'dim', _new_t((1, 2, 1, 4)), lambda t, d: [2], ),
    ('squeeze', 'neg_dim', _new_t((1, 2, 1, 4)), lambda t, d: [-2], ),
    ('t', '', _new_t((1, 2)), lambda t, d: [],),
    ('take', '', _new_t((3, 4)),
        lambda t, d: [torch.LongTensor([[0], [-2]]).to(device=d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('transpose', '', _new_t((1, 2, 3, 4)), lambda t, d: [1, 2],),
    ('transpose', 'neg_dim', _new_t((1, 2, 3, 4)), lambda t, d: [-1, -2], ),
    ('tolist', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('topk', 'dim_sort', _small_3d_unique, lambda t, d: [2, 1, False, True],
        1e-5, 1e-5, 1e-5, _types2, _cpu_types, False),
    ('topk', 'neg_dim_sort', _small_3d_unique, lambda t, d: [2, -1, False, True],
        1e-5, 1e-5, 1e-5, _types2, _cpu_types, False),
    ('topk', 'dim_desc_sort', _small_3d_unique, lambda t, d: [2, 1, True, True],
        1e-5, 1e-5, 1e-5, _types2, _cpu_types, False),
    ('trace', '', _medium_2d, lambda t, d: [], 1e-3, 1e-5, 1e-5, _types, _cpu_types, False),
    ('tril', '', _medium_2d, lambda t, d: [],),
    ('tril', 'zero_stride', _medium_2d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('tril', 'positive', _medium_2d, lambda t, d: [2], ),
    ('tril', 'negative', _medium_2d, lambda t, d: [-2], ),
    ('triu', '', _medium_2d, lambda t, d: [],),
    ('triu', 'zero_stride', _medium_2d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('triu', 'positive', _medium_2d, lambda t, d: [2], ),
    ('triu', 'negative', _medium_2d, lambda t, d: [-2], ),
    ('unsqueeze', '', _new_t((2, 3, 4)), lambda t, d: [2],),
    ('unsqueeze', 'neg_dim', _new_t((2, 3, 4)), lambda t, d: [-2], ),
    ('view', 'contiguous', _small_3d, lambda t, d: [25, 5], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('view_as', '', _small_3d, lambda t, d: [_make_tensor((25, 5), t, d)],
        1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('zero_', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('new_zeros', '', _small_3d, lambda t, d: [1, 2, 3, 4], 1e-5, 1e-5, 1e-5, _types, _cpu_types, False),
    ('flip', 'd0', _small_3d, lambda t, d: [0], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('flip', 'd02', _small_3d, lambda t, d: [0, 2], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('flip', 'd20', _small_3d, lambda t, d: [2, 0], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('flip', 'neg_d', _small_3d, lambda t, d: [-1], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('rot90', 'k1_d01', _small_2d, lambda t, d: [1, [0, 1]], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('rot90', 'k1_d12', _small_3d, lambda t, d: [1, [1, 2]], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('rot90', 'k1_neg_d', _small_3d, lambda t, d: [1, [1, -1]], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('rot90', 'default', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5, _types + _complex_types, _cpu_types, False),
    ('rsqrt', '', lambda t, d: _small_3d(t, d) + 1, lambda t, d: [], 1e-2, 1e-5, 1e-4, _float_types_no_half),
    ('sinh', '', lambda t, d: _small_3d(t, d).clamp(-1, 1), lambda t, d: [], 1e-3, 1e-5, 1e-5, _float_types),
    ('tan', '', lambda t, d: _small_3d(t, d).clamp(-1, 1), lambda t, d: [], 1e-3, 1e-5, 1e-5, _float_types),
    ('tan', 'complex', lambda t, d: _small_3d(t, d), lambda t, d: [], 1e-3, 1e-5, 1e-5, _complex_types),
    ('__lshift__', '',
        lambda t, d: torch.pow(2, torch.arange(1, 5).to(dtype=_convert_t(t, d), device=d)),
        lambda t, d: [2],
        1e-3, 1e-5, 1e-3, _signed_types, _cpu_types, False),
    ('__rshift__', '',
        lambda t, d: torch.pow(2, torch.arange(3, 7).to(dtype=_convert_t(t, d), device=d)),
        lambda t, d: [2],
        1e-3, 1e-5, 1e-3, _signed_types, _cpu_types, False),
    # lapack tests
    ('qr', 'square', _small_2d, lambda t, d: [],
        1e-5, 1e-5, 3e-4, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('qr', 'skinny', _new_t((3, 4)), lambda t, d: [],
        1e-5, 1e-5, 3e-4, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('qr', 'fat', _new_t((4, 3)), lambda t, d: [],
        1e-5, 1e-5, 3e-4, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('qr', 'big', _large_2d, lambda t, d: [],
        1e-5, 1e-5, 3e-4, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('geqrf', '', _new_t((20, 20)), lambda t, d: [],
        1e-5, 1e-5, 3e-4, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('eig', 'with_eigvec', _new_t((10, 10)), lambda t, d: [True],
        1e-5, 1e-5, 1e-5, _float_types_no_half, _cpu_types, False, [skipCUDAIfNoMagma]),
    ('abs', '', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e-5,
        torch.testing.get_all_dtypes(include_complex=False, include_bool=False), [torch.bfloat16]),
    ('sign', '', _small_3d, lambda t, d: []),
    ('log', '', _small_3d, lambda t, d: [], 1e-2, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('log10', '', _small_3d, lambda t, d: [], 1e-2, 5e-2, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('log1p', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, _float_types_no_half, [torch.bfloat16]),
    ('log2', '', _small_3d, lambda t, d: [], 1e-2, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('sigmoid', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('logit', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('sqrt', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('tanh', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5,
        torch.testing.get_all_fp_dtypes() + _complex_types, [torch.bfloat16]),
    ('asin', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('atan', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('acosh', '', lambda t, d: _small_3d(t, d) + 1, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('asinh', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('atanh', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('erf', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('erfc', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('erfinv', '', _small_3d, lambda t, d: [], 1e-3, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('exp', '', _small_3d, lambda t, d: [], 1e-2, 5e-2, 1e-5, torch.testing.get_all_fp_dtypes()),
    ('exp', 'small', lambda t, d: _small_3d(t, d).clamp(-1, 1),
        lambda t, d: [], 1e-2, 5e-2, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('expm1', '', _small_3d, lambda t, d: [], 1e-2, 1e-2, 1e-5, _float_types),
    ('expm1', 'small', lambda t, d: _small_3d(t, d).clamp(-1, 1),
        lambda t, d: [], 1e-2, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('rad2deg', '', _small_3d, lambda t, d: [], 1e-1, 1e-0, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('deg2rad', '', _small_3d, lambda t, d: [], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('reciprocal', '', _small_3d, lambda t, d: [], 1e-1, 1e-1, 1e-5, torch.testing.get_all_fp_dtypes(), [torch.bfloat16]),
    ('floor', '', _small_3d, lambda t, d: [], 1e-5, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('frac', '', _small_3d, lambda t, d: [], 1e-5, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('round', '', _small_3d, lambda t, d: [], 1e-5, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('trunc', '', _small_3d, lambda t, d: [], 1e-5, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('ceil', '', _small_3d, lambda t, d: [], 1e-5, 1e-2, 1e-5, _float_types, [torch.bfloat16]),
    ('lgamma', '', _small_3d, lambda t, d: [], 1e-2, 1e-1, 1e-5, _float_types_no_half, [torch.bfloat16]),
    ('digamma', 'op', _small_3d, lambda t, d: [], 1e-5, 1e-5, 1e0, _float_types_no_half),
]

# Creates and decorates a generic test and adds it to the class.
def generate_test_function(cls,
                           op_str,
                           subtest_str,
                           tensor_ctor,
                           arg_ctor,
                           half_precision,
                           bfloat16_precision,
                           float_precision,
                           dtype_list,
                           dtype_cpu_list,
                           decorators,
                           self_position,
                           test_out) -> None:
    def fn(self, device, dtype) -> None:
        # Generates the CPU inputs
        # Note: CPU tensors are never torch.half
        cpu_tensor = tensor_ctor(dtype, 'cpu')
        cpu_args = arg_ctor(dtype, 'cpu')

        # Converts CPU tensors to device tensors
        device_tensor = cpu_tensor.to(dtype=dtype, device=device)
        device_args = [arg.to(device=device) if isinstance(arg, torch.Tensor) else arg for arg in cpu_args]

        # Converts float device tensors to half/bfloat16 when the dtype is half/bfloat16
        # Note: CPU half tensors don't support many operations.
        if dtype in {torch.half, torch.bfloat16}:
            device_args = [arg.to(dtype=dtype) if
                           (isinstance(arg, torch.Tensor) and arg.dtype == torch.float) else arg
                           for arg in device_args]

        # Special case for binary float ops (binary ops that promote int to float)
        if op_str in binary_float_ops_inplace and \
                'inplace' in subtest_str and dtype in _integer_types:
            with self.assertRaisesRegex(RuntimeError, "result type Float can't be cast to "):
                cpu_result = getattr(cpu_tensor, op_str)(*cpu_args)
            with self.assertRaisesRegex(RuntimeError, "result type Float can't be cast to "):
                device_result = getattr(device_tensor, op_str)(*device_args)
            return  # Nothing more to check

        # Runs the tensor op on CPU and device
        cpu_result = getattr(cpu_tensor, op_str)(*cpu_args)
        device_result = getattr(device_tensor, op_str)(*device_args)

        dtype2precision = {torch.half : half_precision,
                           torch.bfloat16 : bfloat16_precision}

        # Compares CPU and device inputs and outputs
        precision = dtype2precision.get(dtype, float_precision)

        self.assertEqual(cpu_tensor, device_tensor, atol=precision, rtol=0, exact_dtype=False)
        self.assertEqual(cpu_args, device_args, atol=precision, rtol=0, exact_dtype=False)
        self.assertEqual(cpu_result, device_result, atol=precision, rtol=0, exact_dtype=False)

        # check method matches with function
        if self_position >= 0:
            cpu_args.insert(self_position, cpu_tensor)
            device_args.insert(self_position, device_tensor)
            cpu_function_result = getattr(torch, op_str)(*cpu_args)
            device_function_result = getattr(torch, op_str)(*device_args)
            self.assertEqual(cpu_result, cpu_function_result, atol=precision, rtol=0)
            self.assertEqual(device_result, device_function_result, atol=precision, rtol=0)

            # check method matches with function(out)
            if test_out:
                bad_value = math.nan if dtype.is_floating_point or dtype.is_complex else 666
                cpu_out = torch.full_like(cpu_result, bad_value)
                device_out = torch.full_like(device_result, bad_value)
                getattr(torch, op_str)(*cpu_args, out=cpu_out)
                getattr(torch, op_str)(*device_args, out=device_out)
                self.assertEqual(cpu_result, cpu_out, atol=precision, rtol=0)
                self.assertEqual(device_result, device_out, atol=precision, rtol=0)

    test_name = "test_" + op_str + subtest_str
    assert not hasattr(cls, test_name), "{0} already in TestDevicePrecision".format(test_name)

    # Constructs decorator list and applies decorators
    if decorators is None:
        decorators = [dtypes(*dtype_list)]
    else:
        decorators = decorators + [dtypes(*dtype_list)]
    decorators = decorators + [dtypesIfCPU(*dtype_cpu_list)]

    for dec in decorators:
        fn = dec(fn)

    setattr(cls, test_name, fn)

# Instantiates variants of tensor_op_tests and adds them to the given class.
def generate_tensor_op_tests(cls) -> None:

    def caller(cls,
               op_str,
               subtest_str,
               tensor_ctor,
               arg_ctor,
               half_precision=1e-5,
               bfloat16_precision=1e-5,
               float_precision=1e-5,
               dtype_list=_types,
               dtype_cpu_list=_cpu_types,
               make_inplace_variant=True,
               decorators=None,
               self_position=-1,
               test_out=False):
        if subtest_str:
            subtest_str = '_' + subtest_str

        generate_test_function(cls, op_str, subtest_str, tensor_ctor, arg_ctor, half_precision,
                               bfloat16_precision, float_precision, dtype_list, dtype_cpu_list,
                               decorators, self_position, test_out)

        if make_inplace_variant:
            op_str = op_str + '_'
            subtest_str = 'inplace' + subtest_str
            generate_test_function(cls, op_str, subtest_str, tensor_ctor, arg_ctor, half_precision,
                                   bfloat16_precision, float_precision, dtype_list, dtype_cpu_list,
                                   decorators, -1, False)

    for test in tensor_op_tests:
        caller(cls, *test)

def _generate_reference_input(dtype, device):
    input = []
    input.append(list(range(-5, 5)))
    input.append([0 for x in range(-5, 5)])
    input.append([x + 1e-6 for x in range(-5, 5)])
    # Some vectorized implementations don't support large values
    input.append([x + 1e10 for x in range(-5, 5)])
    input.append([x - 1e10 for x in range(-5, 5)])
    input.append([*torch.randn(7).tolist(), math.inf, -math.inf, math.nan])
    input.append((torch.randn(10) * 1e6).tolist())
    input.append([math.pi * (x / 2) for x in range(-5, 5)])
    return torch.tensor(input, dtype=dtype, device=device)

def _generate_gamma_input(dtype, device, test_poles=True):
    input = []
    input.append((torch.randn(10).abs() + 1e-4).tolist())
    input.append((torch.randn(10).abs() + 1e6).tolist())
    zeros = torch.linspace(-9.5, -0.5, 10)
    input.append(zeros.tolist())
    input.append((zeros - 0.49).tolist())
    input.append((zeros + 0.49).tolist())
    input.append((zeros + (torch.rand(10) * 0.99) - 0.5).tolist())

    if test_poles:
        input.append([-0.999999994, -1.999999994, -2.0000000111,
                      -100.99999994, -1931.99999994, 0.000000111,
                      -0.000000111, 0, -2, -329])
    return torch.tensor(input, dtype=dtype, device=device)

# this class contains information needed to generate tests for torch math functions
# the generated tests compare torch implementation with the reference numpy/scipy implementation,
# and also check proper behavior for contiguous/discontiguous/inplace outputs.
class _TorchMathTestMeta(object):
    def __init__(self,
                 opstr,
                 args=(),
                 reffn=None,
                 refargs=lambda x: (x.numpy(),),
                 input_fn=_generate_reference_input,
                 inputargs=(),
                 substr='',
                 make_inplace=True,
                 decorators=None,
                 ref_backend='numpy',
                 rtol=None,
                 atol=None,
                 dtypes=_float_types_no_half,
                 replace_inf_with_nan=False):
        self.opstr = opstr
        self.args = args
        self.reffn = reffn  # reffn is either callable or ref_backend attribute, set to opstr if not specified
        self.refargs = refargs
        self.input_fn = input_fn
        self.inputargs = inputargs
        self.substr = substr
        self.make_inplace = make_inplace
        assert ref_backend == 'numpy' or ref_backend == 'scipy'
        self.ref_backend = ref_backend
        if ref_backend == 'numpy':
            self.ref_decorator = [unittest.skipIf(not TEST_NUMPY, "Numpy not found")]
        elif ref_backend == 'scipy':
            self.ref_decorator = [unittest.skipIf(not TEST_SCIPY, "Scipy not found")]
        self.decorators = decorators
        self.rtol = rtol
        self.atol = atol
        self.dtypes = dtypes
        self.replace_inf_with_nan = replace_inf_with_nan

torch_op_tests = [_TorchMathTestMeta('sqrt'),
                  _TorchMathTestMeta('erf', ref_backend='scipy'),
                  _TorchMathTestMeta('erfc', ref_backend='scipy'),
                  _TorchMathTestMeta('exp'),
                  _TorchMathTestMeta('expm1'),
                  _TorchMathTestMeta('floor'),
                  _TorchMathTestMeta('ceil'),
                  _TorchMathTestMeta('rad2deg'),
                  _TorchMathTestMeta('deg2rad'),
                  _TorchMathTestMeta('rsqrt', reffn=lambda x: np.reciprocal(np.sqrt(x))),
                  _TorchMathTestMeta('frac', reffn='fmod', refargs=lambda x: (x.numpy(), 1)),
                  _TorchMathTestMeta('trunc'),
                  _TorchMathTestMeta('round'),
                  # FIXME lgamma produces different result compared to scipy at -inf
                  _TorchMathTestMeta('lgamma', reffn='gammaln', ref_backend='scipy', replace_inf_with_nan=True),
                  _TorchMathTestMeta('polygamma', args=[0], substr='_0', reffn='polygamma',
                                     refargs=lambda x: (0, x.numpy()), input_fn=_generate_gamma_input, inputargs=[False],
                                     ref_backend='scipy'),
                  _TorchMathTestMeta('polygamma', args=[1], substr='_1', reffn='polygamma',
                                     refargs=lambda x: (1, x.numpy()), input_fn=_generate_gamma_input, inputargs=[False],
                                     ref_backend='scipy', rtol=0.0008, atol=1e-5),
                  _TorchMathTestMeta('polygamma', args=[2], substr='_2', reffn='polygamma',
                                     refargs=lambda x: (2, x.numpy()), input_fn=_generate_gamma_input, inputargs=[False],
                                     ref_backend='scipy', rtol=0.0008, atol=1e-5),
                  _TorchMathTestMeta('digamma',
                                     input_fn=_generate_gamma_input, inputargs=[True], ref_backend='scipy',
                                     replace_inf_with_nan=True),
                  _TorchMathTestMeta('abs', input_fn=_medium_2d, dtypes=_types_no_half, rtol=0., atol=0.),
                  _TorchMathTestMeta('logit', ref_backend='scipy')]


def generate_torch_test_functions(cls, testmeta, inplace):
    opstr = testmeta.opstr if not inplace else testmeta.opstr + "_"

    def torchfn(x):
        return getattr(x, opstr)(*testmeta.args)

    def fn_check_reference(self, device, dtype):
        def reffn(x):
            backend = np if testmeta.ref_backend == 'numpy' else scipy.special
            opstr = None
            if testmeta.reffn is None:
                opstr = testmeta.opstr
            elif isinstance(testmeta.reffn, str):
                opstr = testmeta.reffn
            if callable(testmeta.reffn):
                fn = testmeta.reffn
            else:
                assert opstr is not None, "invalid reffn"
                fn = getattr(backend, opstr)
            return fn(*testmeta.refargs(x))

        inp = testmeta.input_fn(dtype, device, *testmeta.inputargs)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            expected = torch.from_numpy(reffn(inp))
        actual = torchfn(inp)
        if testmeta.replace_inf_with_nan:
            actual[(actual == -inf) | (actual == inf)] = nan
            expected[(expected == -inf) | (expected == inf)] = nan

        torch.testing.assert_allclose(actual, expected, rtol=testmeta.rtol, atol=testmeta.atol)

    def fn_non_contig(self, device, dtype) -> None:
        shapes = [(5, 7), (1024,)]
        for shape in shapes:
            contig = _make_tensor(shape, dtype=dtype, device=device)
            non_contig = torch.empty(shape + (2,), dtype=dtype)[..., 0]
            non_contig.copy_(contig)
            self.assertFalse(non_contig.is_contiguous())
            self.assertEqual(torchfn(contig), torchfn(non_contig), msg='non-contiguous')

    def fn_non_contig_index(self, device, dtype):
        contig = _make_tensor((2, 2, 1, 2), dtype=dtype, device=device)
        non_contig = contig[:, 1, ...]
        contig = non_contig.clone()
        self.assertFalse(non_contig.is_contiguous())
        self.assertEqual(torchfn(contig), torchfn(non_contig), msg='non-contiguous index')

    def fn_non_contig_expand(self, device, dtype):
        shapes = [(1, 3), (1, 7), (5, 7)]
        for shape in shapes:
            contig = _make_tensor(shape, dtype=dtype, device=device)
            non_contig = contig.clone().expand(3, -1, -1)
            self.assertFalse(non_contig.is_contiguous())
            contig = torchfn(contig)
            non_contig = torchfn(non_contig)
            for i in range(3):
                self.assertEqual(contig, non_contig[i], msg='non-contiguous expand[' + str(i) + ']')

    def fn_contig_size1(self, device, dtype):
        contig = _make_tensor((5, 100), dtype=dtype, device=device)
        contig = contig[:1, :50]
        contig2 = torch.empty(contig.size(), dtype=dtype)
        contig2.copy_(contig)
        self.assertTrue(contig.is_contiguous())
        self.assertTrue(contig2.is_contiguous())
        self.assertEqual(torchfn(contig), torchfn(contig2), msg='contiguous size1')

    def fn_contig_size1_large_dim(self, device, dtype):
        contig = _make_tensor((5, 2, 3, 1, 4, 5, 3, 2, 1, 2, 3, 4), dtype=dtype, device=device)
        contig = contig[:1, :, :, :, :, :, :, :, :, :, :, :]
        contig2 = torch.empty(contig.size(), dtype=dtype)
        contig2.copy_(contig)
        self.assertTrue(contig.is_contiguous())
        self.assertTrue(contig2.is_contiguous())
        self.assertEqual(torchfn(contig), torchfn(contig2), msg='contiguous size1')

    def fn_large(self, device, dtype):
        input = _make_tensor((1024, 512), dtype=dtype, device=device)
        # clone input to properly test inplace functions
        actual = torchfn(input.clone())
        expected = torch.stack([torchfn(slice) for slice in input])
        self.assertEqual(actual, expected, msg='large')

    test_functions = {"test_reference_": fn_check_reference,
                      "test_non_contig_": fn_non_contig,
                      "test_non_contig_index_": fn_non_contig_index,
                      "test_non_contig_expand_": fn_non_contig_expand,
                      "test_contig_size1_": fn_contig_size1,
                      "test_check_contig_size1_large_dim_": fn_contig_size1_large_dim,
                      "test_large_": fn_large}
    for name in test_functions:
        if inplace and 'expand' in name:
            continue
        test_name = name + testmeta.opstr + testmeta.substr
        if inplace:
            test_name += "_inplace"
        assert not hasattr(cls, test_name), "{0} already in TestTorchMathOps".format(test_name)

        decorators = [] if testmeta.decorators is None else testmeta.decorators
        if 'reference' in name:
            decorators = decorators + testmeta.ref_decorator
        decorators = decorators + [dtypes(*testmeta.dtypes)]
        fn_test = test_functions[name]
        for dec in decorators:
            fn_test = dec(fn_test)
        setattr(cls, test_name, fn_test)




def generate_torch_op_tests(cls):
    for t in torch_op_tests:
        generate_torch_test_functions(cls, t, False)
        if t.make_inplace:
            generate_torch_test_functions(cls, t, True)





tensor_binary_ops = [
    '__lt__', '__le__',
    '__gt__', '__ge__',
    '__eq__', '__ne__',

    '__add__', '__radd__', '__iadd__',
    '__sub__', '__rsub__', '__isub__',
    '__mul__', '__rmul__', '__imul__',
    '__matmul__', '__rmatmul__', '__imatmul__',
    '__truediv__', '__rtruediv__', '__itruediv__',
    '__floordiv__', '__rfloordiv__', '__ifloordiv__',
    '__mod__', '__rmod__', '__imod__',
    '__divmod__', '__rdivmod__', '__idivmod__',
    '__pow__', '__rpow__', '__ipow__',
    '__lshift__', '__rlshift__', '__ilshift__',
    '__rshift__', '__rrshift__', '__irshift__',
    '__and__', '__rand__', '__iand__',
    '__xor__', '__rxor__', '__ixor__',
    '__or__', '__ror__', '__ior__',
]


# Test that binary math operations return NotImplemented for unknown types.
def generate_not_implemented_tests(cls):
    class UnknownType:
        pass

    for op in tensor_binary_ops:
        @dtypes(*_types)
        def test(self, device, dtype):
            # Generate the inputs
            tensor = _small_2d(dtype, device)

            # Runs the tensor op on the device
            result = getattr(tensor, op)(UnknownType())
            self.assertEqual(result, NotImplemented)

        test_name = "test_{}_not_implemented".format(op)
        assert not hasattr(cls, test_name), "{0} already in {1}".format(
            test_name, cls.__name__)

        setattr(cls, test_name, test)


class TestTensorDeviceOps(TestCase):
    exact_dtype = True

    def _test_svd_helper(self, shape, some, col_maj, device, dtype):
        cpu_tensor = torch.randn(shape, device='cpu').to(dtype)
        device_tensor = cpu_tensor.to(device=device)
        if col_maj:
            cpu_tensor = cpu_tensor.t()
            device_tensor = device_tensor.t()
        cpu_result = torch.svd(cpu_tensor, some=some)
        device_result = torch.svd(device_tensor, some=some)
        m = min(cpu_tensor.shape[-2:])
        # torch.svd returns torch.return_types.svd which is a tuple of (U, V, S).
        # - When some==False, U[..., m:] can be arbitrary.
        # - When some==True, U shape: [..., m], V shape: [m, m]
        # - Signs are not deterministic. If the sign of a column of U is changed
        #   then the corresponding column of the V has to be changed.
        # Thus here we only compare result[..., :m].abs() from CPU and device.
        for x, y in zip(cpu_result, device_result):
            self.assertEqual(x[..., :m].abs(), y[..., :m].abs(), atol=1e-5, rtol=0)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_square(self, device, dtype):
        self._test_svd_helper((10, 10), True, False, device, dtype)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_square_col_maj(self, device, dtype):
        self._test_svd_helper((10, 10), True, True, device, dtype)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_tall_some(self, device, dtype):
        self._test_svd_helper((20, 5), True, False, device, dtype)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_tall_all(self, device, dtype):
        self._test_svd_helper((20, 5), False, False, device, dtype)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_tall_some_col_maj(self, device, dtype):
        self._test_svd_helper((5, 20), True, True, device, dtype)

    @skipCUDAIfNoMagma
    @skipCPUIfNoLapack
    @dtypes(*_float_types_no_half)
    def test_svd_tall_all_col_maj(self, device, dtype):
        self._test_svd_helper((5, 20), False, True, device, dtype)

class TestTorchMathOps(TestCase):
    exact_dtype = True

class TestTorch(AbstractTestCases._TestTorchMixin):
    exact_dtype = True


# Generates tests
# Note: test generation must be done at file scope, not within main, or
# pytest will fail.
add_neg_dim_tests()
generate_tensor_op_tests(TestTensorDeviceOps)
generate_not_implemented_tests(TestTorchDeviceType)
generate_torch_op_tests(TestTorchMathOps)
instantiate_device_type_tests(TestTorchDeviceType, globals())
instantiate_device_type_tests(TestViewOps, globals())
instantiate_device_type_tests(TestDevicePrecision, globals(), except_for='cpu')
instantiate_device_type_tests(TestTensorDeviceOps, globals())
instantiate_device_type_tests(TestTorchMathOps, globals(), only_for='cpu')

if __name__ == '__main__':
    run_tests()

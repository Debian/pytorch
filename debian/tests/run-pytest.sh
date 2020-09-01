#!/bin/bash
set -e
echo "# Running Python Testing Programs"
echo "# The tests are collected from test/run_test.py"
FILES=(
	'test_autograd'
	'test_bundled_inputs'
	'test_complex'
	'test_cpp_api_parity'
	'test_cpp_extensions_aot_no_ninja'
	'test_cpp_extensions_aot_ninja'
	'test_cpp_extensions_jit'
	'distributed/test_c10d'
	'distributed/test_c10d_spawn'
	'test_cuda'
	'test_jit_cuda_fuser'
	'test_cuda_primary_ctx'
	'test_dataloader'
	'distributed/test_data_parallel'
	'distributed/test_distributed'
	'test_distributions'
	'test_expecttest'
	'test_indexing'
	'test_jit'
	'test_logging'
	'test_mkldnn'
	'test_multiprocessing'
	'test_multiprocessing_spawn'
	'distributed/test_nccl'
	'test_nn'
	'test_numba_integration'
	'test_optim'
	'test_mobile_optimizer'
	'test_xnnpack_integration'
	'test_vulkan'
	'test_quantization'
	'test_sparse'
	'test_serialization'
	'test_show_pickle'
	'test_torch'
	'test_type_info'
	'test_type_hints'
	'test_utils'
	'test_namedtuple_return_api'
	'test_jit_profiling'
	'test_jit_legacy'
	'test_jit_fuser_legacy'
	'test_tensorboard'
	'test_namedtensor'
	'test_type_promotion'
	'test_jit_disabled'
	'test_function_schema'
	'test_overrides'
	'test_jit_fuser_te'
	'test_tensorexpr'
)
echo "# Found" ${#FILES[@]} "tests"
echo "#"
sleep 1

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# Py test ${i}/${#FILES[@]} ${FILES[$i]}"
	pytest-3 test/${FILES[$i]}.py -v
done

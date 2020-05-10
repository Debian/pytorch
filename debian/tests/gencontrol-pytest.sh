#!/bin/bash
set -e
echo "# Generating Autopkgtest Test Cases for the Python Testing Programs"
echo "# The tests are collected from test/run_test.py"
FILES=(
  	'test_autograd'
    'test_cpp_api_parity'
    'test_cpp_extensions_aot_no_ninja'
    'test_cpp_extensions_aot_ninja'
    'test_cpp_extensions_jit'
    'distributed/test_c10d'
    'distributed/test_c10d_spawn'
    'test_cuda'
    'test_cuda_primary_ctx'
    'test_dataloader'
    'distributed/test_data_parallel'
    'distributed/test_distributed'
    'test_distributions'
    'test_docs_coverage'
    'test_expecttest'
    'test_fake_quant'
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
    'test_qat'
    'test_quantization'
    'test_quantized'
    'test_quantized_tensor'
    'test_quantized_nn_mods'
    'test_sparse'
    'test_serialization'
    'test_torch'
    'test_type_info'
    'test_type_hints'
    'test_utils'
    'test_namedtuple_return_api'
    'test_jit_fuser'
    'test_jit_simple'
    'test_jit_legacy'
    'test_jit_fuser_legacy'
    'test_tensorboard'
    'test_namedtensor'
    'test_type_promotion'
    'test_jit_disabled'
    'test_function_schema'
    'test_overrides'
)
echo "# Found" ${#FILES[@]} "tests"
echo "#"

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# Py test ${i}/${#FILES[@]}"
	echo "Test-Command: pytest-3 test/${FILES[$i]}.py -v"
	echo "Depends: python3-torch, python3-pytest, python3-hypothesis"
	echo "Features: test-name=$((${i}+1))_of_${#FILES[@]}__pytest__$(basename ${FILES[$i]})"
	echo "Restrictions: allow-stderr"
	echo ""
done

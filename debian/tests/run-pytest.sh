#!/bin/bash
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
    'test_jit_cuda_fuser_legacy'
    'test_jit_cuda_fuser_profiling'
    'test_cuda_primary_ctx'
    'test_dataloader'
    'distributed/test_data_parallel'
    'distributed/test_distributed_fork'
    'distributed/test_distributed_spawn'
    'test_distributions'
    'test_expecttest'
    'test_foreach'
    'test_indexing'
    'test_jit'
    'test_linalg'
    'test_logging'
    'test_mkldnn'
    'test_multiprocessing'
    'test_multiprocessing_spawn'
    'distributed/test_nccl'
    'test_native_functions'
    'test_nn'
    'test_numba_integration'
    'test_ops'
    'test_optim'
    'test_mobile_optimizer'
    'test_xnnpack_integration'
    'test_vulkan'
    'test_quantization'
    'test_sparse'
    'test_spectral_ops'
    'test_serialization'
    'test_show_pickle'
    'test_tensor_creation_ops'
    'test_torch'
    'test_type_info'
    'test_type_hints'
    'test_unary_ufuncs'
    'test_utils'
    'test_vmap'
    'test_namedtuple_return_api'
)
echo "# Found" ${#FILES[@]} "tests"
echo "#"
sleep 1

failed=( )
for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo
	echo
	echo "# Py test ${i}/${#FILES[@]} ${FILES[$i]}"
	python3 -m pytest ${FILES[$i]}.py -v
	if test 0 != $?; then
		failed+=( ${Files[$i]} )
	fi
done

echo
echo "# listing failed tests ..."
for i in ${failed[@]}; do
	echo ${i}
done

#!/bin/bash
set -e
echo "# Generating Autopkgtest Test Cases for the C++ Testing Programs"
FILES=( $(find /usr/lib/libtorch-test/ -type f -executable | sort) )
echo "# Found" ${#FILES[@]} "tests"
echo "#"

PERMISSIVE_LIST=(
/usr/lib/libtorch-test/mpi_test
/usr/lib/libtorch-test/bound_shape_inference_test
/usr/lib/libtorch-test/cpuid_test
/usr/lib/libtorch-test/c10_string_view_test
/usr/lib/libtorch-test/blob_test
/usr/lib/libtorch-test/generate_proposals_op_util_boxes_test
/usr/lib/libtorch-test/generate_proposals_op_util_nms_test
)

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# C++ test ${i}/${#FILES[@]}"
	if echo ${PERMISSIVE_LIST[@]} | grep -o ${FILES[$i]} >/dev/null 2>/dev/null; then
		echo "Test-Command: ${FILES[$i]} || true"
	else
		echo "Test-Command: ${FILES[$i]}"
	fi
	echo "Depends: build-essential, ninja-build, libtorch-dev, libtorch-test"
	echo "Features: test-name=$((${i}+1))_of_${#FILES[@]}__cpptest__$(basename ${FILES[$i]})"
	echo "Restrictions: allow-stderr"
	echo ""
done

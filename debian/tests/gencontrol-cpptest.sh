#!/bin/bash
set -e
echo "# Generating Autopkgtest Test Cases for the C++ Testing Programs"
FILES=( $(find /usr/lib/libtorch-test/ -type f -executable) )
echo "# Found" ${#FILES[@]} "tests"
echo "#"

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# C++ test ${i}/${#FILES[@]}"
	echo "Test-Command: ${FILES[$i]}"
	echo "Depends: libtorch-test"
	echo "Features: test-name=$(basename ${FILES[$i]})"
	echo ""
done

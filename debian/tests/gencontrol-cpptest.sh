#!/bin/bash
set -e
echo "# Generating Autopkgtest Test Cases for the C++ Testing Programs"
FILES=( $(find /usr/lib/libtorch-test/ -type f -executable | sort) )
echo "# Found" ${#FILES[@]} "tests"
echo "#"

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# C++ test ${i}/${#FILES[@]}"
	echo "Test-Command: ${FILES[$i]}"
	echo "Depends: build-essential, libtorch-dev, libtorch-test"
	echo "Features: test-name=$((${i}+1))_of_${#FILES[@]}__cpptest__$(basename ${FILES[$i]})"
	echo "Restrictions: allow-stderr"
	echo ""
done

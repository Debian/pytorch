#!/bin/bash
set -e
echo "# Running C++ Testing Programs"
FILES=( $(find /usr/lib/libtorch-test/ -type f -executable) )
echo "# Found" ${#FILES[@]} "tests"
echo "#"
sleep 1

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# C++ test ${i}/${#FILES[@]} ${FILES[$i]}"
	${FILES[$i]}
	echo ""
done

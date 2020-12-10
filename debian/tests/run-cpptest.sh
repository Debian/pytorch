#!/bin/bash
echo "# Running C++ Testing Programs"
FILES=( $(find /usr/lib/libtorch-test/ -type f -executable) )
echo "# Found" ${#FILES[@]} "tests"
echo "#"
sleep 1

failed=( )

for (( i = 0; i < ${#FILES[@]}; i++ )); do
	echo "# C++ test ${i}/${#FILES[@]} ${FILES[$i]}"
	${FILES[$i]}
	if test 0 != $?; then
		failed+=( ${FILES[$i]} )
	fi
	echo ""
done

echo
echo "# Printing failed tests ..."
for i in ${failed[@]}; do
	echo ${i}
done

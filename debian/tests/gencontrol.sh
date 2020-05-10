#!/bin/bash
echo "Please make sure that bin:libtorch-test and bin:python3-torch have been installed before running this script."
cp -v control.in control

echo "" >> control
bash gencontrol-cpptest.sh >> control

echo "" >> control
bash gencontrol-pytest.sh >> control

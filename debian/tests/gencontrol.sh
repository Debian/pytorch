#!/bin/bash
cp -v control.in control

echo "" >> control
bash gencontrol-cpptest.sh >> control

echo "" >> control
bash gencontrol-pytest.sh >> control

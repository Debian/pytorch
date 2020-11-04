#!/bin/bash

# Required environment variable: $BUILD_ENVIRONMENT
# (This is set by default in the Docker images we build, so you don't
# need to set it yourself.

# shellcheck disable=SC2034
COMPACT_JOB_NAME="${BUILD_ENVIRONMENT}"

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

echo "Testing pytorch (distributed only)"
if [ -n "${IN_CIRCLECI}" ]; then
  # TODO move this to docker
  pip_install unittest-xml-reporting

  if [[ "$BUILD_ENVIRONMENT" == *-xenial-cuda10.1-* ]]; then
    # TODO: move this to Docker
    sudo apt-get update
    sudo apt-get install -y --allow-downgrades --allow-change-held-packages libnccl-dev=2.5.6-1+cuda10.1 libnccl2=2.5.6-1+cuda10.1
  fi

  if [[ "$BUILD_ENVIRONMENT" == *-xenial-cuda10.1-cudnn7-py3* ]]; then
    # TODO: move this to Docker
    sudo apt-get update
    sudo apt-get install -y --allow-downgrades --allow-change-held-packages openmpi-bin libopenmpi-dev
  fi
fi

python tools/download_mnist.py --quiet -d test/cpp/api/mnist
OMP_NUM_THREADS=2 TORCH_CPP_TEST_MNIST_PATH="test/cpp/api/mnist" build/bin/test_api
time python test/run_test.py --verbose -i distributed/test_distributed_fork
time python test/run_test.py --verbose -i distributed/test_c10d
time python test/run_test.py --verbose -i distributed/test_c10d_spawn
assert_git_not_dirty

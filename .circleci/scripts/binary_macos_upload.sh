#!/bin/bash
# Do NOT set -x
set -eu -o pipefail
set +x
export AWS_ACCESS_KEY_ID="${PYTORCH_BINARY_AWS_ACCESS_KEY_ID}"
export AWS_SECRET_ACCESS_KEY="${PYTORCH_BINARY_AWS_SECRET_ACCESS_KEY}"

#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!
# DO NOT TURN -x ON BEFORE THIS LINE
#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!#!
set -eux -o pipefail

source "/Users/distiller/project/env"
export "PATH=$workdir/miniconda/bin:$PATH"

# This gets set in binary_populate_env.sh, but lets have a sane default just in case
PIP_UPLOAD_FOLDER=${PIP_UPLOAD_FOLDER:-nightly}
# TODO: Combine CONDA_UPLOAD_CHANNEL and PIP_UPLOAD_FOLDER into one variable
#       The only difference is the trailing slash
# Strip trailing slashes if there
CONDA_UPLOAD_CHANNEL=$(echo "${PIP_UPLOAD_FOLDER}" | sed 's:/*$::')
BACKUP_BUCKET="s3://pytorch-backup"

retry pip install -q awscli
pushd "$workdir/final_pkgs"
if [[ "$PACKAGE_TYPE" == conda ]]; then
  retry conda install -yq anaconda-client
  retry anaconda -t "${CONDA_PYTORCHBOT_TOKEN}" upload "$(ls)" -u "pytorch-${CONDA_UPLOAD_CHANNEL}" --label main --no-progress --force
  # Fetch  platform (eg. win-64, linux-64, etc.) from index file
  # Because there's no actual conda command to read this
  subdir=$(tar -xOf ./*.bz2 info/index.json | grep subdir  | cut -d ':' -f2 | sed -e 's/[[:space:]]//' -e 's/"//g' -e 's/,//')
  BACKUP_DIR="conda/${subdir}"
elif [[ "$PACKAGE_TYPE" == libtorch ]]; then
  s3_dir="s3://pytorch/libtorch/${PIP_UPLOAD_FOLDER}${DESIRED_CUDA}/"
  for pkg in $(ls); do
    retry aws s3 cp "$pkg" "$s3_dir" --acl public-read
  done
  BACKUP_DIR="libtorch/${PIP_UPLOAD_FOLDER}${DESIRED_CUDA}/"
else
  s3_dir="s3://pytorch/whl/${PIP_UPLOAD_FOLDER}${DESIRED_CUDA}/"
  retry aws s3 cp "$(ls)" "$s3_dir" --acl public-read
  BACKUP_DIR="whl/${PIP_UPLOAD_FOLDER}${DESIRED_CUDA}/"
fi

if [[ -n "${CIRCLE_TAG:-}" ]]; then
  s3_dir="${BACKUP_BUCKET}/${CIRCLE_TAG}/${BACKUP_DIR}"
  retry aws s3 cp --recursive . "$s3_dir"
fi

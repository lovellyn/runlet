#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

# The default Docker builder can consume the image loaded by the main image build.
docker build --builder default \
  --build-arg "BASE_IMAGE=${HF_TEST_BASE_IMAGE:-runlet:ci}" \
  --tag "${HF_TEST_IMAGE:-runlet:huggingface-ci}" \
  deploy/huggingface

python scripts/smoke_container.py \
  --image "${HF_TEST_IMAGE:-runlet:huggingface-ci}" \
  --expected-uid 1000 --expected-gid 1000

#!/usr/bin/env bash
# Shared CPU thread configuration — sourced (not executed) by start-*.sh.
#
# In a container os.cpu_count() reports the host's cores, not the cgroup CPU
# quota, so the BLAS / OpenMP pools used by numpy and sklearn (the Pass 1
# IsolationForest) can be badly mis-sized. Pin them to the vCPU allocation.
# TORCH_NUM_THREADS is additionally read by src/config.py to size the PyTorch
# intra-op pool. Override any of these per-service in the Railway dashboard.
#
# ':=' only sets the default when the variable is unset, so an explicit
# platform env var always wins.
: "${TORCH_NUM_THREADS:=8}"
: "${OMP_NUM_THREADS:=$TORCH_NUM_THREADS}"
: "${OPENBLAS_NUM_THREADS:=$TORCH_NUM_THREADS}"
: "${MKL_NUM_THREADS:=$TORCH_NUM_THREADS}"
: "${NUMEXPR_NUM_THREADS:=$TORCH_NUM_THREADS}"
export TORCH_NUM_THREADS OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS

echo "Thread env: TORCH_NUM_THREADS=${TORCH_NUM_THREADS} OMP_NUM_THREADS=${OMP_NUM_THREADS}"

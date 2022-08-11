#!/bin/sh

# Script to test that a generated BuildStream+BuildBox wheel package is
# functional in the PyPA "manylinux" container images.
#
# The test is run via `run-ci.sh` which in turn uses `docker-compose` to
# execute this script.

set -eux

WHEEL=$1
PYTHON=$2

"$PYTHON" -m venv /tmp/venv
/tmp/venv/bin/pip3 install "$WHEEL"

cd doc/examples/autotools
bst build hello.bst

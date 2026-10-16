#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ "$(uname -s)" != "Darwin" ]; then
  echo 'Run this script on a Mac.' >&2
  exit 1
fi
PYTHON="${PYTHON:-python3.12}"
if [ ! -x .venv-build/bin/python ]; then
  "$PYTHON" -m venv .venv-build
fi
.venv-build/bin/python -m pip install -r requirements-build.txt
.venv-build/bin/python build.py "$@"

#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec "${PYTHON:-python3}" scripts/update-icons.py

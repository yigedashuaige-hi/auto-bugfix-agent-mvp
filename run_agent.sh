#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "Please set OPENAI_API_KEY first."
  echo "Example: export OPENAI_API_KEY='sk-...'"
  exit 1
fi

python -m auto_bugfix_agent "$@"

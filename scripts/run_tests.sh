#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/igor/calories-bot"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"

cd "$PROJECT_DIR"

"$PYTHON_BIN" -m compileall -q calories_bot scripts
"$PYTHON_BIN" -m ruff format --check .
"$PYTHON_BIN" -m ruff check .
"$PYTHON_BIN" -m mypy calories_bot
"$PYTHON_BIN" -m pytest --cov=calories_bot --cov-branch
"$PYTHON_BIN" -m pip check

if [[ $# -eq 0 ]]; then
    exit 0
fi
case $1 in
    --llm)
        shift
        "$PYTHON_BIN" -m scripts.eval_llm "$@"
        ;;
    --e2e)
        shift
        "$PYTHON_BIN" -m scripts.eval_telegram_e2e "$@"
        ;;
    *)
        echo "Usage: bash scripts/run_tests.sh [--llm|--e2e] --confirm [options]" >&2
        exit 2
        ;;
esac

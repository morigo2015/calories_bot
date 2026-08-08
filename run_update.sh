#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 \"Commit message\"" >&2
    exit 2
fi

cd "$(dirname "$0")"

git add \
    AGENTS.md VPS_HELP.txt .env.example .gitignore \
    pyproject.toml requirements.txt requirements-dev.txt run_update.sh \
    calories_bot tests docs deploy scripts evals

if git diff --cached --quiet; then
    echo "No changes to commit."
else
    git commit -m "$1"
fi

git push origin HEAD

sudo systemctl restart calories-bot
sudo systemctl is-active --quiet calories-bot
echo "calories-bot is active."

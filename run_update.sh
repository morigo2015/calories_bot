#!/usr/bin/env bash
# Commit and publish safe source changes, then restart the production bot.

set -euo pipefail

if [[ $# -ne 1 || -z "${1// }" ]]; then
    echo "Usage: $0 \"Commit message\"" >&2
    exit 2
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
commit_message="$1"

cd "$project_dir"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Not a Git repository: $project_dir" >&2
    exit 1
fi

# Do not publish runtime secrets, user data, or local spreadsheet exports.
git add -u -- . \
    ':!*.env' \
    ':!service-account.json' \
    ':!data/' \
    ':!*.xlsx'

# Include new source and project configuration files, but never arbitrary
# untracked files. This keeps accidental exports and attachments out of Git.
while IFS= read -r path; do
    case "$path" in
        calories_bot/*.py|tests/*.py|scripts/*.py|deploy/*|docs/*.md|\
        *.md|pyproject.toml|requirements*.txt|run_update.sh|.gitignore)
            git add -- "$path"
            ;;
    esac
done < <(git ls-files --others --exclude-standard)

if git diff --cached --quiet; then
    echo "No publishable changes to commit."
else
    git commit -m "$commit_message"
fi

git push origin HEAD

sudo systemctl restart calories-bot
sudo systemctl is-active --quiet calories-bot
echo "calories-bot is active."

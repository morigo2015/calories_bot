# Workspace and deployment

This workspace is the live checkout on the Hetzner server. The project lives at
`/home/igor/calories-bot` and is owned by `igor:igor`.

- Do not SSH or copy files to another host to deploy changes: edits made here
  are already on the server.
- Do not restart the bot, commit, or push changes automatically after editing.
  The user runs the deployment script themselves.
- After every application-code update, give the user this exact command, with a
  concise commit message appropriate to the changes:

  ```bash
  bash /home/igor/calories-bot/run_update.sh "<commit message>"
  ```

- `run_update.sh` stages the approved project paths, creates a commit when
  needed, pushes it to `origin`, then runs `sudo systemctl restart calories-bot`
  and confirms that the service is active.
- A direct `sudo systemctl` command may require an interactive password. Do not
  try to work around that requirement; provide the deployment command above.
- Never include `.env`, `service-account.json`, `data/`, or spreadsheet exports
  in Git commits. Preserve unrelated user changes and untracked files.

# Testing

- After a significant application update, run `bash scripts/run_tests.sh`.
- Run the paid LLM eval only after the user explicitly approves it in the current
  conversation: `bash scripts/run_tests.sh --llm --confirm`.

# Permissions and sandboxing

- Keep project directories writable by their owner (`u+rwx`) and regular files
  writable by their owner (`u+rw`). Do not remove owner write permissions as a
  hardening step.
- Secrets such as `.env` and `service-account.json` must remain owner-only
  (`600`).

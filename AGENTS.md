# Workspace permissions

- Keep project directories writable by their owner (`u+rwx`) and regular files
  writable by their owner (`u+rw`). Do not remove owner write permissions as a
  hardening step; atomic editors, `apply_patch`, and Python bytecode generation
  need to create temporary files next to source files.
- The workspace owner is `igor`; secrets such as `.env` and
  `service-account.json` should remain owner-only (`600`).
- If a command fails with `bwrap: loopback: Failed RTM_NEWADDR: Operation not
  permitted`, treat it as a Codex sandbox/network-namespace failure, not as a
  repository permission problem. Retry the required command with sandbox
  escalation instead of changing file ownership or repeatedly auditing modes.

## VPS deployment

Apply these rules when deploying to the VPS:

- VPS SSH alias: `hetzner`; SSH user: `igor`.
- Project directory: `/home/igor/calories-bot`.
- Project files are owned by `igor:igor`; do not use `sudo` to update them.
- Use `sudo` only for systemd operations when it is available.

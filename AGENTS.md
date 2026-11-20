# AGENTS.md

Guidance for AI coding agents working on the **AutoHelix codebase**. This mirrors
the conventions in [`CLAUDE.md`](./CLAUDE.md) — read that for full detail.

## Quick orientation

- AutoHelix runs AI agents in an iterative optimization loop. Each iteration is
  isolated in private copies of the code, validated against constraints, and only
  accepted when it passes. Start in `src/autohelix/harness.py`.
- See [`README.md`](./README.md) for user-facing docs and the config reference.

## Working rules

- **Setup:** activate the venv first (`source .venv/bin/activate`). The package is an
  editable install — edits under `src/autohelix/` are live, no reinstall needed.
- **Test before you finish:** `pytest -n auto` (the fast suite, in parallel). Use
  `pytest -m slow` only when a real agent is needed.
- **Do not commit optimized example code** — `examples/` must keep the slow baseline so
  users can see the optimization potential.
- **No backward-compatibility shims:** an old config form or key stops working;
  a renamed key or flag is an error naming its replacement. New keys are optional.

For architecture notes, gotchas, and project layout, see [`CLAUDE.md`](./CLAUDE.md).

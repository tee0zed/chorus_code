# chorus-code

Multi-agent development swarm powered by Claude Code and git worktrees.

Multiple Claude agents work in isolated git worktrees and communicate through a shared SQLite blackboard. Each agent claims a signal, runs Claude, commits its changes, and publishes new signals for downstream agents.

## Install

```bash
pip install -e .
```

Requires `claude` CLI in PATH with an active session.

## Usage

```bash
# Interactive mode (TUI to configure a run)
chorus_code

# Non-interactive
chorus_code --repo /path/to/project --task "add pagination to the API" --config roles/swarm.yaml
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--repo` | — | Path to target git repository |
| `--task` | — | Task description (omit for interactive TUI) |
| `--config` | `roles/swarm.yaml` | Role config YAML |
| `--agents` | from config | Agent composition, e.g. `coder:3,selector:1,reviewer:1` |
| `--timeout` | 1800 | Max run time in seconds |
| `--no-tui` | false | Disable Textual monitor, print to stdout |

## Modes

### Swarm

`coder×N → selector → reviewer`

Multiple coders implement the same task independently in separate worktrees. The selector compares their diffs and cherry-picks the best implementation. The reviewer does a final check and can send `BUG_FOUND` back to a coder.

### Cooperative

`decomposer → developer×N → integrator → reviewer`

A decomposer splits the task into independent subtasks. Developers implement them in parallel. The integrator assembles the parts and resolves conflicts. The reviewer does a final check and can send `BUG_FOUND` back to a developer.

## Architecture

```
swarm.py          — orchestrator: spawns agent processes, runs TUI
agent.py          — agent loop: claim signal → run Claude → propagate commits → publish signal
blackboard.py     — SQLite signal bus (claim/mark-done/get-all)
worktree.py       — git worktree helpers (add, remove, permissions, CLAUDE.md)
monitor.py        — Textual TUI: live signal board + log pane
init_tui.py       — interactive setup form (mode picker, repo, task, agents, timeout)
roles/            — YAML configs and role definitions
```

### Signal propagation

Each agent runs in a `git worktree --detach` from current HEAD. After Claude finishes:
- Normal roles: commits are cherry-picked into the main repo.
- `no_propagate: true` roles (e.g. `coder` in swarm): commits stay in the object store; the diff and commit hash are stored in the signal payload so the selector can cherry-pick the winner.

### Adding a role

1. Add an entry to `roles/definitions.yaml` with `name`, `responds_to`, `produces`, `can_modify`, and `prompt`.
2. Reference it in a config YAML under `groups`.

#!/usr/bin/env python3
"""
Usage:
  python swarm.py --repo /path/to/project \
                  --task "описание задачи" \
                  --config roles/example.yaml \
                  --agents researcher:1,coder:2,reviewer:1 \
                  [--timeout 1800] [--no-tui]
"""
import argparse
import multiprocessing
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import yaml

from agent import run_agent
from blackboard import Blackboard
from models import Signal
from monitor import SwarmMonitor
import worktree as wt


def parse_agents_arg(agents_str: str) -> list[tuple[str, int]]:
    result = []
    for part in agents_str.split(","):
        name, _, count = part.partition(":")
        result.append((name.strip(), int(count) if count else 1))
    return result


def load_config(config_path: str, mode: str | None = None) -> dict:
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    # Multi-mode format: top-level keys are mode names (no "roles"/"groups" at root)
    if "roles" not in raw and "groups" not in raw:
        if not mode:
            raise ValueError(
                f"Config '{config_path}' has multiple modes but --mode not specified: {list(raw.keys())}"
            )
        if mode not in raw:
            raise ValueError(f"Mode '{mode}' not found. Available: {list(raw.keys())}")
        raw = raw[mode]

    # Load role definitions if referenced
    definitions: dict[str, dict] = {}
    if "definitions" in raw:
        def_path = Path(config_path).parent / raw["definitions"]
        with open(def_path) as f:
            def_data = yaml.safe_load(f)
        definitions = {r["name"]: r for r in def_data.get("roles", [])}

    # Groups format: merge definitions + overrides, flatten to roles list
    if "groups" in raw and "roles" not in raw:
        roles, agent_parts = [], []
        for group in raw["groups"]:
            for entry in group["roles"]:
                role_name = entry["role"]
                count = entry.get("count", 1)
                base = dict(definitions.get(role_name, {"name": role_name}))
                overrides = {k: v for k, v in entry.items() if k not in ("role", "count")}
                merged = {**base, **overrides, "name": role_name}
                roles.append(merged)
                agent_parts.append(f"{role_name}:{count}")
        raw = {**raw, "roles": roles, "default_agents": ",".join(agent_parts)}

    return raw


def _agent_worker(role_config, task, repo_path, db_path, run_dir, stop_signal, log_path):
    with open(log_path, "a", buffering=1) as log:
        sys.stdout = log
        sys.stderr = log
        try:
            run_agent(role_config, task, repo_path, db_path, run_dir, stop_signal)
        except Exception as e:
            print(f"[agent] FATAL: {e}", flush=True)


def _write_report(db_path: str, stop_signal: str, log_path: str):
    """Extract final content from stop signal and append to log."""
    try:
        sig = Blackboard(db_path).get_last(stop_signal)
    except Exception:
        return
    if not sig:
        return
    content = sig.get("payload", {}).get("content", "").strip()
    if not content:
        return
    sep = "=" * 60
    with open(log_path, "a") as f:
        f.write(f"\n{sep}\n[swarm] REPORT:\n{content}\n{sep}\n")


def _run_swarm(args, roles_by_name: dict, stop_signal: str) -> str | None:
    """Run one swarm iteration. Returns next task or None to exit."""
    run_id = uuid.uuid4().hex[:8]
    run_dir = f"/tmp/swarm-{run_id}"
    os.makedirs(run_dir, exist_ok=True)
    db_path = f"{run_dir}/blackboard"
    log_path = f"{run_dir}/swarm.log"

    board = Blackboard(db_path)
    board.write(Signal(type="TASK_DEFINED", payload={"task": args.task}, from_role="orchestrator"))

    agent_specs = parse_agents_arg(args.agents)
    for role_name, count in agent_specs:
        role = roles_by_name.get(role_name, {})
        if role.get("fan_out") and count > 1:
            for _ in range(count - 1):
                board.write(Signal(type="TASK_DEFINED", payload={"task": args.task}, from_role="orchestrator"))

    processes: list[multiprocessing.Process] = []
    for role_name, count in agent_specs:
        if role_name not in roles_by_name:
            with open(log_path, "a") as f:
                f.write(f"[swarm] WARNING: role '{role_name}' not in config, skipping\n")
            continue
        role_config = roles_by_name[role_name]
        for _ in range(count):
            p = multiprocessing.Process(
                target=_agent_worker,
                args=(role_config, args.task, args.repo, db_path, run_dir, stop_signal, log_path),
                daemon=True,
            )
            p.start()
            processes.append(p)

    with open(log_path, "a") as f:
        f.write(f"[swarm] run_id={run_id}  db={db_path}\n")
        f.write(f"[swarm] {len(processes)} agents started\n")

    if args.no_tui:
        # No TUI: watch loop runs in main thread
        deadline = time.time() + args.timeout
        completed = False
        try:
            while time.time() < deadline:
                if board.has_signal_of_type(stop_signal):
                    _write_report(db_path, stop_signal, log_path)
                    completed = True
                    break
                if not any(p.is_alive() for p in processes):
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            pass
        for p in processes:
            p.terminate()
        for p in processes:
            p.join(timeout=5)
        if not completed:
            print(f"Completed (no stop_signal). log: {log_path}")
            return None
        print(f"\nTask completed. log: {log_path}\n")
        try:
            return input("Next task (Enter — exit): ").strip() or None
        except (EOFError, KeyboardInterrupt):
            return None

    # TUI mode: textual MUST run in main thread.
    # Swarm watcher runs in background thread and signals the TUI when done.
    stop_event = threading.Event()  # set on abnormal termination → TUI closes
    monitor = SwarmMonitor(db_path, args.task, run_id, log_path)

    def _swarm_watcher() -> None:
        completed = False
        deadline = time.time() + args.timeout
        try:
            while time.time() < deadline:
                if board.has_signal_of_type(stop_signal):
                    with open(log_path, "a") as f:
                        f.write(f"[swarm] stop signal '{stop_signal}' received\n")
                    _write_report(db_path, stop_signal, log_path)
                    completed = True
                    monitor.set_complete()  # uses call_from_thread internally
                    return  # don't set stop_event — TUI waits for user input
                if not any(p.is_alive() for p in processes):
                    with open(log_path, "a") as f:
                        f.write("[swarm] all agents finished\n")
                    break
                time.sleep(2)
            else:
                with open(log_path, "a") as f:
                    f.write(f"[swarm] timeout after {args.timeout}s\n")
        finally:
            for p in processes:
                p.terminate()
            for p in processes:
                p.join(timeout=5)
            if not completed:
                stop_event.set()  # tell TUI to exit (abnormal)

    watcher = threading.Thread(target=_swarm_watcher, daemon=True)
    watcher.start()

    # Blocks in main thread until user exits TUI
    return monitor.run(stop_event)


def main():
    parser = argparse.ArgumentParser(description="Agent Swarm")
    parser.add_argument("--repo")
    parser.add_argument("--task")
    parser.add_argument("--config")
    parser.add_argument("--mode")
    parser.add_argument("--agents")
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--provider", choices=["claude", "codex"], default=None)
    parser.add_argument("--no-tui", action="store_true")
    args = parser.parse_args()

    if not args.task:
        from init_tui import run as init_run
        cfg = init_run()
        args.repo     = args.repo     or cfg.repo
        args.task     = cfg.task
        args.config   = args.config   or cfg.config
        args.mode     = args.mode     or cfg.mode
        args.agents   = args.agents   or cfg.agents
        args.timeout  = args.timeout  or cfg.timeout
        args.provider = args.provider or cfg.provider

    if not args.config:
        args.config = str(Path(__file__).parent / "roles" / "swarm.yaml")
    if not args.timeout:
        args.timeout = 1800
    if not args.repo:
        parser.error("--repo is required (or omit --task to use interactive mode)")

    config = load_config(args.config, args.mode)
    if not args.agents:
        args.agents = config.get("default_agents", "researcher:1,coder:1,reviewer:1")
    provider = getattr(args, "provider", None) or "claude"
    roles_by_name = {r["name"]: {**r, "provider": provider} for r in config["roles"]}
    stop_signal = config.get("stop_signal", "DONE")

    wt._ensure_commit(args.repo)

    while True:
        next_task = _run_swarm(args, roles_by_name, stop_signal)
        if not next_task:
            break
        args.task = next_task


if __name__ == "__main__":
    main()

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import worktree as wt
from blackboard import Blackboard
from filelock import FileLockManager
from models import Signal


def _build_prompt(task: str, role_config: dict, signal: Signal, context: list) -> str:
    context_text = json.dumps(context, ensure_ascii=False, indent=2) if context else "[]"
    payload_text = json.dumps(signal.payload, ensure_ascii=False, indent=2)
    prompt = role_config["prompt"].format(
        task=task,
        context=context_text,
        signal_payload=payload_text,
    )
    return prompt


def _extract_text_from_json_output(raw: str) -> str | None:
    """Extract the final text content from claude --output-format json response."""
    try:
        data = json.loads(raw)
        # {"type":"result","subtype":"success","result":"...","cost_usd":...}
        if isinstance(data, dict) and "result" in data:
            return data["result"]
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _parse_output(output: str) -> list[dict] | dict | None:
    """Extract JSON signal(s) from claude output. Returns list for multi-signal output."""
    # Try list first (planner produces multiple signals)
    list_matches = re.findall(r"```json\s*(\[.*?\])\s*```", output, re.DOTALL)
    if list_matches:
        try:
            data = json.loads(list_matches[-1])
            if isinstance(data, list) and all(isinstance(x, dict) for x in data):
                return data
        except json.JSONDecodeError:
            pass
    # Try single object in code block
    obj_matches = re.findall(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
    if obj_matches:
        try:
            return json.loads(obj_matches[-1])
        except json.JSONDecodeError:
            pass
    # Fallback: last line that looks like JSON object or array
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if (line.startswith("{") and line.endswith("}")) or \
           (line.startswith("[") and line.endswith("]")):
            try:
                data = json.loads(line)
                return data
            except json.JSONDecodeError:
                pass
    return None


def run_agent(
    role_config: dict,
    task: str,
    repo_path: str,
    db_path: str,
    run_dir: str,
    stop_signal: str,
):
    role_name = role_config["name"]
    responds_to = role_config["responds_to"]
    produces = role_config["produces"]
    can_modify = role_config.get("can_modify", [])
    agent_id = f"{role_name}-{uuid.uuid4().hex[:8]}"

    worktree_path = str(Path(run_dir) / agent_id)
    board = Blackboard(db_path)
    locks = FileLockManager(db_path)

    wt.add(repo_path, worktree_path)
    wt.apply_permissions(worktree_path, can_modify)
    wt.write_claude_md(worktree_path, role_name, role_config["prompt"], can_modify)

    print(f"[{agent_id}] started, responds_to={responds_to}, can_modify={can_modify or 'none'}", flush=True)

    try:
        while True:
            if board.has_signal_of_type(stop_signal):
                break

            signal = board.claim_next(responds_to, agent_id)
            if signal is None:
                time.sleep(2)
                continue

            print(f"[{agent_id}] claimed {signal.type} ({signal.id[:8]})", flush=True)

            # Acquire file locks for files this agent intends to modify
            active_locks = {}
            if can_modify:
                locked_files = locks.get_all_locks()
                blocked = [f for f, holder in locked_files.items() if holder != agent_id]
                if blocked:
                    print(f"[{agent_id}] waiting, files locked by others: {blocked}", flush=True)
                    board.unclaim(signal.id)
                    time.sleep(5)
                    continue
                # Register intent to write — agent will resolve actual files during execution
                locks.acquire(f"__signal__{signal.id}", agent_id)
                active_locks[f"__signal__{signal.id}"] = True

            context = board.get_all_signals()
            prompt = _build_prompt(task, role_config, signal, context)

            result = subprocess.run(
                [
                    "claude", "--print", prompt,
                    "--dangerously-skip-permissions",
                    "--output-format", "json",
                ],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = _extract_text_from_json_output(result.stdout) or result.stdout

            parsed = _parse_output(output)
            out_signals: list[tuple[str, str]]
            if isinstance(parsed, list):
                out_signals = [(item.get("signal", produces), item.get("content", "")) for item in parsed]
            elif isinstance(parsed, dict):
                out_signals = [(parsed.get("signal", produces), parsed.get("content", output))]
            else:
                out_signals = [(produces, output.strip())]

            for signal_type, content in out_signals:
                board.write(Signal(
                    type=signal_type,
                    payload={"content": content, "from_signal": signal.id},
                    from_role=role_name,
                ))
                print(f"[{agent_id}] wrote {signal_type}", flush=True)

            board.mark_done(signal.id)
            locks.release_all(agent_id)

    finally:
        locks.release_all(agent_id)
        wt.remove(repo_path, worktree_path)
        print(f"[{agent_id}] done, worktree removed", flush=True)

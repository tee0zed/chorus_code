import json
import os
import pty
import re
import select
import subprocess
import time
import uuid
from pathlib import Path

import worktree as wt
from blackboard import Blackboard
from models import Signal

_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _build_prompt(task: str, role_config: dict, signal: Signal, context: list) -> str:
    context_text = json.dumps(context, ensure_ascii=False, indent=2) if context else "[]"
    payload_text = json.dumps(signal.payload, ensure_ascii=False, indent=2)
    prompt = role_config["prompt"]
    prompt = prompt.replace("{task}", task)
    prompt = prompt.replace("{context}", context_text)
    prompt = prompt.replace("{signal_payload}", payload_text)
    return prompt


def _parse_output(output: str) -> list[dict] | dict | None:
    """Extract JSON signal(s) from claude output."""
    list_matches = re.findall(r"```json\s*(\[.*?\])\s*```", output, re.DOTALL)
    if list_matches:
        try:
            data = json.loads(list_matches[-1])
            if isinstance(data, list) and all(isinstance(x, dict) for x in data):
                return data
        except json.JSONDecodeError:
            pass
    obj_matches = re.findall(r"```json\s*(\{.*?\})\s*```", output, re.DOTALL)
    if obj_matches:
        try:
            return json.loads(obj_matches[-1])
        except json.JSONDecodeError:
            pass
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if (line.startswith("{") and line.endswith("}")) or \
           (line.startswith("[") and line.endswith("]")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return None


def _run_codex(agent_id: str, prompt: str, worktree_path: str) -> str:
    """Run codex CLI via PTY, return full output as plain text."""
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["codex", "--approval-mode", "full-auto", prompt],
        cwd=worktree_path,
        stdin=subprocess.DEVNULL,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    buf = b""
    lines: list[str] = []

    try:
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break
            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = _strip_ansi(raw.decode("utf-8", errors="replace")).strip()
                    if line:
                        lines.append(line)
                        print(f"[{agent_id}] {line}", flush=True)
            elif proc.poll() is not None:
                try:
                    while True:
                        chunk = os.read(master_fd, 4096)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    pass
                for raw in buf.split(b"\n"):
                    line = _strip_ansi(raw.decode("utf-8", errors="replace")).strip()
                    if line:
                        lines.append(line)
                        print(f"[{agent_id}] {line}", flush=True)
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    return "\n".join(lines)


def _run_claude(agent_id: str, prompt: str, worktree_path: str) -> str:
    """Run claude via PTY so it line-buffers. Stream events to log in real time."""
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["claude", "--print", prompt,
         "--dangerously-skip-permissions",
         "--output-format", "stream-json",
         "--verbose"],
        cwd=worktree_path,
        stdin=subprocess.DEVNULL,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )
    os.close(slave_fd)

    buf = b""
    result_text = None

    def _process_line(line: str) -> None:
        nonlocal result_text
        if not line:
            return
        try:
            event = json.loads(line)
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        for tline in block["text"].splitlines():
                            if tline.strip():
                                print(f"[{agent_id}] {tline}", flush=True)
                    elif block.get("type") == "tool_use":
                        print(f"[{agent_id}] → {block.get('name', '?')}", flush=True)
            elif etype == "result":
                result_text = event.get("result", "")
                if event.get("subtype") != "success":
                    print(f"[{agent_id}] result:{event.get('subtype')}", flush=True)
        except json.JSONDecodeError:
            # non-JSON (e.g. stderr mixed in) — print directly
            if line and not line.startswith("{"):
                print(f"[{agent_id}] {line}", flush=True)

    try:
        while True:
            try:
                r, _, _ = select.select([master_fd], [], [], 0.5)
            except (ValueError, OSError):
                break
            if r:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    _process_line(_strip_ansi(raw.decode("utf-8", errors="replace")).strip())
            elif proc.poll() is not None:
                # Drain remaining bytes after process exits
                try:
                    while True:
                        chunk = os.read(master_fd, 4096)
                        if not chunk:
                            break
                        buf += chunk
                except OSError:
                    pass
                for raw in buf.split(b"\n"):
                    _process_line(_strip_ansi(raw.decode("utf-8", errors="replace")).strip())
                break
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    return result_text or ""


def _repo_head(path: str) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    return r.stdout.strip()


def _propagate_commits(repo_path: str, wt_head: str, original_head: str, agent_id: str) -> str:
    """Cherry-pick agent commits (by hash) into main repo — worktree may already be removed.
    Git objects persist after worktree removal so cherry-pick works by hash.
    Returns unified diff or empty string on failure."""
    try:
        new_commits = subprocess.run(
            ["git", "rev-list", "--reverse", f"{original_head}..{wt_head}"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout.strip().split()
        new_commits = [c for c in new_commits if c]
        if not new_commits:
            return ""

        # Stash dirty working tree so cherry-pick doesn't fail on conflicts with
        # uncommitted changes that aren't part of the agent's work.
        stash_result = subprocess.run(
            ["git", "stash", "--include-untracked", "-m", f"propagate-{agent_id}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        stashed = stash_result.returncode == 0 and "No local changes" not in stash_result.stdout

        try:
            for commit in new_commits:
                result = subprocess.run(
                    ["git", "cherry-pick", "--allow-empty", commit],
                    cwd=repo_path, capture_output=True, text=True,
                )
                if result.returncode != 0:
                    subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_path, capture_output=True)
                    print(f"[{agent_id}] cherry-pick conflict on {commit[:8]}, skipping", flush=True)
                    return ""
        finally:
            if stashed:
                subprocess.run(["git", "stash", "pop"], cwd=repo_path, capture_output=True)

        diff = subprocess.run(
            ["git", "diff", f"{original_head}..HEAD"],
            cwd=repo_path, capture_output=True, text=True,
        ).stdout
        print(f"[{agent_id}] propagated {len(new_commits)} commit(s) to repo", flush=True)
        return diff[:6000]
    except Exception as e:
        print(f"[{agent_id}] propagate error: {e}", flush=True)
        return ""


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

    board = Blackboard(db_path)

    # Worktrees created per-signal so each run starts from current HEAD,
    # which includes commits propagated by earlier agents.
    print(f"[{agent_id}] ready, responds_to={responds_to}, can_modify={can_modify or 'none'}", flush=True)

    try:
        while True:
            if board.has_signal_of_type(stop_signal):
                break

            signal = board.claim_next(responds_to, agent_id)
            if signal is None:
                time.sleep(2)
                continue

            print(f"[{agent_id}] claimed {signal.type} ({signal.id[:8]})", flush=True)

            # Fresh worktree from current HEAD — sees all previous agents' commits
            worktree_path = str(Path(run_dir) / f"{agent_id}-{signal.id[:8]}")
            wt.add(repo_path, worktree_path)
            wt.write_claude_md(worktree_path, role_name, role_config["prompt"], can_modify)
            wt.apply_permissions(worktree_path, can_modify)

            head_before = _repo_head(repo_path) if can_modify else ""
            wt_head_after = ""
            try:
                context = board.get_all_signals()
                prompt = _build_prompt(task, role_config, signal, context)
                provider = role_config.get("provider", "claude")
                output = (_run_codex if provider == "codex" else _run_claude)(agent_id, prompt, worktree_path)
                if can_modify:
                    wt_head_after = _repo_head(worktree_path)
            finally:
                wt.remove(repo_path, worktree_path)

            diff = ""
            wt_head_export = ""
            if can_modify and head_before and wt_head_after and wt_head_after != head_before:
                if role_config.get("no_propagate"):
                    # Keep commits in object store; export diff+hash for selector to cherry-pick
                    diff = subprocess.run(
                        ["git", "diff", f"{head_before}..{wt_head_after}"],
                        cwd=repo_path, capture_output=True, text=True,
                    ).stdout[:6000]
                    wt_head_export = wt_head_after
                    print(f"[{agent_id}] stored diff ({len(diff)}b) wt_head={wt_head_after[:8]}", flush=True)
                else:
                    diff = _propagate_commits(repo_path, wt_head_after, head_before, agent_id)

            parsed = _parse_output(output)
            if isinstance(parsed, list):
                out_signals = [(item.get("signal", produces), item.get("content", "")) for item in parsed]
            elif isinstance(parsed, dict):
                out_signals = [(parsed.get("signal", produces), parsed.get("content", output))]
            else:
                out_signals = [(produces, output.strip())]

            for signal_type, content in out_signals:
                payload: dict = {"content": content, "from_signal": signal.id}
                if diff:
                    payload["diff"] = diff
                if wt_head_export:
                    payload["wt_head"] = wt_head_export
                board.write(Signal(type=signal_type, payload=payload, from_role=role_name))
                print(f"[{agent_id}] wrote {signal_type}", flush=True)

            board.mark_done(signal.id)

    finally:
        print(f"[{agent_id}] done", flush=True)

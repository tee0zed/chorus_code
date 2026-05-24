import fnmatch
import os
import subprocess
from pathlib import Path


def _ensure_commit(repo_path: str) -> None:
    """Create empty initial commit if repo has none."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path, capture_output=True,
    )
    if result.returncode != 0:
        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "chorus_code: init"],
            cwd=repo_path, capture_output=True,
        )


def add(repo_path: str, worktree_path: str, branch: str = "HEAD") -> None:
    _ensure_commit(repo_path)
    subprocess.run(
        ["git", "worktree", "add", "--detach", worktree_path, branch],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


def remove(repo_path: str, worktree_path: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=repo_path,
        check=False,
        capture_output=True,
    )


def apply_permissions(worktree_path: str, can_modify: list[str]) -> None:
    """Make all files read-only, then restore write for patterns in can_modify."""
    root = Path(worktree_path)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        writable = any(fnmatch.fnmatch(rel, pat) for pat in can_modify)
        mode = path.stat().st_mode
        if writable:
            path.chmod(mode | 0o200)
        else:
            path.chmod(mode & ~0o222)


def write_claude_md(
    worktree_path: str,
    role_name: str,
    role_prompt: str,
    can_modify: list[str],
) -> None:
    template_path = Path(__file__).parent / "templates" / "claude_md.j2"
    template = template_path.read_text()
    patterns_text = "\n".join(f"  - {p}" for p in can_modify) if can_modify else "  (нет — только чтение)"
    content = (
        template
        .replace("{{role_name}}", role_name)
        .replace("{{role_prompt}}", role_prompt)
        .replace("{{can_modify_patterns}}", patterns_text)
    )
    claude_md = Path(worktree_path) / "CLAUDE.md"
    claude_md.write_text(content)
    # CLAUDE.md itself must always be writable so agent can re-read it
    claude_md.chmod(claude_md.stat().st_mode | 0o200)

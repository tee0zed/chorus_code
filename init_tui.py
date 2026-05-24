"""
Interactive initialization TUI for agent swarm.
Shows a form to configure a run, then returns the config dict.
"""
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

SWARM_DIR = Path(__file__).parent
ROLES_DIR = SWARM_DIR / "roles"

console = Console()


@dataclass
class SwarmConfig:
    repo: str
    task: str
    config: str
    mode: str
    agents: str
    timeout: int
    provider: str = "claude"


def _banner():
    title = Text()
    title.append("  ⬡ ", style="bold yellow")
    title.append("Agent Swarm", style="bold white")
    title.append(" ⬡  ", style="bold yellow")
    console.print()
    console.print(Panel(title, border_style="blue", expand=False, padding=(0, 4)))
    console.print()


def _is_git_repo(path: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=path,
        capture_output=True,
    )
    return result.returncode == 0


def _pick_repo() -> str:
    cwd = str(Path.cwd())
    if _is_git_repo(cwd):
        default = cwd
        hint = f"[dim](current directory: {cwd})[/dim]"
    else:
        default = ""
        hint = "[yellow]current directory is not a git repo[/yellow]"

    console.print(f"  Repository  {hint}")
    while True:
        repo = Prompt.ask("  Path", default=default or None, console=console)
        if not repo:
            console.print("  [red]Specify repository path[/red]")
            continue
        repo = str(Path(repo).expanduser().resolve())
        if not Path(repo).exists():
            console.print(f"  [red]Path does not exist: {repo}[/red]")
            continue
        if not _is_git_repo(repo):
            console.print(f"  [red]Not a git repository: {repo}[/red]")
            continue
        return repo



def _pick_mode_global() -> tuple[str, str, dict]:
    """Scan all yaml configs, show a flat list of all modes with descriptions.
    Returns (config_path, mode_key, mode_cfg).
    Handles two formats:
      - single-mode: groups/roles at root → one entry per file
      - multimode:   top-level keys are mode dicts → one entry per mode
    """
    import yaml as _yaml

    entries: list[tuple[Path, str, dict]] = []  # (yaml_path, mode_key, mode_cfg)
    for y in sorted(ROLES_DIR.glob("*.yaml")):
        try:
            data = _yaml.safe_load(y.read_text())
        except Exception as e:
            console.print(f"\n  [red]Cannot parse {y.name}:[/red] {e}")
            sys.exit(1)
        if not data or not isinstance(data, dict):
            console.print(f"\n  [red]Config is empty or invalid:[/red] {y.name}")
            sys.exit(1)
        if "groups" in data:
            # Single-mode runnable config — the file itself is the mode
            entries.append((y, "", data))
        else:
            # Multimode — each sub-key with groups/roles is a mode
            for key, cfg in data.items():
                if isinstance(cfg, dict) and ("groups" in cfg or "roles" in cfg):
                    entries.append((y, key, cfg))

    if not entries:
        console.print(f"  [red]No valid modes found in {ROLES_DIR}[/red]")
        sys.exit(1)

    console.print("  [bold]Mode:[/bold]")
    for i, (y, key, cfg) in enumerate(entries, 1):
        label = cfg.get("label", key or y.stem)
        desc  = cfg.get("description", "")
        console.print(f"    [cyan]{i}[/cyan]  [bold]{label}[/bold]  [dim]{desc}[/dim]")
    console.print()

    while True:
        choice = Prompt.ask("  Number", default="1", console=console)
        if choice.isdigit() and 0 <= int(choice) - 1 < len(entries):
            y, key, cfg = entries[int(choice) - 1]
            return str(y), key, cfg
        console.print(f"  [red]Enter a number from 1 to {len(entries)}[/red]")


def _pick_agents(mode_cfg: dict) -> str:
    # Support both flat roles list and groups format
    if "groups" in mode_cfg:
        parts = []
        for group in mode_cfg["groups"]:
            for entry in group["roles"]:
                name = entry.get("role") or entry.get("name", "?")
                count = entry.get("count", 1)
                parts.append((name, count))
    else:
        parts = [(r["name"], r.get("count", 1)) for r in mode_cfg.get("roles", [])]

    role_names = [name for name, _ in parts]
    default = mode_cfg.get("default_agents") or ",".join(f"{n}:{c}" for n, c in parts)
    console.print()
    console.print(f"  [bold]Agents[/bold]  [dim]format: role:count[/dim]")
    console.print(f"  [dim]available roles: {', '.join(role_names)}[/dim]")
    return Prompt.ask("  Composition", default=default, console=console)


def _pick_provider() -> str:
    providers = [
        ("claude", "Claude Code  (claude CLI)"),
        ("codex",  "OpenAI Codex (codex CLI)"),
    ]
    console.print("  [bold]Provider[/bold]")
    for i, (key, label) in enumerate(providers, 1):
        console.print(f"    [cyan]{i}[/cyan]  {label}")
    console.print()
    while True:
        choice = Prompt.ask("  Number", default="1", console=console)
        if choice.isdigit() and 0 <= int(choice) - 1 < len(providers):
            return providers[int(choice) - 1][0]
        console.print(f"  [red]Enter 1 or 2[/red]")


def _pick_task() -> str:
    console.print()
    console.print("  [bold]Task[/bold]")
    while True:
        task = Prompt.ask("  Description", console=console)
        if task.strip():
            return task.strip()
        console.print("  [red]Task cannot be empty[/red]")


def _pick_timeout() -> int:
    console.print()
    raw = Prompt.ask("  Timeout (sec)", default="600", console=console)
    try:
        return int(raw)
    except ValueError:
        return 600


def _summary(cfg: SwarmConfig):
    console.print()
    t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    t.add_column(style="dim", width=12)
    t.add_column()
    t.add_row("repo",     cfg.repo)
    t.add_row("config",   Path(cfg.config).name)
    t.add_row("mode",     cfg.mode or "-")
    t.add_row("provider", cfg.provider)
    t.add_row("agents",   cfg.agents)
    t.add_row("timeout",  f"{cfg.timeout}s")
    t.add_row("task",     Text(cfg.task, style="bold"))
    console.print(Panel(t, title="Launch parameters", border_style="blue"))


def run() -> SwarmConfig:
    _banner()

    config_path, mode, mode_cfg = _pick_mode_global()
    console.print()

    provider = _pick_provider()
    repo     = _pick_repo()
    task     = _pick_task()
    agents   = _pick_agents(mode_cfg)
    timeout  = _pick_timeout()

    cfg = SwarmConfig(
        repo=repo, task=task, config=config_path,
        mode=mode, agents=agents, timeout=timeout, provider=provider,
    )
    _summary(cfg)

    console.print()
    if not Confirm.ask("  Launch?", default=True, console=console):
        console.print("  [dim]Cancelled.[/dim]")
        sys.exit(0)

    console.print()
    return cfg

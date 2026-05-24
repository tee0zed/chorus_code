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


def _load_yaml(path: str) -> dict:
    import yaml
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        console.print(f"\n  [red]Cannot parse config {path}:[/red] {e}")
        sys.exit(1)
    if not raw or not isinstance(raw, dict):
        console.print(f"\n  [red]Config is empty or invalid:[/red] {path}")
        sys.exit(1)
    return raw


def _config_modes(raw: dict) -> dict:
    """Return only the mode entries: top-level keys whose values are dicts with groups/roles."""
    return {k: v for k, v in raw.items() if isinstance(v, dict) and ("groups" in v or "roles" in v)}


def _pick_config_file() -> str:
    """Let user pick a multimode yaml config from roles/."""
    import yaml as _yaml
    all_yamls = sorted(ROLES_DIR.glob("*.yaml"))
    yamls = []
    for y in all_yamls:
        try:
            data = _yaml.safe_load(y.read_text())
            if isinstance(data, dict) and _config_modes(data):
                yamls.append(y)
        except Exception:
            pass
    if not yamls:
        console.print(f"  [red]No valid configs in {ROLES_DIR}[/red]")
        sys.exit(1)
    if len(yamls) == 1:
        console.print(f"  [dim]→ config: {yamls[0].name}[/dim]")
        return str(yamls[0])
    console.print("  [bold]Config:[/bold]")
    for i, y in enumerate(yamls, 1):
        console.print(f"    [cyan]{i}[/cyan]  {y.name}")
    while True:
        raw = Prompt.ask("  Number or path", default="1", console=console)
        if raw.isdigit() and 0 <= int(raw) - 1 < len(yamls):
            return str(yamls[int(raw) - 1])
        p = Path(raw).expanduser()
        if p.exists():
            return str(p)
        console.print("  [red]Not found[/red]")


def _pick_mode(config_path: str, raw: dict) -> str:
    """Pick mode from a multimode yaml. Returns mode key."""
    modes = _config_modes(raw)
    keys = list(modes.keys())

    console.print("  [bold]Mode:[/bold]")
    for i, key in enumerate(keys, 1):
        meta = modes[key]
        label = meta.get("label", key)
        desc  = meta.get("description", "")
        console.print(f"    [cyan]{i}[/cyan]  [bold]{label}[/bold]  [dim]{desc}[/dim]")
    console.print()

    while True:
        choice = Prompt.ask("  Mode", default="1", console=console)
        if choice.isdigit() and 0 <= int(choice) - 1 < len(keys):
            return keys[int(choice) - 1]
        if choice in keys:
            return choice
        console.print(f"  [red]Enter a number from 1 to {len(keys)}[/red]")


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
    t.add_row("repo",    cfg.repo)
    t.add_row("config",  Path(cfg.config).name)
    t.add_row("mode",    cfg.mode or "-")
    t.add_row("agents",  cfg.agents)
    t.add_row("timeout", f"{cfg.timeout}s")
    t.add_row("task",    Text(cfg.task, style="bold"))
    console.print(Panel(t, title="Launch parameters", border_style="blue"))


def run() -> SwarmConfig:
    _banner()

    config_path = _pick_config_file()
    raw = _load_yaml(config_path)

    modes = _config_modes(raw)
    if not modes:
        console.print(f"\n  [red]Config has no valid modes:[/red] {config_path}")
        sys.exit(1)

    console.print()
    mode = _pick_mode(config_path, raw)
    mode_cfg = raw[mode]
    console.print()

    repo    = _pick_repo()
    task    = _pick_task()
    agents  = _pick_agents(mode_cfg)
    timeout = _pick_timeout()

    cfg = SwarmConfig(
        repo=repo, task=task, config=config_path,
        mode=mode, agents=agents, timeout=timeout,
    )
    _summary(cfg)

    console.print()
    if not Confirm.ask("  Launch?", default=True, console=console):
        console.print("  [dim]Cancelled.[/dim]")
        sys.exit(0)

    console.print()
    return cfg

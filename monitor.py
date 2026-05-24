import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from blackboard import Blackboard
from filelock import FileLockManager


STATUS_STYLE = {
    "open":    "yellow",
    "claimed": "cyan",
    "done":    "green",
}


class SwarmMonitor:
    def __init__(self, db_path: str, task: str, run_id: str, log_path: str):
        self.board = Blackboard(db_path)
        self.locks = FileLockManager(db_path)
        self.task = task
        self.run_id = run_id
        self.log_path = Path(log_path)
        self.start = datetime.now(timezone.utc)
        self._log_lines: deque[str] = deque(maxlen=20)

    def _elapsed(self) -> str:
        secs = int((datetime.now(timezone.utc) - self.start).total_seconds())
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _read_log(self):
        if not self.log_path.exists():
            return
        try:
            text = self.log_path.read_text(errors="replace")
            lines = text.splitlines()
            self._log_lines = deque(lines[-20:], maxlen=20)
        except OSError:
            pass

    def _signals_table(self) -> Table:
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold", expand=True)
        t.add_column("Сигнал", style="bold")
        t.add_column("Статус", width=8)
        t.add_column("От", width=14)
        t.add_column("Время", width=10)

        import sqlite3, json
        try:
            conn = sqlite3.connect(self.board.db_path, timeout=3)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT type, status, from_role, created_at, claimed_by "
                "FROM signals ORDER BY created_at DESC LIMIT 30"
            ).fetchall()
            conn.close()
        except Exception:
            rows = []

        for r in rows:
            style = STATUS_STYLE.get(r["status"], "")
            ts = r["created_at"][:19].replace("T", " ") if r["created_at"] else ""
            from_label = r["claimed_by"] if r["status"] == "claimed" else r["from_role"]
            t.add_row(
                r["type"],
                Text(r["status"], style=style),
                from_label or "-",
                ts,
            )
        if not rows:
            t.add_row("-", "-", "-", "-")
        return t

    def _locks_table(self) -> Table:
        t = Table(box=box.SIMPLE, show_header=True, header_style="bold", expand=True)
        t.add_column("Файл")
        t.add_column("Агент", width=20)
        active = self.locks.get_all_locks()
        for fp, agent in active.items():
            t.add_row(fp, Text(agent, style="cyan"))
        if not active:
            t.add_row(Text("(нет)", style="dim"), "")
        return t

    def _log_panel(self) -> Panel:
        self._read_log()
        lines = list(self._log_lines)
        text = Text()
        for line in lines:
            if "ERROR" in line or "error" in line:
                text.append(line + "\n", style="red")
            elif "claimed" in line:
                text.append(line + "\n", style="cyan")
            elif "wrote" in line or "done" in line:
                text.append(line + "\n", style="green")
            elif "started" in line:
                text.append(line + "\n", style="yellow")
            else:
                text.append(line + "\n", style="dim")
        return Panel(text, title="Лог", border_style="dim")

    def render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="log", size=12),
        )
        layout["main"].split_row(
            Layout(name="signals"),
            Layout(name="locks", size=40),
        )

        task_short = self.task[:70] + "…" if len(self.task) > 70 else self.task
        header_text = Text()
        header_text.append("  Agent Swarm  ", style="bold white on dark_blue")
        header_text.append(f"  run: {self.run_id}  ", style="dim")
        header_text.append(f"  elapsed: {self._elapsed()}  ", style="cyan")
        header_text.append(f"\n  {task_short}", style="italic")
        layout["header"].update(Panel(header_text, border_style="blue"))

        layout["signals"].update(Panel(self._signals_table(), title="Blackboard", border_style="blue"))
        layout["locks"].update(Panel(self._locks_table(), title="File Locks", border_style="yellow"))
        layout["log"].update(self._log_panel())
        return layout

    def run(self, stop: Event, refresh_per_second: int = 2):
        console = Console()
        with Live(self.render(), console=console, refresh_per_second=refresh_per_second, screen=True) as live:
            while not stop.is_set():
                live.update(self.render())
                time.sleep(1 / refresh_per_second)

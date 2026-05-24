import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Input, Label, RichLog, Static

from filelock import FileLockManager

_AGENT_PALETTE = [
    "cyan", "magenta", "green", "yellow",
    "blue", "red", "bright_cyan", "bright_magenta",
]
_agent_colors: dict[str, str] = {}
_color_idx = 0
_AGENT_LINE_RE = re.compile(r"^\[([a-z_]+-[0-9a-f]{8})\]")
_STATUS_COLOR = {"open": "yellow", "claimed": "cyan", "done": "green"}


def _get_agent_color(agent_id: str) -> str:
    global _color_idx
    if agent_id not in _agent_colors:
        _agent_colors[agent_id] = _AGENT_PALETTE[_color_idx % len(_AGENT_PALETTE)]
        _color_idx += 1
    return _agent_colors[agent_id]


class _SwarmApp(App):
    CSS = """
    Screen   { layout: vertical; background: #0a0a0a; }

    #header  { height: 4; padding: 0 2; }

    #log     {
        height: 1fr;
        min-height: 12;
        border: solid #2a2a2a;
    }

    #signals {
        height: 10;
        border: solid #003366;
    }
    #locks   { height: 5; border: solid #443300; }

    #footer  {
        layout: horizontal;
        height: 3;
        padding: 0 2;
        background: #001a00;
        border: solid #006600;
        display: none;
    }
    #footer Label  { width: auto; padding: 0 1 0 0; color: #00cc00; }
    #task-input    { width: 1fr; }
    """

    BINDINGS = [
        ("escape", "quit_app", "Выход"),
        ("ctrl+l", "copy_log_path", "Скопировать путь лога"),
    ]

    def __init__(self, db_path: str, task: str, run_id: str, log_path: str):
        super().__init__()
        self.db_path = db_path
        self._task = task
        self.run_id = run_id
        self.log_path = Path(log_path)
        self._start = datetime.now(timezone.utc)
        self._log_pos = 0
        self._complete = False

    def compose(self) -> ComposeResult:
        yield Static("", id="header")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True, max_lines=5000)
        yield DataTable(id="signals", zebra_stripes=True)
        yield DataTable(id="locks")
        with Horizontal(id="footer"):
            yield Label("Следующая задача:")
            yield Input(
                placeholder="опиши задачу (Enter — запустить, Esc — выход)",
                id="task-input",
            )

    def on_mount(self) -> None:
        sig = self.query_one("#signals", DataTable)
        sig.add_columns("Сигнал", "Статус", "От", "Время")
        lk = self.query_one("#locks", DataTable)
        lk.add_columns("Файл", "Агент")

        self.query_one("#log", RichLog).border_title = "Лог агентов  [dim](Ctrl+L — путь к файлу)[/]"
        self.query_one("#signals", DataTable).border_title = "Blackboard"
        self.query_one("#locks", DataTable).border_title = "File Locks"
        self.set_interval(0.5, self._tick)

    # ------------------------------------------------------------------ #

    def set_complete(self) -> None:
        self._complete = True
        self.query_one("#footer").display = True
        self.query_one("#task-input", Input).focus()

    def action_quit_app(self) -> None:
        self.exit(None)

    def action_copy_log_path(self) -> None:
        self.copy_to_clipboard(str(self.log_path))
        self.notify(f"Скопировано: {self.log_path}", severity="information")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.exit(event.value.strip() or None)

    # ------------------------------------------------------------------ #

    def _elapsed(self) -> str:
        s = int((datetime.now(timezone.utc) - self._start).total_seconds())
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _tick(self) -> None:
        try:
            self._upd_header()
            self._upd_signals()
            self._upd_locks()
            self._upd_log()
        except Exception:
            pass

    def _upd_header(self) -> None:
        task_s = self._task[:80] + "…" if len(self._task) > 80 else self._task
        done = "  [bold green]✓ ЗАВЕРШЕНО[/]" if self._complete else ""
        self.query_one("#header", Static).update(
            f"[bold cyan]Agent Swarm[/]  [dim]run:{self.run_id}[/]"
            f"  [cyan]{self._elapsed()}[/]{done}\n"
            f"[dim]{task_s}[/]\n"
            f"[dim]лог: {self.log_path}[/]"
        )

    def _upd_signals(self) -> None:
        dt = self.query_one("#signals", DataTable)
        dt.clear()
        try:
            conn = sqlite3.connect(self.db_path, timeout=3)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT type, status, from_role, created_at, claimed_by "
                "FROM signals ORDER BY created_at DESC LIMIT 60"
            ).fetchall()
            conn.close()
        except Exception:
            return
        for r in rows:
            color = _STATUS_COLOR.get(r["status"], "")
            ts = (r["created_at"] or "")[:19].replace("T", " ")
            from_label = r["claimed_by"] if r["status"] == "claimed" else r["from_role"]
            dt.add_row(r["type"], Text(r["status"], style=color), from_label or "-", ts)

    def _upd_locks(self) -> None:
        dt = self.query_one("#locks", DataTable)
        dt.clear()
        try:
            locks = FileLockManager(self.db_path).get_all_locks()
        except Exception:
            locks = {}
        for fp, agent in locks.items():
            dt.add_row(fp, Text(agent, style="cyan"))
        if not locks:
            dt.add_row(Text("(нет)", style="dim"), "")

    def _upd_log(self) -> None:
        log = self.query_one("#log", RichLog)
        try:
            raw_text = self.log_path.read_text(errors="replace")
        except OSError:
            return
        new = raw_text[self._log_pos:]
        self._log_pos = len(raw_text)
        for line in new.splitlines():
            if not line.strip():
                continue
            m = _AGENT_LINE_RE.match(line)
            if m:
                agent_id = m.group(1)
                color = _get_agent_color(agent_id)
                prefix = f"[{agent_id}]"
                rest = line[len(prefix):]
                t = Text()
                t.append(prefix, style=f"bold {color}")
                if "ERROR" in rest or "FATAL" in rest:
                    t.append(rest, style="red")
                elif "→" in rest:
                    t.append(rest, style=f"dim {color}")
                else:
                    t.append(rest, style=color)
                log.write(t)
            elif "=" * 10 in line:
                log.write(Text(line, style="bold green"))
            elif "ОТЧЁТ" in line:
                log.write(Text(line, style="bold green"))
            elif "ERROR" in line or "FATAL" in line:
                log.write(Text(line, style="bold red"))
            elif line.startswith("[swarm]"):
                log.write(Text(line, style="dim white"))
            else:
                log.write(Text(line, style="dim"))


class SwarmMonitor:
    def __init__(self, db_path: str, task: str, run_id: str, log_path: str):
        self._app = _SwarmApp(db_path, task, run_id, log_path)

    def set_complete(self) -> None:
        try:
            self._app.call_from_thread(self._app.set_complete)
        except Exception:
            self._app.set_complete()

    def run(self, stop: Event, **_) -> str | None:
        """Block until user exits TUI. Returns next task string or None."""
        def _watch() -> None:
            stop.wait()
            if not self._app._complete:
                try:
                    self._app.call_from_thread(self._app.exit, None)
                except Exception:
                    pass
        threading.Thread(target=_watch, daemon=True).start()
        return self._app.run()

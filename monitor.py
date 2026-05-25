import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Input, Label, RichLog, Static

from blackboard import Blackboard

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

    #report  {
        height: auto;
        max-height: 20;
        border: solid #336600;
        background: #0a1a00;
        padding: 0 1;
        display: none;
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
    #task-input    { width: 1fr; color: #00ff00; background: transparent; }
    """

    BINDINGS = [
        ("escape", "quit_app", "Quit"),
        ("ctrl+l", "copy_log_path", "Copy log path"),
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
        yield RichLog(id="report", highlight=False, markup=False, wrap=True, max_lines=500)
        yield DataTable(id="signals", zebra_stripes=True)
        yield DataTable(id="locks")
        with Horizontal(id="footer"):
            yield Label("Next task:")
            yield Input(
                placeholder="describe task (Enter — start, Esc — exit)",
                id="task-input",
            )

    def on_mount(self) -> None:
        sig = self.query_one("#signals", DataTable)
        sig.add_columns("Signal", "Status", "From", "Time")
        lk = self.query_one("#locks", DataTable)
        lk.add_columns("File", "Agent")

        self.query_one("#log", RichLog).border_title = "Agent log  [dim](Ctrl+L — copy path)[/]"
        self.query_one("#report", RichLog).border_title = "Report"
        self.query_one("#signals", DataTable).border_title = "Blackboard"
        self.query_one("#locks", DataTable).border_title = "File Locks"
        self.set_interval(0.5, self._tick)

    # ------------------------------------------------------------------ #

    def set_complete(self, report: str = "") -> None:
        self._complete = True
        if report:
            rlog = self.query_one("#report", RichLog)
            rlog.display = True
            for line in report.splitlines():
                rlog.write(Text(line, style="bright_green"))
        self.query_one("#footer").display = True
        self.query_one("#task-input", Input).focus()

    def action_quit_app(self) -> None:
        self.exit(None)

    def action_copy_log_path(self) -> None:
        self.copy_to_clipboard(str(self.log_path))
        self.notify(f"Copied: {self.log_path}", severity="information")

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
        done = "  [bold green]✓ DONE[/]" if self._complete else ""
        self.query_one("#header", Static).update(
            f"[bold magenta]♬[/] [bold cyan]Chorus Code[/]  [dim]run:{self.run_id}[/]"
            f"  [cyan]{self._elapsed()}[/]{done}\n"
            f"[dim]{task_s}[/]\n"
            f"[dim]log: {self.log_path}[/]"
        )

    def _upd_signals(self) -> None:
        dt = self.query_one("#signals", DataTable)
        dt.clear()
        try:
            rows = list(reversed(Blackboard(self.db_path).get_all_signals(limit=60)))
        except Exception:
            return
        for r in rows:
            color = _STATUS_COLOR.get(r["status"], "")
            ts = (r["at"] or "")[:19].replace("T", " ")
            from_label = r["claimed_by"] if r["status"] == "claimed" else r["from"]
            dt.add_row(r["type"], Text(r["status"], style=color), from_label or "-", ts)

    def _upd_locks(self) -> None:
        dt = self.query_one("#locks", DataTable)
        dt.clear()
        # Show active per-signal claim files (lock-free cooperative model)
        try:
            claims = {
                f.stem: f.read_text(errors="replace").strip()
                for f in (Path(self.db_path) / "signals").glob("*.claim")
            }
        except Exception:
            claims = {}
        for sig_id, agent in claims.items():
            dt.add_row(sig_id[:16], Text(agent, style="cyan"))
        if not claims:
            dt.add_row(Text("(none)", style="dim"), "")

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
            elif "REPORT" in line:
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

    def set_complete(self, report: str = "") -> None:
        try:
            self._app.call_from_thread(self._app.set_complete, report)
        except Exception:
            self._app.set_complete(report)

    def run(self, stop: Event, **_) -> str | None:
        """Block until user exits TUI. Returns next task string or None."""
        def _watch() -> None:
            stop.wait()
            if not self._app._complete:
                for _ in range(20):
                    try:
                        self._app.call_from_thread(self._app.exit, None)
                        return
                    except Exception:
                        time.sleep(0.2)
        threading.Thread(target=_watch, daemon=True).start()
        return self._app.run()

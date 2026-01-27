import asyncio
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import filedialog
from tkinter.scrolledtext import ScrolledText

from dev_team_core import DevTeamRunner, DevTeamConfig


BG = "#070A12"
PANEL = "#0B1020"
FG = "#00FF88"
ACCENT = "#FF00FF"
ACCENT2 = "#00D9FF"
WARN = "#FF3B3B"


class App(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title("DEV_TEAM // CYBER OPS")
        self.geometry("1000x800")
        self.configure(bg=BG)

        self.log_queue = queue.Queue()
        self.workspace = tk.StringVar(value=str(Path.cwd() / "workspace"))
        self.model = tk.StringVar(value="gpt-4o")
        self.use_docker = tk.BooleanVar(value=True)

        self.runner = None

        self._build_ui()
        self._tick_log_queue()

    def _build_ui(self):

        tk.Label(
            self,
            text="DEV_TEAM // AUTONOMOUS ENGINEERING",
            fg=ACCENT2,
            bg=BG,
            font=("Consolas", 14, "bold"),
        ).pack(pady=10)

        cfg = tk.Frame(self, bg=PANEL, highlightbackground=ACCENT2, highlightthickness=1)
        cfg.pack(fill="x", padx=15, pady=5)

        tk.Label(cfg, text="WORKSPACE:", fg=ACCENT2, bg=PANEL).grid(row=0, column=0)
        tk.Entry(cfg, textvariable=self.workspace, bg=BG, fg=FG, width=60).grid(row=0, column=1)
        tk.Button(cfg, text="SELECT", command=self._select_ws, bg=ACCENT).grid(row=0, column=2)

        tk.Label(cfg, text="MODEL:", fg=ACCENT2, bg=PANEL).grid(row=1, column=0)
        tk.Entry(cfg, textvariable=self.model, bg=BG, fg=FG).grid(row=1, column=1, sticky="w")
        tk.Checkbutton(cfg, text="USE DOCKER", variable=self.use_docker, bg=PANEL, fg=FG).grid(row=1, column=2)

        tk.Label(self, text="MISSION INPUT:", fg=ACCENT2, bg=BG).pack(anchor="w", padx=15)

        self.prompt = tk.Text(self, height=10, bg=BG, fg=FG, font=("Consolas", 10))
        self.prompt.pack(fill="x", padx=15)

        self.prompt.insert(
            "1.0",
            """OBJETIVO: Implementar PopScope.

AÇÃO OBRIGATÓRIA:
Use cat bash e escreva arquivos completos.
"""
        )

        btns = tk.Frame(self, bg=BG)
        btns.pack(pady=10)

        self.run_btn = tk.Button(btns, text="RUN", command=self._run, bg=ACCENT2)
        self.run_btn.pack(side="left", padx=5)

        self.stop_btn = tk.Button(btns, text="STOP", command=self._stop, bg=WARN, state="disabled")
        self.stop_btn.pack(side="left")

        self.console = ScrolledText(self, bg=BG, fg=FG, font=("Consolas", 9))
        self.console.pack(fill="both", expand=True, padx=15, pady=10)

    def _select_ws(self):
        f = filedialog.askdirectory()
        if f:
            self.workspace.set(f)

    def _tick_log_queue(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get()
            self.console.insert(tk.END, msg + "\n")
            self.console.see(tk.END)
        self.after(50, self._tick_log_queue)

    def _run(self):
        task = self.prompt.get("1.0", tk.END).strip()
        if not task:
            return

        ws = Path(self.workspace.get())
        ws.mkdir(parents=True, exist_ok=True)

        cfg = DevTeamConfig(
            model=self.model.get(),
            use_docker=self.use_docker.get(),
        )

        self.runner = DevTeamRunner(cfg, lambda m: self.log_queue.put(m))

        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        threading.Thread(target=lambda: self._exec(task, ws), daemon=True).start()

    def _exec(self, task, ws):
        try:
            asyncio.run(self.runner.run(task, ws))
        finally:
            self.run_btn.config(state="normal")
            self.stop_btn.config(state="disabled")

    def _stop(self):
        if self.runner:
            self.runner.stop()


if __name__ == "__main__":
    App().mainloop()

import asyncio
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
# Certifique-se de que esses arquivos existam no seu diretório
from gcal_sync import sync_ops_plan

from day_ops_core import DailyOpsRunner, DailyOpsConfig, TaskStore, TaskItem


# =========================================================
# Tema cyberpunk (KISS)
# =========================================================
THEME = {
    "bg": "#070A0F",
    "panel": "#0B1020",
    "panel2": "#0E1630",
    "text": "#B9FFEC",
    "muted": "#78C7B3",
    "neon": "#00FF9C",
    "pink": "#FF3DF2",
    "warn": "#FFB020",
    "err": "#FF4D4D",
    "font": ("Consolas", 10),
    "font_big": ("Consolas", 12, "bold"),
}


@dataclass
class UIState:
    vault_dir: Path
    running: bool = False


class DailyOpsUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("OPS_AGENT // Daily Control Panel")
        self.root.geometry("1200x720")
        self.root.configure(bg=THEME["bg"])

        self.state = UIState(vault_dir=Path.home() / ".ops_agent")
        self.store = TaskStore(self.state.vault_dir)
        self.tasks = self.store.load_today()

        self.runner = DailyOpsRunner(DailyOpsConfig(model="gpt-5-mini"))

        self.ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._build_layout()
        self._refresh_task_list()
        self._ui_pump()

        self.last_agent_output = ""
        self.tasks = self.store.load_today()


    # ---------------- UI Layout ----------------
    def _build_layout(self) -> None:
        top = tk.Frame(self.root, bg=THEME["bg"])
        top.pack(fill="x", padx=10, pady=10)

        title = tk.Label(
            top, text="OPS_AGENT // Cyberpunk Daily Ops", fg=THEME["neon"], bg=THEME["bg"], font=THEME["font_big"]
        )
        title.pack(side="left")

        self.vault_label = tk.Label(
            top, text=f"Vault: {self.state.vault_dir}", fg=THEME["muted"], bg=THEME["bg"], font=THEME["font"]
        )
        self.vault_label.pack(side="left", padx=15)

        self.sync_btn = tk.Button(
            top,
            text="Sync → GCal",
            command=self._sync_gcal,
            bg=THEME["bg"],
            fg=THEME["text"],
            activebackground=THEME["neon"],
            activeforeground=THEME["bg"],
            relief="flat",
            font=THEME["font"],
        )
        self.sync_btn.pack(side="right", padx=(0, 8))
                
        tk.Button(
            top,
            text="Selecionar Vault",
            command=self._select_vault,
            bg=THEME["panel2"],
            fg=THEME["text"],
            activebackground=THEME["pink"],
            activeforeground=THEME["bg"],
            relief="flat",
            font=THEME["font"],
        ).pack(side="right")

        main = tk.Frame(self.root, bg=THEME["bg"])
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # Left panel: tasks
        left = tk.Frame(main, bg=THEME["panel"], bd=1, highlightthickness=1, highlightbackground=THEME["pink"])
        left.pack(side="left", fill="y", padx=(0, 10))

        tk.Label(left, text="TASKS // TODAY", fg=THEME["pink"], bg=THEME["panel"], font=THEME["font_big"]).pack(
            anchor="w", padx=10, pady=(10, 6)
        )

        self.task_list = tk.Listbox(
            left,
            width=52,
            height=28,
            bg=THEME["panel2"],
            fg=THEME["text"],
            selectbackground=THEME["pink"],
            selectforeground=THEME["bg"],
            font=THEME["font"],
            relief="flat",
            highlightthickness=0,
        )
        self.task_list.pack(padx=10, pady=8)

        btns = tk.Frame(left, bg=THEME["panel"])
        btns.pack(fill="x", padx=10, pady=(0, 10))

        self._btn(btns, "Add", self._add_task).pack(side="left")
        self._btn(btns, "Edit", self._edit_task).pack(side="left", padx=6)
        self._btn(btns, "Done", self._mark_done).pack(side="left", padx=6)
        self._btn(btns, "Delete", self._delete_task).pack(side="left", padx=6)

        # Right panel: chat
        right = tk.Frame(main, bg=THEME["panel"], bd=1, highlightthickness=1, highlightbackground=THEME["neon"])
        right.pack(side="right", fill="both", expand=True)

        tk.Label(right, text="CHAT // OPS STREAM", fg=THEME["neon"], bg=THEME["panel"], font=THEME["font_big"]).pack(
            anchor="w", padx=10, pady=(10, 6)
        )

        self.chat = tk.Text(
            right,
            bg=THEME["panel2"],
            fg=THEME["text"],
            insertbackground=THEME["neon"],
            font=THEME["font"],
            wrap="word",
            relief="flat",
            highlightthickness=0,
        )
        self.chat.pack(fill="both", expand=True, padx=10, pady=8)
        self.chat.configure(state="disabled")

        quick = tk.Frame(right, bg=THEME["panel"])
        quick.pack(fill="x", padx=10, pady=(0, 6))

        self._btn(quick, "Planejar meu dia", lambda: self._send("Planeje meu dia com base nas tarefas.")).pack(
            side="left"
        )
        self._btn(quick, "Próxima ação", lambda: self._send("Qual a próxima ação mais inteligente agora?")).pack(
            side="left", padx=6
        )
        self._btn(quick, "Reduzir escopo", lambda: self._send("Estou sobrecarregado. Reduza para o mínimo viável."))\
            .pack(side="left", padx=6)

        bottom = tk.Frame(right, bg=THEME["panel"])
        bottom.pack(fill="x", padx=10, pady=(0, 10))

        self.entry = tk.Entry(
            bottom,
            bg=THEME["bg"],
            fg=THEME["text"],
            insertbackground=THEME["neon"],
            font=THEME["font_big"],
            relief="flat",
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.entry.bind("<Return>", lambda e: self._send(self.entry.get()))

        self.send_btn = tk.Button(
            bottom,
            text="SEND",
            command=lambda: self._send(self.entry.get()),
            bg=THEME["pink"],
            fg=THEME["bg"],
            activebackground=THEME["neon"],
            activeforeground=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
            width=10,
        )
        self.send_btn.pack(side="right")

        self._log("SYSTEM", "OPS_AGENT online. Adicione tarefas e mande mensagens.")

    def _btn(self, parent, label, cmd):
        return tk.Button(
            parent,
            text=label,
            command=cmd,
            bg=THEME["bg"],
            fg=THEME["text"],
            activebackground=THEME["pink"],
            activeforeground=THEME["bg"],
            relief="flat",
            font=THEME["font"],
            padx=10,
            pady=4,
        )

    # ---------------- Logging ----------------
    def _append_chat(self, who: str, text: str, color: str) -> None:
        self.chat.configure(state="normal")
        self.chat.insert("end", f"\n[{who}] ", ("who",))
        self.chat.insert("end", text, ("msg",))
        self.chat.see("end")
        self.chat.configure(state="disabled")
        self.chat.tag_config("who", foreground=color, font=THEME["font_big"])
        self.chat.tag_config("msg", foreground=THEME["text"], font=THEME["font"])

    def _log(self, who: str, msg: str) -> None:
        color = THEME["pink"] if who == "YOU" else THEME["neon"]
        if who == "SYSTEM":
            color = THEME["warn"]
        self._append_chat(who, msg, color)

    # ---------------- Vault ----------------
    def _select_vault(self) -> None:
        folder = filedialog.askdirectory()
        if not folder:
            return
        self.state.vault_dir = Path(folder)
        self.store = TaskStore(self.state.vault_dir)
        self.tasks = self.store.load_today()
        self.vault_label.config(text=f"Vault: {self.state.vault_dir}")
        self._refresh_task_list()
        self._log("SYSTEM", f"Vault alterado para: {self.state.vault_dir}")

    # ---------------- Tasks ----------------
    def _refresh_task_list(self) -> None:
        self.task_list.delete(0, "end")
        for t in self.tasks:
            icon = "✅" if t.status == "DONE" else ("▶" if t.status == "DOING" else "•")
            self.task_list.insert("end", f"{icon} [{t.priority}] {t.title}")

    def _selected_task_index(self) -> int:
        sel = self.task_list.curselection()
        return int(sel[0]) if sel else -1

    def _add_task(self) -> None:
        self._task_editor(title="Nova tarefa")

    def _edit_task(self) -> None:
        idx = self._selected_task_index()
        if idx < 0:
            messagebox.showinfo("Ops", "Selecione uma tarefa para editar.")
            return
        self._task_editor(title="Editar tarefa", task=self.tasks[idx], index=idx)

    def _delete_task(self) -> None:
        idx = self._selected_task_index()
        if idx < 0:
            return
        self.tasks.pop(idx)
        self.store.save_today(self.tasks)
        self._refresh_task_list()

    def _mark_done(self) -> None:
        idx = self._selected_task_index()
        if idx < 0:
            return
        self.tasks[idx].status = "DONE"
        self.store.save_today(self.tasks)
        self._refresh_task_list()

    def _task_editor(self, title: str, task: TaskItem | None = None, index: int | None = None) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=THEME["panel"])
        win.geometry("520x280")

        tk.Label(win, text="Título", fg=THEME["text"], bg=THEME["panel"], font=THEME["font"]).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        e_title = tk.Entry(win, bg=THEME["bg"], fg=THEME["text"], insertbackground=THEME["neon"], font=THEME["font"])
        e_title.pack(fill="x", padx=12)
        if task:
            e_title.insert(0, task.title)

        tk.Label(win, text="Notas (opcional)", fg=THEME["text"], bg=THEME["panel"], font=THEME["font"]).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        e_notes = tk.Entry(win, bg=THEME["bg"], fg=THEME["text"], insertbackground=THEME["neon"], font=THEME["font"])
        e_notes.pack(fill="x", padx=12)
        if task:
            e_notes.insert(0, task.notes)

        tk.Label(win, text="Prioridade (P1/P2/P3)", fg=THEME["text"], bg=THEME["panel"], font=THEME["font"]).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        e_prio = tk.Entry(win, bg=THEME["bg"], fg=THEME["text"], insertbackground=THEME["neon"], font=THEME["font"])
        e_prio.pack(fill="x", padx=12)
        e_prio.insert(0, (task.priority if task else "P2"))

        def save():
            title_val = e_title.get().strip()
            if not title_val:
                messagebox.showerror("Erro", "Título não pode ficar vazio.")
                return
            notes_val = e_notes.get().strip()
            prio_val = e_prio.get().strip().upper() or "P2"
            if prio_val not in ("P1", "P2", "P3"):
                prio_val = "P2"

            if task and index is not None:
                task.title = title_val
                task.notes = notes_val
                task.priority = prio_val
                self.tasks[index] = task
            else:
                self.tasks.append(TaskItem.create(title_val, notes_val, prio_val))

            self.store.save_today(self.tasks)
            self._refresh_task_list()
            win.destroy()

        tk.Button(
            win,
            text="SALVAR",
            command=save,
            bg=THEME["pink"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
        ).pack(pady=16)

    # ---------------- Chat / Streaming ----------------
    def _send(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return

        self.entry.delete(0, "end")
        self._log("YOU", text)

        self.send_btn.config(state="disabled")
        self.entry.config(state="disabled")

        def worker():
            asyncio.run(self._ask_agent(text))

        threading.Thread(target=worker, daemon=True).start()

    async def _ask_agent(self, text: str) -> None:
        def on_chunk(chunk: str):
            self.ui_queue.put(("chunk", chunk))

        def on_final(full: str):
            self.ui_queue.put(("final", full))

        self.ui_queue.put(("begin", ""))

        await self.runner.ask_stream(
            user_message=text,
            tasks=self.tasks,
            on_chunk=on_chunk,
            on_final=on_final,
        )

    def _ui_pump(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "begin":
                    self._append_chat("OPS", "", THEME["neon"])
                elif kind == "chunk":
                    self.chat.configure(state="normal")
                    self.chat.insert("end", payload)
                    self.chat.see("end")
                    self.chat.configure(state="disabled")
                elif kind == "final":
                    self.last_agent_output = payload
                    self.send_btn.config(state="normal")
                    self.entry.config(state="normal")
                    self.entry.focus_set()
        except queue.Empty:
            pass
        self.root.after(30, self._ui_pump)

    def _sync_gcal(self) -> None:
        self.sync_btn.config(state="disabled")
        self._log("SYSTEM", "Sincronizando PLANO do agente com Google Calendar...")

        if not self.last_agent_output.strip():
            self._log("SYSTEM", "Nenhum plano encontrado ainda. Gere um plano primeiro.")
            self.sync_btn.config(state="normal")
            return

        def worker():
            try:
                result = sync_ops_plan(
                    raw_text=self.last_agent_output,
                    vault_dir=self.state.vault_dir,
                )

                def on_ok():
                    self._log("SYSTEM", "Plano sincronizado com Google Calendar.")
                    self.sync_btn.config(state="normal")

                self.root.after(0, on_ok)

            except Exception as e:
                err = str(e)

                def on_err(err=err):
                    self._log("SYSTEM", f"Erro no sync GCal: {err}")
                    messagebox.showerror("Google Calendar", err)
                    self.sync_btn.config(state="normal")

                self.root.after(0, on_err)

        threading.Thread(target=worker, daemon=True).start()



def main():
    root = tk.Tk()
    app = DailyOpsUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
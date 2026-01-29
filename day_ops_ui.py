import asyncio
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
# Certifique-se de que esses arquivos existam no seu diretório
from gcal_sync import sync_ops_plan

from day_ops_core import (
    DailyOpsRunner,
    DailyOpsConfig,
    DatabaseManager,
    TaskStore,
    TaskItem,
    DistractionStore,
    ChatStore,
    check_identity_overload,
)


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

        # 1. Localização fixa (Home do Usuário)
        self.vault_path = Path.home() / ".ops_agent"
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.state = UIState(vault_dir=self.vault_path)

        # 2. Inicialização na ordem correta
        self.db_manager = DatabaseManager(self.vault_path)
        self.store = TaskStore(self.db_manager)
        self.distraction_store = DistractionStore(self.db_manager)
        self.chat_store = ChatStore(self.db_manager)

        # 3. Carregar tarefas e inicializar UI
        chat_history = self.chat_store.load()
        self.tasks = self.store.load_today()

        # Runner recebe histórico para manter "memória" no dia
        self.runner = DailyOpsRunner(DailyOpsConfig(model="gpt-4o-mini"), history=chat_history)

        self.ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._build_layout()

        self._refresh_task_list()
        self._load_chat_history_to_ui(chat_history)

        self._ui_pump()
        self.last_agent_output = ""


    # ---------------- UI Layout ----------------
    def _build_layout(self) -> None:
        top = tk.Frame(self.root, bg=THEME["bg"])
        top.pack(fill="x", padx=10, pady=10)

        title = tk.Label(
            top, text="OPS_AGENT // Cyberpunk Daily Ops", fg=THEME["neon"], bg=THEME["bg"], font=THEME["font_big"]
        )
        title.pack(side="left")

        self.vault_label = tk.Label(
            top, text=f"Vault: {self.vault_path}", fg=THEME["muted"], bg=THEME["bg"], font=THEME["font"]
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
        left.pack(side="left", fill="both", padx=(0, 10))

        tk.Label(left, text="TASKS // SELEÇÃO ATIVA", fg=THEME["pink"], bg=THEME["panel"], font=THEME["font_big"]).pack(
            anchor="w", padx=10, pady=(10, 6)
        )

        # --- ÁREA DE LISTA ROLÁVEL COM CHECKBOXES ---
        container = tk.Frame(left, bg=THEME["panel2"])
        container.pack(fill="both", expand=True, padx=10, pady=8)

        self.canvas = tk.Canvas(container, bg=THEME["panel2"], highlightthickness=0)
        self.scrollbar = tk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=THEME["panel2"])

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # --- QUICK ADD SECTION ---
        quick_add_frame = tk.Frame(left, bg=THEME["panel2"], pady=5)
        quick_add_frame.pack(fill="x", padx=10, pady=(0, 10))

        # 1. Título
        self.quick_entry = tk.Entry(
            quick_add_frame,
            bg=THEME["bg"],
            fg=THEME["text"],
            insertbackground=THEME["neon"],
            font=THEME["font"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["panel"],
            width=20,
        )
        self.quick_entry.pack(side="left", padx=(5, 2))
        self.quick_entry.insert(0, "Título...")
        self.quick_entry.bind(
            "<FocusIn>",
            lambda e: self.quick_entry.delete(0, "end") if self.quick_entry.get() == "Título..." else None,
        )
        self.quick_entry.bind("<Return>", lambda e: self._quick_add())

        # 2. Notas (Nova)
        self.quick_notes_entry = tk.Entry(
            quick_add_frame,
            bg=THEME["bg"],
            fg=THEME["muted"],
            insertbackground=THEME["neon"],
            font=THEME["font"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["panel"],
            width=15,
        )
        self.quick_notes_entry.pack(side="left", padx=2)
        self.quick_notes_entry.insert(0, "Notas...")
        self.quick_notes_entry.bind(
            "<FocusIn>",
            lambda e: self.quick_notes_entry.delete(0, "end")
            if self.quick_notes_entry.get() == "Notas..."
            else None,
        )
        self.quick_notes_entry.bind("<Return>", lambda e: self._quick_add())

        # 3. Quadrante
        self.quick_quadrant_var = tk.StringVar(value="Q2")
        quad_opt = tk.OptionMenu(quick_add_frame, self.quick_quadrant_var, "Q1", "Q2", "Q3", "Q4")
        quad_opt.config(
            bg=THEME["bg"],
            fg=THEME["pink"],
            activebackground=THEME["pink"],
            relief="flat",
            font=("Consolas", 8),
            highlightthickness=0,
            width=3,
        )
        quad_opt["menu"].config(bg=THEME["panel"], fg=THEME["text"])
        quad_opt.pack(side="left", padx=2)

        # 4. Período (Adicionando FLEXÍVEL)
        self.quick_period_var = tk.StringVar(value="FLEXÍVEL")
        period_opt = tk.OptionMenu(quick_add_frame, self.quick_period_var, "FLEXÍVEL", "MANHÃ", "TARDE", "NOITE")
        period_opt.config(
            bg=THEME["bg"],
            fg=THEME["neon"],
            activebackground=THEME["neon"],
            relief="flat",
            font=("Consolas", 8),
            highlightthickness=0,
            width=6,
        )
        period_opt["menu"].config(bg=THEME["panel"], fg=THEME["text"])
        period_opt.pack(side="left", padx=2)

        # 5. Recorrência (Nova - Checkbutton)
        self.quick_recurring_var = tk.BooleanVar(value=False)
        self.quick_recurring_cb = tk.Checkbutton(
            quick_add_frame,
            text="Rec",
            variable=self.quick_recurring_var,
            bg=THEME["panel2"],
            fg=THEME["muted"],
            selectcolor=THEME["bg"],
            activebackground=THEME["panel2"],
            font=("Consolas", 8),
            relief="flat",
        )
        self.quick_recurring_cb.pack(side="left", padx=2)

        # 6. Botão +
        tk.Button(
            quick_add_frame,
            text="+",
            command=self._quick_add,
            bg=THEME["pink"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
            padx=10,
        ).pack(side="right", padx=5)

        btns = tk.Frame(left, bg=THEME["panel"])
        btns.pack(fill="x", padx=10, pady=(0, 10))

        self._btn(btns, "Add", self._add_task).pack(side="left")
        self._btn(btns, "Edit", self._edit_task).pack(side="left", padx=6)
        self._btn(btns, "Done", self._mark_done).pack(side="left", padx=6)
        self._btn(btns, "Delete", self._delete_task).pack(side="left", padx=6)
        self._btn(btns, "Dominó!", self._capture_distraction).pack(side="left", padx=6)

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
        self._btn(quick, "Desligamento", self._shut_down_ritual).pack(side="left", padx=6)

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

    def _load_chat_history_to_ui(self, history) -> None:
        """Preenche a caixa de texto com as conversas anteriores."""
        for msg in history:
            role = msg.get("role")
            content = msg.get("content", "")
            who = "YOU" if role == "user" else "OPS"
            color = THEME["pink"] if who == "YOU" else THEME["neon"]
            self._append_chat(who, content, color)

    def _reload_chat_ui(self, history) -> None:
        """Limpa o chat e recarrega o histórico (ex.: ao trocar de vault)."""
        self.chat.configure(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.configure(state="disabled")
        self._load_chat_history_to_ui(history)

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
        self.vault_path = Path(folder)
        self.vault_path.mkdir(parents=True, exist_ok=True)
        self.state.vault_dir = self.vault_path
        self.db_manager = DatabaseManager(self.vault_path)
        self.store = TaskStore(self.db_manager)
        self.distraction_store = DistractionStore(self.db_manager)
        self.chat_store = ChatStore(self.db_manager)
        self.tasks = self.store.load_today()
        chat_history = self.chat_store.load()
        self.runner.history = chat_history
        self.vault_label.config(text=f"Vault: {self.vault_path}")
        self._refresh_task_list()
        self._reload_chat_ui(chat_history)
        self._log("SYSTEM", f"Vault alterado para: {self.vault_path}")

    # ---------------- Tasks ----------------
    def _refresh_task_list(self) -> None:
        # Limpa o frame
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        # Ordenar tarefas (Q1 primeiro, depois Q2, etc)
        quad_map = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
        sorted_tasks = sorted(
            self.tasks, key=lambda x: (x.status == "DONE", quad_map.get(x.quadrant, 9), x.created_at)
        )

        for task in sorted_tasks:
            task_frame = tk.Frame(self.scrollable_frame, bg=THEME["panel2"], pady=2)
            task_frame.pack(fill="x", expand=True, padx=5)

            # --- TOGGLE CUSTOMIZADO (SUBSTITUI O CHECKBUTTON) ---
            # Mostra [X] se ativo e [ ] se inativo
            toggle_text = "[X]" if getattr(task, "active", True) else "[ ]"
            toggle_color = THEME["neon"] if getattr(task, "active", True) else THEME["muted"]

            btn_toggle = tk.Button(
                task_frame,
                text=toggle_text,
                fg=toggle_color,
                bg=THEME["panel2"],
                font=("Consolas", 10, "bold"),
                relief="flat",
                activebackground=THEME["panel2"],
                activeforeground=THEME["pink"],
                width=3,
                command=lambda t=task: self._toggle_active_custom(t),
            )
            btn_toggle.pack(side="left")

            # --- RESTO DA LINHA (DONE, TEXTO, DELETE) ---
            # Botão Done
            done_color = THEME["neon"] if task.status == "DONE" else THEME["muted"]
            btn_done = tk.Button(
                task_frame,
                text="✔",
                font=("Consolas", 8, "bold"),
                bg=THEME["bg"],
                fg=done_color,
                relief="flat",
                padx=5,
                command=lambda t=task: self._mark_specific_done(t),
            )
            btn_done.pack(side="left", padx=2)

            # Texto
            icon_rec = " 🔄" if getattr(task, "is_recurring", False) else ""
            color = THEME["muted"] if task.status == "DONE" else THEME["text"]
            period_raw = getattr(task, "period", "FLEXÍVEL")
            period_short = "FLX" if period_raw == "FLEXÍVEL" else period_raw[:3]
            lbl_text = f"[{task.quadrant}] ({period_short}) {task.title}{icon_rec}"

            lbl = tk.Label(
                task_frame, text=lbl_text, fg=color, bg=THEME["panel2"], font=THEME["font"], anchor="w", cursor="hand2"
            )
            lbl.pack(side="left", fill="x", expand=True, padx=5)
            lbl.bind("<Button-1>", lambda e, t=task: self._edit_specific_task(t))

            # Botão Delete
            btn_del = tk.Button(
                task_frame,
                text="✖",
                font=("Consolas", 8),
                bg=THEME["bg"],
                fg=THEME["err"],
                relief="flat",
                padx=5,
                command=lambda t=task: self._delete_specific_task(t),
            )
            btn_del.pack(side="right", padx=2)

        # Atualiza scrollregion após adicionar widgets
        self.scrollable_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        warning = check_identity_overload(self.tasks)
        if warning:
            self._log("SYSTEM", warning)

    def _edit_specific_task(self, task: TaskItem) -> None:
        """Edita uma tarefa específica (chamado ao clicar no texto da tarefa)."""
        idx = self.tasks.index(task)
        self._task_editor(title="Editar tarefa", task=task, index=idx)

    def _toggle_active_custom(self, task: TaskItem) -> None:
        """Inverte o estado ativo da tarefa e atualiza a UI."""
        task.active = not getattr(task, "active", True)
        # Salva no banco
        self.store.save_today(self.tasks)
        # Log de debug
        print(f"[*] Tarefa '{task.title}' -> Active: {task.active}")
        # Recarrega a lista para mostrar [X] ou [ ]
        self._refresh_task_list()

    def _mark_specific_done(self, task: TaskItem) -> None:
        """Alterna entre TODO e DONE direto na linha."""
        task.status = "DONE" if task.status != "DONE" else "TODO"
        self.store.save_today(self.tasks)
        self._refresh_task_list()

    def _delete_specific_task(self, task: TaskItem) -> None:
        """Deleta a tarefa imediatamente após confirmação rápida."""
        if messagebox.askyesno("Confirmar", f"Deletar tarefa: {task.title}?"):
            self.tasks.remove(task)
            self.store.save_today(self.tasks)
            self._refresh_task_list()

    def _add_task(self) -> None:
        self._task_editor(title="Nova tarefa")

    def _edit_task(self) -> None:
        # Com checkboxes, não há seleção de lista. Mostra mensagem informativa.
        messagebox.showinfo("Ops", "Clique no texto da tarefa para editá-la.")

    def _delete_task(self) -> None:
        # Com checkboxes, não há seleção de lista. Mostra mensagem informativa.
        messagebox.showinfo("Ops", "Clique no texto da tarefa para editá-la e depois delete.")

    def _mark_done(self) -> None:
        # Com checkboxes, não há seleção de lista. Mostra mensagem informativa.
        messagebox.showinfo("Ops", "Clique no texto da tarefa para editá-la e depois marque como DONE.")

    def _quick_add(self, event=None) -> None:
        """Adição rápida de tarefas capturando Notas e Recorrência."""
        title = self.quick_entry.get().strip()
        # Evita salvar se o título estiver vazio ou for o placeholder
        if not title or title == "Título...":
            return

        notes = self.quick_notes_entry.get().strip()
        if notes == "Notas...":
            notes = ""

        quad = self.quick_quadrant_var.get() if hasattr(self, "quick_quadrant_var") else "Q2"
        period = self.quick_period_var.get() if hasattr(self, "quick_period_var") else "FLEXÍVEL"
        is_rec = self.quick_recurring_var.get() if hasattr(self, "quick_recurring_var") else False

        # Criar e Salvar
        new_task = TaskItem.create(title=title, notes=notes, quadrant=quad, period=period, is_recurring=is_rec)

        self.tasks.append(new_task)
        self.store.save_today(self.tasks)
        self._refresh_task_list()

        # Limpar campos para a próxima entrada
        self.quick_entry.delete(0, "end")
        self.quick_entry.insert(0, "Título...")
        self.quick_notes_entry.delete(0, "end")
        self.quick_notes_entry.insert(0, "Notas...")
        self.quick_recurring_var.set(False)
        self.quick_entry.focus_set()

    # --- Dominó Mental: captura de distrações ---
    def _capture_distraction(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Dominó Mental // Capturar Distração")
        win.configure(bg=THEME["panel"])
        win.geometry("420x170")

        tk.Label(
            win,
            text="O que quer tirar da cabeça agora?",
            fg=THEME["neon"],
            bg=THEME["panel"],
            font=THEME["font_big"],
        ).pack(pady=(14, 6))

        entry = tk.Entry(
            win,
            bg=THEME["bg"],
            fg=THEME["text"],
            insertbackground=THEME["neon"],
            font=THEME["font"],
        )
        entry.pack(fill="x", padx=20)
        entry.focus_set()

        def save() -> None:
            txt = entry.get().strip()
            if txt:
                self.distraction_store.add(txt)
                self._log("SYSTEM", f"Distração anotada: '{txt}'. Volte ao foco!")
            win.destroy()

        tk.Button(
            win,
            text="ANOTAR E VOLTAR",
            command=save,
            bg=THEME["pink"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
        ).pack(pady=16)

    # --- Ritual de Desligamento: processar distrações do dia ---
    def _shut_down_ritual(self) -> None:
        items = self.distraction_store.load()
        if not items:
            self._log(
                "OPS",
                "Ritual de Desligamento iniciado. Nenhuma distração capturada. O dia está limpo! Desligamento concluído.",
            )
            return

        self._log(
            "OPS",
            f"Ritual de Desligamento: você capturou {len(items)} distrações hoje. Vamos processá-las agora.",
        )

        win = tk.Toplevel(self.root)
        win.title("Ritual de Desligamento")
        win.configure(bg=THEME["panel"])
        win.geometry("520x420")

        txt_area = tk.Text(
            win,
            bg=THEME["panel2"],
            fg=THEME["text"],
            font=THEME["font"],
            wrap="word",
            relief="flat",
        )
        txt_area.pack(fill="both", expand=True, padx=10, pady=10)

        for item in items:
            txt_area.insert("end", f"• {item}\n")

        def finish() -> None:
            self.distraction_store.clear()
            self._log(
                "SYSTEM",
                "Lista de distrações limpa. Agora, foco total na recarga. Desligamento concluído.",
            )
            win.destroy()

        tk.Button(
            win,
            text="LIMPAR TUDO E ENCERRAR DIA",
            command=finish,
            bg=THEME["neon"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
        ).pack(pady=10)

    def _task_editor(self, title: str, task: TaskItem | None = None, index: int | None = None) -> None:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=THEME["panel"])

        # --- LÓGICA DE CENTRALIZAÇÃO ---
        win.withdraw()  # Esconde a janela enquanto calcula a posição
        win.update_idletasks()

        # Tamanho da janela de edição
        w, h = 520, 420

        # Posição e tamanho da janela principal
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()

        # Calcula o centro
        pos_x = root_x + (root_w // 2) - (w // 2)
        pos_y = root_y + (root_h // 2) - (h // 2)

        win.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        win.transient(self.root)  # Define como dependente da principal
        win.grab_set()            # Bloqueia interação com a principal até fechar
        win.deiconify()           # Mostra a janela centralizada

        # --- CAMPOS DO EDITOR ---
        def add_field(label: str, default_val: str = "") -> tk.Entry:
            tk.Label(
                win,
                text=label,
                fg=THEME["text"],
                bg=THEME["panel"],
                font=THEME["font"],
            ).pack(anchor="w", padx=20, pady=(10, 2))
            entry = tk.Entry(
                win,
                bg=THEME["bg"],
                fg=THEME["text"],
                insertbackground=THEME["neon"],
                font=THEME["font"],
                relief="flat",
            )
            entry.pack(fill="x", padx=20)
            entry.insert(0, default_val)
            return entry

        e_title = add_field("Título", task.title if task else "")
        e_notes = add_field("Notas (opcional)", task.notes if task else "")

        # Seletores para Quadrante e Período
        tk.Label(
            win,
            text="Quadrante e Período",
            fg=THEME["text"],
            bg=THEME["panel"],
            font=THEME["font"],
        ).pack(anchor="w", padx=20, pady=(10, 2))

        row = tk.Frame(win, bg=THEME["panel"])
        row.pack(fill="x", padx=20)

        q_var = tk.StringVar(value=(task.quadrant if task else "Q2"))
        tk.OptionMenu(row, q_var, "Q1", "Q2", "Q3", "Q4").pack(
            side="left", expand=True, fill="x"
        )

        p_var = tk.StringVar(value=(task.period if task else "FLEXÍVEL"))
        tk.OptionMenu(row, p_var, "FLEXÍVEL", "MANHÃ", "TARDE", "NOITE").pack(
            side="left", expand=True, fill="x", padx=(10, 0)
        )

        # Checkbox de Recorrente
        r_var = tk.BooleanVar(value=(getattr(task, "is_recurring", False) if task else False))
        tk.Checkbutton(
            win,
            text="Tarefa Recorrente (Não some ao completar)",
            variable=r_var,
            bg=THEME["panel"],
            fg=THEME["neon"],
            selectcolor=THEME["bg"],
            activebackground=THEME["panel"],
            font=THEME["font"],
        ).pack(anchor="w", padx=20, pady=10)

        def save() -> None:
            t_val = e_title.get().strip()
            if not t_val:
                return

            if task:
                # Editando existente
                task.title = t_val
                task.notes = e_notes.get().strip()
                task.quadrant = q_var.get()
                task.period = p_var.get()
                task.is_recurring = r_var.get()
            else:
                # Nova tarefa
                self.tasks.append(
                    TaskItem.create(
                        title=t_val,
                        notes=e_notes.get().strip(),
                        quadrant=q_var.get(),
                        period=p_var.get(),
                        is_recurring=r_var.get(),
                    )
                )

            self.store.save_today(self.tasks)
            self._refresh_task_list()
            win.destroy()

        tk.Button(
            win,
            text="SALVAR ALTERAÇÕES",
            command=save,
            bg=THEME["pink"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
        ).pack(pady=30, fill="x", padx=20)

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
            # Salva histórico assim que o agente terminar de responder
            self.chat_store.save(self.runner.history)
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
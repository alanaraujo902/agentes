import asyncio
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox

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

        # 1. Configuração do Vault
        self.state = UIState(vault_dir=Path.home() / ".ops_agent")
        self.state.vault_dir.mkdir(parents=True, exist_ok=True)

        # 2. Inicialização do Banco e Stores
        self.db_manager = DatabaseManager(self.state.vault_dir)
        self.store = TaskStore(self.db_manager)
        self.distraction_store = DistractionStore(self.db_manager)
        self.chat_store = ChatStore(self.db_manager)

        # 3. Carregar Estado
        chat_history = self.chat_store.load()
        self.tasks = self.store.load_today()

        # Recupera o último plano do histórico para permitir sync imediato
        self.last_agent_output = ""
        for msg in reversed(chat_history):
            if msg.get("role") == "assistant":
                self.last_agent_output = msg.get("content", "")
                break

        # 4. Runner da IA
        self.runner = DailyOpsRunner(DailyOpsConfig(model="gpt-4o-mini"), history=chat_history)

        self.ui_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.selected_task: TaskItem | None = None
        self._build_layout()

        self._refresh_task_list()
        self._load_chat_history_to_ui(chat_history)
        self._ui_pump()


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

        # Container para a lista com scroll
        self.canvas = tk.Canvas(left, bg=THEME["panel2"], highlightthickness=0)
        self.scrollbar = tk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.task_inner_frame = tk.Frame(self.canvas, bg=THEME["panel2"])

        self.task_inner_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        canvas_window = self.canvas.create_window((0, 0), window=self.task_inner_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # Ajusta a largura do frame interno quando o canvas é redimensionado
        def configure_canvas_width(event):
            canvas_width = event.width
            self.canvas.itemconfig(canvas_window, width=canvas_width)
        self.canvas.bind('<Configure>', configure_canvas_width)

        # Bind mouse wheel scrolling (Windows e Linux)
        def _on_mousewheel(event):
            if event.delta:
                # Windows
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            else:
                # Linux
                if event.num == 4:
                    self.canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    self.canvas.yview_scroll(1, "units")
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)
        self.canvas.bind_all("<Button-4>", _on_mousewheel)
        self.canvas.bind_all("<Button-5>", _on_mousewheel)

        self.canvas.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
        self.scrollbar.pack(side="right", fill="y", pady=8, padx=(0, 10))

        # --- QUICK ADD SECTION ---
        quick_add_container = tk.Frame(left, bg=THEME["panel2"], pady=5)
        quick_add_container.pack(fill="x", padx=10, pady=(0, 10))

        # Linha 1: Título e Botão
        row1 = tk.Frame(quick_add_container, bg=THEME["panel2"])
        row1.pack(fill="x", padx=5)

        self.quick_entry = tk.Entry(
            row1,
            bg=THEME["bg"],
            fg=THEME["text"],
            insertbackground=THEME["neon"],
            font=THEME["font"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["panel"],
        )
        self.quick_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))
        self.quick_entry.bind("<Return>", lambda e: self._quick_add())

        tk.Button(
            row1,
            text="+",
            command=self._quick_add,
            bg=THEME["pink"],
            fg=THEME["bg"],
            relief="flat",
            font=THEME["font_big"],
            padx=12,
        ).pack(side="right", padx=2)

        # Linha 2: Notas, Quadrante, Período e Recorrência
        row2 = tk.Frame(quick_add_container, bg=THEME["panel2"])
        row2.pack(fill="x", padx=5, pady=(4, 0))

        # Notas (Mini)
        self.quick_notes_entry = tk.Entry(
            row2,
            bg=THEME["bg"],
            fg=THEME["muted"],
            insertbackground=THEME["neon"],
            font=("Consolas", 9),
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME["panel"],
        )
        self.quick_notes_entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
        # Placeholder fake
        self.quick_notes_entry.insert(0, "Notas...")
        self.quick_notes_entry.bind("<FocusIn>", lambda e: self.quick_notes_entry.delete(0, 'end') if self.quick_notes_entry.get() == "Notas..." else None)

        # Seletor de Quadrante
        self.quick_quadrant_var = tk.StringVar(value="Q2")
        quad_opt = tk.OptionMenu(row2, self.quick_quadrant_var, "Q1", "Q2", "Q3", "Q4")
        quad_opt.config(bg=THEME["bg"], fg=THEME["pink"], relief="flat", font=("Consolas", 8), highlightthickness=0, width=3)
        quad_opt["menu"].config(bg=THEME["panel"], fg=THEME["text"])
        quad_opt.pack(side="left", padx=2)

        # Seletor de Período
        self.quick_period_var = tk.StringVar(value="FLEXÍVEL")
        period_opt = tk.OptionMenu(row2, self.quick_period_var, "FLEXÍVEL", "MANHÃ", "TARDE", "NOITE")
        period_opt.config(bg=THEME["bg"], fg=THEME["neon"], relief="flat", font=("Consolas", 8), highlightthickness=0, width=8)
        period_opt["menu"].config(bg=THEME["panel"], fg=THEME["text"])
        period_opt.pack(side="left", padx=2)

        # Checkbox Recorrência
        self.quick_recur_var = tk.BooleanVar(value=False)
        self.quick_recur_check = tk.Checkbutton(
            row2,
            text="♻",
            variable=self.quick_recur_var,
            bg=THEME["panel2"],
            fg=THEME["neon"],
            selectcolor=THEME["bg"],
            activebackground=THEME["panel2"],
            activeforeground=THEME["neon"],
            relief="flat",
            font=("Consolas", 10)
        )
        self.quick_recur_check.pack(side="left", padx=2)

        btns = tk.Frame(left, bg=THEME["panel"])
        btns.pack(fill="x", padx=10, pady=(0, 10))

        self._btn(btns, "Add", self._add_task).pack(side="left")
        self._btn(btns, "Edit", self._edit_task).pack(side="left", padx=6)
        self._btn(btns, "Done", self._mark_done).pack(side="left", padx=6)
        self._btn(btns, "Delete", self._delete_task).pack(side="left", padx=6)
        self._btn(btns, "Dominó!", self._capture_distraction).pack(side="left", padx=6)

        # LEGENDA DOS CHECKBOXES
        legend_frame = tk.Frame(left, bg=THEME["panel"], pady=5)
        legend_frame.pack(fill="x", padx=10)

        # Legenda Active
        l1 = tk.Frame(legend_frame, bg=THEME["panel"])
        l1.pack(anchor="w")
        tk.Label(l1, text="▣", fg=THEME["neon"], bg=THEME["panel"], font=THEME["font"]).pack(side="left")
        tk.Label(l1, text=" Enviar p/ Planejamento (IA)", fg=THEME["muted"], bg=THEME["panel"], font=("Consolas", 8)).pack(side="left")

        # Legenda Done
        l2 = tk.Frame(legend_frame, bg=THEME["panel"])
        l2.pack(anchor="w")
        tk.Label(l2, text="▣", fg=THEME["pink"], bg=THEME["panel"], font=THEME["font"]).pack(side="left")
        tk.Label(l2, text=" Marcar como Concluído (FIM)", fg=THEME["muted"], bg=THEME["panel"], font=("Consolas", 8)).pack(side="left")

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
        
        # NOVO BOTÃO: Limpar Chat
        self._btn(quick, "Limpar Chat", self._clear_chat_ui).pack(side="left", padx=6)

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
        # Limpa o frame atual
        for widget in self.task_inner_frame.winfo_children():
            widget.destroy()

        # IMPORTANTE: Guardar as variáveis para evitar Garbage Collection
        self.check_vars = []

        # Ordenação: Ativas primeiro, depois por quadrante
        quadrant_order = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
        sorted_tasks = sorted(
            self.tasks,
            key=lambda t: (t.status == "DONE", quadrant_order.get(t.quadrant, 9))
        )

        for task in sorted_tasks:
            row = tk.Frame(self.task_inner_frame, bg=THEME["panel2"], pady=2)
            row.pack(fill="x", expand=True)

            # Variáveis persistentes
            active_var = tk.BooleanVar(value=task.active)
            done_var = tk.BooleanVar(value=(task.status == "DONE"))
            self.check_vars.append((active_var, done_var))  # Mantém na memória

            # 1. CHECKBOX: ENVIAR HOJE (Active)
            cb_active = tk.Checkbutton(
                row, variable=active_var, 
                command=lambda t=task, v=active_var: self._toggle_active(t, v),
                bg=THEME["panel2"], 
                selectcolor=THEME["bg"],  # Cor do fundo do quadradinho quando marcado
                activebackground=THEME["panel2"],
                fg=THEME["neon"]
            )
            cb_active.pack(side="left")

            # 2. CHECKBOX: CONCLUÍDO (Done)
            cb_done = tk.Checkbutton(
                row, variable=done_var,
                command=lambda t=task, v=done_var: self._toggle_done(t, v),
                bg=THEME["panel2"], 
                selectcolor=THEME["bg"],
                activebackground=THEME["panel2"],
                fg=THEME["pink"]
            )
            cb_done.pack(side="left")

            # Label do Texto
            is_done = task.status == "DONE"
            color = THEME["muted"] if is_done else (THEME["neon"] if task.active else THEME["text"])
            
            # Fonte: riscada se estiver pronto
            font_family = THEME["font"][0]
            font_size = THEME["font"][1]
            font_style = (font_family, font_size, "overstrike") if is_done else (font_family, font_size)
            
            period_short = (task.period or "F")[0].upper()
            txt = f"[{task.quadrant}] ({period_short}) {task.title}"
            
            lbl = tk.Label(row, text=txt, fg=color, bg=THEME["panel2"], font=font_style, anchor="w")
            lbl.pack(side="left", fill="x", expand=True, padx=5)
            
            # Abrir editor ao clicar no texto
            lbl.bind("<Button-1>", lambda e, t=task: self._select_task_for_edit(t))

        # Atualiza o scrollregion após adicionar widgets
        self.task_inner_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        warning = check_identity_overload(self.tasks)
        if warning:
            self._log("SYSTEM", warning)

    def _toggle_active(self, task: TaskItem, var: tk.BooleanVar):
        task.active = var.get()
        # Se eu ativo uma tarefa que estava concluída, mudo o status dela para TODO
        if task.active and task.status == "DONE":
            task.status = "TODO"
        
        self.store.save_today(self.tasks)
        # Delay pequeno para o usuário ver o check antes de atualizar a lista toda
        self.root.after(100, self._refresh_task_list)

    def _toggle_done(self, task: TaskItem, var: tk.BooleanVar):
        val = var.get()
        task.status = "DONE" if val else "TODO"
        
        # Se marquei como DONE, desativo do planejamento automaticamente
        if val:
            task.active = False
        else:
            # Se desmarquei o DONE, ativo para o planejamento
            task.active = True
            
        self.store.save_today(self.tasks)
        self.root.after(100, self._refresh_task_list)

    def _select_task_for_edit(self, task: TaskItem):
        # Como não temos mais o Listbox, usamos o clique no texto para abrir o editor
        self.selected_task = task
        idx = self.tasks.index(task)
        self._task_editor(title="Editar tarefa", task=task, index=idx)

    def _add_task(self) -> None:
        self._task_editor(title="Nova tarefa")

    def _edit_task(self) -> None:
        if not self.selected_task:
            messagebox.showinfo("Ops", "Clique em uma tarefa para selecioná-la e depois em 'Edit'.")
            return
        idx = self.tasks.index(self.selected_task)
        self._task_editor(title="Editar tarefa", task=self.selected_task, index=idx)

    def _delete_task(self) -> None:
        if not self.selected_task:
            messagebox.showinfo("Ops", "Clique em uma tarefa para selecioná-la e depois em 'Delete'.")
            return
        self.tasks.remove(self.selected_task)
        self.store.save_today(self.tasks)
        self.selected_task = None
        self._refresh_task_list()

    def _mark_done(self) -> None:
        if not self.selected_task:
            messagebox.showinfo("Ops", "Clique em uma tarefa para selecioná-la e depois em 'Done'.")
            return
        self.selected_task.status = "DONE"
        self.selected_task.active = False
        self.store.save_today(self.tasks)
        self._refresh_task_list()

    def _quick_add(self) -> None:
        """Adição rápida de tarefas sem abrir popups."""
        title = self.quick_entry.get().strip()
        if not title:
            return

        # Captura os novos valores
        notes = self.quick_notes_entry.get().strip()
        if notes == "Notas...": # ignora o placeholder
            notes = ""
            
        quad = self.quick_quadrant_var.get()
        period = self.quick_period_var.get()
        is_recurring = self.quick_recur_var.get()

        # Cria a tarefa com os novos campos
        new_task = TaskItem.create(
            title=title, 
            notes=notes, 
            quadrant=quad, 
            period=period, 
            is_recurring=is_recurring
        )
        
        self.tasks.append(new_task)
        self.store.save_today(self.tasks)
        self._refresh_task_list()

        # Limpeza e Reset
        self.quick_entry.delete(0, "end")
        self.quick_notes_entry.delete(0, "end")
        self.quick_notes_entry.insert(0, "Notas...")
        self.quick_recur_var.set(False)
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

        # Chamada usando nomes de argumentos (mais seguro)
        await self.runner.ask_stream(
            user_message=text,
            tasks=self.tasks,
            on_chunk=on_chunk,           # Passando explicitamente
            last_plan=self.last_agent_output,  # Agora o plano anterior vai no prompt
            on_final=on_final,
        )

    def _clear_chat_ui(self) -> None:
        """Limpa o Banco, a Memória do Agente e a Tela."""
        if messagebox.askyesno("Limpar Chat", "Deseja apagar todo o histórico de conversa de hoje?"):
            # 1. Limpa no Banco de Dados
            self.chat_store.clear()
            
            # 2. Limpa na memória do Runner (IA)
            self.runner.clear_history()
            
            # 3. Limpa a tela (Widget Text)
            self.chat.configure(state="normal")
            self.chat.delete("1.0", "end")
            self.chat.configure(state="disabled")
            
            self._log("SYSTEM", "Histórico de chat e memória da IA resetados para hoje.")

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
        
        if not self.last_agent_output.strip():
            self._log("SYSTEM", "Nenhum plano na memória. Gere um plano ou envie uma mensagem.")
            self.sync_btn.config(state="normal")
            return

        def worker():
            try:
                from ops_plan_parser import parse_ops_plan
                # Teste prévio do parser
                preview = parse_ops_plan(self.last_agent_output)
                if not preview:
                    self._log("SYSTEM", "Falha: O parser não encontrou linhas de horário no formato '- HH:MM–HH:MM'.")
                    self.root.after(0, lambda: self.sync_btn.config(state="normal"))
                    return

                self._log("SYSTEM", f"Sincronizando {len(preview)} tarefas com GCal...")
                
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
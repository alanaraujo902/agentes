import asyncio
import json
import re
import time
import uuid
import sqlite3
from datetime import datetime
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.models.openai import OpenAIChatCompletionClient


# =========================================================
# Config
# =========================================================
@dataclass(frozen=True)
class DailyOpsConfig:
    model: str = "gpt-4o-mini"
    max_tool_iterations: int = 2
    max_context_tasks: int = 40

# =========================================================
# Persistência com SQLite (Substituindo JSON)
# =========================================================

class DatabaseManager:
    def __init__(self, vault_dir: Path) -> None:
        self.db_path = (vault_dir / "ops_agent_vault.db").absolute()
        vault_dir.mkdir(parents=True, exist_ok=True)
        print(f"[*] Iniciando Banco de Dados em: {self.db_path}")
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self):
        with self._get_connection() as conn:
            # Tabela de Tarefas
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    notes TEXT,
                    quadrant TEXT,
                    period TEXT,
                    status TEXT,
                    active INTEGER DEFAULT 1,
                    is_recurring INTEGER DEFAULT 0,
                    created_at REAL,
                    day_date TEXT
                )
            """)
            # Tabela de Chat
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT,
                    content TEXT,
                    timestamp REAL,
                    day_date TEXT
                )
            """)
            # Tabela de Distrações (Dominó)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS distractions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT,
                    processed INTEGER DEFAULT 0,
                    day_date TEXT
                )
            """)
            conn.commit()


# =========================================================
# Modelo de Tarefa
# =========================================================
@dataclass
class TaskItem:
    id: str
    title: str
    notes: str = ""
    quadrant: str = "Q2"       # Q1, Q2, Q3, Q4
    period: str = "FLEXÍVEL"   # FLEXÍVEL / MANHÃ / TARDE / NOITE
    status: str = "TODO"       # TODO / DOING / DONE
    active: bool = True        # Define se a tarefa entra no plano
    is_recurring: bool = False  # Recorrente ou Única
    created_at: float = 0.0

    @staticmethod
    def create(title: str, notes: str = "", quadrant: str = "Q2", period: str = "FLEXÍVEL", is_recurring: bool = False) -> "TaskItem":
        return TaskItem(
            id=str(uuid.uuid4())[:8],
            title=title.strip(),
            notes=notes.strip(),
            quadrant=quadrant.strip().upper(),
            period=period.strip().upper(),
            status="TODO",
            active=True,
            is_recurring=is_recurring,
            created_at=time.time(),
        )


# =========================================================
# Persistência simples (JSON por dia)
# =========================================================
class TaskStore:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db = db_manager

    def _today_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def load_today(self) -> List[TaskItem]:
        today = self._today_str()
        tasks = self._fetch_tasks_by_date(today)
        
        # Se não há tarefas hoje, vamos processar a virada do dia
        if not tasks:
            tasks = self._rollover_tasks(today)
            if tasks:
                print(f"[*] Dia {today} vazio. Buscando tarefas do dia anterior...")
                print(f"[*] Encontradas {len(tasks)} tarefas do dia anterior. Aplicando rollover...")
                self.save_today(tasks)
            else:
                print(f"[*] Nenhuma tarefa encontrada para rollover.")
        
        return tasks
    
    def _fetch_tasks_by_date(self, date_str: str) -> List[TaskItem]:
        tasks = []
        with self.db._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM tasks WHERE day_date = ?", (date_str,))
            for row in cursor:
                # row.keys() para checar se a coluna existe e evitar erros em migrações
                keys = row.keys()
                tasks.append(TaskItem(
                    id=row["id"],
                    title=row["title"],
                    notes=row["notes"],
                    quadrant=row["quadrant"],
                    period=row["period"],
                    status=row["status"],
                    active=bool(row["active"]) if "active" in keys else True,
                    is_recurring=bool(row["is_recurring"]) if "is_recurring" in keys else False,
                    created_at=row["created_at"]
                ))
        return tasks
    
    def _rollover_tasks(self, today_str: str) -> List[TaskItem]:
        """Busca o último dia com tarefas e decide o que sobrevive."""
        with self.db._get_connection() as conn:
            # Pega a data mais recente antes de hoje
            last_date_row = conn.execute(
                "SELECT day_date FROM tasks WHERE day_date < ? ORDER BY day_date DESC LIMIT 1", 
                (today_str,)
            ).fetchone()
            
            if not last_date_row:
                return []
            
            last_date = last_date_row[0]
            last_tasks = self._fetch_tasks_by_date(last_date)
            
            new_tasks = []
            for t in last_tasks:
                # REGRA NINJA:
                # 1. Se é recorrente: Sempre passa para o dia seguinte (resetando status)
                # 2. Se é única mas NÃO foi feita: Passa para o dia seguinte (acumula)
                # 3. Se é única e FOI feita: Morre no dia anterior (concluído!)
                
                should_pass = t.is_recurring or (not t.is_recurring and t.status != "DONE")
                
                if should_pass:
                    # Resetamos status de tarefas recorrentes feitas
                    status = "TODO" if t.is_recurring else t.status
                    
                    # Criamos uma nova instância para o novo dia
                    new_tasks.append(TaskItem(
                        id=str(uuid.uuid4())[:8],  # Novo ID para o novo dia
                        title=t.title,
                        notes=t.notes,
                        quadrant=t.quadrant,
                        period=t.period,
                        status=status,
                        active=t.active,
                        is_recurring=t.is_recurring,
                        created_at=time.time()
                    ))
            return new_tasks

    def save_today(self, tasks: List[TaskItem]) -> None:
        today = self._today_str()
        with self.db._get_connection() as conn:
            # Upsert simples: deleta as do dia e reinsere (estratégia KISS)
            conn.execute("DELETE FROM tasks WHERE day_date = ?", (today,))
            for t in tasks:
                conn.execute("""
                    INSERT INTO tasks (id, title, notes, quadrant, period, status, active, is_recurring, created_at, day_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (t.id, t.title, t.notes, t.quadrant, t.period, t.status, 
                      int(t.active), int(t.is_recurring), t.created_at, today))
            conn.commit()
            print(f"[*] Salvas {len(tasks)} tarefas para o dia {today}")


class DistractionStore:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db = db_manager

    def add(self, text: str) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.db._get_connection() as conn:
            conn.execute("INSERT INTO distractions (content, day_date) VALUES (?, ?)", (text, today))
            conn.commit()

    def load(self) -> List[str]:
        with self.db._get_connection() as conn:
            cursor = conn.execute("SELECT content FROM distractions WHERE processed = 0")
            return [row[0] for row in cursor.fetchall()]

    def clear(self) -> None:
        with self.db._get_connection() as conn:
            conn.execute("UPDATE distractions SET processed = 1")
            conn.commit()


class ChatStore:
    def __init__(self, db_manager: DatabaseManager) -> None:
        self.db = db_manager

    def load(self) -> List[Dict[str, str]]:
        today = datetime.now().strftime("%Y-%m-%d")
        messages = []
        with self.db._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT role, content FROM chat_history WHERE day_date = ? ORDER BY timestamp ASC", 
                (today,)
            )
            for row in cursor:
                messages.append({"role": row["role"], "content": row["content"]})
        return messages

    def save(self, messages: List[Dict[str, str]]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        with self.db._get_connection() as conn:
            # Evitar duplicados: limpa o chat do dia antes de salvar o histórico atualizado
            conn.execute("DELETE FROM chat_history WHERE day_date = ?", (today,))
            for msg in messages:
                conn.execute("""
                    INSERT INTO chat_history (role, content, timestamp, day_date)
                    VALUES (?, ?, ?, ?)
                """, (msg["role"], msg["content"], time.time(), today))
            conn.commit()

    def clear(self) -> None:
        """Apaga o histórico de chat do dia atual no banco."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self.db._get_connection() as conn:
            conn.execute("DELETE FROM chat_history WHERE day_date = ?", (today,))
            conn.commit()


# =========================================================
# Diretriz do Agente (Produtividade + insights)
# =========================================================
OPS_SYSTEM = r"""
Você é o OPS_AGENT, um mestre em produtividade Ninja e Essencialismo.
Sua missão é transformar a reatividade do usuário em protagonismo e "Transformação Vivida".

ESTRUTURA DA RESPOSTA (OBRIGATÓRIA):

1) Intento Essencial do Dia
- Defina em uma linha o objetivo que traria 80% do resultado (Pareto). Use a perspectiva do "Porquê".

2) Plano (Cronograma Ninja)
REGRAS DE FORMATO PARA O PLANO:
- Use "- HH:MM–HH:MM — [CATEGORIA] Atividade (Duração; Prioridade)".
- Categorias permitidas: 
  * [TRABALHO FOCADO] (Deep Work/Teleporte: blocos de 90 min para tarefas P1)
  * [TRABALHO SUPERFICIAL] (Logística, e-mails, burocracia)
  * [POWER UP] (Recarga: respiração, hidratação, alongamento)
  * [BUFFER] (Margem de segurança de 15-30 min entre tarefas complexas)

3) Próximo Passo (MVT - Tarefa Mínima Viável)
- Identifique a tarefa P1 e reduza-a a uma ação de 2 minutos para vencer a inércia.

4) Higiene Mental (Cinegrafista)
- Uma observação sobre um possível "Inimigo do Foco" (ex: afobação, multitarefa ou dominó mental) detectado no contexto.

DIRETRIZES TÉCNICAS:
- Aplique a Regra dos 90%: Se uma tarefa não é claramente um "sim" (importância > 90), sugira descartar ou delegar.
- Insira um [POWER UP] obrigatoriamente a cada 90-120 min de trabalho.
- Use o tom de Seiiti Arata: direto, prático, focado em "Transformação Vivida > Teoria Entendida".

DIRETRIZES DE CONTINUIDADE:
- Você é um planejador dinâmico. Se o usuário enviar uma mensagem e já houver um plano, NÃO REESCREVA DO ZERO.
- Preserve os blocos de tempo que já passaram como 'Histórico' e ajuste o futuro.
- Se uma tarefa foi marcada como concluída (✅) no contexto, remova-a do "Plano (Cronograma Ninja)" futuro ou marque-a como feita no plano.
- Se o usuário pedir para "adicionar algo", encontre um [BUFFER] ou [TRABALHO SUPERFICIAL] no plano existente para substituir ou encaixar, mantendo o restante estável.

EXEMPLO DE SAÍDA:
1) Intento Essencial do Dia:
Consolidar a arquitetura do projeto para eliminar retrabalho futuro.

2) Plano
- 08:00–08:15 — [POWER UP] Ritual Matinal: 9 respirações e hidratação (15 min; P1)
- 08:15–09:45 — [TRABALHO FOCADO] Codar módulo de autenticação (90 min; P1)
- 09:45–10:00 — [BUFFER] Margem de segurança (15 min; P3)
- 10:00–10:30 — [TRABALHO SUPERFICIAL] Responder e-mails e Slack (30 min; P2)

3) Próximo Passo (MVT)
Abrir o arquivo index.js e escrever o comentário da primeira função (2 min).

4) Higiene Mental (Cinegrafista)
Notei muitas tarefas pequenas. Cuidado com o "Dominó Mental": não deixe uma aba de e-mail aberta destruir seu Teleporte no código.
"""


def check_identity_overload(tasks: List[TaskItem]) -> str:
    """Filtro de Identidade e alerta de excesso de Q1 (incêndios demais)."""
    q1_tasks = [t for t in tasks if getattr(t, "quadrant", "Q2") == "Q1" and t.status != "DONE"]
    if len(q1_tasks) > 5:
        return (
            "\n⚠️ ALERTA NINJA: Você tem mais de 5 tarefas no Quadrante Q1. Parece que está tentando apagar incêndios demais. "
            "Escolha o incêndio que, se resolvido, reduz ou elimina vários outros."
        )
    return ""


# =========================================================
# Runner stateful (mantém conversa)
# =========================================================
class DailyOpsRunner:
    def __init__(self, config: DailyOpsConfig, history: Optional[List[Dict[str, str]]] = None) -> None:
        self.config = config
        self._model_client = OpenAIChatCompletionClient(model=config.model)
        self._agent = AssistantAgent(
            name="ops_agent",
            model_client=self._model_client,
            system_message=OPS_SYSTEM,
            model_client_stream=True,
            max_tool_iterations=config.max_tool_iterations,
        )
        # Histórico de mensagens user/assistant (persistido por dia)
        self.history: List[Dict[str, str]] = history or []
        self._lock = asyncio.Lock()

    def _build_context(self, tasks: List[TaskItem], last_plan: str = "") -> str:
        agora = datetime.now()
        
        # FILTRO CRÍTICO: Só envia para o agente o que está ATIVO (checkbox marcado)
        tarefas_ativas = [t for t in tasks if getattr(t, "active", True)]
        
        if not tarefas_ativas:
            return "O usuário não selecionou nenhuma tarefa como 'ativa' para hoje ainda."
        
        # Ordenação para o prompt
        quadrant_order = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
        tasks_sorted = sorted(
            tarefas_ativas,
            key=lambda t: (
                t.status == "DONE",  # TODO/DOING primeiro
                quadrant_order.get(getattr(t, "quadrant", "Q2"), 9),
                t.created_at,
            ),
        )[: self.config.max_context_tasks]
        
        lines: List[str] = []
        lines.append(f"HORA ATUAL: {agora.strftime('%H:%M')}")
        
        # Se houver um plano anterior, injetamos ele como a "verdade atual"
        if last_plan:
            # LIMPEZA: Se o last_plan já contém "=== CRONOGRAMA VIGENTE ===", 
            # pegamos apenas a parte do Plano para não empilhar lixo.
            if "2) Plano" in last_plan or "2) Cronograma" in last_plan:
                # Tenta extrair apenas do "2) Plano" em diante
                match = re.search(r"2\)\s*(?:Plano|Cronograma).*", last_plan, re.S | re.I)
                if match:
                    last_plan = match.group(0)
            
            lines.append("\n=== CRONOGRAMA VIGENTE (Última versão) ===")
            lines.append(last_plan)
            lines.append("===========================================\n")
        
        lines.append("ESTADO ATUAL DAS TAREFAS:")
        
        for t in tasks_sorted:
            status = "✅" if t.status == "DONE" else "•"
            quadrant = getattr(t, "quadrant", "Q2")
            # Se for flexível, avisamos explicitamente ao agente
            period_raw = getattr(t, "period", "FLEXÍVEL")
            period = period_raw if period_raw != "FLEXÍVEL" else "QUALQUER MOMENTO (FLEXÍVEL)"
            notes_part = f" (Notas: {t.notes})" if t.notes else ""
            lines.append(f"  {status} [{quadrant}] ({period}) {t.title}{notes_part}")
        
        return "\n".join(lines)

    async def ask_stream(
        self,
        user_message: str,
        tasks: List[TaskItem],
        on_chunk: Callable[[str], None],  # Parâmetro obrigatório vem antes
        last_plan: str = "",             # Parâmetros com default vêm depois
        on_final: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Chama o agente em streaming e faz callback com chunks.
        AgentChat é stateful: chamadas subsequentes continuam a conversa. :contentReference[oaicite:3]{index=3}
        """
        async with self._lock:
            # Agora o contexto leva o plano anterior
            tasks_ctx = self._build_context(tasks, last_plan)

            prompt = (
                "CONTEXTO DO SISTEMA:\n"
                f"{tasks_ctx}\n\n"
                "INSTRUÇÃO DO USUÁRIO:\n"
                f"{user_message.strip()}\n\n"
                "⚠️ IMPORTANTE: Mantenha a estrutura do plano anterior. "
                "Ajuste apenas os horários necessários para acomodar a nova solicitação ou mudanças de status. "
                "Não remova tarefas que ainda não foram concluídas, a menos que solicitado."
            )

            full = ""
            async for item in self._agent.run_stream(task=prompt):
                # Os eventos de streaming carregam "content" nos chunks finais e/ou intermediários
                text = getattr(item, "content", None)
                if not text:
                    continue
                full += text
                on_chunk(text)

            # Atualiza histórico interno
            self.history.append({"role": "user", "content": user_message})
            self.history.append({"role": "assistant", "content": full})

            if on_final is not None:
                on_final(full)

    def clear_history(self) -> None:
        """Limpa a memória de curto prazo do agente."""
        self.history = []
        # O AutoGen armazena estado no AgentChat, resetar o histórico 
        # aqui garante que nas próximas chamadas o prompt seja 'limpo'.

    async def close(self) -> None:
        await self._model_client.close()

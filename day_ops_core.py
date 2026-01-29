import asyncio
import json
import time
import uuid
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
    model: str = "gpt-5-nano"
    max_tool_iterations: int = 2  # KISS: sem tool-use pesado aqui
    max_context_tasks: int = 40   # limite pra não virar dump gigante


# =========================================================
# Modelo de Tarefa
# =========================================================
@dataclass
class TaskItem:
    id: str
    title: str
    notes: str = ""
    quadrant: str = "Q2"       # Q1, Q2, Q3, Q4
    period: str = "MANHÃ"      # MANHÃ / TARDE / NOITE
    status: str = "TODO"       # TODO / DOING / DONE
    created_at: float = 0.0

    @staticmethod
    def create(title: str, notes: str = "", quadrant: str = "Q2", period: str = "MANHÃ") -> "TaskItem":
        return TaskItem(
            id=str(uuid.uuid4())[:8],
            title=title.strip(),
            notes=notes.strip(),
            quadrant=quadrant.strip().upper(),
            period=period.strip().upper(),
            status="TODO",
            created_at=time.time(),
        )


# =========================================================
# Persistência simples (JSON por dia)
# =========================================================
class TaskStore:
    def __init__(self, vault_dir: Path) -> None:
        self.vault_dir = vault_dir
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def _today_file(self) -> Path:
        yyyy_mm_dd = time.strftime("%Y-%m-%d")
        return self.vault_dir / f"tasks_{yyyy_mm_dd}.json"

    def load_today(self) -> List[TaskItem]:
        fp = self._today_file()
        if not fp.exists():
            return []
        data = json.loads(fp.read_text(encoding="utf-8"))
        tasks: List[TaskItem] = []
        for item in data.get("tasks", []):
            # Backward-compat: converte tarefas antigas com 'priority' para 'quadrant'
            quadrant = item.get("quadrant")
            if quadrant is None:
                prio = item.get("priority", "P2")
                quadrant = {
                    "P1": "Q1",  # Importante e urgente
                    "P2": "Q2",  # Importante, não urgente
                    "P3": "Q3",  # Não importante, urgente
                }.get(prio, "Q4")

            period = item.get("period", "MANHÃ")

            tasks.append(
                TaskItem(
                    id=item.get("id", str(uuid.uuid4())[:8]),
                    title=item.get("title", "").strip(),
                    notes=item.get("notes", "").strip(),
                    quadrant=str(quadrant).upper(),
                    period=str(period).upper(),
                    status=item.get("status", "TODO"),
                    created_at=item.get("created_at", time.time()),
                )
            )
        return tasks

    def save_today(self, tasks: List[TaskItem]) -> None:
        fp = self._today_file()
        payload = {"tasks": [asdict(t) for t in tasks]}
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class DistractionStore:
    """Captura e processa 'Dominó Mental' (distrações ao longo do dia)."""

    def __init__(self, vault_dir: Path) -> None:
        self.fp = vault_dir / "distractions_to_process.json"

    def load(self) -> List[str]:
        if not self.fp.exists():
            return []
        try:
            return json.loads(self.fp.read_text(encoding="utf-8")).get("distractions", [])
        except json.JSONDecodeError:
            return []

    def add(self, text: str) -> None:
        items = self.load()
        items.append(text)
        self.fp.write_text(json.dumps({"distractions": items}, ensure_ascii=False), encoding="utf-8")

    def clear(self) -> None:
        if self.fp.exists():
            self.fp.unlink()


class ChatStore:
    """Persistência do histórico de chat por dia."""

    def __init__(self, vault_dir: Path) -> None:
        self.vault_dir = vault_dir
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def _get_file(self) -> Path:
        yyyy_mm_dd = time.strftime("%Y-%m-%d")
        return self.vault_dir / f"chat_history_{yyyy_mm_dd}.json"

    def load(self) -> List[Dict[str, str]]:
        fp = self._get_file()
        if not fp.exists():
            return []
        try:
            return json.loads(fp.read_text(encoding="utf-8")).get("messages", [])
        except Exception:
            return []

    def save(self, messages: List[Dict[str, str]]) -> None:
        fp = self._get_file()
        payload = {"messages": messages}
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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

    def _build_context(self, tasks: List[TaskItem]) -> str:
        # Limita número de tasks para não poluir contexto
        quadrant_order = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
        tasks_sorted = sorted(
            tasks,
            key=lambda t: (
                t.status == "DONE",  # TODO/DOING primeiro
                quadrant_order.get(getattr(t, "quadrant", "Q2"), 9),
                t.created_at,
            ),
        )[: self.config.max_context_tasks]

        lines: List[str] = []
        for t in tasks_sorted:
            prefix = "✅" if t.status == "DONE" else ("▶" if t.status == "DOING" else "•")
            quadrant = getattr(t, "quadrant", "Q2")
            period = getattr(t, "period", "MANHÃ")
            lines.append(f"{prefix} [{quadrant}] ({period}) {t.title}")

        if not lines:
            return "Sem tarefas registradas hoje."

        return "\n".join(lines)

    async def ask_stream(
        self,
        user_message: str,
        tasks: List[TaskItem],
        on_chunk: Callable[[str], None],
        on_final: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        Chama o agente em streaming e faz callback com chunks.
        AgentChat é stateful: chamadas subsequentes continuam a conversa. :contentReference[oaicite:3]{index=3}
        """
        async with self._lock:
            tasks_ctx = self._build_context(tasks)

            prompt = (
                "TAREFAS DO DIA (contexto):\n"
                f"{tasks_ctx}\n\n"
                "MENSAGEM DO USUÁRIO:\n"
                f"{user_message.strip()}\n"
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

    async def close(self) -> None:
        await self._model_client.close()

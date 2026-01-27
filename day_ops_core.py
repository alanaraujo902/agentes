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
    priority: str = "P2"       # P1 / P2 / P3
    status: str = "TODO"       # TODO / DOING / DONE
    created_at: float = 0.0

    @staticmethod
    def create(title: str, notes: str = "", priority: str = "P2") -> "TaskItem":
        return TaskItem(
            id=str(uuid.uuid4())[:8],
            title=title.strip(),
            notes=notes.strip(),
            priority=priority.strip().upper(),
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
        tasks = []
        for item in data.get("tasks", []):
            tasks.append(TaskItem(**item))
        return tasks

    def save_today(self, tasks: List[TaskItem]) -> None:
        fp = self._today_file()
        payload = {"tasks": [asdict(t) for t in tasks]}
        fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# =========================================================
# Diretriz do Agente (Produtividade + insights)
# =========================================================
OPS_SYSTEM = r"""
Você é o OPS_AGENT, um copiloto de produtividade diária.

OBJETIVO:
- Organizar o dia do usuário em blocos claros de tempo.
- Priorizar tarefas com P1 / P2 / P3.
- Produzir um PLANO EXECUTÁVEL que possa ser convertido diretamente em eventos de calendário.

REGRAS ABSOLUTAS:

1. TODA resposta deve conter a seção:

2) Plano

2. Dentro de "2) Plano", TODAS as linhas DEVEM seguir UM destes dois formatos:

A) Ação imediata (sem horário):

- <descrição> (<duração>; P1|P2|P3) — impacto: <curto>; esforço: <curto>.

Exemplo:
- Colocar a roupa de corrida na mochila agora (2 min; P2) — impacto: evita atrito à noite; esforço: mínimo.

B) Bloco com horário:

- HH:MM–HH:MM — <atividade> (<duração>; P1|P2|P3). <comentário curto opcional>

Exemplo:
- 07:00–08:30 — Programar (90 min; P2). Use modo foco: DND, sem interrupções.

3. SEMPRE usar:

- traço "-" no início da linha
- intervalo horário com “–”
- prioridade dentro de parênteses: (P1), (P2) ou (P3)

4. Nunca misture formatos.
5. Nunca escreva parágrafos dentro do Plano.
6. Nunca use bullets diferentes de "-".
7. Nunca omita prioridade.
8. Nunca escreva tarefas fora da seção "2) Plano".

ESTRUTURA FIXA DA RESPOSTA:

1) Situação (máx 2 linhas)

2) Plano
- linhas obrigatoriamente no formato acima

3) Próximo passo
- exatamente UMA ação curta

4) Pergunta rápida
- exatamente UMA pergunta

OUTRAS DIRETRIZES:

- Sugira blocos de foco (25/50/90 min).
- Use impacto x esforço quando houver ação imediata.
- Seja direto.
- Nada de textão.
- Linguagem operacional.

IMPORTANTE:
Este formato é CONTRATO DE INTEGRAÇÃO COM SOFTWARE.
Quebras de formato quebram automação.
"""


# =========================================================
# Runner stateful (mantém conversa)
# =========================================================
class DailyOpsRunner:
    def __init__(self, config: DailyOpsConfig) -> None:
        self.config = config
        self._model_client = OpenAIChatCompletionClient(model=config.model)
        self._agent = AssistantAgent(
            name="ops_agent",
            model_client=self._model_client,
            system_message=OPS_SYSTEM,
            model_client_stream=True,                 # streaming :contentReference[oaicite:2]{index=2}
            max_tool_iterations=config.max_tool_iterations,
        )
        self._lock = asyncio.Lock()

    def _build_context(self, tasks: List[TaskItem]) -> str:
        # Limita número de tasks para não poluir contexto
        tasks_sorted = sorted(
            tasks,
            key=lambda t: (t.status != "DOING", t.priority, t.created_at),
        )[: self.config.max_context_tasks]

        lines = []
        for t in tasks_sorted:
            prefix = "✅" if t.status == "DONE" else ("▶" if t.status == "DOING" else "•")
            lines.append(f"{prefix} [{t.priority}] ({t.status}) {t.title}")

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

            if on_final is not None:
                on_final(full)

    async def close(self) -> None:
        await self._model_client.close()

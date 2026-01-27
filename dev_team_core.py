import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from autogen_agentchat.agents import AssistantAgent, CodeExecutorAgent
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_agentchat.conditions import (
    TextMentionTermination,
    MaxMessageTermination,
    ExternalTermination,
)
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.code_executors.docker import DockerCommandLineCodeExecutor
from autogen_ext.code_executors.local import LocalCommandLineCodeExecutor

@dataclass(frozen=True)
class DevTeamConfig:
    model: str = "gpt-4o"
    use_docker: bool = True
    max_messages: int = 20

def safe_approval_func(code: str) -> bool:
    code = (code or "").lower()
    deny = ["rm -rf /", "mkfs", "format "]
    return not any(p in code for p in deny)

# Diretrizes focadas em SH (mais compatível com executores)
ENGINEERING_DIRECTIVES = r"""
REGRAS DE OURO:
1) Use APENAS blocos de código com a tag 'sh'. Exemplo: ```sh
2) Para escrever arquivos: mkdir -p "caminho" && cat <<'EOF' > "caminho/arquivo"
(Conteúdo)
EOF
3) Sempre escreva o arquivo COMPLETO.
"""

PLANNER_SYSTEM = f"""Você é o PLANNER. 
{ENGINEERING_DIRECTIVES}
Crie a estratégia e liste os arquivos que serão criados."""

CODER_SYSTEM = f"""Você é o CODER. 
{ENGINEERING_DIRECTIVES}
Sua única função é gerar o comando SH para criar os arquivos. 
Não omita partes do código original. Use sempre ```sh ... ```."""

TESTER_SYSTEM = """Você é o TESTER. 
Sua única função é extrair e executar os blocos de código 'sh' fornecidos pelo CODER.
Se o código for executado sem erros, diga: 'EXECUÇÃO BEM SUCEDIDA'."""

REVIEWER_SYSTEM = """Você é o REVIEWER. 
Verifique se os arquivos foram criados e se o código está correto.
Se estiver tudo ok, finalize com a palavra: AUTOGEN_OK_9F1C"""

_AGENT_BUFFERS = {}

def _format_stream_item(item) -> str:
    if not hasattr(item, "content"): return ""
    agent = getattr(item, "source", None) or getattr(item, "name", None) or "team"
    text = item.content or ""
    buf = _AGENT_BUFFERS.setdefault(agent, "")
    buf += text
    if text.endswith(("\n", "```")) or len(buf) > 300:
        _AGENT_BUFFERS[agent] = ""
        return f"\n=== {agent.upper()} ===\n{buf.strip()}\n"
    _AGENT_BUFFERS[agent] = buf
    return ""

class DevTeamRunner:
    def __init__(self, config: DevTeamConfig, on_log: Callable[[str], None]) -> None:
        self._config = config
        self._on_log = on_log
        self._external_stop = ExternalTermination()
        self._running = False

    def stop(self): self._external_stop.set()

    async def run(self, task: str, workspace: Path):
        if self._running: return
        self._running = True
        
        model_client = OpenAIChatCompletionClient(model=self._config.model)

        # Agentes
        planner = AssistantAgent("planner", model_client, system_message=PLANNER_SYSTEM)
        coder = AssistantAgent("coder", model_client, system_message=CODER_SYSTEM)
        
        # Executor
        if self._config.use_docker:
            executor = DockerCommandLineCodeExecutor(work_dir=str(workspace), bind_dir=str(workspace))
            await executor.start()
        else:
            executor = LocalCommandLineCodeExecutor(work_dir=str(workspace))

        # O Tester agora é configurado explicitamente para não pedir confirmação e agir sobre 'sh'
        tester = CodeExecutorAgent(
            "tester", 
            executor, 
            system_message=TESTER_SYSTEM
        )
        
        reviewer = AssistantAgent("reviewer", model_client, system_message=REVIEWER_SYSTEM)

        termination = TextMentionTermination("AUTOGEN_OK_9F1C") | MaxMessageTermination(self._config.max_messages) | self._external_stop
        team = RoundRobinGroupChat([planner, coder, tester, reviewer], termination_condition=termination)

        try:
            async for item in team.run_stream(task=task):
                msg = _format_stream_item(item)
                if msg: self._on_log(msg)
        finally:
            if self._config.use_docker: await executor.stop()
            await model_client.close()
            self._running = False
import re
from dataclasses import dataclass
from typing import List

# MELHORIA: Só encerra o plano se encontrar o "3)" isolado no início de uma linha
PLAN_BLOCK = re.compile(r"2\)\s*Plano(.*?)(?=\n\s*3\)|$)", re.S | re.I)

# MELHORIA: Regex ultra-flexível para capturar a linha independente do tipo de traço
# Captura: 1.Início, 2.Fim, 3.Categoria(opcional), 4.Resto
LINE = re.compile(r"-\s*(\d{2}:\d{2})\s*[\-–—]\s*(\d{2}:\d{2})\s*[\-–—]?\s*(\[.*?\])?\s*(.*)")


@dataclass
class PlanTask:
    start: str
    end: str
    title: str
    priority: str = "P2"


def parse_ops_plan(text: str) -> List[PlanTask]:
    m = PLAN_BLOCK.search(text)
    if not m:
        return []

    block = m.group(1)
    tasks: List[PlanTask] = []

    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue

        lm = LINE.match(line)
        if not lm:
            continue

        start, end, category, rest = lm.groups()

        # Extrair Prioridade (P1, P2 ou P3) de dentro dos parênteses
        prio = "P2"
        prio_match = re.search(r"\(.*?(P\d).*?\)", rest)
        if prio_match:
            prio = prio_match.group(1)

        # Limpar o título: remove os parênteses do final (ex: tempo e prioridade)
        title_clean = re.sub(r"\(.*?\)\s*$", "", rest).strip()

        # Remove traços soltos que sobram no título
        title_clean = title_clean.lstrip("—–- ").strip()

        # Deixa apenas o nome da tarefa, sem categoria
        full_title = title_clean

        tasks.append(PlanTask(start=start, end=end, title=full_title, priority=prio))

    return tasks
import re
from dataclasses import dataclass
from typing import List

# Busca a ÚLTIMA ocorrência do bloco "2) Plano"
PLAN_BLOCK_RE = re.compile(r"2\)\s*(?:Plano|Cronograma).*?\n(.*?)(?=\n\s*3\)|$)", re.S | re.I)

# Regex para capturar as linhas
LINE_RE = re.compile(r"^\s*-\s*(\d{2}:\d{2})\s*[\-–—]\s*(\d{2}:\d{2})\s*[\-–—]?\s*(\[.*?\])?\s*(.*)", re.M)


@dataclass
class PlanTask:
    start: str
    end: str
    title: str
    priority: str = "P2"


def parse_ops_plan(text: str) -> List[PlanTask]:
    # Encontra todos os blocos de plano no texto
    blocks = list(PLAN_BLOCK_RE.finditer(text))
    if not blocks:
        return []

    # Pega apenas o ÚLTIMO bloco encontrado (o mais recente)
    last_block = blocks[-1].group(1)
    
    tasks: List[PlanTask] = []
    matches = LINE_RE.findall(last_block)
    
    for start, end, category, rest in matches:
        # 1. Extrair Prioridade
        prio = "P2"
        prio_match = re.search(r"\(.*?(P\d).*?\)", rest)
        if prio_match:
            prio = prio_match.group(1)

        # 2. LIMPAR TÍTULO (Remove os colchetes [CATEGORIA] e as notas no final)
        # Primeiro remove as notas (HH min; PX)
        title_clean = re.sub(r"\(.*?\)\s*$", "", rest).strip()
        # Remove traços iniciais
        title_clean = title_clean.lstrip("—–- ").strip()
        
        # O título final não deve conter a categoria [TRABALHO FOCADO]
        # pois ela já vem no parâmetro 'category' da regex LINE_RE
        tasks.append(PlanTask(start=start, end=end, title=title_clean, priority=prio))

    return tasks
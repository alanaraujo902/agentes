# ops_plan_parser.py

import re
from dataclasses import dataclass
from typing import List

PLAN_BLOCK = re.compile(r"2\)\s*Plano(.*?)3\)", re.S)
LINE = re.compile(
    r"-\s*(\d{2}:\d{2})[–-](\d{2}:\d{2})\s*—\s*(.+)"
)
PRIO = re.compile(r"\((?:\d+ min;\s*)?(P\d)\)")

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

    tasks = []

    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue

        lm = LINE.match(line)
        if not lm:
            continue

        start, end, rest = lm.groups()

        prio = "P2"
        pm = PRIO.search(rest)
        if pm:
            prio = pm.group(1)

        # remove (...) finais
        title = re.sub(r"\(.*?\)", "", rest).strip()

        tasks.append(
            PlanTask(
                start=start,
                end=end,
                title=title,
                priority=prio,
            )
        )

    return tasks

# gcal_sync.py
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Iterable, Optional, Tuple


from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build



# Escopo com permissão de escrita no calendário
SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Aceita qualquer um dos 3 tipos de traços entre as horas
TIME_RANGE_RE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*[\-–—]\s*(?P<end>\d{1,2}:\d{2})")

# Remove o prefixo de hora e o traço seguinte do título, aceitando qualquer traço
PREFIX_CLEAN_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*[\-–—]\s*\d{1,2}:\d{2}\s*[\-–—]\s*")

DEFAULT_TZ = "America/Sao_Paulo"


def _parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(hour=int(h), minute=int(m))


def _extract_time_range(title: str) -> Optional[Tuple[str, str]]:
    m = TIME_RANGE_RE.search(title)
    if not m:
        return None
    return m.group("start"), m.group("end")


def _strip_time_prefix(title: str) -> str:
    # Remove "06:00–06:20 — " do começo, se existir
    return PREFIX_CLEAN_RE.sub("", title).strip()


def _get_service(vault_dir: Path):
    """
    Guarda token no vault para não pedir login toda hora.
    """
    vault_dir.mkdir(parents=True, exist_ok=True)
    creds_path = vault_dir / "gcal_credentials.json"
    token_path = vault_dir / "gcal_token.json"

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Não achei {creds_path}. Coloque o JSON OAuth do Google Calendar com esse nome no seu vault."
        )

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            # abre navegador e autentica (desktop app)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    # cache_discovery=False evita criação de arquivo cache (menos atrito em ambientes variados)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _day_bounds(day: date, tz: ZoneInfo) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def sync_tasks_to_gcal(
    *,
    tasks: Iterable,
    vault_dir: Path,
    day: Optional[date] = None,
    calendar_id: str = "primary",
    tz_name: str = DEFAULT_TZ,
) -> dict:
    """
    Sincroniza as tarefas limpando o que o agente já tinha postado no dia.
    Estratégia "Clean Slate": deleta todos os eventos do OPS_AGENT do dia antes de inserir novos.
    """
    tz = ZoneInfo(tz_name)
    day = day or datetime.now(tz).date()
    day_start, day_end = _day_bounds(day, tz)

    service = _get_service(vault_dir)

    # --- 1. LIMPEZA (Nuke) ---
    # Busca e deleta todos os eventos criados pelo 'ops_agent' neste dia específico
    existing_ops_events = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            singleEvents=True,
            privateExtendedProperty="ops_owner=ops_agent",
        )
        .execute()
    )
    
    cleaned = 0
    for ev in existing_ops_events.get("items", []):
        try:
            service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
            cleaned += 1
        except Exception:
            pass  # Ignora se já foi deletado ou erro menor

    # --- 2. INSERÇÃO (Fresh Start) ---
    created = 0
    skipped_no_time = 0

    for t in tasks:
        title = getattr(t, "title", "").strip()
        if not title:
            continue

        tr = _extract_time_range(title)
        if not tr:
            skipped_no_time += 1
            continue

        start_hhmm, end_hhmm = tr
        start_dt = datetime.combine(day, _parse_hhmm(start_hhmm), tzinfo=tz)
        end_dt = datetime.combine(day, _parse_hhmm(end_hhmm), tzinfo=tz)

        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)

        clean_title = _strip_time_prefix(title)
        
        event_body = {
            "summary": clean_title,
            "description": f"Plano gerado pelo OPS_AGENT em {datetime.now().strftime('%H:%M')}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
            "extendedProperties": {
                "private": {
                    "ops_owner": "ops_agent",  # Marca registrada para podermos deletar depois
                }
            },
            # Cor diferenciada para o plano do agente (ex: cor 5 é amarela/banana)
            "colorId": "5"
        }

        service.events().insert(calendarId=calendar_id, body=event_body).execute()
        created += 1

    return {"status": "success", "created": created, "cleaned": cleaned}
def sync_ops_plan(raw_text: str, vault_dir):
    from ops_plan_parser import parse_ops_plan

    tz = ZoneInfo("America/Sao_Paulo")
    today = datetime.now(tz).date()

    tasks = parse_ops_plan(raw_text)
    
    # Criamos objetos simples para o sync
    class PseudoTask:
        def __init__(self, title):
            self.title = title

    pseudo_tasks = [PseudoTask(f"{t.start}–{t.end} — {t.title}") for t in tasks]

    return sync_tasks_to_gcal(
        tasks=pseudo_tasks,
        vault_dir=vault_dir,
        tz_name="America/Sao_Paulo",
    )
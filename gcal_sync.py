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

# Aceita 06:00–06:20, 06:00-06:20, etc.
TIME_RANGE_RE = re.compile(r"(?P<start>\d{1,2}:\d{2})\s*[–-]\s*(?P<end>\d{1,2}:\d{2})")
PREFIX_CLEAN_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*[–-]\s*\d{1,2}:\d{2}\s*[—-]\s*")

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
    prune_missing: bool = False,
) -> dict:
    """
    - Upsert por task.id usando extendedProperties.private:
      ops_owner=ops_agent e ops_task_id=<id>
    - Se prune_missing=True, remove do dia os eventos 'ops_agent' que não existirem mais na lista.
    """
    tz = ZoneInfo(tz_name)
    day = day or datetime.now(tz).date()
    day_start, day_end = _day_bounds(day, tz)

    service = _get_service(vault_dir)

    # IDs atuais para prune
    current_ids = {getattr(t, "id") for t in tasks}

    created = 0
    updated = 0
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

        # Se alguém botar 23:50-00:10, isso cruza dia; aqui você pode decidir a regra.
        if end_dt <= start_dt:
            # regra simples: empurra pro próximo dia
            end_dt = end_dt + timedelta(days=1)

        clean_title = _strip_time_prefix(title)
        prio = getattr(t, "priority", "P2")
        status = getattr(t, "status", "TODO")
        notes = getattr(t, "notes", "")

        event_body = {
            "summary": f"[{prio}] {clean_title}",
            "description": (
                f"OPS_AGENT task_id={getattr(t, 'id', '')}\n"
                f"status={status}\n\n"
                f"{notes}".strip()
            ),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
            "extendedProperties": {
                "private": {
                    "ops_owner": "ops_agent",
                    "ops_task_id": getattr(t, "id", ""),
                }
            },
        }

        # Busca evento do dia com esse task_id (privateExtendedProperty)
        existing = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                privateExtendedProperty=f"ops_task_id={getattr(t, 'id', '')}",
            )
            .execute()
        )
        items = existing.get("items", [])

        if items:
            event_id = items[0]["id"]
            service.events().patch(calendarId=calendar_id, eventId=event_id, body=event_body).execute()
            updated += 1
        else:
            service.events().insert(calendarId=calendar_id, body=event_body).execute()
            created += 1

    deleted = 0
    if prune_missing:
        # lista todos eventos do dia que pertencem ao ops_agent e deleta os que não estão mais em tasks
        all_ops = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=day_start.isoformat(),
                timeMax=day_end.isoformat(),
                singleEvents=True,
                privateExtendedProperty="ops_owner=ops_agent",
            )
            .execute()
            .get("items", [])
        )
        for ev in all_ops:
            priv = (ev.get("extendedProperties") or {}).get("private") or {}
            tid = priv.get("ops_task_id")
            if tid and tid not in current_ids:
                service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
                deleted += 1

    return {
        "day": day.isoformat(),
        "calendar_id": calendar_id,
        "created": created,
        "updated": updated,
        "deleted": deleted,
        "skipped_no_time": skipped_no_time,
    }
def sync_ops_plan(raw_text: str, vault_dir):
    from ops_plan_parser import parse_ops_plan

    tz = ZoneInfo("America/Sao_Paulo")
    today = datetime.now(tz).date()

    tasks = parse_ops_plan(raw_text)

    pseudo_tasks = []

    for i, t in enumerate(tasks):
        class X:
            pass

        x = X()
        x.id = f"ops_{today}_{i}"
        x.title = f"{t.start}–{t.end} — {t.title}"
        x.priority = t.priority
        x.status = "TODO"
        x.notes = ""

        pseudo_tasks.append(x)

    return sync_tasks_to_gcal(
        tasks=pseudo_tasks,
        vault_dir=vault_dir,
        tz_name="America/Sao_Paulo",
    )
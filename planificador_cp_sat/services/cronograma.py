"""Consulta de només lectura per al cronograma de serveis."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


VISIBLE_PROPOSAL_STATES = ("esborrany", "validada")
MAX_VISIBLE_DAYS = 31
REQUIRED_TABLES = {
    "assig_grup_T",
    "canvis_planificacio_cp_sat",
    "cobertura",
    "execucions_planificacio_cp_sat",
    "publicacions_planificacio_cp_sat",
    "treballadors",
}


class TimelineReadError(RuntimeError):
    """Indica que el cronograma no es pot construir de manera segura."""


def _read_connection(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path).resolve()
    if not path.is_file():
        raise TimelineReadError(f"No s'ha trobat la base de dades: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    found = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = REQUIRED_TABLES - found
    if missing:
        raise TimelineReadError(
            "La base no té l'esquema necessari per al cronograma: "
            + ", ".join(sorted(missing))
        )


def _as_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as error:
        raise TimelineReadError(f"{field} no és una data vàlida") from error


def _json_object(raw_value: object, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw_value or "{}"))
    except json.JSONDecodeError as error:
        raise TimelineReadError(f"{field} no conté JSON vàlid") from error
    if not isinstance(value, dict):
        raise TimelineReadError(f"{field} no conté un objecte JSON")
    return value


def _json_assignment(raw_value: object) -> dict[str, Any] | None:
    if not raw_value:
        return None
    return _json_object(raw_value, field="Assignació de la proposta")


def _time_label(value: object) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.fromisoformat(text).strftime("%H:%M")
    except ValueError:
        return text[:5]


def _date_in_intervals(day: date, intervals: Iterable[tuple[date, date]]) -> bool:
    return any(start <= day <= end for start, end in intervals)


def _published_intervals(connection: sqlite3.Connection) -> tuple[tuple[date, date], ...]:
    rows = connection.execute(
        """
        SELECT data_inici, data_fi
        FROM publicacions_planificacio_cp_sat
        WHERE reverted_at IS NULL
        ORDER BY data_inici, data_fi, id
        """
    ).fetchall()
    return tuple(
        (
            _as_date(row["data_inici"], field="Inici de publicació"),
            _as_date(row["data_fi"], field="Final de publicació"),
        )
        for row in rows
    )


def _proposal_rows(
    connection: sqlite3.Connection,
    official_end: date | None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in VISIBLE_PROPOSAL_STATES)
    proposals = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT id, estat, data_inici, data_fi, solver_status,
                   necessitats_cobertes, necessitats_totals,
                   necessitats_descobertes, created_at
            FROM execucions_planificacio_cp_sat
            WHERE estat IN ({placeholders})
            ORDER BY id DESC
            """,
            VISIBLE_PROPOSAL_STATES,
        )
    ]
    if official_end is None:
        return proposals
    return [
        proposal
        for proposal in proposals
        if _as_date(proposal["data_fi"], field="Final de proposta") > official_end
    ]


def load_timeline_options(database_path: str | Path) -> dict[str, Any]:
    """Carrega límits, filtres i propostes sense modificar SQLite."""
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        limits = connection.execute(
            "SELECT MIN(data) AS inici, MAX(data) AS fi FROM cobertura"
        ).fetchone()
        if limits is None or not limits["inici"] or not limits["fi"]:
            raise TimelineReadError("La base no conté necessitats de cobertura")
        intervals = _published_intervals(connection)
        official_end = max((end for _, end in intervals), default=None)
        if official_end is None:
            active_limit = connection.execute(
                """
                SELECT MAX(data) FROM assig_grup_T
                WHERE estat_planificacio IN ('publicada', 'bloquejada')
                """
            ).fetchone()[0]
            official_end = (
                _as_date(active_limit, field="Final del pla")
                if active_limit
                else None
            )
        workers = [
            {"id": str(row["id"]), "name": str(row["treballador"])}
            for row in connection.execute(
                "SELECT id, treballador FROM treballadors "
                "ORDER BY treballador COLLATE NOCASE, id"
            )
        ]
        return {
            "coverage_start": _as_date(limits["inici"], field="Inici de cobertura"),
            "coverage_end": _as_date(limits["fi"], field="Final de cobertura"),
            "official_end": official_end,
            "lines": [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT linia FROM cobertura
                    WHERE COALESCE(linia, '') <> '' ORDER BY linia COLLATE NOCASE
                    """
                )
            ],
            "zones": [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT zona FROM cobertura
                    WHERE COALESCE(zona, '') <> ''
                    ORDER BY zona COLLATE NOCASE
                    """
                )
            ],
            "services": [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT servei FROM cobertura "
                    "ORDER BY servei COLLATE NOCASE"
                )
            ],
            "workers": workers,
            "proposals": _proposal_rows(connection, official_end),
        }


def _base_cells(
    connection: sqlite3.Connection,
    start: date,
    end: date,
    published_intervals: tuple[tuple[date, date], ...],
) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT c.data, c.servei, c.linia, c.zona, c.formacio,
               a.id AS assignment_id, a.treballador_id AS worker_id,
               COALESCE(NULLIF(a.treballador_nom, ''), t.treballador,
                        CAST(a.treballador_id AS TEXT)) AS worker_name,
               a.hora_inici, a.hora_fi, a.durada_hores,
               a.estat_planificacio, a.es_canvi_zona, a.es_canvi_torn
        FROM cobertura AS c
        LEFT JOIN assig_grup_T AS a
          ON a.data = c.data AND a.torn = c.servei
         AND a.estat_planificacio IN ('publicada', 'bloquejada')
        LEFT JOIN treballadors AS t
          ON CAST(t.id AS TEXT) = CAST(a.treballador_id AS TEXT)
        WHERE c.data BETWEEN ? AND ?
        ORDER BY c.servei, c.data, a.id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    cells: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = _as_date(row["data"], field="Data de cobertura")
        key = f"{day.isoformat()}::{row['servei']}"
        if key in cells:
            raise TimelineReadError(f"Hi ha més d'una fila per a {key}")
        has_assignment = row["assignment_id"] is not None
        status = (
            "locked"
            if row["estat_planificacio"] == "bloquejada"
            else "published"
            if has_assignment
            else "uncovered"
            if _date_in_intervals(day, published_intervals)
            else "pending"
        )
        start_time = str(row["hora_inici"] or "")
        end_time = str(row["hora_fi"] or "")
        cells[key] = {
            "key": key,
            "date": day.isoformat(),
            "service_id": str(row["servei"]),
            "line": str(row["linia"] or ""),
            "zone": str(row["zona"] or ""),
            "skills": str(row["formacio"] or ""),
            "status": status,
            "assignment_id": row["assignment_id"],
            "worker_id": (
                str(row["worker_id"])
                if row["worker_id"] is not None
                else None
            ),
            "worker_name": str(row["worker_name"] or "") if has_assignment else "",
            "start_time": start_time,
            "end_time": end_time,
            "duration_hours": float(row["durada_hores"] or 0),
            "overnight": bool(start_time and end_time and end_time <= start_time),
            "zone_change": bool(row["es_canvi_zona"]),
            "turn_change": bool(row["es_canvi_torn"]),
            "incident_count": 0,
            "proposal_id": None,
            "proposal_state": None,
            "previous_worker_id": None,
            "previous_worker_name": "",
            "reason": "",
        }
    return cells


def _worker_names(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["id"]): str(row["treballador"])
        for row in connection.execute("SELECT id, treballador FROM treballadors")
    }


def _proposal_interval_contains(day: date, scope: dict[str, Any]) -> bool:
    return (
        _as_date(scope.get("start_date"), field="Inici de proposta")
        <= day
        <= _as_date(scope.get("end_date"), field="Final de proposta")
    )


def _apply_proposal(
    connection: sqlite3.Connection,
    cells: dict[str, dict[str, Any]],
    proposal_id: int,
) -> dict[str, Any]:
    proposal = connection.execute(
        """
        SELECT id, estat, data_inici, data_fi, abast_json, metriques_json
        FROM execucions_planificacio_cp_sat WHERE id = ?
        """,
        (proposal_id,),
    ).fetchone()
    if proposal is None:
        raise TimelineReadError(f"No existeix la proposta P-{proposal_id}")
    if proposal["estat"] not in VISIBLE_PROPOSAL_STATES:
        raise TimelineReadError(
            f"La proposta P-{proposal_id} està {proposal['estat']} i no es pot superposar"
        )
    scope = _json_object(proposal["abast_json"], field="Abast de la proposta")
    metrics = _json_object(
        proposal["metriques_json"], field="Mètriques de la proposta"
    )
    published = _published_intervals(connection)
    workers = _worker_names(connection)
    changes = connection.execute(
        """
        SELECT tipus, necessitat_id, data, servei, treballador_anterior_id,
               treballador_nou_id, anterior_json, posterior_json, motiu
        FROM canvis_planificacio_cp_sat
        WHERE execucio_id = ? ORDER BY ordre
        """,
        (proposal_id,),
    ).fetchall()
    visible_changes: list[tuple[sqlite3.Row, dict[str, Any]]] = []
    incompatibilities: list[str] = []
    for change in changes:
        key = str(change["necessitat_id"])
        cell = cells.get(key)
        if cell is None:
            continue
        day = _as_date(change["data"], field="Data del canvi")
        if _date_in_intervals(day, published):
            continue
        previous = _json_assignment(change["anterior_json"])
        current_worker = cell["worker_id"]
        expected_worker = str(previous["worker_id"]) if previous else None
        if current_worker != expected_worker:
            incompatibilities.append(key)
            continue
        visible_changes.append((change, cell))

    if incompatibilities:
        preview = ", ".join(incompatibilities[:3])
        suffix = "…" if len(incompatibilities) > 3 else ""
        return {
            "id": int(proposal["id"]),
            "state": str(proposal["estat"]),
            "applied": False,
            "message": (
                "La proposta no coincideix amb el pla oficial actual "
                f"({preview}{suffix}) i no s'ha superposat."
            ),
        }

    for change, cell in visible_changes:
        previous = _json_assignment(change["anterior_json"])
        proposed = _json_assignment(change["posterior_json"])
        cell["proposal_id"] = int(proposal["id"])
        cell["proposal_state"] = str(proposal["estat"])
        cell["previous_worker_id"] = (
            str(previous["worker_id"]) if previous else None
        )
        cell["previous_worker_name"] = (
            workers.get(str(previous["worker_id"]), str(previous["worker_id"]))
            if previous
            else ""
        )
        cell["reason"] = str(change["motiu"] or "")
        if proposed is None:
            cell.update(
                status="provisional_uncovered",
                assignment_id=None,
                worker_id=None,
                worker_name="",
                start_time="",
                end_time="",
                duration_hours=0.0,
                overnight=False,
                zone_change=False,
                turn_change=False,
            )
            continue
        start_value = str(proposed.get("start") or "")
        end_value = str(proposed.get("end") or "")
        worker_id = str(proposed["worker_id"])
        try:
            overnight = (
                datetime.fromisoformat(end_value).date()
                > datetime.fromisoformat(start_value).date()
            )
        except ValueError:
            overnight = False
        cell.update(
            status="provisional",
            assignment_id=None,
            worker_id=worker_id,
            worker_name=workers.get(worker_id, worker_id),
            start_time=_time_label(start_value),
            end_time=_time_label(end_value),
            duration_hours=float(proposed.get("duration_minutes") or 0) / 60,
            overnight=overnight,
            zone_change=False,
            turn_change=False,
        )

    for item in metrics.get("uncovered", []):
        if not isinstance(item, dict) or not item.get("need_id"):
            continue
        key = str(item["need_id"])
        cell = cells.get(key)
        if cell is None:
            continue
        day = _as_date(cell["date"], field="Data descoberta")
        if _date_in_intervals(day, published) or not _proposal_interval_contains(
            day, scope
        ):
            continue
        cell.update(
            status="provisional_uncovered",
            assignment_id=None,
            worker_id=None,
            worker_name="",
            proposal_id=int(proposal["id"]),
            proposal_state=str(proposal["estat"]),
            reason=str(item.get("reason") or ""),
        )
    return {
        "id": int(proposal["id"]),
        "state": str(proposal["estat"]),
        "applied": True,
        "message": "",
    }


def _add_incident_counts(
    connection: sqlite3.Connection,
    cells: dict[str, dict[str, Any]],
    start: date,
    end: date,
) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "incidencies_personal" not in tables:
        return
    incidents = connection.execute(
        """
        SELECT CAST(treballador_id AS TEXT) AS worker_id,
               data_inici, data_fi
        FROM incidencies_personal
        WHERE data_inici <= ? AND COALESCE(data_fi, data_inici) >= ?
          AND estat NOT IN ('anul_lada', 'tancada')
        """,
        (end.isoformat(), start.isoformat()),
    ).fetchall()
    for cell in cells.values():
        if not cell["worker_id"]:
            continue
        day = _as_date(cell["date"], field="Data de la casella")
        cell["incident_count"] = sum(
            1
            for item in incidents
            if str(item["worker_id"]) == cell["worker_id"]
            and _as_date(item["data_inici"], field="Inici d'incidència")
            <= day
            <= _as_date(
                item["data_fi"] or item["data_inici"],
                field="Final d'incidència",
            )
        )


def _row_value(values: Iterable[str]) -> str:
    selected = sorted({value for value in values if value})
    if not selected:
        return ""
    return selected[0] if len(selected) == 1 else "Diverses"


def _rows_from_cells(cells: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for cell in cells.values():
        grouped.setdefault(cell["service_id"], []).append(cell)
    return [
        {
            "service_id": service_id,
            "line": _row_value(cell["line"] for cell in service_cells),
            "zone": _row_value(cell["zone"] for cell in service_cells),
            "cells": {
                cell["date"]: cell
                for cell in sorted(service_cells, key=lambda item: item["date"])
            },
        }
        for service_id, service_cells in sorted(grouped.items())
    ]


def _totals(cells: Iterable[dict[str, Any]]) -> dict[str, int]:
    statuses = (
        "published",
        "locked",
        "provisional",
        "uncovered",
        "provisional_uncovered",
        "pending",
    )
    result = {status: 0 for status in statuses}
    for cell in cells:
        result[cell["status"]] += 1
    result["coverage"] = sum(result.values())
    return result


def load_timeline(
    database_path: str | Path,
    start_date: date,
    end_date: date,
    *,
    proposal_id: int | None = None,
) -> dict[str, Any]:
    """Construeix el període visible sense efectuar cap operació d'escriptura."""
    start = _as_date(start_date, field="Data inicial")
    end = _as_date(end_date, field="Data final")
    if end < start:
        raise TimelineReadError("La data final no pot ser anterior a la inicial")
    if (end - start).days + 1 > MAX_VISIBLE_DAYS:
        raise TimelineReadError(
            f"El cronograma admet un màxim de {MAX_VISIBLE_DAYS} dies"
        )
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        intervals = _published_intervals(connection)
        cells = _base_cells(connection, start, end, intervals)
        proposal = (
            _apply_proposal(connection, cells, proposal_id)
            if proposal_id is not None
            else None
        )
        _add_incident_counts(connection, cells, start, end)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": [
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        ],
        "rows": _rows_from_cells(cells),
        "totals": _totals(cells.values()),
        "proposal": proposal,
    }


def filter_timeline(
    timeline: dict[str, Any],
    *,
    lines: Iterable[str] = (),
    zones: Iterable[str] = (),
    services: Iterable[str] = (),
    worker_ids: Iterable[str] = (),
    statuses: Iterable[str] = (),
    search: str = "",
) -> dict[str, Any]:
    """Filtra files conservant tots els dies de cada servei coincident."""
    selected_lines = set(lines)
    selected_zones = set(zones)
    selected_services = set(services)
    selected_workers = {str(value) for value in worker_ids}
    selected_statuses = set(statuses)
    query = search.strip().casefold()
    rows = []
    for row in timeline["rows"]:
        cells = tuple(row["cells"].values())
        if selected_lines and row["line"] not in selected_lines:
            continue
        if selected_zones and row["zone"] not in selected_zones:
            continue
        if selected_services and row["service_id"] not in selected_services:
            continue
        if selected_workers and not any(
            cell["worker_id"] in selected_workers for cell in cells
        ):
            continue
        if selected_statuses and not any(
            cell["status"] in selected_statuses for cell in cells
        ):
            continue
        searchable = " ".join(
            [row["service_id"], row["line"], row["zone"]]
            + [cell["worker_name"] for cell in cells]
        ).casefold()
        if query and query not in searchable:
            continue
        rows.append(row)
    visible_cells = [cell for row in rows for cell in row["cells"].values()]
    return {**timeline, "rows": rows, "totals": _totals(visible_cells)}


def find_timeline_cell(
    timeline: dict[str, Any],
    selected: object,
) -> dict[str, Any] | None:
    """Resol una selecció retornada pel component sense confiar en el client."""
    if not isinstance(selected, dict):
        return None
    day = str(selected.get("date") or "")
    service_id = str(selected.get("service_id") or "")
    for row in timeline["rows"]:
        if row["service_id"] == service_id:
            return row["cells"].get(day)
    return None

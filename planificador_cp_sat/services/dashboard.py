"""Resum operatiu de només lectura per al dashboard principal."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from planificador_cp_sat.services.pla_publicat import (
    PublishedPlanReadError,
    load_published_plan_summary,
)


ACTIVE_ASSIGNMENT_STATES = ("publicada", "bloquejada")
OPEN_INCIDENT_STATES = ("registrada", "en_proposta")
PENDING_PLANNING_STATES = ("esborrany", "validada")


def _read_connection(database_path: str | Path) -> sqlite3.Connection:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _as_date(value: object) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _reference_date(
    today: date,
    plan: dict[str, Any] | None,
    coverage_start: date | None,
    coverage_end: date | None,
) -> date | None:
    """Tria una data operativa i evita mostrar un fals resum buit."""
    if coverage_start is None or coverage_end is None:
        return None
    plan_start = _as_date(plan.get("start_date")) if plan else None
    plan_end = _as_date(plan.get("end_date")) if plan else None
    if plan_start and plan_end:
        return min(max(today, plan_start), plan_end)
    return min(max(today, coverage_start), coverage_end)


def _coverage_summary(
    connection: sqlite3.Connection,
    reference: date | None,
    coverage_end: date | None,
) -> dict[str, Any]:
    if reference is None:
        return {
            "coverage_needs": 0,
            "coverage_covered": 0,
            "uncovered_next_7": 0,
            "uncovered_by_date": [],
            "horizon_end": None,
        }

    reference_text = reference.isoformat()
    horizon_end = min(reference + timedelta(days=6), coverage_end or reference)
    active_states = ", ".join("?" for _ in ACTIVE_ASSIGNMENT_STATES)
    coverage = connection.execute(
        f"""
        SELECT COUNT(*) AS needs,
               SUM(CASE WHEN EXISTS (
                   SELECT 1 FROM assig_grup_T a
                   WHERE a.data = c.data
                     AND a.torn = c.servei
                     AND a.estat_planificacio IN ({active_states})
               ) THEN 1 ELSE 0 END) AS covered
        FROM cobertura c
        WHERE c.data = ?
        """,
        (*ACTIVE_ASSIGNMENT_STATES, reference_text),
    ).fetchone()
    uncovered_rows = connection.execute(
        f"""
        SELECT c.data, COUNT(*) AS uncovered
        FROM cobertura c
        WHERE c.data BETWEEN ? AND ?
          AND NOT EXISTS (
              SELECT 1 FROM assig_grup_T a
              WHERE a.data = c.data
                AND a.torn = c.servei
                AND a.estat_planificacio IN ({active_states})
          )
        GROUP BY c.data
        ORDER BY c.data
        """,
        (
            reference_text,
            horizon_end.isoformat(),
            *ACTIVE_ASSIGNMENT_STATES,
        ),
    ).fetchall()
    uncovered_by_date = [
        {"date": str(row["data"]), "count": int(row["uncovered"])}
        for row in uncovered_rows
    ]
    return {
        "coverage_needs": int(coverage["needs"] or 0),
        "coverage_covered": int(coverage["covered"] or 0),
        "uncovered_next_7": sum(item["count"] for item in uncovered_by_date),
        "uncovered_by_date": uncovered_by_date,
        "horizon_end": horizon_end.isoformat(),
    }


def _pending_summary(
    connection: sqlite3.Connection,
    available_tables: set[str],
) -> dict[str, int]:
    open_incidents = 0
    pending_planning = 0
    pending_repairs = 0
    if "incidencies_personal" in available_tables:
        placeholders = ", ".join("?" for _ in OPEN_INCIDENT_STATES)
        open_incidents = int(
            connection.execute(
                f"SELECT COUNT(*) FROM incidencies_personal "
                f"WHERE estat IN ({placeholders})",
                OPEN_INCIDENT_STATES,
            ).fetchone()[0]
        )
    if "execucions_planificacio_cp_sat" in available_tables:
        placeholders = ", ".join("?" for _ in PENDING_PLANNING_STATES)
        pending_planning = int(
            connection.execute(
                f"""
                SELECT COUNT(*) FROM execucions_planificacio_cp_sat
                WHERE estat IN ({placeholders}) AND origen <> 'incidencia'
                """,
                PENDING_PLANNING_STATES,
            ).fetchone()[0]
        )
    if "propostes_replanificacio" in available_tables:
        pending_repairs = int(
            connection.execute(
                "SELECT COUNT(*) FROM propostes_replanificacio "
                "WHERE estat = 'esborrany'"
            ).fetchone()[0]
        )
    return {
        "open_incidents": open_incidents,
        "pending_planning": pending_planning,
        "pending_repairs": pending_repairs,
        "pending_total": pending_planning + pending_repairs,
    }


def _ending_leaves(
    connection: sqlite3.Connection,
    available_tables: set[str],
    reference: date | None,
) -> list[dict[str, Any]]:
    if reference is None or not {"descansos_dies", "treballadors"}.issubset(
        available_tables
    ):
        return []
    limit = reference + timedelta(days=7)
    rows = connection.execute(
        """
        WITH latest_leave AS (
            SELECT treballador_id, MAX(data) AS end_date, MAX(motiu) AS reason
            FROM descansos_dies
            WHERE origen = 'baixa'
            GROUP BY treballador_id
        )
        SELECT t.id, t.treballador, l.end_date, l.reason
        FROM latest_leave l
        JOIN treballadors t ON t.id = l.treballador_id
        WHERE l.end_date BETWEEN ? AND ?
        ORDER BY l.end_date, t.treballador
        """,
        (reference.isoformat(), limit.isoformat()),
    ).fetchall()
    return [dict(row) for row in rows]


def _recent_activity(
    connection: sqlite3.Connection,
    available_tables: set[str],
) -> list[dict[str, str]]:
    activity: list[dict[str, str]] = []
    if "versions_pla_publicat" in available_tables:
        for row in connection.execute(
            """
            SELECT versio, tipus_event, created_at
            FROM versions_pla_publicat ORDER BY created_at DESC, versio DESC
            LIMIT 5
            """
        ):
            activity.append(
                {
                    "created_at": str(row["created_at"]),
                    "label": f"Versió oficial V{row['versio']} · {row['tipus_event']}",
                }
            )
    if "incidencies_personal" in available_tables:
        for row in connection.execute(
            """
            SELECT id, tipus, estat, created_at
            FROM incidencies_personal ORDER BY created_at DESC, id DESC LIMIT 5
            """
        ):
            activity.append(
                {
                    "created_at": str(row["created_at"]),
                    "label": (
                        f"Incidència #{row['id']} · {row['tipus']} "
                        f"· {row['estat']}"
                    ),
                }
            )
    if "execucions_planificacio_cp_sat" in available_tables:
        for row in connection.execute(
            """
            SELECT id, estat, created_at
            FROM execucions_planificacio_cp_sat
            WHERE origen <> 'incidencia'
            ORDER BY created_at DESC, id DESC LIMIT 5
            """
        ):
            activity.append(
                {
                    "created_at": str(row["created_at"]),
                    "label": f"Proposta P-{row['id']} · {row['estat']}",
                }
            )
    if "propostes_replanificacio" in available_tables:
        for row in connection.execute(
            """
            SELECT id, estat, created_at
            FROM propostes_replanificacio
            ORDER BY created_at DESC, id DESC LIMIT 5
            """
        ):
            activity.append(
                {
                    "created_at": str(row["created_at"]),
                    "label": f"Reparació R-{row['id']} · {row['estat']}",
                }
            )
    activity.sort(key=lambda item: item["created_at"], reverse=True)
    return activity[:5]


def load_dashboard_summary(
    database_path: str | Path,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Agrega els indicadors accionables sense alterar cap dada."""
    try:
        plan = load_published_plan_summary(database_path)
    except PublishedPlanReadError:
        plan = None

    with closing(_read_connection(database_path)) as connection:
        available_tables = _tables(connection)
        coverage_start = coverage_end = None
        if {"cobertura", "assig_grup_T"}.issubset(available_tables):
            limits = connection.execute(
                "SELECT MIN(data), MAX(data) FROM cobertura"
            ).fetchone()
            coverage_start = _as_date(limits[0])
            coverage_end = _as_date(limits[1])
        reference = _reference_date(
            today or date.today(), plan, coverage_start, coverage_end
        )
        coverage = (
            _coverage_summary(connection, reference, coverage_end)
            if {"cobertura", "assig_grup_T"}.issubset(available_tables)
            else _coverage_summary(connection, None, None)
        )
        return {
            "reference_date": reference.isoformat() if reference else None,
            "coverage_start": (
                coverage_start.isoformat() if coverage_start else None
            ),
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "plan": plan,
            **coverage,
            **_pending_summary(connection, available_tables),
            "ending_leaves": _ending_leaves(
                connection, available_tables, reference
            ),
            "recent_activity": _recent_activity(connection, available_tables),
        }

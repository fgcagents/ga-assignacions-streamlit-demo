"""Consulta de només lectura del pla oficial publicat."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


ACTIVE_STATES = ("publicada", "bloquejada")


class PublishedPlanReadError(RuntimeError):
    """Indica que el pla oficial no es pot consultar de manera segura."""


@dataclass(frozen=True, slots=True)
class PublishedPlanFilters:
    start_date: date | None = None
    end_date: date | None = None
    worker_ids: tuple[str, ...] = ()
    lines: tuple[str, ...] = ()
    services: tuple[str, ...] = ()


def _read_connection(database_path: str | Path) -> sqlite3.Connection:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {"assig_grup_T", "versions_pla_publicat"}
    found = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = required - found
    if missing:
        raise PublishedPlanReadError(
            "Falta preparar l'esquema del pla publicat: "
            + ", ".join(sorted(missing))
        )


def load_published_plan_summary(database_path: str | Path) -> dict:
    """Retorna la capçalera de la versió oficial vigent."""
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        version = connection.execute(
            """
            SELECT versio, tipus_event, origen, origen_id, data_inici,
                   data_fi, assignacions_actives, created_at
            FROM versions_pla_publicat
            ORDER BY versio DESC LIMIT 1
            """
        ).fetchone()
        totals = connection.execute(
            """
            SELECT SUM(CASE
                           WHEN estat_planificacio IN ('publicada', 'bloquejada')
                           THEN 1 ELSE 0
                       END) AS assignments,
                   SUM(CASE WHEN estat_planificacio = 'bloquejada'
                            THEN 1 ELSE 0 END) AS blocked,
                   SUM(CASE WHEN estat_planificacio = 'anul_lada'
                            THEN 1 ELSE 0 END) AS cancelled,
                   COUNT(DISTINCT CASE
                       WHEN estat_planificacio IN ('publicada', 'bloquejada')
                       THEN CAST(treballador_id AS TEXT)
                   END) AS workers,
                   COUNT(DISTINCT CASE
                       WHEN estat_planificacio IN ('publicada', 'bloquejada')
                       THEN torn
                   END) AS services,
                   MIN(CASE
                       WHEN estat_planificacio IN ('publicada', 'bloquejada')
                       THEN data
                   END) AS start_date,
                   MAX(CASE
                       WHEN estat_planificacio IN ('publicada', 'bloquejada')
                       THEN data
                   END) AS end_date
            FROM assig_grup_T
            """
        ).fetchone()
        if version is None:
            raise PublishedPlanReadError(
                "Encara no hi ha cap versió oficial registrada"
            )
        return {
            "version": int(version["versio"]),
            "event_type": str(version["tipus_event"]),
            "origin": str(version["origen"]),
            "origin_id": version["origen_id"],
            "published_at": str(version["created_at"]),
            "assignments": int(totals["assignments"] or 0),
            "blocked": int(totals["blocked"] or 0),
            "cancelled": int(totals["cancelled"] or 0),
            "workers": int(totals["workers"] or 0),
            "services": int(totals["services"] or 0),
            "start_date": totals["start_date"],
            "end_date": totals["end_date"],
        }


def load_published_plan_filter_options(database_path: str | Path) -> dict:
    """Carrega exclusivament opcions presents al pla oficial."""
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        workers = [
            (str(row["id"]), str(row["label"]))
            for row in connection.execute(
                """
                SELECT CAST(treballador_id AS TEXT) AS id,
                       MAX(COALESCE(NULLIF(treballador_nom, ''),
                           CAST(treballador_id AS TEXT))) AS label
                FROM assig_grup_T
                WHERE estat_planificacio IN ('publicada', 'bloquejada')
                GROUP BY CAST(treballador_id AS TEXT)
                ORDER BY label COLLATE NOCASE, id
                """
            )
        ]
        lines = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT linia FROM assig_grup_T
                WHERE estat_planificacio IN ('publicada', 'bloquejada')
                  AND COALESCE(linia, '') <> ''
                ORDER BY linia COLLATE NOCASE
                """
            )
        ]
        services = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT torn FROM assig_grup_T
                WHERE estat_planificacio IN ('publicada', 'bloquejada')
                  AND COALESCE(torn, '') <> ''
                ORDER BY torn COLLATE NOCASE
                """
            )
        ]
        return {"workers": workers, "lines": lines, "services": services}


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _ in values)


def list_published_assignments(
    database_path: str | Path,
    filters: PublishedPlanFilters | None = None,
) -> list[dict]:
    """Llista el pla vigent amb procedència, torn i avisos relacionats."""
    selected = filters or PublishedPlanFilters()
    conditions = [
        "a.estat_planificacio IN ('publicada', 'bloquejada')"
    ]
    parameters: list[object] = []
    if selected.start_date:
        conditions.append("a.data >= ?")
        parameters.append(selected.start_date.isoformat())
    if selected.end_date:
        conditions.append("a.data <= ?")
        parameters.append(selected.end_date.isoformat())
    for column, values in (
        ("CAST(a.treballador_id AS TEXT)", selected.worker_ids),
        ("a.linia", selected.lines),
        ("a.torn", selected.services),
    ):
        if values:
            conditions.append(f"{column} IN ({_placeholders(values)})")
            parameters.extend(values)
    where = " AND ".join(conditions)
    query = f"""
        WITH cobertura_unica AS (
            SELECT data, servei, MAX(torn) AS torn_operatiu,
                   MAX(linia) AS linia, MAX(zona) AS zona
            FROM cobertura
            GROUP BY data, servei
        ), origen_assignacio AS (
            SELECT c.assignacio_nova_id AS assignacio_id,
                   e.id AS execucio_id, e.origen, e.origen_id,
                   e.published_at,
                   v.versio
            FROM canvis_planificacio_cp_sat c
            JOIN execucions_planificacio_cp_sat e ON e.id = c.execucio_id
            LEFT JOIN versions_pla_publicat v
              ON v.execucio_id = e.id AND v.tipus_event = 'publicacio'
            WHERE c.assignacio_nova_id IS NOT NULL
        )
        SELECT a.id, a.data,
               CAST(a.treballador_id AS TEXT) AS worker_id,
               COALESCE(NULLIF(a.treballador_nom, ''),
                        CAST(a.treballador_id AS TEXT)) AS worker_name,
               a.torn AS service, a.hora_inici AS start_time,
               a.hora_fi AS end_time,
               COALESCE(NULLIF(a.linia, ''), cu.linia, '') AS line,
               COALESCE(NULLIF(a.zona, ''), cu.zona, '') AS zone,
               COALESCE(cu.torn_operatiu, '') AS shift,
               a.estat_planificacio AS assignment_state,
               COALESCE(oa.published_at, a.created_at) AS published_at,
               COALESCE(oa.versio, 1) AS plan_version,
               oa.execucio_id, oa.origen, oa.origen_id,
               (SELECT COUNT(*) FROM incidencies_personal i
                WHERE CAST(i.treballador_id AS TEXT) =
                      CAST(a.treballador_id AS TEXT)
                  AND a.data BETWEEN i.data_inici
                      AND COALESCE(i.data_fi, i.data_inici)) AS incident_count,
               (SELECT COUNT(*) FROM canvis_planificacio_cp_sat c2
                WHERE c2.necessitat_id = a.data || '::' || a.torn
               ) AS change_count
        FROM assig_grup_T a
        LEFT JOIN cobertura_unica cu
          ON cu.data = a.data AND cu.servei = a.torn
        LEFT JOIN origen_assignacio oa ON oa.assignacio_id = a.id
        WHERE {where}
        ORDER BY a.data, a.hora_inici, a.torn, a.treballador_nom, a.id
    """
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        try:
            return [dict(row) for row in connection.execute(query, parameters)]
        except sqlite3.Error as error:
            raise PublishedPlanReadError(
                f"No s'ha pogut consultar el pla publicat: {error}"
            ) from error


def load_published_assignment_detail(
    database_path: str | Path,
    assignment_id: int,
) -> dict:
    """Retorna incidències, bloquejos i canvis d'una assignació vigent."""
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        assignment = connection.execute(
            """
            SELECT id, data, torn AS service,
                   CAST(treballador_id AS TEXT) AS worker_id,
                   treballador_nom AS worker_name, estat_planificacio,
                   created_at
            FROM assig_grup_T
            WHERE id = ?
              AND estat_planificacio IN ('publicada', 'bloquejada')
            """,
            (assignment_id,),
        ).fetchone()
        if assignment is None:
            raise PublishedPlanReadError(
                "L'assignació seleccionada ja no forma part del pla oficial"
            )
        incidents = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, tipus, estat, data_comunicacio, data_inici,
                       data_fi, motiu
                FROM incidencies_personal
                WHERE CAST(treballador_id AS TEXT) = ?
                  AND ? BETWEEN data_inici AND COALESCE(data_fi, data_inici)
                ORDER BY data_comunicacio DESC, id DESC
                """,
                (assignment["worker_id"], assignment["data"]),
            )
        ]
        changes = [
            dict(row)
            for row in connection.execute(
                """
                SELECT c.id, c.tipus, c.motiu, c.treballador_anterior_id,
                       c.treballador_nou_id, e.id AS execution_id,
                       e.origen, e.origen_id, e.estat, e.created_at,
                       e.published_at, e.reverted_at,
                       vp.versio AS publication_version,
                       vr.versio AS rollback_version
                FROM canvis_planificacio_cp_sat c
                JOIN execucions_planificacio_cp_sat e
                  ON e.id = c.execucio_id
                LEFT JOIN versions_pla_publicat vp
                  ON vp.execucio_id = e.id
                 AND vp.tipus_event = 'publicacio'
                LEFT JOIN versions_pla_publicat vr
                  ON vr.execucio_id = e.id
                 AND vr.tipus_event = 'rollback'
                WHERE c.necessitat_id = ? || '::' || ?
                ORDER BY c.created_at DESC, c.id DESC
                """,
                (assignment["data"], assignment["service"]),
            )
        ]
        locks = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, origen, origen_id, motiu, vigent_des_de,
                       vigent_fins, estat, created_at
                FROM bloquejos_planificacio
                WHERE (assignacio_id = ? OR necessitat_id = ? || '::' || ?)
                ORDER BY created_at DESC, id DESC
                """,
                (assignment_id, assignment["data"], assignment["service"]),
            )
        ]
        return {
            "assignment": dict(assignment),
            "incidents": incidents,
            "changes": changes,
            "locks": locks,
        }


def list_published_plan_versions(
    database_path: str | Path,
    *,
    limit: int = 20,
) -> list[dict]:
    """Llista les versions oficials més recents per a traçabilitat."""
    with closing(_read_connection(database_path)) as connection:
        _require_schema(connection)
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT versio, tipus_event, origen, origen_id,
                       data_inici, data_fi, assignacions_actives, created_at
                FROM versions_pla_publicat
                ORDER BY versio DESC LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            )
        ]

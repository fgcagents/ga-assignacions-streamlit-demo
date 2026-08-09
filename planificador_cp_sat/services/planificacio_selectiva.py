"""Preassignacions obligatòries creades abans d'executar el planificador."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Iterable

from cp_sat_pilot import Assignment, CpSatPlanner
from cp_sat_pilot.sqlite_adapter import SqliteInputError, load_problem_from_sqlite

from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
)


class SelectivePlanningError(ValueError):
    """Indica que una preassignació no es pot desar."""


def load_selective_planning_options(database_path: str | Path) -> dict:
    """Carrega treballadors, cobertura datada i preassignacions actives."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        workers = [
            (str(row["id"]), str(row["treballador"]))
            for row in connection.execute(
                """
                SELECT id, treballador FROM treballadors
                WHERE grup = 'T'
                ORDER BY treballador COLLATE NOCASE, id
                """
            )
        ]
        needs = [
            {
                "need_id": f"{row['data']}::{row['servei']}",
                "date": str(row["data"]),
                "service_id": str(row["servei"]),
            }
            for row in connection.execute(
                """
                SELECT DISTINCT data, servei FROM cobertura
                WHERE COALESCE(data, '') <> ''
                  AND COALESCE(servei, '') <> ''
                ORDER BY data, servei COLLATE NOCASE
                """
            )
        ]
        preassignments = [
            dict(row)
            for row in connection.execute(
                """
                SELECT p.id, p.necessitat_id, p.data, p.servei,
                       p.treballador_id,
                       COALESCE(t.treballador, p.treballador_id)
                           AS treballador,
                       p.motiu, p.created_at
                FROM preassignacions_planificacio p
                LEFT JOIN treballadors t
                  ON CAST(t.id AS TEXT) = p.treballador_id
                WHERE p.estat = 'activa'
                ORDER BY p.data, p.servei, p.id
                """
            )
        ]
    return {
        "workers": workers,
        "needs": needs,
        "preassignments": preassignments,
    }


def _normalize_needs(need_ids: Iterable[object]) -> tuple[str, ...]:
    needs = tuple(
        dict.fromkeys(str(item).strip() for item in need_ids if str(item).strip())
    )
    if not needs:
        raise SelectivePlanningError(
            "Cal seleccionar almenys una combinació de servei i dia"
        )
    if any("::" not in need_id for need_id in needs):
        raise SelectivePlanningError("Hi ha una combinació de servei i dia invàlida")
    return needs


def _required_assignment_objects(problem: object) -> tuple[Assignment, ...]:
    needs = {need.id: need for need in problem.needs}
    required_by_need = dict(problem.required_assignments)
    assignments = [
        Assignment(
            worker_id=worker_id,
            need_id=need_id,
            service_id=needs[need_id].service_id,
            date=needs[need_id].date,
            start=needs[need_id].start,
            end=needs[need_id].end,
            duration_minutes=needs[need_id].duration_minutes,
        )
        for need_id, worker_id in problem.required_assignments
    ]
    assignments.extend(
        assignment
        for assignment in problem.reference_assignments
        if assignment.need_id in problem.locked_need_ids
        and assignment.need_id not in required_by_need
    )
    return tuple(assignments)


def preview_preassignments(
    database_path: str | Path,
    *,
    start_date: date,
    end_date: date,
    worker_id: object,
    need_ids: Iterable[object],
    reason: str = "Assignació fixada abans de planificar",
) -> dict:
    """Valida preassignacions de cobertura sense modificar la base."""
    if start_date > end_date:
        raise SelectivePlanningError(
            "La data d'inici no pot ser posterior a la data final"
        )
    normalized_worker = str(worker_id or "").strip()
    if not normalized_worker:
        raise SelectivePlanningError("Cal seleccionar un treballador")
    selected_need_ids = _normalize_needs(need_ids)
    selected_set = set(selected_need_ids)
    options = load_selective_planning_options(database_path)
    worker_names = dict(options["workers"])
    if normalized_worker not in worker_names:
        raise SelectivePlanningError(
            "El treballador seleccionat no pertany al grup T"
        )
    available = {
        item["need_id"]: item
        for item in options["needs"]
        if start_date.isoformat() <= item["date"] <= end_date.isoformat()
    }
    missing = selected_set - set(available)
    if missing:
        raise SelectivePlanningError(
            "Hi ha serveis i dies que no consten a cobertura dins l'interval: "
            + ", ".join(sorted(missing))
        )
    existing = {
        item["necessitat_id"]: item for item in options["preassignments"]
    }
    duplicated = selected_set & set(existing)
    if duplicated:
        raise SelectivePlanningError(
            "Ja existeix una preassignació per a: "
            + ", ".join(sorted(duplicated))
        )

    with closing(sqlite3.connect(database_path)) as connection:
        already_planned = selected_set & {
            f"{row[0]}::{row[1]}"
            for row in connection.execute(
                """
                SELECT data, torn FROM assig_grup_T
                WHERE estat_planificacio IN ('publicada', 'bloquejada')
                  AND data BETWEEN ? AND ?
                """,
                (start_date.isoformat(), end_date.isoformat()),
            )
        }
    if already_planned:
        raise SelectivePlanningError(
            "Aquestes necessitats ja formen part del pla publicat; utilitza "
            "Substitució per canviar-ne el treballador: "
            + ", ".join(sorted(already_planned))
        )
    try:
        base_problem = load_problem_from_sqlite(
            database_path,
            start_date=start_date,
            end_date=end_date,
            duplicate_policy="replace_all",
        )
    except SqliteInputError as error:
        raise SelectivePlanningError(str(error)) from error
    known_need_ids = {need.id for need in base_problem.needs}
    required = {
        item["necessitat_id"]: str(item["treballador_id"])
        for item in options["preassignments"]
        if item["necessitat_id"] in known_need_ids
    }
    required.update(
        (need_id, normalized_worker) for need_id in selected_need_ids
    )
    problem = replace(
        base_problem,
        required_assignments=tuple(sorted(required.items())),
    )
    errors = CpSatPlanner(problem).validate(
        _required_assignment_objects(problem)
    )
    if errors:
        raise SelectivePlanningError(
            "Les preassignacions no compleixen les restriccions: "
            + "; ".join(errors[:5])
        )
    rows = [
        {
            "necessitat_id": need_id,
            "data": available[need_id]["date"],
            "servei": available[need_id]["service_id"],
            "treballador_id": normalized_worker,
            "treballador": worker_names[normalized_worker],
        }
        for need_id in selected_need_ids
    ]
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "worker_id": normalized_worker,
        "worker_name": worker_names[normalized_worker],
        "need_ids": selected_need_ids,
        "reason": reason.strip() or "Assignació fixada abans de planificar",
        "assignments": rows,
    }


def save_preassignments(
    database_path: str | Path,
    *,
    start_date: date,
    end_date: date,
    worker_id: object,
    need_ids: Iterable[object],
    reason: str = "Assignació fixada abans de planificar",
) -> dict:
    """Desa les preassignacions validades sense publicar cap pla."""
    preview = preview_preassignments(
        database_path,
        start_date=start_date,
        end_date=end_date,
        worker_id=worker_id,
        need_ids=need_ids,
        reason=reason,
    )
    with closing(sqlite3.connect(database_path)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            for row in preview["assignments"]:
                connection.execute(
                    """
                    INSERT INTO preassignacions_planificacio
                    (necessitat_id, data, servei, treballador_id, motiu)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        row["necessitat_id"],
                        row["data"],
                        row["servei"],
                        row["treballador_id"],
                        preview["reason"],
                    ),
                )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise SelectivePlanningError(
                "Alguna necessitat ja té una preassignació activa"
            ) from error
        except Exception:
            connection.rollback()
            raise
    return {**preview, "saved_count": len(preview["assignments"])}


def revoke_preassignment(
    database_path: str | Path,
    preassignment_id: int,
) -> None:
    """Retira una preassignació abans d'una planificació posterior."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection, connection:
        updated = connection.execute(
            """
            UPDATE preassignacions_planificacio
            SET estat = 'revocada', deactivated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat = 'activa'
            """,
            (preassignment_id,),
        )
        if updated.rowcount != 1:
            raise SelectivePlanningError(
                "La preassignació ja no està activa o no existeix"
            )

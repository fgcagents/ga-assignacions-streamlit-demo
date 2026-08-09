"""Consulta d'auditoria i rollback diferencial exacte."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
    register_published_plan_version,
)
from planificador_cp_sat.services.persistencia_planificacio import (
    PlanningExecutionPersistenceError,
    PlanningExecutionStaleError,
    _load_with_connection,
)
from planificador_cp_sat.services.publicacio_planificacio import (
    _affected_hash,
    _operational_hash,
)


FailureInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PlanningPublicationAudit:
    publication_id: int
    execution_id: int
    origin: str
    origin_id: str | None
    start_date: str
    end_date: str
    previous_snapshot_hash: str
    published_snapshot_hash: str
    affected_previous_hash: str
    affected_published_hash: str
    summary: dict
    backup_path: str
    created_at: str
    reverted_at: str | None


def _audit_from_row(row: sqlite3.Row) -> PlanningPublicationAudit:
    return PlanningPublicationAudit(
        publication_id=int(row["publication_id"]),
        execution_id=int(row["execution_id"]),
        origin=str(row["origen"]),
        origin_id=row["origen_id"],
        start_date=str(row["data_inici"]),
        end_date=str(row["data_fi"]),
        previous_snapshot_hash=str(row["snapshot_anterior_hash"]),
        published_snapshot_hash=str(row["snapshot_posterior_hash"]),
        affected_previous_hash=str(row["affected_anterior_hash"]),
        affected_published_hash=str(row["affected_posterior_hash"]),
        summary=json.loads(row["resum_json"]),
        backup_path=str(row["backup_path"]),
        created_at=str(row["created_at"]),
        reverted_at=row["reverted_at"],
    )


def load_planning_publication_audit(
    database_path: str | Path,
    execution_id: int,
) -> PlanningPublicationAudit:
    migrate_planning_schema(database_path)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT p.id AS publication_id, p.execucio_id AS execution_id,
                   p.snapshot_anterior_hash, p.snapshot_posterior_hash,
                   p.affected_anterior_hash, p.affected_posterior_hash,
                   p.resum_json, p.backup_path, p.created_at, p.reverted_at,
                   e.origen, e.origen_id, e.data_inici, e.data_fi
            FROM publicacions_planificacio_cp_sat p
            JOIN execucions_planificacio_cp_sat e ON e.id = p.execucio_id
            WHERE p.execucio_id = ?
            """,
            (execution_id,),
        ).fetchone()
        if row is None:
            raise PlanningExecutionPersistenceError(
                f"L'execució {execution_id} no té cap publicació"
            )
        return _audit_from_row(row)
    finally:
        connection.close()


def _restore_assignment(
    connection: sqlite3.Connection,
    payload: dict,
) -> None:
    assignment_id = int(payload["id"])
    values = {key: value for key, value in payload.items() if key != "id"}
    setters = ", ".join(f'"{key}" = ?' for key in values)
    updated = connection.execute(
        f'UPDATE assig_grup_T SET {setters} WHERE id = ?',
        (*values.values(), assignment_id),
    )
    if updated.rowcount != 1:
        raise PlanningExecutionStaleError(
            f"No es pot restaurar l'assignació anterior #{assignment_id}"
        )


def _restore_history(
    connection: sqlite3.Connection,
    payload: dict,
) -> None:
    rowid = int(payload["_rowid"])
    values = {key: value for key, value in payload.items() if key != "_rowid"}
    columns = ", ".join(["rowid", *(f'"{key}"' for key in values)])
    placeholders = ", ".join("?" for _ in range(len(values) + 1))
    connection.execute(
        f"INSERT INTO historic_assignacions ({columns}) "
        f"VALUES ({placeholders})",
        (rowid, *values.values()),
    )


def rollback_planning_changeset(
    database_path: str | Path,
    execution_id: int,
    *,
    failure_injector: FailureInjector | None = None,
) -> dict:
    """Reverteix exclusivament els deltes si cap afectat ha canviat després."""
    migrate_planning_schema(database_path)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        stored = _load_with_connection(connection, execution_id)
        if stored.state != "publicada":
            raise PlanningExecutionPersistenceError(
                "Només es pot revertir una proposta publicada una sola vegada"
            )
        publication = connection.execute(
            """
            SELECT * FROM publicacions_planificacio_cp_sat
            WHERE execucio_id = ?
            """,
            (execution_id,),
        ).fetchone()
        if publication is None or publication["reverted_at"] is not None:
            raise PlanningExecutionPersistenceError(
                "La publicació no està disponible per revertir"
            )
        current_affected_hash = _affected_hash(connection, stored)
        if current_affected_hash != publication["affected_posterior_hash"]:
            raise PlanningExecutionStaleError(
                "Alguna necessitat afectada ha canviat després de la "
                "publicació; revisa el pla abans de revertir"
            )
        rollback = json.loads(publication["rollback_json"])

        for rowid in rollback["inserted_history_rowids"]:
            connection.execute(
                "DELETE FROM historic_assignacions WHERE rowid = ?",
                (rowid,),
            )
        for assignment_id in rollback["inserted_assignment_ids"]:
            connection.execute(
                """
                UPDATE bloquejos_planificacio
                SET estat = 'revocat', deactivated_at = CURRENT_TIMESTAMP
                WHERE assignacio_id = ? AND estat = 'actiu'
                """,
                (assignment_id,),
            )
            connection.execute(
                "DELETE FROM assig_grup_T WHERE id = ?",
                (assignment_id,),
            )
        if failure_injector:
            failure_injector("after_delete_insertions")

        for payload in rollback["previous_assignments"]:
            _restore_assignment(connection, payload)
        if failure_injector:
            failure_injector("after_restore_assignments")

        for payload in rollback["deleted_history"]:
            _restore_history(connection, payload)
        if failure_injector:
            failure_injector("after_restore_history")

        restored_affected_hash = _affected_hash(connection, stored)
        if restored_affected_hash != publication["affected_anterior_hash"]:
            raise PlanningExecutionPersistenceError(
                "El pla afectat no coincideix amb l'estat anterior; "
                "s'ha cancel·lat el rollback"
            )
        final_snapshot_hash = _operational_hash(connection, stored)
        connection.execute(
            """
            UPDATE publicacions_planificacio_cp_sat
            SET reverted_at = CURRENT_TIMESTAMP
            WHERE execucio_id = ? AND reverted_at IS NULL
            """,
            (execution_id,),
        )
        updated = connection.execute(
            """
            UPDATE execucions_planificacio_cp_sat
            SET estat = 'revertida', reverted_at = CURRENT_TIMESTAMP,
                snapshot_final_hash = ?
            WHERE id = ? AND estat = 'publicada'
            """,
            (final_snapshot_hash, execution_id),
        )
        if updated.rowcount != 1:
            raise PlanningExecutionPersistenceError(
                "L'execució ha canviat d'estat durant el rollback"
            )
        rollback_version = register_published_plan_version(
            connection,
            event_type="rollback",
            execution_id=execution_id,
            publication_id=int(publication["id"]),
            origin=stored.request.trigger.kind.value,
            origin_id=stored.request.trigger.source_id,
            start_date=stored.request.scope.start_date.isoformat(),
            end_date=stored.request.scope.end_date.isoformat(),
        )
        connection.commit()
        return {
            "execution_id": execution_id,
            "plan_version": rollback_version,
            "state": "revertida",
            "restored_snapshot_hash": final_snapshot_hash,
            "restored_assignments": len(rollback["previous_assignments"]),
            "removed_assignments": len(rollback["inserted_assignment_ids"]),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

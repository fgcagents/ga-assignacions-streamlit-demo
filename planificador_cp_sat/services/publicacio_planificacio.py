"""Publicació diferencial, atòmica i reversible de planificació."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from cp_sat_pilot import Assignment, PlannerCore

from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
    register_published_plan_version,
)
from planificador_cp_sat.services.persistencia_planificacio import (
    PlanningExecutionPersistenceError,
    PlanningExecutionStaleError,
    StoredPlanningExecution,
    _load_with_connection,
    _reconstruct_final_assignments,
    planning_problem_hash,
)
from planificador_cp_sat.services.preparacio_planificacio import (
    prepare_planning_problem,
)
from planificador_cp_sat.solver_engines import (
    PUBLISHABLE_SOLVER_STATUSES,
    SolverEngine,
    normalize_solver_engine,
)
from planificador_cp_sat.services.proposta_planificacio import (
    PlanningChangeKind,
)


FailureInjector = Callable[[str], None]
PublicationHook = Callable[
    [sqlite3.Connection, StoredPlanningExecution, dict[int, int]],
    dict[str, Any] | None,
]


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _operational_hash(
    connection: sqlite3.Connection,
    execution: StoredPlanningExecution,
) -> str:
    start = execution.request.scope.start_date.isoformat()
    end = execution.request.scope.end_date.isoformat()
    assignments = [
        _row_dict(row)
        for row in connection.execute(
            """
            SELECT * FROM assig_grup_T
            WHERE data BETWEEN ? AND ?
            ORDER BY id
            """,
            (start, end),
        )
    ]
    history = [
        _row_dict(row)
        for row in connection.execute(
            """
            SELECT rowid AS _rowid, * FROM historic_assignacions
            WHERE data BETWEEN ? AND ?
            ORDER BY rowid
            """,
            (start, end),
        )
    ]
    encoded = _json_dumps(
        {"assignments": assignments, "history": history}
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _affected_hash(
    connection: sqlite3.Connection,
    execution: StoredPlanningExecution,
    changes: tuple | None = None,
) -> str:
    selected_changes = execution.changes if changes is None else changes
    need_ids = {item.need_id for item in selected_changes}
    assignments = [
        _row_dict(row)
        for row in connection.execute("SELECT * FROM assig_grup_T ORDER BY id")
        if f"{row['data']}::{row['torn']}" in need_ids
    ]
    history = [
        _row_dict(row)
        for row in connection.execute(
            "SELECT rowid AS _rowid, * FROM historic_assignacions ORDER BY rowid"
        )
        if f"{row['data']}::{row['torn_id']}" in need_ids
    ]
    return hashlib.sha256(
        _json_dumps(
            {"assignments": assignments, "history": history}
        ).encode("utf-8")
    ).hexdigest()


def _active_publications(
    connection: sqlite3.Connection,
    execution_id: int,
) -> tuple[sqlite3.Row, ...]:
    return tuple(
        connection.execute(
            """
            SELECT * FROM publicacions_planificacio_cp_sat
            WHERE execucio_id = ? AND reverted_at IS NULL
            ORDER BY data_inici, id
            """,
            (execution_id,),
        ).fetchall()
    )


def _publication_progress(
    execution: StoredPlanningExecution,
    publications: tuple[sqlite3.Row, ...],
) -> dict[str, Any]:
    scope_start = execution.request.scope.start_date
    scope_end = execution.request.scope.end_date
    expected_start = scope_start
    for publication in publications:
        segment_start = date.fromisoformat(publication["data_inici"])
        segment_end = date.fromisoformat(publication["data_fi"])
        if segment_start != expected_start or segment_end < segment_start:
            raise PlanningExecutionPersistenceError(
                "Els blocs publicats de la proposta no són consecutius"
            )
        expected_start = segment_end + timedelta(days=1)
    published_until = (
        date.fromisoformat(publications[-1]["data_fi"])
        if publications
        else None
    )
    complete = expected_start > scope_end
    return {
        "start_date": scope_start,
        "end_date": scope_end,
        "published_until": published_until,
        "next_start": None if complete else expected_start,
        "complete": complete,
        "publication_count": len(publications),
    }


def planning_publication_progress(
    database_path: str | Path,
    execution_id: int,
) -> dict[str, Any]:
    """Retorna el tram publicat i el següent inici obligatori."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        execution = _load_with_connection(connection, execution_id)
        publications = _active_publications(connection, execution_id)
        return _publication_progress(execution, publications)


def planning_publication_staleness(
    database_path: str | Path,
    execution_id: int,
) -> dict[str, Any]:
    """Detecta canvis oficials posteriors a l'últim bloc publicat."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        execution = _load_with_connection(connection, execution_id)
        publications = _active_publications(connection, execution_id)
        progress = _publication_progress(execution, publications)
        if not publications:
            return {"stale": False, "events": (), **progress}

        last_publication = publications[-1]
        stale = (
            _operational_hash(connection, execution)
            != last_publication["snapshot_posterior_hash"]
        )
        if not stale:
            return {"stale": False, "events": (), **progress}

        rows = connection.execute(
            """
            SELECT p.id AS publication_id, p.execucio_id, p.data_inici,
                   p.data_fi, e.origen, e.origen_id
            FROM publicacions_planificacio_cp_sat p
            JOIN execucions_planificacio_cp_sat e ON e.id = p.execucio_id
            WHERE p.id > ? AND p.execucio_id <> ?
              AND p.reverted_at IS NULL
              AND p.data_inici <= ? AND p.data_fi >= ?
            ORDER BY p.id
            """,
            (
                last_publication["id"],
                execution_id,
                execution.request.scope.end_date.isoformat(),
                execution.request.scope.start_date.isoformat(),
            ),
        ).fetchall()
        pending_start = progress["next_start"]
        events = tuple(
            {
                **dict(row),
                "overlaps_pending": bool(
                    pending_start
                    and date.fromisoformat(row["data_fi"]) >= pending_start
                ),
            }
            for row in rows
        )
        return {"stale": True, "events": events, **progress}


def _invalidate_stale_validated_executions(
    connection: sqlite3.Connection,
    *,
    exclude_execution_id: int | None = None,
) -> tuple[int, ...]:
    """Invalida propostes parcials si el pla ja no coincideix."""
    query = """
        SELECT DISTINCT e.id
        FROM execucions_planificacio_cp_sat e
        JOIN publicacions_planificacio_cp_sat p ON p.execucio_id = e.id
        WHERE e.estat = 'validada' AND p.reverted_at IS NULL
    """
    parameters: tuple[int, ...] = ()
    if exclude_execution_id is not None:
        query += " AND e.id <> ?"
        parameters = (exclude_execution_id,)
    invalidated = []
    for row in connection.execute(query, parameters).fetchall():
        execution_id = int(row[0])
        execution = _load_with_connection(connection, execution_id)
        publications = _active_publications(connection, execution_id)
        if (
            publications
            and _operational_hash(connection, execution)
            != publications[-1]["snapshot_posterior_hash"]
        ):
            updated = connection.execute(
                """
                UPDATE execucions_planificacio_cp_sat
                SET estat = 'invalidada'
                WHERE id = ? AND estat = 'validada'
                """,
                (execution_id,),
            )
            if updated.rowcount == 1:
                invalidated.append(execution_id)
    return tuple(invalidated)


def invalidate_stale_planning_executions(
    database_path: str | Path,
) -> tuple[int, ...]:
    """Actualitza l'estat formal de les propostes parcials obsoletes."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            invalidated = _invalidate_stale_validated_executions(connection)
            connection.commit()
            return invalidated
        except Exception:
            connection.rollback()
            raise


def _create_backup(
    database_path: str | Path,
    execution_id: int,
    backup_directory: str | Path | None,
) -> Path:
    directory = (
        Path(backup_directory).resolve()
        if backup_directory is not None
        else Path(database_path).resolve().parent / "backups" / "cp_sat"
    )
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    target = directory / (
        f"planificacio_abans_execucio_{execution_id}_{timestamp}.db"
    )
    with closing(sqlite3.connect(database_path)) as source, closing(
        sqlite3.connect(target)
    ) as destination:
        source.backup(destination)
    return target


def _active_row_for_need(
    connection: sqlite3.Connection,
    day: str,
    service_id: str,
) -> sqlite3.Row | None:
    rows = connection.execute(
        """
        SELECT * FROM assig_grup_T
        WHERE data = ? AND torn = ?
          AND estat_planificacio IN ('publicada', 'bloquejada')
        ORDER BY id
        """,
        (day, service_id),
    ).fetchall()
    if len(rows) > 1:
        raise PlanningExecutionStaleError(
            f"Hi ha més d'una assignació activa per {day}::{service_id}"
        )
    return rows[0] if rows else None


def _turn_change(
    worker_rotation: object,
    coverage_rotation: object,
    coverage_turn: object,
) -> int:
    def values(raw: object) -> set[str]:
        return {
            item.strip().lower().replace("í", "i")
            for item in str(raw or "")
            .replace("+", ",")
            .replace(";", ",")
            .split(",")
            if item.strip()
        }

    worker = values(worker_rotation)
    required = values(coverage_rotation or coverage_turn)
    return int(bool(worker and required and worker.isdisjoint(required)))


def _insert_assignment(
    connection: sqlite3.Connection,
    assignment: Assignment,
    source_row: sqlite3.Row | None = None,
) -> tuple[int, int]:
    worker = connection.execute(
        """
        SELECT id, treballador, plaza, rotacio, zona, grup
        FROM treballadors
        WHERE CAST(id AS TEXT) = CAST(? AS TEXT)
        """,
        (assignment.worker_id,),
    ).fetchone()
    if worker is None or worker["grup"] != "T":
        raise PlanningExecutionStaleError(
            f"Treballador proposat invàlid: {assignment.worker_id}"
        )
    coverage = connection.execute(
        """
        SELECT linia, zona, formacio, rotacio, torn
        FROM cobertura WHERE data = ? AND servei = ? LIMIT 1
        """,
        (assignment.date.isoformat(), assignment.service_id),
    ).fetchone()
    if coverage is None and source_row is None:
        raise PlanningExecutionStaleError(
            f"La necessitat {assignment.need_id} ja no existeix"
        )
    zone = (
        coverage["zona"] if coverage is not None else source_row["zona"]
    ) or ""
    zone_change = int(bool(worker["zona"] and zone and worker["zona"] != zone))
    turn_change = _turn_change(
        worker["rotacio"],
        coverage["rotacio"] if coverage is not None else None,
        coverage["torn"] if coverage is not None else assignment.service_id,
    )
    historic_hours = float(
        connection.execute(
            """
            SELECT COALESCE(SUM(durada_hores), 0)
            FROM historic_assignacions
            WHERE CAST(treballador_id AS TEXT) = CAST(? AS TEXT)
            """,
            (worker["id"],),
        ).fetchone()[0]
        or 0
    )
    duration_hours = assignment.duration_minutes / 60
    day_names = ("Dl", "Dt", "Dc", "Dj", "Dv", "Ds", "Dg")
    assignment_id = int(
        connection.execute(
            """
            INSERT INTO assig_grup_T
            (data, dia_setmana, torn, treballador_id, treballador_nom,
             treballador_plaza, treballador_grup, hora_inici, hora_fi,
             durada_hores, linia, zona, formacio, es_canvi_zona,
             es_canvi_torn, hores_totals_any, estat_planificacio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'publicada')
            """,
            (
                assignment.date.isoformat(),
                day_names[assignment.date.weekday()],
                assignment.service_id,
                str(worker["id"]),
                worker["treballador"],
                worker["plaza"],
                worker["grup"],
                assignment.start.strftime("%H:%M"),
                assignment.end.strftime("%H:%M"),
                duration_hours,
                (
                    coverage["linia"]
                    if coverage is not None
                    else source_row["linia"]
                )
                or "",
                zone,
                (
                    coverage["formacio"]
                    if coverage is not None
                    else source_row["formacio"]
                )
                or "",
                zone_change,
                turn_change,
                historic_hours + duration_hours,
            ),
        ).lastrowid
    )
    history_rowid = 0
    if coverage is not None:
        history_rowid = int(
            connection.execute(
            """
            INSERT INTO historic_assignacions
            (treballador_id, torn_id, data, hora_inici, hora_fi,
             durada_hores, es_canvi_zona, es_canvi_torn, data_apunt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                str(worker["id"]),
                assignment.service_id,
                assignment.date.isoformat(),
                assignment.start.strftime("%H:%M"),
                assignment.end.strftime("%H:%M"),
                duration_hours,
                zone_change,
                turn_change,
            ),
            ).lastrowid
        )
    return assignment_id, history_rowid


def apply_planning_changeset(
    database_path: str | Path,
    execution_id: int,
    *,
    publish_until: date | str | None = None,
    backup_directory: str | Path | None = None,
    failure_injector: FailureInjector | None = None,
    transaction_hook: PublicationHook | None = None,
) -> dict:
    """Publica el següent bloc consecutiu d'una proposta validada."""
    migrate_planning_schema(database_path)
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    backup_path: Path | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        stored = _load_with_connection(connection, execution_id)
        if stored.state != "validada":
            raise PlanningExecutionPersistenceError(
                "Només es pot publicar una proposta validada"
            )
        if stored.solver_status not in PUBLISHABLE_SOLVER_STATUSES:
            raise PlanningExecutionPersistenceError(
                "No es pot publicar la proposta: el solver no ha produït "
                "una solució factible utilitzable"
            )
        engine = normalize_solver_engine(
            stored.configuration.get("solver_engine", SolverEngine.CURRENT.value)
        )
        if engine is SolverEngine.ANNUAL:
            raise PlanningExecutionPersistenceError("El roadmap anual no es publica directament")
        publications = _active_publications(connection, execution_id)
        progress = _publication_progress(stored, publications)
        if progress["complete"]:
            raise PlanningExecutionPersistenceError(
                "La proposta ja està publicada completament"
            )
        segment_start = progress["next_start"]
        assert segment_start is not None
        segment_end = (
            date.fromisoformat(publish_until)
            if isinstance(publish_until, str)
            else publish_until
            if publish_until is not None
            else stored.request.scope.end_date
        )
        if not segment_start <= segment_end <= stored.request.scope.end_date:
            raise PlanningExecutionPersistenceError(
                "La data final ha de quedar dins del següent bloc pendent"
            )
        if (
            engine is not SolverEngine.PRIORITY
            and segment_end != stored.request.scope.end_date
        ):
            raise PlanningExecutionPersistenceError(
                "La publicació parcial només està disponible al solver vigent"
            )
        try:
            prepared = prepare_planning_problem(database_path, stored.request)
        except Exception as error:
            raise PlanningExecutionStaleError(
                f"No es pot reconstruir el pla actual: {error}"
            ) from error
        if not publications:
            if prepared.snapshot.fingerprint != stored.snapshot_hash:
                raise PlanningExecutionStaleError(
                    "El pla operatiu ha canviat des de la validació"
                )
            if planning_problem_hash(prepared.problem) != stored.problem_hash:
                raise PlanningExecutionStaleError(
                    "Les dades de planificació han canviat des de la validació"
                )
        elif _operational_hash(connection, stored) != publications[-1][
            "snapshot_posterior_hash"
        ]:
            raise PlanningExecutionStaleError(
                "El pla operatiu ha canviat des de l'últim bloc publicat"
            )
        segment_changes = tuple(
            change
            for change in stored.changes
            if segment_start <= change.date <= segment_end
        )
        final_assignments = _reconstruct_final_assignments(
            prepared.problem,
            segment_changes,
        )
        errors = PlannerCore(prepared.problem).validate(final_assignments)
        if errors:
            raise PlanningExecutionPersistenceError(
                "El pla final no supera les restriccions dures: "
                + "; ".join(errors[:5])
            )

        operational_before = _operational_hash(connection, stored)
        affected_before = _affected_hash(connection, stored, segment_changes)
        backup_path = _create_backup(
            database_path,
            execution_id,
            backup_directory,
        )
        rollback = {
            "previous_assignments": [],
            "inserted_assignment_ids": [],
            "deleted_history": [],
            "inserted_history_rowids": [],
        }
        new_assignment_ids: list[int] = []
        new_assignment_ids_by_change: dict[int, int] = {}
        for change in segment_changes:
            current = _active_row_for_need(
                connection,
                change.date.isoformat(),
                change.service_id,
            )
            if change.kind is PlanningChangeKind.ADDITION:
                if current is not None:
                    raise PlanningExecutionStaleError(
                        f"La necessitat {change.need_id} ja està coberta"
                    )
            else:
                if (
                    current is None
                    or change.previous_assignment_id is None
                    or int(current["id"]) != change.previous_assignment_id
                ):
                    raise PlanningExecutionStaleError(
                        f"L'assignació activa de {change.need_id} ha canviat"
                    )
                rollback["previous_assignments"].append(_row_dict(current))
                updated = connection.execute(
                    """
                    UPDATE assig_grup_T SET estat_planificacio = 'anul_lada'
                    WHERE id = ?
                      AND estat_planificacio IN ('publicada', 'bloquejada')
                    """,
                    (change.previous_assignment_id,),
                )
                if updated.rowcount != 1:
                    raise PlanningExecutionStaleError(
                        f"No s'ha pogut anul·lar {change.need_id}"
                    )
            if failure_injector:
                failure_injector("after_deactivate")

            old_history = connection.execute(
                """
                SELECT rowid AS _rowid, * FROM historic_assignacions
                WHERE data = ? AND torn_id = ? ORDER BY rowid
                """,
                (change.date.isoformat(), change.service_id),
            ).fetchall()
            rollback["deleted_history"].extend(
                _row_dict(row) for row in old_history
            )
            connection.execute(
                """
                DELETE FROM historic_assignacions
                WHERE data = ? AND torn_id = ?
                """,
                (change.date.isoformat(), change.service_id),
            )
            if failure_injector:
                failure_injector("after_history_delete")

            if change.kind is not PlanningChangeKind.REMOVAL:
                assert change.proposed is not None
                assignment_id, history_rowid = _insert_assignment(
                    connection,
                    change.proposed,
                    current,
                )
                new_assignment_ids.append(assignment_id)
                new_assignment_ids_by_change[change.id] = assignment_id
                rollback["inserted_assignment_ids"].append(assignment_id)
                if history_rowid:
                    rollback["inserted_history_rowids"].append(history_rowid)
                connection.execute(
                    """
                    UPDATE canvis_planificacio_cp_sat
                    SET assignacio_nova_id = ? WHERE id = ?
                    """,
                    (assignment_id, change.id),
                )
            if failure_injector:
                failure_injector("after_insert")

        summary = {
            "changes": len(segment_changes),
            "additions": sum(
                item.kind is PlanningChangeKind.ADDITION
                for item in segment_changes
            ),
            "removals": sum(
                item.kind is PlanningChangeKind.REMOVAL
                for item in segment_changes
            ),
            "reassignments": sum(
                item.kind is PlanningChangeKind.REASSIGNMENT
                for item in segment_changes
            ),
            "new_assignment_ids": new_assignment_ids,
        }
        if transaction_hook is not None:
            hook_summary = transaction_hook(
                connection,
                stored,
                new_assignment_ids_by_change,
            )
            if hook_summary:
                summary["origin"] = hook_summary
        operational_after = _operational_hash(connection, stored)
        affected_after = _affected_hash(connection, stored, segment_changes)
        publication_id = int(
            connection.execute(
                """
                INSERT INTO publicacions_planificacio_cp_sat
                (execucio_id, data_inici, data_fi,
                 snapshot_anterior_hash, snapshot_posterior_hash,
                 affected_anterior_hash, affected_posterior_hash,
                 backup_path, resum_json, rollback_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    segment_start.isoformat(),
                    segment_end.isoformat(),
                    operational_before,
                    operational_after,
                    affected_before,
                    affected_after,
                    str(backup_path),
                    _json_dumps(summary),
                    _json_dumps(rollback),
                ),
            ).lastrowid
        )
        complete = segment_end == stored.request.scope.end_date
        updated = connection.execute(
            """
            UPDATE execucions_planificacio_cp_sat
            SET estat = ?,
                published_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                backup_path = ?, snapshot_final_hash = ?
            WHERE id = ? AND estat = 'validada'
            """,
            (
                "publicada" if complete else "validada",
                int(complete),
                str(backup_path),
                operational_after,
                execution_id,
            ),
        )
        if updated.rowcount != 1:
            raise PlanningExecutionPersistenceError(
                "La proposta ha canviat d'estat durant la publicació"
            )
        plan_version = register_published_plan_version(
            connection,
            event_type="publicacio",
            execution_id=execution_id,
            publication_id=publication_id,
            origin=stored.request.trigger.kind.value,
            origin_id=stored.request.trigger.source_id,
            start_date=segment_start.isoformat(),
            end_date=segment_end.isoformat(),
        )
        invalidated_execution_ids = _invalidate_stale_validated_executions(
            connection,
            exclude_execution_id=execution_id,
        )
        connection.commit()
        return {
            "publication_id": publication_id,
            "plan_version": plan_version,
            "execution_id": execution_id,
            "state": "publicada" if complete else "validada",
            "complete": complete,
            "segment_start": segment_start.isoformat(),
            "segment_end": segment_end.isoformat(),
            "new_assignment_ids": tuple(new_assignment_ids),
            "final_snapshot_hash": operational_after,
            "backup_path": str(backup_path),
            "invalidated_execution_ids": invalidated_execution_ids,
        }
    except PlanningExecutionStaleError:
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE execucions_planificacio_cp_sat
            SET estat = 'invalidada'
            WHERE id = ? AND estat = 'validada'
            """,
            (execution_id,),
        )
        connection.commit()
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

"""Persistència i validació de propostes diferencials genèriques."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from cp_sat_pilot import (
    Assignment,
    CpSatPlanner,
    PlanningProblem,
    SolverConfig,
    assess_equity_execution,
)

from planificador_cp_sat.domain import (
    PlanningExecutionRequest,
    PlanningInputAdjustments,
    PlanningScope,
    PlanningTrigger,
    ProtectionPolicy,
)
from planificador_cp_sat.services.esquema_planificacio import (
    migrate_planning_schema,
)
from planificador_cp_sat.services.preparacio_planificacio import (
    prepare_planning_problem,
)
from planificador_cp_sat.services.proposta_planificacio import (
    PlanningChangeKind,
    PlanningProposal,
)


class PlanningExecutionPersistenceError(ValueError):
    """Indica una operació invàlida sobre una proposta desada."""


class PlanningExecutionStaleError(PlanningExecutionPersistenceError):
    """Indica que les dades actuals ja no coincideixen amb la proposta."""


@dataclass(frozen=True, slots=True)
class StoredPlanningChange:
    id: int
    order: int
    kind: PlanningChangeKind
    need_id: str
    date: date
    service_id: str
    previous_assignment_id: int | None
    previous: Assignment | None
    new_assignment_id: int | None
    proposed: Assignment | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class StoredPlanningExecution:
    id: int
    state: str
    request: PlanningExecutionRequest
    configuration: dict[str, Any]
    metrics: dict[str, Any]
    snapshot_hash: str
    problem_hash: str
    result_hash: str
    solver_status: str
    covered_needs: int
    total_needs: int
    unchanged_assignments: int
    persistent_changes: int
    uncovered_needs: int
    selected_seed: int
    changes: tuple[StoredPlanningChange, ...]
    created_at: str
    validated_at: str | None
    published_at: str | None
    discarded_at: str | None
    reverted_at: str | None
    backup_path: str | None
    final_snapshot_hash: str | None
    equity_override: dict[str, Any] | None
    equity_override_at: str | None


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _stable_hash(value: object) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _assignment_payload(assignment: Assignment | None) -> dict | None:
    if assignment is None:
        return None
    return {
        "worker_id": assignment.worker_id,
        "need_id": assignment.need_id,
        "service_id": assignment.service_id,
        "date": assignment.date.isoformat(),
        "start": assignment.start.isoformat(),
        "end": assignment.end.isoformat(),
        "duration_minutes": assignment.duration_minutes,
    }


def _assignment_from_payload(payload: dict | None) -> Assignment | None:
    if payload is None:
        return None
    return Assignment(
        worker_id=str(payload["worker_id"]),
        need_id=str(payload["need_id"]),
        service_id=str(payload["service_id"]),
        date=date.fromisoformat(payload["date"]),
        start=datetime.fromisoformat(payload["start"]),
        end=datetime.fromisoformat(payload["end"]),
        duration_minutes=int(payload["duration_minutes"]),
    )


def _request_payload(request: PlanningExecutionRequest) -> dict:
    return {
        "scope": {
            "start_date": request.scope.start_date.isoformat(),
            "end_date": request.scope.end_date.isoformat(),
            "worker_ids": sorted(request.scope.worker_ids),
            "service_ids": sorted(request.scope.service_ids),
            "assignment_ids": sorted(request.scope.assignment_ids),
        },
        "protection": {
            "freeze_until": (
                request.protection.freeze_until.isoformat()
                if request.protection.freeze_until
                else None
            ),
            "protect_outside_scope": request.protection.protect_outside_scope,
            "allow_unselected_workers_as_recipients": (
                request.protection.allow_unselected_workers_as_recipients
            ),
        },
        "trigger": {
            "kind": request.trigger.kind.value,
            "source_id": request.trigger.source_id,
            "affected_need_ids": sorted(request.trigger.affected_need_ids),
            "reason": request.trigger.reason,
        },
        "adjustments": {
            "unavailable_worker_dates": [
                [worker_id, day.isoformat()]
                for worker_id, day in sorted(
                    request.adjustments.unavailable_worker_dates
                )
            ],
            "released_worker_dates": [
                [worker_id, day.isoformat()]
                for worker_id, day in sorted(
                    request.adjustments.released_worker_dates
                )
            ],
            "preferred_assignments": [
                list(item) for item in request.adjustments.preferred_assignments
            ],
            "allow_active_assignments_without_coverage": (
                request.adjustments.allow_active_assignments_without_coverage
            ),
        },
    }


def _request_from_payload(payload: dict) -> PlanningExecutionRequest:
    scope = payload["scope"]
    protection = payload["protection"]
    trigger = payload["trigger"]
    adjustments = payload.get("adjustments", {})
    return PlanningExecutionRequest(
        scope=PlanningScope(
            date.fromisoformat(scope["start_date"]),
            date.fromisoformat(scope["end_date"]),
            worker_ids=scope["worker_ids"],
            service_ids=scope["service_ids"],
            assignment_ids=scope["assignment_ids"],
        ),
        protection=ProtectionPolicy(
            freeze_until=(
                date.fromisoformat(protection["freeze_until"])
                if protection["freeze_until"]
                else None
            ),
            protect_outside_scope=protection["protect_outside_scope"],
            allow_unselected_workers_as_recipients=protection.get(
                "allow_unselected_workers_as_recipients", False
            ),
        ),
        trigger=PlanningTrigger(
            kind=trigger["kind"],
            source_id=trigger["source_id"],
            affected_need_ids=trigger["affected_need_ids"],
            reason=trigger["reason"],
        ),
        adjustments=PlanningInputAdjustments(
            unavailable_worker_dates={
                (worker_id, date.fromisoformat(day))
                for worker_id, day in adjustments.get(
                    "unavailable_worker_dates", ()
                )
            },
            released_worker_dates={
                (worker_id, date.fromisoformat(day))
                for worker_id, day in adjustments.get(
                    "released_worker_dates", ()
                )
            },
            preferred_assignments=adjustments.get(
                "preferred_assignments", ()
            ),
            allow_active_assignments_without_coverage=adjustments.get(
                "allow_active_assignments_without_coverage", False
            ),
        ),
    )


def planning_problem_hash(problem: PlanningProblem) -> str:
    """Empremta canònica de totes les dades que condicionen el solver."""
    payload = {
        "workers": [
            {
                "id": worker.id,
                "group": worker.group,
                "skills": sorted(worker.skills),
                "rest_dates": sorted(day.isoformat() for day in worker.rest_dates),
                "annual_minutes": worker.annual_minutes,
                "max_annual_minutes": worker.max_annual_minutes,
                "home_zone": worker.home_zone,
                "turn_options": sorted(worker.turn_options),
                "historical_assignments": worker.historical_assignments,
                "historical_zone_changes": worker.historical_zone_changes,
                "historical_turn_changes": worker.historical_turn_changes,
                "annual_equity_target_minutes": (
                    worker.annual_equity_target_minutes
                ),
                "annual_base_target_minutes": (
                    worker.annual_base_target_minutes
                ),
                "annual_flexible_target_minutes": (
                    worker.annual_flexible_target_minutes
                ),
                "annual_reliever_uplift_minutes": (
                    worker.annual_reliever_uplift_minutes
                ),
                "annual_equity_basis_days": worker.annual_equity_basis_days,
                "annual_absence_days": worker.annual_absence_days,
                "compatible_opportunities": worker.compatible_opportunities,
                "compatible_opportunity_minutes": (
                    worker.compatible_opportunity_minutes
                ),
            }
            for worker in sorted(problem.workers, key=lambda item: item.id)
        ],
        "needs": [
            {
                "id": need.id,
                "service_id": need.service_id,
                "date": need.date.isoformat(),
                "start": need.start.isoformat(),
                "end": need.end.isoformat(),
                "skills": sorted(need.required_skills),
                "zone": need.zone,
                "turn_options": sorted(need.turn_options),
            }
            for need in sorted(problem.needs, key=lambda item: item.id)
        ],
        "history": [
            {
                "worker_id": item.worker_id,
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "duration_minutes": item.duration_minutes,
                "zone_change": item.zone_change,
                "turn_change": item.turn_change,
            }
            for item in sorted(
                problem.history,
                key=lambda current: (
                    current.worker_id,
                    current.start,
                    current.end,
                ),
            )
        ],
        "exclusions": sorted(
            (worker_id, day.isoformat())
            for worker_id, day in problem.exclusions
        ),
        "references": [
            _assignment_payload(item)
            for item in sorted(
                problem.reference_assignments,
                key=lambda current: current.need_id,
            )
        ],
        "locked_need_ids": sorted(problem.locked_need_ids),
        "affected_need_ids": sorted(problem.affected_need_ids),
        "preferred_assignments": sorted(problem.preferred_assignments),
        "required_assignments": sorted(problem.required_assignments),
        "recipient_worker_ids": sorted(problem.recipient_worker_ids),
    }
    return _stable_hash(payload)


def _configuration_payload(proposal: PlanningProposal) -> dict:
    return {
        "solver": (
            asdict(proposal.solver_config) if proposal.solver_config else None
        ),
        "seeds": list(proposal.requested_seeds),
        "force_all_seeds": proposal.force_all_seeds,
    }


def _metrics_payload(proposal: PlanningProposal) -> dict:
    equity_assessment = assess_equity_execution(proposal.result)
    return {
        "soft_metrics": (
            asdict(proposal.result.soft_metrics)
            if proposal.result.soft_metrics
            else None
        ),
        "phases": [asdict(item) for item in proposal.result.optimization_phases],
        "equity_assessment": asdict(equity_assessment),
        "equity_diagnostics": [
            asdict(item) for item in proposal.result.equity_diagnostics
        ],
        "wall_time_seconds": proposal.result.wall_time_seconds,
        "candidates": [asdict(item) for item in proposal.selection.candidates],
        "uncovered": [
            {
                "need_id": item.need_id,
                "reason": item.reason,
                "had_reference_assignment": item.had_reference_assignment,
                "static_candidates": item.static_candidates,
                "compatible_candidates": item.compatible_candidates,
            }
            for item in proposal.uncovered_needs
        ],
    }


def _change_reason(proposal: PlanningProposal, need_id: str) -> str | None:
    uncovered = next(
        (item for item in proposal.uncovered_needs if item.need_id == need_id),
        None,
    )
    if uncovered:
        return uncovered.reason
    classified = next(
        (
            item
            for item in proposal.prepared.classification.assignments
            if item.assignment.need_id == need_id
            and not item.assignment.is_boundary
        ),
        None,
    )
    return classified.reason if classified else "Nova cobertura proposada."


def save_planning_proposal(
    database_path: str | Path,
    proposal: PlanningProposal,
) -> int:
    """Desa la capçalera i només els canvis persistibles."""
    if not isinstance(proposal, PlanningProposal):
        raise TypeError("proposal ha de ser PlanningProposal")
    migrate_planning_schema(database_path)
    request_payload = _request_payload(proposal.prepared.request)
    configuration = _configuration_payload(proposal)
    metrics = _metrics_payload(proposal)
    problem_hash = planning_problem_hash(proposal.prepared.problem)
    source_ids = {
        item.need_id: item.assignment_id
        for item in proposal.prepared.snapshot.assignments
    }
    change_payloads = [
        {
            "kind": item.kind.value,
            "need_id": item.need_id,
            "before": _assignment_payload(item.before),
            "after": _assignment_payload(item.after),
        }
        for item in proposal.changes
    ]
    result_hash = _stable_hash(
        {
            "snapshot": proposal.snapshot_fingerprint,
            "changes": change_payloads,
            "uncovered": metrics["uncovered"],
        }
    )
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            execution_id = connection.execute(
                """
                INSERT INTO execucions_planificacio_cp_sat
                (origen, origen_id, motiu, data_inici, data_fi, abast_json,
                 politica_json, configuracio_json, snapshot_hash, problem_hash,
                 solver_status, metriques_json, necessitats_cobertes,
                 necessitats_totals, assignacions_conservades,
                 canvis_persistibles, necessitats_descobertes,
                 llavor_seleccionada, resultat_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal.prepared.request.trigger.kind.value,
                    proposal.prepared.request.trigger.source_id,
                    proposal.prepared.request.trigger.reason,
                    proposal.prepared.request.scope.start_date.isoformat(),
                    proposal.prepared.request.scope.end_date.isoformat(),
                    _json_dumps(request_payload["scope"]),
                    _json_dumps(
                        {
                            "protection": request_payload["protection"],
                            "trigger": request_payload["trigger"],
                            "adjustments": request_payload["adjustments"],
                        }
                    ),
                    _json_dumps(configuration),
                    proposal.snapshot_fingerprint,
                    problem_hash,
                    proposal.result.status,
                    _json_dumps(metrics),
                    proposal.covered_needs,
                    proposal.total_needs,
                    len(proposal.unchanged_assignments),
                    proposal.persistent_change_count,
                    len(proposal.uncovered_needs),
                    proposal.selection.selected_seed,
                    result_hash,
                ),
            ).lastrowid
            assert execution_id is not None
            for order, change in enumerate(proposal.changes):
                connection.execute(
                    """
                    INSERT INTO canvis_planificacio_cp_sat
                    (execucio_id, ordre, tipus, necessitat_id, data, servei,
                     assignacio_anterior_id, treballador_anterior_id,
                     treballador_nou_id, anterior_json, posterior_json, motiu)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        order,
                        change.kind.value,
                        change.need_id,
                        change.need.date.isoformat(),
                        change.need.service_id,
                        source_ids.get(change.need_id),
                        change.before.worker_id if change.before else None,
                        change.after.worker_id if change.after else None,
                        (
                            _json_dumps(_assignment_payload(change.before))
                            if change.before
                            else None
                        ),
                        (
                            _json_dumps(_assignment_payload(change.after))
                            if change.after
                            else None
                        ),
                        _change_reason(proposal, change.need_id),
                    ),
                )
            connection.commit()
            return int(execution_id)
        except Exception:
            connection.rollback()
            raise


def _stored_change(row: sqlite3.Row) -> StoredPlanningChange:
    return StoredPlanningChange(
        id=int(row["id"]),
        order=int(row["ordre"]),
        kind=PlanningChangeKind(row["tipus"]),
        need_id=str(row["necessitat_id"]),
        date=date.fromisoformat(row["data"]),
        service_id=str(row["servei"]),
        previous_assignment_id=row["assignacio_anterior_id"],
        previous=_assignment_from_payload(
            json.loads(row["anterior_json"]) if row["anterior_json"] else None
        ),
        new_assignment_id=row["assignacio_nova_id"],
        proposed=_assignment_from_payload(
            json.loads(row["posterior_json"]) if row["posterior_json"] else None
        ),
        reason=row["motiu"],
    )


def _stored_execution(
    header: sqlite3.Row,
    changes: Iterable[sqlite3.Row],
) -> StoredPlanningExecution:
    scope = json.loads(header["abast_json"])
    policy_trigger = json.loads(header["politica_json"])
    request = _request_from_payload(
        {
            "scope": scope,
            "protection": policy_trigger["protection"],
            "trigger": policy_trigger["trigger"],
            "adjustments": policy_trigger.get("adjustments", {}),
        }
    )
    return StoredPlanningExecution(
        id=int(header["id"]),
        state=str(header["estat"]),
        request=request,
        configuration=json.loads(header["configuracio_json"]),
        metrics=json.loads(header["metriques_json"]),
        snapshot_hash=str(header["snapshot_hash"]),
        problem_hash=str(header["problem_hash"]),
        result_hash=str(header["resultat_hash"]),
        solver_status=str(header["solver_status"]),
        covered_needs=int(header["necessitats_cobertes"]),
        total_needs=int(header["necessitats_totals"]),
        unchanged_assignments=int(header["assignacions_conservades"]),
        persistent_changes=int(header["canvis_persistibles"]),
        uncovered_needs=int(header["necessitats_descobertes"]),
        selected_seed=int(header["llavor_seleccionada"]),
        changes=tuple(_stored_change(row) for row in changes),
        created_at=str(header["created_at"]),
        validated_at=header["validated_at"],
        published_at=header["published_at"],
        discarded_at=header["discarded_at"],
        reverted_at=header["reverted_at"],
        backup_path=header["backup_path"],
        final_snapshot_hash=header["snapshot_final_hash"],
        equity_override=(
            json.loads(header["equity_override_json"])
            if header["equity_override_json"]
            else None
        ),
        equity_override_at=header["equity_override_at"],
    )


def _load_with_connection(
    connection: sqlite3.Connection,
    execution_id: int,
) -> StoredPlanningExecution:
    header = connection.execute(
        "SELECT * FROM execucions_planificacio_cp_sat WHERE id = ?",
        (execution_id,),
    ).fetchone()
    if header is None:
        raise PlanningExecutionPersistenceError(
            f"No existeix l'execució de planificació {execution_id}"
        )
    changes = connection.execute(
        """
        SELECT * FROM canvis_planificacio_cp_sat
        WHERE execucio_id = ? ORDER BY ordre
        """,
        (execution_id,),
    ).fetchall()
    return _stored_execution(header, changes)


def load_planning_execution(
    database_path: str | Path,
    execution_id: int,
) -> StoredPlanningExecution:
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        return _load_with_connection(connection, execution_id)


def list_planning_executions(
    database_path: str | Path,
    *,
    states: Iterable[str] | None = None,
) -> tuple[StoredPlanningExecution, ...]:
    migrate_planning_schema(database_path)
    selected_states = tuple(dict.fromkeys(states or ()))
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        if selected_states:
            placeholders = ",".join("?" for _ in selected_states)
            ids = connection.execute(
                "SELECT id FROM execucions_planificacio_cp_sat "
                f"WHERE estat IN ({placeholders}) ORDER BY id DESC",
                selected_states,
            ).fetchall()
        else:
            ids = connection.execute(
                "SELECT id FROM execucions_planificacio_cp_sat ORDER BY id DESC"
            ).fetchall()
        return tuple(
            _load_with_connection(connection, int(row[0])) for row in ids
        )


def _reconstruct_final_assignments(
    problem: PlanningProblem,
    changes: tuple[StoredPlanningChange, ...],
) -> tuple[Assignment, ...]:
    needs = {need.id: need for need in problem.needs}
    final = {
        assignment.need_id: assignment
        for assignment in problem.reference_assignments
    }
    for change in changes:
        need = needs.get(change.need_id)
        if need is None:
            raise PlanningExecutionStaleError(
                f"La necessitat {change.need_id} ja no existeix"
            )
        current = final.get(change.need_id)
        if change.kind is PlanningChangeKind.ADDITION:
            if current is not None or change.proposed is None:
                raise PlanningExecutionStaleError(
                    f"L'alta {change.need_id} ja no és aplicable"
                )
        else:
            if (
                current is None
                or change.previous is None
                or current.worker_id != change.previous.worker_id
            ):
                raise PlanningExecutionStaleError(
                    f"L'assignació anterior de {change.need_id} ha canviat"
                )
        if change.kind is PlanningChangeKind.REMOVAL:
            final.pop(change.need_id, None)
        else:
            assert change.proposed is not None
            final[change.need_id] = Assignment(
                worker_id=change.proposed.worker_id,
                need_id=need.id,
                service_id=need.service_id,
                date=need.date,
                start=need.start,
                end=need.end,
                duration_minutes=need.duration_minutes,
            )
    return tuple(final[key] for key in sorted(final))


def validate_planning_execution(
    database_path: str | Path,
    execution_id: int,
) -> StoredPlanningExecution:
    """Revalida fotografia, dades i pla final sense modificar l'operativa."""
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            stored = _load_with_connection(connection, execution_id)
            if stored.state != "esborrany":
                raise PlanningExecutionPersistenceError(
                    "Només es pot validar una proposta en estat esborrany"
                )
            try:
                prepared = prepare_planning_problem(
                    database_path,
                    stored.request,
                )
            except Exception as error:
                raise PlanningExecutionStaleError(
                    f"No es pot reconstruir la fotografia actual: {error}"
                ) from error
            if prepared.snapshot.fingerprint != stored.snapshot_hash:
                raise PlanningExecutionStaleError(
                    "La fotografia del pla ha canviat des de la simulació"
                )
            if planning_problem_hash(prepared.problem) != stored.problem_hash:
                raise PlanningExecutionStaleError(
                    "Les dades de planificació han canviat des de la simulació"
                )
            final_assignments = _reconstruct_final_assignments(
                prepared.problem,
                stored.changes,
            )
            errors = CpSatPlanner(prepared.problem).validate(final_assignments)
            if errors:
                raise PlanningExecutionPersistenceError(
                    "El pla reconstruït incompleix restriccions dures: "
                    + "; ".join(errors[:5])
                )
            if len(final_assignments) != stored.covered_needs:
                raise PlanningExecutionPersistenceError(
                    "La cobertura reconstruïda no concorda amb la proposta"
                )
            updated = connection.execute(
                """
                UPDATE execucions_planificacio_cp_sat
                SET estat = 'validada', validated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND estat = 'esborrany'
                """,
                (execution_id,),
            )
            if updated.rowcount != 1:
                raise PlanningExecutionPersistenceError(
                    "La proposta ha canviat d'estat durant la validació"
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return load_planning_execution(database_path, execution_id)


def discard_planning_execution(
    database_path: str | Path,
    execution_id: int,
) -> StoredPlanningExecution:
    migrate_planning_schema(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        updated = connection.execute(
            """
            UPDATE execucions_planificacio_cp_sat
            SET estat = 'descartada', discarded_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estat IN ('esborrany', 'validada')
            """,
            (execution_id,),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise PlanningExecutionPersistenceError(
                "Només es pot descartar una proposta esborrany o validada"
            )
        connection.commit()
    return load_planning_execution(database_path, execution_id)

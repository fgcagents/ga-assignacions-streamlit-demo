"""Construcció genèrica del problema CP-SAT a partir del pla vigent."""

from __future__ import annotations

import sys
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from planificador_cp_sat.domain import PlanningExecutionRequest
from planificador_cp_sat.services.classificacio_planificacio import (
    AssignmentClassification,
    classify_snapshot_assignments,
)
from planificador_cp_sat.services.fotografia_planificacio import (
    ActiveAssignmentSnapshot,
    PlanningSnapshot,
    load_planning_snapshot,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PILOT_SRC = PROJECT_ROOT / "cp_sat_pilot" / "src"
if str(PILOT_SRC) not in sys.path:
    sys.path.insert(0, str(PILOT_SRC))

from cp_sat_pilot import (  # noqa: E402
    Assignment,
    CpSatPlanner,
    HistoricalAssignment,
    Need,
    PlanningProblem,
    Worker,
)
from cp_sat_pilot.sqlite_adapter import (  # noqa: E402
    SqliteInputError,
    load_problem_from_sqlite,
)


class PlanningProblemPreparationError(ValueError):
    """Indica que l'estat actual no es pot convertir en un problema segur."""


@dataclass(frozen=True, slots=True)
class PreparedPlanningProblem:
    """Context complet i reproduïble lliurat al futur generador."""

    request: PlanningExecutionRequest
    snapshot: PlanningSnapshot
    classification: AssignmentClassification
    problem: PlanningProblem


def _history_key(
    worker_id: str,
    start: datetime,
    end: datetime,
    duration_minutes: int,
) -> tuple[str, datetime, datetime, int]:
    return worker_id, start, end, duration_minutes


def _merge_boundary_history(
    workers: tuple[Worker, ...],
    history: tuple[HistoricalAssignment, ...],
    boundary_assignments: tuple[ActiveAssignmentSnapshot, ...],
) -> tuple[tuple[Worker, ...], tuple[HistoricalAssignment, ...]]:
    """Afegeix només les fronteres que no consten ja a l'històric."""
    merged = list(history)
    existing = {
        _history_key(item.worker_id, item.start, item.end, item.duration_minutes)
        for item in history
    }
    added_minutes: dict[str, int] = {}
    added_counts: dict[str, int] = {}
    added_zone_changes: dict[str, int] = {}
    added_turn_changes: dict[str, int] = {}

    for assignment in boundary_assignments:
        key = _history_key(
            assignment.worker_id,
            assignment.start,
            assignment.end,
            assignment.duration_minutes,
        )
        if key in existing:
            continue
        existing.add(key)
        merged.append(
            HistoricalAssignment(
                worker_id=assignment.worker_id,
                start=assignment.start,
                end=assignment.end,
                duration_minutes=assignment.duration_minutes,
                zone_change=assignment.zone_change,
                turn_change=assignment.turn_change,
            )
        )
        added_minutes[assignment.worker_id] = (
            added_minutes.get(assignment.worker_id, 0)
            + assignment.duration_minutes
        )
        added_counts[assignment.worker_id] = (
            added_counts.get(assignment.worker_id, 0) + 1
        )
        added_zone_changes[assignment.worker_id] = (
            added_zone_changes.get(assignment.worker_id, 0)
            + int(assignment.zone_change)
        )
        added_turn_changes[assignment.worker_id] = (
            added_turn_changes.get(assignment.worker_id, 0)
            + int(assignment.turn_change)
        )

    adjusted_workers = tuple(
        replace(
            worker,
            annual_minutes=(
                worker.annual_minutes + added_minutes.get(worker.id, 0)
            ),
            historical_assignments=(
                worker.historical_assignments
                + added_counts.get(worker.id, 0)
            ),
            historical_zone_changes=(
                worker.historical_zone_changes
                + added_zone_changes.get(worker.id, 0)
            ),
            historical_turn_changes=(
                worker.historical_turn_changes
                + added_turn_changes.get(worker.id, 0)
            ),
        )
        for worker in workers
    )
    merged.sort(key=lambda item: (item.start, item.end, item.worker_id))
    return adjusted_workers, tuple(merged)


def _selected_needs(
    snapshot: PlanningSnapshot,
    request: PlanningExecutionRequest,
    required_need_ids: frozenset[str] = frozenset(),
) -> tuple[Need, ...]:
    active_need_ids = {
        assignment.need_id for assignment in snapshot.assignments
    }
    origin_filter_active = bool(
        request.scope.worker_ids or request.scope.assignment_ids
    )
    selected: list[Need] = []
    for item in snapshot.needs:
        need_id = item.need.id
        if need_id in required_need_ids:
            selected.append(item.need)
            continue
        if need_id in active_need_ids:
            selected.append(item.need)
            continue
        if not item.in_scope:
            continue
        if origin_filter_active and need_id not in request.trigger.affected_need_ids:
            continue
        selected.append(item.need)
    return tuple(selected)


def _load_required_assignments(
    database_path: str | Path,
    request: PlanningExecutionRequest,
) -> tuple[tuple[str, str], ...]:
    with closing(sqlite3.connect(database_path)) as connection:
        exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'preassignacions_planificacio'
            """
        ).fetchone()
        if exists is None:
            return ()
        return tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT necessitat_id, treballador_id
                FROM preassignacions_planificacio
                WHERE estat = 'activa' AND data BETWEEN ? AND ?
                ORDER BY necessitat_id
                """,
                (
                    request.scope.start_date.isoformat(),
                    request.scope.end_date.isoformat(),
                ),
            )
        )


def _reference_assignments(
    snapshot: PlanningSnapshot,
    needs_by_id: dict[str, Need],
) -> tuple[Assignment, ...]:
    references: list[Assignment] = []
    for source in snapshot.assignments:
        need = needs_by_id[source.need_id]
        if (
            source.start != need.start
            or source.end != need.end
            or source.duration_minutes != need.duration_minutes
        ):
            raise PlanningProblemPreparationError(
                f"L'assignació activa #{source.assignment_id} no coincideix "
                f"amb l'horari vigent de la necessitat {source.need_id}"
            )
        references.append(
            Assignment(
                worker_id=source.worker_id,
                need_id=need.id,
                service_id=need.service_id,
                date=need.date,
                start=need.start,
                end=need.end,
                duration_minutes=need.duration_minutes,
            )
        )
    return tuple(references)


def _validate_locked_assignments(problem: PlanningProblem) -> None:
    if not problem.locked_need_ids or problem.required_assignments:
        return
    locked = tuple(
        assignment
        for assignment in problem.reference_assignments
        if assignment.need_id in problem.locked_need_ids
    )
    errors = CpSatPlanner(problem).validate(locked)
    if errors:
        raise PlanningProblemPreparationError(
            "Les assignacions protegides ja no compleixen les restriccions "
            "dures: "
            + "; ".join(errors[:5])
        )


def _validate_required_assignments(problem: PlanningProblem) -> None:
    if not problem.required_assignments:
        return
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
    errors = CpSatPlanner(problem).validate(assignments)
    if errors:
        raise PlanningProblemPreparationError(
            "Les preassignacions no compleixen les restriccions dures: "
            + "; ".join(errors[:5])
        )


def prepare_planning_problem(
    database_path: str | Path,
    request: PlanningExecutionRequest,
    *,
    snapshot: PlanningSnapshot | None = None,
) -> PreparedPlanningProblem:
    """Construeix el mateix `PlanningProblem` per a qualsevol origen."""
    if not isinstance(request, PlanningExecutionRequest):
        raise TypeError("request ha de ser PlanningExecutionRequest")
    current_snapshot = snapshot or load_planning_snapshot(
        database_path,
        request.scope,
        allow_active_assignments_without_coverage=(
            request.adjustments.allow_active_assignments_without_coverage
        ),
    )
    if current_snapshot.scope != request.scope:
        raise PlanningProblemPreparationError(
            "L'abast de la fotografia no coincideix amb el de la petició"
        )
    classification = classify_snapshot_assignments(
        current_snapshot,
        request,
    )
    required_assignments = _load_required_assignments(database_path, request)
    required_need_ids = frozenset(
        need_id for need_id, _worker_id in required_assignments
    )

    try:
        base_problem = load_problem_from_sqlite(
            database_path,
            start_date=request.scope.start_date,
            end_date=request.scope.end_date,
            duplicate_policy="replace_all",
            allow_empty_needs=True,
        )
    except SqliteInputError as error:
        raise PlanningProblemPreparationError(str(error)) from error

    selected_needs = _selected_needs(
        current_snapshot,
        request,
        required_need_ids,
    )
    needs_by_id = {need.id: need for need in selected_needs}
    missing_active = {
        item.need_id for item in current_snapshot.assignments
    } - set(needs_by_id)
    if missing_active:
        raise PlanningProblemPreparationError(
            "Assignacions actives excloses del problema: "
            + ", ".join(sorted(missing_active))
        )
    references = _reference_assignments(current_snapshot, needs_by_id)
    workers, history = _merge_boundary_history(
        base_problem.workers,
        base_problem.history,
        current_snapshot.boundary_assignments,
    )

    released = request.adjustments.released_worker_dates
    unavailable = request.adjustments.unavailable_worker_dates
    workers = tuple(
        replace(
            worker,
            rest_dates=(
                worker.rest_dates
                - {
                    day
                    for worker_id, day in released
                    if worker_id == worker.id
                }
            ),
        )
        for worker in workers
    )
    exclusions = {
        item for item in base_problem.exclusions if item not in released
    }
    exclusions.update(unavailable)

    known_need_ids = {need.id for need in selected_needs}
    unknown_preferences = {
        need_id
        for need_id, _worker_id in request.adjustments.preferred_assignments
        if need_id not in known_need_ids
    }
    if unknown_preferences:
        raise PlanningProblemPreparationError(
            "Preferències per a necessitats fora del problema: "
            + ", ".join(sorted(unknown_preferences))
        )

    problem = PlanningProblem(
        workers=workers,
        needs=selected_needs,
        history=history,
        exclusions=frozenset(exclusions),
        reference_assignments=references,
        locked_need_ids=classification.hard_locked_need_ids,
        affected_need_ids=classification.affected_need_ids,
        preferred_assignments=request.adjustments.preferred_assignments,
        required_assignments=required_assignments,
        recipient_worker_ids=(
            frozenset()
            if (
                not request.scope.worker_ids
                or request.protection.allow_unselected_workers_as_recipients
            )
            else request.scope.worker_ids
        ),
    )
    _validate_locked_assignments(problem)
    _validate_required_assignments(problem)
    return PreparedPlanningProblem(
        request=request,
        snapshot=current_snapshot,
        classification=classification,
        problem=problem,
    )

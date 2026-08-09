from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable


MINIMUM_REST_MINUTES = 12 * 60


@dataclass(frozen=True, slots=True)
class Worker:
    id: str
    group: str
    skills: frozenset[str]
    rest_dates: frozenset[date] = field(default_factory=frozenset)
    annual_minutes: int = 0
    max_annual_minutes: int = 1605 * 60
    home_zone: str = ""
    turn_options: frozenset[str] = field(default_factory=frozenset)
    historical_assignments: int = 0
    historical_zone_changes: int = 0
    historical_turn_changes: int = 0

    @property
    def remaining_annual_minutes(self) -> int:
        return max(0, self.max_annual_minutes - self.annual_minutes)


@dataclass(frozen=True, slots=True)
class Need:
    id: str
    service_id: str
    date: date
    start: datetime
    end: datetime
    required_skills: frozenset[str]
    zone: str = ""
    turn_options: frozenset[str] = field(default_factory=frozenset)

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True, slots=True)
class HistoricalAssignment:
    worker_id: str
    start: datetime
    end: datetime
    duration_minutes: int
    zone_change: bool = False
    turn_change: bool = False


@dataclass(frozen=True, slots=True)
class PlanningProblem:
    workers: tuple[Worker, ...]
    needs: tuple[Need, ...]
    history: tuple[HistoricalAssignment, ...] = ()
    exclusions: frozenset[tuple[str, date]] = field(default_factory=frozenset)
    reference_assignments: tuple[Assignment, ...] = ()
    locked_need_ids: frozenset[str] = field(default_factory=frozenset)
    affected_need_ids: frozenset[str] = field(default_factory=frozenset)
    preferred_assignments: tuple[tuple[str, str], ...] = ()
    required_assignments: tuple[tuple[str, str], ...] = ()
    recipient_worker_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        worker_ids = [worker.id for worker in self.workers]
        need_ids = [need.id for need in self.needs]
        if len(worker_ids) != len(set(worker_ids)):
            raise ValueError("Els identificadors de treballador han de ser únics")
        if len(need_ids) != len(set(need_ids)):
            raise ValueError("Els identificadors de necessitat han de ser únics")
        if any(need.end <= need.start for need in self.needs):
            raise ValueError("Totes les necessitats han de tenir una durada positiva")
        worker_id_set = set(worker_ids)
        need_id_set = set(need_ids)
        reference_need_ids = [
            assignment.need_id for assignment in self.reference_assignments
        ]
        if len(reference_need_ids) != len(set(reference_need_ids)):
            raise ValueError(
                "El pla de referència només pot tenir una assignació per necessitat"
            )
        for assignment in self.reference_assignments:
            if assignment.worker_id not in worker_id_set:
                raise ValueError(
                    "Treballador desconegut al pla de referència: "
                    f"{assignment.worker_id}"
                )
            if assignment.need_id not in need_id_set:
                raise ValueError(
                    "Necessitat desconeguda al pla de referència: "
                    f"{assignment.need_id}"
                )
        unknown_locked = self.locked_need_ids - need_id_set
        if unknown_locked:
            raise ValueError(
                "Necessitats bloquejades desconegudes: "
                + ", ".join(sorted(unknown_locked))
            )
        unknown_affected = self.affected_need_ids - need_id_set
        if unknown_affected:
            raise ValueError(
                "Necessitats afectades desconegudes: "
                + ", ".join(sorted(unknown_affected))
            )
        unknown_recipients = self.recipient_worker_ids - worker_id_set
        if unknown_recipients:
            raise ValueError(
                "Treballadors receptors desconeguts: "
                + ", ".join(sorted(unknown_recipients))
            )
        if self.locked_need_ids & self.affected_need_ids:
            raise ValueError(
                "Una necessitat no pot estar bloquejada i afectada alhora"
            )
        missing_locked_reference = self.locked_need_ids - set(reference_need_ids)
        if missing_locked_reference:
            raise ValueError(
                "Tota necessitat bloquejada ha de tenir una assignació de referència: "
                + ", ".join(sorted(missing_locked_reference))
            )
        preferred_need_ids = [need_id for need_id, _ in self.preferred_assignments]
        if len(preferred_need_ids) != len(set(preferred_need_ids)):
            raise ValueError(
                "Només es pot indicar un treballador preferit per necessitat"
            )
        for need_id, worker_id in self.preferred_assignments:
            if need_id not in need_id_set or worker_id not in worker_id_set:
                raise ValueError(
                    "Preferència d'assignació desconeguda: "
                    f"{worker_id} -> {need_id}"
                )
        required_need_ids = [need_id for need_id, _ in self.required_assignments]
        if len(required_need_ids) != len(set(required_need_ids)):
            raise ValueError(
                "Només es pot exigir un treballador per necessitat"
            )
        for need_id, worker_id in self.required_assignments:
            if need_id not in need_id_set or worker_id not in worker_id_set:
                raise ValueError(
                    "Preassignació obligatòria desconeguda: "
                    f"{worker_id} -> {need_id}"
                )


@dataclass(frozen=True, slots=True)
class Assignment:
    worker_id: str
    need_id: str
    service_id: str
    date: date
    start: datetime
    end: datetime
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class SolveResult:
    status: str
    assignments: tuple[Assignment, ...]
    covered_needs: int
    total_needs: int
    objective_value: float | None
    best_objective_bound: float | None
    relative_gap: float | None
    wall_time_seconds: float
    conflicts: int
    branches: int
    candidate_variables: int
    incompatibility_constraints: int
    validation_errors: tuple[str, ...] = ()
    soft_metrics: SoftMetrics | None = None
    optimization_phases: tuple[OptimizationPhase, ...] = ()

    @property
    def feasible(self) -> bool:
        return self.status in {"FEASIBLE", "OPTIMAL"} and not self.validation_errors


@dataclass(frozen=True, slots=True)
class OptimizationPhase:
    name: str
    status: str
    objective_value: float | None
    best_objective_bound: float | None
    relative_gap: float | None
    wall_time_seconds: float
    conflicts: int
    branches: int


@dataclass(frozen=True, slots=True)
class SoftMetrics:
    plan_alterations: int
    preferred_assignment_violations: int
    consecutive_excess_windows: int
    friday_violation: int
    zone_changes: int
    turn_changes: int
    annual_hours_range_minutes: int
    annual_hours_equity_penalty: int
    accumulated_zone_rate_range_permille: int
    accumulated_turn_rate_range_permille: int
    worst_change_equity_gap_permille: int
    accumulated_zone_equity_penalty: int
    accumulated_turn_equity_penalty: int
    operational_penalty: int
    annual_fairness_objective: int
    change_fairness_objective: int
    change_tiebreak_penalty: int
    normalized_total_changes: int
    opportunistic_equity_objective: int


def assignments_compatible(
    first_start: datetime,
    first_end: datetime,
    second_start: datetime,
    second_end: datetime,
    minimum_rest_minutes: int = MINIMUM_REST_MINUTES,
) -> bool:
    """Comprova absència de solapament i descans mínim en qualsevol ordre."""
    if first_end <= second_start:
        rest = int((second_start - first_end).total_seconds() // 60)
        return rest >= minimum_rest_minutes
    if second_end <= first_start:
        rest = int((first_start - second_end).total_seconds() // 60)
        return rest >= minimum_rest_minutes
    return False


def group_history_by_worker(
    history: Iterable[HistoricalAssignment],
) -> dict[str, tuple[HistoricalAssignment, ...]]:
    grouped: dict[str, list[HistoricalAssignment]] = {}
    for assignment in history:
        grouped.setdefault(assignment.worker_id, []).append(assignment)
    return {
        worker_id: tuple(sorted(items, key=lambda item: item.start))
        for worker_id, items in grouped.items()
    }

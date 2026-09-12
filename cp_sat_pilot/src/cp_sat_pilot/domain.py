from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable


MINIMUM_REST_MINUTES = 12 * 60
NIGHT_START_TIME = time(19, 0)
NIGHT_END_LIMIT = time(6, 0)


def is_night_interval(start: datetime, end: datetime) -> bool:
    """Classifica un servei nocturn només a partir del seu interval."""
    next_day = start.date() + timedelta(days=1)
    midnight = datetime.combine(next_day, time.min)
    latest_end = datetime.combine(next_day, NIGHT_END_LIMIT)
    return (
        start.time() > NIGHT_START_TIME
        and midnight < end <= latest_end
    )


@dataclass(frozen=True, slots=True)
class Worker:
    id: str
    group: str
    skills: frozenset[str]
    rest_dates: frozenset[date] = field(default_factory=frozenset)
    base_rest_dates: frozenset[date] = field(default_factory=frozenset)
    annual_minutes: int = 0
    max_annual_minutes: int = 1605 * 60
    home_zone: str = ""
    turn_options: frozenset[str] = field(default_factory=frozenset)
    can_work_nights: bool = field(init=False)
    historical_assignments: int = 0
    historical_zone_changes: int = 0
    historical_turn_changes: int = 0
    historical_preference_exceptions: int = 0
    historical_night_services: int = 0
    annual_equity_target_minutes: int = 0
    annual_equity_basis_days: int = 0
    annual_absence_days: int = 0
    compatible_opportunities: int = 0
    compatible_opportunity_minutes: int = 0
    annual_base_target_minutes: int = 0
    annual_flexible_target_minutes: int = 0
    annual_reliever_uplift_minutes: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "can_work_nights",
            any(option.casefold() == "nit" for option in self.turn_options),
        )
        if self.historical_preference_exceptions <= 0:
            object.__setattr__(
                self,
                "historical_preference_exceptions",
                max(
                    self.historical_zone_changes,
                    self.historical_turn_changes,
                ),
            )
        if self.annual_equity_target_minutes <= 0:
            object.__setattr__(
                self,
                "annual_equity_target_minutes",
                self.max_annual_minutes,
            )
        if self.annual_base_target_minutes <= 0:
            object.__setattr__(
                self,
                "annual_base_target_minutes",
                self.annual_equity_target_minutes,
            )
        if self.annual_flexible_target_minutes <= 0:
            object.__setattr__(
                self,
                "annual_flexible_target_minutes",
                self.annual_equity_target_minutes,
            )

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
    is_night: bool | None = None

    def __post_init__(self) -> None:
        if self.is_night is None:
            object.__setattr__(
                self,
                "is_night",
                is_night_interval(self.start, self.end),
            )

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def violates_late_friday_base_weekend(worker: Worker, need: Need) -> bool:
    """Protegeix el descans base contigu de dissabte i diumenge."""
    if need.date.weekday() != 4:
        return False
    saturday = need.date + timedelta(days=1)
    sunday = need.date + timedelta(days=2)
    if not {saturday, sunday}.issubset(worker.base_rest_dates):
        return False
    return need.end.date() > need.date or need.end.time() > time(22, 0)


@dataclass(frozen=True, slots=True)
class HistoricalAssignment:
    worker_id: str
    start: datetime
    end: datetime
    duration_minutes: int
    zone_change: bool = False
    turn_change: bool = False
    is_night: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "is_night",
            is_night_interval(self.start, self.end),
        )


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
    equity_diagnostics: tuple[EquityWorkerDiagnostic, ...] = ()
    social_diagnostic_summary: SocialDiagnosticSummary | None = None

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
    adjusted_annual_rate_range_permille: int = 0
    outside_preference_services: int = 0
    max_accumulated_night_services: int = 0
    social_objective: int = 0


@dataclass(frozen=True, slots=True)
class EquityWorkerDiagnostic:
    worker_id: str
    annual_minutes: int
    adjusted_target_minutes: int
    completion_rate_permille: int
    absence_days: int
    availability_basis_days: int
    compatible_opportunities: int
    compatible_opportunity_minutes: int
    assigned_opportunities: int
    comparable: bool
    justification_codes: tuple[str, ...]
    peer_gap_permille: int = 0
    review_status: str = "no_comparable"
    base_target_minutes: int = 0
    flexible_target_minutes: int = 0
    reliever_uplift_minutes: int = 0
    maximum_minutes: int = 0
    current_services: int = 0
    historical_services: int = 0
    accumulated_services: int = 0
    current_zone_exception_services: int = 0
    historical_zone_exception_services: int = 0
    accumulated_zone_exception_services: int = 0
    current_turn_exception_services: int = 0
    historical_turn_exception_services: int = 0
    accumulated_turn_exception_services: int = 0
    current_double_exception_services: int = 0
    historical_double_exception_services: int = 0
    accumulated_double_exception_services: int = 0
    current_preference_exception_services: int = 0
    historical_preference_exception_services: int = 0
    accumulated_preference_exception_services: int = 0
    current_night_services: int = 0
    historical_night_services: int | None = None
    accumulated_night_services: int | None = None
    can_work_nights: bool = False
    comparison_profile: str = ""
    comparison_group_size: int = 0
    comparison_status: str = "sense perfil comparable"


@dataclass(frozen=True, slots=True)
class SocialDiagnosticSummary:
    worker_count: int
    comparable_worker_count: int
    comparison_profile_count: int
    unique_profile_worker_count: int
    current_services: int
    current_zone_exception_services: int
    current_turn_exception_services: int
    current_double_exception_services: int
    current_night_services: int
    night_capable_worker_count: int
    completion_rate_mean_absolute_deviation_permille: float
    completion_rate_gini: float
    completion_rate_p10_permille: float
    completion_rate_p90_permille: float
    completion_rate_p90_p10_gap_permille: float
    night_history_available: bool = False
    current_preference_exception_services: int = 0


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

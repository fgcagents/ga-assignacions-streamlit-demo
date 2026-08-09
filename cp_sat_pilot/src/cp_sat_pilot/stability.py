from __future__ import annotations

import hashlib
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Iterable

from .domain import Assignment, SolveResult


@dataclass(frozen=True, slots=True)
class StabilityRun:
    time_limit_seconds: float
    num_workers: int
    seed: int
    repetition: int
    status: str
    covered: int
    total_needs: int
    coverage_phase_status: str
    coverage_gap: float | None
    operational_phase_status: str
    annual_phase_status: str
    annual_phase_gap: float | None
    change_phase_status: str
    change_phase_gap: float | None
    tiebreak_phase_status: str
    annual_hours_range: float | None
    zone_rate_range_points: float | None
    turn_rate_range_points: float | None
    worst_change_gap_points: float | None
    annual_fairness_objective: int | None
    change_fairness_objective: int | None
    zone_changes: int | None
    turn_changes: int | None
    wall_time_seconds: float
    assignment_fingerprint: str
    validation_errors: int
    stability_phase_status: str = "NO_EXECUTADA"
    equity_phase_status: str = "NO_EXECUTADA"
    equity_phase_gap: float | None = None
    plan_alterations: int | None = None
    opportunistic_equity_objective: int | None = None


@dataclass(frozen=True, slots=True)
class StabilityAggregate:
    time_limit_seconds: float
    num_workers: int
    runs: int
    seeds: tuple[int, ...]
    repetitions: int
    coverage_min: int
    coverage_max: int
    coverage_all_optimal: bool
    annual_solved_runs: int
    change_solved_runs: int
    tiebreak_solved_runs: int
    annual_hours_min: float | None
    annual_hours_max: float | None
    annual_hours_mean: float | None
    annual_hours_stddev: float | None
    annual_gap_min: float | None
    annual_gap_max: float | None
    zone_rate_min: float | None
    zone_rate_max: float | None
    turn_rate_min: float | None
    turn_rate_max: float | None
    unique_annual_objectives: int
    unique_change_objectives: int
    unique_assignment_plans: int
    stability_solved_runs: int = 0
    equity_solved_runs: int = 0
    plan_alterations_min: int | None = None
    plan_alterations_max: int | None = None
    unique_equity_objectives: int = 0


def assignment_fingerprint(assignments: Iterable[Assignment]) -> str:
    payload = "\n".join(
        f"{assignment.need_id}:{assignment.worker_id}"
        for assignment in sorted(
            assignments,
            key=lambda item: (item.need_id, item.worker_id),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def summarize_stability_run(
    result: SolveResult,
    *,
    time_limit_seconds: float,
    num_workers: int,
    seed: int,
    repetition: int,
) -> StabilityRun:
    phases = {phase.name: phase for phase in result.optimization_phases}
    coverage_phase = phases.get("cobertura")
    stability_phase = phases.get("estabilitat_pla")
    operational_phase = phases.get("preferencies_operatives")
    equity_phase = phases.get("equitat_oportunista")
    metrics = result.soft_metrics

    return StabilityRun(
        time_limit_seconds=time_limit_seconds,
        num_workers=num_workers,
        seed=seed,
        repetition=repetition,
        status=result.status,
        covered=result.covered_needs,
        total_needs=result.total_needs,
        coverage_phase_status=(
            coverage_phase.status if coverage_phase else "NO_EXECUTADA"
        ),
        coverage_gap=coverage_phase.relative_gap if coverage_phase else None,
        operational_phase_status=(
            operational_phase.status if operational_phase else "NO_EXECUTADA"
        ),
        annual_phase_status=equity_phase.status if equity_phase else "NO_EXECUTADA",
        annual_phase_gap=equity_phase.relative_gap if equity_phase else None,
        change_phase_status=equity_phase.status if equity_phase else "NO_EXECUTADA",
        change_phase_gap=equity_phase.relative_gap if equity_phase else None,
        tiebreak_phase_status=(
            equity_phase.status if equity_phase else "NO_EXECUTADA"
        ),
        annual_hours_range=(
            metrics.annual_hours_range_minutes / 60 if metrics else None
        ),
        zone_rate_range_points=(
            metrics.accumulated_zone_rate_range_permille / 10
            if metrics
            else None
        ),
        turn_rate_range_points=(
            metrics.accumulated_turn_rate_range_permille / 10
            if metrics
            else None
        ),
        worst_change_gap_points=(
            metrics.worst_change_equity_gap_permille / 10
            if metrics
            else None
        ),
        annual_fairness_objective=(
            metrics.annual_fairness_objective if metrics else None
        ),
        change_fairness_objective=(
            metrics.change_fairness_objective if metrics else None
        ),
        zone_changes=metrics.zone_changes if metrics else None,
        turn_changes=metrics.turn_changes if metrics else None,
        wall_time_seconds=result.wall_time_seconds,
        assignment_fingerprint=assignment_fingerprint(result.assignments),
        validation_errors=len(result.validation_errors),
        stability_phase_status=(
            stability_phase.status if stability_phase else "NO_EXECUTADA"
        ),
        equity_phase_status=(
            equity_phase.status if equity_phase else "NO_EXECUTADA"
        ),
        equity_phase_gap=equity_phase.relative_gap if equity_phase else None,
        plan_alterations=metrics.plan_alterations if metrics else None,
        opportunistic_equity_objective=(
            metrics.opportunistic_equity_objective if metrics else None
        ),
    )


def aggregate_stability_runs(
    runs: Iterable[StabilityRun],
) -> tuple[StabilityAggregate, ...]:
    grouped: dict[tuple[float, int], list[StabilityRun]] = {}
    for run in runs:
        grouped.setdefault(
            (run.time_limit_seconds, run.num_workers),
            [],
        ).append(run)

    aggregates: list[StabilityAggregate] = []
    for (time_limit, num_workers), items in sorted(grouped.items()):
        annual_items = [
            item
            for item in items
            if item.annual_phase_status in {"FEASIBLE", "OPTIMAL"}
        ]
        change_items = [
            item
            for item in items
            if item.change_phase_status in {"FEASIBLE", "OPTIMAL"}
        ]
        tiebreak_items = [
            item
            for item in items
            if item.tiebreak_phase_status in {"FEASIBLE", "OPTIMAL"}
        ]
        stability_items = [
            item
            for item in items
            if item.stability_phase_status in {"FEASIBLE", "OPTIMAL"}
        ]
        equity_items = [
            item
            for item in items
            if item.equity_phase_status in {"FEASIBLE", "OPTIMAL"}
        ]
        annual_values = [
            item.annual_hours_range
            for item in annual_items
            if item.annual_hours_range is not None
        ]
        zone_values = [
            item.zone_rate_range_points
            for item in change_items
            if item.zone_rate_range_points is not None
        ]
        turn_values = [
            item.turn_rate_range_points
            for item in change_items
            if item.turn_rate_range_points is not None
        ]
        annual_gaps = [
            item.annual_phase_gap
            for item in annual_items
            if item.annual_phase_gap is not None
        ]
        annual_objectives = {
            item.annual_fairness_objective
            for item in annual_items
            if item.annual_fairness_objective is not None
        }
        change_objectives = {
            item.change_fairness_objective
            for item in change_items
            if item.change_fairness_objective is not None
        }
        plan_alterations = [
            item.plan_alterations
            for item in stability_items
            if item.plan_alterations is not None
        ]
        equity_objectives = {
            item.opportunistic_equity_objective
            for item in equity_items
            if item.opportunistic_equity_objective is not None
        }
        aggregates.append(
            StabilityAggregate(
                time_limit_seconds=time_limit,
                num_workers=num_workers,
                runs=len(items),
                seeds=tuple(sorted({item.seed for item in items})),
                repetitions=max(item.repetition for item in items),
                coverage_min=min(item.covered for item in items),
                coverage_max=max(item.covered for item in items),
                coverage_all_optimal=all(
                    item.coverage_phase_status == "OPTIMAL"
                    and (item.coverage_gap or 0) == 0
                    for item in items
                ),
                annual_solved_runs=len(annual_items),
                change_solved_runs=len(change_items),
                tiebreak_solved_runs=len(tiebreak_items),
                annual_hours_min=min(annual_values) if annual_values else None,
                annual_hours_max=max(annual_values) if annual_values else None,
                annual_hours_mean=mean(annual_values) if annual_values else None,
                annual_hours_stddev=(
                    pstdev(annual_values) if annual_values else None
                ),
                annual_gap_min=min(annual_gaps) if annual_gaps else None,
                annual_gap_max=max(annual_gaps) if annual_gaps else None,
                zone_rate_min=min(zone_values) if zone_values else None,
                zone_rate_max=max(zone_values) if zone_values else None,
                turn_rate_min=min(turn_values) if turn_values else None,
                turn_rate_max=max(turn_values) if turn_values else None,
                unique_annual_objectives=len(annual_objectives),
                unique_change_objectives=len(change_objectives),
                unique_assignment_plans=len(
                    {item.assignment_fingerprint for item in items}
                ),
                stability_solved_runs=len(stability_items),
                equity_solved_runs=len(equity_items),
                plan_alterations_min=(
                    min(plan_alterations) if plan_alterations else None
                ),
                plan_alterations_max=(
                    max(plan_alterations) if plan_alterations else None
                ),
                unique_equity_objectives=len(equity_objectives),
            )
        )
    return tuple(aggregates)

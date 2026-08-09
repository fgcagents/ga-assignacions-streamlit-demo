from __future__ import annotations

from dataclasses import dataclass
from datetime import time, timedelta

from ortools.sat.python import cp_model

from ...domain import (
    HistoricalAssignment,
    Need,
    PlanningProblem,
    Worker,
)
from ..types import CoreModel, SoftObjectiveWeights


@dataclass(slots=True)
class OperationalComponents:
    workers: tuple[Worker, ...]
    load_vars: dict[str, cp_model.IntVar]
    zone_change_vars: dict[str, cp_model.IntVar]
    turn_change_vars: dict[str, cp_model.IntVar]
    zone_assignment_vars: list[cp_model.IntVar]
    turn_assignment_vars: list[cp_model.IntVar]
    consecutive_excess_vars: list[cp_model.IntVar]
    preferred_violation_vars: list[cp_model.IntVar]
    plan_alterations: cp_model.IntVar
    preferred_assignment_violations: cp_model.IntVar
    consecutive_excess: cp_model.IntVar
    friday_violation: cp_model.IntVar
    zone_changes: cp_model.IntVar
    turn_changes: cp_model.IntVar
    operational_penalty: cp_model.IntVar


def aggregate_count(
    model: cp_model.CpModel,
    variables: list[cp_model.IntVar],
    upper_bound: int,
    name: str,
) -> cp_model.IntVar:
    result = model.new_int_var(0, max(0, upper_bound), name)
    model.add(result == sum(variables) if variables else result == 0)
    return result


def is_zone_change(worker: Worker, need: Need) -> bool:
    return bool(
        worker.home_zone
        and need.zone
        and worker.home_zone != need.zone
    )


def is_turn_change(worker: Worker, need: Need) -> bool:
    return bool(
        worker.turn_options
        and need.turn_options
        and worker.turn_options.isdisjoint(need.turn_options)
    )


def _build_consecutive_day_penalties(
    problem: PlanningProblem,
    core: CoreModel,
    workers: tuple[Worker, ...],
    history_by_worker: dict[str, tuple[HistoricalAssignment, ...]],
) -> list[cp_model.IntVar]:
    if not problem.needs:
        return []
    model = core.model
    min_day = min(need.date for need in problem.needs)
    max_day = max(need.date for need in problem.needs)
    historical_days = {
        worker_id: {
            assignment.start.date() for assignment in assignments
        }
        for worker_id, assignments in history_by_worker.items()
    }
    penalties: list[cp_model.IntVar] = []

    for worker in workers:
        worked_day_vars: dict[object, cp_model.IntVar] = {}
        day = min_day - timedelta(days=9)
        while day <= max_day + timedelta(days=9):
            worked = model.new_bool_var(
                f"worked__{worker.id}__{day.isoformat()}"
            )
            if day in historical_days.get(worker.id, set()):
                model.add(worked == 1)
            else:
                variables = core.vars_by_worker_day.get(
                    (worker.id, day), []
                )
                model.add(
                    worked == sum(variables)
                    if variables
                    else worked == 0
                )
            worked_day_vars[day] = worked
            day += timedelta(days=1)

        window_start = min_day - timedelta(days=9)
        while window_start <= max_day:
            window = [
                worked_day_vars[window_start + timedelta(days=offset)]
                for offset in range(10)
            ]
            violation = model.new_bool_var(
                "consecutive_excess__"
                f"{worker.id}__{window_start.isoformat()}"
            )
            model.add(sum(window) <= 9 + violation)
            penalties.append(violation)
            window_start += timedelta(days=1)
    return penalties


def _violates_friday_rule(worker: Worker, need: Need) -> bool:
    if need.date.weekday() != 4:
        return False
    saturday = need.date + timedelta(days=1)
    sunday = need.date + timedelta(days=2)
    if saturday not in worker.rest_dates or sunday not in worker.rest_dates:
        return False
    crosses_midnight = need.end.date() > need.date
    return crosses_midnight or need.end.time() > time(22, 0)


def build_operational_components(
    problem: PlanningProblem,
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
    history_by_worker: dict[str, tuple[HistoricalAssignment, ...]],
    *,
    coverage_target: int,
    weights: SoftObjectiveWeights,
) -> OperationalComponents:
    """Construeix estabilitat i preferències operatives."""
    model = core.model
    workers = tuple(
        worker for worker in problem.workers if worker.group == "T"
    )
    max_load = max(coverage_target, 1)
    load_vars: dict[str, cp_model.IntVar] = {}
    zone_change_vars: dict[str, cp_model.IntVar] = {}
    turn_change_vars: dict[str, cp_model.IntVar] = {}
    zone_assignment_vars: list[cp_model.IntVar] = []
    turn_assignment_vars: list[cp_model.IntVar] = []

    for worker in workers:
        worker_need_ids = tuple(
            dict.fromkeys(core.need_ids_by_worker.get(worker.id, []))
        )
        worker_vars = [
            core.assignment_vars[(worker.id, need_id)]
            for need_id in worker_need_ids
        ]
        load = model.new_int_var(0, max_load, f"load__{worker.id}")
        model.add(load == sum(worker_vars) if worker_vars else load == 0)
        load_vars[worker.id] = load

        worker_zone_vars = [
            core.assignment_vars[(worker.id, need_id)]
            for need_id in worker_need_ids
            if is_zone_change(worker, needs_by_id[need_id])
        ]
        zone_change_vars[worker.id] = aggregate_count(
            model,
            worker_zone_vars,
            max_load,
            f"zone_changes__{worker.id}",
        )
        zone_assignment_vars.extend(worker_zone_vars)

        worker_turn_vars = [
            core.assignment_vars[(worker.id, need_id)]
            for need_id in worker_need_ids
            if is_turn_change(worker, needs_by_id[need_id])
        ]
        turn_change_vars[worker.id] = aggregate_count(
            model,
            worker_turn_vars,
            max_load,
            f"turn_changes__{worker.id}",
        )
        turn_assignment_vars.extend(worker_turn_vars)

    consecutive_excess_vars = _build_consecutive_day_penalties(
        problem, core, workers, history_by_worker
    )
    consecutive_excess = aggregate_count(
        model,
        consecutive_excess_vars,
        len(consecutive_excess_vars),
        "consecutive_excess_total",
    )

    friday_bad_vars = [
        variable
        for (worker_id, need_id), variable in core.assignment_vars.items()
        if _violates_friday_rule(
            workers_by_id[worker_id], needs_by_id[need_id]
        )
    ]
    friday_violation = model.new_bool_var("friday_violation")
    if friday_bad_vars:
        model.add_max_equality(friday_violation, friday_bad_vars)
    else:
        model.add(friday_violation == 0)

    preferred_violation_vars: list[cp_model.IntVar] = []
    for need_id, worker_id in problem.preferred_assignments:
        violation = model.new_bool_var(
            f"preferred_violation__{need_id}__{worker_id}"
        )
        preferred_variable = core.assignment_vars.get(
            (worker_id, need_id)
        )
        if preferred_variable is None:
            model.add(violation == 1)
        else:
            model.add(violation + preferred_variable == 1)
        preferred_violation_vars.append(violation)
    preferred_assignment_violations = aggregate_count(
        model,
        preferred_violation_vars,
        len(preferred_violation_vars),
        "preferred_assignment_violations",
    )

    zone_changes = aggregate_count(
        model,
        zone_assignment_vars,
        len(zone_assignment_vars),
        "zone_changes_total",
    )
    turn_changes = aggregate_count(
        model,
        turn_assignment_vars,
        len(turn_assignment_vars),
        "turn_changes_total",
    )

    stable_references = tuple(
        assignment
        for assignment in problem.reference_assignments
        if assignment.need_id not in problem.affected_need_ids
    )
    preserved_reference_vars = [
        core.assignment_vars[
            (assignment.worker_id, assignment.need_id)
        ]
        for assignment in stable_references
        if (
            assignment.worker_id,
            assignment.need_id,
        ) in core.assignment_vars
    ]
    plan_alterations = model.new_int_var(
        0, len(stable_references), "plan_alterations"
    )
    model.add(
        plan_alterations
        == len(stable_references) - sum(preserved_reference_vars)
    )

    operational_upper = (
        weights.consecutive_days * len(consecutive_excess_vars)
        + weights.friday_rule
        + weights.preferred_assignment * len(preferred_violation_vars)
    )
    operational_penalty = model.new_int_var(
        0, max(operational_upper, 0), "operational_penalty"
    )
    model.add(
        operational_penalty
        == weights.consecutive_days * consecutive_excess
        + weights.friday_rule * friday_violation
        + weights.preferred_assignment
        * preferred_assignment_violations
    )

    return OperationalComponents(
        workers=workers,
        load_vars=load_vars,
        zone_change_vars=zone_change_vars,
        turn_change_vars=turn_change_vars,
        zone_assignment_vars=zone_assignment_vars,
        turn_assignment_vars=turn_assignment_vars,
        consecutive_excess_vars=consecutive_excess_vars,
        preferred_violation_vars=preferred_violation_vars,
        plan_alterations=plan_alterations,
        preferred_assignment_violations=preferred_assignment_violations,
        consecutive_excess=consecutive_excess,
        friday_violation=friday_violation,
        zone_changes=zone_changes,
        turn_changes=turn_changes,
        operational_penalty=operational_penalty,
    )

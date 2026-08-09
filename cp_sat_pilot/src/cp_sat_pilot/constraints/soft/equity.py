from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from ...domain import Need, PlanningProblem, Worker
from ..types import (
    RATE_SCALE,
    CoreModel,
    SoftComponents,
    SoftObjectiveWeights,
)
from .operational import OperationalComponents, aggregate_count


def _build_annual_hours_penalty(
    model: cp_model.CpModel,
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
) -> tuple[cp_model.IntVar, cp_model.IntVar, cp_model.IntVar]:
    eligible_ids = tuple(
        worker_id
        for worker_id in workers_by_id
        if core.need_ids_by_worker.get(worker_id)
    )
    if not eligible_ids:
        penalty = model.new_int_var(
            0, 0, "annual_hours_equity_penalty"
        )
        hours_range = model.new_int_var(
            0, 0, "annual_hours_range_minutes"
        )
        scaled_range = model.new_int_var(
            0, 0, "annual_hours_range_scaled"
        )
        return penalty, hours_range, scaled_range

    annual_totals: list[cp_model.IntVar] = []
    reference_minutes = max(
        workers_by_id[worker_id].max_annual_minutes
        for worker_id in eligible_ids
    )
    for worker_id in eligible_ids:
        worker = workers_by_id[worker_id]
        need_ids = tuple(
            dict.fromkeys(core.need_ids_by_worker[worker_id])
        )
        total = model.new_int_var(
            0,
            worker.max_annual_minutes,
            f"annual_minutes__{worker_id}",
        )
        model.add(
            total
            == worker.annual_minutes
            + sum(
                needs_by_id[need_id].duration_minutes
                * core.assignment_vars[(worker_id, need_id)]
                for need_id in need_ids
            )
        )
        annual_totals.append(total)

    maximum = model.new_int_var(
        0, reference_minutes, "annual_hours_maximum"
    )
    minimum = model.new_int_var(
        0, reference_minutes, "annual_hours_minimum"
    )
    model.add_max_equality(maximum, annual_totals)
    model.add_min_equality(minimum, annual_totals)
    hours_range = model.new_int_var(
        0, reference_minutes, "annual_hours_range_minutes"
    )
    model.add(hours_range == maximum - minimum)

    range_numerator = model.new_int_var(
        0,
        reference_minutes * RATE_SCALE,
        "annual_hours_range_numerator",
    )
    model.add(range_numerator == hours_range * RATE_SCALE)
    scaled_range = model.new_int_var(
        0, RATE_SCALE, "annual_hours_range_scaled"
    )
    model.add_division_equality(
        scaled_range,
        range_numerator,
        max(1, reference_minutes),
    )

    worker_count = len(annual_totals)
    total_minutes = sum(annual_totals)
    deviations: list[cp_model.IntVar] = []
    for worker_id, annual_total in zip(eligible_ids, annual_totals):
        absolute = model.new_int_var(
            0,
            worker_count * reference_minutes,
            f"annual_hours_abs__{worker_id}",
        )
        model.add_abs_equality(
            absolute,
            worker_count * annual_total - total_minutes,
        )
        numerator = model.new_int_var(
            0,
            worker_count * reference_minutes * RATE_SCALE,
            f"annual_hours_num__{worker_id}",
        )
        model.add(numerator == absolute * RATE_SCALE)
        scaled = model.new_int_var(
            0, RATE_SCALE, f"annual_hours_scaled__{worker_id}"
        )
        model.add_division_equality(
            scaled,
            numerator,
            max(1, worker_count * reference_minutes),
        )
        deviations.append(scaled)

    raw_penalty = model.new_int_var(
        0,
        2 * worker_count * RATE_SCALE,
        "annual_hours_equity_raw",
    )
    model.add(
        raw_penalty == worker_count * scaled_range + sum(deviations)
    )
    penalty = model.new_int_var(
        0, RATE_SCALE, "annual_hours_equity_penalty"
    )
    model.add_division_equality(
        penalty,
        raw_penalty,
        max(1, 2 * worker_count),
    )
    return penalty, hours_range, scaled_range


def _build_accumulated_equity_penalty(
    model: cp_model.CpModel,
    problem: PlanningProblem,
    workers: tuple[Worker, ...],
    load_vars: dict[str, cp_model.IntVar],
    new_change_vars: dict[str, cp_model.IntVar],
    *,
    change_type: str,
) -> tuple[cp_model.IntVar, cp_model.IntVar]:
    groups: dict[tuple[object, ...], list[Worker]] = defaultdict(list)
    for worker in workers:
        eligibility_signature = frozenset(
            (need.service_id, tuple(sorted(need.required_skills)))
            for need in problem.needs
            if need.required_skills.intersection(worker.skills)
        )
        if not eligibility_signature:
            continue
        structural_profile: object
        if change_type == "zone":
            structural_profile = worker.home_zone
        else:
            structural_profile = tuple(sorted(worker.turn_options))
        groups[(eligibility_signature, structural_profile)].append(worker)

    rate_vars: dict[str, cp_model.IntVar] = {}
    rate_upper_bounds: dict[str, int] = {}
    for worker in workers:
        historic_assignments = worker.historical_assignments
        historic_changes = (
            worker.historical_zone_changes
            if change_type == "zone"
            else worker.historical_turn_changes
        )
        max_assignments = historic_assignments + len(problem.needs)

        total_assignments = model.new_int_var(
            0,
            max(max_assignments, 0),
            f"{change_type}_acc_assign__{worker.id}",
        )
        model.add(
            total_assignments
            == historic_assignments + load_vars[worker.id]
        )
        has_assignments = model.new_bool_var(
            f"{change_type}_has_assign__{worker.id}"
        )
        model.add(total_assignments >= 1).only_enforce_if(
            has_assignments
        )
        model.add(total_assignments == 0).only_enforce_if(
            has_assignments.Not()
        )
        safe_denominator = model.new_int_var(
            1,
            max(1, max_assignments),
            f"{change_type}_safe_den__{worker.id}",
        )
        model.add(
            safe_denominator == total_assignments + 1 - has_assignments
        )

        total_changes = model.new_int_var(
            0,
            max(max_assignments, 0),
            f"{change_type}_acc_changes__{worker.id}",
        )
        model.add(
            total_changes
            == historic_changes + new_change_vars[worker.id]
        )
        model.add(total_changes <= total_assignments)
        numerator = model.new_int_var(
            0,
            max(max_assignments * RATE_SCALE, 0),
            f"{change_type}_rate_num__{worker.id}",
        )
        model.add(numerator == total_changes * RATE_SCALE)
        rate_upper = RATE_SCALE
        rate = model.new_int_var(
            0,
            rate_upper,
            f"{change_type}_rate__{worker.id}",
        )
        model.add_division_equality(rate, numerator, safe_denominator)
        rate_vars[worker.id] = rate
        rate_upper_bounds[worker.id] = rate_upper

    group_penalties: list[cp_model.IntVar] = []
    group_penalty_upper_bounds: list[int] = []
    group_rate_ranges: list[cp_model.IntVar] = []
    normalization_denominator = 0
    for group_index, members in enumerate(groups.values()):
        if len(members) < 2:
            continue
        size = len(members)
        member_rates = [rate_vars[worker.id] for worker in members]
        sum_rates = sum(member_rates)
        max_rate = max(
            rate_upper_bounds[worker.id] for worker in members
        )
        group_excesses: list[cp_model.IntVar] = []
        for worker in members:
            excess_upper = size * max_rate
            excess = model.new_int_var(
                0,
                excess_upper,
                f"{change_type}_positive_excess__"
                f"{group_index}__{worker.id}",
            )
            model.add(
                excess >= size * rate_vars[worker.id] - sum_rates
            )
            group_excesses.append(excess)

        maximum_rate = model.new_int_var(
            0,
            max_rate,
            f"{change_type}_max_rate__{group_index}",
        )
        minimum_rate = model.new_int_var(
            0,
            max_rate,
            f"{change_type}_min_rate__{group_index}",
        )
        model.add_max_equality(maximum_rate, member_rates)
        model.add_min_equality(minimum_rate, member_rates)
        rate_range = model.new_int_var(
            0,
            max_rate,
            f"{change_type}_rate_range__{group_index}",
        )
        model.add(rate_range == maximum_rate - minimum_rate)
        group_rate_ranges.append(rate_range)
        group_upper = size * max_rate + sum(
            size * max_rate for _ in group_excesses
        )
        group_penalty = model.new_int_var(
            0,
            group_upper,
            f"{change_type}_group_equity__{group_index}",
        )
        model.add(
            group_penalty == size * rate_range + sum(group_excesses)
        )
        group_penalties.append(group_penalty)
        group_penalty_upper_bounds.append(group_upper)
        normalization_denominator += size * (size + 1)

    upper = sum(group_penalty_upper_bounds)
    raw_penalty = aggregate_count(
        model,
        group_penalties,
        upper,
        f"accumulated_{change_type}_equity_raw",
    )
    if normalization_denominator == 0:
        penalty = model.new_int_var(
            0,
            0,
            f"accumulated_{change_type}_equity_penalty",
        )
        maximum_range = model.new_int_var(
            0,
            0,
            f"accumulated_{change_type}_rate_range",
        )
        return penalty, maximum_range
    penalty = model.new_int_var(
        0,
        RATE_SCALE,
        f"accumulated_{change_type}_equity_penalty",
    )
    model.add_division_equality(
        penalty,
        raw_penalty,
        normalization_denominator,
    )
    maximum_range = model.new_int_var(
        0,
        RATE_SCALE,
        f"accumulated_{change_type}_rate_range",
    )
    model.add_max_equality(maximum_range, group_rate_ranges)
    return penalty, maximum_range


def build_equity_components(
    problem: PlanningProblem,
    core: CoreModel,
    operational: OperationalComponents,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
    weights: SoftObjectiveWeights,
) -> SoftComponents:
    """Construeix l'equitat oportunista sense decidir-ne la prioritat."""
    model = core.model
    (
        annual_hours_equity_penalty,
        annual_hours_range_minutes,
        annual_hours_range_scaled,
    ) = _build_annual_hours_penalty(
        model, core, workers_by_id, needs_by_id
    )
    (
        accumulated_zone_equity_penalty,
        accumulated_zone_rate_range,
    ) = _build_accumulated_equity_penalty(
        model,
        problem,
        operational.workers,
        operational.load_vars,
        operational.zone_change_vars,
        change_type="zone",
    )
    (
        accumulated_turn_equity_penalty,
        accumulated_turn_rate_range,
    ) = _build_accumulated_equity_penalty(
        model,
        problem,
        operational.workers,
        operational.load_vars,
        operational.turn_change_vars,
        change_type="turn",
    )

    annual_secondary_upper = weights.annual_hours_balance * RATE_SCALE
    maximum_annual_minutes = max(
        (worker.max_annual_minutes for worker in operational.workers),
        default=0,
    )
    annual_fairness_upper = (
        maximum_annual_minutes * (annual_secondary_upper + 1)
        + annual_secondary_upper
    )
    annual_fairness_objective = model.new_int_var(
        0,
        max(0, annual_fairness_upper),
        "annual_fairness_objective",
    )
    model.add(
        annual_fairness_objective
        == annual_hours_range_minutes * (annual_secondary_upper + 1)
        + weights.annual_hours_balance * annual_hours_equity_penalty
    )

    change_secondary_upper = (
        weights.accumulated_zone_equity
        + weights.accumulated_turn_equity
    ) * RATE_SCALE
    worst_change_equity_gap = model.new_int_var(
        0, RATE_SCALE, "worst_change_equity_gap"
    )
    model.add_max_equality(
        worst_change_equity_gap,
        [accumulated_zone_rate_range, accumulated_turn_rate_range],
    )
    change_fairness_upper = (
        RATE_SCALE * (change_secondary_upper + 1)
        + change_secondary_upper
    )
    change_fairness_objective = model.new_int_var(
        0, change_fairness_upper, "change_fairness_objective"
    )
    model.add(
        change_fairness_objective
        == worst_change_equity_gap * (change_secondary_upper + 1)
        + weights.accumulated_zone_equity
        * accumulated_zone_equity_penalty
        + weights.accumulated_turn_equity
        * accumulated_turn_equity_penalty
    )

    change_tiebreak_upper = (
        weights.zone_changes_tiebreak
        * len(operational.zone_assignment_vars)
        + weights.turn_changes_tiebreak
        * len(operational.turn_assignment_vars)
    )
    change_tiebreak_penalty = model.new_int_var(
        0,
        max(0, change_tiebreak_upper),
        "change_tiebreak_penalty",
    )
    model.add(
        change_tiebreak_penalty
        == weights.zone_changes_tiebreak * operational.zone_changes
        + weights.turn_changes_tiebreak * operational.turn_changes
    )
    normalized_total_changes = model.new_int_var(
        0,
        RATE_SCALE if change_tiebreak_upper else 0,
        "normalized_total_changes",
    )
    if change_tiebreak_upper:
        change_numerator = model.new_int_var(
            0,
            change_tiebreak_upper * RATE_SCALE,
            "normalized_total_changes_numerator",
        )
        model.add(
            change_numerator == change_tiebreak_penalty * RATE_SCALE
        )
        model.add_division_equality(
            normalized_total_changes,
            change_numerator,
            change_tiebreak_upper,
        )
    else:
        model.add(normalized_total_changes == 0)

    opportunistic_equity_upper = (
        weights.annual_hours_balance
        + weights.accumulated_zone_equity
        + weights.accumulated_turn_equity
        + 1
    ) * RATE_SCALE
    opportunistic_equity_objective = model.new_int_var(
        0,
        opportunistic_equity_upper,
        "opportunistic_equity_objective",
    )
    model.add(
        opportunistic_equity_objective
        == weights.annual_hours_balance * annual_hours_equity_penalty
        + weights.accumulated_zone_equity
        * accumulated_zone_equity_penalty
        + weights.accumulated_turn_equity
        * accumulated_turn_equity_penalty
        + normalized_total_changes
    )

    return SoftComponents(
        plan_alterations=operational.plan_alterations,
        preferred_assignment_violations=(
            operational.preferred_assignment_violations
        ),
        consecutive_excess=operational.consecutive_excess,
        friday_violation=operational.friday_violation,
        zone_changes=operational.zone_changes,
        turn_changes=operational.turn_changes,
        annual_hours_range_minutes=annual_hours_range_minutes,
        annual_hours_range_scaled=annual_hours_range_scaled,
        annual_hours_equity_penalty=annual_hours_equity_penalty,
        accumulated_zone_rate_range=accumulated_zone_rate_range,
        accumulated_turn_rate_range=accumulated_turn_rate_range,
        accumulated_zone_equity_penalty=(
            accumulated_zone_equity_penalty
        ),
        accumulated_turn_equity_penalty=(
            accumulated_turn_equity_penalty
        ),
        worst_change_equity_gap=worst_change_equity_gap,
        operational_penalty=operational.operational_penalty,
        annual_fairness_objective=annual_fairness_objective,
        change_fairness_objective=change_fairness_objective,
        change_tiebreak_penalty=change_tiebreak_penalty,
        normalized_total_changes=normalized_total_changes,
        opportunistic_equity_objective=opportunistic_equity_objective,
    )

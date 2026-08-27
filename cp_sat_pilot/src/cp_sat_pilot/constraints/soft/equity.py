from __future__ import annotations

from ortools.sat.python import cp_model

from ...domain import Need, Worker
from ..types import RATE_SCALE, CoreModel, SoftComponents, SoftObjectiveWeights
from .operational import OperationalComponents


def _zero(model: cp_model.CpModel, name: str) -> cp_model.IntVar:
    return model.new_int_var(0, 0, name)


def _build_hours_components(
    model: cp_model.CpModel,
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
) -> tuple[cp_model.IntVar, cp_model.IntVar, int]:
    """Compara el progrés respecte de la referència contractual ajustada."""

    eligible_ids = tuple(
        worker_id
        for worker_id, worker in workers_by_id.items()
        if worker.group == "T"
        and worker.annual_equity_target_minutes > 0
        and core.need_ids_by_worker.get(worker_id)
    )
    if not eligible_ids:
        return (
            _zero(model, "annual_hours_range_minutes"),
            _zero(model, "adjusted_annual_rate_range"),
            0,
        )

    totals: list[cp_model.IntVar] = []
    rates: list[cp_model.IntVar] = []
    rate_bounds: list[int] = []
    maximum_minutes = max(
        workers_by_id[worker_id].max_annual_minutes
        for worker_id in eligible_ids
    )

    for worker_id in eligible_ids:
        worker = workers_by_id[worker_id]
        need_ids = tuple(dict.fromkeys(core.need_ids_by_worker[worker_id]))
        total = model.new_int_var(
            0,
            worker.max_annual_minutes,
            f"annual_total_minutes__{worker_id}",
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
        totals.append(total)

        target = worker.annual_equity_target_minutes
        rate_bound = worker.max_annual_minutes * RATE_SCALE // target
        numerator = model.new_int_var(
            0,
            worker.max_annual_minutes * RATE_SCALE,
            f"contractual_progress_num__{worker_id}",
        )
        model.add(numerator == total * RATE_SCALE)
        rate = model.new_int_var(
            0,
            rate_bound,
            f"contractual_progress_rate__{worker_id}",
        )
        model.add_division_equality(rate, numerator, target)
        rates.append(rate)
        rate_bounds.append(rate_bound)

    maximum = model.new_int_var(
        0, maximum_minutes, "annual_hours_maximum"
    )
    minimum = model.new_int_var(
        0, maximum_minutes, "annual_hours_minimum"
    )
    model.add_max_equality(maximum, totals)
    model.add_min_equality(minimum, totals)
    hours_range = model.new_int_var(
        0, maximum_minutes, "annual_hours_range_minutes"
    )
    model.add(hours_range == maximum - minimum)

    rate_bound = max(rate_bounds)
    maximum_rate = model.new_int_var(
        0, rate_bound, "adjusted_annual_rate_maximum"
    )
    minimum_rate = model.new_int_var(
        0, rate_bound, "adjusted_annual_rate_minimum"
    )
    model.add_max_equality(maximum_rate, rates)
    model.add_min_equality(minimum_rate, rates)
    adjusted_range = model.new_int_var(
        0, rate_bound, "adjusted_annual_rate_range"
    )
    model.add(adjusted_range == maximum_rate - minimum_rate)
    return hours_range, adjusted_range, rate_bound


def _build_night_maximum(
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
) -> tuple[cp_model.IntVar, int]:
    model = core.model
    accumulated: list[cp_model.IntVar] = []
    bounds: list[int] = []
    for worker_id, worker in workers_by_id.items():
        if worker.group != "T" or not worker.can_work_nights:
            continue
        night_need_ids = tuple(
            need_id
            for need_id in dict.fromkeys(
                core.need_ids_by_worker.get(worker_id, ())
            )
            if needs_by_id[need_id].is_night
        )
        if not night_need_ids:
            continue
        bound = worker.historical_night_services + len(night_need_ids)
        total = model.new_int_var(
            0,
            bound,
            f"accumulated_night_services__{worker_id}",
        )
        model.add(
            total
            == worker.historical_night_services
            + sum(
                core.assignment_vars[(worker_id, need_id)]
                for need_id in night_need_ids
            )
        )
        accumulated.append(total)
        bounds.append(bound)

    if not accumulated:
        return _zero(model, "max_accumulated_night_services"), 0
    bound = max(bounds)
    maximum = model.new_int_var(
        0, bound, "max_accumulated_night_services"
    )
    model.add_max_equality(maximum, accumulated)
    return maximum, bound


def build_equity_components(
    core: CoreModel,
    operational: OperationalComponents,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
    weights: SoftObjectiveWeights,
) -> SoftComponents:
    """Construeix una fase social lexicogràfica sense barrejar unitats."""

    model = core.model
    hours_range, adjusted_range, adjusted_bound = _build_hours_components(
        model, core, workers_by_id, needs_by_id
    )
    change_bound = (
        weights.zone_changes_tiebreak
        * len(operational.zone_assignment_vars)
        + weights.turn_changes_tiebreak
        * len(operational.turn_assignment_vars)
    )
    weighted_changes = model.new_int_var(
        0, max(0, change_bound), "change_tiebreak_penalty"
    )
    model.add(
        weighted_changes
        == weights.zone_changes_tiebreak * operational.zone_changes
        + weights.turn_changes_tiebreak * operational.turn_changes
    )
    night_max, night_bound = _build_night_maximum(
        core, workers_by_id, needs_by_id
    )
    social_bound = adjusted_bound
    for lower_bound in (change_bound, night_bound):
        social_bound = social_bound * (lower_bound + 1) + lower_bound
    social_objective = model.new_int_var(
        0, social_bound, "social_objective"
    )
    model.add(
        social_objective
        == (
            adjusted_range * (change_bound + 1)
            + weighted_changes
        )
        * (night_bound + 1)
        + night_max
    )

    zone_equity = _zero(model, "accumulated_zone_equity_penalty")
    turn_equity = _zero(model, "accumulated_turn_equity_penalty")
    zone_rate = _zero(model, "accumulated_zone_rate_range")
    turn_rate = _zero(model, "accumulated_turn_rate_range")
    worst_change = _zero(model, "worst_change_equity_gap")

    return SoftComponents(
        plan_alterations=operational.plan_alterations,
        preferred_assignment_violations=(
            operational.preferred_assignment_violations
        ),
        consecutive_excess=operational.consecutive_excess,
        friday_violation=operational.friday_violation,
        zone_changes=operational.zone_changes,
        turn_changes=operational.turn_changes,
        annual_hours_range_minutes=hours_range,
        adjusted_annual_rate_range=adjusted_range,
        annual_hours_equity_penalty=adjusted_range,
        accumulated_zone_rate_range=zone_rate,
        accumulated_turn_rate_range=turn_rate,
        accumulated_zone_equity_penalty=zone_equity,
        accumulated_turn_equity_penalty=turn_equity,
        worst_change_equity_gap=worst_change,
        operational_penalty=operational.operational_penalty,
        annual_fairness_objective=adjusted_range,
        change_fairness_objective=social_objective,
        change_tiebreak_penalty=weighted_changes,
        normalized_total_changes=weighted_changes,
        opportunistic_equity_objective=social_objective,
        outside_preference_services=operational.preference_exceptions,
        max_accumulated_night_services=night_max,
        social_objective=social_objective,
    )

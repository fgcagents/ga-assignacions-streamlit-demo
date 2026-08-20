from __future__ import annotations

from ortools.sat.python import cp_model

from ...domain import Need, PlanningProblem, Worker
from ..types import RATE_SCALE, CoreModel, SoftComponents, SoftObjectiveWeights
from .operational import OperationalComponents


def _zero(model: cp_model.CpModel, name: str) -> cp_model.IntVar:
    return model.new_int_var(0, 0, name)


def _build_adjusted_hours_range(
    model: cp_model.CpModel,
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
) -> tuple[cp_model.IntVar, cp_model.IntVar]:
    """Mesura una única equitat: progrés respecte del 75 % contractual.

    ``annual_equity_target_minutes`` és la referència comuna del grup T
    (75 % de 1.605 hores), prorratejada només per les baixes pròpies. No és
    una obligació de jornada ni un sostre: serveix per comparar el progrés
    relatiu de treballadors amb disponibilitats diferents.
    """

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
        )

    annual_totals: list[cp_model.IntVar] = []
    completion_rates: list[cp_model.IntVar] = []
    rate_bounds: list[int] = []
    maximum_minutes = max(
        workers_by_id[worker_id].max_annual_minutes
        for worker_id in eligible_ids
    )

    for worker_id in eligible_ids:
        worker = workers_by_id[worker_id]
        need_ids = tuple(
            dict.fromkeys(core.need_ids_by_worker.get(worker_id, ()))
        )
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
        annual_totals.append(total)

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
        completion_rates.append(rate)
        rate_bounds.append(rate_bound)

    maximum = model.new_int_var(
        0, maximum_minutes, "annual_hours_maximum"
    )
    minimum = model.new_int_var(
        0, maximum_minutes, "annual_hours_minimum"
    )
    model.add_max_equality(maximum, annual_totals)
    model.add_min_equality(minimum, annual_totals)
    hours_range = model.new_int_var(
        0, maximum_minutes, "annual_hours_range_minutes"
    )
    model.add(hours_range == maximum - minimum)

    maximum_rate_bound = max(rate_bounds)
    maximum_rate = model.new_int_var(
        0, maximum_rate_bound, "adjusted_annual_rate_maximum"
    )
    minimum_rate = model.new_int_var(
        0, maximum_rate_bound, "adjusted_annual_rate_minimum"
    )
    model.add_max_equality(maximum_rate, completion_rates)
    model.add_min_equality(minimum_rate, completion_rates)
    adjusted_range = model.new_int_var(
        0, maximum_rate_bound, "adjusted_annual_rate_range"
    )
    model.add(adjusted_range == maximum_rate - minimum_rate)
    return hours_range, adjusted_range


def build_equity_components(
    problem: PlanningProblem,
    core: CoreModel,
    operational: OperationalComponents,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
    weights: SoftObjectiveWeights,
) -> SoftComponents:
    """Construeix l'objectiu mínim d'equitat i el desempat de canvis.

    La cobertura es resol fora d'aquest mòdul. Després només es minimitza
    el rang de progrés respecte del 75 % contractual i, com a desempat, el
    nombre total de canvis de torn i zona. Els altres camps es mantenen a zero
    per compatibilitat amb informes i propostes persistides anteriors.
    """

    del problem, weights
    model = core.model
    annual_hours_range, adjusted_range = _build_adjusted_hours_range(
        model, core, workers_by_id, needs_by_id
    )

    change_upper = (
        len(operational.zone_assignment_vars)
        + len(operational.turn_assignment_vars)
    )
    total_changes = model.new_int_var(
        0, max(0, change_upper), "change_tiebreak_penalty"
    )
    model.add(
        total_changes
        == operational.zone_changes + operational.turn_changes
    )

    zero_zone_range = _zero(model, "accumulated_zone_rate_range")
    zero_turn_range = _zero(model, "accumulated_turn_rate_range")
    zero_zone_penalty = _zero(
        model, "accumulated_zone_equity_penalty"
    )
    zero_turn_penalty = _zero(
        model, "accumulated_turn_equity_penalty"
    )
    zero_worst_gap = _zero(model, "worst_change_equity_gap")

    return SoftComponents(
        plan_alterations=operational.plan_alterations,
        preferred_assignment_violations=(
            operational.preferred_assignment_violations
        ),
        consecutive_excess=operational.consecutive_excess,
        friday_violation=operational.friday_violation,
        zone_changes=operational.zone_changes,
        turn_changes=operational.turn_changes,
        annual_hours_range_minutes=annual_hours_range,
        adjusted_annual_rate_range=adjusted_range,
        annual_hours_equity_penalty=adjusted_range,
        accumulated_zone_rate_range=zero_zone_range,
        accumulated_turn_rate_range=zero_turn_range,
        accumulated_zone_equity_penalty=zero_zone_penalty,
        accumulated_turn_equity_penalty=zero_turn_penalty,
        worst_change_equity_gap=zero_worst_gap,
        operational_penalty=operational.operational_penalty,
        annual_fairness_objective=adjusted_range,
        change_fairness_objective=total_changes,
        change_tiebreak_penalty=total_changes,
        normalized_total_changes=total_changes,
        opportunistic_equity_objective=adjusted_range,
    )

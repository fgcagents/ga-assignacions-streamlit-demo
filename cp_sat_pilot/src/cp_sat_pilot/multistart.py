from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .domain import SolveResult
from .model import CpSatPlanner, SolverConfig
from .stability import assignment_fingerprint


SOLVED_STATUSES = {"FEASIBLE", "OPTIMAL"}


class MultiStartSelectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MultiStartCandidate:
    seed: int
    feasible: bool
    status: str
    validation_errors: int
    covered_needs: int
    total_needs: int
    coverage_phase_status: str
    coverage_phase_gap: float | None
    stability_phase_status: str
    stability_phase_gap: float | None
    operational_phase_status: str
    equity_phase_status: str
    equity_phase_gap: float | None
    annual_phase_status: str
    annual_phase_gap: float | None
    change_phase_status: str
    change_phase_gap: float | None
    tiebreak_phase_status: str
    tiebreak_phase_gap: float | None
    operational_penalty: int | None
    annual_fairness_objective: int | None
    change_fairness_objective: int | None
    change_tiebreak_penalty: int | None
    plan_alterations: int | None
    opportunistic_equity_objective: int | None
    annual_hours_range: float | None
    zone_rate_range_points: float | None
    turn_rate_range_points: float | None
    wall_time_seconds: float
    assignment_fingerprint: str


@dataclass(frozen=True, slots=True)
class MultiStartSelection:
    selected_seed: int
    selected_result: SolveResult
    candidates: tuple[MultiStartCandidate, ...]
    stopped_after_first_seed: bool = False

    @property
    def total_wall_time_seconds(self) -> float:
        return sum(candidate.wall_time_seconds for candidate in self.candidates)


def _phase_status(result: SolveResult, name: str) -> str:
    return next(
        (
            phase.status
            for phase in result.optimization_phases
            if phase.name == name
        ),
        "NO_EXECUTADA",
    )


def _phase_gap(result: SolveResult, name: str) -> float | None:
    return next(
        (
            phase.relative_gap
            for phase in result.optimization_phases
            if phase.name == name
        ),
        None,
    )


def summarize_candidate(seed: int, result: SolveResult) -> MultiStartCandidate:
    metrics = result.soft_metrics
    operational_status = _phase_status(result, "preferencies_operatives")
    stability_status = _phase_status(result, "estabilitat_pla")
    equity_status = _phase_status(result, "equitat_oportunista")
    return MultiStartCandidate(
        seed=seed,
        feasible=result.feasible,
        status=result.status,
        validation_errors=len(result.validation_errors),
        covered_needs=result.covered_needs,
        total_needs=result.total_needs,
        coverage_phase_status=_phase_status(result, "cobertura"),
        coverage_phase_gap=_phase_gap(result, "cobertura"),
        stability_phase_status=stability_status,
        stability_phase_gap=_phase_gap(result, "estabilitat_pla"),
        operational_phase_status=operational_status,
        equity_phase_status=equity_status,
        equity_phase_gap=_phase_gap(result, "equitat_oportunista"),
        annual_phase_status=equity_status,
        annual_phase_gap=_phase_gap(result, "equitat_oportunista"),
        change_phase_status=equity_status,
        change_phase_gap=_phase_gap(result, "equitat_oportunista"),
        tiebreak_phase_status=equity_status,
        tiebreak_phase_gap=_phase_gap(result, "equitat_oportunista"),
        operational_penalty=(
            metrics.operational_penalty
            if metrics and operational_status in SOLVED_STATUSES
            else None
        ),
        annual_fairness_objective=(
            metrics.annual_fairness_objective
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        change_fairness_objective=(
            metrics.change_fairness_objective
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        change_tiebreak_penalty=(
            metrics.change_tiebreak_penalty
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        plan_alterations=(
            metrics.plan_alterations
            if metrics and stability_status in SOLVED_STATUSES
            else None
        ),
        opportunistic_equity_objective=(
            metrics.opportunistic_equity_objective
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        annual_hours_range=(
            metrics.annual_hours_range_minutes / 60
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        zone_rate_range_points=(
            metrics.accumulated_zone_rate_range_permille / 10
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        turn_rate_range_points=(
            metrics.accumulated_turn_rate_range_permille / 10
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        wall_time_seconds=result.wall_time_seconds,
        assignment_fingerprint=assignment_fingerprint(result.assignments),
    )


def lexicographic_quality_key(
    candidate: MultiStartCandidate,
) -> tuple[int, ...]:
    """
    Clau ascendent: cobertura màxima i objectius tous mínims.

    Una fase no resolta perd contra una fase resolta només quan totes les
    prioritats anteriors empaten.
    """

    def objective_key(value: int | None) -> tuple[int, int]:
        return (1, 0) if value is None else (0, value)

    return (
        -candidate.covered_needs,
        0 if candidate.coverage_phase_status == "OPTIMAL" else 1,
        *objective_key(candidate.plan_alterations),
        *objective_key(candidate.operational_penalty),
        *objective_key(candidate.opportunistic_equity_objective),
        candidate.seed,
    )


def select_best_result(
    results: Iterable[tuple[int, SolveResult]],
) -> MultiStartSelection:
    pairs = tuple(results)
    if not pairs:
        raise MultiStartSelectionError("No s'ha proporcionat cap execució")
    seeds = [seed for seed, _ in pairs]
    if len(seeds) != len(set(seeds)):
        raise MultiStartSelectionError("Les llavors del mode multillavor han de ser úniques")

    candidates = tuple(
        summarize_candidate(seed, result) for seed, result in pairs
    )
    valid_indexes = [
        index for index, candidate in enumerate(candidates) if candidate.feasible
    ]
    if not valid_indexes:
        raise MultiStartSelectionError(
            "Cap llavor ha produït una proposta factible i validada"
        )
    selected_index = min(
        valid_indexes,
        key=lambda index: lexicographic_quality_key(candidates[index]),
    )
    return MultiStartSelection(
        selected_seed=candidates[selected_index].seed,
        selected_result=pairs[selected_index][1],
        candidates=candidates,
    )


def solve_multi_start(
    planner: CpSatPlanner,
    config: SolverConfig,
    seeds: Iterable[int],
) -> MultiStartSelection:
    unique_seeds = tuple(dict.fromkeys(seeds))
    if not unique_seeds:
        raise MultiStartSelectionError("Cal indicar almenys una llavor")
    results = tuple(
        (
            seed,
            planner.solve(replace(config, random_seed=seed)),
        )
        for seed in unique_seeds
    )
    return select_best_result(results)


def solve_adaptive_multi_start(
    planner: CpSatPlanner,
    config: SolverConfig,
    seeds: Iterable[int],
    *,
    force_all_seeds: bool = False,
) -> MultiStartSelection:
    """
    Executa una sola llavor en el cas normal i amplia la cerca si cal.

    Les llavors restants només s'executen quan la primera proposta no és
    vàlida, no cobreix totes les necessitats, no prova l'òptim de cobertura
    o quan es demanen alternatives explícitament.
    """
    unique_seeds = tuple(dict.fromkeys(seeds))
    if not unique_seeds:
        raise MultiStartSelectionError("Cal indicar almenys una llavor")

    first_seed = unique_seeds[0]
    first_result = planner.solve(replace(config, random_seed=first_seed))
    first_candidate = summarize_candidate(first_seed, first_result)
    coverage_proven = (
        first_candidate.coverage_phase_status == "OPTIMAL"
        and (first_candidate.coverage_phase_gap or 0.0) == 0.0
    )
    needs_more_seeds = (
        force_all_seeds
        or not first_candidate.feasible
        or first_candidate.covered_needs < first_candidate.total_needs
        or not coverage_proven
    )
    if not needs_more_seeds or len(unique_seeds) == 1:
        selection = select_best_result(((first_seed, first_result),))
        return replace(
            selection,
            stopped_after_first_seed=(
                not force_all_seeds and len(unique_seeds) > 1
            ),
        )

    remaining_results = tuple(
        (
            seed,
            planner.solve(replace(config, random_seed=seed)),
        )
        for seed in unique_seeds[1:]
    )
    return select_best_result(
        ((first_seed, first_result), *remaining_results)
    )

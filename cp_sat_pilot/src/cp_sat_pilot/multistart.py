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
    adjusted_annual_rate_range_points: float | None = None
    zone_changes: int | None = None
    turn_changes: int | None = None
    night_minutes: int = 0
    retry_kind: str = "base"
    max_time_seconds: float | None = None
    equity_time_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MultiStartSelection:
    selected_seed: int
    selected_result: SolveResult
    candidates: tuple[MultiStartCandidate, ...]
    stopped_after_first_seed: bool = False
    # Es mantenen per compatibilitat amb propostes i informes antics.
    directed_retry_triggered: bool = False
    directed_retry_seeds: tuple[int, ...] = ()
    initial_selected_seed: int | None = None

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


def summarize_candidate(
    seed: int,
    result: SolveResult,
    *,
    config: SolverConfig | None = None,
) -> MultiStartCandidate:
    metrics = result.soft_metrics
    stability_status = _phase_status(result, "estabilitat_pla")
    equity_status = _phase_status(result, "equitat_hores_contractual")
    changes_status = _phase_status(result, "desempat_canvis")
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
        operational_phase_status=changes_status,
        equity_phase_status=equity_status,
        equity_phase_gap=_phase_gap(result, "equitat_hores_contractual"),
        annual_phase_status=equity_status,
        annual_phase_gap=_phase_gap(result, "equitat_hores_contractual"),
        change_phase_status=changes_status,
        change_phase_gap=_phase_gap(result, "desempat_canvis"),
        tiebreak_phase_status=changes_status,
        tiebreak_phase_gap=_phase_gap(result, "desempat_canvis"),
        operational_penalty=(metrics.operational_penalty if metrics else None),
        annual_fairness_objective=(
            metrics.annual_fairness_objective
            if metrics and equity_status in SOLVED_STATUSES
            else None
        ),
        change_fairness_objective=(
            metrics.change_fairness_objective
            if metrics and changes_status in SOLVED_STATUSES
            else None
        ),
        change_tiebreak_penalty=(
            metrics.change_tiebreak_penalty
            if metrics and changes_status in SOLVED_STATUSES
            else None
        ),
        plan_alterations=(metrics.plan_alterations if metrics else None),
        opportunistic_equity_objective=(
            metrics.opportunistic_equity_objective if metrics else None
        ),
        annual_hours_range=(
            metrics.annual_hours_range_minutes / 60 if metrics else None
        ),
        zone_rate_range_points=None,
        turn_rate_range_points=None,
        wall_time_seconds=result.wall_time_seconds,
        assignment_fingerprint=assignment_fingerprint(result.assignments),
        adjusted_annual_rate_range_points=(
            metrics.adjusted_annual_rate_range_permille / 10
            if metrics
            else None
        ),
        zone_changes=metrics.zone_changes if metrics else None,
        turn_changes=metrics.turn_changes if metrics else None,
        retry_kind="base",
        max_time_seconds=config.max_time_seconds if config else None,
        equity_time_seconds=config.equity_time_seconds if config else None,
    )


def lexicographic_quality_key(
    candidate: MultiStartCandidate,
) -> tuple[object, ...]:
    def objective_key(value: int | None) -> tuple[int, int]:
        return (1, 0) if value is None else (0, value)

    return (
        0 if candidate.feasible and candidate.validation_errors == 0 else 1,
        -candidate.covered_needs,
        0 if candidate.coverage_phase_status == "OPTIMAL" else 1,
        *objective_key(candidate.plan_alterations),
        *objective_key(candidate.annual_fairness_objective),
        *objective_key(candidate.change_tiebreak_penalty),
        candidate.seed,
    )


def select_best_result(
    results: Iterable[tuple[int, SolveResult]],
    *,
    contexts: dict[int, SolverConfig] | None = None,
) -> MultiStartSelection:
    pairs = tuple(results)
    if not pairs:
        raise MultiStartSelectionError("No s'ha proporcionat cap execució")
    seeds = tuple(seed for seed, _ in pairs)
    if len(seeds) != len(set(seeds)):
        raise MultiStartSelectionError(
            "Les llavors del mode multillavor han de ser úniques"
        )

    candidates = tuple(
        summarize_candidate(seed, result, config=(contexts or {}).get(seed))
        for seed, result in pairs
    )
    valid_indexes = [
        index
        for index, candidate in enumerate(candidates)
        if candidate.feasible and candidate.validation_errors == 0
    ]
    if not valid_indexes:
        raise MultiStartSelectionError(
            "Cap llavor ha produït una proposta vàlida"
        )
    selected_index = min(
        valid_indexes,
        key=lambda index: lexicographic_quality_key(candidates[index]),
    )
    return MultiStartSelection(
        selected_seed=candidates[selected_index].seed,
        selected_result=pairs[selected_index][1],
        candidates=candidates,
        initial_selected_seed=candidates[selected_index].seed,
    )


def solve_multi_start(
    planner: CpSatPlanner,
    config: SolverConfig,
    seeds: Iterable[int],
) -> MultiStartSelection:
    unique_seeds = tuple(dict.fromkeys(seeds))
    if not unique_seeds:
        raise MultiStartSelectionError("Cal indicar almenys una llavor")
    contexts = {
        seed: replace(config, random_seed=seed) for seed in unique_seeds
    }
    results = tuple(
        (seed, planner.solve(contexts[seed])) for seed in unique_seeds
    )
    return select_best_result(results, contexts=contexts)


def solve_adaptive_multi_start(
    planner: CpSatPlanner,
    config: SolverConfig,
    seeds: Iterable[int],
    *,
    force_all_seeds: bool = False,
) -> MultiStartSelection:
    """Amplia les llavors només si la cobertura no queda demostrada."""

    unique_seeds = tuple(dict.fromkeys(seeds))
    if not unique_seeds:
        raise MultiStartSelectionError("Cal indicar almenys una llavor")

    first_seed = unique_seeds[0]
    first_config = replace(config, random_seed=first_seed)
    first_result = planner.solve(first_config)
    first_candidate = summarize_candidate(
        first_seed, first_result, config=first_config
    )
    coverage_proven = (
        first_candidate.coverage_phase_status == "OPTIMAL"
        and (first_candidate.coverage_phase_gap or 0.0) == 0.0
    )
    needs_more_seeds = (
        force_all_seeds
        or not first_candidate.feasible
        or first_candidate.validation_errors > 0
        or not coverage_proven
    )
    if not needs_more_seeds or len(unique_seeds) == 1:
        selection = select_best_result(
            ((first_seed, first_result),),
            contexts={first_seed: first_config},
        )
        return replace(
            selection,
            stopped_after_first_seed=(
                not force_all_seeds and len(unique_seeds) > 1
            ),
        )

    contexts = {
        seed: replace(config, random_seed=seed) for seed in unique_seeds
    }
    results = [(first_seed, first_result)]
    results.extend(
        (seed, planner.solve(contexts[seed]))
        for seed in unique_seeds[1:]
    )
    return select_best_result(results, contexts=contexts)

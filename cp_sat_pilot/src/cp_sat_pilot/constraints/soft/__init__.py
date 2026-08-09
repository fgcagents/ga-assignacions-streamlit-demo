from __future__ import annotations

from ...domain import (
    HistoricalAssignment,
    Need,
    PlanningProblem,
    Worker,
)
from ..types import CoreModel, SoftComponents, SoftObjectiveWeights
from .equity import build_equity_components
from .operational import build_operational_components


def build_soft_components(
    problem: PlanningProblem,
    core: CoreModel,
    workers_by_id: dict[str, Worker],
    needs_by_id: dict[str, Need],
    history_by_worker: dict[str, tuple[HistoricalAssignment, ...]],
    *,
    coverage_target: int,
    weights: SoftObjectiveWeights,
) -> SoftComponents:
    operational = build_operational_components(
        problem,
        core,
        workers_by_id,
        needs_by_id,
        history_by_worker,
        coverage_target=coverage_target,
        weights=weights,
    )
    return build_equity_components(
        problem,
        core,
        operational,
        workers_by_id,
        needs_by_id,
        weights,
    )


__all__ = ["build_soft_components"]

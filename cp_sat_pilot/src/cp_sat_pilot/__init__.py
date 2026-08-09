"""Pilot CP-SAT independent del motor genètic."""

from .domain import (
    Assignment,
    HistoricalAssignment,
    Need,
    OptimizationPhase,
    PlanningProblem,
    SolveResult,
    SoftMetrics,
    Worker,
)
from .model import CpSatPlanner, SoftObjectiveWeights, SolverConfig
from .multistart import (
    MultiStartCandidate,
    MultiStartSelection,
    MultiStartSelectionError,
    lexicographic_quality_key,
    select_best_result,
    solve_adaptive_multi_start,
    solve_multi_start,
)
from .scenarios import ScenarioSpec, apply_scenario, build_standard_scenarios
from .scale import build_scaled_problem, peak_working_set_bytes
from .stability import (
    StabilityAggregate,
    StabilityRun,
    aggregate_stability_runs,
    assignment_fingerprint,
)
from .sqlite_adapter import SqliteInputError, load_problem_from_sqlite

__all__ = [
    "Assignment",
    "CpSatPlanner",
    "HistoricalAssignment",
    "MultiStartCandidate",
    "MultiStartSelection",
    "MultiStartSelectionError",
    "Need",
    "OptimizationPhase",
    "PlanningProblem",
    "SolveResult",
    "SoftObjectiveWeights",
    "SoftMetrics",
    "SolverConfig",
    "SqliteInputError",
    "ScenarioSpec",
    "StabilityAggregate",
    "StabilityRun",
    "Worker",
    "aggregate_stability_runs",
    "apply_scenario",
    "assignment_fingerprint",
    "build_scaled_problem",
    "build_standard_scenarios",
    "lexicographic_quality_key",
    "load_problem_from_sqlite",
    "select_best_result",
    "solve_adaptive_multi_start",
    "solve_multi_start",
    "peak_working_set_bytes",
]

"""Pilot CP-SAT independent del motor genètic."""

from .configuration import PriorityPolicy
from .domain import (
    Assignment,
    EquityWorkerDiagnostic,
    HistoricalAssignment,
    Need,
    OptimizationPhase,
    PlanningProblem,
    SocialDiagnosticSummary,
    SolveResult,
    SoftMetrics,
    Worker,
    is_night_interval,
)
from .model import PlannerCore, SoftObjectiveWeights, SolverConfig
from .priority_planner import PriorityPlanner

# Compatibilitat d'importació: és el mateix motor, no una implementació antiga.
CpSatPlanner = PriorityPlanner
from .quality import EquityExecutionAssessment, assess_equity_execution
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
    "EquityWorkerDiagnostic",
    "CpSatPlanner",
    "PlannerCore",
    "EquityExecutionAssessment",
    "HistoricalAssignment",
    "MultiStartCandidate",
    "MultiStartSelection",
    "MultiStartSelectionError",
    "Need",
    "OptimizationPhase",
    "PlanningProblem",
    "PriorityPlanner",
    "PriorityPolicy",
    "SocialDiagnosticSummary",
    "SolveResult",
    "SoftObjectiveWeights",
    "SoftMetrics",
    "SolverConfig",
    "SqliteInputError",
    "ScenarioSpec",
    "StabilityAggregate",
    "StabilityRun",
    "Worker",
    "is_night_interval",
    "aggregate_stability_runs",
    "assess_equity_execution",
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

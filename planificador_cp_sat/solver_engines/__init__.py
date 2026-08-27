"""Selecció explícita dels motors de planificació disponibles."""

from __future__ import annotations

from enum import StrEnum

from cp_sat_pilot import (
    CpSatPlanner,
    EquityExecutionAssessment,
    PlanningProblem,
    PriorityPlanner,
    SolveResult,
    assess_equity_execution,
)


class SolverEngine(StrEnum):
    CURRENT = "current"
    PRIORITY = "priority"


PUBLISHABLE_SOLVER_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})


_LABELS = {
    SolverEngine.CURRENT: "Vigent",
    SolverEngine.PRIORITY: "Nou per prioritats",
}


def normalize_solver_engine(value: SolverEngine | str) -> SolverEngine:
    try:
        return SolverEngine(value)
    except ValueError as error:
        raise ValueError(f"Motor de planificació desconegut: {value}") from error


def solver_engine_label(value: SolverEngine | str) -> str:
    return _LABELS[normalize_solver_engine(value)]


def create_planner(
    problem: PlanningProblem,
    engine: SolverEngine | str = SolverEngine.CURRENT,
) -> CpSatPlanner:
    selected = normalize_solver_engine(engine)
    if selected is SolverEngine.PRIORITY:
        return PriorityPlanner(problem)
    return CpSatPlanner(problem)


def assess_solver_execution(
    result: SolveResult,
    engine: SolverEngine | str,
) -> EquityExecutionAssessment:
    """Aplica la porta d'optimalitat pròpia de cada motor."""
    selected = normalize_solver_engine(engine)
    if selected is SolverEngine.CURRENT:
        return assess_equity_execution(result)

    phases = tuple(result.optimization_phases)
    coverage_status = next(
        (phase.status for phase in phases if phase.name == "cobertura"),
        "NO_EXECUTADA",
    )
    hours_status = next(
        (phase.status for phase in phases if phase.name == "hores_cobertes"),
        "NO_EXECUTADA",
    )
    priority_phases = tuple(
        phase for phase in phases if phase.name.startswith("prioritat_")
    )
    reasons: list[str] = []
    if coverage_status != "OPTIMAL":
        reasons.append("cobertura_no_resolta")
    if hours_status != "OPTIMAL":
        reasons.append("hores_cobertes_no_optimitzades")
    if not priority_phases or any(
        phase.status != "OPTIMAL" for phase in priority_phases
    ):
        reasons.append("prioritats_no_optimitzades")
    if result.validation_errors:
        reasons.append("errors_restriccions_dures")
    optimality_certified = (
        result.status == "OPTIMAL"
        and result.feasible
        and not result.validation_errors
        and not reasons
    )
    publishable = (
        result.status in PUBLISHABLE_SOLVER_STATUSES
        and result.feasible
        and not result.validation_errors
    )
    return EquityExecutionAssessment(
        status="avaluada" if optimality_certified else "factible_no_optima",
        publishable=publishable,
        operational_phase_status=hours_status,
        equity_phase_status=(
            "OPTIMAL" if optimality_certified else result.status
        ),
        reasons=tuple(reasons),
        principle="prioritats_acumulades_informatives",
        technical_ready=optimality_certified,
    )


__all__ = [
    "PUBLISHABLE_SOLVER_STATUSES",
    "SolverEngine",
    "assess_solver_execution",
    "create_planner",
    "normalize_solver_engine",
    "solver_engine_label",
]

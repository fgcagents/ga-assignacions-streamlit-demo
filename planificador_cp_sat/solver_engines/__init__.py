"""Selecció explícita dels motors de planificació disponibles."""

from __future__ import annotations

from enum import StrEnum

from cp_sat_pilot import (
    EquityExecutionAssessment,
    PlanningProblem,
    PriorityPlanner,
    SolveResult,
    assess_equity_execution,
)


class SolverEngine(StrEnum):
    CURRENT = "current"  # Només registres històrics.
    PRIORITY = "priority"
    ANNUAL = "annual"  # Només lectura de propostes històriques; motor retirat.


PUBLISHABLE_SOLVER_STATUSES = frozenset({"FEASIBLE", "OPTIMAL"})


_LABELS = {
    SolverEngine.CURRENT: "Anterior",
    SolverEngine.PRIORITY: "Vigent",
    SolverEngine.ANNUAL: "Anual (roadmap)",
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
    engine: SolverEngine | str = SolverEngine.PRIORITY,
) -> PriorityPlanner:
    selected = normalize_solver_engine(engine)
    if selected is SolverEngine.PRIORITY:
        return PriorityPlanner(problem)
    if selected is SolverEngine.ANNUAL:
        raise ValueError("El motor anual està retirat del projecte principal")
    raise ValueError("El motor anterior està retirat; utilitza el motor vigent")


def assess_solver_execution(
    result: SolveResult,
    engine: SolverEngine | str,
) -> EquityExecutionAssessment:
    """Aplica la porta d'optimalitat pròpia de cada motor."""
    selected = normalize_solver_engine(engine)
    if selected is SolverEngine.CURRENT:
        return assess_equity_execution(result)

    if selected is SolverEngine.ANNUAL:
        phase = next((p for p in result.optimization_phases if p.name == "equitat_anual"), None)
        return EquityExecutionAssessment(
            status="avaluada" if result.status == "OPTIMAL" else "factible_no_optima",
            publishable=False,
            operational_phase_status=result.optimization_phases[0].status if result.optimization_phases else "NO_EXECUTADA",
            equity_phase_status=phase.status if phase else "NO_EXECUTADA",
            reasons=("roadmap_no_publicable",),
            principle="equitat_anual_ponderada",
            technical_ready=result.feasible and phase is not None,
        )

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

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ortools.sat.python import cp_model

from .constraints import (
    CoreModel,
    HardConstraintSet,
    SoftComponents,
    SoftObjectiveWeights,
    build_soft_components,
)
from .constraints.soft.operational import is_turn_change, is_zone_change
from .domain import (
    Assignment,
    Need,
    OptimizationPhase,
    PlanningProblem,
    SoftMetrics,
    SolveResult,
    Worker,
)


STATUS_NAMES = {
    cp_model.UNKNOWN: "UNKNOWN",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.OPTIMAL: "OPTIMAL",
}


@dataclass(frozen=True, slots=True)
class SolverConfig:
    max_time_seconds: float = 60.0
    equity_time_seconds: float = 15.0
    num_workers: int = 8
    random_seed: int = 0
    log_search_progress: bool = False
    soft_weights: SoftObjectiveWeights = SoftObjectiveWeights()


class CpSatPlanner:
    """Orquestra les fases; les regles viuen a ``constraints``."""

    def __init__(self, problem: PlanningProblem):
        self.problem = problem
        self.workers = {worker.id: worker for worker in problem.workers}
        self.needs = {need.id: need for need in problem.needs}
        self.hard_constraints = HardConstraintSet(problem)
        self.history_by_worker = self.hard_constraints.history_by_worker

    def is_static_candidate(self, worker: Worker, need: Need) -> bool:
        return self.hard_constraints.is_static_candidate(worker, need)

    def candidate_pairs(self) -> tuple[tuple[str, str], ...]:
        return self.hard_constraints.candidate_pairs()

    @staticmethod
    def is_zone_change(worker: Worker, need: Need) -> bool:
        return is_zone_change(worker, need)

    @staticmethod
    def is_turn_change(worker: Worker, need: Need) -> bool:
        return is_turn_change(worker, need)

    def solve(self, config: SolverConfig | None = None) -> SolveResult:
        config = config or SolverConfig()
        self._validate_config(config)
        core = self._build_core_model()
        model = core.model
        phases: list[OptimizationPhase] = []

        model.maximize(sum(core.coverage_vars.values()))
        coverage_solver, coverage_phase = self._solve_phase(
            model, config, "cobertura", maximize=True
        )
        phases.append(coverage_phase)
        if coverage_phase.status not in {"FEASIBLE", "OPTIMAL"}:
            return self._empty_result(core, phases)

        coverage_target = round(coverage_phase.objective_value or 0)
        model.add(sum(core.coverage_vars.values()) == coverage_target)
        soft = self._build_soft_components(
            core,
            coverage_target=coverage_target,
            weights=config.soft_weights,
        )

        model.clear_objective()
        stable_references_exist = any(
            assignment.need_id not in self.problem.affected_need_ids
            for assignment in self.problem.reference_assignments
        )
        stability_solver: cp_model.CpSolver | None = None
        if stable_references_exist:
            model.minimize(soft.plan_alterations)
            stability_solver, stability_phase = self._solve_phase(
                model, config, "estabilitat_pla", maximize=False
            )
        else:
            stability_phase = OptimizationPhase(
                name="estabilitat_pla",
                status="OPTIMAL",
                objective_value=0.0,
                best_objective_bound=0.0,
                relative_gap=0.0,
                wall_time_seconds=0.0,
                conflicts=0,
                branches=0,
            )
        phases.append(stability_phase)

        final_solver = coverage_solver
        soft_solution_available = False
        if stability_phase.status in {"FEASIBLE", "OPTIMAL"}:
            if stability_solver is not None:
                final_solver = stability_solver
                soft_solution_available = True
            stability_target = round(
                stability_phase.objective_value or 0
            )
            model.add(soft.plan_alterations == stability_target)
            if stability_solver is not None:
                self._add_assignment_hints(
                    model, core, stability_solver
                )

            model.clear_objective()
            model.minimize(soft.operational_penalty)
            operational_solver, operational_phase = self._solve_phase(
                model,
                config,
                "preferencies_operatives",
                maximize=False,
            )
            phases.append(operational_phase)
        else:
            operational_solver = None
            operational_phase = None

        if (
            operational_solver is not None
            and operational_phase is not None
            and operational_phase.status in {"FEASIBLE", "OPTIMAL"}
        ):
            final_solver = operational_solver
            soft_solution_available = True
            operational_target = round(
                operational_phase.objective_value or 0
            )
            model.add(soft.operational_penalty == operational_target)
            self._add_assignment_hints(
                model, core, operational_solver
            )

            model.clear_objective()
            model.minimize(soft.opportunistic_equity_objective)
            equity_solver, equity_phase = self._solve_phase(
                model,
                config,
                "equitat_oportunista",
                maximize=False,
                time_limit_seconds=config.equity_time_seconds,
            )
            phases.append(equity_phase)
            if equity_phase.status in {"FEASIBLE", "OPTIMAL"}:
                final_solver = equity_solver

        assignments = self._extract_assignments(
            final_solver, core.assignment_vars
        )
        errors = tuple(self.validate(assignments))
        soft_metrics = (
            self._extract_soft_metrics(final_solver, soft)
            if soft_solution_available
            else None
        )
        status = (
            "OPTIMAL"
            if all(phase.status == "OPTIMAL" for phase in phases)
            else "FEASIBLE"
        )
        return SolveResult(
            status=status,
            assignments=tuple(assignments),
            covered_needs=len(
                {assignment.need_id for assignment in assignments}
            ),
            total_needs=len(self.problem.needs),
            objective_value=coverage_phase.objective_value,
            best_objective_bound=coverage_phase.best_objective_bound,
            relative_gap=coverage_phase.relative_gap,
            wall_time_seconds=sum(
                phase.wall_time_seconds for phase in phases
            ),
            conflicts=sum(phase.conflicts for phase in phases),
            branches=sum(phase.branches for phase in phases),
            candidate_variables=len(core.candidate_pairs),
            incompatibility_constraints=(
                core.incompatibility_constraints
            ),
            validation_errors=errors,
            soft_metrics=soft_metrics,
            optimization_phases=tuple(phases),
        )

    @staticmethod
    def _validate_config(config: SolverConfig) -> None:
        if config.max_time_seconds <= 0:
            raise ValueError("El límit de temps ha de ser positiu")
        if config.equity_time_seconds <= 0:
            raise ValueError(
                "El límit de la fase d'equitat ha de ser positiu"
            )
        if config.num_workers <= 0:
            raise ValueError(
                "El nombre de workers del solver ha de ser positiu"
            )
        weights = config.soft_weights
        if any(
            value < 0
            for value in (
                weights.consecutive_days,
                weights.friday_rule,
                weights.preferred_assignment,
                weights.annual_hours_balance,
                weights.accumulated_zone_equity,
                weights.accumulated_turn_equity,
                weights.zone_changes_tiebreak,
                weights.turn_changes_tiebreak,
            )
        ):
            raise ValueError(
                "Els pesos dels objectius tous no poden ser negatius"
            )

    def _build_core_model(self) -> CoreModel:
        return self.hard_constraints.build_core_model()

    def _build_soft_components(
        self,
        core: CoreModel,
        *,
        coverage_target: int,
        weights: SoftObjectiveWeights,
    ) -> SoftComponents:
        return build_soft_components(
            self.problem,
            core,
            self.workers,
            self.needs,
            self.history_by_worker,
            coverage_target=coverage_target,
            weights=weights,
        )

    @staticmethod
    def _add_assignment_hints(
        model: cp_model.CpModel,
        core: CoreModel,
        solver: cp_model.CpSolver,
    ) -> None:
        model.clear_hints()
        for variable in core.assignment_vars.values():
            model.add_hint(variable, solver.value(variable))

    @staticmethod
    def _configured_solver(
        config: SolverConfig,
        *,
        time_limit_seconds: float | None = None,
    ) -> cp_model.CpSolver:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = (
            config.max_time_seconds
            if time_limit_seconds is None
            else time_limit_seconds
        )
        solver.parameters.num_workers = config.num_workers
        solver.parameters.random_seed = config.random_seed
        solver.parameters.log_search_progress = (
            config.log_search_progress
        )
        return solver

    def _solve_phase(
        self,
        model: cp_model.CpModel,
        config: SolverConfig,
        name: str,
        *,
        maximize: bool,
        time_limit_seconds: float | None = None,
    ) -> tuple[cp_model.CpSolver, OptimizationPhase]:
        solver = self._configured_solver(
            config,
            time_limit_seconds=time_limit_seconds,
        )
        status_code = solver.solve(model)
        status = STATUS_NAMES.get(
            status_code, f"STATUS_{status_code}"
        )
        has_solution = status in {"FEASIBLE", "OPTIMAL"}
        objective = (
            float(solver.objective_value) if has_solution else None
        )
        bound = (
            float(solver.best_objective_bound)
            if has_solution
            else None
        )
        gap = self._relative_gap(
            objective, bound, maximize=maximize
        )
        return solver, OptimizationPhase(
            name=name,
            status=status,
            objective_value=objective,
            best_objective_bound=bound,
            relative_gap=gap,
            wall_time_seconds=float(solver.wall_time),
            conflicts=int(solver.num_conflicts),
            branches=int(solver.num_branches),
        )

    def _extract_assignments(
        self,
        solver: cp_model.CpSolver,
        assignment_vars: dict[
            tuple[str, str], cp_model.IntVar
        ],
    ) -> list[Assignment]:
        assignments: list[Assignment] = []
        for (worker_id, need_id), variable in assignment_vars.items():
            if solver.value(variable):
                need = self.needs[need_id]
                assignments.append(
                    Assignment(
                        worker_id=worker_id,
                        need_id=need_id,
                        service_id=need.service_id,
                        date=need.date,
                        start=need.start,
                        end=need.end,
                        duration_minutes=need.duration_minutes,
                    )
                )
        assignments.sort(
            key=lambda item: (
                item.date,
                item.start,
                item.service_id,
            )
        )
        return assignments

    @staticmethod
    def _extract_soft_metrics(
        solver: cp_model.CpSolver,
        soft: SoftComponents,
    ) -> SoftMetrics:
        return SoftMetrics(
            plan_alterations=solver.value(soft.plan_alterations),
            preferred_assignment_violations=solver.value(
                soft.preferred_assignment_violations
            ),
            consecutive_excess_windows=solver.value(
                soft.consecutive_excess
            ),
            friday_violation=solver.value(soft.friday_violation),
            zone_changes=solver.value(soft.zone_changes),
            turn_changes=solver.value(soft.turn_changes),
            annual_hours_range_minutes=solver.value(
                soft.annual_hours_range_minutes
            ),
            annual_hours_equity_penalty=solver.value(
                soft.annual_hours_equity_penalty
            ),
            accumulated_zone_rate_range_permille=solver.value(
                soft.accumulated_zone_rate_range
            ),
            accumulated_turn_rate_range_permille=solver.value(
                soft.accumulated_turn_rate_range
            ),
            worst_change_equity_gap_permille=solver.value(
                soft.worst_change_equity_gap
            ),
            accumulated_zone_equity_penalty=solver.value(
                soft.accumulated_zone_equity_penalty
            ),
            accumulated_turn_equity_penalty=solver.value(
                soft.accumulated_turn_equity_penalty
            ),
            operational_penalty=solver.value(
                soft.operational_penalty
            ),
            annual_fairness_objective=solver.value(
                soft.annual_fairness_objective
            ),
            change_fairness_objective=solver.value(
                soft.change_fairness_objective
            ),
            change_tiebreak_penalty=solver.value(
                soft.change_tiebreak_penalty
            ),
            normalized_total_changes=solver.value(
                soft.normalized_total_changes
            ),
            opportunistic_equity_objective=solver.value(
                soft.opportunistic_equity_objective
            ),
        )

    def _empty_result(
        self,
        core: CoreModel,
        phases: list[OptimizationPhase],
    ) -> SolveResult:
        phase = phases[0]
        return SolveResult(
            status=phase.status,
            assignments=(),
            covered_needs=0,
            total_needs=len(self.problem.needs),
            objective_value=phase.objective_value,
            best_objective_bound=phase.best_objective_bound,
            relative_gap=phase.relative_gap,
            wall_time_seconds=phase.wall_time_seconds,
            conflicts=phase.conflicts,
            branches=phase.branches,
            candidate_variables=len(core.candidate_pairs),
            incompatibility_constraints=(
                core.incompatibility_constraints
            ),
            optimization_phases=tuple(phases),
        )

    @staticmethod
    def _relative_gap(
        objective: float | None,
        bound: float | None,
        *,
        maximize: bool,
    ) -> float | None:
        if objective is None or bound is None:
            return None
        difference = (
            bound - objective if maximize else objective - bound
        )
        return max(0.0, difference) / max(
            1.0, abs(bound), abs(objective)
        )

    def validate(
        self, assignments: Iterable[Assignment]
    ) -> list[str]:
        return self.hard_constraints.validate(assignments)
